import http.client
import json
import os
from pathlib import Path
import shutil
import threading
import tomllib
import unittest
from unittest.mock import patch

from devrelay.cli import main, parser
from devrelay.config import load
from devrelay.errors import RelayError
from devrelay.ui import DashboardServer, Project, projects
from devrelay.ui_document import update

from helpers import RepositoryTest


class DocumentTests(unittest.TestCase):
    def test_scalar_edit_preserves_comments_multiline_strings_and_unrelated_bytes(self):
        source = '# intro\n[queue]\nscope = """line one\n# still a string\nline two""" # scope note\nmode = "manual" # policy\n\n[codex]\nmax_attempts = 2\n'
        result = update(source, {"queue.scope": "bounded # scope", "queue.mode": "autonomous"})
        self.assertIn('scope = "bounded # scope" # scope note', result)
        self.assertIn('mode = "autonomous" # policy', result)
        self.assertTrue(result.startswith("# intro\n"))
        self.assertTrue(result.endswith("\n[codex]\nmax_attempts = 2\n"))

    def test_add_remove_fields_and_commands_round_trip(self):
        source = '[codex]\nmodel = "example"\n[queue]\nmode = "manual"\n\n[[checks]]\nid = "old"\nargv = ["old"]\n'
        changes = {"codex.model": None, "queue.allowed_authors": ["maintainer"],
                   "delivery.auto_review": True, "checks": [],
                   "setup": [{"id": "install", "argv": ["npm", "ci"], "timeout_seconds": 900}]}
        result = tomllib.loads(update(source, changes))
        self.assertEqual(result["codex"], {})
        self.assertEqual(result["checks"], [])
        self.assertEqual(result["setup"], changes["setup"])
        self.assertEqual(tomllib.loads(update(update(source, changes), {"checks": [{"id": "new", "argv": ["test"]}]}))["checks"][0]["id"], "new")

    def test_unusual_layout_fails_closed_without_rewriting(self):
        with self.assertRaisesRegex(ValueError, "source editor"):
            update('queue = {mode = "manual"}\n', {"queue.mode": "autonomous"})


class DashboardTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure()
        self.project = projects(self.root)[0]
        self.path = self.root / ".devrelay/config.toml"

    def inspect(self):
        return self.project.call("inspect")

    def preview(self, **changes):
        current = self.inspect()
        return self.project.call("preview", revision=current["revision"], changes=changes)

    def save(self, preview):
        return self.project.call("save", revision=preview["revision"], source=preview["source"],
                                 preview_sha256=preview["preview_sha256"])

    def test_save_uses_validation_and_preserves_original_index_and_evidence(self):
        original = self.path.read_bytes()
        index = self.git("write-tree")
        self.state.save({"id": "1", "status": "verified", "verification": {"passed": True, "source_tree": "original"}})
        task = self.state.get("1")
        preview = self.preview(**{"codex.max_attempts": 4})
        self.assertTrue(preview["changed"])
        self.assertEqual(preview["affected_stages"], [])
        self.assertEqual(self.path.read_bytes(), original)
        result = self.save(preview)
        self.assertTrue(result["saved"])
        self.assertEqual(Path(result["backup"]).read_bytes(), original)
        self.assertEqual(Path(result["backup"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(load(self.root).data["codex"]["max_attempts"], 4)
        self.assertEqual(self.git("write-tree"), index)
        self.assertEqual(self.state.get("1"), task)

    def test_invalid_values_and_missing_preview_never_write(self):
        original = self.path.read_bytes()
        current = self.inspect()
        for changes in ({"codex.max_attempts": 0}, {"checks": [{"id": "shell", "argv": "echo unsafe"}]},
                        {"queue.unknown": True}, {"queue.allowed_authors": ["same", "SAME"]}):
            with self.subTest(changes=changes), self.assertRaises(RelayError):
                self.project.call("preview", revision=current["revision"], changes=changes)
        with self.assertRaisesRegex(RelayError, "Preview"):
            self.project.call("save", revision=current["revision"], changes={"codex.max_attempts": 3})
        self.assertEqual(self.path.read_bytes(), original)

    def test_stale_preview_and_runtime_changes_never_overwrite(self):
        preview = self.preview(**{"codex.max_attempts": 3})
        self.path.write_text(self.path.read_text() + "\n# another editor\n")
        with self.assertRaisesRegex(RelayError, "changed"):
            self.save(preview)
        self.assertIn("# another editor", self.path.read_text())
        self.assertEqual(load(self.root).data["codex"]["max_attempts"], 2)
        runtime = Path(self.temporary.name) / "runtime"
        shutil.copytree(self.project.runtime / "devrelay", runtime / "devrelay", ignore=shutil.ignore_patterns("__pycache__"))
        self.project = Project("other", "Other", self.root, runtime)
        preview = self.preview(**{"codex.max_attempts": 3})
        module = runtime / "devrelay/config.py"
        module.write_text(module.read_text() + "\n# runtime updated\n")
        with self.assertRaisesRegex(RelayError, "changed"):
            self.save(preview)

    def test_worker_lock_reservation_and_active_process_block_save(self):
        preview = self.preview(**{"codex.max_attempts": 3})
        with self.state.lock():
            self.assertTrue(self.inspect()["activity"]["busy"])
            with self.assertRaisesRegex(RelayError, "lock"):
                self.save(preview)
        self.state.set_metadata("workspace_owner", "another-workspace")
        self.assertTrue(self.inspect()["activity"]["busy"])
        with self.assertRaisesRegex(RelayError, "reserved"):
            self.save(preview)
        self.state.set_metadata("workspace_owner", None)
        active = {"group": os.getpgrp()}
        self.state.set_metadata("active_process", active)
        self.assertTrue(self.inspect()["activity"]["busy"])
        with self.assertRaisesRegex(RelayError, "still runs"):
            self.save(preview)
        self.assertEqual(self.state.metadata("active_process"), active)

    def test_source_repair_and_evidence_impact(self):
        self.path.write_text("[broken TOML")
        current = self.inspect()
        self.assertTrue(current["errors"])
        preview = self.project.call("preview", revision=current["revision"], source='[queue]\nmode = "manual"\n')
        self.assertEqual(set(preview["affected_stages"]), {"setup", "checks", "review", "delivery"})
        self.save(preview)
        preview = self.preview(**{"queue.mode": "autonomous"})
        self.assertEqual(preview["affected_stages"], ["delivery"])

    def test_form_edits_can_follow_source_edits_without_losing_them(self):
        current = self.inspect()
        result = self.project.call("preview", revision=current["revision"],
                                   source=current["source"] + "\n# source note\n", changes={"codex.max_attempts": 7})
        self.assertIn("# source note", result["source"])
        self.assertEqual(result["data"]["codex"]["max_attempts"], 7)

    def test_selected_runtime_controls_supported_fields_and_validation(self):
        runtime = Path(self.temporary.name) / "legacy"
        shutil.copytree(self.project.runtime / "devrelay", runtime / "devrelay", ignore=shutil.ignore_patterns("__pycache__"))
        module = runtime / "devrelay/config.py"
        source = module.read_text().replace('"approval_label", "allowed_authors",', '"approval_label",')
        start, end = source.index('    queue.setdefault("allowed_authors"'), source.index('    queue.setdefault("mode"')
        module.write_text(source[:start] + source[end:])
        self.project = Project("old", "Old", self.root, runtime)
        current = self.inspect()
        self.assertNotIn("queue.allowed_authors", current["supported"])
        with self.assertRaisesRegex(RelayError, "Unknown queue"):
            self.project.call("preview", revision=current["revision"], changes={"queue.allowed_authors": ["user"]})

    def test_controller_routing_and_symlink_rejection(self):
        checkout = Path(self.temporary.name) / "controller"
        self.git("worktree", "add", "-b", "controller", str(checkout))
        self.state.set_metadata("controller_root", str(checkout))
        self.assertEqual(self.inspect()["root"], str(checkout))
        result = self.save(self.preview(**{"codex.max_attempts": 3}))
        self.assertTrue(result["saved"])
        self.assertEqual(load(checkout).data["codex"]["max_attempts"], 3)
        self.assertEqual(load(self.root).data["codex"]["max_attempts"], 2)
        controller_path = checkout / ".devrelay/config.toml"
        controller_path.unlink()
        controller_path.symlink_to(self.path)
        with self.assertRaisesRegex(RelayError, "symlink"):
            self.inspect()

    def test_catalog_relative_paths_and_duplicate_ids(self):
        catalog = Path(self.temporary.name) / "projects.toml"
        catalog.write_text('[[projects]]\nid="demo"\nname="Demo"\npath="project"\n')
        self.assertEqual(projects(self.root, catalog)[0].root, self.root.resolve())
        catalog.write_text(catalog.read_text() * 2)
        with self.assertRaisesRegex(RelayError, "unique"):
            projects(self.root, catalog)

    def test_http_same_origin_round_trip_and_attack_rejection(self):
        server = DashboardServer([self.project])
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        def request(path, *, method="GET", payload=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                conn.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers or {})
                response = conn.getresponse()
                return response.status, response.read(), dict(response.getheaders())
            finally:
                conn.close()

        status, body, headers = request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"DevRelay", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for asset in ("/app.js", "/style.css"):
            self.assertEqual(request(asset)[0], 200)
        status, body, _ = request("/api/session")
        token = json.loads(body)["token"]
        trusted = {"Content-Type": "application/json", "Origin": server.origin, "X-DevRelay-Token": token}
        endpoint = "/api/projects/project/inspect"
        self.assertEqual(request(endpoint, method="POST", payload={}, headers=trusted)[0], 200)
        for changes in ({"Origin": "https://evil.example"}, {"X-DevRelay-Token": "wrong"},
                        {"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(request(endpoint, method="POST", payload={}, headers={**trusted, **changes})[0], 403)
        self.assertEqual(request("/api/session", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(request("/../ui.py")[0], 404)
        self.assertEqual(request("/api/projects/unknown/save", method="POST", payload={}, headers=trusted)[0], 404)
        self.assertEqual(request(endpoint, method="POST", payload={"root": "/tmp"}, headers=trusted)[0], 400)
        current = json.loads(request(endpoint, method="POST", payload={}, headers=trusted)[1])
        preview = json.loads(request("/api/projects/project/preview", method="POST", payload={"revision": current["revision"], "changes": {"codex.max_attempts": 6}}, headers=trusted)[1])
        status, body, _ = request("/api/projects/project/save", method="POST", payload={key: preview[key] for key in ("revision", "source", "preview_sha256")}, headers=trusted)
        self.assertEqual(status, 200, body)
        self.assertEqual(load(self.root).data["codex"]["max_attempts"], 6)

    def test_ui_cli_routes_before_loading_current_project(self):
        args = parser().parse_args(["ui", "--port", "0", "--open"])
        self.assertEqual(args.port, 0)
        self.assertTrue(args.open)
        with patch("devrelay.ui.serve", return_value=0) as serve, patch("devrelay.cli.State") as state:
            self.assertEqual(main(["ui", "--catalog", "/tmp/catalog.toml"]), 0)
            serve.assert_called_once()
            state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
