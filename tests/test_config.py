import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from devrelay.config import detect, initialize, load
from devrelay.errors import RelayError
from devrelay.git import git, github_slug, pinned_transport, remote_tracking_ref, validate_remote

from helpers import RepositoryTest


class ConfigurationTests(RepositoryTest):
    def test_receipts_only_expire_when_their_stage_inputs_change(self):
        self.configure()
        original = self.config
        path = self.root / ".devrelay/config.toml"
        source = path.read_text()
        path.write_text(source.replace("max_attempts = 2", "max_attempts = 9"))
        changed = load(self.root)
        for stage in ("setup", "checks", "review"):
            self.assertEqual(original.stage_fingerprint(stage), changed.stage_fingerprint(stage))
        path.write_text(source.replace("[codex]", '[codex]\nmodel = "different-model"'))
        changed = load(self.root)
        for stage in ("setup", "checks"):
            self.assertEqual(original.stage_fingerprint(stage), changed.stage_fingerprint(stage))
        self.assertNotEqual(original.stage_fingerprint("review"), changed.stage_fingerprint("review"))
        path.write_text(source.replace('id = "checks-0"', 'id = "different-check"'))
        changed = load(self.root)
        self.assertEqual(original.stage_fingerprint("setup"), changed.stage_fingerprint("setup"))
        self.assertNotEqual(original.stage_fingerprint("checks"), changed.stage_fingerprint("checks"))
        self.assertFalse(changed.matches_evidence({"config_sha256": original.fingerprint}, "checks"))

    def test_init_preserves_instructions_and_ignore_rules(self):
        (self.root / "AGENTS.md").write_text("Existing project rules")
        before = (self.root / ".gitignore").read_text()
        result = initialize(self.root, "local")
        self.assertEqual(result["provider"], "local")
        self.assertEqual((self.root / "AGENTS.md").read_text(), "Existing project rules")
        self.assertTrue((self.root / ".gitignore").read_text().startswith(before))
        self.assertEqual(load(self.root).base, "main")
        self.assertEqual(load(self.root).data["queue"]["allowed_authors"], [])
        self.assertEqual(load(self.root).data["discovery"], {"enabled": True, "auto_triage": True,
                                                         "auto_review": True, "max_per_task": 3})
        self.assertEqual(load(self.root).data["delivery"], {"auto_review": False, "auto_merge": False,
                                                        "allow_no_ci": False, "required_checks": [],
                                                        "cleanup_branches": False, "cleanup_worktrees": False,
                                                        "post_merge_checks": True})
        original = (self.root / ".devrelay/config.toml").read_text()
        with self.assertRaisesRegex(RelayError, "already exists"):
            initialize(self.root)
        self.assertEqual((self.root / ".devrelay/config.toml").read_text(), original)

    def test_node_commands_follow_lockfile_and_existing_scripts(self):
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}))
        (self.root / "pnpm-lock.yaml").write_text("lockfileVersion: 9")
        setup, checks, _ = detect(self.root)
        self.assertEqual(setup[0]["argv"], ["pnpm", "install", "--frozen-lockfile"])
        self.assertEqual([item["id"] for item in checks], ["test", "build"])

    def test_python_runner_is_not_guessed(self):
        (self.root / "pyproject.toml").write_text('[project]\nname="sample"\nversion="0.1.0"\n')
        (self.root / "tests").mkdir()
        setup, checks, notes = detect(self.root)
        self.assertEqual(setup[0]["argv"], ["python3", "-m", "venv", ".venv"])
        self.assertEqual(checks, [])
        self.assertTrue(any("actual runner" in note for note in notes))

    def test_github_ssh_and_https_detection(self):
        for remote in ("https://github.com/owner/repo.git", "git@github.com:owner/repo.git",
                       "ssh://git@github.com/owner/repo"):
            self.assertEqual(github_slug(remote), "owner/repo")
        self.assertEqual(github_slug("https://github.com.evil.test/owner/repo"), "")

    def test_detect_github_without_network(self):
        self.git("remote", "add", "origin", "git@github.com:example/project.git")
        initialize(self.root)
        config = load(self.root)
        self.assertEqual(config.provider, "github")
        self.assertEqual(config.repo, "example/project")

    def test_invalid_command_and_limits_fail_before_execution(self):
        self.configure()
        path = self.root / ".devrelay/config.toml"
        original = path.read_text()
        for change in (original.replace("max_attempts = 2", "max_attempts = 0"),
                       original.replace("timeout_seconds = 10", "timeout_seconds = true"),
                       original.replace("argv = [", "argv = 'shell command' # [")):
            with self.subTest(config=change):
                path.write_text(change)
                with self.assertRaises(RelayError):
                    load(self.root)

    def test_unknown_configuration_fields_are_rejected(self):
        self.configure()
        path = self.root / ".devrelay/config.toml"
        path.write_text(path.read_text().replace("max_attempts = 2", "max_attempt = 2"))
        with self.assertRaisesRegex(RelayError, "Unknown"):
            load(self.root)

    def test_author_allowlist_requires_unique_github_logins(self):
        self.configure()
        path = self.root / ".devrelay/config.toml"
        original = path.read_text()
        for value in ('"maintainer"', 'true', '[1]', '[""]', '[" user"]',
                      '["user name"]', '["@user"]', '["user", "USER"]'):
            with self.subTest(value=value):
                path.write_text(original + "\n[queue]\nallowed_authors = " + value + "\n")
                with self.assertRaisesRegex(RelayError, "queue.allowed_authors"):
                    load(self.root)
        path.write_text(original + '\n[queue]\nallowed_authors = ["Maintainer", "autodev[bot]"]\n')
        self.assertEqual(load(self.root).data["queue"]["allowed_authors"], ["Maintainer", "autodev[bot]"])

    def test_symlinked_ignore_file_is_not_modified(self):
        original = self.root / ".gitignore"
        original.unlink()
        outside = self.root.parent / "outside-ignore"
        outside.write_text("preserve me")
        original.symlink_to(outside)
        with self.assertRaisesRegex(RelayError, "symlink"):
            initialize(self.root)
        self.assertEqual(outside.read_text(), "preserve me")
        self.assertFalse((self.root / ".devrelay").exists())

    def test_python_generated_artifacts_are_ignored(self):
        (self.root / "pyproject.toml").write_text('[project]\nname="sample"\nversion="0.1.0"\n')
        initialize(self.root, "local")
        for name in (".venv/lib.py", "sample.egg-info/PKG-INFO", "src/__pycache__/cached.pyc"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runtime")
            self.assertEqual(self.git("check-ignore", name), name)

    def test_push_remote_must_match_the_configured_repository(self):
        self.configure(provider="github")
        self.git("remote", "add", "origin", "https://github.com/example/project.git")
        self.assertEqual(validate_remote(self.config), "https://github.com/example/project.git")
        self.git("remote", "set-url", "--push", "origin", "https://github.com/example/wrong-project.git")
        with self.assertRaisesRegex(RelayError, "Git push URL does not match project.repository"):
            validate_remote(self.config)
        self.git("remote", "set-url", "--push", "origin", "https://github.com/example/project.git")
        self.git("remote", "set-url", "origin", "https://github.com/example/wrong-project.git")
        with self.assertRaisesRegex(RelayError, "Git fetch URL does not match project.repository"):
            validate_remote(self.config)

    def test_remote_rejects_multiple_fetch_or_push_urls(self):
        self.configure(provider="github")
        self.git("remote", "add", "origin", "git@github.com:example/project.git")
        self.git("remote", "set-url", "--add", "--push", "origin",
                 "https://github.com/example/project.git")
        self.git("remote", "set-url", "--add", "--push", "origin",
                 "git@github.com:example/project.git")
        with self.assertRaisesRegex(RelayError, "exactly one.*push fan-out"):
            validate_remote(self.config)
        self.git("remote", "set-url", "--delete", "--push", "origin",
                 "git@github.com:example/project.git")
        self.git("remote", "set-url", "--add", "origin",
                 "https://github.com/example/project.git")
        with self.assertRaisesRegex(RelayError, "exactly one"):
            validate_remote(self.config)

    def test_remote_validates_resolved_url_rewrites(self):
        self.configure(provider="github")
        original = "https://github.com/example/project.git"
        self.git("remote", "add", "origin", original)
        self.git("config", "url.https://github.com/example/other.git.insteadOf", original)
        with self.assertRaisesRegex(RelayError, "URL rewrite"):
            validate_remote(self.config)

    def test_transport_preserves_system_settings_when_nosystem_is_false(self):
        system = Path(self.temporary.name) / "system-false.gitconfig"
        self.git("config", "--file", str(system), "devrelay.probe", "preserved")
        for value in ("0", "false", "no", "off", "FALSE", "", "00"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "GIT_CONFIG_NOSYSTEM": value, "GIT_CONFIG_SYSTEM": str(system),
                "GIT_CONFIG_GLOBAL": os.devnull,
            }):
                self.assertEqual(git(self.root, "config", "--get", "devrelay.probe"), "preserved")
                with pinned_transport(self.root, "https://github.com/example/project.git") as (_, env):
                    self.assertEqual(git(self.root, "config", "--get", "devrelay.probe", env=env),
                                     "preserved")

    def test_transport_keeps_system_settings_disabled_when_nosystem_is_true(self):
        system = Path(self.temporary.name) / "system-true.gitconfig"
        self.git("config", "--file", str(system), "devrelay.probe", "disabled")
        for value in ("1", "true", "yes", "on", "TRUE", "2", "-1"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "GIT_CONFIG_NOSYSTEM": value, "GIT_CONFIG_SYSTEM": str(system),
                "GIT_CONFIG_GLOBAL": os.devnull,
            }):
                with pinned_transport(self.root, "https://github.com/example/project.git") as (_, env):
                    for actual_env in (os.environ, env):
                        result = subprocess.run(
                            ["git", "config", "--get", "devrelay.probe"], cwd=self.root,
                            env=actual_env, text=True, capture_output=True, timeout=60,
                        )
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stdout, "")

    def test_transport_rejects_invalid_nosystem_boolean_before_yielding(self):
        for value in ("invalid", "truthy", "0.0"):
            with self.subTest(value=value), patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": value}):
                with self.assertRaisesRegex(RelayError, "GIT_CONFIG_NOSYSTEM.*Git boolean"):
                    with pinned_transport(self.root, "https://github.com/example/project.git"):
                        self.fail("Invalid system configuration policy must stop the transport")

    def test_transport_pin_wins_over_a_later_url_rewrite(self):
        allowed = Path(self.temporary.name) / "pin-allowed.git"
        unapproved = Path(self.temporary.name) / "pin-unapproved.git"
        for remote in (allowed, unapproved):
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self.git("config", f"url.{unapproved}.pushInsteadOf", str(allowed))
        with pinned_transport(self.root, str(allowed)) as (target, env):
            git(self.root, "push", target, "refs/heads/main:refs/heads/pinned", env=env)
        self.assertEqual(self.git("--git-dir", str(allowed), "rev-parse", "refs/heads/pinned"),
                         self.git("rev-parse", "main"))
        refs = self.git("--git-dir", str(unapproved), "for-each-ref", "--format=%(refname)")
        self.assertNotIn("refs/heads/pinned", refs)

    def test_transport_pin_ignores_url_named_alias_added_before_each_transport(self):
        allowed = Path(self.temporary.name) / "alias-allowed.git"
        unapproved = [Path(self.temporary.name) / f"alias-extra-{index}.git" for index in range(2)]
        for remote in (allowed, *unapproved):
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        url = allowed.as_uri()
        ref = "refs/heads/alias-probe"
        head = self.git("rev-parse", "main")
        with pinned_transport(self.root, url) as (target, env):
            # Mutate after the pin is prepared too: neither the validated URL
            # nor the actual argument may become a fan-out remote alias.
            for alias in (url, target):
                self.git("config", f"remote.{alias}.url", str(unapproved[0]))
                for remote in unapproved:
                    self.git("config", "--add", f"remote.{alias}.pushurl", str(remote))
                self.git("config", "--add", f"url.{unapproved[0]}.insteadOf", alias)
                self.git("config", "--add", f"url.{unapproved[0]}.pushInsteadOf", alias)
            git(self.root, "push", target, "refs/heads/main:" + ref, env=env)
            self.assertEqual(git(self.root, "ls-remote", target, ref, env=env).split(), [head, ref])
            for remote in unapproved:
                self.assertEqual(self.git("--git-dir", str(remote), "for-each-ref"), "")
                self.git("push", str(remote), "refs/heads/main:" + ref)
            # Deletion and readback must use the same bound endpoint, never
            # the URL alias's first fetch URL or its two push URLs.
            git(self.root, "push", "--force-with-lease=" + ref + ":" + head,
                target, ":" + ref, env=env)
            self.assertEqual(git(self.root, "ls-remote", target, ref, env=env), "")
        for remote in unapproved:
            self.assertEqual(self.git("--git-dir", str(remote), "rev-parse", ref), head)

    def test_tracking_ref_follows_fetch_mapping_and_actual_upstream_sha(self):
        remote = Path(self.temporary.name) / "tracking.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("config", "--replace-all", "remote.origin.fetch",
                 "+refs/heads/main:refs/remotes/origin/main")
        self.git("config", "--add", "remote.origin.fetch",
                 "+refs/heads/devrelay/*:refs/remotes/custom/*")
        self.git("config", "--add", "remote.origin.fetch", "^refs/heads/devrelay/*")
        branch = "devrelay/task-tracking"
        original = self.git("rev-parse", "main")
        self.git("checkout", "-qb", branch)
        self.git("commit", "--allow-empty", "-qm", "tracking update")
        head = self.git("rev-parse", branch)
        self.git("config", f"branch.{branch}.remote", "origin")
        self.git("config", f"branch.{branch}.merge", "refs/heads/" + branch)
        # Establish the oracle with a real named-remote push, not the helper:
        # the negative fetch rule does not suppress Git's tracking update.
        self.git("push", "origin", branch)
        self.assertEqual(self.git("rev-parse", branch + "@{upstream}"), head)
        self.assertEqual(self.git("rev-parse", "refs/remotes/custom/task-tracking"), head)
        tracking = remote_tracking_ref(self.root, "origin", branch)
        self.assertEqual(tracking, "refs/remotes/custom/task-tracking")
        self.git("update-ref", tracking, original)
        with pinned_transport(self.root, str(remote)) as (target, env):
            git(self.root, "push", target, f"refs/heads/{branch}:refs/heads/{branch}", env=env)
        self.git("update-ref", tracking, head)
        self.assertEqual(self.git("--git-dir", str(remote), "rev-parse", "refs/heads/" + branch), head)
        self.assertEqual(self.git("rev-parse", branch + "@{upstream}"), head)
        self.assertIsNone(remote_tracking_ref(self.root, "origin", "unmapped"))

    def test_remote_validation_uses_the_transport_worktree_context(self):
        self.configure(provider="github")
        allowed = "https://github.com/example/project.git"
        unapproved = "https://github.com/other/unapproved.git"
        self.git("remote", "add", "origin", allowed)
        worktree = Path(self.temporary.name) / "task-context"
        self.git("worktree", "add", "-q", "-b", "devrelay/task-context", str(worktree), "main")
        included = Path(self.temporary.name) / "task-context.gitconfig"
        self.git("config", "--file", str(included), f"url.{unapproved}.insteadOf", allowed)
        self.git("config", "includeIf.onbranch:devrelay/task-*.path", str(included))

        self.assertEqual(validate_remote(self.config), allowed)
        self.assertEqual(self.git("ls-remote", "--get-url", allowed, cwd=worktree), unapproved)
        with self.assertRaisesRegex(RelayError, "URL rewrite"):
            validate_remote(self.config, worktree)

    def test_fetch_and_push_protocols_may_differ_for_the_same_repository(self):
        self.configure(provider="github")
        self.git("remote", "add", "origin", "git@github.com:example/project.git")
        self.git("remote", "set-url", "--push", "origin",
                 "https://github.com/example/project.git")
        self.assertEqual(validate_remote(self.config), "https://github.com/example/project.git")

    def test_delivery_configuration_is_strict_and_github_only(self):
        self.configure()
        path = self.root / ".devrelay/config.toml"
        original = path.read_text()
        for policy in ('auto_merge = "true"', 'auto_review = 1', 'allow_no_ci = "yes"',
                       'required_checks = ["test", "test"]', 'required_checks = [""]',
                       'required_checks = "test"', 'auto_merg = true', 'auto_merge = true',
                       'cleanup_worktrees = "true"', 'cleanup_branches = true',
                       'cleanup_worktrees = true', 'cleanup_worktrees = true\ncleanup_branches = true'):
            with self.subTest(policy=policy):
                path.write_text(original + "\n[delivery]\n" + policy + "\n")
                with self.assertRaises(RelayError):
                    load(self.root)


if __name__ == "__main__":
    unittest.main()
