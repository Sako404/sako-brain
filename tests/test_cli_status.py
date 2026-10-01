"""`brain status` — regression coverage for the count_by_type()/
count_inbox_pending() extraction (brain state V1, commit 1). Locks that the
CLI's printed numbers come from those functions, not a second, possibly
diverging inline computation.
"""
from __future__ import annotations

import contextlib
import io
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


class TestStatusCommand(unittest.TestCase):
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
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        self.vault.write_note("10_PEOPLE", "p.md", id="person-p", type="person")
        # Stage 2: a registry entry's visibility is decided by its backing
        # note's own frontmatter (registry.py's _project_note_visible) — a
        # registry row with no corresponding note fails closed, so this
        # fixture needs one, matching what `brain project create` always
        # produces together in real usage.
        self.vault.write_note("30_PROJECTS/ACTIVE", "project-alpha.md", id="project-alpha", type="project")
        indexer.rebuild(self.config)
        (self.config.inbox_dir / "draft.md").write_text("draft\n")

    def tearDown(self):
        self.vault.cleanup()

    def test_status_reports_expected_counts(self):
        rc, out = run_cli(["status"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Indexed notes: 3", out)
        self.assertIn("knowledge: 1", out)
        self.assertIn("person: 1", out)
        self.assertIn("project: 1", out)
        self.assertIn("Inbox pending triage: 1", out)
        self.assertIn("Registered projects: 1", out)
        self.assertIn("active: 1", out)

    def test_status_matches_extracted_functions_directly(self):
        rc, out = run_cli(["status"], self.vault)
        counts = indexer.count_by_type(self.config)
        self.assertTrue(counts, "fixture should have produced indexed notes")
        for type_name, count in counts.items():
            self.assertIn(f"{type_name}: {count}", out)
        self.assertIn(f"Inbox pending triage: {indexer.count_inbox_pending(self.config)}", out)

    def test_status_before_index_built(self):
        vault = TempVault()
        try:
            rc, out = run_cli(["status"], vault)
            self.assertEqual(rc, 0)
            self.assertIn("Index: not built yet", out)
        finally:
            vault.cleanup()


if __name__ == "__main__":
    unittest.main()
