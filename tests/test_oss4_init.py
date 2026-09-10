"""OSS-4.2: `brain init` — the positive first-run path.

OSS-2 removed every private default, which left a new user with correct but
unhelpful messages about missing configuration. These tests hold the contract
that replaced it: safe, explicit, idempotent, and ending in a vault that
`brain doctor` calls healthy.

The rule everything else serves: **no file this command did not create is ever
written to.**
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from brain import init as init_mod
from brain import validate
from brain.paths import CONTENT_ROLES, SYSTEM_DIRNAME, default_config


from tests.helpers import requires_git

class InitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def vault(self, name="vault") -> Path:
        return self.base / name


class TestMinimalInit(InitTestCase):
    def test_creates_a_vault_the_core_recognises(self):
        target = self.vault()
        result = init_mod.initialise(target)
        self.assertEqual(result.vault, target.resolve())
        self.assertTrue((target / SYSTEM_DIRNAME / "config.yaml").is_file())
        self.assertFalse(result.already_initialised)

    def test_creates_every_content_directory(self):
        target = self.vault()
        init_mod.initialise(target)
        config = default_config(target)
        for role in CONTENT_ROLES:
            self.assertTrue(config.dir_for(role).is_dir(), f"missing role {role}")

    def test_the_new_vault_passes_doctor(self):
        target = self.vault()
        init_mod.initialise(target)
        self.assertEqual(validate.run_all(default_config(target)), [])

    def test_does_not_generate_agents_md_by_default(self):
        """0.2.0: agent rules are opt-in. A plain init writes no AGENTS.md."""
        target = self.vault()
        init_mod.initialise(target)
        self.assertFalse((target / "AGENTS.md").exists())

    def test_opting_in_still_renders_a_complete_agents_md(self):
        """The capability is unchanged — only the default moved."""
        target = self.vault()
        init_mod.initialise(target, with_agents=True)
        agents = target / "AGENTS.md"
        self.assertTrue(agents.is_file())
        text = agents.read_text()
        self.assertIn("# AGENTS.md — vault", text)
        self.assertNotIn("{{", text)

    def test_no_agents_still_produces_a_valid_vault(self):
        """AGENTS.md is not a vault-recognition requirement."""
        target = self.vault()
        init_mod.initialise(target, with_agents=False)
        self.assertFalse((target / "AGENTS.md").exists())
        self.assertTrue((target / SYSTEM_DIRNAME / "config.yaml").is_file())
        self.assertEqual(validate.run_all(default_config(target)), [])

    def test_creates_no_state_or_git_directory(self):
        """Both are derived and made on demand; init inventing them would be
        creating state before there is anything to store."""
        target = self.vault()
        init_mod.initialise(target)
        config = default_config(target)
        self.assertFalse(config.state_dir.exists())
        self.assertFalse(config.git_dir.exists())

    def test_writes_no_backup_configuration(self):
        target = self.vault()
        init_mod.initialise(target)
        config = default_config(target)
        self.assertIsNone(config.backup_repo)
        self.assertIsNone(config.backup_target)
        self.assertEqual(config.backup_rclone_remote, "")

    def test_creates_no_credential_file_anywhere(self):
        target = self.vault()
        init_mod.initialise(target)
        for path in target.rglob("*"):
            self.assertNotIn("password", path.name.lower())
            self.assertNotIn("secret", path.name.lower())

    def test_seeds_no_example_content(self):
        """A real user's first vault must not be pre-filled with fiction."""
        target = self.vault()
        init_mod.initialise(target)
        notes = [p for p in target.rglob("*.md") if p.name != "AGENTS.md"]
        self.assertEqual(notes, [])

    def test_does_not_initialise_git_by_default(self):
        target = self.vault()
        init_mod.initialise(target)
        self.assertFalse((target / ".git").exists())

    @requires_git
    def test_git_is_created_only_when_asked_and_lands_outside_the_vault(self):
        target = self.vault()
        init_mod.initialise(target, with_git=True)
        config = default_config(target)
        self.assertTrue((config.git_dir / "HEAD").is_file())
        self.assertFalse(str(config.git_dir).startswith(str(target.resolve())))


