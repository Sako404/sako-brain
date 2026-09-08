"""List timeline entries in chronological order."""
from __future__ import annotations

from dataclasses import dataclass

from . import frontmatter
from .paths import Config


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
