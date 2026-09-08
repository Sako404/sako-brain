"""OSS-3: does the portable core work WITHOUT the private vault?

OSS-1 and OSS-2 proved the production vault still works. This file proves the
opposite direction: a synthetic user, synthetic paths, a synthetic taxonomy and
a synthetic vocabulary can use the core successfully.

**The central technique is a scrubbed environment.** Every other test in this
suite patches `os.environ` with `clear=False` and therefore inherits the real
HOME, the real XDG directories and the real user's config — so none of them can
tell "works for anybody" apart from "works for this user". The CLI here runs in
a subprocess started from an environment containing only PATH, a temporary
HOME, temporary XDG directories and PYTHONPATH. If the code reaches back into
the developer's machine for anything, these tests fail.

Assertions are semantic wherever possible (roles, ids, behaviour) rather than
tied to exact note counts, so the demo can grow without a test rewrite.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from brain import demo

PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)

# Strings that must never appear in anything the synthetic run produces. Kept
# in step with the OSS-2 gate rather than duplicated: this list extends
# test_oss2_neutralisation.PERSONAL_MARKERS with the production-specific
# identifiers a demo artefact could plausibly pick up.
from tests.test_oss2_neutralisation import DEPLOYMENT_MARKERS, PERSONAL_MARKERS

# Nothing is named here. This file used to extend the list with the private
# vault's directory name, the production backup mount and the owner's account
# name — which made a public test file an inventory of private identifiers
# (found in PRR-2). OSS-2's vocabulary now carries whatever the deployment
# supplied, and this file just picks the right scope.
#
# Artefacts and source: the full set, generic markers included.
PRODUCTION_MARKERS = PERSONAL_MARKERS
# Command output: deployment identifiers only — see DEPLOYMENT_MARKERS.
OUTPUT_MARKERS = DEPLOYMENT_MARKERS

# Directory names the core ships as defaults. They are public, so their
# presence is not a leak — but if they turn up in a *generated* artefact for
# the demo vault, the configured taxonomy did not take effect.
SHIPPED_DEFAULT_DIRS = ("00_INBOX", "10_PEOPLE", "20_AREAS", "30_PROJECTS",
                        "40_DECISIONS", "50_TIMELINE", "60_KNOWLEDGE", "70_DOCUMENTS")


def assert_no_production_markers(case: unittest.TestCase, text: str, what: str) -> None:
    """Assert no production identifier appears in `text`.

    Deliberately expressed through PRODUCTION_MARKERS rather than by writing
    the forbidden strings here: OSS-2's fixture scan flags any test file that
    spells them out, and it is right to — the marker list has exactly one home.
    """
    for marker in OUTPUT_MARKERS:
        case.assertFalse(marker in text, f"production marker {marker!r} leaked into {what}")


from tests.helpers import requires_git

class SyntheticWorld:
    """A throwaway machine: its own HOME, XDG dirs, vault, and project roots.

    Nothing in here is derived from the developer's environment, and the CLI is
    invoked with `env -i` semantics — an environment built from scratch.
    """

    def __init__(self, seed_content: bool = True):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.home.mkdir(parents=True)
        self.demo = demo.build(
            self.base / "vault",
            project_roots_parent=self.base / "projects",
            external=self.base / "external",
            seed_content=seed_content,
        )

    @property
    def vault(self) -> Path:
        return self.demo.root

    def env(self, **extra) -> dict:
        """A from-scratch environment. Nothing is inherited except PATH."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "PYTHONPATH": PACKAGE_PARENT,
        }
        env.update(extra)
        return env

    def run(self, *args, stdin: str | None = None, vault: Path | str | None = None,
            **env_extra) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "brain.cli"]
        target = self.vault if vault is None else vault
        if target is not None:
            cmd += ["--vault", str(target)]
        return subprocess.run(
            cmd + list(args), env=self.env(**env_extra), input=stdin,
            capture_output=True, text=True, timeout=120,
        )

    def cleanup(self):
        self._tmp.cleanup()


