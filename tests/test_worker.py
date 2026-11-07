from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from devrelay.cli import main, parser
from devrelay.config import load
from devrelay.errors import RelayError
from devrelay.runner import run_task
from devrelay.worker import watch

from test_delivery import GitHubDeliveryFixture


class WorkerFixture(GitHubDeliveryFixture):
    def setUp(self):
        super().setUp()
        self.issues = [self.issue, {**self.issue, "number": 13, "title": "Fix second value",
                                   "labels": [{"name": "auto:approved"}, {"name": "priority:P0"}]}]
        self.pulls = {}
        self.events = []
        executable = Path(self.config.data["codex"]["executable"])
        # Each task must have its own implementation and review sessions.
        executable.write_text(executable.read_text().replace("'exact-test-session'", "'worker-session-' + Path.cwd().name"))

    def inventory(self, config, *argv):
        if argv[0] == "issue":
            return self.issues
        return [{"state": "OPEN", "headRefName": "devrelay/task-" + identifier,
                 "body": "Refs #" + identifier} for identifier in self.pulls]

    def pull_read(self, config, *argv):
        if argv[1] == "list":
            identifier = argv[argv.index("--head") + 1].removeprefix("devrelay/task-")
            return [self.pulls[identifier]] if identifier in self.pulls else []
        identifier = argv[2].rsplit("/", 1)[1]
        task = self.state.get(identifier)
        return {"url": self.pulls[identifier]["url"], "state": "OPEN", "headRefName": task["branch"],
                "headRefOid": self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                "baseRefName": "main", "isCrossRepository": False,
                "autoMergeRequest": None}

    def pull_write(self, config, *argv, input=None):
        if argv[1] == "edit":
            self.edit_count += 1
            return ""
        if argv[1] != "create":
            raise AssertionError("Worker must not merge or mark PRs ready")
        identifier = argv[argv.index("--head") + 1].removeprefix("devrelay/task-")
        self.assertIn("--draft", argv)
        self.assertIn("Refs #" + identifier, input)
        self.assertNotIn("Closes #", input)
        url = "https://github.com/example/project/pull/" + identifier
        self.pulls[identifier] = {"number": int(identifier), "url": url}
        self.create_count += 1
        return url

    def watching(self, **options):
        return watch(self.config, self.state, emit=self.events.append, **options)

    def enable_review(self):
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("[[checks]]", "[delivery]\nauto_review = true\n[[checks]]", 1))
        self.config = load(self.root)


