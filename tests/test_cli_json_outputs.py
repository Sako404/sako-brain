"""`--json` added to `search`/`remember`/`note create`/`project update` —
the minimum needed for a stable, machine-readable CLI contract (the
Interactive Brain V1 MCP bridge shells out to these exact commands rather
than duplicating their business logic, so the JSON shape must be stable
and must match what the equivalent MCP tool already returns).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli, indexer
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


class TestSearchJson(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.vault.write_note(
            "60_KNOWLEDGE", "widget.md", id="knowledge-widget", type="knowledge",
            title="Widget notes", body="The widget is a small mechanical part.",
        )
        indexer.rebuild(self.vault.config())

    def tearDown(self):
        self.vault.cleanup()

    def test_json_flag_emits_results_matching_the_mcp_tool_shape(self):
        rc, out = run_cli(["search", "widget", "--json"], self.vault)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertIn("results", payload)
        r = payload["results"][0]
        for key in ("id", "type", "status", "title", "path", "snippet"):
            self.assertIn(key, r)
        self.assertEqual(r["id"], "knowledge-widget")

    def test_no_matches_still_emits_valid_json(self):
        rc, out = run_cli(["search", "nonexistent-zzz", "--json"], self.vault)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"results": []})

    def test_without_json_flag_output_is_unchanged_human_text(self):
        _, out = run_cli(["search", "widget"], self.vault)
        self.assertIn("knowledge-widget", out)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


class TestRememberJson(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_json_flag_emits_created_path_matching_the_mcp_tool_shape(self):
        rc, out = run_cli(
            ["remember", "--type", "fact", "--title", "test fact", "--json"], self.vault,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(set(payload.keys()), {"created_path"})
        self.assertTrue(payload["created_path"])


class TestNoteCreateJson(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_json_flag_emits_created_path_matching_the_mcp_tool_shape(self):
        rc, out = run_cli(
            ["note", "create", "--type", "knowledge", "--title", "test knowledge", "--json"],
            self.vault,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(set(payload.keys()), {"created_path"})
        self.assertTrue(payload["created_path"])


class TestProjectUpdateJson(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.vault.config().registry_path.write_text(
            "projects:\n"
            "  - id: project-example\n"
            "    name: Example Project\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )
        (self.vault.root / "30_PROJECTS" / "ACTIVE").mkdir(parents=True, exist_ok=True)
        (self.vault.root / "30_PROJECTS" / "ACTIVE" / "project-example.md").write_text(
            "---\nid: project-example\ntype: project\nstatus: active\n---\n\n# Example Project\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_status_change_json_matches_update_project_status_tool_shape(self):
        rc, out = run_cli(
            ["project", "update", "project-example", "--status", "on-hold", "--json"], self.vault,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["id"], "project-example")
        self.assertEqual(payload["status_change"]["old_status"], "active")
        self.assertEqual(payload["status_change"]["new_status"], "on-hold")
        self.assertIn("updated_path", payload["status_change"])
        self.assertIsNone(payload["updated_path"])

    def test_set_fields_only_reports_updated_path_and_null_status_change(self):
        rc, out = run_cli(
            ["project", "update", "project-example", "--set", "category=software", "--json"],
            self.vault,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertIsNone(payload["status_change"])
        self.assertTrue(payload["updated_path"])


if __name__ == "__main__":
    unittest.main()
