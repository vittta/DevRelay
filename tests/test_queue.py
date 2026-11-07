import unittest
from unittest.mock import patch

from devrelay.errors import RelayError
from devrelay.providers import Snapshot, closing_ids, dependencies, gh, linked_ids, queue_report, snapshot, task_id

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

    def test_approval_is_required_and_conflicting_priorities_are_rejected(self):
        for labels in (["research", "priority:P1"],
                       ["auto:approved", "priority:P0", "priority:P1"],
                       ["auto:approved", "priority:P1", "blocked"],
                       ["auto:approved", "priority:P1", "needs-input"]):
            with self.subTest(labels=labels):
                self.assertIsNone(self.report([issue(1, labels=labels)])["selected"])

    def test_approved_plain_issue_uses_normal_priority_without_dependencies(self):
        plain = issue(1, labels=["auto:approved"], body="Deliver one observable fix")
        urgent = issue(2, labels=["auto:approved", "priority:P0"])
        later = issue(3, labels=["auto:approved", "priority:P2"])
        self.assertEqual([item["id"] for item in self.report([later, plain, urgent])["eligible"]], ["2", "1", "3"])
        self.assertEqual(dependencies(plain["body"]), [])

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
                              ([issue(1, body="Depends on: none\nDepends on: none")], "at most one"),
                              ([issue(1, body="Depends on: #2, #2")], "Duplicate")):
            with self.subTest(reason=reason):
                report = self.report(tasks)
                self.assertIsNone(report["selected"])
                self.assertIn(reason, str(report["excluded"]))

    def test_shared_dependency_graph_parses_each_task_once(self):
        tasks = [issue(number, opened=number == 60,
                       body="Depends on: none" if number < 3 else f"Depends on: #{number - 1}, #{number - 2}")
                 for number in range(1, 61)]
        with patch("devrelay.providers.dependencies", wraps=dependencies) as parsed:
            report = self.report(tasks, integrated={str(number) for number in range(1, 60)})
        self.assertEqual(report["selected"]["id"], "60")
        self.assertEqual(parsed.call_count, 60)

    def test_shared_dependency_failures_remain_explicit_and_deduplicated(self):
        tasks = [issue(number, opened=number == 40,
                       body="Depends on: #999" if number < 3 else f"Depends on: #{number - 1}, #{number - 2}")
                 for number in range(1, 41)]
        report = self.report(tasks, integrated={str(number) for number in range(1, 40)})
        self.assertIsNone(report["selected"])
        self.assertEqual(report["excluded"], [{"id": "40", "reasons": ["Unknown dependency #999"]}])

    def test_cached_dependencies_keep_path_depth_and_cycles(self):
        chain = [issue(number, opened=number == 66,
                       body="Depends on: none" if number == 1 else f"Depends on: #{number - 1}")
                 for number in range(1, 67)]
        report = self.report([issue(67, body="Depends on: #1"), *chain],
                             integrated={str(number) for number in range(1, 66)})
        self.assertEqual([task["id"] for task in report["eligible"]], ["67"])
        self.assertIn("Dependency chain exceeds the supported depth (64)", str(report["excluded"]))
        tasks = [issue(1, opened=False, body="Depends on: #2"),
                 issue(2, opened=False, body="Depends on: #1"),
                 issue(3, body="Depends on: #1"), issue(4, body="Depends on: #2")]
        report = self.report(tasks, integrated={"1", "2"})
        self.assertIsNone(report["selected"])
        self.assertEqual({task["id"] for task in report["excluded"]}, {"3", "4"})
        for excluded in report["excluded"]:
            self.assertTrue(any(reason.startswith("Dependency cycle:") for reason in excluded["reasons"]))

    def test_dependency_cache_does_not_outlive_its_snapshot_report(self):
        tasks = [issue(1, opened=False), issue(2, body="Depends on: #1")]
        self.assertEqual(self.report(tasks, integrated={"1"})["selected"]["id"], "2")
        tasks[0]["body"] = "Depends on: #999"
        report = self.report(tasks, integrated={"1"})
        self.assertIsNone(report["selected"])
        self.assertIn("Unknown dependency #999", str(report["excluded"]))

    def test_claimed_task_can_be_authorized_for_recovery(self):
        view = Snapshot([issue(1)], {"1"}, set())
        self.assertIsNone(queue_report(self.config, view)["selected"])
        self.assertEqual(queue_report(self.config, view, exclude_claimed=False)["selected"]["id"], "1")

    def test_pr_refs_do_not_confuse_dependency_mentions(self):
        pull = {"headRefName": "fix/other", "body": "Refs #12, #27\nDepends on: #99"}
        self.assertEqual(linked_ids(pull, self.config.prefix, self.config.repo), {"12", "27"})

    def test_closing_references_bind_the_repository_and_issue_number(self):
        references = [
            {"number": 12, "url": "https://github.com/other/repo/issues/12"},
            {"number": 13, "url": "https://github.com/EXAMPLE/Project/issues/13"},
            {"number": 14},
            {"number": 15, "url": "https://github.com/example/project/issues/16"},
            {"number": 17, "url": "https://github.example/example/project/issues/17"},
            {"number": 18, "url": "https://github.com/example/project/pull/18"},
        ]
        self.assertEqual(closing_ids({"closingIssuesReferences": references}, self.config.repo), {"13"})

    def test_open_pr_cross_repository_closing_reference_does_not_claim_local_issue(self):
        pull = {"headRefName": "fix/unrelated", "body": "Fixes other/repo#12\nRefs #13",
                "closingIssuesReferences": [
                    {"number": 12, "url": "https://github.com/other/repo/issues/12"},
                    {"number": 14, "url": "https://github.com/example/project/issues/14"},
                ]}
        self.assertEqual(linked_ids(pull, self.config.prefix, self.config.repo), {"13", "14"})
        pull["headRefName"] = self.config.prefix + "15"
        self.assertEqual(linked_ids(pull, self.config.prefix, self.config.repo), {"13", "14", "15"})

    def test_cross_repository_merge_does_not_unlock_a_local_dependency(self):
        self.config = self.configure(provider="github")
        self.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", base)
        issues = [{"number": identifier, "title": "Task", "body": body, "state": state,
                   "labels": [{"name": "auto:approved"}, {"name": "priority:P1"}],
                   "url": f"https://github.com/example/project/issues/{identifier}",
                   "createdAt": "2026-10-09T00:00:00Z"}
                  for identifier, body, state in ((12, "Depends on: none", "CLOSED"),
                                                   (13, "Depends on: #12", "OPEN"))]
        pull = {"state": "MERGED", "baseRefName": "main", "mergeCommit": {"oid": base},
                "closingIssuesReferences": [{"number": 12, "url": "https://github.com/other/repo/issues/12"}]}
        from devrelay import providers
        real_git = providers.git
        def offline_git(root, *argv):
            return "" if argv[0] == "fetch" else real_git(root, *argv)
        for repository, expected in (("other/repo", set()), ("example/project", {"12"})):
            with self.subTest(repository=repository):
                pull["closingIssuesReferences"][0]["url"] = f"https://github.com/{repository}/issues/12"
                with (patch.object(providers, "git", side_effect=offline_git),
                      patch.object(providers, "validate_remote"),
                      patch.object(providers, "gh_json", side_effect=[issues, [pull]])):
                    view = snapshot(self.config, self.state)
                self.assertEqual(view.integrated, expected)
                report = queue_report(self.config, view)
                if expected:
                    self.assertEqual(report["selected"]["id"], "13")
                else:
                    self.assertIsNone(report["selected"])
                    self.assertIn("Dependency #12 is not closed and integrated", str(report["excluded"]))

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
                  "closingIssuesReferences": [{"number": number,
                                                "url": f"https://github.com/example/project/issues/{number}"}]} for number, oid in ((1, base), (2, unmerged))]
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
