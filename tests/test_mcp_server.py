import unittest

from brain import mcp_server
from tests.helpers import TempVault


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
                          "get_project_path", "search_timeline"):
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


if __name__ == "__main__":
    unittest.main()
