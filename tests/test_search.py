import unittest
from unittest import mock

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


class TestTitleTier(unittest.TestCase):
    """bm25() normalizes by the whole row's length, so a long enough body
    sinks a title match despite TITLE_WEIGHT. Reproduces the live failure:
    the backlog document titled "Cross-project next actions" ranked 12th for
    "next actions" behind short handoffs with a "Next actions" heading."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        filler = " ".join(f"unrelated{i} filler{i} content{i}" for i in range(1500))
        self.vault.write_note(
            "30_PROJECTS", "_next-actions.md", id="document-target", type="document",
            title="Cross-project next actions", body=f"The list. {filler}",
        )
        for i in range(12):
            self.vault.write_note(
                "30_PROJECTS/ACTIVE", f"handoff-{i}.md", id=f"handoff-{i}", type="document",
                title=f"Handoff {i}",
                body=f"## Next actions\n\nNext logical action for {i}.",
            )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_long_note_whose_title_has_every_term_ranks_first(self):
        ids = [r.id for r in search.search(self.config, "next actions", limit=8)]
        self.assertEqual(ids[0], "document-target", ids)

    def test_single_term_title_match_ranks_first(self):
        ids = [r.id for r in search.search(self.config, "cross-project", limit=8)]
        self.assertEqual(ids[0], "document-target", ids)

    def test_body_matches_still_follow_the_title_match(self):
        ids = [r.id for r in search.search(self.config, "next actions", limit=20)]
        self.assertEqual(sorted(ids[1:]), sorted(f"handoff-{i}" for i in range(12)))

    def test_shortest_title_wins_among_title_matches(self):
        # Live case: "next"* also matches "Nextcloud", and these short notes
        # beat the long backlog on bm25() inside the title tier.
        for i in range(3):
            self.vault.write_note(
                "50_TIMELINE", f"event-{i}.md", id=f"event-{i}", type="event",
                title=f"Nextcloud domain migration {i}: partial execution, two human actions blocking",
                body="Short.",
            )
        indexer.rebuild(self.config)
        ids = [r.id for r in search.search(self.config, "next actions", limit=8)]
        self.assertEqual(ids[0], "document-target", ids)
        self.assertEqual(sorted(ids[1:4]), ["event-0", "event-1", "event-2"])

    def test_partial_title_match_does_not_jump_the_queue(self):
        # "next" is in the title but "logical" is not: no title-tier hit,
        # so ordering falls back to the content tiers.
        ids = [r.id for r in search.search(self.config, "next logical", limit=8)]
        self.assertNotIn("document-target", ids[:1])


class TestAliases(unittest.TestCase):
    """A note's `aliases` are names it goes by, so searching for one must
    find it the way its title would — live, the backlog's alias
    `next-actions` and project-family-command-center's alias `fcc` were
    not indexed at all."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        path = self.vault.root / "30_PROJECTS" / "ACTIVE" / "project-fcc.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        filler = " ".join(f"unrelated{i}" for i in range(1500))
        path.write_text(
            "---\nid: project-fcc\ntype: project\nstatus: active\n"
            "aliases:\n- fcc\n- household-dashboard\n---\n\n"
            f"# Family Command Center\n\n{filler}\n"
        )
        for i in range(5):
            self.vault.write_note(
                "50_TIMELINE", f"event-{i}.md", id=f"event-{i}", type="event",
                title=f"Event {i}", body=f"The FCC did a thing, round {i}.",
            )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_alias_finds_the_note_first(self):
        ids = [r.id for r in search.search(self.config, "fcc", limit=8)]
        self.assertEqual(ids[0], "project-fcc", ids)

    def test_hyphenated_alias_matches(self):
        ids = [r.id for r in search.search(self.config, "household-dashboard", limit=8)]
        self.assertEqual(ids[0], "project-fcc", ids)

    def test_displayed_title_is_the_title_alone(self):
        result = search.search(self.config, "fcc", limit=1)[0]
        self.assertEqual(result.title, "Family Command Center")

    def test_note_without_aliases_still_indexes(self):
        ids = [r.id for r in search.search(self.config, "Event 3", limit=8)]
        self.assertEqual(ids[0], "event-3", ids)


