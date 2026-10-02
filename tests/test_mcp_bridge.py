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
                # queue_memory is deliberately, permanently unsupported here —
                # the dispatcher's security boundary doesn't allow `brain
                # memory` at all (see decision record), not merely "not yet
                # wired up" — so it's a stable fixture for "no translator".
                {"name": "queue_memory", "description": "d", "input_schema": {}},
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

    # Deliberately, permanently unsupported over this bridge — see
    # mcp_bridge.py's own module docstring (the dispatcher's security
    # boundary doesn't allow `brain memory` at all, not an oversight).
    DELIBERATELY_UNBRIDGED_TOOLS = {"queue_memory"}

    def test_translators_has_no_gap_against_the_real_tool_source(self):
        """Pre-onboarding hardening (2026-10-02): the test above only proves
        TRANSLATORS is internally consistent against a *synthetic* caps
        fixture built FROM TRANSLATORS itself — it can never notice a tool
        that mcp_server.TOOLS (the actual, hand-maintained source of truth
        `brain capabilities` serves) has grown and TRANSLATORS has not. That
        is exactly the shape of the historical v0.9.2 incident this module's
        docstring references (20 real tools, only 9 translated) — caught
        that time by a manual audit, not by any automated test. This closes
        that gap directly: no synthetic fixture, no mock, a straight
        comparison of the two real, in-process dicts a human would otherwise
        have to remember to compare by hand."""
        from brain import mcp_server

        real_tools = set(mcp_server.TOOLS.keys())
        bridged_tools = set(mcp_bridge.TRANSLATORS.keys())
        self.assertEqual(
            real_tools - self.DELIBERATELY_UNBRIDGED_TOOLS, bridged_tools,
            "mcp_server.TOOLS and mcp_bridge.TRANSLATORS have drifted — a new "
            "tool needs a translator (or an explicit addition to "
            "DELIBERATELY_UNBRIDGED_TOOLS above, with a reason).",
        )


