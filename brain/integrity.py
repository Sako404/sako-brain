"""`brain integrity` — a point-in-time health/manifest report.

Combines brain doctor's checks with file hashes and git/backup status so a
human or AI can sanity-check the vault's actual state at a glance. This
manifest is a *report*, never authoritative over Markdown — if it and the
Markdown ever disagree, Markdown wins, same as the search index.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import backup, frontmatter, gitops, validate
from .paths import Config


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(config: Config) -> dict:
    manifest = {}
    for path in frontmatter.iter_markdown_files(config.brain_root, config.content_dirs):
        rel = str(path.relative_to(config.brain_root))
        manifest[rel] = _hash_file(path)
    return manifest


@dataclass
class IntegrityReport:
    generated_at: str
    total_notes: int
    note_count_by_type: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)
    duplicate_ids: int = 0
    broken_links: int = 0
    registry_errors: int = 0
    manifest: dict = field(default_factory=dict)
    git_initialized: bool = False
    last_git_snapshot: str | None = None
    backup_checked: bool = False
    backup_repo_reachable: bool | None = None
    backup_repo_detail: str = ""
    last_backup_date: str | None = None


def run(config: Config, check_backup: bool = True) -> IntegrityReport:
    notes, _parse_errors = validate.load_all_notes(config)
    problems = validate.run_all(config)

    by_type: dict[str, int] = {}
    for n in notes:
        key = n.type or "(none)"
        by_type[key] = by_type.get(key, 0) + 1

    git_init = gitops.is_initialized(config)
    last_git = gitops.last_snapshot_date(config) if git_init else None

    report = IntegrityReport(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        total_notes=len(notes),
        note_count_by_type=by_type,
        problems=problems,
        duplicate_ids=len([p for p in problems if p.check == "duplicate_ids"]),
        broken_links=len([p for p in problems if p.check == "broken_links"]),
        registry_errors=len([p for p in problems if p.check in
                              {"duplicate_registry_entries", "missing_project_dirs"}]),
        manifest=build_manifest(config),
        git_initialized=git_init,
        last_git_snapshot=last_git,
    )

    if check_backup:
        report.backup_checked = True
        settings = backup.default_settings(config)
        if not backup.credential_ready(settings):
            report.backup_repo_detail = f"no credential file at {settings.password_file}"
        else:
            try:
                reachable, detail = backup.is_initialized(settings)
                report.backup_repo_reachable = reachable
                report.backup_repo_detail = detail
                if reachable:
                    report.last_backup_date = backup.last_backup_date(settings)
            except Exception as exc:  # network/tooling issues shouldn't crash the report
                report.backup_repo_reachable = False
                report.backup_repo_detail = str(exc)

    return report


def save_manifest(config: Config, report: IntegrityReport) -> Path:
    out_dir = config.integrity_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"manifest-{datetime.now():%Y%m%d-%H%M%S}.json"
    payload = {
        "generated_at": report.generated_at,
        "total_notes": report.total_notes,
        "note_count_by_type": report.note_count_by_type,
        "manifest": report.manifest,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
