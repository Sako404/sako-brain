"""Regression coverage for the v0.10.1 shared write-safety fix: every CLI
write command that carries free text must refuse a secret-shaped payload,
exactly like the in-process MCP server already did — closing the gap the
v0.10.0 capability audit found (mcp_server.py had this protection, the CLI
— and therefore mcp_bridge.py, which shells out to the CLI for every
write tool — did not). One test per command confirms the transport-level
wiring; the policy logic itself is covered in test_writepolicy.py."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli
from tests.helpers import TempVault

SECRET = "AKIAABCDEFGHIJKLMNOP"  # a known AWS-access-key-shaped pattern


def run_cli(argv, vault, stdin_data=None):
    out = io.StringIO()
    env = dict(os.environ)
    env["BRAIN_ROOT"] = str(vault.root)
    env["BRAIN_STATE_DIR"] = str(vault.state_dir)
    with patch.dict(os.environ, env, clear=False):
        with contextlib.redirect_stdout(out):
            if stdin_data is not None:
                with patch("sys.stdin", io.StringIO(stdin_data)):
                    rc = cli.main(argv)
            else:
                rc = cli.main(argv)
    return rc, out.getvalue()


class TestCliWriteCommandsRefuseSecrets(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_remember_refuses_secret_in_text(self):
        rc, _ = run_cli(["remember", "--type", "fact", "--title", "ok title",
                          "--text", f"key: {SECRET}"], self.vault)
        self.assertEqual(rc, 1)

    def test_remember_refuses_secret_in_title(self):
        rc, _ = run_cli(["remember", "--type", "fact", "--title", f"key {SECRET}"], self.vault)
        self.assertEqual(rc, 1)

    def test_update_refuses_secret_in_append_text(self):
        run_cli(["remember", "--type", "fact", "--title", "target note"], self.vault)
        note_id = [f for f in (self.vault.root / "00_INBOX").glob("*.md")][0].stem
        rc, _ = run_cli(["update", note_id, "--append-text", f"key: {SECRET}"], self.vault)
        self.assertEqual(rc, 1)

    def test_decision_create_refuses_secret_in_decision_body(self):
        rc, _ = run_cli(["decision", "create", "--title", "ok title",
                          "--decision", f"use key {SECRET}"], self.vault)
        self.assertEqual(rc, 1)

    def test_project_create_refuses_secret_in_name(self):
        rc, _ = run_cli(["project", "create", "--id", "project-x",
                          "--name", f"Project {SECRET}", "--path", "/tmp/x"], self.vault)
        self.assertEqual(rc, 1)

    def test_project_close_refuses_secret_in_summary(self):
        run_cli(["project", "create", "--id", "project-x", "--name", "Project X",
                  "--path", "/tmp/x"], self.vault)
        rc, _ = run_cli(["project", "close", "project-x", "--summary", f"leaked {SECRET}"], self.vault)
        self.assertEqual(rc, 1)

    def test_project_section_update_refuses_secret_in_content(self):
        run_cli(["project", "create", "--id", "project-x", "--name", "Project X",
                  "--path", "/tmp/x"], self.vault)
        path = self.vault.root / "30_PROJECTS" / "ACTIVE" / "project-x.md"
        path.write_text(path.read_text() + "\n## Next actions\n\nnothing yet\n")
        rc, _ = run_cli(["project", "section-update", "project-x", "--section", "Next actions",
                          "--mode", "append", "--content", f"key {SECRET}"], self.vault)
        self.assertEqual(rc, 1)

    def test_timeline_add_refuses_secret_in_what_happened(self):
        rc, _ = run_cli(["timeline", "add", "--title", "ok title", "--date", "2026-01-01",
                          "--what-happened", f"rotated {SECRET}"], self.vault)
        self.assertEqual(rc, 1)

    def test_note_create_refuses_secret_in_text(self):
        rc, _ = run_cli(["note", "create", "--type", "knowledge", "--title", "ok title",
                          "--text", f"key {SECRET}"], self.vault)
        self.assertEqual(rc, 1)

    def test_handoff_write_refuses_secret_in_payload(self):
        run_cli(["project", "create", "--id", "project-x", "--name", "Project X",
                  "--path", "/tmp/x"], self.vault)
        payload = json.dumps({"attempted": f"used key {SECRET}"})
        rc, _ = run_cli(["handoff", "write", "--project", "project-x"], self.vault, stdin_data=payload)
        self.assertEqual(rc, 1)


class TestCliWriteCommandsStillAllowSafeWrites(unittest.TestCase):
    """Sanity check that the new scan is not overzealous — ordinary,
    secret-free writes must keep working exactly as before."""

    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_remember_with_ordinary_text_succeeds(self):
        rc, _ = run_cli(["remember", "--type", "fact", "--title", "Postgres upgrade window",
                          "--text", "Sunday 02:00, no downtime expected."], self.vault)
        self.assertEqual(rc, 0)

    def test_decision_create_with_ordinary_text_succeeds(self):
        rc, _ = run_cli(["decision", "create", "--title", "Use widgets",
                          "--decision", "Go with the blue ones."], self.vault)
        self.assertEqual(rc, 0)


class TestCliRestrictedConfirmationParity(unittest.TestCase):
    """v0.10.1: --sensitivity restricted (or --set sensitivity=restricted)
    now requires --confirm-restricted at the CLI layer too — "a human
    typed this" is no longer implicit confirmation, since this same
    command is also how the MCP bridge writes on an automated caller's
    behalf. One positive + one negative test per affected command."""

    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_remember_restricted_without_confirmation_refused(self):
        rc, _ = run_cli(["remember", "--type", "fact", "--title", "x",
                          "--sensitivity", "restricted"], self.vault)
        self.assertEqual(rc, 1)

    def test_remember_restricted_with_confirmation_succeeds(self):
        rc, _ = run_cli(["remember", "--type", "fact", "--title", "x",
                          "--sensitivity", "restricted", "--confirm-restricted"], self.vault)
        self.assertEqual(rc, 0)

    def test_note_create_restricted_without_confirmation_refused(self):
        rc, _ = run_cli(["note", "create", "--type", "knowledge", "--title", "x",
                          "--sensitivity", "restricted"], self.vault)
        self.assertEqual(rc, 1)

    def test_note_create_restricted_with_confirmation_succeeds(self):
        rc, _ = run_cli(["note", "create", "--type", "knowledge", "--title", "x",
                          "--sensitivity", "restricted", "--confirm-restricted"], self.vault)
        self.assertEqual(rc, 0)

    def test_decision_create_restricted_without_confirmation_refused(self):
        rc, _ = run_cli(["decision", "create", "--title", "x", "--sensitivity", "restricted"], self.vault)
        self.assertEqual(rc, 1)

    def test_decision_create_restricted_with_confirmation_succeeds(self):
        rc, _ = run_cli(["decision", "create", "--title", "x", "--sensitivity", "restricted",
                          "--confirm-restricted"], self.vault)
        self.assertEqual(rc, 0)

    def test_timeline_add_restricted_without_confirmation_refused(self):
        rc, _ = run_cli(["timeline", "add", "--title", "x", "--date", "2026-01-01",
                          "--sensitivity", "restricted"], self.vault)
        self.assertEqual(rc, 1)

    def test_timeline_add_restricted_with_confirmation_succeeds(self):
        rc, _ = run_cli(["timeline", "add", "--title", "x", "--date", "2026-01-01",
                          "--sensitivity", "restricted", "--confirm-restricted"], self.vault)
        self.assertEqual(rc, 0)

    def test_update_restricted_set_without_confirmation_refused(self):
        run_cli(["remember", "--type", "fact", "--title", "target note"], self.vault)
        note_id = [f for f in (self.vault.root / "00_INBOX").glob("*.md")][0].stem
        rc, _ = run_cli(["update", note_id, "--set", "sensitivity=restricted"], self.vault)
        self.assertEqual(rc, 1)

    def test_update_restricted_set_with_confirmation_succeeds(self):
        run_cli(["remember", "--type", "fact", "--title", "target note"], self.vault)
        note_id = [f for f in (self.vault.root / "00_INBOX").glob("*.md")][0].stem
        rc, _ = run_cli(["update", note_id, "--set", "sensitivity=restricted",
                          "--confirm-restricted"], self.vault)
        self.assertEqual(rc, 0)

    def test_update_non_restricted_set_needs_no_confirmation(self):
        # Sanity: the gate is specific to sensitivity=restricted, not to
        # --set in general.
        run_cli(["remember", "--type", "fact", "--title", "target note"], self.vault)
        note_id = [f for f in (self.vault.root / "00_INBOX").glob("*.md")][0].stem
        rc, _ = run_cli(["update", note_id, "--set", "confidence=assumption"], self.vault)
        self.assertEqual(rc, 0)

    def test_project_update_restricted_set_without_confirmation_refused(self):
        run_cli(["project", "create", "--id", "project-x", "--name", "X", "--path", "/tmp/x"], self.vault)
        rc, _ = run_cli(["project", "update", "project-x", "--set", "sensitivity=restricted"], self.vault)
        self.assertEqual(rc, 1)

    def test_project_update_restricted_set_with_confirmation_succeeds(self):
        run_cli(["project", "create", "--id", "project-x", "--name", "X", "--path", "/tmp/x"], self.vault)
        rc, _ = run_cli(["project", "update", "project-x", "--set", "sensitivity=restricted",
                          "--confirm-restricted"], self.vault)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
