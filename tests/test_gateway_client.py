"""BrainGatewayClient tests — these spawn the real MCP server as a real
subprocess and talk real stdio JSON-RPC to it (no mocking of the
transport), against a disposable TempVault via BRAIN_ROOT."""
import unittest

from brain import indexer
from brain.gateway_client import BrainGatewayClient, BrainGatewayError
from tests.helpers import TempVault


class TestGatewayClientTransport(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_client_spawns_dedicated_subprocess_no_listening_socket(self):
        """Transport safety: confirm this is a plain subprocess with pipes,
        not anything that binds a socket or port."""
        with BrainGatewayClient(client_name="transport-test", brain_root=self.vault.root, state_dir=self.vault.state_dir) as brain:
            self.assertIsNone(brain._proc.poll(), "server process should be alive")
            # A stdio subprocess has stdin/stdout pipes, not a socket/port.
            self.assertTrue(hasattr(brain._proc, "stdin"))
            self.assertTrue(hasattr(brain._proc, "stdout"))
        # After close(), the process should have exited.
        self.assertIsNotNone(brain._proc.poll())

    def test_each_client_gets_its_own_process(self):
        with BrainGatewayClient(client_name="a", brain_root=self.vault.root, state_dir=self.vault.state_dir) as a:
            with BrainGatewayClient(client_name="b", brain_root=self.vault.root, state_dir=self.vault.state_dir) as b:
                self.assertNotEqual(a._proc.pid, b._proc.pid)

    def test_close_is_idempotent(self):
        brain = BrainGatewayClient(client_name="idempotent-test", brain_root=self.vault.root, state_dir=self.vault.state_dir)
        brain.close()
        brain.close()  # must not raise


class TestGatewayClientOperations(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.brain = BrainGatewayClient(client_name="ops-test", brain_root=self.vault.root, state_dir=self.vault.state_dir)

    def tearDown(self):
        self.brain.close()
        self.vault.cleanup()

    def test_search_returns_list(self):
        # search_memory reads the SQLite index, which (same as the real
        # `brain remember` -> `brain index` workflow) needs an explicit
        # rebuild after a write — the gateway client doesn't do this
        # implicitly, same as the CLI doesn't.
        self.brain.remember(type="fact", title="Gateway test fact", text="hello", source="test")
        indexer.rebuild(self.vault.config())
        results = self.brain.search("Gateway test fact")
        self.assertTrue(any("gateway-test-fact" in r["id"] for r in results))

    def test_get_context_returns_structured_dict(self):
        self.brain.remember(type="fact", title="Context test fact", text="hello there", source="test")
        indexer.rebuild(self.vault.config())
        ctx = self.brain.get_context("Context test fact")
        self.assertIn("notes", ctx)
        self.assertIn("restricted_omitted", ctx)

    def test_list_projects_empty_registry(self):
        self.assertEqual(self.brain.list_projects(), [])

    def test_queue_memory_does_not_create_authoritative_note(self):
        result = self.brain.queue_memory("Some candidate fact", source="test")
        self.assertIn("queued_id", result)
        self.assertEqual(result["status"], "pending")
        inbox_md = list((self.vault.root / "00_INBOX").glob("*.md"))
        self.assertEqual(inbox_md, [])

    def test_search_timeline_returns_list(self):
        self.assertEqual(self.brain.search_timeline("nonexistent"), [])


class TestGatewayClientWriteSafety(unittest.TestCase):
    """The gateway client must not be able to bypass any Phase 5A write
    policy — it's just a thinner way to call the same guarded tools."""

    def setUp(self):
        self.vault = TempVault()
        self.brain = BrainGatewayClient(client_name="safety-test", brain_root=self.vault.root, state_dir=self.vault.state_dir)

    def tearDown(self):
        self.brain.close()
        self.vault.cleanup()

    def test_remember_refuses_secret_pattern(self):
        with self.assertRaises(BrainGatewayError):
            self.brain.remember(type="fact", title="test", text="api_key: sk-abcdefghijklmnopqrstuvwx", source="test")
        inbox_md = list((self.vault.root / "00_INBOX").glob("*.md"))
        self.assertEqual(inbox_md, [])

    def test_remember_restricted_without_confirmation_refused(self):
        with self.assertRaises(BrainGatewayError) as ctx:
            self.brain.remember(type="fact", title="sensitive", text="x", sensitivity="restricted", source="test")
        self.assertIn("confirm_restricted", str(ctx.exception))

    def test_remember_restricted_with_confirmation_succeeds(self):
        result = self.brain.remember(type="fact", title="sensitive ok", text="x",
                                      sensitivity="restricted", confirm_restricted=True, source="test")
        self.assertIn("created_path", result)

    def test_queue_memory_refuses_secret_pattern(self):
        with self.assertRaises(BrainGatewayError):
            self.brain.queue_memory("my password: hunter2plaintext123")


if __name__ == "__main__":
    unittest.main()
