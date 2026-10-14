"""Synthetic record-consistency scenarios.

A fictional project's handoff can be stranded in an old status folder or
copied under the same ID. Tests verify relocation, refusal of ambiguous
writes and continued ACL-scoped reads without silently splitting history.
"""
import unittest

from brain import handoff, projectops, update, validate
from brain.gitops import BLOCKING_CHECKS
from tests.helpers import TempVault


def _register(vault, project_id="project-example", status="active"):
    config = vault.config()
    projectops.create_project(config, id=project_id, name="Example",
                               path=str(vault.projects_root / "example"), status=status)
    return config


class TestHandoffTravelsWithItsProject(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = _register(self.vault)
        (self.vault.projects_root / "example").mkdir(parents=True, exist_ok=True)
        handoff.write(self.config, "project-example",
                      handoff.HandoffSections(attempted="deployed it"),
                      session_date="2026-09-19")

    def tearDown(self):
        self.vault.cleanup()

    def test_status_change_relocates_the_handoff(self):
        old = self.config.projects_dir / "ACTIVE" / "project-example-handoff.md"
        self.assertTrue(old.exists())

        result = projectops.set_project_status(self.config, "project-example", "on-hold")

        new = self.config.projects_dir / "ON-HOLD" / "project-example-handoff.md"
        self.assertTrue(new.exists())
        self.assertFalse(old.exists())
        self.assertEqual(result.handoff_old_path, old)
        self.assertEqual(result.handoff_new_path, new)

    def test_relocated_handoff_keeps_its_history_and_accepts_the_next_session(self):
        projectops.set_project_status(self.config, "project-example", "on-hold")
        handoff.write(self.config, "project-example",
                      handoff.HandoffSections(attempted="session close"),
                      session_date="2026-09-30")

        text = (self.config.projects_dir / "ON-HOLD" / "project-example-handoff.md").read_text()
        self.assertIn("deployed it", text)
        self.assertIn("session close", text)
        self.assertEqual(len(update.find_note_paths(self.config, "handoff-project-example")), 1)

    def test_no_second_handoff_is_created_after_a_status_change(self):
        """The regression itself: before this fix, the write below produced a
        second file with the same id in the new status folder."""
        projectops.set_project_status(self.config, "project-example", "on-hold")
        handoff.write(self.config, "project-example",
                      handoff.HandoffSections(attempted="second session"),
                      session_date="2026-09-30")
        problems = validate.check_duplicate_ids(validate.load_all_notes(self.config)[0])
        self.assertEqual(problems, [])

    def test_a_project_with_no_handoff_is_unaffected(self):
        projectops.create_project(self.config, id="project-plain", name="Plain",
                                   path=str(self.vault.projects_root / "plain"))
        result = projectops.set_project_status(self.config, "project-plain", "on-hold")
        self.assertIsNone(result.handoff_old_path)
        self.assertIsNone(result.handoff_new_path)

    def test_an_existing_duplicate_is_never_overwritten_by_the_relocation(self):
        """If both copies already exist, merging them is a content decision —
        this must leave both alone and let `duplicate_ids` keep reporting."""
        stale = self.config.projects_dir / "ON-HOLD" / "project-example-handoff.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("---\nid: handoff-project-example\ntype: document\n"
                         "status: current\n---\n\n# Other copy\n\nUNIQUE CONTENT\n")

        result = projectops.set_project_status(self.config, "project-example", "on-hold")

        self.assertIsNone(result.handoff_new_path)
        self.assertIn("UNIQUE CONTENT", stale.read_text())
        self.assertTrue((self.config.projects_dir / "ACTIVE" / "project-example-handoff.md").exists())


