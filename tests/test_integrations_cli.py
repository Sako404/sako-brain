"""Unit coverage for brain/integrations_cli.py — the Claude Code / Codex
installer and doctor. Every install/uninstall test asserts merge-safety:
content this installer does not own must survive byte-for-byte."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain import integrations_cli as ic


class TestShippedSkillNames(unittest.TestCase):
    def test_at_least_fifteen_skills_shipped(self):
        self.assertGreaterEqual(len(ic.shipped_skill_names()), 15)

    def test_names_are_sorted_unique(self):
        names = ic.shipped_skill_names()
        self.assertEqual(names, sorted(set(names)))


class TestClaudeCodeInstall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.skills_dir = Path(self._tmp.name) / "skills"
        self.mcp_config = Path(self._tmp.name) / "claude.json"

    def test_install_creates_all_symlinks_and_mcp_entry(self):
        result = ic.install_claude_code(self.skills_dir, self.mcp_config)
        expected = set(ic.shipped_skill_names())
        self.assertEqual(set(result["skills_installed"]), expected)
        self.assertEqual(result["skills_skipped"], [])
        for name in expected:
            self.assertTrue((self.skills_dir / name).is_symlink())
        data = json.loads(self.mcp_config.read_text())
        self.assertIn("sako-brain", data["mcpServers"])
        self.assertEqual(result["mcp_config"]["action"], "added")

    def test_install_is_idempotent(self):
        ic.install_claude_code(self.skills_dir, self.mcp_config)
        result2 = ic.install_claude_code(self.skills_dir, self.mcp_config)
        self.assertEqual(set(result2["skills_installed"]), set(ic.shipped_skill_names()))
        self.assertEqual(result2["skills_skipped"], [])

    def test_install_preserves_unrelated_mcp_servers(self):
        self.mcp_config.write_text(json.dumps({
            "mcpServers": {"other-tool": {"type": "http", "url": "https://x", "headers": {"Authorization": "secret"}}},
            "unrelatedTopLevelKey": {"nested": True},
        }), encoding="utf-8")
        ic.install_claude_code(self.skills_dir, self.mcp_config)
        data = json.loads(self.mcp_config.read_text())
        self.assertEqual(data["mcpServers"]["other-tool"]["headers"]["Authorization"], "secret")
        self.assertTrue(data["unrelatedTopLevelKey"]["nested"])
        self.assertIn("sako-brain", data["mcpServers"])

    def test_install_does_not_clobber_a_foreign_skill_without_force(self):
        self.skills_dir.mkdir(parents=True)
        name = ic.shipped_skill_names()[0]
        (self.skills_dir / name).mkdir()
        (self.skills_dir / name / "SKILL.md").write_text("mine, not Brain's", encoding="utf-8")

        result = ic.install_claude_code(self.skills_dir, self.mcp_config)
        skipped_names = {s["skill"] for s in result["skills_skipped"]}
        self.assertIn(name, skipped_names)
        self.assertEqual(
            (self.skills_dir / name / "SKILL.md").read_text(encoding="utf-8"),
            "mine, not Brain's",
        )

    def test_force_overwrites_a_foreign_skill(self):
        self.skills_dir.mkdir(parents=True)
        name = ic.shipped_skill_names()[0]
        (self.skills_dir / name).mkdir()
        (self.skills_dir / name / "SKILL.md").write_text("mine, not Brain's", encoding="utf-8")

        result = ic.install_claude_code(self.skills_dir, self.mcp_config, force=True)
        self.assertIn(name, result["skills_installed"])
        self.assertTrue((self.skills_dir / name).is_symlink())

    def test_uninstall_removes_only_what_it_installed(self):
        self.skills_dir.mkdir(parents=True)
        foreign = self.skills_dir / "my-own-skill"
        foreign.mkdir()
        (foreign / "SKILL.md").write_text("mine", encoding="utf-8")

        ic.install_claude_code(self.skills_dir, self.mcp_config)
        result = ic.uninstall_claude_code(self.skills_dir, self.mcp_config)

        self.assertEqual(set(result["skills_removed"]), set(ic.shipped_skill_names()))
        self.assertTrue(foreign.is_dir())  # untouched
        for name in ic.shipped_skill_names():
            self.assertFalse((self.skills_dir / name).exists())
        data = json.loads(self.mcp_config.read_text())
        self.assertNotIn("sako-brain", data.get("mcpServers", {}))


class TestCodexInstall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_toml = Path(self._tmp.name) / "config.toml"
        self.agents_md = Path(self._tmp.name) / "AGENTS.md"

    def test_install_from_scratch(self):
        result = ic.install_codex(self.config_toml, self.agents_md)
        self.assertEqual(result["config_toml"]["action"], "created")
        self.assertEqual(result["agents_md"]["action"], "created")
        self.assertIn("[mcp_servers.sako-brain]", self.config_toml.read_text())
        self.assertIn(ic.MARKER_BEGIN, self.agents_md.read_text())

    def test_install_preserves_other_toml_tables(self):
        self.config_toml.write_text(
            '[some_setting]\nfoo = "bar"\n\n'
            '[mcp_servers.other-tool]\ncommand = "x"\n\n'
            '[mcp_servers.yet-another]\ncommand = "y"\n',
            encoding="utf-8",
        )
        ic.install_codex(self.config_toml, self.agents_md)
        text = self.config_toml.read_text()
        self.assertIn('[some_setting]', text)
        self.assertIn('foo = "bar"', text)
        self.assertIn('[mcp_servers.other-tool]', text)
        self.assertIn('[mcp_servers.yet-another]', text)
        self.assertIn('[mcp_servers.sako-brain]', text)

    def test_reinstall_replaces_only_its_own_table(self):
        ic.install_codex(self.config_toml, self.agents_md)
        self.config_toml.write_text(
            self.config_toml.read_text() + '\n[mcp_servers.added-later]\ncommand = "z"\n',
            encoding="utf-8",
        )
        result = ic.install_codex(self.config_toml, self.agents_md)
        self.assertEqual(result["config_toml"]["action"], "replaced")
        text = self.config_toml.read_text()
        self.assertIn('[mcp_servers.added-later]', text)
        self.assertEqual(text.count("[mcp_servers.sako-brain]"), 1)

    def test_install_preserves_surrounding_agents_md_content(self):
        self.agents_md.write_text("# My own instructions\n\nDo the thing.\n", encoding="utf-8")
        ic.install_codex(self.config_toml, self.agents_md)
        text = self.agents_md.read_text()
        self.assertIn("# My own instructions", text)
        self.assertIn("Do the thing.", text)
        self.assertIn(ic.MARKER_BEGIN, text)

    def test_reinstall_replaces_only_marked_section(self):
        self.agents_md.write_text(
            "# Before\n\n" + ic.MARKER_BEGIN + "\nstale content\n" + ic.MARKER_END + "\n\n# After\n",
            encoding="utf-8",
        )
        ic.install_codex(self.config_toml, self.agents_md)
        text = self.agents_md.read_text()
        self.assertIn("# Before", text)
        self.assertIn("# After", text)
        self.assertNotIn("stale content", text)
        self.assertEqual(text.count(ic.MARKER_BEGIN), 1)

    def test_uninstall_removes_both_additions_only(self):
        self.config_toml.write_text('[some_setting]\nfoo = "bar"\n', encoding="utf-8")
        self.agents_md.write_text("# Mine\n", encoding="utf-8")
        ic.install_codex(self.config_toml, self.agents_md)
        ic.uninstall_codex(self.config_toml, self.agents_md)
        self.assertNotIn("sako-brain", self.config_toml.read_text())
        self.assertIn('[some_setting]', self.config_toml.read_text())
        self.assertNotIn(ic.MARKER_BEGIN, self.agents_md.read_text())
        self.assertIn("# Mine", self.agents_md.read_text())


class TestDoctorNeverPrintsSecrets(unittest.TestCase):
    """Hermetic: the real `brain state`/MCP-bridge subprocess calls are
    mocked out so this never depends on network or an actual configured
    client — the concern under test is report *shape*, not connectivity."""

    def test_doctor_report_is_json_serializable_and_has_no_obvious_secret_keys(self):
        from unittest.mock import patch, MagicMock

        fake_state_proc = MagicMock(returncode=0, stdout='{"brain_version": "0.11.0"}', stderr="")
        fake_bridge_proc = MagicMock(
            returncode=0,
            stdout='{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"search_memory"}]}}\n',
            stderr="",
        )
        with patch.object(ic, "_run_brain_capture", return_value=fake_state_proc), \
             patch("subprocess.run", return_value=fake_bridge_proc):
            report = ic.run_doctor()

        dumped = json.dumps(report)
        for bad in ("identity_file", "private", "BEGIN OPENSSH", "BEGIN RSA"):
            self.assertNotIn(bad, dumped)
        self.assertIn("checks", report)
        self.assertTrue(any(c["name"] == "canonical_brain_connectivity" for c in report["checks"]))


class TestProjectPathsPresent(unittest.TestCase):
    """The canonical server cannot see project paths, so the client checks
    the live ones (project_paths_present); finished projects may be gone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.present = Path(self.tmp.name) / "present"
        self.present.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, *entries):
        return {"projects": {"entries": list(entries)}}

    def test_ok_when_every_live_path_exists(self):
        check = ic._check_project_paths_present(self._state(
            {"id": "project-a", "path": str(self.present), "status": "active"},
            {"id": "project-old", "path": "/nonexistent/old", "status": "archived"},
            {"id": "project-done", "path": "/nonexistent/done", "status": "completed"},
            {"id": "project-nopath", "path": "", "status": "active"},
        ))
        self.assertTrue(check["ok"])
        self.assertIn("1 active/planned/on-hold", check["detail"])

    def test_fails_naming_each_missing_live_path(self):
        check = ic._check_project_paths_present(self._state(
            {"id": "project-a", "path": str(self.present), "status": "active"},
            {"id": "project-gone", "path": "/nonexistent/gone", "status": "on-hold"},
        ))
        self.assertFalse(check["ok"])
        self.assertIn("1 of 2", check["detail"])
        self.assertIn("project-gone: /nonexistent/gone", check["detail"])

    def test_run_doctor_checks_paths_only_with_a_remote_configured(self):
        from unittest.mock import patch, MagicMock

        state = json.dumps(self._state({"id": "project-gone", "path": "/nonexistent/gone", "status": "active"}))
        fake_state_proc = MagicMock(returncode=0, stdout=state, stderr="")
        fake_bridge_proc = MagicMock(returncode=0, stdout="", stderr="")
        fake_cfg = MagicMock(ssh_user="u", server="s", ssh_port="22")
        for cfg, expected in ((fake_cfg, [False]), (None, [])):
            with patch.object(ic.remote, "load_client_config", return_value=cfg), \
                 patch.object(ic, "_run_brain_capture", return_value=fake_state_proc), \
                 patch("subprocess.run", return_value=fake_bridge_proc):
                report = ic.run_doctor()
            got = [c["ok"] for c in report["checks"] if c["name"] == "project_paths_present"]
            self.assertEqual(got, expected)