class TestTranslatorsPreferJsonOverTextParsing(unittest.TestCase):
    """Contract: a translator should build a `brain` invocation that emits
    machine-readable JSON, not rely on _execute_tool parsing human-readable
    text — the explicit v0.10.0 preference (stable --json over a new
    parser) for every mapping added this pass. A short, named allowlist of
    pre-existing exceptions is fine (each documented in _execute_tool's own
    special-casing); a translator silently added without --json and
    without joining that allowlist is the drift this test exists to catch."""

    # Tools whose underlying CLI command has no --json mode (by design —
    # `brain projects`/`brain handoff write`/`brain get` predate --json and
    # each is parsed in _execute_tool instead, see its own comments there).
    TEXT_PARSED_EXCEPTIONS = {"read_memory", "list_projects", "write_handoff"}

    MINIMAL_ARGS = {
        "search_memory": {"query": "x"},
        "get_context": {"query": "x"},
        "read_memory": {"id": "x"},
        "list_projects": {},
        "get_operational_state": {},
        "remember": {"type": "fact", "title": "x"},
        "create_memory_note": {"type": "fact", "title": "x"},
        "update_project_status": {"id": "x", "status": "active"},
        "write_handoff": {"project_id": "x"},
        "get_project": {"id": "x"},
        "search_timeline": {},
        "update_memory": {"id": "x"},
        "create_decision": {"title": "x"},
        "create_project": {"id": "x", "name": "n", "path": "/tmp/x"},
        "close_project": {"id": "x"},
        "update_project_section": {"id": "x", "section": "s", "mode": "append", "content": "c"},
        "create_timeline_event": {"title": "t", "valid_from": "2026-01-01"},
    }

    def test_every_non_exempt_translator_argv_includes_json(self):
        checked = 0
        for name, fn in mcp_bridge.TRANSLATORS.items():
            if fn is None or name in self.TEXT_PARSED_EXCEPTIONS:
                continue
            self.assertIn(name, self.MINIMAL_ARGS, f"{name} has no MINIMAL_ARGS fixture — add one")
            argv, _ = fn(self.MINIMAL_ARGS[name])
            self.assertIn("--json", argv, f"{name}'s translator does not request --json: {argv}")
            checked += 1
        # Sanity: this test actually exercised something, not an empty loop.
        self.assertGreaterEqual(checked, 13)

    def test_minimal_args_fixture_covers_every_callable_translator(self):
        callable_names = {n for n, fn in mcp_bridge.TRANSLATORS.items() if fn is not None}
        self.assertEqual(set(self.MINIMAL_ARGS.keys()), callable_names)


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

    def test_get_project_argv(self):
        argv, stdin = mcp_bridge._t_get_project({"id": "project-example"})
        self.assertEqual(argv, ["project", "show", "project-example", "--json"])
        self.assertIsNone(stdin)

    def test_search_timeline_argv_with_query(self):
        argv, _ = mcp_bridge._t_search_timeline({"query": "launch", "limit": 10})
        self.assertEqual(argv, ["timeline", "--json", "--limit", "10", "--query", "launch"])

    def test_search_timeline_argv_no_query_uses_default_limit(self):
        argv, _ = mcp_bridge._t_search_timeline({})
        self.assertEqual(argv, ["timeline", "--json", "--limit", "50"])
        self.assertNotIn("--query", argv)

    def test_update_memory_argv_set_fields_and_append_text(self):
        argv, _ = mcp_bridge._t_update_memory({
            "id": "knowledge-x", "set_fields": {"status": "current"}, "append_text": "more detail",
        })
        self.assertEqual(argv, ["update", "knowledge-x", "--json",
                                 "--set", "status=current", "--append-text", "more detail"])

    def test_create_decision_argv_minimal(self):
        argv, _ = mcp_bridge._t_create_decision({"title": "Use widgets"})
        self.assertEqual(argv, ["decision", "create", "--title", "Use widgets", "--json"])

    def test_create_decision_argv_full(self):
        argv, _ = mcp_bridge._t_create_decision({
            "title": "Use widgets", "status": "decided", "supersedes": "decision-old",
            "people": ["person-a"], "projects": ["project-x"], "tags": ["t1"],
        })
        self.assertIn("--status", argv)
        self.assertEqual(argv[argv.index("--status") + 1], "decided")
        self.assertIn("--supersedes", argv)
        self.assertIn("--people", argv)
        self.assertIn("--projects", argv)
        self.assertIn("--tags", argv)

    def test_create_project_argv(self):
        argv, _ = mcp_bridge._t_create_project({
            "id": "project-x", "name": "Project X", "path": "/tmp/x", "aliases": ["px"],
        })
        self.assertEqual(argv, ["project", "create", "--id", "project-x", "--name", "Project X",
                                 "--path", "/tmp/x", "--json", "--aliases", "px"])

    def test_close_project_argv_with_summary(self):
        argv, _ = mcp_bridge._t_close_project({"id": "project-x", "summary": "done"})
        self.assertEqual(argv, ["project", "close", "project-x", "--json", "--summary", "done"])

    def test_update_project_section_argv(self):
        argv, _ = mcp_bridge._t_update_project_section({
            "id": "project-x", "section": "Next actions", "mode": "append", "content": "do the thing",
        })
        self.assertEqual(argv, ["project", "section-update", "project-x", "--section", "Next actions",
                                 "--mode", "append", "--content", "do the thing", "--json"])

    def test_create_timeline_event_argv(self):
        argv, _ = mcp_bridge._t_create_timeline_event({
            "title": "Launch day", "valid_from": "2026-10-01", "what_happened": "shipped it",
        })
        self.assertEqual(argv, ["timeline", "add", "--title", "Launch day", "--date", "2026-10-01",
                                 "--json", "--what-happened", "shipped it"])


class TestConfirmRestrictedForwarding(unittest.TestCase):
    """v0.10.1: the bridge never decides confirm_restricted on a caller's
    behalf — it only relays whatever the MCP tool call's own argument
    already said, as --confirm-restricted. One positive + one negative
    argv test per translator that accepts confirm_restricted."""

    def test_confirm_restricted_flag_present_when_true(self):
        self.assertEqual(mcp_bridge._confirm_restricted_flag({"confirm_restricted": True}),
                          ["--confirm-restricted"])

    def test_confirm_restricted_flag_absent_when_false(self):
        self.assertEqual(mcp_bridge._confirm_restricted_flag({"confirm_restricted": False}), [])

    def test_confirm_restricted_flag_absent_when_missing(self):
        self.assertEqual(mcp_bridge._confirm_restricted_flag({}), [])

    def test_remember_forwards_confirm_restricted(self):
        argv, _ = mcp_bridge._t_remember({
            "type": "fact", "title": "x", "sensitivity": "restricted", "confirm_restricted": True,
        })
        self.assertIn("--confirm-restricted", argv)

    def test_remember_omits_confirm_restricted_when_not_given(self):
        argv, _ = mcp_bridge._t_remember({"type": "fact", "title": "x", "sensitivity": "restricted"})
        self.assertNotIn("--confirm-restricted", argv)

    def test_create_memory_note_forwards_confirm_restricted(self):
        argv, _ = mcp_bridge._t_create_memory_note({
            "type": "fact", "title": "x", "sensitivity": "restricted", "confirm_restricted": True,
        })
        self.assertIn("--confirm-restricted", argv)

    def test_create_decision_forwards_confirm_restricted(self):
        argv, _ = mcp_bridge._t_create_decision({
            "title": "x", "sensitivity": "restricted", "confirm_restricted": True,
        })
        self.assertIn("--confirm-restricted", argv)

    def test_create_timeline_event_forwards_confirm_restricted(self):
        argv, _ = mcp_bridge._t_create_timeline_event({
            "title": "x", "valid_from": "2026-01-01",
            "sensitivity": "restricted", "confirm_restricted": True,
        })
        self.assertIn("--confirm-restricted", argv)

    def test_update_memory_forwards_confirm_restricted(self):
        argv, _ = mcp_bridge._t_update_memory({
            "id": "x", "set_fields": {"sensitivity": "restricted"}, "confirm_restricted": True,
        })
        self.assertIn("--confirm-restricted", argv)

    def test_update_memory_omits_confirm_restricted_when_not_given(self):
        argv, _ = mcp_bridge._t_update_memory({"id": "x", "set_fields": {"sensitivity": "restricted"}})
        self.assertNotIn("--confirm-restricted", argv)


