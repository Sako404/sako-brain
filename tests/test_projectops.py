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


class TestProjectPathVisibility(unittest.TestCase):
    """The canonical server cannot see project directories: it must say
    "not checked" rather than a False it has no grounds for, and a record
    path that drifted from the registry must still be caught there."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.remote = dataclasses.replace(self.config, remote_project_paths=True)
        projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/nonexistent/widget",
            status="active", created="2026-01-01",
        )

    def tearDown(self):
        self.vault.cleanup()

    def _show(self, config, *args) -> str:
        import contextlib
        import io

        from brain import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_project_show(config, "project-widget", *args)
        return out.getvalue()

    def test_path_exists_is_unknown_on_a_remote_host(self):
        self.assertIsNone(projectops.project_path_exists(self.remote, "/nonexistent/widget"))
        self.assertFalse(projectops.project_path_exists(self.config, "/nonexistent/widget"))
        self.assertTrue(projectops.project_path_exists(self.config, str(self.vault.root)))
        self.assertFalse(projectops.project_path_exists(self.config, None))

    def test_project_show_says_not_checked_instead_of_false(self):
        import json

        self.assertIn("path exists: not checked on this host", self._show(self.remote))
        self.assertNotIn("path exists: False", self._show(self.remote))
        self.assertIn("path exists: False", self._show(self.config))
        self.assertIsNone(json.loads(self._show(self.remote, True))["path_exists"])

    def test_get_project_returns_null_path_exists_on_a_remote_host(self):
        from brain import mcp_server
        self.assertIsNone(mcp_server.tool_get_project(self.remote, "project-widget")["path_exists"])
        self.assertFalse(mcp_server.tool_get_project(self.config, "project-widget")["path_exists"])

    def test_record_path_drift_is_reported_on_every_host_and_fixed_by_set_path(self):
        from brain import validate
        record = self.config.projects_dir / "ACTIVE" / "project-widget.md"
        record.write_text(record.read_text(encoding="utf-8").replace(
            "path: /nonexistent/widget", "path: /nonexistent/elsewhere"), encoding="utf-8")
        for config in (self.config, self.remote):
            mismatches = [p for p in validate.run_all(config) if p.check == "project_path_mismatch"]
            self.assertEqual(len(mismatches), 1)
            self.assertIn("/nonexistent/elsewhere", mismatches[0].message)
        projectops.set_project_path(self.config, "project-widget", "/nonexistent/widget")
        self.assertNotIn("project_path_mismatch", {p.check for p in validate.run_all(self.remote)})

    def test_matching_paths_are_not_a_mismatch(self):
        from brain import validate
        self.assertNotIn("project_path_mismatch", {p.check for p in validate.run_all(self.remote)})


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


class TestCurrentStateAndArchive(unittest.TestCase):
    """`Current state` is replaced as a whole and dated; whatever a replace
    overwrites moves to `## Archive`, so nothing is lost; an older record
    without the section gets it at the top of the body."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.path = projectops.create_project(
            self.config, id="project-widget", name="Widget", path="/a", status="active",
        )

    def tearDown(self):
        self.vault.cleanup()

    def _replace(self, text, section="Current state"):
        return projectops.update_section(self.config, "project-widget", section, "replace", text)

    def test_replace_sets_current_state_updated(self):
        import datetime as dt
        note = frontmatter.parse_file(self._replace("Broken: alerts down."))
        self.assertEqual(note.meta["current_state_updated"], dt.date.today().isoformat())

    def test_other_sections_do_not_touch_current_state_date(self):
        note = frontmatter.parse_file(self._replace("x", section="Next actions"))
        self.assertNotIn("current_state_updated", note.meta)

    def test_replaced_content_moves_to_archive(self):
        self._replace("Broken: alerts down.")
        note = frontmatter.parse_file(self._replace("Fixed: alerts work."))
        state = projectops.current_state(note)
        self.assertEqual(state["text"], "Fixed: alerts work.")
        archive = note.body[note.body.index("## Archive"):]
        self.assertIn("### Current state (replaced ", archive)
        self.assertIn("Broken: alerts down.", archive)
        self.assertNotIn("Broken: alerts down.", note.body[:note.body.index("## Archive")])

    def test_archive_keeps_every_replaced_version_in_order(self):
        self._replace("One.")
        self._replace("Two.")
        note = frontmatter.parse_file(self._replace("Three."))
        archive = note.body[note.body.index("## Archive"):]
        self.assertEqual(note.body.count("## Archive"), 1)
        self.assertLess(archive.index("One."), archive.index("Two."))
        self.assertNotIn("Three.", archive)

    def test_replacing_an_empty_section_archives_nothing(self):
        note = frontmatter.parse_file(self._replace("First state."))
        self.assertNotIn("## Archive", note.body)

    def test_missing_current_state_is_created_after_title(self):
        self.vault.write_note(str(self.path.parent.relative_to(self.vault.root)), self.path.name,
                               id="project-widget",
                               type="project", status="active", title="Widget",
                               body="## Status\n\nOld status text.\n")
        note = frontmatter.parse_file(self._replace("Now running."))
        self.assertTrue(note.body.lstrip("\n").startswith("# Widget\n\n## Current state\nNow running."))
        self.assertIn("## Status\n\nOld status text.", note.body)
        self.assertNotIn("## Archive", note.body)

    def test_missing_current_state_keeps_intro_text_out_of_the_section(self):
        # Regression (v0.18.0, found on the first live record): an intro
        # line between the title and the first section became the new
        # section's content and a replace archived it as "Current state".
        self.vault.write_note(str(self.path.parent.relative_to(self.vault.root)), self.path.name,
                               id="project-widget", type="project", status="active", title="Widget",
                               body="*A one-line motto.*\n\n## Status\n\nOld status text.\n")
        note = frontmatter.parse_file(self._replace("Now running."))
        body = note.body.lstrip("\n")
        self.assertTrue(body.startswith("# Widget\n\n*A one-line motto.*\n\n## Current state\nNow running."))
        self.assertNotIn("## Archive", note.body)

    def test_missing_current_state_without_any_section_goes_at_the_end(self):
        self.vault.write_note(str(self.path.parent.relative_to(self.vault.root)), self.path.name,
                               id="project-widget", type="project", status="active", title="Widget",
                               body="Just an intro.")
        note = frontmatter.parse_file(self._replace("Now running."))
        self.assertIn("Just an intro.\n\n## Current state\nNow running.", note.body)
        self.assertNotIn("## Archive", note.body)

    def test_other_missing_sections_still_refused(self):
        self.vault.write_note(str(self.path.parent.relative_to(self.vault.root)), self.path.name,
                               id="project-widget",
                               type="project", status="active", title="Widget", body="")
        with self.assertRaises(projectops.SectionEditError):
            self._replace("x", section="Next actions")

    def test_read_current_state(self):
        self._replace("Running.")
        state = projectops.read_current_state(self.config, "project-widget")
        self.assertEqual(state["text"], "Running.")
        self.assertTrue(state["updated"])
        self.assertIsNone(projectops.read_current_state(self.config, "project-nope"))


