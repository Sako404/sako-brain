"""`brain capabilities` — self-description for any client (Claude Code,
Codex, a phone quick-capture flow, ...) so it can discover what this Brain
can do without prior knowledge of its deployment or command surface.

The command list must come from build_parser() itself (never a hand-kept
second list) so it can never silently drift from what the CLI actually
accepts, and the MCP tool list must come from mcp_server.TOOLS for the
same reason.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli, mcp_server
from tests.helpers import TempVault


def run_cli(argv, vault):
    out = io.StringIO()
    env = dict(os.environ)
    env["BRAIN_ROOT"] = str(vault.root)
    env["BRAIN_STATE_DIR"] = str(vault.state_dir)
    with patch.dict(os.environ, env, clear=False):
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
    return rc, out.getvalue()


class TestCapabilitiesCommand(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_exits_zero_and_prints_json(self):
        rc, out = run_cli(["capabilities"], self.vault)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["brain_version"], cli.__version__)

    def test_cli_commands_include_a_leaf_and_a_nested_subcommand(self):
        _, out = run_cli(["capabilities"], self.vault)
        names = {c["command"] for c in json.loads(out)["interfaces"]["cli"]["commands"]}
        self.assertIn("status", names)
        self.assertIn("project create", names)
        self.assertIn("capabilities", names)
        # Group parsers themselves (e.g. bare "project") are never leaves.
        self.assertNotIn("project", names)

    def test_every_cli_command_has_help_text(self):
        _, out = run_cli(["capabilities"], self.vault)
        commands = json.loads(out)["interfaces"]["cli"]["commands"]
        self.assertGreater(len(commands), 10)
        for c in commands:
            self.assertTrue(c["help"], f"{c['command']} has no help text")

    def test_mcp_tools_match_the_real_tools_dict(self):
        _, out = run_cli(["capabilities"], self.vault)
        listed = {t["name"] for t in json.loads(out)["interfaces"]["mcp"]["tools"]}
        self.assertEqual(listed, set(mcp_server.TOOLS.keys()))

    def test_does_not_predict_read_vs_write_permission(self):
        # Enforcement lives server-side, per connected identity — this
        # command must never claim to know it, to avoid a second copy
        # drifting from the real dispatcher allowlist.
        _, out = run_cli(["capabilities"], self.vault)
        payload = json.loads(out)
        for command in payload["interfaces"]["cli"]["commands"]:
            self.assertNotIn("read_only", command)
            self.assertNotIn("write", command)


if __name__ == "__main__":
    unittest.main()
