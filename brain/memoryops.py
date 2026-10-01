"""Create a memory note at its canonical final destination — the "create
at an obvious final home" half of what `/remember` and `/import` do today
via direct Write, for the note types that have an unambiguous one.

Never accepts a client-supplied path. The destination is derived from
`type` alone for person/knowledge/document (each has exactly one canonical
bucket in this vault's taxonomy), or from `type` plus a validated,
already-existing `area` name for `fact` (the one type that genuinely has no
single bucket — real facts live under whichever `20_AREAS/<area>/` they
belong to). A `fact` with no matching area, or any other type, is refused
rather than guessed — `brain remember` (raw inbox capture) is still the
right tool for those, unchanged.

Each type's frontmatter/body shape matches its `80_TEMPLATES/*.md` file
exactly, so a note created here is indistinguishable from one a human
created by hand from the template.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import frontmatter
from . import indexer
from . import visibility
from .capture import slugify
from .paths import Config

# Types with exactly one destination directory, independent of content.
FIXED_DESTINATION_TYPES = {
    "person": "people",
    "knowledge": "knowledge",
    "document": "documents",
}


class MemoryWriteError(ValueError):
    """A remember/import request that cannot be placed at a safe, known destination."""


def existing_areas(config: Config) -> list[str]:
    """The only valid values for `area` — real, already-existing
    `20_AREAS/` subdirectories. Never creates a new one implicitly."""
    areas_dir = config.dir_for("areas")
    if not areas_dir.exists():
        return []
    return sorted(p.name for p in areas_dir.iterdir() if p.is_dir())


def _body_for(type_: str, title: str, text: str) -> str:
    detail = text or ""
    if type_ == "person":
        return f"# {title}\n\n## Summary\n\n{detail}\n\n## Relationship\n\n## Contact\n\n## Notes\n\n## Related\n"
    if type_ == "knowledge":
        return f"# {title}\n\n## Summary\n\n{detail}\n\n## Detail\n\n## Related\n"
    if type_ == "document":
        return f"# {title}\n\n## What this is\n\n{detail}\n\n## Where it lives\n\n## Key points\n\n## Related\n"
    if type_ == "fact":
        return f"# {title}\n\n## Detail\n\n{detail}\n\n## Source / provenance\n\n## Related\n"
    raise AssertionError(f"no body template for type {type_!r}")  # unreachable — caller validates first


def create_memory(config: Config, type_: str, title: str, text: str = "",
                   tags: list[str] | None = None, people: list[str] | None = None,
                   projects: list[str] | None = None, sensitivity: str = "normal",
                   confidence: str = "fact", source: str = "", source_date: str = "",
                   area: str | None = None, doc_path: str = "",
                   audience: list[str] | None = None) -> Path:
    allowed = config.vocabulary.note_types
    if type_ not in allowed:
        raise MemoryWriteError(f"unknown type '{type_}', must be one of {sorted(allowed)}")

    if type_ in FIXED_DESTINATION_TYPES:
        if area is not None:
            raise MemoryWriteError(f"type '{type_}' has a fixed destination — 'area' is not accepted")
        dest_dir = config.dir_for(FIXED_DESTINATION_TYPES[type_])
    elif type_ == "fact":
        if area is None:
            raise MemoryWriteError(
                "type 'fact' has no single destination — pass 'area' (an existing 20_AREAS/ "
                "subdirectory), or use 'brain remember' for undifferentiated inbox capture instead"
            )
        valid = existing_areas(config)
        if area not in valid:
            raise MemoryWriteError(f"unknown area '{area}' — must be one of {valid}")
        dest_dir = config.dir_for("areas") / area
    else:
        raise MemoryWriteError(
            f"type '{type_}' has no final-destination primitive yet — use 'brain remember' for "
            "inbox capture, or the dedicated 'brain project create'/'brain decision create'"
        )

    today = dt.date.today().isoformat()
    slug = slugify(title)
    note_id = f"{type_}-{slug}"
    dest = dest_dir / f"{note_id}.md"
    if dest.exists():
        raise MemoryWriteError(f"{dest} already exists")

    meta = {
        "id": note_id, "type": type_,
        "status": "current" if type_ == "fact" else "",
        "created": today, "updated": today,
        "people": people or [], "projects": projects or [], "tags": tags or [],
        "sensitivity": sensitivity, "source": source, "source_date": source_date,
        "confidence": confidence,
    }
    if type_ == "fact":
        meta.update({"valid_from": today, "valid_to": "", "supersedes": ""})
    if type_ == "document":
        meta["path"] = doc_path
    meta["aliases"] = []
    meta["owner_principal"] = visibility.id_to_ref(config.acting_principal)
    meta["audience"] = audience or []

    note = frontmatter.Note(path=dest, meta=meta, body=_body_for(type_, title, text))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(frontmatter.render(note), encoding="utf-8")
    indexer.index_note(config, dest)
    return dest
