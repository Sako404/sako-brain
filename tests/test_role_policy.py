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

from brain import cli, identity, rolepolicy, writepolicy
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
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False)
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        p = rolepolicy.policy_for_principal(self.config, "principal-wiktor")
        self.assertFalse(p.can_write_restricted)

    def test_policy_for_principal_is_read_live_not_cached(self):
        """Same discipline as identity.is_active()/visibility.py: a stale
        answer here could broaden access."""
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=True)
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
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
        # Pre-onboarding hardening, round 2: role policies must exist
        # BEFORE a principal with that role can become active -- this is
        # the real safe creation order, not just test setup convenience.
        # admin gets no explicit policy -- DEFAULT_POLICY already matches
        # "normal read/write of own + audience-visible records".
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=True,
                                    audience_allowlist=None)
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False,
                                    audience_allowlist=["group:household"])
        rolepolicy.set_role_policy(self.config, role="restricted_child", can_write_restricted=False,
                                    audience_allowlist=[])

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


class TestFailClosedOnMissingRolePolicy(RolePolicyTestCase):
    """Pre-onboarding hardening, round 2 (2026-10-02) -- Marcin's own
    explicit, named security requirement: a role that fails to resolve a
    policy (typo, deletion, malformed record, deployment drift) must NEVER
    silently become permissive. DEFAULT_POLICY existing at all must never
    be reachable by a real, active, non-admin principal whose role simply
    doesn't resolve -- only FAIL_CLOSED_POLICY may be reached that way."""

    def test_unknown_role_name_fails_closed_at_creation(self):
        """A typo'd/never-configured role has no policy -- creation itself
        must refuse, not silently create an active, unrestricted principal."""
        with self.assertRaises(identity.IdentityError):
            identity.create_principal(self.config, display_name="Marcel", role="restricted_chld",
                                       principal_id="principal-marcel")
        self.assertIsNone(identity.get_principal(self.config, "principal-marcel"))

    def test_typo_in_role_name_is_not_silently_treated_as_the_real_role(self):
        """The typo'd role and the real role are NOT the same string --
        configuring 'restricted_child' must not rescue 'restricted_chid'."""
        rolepolicy.set_role_policy(self.config, role="restricted_child", can_write_restricted=False,
                                    audience_allowlist=[])
        with self.assertRaises(identity.IdentityError):
            identity.create_principal(self.config, display_name="Marcel", role="restricted_chid",
                                       principal_id="principal-marcel")

    def test_deleted_role_policy_fails_closed_for_already_active_principal(self):
        """The critical drift scenario: a principal was validly activated,
        then its role's policy is deleted later (deployment drift, admin
        mistake). The next request must fail closed immediately -- never
        wait for a re-login or re-activation to notice."""
        rolepolicy.set_role_policy(self.config, role="restricted_child", can_write_restricted=False,
                                    audience_allowlist=["group:parents"])
        identity.create_principal(self.config, display_name="Marcel", role="restricted_child",
                                   principal_id="principal-marcel")
        self.assertTrue(rolepolicy.policy_for_principal(self.config, "principal-marcel").audience_allowlist)

        rolepolicy.delete_role_policy(self.config, "restricted_child")

        policy = rolepolicy.policy_for_principal(self.config, "principal-marcel")
        self.assertEqual(policy, rolepolicy.FAIL_CLOSED_POLICY)
        self.assertFalse(policy.can_write_restricted)
        self.assertEqual(policy.audience_allowlist, [])

    def test_malformed_role_policy_fails_closed_not_default(self):
        """A policy record that exists but is corrupted (a bad ref in its
        own stored audience_allowlist) must resolve as 'no valid policy',
        never fall through to the permissive default."""
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False,
                                    audience_allowlist=["group:household"])
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")

        # Corrupt the stored policy record directly, bypassing set_role_policy's
        # own validation -- simulates hand-edited or drifted-on-disk state.
        path = rolepolicy._policy_path(self.config, "standard_child")
        text = path.read_text(encoding="utf-8")
        corrupted = text.replace("- group:household", "- not-a-valid-ref")
        path.write_text(corrupted, encoding="utf-8")

        policy = rolepolicy.policy_for_principal(self.config, "principal-wiktor")
        self.assertEqual(policy, rolepolicy.FAIL_CLOSED_POLICY)

    def test_policy_disappearing_after_credential_issuance_is_caught_live(self):
        """Simulates: principal activated + gateway credential issued while
        a valid policy existed, then the policy vanishes. The principal's
        identity/credential still exist (a token could still be valid) --
        only the POLICY-CONTROLLED capabilities must fail closed, checked
        live on every call, never cached from whenever the token/session
        was created."""
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=True,
                                    audience_allowlist=None)
        identity.create_principal(self.config, display_name="Ania", role="adult",
                                   principal_id="principal-ania")
        # "Credential issuance" is simulated by nothing more than time
        # passing with the principal active -- policy_for_principal has no
        # notion of a session/token at all, so there is nothing to refresh;
        # this test proves exactly that it is never cached.
        self.assertTrue(rolepolicy.policy_for_principal(self.config, "principal-ania").can_write_restricted)

        rolepolicy.delete_role_policy(self.config, "adult")

        with self.assertRaises(rolepolicy.RolePolicyError):
            writepolicy.require_restricted_confirmation(self.config, "principal-ania", "restricted", True)
        with self.assertRaises(rolepolicy.RolePolicyError):
            rolepolicy.require_audience_allowed(self.config, "principal-ania", ["group:anyone"])

    def test_existing_active_token_cannot_exploit_missing_policy(self):
        """Reframed from the OAuth/token layer into what this module can
        actually prove directly: nothing about a principal being "already
        in a session" changes policy_for_principal's answer -- there is no
        session-scoped cache anywhere in this path for a stale token to
        exploit. Calling it twice in a row, simulating two requests on the
        same already-issued token, gives the same fail-closed answer both
        times once the policy is gone -- not permissive on a first 'cached'
        call and strict only later."""
        rolepolicy.set_role_policy(self.config, role="standard_child", can_write_restricted=False,
                                    audience_allowlist=["group:household"])
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        rolepolicy.delete_role_policy(self.config, "standard_child")

        first = rolepolicy.policy_for_principal(self.config, "principal-wiktor")
        second = rolepolicy.policy_for_principal(self.config, "principal-wiktor")
        self.assertEqual(first, rolepolicy.FAIL_CLOSED_POLICY)
        self.assertEqual(second, rolepolicy.FAIL_CLOSED_POLICY)

    def test_restricted_child_can_never_become_more_permissive_than_fail_closed(self):
        """However restricted_child's policy fails to resolve (missing,
        deleted, malformed, typo'd), the result can never grant MORE than
        FAIL_CLOSED_POLICY already grants -- i.e. restricted_child can
        never end up more permissive purely because its policy vanished."""
        identity.create_principal(self.config, display_name="X", role="",
                                   principal_id="principal-x")
        # No role assigned at all yet -- give it the real restricted_child
        # role only once a (deliberately broken) policy situation exists.
        rolepolicy.set_role_policy(self.config, role="restricted_child", can_write_restricted=False,
                                    audience_allowlist=["group:parents"])
        identity.set_principal_role(self.config, "principal-x", "restricted_child")
        rolepolicy.delete_role_policy(self.config, "restricted_child")

        policy = rolepolicy.policy_for_principal(self.config, "principal-x")
        # FAIL_CLOSED_POLICY is the strictest point in the whole space --
        # nothing else to compare against except itself.
        self.assertFalse(policy.can_write_restricted)
        self.assertEqual(policy.audience_allowlist, [])
        self.assertEqual(policy, rolepolicy.FAIL_CLOSED_POLICY)

    def test_inactive_principal_with_unresolvable_role_also_fails_closed(self):
        """A disabled principal is already denied everything by
        identity.is_active() elsewhere -- this is defense in depth, not
        the primary gate, but it must still never answer permissively."""
        rolepolicy.set_role_policy(self.config, role="standard_child", audience_allowlist=["group:household"])
        identity.create_principal(self.config, display_name="Wiktor", role="standard_child",
                                   principal_id="principal-wiktor")
        identity.set_principal_status(self.config, "principal-wiktor", "disabled")
        rolepolicy.delete_role_policy(self.config, "standard_child")

        policy = rolepolicy.policy_for_principal(self.config, "principal-wiktor")
        self.assertEqual(policy, rolepolicy.FAIL_CLOSED_POLICY)

    def test_admin_role_stays_exempt_even_without_a_policy_record(self):
        """Documents the one deliberate exemption explicitly: admin never
        needs a role-policy record, matching validate.is_admin_principal's
        own, separate admin gate."""
        identity.create_principal(self.config, display_name="Marcin", role="admin",
                                   principal_id="principal-marcin")
        self.assertEqual(rolepolicy.policy_for_principal(self.config, "principal-marcin"),
                          rolepolicy.DEFAULT_POLICY)

    def test_empty_role_stays_exempt_as_the_pre_rollout_default(self):
        identity.create_principal(self.config, display_name="Legacy", role="",
                                   principal_id="principal-legacy")
        self.assertEqual(rolepolicy.policy_for_principal(self.config, "principal-legacy"),
                          rolepolicy.DEFAULT_POLICY)

    def test_activation_of_a_disabled_principal_is_also_gated(self):
        """The staged-creation path: create disabled, then activate later
        -- activation itself must refuse if the role still has no policy
        at that point, not just at creation time."""
        identity.create_principal(self.config, display_name="Marcel", role="restricted_child",
                                   principal_id="principal-marcel", status="disabled")
        with self.assertRaises(identity.IdentityError):
            identity.set_principal_status(self.config, "principal-marcel", "active")
        self.assertEqual(identity.get_principal(self.config, "principal-marcel").status, "disabled")

        rolepolicy.set_role_policy(self.config, role="restricted_child", can_write_restricted=False,
                                    audience_allowlist=["group:parents"])
        identity.set_principal_status(self.config, "principal-marcel", "active")
        self.assertEqual(identity.get_principal(self.config, "principal-marcel").status, "active")

    def test_set_role_on_an_already_active_principal_is_also_gated(self):
        """A role CHANGE on an already-active principal must be checked
        just as strictly as creation -- an admin fat-fingering a role
        update must not leave an active principal with an unresolvable
        role in between."""
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=True)
        identity.create_principal(self.config, display_name="Ania", role="adult",
                                   principal_id="principal-ania")
        with self.assertRaises(identity.IdentityError):
            identity.set_principal_role(self.config, "principal-ania", "typo_role")
        self.assertEqual(identity.get_principal(self.config, "principal-ania").role, "adult")


if __name__ == "__main__":
    unittest.main()
