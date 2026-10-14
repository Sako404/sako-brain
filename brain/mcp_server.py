"""Minimal local MCP server for the Sako Brain — stdlib only, stdio transport.

No `mcp` SDK is installed in this environment (no pip available), so this
implements the small newline-delimited JSON-RPC 2.0 subset of the Model
Context Protocol needed for `tools/list` and `tools/call` by hand. If the
official `mcp` Python package becomes available later, this can be swapped
for `mcp.server.fastmcp.FastMCP` with the same tool functions — see README.

Run with:  python3 -m brain.mcp_server
Never expose this over a network socket — stdio only, local use only.

Write-policy note: every write tool here (`remember`, `update_memory`,
`write_handoff`, `create_decision`, `create_project`, `update_project_status`,
`close_project`, `update_project_section`, `create_memory_note`,
`create_timeline_event`) is a thin wrapper over the same business-logic
function the CLI calls — no tool
duplicates logic the CLI doesn't also have. None bypasses
what a human typing the equivalent `brain` command would be subject to —
as of v0.10.1 this is literally true, not just aspirational: the
secret-pattern scan and the confirm_restricted gate both live in
writepolicy.py (shared with the CLI, which mcp_bridge.py shells out to
for every one of its own write tools), not duplicated here. Each
free-text-carrying tool runs the same secret-pattern scan `brain doctor`
uses (refusing outright on a match, the same as `brain git snapshot`/`brain
backup run` do); writing `sensitivity: restricted` content requires an
explicit `confirm_restricted: true` argument (the CLI's equivalent:
`--confirm-restricted`), so a restricted write can never happen as an
unnoticed side effect of a tool call. `queue_memory` exposes the
Level 2 pending-memory queue (see `memoryqueue.py`) for MCP-only clients —
the "durable-seeming but not explicitly confirmed" case — as a
non-authoritative staging step rather than an immediate write.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime
from pathlib import Path

from . import capture, context as context_mod, discover as discover_mod, memoryops, memoryqueue, projectops, projectsync
from . import decision as decision_mod
from . import handoff as handoff_mod
from . import listing
from . import search as search_mod
from . import state as state_mod
from . import timeline as timeline_mod
from . import update as update_mod
from . import rolepolicy
from . import validate as validate_mod
from . import visibility
from . import writepolicy
from . import paths as paths_mod
from . import __version__
from .paths import Config, default_config
from .registry import find_visible_project, load_visible_registry

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = paths_mod.APP_DIRNAME
SERVER_VERSION = __version__   # never duplicated — see brain/__init__.py

# Set once per process from the `initialize` request's clientInfo, if the
# client sends one — stdio is one connection per spawned process, so a
# module-level value is safe here (no cross-client leakage).
_CURRENT_CLIENT = "unknown"

# Result dict keys safe to log as a one-line "detail" — all are ids/paths,
# never free-text content (title/text/append_text/candidate_fact are never
# logged, on purpose).
_SAFE_DETAIL_KEYS = ("id", "created_path", "updated_path", "queued_id", "path")


# Shared write policy (secret-pattern scanning, restricted-confirmation)
# now lives in writepolicy.py — used by this server AND by the CLI write
# path the MCP bridge shells out to, so both transports enforce the same
# rule instead of one having it and the other silently not. Kept as a
# module-level alias so any code still catching McpWriteRefused by name
# keeps working.
McpWriteRefused = writepolicy.WritePolicyError


def _log_call(config: Config, tool: str, success: bool, result=None, error: str = "") -> None:
    """One redaction-safe line per tool call: timestamp, client, tool name,
    success/failure, and only an id/path-shaped detail if one is available
    in the result — never a query string, note title, or body text, and
    never full restricted content."""
    try:
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = config.logs_dir / f"mcp-{datetime.now():%Y-%m}.log"
        status = "OK" if success else "FAILED"
        line = f"{datetime.now().isoformat(timespec='seconds')}  client={_CURRENT_CLIENT}  tool={tool}  {status}"
        if success and isinstance(result, dict):
            for key in _SAFE_DETAIL_KEYS:
                if key in result and result[key]:
                    line += f"  {key}={result[key]}"
                    break
        elif not success and error:
            # Error messages here are our own refusal text (e.g. "possible
            # API key found" / "confirm_restricted required") — never the
            # caller's input — so safe to log as-is.
            line += f"  reason={error[:200]}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        paths_mod.ensure_private_file(log_path)
    except OSError:
        pass  # logging must never crash the server


def _read_note_text(config: Config, note_id: str) -> str:
    return visibility.read_visible_note_text(config, config.acting_principal, note_id)


_scan_for_secrets = writepolicy.scan_for_secrets
_require_restricted_confirmation = writepolicy.require_restricted_confirmation


# ---- tool implementations -------------------------------------------------

def tool_search_memory(config: Config, query: str, limit: int = 20) -> dict:
    """RANKED FULL-TEXT SEARCH over note text, truncated at `limit`. NOT a
    metadata filter and NOT a complete listing.

    A query like "status: proposed" is matched as the WORDS "status" and
    "proposed" against title/body/tags — so it returns records whose status
    is `decided` (their body text quotes the word, e.g. in a template
    heading or a superseded-history line) and omits `proposed` ones that
    never spell it out. Use `list_notes` for any question of the form "all
    X with status Y", "every open decision", "what is still pending" — it
    filters on frontmatter and reports its own completeness. Results here
    are ranked by relevance, so they are also NOT a stable or exhaustive
    enumeration of anything.
    """
    results = search_mod.search(config, query, limit=limit)
    return {"results": [r.__dict__ for r in results]}


def tool_list_notes(config: Config, type: str | None = None, status: str | None = None,
                     project: str | None = None, tag: str | None = None,
                     limit: int = listing.DEFAULT_LIMIT, offset: int = 0) -> dict:
    """COMPLETE listing by METADATA — the correct tool for "every decision
    with status proposed", "all notes for project X", or any other question
    that needs an exhaustive answer rather than the best text matches.

    Both discovery and filtering read canonical Markdown directly, never the
    search index, so a note written a second ago is listed now and a note
    deleted a second ago is not, with no reindex in between. Filters
    (`type`, `status`, `project`, `tag`) come from each note's live
    frontmatter, never its text, so the result does not depend on what the
    body happens to say. Ordering is deterministic: updated descending, then
    id ascending.

    Three completeness fields, and they mean different things:
    `source_complete` is false when a visible file could not be read as a note
    (`unreadable`) or lacks metadata (`unstructured_skipped`) — those are missing from
    the listing whatever the filters say. It is also false when traversal
    fails or a file's visibility cannot be established; these unknowns add
    no paths or counts. A known ACL denial does not affect the result.
    `page_complete` is false whenever
    `entries` is a window rather than the whole match set: any `offset > 0`,
    any `limit` that cut the result, the FINAL page, and an offset past the
    last match all count as windows. `complete` is both together, and is
    the only field that means "entries is the full answer" — never report a
    count as complete without it. `next_offset` continues the listing, or is
    null when nothing follows this page.

    `total_visible` counts only matches this caller may see. `status` is
    validated against the vault's own vocabulary, so a typo is refused
    instead of returning an empty list that reads like "none exist".
    """
    try:
        result = listing.list_notes(config, type=type, status=status, project=project,
                                     tag=tag, limit=limit, offset=offset)
    except listing.ListingError as exc:
        return {"error": str(exc)}
    return listing.result_to_dict(result)


def tool_get_context(config: Config, query: str, limit: int = 10,
                      include_projects: bool = True, include_timeline: bool = True,
                      include_restricted: bool = False) -> dict:
    """Higher-level context retrieval: search + rank + concise structured
    results (id/title/snippet/provenance/current-vs-historical), never full
    note bodies. Restricted notes excluded unless include_restricted=true."""
    result = context_mod.get_context(
        config, query, limit=limit, include_projects=include_projects,
        include_timeline=include_timeline, include_restricted=include_restricted,
    )
    return result.to_dict()


def tool_read_memory(config: Config, id: str) -> dict:
    """One full record by its exact id, resolved from Markdown (never the
    search index, so a note written a second ago is readable now).

    "Not found" is returned identically whether the id does not exist, its
    frontmatter does not parse, or the record simply is not visible to the
    calling principal — deliberately indistinguishable, so a caller cannot
    enumerate other people's records. Never report "no such record" when
    "not visible to this principal" is the honest statement.
    """
    return {"id": id, "content": _read_note_text(config, id)}


def tool_remember(config: Config, type: str, title: str, text: str = "",
                   tags: list | None = None, people: list | None = None,
                   projects: list | None = None, sensitivity: str = "normal",
                   confidence: str = "fact", source: str = "", source_date: str = "",
                   confirm_restricted: bool = False, audience: list | None = None) -> dict:
    """Raw capture into the inbox, for triage later — the deliberately
    unstructured write path. Use `create_memory_note` instead when the note
    already has an obvious final home, and the dedicated
    `create_decision`/`create_project`/`create_timeline_event` tools for
    those types. Secret-shaped content is refused outright, and
    `sensitivity="restricted"` requires `confirm_restricted=true`."""
    _require_restricted_confirmation(config, config.acting_principal, sensitivity, confirm_restricted)
    rolepolicy.require_audience_allowed(config, config.acting_principal, audience)
    _scan_for_secrets(title, text)
    path = capture.capture(
        config, type_=type, title=title, text=text, tags=tags, people=people,
        projects=projects, sensitivity=sensitivity, confidence=confidence, source=source,
        source_date=source_date, audience=audience,
    )
    return {"created_path": str(path.relative_to(config.brain_root))}


def tool_update_memory(config: Config, id: str, set_fields: dict | None = None,
                        append_text: str | None = None, confirm_restricted: bool = False) -> dict:
    """Set frontmatter fields and/or append to the body of one existing
    record. Never rewrites or deletes existing body content — history is
    appended to, never edited in place.

    Only the record's `owner_principal` may modify it; being in its
    `audience` grants read access only. An id claimed by more than one file
    is refused rather than resolved by preference, so a duplicate cannot
    quietly split a record's history across two files.
    """
    set_fields = set_fields or {}
    if set_fields.get("sensitivity") == "restricted":
        _require_restricted_confirmation(config, config.acting_principal, "restricted", confirm_restricted)
    if "audience" in set_fields:
        rolepolicy.require_audience_allowed(config, config.acting_principal, set_fields["audience"])
    _scan_for_secrets(append_text or "", *(str(v) for v in set_fields.values()))
    path = update_mod.update_memory(config, id, set_fields=set_fields, append_text=append_text)
    return {"updated_path": str(path.relative_to(config.brain_root))}


def tool_queue_memory(config: Config, candidate_fact: str, entities: list | None = None,
                       source: str = "", source_date: str = "", proposed_destination: str = "",
                       proposed_type: str = "fact", sensitivity: str = "normal",
                       confidence: str = "assumption", reason: str = "") -> dict:
    """Level 2 write policy: queue a plausible-but-unconfirmed durable fact
    for human review rather than writing it as an authoritative record."""
    _scan_for_secrets(candidate_fact, reason)
    entry = memoryqueue.add(
        config, candidate_fact=candidate_fact, entities=entities, source=source,
        source_date=source_date, proposed_destination=proposed_destination,
        proposed_type=proposed_type, sensitivity=sensitivity, confidence=confidence, reason=reason,
    )
    return {"queued_id": entry.id, "status": entry.status}


def tool_list_projects(config: Config) -> dict:
    """Every registered project this caller may see, with its id, name,
    status and working-directory path. The registry is authoritative for
    which projects exist; a project's own record and its latest handoff
    carry the detail."""
    return {"projects": [e.__dict__ for e in load_visible_registry(config, config.acting_principal)]}


def tool_get_project(config: Config, id: str) -> dict:
    """One registered project's record by id or alias. A project record is a
    summary written at a point in time — for "what is the state of X right
    now", read the latest handoff too, and for code state check the actual
    repository."""
    e = find_visible_project(config, config.acting_principal, id)
    if not e:
        raise KeyError(f"no registered project with id or alias '{id}'")
    return {"registry": e.__dict__, "path_exists": Path(e.path).exists()}


def tool_search_timeline(config: Config, query: str = "", limit: int = 50) -> dict:
    """Dated timeline events, newest first, optionally narrowed by a
    full-text `query` over their text. Like `search_memory` this is text
    matching with a limit, not a metadata filter — use `list_notes` when the
    question needs a complete enumeration."""
    entries = timeline_mod.list_timeline(config)
    if query:
        q = query.lower()
        entries = [e for e in entries if q in e.title.lower() or q in e.id.lower()]
    return {"entries": [e.__dict__ for e in entries[:limit]]}


def tool_project_context(config: Config, id: str) -> dict:
    """Everything needed to resume one project in a single call: its record,
    its latest handoff, and (only when Brain runs on the same machine as the
    project) live git/filesystem facts. `filesystem_facts` is null whenever
    the project directory is not visible from where Brain itself runs —
    null means "not checked from here", never "nothing there"."""
    e = find_visible_project(config, config.acting_principal, id)
    if not e:
        raise KeyError(f"no registered project with id or alias '{id}'")
    facts = projectsync.gather(e.path)
    record_text = ""
    try:
        record_text = _read_note_text(config, e.id)
    except FileNotFoundError:
        pass
    return {"registry": e.__dict__, "record": record_text, "filesystem_facts": facts.__dict__}


def tool_write_handoff(config: Config, project_id: str, attempted: str = "", changed: str = "",
                        working_state: str = "", unresolved: str = "", next_action: str = "",
                        files_changed: list | None = None, decisions: list | None = None,
                        source: str | None = None, audience: list | None = None) -> dict:
    """Thin wrapper over handoff.write() — the exact function the CLI's
    `brain handoff write` already calls, including its existing refusal on
    an all-blank payload. No new logic. `source` defaults to the connected
    MCP client's own reported name (`_CURRENT_CLIENT`, set from
    `initialize`'s clientInfo) rather than a hardcoded value — this is the
    one call site that previously hardcoded "claude-session" regardless of
    which client (or human) actually wrote the handoff."""
    rolepolicy.require_audience_allowed(config, config.acting_principal, audience)
    _scan_for_secrets(attempted, changed, working_state, unresolved, next_action,
                       *(files_changed or []), *(decisions or []))
    sections = handoff_mod.HandoffSections(
        attempted=attempted, changed=changed, working_state=working_state,
        unresolved=unresolved, next_action=next_action,
        files_changed=files_changed or [], decisions=decisions or [],
    )
    path = handoff_mod.write(config, project_id, sections, source=source or _CURRENT_CLIENT,
                              audience=audience)
    return {"updated_path": str(path.relative_to(config.brain_root))}


def tool_create_decision(config: Config, title: str, context: str = "", options: str = "",
                          decision: str = "", reasoning: str = "", consequences: str = "",
                          status: str = "proposed", people: list | None = None,
                          projects: list | None = None, tags: list | None = None,
                          sensitivity: str = "normal", source: str = "", source_date: str = "",
                          supersedes: str | None = None, confirm_restricted: bool = False,
                          audience: list | None = None) -> dict:
    """Thin wrapper over decision.create_decision() — the exact function
    `brain decision create` already calls. No new logic."""
    _require_restricted_confirmation(config, config.acting_principal, sensitivity, confirm_restricted)
    rolepolicy.require_audience_allowed(config, config.acting_principal, audience)
    _scan_for_secrets(title, context, options, decision, reasoning, consequences)
    path = decision_mod.create_decision(
        config, title=title, context=context, options=options, decision=decision,
        reasoning=reasoning, consequences=consequences, status=status, people=people,
        projects=projects, tags=tags, sensitivity=sensitivity, source=source,
        source_date=source_date, supersedes=supersedes, audience=audience,
    )
    return {"created_path": str(path.relative_to(config.brain_root))}


def tool_create_project(config: Config, id: str, name: str, path: str, status: str = "active",
                         category: str | None = None, aliases: list | None = None,
                         audience: list | None = None) -> dict:
    """Thin wrapper over projectops.create_project(). No new logic. Never
    copies project source files — `path` is a reference only."""
    rolepolicy.require_audience_allowed(config, config.acting_principal, audience)
    _scan_for_secrets(name, category or "")
    dest = projectops.create_project(
        config, id=id, name=name, path=path, status=status, category=category, aliases=aliases,
        audience=audience,
    )
    return {"created_path": str(dest.relative_to(config.brain_root))}


def _status_change_payload(config: Config, result) -> dict:
    """One shape for both status-change tools, so `index_updated` and the
    handoff relocation cannot be reported by one and omitted by the other."""
    return {
        "id": result.id, "old_status": result.old_status, "new_status": result.new_status,
        "moved": result.moved, "registry_updated": result.registry_updated,
        "index_updated": result.index_updated,
        "handoff_moved": (str(result.handoff_new_path.relative_to(config.brain_root))
                           if result.handoff_new_path else None),
        "updated_path": str(result.new_path.relative_to(config.brain_root)),
    }


def tool_update_project_status(config: Config, id: str, status: str) -> dict:
    """Move a project record between status folders and sync its registry
    entry and its handoff document, in one call — the one thing plain
    `update_memory` cannot do.

    All-or-nothing: if any step fails, every step is rolled back and an
    error is raised; it never reports success on a partially applied change.
    Safe to re-run — it computes what to change from the actual state on
    disk, so repeating it after a failure converges, and repeating it on an
    already-correct project is a no-op. `index_updated: false` means the
    record and registry are correct but the search-index cache is behind and
    `brain index` should be run."""
    result = projectops.set_project_status(config, id, status)
    return _status_change_payload(config, result)


def tool_close_project(config: Config, id: str, summary: str = "") -> dict:
    """Thin wrapper over projectops.close_project(). No new logic."""
    _scan_for_secrets(summary)
    result = projectops.close_project(config, id, summary=summary)
    return _status_change_payload(config, result)


def tool_update_project_section(config: Config, id: str, section: str, mode: str, content: str,
                                 if_match: str | None = None) -> dict:
    """Thin wrapper over projectops.update_section() — the exact function
    `brain project section-update` already calls. No new logic. `section`
    is validated against a fixed allowlist server-side; never an arbitrary
    path or line range."""
    _scan_for_secrets(content)
    path = projectops.update_section(config, id, section, mode, content, if_match=if_match)
    return {"updated_path": str(path.relative_to(config.brain_root))}


def tool_create_memory_note(config: Config, type: str, title: str, text: str = "",
                             tags: list | None = None, people: list | None = None,
                             projects: list | None = None, sensitivity: str = "normal",
                             confidence: str = "fact", source: str = "", source_date: str = "",
                             area: str | None = None, doc_path: str = "",
                             confirm_restricted: bool = False, audience: list | None = None) -> dict:
    """Thin wrapper over memoryops.create_memory() — the exact function
    `brain note create` already calls. No new logic. Never accepts a
    client-supplied path: the destination is derived from `type` (plus a
    validated `area` for facts), never taken as-is."""
    _require_restricted_confirmation(config, config.acting_principal, sensitivity, confirm_restricted)
    rolepolicy.require_audience_allowed(config, config.acting_principal, audience)
    _scan_for_secrets(title, text)
    path = memoryops.create_memory(
        config, type_=type, title=title, text=text, tags=tags, people=people,
        projects=projects, sensitivity=sensitivity, confidence=confidence,
        source=source, source_date=source_date, area=area, doc_path=doc_path,
        audience=audience,
    )
    return {"created_path": str(path.relative_to(config.brain_root))}


def tool_create_timeline_event(config: Config, title: str, valid_from: str,
                                what_happened: str = "", why_it_matters: str = "",
                                people: list | None = None, projects: list | None = None,
                                tags: list | None = None, sensitivity: str = "normal",
                                confidence: str = "fact", source: str = "", source_date: str = "",
                                confirm_restricted: bool = False, audience: list | None = None) -> dict:
    """Thin wrapper over timeline.create_event(). No new logic."""
    _require_restricted_confirmation(config, config.acting_principal, sensitivity, confirm_restricted)
    rolepolicy.require_audience_allowed(config, config.acting_principal, audience)
    _scan_for_secrets(title, what_happened, why_it_matters)
    path = timeline_mod.create_event(
        config, title=title, valid_from=valid_from, what_happened=what_happened,
        why_it_matters=why_it_matters, people=people, projects=projects, tags=tags,
        sensitivity=sensitivity, confidence=confidence, source=source, source_date=source_date,
        audience=audience,
    )
    return {"created_path": str(path.relative_to(config.brain_root))}


def tool_get_operational_state(config: Config, include_restricted: bool = False,
                                timeline_window_days: int = 14) -> dict:
    """Deterministic, read-only operational-state snapshot: projects,
    decisions, memory queue, handoffs, doctor, timeline, systemd timers,
    and a reference to the last saved integrity manifest. No AI, no
    network calls, never writes anything. Restricted content excluded
    unless include_restricted=true."""
    result = state_mod.get_operational_state(
        config, include_restricted=include_restricted,
        timeline_window_days=timeline_window_days,
    )
    return dataclasses.asdict(result)


TOOLS = {
    "search_memory": (tool_search_memory, {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    }),
    "list_notes": (tool_list_notes, {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "note type, e.g. decision | project | document | knowledge"},
            "status": {"type": "string", "description": "frontmatter status, e.g. proposed | decided | superseded (validated against this vault's vocabulary)"},
            "project": {"type": "string", "description": "only notes whose `projects:` list contains this project id"},
            "tag": {"type": "string", "description": "only notes whose `tags:` list contains this tag"},
            "limit": {"type": "integer", "description": f"max entries per page (default {listing.DEFAULT_LIMIT}, max {listing.MAX_LIMIT}); the result always reports the full visible count regardless, and any limit that cuts the result sets page_complete=false"},
            "offset": {"type": "integer", "description": "skip this many entries — pass `next_offset` from a previous page. Any offset above 0 makes the result a page, so page_complete and complete are false even on the last page"},
        },
    }),
    "read_memory": (tool_read_memory, {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
    }),
    "get_context": (tool_get_context, {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "include_projects": {"type": "boolean"},
            "include_timeline": {"type": "boolean"},
            "include_restricted": {"type": "boolean", "description": "Default false — set true only when restricted content is directly relevant and intended"},
        },
        "required": ["query"],
    }),
    "remember": (tool_remember, {
        "type": "object",
        "properties": {
            "type": {"type": "string"}, "title": {"type": "string"}, "text": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "people": {"type": "array", "items": {"type": "string"}},
            "projects": {"type": "array", "items": {"type": "string"}},
            "sensitivity": {"type": "string"}, "confidence": {"type": "string"}, "source": {"type": "string"},
            "source_date": {"type": "string"},
            "confirm_restricted": {"type": "boolean", "description": "Required (true) when sensitivity='restricted'"},
            "audience": {"type": "array", "items": {"type": "string"},
                         "description": "Explicit sharing (Stage 2), e.g. ['group:household'] or ['principal:ania'] -- omit for private (default: owner only)"},
        },
        "required": ["type", "title"],
    }),
    "update_memory": (tool_update_memory, {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "set_fields": {"type": "object"},
            "append_text": {"type": "string"},
            "confirm_restricted": {"type": "boolean", "description": "Required (true) when set_fields.sensitivity='restricted'"},
        },
        "required": ["id"],
    }),
    "queue_memory": (tool_queue_memory, {
        "type": "object",
        "properties": {
            "candidate_fact": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "source": {"type": "string"}, "source_date": {"type": "string"},
            "proposed_destination": {"type": "string"}, "proposed_type": {"type": "string"},
            "sensitivity": {"type": "string"}, "confidence": {"type": "string"}, "reason": {"type": "string"},
        },
        "required": ["candidate_fact"],
    }),
    "list_projects": (tool_list_projects, {"type": "object", "properties": {}}),
    "get_project": (tool_get_project, {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
    }),
    "search_timeline": (tool_search_timeline, {
        "type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    }),
    "project_context": (tool_project_context, {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
    }),
    "write_handoff": (tool_write_handoff, {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "attempted": {"type": "string"}, "changed": {"type": "string"},
            "working_state": {"type": "string"}, "unresolved": {"type": "string"},
            "next_action": {"type": "string"},
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "source": {"type": "string", "description": "Overrides the auto-detected connected-client name for provenance; usually left unset"},
            "audience": {"type": "array", "items": {"type": "string"},
                         "description": "Explicit sharing (Stage 2), e.g. ['group:household'] or ['principal:ania'] -- omit for private (default: owner only)"},
        },
        "required": ["project_id"],
    }),
    "create_decision": (tool_create_decision, {
        "type": "object",
        "properties": {
            "title": {"type": "string"}, "context": {"type": "string"}, "options": {"type": "string"},
            "decision": {"type": "string"}, "reasoning": {"type": "string"}, "consequences": {"type": "string"},
            "status": {"type": "string", "description": "proposed | decided | superseded"},
            "people": {"type": "array", "items": {"type": "string"}},
            "projects": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "sensitivity": {"type": "string"}, "source": {"type": "string"}, "source_date": {"type": "string"},
            "supersedes": {"type": "string", "description": "id of an older decision this replaces"},
            "confirm_restricted": {"type": "boolean", "description": "Required (true) when sensitivity='restricted'"},
            "audience": {"type": "array", "items": {"type": "string"},
                         "description": "Explicit sharing (Stage 2), e.g. ['group:household'] or ['principal:ania'] -- omit for private (default: owner only)"},
        },
        "required": ["title"],
    }),
    "create_project": (tool_create_project, {
        "type": "object",
        "properties": {
            "id": {"type": "string"}, "name": {"type": "string"},
            "path": {"type": "string", "description": "The project's real working directory — reference only, never copied"},
            "status": {"type": "string"}, "category": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "audience": {"type": "array", "items": {"type": "string"},
                         "description": "Explicit sharing (Stage 2), e.g. ['group:household'] or ['principal:ania'] -- omit for private (default: owner only)"},
        },
        "required": ["id", "name", "path"],
    }),
    "update_project_status": (tool_update_project_status, {
        "type": "object",
        "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
        "required": ["id", "status"],
    }),
    "close_project": (tool_close_project, {
        "type": "object",
        "properties": {"id": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["id"],
    }),
    "update_project_section": (tool_update_project_section, {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "section": {"type": "string",
                        "description": f"one of: {', '.join(projectops.PROJECT_SECTION_ALLOWLIST)}"},
            "mode": {"type": "string", "description": "replace | append"},
            "content": {"type": "string"},
            "if_match": {"type": "string",
                         "description": "sha256 of the section's content as last read — optional optimistic-concurrency guard"},
        },
        "required": ["id", "section", "mode", "content"],
    }),
    "create_memory_note": (tool_create_memory_note, {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "person | knowledge | document | fact"},
            "title": {"type": "string"}, "text": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "people": {"type": "array", "items": {"type": "string"}},
            "projects": {"type": "array", "items": {"type": "string"}},
            "sensitivity": {"type": "string"}, "confidence": {"type": "string"},
            "source": {"type": "string"}, "source_date": {"type": "string"},
            "area": {"type": "string", "description": "Required for type='fact': an existing 20_AREAS/ subdirectory name"},
            "doc_path": {"type": "string", "description": "type='document' only: filesystem location of the actual document"},
            "confirm_restricted": {"type": "boolean", "description": "Required (true) when sensitivity='restricted'"},
            "audience": {"type": "array", "items": {"type": "string"},
                         "description": "Explicit sharing (Stage 2), e.g. ['group:household'] or ['principal:ania'] -- omit for private (default: owner only)"},
        },
        "required": ["type", "title"],
    }),
    "create_timeline_event": (tool_create_timeline_event, {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "valid_from": {"type": "string", "description": "The event's own date (YYYY-MM-DD) — sorts the timeline"},
            "what_happened": {"type": "string"}, "why_it_matters": {"type": "string"},
            "people": {"type": "array", "items": {"type": "string"}},
            "projects": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "sensitivity": {"type": "string"}, "confidence": {"type": "string"},
            "source": {"type": "string"}, "source_date": {"type": "string"},
            "confirm_restricted": {"type": "boolean", "description": "Required (true) when sensitivity='restricted'"},
            "audience": {"type": "array", "items": {"type": "string"},
                         "description": "Explicit sharing (Stage 2), e.g. ['group:household'] or ['principal:ania'] -- omit for private (default: owner only)"},
        },
        "required": ["title", "valid_from"],
    }),
    "get_operational_state": (tool_get_operational_state, {
        "type": "object",
        "properties": {
            "include_restricted": {"type": "boolean", "description": "Default false — set true only when restricted content is directly relevant and intended"},
            "timeline_window_days": {"type": "integer"},
        },
    }),
}


def _tools_list_payload():
    return [
        {"name": name, "description": fn.__doc__ or name, "inputSchema": schema}
        for name, (fn, schema) in TOOLS.items()
    ]


def handle_request(config: Config, req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    is_notification = req_id is None and "id" not in req

    def ok(result):
        return None if is_notification else {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, message):
        return None if is_notification else {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    global _CURRENT_CLIENT

    try:
        if method == "initialize":
            client_info = (req.get("params") or {}).get("clientInfo") or {}
            _CURRENT_CLIENT = client_info.get("name", "unknown")
            return ok({
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return ok({"tools": _tools_list_payload()})
        if method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {}) or {}
            if name not in TOOLS:
                _log_call(config, str(name), success=False, error=f"unknown tool '{name}'")
                return err(-32602, f"unknown tool '{name}'")
            fn, _schema = TOOLS[name]
            try:
                result = fn(config, **arguments)
            except Exception as exc:  # noqa: BLE001 — log then re-raise to the outer handler
                _log_call(config, name, success=False, error=str(exc))
                raise
            _log_call(config, name, success=True, result=result)
            return ok({"content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]})
        if method == "ping":
            return ok({})
        return err(-32601, f"method not found: {method}")
    except Exception as exc:  # noqa: BLE001 — surface to the MCP client, don't crash the server
        return err(-32000, str(exc))


def serve(config: Config | None = None) -> None:
    config = config or default_config()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(config, req)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
