"""brain.decision — decision-record create/supersede, extracted from what
the /decision skill does today via direct Write/Edit."""
from __future__ import annotations

import dataclasses
import unittest

from brain import decision, frontmatter
from tests.helpers import TempVault


class TestCreateDecision(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_creates_record_at_expected_path(self):
        path = decision.create_decision(
            self.config, title="Use SQLite for the index",
            context="Need a search index.", decision="Use SQLite FTS5.",
            date="2026-09-26",
        )
        self.assertEqual(path.name, "decision-2026-09-26-use-sqlite-for-the-index.md")
        self.assertTrue(path.exists())
        note = frontmatter.parse_file(path)
        self.assertEqual(note.id, "decision-2026-09-26-use-sqlite-for-the-index")
        self.assertEqual(note.type, "decision")
        self.assertEqual(note.status, "proposed")
        self.assertIn("## Context", note.body)
        self.assertIn("Need a search index.", note.body)
        self.assertIn("Use SQLite FTS5.", note.body)

    def test_default_status_is_proposed(self):
        path = decision.create_decision(self.config, title="Test", date="2026-09-26")
        note = frontmatter.parse_file(path)
        self.assertEqual(note.status, "proposed")

    def test_explicit_status_decided(self):
        path = decision.create_decision(self.config, title="Test", status="decided", date="2026-09-26")
        note = frontmatter.parse_file(path)
        self.assertEqual(note.status, "decided")

    def test_invalid_status_rejected(self):
        with self.assertRaises(decision.DecisionError):
            decision.create_decision(self.config, title="Test", status="bogus", date="2026-09-26")

    def test_duplicate_id_rejected(self):
        decision.create_decision(self.config, title="Test", date="2026-09-26")
        with self.assertRaises(decision.DecisionError):
            decision.create_decision(self.config, title="Test", date="2026-09-26")

    def test_supersedes_marks_old_decision_and_links_forward(self):
        old_path = decision.create_decision(
            self.config, title="Old approach", status="decided", date="2026-01-01",
        )
        new_path = decision.create_decision(
            self.config, title="New approach", status="decided", date="2026-09-26",
            supersedes="decision-2026-01-01-old-approach",
        )
        old_note = frontmatter.parse_file(old_path)
        self.assertEqual(old_note.status, "superseded")
        self.assertIn("decision-2026-09-26-new-approach", old_note.body)

        new_note = frontmatter.parse_file(new_path)
        self.assertEqual(new_note.meta["supersedes"], "decision-2026-01-01-old-approach")

    def test_supersedes_never_touches_old_context_or_reasoning(self):
        old_path = decision.create_decision(
            self.config, title="Old approach", context="Original context.",
            reasoning="Original reasoning.", date="2026-01-01",
        )
        decision.create_decision(
            self.config, title="New approach", date="2026-09-26",
            supersedes="decision-2026-01-01-old-approach",
        )
        old_note = frontmatter.parse_file(old_path)
        self.assertIn("Original context.", old_note.body)
        self.assertIn("Original reasoning.", old_note.body)

    def test_supersedes_unknown_id_fails_cleanly_without_creating_anything(self):
        with self.assertRaises(decision.DecisionError):
            decision.create_decision(
                self.config, title="New approach", date="2026-09-26",
                supersedes="decision-does-not-exist",
            )
        dest = self.config.dir_for("decisions") / "decision-2026-09-26-new-approach.md"
        self.assertFalse(dest.exists())


class TestDecisionSetsOwnership(unittest.TestCase):
    """Stage 2 (multi-user visibility): same default-ownership-on-write
    rule as capture.capture — see tests/test_capture.py."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_owner_principal_is_the_acting_principal(self):
        config = dataclasses.replace(self.config, acting_principal="principal-ania")
        dest = decision.create_decision(config, title="A decision")
        note = frontmatter.parse_file(dest)
        self.assertEqual(note.meta["owner_principal"], "principal:ania")

    def test_audience_defaults_to_private(self):
        dest = decision.create_decision(self.config, title="A decision")
        note = frontmatter.parse_file(dest)
        self.assertEqual(note.meta["audience"], [])

    def test_explicit_audience_is_honored(self):
        dest = decision.create_decision(self.config, title="A decision",
                                         audience=["group:household"])
        note = frontmatter.parse_file(dest)
        self.assertEqual(note.meta["audience"], ["group:household"])


if __name__ == "__main__":
    unittest.main()
