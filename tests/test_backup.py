import shutil
import tempfile
import unittest
from pathlib import Path

from brain import backup
from tests.helpers import requires_git, TempVault

RESTIC_AVAILABLE = shutil.which("restic") is not None


class TestBackupSettings(unittest.TestCase):
    def test_rclone_repository_string(self):
        settings = backup.BackupSettings(backend="rclone", rclone_remote="REMOTE", rclone_repo_path="path/to/repo")
        self.assertEqual(settings.repository(), "rclone:REMOTE:path/to/repo")

    def test_local_repository_string(self):
        settings = backup.BackupSettings(backend="local", local_repo_path=Path("/tmp/somewhere"))
        self.assertEqual(settings.repository(), "/tmp/somewhere")

    def test_unknown_backend_raises(self):
        settings = backup.BackupSettings(backend="ftp")
        with self.assertRaises(backup.BackupError):
            settings.repository()

    def test_default_settings_uses_rclone_by_default(self):
        vault = TempVault()
        try:
            settings = backup.default_settings(vault.config())
            self.assertEqual(settings.backend, "rclone")
            self.assertEqual(settings.local_repo_path, vault.backup_repo)
        finally:
            vault.cleanup()


class TestCredential(unittest.TestCase):
    def test_credential_not_ready_when_missing(self):
        settings = backup.BackupSettings(password_file=Path("/nonexistent/path/to/password"))
        self.assertFalse(backup.credential_ready(settings))

    def test_credential_not_ready_when_empty(self):
        with tempfile.NamedTemporaryFile() as f:
            settings = backup.BackupSettings(password_file=Path(f.name))
            self.assertFalse(backup.credential_ready(settings))

    def test_credential_ready_when_populated(self):
        with tempfile.NamedTemporaryFile(mode="w") as f:
            f.write("a-test-password\n")
            f.flush()
            settings = backup.BackupSettings(password_file=Path(f.name))
            self.assertTrue(backup.credential_ready(settings))

    def test_operations_refuse_without_credential(self):
        settings = backup.BackupSettings(backend="local", local_repo_path=Path("/tmp/wont-be-used"),
                                          password_file=Path("/nonexistent/password"))
        with self.assertRaises(backup.BackupError):
            backup.init_repo(settings)


class TestExcludesAndPaths(unittest.TestCase):
    def test_excludes_cover_generated_data(self):
        self.assertIn("90_SYSTEM/brain.db", backup.EXCLUDES)
        self.assertIn("**/__pycache__", backup.EXCLUDES)
        self.assertIn("*.pyc", backup.EXCLUDES)

    def test_excludes_do_not_exclude_content_dirs(self):
        for content_dir in ("10_PEOPLE", "20_AREAS", "30_PROJECTS", "40_DECISIONS"):
            self.assertFalse(any(content_dir in pattern for pattern in backup.EXCLUDES))

    def test_backup_paths_includes_brain_root_always(self):
        vault = TempVault()
        try:
            config = vault.config()
            paths = backup.backup_paths(config)
            self.assertIn(str(config.brain_root), paths)
        finally:
            vault.cleanup()

    def test_backup_paths_omits_git_dir_when_not_initialized(self):
        vault = TempVault()
        try:
            config = vault.config()
            self.assertFalse(config.git_dir.exists())
            paths = backup.backup_paths(config)
            self.assertNotIn(str(config.git_dir), paths)
        finally:
            vault.cleanup()

    @requires_git
    def test_backup_paths_includes_git_dir_once_initialized(self):
        from brain import gitops
        vault = TempVault()
        try:
            config = vault.config()
            gitops.init_repo(config)
            paths = backup.backup_paths(config)
            self.assertIn(str(config.git_dir), paths)
        finally:
            vault.cleanup()


