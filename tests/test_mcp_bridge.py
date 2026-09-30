"""brain/mcp_bridge.py — the subprocess/CLI-transport MCP server used when
Brain is not local (the normal desktop case: the real vault is reached only
through the `brain` client wrapper's own SSH proxy). Every tool call must
become a `brain` CLI invocation; nothing here may call brain's Python
business logic directly (that would be the second implementation this
bridge exists specifically to avoid), so these tests mock at the process
boundary (`_run_brain`) rather than at any brain.* internal function.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest.mock import patch

from brain import mcp_bridge


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["brain"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestCleanEnv(unittest.TestCase):
    def test_local_override_vars_are_stripped(self):
        env = {
            "BRAIN_ROOT": "/tmp/demoted-vault-copy",
            "BRAIN_LOCAL": "1",
            "BRAIN_STATE_DIR": "/tmp/state",
            "BRAIN_VAULT": "/tmp/vault",
            "SAKO_BRAIN_CLIENT_CONFIG": "/tmp/example-client-config/client.toml",
            "PATH": "/usr/bin",
        }
        with patch.dict(os.environ, env, clear=True):
            cleaned = mcp_bridge._clean_env()
        for key in ("BRAIN_ROOT", "BRAIN_LOCAL", "BRAIN_STATE_DIR", "BRAIN_VAULT"):
            self.assertNotIn(key, cleaned)
        # The client-proxy config must be preserved — stripping it would be
        # the opposite of the point (it's how `brain` finds the server).
        self.assertEqual(cleaned["SAKO_BRAIN_CLIENT_CONFIG"], env["SAKO_BRAIN_CLIENT_CONFIG"])
        self.assertEqual(cleaned["PATH"], "/usr/bin")


class TestBrainExecutableResolution(unittest.TestCase):
    def test_explicit_override_wins(self):
        with patch.dict(os.environ, {"BRAIN_MCP_BRIDGE_EXECUTABLE": "/custom/brain"}, clear=False):
            self.assertEqual(mcp_bridge._brain_executable(), "/custom/brain")


class TestToolsListDerivedFromCapabilities(unittest.TestCase):
    def test_remaps_input_schema_key_and_uses_real_tool_names(self):
        caps = {
            "interfaces": {"mcp": {"tools": [
                {"name": "search_memory", "description": "desc", "input_schema": {"type": "object"}},
            ]}}
        }
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout=json.dumps(caps))):
            tools = mcp_bridge._tools_list_payload()
        self.assertEqual(tools, [{"name": "search_memory", "description": "desc", "inputSchema": {"type": "object"}}])

    def test_capabilities_unreachable_raises_bridge_error(self):
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stderr="refused", returncode=2)):
            with self.assertRaises(mcp_bridge.BridgeError):
                mcp_bridge._tools_list_payload()


class TestToolsListAdvertisedMatchesExecutable(unittest.TestCase):
    """Contract: `tools/list` must never advertise a tool `tools/call` cannot
    execute (the bridge's own documented invariant — see mcp_bridge.py's
    module docstring). This is regression coverage for the v0.9.2 parity
    audit, which found `tools/list` blindly forwarding all of Brain's real
    tools (20, per `brain capabilities`) while only 9 had a TRANSLATORS
    entry — a client could see and attempt to call 11 tools that would
    always fail with 'not yet supported'."""

    def test_tools_list_drops_tools_with_no_translator(self):
        caps = {
            "interfaces": {"mcp": {"tools": [
                {"name": "search_memory", "description": "d", "input_schema": {}},
                {"name": "get_project", "description": "d", "input_schema": {}},
                {"name": "create_decision", "description": "d", "input_schema": {}},
            ]}}
        }
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout=json.dumps(caps))):
            tools = mcp_bridge._tools_list_payload()
        self.assertEqual({t["name"] for t in tools}, {"search_memory"})

    def test_advertised_tools_are_always_a_subset_of_translators(self):
        """General form: no matter what `brain capabilities` returns, every
        name tools/list surfaces must be in TRANSLATORS — and every
        translator this bridge has should surface when Brain still knows
        about that name, so nothing silently drops out of the other end."""
        caps = {
            "interfaces": {"mcp": {"tools": [
                {"name": name, "description": "d", "input_schema": {}}
                for name in [*mcp_bridge.TRANSLATORS, "some_future_tool_not_yet_wired"]
            ]}}
        }
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout=json.dumps(caps))):
            tools = mcp_bridge._tools_list_payload()
        advertised = {t["name"] for t in tools}
        self.assertTrue(advertised.issubset(mcp_bridge.TRANSLATORS.keys()))
        self.assertEqual(advertised, set(mcp_bridge.TRANSLATORS.keys()))


class TestTranslators(unittest.TestCase):
    def test_search_memory_argv(self):
        argv, stdin = mcp_bridge._t_search_memory({"query": "widget", "limit": 5})
        self.assertEqual(argv, ["search", "widget", "--limit", "5", "--json"])
        self.assertIsNone(stdin)

    def test_get_context_restricted_flag(self):
        argv, _ = mcp_bridge._t_get_context({"query": "widget", "include_restricted": True})
        self.assertIn("--restricted", argv)

    def test_get_context_default_no_restricted_flag(self):
        argv, _ = mcp_bridge._t_get_context({"query": "widget"})
        self.assertNotIn("--restricted", argv)

    def test_remember_defaults_source_to_client_name(self):
        mcp_bridge._CURRENT_CLIENT = "codex"
        try:
            argv, _ = mcp_bridge._t_remember({"type": "fact", "title": "x"})
        finally:
            mcp_bridge._CURRENT_CLIENT = "unknown"
        self.assertIn("--source", argv)
        self.assertEqual(argv[argv.index("--source") + 1], "mcp-bridge:codex")

    def test_remember_explicit_source_overrides_client_default(self):
        argv, _ = mcp_bridge._t_remember({"type": "fact", "title": "x", "source": "explicit"})
        self.assertEqual(argv[argv.index("--source") + 1], "explicit")

    def test_write_handoff_builds_stdin_json_with_client_provenance(self):
        mcp_bridge._CURRENT_CLIENT = "claude-code"
        try:
            argv, stdin = mcp_bridge._t_write_handoff({
                "project_id": "project-example", "attempted": "did a thing",
            })
        finally:
            mcp_bridge._CURRENT_CLIENT = "unknown"
        self.assertEqual(argv, ["handoff", "write", "--project", "project-example"])
        payload = json.loads(stdin)
        self.assertEqual(payload["source"], "claude-code")
        self.assertEqual(payload["attempted"], "did a thing")

    def test_update_project_status_argv(self):
        argv, _ = mcp_bridge._t_update_project_status({"id": "project-example", "status": "on-hold"})
        self.assertEqual(argv, ["project", "update", "project-example", "--status", "on-hold", "--json"])


class TestExecuteTool(unittest.TestCase):
    def test_unsupported_tool_raises_clear_error(self):
        with self.assertRaises(mcp_bridge.BridgeError) as ctx:
            mcp_bridge._execute_tool("create_decision", {})
        self.assertIn("not yet supported", str(ctx.exception))

    def test_nonzero_exit_raises_with_stderr(self):
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stderr="refused: nope", returncode=2)):
            with self.assertRaises(mcp_bridge.BridgeError) as ctx:
                mcp_bridge._execute_tool("search_memory", {"query": "x"})
        self.assertIn("refused: nope", str(ctx.exception))

    def test_search_memory_returns_parsed_json(self):
        payload = {"results": [{"id": "knowledge-x"}]}
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout=json.dumps(payload))):
            result = mcp_bridge._execute_tool("search_memory", {"query": "x"})
        self.assertEqual(result, payload)

    def test_read_memory_wraps_raw_text_not_json_parsed(self):
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout="---\nid: x\n---\nbody")):
            result = mcp_bridge._execute_tool("read_memory", {"id": "x"})
        self.assertEqual(result["id"], "x")
        self.assertIn("body", result["content"])

    def test_write_handoff_parses_the_fixed_success_line(self):
        stdout = "Handoff written: 30_PROJECTS/ACTIVE/project-example-handoff.md\n"
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout=stdout)):
            result = mcp_bridge._execute_tool("write_handoff", {"project_id": "project-example", "attempted": "x"})
        self.assertEqual(result, {"updated_path": "30_PROJECTS/ACTIVE/project-example-handoff.md"})

    def test_write_handoff_unexpected_output_raises_clear_error(self):
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout="something unexpected")):
            with self.assertRaises(mcp_bridge.BridgeError):
                mcp_bridge._execute_tool("write_handoff", {"project_id": "project-example", "attempted": "x"})

    def test_list_projects_parses_two_line_records(self):
        stdout = (
            "project-alpha  [active]  Alpha Project\n"
            "    /tmp/example-projects/alpha\n"
            "project-beta  [on-hold]  Beta Project\n"
            "    /tmp/example-projects/beta\n"
        )
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout=stdout)):
            result = mcp_bridge._execute_tool("list_projects", {})
        self.assertEqual(len(result["projects"]), 2)
        self.assertEqual(result["projects"][0], {
            "id": "project-alpha", "status": "active", "name": "Alpha Project",
            "path": "/tmp/example-projects/alpha",
        })

    def test_update_project_status_reshapes_to_flat_mcp_tool_shape(self):
        cli_payload = {
            "id": "project-example",
            "status_change": {"old_status": "active", "new_status": "on-hold", "moved": True, "registry_updated": True, "updated_path": "x.md"},
            "updated_path": None,
        }
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout=json.dumps(cli_payload))):
            result = mcp_bridge._execute_tool("update_project_status", {"id": "project-example", "status": "on-hold"})
        self.assertEqual(result, {
            "id": "project-example", "old_status": "active", "new_status": "on-hold",
            "moved": True, "registry_updated": True, "updated_path": "x.md",
        })

    def test_non_json_output_raises_clear_bridge_error(self):
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout="not json")):
            with self.assertRaises(mcp_bridge.BridgeError):
                mcp_bridge._execute_tool("search_memory", {"query": "x"})


class TestHandleRequest(unittest.TestCase):
    def test_initialize_captures_client_name(self):
        mcp_bridge.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "codex"}},
        })
        self.assertEqual(mcp_bridge._CURRENT_CLIENT, "codex")

    def test_tools_call_unknown_method_returns_json_rpc_error(self):
        resp = mcp_bridge.handle_request({"jsonrpc": "2.0", "id": 1, "method": "not/a/method"})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tools_call_success_wraps_result_as_text_content(self):
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stdout=json.dumps({"results": []}))):
            resp = mcp_bridge.handle_request({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search_memory", "arguments": {"query": "x"}},
            })
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload, {"results": []})

    def test_tools_call_failure_returns_json_rpc_error_not_a_crash(self):
        with patch.object(mcp_bridge, "_run_brain", return_value=_completed(stderr="refused", returncode=2)):
            resp = mcp_bridge.handle_request({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "search_memory", "arguments": {"query": "x"}},
            })
        self.assertIn("error", resp)

    def test_notification_with_no_id_gets_no_response(self):
        resp = mcp_bridge.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(resp)


if __name__ == "__main__":
    unittest.main()
