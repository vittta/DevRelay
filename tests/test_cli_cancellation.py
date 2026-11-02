import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from unittest.mock import patch

from devrelay.cli import main
from devrelay.errors import RelayError
from devrelay.process import execute
from devrelay.providers import add_local
from devrelay.runner import start
from devrelay.state import group_alive

from helpers import RepositoryTest


class CliCancellationTests(RepositoryTest):
    def invoke_process(self, *arguments, **environment):
        source = str(Path(__file__).resolve().parents[1] / "src")
        return subprocess.run([sys.executable, "-m", "devrelay", "--project", str(self.root), *arguments],
                              env={**os.environ, "PYTHONPATH": source, **environment},
                              capture_output=True, text=True, timeout=15)

    def signal_command(self):
        group = Path(self.temporary.name) / "command-group.txt"
        return [sys.executable, "-c",
                "import os, signal, subprocess, sys, time; from pathlib import Path; "
                f"Path({str(group)!r}).write_text(str(os.getpgrp())); "
                "subprocess.Popen([sys.executable, '-c', "
                "\"import time; from pathlib import Path; time.sleep(.6); "
                "Path('late-descendant.txt').write_text('escaped')\"]); "
                "os.kill(os.getppid(), signal.SIGTERM); time.sleep(.6); "
                "Path('value.txt').write_text('escaped')"], group

    def assert_cancelled(self, result, group):
        self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
        self.assertIn("Interrupted", json.loads(result.stderr)["error"])
        task = self.state.get("fix")
        self.assertEqual(task["status"], "interrupted")
        self.assertIsNone(task.get("verification"))
        self.assertIsNone(self.state.metadata("active_process"))
        self.assertFalse(group_alive(int(group.read_text())))
        worktree = Path(task["worktree"])
        self.assertFalse((worktree / "late-descendant.txt").exists())
        self.assertNotEqual((worktree / "value.txt").read_text(), "escaped")
        return task

    def test_sigterm_during_prepare_stops_group_and_retains_setup(self):
        command, group = self.signal_command()
        self.configure(setup=[command])
        add_local(self.config, self.state, "fix", "Fix value", "Depends on: none", "priority:P1")
        with self.state.lock():
            start(self.config, self.state, "fix")
        task = self.assert_cancelled(self.invoke_process("prepare", "fix"), group)
        self.assertIsNone(task.get("setup_fingerprint"))
        self.assertTrue((Path(task["setup_path"]) / "setup-0.log").is_file())

    def test_sigterm_during_check_stops_group_and_invalidates_verification(self):
        command, group = self.signal_command()
        self.configure(checks=[command])
        add_local(self.config, self.state, "fix", "Fix value", "Depends on: none", "priority:P1")
        with self.state.lock():
            start(self.config, self.state, "fix")
        task = self.assert_cancelled(self.invoke_process("check", "fix"), group)
        evidence = json.loads(Path(task["verification_path"]).read_text())
        self.assertFalse(evidence["passed"])
        self.assertEqual(evidence["error"], "Interrupted")

    def test_sigterm_during_run_retains_exact_session_and_stops_implementation(self):
        executable = self.fake_codex()
        group = Path(self.temporary.name) / "implementation-group.txt"
        script = executable.read_text().replace("signal.SIGINT", "signal.SIGTERM")
        script = script.replace("if mode == 'interrupt':\n", "if mode == 'interrupt':\n"
                                f"    Path({str(group)!r}).write_text(str(os.getpgrp()))\n"
                                "    time.sleep(.15)\n")
        executable.write_text(script)
        self.configure(executable=executable)
        add_local(self.config, self.state, "fix", "Fix value", "Depends on: none", "priority:P1")
        result = self.invoke_process("run", "fix", DEVRELAY_TEST_MODE="interrupt")
        task = self.assert_cancelled(result, group)
        self.assertEqual(task["session_id"], "exact-test-session")
        self.assertTrue(Path(task["event_log"]).is_file())
        resumed = self.invoke_process("resume", "fix", DEVRELAY_TEST_MODE="pass")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["status"], "verified")

    def test_cli_restores_sigterm_handler_after_failure(self):
        self.configure()
        previous = signal.getsignal(signal.SIGTERM)
        with patch("devrelay.cli.load", side_effect=KeyboardInterrupt), patch("builtins.print"):
            self.assertEqual(main(["--project", str(self.root), "status"]), 130)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_interruption_before_process_marker_is_saved_still_stops_command(self):
        self.configure()
        save = self.state.set_metadata
        group = None

        def interrupt_first_marker(key, value):
            nonlocal group
            if key == "active_process" and value is not None and group is None:
                group = value["group"]
                raise KeyboardInterrupt
            return save(key, value)

        with patch.object(self.state, "set_metadata", side_effect=interrupt_first_marker):
            with self.assertRaises(KeyboardInterrupt):
                execute(self.state, [sys.executable, "-c", "import time; time.sleep(30)"],
                        self.root, self.state.directory / "interrupted-start.log", 10)
        self.assertIsNotNone(group)
        self.assertFalse(group_alive(group))
        self.assertIsNone(self.state.metadata("active_process"))

    def test_failed_startup_cancellation_retains_process_group_marker(self):
        self.configure()
        save = self.state.set_metadata
        interrupted = False
        process = None

        def interrupt_first_marker(key, value):
            nonlocal interrupted
            if key == "active_process" and value is not None and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return save(key, value)

        def fail_termination(command):
            nonlocal process
            process = command
            raise RelayError("Cannot stop process group")

        try:
            with patch.object(self.state, "set_metadata", side_effect=interrupt_first_marker):
                with patch("devrelay.process.terminate", side_effect=fail_termination):
                    with self.assertRaisesRegex(RelayError, "Cannot stop process group"):
                        execute(self.state, [sys.executable, "-c", "import time; time.sleep(30)"],
                                self.root, self.state.directory / "failed-start-cancellation.log", 10)
            self.assertEqual(self.state.metadata("active_process")["group"], process.pid)
            self.assertTrue(group_alive(process.pid))
            with self.assertRaisesRegex(RelayError, "Previous command still runs"):
                with self.state.lock():
                    self.fail("The interrupted startup must keep recovery blocked")
        finally:
            if process is not None:
                process.kill()
                process.wait(timeout=5)
