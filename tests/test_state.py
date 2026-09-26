"""get_operational_state() — brain state V1.

Follows the accepted design (Brain: project-sako-brain-sako-os-audit §16-18,
decision-2026-09-26-sako-brain-brain-state-design-accepted): deterministic,
read-only, compositional. These tests lock exactly that contract, not just
that the function runs.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import unittest
from datetime import date, timedelta

from brain import indexer, memoryqueue, state
from brain.handoff import HandoffSections, write as write_handoff
from tests.helpers import TempVault


class TestOperationalStateEmptyVault(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_empty_vault_returns_valid_envelope(self):
        result = state.get_operational_state(self.config)
        self.assertEqual(result.schema_version, 1)
        self.assertTrue(result.brain_version)
        for name, status in result.sources.items():
            self.assertTrue(status.ok, f"{name} unexpectedly failed: {status.error}")
        self.assertEqual(result.projects.entries, [])
        self.assertEqual(result.decisions.open, [])
        self.assertEqual(result.memory_queue.entries, [])
        self.assertEqual(result.handoffs.project_ids, [])
        self.assertEqual(result.doctor.problem_count, 0)
        self.assertEqual(result.timeline_recent.entries, [])

    def test_is_json_serializable(self):
        result = state.get_operational_state(self.config)
        json.dumps(dataclasses.asdict(result))  # must not raise

    def test_schema_version_is_stable(self):
        result = state.get_operational_state(self.config)
        self.assertEqual(result.schema_version, 1)


class TestOperationalStateProjects(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-alpha\n"
            "    name: Alpha\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
            "  - id: project-beta\n"
            "    name: Beta\n"
            "    path: /tmp/does-not-need-to-exist-either\n"
            "    status: completed\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_aggregates_projects_by_status(self):
        result = state.get_operational_state(self.config)
        self.assertEqual(result.projects.by_status, {"active": 1, "completed": 1})
        ids = sorted(e.id for e in result.projects.entries)
        self.assertEqual(ids, ["project-alpha", "project-beta"])


class TestOperationalStateDecisions(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_only_proposed_decisions_are_open(self):
        self.vault.write_note("40_DECISIONS", "decision-a.md", id="decision-a", type="decision",
                               status="proposed", title="Decision A")
        self.vault.write_note("40_DECISIONS", "decision-b.md", id="decision-b", type="decision",
                               status="decided", title="Decision B")
        indexer.rebuild(self.config)
        result = state.get_operational_state(self.config)
        open_ids = [d["id"] for d in result.decisions.open]
        self.assertEqual(open_ids, ["decision-a"])
        self.assertEqual(result.decisions.counts_by_status, {"proposed": 1, "decided": 1})

    def test_restricted_proposed_decision_omitted_by_default(self):
        path = self.vault.root / "40_DECISIONS" / "decision-restricted.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: decision-restricted\ntype: decision\nstatus: proposed\n"
            "sensitivity: restricted\n---\n\n# Restricted decision\n"
        )
        indexer.rebuild(self.config)
        result = state.get_operational_state(self.config)
        self.assertEqual(result.decisions.open, [])
        self.assertEqual(result.sensitivity_omitted["decisions"], 1)
        # Structural count is not sensitive content — it is not filtered.
        self.assertEqual(result.decisions.counts_by_status, {"proposed": 1})

    def test_restricted_proposed_decision_included_when_requested(self):
        path = self.vault.root / "40_DECISIONS" / "decision-restricted.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: decision-restricted\ntype: decision\nstatus: proposed\n"
            "sensitivity: restricted\n---\n\n# Restricted decision\n"
        )
        indexer.rebuild(self.config)
        result = state.get_operational_state(self.config, include_restricted=True)
        open_ids = [d["id"] for d in result.decisions.open]
        self.assertEqual(open_ids, ["decision-restricted"])

    def test_decisions_empty_before_index_built(self):
        self.vault.write_note("40_DECISIONS", "decision-a.md", id="decision-a", type="decision",
                               status="proposed", title="Decision A")
        # Deliberately not calling indexer.rebuild().
        result = state.get_operational_state(self.config)
        self.assertEqual(result.decisions.open, [])
        self.assertTrue(result.sources["decisions"].ok)


class TestOperationalStateMemoryQueue(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_restricted_entry_omitted_by_default(self):
        memoryqueue.add(self.config, candidate_fact="a normal fact")
        memoryqueue.add(self.config, candidate_fact="a restricted fact", sensitivity="restricted")
        result = state.get_operational_state(self.config)
        self.assertEqual(result.memory_queue.pending_count, 2)
        self.assertEqual(len(result.memory_queue.entries), 1)
        self.assertEqual(result.sensitivity_omitted["memory_queue"], 1)

    def test_restricted_entry_included_when_requested(self):
        memoryqueue.add(self.config, candidate_fact="a restricted fact", sensitivity="restricted")
        result = state.get_operational_state(self.config, include_restricted=True)
        self.assertEqual(len(result.memory_queue.entries), 1)


class TestOperationalStateHandoffs(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-alpha\n"
            "    name: Alpha\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_bare_project_id_list_only(self):
        write_handoff(self.config, "project-alpha", HandoffSections(attempted="did a thing"))
        result = state.get_operational_state(self.config)
        self.assertEqual(result.handoffs.project_ids, ["project-alpha"])
        # No age/staleness/priority field exists — guards against smuggling
        # a new "stale handoff" definition in later.
        field_names = {f.name for f in dataclasses.fields(result.handoffs)}
        self.assertEqual(field_names, {"project_ids"})


class TestOperationalStateDoctor(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_blocking_matches_gitops_constant(self):
        # Two notes sharing an id trips check_duplicate_ids, which IS in
        # gitops.BLOCKING_CHECKS.
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-dup", type="knowledge")
        self.vault.write_note("60_KNOWLEDGE", "b.md", id="knowledge-dup", type="knowledge")
        result = state.get_operational_state(self.config)
        self.assertGreaterEqual(result.doctor.problem_count, 1)
        self.assertGreaterEqual(result.doctor.blocking_count, 1)
        checks = {p.check for p in result.doctor.problems}
        self.assertIn("duplicate_ids", checks)


class TestOperationalStateTimeline(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def _write_timeline_note(self, note_id, event_date, sensitivity=None):
        path = self.vault.root / "50_TIMELINE" / f"{note_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        sens_line = f"sensitivity: {sensitivity}\n" if sensitivity else ""
        path.write_text(
            f"---\nid: {note_id}\ntype: event\nvalid_from: {event_date}\n{sens_line}---\n\n# {note_id}\n"
        )

    def test_window_filters_by_date(self):
        recent = (date.today() - timedelta(days=1)).isoformat()
        old = (date.today() - timedelta(days=30)).isoformat()
        self._write_timeline_note("event-recent", recent)
        self._write_timeline_note("event-old", old)
        indexer.rebuild(self.config)
        result = state.get_operational_state(self.config, timeline_window_days=14)
        ids = [e.id for e in result.timeline_recent.entries]
        self.assertIn("event-recent", ids)
        self.assertNotIn("event-old", ids)

    def test_restricted_timeline_note_omitted_via_canonical_join(self):
        recent = (date.today() - timedelta(days=1)).isoformat()
        self._write_timeline_note("event-restricted", recent, sensitivity="restricted")
        indexer.rebuild(self.config)
        result = state.get_operational_state(self.config)
        ids = [e.id for e in result.timeline_recent.entries]
        self.assertNotIn("event-restricted", ids)
        self.assertEqual(result.sensitivity_omitted["timeline_recent"], 1)

    def test_restricted_timeline_note_included_when_requested(self):
        recent = (date.today() - timedelta(days=1)).isoformat()
        self._write_timeline_note("event-restricted", recent, sensitivity="restricted")
        indexer.rebuild(self.config)
        result = state.get_operational_state(self.config, include_restricted=True)
        ids = [e.id for e in result.timeline_recent.entries]
        self.assertIn("event-restricted", ids)

    def test_unindexed_timeline_entry_is_omitted_not_assumed_safe(self):
        recent = (date.today() - timedelta(days=1)).isoformat()
        self._write_timeline_note("event-unindexed", recent)
        # File exists on disk (list_timeline() sees it) but the index was
        # never built, so its sensitivity cannot be verified — must not be
        # shown on the assumption it is probably fine.
        result = state.get_operational_state(self.config)
        ids = [e.id for e in result.timeline_recent.entries]
        self.assertNotIn("event-unindexed", ids)
        self.assertEqual(result.sensitivity_omitted["timeline_recent"], 1)


class TestOperationalStatePartialFailure(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_malformed_integrity_manifest_does_not_crash_whole_call(self):
        integrity_dir = self.config.integrity_dir
        integrity_dir.mkdir(parents=True, exist_ok=True)
        (integrity_dir / "manifest-20260101-000000.json").write_text("{not valid json")
        result = state.get_operational_state(self.config)
        self.assertFalse(result.sources["integrity_ref"].ok)
        self.assertIsNotNone(result.sources["integrity_ref"].error)
        # Every other section still ran fine — one bad collector must not
        # take the rest of the envelope down with it.
        self.assertTrue(result.sources["projects"].ok)
        self.assertTrue(result.sources["doctor"].ok)
        self.assertTrue(result.sources["decisions"].ok)

    def test_valid_integrity_manifest_is_read(self):
        integrity_dir = self.config.integrity_dir
        integrity_dir.mkdir(parents=True, exist_ok=True)
        (integrity_dir / "manifest-20260101-000000.json").write_text(json.dumps({
            "generated_at": "2026-01-01T00:00:00", "total_notes": 42,
            "note_count_by_type": {}, "manifest": {},
        }))
        result = state.get_operational_state(self.config)
        self.assertTrue(result.sources["integrity_ref"].ok)
        self.assertEqual(result.integrity_ref.total_notes, 42)

    def test_no_saved_manifest_is_not_an_error(self):
        result = state.get_operational_state(self.config)
        self.assertTrue(result.sources["integrity_ref"].ok)
        self.assertIsNone(result.integrity_ref.total_notes)


class TestOperationalStateDeterminism(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-alpha\n"
            "    name: Alpha\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )
        self.vault.write_note("40_DECISIONS", "decision-a.md", id="decision-a", type="decision",
                               status="proposed", title="Decision A")
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_two_calls_produce_identical_output_except_generated_at(self):
        first = dataclasses.asdict(state.get_operational_state(self.config))
        second = dataclasses.asdict(state.get_operational_state(self.config))
        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(first, second)


class TestOperationalStateScopeGuards(unittest.TestCase):
    """No AI, no network side effects — enforced statically, the same way
    TRON's test_no_special_cased_modules.py enforces its own boundary."""

    def test_module_never_references_ai_or_slow_network_calls(self):
        source = inspect.getsource(state)
        forbidden = (
            "import assistant", "ollama_demo", "backup.is_initialized",
            "backup.latest_snapshot", "import backup", "requests.", "urllib.",
        )
        for token in forbidden:
            self.assertNotIn(token, source, f"brain/state.py must not reference {token!r}")


if __name__ == "__main__":
    unittest.main()