class TestAmbiguousIdWritesAreRefused(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for folder in ("ACTIVE", "ON-HOLD"):
            self.vault.write_note(f"30_PROJECTS/{folder}", "project-dup-handoff.md",
                                   id="handoff-project-dup", type="document", status="current",
                                   title=f"Handoff in {folder}", body=f"Unique to {folder}.")

    def tearDown(self):
        self.vault.cleanup()

    def test_find_note_paths_reports_both(self):
        paths = update.find_note_paths(self.config, "handoff-project-dup")
        self.assertEqual(len(paths), 2)

    def test_update_refuses_rather_than_picking_one(self):
        with self.assertRaises(update.AmbiguousNoteIdError) as ctx:
            update.update_memory(self.config, "handoff-project-dup", append_text="x")
        message = str(ctx.exception)
        self.assertIn("does not resolve to a single record", message)
        # The refusal must stay contentless — see TestAmbiguityRefusalLeaksNothing
        # for the full privacy matrix.
        self.assertNotIn("ACTIVE", message)
        self.assertNotIn("ON-HOLD", message)

    def test_neither_copy_was_modified_by_the_refused_write(self):
        before = {p: p.read_text() for p in update.find_note_paths(self.config, "handoff-project-dup")}
        with self.assertRaises(update.AmbiguousNoteIdError):
            update.update_memory(self.config, "handoff-project-dup", append_text="x")
        for path, text in before.items():
            self.assertEqual(path.read_text(), text)

    def test_a_unique_id_still_writes_normally(self):
        self.vault.write_note("60_KNOWLEDGE", "knowledge-ok.md", id="knowledge-ok",
                               type="knowledge", title="Fine")
        path = update.update_memory(self.config, "knowledge-ok", append_text="appended")
        self.assertIn("appended", path.read_text())

    def test_a_missing_id_still_raises_file_not_found_not_ambiguity(self):
        with self.assertRaises(FileNotFoundError):
            update.update_memory(self.config, "knowledge-nope", append_text="x")

    def test_reads_remain_possible(self):
        """Fail-closed on writes, not on reads: a duplicate must not make a
        record unreachable. `brain get`'s own warning is what keeps this from
        being silent (see TestDuplicateReadsAreNotSilent)."""
        self.assertIsNotNone(update.find_note_path(self.config, "handoff-project-dup"))


class TestDuplicateReadsAreNotSilent(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for folder in ("ACTIVE", "ON-HOLD"):
            self.vault.write_note(f"30_PROJECTS/{folder}", "project-dup-handoff.md",
                                   id="handoff-project-dup", type="document", status="current",
                                   title=f"Handoff in {folder}", body=f"Unique to {folder}.")

    def tearDown(self):
        self.vault.cleanup()

    def test_brain_get_warns_and_names_the_other_copy(self):
        import contextlib
        import io

        from brain import cli

        class Args:
            id = "handoff-project-dup"

        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = cli.cmd_get(self.config, Args())
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("claimed by 2 files", err.getvalue())
        self.assertIn("ON-HOLD", err.getvalue())

    def test_no_warning_for_a_unique_id(self):
        import contextlib
        import io

        from brain import cli

        self.vault.write_note("60_KNOWLEDGE", "knowledge-ok.md", id="knowledge-ok",
                               type="knowledge", title="Fine")

        class Args:
            id = "knowledge-ok"

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_get(self.config, Args())
        self.assertEqual(err.getvalue(), "")

    def test_the_warning_only_names_copies_the_caller_may_see(self):
        """An invisible second copy must not be announced — the warning would
        otherwise become a way to learn that a record exists under this id."""
        import contextlib
        import io

        from brain import cli

        self.vault.write_note("60_KNOWLEDGE", "knowledge-mine.md", id="knowledge-shared-id",
                               type="knowledge", title="Mine",
                               owner_principal="principal:marcin", audience=[])
        self.vault.write_note("20_AREAS", "knowledge-theirs.md", id="knowledge-shared-id",
                               type="knowledge", title="Theirs",
                               owner_principal="principal:ania", audience=[])

        class Args:
            id = "knowledge-shared-id"

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_get(self.config, Args())
        self.assertNotIn("WARNING", err.getvalue())


class TestDoctorChecks(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = _register(self.vault)

    def tearDown(self):
        self.vault.cleanup()

    def test_orphaned_handoff_is_a_deterministic_finding(self):
        stale = self.config.projects_dir / "ON-HOLD" / "project-example-handoff.md"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("---\nid: handoff-project-example\ntype: document\n---\n\n# x\n")

        problems = validate.check_handoff_in_wrong_status_folder(self.config)
        self.assertEqual([p.check for p in problems], ["handoff_in_wrong_status_folder"])
        self.assertIn("ON-HOLD", problems[0].message)
        self.assertIn("ACTIVE", problems[0].message)

    def test_a_correctly_placed_handoff_is_not_flagged(self):
        handoff.write(self.config, "project-example",
                      handoff.HandoffSections(attempted="x"), session_date="2026-09-19")
        self.assertEqual(validate.check_handoff_in_wrong_status_folder(self.config), [])

    def test_status_content_conflict_is_reported_for_a_closed_looking_proposed(self):
        self.vault.write_note("40_DECISIONS", "decision-incident.md",
                               id="decision-incident", type="decision", status="proposed",
                               title="Incident",
                               body="## Context\n\nx\n\n## Closure (2026-10-10)\n\n"
                                    "**Status: closed.** Fix deployed and verified.\n")
        notes, _ = validate.load_all_notes(self.config)
        problems = validate.check_status_content_conflict(self.config, notes)
        self.assertEqual([p.check for p in problems], ["status_content_conflict"])
        self.assertIn("TO RECONCILE", problems[0].message)

    def test_conflict_detection_never_changes_the_status(self):
        path = self.vault.write_note(
            "40_DECISIONS", "decision-incident.md",
            id="decision-incident", type="decision", status="proposed", title="Incident",
            body="## Closure (2026-10-10)\n\n**Status: closed.**\n")
        before = path.read_text()
        notes, _ = validate.load_all_notes(self.config)
        validate.check_status_content_conflict(self.config, notes)
        self.assertEqual(path.read_text(), before)

    def test_an_old_closed_mention_in_prose_is_not_treated_as_a_ruling(self):
        """Exactly the mistake the earlier report made: reading the word
        "closed" somewhere in a long record and calling the record closed."""
        self.vault.write_note(
            "40_DECISIONS", "decision-historical.md",
            id="decision-historical", type="decision", status="proposed", title="Open design",
            body="## Context\n\nStage 1H-A was closed on 2026-08-23; this stage is not. "
                 "The approve-and-act loop stays disabled and needs its own GO.\n")
        notes, _ = validate.load_all_notes(self.config)
        problems = validate.check_status_content_conflict(self.config, notes)
        self.assertEqual(problems, [])

    def test_a_decided_record_quoting_closure_is_not_flagged(self):
        self.vault.write_note("40_DECISIONS", "decision-done.md",
                               id="decision-done", type="decision", status="decided",
                               title="Done", body="## Closure\n\n**Status: closed.**\n")
        notes, _ = validate.load_all_notes(self.config)
        self.assertEqual(validate.check_status_content_conflict(self.config, notes), [])

    def test_the_heuristic_check_can_never_block_a_snapshot_or_backup(self):
        self.assertNotIn("status_content_conflict", BLOCKING_CHECKS)


if __name__ == "__main__":
    unittest.main()