class TestRelaxedTitleTier(unittest.TestCase):
    """Live case: "Where did Family Command Center finish?" put a RomM
    handoff first — a long note mentioning the project in one place and
    "finish" in another matched every term, while no title holds "finish"
    and the project record came after all such notes."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        filler = " ".join(f"unrelated{i}" for i in range(1500))
        self.vault.write_note(
            "30_PROJECTS/ACTIVE", "project-wcc.md", id="project-wcc", type="project",
            title="Widget Command Center", body=f"The project record. {filler}",
        )
        for i in range(6):
            self.vault.write_note(
                "30_PROJECTS/ACTIVE", f"handoff-other-{i}.md", id=f"handoff-other-{i}",
                type="document", title=f"Handoff — Other {i}",
                body=f"We finish the scan today. Unlike the Widget Command Center, this one {i}.",
            )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_question_puts_the_subject_first(self):
        ids = [r.id for r in search.search(self.config, "Where did Widget Command Center finish?", limit=8)]
        self.assertEqual(ids[0], "project-wcc", ids)

    def test_two_terms_are_not_relaxed_to_one_title_word(self):
        # "Gadget finish": a note with "Gadget" inside a longer title and no
        # "finish" anywhere must not jump above a note that holds both
        # words. (A note whose whole title is "Gadget" may — that is the
        # whole-title tier, see TestWholeTitleTier.)
        self.vault.write_note(
            "60_KNOWLEDGE", "gadget.md", id="knowledge-gadget", type="knowledge",
            title="Gadget supply notes", body="Nothing else here.",
        )
        self.vault.write_note(
            "60_KNOWLEDGE", "k.md", id="knowledge-both", type="knowledge",
            title="Notes", body="The gadget team will finish late.",
        )
        indexer.rebuild(self.config)
        ids = [r.id for r in search.search(self.config, "Gadget finish", limit=20)]
        self.assertLess(ids.index("knowledge-both"), ids.index("knowledge-gadget"), ids)


class TestWholeTitleTier(unittest.TestCase):
    """Live case: "What is the status of TRON?" (status, TRON) did not put
    project-tron — titled just "TRON" — first; long decisions with both
    words in the body won."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        filler = " ".join(f"unrelated{i}" for i in range(1500))
        self.vault.write_note(
            "30_PROJECTS/ACTIVE", "project-gizmo.md", id="project-gizmo", type="project",
            title="GIZMO", body=f"The project record. {filler}",
        )
        for i in range(6):
            self.vault.write_note(
                "40_DECISIONS", f"decision-{i}.md", id=f"decision-{i}", type="decision",
                title=f"Decision {i}: GIZMO stage {i}", body=f"The status of stage {i} is fine.",
            )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_two_term_question_puts_the_subject_first(self):
        ids = [r.id for r in search.search(self.config, "What is the status of GIZMO?", limit=8)]
        self.assertEqual(ids[0], "project-gizmo", ids)

    def test_works_without_an_english_word_list(self):
        ids = [r.id for r in search.search(self.config, "Jaki jest status GIZMO?", limit=8)]
        self.assertEqual(ids[0], "project-gizmo", ids)

    def test_title_holding_both_terms_still_comes_first(self):
        self.vault.write_note(
            "40_DECISIONS", "outage.md", id="decision-outage", type="decision",
            title="GIZMO outage", body="Short.",
        )
        indexer.rebuild(self.config)
        ids = [r.id for r in search.search(self.config, "GIZMO outage", limit=8)]
        self.assertEqual(ids[:2], ["decision-outage", "project-gizmo"], ids)

    def test_a_word_inside_a_longer_title_is_not_a_whole_title(self):
        # "stage" is in every decision title but is no decision's whole
        # title, so nothing is promoted on its account.
        ids = [r.id for r in search.search(self.config, "stage fine", limit=8)]
        self.assertNotIn("project-gizmo", ids)


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


class TestSearchVisibility(unittest.TestCase):
    """Stage 2 (multi-user visibility): search must never surface the
    existence, title, or snippet of a note the caller may not see —
    candidate ids -> live authoritative visibility check -> allowed
    candidates only -> materialize. Default acting_principal is
    principal-marcin, matching every note's own default owner, so tests
    that want a DIFFERENT caller override via dataclasses.replace."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("60_KNOWLEDGE", "knowledge-private.md",
                               id="knowledge-private", type="knowledge",
                               title="Marcin's Private Widget Notes",
                               body="Only marcin should ever see this widget content.",
                               owner_principal="principal:marcin", audience=[])
        self.vault.write_note("60_KNOWLEDGE", "knowledge-shared.md",
                               id="knowledge-shared", type="knowledge",
                               title="Shared Widget Notes",
                               body="Marcin and ania can both see this widget content.",
                               owner_principal="principal:marcin", audience=["principal:ania"])
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def _as(self, principal_id: str):
        import dataclasses
        return dataclasses.replace(self.config, acting_principal=principal_id)

    def test_owner_sees_their_own_private_note(self):
        results = search.search(self._as("principal-marcin"), "widget")
        ids = [r.id for r in results]
        self.assertIn("knowledge-private", ids)
        self.assertIn("knowledge-shared", ids)

    def test_non_owner_never_sees_the_private_note_in_results(self):
        results = search.search(self._as("principal-ania"), "widget")
        ids = [r.id for r in results]
        self.assertNotIn("knowledge-private", ids)

    def test_explicit_audience_principal_sees_the_shared_note(self):
        results = search.search(self._as("principal-ania"), "widget")
        ids = [r.id for r in results]
        self.assertIn("knowledge-shared", ids)

    def test_unrelated_third_party_sees_neither_note(self):
        results = search.search(self._as("principal-marcel"), "widget")
        ids = [r.id for r in results]
        self.assertNotIn("knowledge-private", ids)
        self.assertNotIn("knowledge-shared", ids)

    def test_candidate_fetch_overfetches_so_filtering_can_still_fill_limit(self):
        # If search() only ever pulled exactly `limit` raw candidates before
        # filtering, a caller whose visible notes rank below the top `limit`
        # raw matches would silently get fewer than `limit` results even
        # when enough visible ones exist. Proven directly against the SQL
        # call rather than by engineering BM25 ranking in a fixture: the
        # candidate fetch must ask for more than the requested limit.
        with mock.patch("brain.search._run_match", wraps=search._run_match) as spy:
            search.search(self._as("principal-marcin"), "widget", limit=5)
        fetch_limits = [call.args[2] for call in spy.call_args_list]
        self.assertTrue(fetch_limits)
        self.assertTrue(all(fl > 5 for fl in fetch_limits))


if __name__ == "__main__":
    unittest.main()
