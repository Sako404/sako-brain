import unittest
from pathlib import Path

from brain.paths import default_config, ensure_private_file


class TestDefaultConfig(unittest.TestCase):
    """Regression coverage for a Phase 4.2 bug: config.yaml's own
    `brain_root:` field used to override the physically-detected root,
    which meant restoring a backup into a temp directory for a recovery
    test silently re-indexed the *live* vault instead of the restored
    copy (because the restored config.yaml still said the live path)."""

    def test_brain_root_uses_detected_location_not_config_file_value(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "restored-copy"
            (fake_root / "90_SYSTEM").mkdir(parents=True)
            # Simulate a restored vault whose own config.yaml still claims
            # to live at the original (different) location.
            (fake_root / "90_SYSTEM" / "config.yaml").write_text(
                "brain_root: /some/other/original/path\n"
                "projects_root: /some/other/original/projects\n"
            )

            config = default_config(brain_root=fake_root)

            self.assertEqual(config.brain_root, fake_root)
            self.assertNotEqual(config.brain_root, Path("/some/other/original/path"))

    def test_brain_root_with_no_config_file_falls_back_to_detected_root(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "no-config-yet"
            fake_root.mkdir(parents=True)
            config = default_config(brain_root=fake_root)
            self.assertEqual(config.brain_root, fake_root)


class TestEnsurePrivateFile(unittest.TestCase):
    """Phase 5B finding: log files created via plain `open(path, 'a')`
    inherit the process umask (commonly 022 -> world-readable 644)
    regardless of how tightly Phase 4.2 chmod'd the surrounding
    directories. Every log-writing call site must correct this
    explicitly."""

    def test_chmods_file_to_owner_only(self):
        import stat
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.log"
            path.write_text("hello\n")
            path.chmod(0o644)  # simulate a default-umask file

            ensure_private_file(path)

            mode = path.stat().st_mode
            self.assertEqual(mode & stat.S_IRWXG, 0)
            self.assertEqual(mode & stat.S_IRWXO, 0)
            self.assertTrue(mode & stat.S_IRUSR)
            self.assertTrue(mode & stat.S_IWUSR)

    def test_missing_file_does_not_raise(self):
        ensure_private_file(Path("/nonexistent/path/does/not/exist.log"))  # must not raise


if __name__ == "__main__":
    unittest.main()
