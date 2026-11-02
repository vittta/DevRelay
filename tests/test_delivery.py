from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from devrelay.errors import RelayError
from devrelay.git import git as real_git, validate_remote as real_validate_remote
from devrelay.runner import prepare_task, run_task, submit, verify

from helpers import RepositoryTest


class GitHubDeliveryFixture(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure(provider="github", executable=self.fake_codex())
        self.remote = Path(self.temporary.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "origin", "main")
        self.issue = {"number": 12, "title": "Fix value", "body": "Fix the bounded behavior\nDepends on: none",
                      "state": "OPEN", "labels": [{"name": "auto:approved"}, {"name": "priority:P1"}],
                      "createdAt": "2026-10-09T00:00:00Z", "url": "https://github.com/example/project/issues/12"}
        self.created = False
        self.create_count = 0
        self.edit_count = 0
        self.timeout_after_create = False
        self.readback_head = None
        self.submission_queue = None
        self.submission_auto_merge = None
        self.last_body = ""
        self.create_argv = ()
        stack = ExitStack()
        self.addCleanup(stack.close)
        # Git transport is real and confined to the temporary bare repository.
        # Mock only the GitHub control plane and its production hostname check.
        stack.enter_context(patch("devrelay.providers.validate_remote"))
        stack.enter_context(patch("devrelay.runner.validate_remote", return_value=str(self.remote)))
        stack.enter_context(patch("devrelay.cleanup.validate_remote", return_value=str(self.remote)))
        stack.enter_context(patch("devrelay.providers.gh_json", side_effect=self.inventory))
        stack.enter_context(patch("devrelay.runner.gh_json", side_effect=self.pull_read))
        stack.enter_context(patch("devrelay.runner.gh", side_effect=self.pull_write))
        stack.enter_context(patch("devrelay.runner.command", side_effect=self.submission_gates))

    def inventory(self, config, *argv):
        if argv[0] == "issue":
            return [self.issue]
        return ([{"state": "OPEN", "headRefName": "devrelay/task-12", "body": "Refs #12"}]
                if self.created else [])

    def pull_read(self, config, *argv):
        if argv[1] == "list":
            return [{"number": 1, "url": "https://github.com/example/project/pull/1"}] if self.created else []
        task = self.state.get("12")
        return {"url": "https://github.com/example/project/pull/1", "state": "OPEN",
                "headRefName": task["branch"],
                "headRefOid": self.readback_head or self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]), "baseRefName": "main",
                "isCrossRepository": False, "autoMergeRequest": None}

    def submission_gates(self, argv, cwd):
        self.assertEqual(argv[:5], ["gh", "api", "--hostname", "github.com", "graphql"])
        self.assertIn("owner=example", argv)
        self.assertIn("name=project", argv)
        self.assertIn("mergeQueueEntry{id}", argv[argv.index("-f") + 1])
        number = next(argument.removeprefix("number=") for argument in argv if argument.startswith("number="))
        return json.dumps({"data": {"repository": {"pullRequest": {
            "url": "https://github.com/example/project/pull/" + number,
            "mergeQueueEntry": self.submission_queue, "autoMergeRequest": self.submission_auto_merge,
        }}}})

    def pull_write(self, config, *argv, input=None):
        self.last_body = input
        if argv[1] == "create":
            self.created = True
            self.create_count += 1
            self.create_argv = argv
            if self.timeout_after_create:
                raise RelayError("Request timed out after server created the PR")
            return "https://github.com/example/project/pull/1"
        self.edit_count += 1
        return ""

    def implement(self):
        with self.state.lock():
            return run_task(self.config, self.state, "12")

    def add_dependency_submodule(self):
        source = Path(self.temporary.name) / "delivery-dependency"
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


