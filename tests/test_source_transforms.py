import os
from pathlib import Path

from helpers import RepositoryTest
from devrelay.errors import RelayError
from devrelay.git import source_tree
from devrelay.providers import add_local
from devrelay.runner import start, submit, verify


class SourceTransformTests(RepositoryTest):
    def install_filter(self, specification="value.txt", command="printf 'broken\\n'"):
        (self.root / ".git/info/attributes").write_text(specification + " filter=mask\n")
        self.git("config", "filter.mask.clean", command)
        self.git("config", "filter.mask.required", "true")

    def test_masking_clean_filter_blocks_verification_and_delivery(self):
        self.configure()
        add_local(self.config, self.state, "fix", "Fix value", "Depends on: none", "priority:P1")
        task = start(self.config, self.state, "fix")
        worktree = Path(task["worktree"])
        (worktree / "value.txt").write_text("fixed\n")
        (worktree / "extra.txt").write_text("implementation\n")
        self.install_filter()
        index = Path(self.git("rev-parse", "--git-path", "index", cwd=worktree))
        original = index.read_bytes()
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        with self.assertRaisesRegex(RelayError, "differs from deliverable Git blobs"):
            verify(self.config, self.state, "fix")
        with self.assertRaises(RelayError):
            submit(self.config, self.state, "fix", local=True)
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), head)
        self.assertEqual((worktree / "value.txt").read_bytes(), b"fixed\n")
        self.assertFalse(self.state.get("fix").get("verification"))

    def test_adding_masking_filter_after_verification_blocks_submission(self):
        self.configure()
        add_local(self.config, self.state, "fix", "Fix value", "Depends on: none", "priority:P1")
        task = start(self.config, self.state, "fix")
        worktree = Path(task["worktree"])
        (worktree / "value.txt").write_text("fixed\n")
        verify(self.config, self.state, "fix")
        self.install_filter()
        head = self.git("rev-parse", "HEAD", cwd=worktree)
        with self.assertRaisesRegex(RelayError, "differs from deliverable Git blobs"):
            submit(self.config, self.state, "fix", local=True)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), head)

    def test_identity_filter_still_allows_verified_delivery(self):
        self.configure()
        add_local(self.config, self.state, "fix", "Fix value", "Depends on: none", "priority:P1")
        task = start(self.config, self.state, "fix")
        worktree = Path(task["worktree"])
        (worktree / "value.txt").write_text("fixed\n")
        self.install_filter(command="cat")
        verify(self.config, self.state, "fix")
        result = submit(self.config, self.state, "fix", local=True)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(self.git("show", "HEAD:value.txt", cwd=worktree), "fixed")
        self.assertEqual((worktree / "value.txt").read_bytes(), b"fixed\n")

    def test_byte_changing_filter_on_new_file_is_rejected(self):
        (self.root / "new.txt").write_bytes(b"verified\n")
        self.install_filter("new.txt")
        index = (self.root / ".git/index").read_bytes()
        with self.assertRaisesRegex(RelayError, "differs from deliverable Git blobs"):
            source_tree(self.root)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual((self.root / "new.txt").read_bytes(), b"verified\n")

    def test_line_ending_conversion_requires_delivery_bytes(self):
        (self.root / ".gitattributes").write_text("value.txt text eol=crlf\n")
        (self.root / "value.txt").write_bytes(b"fixed\r\n")
        with self.assertRaisesRegex(RelayError, "line-ending normalization"):
            source_tree(self.root)
        self.assertEqual((self.root / "value.txt").read_bytes(), b"fixed\r\n")
        (self.root / "value.txt").write_bytes(b"fixed\n")
        self.assertTrue(source_tree(self.root))

    def test_working_tree_encoding_requires_delivery_bytes(self):
        (self.root / ".gitattributes").write_text("new.txt working-tree-encoding=UTF-16\n")
        (self.root / "new.txt").write_bytes("fixed\n".encode("utf-16"))
        with self.assertRaisesRegex(RelayError, "working-tree encoding conversion"):
            source_tree(self.root)
        self.assertEqual((self.root / "new.txt").read_bytes(), "fixed\n".encode("utf-16"))

    def test_symlink_checkout_type_must_match_deliverable_tree(self):
        link = self.root / "link.txt"
        link.symlink_to("value.txt")
        self.git("-c", "core.symlinks=true", "add", "link.txt")
        self.git("commit", "-qm", "add link")
        self.git("config", "core.symlinks", "false")
        link.unlink()
        link.write_bytes(b"value.txt")
        with self.assertRaisesRegex(RelayError, "symlink types"):
            source_tree(self.root)
        self.assertFalse(link.is_symlink())

    def test_raw_symlink_target_and_byte_paths_preserve_index(self):
        filename = b"raw-\r-name.txt"
        self.write_filename_fixture(self.root, filename, b"raw\x00bytes\xff")
        (self.root / "link.txt").symlink_to(os.fsdecode(filename))
        before = (self.root / ".git/index").read_bytes()
        tree = source_tree(self.root)
        self.assertTrue(tree)
        self.assertEqual((self.root / ".git/index").read_bytes(), before)
        self.git("add", ".")
        self.assertEqual(tree, self.git("write-tree"))
