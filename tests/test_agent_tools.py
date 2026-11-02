"""Real tool-driven delivery, without a DevRelay-managed implementation agent."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

from devrelay.cli import doctor, main, probe
from devrelay.config import load

from helpers import RepositoryTest


class AgentToolTests(RepositoryTest):
    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--project", str(self.root), *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def add_task(self):
        path = Path(self.temporary.name) / "task.md"
        path.write_text("Fix value.txt\nDepends on: none\n")
        code, task = self.invoke("task", "add", "fix", "--title", "Fix value", "--file", str(path))
        self.assertEqual(code, 0, task)

    def test_agent_delivers_without_codex_and_preserves_controller_work(self):
        self.configure(executable="/no-such-devrelay-codex", setup=[[
            sys.executable, "-c",
            "from pathlib import Path; p=Path('.devrelay/runtime/environment'); p.parent.mkdir(parents=True); p.write_text('ready')",
        ]])
        self.add_task()
        (self.root / "user-work.txt").write_text("Keep controller work")
        with patch("devrelay.runner.codex.implement", side_effect=AssertionError("Unexpected implementation agent")):
            code, report = self.invoke("doctor")
            self.assertEqual(code, 0, report)
            self.assertFalse(report["codex_required"])
            self.assertNotIn("codex", {item["id"] for item in report["checks"]})
            code, report = self.invoke("next")
            self.assertEqual(code, 0, report)
            self.assertEqual(report["selected"]["id"], "fix")
            code, task = self.invoke("start", "fix")
            self.assertEqual(code, 0, task)
            worktree = Path(task["worktree"])
            code, task = self.invoke("prepare", "fix")
            self.assertEqual(code, 0, task)
            self.assertEqual(task["status"], "prepared")
            self.assertEqual((worktree / ".devrelay/runtime/environment").read_text(), "ready")
            self.assertTrue(list(Path(task["setup_path"]).glob("*.receipt.json")))
            (worktree / "value.txt").write_text("fixed\n")
            code, task = self.invoke("check", "fix")
            self.assertEqual(code, 0, task)
            self.assertTrue(task["verification"]["passed"])
            code, task = self.invoke("submit", "fix", "--local")
            self.assertEqual(code, 0, task)
            self.assertEqual(task["status"], "committed")
        self.assertEqual(task["attempts"], 0)
        self.assertIsNone(task.get("session_id"))
        self.assertEqual((self.root / "value.txt").read_text(), "broken\n")
        self.assertEqual((self.root / "user-work.txt").read_text(), "Keep controller work")
        self.assertEqual(self.git("show", "HEAD:value.txt", cwd=worktree), "fixed")

    def test_doctor_checks_codex_only_when_requested(self):
        self.configure(executable="/no-such-devrelay-codex")
        with patch("devrelay.cli.probe", wraps=probe) as calls:
            code, report = self.invoke("doctor")
            self.assertEqual(code, 0, report)
            self.assertFalse(any(call.args[0][0] == "/no-such-devrelay-codex" for call in calls.call_args_list))
        code, report = self.invoke("doctor", "--with-codex")
        self.assertEqual(code, 1, report)
        self.assertTrue(report["codex_required"])
        self.assertFalse(next(item for item in report["checks"] if item["id"] == "codex")["ok"])

    def test_doctor_checks_codex_for_configured_ai_delivery(self):
        self.configure(provider="github", executable="/no-such-devrelay-codex")
        path = self.root / ".devrelay/config.toml"
        with path.open("a") as stream:
            stream.write("\n[delivery]\nauto_review = true\n")
        config = load(self.root)
        with patch("devrelay.cli.validate_remote"), patch("devrelay.cli.probe", return_value=True):
            report = doctor(config)
        self.assertFalse(report["ready"])
        self.assertTrue(report["codex_required"])
        self.assertFalse(next(item for item in report["checks"] if item["id"] == "codex")["ok"])
        self.assertIn("no fetch tracking mapping",
                      next(item for item in report["checks"] if item["id"] == "base")["error"])
        self.assertIn("codex_login", {item["id"] for item in report["checks"]})

    def test_doctor_reports_missing_base_mapping_and_keeps_other_diagnostics(self):
        self.configure(provider="github")
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        with (patch("devrelay.cli.validate_remote"),
              patch("devrelay.cli.shutil.which", return_value="available"),
              patch("devrelay.cli.probe", return_value=True)):
            code, report = self.invoke("doctor", "--with-codex")
        self.assertEqual(code, 1, report)
        self.assertFalse(report["ready"])
        self.assertTrue(report["codex_required"])
        checks = {item["id"]: item for item in report["checks"]}
        self.assertFalse(checks["base"]["ok"])
        self.assertEqual(checks["base"]["error"],
                         "Base main has no fetch tracking mapping for remote origin; "
                         "configure a positive fetch refspec before using this project")
        self.assertEqual(set(checks), {"git", "base", "acceptance", "git_identity", "codex",
                                      "codex_login", "gh", "github_login", "remote_matches_repository"})
        self.assertTrue(all(item["ok"] for key, item in checks.items() if key != "base"))

    def test_prepare_failure_retains_work_and_can_be_retried(self):
        self.configure(executable="/no-such-devrelay-codex", setup=[[
            sys.executable, "-c", "from pathlib import Path; assert Path('allow-setup').exists()",
        ]])
        self.add_task()
        _, task = self.invoke("start", "fix")
        worktree = Path(task["worktree"])
        (worktree / "unfinished.txt").write_text("Keep implementation")
        code, report = self.invoke("prepare", "fix")
        self.assertEqual(code, 1, report)
        saved = self.state.get("fix")
        self.assertEqual(saved["status"], "failed")
        self.assertIsNone(saved["setup_fingerprint"])
        failed_path = Path(saved["setup_path"])
        self.assertTrue(list(failed_path.glob("*.receipt.json")))
        (worktree / "allow-setup").touch()
        code, task = self.invoke("prepare", "fix")
        self.assertEqual(code, 0, task)
        self.assertEqual(task["status"], "prepared")
        self.assertNotEqual(Path(task["setup_path"]), failed_path)
        self.assertTrue(failed_path.exists())
        self.assertEqual((worktree / "unfinished.txt").read_text(), "Keep implementation")

    def test_prepare_rechecks_authorization_before_any_setup(self):
        self.configure(setup=[[sys.executable, "-c", "raise AssertionError('Must not run')"]])
        self.add_task()
        _, task = self.invoke("start", "fix")
        task["labels"] = ["priority:P1"]
        self.state.save(task)
        with patch("devrelay.runner.execute") as execute:
            code, report = self.invoke("prepare", "fix")
        self.assertEqual(code, 1, report)
        self.assertIn("Missing auto:approved", report["error"])
        execute.assert_not_called()
        self.assertEqual(self.state.get("fix")["status"], "claimed")

    def test_failed_and_stale_evidence_blocks_agent_submission(self):
        self.configure(executable="/no-such-devrelay-codex")
        self.add_task()
        _, task = self.invoke("start", "fix")
        worktree = Path(task["worktree"])
        code, _ = self.invoke("check", "fix")
        self.assertEqual(code, 1)
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1, report)
        self.assertIn("missing or stale", report["error"])
        (worktree / "value.txt").write_text("fixed\n")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 0, report)
        (worktree / "new-unverified.txt").write_text("New implementation")
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1, report)
        self.assertIn("missing or stale", report["error"])
        self.assertEqual(self.git("show", "HEAD:value.txt", cwd=worktree), "broken")

    def test_explicit_prepare_invalidates_existing_verification(self):
        self.configure(executable="/no-such-devrelay-codex")
        self.add_task()
        _, task = self.invoke("start", "fix")
        (Path(task["worktree"]) / "value.txt").write_text("fixed\n")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 0, report)
        code, task = self.invoke("prepare", "fix")
        self.assertEqual(code, 0, task)
        self.assertIsNone(task["verification"])
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1, report)
        self.assertIn("missing or stale", report["error"])

    def test_prepare_can_recover_a_commit_but_not_completed_delivery(self):
        self.configure()
        self.add_task()
        _, task = self.invoke("start", "fix")
        (Path(task["worktree"]) / "value.txt").write_text("fixed\n")
        self.invoke("check", "fix")
        code, task = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 0, task)
        code, prepared = self.invoke("prepare", "fix")
        self.assertEqual(code, 0, prepared)
        self.assertIsNone(prepared["verification"])
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 1, report)
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 0, report)
        code, recovered = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 0, recovered)
        self.assertEqual(recovered["head_sha"], task["head_sha"])
        self.git("merge", "--ff-only", recovered["branch"])
        code, report = self.invoke("task", "complete", "fix")
        self.assertEqual(code, 0, report)
        with patch("devrelay.runner.execute") as execute:
            code, report = self.invoke("prepare", "fix")
        self.assertEqual(code, 1, report)
        execute.assert_not_called()
        self.assertEqual(self.state.get("fix")["status"], "completed")

    def test_missing_and_failed_setup_block_checks_and_delivery_then_retry(self):
        self.configure(executable="/no-such-devrelay-codex", setup=[[
            sys.executable, "-c", "from pathlib import Path; assert Path('.devrelay/runtime/allow').exists()",
        ]])
        self.add_task()
        _, task = self.invoke("start", "fix")
        worktree = Path(task["worktree"])
        (worktree / "value.txt").write_text("fixed\n")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1, report)
        self.assertIn("setup is missing or stale", report["error"])
        code, report = self.invoke("prepare", "fix")
        self.assertEqual(code, 1, report)
        for operation in (("check", "fix"), ("submit", "fix", "--local")):
            code, report = self.invoke(*operation)
            self.assertEqual(code, 1, report)
            self.assertIn("setup is missing or stale", report["error"])
        self.assertEqual(self.state.get("fix")["status"], "failed")
        marker = worktree / ".devrelay/runtime/allow"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        for operation in (("prepare", "fix"), ("check", "fix"), ("submit", "fix", "--local")):
            code, report = self.invoke(*operation)
            self.assertEqual(code, 0, report)

    def test_stale_setup_blocks_checks_and_submission_with_existing_proof(self):
        self.configure(setup=[[sys.executable, "-c", "pass"]])
        self.add_task()
        _, task = self.invoke("start", "fix")
        self.invoke("prepare", "fix")
        (Path(task["worktree"]) / "value.txt").write_text("fixed\n")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 0, report)
        config_path = self.root / ".devrelay/config.toml"
        config_path.write_text(config_path.read_text().replace("timeout_seconds = 10", "timeout_seconds = 11"))
        for operation in (("check", "fix"), ("submit", "fix", "--local")):
            code, report = self.invoke(*operation)
            self.assertEqual(code, 1, report)
            self.assertIn("setup is missing or stale", report["error"])
        code, report = self.invoke("prepare", "fix")
        self.assertEqual(code, 0, report)
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 0, report)

    def test_interrupted_setup_keeps_work_and_requires_explicit_retry(self):
        self.configure(setup=[[sys.executable, "-c", "pass"]])
        self.add_task()
        _, task = self.invoke("start", "fix")
        worktree = Path(task["worktree"])
        (worktree / "value.txt").write_text("fixed\n")
        with patch("devrelay.runner.execute", side_effect=KeyboardInterrupt):
            code, report = self.invoke("prepare", "fix")
        self.assertEqual(code, 130, report)
        self.assertEqual(self.state.get("fix")["status"], "interrupted")
        self.assertIsNone(self.state.get("fix")["setup_fingerprint"])
        self.assertEqual((worktree / "value.txt").read_text(), "fixed\n")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1, report)
        code, report = self.invoke("prepare", "fix")
        self.assertEqual(code, 0, report)
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 0, report)

    def test_local_completion_requires_latest_content_to_be_submitted_and_integrated(self):
        self.configure()
        self.add_task()
        _, task = self.invoke("start", "fix")
        worktree = Path(task["worktree"])
        (worktree / "value.txt").write_text("fixed\n")
        self.invoke("check", "fix")
        _, delivered = self.invoke("submit", "fix", "--local")
        self.git("merge", "--ff-only", delivered["branch"])
        dependency_file = Path(self.temporary.name) / "dependent.md"
        dependency_file.write_text("Depends on: #fix")
        self.invoke("task", "add", "dependent", "--title", "Dependent", "--file", str(dependency_file))
        (worktree / "new-content.txt").write_text("Latest task content\n")
        for check_again in (False, True):
            if check_again:
                code, report = self.invoke("check", "fix")
                self.assertEqual(code, 0, report)
            code, report = self.invoke("task", "complete", "fix")
            self.assertEqual(code, 1, report)
            self.assertIn("Latest verified content", report["error"])
            _, queue = self.invoke("next")
            self.assertIsNone(queue["selected"])
        code, report = self.invoke("submit", "fix", "--local")
        self.assertEqual(code, 0, report)
        code, report = self.invoke("task", "complete", "fix")
        self.assertEqual(code, 1, report)
        self.assertIn("integrated", report["error"])
        self.git("merge", "--ff-only", delivered["branch"])
        code, report = self.invoke("task", "complete", "fix")
        self.assertEqual(code, 0, report)
        self.assertEqual((self.root / "new-content.txt").read_text(), "Latest task content\n")
        _, queue = self.invoke("next")
        self.assertEqual(queue["selected"]["id"], "dependent")
        code, report = self.invoke("task", "complete", "fix")
        self.assertEqual(code, 0, report)

    def test_local_completion_rejects_changed_requirements_and_failed_checks(self):
        self.configure()
        self.add_task()
        _, task = self.invoke("start", "fix")
        worktree = Path(task["worktree"])
        (worktree / "value.txt").write_text("fixed\n")
        self.invoke("check", "fix")
        _, delivered = self.invoke("submit", "fix", "--local")
        self.git("merge", "--ff-only", delivered["branch"])
        saved = self.state.get("fix")
        saved["body"] += "\nUpdated acceptance requirements"
        self.state.save(saved)
        code, report = self.invoke("task", "complete", "fix")
        self.assertEqual(code, 1, report)
        self.assertIn("Latest verified content", report["error"])
        (worktree / "value.txt").write_text("broken\n")
        code, report = self.invoke("check", "fix")
        self.assertEqual(code, 1, report)
        code, report = self.invoke("task", "complete", "fix")
        self.assertEqual(code, 1, report)
        self.assertNotEqual(self.state.get("fix")["status"], "completed")
