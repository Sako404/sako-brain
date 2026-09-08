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
from dataclasses import asdict, dataclass, field

from .indexer import connect
from . import search as search_mod
from . import timeline as timeline_mod
from .paths import Config
from .registry import load_registry


def _clean(value) -> str:
    """DB fields can legitimately be '' (see indexer._str_field) — this
    just guards against any stray literal-None text reaching a client."""
    if value is None or value == "None":
        return ""
    return str(value)


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
            result.notes.append(ContextItem(
                id=row["id"], type=row["type"] or "", title=row["title"] or "",
                snippet=r.snippet, status=_clean(row["status"]),
                is_current=_is_current(_clean(row["status"]), row["valid_to"]),
                sensitivity=sensitivity, source=_clean(row["source"]),
                source_date=_clean(row["source_date"]), confidence=_clean(row["confidence"]),
            ))
            if len(result.notes) >= limit:
                break
    finally:
        conn.close()

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
