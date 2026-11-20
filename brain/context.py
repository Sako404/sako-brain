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
import unicodedata
from dataclasses import asdict, dataclass, field

from .indexer import connect
from . import search as search_mod
from . import timeline as timeline_mod
from .paths import Config
from .registry import load_visible_registry

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


# --------------------------------------------------------------------------
# Matching the question to projects and timeline events.
#
# Until 0.24.1 both sections matched any query word as a SUBSTRING of a
# project's id/name/aliases or an event's title/id, with no stopwords and
# no punctuation handling: "a" is inside "lechia", "for" inside "platform",
# so a question like "Design a website for a client" listed 34 of 37
# projects and the first 10 events, while "Jaki jest status TRON?" listed
# no project at all ("tron?" is not inside "project-tron"). Words are now
# compared whole, after case and diacritic folding, without English or
# Polish function words, and a project the question names outright (its
# whole name, an alias or its id slug) takes precedence over projects that
# merely share a word with the question.
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+")
_PHRASE_RE = re.compile(r"[^\W_]+(?:[-'.][^\W_]+)*")

# English: the same words `brain search` drops, so English questions reach
# the search exactly as before. Polish: TRON forwards Matrix messages as
# they are written, and Brain notes are written in both languages.
_PL_STOPWORDS = frozenset({
    "a", "i", "o", "u", "w", "z", "we", "ze", "na", "do", "od", "po", "za", "przy",
    "przez", "dla", "bez", "nad", "pod", "jest", "sa", "byl", "byla", "bylo", "byc",
    "sie", "to", "ten", "ta", "te", "tego", "tej", "czy", "jak", "jaki", "jaka",
    "jakie", "co", "kto", "gdzie", "kiedy", "dlaczego", "czemu", "ktory", "ktora",
    "ktore", "oraz", "albo", "lub", "ale", "nie", "tak", "juz", "jeszcze", "mi",
    "mnie", "ja", "ty", "on", "ona", "my", "wy", "oni", "moj", "moja", "moje",
})
_EN_EXTRA = frozenset({"can", "could", "should", "would", "will", "please", "there",
                       "here", "into", "not", "no", "my", "any", "some", "all"})
STOPWORDS = frozenset(search_mod.STOPWORDS) | _PL_STOPWORDS | _EN_EXTRA

# Words that ask about a subject's state rather than name it, and the word
# every project carries. They never pick a project or an event on their own.
_META_WORDS = frozenset({"status", "stan", "next", "latest", "update", "updates",
                         "progress", "finish", "finished", "dalej", "idzie", "news"})
_GENERIC_PROJECT_WORDS = frozenset({"project", "projects", "projekt", "projektu",
                                    "projekty", "projektem", "projektow"})
# A single shared word must be at least this long to count ("uk", "ai" are not).
_MIN_WORD = 3


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").replace("ł", "l").replace("Ł", "L"))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(_fold(text))


def content_words(query: str) -> list[str]:
    """Folded words of the question that can carry a subject."""
    return [w for w in _words(query) if len(w) >= 2 and w not in STOPWORDS]


def search_query(query: str) -> str:
    """The question as handed to the search: its own words, in order, minus
    function words and one-character tokens. Empty when nothing is left."""
    kept = []
    for phrase in _PHRASE_RE.findall(query or ""):
        if any(len(w) >= 2 and w not in STOPWORDS for w in _words(phrase)):
            kept.append(phrase)
    return " ".join(kept)


