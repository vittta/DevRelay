from contextlib import redirect_stdout, redirect_stderr
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import threading
import sys
import unittest
from unittest.mock import patch

from devrelay.cli import main, doctor
from devrelay.config import load
from devrelay.errors import RelayError
from devrelay.git import common_dir
from devrelay.project_registry import Registry, default_catalog, repository_state
from devrelay.state import State
from devrelay.ui import DashboardServer, projects

from helpers import RepositoryTest


class RegistrationTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.catalog = Path(self.temporary.name).resolve() / 'user config/projects.toml'
        self.registry = Registry(self.catalog)

    def add(self, root=None, **kwargs):
        return self.registry.add(str(root or self.root), **kwargs)

    def test_probe_is_read_only_and_registration_matches_reviewed_configuration(self):
        (self.root / 'package.json').write_text(json.dumps({'scripts': {'lint': 'touch must-not-run'}}))
        before = self.git('status', '--porcelain')
        index = self.git('write-tree')
        proposal = self.registry.plan(str(self.root))
        self.assertFalse(self.catalog.exists())
        self.assertFalse((self.root / '.devrelay').exists())
        self.assertFalse((common_dir(self.root) / 'devrelay').exists())
        self.assertEqual(self.git('status', '--porcelain'), before)
        result = self.add(revision=proposal['revision'])
        self.assertEqual((self.root / '.devrelay/config.toml').read_text(), proposal['source'])
        self.assertFalse((self.root / 'must-not-run').exists())
        self.assertEqual(self.git('write-tree'), index)
        metadata, tasks = repository_state(self.root)
        self.assertEqual(metadata['controller_root'], str(self.root))
        self.assertEqual(tasks, [])
        self.assertFalse(result['needs_acceptance'])
        self.assertEqual(load(self.root).data['queue']['mode'], 'manual')
        self.assertFalse(load(self.root).data['delivery']['auto_merge'])

    def test_existing_config_index_dirty_files_and_task_evidence_are_preserved(self):
        self.configure()
        self.state.set_metadata('controller_root', str(self.root))
        task = self.state.save({'id': '1', 'status': 'verified', 'verification': {'source_tree': 'old'}})
        config = self.root / '.devrelay/config.toml'
        config.write_text(config.read_text() + '\n# personal policy\n')
        (self.root / 'value.txt').write_text('staged edit\n')
        self.git('add', 'value.txt')
        (self.root / 'value.txt').write_text('additional edit\n')
        index = self.git('write-tree')
        original = config.read_bytes()
        result = self.add()
        first_catalog = self.catalog.read_bytes()
        duplicate = self.add()
        self.assertTrue(duplicate['already_added'])
        self.assertEqual(result['id'], duplicate['id'])
        self.assertEqual(self.catalog.read_bytes(), first_catalog)
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(self.git('write-tree'), index)
        self.assertEqual((self.root / 'value.txt').read_text(), 'additional edit\n')
        self.assertEqual(self.state.get('1'), task)

    def test_other_worktree_deduplicates_and_keeps_the_registered_controller(self):
        self.configure()
        result = self.add()
        other = Path(self.temporary.name) / 'other 工作树 $(no-shell)'
        self.git('worktree', 'add', '-b', 'other', str(other))
        other_config = other / '.devrelay/config.toml'
        other_config.write_text(other_config.read_text() + '\n# unrelated work\n')
        duplicate = self.add(other)
        self.assertEqual(duplicate['id'], result['id'])
        self.assertEqual(duplicate['root'], str(self.root))
        self.assertEqual(len(self.registry.read()[1]), 1)
        self.assertIn('unrelated work', other_config.read_text())

    def test_move_checkout_and_relocate_preserves_identity_and_configuration(self):
        result = self.add()
        source = (self.root / '.devrelay/config.toml').read_bytes()
        destination = self.root.with_name('moved project')
        self.root.rename(destination)
        self.assertEqual(len(projects(None, self.catalog)), 1)  # broken entries remain repairable
        proposal = self.registry.plan(str(destination), replace_id=result['id'])
        self.assertTrue(proposal['relocating'])
        moved = self.add(destination, replace_id=result['id'], revision=proposal['revision'])
        self.assertEqual(moved['id'], result['id'])
        self.assertEqual(self.registry.read()[1][0]['path'], str(destination))
        self.assertEqual(repository_state(destination)[0]['controller_root'], str(destination))
        self.assertEqual((destination / '.devrelay/config.toml').read_bytes(), source)
        self.assertTrue(list((self.catalog.parent / 'catalog-backups').glob('*.toml')))

    def test_wrong_repository_and_copied_state_cannot_replace_a_live_project(self):
        result = self.add()
        before = self.catalog.read_bytes()
        other = self.root.with_name('another')
        self.git('clone', '--quiet', str(self.root), str(other))
        with self.assertRaisesRegex(RelayError, 'identity does not match'):
            self.add(other, replace_id=result['id'])
        shutil.copytree(common_dir(self.root) / 'devrelay', common_dir(other) / 'devrelay')
        with self.assertRaisesRegex(RelayError, 'already used'):
            self.add(other)
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_stale_preview_or_changed_catalog_cannot_overwrite(self):
        proposal = self.registry.plan(str(self.root))
        (self.root / 'package.json').write_text('{"scripts":{"check":"changed"}}')
        with self.assertRaisesRegex(RelayError, 'changed'):
            self.add(revision=proposal['revision'])
        self.assertFalse((self.root / '.devrelay/config.toml').exists())
        proposal = self.registry.plan(str(self.root))
        self.catalog.parent.mkdir(exist_ok=True)
        self.catalog.write_text('projects = []\n# someone edited this\n')
        with self.assertRaisesRegex(RelayError, 'changed'):
            self.add(revision=proposal['revision'])
        self.assertIn('someone edited', self.catalog.read_text())

    def test_active_worker_workspace_and_tasks_block_unsafe_rebinding(self):
        self.configure()
        result = self.add()
        with self.state.lock(), self.assertRaisesRegex(RelayError, 'lock'):
            self.add()
        for key, value, error in [('workspace_owner', 'workspace', 'reserved'),
                                  ('active_process', {'group': os.getpgrp()}, 'still runs')]:
            self.state.set_metadata(key, value)
            with self.assertRaisesRegex(RelayError, error):
                self.add()
            self.assertEqual(self.state.metadata(key), value)
            self.state.set_metadata(key, None)
        task = self.state.save({'id': '1', 'status': 'running', 'session': 'keep'})
        other = self.root.with_name('other')
        self.git('worktree', 'add', '-b', 'other', str(other))
        with self.assertRaisesRegex(RelayError, 'saved tasks'):
            self.add(other, replace_id=result['id'])
        self.assertEqual(self.state.get('1'), task)
        self.assertEqual(self.state.metadata('controller_root'), str(self.root))

    def test_catalog_lock_and_partial_write_can_be_retried_without_reset(self):
        with self.registry.lock(), self.assertRaisesRegex(RelayError, 'registration is in progress'):
            self.add()
        with patch.object(self.registry, 'write', side_effect=OSError('disk unavailable')):
            with self.assertRaisesRegex(OSError, 'disk unavailable'):
                self.add()
        config = (self.root / '.devrelay/config.toml').read_bytes()
        metadata = repository_state(self.root)[0]
        result = self.add()
        self.assertFalse(result['created_config'])
        self.assertEqual((self.root / '.devrelay/config.toml').read_bytes(), config)
        self.assertEqual(repository_state(self.root)[0], metadata)

    def test_legacy_catalog_preserves_custom_runtime_and_other_projects(self):
        self.configure()
        self.catalog.parent.mkdir()
        self.catalog.write_text('''# Legacy catalog\n[[projects]]\nid="existing"\nname="Keep name"\npath="../project"\nruntime="../custom runtime"\n\n[[projects]]\nid="missing"\npath="/missing/project"\n''')
        result = self.add()
        entries = self.registry.read()[1]
        self.assertEqual(result['id'], 'existing')
        self.assertEqual(entries[0]['name'], 'Keep name')
        self.assertEqual(entries[0]['runtime'], '../custom runtime')
        self.assertEqual(entries[1], {'id': 'missing', 'path': '/missing/project'})

    def test_invalid_paths_symlinks_and_configuration_are_preserved(self):
        for path in [self.root.with_name('missing'), self.root.parent]:
            with self.assertRaises(RelayError):
                self.add(path)
        (self.root / '.devrelay').mkdir()
        config = self.root / '.devrelay/config.toml'
        config.write_text('[invalid')
        with self.assertRaisesRegex(RelayError, 'Invalid project configuration'):
            self.add()
        self.assertEqual(config.read_text(), '[invalid')
        config.unlink()
        config.symlink_to(self.root / 'value.txt')
        with self.assertRaisesRegex(RelayError, 'symlink'):
            self.add()
        self.assertEqual((self.root / 'value.txt').read_text(), 'broken\n')

    def test_state_identity_loss_is_reported_instead_of_duplicating_registration(self):
        self.add()
        state = State(self.root)
        try:
            state.set_metadata('project_identity', None)
        finally:
            state.close()
        before = self.catalog.read_bytes()
        with self.assertRaisesRegex(RelayError, 'identity changed'):
            self.add()
        self.assertEqual(self.catalog.read_bytes(), before)

    def test_doctor_identifies_unregistered_configuration_then_uses_registered_runtime(self):
        config = self.configure()
        self.assertFalse(doctor(config)['ready'])
        self.add()
        report = doctor(config)
        self.assertTrue(report['ready'], report)
        self.assertIn('python', report['runtime'])
        self.assertTrue(Path(report['runtime']['path']).is_dir())

    def test_cli_uses_xdg_catalog_from_another_working_directory(self):
        config_home = self.catalog.parent.parent / 'xdg'
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config_home)}):
            self.assertEqual(default_catalog(), config_home / 'devrelay/projects.toml')
            for args in (['projects', 'add', str(self.root)], ['projects', 'list']):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(main(args), 0, err.getvalue())
                result = json.loads(out.getvalue())
                self.assertEqual(result['catalog'], str(default_catalog()))
            self.assertEqual(len(result['projects']), 1)

    def test_github_setup_is_explicit_revision_checked_and_locked(self):
        from devrelay.ui_bridge import main as bridge
        self.configure(provider="github")
        self.assertEqual(self.registry.plan(str(self.root))["base"], "main")
        self.add()
        project = projects(self.root)[0]
        current = project.call("inspect")
        result = {"labels": {"created": []}, "templates": {"created": []}}
        with patch('devrelay.onboarding.setup_github', return_value=result) as setup:
            with patch.object(sys, 'path', list(sys.path)):
                self.assertEqual(bridge({"operation": "setup-github", "revision": current['revision']},
                                        self.root, project.runtime), result)
                setup.assert_called_once()
                setup.reset_mock()
                with self.assertRaisesRegex(ValueError, 'changed'):
                    bridge({"operation": "setup-github", "revision": "stale"}, self.root, project.runtime)
                with self.state.lock(), self.assertRaisesRegex(RelayError, 'lock'):
                    bridge({"operation": "setup-github", "revision": current['revision']}, self.root, project.runtime)
                setup.assert_not_called()
        self.assertEqual(self.state.tasks(), [])

    def test_http_empty_catalog_check_add_and_request_protection(self):
        with patch('socket.getfqdn', side_effect=AssertionError('Local dashboard must not need DNS')):
            server = DashboardServer([], registry=self.registry)
        self.assertEqual(server.server_name, '127.0.0.1')
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        def request(path, payload=None, headers=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            try:
                connection.request('POST' if payload is not None else 'GET', path,
                                   body=json.dumps(payload) if payload is not None else None, headers=headers or {})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        status, session = request('/api/session')
        self.assertEqual(session['projects'], [])
        self.assertFalse(self.catalog.exists())
        headers = {'Origin': server.origin, 'X-DevRelay-Token': session['token'], 'Content-Type': 'application/json'}
        payload = {'path': str(self.root)}
        for hostile in ({'Origin': 'https://evil.test'}, {'Host': 'evil.test'}, {'X-DevRelay-Token': 'wrong'},
                        {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(request('/api/projects/probe', payload, {**headers, **hostile})[0], 403)
            self.assertEqual(request('/api/projects/add', payload, {**headers, **hostile})[0], 403)
        self.assertEqual(request('/api/projects/add', payload, headers)[0], 400)
        status, proposal = request('/api/projects/probe', payload, headers)
        self.assertEqual(status, 200, proposal)
        status, added = request('/api/projects/add', {**payload, 'revision': proposal['revision']}, headers)
        self.assertEqual(status, 200, added)
        session = request('/api/session')[1]
        self.assertEqual(len(session['projects']), 1)
        self.assertTrue(session['projects'][0]['registered'])
        endpoint = '/api/projects/' + added['id']
        status, inspected = request(endpoint + '/inspect', {}, headers)
        self.assertEqual(status, 200, inspected)
        self.assertEqual(inspected['root'], str(self.root))
        self.assertEqual(request(endpoint + '/setup-github', {'revision': 'stale'}, headers)[0], 400)
        self.assertFalse(request(endpoint + '/doctor', {}, headers)[1]['ready'])  # acceptance still required


if __name__ == '__main__':
    unittest.main()
