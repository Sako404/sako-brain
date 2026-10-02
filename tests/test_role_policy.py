"""Role policy (rolepolicy.py) — pre-onboarding hardening (2026-10-02).

Before this module existed, `role` was consulted in exactly one place in
the whole codebase (validate.is_admin_principal, gating `brain doctor`/
`brain state` only) — adult/standard_child/restricted_child were labels
with no behavioural difference. Marcin's own stated concern: "I don't want
a situation where restricted_child is just a label while functionally
having exactly the same capabilities as adult."

TestAdversarialFamilyScenario is the direct proof of that concern being
addressed — it drives the actual CLI transport (what a remote principal
genuinely uses, via the SSH dispatcher + mcp_bridge.py) through a
realistic four-principal family setup and shows Marcel (restricted_child)
cannot do several things Ania (adult) can, end to end, not just at the
level of a policy object's own fields.
"""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from brain import cli, identity, rolepolicy
from tests.helpers import TempVault


def run_cli(argv, vault, acting_principal=None):
    out = io.StringIO()
    err = io.StringIO()
    env = dict(os.environ)
    env["BRAIN_ROOT"] = str(vault.root)
    env["BRAIN_STATE_DIR"] = str(vault.state_dir)
    if acting_principal:
        env["BRAIN_CALLER_PRINCIPAL"] = acting_principal
    else:
        env.pop("BRAIN_CALLER_PRINCIPAL", None)
    with patch.dict(os.environ, env, clear=False):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
    return rc, out.getvalue() + err.getvalue()


class RolePolicyTestCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()


class TestRolePolicyStorage(RolePolicyTestCase):
    def test_no_policy_is_fully_permissive_default(self):
        """Pure-addition guarantee: a role nobody has ever restricted
        behaves exactly like Stage 2 before this module existed."""
        self.assertEqual(rolepolicy.get_role_policy(self.config, "adult"), rolepolicy.DEFAULT_POLICY)

    def test_empty_role_string_is_also_the_default(self):
        self.assertEqual(rolepolicy.get_role_policy(self.config, ""), rolepolicy.DEFAULT_POLICY)

    def test_set_then_get_round_trips(self):
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False,
                                    audience_allowlist=["group:household"])
        p = rolepolicy.get_role_policy(self.config, "standard_child")
        self.assertFalse(p.can_write_restricted)
        self.assertEqual(p.audience_allowlist, ["group-household"])

    def test_empty_audience_allowlist_means_never_share_beyond_private(self):
        rolepolicy.set_role_policy(self.config, role="restricted_child", audience_allowlist=[])
        p = rolepolicy.get_role_policy(self.config, "restricted_child")
        self.assertEqual(p.audience_allowlist, [])
        self.assertIsNotNone(p.audience_allowlist)  # [] is not None -- distinct meanings

    def test_omitted_audience_allowlist_means_unrestricted(self):
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=True)
        p = rolepolicy.get_role_policy(self.config, "adult")
        self.assertIsNone(p.audience_allowlist)

    def test_set_is_idempotent_and_replaces_not_merges(self):
        rolepolicy.set_role_policy(self.config, role="adult", audience_allowlist=["group:household"])
        rolepolicy.set_role_policy(self.config, role="adult", audience_allowlist=["group:parents"])
        p = rolepolicy.get_role_policy(self.config, "adult")
        self.assertEqual(p.audience_allowlist, ["group-parents"])

    def test_list_role_policies(self):
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=True)
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False)
        roles = {r for r, _ in rolepolicy.list_role_policies(self.config)}
        self.assertEqual(roles, {"adult", "standard_child"})

    def test_delete_reverts_to_default(self):
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=False)
        rolepolicy.delete_role_policy(self.config, "adult")
        self.assertEqual(rolepolicy.get_role_policy(self.config, "adult"), rolepolicy.DEFAULT_POLICY)

    def test_delete_unknown_role_raises(self):
        with self.assertRaises(rolepolicy.RolePolicyError):
            rolepolicy.delete_role_policy(self.config, "no-such-role")

    def test_set_rejects_empty_role(self):
        with self.assertRaises(rolepolicy.RolePolicyError):
            rolepolicy.set_role_policy(self.config, role="")

    def test_malformed_audience_ref_in_policy_rejected_up_front(self):
        with self.assertRaises(Exception):
            rolepolicy.set_role_policy(self.config, role="adult", audience_allowlist=["not-a-valid-ref"])

    def test_policy_for_principal_follows_live_role(self):
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False)
        p = rolepolicy.policy_for_principal(self.config, "principal-wiktor")
        self.assertFalse(p.can_write_restricted)

    def test_policy_for_principal_is_read_live_not_cached(self):
        """Same discipline as identity.is_active()/visibility.py: a stale
        answer here could broaden access."""
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=True)
        self.assertTrue(rolepolicy.policy_for_principal(self.config, "principal-wiktor").can_write_restricted)
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False)
        self.assertFalse(rolepolicy.policy_for_principal(self.config, "principal-wiktor").can_write_restricted)

    def test_unknown_principal_gets_default_policy(self):
        self.assertEqual(rolepolicy.policy_for_principal(self.config, "principal-does-not-exist"),
                          rolepolicy.DEFAULT_POLICY)

    def test_principal_with_no_role_set_gets_default_policy(self):
        identity.create_principal(self.config, display_name="Nobody", role="",
                                   principal_id="principal-nobody")
        self.assertEqual(rolepolicy.policy_for_principal(self.config, "principal-nobody"),
                          rolepolicy.DEFAULT_POLICY)


