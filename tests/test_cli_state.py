"""`brain state` / `brain state --json` — the CLI surface over
get_operational_state(). No business logic of its own: this file only
locks that the CLI calls the same function and prints/serializes its
result, never a second computation.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli, indexer, state as state_mod
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


class TestStateCommand(unittest.TestCase):
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
        self.vault.write_note("40_DECISIONS", "decision-a.md", id="decision-a", type="decision",
                               status="proposed", title="Decision A")
        indexer.rebuild(self.config)

    def tearDown(self):
        self.vault.cleanup()

    def test_human_readable_output_runs_clean(self):
        rc, out = run_cli(["state"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Operational state", out)
        self.assertIn("Projects: 1", out)
        self.assertIn("Open decisions: 1", out)
        self.assertIn("decision-a", out)

    def test_json_output_is_valid_and_matches_the_function_directly(self):
        rc, out = run_cli(["state", "--json"], self.vault)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        direct = dataclasses.asdict(state_mod.get_operational_state(self.config))
        # generated_at legitimately differs between the two calls.
        payload.pop("generated_at")
        direct.pop("generated_at")
        self.assertEqual(payload, direct)

    def test_include_restricted_flag_is_wired_through(self):
        path = self.vault.root / "40_DECISIONS" / "decision-restricted.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid: decision-restricted\ntype: decision\nstatus: proposed\n"
            "sensitivity: restricted\n---\n\n# Restricted decision\n"
        )
        indexer.rebuild(self.config)

        rc, out = run_cli(["state", "--json"], self.vault)
        payload = json.loads(out)
        self.assertNotIn("decision-restricted", [d["id"] for d in payload["decisions"]["open"]])

        rc, out = run_cli(["state", "--json", "--include-restricted"], self.vault)
        payload = json.loads(out)
        self.assertIn("decision-restricted", [d["id"] for d in payload["decisions"]["open"]])

    def test_timeline_days_flag_is_wired_through(self):
        rc, out = run_cli(["state", "--json", "--timeline-days", "3"], self.vault)
        payload = json.loads(out)
        self.assertEqual(payload["timeline_recent"]["window_days"], 3)


if __name__ == "__main__":
    unittest.main()
