"""Shared helpers for brain tests — builds a throwaway temp vault so tests
never touch the real Nextcloud Brain."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from brain.paths import CONTENT_DIRS, Config

# git is an OPTIONAL feature (OSS-4): the core works without it, so its tests
# must skip rather than fail on a machine that has no git. Found by running the
# suite on a python:3.11-slim container, which ships no git — exactly the
# environment a user with a minimal install would have.
requires_git = unittest.skipUnless(shutil.which("git"),
                                   "git is not installed (optional feature)")

NOTE_TEMPLATE = """---
id: {id}
type: {type}
status: {status}
created: {created}
updated: {updated}
people: {people}
projects: {projects}
tags: {tags}
sensitivity: normal
source: test
confidence: fact
aliases: []
---

# {title}

{body}
"""


class TempVault:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for d in CONTENT_DIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.root / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
        (self.root / "30_PROJECTS" / "_registry.yaml").write_text("projects: []\n")
        self.projects_root = self.root / "fake_projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.backup_target = self.root / "fake_backup"
        # Kept OUTSIDE self.root, same as the real design (git-dir/backup repo
        # live outside the Brain) — and critically outside the real system's
        # default paths too, so tests never touch real git/backup state.
        self._external_tmp = tempfile.TemporaryDirectory()
        self.git_dir = Path(self._external_tmp.name) / "git" / "example-vault.git"
        self.backup_repo = Path(self._external_tmp.name) / "backup_repo"
        # Runtime state (index, logs, integrity manifests) also lives outside
        # the vault, mirroring the real design (OSS-1). A test that asserts the
        # vault stays free of runtime artefacts depends on this being external.
        self.state_dir = Path(self._external_tmp.name) / "state"

    def config(self) -> Config:
        return Config(
            brain_root=self.root,
            projects_roots=(self.projects_root,),
            backup_target=self.backup_target,
            git_dir=self.git_dir,
            backup_repo=self.backup_repo,
            state_dir=self.state_dir,
        )

    def write_note(self, rel_dir: str, filename: str, *, id: str, type: str,
                   status: str = "", created: str = "2026-01-01", updated: str = "2026-01-01",
                   people=None, projects=None, tags=None, title: str = "Title", body: str = ""):
        path = self.root / rel_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(NOTE_TEMPLATE.format(
            id=id, type=type, status=status, created=created, updated=updated,
            people=people or [], projects=projects or [], tags=tags or [],
            title=title, body=body,
        ))
        return path

    def cleanup(self):
        self._tmp.cleanup()
        self._external_tmp.cleanup()
