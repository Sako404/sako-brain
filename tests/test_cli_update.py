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


def run_cli(argv, vault, acting_principal=None):
    out = io.StringIO()
    env = dict(os.environ)
    env["BRAIN_ROOT"] = str(vault.root)
    env["BRAIN_STATE_DIR"] = str(vault.state_dir)
    if acting_principal:
        env["BRAIN_CALLER_PRINCIPAL"] = acting_principal
    else:
        env.pop("BRAIN_CALLER_PRINCIPAL", None)
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

    def test_repeated_set_flags_all_apply(self):
        # Regression: argparse's default "store" kept only the last --set,
        # silently dropping every earlier field of a multi-field update.
        rc, _ = run_cli(["update", "knowledge-widget", "--set", "status=current",
                         "--set", "source=manual", "--set", "confidence=fact"], self.vault)
        self.assertEqual(rc, 0)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["status"], "current")
        self.assertEqual(note.meta["source"], "manual")
        self.assertEqual(note.meta["confidence"], "fact")

    def test_set_json_writes_a_real_list(self):
        rc, _ = run_cli(["update", "knowledge-widget",
                         "--set-json", 'people=["person-a", "person-b"]',
                         "--set-json", 'tags=["x", "y"]', "--set", "status=current"], self.vault)
        self.assertEqual(rc, 0)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["people"], ["person-a", "person-b"])
        self.assertEqual(note.meta["tags"], ["x", "y"])
        self.assertEqual(note.meta["status"], "current")

    def test_set_json_rejects_invalid_json(self):
        rc, _ = run_cli(["update", "knowledge-widget", "--set-json", "tags=[not json"], self.vault)
        self.assertEqual(rc, 2)
        self.assertNotIn("tags: '[not json", self.path.read_text())

    def test_restricted_is_not_lost_among_other_fields(self):
        # With last-wins parsing, sensitivity=restricted followed by another
        # --set was silently dropped while the call reported success.
        rc, _ = run_cli(["update", "knowledge-widget", "--set", "sensitivity=restricted",
                         "--set", "status=current", "--confirm-restricted"], self.vault)
        self.assertEqual(rc, 0)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["sensitivity"], "restricted")
        self.assertEqual(note.meta["status"], "current")

    def test_unknown_id_fails_cleanly(self):
        rc, out = run_cli(["update", "knowledge-does-not-exist", "--set", "status=current"], self.vault)
        self.assertEqual(rc, 1)

    def test_malformed_set_argument_fails_cleanly(self):
        rc, out = run_cli(["update", "knowledge-widget", "--set", "not-a-key-value-pair"], self.vault)
        self.assertEqual(rc, 2)


class TestUpdateAudienceAuthorization(unittest.TestCase):
    """Stage 2 (multi-user visibility): changing WHO can see a record is a
    privilege that belongs to its owner alone — write access to a record's
    other fields does not imply authority to reshare it."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.path = self.vault.write_note(
            "60_KNOWLEDGE", "widget.md", id="knowledge-widget", type="knowledge",
            title="Widget notes", owner_principal="principal:marcin", audience=[],
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_owner_can_change_audience_via_dedicated_flag(self):
        rc, out = run_cli(["update", "knowledge-widget", "--audience", "group:household"],
                           self.vault, acting_principal="principal-marcin")
        self.assertEqual(rc, 0)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["audience"], ["group:household"])

    def test_owner_can_make_it_private_again_with_no_values(self):
        run_cli(["update", "knowledge-widget", "--audience", "group:household"],
                self.vault, acting_principal="principal-marcin")
        rc, out = run_cli(["update", "knowledge-widget", "--audience"],
                           self.vault, acting_principal="principal-marcin")
        self.assertEqual(rc, 0)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["audience"], [])

    def test_non_owner_is_refused_and_frontmatter_is_unchanged(self):
        rc, out = run_cli(["update", "knowledge-widget", "--audience", "group:household"],
                           self.vault, acting_principal="principal-ania")
        self.assertEqual(rc, 1)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["audience"], [])

    def test_unrelated_field_update_by_a_non_owner_is_also_refused(self):
        # Stage 2 V1 write rule, locked explicitly: owner_principal may
        # modify a record; audience grants READ visibility only — write
        # access is never implied by being in a record's audience, for
        # ANY field, not just owner_principal/audience themselves.
        rc, out = run_cli(["update", "knowledge-widget", "--set", "status=current"],
                           self.vault, acting_principal="principal-ania")
        self.assertEqual(rc, 1)
        note = frontmatter.parse_file(self.path)
        self.assertNotEqual(note.meta.get("status"), "current")

    def test_owner_can_update_unrelated_fields(self):
        rc, out = run_cli(["update", "knowledge-widget", "--set", "status=current"],
                           self.vault, acting_principal="principal-marcin")
        self.assertEqual(rc, 0)
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["status"], "current")


if __name__ == "__main__":
    unittest.main()


class TestWriteRefusalsAreAudited(unittest.TestCase):
    """Policy refusals used to leave no trace — only ACL denials were
    audited — so a client that reported "blocked" could not be checked."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note(
            "60_KNOWLEDGE", "widget.md", id="knowledge-widget", type="knowledge",
            title="Widget notes", body="Original body.",
        )

    def tearDown(self):
        self.vault.cleanup()

    def _refusals(self) -> list[str]:
        lines = []
        for log in self.config.logs_dir.glob("brain-audit-*.log"):
            lines += log.read_text(encoding="utf-8").splitlines()
        return [l for l in lines if "event=write.refused" in l]

    def test_unconfirmed_restricted_write_is_audited(self):
        rc, _ = run_cli(["update", "knowledge-widget", "--set", "sensitivity=restricted"],
                        self.vault, acting_principal="principal-marcin")
        self.assertEqual(rc, 1)
        refusals = self._refusals()
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn("principal=principal-marcin", refusals[0])
        self.assertIn("command=update reason=restricted_unconfirmed id=knowledge-widget", refusals[0])

    def test_secret_refusal_is_audited_without_the_secret(self):
        rc, _ = run_cli(["update", "knowledge-widget", "--append-text", "password: hunter2-zz9"],
                        self.vault)
        self.assertEqual(rc, 1)
        refusals = self._refusals()
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn("reason=secret_pattern:password assignment", refusals[0])
        self.assertNotIn("hunter2", refusals[0])

    def test_successful_write_logs_no_refusal(self):
        rc, _ = run_cli(["update", "knowledge-widget", "--set", "status=current"], self.vault)
        self.assertEqual(rc, 0)
        self.assertEqual(self._refusals(), [])