class TestConfigBootstrap(InitTestCase):
    def test_config_is_commented_and_sets_only_safe_values(self):
        import yaml

        target = self.vault()
        init_mod.initialise(target)
        raw = (target / SYSTEM_DIRNAME / "config.yaml").read_text()
        self.assertIn("# ", raw)
        data = yaml.safe_load(raw)
        self.assertEqual(set(data), {"vault_name"},
                         f"init set more than vault_name: {sorted(data)}")

    def test_config_documents_the_optional_keys_without_enabling_them(self):
        target = self.vault()
        init_mod.initialise(target)
        raw = (target / SYSTEM_DIRNAME / "config.yaml").read_text()
        for key in ("projects_root", "state_dir", "git_dir", "directories",
                    "note_types", "backup_repo", "backup_rclone_remote"):
            self.assertIn(f"# {key}", raw, f"{key} not documented")

    def test_no_private_default_is_reintroduced(self):
        from tests.test_oss2_neutralisation import IDENTIFIER_MARKERS

        target = self.vault()
        init_mod.initialise(target)
        raw = (target / SYSTEM_DIRNAME / "config.yaml").read_text()
        for marker in IDENTIFIER_MARKERS:
            self.assertNotIn(marker, raw)

    def test_projects_roots_starts_unset_rather_than_guessed(self):
        target = self.vault()
        init_mod.initialise(target)
        self.assertEqual(default_config(target).projects_roots, ())


class TestSafety(InitTestCase):
    def test_refuses_an_existing_vault(self):
        target = self.vault()
        init_mod.initialise(target)
        with self.assertRaises(init_mod.InitError) as ctx:
            init_mod.initialise(target)
        self.assertIn("already a vault", str(ctx.exception))

    def test_refusal_changes_nothing(self):
        target = self.vault()
        init_mod.initialise(target)
        config_path = target / SYSTEM_DIRNAME / "config.yaml"
        before = config_path.read_text()
        with self.assertRaises(init_mod.InitError):
            init_mod.initialise(target)
        self.assertEqual(config_path.read_text(), before)

    def test_force_never_overwrites_an_existing_config(self):
        target = self.vault()
        init_mod.initialise(target)
        config_path = target / SYSTEM_DIRNAME / "config.yaml"
        config_path.write_text("vault_name: Edited By Hand\n")
        init_mod.initialise(target, force=True)
        self.assertEqual(config_path.read_text(), "vault_name: Edited By Hand\n")

    def test_force_never_overwrites_an_existing_agents_file(self):
        target = self.vault()
        init_mod.initialise(target, with_agents=True)
        agents = target / "AGENTS.md"
        agents.write_text("hand-written\n")
        init_mod.initialise(target, with_agents=True, force=True)
        self.assertEqual(agents.read_text(), "hand-written\n")

    def test_force_fills_in_only_what_is_missing(self):
        target = self.vault()
        init_mod.initialise(target)
        config = default_config(target)
        removed = config.dir_for("timeline")
        removed.rmdir()
        result = init_mod.initialise(target, force=True)
        self.assertTrue(removed.is_dir())
        self.assertEqual(result.created, [f"{removed.name}/"])

    def test_never_touches_existing_notes(self):
        target = self.vault()
        init_mod.initialise(target)
        config = default_config(target)
        note = config.dir_for("knowledge") / "mine.md"
        note.write_text("my note\n")
        init_mod.initialise(target, force=True)
        self.assertEqual(note.read_text(), "my note\n")

    def test_adopts_a_non_empty_directory_without_disturbing_it(self):
        """It may be an existing notes folder someone is adopting."""
        target = self.vault()
        target.mkdir()
        stray = target / "existing.md"
        stray.write_text("pre-existing\n")
        init_mod.initialise(target)
        self.assertEqual(stray.read_text(), "pre-existing\n")
        self.assertTrue((target / SYSTEM_DIRNAME / "config.yaml").is_file())

    def test_refuses_a_path_that_is_a_file(self):
        target = self.vault("afile")
        target.write_text("not a directory\n")
        with self.assertRaises(init_mod.InitError):
            init_mod.initialise(target)

    def test_refuses_when_the_parent_does_not_exist(self):
        with self.assertRaises(init_mod.InitError) as ctx:
            init_mod.initialise(self.base / "no" / "such" / "parent" / "vault")
        self.assertIn("parent directory does not exist", str(ctx.exception))

    def test_refusal_leaves_nothing_on_disk(self):
        target = self.base / "no" / "such" / "parent" / "vault"
        with self.assertRaises(init_mod.InitError):
            init_mod.initialise(target)
        self.assertFalse(target.exists())
        self.assertFalse((self.base / "no").exists())


