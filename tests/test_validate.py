import dataclasses
import unittest

from brain import identity, validate
from tests.helpers import TempVault


class TestValidate(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()

    def tearDown(self):
        self.vault.cleanup()

    def test_clean_vault_has_no_problems(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        problems = validate.run_all(self.config)
        self.assertEqual(problems, [])

    def test_detects_duplicate_ids(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-dup", type="knowledge")
        self.vault.write_note("60_KNOWLEDGE", "b.md", id="knowledge-dup", type="knowledge")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("duplicate_ids", checks)

    def test_detects_invalid_status(self):
        self.vault.write_note("30_PROJECTS/ACTIVE", "p.md", id="project-x", type="project", status="in-progress")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("invalid_status", checks)

    def test_detects_invalid_yaml(self):
        path = self.vault.root / "60_KNOWLEDGE" / "bad.md"
        path.write_text("---\nid: [unterminated\n---\nbody\n")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("invalid_yaml", checks)

    def test_detects_broken_wikilink(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge",
                               body="See [[knowledge-nonexistent]] for more.")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("broken_links", checks)

    def test_valid_wikilink_is_not_flagged(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge",
                               body="See [[knowledge-b]] for more.")
        self.vault.write_note("60_KNOWLEDGE", "b.md", id="knowledge-b", type="knowledge")
        problems = validate.run_all(self.config)
        broken = [p for p in problems if p.check == "broken_links"]
        self.assertEqual(broken, [])

    def test_detects_secret_pattern(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge",
                               body="my aws key is AKIAABCDEFGHIJKLMNOP, don't lose it")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("secret_pattern", checks)

    def test_detects_missing_project_dir(self):
        path = self.vault.root / "30_PROJECTS" / "ACTIVE" / "p.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "id: project-ghost\n"
            "type: project\n"
            "status: active\n"
            "path: /nonexistent/ghost/path\n"
            "---\n\n"
            "# Ghost project\n"
        )
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("missing_project_dirs", checks)

    def test_missing_project_dir_reported_as_unverifiable_when_remote(self):
        """A restricted-SSH server dispatcher (config.remote_project_paths)
        can never see desktop project paths — the same gap must be reported
        as project_path_unverifiable, never as missing_project_dirs, so it
        is never mistaken for actual data drift."""
        import dataclasses

        path = self.vault.root / "30_PROJECTS" / "ACTIVE" / "p.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "id: project-ghost\n"
            "type: project\n"
            "status: active\n"
            "path: /nonexistent/ghost/path\n"
            "---\n\n"
            "# Ghost project\n"
        )
        remote_config = dataclasses.replace(self.config, remote_project_paths=True)
        problems = validate.run_all(remote_config)
        checks = {p.check for p in problems}
        self.assertIn("project_path_unverifiable", checks)
        self.assertNotIn("missing_project_dirs", checks)
        unverifiable = [p for p in problems if p.check == "project_path_unverifiable"]
        self.assertTrue(any("cannot verify from this host" in p.message for p in unverifiable))

    def test_missing_project_dir_is_not_a_blocking_check_in_either_mode(self):
        import dataclasses

        from brain import gitops
        self.assertNotIn("missing_project_dirs", gitops.BLOCKING_CHECKS)
        self.assertNotIn("project_path_unverifiable", gitops.BLOCKING_CHECKS)

    def test_detects_registry_duplicate(self):
        self.config.registry_path.write_text(
            "projects:\n"
            "  - id: project-dup\n    name: A\n    path: /tmp/a\n    status: active\n"
            "  - id: project-dup\n    name: B\n    path: /tmp/b\n    status: active\n"
        )
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("duplicate_registry_entries", checks)

    def test_detects_dangling_supersedes(self):
        path = self.vault.root / "40_DECISIONS" / "d.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "id: decision-new\n"
            "type: decision\n"
            "status: decided\n"
            "supersedes: decision-nonexistent\n"
            "---\n\n"
            "# New decision\n"
        )
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("dangling_supersedes", checks)

    def test_valid_supersedes_is_not_flagged(self):
        old = self.vault.root / "40_DECISIONS" / "old.md"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("---\nid: decision-old\ntype: decision\nstatus: superseded\n---\n\n# Old\n")
        new = self.vault.root / "40_DECISIONS" / "new.md"
        new.write_text("---\nid: decision-new\ntype: decision\nstatus: decided\nsupersedes: decision-old\n---\n\n# New\n")
        problems = validate.run_all(self.config)
        dangling = [p for p in problems if p.check == "dangling_supersedes"]
        self.assertEqual(dangling, [])

    def test_detects_stale_validity_on_current_fact(self):
        path = self.vault.root / "60_KNOWLEDGE" / "f.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "id: fact-old-balance\n"
            "type: fact\n"
            "status: current\n"
            "valid_to: 2020-01-01\n"
            "---\n\n"
            "# Some fact\n"
        )
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("stale_validity", checks)

    def test_superseded_fact_with_past_valid_to_not_flagged(self):
        path = self.vault.root / "60_KNOWLEDGE" / "f.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            "id: fact-old-balance\n"
            "type: fact\n"
            "status: superseded\n"
            "valid_to: 2020-01-01\n"
            "---\n\n"
            "# Some fact\n"
        )
        problems = validate.run_all(self.config)
        stale = [p for p in problems if p.check == "stale_validity"]
        self.assertEqual(stale, [])

    def test_backup_as_canonical_detected(self):
        self.vault.config()
        from brain.paths import Config
        bad_config = Config(
            brain_root=self.vault.root,
            projects_roots=(self.vault.projects_root,),
            backup_target=self.vault.root,
        )
        problems = validate.run_all(bad_config)
        checks = {p.check for p in problems}
        self.assertIn("backup_as_canonical", checks)

    def test_detects_invalid_sensitivity(self):
        path = self.vault.root / "60_KNOWLEDGE" / "a.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nid: knowledge-a\ntype: knowledge\nsensitivity: top-secret\n---\n\n# A\n")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("invalid_sensitivity", checks)

    def test_valid_sensitivity_values_not_flagged(self):
        for i, s in enumerate(["normal", "private", "restricted"]):
            self.vault.write_note("60_KNOWLEDGE", f"n{i}.md", id=f"knowledge-n{i}", type="knowledge")
        problems = validate.run_all(self.config)
        self.assertEqual([p for p in problems if p.check == "invalid_sensitivity"], [])

    def test_detects_credential_filename_in_brain(self):
        secret_path = self.vault.root / "90_SYSTEM" / "restic-password"
        secret_path.write_text("hunter2\n")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("backup_secret_in_brain", checks)

    def test_credential_filename_ignored_inside_dot_git(self):
        # .git internals aren't Brain content and shouldn't be scanned this way.
        dotgit_path = self.vault.root / ".git" / "id_rsa"
        dotgit_path.parent.mkdir(parents=True, exist_ok=True)
        dotgit_path.write_text("not actually a key\n")
        problems = validate.check_backup_secret_in_brain(self.config)
        self.assertEqual(problems, [])

    def test_detects_nonempty_deprecated_plaintext_backup_target(self):
        self.config.backup_target.mkdir(parents=True, exist_ok=True)
        (self.config.backup_target / "leftover.md").write_text("# leftover\n")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("unsafe_plaintext_backup", checks)

    def test_detects_restricted_note_in_plaintext_backup_target(self):
        self.config.backup_target.mkdir(parents=True, exist_ok=True)
        (self.config.backup_target / "leaked.md").write_text(
            "---\nid: fact-leaked\ntype: fact\nsensitivity: restricted\n---\n\n# Leaked\n"
        )
        problems = validate.run_all(self.config)
        messages = " ".join(p.message for p in problems if p.check == "unsafe_plaintext_backup")
        self.assertIn("RESTRICTED", messages)

    def test_empty_backup_target_not_flagged(self):
        self.config.backup_target.mkdir(parents=True, exist_ok=True)
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertNotIn("unsafe_plaintext_backup", checks)

    def test_restic_password_mention_in_note_body_flagged_as_secret(self):
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge",
                               body="RESTIC_PASSWORD=hunter2plaintextpassword")
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("secret_pattern", checks)

    def test_password_file_outside_backup_scope_not_flagged(self):
        # TempVault's default password file (from BackupSettings' own
        # default) lives outside both brain_root and git_dir already.
        problems = validate.check_password_file_not_backed_up(self.config)
        self.assertEqual(problems, [])

    def test_password_file_inside_brain_root_flagged(self):
        from unittest.mock import patch

        from brain import backup as backup_module
        bad_password_file = self.vault.root / "90_SYSTEM" / "restic-password-oops"
        bad_password_file.write_text("hunter2\n")
        bad_settings = backup_module.BackupSettings(
            password_file=bad_password_file, local_repo_path=self.config.backup_repo,
        )

        with patch.object(backup_module, "default_settings", return_value=bad_settings):
            problems = validate.check_password_file_not_backed_up(self.config)

        checks = {p.check for p in problems}
        self.assertIn("password_file_in_backup_scope", checks)

    def test_password_file_inside_git_dir_flagged(self):
        from unittest.mock import patch

        from brain import backup as backup_module
        self.config.git_dir.mkdir(parents=True, exist_ok=True)
        bad_password_file = self.config.git_dir / "restic-password-oops"
        bad_password_file.write_text("hunter2\n")
        bad_settings = backup_module.BackupSettings(
            password_file=bad_password_file, local_repo_path=self.config.backup_repo,
        )

        with patch.object(backup_module, "default_settings", return_value=bad_settings):
            problems = validate.check_password_file_not_backed_up(self.config)

        checks = {p.check for p in problems}
        self.assertIn("password_file_in_backup_scope", checks)


