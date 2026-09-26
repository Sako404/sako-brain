"""`brain update` — CLI surface over update_mod.update_memory(), the exact
function the MCP update_memory tool already calls. No new logic: these tests
lock that the CLI composes it correctly, not a second implementation.
"""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from brain import cli, frontmatter
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


class TestUpdateCommand(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.path = self.vault.write_note(
            "60_KNOWLEDGE", "widget.md", id="knowledge-widget", type="knowledge",
            title="Widget notes", body="Original body.",
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_set_field_updates_frontmatter(self):
        rc, out = run_cli(["update", "knowledge-widget", "--set", "status=current"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Updated", out)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["status"], "current")

    def test_append_text_adds_dated_section(self):
        rc, out = run_cli(["update", "knowledge-widget", "--append-text", "New detail."], self.vault)
        self.assertEqual(rc, 0)
        note = frontmatter.parse_file(self.path)
        self.assertIn("## Update (", note.body)
        self.assertIn("New detail.", note.body)
        self.assertIn("Original body.", note.body)  # never deletes prior content

    def test_id_and_created_are_never_mutated(self):
        run_cli(["update", "knowledge-widget", "--set", "id=something-else", "--set", "created=2000-01-01"], self.vault)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.id, "knowledge-widget")
        self.assertNotEqual(note.meta.get("created"), "2000-01-01")

    def test_unknown_id_fails_cleanly(self):
        rc, out = run_cli(["update", "knowledge-does-not-exist", "--set", "status=current"], self.vault)
        self.assertEqual(rc, 1)

    def test_malformed_set_argument_fails_cleanly(self):
        rc, out = run_cli(["update", "knowledge-widget", "--set", "not-a-key-value-pair"], self.vault)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