class TestRequireRestrictedWriteAllowed(RolePolicyTestCase):
    def test_non_restricted_sensitivity_never_checked(self):
        rolepolicy.set_role_policy(self.config, role="x", can_write_restricted=False)
        identity.create_principal(self.config, display_name="X", role="x", principal_id="principal-x")
        rolepolicy.require_restricted_write_allowed(self.config, "principal-x", "normal")  # no raise

    def test_denies_when_role_disallows(self):
        rolepolicy.set_role_policy(self.config, role="restricted_child", can_write_restricted=False)
        identity.create_principal(self.config, display_name="Marcel", role="restricted_child",
                                   principal_id="principal-marcel")
        with self.assertRaises(rolepolicy.RolePolicyError):
            rolepolicy.require_restricted_write_allowed(self.config, "principal-marcel", "restricted")

    def test_allows_when_role_permits(self):
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=True)
        identity.create_principal(self.config, display_name="Ania", role="adult",
                                   principal_id="principal-ania")
        rolepolicy.require_restricted_write_allowed(self.config, "principal-ania", "restricted")  # no raise


class TestRequireAudienceAllowed(RolePolicyTestCase):
    def test_unrestricted_when_no_policy(self):
        rolepolicy.require_audience_allowed(self.config, "principal-marcin", ["group:anything"])  # no raise

    def test_empty_or_none_audience_never_checked(self):
        rolepolicy.set_role_policy(self.config, role="restricted_child", audience_allowlist=[])
        identity.create_principal(self.config, display_name="Marcel", role="restricted_child",
                                   principal_id="principal-marcel")
        rolepolicy.require_audience_allowed(self.config, "principal-marcel", None)
        rolepolicy.require_audience_allowed(self.config, "principal-marcel", [])

    def test_allowed_target_passes(self):
        rolepolicy.set_role_policy(self.config, role="standard_child", audience_allowlist=["group:household"])
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        rolepolicy.require_audience_allowed(self.config, "principal-wiktor", ["group:household"])  # no raise

    def test_disallowed_target_refused(self):
        rolepolicy.set_role_policy(self.config, role="standard_child", audience_allowlist=["group:household"])
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        with self.assertRaises(rolepolicy.RolePolicyError):
            rolepolicy.require_audience_allowed(self.config, "principal-wiktor", ["group:everyone"])

    def test_empty_allowlist_refuses_any_target(self):
        rolepolicy.set_role_policy(self.config, role="restricted_child", audience_allowlist=[])
        identity.create_principal(self.config, display_name="Marcel", role="restricted_child",
                                   principal_id="principal-marcel")
        with self.assertRaises(rolepolicy.RolePolicyError):
            rolepolicy.require_audience_allowed(self.config, "principal-marcel", ["group:household"])

    def test_one_disallowed_target_among_several_refuses_the_whole_write(self):
        rolepolicy.set_role_policy(self.config, role="standard_child", audience_allowlist=["group:household"])
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        with self.assertRaises(rolepolicy.RolePolicyError):
            rolepolicy.require_audience_allowed(self.config, "principal-wiktor",
                                                 ["group:household", "group:everyone"])


