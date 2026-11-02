from pathlib import Path
import subprocess
import os
from unittest.mock import patch

from helpers import RepositoryTest
from devrelay.git import _evidence_git, _submodule_content_matches_head, git, is_ancestor


class RawBlobEvidenceTests(RepositoryTest):
    def test_binary_stream_and_raw_symlink_match_git_objects(self):
        (self.root / "binary.dat").write_bytes(bytes(range(256)) * 9000)
        (self.root / "link.txt").symlink_to("value.txt\n")
        executable = self.root / "executable"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "blob fixtures")
        self.assertTrue(_submodule_content_matches_head(self.root))
        (self.root / "binary.dat").write_bytes(b"changed")
        self.assertFalse(_submodule_content_matches_head(self.root))

    def test_process_count_is_constant_with_many_files(self):
        for index in range(64):
            (self.root / f"fixture-{index}.txt").write_text(str(index))
        self.git("add", ".")
        self.git("commit", "-qm", "many files")
        with patch("devrelay.git._evidence_git", wraps=_evidence_git) as calls:
            self.assertTrue(_submodule_content_matches_head(self.root))
        self.assertLessEqual(calls.call_count, 3)
        self.assertFalse(any("hash-object" in call.args for call in calls.call_args_list))

    def test_sha256_repository_uses_its_native_blob_format(self):
        root = Path(self.temporary.name) / "sha256"
        result = subprocess.run(["git", "init", "-q", "--object-format=sha256", str(root)], capture_output=True)
        if result.returncode:
            self.skipTest("Installed Git does not support SHA-256 repositories")
        self.git("config", "user.name", "DevRelay Test", cwd=root)
        self.git("config", "user.email", "devrelay@example.invalid", cwd=root)
        (root / "value.dat").write_bytes(b"native\0blob\xff")
        (root / "link").symlink_to("value.dat ")
        self.git("add", ".", cwd=root)
        self.git("commit", "-qm", "sha256 blob", cwd=root)
        self.assertTrue(_submodule_content_matches_head(root))
        (root / "value.dat").write_bytes(b"different")
        self.assertFalse(_submodule_content_matches_head(root))

    def test_regular_file_replaced_by_symlink_is_not_a_matching_blob(self):
        (self.root / "copy.txt").write_bytes((self.root / "value.txt").read_bytes())
        (self.root / "value.txt").unlink()
        (self.root / "value.txt").symlink_to("copy.txt")
        self.assertFalse(_submodule_content_matches_head(self.root))

    def test_raw_changes_are_not_masked_by_clean_filter(self):
        (self.root / ".git/info/attributes").write_text("value.txt filter=mask\n")
        self.git("config", "filter.mask.clean", "printf 'broken\\n'")
        (self.root / "value.txt").write_text("different\n")
        self.assertEqual(self.git("hash-object", "--path=value.txt", "value.txt"), self.git("rev-parse", "HEAD:value.txt"))
        self.assertFalse(_submodule_content_matches_head(self.root))


class ActualHistoryTests(RepositoryTest):
    def test_replacement_cannot_forge_ancestry_and_is_preserved(self):
        original = self.git("rev-parse", "HEAD")
        (self.root / "required-base.txt").write_text("required\n")
        self.git("add", ".")
        self.git("commit", "-qm", "new base")
        base = self.git("rev-parse", "HEAD")
        tree = self.git("rev-parse", original + "^{tree}")
        replacement = subprocess.check_output(
            ["git", "commit-tree", tree, "-p", base], cwd=self.root,
            input="replacement\n", text=True).strip()
        self.git("replace", original, replacement)
        self.assertEqual(subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, original],
            cwd=self.root, env={key: value for key, value in os.environ.items() if key != "GIT_NO_REPLACE_OBJECTS"}).returncode, 0)
        self.assertFalse(is_ancestor(self.root, base, original))
        self.assertEqual(git(self.root, "rev-list", "--parents", "-n", "1", original), original)
        self.assertEqual(self.git("rev-parse", "refs/replace/" + original), replacement)

    def test_commit_tree_queries_ignore_replacement_trees(self):
        original = self.git("rev-parse", "HEAD")
        original_tree = self.git("rev-parse", "HEAD^{tree}")
        (self.root / "value.txt").write_text("replacement\n")
        self.git("commit", "-qam", "replacement")
        replacement = self.git("rev-parse", "HEAD")
        self.git("replace", original, replacement)
        self.assertNotEqual(self.git("rev-parse", original + "^{tree}"), original_tree)
        self.assertEqual(git(self.root, "rev-parse", original + "^{tree}"), original_tree)
        self.assertEqual(git(self.root, "show", original + ":value.txt"), "broken")
        self.assertEqual(self.git("rev-parse", "refs/replace/" + original), replacement)
