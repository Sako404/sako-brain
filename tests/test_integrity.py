import unittest

from brain import integrity
from tests.helpers import requires_git, TempVault


class TestIntegrity(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_run_on_empty_vault(self):
        report = integrity.run(self.config, check_backup=False)
        self.assertEqual(report.total_notes, 0)
        self.assertEqual(report.manifest, {})
        self.assertFalse(report.git_initialized)
        self.assertIsNone(report.last_git_snapshot)
        self.assertFalse(report.backup_checked)

    def test_run_counts_notes_by_type(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        self.vault.write_note("10_PEOPLE", "p.md", id="person-a", type="person")
        report = integrity.run(self.config, check_backup=False)
        self.assertEqual(report.total_notes, 2)
        self.assertEqual(report.note_count_by_type["knowledge"], 1)
        self.assertEqual(report.note_count_by_type["person"], 1)

    def test_manifest_contains_sha256_hashes(self):
        path = self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        report = integrity.run(self.config, check_backup=False)
        rel = str(path.relative_to(self.config.brain_root))
        self.assertIn(rel, report.manifest)
        self.assertEqual(len(report.manifest[rel]), 64)  # sha256 hex digest length

    def test_manifest_changes_when_file_content_changes(self):
        path = self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge", body="v1")
        report1 = integrity.run(self.config, check_backup=False)
        rel = str(path.relative_to(self.config.brain_root))

        path.write_text(path.read_text() + "\nmore content\n")
        report2 = integrity.run(self.config, check_backup=False)
        self.assertNotEqual(report1.manifest[rel], report2.manifest[rel])

    def test_run_surfaces_doctor_problems(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-dup", type="knowledge")
        self.vault.write_note("60_KNOWLEDGE", "b.md", id="knowledge-dup", type="knowledge")
        report = integrity.run(self.config, check_backup=False)
        self.assertEqual(report.duplicate_ids, 1)

    def test_save_manifest_writes_json_under_integrity_dir(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        report = integrity.run(self.config, check_backup=False)
        path = integrity.save_manifest(self.config, report)
        self.assertTrue(path.exists())
        # OSS-1: manifests are runtime state, so they live under the configured
        # state directory — never inside the vault.
        self.assertEqual(path.parent, self.config.integrity_dir)
        self.assertFalse(
            str(path).startswith(str(self.vault.root)),
            f"integrity manifest was written inside the vault: {path}",
        )
        self.assertTrue(path.name.startswith("manifest-"))

    @requires_git
    def test_git_status_reflected_when_initialized(self):
        from brain import gitops
        gitops.init_repo(self.config)
        report = integrity.run(self.config, check_backup=False)
        self.assertTrue(report.git_initialized)
        self.assertIsNone(report.last_git_snapshot)  # no commits yet

        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        gitops.snapshot(self.config)
        report2 = integrity.run(self.config, check_backup=False)
        self.assertIsNotNone(report2.last_git_snapshot)


if __name__ == "__main__":
    unittest.main()
