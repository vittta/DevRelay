from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from devrelay.config import load
from devrelay.delivery import merge, pull_snapshot, review
from devrelay.errors import RelayError
from devrelay.git import git as real_git, has_ref
from devrelay.process import execute
from devrelay.providers import queue_report, snapshot
from devrelay.runner import prepare_task, run_task, start, submit, verify

from test_delivery import GitHubDeliveryFixture


class MergeTests(GitHubDeliveryFixture):
    def test_open_pr_repreparation_invalidates_review_and_requires_new_acceptance(self):
        self.enable(auto_merge=False, auto_review=True)
        task = self.deliver()
        old_review = Path(task["review_path"])
        with self.state.lock():
            prepared = prepare_task(self.config, self.state, "12")
            self.assertEqual(prepared["status"], "prepared")
            self.assertIsNone(prepared["verification"])
            self.assertIsNone(prepared["review"])
            with self.assertRaisesRegex(RelayError, "missing or stale"):
                review(self.config, self.state, "12")
            verify(self.config, self.state, "12")
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["status"], "reviewed")
        self.assertNotEqual(recovered["review_path"], str(old_review))
        self.assertTrue(old_review.is_file())
        self.assertEqual(self.create_count, 1)
        self.assertEqual(recovered["pr_url"], task["pr_url"])

    def test_open_pr_recovers_new_environment_configuration_without_a_model(self):
        with self.state.lock():
            task = start(self.config, self.state, "12")
            (Path(task["worktree"]) / "value.txt").write_text("fixed\n")
            verify(self.config, self.state, "12")
            task = submit(self.config, self.state, "12")
        config_path = self.root / ".devrelay/config.toml"
        config_path.write_text(config_path.read_text() +
            '\n[[setup]]\nid = "new-env"\nargv = ' + json.dumps([
                sys.executable, "-c", "from pathlib import Path; p=Path('.devrelay/runtime/new-env'); p.parent.mkdir(parents=True, exist_ok=True); p.touch()"
            ]) + '\n[[checks]]\nid = "new-env-check"\nargv = ' + json.dumps([
                sys.executable, "-c", "from pathlib import Path; assert Path('.devrelay/runtime/new-env').exists()"
            ]) + '\n')
        self.git("add", ".devrelay/config.toml")
        self.git("commit", "-qm", "updated setup configuration")
        self.git("push", "-q", "origin", "main")
        self.config = load(self.root)
        self.git("merge", "--no-edit", "origin/main", cwd=task["worktree"])
        with self.state.lock(), patch("devrelay.runner.codex.implement", side_effect=AssertionError("Unexpected model")):
            with self.assertRaisesRegex(RelayError, "setup is missing or stale"):
                verify(self.config, self.state, "12")
            prepared = prepare_task(self.config, self.state, "12")
            self.assertEqual(prepared["setup_fingerprint"], self.config.fingerprint)
            with self.assertRaisesRegex(RelayError, "missing or stale"):
                submit(self.config, self.state, "12")
            verify(self.config, self.state, "12")
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["status"], "pr_open")
        self.assertEqual(recovered["pr_url"], task["pr_url"])
        self.assertEqual(self.create_count, 1)
        self.assertEqual(self.edit_count, 1)

    def test_prepare_rejects_closed_or_changed_pr_without_touching_evidence(self):
        task = self.deliver()
        original = self.state.get("12")
        for changes in ({"state": "CLOSED"}, {"headRefOid": "0" * 40}, {"baseRefName": "other"}):
            def change_pull(pull):
                pull.update(changes)
                if "headRefOid" in changes:
                    pull["commits"]["nodes"][0]["commit"]["oid"] = changes["headRefOid"]
            self.query_hook = change_pull
            with self.subTest(changes=changes), self.state.lock():
                with self.assertRaisesRegex(RelayError, "closed, merged or differs"):
                    prepare_task(self.config, self.state, "12")
            self.assertEqual(self.state.get("12"), original)
        self.assertTrue(Path(task["worktree"]).exists())

    def test_prepare_rejects_completed_merge_and_retains_acceptance(self):
        self.enable()
        task = self.deliver()
        original = self.state.get("12")
        with self.state.lock():
            with self.assertRaisesRegex(RelayError, "already integrated"):
                prepare_task(self.config, self.state, "12")
        self.assertEqual(self.state.get("12"), original)
        self.assertTrue(task["post_merge"]["passed"])

    def setUp(self):
        super().setUp()
        self.draft = True
        self.merged = False
        self.merge_oid = None
        self.merge_count = 0
        self.ready_count = 0
        self.request_timeout = False
        self.request_pending = False
        self.unexpected_tree = False
        self.head_override = None
        self.query_hook = None
        self.checks = [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.threads = []
        self.reviews = []
        self.review_decision = None
        self.merge_state = "CLEAN"
        self.truncated = False
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("devrelay.delivery.command", side_effect=self.query))
        stack.enter_context(patch("devrelay.delivery.gh", side_effect=self.mutate))
        stack.enter_context(patch("devrelay.delivery.validate_remote"))

    def enable(self, **settings):
        settings = {"auto_merge": True, **settings}
        path = self.root / ".devrelay/config.toml"
        text = path.read_text() + "\n[delivery]\n"
        text += "\n".join(key + " = " + json.dumps(value) for key, value in settings.items()) + "\n"
        path.write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", "delivery policy")
        self.git("push", "-q", "origin", "main")
        self.config = load(self.root)

    def query(self, argv, cwd):
        self.assertEqual(argv[:5], ["gh", "api", "--hostname", "github.com", "graphql"])
        self.assertIn("owner=example", argv)
        self.assertIn("name=project", argv)
        self.assertIn("number=1", argv)
        task = self.state.get("12")
        pull = {"id": "PR-test", "mergeQueueEntry": {"id": "queue-test"} if self.request_pending and self.merge_count and not self.merged else None,
                "autoMergeRequest": None, "url": task["pr_url"], "state": "MERGED" if self.merged else "OPEN",
                "headRefName": task["branch"], "headRefOid": self.head_override or task["head_sha"],
                "baseRefName": "main", "baseRefOid": self.git("rev-parse", "origin/main"),
                "isCrossRepository": False, "isDraft": self.draft, "mergeable": "MERGEABLE",
                "mergeStateStatus": self.merge_state, "reviewDecision": self.review_decision,
                "mergeCommit": {"oid": self.merge_oid} if self.merge_oid else None,
                "reviewThreads": {"nodes": self.threads, "pageInfo": {"hasNextPage": self.truncated}},
                "latestReviews": {"nodes": self.reviews, "pageInfo": {"hasNextPage": False}},
                "commits": {"nodes": [{"commit": {"oid": self.head_override or task["head_sha"],
                "statusCheckRollup": {"contexts": {"nodes": self.checks, "pageInfo": {"hasNextPage": False}}}}}]}}
        if self.query_hook:
            self.query_hook(pull)
        return json.dumps({"data": {"repository": {"pullRequest": pull}}})

    def server_merge(self):
        server = Path(self.temporary.name) / "server-merge"
        self.git("worktree", "add", "--detach", str(server), "origin/main")
        self.git("merge", "--squash", self.state.get("12")["branch"], cwd=server)
        if self.unexpected_tree:
            (server / "unexpected.txt").write_text("unreviewed base change\n")
            self.git("add", ".", cwd=server)
        self.git("commit", "-qm", "server squash", cwd=server)
        self.merge_oid = self.git("rev-parse", "HEAD", cwd=server)
        self.git("push", "-q", "origin", "HEAD:main", cwd=server)
        self.git("worktree", "remove", str(server))
        self.merged = True

    def mutate(self, config, *argv, input=None):
        if argv[1] == "ready":
            self.draft = False
            self.ready_count += 1
            return ""
        self.assertEqual(argv[0:2], ("pr", "merge"))
        self.assertIn("--squash", argv)
        self.assertNotIn("--admin", argv)
        self.assertNotIn("--auto", argv)
        self.assertEqual(argv[argv.index("--match-head-commit") + 1], self.state.get("12")["head_sha"])
        self.merge_count += 1
        if not self.request_pending:
            self.server_merge()
        if self.request_timeout:
            raise RelayError("Merge request timed out")
        return ""

    def deliver(self):
        self.implement()
        with self.state.lock():
            return submit(self.config, self.state, "12")

    def add_dependency_submodule(self):
        source = Path(self.temporary.name) / "review-dependency"
        source.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.name", "DevRelay Test"], cwd=source, check=True)
        subprocess.run(["git", "config", "user.email", "devrelay@example.invalid"], cwd=source, check=True)
        (source / "dependency.txt").write_text("delivered\n")
        subprocess.run(["git", "add", "."], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "dependency"], cwd=source, check=True)
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(source), "vendor/dependency"], cwd=self.root, check=True)
        self.git("commit", "-qam", "add dependency")
        self.git("push", "-q", "origin", "main")

    def test_opt_in_runs_fresh_review_exact_head_merge_and_real_post_checks(self):
        self.enable(required_checks=["test"])
        trace = Path(self.temporary.name) / "trace.jsonl"
        with patch.dict("os.environ", {"DEVRELAY_TEST_TRACE": str(trace)}):
            result = self.deliver()
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(events), 2)
        self.assertIn("read-only", events[1]["argv"])
        self.assertNotIn("resume", events[1]["argv"])
        self.assertNotEqual(result["session_id"], result["review"]["session_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["post_merge"]["source_tree"], result["verification"]["source_tree"])
        self.assertTrue(result["post_merge"]["passed"])
        self.assertEqual(len(result["post_merge"]["checks"]), 1)
        self.assertNotEqual(result["merge_sha"], result["head_sha"])
        self.assertEqual(self.merge_count, 1)
        self.assertEqual(self.ready_count, 1)
        self.assertEqual((self.root / "value.txt").read_text(), "broken\n")
        self.assertNotIn("post_merge_worktree", result)
        with self.state.lock():
            again = merge(self.config, self.state, "12")
        self.assertEqual(again["merge_sha"], result["merge_sha"])
        self.assertEqual(self.merge_count, 1)

    def test_merge_disabled_by_default_and_manual_review_allowed(self):
        task = self.deliver()
        self.assertEqual(task["status"], "pr_open")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "disabled"):
            merge(self.config, self.state, "12")
        with self.state.lock():
            result = review(self.config, self.state, "12")
        self.assertEqual(result["status"], "reviewed")
        self.assertEqual(self.ready_count, 0)
        self.assertEqual(self.merge_count, 0)

    def test_auto_review_only_retains_draft(self):
        self.enable(auto_merge=False, auto_review=True)
        result = self.deliver()
        self.assertEqual(result["status"], "reviewed")
        self.assertTrue(self.draft)
        self.assertEqual(self.merge_count, 0)

    def test_negative_review_blocks_merge_and_new_dispatch(self):
        self.enable()
        with patch.dict("os.environ", {"DEVRELAY_TEST_REVIEW": "changes_requested"}):
            result = self.deliver()
        self.assertEqual(result["status"], "review_blocked")
        self.assertEqual(self.merge_count, 0)
        self.assertEqual(self.ready_count, 0)
        with self.state.lock():
            pending = run_task(self.config, self.state)
        self.assertEqual(pending["status"], "needs_attention")
        self.assertEqual(pending["tasks"], ["12"])

    def test_invalid_or_incomplete_review_never_merges(self):
        self.enable()
        self.implement()
        # Retain the submitted PR while retrying independent review reports.
        for mode in ("invalid", "inconsistent", "same-session", "incomplete", "failure"):
            with self.subTest(mode=mode), patch.dict("os.environ", {"DEVRELAY_TEST_REVIEW": mode}):
                with self.state.lock(), self.assertRaises(RelayError):
                    submit(self.config, self.state, "12")
                self.assertEqual(self.state.get("12")["status"], "review_blocked")
                self.assertIsNone(self.state.get("12")["review"])
        self.assertEqual(self.merge_count, 0)

    def test_read_only_review_source_mutation_invalidates_evidence(self):
        self.enable()
        with patch.dict("os.environ", {"DEVRELAY_TEST_REVIEW": "modify"}):
            with self.assertRaisesRegex(RelayError, "stale"):
                self.deliver()
        self.assertEqual(self.merge_count, 0)

    def test_dirty_submodule_blocks_review_gate(self):
        self.add_dependency_submodule()
        task = self.deliver()
        worktree = Path(task["worktree"])
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init",
                        "--recursive"], cwd=worktree, check=True)
        dependency = worktree / "vendor/dependency/dependency.txt"
        dependency.write_text("not delivered\n")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            review(self.config, self.state, "12")
        self.assertEqual(self.state.get("12")["status"], "review_blocked")
        self.assertEqual(dependency.read_text(), "not delivered\n")

    def test_ci_waiting_reuses_review_then_completes_without_reimplementation(self):
        self.enable(required_checks=["test"])
        self.checks[0]["status"] = "IN_PROGRESS"
        trace = Path(self.temporary.name) / "trace.jsonl"
        with patch.dict("os.environ", {"DEVRELAY_TEST_TRACE": str(trace)}):
            waiting = self.deliver()
            self.assertEqual(waiting["status"], "merge_waiting")
            self.assertEqual(self.merge_count, 0)
            self.checks[0]["status"] = "COMPLETED"
            with self.state.lock():
                result = run_task(self.config, self.state, submit_after=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(trace.read_text().splitlines()), 2)
        self.assertEqual(self.ready_count, 1)

    def test_ci_failures_missing_names_and_github_reviews_block(self):
        self.enable(required_checks=["test"])
        self.checks[0]["conclusion"] = "FAILURE"
        self.deliver()
        scenarios = [([], [], [], None, "CLEAN"),
                     (self.checks, [], [], None, "CLEAN"),
                     ([{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SKIPPED"}], [], [], None, "CLEAN"),
                     ([{"__typename": "StatusContext", "context": "test", "state": "SUCCESS"}], [{"isResolved": False}], [], None, "CLEAN"),
                     ([{"__typename": "StatusContext", "context": "test", "state": "SUCCESS"}], [], [{"state": "CHANGES_REQUESTED"}], None, "CLEAN"),
                     ([{"__typename": "StatusContext", "context": "test", "state": "SUCCESS"}], [], [], "REVIEW_REQUIRED", "CLEAN"),
                     ([{"__typename": "StatusContext", "context": "test", "state": "SUCCESS"}], [], [], None, "BLOCKED")]
        for checks, threads, reviews, decision, state in scenarios:
            self.checks, self.threads, self.reviews = checks, threads, reviews
            self.review_decision, self.merge_state = decision, state
            with self.state.lock():
                result = merge(self.config, self.state, "12")
            self.assertEqual(result["status"], "merge_waiting")
        self.assertEqual(self.merge_count, 0)

    def test_no_ci_requires_explicit_allowance(self):
        self.enable(allow_no_ci=True)
        self.checks = []
        self.assertEqual(self.deliver()["status"], "completed")

    def test_required_names_still_apply_when_no_ci_allowed(self):
        self.enable(allow_no_ci=True, required_checks=["test"])
        self.checks = []
        self.assertEqual(self.deliver()["status"], "merge_waiting")
        self.assertEqual(self.merge_count, 0)

    def test_truncated_or_graphql_error_data_is_rejected(self):
        self.deliver()
        self.truncated = True
        with self.assertRaisesRegex(RelayError, "incomplete"):
            pull_snapshot(self.config, self.state.get("12"))
        with patch("devrelay.delivery.command", return_value=json.dumps({"errors": [{"message": "denied"}]})):
            with self.assertRaisesRegex(RelayError, "incomplete"):
                pull_snapshot(self.config, self.state.get("12"))

    def test_changed_remote_head_never_merges(self):
        self.enable()
        self.head_override = "0" * 40
        with self.assertRaisesRegex(RelayError, "HEAD differs"):
            self.deliver()
        self.assertEqual(self.merge_count, 0)

    def test_base_or_issue_changes_after_review_block_merge(self):
        self.enable()
        self.checks[0]["status"] = "IN_PROGRESS"
        self.deliver()
        self.issue["body"] += "\nChanged acceptance"
        with self.state.lock(), self.assertRaisesRegex(RelayError, "stale"):
            merge(self.config, self.state, "12")
        self.issue["body"] = self.state.get("12")["body"]
        (self.root / "base.txt").write_text("advanced\n")
        self.git("add", ".")
        self.git("commit", "-qm", "advance base")
        self.git("push", "-q", "origin", "main")
        with self.state.lock(), self.assertRaisesRegex(RelayError, "base changed"):
            merge(self.config, self.state, "12")
        self.assertEqual(self.merge_count, 0)

    def test_authorization_withdrawn_after_review_blocks_merge(self):
        self.enable()
        self.checks[0]["status"] = "IN_PROGRESS"
        self.deliver()
        self.issue["labels"] = [{"name": "priority:P1"}]
        with self.state.lock(), self.assertRaisesRegex(RelayError, "not eligible"):
            merge(self.config, self.state, "12")
        self.assertEqual(self.merge_count, 0)

    def test_timeout_after_remote_merge_recovers_readback_once(self):
        self.enable()
        self.request_timeout = True
        self.assertEqual(self.deliver()["status"], "completed")
        self.assertEqual(self.merge_count, 1)

    def test_queued_merge_is_pending_until_remote_readback(self):
        self.enable()
        self.request_pending = True
        result = self.deliver()
        self.assertEqual(result["status"], "merge_waiting")
        self.server_merge()
        with self.state.lock():
            result = merge(self.config, self.state, "12")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.merge_count, 1)

    def test_submit_recovers_queued_merge_without_repeating_publication_or_merge_request(self):
        self.enable(required_checks=["test"])
        self.request_pending = True
        waiting = self.deliver()
        self.assertEqual(waiting["status"], "merge_waiting")
        with self.state.lock():
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["status"], "merge_waiting")
        self.assertEqual(recovered["merge_request"], waiting["merge_request"])
        self.assertEqual(recovered["review"], waiting["review"])
        self.assertEqual((self.create_count, self.edit_count, self.merge_count), (1, 0, 1))
        self.server_merge()
        with self.state.lock():
            completed = submit(self.config, self.state, "12")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual((self.create_count, self.edit_count, self.merge_count), (1, 0, 1))

    def test_post_merge_check_failure_preserves_merged_state_and_retry_does_not_merge_again(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        def fail_post(state, argv, cwd, log, timeout, **kwargs):
            if "-post-" in str(cwd):
                raise RelayError("Post-merge environment unavailable")
            return execute(state, argv, cwd, log, timeout, **kwargs)
        with patch("devrelay.delivery.execute", side_effect=fail_post), self.assertRaisesRegex(RelayError, "environment"):
            self.deliver()
        failed = self.state.get("12")
        self.assertEqual(failed["status"], "post_merge_failed")
        self.assertTrue(failed["merge_sha"])
        self.assertFalse(failed["post_merge"]["passed"])
        self.assertTrue(Path(failed["post_merge_worktree"]).exists())
        self.assertTrue(Path(failed["worktree"]).exists())
        self.assertTrue(has_ref(self.root, failed["branch"]))
        self.assertTrue(self.git("--git-dir", str(self.remote), "rev-parse", failed["branch"]))
        with self.state.lock():
            result = merge(self.config, self.state, "12")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.merge_count, 1)
        self.assertTrue(Path(failed["post_merge_worktree"]).exists())
        self.assertIn(failed["post_merge_worktree"], result["retained_worktrees"])
        self.assertFalse(Path(failed["worktree"]).exists())

    def test_unexpected_merged_tree_is_not_reported_complete(self):
        self.enable()
        self.unexpected_tree = True
        with self.assertRaisesRegex(RelayError, "Merged tree differs"):
            self.deliver()
        failed = self.state.get("12")
        self.assertEqual(failed["status"], "post_merge_failed")
        self.assertFalse(failed["post_merge"]["passed"])
        self.assertEqual(self.merge_count, 1)

    def test_configuration_changes_invalidate_review(self):
        self.enable()
        self.checks[0]["status"] = "IN_PROGRESS"
        self.deliver()
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("auto_merge = true", "auto_merge = false"))
        with self.state.lock(), self.assertRaisesRegex(RelayError, "configuration changed"):
            merge(self.config, self.state, "12")
        self.assertEqual(self.merge_count, 0)

    def test_ci_regression_during_last_gate_read_prevents_mutation(self):
        self.enable()
        def regress(pull):
            if self.state.get("12")["status"] == "merging":
                pull["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]["conclusion"] = "FAILURE"
        self.query_hook = regress
        result = self.deliver()
        self.assertEqual(result["status"], "merge_waiting")
        self.assertEqual(self.merge_count, 0)
        self.assertIn("CI check", result["error"])

    def test_lost_api_readback_after_merge_is_recovered_without_second_mutation(self):
        self.enable()
        def fail_readback(pull):
            if self.merged:
                raise RelayError("Readback unavailable")
        self.query_hook = fail_readback
        with self.assertRaisesRegex(RelayError, "Readback unavailable"):
            self.deliver()
        self.assertEqual(self.state.get("12")["status"], "merge_blocked")
        self.query_hook = None
        with self.state.lock():
            result = merge(self.config, self.state, "12")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.merge_count, 1)

    def test_completed_pr_identity_is_rechecked(self):
        self.enable()
        self.deliver()
        self.head_override = "0" * 40
        with self.state.lock(), self.assertRaisesRegex(RelayError, "readback differs"):
            merge(self.config, self.state, "12")
        self.assertEqual(self.state.get("12")["status"], "post_merge_failed")
        self.assertEqual(self.merge_count, 1)

    def test_cleanup_is_disabled_by_default(self):
        self.enable()
        result = self.deliver()
        self.assertTrue(Path(result["worktree"]).exists())
        self.assertTrue(has_ref(self.root, result["branch"]))
        self.assertTrue(self.git("--git-dir", str(self.remote), "rev-parse", result["branch"]))
        self.assertEqual([step["id"] for step in result["cleanup"]["steps"]], ["acceptance_worktree"])

    def test_successful_cleanup_removes_owned_trees_and_both_branches_and_is_idempotent(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        result = self.deliver()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["post_merge"]["passed"])
        self.assertEqual(result["cleanup"]["status"], "completed")
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertFalse(has_ref(self.root, result["branch"]))
        self.assertFalse(has_ref(self.root, "refs/remotes/origin/" + result["branch"]))
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)",
                                  "refs/heads/" + result["branch"]), "")
        self.assertTrue(has_ref(self.root, "main"))
        self.assertEqual((self.root / "value.txt").read_text(), "broken\n")
        self.assertTrue(Path(result["verification_path"]).exists())
        self.assertTrue(Path(result["review_path"]).exists())
        self.assertTrue(Path(result["post_merge_path"]).exists())
        self.assertTrue(Path(result["cleanup_path"]).exists())
        with self.state.lock(), patch("devrelay.delivery.execute") as checks:
            again = merge(self.config, self.state, "12")
            resumed = run_task(self.config, self.state, "12", resume=True, submit_after=True)
            submitted = submit(self.config, self.state, "12")
        checks.assert_not_called()
        self.assertEqual(again["status"], "completed")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(submitted["status"], "completed")
        self.assertEqual(self.merge_count, 1)
        # Refs preserves the issue scope. Removing its worktree must not re-dispatch the same open issue.
        self.assertIsNone(queue_report(self.config, snapshot(self.config, self.state))["selected"])
        with self.state.lock(), self.assertRaisesRegex(RelayError, "already completed"):
            start(self.config, self.state, "12")

    def test_cleanup_removes_the_custom_fetch_mapped_tracking_ref(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.git("config", "--add", "remote.origin.fetch",
                 "+refs/heads/devrelay/*:refs/remotes/custom/*")

        def check_upstream():
            task = self.state.get("12")
            self.assertEqual(self.git("rev-parse", "@{upstream}", cwd=task["worktree"]),
                             task["head_sha"])

        with self.after_acceptance(check_upstream):
            result = self.deliver()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(has_ref(self.root, "refs/remotes/custom/task-12"))
        self.assertFalse(has_ref(self.root, "refs/remotes/origin/" + result["branch"]))
        self.assertTrue(has_ref(self.root, "refs/remotes/origin/main"))

    def test_cleanup_removes_tracking_ref_excluded_by_negative_fetch_refspec(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.git("config", "--add", "remote.origin.fetch",
                 "+refs/heads/devrelay/*:refs/remotes/custom/*")
        self.git("config", "--add", "remote.origin.fetch", "^refs/heads/devrelay/*")

        def check_upstream():
            task = self.state.get("12")
            self.assertEqual(self.git("rev-parse", "@{upstream}", cwd=task["worktree"]),
                             task["head_sha"])
            self.assertEqual(self.git("rev-parse", "refs/remotes/custom/task-12"), task["head_sha"])

        with self.after_acceptance(check_upstream):
            result = self.deliver()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(has_ref(self.root, "refs/remotes/custom/task-12"))
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)",
                                  "refs/heads/" + result["branch"]), "")
        self.assertTrue(has_ref(self.root, "refs/remotes/origin/main"))

    def test_worktree_only_cleanup_keeps_branches(self):
        self.enable(cleanup_worktrees=True)
        result = self.deliver()
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertTrue(has_ref(self.root, result["branch"]))
        self.assertTrue(self.git("--git-dir", str(self.remote), "rev-parse", result["branch"]))

    def test_cleanup_waits_for_merge_and_post_merge_acceptance(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        self.checks[0]["status"] = "IN_PROGRESS"
        result = self.deliver()
        self.assertEqual(result["status"], "merge_waiting")
        self.assertNotIn("cleanup", result)
        self.assertTrue(Path(result["worktree"]).exists())
        self.assertTrue(has_ref(self.root, result["branch"]))

    def after_acceptance(self, action):
        def execute_then_change(state, argv, cwd, log, timeout, **kwargs):
            result = execute(state, argv, cwd, log, timeout, **kwargs)
            if "-post-" in str(cwd):
                action()
            return result
        return patch("devrelay.delivery.execute", side_effect=execute_then_change)

    def test_dirty_task_worktree_is_retained_and_cleanup_can_resume_without_reverification(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        def edit():
            task = self.state.get("12")
            (Path(task["worktree"]) / "personal.txt").write_text("preserve this untracked work\n")
        with self.after_acceptance(edit), self.assertRaisesRegex(RelayError, "new changes"):
            self.deliver()
        failed = self.state.get("12")
        self.assertEqual(failed["status"], "cleanup_failed")
        self.assertTrue(failed["post_merge"]["passed"])
        self.assertTrue(Path(failed["post_merge_worktree"]).exists())
        self.assertTrue(has_ref(self.root, failed["branch"]))
        with self.state.lock():
            self.assertEqual(run_task(self.config, self.state)["status"], "needs_attention")
        (Path(failed["worktree"]) / "personal.txt").unlink()
        with self.state.lock(), patch("devrelay.delivery.execute") as checks:
            result = merge(self.config, self.state, "12")
        checks.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertEqual(self.merge_count, 1)

    def test_advanced_remote_branch_is_preserved_before_any_cleanup(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        def advance():
            task = self.state.get("12")
            self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/" + task["branch"], self.merge_oid)
        with self.after_acceptance(advance), self.assertRaisesRegex(RelayError, "Remote task branch advanced"):
            self.deliver()
        failed = self.state.get("12")
        self.assertEqual(failed["status"], "cleanup_failed")
        self.assertTrue(Path(failed["worktree"]).exists())
        self.assertTrue(has_ref(self.root, failed["branch"]))
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", failed["branch"]), self.merge_oid)

    def test_advanced_local_branch_is_preserved(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        def advance():
            self.git("commit", "--allow-empty", "-qm", "new local work", cwd=self.state.get("12")["worktree"])
        with self.after_acceptance(advance), self.assertRaisesRegex(RelayError, "HEAD changed"):
            self.deliver()
        failed = self.state.get("12")
        self.assertTrue(Path(failed["worktree"]).exists())
        self.assertNotEqual(self.git("rev-parse", failed["branch"]), failed["head_sha"])

    def test_remote_delete_lease_blocks_a_concurrent_branch_update_and_partial_cleanup_recovers(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        def race(root, *argv, **kwargs):
            if argv[0] == "push":
                task = self.state.get("12")
                self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/" + task["branch"], self.merge_oid)
            return real_git(root, *argv, **kwargs)
        with patch("devrelay.cleanup.git", side_effect=race), self.assertRaises(RelayError):
            self.deliver()
        failed = self.state.get("12")
        self.assertEqual(failed["status"], "cleanup_failed")
        self.assertFalse(Path(failed["worktree"]).exists())
        self.assertTrue(has_ref(self.root, failed["branch"]))
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", failed["branch"]), self.merge_oid)
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/" + failed["branch"], failed["head_sha"])
        with self.state.lock(), patch("devrelay.delivery.execute") as checks:
            result = merge(self.config, self.state, "12")
        checks.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(has_ref(self.root, result["branch"]))
        self.assertEqual(self.merge_count, 1)

    def test_remote_deletion_timeout_is_read_back_before_retry(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        def timeout(root, *argv, **kwargs):
            result = real_git(root, *argv, **kwargs)
            if argv[0] == "push":
                raise RelayError("Delete timed out after the remote accepted it")
            return result
        with patch("devrelay.cleanup.git", side_effect=timeout):
            result = self.deliver()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(has_ref(self.root, result["branch"]))

    def test_rewrite_added_after_validation_cannot_redirect_remote_deletion(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        extra = Path(self.temporary.name) / "cleanup-race-extra.git"
        self.git("init", "--bare", "-q", str(extra))
        validation_calls = 0

        def validate_then_rewrite(config, root=None):
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 2:
                self.git("config", f"url.{extra}.pushInsteadOf", str(self.remote))
            return str(self.remote)

        def seed_extra():
            task = self.state.get("12")
            self.git("push", "-q", str(extra), task["branch"], cwd=task["worktree"])

        with (patch("devrelay.cleanup.validate_remote", side_effect=validate_then_rewrite),
              self.after_acceptance(seed_extra)):
            result = self.deliver()
        self.assertEqual(validation_calls, 2)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)",
                                  "refs/heads/" + result["branch"]), "")
        self.assertEqual(self.git("--git-dir", str(extra), "rev-parse", result["branch"]),
                         result["head_sha"])

    def test_url_named_alias_added_after_validation_cannot_redirect_cleanup(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        extras = [Path(self.temporary.name) / f"cleanup-alias-extra-{index}.git" for index in range(2)]
        for extra in extras:
            self.git("init", "--bare", "-q", str(extra))
        url = self.remote.as_uri()
        validation_calls = 0

        def validate_then_alias(config, root=None):
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 2:
                self.git("config", f"remote.{url}.url", str(extras[0]))
                for extra in extras:
                    self.git("config", "--add", f"remote.{url}.pushurl", str(extra))
            return url

        def seed_extras():
            task = self.state.get("12")
            for extra in extras:
                self.git("push", "-q", str(extra), task["branch"], cwd=task["worktree"])

        with (patch("devrelay.cleanup.validate_remote", side_effect=validate_then_alias),
              self.after_acceptance(seed_extras)):
            result = self.deliver()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(validation_calls, 2)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)",
                                  "refs/heads/" + result["branch"]), "")
        for extra in extras:
            self.assertEqual(self.git("--git-dir", str(extra), "rev-parse", result["branch"]),
                             result["head_sha"])

    def test_remote_branch_already_deleted_by_github_is_idempotent(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        def remove_remote():
            self.git("--git-dir", str(self.remote), "update-ref", "-d", "refs/heads/" + self.state.get("12")["branch"])
        with self.after_acceptance(remove_remote):
            result = self.deliver()
        self.assertEqual(result["status"], "completed")
        remote = next(step for step in result["cleanup"]["steps"] if step["id"] == "remote_branch")
        self.assertEqual(remote["status"], "already_absent")
        self.assertFalse(has_ref(self.root, result["branch"]))

    def test_locked_worktree_is_preserved(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        def lock():
            self.git("worktree", "lock", self.state.get("12")["worktree"])
        with self.after_acceptance(lock), self.assertRaisesRegex(RelayError, "locked"):
            self.deliver()
        failed = self.state.get("12")
        self.assertTrue(Path(failed["worktree"]).exists())
        self.assertTrue(has_ref(self.root, failed["branch"]))
        self.git("worktree", "unlock", failed["worktree"])

    def test_branch_used_in_another_worktree_is_preserved(self):
        self.enable(cleanup_branches=True, cleanup_worktrees=True)
        other = Path(self.temporary.name) / "other-worktree"
        def reuse():
            self.git("worktree", "add", "--force", str(other), self.state.get("12")["branch"])
        with self.after_acceptance(reuse), self.assertRaisesRegex(RelayError, "another worktree"):
            self.deliver()
        failed = self.state.get("12")
        self.assertTrue(Path(failed["worktree"]).exists())
        self.assertTrue(other.exists())
        self.assertTrue(has_ref(self.root, failed["branch"]))


if __name__ == "__main__":
    unittest.main()
