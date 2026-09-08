"""`brain context` — the CLI surface over get_context().

Added in OSS-1 because the v0.1 public scope ships the CLI and the text-based
agent rules, but not MCP. Without this command `get_context` — the one
abstraction built specifically so an agent can ground itself in a vault without
being handed full note bodies — would have no caller at all in v0.1.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli, indexer
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


class TestContextCommand(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note(
            "60_KNOWLEDGE", "kettle.md", id="knowledge-kettle", type="knowledge",
            title="Descaling the kettle", body="Vinegar and patience, mostly.",
        )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_json_output_is_valid_and_carries_provenance(self):
        rc, out = run_cli(["context", "descaling kettle", "--json"], self.vault)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["query"], "descaling kettle")
        self.assertIn("notes", payload)
        ids = [n["id"] for n in payload["notes"]]
        self.assertIn("knowledge-kettle", ids)
        note = next(n for n in payload["notes"] if n["id"] == "knowledge-kettle")
        for field in ("title", "status", "sensitivity", "source", "is_current"):
            self.assertIn(field, note)

    def test_json_output_never_contains_the_full_note_body(self):
        """get_context returns snippets and provenance, not note contents —
        that distinction is the whole reason it exists."""
        rc, out = run_cli(["context", "descaling kettle", "--json"], self.vault)
        self.assertEqual(rc, 0)
        note = next(n for n in json.loads(out)["notes"] if n["id"] == "knowledge-kettle")
        self.assertNotIn("body", note)
        self.assertNotIn("content", note)

    def test_human_output_is_not_json(self):
        rc, out = run_cli(["context", "descaling kettle"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("knowledge-kettle", out)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)

    def test_restricted_notes_are_excluded_unless_asked_for(self):
        path = self.vault.write_note(
            "20_AREAS", "secret.md", id="fact-restricted-thing", type="fact",
            title="Restricted thing", body="descaling secrets",
        )
        path.write_text(path.read_text().replace("sensitivity: normal", "sensitivity: restricted"))
        indexer.rebuild(self.config)

        _, default_out = run_cli(["context", "descaling", "--json"], self.vault)
        self.assertNotIn("fact-restricted-thing", [n["id"] for n in json.loads(default_out)["notes"]])

        _, opted_in = run_cli(["context", "descaling", "--json", "--restricted"], self.vault)
        self.assertIn("fact-restricted-thing", [n["id"] for n in json.loads(opted_in)["notes"]])

    def test_reports_missing_index_instead_of_crashing(self):
        self.config.db_path.unlink()
        rc, _ = run_cli(["context", "anything", "--json"], self.vault)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