class GitHubDeliveryTests(GitHubDeliveryFixture):
    def test_closed_saved_pr_blocks_new_commit_and_push_and_can_recover_same_pr(self):
        self.implement()
        with self.state.lock():
            original = submit(self.config, self.state, "12")
        path = Path(original["worktree"])
        (path / "new.txt").write_text("new implementation\n")
        with self.state.lock():
            verify(self.config, self.state, "12")
        index = self.git("diff", "--cached", cwd=path)

        def closed(config, *argv):
            self.assertEqual(argv[1:3], ("view", original["pr_url"]))
            return {**self.pull_read(config, *argv), "state": "CLOSED"}

        with self.state.lock(), patch("devrelay.runner.gh_json", side_effect=closed):
            with self.assertRaisesRegex(RelayError, "closed/merged/pending"):
                submit(self.config, self.state, "12")
        saved = self.state.get("12")
        self.assertEqual(saved["pr_url"], original["pr_url"])
        self.assertEqual(saved["pr_observation"]["pull"]["state"], "CLOSED")
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=path), original["head_sha"])
        self.assertEqual(self.git("diff", "--cached", cwd=path), index)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", original["branch"]), original["head_sha"])
        self.assertEqual((path / "new.txt").read_text(), "new implementation\n")
        with self.state.lock():
            verify(self.config, self.state, "12")
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["pr_url"], original["pr_url"])
        self.assertEqual(self.create_count, 1)
        self.assertEqual(self.edit_count, 1)

    def test_saved_pr_identity_and_pending_merge_are_checked_before_publication(self):
        self.implement()
        with self.state.lock():
            original = submit(self.config, self.state, "12")
        path = Path(original["worktree"])
        (path / "new.txt").write_text("pending implementation\n")
        changes = [{"state": "MERGED"}, {"headRefName": "other"}, {"headRefOid": "0" * 40},
                   {"baseRefName": "other"}, {"isCrossRepository": True},
                   {"url": "https://github.com/other/repo/pull/1"}, {"autoMergeRequest": {"enabledAt": "now"}}]
        for change in changes:
            with self.subTest(change=change), self.state.lock():
                verify(self.config, self.state, "12")
                with patch("devrelay.runner.gh_json", side_effect=lambda c, *a: {**self.pull_read(c, *a), **change}):
                    with self.assertRaisesRegex(RelayError, "PR readback differs"):
                        submit(self.config, self.state, "12")
                self.assertEqual(self.state.get("12")["pr_url"], original["pr_url"])
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=path), original["head_sha"])
                self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", original["branch"]), original["head_sha"])
        self.assertEqual(self.create_count, 1)
        self.assertEqual(self.edit_count, 0)

    def test_saved_pr_from_another_repository_blocks_api_and_publication(self):
        self.implement()
        with self.state.lock():
            task = submit(self.config, self.state, "12")
            verify(self.config, self.state, "12")
            task = self.state.get("12")
            task["pr_url"] = "https://github.com/other/repo/pull/1"
            self.state.save(task)
            with patch("devrelay.runner.gh_json", side_effect=AssertionError("Foreign PR must not be queried")):
                with self.assertRaisesRegex(RelayError, "configured repository"):
                    submit(self.config, self.state, "12")
        self.assertEqual(self.create_count, 1)

    def test_pr_closed_during_commit_blocks_push_and_preserves_local_commit(self):
        self.implement()
        with self.state.lock():
            original = submit(self.config, self.state, "12")
        (Path(original["worktree"]) / "new.txt").write_text("new implementation\n")
        closed = False

        def commit_then_close(root, *argv, **kwargs):
            nonlocal closed
            result = real_git(root, *argv, **kwargs)
            if argv[0] == "commit":
                closed = True
            return result

        def read(config, *argv):
            pull = self.pull_read(config, *argv)
            return {**pull, "state": "CLOSED"} if closed else pull

        with self.state.lock():
            verify(self.config, self.state, "12")
            with patch("devrelay.runner.git", side_effect=commit_then_close), patch("devrelay.runner.gh_json", side_effect=read):
                with self.assertRaisesRegex(RelayError, "PR readback differs"):
                    submit(self.config, self.state, "12")
        saved = self.state.get("12")
        self.assertNotEqual(saved["head_sha"], original["head_sha"])
        self.assertEqual(saved["published_head"], original["head_sha"])
        self.assertEqual(saved["pr_url"], original["pr_url"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", saved["branch"]), original["head_sha"])

    def test_uncertain_push_recovers_using_saved_publication_heads(self):
        self.implement()
        with self.state.lock():
            original = submit(self.config, self.state, "12")
        (Path(original["worktree"]) / "new.txt").write_text("new implementation\n")

        def push_then_timeout(root, *argv, **kwargs):
            result = real_git(root, *argv, **kwargs)
            if argv[0] == "push":
                raise RelayError("Push timed out after remote accepted it")
            return result

        with self.state.lock():
            verify(self.config, self.state, "12")
            with patch("devrelay.runner.git", side_effect=push_then_timeout):
                with self.assertRaisesRegex(RelayError, "Push timed out"):
                    submit(self.config, self.state, "12")
            interrupted = self.state.get("12")
            self.assertEqual(interrupted["published_head"], original["head_sha"])
            self.assertEqual(interrupted["push_intent"]["head_sha"], interrupted["head_sha"])
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["pr_url"], original["pr_url"])
        self.assertEqual(recovered["head_sha"], interrupted["head_sha"])
        self.assertEqual(recovered["published_head"], recovered["head_sha"])
        self.assertNotIn("push_intent", recovered)
        self.assertEqual(self.create_count, 1)

    def test_closed_pr_after_uncertain_creation_is_discovered_without_replacement(self):
        self.implement()
        self.timeout_after_create = True
        with self.state.lock(), self.assertRaisesRegex(RelayError, "timed out"):
            submit(self.config, self.state, "12")

        def read(config, *argv):
            result = self.pull_read(config, *argv)
            if argv[1] == "list":
                self.assertEqual(argv[argv.index("--state") + 1], "all")
                return result
            return {**result, "state": "CLOSED"}

        with self.state.lock(), patch("devrelay.runner.gh_json", side_effect=read):
            with self.assertRaisesRegex(RelayError, "PR readback differs"):
                submit(self.config, self.state, "12")
        self.assertEqual(self.state.get("12")["pr_url"], "https://github.com/example/project/pull/1")
        self.assertEqual(self.create_count, 1)

    def test_external_merge_queue_blocks_publication_without_a_local_merge_request(self):
        self.implement()
        with self.state.lock():
            original = submit(self.config, self.state, "12")
            path = Path(original["worktree"])
            (path / "new.txt").write_text("queued PR changes\n")
            verify(self.config, self.state, "12")
            self.submission_queue = {"id": "external-queue-entry"}
            with self.assertRaisesRegex(RelayError, "closed/merged/pending"):
                submit(self.config, self.state, "12")
        saved = self.state.get("12")
        self.assertEqual(saved["pr_url"], original["pr_url"])
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["pr_observation"]["merge"]["data"]["repository"]["pullRequest"]["mergeQueueEntry"],
                         self.submission_queue)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=path), original["head_sha"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", original["branch"]), original["head_sha"])
        self.assertEqual((self.create_count, self.edit_count), (1, 0))

    def test_incomplete_graphql_merge_state_blocks_publication_and_retains_pr_identity(self):
        self.implement()
        with self.state.lock():
            original = submit(self.config, self.state, "12")
        valid = {"url": original["pr_url"], "mergeQueueEntry": None, "autoMergeRequest": None}
        invalid = ["invalid json", "[]", json.dumps({"errors": [{"message": "Unavailable"}]}),
                   json.dumps({"data": {"repository": {"pullRequest": None}}})]
        for change in ({"url": "https://github.com/other/repo/pull/1"},
                       {"mergeQueueEntry": {"id": ""}}, {"autoMergeRequest": {"enabledAt": 0}}):
            invalid.append(json.dumps({"data": {"repository": {"pullRequest": {**valid, **change}}}}))
        invalid.append(json.dumps({"data": {"repository": {"pullRequest": {"url": original["pr_url"]}}}}))
        for response in invalid:
            with self.subTest(response=response), self.state.lock():
                verify(self.config, self.state, "12")
                with patch("devrelay.runner.command", return_value=response):
                    with self.assertRaisesRegex(RelayError, "incomplete or invalid PR merge state"):
                        submit(self.config, self.state, "12")
                self.assertEqual(self.state.get("12")["pr_url"], original["pr_url"])
                self.assertEqual(self.state.get("12")["status"], "blocked")
                self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", original["branch"]), original["head_sha"])
        self.assertEqual((self.create_count, self.edit_count), (1, 0))

    def test_authorization_changed_during_pre_push_pr_readback_blocks_push(self):
        self.implement()
        with self.state.lock():
            original = submit(self.config, self.state, "12")
            path = Path(original["worktree"])
            (path / "new.txt").write_text("new implementation\n")
            verify(self.config, self.state, "12")
        reads = 0

        def withdraw_during_readback(argv, cwd):
            nonlocal reads
            result = self.submission_gates(argv, cwd)
            reads += 1
            if reads == 2:
                self.issue["labels"] = [{"name": "priority:P1"}]
            return result

        with self.state.lock(), patch("devrelay.runner.command", side_effect=withdraw_during_readback):
            with self.assertRaisesRegex(RelayError, "not eligible"):
                submit(self.config, self.state, "12")
        saved = self.state.get("12")
        self.assertNotEqual(saved["head_sha"], original["head_sha"])
        self.assertEqual(saved["pr_url"], original["pr_url"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", original["branch"]), original["head_sha"])
        self.assertEqual((self.create_count, self.edit_count), (1, 0))

    def test_requirements_changed_during_commit_block_push_and_can_be_revalidated(self):
        task = self.implement()
        worktree = Path(task["worktree"])
        def change_requirement(root, *argv, **kwargs):
            result = real_git(root, *argv, **kwargs)
            if argv[0] == "commit":
                self.issue["body"] += "\nAcceptance: add requirement.txt\n"
            return result
        with self.state.lock(), patch("devrelay.runner.git", side_effect=change_requirement):
            with self.assertRaisesRegex(RelayError, "missing or stale"):
                submit(self.config, self.state, "12")
        self.assertEqual(self.create_count, 0)
        self.assertEqual(self.git("--git-dir", str(self.remote), "for-each-ref", "refs/heads/devrelay/"), "")
        self.assertEqual(self.git("show", "HEAD:value.txt", cwd=worktree), "fixed")
        (worktree / "requirement.txt").write_text("New acceptance implemented\n")
        with self.state.lock():
            verify(self.config, self.state, "12")
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["status"], "pr_open")
        self.assertEqual(recovered["verification"]["task_body"], self.issue["body"])
        self.assertEqual(self.create_count, 1)

    def test_requirements_changed_during_push_block_pr_creation_and_preserve_remote(self):
        task = self.implement()
        def change_requirement(root, *argv, **kwargs):
            result = real_git(root, *argv, **kwargs)
            if argv[0] == "push":
                self.issue["body"] += "\nAcceptance changed during push\n"
            return result
        with self.state.lock(), patch("devrelay.runner.git", side_effect=change_requirement):
            with self.assertRaisesRegex(RelayError, "missing or stale"):
                submit(self.config, self.state, "12")
        saved = self.state.get("12")
        self.assertEqual(self.create_count, 0)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", saved["branch"]), saved["head_sha"])
        with self.state.lock():
            verify(self.config, self.state, "12")
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["head_sha"], saved["head_sha"])
        self.assertEqual(recovered["status"], "pr_open")
        self.assertTrue(Path(task["worktree"]).exists())

    def test_title_changed_during_pr_lookup_blocks_publication(self):
        self.implement()
        def change_title(config, *argv):
            result = self.pull_read(config, *argv)
            if argv[1] == "list":
                self.issue["title"] = "Updated task scope"
            return result
        with self.state.lock(), patch("devrelay.runner.gh_json", side_effect=change_title):
            with self.assertRaisesRegex(RelayError, "missing or stale"):
                submit(self.config, self.state, "12")
        self.assertEqual(self.create_count, 0)
        with self.state.lock():
            verify(self.config, self.state, "12")
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["verification"]["task_title"], self.issue["title"])
        self.assertEqual(recovered["status"], "pr_open")

    def test_drift_during_pr_creation_retains_pr_identity_for_recovery(self):
        self.implement()
        def change_requirement(config, *argv, input=None):
            result = self.pull_write(config, *argv, input=input)
            if argv[1] == "create":
                self.issue["body"] += "\nAcceptance changed during creation\n"
            return result
        with self.state.lock(), patch("devrelay.runner.gh", side_effect=change_requirement):
            with self.assertRaisesRegex(RelayError, "missing or stale"):
                submit(self.config, self.state, "12")
        saved = self.state.get("12")
        self.assertTrue(saved["pr_url"])
        with self.state.lock():
            with self.assertRaisesRegex(RelayError, "missing or stale"):
                submit(self.config, self.state, "12")
            verify(self.config, self.state, "12")
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(self.create_count, 1)
        self.assertEqual(self.edit_count, 1)
        self.assertEqual(recovered["pr_url"], saved["pr_url"])

    def test_preparation_after_api_timeout_reuses_the_existing_pr(self):
        self.implement()
        self.timeout_after_create = True
        with self.state.lock():
            with self.assertRaisesRegex(RelayError, "timed out"):
                submit(self.config, self.state, "12")
            saved = self.state.get("12")
            self.assertEqual(saved["status"], "committed")
            self.assertFalse(saved.get("pr_url"))
            prepared = prepare_task(self.config, self.state, "12")
            self.assertIsNone(prepared["verification"])
            verify(self.config, self.state, "12")
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(self.create_count, 1)
        self.assertEqual(self.edit_count, 1)
        self.assertEqual(recovered["head_sha"], saved["head_sha"])

    def test_verified_branch_is_pushed_and_draft_pr_is_read_back(self):
        self.implement()
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        pushed = subprocess.check_output(["git", "--git-dir", str(self.remote), "rev-parse", task["branch"]], text=True).strip()
        self.assertEqual(pushed, task["head_sha"])
        self.assertEqual(self.git("config", "--get", "branch." + task["branch"] + ".remote",
                                  cwd=task["worktree"]), "origin")
        self.assertEqual(self.git("config", "--get", "branch." + task["branch"] + ".merge",
                                  cwd=task["worktree"]), "refs/heads/" + task["branch"])
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/" + task["branch"]),
                         task["head_sha"])
        self.assertIn("--draft", self.create_argv)
        self.assertIn("Refs #12", self.last_body)
        self.assertIn(task["verification"]["source_tree"], self.last_body)

    def test_api_timeout_retry_reuses_the_commit_and_existing_pr(self):
        self.implement()
        self.timeout_after_create = True
        with self.state.lock(), self.assertRaisesRegex(RelayError, "timed out"):
            submit(self.config, self.state, "12")
        head = self.state.get("12")["head_sha"]
        with self.state.lock():
            recovered = submit(self.config, self.state, "12")
        self.assertEqual(recovered["head_sha"], head)
        self.assertEqual(self.create_count, 1)
        self.assertEqual(self.edit_count, 1)
        self.assertEqual(recovered["status"], "pr_open")

    def test_restricted_fetch_refspec_does_not_break_branch_tracking(self):
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.implement()
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        self.assertEqual(self.git("config", "--get", "branch." + task["branch"] + ".remote",
                                  cwd=task["worktree"]), "origin")
        self.assertEqual(self.git("config", "--get", "branch." + task["branch"] + ".merge",
                                  cwd=task["worktree"]), "refs/heads/" + task["branch"])
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                         task["head_sha"])

    def test_custom_fetch_refspec_updates_the_actual_upstream(self):
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.git("config", "--add", "remote.origin.fetch",
                 "+refs/heads/devrelay/*:refs/remotes/custom/*")
        self.implement()
        # An existing mapped ref must advance too, rather than leaving a stale upstream.
        self.git("update-ref", "refs/remotes/custom/task-12", self.git("rev-parse", "main"))
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        self.assertEqual(self.git("rev-parse", "@{upstream}", cwd=task["worktree"]), task["head_sha"])
        self.assertEqual(self.git("rev-parse", "--symbolic-full-name", "@{upstream}",
                                  cwd=task["worktree"]), "refs/remotes/custom/task-12")
        self.assertEqual(self.git("for-each-ref", "refs/remotes/origin/" + task["branch"]), "")

    def test_negative_fetch_refspec_does_not_suppress_pushed_upstream_update(self):
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.git("config", "--add", "remote.origin.fetch",
                 "+refs/heads/devrelay/*:refs/remotes/custom/*")
        self.git("config", "--add", "remote.origin.fetch", "^refs/heads/devrelay/*")
        self.implement()
        self.git("update-ref", "refs/remotes/custom/task-12", self.git("rev-parse", "main"))
        with self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertEqual(task["status"], "pr_open")
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                         task["head_sha"])
        self.assertEqual(self.git("rev-parse", "@{upstream}", cwd=task["worktree"]), task["head_sha"])
        self.assertEqual(self.git("rev-parse", "--symbolic-full-name", "@{upstream}",
                                  cwd=task["worktree"]), "refs/remotes/custom/task-12")

    def test_mismatched_pr_head_is_rejected(self):
        self.implement()
        self.readback_head = "0" * 40
        with self.state.lock(), self.assertRaisesRegex(RelayError, "readback differs"):
            submit(self.config, self.state, "12")
        self.assertNotEqual(self.state.get("12")["status"], "pr_open")

    def test_withdrawn_issue_authorization_prevents_push(self):
        self.implement()
        self.issue["labels"] = [{"name": "priority:P1"}]
        with self.state.lock(), self.assertRaisesRegex(RelayError, "not eligible"):
            submit(self.config, self.state, "12")
        self.assertEqual(self.create_count, 0)
        refs = subprocess.check_output(["git", "--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)"] , text=True)
        self.assertNotIn("devrelay/task-12", refs)

    def test_pre_push_submodule_mutation_blocks_pr_creation(self):
        self.add_dependency_submodule()
        task = self.implement()
        worktree = Path(task["worktree"])
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init",
                        "--recursive"], cwd=worktree, check=True)
        hooks = Path(self.temporary.name) / "push-hooks"
        hooks.mkdir()
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\nprintf 'push mutation\\n' > vendor/dependency/from-push.txt\n")
        hook.chmod(0o755)
        self.git("config", "core.hooksPath", str(hooks), cwd=worktree)
        with self.state.lock(), self.assertRaisesRegex(RelayError, "Submodule worktree"):
            submit(self.config, self.state, "12")
        saved = self.state.get("12")
        pushed = subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "rev-parse", saved["branch"]], text=True).strip()
        self.assertEqual(pushed, saved["head_sha"])
        self.assertEqual(self.create_count, 0)
        self.assertEqual((worktree / "vendor/dependency/from-push.txt").read_text(), "push mutation\n")

    def test_multiple_real_push_targets_are_rejected_before_either_receives_the_branch(self):
        self.implement()
        extra = Path(self.temporary.name) / "extra.git"
        subprocess.run(["git", "init", "--bare", "-q", str(extra)], check=True)
        self.git("remote", "set-url", "--add", "--push", "origin", str(self.remote))
        self.git("remote", "set-url", "--add", "--push", "origin", str(extra))
        with (patch("devrelay.runner.validate_remote", side_effect=real_validate_remote),
              self.state.lock(), self.assertRaisesRegex(RelayError, "exactly one")):
            submit(self.config, self.state, "12")
        branch = "refs/heads/devrelay/task-12"
        for remote in (self.remote, extra):
            refs = subprocess.check_output(
                ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"], text=True
            )
            self.assertNotIn(branch, refs)

    def test_worktree_conditional_rewrite_is_rejected_before_real_push(self):
        task = self.implement()
        extra = Path(self.temporary.name) / "conditional-extra.git"
        subprocess.run(["git", "init", "--bare", "-q", str(extra)], check=True)
        included = Path(self.temporary.name) / "task-transport.gitconfig"
        self.git("config", "--file", str(included), f"url.{extra}.insteadOf", str(self.remote))
        self.git("config", "includeIf.onbranch:devrelay/task-*.path", str(included))
        self.assertEqual(self.git("ls-remote", "--get-url", str(self.remote)), str(self.remote))
        self.assertEqual(self.git("ls-remote", "--get-url", str(self.remote), cwd=task["worktree"]),
                         str(extra))

        def validate_transport(config, root=None):
            return str(self.remote) if root is None else real_validate_remote(config, root)

        with (patch("devrelay.runner.validate_remote", side_effect=validate_transport),
              self.state.lock(), self.assertRaisesRegex(RelayError, "URL rewrite")):
            submit(self.config, self.state, "12")
        branch = "refs/heads/devrelay/task-12"
        for remote in (self.remote, extra):
            refs = subprocess.check_output(
                ["git", "--git-dir", str(remote), "for-each-ref", "--format=%(refname)"], text=True
            )
            self.assertNotIn(branch, refs)

    def test_rewrite_added_after_validation_cannot_redirect_real_push(self):
        self.implement()
        extra = Path(self.temporary.name) / "race-extra.git"
        subprocess.run(["git", "init", "--bare", "-q", str(extra)], check=True)
        changed = False

        def validate_then_rewrite(config, root=None):
            nonlocal changed
            if root is not None and not changed:
                changed = True
                self.git("config", f"url.{extra}.pushInsteadOf", str(self.remote))
            return str(self.remote)

        with patch("devrelay.runner.validate_remote", side_effect=validate_then_rewrite), self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertTrue(changed)
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                         task["head_sha"])
        refs = subprocess.check_output(
            ["git", "--git-dir", str(extra), "for-each-ref", "--format=%(refname)"], text=True
        )
        self.assertNotIn("refs/heads/" + task["branch"], refs)

    def test_url_named_remote_added_after_validation_cannot_fan_out_real_push(self):
        self.implement()
        extras = [Path(self.temporary.name) / f"submit-alias-extra-{index}.git" for index in range(2)]
        for extra in extras:
            subprocess.run(["git", "init", "--bare", "-q", str(extra)], check=True)
        url = self.remote.as_uri()
        changed = False

        def validate_then_alias(config, root=None):
            nonlocal changed
            if root is not None:
                changed = True
                self.git("config", f"remote.{url}.url", str(extras[0]))
                for extra in extras:
                    self.git("config", "--add", f"remote.{url}.pushurl", str(extra))
                return url
            return str(self.remote)

        with patch("devrelay.runner.validate_remote", side_effect=validate_then_alias), self.state.lock():
            task = submit(self.config, self.state, "12")
        self.assertTrue(changed)
        self.assertEqual(task["status"], "pr_open")
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", task["branch"]),
                         task["head_sha"])
        self.assertEqual(self.git("rev-parse", "@{upstream}", cwd=task["worktree"]), task["head_sha"])
        for extra in extras:
            self.assertEqual(self.git("--git-dir", str(extra), "for-each-ref"), "")


if __name__ == "__main__":
    unittest.main()
