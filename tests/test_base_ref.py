from devrelay.errors import RelayError
from devrelay.runner import base_commit, start

from test_delivery import GitHubDeliveryFixture


class BaseRefTests(GitHubDeliveryFixture):
    def advance_remote_base(self):
        (self.root / "new-base.txt").write_text("latest base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "advance target")
        self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "main")

    def test_custom_mapping_ignores_stale_origin_reference(self):
        stale = self.git("rev-parse", "origin/main")
        self.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/custom/*")
        latest = self.advance_remote_base()
        self.assertNotEqual(stale, latest)
        self.assertEqual(base_commit(self.config), latest)
        self.assertEqual(self.config.base_ref, "refs/remotes/custom/main")
        self.assertEqual(self.git("rev-parse", "origin/main"), stale)
        task = start(self.config, self.state, "12")
        self.assertEqual(task["base_sha"], latest)
        self.assertEqual(self.git("show", "HEAD:new-base.txt", cwd=task["worktree"]), "latest base")

    def test_custom_mapping_works_without_origin_main_reference(self):
        self.git("config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/custom/target")
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        latest = self.advance_remote_base()
        self.assertEqual(base_commit(self.config), latest)
        self.assertEqual(self.config.base_ref, "refs/remotes/custom/target")
        self.assertEqual(self.git("for-each-ref", "refs/remotes/origin/main"), "")

    def test_missing_mapping_never_uses_stale_origin_reference(self):
        self.git("config", "remote.origin.fetch", "+refs/heads/other:refs/remotes/custom/other")
        self.advance_remote_base()
        with self.assertRaisesRegex(RelayError, "no fetch tracking mapping"):
            base_commit(self.config)
