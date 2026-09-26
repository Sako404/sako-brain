"""CLI surface over memoryops/timeline.create_event — argparse wiring only
(see test_memoryops.py / test_timeline.py for the business logic)."""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest.mock import patch

from brain import cli
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


class TestCliNoteCreate(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_person(self):
        rc, out = run_cli(["note", "create", "--type", "person", "--title", "Jane Doe"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Created", out)

    def test_fact_needs_area(self):
        rc, out = run_cli(["note", "create", "--type", "fact", "--title", "X"], self.vault)
        self.assertEqual(rc, 1)

    def test_fact_with_valid_area(self):
        (self.vault.root / "20_AREAS" / "Finance").mkdir(parents=True, exist_ok=True)
        rc, out = run_cli(
            ["note", "create", "--type", "fact", "--title", "X", "--area", "Finance"], self.vault,
        )
        self.assertEqual(rc, 0)

    def test_unsupported_type_fails_cleanly(self):
        rc, out = run_cli(["note", "create", "--type", "event", "--title", "X"], self.vault)
        self.assertEqual(rc, 1)


class TestCliTimelineAdd(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()

    def tearDown(self):
        self.vault.cleanup()

    def test_adds_event(self):
        rc, out = run_cli(
            ["timeline", "add", "--title", "Something happened", "--date", "2026-09-26"], self.vault,
        )
        self.assertEqual(rc, 0)
        self.assertIn("Created", out)

    def test_bare_timeline_still_lists(self):
        run_cli(["timeline", "add", "--title", "X", "--date", "2026-01-01"], self.vault)
        rc, out = run_cli(["timeline"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("event-2026-01-01-x", out)

    def test_duplicate_fails_cleanly(self):
        run_cli(["timeline", "add", "--title", "Dup", "--date", "2026-01-01"], self.vault)
        rc, out = run_cli(["timeline", "add", "--title", "Dup", "--date", "2026-01-01"], self.vault)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
