import unittest

from brain import gitops
from tests.helpers import requires_git, TempVault


@requires_git
class TestGitops(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_not_initialized_by_default(self):
        self.assertFalse(gitops.is_initialized(self.config))

    def test_init_creates_repo_outside_brain_root(self):
        gitops.init_repo(self.config)
        self.assertTrue(gitops.is_initialized(self.config))
        # git-dir lives outside brain_root, only a small pointer file inside it
        self.assertFalse(str(self.config.git_dir).startswith(str(self.config.brain_root)))
        self.assertTrue((self.config.brain_root / ".git").exists())

    def test_init_twice_raises(self):
        gitops.init_repo(self.config)
        with self.assertRaises(gitops.GitError):
            gitops.init_repo(self.config)

    def test_snapshot_before_init_raises(self):
        with self.assertRaises(gitops.GitError):
            gitops.snapshot(self.config)

    def test_snapshot_commits_new_notes(self):
        gitops.init_repo(self.config)
        # Baseline commit first (TempVault pre-creates _registry.yaml), so
        # the second snapshot cleanly reflects only the note added below.
        gitops.snapshot(self.config)

        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        result = gitops.snapshot(self.config)
        self.assertTrue(result.committed)
        self.assertTrue(result.commit_hash)
        self.assertIn("1 added", result.message)
        self.assertIn("60_KNOWLEDGE", result.message)

    def test_snapshot_with_nothing_changed_does_not_commit(self):
        gitops.init_repo(self.config)
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        first = gitops.snapshot(self.config)
        self.assertTrue(first.committed)

        second = gitops.snapshot(self.config)
        self.assertFalse(second.committed)
        self.assertEqual(second.reason, "nothing_to_commit")

    def test_snapshot_refuses_on_secret_pattern(self):
        gitops.init_repo(self.config)
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge",
                               body="api_key: sk-abcdefghijklmnop1234567890")
        result = gitops.snapshot(self.config)
        self.assertFalse(result.committed)
        self.assertEqual(result.reason, "blocking_doctor_problems")
        self.assertTrue(any(p.check == "secret_pattern" for p in result.blocking))

    def test_snapshot_warns_but_proceeds_on_broken_link(self):
        gitops.init_repo(self.config)
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge",
                               body="See [[knowledge-nonexistent]].")
        result = gitops.snapshot(self.config)
        self.assertTrue(result.committed)
        self.assertTrue(any(p.check == "broken_links" for p in result.warnings))

    def test_log_and_status_after_init(self):
        import datetime

        gitops.init_repo(self.config)
        self.assertIn("No commits yet", gitops.status(self.config))
        self.assertIsNone(gitops.last_snapshot_date(self.config))

        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        gitops.snapshot(self.config)

        log_output = gitops.log(self.config)
        self.assertIn("Brain snapshot", log_output)
        self.assertEqual(gitops.last_snapshot_date(self.config), datetime.date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
