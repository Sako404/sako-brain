"""brain.identity — principal/group records (SAKO Brain multi-user, Stage 1).

Core invariants under test, matching the hardening requirements directly:
  - principal/group records live outside every content directory, so
    ordinary write commands cannot reach them even in principle.
  - they never appear in search/context (never indexed, never under
    content_dirs).
  - is_active() always reflects the current on-disk state, with zero
    caching anywhere in the path.
  - break_glass_restore_admin() is not reachable via any ordinary
    identity function — it is its own, clearly separate entry point.
"""
from __future__ import annotations

import unittest

from brain import identity, search
from brain.indexer import rebuild
from tests.helpers import TempVault


class IdentityTestCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()


class TestPrincipalLifecycle(IdentityTestCase):
    def test_create_get_round_trips(self):
        p = identity.create_principal(self.config, display_name="Marcin", kind="human", role="admin")
        self.assertEqual(p.id, "principal-marcin")
        self.assertEqual(p.status, "active")
        self.assertTrue(p.is_active)

        fetched = identity.get_principal(self.config, "principal-marcin")
        self.assertEqual(fetched, p)

    def test_duplicate_create_rejected(self):
        identity.create_principal(self.config, display_name="Marcin")
        with self.assertRaises(identity.IdentityError):
            identity.create_principal(self.config, display_name="Marcin")

    def test_invalid_kind_rejected(self):
        with self.assertRaises(identity.IdentityError):
            identity.create_principal(self.config, display_name="X", kind="robot")

    def test_disable_then_is_active_false(self):
        identity.create_principal(self.config, display_name="Ania")
        identity.set_principal_status(self.config, "principal-ania", "disabled")
        self.assertFalse(identity.is_active(self.config, "principal-ania"))

    def test_reenable_restores_active(self):
        identity.create_principal(self.config, display_name="Ania")
        identity.set_principal_status(self.config, "principal-ania", "disabled")
        identity.set_principal_status(self.config, "principal-ania", "active")
        self.assertTrue(identity.is_active(self.config, "principal-ania"))

    def test_invalid_status_rejected(self):
        identity.create_principal(self.config, display_name="Ania")
        with self.assertRaises(identity.IdentityError):
            identity.set_principal_status(self.config, "principal-ania", "deleted")

    def test_is_active_false_for_unknown_principal(self):
        self.assertFalse(identity.is_active(self.config, "principal-does-not-exist"))

    def test_set_role_updates_role(self):
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child")
        identity.set_principal_role(self.config, "principal-wiktor", "restricted_child")
        self.assertEqual(identity.get_principal(self.config, "principal-wiktor").role, "restricted_child")

    def test_list_principals_returns_all(self):
        identity.create_principal(self.config, display_name="Marcin")
        identity.create_principal(self.config, display_name="Ania")
        ids = {p.id for p in identity.list_principals(self.config)}
        self.assertEqual(ids, {"principal-marcin", "principal-ania"})

    def test_list_principals_empty_vault_returns_empty_not_error(self):
        self.assertEqual(identity.list_principals(self.config), [])


class TestGroupLifecycle(IdentityTestCase):
    def test_create_group_starts_with_no_members(self):
        g = identity.create_group(self.config, display_name="Household")
        self.assertEqual(g.id, "group-household")
        self.assertEqual(g.members, [])

    def test_add_member_requires_existing_principal(self):
        identity.create_group(self.config, display_name="Household")
        with self.assertRaises(identity.IdentityError):
            identity.add_group_member(self.config, "group-household", "principal-nobody")

    def test_add_and_remove_member(self):
        identity.create_principal(self.config, display_name="Marcin")
        identity.create_group(self.config, display_name="Household")
        identity.add_group_member(self.config, "group-household", "principal-marcin")
        self.assertIn("principal-marcin", identity.get_group(self.config, "group-household").members)

        identity.remove_group_member(self.config, "group-household", "principal-marcin")
        self.assertNotIn("principal-marcin", identity.get_group(self.config, "group-household").members)

    def test_add_member_twice_is_idempotent(self):
        identity.create_principal(self.config, display_name="Marcin")
        identity.create_group(self.config, display_name="Household")
        identity.add_group_member(self.config, "group-household", "principal-marcin")
        identity.add_group_member(self.config, "group-household", "principal-marcin")
        self.assertEqual(identity.get_group(self.config, "group-household").members, ["principal-marcin"])

    def test_groups_for_principal(self):
        identity.create_principal(self.config, display_name="Marcin")
        identity.create_group(self.config, display_name="Household")
        identity.create_group(self.config, display_name="Parents")
        identity.add_group_member(self.config, "group-household", "principal-marcin")
        identity.add_group_member(self.config, "group-parents", "principal-marcin")
        self.assertEqual(set(identity.groups_for_principal(self.config, "principal-marcin")),
                          {"group-household", "group-parents"})

    def test_groups_for_principal_with_no_memberships_is_empty(self):
        identity.create_principal(self.config, display_name="Marcin")
        self.assertEqual(identity.groups_for_principal(self.config, "principal-marcin"), [])