def _same_word(a: str, b: str) -> bool:
    """Equal, or one is the other with a short ending added (TRON/TRON-a,
    backup/backupu, website/websites): both at least five letters long."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 5 and long_.startswith(short) and len(long_) - len(short) <= 3


def _contains_phrase(phrase: list[str], words: list[str]) -> bool:
    n = len(phrase)
    return n > 0 and any(words[i:i + n] == phrase for i in range(len(words) - n + 1))


def _label_words(label: str) -> list[str]:
    return [w for w in _words(label) if w not in STOPWORDS]


def _project_labels(entry) -> list[list[str]]:
    slug = entry.id[len("project-"):] if (entry.id or "").startswith("project-") else (entry.id or "")
    labels = [entry.name or "", *(entry.aliases or []), slug]
    return [lw for lw in (_label_words(l) for l in labels) if lw]


def match_projects(entries, query: str) -> tuple[list, bool]:
    """(matching registry entries, best first; True if the question names at
    least one of them outright). A project named outright — its whole name,
    an alias or its id slug appears in the question — is returned with the
    other named projects only. Otherwise a project matches on shared words
    (at least _MIN_WORD letters, not a status word), most shared first."""
    words = content_words(query)
    named, shared = [], []
    for pos, e in enumerate(entries):
        labels = _project_labels(e)
        longest = max((len(lw) for lw in labels if _contains_phrase(lw, words)), default=0)
        if longest:
            named.append((-longest, pos, e))
            continue
        hits = {w for w in words if len(w) >= _MIN_WORD and w not in _META_WORDS
                and w not in _GENERIC_PROJECT_WORDS
                and any(_same_word(w, lw) for label in labels for lw in label)}
        if hits:
            shared.append((-len(hits), pos, e))
    if named:
        return [e for *_, e in sorted(named, key=lambda x: x[:2])], True
    return [e for *_, e in sorted(shared, key=lambda x: x[:2])], False


_EVENT_ID_PREFIX_RE = re.compile(r"^event-\d{4}-\d{2}-\d{2}-")


def match_timeline(entries, query: str, limit: int, projects=(), named: bool = False) -> list:
    """Timeline entries for the question, best first, at most `limit`.

    If the question names projects outright, the events are those whose
    title (or id slug) names one of them; when some of those also share the
    question's other words, only those are kept. Otherwise an event must
    share every word of a one-word question, or at least two words of a
    longer one. Ties keep the timeline's own order (newest first)."""
    words = content_words(query)
    subject = sorted({w for w in words if len(w) >= _MIN_WORD and w not in _META_WORDS
                      and w not in _GENERIC_PROJECT_WORDS})
    project_labels = [lw for p in projects for lw in _project_labels(p)] if named else []
    label_words = {w for lw in project_labels for w in lw}
    scored = []
    for pos, e in enumerate(entries):
        ev_words = _words(e.title) + _words(_EVENT_ID_PREFIX_RE.sub("", e.id or ""))
        ev_set = set(ev_words)
        if named:
            if not any(_contains_phrase(lw, ev_words) for lw in project_labels):
                continue
            extra = sum(1 for w in subject if w not in label_words and any(_same_word(w, x) for x in ev_set))
            scored.append((-extra, pos, e))
        else:
            hits = sum(1 for w in subject if any(_same_word(w, x) for x in ev_set))
            if hits and hits >= min(2, len(subject)):
                scored.append((-hits, pos, e))
    scored.sort(key=lambda x: x[:2])
    if named and scored and scored[0][0] < 0:
        scored = [s for s in scored if s[0] < 0]
    return [e for *_, e in scored[:limit]]


def get_context(config: Config, query: str, limit: int = 10,
                include_projects: bool = True, include_timeline: bool = True,
                include_restricted: bool = False, type: str | None = None,
                tag: str | None = None) -> ContextResult:
    """Context for one question. `type` / `tag` narrow every section the
    same way `brain list --type/--tag` does: notes must match them; the
    projects section is kept only for type `project` and the timeline only
    for type `event` (neither carries tags, so a tag filter leaves both
    empty). A question with no subject words left (only function words or
    single letters) returns nothing rather than arbitrary notes."""
    result = ContextResult(query=query)
    if type:
        from .listing import _validate_type
        _validate_type(config, type)
    q = search_query(query)
    if not q:
        return result

    conn = connect(config)
    try:
        # Search a bit wider than `limit` so filtering out restricted notes
        # (the common case) still leaves `limit` usable results.
        raw = search_mod.search(config, q, limit=limit * 2, type=type, tag=tag)
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

    want_projects = include_projects and not tag and type in (None, "project")
    want_timeline = include_timeline and not tag and type in (None, "event")
    matched, named = ([], False)
    if want_projects or want_timeline:
        matched, named = match_projects(load_visible_registry(config, config.acting_principal), query)
    if want_projects:
        result.projects = [{"id": e.id, "name": e.name, "status": e.status, "path": e.path} for e in matched]
    if want_timeline:
        events = match_timeline(timeline_mod.list_timeline(config), query, limit, matched, named)
        result.timeline = [{"id": e.id, "date": e.date, "title": e.title} for e in events]

    return result