class TestIdempotency(InitTestCase):
    def test_forced_rerun_creates_nothing_and_reports_everything_kept(self):
        target = self.vault()
        init_mod.initialise(target)
        result = init_mod.initialise(target, force=True)
        self.assertTrue(result.already_initialised)
        self.assertEqual(result.created, [])
        self.assertIn(f"{SYSTEM_DIRNAME}/", result.skipped)

    def test_repeated_runs_leave_an_identical_vault(self):
        target = self.vault()
        init_mod.initialise(target)
        before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))
        contents = {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}
        for _ in range(3):
            init_mod.initialise(target, force=True)
        after = sorted(p.relative_to(target).as_posix() for p in target.rglob("*"))
        self.assertEqual(before, after)
        for path, content in contents.items():
            self.assertEqual(path.read_bytes(), content)

    def test_forced_rerun_respects_a_remapped_taxonomy(self):
        """A --force run must create the user's directories, not the shipped ones."""
        target = self.vault()
        init_mod.initialise(target)
        (target / SYSTEM_DIRNAME / "config.yaml").write_text(
            "vault_name: Remapped\ndirectories:\n  knowledge: library\n")
        init_mod.initialise(target, force=True)
        self.assertTrue((target / "library").is_dir())


@unittest.skipUnless(os.name == "posix", "POSIX mode bits only")
class TestPermissions(InitTestCase):
    def test_new_vault_root_is_owner_only(self):
        target = self.vault()
        init_mod.initialise(target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), init_mod.VAULT_MODE)

    def test_config_is_owner_only(self):
        target = self.vault()
        init_mod.initialise(target)
        config_path = target / SYSTEM_DIRNAME / "config.yaml"
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), init_mod.CONFIG_MODE)

    def test_an_adopted_directory_keeps_its_own_permissions(self):
        """Init must not silently tighten a directory the user already had."""
        target = self.vault()
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        init_mod.initialise(target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)


