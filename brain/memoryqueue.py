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

Ownership (v0.22.0): every entry carries `owner_principal` (whoever
queued it) and `audience`, read through visibility.py exactly like a note's
own frontmatter. Before this the queue had neither, so it was kept away
from the dispatcher and the remote gateway entirely (the 2026-10-02
pre-onboarding INVARIANT). Now every read is filtered to what the caller
may see, and only an entry's owner may accept or reject it — audience
grants read visibility only, as everywhere else. An entry written before
v0.22.0 has no owner field and belongs to visibility.DEFAULT_OWNER_ID, the
same fail-closed default an un-migrated note gets.

Remote writers can now reach the file concurrently, so every
read-modify-write holds an exclusive lock (a lock file in the state
directory, never inside the vault).
"""
from __future__ import annotations

import contextlib
import fcntl
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from . import capture
from . import paths as paths_mod
from . import visibility
from . import writepolicy
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
    owner_principal: str = visibility.id_to_ref(visibility.DEFAULT_OWNER_ID)
    audience: list = field(default_factory=list)


def _queue_path(config: Config) -> Path:
    return config.inbox_dir / QUEUE_RELATIVE_PATH


@contextlib.contextmanager
def _locked(config: Config):
    """Exclusive lock around one read-modify-write of the queue. Not
    reentrant: public functions take it once and call the unlocked helpers."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    with open(config.state_dir / "memory-queue.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _load(config: Config) -> list[dict]:
    path = _queue_path(config)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return data if isinstance(data, list) else []


def _visible(config: Config, principal_id: str, entry: dict) -> bool:
    return visibility.can_view(config, principal_id, entry)


def _save(config: Config, entries: list[dict]) -> None:
    path = _queue_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True) if entries else "[]\n"
    path.write_text(QUEUE_HEADER + "\n" + body, encoding="utf-8")


def add(config: Config, *, candidate_fact: str, entities: list[str] | None = None,
        source: str = "", source_date: str = "", proposed_destination: str = "",
        proposed_type: str = "fact", sensitivity: str = "normal",
        confidence: str = "assumption", reason: str = "",
        audience: list[str] | None = None) -> PendingEntry:
    """Queues under the acting principal. Callers run the write-policy and
    role-policy checks first (secret scan, restricted confirmation, audience
    allowed), the same as for `remember`."""
    if sensitivity not in {"normal", "private", "restricted"}:
        raise MemoryQueueError(f"invalid sensitivity '{sensitivity}'")
    owner = visibility.id_to_ref(config.acting_principal)
    try:
        visibility.owner_and_audience({"owner_principal": owner, "audience": list(audience or [])})
    except visibility.VisibilityError as exc:
        raise MemoryQueueError(str(exc)) from None

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
        owner_principal=owner,
        audience=list(audience or []),
    )
    with _locked(config):
        entries = _load(config)
        entries.append(asdict(entry))
        _save(config, entries)
    return entry


def list_all(config: Config, principal_id: str | None = None) -> list[PendingEntry]:
    """Every entry `principal_id` (default: the acting principal) may see,
    resolved ones included."""
    principal_id = principal_id or config.acting_principal
    return [PendingEntry(**e) for e in _load(config) if _visible(config, principal_id, e)]


def list_pending(config: Config, principal_id: str | None = None) -> list[PendingEntry]:
    return [e for e in list_all(config, principal_id) if e.status == "pending"]


def _find(config: Config, entries: list[dict], entry_id: str) -> dict:
    """The entry the acting principal may see, or the same "no entry" error
    whether it is missing or someone else's — a caller cannot probe for
    other people's ids."""
    for e in entries:
        if e.get("id") == entry_id and _visible(config, config.acting_principal, e):
            return e
    raise MemoryQueueError(f"no pending memory entry with id '{entry_id}'")


def _find_own_pending(config: Config, entries: list[dict], entry_id: str) -> dict:
    e = _find(config, entries, entry_id)
    if not visibility.is_owner(config, config.acting_principal, e):
        raise MemoryQueueError(f"only the owner of '{entry_id}' may resolve it")
    if e.get("status") != "pending":
        raise MemoryQueueError(f"entry '{entry_id}' is already '{e.get('status')}', not pending")
    return e


def get(config: Config, entry_id: str) -> PendingEntry | None:
    try:
        return PendingEntry(**_find(config, _load(config), entry_id))
    except MemoryQueueError:
        return None


def reject(config: Config, entry_id: str, note: str = "") -> PendingEntry:
    with _locked(config):
        entries = _load(config)
        e = _find_own_pending(config, entries, entry_id)
        e.update(status="rejected", resolved_at=datetime.now().isoformat(timespec="seconds"),
                 resolved_note=note)
        _save(config, entries)
    return PendingEntry(**e)


def accept(config: Config, entry_id: str, title: str | None = None,
           confirm_restricted: bool = False) -> PendingEntry:
    """Writes the candidate fact into the Brain via the normal capture path
    (same well-formed-frontmatter mechanism the CLI/MCP `remember` use —
    accepting a pending entry is not a shortcut around provenance). Only the
    owner may accept, so the note is written under the owner's identity with
    the entry's own audience. A restricted entry needs the same explicit
    confirmation a restricted `remember` does."""
    with _locked(config):
        entries = _load(config)
        e = _find_own_pending(config, entries, entry_id)
        entry = PendingEntry(**e)
        writepolicy.require_restricted_confirmation(
            config, config.acting_principal, entry.sensitivity, confirm_restricted)
        dest = capture.capture(
            config,
            type_=entry.proposed_type,
            title=title or entry.candidate_fact[:80],
            text=entry.candidate_fact,
            people=[x for x in entry.entities if x.startswith("person-")],
            projects=[x for x in entry.entities if x.startswith("project-")],
            sensitivity=entry.sensitivity,
            confidence=entry.confidence,
            source=entry.source,
            source_date=entry.source_date,
            audience=entry.audience,
        )
        e.update(status="accepted", resolved_at=datetime.now().isoformat(timespec="seconds"),
                 written_to=str(dest.relative_to(config.brain_root)))
        _save(config, entries)
    return PendingEntry(**e)
