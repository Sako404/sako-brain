"""Local git version history for the Brain.

The git metadata directory (`git_dir`) lives OUTSIDE Nextcloud on purpose —
git internals churn on every commit and aren't meaningful for Nextcloud to
sync/version itself, and syncing a live `.git/` directory is a known way to
corrupt it if Nextcloud uploads mid-write. We use `git --separate-git-dir`
instead: the Brain working tree keeps only a tiny `.git` pointer *file*
(a few bytes, changes essentially never), while real history lives at
`config.git_dir`. Everything else about the workflow is standard git.

No remote is configured or pushed anywhere by this module. That is a
deliberate, separate, explicit-only step for later.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import validate
from .paths import Config

# Doctor problems severe enough to refuse a snapshot outright — real
# corruption or a security issue, not just something worth a heads-up.
BLOCKING_CHECKS = {"invalid_yaml", "duplicate_ids", "secret_pattern", "backup_secret_in_brain",
                    "password_file_in_backup_scope"}


class GitError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        # git is an optional feature, not a dependency (OSS-4). The core works
        # without it; only `brain git *` needs it.
        raise GitError(
            "git is not installed or not on PATH. Version history is an "
            "optional feature; install git to use it."
        ) from None


def _git(config: Config, *args: str) -> subprocess.CompletedProcess:
    cmd = ["git", "--git-dir", str(config.git_dir), "--work-tree", str(config.brain_root), *args]
    return _run(cmd, cwd=config.brain_root)


def is_initialized(config: Config) -> bool:
    return (config.git_dir / "HEAD").exists()


def init_repo(config: Config) -> str:
    """Initialize the repo with a separate git-dir. Safe to call once;
    raises if already initialized so it's never accidentally re-run."""
    if is_initialized(config):
        raise GitError(f"already initialized at {config.git_dir}")

    config.git_dir.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        ["git", "init", f"--separate-git-dir={config.git_dir}", "."],
        cwd=config.brain_root,
    )
    if result.returncode != 0:
        raise GitError(result.stderr)

    branch_result = _git(config, "symbolic-ref", "HEAD", "refs/heads/main")
    if branch_result.returncode != 0:
        raise GitError(branch_result.stderr)

    return result.stdout


def status(config: Config) -> str:
    result = _git(config, "status")
    return result.stdout


def log(config: Config, limit: int = 20) -> str:
    result = _git(config, "log", f"-{limit}", "--date=short", "--pretty=format:%h  %ad  %s")
    return result.stdout


def last_snapshot_date(config: Config) -> str | None:
    """Date of the most recent commit, or None if no commits exist yet."""
    if not is_initialized(config):
        return None
    result = _git(config, "log", "-1", "--date=short", "--pretty=format:%ad")
    out = result.stdout.strip()
    return out or None


@dataclass
class SnapshotResult:
    committed: bool
    reason: str = ""
    commit_hash: str = ""
    message: str = ""
    stat: str = ""
    blocking: list = None
    warnings: list = None

    def __post_init__(self):
        self.blocking = self.blocking or []
        self.warnings = self.warnings or []


def _build_commit_message(config: Config) -> str:
    result = _git(config, "diff", "--cached", "--name-status")
    lines = [l for l in result.stdout.splitlines() if l.strip()]

    by_dir: dict[str, dict[str, int]] = {}
    total = {"A": 0, "M": 0, "D": 0}
    for line in lines:
        parts = line.split("\t")
        code = parts[0][0] if parts[0] else "M"
        if code not in "AMD":
            code = "M"  # renames (R###) etc. — treat as modify for the summary
        path = parts[-1]
        top = path.split("/")[0]
        by_dir.setdefault(top, {"A": 0, "M": 0, "D": 0})
        by_dir[top][code] += 1
        total[code] += 1

    header = f"Brain snapshot: {total['A']} added, {total['M']} modified, {total['D']} deleted"
    body_lines = []
    for d in sorted(by_dir):
        counts = by_dir[d]
        bits = "/".join(f"{v}{k}" for k, v in counts.items() if v)
        body_lines.append(f"- {d} ({bits})")

    if not body_lines:
        return header
    return header + "\n\n" + "\n".join(body_lines)


def snapshot(config: Config, message: str | None = None) -> SnapshotResult:
    """Doctor-gated commit of everything currently different in the working tree.

    Never auto-runs; only called when the user (or the /brain-doctor-style
    skill) explicitly invokes `brain git snapshot`. Refuses on blocking
    doctor problems, warns (but proceeds) on everything else, and does
    nothing (not even an empty commit) if there's nothing to snapshot.
    """
    if not is_initialized(config):
        raise GitError(f"git not initialized at {config.git_dir} — run 'brain git init' first")

    problems = validate.run_all(config)
    blocking = [p for p in problems if p.check in BLOCKING_CHECKS]
    warnings = [p for p in problems if p.check not in BLOCKING_CHECKS]

    if blocking:
        return SnapshotResult(committed=False, reason="blocking_doctor_problems",
                               blocking=blocking, warnings=warnings)

    add_result = _git(config, "add", "-A")
    if add_result.returncode != 0:
        raise GitError(add_result.stderr)

    diff = _git(config, "diff", "--cached", "--stat")
    if not diff.stdout.strip():
        return SnapshotResult(committed=False, reason="nothing_to_commit", warnings=warnings)

    commit_message = message or _build_commit_message(config)
    commit_result = _git(config, "commit", "-m", commit_message)
    if commit_result.returncode != 0:
        raise GitError(commit_result.stderr)

    hash_result = _git(config, "rev-parse", "--short", "HEAD")

    return SnapshotResult(
        committed=True,
        commit_hash=hash_result.stdout.strip(),
        message=commit_message,
        stat=diff.stdout,
        warnings=warnings,
    )
