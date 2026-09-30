"""writepolicy.py — the shared write-safety policy used by BOTH the
in-process MCP server (mcp_server.py) and the CLI (cli.py, which the
subprocess/CLI-transport MCP bridge shells out to for every write tool).
Direct unit coverage of the shared functions themselves; tests/test_cli_*
and tests/test_mcp_server.py cover each transport's actual wiring."""
from __future__ import annotations

import unittest

from brain import writepolicy


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


class TestRequireRestrictedConfirmation(unittest.TestCase):
    def test_restricted_without_confirmation_raises(self):
        with self.assertRaises(writepolicy.WritePolicyError) as ctx:
            writepolicy.require_restricted_confirmation("restricted", False)
        self.assertIn("confirm_restricted", str(ctx.exception))
        self.assertIn("--confirm-restricted", str(ctx.exception))

    def test_restricted_with_confirmation_does_not_raise(self):
        writepolicy.require_restricted_confirmation("restricted", True)

    def test_normal_sensitivity_never_requires_confirmation(self):
        writepolicy.require_restricted_confirmation("normal", False)

    def test_private_sensitivity_never_requires_confirmation(self):
        # Only 'restricted' carries the extra friction — 'private' is a
        # real, distinct sensitivity level in this vault's vocabulary, not
        # a synonym for 'restricted'.
        writepolicy.require_restricted_confirmation("private", False)


if __name__ == "__main__":
    unittest.main()
