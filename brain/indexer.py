"""SQLite FTS5 index — a rebuildable cache over the Markdown vault.

Markdown is always authoritative. This module never needs to be trusted
across a Markdown edit made outside `brain`; re-run `brain index` instead.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import frontmatter
from .paths import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    type TEXT,
    status TEXT,
    title TEXT,
    path TEXT,
    created TEXT,
    updated TEXT,
    tags TEXT,
    people TEXT,
    projects TEXT,
    sensitivity TEXT,
    confidence TEXT,
    source TEXT,
    source_date TEXT,
    valid_from TEXT,
    valid_to TEXT,
    supersedes TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    id UNINDEXED,
    title,
    body,
    tags,
    tokenize = 'porter unicode61'
);
"""


def _str_field(meta: dict, key: str) -> str:
    """`meta.get(key, "")` only defaults when the key is absent — if the key
    is present with an empty YAML value (`valid_to:` with nothing after
    it, common in our templates), `.get` returns None and `str(None)`
    would store the literal text "None". Treat both cases as ''."""
    value = meta.get(key)
    return "" if value is None else str(value)


def connect(config: Config) -> sqlite3.Connection:
    # The state directory lives outside the vault (see paths.default_state_dir),
    # so it may not exist yet on a first run — create the DB's own parent.
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def rebuild(config: Config) -> dict:
    """Wipe and rebuild the index from Markdown. Returns a stats dict."""
    config.db_path.unlink(missing_ok=True)
    conn = connect(config)
    conn.executescript(SCHEMA)

    indexed, errors = 0, []
    for path in frontmatter.iter_markdown_files(config.brain_root, config.content_dirs):
        try:
            note = frontmatter.parse_file(path)
        except frontmatter.FrontmatterError as exc:
            errors.append(str(exc))
            continue

        if not note.id:
            errors.append(f"{path}: missing 'id' field, skipped")
            continue

        tags = ",".join(note.list_field("tags"))
        people = ",".join(note.list_field("people"))
        projects = ",".join(note.list_field("projects"))
        rel_path = str(path.relative_to(config.brain_root))

        supersedes = note.meta.get("supersedes") or ""
        if isinstance(supersedes, list):
            supersedes = ",".join(str(s) for s in supersedes)

        conn.execute(
            """INSERT OR REPLACE INTO notes
               (id, type, status, title, path, created, updated, tags, people, projects, sensitivity, confidence,
                source, source_date, valid_from, valid_to, supersedes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                note.id, note.type, note.status, note.title, rel_path,
                _str_field(note.meta, "created"), _str_field(note.meta, "updated"),
                tags, people, projects,
                note.meta.get("sensitivity", "normal"), note.meta.get("confidence", ""),
                _str_field(note.meta, "source"), _str_field(note.meta, "source_date"),
                _str_field(note.meta, "valid_from"), _str_field(note.meta, "valid_to"),
                str(supersedes),
            ),
        )
        conn.execute("DELETE FROM notes_fts WHERE id = ?", (note.id,))
        conn.execute(
            "INSERT INTO notes_fts (id, title, body, tags) VALUES (?, ?, ?, ?)",
            (note.id, note.title, note.body, tags),
        )
        indexed += 1

    conn.commit()
    conn.close()
    return {"indexed": indexed, "errors": errors}
