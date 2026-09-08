import unittest

from brain import context, indexer
from tests.helpers import TempVault


class TestGetContext(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-example\n"
            "    name: Example Widget Project\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )
        self.vault.write_note(
            "30_PROJECTS/ACTIVE", "project-example.md", id="project-example", type="project",
            title="Example Widget Project", body="The widget project is complete and stable.",
        )
        self.vault.write_note(
            "50_TIMELINE", "event-widget-shipped.md", id="event-widget-shipped", type="event",
            title="Widget project shipped", body="The widget project shipped today.",
        )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_returns_concise_structured_notes_not_full_bodies(self):
        result = context.get_context(self.config, "widget", limit=5)
        self.assertGreater(len(result.notes), 0)
        for item in result.notes:
            # A snippet, not the full body — this is the whole point of
            # get_context vs read_memory.
            self.assertLess(len(item.snippet), 500)

    def test_includes_provenance_fields(self):
        result = context.get_context(self.config, "widget", limit=5)
        item = next(n for n in result.notes if n.id == "project-example")
        self.assertEqual(item.type, "project")
        self.assertIsInstance(item.source, str)
        self.assertIsInstance(item.confidence, str)

    def test_matches_related_registered_project(self):
        result = context.get_context(self.config, "widget", limit=5, include_projects=True)
        ids = [p["id"] for p in result.projects]
        self.assertIn("project-example", ids)

    def test_matches_related_timeline_entry(self):
        result = context.get_context(self.config, "widget", limit=5, include_timeline=True)
        ids = [t["id"] for t in result.timeline]
        self.assertIn("event-widget-shipped", ids)

    def test_can_disable_projects_and_timeline(self):
        result = context.get_context(self.config, "widget", limit=5,
                                      include_projects=False, include_timeline=False)
        self.assertEqual(result.projects, [])
        self.assertEqual(result.timeline, [])

    def test_to_dict_is_json_serializable(self):
        import json
        result = context.get_context(self.config, "widget", limit=5)
        json.dumps(result.to_dict())  # must not raise


class TestGetContextRestrictedFiltering(unittest.TestCase):
    """Preserve Phase 5A sensitivity policy: restricted notes must not
    leak into a context response by default."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        path = self.vault.root / "10_PEOPLE" / "restricted-widget-note.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "id: fact-restricted-widget\n"
            "type: fact\n"
            "sensitivity: restricted\n"
            "---\n\n"
            "# Restricted widget detail\n\n"
            "Sensitive widget information that should not leak by default.\n"
        )
        self.vault.write_note(
            "60_KNOWLEDGE", "normal-widget.md", id="knowledge-normal-widget", type="knowledge",
            title="Normal widget info", body="Ordinary, non-sensitive widget information.",
        )
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_restricted_note_excluded_by_default(self):
        result = context.get_context(self.config, "widget", limit=10)
        ids = [n.id for n in result.notes]
        self.assertNotIn("fact-restricted-widget", ids)
        self.assertIn("knowledge-normal-widget", ids)

    def test_restricted_omitted_count_reported(self):
        result = context.get_context(self.config, "widget", limit=10)
        self.assertGreaterEqual(result.restricted_omitted, 1)

    def test_restricted_note_included_when_explicitly_requested(self):
        result = context.get_context(self.config, "widget", limit=10, include_restricted=True)
        ids = [n.id for n in result.notes]
        self.assertIn("fact-restricted-widget", ids)


class TestGetContextCurrentVsHistorical(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_superseded_fact_marked_not_current(self):
        path = self.vault.root / "60_KNOWLEDGE" / "old-fact.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: fact-widget-old\ntype: fact\nstatus: superseded\n---\n\n"
            "# Old widget fact\n\nThe widget cost X.\n"
        )
        indexer.rebuild(self.config)
        result = context.get_context(self.config, "widget", limit=5)
        item = next(n for n in result.notes if n.id == "fact-widget-old")
        self.assertFalse(item.is_current)

    def test_current_fact_marked_current(self):
        path = self.vault.root / "60_KNOWLEDGE" / "new-fact.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: fact-widget-new\ntype: fact\nstatus: current\n---\n\n"
            "# New widget fact\n\nThe widget costs Y now.\n"
        )
        indexer.rebuild(self.config)
        result = context.get_context(self.config, "widget", limit=5)
        item = next(n for n in result.notes if n.id == "fact-widget-new")
        self.assertTrue(item.is_current)

    def test_past_valid_to_marked_not_current(self):
        path = self.vault.root / "60_KNOWLEDGE" / "expired-fact.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: fact-widget-expired\ntype: fact\nstatus: current\nvalid_to: 2020-01-01\n---\n\n"
            "# Expired widget fact\n\nWas true once.\n"
        )
        indexer.rebuild(self.config)
        result = context.get_context(self.config, "widget", limit=5)
        item = next(n for n in result.notes if n.id == "fact-widget-expired")
        self.assertFalse(item.is_current)

    def test_blank_valid_to_does_not_crash_and_is_current(self):
        # Regression: a blank "valid_to:" field used to be stored as the
        # literal string "None" — must not be treated as a past date.
        path = self.vault.root / "60_KNOWLEDGE" / "blank-fact.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: fact-widget-blank\ntype: fact\nstatus: current\nvalid_to:\n---\n\n"
            "# Blank valid_to widget fact\n\nStill true.\n"
        )
        indexer.rebuild(self.config)
        result = context.get_context(self.config, "widget", limit=5)
        item = next(n for n in result.notes if n.id == "fact-widget-blank")
        self.assertTrue(item.is_current)
        self.assertEqual(item.source, "")  # also blank, also should not be "None"


if __name__ == "__main__":
    unittest.main()
