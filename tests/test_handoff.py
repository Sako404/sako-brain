import unittest

from brain import handoff
from tests.helpers import TempVault


class TestHandoff(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-example\n"
            "    name: Example Project\n"
            "    path: /tmp/does-not-need-to-exist-for-this-test\n"
            "    status: active\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_unknown_project_raises(self):
        sections = handoff.HandoffSections(attempted="x")
        with self.assertRaises(handoff.HandoffError):
            handoff.write(self.config, "project-nonexistent", sections)

    def test_first_write_creates_file_with_frontmatter(self):
        sections = handoff.HandoffSections(
            attempted="Implement feature X", changed="Added the X module",
            working_state="Builds and passes tests", unresolved="None",
            next_action="Deploy", files_changed=["src/x.py"], decisions=["decision-example"],
        )
        path = handoff.write(self.config, "project-example", sections, session_date="2026-07-01")

        self.assertTrue(path.exists())
        text = path.read_text()
        self.assertIn("id: handoff-project-example", text)
        self.assertIn("projects: [project-example]", text)
        self.assertIn("## Session 2026-07-01", text)
        self.assertIn("Implement feature X", text)
        self.assertIn("src/x.py", text)
        self.assertIn("decision-example", text)

    def test_second_write_prepends_and_keeps_history(self):
        sections1 = handoff.HandoffSections(attempted="Session one work")
        handoff.write(self.config, "project-example", sections1, session_date="2026-07-01")

        sections2 = handoff.HandoffSections(attempted="Session two work")
        path = handoff.write(self.config, "project-example", sections2, session_date="2026-07-02")

        text = path.read_text()
        self.assertIn("Session one work", text)
        self.assertIn("Session two work", text)
        self.assertEqual(text.count("## Session"), 2)
        # Most recent session appears first.
        self.assertLess(text.index("## Session 2026-07-02"), text.index("## Session 2026-07-01"))

    def test_updated_field_refreshed_created_field_preserved(self):
        handoff.write(self.config, "project-example", handoff.HandoffSections(attempted="s1"), session_date="2026-07-01")
        path = handoff.write(self.config, "project-example", handoff.HandoffSections(attempted="s2"), session_date="2026-07-02")

        text = path.read_text()
        self.assertIn("created: 2026-07-01", text)
        self.assertIn("updated: 2026-07-02", text)

    def test_read_latest_returns_only_most_recent_session(self):
        handoff.write(self.config, "project-example", handoff.HandoffSections(attempted="old session"), session_date="2026-07-01")
        handoff.write(self.config, "project-example", handoff.HandoffSections(attempted="new session"), session_date="2026-07-02")

        latest = handoff.read_latest(self.config, "project-example")
        self.assertIn("new session", latest)
        self.assertNotIn("old session", latest)

    def test_read_latest_none_when_no_handoff_exists(self):
        self.assertIsNone(handoff.read_latest(self.config, "project-example"))

    def test_has_handoff_and_list(self):
        self.assertFalse(handoff.has_handoff(self.config, "project-example"))
        self.assertEqual(handoff.list_projects_with_handoffs(self.config), [])

        handoff.write(self.config, "project-example", handoff.HandoffSections(attempted="x"))

        self.assertTrue(handoff.has_handoff(self.config, "project-example"))
        self.assertEqual(handoff.list_projects_with_handoffs(self.config), ["project-example"])

    def test_handoff_lands_in_correct_status_folder(self):
        path = handoff.write(self.config, "project-example", handoff.HandoffSections(attempted="x"))
        self.assertIn("30_PROJECTS/ACTIVE", str(path))


class TestEmptyHandoffIsRefused(unittest.TestCase):
    """An OSS-1 handoff was written with only `decisions` populated, so every
    prose section rendered "(not noted)" — a document that looks written and
    carries nothing. The next session has to reconstruct from git instead, and
    worse, may trust it."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        registry = self.vault.root / "30_PROJECTS" / "_registry.yaml"
        registry.write_text(
            "projects:\n  - id: project-x\n    name: X\n    path: /tmp/x\n    status: active\n")

    def tearDown(self):
        self.vault.cleanup()

    def test_all_prose_blank_is_refused(self):
        sections = handoff.HandoffSections(decisions=["a decision"])
        with self.assertRaises(handoff.HandoffError) as ctx:
            handoff.write(self.config, "project-x", sections)
        self.assertIn("empty handoff", str(ctx.exception))
        self.assertFalse(handoff.handoff_path(self.config, "project-x").exists())

    def test_whitespace_only_prose_is_still_empty(self):
        sections = handoff.HandoffSections(attempted="   \n  ")
        with self.assertRaises(handoff.HandoffError):
            handoff.write(self.config, "project-x", sections)

    def test_one_populated_prose_field_is_enough(self):
        sections = handoff.HandoffSections(next_action="Start OSS-3")
        path = handoff.write(self.config, "project-x", sections)
        self.assertIn("Start OSS-3", path.read_text())


if __name__ == "__main__":
    unittest.main()
