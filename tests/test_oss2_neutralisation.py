"""OSS-2 regression coverage: the package must not carry one person's machine.

OSS-1 separated code, vault and runtime state. OSS-2 removes what was left
*inside* the code: home-directory defaults, one deployment's cloud remote, a
fixed taxonomy, a single projects root. Each stage of OSS-2 adds its cases
here.

The rule these tests defend: **a path or destination the program cannot derive
is configuration with no default at all.** An unconfigured vault must get an
explicit error or a `None`, never a fallback pointing at the author's disk.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain import agentsdoc, backup, validate
from brain.paths import (
    APP_DIRNAME,
    DEFAULT_DIRECTORIES,
    DEFAULT_NOTE_TYPES,
    SYSTEM_DIRNAME,
    Taxonomy,
    TaxonomyError,
    Config,
    Vocabulary,
    VocabularyError,
    default_config,
    default_git_dir,
    default_state_dir,
    user_config_path,
    vault_slug,
)


def _xdg(tmp: str) -> dict:
    """Point every XDG base directory at a throwaway tree."""
    return {
        "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
        "XDG_DATA_HOME": str(Path(tmp) / "data"),
        "XDG_STATE_HOME": str(Path(tmp) / "state"),
    }


class TestNoHomeDirectoryDefaults(unittest.TestCase):
    """C6 — deployment constants lose their home-directory defaults."""

    def test_git_dir_default_follows_xdg_data_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                got = default_git_dir(Path(tmp) / "my-vault")
            self.assertTrue(str(got).startswith(str(Path(tmp) / "data")), got)
            self.assertTrue(str(got).endswith(".git"), got)

    def test_git_dir_default_is_keyed_per_vault(self):
        """Two vaults sharing a directory name must not share git metadata."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                a = default_git_dir(Path(tmp) / "a" / "brain")
                b = default_git_dir(Path(tmp) / "b" / "brain")
            self.assertNotEqual(a, b)

    def test_vault_slug_keeps_the_name_legible(self):
        slug = vault_slug(Path("/nonexistent/some-vault"))
        self.assertTrue(slug.startswith("some-vault-"), slug)

    def test_config_derives_git_and_state_but_never_invents_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                config = Config(brain_root=root, projects_roots=(root / "projects",))
                self.assertEqual(config.git_dir, default_git_dir(root))
                self.assertEqual(config.state_dir, default_state_dir(root))
            # Deployment-only destinations stay unset rather than guessed.
            self.assertIsNone(config.backup_target)
            self.assertIsNone(config.backup_repo)
            self.assertEqual(config.backup_rclone_remote, "")
            self.assertEqual(config.backup_rclone_repo_path, "")

    def test_config_yaml_without_backup_keys_yields_no_backup_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            (root / "90_SYSTEM").mkdir(parents=True)
            (root / "90_SYSTEM" / "config.yaml").write_text("projects_root: /tmp/p\n")
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                config = default_config(root)
            self.assertIsNone(config.backup_repo)
            self.assertIsNone(config.backup_target)

    def test_blank_config_value_is_treated_as_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            (root / "90_SYSTEM").mkdir(parents=True)
            (root / "90_SYSTEM" / "config.yaml").write_text("backup_repo: ''\n")
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                config = default_config(root)
            self.assertIsNone(config.backup_repo)

    def test_password_file_follows_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                got = backup.default_password_file()
                self.assertEqual(got.parent, user_config_path().parent)
            self.assertTrue(str(got).startswith(str(Path(tmp) / "config")), got)


