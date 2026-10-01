"""brain.migrate_stage2 — owner_principal/audience backfill for existing
records (SAKO Brain multi-user, Stage 2).

Covers the mandatory migration-testing protocol from the v0.13.0
production-database incident: fresh state AND a realistic pre-existing
state (several note types, some already migrated, some with partial
fields, one genuinely unparseable), idempotency (safe to re-run, safe
against a mixed/partially-migrated state as if a previous run had been
interrupted), dry-run changes nothing on disk, and that a full index
rebuild after migrating does not break.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli, frontmatter, indexer, migrate_stage2
from tests.helpers import TempVault


def run_cli(argv, vault):
    out = io.StringIO()
    env = dict(os.environ)
    env["BRAIN_ROOT"] = str(vault.root)
    env["BRAIN_STATE_DIR"] = str(vault.state_dir)
    with patch.dict(os.environ, env, clear=False):
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
    return rc, out.getvalue()


class TestFreshVault(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_empty_vault_is_a_clean_no_op(self):
        report = migrate_stage2.migrate_owner_principal(self.config)
        self.assertEqual(report.total_notes, 0)
        self.assertEqual(report.migrated, 0)
        self.assertEqual(report.already_migrated, 0)
        self.assertEqual(report.parse_errors, [])


class TestRealisticPreExistingVault(unittest.TestCase):
    """A vault shaped like one that has been in real use — several note
    types, some already carrying owner_principal (as if a previous,
    interrupted migration run had reached them, or Stage 2's own
    default-ownership-on-write had already produced them), one with a
    pre-existing `audience` but no owner_principal, and one genuinely
    unparseable file — never a clean, freshly-built fixture only."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

        self.vault.write_note("60_KNOWLEDGE", "unmigrated.md",
                               id="knowledge-unmigrated", type="knowledge", title="Needs migrating")
        self.vault.write_note("10_PEOPLE", "already-done.md",
                               id="person-already-done", type="person", title="Already migrated",
                               owner_principal="principal:marcin", audience=[])
        self.vault.write_note("60_KNOWLEDGE", "partial.md",
                               id="knowledge-partial", type="knowledge", title="Has audience only",
                               audience=["principal:ania"])
        self.vault.write_note("30_PROJECTS/ACTIVE", "project-x.md",
                               id="project-x", type="project", title="A project")

        self.broken_path = self.vault.root / "60_KNOWLEDGE" / "broken.md"
        self.broken_path.write_text("not valid frontmatter at all\n", encoding="utf-8")

    def tearDown(self):
        self.vault.cleanup()

    def test_counts_are_correct(self):
        report = migrate_stage2.migrate_owner_principal(self.config)
        # total_notes counts every candidate file iter_markdown_files finds,
        # including the one that fails to parse (5 = 4 well-formed + 1
        # broken) — parse_errors reports that one separately, never silently
        # folded into "migrated" or "already_migrated".
        self.assertEqual(report.total_notes, 5)
        self.assertEqual(report.already_migrated, 1)   # already-done.md
        self.assertEqual(report.migrated, 3)            # unmigrated, partial, project-x
        self.assertEqual(len(report.parse_errors), 1)
        self.assertIn("broken.md", str(report.parse_errors[0][0]))

    def test_unmigrated_note_gets_both_fields(self):
        migrate_stage2.migrate_owner_principal(self.config)
        note = frontmatter.parse_file(self.vault.root / "60_KNOWLEDGE" / "unmigrated.md")
        self.assertEqual(note.meta["owner_principal"], "principal:marcin")
        self.assertEqual(note.meta["audience"], [])

    def test_already_migrated_note_is_untouched_byte_for_byte(self):
        path = self.vault.root / "10_PEOPLE" / "already-done.md"
        before = path.read_text(encoding="utf-8")
        migrate_stage2.migrate_owner_principal(self.config)
        after = path.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_note_with_pre_existing_audience_keeps_it_not_overwritten(self):
        migrate_stage2.migrate_owner_principal(self.config)
        note = frontmatter.parse_file(self.vault.root / "60_KNOWLEDGE" / "partial.md")
        self.assertEqual(note.meta["owner_principal"], "principal:marcin")
        self.assertEqual(note.meta["audience"], ["principal:ania"])

    def test_broken_note_is_reported_not_migrated_not_crashed(self):
        report = migrate_stage2.migrate_owner_principal(self.config)
        self.assertEqual(report.migrated, 3)  # unaffected by the broken file
        text = self.broken_path.read_text(encoding="utf-8")
        self.assertEqual(text, "not valid frontmatter at all\n")  # untouched


