"""Brain-core audit log — Stage 2 extension: write and sharing-change
events, across every creation path and update.update_memory(). Mirrors
test_identity.py's own audit-coverage style for Stage 1's principal/group
lifecycle events, consolidated here rather than duplicated per module."""
from __future__ import annotations

import dataclasses
import unittest

from brain import capture, decision, handoff, memoryops, projectops, timeline, update as update_mod
from tests.helpers import TempVault


class AuditTestCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def _audit_lines(self) -> list[str]:
        log_files = list(self.config.logs_dir.glob("brain-audit-*.log"))
        if not log_files:
            return []
        return log_files[0].read_text(encoding="utf-8").splitlines()

    def _as(self, principal_id: str):
        return dataclasses.replace(self.config, acting_principal=principal_id)


class TestCreationPathsAreAudited(AuditTestCase):
    def test_capture_is_audited(self):
        dest = capture.capture(self._as("principal-ania"), type_="fact", title="A fact")
        lines = self._audit_lines()
        self.assertTrue(any("event=note.write" in l and "principal=principal-ania" in l
                             and f"id={dest.stem}" in l for l in lines), lines)

    def test_create_memory_note_is_audited(self):
        dest = memoryops.create_memory(self._as("principal-ania"), "person", "Jane Doe")
        lines = self._audit_lines()
        self.assertTrue(any("event=note.write" in l and "principal=principal-ania" in l
                             and f"id={dest.stem}" in l for l in lines), lines)

    def test_create_project_is_audited(self):
        projectops.create_project(self._as("principal-ania"), id="project-widget",
                                   name="Widget", path="/tmp/example-widget")
        lines = self._audit_lines()
        self.assertTrue(any("event=note.write" in l and "principal=principal-ania" in l
                             and "id=project-widget" in l for l in lines), lines)

    def test_create_decision_is_audited(self):
        dest = decision.create_decision(self._as("principal-ania"), title="A decision")
        lines = self._audit_lines()
        self.assertTrue(any("event=note.write" in l and "principal=principal-ania" in l
                             and f"id={dest.stem}" in l for l in lines), lines)

    def test_create_timeline_event_is_audited(self):
        dest = timeline.create_event(self._as("principal-ania"), title="An event",
                                      valid_from="2026-05-01")
        lines = self._audit_lines()
        self.assertTrue(any("event=note.write" in l and "principal=principal-ania" in l
                             and f"id={dest.stem}" in l for l in lines), lines)

    def test_handoff_write_is_audited(self):
        self.config.registry_path.write_text(
            "projects:\n  - id: project-example\n    name: Example\n"
            "    path: /tmp/does-not-need-to-exist\n    status: active\n"
        )
        sections = handoff.HandoffSections(attempted="x")
        handoff.write(self._as("principal-ania"), "project-example", sections)
        lines = self._audit_lines()
        self.assertTrue(any("event=note.write" in l and "principal=principal-ania" in l
                             and "id=handoff-project-example" in l for l in lines), lines)

    def test_project_status_change_is_audited(self):
        projectops.create_project(self._as("principal-ania"), id="project-widget", name="Widget",
                                   path="/tmp/example-widget")
        projectops.set_project_status(self._as("principal-ania"), "project-widget", "on-hold")
        lines = self._audit_lines()
        self.assertTrue(any("event=note.write" in l and "principal=principal-ania" in l
                             and "status=active->on-hold" in l for l in lines), lines)


class TestUpdateMemoryIsAudited(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.vault.write_note(
            "60_KNOWLEDGE", "widget.md", id="knowledge-widget", type="knowledge",
            title="Widget notes", owner_principal="principal:marcin", audience=[],
        )

    def test_ordinary_update_is_audited_as_note_write(self):
        update_mod.update_memory(self._as("principal-marcin"), "knowledge-widget",
                                  set_fields={"status": "current"})
        lines = self._audit_lines()
        self.assertTrue(any("event=note.write" in l and "principal=principal-marcin" in l
                             and "id=knowledge-widget" in l for l in lines), lines)

    def test_successful_sharing_change_is_audited_distinctly(self):
        update_mod.update_memory(self._as("principal-marcin"), "knowledge-widget",
                                  set_fields={"audience": ["group:household"]})
        lines = self._audit_lines()
        self.assertTrue(any("event=note.sharing_change" in l
                             and "event=note.sharing_change.denied" not in l
                             and "principal=principal-marcin" in l for l in lines), lines)

    def test_denied_sharing_change_is_audited(self):
        with self.assertRaises(update_mod.UpdateError):
            update_mod.update_memory(self._as("principal-ania"), "knowledge-widget",
                                      set_fields={"audience": ["group:household"]})
        lines = self._audit_lines()
        self.assertTrue(any("event=note.sharing_change.denied" in l
                             and "principal=principal-ania" in l
                             and "owner=principal-marcin" in l for l in lines), lines)


if __name__ == "__main__":
    unittest.main()