class TestProjectsRootsIsAList(unittest.TestCase):
    """C5 — one projects root is an assumption about one machine."""

    def _config_from(self, tmp: str, yaml_body: str) -> Config:
        root = Path(tmp) / "vault"
        (root / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (root / "90_SYSTEM" / "config.yaml").write_text(yaml_body)
        with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
            return default_config(root)

    def test_singular_scalar_key_still_works(self):
        """An existing single-root config must keep working untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(tmp, "projects_root: /tmp/projects\n")
            self.assertEqual(config.projects_roots, (Path("/tmp/projects"),))

    def test_singular_key_accepts_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(tmp, "projects_root:\n  - /tmp/work\n  - /tmp/personal\n")
            self.assertEqual(config.projects_roots, (Path("/tmp/work"), Path("/tmp/personal")))

    def test_plural_key_is_accepted_and_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(
                tmp, "projects_root: /tmp/ignored\nprojects_roots:\n  - /tmp/work\n")
            self.assertEqual(config.projects_roots, (Path("/tmp/work"),))

    def test_duplicates_are_collapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(tmp, "projects_roots:\n  - /tmp/w\n  - /tmp/w\n")
            self.assertEqual(config.projects_roots, (Path("/tmp/w"),))

    def test_absent_key_yields_no_roots_rather_than_a_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(tmp, "brain_root: /tmp/whatever\n")
            self.assertEqual(config.projects_roots, ())

    def test_discover_scans_every_configured_root(self):
        from brain import discover

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work" / "alpha"
            personal = Path(tmp) / "personal" / "beta"
            for d in (work, personal):
                (d / ".git").mkdir(parents=True)
            vault = Path(tmp) / "vault"
            (vault / "30_PROJECTS").mkdir(parents=True)
            (vault / "30_PROJECTS" / "_registry.yaml").write_text("projects: []\n")
            config = Config(
                brain_root=vault,
                projects_roots=(work.parent, personal.parent),
                state_dir=Path(tmp) / "state",
            )
            found = {c.name for c in discover.discover(config)}
            self.assertEqual(found, {"alpha", "beta"})


class TestVocabularyIsConfiguration(unittest.TestCase):
    """C4 — note types and status words ship as defaults, not as constants."""

    def _config_from(self, tmp: str, yaml_body: str) -> Config:
        root = Path(tmp) / "vault"
        (root / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (root / "90_SYSTEM" / "config.yaml").write_text(yaml_body)
        with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
            return default_config(root)

    def test_defaults_are_todays_values(self):
        vocab = Vocabulary()
        self.assertEqual(vocab.note_types, DEFAULT_NOTE_TYPES)
        self.assertEqual(vocab.statuses_for("decision"), ("proposed", "decided", "superseded"))
        self.assertIsNone(vocab.statuses_for("person"))

    def test_note_types_can_be_extended_by_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = "note_types:\n" + "".join(f"  - {t}\n" for t in (*DEFAULT_NOTE_TYPES, "recipe"))
            config = self._config_from(tmp, body)
            self.assertIn("recipe", config.vocabulary.note_types)

    def test_required_types_cannot_be_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(VocabularyError) as ctx:
                self._config_from(tmp, "note_types:\n  - fact\n  - person\n")
            self.assertIn("project", str(ctx.exception))

    def test_status_override_is_per_type_not_wholesale(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(tmp, "status_by_type:\n  project:\n    - live\n    - dead\n")
            self.assertEqual(config.vocabulary.statuses_for("project"), ("live", "dead"))
            # Untouched types keep their shipped vocabulary.
            self.assertEqual(config.vocabulary.statuses_for("fact"), ("current", "superseded"))

    def test_empty_status_list_means_unconstrained(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(tmp, "status_by_type:\n  fact: []\n")
            self.assertIsNone(config.vocabulary.statuses_for("fact"))

    def test_capture_rejects_a_type_outside_the_configured_vocabulary(self):
        from brain import capture

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(brain_root=root, state_dir=Path(tmp) / "state")
            with self.assertRaises(ValueError):
                capture.capture(config, "recipe", "Bigos")

    def test_capture_accepts_a_type_the_config_added(self):
        from brain import capture

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(
                brain_root=root,
                state_dir=Path(tmp) / "state",
                vocabulary=Vocabulary(note_types=(*DEFAULT_NOTE_TYPES, "recipe")),
            )
            dest = capture.capture(config, "recipe", "Bigos")
            self.assertTrue(dest.exists())
            self.assertIn("type: recipe", dest.read_text())

    def test_validate_uses_the_configured_status_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(
                brain_root=root,
                state_dir=Path(tmp) / "state",
                vocabulary=Vocabulary(status_vocabularies=(("project", ("live",)),)),
            )
            notes = [_FakeNote(type="project", status="active"),
                     _FakeNote(type="project", status="live")]
            problems = validate.check_invalid_status(config, notes)
            self.assertEqual(len(problems), 1)
            self.assertIn("'active'", problems[0].message)

    def test_argparse_does_not_freeze_the_vocabulary(self):
        """A vault's own note type must not be rejected before config is read."""
        from brain import cli

        parser = cli.build_parser()
        args = parser.parse_args(["remember", "--type", "recipe", "--title", "Bigos"])
        self.assertEqual(args.type, "recipe")


class _FakeNote:
    def __init__(self, type: str, status: str):
        self.type = type
        self.status = status
        self.path = Path("/nonexistent/note.md")


class TestTaxonomyIsARoleMap(unittest.TestCase):
    """C3 — code depends on roles; only the taxonomy knows directory names."""

    def _config_from(self, tmp: str, yaml_body: str) -> Config:
        root = Path(tmp) / "vault"
        (root / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (root / "90_SYSTEM" / "config.yaml").write_text(yaml_body)
        with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
            return default_config(root)

    def test_shipped_default_is_the_numbered_layout(self):
        self.assertEqual(
            Taxonomy().content_dirs(),
            ("00_INBOX", "10_PEOPLE", "20_AREAS", "30_PROJECTS",
             "40_DECISIONS", "50_TIMELINE", "60_KNOWLEDGE", "70_DOCUMENTS"),
        )

    def test_a_role_can_be_remapped_without_forking(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(tmp, "directories:\n  inbox: Inbox\n  people: People\n")
            self.assertEqual(config.inbox_dir.name, "Inbox")
            self.assertIn("People", config.content_dirs)
            # Unremapped roles keep the shipped names.
            self.assertIn("40_DECISIONS", config.content_dirs)

    def test_remapping_moves_the_registry_and_templates_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(
                tmp, "directories:\n  projects: Projects\n  templates: Templates\n")
            self.assertEqual(config.registry_path, config.brain_root / "Projects" / "_registry.yaml")
            self.assertEqual(config.templates_dir, config.brain_root / "Templates")

    def test_unknown_role_is_rejected_rather_than_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TaxonomyError):
                self._config_from(tmp, "directories:\n  recipes: Recipes\n")

    def test_two_roles_cannot_share_a_directory(self):
        with self.assertRaises(TaxonomyError):
            Taxonomy(directories=tuple({**DEFAULT_DIRECTORIES, "inbox": "10_PEOPLE"}.items()))

    def test_the_vault_marker_directory_cannot_be_a_content_role(self):
        with self.assertRaises(TaxonomyError):
            Taxonomy(directories=tuple({**DEFAULT_DIRECTORIES, "inbox": SYSTEM_DIRNAME}.items()))

    def test_system_dir_stays_fixed_because_it_is_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(tmp, "directories:\n  inbox: Inbox\n")
            self.assertEqual(config.system_dir.name, SYSTEM_DIRNAME)

    def test_project_status_folders_are_configurable_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config_from(
                tmp, "project_status_folders:\n  active: Live\n")
            self.assertEqual(config.taxonomy.folder_for_status("active"), "Live")
            self.assertEqual(config.taxonomy.folder_for_status("completed"), "ARCHIVED")

    def test_unknown_status_falls_back_to_the_archive_bucket(self):
        self.assertEqual(Taxonomy().folder_for_status("no-such-status"), "ARCHIVED")

    def test_capture_writes_into_the_remapped_inbox(self):
        from brain import capture

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(
                brain_root=root,
                state_dir=Path(tmp) / "state",
                taxonomy=Taxonomy(directories=tuple({**DEFAULT_DIRECTORIES, "inbox": "Inbox"}.items())),
            )
            dest = capture.capture(config, "fact", "A fact")
            self.assertEqual(dest.parent, root / "Inbox")

    def test_memory_queue_follows_the_remapped_inbox(self):
        from brain import memoryqueue

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(
                brain_root=root,
                state_dir=Path(tmp) / "state",
                taxonomy=Taxonomy(directories=tuple({**DEFAULT_DIRECTORIES, "inbox": "Inbox"}.items())),
            )
            self.assertEqual(
                memoryqueue._queue_path(config), root / "Inbox" / "memory" / "pending.yaml")

    def test_indexer_reads_the_remapped_content_dirs(self):
        from brain import indexer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            (root / "Knowledge").mkdir(parents=True)
            (root / "Knowledge" / "n.md").write_text(
                "---\nid: knowledge-n\ntype: knowledge\nstatus: current\n"
                "created: 2026-01-01\nupdated: 2026-01-01\naliases: []\n---\n\n# N\n")
            config = Config(
                brain_root=root,
                state_dir=Path(tmp) / "state",
                taxonomy=Taxonomy(
                    directories=tuple({**DEFAULT_DIRECTORIES, "knowledge": "Knowledge"}.items())),
            )
            self.assertEqual(indexer.rebuild(config)["indexed"], 1)


class TestIdentityHasOneSite(unittest.TestCase):
    """C8 — the naming pass. The tool's name appears in the package once.

    The public name is unchanged ("Sako Brain"), so nothing on disk was
    renamed — unit names, the MCP server name, the backup tag, the XDG
    directories and the credential path all keep the exact strings the live
    deployment already uses. What changed is that they now all derive from one
    constant, so a future rename is one edit instead of a hunt.
    """

    def test_the_slug_is_not_repeated_anywhere_in_the_package(self):
        package_dir = Path(validate.__file__).parent
        offenders = []
        for module in sorted(package_dir.glob("*.py")):
            for lineno, line in enumerate(module.read_text().splitlines(), 1):
                if APP_DIRNAME not in line:
                    continue
                # paths.py is the one site allowed to spell it.
                if module.name == "paths.py":
                    continue
                offenders.append(f"{module.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "hardcoded name outside paths.py:\n" + "\n".join(offenders))

    def test_the_shipped_identity_matches_what_is_deployed(self):
        """The names on this machine must not change as a side effect of C8."""
        from brain import backup, mcp_server, systemdstatus

        self.assertEqual(APP_DIRNAME, "sako-brain")
        self.assertEqual(systemdstatus.BACKUP_TIMER, "sako-brain-backup.timer")
        self.assertEqual(systemdstatus.MAINTENANCE_TIMER, "sako-brain-maintenance.timer")
        self.assertEqual(mcp_server.SERVER_NAME, "sako-brain")
        self.assertEqual(backup.BACKUP_TAG, "sako-brain")
        self.assertEqual(user_config_path().name, "config.yaml")
        self.assertEqual(user_config_path().parent.name, "sako-brain")

    def test_display_and_component_names_are_distinct(self):
        from brain import paths as paths_mod

        self.assertEqual(paths_mod.APP_NAME, "Sako Brain")
        self.assertEqual(paths_mod.APP_CORE_NAME, "Sako Brain Core")

    def test_derived_paths_follow_the_slug(self):
        from brain import paths as paths_mod

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                state = default_state_dir(Path(tmp) / "v")
                git = default_git_dir(Path(tmp) / "v")
            self.assertIn(paths_mod.APP_DIRNAME, str(state))
            self.assertIn(paths_mod.APP_DIRNAME, str(git))


class TestPackageCarriesNoPersonalPaths(unittest.TestCase):
    """The headline OSS-2 guard: no home directory anywhere in the package.

    A single grep-equivalent assertion over every shipped module, so a future
    change reintroducing `/home/<someone>/...` fails a test rather than
    reaching a public repository.
    """

    def test_no_absolute_home_paths_in_any_module(self):
        package_dir = Path(validate.__file__).parent
        offenders = []
        for module in sorted(package_dir.glob("*.py")):
            for lineno, line in enumerate(module.read_text().splitlines(), 1):
                if "/home/" in line or "/Users/" in line:
                    offenders.append(f"{module.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "personal absolute paths in the package:\n" + "\n".join(offenders))


class TestAgentsDocIsGenerated(unittest.TestCase):
    """C7 — AGENTS.md is generated from a template, not hand-maintained per vault."""

    def _config(self, tmp: str, **kwargs) -> Config:
        root = Path(tmp) / "vault"
        root.mkdir(parents=True, exist_ok=True)
        kwargs.setdefault("state_dir", Path(tmp) / "state")
        return Config(brain_root=root, **kwargs)

    def test_the_template_itself_carries_no_personal_data(self):
        """Scans the template's TEXT, as loaded from the package (OSS-4), so
        this gate covers the installed artefact rather than a source path."""
        offenders = [f"AGENTS.md.template:{n}: {line.strip()}"
                     for n, line in enumerate(agentsdoc.template_text().splitlines(), 1)
                     for marker in PERSONAL_MARKERS if marker in line]
        self.assertEqual(offenders, [], "personal data in the AGENTS.md template:\n" + "\n".join(offenders))

    def test_render_leaves_no_unresolved_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotIn("{{", agentsdoc.render(self._config(tmp)))

    def test_vault_name_defaults_to_the_directory_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            self.assertEqual(config.vault_name, "vault")
            self.assertIn("# AGENTS.md — vault (provider-neutral)", agentsdoc.render(config))

    def test_configured_vault_name_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            rendered = agentsdoc.render(self._config(tmp, configured_vault_name="Second Brain"))
            self.assertIn("# AGENTS.md — Second Brain (provider-neutral)", rendered)

    def test_a_remapped_taxonomy_produces_a_correct_directory_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, taxonomy=Taxonomy(
                directories=tuple({**DEFAULT_DIRECTORIES, "inbox": "Inbox", "timeline": "Log"}.items())))
            rendered = agentsdoc.render(config)
            self.assertIn("| `Inbox/` |", rendered)
            self.assertIn("| `Log/` |", rendered)
            self.assertNotIn("00_INBOX", rendered)
            self.assertIn("`Inbox/memory/pending.yaml`", rendered)
            self.assertIn("Create a `Log/` entry", rendered)

    def test_an_extended_vocabulary_reaches_the_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, vocabulary=Vocabulary(
                note_types=(*DEFAULT_NOTE_TYPES, "recipe")))
            self.assertIn("|recipe", agentsdoc.render(config))

    def test_areas_are_listed_only_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("| Ongoing life areas |", agentsdoc.render(self._config(tmp)))
            with_areas = agentsdoc.render(self._config(tmp, areas=("Property", "Work")))
            self.assertIn("Ongoing life areas (Property, Work)", with_areas)

    def test_backup_line_is_omitted_when_no_backup_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotIn("Backup only", agentsdoc.render(self._config(tmp)))
            with_backup = agentsdoc.render(self._config(tmp, backup_target=Path("/mnt/mirror")))
            self.assertIn("- Backup only, never authoritative, never sync source: `/mnt/mirror/`.", with_backup)

    def test_several_projects_roots_are_all_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            rendered = agentsdoc.render(
                self._config(tmp, projects_roots=(Path("/w/work"), Path("/w/personal"))))
            self.assertIn("`/w/work/`, `/w/personal/`", rendered)

    def test_no_projects_root_still_renders_a_sensible_sentence(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("the configured projects roots (see `brain status`)",
                          agentsdoc.render(self._config(tmp)))

    def test_the_archive_bucket_status_is_not_described_as_collapsed(self):
        """`archived` names the ARCHIVED folder; it owns it rather than losing one."""
        with tempfile.TemporaryDirectory() as tmp:
            rendered = agentsdoc.render(self._config(tmp))
            self.assertIn("— `completed`/`abandoned`/`unknown` all physically live under", rendered)

    def test_this_vaults_agents_md_matches_what_the_template_renders(self):
        """Drift guard: a hand-edit to AGENTS.md that the template can't produce.

        Skipped in a checkout with no AGENTS.md or no vault config — the point
        is to keep a real vault's document and its generator in step.
        """
        agents_md = _REPO_ROOT / "AGENTS.md"
        if not agents_md.is_file() or not (_REPO_ROOT / SYSTEM_DIRNAME / "config.yaml").is_file():
            self.skipTest("no vault AGENTS.md in this checkout")
        rendered = agentsdoc.render(default_config(_REPO_ROOT))
        self.assertEqual(
            rendered, agents_md.read_text(encoding="utf-8"),
            "AGENTS.md and the template have diverged — reconcile them, then "
            "`brain agents-doc --write --force`",
        )

    def test_write_refuses_to_clobber_without_force(self):
        from brain import cli

        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            dest = agentsdoc.output_path(config)
            dest.write_text("hand-written\n")
            args = cli.build_parser().parse_args(["agents-doc", "--write"])
            self.assertEqual(cli.cmd_agents_doc(config, args), 1)
            self.assertEqual(dest.read_text(), "hand-written\n")

            forced = cli.build_parser().parse_args(["agents-doc", "--write", "--force"])
            self.assertEqual(cli.cmd_agents_doc(config, forced), 0)
            self.assertIn("AGENTS.md", dest.read_text())


class TestDoctorGuardsStateDirPlacement(unittest.TestCase):
    """The OSS-1 recommendation: tests guard the code, nothing guarded the user.

    `state_dir` is deliberately honoured when configured (unlike `brain_root`),
    because pointing a recovery copy at a chosen state directory is legitimate.
    That makes misconfiguring it back inside the vault possible, and it
    silently undoes both reasons OSS-1 moved state out.
    """

    def test_state_dir_inside_the_vault_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(brain_root=root, state_dir=root / "90_SYSTEM" / "state")
            problems = validate.check_state_dir_outside_vault(config)
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0].check, "state_dir_inside_vault")

    def test_state_dir_equal_to_the_vault_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(brain_root=root, state_dir=root)
            self.assertEqual(len(validate.check_state_dir_outside_vault(config)), 1)

    def test_state_dir_outside_the_vault_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(brain_root=root, state_dir=Path(tmp) / "state")
            self.assertEqual(validate.check_state_dir_outside_vault(config), [])

    def test_the_xdg_default_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                config = Config(brain_root=root)
            self.assertEqual(validate.check_state_dir_outside_vault(config), [])

    def test_the_check_runs_as_part_of_brain_doctor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            (root / "30_PROJECTS").mkdir(parents=True)
            (root / "30_PROJECTS" / "_registry.yaml").write_text("projects: []\n")
            config = Config(brain_root=root, state_dir=root / "state")
            checks = {p.check for p in validate.run_all(config)}
            self.assertIn("state_dir_inside_vault", checks)


# Strings that must not appear in anything shippable.
#
# Two tiers, because the list itself is evidence (PRR-2). Naming a maintainer's
# vault directory, cloud remote and account name inside a *public* test file
# would publish exactly what the gate exists to keep private — the list would
# become the leak.
#
#   GENERIC_MARKERS   ship with the project. They are the ones a contributor
#                     benefits from: no absolute home directory belongs in a
#                     package, on anyone's machine.
#   private markers   are supplied by the environment and never committed
#                     here. A deployment that has them gets a strictly
#                     stronger gate; a public checkout gets the generic one
#                     and says so.
#
# Supply them with SAKO_BRAIN_PRIVATE_MARKERS=<file>, or by placing
# `private-markers.txt` beside this directory — one marker per line, `#` for
# comments. That file must never be committed to a public repository.
GENERIC_MARKERS = ("/home/", "/Users/", "/root/")


def _load_private_markers() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Deployment-supplied markers, if this environment has any.

    Returns (identifiers, attribution). A `name:` prefix declares attribution;
    everything else is a hard leak wherever it appears. Declared rather than
    guessed on purpose — "alphabetic means it is a name" would classify a cloud
    remote or an account name as attribution and open a hole in the gate.
    """
    candidates = []
    env = os.environ.get("SAKO_BRAIN_PRIVATE_MARKERS")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parent.parent / "private-markers.txt")
    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        ids, names = [], []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            (names if line.startswith("name:") else ids).append(
                line[5:].strip() if line.startswith("name:") else line)
        return tuple(ids), tuple(names)
    return (), ()


PRIVATE_MARKERS, ATTRIBUTION_MARKERS = _load_private_markers()
PERSONAL_MARKERS = GENERIC_MARKERS + PRIVATE_MARKERS + ATTRIBUTION_MARKERS

# Two kinds of marker, split here because OSS-4 needs the distinction and this
# module is the one place allowed to spell any of them out.
#
# A person's NAME is intended in one specific shipped place: the `Author:` line
# of package metadata. The open-source standard this project follows requires
# authorship/origin, and a package with no author is worse, not safer.
#
# A person's PATHS, accounts and private artefact names are never intended
# anywhere — not in code, not in fixtures, not in metadata.
# Everything that is never permitted anywhere: paths, accounts, remotes.
# ATTRIBUTION_MARKERS (declared above) are the narrow exception.
IDENTIFIER_MARKERS = GENERIC_MARKERS + PRIVATE_MARKERS

# Two scopes, because a marker that is right for one is wrong for the other.
#
#   PERSONAL_MARKERS    scanning SOURCE and ARTEFACTS. `/home/` belongs here:
#                       no absolute home path belongs in a package, ever.
#   DEPLOYMENT_MARKERS  scanning a command's RUNTIME OUTPUT. `/home/` must NOT
#                       be here — a test's own throwaway sandbox can sit under
#                       a home directory, and asserting otherwise fails for a
#                       contributor whose TMPDIR does. What matters in output
#                       is that THIS deployment's identifiers never appear.
#
# In a public checkout with no private markers file, DEPLOYMENT_MARKERS is
# empty and those assertions become vacuous — which is honest: a machine with
# no private identifiers has none to leak.
DEPLOYMENT_MARKERS = PRIVATE_MARKERS + ATTRIBUTION_MARKERS

# The complete set of line shapes on which the author's name is intended. Kept
# here, with the vocabulary, so every gate shares one definition instead of
# three drifting copies — and kept narrow: a line must LOOK like attribution,
# not merely contain the name.
#
#   Author: / Author-Email:      package metadata
#   authors = [...]              pyproject.toml
#   Copyright (C) <year> <name>  a copyright notice, wherever it appears
#
# The README carries a copyright notice and is embedded in package metadata as
# the long description, which is why the third form is needed at all.
_ATTRIBUTION_LINE_RE = re.compile(
    r"^\s*(?:Author[\w-]*\s*:|authors\s*=|Copyright\s*\(C\)\s*\d{4})")


def is_attribution_line(line: str) -> bool:
    """True when this line is an intended attribution, not a leak."""
    return bool(_ATTRIBUTION_LINE_RE.match(line))

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _scan(paths) -> list[str]:
    offenders = []
    for path in paths:
        if path.resolve() == Path(__file__).resolve():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for marker in PERSONAL_MARKERS:
                if marker in line:
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
                    break
    return offenders


class TestMarkerVocabulary(unittest.TestCase):
    """The gate's own configuration is load-bearing, so it is tested (PRR-2).

    The list of private identifiers is itself private data. Publishing a test
    file that spells out a maintainer's vault name, cloud remote and account
    name would make the gate the leak. So the shipped set is generic and the
    deployment-specific set is supplied by the environment.
    """

    def test_the_shipped_set_names_nobody(self):
        """Generic markers must be about SHAPE, not about a person."""
        for marker in GENERIC_MARKERS:
            self.assertTrue(marker.startswith("/") and marker.endswith("/"),
                            f"{marker!r} is not a generic path shape")

    def test_this_deployment_supplies_its_own_markers(self):
        """On the private vault the file exists, so the gate is the stronger one."""
        if not PRIVATE_MARKERS and not ATTRIBUTION_MARKERS:
            self.skipTest("no private markers file — public-checkout mode")
        self.assertTrue(PRIVATE_MARKERS, "markers file present but no identifiers")

    def test_attribution_is_declared_not_guessed(self):
        """A heuristic would classify a cloud remote or account name as a name."""
        for marker in ATTRIBUTION_MARKERS:
            self.assertNotIn(marker, PRIVATE_MARKERS,
                             f"{marker!r} is both attribution and identifier")

    def test_output_scope_excludes_generic_path_markers(self):
        """`/home/` in command output may be the test's own sandbox."""
        for marker in GENERIC_MARKERS:
            self.assertNotIn(marker, DEPLOYMENT_MARKERS)

    def test_source_scope_includes_everything(self):
        for marker in GENERIC_MARKERS + PRIVATE_MARKERS + ATTRIBUTION_MARKERS:
            self.assertIn(marker, PERSONAL_MARKERS)

    def test_no_private_marker_is_written_into_this_file(self):
        """The point of the whole arrangement, asserted directly."""
        source = Path(__file__).read_text()
        head = source.split("class TestMarkerVocabulary")[0]
        for marker in PRIVATE_MARKERS + ATTRIBUTION_MARKERS:
            self.assertNotIn(marker, head,
                             f"{marker!r} is spelled out in a shipped test file")


class TestFixturesAndSkillsCarryNoPersonalData(unittest.TestCase):
    """Sanitisation: the test suite and the agent rules ship, so they must be clean.

    The OSS-0 privacy scan flagged test fixtures carrying real personal data and
    skills naming one person's home directory and given name. Both are part of
    what a public release distributes, so both are guarded here rather than
    left to a reviewer's eye.
    """

    def test_no_personal_data_in_test_fixtures(self):
        offenders = _scan(sorted(Path(__file__).resolve().parent.glob("*.py")))
        self.assertEqual(offenders, [], "personal data in test fixtures:\n" + "\n".join(offenders))

    def test_no_personal_data_in_agent_skills(self):
        skills_dir = _REPO_ROOT / ".claude" / "skills"
        if not skills_dir.is_dir():
            self.skipTest("no .claude/skills/ in this checkout")
        offenders = _scan(sorted(skills_dir.rglob("*.md")))
        self.assertEqual(offenders, [], "personal data in agent skills:\n" + "\n".join(offenders))


class TestBackupDestinationIsConfigured(unittest.TestCase):
    """C6 — the restic destination is deployment identity, not a package default."""

    def test_rclone_backend_refuses_without_a_configured_remote(self):
        settings = backup.BackupSettings(backend="rclone")
        with self.assertRaises(backup.BackupError) as ctx:
            settings.repository()
        self.assertIn("backup_rclone_remote", str(ctx.exception))

    def test_local_backend_refuses_without_a_configured_path(self):
        settings = backup.BackupSettings(backend="local")
        with self.assertRaises(backup.BackupError) as ctx:
            settings.repository()
        self.assertIn("backup_repo", str(ctx.exception))

    def test_configured_destination_is_carried_through_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            (root / "90_SYSTEM").mkdir(parents=True)
            (root / "90_SYSTEM" / "config.yaml").write_text(
                "backup_rclone_remote: SOME_REMOTE\n"
                "backup_rclone_repo_path: some/path\n"
            )
            with mock.patch.dict(os.environ, _xdg(tmp), clear=False):
                settings = backup.default_settings(default_config(root))
            self.assertEqual(settings.repository(), "rclone:SOME_REMOTE:some/path")

    def test_no_author_specific_string_survives_in_the_backup_module(self):
        source = Path(backup.__file__).read_text()
        for leaked in PERSONAL_MARKERS:
            self.assertNotIn(leaked, source)


class TestValidateToleratesUnconfiguredBackup(unittest.TestCase):
    """A vault with no backup configured must still validate, not crash."""

    def _config(self, tmp: str) -> Config:
        root = Path(tmp) / "vault"
        root.mkdir(parents=True, exist_ok=True)
        return Config(brain_root=root, projects_roots=(root / "projects",))

    def test_backup_not_canonical_check_skips_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(validate.check_backup_not_canonical(self._config(tmp)), [])

    def test_unsafe_plaintext_backup_check_skips_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(validate.check_unsafe_plaintext_backup(self._config(tmp)), [])


if __name__ == "__main__":
    unittest.main()
