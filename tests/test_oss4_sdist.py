"""OSS-4: the sdist must also build, install and run.

Wheel is the primary acceptance path (`test_oss4_installed.py`). This is the
secondary one: if an sdist is produced as a distributable artefact, it has to
work, because a source distribution is what anyone building from source gets.

Deliberately thinner than the wheel suite — build, install, CLI starts,
resources present — rather than a second full acceptance run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_STATE: dict = {}


def setUpModule():
    holder = Path(tempfile.mkdtemp(prefix="oss4-sdist-"))
    _STATE["holder"] = holder

    prebuilt = os.environ.get("SAKO_BRAIN_SDIST")
    if prebuilt and Path(prebuilt).is_file():
        _STATE["sdist"] = Path(prebuilt)
    else:
        builder = holder / "buildenv"
        try:
            subprocess.run([sys.executable, "-m", "venv", str(builder)],
                           capture_output=True, timeout=300, check=True)
            subprocess.run([str(builder / "bin" / "pip"), "install", "-q", "build"],
                           capture_output=True, timeout=900, check=True)
            subprocess.run([str(builder / "bin" / "python"), "-m", "build", "--sdist",
                            "--outdir", str(holder / "dist"), str(PROJECT_ROOT)],
                           capture_output=True, timeout=900, check=True)
        except (OSError, subprocess.SubprocessError):
            _STATE["skip"] = "could not build an sdist (needs `build` and network)"
            return
        sdists = sorted((holder / "dist").glob("*.tar.gz"))
        if not sdists:
            _STATE["skip"] = "no sdist produced"
            return
        _STATE["sdist"] = sdists[0]

    venv = holder / "venv"
    try:
        subprocess.run([sys.executable, "-m", "venv", str(venv)],
                       capture_output=True, timeout=300, check=True)
        subprocess.run([str(venv / "bin" / "pip"), "install", "-q", str(_STATE["sdist"])],
                       capture_output=True, timeout=900, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        _STATE["skip"] = f"could not install the sdist: {exc}"
        return
    _STATE["venv"] = venv


def tearDownModule():
    holder = _STATE.get("holder")
    if holder:
        shutil.rmtree(holder, ignore_errors=True)


class SdistTestCase(unittest.TestCase):
    def setUp(self):
        if "venv" not in _STATE:
            self.skipTest(_STATE.get("skip", "no sdist environment"))
        self.venv = _STATE["venv"]
        self.home = _STATE["holder"] / "home"
        self.home.mkdir(exist_ok=True)

    def env(self) -> dict:
        env = {
            "PATH": f"{self.venv / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
        }
        assert "PYTHONPATH" not in env
        return env

    def run_brain(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.venv / "bin" / "brain"), *args],
                              env=self.env(), cwd=str(self.home),
                              capture_output=True, text=True, timeout=300)


class TestSdistInstallation(SdistTestCase):
    def test_cli_starts(self):
        result = self.run_brain("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "brain 0.2.0")

    def test_package_resources_are_present(self):
        result = subprocess.run(
            [str(self.venv / "bin" / "python"), "-c",
             "from brain import agentsdoc; print('{{VAULT_NAME}}' in agentsdoc.template_text())"],
            env=self.env(), cwd=str(self.home), capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True")

    def test_sdist_contains_the_packaging_inputs(self):
        import tarfile

        with tarfile.open(_STATE["sdist"]) as t:
            names = t.getnames()
        for expected in ("pyproject.toml", "LICENSE", "README.md",
                         "brain/templates/AGENTS.md.template"):
            self.assertTrue(any(n.endswith(expected) for n in names),
                            f"{expected} missing from the sdist")

    def test_sdist_excludes_the_test_suite(self):
        """Deliberate, and not for size.

        The leak gate must spell out the private identifiers it forbids
        everywhere else — that is its job — and a distribution artefact is the
        last place those strings belong. The tests live in the repository.
        """
        import tarfile

        with tarfile.open(_STATE["sdist"]) as t:
            names = t.getnames()
        self.assertFalse(any("/tests/" in n for n in names),
                         "the sdist ships the test suite, including the leak gate")

    def test_sdist_never_carries_the_deployment_marker_file(self):
        """The leak gate's own marker list is the private data it protects.

        setuptools ships only declared files, so this could not happen today —
        which is precisely why it is asserted: "it would not be included
        anyway" is the assumption that stops being true after a packaging
        change, and the cost of being wrong is publishing the strings the gate
        exists to hide.
        """
        import tarfile

        with tarfile.open(_STATE["sdist"]) as t:
            names = t.getnames()
        for forbidden in ("private-markers.txt", ".gitleaks.toml"):
            self.assertFalse(any(forbidden in n for n in names),
                             f"{forbidden} reached the sdist")

    def test_sdist_carries_no_private_identifier(self):
        import tarfile

        from tests.test_oss2_neutralisation import (ATTRIBUTION_MARKERS,
                                                     is_attribution_line)
        from tests.test_oss3_portability import PRODUCTION_MARKERS

        offenders = []
        with tarfile.open(_STATE["sdist"]) as t:
            for member in t.getmembers():
                if not member.isfile():
                    continue
                text = t.extractfile(member).read().decode("utf-8", errors="ignore")
                for line in text.splitlines():
                    for marker in PRODUCTION_MARKERS:
                        if marker not in line:
                            continue
                        if marker in ATTRIBUTION_MARKERS and is_attribution_line(line):
                            continue  # intended attribution — see the predicate
                        offenders.append(f"{member.name}: {marker}")
        self.assertEqual(offenders, [], "private data in the sdist:\n" + "\n".join(offenders))

    def test_a_vault_works_from_the_sdist_install(self):
        vault = _STATE["holder"] / "sdist-vault"
        (vault / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (vault / "90_SYSTEM" / "config.yaml").write_text("vault_name: Sdist\n")
        result = self.run_brain("--vault", str(vault), "doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no problems found", result.stdout)


if __name__ == "__main__":
    unittest.main()
