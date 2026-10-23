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
import unicodedata
from pathlib import Path

from . import audit
from . import frontmatter
from . import indexer
from . import visibility
from .paths import DEFAULT_NOTE_TYPES, Config

# Kept as the *shipped default* only, for callers that need a vocabulary before
# a vault is resolved (argparse help text). The authority at capture time is
# `config.vocabulary.note_types`, which a vault's config.yaml can extend.
ALLOWED_TYPES = frozenset(DEFAULT_NOTE_TYPES)


# Letters that NFKD does not split into a base letter plus a mark, so they
# would otherwise be dropped whole ("słownym" became "s-ownym").
_NO_DECOMPOSITION = str.maketrans({
    "ł": "l", "đ": "d", "ø": "o", "ħ": "h", "ı": "i",
    "ß": "ss", "æ": "ae", "œ": "oe", "þ": "th", "ð": "d",
})


def slugify(text: str) -> str:
    """ASCII id fragment for a title. Accented letters keep their base
    letter ("dowodów" → "dowodow") rather than splitting the word at a
    hyphen; anything with no Latin base (CJK, emoji) still becomes a
    separator. Only new ids are affected — an existing note keeps the id
    its file already carries."""
    text = text.strip().lower().translate(_NO_DECOMPOSITION)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "note"


def capture(config: Config, type_: str, title: str, text: str = "", tags: list[str] | None = None,
            people: list[str] | None = None, projects: list[str] | None = None,
            sensitivity: str = "normal", confidence: str = "fact", source: str = "",
            source_date: str = "", audience: list[str] | None = None) -> Path:
    allowed = config.vocabulary.note_types
    if type_ not in allowed:
        raise ValueError(f"unknown type '{type_}', must be one of {sorted(allowed)}")

    today = dt.date.today().isoformat()
    slug = slugify(title)
    note_id = f"{type_}-{slug}"

    inbox = config.inbox_dir
    if not frontmatter.is_memory_path(inbox, config.brain_root):
        raise ValueError("ordinary memory cannot target protected or outside-vault paths")
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / f"{note_id}.md"
    n = 2
    while dest.exists():
        dest = inbox / f"{note_id}-{n}.md"
        n += 1

    if not frontmatter.is_memory_path(dest, config.brain_root):
        raise ValueError("ordinary memory cannot target protected or outside-vault paths")

    meta_lines = [
        "---",
        f"id: {dest.stem}",
        f"type: {type_}",
        # An idea waits for its owner from the start, as in create_memory;
        # anything else is an unreviewed capture with no status.
        f"status: {'open' if type_ == 'idea' else ''}".rstrip(),
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
        f"owner_principal: {visibility.id_to_ref(config.acting_principal)}",
        f"audience: {audience or []}",
        "---",
        "",
        f"# {title}",
        "",
        text,
        "",
    ]
    dest.write_text("\n".join(meta_lines), encoding="utf-8")
    indexer.index_note(config, dest)
    audit.log_event(config, event="note.write", principal_id=config.acting_principal,
                     **audit.caller_provenance(config),
                     detail=f"id={dest.stem}")
    return dest
