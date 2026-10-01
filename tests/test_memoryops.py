"""brain.memoryops — create-at-canonical-destination for person/knowledge/
document/fact, the half of /remember and /import that isn't raw inbox
capture. Never accepts a client-supplied path."""
from __future__ import annotations

import dataclasses
import unittest

from brain import frontmatter, memoryops
from tests.helpers import TempVault


class TestCreateMemoryFixedDestinations(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_person_goes_to_people_dir(self):
        path = memoryops.create_memory(self.config, "person", "Jane Doe", text="A colleague.")
        self.assertIn("10_PEOPLE", str(path))
        note = frontmatter.parse_file(path)
        self.assertEqual(note.id, "person-jane-doe")
        self.assertEqual(note.type, "person")
        self.assertIn("## Summary", note.body)
        self.assertIn("A colleague.", note.body)

    def test_knowledge_goes_to_knowledge_dir(self):
        path = memoryops.create_memory(self.config, "knowledge", "Widget internals")
        self.assertIn("60_KNOWLEDGE", str(path))

    def test_document_goes_to_documents_dir_with_path_field(self):
        path = memoryops.create_memory(
            self.config, "document", "Contract PDF", doc_path="/tmp/example-contract.pdf",
        )
        self.assertIn("70_DOCUMENTS", str(path))
        note = frontmatter.parse_file(path)
        self.assertEqual(note.meta["path"], "/tmp/example-contract.pdf")

    def test_fixed_destination_type_rejects_area(self):
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "person", "Jane Doe", area="Finance")

    def test_duplicate_id_rejected(self):
        memoryops.create_memory(self.config, "person", "Jane Doe")
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "person", "Jane Doe")

    def test_unknown_type_rejected(self):
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "bogus", "X")


class TestCreateMemoryFact(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        (self.config.brain_root / "20_AREAS" / "Finance").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.vault.cleanup()

    def test_fact_with_valid_area_succeeds(self):
        path = memoryops.create_memory(
            self.config, "fact", "Mortgage rate fixed until 2028", area="Finance",
        )
        self.assertIn("20_AREAS/Finance", str(path))
        note = frontmatter.parse_file(path)
        self.assertEqual(note.status, "current")
        self.assertIn("valid_from", note.meta)

    def test_fact_without_area_refused(self):
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "fact", "Some fact")

    def test_fact_with_unknown_area_refused(self):
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "fact", "Some fact", area="NotARealArea")

    def test_existing_areas_lists_real_subdirectories_only(self):
        areas = memoryops.existing_areas(self.config)
        self.assertEqual(areas, ["Finance"])


class TestCreateMemoryUnsupportedTypes(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_area_type_refused_with_clear_message(self):
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "area", "New area")

    def test_event_type_refused_with_clear_message(self):
        # event has its own dedicated primitive: timeline.create_event()
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "event", "Something happened")

    def test_project_and_decision_refused_with_pointer_to_dedicated_commands(self):
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "project", "X")
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "decision", "X")


class TestCreateMemorySetsOwnership(unittest.TestCase):
    """Stage 2 (multi-user visibility): same default-ownership-on-write
    rule as capture.capture — see tests/test_capture.py."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_owner_principal_is_the_acting_principal(self):
        config = dataclasses.replace(self.config, acting_principal="principal-ania")
        path = memoryops.create_memory(config, "person", "Jane Doe", text="A colleague.")
        note = frontmatter.parse_file(path)
        self.assertEqual(note.meta["owner_principal"], "principal:ania")

    def test_audience_defaults_to_private(self):
        path = memoryops.create_memory(self.config, "person", "Jane Doe", text="A colleague.")
        note = frontmatter.parse_file(path)
        self.assertEqual(note.meta["audience"], [])

    def test_explicit_audience_is_honored(self):
        path = memoryops.create_memory(self.config, "person", "Jane Doe", text="A colleague.",
                                        audience=["group:household"])
        note = frontmatter.parse_file(path)
        self.assertEqual(note.meta["audience"], ["group:household"])


if __name__ == "__main__":
    unittest.main()
