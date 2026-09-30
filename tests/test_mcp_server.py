import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli, mcp_server
from tests.helpers import TempVault


def _run_cli(argv, vault):
    out = io.StringIO()
    env = dict(os.environ)
    env["BRAIN_ROOT"] = str(vault.root)
    env["BRAIN_STATE_DIR"] = str(vault.state_dir)
    with patch.dict(os.environ, env, clear=False):
        with contextlib.redirect_stdout(out):
            cli.main(argv)
    return out.getvalue()


def _call(config, name, arguments):
    return mcp_server.handle_request(config, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


class TestMcpToolsList(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_tools_list_exposes_expected_read_tools(self):
        resp = mcp_server.handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        for expected in ("search_memory", "read_memory", "list_projects", "get_project",
                          "search_timeline"):
            self.assertIn(expected, names)


class TestMcpWriteSafety(unittest.TestCase):
    """MCP writes must never bypass the same secret-detection and
    restricted-sensitivity policy the CLI enforces."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_remember_refuses_on_secret_pattern_in_text(self):
        resp = _call(self.config, "remember", {
            "type": "fact", "title": "test", "source": "test",
            "text": "api_key: sk-abcdefghijklmnopqrstuvwx",
        })
        self.assertIn("error", resp)
        self.assertIn("possible", resp["error"]["message"].lower())
        # Nothing should have been written.
        inbox = self.config.brain_root / "00_INBOX"
        self.assertEqual(list(inbox.glob("*.md")), [])

    def test_remember_refuses_on_secret_pattern_in_title(self):
        resp = _call(self.config, "remember", {
            "type": "fact", "title": "password: hunter2hunter2", "source": "test",
        })
        self.assertIn("error", resp)

    def test_remember_restricted_without_confirmation_refused(self):
        resp = _call(self.config, "remember", {
            "type": "fact", "title": "sensitive fact", "text": "details",
            "sensitivity": "restricted", "source": "test",
        })
        self.assertIn("error", resp)
        self.assertIn("confirm_restricted", resp["error"]["message"])
        inbox = self.config.brain_root / "00_INBOX"
        self.assertEqual(list(inbox.glob("*.md")), [])

    def test_remember_restricted_with_confirmation_succeeds(self):
        resp = _call(self.config, "remember", {
            "type": "fact", "title": "sensitive fact", "text": "details",
            "sensitivity": "restricted", "confirm_restricted": True, "source": "test",
        })
        self.assertIn("result", resp)
        inbox = self.config.brain_root / "00_INBOX"
        self.assertEqual(len(list(inbox.glob("*.md"))), 1)

    def test_remember_normal_sensitivity_succeeds_without_confirmation(self):
        resp = _call(self.config, "remember", {
            "type": "fact", "title": "ordinary fact", "text": "nothing sensitive",
            "source": "test",
        })
        self.assertIn("result", resp)

    def test_update_memory_refuses_secret_in_append_text(self):
        # Seed a note to update.
        _call(self.config, "remember", {"type": "fact", "title": "base note", "source": "test"})
        resp = _call(self.config, "update_memory", {
            "id": "fact-base-note",
            "append_text": "AKIAABCDEFGHIJKLMNOP is the key",
        })
        self.assertIn("error", resp)

    def test_update_memory_restricted_set_field_requires_confirmation(self):
        _call(self.config, "remember", {"type": "fact", "title": "base note two", "source": "test"})
        resp = _call(self.config, "update_memory", {
            "id": "fact-base-note-two",
            "set_fields": {"sensitivity": "restricted"},
        })
        self.assertIn("error", resp)
        self.assertIn("confirm_restricted", resp["error"]["message"])

    def test_queue_memory_refuses_secret_pattern(self):
        resp = _call(self.config, "queue_memory", {
            "candidate_fact": "my password: hunter2hunter2plaintext",
        })
        self.assertIn("error", resp)

    def test_queue_memory_writes_to_pending_not_authoritative(self):
        resp = _call(self.config, "queue_memory", {
            "candidate_fact": "Alex mentioned buying a new car",
            "source": "conversation",
        })
        self.assertIn("result", resp)
        # Queueing must not create an authoritative note directly.
        inbox_md = list((self.config.brain_root / "00_INBOX").glob("*.md"))
        self.assertEqual(inbox_md, [])
        queue_file = self.config.brain_root / "00_INBOX" / "memory" / "pending.yaml"
        self.assertTrue(queue_file.exists())


class TestGetContextTool(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_get_context_tool_returns_structured_result(self):
        from brain import indexer
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-widget", type="knowledge",
                               title="Widget notes", body="All about widgets.")
        indexer.rebuild(self.config)

        resp = _call(self.config, "get_context", {"query": "widget"})
        self.assertIn("result", resp)
        import json
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("notes", payload)
        self.assertIn("restricted_omitted", payload)


class TestMcpLoggingRedaction(unittest.TestCase):
    """Observability requirement: log request type/timestamp/client/outcome,
    never query text, note bodies, or restricted content."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def _log_content(self) -> str:
        log_files = list(self.config.logs_dir.glob("mcp-*.log"))
        self.assertEqual(len(log_files), 1)
        return log_files[0].read_text()

    def test_successful_call_logged_with_tool_and_client(self):
        mcp_server.handle_request(self.config, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "redaction-test-client"}},
        })
        _call(self.config, "list_projects", {})

        content = self._log_content()
        self.assertIn("tool=list_projects", content)
        self.assertIn("client=redaction-test-client", content)
        self.assertIn("OK", content)

    def test_secret_bearing_write_refusal_does_not_log_the_secret(self):
        secret = "sk-abcdefghijklmnopqrstuvwx"
        _call(self.config, "remember", {
            "type": "fact", "title": "test", "source": "test",
            "text": f"api_key: {secret}",
        })
        content = self._log_content()
        self.assertNotIn(secret, content)
        self.assertIn("FAILED", content)

    def test_restricted_write_logs_no_note_body(self):
        secret_body = "very sensitive restricted medical detail xyz123"
        _call(self.config, "remember", {
            "type": "fact", "title": "restricted note", "text": secret_body,
            "sensitivity": "restricted", "confirm_restricted": True, "source": "test",
        })
        content = self._log_content()
        self.assertNotIn(secret_body, content)
        self.assertIn("tool=remember", content)
        self.assertIn("OK", content)

    def test_query_text_never_logged(self):
        from brain import indexer
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        indexer.rebuild(self.config)
        distinctive_query = "zzz_very_distinctive_query_text_zzz"
        _call(self.config, "search_memory", {"query": distinctive_query})
        content = self._log_content()
        self.assertNotIn(distinctive_query, content)
        self.assertIn("tool=search_memory", content)