class TestDeletePrincipal(IdentityTestCase):
    def test_deletes_an_unreferenced_principal(self):
        identity.create_principal(self.config, display_name="Throwaway Test", kind="service", role="test")
        identity.delete_principal(self.config, "principal-throwaway-test")
        self.assertIsNone(identity.get_principal(self.config, "principal-throwaway-test"))

    def test_delete_nonexistent_principal_raises(self):
        with self.assertRaises(identity.IdentityError):
            identity.delete_principal(self.config, "principal-does-not-exist")

    def test_refuses_to_delete_a_principal_still_in_a_group(self):
        identity.create_principal(self.config, display_name="Ania")
        identity.create_group(self.config, display_name="Household")
        identity.add_group_member(self.config, "group-household", "principal-ania")
        with self.assertRaises(identity.IdentityError):
            identity.delete_principal(self.config, "principal-ania")
        # Still there, unaffected by the refused attempt.
        self.assertIsNotNone(identity.get_principal(self.config, "principal-ania"))

    def test_delete_is_audited(self):
        identity.create_principal(self.config, display_name="Throwaway Test")
        identity.delete_principal(self.config, "principal-throwaway-test")
        log_files = list(self.config.logs_dir.glob("brain-audit-*.log"))
        lines = log_files[0].read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("event=principal.delete" in l and "principal=principal-throwaway-test" in l
                             for l in lines), lines)


class TestBreakGlass(IdentityTestCase):
    def test_restores_disabled_principal_to_active(self):
        identity.create_principal(self.config, display_name="Marcin", role="admin")
        identity.set_principal_status(self.config, "principal-marcin", "disabled")
        self.assertFalse(identity.is_active(self.config, "principal-marcin"))

        identity.break_glass_restore_admin(self.config, "principal-marcin", "admin")
        self.assertTrue(identity.is_active(self.config, "principal-marcin"))

    def test_creates_principal_if_missing_entirely(self):
        # The genuinely worst case: the admin principal record itself was
        # somehow deleted/corrupted, not just disabled.
        self.assertIsNone(identity.get_principal(self.config, "principal-marcin"))
        p = identity.break_glass_restore_admin(self.config, "principal-marcin", "admin")
        self.assertTrue(p.is_active)
        self.assertEqual(p.role, "admin")

    def test_does_not_touch_other_principals_or_groups(self):
        identity.create_principal(self.config, display_name="Marcin", role="admin")
        identity.create_principal(self.config, display_name="Ania", role="adult")
        identity.create_group(self.config, display_name="Household")
        identity.add_group_member(self.config, "group-household", "principal-ania")

        identity.break_glass_restore_admin(self.config, "principal-marcin", "admin")

        ania = identity.get_principal(self.config, "principal-ania")
        self.assertEqual(ania.role, "adult")
        self.assertTrue(ania.is_active)
        self.assertEqual(identity.get_group(self.config, "group-household").members, ["principal-ania"])


class TestIdentityRecordsAreStructurallyIsolated(IdentityTestCase):
    """Hardening requirements 1 and 5: ordinary write/search/context paths
    must never reach these records — proven here by construction, not by
    trusting a permission check."""

    def test_principal_directory_is_outside_every_content_dir(self):
        identity.create_principal(self.config, display_name="Marcin")
        principal_path = identity._principal_path(self.config, "principal-marcin")
        rel_parts = principal_path.relative_to(self.config.brain_root).parts
        self.assertNotIn(rel_parts[0], self.config.content_dirs)

    def test_find_note_path_cannot_find_a_principal(self):
        from brain.update import find_note_path
        identity.create_principal(self.config, display_name="Marcin")
        self.assertIsNone(find_note_path(self.config, "principal-marcin"))

    def test_rebuild_never_indexes_a_principal(self):
        identity.create_principal(self.config, display_name="Marcin")
        stats = rebuild(self.config)
        self.assertEqual(stats["indexed"], 0)
        self.assertIsNone(search.get_note_row(self.config, "principal-marcin"))

    def test_principal_is_not_findable_via_search(self):
        identity.create_principal(self.config, display_name="Qorvathex unique principal name")
        rebuild(self.config)
        results = search.search(self.config, "qorvathex")
        self.assertEqual(results, [])

    def test_group_directory_is_outside_every_content_dir(self):
        identity.create_group(self.config, display_name="Household")
        group_path = identity._group_path(self.config, "group-household")
        rel_parts = group_path.relative_to(self.config.brain_root).parts
        self.assertNotIn(rel_parts[0], self.config.content_dirs)


class TestIdentityEventsAreAudited(IdentityTestCase):
    def _audit_lines(self) -> list[str]:
        log_files = list(self.config.logs_dir.glob("brain-audit-*.log"))
        if not log_files:
            return []
        return log_files[0].read_text(encoding="utf-8").splitlines()

    def test_principal_create_is_audited(self):
        identity.create_principal(self.config, display_name="Marcin")
        lines = self._audit_lines()
        self.assertTrue(any("event=principal.create" in l and "principal=principal-marcin" in l
                             for l in lines), lines)

    def test_principal_disable_is_audited(self):
        identity.create_principal(self.config, display_name="Marcin")
        identity.set_principal_status(self.config, "principal-marcin", "disabled")
        lines = self._audit_lines()
        self.assertTrue(any("event=principal.status" in l and "detail=disabled" in l for l in lines), lines)

    def test_break_glass_is_audited_with_distinct_event_name(self):
        identity.break_glass_restore_admin(self.config, "principal-marcin", "admin")
        lines = self._audit_lines()
        self.assertTrue(any("event=principal.BREAK_GLASS" in l for l in lines), lines)

    def test_audit_log_never_contains_more_than_short_detail(self):
        identity.create_principal(self.config, display_name="Marcin", role="admin")
        lines = self._audit_lines()
        for line in lines:
            self.assertLess(len(line), 400)


if __name__ == "__main__":
    unittest.main()
