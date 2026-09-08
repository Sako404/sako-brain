"""Gather raw facts about a working project directory for `brain project sync`.

This module only *reads* the project's own working directory — it never
writes there, and
it never edits the Brain project record itself. It hands back structured
facts; deciding what's "meaningful" and updating the Markdown record is the
/project-sync skill's job.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProjectFacts:
    path: str
    exists: bool
    is_git_repo: bool = False
    git_last_commit_date: str = ""
    git_recent_commits: list[str] = field(default_factory=list)
    git_status_dirty: bool = False
    readme_excerpt: str = ""
    top_level_entries: list[str] = field(default_factory=list)


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def gather(project_path: str) -> ProjectFacts:
    p = Path(project_path)
    if not p.exists():
        return ProjectFacts(path=project_path, exists=False)

    facts = ProjectFacts(path=project_path, exists=True)
    facts.top_level_entries = sorted(x.name for x in p.iterdir())

    git_dir = p / ".git"
    if git_dir.exists():
        facts.is_git_repo = True
        facts.git_last_commit_date = _run_git(["log", "-1", "--format=%ci"], p)
        log = _run_git(["log", "-10", "--format=%h %ad %s", "--date=short"], p)
        facts.git_recent_commits = [line for line in log.splitlines() if line]
        status = _run_git(["status", "--porcelain"], p)
        facts.git_status_dirty = bool(status)

    for readme_name in ("README.md", "README", "readme.md"):
        readme = p / readme_name
        if readme.exists():
            try:
                text = readme.read_text(encoding="utf-8", errors="replace")
                facts.readme_excerpt = "\n".join(text.splitlines()[:40])
            except OSError:
                pass
            break

    return facts
