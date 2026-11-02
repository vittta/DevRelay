import unittest
from unittest.mock import patch

from devrelay.errors import RelayError
from devrelay.providers import Snapshot, dependencies, gh, linked_ids, queue_report, snapshot, task_id

from helpers import RepositoryTest


def issue(identifier, *, labels=None, body="Depends on: none", opened=True, created="2026-10-09T00:00:00Z"):
    return {"id": str(identifier), "title": "Task", "body": body, "open": opened,
            "labels": labels if labels is not None else ["auto:approved", "priority:P1"], "created_at": created}


class QueueTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure()

    def report(self, tasks, claimed=(), integrated=()):
        return queue_report(self.config, Snapshot(tasks, set(claimed), set(integrated)))

    def test_explicit_approval_and_unique_priority_are_required(self):
        for labels in (["research", "priority:P1"], ["auto:approved"],
                       ["auto:approved", "priority:P0", "priority:P1"],
                       ["auto:approved", "priority:P1", "blocked"],
                       ["auto:approved", "priority:P1", "needs-input"]):
            with self.subTest(labels=labels):
                self.assertIsNone(self.report([issue(1, labels=labels)])["selected"])

    def test_priority_then_creation_time(self):
        tasks = [issue(1, created="2026-10-09T01:00:00Z"), issue(2),
                 issue(3, labels=["auto:approved", "priority:P0"])]
        report = self.report(tasks)
        self.assertEqual([task["id"] for task in report["eligible"]], ["3", "2", "1"])

    def test_authorized_plan_parent_is_skipped_and_child_remains_eligible(self):
        parent = issue(1, labels=["scope:plan", "auto:approved", "priority:P0"])
        child = issue(2, body="Parent: #1\nDeliver one tested fix.\nDepends on: none")
        report = self.report([parent, child])
        self.assertEqual([task["id"] for task in report["eligible"]], ["2"])
        self.assertIn("Tracking plan parents are not executable", str(report["excluded"]))

    def test_plan_markers_block_selection_and_recovery_for_both_providers(self):
        markers = ("Task kind: plan", "Task kind: PLAN\r\n",
                   "Task kind: implementation\nTask kind: plan",
                   "## Child issues and delivery plan / 子任务与交付计划",
                   "## Child issues and delivery plan")
        for provider in ("local", "github"):
            self.config.data["project"]["provider"] = provider
            for marker in markers:
                for exclude_claimed in (True, False):
                    with self.subTest(provider=provider, marker=marker, recovery=not exclude_claimed):
                        parent = issue(1, body=marker + "\nDepends on: none")
                        report = queue_report(self.config, Snapshot([parent], set(), set()),
                                              exclude_claimed=exclude_claimed)
                        self.assertIsNone(report["selected"])
                        self.assertIn("Tracking plan parents are not executable", str(report["excluded"]))

    def test_custom_authorization_cannot_override_plan_label(self):
        self.config.data["queue"].update(approval_label="execute", priorities=["urgent"])
        parent = issue(1, labels=["SCOPE:PLAN", "execute", "urgent"])
        self.assertIsNone(self.report([parent])["selected"])

    def test_child_mentions_of_plan_markers_do_not_classify_it_as_parent(self):
        child = issue(2, body="Parent: #1 (scope:plan).\nRemove the old Task kind: plan marker.\n"
                     "Depends on: none")
        child["title"] = "Implement the plan's independent validation result"
        self.assertEqual(self.report([child])["selected"]["id"], "2")

    def test_dependencies_require_closed_integrated_results(self):
        tasks = [issue(1, opened=False), issue(2, body="Depends on: #1")]
        self.assertIsNone(self.report(tasks)["selected"])
        self.assertEqual(self.report(tasks, integrated={"1"})["selected"]["id"], "2")
        tasks[0]["open"] = True
        self.assertNotIn("2", [task["id"] for task in self.report(tasks, integrated={"1"})["eligible"]])

    def test_cycle_unknown_and_malformed_dependencies_explained(self):
        for tasks, reason in (([issue(1, body="Depends on: #2")], "Unknown dependency"),
                              ([issue(1, body="Depends on: #1")], "Dependency cycle"),
                              ([issue(1, body="")], "Exactly one"),
                              ([issue(1, body="Depends on: #2, #2")], "Duplicate")):
            with self.subTest(reason=reason):
                report = self.report(tasks)
                self.assertIsNone(report["selected"])
                self.assertIn(reason, str(report["excluded"]))

    def test_claimed_task_can_be_authorized_for_recovery(self):
        view = Snapshot([issue(1)], {"1"}, set())
        self.assertIsNone(queue_report(self.config, view)["selected"])
        self.assertEqual(queue_report(self.config, view, exclude_claimed=False)["selected"]["id"], "1")

    def test_pr_refs_do_not_confuse_dependency_mentions(self):
        pull = {"headRefName": "fix/other", "body": "Refs #12, #27\nDepends on: #99"}
        self.assertEqual(linked_ids(pull, self.config.prefix), {"12", "27"})

    def test_github_command_pins_the_host_instead_of_inheriting_gh_host(self):
        with patch("devrelay.providers.command", return_value="[]") as called:
            gh(self.config, "issue", "list")
        self.assertEqual(called.call_args.args[0][-2:], ["--repo", "https://github.com/example/project"])

    def test_task_ids_cannot_escape_worktree_paths(self):
        for value in ("../outside", "--option", "a/b", "", "a\nline"):
            with self.assertRaises(RelayError):
                task_id(value)

    def test_a_queued_local_task_does_not_hide_an_existing_worktree(self):
        from devrelay.providers import add_local
        add_local(self.config, self.state, "manual", "Task", "Do bounded work", "priority:P1")
        path = self.root / ".devrelay/worktrees/manual"
        self.git("worktree", "add", "-b", "devrelay/task-manual", str(path), "main")
        report = queue_report(self.config, snapshot(self.config, self.state))
        self.assertIsNone(report["selected"])
        self.assertIn("Existing work", str(report["excluded"]))

    def test_github_dependency_ids_are_positive_integers(self):
        self.assertEqual(dependencies("Depends on: #12, #27", github=True), ["12", "27"])
        for value in ("Depends on: #0", "Depends on: #local", "Depends on: #12; #27"):
            with self.assertRaises(RelayError):
                dependencies(value, github=True)

    def test_github_integration_uses_actual_base_ancestry(self):
        self.config = self.configure(provider="github")
        self.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", base)
        (self.root / "value.txt").write_text("unmerged\n")
        self.git("commit", "-qam", "unmerged")
        unmerged = self.git("rev-parse", "HEAD")
        pulls = [{"state": "MERGED", "baseRefName": "main", "mergeCommit": {"oid": oid},
                  "closingIssuesReferences": [{"number": number}]} for number, oid in ((1, base), (2, unmerged))]
        from devrelay import providers
        real_git = providers.git
        def offline_git(root, *argv):
            return "" if argv[0] == "fetch" else real_git(root, *argv)
        with (patch.object(providers, "git", side_effect=offline_git),
              patch.object(providers, "validate_remote"),
              patch.object(providers, "gh_json", side_effect=[[], pulls])):
            view = snapshot(self.config, self.state)
        self.assertEqual(view.integrated, {"1"})


if __name__ == "__main__":
    unittest.main()
