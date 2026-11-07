"""Real task loops with mocked GitHub writes; discoveries never grant execution."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from devrelay.cli import main
from devrelay.config import load
from devrelay.discovery import discover
from devrelay.errors import RelayError
from devrelay.providers import add_local, approve_local, queue_report, snapshot
from devrelay.runner import run_task, start
from devrelay.triage import triage
from devrelay.worker import selection
from devrelay.workflow import work

from helpers import RepositoryTest
from test_worker import WorkerFixture


FINDING = {"title": "Reject an empty export filename", "description": "The independent export path accepts an empty filename.",
           "evidence": "On the unchanged base, export('') raises FileNotFoundError instead of reporting invalid input.",
           "acceptance": "Empty filenames return a validation error; valid exports remain unchanged."}


def configure_discovery(test, limit=3):
    path = test.root / ".devrelay/config.toml"
    with path.open("a") as stream:
        stream.write(f"\n[discovery]\nmax_per_task = {limit}\n")
    test.config = load(test.root)


class LocalDiscoveryTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure(executable=self.fake_codex())
        add_local(self.config, self.state, "source", "Fix value", "Fix the bounded behavior", None)

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--project", str(self.root), *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_external_agent_can_opt_out_and_require_explicit_authorization(self):
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text() + "\n[discovery]\nauto_triage = false\n")
        self.config = load(self.root)
        start(self.config, self.state, "source")
        path = Path(self.temporary.name) / "findings.json"
        path.write_text(json.dumps([FINDING]))
        code, source = self.invoke("discover", "source", "--file", str(path))
        self.assertEqual(code, 0, source)
        entry = source["discoveries"][0]
        child = self.state.get(entry["issue_id"])
        self.assertEqual(child["labels"], [])
        self.assertIn(FINDING["evidence"], child["body"])
        self.assertIn("Observed source tree:", child["body"])
        self.assertNotIn("Depends on: #source", child["body"])
        self.assertIsNone(queue_report(self.config, snapshot(self.config, self.state))["selected"])
        code, report = self.invoke("status")
        self.assertEqual(next(t for t in report["tasks"] if t["id"] == child["id"])["next_action"]["kind"], "authorize")
        code, approved = self.invoke("task", "approve", child["id"])
        self.assertEqual(code, 0, approved)
        self.assertEqual(queue_report(self.config, snapshot(self.config, self.state))["selected"]["id"], child["id"])
        self.invoke("discover", "source", "--file", str(path))
        self.assertEqual(len(self.state.tasks()), 2)
        self.assertEqual(len(self.state.get("source")["discoveries"][0]["observations"]), 1)

    def test_default_external_discovery_uses_independent_triage_and_shared_gates(self):
        start(self.config, self.state, "source")
        source = discover(self.config, self.state, "source", [FINDING])
        identifier = source["discoveries"][0]["issue_id"]
        report = queue_report(self.config, snapshot(self.config, self.state))
        self.assertEqual([t["id"] for t in report["triage_candidates"]], [identifier])
        self.assertIsNone(report["selected"])
        code, status = self.invoke("status")
        self.assertEqual(next(t for t in status["tasks"] if t["id"] == identifier)["next_action"]["kind"], "triage")
        with self.assertRaisesRegex(RelayError, "triage"):
            start(self.config, self.state, identifier)
        # The same external tool protocol works without launching a managed model.
        with patch("devrelay.codex.triage", side_effect=AssertionError("Unexpected managed triage")):
            prepared = triage(self.config, self.state, identifier, prepare_only=True)
            triage(self.config, self.state, identifier, report={
                "context_sha256": prepared["triage"]["context"]["context_sha256"],
                "decision": "ready", "kind": "bug", "summary": "Confirmed bug",
                "evidence": "Read value.txt: broken", "acceptance": "value.txt reads fixed",
                "questions": [], "duplicate_of": ""})
        task = work(self.config, self.state, identifier)
        self.assertEqual(task["status"], "committed")
        self.assertEqual(task["labels"], [])
        self.assertTrue(task["verification"]["scope_sha256"])

    def test_default_managed_discovery_keeps_failed_acceptance_on_original_task(self):
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_attempts = 2", "max_attempts = 1"))
        self.config = load(self.root)
        with patch.dict(os.environ, {"DEVRELAY_TEST_DISCOVERIES": json.dumps([FINDING]),
                                     "DEVRELAY_TEST_MODE": "retry"}):
            with self.assertRaisesRegex(RelayError, "checks-0"):
                run_task(self.config, self.state, "source", submit_after=False)
        source = self.state.get("source")
        self.assertEqual(source["status"], "failed")
        self.assertEqual(source["failure"]["kind"], "checks")
        self.assertEqual(len(source["discoveries"]), 1)
        self.assertFalse(source.get("pr_url"))
        self.assertEqual(self.state.get(source["discoveries"][0]["issue_id"])["labels"], [])

    def test_disabled_managed_discovery_does_not_publish(self):
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text() + "\n[discovery]\nenabled = false\n")
        self.config = load(self.root)
        with patch.dict(os.environ, {"DEVRELAY_TEST_DISCOVERIES": json.dumps([FINDING])}):
            task = run_task(self.config, self.state, "source")
        self.assertEqual(task["status"], "verified")
        self.assertNotIn("discoveries", task)
        self.assertEqual(len(self.state.tasks()), 1)

    def test_invalid_or_excessive_reports_do_not_create_tasks(self):
        configure_discovery(self, limit=1)
        start(self.config, self.state, "source")
        for reports in ([{**FINDING, "evidence": ""}], [FINDING, {**FINDING, "title": "Another bug"}],
                        [{**FINDING, "description": "Task kind: plan"}], {"discoveries": [FINDING]}):
            with self.subTest(reports=reports), self.assertRaises(RelayError):
                discover(self.config, self.state, "source", reports)
            self.assertEqual(len(self.state.tasks()), 1)

    def test_local_approval_rejects_tracking_parent(self):
        self.state.save({"id": "plan", "title": "Plan", "body": "Task kind: plan", "labels": [], "status": "queued"})
        with self.assertRaisesRegex(RelayError, "Tracking plan"):
            approve_local(self.config, self.state, "plan")

    def test_over_limit_reports_survive_retry_with_original_observation(self):
        configure_discovery(self, limit=1)
        source = start(self.config, self.state, "source")
        with self.assertRaisesRegex(RelayError, "limit"):
            discover(self.config, self.state, "source", [FINDING, {**FINDING, "title": "Another independent bug"}])
        original = self.state.get("source")["discovery_pending"][0]["observation"]
        with self.assertRaisesRegex(RelayError, "limit"):
            discover(self.config, self.state, "source")
        (Path(source["worktree"]) / "value.txt").write_text("edited after the observation\n")
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_per_task = 1", "max_per_task = 2"))
        self.config = load(self.root)
        recovered = discover(self.config, self.state, "source")
        self.assertEqual(len(recovered["discoveries"]), 2)
        self.assertEqual(recovered["discovery_pending"], [])
        self.assertNotIn("discovery_error", recovered)
        self.assertEqual(recovered["discoveries"][0]["observations"][0]["source_tree"], original["source_tree"])

    def test_discovery_config_is_bounded_and_does_not_expire_acceptance(self):
        original = self.config
        path = self.root / ".devrelay/config.toml"
        source = path.read_text()
        for value in ('enabled = "yes"', 'max_per_task = 0', 'max_per_task = 21', 'max_per_task = true',
                      'auto_approve = true', 'auto_triage = "yes"', 'auto_review = 1'):
            path.write_text(source + "\n[discovery]\n" + value + "\n")
            with self.subTest(value=value), self.assertRaises(RelayError):
                load(self.root)
        path.write_text(source + "\n[discovery]\nenabled = false\n")
        self.config = load(self.root)
        for stage in ("setup", "checks", "review"):
            self.assertEqual(original.stage_fingerprint(stage), self.config.stage_fingerprint(stage))


class GitHubDiscoveryTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.issues = [self.issue]
        self.issue_creates = []
        self.create_failure = None
        self.after_create_failure = None
        patcher = patch("devrelay.discovery.validate_remote")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("devrelay.providers.gh", side_effect=self.create_issue)
        patcher.start()
        self.addCleanup(patcher.stop)
        configure_discovery(self)

    def create_issue(self, config, *argv, input=None):
        self.assertEqual(argv[:2], ("issue", "create"))
        self.assertEqual(argv[-2:], ("--body-file", "-"))
        self.assertNotIn("--label", argv)
        self.issue_creates.append({"argv": argv, "body": input})
        if self.create_failure:
            raise self.create_failure
        number = 20 + len(self.issue_creates)
        issue = {"number": number, "title": argv[argv.index("--title") + 1], "body": input,
                 "state": "OPEN", "labels": [], "createdAt": "2026-11-05T00:00:00Z",
                 "url": f"https://github.com/example/project/issues/{number}"}
        self.issues.append(issue)
        if self.after_create_failure:
            raise self.after_create_failure
        return issue["url"]

    def pull_snapshot(self, config, task):
        return {"state": "OPEN", "headRefName": task["branch"], "headRefOid": task["head_sha"],
                "baseRefName": "main", "baseRefOid": task["verification"]["base_sha"], "isCrossRepository": False}

    def test_default_watch_triages_and_reviews_discovery_without_affecting_human_issues(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_DISCOVERIES": json.dumps([FINDING])}):
            self.watching(max_tasks=1)
        source = self.state.get("12")
        self.assertEqual(source["status"], "pr_open")
        self.assertEqual(source["discoveries"][0]["issue_id"], "21")
        self.assertEqual(len(self.issue_creates), 1)
        self.assertEqual(next(event for event in self.events if event["event"] == "task_finished")["discoveries"][0]["issue_id"], "21")
        self.issues.append({**self.issue, "number": 22, "labels": [], "title": "Human unapproved bug"})
        self.assertEqual(selection(self.config, self.state)["action"], "triage")
        with patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            self.watching(max_tasks=1)
        child = self.state.get("21")
        self.assertEqual(child["status"], "reviewed")
        self.assertEqual(child["triage"]["result"]["decision"], "ready")
        self.assertEqual(child["labels"], [])
        self.assertNotIn("merge_request", child)
        self.assertIsNone(self.state.get("22"))
        self.assertEqual(selection(self.config, self.state)["status"], "idle")
        self.assertEqual(self.state.metadata("watch_budget")["used"], 2)
        self.assertEqual(self.create_count, 2)

    def test_duplicate_human_issue_and_copied_marker_do_not_gain_automatic_policy(self):
        start(self.config, self.state, "12")
        self.issues.append({**self.issue, "number": 30, "title": FINDING["title"], "labels": [],
                            "url": "https://github.com/example/project/issues/30"})
        source = discover(self.config, self.state, "12", [FINDING])
        self.assertEqual(source["discoveries"][0]["status"], "existing")
        self.assertEqual(len(self.issue_creates), 0)
        other = {**FINDING, "title": "A second independently observed bug"}
        source = discover(self.config, self.state, "12", [other])
        created = self.issues[-1]
        self.issues.append({**created, "number": 31, "url": "https://github.com/example/project/issues/31"})
        report = queue_report(self.config, snapshot(self.config, self.state))
        self.assertEqual([t["id"] for t in report["triage_candidates"]], ["21"])
        for identifier in ("30", "31"):
            with self.assertRaisesRegex(RelayError, "controller-created"):
                triage(self.config, self.state, identifier, prepare_only=True)

    def test_discovery_review_resumes_and_reuses_current_review_after_restart(self):
        start(self.config, self.state, "12")
        discover(self.config, self.state, "12", [FINDING])
        # Simulate interruption after publishing a PR but before automatic review.
        with patch("devrelay.delivery.advance", side_effect=lambda c, s, i: s.get(str(i))):
            task = work(self.config, self.state, "21")
        self.assertEqual(task["status"], "pr_open")
        task.pop("discovery_source")  # Older saved tasks reconstruct source from retained publication receipts.
        self.state.save(task)
        with patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            task = work(self.config, self.state, "21")
            self.assertEqual(task["status"], "reviewed")
            with patch("devrelay.codex.review", side_effect=AssertionError("Review already current")):
                task = work(self.config, self.state, "21")
        self.assertEqual(task["review"]["verdict"], "approve")

    def test_preapproved_discovery_still_reviews_and_charges_budget_without_triage(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_DISCOVERIES": json.dumps([FINDING])}):
            self.watching(max_tasks=1)
        self.issues[-1]["labels"] = [{"name": "auto:approved"}]
        with patch("devrelay.codex.triage", side_effect=AssertionError("Explicit approval bypasses triage")), \
                patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            self.watching(max_tasks=1)
        self.assertEqual(self.state.get("21")["status"], "reviewed")
        self.assertEqual(self.state.metadata("watch_budget")["used"], 1)

    def test_default_review_blocks_delivery_and_parks_only_discovered_task(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_DISCOVERIES": json.dumps([FINDING])}):
            self.watching(max_tasks=1)
        with patch.dict(os.environ, {"DEVRELAY_TEST_REVIEW": "changes_requested"}), \
                patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            child = work(self.config, self.state, "21")
        self.assertEqual(child["status"], "review_blocked")
        self.assertEqual(child["handoff"]["kind"], "budget")
        self.assertEqual(child["workflow"]["used"], self.config.data["codex"]["max_attempts"])
        self.assertNotIn("merge_request", child)
        self.issues.append({**self.issue, "number": 22, "title": "Another approved human task"})
        self.assertEqual(selection(self.config, self.state)["id"], "22")

    def test_changing_source_policy_or_requirements_revokes_automatic_acceptance(self):
        start(self.config, self.state, "12")
        discover(self.config, self.state, "12", [FINDING])
        with patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            work(self.config, self.state, "21")
        self.issues[-1]["body"] += "\nNew requirements"
        with self.assertRaisesRegex(RelayError, "triage.*stale"):
            start(self.config, self.state, "21")
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("[discovery]", "[discovery]\nauto_triage = false"))
        self.config = load(self.root)
        with self.assertRaisesRegex(RelayError, "auto:approved"):
            start(self.config, self.state, "21")

    def test_discovery_policy_can_disable_triage_and_review_separately(self):
        start(self.config, self.state, "12")
        discover(self.config, self.state, "12", [FINDING])
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("[discovery]", "[discovery]\nauto_triage = false\nauto_review = false"))
        self.config = load(self.root)
        with self.assertRaisesRegex(RelayError, "auto:approved"):
            work(self.config, self.state, "21")
        self.issues[-1]["labels"] = [{"name": "auto:approved"}]
        with patch("devrelay.codex.review", side_effect=AssertionError("Review opted out")):
            task = work(self.config, self.state, "21")
        self.assertEqual(task["status"], "pr_open")

    def test_discovery_needs_input_is_cached_and_default_budget_survives_restart(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_DISCOVERIES": json.dumps([FINDING])}):
            self.watching(max_tasks=1)
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text() + "\n[queue]\nmax_dispatches = 1\n")
        self.config = load(self.root)
        with patch.dict(os.environ, {"DEVRELAY_TEST_TRIAGE": "needs_input"}), \
                patch("devrelay.worker.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.watching()
        child = self.state.get("21")
        self.assertEqual(child["triage"]["result"]["decision"], "needs_input")
        self.assertNotIn("worktree", child)
        self.assertEqual(self.state.metadata("watch_budget")["used"], 1)
        self.assertEqual(selection(self.config, self.state)["status"], "idle")
        self.issues[-1]["body"] += "\nMaintainer decision: preserve existing behavior."
        self.assertEqual(self.watching()["reason"], "dispatch_budget")
        with patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            result = self.watching(reset_budget=True)
        self.assertEqual(result["reason"], "dispatch_budget")
        self.assertEqual(self.state.get("21")["triage"]["result"]["decision"], "ready")

    def test_review_can_report_preexisting_bug_without_blocking_current_pr(self):
        self.enable_review()
        with patch.dict(os.environ, {"DEVRELAY_TEST_REVIEW_DISCOVERIES": json.dumps([FINDING])}), \
                patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            task = work(self.config, self.state, "12")
        self.assertEqual(task["status"], "reviewed")
        self.assertEqual(task["review"]["verdict"], "approve")
        self.assertEqual(task["discoveries"][0]["observations"][0]["origin"], "review")
        self.assertEqual(len(self.issue_creates), 1)

    def test_rejected_review_remains_blocking_even_after_publishing_discovery(self):
        self.enable_review()
        with patch.dict(os.environ, {"DEVRELAY_TEST_REVIEW_DISCOVERIES": json.dumps([FINDING]),
                                     "DEVRELAY_TEST_REVIEW": "changes_requested"}), \
                patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            task = work(self.config, self.state, "12")
        self.assertEqual(task["status"], "review_blocked")
        self.assertEqual(task["handoff"]["kind"], "budget")
        self.assertEqual(len(self.issue_creates), 1)
        self.assertEqual(len(task["discoveries"][0]["observations"]), 1)

    def test_lost_create_response_recovers_without_duplicate_or_new_model_call(self):
        self.after_create_failure = RelayError("connection lost after creation")
        with patch.dict(os.environ, {"DEVRELAY_TEST_DISCOVERIES": json.dumps([FINDING])}):
            task = work(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        self.assertIn("discovery_error", task)
        self.assertEqual(task["discoveries"][0]["status"], "publishing")
        task = discover(self.config, self.state, "12")
        self.assertNotIn("discovery_error", task)
        self.assertEqual(task["discoveries"][0]["issue_id"], "21")
        self.assertEqual(len(self.issue_creates), 1)

    def test_uncertain_absent_write_needs_explicit_retry(self):
        start(self.config, self.state, "12")
        self.create_failure = RelayError("connection failed")
        with self.assertRaises(RelayError):
            discover(self.config, self.state, "12", [FINDING])
        self.create_failure = None
        with self.assertRaisesRegex(RelayError, "uncertain"):
            discover(self.config, self.state, "12")
        self.assertEqual(len(self.issue_creates), 1)
        task = discover(self.config, self.state, "12", retry_create=True)
        self.assertEqual(task["discoveries"][0]["status"], "published")
        self.assertEqual(len(self.issue_creates), 2)

    def test_existing_closed_issue_and_case_whitespace_variant_are_reused(self):
        start(self.config, self.state, "12")
        self.issues.append({**self.issue, "number": 50, "title": "  REJECT an empty export filename  ",
                            "state": "CLOSED", "url": "https://github.com/example/project/issues/50"})
        task = discover(self.config, self.state, "12", [FINDING])
        self.assertEqual(task["discoveries"][0]["status"], "existing")
        self.assertEqual(task["discoveries"][0]["issue_id"], "50")
        self.assertFalse(self.issue_creates)

    def test_revoked_source_authorization_keeps_report_but_prevents_creation(self):
        start(self.config, self.state, "12")
        self.issue["labels"] = []
        with self.assertRaisesRegex(RelayError, "Missing auto:approved"):
            discover(self.config, self.state, "12", [FINDING])
        self.assertFalse(self.issue_creates)
        self.assertEqual(self.state.get("12")["discoveries"][0]["status"], "recorded")

    def test_title_metacharacters_are_one_argument_and_body_is_stdin(self):
        start(self.config, self.state, "12")
        title = "Export rejects literal $(touch never-created) and `text`"
        discover(self.config, self.state, "12", [{**FINDING, "title": title}])
        call = self.issue_creates[0]
        self.assertEqual(call["argv"][3], title)
        self.assertIn(FINDING["evidence"], call["body"])
        self.assertFalse((self.root / "never-created").exists())

    def test_implementation_and_review_deduplicate_but_keep_both_observations(self):
        self.enable_review()
        with patch.dict(os.environ, {"DEVRELAY_TEST_DISCOVERIES": json.dumps([FINDING]),
                                     "DEVRELAY_TEST_REVIEW_DISCOVERIES": json.dumps([FINDING])}), \
                patch("devrelay.delivery.pull_snapshot", side_effect=self.pull_snapshot):
            task = work(self.config, self.state, "12")
        self.assertEqual(task["status"], "reviewed")
        self.assertEqual(len(self.issue_creates), 1)
        self.assertEqual([item["origin"] for item in task["discoveries"][0]["observations"]], ["implementation", "review"])
