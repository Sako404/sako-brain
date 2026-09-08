"""Update an existing note in place — used by update_memory (MCP) and /remember.

Never rewrites history: this only sets/merges frontmatter fields and can
append to the body. It does not delete existing body content.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import frontmatter
from .paths import Config


def find_note_path(config: Config, note_id: str) -> Path | None:
    for path in frontmatter.iter_markdown_files(config.brain_root, config.content_dirs):
        try:
            note = frontmatter.parse_file(path)
        except frontmatter.FrontmatterError:
            continue
        if note.id == note_id:
            return path
    return None


def update_memory(config: Config, note_id: str, set_fields: dict | None = None,
                   append_text: str | None = None) -> Path:
    path = find_note_path(config, note_id)
    if not path:
        raise FileNotFoundError(f"no note with id '{note_id}' found under {config.brain_root}")

    note = frontmatter.parse_file(path)

    for key, value in (set_fields or {}).items():
        if key in {"id", "created"}:
            continue  # never mutate identity or original creation date
        note.meta[key] = value

    note.meta["updated"] = dt.date.today().isoformat()

    if append_text:
        stamp = dt.date.today().isoformat()
        note.body = note.body.rstrip("\n") + f"\n\n## Update ({stamp})\n\n{append_text}\n"

    path.write_text(frontmatter.render(note), encoding="utf-8")
    return path
