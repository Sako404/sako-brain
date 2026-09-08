"""Deterministic raw capture into the vault's inbox directory.

This is intentionally dumb: it just writes a well-formed frontmatter note.
The *smart* work (search for duplicates, decide create-vs-update, pick the
right final location, add timeline entries) is the /remember skill's job —
it should call `brain search` itself before deciding to use this, or should
edit an existing note directly instead.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .paths import DEFAULT_NOTE_TYPES, Config

# Kept as the *shipped default* only, for callers that need a vocabulary before
# a vault is resolved (argparse help text). The authority at capture time is
# `config.vocabulary.note_types`, which a vault's config.yaml can extend.
ALLOWED_TYPES = frozenset(DEFAULT_NOTE_TYPES)


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "note"


def capture(config: Config, type_: str, title: str, text: str = "", tags: list[str] | None = None,
            people: list[str] | None = None, projects: list[str] | None = None,
            sensitivity: str = "normal", confidence: str = "fact", source: str = "",
            source_date: str = "") -> Path:
    allowed = config.vocabulary.note_types
    if type_ not in allowed:
        raise ValueError(f"unknown type '{type_}', must be one of {sorted(allowed)}")

    today = dt.date.today().isoformat()
    slug = slugify(title)
    note_id = f"{type_}-{slug}"

    inbox = config.inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / f"{note_id}.md"
    n = 2
    while dest.exists():
        dest = inbox / f"{note_id}-{n}.md"
        n += 1

    meta_lines = [
        "---",
        f"id: {dest.stem}",
        f"type: {type_}",
        "status:",
        f"created: {today}",
        f"updated: {today}",
        f"people: {people or []}",
        f"projects: {projects or []}",
        f"tags: {tags or []}",
        f"sensitivity: {sensitivity}",
        f"source: {source}",
        f"source_date: {source_date}",
        f"confidence: {confidence}",
        "aliases: []",
        "---",
        "",
        f"# {title}",
        "",
        text,
        "",
    ]
    dest.write_text("\n".join(meta_lines), encoding="utf-8")
    return dest
