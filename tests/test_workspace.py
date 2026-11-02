import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from helpers import RepositoryTest
from devrelay.cli import main
from devrelay.errors import RelayError
from devrelay.state import State
from devrelay import workspace as ws


class WorkspaceTest(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.second = (Path(self.temporary.name) / "second").resolve()
        subprocess.check_call(["git", "clone", "-q", str(self.root), str(self.second)])
        self.git("config", "user.name", "DevRelay Test", cwd=self.second)
        self.git("config", "user.email", "devrelay@example.invalid", cwd=self.second)
        self.manifest = Path(self.temporary.name) / "workspace.toml"
        self.requirements = Path(self.temporary.name) / "task.md"
        self.requirements.write_text("Fix both contract endpoints; preserve the existing controller work.\n")
        command = [sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'fixed\\n'"]
        joint = [sys.executable, "-c", "import os; from pathlib import Path; assert Path(os.environ['DEVRELAY_WORKTREE_BP'],'value.txt').read_text() == Path(os.environ['DEVRELAY_WORKTREE_PRO'],'value.txt').read_text()"]
        self.fake = self.fake_codex()
        with self.fake.open("a") as stream:
            # The existing fake executable exits only in review/error modes; implementation falls through.
            stream.write("\nif 'verdict' not in schema['properties'] and mode not in ('blocked',):\n    other = [args[i+1] for i,a in enumerate(args) if a == '--add-dir']\n    for path in other:\n        Path(path,'value.txt').write_text(Path('value.txt').read_text())\n")
        text = f'[codex]\nexecutable = {json.dumps(str(self.fake))}\ntimeout_seconds = 15\nmax_attempts = 2\n'
        for key, root in (("bp", self.root), ("pro", self.second)):
            text += f'\n[[repositories]]\nkey = "{key}"\npath = {json.dumps(str(root))}\n[repositories.project]\nprovider = "local"\nbase_branch = "main"\n[[repositories.checks]]\nid = "contract"\nargv = {json.dumps(command)}\ntimeout_seconds = 15\n'
        text += '\n[[checks]]\nid = "integration"\nargv = ' + json.dumps(joint) + '\ntimeout_seconds = 15\n'
        self.manifest.write_text(text)

    def invoke(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--workspace", str(self.manifest), *args])
        raw = output.getvalue() or errors.getvalue()
        return code, json.loads(raw)

    def add_task(self, identifier="fix"):
        code, task = self.invoke("task", "add", identifier, "--title", "Repair shared contract", "--file", str(self.requirements))
        self.assertEqual(code, 0, task)
        return task

    def started(self):
        self.add_task()
        code, task = self.invoke("start", "fix")
        self.assertEqual(code, 0, task)
        code, task = self.invoke("prepare", "fix")
        self.assertEqual(code, 0, task)
        return task

    def fixed(self):
        task = self.started()
        for member in task["members"].values():
            Path(member["worktree"], "value.txt").write_text("fixed\n")
        code, task = self.invoke("check", "fix")
        self.assertEqual(code, 0, task)
        return task

    def test_external_tools_joint_commit_preserve_controllers(self):
        (self.root / "unfinished.txt").write_text("Keep original work")
        task = self.fixed()
        self.assertEqual(len(task["verification"]["checks"]), 3)
        code, task = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 0, task)
        self.assertEqual(task["status"], "committed")
        for member in task["members"].values():
            self.assertEqual(self.git("show", "HEAD:value.txt", cwd=member["worktree"]), "fixed")
        self.assertEqual((self.root / "value.txt").read_text(), "broken\n")
        self.assertEqual((self.second / "value.txt").read_text(), "broken\n")
        self.assertEqual((self.root / "unfinished.txt").read_text(), "Keep original work")

    def test_auto_run_repairs_both_repositories_and_reviews(self):
        self.add_task()
        trace = Path(self.temporary.name) / "trace.jsonl"
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="retry", DEVRELAY_TEST_TRACE=str(trace)):
            code, task = self.invoke("run", "fix")
        self.assertEqual(code, 0, task)
        self.assertEqual(task["attempts"], 2)
        self.assertEqual(task["status"], "reviewed")
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(events), 3)
        self.assertIn("resume", events[1]["argv"])
        self.assertIn("--add-dir", events[0]["argv"])
        self.assertIn("Review the complete task-relative working source", events[-1]["prompt"])
        self.assertIn("Workspace context", events[0]["prompt"])
        self.assertNotEqual(task["session_id"], task["review"]["session_id"])

    def test_second_member_change_invalidates_all_delivery(self):
        task = self.fixed()
        Path(task["members"]["pro"]["worktree"], "value.txt").write_text("different\n")
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("changed", report["error"])
        for member in task["members"].values():
            self.assertEqual(self.git("show", "HEAD:value.txt", cwd=member["worktree"]), "broken")

    def test_other_base_movement_invalidates_receipt(self):
        task = self.fixed()
        (self.second / "new.txt").write_text("base advanced")
        self.git("add", "new.txt", cwd=self.second)
        self.git("commit", "-qm", "base advanced", cwd=self.second)
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("base advanced", report["error"])

    def test_check_mutation_in_another_member_is_rejected(self):
        original = self.manifest.read_text()
        command = [sys.executable, "-c", "import os; from pathlib import Path; Path(os.environ['DEVRELAY_WORKTREE_PRO'],'changed.txt').write_text('mutation')"]
        self.manifest.write_text(original + '\n[[checks]]\nid = "mutation"\nargv = ' + json.dumps(command) + '\n')
        task = self.started()
        for member in task["members"].values():
            Path(member["worktree"], "value.txt").write_text("fixed\n")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        self.assertIn("changed during", report["error"])

    def test_ownership_blocks_single_repository_and_second_bundle(self):
        task = self.started()
        member_state = State(self.root)
        self.addCleanup(member_state.close)
        with self.assertRaisesRegex(RelayError, "reserved"):
            with member_state.lock():
                pass
        self.add_task("other")
        code, report = self.invoke("start", "other")
        self.assertEqual(code, 1)
        self.assertIn("reserved", report["error"])
        code, task = self.invoke("task", "revoke", "fix")
        self.assertEqual(code, 0, task)
        with member_state.lock():
            pass
        self.assertTrue(Path(task["members"]["bp"]["worktree"]).exists())
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        self.assertIn("revoked", report["error"])

    def test_partial_start_recovers_without_recreating_first_member(self):
        self.add_task()
        real = ws.git
        def failure(root, *args, **kwargs):
            if root == self.second and args[:2] == ("worktree", "add"):
                raise RelayError("Second checkout failed")
            return real(root, *args, **kwargs)
        with patch("devrelay.workspace.git", side_effect=failure):
            code, report = self.invoke("start", "fix")
        self.assertEqual(code, 1)
        _, report = self.invoke("status")
        first = Path(report["tasks"][0]["members"]["bp"]["worktree"])
        (first / "keep.txt").write_text("keep")
        code, task = self.invoke("start", "fix")
        self.assertEqual(code, 0, task)
        self.assertEqual((first / "keep.txt").read_text(), "keep")

    def test_missing_setup_and_missing_joint_check_are_rejected(self):
        self.add_task()
        self.invoke("start", "fix")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        self.assertIn("preparation", report["error"])
        self.manifest.write_text(self.manifest.read_text().split('\n[[checks]]')[0])
        code, report = self.invoke("doctor")
        self.assertEqual(code, 1)
        self.assertIn("integration check", report["error"])

    def test_changed_requirements_and_configuration_block(self):
        self.fixed()
        self.requirements.write_text("Different authorization")
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("requirements changed", report["error"])
        self.requirements.write_text("Fix both contract endpoints; preserve the existing controller work.\n")
        self.manifest.write_text(self.manifest.read_text().replace("max_attempts = 2", "max_attempts = 3"))
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        self.assertIn("configuration changed", report["error"])

    def test_tracking_parent_duplicate_repo_and_auto_merge_are_rejected(self):
        self.requirements.write_text("Task kind: plan\nImplement many unrelated tasks")
        code, report = self.invoke("task", "add", "plan", "--title", "Plan", "--file", str(self.requirements))
        self.assertEqual(code, 1)
        self.assertIn("Tracking", report["error"])
        original = self.manifest.read_text()
        self.manifest.write_text(original.replace(str(self.second), str(self.root)))
        code, report = self.invoke("doctor")
        self.assertEqual(code, 1)
        self.assertIn("Duplicate", report["error"])
        self.manifest.write_text(original + '\n[delivery]\nauto_merge = true\n')
        code, report = self.invoke("doctor")
        self.assertEqual(code, 1)
        self.assertIn("automatic merge", report["error"])

    def test_blocked_model_and_rejected_review_prevent_submission(self):
        self.add_task()
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="blocked"):
            code, task = self.invoke("run", "fix")
        self.assertEqual(code, 1)
        self.assertEqual(task["status"], "blocked")
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="pass", DEVRELAY_TEST_REVIEW="changes_requested"):
            code, report = self.invoke("resume", "fix")
        self.assertEqual(code, 1)
        self.assertIn("review requested changes", report["error"])
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("review is rejected", report["error"])

    def test_review_source_mutation_invalidates_delivery(self):
        self.fixed()
        with patch.dict(os.environ, DEVRELAY_TEST_REVIEW="modify"):
            code, report = self.invoke("review", "fix")
        self.assertEqual(code, 1)
        self.assertIn("changed", report["error"])

    def test_successful_commit_interruption_recovers_exact_source(self):
        task = self.fixed()
        original = ws.git
        interrupted = []
        def commit_then_interrupt(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[0] == "commit" and not interrupted:
                interrupted.append(root)
                raise RelayError("Commit completed but receipt interrupted")
            return result
        with patch("devrelay.workspace.git", side_effect=commit_then_interrupt):
            code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("receipt interrupted", report["error"])
        head = self.git("rev-parse", "HEAD", cwd=task["members"]["bp"]["worktree"])
        code, task = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 0, task)
        self.assertEqual(task["members"]["bp"]["head_sha"], head)
        self.assertEqual(self.git("rev-list", "--count", "main..HEAD", cwd=task["members"]["bp"]["worktree"]), "1")

    def test_prepare_rejects_a_member_pr_that_was_merged_externally(self):
        task = self.fixed()
        space = ws.load(self.manifest)
        state = State(space.path.parent, directory=space.directory)
        self.addCleanup(state.close)
        task["members"]["bp"]["pr_url"] = "https://github.com/example/project/pull/1"
        with patch("devrelay.workspace.validate_pull", side_effect=RelayError("PR already merged")):
            with self.assertRaisesRegex(RelayError, "already merged"):
                ws.prepare(space, state, task)
        self.assertTrue(state.get("fix")["verification"]["passed"])

    def test_next_honors_priorities_and_stops_on_failed_work(self):
        self.add_task("later")
        code, task = self.invoke("task", "add", "urgent", "--title", "Urgent repair", "--file", str(self.requirements), "--priority", "priority:P0")
        self.assertEqual(code, 0, task)
        code, report = self.invoke("next")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["selected"]["id"], "urgent")
        self.invoke("start", "urgent")
        self.invoke("check", "urgent")  # Preparation is missing; fail without selecting another issue.
        code, report = self.invoke("next")
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "needs_attention")
        self.assertEqual(report["tasks"], ["urgent"])

    def test_base_advance_stops_without_repeated_implementation(self):
        self.add_task()
        calls = []
        def implement(config, state, task, directory, *args, **kwargs):
            calls.append(task["id"])
            for member in task["members"].values():
                Path(member["worktree"], "value.txt").write_text("fixed\n")
            (self.second / "latest.txt").write_text("new base")
            self.git("add", "latest.txt", cwd=self.second)
            self.git("commit", "-qm", "new base", cwd=self.second)
            return {"outcome": "ready", "summary": "Implemented", "blockers": []}
        with patch("devrelay.workspace.codex.implement", side_effect=implement):
            code, report = self.invoke("run", "fix")
        self.assertEqual(code, 1)
        self.assertIn("base advanced", report["error"])
        self.assertEqual(calls, ["fix"])

    def test_explicit_resume_retains_previous_blocker_feedback(self):
        self.add_task()
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="blocked"):
            self.invoke("run", "fix")
        trace = Path(self.temporary.name) / "resume-trace.jsonl"
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="pass", DEVRELAY_TEST_TRACE=str(trace)):
            code, task = self.invoke("resume", "fix")
        self.assertEqual(code, 0, task)
        first = json.loads(trace.read_text().splitlines()[0])
        self.assertIn("Provide dataset", first["prompt"])

    def test_rejected_review_cannot_be_bypassed_by_new_checks(self):
        self.fixed()
        with patch.dict(os.environ, DEVRELAY_TEST_REVIEW="changes_requested"):
            code, reviewed = self.invoke("review", "fix")
            self.assertEqual(code, 1, reviewed)
            self.assertEqual(reviewed["status"], "review_blocked")
            self.assertEqual(reviewed['review']['verdict'], 'changes_requested')
        code, checked = self.invoke("check", "fix")
        self.assertEqual(code, 0, checked)
        self.assertTrue(checked["review_required"])
        self.assertIsNone(checked["review"])
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("review", report["error"])
        for key, member in checked["members"].items():
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=member["worktree"]),
                             checked["verification"]["members"][key]["head_sha"])
        self.assertEqual(self.invoke("review", "fix")[0], 0)
        self.assertEqual(self.invoke("submit", "fix", "--local")[0], 0)

    def test_failed_snapshot_discards_previous_acceptance(self):
        self.fixed()
        with patch("devrelay.workspace.snapshot", side_effect=RelayError("Snapshot unavailable")):
            self.assertEqual(self.invoke("check", "fix")[0], 1)
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("verification is missing", report["error"])

    def test_joint_artifacts_are_retained_and_tampering_blocks_delivery(self):
        command = [sys.executable, "-c", "import os; from pathlib import Path; p=Path(os.environ['DEVRELAY_EVIDENCE_DIR']); p.mkdir(parents=True); (p/'native.zip').write_bytes(b'compiled')"]
        self.manifest.write_text(self.manifest.read_text() + '\n[[checks]]\nid = "artifact"\nargv = ' + json.dumps(command) + '\n')
        task = self.fixed()
        first = Path(task["verification_path"])
        artifact = first.parent / "artifacts/native.zip"
        self.assertIn("artifacts/native.zip", task["verification"]["files"])
        artifact.write_bytes(b'replaced')
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1)
        self.assertIn("artifacts changed", report["error"])
        code, task = self.invoke("check", "fix")
        self.assertEqual(code, 0, task)
        self.assertNotEqual(first, Path(task["verification_path"]))
        self.assertEqual(artifact.read_bytes(), b'replaced', 'Old evidence must not be overwritten')
        self.assertEqual(self.invoke("submit", "fix", "--local")[0], 0)

    def test_revoke_uses_registered_roots_when_manifest_is_invalid(self):
        task = self.started()
        self.manifest.write_text('not a valid manifest [')
        self.assertEqual(self.invoke("status")[0], 0)
        code, task = self.invoke("task", "revoke", "fix")
        self.assertEqual(code, 0, task)
        for root in (self.root, self.second):
            member_state = State(root)
            self.addCleanup(member_state.close)
            with member_state.lock():
                pass
        self.assertTrue(Path(task["members"]["pro"]["worktree"]).exists())

    def test_unfinished_single_repo_reviews_block_workspace_claim(self):
        self.add_task()
        state = State(self.root)
        self.addCleanup(state.close)
        state.save({"id": "single", "status": "review_blocked", "worktree": str(self.root)})
        code, report = self.invoke("start", "fix")
        self.assertEqual(code, 1)
        self.assertIn("unfinished", report["error"])

    def test_workspace_is_a_reserved_repository_key(self):
        self.manifest.write_text(self.manifest.read_text().replace('key = "bp"', 'key = "workspace"'))
        self.assertEqual(self.invoke("doctor")[0], 1)

    def test_member_and_joint_checks_with_same_id_retain_distinct_logs(self):
        self.manifest.write_text(self.manifest.read_text().replace('id = "integration"',
            'id = "contract"\ncwd = "bp"'))
        task = self.fixed()
        receipts = task['verification']['checks']
        self.assertEqual(len({r['log'] for r in receipts}), 3)
        self.assertTrue(all(Path(r['log']).is_file() for r in receipts))

    def test_branch_creation_interruption_recovers_only_owned_base(self):
        self.add_task()
        real = ws.git
        def interrupt(root, *args, **kwargs):
            if root == self.second and args[:2] == ('worktree', 'add'):
                self.git('branch', args[3], args[5], cwd=root)
                raise RelayError('Checkout interrupted after branch creation')
            return real(root, *args, **kwargs)
        with patch('devrelay.workspace.git', side_effect=interrupt):
            self.assertEqual(self.invoke('start', 'fix')[0], 1)
        code, task = self.invoke('start', 'fix')
        self.assertEqual(code, 0, task)
        self.assertTrue(Path(task['members']['pro']['worktree']).is_dir())

    @contextlib.contextmanager
    def remote_delivery(self):
        """Real commits, refs, leases and pushes to bare Git; only the GitHub API is simulated."""
        task = self.fixed()
        space = ws.load(self.manifest)
        base = self.git("rev-parse", "HEAD")
        remotes = {}
        for key, config in space.members.items():
            remote = Path(self.temporary.name) / (key + '.git')
            subprocess.check_call(['git', 'init', '--bare', '-q', str(remote)])
            self.git('push', str(remote), 'main', cwd=config.root)
            remotes[config.root] = remote
            config.data["project"].update(provider="github", repository="example/" + key)
        state = State(space.path.parent, directory=space.directory)
        self.addCleanup(state.close)
        service = {"pulls": {}, "calls": [], "fail_create": None, "uncertain_create": False}
        def branch_head(config, branch):
            result = subprocess.run(['git', 'rev-parse', '--verify', 'refs/heads/' + branch],
                                    cwd=remotes[config.root], text=True, capture_output=True)
            return result.stdout.strip() if result.returncode == 0 else None
        def gh(config, *args, **kwargs):
            service['calls'].append((config.repo, args))
            if args[:2] == ('pr', 'create'):
                fail = service['fail_create'] == config.repo
                if fail and not service['uncertain_create']:
                    raise RelayError('API publication failed')
                branch = args[args.index('--head') + 1]
                url = 'https://github.com/' + config.repo + '/pull/1'
                service['pulls'][config.repo] = {'url': url, 'state': 'OPEN', 'isDraft': True,
                    'isCrossRepository': False, 'baseRefName': 'main', 'headRefName': branch}
                if fail:
                    raise RelayError('API publication outcome uncertain')
                return url
            return ''
        def gh_json(config, *args):
            pull = service['pulls'].get(config.repo)
            value = {**pull, 'headRefOid': branch_head(config, pull['headRefName'])} if pull else None
            return ([value] if value else []) if args[:2] == ('pr', 'list') else value
        @contextlib.contextmanager
        def transport(root, url):
            yield url, os.environ
        def endpoint(config, root=None):
            return str(remotes[config.root])
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('devrelay.workspace.assert_current'))
            stack.enter_context(patch('devrelay.workspace.base_commit', return_value=base))
            stack.enter_context(patch('devrelay.workspace.validate_remote', side_effect=endpoint))
            stack.enter_context(patch('devrelay.workspace.pinned_transport', side_effect=transport))
            stack.enter_context(patch('devrelay.workspace.gh', side_effect=gh))
            stack.enter_context(patch('devrelay.workspace.gh_json', side_effect=gh_json))
            yield space, state, task, service, remotes

    def test_remote_partial_delivery_rechecks_edits_and_reuses_existing_draft(self):
        with self.remote_delivery() as (space, state, task, service, remotes):
            service['fail_create'] = 'example/pro'
            with ws.member_locks(space, task) as states:
                with self.assertRaisesRegex(RelayError, 'API publication failed'):
                    ws.submit(space, state, task, states)
                old = task['members']['bp']['published_head']
                Path(task['members']['bp']['worktree'], 'extra.txt').write_text('Reviewed repair')
                ws.check(space, state, task)
                service['fail_create'] = None
                result = ws.submit(space, state, task, states)
        self.assertEqual(result['status'], 'delivered')
        self.assertNotEqual(old, result['members']['bp']['published_head'])
        self.assertEqual(sum(1 for repo, args in service['calls'] if repo == 'example/bp' and args[:2] == ('pr', 'create')), 1)
        self.assertFalse(any(args[:2] == ('pr', 'merge') for _, args in service['calls']))

    def test_confirmed_publication_rewind_blocks_before_any_mutation(self):
        with self.remote_delivery() as (space, state, task, service, remotes):
            service['fail_create'] = 'example/pro'
            with ws.member_locks(space, task) as states:
                with self.assertRaisesRegex(RelayError, 'API publication failed'):
                    ws.submit(space, state, task, states)
                earlier = task['members']['bp']['published_head']
                Path(task['members']['bp']['worktree'], 'repair.txt').write_text('verified repair')
                ws.check(space, state, task)
                with self.assertRaisesRegex(RelayError, 'API publication failed'):
                    ws.submit(space, state, task, states)
                self.assertNotEqual(earlier, task['members']['bp']['published_head'])
                self.git('update-ref', 'refs/heads/' + task['members']['bp']['branch'], earlier,
                         cwd=remotes[self.root])
                with patch('devrelay.workspace.git', wraps=ws.git) as calls:
                    with self.assertRaisesRegex(RelayError, 'identity changed'):
                        ws.submit(space, state, task, states)
                self.assertFalse(any(call.args[1] in ('push', 'commit') for call in calls.call_args_list))

    def test_uncertain_create_closed_draft_stops_before_any_push(self):
        with self.remote_delivery() as (space, state, task, service, remotes):
            service.update(fail_create='example/bp', uncertain_create=True)
            with ws.member_locks(space, task) as states:
                with self.assertRaisesRegex(RelayError, 'uncertain'):
                    ws.submit(space, state, task, states)
                self.assertNotIn('pr_url', task['members']['bp'])
                service['pulls']['example/bp']['state'] = 'CLOSED'
                Path(task['members']['bp']['worktree'], 'repair.txt').write_text('new repair')
                ws.check(space, state, task)
                with patch('devrelay.workspace.git', wraps=ws.git) as calls:
                    with self.assertRaisesRegex(RelayError, 'identity changed'):
                        ws.submit(space, state, task, states)
                self.assertFalse(any(call.args[1] in ('push', 'commit') for call in calls.call_args_list))

    def test_uncertain_push_is_recovered_without_duplicate_commit_or_pr(self):
        with self.remote_delivery() as (space, state, task, service, remotes):
            real = ws.git
            interrupted = []
            def after_push(root, *args, **kwargs):
                result = real(root, *args, **kwargs)
                if args[0] == 'push' and not interrupted:
                    interrupted.append(root)
                    raise RelayError('Lost push receipt')
                return result
            with ws.member_locks(space, task) as states:
                with patch('devrelay.workspace.git', side_effect=after_push):
                    with self.assertRaisesRegex(RelayError, 'Lost push'):
                        ws.submit(space, state, task, states)
                result = ws.submit(space, state, state.get('fix'), states)
                self.assertEqual(result['status'], 'delivered')
                for member in result['members'].values():
                    self.assertEqual(self.git('rev-list', '--count', 'main..HEAD', cwd=member['worktree']), '1')

    def test_push_lease_rejects_remote_race(self):
        with self.remote_delivery() as (space, state, task, service, remotes):
            real = ws.git
            raced = []
            def race(root, *args, **kwargs):
                if args[0] == 'push' and not raced:
                    raced.append(root)
                    config = space.members['bp']
                    self.git('branch', task['members']['bp']['branch'], 'main', cwd=remotes[config.root])
                return real(root, *args, **kwargs)
            with ws.member_locks(space, task) as states, patch('devrelay.workspace.git', side_effect=race):
                with self.assertRaises(RelayError):
                    ws.submit(space, state, task, states)
            self.assertEqual(service['pulls'], {})

    def test_interrupted_release_is_idempotent_and_keeps_new_owner(self):
        with self.remote_delivery() as (space, state, task, service, remotes):
            real = State.set_metadata
            released = []
            def fail_release(member_state, key, value):
                if key == 'workspace_owner' and value is None:
                    if released:
                        raise OSError('Release interrupted')
                    released.append(member_state.directory)
                return real(member_state, key, value)
            with ws.member_locks(space, task) as states:
                with patch.object(State, 'set_metadata', new=fail_release):
                    with self.assertRaisesRegex(OSError, 'Release interrupted'):
                        ws.submit(space, state, task, states)
            saved = state.get('fix')
            self.assertEqual(saved['status'], 'delivered')
            first = State(self.root)
            self.addCleanup(first.close)
            first.set_metadata('workspace_owner', 'new-bundle')
            # Cleanup does not parse the changed manifest or reclaim the now foreign reservation.
            self.manifest.write_text('invalid [')
            code, result = self.invoke('submit', 'fix')
            self.assertEqual(code, 0, result)
            self.assertTrue(result['reservations_released'])
            self.assertEqual(first.metadata('workspace_owner'), 'new-bundle')
            self.assertEqual(self.invoke('check', 'fix')[0], 1)
