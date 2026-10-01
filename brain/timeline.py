"""List timeline entries in chronological order, and create new ones."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from . import frontmatter
from . import indexer
from .capture import slugify
from .paths import Config


class TimelineWriteError(ValueError):
    """A timeline entry that cannot be created safely."""


@dataclass
class TimelineEntry:
    date: str
    id: str
    title: str
    path: str


def list_timeline(config: Config, reverse: bool = True) -> list[TimelineEntry]:
    base = config.timeline_dir
    entries = []
    if not base.exists():
        return entries
    for path in sorted(base.rglob("*.md")):
        try:
            note = frontmatter.parse_file(path)
        except frontmatter.FrontmatterError:
            continue
        date = str(note.meta.get("valid_from") or note.meta.get("created") or "")
        entries.append(TimelineEntry(date=date, id=note.id or "", title=note.title, path=str(path.relative_to(config.brain_root))))
    entries.sort(key=lambda e: e.date, reverse=reverse)
    return entries


def create_event(config: Config, title: str, valid_from: str, what_happened: str = "",
                  why_it_matters: str = "", people: list[str] | None = None,
                  projects: list[str] | None = None, tags: list[str] | None = None,
                  sensitivity: str = "normal", confidence: str = "fact",
                  source: str = "", source_date: str = "") -> Path:
    """Create `50_TIMELINE/event-<valid_from>-<slug>.md` from the event
    template — the fixed schema `/timeline`'s "Adding an event" step
    already specifies, extracted rather than left to a direct Write.
    `valid_from` is the event's own date (what sorts the timeline), never
    just today's date — the caller must supply it explicitly."""
    today = dt.date.today().isoformat()
    slug = slugify(title)
    note_id = f"event-{valid_from}-{slug}"
    dest = config.timeline_dir / f"{note_id}.md"
    if dest.exists():
        raise TimelineWriteError(f"{dest} already exists")

    meta = {
        "id": note_id, "type": "event", "status": "",
        "created": today, "updated": today,
        "people": people or [], "projects": projects or [], "tags": tags or [],
        "sensitivity": sensitivity, "source": source, "source_date": source_date,
        "confidence": confidence, "valid_from": valid_from, "aliases": [],
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
    indexer.index_note(config, dest)
    return dest
