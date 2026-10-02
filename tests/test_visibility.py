"""brain.visibility — query-time visibility enforcement (SAKO Brain
multi-user, Stage 2).

Core invariants under test:
  - the owner always sees their own record.
  - an audience entry naming a principal directly grants that principal
    access, and no one else.
  - an audience entry naming a group grants access to every member of that
    group, read live via identity.groups_for_principal() — never cached.
  - a record with no owner_principal at all defaults to the same identity
    Stage 1 already treats as the default ("principal-marcin"), never to
    "visible to everyone".
  - a malformed owner_principal/audience reference denies rather than
    raising past the caller.
"""
from __future__ import annotations

import unittest

from brain import identity, visibility
from tests.helpers import TempVault


class VisibilityTestCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()


class TestOwnerAndAudience(VisibilityTestCase):
    def test_owner_always_sees_their_own_record(self):
        meta = {"owner_principal": "principal:ania", "audience": []}
        self.assertTrue(visibility.can_view(self.config, "principal-ania", meta))

    def test_non_owner_with_empty_audience_denied(self):
        meta = {"owner_principal": "principal:ania", "audience": []}
        self.assertFalse(visibility.can_view(self.config, "principal-marcin", meta))

    def test_audience_naming_a_principal_directly_grants_access(self):
        meta = {"owner_principal": "principal:wiktor", "audience": ["principal:ania"]}
        self.assertTrue(visibility.can_view(self.config, "principal-ania", meta))

    def test_audience_naming_a_principal_does_not_grant_a_third_party(self):
        meta = {"owner_principal": "principal:wiktor", "audience": ["principal:ania"]}
        self.assertFalse(visibility.can_view(self.config, "principal-marcel", meta))

    def test_audience_group_grants_access_to_every_member(self):
        identity.create_principal(self.config, display_name="Ania", principal_id="principal-ania")
        identity.create_principal(self.config, display_name="Wiktor", principal_id="principal-wiktor")
        identity.create_group(self.config, display_name="Household", group_id="group-household")
        identity.add_group_member(self.config, "group-household", "principal-ania")
        identity.add_group_member(self.config, "group-household", "principal-wiktor")

        meta = {"owner_principal": "principal:marcin", "audience": ["group:household"]}
        self.assertTrue(visibility.can_view(self.config, "principal-ania", meta))
        self.assertTrue(visibility.can_view(self.config, "principal-wiktor", meta))

    def test_audience_group_denies_a_non_member(self):
        identity.create_principal(self.config, display_name="Marcel", principal_id="principal-marcel")
        identity.create_group(self.config, display_name="Household", group_id="group-household")
        meta = {"owner_principal": "principal:marcin", "audience": ["group:household"]}
        self.assertFalse(visibility.can_view(self.config, "principal-marcel", meta))

    def test_group_membership_is_read_live_not_cached(self):
        """Removing a principal from a group must immediately revoke access
        on the next check — no caching anywhere in this path."""
        identity.create_principal(self.config, display_name="Ania", principal_id="principal-ania")
        identity.create_group(self.config, display_name="Household", group_id="group-household")
        identity.add_group_member(self.config, "group-household", "principal-ania")
        meta = {"owner_principal": "principal:marcin", "audience": ["group:household"]}
        self.assertTrue(visibility.can_view(self.config, "principal-ania", meta))

        identity.remove_group_member(self.config, "group-household", "principal-ania")
        self.assertFalse(visibility.can_view(self.config, "principal-ania", meta))

    def test_missing_owner_principal_defaults_to_marcin_not_public(self):
        meta = {"audience": []}
        self.assertTrue(visibility.can_view(self.config, "principal-marcin", meta))
        self.assertFalse(visibility.can_view(self.config, "principal-ania", meta))

    def test_malformed_owner_principal_denies_rather_than_raises(self):
        meta = {"owner_principal": "not-a-valid-reference", "audience": []}
        self.assertFalse(visibility.can_view(self.config, "principal-marcin", meta))

    def test_malformed_audience_entry_denies_rather_than_raises(self):
        meta = {"owner_principal": "principal:ania", "audience": ["garbage"]}
        self.assertFalse(visibility.can_view(self.config, "principal-marcin", meta))

    def test_audience_as_a_bare_string_is_treated_as_a_single_entry(self):
        meta = {"owner_principal": "principal:wiktor", "audience": "principal:ania"}
        self.assertTrue(visibility.can_view(self.config, "principal-ania", meta))


class TestIdRefConversion(unittest.TestCase):
    def test_id_to_ref(self):
        self.assertEqual(visibility.id_to_ref("principal-marcin"), "principal:marcin")
        self.assertEqual(visibility.id_to_ref("group-household"), "group:household")

    def test_ref_to_id_round_trips(self):
        for ref in ("principal:marcin", "group:household", "principal:ania"):
            record_id = visibility._ref_to_id(ref)
            self.assertEqual(visibility.id_to_ref(record_id), ref)


class TestFilterVisible(VisibilityTestCase):
    def test_filters_a_list_of_notes_to_the_visible_ones(self):
        from brain import frontmatter
        from pathlib import Path

        visible = frontmatter.Note(path=Path("a.md"),
                                    meta={"owner_principal": "principal:marcin", "audience": []})
        hidden = frontmatter.Note(path=Path("b.md"),
                                   meta={"owner_principal": "principal:ania", "audience": []})
        result = visibility.filter_visible(self.config, "principal-marcin", [visible, hidden])
        self.assertEqual(result, [visible])


if __name__ == "__main__":
    unittest.main()
