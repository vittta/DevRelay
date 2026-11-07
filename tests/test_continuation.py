"""Exercise task continuation with real Git, subprocesses and retained evidence."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from devrelay.config import load
from devrelay.delivery import review
from devrelay.errors import RelayError
from devrelay.workflow import work
from devrelay.worker import selection

from test_worker import WorkerFixture


class ContinuationTests(WorkerFixture):
    def setUp(self):
        super().setUp()
        self.issues = [self.issue]
        self.enable_review()
        self.git("add", ".devrelay/config.toml")
        self.git("commit", "-qm", "review policy")
        self.git("push", "-q", "origin", "main")
        self.trace = Path(self.temporary.name) / "continuation.jsonl"
        executable = Path(self.config.data["codex"]["executable"])
        text = executable.read_text()
        text = text.replace("review_mode = os.environ.get('DEVRELAY_TEST_REVIEW', 'approve')",
                            "review_mode = (('approve' if Path('regression-fixed.txt').exists() else 'changes_requested') "
                            "if mode == 'review-fix' else os.environ.get('DEVRELAY_TEST_REVIEW', 'approve'))")
        text = text.replace("if mode != 'blocked':",
                            "if mode == 'review-fix' and 'Fix the reported regression' in prompt:\n"
                            "    Path('regression-fixed.txt').write_text('verified fix\\n')\nif mode != 'blocked':")
        executable.write_text(text)
        for target, options in (("devrelay.delivery.pull_snapshot", {"side_effect": self.read_pull}),):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {"DEVRELAY_TEST_TRACE": str(self.trace)})
        environment.start()
        self.addCleanup(environment.stop)

    def read_pull(self, config, task):
        return {"state": "OPEN", "headRefName": task["branch"], "headRefOid": task["head_sha"],
                "baseRefName": "main", "baseRefOid": self.git("rev-parse", "origin/main"),
                "isCrossRepository": False}

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def continue_task(self, **kwargs):
        with self.state.lock():
            return work(self.config, self.state, "12", **kwargs)

    def test_review_findings_resume_implementation_recheck_and_fresh_review_same_pr(self):
        original = self.git("rev-parse", "HEAD")
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "review-fix"}):
            result = self.continue_task()
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(result["workflow"]["used"], 2)
        calls = self.calls()
        self.assertEqual(len(calls), 4)
        self.assertIn("resume", calls[2]["argv"])
        self.assertIn(result["session_id"], calls[2]["argv"])
        self.assertIn("Fix the reported regression", calls[2]["prompt"])
        self.assertNotIn("resume", calls[1]["argv"])
        self.assertNotIn("resume", calls[3]["argv"])
        self.assertEqual((self.create_count, self.edit_count), (1, 1))
        self.assertEqual(self.git("rev-parse", "HEAD"), original)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", result["branch"]), result["head_sha"])
        reviews = list((self.state.directory / "runs/12").glob("*-review/review.json"))
        self.assertEqual(len(reviews), 2)
        self.assertEqual(len({json.loads(path.read_text())["session_id"] for path in reviews}), 2)
        self.assertEqual(self.git("rev-parse", result["head_sha"] + "^{tree}"), result["verification"]["source_tree"])

    def test_budget_survives_restarts_and_explicit_retry_opens_one_new_budget(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_REVIEW": "changes_requested"}):
            result = self.continue_task()
            self.assertEqual(result["handoff"]["kind"], "budget")
            count = len(self.calls())
            self.assertEqual(self.continue_task()["handoff"]["kind"], "budget")
            self.assertEqual(len(self.calls()), count)
            self.assertEqual(selection(self.config, self.state)["status"], "needs_attention")
            result = self.continue_task(retry=True)
        self.assertEqual(result["workflow"]["used"], 2)
        self.assertEqual(len(result["workflow_history"]), 1)
        self.assertEqual(result["workflow_history"][0]["used"], 2)
        self.assertGreater(len(self.calls()), count)
        self.assertEqual(self.create_count, 1)

    def test_larger_configured_budget_is_not_cut_short_by_phase_transitions(self):
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_attempts = 2", "max_attempts = 9"))
        self.config = load(self.root)
        with patch.dict(os.environ, {"DEVRELAY_TEST_REVIEW": "changes_requested"}):
            result = self.continue_task()
        self.assertEqual(result["workflow"]["used"], 9)
        self.assertEqual(result["handoff"]["kind"], "budget")
        self.assertEqual(result["status"], "review_blocked")

    def test_incomplete_review_requires_input_not_an_implementation_attempt(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_REVIEW": "blocked"}):
            result = self.continue_task()
        self.assertEqual(result["workflow"]["used"], 1)
        self.assertEqual(result["handoff"]["kind"], "attention")
        self.assertEqual(len(self.calls()), 2)
        result = self.continue_task(retry=True)
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(len(self.calls()), 3)
        self.assertNotIn("handoff", result)

    def test_check_repair_and_review_share_a_persisted_implementation_budget(self):
        with patch.dict(os.environ, {"DEVRELAY_TEST_MODE": "retry"}):
            result = self.continue_task()
        self.assertEqual(result["workflow"]["used"], 2)
        self.assertEqual(result["status"], "reviewed")
        self.assertIn('"kind": "checks"', self.calls()[1]["prompt"])
        self.assertEqual(len(self.calls()), 3)

    def test_model_change_only_repeats_review_and_preserves_check_receipt(self):
        previous = self.continue_task()
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("[codex]", '[codex]\nmodel = "another-model"'))
        self.config = load(self.root)
        result = self.continue_task()
        self.assertEqual(result["verification_path"], previous["verification_path"])
        self.assertEqual(result["setup_path"], previous["setup_path"])
        self.assertNotEqual(result["review_path"], previous["review_path"])
        self.assertEqual(len(self.calls()), 3)

    def test_clean_base_advance_rechecks_resubmits_and_reviews_without_reimplementation(self):
        previous = self.continue_task()
        (self.root / "upstream.txt").write_text("independent base change\n")
        self.git("add", "upstream.txt")
        self.git("commit", "-qm", "advance base")
        self.git("push", "-q", "origin", "main")
        result = self.continue_task()
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(result["attempts"], previous["attempts"])
        self.assertNotEqual(result["verification_path"], previous["verification_path"])
        self.assertNotEqual(result["review_path"], previous["review_path"])
        self.assertEqual(result["verification"]["base_sha"], self.git("rev-parse", "main"))
        self.assertEqual((self.create_count, self.edit_count), (1, 1))

    def test_base_advance_with_edits_preserves_work_and_does_not_launch_agent(self):
        previous = self.continue_task()
        edited = Path(previous["worktree"]) / "value.txt"
        edited.write_text("external work in progress\n")
        (self.root / "upstream.txt").write_text("new base\n")
        self.git("add", "upstream.txt")
        self.git("commit", "-qm", "advance base")
        self.git("push", "-q", "origin", "main")
        with self.assertRaisesRegex(RelayError, "worktree has edits"):
            self.continue_task()
        self.assertEqual(edited.read_text(), "external work in progress\n")
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.state.get("12")["handoff"]["kind"], "attention")

    def test_base_merge_conflict_keeps_conflict_and_original_head(self):
        previous = self.continue_task()
        (self.root / "value.txt").write_text("upstream replacement\n")
        self.git("commit", "-qam", "conflicting base")
        self.git("push", "-q", "origin", "main")
        with self.assertRaises(RelayError):
            self.continue_task()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=previous["worktree"]), previous["head_sha"])
        self.assertTrue(self.git("ls-files", "--unmerged", cwd=previous["worktree"]))
        self.assertIn("upstream replacement", (Path(previous["worktree"]) / "value.txt").read_text())
        self.assertEqual(len(self.calls()), 2)

    def test_new_review_cannot_reuse_a_previous_review_session(self):
        previous = self.continue_task()
        executable = Path(self.config.data["codex"]["executable"])
        executable.write_text(executable.read_text().replace(
            "'fresh-review-session' + __import__('uuid').uuid4().hex", json.dumps(previous["review"]["session_id"])))
        with self.state.lock(), self.assertRaisesRegex(RelayError, "previously owned session"):
            review(self.config, self.state, "12")
        self.assertEqual(self.state.get("12")["status"], "review_blocked")
        self.assertIsNone(self.state.get("12")["review"])
        self.assertTrue(Path(previous["review_path"]).is_file())
