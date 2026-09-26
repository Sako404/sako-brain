"""CLI surface over projectops/decision — locks in argparse wiring (flags,
exit codes, dispatch), not the business logic itself (see test_projectops.py
/ test_decision.py for that)."""
from __future__ import annotations

import contextlib
import io
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

    def tearDown(self):
        self.vault.cleanup()

    def test_show(self):
        rc, out = run_cli(["project", "show", "project-example"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Example Project", out)

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