class TestDoctorAdminGate(unittest.TestCase):
    """Stage 2 (multi-user visibility): `brain doctor`'s detailed,
    vault-wide diagnostics (ids/paths/titles, backup/state configuration)
    are admin-only — it's reachable over the SSH dispatcher's read mode
    like any other `brain doctor` call, so a non-admin family member
    asking for it must not get the full picture just by asking. The
    underlying checks (check_duplicate_ids etc.) stay unfiltered/
    whole-vault internally — only run_all's gate guards the output."""

    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        self.vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge")
        self.vault.write_note("60_KNOWLEDGE", "b.md", id="knowledge-a", type="knowledge")  # duplicate id

    def tearDown(self):
        self.vault.cleanup()

    def _as(self, principal_id: str):
        return dataclasses.replace(self.config, acting_principal=principal_id)

    def test_no_principal_records_at_all_gets_full_diagnostics(self):
        # Matches every pre-Stage-1 identity's established "full access"
        # default (Config.acting_principal) — a vault that hasn't
        # bootstrapped Stage 1 yet must not suddenly lose doctor output
        # the day this gate landed.
        problems = validate.run_all(self.config)
        checks = {p.check for p in problems}
        self.assertIn("duplicate_ids", checks)

    def test_admin_principal_gets_full_diagnostics(self):
        identity.create_principal(self.config, display_name="Marcin", role="admin",
                                   principal_id="principal-marcin")
        problems = validate.run_all(self._as("principal-marcin"))
        checks = {p.check for p in problems}
        self.assertIn("duplicate_ids", checks)

    def test_non_admin_principal_gets_generic_message_only(self):
        identity.create_principal(self.config, display_name="Marcin", role="admin",
                                   principal_id="principal-marcin")
        identity.create_principal(self.config, display_name="Ania", role="adult",
                                   principal_id="principal-ania")
        problems = validate.run_all(self._as("principal-ania"))
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].check, "admin_required")
        # Never leaks the actual finding to a non-admin caller.
        self.assertNotIn("knowledge-a", problems[0].message)

    def test_disabled_admin_principal_also_denied(self):
        identity.create_principal(self.config, display_name="Marcin", role="admin",
                                   principal_id="principal-marcin")
        identity.set_principal_status(self.config, "principal-marcin", "disabled")
        problems = validate.run_all(self._as("principal-marcin"))
        self.assertEqual(problems[0].check, "admin_required")

    def test_unknown_principal_in_a_populated_vault_is_denied(self):
        identity.create_principal(self.config, display_name="Marcin", role="admin",
                                   principal_id="principal-marcin")
        problems = validate.run_all(self._as("principal-does-not-exist"))
        self.assertEqual(problems[0].check, "admin_required")


if __name__ == "__main__":
    unittest.main()
