"""SQLite FTS5 index — a rebuildable cache over the Markdown vault.

Markdown is always authoritative. This module never needs to be trusted
across a Markdown edit made outside `brain`; re-run `brain index` instead.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import frontmatter
from .paths import Config, ensure_private_file

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
    # sqlite3.connect() creates the file (if missing) under the process
    # umask like any other `open()` — commonly 022, i.e. world-readable —
    # exactly the log-file bug `ensure_private_file` already exists to fix
    # (see paths.py), just never applied here. This index holds full note
    # bodies, restricted ones included, so it deserves the same treatment,
    # applied at the one choke point every caller already goes through.
    ensure_private_file(config.db_path)
    return conn


def count_by_type(config: Config) -> dict[str, int]:
    """Indexed note counts grouped by type, in the same order the index
    returns them (`(none)` for a missing/blank type). {} if the index has not
    been built yet."""
    if not config.db_path.exists():
        return {}
    conn = connect(config)
    try:
        rows = conn.execute(
            "SELECT type, COUNT(*) c FROM notes GROUP BY type ORDER BY type"
        ).fetchall()
    finally:
        conn.close()
    return {(row["type"] or "(none)"): row["c"] for row in rows}


def count_inbox_pending(config: Config) -> int:
    """Markdown files sitting in the inbox, not yet triaged into the vault."""
    inbox = config.inbox_dir
    if not inbox.exists():
        return 0
    return len(list(inbox.glob("*.md")))


def _upsert_note(conn: sqlite3.Connection, config: Config, path: Path) -> str | None:
    """Index exactly one markdown file into an already-open connection.
    Returns the note's id on success, or None if it was skipped (parse
    error, or no `id` field) — the caller decides how to report that.
    Shared by `rebuild()` (one connection, every file) and `index_note()`
    (one connection, one file, called right after a write) so there is
    exactly one place that knows how a note's frontmatter maps onto the
    `notes`/`notes_fts` schema — never two copies to drift apart."""
    try:
        note = frontmatter.parse_file(path)
    except frontmatter.FrontmatterError:
        return None
    if not note.id:
        return None

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
    return note.id


def rebuild(config: Config) -> dict:
    """Wipe and rebuild the index from Markdown. Returns a stats dict."""
    config.db_path.unlink(missing_ok=True)
    conn = connect(config)
    conn.executescript(SCHEMA)

    indexed, errors = 0, []
    for path in frontmatter.iter_markdown_files(config.brain_root, config.content_dirs):
        try:
            note_id = _upsert_note(conn, config, path)
        except frontmatter.FrontmatterError as exc:
            errors.append(str(exc))
            continue
        if note_id is None:
            errors.append(f"{path}: missing 'id' field, skipped")
            continue
        indexed += 1

    conn.commit()
    conn.close()
    return {"indexed": indexed, "errors": errors}


def index_note(config: Config, path: Path) -> str | None:
    """Incrementally bring the index up to date for exactly one note,
    without touching any other row — safe and cheap to call after every
    single write (`remember`, `update`, `project create/update/close/
    section-update`, `decision create`, `timeline add`, `note create`,
    `memory accept`, `handoff write`), so a write's effect on search/
    context is visible immediately rather than only after a later
    `brain index`. Returns the indexed note's id, or None if the file
    couldn't be parsed/had no id (mirrors `rebuild()`'s own skip
    behavior; callers treat this as non-fatal, matching every existing
    write command's own error handling for its primary write).

    The index stays what it has always been — a rebuildable cache, never
    the authoritative copy — this only shrinks the window during which it
    lags the Markdown that actually matters."""
    conn = connect(config)
    try:
        conn.executescript(SCHEMA)  # no-op if already created; safe on a fresh db_path too
        note_id = _upsert_note(conn, config, path)
        conn.commit()
        return note_id
    finally:
        conn.close()
