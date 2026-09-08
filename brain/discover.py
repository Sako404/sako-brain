"""Propose candidate projects under the configured projects roots for review.

This module NEVER writes to the registry. It only reports what it finds so a
human (or a Claude Code skill acting on the human's behalf) can decide what to
register via /project-new.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .paths import Config
from .registry import load_registry

IGNORED_NAMES = {".git", ".claude", "node_modules", "__pycache__", ".astro", "dist"}


@dataclass
class Candidate:
    name: str
    path: str
    has_git: bool
    has_readme: bool
    has_package_manifest: bool
    subdir_count: int
    already_registered: bool


def _has_manifest(d: Path) -> bool:
    manifest_names = (
        "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml",
        "go.mod", "composer.json", "Gemfile",
    )
    return any((d / m).exists() for m in manifest_names)


def _has_readme(d: Path) -> bool:
    return any(
        (d / n).exists()
        for n in ("README.md", "README", "readme.md", "CLAUDE.md", "PROJECT_STATUS.md")
    )


def discover(config: Config, max_depth: int = 2) -> list[Candidate]:
    registered_paths = {
        str(Path(e.path)) for e in load_registry(config) if e.path
    }

    candidates: list[Candidate] = []

    def scan(base: Path, depth: int):
        if depth > max_depth or not base.is_dir():
            return
        try:
            children = sorted(p for p in base.iterdir() if p.is_dir())
        except PermissionError:
            return
        for child in children:
            if child.name in IGNORED_NAMES or child.name.startswith("."):
                continue

            has_git = (child / ".git").exists()
            has_readme = _has_readme(child)
            has_manifest = _has_manifest(child)
            is_project_signal = has_git or has_readme or has_manifest

            if is_project_signal:
                subdirs = [p for p in child.iterdir() if p.is_dir()] if child.is_dir() else []
                candidates.append(Candidate(
                    name=child.name,
                    path=str(child),
                    has_git=has_git,
                    has_readme=has_readme,
                    has_package_manifest=has_manifest,
                    subdir_count=len(subdirs),
                    already_registered=str(child) in registered_paths,
                ))
            else:
                scan(child, depth + 1)

    for root in config.projects_roots:
        scan(root, 1)
    return candidates
