import unittest

from brain.discover import discover
from tests.helpers import TempVault


class TestDiscover(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_finds_git_project(self):
        proj = self.vault.projects_root / "my-app"
        (proj / ".git").mkdir(parents=True)
        candidates = discover(self.config)
        names = [c.name for c in candidates]
        self.assertIn("my-app", names)

    def test_finds_readme_project(self):
        proj = self.vault.projects_root / "notes-only"
        proj.mkdir(parents=True)
        (proj / "README.md").write_text("# Notes only\n")
        candidates = discover(self.config)
        names = [c.name for c in candidates]
        self.assertIn("notes-only", names)

    def test_ignores_plain_folder_without_signals(self):
        proj = self.vault.projects_root / "just-some-files"
        (proj / "subdir").mkdir(parents=True)
        (proj / "subdir" / "data.txt").write_text("hello\n")
        candidates = discover(self.config)
        names = [c.name for c in candidates]
        self.assertNotIn("just-some-files", names)
        self.assertNotIn("subdir", names)

    def test_marks_already_registered(self):
        proj = self.vault.projects_root / "registered-app"
        (proj / ".git").mkdir(parents=True)
        self.config.registry_path.write_text(
            f"projects:\n  - id: project-registered-app\n    name: X\n    path: {proj}\n    status: active\n"
        )
        candidates = discover(self.config)
        match = next(c for c in candidates if c.name == "registered-app")
        self.assertTrue(match.already_registered)

    def test_never_writes_to_registry(self):
        proj = self.vault.projects_root / "some-app"
        (proj / ".git").mkdir(parents=True)
        before = self.config.registry_path.read_text()
        discover(self.config)
        after = self.config.registry_path.read_text()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
