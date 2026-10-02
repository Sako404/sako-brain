"""Stage 2 V1 shared-record write semantics — locked explicitly by Marcin:
owner_principal may modify a record; audience grants READ visibility only;
audience membership alone must never grant edit/update/delete rights. No
editors/grants/collaboration subsystem exists for V1.

Each test below is labeled with the exact proof Marcin's own message
required. Exercised at the update.update_memory() level (the one place
every modify-existing-record path — brain update, brain project update,
the update_memory MCP tool — ultimately goes through), plus one test per
other modify-existing-record path (project status, project section,
decision supersede, handoff append) to confirm the same rule holds
everywhere, not just in the one place it was first built.
"""
from __future__ import annotations

import dataclasses
import unittest

from brain import decision, frontmatter, handoff, projectops, update as update_mod
from tests.helpers import TempVault


class WriteSemanticsTestCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def _as(self, principal_id: str):
        return dataclasses.replace(self.config, acting_principal=principal_id)


class TestCoreFiveProofs(WriteSemanticsTestCase):
    """The exact five proofs Marcin's message asked for, against
    update.update_memory()."""

    def setUp(self):
        super().setUp()
        self.path = self.vault.write_note(
            "60_KNOWLEDGE", "shared.md", id="knowledge-shared", type="knowledge",
            title="A shared record", body="Original body.",
            owner_principal="principal:marcin", audience=["principal:ania"])

    def test_visible_shared_record_can_be_read(self):
        from brain import visibility
        text = visibility.read_visible_note_text(self.config, "principal-ania", "knowledge-shared")
        self.assertIn("A shared record", text)

    def test_audience_member_cannot_modify_it(self):
        with self.assertRaises(update_mod.UpdateError):
            update_mod.update_memory(self._as("principal-ania"), "knowledge-shared",
                                      set_fields={"status": "current"})
        note = frontmatter.parse_file(self.path)
        self.assertNotEqual(note.meta.get("status"), "current")

    def test_owner_can_modify_it(self):
        update_mod.update_memory(self._as("principal-marcin"), "knowledge-shared",
                                  set_fields={"status": "current"})
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["status"], "current")

    def test_audience_member_cannot_change_its_acl(self):
        with self.assertRaises(update_mod.UpdateError):
            update_mod.update_memory(self._as("principal-ania"), "knowledge-shared",
                                      set_fields={"audience": ["group:everyone"]})
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["audience"], ["principal:ania"])  # unchanged

    def test_forged_owner_principal_cannot_take_ownership(self):
        # principal-ania tries to make the record her own by setting
        # owner_principal directly — refused BEFORE any field is touched,
        # same as every other set_fields change from a non-owner.
        with self.assertRaises(update_mod.UpdateError):
            update_mod.update_memory(self._as("principal-ania"), "knowledge-shared",
                                      set_fields={"owner_principal": "principal:ania"})
        note = frontmatter.parse_file(self.path)
        self.assertEqual(note.meta["owner_principal"], "principal:marcin")  # unchanged

    def test_append_text_alone_is_also_owner_gated(self):
        # Not just set_fields — appending body text to a shared record is
        # also a modification, and audience membership doesn't grant it.
        with self.assertRaises(update_mod.UpdateError):
            update_mod.update_memory(self._as("principal-ania"), "knowledge-shared",
                                      append_text="Ania was here.")
        note = frontmatter.parse_file(self.path)
        self.assertNotIn("Ania was here", note.body)


class TestOwnerOnlyHoldsAcrossEveryModifyPath(WriteSemanticsTestCase):
    """The same rule, proven at each of the other three places that modify
    an EXISTING record outside update_memory itself — not just asserted
    once and assumed to generalize."""

    def test_project_status_change_requires_ownership(self):
        projectops.create_project(self._as("principal-marcin"), id="project-x",
                                   name="X", path="/tmp/x")
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_status(self._as("principal-ania"), "project-x", "on-hold")
        # Owner can.
        projectops.set_project_status(self._as("principal-marcin"), "project-x", "on-hold")

    def test_project_section_update_requires_ownership(self):
        dest = projectops.create_project(self._as("principal-marcin"), id="project-x",
                                          name="X", path="/tmp/x")
        with self.assertRaises(projectops.SectionEditError):
            projectops.update_section(self._as("principal-ania"), "project-x",
                                       "Current state", "replace", "New state")
        # Owner can.
        projectops.update_section(self._as("principal-marcin"), "project-x",
                                   "Current state", "replace", "New state")

    def test_decision_supersede_requires_owning_the_old_decision(self):
        old = decision.create_decision(self._as("principal-marcin"), title="Old decision")
        old_id = frontmatter.parse_file(old).id
        before = old.read_text(encoding="utf-8")

        with self.assertRaises(decision.DecisionError):
            decision.create_decision(self._as("principal-ania"), title="New decision",
                                      supersedes=old_id)
        self.assertEqual(old.read_text(encoding="utf-8"), before)  # untouched by the refused attempt

        # Owner can.
        decision.create_decision(self._as("principal-marcin"), title="New decision 2",
                                  supersedes=old_id)
        self.assertNotEqual(old.read_text(encoding="utf-8"), before)  # now actually marked superseded

    def test_handoff_second_session_requires_ownership(self):
        self.config.registry_path.write_text(
            "projects:\n  - id: project-x\n    name: X\n"
            "    path: /tmp/x\n    status: active\n"
        )
        handoff.write(self._as("principal-marcin"), "project-x",
                      handoff.HandoffSections(attempted="first"))
        with self.assertRaises(handoff.HandoffError):
            handoff.write(self._as("principal-ania"), "project-x",
                           handoff.HandoffSections(attempted="second"))
        # Owner can.
        handoff.write(self._as("principal-marcin"), "project-x",
                       handoff.HandoffSections(attempted="second"))


if __name__ == "__main__":
    unittest.main()