class TestDemoInit(InitTestCase):
    """OSS-4.3 — a thin wrapper over the OSS-3 generator, never a second one."""

    def test_creates_the_synthetic_example_brain(self):
        from brain import demo

        target = self.vault("example")
        result = init_mod.initialise_demo(target)
        self.assertEqual(result.vault, target.resolve())
        self.assertEqual(default_config(target).vault_name, demo.DEMO_VAULT_NAME)

    def test_the_demo_vault_is_clean_end_to_end(self):
        from brain import indexer

        target = self.vault("example")
        init_mod.initialise_demo(target)
        config = default_config(target)
        self.assertEqual(validate.run_all(config), [])
        self.assertEqual(indexer.rebuild(config)["indexed"], len(_seeded_ids()))

    def test_every_seeded_note_is_present(self):
        target = self.vault("example")
        init_mod.initialise_demo(target)
        config = default_config(target)
        found = {p.stem for p in config.brain_root.rglob("*.md")}
        for note_id in _seeded_ids():
            self.assertIn(note_id, found)

    def test_it_uses_the_generator_rather_than_reimplementing_it(self):
        """The demo taxonomy and vocabulary must come from `demo`, not from a
        copy that could drift out of step with it."""
        from brain import demo

        target = self.vault("example")
        init_mod.initialise_demo(target)
        config = default_config(target)
        self.assertEqual(dict(config.taxonomy.directories), demo.DEMO_DIRECTORIES)
        self.assertEqual(config.vocabulary.note_types, demo.DEMO_NOTE_TYPES)

    def test_generates_agents_reflecting_the_synthetic_taxonomy(self):
        target = self.vault("example")
        init_mod.initialise_demo(target, with_agents=True)
        text = (target / "AGENTS.md").read_text()
        self.assertIn("Example Brain", text)
        self.assertIn("`work/`", text)
        self.assertNotIn("30_PROJECTS", text)

    def test_no_agents_is_honoured_for_the_demo_too(self):
        target = self.vault("example")
        init_mod.initialise_demo(target, with_agents=False)
        self.assertFalse((target / "AGENTS.md").exists())
        self.assertEqual(validate.run_all(default_config(target)), [])

    def test_the_demo_writes_no_agents_md_by_default_either(self):
        target = self.vault("example")
        init_mod.initialise_demo(target)
        self.assertFalse((target / "AGENTS.md").exists())
        self.assertEqual(validate.run_all(default_config(target)), [])

    def test_supporting_directories_land_outside_the_vault(self):
        target = self.vault("example")
        init_mod.initialise_demo(target)
        config = default_config(target)
        for path in (config.state_dir, config.git_dir, *config.projects_roots):
            self.assertFalse(str(path).startswith(str(target.resolve()) + "/"))

    def test_refuses_a_non_empty_target(self):
        target = self.vault("example")
        target.mkdir()
        (target / "mine.md").write_text("existing\n")
        with self.assertRaises(init_mod.InitError) as ctx:
            init_mod.initialise_demo(target)
        self.assertIn("not empty", str(ctx.exception))
        self.assertEqual((target / "mine.md").read_text(), "existing\n")

    def test_rerun_is_refused_rather_than_silently_overwriting(self):
        target = self.vault("example")
        init_mod.initialise_demo(target)
        with self.assertRaises(init_mod.InitError):
            init_mod.initialise_demo(target)

    def test_carries_no_production_identifier(self):
        from tests.test_oss3_portability import PRODUCTION_MARKERS

        target = self.vault("example")
        init_mod.initialise_demo(target)
        offenders = []
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(errors="ignore")
            offenders += [f"{path.name}: {m}" for m in PRODUCTION_MARKERS if m in text]
        self.assertEqual(offenders, [])

    def test_writes_no_credential(self):
        target = self.vault("example")
        init_mod.initialise_demo(target)
        config = default_config(target)
        self.assertIsNone(config.backup_repo)
        for path in target.rglob("*"):
            self.assertNotIn("password", path.name.lower())

    def test_is_deterministic(self):
        """Same input, same output — byte for byte.

        Compared at the same target path, because a demo built somewhere else
        legitimately differs: project notes carry the absolute path of their
        synthetic working directory, which is a property of where you put it,
        not of the generator.
        """
        import shutil

        target = self.vault("example")
        init_mod.initialise_demo(target, with_agents=False)
        first = {p.relative_to(target).as_posix(): p.read_bytes()
                 for p in sorted(target.rglob("*")) if p.is_file()}

        shutil.rmtree(target)
        shutil.rmtree(target.parent / f"{target.name}-projects", ignore_errors=True)
        shutil.rmtree(target.parent / f"{target.name}-state", ignore_errors=True)

        init_mod.initialise_demo(target, with_agents=False)
        second = {p.relative_to(target).as_posix(): p.read_bytes()
                  for p in sorted(target.rglob("*")) if p.is_file()}

        self.assertEqual(sorted(first), sorted(second))
        for name, content in first.items():
            self.assertEqual(second[name], content, f"{name} differs between runs")

    def test_layout_is_stable_wherever_it_is_created(self):
        """Two demos in different places agree on structure, and differ only
        where they must: the absolute paths of their own directories."""
        first, second = self.vault("a"), self.vault("b")
        init_mod.initialise_demo(first, with_agents=False)
        init_mod.initialise_demo(second, with_agents=False)
        self.assertEqual(
            sorted(p.relative_to(first).as_posix() for p in first.rglob("*")),
            sorted(p.relative_to(second).as_posix() for p in second.rglob("*")),
        )
        for note_id in _seeded_ids():
            a = next(first.rglob(f"{note_id}.md"))
            b = next(second.rglob(f"{note_id}.md"))
            self.assertEqual(a.relative_to(first), b.relative_to(second))
            self.assertEqual(a.read_text().replace(str(first), "<VAULT>"),
                             b.read_text().replace(str(second), "<VAULT>"))


