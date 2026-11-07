"""Exercise a built wheel through its installed CLI and HTTP API, outside the source tree.

Usage: python3 scripts/smoke_install.py dist/devrelay-*.whl
Only temporary local Git repositories are used; no GitHub writes or models.
"""

import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import venv


def run(argv, cwd, env):
    return subprocess.check_output(argv, cwd=cwd, env=env, text=True, stderr=subprocess.PIPE).strip()


def serve(entry, cwd, env):
    process = subprocess.Popen([str(entry), 'ui', '--port', '0'], cwd=cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        if not selector.select(20):
            raise RuntimeError('Installed dashboard did not start within 20 seconds')
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(process.stderr.read())
        return process, json.loads(line)['url']
    except BaseException as error:
        # SIGINT retains Python's startup traceback if the installed command hangs.
        process.send_signal(signal.SIGINT)
        try:
            output, errors = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, errors = process.communicate()
        raise RuntimeError(f'{error}\nDashboard exit: {process.returncode}\n'
                           f'stdout: {output}\nstderr: {errors}') from error
    finally:
        selector.close()


def stop(process):
    process.send_signal(signal.SIGINT)
    process.communicate(timeout=15)
    if process.returncode != 0:
        raise RuntimeError(f'Dashboard exited with {process.returncode}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='devrelay-installed-') as temporary:
        root = Path(temporary).resolve()
        environment = root / 'tool'
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / 'bin/python'
        entry = environment / 'bin/devrelay'
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        env.update(XDG_CONFIG_HOME=str(root / 'user config'), PYTHONNOUSERSITE='1',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                   PATH=str(environment / 'bin') + os.pathsep + env.get('PATH', ''))
        run([str(python), '-m', 'pip', 'install', '--no-deps', str(wheel)], root, env)
        assert run([str(entry), '--version'], root, env)
        repo = root / 'first project 中文'
        repo.mkdir()
        def git(path, *args):
            return run(['git', '-C', str(path), *args], root, env)
        def init(path):
            git(path, 'init', '-q', '-b', 'main')
            git(path, 'config', 'user.name', 'Installation Test')
            git(path, 'config', 'user.email', 'test@example.invalid')
            (path / 'value.txt').write_text('keep\n')
            git(path, 'add', '.')
            git(path, 'commit', '-qm', 'initial')
        init(repo)
        process, origin = serve(entry, root, env)
        try:
            def get(path):
                return urllib.request.urlopen(origin + path, timeout=20).read()
            session = json.loads(get('/api/session'))
            assert session['projects'] == []
            assert Path(session['runtime']['path']).is_relative_to(environment)
            for path in ('/', '/app.js', '/style.css'):
                assert get(path)
            def post(path, body):
                request = urllib.request.Request(origin + path, data=json.dumps(body).encode(), headers={
                    'Origin': origin, 'Content-Type': 'application/json', 'X-DevRelay-Token': session['token']})
                return json.load(urllib.request.urlopen(request, timeout=30))
            def add(path, **kwargs):
                payload = {'path': str(path), **kwargs}
                plan = post('/api/projects/probe', payload)
                return post('/api/projects/add', {**payload, 'revision': plan['revision']})
            plan = post('/api/projects/probe', {'path': str(repo)})
            assert not (repo / '.devrelay').exists()
            first = post('/api/projects/add', {'path': str(repo), 'revision': plan['revision']})
            assert first['needs_acceptance']
            endpoint = '/api/projects/' + first['id']
            inspected = post(endpoint + '/inspect', {})
            preview = post(endpoint + '/preview', {'revision': inspected['revision'], 'changes': {
                'checks': [{'id': 'test', 'argv': ['python3', '-c', 'pass'], 'timeout_seconds': 60}]}})
            post(endpoint + '/save', {key: preview[key] for key in ('revision', 'source', 'preview_sha256')})
            assert post(endpoint + '/doctor', {})['ready']
            assert add(repo)['already_added']
            git(repo, 'add', '.')
            git(repo, 'commit', '-qm', 'shared config')
            other = root / 'second project'
            git(root, 'clone', '-q', str(repo), str(other))
            git(other, 'config', 'user.name', 'Installation Test')
            git(other, 'config', 'user.email', 'test@example.invalid')
            config = (other / '.devrelay/config.toml').read_bytes()
            (other / 'value.txt').write_text('staged edit\n')
            git(other, 'add', 'value.txt')
            (other / 'value.txt').write_text('unstaged edit\n')
            index = git(other, 'write-tree')
            second = add(other)
            assert not second['created_config']
            assert (other / '.devrelay/config.toml').read_bytes() == config
            assert git(other, 'write-tree') == index
            assert (other / 'value.txt').read_text() == 'unstaged edit\n'
            assert len(json.loads(get('/api/session'))['projects']) == 2
            moved = root / 'relocated project'
            repo.rename(moved)
            relocated = add(moved, replace_id=first['id'])
            assert relocated['id'] == first['id']
            assert post(endpoint + '/inspect', {})['root'] == str(moved)
            try:
                add(other, replace_id=first['id'])
            except urllib.error.HTTPError as error:
                assert error.code == 400
            else:
                raise AssertionError('Wrong repository was accepted for relocation')
            listed = json.loads(run([str(entry), 'projects', 'list'], root, env))
            assert len(listed['projects']) == 2
            for path in (moved, other):
                status = json.loads(run([str(entry), '--project', str(path), 'status'], root, env))
                assert status['tasks'] == [] and status['project'] == str(path)
        finally:
            stop(process)
        # A reinstall must preserve the user catalog, controller identity and edits.
        catalog = Path(listed['catalog'])
        before = catalog.read_bytes()
        run([str(python), '-m', 'pip', 'install', '--force-reinstall', '--no-deps', str(wheel)], root, env)
        process, origin = serve(entry, root, env)
        try:
            session = json.load(urllib.request.urlopen(origin + '/api/session', timeout=20))
            assert len(session['projects']) == 2 and catalog.read_bytes() == before
            assert (other / '.devrelay/config.toml').read_bytes() == config
            assert git(other, 'write-tree') == index
        finally:
            stop(process)
        print(json.dumps({'passed': True, 'wheel': wheel.name, 'projects': 2,
                          'source_independent': True, 'reinstall_preserved_state': True,
                          'tasks_started': 0}))


if __name__ == '__main__':
    main()
