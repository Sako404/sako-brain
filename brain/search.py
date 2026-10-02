"""Query the FTS5 index."""
from __future__ import annotations

from dataclasses import dataclass

from . import frontmatter
from . import visibility
from .indexer import connect
from .paths import Config


@dataclass
class SearchResult:
    id: str
    type: str
    status: str
    title: str
    path: str
    snippet: str


# Phase 5B finding: a natural-language question ("Where did Family Command
# Center finish?") returned almost nothing under the old implicit-AND
# matching, because every word — including "where"/"did"/"finish" — was a
# required term. A keyword query typed by a human ("Family Command
# Center") never hits this, but a chat-style client (the Ollama demo, or
# a future ChatGPT bridge) sends full questions. Strip common
# function/question words before matching.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "did", "do", "does", "done",
    "where", "what", "when", "who", "whom", "how", "why", "which",
    "that", "this", "these", "those", "it", "its", "to", "of", "in", "on",
    "for", "with", "about", "at", "by", "from", "as", "be", "been", "being",
    "and", "or", "but", "if", "than", "so", "we", "i", "you", "he", "she",
    "they", "them", "his", "her", "our", "your", "their", "us", "me",
}


def _clean_terms(raw: str) -> list[str]:
    raw_terms = [t.strip(".,!?;:\"'") for t in raw.strip().split()]
    raw_terms = [t for t in raw_terms if t]
    return [t for t in raw_terms if t.lower() not in STOPWORDS] or raw_terms


def _escape_term(t: str) -> str:
    return f'"{t.replace(chr(34), chr(34) * 2)}"*'


def _and_expr(terms: list[str]) -> str:
    return " ".join(_escape_term(t) for t in terms)


def _or_expr(terms: list[str]) -> str:
    return " OR ".join(_escape_term(t) for t in terms)


# Weights for bm25(), one per column IN TABLE-DECLARATION ORDER — this
# includes `id`, even though it's UNINDEXED and never actually matches
# text (SQLite still reserves a weight slot for it). Columns are:
# id, title, body, tags.
#
# Phase 5A finding: with FTS5's default equal weighting, a query matching
# a note's own title (e.g. "Family Command Center" against the project
# record literally titled that) lost badly (ranked ~12th) to short,
# unrelated notes that just mention the phrase once in passing — BM25's
# document-length normalization means a long, comprehensive project
# record is systematically penalized relative to a two-paragraph note
# with the same term hits. Weighting title matches heavily fixes exactly
# this class of failure without changing the underlying engine. (An
# earlier version of this fix passed only 3 weight args, which silently
# misaligned onto id/title/body instead of title/body/tags — verified the
# correct 4-argument mapping empirically before landing this.)
ID_WEIGHT = 0.0     # no effect either way — id has no text to match
TITLE_WEIGHT = 10.0
BODY_WEIGHT = 1.0
TAGS_WEIGHT = 3.0


def _run_match(conn, match_expr: str, limit: int):
    return conn.execute(
        """
        SELECT n.id, n.type, n.status, n.title, n.path,
               snippet(notes_fts, 2, '[', ']', '...', 10) AS snip
        FROM notes_fts
        JOIN notes n ON n.id = notes_fts.id
        WHERE notes_fts MATCH ?
        ORDER BY bm25(notes_fts, ?, ?, ?, ?)
        LIMIT ?
        """,
        (match_expr, ID_WEIGHT, TITLE_WEIGHT, BODY_WEIGHT, TAGS_WEIGHT, limit),
    ).fetchall()


# Stage 2 (multi-user visibility): the index is discovery/ranking only, never
# authoritative for a security decision — see brain/visibility.py. Candidate
# ids come from the FTS query exactly as before; this module then fetches
# MORE of them than `limit` asks for, so that filtering out ones the caller
# may not see (a live, per-record check — never the index's own "sensitivity"
# field or any other cached projection) still leaves `limit` usable results
# where the vault actually has that many. Title/snippet are only ever
# attached to the final SearchResult list AFTER that check — never materialize
# inaccessible content before authorization.
_OVERFETCH = 4


def search(config: Config, query: str, limit: int = 20) -> list[SearchResult]:
    """Tiered query relaxation, strictest first:

    1. AND of every content term — most precise, right answer for a clean
       keyword query like "Family Command Center".
    2. AND with exactly one term dropped, tried once per term. Phase 5B
       finding: a single rare/incidental word in an otherwise-good query
       (e.g. "finish" in "Where did Family Command Center finish?") can
       make tier 1 match nothing relevant, or make a full-OR match get
       swamped by that one rare word's high IDF score in unrelated notes.
       Dropping it one at a time recovers the clean multi-word match
       without discarding precision entirely.
    3. Full OR across all terms — last resort, guarantees *something*
       rather than nothing once tiers 1-2 are exhausted.

    Each tier only runs enough to top up to an internal, overfetched
    candidate limit; already-seen ids are never duplicated, and stricter
    tiers always rank above looser ones. Results are then filtered down to
    what `config.acting_principal` may see, and only THEN cut to `limit`.
    """
    terms = _clean_terms(query)
    if not terms:
        return []

    fetch_limit = limit * _OVERFETCH

    conn = connect(config)
    try:
        seen: set[str] = set()
        rows = []

        def _add(new_rows):
            for r in new_rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    rows.append(r)

        if len(terms) > 1:
            _add(_run_match(conn, _and_expr(terms), fetch_limit))

        if len(rows) < fetch_limit and len(terms) > 2:
            for i in range(len(terms)):
                if len(rows) >= fetch_limit:
                    break
                relaxed = terms[:i] + terms[i + 1:]
                _add(_run_match(conn, _and_expr(relaxed), fetch_limit - len(rows)))

        if len(rows) < fetch_limit:
            _add(_run_match(conn, _or_expr(terms), fetch_limit - len(rows)))

        rows = rows[:fetch_limit]
    finally:
        conn.close()

    results = []
    for r in rows:
        if len(results) >= limit:
            break
        note_path = config.brain_root / r["path"]
        try:
            note = frontmatter.parse_file(note_path)
        except (frontmatter.FrontmatterError, OSError):
            continue
        if not visibility.can_view_note(config, config.acting_principal, note):
            continue
        results.append(SearchResult(r["id"], r["type"], r["status"], r["title"], r["path"], r["snip"]))

    return results


def get_note_row(config: Config, note_id: str):
    conn = connect(config)
    try:
        return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    finally:
        conn.close()
