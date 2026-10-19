"""Synthetic regressions for listing, ACL and project lifecycle failures.

Each scenario injects a fault or constructs invalid fictional records and
checks both the response and the state left on disk.
"""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain import handoff, indexer, listing, projectops, update, validate, visibility
from brain.registry import find_project
from tests.helpers import TempVault


def _audit_lines(config, event: str) -> list[str]:
    lines = []
    for log in sorted(config.logs_dir.glob("brain-audit-*.log")):
        lines += [ln for ln in log.read_text().splitlines() if f"event={event}" in ln]
    return lines


# --------------------------------------------------------------------------
# Finding 1 — discovery must not depend on the index
# --------------------------------------------------------------------------

class TestListingDiscoveryIsIndexIndependent(unittest.TestCase):
    """Reproduction: two visible files, one index row removed -> the listing
    returned one record, `total_visible: 1`, `complete: true`."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for n in ("a", "b"):
            self.vault.write_note("40_DECISIONS", f"d-{n}.md", id=f"decision-{n}",
                                   type="decision", status="proposed",
                                   updated="2026-10-01", title=n.upper())
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def _ids(self, **kw):
        return sorted(e.id for e in listing.list_notes(self.config, **kw).entries)

    def test_a_deleted_index_row_does_not_hide_a_record(self):
        conn = indexer.connect(self.config)
        try:
            conn.execute("DELETE FROM notes WHERE id = 'decision-b'")
            conn.execute("DELETE FROM notes_fts WHERE id = 'decision-b'")
            conn.commit()
        finally:
            conn.close()
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual(sorted(e.id for e in result.entries), ["decision-a", "decision-b"])
        self.assertEqual(result.total_visible, 2)
        self.assertTrue(result.complete)

    def test_a_file_written_after_the_last_reindex_is_listed(self):
        self.vault.write_note("40_DECISIONS", "d-c.md", id="decision-c", type="decision",
                               status="proposed", updated="2026-10-02", title="C")
        self.assertEqual(self._ids(type="decision", status="proposed"),
                          ["decision-a", "decision-b", "decision-c"])

    def test_an_emptied_index_does_not_empty_the_listing(self):
        self.config.db_path.unlink()
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual(result.total_visible, 2)
        self.assertTrue(result.complete)

    def test_metadata_edited_on_disk_wins_over_a_stale_index(self):
        path = self.vault.root / "40_DECISIONS" / "d-a.md"
        path.write_text(path.read_text().replace("status: proposed", "status: decided"))
        self.assertEqual(self._ids(type="decision", status="proposed"), ["decision-b"])
        self.assertEqual(self._ids(type="decision", status="decided"), ["decision-a"])

    def test_a_deleted_file_disappears_even_though_its_index_row_remains(self):
        (self.vault.root / "40_DECISIONS" / "d-b.md").unlink()
        self.assertEqual(self._ids(type="decision", status="proposed"), ["decision-a"])


class TestSourceCompletenessIsSeparateFromPagination(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("40_DECISIONS", "ok.md", id="decision-ok", type="decision",
                               status="proposed", title="Fine")

    def tearDown(self):
        self.vault.cleanup()

    def test_an_offset_past_the_last_match_is_not_complete(self):
        """Reproduction: one match, `offset=100` -> returned 0, total 1,
        `complete: true`, which read as "there are none"."""
        result = listing.list_notes(self.config, type="decision", status="proposed", offset=100)
        self.assertEqual(result.returned, 0)
        self.assertEqual(result.total_visible, 1)
        self.assertFalse(result.complete)
        self.assertFalse(result.page_complete)
        self.assertTrue(result.source_complete)
        self.assertIsNone(result.next_offset)

    def test_an_unparseable_note_drops_source_complete_and_is_counted(self):
        (self.vault.root / "40_DECISIONS" / "broken.md").write_text(
            "---\nid: [unclosed\ntype: decision\n---\n\n# broken\n")
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual([e.id for e in result.entries], ["decision-ok"])
        self.assertFalse(result.complete)
        self.assertFalse(result.source_complete)
        self.assertTrue(result.page_complete)
        self.assertEqual(result.unreadable, 1)

    def test_a_note_with_no_id_counts_as_unaccounted_for(self):
        (self.vault.root / "40_DECISIONS" / "no-id.md").write_text(
            "---\ntype: decision\nstatus: proposed\n---\n\n# no id\n")
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual(result.unreadable, 1)
        self.assertFalse(result.source_complete)

    def test_a_raw_inbox_capture_prevents_claiming_source_completeness(self):
        """Metadata filters cannot classify a raw capture. Keep its separate
        counter, but never claim the entire visible source was classified."""
        (self.vault.root / "00_INBOX" / "raw.md").write_text("a dictated thought\n")
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertFalse(result.source_complete)
        self.assertFalse(result.complete)
        self.assertEqual(result.unreadable, 0)
        self.assertEqual(result.unstructured_skipped, 1)

    def test_the_cli_reports_the_two_reasons_separately(self):
        from brain import cli

        (self.vault.root / "40_DECISIONS" / "broken.md").write_text(
            "---\nid: [unclosed\n---\n\n# broken\n")

        class Args:
            type, status, project, tag = "decision", "proposed", None, None
            limit, offset, json = 100, 0, False

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_list(self.config, Args()), 0)
        self.assertIn("NOT THE FULL LIST", out.getvalue())
        self.assertIn("source:", err.getvalue())
        self.assertNotIn("page:", err.getvalue())


# --------------------------------------------------------------------------
# Finding 2 — the refusal must not describe what it is protecting
# --------------------------------------------------------------------------

class TestAmbiguityRefusalLeaksNothing(unittest.TestCase):
    """Reproduction: a visible copy owned by the caller and an invisible copy
    owned by another principal shared one id; the refusal named the invisible
    copy's full path and the total number of files."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("60_KNOWLEDGE", "mine.md", id="knowledge-shared",
                               type="knowledge", title="Mine",
                               owner_principal="principal:marcin", audience=[])
        self.vault.write_note("20_AREAS", "theirs-secret.md", id="knowledge-shared",
                               type="knowledge", title="Theirs",
                               owner_principal="principal:ania", audience=[])

    def tearDown(self):
        self.vault.cleanup()

    def _assert_contentless(self, message: str):
        for leak in ("theirs-secret", "20_AREAS", "60_KNOWLEDGE", "ania", "2 files", "copies=", "/tmp"):
            self.assertNotIn(leak, message, f"refusal leaked {leak!r}: {message}")

    def test_update_still_refuses_when_one_copy_is_hidden(self):
        with self.assertRaises(update.AmbiguousNoteIdError) as ctx:
            update.update_memory(self.config, "knowledge-shared", append_text="x",
                                  principal_id="principal-marcin")
        self._assert_contentless(str(ctx.exception))

    def test_it_does_not_quietly_write_to_the_visible_copy(self):
        before = {p: p.read_text() for p in update.find_note_paths(self.config, "knowledge-shared")}
        with self.assertRaises(update.AmbiguousNoteIdError):
            update.update_memory(self.config, "knowledge-shared", append_text="x",
                                  principal_id="principal-marcin")
        for path, text in before.items():
            self.assertEqual(path.read_text(), text)

    def test_supersedes_refuses_the_same_way(self):
        from brain import decision as decision_mod

        self.vault.write_note("40_DECISIONS", "dup-a.md", id="decision-dup", type="decision",
                               status="proposed", title="A",
                               owner_principal="principal:marcin", audience=[])
        self.vault.write_note("50_TIMELINE", "dup-b-secret.md", id="decision-dup",
                               type="decision", status="proposed", title="B",
                               owner_principal="principal:ania", audience=[])
        with self.assertRaises(update.AmbiguousNoteIdError) as ctx:
            decision_mod.create_decision(self.config, title="New", supersedes="decision-dup")
        message = str(ctx.exception)
        for leak in ("dup-b-secret", "50_TIMELINE", "40_DECISIONS", "ania"):
            self.assertNotIn(leak, message)
        self.assertEqual(
            list(self.config.dir_for("decisions").glob("decision-*-new.md")), [],
            "a refused supersedes must not leave a new decision behind")

    def test_the_project_status_write_path_refuses_the_same_way(self):
        self.vault.write_note("30_PROJECTS/ACTIVE", "p.md", id="project-dup", type="project",
                               status="active", title="P",
                               owner_principal="principal:marcin", audience=[])
        self.vault.write_note("30_PROJECTS/PLANNED", "p-secret.md", id="project-dup",
                               type="project", status="active", title="P2",
                               owner_principal="principal:ania", audience=[])
        self.config.registry_path.write_text(
            "projects:\n  - id: project-dup\n    name: P\n    path: /tmp/p\n"
            "    status: active\n    updated: '2026-01-01'\n")
        with self.assertRaises(update.AmbiguousNoteIdError) as ctx:
            projectops.set_project_status(self.config, "project-dup", "on-hold")
        for leak in ("p-secret", "PLANNED", "ania"):
            self.assertNotIn(leak, str(ctx.exception))

    def test_the_details_do_reach_the_operator_audit_log(self):
        with self.assertRaises(update.AmbiguousNoteIdError):
            update.update_memory(self.config, "knowledge-shared", append_text="x",
                                  principal_id="principal-marcin")
        lines = _audit_lines(self.config, "note.write.ambiguous_id")
        self.assertEqual(len(lines), 1)
        self.assertIn("copies=2", lines[0])
        self.assertIn("theirs-secret.md", lines[0])

    def test_that_audit_log_is_private_and_outside_the_vault(self):
        with self.assertRaises(update.AmbiguousNoteIdError):
            update.update_memory(self.config, "knowledge-shared", append_text="x",
                                  principal_id="principal-marcin")
        log = sorted(self.config.logs_dir.glob("brain-audit-*.log"))[0]
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
        self.assertNotIn(self.config.brain_root, log.parents)


