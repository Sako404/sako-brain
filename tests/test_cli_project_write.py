"""CLI surface over projectops/decision — locks in argparse wiring (flags,
exit codes, dispatch), not the business logic itself (see test_projectops.py
/ test_decision.py for that)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli, frontmatter
from brain.registry import load_registry
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


class TestProjectShowDiscoverSyncStillWork(unittest.TestCase):
    """The pre-existing flat-positional form became explicit subcommands —
    confirm the renamed paths still do what they did before."""

    def setUp(self):
        self.vault = TempVault()
        self.vault.config().registry_path.write_text(
            "projects:\n"
            "  - id: project-example\n"
            "    name: Example Project\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )
        # Stage 2: a registry entry's visibility is decided by its backing
        # note's own frontmatter — a row with no corresponding note fails
        # closed, so this fixture needs one.
        self.vault.write_note("30_PROJECTS/ACTIVE", "project-example.md", id="project-example", type="project")

    def tearDown(self):
        self.vault.cleanup()

    def test_show(self):
        rc, out = run_cli(["project", "show", "project-example"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Example Project", out)

    def test_show_json_carries_current_state(self):
        import json
        self.vault.write_note("30_PROJECTS/ACTIVE", "project-example.md", id="project-example",
                               type="project", body="## Current state\n\nRunning fine.\n")
        rc, out = run_cli(["project", "show", "project-example", "--json"], self.vault)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["current_state"], {"text": "Running fine.", "updated": ""})

    def test_show_text_prints_current_state(self):
        self.vault.write_note("30_PROJECTS/ACTIVE", "project-example.md", id="project-example",
                               type="project", body="## Current state\n\nRunning fine.\n")
        rc, out = run_cli(["project", "show", "project-example"], self.vault)
        self.assertIn("Current state (updated never):\nRunning fine.", out)

    def test_discover_routes_correctly(self):
        # run_cli() drives the CLI through env vars only (BRAIN_ROOT/BRAIN_STATE_DIR),
        # not vault.config()'s projects_roots, so a fresh vault's 90_SYSTEM/config.yaml
        # has none configured — this just exercises that 'discover' reaches
        # cmd_project_discover and gets its real, already-tested-elsewhere behavior.
        rc, out = run_cli(["project", "discover"], self.vault)
        self.assertEqual(rc, 1)  # message goes to stderr, not captured here


class TestCliProjectCreate(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_creates_project(self):
        rc, out = run_cli(
            ["project", "create", "--id", "project-widget", "--name", "Widget",
             "--path", "/tmp/example-widget"],
            self.vault,
        )
        self.assertEqual(rc, 0)
        self.assertIn("Created", out)
        entries = {e.id: e for e in load_registry(self.config)}
        self.assertIn("project-widget", entries)

    def test_duplicate_id_fails_cleanly(self):
        run_cli(["project", "create", "--id", "project-widget", "--name", "Widget", "--path", "/a"], self.vault)
        rc, out = run_cli(["project", "create", "--id", "project-widget", "--name", "Widget2", "--path", "/b"], self.vault)
        self.assertEqual(rc, 1)


class TestCliProjectUpdate(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        run_cli(["project", "create", "--id", "project-widget", "--name", "Widget", "--path", "/a"], self.vault)

    def tearDown(self):
        self.vault.cleanup()

    def test_status_change_moves_and_syncs_registry(self):
        rc, out = run_cli(["project", "update", "project-widget", "--status", "on-hold"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("active -> on-hold", out)
        entries = {e.id: e for e in load_registry(self.config)}
        self.assertEqual(entries["project-widget"].status, "on-hold")

    def test_set_status_via_generic_set_is_refused(self):
        rc, out = run_cli(["project", "update", "project-widget", "--set", "status=on-hold"], self.vault)
        self.assertEqual(rc, 2)

    def test_set_other_field_and_append_text(self):
        rc, out = run_cli(
            ["project", "update", "project-widget", "--set", "category=infra", "--append-text", "New note."],
            self.vault,
        )
        self.assertEqual(rc, 0)

    def test_nothing_to_do_is_a_clean_error(self):
        rc, out = run_cli(["project", "update", "project-widget"], self.vault)
        self.assertEqual(rc, 2)

    def test_path_change_syncs_record_and_registry(self):
        rc, out = run_cli(["project", "update", "project-widget", "--path", "/b", "--json"], self.vault)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["path_change"]["old_path"], "/a")
        self.assertEqual(payload["path_change"]["new_path"], "/b")
        self.assertTrue(payload["path_change"]["changed"])
        entries = {e.id: e for e in load_registry(self.config)}
        self.assertEqual(entries["project-widget"].path, "/b")
        record = self.config.projects_dir / "ACTIVE" / "project-widget.md"
        self.assertEqual(frontmatter.parse_file(record).meta["path"], "/b")

    def test_set_path_via_generic_set_is_refused(self):
        rc, out = run_cli(["project", "update", "project-widget", "--set", "path=/b"], self.vault)
        self.assertEqual(rc, 2)
        entries = {e.id: e for e in load_registry(self.config)}
        self.assertEqual(entries["project-widget"].path, "/a")

    def test_relative_path_fails_cleanly(self):
        rc, out = run_cli(["project", "update", "project-widget", "--path", "b"], self.vault)
        self.assertEqual(rc, 1)

    def test_rename_syncs_listing_title_and_alias(self):
        rc, out = run_cli(["project", "update", "project-widget", "--name", "Gadget",
                           "--add-alias", "gadget", "--json"], self.vault)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["name_change"]["old_name"], "Widget")
        self.assertEqual(payload["name_change"]["new_name"], "Gadget")
        self.assertEqual(payload["name_change"]["added_aliases"], ["gadget"])
        entry = {e.id: e for e in load_registry(self.config)}["project-widget"]
        self.assertEqual(entry.name, "Gadget")
        self.assertIn("gadget", entry.aliases)
        record = frontmatter.parse_file(self.config.projects_dir / "ACTIVE" / "project-widget.md")
        self.assertEqual(record.title, "Gadget")
        self.assertIn("gadget", record.meta["aliases"])
        rc, out = run_cli(["project", "show", "gadget"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Gadget", out)

    def test_generic_set_cannot_fake_a_rename(self):
        record_path = self.config.projects_dir / "ACTIVE" / "project-widget.md"
        before = record_path.read_text(encoding="utf-8")
        for argv in (["project", "update", "project-widget", "--set", "name=Gadget"],
                     ["project", "update", "project-widget", "--set", "title=Gadget"],
                     ["update", "project-widget", "--set", "name=Gadget"],
                     ["update", "project-widget", "--set", "title=Gadget"],
                     ["update", "project-widget", "--set-json", 'aliases=["gadget"]']):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc, _ = run_cli(argv, self.vault)
                self.assertEqual(rc, 1)
                self.assertIn("--add-alias" if "aliases" in argv[-1] else "--name", err.getvalue())
        self.assertEqual(record_path.read_text(encoding="utf-8"), before)

    def test_aliases_on_other_notes_are_still_settable(self):
        run_cli(["note", "create", "--type", "knowledge", "--title", "Plain note"], self.vault)
        rc, _ = run_cli(["update", "knowledge-plain-note", "--set-json", 'aliases=["plain"]'], self.vault)
        self.assertEqual(rc, 0)

    def test_remove_alias_through_the_cli(self):
        run_cli(["project", "update", "project-widget", "--add-alias", "gadget"], self.vault)
        rc, out = run_cli(["project", "update", "project-widget", "--remove-alias", "gadget", "--json"], self.vault)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["name_change"]["removed_aliases"], ["gadget"])
        rc, _ = run_cli(["project", "show", "gadget"], self.vault)
        self.assertNotEqual(rc, 0)

    def test_rename_secret_is_refused(self):
        secret = "AKIAABCDEFGHIJKLMNOP"  # AWS-key-shaped, see test_cli_write_security
        for flag in ("--name", "--add-alias"):
            rc, _ = run_cli(["project", "update", "project-widget", flag, secret], self.vault)
            self.assertEqual(rc, 1)
        entry = {e.id: e for e in load_registry(self.config)}["project-widget"]
        self.assertEqual(entry.name, "Widget")


class TestCliNoteTitle(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        rc, out = run_cli(["note", "create", "--type", "knowledge", "--title", "Plain note", "--json"], self.vault)
        self.assertEqual(rc, 0)
        self.note_id = "knowledge-plain-note"
        run_cli(["project", "create", "--id", "project-widget", "--name", "Widget", "--path", "/a"], self.vault)

    def _note(self):
        return frontmatter.parse_file(self.vault.root / "60_KNOWLEDGE" / f"{self.note_id}.md")

    def _run_err(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, _ = run_cli(argv, self.vault)
        return rc, err.getvalue()

    def test_title_flag_changes_the_heading_and_search(self):
        rc, _ = run_cli(["update", self.note_id, "--title", "Better name"], self.vault)
        self.assertEqual(rc, 0)
        self.assertEqual(self._note().title, "Better name")
        rc, out = run_cli(["search", "Better name"], self.vault)
        self.assertIn(self.note_id, out)

    def test_set_title_is_refused_and_points_to_the_flag(self):
        before = self._note().body
        rc, err = self._run_err(["update", self.note_id, "--set", "title=Better name"])
        self.assertEqual(rc, 1)
        self.assertIn("--title", err)
        self.assertEqual(self._note().body, before)
        self.assertNotIn("title", self._note().meta)

    def test_resetting_an_existing_title_field_stays_allowed(self):
        path = self.vault.root / "60_KNOWLEDGE" / f"{self.note_id}.md"
        note = frontmatter.parse_file(path)
        note.meta["title"] = "Old field"
        path.write_text(frontmatter.render(note), encoding="utf-8")
        rc, _ = run_cli(["update", self.note_id, "--set", "title=Old field"], self.vault)
        self.assertEqual(rc, 0)

    def test_title_flag_on_a_project_points_to_name(self):
        rc, err = self._run_err(["update", "project-widget", "--title", "Gadget"])
        self.assertEqual(rc, 1)
        self.assertIn("--name", err)
        self.assertEqual({e.id: e for e in load_registry(self.config)}["project-widget"].name, "Widget")

    def test_title_must_be_one_line_and_free_of_secrets(self):
        before = self._note().body
        for bad in ("", "Two\nlines", "AKIAABCDEFGHIJKLMNOP"):
            with self.subTest(bad=bad):
                rc, _ = self._run_err(["update", self.note_id, "--title", bad])
                self.assertEqual(rc, 1)
        self.assertEqual(self._note().body, before)


class TestCliProjectClose(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        run_cli(["project", "create", "--id", "project-widget", "--name", "Widget", "--path", "/a"], self.vault)

    def tearDown(self):
        self.vault.cleanup()

    def test_closes_project(self):
        rc, out = run_cli(["project", "close", "project-widget", "--summary", "Done."], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("archived", out)


class TestCliDecisionCreate(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_creates_decision(self):
        rc, out = run_cli(
            ["decision", "create", "--title", "Use SQLite", "--decision", "Use SQLite FTS5."],
            self.vault,
        )
        self.assertEqual(rc, 0)
        self.assertIn("Created", out)

    def test_invalid_status_fails_cleanly(self):
        rc, out = run_cli(["decision", "create", "--title", "X", "--status", "bogus"], self.vault)
        self.assertEqual(rc, 1)

    def test_supersedes_unknown_id_fails_cleanly(self):
        rc, out = run_cli(["decision", "create", "--title", "X", "--supersedes", "decision-nope"], self.vault)
        self.assertEqual(rc, 1)


class TestCliProjectSectionUpdate(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        run_cli(["project", "create", "--id", "project-widget", "--name", "Widget", "--path", "/a"], self.vault)

    def tearDown(self):
        self.vault.cleanup()

    def test_replace(self):
        rc, out = run_cli(
            ["project", "section-update", "project-widget", "--section", "Current state",
             "--mode", "replace", "--content", "All good."],
            self.vault,
        )
        self.assertEqual(rc, 0)
        self.assertIn("Updated section", out)

    def test_append(self):
        rc, out = run_cli(
            ["project", "section-update", "project-widget", "--section", "Milestones",
             "--mode", "append", "--content", "v1 shipped."],
            self.vault,
        )
        self.assertEqual(rc, 0)

    def test_unknown_section_fails_cleanly(self):
        rc, out = run_cli(
            ["project", "section-update", "project-widget", "--section", "Purpose",
             "--mode", "replace", "--content", "x"],
            self.vault,
        )
        self.assertEqual(rc, 1)

    def test_bad_mode_rejected_by_argparse(self):
        # --mode has a fixed `choices=`, so argparse itself refuses an
        # invalid value via SystemExit(2), before any business logic runs.
        with self.assertRaises(SystemExit) as ctx:
            run_cli(
                ["project", "section-update", "project-widget", "--section", "Current state",
                 "--mode", "delete", "--content", "x"],
                self.vault,
            )
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
