"""Minimal local MCP server for the Sako Brain — stdlib only, stdio transport.

No `mcp` SDK is installed in this environment (no pip available), so this
implements the small newline-delimited JSON-RPC 2.0 subset of the Model
Context Protocol needed for `tools/list` and `tools/call` by hand. If the
official `mcp` Python package becomes available later, this can be swapped
for `mcp.server.fastmcp.FastMCP` with the same tool functions — see README.

Run with:  python3 -m brain.mcp_server
Never expose this over a network socket — stdio only, local use only.

Phase 5A write-policy note: `remember` and `update_memory` are the only
tools that write. Neither bypasses what a human typing `brain remember` /
editing a file by hand would be subject to — both run the same
secret-pattern scan `brain doctor` uses (refusing outright on a match, the
same as `brain git snapshot`/`brain backup run` do), and writing
`sensitivity: restricted` content requires an explicit `confirm_restricted:
true` argument, so a restricted write can never happen as an unnoticed
side effect of a tool call. `queue_memory` exposes the Level 2
pending-memory queue (see `memoryqueue.py`) for MCP-only clients — the
"durable-seeming but not explicitly confirmed" case — as a non-authoritative
staging step rather than an immediate write.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import capture, context as context_mod, discover as discover_mod, memoryqueue, projectsync
from . import search as search_mod
from . import timeline as timeline_mod
from . import update as update_mod
from . import validate as validate_mod
from . import paths as paths_mod
from . import __version__
from .paths import Config, default_config
from .registry import load_registry

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


class McpWriteRefused(RuntimeError):
    pass


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
    path = update_mod.find_note_path(config, note_id)
    if not path:
        raise FileNotFoundError(f"no note with id '{note_id}'")
    return path.read_text(encoding="utf-8")


def _scan_for_secrets(*texts: str) -> None:
    """Same patterns `brain doctor` flags as `secret_pattern` — refuse the
    write outright rather than let it land and rely on a later doctor run
    to notice."""
    for text in texts:
        if not text:
            continue
        for line in text.splitlines():
            for label, pattern in validate_mod.SECRET_PATTERNS:
                if pattern.search(line):
                    raise McpWriteRefused(
                        f"refusing to write: possible {label} found in the provided text. "
                        "Remove/rotate the credential — secrets must never be stored in the Brain."
                    )


def _require_restricted_confirmation(sensitivity: str, confirm_restricted: bool) -> None:
    if sensitivity == "restricted" and not confirm_restricted:
        raise McpWriteRefused(
            "sensitivity='restricted' requires confirm_restricted=true — this is a deliberate "
            "friction point so a restricted write can never happen as an unnoticed side effect."
        )


# ---- tool implementations -------------------------------------------------

def tool_search_memory(config: Config, query: str, limit: int = 20) -> dict:
    results = search_mod.search(config, query, limit=limit)
    return {"results": [r.__dict__ for r in results]}


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
    return {"id": id, "content": _read_note_text(config, id)}


def tool_remember(config: Config, type: str, title: str, text: str = "",
                   tags: list | None = None, people: list | None = None,
                   projects: list | None = None, sensitivity: str = "normal",
                   confidence: str = "fact", source: str = "", source_date: str = "",
                   confirm_restricted: bool = False) -> dict:
    _require_restricted_confirmation(sensitivity, confirm_restricted)
    _scan_for_secrets(title, text)
    path = capture.capture(
        config, type_=type, title=title, text=text, tags=tags, people=people,
        projects=projects, sensitivity=sensitivity, confidence=confidence, source=source,
        source_date=source_date,
    )
    return {"created_path": str(path.relative_to(config.brain_root))}


def tool_update_memory(config: Config, id: str, set_fields: dict | None = None,
                        append_text: str | None = None, confirm_restricted: bool = False) -> dict:
    set_fields = set_fields or {}
    if set_fields.get("sensitivity") == "restricted":
        _require_restricted_confirmation("restricted", confirm_restricted)
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
    return {"projects": [e.__dict__ for e in load_registry(config)]}


def tool_get_project(config: Config, id: str) -> dict:
    entries = {e.id: e for e in load_registry(config)}
    e = entries.get(id)
    if not e:
        raise KeyError(f"no registered project with id '{id}'")
    return {"registry": e.__dict__, "path_exists": Path(e.path).exists()}


def tool_search_timeline(config: Config, query: str = "", limit: int = 50) -> dict:
    entries = timeline_mod.list_timeline(config)
    if query:
        q = query.lower()
        entries = [e for e in entries if q in e.title.lower() or q in e.id.lower()]
    return {"entries": [e.__dict__ for e in entries[:limit]]}


def tool_get_project_path(config: Config, id: str) -> dict:
    entries = {e.id: e for e in load_registry(config)}
    e = entries.get(id)
    if not e:
        raise KeyError(f"no registered project with id '{id}'")
    return {"path": e.path}


def tool_project_context(config: Config, id: str) -> dict:
    entries = {e.id: e for e in load_registry(config)}
    e = entries.get(id)
    if not e:
        raise KeyError(f"no registered project with id '{id}'")
    facts = projectsync.gather(e.path)
    record_text = ""
    try:
        record_text = _read_note_text(config, id)
    except FileNotFoundError:
        pass
    return {"registry": e.__dict__, "record": record_text, "filesystem_facts": facts.__dict__}


TOOLS = {
    "search_memory": (tool_search_memory, {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
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
    "get_project_path": (tool_get_project_path, {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
    }),
    "project_context": (tool_project_context, {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
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
