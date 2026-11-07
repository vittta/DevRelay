from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from devrelay.cli import main
from devrelay.config import load
from devrelay.errors import RelayError
from devrelay.onboarding import setup_github, templates
from devrelay.providers import Snapshot, queue_report

from helpers import RepositoryTest


class GitHubOnboardingTests(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.configure(provider="github")
        self.git("remote", "add", "origin", "https://github.com/example/project.git")
        self.labels = {"bug": {"name": "bug", "color": "123456", "description": "Keep our definition"}}
        self.requests = []
        self.failure = None
        self.timeout = None
        self.malformed = False
        mock = patch("devrelay.onboarding.command", side_effect=self.api)
        mock.start()
        self.addCleanup(mock.stop)

    def api(self, argv, cwd, *, input=None):
        self.assertEqual(argv[:4], ["gh", "api", "--hostname", "github.com"])
        self.assertEqual(Path(cwd).resolve(), self.root.resolve())
        method, endpoint = argv[5:7]
        target = urlsplit(endpoint)
        # No issue, workflow or repository-setting mutation is allowed by this fixture.
        self.assertEqual(target.path, "repos/example/project/labels")
        body = json.loads(input) if input else None
        self.requests.append((method, endpoint, body))
        if method == "GET":
            if self.malformed:
                return json.dumps({"message": "bad inventory"})
            page = int(parse_qs(target.query)["page"][0])
            return json.dumps(list(self.labels.values())[(page - 1) * 100:page * 100])
        self.assertEqual(method, "POST")
        name = body["name"]
        if name == self.failure:
            raise RelayError("GitHub denied label creation")
        self.labels[name] = dict(body)
        if name == self.timeout:
            self.timeout = None
            raise RelayError("Timed out after server created label")
        return json.dumps(body)

    def invoke(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--project", str(self.root), "init", *argv])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_adds_management_labels_and_safe_templates_without_touching_existing_definitions(self):
        original = dict(self.labels["bug"])
        result = setup_github(self.config)
        expected = {"bug", "enhancement", "documentation", "experiment", "infrastructure",
                    "auto:approved", "priority:P0",
                    "priority:P1", "priority:P2", "blocked", "needs-input", "scope:plan"}
        self.assertEqual(set(self.labels), expected)
        self.assertEqual(self.labels["bug"], original)
        self.assertEqual(result["labels"]["preserved"], ["bug"])
        self.assertEqual(len(result["labels"]["created"]), 11)
        self.assertEqual(len(result["templates"]["created"]), 4)
        for filename in result["templates"]["created"]:
            text = (self.root / filename).read_text()
            front, body = text.split("---\n", 2)[1:]
            label = json.loads(next(line.removeprefix("labels: ") for line in front.splitlines()
                                    if line.startswith("labels: ")))
            self.assertIn(label, ("bug", "enhancement", "experiment", "scope:plan"))
            self.assertNotIn("auto:approved", front)
            self.assertEqual(len(re.findall(r"^Depends on:", body, re.M)), 1)
            self.assertIn("Depends on: none", body)
            self.assertIn("One issue delivers one observable, reviewable result", body)
            self.assertIn("Split child issues only for independent value or acceptance criteria", body)
            task = {"id": "12", "title": "New task", "body": body, "labels": [label, "priority:P1"],
                    "open": True, "created_at": "2026-10-10"}
            report = queue_report(self.config, Snapshot([task], set(), set()))
            self.assertIsNone(report["selected"])
            self.assertIn("Missing auto:approved", report["excluded"][0]["reasons"])
            task["labels"].append("auto:approved")
            authorized = queue_report(self.config, Snapshot([task], set(), set()))
            if filename.endswith("devrelay-plan.md"):
                self.assertIsNone(authorized["selected"])
                self.assertIn("Tracking plan parents are not executable", str(authorized["excluded"]))
                # Removing only the label must not turn a tracking parent into work.
                task["labels"].remove("scope:plan")
                self.assertIsNone(queue_report(self.config, Snapshot([task], set(), set()))["selected"])
            else:
                self.assertEqual(authorized["selected"]["id"], "12")
        self.assertIn("tracking parent", (self.root / ".github/ISSUE_TEMPLATE/devrelay-plan.md").read_text())

    def test_repeat_sync_preserves_custom_templates_config_ignore_rules_and_labels(self):
        setup_github(self.config)
        path = self.root / ".github/ISSUE_TEMPLATE/devrelay-task.md"
        path.write_text("Maintainer's custom template\n")
        other = path.parent / "custom.yml"
        other.write_text("name: Existing form\n")
        before_config = (self.root / ".devrelay/config.toml").read_bytes()
        before_ignore = (self.root / ".gitignore").read_bytes()
        before_labels = json.dumps(self.labels, sort_keys=True)
        self.requests.clear()
        code, result = self.invoke("--sync-github")
        self.assertEqual(code, 0)
        self.assertEqual(result["github"]["labels"]["created"], [])
        self.assertEqual(len(result["github"]["templates"]["preserved"]), 4)
        self.assertEqual(path.read_text(), "Maintainer's custom template\n")
        self.assertEqual(other.read_text(), "name: Existing form\n")
        self.assertEqual((self.root / ".devrelay/config.toml").read_bytes(), before_config)
        self.assertEqual((self.root / ".gitignore").read_bytes(), before_ignore)
        self.assertEqual(json.dumps(self.labels, sort_keys=True), before_labels)
        self.assertTrue(all(method == "GET" for method, _, _ in self.requests))

    def test_first_init_automatically_sets_up_github(self):
        (self.root / ".devrelay/config.toml").unlink()
        code, result = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(result["provider"], "github")
        self.assertEqual(result["github"]["repository"], "example/project")
        self.assertEqual(len(result["github"]["templates"]["created"]), 4)

    def test_partial_remote_failure_is_reported_and_sync_recovers_without_overwriting_config(self):
        (self.root / ".devrelay/config.toml").unlink()
        self.failure = "scope:plan"
        code, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("init --sync-github", error["error"])
        original = (self.root / ".devrelay/config.toml").read_bytes()
        self.assertIn("enhancement", self.labels)
        self.assertNotIn("scope:plan", self.labels)
        self.failure = None
        code, result = self.invoke("--sync-github")
        self.assertEqual(code, 0)
        self.assertIn("enhancement", result["github"]["labels"]["preserved"])
        self.assertEqual((self.root / ".devrelay/config.toml").read_bytes(), original)

    def test_uncertain_create_response_reads_back_existing_label(self):
        self.timeout = "auto:approved"
        result = setup_github(self.config)
        self.assertIn("auto:approved", result["labels"]["preserved"])
        writes = [body for method, _, body in self.requests if method == "POST" and body["name"] == "auto:approved"]
        self.assertEqual(len(writes), 1)

    def test_partial_template_failure_leaves_only_complete_files_and_sync_recovers(self):
        count = 0
        link = os.link
        def fail_second(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Template publication interrupted")
            return link(source, destination)
        with patch("devrelay.onboarding.os.link", side_effect=fail_second):
            with self.assertRaisesRegex(RelayError, "init --sync-github"):
                setup_github(self.config)
        directory = self.root / ".github/ISSUE_TEMPLATE"
        self.assertEqual([path.name for path in directory.iterdir()], ["devrelay-bug.md"])
        complete = (directory / "devrelay-bug.md").read_bytes()
        self.assertEqual(complete.decode(), templates(self.config)["devrelay-bug.md"])
        result = setup_github(self.config)
        self.assertEqual(len(result["templates"]["created"]), 3)
        self.assertEqual((directory / "devrelay-bug.md").read_bytes(), complete)

    def test_label_inventory_is_paginated_and_existing_case_variant_type_is_preserved(self):
        self.labels = {f"custom-{index}": {"name": f"custom-{index}"} for index in range(100)}
        self.labels["Bug"] = {"name": "Bug", "color": "123456"}
        result = setup_github(self.config)
        self.assertIn("Bug", result["labels"]["preserved"])
        self.assertNotIn("bug", self.labels)
        self.assertTrue(any("page=2" in endpoint for _, endpoint, _ in self.requests))

    def test_invalid_inventory_blocks_remote_writes_and_template_creation(self):
        self.malformed = True
        with self.assertRaisesRegex(RelayError, "invalid label inventory"):
            setup_github(self.config)
        self.assertTrue(all(method == "GET" for method, _, _ in self.requests))
        self.assertFalse((self.root / ".github").exists())

    def test_symlinked_template_directory_is_preserved_before_remote_access(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        for relative in (".github", ".github/ISSUE_TEMPLATE"):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(exist_ok=True)
                path.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(RelayError, "symlink"):
                    setup_github(self.config)
                self.assertEqual(self.requests, [])
                self.assertEqual(list(outside.iterdir()), [])
                path.unlink()

    def test_existing_template_symlink_is_not_followed(self):
        directory = self.root / ".github/ISSUE_TEMPLATE"
        directory.mkdir(parents=True)
        outside = Path(self.temporary.name) / "outside-template"
        outside.write_text("Keep external content")
        (directory / "devrelay-task.md").symlink_to(outside)
        result = setup_github(self.config)
        self.assertIn(".github/ISSUE_TEMPLATE/devrelay-task.md", result["templates"]["preserved"])
        self.assertEqual(outside.read_text(), "Keep external content")

    def test_custom_queue_labels_are_used_without_automatic_authorization(self):
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text() + '\n[queue]\napproval_label = "enhancement"\n'
                        'priorities = ["bug", "urgent", "later"]\nblockers = ["experiment", "waiting"]\n')
        config = load(self.root)
        result = setup_github(config)
        self.assertIn("urgent", self.labels)
        self.assertIn("waiting", self.labels)
        self.assertNotIn("auto:approved", self.labels)
        self.assertEqual(len(result["templates"]["created"]), 4)
        for filename, text in templates(config).items():
            front = text.split("---\n", 2)[1]
            expected = 'labels: "scope:plan"' if filename == "devrelay-plan.md" else 'labels: ""'
            self.assertIn(expected, front)
            self.assertIn('Authorization / 执行授权: "enhancement"', text)

    def test_archived_or_differently_spelled_queue_labels_require_maintainer_resolution(self):
        for label in ({"name": "auto:approved", "archived_at": "2026-10-01"},
                      {"name": "Auto:Approved"}):
            with self.subTest(label=label):
                self.labels = {"auto": label}
                with self.assertRaisesRegex(RelayError, "archived|spelling differs"):
                    setup_github(self.config)
                self.assertFalse((self.root / ".github").exists())

    def test_remote_mismatch_blocks_all_github_requests(self):
        self.git("remote", "set-url", "--push", "origin", "https://github.com/example/other.git")
        with self.assertRaisesRegex(RelayError, "does not match"):
            setup_github(self.config)
        self.assertEqual(self.requests, [])

    def test_sync_rejects_conflicting_cli_options_without_remote_access(self):
        code, result = self.invoke("--sync-github", "--base", "main")
        self.assertEqual(code, 1)
        self.assertIn("omit --provider and --base", result["error"])
        self.assertEqual(self.requests, [])


class LocalOnboardingTests(RepositoryTest):
    def test_local_init_stays_offline_and_sync_rejects_local_provider(self):
        with patch("devrelay.onboarding.command") as api:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--project", str(self.root), "init", "--provider", "local"])
            self.assertEqual(code, 0)
            self.assertNotIn("github", json.loads(out.getvalue()))
            self.assertFalse((self.root / ".github").exists())
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["--project", str(self.root), "init", "--sync-github"])
            self.assertEqual(code, 1)
            self.assertIn("provider=github", err.getvalue())
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
