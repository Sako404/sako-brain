"""get_context — one higher-level, concise context-retrieval operation.

Built so a smaller/local model (or any client that shouldn't have to do
its own multi-step search -> rank -> read dance) can get relevant Brain
context in a single call: search, rank, and return compact structured
results — id/title/snippet/provenance, current-vs-historical status —
never full note bodies. If a caller genuinely needs the full text of one
specific note, that's a separate `read_memory` call, by design (avoids
dumping the whole Brain into context for one question).

Restricted-sensitivity notes are excluded by default (`include_restricted=
False`) — this endpoint is meant for lower-trust/less-nuanced local
clients (e.g. a small Ollama model), not a replacement for
`search_memory`/`read_memory`, which Claude Code continues to use directly
and unchanged from Phase 5A.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field

from .indexer import connect
from . import search as search_mod
from . import timeline as timeline_mod
from .paths import Config
from .registry import load_registry

# Handoffs prepend "## Session <date>" (newest first); `brain update
# --append-text` appends "## Update (<date>)" (newest last) — opposite
# conventions, so "latest" must come from the date each heading carries,
# never from position in the file. Matches either heading shape.
_DATED_SECTION_RE = re.compile(
    r"^## (?:Session|Update) \(?(\d{4}-\d{2}-\d{2})\)?", re.MULTILINE
)


def _latest_dated_section(body: str) -> str | None:
    """A note that accumulates sessions/updates over time (handoffs; any
    note carrying a `brain update --append-text` correction) holds several
    generations of content in one file/one FTS row. SQLite's `snippet()`
    has no notion of that — it returns whichever window best matches the
    query terms, old or new. This finds the section whose own heading date
    is latest and returns just that section's text, so a caller asking
    "what's current" is never handed a stale section purely because it
    happened to score better textually. Storage/history is untouched:
    this only changes what a read-time caller is shown."""
    matches = list(_DATED_SECTION_RE.finditer(body))
    if not matches:
        return None
    latest = max(matches, key=lambda m: m.group(1))
    start = latest.start()
    end_candidates = [m.start() for m in matches if m.start() > start]
    end = min(end_candidates) if end_candidates else len(body)
    return body[start:end].strip()


def _clean(value) -> str:
    """DB fields can legitimately be '' (see indexer._str_field) — this
    just guards against any stray literal-None text reaching a client."""
    if value is None or value == "None":
        return ""
    return str(value)


# Cap on how much of a note's own "latest dated section" (see
# _latest_dated_section) is shown as a snippet override — keeps this a
# snippet, not a full-body dump, matching what a raw FTS snippet already
# looks like to a caller.
_SECTION_SNIPPET_CHARS = 400


@dataclass
class ContextItem:
    id: str
    type: str
    title: str
    snippet: str
    status: str = ""
    is_current: bool = True
    sensitivity: str = "normal"
    source: str = ""
    source_date: str = ""
    confidence: str = ""
    updated: str = ""
    snippet_from_latest_section: bool = False


@dataclass
class ContextResult:
    query: str
    notes: list = field(default_factory=list)      # list[ContextItem]
    projects: list = field(default_factory=list)    # list[dict]
    timeline: list = field(default_factory=list)    # list[dict]
    restricted_omitted: int = 0

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "notes": [asdict(n) for n in self.notes],
            "projects": self.projects,
            "timeline": self.timeline,
            "restricted_omitted": self.restricted_omitted,
        }


def _is_current(status: str, valid_to: str) -> bool:
    if status == "superseded":
        return False
    valid_to = _clean(valid_to)
    if valid_to:
        try:
            if dt.date.fromisoformat(valid_to) < dt.date.today():
                return False
        except ValueError:
            pass
    return True


def get_context(config: Config, query: str, limit: int = 10,
                 include_projects: bool = True, include_timeline: bool = True,
                 include_restricted: bool = False) -> ContextResult:
    result = ContextResult(query=query)

    conn = connect(config)
    try:
        # Search a bit wider than `limit` so filtering out restricted notes
        # (the common case) still leaves `limit` usable results.
        raw = search_mod.search(config, query, limit=limit * 2)
        for r in raw:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (r.id,)).fetchone()
            if row is None:
                continue
            sensitivity = row["sensitivity"] or "normal"
            if sensitivity == "restricted" and not include_restricted:
                result.restricted_omitted += 1
                continue

            snippet = r.snippet
            snippet_from_latest_section = False
            fts_row = conn.execute("SELECT body FROM notes_fts WHERE id = ?", (r.id,)).fetchone()
            if fts_row is not None:
                latest_section = _latest_dated_section(fts_row["body"] or "")
                if latest_section:
                    snippet = latest_section[:_SECTION_SNIPPET_CHARS]
                    if len(latest_section) > _SECTION_SNIPPET_CHARS:
                        snippet += "..."
                    snippet_from_latest_section = True

            result.notes.append(ContextItem(
                id=row["id"], type=row["type"] or "", title=row["title"] or "",
                snippet=snippet, status=_clean(row["status"]),
                is_current=_is_current(_clean(row["status"]), row["valid_to"]),
                sensitivity=sensitivity, source=_clean(row["source"]),
                source_date=_clean(row["source_date"]), confidence=_clean(row["confidence"]),
                updated=_clean(row["updated"]),
                snippet_from_latest_section=snippet_from_latest_section,
            ))
            if len(result.notes) >= limit:
                break
    finally:
        conn.close()

    # Current-first, otherwise stable: history is never dropped (a
    # superseded note can still be exactly what was asked for), it just
    # never silently outranks a current one that matched equally well.
    result.notes.sort(key=lambda n: not n.is_current)

    if include_projects:
        terms = [t for t in query.lower().split() if t]
        for e in load_registry(config):
            haystack = " ".join([e.id or "", e.name or "", " ".join(e.aliases or [])]).lower()
            if terms and any(t in haystack for t in terms):
                result.projects.append({"id": e.id, "name": e.name, "status": e.status, "path": e.path})

    if include_timeline:
        terms = [t for t in query.lower().split() if t]
        entries = timeline_mod.list_timeline(config)
        matched = [e for e in entries if terms and any(t in e.title.lower() or t in e.id.lower() for t in terms)]
        result.timeline = [{"id": e.id, "date": e.date, "title": e.title} for e in matched[:limit]]

    return result