@unittest.skipUnless(RESTIC_AVAILABLE, "restic is not installed")
class TestResticRoundTrip(unittest.TestCase):
    """Exercises the real restic binary against a disposable local repo —
    never the real Google Drive destination. Per project instruction: don't
    mock the backup program's documented behaviour."""

    def setUp(self):
        self.vault = TempVault()
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge",
                               title="Widgets", body="All about widgets.")
        self.config = self.vault.config()

        self._tmp = tempfile.TemporaryDirectory()
        password_file = Path(self._tmp.name) / "password"
        password_file.write_text("test-password-not-real\n")
        self.settings = backup.BackupSettings(
            backend="local",
            local_repo_path=Path(self._tmp.name) / "repo",
            password_file=password_file,
        )

    def tearDown(self):
        self.vault.cleanup()
        self._tmp.cleanup()

    def test_not_initialized_before_init(self):
        initialized, _detail = backup.is_initialized(self.settings)
        self.assertFalse(initialized)

    def test_init_then_backup_then_check(self):
        init_result = backup.init_repo(self.settings)
        self.assertEqual(init_result.returncode, 0)

        initialized, _ = backup.is_initialized(self.settings)
        self.assertTrue(initialized)

        backup_result = backup.run_backup(self.config, self.settings)
        self.assertEqual(backup_result.returncode, 0, backup_result.stderr)

        check_result = backup.check_repo(self.settings)
        self.assertEqual(check_result.returncode, 0, check_result.stderr)

    def test_backup_excludes_generated_files(self):
        # Simulate a generated file that must never be backed up.
        db_path = self.config.brain_root / "90_SYSTEM" / "brain.db"
        db_path.write_text("pretend sqlite content")

        backup.init_repo(self.settings)
        backup.run_backup(self.config, self.settings)

        snaps = backup.list_snapshots(self.settings)
        self.assertEqual(len(snaps), 1)
        restore_dir = Path(self._tmp.name) / "restore-check"
        backup.restore_snapshot(self.settings, snaps[0].short_id, restore_dir)

        restored_db = list(restore_dir.rglob("brain.db"))
        self.assertEqual(restored_db, [], "brain.db should have been excluded from the backup")

    def test_restore_round_trip_matches_original(self):
        backup.init_repo(self.settings)
        backup.run_backup(self.config, self.settings)
        snaps = backup.list_snapshots(self.settings)

        restore_dir = Path(self._tmp.name) / "restore-out"
        result = backup.restore_snapshot(self.settings, snaps[0].short_id, restore_dir)
        self.assertEqual(result.returncode, 0)

        restored = list(restore_dir.rglob("a.md"))
        self.assertEqual(len(restored), 1)
        original = (self.config.brain_root / "60_KNOWLEDGE" / "a.md").read_text()
        self.assertEqual(restored[0].read_text(), original)

    def test_restore_refuses_dangerous_target(self):
        backup.init_repo(self.settings)
        backup.run_backup(self.config, self.settings)
        snaps = backup.list_snapshots(self.settings)

        with self.assertRaises(backup.BackupError):
            backup.restore_snapshot(self.settings, snaps[0].short_id, Path.home())

    def test_last_backup_date_reflects_most_recent_snapshot(self):
        import datetime

        self.assertIsNone(backup.last_backup_date(self.settings))
        backup.init_repo(self.settings)
        backup.run_backup(self.config, self.settings)
        self.assertEqual(backup.last_backup_date(self.settings), datetime.date.today().isoformat())

    def test_log_result_writes_secret_free_line(self):
        backup.init_repo(self.settings)
        result = backup.run_backup(self.config, self.settings)
        log_path = backup.log_result(self.config, "run", result)
        content = log_path.read_text()
        self.assertIn("run", content)
        self.assertIn("OK", content)
        self.assertNotIn("test-password-not-real", content)

    def test_latest_snapshot_returns_none_when_empty(self):
        backup.init_repo(self.settings)
        self.assertIsNone(backup.latest_snapshot(self.settings))

    def test_latest_snapshot_returns_most_recent(self):
        backup.init_repo(self.settings)
        backup.run_backup(self.config, self.settings)
        snap = backup.latest_snapshot(self.settings)
        self.assertIsNotNone(snap)
        self.assertTrue(snap.short_id)
        self.assertIn("sako-brain", snap.tags)

    def test_forget_and_prune_respects_retention_policy(self):
        backup.init_repo(self.settings)
        backup.run_backup(self.config, self.settings)
        # A single recent snapshot is well within keep-daily=14 — forget
        # should keep it, not remove it.
        result = backup.forget_and_prune(self.settings, keep_daily=14, keep_weekly=8, keep_monthly=24)
        self.assertEqual(result.returncode, 0, result.stderr)
        snaps = backup.list_snapshots(self.settings)
        self.assertEqual(len(snaps), 1)

    def test_forget_and_prune_dry_run_does_not_error_or_remove(self):
        backup.init_repo(self.settings)
        backup.run_backup(self.config, self.settings)
        result = backup.forget_and_prune(self.settings, keep_daily=14, keep_weekly=8, keep_monthly=24, dry_run=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        snaps = backup.list_snapshots(self.settings)
        self.assertEqual(len(snaps), 1)

    def test_forget_and_prune_uses_tag_scope(self):
        backup.init_repo(self.settings)
        backup.run_backup(self.config, self.settings, tag="sako-brain")
        # A retention call scoped to a *different* tag matches zero
        # snapshots, so our sako-brain-tagged snapshot must survive
        # untouched regardless of the keep policy applied to that (empty)
        # selection.
        result = backup.forget_and_prune(self.settings, keep_daily=1, keep_weekly=1, keep_monthly=1,
                                          tag="some-other-tag")
        self.assertEqual(result.returncode, 0, result.stderr)
        snaps = backup.list_snapshots(self.settings)
        self.assertEqual(len(snaps), 1)
        self.assertIn("sako-brain", snaps[0].tags)


class TestRetentionPolicyConstants(unittest.TestCase):
    def test_retention_policy_matches_phase_4_3_spec(self):
        self.assertEqual(backup.RETENTION_POLICY, {"daily": 14, "weekly": 8, "monthly": 24})


if __name__ == "__main__":
    unittest.main()
