"""brain.projectops — project registry + record lifecycle (create, status
transitions, close). The core invariant under test: frontmatter status,
physical status folder, and the registry's status field must never drift
apart after any of these calls."""
from __future__ import annotations

import dataclasses
import unittest
from unittest.mock import patch

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


class TestSetProjectPath(unittest.TestCase):
    """The record's `path:` and the registry's `path:` must change together,
    or not at all."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/old/widget",
            status="active", aliases=["widget"], created="2026-01-01",
        )
        projectops.create_project(
            self.config, id="project-other", name="Other", path="/old/other",
            status="active", created="2026-01-01",
        )

    def tearDown(self):
        self.vault.cleanup()

    def _record(self):
        return frontmatter.parse_file(self.config.projects_dir / "ACTIVE" / "project-widget.md")

    def _entries(self):
        return {e.id: e for e in load_registry(self.config)}

    def test_updates_record_and_registry_together(self):
        result = projectops.set_project_path(self.config, "project-widget", "/new/widget")
        self.assertTrue(result.changed)
        self.assertTrue(result.registry_updated)
        self.assertEqual(result.old_path, "/old/widget")
        self.assertEqual(result.new_path, "/new/widget")
        self.assertEqual(self._record().meta["path"], "/new/widget")
        self.assertEqual(self._entries()["project-widget"].path, "/new/widget")

    def test_other_registry_entries_untouched(self):
        before = self.config.registry_path.read_text(encoding="utf-8")
        other_block = before[before.index("  - id: project-other"):]
        projectops.set_project_path(self.config, "project-widget", "/new/widget")
        self.assertTrue(self.config.registry_path.read_text(encoding="utf-8").endswith(other_block))

    def test_alias_resolves_to_canonical_id(self):
        result = projectops.set_project_path(self.config, "widget", "/new/widget")
        self.assertEqual(result.id, "project-widget")
        self.assertEqual(self._entries()["project-widget"].path, "/new/widget")

    def test_same_path_is_a_no_op(self):
        before = self.config.registry_path.read_text(encoding="utf-8")
        result = projectops.set_project_path(self.config, "project-widget", "/old/widget")
        self.assertFalse(result.changed)
        self.assertEqual(self.config.registry_path.read_text(encoding="utf-8"), before)

    def test_converges_when_only_the_registry_is_stale(self):
        # The state a frontmatter-only edit leaves behind.
        record = self.config.projects_dir / "ACTIVE" / "project-widget.md"
        note = frontmatter.parse_file(record)
        note.meta["path"] = "/new/widget"
        record.write_text(frontmatter.render(note), encoding="utf-8")
        result = projectops.set_project_path(self.config, "project-widget", "/new/widget")
        self.assertTrue(result.changed)
        self.assertEqual(self._entries()["project-widget"].path, "/new/widget")

    def test_invalid_paths_refused_without_writing(self):
        before = self.config.registry_path.read_text(encoding="utf-8")
        for bad in ("", "   ", "relative/dir", "/a/../b", "/a/b\nc",
                    str(self.config.brain_root), str(self.config.brain_root / "30_PROJECTS")):
            with self.subTest(path=bad):
                with self.assertRaises(projectops.ProjectWriteError):
                    projectops.set_project_path(self.config, "project-widget", bad)
        self.assertEqual(self.config.registry_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self._record().meta["path"], "/old/widget")

    def test_path_of_another_project_refused(self):
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_path(self.config, "project-widget", "/old/other")

    def test_unknown_project_id_fails_cleanly(self):
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_path(self.config, "project-nope", "/new")

    def test_non_owner_refused(self):
        other = dataclasses.replace(self.config, acting_principal="principal-other")
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_path(other, "project-widget", "/new/widget")
        self.assertEqual(self._record().meta["path"], "/old/widget")
        self.assertEqual(self._entries()["project-widget"].path, "/old/widget")

    def test_registry_failure_rolls_back_the_record(self):
        record_before = (self.config.projects_dir / "ACTIVE" / "project-widget.md").read_bytes()
        registry_before = self.config.registry_path.read_bytes()
        with patch.object(projectops, "set_registry_fields", side_effect=OSError("disk full")):
            with self.assertRaises(projectops.ProjectWriteError) as ctx:
                projectops.set_project_path(self.config, "project-widget", "/new/widget")
        self.assertIn("was rolled back", str(ctx.exception))
        self.assertEqual((self.config.projects_dir / "ACTIVE" / "project-widget.md").read_bytes(),
                         record_before)
        self.assertEqual(self.config.registry_path.read_bytes(), registry_before)


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


class TestCreateProjectSetsOwnership(unittest.TestCase):
    """Stage 2 (multi-user visibility): same default-ownership-on-write
    rule as capture.capture — see tests/test_capture.py."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_owner_principal_is_the_acting_principal(self):
        config = dataclasses.replace(self.config, acting_principal="principal-ania")
        path = projectops.create_project(
            config, id="project-widget", name="Widget", path="/tmp/example-widget")
        note = frontmatter.parse_file(path)
        self.assertEqual(note.meta["owner_principal"], "principal:ania")

    def test_audience_defaults_to_private(self):
        path = projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/tmp/example-widget")
        note = frontmatter.parse_file(path)
        self.assertEqual(note.meta["audience"], [])

    def test_explicit_audience_is_honored(self):
        path = projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/tmp/example-widget",
            audience=["group:household"])
        note = frontmatter.parse_file(path)
        self.assertEqual(note.meta["audience"], ["group:household"])


if __name__ == "__main__":
    unittest.main()
