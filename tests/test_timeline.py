"""brain.timeline — listing (pre-existing) and create_event() (new): the
fixed-schema `50_TIMELINE/event-<date>-<slug>.md` primitive extracted from
what `/timeline`'s "Adding an event" step already does via direct Write."""
from __future__ import annotations

import dataclasses
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


class TestSupersedeEvent(unittest.TestCase):
    """An event that replaces the state an older one described (a problem
    logged, then fixed) marks the old one superseded, so state readers stop
    reporting the old title as current."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.old = timeline.create_event(self.config, title="Alert channel down",
                                         valid_from="2026-10-22",
                                         what_happened="Token expired.")
        self.old_id = frontmatter.parse_file(self.old).id

    def tearDown(self):
        self.vault.cleanup()

    def test_marks_old_event_and_links_forward(self):
        new = timeline.create_event(self.config, title="Alert channel fixed",
                                    valid_from="2026-10-22", supersedes=self.old_id)
        new_note = frontmatter.parse_file(new)
        old_note = frontmatter.parse_file(self.old)
        self.assertEqual(new_note.meta["supersedes"], self.old_id)
        self.assertEqual(old_note.status, "superseded")
        self.assertIn(f"Superseded by [[{new_note.id}]]", old_note.body)

    def test_never_touches_old_body(self):
        timeline.create_event(self.config, title="Alert channel fixed",
                              valid_from="2026-10-22", supersedes=self.old_id)
        self.assertIn("Token expired.", frontmatter.parse_file(self.old).body)

    def test_list_timeline_reports_status_and_superseded_by(self):
        new = timeline.create_event(self.config, title="Alert channel fixed",
                                    valid_from="2026-10-22", supersedes=self.old_id)
        by_id = {e.id: e for e in timeline.list_timeline(self.config)}
        self.assertEqual(by_id[self.old_id].status, "superseded")
        self.assertEqual(by_id[self.old_id].superseded_by, frontmatter.parse_file(new).id)
        self.assertEqual(by_id[frontmatter.parse_file(new).id].superseded_by, "")

    def test_unknown_target_fails_without_creating_anything(self):
        before = sorted(self.config.timeline_dir.glob("*.md"))
        with self.assertRaises(timeline.TimelineWriteError):
            timeline.create_event(self.config, title="Fix", valid_from="2026-10-22",
                                  supersedes="event-does-not-exist")
        self.assertEqual(sorted(self.config.timeline_dir.glob("*.md")), before)

    def test_target_must_be_an_event(self):
        from brain import decision
        d = decision.create_decision(self.config, title="Some call", date="2026-10-01")
        with self.assertRaises(timeline.TimelineWriteError):
            timeline.create_event(self.config, title="Fix", valid_from="2026-10-22",
                                  supersedes=frontmatter.parse_file(d).id)
        self.assertNotEqual(frontmatter.parse_file(d).status, "superseded")


class TestTimelineVisibility(unittest.TestCase):
    """Stage 2 (multi-user visibility): list_timeline filters directly
    (unlike registry.py, nothing needs the unfiltered list for write/
    integrity correctness — see timeline.py's own docstring on this)."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("50_TIMELINE", "event-private.md",
                               id="event-private", type="event", title="Marcin's private event",
                               created="2026-01-01", owner_principal="principal:marcin", audience=[])
        self.vault.write_note("50_TIMELINE", "event-shared.md",
                               id="event-shared", type="event", title="Shared event",
                               created="2026-01-02", owner_principal="principal:marcin",
                               audience=["principal:ania"])

    def tearDown(self):
        self.vault.cleanup()

    def _as(self, principal_id: str):
        return dataclasses.replace(self.config, acting_principal=principal_id)

    def test_owner_sees_both_events(self):
        entries = timeline.list_timeline(self._as("principal-marcin"))
        self.assertEqual({e.id for e in entries}, {"event-private", "event-shared"})

    def test_audience_principal_sees_only_the_shared_event(self):
        entries = timeline.list_timeline(self._as("principal-ania"))
        self.assertEqual({e.id for e in entries}, {"event-shared"})

    def test_unrelated_third_party_sees_nothing(self):
        entries = timeline.list_timeline(self._as("principal-marcel"))
        self.assertEqual(entries, [])

    def test_explicit_principal_id_argument_overrides_config(self):
        # Callers that already have a resolved principal_id (rather than
        # relying on config.acting_principal) can pass it directly.
        entries = timeline.list_timeline(self.config, principal_id="principal-ania")
        self.assertEqual({e.id for e in entries}, {"event-shared"})


class TestCreateEventSetsOwnership(unittest.TestCase):
    """Stage 2 (multi-user visibility): same default-ownership-on-write
    rule as capture.capture — see tests/test_capture.py."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_owner_principal_is_the_acting_principal(self):
        config = dataclasses.replace(self.config, acting_principal="principal-ania")
        dest = timeline.create_event(config, title="An event", valid_from="2026-05-01")
        note = frontmatter.parse_file(dest)
        self.assertEqual(note.meta["owner_principal"], "principal:ania")

    def test_audience_defaults_to_private(self):
        dest = timeline.create_event(self.config, title="An event", valid_from="2026-05-01")
        note = frontmatter.parse_file(dest)
        self.assertEqual(note.meta["audience"], [])

    def test_explicit_audience_is_honored(self):
        dest = timeline.create_event(self.config, title="An event", valid_from="2026-05-01",
                                      audience=["group:household"])
        note = frontmatter.parse_file(dest)
        self.assertEqual(note.meta["audience"], ["group:household"])


if __name__ == "__main__":
    unittest.main()
