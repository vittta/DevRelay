from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from unittest.mock import patch

from devrelay.cli import main
from devrelay.providers import add_local

from helpers import RepositoryTest


class CliExitCodeTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure(executable=self.fake_codex())

    def invoke(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--project", str(self.root), *arguments])
        self.assertEqual(err.getvalue(), "")
        return code, json.loads(out.getvalue())

    def test_blocked_run_and_following_attention_are_unsuccessful(self):
        add_local(self.config, self.state, "fix", "Fix value", "Depends on: none", "priority:P1")
        with patch.dict(os.environ, DEVRELAY_TEST_MODE="blocked"):
            code, result = self.invoke("run", "fix")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(code, 1)
        self.assertEqual(self.state.get("fix")["status"], "blocked")
        code, result = self.invoke("run")
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(code, 1)

    def test_idle_run_returns_success(self):
        code, result = self.invoke("run")
        self.assertEqual(result["status"], "idle")
        self.assertEqual(code, 0)

    def test_single_repository_workspace_and_watch_share_result_exit_codes(self):
        results = [({"status": status}, 1) for status in
                   ("failed", "blocked", "needs_attention", "start_failed", "review_blocked",
                    "merge_blocked", "post_merge_failed", "cleanup_failed")]
        results += [({"ready": False}, 1),
                    ({"status": "merge_waiting", "merge_gates": {"blockers": ["CI failed"]}}, 1),
                    ({"status": "merge_waiting", "merge_gates": {"blockers": [], "reasons": ["CI pending"]}}, 0)]
        results += [({"status": status}, 0) for status in
                    ("idle", "verified", "committed", "pr_open", "reviewed", "merge_waiting", "completed", "delivered", "stopped")]
        for result, expected in results:
            routes = [("devrelay.cli.run_task", ["--project", str(self.root), "run", "fix"]),
                      ("devrelay.cli.watch", ["--project", str(self.root), "watch"]),
                      ("devrelay.workspace.dispatch", ["--workspace", str(self.root / "workspace.toml"), "run", "fix"])]
            for target, arguments in routes:
                with self.subTest(result=result, target=target), patch(target, return_value=result):
                    with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                        self.assertEqual(main(arguments), expected)
                    self.assertEqual(json.loads(out.getvalue()), result)
                    self.assertEqual(err.getvalue(), "")