# --------------------------------------------------------------------------
# Finding 3 — a half-applied status change must not be reported as success
# --------------------------------------------------------------------------

class TestStatusChangeRecovery(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        projectops.create_project(self.config, id="project-x", name="X",
                                   path=str(self.vault.projects_root / "x"), status="active")
        handoff.write(self.config, "project-x",
                      handoff.HandoffSections(attempted="first"), session_date="2026-09-19")
        self._real_rename = Path.rename

    def tearDown(self):
        self.vault.cleanup()

    def _folders(self):
        pj = self.config.projects_dir
        return (
            [f.parent.name for f in pj.rglob("project-x.md")],
            [f.parent.name for f in pj.rglob("project-x-handoff.md")],
            find_project(self.config, "project-x").status,
        )

    def _frontmatter_status(self):
        # Resolved by id, not by filename: the collision test deliberately
        # puts an unrelated file at the target *path*, and rglob would find
        # that one too.
        path = update.find_note_paths(self.config, "project-x")[0]
        return next(l.split(": ", 1)[1] for l in path.read_text().splitlines()
                    if l.startswith("status:"))

    def _failing_rename(self, only_handoff: bool):
        real = self._real_rename

        def rename(inner_self, target):
            is_handoff = "handoff" in inner_self.name
            if is_handoff == only_handoff:
                raise OSError("simulated rename failure")
            return real(inner_self, target)
        return rename

    def test_a_failed_handoff_move_rolls_the_whole_change_back(self):
        """Reproduction: the handoff rename failed after the record AND the
        registry had already moved, leaving the handoff in ACTIVE/ while
        every reader resolved it from ON-HOLD/."""
        with mock.patch.object(Path, "rename", self._failing_rename(only_handoff=True)):
            with self.assertRaises(projectops.ProjectWriteError):
                projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertEqual(self._folders(), (["ACTIVE"], ["ACTIVE"], "active"))
        self.assertEqual(self._frontmatter_status(), "active")

    def test_retrying_after_that_failure_converges(self):
        """Reproduction: retrying the same status change did NOT move the
        orphaned handoff, because the record's frontmatter already claimed
        the new status so nothing looked like it needed moving."""
        with mock.patch.object(Path, "rename", self._failing_rename(only_handoff=True)):
            with contextlib.suppress(projectops.ProjectWriteError):
                projectops.set_project_status(self.config, "project-x", "on-hold")
        result = projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertEqual(self._folders(), (["ON-HOLD"], ["ON-HOLD"], "on-hold"))
        self.assertTrue(result.registry_updated)
        self.assertEqual(validate.check_handoff_in_wrong_status_folder(self.config), [])
        self.assertEqual(len(update.find_note_paths(self.config, "handoff-project-x")), 1)

    def test_a_failed_record_move_does_not_leave_a_rewritten_frontmatter(self):
        """Reproduction: the frontmatter was rewritten before the rename, so a
        failed rename left `status: on-hold` on a file sitting in ACTIVE/ —
        and a retry then could not tell where the handoff had been."""
        with mock.patch.object(Path, "rename", self._failing_rename(only_handoff=False)):
            with self.assertRaises(projectops.ProjectWriteError):
                projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertEqual(self._frontmatter_status(), "active")
        self.assertEqual(self._folders(), (["ACTIVE"], ["ACTIVE"], "active"))

    def test_a_failed_registry_update_is_fatal_not_a_warning(self):
        """Reproduction: it returned a result with `registry_updated: False`
        while the record and handoff had already moved — so readers resolving
        the handoff from the registry looked in the old folder."""
        with mock.patch.object(projectops, "set_registry_fields",
                                side_effect=projectops.ProjectWriteError("simulated")):
            with self.assertRaises(projectops.ProjectWriteError):
                projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertEqual(self._folders(), (["ACTIVE"], ["ACTIVE"], "active"))

    def test_the_rollback_is_audited(self):
        with mock.patch.object(projectops, "set_registry_fields",
                                side_effect=projectops.ProjectWriteError("simulated")):
            with contextlib.suppress(projectops.ProjectWriteError):
                projectops.set_project_status(self.config, "project-x", "on-hold")
        lines = _audit_lines(self.config, "project.status_change.failed")
        self.assertEqual(len(lines), 1)
        self.assertIn("rolled_back=True", lines[0])

    def test_a_target_collision_refuses_before_touching_anything(self):
        blocker = self.config.projects_dir / "ON-HOLD" / "project-x.md"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("blocker\n")
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertEqual(self._frontmatter_status(), "active")
        self.assertEqual(blocker.read_text(), "blocker\n")

    def test_an_index_failure_does_not_undo_correct_data(self):
        with mock.patch.object(projectops.indexer, "index_note",
                                side_effect=RuntimeError("simulated index failure")):
            result = projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertTrue(result.moved)
        self.assertTrue(result.registry_updated)
        self.assertFalse(result.index_updated)
        self.assertEqual(self._folders(), (["ON-HOLD"], ["ON-HOLD"], "on-hold"))

    def test_the_cli_says_so_when_only_the_index_is_behind(self):
        from brain import cli

        class Args:
            id, status, section, mode, content = "project-x", "on-hold", None, None, None
            append_text, set = None, []
            if_match, audience, confirm_restricted, json = None, None, False, False

        out = io.StringIO()
        with mock.patch.object(projectops.indexer, "index_note",
                                side_effect=RuntimeError("simulated")), \
             contextlib.redirect_stdout(out):
            cli.cmd_project_update(self.config, Args())
        self.assertIn("run `brain index`", out.getvalue())

    def test_a_legacy_orphan_is_repaired_by_a_no_op_status_change(self):
        """A handoff stranded by a status change made *before* this fix: the
        record is already in the right folder, so nothing "moves", and the
        orphan still has to be collected."""
        projectops.set_project_status(self.config, "project-x", "on-hold")
        orphan = self.config.projects_dir / "ACTIVE" / "project-x-handoff.md"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text(
            (self.config.projects_dir / "ON-HOLD" / "project-x-handoff.md").read_text())
        (self.config.projects_dir / "ON-HOLD" / "project-x-handoff.md").unlink()
        self.assertTrue(validate.check_handoff_in_wrong_status_folder(self.config))

        result = projectops.set_project_status(self.config, "project-x", "on-hold")

        self.assertFalse(result.moved)
        self.assertEqual(result.handoff_old_path, orphan)
        self.assertEqual(validate.check_handoff_in_wrong_status_folder(self.config), [])
        self.assertIn("first", handoff.handoff_path(self.config, "project-x").read_text())

    def test_two_existing_copies_are_left_for_a_human(self):
        for folder, body in (("ACTIVE", "A-side"), ("ON-HOLD", "B-side")):
            f = self.config.projects_dir / folder / "project-x-handoff.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"---\nid: handoff-project-x\ntype: document\n---\n\n# H\n\n{body}\n")
        result = projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertIsNone(result.handoff_old_path)
        self.assertIn("A-side", (self.config.projects_dir / "ACTIVE" / "project-x-handoff.md").read_text())
        self.assertIn("B-side", (self.config.projects_dir / "ON-HOLD" / "project-x-handoff.md").read_text())

    def test_repeating_a_completed_change_is_a_no_op(self):
        projectops.set_project_status(self.config, "project-x", "on-hold")
        again = projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertFalse(again.moved)
        self.assertIsNone(again.handoff_old_path)
        self.assertEqual(self._folders(), (["ON-HOLD"], ["ON-HOLD"], "on-hold"))


# --------------------------------------------------------------------------
# Finding 4 — the handoff write path, and read-side shadowing
# --------------------------------------------------------------------------

class TestHandoffWriteIsGated(unittest.TestCase):
    """Reproduction: with two copies of a handoff sharing one id, the next
    session was appended to whichever copy the registry pointed at, splitting
    the history further. The id-based guard never saw it, because this write
    is addressed by project + status folder."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        projectops.create_project(self.config, id="project-x", name="X",
                                   path=str(self.vault.projects_root / "x"), status="on-hold")
        self.copies = {}
        for folder, body in (("ACTIVE", "A-history"), ("ON-HOLD", "B-history")):
            f = self.config.projects_dir / folder / "project-x-handoff.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("---\nid: handoff-project-x\ntype: document\nstatus: current\n"
                          "owner_principal: principal:marcin\naudience: []\n---\n\n"
                          f"# Handoff\n\n{body}\n")
            self.copies[folder] = f

    def tearDown(self):
        self.vault.cleanup()

    def test_write_refuses_instead_of_appending_to_one_of_them(self):
        with self.assertRaises(update.AmbiguousNoteIdError):
            handoff.write(self.config, "project-x",
                           handoff.HandoffSections(attempted="third session"))

    def test_neither_copy_is_modified(self):
        before = {k: v.read_text() for k, v in self.copies.items()}
        with contextlib.suppress(update.AmbiguousNoteIdError):
            handoff.write(self.config, "project-x",
                           handoff.HandoffSections(attempted="third session"))
        for folder, text in before.items():
            self.assertEqual(self.copies[folder].read_text(), text)

    def test_the_refusal_names_no_paths(self):
        with self.assertRaises(update.AmbiguousNoteIdError) as ctx:
            handoff.write(self.config, "project-x",
                           handoff.HandoffSections(attempted="third session"))
        for leak in ("ACTIVE", "ON-HOLD", "/tmp", "copies="):
            self.assertNotIn(leak, str(ctx.exception))

    def test_it_is_audited(self):
        with contextlib.suppress(update.AmbiguousNoteIdError):
            handoff.write(self.config, "project-x",
                           handoff.HandoffSections(attempted="third session"))
        lines = _audit_lines(self.config, "note.write.ambiguous_id")
        self.assertEqual(len(lines), 1)
        self.assertIn("id=handoff-project-x", lines[0])

    def test_a_single_copy_still_writes_normally(self):
        self.copies["ACTIVE"].unlink()
        path = handoff.write(self.config, "project-x",
                              handoff.HandoffSections(attempted="third session"))
        self.assertIn("third session", path.read_text())
        self.assertIn("B-history", path.read_text())


class TestHiddenCopyDoesNotShadowAVisibleOne(unittest.TestCase):
    """Reproduction: `20_AREAS` is scanned before `60_KNOWLEDGE`, so a copy
    owned by another principal resolved first and the caller's own visible
    copy read as "not found"."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("20_AREAS", "hidden.md", id="knowledge-shadow",
                               type="knowledge", title="Theirs",
                               owner_principal="principal:ania", audience=[])
        self.vault.write_note("60_KNOWLEDGE", "visible.md", id="knowledge-shadow",
                               type="knowledge", title="Mine",
                               owner_principal="principal:marcin", audience=[])

    def tearDown(self):
        self.vault.cleanup()

    def test_the_hidden_copy_really_does_sort_first(self):
        self.assertEqual(update.find_note_path(self.config, "knowledge-shadow").parent.name,
                          "20_AREAS")

    def test_the_visible_copy_is_served(self):
        text = visibility.read_visible_note_text(self.config, "principal-marcin",
                                                   "knowledge-shadow")
        self.assertIn("Mine", text)
        self.assertNotIn("Theirs", text)

    def test_the_other_principal_gets_their_own(self):
        text = visibility.read_visible_note_text(self.config, "principal-ania",
                                                   "knowledge-shadow")
        self.assertIn("Theirs", text)

    def test_a_principal_who_may_see_neither_still_gets_the_same_not_found(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            visibility.read_visible_note_text(self.config, "principal-marcel",
                                               "knowledge-shadow")
        self.assertEqual(str(ctx.exception), "no note with id 'knowledge-shadow'")

    def test_a_genuinely_missing_id_is_unchanged(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            visibility.read_visible_note_text(self.config, "principal-marcin", "knowledge-nope")
        self.assertEqual(str(ctx.exception), "no note with id 'knowledge-nope'")

    def test_the_denial_is_still_audited(self):
        visibility.read_visible_note_text(self.config, "principal-marcin", "knowledge-shadow")
        self.assertTrue(_audit_lines(self.config, "note.read.denied"))

    def test_visible_note_paths_never_reveals_the_hidden_copy(self):
        paths = visibility.visible_note_paths(self.config, "principal-marcin", "knowledge-shadow")
        self.assertEqual([p.parent.name for p in paths], ["60_KNOWLEDGE"])

    def test_brain_get_prints_the_visible_copy_without_a_warning(self):
        from brain import cli

        class Args:
            id = "knowledge-shadow"

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_get(self.config, Args()), 0)
        self.assertIn("Mine", out.getvalue())
        self.assertNotIn("WARNING", err.getvalue())


class TestSeveralVisibleCopiesHaveDefinedBehaviour(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for d, title in (("20_AREAS", "First"), ("60_KNOWLEDGE", "Second")):
            self.vault.write_note(d, "copy.md", id="knowledge-both", type="knowledge",
                                   title=title, owner_principal="principal:marcin", audience=[])

    def tearDown(self):
        self.vault.cleanup()

    def test_the_first_in_deterministic_order_is_served(self):
        text = visibility.read_visible_note_text(self.config, "principal-marcin", "knowledge-both")
        self.assertIn("First", text)

    def test_it_is_audited_as_ambiguous(self):
        visibility.read_visible_note_text(self.config, "principal-marcin", "knowledge-both")
        lines = _audit_lines(self.config, "note.read.ambiguous_id")
        self.assertEqual(len(lines), 1)
        self.assertIn("visible_copies=2", lines[0])

    def test_brain_get_warns_naming_both_visible_copies(self):
        from brain import cli

        class Args:
            id = "knowledge-both"

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_get(self.config, Args())
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("20_AREAS", err.getvalue())
        self.assertIn("60_KNOWLEDGE", err.getvalue())


if __name__ == "__main__":
    unittest.main()


# ==========================================================================
# Additional synthetic failure scenarios
#
# The first round's fixes held; this round broke them in new places. Three
# themes again, and again none of them is a missing feature:
#
#   1. a rollback that made the vault worse than the failure did, and no
#      coordination at all between two writers;
#   2. completeness that still quietly depended on how a file was encoded;
#   3. counters and refusals that carried information across an ACL.
# ==========================================================================

import os
import subprocess
import sys
import textwrap

from brain import frontmatter

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

requires_flock = unittest.skipIf(fcntl is None, "fcntl.flock is POSIX-only")


class TestRollbackNeverManufacturesADuplicate(unittest.TestCase):
    """Reproduction: with the handoff move failing AND the record's
    rename-back failing during rollback, the journal went on to write the
    original bytes at the record's old path — so the vault held the moved
    file and a resurrected copy of it. Two files under one id, which the
    converging retry then could not repair, because a write addressed by an
    ambiguous id is refused by design:

        ROLLBACK_RECORD_COPIES ['ACTIVE', 'ON-HOLD']
        RETRY_FAILED AmbiguousNoteIdError
    """

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        projectops.create_project(self.config, id="project-x", name="X",
                                   path=str(self.vault.projects_root / "x"), status="active")
        handoff.write(self.config, "project-x",
                      handoff.HandoffSections(attempted="first"), session_date="2026-09-19")
        self._real_rename = Path.rename

    def tearDown(self):
        self.vault.cleanup()

    def _break_handoff_move_and_the_rollback(self):
        real = self._real_rename

        def rename(inner_self, target):
            if "handoff" in inner_self.name:
                raise OSError("simulated handoff move failure")
            if inner_self.parent.name == "ON-HOLD":
                raise OSError("simulated rollback rename failure")
            return real(inner_self, target)
        return rename

    def _record_copies(self):
        return sorted(p.parent.name for p in self.config.projects_dir.rglob("project-x.md"))

    def test_only_one_copy_of_the_record_survives(self):
        with mock.patch.object(Path, "rename", self._break_handoff_move_and_the_rollback()):
            with self.assertRaises(projectops.ProjectWriteError):
                projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertEqual(self._record_copies(), ["ON-HOLD"])
        self.assertEqual(len(update.find_note_paths(self.config, "project-x")), 1)

    def test_the_retry_can_still_converge(self):
        with mock.patch.object(Path, "rename", self._break_handoff_move_and_the_rollback()):
            with contextlib.suppress(projectops.ProjectWriteError):
                projectops.set_project_status(self.config, "project-x", "on-hold")
        result = projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertTrue(result.registry_updated)
        self.assertEqual(self._record_copies(), ["ON-HOLD"])
        self.assertEqual(validate.check_handoff_in_wrong_status_folder(self.config), [])

    def test_the_declined_inverse_is_reported_not_silent(self):
        """An inverse that refuses to run is a rollback failure like any
        other — the message has to say so, or the operator believes the
        rollback was clean."""
        record = update.find_note_paths(self.config, "project-x")[0]
        real = self._real_rename

        def rename(inner_self, target):
            if "handoff" in inner_self.name:
                raise OSError("simulated handoff move failure")
            result = real(inner_self, target)
            if inner_self.name == record.name:
                # The file is gone from where the content-restoring inverse
                # expects it; that inverse must decline, not recreate it.
                target.unlink()
            return result

        with mock.patch.object(Path, "rename", rename):
            with self.assertRaises(projectops.ProjectWriteError) as ctx:
                projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertIn("rollback itself did not fully succeed", str(ctx.exception))

    def test_the_message_does_not_promise_a_retry_that_cannot_work(self):
        """With two copies on disk a retry is refused, so telling the
        operator to re-run is the one thing the message must not do."""
        for folder in ("ACTIVE", "PLANNED"):
            path = self.config.projects_dir / folder / "project-x.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("---\nid: project-x\ntype: project\nstatus: active\n---\n\n# X\n")
        real = self._real_rename

        def rename(inner_self, target):
            raise OSError("simulated failure")

        with mock.patch.object(Path, "rename", rename):
            with self.assertRaises(Exception) as ctx:
                projectops.set_project_status(self.config, "project-x", "on-hold")
        # Either the ambiguity guard refuses up front, or the failure message
        # says a retry cannot repair it. Both are honest; silence is not.
        message = str(ctx.exception)
        self.assertTrue(
            "does not resolve to a single record" in message
            or "CANNOT repair" in message
            or "rolled back" in message,
            message)


@requires_flock
class TestConcurrentProjectWritesAreCoordinated(unittest.TestCase):
    """Reproduction: two status changes on one project, the slower one failing
    and replaying its inverses over bytes the faster one had already
    committed. End state: two record copies, a registry naming neither, and
    the handoff beside the wrong one. No journal can fix that — the
    operations have to not interleave."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        projectops.create_project(self.config, id="project-x", name="X",
                                   path=str(self.vault.projects_root / "x"), status="active")
        handoff.write(self.config, "project-x",
                      handoff.HandoffSections(attempted="first"), session_date="2026-09-19")

    def tearDown(self):
        self.vault.cleanup()

    def _worker_source(self, status: str) -> str:
        return textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from pathlib import Path
            from brain import projectops
            from brain.paths import Config
            cfg = Config(brain_root=Path({str(self.vault.root)!r}),
                         projects_roots=(Path({str(self.vault.projects_root)!r}),),
                         backup_target=Path({str(self.vault.backup_target)!r}),
                         git_dir=Path({str(self.vault.git_dir)!r}),
                         backup_repo=Path({str(self.vault.backup_repo)!r}),
                         state_dir=Path({str(self.vault.state_dir)!r}))
            projectops.set_project_status(cfg, "project-x", {status!r})
            print("done")
        """)

    def test_the_lock_actually_blocks_a_second_process(self):
        """Deterministic, no sleeps: the second process asks for the lock
        non-blockingly and must be refused while the first holds it."""
        probe = textwrap.dedent(f"""
            import fcntl, sys
            path = {str(self.config.state_dir / projectops.LOCK_FILENAME)!r}
            with open(path, "a+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print("BLOCKED")
                else:
                    print("ACQUIRED")
        """)
        with projectops.project_write_lock(self.config):
            held = subprocess.run([sys.executable, "-c", probe],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(held.stdout.strip(), "BLOCKED", held.stderr)
        free = subprocess.run([sys.executable, "-c", probe],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(free.stdout.strip(), "ACQUIRED", free.stderr)

    def test_concurrent_status_changes_leave_one_consistent_state(self):
        procs = [subprocess.Popen([sys.executable, "-c", self._worker_source(s)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for s in ("on-hold", "completed", "on-hold")]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, f"worker failed: {err}")

        pj = self.config.projects_dir
        records = sorted(p.parent.name for p in pj.rglob("project-x.md"))
        handoffs = sorted(p.parent.name for p in pj.rglob("project-x-handoff.md"))
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records, handoffs)
        self.assertTrue(handoff.handoff_path(self.config, "project-x").exists())
        self.assertEqual(validate.check_duplicate_ids(validate.load_all_notes(self.config)[0]), [])
        self.assertEqual(validate.check_handoff_in_wrong_status_folder(self.config), [])

    def test_the_lock_file_lives_outside_the_vault(self):
        with projectops.project_write_lock(self.config):
            lock_path = self.config.state_dir / projectops.LOCK_FILENAME
            self.assertTrue(lock_path.exists())
        self.assertFalse(lock_path.is_relative_to(self.config.brain_root))

    def test_create_project_and_section_edits_take_the_same_lock(self):
        """One lock for every write that touches a record's location or the
        shared registry — a second lock would only add a deadlock."""
        import inspect

        for fn in (projectops.create_project, projectops.set_project_status,
                    projectops.update_section):
            self.assertIn("project_write_lock", inspect.getsource(fn),
                           f"{fn.__name__} does not take the write lock")


class TestEncodingDoesNotDecideCompleteness(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_a_note_with_a_utf8_bom_is_an_ordinary_note(self):
        """Reproduction: a BOM made the opening `---` fail a strict
        startswith, so the note was classified as a raw capture and
        disappeared from a listing that still reported `complete: true`."""
        path = self.vault.write_note("40_DECISIONS", "bom.md", id="decision-bom",
                                      type="decision", status="proposed", title="Has a BOM")
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual([e.id for e in result.entries], ["decision-bom"])
        self.assertEqual(result.unstructured_skipped, 0)
        self.assertEqual(result.unreadable, 0)
        self.assertTrue(result.complete)

    def test_doctor_no_longer_calls_a_bom_note_malformed(self):
        path = self.vault.write_note("40_DECISIONS", "bom.md", id="decision-bom",
                                      type="decision", status="proposed", title="Has a BOM")
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        _notes, parse_errors = validate.load_all_notes(self.config)
        self.assertEqual(parse_errors, [])

    def test_parse_text_tolerates_a_leading_bom_for_direct_callers(self):
        note = frontmatter.parse_text("﻿---\nid: x\n---\n\n# T\n", Path("x.md"))
        self.assertEqual(note.id, "x")

    def test_undecodable_bytes_are_one_unreadable_file_not_a_crash(self):
        """Reproduction: an unhandled UnicodeDecodeError took the whole
        listing down instead of being reported as `source_complete: false`."""
        self.vault.write_note("40_DECISIONS", "ok.md", id="decision-ok", type="decision",
                               status="proposed", title="Fine")
        (self.vault.root / "40_DECISIONS" / "bad.md").write_bytes(
            b"---\nid: decision-bad\ntype: decision\nstatus: proposed\n---\n\n# \xff\xfe\n")

        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual([e.id for e in result.entries], ["decision-ok"])
        self.assertFalse(result.source_complete)
        self.assertFalse(result.complete)
        self.assertEqual(result.unreadable, 1)

    def test_undecodable_bytes_do_not_crash_doctor_or_the_indexer(self):
        from brain import indexer

        self.vault.write_note("40_DECISIONS", "ok.md", id="decision-ok", type="decision",
                               status="proposed", title="Fine")
        (self.vault.root / "40_DECISIONS" / "bad.md").write_bytes(
            b"---\nid: decision-bad\ntype: decision\n---\n\n# \xff\xfe\n")

        _notes, parse_errors = validate.load_all_notes(self.config)
        self.assertEqual([p.check for p in parse_errors], ["invalid_yaml"])
        stats = indexer.rebuild(self.config)
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(len(stats["errors"]), 1)


class TestCountersStayInsideTheCallersView(unittest.TestCase):
    """Reproduction: adding one unparseable file owned by another principal
    flipped this caller's `complete` from true to false without changing a
    single visible entry — and `unstructured_skipped` handed a non-owner the
    number of raw files in someone else's inbox."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("60_KNOWLEDGE", "mine.md", id="knowledge-mine",
                               type="knowledge", title="Mine",
                               owner_principal="principal:marcin", audience=[])

    def tearDown(self):
        self.vault.cleanup()

    def _as(self, principal_id):
        return listing.list_notes(self.config, principal_id=principal_id)

    def test_another_principals_unidentified_note_does_not_move_my_counters(self):
        before = self._as("principal-marcin")
        (self.vault.root / "20_AREAS" / "theirs.md").write_text(
            "---\ntype: knowledge\nstatus: current\nowner_principal: principal:other\n"
            "audience: []\n---\n\n# Theirs, no id\n")
        after = self._as("principal-marcin")
        self.assertEqual(after.unreadable, before.unreadable)
        self.assertEqual(after.complete, before.complete)
        self.assertEqual([e.id for e in after.entries], [e.id for e in before.entries])

    def test_my_own_unidentified_note_still_counts_for_me(self):
        (self.vault.root / "20_AREAS" / "mine-no-id.md").write_text(
            "---\ntype: knowledge\nstatus: current\nowner_principal: principal:marcin\n"
            "audience: []\n---\n\n# Mine, no id\n")
        result = self._as("principal-marcin")
        self.assertEqual(result.unreadable, 1)
        self.assertFalse(result.source_complete)

    def test_a_stranger_is_told_nothing_about_the_default_owners_raw_inbox(self):
        (self.vault.root / "00_INBOX" / "raw.md").write_text("a dictated thought\n")
        (self.vault.root / "40_DECISIONS" / "broken.md").write_text("---\nid: [unclosed\n---\n\n# x\n")
        stranger = self._as("principal-ania")
        self.assertEqual(stranger.total_visible, 0)
        self.assertEqual(stranger.unstructured_skipped, 0)
        self.assertEqual(stranger.unreadable, 0)
        self.assertTrue(stranger.complete)

    def test_the_default_owner_does_see_them(self):
        """Scoping the counters must not turn into hiding real faults from
        the person who can act on them."""
        (self.vault.root / "00_INBOX" / "raw.md").write_text("a dictated thought\n")
        (self.vault.root / "40_DECISIONS" / "broken.md").write_text("---\nid: [unclosed\n---\n\n# x\n")
        owner = self._as("principal-marcin")
        self.assertEqual(owner.unstructured_skipped, 1)
        self.assertEqual(owner.unreadable, 1)
        self.assertFalse(owner.source_complete)


class TestRefusalIsInformationallyNeutral(unittest.TestCase):
    """Reproduction: an unknown id raised FileNotFoundError while an id held
    by three invisible copies raised AmbiguousNoteIdError — so the pair of
    refusals told a stranger the record exists."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for i, d in enumerate(("20_AREAS", "60_KNOWLEDGE", "70_DOCUMENTS")):
            self.vault.write_note(d, f"h{i}.md", id="knowledge-allhidden", type="knowledge",
                                   title=f"H{i}", owner_principal="principal:other", audience=[])

    def tearDown(self):
        self.vault.cleanup()

    def _refusal(self, note_id):
        try:
            update.update_memory(self.config, note_id, append_text="x",
                                  principal_id="principal-marcin")
        except Exception as exc:
            return type(exc), str(exc)
        return None, "wrote"

    def test_all_copies_hidden_is_indistinguishable_from_not_existing(self):
        hidden = self._refusal("knowledge-allhidden")
        missing = self._refusal("knowledge-does-not-exist")
        self.assertEqual(hidden[0], FileNotFoundError)
        self.assertEqual(hidden[0], missing[0])
        self.assertEqual(
            hidden[1].replace("knowledge-allhidden", "ID"),
            missing[1].replace("knowledge-does-not-exist", "ID"))

    def test_the_write_is_still_blocked(self):
        before = {p: p.read_text() for p in update.find_note_paths(self.config, "knowledge-allhidden")}
        self._refusal("knowledge-allhidden")
        for path, text in before.items():
            self.assertEqual(path.read_text(), text)

    def test_the_operator_log_records_what_really_happened(self):
        self._refusal("knowledge-allhidden")
        lines = _audit_lines(self.config, "note.write.ambiguous_id")
        self.assertEqual(len(lines), 1)
        self.assertIn("copies=3", lines[0])

    def test_a_caller_who_can_see_one_copy_gets_the_ambiguity_error(self):
        """They already know the record exists, so this tells them nothing
        new — and they are the one who can reconcile it."""
        self.vault.write_note("10_PEOPLE", "mine.md", id="knowledge-mixed", type="knowledge",
                               title="Mine", owner_principal="principal:marcin", audience=[])
        self.vault.write_note("20_AREAS", "theirs.md", id="knowledge-mixed", type="knowledge",
                               title="Theirs", owner_principal="principal:other", audience=[])
        kind, message = self._refusal("knowledge-mixed")
        self.assertEqual(kind, update.AmbiguousNoteIdError)
        for leak in ("20_AREAS", "theirs", "other", "copies="):
            self.assertNotIn(leak, message)


class TestHandoffGateCoversTheOrphanCase(unittest.TestCase):
    """Reproduction: with the project's ONLY handoff stranded in the wrong
    status folder there was exactly one claimant, so a count-based gate let
    this write create a fresh file at the canonical path — manufacturing the
    duplicate it exists to prevent."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        projectops.create_project(self.config, id="project-x", name="X",
                                   path=str(self.vault.projects_root / "x"), status="on-hold")
        handoff.write(self.config, "project-x",
                      handoff.HandoffSections(attempted="first"), session_date="2026-09-19")
        self.stranded = self.config.projects_dir / "ACTIVE" / "project-x-handoff.md"
        self.stranded.parent.mkdir(parents=True, exist_ok=True)
        (self.config.projects_dir / "ON-HOLD" / "project-x-handoff.md").rename(self.stranded)

    def tearDown(self):
        self.vault.cleanup()

    def test_a_single_stranded_copy_still_blocks_the_write(self):
        self.assertEqual(len(update.find_note_paths(self.config, "handoff-project-x")), 1)
        with self.assertRaises(update.AmbiguousNoteIdError):
            handoff.write(self.config, "project-x",
                           handoff.HandoffSections(attempted="second"))

    def test_no_second_copy_is_created(self):
        with contextlib.suppress(update.AmbiguousNoteIdError):
            handoff.write(self.config, "project-x",
                           handoff.HandoffSections(attempted="second"))
        copies = sorted(p.parent.name for p in
                         self.config.projects_dir.rglob("project-x-handoff.md"))
        self.assertEqual(copies, ["ACTIVE"])
        self.assertIn("first", self.stranded.read_text())
        self.assertNotIn("second", self.stranded.read_text())

    def test_the_refusal_names_no_path_and_no_count(self):
        with self.assertRaises(update.AmbiguousNoteIdError) as ctx:
            handoff.write(self.config, "project-x",
                           handoff.HandoffSections(attempted="second"))
        for leak in ("ACTIVE", "ON-HOLD", "/tmp", "copies="):
            self.assertNotIn(leak, str(ctx.exception))

    def test_repairing_the_orphan_unblocks_the_write(self):
        """The refusal has to have a way out, and it is the documented one."""
        projectops.set_project_status(self.config, "project-x", "on-hold")
        path = handoff.write(self.config, "project-x",
                              handoff.HandoffSections(attempted="second"))
        self.assertIn("first", path.read_text())
        self.assertIn("second", path.read_text())
        self.assertEqual(len(update.find_note_paths(self.config, "handoff-project-x")), 1)

    def test_a_handoff_already_at_the_canonical_path_is_not_blocked(self):
        self.stranded.rename(self.config.projects_dir / "ON-HOLD" / "project-x-handoff.md")
        path = handoff.write(self.config, "project-x",
                              handoff.HandoffSections(attempted="second"))
        self.assertIn("second", path.read_text())


class TestSymlinkPolicyIsExplicit(unittest.TestCase):
    """The scan boundary is the vault, not wherever its directories happen to
    point. A link escaping it is skipped by every scan AND reported, because
    silently dropping a file is the failure mode this whole release is about."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.outside_dir = Path(tempfile.mkdtemp())
        self.outside = self.outside_dir / "outside.md"
        self.outside.write_text(
            "---\nid: knowledge-outside\ntype: knowledge\nstatus: current\n"
            "sensitivity: normal\n---\n\n# Outside the vault\n")
        self.link = self.vault.root / "60_KNOWLEDGE" / "link.md"
        self.link.symlink_to(self.outside)

    def tearDown(self):
        shutil.rmtree(self.outside_dir, ignore_errors=True)
        self.vault.cleanup()

    def test_it_is_not_listed(self):
        result = listing.list_notes(self.config, type="knowledge")
        self.assertEqual([e.id for e in result.entries], [])

    def test_it_is_not_indexed(self):
        from brain import indexer

        stats = indexer.rebuild(self.config)
        self.assertEqual(stats["indexed"], 0)

    def test_doctor_reports_it_rather_than_only_skipping_it(self):
        problems = validate.check_symlink_escapes_vault(self.config)
        self.assertEqual([p.check for p in problems], ["symlink_escapes_vault"])
        self.assertIn("link.md", problems[0].message)

    def test_it_is_not_a_blocking_finding(self):
        from brain.gitops import BLOCKING_CHECKS as blocking

        self.assertNotIn("symlink_escapes_vault", blocking)

    def test_a_broken_link_is_treated_as_outside_and_reported(self):
        self.outside.unlink()
        self.assertEqual([e.id for e in listing.list_notes(self.config).entries], [])
        self.assertTrue(validate.check_symlink_escapes_vault(self.config))

    def test_a_link_that_stays_inside_the_vault_still_works(self):
        """Scoping the boundary must not break a legitimate in-vault link."""
        self.link.unlink()  # drop this class's escaping link; it has its own tests
        real = self.vault.write_note("60_KNOWLEDGE", "real.md", id="knowledge-real",
                                      type="knowledge", title="Real")
        inside_link = self.vault.root / "20_AREAS" / "alias.md"
        inside_link.symlink_to(real)
        ids = [e.id for e in listing.list_notes(self.config, type="knowledge").entries]
        self.assertIn("knowledge-real", ids)
        self.assertEqual(validate.check_symlink_escapes_vault(self.config), [])
