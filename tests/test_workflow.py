import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import call, patch

from devrelay.codex import build_command
from devrelay.config import load
from devrelay.delivery import post_merge
from devrelay.errors import RelayError
from devrelay.git import _gitlink_paths, _isolated_source_tree, source_tree
from devrelay.process import execute, terminate
from devrelay.providers import add_local, queue_report, snapshot
from devrelay.runner import complete_local, run_task, start, submit, verify
from devrelay.state import State, group_alive

from helpers import RepositoryTest


class WorkflowTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.executable = self.fake_codex()
        self.configure(executable=self.executable)
        add_local(self.config, self.state, "fix-1", "Fix the value", "Fix value.txt; configured regression must pass.", "priority:P1")

    def run_task(self, identifier="fix-1", **kwargs):
        with self.state.lock():
            return run_task(self.config, self.state, identifier, **kwargs)

    def test_explicit_plan_id_is_rejected_before_worktree_or_codex(self):
        add_local(self.config, self.state, "plan", "Overall plan",
                  "Task kind: plan\nDepends on: none", "priority:P1")
        with patch("devrelay.runner.codex.implement") as implement:
            with self.assertRaisesRegex(RelayError, "Tracking plan parents are not executable"):
                self.run_task("plan")
        implement.assert_not_called()
        self.assertFalse((self.root / ".devrelay/worktrees/plan").exists())
        self.assertEqual(self.state.get("plan")["status"], "queued")

    def test_resume_rechecks_parent_status_and_preserves_existing_work(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        worktree = Path(task["worktree"])
        (worktree / "unfinished.txt").write_text("Keep this work\n")
        task["body"] = "Task kind: plan\nDepends on: none"
        self.state.save(task)
        with patch("devrelay.runner.codex.implement") as implement:
            with self.assertRaisesRegex(RelayError, "Tracking plan parents are not executable"):
                self.run_task(resume=True)
        implement.assert_not_called()
        self.assertEqual((worktree / "unfinished.txt").read_text(), "Keep this work\n")

    def test_real_worktree_and_verified_local_delivery(self):
        result = self.run_task()
        self.assertEqual(result["status"], "verified")
        self.assertEqual((self.root / "value.txt").read_text(), "broken\n")
        self.assertEqual((Path(result["worktree"]) / "value.txt").read_text(), "fixed\n")
        self.assertTrue(result["verification"]["passed"])
        self.assertTrue(Path(result["verification_path"]).is_file())
        with self.state.lock():
            committed = submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(committed["status"], "committed")
        with self.assertRaisesRegex(RelayError, "integrated"):
            complete_local(self.config, self.state, "fix-1")
        self.git("merge", "--ff-only", committed["branch"])
        with self.state.lock():
            completed = complete_local(self.config, self.state, "fix-1")
        self.assertFalse(completed["open"])

    def test_failed_check_repairs_in_exact_saved_session(self):
        trace = Path(self.temporary.name) / "trace.jsonl"
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "retry", "DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task()
        invocations = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(invocations), 2)
        argv = invocations[1]["argv"]
        self.assertEqual(argv[argv.index("resume") + 1], "exact-test-session")
        self.assertNotIn("--last", argv)
        self.assertIn("failed", invocations[1]["prompt"])

    def test_sandbox_validation_diagnostics_still_require_actual_host_acceptance(self):
        script = self.executable.read_text().replace("Fixed the bounded behavior",
                                                     "Sandbox validation cannot execute ps; implementation is ready for host checks")
        self.executable.write_text(script)
        task = self.run_task()
        self.assertEqual(task["status"], "verified")
        self.assertIn("Sandbox validation", task["summary"])
        self.assertTrue(task["verification"]["passed"])
        self.assertEqual(task["verification"]["checks"][0]["returncode"], 0)
        prompt = (Path(task["last_run"]) / "prompt.txt").read_text()
        self.assertIn("return outcome=ready with blockers=[]", prompt)
        self.assertIn("host failures will stop submission", prompt)
        self.assertIn("implementation itself cannot proceed", prompt)

    def test_sandbox_diagnostics_cannot_excuse_failed_host_checks(self):
        script = self.executable.read_text().replace("Fixed the bounded behavior",
                                                     "Sandbox validation cannot execute ps; implementation is ready for host checks")
        self.executable.write_text(script)
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_attempts = 2", "max_attempts = 1"))
        self.config = load(self.root)
        # This is a genuine controller-side assertion failure, not a sandbox error.
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "retry"}), self.assertRaises(RelayError):
            self.run_task(submit_after=True)
        task = self.state.get("fix-1")
        self.assertEqual(task["status"], "failed")
        self.assertIsNone(task.get("verification"))
        self.assertIsNone(task.get("pr_url"))

    def test_codex_failure_retains_session_and_work_for_resume(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "command-failure"}):
            with self.assertRaises(RelayError):
                self.run_task()
        task = self.state.get("fix-1")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["session_id"], "exact-test-session")
        self.assertTrue(Path(task["worktree"]).is_dir())
        result = self.run_task(resume=True)
        self.assertEqual(result["status"], "verified")

    def test_stderr_diagnostic_does_not_corrupt_events_or_exact_resume(self):
        trace = Path(self.temporary.name) / "stderr-trace.jsonl"
        with (patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "stderr-command-failure",
                                     "DEVRELAY_TEST_TRACE": str(trace)}),
              self.assertRaises(RelayError)):
            self.run_task()
        task = self.state.get("fix-1")
        events = Path(task["event_log"])
        diagnostics = events.with_suffix(events.suffix + ".stderr.log")
        self.assertEqual(task["session_id"], "exact-test-session")
        self.assertNotIn("ordinary CLI diagnostic", events.read_text())
        self.assertIn("ordinary CLI diagnostic", diagnostics.read_text())

        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().splitlines()[1])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocation["argv"][invocation["argv"].index("resume") + 1],
                         "exact-test-session")

    def test_chunked_unicode_agent_message_preserves_exact_resume(self):
        trace = Path(self.temporary.name) / "unicode-trace.jsonl"
        with (patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "unicode-chunks",
                                     "DEVRELAY_TEST_TRACE": str(trace)}),
              self.assertRaises(RelayError)):
            self.run_task()
        task = self.state.get("fix-1")
        self.assertEqual(task["session_id"], "exact-test-session")
        events = [json.loads(line) for line in Path(task["event_log"]).read_text().split("\n") if line]
        self.assertEqual(events[1]["item"]["text"], "first\u0085second\u2028third\u2029last")
        self.assertEqual(events[-1]["type"], "turn.failed")
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().split("\n")[1])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocation["argv"][invocation["argv"].index("resume") + 1], "exact-test-session")
        self.assertNotIn("--last", invocation["argv"])

    def test_upgrade_from_legacy_mixed_log_resumes_exact_session(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        directory = self.state.directory / "runs" / task["id"] / "legacy-codex"
        directory.mkdir(parents=True)
        log = directory / "events.jsonl"
        log.write_text("ordinary CLI diagnostic\n" +
                       json.dumps({"type": "thread.started", "thread_id": "legacy-session"}) + "\n" +
                       "2026-01-01 WARN ordinary CLI diagnostic\n")
        task.update(status="interrupted", setup_fingerprint=self.config.fingerprint, session_id="legacy-session",
                    last_run=str(directory), event_log=str(log))
        self.state.save(task)
        trace = Path(self.temporary.name) / "upgrade-trace.jsonl"
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().split("\n")[0])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocation["argv"][invocation["argv"].index("resume") + 1], "legacy-session")
        self.assertNotIn("--last", invocation["argv"])
        marker = Path(result["event_log"]).with_suffix(".jsonl.format.json")
        self.assertEqual(json.loads(marker.read_text()), {"format": "stdout-jsonl-v1"})

    def test_foreign_truncated_message_does_not_block_exact_session_resume(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        add_local(self.config, self.state, "fix-other", "Other", "Depends on: none", "priority:P1")
        other_directory = self.state.directory / "runs" / "fix-other" / "crashed-codex"
        other_directory.mkdir(parents=True)
        other_log = other_directory / "events.jsonl"
        other_log.write_text(json.dumps({"type": "thread.started", "thread_id": "other-session"}) + "\n" +
                             '{"type":"item.completed","item":')
        other = self.state.get("fix-other")
        other.update(status="interrupted", last_run=str(other_directory), event_log=str(other_log))
        self.state.save(other)
        current = self.state.directory / "runs" / task["id"] / "current-codex"
        current.mkdir(parents=True)
        log = current / "events.jsonl"
        log.write_text(json.dumps({"type": "thread.started", "thread_id": "current-session"}) + "\n")
        task.update(status="interrupted", setup_fingerprint=self.config.fingerprint, session_id="current-session",
                    last_run=str(current), event_log=str(log))
        self.state.save(task)
        trace = Path(self.temporary.name) / "two-task-trace.jsonl"
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().split("\n")[0])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocation["argv"][invocation["argv"].index("resume") + 1], "current-session")
        self.assertNotIn("--last", invocation["argv"])
        self.assertIsNone(self.state.get("fix-other").get("session_id"))
        task = self.state.get("fix-1")
        task["session_id"] = "other-session"
        self.state.save(task)
        with (patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}),
              self.assertRaisesRegex(RelayError, "another task's implementation log")):
            self.run_task(resume=True)
        self.assertEqual(len(trace.read_text().split("\n")) - 1, 1, "Foreign session must not reach Codex")

    def test_interrupt_after_thread_started_resumes_the_exact_session(self):
        trace = Path(self.temporary.name) / "interrupt-trace.jsonl"
        with (patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "interrupt", "DEVRELAY_TEST_TRACE": str(trace)}),
              self.assertRaises(KeyboardInterrupt)):
            self.run_task()
        task = self.state.get("fix-1")
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["session_id"], "exact-test-session")
        self.assertEqual(task["event_log"], str(Path(task["last_run"]) / "events.jsonl"))

        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocations = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(result["status"], "verified")
        self.assertEqual(invocations[1]["argv"][invocations[1]["argv"].index("resume") + 1],
                         "exact-test-session")
        self.assertNotIn("--last", invocations[1]["argv"])

    def test_controller_crash_log_recovers_session_before_new_codex_run(self):
        trace = Path(self.temporary.name) / "crash-trace.jsonl"
        source = Path(__file__).resolve().parents[1] / "src"
        command = [sys.executable, "-c", "from devrelay.cli import main; raise SystemExit(main())",
                   "--project", str(self.root)]
        environment = {**os.environ, "PYTHONPATH": str(source), "DEVRELAY_TEST_TRACE": str(trace)}
        environment.pop("DEVRELAY_TEST_MODE", None)
        controller = subprocess.Popen([*command, "run", "fix-1"], env={**environment,
                                      "DEVRELAY_TEST_MODE": "controller-crash"},
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        group = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                task = self.state.get("fix-1")
                active = self.state.metadata("active_process")
                if task and task.get("last_run") and task.get("event_log") and active:
                    events = Path(task["event_log"])
                    if events.exists() and '"thread_id": "started-session"' in events.read_text():
                        group = active["group"]
                        break
                if controller.poll() is not None:
                    output, error = controller.communicate()
                    self.fail(f"controller exited before crash boundary: {output} {error}")
                time.sleep(0.01)
            else:
                self.fail("controller did not persist its run index and first thread event")

            self.assertEqual(task["event_log"], str(Path(task["last_run"]) / "events.jsonl"))
            self.assertIsNone(task.get("session_id"))
            controller.kill()
            controller.communicate(timeout=5)
            self.assertEqual(controller.returncode, -signal.SIGKILL)

            blocked = subprocess.run([*command, "resume", "fix-1"], env=environment,
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("still runs in process group", blocked.stderr)
            self.assertEqual(len(trace.read_text().splitlines()), 1)

            os.killpg(group, signal.SIGKILL)
            time.sleep(0.1)
            resumed = subprocess.run([*command, "resume", "fix-1"], env=environment,
                                     capture_output=True, text=True, timeout=15)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            invocations = [json.loads(line) for line in trace.read_text().splitlines()]
            argv = invocations[1]["argv"]
            self.assertEqual(argv[argv.index("resume") + 1], "started-session")
            self.assertNotIn("--last", argv)
            self.assertEqual(self.state.get("fix-1")["status"], "verified")
        finally:
            if controller.poll() is None:
                controller.kill()
                controller.communicate(timeout=5)
            active = self.state.metadata("active_process")
            for candidate in {group, active.get("group") if active else None} - {None}:
                try:
                    os.killpg(candidate, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_incomplete_or_conflicting_recovery_events_fail_closed(self):
        cases = [
            ("incomplete", '{"type":"thread.started","thread_id":'),
            ("conflicting", '\n'.join((
                json.dumps({"type": "thread.started", "thread_id": "session-one"}),
                json.dumps({"type": "thread.started", "thread_id": "session-two"})))),
        ]
        for name, content in cases:
            with self.subTest(name=name):
                identifier = "fix-" + name
                add_local(self.config, self.state, identifier, "Recovery", "Depends on: none", "priority:P1")
                with self.state.lock():
                    task = start(self.config, self.state, identifier)
                task["setup_fingerprint"] = self.config.fingerprint
                task["status"] = "implementing"
                directory = self.state.directory / "runs" / identifier / "crashed-codex"
                directory.mkdir(parents=True)
                (directory / "events.jsonl").write_text(content)
                task["last_run"] = str(directory)
                self.state.save(task)
                with self.assertRaisesRegex(RelayError, "incomplete|different session"):
                    self.run_task(identifier, resume=True)

    def test_empty_prelaunch_log_can_be_retried_as_a_new_session(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        task["setup_fingerprint"] = self.config.fingerprint
        task["status"] = "implementing"
        directory = self.state.directory / "runs" / task["id"] / "prelaunch-codex"
        directory.mkdir(parents=True)
        (directory / "events.jsonl").touch()
        task["last_run"] = str(directory)
        self.state.save(task)
        trace = Path(self.temporary.name) / "prelaunch-trace.jsonl"

        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.run_task(resume=True)
        invocation = json.loads(trace.read_text().splitlines()[0])
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("resume", invocation["argv"])

    def test_recovery_never_reuses_another_tasks_session(self):
        add_local(self.config, self.state, "fix-other", "Other", "Depends on: none", "priority:P1")
        other = self.state.get("fix-other")
        other["session_id"] = "other-task-session"
        self.state.save(other)
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        task["setup_fingerprint"] = self.config.fingerprint
        task["status"] = "implementing"
        directory = self.state.directory / "runs" / task["id"] / "crashed-codex"
        directory.mkdir(parents=True)
        (directory / "events.jsonl").write_text(
            json.dumps({"type": "thread.started", "thread_id": "other-task-session"}) + "\n")
        task["last_run"] = str(directory)
        self.state.save(task)

        with self.assertRaisesRegex(RelayError, "another task"):
            self.run_task(resume=True)

    def test_cached_session_is_validated_before_cli_launch_without_thread_started(self):
        add_local(self.config, self.state, "fix-other", "Other", "Depends on: none", "priority:P1")
        other = self.state.get("fix-other")
        other["session_id"] = "other-task-session"
        self.state.save(other)
        with self.state.lock():
            original = start(self.config, self.state, "fix-1")
        original["setup_fingerprint"] = self.config.fingerprint
        trace = Path(self.temporary.name) / "rejected-session-trace.jsonl"
        log_cases = ("no-run", "missing", "empty", "turn-only")
        sessions = ("other-task-session", "--last", "", 0, ["session"])
        for log_case in log_cases:
            for index, session in enumerate(sessions):
                with self.subTest(log=log_case, session=session):
                    task = {**original, "status": "interrupted", "session_id": session}
                    directory = self.state.directory / "runs" / task["id"] / f"{log_case}-{index}"
                    if log_case != "no-run":
                        directory.mkdir(parents=True)
                        log = directory / "events.jsonl"
                        if log_case == "empty":
                            log.touch()
                        elif log_case == "turn-only":
                            log.write_text(json.dumps({"type": "turn.started"}) + "\n")
                        task.update(last_run=str(directory), event_log=str(log))
                    self.state.save(task)
                    reason = "another task" if session == "other-task-session" else "session ID is invalid"
                    with (patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}),
                          self.assertRaisesRegex(RelayError, reason)):
                        self.run_task(resume=True)
                    self.assertFalse(trace.exists(), "Codex must not receive a rejected session's prompt")
                    saved = self.state.get(task["id"])
                    self.assertEqual(saved.get("last_run"), task.get("last_run"))
                    self.assertEqual(saved.get("event_log"), task.get("event_log"))

    def test_blocked_worker_does_not_run_acceptance(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "blocked"}):
            task = self.run_task()
        self.assertEqual(task["status"], "blocked")
        self.assertIn("dataset", task["error"])
        self.assertNotIn("verification", task if task.get("verification") else {})

    def test_worker_exit_zero_is_insufficient_without_completed_turn_and_result(self):
        for mode in ("bad-result", "no-completed-turn"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"DEVRELAY_TEST_MODE": mode}):
                with self.assertRaises(RelayError):
                    self.run_task(resume=self.state.get("fix-1").get("worktree") is not None)
                self.assertEqual(self.state.get("fix-1")["status"], "failed")

    def test_source_drift_rejects_submission(self):
        task = self.run_task()
        (Path(task["worktree"]) / "new-unverified.txt").write_text("new code")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(self.git("log", "--format=%s", "-1", cwd=task["worktree"]), "project config")

    def test_check_configuration_drift_rejects_submission(self):
        self.run_task()
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace('id = "checks-0"', 'id = "changed-acceptance"'))
        config = load(self.root)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            submit(config, self.state, "fix-1", local=True)

    def test_base_movement_requires_combined_reverification(self):
        self.run_task()
        (self.root / "other.txt").write_text("new base content")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "base moved")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            submit(self.config, self.state, "fix-1", local=True)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "integrate"):
            verify(self.config, self.state, "fix-1")

    def test_check_that_mutates_source_is_rejected(self):
        self.config = self.configure(executable=self.executable, attempts=1,
                                     checks=[[sys.executable, "-c", "from pathlib import Path; Path('value.txt').write_text('mutation')"]])
        with self.assertRaisesRegex(RelayError, "changed during verification"):
            self.run_task()

    def test_start_preserves_uncommitted_work_and_excludes_duplicate_dispatch(self):
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
            marker = Path(task["worktree"]) / "ongoing.txt"
            marker.write_text("existing work")
            recovered = start(self.config, self.state, "fix-1")
        self.assertEqual(task["worktree"], recovered["worktree"])
        self.assertEqual(marker.read_text(), "existing work")
        self.assertIsNone(queue_report(self.config, snapshot(self.config, self.state))["selected"])

    def test_no_acceptance_means_no_model_invocation(self):
        self.config = self.configure(executable=self.executable, checks=[])
        with self.assertRaisesRegex(RelayError, "at least one"):
            self.run_task()
        self.assertFalse((self.root / ".devrelay/worktrees/fix-1").exists())

    def test_local_dependencies_wait_for_integration(self):
        add_local(self.config, self.state, "fix-2", "Next fix", "Depends on: #fix-1", "priority:P1")
        self.assertNotIn("fix-2", [item["id"] for item in queue_report(self.config, snapshot(self.config, self.state))["eligible"]])
        task = self.run_task()
        with self.state.lock():
            submit(self.config, self.state, "fix-1", local=True)
        self.git("merge", "--ff-only", task["branch"])
        with self.state.lock():
            complete_local(self.config, self.state, "fix-1")
        self.assertEqual(queue_report(self.config, snapshot(self.config, self.state))["selected"]["id"], "fix-2")

    def test_source_snapshot_preserves_the_real_index(self):
        before = self.git("write-tree")
        (self.root / "value.txt").write_text("changed")
        (self.root / "new.txt").write_text("new")
        computed = source_tree(self.root)
        self.assertEqual(self.git("write-tree"), before)
        self.git("add", "-A")
        self.assertEqual(self.git("write-tree"), computed)

    def _assert_source_snapshot_accepts_parent_filename(self, filename):
        self.write_filename_fixture(self.root, filename, b"delivered\n")
        self.git("add", "--", os.fsdecode(filename))
        self.git("commit", "-qm", "add filename fixture")
        index = self.root / ".git/index"
        original = index.read_bytes()
        self.assertEqual(source_tree(self.root), self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(index.read_bytes(), original)

    def test_source_snapshot_accepts_non_utf8_parent_filenames(self):
        self._assert_source_snapshot_accepts_parent_filename(b"parent-\xff.txt")

    def test_source_snapshot_accepts_carriage_returns_in_parent_filenames(self):
        self._assert_source_snapshot_accepts_parent_filename(b"parent-\r.txt")

    def test_non_utf8_fixture_reports_eilseq_as_platform_skip(self):
        with patch.object(Path, "write_bytes", side_effect=OSError(errno.EILSEQ, "Illegal byte sequence")):
            with self.assertRaisesRegex(unittest.SkipTest, "EILSEQ.*end-to-end case unavailable"):
                self.write_filename_fixture(self.root, b"parent-\xff.txt", b"delivered\n")

    def test_filename_fixture_does_not_skip_permission_or_valid_utf8_errors(self):
        for filename, error_number in ((b"parent-\xff.txt", errno.EPERM),
                                       (b"parent-\xff.txt", errno.EACCES),
                                       (b"parent-\r.txt", errno.EILSEQ)):
            with self.subTest(filename=filename, errno=error_number):
                error = OSError(error_number, "fixture creation failed")
                with patch.object(Path, "write_bytes", side_effect=error):
                    with self.assertRaises(OSError) as raised:
                        self.write_filename_fixture(self.root, filename, b"delivered\n")
                self.assertIs(raised.exception, error)

    def test_command_builder_preserves_configured_model_and_profile(self):
        self.config.data["codex"].update(model="requested-model", profile="requested-profile")
        command = build_command(self.config, Path("/log directory"), "session-for-this-task")
        self.assertIn("requested-model", command)
        self.assertIn("requested-profile", command)
        self.assertEqual(command[command.index("resume") + 1], "session-for-this-task")
        self.assertNotIn("--last", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_saved_session_cannot_become_a_cli_option(self):
        with self.assertRaisesRegex(RelayError, "session ID is invalid"):
            build_command(self.config, Path("/logs"), "--last")

    def test_partial_worktree_creation_can_be_retried(self):
        from devrelay import runner
        real_git = runner.git
        def fail_add(root, *argv):
            if argv[:2] == ("worktree", "add"):
                raise RelayError("Interrupted creation")
            return real_git(root, *argv)
        with self.state.lock(), patch.object(runner, "git", side_effect=fail_add):
            with self.assertRaisesRegex(RelayError, "Interrupted creation"):
                start(self.config, self.state, "fix-1")
        self.assertEqual(self.state.get("fix-1")["status"], "start_failed")
        with self.state.lock():
            result = start(self.config, self.state, "fix-1")
        self.assertTrue(Path(result["worktree"]).is_dir())

    def test_submission_error_does_not_restart_the_model(self):
        from devrelay import runner
        trace = Path(self.temporary.name) / "submission-trace"
        with (patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}),
              patch("devrelay.workflow.submit", side_effect=RelayError("PR request timed out")),
              self.assertRaisesRegex(RelayError, "PR request")):
            self.run_task(submit_after=True)
        self.assertEqual(len(trace.read_text().splitlines()), 1)
        self.assertTrue(self.state.get("fix-1")["verification"]["passed"])

    def test_failed_work_requires_explicit_recovery_before_automatic_dispatch(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "command-failure"}):
            with self.assertRaises(RelayError):
                self.run_task()
        with self.state.lock():
            result = run_task(self.config, self.state)
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["tasks"], ["fix-1"])

    def test_task_requirement_drift_rejects_submission(self):
        self.run_task()
        task = self.state.get("fix-1")
        task["body"] += "\nAdditional acceptance requirement"
        self.state.save(task)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            submit(self.config, self.state, "fix-1", local=True)

    def test_withdrawn_approval_rejects_submission(self):
        self.run_task()
        task = self.state.get("fix-1")
        task["labels"].remove("auto:approved")
        self.state.save(task)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "not eligible"):
            submit(self.config, self.state, "fix-1", local=True)


class SubmoduleWorkflowTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.dependency_source = Path(self.temporary.name) / "dependency-source"
        self._initialize_repository(self.dependency_source, "dependency.txt", "broken\n")

    def _initialize_repository(self, path, filename, content):
        path.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "DevRelay Test"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "devrelay@example.invalid"], cwd=path, check=True)
        subprocess.run(["git", "config", "receive.denyCurrentBranch", "updateInstead"], cwd=path, check=True)
        (path / filename).write_text(content)
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)

    def _add_nested_submodule(self):
        nested_source = Path(self.temporary.name) / "nested-source"
        self._initialize_repository(nested_source, "nested.txt", "nested\n")
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(nested_source), "nested/inner"], cwd=self.dependency_source, check=True)
        subprocess.run(["git", "commit", "-qam", "add nested dependency"],
                       cwd=self.dependency_source, check=True)

    def _prepare(self, checks=None, *, initialize=True, gitmodules_ignore=True, setup=None):
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(self.dependency_source), "vendor/dependency"], cwd=self.root, check=True)
        if gitmodules_ignore:
            subprocess.run(["git", "config", "-f", ".gitmodules",
                            "submodule.vendor/dependency.ignore", "all"], cwd=self.root, check=True)
        self.git("commit", "-qam", "add dependency")
        self.configure(checks=checks or [[sys.executable, "-c", "pass"]], setup=setup)
        add_local(self.config, self.state, "fix-1", "Fix dependency", "Update the dependency.\nDepends on: none",
                  "priority:P1")
        with self.state.lock():
            task = start(self.config, self.state, "fix-1")
        worktree = Path(task["worktree"])
        dependency = worktree / "vendor/dependency"
        if initialize:
            subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init",
                            "--recursive"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "DevRelay Test"], cwd=dependency, check=True)
            subprocess.run(["git", "config", "user.email", "devrelay@example.invalid"], cwd=dependency, check=True)
        return worktree, dependency

    def test_tracked_submodule_change_cannot_be_hidden_by_ignore_configuration(self):
        worktree, dependency = self._prepare()
        (dependency / "dependency.txt").write_text("fixed\n")
        self.git("config", "submodule.vendor/dependency.ignore", "all", cwd=worktree)
        self.git("config", "status.showUntrackedFiles", "no", cwd=dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def _install_submodule_replacement(self, dependency):
        original = self.git("rev-parse", "HEAD", cwd=dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        self.git("add", "dependency.txt", cwd=dependency)
        self.git("commit", "-qm", "replacement content", cwd=dependency)
        replacement = self.git("rev-parse", "HEAD", cwd=dependency)
        self.git("checkout", "--detach", "-q", original, cwd=dependency)
        self.git("replace", original, replacement, cwd=dependency)
        self.git("read-tree", replacement, cwd=dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=dependency), original)
        return original, replacement

    def test_replacement_refs_cannot_hide_undelivered_submodule_source(self):
        worktree, dependency = self._prepare([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]])
        original, replacement = self._install_submodule_replacement(dependency)
        self.assertEqual(self.git("status", "--porcelain", cwd=dependency), "")
        index = self._git_metadata_path(dependency, "index")
        original_index = index.read_bytes()
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual(self.git("rev-parse", f"refs/replace/{original}", cwd=dependency), replacement)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_replacement_refs_do_not_change_clean_submodule_evidence(self):
        worktree, dependency = self._prepare()
        before = source_tree(worktree)
        original, replacement = self._install_submodule_replacement(dependency)
        self.git("--no-replace-objects", "read-tree", original, cwd=dependency)
        (dependency / "dependency.txt").write_text("broken\n")
        self.assertEqual(source_tree(worktree), before)
        self.assertEqual(self.git("rev-parse", f"refs/replace/{original}", cwd=dependency), replacement)

    def test_replacement_refs_after_verification_block_submission(self):
        _, dependency = self._prepare()
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        original, replacement = self._install_submodule_replacement(dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(self.git("rev-parse", f"refs/replace/{original}", cwd=dependency), replacement)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def _assert_source_snapshot_accepts_submodule_filename(self, filename):
        path = os.fsdecode(filename)
        self.write_filename_fixture(self.dependency_source, filename, b"delivered\n")
        self.git("add", "--", path, cwd=self.dependency_source)
        self.git("commit", "-qm", "add filename fixture", cwd=self.dependency_source)
        worktree, dependency = self._prepare()
        index = self._git_metadata_path(dependency, "index")
        original_index = index.read_bytes()
        self.assertEqual(source_tree(worktree), self.git("rev-parse", "HEAD^{tree}", cwd=worktree))
        self.assertEqual(index.read_bytes(), original_index)
        (dependency / path).write_bytes(b"undelivered\n")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)

    def test_source_snapshot_accepts_non_utf8_submodule_filenames(self):
        self._assert_source_snapshot_accepts_submodule_filename(b"dependency-\xff.txt")

    def test_source_snapshot_accepts_carriage_returns_in_submodule_filenames(self):
        self._assert_source_snapshot_accepts_submodule_filename(b"dependency-\r.txt")

    def test_gitlink_index_paths_preserve_non_utf8_and_carriage_return_bytes(self):
        worktree, dependency = self._prepare()
        head = self.git("rev-parse", "HEAD", cwd=dependency)
        blob = self.git("rev-parse", "HEAD:value.txt", cwd=worktree)
        self.git("update-index", "--add", "--cacheinfo", "100644", blob,
                 os.fsdecode(b"parent-\xff.txt"), cwd=worktree)
        paths = [b"vendor/dependency-\xff", b"vendor/dependency-\r"]
        for path in paths:
            self.git("update-index", "--add", "--cacheinfo", "160000", head, os.fsdecode(path), cwd=worktree)
        self.assertCountEqual([os.fsencode(path) for path in _gitlink_paths(worktree)],
                              [b"vendor/dependency", *paths])

    def test_submodule_tree_byte_paths_are_inspected_without_checkout(self):
        worktree, dependency = self._prepare()
        head = self.git("rev-parse", "HEAD", cwd=dependency)
        blob = self.git("rev-parse", "HEAD:dependency.txt", cwd=dependency)
        link_paths = [b"child-\xff", b"child-\r"]
        entries = b"".join(b"100644 blob " + blob.encode("ascii") + b"\t" + path + b"\0"
                           for path in (b"dependency.txt", b"missing-\xff.txt", b"missing-\r.txt"))
        entries += b"".join(b"160000 commit " + head.encode("ascii") + b"\t" + path + b"\0"
                            for path in link_paths)
        tree = subprocess.check_output(["git", "mktree", "-z"], input=entries, cwd=dependency).strip().decode("ascii")
        commit = self.git("commit-tree", tree, "-p", head, "-m", "byte paths without checkout", cwd=dependency)
        self.git("update-ref", "HEAD", commit, cwd=dependency)
        self.git("read-tree", tree, cwd=dependency)
        self.assertCountEqual([os.fsencode(path) for path in _gitlink_paths(dependency)], link_paths)
        index = self._git_metadata_path(dependency, "index")
        original = index.read_bytes()
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual(index.read_bytes(), original)

    def _assert_index_mask_cannot_hide_tracked_submodule_change(self, flag):
        checks = [[sys.executable, "-c",
                   "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]]
        _, dependency = self._prepare(checks)
        self.git("update-index", flag, "dependency.txt", cwd=dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_assume_unchanged_cannot_hide_tracked_submodule_content(self):
        self._assert_index_mask_cannot_hide_tracked_submodule_change("--assume-unchanged")

    def test_skip_worktree_cannot_hide_tracked_submodule_content(self):
        self._assert_index_mask_cannot_hide_tracked_submodule_change("--skip-worktree")

    def _prepare_symlink_dependency(self, checks=None, *, link_target="dependency.txt"):
        (self.dependency_source / "link.txt").symlink_to(link_target)
        self.git("-c", "core.symlinks=true", "add", "link.txt", cwd=self.dependency_source)
        self.git("commit", "-qm", "add dependency symlink", cwd=self.dependency_source)
        worktree, dependency = self._prepare(checks)
        self.git("config", "core.symlinks", "false", cwd=dependency)
        self.assertTrue((dependency / "link.txt").is_symlink())
        return worktree, dependency

    def test_core_symlinks_false_cannot_hide_replacement_between_source_snapshots(self):
        worktree, dependency = self._prepare_symlink_dependency()
        source_tree(worktree)
        link = dependency / "link.txt"
        link.unlink()
        link.write_text("dependency.txt")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), "dependency.txt")

    def test_core_symlinks_false_cannot_hide_replacement_before_verification(self):
        _, dependency = self._prepare_symlink_dependency([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/link.txt').read_text() == 'dependency.txt'"]])
        link = dependency / "link.txt"
        link.unlink()
        link.write_text("dependency.txt")
        index = self._git_metadata_path(dependency, "index")
        original = index.read_bytes()
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(link.read_text(), "dependency.txt")

    def test_core_symlinks_false_cannot_hide_replacement_after_verification(self):
        _, dependency = self._prepare_symlink_dependency([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/link.txt').read_text() == 'broken\\n'"]])
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        link = dependency / "link.txt"
        link.unlink()
        link.write_text("dependency.txt")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(link.read_text(), "dependency.txt")

    def test_submodule_symlink_targets_are_compared_as_raw_bytes(self):
        original_target = b"missing-\xff \n"
        worktree, dependency = self._prepare_symlink_dependency(link_target=os.fsdecode(original_target))
        source_tree(worktree)
        link = dependency / "link.txt"
        link.unlink()
        link.symlink_to(os.fsdecode(original_target.rstrip()))
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual(os.readlink(os.fsencode(link)), original_target.rstrip())

    def _install_masking_clean_filter(self, dependency, *, info_attributes=False):
        if info_attributes:
            attributes = Path(self.git("rev-parse", "--git-path", "info/attributes", cwd=dependency))
            if not attributes.is_absolute():
                attributes = dependency / attributes
            attributes.parent.mkdir(parents=True, exist_ok=True)
        else:
            attributes = Path(self.temporary.name) / "local-attributes"
            self.git("config", "core.attributesFile", str(attributes), cwd=dependency)
        attributes.write_text("dependency.txt filter=mask\n")
        self.git("config", "filter.mask.clean", "printf 'broken\\n'", cwd=dependency)
        self.git("config", "filter.mask.required", "true", cwd=dependency)

    def test_local_attributes_clean_filter_cannot_hide_source_during_verification(self):
        _, dependency = self._prepare([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]])
        self._install_masking_clean_filter(dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        self.assertEqual(self.git("hash-object", "--path=dependency.txt", "dependency.txt", cwd=dependency),
                         self.git("rev-parse", "HEAD:dependency.txt", cwd=dependency))
        index = Path(self.git("rev-parse", "--git-path", "index", cwd=dependency))
        if not index.is_absolute():
            index = dependency / index
        original_index = index.read_bytes()
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_local_attributes_clean_filter_after_verification_blocks_submission(self):
        _, dependency = self._prepare()
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        self._install_masking_clean_filter(dependency)
        (dependency / "dependency.txt").write_text("fixed\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_info_attributes_clean_filter_cannot_hide_changes_between_source_snapshots(self):
        worktree, dependency = self._prepare()
        self._install_masking_clean_filter(dependency, info_attributes=True)
        source_tree(worktree)
        (dependency / "dependency.txt").write_text("fixed\n")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_line_ending_conversion_cannot_hide_submodule_content(self):
        worktree, dependency = self._prepare()
        self.git("config", "core.autocrlf", "true", cwd=dependency)
        (dependency / "dependency.txt").write_bytes(b"broken\r\n")
        self.assertEqual(self.git("hash-object", "--path=dependency.txt", "dependency.txt", cwd=dependency),
                         self.git("rev-parse", "HEAD:dependency.txt", cwd=dependency))
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual((dependency / "dependency.txt").read_bytes(), b"broken\r\n")

    def test_filemode_configuration_cannot_hide_submodule_mode_change(self):
        checks = [[sys.executable, "-c",
                   "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').stat().st_mode & 0o111"]]
        _, dependency = self._prepare(checks)
        target = dependency / "dependency.txt"
        self.git("config", "core.fileMode", "false", cwd=dependency)
        target.chmod(0o755)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertTrue(target.stat().st_mode & 0o111)

    def test_untracked_submodule_file_cannot_be_hidden_by_status_configuration(self):
        worktree, dependency = self._prepare()
        (dependency / "untracked.txt").write_text("not delivered\n")
        excludes = Path(self.temporary.name) / "global-excludes"
        excludes.write_text("*.txt\n")
        self.git("config", "submodule.vendor/dependency.ignore", "untracked", cwd=worktree)
        self.git("config", "core.excludesFile", str(excludes), cwd=dependency)
        self.git("config", "status.showUntrackedFiles", "no", cwd=dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertTrue((dependency / "untracked.txt").is_file())

    def _git_metadata_path(self, root, name):
        path = Path(self.git("rev-parse", "--git-path", name, cwd=root))
        return path if path.is_absolute() else root / path

    def test_info_exclude_cannot_hide_untracked_submodule_source_between_snapshots(self):
        worktree, dependency = self._prepare()
        source_tree(worktree)
        excluded = dependency / "repair.txt"
        excluded.write_text("fixed\n")
        exclude = self._git_metadata_path(dependency, "info/exclude")
        exclude.write_text("repair.txt\n")
        self.assertEqual(self.git("ls-files", "--others", "--exclude-standard", cwd=dependency), "")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual(excluded.read_text(), "fixed\n")
        self.assertEqual(exclude.read_text(), "repair.txt\n")

    def test_info_exclude_cannot_hide_untracked_submodule_source_from_verification(self):
        _, dependency = self._prepare([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/repair.txt').read_text() == 'fixed\\n'"]])
        (dependency / "repair.txt").write_text("fixed\n")
        self._git_metadata_path(dependency, "info/exclude").write_text("repair.txt\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((dependency / "repair.txt").read_text(), "fixed\n")

    def test_info_exclude_cannot_hide_untracked_nested_submodule_source(self):
        self._add_nested_submodule()
        worktree, dependency = self._prepare()
        nested = dependency / "nested/inner"
        (nested / "repair.txt").write_text("fixed\n")
        self._git_metadata_path(nested, "info/exclude").write_text("repair.txt\n")
        with self.assertRaisesRegex(RelayError, "vendor/dependency/nested/inner"):
            source_tree(worktree)
        self.assertEqual((nested / "repair.txt").read_text(), "fixed\n")

    def test_committed_gitignore_still_excludes_submodule_build_artifacts(self):
        (self.dependency_source / ".gitignore").write_text("build/\n")
        self.git("add", ".gitignore", cwd=self.dependency_source)
        self.git("commit", "-qm", "ignore build artifacts", cwd=self.dependency_source)
        worktree, dependency = self._prepare()
        before = source_tree(worktree)
        build = dependency / "build"
        build.mkdir()
        (build / "output.txt").write_text("generated\n")
        self.assertEqual(source_tree(worktree), before)
        self.assertEqual((build / "output.txt").read_text(), "generated\n")

    def _prepare_ignore_case_dependency(self, checks=None):
        (self.dependency_source / ".gitignore").write_text("build/\n")
        self.git("add", ".gitignore", cwd=self.dependency_source)
        self.git("commit", "-qm", "ignore build artifacts", cwd=self.dependency_source)
        worktree, dependency = self._prepare(checks)
        self.git("config", "core.ignoreCase", "true", cwd=dependency)
        return worktree, dependency

    def _add_case_variant_source(self, dependency):
        build = dependency / "BuIlD"
        build.mkdir()
        source = build / "undelivered.txt"
        source.write_text("fixed\n")
        self.assertEqual(self.git("ls-files", "--others", "--exclude-per-directory=.gitignore",
                                  cwd=dependency), "")
        return source

    def test_ignore_case_cannot_hide_untracked_submodule_source_from_verification(self):
        worktree, dependency = self._prepare_ignore_case_dependency([[sys.executable, "-c",
            "from pathlib import Path; assert Path('vendor/dependency/BuIlD/undelivered.txt').read_text() == 'fixed\\n'"]])
        source = self._add_case_variant_source(dependency)
        index = self._git_metadata_path(dependency, "index")
        original = index.read_bytes()
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(source.read_text(), "fixed\n")
        self.assertEqual(self.git("config", "core.ignoreCase", cwd=dependency), "true")
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)

    def test_ignore_case_after_verification_blocks_submission(self):
        worktree, dependency = self._prepare_ignore_case_dependency()
        (worktree / "value.txt").write_text("fixed\n")
        with self.state.lock():
            verified = verify(self.config, self.state, "fix-1")
        self.assertTrue(verified["verification"]["passed"])
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        indexes = [self._git_metadata_path(root, "index") for root in (worktree, dependency)]
        originals = [index.read_bytes() for index in indexes]
        source = self._add_case_variant_source(dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), head)
        self.assertEqual([index.read_bytes() for index in indexes], originals)
        self.assertEqual(source.read_text(), "fixed\n")
        self.assertEqual(self.git("config", "core.ignoreCase", cwd=dependency), "true")

    def test_isolated_submodule_index_uses_case_sensitive_ignore_rules(self):
        _, dependency = self._prepare_ignore_case_dependency()
        source = self._add_case_variant_source(dependency)
        index = self._git_metadata_path(dependency, "index")
        original = index.read_bytes()
        tree = _isolated_source_tree(dependency, inspect_submodules=False, neutralize_config=True)
        self.assertNotEqual(tree, self.git("rev-parse", "HEAD^{tree}", cwd=dependency))
        self.assertIn("BuIlD/undelivered.txt", self.git("ls-tree", "-r", "--name-only", tree, cwd=dependency))
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(source.read_text(), "fixed\n")
        self.assertEqual(self.git("config", "core.ignoreCase", cwd=dependency), "true")

    def test_source_snapshot_preserves_submodule_indexes_with_invalid_cache_trees(self):
        self._add_nested_submodule()
        worktree, dependency = self._prepare()
        indexes = []
        for checkout, filename in ((dependency, "dependency.txt"), (dependency / "nested/inner", "nested.txt")):
            object_id = self.git("rev-parse", f"HEAD:{filename}", cwd=checkout)
            self.git("update-index", "--cacheinfo", "100644", object_id, filename, cwd=checkout)
            index = self._git_metadata_path(checkout, "index")
            original = index.read_bytes()
            self.assertIn(b"\0-1 ", original)
            indexes.append((index, original))
        source_tree(worktree)
        for index, original in indexes:
            self.assertEqual(index.read_bytes(), original)

    def test_staged_submodule_changes_are_rejected_without_rewriting_the_index(self):
        worktree, dependency = self._prepare()
        (dependency / "staged.txt").write_text("undelivered\n")
        self.git("add", "staged.txt", cwd=dependency)
        (dependency / "staged.txt").unlink()
        index = self._git_metadata_path(dependency, "index")
        original = index.read_bytes()
        with self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            source_tree(worktree)
        self.assertEqual(index.read_bytes(), original)

    def test_uninitialized_submodule_with_files_blocks_verification(self):
        checks = [[sys.executable, "-c",
                   "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]]
        worktree, dependency = self._prepare(checks, initialize=False)
        source_tree(worktree)
        dependency.mkdir(parents=True, exist_ok=True)
        (dependency / "dependency.txt").write_text("fixed\n")
        with self.state.lock(), self.assertRaisesRegex(
                RelayError, "not initialized.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")

    def test_dirty_nested_submodule_is_detected_recursively(self):
        self._add_nested_submodule()
        worktree, dependency = self._prepare()
        nested = dependency / "nested/inner"
        (nested / "nested.txt").write_text("dirty nested source\n")
        self.git("config", "submodule.vendor/dependency.ignore", "all", cwd=worktree)
        self.git("config", "submodule.nested/inner.ignore", "all", cwd=dependency)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "vendor/dependency/nested/inner"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual((nested / "nested.txt").read_text(), "dirty nested source\n")

    def test_submodule_change_during_checks_invalidates_verification(self):
        checks = [[sys.executable, "-c",
                   "from pathlib import Path; Path('vendor/dependency/dependency.txt').write_text('during checks\\n')"]]
        _, dependency = self._prepare(checks)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            verify(self.config, self.state, "fix-1")
        self.assertEqual(self.state.get("fix-1")["status"], "failed")
        self.assertEqual((dependency / "dependency.txt").read_text(), "during checks\n")

    def _submodule_modify_and_restore_checks(self):
        return [[sys.executable, "-c",
                 "from pathlib import Path; dependency = Path('vendor/dependency/dependency.txt'); "
                 "dependency.write_text('fixed\\n'); assert dependency.read_text() == 'fixed\\n'"],
                [sys.executable, "-c",
                 "from pathlib import Path; Path('vendor/dependency/dependency.txt').write_text('broken\\n'); "
                 "marker = Path('.devrelay/runtime/restore-ran.txt'); marker.parent.mkdir(parents=True, exist_ok=True); "
                 "marker.write_text('ran\\n')"]]

    def test_dirty_submodule_stops_checks_before_later_command_can_restore_it(self):
        worktree, dependency = self._prepare(self._submodule_modify_and_restore_checks())
        (worktree / "value.txt").write_text("fixed\n")
        indexes = [self._git_metadata_path(root, "index") for root in (worktree, dependency)]
        originals = [index.read_bytes() for index in indexes]
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            verify(self.config, self.state, "fix-1")
        failed = self.state.get("fix-1")
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["verification"])
        receipts = list((self.state.directory / "runs/fix-1").glob("*-checks/verification.json"))
        self.assertEqual(len(receipts), 1)
        record = json.loads(receipts[0].read_text())
        self.assertFalse(record["passed"])
        self.assertEqual([check["id"] for check in record["checks"]], ["checks-0"])
        self.assertEqual(record["checks"][0]["returncode"], 0)
        self.assertFalse((worktree / ".devrelay/runtime/restore-ran.txt").exists())
        self.assertEqual((dependency / "dependency.txt").read_text(), "fixed\n")
        self.assertEqual([index.read_bytes() for index in indexes], originals)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "missing or stale"):
            submit(self.config, self.state, "fix-1", local=True)

    def test_post_merge_dirty_submodule_stops_checks_before_later_restore(self):
        worktree, _ = self._prepare(self._submodule_modify_and_restore_checks(), setup=[
            ["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"]])
        task = self.state.get("fix-1")
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        task.update(head_sha=head, verification={"source_tree": source_tree(worktree)})
        pull = {"state": "MERGED", "baseRefName": "main", "headRefOid": head,
                "headRefName": task["branch"], "isCrossRepository": False, "mergeCommit": {"oid": head}}
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree.*vendor/dependency"):
            post_merge(self.config, self.state, task, pull)
        failed = self.state.get("fix-1")
        self.assertEqual(failed["status"], "post_merge_failed")
        self.assertEqual(failed["merge_sha"], head)
        self.assertFalse(failed["post_merge"]["passed"])
        self.assertEqual([check["id"] for check in failed["post_merge"]["checks"]], ["checks-0"])
        self.assertEqual(failed["post_merge"]["checks"][0]["returncode"], 0)
        self.assertEqual(len(failed["post_merge"]["setup"]), 1)
        self.assertEqual(json.loads(Path(failed["post_merge_path"]).read_text()), failed["post_merge"])
        path = Path(failed["post_merge_worktree"])
        self.assertFalse((path / ".devrelay/runtime/restore-ran.txt").exists())
        self.assertEqual((path / "vendor/dependency/dependency.txt").read_text(), "fixed\n")
        self.assertEqual((worktree / "vendor/dependency/dependency.txt").read_text(), "broken\n")
        self.assertTrue(worktree.exists())

    def test_submodule_change_after_checks_invalidates_submission(self):
        _, dependency = self._prepare()
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        (dependency / "after-checks.txt").write_text("not verified\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertTrue((dependency / "after-checks.txt").is_file())

    def test_submodule_change_from_parent_commit_hook_blocks_submission(self):
        worktree, dependency = self._prepare()
        (worktree / "value.txt").write_text("fixed\n")
        with self.state.lock():
            verify(self.config, self.state, "fix-1")
        hooks = Path(self.temporary.name) / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook mutation\\n' > vendor/dependency/from-hook.txt\n")
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks), cwd=worktree)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            submit(self.config, self.state, "fix-1", local=True)
        self.assertEqual((dependency / "from-hook.txt").read_text(), "hook mutation\n")

    def _assert_committed_submodule_head_is_delivered(self, *, gitmodules_ignore, local_ignore):
        worktree, dependency = self._prepare(
            [[sys.executable, "-c",
              "from pathlib import Path; assert Path('vendor/dependency/dependency.txt').read_text() == 'fixed\\n'"]],
            gitmodules_ignore=gitmodules_ignore)
        (dependency / "dependency.txt").write_text("fixed\n")
        self.git("add", "dependency.txt", cwd=dependency)
        self.git("commit", "-qm", "fix dependency", cwd=dependency)
        submodule_head = self.git("rev-parse", "HEAD", cwd=dependency)
        self.git("push", "-q", "origin", "HEAD:main", cwd=dependency)
        if local_ignore:
            self.git("config", "submodule.vendor/dependency.ignore", "all", cwd=worktree)
        (worktree / ".gitignore").write_text("local.cache\n")
        (worktree / "local.cache").write_text("not source\n")
        with self.state.lock():
            verified = verify(self.config, self.state, "fix-1")
            committed = submit(self.config, self.state, "fix-1", local=True)
        self.assertTrue(verified["verification"]["passed"])
        self.assertEqual(committed["status"], "committed")
        self.assertIn(submodule_head, self.git("ls-tree", "HEAD", "vendor/dependency", cwd=worktree))
        self.assertEqual(self.git("ls-tree", "HEAD", "local.cache", cwd=worktree), "")
        self.assertEqual((worktree / "local.cache").read_text(), "not source\n")

    def test_committed_submodule_head_is_delivered_with_gitmodules_ignore(self):
        self._assert_committed_submodule_head_is_delivered(gitmodules_ignore=True, local_ignore=False)

    def test_committed_submodule_head_is_delivered_with_local_ignore(self):
        self._assert_committed_submodule_head_is_delivered(gitmodules_ignore=False, local_ignore=True)


class LockAndProcessTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure()

    def test_repository_lock_blocks_a_second_process(self):
        source = Path(__file__).resolve().parents[1] / "src"
        script = "from pathlib import Path; from devrelay.state import State; import sys; s=State(Path(sys.argv[1]));\nwith s.lock(): print('unexpected acquisition')"
        with self.state.lock():
            result = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                    env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another DevRelay worker", result.stderr)

    def test_failed_command_retains_nonzero_receipt(self):
        log = self.state.directory / "failure.log"
        with self.state.lock(), self.assertRaises(RelayError):
            execute(self.state, [sys.executable, "-c", "print('failure evidence'); raise SystemExit(7)"], self.root, log, 5)
        self.assertIn("failure evidence", log.read_text())
        self.assertEqual(json.loads(log.with_suffix(".log.receipt.json").read_text())["returncode"], 7)

    def test_timeout_stops_foreground_process(self):
        log = self.state.directory / "timeout.log"
        with self.state.lock(), self.assertRaisesRegex(RelayError, "timed out"):
            execute(self.state, [sys.executable, "-c", "import time; time.sleep(30)"], self.root, log, 0.1)
        receipt = json.loads(log.with_suffix(".log.receipt.json").read_text())
        self.assertTrue(receipt["timed_out"])
        self.assertIsNone(self.state.metadata("active_process"))

    def test_streaming_delivers_large_input_without_a_writer_thread(self):
        seen = []
        payload = "x" * 2_000_000
        script = "import sys; data = sys.stdin.buffer.read(); print(len(data), flush=True)"
        with self.state.lock():
            receipt = execute(self.state, [sys.executable, "-c", script], self.root,
                              self.state.directory / "large-input.log", 5, input=payload,
                              on_output_line=seen.append)
        self.assertEqual(seen, [str(len(payload))])
        self.assertEqual(receipt["returncode"], 0)
        self.assertEqual(Path(receipt["stderr_log"]).read_text(), "")

    def test_streaming_large_input_does_not_wait_on_descendant_held_stdin(self):
        group_file = Path(self.temporary.name) / "held-stdin-group"
        script = """import os, sys, time
path = sys.argv[1]
with open(path, 'w') as stream:
    stream.write(str(os.getpgrp()))
    stream.flush()
if os.fork() == 0:
    time.sleep(30)
    os._exit(0)
os._exit(0)
"""
        started = time.monotonic()
        group = None
        try:
            with self.state.lock(), self.assertRaisesRegex(RelayError, "live descendants"):
                execute(self.state, [sys.executable, "-c", script, str(group_file)], self.root,
                        self.state.directory / "held-stdin.log", 1, input="x" * 2_000_000,
                        on_output_line=lambda line: None)
            self.assertLess(time.monotonic() - started, 3)
            group = int(group_file.read_text())
            self.assertEqual(self.state.metadata("active_process")["group"], group)
        finally:
            if group is None and group_file.exists():
                group = int(group_file.read_text())
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.killpg(group, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f"process group {group} survived test cleanup")

    def test_surviving_process_group_blocks_crash_recovery(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            self.state.set_metadata("active_process", {"group": process.pid})
            with self.assertRaisesRegex(RelayError, "still runs"):
                with self.state.lock():
                    self.fail("must not acquire while child survives")
        finally:
            process.terminate()
            process.wait(timeout=5)
        with self.state.lock():
            self.assertIsNone(self.state.metadata("active_process"))

    def test_execute_after_termination_failure_preserves_live_group_marker(self):
        self._assert_termination_failure_preserves_live_group_marker()

    def test_streaming_termination_failure_preserves_live_group_marker(self):
        self._assert_termination_failure_preserves_live_group_marker(on_output_line=lambda line: None)

    def _assert_termination_failure_preserves_live_group_marker(self, on_output_line=None):
        log = self.state.directory / "termination-failure.log"
        retry_log = self.state.directory / "blocked-retry.log"
        retry_output = Path(self.temporary.name) / "retry-started"
        process = None

        def fail_termination(command):
            nonlocal process
            process = command
            raise RelayError("Cannot stop process group; recovery will remain blocked")

        try:
            # Stay inside the same lock, as runner's automatic retry does.
            with self.state.lock():
                with (patch("devrelay.process.terminate", side_effect=fail_termination),
                      self.assertRaisesRegex(RelayError, "Cannot stop process group")):
                    execute(self.state, [sys.executable, "-c", "import time; time.sleep(30)"],
                            self.root, log, 0.1, on_output_line=on_output_line)
                active = self.state.metadata("active_process")
                self.assertEqual(active["group"], process.pid)
                with self.assertRaisesRegex(RelayError, "Previous command still runs"):
                    execute(self.state,
                            [sys.executable, "-c",
                             "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(retry_output)],
                            self.root, retry_log, 5)
                self.assertEqual(self.state.metadata("active_process"), active)
                self.assertFalse(retry_log.exists())
                self.assertFalse(retry_output.exists())
                process.kill()
                process.wait(timeout=5)
                # A retry becomes safe once the original group is confirmed gone.
                execute(self.state, [sys.executable, "-c", "print('recovered')"],
                        self.root, retry_log, 5)
                self.assertIsNone(self.state.metadata("active_process"))
        finally:
            if process is not None:
                process.kill()
                process.wait(timeout=5)


    def test_timeout_kills_descendant_that_ignores_sigterm(self):
        self._assert_timeout_kills_descendant_that_ignores_sigterm()

    def test_streaming_timeout_kills_descendant_that_ignores_sigterm(self):
        seen = []
        self._assert_timeout_kills_descendant_that_ignores_sigterm(on_output_line=seen.append)
        self.assertEqual(seen, ["started"])

    def _assert_timeout_kills_descendant_that_ignores_sigterm(self, on_output_line=None):
        log = self.state.directory / "stubborn-descendant.log"
        marker = Path(self.temporary.name) / "stubborn-descendant"
        child = ("import signal,time; "
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 "print('ready', flush=True); time.sleep(30)")
        parent = ("import os,subprocess,sys,time; "
                  "child=subprocess.Popen([sys.executable, '-c', sys.argv[2]], "
                  "stdout=subprocess.PIPE, text=True); "
                  "assert child.stdout.readline() == 'ready\\n'; "
                  "marker=open(sys.argv[1], 'w'); "
                  "marker.write(f'{os.getpgrp()} {child.pid}'); marker.close(); "
                  "print('started', flush=True); "
                  "time.sleep(30)")
        group = None
        try:
            with (patch("devrelay.process._TERMINATE_GRACE_SECONDS", 1),
                  patch("devrelay.process._KILL_GRACE_SECONDS", 2)):
                with self.state.lock(), self.assertRaisesRegex(RelayError, "timed out"):
                    execute(self.state, [sys.executable, "-c", parent, str(marker), child],
                            self.root, log, 1, on_output_line=on_output_line)
            group, child_pid = (int(value) for value in marker.read_text().split())
            self.assertNotEqual(group, child_pid)
            self.assertFalse(group_alive(group))
            receipt = json.loads(log.with_suffix(".log.receipt.json").read_text())
            self.assertTrue(receipt["timed_out"])
            self.assertEqual(receipt["returncode"], -signal.SIGTERM)
            self.assertIsNone(self.state.metadata("active_process"))
            with self.state.lock():
                self.assertIsNone(self.state.metadata("active_process"))
        finally:
            if group is None and marker.exists():
                try:
                    group = int(marker.read_text().split()[0])
                except (OSError, ValueError, IndexError):
                    pass
            if group is None:
                active = self.state.metadata("active_process")
                if active:
                    group = active["group"]
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 5
                try:
                    while group_alive(group) and time.monotonic() < deadline:
                        time.sleep(0.05)
                except (OSError, RelayError):
                    # SIGKILL above is the cleanup guarantee; an environment
                    # that forbids ps cannot confirm it here.
                    pass


    def test_hung_process_group_probe_is_bounded_and_keeps_recovery_marker(self):
        fake_bin = Path(self.temporary.name) / "fake-bin"
        fake_bin.mkdir()
        fake_ps = fake_bin / "ps"
        fake_ps.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        fake_ps.chmod(0o755)
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   start_new_session=True)
        self.state.set_metadata("active_process", {"group": process.pid})
        try:
            with (patch.dict(os.environ, {"PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}),
                  patch("devrelay.process._TERMINATE_GRACE_SECONDS", 0.2),
                  patch("devrelay.process._KILL_GRACE_SECONDS", 0.2),
                  patch("devrelay.state._GROUP_PROBE_TIMEOUT_SECONDS", 0.2),
                  patch("devrelay.process.os.killpg") as kill_group):
                started = time.monotonic()
                with self.assertRaisesRegex(RelayError, "inspection timed out"):
                    terminate(process)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(kill_group.call_args_list,
                                 [call(process.pid, signal.SIGTERM), call(process.pid, signal.SIGKILL)])
                recovery_started = time.monotonic()
                with self.assertRaisesRegex(RelayError, "inspection timed out"):
                    with self.state.lock():
                        self.fail("an uninspectable group must keep recovery blocked")
                self.assertLess(time.monotonic() - recovery_started, 1)
                self.assertEqual(self.state.metadata("active_process")["group"], process.pid)
                active = self.state.metadata("active_process")
                retry_log = self.state.directory / "uninspectable-retry.log"
                retry_output = Path(self.temporary.name) / "uninspectable-retry-started"
                retry_started = time.monotonic()
                with self.assertRaisesRegex(RelayError, "inspection timed out"):
                    execute(self.state,
                            [sys.executable, "-c",
                             "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(retry_output)],
                            self.root, retry_log, 5)
                self.assertLess(time.monotonic() - retry_started, 1)
                self.assertEqual(self.state.metadata("active_process"), active)
                self.assertFalse(retry_log.exists())
                self.assertFalse(retry_output.exists())
        finally:
            process.kill()
            process.wait(timeout=5)



if __name__ == "__main__":
    unittest.main()
