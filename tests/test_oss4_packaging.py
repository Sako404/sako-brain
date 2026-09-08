"""OSS-4: packaging metadata and installed-resource behaviour.

These tests run against the *source tree*. The far stronger proof — a built
wheel installed into a clean virtualenv with PYTHONPATH scrubbed — lives in
`test_oss4_installed.py`, which is the authoritative acceptance for public
installation. This file guards the metadata that makes that possible.
"""
from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from brain import __version__, agentsdoc
from brain import paths as paths_mod

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


class TestPackagingMetadata(unittest.TestCase):
    def setUp(self):
        if not PYPROJECT.is_file():
            self.skipTest("no pyproject.toml in this checkout")
        self.data = _pyproject()
        self.project = self.data["project"]

    def test_distribution_name_is_the_canonical_slug(self):
        """The one unavoidable duplicate of the identity string, pinned.

        Static TOML cannot import Python, so `pyproject.toml` must spell the
        name out. This test is what stops that copy from drifting away from
        `paths.APP_DIRNAME` — packaging introduces no second identity.
        """
        self.assertEqual(self.project["name"], paths_mod.DISTRIBUTION_NAME)
        self.assertEqual(paths_mod.DISTRIBUTION_NAME, paths_mod.APP_DIRNAME)

    def test_python_floor_is_declared(self):
        self.assertEqual(self.project["requires-python"], ">=3.11")

    def test_runtime_dependencies_are_exactly_pyyaml(self):
        """One dependency, deliberately. A second one appearing here should be
        a decision, not an accident."""
        names = [d.split(">")[0].split("=")[0].split("[")[0].strip().lower()
                 for d in self.project["dependencies"]]
        self.assertEqual(names, ["pyyaml"])

    def test_no_optional_dependency_groups(self):
        """Everything optional in this tool is an external binary. pip must
        never be asked to install restic, rclone, git or systemd."""
        self.assertNotIn("optional-dependencies", self.project)

    def test_console_script_points_at_the_cli(self):
        self.assertEqual(self.project["scripts"], {"brain": "brain.cli:main"})

    def test_no_second_entry_point_is_shipped(self):
        """`brain-mcp` is deliberately absent: MCP is out of public v0.1."""
        self.assertEqual(list(self.project["scripts"]), ["brain"])

    def test_version_is_dynamic_from_the_single_source(self):
        self.assertIn("version", self.project["dynamic"])
        self.assertNotIn("version", self.project)
        attr = self.data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        self.assertEqual(attr, "brain.__version__")

    def test_public_version_baseline(self):
        self.assertEqual(__version__, "0.1.0")

    def test_template_is_declared_as_package_data(self):
        """Without this the wheel ships a package whose agents-doc is broken."""
        package_data = self.data["tool"]["setuptools"]["package-data"]
        self.assertIn("templates/*.template", package_data["brain"])

    def test_licence_metadata_matches_the_recorded_decision(self):
        self.assertEqual(self.project["license"], "AGPL-3.0-or-later")
        self.assertIn("LICENSE", self.project["license-files"])
        self.assertTrue((PROJECT_ROOT / "LICENSE").is_file())
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE",
                      (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8"))

    def test_no_fabricated_public_urls(self):
        """No repository, homepage or issue tracker exists yet. Shipping a
        made-up URL in distribution metadata is not acceptable."""
        self.assertNotIn("urls", self.project)

    def test_readme_exists_and_is_referenced(self):
        self.assertEqual(self.project["readme"], "README.md")
        self.assertTrue((PROJECT_ROOT / "README.md").is_file())

    def test_build_backend_is_setuptools(self):
        self.assertEqual(self.data["build-system"]["build-backend"],
                         "setuptools.build_meta")


class TestResourceLoading(unittest.TestCase):
    """The template must be reachable through the package, not through a path."""

    def test_template_loads_through_importlib_resources(self):
        text = agentsdoc.template_text()
        self.assertIn("{{DIRECTORY_MAP}}", text)
        self.assertIn("{{VAULT_NAME}}", text)

    def test_agentsdoc_no_longer_holds_a_source_relative_path(self):
        source = Path(agentsdoc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Path(__file__).parent", source)
        self.assertIn("importlib.resources", source)

    def test_render_uses_the_packaged_template_by_default(self):
        import tempfile

        from brain.paths import Config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            rendered = agentsdoc.render(
                Config(brain_root=root, state_dir=Path(tmp) / "state"))
            self.assertIn("# AGENTS.md — vault", rendered)
            self.assertNotIn("{{", rendered)


class TestOptionalBinariesFailCleanly(unittest.TestCase):
    """A missing external tool disables a feature; it never breaks the package."""

    def test_importing_the_package_needs_no_external_binary(self):
        import importlib

        for module in ("brain.backup", "brain.gitops", "brain.systemdstatus",
                       "brain.indexer", "brain.validate", "brain.agentsdoc"):
            importlib.import_module(module)

    def test_missing_restic_becomes_a_backup_error(self):
        import subprocess
        from unittest import mock

        from brain import backup

        settings = backup.BackupSettings(
            backend="local", local_repo_path=Path("/tmp/nonexistent-repo"),
            password_file=Path(__file__),  # any existing non-empty file
        )
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(backup.BackupError) as ctx:
                backup._run_restic(settings, ["cat", "config"])
        self.assertIn("restic is not installed", str(ctx.exception))

    def test_missing_git_becomes_a_git_error(self):
        import subprocess
        from unittest import mock

        from brain import gitops

        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(gitops.GitError) as ctx:
                gitops._run(["git", "status"])
        self.assertIn("git is not installed", str(ctx.exception))


class TestSqliteFts5Check(unittest.TestCase):
    """FTS5 is an environment prerequisite pip cannot install."""

    def test_check_passes_on_this_interpreter(self):
        import tempfile

        from brain import validate
        from brain.paths import Config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(brain_root=root, state_dir=Path(tmp) / "state")
            self.assertEqual(validate.check_sqlite_fts5(config), [])

    def test_check_reports_actionably_when_fts5_is_missing(self):
        import sqlite3
        import tempfile
        from unittest import mock

        from brain import validate
        from brain.paths import Config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            config = Config(brain_root=root, state_dir=Path(tmp) / "state")
            with mock.patch.object(sqlite3, "connect",
                                   side_effect=sqlite3.OperationalError("no such module: fts5")):
                problems = validate.check_sqlite_fts5(config)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0].check, "sqlite_fts5_missing")
        self.assertIn("pip", problems[0].message)


