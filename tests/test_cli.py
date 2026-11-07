from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from devrelay import __version__
from devrelay.cli import main, parser

from helpers import RepositoryTest


class WatchArgumentTests(unittest.TestCase):
    def test_poll_interval_alias_uses_same_destination_and_default(self):
        self.assertEqual(parser().parse_args(["watch"]).poll_interval, 60)
        for option in ("--poll-interval", "--interval"):
            with self.subTest(option=option):
                args = parser().parse_args(["watch", option, "30"])
                self.assertEqual(args.poll_interval, 30)

    def test_poll_interval_alias_rejects_non_positive_integers(self):
        for option in ("--poll-interval", "--interval"):
            for value in ("0", "-1", "1.5", "invalid"):
                with self.subTest(option=option, value=value):
                    with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
                        parser().parse_args(["watch", option, value])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn("must be a positive integer", err.getvalue())

    def test_max_tasks_alias_uses_same_destination_and_default(self):
        self.assertIsNone(parser().parse_args(["watch"]).max_tasks)
        for option in ("--max-tasks", "--limit"):
            with self.subTest(option=option):
                args = parser().parse_args(["watch", option, "2"])
                self.assertEqual(args.max_tasks, 2)

    def test_max_tasks_alias_rejects_non_positive_integers(self):
        for option in ("--max-tasks", "--limit"):
            for value in ("0", "-1", "1.5", "invalid"):
                with self.subTest(option=option, value=value):
                    with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
                        main(["watch", option, value])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn("must be a positive integer", err.getvalue())

    def test_watch_help_lists_all_aliases_without_side_effects(self):
        with patch("devrelay.cli.repository_root") as repository_root, patch("devrelay.cli.watch") as watch, \
                patch("devrelay.cli.subprocess.run") as subprocess_run:
            with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as raised:
                main(["watch", "--help"])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("--poll-interval", out.getvalue())
            self.assertIn("--interval", out.getvalue())
            self.assertIn("--max-tasks", out.getvalue())
            self.assertIn("--limit", out.getvalue())
            repository_root.assert_not_called()
            watch.assert_not_called()
            subprocess_run.assert_not_called()


class VersionTests(unittest.TestCase):
    def test_version_skips_project_initialization(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            for flag in ("--version", "-V"):
                with self.subTest(flag=flag):
                    out, err = io.StringIO(), io.StringIO()
                    with (redirect_stdout(out), redirect_stderr(err),
                          patch("devrelay.cli.repository_root") as repository,
                          patch("devrelay.cli.load") as load,
                          patch("devrelay.cli.State") as state,
                          self.assertRaises(SystemExit) as exit):
                        main([flag])
                    self.assertEqual(exit.exception.code, 0)
                    self.assertEqual(out.getvalue(), f"{__version__}\n")
                    self.assertEqual(err.getvalue(), "")
                    repository.assert_not_called()
                    load.assert_not_called()
                    state.assert_not_called()
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_version_process_succeeds_outside_git_repository(self):
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        with tempfile.TemporaryDirectory() as directory:
            for flag in ("--version", "-V"):
                with self.subTest(flag=flag):
                    result = subprocess.run([sys.executable, "-m", "devrelay", flag], cwd=directory,
                                            env=environment, text=True, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, f"{__version__}\n")
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(list(Path(directory).iterdir()), [])


class CliTests(RepositoryTest):
    def test_work_delivers_local_task_and_status_explains_next_action(self):
        self.configure(executable=self.fake_codex())
        file = Path(self.temporary.name) / "task.md"
        file.write_text("Fix one bounded behavior")
        self.assertEqual(self.invoke("task", "add", "fix", "--title", "Fix value", "--file", str(file))[0], 0)
        code, result = self.invoke("work", "fix")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["next_action"]["kind"], "delivered")
        code, status = self.invoke("status")
        self.assertEqual(status["tasks"][0]["next_action"]["kind"], "delivered")
        code, error = self.invoke("work", "--retry")
        self.assertEqual(code, 1)
        self.assertIn("requires a task ID", error["error"])

    def invoke(self, *argv, project=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--project", str(project or self.root), *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_onboarding_is_non_destructive_and_reports_missing_acceptance(self):
        code, result = self.invoke("init", "--provider", "local")
        self.assertEqual(code, 0)
        self.assertEqual(result["checks"], 0)
        code, result = self.invoke("doctor")
        self.assertEqual(code, 1)
        self.assertFalse(next(item for item in result["checks"] if item["id"] == "acceptance")["ok"])
        code, result = self.invoke("init")
        self.assertEqual(code, 1)
        self.assertIn("already exists", result["error"])

    def test_doctor_checks_current_github_credentials_despite_inactive_account_failure(self):
        self.configure(provider="github", executable=self.fake_codex())
        self.state.set_metadata("controller_root", str(self.root))
        self.git("remote", "add", "origin", "https://github.com/example/project.git")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        run, which = subprocess.run, shutil.which
        api = ["gh", "api", "--hostname", "github.com", "user", "--silent"]
        outcomes = (0, 1, FileNotFoundError("gh unavailable"), subprocess.TimeoutExpired(api, 15))
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                calls = []

                def gh_run(argv, **kwargs):
                    if argv[0] != "gh":
                        return run(argv, **kwargs)
                    calls.append(argv)
                    # A stale inactive account makes the all-account check fail.
                    if argv[1:3] == ["auth", "status"]:
                        return subprocess.CompletedProcess(argv, 1, "", "Inactive account expired")
                    self.assertEqual(argv, api)
                    self.assertEqual(kwargs["timeout"], 15)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return subprocess.CompletedProcess(argv, outcome, "", "")

                with (patch("devrelay.cli.subprocess.run", side_effect=gh_run),
                      patch("devrelay.cli.shutil.which", side_effect=lambda name: "/test/gh" if name == "gh" else which(name))):
                    code, report = self.invoke("doctor")
                expected = outcome == 0
                self.assertEqual(code, 0 if expected else 1, report)
                self.assertEqual(report["ready"], expected)
                checks = {item["id"]: item["ok"] for item in report["checks"]}
                self.assertEqual(checks.pop("github_login"), expected)
                self.assertTrue(all(checks.values()), checks)
                self.assertEqual(calls, [api])

    def test_cli_runs_an_offline_task_and_routes_calls_from_its_worktree(self):
        self.configure(executable=self.fake_codex())
        task_file = Path(self.temporary.name) / "task.md"
        task_file.write_text("Fix the bounded behavior")
        code, _ = self.invoke("task", "add", "fix", "--title", "Fix value", "--file", str(task_file))
        self.assertEqual(code, 0)
        code, result = self.invoke("run", "fix")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "verified")
        # Record the anchor used by init so a CLI call inside the task uses the controller config.
        self.state.set_metadata("controller_root", str(self.root))
        code, status = self.invoke("status", project=Path(result["worktree"]))
        self.assertEqual(code, 0)
        self.assertEqual(status["project"], str(self.root))
        code, committed = self.invoke("submit", "fix", "--local", project=Path(result["worktree"]))
        self.assertEqual(code, 0)
        self.assertEqual(committed["status"], "committed")

    def test_review_and_merge_commands_reject_local_projects(self):
        self.configure(executable=self.fake_codex())
        for operation in ("review", "merge"):
            with self.subTest(operation=operation):
                code, result = self.invoke(operation, "fix")
                self.assertEqual(code, 1)
                self.assertIn("provider=github", result["error"])


if __name__ == "__main__":
    unittest.main()
