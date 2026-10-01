"""Stage 2 (SAKO Brain multi-user): owner_principal/audience backfill.

Existing records migrate safely to `owner_principal: principal:marcin`,
`audience: []` — the exact Stage 2 design default, never inferred from a
note's content, type, tags, or people. No automatic family sharing, no
content-based guessing.

Pure addition, never destructive: a note that already carries
`owner_principal` is left completely untouched (how a note that already
has it — migrated earlier, or newly written under Stage 2's own default
ownership-on-write — is told apart from one that still needs it). Safe to
re-run any number of times, including after a partial/interrupted run —
there is no step that depends on a previous run having completed.

Deliberately NOT reachable via the SSH forced-command dispatcher under any
mode (never added to its allowlists) — same structural, local-only
isolation as `brain principal`/`brain group`. Recovery path: this vault is
git-tracked (see `gitops.py`); since every change this migration makes is
additive, `git diff`/`git revert` against the commit it produced is the
rollback mechanism, not a separate undo feature built here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter
from . import indexer
from .paths import Config
from .visibility import DEFAULT_OWNER_ID, id_to_ref


@dataclass
class MigrationReport:
    total_notes: int = 0
    already_migrated: int = 0
    migrated: int = 0
    migrated_paths: list = field(default_factory=list)   # list[Path]
    parse_errors: list = field(default_factory=list)      # list[tuple[Path, str]]


def migrate_owner_principal(config: Config, *, dry_run: bool = False) -> MigrationReport:
    """Walks every content note exactly once. A parse error is recorded and
    skipped — never raised past the caller, and never allowed to abort the
    rest of the run (the same "one bad note never takes the batch down"
    discipline brain/state.py already uses for its own section collectors).
    `dry_run=True` computes and reports the exact same thing without
    writing anything — what a caller should always run first against
    production."""
    report = MigrationReport()
    for path in frontmatter.iter_markdown_files(config.brain_root, config.content_dirs):
        report.total_notes += 1
        try:
            note = frontmatter.parse_file(path)
        except frontmatter.FrontmatterError as exc:
            report.parse_errors.append((path, str(exc)))
            continue

        if "owner_principal" in note.meta:
            report.already_migrated += 1
            continue

        note.meta["owner_principal"] = id_to_ref(DEFAULT_OWNER_ID)
        note.meta.setdefault("audience", [])
        report.migrated += 1
        report.migrated_paths.append(path)
        if not dry_run:
            path.write_text(frontmatter.render(note), encoding="utf-8")
            indexer.index_note(config, path)

    return report