if __name__ == "__main__":
    unittest.main()


class TestPublicDocumentation(unittest.TestCase):
    """The public-facing documents are shipped artefacts and first contact.

    Assertions test the CLAIM, not the phrasing, so the prose can be edited
    without a test edit — except where the exact wording is the point (a
    published-package pretence, a widened platform promise).
    """

    FILES = ("README.md", "CONTRIBUTING.md", "SECURITY.md", "TRADEMARK.md", "CHANGELOG.md")

    def setUp(self):
        readme = PROJECT_ROOT / "README.md"
        if not readme.is_file():
            self.skipTest("no README.md")
        self.text = readme.read_text(encoding="utf-8")

    def _has(self, *phrases: str) -> bool:
        """Line breaks must not decide whether a claim is present."""
        flat = " ".join(self.text.split())
        return all(p in flat for p in phrases)

    # --- the public governance set ---------------------------------------

    def test_every_public_document_exists(self):
        for name in self.FILES:
            self.assertTrue((PROJECT_ROOT / name).is_file(), f"{name} missing")

    def test_readme_links_to_the_documents_it_names(self):
        for name in ("CONTRIBUTING.md", "SECURITY.md", "TRADEMARK.md", "LICENSE"):
            self.assertIn(f"({name})", self.text, f"README does not link {name}")

    # --- honesty about status --------------------------------------------

    def test_states_the_python_floor_that_is_declared(self):
        self.assertTrue(self._has("Python 3.11"))

    def test_platform_claims_match_what_was_actually_proven(self):
        """Linux tested, macOS untested, Windows unsupported — no wider."""
        self.assertTrue(self._has("Supported and tested"), "Linux claim missing")
        self.assertTrue(self._has("not tested"), "macOS must be marked untested")
        self.assertTrue(self._has("Not supported"), "Windows must be marked unsupported")
        for overclaim in ("Windows is supported", "works on Windows",
                          "macOS is supported", "cross-platform"):
            self.assertNotIn(overclaim, self.text, f"platform overclaim: {overclaim!r}")

    def test_does_not_pretend_the_package_is_published(self):
        """The exact wording matters here: these lines would be a lie."""
        for lie in ("pip install sako-brain", "pipx install sako-brain"):
            self.assertNotIn(lie, self.text, f"README implies PyPI: {lie!r}")
        self.assertTrue(self._has("not on PyPI"))

    def test_unimplemented_features_appear_only_as_roadmap(self):
        body, _, roadmap = self.text.partition("## Roadmap")
        for term in ("MCP", "skills", "cloud sync", "hosted service", "web UI"):
            self.assertNotIn(term, body,
                             f"{term!r} is mentioned before the roadmap section")
        self.assertIn("Not implemented", roadmap)

    def test_says_what_the_project_does_not_do(self):
        """The most load-bearing section: it stops a reader inferring a
        service, a UI or a built-in AI."""
        self.assertIn("## What it does not do", self.text)
        for claim in ("No cloud service", "No sync", "No web or graphical",
                      "No AI built in", "No automatic backup"):
            self.assertIn(claim, self.text, f"non-goal missing: {claim}")

    # --- orientation for a reader who knows nothing -----------------------

    def test_explains_what_it_is_and_why_it_exists(self):
        self.assertTrue(self._has("local-first"))
        self.assertIn("## Why it exists", self.text)

    def test_documents_both_first_run_modes(self):
        self.assertTrue(self._has("brain init "))
        self.assertTrue(self._has("brain init --demo"))

    def test_documents_where_state_and_config_live(self):
        for token in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "90_SYSTEM/config.yaml"):
            self.assertIn(token, self.text)

    def test_marks_git_and_backup_as_optional(self):
        self.assertTrue(self._has("Optional external tools"))
        self.assertTrue(self._has("no default destination"))

    def test_sets_support_expectations_without_an_sla(self):
        self.assertTrue(self._has("Best-effort"))
        self.assertTrue(self._has("no service level"))

    def test_explains_the_licence_rather_than_only_naming_it(self):
        self.assertTrue(self._has("Affero"))
        self.assertTrue(self._has("corresponding source"))

    # --- privacy ----------------------------------------------------------

    def test_no_public_document_carries_a_private_identifier(self):
        from tests.test_oss2_neutralisation import IDENTIFIER_MARKERS

        offenders = []
        for name in self.FILES:
            path = PROJECT_ROOT / name
            if not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                offenders += [f"{name}:{lineno}: {m}" for m in IDENTIFIER_MARKERS if m in line]
        self.assertEqual(offenders, [], "private identifiers in public docs:\n" + "\n".join(offenders))

    def test_no_document_exposes_a_contact_address(self):
        """Attribution is a name. An email, phone or address is not."""
        email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
        for name in self.FILES:
            path = PROJECT_ROOT / name
            if not path.is_file():
                continue
            found = [m for m in email.findall(path.read_text())
                     if not m.endswith(".invalid")]
            self.assertEqual(found, [], f"{name} exposes an address: {found}")

    def test_no_invented_project_urls(self):
        """No repository, homepage or issue tracker exists yet."""
        for name in self.FILES:
            path = PROJECT_ROOT / name
            if not path.is_file():
                continue
            text = path.read_text()
            for invented in ("github.com/", "pypi.org/project/sako"):
                self.assertNotIn(invented, text, f"{name} contains an invented URL")

    # --- the other public documents ---------------------------------------

    def test_security_policy_uses_private_reporting_and_no_address(self):
        text = (PROJECT_ROOT / "SECURITY.md").read_text()
        self.assertIn("private vulnerability reporting", text.lower())
        self.assertIn("do not open a public issue", text.lower())

    def test_contributing_states_the_privacy_rules(self):
        text = (PROJECT_ROOT / "CONTRIBUTING.md").read_text()
        self.assertIn("No private fixture data", text)
        self.assertIn("AGPL", text)

    def test_trademark_policy_does_not_claim_a_registered_mark(self):
        text = (PROJECT_ROOT / "TRADEMARK.md").read_text()
        self.assertIn("No trademark is registered", text)
        self.assertIn("AGPL", text)

    def test_changelog_has_one_honest_entry(self):
        text = (PROJECT_ROOT / "CHANGELOG.md").read_text()
        self.assertIn("0.1.0", text)
        self.assertIn("First public release", text)
        for fake in ("## 0.0.", "## [0.0."):
            self.assertNotIn(fake, text, "changelog invents earlier public versions")

    def test_licence_named_consistently_across_every_document(self):
        for name in ("README.md", "CONTRIBUTING.md", "TRADEMARK.md"):
            text = (PROJECT_ROOT / name).read_text()
            self.assertTrue("AGPL-3.0-or-later" in text or "Affero" in text,
                            f"{name} does not name the licence")