if __name__ == "__main__":
    unittest.main()


class TestNotePathsSnapshot(unittest.TestCase):
    """Registry visibility and `brain state` look a record up per project;
    each lookup used to rescan the whole vault. Inside note_paths_snapshot
    the vault is scanned once and the answers are the same."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for i in range(5):
            projectops.create_project(
                self.config, id=f"project-p{i}", name=f"P{i}", path=f"/nonexistent/p{i}",
                status="active", created="2026-01-01",
            )

    def tearDown(self):
        self.vault.cleanup()

    def _counting_scans(self):
        from brain import update
        calls = []
        real = update._scan_paths_by_id

        def counting(config):
            calls.append(1)
            return real(config)
        return calls, patch.object(update, "_scan_paths_by_id", side_effect=counting)

    def test_visible_registry_scans_the_vault_once_with_the_same_answer(self):
        from brain import registry, update
        expected = {e.id: update.find_note_paths(self.config, e.id) for e in load_registry(self.config)}
        calls, patcher = self._counting_scans()
        with patcher:
            visible = registry.load_visible_registry(self.config)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(e.id for e in visible), sorted(expected))
        with update.note_paths_snapshot(self.config):
            for note_id, paths in expected.items():
                self.assertEqual(update.find_note_paths(self.config, note_id), paths)
            self.assertEqual(update.find_note_paths(self.config, "project-missing"), [])

    def test_state_projects_section_scans_once(self):
        from brain import state
        calls, patcher = self._counting_scans()
        with patcher:
            with patch.object(state, "_collect_decisions", return_value=(state.DecisionsSection(), 0)):
                result = state.get_operational_state(self.config)
        self.assertTrue(result.sources["projects"].ok)
        self.assertEqual(len(result.projects.entries), 5)
        # projects: one scan; nothing else in state looks records up by id.
        self.assertEqual(len(calls), 1)

    def test_snapshot_is_reused_when_nested_and_dropped_after(self):
        from brain import update
        calls, patcher = self._counting_scans()
        with patcher:
            with update.note_paths_snapshot(self.config):
                with update.note_paths_snapshot(self.config):
                    update.find_note_paths(self.config, "project-p0")
            self.assertEqual(len(calls), 1)
            self.vault.write_note("60_KNOWLEDGE", "late.md", id="knowledge-late", type="knowledge")
            self.assertEqual(len(update.find_note_paths(self.config, "knowledge-late")), 1)
