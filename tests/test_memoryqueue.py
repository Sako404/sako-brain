import unittest

from brain import memoryqueue
from tests.helpers import TempVault


class TestMemoryQueue(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_queue_starts_empty(self):
        self.assertEqual(memoryqueue.list_all(self.config), [])
        self.assertEqual(memoryqueue.list_pending(self.config), [])

    def test_add_creates_single_queue_file_not_many(self):
        memoryqueue.add(self.config, candidate_fact="Fact one")
        memoryqueue.add(self.config, candidate_fact="Fact two")
        memoryqueue.add(self.config, candidate_fact="Fact three")

        queue_dir = self.config.brain_root / "00_INBOX" / "memory"
        files = list(queue_dir.glob("*"))
        self.assertEqual(len(files), 1, "expected one growing queue file, not one per candidate")
        self.assertEqual(len(memoryqueue.list_all(self.config)), 3)

    def test_add_defaults_source_date_to_today(self):
        import datetime
        entry = memoryqueue.add(self.config, candidate_fact="No explicit date")
        self.assertEqual(entry.source_date, datetime.date.today().isoformat())

    def test_add_rejects_invalid_sensitivity(self):
        with self.assertRaises(memoryqueue.MemoryQueueError):
            memoryqueue.add(self.config, candidate_fact="x", sensitivity="top-secret")

    def test_preserves_provenance_fields(self):
        entry = memoryqueue.add(
            self.config, candidate_fact="Alex bought a used estate car",
            entities=["person-alex-example"], source="user, 2026-07-27 conversation",
            source_date="2026-07-22", proposed_destination="areas/Vehicles",
            proposed_type="fact", sensitivity="normal", confidence="fact",
            reason="vehicle purchase is a durable fact",
        )
        reloaded = memoryqueue.get(self.config, entry.id)
        self.assertEqual(reloaded.entities, ["person-alex-example"])
        self.assertEqual(reloaded.source, "user, 2026-07-27 conversation")
        self.assertEqual(reloaded.source_date, "2026-07-22")
        self.assertEqual(reloaded.proposed_destination, "areas/Vehicles")
        self.assertEqual(reloaded.reason, "vehicle purchase is a durable fact")
        self.assertEqual(reloaded.status, "pending")

    def test_reject_marks_status_without_writing_a_note(self):
        entry = memoryqueue.add(self.config, candidate_fact="Speculative, not confirmed")
        memoryqueue.reject(self.config, entry.id, note="turned out to be wrong")

        reloaded = memoryqueue.get(self.config, entry.id)
        self.assertEqual(reloaded.status, "rejected")
        self.assertEqual(reloaded.resolved_note, "turned out to be wrong")
        self.assertEqual(memoryqueue.list_pending(self.config), [])
        # Rejected entries are kept for audit history, not deleted.
        self.assertEqual(len(memoryqueue.list_all(self.config)), 1)

    def test_accept_writes_note_via_capture_and_marks_accepted(self):
        entry = memoryqueue.add(
            self.config, candidate_fact="Alex bought a used estate car",
            entities=["person-alex-example"], proposed_type="fact",
            sensitivity="normal", confidence="fact", source="user statement",
        )
        accepted = memoryqueue.accept(self.config, entry.id)

        self.assertEqual(accepted.status, "accepted")
        self.assertTrue(accepted.written_to)
        written_path = self.config.brain_root / accepted.written_to
        self.assertTrue(written_path.exists())
        content = written_path.read_text()
        self.assertIn("Alex bought a used estate car", content)
        self.assertIn("person-alex-example", content)

    def test_cannot_accept_twice(self):
        entry = memoryqueue.add(self.config, candidate_fact="One-time fact")
        memoryqueue.accept(self.config, entry.id)
        with self.assertRaises(memoryqueue.MemoryQueueError):
            memoryqueue.accept(self.config, entry.id)

    def test_cannot_reject_already_accepted(self):
        entry = memoryqueue.add(self.config, candidate_fact="One-time fact")
        memoryqueue.accept(self.config, entry.id)
        with self.assertRaises(memoryqueue.MemoryQueueError):
            memoryqueue.reject(self.config, entry.id)

    def test_accept_unknown_id_raises(self):
        with self.assertRaises(memoryqueue.MemoryQueueError):
            memoryqueue.accept(self.config, "mem-doesnotexist")

    def test_list_pending_excludes_resolved(self):
        e1 = memoryqueue.add(self.config, candidate_fact="Fact A")
        e2 = memoryqueue.add(self.config, candidate_fact="Fact B")
        memoryqueue.accept(self.config, e1.id)

        pending = memoryqueue.list_pending(self.config)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].id, e2.id)


if __name__ == "__main__":
    unittest.main()