class TestIdempotencyAndMixedState(unittest.TestCase):
    """Safe to re-run, and safe starting from a state as if a previous run
    had been interrupted partway (some notes already migrated, some not)
    — never assumes it's the first run."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for i in range(5):
            self.vault.write_note("60_KNOWLEDGE", f"note-{i}.md",
                                   id=f"knowledge-{i}", type="knowledge", title=f"Note {i}")
        # Simulate an interrupted prior run: two of the five already migrated.
        for note_id in ("knowledge-0", "knowledge-2"):
            path = next(p for p in (self.vault.root / "60_KNOWLEDGE").glob("*.md")
                        if frontmatter.parse_file(p).id == note_id)
            note = frontmatter.parse_file(path)
            note.meta["owner_principal"] = "principal:marcin"
            note.meta["audience"] = []
            path.write_text(frontmatter.render(note), encoding="utf-8")

    def tearDown(self):
        self.vault.cleanup()

    def test_first_run_from_mixed_state_migrates_only_the_remaining_ones(self):
        report = migrate_stage2.migrate_owner_principal(self.config)
        self.assertEqual(report.already_migrated, 2)
        self.assertEqual(report.migrated, 3)

    def test_second_run_is_a_complete_no_op(self):
        migrate_stage2.migrate_owner_principal(self.config)
        report = migrate_stage2.migrate_owner_principal(self.config)
        self.assertEqual(report.migrated, 0)
        self.assertEqual(report.already_migrated, 5)

    def test_file_contents_are_stable_across_repeated_runs(self):
        migrate_stage2.migrate_owner_principal(self.config)
        snapshots_after_first = {
            p: p.read_text(encoding="utf-8")
            for p in (self.vault.root / "60_KNOWLEDGE").glob("*.md")
        }
        migrate_stage2.migrate_owner_principal(self.config)
        for p, text in snapshots_after_first.items():
            self.assertEqual(p.read_text(encoding="utf-8"), text)


class TestDryRun(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.path = self.vault.write_note(
            "60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge", title="A")

    def tearDown(self):
        self.vault.cleanup()

    def test_dry_run_reports_what_would_change(self):
        report = migrate_stage2.migrate_owner_principal(self.config, dry_run=True)
        self.assertEqual(report.migrated, 1)
        self.assertEqual(report.migrated_paths, [self.path])

    def test_dry_run_writes_nothing_to_disk(self):
        before = self.path.read_text(encoding="utf-8")
        migrate_stage2.migrate_owner_principal(self.config, dry_run=True)
        after = self.path.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        note = frontmatter.parse_file(self.path)
        self.assertNotIn("owner_principal", note.meta)


class TestIndexRebuildAfterMigration(unittest.TestCase):
    """The v0.13.0 production incident's lesson, applied here: never
    assume a migration's interaction with the index/reindex path is fine
    just because the migration itself looks correct in isolation."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for i in range(3):
            self.vault.write_note("60_KNOWLEDGE", f"note-{i}.md",
                                   id=f"knowledge-{i}", type="knowledge", title=f"Note {i}")

    def tearDown(self):
        self.vault.cleanup()

    def test_full_rebuild_after_migration_does_not_crash_and_indexes_everything(self):
        migrate_stage2.migrate_owner_principal(self.config)
        stats = indexer.rebuild(self.config)
        self.assertEqual(stats["indexed"], 3)


class TestCliWiring(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.path = self.vault.write_note(
            "60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge", title="A")

    def tearDown(self):
        self.vault.cleanup()

    def test_default_is_dry_run_and_changes_nothing(self):
        rc, out = run_cli(["migrate-stage2-owner-principal"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Would migrate 1 of 1 notes", out)
        self.assertIn("dry run", out)
        note = frontmatter.parse_file(self.path)
        self.assertNotIn("owner_principal", note.meta)

    def test_apply_flag_actually_writes(self):
        rc, out = run_cli(["migrate-stage2-owner-principal", "--apply"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Migrated 1 of 1 notes", out)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["owner_principal"], "principal:marcin")

    def test_json_output_shape(self):
        rc, out = run_cli(["migrate-stage2-owner-principal", "--json"], self.vault)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["migrated"], 1)
        self.assertEqual(payload["total_notes"], 1)

    def test_parse_errors_produce_nonzero_exit(self):
        (self.vault.root / "60_KNOWLEDGE" / "broken.md").write_text("not frontmatter\n")
        rc, out = run_cli(["migrate-stage2-owner-principal"], self.vault)
        self.assertEqual(rc, 1)
        self.assertIn("could not be parsed", out)

if __name__ == "__main__":
    unittest.main()
