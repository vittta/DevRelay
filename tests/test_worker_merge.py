from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import unittest
from unittest.mock import patch

from devrelay.cli import main
from devrelay.config import load
from devrelay.delivery import merge, review
from devrelay.errors import RelayError
from devrelay.process import execute
from devrelay.runner import run_task
from devrelay.worker import selection

from test_worker import WorkerFixture


class AutoMergeWorkerFixture(WorkerFixture):
    def setUp(self):
        super().setUp()
        path = self.root / '.devrelay/config.toml'
        path.write_text(path.read_text() + '\n[delivery]\nauto_merge = true\nrequired_checks = ["test"]\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'automatic delivery')
        self.git('push', '-q', 'origin', 'main')
        self.config = load(self.root)
        executable = Path(self.config.data['codex']['executable'])
        executable.write_text(executable.read_text().replace("result = {'outcome':", "Path('task-' + Path.cwd().name + '.txt').write_text('delivered\\n')\nresult = {'outcome':"))
        self.remote_pulls = {}
        self.merge_count = self.ready_count = self.cancel_count = 0
        self.check_status = 'COMPLETED'
        self.check_conclusion = 'SUCCESS'
        self.queued = False
        self.request_error = False
        self.trace = Path(self.temporary.name) / 'trace.jsonl'
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {'DEVRELAY_TEST_TRACE': str(self.trace)}))
        stack.enter_context(patch('devrelay.delivery.validate_remote'))
        stack.enter_context(patch('devrelay.delivery.command', side_effect=self.query))
        stack.enter_context(patch('devrelay.delivery.gh', side_effect=self.mutate))

    def query(self, argv, cwd):
        if argv[argv.index('-f') + 1].startswith('query=mutation'):
            identifier = next(arg.removeprefix('id=PR-') for arg in argv if arg.startswith('id=PR-'))
            self.remote_pulls[identifier]['mergeQueueEntry'] = None
            self.remote_pulls[identifier]['autoMergeRequest'] = None
            self.cancel_count += 1
            mutation = 'dequeuePullRequest' if 'dequeuePullRequest' in ' '.join(argv) else 'disablePullRequestAutoMerge'
            return json.dumps({'data': {mutation: {'clientMutationId': None}}})
        identifier = next(arg.removeprefix('number=') for arg in argv if arg.startswith('number='))
        task = self.state.get(identifier)
        pull = self.remote_pulls.setdefault(identifier, {'id': 'PR-' + identifier, 'isDraft': True,
                 'state': 'OPEN', 'mergeCommit': None, 'mergeQueueEntry': None, 'autoMergeRequest': None})
        pull.update(url=task['pr_url'], headRefName=task['branch'], headRefOid=task['head_sha'],
                    baseRefName='main', baseRefOid=self.git('rev-parse', 'origin/main'),
                    isCrossRepository=False, mergeable='MERGEABLE', mergeStateStatus='CLEAN', reviewDecision=None,
                    reviewThreads={'nodes': [], 'pageInfo': {'hasNextPage': False}},
                    latestReviews={'nodes': [], 'pageInfo': {'hasNextPage': False}},
                    commits={'nodes': [{'commit': {'oid': task['head_sha'], 'statusCheckRollup': {'contexts': {
                        'nodes': [{'__typename': 'CheckRun', 'name': 'test', 'status': self.check_status,
                                   'conclusion': self.check_conclusion}], 'pageInfo': {'hasNextPage': False}}}}}]})
        return json.dumps({'data': {'repository': {'pullRequest': pull}}})

    def server_merge(self, identifier):
        task = self.state.get(identifier)
        server = Path(self.temporary.name) / ('server-' + identifier)
        self.git('fetch', 'origin', 'main')
        self.git('worktree', 'add', '--detach', str(server), 'origin/main')
        self.git('merge', '--squash', task['branch'], cwd=server)
        self.git('commit', '-qm', 'server squash ' + identifier, cwd=server)
        oid = self.git('rev-parse', 'HEAD', cwd=server)
        self.git('push', '-q', 'origin', 'HEAD:main', cwd=server)
        self.git('worktree', 'remove', str(server))
        self.remote_pulls[identifier].update(state='MERGED', mergeCommit={'oid': oid}, mergeQueueEntry=None,
                                             autoMergeRequest=None)

    def mutate(self, config, *argv, input=None):
        identifier = argv[2].rsplit('/', 1)[1]
        if argv[1] == 'ready':
            self.remote_pulls[identifier]['isDraft'] = False
            self.ready_count += 1
            return ''
        self.assertEqual(argv[:2], ('pr', 'merge'))
        self.assertNotIn('--admin', argv)
        self.assertNotIn('--auto', argv)
        self.assertEqual(argv[argv.index('--match-head-commit') + 1], self.state.get(identifier)['head_sha'])
        self.assertEqual(self.state.get(identifier)['merge_request']['state'], 'started')
        self.merge_count += 1
        if self.queued:
            self.remote_pulls[identifier]['mergeQueueEntry'] = {'id': 'queue-' + identifier}
        else:
            self.server_merge(identifier)
        if self.request_error:
            raise RelayError('Uncertain request timeout')
        return ''

    def trace_count(self):
        return len(self.trace.read_text().splitlines())

    def only_one(self):
        self.issues = [self.issue]