class TestStaleSkillLinks(unittest.TestCase):
    """A skill link left on an old venv after an upgrade keeps serving that
    version's instructions; doctor must see it (claude_skills_current)."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.skills = self.root / "skills"

    def tearDown(self):
        self._tmp.cleanup()

    def test_fresh_install_has_no_stale_links(self):
        ic.install_claude_code(skills_dir=self.skills, mcp_config_path=self.root / "mcp.json")
        self.assertEqual(ic.stale_skill_links(self.skills), [])

    def test_link_to_an_old_copy_is_reported(self):
        ic.install_claude_code(skills_dir=self.skills, mcp_config_path=self.root / "mcp.json")
        old = self.root / "old-venv" / "timeline"
        old.mkdir(parents=True)
        (self.skills / "timeline").unlink()
        (self.skills / "timeline").symlink_to(old)
        self.assertEqual(ic.stale_skill_links(self.skills), [{"skill": "timeline", "target": str(old)}])

    def test_force_reinstall_clears_stale_links(self):
        ic.install_claude_code(skills_dir=self.skills, mcp_config_path=self.root / "mcp.json")
        old = self.root / "old-venv" / "timeline"
        old.mkdir(parents=True)
        (self.skills / "timeline").unlink()
        (self.skills / "timeline").symlink_to(old)
        ic.install_claude_code(skills_dir=self.skills, mcp_config_path=self.root / "mcp.json", force=True)
        self.assertEqual(ic.stale_skill_links(self.skills), [])

    def test_foreign_non_symlink_is_not_this_checks_business(self):
        self.skills.mkdir()
        (self.skills / "timeline").mkdir()
        self.assertEqual(ic.stale_skill_links(self.skills), [])


if __name__ == "__main__":
    unittest.main()