def _seeded_ids():
    from brain import demo

    return demo.SEEDED_NOTE_IDS


class TestInitCli(InitTestCase):
    def _run(self, *args) -> tuple[int, str]:
        import contextlib
        import io

        from brain import cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli.main(list(args))
        return code, buf.getvalue()

    def test_init_succeeds_and_reports_doctor(self):
        code, out = self._run("init", str(self.vault()))
        self.assertEqual(code, 0, out)
        self.assertIn("Initialised vault", out)
        self.assertIn("no problems found", out)

    def test_init_refuses_an_existing_vault_with_a_clean_message(self):
        target = self.vault()
        self._run("init", str(target))
        code, out = self._run("init", str(target))
        self.assertEqual(code, 1)
        self.assertIn("already a vault", out)
        self.assertNotIn("Traceback", out)

    def test_the_cli_writes_no_agents_md_by_default(self):
        target = self.vault()
        code, _ = self._run("init", str(target))
        self.assertEqual(code, 0)
        self.assertFalse((target / "AGENTS.md").exists())

    def test_no_agents_flag_is_still_accepted_as_a_legacy_no_op(self):
        """Kept for 0.1.0 scripts: accepted, ignored, and never an error."""
        target = self.vault()
        code, out = self._run("init", "--no-agents", str(target))
        self.assertEqual(code, 0)
        self.assertFalse((target / "AGENTS.md").exists())
        self.assertNotIn("unrecognized arguments", out)
        self.assertNotIn("Traceback", out)

    def test_agents_doc_write_is_the_opt_in_path(self):
        """The documented way to get AGENTS.md after a default init."""
        target = self.vault()
        self._run("init", str(target))
        self.assertFalse((target / "AGENTS.md").exists())
        code, _ = self._run("--vault", str(target), "agents-doc", "--write")
        self.assertEqual(code, 0)
        agents = target / "AGENTS.md"
        self.assertTrue(agents.is_file())
        text = agents.read_text()
        self.assertIn("# AGENTS.md — vault", text)
        self.assertNotIn("{{", text)

    def test_demo_flag_through_the_cli(self):
        target = self.vault("example")
        code, out = self._run("init", "--demo", str(target))
        self.assertEqual(code, 0, out)
        self.assertIn("Example Brain", out)
        self.assertIn("no problems found", out)

    def test_demo_rerun_refused_cleanly(self):
        target = self.vault("example")
        self._run("init", "--demo", str(target))
        code, out = self._run("init", "--demo", str(target))
        self.assertEqual(code, 1)
        self.assertIn("not empty", out)
        self.assertNotIn("Traceback", out)

    def test_path_is_required_and_never_inferred_from_cwd(self):
        from brain import cli

        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["init"])


if __name__ == "__main__":
    unittest.main()
