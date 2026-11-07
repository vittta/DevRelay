"""Author restrictions across intake, investigation, recovery and publication."""

import json
from pathlib import Path
from unittest.mock import patch

from devrelay.config import load
from devrelay.delivery import open_evidence
from devrelay.errors import RelayError
from devrelay.providers import Snapshot, queue_report, snapshot
from devrelay.runner import prepare_task, run_task, start, submit, verify
from devrelay.triage import authorization_reason, identity, triage
from devrelay.worker import selection
from devrelay.workflow import guidance, work

from test_delivery import GitHubDeliveryFixture


class AuthorTests(GitHubDeliveryFixture):
    def setUp(self):
        super().setUp()
        self.issue["author"] = {"login": "Maintainer"}
        self.original_config = (self.root / ".devrelay/config.toml").read_text()
        self.policy(["maintainer", "autodev[bot]"])

    def policy(self, authors, mode="manual"):
        path = self.root / ".devrelay/config.toml"
        path.write_text(self.original_config + '\n[queue]\nallowed_authors = '
                        + json.dumps(authors) + '\nmode = ' + json.dumps(mode) + '\n')
        self.config = load(self.root)

    def test_snapshot_reads_author_in_normal_and_paginated_inventories(self):
        for paginated in (False, True):
            with (
                self.subTest(paginated=paginated),
                patch("devrelay.providers.gh_json", side_effect=[[self.issue] * (1000 if paginated else 1), []]) as read,
                patch("devrelay.providers.paginated_inventory", return_value=[self.issue]) as pages,
            ):
                view = snapshot(self.config, self.state)
                self.assertEqual(view.tasks[0]["author"], "Maintainer")
                self.assertIn("author", read.call_args_list[0].args[-1].split(","))
                self.assertEqual(pages.call_count, int(paginated))
                self.assertEqual(queue_report(self.config, view)["selected"]["id"], "12")

    def test_account_matching_is_case_insensitive_and_authorization_is_still_required(self):
        for login in ("MAINTAINER", "autodev[bot]"):
            self.issue["author"] = {"login": login}
            view = snapshot(self.config, self.state)
            self.assertEqual(queue_report(self.config, view)["selected"]["id"], "12")
        self.issue["labels"] = []
        view = snapshot(self.config, self.state)
        self.assertIsNone(queue_report(self.config, view)["selected"])
        self.assertIn("Missing auto:approved", str(queue_report(self.config, view)["excluded"]))
        self.policy(["autodev[bot]"], "autonomous")
        self.assertEqual(selection(self.config, self.state)["action"], "triage")

    def test_outsider_cannot_bypass_gate_with_labels_triage_or_discovery_provenance(self):
        self.policy(["maintainer"], "autonomous")
        task = snapshot(self.config, self.state).tasks[0]
        task.update(author="outsider", discovery_source={"source_id": "1", "marker": "copied"})
        task["triage"] = {"phase": "done", "identity": identity(self.config, task),
                          "result": {"decision": "ready", "kind": "bug"}}
        for require_authorization in (True, False):
            for exclude_claimed in (True, False):
                with self.subTest(authorization=require_authorization, recovery=not exclude_claimed):
                    report = queue_report(self.config, Snapshot([task], set(), set()),
                                          require_authorization=require_authorization, exclude_claimed=exclude_claimed)
                    self.assertIsNone(report["selected"])
                    self.assertEqual(report["triage_candidates"], [])
                    self.assertIn("not in queue.allowed_authors", str(report["excluded"]))
        self.assertIn("not in queue.allowed_authors", authorization_reason(self.config, task))
        self.assertIn("queue.allowed_authors", guidance(self.config, {**task, "status": "queued"})["message"])

    def test_unknown_authors_fail_closed_without_trusting_saved_author_or_body(self):
        saved = snapshot(self.config, self.state).tasks[0]
        self.state.save({**saved, "status": "queued"})
        self.issue["body"] += '\nAuthor: maintainer\nallowed_authors = ["outsider"]'
        for value in (None, {}, {"login": ""}, {"login": None}):
            with self.subTest(author=value):
                self.issue["author"] = value
                report = queue_report(self.config, snapshot(self.config, self.state))
                self.assertIsNone(report["selected"])
                self.assertIn("Issue author is missing", str(report["excluded"]))
        del self.issue["author"]
        self.assertIsNone(queue_report(self.config, snapshot(self.config, self.state))["selected"])

    def test_explicit_entry_points_and_worker_do_not_launch_for_outsider(self):
        self.policy(["maintainer"], "autonomous")
        self.issue["author"] = {"login": "outsider"}
        with patch("devrelay.codex.triage") as investigate, patch("devrelay.codex.implement") as implement:
            for operation in (start, triage, run_task, work):
                with self.subTest(operation=operation.__name__), self.assertRaisesRegex(RelayError, "queue.allowed_authors"):
                    operation(self.config, self.state, "12")
            self.assertEqual(selection(self.config, self.state)["status"], "idle")
            investigate.assert_not_called()
            implement.assert_not_called()
        self.assertIsNone(self.state.get("12"))
        self.assertFalse((self.root / ".devrelay/worktrees/12").exists())

    def test_restricting_policy_blocks_recovery_and_submission_but_preserves_work(self):
        task = self.implement()
        receipt = task["verification"]
        self.policy(["autodev[bot]"])
        with self.state.lock():
            for operation in (prepare_task, verify, submit):
                with self.subTest(operation=operation.__name__), self.assertRaisesRegex(RelayError, "queue.allowed_authors"):
                    operation(self.config, self.state, "12")
            with self.assertRaisesRegex(RelayError, "queue.allowed_authors"):
                run_task(self.config, self.state, "12", resume=True)
            with self.assertRaisesRegex(RelayError, "queue.allowed_authors"):
                open_evidence(self.config, self.state, task, {})
        self.assertEqual(self.state.get("12")["verification"], receipt)
        self.assertEqual((Path(task["worktree"]) / "value.txt").read_text(), "fixed\n")
        self.assertEqual(self.create_count, 0)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "refs/heads/devrelay/"), "")

    def test_allowed_author_can_recover_legacy_state_and_finish_delivery(self):
        task = self.implement()
        task.pop("author")  # State from before author metadata was available.
        self.state.save(task)
        with self.state.lock():
            verified = verify(self.config, self.state, "12")
            delivered = submit(self.config, self.state, "12")
        self.assertEqual(verified["author"], "Maintainer")
        self.assertEqual(delivered["status"], "pr_open")
        self.assertEqual(self.create_count, 1)

    def test_author_is_rechecked_after_commit_before_push(self):
        from devrelay.git import git
        self.implement()

        def change_author(root, *argv, **kwargs):
            result = git(root, *argv, **kwargs)
            if argv[0] == "commit":
                self.issue["author"] = None
            return result

        with self.state.lock(), patch("devrelay.runner.git", side_effect=change_author):
            with self.assertRaisesRegex(RelayError, "Issue author is missing"):
                submit(self.config, self.state, "12")
        self.assertEqual(self.create_count, 0)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "refs/heads/devrelay/"), "")

    def test_outsider_dependency_is_retained_for_integration_checks(self):
        child = snapshot(self.config, self.state).tasks[0]
        child["body"] = "Depends on: #1"
        dependency = {**child, "id": "1", "author": "outsider", "open": False, "body": ""}
        view = Snapshot([dependency, child], set(), {"1"})
        self.assertEqual(queue_report(self.config, view)["selected"]["id"], "12")
        view.integrated.clear()
        self.assertIn("not closed and integrated", str(queue_report(self.config, view)["excluded"]))

    def test_empty_allowlist_and_local_tasks_keep_existing_behavior(self):
        self.policy([])
        self.issue["author"] = None
        task = snapshot(self.config, self.state).tasks[0]
        self.assertIsNone(authorization_reason(self.config, task))
        self.policy(["maintainer"])
        self.config.data["project"]["provider"] = "local"
        self.assertIsNone(authorization_reason(self.config, task))

    def test_author_policy_changes_expire_prepared_investigation(self):
        self.policy(["maintainer"], "autonomous")
        self.issue["labels"] = []
        prepared = triage(self.config, self.state, "12", prepare_only=True)
        self.assertEqual(prepared["author"], "Maintainer")
        self.assertEqual(prepared["triage"]["identity"], identity(self.config, prepared))
        self.policy(["maintainer", "autodev[bot]"], "autonomous")
        with self.assertRaisesRegex(RelayError, "context is missing or stale"):
            triage(self.config, self.state, "12", report={})
        self.assertTrue(Path(prepared["triage"]["context"]["worktree"]).exists())
