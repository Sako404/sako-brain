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


class TestUpdateSection(unittest.TestCase):
    """Extracted from what /project-sync does today via a direct Edit on a
    named body section. The core invariant: only the targeted section's
    content changes — the header itself, and every other section, stay
    byte-identical."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.path = projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/a", status="active",
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_replace_current_state(self):
        path = projectops.update_section(
            self.config, "project-widget", "Current state", "replace", "Now feature-complete.",
        )
        note = frontmatter.parse_file(path)
        self.assertIn("Now feature-complete.", note.body)

    def test_replace_never_touches_other_sections(self):
        before = frontmatter.parse_file(self.path)
        projectops.update_section(self.config, "project-widget", "Current state", "replace", "New state.")
        after = frontmatter.parse_file(self.path)
        for other_section in ("## Purpose", "## Location", "## Architecture / technologies",
                               "## Important locations", "## Milestones", "## Decisions",
                               "## Problems / limitations", "## Next actions",
                               "## Relationships", "## Sources"):
            self.assertIn(other_section, after.body)
        # And the pre-existing placeholder text under Milestones (untouched section) survives.
        self.assertIn("Links to `40_DECISIONS/`", after.body)
        self.assertEqual(before.body.count("## "), after.body.count("## "))

    def test_append_preserves_prior_content(self):
        projectops.update_section(self.config, "project-widget", "Milestones", "append", "v1 shipped.")
        note = frontmatter.parse_file(self.path)
        self.assertIn("Links to `40_DECISIONS/`", note.body)  # original placeholder still there
        self.assertIn("v1 shipped.", note.body)

    def test_append_twice_keeps_both_entries(self):
        projectops.update_section(self.config, "project-widget", "Milestones", "append", "First.")
        projectops.update_section(self.config, "project-widget", "Milestones", "append", "Second.")
        note = frontmatter.parse_file(self.path)
        self.assertIn("First.", note.body)
        self.assertIn("Second.", note.body)

    def test_unknown_section_rejected(self):
        with self.assertRaises(projectops.SectionEditError):
            projectops.update_section(self.config, "project-widget", "Purpose", "replace", "x")

    def test_decisions_section_rejected(self):
        # Explicitly excluded — /project-sync's own rule: use /decision instead.
        with self.assertRaises(projectops.SectionEditError):
            projectops.update_section(self.config, "project-widget", "Decisions", "replace", "x")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(projectops.SectionEditError):
            projectops.update_section(self.config, "project-widget", "Current state", "delete", "x")

    def test_unknown_project_id_fails_cleanly(self):
        with self.assertRaises(projectops.SectionEditError):
            projectops.update_section(self.config, "project-does-not-exist", "Current state", "replace", "x")

    def test_non_project_note_rejected(self):
        self.vault.write_note("60_KNOWLEDGE", "k.md", id="knowledge-k", type="knowledge",
                               body="# K\n\n## Current state\n\nsomething\n")
        with self.assertRaises(projectops.SectionEditError):
            projectops.update_section(self.config, "knowledge-k", "Current state", "replace", "x")

    def test_if_match_succeeds_when_hash_matches(self):
        import hashlib
        note = frontmatter.parse_file(self.path)
        lines = note.body.splitlines(keepends=True)
        start, end = projectops._section_span(lines, "Current state")
        current_hash = hashlib.sha256("".join(lines[start:end]).encode("utf-8")).hexdigest()
        # Should not raise.
        projectops.update_section(
            self.config, "project-widget", "Current state", "replace", "New.", if_match=current_hash,
        )

    def test_if_match_refused_on_stale_hash(self):
        with self.assertRaises(projectops.SectionEditError):
            projectops.update_section(
                self.config, "project-widget", "Current state", "replace", "New.",
                if_match="0" * 64,
            )

    def test_bumps_updated_timestamp(self):
        import datetime as dt
        projectops.update_section(self.config, "project-widget", "Current state", "replace", "x")
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["updated"], dt.date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
