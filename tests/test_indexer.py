import dataclasses
import shutil
import stat
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

    def test_count_by_type_before_index_built(self):
        self.assertEqual(indexer.count_by_type(self.config), {})

    def test_count_by_type_matches_grouped_totals(self):
        self.vault.write_note("60_KNOWLEDGE", "k.md", id="knowledge-k", type="knowledge")
        self.vault.write_note("10_PEOPLE", "p1.md", id="person-p1", type="person")
        self.vault.write_note("10_PEOPLE", "p2.md", id="person-p2", type="person")
        indexer.rebuild(self.config)
        counts = indexer.count_by_type(self.config)
        self.assertEqual(counts, {"knowledge": 1, "person": 2})
        self.assertEqual(sum(counts.values()), 3)

    def test_count_by_type_buckets_blank_type_as_none_label(self):
        path = self.vault.root / "60_KNOWLEDGE" / "no-type.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nid: knowledge-untyped\n---\n\n# Untyped\n")
        indexer.rebuild(self.config)
        self.assertEqual(indexer.count_by_type(self.config), {"(none)": 1})

    def test_count_inbox_pending_zero_when_inbox_empty(self):
        self.assertTrue(self.config.inbox_dir.exists())
        self.assertEqual(indexer.count_inbox_pending(self.config), 0)

    def test_count_inbox_pending_zero_when_inbox_missing(self):
        shutil.rmtree(self.config.inbox_dir)
        self.assertEqual(indexer.count_inbox_pending(self.config), 0)

    def test_count_inbox_pending_counts_markdown_files_only(self):
        inbox = self.config.inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "a.md").write_text("draft a\n")
        (inbox / "b.md").write_text("draft b\n")
        (inbox / "notes.txt").write_text("not markdown\n")
        self.assertEqual(indexer.count_inbox_pending(self.config), 2)

    def test_count_inbox_pending_raw_draft_with_no_frontmatter_defaults_to_marcin(self):
        # A raw capture with no frontmatter at all is NOT the same case as
        # a note whose frontmatter parsed but omitted owner_principal —
        # there's no meta dict to read. It must still count for the
        # default owner (principal-marcin), not vanish from everyone's
        # count including its own.
        inbox = self.config.inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "raw.md").write_text("just a raw draft, no frontmatter\n")
        self.assertEqual(indexer.count_inbox_pending(self.config, "principal-marcin"), 1)
        self.assertEqual(indexer.count_inbox_pending(self.config, "principal-ania"), 0)

    def test_db_file_not_world_or_group_readable(self):
        # The index holds full note bodies, restricted ones included —
        # same privacy reasoning as the MCP log files (see paths.py's
        # ensure_private_file docstring). sqlite3.connect() creates the
        # file under the process umask like any other open(), which is
        # commonly 022 (world-readable) regardless of directory permissions.
        self.vault.write_note("60_KNOWLEDGE", "k.md", id="knowledge-k", type="knowledge")
        indexer.rebuild(self.config)
        mode = self.config.db_path.stat().st_mode
        self.assertEqual(mode & stat.S_IRWXG, 0, "db file should not be group-accessible")
        self.assertEqual(mode & stat.S_IRWXO, 0, "db file should not be other-accessible")


class TestCountsAreVisibilityFiltered(unittest.TestCase):
    """Pre-onboarding hardening: `brain status`'s aggregate counts are
    themselves an information-leak surface — a non-admin principal could
    otherwise infer hidden record volume (e.g. "person: 7" shown to
    someone who can only see 2 of them reveals that 5 more exist) purely
    from the numbers, without ever reading a single title or snippet."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("10_PEOPLE", "p1.md", id="person-p1", type="person",
                               owner_principal="principal:marcin", audience=[])
        self.vault.write_note("10_PEOPLE", "p2.md", id="person-p2", type="person",
                               owner_principal="principal:marcin", audience=[])
        self.vault.write_note("10_PEOPLE", "p3.md", id="person-p3", type="person",
                               owner_principal="principal:marcin", audience=["principal:ania"])
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def _as(self, principal_id: str):
        return dataclasses.replace(self.config, acting_principal=principal_id)

    def test_owner_sees_the_full_count(self):
        self.assertEqual(indexer.count_by_type(self._as("principal-marcin")), {"person": 3})

    def test_non_owner_only_sees_the_count_of_what_was_shared_with_them(self):
        self.assertEqual(indexer.count_by_type(self._as("principal-ania")), {"person": 1})

    def test_unrelated_third_party_sees_nothing_not_even_a_zero_entry(self):
        # dict with no "person" key at all, not {"person": 0} — the TYPE
        # itself existing is not revealed either, only actual visible counts.
        self.assertEqual(indexer.count_by_type(self._as("principal-marcel")), {})

    def test_defaults_to_config_acting_principal_when_not_given_explicitly(self):
        self.assertEqual(indexer.count_by_type(self._as("principal-ania")), {"person": 1})


if __name__ == "__main__":
    unittest.main()