class TestProjectContext(unittest.TestCase):
    """project_context is not a 1:1 CLI translation — see mcp_bridge.py's
    _execute_project_context docstring. It composes two `brain` calls and
    always returns filesystem_facts: None over the bridge (the server
    cannot see a desktop project's git state)."""

    def test_composes_project_show_and_get(self):
        show_payload = json.dumps({"registry": {"id": "project-x", "path": "/tmp/x"}, "path_exists": True})
        note_text = "---\nid: project-x\n---\nbody"
        calls = []

        def fake_run_brain(argv, stdin_data=None):
            calls.append(argv)
            if argv[:2] == ["project", "show"]:
                return _completed(stdout=show_payload)
            if argv[0] == "get":
                return _completed(stdout=note_text)
            raise AssertionError(f"unexpected argv {argv}")

        with patch.object(mcp_bridge, "_run_brain", side_effect=fake_run_brain):
            result = mcp_bridge._execute_tool("project_context", {"id": "project-x"})

        self.assertEqual(result["registry"], {"id": "project-x", "path": "/tmp/x"})
        self.assertEqual(result["record"], note_text)
        self.assertIsNone(result["filesystem_facts"])
        self.assertEqual(calls, [["project", "show", "project-x", "--json"], ["get", "project-x"]])

    def test_uses_resolved_canonical_id_for_get_when_called_by_alias(self):
        # Regression test for a real bug found live during v0.10.1 Codex
        # acceptance: `project show <alias>` resolves fine (find_project
        # handles aliases), but `get <alias>` does an exact note-id match
        # only — calling `get` with the raw alias 404s even though the
        # project itself was just found. The canonical id from the first
        # call's own registry response must be used for the second.
        show_payload = json.dumps({
            "registry": {"id": "project-sako-brain", "path": "/tmp/x"}, "path_exists": True,
        })
        note_text = "---\nid: project-sako-brain\n---\nbody"
        calls = []

        def fake_run_brain(argv, stdin_data=None):
            calls.append(argv)
            if argv[:2] == ["project", "show"]:
                return _completed(stdout=show_payload)
            if argv[0] == "get":
                return _completed(stdout=note_text)
            raise AssertionError(f"unexpected argv {argv}")

        with patch.object(mcp_bridge, "_run_brain", side_effect=fake_run_brain):
            result = mcp_bridge._execute_tool("project_context", {"id": "sako-brain"})

        self.assertEqual(result["record"], note_text)
        self.assertEqual(calls, [
            ["project", "show", "sako-brain", "--json"],
            ["get", "project-sako-brain"],  # canonical id, NOT the alias "sako-brain"
        ])

    def test_project_show_failure_raises_bridge_error(self):
        with patch.object(mcp_bridge, "_run_brain",
                           return_value=_completed(stderr="no such project", returncode=1)):
            with self.assertRaises(mcp_bridge.BridgeError):
                mcp_bridge._execute_tool("project_context", {"id": "project-x"})


class TestExecuteTool(unittest.TestCase):
    def test_unsupported_tool_raises_clear_error(self):
        with self.assertRaises(mcp_bridge.BridgeError) as ctx:
            mcp_bridge._execute_tool("queue_memory", {})
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