class WorkerTests(WorkerFixture):
    def test_two_real_workflows_deliver_in_priority_order_without_waiting_for_merge(self):
        result = self.watching(max_tasks=2)
        self.assertEqual(result, {"status": "stopped", "reason": "max_tasks", "delivered": 2})
        self.assertEqual([event["id"] for event in self.events if event["event"] == "task_finished"], ["13", "12"])
        self.assertEqual(self.create_count, 2)
        for identifier in ("12", "13"):
            task = self.state.get(identifier)
            self.assertEqual(task["status"], "pr_open")
            self.assertTrue(task["verification"]["passed"])
            self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]), task["head_sha"])
        self.assertEqual(self.git("rev-parse", "main"), self.git("--git-dir", str(self.remote), "rev-parse", "main"))
        with patch("devrelay.worker.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.watching()
        self.assertEqual(self.create_count, 2)

    def test_auto_review_runs_once_per_pr_and_does_not_starve_next_issue(self):
        self.enable_review()

        def pull_snapshot(config, task):
            return {"state": "OPEN", "headRefName": task["branch"], "headRefOid": task["head_sha"],
                    "baseRefName": "main", "baseRefOid": task["verification"]["base_sha"],
                    "isCrossRepository": False}

        with patch("devrelay.delivery.pull_snapshot", side_effect=pull_snapshot) as reads:
            self.watching(max_tasks=2)
            calls = reads.call_count
            with patch("devrelay.worker.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                self.watching()
            self.assertEqual(reads.call_count, calls)
        self.assertEqual(self.create_count, 2)
        self.assertEqual([task["status"] for task in self.state.tasks()], ["reviewed", "reviewed"])

    def test_idle_worker_holds_lock_and_picks_up_newly_authorized_issue(self):
        self.issues = []

        def arrive(seconds):
            self.assertEqual(seconds, 3)
            script = "from pathlib import Path; from devrelay.state import State; import sys; s=State(Path(sys.argv[1]));\nwith s.lock(): pass"
            environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            other = subprocess.run([sys.executable, "-c", script, str(self.root)], env=environment,
                                   capture_output=True, text=True, timeout=10)
            self.assertNotEqual(other.returncode, 0)
            self.assertIn("Another DevRelay worker", other.stderr)
            self.issues.append(self.issue)

        with patch("devrelay.worker.sleep", side_effect=arrive) as sleep:
            self.watching(poll_interval=3, max_tasks=1)
        sleep.assert_called_once_with(3)
        self.assertEqual(self.create_count, 1)

    def test_unapproved_blocked_and_dependent_issues_are_not_dispatched(self):
        self.issues = [{**self.issue, "number": 10, "labels": [{"name": "priority:P1"}]},
                       {**self.issue, "number": 11, "labels": self.issue["labels"] + [{"name": "blocked"}]},
                       {**self.issue, "number": 14, "body": "Depends on: #12"}]
        with patch("devrelay.worker.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.watching()
        self.assertEqual(self.create_count, 0)
        self.assertEqual(self.state.tasks(), [])

    def test_failed_checks_stop_dispatch_and_preserve_work(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "retry"}):
            path = self.root / ".devrelay/config.toml"
            path.write_text(path.read_text().replace("max_attempts = 2", "max_attempts = 1"))
            self.config = load(self.root)
            with self.assertRaises(RelayError):
                self.watching()
        failed = self.state.get("13")
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(Path(failed["worktree"]).is_dir())
        self.assertIsNone(self.state.get("12"))
        self.assertEqual(self.watching()["status"], "needs_attention")

    def test_external_blocker_stops_before_next_task(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "blocked"}):
            result = self.watching()
        self.assertEqual(result["status"], "needs_attention")
        self.assertIsNone(self.state.get("12"))
        self.assertEqual(self.create_count, 0)

    def test_recover_verified_task_without_running_implementation_again(self):
        with self.state.lock():
            task = run_task(self.config, self.state, "12")
        attempts = task["attempts"]
        self.watching(max_tasks=1)
        self.assertEqual(self.state.get("12")["attempts"], attempts)
        self.assertIsNone(self.state.get("13"))
        self.assertEqual(next(event for event in self.events if event["event"] == "task_started")["action"], "submit")

    def test_sigterm_stops_idle_worker_restores_handler_and_releases_lock(self):
        self.issues = []
        previous = signal.getsignal(signal.SIGTERM)
        with patch("devrelay.worker.sleep", side_effect=lambda seconds: os.kill(os.getpid(), signal.SIGTERM)):
            with self.assertRaises(KeyboardInterrupt):
                self.watching()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        with self.state.lock():
            pass
        self.assertEqual(self.events[-1]["event"], "watch_stopped")

    def test_cli_streams_json_events_and_final_summary(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--project", str(self.root), "watch", "--max-tasks", "2"])
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(records[0]["event"], "watch_started")
        self.assertEqual(records[-1]["delivered"], 2)

    def test_sigterm_during_implementation_is_saved_and_resumed_before_new_work(self):
        executable = Path(self.config.data["codex"]["executable"])
        executable.write_text(executable.read_text().replace("signal.SIGINT", "signal.SIGTERM"))
        previous = signal.getsignal(signal.SIGTERM)
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "interrupt"}), self.assertRaises(KeyboardInterrupt):
            self.watching()
        task = self.state.get("13")
        self.assertEqual(task["status"], "interrupted")
        self.assertIsNone(self.state.metadata("active_process"))
        self.assertIsNone(self.state.get("12"))
        session = task["session_id"]
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.watching(max_tasks=1)
        recovered = self.state.get("13")
        self.assertEqual(recovered["status"], "pr_open")
        self.assertEqual(recovered["session_id"], session)
        self.assertIsNone(self.state.get("12"))

    def test_recover_pr_created_before_review_without_duplicate_submission(self):
        self.enable_review()
        with self.state.lock(), patch("devrelay.delivery.advance", side_effect=lambda config, state, identifier: state.get(identifier)):
            task = run_task(self.config, self.state, "12", submit_after=True)
        self.assertEqual(task["status"], "pr_open")

        def pull_snapshot(config, task):
            return {"state": "OPEN", "headRefName": task["branch"], "headRefOid": task["head_sha"],
                    "baseRefName": "main", "baseRefOid": task["verification"]["base_sha"],
                    "isCrossRepository": False}

        with patch("devrelay.delivery.pull_snapshot", side_effect=pull_snapshot):
            self.watching(max_tasks=1)
        self.assertEqual(self.create_count, 1)
        self.assertEqual(self.state.get("12")["status"], "reviewed")
        self.assertIsNone(self.state.get("13"))

    def test_rejected_review_stops_dispatch(self):
        self.enable_review()

        def pull_snapshot(config, task):
            return {"state": "OPEN", "headRefName": task["branch"], "headRefOid": task["head_sha"],
                    "baseRefName": "main", "baseRefOid": task["verification"]["base_sha"],
                    "isCrossRepository": False}

        with patch("devrelay.delivery.pull_snapshot", side_effect=pull_snapshot), patch.dict(os.environ, {"DEVRELAY_TEST_REVIEW": "changes_requested"}):
            result = self.watching()
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(self.state.get("13")["status"], "review_blocked")
        self.assertIsNone(self.state.get("12"))

    def test_stale_verified_source_blocks_submission_and_preserves_changes(self):
        with self.state.lock():
            task = run_task(self.config, self.state, "12")
        path = Path(task["worktree"]) / "value.txt"
        path.write_text("new unverified work\n")
        with self.assertRaisesRegex(RelayError, "missing or stale"):
            self.watching(max_tasks=1)
        self.assertEqual(path.read_text(), "new unverified work\n")
        self.assertEqual(self.create_count, 0)
        self.assertIsNone(self.state.get("13"))

    def test_cli_returns_nonzero_for_attention(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "blocked"}), redirect_stdout(io.StringIO()) as out:
            code = main(["--project", str(self.root), "watch"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue().splitlines()[-1])["status"], "needs_attention")

    def test_config_change_stops_worker_before_new_dispatch(self):
        def changed(event):
            if event["event"] == "task_finished":
                path = self.root / ".devrelay/config.toml"
                path.write_text(path.read_text().replace("max_attempts = 2", "max_attempts = 3"))
        with self.assertRaisesRegex(RelayError, "configuration changed"):
            watch(self.config, self.state, emit=changed)
        self.assertEqual(self.create_count, 1)

    def test_network_error_stops_worker_without_claiming_work(self):
        with patch("devrelay.providers.gh_json", side_effect=RelayError("network unavailable")):
            with self.assertRaisesRegex(RelayError, "network unavailable"):
                self.watching()
        self.assertEqual(self.state.tasks(), [])

    def test_refuse_local_provider_and_invalid_limits(self):
        for options in ({"poll_interval": 0}, {"max_tasks": 0}, {"max_tasks": -1}):
            with self.subTest(options=options), self.assertRaises(RelayError):
                self.watching(**options)
        self.config.data["project"]["provider"] = "local"
        with self.assertRaisesRegex(RelayError, "provider=github"):
            self.watching()

    def test_parser_rejects_nonpositive_limits(self):
        for option in ("--poll-interval", "--max-tasks"):
            for value in ("0", "-1", "1.5"):
                with self.subTest(option=option, value=value), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser().parse_args(["watch", option, value])


if __name__ == "__main__":
    unittest.main()
