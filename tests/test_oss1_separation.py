"""OSS-1 regression coverage: code, configuration, vault and runtime state
must each be able to live somewhere different.

Before OSS-1 the package computed the vault from its own location on disk
(`__file__` -> 90_SYSTEM -> parent) and put the search index, logs and
integrity manifests inside that vault. Both are guarded here, because both
are the kind of coupling that creeps back in one convenient default at a time.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain import indexer, integrity
from brain.paths import (
    Config,
    VaultNotFoundError,
    default_config,
    default_state_dir,
    find_vault_upwards,
    resolve_brain_root,
)
from tests.helpers import TempVault


def _make_vault(root: Path) -> Path:
    """Minimum on disk for a directory to count as a vault."""
    (root / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
    (root / "90_SYSTEM" / "config.yaml").write_text("projects_root: /nonexistent\n")
    return root


class TestCodeVaultSeparation(unittest.TestCase):
    """The package must never answer "which vault?" with "wherever I live"."""

    def test_refuses_instead_of_guessing_when_no_vault_is_configured(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as cfg:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}, clear=False):
                os.environ.pop("BRAIN_ROOT", None)
                previous = Path.cwd()
                try:
                    os.chdir(cwd)
                    with self.assertRaises(VaultNotFoundError):
                        resolve_brain_root()
                finally:
                    os.chdir(previous)

    def test_package_location_constants_are_gone(self):
        """Guards against reintroducing the exact coupling OSS-1 removed."""
        import brain.paths as paths_mod

        for name in ("PACKAGE_DIR", "SYSTEM_DIR", "DEFAULT_BRAIN_ROOT"):
            self.assertFalse(
                hasattr(paths_mod, name),
                f"brain.paths.{name} is back — the package is deriving paths from its own location",
            )

    def test_resolved_vault_is_unrelated_to_where_the_package_lives(self):
        package_dir = Path(__import__("brain").__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cwd:
            vault = _make_vault(Path(tmp) / "somewhere-else")
            with patch.dict(os.environ, {"BRAIN_ROOT": str(vault)}, clear=False):
                previous = Path.cwd()
                try:
                    os.chdir(cwd)
                    resolved = resolve_brain_root()
                finally:
                    os.chdir(previous)
            self.assertEqual(resolved, vault)
            self.assertNotIn(str(package_dir), str(resolved))

    def test_explicit_argument_beats_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            chosen = _make_vault(Path(tmp) / "chosen")
            other = _make_vault(Path(tmp) / "other")
            with patch.dict(os.environ, {"BRAIN_ROOT": str(other)}, clear=False):
                self.assertEqual(resolve_brain_root(chosen), chosen)

    def test_upward_search_finds_an_enclosing_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(Path(tmp) / "vault")
            deep = vault / "20_AREAS" / "nested"
            deep.mkdir(parents=True)
            self.assertEqual(find_vault_upwards(deep), vault.resolve())

    def test_upward_search_returns_none_outside_any_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(find_vault_upwards(Path(tmp)))


class TestVaultStateSeparation(unittest.TestCase):
    """Runtime state must not live inside the vault."""

    def test_state_paths_are_outside_the_vault_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(Path(tmp) / "vault")
            config = Config(
                brain_root=vault,
                projects_roots=(Path(tmp) / "projects",),
                backup_target=Path(tmp) / "backup",
            )
            for label, path in (
                ("db_path", config.db_path),
                ("logs_dir", config.logs_dir),
                ("integrity_dir", config.integrity_dir),
            ):
                self.assertEqual(path.parent, config.state_dir)
                self.assertFalse(
                    str(path).startswith(str(vault)),
                    f"{label} resolved inside the vault: {path}",
                )

    def test_indexing_and_integrity_leave_no_runtime_artefacts_in_the_vault(self):
        vault = TempVault()
        try:
            vault.write_note("60_KNOWLEDGE", "a.md", id="knowledge-a", type="knowledge",
                             title="A note", body="Some searchable body text.")
            config = vault.config()

            indexer.rebuild(config)
            integrity.save_manifest(config, integrity.run(config, check_backup=False))

            stray = [
                p for p in vault.root.rglob("*")
                if p.is_file() and (
                    p.suffix in {".db", ".log"}
                    or p.name.startswith("brain.db")
                    or p.name.startswith("manifest-")
                )
            ]
            self.assertEqual(stray, [], f"runtime artefacts written into the vault: {stray}")
            self.assertTrue(config.db_path.exists(), "index was not written to the state directory")
        finally:
            vault.cleanup()

    def test_different_vaults_get_different_state_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = _make_vault(Path(tmp) / "vault-a")
            b = _make_vault(Path(tmp) / "vault-b")
            self.assertNotEqual(default_state_dir(a), default_state_dir(b))

    def test_state_dir_is_stable_for_the_same_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _make_vault(Path(tmp) / "vault")
            self.assertEqual(default_state_dir(vault), default_state_dir(vault))


class TestTempVaultAndTempState(unittest.TestCase):
    """A full working setup must be constructible entirely from temp dirs —
    no real vault, no real state, nothing under the developer's home."""

    def test_temp_vault_with_temp_state_indexes_and_searches(self):
        vault = TempVault()
        try:
            config = vault.config()
            self.assertTrue(str(vault.root).startswith(tempfile.gettempdir()))
            self.assertTrue(str(config.state_dir).startswith(tempfile.gettempdir()))
            self.assertFalse(str(config.state_dir).startswith(str(vault.root)))

            vault.write_note("60_KNOWLEDGE", "n.md", id="knowledge-n", type="knowledge",
                             title="Findable", body="unmistakable-token-oss1")
            stats = indexer.rebuild(config)
            self.assertEqual(stats["indexed"], 1)

            conn = sqlite3.connect(config.db_path)
            rows = conn.execute("SELECT id FROM notes").fetchall()
            conn.close()
            self.assertEqual([r[0] for r in rows], ["knowledge-n"])
        finally:
            vault.cleanup()

    def test_two_state_dirs_over_one_vault_stay_independent(self):
        vault = TempVault()
        try:
            vault.write_note("60_KNOWLEDGE", "n.md", id="knowledge-n", type="knowledge")
            base = vault.config()
            with tempfile.TemporaryDirectory() as other_state:
                second = Config(
                    brain_root=base.brain_root,
                    projects_roots=base.projects_roots,
                    backup_target=base.backup_target,
                    git_dir=base.git_dir,
                    backup_repo=base.backup_repo,
                    state_dir=Path(other_state) / "state",
                )
                indexer.rebuild(base)
                self.assertTrue(base.db_path.exists())
                self.assertFalse(second.db_path.exists())
                self.assertNotEqual(base.db_path, second.db_path)
        finally:
            vault.cleanup()

    def test_env_var_overrides_configured_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            (vault / "90_SYSTEM").mkdir(parents=True)
            (vault / "90_SYSTEM" / "config.yaml").write_text(
                f"state_dir: {Path(tmp) / 'from-config'}\n"
            )
            from_env = Path(tmp) / "from-env"

            configured = default_config(brain_root=vault)
            self.assertEqual(configured.state_dir, Path(tmp) / "from-config")

            with patch.dict(os.environ, {"BRAIN_STATE_DIR": str(from_env)}, clear=False):
                overridden = default_config(brain_root=vault)
            self.assertEqual(overridden.state_dir, from_env)


if __name__ == "__main__":
    unittest.main()