class TestBridgeCannotBypassSharedWritePolicy(unittest.TestCase):
    """Real, non-mocked integration test: the bridge shells out to the ACTUAL
    `brain` CLI (not a mock at the _run_brain boundary, unlike every other
    test in this file) against a real temp vault, proving the bridge has no
    private, unprotected path to Brain data — it is exactly as constrained
    by writepolicy.py as a human typing the same `brain` command would be,
    because it IS that same command. This is the concrete evidence for the
    v0.10.1 fix: before it, this exact test would have PASSED where it now
    fails (remember written straight through with a secret in it)."""

    SECRET = "AKIAABCDEFGHIJKLMNOP"

    def setUp(self):
        import sys
        import tempfile
        from pathlib import Path

        self.repo_root = Path(__file__).resolve().parent.parent
        self._tmp = tempfile.TemporaryDirectory()
        vault_root = Path(self._tmp.name) / "vault"
        state_dir = Path(self._tmp.name) / "state"
        vault_root.mkdir()
        state_dir.mkdir()

        # A minimal real vault the CLI itself can operate on (mirrors what
        # `brain init` produces for the one directory `remember` needs).
        (vault_root / "00_INBOX").mkdir()
        (vault_root / "90_SYSTEM").mkdir()
        # BRAIN_ROOT/BRAIN_STATE_DIR are deliberately stripped by the
        # bridge's own _clean_env() (never let a local-override env var
        # leak into a subprocess call meant for the canonical server) — so
        # this test cannot point the shim at the temp vault that way
        # either, by design. Use the two mechanisms that survive
        # _clean_env instead: --vault baked into the shim's own argv, and
        # state_dir: in the vault's own config.yaml.
        (vault_root / "90_SYSTEM" / "config.yaml").write_text(f"state_dir: {state_dir}\n")

        # A real shim executable: `_run_brain` shells out to whatever
        # BRAIN_MCP_BRIDGE_EXECUTABLE names, so point it at the actual,
        # installed-from-source `brain.cli`, not a mock.
        shim = Path(self._tmp.name) / "brain-shim"
        shim.write_text(
            f"#!/usr/bin/env bash\nexec {sys.executable} -m brain.cli --vault {vault_root} \"$@\"\n"
        )
        shim.chmod(0o755)

        self._env_patch = patch.dict(os.environ, {
            "BRAIN_MCP_BRIDGE_EXECUTABLE": str(shim),
            "PYTHONPATH": str(self.repo_root),
        })
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_remember_with_secret_is_refused_through_the_real_cli(self):
        with self.assertRaises(mcp_bridge.BridgeError) as ctx:
            mcp_bridge._execute_tool("remember", {
                "type": "fact", "title": "ok title", "text": f"api key: {self.SECRET}",
            })
        self.assertIn("possible", str(ctx.exception).lower())
        self.assertNotIn(self.SECRET, str(ctx.exception))

    def test_remember_without_a_secret_succeeds_through_the_real_cli(self):
        result = mcp_bridge._execute_tool("remember", {
            "type": "fact", "title": "ordinary fact", "text": "nothing sensitive here",
        })
        self.assertIn("created_path", result)

    def test_restricted_remember_without_confirmation_is_refused_through_the_real_cli(self):
        # The other half of the v0.10.1 fix: a bridge call that sets
        # sensitivity=restricted but omits confirm_restricted must be
        # refused by the real CLI, exactly as if a human had typed
        # `brain remember --sensitivity restricted` with no
        # --confirm-restricted.
        with self.assertRaises(mcp_bridge.BridgeError) as ctx:
            mcp_bridge._execute_tool("remember", {
                "type": "fact", "title": "ok title", "text": "sensitive but not secret-shaped",
                "sensitivity": "restricted",
            })
        self.assertIn("confirm_restricted", str(ctx.exception))

    def test_restricted_remember_with_confirmation_succeeds_through_the_real_cli(self):
        result = mcp_bridge._execute_tool("remember", {
            "type": "fact", "title": "ok title", "text": "sensitive but not secret-shaped",
            "sensitivity": "restricted", "confirm_restricted": True,
        })
        self.assertIn("created_path", result)

    def test_bridge_cannot_implicitly_grant_confirmation(self):
        """The specific requirement: the bridge must never add confirmation
        implicitly. Calling the tool exactly as a naive/malicious client
        might — sensitivity=restricted, confirm_restricted simply absent
        from the arguments dict rather than explicitly False — must still
        be refused, not silently treated as confirmed."""
        arguments = {"type": "fact", "title": "ok title", "text": "x", "sensitivity": "restricted"}
        self.assertNotIn("confirm_restricted", arguments)
        with self.assertRaises(mcp_bridge.BridgeError) as ctx:
            mcp_bridge._execute_tool("remember", arguments)
        self.assertIn("confirm_restricted", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
