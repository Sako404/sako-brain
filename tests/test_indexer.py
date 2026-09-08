import unittest

from brain import indexer
from tests.helpers import TempVault


class TestIndexer(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_rebuild_indexes_valid_notes(self):
        self.vault.write_note("60_KNOWLEDGE", "knowledge-widgets.md",
                               id="knowledge-widgets", type="knowledge", title="Widgets", body="All about widgets.")
        self.vault.write_note("10_PEOPLE", "person-jane.md",
                               id="person-jane", type="person", title="Jane Doe", body="A colleague.")
        stats = indexer.rebuild(self.config)
        self.assertEqual(stats["indexed"], 2)
        self.assertEqual(stats["errors"], [])
        self.assertTrue(self.config.db_path.exists())

    def test_rebuild_reports_parse_errors_without_crashing(self):
        good = self.vault.write_note("60_KNOWLEDGE", "good.md", id="knowledge-good", type="knowledge")
        bad_path = self.vault.root / "60_KNOWLEDGE" / "bad.md"
        bad_path.write_text("no frontmatter at all\n")
        stats = indexer.rebuild(self.config)
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(len(stats["errors"]), 1)
        self.assertTrue(good.exists())

    def test_rebuild_skips_notes_missing_id(self):
        path = self.vault.root / "60_KNOWLEDGE" / "no-id.md"
        path.write_text("---\ntype: knowledge\n---\n\n# No id\n")
        stats = indexer.rebuild(self.config)
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(len(stats["errors"]), 1)

    def test_rebuild_is_idempotent(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        indexer.rebuild(self.config)
        stats = indexer.rebuild(self.config)
        self.assertEqual(stats["indexed"], 1)

    def test_rebuild_stores_empty_string_not_literal_none_for_blank_optional_fields(self):
        # Phase 5B regression: a YAML key present but with no value (e.g.
        # "valid_to:" with nothing after it, common in our own templates)
        # used to be stored as the literal text "None" rather than "".
        path = self.vault.root / "60_KNOWLEDGE" / "f.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "id: fact-blank-fields\n"
            "type: fact\n"
            "source:\n"
            "source_date:\n"
            "valid_from:\n"
            "valid_to:\n"
            "---\n\n# Example\n"
        )
        indexer.rebuild(self.config)
        conn = indexer.connect(self.config)
        row = conn.execute("SELECT * FROM notes WHERE id = 'fact-blank-fields'").fetchone()
        conn.close()
        for field in ("source", "source_date", "valid_from", "valid_to"):
            self.assertEqual(row[field], "", f"{field} should be '', got {row[field]!r}")

    def test_rebuild_indexes_provenance_fields(self):
        path = self.vault.root / "60_KNOWLEDGE" / "f.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "id: fact-example\n"
            "type: fact\n"
            "status: superseded\n"
            "source: chatgpt\n"
            "source_date: 2026-07-27\n"
            "valid_from: 2026-01-01\n"
            "valid_to: 2026-06-01\n"
            "supersedes: fact-older\n"
            "---\n\n"
            "# Example\n"
        )
        indexer.rebuild(self.config)
        conn = indexer.connect(self.config)
        row = conn.execute("SELECT * FROM notes WHERE id = 'fact-example'").fetchone()
        conn.close()
        self.assertEqual(row["source"], "chatgpt")
        self.assertEqual(row["source_date"], "2026-07-27")
        self.assertEqual(row["valid_from"], "2026-01-01")
        self.assertEqual(row["valid_to"], "2026-06-01")
        self.assertEqual(row["supersedes"], "fact-older")

    def test_rebuild_indexes_supersedes_list(self):
        path = self.vault.root / "60_KNOWLEDGE" / "f.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: fact-example\ntype: fact\nsupersedes: [fact-a, fact-b]\n---\n\n# Example\n"
        )
        indexer.rebuild(self.config)
        conn = indexer.connect(self.config)
        row = conn.execute("SELECT * FROM notes WHERE id = 'fact-example'").fetchone()
        conn.close()
        self.assertEqual(row["supersedes"], "fact-a,fact-b")


if __name__ == "__main__":
    unittest.main()
