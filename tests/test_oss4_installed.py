"""OSS-4: acceptance against an INSTALLED artefact, not the source tree.

OSS-3 proved the core works in a scrubbed environment over a **source
checkout** — it still put the repository on `PYTHONPATH`. This file removes
that last crutch and is the authoritative acceptance for public installation:

    build a wheel -> install it into a clean virtualenv -> scrub PYTHONPATH
    -> run the console script from outside the repository

The decisive assertion is that `brain.__file__` resolves inside the virtualenv.
Without it the whole exercise could pass while quietly importing the checkout,
which is the exact failure this stage exists to rule out.

Skipped, loudly, when no wheel can be produced (no `build` module, or no
network for the isolated build). The suite must stay runnable offline; the
merge gate is this file *executed*, not merely present.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_WORLD: "InstalledWorld | None" = None
_SKIP_REASON = ""


def _build_wheel(outdir: Path) -> Path | None:
    """Build a wheel with PEP 517 tooling. Returns None if that is impossible.

    A pre-built wheel may be supplied through SAKO_BRAIN_WHEEL, which is how a
    CI or an offline run avoids paying for the build twice.
    """
    prebuilt = os.environ.get("SAKO_BRAIN_WHEEL")
    if prebuilt and Path(prebuilt).is_file():
        return Path(prebuilt)

    builder = outdir / "buildenv"
    try:
        subprocess.run([sys.executable, "-m", "venv", str(builder)],
                       capture_output=True, timeout=300, check=True)
        subprocess.run([str(builder / "bin" / "pip"), "install", "-q", "build"],
                       capture_output=True, timeout=900, check=True)
        subprocess.run([str(builder / "bin" / "python"), "-m", "build", "--wheel",
                        "--outdir", str(outdir / "dist"), str(PROJECT_ROOT)],
                       capture_output=True, timeout=900, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    wheels = sorted((outdir / "dist").glob("*.whl"))
    return wheels[0] if wheels else None


class InstalledWorld:
    """A clean virtualenv with the wheel installed, and a throwaway HOME."""

    def __init__(self, wheel: Path):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.wheel = wheel
        self.venv = self.base / "venv"
        self.home = self.base / "home"
        self.cwd = self.base / "elsewhere"
        for d in (self.home, self.cwd):
            d.mkdir(parents=True)

        subprocess.run([sys.executable, "-m", "venv", str(self.venv)],
                       capture_output=True, timeout=300, check=True)
        subprocess.run([str(self.venv / "bin" / "pip"), "install", "-q", str(wheel)],
                       capture_output=True, timeout=900, check=True)

    def make_vault(self, name: str) -> Path:
        """The minimum a vault is: one config file. No `brain init` needed —
        that lands in OSS-4.2 and extends this file with its own cases."""
        vault = self.base / name
        (vault / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (vault / "90_SYSTEM" / "config.yaml").write_text(
            f"vault_name: {name}\n", encoding="utf-8")
        return vault

    @property
    def brain(self) -> Path:
        return self.venv / "bin" / "brain"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    def env(self, **extra) -> dict:
        """From scratch, and PYTHONPATH is not merely unset — it is absent."""
        env = {
            "PATH": f"{self.venv / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
        }
        env.update(extra)
        assert "PYTHONPATH" not in env
        return env

    def run(self, *args, stdin: str | None = None, cwd: Path | None = None,
            **env_extra) -> subprocess.CompletedProcess:
        """Invoke the installed console script — never `python -m`."""
        return subprocess.run(
            [str(self.brain), *args], env=self.env(**env_extra),
            cwd=str(cwd or self.cwd), input=stdin,
            capture_output=True, text=True, timeout=300,
        )

    def cleanup(self):
        self._tmp.cleanup()


def setUpModule():
    global _WORLD, _SKIP_REASON
    holder = tempfile.mkdtemp(prefix="oss4-build-")
    wheel = _build_wheel(Path(holder))
    if wheel is None:
        shutil.rmtree(holder, ignore_errors=True)
        _SKIP_REASON = ("could not build a wheel (needs the `build` module and "
                        "network for an isolated build)")
        return
    try:
        _WORLD = InstalledWorld(wheel)
    except (OSError, subprocess.SubprocessError) as exc:
        _SKIP_REASON = f"could not install the wheel: {exc}"
    finally:
        InstalledWorld._build_holder = holder  # kept until teardown


def tearDownModule():
    if _WORLD is not None:
        _WORLD.cleanup()
    holder = getattr(InstalledWorld, "_build_holder", None)
    if holder:
        shutil.rmtree(holder, ignore_errors=True)


class InstalledTestCase(unittest.TestCase):
    def setUp(self):
        if _WORLD is None:
            self.skipTest(_SKIP_REASON or "no installed world")
        self.world = _WORLD

    def assertOk(self, result, msg=""):
        self.assertNotIn("Traceback", result.stderr, f"{msg}\n{result.stderr}")
        self.assertEqual(result.returncode, 0,
                         f"{msg}\nstdout:{result.stdout}\nstderr:{result.stderr}")
        return result.stdout


class TestInstalledPackageIsTheOneBeingUsed(InstalledTestCase):
    """The assertions that make every other test in this file meaningful."""

    def test_brain_imports_from_inside_the_virtualenv(self):
        result = subprocess.run(
            [str(self.world.python), "-c", "import brain; print(brain.__file__)"],
            env=self.world.env(), cwd=str(self.world.cwd),
            capture_output=True, text=True, timeout=120,
        )
        location = self.assertOk(result).strip()
        self.assertTrue(location.startswith(str(self.world.venv)),
                        f"brain imported from {location}, not from the venv")
        # Compared against the project root, not its parent: in a container
        # the source can sit one level below `/`, and "starts with the parent"
        # is then true of every absolute path — an assertion that cannot fail
        # proves nothing. Found by running this suite on python:3.11-slim.
        self.assertFalse(location.startswith(str(PROJECT_ROOT) + "/"),
                         f"brain was imported from the SOURCE TREE ({location}) — "
                         "the install is not standing on its own")

    def test_pythonpath_is_absent_from_the_environment(self):
        self.assertNotIn("PYTHONPATH", self.world.env())

    def test_the_console_script_lives_in_the_virtualenv(self):
        self.assertTrue(self.world.brain.is_file())
        self.assertTrue(os.access(self.world.brain, os.X_OK))

    def test_the_repository_is_not_on_the_interpreter_path(self):
        result = subprocess.run(
            [str(self.world.python), "-c", "import sys, json; print(json.dumps(sys.path))"],
            env=self.world.env(), cwd=str(self.world.cwd),
            capture_output=True, text=True, timeout=120,
        )
        for entry in json.loads(self.assertOk(result)):
            if entry:
                self.assertFalse(Path(entry).resolve() == PROJECT_ROOT,
                                 f"project root leaked onto sys.path: {entry}")

    def test_distribution_metadata_reports_the_public_version(self):
        result = subprocess.run(
            [str(self.world.python), "-c",
             "from importlib import metadata; print(metadata.version('sako-brain'))"],
            env=self.world.env(), cwd=str(self.world.cwd),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(self.assertOk(result).strip(), "0.2.0")


class TestInstalledCliRunsOutsideTheRepository(InstalledTestCase):
    def test_help(self):
        out = self.assertOk(self.world.run("--help"))
        self.assertIn("brain", out)
        self.assertIn("init", out)

    def test_version(self):
        self.assertEqual(self.assertOk(self.world.run("--version")).strip(), "brain 0.2.0")

    def test_with_no_vault_it_refuses_cleanly_rather_than_failing_to_import(self):
        """An expected vault-discovery failure is a pass; an import or resource
        failure is not. Keeping the two apart is the point of this test."""
        result = self.world.run("status")
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("No Brain vault found", combined)
        self.assertNotIn("ModuleNotFoundError", combined)
        self.assertNotIn("Traceback", result.stderr)


class TestInstalledResources(InstalledTestCase):
    """The template must have travelled inside the wheel."""

    def test_agents_template_is_readable_from_the_installed_package(self):
        result = subprocess.run(
            [str(self.world.python), "-c",
             "from brain import agentsdoc; t = agentsdoc.template_text(); "
             "print(len(t)); print('{{VAULT_NAME}}' in t)"],
            env=self.world.env(), cwd=str(self.world.cwd),
            capture_output=True, text=True, timeout=120,
        )
        size, has_placeholder = self.assertOk(result).split()
        self.assertGreater(int(size), 1000)
        self.assertEqual(has_placeholder, "True")

    def test_the_wheel_itself_contains_the_template(self):
        import zipfile

        with zipfile.ZipFile(self.world.wheel) as z:
            names = z.namelist()
        self.assertIn("brain/templates/AGENTS.md.template", names)
        self.assertTrue(any("licenses/LICENSE" in n for n in names),
                        "the wheel does not carry the licence")


class TestInstalledCliOnAVault(InstalledTestCase):
    """The installed CLI operating on a real vault, from outside the repo.

    The vault here is hand-made — one config file, which is all a vault is.
    `brain init` is OSS-4.2 and adds its own cases to this file.
    """

    def test_doctor_integrity_and_index_are_clean(self):
        vault = self.world.make_vault("cli-vault")
        self.assertIn("no problems found",
                      self.assertOk(self.world.run("--vault", str(vault), "doctor")))
        self.assertIn("Doctor problems: 0",
                      self.assertOk(self.world.run("--vault", str(vault), "integrity")))
        self.assertIn("Indexed",
                      self.assertOk(self.world.run("--vault", str(vault), "index")))

    def test_agents_doc_renders_from_the_installed_template(self):
        vault = self.world.make_vault("agents-vault")
        out = self.assertOk(self.world.run("--vault", str(vault), "agents-doc"))
        self.assertIn("# AGENTS.md", out)
        self.assertNotIn("{{", out)

    def test_representative_content_can_be_created_and_retrieved(self):
        vault = self.world.make_vault("content-vault")
        self.assertOk(self.world.run(
            "--vault", str(vault), "remember", "--type", "fact",
            "--title", "Installed smoke test"))
        self.assertOk(self.world.run("--vault", str(vault), "index"))
        out = self.assertOk(self.world.run("--vault", str(vault), "search", "smoke"))
        self.assertIn("Installed smoke test", out)

    def test_state_lands_under_the_throwaway_home_not_in_the_vault(self):
        vault = self.world.make_vault("state-vault")
        self.assertOk(self.world.run("--vault", str(vault), "index"))
        self.assertEqual(list(vault.rglob("brain.db")), [])
        self.assertTrue(any((self.world.home / ".local" / "state").rglob("brain.db")))


class TestInstalledMinimalFirstRun(InstalledTestCase):
    """`brain init` driven through the installed console script (OSS-4.2)."""

    def test_init_creates_a_healthy_vault(self):
        vault = self.world.base / "init-vault"
        out = self.assertOk(self.world.run("init", str(vault)), "minimal init")
        self.assertIn("Initialised vault", out)
        self.assertIn("no problems found", out)
        self.assertTrue((vault / "90_SYSTEM" / "config.yaml").is_file())
        # 0.2.0: agent rules are opt-in, so a healthy vault has no AGENTS.md.
        self.assertFalse((vault / "AGENTS.md").exists())

    def test_the_initialised_vault_indexes_and_validates(self):
        vault = self.world.base / "init-usable"
        self.assertOk(self.world.run("init", str(vault)))
        self.assertIn("Doctor problems: 0",
                      self.assertOk(self.world.run("--vault", str(vault), "integrity")))
        self.assertIn("Indexed",
                      self.assertOk(self.world.run("--vault", str(vault), "index")))

    def test_rerun_refuses_without_force(self):
        vault = self.world.base / "init-rerun"
        self.assertOk(self.world.run("init", str(vault)))
        result = self.world.run("init", str(vault))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already a vault", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_no_agents_flag_through_the_installed_cli(self):
        """Legacy no-op: a 0.1.0 script keeps working against the installed 0.2.0."""
        vault = self.world.base / "init-no-agents"
        self.assertOk(self.world.run("init", "--no-agents", str(vault)))
        self.assertFalse((vault / "AGENTS.md").exists())
        self.assertIn("no problems found",
                      self.assertOk(self.world.run("--vault", str(vault), "doctor")))

    def test_agents_doc_write_opts_in_through_the_installed_cli(self):
        vault = self.world.base / "init-agents-optin"
        self.assertOk(self.world.run("init", str(vault)))
        self.assertFalse((vault / "AGENTS.md").exists())
        self.assertOk(self.world.run("--vault", str(vault), "agents-doc", "--write"))
        agents = vault / "AGENTS.md"
        self.assertTrue(agents.is_file())
        self.assertNotIn("{{", agents.read_text())

    def test_state_lands_under_the_throwaway_home(self):
        vault = self.world.base / "init-state"
        self.assertOk(self.world.run("init", str(vault)))
        self.assertOk(self.world.run("--vault", str(vault), "index"))
        self.assertEqual(list(vault.rglob("brain.db")), [])
        self.assertTrue(any((self.world.home / ".local" / "state").rglob("brain.db")))


class TestInstalledDemoFirstRun(InstalledTestCase):
    """OSS-4.3 through the installed console script."""

    def test_demo_init_is_clean_end_to_end(self):
        vault = self.world.base / "demo-vault"
        out = self.assertOk(self.world.run("init", "--demo", str(vault)), "demo init")
        self.assertIn("Example Brain", out)
        self.assertIn("no problems found", out)
        self.assertIn("Doctor problems: 0",
                      self.assertOk(self.world.run("--vault", str(vault), "integrity")))
        self.assertIn("Indexed 9 notes",
                      self.assertOk(self.world.run("--vault", str(vault), "index")))

    def test_the_demo_is_immediately_searchable(self):
        vault = self.world.base / "demo-searchable"
        self.assertOk(self.world.run("init", "--demo", str(vault)))
        self.assertOk(self.world.run("--vault", str(vault), "index"))
        out = self.assertOk(self.world.run("--vault", str(vault), "search", "observatory"))
        self.assertIn("Observatory", out)

    def test_demo_uses_the_synthetic_taxonomy_not_the_shipped_one(self):
        vault = self.world.base / "demo-taxonomy"
        self.assertOk(self.world.run("init", "--demo", str(vault)))
        present = {p.name for p in vault.iterdir() if p.is_dir()}
        self.assertIn("work", present)
        self.assertNotIn("30_PROJECTS", present)


class TestInstalledOptionalFeatures(InstalledTestCase):
    """Optional external tools are not Python dependencies."""

    def test_backup_reports_missing_configuration_without_a_traceback(self):
        vault = self.world.make_vault("backup-vault")
        result = self.world.run("--vault", str(vault), "backup", "status")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("backup_rclone_remote", result.stdout + result.stderr)

    def test_installed_dependencies_are_only_pyyaml(self):
        result = subprocess.run(
            [str(self.world.venv / "bin" / "pip"), "list", "--format=json"],
            env=self.world.env(), capture_output=True, text=True, timeout=300,
        )
        installed = {p["name"].lower() for p in json.loads(self.assertOk(result))}
        installed -= {"pip", "setuptools", "wheel", "sako-brain"}
        self.assertEqual(installed, {"pyyaml"},
                         f"unexpected runtime dependencies pulled in: {installed}")


class TestNoPrivateDataInTheDistribution(InstalledTestCase):
    """The leak gate, applied to what actually ships."""

    # Imported, never re-typed: OSS-2's fixture gate flags any test file that
    # spells a forbidden literal, and it is right to — the marker vocabulary
    # has exactly one home. See test_oss2_neutralisation for what the split
    # between attribution and identifier markers means.

    def test_wheel_contents_carry_no_production_identifier(self):
        import zipfile

        from tests.test_oss2_neutralisation import (ATTRIBUTION_MARKERS,
                                                     is_attribution_line)
        from tests.test_oss3_portability import PRODUCTION_MARKERS

        offenders = []
        with zipfile.ZipFile(self.world.wheel) as z:
            for name in z.namelist():
                if name.endswith(("/", ".so")):
                    continue
                text = z.read(name).decode("utf-8", errors="ignore")
                for lineno, line in enumerate(text.splitlines(), 1):
                    for marker in PRODUCTION_MARKERS:
                        if marker not in line:
                            continue
                        if marker in ATTRIBUTION_MARKERS and is_attribution_line(line):
                            continue  # intended attribution — see the predicate
                        offenders.append(f"{name}:{lineno}: {marker} -> {line.strip()[:70]}")
        self.assertEqual(offenders, [], "production data inside the wheel:\n" + "\n".join(offenders))

    def test_every_occurrence_of_the_name_is_an_attribution_line(self):
        """The exemption must stay a shape, not become a loophole.

        The author's name may appear only on a line that LOOKS like
        attribution — an `Author:` header, a pyproject `authors =` entry, or a
        `Copyright (C) <year>` notice. Anywhere else it is a leak, even in
        metadata.
        """
        import zipfile

        from tests.test_oss2_neutralisation import (ATTRIBUTION_MARKERS,
                                                     is_attribution_line)

        stray, attributions = [], 0
        with zipfile.ZipFile(self.world.wheel) as z:
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                for line in z.read(name).decode("utf-8", errors="ignore").splitlines():
                    if not any(m in line for m in ATTRIBUTION_MARKERS):
                        continue
                    if is_attribution_line(line):
                        attributions += 1
                    else:
                        stray.append(f"{name}: {line.strip()[:70]}")
        self.assertEqual(stray, [], "author name outside an attribution line:\n" + "\n".join(stray))
        if ATTRIBUTION_MARKERS:
            # Only meaningful where this deployment declares a name to look for.
            # A public checkout supplies no private markers file, so there is
            # nothing to find and nothing to assert — the half that matters is
            # `stray`, which holds in both modes.
            self.assertGreater(attributions, 0, "no attribution found at all")


if __name__ == "__main__":
    unittest.main()
