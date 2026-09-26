"""brain.timeline — listing (pre-existing) and create_event() (new): the
fixed-schema `50_TIMELINE/event-<date>-<slug>.md` primitive extracted from
what `/timeline`'s "Adding an event" step already does via direct Write."""
from __future__ import annotations

import unittest

from brain import frontmatter, timeline
from tests.helpers import TempVault


class TestCreateEvent(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_creates_event_at_expected_path(self):
        path = timeline.create_event(
            self.config, title="Server migration shipped", valid_from="2026-09-26",
            what_happened="Cut over to the TrueNAS server.", projects=["project-sako-brain"],
        )
        self.assertEqual(path.name, "event-2026-09-26-server-migration-shipped.md")
        note = frontmatter.parse_file(path)
        self.assertEqual(note.id, "event-2026-09-26-server-migration-shipped")
        self.assertEqual(note.type, "event")
        self.assertEqual(note.meta["valid_from"], "2026-09-26")
        self.assertIn("Cut over to the TrueNAS server.", note.body)
        self.assertIn("project-sako-brain", note.body)

    def test_valid_from_can_differ_from_created(self):
        path = timeline.create_event(self.config, title="Past event", valid_from="2020-01-01")
        note = frontmatter.parse_file(path)
        self.assertEqual(note.meta["valid_from"], "2020-01-01")
        self.assertNotEqual(note.meta["created"], "2020-01-01")

    def test_appears_in_list_timeline(self):
        timeline.create_event(self.config, title="An event", valid_from="2026-05-01")
        entries = timeline.list_timeline(self.config)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "event-2026-05-01-an-event")

    def test_duplicate_id_rejected(self):
        timeline.create_event(self.config, title="Dup", valid_from="2026-01-01")
        with self.assertRaises(timeline.TimelineWriteError):
            timeline.create_event(self.config, title="Dup", valid_from="2026-01-01")


if __name__ == "__main__":
    unittest.main()
