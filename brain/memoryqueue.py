"""Pending-memory queue — Level 2 write-policy staging area (Phase 5A).

Sako Brain's write policy has three levels (see AGENTS.md / the /remember
skill):

  1. Safe automatic update — Claude may write directly (project record
     updates after its own work, objective milestones, decisions made
     explicitly in-session, stale-data corrections from clear evidence).
  2. User-derived durable information — a plausible durable fact stated in
     conversation, but not explicitly "remember this". Goes here, in the
     pending queue, rather than straight into an authoritative record.
  3. Restricted/sensitive — never auto-written at all; requires explicit,
     strong user intent even to reach the pending queue.

This is deliberately ONE growing queue file
(`<inbox>/memory/pending.yaml`), not one file per candidate — a Brain
with dozens of small "maybe" facts accumulating as individual notes would
be worse to review than a single list. It is a YAML file, not Markdown
with frontmatter, so the FTS5 indexer's markdown scanner never picks up
entries as if they were authoritative notes.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from . import capture
from . import paths as paths_mod
from .paths import Config

# Relative to the vault's inbox directory, whatever that is named (OSS-2/C3).
QUEUE_RELATIVE_PATH = Path("memory") / "pending.yaml"

QUEUE_HEADER = (
    f"# {paths_mod.APP_NAME} — pending memory queue (Level 2 write-policy staging area).\n"
    "# NOT authoritative — nothing here is a real Brain fact yet.\n"
    "# Review: `brain memory pending` (open items) / `brain memory review` (full detail).\n"
    "# Resolve: `brain memory accept <id>` (writes it into the Brain) or\n"
    "#          `brain memory reject <id>` (discards it, entry kept for audit history).\n"
)


class MemoryQueueError(RuntimeError):
    pass


@dataclass
class PendingEntry:
    id: str
    created: str
    candidate_fact: str
    entities: list = field(default_factory=list)     # person-*/project-* ids mentioned
    source: str = ""
    source_date: str = ""
    proposed_destination: str = ""    # human-readable hint, e.g. an area directory or an existing note id
    proposed_type: str = "fact"       # Brain note type to use if accepted
    sensitivity: str = "normal"
    confidence: str = "assumption"
    reason: str = ""
    status: str = "pending"           # pending | accepted | rejected
    resolved_at: str = ""
    resolved_note: str = ""
    written_to: str = ""


def _queue_path(config: Config) -> Path:
    return config.inbox_dir / QUEUE_RELATIVE_PATH


def _load(config: Config) -> list[dict]:
    path = _queue_path(config)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return data if isinstance(data, list) else []


def _save(config: Config, entries: list[dict]) -> None:
    path = _queue_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True) if entries else "[]\n"
    path.write_text(QUEUE_HEADER + "\n" + body, encoding="utf-8")


def add(config: Config, *, candidate_fact: str, entities: list[str] | None = None,
        source: str = "", source_date: str = "", proposed_destination: str = "",
        proposed_type: str = "fact", sensitivity: str = "normal",
        confidence: str = "assumption", reason: str = "") -> PendingEntry:
    if sensitivity not in {"normal", "private", "restricted"}:
        raise MemoryQueueError(f"invalid sensitivity '{sensitivity}'")

    entries = _load(config)
    entry = PendingEntry(
        id=f"mem-{uuid.uuid4().hex[:8]}",
        created=date.today().isoformat(),
        candidate_fact=candidate_fact,
        entities=list(entities or []),
        source=source,
        source_date=source_date or date.today().isoformat(),
        proposed_destination=proposed_destination,
        proposed_type=proposed_type,
        sensitivity=sensitivity,
        confidence=confidence,
        reason=reason,
    )
    entries.append(asdict(entry))
    _save(config, entries)
    return entry


def list_all(config: Config) -> list[PendingEntry]:
    return [PendingEntry(**e) for e in _load(config)]


def list_pending(config: Config) -> list[PendingEntry]:
    return [e for e in list_all(config) if e.status == "pending"]


def get(config: Config, entry_id: str) -> PendingEntry | None:
    for e in _load(config):
        if e.get("id") == entry_id:
            return PendingEntry(**e)
    return None


def _mutate(config: Config, entry_id: str, **changes) -> PendingEntry:
    entries = _load(config)
    found = None
    for e in entries:
        if e.get("id") == entry_id:
            e.update(changes)
            found = e
            break
    if found is None:
        raise MemoryQueueError(f"no pending memory entry with id '{entry_id}'")
    _save(config, entries)
    return PendingEntry(**found)


def reject(config: Config, entry_id: str, note: str = "") -> PendingEntry:
    entry = get(config, entry_id)
    if entry is None:
        raise MemoryQueueError(f"no pending memory entry with id '{entry_id}'")
    if entry.status != "pending":
        raise MemoryQueueError(f"entry '{entry_id}' is already '{entry.status}', not pending")
    return _mutate(config, entry_id, status="rejected",
                    resolved_at=datetime.now().isoformat(timespec="seconds"),
                    resolved_note=note)


def accept(config: Config, entry_id: str, title: str | None = None) -> PendingEntry:
    """Writes the candidate fact into the Brain via the normal capture path
    (same well-formed-frontmatter mechanism the CLI/MCP `remember` use —
    accepting a pending entry is not a shortcut around provenance)."""
    entry = get(config, entry_id)
    if entry is None:
        raise MemoryQueueError(f"no pending memory entry with id '{entry_id}'")
    if entry.status != "pending":
        raise MemoryQueueError(f"entry '{entry_id}' is already '{entry.status}', not pending")

    dest = capture.capture(
        config,
        type_=entry.proposed_type,
        title=title or entry.candidate_fact[:80],
        text=entry.candidate_fact,
        people=[e for e in entry.entities if e.startswith("person-")],
        projects=[e for e in entry.entities if e.startswith("project-")],
        sensitivity=entry.sensitivity,
        confidence=entry.confidence,
        source=entry.source,
        source_date=entry.source_date,
    )
    return _mutate(
        config, entry_id, status="accepted",
        resolved_at=datetime.now().isoformat(timespec="seconds"),
        written_to=str(dest.relative_to(config.brain_root)),
    )
