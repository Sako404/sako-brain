"""List timeline entries in chronological order, and create new ones."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from . import audit
from . import frontmatter
from . import indexer
from . import visibility
from .capture import slugify
from .decision import _mark_superseded
from .paths import Config
from .update import resolve_write_target


class TimelineWriteError(ValueError):
    """A timeline entry that cannot be created safely."""


@dataclass
class TimelineEntry:
    date: str
    id: str
    title: str
    path: str
    status: str = ""
    superseded_by: str = ""


def list_timeline(config: Config, reverse: bool = True,
                   principal_id: str | None = None) -> list[TimelineEntry]:
    """Stage 2: every caller of this — `get_context`, `search_timeline`,
    `brain timeline`, the operational-state dashboard — is a genuine read
    path, so filtering happens here directly rather than through a
    separate load_visible_* variant (unlike registry.py, nothing needs
    the unfiltered list for write/integrity correctness)."""
    if principal_id is None:
        principal_id = config.acting_principal
    base = config.timeline_dir
    entries = []
    if not base.exists():
        return entries
    # A newer event names the one it replaces in `supersedes`; the old one
    # only carries `status: superseded`. The forward link is rebuilt here so
    # a reader of the old entry sees what replaced it without opening it.
    superseded_by: dict[str, str] = {}
    for path in sorted(base.rglob("*.md")):
        if not frontmatter.is_memory_path(path, config.brain_root):
            continue
        try:
            note = frontmatter.parse_file(path)
        except frontmatter.FrontmatterError:
            continue
        if not visibility.can_view_note(config, principal_id, note):
            continue
        date = str(note.meta.get("valid_from") or note.meta.get("created") or "")
        supersedes = str(note.meta.get("supersedes") or "")
        if supersedes and note.id:
            superseded_by[supersedes] = note.id
        entries.append(TimelineEntry(date=date, id=note.id or "", title=note.title,
                                     path=str(path.relative_to(config.brain_root)),
                                     status=str(note.meta.get("status") or "")))
    for e in entries:
        e.superseded_by = superseded_by.get(e.id, "")
    entries.sort(key=lambda e: e.date, reverse=reverse)
    return entries


def create_event(config: Config, title: str, valid_from: str, what_happened: str = "",
                  why_it_matters: str = "", people: list[str] | None = None,
                  projects: list[str] | None = None, tags: list[str] | None = None,
                  sensitivity: str = "normal", confidence: str = "fact",
                  source: str = "", source_date: str = "",
                  audience: list[str] | None = None, supersedes: str | None = None) -> Path:
    """Create `50_TIMELINE/event-<valid_from>-<slug>.md` from the event
    template — the fixed schema `/timeline`'s "Adding an event" step
    already specifies, extracted rather than left to a direct Write.
    `valid_from` is the event's own date (what sorts the timeline), never
    just today's date — the caller must supply it explicitly.

    `supersedes` names an older event whose state this one replaces (a
    problem logged earlier and fixed later): the old event is marked
    `status: superseded` and linked forward, the same way decisions are."""
    if not supersedes:
        return _create_event_locked(config, title, valid_from, what_happened, why_it_matters,
                                    people, projects, tags, sensitivity, confidence, source,
                                    source_date, audience, None)
    # Same lock as decision supersede: marking the old event is a
    # read-modify-write of an existing record.
    from .projectops import project_write_lock

    with project_write_lock(config):
        return _create_event_locked(config, title, valid_from, what_happened, why_it_matters,
                                    people, projects, tags, sensitivity, confidence, source,
                                    source_date, audience, supersedes)


def _create_event_locked(config: Config, title, valid_from, what_happened, why_it_matters,
                         people, projects, tags, sensitivity, confidence, source,
                         source_date, audience, supersedes) -> Path:
    """`create_event`'s body; under the write lock whenever `supersedes` is set."""
    old_path = None
    if supersedes:
        try:
            old_path = resolve_write_target(config, supersedes)
        except FileNotFoundError:
            raise TimelineWriteError(f"supersedes target '{supersedes}' not found") from None
        principal_id = config.acting_principal
        old_note = frontmatter.parse_file(old_path)
        if old_note.type != "event":
            raise TimelineWriteError("supersedes target must be a timeline event")
        if not visibility.is_owner(config, principal_id, old_note.meta):
            current_owner, _ = visibility.owner_and_audience(old_note.meta)
            audit.log_event(config, event="note.write.denied", principal_id=principal_id,
                             **audit.caller_provenance(config),
                             detail=f"id={supersedes} owner={current_owner}")
            raise TimelineWriteError(
                f"only '{current_owner}' (the owner of '{supersedes}') may mark it superseded "
                f"— '{principal_id}' is not permitted"
            )

    today = dt.date.today().isoformat()
    slug = slugify(title)
    note_id = f"event-{valid_from}-{slug}"
    dest = config.timeline_dir / f"{note_id}.md"
    if not frontmatter.is_memory_path(dest, config.brain_root):
        raise TimelineWriteError("ordinary memory cannot target protected or outside-vault paths")
    if dest.exists():
        raise TimelineWriteError(f"{dest} already exists")

    meta = {
        "id": note_id, "type": "event", "status": "",
        "created": today, "updated": today,
        "people": people or [], "projects": projects or [], "tags": tags or [],
        "sensitivity": sensitivity, "source": source, "source_date": source_date,
        "confidence": confidence, "valid_from": valid_from, "supersedes": supersedes or "",
        "aliases": [],
        "owner_principal": visibility.id_to_ref(config.acting_principal),
        "audience": audience or [],
    }
    related = "\n".join(f"`{p}`" for p in (projects or []))
    body = (
        f"# {title}\n\n"
        f"## What happened\n\n{what_happened}\n\n"
        f"## Why it matters\n\n{why_it_matters}\n\n"
        f"## Related\n\n{related}\n"
    )
    note = frontmatter.Note(path=dest, meta=meta, body=body)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(frontmatter.render(note), encoding="utf-8")

    if old_path is not None:
        _mark_superseded(old_path, note_id)
        indexer.index_note(config, old_path)

    indexer.index_note(config, dest)
    audit.log_event(config, event="note.write", principal_id=config.acting_principal,
                     **audit.caller_provenance(config),
                     detail=f"id={note_id}")
    return dest
