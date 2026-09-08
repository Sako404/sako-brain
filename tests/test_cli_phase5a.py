"""CLI-level dispatch tests for the Phase 5A commands (brain memory / brain
handoff), plus small integration tests for "project resolution" and the
data sources /resume relies on. Exercises `cli.main()` end-to-end via
BRAIN_ROOT, the same way the real `brain` binary is invoked — not just the
underlying library functions.
"""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from brain import cli, gitops, handoff, memoryqueue
from tests.helpers import requires_git, TempVault


def run_cli(argv, vault, stdin_text=None):
    """Drive the CLI the way the real `brain` binary is driven — through the
    environment. Both variables matter since OSS-1: BRAIN_ROOT says which
    vault, BRAIN_STATE_DIR says where that vault's runtime state lives. Without
    the second one the CLI would derive a real XDG state directory and litter
    the developer's machine with per-temp-vault index directories."""
    out = io.StringIO()
    env = dict(os.environ)
    env["BRAIN_ROOT"] = str(vault.root)
    env["BRAIN_STATE_DIR"] = str(vault.state_dir)
    with patch.dict(os.environ, env, clear=False):
        if stdin_text is not None:
            with patch("sys.stdin", io.StringIO(stdin_text)):
                with contextlib.redirect_stdout(out):
                    rc = cli.main(argv)
        else:
            with contextlib.redirect_stdout(out):
                rc = cli.main(argv)
    return rc, out.getvalue()


class TestMemoryCliDispatch(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_add_then_pending_then_accept_round_trip(self):
        rc, out = run_cli(
            ["memory", "add", "--fact", "Alex sold the estate car", "--source", "user statement"],
            self.vault,
        )
        self.assertEqual(rc, 0)
        self.assertIn("Queued as mem-", out)

        entries = memoryqueue.list_pending(self.config)
        self.assertEqual(len(entries), 1)
        entry_id = entries[0].id

        rc, out = run_cli(["memory", "pending"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn(entry_id, out)

        rc, out = run_cli(["memory", "accept", entry_id], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Accepted", out)
        self.assertEqual(memoryqueue.get(self.config, entry_id).status, "accepted")

    def test_reject_via_cli(self):
        entry = memoryqueue.add(self.config, candidate_fact="Speculative fact")
        rc, out = run_cli(["memory", "reject", entry.id, "--note", "not confirmed"], self.vault)
        self.assertEqual(rc, 0)
        self.assertEqual(memoryqueue.get(self.config, entry.id).status, "rejected")

    def test_accept_unknown_id_returns_nonzero(self):
        rc, out = run_cli(["memory", "accept", "mem-doesnotexist"], self.vault)
        self.assertNotEqual(rc, 0)


class TestHandoffCliDispatch(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-example\n"
            "    name: Example Project\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_write_then_show_round_trip(self):
        payload = json.dumps({
            "attempted": "Do the thing", "changed": "Changed the thing",
            "working_state": "Works", "unresolved": "None",
            "next_action": "Ship it", "files_changed": ["a.py"], "decisions": [],
        })
        rc, out = run_cli(["handoff", "write", "--project", "project-example"], self.vault, stdin_text=payload)
        self.assertEqual(rc, 0)
        self.assertIn("Handoff written", out)

        rc, out = run_cli(["handoff", "show", "project-example"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Do the thing", out)

    def test_list_shows_projects_with_handoffs(self):
        rc, out = run_cli(["handoff", "list"], self.vault)
        self.assertIn("No project has a handoff yet", out)

        handoff.write(self.config, "project-example", handoff.HandoffSections(attempted="x"))

        rc, out = run_cli(["handoff", "list"], self.vault)
        self.assertIn("project-example", out)

    def test_show_for_project_without_handoff(self):
        rc, out = run_cli(["handoff", "show", "project-example"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("No handoff exists yet", out)


class TestProjectResolution(unittest.TestCase):
    """'Resolve project via Brain registry' — the first step of both the
    project workflow and /resume."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-example\n"
            "    name: Example Project\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )

    def tearDown(self):
        self.vault.cleanup()

    def test_brain_project_show_resolves_registered_project(self):
        rc, out = run_cli(["project", "project-example"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Example Project", out)

    def test_brain_project_show_unknown_id_fails_clearly(self):
        rc, out = run_cli(["project", "project-nonexistent"], self.vault)
        self.assertNotEqual(rc, 0)


class TestResumeDataSources(unittest.TestCase):
    """/resume has no single function — it composes a handoff, the Brain
    project record, and registry data. Verify those pieces agree and are
    all independently reachable, which is what the skill relies on."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-example\n"
            "    name: Example Project\n"
            "    path: /tmp/does-not-need-to-exist\n"
            "    status: active\n"
        )
        self.vault.write_note(
            "30_PROJECTS/ACTIVE", "project-example.md", id="project-example", type="project",
            status="active", title="Example Project", body="Purpose: testing resume.",
        )

    def tearDown(self):
        self.vault.cleanup()

    @requires_git
    def test_resume_has_handoff_plus_project_record_plus_git_history(self):
        gitops.init_repo(self.config)
        handoff.write(self.config, "project-example",
                       handoff.HandoffSections(attempted="previous session work", next_action="do the next bit"))
        gitops.snapshot(self.config)

        # All three data sources /resume needs are independently available:
        latest = handoff.read_latest(self.config, "project-example")
        self.assertIn("previous session work", latest)

        record_path = self.config.brain_root / "30_PROJECTS" / "ACTIVE" / "project-example.md"
        self.assertTrue(record_path.exists())
        self.assertIn("testing resume", record_path.read_text())

        self.assertIsNotNone(gitops.last_snapshot_date(self.config))


if __name__ == "__main__":
    unittest.main()
