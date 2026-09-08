import unittest

from brain import indexer, search
from tests.helpers import TempVault


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("60_KNOWLEDGE", "knowledge-sqlite.md",
                               id="knowledge-sqlite", type="knowledge",
                               title="SQLite FTS5", body="Full text search using SQLite FTS5 extension.")
        self.vault.write_note("60_KNOWLEDGE", "knowledge-yaml.md",
                               id="knowledge-yaml", type="knowledge",
                               title="YAML frontmatter", body="Notes use YAML frontmatter for metadata.")
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_search_finds_matching_note(self):
        results = search.search(self.config, "SQLite")
        ids = [r.id for r in results]
        self.assertIn("knowledge-sqlite", ids)
        self.assertNotIn("knowledge-yaml", ids)

    def test_search_no_match_returns_empty(self):
        results = search.search(self.config, "nonexistentxyz")
        self.assertEqual(results, [])

    def test_get_note_row(self):
        row = search.get_note_row(self.config, "knowledge-yaml")
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "YAML frontmatter")

    def test_get_note_row_missing_returns_none(self):
        self.assertIsNone(search.get_note_row(self.config, "does-not-exist"))


class TestSearchRanking(unittest.TestCase):
    """Phase 5A regression: a query matching a note's own title must not
    lose to short, incidental mentions in unrelated notes. Reproduces the
    real failure found against the live Brain (project-family-command-center
    ranked ~12th for the query "Family Command Center" before column
    weighting was added to bm25())."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

        # The "target": a long note whose title is an exact phrase match,
        # padded with unrelated body text (mirrors a real project record).
        filler = " ".join(f"unrelated{i} filler{i} content{i}" for i in range(150))
        self.vault.write_note(
            "30_PROJECTS/ACTIVE", "project-target.md",
            id="project-target", type="project", title="Widget Command Center",
            body=f"Widget Command Center is the project record. {filler}",
        )

        # Several short "distractor" notes that each mention the phrase
        # once in passing, with no title relevance at all.
        for i in range(5):
            self.vault.write_note(
                "10_PEOPLE", f"distractor-{i}.md",
                id=f"distractor-{i}", type="person", title=f"Person {i}",
                body=f"Person {i} uses the Widget Command Center sometimes.",
            )

        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_title_match_outranks_incidental_body_mentions(self):
        results = search.search(self.config, "Widget Command Center", limit=10)
        ids = [r.id for r in results]
        self.assertIn("project-target", ids)
        rank = ids.index("project-target") + 1
        self.assertLessEqual(rank, 2, f"expected project-target in the top 2, got rank {rank}: {ids}")


class TestNaturalLanguageQuestions(unittest.TestCase):
    """Phase 5B regression: a full question ("Where did X finish?") must
    not fail just because most of its words are function words that don't
    appear in the target note, or because one incidental content word
    (present by chance in unrelated notes) doesn't appear there either.
    A chat-style client (the Ollama demo, a future remote MCP client)
    sends full questions, not bare keywords — this must work for both."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note(
            "30_PROJECTS/ACTIVE", "project-target.md",
            id="project-target", type="project", title="Widget Command Center",
            body="Widget Command Center is the project record, fully shipped and stable.",
        )
        # A decoy containing a rare word ("finish") that must not let an
        # unrelated note dominate a question containing that word.
        self.vault.write_note(
            "60_KNOWLEDGE", "decoy.md", id="knowledge-decoy", type="knowledge",
            title="Unrelated topic", body="This note happens to use the word finish once.",
        )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_bare_keyword_query_still_works(self):
        results = search.search(self.config, "Widget Command Center", limit=5)
        ids = [r.id for r in results]
        self.assertIn("project-target", ids)

    def test_full_question_finds_the_right_note(self):
        results = search.search(self.config, "Where did Widget Command Center finish?", limit=5)
        ids = [r.id for r in results]
        self.assertIn("project-target", ids)

    def test_stopwords_alone_do_not_crash_or_hang(self):
        results = search.search(self.config, "what is the of", limit=5)
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()
