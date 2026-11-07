"""Automatic project policy with real isolated checkouts and mocked remote control."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from devrelay.cli import main
from devrelay.config import initialize, load
from devrelay.errors import RelayError
from devrelay.providers import add_local, authorized, queue_report, snapshot
from devrelay.runner import complete_local, run_task, start, submit
from devrelay.triage import current, triage
from devrelay.worker import selection
from devrelay.workflow import guidance, work

from helpers import RepositoryTest
import test_discovery
from test_discovery import FINDING
from test_worker import WorkerFixture
from test_worker_merge import AutoMergeWorkerFixture


def enable(test, *, budget=20, scope="Fix confirmed bugs without changing intended behavior."):
    path = test.root / ".devrelay/config.toml"
    path.write_text(path.read_text() + '\n[queue]\nmode = "autonomous"\nauto_kinds = ["bug"]\n'
                    + f"scope = {json.dumps(scope)}\nmax_dispatches = {budget}\n")
    test.config = load(test.root)


class LocalAutonomyTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure(executable=self.fake_codex())
        enable(self)
        task = add_local(self.config, self.state, "bug", "Fix value", "Expected fixed, observed broken.", None)
        task["labels"] = []
        self.state.save(task)

    def report(self, task, **changes):
        return {"context_sha256": task["triage"]["context"]["context_sha256"], "decision": "ready", "kind": "bug",
                "summary": "Confirmed input validation bug", "evidence": "Read value.txt on the base: broken",
                "acceptance": "value.txt reads fixed", "questions": [], "duplicate_of": "", **changes}

    def test_external_agent_uses_same_gate_without_a_model(self):
        with self.assertRaisesRegex(RelayError, "triage"):
            start(self.config, self.state, "bug")
        with patch("devrelay.codex.triage", side_effect=AssertionError("External tool must not invoke Codex")):
            prepared = triage(self.config, self.state, "bug", prepare_only=True)
            self.assertNotIn("worktree", prepared)
            context = prepared["triage"]["context"]
            self.assertEqual((Path(context["worktree"]) / "value.txt").read_text(), "broken\n")
            self.assertEqual(self.git("branch", "--show-current", cwd=context["worktree"]), "")
            ready = triage(self.config, self.state, "bug", report=self.report(prepared))
        self.assertTrue(current(self.config, ready))
        self.assertEqual(authorized(self.config, self.state, "bug")["labels"], [])
        task = start(self.config, self.state, "bug")
        self.assertEqual(task["triage"], ready["triage"])

    def test_managed_work_investigates_then_implements_in_a_distinct_session(self):
        trace = Path(self.temporary.name) / "trace"
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(trace)}):
            task = work(self.config, self.state, "bug")
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["cwd"], calls[1]["cwd"])
        self.assertNotIn("resume", calls[1]["argv"])
        self.assertIn("Investigated scope", calls[1]["prompt"])
        self.assertTrue(task["verification"]["scope_sha256"])
        self.assertEqual(task["status"], "committed")

    def test_issue_policy_and_report_edits_invalidate_authorization_or_acceptance(self):
        task = work(self.config, self.state, "bug")
        task["body"] += "\nNew expected behavior"
        self.state.save(task)
        with self.assertRaisesRegex(RelayError, "triage.*stale"):
            authorized(self.config, self.state, "bug")
        task["body"] = "Expected fixed, observed broken.\n\nDepends on: none\n"
        self.state.save(task)
        # Restore the exact live requirements, then changing the accepted diagnosis expires verification.
        task["body"] = task["verification"]["task_body"]
        task["triage"]["result"]["acceptance"] = "A different observable result"
        self.state.save(task)
        with self.assertRaisesRegex(RelayError, "stale"):
            submit(self.config, self.state, "bug", local=True)
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("Fix confirmed bugs without changing intended behavior.", "Only fix parser bugs."))
        self.config = load(self.root)
        with self.assertRaisesRegex(RelayError, "triage.*stale"):
            authorized(self.config, self.state, "bug")

    def test_external_reports_reject_stale_context_and_missing_evidence(self):
        prepared = triage(self.config, self.state, "bug", prepare_only=True)
        for changes in ({"context_sha256": "other"}, {"evidence": ""}, {"questions": ["Unresolved"]},
                        {"decision": "needs_input", "questions": []}):
            with self.subTest(changes=changes), self.assertRaises(RelayError):
                triage(self.config, self.state, "bug", report=self.report(prepared, **changes))
        task = self.state.get("bug")
        task["body"] += " changed while investigating"
        self.state.save(task)
        with self.assertRaisesRegex(RelayError, "stale"):
            triage(self.config, self.state, "bug", report=self.report(prepared))

    def test_disallowed_kind_goes_to_human_even_if_agent_says_ready(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRIAGE_KIND": "feature"}):
            task = work(self.config, self.state, "bug")
        self.assertNotIn("worktree", task)
        self.assertEqual(task["triage"]["result"]["decision"], "needs_input")
        self.assertEqual(guidance(self.config, task)["actor"], "human")
        self.assertTrue(guidance(self.config, task)["questions"])
        with self.assertRaises(RelayError):
            start(self.config, self.state, "bug")

    def test_plan_blockers_and_manual_approval_obey_common_rules(self):
        task = self.state.get("bug")
        task["body"] += "\nTask kind: plan"
        task["labels"] = ["auto:approved"]
        self.state.save(task)
        with self.assertRaisesRegex(RelayError, "plan"):
            start(self.config, self.state, "bug")
        task["body"] = "A bounded fix"
        task["labels"] = ["blocked"]
        self.state.save(task)
        with self.assertRaisesRegex(RelayError, "blocked"):
            triage(self.config, self.state, "bug")
        task["labels"] = ["auto:approved"]
        self.state.save(task)
        with patch("devrelay.codex.triage", side_effect=AssertionError("Explicit approval does not need triage")):
            self.assertEqual(work(self.config, self.state, "bug")["status"], "committed")

    def test_triage_session_cannot_be_reused_by_implementation(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRIAGE_SESSION": "exact-test-session"}):
            triage(self.config, self.state, "bug")
        with self.assertRaisesRegex(RelayError, "triage investigation"):
            work(self.config, self.state, "bug")

    def test_base_change_rejects_old_investigation_and_retry_preserves_evidence(self):
        prepared = triage(self.config, self.state, "bug", prepare_only=True)
        (self.root / "new.txt").write_text("new base")
        self.git("add", "new.txt")
        self.git("commit", "-qm", "advance base")
        with self.assertRaisesRegex(RelayError, "base changed"):
            triage(self.config, self.state, "bug", report=self.report(prepared))
        fresh = triage(self.config, self.state, "bug", retry=True)
        self.assertEqual(len(fresh["triage_history"]), 1)
        self.assertTrue(Path(prepared["triage"]["context"]["worktree"]).is_dir())
        self.assertNotEqual(fresh["triage"]["context"]["context_sha256"], prepared["triage"]["context"]["context_sha256"])

    def test_cli_prepare_and_external_result(self):
        def invoke(*args):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--project", str(self.root), *args])
            return code, json.loads(out.getvalue() or err.getvalue())
        code, task = invoke("triage", "bug", "--prepare")
        self.assertEqual(code, 0, task)
        file = Path(self.temporary.name) / "report.json"
        file.write_text(json.dumps(self.report(task)))
        code, task = invoke("triage", "bug", "--file", str(file))
        self.assertEqual(code, 0, task)
        code, result = invoke("next")
        self.assertEqual(result["selected"]["id"], "bug")

    def test_skip_is_cached_without_closing_or_starting_the_task(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRIAGE": "skip"}):
            skipped = triage(self.config, self.state, "bug")
        with patch("devrelay.codex.triage", side_effect=AssertionError("Unchanged negative result must be cached")):
            repeated = triage(self.config, self.state, "bug")
        self.assertEqual(skipped["triage"], repeated["triage"])
        self.assertTrue(repeated["open"])
        report = queue_report(self.config, snapshot(self.config, self.state))
        self.assertFalse(report["triage_candidates"])
        self.assertIsNone(report["selected"])

    def test_run_alias_also_uses_project_triage_without_submission(self):
        result = run_task(self.config, self.state)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["triage"]["result"]["decision"], "ready")

    def test_external_report_does_not_write_through_a_symlink(self):
        prepared = triage(self.config, self.state, "bug", prepare_only=True)
        outside = Path(self.temporary.name) / "untouched"
        outside.write_text("preserve")
        (Path(prepared["triage"]["context"]["run"]) / "triage.json").symlink_to(outside)
        with self.assertRaisesRegex(RelayError, "owned directory"):
            triage(self.config, self.state, "bug", report=self.report(prepared))
        self.assertEqual(outside.read_text(), "preserve")

    def test_autonomous_configuration_rejects_invalid_policy_and_budget(self):
        path = self.root / ".devrelay/config.toml"
        original = path.read_text()
        for old, new in [('mode = "autonomous"', 'mode = "all"'), ('auto_kinds = ["bug"]', 'auto_kinds = []'),
                         ('auto_kinds = ["bug"]', 'auto_kinds = ["plan"]'), ('auto_kinds = ["bug"]', 'auto_kinds = ["bug", "bug"]'),
                         ('max_dispatches = 20', 'max_dispatches = 0'), ('max_dispatches = 20', 'max_dispatches = true')]:
            path.write_text(original.replace(old, new))
            with self.subTest(new=new), self.assertRaises(RelayError):
                load(self.root)
        path.write_text(original)

    def test_local_completion_also_binds_the_investigated_acceptance(self):
        task = work(self.config, self.state, "bug")
        self.git("merge", "--ff-only", task["head_sha"])
        task["triage"]["result"]["acceptance"] = "A newly requested behavior"
        self.state.save(task)
        with self.assertRaisesRegex(RelayError, "Latest verified content"):
            complete_local(self.config, self.state, "bug")


class AutonomousWorkerTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        for issue in self.issues:
            issue["labels"] = [label for label in issue["labels"] if label["name"] != "auto:approved"]
        enable(self)

    def test_unapproved_issues_are_investigated_and_delivered(self):
        result = self.watching(max_tasks=2)
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(self.create_count, 2)
        self.assertEqual(self.state.metadata("watch_budget")["used"], 4)
        self.assertEqual([event["id"] for event in self.events if event["event"] == "task_triaged"], ["13", "12"])
        self.assertFalse(any(label["name"] == "auto:approved" for issue in self.issues for label in issue["labels"]))

    def test_needs_input_and_skipped_issue_do_not_repeat_or_starve_next_task(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRIAGE_BY_ID": '{"13": "needs_input"}'}):
            result = self.watching(max_tasks=1)
        self.assertEqual(result["delivered"], 1)
        task = self.state.get("13")
        self.assertNotIn("worktree", task)
        self.assertEqual(task["triage"]["result"]["decision"], "needs_input")
        self.assertEqual(selection(self.config, self.state)["status"], "idle")
        # Answer by editing the Issue body; the next poll re-investigates it.
        self.issues[1]["body"] += "\nDecision: expected value is fixed."
        self.assertEqual(selection(self.config, self.state)["action"], "triage")
        self.watching(max_tasks=1)
        self.assertEqual(self.state.get("13")["status"], "pr_open")
        self.assertEqual(len(self.state.get("13")["triage_history"]), 1)

    def test_task_handoff_skips_then_body_answer_resumes_original_session(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "blocked"}):
            # Both tasks reach implementation and park; interrupt only the idle poll.
            with patch("devrelay.worker.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                self.watching()
        original = self.state.get("13")
        self.assertEqual(original["handoff"]["kind"], "input")
        self.assertEqual(self.state.get("12")["handoff"]["kind"], "input")
        self.issues[1]["body"] += "\nDecision: dataset is now available; implement fixed."
        self.watching(max_tasks=1)
        finished = self.state.get("13")
        self.assertEqual(finished["session_id"], original["session_id"])
        self.assertEqual(finished["status"], "pr_open")
        self.assertEqual(self.state.get("12")["status"], "blocked")

    def test_changed_requirements_needing_a_decision_park_existing_work(self):
        triage(self.config, self.state, "13")
        start(self.config, self.state, "13")
        self.issues[1]["body"] += "\nUnresolved architecture choice"
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRIAGE_BY_ID": '{"13": "needs_input"}'}):
            self.watching(max_tasks=1)
        paused = self.state.get("13")
        self.assertEqual(paused["handoff"]["kind"], "input")
        self.assertEqual(paused["triage"]["result"]["decision"], "needs_input")
        self.assertEqual(self.state.get("12")["status"], "pr_open")
        self.assertEqual(selection(self.config, self.state)["status"], "idle")

    def test_budget_counts_triage_failures_and_survives_restart(self):
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_dispatches = 20", "max_dispatches = 2"))
        self.config = load(self.root)
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRIAGE": "needs_input"}):
            result = self.watching()
        self.assertEqual(result["reason"], "dispatch_budget")
        self.assertEqual(result["dispatches"], 2)
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(self.watching()["reason"], "dispatch_budget")
        self.issues[1]["body"] += "\nResolved decision"
        result = self.watching(reset_budget=True, max_tasks=1)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(len(self.state.metadata("watch_budget_history")), 1)

    def test_model_or_setup_failure_stops_repository_and_does_not_refill_budget(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRIAGE": "failure"}), self.assertRaises(RelayError):
            self.watching()
        self.assertEqual(self.state.get("13")["triage"]["phase"], "failed")
        self.assertIsNone(self.state.get("12"))
        self.assertEqual(self.watching()["status"], "needs_attention")
        self.assertEqual(self.state.metadata("watch_budget")["used"], 1)

    def test_exhausted_implementation_budget_does_not_starve_other_issues(self):
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_attempts = 2", "max_attempts = 1"))
        self.config = load(self.root)
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "retry"}):
            with patch("devrelay.worker.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                self.watching()
        for identifier in ("12", "13"):
            task = self.state.get(identifier)
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["handoff"]["kind"], "budget")
        self.assertEqual(self.create_count, 0)
        self.assertEqual(self.state.metadata("watch_budget")["used"], 4)

    def test_pending_merge_or_repository_fault_still_blocks_other_tasks(self):
        task = {"id": "99", "status": "merge_blocked", "handoff": {"kind": "input"}, "merge_request": {"state": "uncertain"}}
        self.state.save(task)
        self.assertEqual(selection(self.config, self.state)["status"], "needs_attention")
        self.assertIsNone(self.state.get("13"))
        task.pop("merge_request")
        task["merge_gates"] = {"blockers": ["Failed or unknown CI check"]}
        self.state.save(task)
        self.assertEqual(selection(self.config, self.state)["status"], "needs_attention")


class AutonomousInitTests(RepositoryTest):
    def test_opt_in_initialization_enables_discovery_but_preserves_delivery_policy(self):
        initialize(self.root, "local", "main", mode="autonomous")
        config = load(self.root)
        self.assertEqual(config.data["queue"]["mode"], "autonomous")
        self.assertTrue(config.data["discovery"]["enabled"])
        self.assertFalse(config.data["delivery"]["auto_merge"])
        original = (self.root / ".devrelay/config.toml").read_text()
        with self.assertRaises(RelayError):
            initialize(self.root, "local", "main", mode="manual")
        self.assertEqual((self.root / ".devrelay/config.toml").read_text(), original)


class AutonomousDiscoveryMergeTests(AutoMergeWorkerFixture):
    def setUp(self):
        super().setUp()
        self.issues = [self.issue]
        self.issue["labels"] = []
        self.issue_creates = []
        self.create_failure = self.after_create_failure = None
        patcher = patch("devrelay.providers.gh", side_effect=self.create_issue)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("devrelay.discovery.validate_remote")
        patcher.start()
        self.addCleanup(patcher.stop)
        enable(self)
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text() + "\n[discovery]\nenabled = true\n")
        self.config = load(self.root)

    create_issue = test_discovery.GitHubDiscoveryTests.create_issue

    def test_review_discovery_investigates_and_merges_without_manual_approval(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_REVIEW_DISCOVERIES": json.dumps([FINDING])}):
            result = self.watching(max_tasks=2)
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(self.merge_count, 2)
        self.assertEqual(len(self.issue_creates), 1)
        self.assertEqual(self.issues[1]["labels"], [])
        for identifier in ("12", "21"):
            task = self.state.get(identifier)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["triage"]["result"]["decision"], "ready")
            self.assertEqual(task["review"]["verdict"], "approve")
            self.assertTrue(task["post_merge"]["passed"])
        self.assertEqual(self.state.get("21")["triage"]["context"]["base_sha"], self.state.get("12")["merge_sha"])
        self.assertEqual(self.state.get("12")["discoveries"][0]["observations"][0]["origin"], "review")