class SyntheticVaultTestCase(unittest.TestCase):
    """Shared world, built once per class — generating it is the slow part."""

    seed_content = True

    @classmethod
    def setUpClass(cls):
        cls.world = SyntheticWorld(seed_content=cls.seed_content)

    @classmethod
    def tearDownClass(cls):
        cls.world.cleanup()

    def assertOk(self, result: subprocess.CompletedProcess, *, msg: str = ""):
        self.assertNotIn("Traceback", result.stderr,
                         f"command crashed instead of reporting cleanly{': ' + msg if msg else ''}\n"
                         f"{result.stderr}")
        self.assertEqual(result.returncode, 0, f"{msg}\nstdout:{result.stdout}\nstderr:{result.stderr}")
        return result.stdout


class TestIsolation(SyntheticVaultTestCase):
    """A synthetic run must not be able to see the real user's environment."""

    def test_environment_is_built_from_scratch_not_inherited(self):
        env = self.world.env()
        self.assertEqual(
            set(env), {"PATH", "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                       "XDG_DATA_HOME", "PYTHONPATH"})
        self.assertNotIn("BRAIN_ROOT", env)
        self.assertNotIn("BRAIN_STATE_DIR", env)

    def test_home_and_xdg_all_point_inside_the_throwaway_world(self):
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
            self.assertTrue(self.world.env()[key].startswith(str(self.world.base)))

    def test_without_any_vault_the_core_refuses_rather_than_finding_the_real_one(self):
        """No marker, no env, no user config — and crucially, no fallback."""
        result = subprocess.run(
            [sys.executable, "-m", "brain.cli", "status"],
            env=self.world.env(), cwd=str(self.world.home),
            capture_output=True, text=True, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("No Brain vault found", combined)
        assert_no_production_markers(self, combined, "the no-vault error message")

    def test_the_synthetic_run_reports_only_synthetic_locations(self):
        out = self.assertOk(self.world.run("status"))
        self.assertIn(str(self.world.vault), out)
        assert_no_production_markers(self, out, "`brain status` output")


class TestTaxonomy(SyntheticVaultTestCase):
    """C3, proved end to end through real commands rather than Config asserts."""

    def test_the_vault_uses_none_of_the_shipped_directory_names(self):
        present = {p.name for p in self.world.vault.iterdir() if p.is_dir()}
        self.assertTrue({"work", "choices", "log", "library", "papers"} <= present)
        for shipped in ("00_INBOX", "30_PROJECTS", "40_DECISIONS", "50_TIMELINE"):
            self.assertNotIn(shipped, present)

    def test_indexing_finds_notes_in_the_remapped_directories(self):
        self.assertOk(self.world.run("index"))
        out = self.assertOk(self.world.run("get", demo.DEMO_DECISION_ID))
        self.assertIn("Use local JSON fixtures", out)

    def test_the_registry_lives_under_the_remapped_projects_role(self):
        self.assertTrue((self.world.vault / "work" / "_registry.yaml").is_file())
        out = self.assertOk(self.world.run("projects"))
        self.assertIn(demo.DEMO_PROJECT_NAME, out)

    def test_archived_project_lives_in_the_remapped_archive_bucket(self):
        """OSS-2 archived semantics: status stays in frontmatter, folder is a bucket."""
        archived = self.world.vault / "work" / "archive-box" / f"{demo.DEMO_ARCHIVED_PROJECT_ID}.md"
        self.assertTrue(archived.is_file())
        self.assertIn("status: completed", archived.read_text())

    def test_system_dirname_is_still_the_marker_and_was_not_remapped(self):
        self.assertTrue((self.world.vault / "90_SYSTEM" / "config.yaml").is_file())
        self.assertNotIn("90_SYSTEM", demo.DEMO_DIRECTORIES.values())

    def test_capture_writes_into_the_remapped_inbox(self):
        self.assertOk(self.world.run("remember", "--type", "fact", "--title", "Taxonomy capture"))
        self.assertTrue((self.world.vault / "inbox" / "fact-taxonomy-capture.md").is_file())


class TestVocabulary(SyntheticVaultTestCase):
    """C4: a type that exists only in this vault's configuration."""

    def test_custom_type_is_accepted_only_because_the_config_declares_it(self):
        self.assertIn("experiment", demo.DEMO_NOTE_TYPES)
        out = self.assertOk(self.world.run("remember", "--type", "experiment", "--title", "Custom type note"))
        self.assertIn("experiment-custom-type-note", out)

    def test_argparse_does_not_reject_the_custom_type_before_config_is_read(self):
        """The parser is built before a vault is resolved; it must not judge types."""
        result = self.world.run("remember", "--type", "experiment", "--title", "Parser check")
        self.assertNotIn("invalid choice", result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_type_is_rejected_after_config_resolves_naming_the_real_vocabulary(self):
        result = self.world.run("remember", "--type", "nonsense", "--title", "Nope")
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("unknown type 'nonsense'", combined)
        # The message must list THIS vault's vocabulary, not the shipped default.
        self.assertIn("experiment", combined)

    def test_custom_project_status_validates_cleanly(self):
        project = self.world.vault / "work" / "current" / f"{demo.DEMO_PROJECT_ID}.md"
        original = project.read_text()
        try:
            project.write_text(original.replace("status: active", "status: piloting"))
            out = self.assertOk(self.world.run("doctor"))
            self.assertIn("no problems found", out)
        finally:
            project.write_text(original)

    def test_required_types_survive_in_the_custom_vocabulary(self):
        for required in ("project", "decision"):
            self.assertIn(required, demo.DEMO_NOTE_TYPES)


class TestXdgStateAndGitSeparation(SyntheticVaultTestCase):
    """OSS-1's separation, re-proved on a foreign vault under a scrubbed env."""

    def test_state_and_git_are_configured_outside_the_vault(self):
        for path in (self.world.demo.state_dir, self.world.demo.git_dir):
            self.assertFalse(str(path).startswith(str(self.world.vault)))

    def test_indexing_leaves_no_runtime_artefact_inside_the_vault(self):
        self.assertOk(self.world.run("index"))
        stray = [p for p in self.world.vault.rglob("*")
                 if p.suffix in {".db", ".log"} or p.name.startswith("brain.db")]
        self.assertEqual(stray, [], f"runtime artefacts inside the vault: {stray}")
        self.assertTrue(any(self.world.demo.state_dir.rglob("brain.db")))

    @requires_git
    def test_git_metadata_lands_outside_the_vault(self):
        self.assertOk(self.world.run("git", "init"))
        self.assertTrue((self.world.demo.git_dir / "HEAD").is_file())
        # Only a small pointer file is left in the working tree.
        pointer = self.world.vault / ".git"
        self.assertTrue(pointer.is_file(), "expected a .git pointer FILE, not a directory")

    def test_an_unconfigured_vault_derives_state_under_the_synthetic_xdg_home(self):
        bare = self.world.base / "bare-vault"
        (bare / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (bare / "90_SYSTEM" / "config.yaml").write_text("vault_name: Bare\n")
        self.assertOk(self.world.run("index", vault=bare))
        derived = self.world.home / ".local" / "state" / "sako-brain" / "vaults"
        self.assertTrue(derived.is_dir(), "state did not land under the synthetic XDG_STATE_HOME")


class TestProjectRoots(SyntheticVaultTestCase):
    """C5: several roots, none of them the author's."""

    def test_status_lists_every_configured_root(self):
        out = self.assertOk(self.world.run("status"))
        for root in self.world.demo.project_roots:
            self.assertIn(str(root), out)

    def test_discover_scans_all_roots(self):
        out = self.assertOk(self.world.run("project", "discover"))
        self.assertIn("demo-observatory", out)
        self.assertIn("paper-telescope", out)

    def test_a_scalar_projects_root_is_still_accepted(self):
        """The legacy single-root spelling must keep working for existing vaults."""
        scalar = self.world.base / "scalar-vault"
        (scalar / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        only = self.world.demo.project_roots[0]
        (scalar / "90_SYSTEM" / "config.yaml").write_text(f"projects_root: {only}\n")
        out = self.assertOk(self.world.run("status", vault=scalar))
        self.assertIn(str(only), out)
        self.assertNotIn(str(self.world.demo.project_roots[1]), out)


class TestProjectDecisionAndHandoffFlow(SyntheticVaultTestCase):
    def test_registered_projects_resolve_to_their_synthetic_directories(self):
        out = self.assertOk(self.world.run("projects"))
        for root in self.world.demo.project_roots:
            self.assertIn(str(root), out)

    def test_decision_record_is_retrievable(self):
        self.assertOk(self.world.run("index"))
        out = self.assertOk(self.world.run("get", demo.DEMO_DECISION_ID))
        self.assertIn("status: decided", out)

    def test_timeline_lists_the_synthetic_event(self):
        out = self.assertOk(self.world.run("timeline"))
        self.assertIn("Observatory opened", out)

    def test_handoff_with_content_writes_under_the_remapped_projects_role(self):
        payload = json.dumps({
            "attempted": "Align the demo telescope",
            "changed": "Recorded a synthetic calibration run",
            "working_state": "Demo Observatory is idle and clean",
            "unresolved": "None",
            "next_action": "Calibrate the second mirror",
        })
        out = self.assertOk(self.world.run(
            "handoff", "write", "--project", demo.DEMO_PROJECT_ID, stdin=payload))
        self.assertIn("work/current", out)
        shown = self.assertOk(self.world.run("handoff", "show", demo.DEMO_PROJECT_ID))
        self.assertIn("Align the demo telescope", shown)

    def test_empty_handoff_is_refused_and_leaves_no_artefact(self):
        target = self.world.vault / "work" / "current" / f"{demo.DEMO_ARCHIVED_PROJECT_ID}-handoff.md"
        before = target.exists()
        result = self.world.run(
            "handoff", "write", "--project", demo.DEMO_ARCHIVED_PROJECT_ID,
            stdin=json.dumps({"decisions": ["a decision but no prose"]}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty handoff", result.stdout + result.stderr)
        self.assertEqual(target.exists(), before, "a refused handoff left an artefact behind")


class TestAgentsDocGeneration(SyntheticVaultTestCase):
    def test_generation_succeeds_on_a_foreign_vault(self):
        out = self.assertOk(self.world.run("agents-doc"))
        self.assertIn(f"# AGENTS.md — {demo.DEMO_VAULT_NAME}", out)

    def test_generated_document_reflects_the_synthetic_taxonomy_and_vocabulary(self):
        out = self.assertOk(self.world.run("agents-doc"))
        for role_dir in ("inbox/", "work/", "choices/", "log/", "library/", "papers/"):
            self.assertIn(f"`{role_dir}`", out)
        for shipped in SHIPPED_DEFAULT_DIRS:
            self.assertNotIn(shipped, out,
                             f"shipped default {shipped!r} survived into a remapped vault's document")
        self.assertIn("experiment", out)          # custom note type
        self.assertIn("piloting", out)            # custom project status
        self.assertIn("archive-box", out)         # remapped archive bucket
        for root in self.world.demo.project_roots:
            self.assertIn(str(root), out)

    def test_generation_is_deterministic(self):
        first = self.assertOk(self.world.run("agents-doc"))
        second = self.assertOk(self.world.run("agents-doc"))
        self.assertEqual(first, second, "two renders differed — generation is not deterministic")

    def test_no_production_path_or_name_leaks_into_the_generated_document(self):
        out = self.assertOk(self.world.run("agents-doc"))
        assert_no_production_markers(self, out, "the generated AGENTS.md")

    def test_structural_invariants_hold_the_same_as_for_any_vault(self):
        """Compared to production only as structure, never as content."""
        out = self.assertOk(self.world.run("agents-doc"))
        for section in ("## What this is", "## Directory map", "## Data model",
                        "## Memory rules (must follow)", "## Which vault?", "## Validation"):
            self.assertIn(section, out)
        self.assertNotIn("{{", out)


class TestCleanSyntheticVaultPasses(SyntheticVaultTestCase):
    def test_index_doctor_and_integrity_are_all_clean(self):
        index_out = self.assertOk(self.world.run("index"))
        self.assertIn("Indexed", index_out)

        doctor_out = self.assertOk(self.world.run("doctor"))
        self.assertIn("no problems found", doctor_out)

        integrity_out = self.assertOk(self.world.run("integrity"))
        self.assertIn("Doctor problems: 0", integrity_out)
        self.assertIn("duplicate_ids=0, broken_links=0, registry_errors=0", integrity_out)

    def test_every_seeded_note_is_indexed_and_retrievable(self):
        """Semantic rather than count-based, so the demo can grow freely."""
        self.assertOk(self.world.run("index"))
        for note_id in demo.SEEDED_NOTE_IDS:
            self.assertOk(self.world.run("get", note_id), msg=f"note {note_id} not retrievable")

    def test_internal_links_in_the_demo_actually_resolve(self):
        out = self.assertOk(self.world.run("doctor"))
        self.assertNotIn("broken_links", out)


class TestNegativeValidations(unittest.TestCase):
    """Each invalid state is introduced alone, proved caught, then reverted.

    A fresh world per test, so no negative case can contaminate another and the
    vault each one leaves behind is provably clean again.
    """

    def setUp(self):
        self.world = SyntheticWorld()
        self.assertEqual(self.world.run("doctor").returncode, 0)

    def tearDown(self):
        self.world.cleanup()

    def _doctor(self, **env_extra) -> str:
        result = self.world.run("doctor", **env_extra)
        return result.stdout + result.stderr

    def assertVaultCleanAgain(self):
        out = self.world.run("doctor")
        self.assertIn("no problems found", out.stdout, "vault did not return to a clean state")

    def test_state_dir_inside_the_vault_is_caught(self):
        inside = self.world.vault / "90_SYSTEM" / "state"
        out = self._doctor(BRAIN_STATE_DIR=str(inside))
        self.assertIn("state_dir_inside_vault", out)
        self.assertVaultCleanAgain()

    def test_missing_required_config_for_discover_is_reported_not_guessed(self):
        config = self.world.vault / "90_SYSTEM" / "config.yaml"
        original = config.read_text()
        try:
            config.write_text("\n".join(
                line for line in original.splitlines()
                if not line.startswith(("projects_roots:", "- /"))
            ) + "\n")
            result = self.world.run("project", "discover")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No projects root configured", result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        finally:
            config.write_text(original)
        self.assertVaultCleanAgain()

    def test_broken_taxonomy_role_is_rejected_at_config_load(self):
        config = self.world.vault / "90_SYSTEM" / "config.yaml"
        original = config.read_text()
        try:
            config.write_text(original.replace("directories:", "directories:\n  telescopes: scopes"))
            result = self.world.run("doctor")
            combined = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unknown directory role", combined)
        finally:
            config.write_text(original)
        self.assertVaultCleanAgain()

    def test_a_vocabulary_dropping_a_required_type_is_rejected(self):
        config = self.world.vault / "90_SYSTEM" / "config.yaml"
        original = config.read_text()
        try:
            config.write_text(original.replace("- project\n", ""))
            result = self.world.run("doctor")
            combined = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("note_types must include", combined)
        finally:
            config.write_text(original)
        self.assertVaultCleanAgain()

    def test_a_status_outside_the_configured_vocabulary_is_caught(self):
        project = self.world.vault / "work" / "current" / f"{demo.DEMO_PROJECT_ID}.md"
        original = project.read_text()
        try:
            project.write_text(original.replace("status: active", "status: decommissioned"))
            self.assertIn("invalid_status", self._doctor())
        finally:
            project.write_text(original)
        self.assertVaultCleanAgain()

    def test_a_broken_internal_link_is_caught(self):
        note = self.world.vault / "library" / "knowledge-demo-overview.md"
        original = note.read_text()
        try:
            note.write_text(original + "\n- Dangling: [[knowledge-does-not-exist]]\n")
            self.assertIn("broken_links", self._doctor())
        finally:
            note.write_text(original)
        self.assertVaultCleanAgain()

    def test_a_registry_entry_pointing_nowhere_is_caught(self):
        registry = self.world.vault / "work" / "_registry.yaml"
        original = registry.read_text()
        try:
            registry.write_text(original.replace(
                str(self.world.demo.project_roots[0]), "/nonexistent/synthetic"))
            self.assertIn("missing_project_dirs", self._doctor())
        finally:
            registry.write_text(original)
        self.assertVaultCleanAgain()

    def test_an_empty_handoff_leaves_the_vault_clean(self):
        result = self.world.run(
            "handoff", "write", "--project", demo.DEMO_PROJECT_ID,
            stdin=json.dumps({"attempted": "   "}))
        self.assertNotEqual(result.returncode, 0)
        self.assertVaultCleanAgain()


class TestBackupConfigurationBehaviour(SyntheticVaultTestCase):
    """Portable configuration behaviour only — no repository is ever contacted.

    The demo vault configures no backup destination, which is exactly the
    first-run state a new user is in.
    """

    def test_status_reports_no_backup_target_rather_than_inventing_one(self):
        out = self.assertOk(self.world.run("status"))
        self.assertIn("Backup target:  (not configured)", out)

    def test_missing_local_target_names_the_config_key(self):
        from brain import backup

        with self.assertRaises(backup.BackupError) as ctx:
            backup.BackupSettings(backend="local").repository()
        self.assertIn("backup_repo:", str(ctx.exception))

    def test_missing_rclone_remote_names_the_config_keys(self):
        from brain import backup

        with self.assertRaises(backup.BackupError) as ctx:
            backup.BackupSettings(backend="rclone").repository()
        self.assertIn("backup_rclone_remote:", str(ctx.exception))
        self.assertIn("backup_rclone_repo_path:", str(ctx.exception))

    def test_a_configured_synthetic_destination_resolves(self):
        from brain import backup
        from brain.paths import default_config

        configured = self.world.base / "configured-vault"
        (configured / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (configured / "90_SYSTEM" / "config.yaml").write_text(
            "backup_rclone_remote: DEMO_REMOTE\nbackup_rclone_repo_path: demo/backups\n")
        settings = backup.default_settings(default_config(configured))
        self.assertEqual(settings.repository(), "rclone:DEMO_REMOTE:demo/backups")

    def test_the_credential_path_follows_the_synthetic_home(self):
        """No credential exists in a fresh world, so this must fail cleanly —
        naming a path under the synthetic HOME, never the author's."""
        result = self.world.run("backup", "snapshots")
        combined = result.stdout + result.stderr
        self.assertNotIn("Traceback", result.stderr, combined)
        self.assertIn("No backup password file", combined)
        self.assertIn(str(self.world.home), combined)
        assert_no_production_markers(self, combined, "`brain backup snapshots` output")


class TestFirstRunErrorsAreReportedNotCrashed(SyntheticVaultTestCase):
    """A clean install must never answer a missing setting with a stack trace.

    Found by OSS-3 and fixed in the same stage: on the author's machine a
    backup destination and a git identity always exist, so three `brain backup`
    commands and `brain git snapshot` printed a traceback on top of a perfectly
    good message for anyone who had not configured them yet. `backup snapshots`
    and `brain integrity` already handled their own case; `main()` now applies
    that pattern to every user-facing domain error.
    """

    def _assert_clean_failure(self, result, *, expect: str, what: str):
        self.assertNotIn("Traceback", result.stderr, f"{what} crashed:\n{result.stderr}")
        self.assertNotEqual(result.returncode, 0, f"{what} should have failed")
        combined = result.stdout + result.stderr
        self.assertIn(expect, combined, f"{what} did not name what to configure:\n{combined}")

    def test_a_fresh_world_reports_the_missing_credential_not_a_traceback(self):
        """Nothing is configured at all — the true first-run state."""
        for command in (("backup", "check"), ("backup", "init"), ("backup", "snapshots")):
            with self.subTest(command=" ".join(command)):
                self._assert_clean_failure(
                    self.world.run(*command, stdin="n\n"),
                    expect="No backup password file", what=" ".join(command))

    def test_backup_status_reports_the_missing_destination(self):
        """`status` reaches the destination even with no credential, because it
        prints the repository first. It was the loudest of the three crashes."""
        self._assert_clean_failure(
            self.world.run("backup", "status"),
            expect="backup_rclone_remote:", what="backup status")

    def test_with_a_credential_present_every_command_names_the_destination_key(self):
        """The next state a new user reaches: a password file, no destination.

        The credential is a synthetic string in a throwaway HOME and no
        repository is ever contacted — these commands fail before any network
        or restic call.
        """
        world = SyntheticWorld()
        try:
            credential = world.home / ".config" / "sako-brain" / "restic-password"
            credential.parent.mkdir(parents=True, exist_ok=True)
            credential.write_text("synthetic-not-a-real-password\n")
            for command in (("backup", "status"), ("backup", "check"), ("backup", "init")):
                with self.subTest(command=" ".join(command)):
                    self._assert_clean_failure(
                        world.run(*command, stdin="n\n"),
                        expect="backup_rclone_remote:", what=" ".join(command))
        finally:
            world.cleanup()

    def test_backup_snapshots_keeps_its_own_handling(self):
        result = self.world.run("backup", "snapshots")
        self.assertNotIn("Traceback", result.stderr)

    def test_an_unresolvable_vault_is_an_error_not_a_traceback(self):
        result = subprocess.run(
            [sys.executable, "-m", "brain.cli", "status"],
            env=self.world.env(), cwd=str(self.world.home),
            capture_output=True, text=True, timeout=120,
        )
        self._assert_clean_failure(result, expect="No Brain vault found", what="status with no vault")

    def test_a_broken_config_is_an_error_not_a_traceback(self):
        broken = self.world.base / "broken-config-vault"
        (broken / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (broken / "90_SYSTEM" / "config.yaml").write_text("directories:\n  telescopes: scopes\n")
        self._assert_clean_failure(
            self.world.run("doctor", vault=broken),
            expect="unknown directory role", what="doctor on a broken config")

    def test_git_snapshot_without_an_identity_does_not_crash(self):
        """git's own error must arrive as a message.

        Tolerant by design: a machine with a system-wide git identity will
        succeed here. The invariant under test is "never a traceback", not
        "always fails" — the latter would depend on the host's git config.
        """
        world = SyntheticWorld()
        try:
            world.run("git", "init")
            result = world.run("git", "snapshot", "-m", "synthetic snapshot")
            self.assertNotIn("Traceback", result.stderr, result.stderr)
        finally:
            world.cleanup()

    def test_every_user_facing_error_type_is_actually_routed(self):
        """Pins the set deliberately: adding a domain error is a decision, and
        forgetting to route one is how tracebacks came back last time.

        Grew by one in OSS-4 when `brain init` gained InitError.
        """
        from brain import backup, cli, gitops, handoff
        from brain import init as init_mod
        from brain import paths as paths_mod

        self.assertEqual(
            set(cli.USER_FACING_ERRORS),
            {paths_mod.VaultNotFoundError, paths_mod.TaxonomyError,
             paths_mod.VocabularyError, backup.BackupError, gitops.GitError,
             handoff.HandoffError, init_mod.InitError},
        )


class TestNoPrivateDataInTheDemo(unittest.TestCase):
    """Permanent leak gate over the generator and everything it produces."""

    def test_the_generator_module_carries_no_production_identifier(self):
        source = Path(demo.__file__).read_text()
        for marker in PRODUCTION_MARKERS:
            self.assertNotIn(marker, source, f"{marker!r} in the demo generator")

    def test_nothing_the_generator_writes_carries_a_production_identifier(self):
        world = SyntheticWorld()
        try:
            world.run("index")
            offenders = []
            for path in sorted(world.vault.rglob("*")):
                if not path.is_file():
                    continue
                text = path.read_text(errors="ignore")
                for marker in PRODUCTION_MARKERS:
                    if marker in text:
                        offenders.append(f"{path.relative_to(world.vault)}: {marker}")
            self.assertEqual(offenders, [], "production data in generated vault:\n" + "\n".join(offenders))
        finally:
            world.cleanup()

    def test_the_synthetic_identity_is_obviously_fictional(self):
        self.assertEqual(demo.DEMO_USER, "Alex Example")
        self.assertIn("example", demo.DEMO_PERSON_ID)


class TestPortabilityNegativeProof(unittest.TestCase):
    """Tests that would have FAILED before OSS-1/OSS-2, with the causality stated.

    Each case here depends on a specific completed change. They are the proof
    that the synthetic vault is not merely passing by accident.
    """

    def setUp(self):
        self.world = SyntheticWorld()

    def tearDown(self):
        self.world.cleanup()

    def test_depends_on_oss1_the_vault_is_not_the_package_location(self):
        """Before OSS-1, `paths.py` derived the vault from the package's own
        directory, so a foreign vault was unreachable: every command would have
        operated on the vault containing the code."""
        out = self.world.run("status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(str(self.world.vault), out.stdout)
        self.assertNotIn(PACKAGE_PARENT, out.stdout)

    def test_depends_on_oss1_runtime_state_is_not_written_into_the_vault(self):
        """Before OSS-1 the index lived at `90_SYSTEM/brain.db` inside the vault."""
        self.world.run("index")
        self.assertFalse((self.world.vault / "90_SYSTEM" / "brain.db").exists())
        self.assertTrue(any(self.world.demo.state_dir.rglob("brain.db")))

    def test_depends_on_oss2_c5_no_fallback_to_the_authors_projects_root(self):
        """Before C5, `projects_root` fell back to a hardcoded home directory,
        so a vault that configured nothing silently scanned the author's disk."""
        bare = self.world.base / "no-roots"
        (bare / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (bare / "90_SYSTEM" / "config.yaml").write_text("vault_name: NoRoots\n")
        result = self.world.run("project", "discover", vault=bare)
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("No projects root configured", combined)
        assert_no_production_markers(self, combined, "`brain project discover` output")

    def test_depends_on_oss2_c3_a_remapped_taxonomy_is_actually_indexed(self):
        """Before C3, CONTENT_DIRS was a fixed 8-tuple, so a vault using
        `work/` and `choices/` would have indexed zero notes."""
        out = self.world.run("index")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("Indexed 0 notes", out.stdout)

    def test_depends_on_oss2_c4_a_custom_note_type_can_be_written(self):
        """Before C4, argparse's `choices=` rejected any type outside the
        hardcoded set before configuration was ever read."""
        result = self.world.run("remember", "--type", "experiment", "--title", "Negative proof")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("invalid choice", result.stderr)

    def test_depends_on_oss2_c6_the_credential_path_follows_the_synthetic_home(self):
        """Before C6, DEFAULT_PASSWORD_FILE was an absolute path in the
        author's home directory, identical for every user on every machine."""
        out = self.world.run("backup", "snapshots")
        combined = out.stdout + out.stderr
        self.assertIn(str(self.world.home), combined)
        assert_no_production_markers(self, combined, "the credential path")
        self.assertNotIn("Traceback", out.stderr)


if __name__ == "__main__":
    unittest.main()
