"""Stage 2 (SAKO Brain multi-user): query-time visibility enforcement.

Logical spaces — private and shared — are NOT separate vaults, separate
indexes, or separate installations. They are a projection over ONE canonical
Brain, decided per-record from two frontmatter fields:

    owner_principal: principal:marcin
    audience:
      - group:household
      - principal:ania

This module is the ONE shared policy every read path must call before it
materializes a record's title, snippet, content, or even its existence to a
caller — search, direct get/read, project registry, project context,
timeline, handoffs, the memory/inbox queue, `brain doctor`, the LLM
assistant, every MCP tool, every CLI command. Required flow, everywhere:

    candidate ids/ranking -> live authoritative visibility check
        -> allowed candidates only -> materialize title/snippet/content

Never materialize inaccessible content before authorization.

Like `identity.is_active()`, this reads each record's OWN frontmatter live,
every time — never a cached/index projection. The SQLite FTS index is
discovery/ranking only, never authoritative for a security decision: it may
be used to produce *candidate* ids, but never to decide whether a caller may
see what's behind one.

Audience entries use a type-prefixed reference form (`principal:<name>`,
`group:<name>`) distinct from this project's internal record ids
(`principal-<name>`, `group-<name>`) — the frontmatter convention the Stage 2
design settled on, not a simplification of it.
"""
from __future__ import annotations

from . import frontmatter
from .paths import DEFAULT_ACTING_PRINCIPAL, Config


class VisibilityError(ValueError):
    """A record's owner_principal/audience value doesn't parse."""


def _ref_to_id(ref: str) -> str:
    """'principal:ania' -> 'principal-ania'; 'group:household' -> 'group-household'."""
    kind, sep, name = str(ref).partition(":")
    if not sep or kind not in ("principal", "group") or not name:
        raise VisibilityError(
            f"malformed reference {ref!r} — expected 'principal:<name>' or 'group:<name>'"
        )
    return f"{kind}-{name}"


def id_to_ref(record_id: str) -> str:
    """'principal-marcin' -> 'principal:marcin' — for writing frontmatter."""
    kind, _, name = record_id.partition("-")
    return f"{kind}:{name}"


# What an un-migrated or malformed record's owner is taken to be. Matches the
# migration's own default exactly, so a record this module sees before
# migration behaves identically to one it sees after — never more visible
# either way. See DEFAULT_ACTING_PRINCIPAL (paths.py): the same identity
# Stage 1 already treats as the default caller.
DEFAULT_OWNER_ID = DEFAULT_ACTING_PRINCIPAL


def owner_and_audience(meta: dict) -> tuple[str, list[str]]:
    """Reads owner_principal/audience straight off a record's own frontmatter
    dict — never the index, never a cache. A missing owner_principal fails
    closed to DEFAULT_OWNER_ID (never to "visible to everyone"); a malformed
    reference fails closed by raising, which callers treat as "not visible"
    rather than letting a bad record become an open one."""
    owner_ref = meta.get("owner_principal") or id_to_ref(DEFAULT_OWNER_ID)
    owner_id = _ref_to_id(owner_ref)

    audience_raw = meta.get("audience") or []
    if isinstance(audience_raw, str):
        audience_raw = [audience_raw]
    audience_ids = [_ref_to_id(a) for a in audience_raw]

    return owner_id, audience_ids


def can_view(config: Config, principal_id: str, meta: dict) -> bool:
    """The one authorization decision every read path must make before
    materializing a record's title/snippet/content.

    `principal_id` must be the caller's TRUSTED identity — resolved by the
    SSH dispatcher from its own authorized_keys-fixed --principal flag
    (never anything a client sent), surfaced here as `config.acting_principal`.
    `meta` must be the record's own frontmatter dict, read live by the
    caller immediately before this call — never an index/cache projection.

    A malformed owner_principal/audience value denies rather than raising
    past the caller — a broken record must never become an open one."""
    try:
        owner_id, audience_ids = owner_and_audience(meta)
    except VisibilityError:
        return False

    if principal_id == owner_id:
        return True
    if principal_id in audience_ids:
        return True

    group_refs = {a for a in audience_ids if a.startswith("group-")}
    if group_refs:
        # Local import: identity.py itself imports capture.slugify, so a
        # module-level `from . import identity` here would make
        # capture.py (which needs visibility.id_to_ref) <-> identity.py <->
        # visibility.py a real circular import. Deferred to call time,
        # same reasoning as find_note_path's own local import below.
        from . import identity
        caller_groups = set(identity.groups_for_principal(config, principal_id))
        if caller_groups & group_refs:
            return True

    return False


def can_view_note(config: Config, principal_id: str, note: "frontmatter.Note") -> bool:
    """Convenience wrapper for callers that already have a parsed Note."""
    return can_view(config, principal_id, note.meta)


def filter_visible(config: Config, principal_id: str,
                    notes: "list[frontmatter.Note]") -> "list[frontmatter.Note]":
    """Filters a list of already-parsed Notes down to the ones `principal_id`
    may see. Does not re-read anything — callers that only have ids should
    resolve to Notes (reading frontmatter live) before calling this, never
    pass index-derived metadata."""
    return [n for n in notes if can_view_note(config, principal_id, n)]


def read_visible_note_text(config: Config, principal_id: str, note_id: str) -> str:
    """The authorized counterpart to update.find_note_path (a pure,
    unauthenticated id -> path primitive used internally by write
    functions, which must keep resolving regardless of visibility — Stage
    2 only gates reads). This is THE function every direct get/read path
    (`brain get`, the MCP `read_memory` tool, `project_context`'s own
    record text) must call instead of reading a resolved path directly —
    "authorize BEFORE reading/materializing record content".

    Raises FileNotFoundError identically whether the note doesn't exist at
    all or merely isn't visible to `principal_id` — a caller must never be
    able to tell the two apart from this alone; that distinction would
    itself leak the note's existence."""
    # Local import: avoids update.py <-> visibility.py becoming a real
    # import cycle (update.py has no reason to import this module back).
    from .update import find_note_path

    path = find_note_path(config, note_id)
    if path is None:
        raise FileNotFoundError(f"no note with id '{note_id}'")
    try:
        note = frontmatter.parse_file(path)
    except frontmatter.FrontmatterError:
        raise FileNotFoundError(f"no note with id '{note_id}'")
    if not can_view_note(config, principal_id, note):
        raise FileNotFoundError(f"no note with id '{note_id}'")
    return path.read_text(encoding="utf-8")
