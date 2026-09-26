"""brain.projectops — project registry + record lifecycle (create, status
transitions, close). The core invariant under test: frontmatter status,
physical status folder, and the registry's status field must never drift
apart after any of these calls."""
from __future__ import annotations

import unittest

from brain import frontmatter, projectops
from brain.registry import load_registry
from tests.helpers import TempVault


class TestCreateProject(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_creates_registry_entry_and_record(self):
        path = projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/tmp/example-widget",
            status="active", category="software", created="2026-09-26",
        )
        self.assertEqual(path.name, "project-widget.md")
        self.assertIn("30_PROJECTS/ACTIVE", str(path))
        note = frontmatter.parse_file(path)
        self.assertEqual(note.id, "project-widget")
        self.assertEqual(note.status, "active")
        self.assertEqual(note.meta["path"], "/tmp/example-widget")

        entries = {e.id: e for e in load_registry(self.config)}
        self.assertIn("project-widget", entries)
        self.assertEqual(entries["project-widget"].status, "active")
        self.assertEqual(entries["project-widget"].category, "software")

    def test_default_status_folder_matches_status(self):
        path = projectops.create_project(
            self.config, id="project-onhold", name="On Hold", path="/x", status="on-hold",
        )
        self.assertIn("30_PROJECTS/ON-HOLD", str(path))

    def test_never_copies_project_source(self):
        # Only a reference path is ever written — no file tree is created under it.
        projectops.create_project(self.config, id="project-x", name="X", path="/does/not/exist")
        self.assertFalse((self.config.brain_root / "does").exists())

    def test_duplicate_id_rejected(self):
        projectops.create_project(self.config, id="project-x", name="X", path="/a")
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.create_project(self.config, id="project-x", name="X2", path="/b")

    def test_duplicate_path_rejected(self):
        projectops.create_project(self.config, id="project-x", name="X", path="/a")
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.create_project(self.config, id="project-y", name="Y", path="/a")

    def test_invalid_status_rejected(self):
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.create_project(self.config, id="project-x", name="X", path="/a", status="bogus")


class TestSetProjectStatus(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/a",
            status="active", created="2026-01-01",
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_moves_file_between_status_folders(self):
        result = projectops.set_project_status(self.config, "project-widget", "on-hold")
        self.assertTrue(result.moved)
        self.assertIn("30_PROJECTS/ON-HOLD", str(result.new_path))
        self.assertFalse(result.old_path.exists())
        self.assertTrue(result.new_path.exists())

    def test_updates_frontmatter_status(self):
        result = projectops.set_project_status(self.config, "project-widget", "on-hold")
        note = frontmatter.parse_file(result.new_path)
        self.assertEqual(note.status, "on-hold")

    def test_updates_registry_entry(self):
        projectops.set_project_status(self.config, "project-widget", "on-hold")
        entries = {e.id: e for e in load_registry(self.config)}
        self.assertEqual(entries["project-widget"].status, "on-hold")

    def test_registry_entries_other_than_target_untouched(self):
        projectops.create_project(self.config, id="project-other", name="Other", path="/b")
        before = self.config.registry_path.read_text(encoding="utf-8")
        before_other_block = before[before.index("project-other") - 10:]

        projectops.set_project_status(self.config, "project-widget", "on-hold")

        after = self.config.registry_path.read_text(encoding="utf-8")
        self.assertIn(before_other_block.strip().splitlines()[0], after)

    def test_same_status_is_a_no_op_move(self):
        result = projectops.set_project_status(self.config, "project-widget", "active")
        self.assertFalse(result.moved)
        self.assertTrue(result.old_path.exists())

    def test_invalid_status_rejected(self):
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_status(self.config, "project-widget", "bogus")

    def test_unknown_project_id_fails_cleanly(self):
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_status(self.config, "project-does-not-exist", "on-hold")

    def test_target_file_collision_refused(self):
        # Simulate an orphan file already sitting at the destination.
        (self.config.projects_dir / "ON-HOLD").mkdir(parents=True, exist_ok=True)
        (self.config.projects_dir / "ON-HOLD" / "project-widget.md").write_text("stray")
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_status(self.config, "project-widget", "on-hold")


class TestCloseProject(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/a", status="active",
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_moves_to_archived(self):
        result = projectops.close_project(self.config, "project-widget")
        self.assertEqual(result.new_status, "archived")
        self.assertIn("30_PROJECTS/ARCHIVED", str(result.new_path))

    def test_summary_appended_without_deleting_prior_content(self):
        result = projectops.close_project(self.config, "project-widget", summary="Shipped v1.")
        note = frontmatter.parse_file(result.new_path)
        self.assertIn("Shipped v1.", note.body)
        self.assertIn("## Purpose", note.body)

    def test_no_summary_is_fine(self):
        result = projectops.close_project(self.config, "project-widget")
        self.assertEqual(result.new_status, "archived")


if __name__ == "__main__":
    unittest.main()
