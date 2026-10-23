import dataclasses
import unittest

import yaml

from brain import memoryqueue, writepolicy
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



class TestMemoryQueueOwnership(unittest.TestCase):
    """v0.22.0: entries belong to whoever queued them and are read through
    visibility.py, so the queue is safe to reach through the dispatcher."""

    def setUp(self):
        self.vault = TempVault()
        self.marcin = self.vault.config()
        self.sam = dataclasses.replace(self.marcin, acting_principal="principal-sam")

    def tearDown(self):
        self.vault.cleanup()

    def test_entry_belongs_to_the_acting_principal(self):
        entry = memoryqueue.add(self.sam, candidate_fact="Sam's fact")
        self.assertEqual(entry.owner_principal, "principal:sam")
        self.assertEqual(entry.audience, [])

    def test_private_entry_is_invisible_to_anyone_else(self):
        entry = memoryqueue.add(self.sam, candidate_fact="Sam's private fact")
        self.assertEqual(memoryqueue.list_all(self.marcin), [])
        self.assertIsNone(memoryqueue.get(self.marcin, entry.id))
        for resolve in (lambda: memoryqueue.accept(self.marcin, entry.id),
                        lambda: memoryqueue.reject(self.marcin, entry.id)):
            with self.assertRaises(memoryqueue.MemoryQueueError) as ctx:
                resolve()
            # Same message as for an id that does not exist at all.
            self.assertIn("no pending memory entry", str(ctx.exception))

    def test_audience_grants_reading_but_not_resolving(self):
        entry = memoryqueue.add(self.sam, candidate_fact="Shared fact", audience=["principal:marcin"])
        self.assertEqual([e.id for e in memoryqueue.list_pending(self.marcin)], [entry.id])
        with self.assertRaises(memoryqueue.MemoryQueueError) as ctx:
            memoryqueue.accept(self.marcin, entry.id)
        self.assertIn("only the owner", str(ctx.exception))
        self.assertEqual(memoryqueue.get(self.sam, entry.id).status, "pending")

    def test_accepted_note_carries_owner_and_audience(self):
        entry = memoryqueue.add(self.sam, candidate_fact="Shared fact", audience=["principal:marcin"])
        accepted = memoryqueue.accept(self.sam, entry.id)
        text = (self.sam.brain_root / accepted.written_to).read_text()
        self.assertIn("owner_principal: principal:sam", text)
        self.assertIn("principal:marcin", text)

    def test_malformed_audience_is_refused(self):
        with self.assertRaises(memoryqueue.MemoryQueueError):
            memoryqueue.add(self.sam, candidate_fact="x", audience=["sam"])

    def test_entry_without_owner_belongs_to_the_default_owner(self):
        path = self.marcin.inbox_dir / memoryqueue.QUEUE_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump([{
            "id": "mem-legacy1", "created": "2026-09-01", "candidate_fact": "Old fact",
            "status": "pending",
        }]))
        self.assertEqual([e.id for e in memoryqueue.list_pending(self.marcin)], ["mem-legacy1"])
        self.assertEqual(memoryqueue.list_pending(self.sam), [])
        self.assertEqual(memoryqueue.get(self.marcin, "mem-legacy1").owner_principal, "principal:marcin")

    def test_restricted_entry_needs_confirmation_to_accept(self):
        entry = memoryqueue.add(self.marcin, candidate_fact="x", sensitivity="restricted")
        with self.assertRaises(writepolicy.WritePolicyError):
            memoryqueue.accept(self.marcin, entry.id)
        self.assertEqual(memoryqueue.get(self.marcin, entry.id).status, "pending")
        self.assertEqual(memoryqueue.accept(self.marcin, entry.id, confirm_restricted=True).status,
                         "accepted")

    def test_concurrent_adds_lose_nothing(self):
        import threading
        threads = [threading.Thread(target=memoryqueue.add, args=(self.marcin,),
                                    kwargs={"candidate_fact": f"fact {i}"}) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(memoryqueue.list_all(self.marcin)), 20)


if __name__ == "__main__":
    unittest.main()
