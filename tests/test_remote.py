"""Unit coverage for brain/remote.py — client config, SSH config rendering,
proxy-or-not decision, and read/write identity selection."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain import remote


class TestIsWriteSubcommand(unittest.TestCase):
    def test_project_show_discover_sync_are_read(self):
        for sub in ("show", "discover", "sync"):
            self.assertFalse(remote._is_write_subcommand(["project", sub]), sub)

    def test_project_create_update_close_section_update_are_write(self):
        for sub in ("create", "update", "close", "section-update"):
            self.assertTrue(remote._is_write_subcommand(["project", sub]), sub)

    def test_timeline_add_is_write_bare_timeline_is_not(self):
        self.assertTrue(remote._is_write_subcommand(["timeline", "add"]))
        self.assertFalse(remote._is_write_subcommand(["timeline"]))

    def test_search_get_status_are_read(self):
        for argv in (["search", "x"], ["get", "id"], ["status"], ["capabilities"]):
            self.assertFalse(remote._is_write_subcommand(argv), argv)

    def test_remember_decision_handoff_note_update_are_write(self):
        for top in ("remember", "decision", "handoff", "note", "update", "memory"):
            self.assertTrue(remote._is_write_subcommand([top]), top)

    def test_index_is_write(self):
        """Regression: the server dispatcher's WRITE_ALLOWED includes
        'index' but READ_ALLOWED does not — every `brain index` call
        (which every skill recommends after a write) was being sent under
        the read identity and refused, regardless of which identity was
        actually configured. Found live during v0.11.0 session-close."""
        self.assertTrue(remote._is_write_subcommand(["index"]))

    def test_empty_argv_is_not_write(self):
        self.assertFalse(remote._is_write_subcommand([]))


class TestShouldProxy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = Path(self._tmp.name) / "client.toml"
        self._env_patch = patch.dict(os.environ, {
            remote.CLIENT_CONFIG_ENV: str(self.config_path),
        }, clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        for var in ("BRAIN_ROOT", "BRAIN_LOCAL"):
            os.environ.pop(var, None)

    def _write_remote_config(self):
        self.config_path.write_text(
            'server = "brain.example.invalid"\n'
            'ssh_user = "brain"\n'
            'ssh_port = "22"\n'
            'identity_file = "/tmp/fake-key"\n',
            encoding="utf-8",
        )

    def test_no_config_file_means_local(self):
        self.assertFalse(remote.should_proxy(["search", "x"]))

    def test_config_without_server_means_local(self):
        self.config_path.write_text("profile = \"x\"\n", encoding="utf-8")
        self.assertFalse(remote.should_proxy(["search", "x"]))

    def test_config_with_server_means_proxy(self):
        self._write_remote_config()
        self.assertTrue(remote.should_proxy(["search", "x"]))

    def test_local_only_commands_never_proxy_even_with_remote_configured(self):
        self._write_remote_config()
        for cmd in ("setup", "integration", "init"):
            self.assertFalse(remote.should_proxy([cmd]), cmd)

    def test_explicit_vault_flag_forces_local(self):
        self._write_remote_config()
        self.assertFalse(remote.should_proxy(["search", "x", "--vault", "/tmp/v"]))

    def test_brain_root_env_forces_local(self):
        self._write_remote_config()
        with patch.dict(os.environ, {"BRAIN_ROOT": "/tmp/v"}):
            self.assertFalse(remote.should_proxy(["search", "x"]))

    def test_empty_argv_is_local(self):
        self._write_remote_config()
        self.assertFalse(remote.should_proxy([]))

    def test_incomplete_remote_config_is_a_surfaced_error_not_silent_local(self):
        self.config_path.write_text('server = "x"\n', encoding="utf-8")  # missing identity_file
        self.assertTrue(remote.should_proxy(["search", "x"]))
        with self.assertRaises(remote.RemoteConfigError):
            remote.load_client_config()


class TestRenderSshConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = remote.ClientConfig(
            server="brain.example.invalid", ssh_user="brain", ssh_port="2222",
            identity_file="/srv/u/.ssh/read", write_identity_file="/srv/u/.ssh/write",
        )
        self.text = remote.render_ssh_config(self.cfg, Path("/srv/u/.config/sako-brain/ssh_known_hosts"))

    def test_strict_host_key_checking_is_on(self):
        self.assertIn("StrictHostKeyChecking yes", self.text)
        self.assertNotIn("StrictHostKeyChecking no", self.text)

    def test_no_forwarding(self):
        self.assertIn("ForwardAgent no", self.text)
        self.assertIn("ForwardX11 no", self.text)

    def test_dedicated_known_hosts_no_global(self):
        self.assertIn("UserKnownHostsFile /srv/u/.config/sako-brain/ssh_known_hosts", self.text)
        self.assertIn("GlobalKnownHostsFile /dev/null", self.text)

    def test_two_host_aliases_with_distinct_identities(self):
        self.assertIn("Host sako-brain-read", self.text)
        self.assertIn("Host sako-brain-write", self.text)
        self.assertIn("IdentityFile /srv/u/.ssh/read", self.text)
        self.assertIn("IdentityFile /srv/u/.ssh/write", self.text)

    def test_write_identity_falls_back_to_read_identity_when_unset(self):
        cfg = remote.ClientConfig(server="s", ssh_user="u", ssh_port="22", identity_file="/k")
        text = remote.render_ssh_config(cfg, Path("/kh"))
        self.assertEqual(text.count("IdentityFile /k"), 2)


class TestClientTomlRoundTrip(unittest.TestCase):
    def test_render_then_load_round_trips(self):
        cfg = remote.ClientConfig(
            server="brain.example.invalid", ssh_user="brain", ssh_port="2222",
            identity_file="/srv/u/.ssh/read", write_identity_file="/srv/u/.ssh/write",
            profile="desktop",
        )
        text = remote.render_client_toml(cfg)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.toml"
            path.write_text(text, encoding="utf-8")
            loaded = remote.load_client_config(path)
        self.assertEqual(loaded.server, cfg.server)
        self.assertEqual(loaded.ssh_user, cfg.ssh_user)
        self.assertEqual(loaded.ssh_port, cfg.ssh_port)
        self.assertEqual(loaded.identity_file, cfg.identity_file)
        self.assertEqual(loaded.write_identity_file, cfg.write_identity_file)
        self.assertEqual(loaded.profile, cfg.profile)

    def test_special_characters_in_paths_are_escaped(self):
        cfg = remote.ClientConfig(server="s", ssh_user="u", ssh_port="22",
                                   identity_file='/srv/u/weird"key\\path')
        text = remote.render_client_toml(cfg)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.toml"
            path.write_text(text, encoding="utf-8")
            loaded = remote.load_client_config(path)
        self.assertEqual(loaded.identity_file, cfg.identity_file)


class TestProxyToRemoteDelegation(unittest.TestCase):
    """Stage 2 (multi-user visibility, gateway delegation): proxy_to_remote
    is what the remote gateway's own `brain` client process uses to reach
    canonical Brain — when BRAIN_GATEWAY_ACTING_PRINCIPAL is set (only the
    gateway ever sets it, from an already-validated OAuth token's
    principal_id), the SSH command it sends must carry a
    '--acting-principal <id> -- ' prefix the server-side dispatcher can
    independently re-validate. An ordinary desktop/TRON client never sets
    this env var, so its command is unaffected — unit-tested here as "not
    present means the prefix is absent", the actual trust decision is
    brain-dispatch.py's resolve_effective_principal(), tested in
    sako-brain-tooling's own suite."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = remote.ClientConfig(
            server="brain.example.invalid", ssh_user="brain", ssh_port="2222",
            identity_file="/k-read", write_identity_file="/k-write",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _run_proxy(self, argv, *, acting_principal=None, acting_client_id=None,
                   acting_client_name=None):
        captured = {}

        def fake_execvp(file, ssh_argv):
            captured["file"] = file
            captured["argv"] = ssh_argv

        env_patch = {}
        if acting_principal:
            env_patch[remote.GATEWAY_ACTING_PRINCIPAL_ENV] = acting_principal
        if acting_client_id:
            env_patch[remote.GATEWAY_ACTING_CLIENT_ID_ENV] = acting_client_id
        if acting_client_name:
            env_patch[remote.GATEWAY_ACTING_CLIENT_NAME_ENV] = acting_client_name
        with patch.object(remote, "load_client_config", return_value=self.cfg), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(os, "execvp", fake_execvp), \
             patch.dict(os.environ, env_patch, clear=False):
            if not acting_principal:
                os.environ.pop(remote.GATEWAY_ACTING_PRINCIPAL_ENV, None)
            if not acting_client_id:
                os.environ.pop(remote.GATEWAY_ACTING_CLIENT_ID_ENV, None)
            if not acting_client_name:
                os.environ.pop(remote.GATEWAY_ACTING_CLIENT_NAME_ENV, None)
            remote.proxy_to_remote(argv)
        return captured

    def _remote_command(self, captured) -> str:
        # ssh_argv is ["ssh", "-F", <cfg_path>, <alias>, "--", <remote_command>]
        return captured["argv"][-1]

    def test_no_acting_principal_set_sends_the_plain_command(self):
        captured = self._run_proxy(["status"])
        self.assertEqual(self._remote_command(captured), "status")

    def test_acting_principal_set_prepends_the_delegation_prefix(self):
        captured = self._run_proxy(["context", "widget"], acting_principal="principal-ania")
        self.assertEqual(
            self._remote_command(captured),
            "--acting-principal principal-ania -- context widget",
        )

    def test_delegated_write_subcommand_still_selects_the_write_host_alias(self):
        captured = self._run_proxy(["decision", "create", "--title", "x"],
                                    acting_principal="principal-ania")
        # Delegation changes the asserted principal, never which SSH
        # identity (and therefore which --mode) this process connects as.
        self.assertIn(f"{remote.SLUG}-write", captured["argv"])

    def test_acting_client_id_and_name_are_appended_to_the_delegation_prefix(self):
        captured = self._run_proxy(
            ["context", "widget"], acting_principal="principal-ania",
            acting_client_id="oauth-client-hermes", acting_client_name="Hermes Desktop (Marcin)")
        self.assertEqual(
            self._remote_command(captured),
            "--acting-principal principal-ania --acting-client-id oauth-client-hermes "
            "--acting-client-name 'Hermes Desktop (Marcin)' -- context widget",
        )

    def test_acting_client_id_without_name_omits_the_name_flag(self):
        captured = self._run_proxy(
            ["status"], acting_principal="principal-ania", acting_client_id="oauth-client-hermes")
        self.assertEqual(
            self._remote_command(captured),
            "--acting-principal principal-ania --acting-client-id oauth-client-hermes -- status",
        )

    def test_client_id_env_without_acting_principal_is_never_sent(self):
        # GATEWAY_ACTING_CLIENT_ID_ENV alone (no principal) should never
        # happen in practice — mcp_bridge.acting_as() always sets both
        # from the same validated token — but if it somehow did, the
        # plain command must still be sent, never a dangling client-id
        # prefix with no principal.
        captured = self._run_proxy(["status"], acting_client_id="oauth-client-orphan")
        self.assertEqual(self._remote_command(captured), "status")


if __name__ == "__main__":
    unittest.main()
