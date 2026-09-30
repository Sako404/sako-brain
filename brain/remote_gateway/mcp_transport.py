"""Streamable HTTP MCP transport for the remote gateway (v0.12.0, item 5).

Reuses `brain.mcp_bridge.handle_request` — the SAME subprocess/CLI-
transport dispatch the Claude Code and Codex integrations already go
through — so this is a new *transport* wrapped around the existing
capability surface, never a second Brain implementation.

Deliberately `mcp_bridge`, not the in-process `mcp_server`: the bridge
shells out to the installed `brain` CLI, which resolves this process's
own `brain setup`-configured client (a dedicated remote-gateway SSH
identity — see docs/REMOTE_ACCESS.md) and reaches canonical Brain through
the existing SSH forced-command dispatcher, exactly like any other
client. The in-process server instead requires a *locally resolvable
vault* — using it here would mean the internet-facing gateway getting
direct filesystem access to the vault, bypassing the dispatcher's own
identity-based authority entirely. That is exactly what item 5/18 of the
v0.12.0 task rules out ("Gateway ma być capability adapter, nie drugim
Brainem" / preferred model: remote MCP -> canonical Brain CLI/service
interface -> restricted dispatcher). The gateway's *own* SSH identity is
independently revocable (item 23) precisely because it is a real,
separate dispatcher-side identity, not a filesystem bypass.

What this module adds on top, all before/around that shared call:

- OAuth scope enforcement per tool call (item 7/8) — the outer gate;
  Brain's own writepolicy (secret scanning, restricted confirmation)
  still runs unchanged *inside* `handle_request` itself.
- Tool annotations (readOnlyHint etc., item 9) and vendor-neutral server
  instructions (item 10) added to what the shared dispatch returns.
- `tools/list` filtered to what the presented token's scopes actually
  allow — a browser AI is never shown a tool `tools/call` would then
  refuse, matching the existing bridge's own documented principle.
"""
from __future__ import annotations

import json as json_mod

from .. import mcp_bridge
from . import scopes as scopes_mod
from .instructions import SERVER_INSTRUCTIONS


class ScopeError(Exception):
    def __init__(self, missing: frozenset[str]):
        super().__init__(f"insufficient_scope: {' '.join(sorted(missing))}")
        self.missing = missing


def _annotated_tools_list(granted_scopes: frozenset[str]) -> list[dict]:
    out = []
    for tool in mcp_bridge._tools_list_payload():
        name = tool["name"]
        needed = scopes_mod.base_scope_for_tool(name)
        if needed is not None and needed not in granted_scopes:
            continue  # never advertise a tool this token could not call at all
        annotated = dict(tool)
        annotated["annotations"] = scopes_mod.tool_annotations(name)
        if needed:
            annotated["annotations"]["securityScopes"] = [needed]
        out.append(annotated)
    return out


def handle_mcp_request(req: dict, granted_scopes: frozenset[str]) -> dict | None:
    """The gateway's own request handler: scope-checks `tools/call`,
    filters `tools/list`, augments `initialize` with server instructions,
    and defers everything else — including all actual tool execution —
    to `mcp_bridge.handle_request` verbatim."""
    method = req.get("method")
    req_id = req.get("id")
    is_notification = req_id is None and "id" not in req

    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {}) or {}
        missing = scopes_mod.missing_scopes(name, arguments, granted_scopes)
        if missing:
            raise ScopeError(missing)
        return mcp_bridge.handle_request(req)

    if method == "tools/list":
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _annotated_tools_list(granted_scopes)}}

    response = mcp_bridge.handle_request(req)
    if method == "initialize" and response is not None and "result" in response:
        response["result"]["instructions"] = SERVER_INSTRUCTIONS
    return response


def handle_mcp_body(body: bytes, granted_scopes: frozenset[str]) -> tuple[dict | None, int, str | None]:
    """Parses one Streamable-HTTP MCP POST body (a single JSON-RPC object;
    batching is not implemented — no current target client requires it)
    and returns (response_dict_or_None, http_status, missing_scope_str).
    `missing_scope_str` is only set on a 403, and is the exact value the
    caller must put in the WWW-Authenticate `scope=` parameter (MCP
    authorization spec, "Runtime Insufficient Scope Errors" — the client
    needs this to know what to re-request). A None response with status
    202 is the correct Streamable-HTTP shape for a notification, which
    has no JSON-RPC response at all."""
    try:
        req = json_mod.loads(body)
    except json_mod.JSONDecodeError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400, None

    try:
        result = handle_mcp_request(req, granted_scopes)
    except ScopeError as exc:
        missing_str = " ".join(sorted(exc.missing))
        return {
            "jsonrpc": "2.0", "id": req.get("id"),
            "error": {"code": -32000, "message": f"insufficient scope: {missing_str}"},
        }, 403, missing_str

    if result is None:
        return None, 202, None
    return result, 200, None