class TestClientPermissions(unittest.TestCase):
    """Local security: MCP log files must inherit restrictive permissions
    consistent with the rest of the (Phase 4.2-tightened) vault."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_log_file_not_world_or_group_readable(self):
        import stat
        _call(self.config, "list_projects", {})
        log_files = list(self.config.logs_dir.glob("mcp-*.log"))
        self.assertEqual(len(log_files), 1)
        mode = log_files[0].stat().st_mode
        self.assertEqual(mode & stat.S_IRWXG, 0, "log file should not be group-accessible")
        self.assertEqual(mode & stat.S_IRWXO, 0, "log file should not be other-accessible")


class TestMcpGetOperationalState(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-alpha\n"
            "    name: Alpha\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_is_listed_in_tools(self):
        resp = mcp_server.handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("get_operational_state", names)

    def test_returns_expected_shape(self):
        resp = _call(self.config, "get_operational_state", {})
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("projects", payload)
        self.assertIn("decisions", payload)
        self.assertEqual(payload["projects"]["by_status"], {"active": 1})

    def test_matches_cli_json_output_for_the_same_vault(self):
        """One function, two callers — the invariant the design insists on."""
        cli_out = _run_cli(["state", "--json"], self.vault)
        cli_payload = json.loads(cli_out)

        mcp_resp = _call(self.config, "get_operational_state", {})
        mcp_payload = json.loads(mcp_resp["result"]["content"][0]["text"])

        cli_payload.pop("generated_at")
        mcp_payload.pop("generated_at")
        self.assertEqual(cli_payload, mcp_payload)


class TestMcpWriteHandoff(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-alpha\n"
            "    name: Alpha\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_is_listed_in_tools(self):
        resp = mcp_server.handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("write_handoff", names)

    def test_writes_a_handoff_matching_cli_shape(self):
        resp = _call(self.config, "write_handoff", {
            "project_id": "project-alpha", "attempted": "did a thing",
            "changed": "changed a thing", "files_changed": ["a.py"],
        })
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("updated_path", payload)
        from brain import handoff as handoff_mod
        self.assertTrue(handoff_mod.has_handoff(self.config, "project-alpha"))

    def test_empty_payload_refused(self):
        resp = _call(self.config, "write_handoff", {"project_id": "project-alpha"})
        self.assertIn("error", resp)

    def test_secret_bearing_text_refused(self):
        resp = _call(self.config, "write_handoff", {
            "project_id": "project-alpha", "attempted": "api_key: sk-abcdefghijklmnopqrstuvwx",
        })
        self.assertIn("error", resp)
        from brain import handoff as handoff_mod
        self.assertFalse(handoff_mod.has_handoff(self.config, "project-alpha"))

    def test_source_defaults_to_connected_client_not_a_hardcoded_value(self):
        mcp_server.handle_request(self.config, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "codex-cli"}},
        })
        _call(self.config, "write_handoff", {
            "project_id": "project-alpha", "attempted": "did a thing",
        })
        from brain import handoff as handoff_mod
        path = handoff_mod.handoff_path(self.config, "project-alpha")
        self.assertIn("source: codex-cli", path.read_text())

    def test_explicit_source_argument_overrides_connected_client(self):
        mcp_server.handle_request(self.config, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"clientInfo": {"name": "codex-cli"}},
        })
        _call(self.config, "write_handoff", {
            "project_id": "project-alpha", "attempted": "did a thing", "source": "explicit-override",
        })
        from brain import handoff as handoff_mod
        path = handoff_mod.handoff_path(self.config, "project-alpha")
        self.assertIn("source: explicit-override", path.read_text())


class TestMcpCreateDecision(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_is_listed_in_tools(self):
        resp = mcp_server.handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("create_decision", names)

    def test_creates_decision(self):
        resp = _call(self.config, "create_decision", {"title": "Use SQLite", "decision": "Use SQLite FTS5."})
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("created_path", payload)

    def test_secret_in_context_refused(self):
        resp = _call(self.config, "create_decision", {
            "title": "X", "context": "api_key: sk-abcdefghijklmnopqrstuvwx",
        })
        self.assertIn("error", resp)

    def test_restricted_without_confirmation_refused(self):
        resp = _call(self.config, "create_decision", {"title": "X", "sensitivity": "restricted"})
        self.assertIn("error", resp)
        self.assertIn("confirm_restricted", resp["error"]["message"])


class TestMcpProjectWriteTools(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_all_listed_in_tools(self):
        resp = mcp_server.handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        for expected in ("create_project", "update_project_status", "close_project"):
            self.assertIn(expected, names)

    def test_create_project(self):
        resp = _call(self.config, "create_project", {
            "id": "project-widget", "name": "Widget", "path": "/tmp/example-widget",
        })
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("created_path", payload)

    def test_update_project_status_moves_and_syncs(self):
        _call(self.config, "create_project", {"id": "project-widget", "name": "Widget", "path": "/a"})
        resp = _call(self.config, "update_project_status", {"id": "project-widget", "status": "on-hold"})
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["new_status"], "on-hold")
        self.assertTrue(payload["moved"])
        self.assertTrue(payload["registry_updated"])

    def test_close_project(self):
        _call(self.config, "create_project", {"id": "project-widget", "name": "Widget", "path": "/a"})
        resp = _call(self.config, "close_project", {"id": "project-widget", "summary": "Shipped."})
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["new_status"], "archived")

    def test_close_project_secret_in_summary_refused(self):
        _call(self.config, "create_project", {"id": "project-widget", "name": "Widget", "path": "/a"})
        resp = _call(self.config, "close_project", {
            "id": "project-widget", "summary": "api_key: sk-abcdefghijklmnopqrstuvwx",
        })
        self.assertIn("error", resp)


class TestMcpUpdateProjectSection(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        _call(self.config, "create_project", {"id": "project-widget", "name": "Widget", "path": "/a"})

    def tearDown(self):
        self.vault.cleanup()

    def test_is_listed_in_tools(self):
        resp = mcp_server.handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("update_project_section", names)

    def test_replace_section(self):
        resp = _call(self.config, "update_project_section", {
            "id": "project-widget", "section": "Current state", "mode": "replace", "content": "Good.",
        })
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("updated_path", payload)

    def test_unknown_section_refused(self):
        resp = _call(self.config, "update_project_section", {
            "id": "project-widget", "section": "Purpose", "mode": "replace", "content": "x",
        })
        self.assertIn("error", resp)

    def test_secret_in_content_refused(self):
        resp = _call(self.config, "update_project_section", {
            "id": "project-widget", "section": "Current state", "mode": "replace",
            "content": "api_key: sk-abcdefghijklmnopqrstuvwx",
        })
        self.assertIn("error", resp)

    def test_stale_if_match_refused(self):
        resp = _call(self.config, "update_project_section", {
            "id": "project-widget", "section": "Current state", "mode": "replace",
            "content": "x", "if_match": "0" * 64,
        })
        self.assertIn("error", resp)


class TestMcpCreateMemoryNote(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_is_listed_in_tools(self):
        resp = mcp_server.handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("create_memory_note", names)

    def test_creates_person_note(self):
        resp = _call(self.config, "create_memory_note", {"type": "person", "title": "Jane Doe"})
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("created_path", payload)

    def test_fact_without_area_refused(self):
        resp = _call(self.config, "create_memory_note", {"type": "fact", "title": "X"})
        self.assertIn("error", resp)

    def test_secret_in_text_refused(self):
        resp = _call(self.config, "create_memory_note", {
            "type": "knowledge", "title": "X", "text": "api_key: sk-abcdefghijklmnopqrstuvwx",
        })
        self.assertIn("error", resp)

    def test_restricted_without_confirmation_refused(self):
        resp = _call(self.config, "create_memory_note", {
            "type": "person", "title": "X", "sensitivity": "restricted",
        })
        self.assertIn("error", resp)
        self.assertIn("confirm_restricted", resp["error"]["message"])


class TestMcpCreateTimelineEvent(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_is_listed_in_tools(self):
        resp = mcp_server.handle_request(self.config, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("create_timeline_event", names)

    def test_creates_event(self):
        resp = _call(self.config, "create_timeline_event", {
            "title": "Something happened", "valid_from": "2026-09-26",
        })
        self.assertNotIn("error", resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("created_path", payload)

    def test_secret_in_what_happened_refused(self):
        resp = _call(self.config, "create_timeline_event", {
            "title": "X", "valid_from": "2026-09-26",
            "what_happened": "api_key: sk-abcdefghijklmnopqrstuvwx",
        })
        self.assertIn("error", resp)


if __name__ == "__main__":
    unittest.main()
