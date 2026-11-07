import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from devrelay.config import load
from devrelay.state import State


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="devrelay-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "DevRelay Test")
        self.git("config", "user.email", "devrelay@example.invalid")
        (self.root / ".gitignore").write_text(".devrelay/worktrees/\n.devrelay/runtime/\n__pycache__/\n")
        (self.root / "value.txt").write_text("broken\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.root, text=True,
                                       stderr=subprocess.PIPE).strip()

    def write_filename_fixture(self, root, filename, content):
        path = root / os.fsdecode(filename)
        try:
            path.write_bytes(content)
        except OSError as error:
            if error.errno == errno.EILSEQ:
                try:
                    filename.decode("utf-8")
                except UnicodeDecodeError:
                    self.skipTest(f"Filesystem rejects non-UTF-8 filename {filename!r} with EILSEQ; "
                                  "filesystem end-to-end case unavailable")
            raise
        return path

    def configure(self, *, provider="local", checks=None, setup=None, executable="codex", attempts=2):
        directory = self.root / ".devrelay"
        directory.mkdir(exist_ok=True)
        lines = ["[project]", f"provider = {json.dumps(provider)}", 'base_branch = "main"',
                 'repository = "example/project"', "[codex]", f"executable = {json.dumps(str(executable))}",
                 'sandbox = "workspace-write"', "timeout_seconds = 10", f"max_attempts = {attempts}"]
        if checks is None:
            checks = [[sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'fixed\\n'"]]
        for phase, commands in (("checks", checks), ("setup", setup or [])):
            for index, command in enumerate(commands):
                lines += [f"[[{phase}]]", f'id = "{phase}-{index}"', f"argv = {json.dumps(command)}", "timeout_seconds = 10"]
        (directory / "config.toml").write_text("\n".join(lines) + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", "project config")
        self.config = load(self.root)
        self.state = State(self.root)
        self.addCleanup(self.state.close)
        return self.config

    def fake_codex(self):
        executable = Path(self.temporary.name) / "fake-codex"
        executable.write_text('''#!/usr/bin/env python3
import json, os, signal, sys, time
from pathlib import Path
args = sys.argv[1:]
if args == ['login', 'status']:
    raise SystemExit(0)
prompt = sys.stdin.read()
mode = os.environ.get('DEVRELAY_TEST_MODE', 'pass')
trace = os.environ.get('DEVRELAY_TEST_TRACE')
if trace:
    with open(trace, 'a') as f:
        f.write(json.dumps({'argv': args, 'cwd': str(Path.cwd()), 'prompt': prompt}) + '\\n')
destination = Path(args[args.index('--output-last-message') + 1])
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
if 'decision' in schema['properties']:
    task = json.loads(next(line.removeprefix('Task (JSON): ') for line in prompt.splitlines() if line.startswith('Task (JSON): ')))
    context = json.loads(next(line.removeprefix('Investigation context: ') for line in prompt.splitlines() if line.startswith('Investigation context: ')))
    decision = json.loads(os.environ.get('DEVRELAY_TEST_TRIAGE_BY_ID', '{}')).get(task['id'], os.environ.get('DEVRELAY_TEST_TRIAGE', 'ready'))
    session = os.environ.get('DEVRELAY_TEST_TRIAGE_SESSION', 'triage-session-' + __import__('uuid').uuid4().hex)
    print(json.dumps({'type': 'thread.started', 'thread_id': session}), flush=True)
    if decision == 'failure':
        print(json.dumps({'type': 'turn.failed'}), flush=True)
        raise SystemExit(9)
    result = {'context_sha256': context['context_sha256'], 'decision': decision,
              'kind': os.environ.get('DEVRELAY_TEST_TRIAGE_KIND', 'bug'),
              'summary': 'Investigated bounded behavior',
              'evidence': 'Read value.txt on the unchanged base: ' + Path('value.txt').read_text().strip(),
              'acceptance': 'value.txt contains fixed with a trailing newline',
              'questions': ['Which expected behavior should this implement? Choose fixed or unchanged.'] if decision == 'needs_input' else [],
              'duplicate_of': ''}
    destination.write_text(json.dumps(result))
    print(json.dumps({'type': 'turn.completed'}), flush=True)
    raise SystemExit(0)
if 'verdict' in schema['properties']:
    review_mode = os.environ.get('DEVRELAY_TEST_REVIEW', 'approve')
    print(json.dumps({'type': 'thread.started', 'thread_id':
        'exact-test-session' if review_mode == 'same-session' else 'fresh-review-session' + __import__('uuid').uuid4().hex}), flush=True)
    if review_mode == 'failure':
        print(json.dumps({'type': 'turn.failed'}))
        raise SystemExit(9)
    if review_mode == 'modify':
        Path('value.txt').write_text('changed by reviewer\\n')
    result = {'verdict': review_mode if review_mode in ('changes_requested', 'blocked') else 'approve',
              'summary': 'Inspected the diff',
              'findings': ['Fix the reported regression'] if review_mode in ('changes_requested', 'blocked') else []}
    if review_mode == 'inconsistent':
        result['findings'] = ['Blocking issue']
    if 'discoveries' in schema['properties']:
        result['discoveries'] = json.loads(os.environ.get('DEVRELAY_TEST_REVIEW_DISCOVERIES', '[]'))
    destination.write_text('invalid json' if review_mode == 'invalid' else json.dumps(result))
    if review_mode != 'incomplete':
        print(json.dumps({'type': 'turn.completed'}), flush=True)
    raise SystemExit(0)
session = (args[args.index('resume') + 1] if 'resume' in args else
           'started-session' if mode == 'controller-crash' else 'exact-test-session')
if mode == 'stderr-command-failure':
    print('ordinary CLI diagnostic', file=sys.stderr, flush=True)
if mode == 'controller-crash' and 'resume' not in args:
    os.kill(os.getppid(), signal.SIGSTOP)
print(json.dumps({'type': 'thread.started', 'thread_id': session}), flush=True)
if mode == 'unicode-chunks':
    message = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
                         'text': 'first\\u0085second\\u2028third\\u2029last'}}, ensure_ascii=False)
    boundary = message.index('\\u2028') + 1
    sys.stdout.write(message[:boundary])
    sys.stdout.flush()
    time.sleep(0.15)
    sys.stdout.write(message[boundary:] + '\\n')
    sys.stdout.flush()
if mode == 'controller-crash' and 'resume' not in args:
    time.sleep(30)
if mode == 'interrupt':
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(30)
if mode in ('command-failure', 'stderr-command-failure', 'unicode-chunks'):
    print(json.dumps({'type': 'turn.failed'}))
    raise SystemExit(9)
if mode != 'blocked':
    marker = Path('.devrelay/runtime/fake-attempt')
    marker.parent.mkdir(parents=True, exist_ok=True)
    first = not marker.exists()
    marker.write_text('attempted')
    Path('value.txt').write_text('broken\\n' if mode == 'retry' and first else 'fixed\\n')
result = {'outcome': 'blocked' if mode == 'blocked' else 'ready',
          'summary': 'Missing dataset' if mode == 'blocked' else 'Fixed the bounded behavior',
          'blockers': ['Provide dataset'] if mode == 'blocked' else []}
if 'discoveries' in schema['properties']:
    result['discoveries'] = json.loads(os.environ.get('DEVRELAY_TEST_DISCOVERIES', '[]'))
destination.write_text('invalid json' if mode == 'bad-result' else json.dumps(result))
if mode != 'no-completed-turn':
    print(json.dumps({'type': 'turn.completed'}), flush=True)
''')
        executable.chmod(0o755)
        return executable
