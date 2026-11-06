"""writepolicy.py — the shared write-safety policy used by BOTH the
in-process MCP server (mcp_server.py) and the CLI (cli.py, which the
subprocess/CLI-transport MCP bridge shells out to for every write tool).
Direct unit coverage of the shared functions themselves; tests/test_cli_*
and tests/test_mcp_server.py cover each transport's actual wiring."""
from __future__ import annotations

import unittest

from brain import identity, rolepolicy, validate, writepolicy
from tests.helpers import TempVault


class TestScanForSecrets(unittest.TestCase):
    def test_raises_on_a_known_secret_pattern(self):
        with self.assertRaises(writepolicy.WritePolicyError):
            writepolicy.scan_for_secrets("my aws key is AKIAABCDEFGHIJKLMNOP, don't lose it")

    def test_checks_every_positional_text_argument(self):
        with self.assertRaises(writepolicy.WritePolicyError):
            writepolicy.scan_for_secrets("harmless title", "aws key AKIAABCDEFGHIJKLMNOP here")

    def test_does_not_raise_on_ordinary_text(self):
        writepolicy.scan_for_secrets("just an ordinary title", "and some ordinary body text")

    def test_empty_and_none_like_strings_are_skipped_not_errored(self):
        writepolicy.scan_for_secrets("", "   ")

    def test_error_message_never_echoes_the_secret_value(self):
        secret = "AKIAABCDEFGHIJKLMNOP"
        try:
            writepolicy.scan_for_secrets(f"aws key {secret}")
            self.fail("expected WritePolicyError")
        except writepolicy.WritePolicyError as exc:
            self.assertNotIn(secret, str(exc))


# Built at runtime so no credential-shaped literal sits in the repository.
TOKEN_SHAPES = {
    "GitHub classic": "ghp_" + "a1" * 18,
    "GitHub server": "ghs_" + "a1" * 18,
    "GitHub fine-grained": "github_pat_" + "a1" * 20,
    "Slack": "xoxb-" + "1" * 12 + "-abc",
    "Stripe": "sk_" + "live_" + "a1" * 12,
    "Google": "AIza" + "a1" * 17 + "a",
    "Anthropic": "sk-" + "ant-" + "a1" * 15,
    "OpenAI": "sk-" + "proj-" + "a1" * 15,
    "GitLab": "glpat-" + "a1" * 12,
    "npm": "npm_" + "a1" * 18,
    "Hugging Face": "hf_" + "a1" * 17,
    "JWT": "eyJ" + "a1" * 8 + ".eyJ" + "a1" * 8 + "." + "a1" * 8,
}


class TestVendorTokenShapes(unittest.TestCase):
    def test_each_shape_is_refused_without_echoing_it(self):
        for vendor, token in TOKEN_SHAPES.items():
            with self.subTest(vendor=vendor):
                with self.assertRaises(writepolicy.WritePolicyError) as caught:
                    writepolicy.scan_for_secrets(f"the token is {token} for now")
                self.assertNotIn(token, str(caught.exception))

    def test_doctor_flags_each_shape_too(self):
        class FakeNote:
            path = "note.md"

        for vendor, token in TOKEN_SHAPES.items():
            with self.subTest(vendor=vendor):
                note = FakeNote()
                note.body = f"token {token}\n"
                self.assertEqual([p.check for p in validate.check_secrets([note])], ["secret_pattern"])

    def test_ordinary_text_with_similar_prefixes_is_allowed(self):
        writepolicy.scan_for_secrets(
            "task-assessment-for-the-new-server-migration-plan",
            "risk-assessment-for-the-new-server-migration-plan",
            "the ghp_ prefix, a ghp_shorttoken, and xoxb- on their own",
            "sk_live_ alone, AIza alone, glpat- alone, npm_install_packages, hf_model",
            "eyJ alone, and eyJabc.notatoken.value",
            "decision-2026-11-06-github_pat-format",
        )


class TestRequireRestrictedConfirmation(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_restricted_without_confirmation_raises(self):
        with self.assertRaises(writepolicy.WritePolicyError) as ctx:
            writepolicy.require_restricted_confirmation(self.config, "principal-marcin", "restricted", False)
        self.assertIn("confirm_restricted", str(ctx.exception))
        self.assertIn("--confirm-restricted", str(ctx.exception))

    def test_restricted_with_confirmation_does_not_raise(self):
        writepolicy.require_restricted_confirmation(self.config, "principal-marcin", "restricted", True)

    def test_normal_sensitivity_never_requires_confirmation(self):
        writepolicy.require_restricted_confirmation(self.config, "principal-marcin", "normal", False)

    def test_private_sensitivity_never_requires_confirmation(self):
        # Only 'restricted' carries the extra friction — 'private' is a
        # real, distinct sensitivity level in this vault's vocabulary, not
        # a synonym for 'restricted'.
        writepolicy.require_restricted_confirmation(self.config, "principal-marcin", "private", False)

    def test_role_policy_denial_wins_even_with_confirmation_given(self):
        # Pre-onboarding hardening: confirm_restricted=True answers "did you
        # mean to write restricted content", never "is this role allowed
        # to at all" — a role-level denial must still win.
        rolepolicy.set_role_policy(self.config, role="restricted_child", can_write_restricted=False)
        identity.create_principal(self.config, display_name="Marcel", role="restricted_child",
                                   principal_id="principal-marcel")
        with self.assertRaises(rolepolicy.RolePolicyError):
            writepolicy.require_restricted_confirmation(self.config, "principal-marcel", "restricted", True)

    def test_role_policy_allows_when_permitted(self):
        rolepolicy.set_role_policy(self.config, role="adult", can_write_restricted=True)
        identity.create_principal(self.config, display_name="Ania", role="adult",
                                   principal_id="principal-ania")
        writepolicy.require_restricted_confirmation(self.config, "principal-ania", "restricted", True)


if __name__ == "__main__":
    unittest.main()