class TestRolePolicyCliCommands(RolePolicyTestCase):
    def test_set_then_show(self):
        rc, _ = run_cli(["role-policy", "set", "standard_child", "--no-write-restricted",
                          "--audience-allowlist", "group:household"], self.vault)
        self.assertEqual(rc, 0)
        rc, out = run_cli(["role-policy", "show", "standard_child", "--json"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("can_write_restricted", out)
        self.assertIn("false", out.lower())

    def test_audience_allowlist_with_no_values_means_empty_not_unrestricted(self):
        rc, _ = run_cli(["role-policy", "set", "restricted_child", "--audience-allowlist"], self.vault)
        self.assertEqual(rc, 0)
        p = rolepolicy.get_role_policy(self.config, "restricted_child")
        self.assertEqual(p.audience_allowlist, [])

    def test_list_and_delete(self):
        run_cli(["role-policy", "set", "adult"], self.vault)
        rc, out = run_cli(["role-policy", "list", "--json"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("adult", out)
        rc, _ = run_cli(["role-policy", "delete", "adult"], self.vault)
        self.assertEqual(rc, 0)
        self.assertEqual(rolepolicy.get_role_policy(self.config, "adult"), rolepolicy.DEFAULT_POLICY)


class TestAdversarialFamilyScenario(RolePolicyTestCase):
    """End-to-end proof, through the actual CLI transport, of a realistic
    four-principal family deployment matching Marcin's own exact spec.
    Marcel (restricted_child) must be functionally different from Ania
    (adult) — not just a differently-spelled label."""

    def setUp(self):
        super().setUp()
        identity.create_principal(self.config, display_name="Marcin", role="admin",
                                   principal_id="principal-marcin")
        identity.create_principal(self.config, display_name="Ania", role="adult",
                                   principal_id="principal-ania")
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        identity.create_principal(self.config, display_name="Marcel", role="restricted_child",
                                   principal_id="principal-marcel")
        identity.create_group(self.config, display_name="Household", group_id="group-household")
        identity.create_group(self.config, display_name="Parents", group_id="group-parents")
        for pid in ("principal-marcin", "principal-ania", "principal-wiktor", "principal-marcel"):
            identity.add_group_member(self.config, "group-household", pid)
        identity.add_group_member(self.config, "group-parents", "principal-marcin")
        identity.add_group_member(self.config, "group-parents", "principal-ania")

        # admin gets no explicit policy -- DEFAULT_POLICY already matches
        # "normal read/write of own + audience-visible records".
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=True,
                                    audience_allowlist=None)
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False,
                                    audience_allowlist=["group:household"])
        rolepolicy.set_role_policy(self.config, role="restricted_child", can_write_restricted=False,
                                    audience_allowlist=[])

    def test_admin_needs_no_explicit_policy_to_write_normally(self):
        rc, out = run_cli(["note", "create", "--type", "knowledge", "--title", "Marcin's note"],
                           self.vault, acting_principal="principal-marcin")
        self.assertEqual(rc, 0, out)

    def test_adult_can_write_restricted_content_on_own_record(self):
        rc, out = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Ania's restricted note",
             "--sensitivity", "restricted", "--confirm-restricted"],
            self.vault, acting_principal="principal-ania",
        )
        self.assertEqual(rc, 0, out)

    def test_standard_child_cannot_write_restricted_content_even_with_confirmation(self):
        rc, _ = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Wiktor tries restricted",
             "--sensitivity", "restricted", "--confirm-restricted"],
            self.vault, acting_principal="principal-wiktor",
        )
        self.assertEqual(rc, 1)

    def test_restricted_child_cannot_write_restricted_content_either(self):
        rc, _ = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Marcel tries restricted",
             "--sensitivity", "restricted", "--confirm-restricted"],
            self.vault, acting_principal="principal-marcel",
        )
        self.assertEqual(rc, 1)

    def test_standard_child_can_share_to_the_approved_household_group(self):
        rc, out = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Wiktor shares with household",
             "--audience", "group:household"],
            self.vault, acting_principal="principal-wiktor",
        )
        self.assertEqual(rc, 0, out)

    def test_standard_child_cannot_broaden_to_an_unapproved_target(self):
        rc, _ = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Wiktor overreaches",
             "--audience", "group:parents"],
            self.vault, acting_principal="principal-wiktor",
        )
        self.assertEqual(rc, 1)

    def test_restricted_child_cannot_share_beyond_private_at_all(self):
        rc, _ = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Marcel tries to share",
             "--audience", "group:household"],
            self.vault, acting_principal="principal-marcel",
        )
        self.assertEqual(rc, 1)

    def test_restricted_child_can_still_create_a_private_record(self):
        rc, out = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Marcel's own private note"],
            self.vault, acting_principal="principal-marcel",
        )
        self.assertEqual(rc, 0, out)

    def test_standard_child_cannot_retroactively_broaden_audience_via_update(self):
        import dataclasses

        from brain import indexer, search as search_mod

        rc, out = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Wiktor's own note", "--json"],
            self.vault, acting_principal="principal-wiktor",
        )
        self.assertEqual(rc, 0, out)
        indexer.rebuild(self.config)
        wiktor_config = dataclasses.replace(self.config, acting_principal="principal-wiktor")
        results = search_mod.search(wiktor_config, "Wiktor's own note")
        self.assertTrue(results)
        note_id = results[0].id
        rc, _ = run_cli(["update", note_id, "--audience", "group:parents"],
                         self.vault, acting_principal="principal-wiktor")
        self.assertEqual(rc, 1)

    def test_marcel_is_not_functionally_identical_to_ania(self):
        """The exact scenario Marcin named: restricted_child must not just
        be a label while behaving exactly like adult."""
        ania_rc, _ = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Ania restricted content",
             "--sensitivity", "restricted", "--confirm-restricted"],
            self.vault, acting_principal="principal-ania",
        )
        marcel_rc, _ = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Marcel restricted content",
             "--sensitivity", "restricted", "--confirm-restricted"],
            self.vault, acting_principal="principal-marcel",
        )
        self.assertEqual(ania_rc, 0)
        self.assertEqual(marcel_rc, 1)
        self.assertNotEqual(ania_rc, marcel_rc)

    def test_groups_and_roles_stay_orthogonal(self):
        """A role policy restricts what a principal may NAME as audience
        when sharing their OWN records -- it must never touch group
        membership itself (who may SEE a shared record stays entirely
        visibility.py's/identity.py's own concern)."""
        rc, _ = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "Ania shares with household",
             "--audience", "group:household"],
            self.vault, acting_principal="principal-ania",
        )
        self.assertEqual(rc, 0)
        self.assertIn("principal-marcel", identity.get_group(self.config, "group-household").members)
        self.assertIn("principal-wiktor", identity.get_group(self.config, "group-household").members)


if __name__ == "__main__":
    unittest.main()
