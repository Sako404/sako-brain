"""Regression suite for the write -> immediate read/search consistency fix
(2026-10, SAKO Brain multi-user Stage 0A).

Before this fix, every write primitive left the SQLite FTS5 index
untouched — a note existed on disk the instant it was written, but
`brain search`/`brain get`/MCP `search_memory`/`read_memory` would not see
it until a later, separate `brain index` ran. Found live during a
staleness-test acceptance pass (a client read a record moments after
writing it and got "not found").

The fix has two parts, both exercised here:
  1. Every write primitive (`capture.capture`, `update.update_memory`,
     `projectops.create_project/set_project_status/close_project/
     update_section`, `decision.create_decision` (including the
     superseded-record side-write), `timeline.create_event`,
     `memoryops.create_memory`, `handoff.write`) now calls
     `indexer.index_note()` on whatever it just wrote, incrementally —
     never a full `rebuild()` per write.
  2. `brain get` (`cli.cmd_get`) no longer depends on the index at all —
     it resolves by walking Markdown directly
     (`update_mod.find_note_path`), the same index-independent path
     `mcp_server.py`'s `read_memory` tool already used. A write's direct
     readability was never supposed to depend on indexing timing; now it
     structurally can't.

This file deliberately never calls `indexer.rebuild()` — if a test here
needs a rebuild to pass, the fix has regressed.
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from brain import capture, cli, decision, handoff, memoryops, memoryqueue, projectops, search, timeline
from brain import update as update_mod
from tests.helpers import TempVault


def _search_ids(config, query: str) -> set[str]:
    return {r.id for r in search.search(config, query, limit=50)}


class WriteIndexConsistencyTestCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()


class TestCaptureIsImmediatelySearchable(WriteIndexConsistencyTestCase):
    def test_remember_is_findable_without_a_separate_index_run(self):
        capture.capture(self.config, type_="fact", title="Zbltmorph widget threshold",
                         text="A deliberately unique token: zbltmorph.", source="test")
        self.assertIn("fact-zbltmorph-widget-threshold", _search_ids(self.config, "zbltmorph"))

    def test_memory_accept_is_immediately_searchable(self):
        entry = memoryqueue.add(self.config, candidate_fact="Quetzalorn calibration value")
        memoryqueue.accept(self.config, entry.id, title="Quetzalorn calibration value")
        results = search.search(self.config, "quetzalorn")
        self.assertTrue(any("quetzalorn" in r.title.lower() for r in results), results)


class TestUpdateIsImmediatelyVisible(WriteIndexConsistencyTestCase):
    def test_appended_text_is_immediately_searchable(self):
        path = capture.capture(self.config, type_="fact", title="Base fact", text="original body")
        import brain.frontmatter as fm
        note_id = fm.parse_file(path).id
        update_mod.update_memory(self.config, note_id, append_text="Xyrqual append marker.")
        self.assertIn(note_id, _search_ids(self.config, "xyrqual"))


class TestDecisionIsImmediatelyVisible(WriteIndexConsistencyTestCase):
    def test_new_decision_is_immediately_searchable(self):
        path = decision.create_decision(self.config, title="Frambosel architecture choice",
                                         decision="Use frambosel.", date="2026-10-01")
        self.assertIn("decision-2026-10-01-frambosel-architecture-choice",
                      _search_ids(self.config, "frambosel"))

    def test_superseded_old_decision_status_is_immediately_updated_in_index(self):
        old = decision.create_decision(self.config, title="Old vorlex call", decision="Use vorlex v1.",
                                        status="decided", date="2026-09-01")
        import brain.frontmatter as fm
        old_id = fm.parse_file(old).id
        decision.create_decision(self.config, title="New vorlex call", decision="Use vorlex v2.",
                                  supersedes=old_id, date="2026-10-01")
        row = search.get_note_row(self.config, old_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "superseded")


class TestTimelineIsImmediatelyVisible(WriteIndexConsistencyTestCase):
    def test_new_event_is_immediately_searchable(self):
        timeline.create_event(self.config, title="Snorqath event occurred", valid_from="2026-10-01",
                               what_happened="The snorqath happened.")
        self.assertIn("event-2026-10-01-snorqath-event-occurred", _search_ids(self.config, "snorqath"))


class TestNoteCreateIsImmediatelyVisible(WriteIndexConsistencyTestCase):
    def test_new_document_note_is_immediately_searchable(self):
        memoryops.create_memory(self.config, type_="document", title="Plexivane reference doc",
                                 text="plexivane details", doc_path="/tmp/plexivane.pdf")
        self.assertIn("document-plexivane-reference-doc", _search_ids(self.config, "plexivane"))


class TestProjectWritesAreImmediatelyVisible(WriteIndexConsistencyTestCase):
    def test_created_project_is_immediately_searchable(self):
        projectops.create_project(self.config, id="project-torvennel", name="Torvennel",
                                   path="/tmp/torvennel", status="active", created="2026-10-01")
        self.assertIn("project-torvennel", _search_ids(self.config, "torvennel"))

    def test_status_change_is_immediately_reflected_in_index(self):
        projectops.create_project(self.config, id="project-quindex", name="Quindex",
                                   path="/tmp/quindex", status="active", created="2026-10-01")
        projectops.set_project_status(self.config, "project-quindex", "archived")
        row = search.get_note_row(self.config, "project-quindex")
        self.assertEqual(row["status"], "archived")

    def test_close_project_is_immediately_reflected_in_index(self):
        projectops.create_project(self.config, id="project-harmolex", name="Harmolex",
                                   path="/tmp/harmolex", status="active", created="2026-10-01")
        projectops.close_project(self.config, "project-harmolex", summary="Wrapped up.")
        row = search.get_note_row(self.config, "project-harmolex")
        self.assertEqual(row["status"], "archived")

    def test_section_update_is_immediately_searchable(self):
        projectops.create_project(self.config, id="project-dalvorix", name="Dalvorix",
                                   path="/tmp/dalvorix", status="active", created="2026-10-01")
        projectops.update_section(self.config, "project-dalvorix", "Next actions", "append",
                                   "Investigate the fjolmerith integration.")
        self.assertIn("project-dalvorix", _search_ids(self.config, "fjolmerith"))


class TestHandoffIsImmediatelyVisible(WriteIndexConsistencyTestCase):
    def setUp(self):
        super().setUp()
        self.config.registry_path.write_text(
            "projects:\n  - id: project-example\n    name: Example\n    path: /tmp/x\n    status: active\n"
        )

    def test_handoff_write_is_immediately_searchable(self):
        sections = handoff.HandoffSections(attempted="Grimwattle integration attempt")
        handoff.write(self.config, "project-example", sections, session_date="2026-10-01")
        self.assertIn("handoff-project-example", _search_ids(self.config, "grimwattle"))


class TestGetNoLongerDependsOnTheIndex(WriteIndexConsistencyTestCase):
    """The specific bug report: a write followed immediately by `brain get`
    (never `brain search`) used to 404 until a separate `brain index` ran,
    because cmd_get resolved ids via the SQLite index rather than reading
    Markdown directly. Proven here with NO call to indexer.rebuild() or
    indexer.index_note() at all — if cmd_get regresses to depending on the
    index, this fails even though the write-path fix above still passes."""

    def test_get_succeeds_immediately_after_remember_with_zero_indexing_calls(self):
        dest = capture.capture(self.config, type_="fact", title="Uncontacted record",
                                text="never touched the indexer on purpose")
        import brain.frontmatter as fm
        note_id = fm.parse_file(dest).id

        # Bypass capture.capture's own index_note call to simulate the
        # worst case (an index that is arbitrarily stale/absent) and prove
        # cmd_get still works — it must never consult the index at all.
        self.config.db_path.unlink(missing_ok=True)

        args = SimpleNamespace(id=note_id)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_get(self.config, args)
        self.assertEqual(rc, 0)
        self.assertIn("never touched the indexer on purpose", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