class AutoMergeWorkerTests(AutoMergeWorkerFixture):
    def test_two_issues_merge_and_pass_post_checks_before_next_claim(self):
        def post_check(state, argv, cwd, log, timeout, **kwargs):
            if '-post-' in str(cwd):
                if '13-post-' in str(cwd):
                    self.assertIsNone(self.state.get('12'))
                else:
                    self.assertEqual(self.state.get('13')['status'], 'completed')
            return execute(state, argv, cwd, log, timeout, **kwargs)
        with patch('devrelay.delivery.execute', side_effect=post_check):
            result = self.watching(max_tasks=2)
        self.assertEqual(result['delivered'], 2)
        self.assertEqual(self.events[0]['mode'], 'auto_merge')
        self.assertEqual([e['id'] for e in self.events if e['event'] == 'task_finished'], ['13', '12'])
        self.assertEqual((self.create_count, self.merge_count, self.ready_count, self.trace_count()), (2, 2, 2, 4))
        for task in self.state.tasks():
            self.assertEqual(task['status'], 'completed')
            self.assertTrue(task['post_merge']['passed'])
        self.assertEqual(self.state.get('12')['verification']['base_sha'], self.state.get('13')['merge_sha'])

    def test_pending_ci_polls_same_task_without_reimplementation_or_review(self):
        self.check_status = 'IN_PROGRESS'
        def finish(seconds):
            self.assertIsNone(self.state.get('12'))
            self.assertEqual(self.state.get('13')['status'], 'merge_waiting')
            self.assertEqual((self.create_count, self.merge_count, self.trace_count()), (1, 0, 2))
            self.check_status = 'COMPLETED'
        with patch('devrelay.worker.sleep', side_effect=finish) as sleeper:
            self.watching(max_tasks=1, poll_interval=3)
        sleeper.assert_called_once_with(3)
        self.assertEqual(self.trace_count(), 2)
        self.assertEqual(self.merge_count, 1)

    def test_queue_restart_reads_back_without_duplicate_mutations(self):
        self.queued = True
        with patch('devrelay.worker.sleep', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.watching()
        self.assertEqual(self.state.get('13')['status'], 'merge_waiting')
        self.assertEqual(self.cancel_count, 0)
        self.assertEqual(selection(self.config, self.state)['action'], 'merge')
        def finish(seconds):
            self.assertIsNone(self.state.get('12'))
            self.assertEqual(self.merge_count, 1)
            self.server_merge('13')
        with patch('devrelay.worker.sleep', side_effect=finish):
            self.watching(max_tasks=1)
        self.assertEqual((self.merge_count, self.ready_count, self.create_count, self.trace_count()), (1, 1, 1, 2))
        self.assertEqual(self.state.get('13')['status'], 'completed')

    def test_revocation_cancels_queued_merge_and_blocks_new_work(self):
        self.queued = True
        def revoke(seconds):
            self.issues[1]['labels'] = [{'name': 'priority:P0'}]
        with patch('devrelay.worker.sleep', side_effect=revoke), self.assertRaisesRegex(RelayError, 'not eligible'):
            self.watching()
        task = self.state.get('13')
        self.assertEqual(task['status'], 'merge_blocked')
        self.assertEqual(task['merge_cancellation']['status'], 'cancelled')
        self.assertEqual(self.cancel_count, 1)
        self.assertIsNone(self.state.get('12'))
        self.assertTrue(Path(task['worktree']).is_dir())

    def test_config_change_cancels_queued_merge(self):
        self.queued = True
        def change(seconds):
            path = self.root / '.devrelay/config.toml'
            path.write_text(path.read_text().replace('auto_merge = true', 'auto_merge = false'))
        with patch('devrelay.worker.sleep', side_effect=change), self.assertRaisesRegex(RelayError, 'configuration changed'):
            self.watching()
        self.assertEqual(self.state.get('13')['merge_cancellation']['status'], 'cancelled')
        self.assertIsNone(self.state.get('12'))

    def test_ci_failure_stops_with_attention_without_sleep_or_merge(self):
        self.check_conclusion = 'FAILURE'
        with patch('devrelay.worker.sleep') as sleeper:
            result = self.watching()
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(self.state.get('13')['status'], 'merge_blocked')
        sleeper.assert_not_called()
        self.assertEqual(self.merge_count, 0)
        self.assertIsNone(self.state.get('12'))

    def test_failed_ci_is_repaired_rechecked_and_reviewed_before_merge(self):
        self.only_one()
        self.check_conclusion = 'FAILURE'
        executable = Path(self.config.data['codex']['executable'])
        executable.write_text(executable.read_text().replace("if mode != 'blocked':",
            "if '\"kind\": \"ci\"' in prompt:\n    Path('ci-fixed.txt').write_text('CI regression fixed\\n')\nif mode != 'blocked':"))
        def query(argv, cwd):
            task = self.state.get('12')
            if task and (Path(task['worktree']) / 'ci-fixed.txt').exists():
                self.check_conclusion = 'SUCCESS'
            return self.query(argv, cwd)
        with patch('devrelay.delivery.command', side_effect=query):
            result = self.watching(max_tasks=1)
        self.assertEqual(result['delivered'], 1)
        task = self.state.get('12')
        self.assertEqual(task['workflow']['used'], 2)
        self.assertEqual(task['status'], 'completed')
        self.assertEqual((self.create_count, self.edit_count, self.merge_count), (1, 1, 1))
        self.assertEqual(self.trace_count(), 4)
        self.assertEqual(task['review']['head_sha'], task['head_sha'])

    def test_post_merge_failure_preserves_scene_and_stops_before_next_issue(self):
        def fail(state, argv, cwd, log, timeout, **kwargs):
            if '-post-' in str(cwd):
                raise RelayError('Post acceptance failed')
            return execute(state, argv, cwd, log, timeout, **kwargs)
        with patch('devrelay.delivery.execute', side_effect=fail), self.assertRaisesRegex(RelayError, 'Post acceptance'):
            self.watching()
        task = self.state.get('13')
        self.assertEqual(task['status'], 'post_merge_failed')
        self.assertTrue(task['merge_sha'])
        self.assertTrue(Path(task['post_merge_worktree']).is_dir())
        self.assertEqual(self.watching()['status'], 'needs_attention')
        self.assertIsNone(self.state.get('12'))

    def test_missing_queue_after_request_requires_explicit_retry(self):
        self.queued = True
        with patch('devrelay.worker.sleep', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.watching()
        self.remote_pulls['13']['mergeQueueEntry'] = None
        with self.assertRaisesRegex(RelayError, 'retry-request'):
            self.watching()
        self.assertEqual(self.merge_count, 1)
        self.queued = False
        with self.state.lock():
            result = merge(self.config, self.state, '13', retry_request=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.merge_count, 2)
        self.assertEqual(self.trace_count(), 2)
        self.assertEqual(len(result['merge_request_history']), 1)

    def test_sigterm_after_remote_merge_recovers_without_second_request(self):
        original = self.mutate
        def interrupted(config, *argv, **kwargs):
            result = original(config, *argv, **kwargs)
            if argv[1] == 'merge':
                os.kill(os.getpid(), signal.SIGTERM)
            return result
        with patch('devrelay.delivery.gh', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.watching()
        self.assertEqual(self.state.get('13')['status'], 'merging')
        self.watching(max_tasks=1)
        self.assertEqual(self.state.get('13')['status'], 'completed')
        self.assertEqual(self.merge_count, 1)
        self.assertEqual(self.trace_count(), 2)

    def test_completed_review_checkpoint_recovers_without_new_session(self):
        self.only_one()
        with patch('devrelay.delivery.advance', side_effect=lambda c, s, i: s.get(i)):
            task = run_task(self.config, self.state, '12', submit_after=True)
        with self.state.lock():
            task = review(self.config, self.state, '12')
        task.update(status='reviewing', review=None)
        self.state.save(task)  # Simulate crash after successful process receipt, before review state save.
        self.watching(max_tasks=1)
        self.assertEqual(self.trace_count(), 2)
        self.assertEqual(self.merge_count, 1)

    def test_incomplete_review_checkpoint_blocks_without_repeating_review(self):
        self.only_one()
        with patch('devrelay.delivery.advance', side_effect=lambda c, s, i: s.get(i)):
            task = run_task(self.config, self.state, '12', submit_after=True)
        task.update(status='reviewing', review_run=str(self.state.directory / 'runs/12/unfinished-review'))
        self.state.save(task)
        with self.assertRaisesRegex(RelayError, 'review'):
            self.watching()
        self.assertEqual(self.trace_count(), 1)
        self.assertEqual(self.state.get('12')['status'], 'review_blocked')

    def test_disabled_policy_on_restart_cancels_saved_remote_request(self):
        self.queued = True
        with patch('devrelay.worker.sleep', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.watching()
        path = self.root / '.devrelay/config.toml'
        path.write_text(path.read_text().replace('auto_merge = true', 'auto_merge = false'))
        self.config = load(self.root)
        with self.assertRaisesRegex(RelayError, 'disabled'):
            self.watching()
        self.assertEqual(self.state.get('13')['merge_cancellation']['status'], 'cancelled')
        self.assertEqual(self.cancel_count, 1)
        self.assertIsNone(self.state.get('12'))

    def test_cancellation_failure_retains_remote_pending_fact_and_scene(self):
        self.queued = True
        with patch('devrelay.worker.sleep', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.watching()
        self.issues[1]['labels'] = [{'name': 'priority:P0'}]
        original = self.query
        def unavailable(argv, cwd):
            if 'mutation(' in ' '.join(argv):
                raise RelayError('Cancellation unavailable')
            return original(argv, cwd)
        with patch('devrelay.delivery.command', side_effect=unavailable), self.assertRaisesRegex(RelayError, 'not eligible'):
            self.watching()
        task = self.state.get('13')
        self.assertEqual(task['status'], 'merge_blocked')
        self.assertEqual(task['merge_cancellation']['status'], 'failed')
        self.assertIn('Cancellation unavailable', task['error'])
        self.assertTrue(self.remote_pulls['13']['mergeQueueEntry'])
        self.assertTrue(Path(task['worktree']).is_dir())
        self.assertIsNone(self.state.get('12'))

    def test_closed_pr_is_blocked_and_never_recreated(self):
        self.check_status = 'IN_PROGRESS'
        with patch('devrelay.worker.sleep', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.watching()
        self.remote_pulls['13']['state'] = 'CLOSED'
        with self.assertRaisesRegex(RelayError, 'identity'):
            self.watching()
        self.assertEqual(self.state.get('13')['status'], 'merge_blocked')
        self.assertEqual((self.create_count, self.merge_count, self.trace_count()), (1, 0, 2))

    def test_cli_auto_mode_counts_completed_acceptance(self):
        self.only_one()
        with redirect_stdout(io.StringIO()) as out:
            result = main(['--project', str(self.root), 'watch', '--max-tasks', '1'])
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(result, 0)
        self.assertEqual(events[0]['mode'], 'auto_merge')
        self.assertEqual(events[-2]['status'], 'completed')
        self.assertEqual(events[-1]['delivered'], 1)


if __name__ == '__main__':
    unittest.main()
