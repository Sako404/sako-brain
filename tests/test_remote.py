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


if __name__ == "__main__":
    unittest.main()
