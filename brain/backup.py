"""Encrypted, versioned, deduplicated backups via restic.

Design (Phase 4):

- Tool: restic. Client-side AES-256 encryption, content-addressed dedup,
  built-in integrity checking (`restic check`), mature and well-documented.
  No custom cryptography is implemented here — all encryption is restic's.
- Destination: **entirely a deployment choice, configured, never assumed**
  (OSS-2/C6 — this module previously hardcoded one person's cloud remote).
  Two backends:
    * `rclone` (preferred) — `rclone:<remote>:<path>`, from
      `backup_rclone_remote:` / `backup_rclone_repo_path:` in the vault's
      config.yaml. Where the destination is an rclone FUSE mount, talking to
      restic's native rclone backend is more robust than writing through the
      mount (no POSIX-over-FUSE locking/rename quirks, no dependency on the
      mount being active) and lands in exactly the path the user sees.
    * `local` — any plain filesystem path, from `backup_repo:`.
  Neither has a built-in default: an unconfigured vault gets a clear error,
  not somebody else's repository.
- Credential: the repository password is never stored in the Brain, never
  logged, and never passed as a CLI argument (which would leak it into
  `ps`). It lives in an external file (`RESTIC_PASSWORD_FILE`) outside the
  Brain, outside Nextcloud, outside Google Drive — see `password_file`
  below. Move it into Vaultwarden as the long-term canonical store; the
  file is a local stopgap so `brain backup` has something to read.
- What's backed up: the Brain working tree AND the external git
  metadata directory (so full note history survives a total local disk
  loss, not just the current snapshot of each file).
- What's excluded: only generated/rebuildable data (SQLite index, caches,
  bytecode, logs, local Obsidian workspace state) — the same spirit as
  `.gitignore`. Restricted content is protected by encryption, never by
  excluding it from the backup.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths as paths_mod
from .paths import Config

def default_password_file() -> Path:
    """The restic repository password file, under the XDG config directory.

    Resolved at call time rather than import time so it follows XDG_CONFIG_HOME
    (and so tests can redirect it). On an ordinary Linux account this is
    `~/.config/<app>/restic-password` — the same location this deployment has
    always used, now derived instead of hardcoded (OSS-2/C6).
    """
    return paths_mod.user_config_path().parent / "restic-password"
BACKUP_TAG = paths_mod.APP_DIRNAME

# Phase 4.3: retention policy applied by `brain backup retention`, run on a
# weekly cadence (not after every daily backup) via the maintenance timer.
RETENTION_POLICY = {"daily": 14, "weekly": 8, "monthly": 24}

# Mirrors .gitignore's generated/rebuildable exclusions — see that file's
# comments for why each of these is safe to skip.
#
# The four 90_SYSTEM/ patterns are kept deliberately even though OSS-1 moved
# runtime state out of the vault entirely: they still cover a vault restored
# from a pre-OSS-1 snapshot, which carries an index, logs and manifests in the
# old locations. They match nothing in a migrated vault, which is the point.
_SYS = paths_mod.SYSTEM_DIRNAME
EXCLUDES = [
    f"{_SYS}/brain.db",
    f"{_SYS}/brain.db-*",
    f"{_SYS}/logs/*.log",
    f"{_SYS}/logs/*.json",
    f"{_SYS}/integrity/*.json",
    "**/__pycache__",
    "*.pyc",
    ".obsidian/workspace.json",
    ".obsidian/workspace-mobile.json",
    ".DS_Store",
]


class BackupError(RuntimeError):
    pass


@dataclass
class BackupSettings:
    backend: str = "rclone"  # "rclone" (preferred) or "local" (any plain path)
    rclone_remote: str = ""
    rclone_repo_path: str = ""
    local_repo_path: Path | None = None  # only used when backend == "local"
    password_file: Path = field(default_factory=default_password_file)

    def repository(self) -> str:
        if self.backend == "rclone":
            if not self.rclone_remote or not self.rclone_repo_path:
                raise BackupError(
                    "the 'rclone' backup backend needs a remote and a repository "
                    "path — set `backup_rclone_remote:` and "
                    "`backup_rclone_repo_path:` in the vault's "
                    "90_SYSTEM/config.yaml."
                )
            return f"rclone:{self.rclone_remote}:{self.rclone_repo_path}"
        if self.backend == "local":
            if self.local_repo_path is None:
                raise BackupError(
                    "the 'local' backup backend needs a repository path — set "
                    "`backup_repo:` in the vault's 90_SYSTEM/config.yaml."
                )
            return str(self.local_repo_path)
        raise BackupError(f"unknown backend '{self.backend}'")


def default_settings(config: Config) -> BackupSettings:
    return BackupSettings(
        rclone_remote=config.backup_rclone_remote,
        rclone_repo_path=config.backup_rclone_repo_path,
        local_repo_path=config.backup_repo,
    )


def _restic_env(settings: BackupSettings) -> dict:
    import os
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = settings.repository()
    env["RESTIC_PASSWORD_FILE"] = str(settings.password_file)
    # Keep restic's own cache out of the Brain / Nextcloud / Google Drive.
    env.setdefault("RESTIC_CACHE_DIR", str(Path.home() / ".cache" / "restic"))
    return env


def credential_ready(settings: BackupSettings) -> bool:
    return settings.password_file.exists() and settings.password_file.stat().st_size > 0


def _run_restic(settings: BackupSettings, args: list[str], input_text: str | None = None,
                 timeout: int = 600) -> subprocess.CompletedProcess:
    if not credential_ready(settings):
        raise BackupError(
            f"backup password file not found or empty: {settings.password_file}\n"
            "Create it (600 permissions, contents = the repository password only) before running backup commands."
        )
    cmd = ["restic", *args]
    try:
        return subprocess.run(
            cmd, env=_restic_env(settings), capture_output=True, text=True,
            input=input_text, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        # An optional external tool, not a Python dependency (OSS-4): pip
        # cannot install restic and must not try. Missing means this feature
        # is unavailable, not that the program is broken.
        raise BackupError(
            "restic is not installed or not on PATH. Backup is an optional "
            "feature; install restic to use it, or ignore this command."
        ) from None


def is_initialized(settings: BackupSettings) -> tuple[bool, str]:
    """Check repo existence without creating one. Returns (initialized, detail)."""
    result = _run_restic(settings, ["cat", "config"], timeout=60)
    if result.returncode == 0:
        return True, "repository exists"
    return False, (result.stderr or result.stdout).strip()


def init_repo(settings: BackupSettings) -> subprocess.CompletedProcess:
    return _run_restic(settings, ["init"], timeout=120)


def backup_paths(config: Config) -> list[str]:
    """The Brain working tree, plus the external git-dir *if it exists yet*
    (git history is optional — a vault that's never run `brain git init`
    should still be backupable)."""
    paths = [str(config.brain_root)]
    if config.git_dir.exists():
        paths.append(str(config.git_dir))
    return paths


def run_backup(config: Config, settings: BackupSettings, tag: str = BACKUP_TAG) -> subprocess.CompletedProcess:
    args = ["backup", *backup_paths(config), "--tag", tag]
    for pattern in EXCLUDES:
        args += ["--exclude", pattern]
    return _run_restic(settings, args, timeout=1800)


def check_repo(settings: BackupSettings, read_data: bool = False) -> subprocess.CompletedProcess:
    args = ["check"]
    if read_data:
        args.append("--read-data")
    return _run_restic(settings, args, timeout=1800)


def forget_and_prune(settings: BackupSettings, keep_daily: int = RETENTION_POLICY["daily"],
                      keep_weekly: int = RETENTION_POLICY["weekly"],
                      keep_monthly: int = RETENTION_POLICY["monthly"],
                      tag: str = BACKUP_TAG, dry_run: bool = False) -> subprocess.CompletedProcess:
    """Apply the retention policy and reclaim space from pruned snapshots.
    Intended to run on a lower-frequency (weekly) schedule, not after every
    daily backup — repacking is the expensive part of `--prune`."""
    args = [
        "forget", "--tag", tag,
        "--keep-daily", str(keep_daily),
        "--keep-weekly", str(keep_weekly),
        "--keep-monthly", str(keep_monthly),
        "--prune",
    ]
    if dry_run:
        args.append("--dry-run")
    return _run_restic(settings, args, timeout=3600)


@dataclass
class SnapshotInfo:
    short_id: str
    time: str
    paths: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def list_snapshots(settings: BackupSettings) -> list[SnapshotInfo]:
    result = _run_restic(settings, ["snapshots", "--json"], timeout=120)
    if result.returncode != 0:
        raise BackupError((result.stderr or result.stdout).strip())
    data = json.loads(result.stdout or "[]")
    return [
        SnapshotInfo(short_id=s.get("short_id", ""), time=s.get("time", ""),
                     paths=s.get("paths", []), tags=s.get("tags", []))
        for s in data
    ]


def last_backup_date(settings: BackupSettings) -> str | None:
    try:
        snaps = list_snapshots(settings)
    except (BackupError, subprocess.SubprocessError):
        return None
    if not snaps:
        return None
    latest = max(snaps, key=lambda s: s.time)
    return latest.time[:10] if latest.time else None


def latest_snapshot(settings: BackupSettings) -> SnapshotInfo | None:
    snaps = list_snapshots(settings)
    if not snaps:
        return None
    return max(snaps, key=lambda s: s.time)


def restore_snapshot(settings: BackupSettings, snapshot_id: str, target_dir: Path,
                      include: str | None = None) -> subprocess.CompletedProcess:
    target_dir = Path(target_dir)
    if str(target_dir.resolve()).rstrip("/") in {"/", str(Path.home())}:
        raise BackupError(f"refusing to restore into a dangerous target: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    args = ["restore", snapshot_id, "--target", str(target_dir)]
    if include:
        args += ["--include", include]
    return _run_restic(settings, args, timeout=1800)


def log_result(config: Config, action: str, result: subprocess.CompletedProcess) -> Path:
    """Append a one-line, secret-free summary to a local log file."""
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.logs_dir / f"backup-{datetime.now():%Y-%m}.log"
    status_word = "OK" if result.returncode == 0 else f"FAILED(rc={result.returncode})"
    line = f"{datetime.now().isoformat(timespec='seconds')}  {action}  {status_word}\n"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line)
    paths_mod.ensure_private_file(log_path)
    return log_path
