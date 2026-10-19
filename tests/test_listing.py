"""Metadata-listing acceptance tests on fictional records.

Synthetic fixtures distinguish ranked text matches from metadata filters,
then verify completeness, deterministic ordering and principal-scoped counts.
"""
import unittest

from brain import indexer, listing, search
from tests.helpers import TempVault


class TestMetadataFilteringIsNotTextSearch(unittest.TestCase):
    """The two halves of the original failure, on one fixture."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

        # A genuinely open decision whose body never contains the word
        # "proposed" — invisible to the text search that was used.
        self.vault.write_note(
            "40_DECISIONS", "decision-silent-open.md",
            id="decision-silent-open", type="decision", status="proposed",
            updated="2026-10-02", title="Tier2 enforcement gate",
            body="## Context\n\nStill being designed.\n",
        )
        # A closed decision whose body quotes the word "proposed" in its own
        # history — a top text-search hit, and the wrong answer.
        self.vault.write_note(
            "40_DECISIONS", "decision-noisy-closed.md",
            id="decision-noisy-closed", type="decision", status="decided",
            updated="2026-10-03", title="Approval protocol",
            body="## Context\n\nThis supersedes the status: proposed draft "
                 "that was proposed earlier and is no longer proposed.\n",
        )
        self.vault.write_note(
            "40_DECISIONS", "decision-second-open.md",
            id="decision-second-open", type="decision", status="proposed",
            updated="2026-10-01", title="Synthetic event marking",
            body="## Context\n\nAlso still open.\n",
        )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_full_proposed_listing_includes_records_that_never_say_proposed(self):
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual(
            sorted(e.id for e in result.entries),
            ["decision-second-open", "decision-silent-open"],
        )
        self.assertEqual(result.total_visible, 2)
        self.assertTrue(result.complete)

    def test_decided_record_quoting_proposed_is_excluded(self):
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertNotIn("decision-noisy-closed", [e.id for e in result.entries])

    def test_text_search_really_does_return_the_wrong_answer(self):
        """Pins the premise, so this suite documents a real difference rather
        than asserting against an imagined one. If `search` ever became a
        metadata filter, this test should fail and be reconsidered."""
        hits = [r.id for r in search.search(self.config, "status: proposed")]
        self.assertIn("decision-noisy-closed", hits)
        statuses = {r.id: r.status for r in search.search(self.config, "status: proposed")}
        self.assertTrue(
            any(s == "decided" for s in statuses.values()),
            "text search was expected to return at least one non-proposed record",
        )

    def test_deterministic_order_newest_updated_first_then_id(self):
        self.vault.write_note(
            "40_DECISIONS", "decision-aaa-tie.md",
            id="decision-aaa-tie", type="decision", status="proposed",
            updated="2026-10-02", title="Tie on the same updated date",
        )
        indexer.rebuild(self.config)
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual(
            [e.id for e in result.entries],
            # 2026-10-02 ties are broken by id ascending, then 2026-10-01.
            ["decision-aaa-tie", "decision-silent-open", "decision-second-open"],
        )

    def test_unknown_status_is_refused_not_answered_with_an_empty_list(self):
        with self.assertRaises(listing.ListingError):
            listing.list_notes(self.config, type="decision", status="propsed")

    def test_unknown_type_is_refused(self):
        with self.assertRaises(listing.ListingError):
            listing.list_notes(self.config, type="dcision")


class TestCompletenessIsNeverImplied(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        for i in range(5):
            self.vault.write_note(
                "40_DECISIONS", f"decision-open-{i}.md",
                id=f"decision-open-{i}", type="decision", status="proposed",
                updated=f"2026-10-0{i + 1}", title=f"Open {i}",
            )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_a_limited_page_reports_itself_incomplete_with_the_real_total(self):
        result = listing.list_notes(self.config, type="decision", status="proposed", limit=2)
        self.assertEqual(result.returned, 2)
        self.assertEqual(result.total_visible, 5)
        self.assertFalse(result.complete)
        self.assertFalse(result.page_complete)
        self.assertTrue(result.source_complete)
        self.assertEqual(result.next_offset, 2)

    def test_paging_through_sees_every_record_exactly_once(self):
        """`next_offset is None` is the terminator, not `complete` — a final
        page is still a page, and says so."""
        seen = []
        offset, guard = 0, 0
        while True:
            guard += 1
            self.assertLess(guard, 10, "pagination did not terminate")
            page = listing.list_notes(self.config, type="decision",
                                       status="proposed", limit=2, offset=offset)
            seen += [e.id for e in page.entries]
            if page.next_offset is None:
                self.assertFalse(page.complete, "the last page is still only a page")
                break
            offset = page.next_offset
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_a_single_unlimited_call_is_the_only_complete_one(self):
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertTrue(result.complete)
        self.assertTrue(result.page_complete)
        self.assertEqual(result.returned, result.total_visible)
        self.assertIsNone(result.next_offset)

    def test_cli_text_output_states_incompleteness_explicitly(self):
        from brain import cli

        class Args:
            type, status, project, tag = "decision", "proposed", None, None
            limit, offset, json = 2, 0, False

        import io
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_list(self.config, Args())
        self.assertEqual(rc, 0)
        self.assertIn("NOT THE FULL LIST", out.getvalue())
        self.assertIn("2 of 5 visible", out.getvalue())
        self.assertIn("--offset 2", err.getvalue())

    def test_a_count_only_call_offers_no_next_offset(self):
        """limit=0 returns no entries, so following next_offset could never
        make progress — a client that pages by it would loop forever."""
        result = listing.list_notes(self.config, type="decision", status="proposed", limit=0)
        self.assertEqual(result.returned, 0)
        self.assertEqual(result.total_visible, 5)
        self.assertIsNone(result.next_offset)

    def test_cli_never_suggests_a_limit_above_the_maximum(self):
        from unittest.mock import patch
        from brain import cli

        class Args:
            type, status, project, tag = "decision", "proposed", None, None
            limit, offset, json = 2, 0, False

        import io
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with patch.object(listing, "MAX_LIMIT", 3), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_list(self.config, Args())
        self.assertEqual(rc, 0)
        self.assertNotIn("--limit 5", err.getvalue())
        self.assertIn("--limit 3", err.getvalue())

    def test_no_index_at_all_still_lists_everything(self):
        """Discovery is a Markdown scan, so the index's state is irrelevant —
        this is the property the first version of the module did not have."""
        vault = TempVault()
        config = vault.config()
        try:
            vault.write_note("40_DECISIONS", "never-indexed.md", id="decision-never-indexed",
                              type="decision", status="proposed", title="Never indexed")
            self.assertFalse(config.db_path.exists())
            result = listing.list_notes(config, type="decision", status="proposed")
            self.assertEqual([e.id for e in result.entries], ["decision-never-indexed"])
            self.assertTrue(result.complete)
        finally:
            vault.cleanup()


class TestVisibilityScope(unittest.TestCase):
    """A listing must never reveal another principal's records — nor how
    many of them there are. `total_visible` is a count of what this caller
    may see, for exactly that reason."""

    def setUp(self):
        self.vault = TempVault()
        self.base = self.vault.config()
        self.vault.write_note(
            "40_DECISIONS", "decision-mine.md",
            id="decision-mine", type="decision", status="proposed",
            updated="2026-10-01", title="Mine",
            owner_principal="principal:marcin", audience=[],
        )
        for i in range(3):
            self.vault.write_note(
                "40_DECISIONS", f"decision-theirs-{i}.md",
                id=f"decision-theirs-{i}", type="decision", status="proposed",
                updated="2026-10-01", title=f"Theirs {i}",
                owner_principal="principal:ania", audience=[],
            )
        indexer.rebuild(self.base)

    def tearDown(self):
        self.vault.cleanup()

    def test_other_principals_records_are_not_listed(self):
        result = listing.list_notes(self.base, type="decision", status="proposed",
                                     principal_id="principal-marcin")
        self.assertEqual([e.id for e in result.entries], ["decision-mine"])

    def test_total_visible_does_not_leak_the_count_of_hidden_records(self):
        result = listing.list_notes(self.base, type="decision", status="proposed",
                                     principal_id="principal-marcin")
        self.assertEqual(result.total_visible, 1)
        self.assertTrue(result.complete)

    def test_the_owner_sees_their_own(self):
        result = listing.list_notes(self.base, type="decision", status="proposed",
                                     principal_id="principal-ania")
        self.assertEqual(result.total_visible, 3)

    def test_scope_is_stated_in_the_result(self):
        result = listing.list_notes(self.base, principal_id="principal-marcin")
        self.assertIn("principal-marcin", result.scope)


class TestProjectAndTagFilters(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note(
            "40_DECISIONS", "decision-example-service-task.md",
            id="decision-example-service-task", type="decision", status="proposed",
            updated="2026-10-10", title="example-service task",
            projects=["project-example-service"], tags=["task"],
        )
        self.vault.write_note(
            "40_DECISIONS", "decision-example-service-real.md",
            id="decision-example-service-real", type="decision", status="proposed",
            updated="2026-10-09", title="example-service real decision",
            projects=["project-example-service"], tags=[],
        )
        self.vault.write_note(
            "40_DECISIONS", "decision-other.md",
            id="decision-other", type="decision", status="proposed",
            updated="2026-10-08", title="Unrelated",
            projects=["project-other"], tags=["task"],
        )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_project_filter_matches_the_projects_list(self):
        result = listing.list_notes(self.config, project="project-example-service")
        self.assertEqual(sorted(e.id for e in result.entries),
                          ["decision-example-service-real", "decision-example-service-task"])

    def test_tag_filter_separates_tasks_from_real_decisions(self):
        result = listing.list_notes(self.config, type="decision", status="proposed", tag="task")
        self.assertEqual(sorted(e.id for e in result.entries),
                          ["decision-example-service-task", "decision-other"])

    def test_filters_combine(self):
        result = listing.list_notes(self.config, type="decision", status="proposed",
                                     project="project-example-service", tag="task")
        self.assertEqual([e.id for e in result.entries], ["decision-example-service-task"])

    def test_filters_are_echoed_back_so_a_report_can_cite_them(self):
        result = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual(result.filters["type"], "decision")
        self.assertEqual(result.filters["status"], "proposed")
        self.assertIsNone(result.filters["project"])


class TestResultShapeIsStableAcrossTransports(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("40_DECISIONS", "decision-a.md", id="decision-a",
                               type="decision", status="proposed", title="A")
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_mcp_tool_returns_the_same_dict_the_cli_emits(self):
        from brain import mcp_server

        direct = listing.result_to_dict(
            listing.list_notes(self.config, type="decision", status="proposed"))
        via_tool = mcp_server.tool_list_notes(self.config, type="decision", status="proposed")
        self.assertEqual(direct, via_tool)

    def test_mcp_tool_surfaces_a_refusal_rather_than_an_empty_listing(self):
        from brain import mcp_server

        out = mcp_server.tool_list_notes(self.config, type="decision", status="nonsense")
        self.assertIn("error", out)
        self.assertNotIn("entries", out)

    def test_every_documented_completeness_field_is_present(self):
        payload = listing.result_to_dict(listing.list_notes(self.config))
        for key in ("entries", "total_visible", "returned", "offset", "limit",
                    "complete", "page_complete", "source_complete", "unreadable",
                    "unstructured_skipped", "next_offset", "filters", "sort", "scope"):
            self.assertIn(key, payload)

    def test_the_index_is_never_consulted(self):
        """Guards the fix structurally rather than by behaviour alone: if this
        module ever imports the index layer again, the completeness promise
        goes back to depending on the last `brain index`. Checked on the
        parsed imports, not the text, so prose explaining the decision does
        not trip it."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(listing))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
                imported.update(a.name for a in node.names)
        self.assertNotIn("indexer", imported)
        self.assertNotIn("search", imported)


if __name__ == "__main__":
    unittest.main()
