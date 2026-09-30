import dataclasses
import json
import unittest

from brain.registry import ProjectEntry, find_duplicates, find_project, load_registry
from tests.helpers import TempVault


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_empty_registry_loads_as_empty_list(self):
        self.assertEqual(load_registry(self.config), [])

    def test_load_registry_parses_entries(self):
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-foo\n"
            "    name: Foo\n"
            "    path: /tmp/foo\n"
            "    status: active\n"
        )
        entries = load_registry(self.config)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "project-foo")
        self.assertEqual(entries[0].status, "active")

    def test_unquoted_yaml_dates_are_coerced_to_str(self):
        # YAML auto-types an unquoted date-like scalar as datetime.date, not
        # str, even though ProjectEntry.created/updated are documented str |
        # None — this broke JSON serialization (e.g. brain state) the first
        # time anything actually serialized a real registry to JSON.
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-foo\n"
            "    name: Foo\n"
            "    path: /tmp/foo\n"
            "    status: active\n"
            "    created: 2026-01-15\n"
            "    updated: 2026-02-01\n"
        )
        entry = load_registry(self.config)[0]
        self.assertIsInstance(entry.created, str)
        self.assertIsInstance(entry.updated, str)
        self.assertEqual(entry.created, "2026-01-15")
        self.assertEqual(entry.updated, "2026-02-01")
        json.dumps(dataclasses.asdict(entry))  # must not raise

    def test_find_duplicates_detects_duplicate_id(self):
        entries = [
            ProjectEntry(id="project-foo", name="Foo", path="/tmp/foo", status="active"),
            ProjectEntry(id="project-foo", name="Foo again", path="/tmp/foo2", status="active"),
        ]
        problems = find_duplicates(entries)
        self.assertTrue(any("duplicate registry id" in p for p in problems))

    def test_find_duplicates_detects_duplicate_path(self):
        entries = [
            ProjectEntry(id="project-a", name="A", path="/tmp/shared", status="active"),
            ProjectEntry(id="project-b", name="B", path="/tmp/shared", status="active"),
        ]
        problems = find_duplicates(entries)
        self.assertTrue(any("duplicate registry path" in p for p in problems))

    def test_find_duplicates_clean_registry_has_no_problems(self):
        entries = [
            ProjectEntry(id="project-a", name="A", path="/tmp/a", status="active"),
            ProjectEntry(id="project-b", name="B", path="/tmp/b", status="planned"),
        ]
        self.assertEqual(find_duplicates(entries), [])


class TestFindProject(unittest.TestCase):
    """The one canonical project-identifier resolver every project/handoff
    operation should use — a name that works in one command must work in
    every other one that takes a project identifier."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-sako-brain\n"
            "    name: Sako Brain\n"
            "    path: /tmp/sako-brain\n"
            "    status: active\n"
            "    aliases: [sako-brain, brain, personal-ai]\n"
            "  - id: project-other\n"
            "    name: Other\n"
            "    path: /tmp/other\n"
            "    status: active\n"
            "    aliases: [brain-other]\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_resolves_by_canonical_id(self):
        e = find_project(self.config, "project-sako-brain")
        self.assertIsNotNone(e)
        self.assertEqual(e.id, "project-sako-brain")

    def test_resolves_by_alias(self):
        e = find_project(self.config, "sako-brain")
        self.assertIsNotNone(e)
        self.assertEqual(e.id, "project-sako-brain")

    def test_resolves_by_second_alias(self):
        e = find_project(self.config, "personal-ai")
        self.assertEqual(e.id, "project-sako-brain")

    def test_unknown_identifier_returns_none(self):
        self.assertIsNone(find_project(self.config, "project-does-not-exist"))

    def test_id_match_wins_over_a_different_entrys_alias(self):
        # "project-other" is a real id; it must resolve to itself even
        # though nothing else aliases it — sanity check that id-priority
        # logic doesn't accidentally short-circuit on the wrong entry.
        e = find_project(self.config, "project-other")
        self.assertEqual(e.id, "project-other")

    def test_alias_match_is_case_sensitive(self):
        # Exact-match only, same discipline as id matching — a fuzzy or
        # case-insensitive match would make behavior depend on what else
        # is registered, not on what the caller actually typed.
        self.assertIsNone(find_project(self.config, "Sako-Brain"))


if __name__ == "__main__":
    unittest.main()
