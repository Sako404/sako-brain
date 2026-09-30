"""MCP server that reaches Brain *exclusively* through the already-installed
`brain` CLI executable, as a subprocess — never a direct filesystem/library
call, and never a second implementation of `brain`'s own business logic.

Why this exists, distinct from `mcp_server.py`: that server calls Brain's
Python functions in-process against a locally resolved vault (`BRAIN_ROOT` /
an upward filesystem walk / a per-user config file) — correct when Brain
*is* local, but this desktop's real vault is the TrueNAS server-canonical
one, reached only through the `brain` client wrapper's own transparent SSH
proxy (its client-config file, alongside the vault's own per-user config —
see `paths.user_config_path()`). That proxy logic lives entirely
in the wrapper script, not in this Python package ("BRAIN_ROOT is
deployment knowledge, not library knowledge" — paths.py's own words) — so
`mcp_server.py`, run directly, cannot reach the canonical vault from here at
all, and must never be pointed at BRAIN_ROOT on this machine (it would
silently serve the demoted, non-canonical Nextcloud copy instead).

This bridge is the other side of that same trick: it shells out to the
`brain` executable exactly as a human typing the command would, so it
inherits whatever `brain` itself is configured to do — transparent proxy to
the server here, direct local access if this ever ran on the server itself,
identical identity/permission enforcement either way. Zero new credentials,
zero new server-side surface, zero business logic of its own: every tool
call is a subprocess invocation of `brain <subcommand> ... --json`, and
every tool's *existence and schema* comes from `brain capabilities` (the
same TOOLS dict `mcp_server.py` itself serves) — never a second, hand-kept
list that could drift from what `brain` actually does.

Coverage is intentionally partial in this first version: exactly the tools
needed for Claude/Codex live acceptance (search, context, read, remember,
note create, a controlled project-status update, session handoffs) plus the
operational-state snapshot. Invariant: `tools/list` must never advertise a
tool `tools/call` cannot actually execute — so it reports Brain's real tool
set (from `brain capabilities`, never a second hand-kept list) filtered down
to exactly the names this bridge has a translator for (see TRANSLATORS
below); a name `brain capabilities` adds tomorrow is invisible here until a
translator exists for it, rather than being advertised and then failing.
`tools/call` on an untranslated name (reachable only by a client that cached
an older, unfiltered tools/list) returns a clear, honest error rather than
guessing — it never silently does the wrong thing.

Run with: python3 -m brain.mcp_bridge
Never expose this over a network socket — stdio only, local use only.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import paths as paths_mod

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = f"{paths_mod.APP_DIRNAME}-mcp-bridge"
# Independent of the brain package's own version — this is a thin,
# separately-evolving transport adapter, not part of that package's
# release cycle.
BRIDGE_VERSION = "0.1.0"

# Env vars that could make the underlying `brain` invocation resolve a
# *local* vault instead of proxying to the canonical server — stripped from
# every subprocess call so this bridge can never reach the demoted,
# non-canonical Nextcloud copy no matter what the parent process's
# environment happens to carry. SAKO_BRAIN_CLIENT_CONFIG is deliberately
# NOT in this list: that is how `brain` finds its proxy configuration in
# the first place, and must be respected, not stripped.
_LOCAL_OVERRIDE_ENV_VARS = ("BRAIN_ROOT", "BRAIN_LOCAL", "BRAIN_STATE_DIR", "BRAIN_VAULT")

_SUBPROCESS_TIMEOUT_SECONDS = 30.0

# Set once per process from the `initialize` request's clientInfo, mirroring
# mcp_server.py's own _CURRENT_CLIENT — used for logging and as the default
# handoff `source` (see decision-2026-09-28-... and the handoff-provenance
# fix this bridge relies on: a handoff written through this bridge should
# say which real client wrote it, never a hardcoded value).
_CURRENT_CLIENT = "unknown"


class BridgeError(RuntimeError):
    """A tool call this bridge could not satisfy — always surfaced to the
    MCP client as a clear message, never swallowed."""


def _brain_executable() -> str:
    override = os.environ.get("BRAIN_MCP_BRIDGE_EXECUTABLE")
    if override:
        return override
    found = shutil.which("brain")
    if found:
        return found
    # MCP clients are often launched with a minimal PATH that does not
    # include a user's ~/.local/bin — fall back to the known desktop
    # install location rather than failing outright.
    fallback = Path.home() / ".local" / "bin" / "brain"
    return str(fallback) if fallback.exists() else "brain"


def _clean_env() -> dict:
    env = dict(os.environ)
    for key in _LOCAL_OVERRIDE_ENV_VARS:
        env.pop(key, None)
    return env


def _run_brain(argv: list[str], *, stdin_data: str | None = None):
    import subprocess

    try:
        return subprocess.run(
            [_brain_executable(), *argv],
            input=stdin_data,
            capture_output=True,
            text=True,
            env=_clean_env(),
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            f"brain {' '.join(argv)} timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s "
            "(the canonical Brain may be unreachable — this is a caller-visible "
            "failure, never a silent hang or a guess at the answer)"
        ) from exc
    except FileNotFoundError as exc:
        raise BridgeError(
            f"could not find the `brain` executable ({_brain_executable()!r}) — "
            "set BRAIN_MCP_BRIDGE_EXECUTABLE if it is not on PATH"
        ) from exc


def _tools_list_payload() -> list[dict]:
    proc = _run_brain(["capabilities"])
    if proc.returncode != 0:
        raise BridgeError(
            f"could not reach canonical Brain for capabilities: {(proc.stderr or proc.stdout).strip()}"
        )
    try:
        caps = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"brain capabilities produced non-JSON output: {exc}") from exc
    # Invariant: never advertise a tool this bridge cannot execute — filter
    # Brain's real tool set down to exactly the names TRANSLATORS covers.
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
        for t in caps["interfaces"]["mcp"]["tools"]
        if t["name"] in TRANSLATORS
    ]


# ---- per-tool translation: MCP arguments -> (brain argv, optional stdin JSON) ----
# Each function's only job is building the exact command line a human typing
# `brain ...` would use for the same request — no validation, no business
# logic, nothing brain's own CLI does not already do for itself.

def _t_search_memory(a: dict):
    return ["search", a["query"], "--limit", str(a.get("limit", 20)), "--json"], None


def _t_get_context(a: dict):
    argv = ["context", a["query"], "--json", "--limit", str(a.get("limit", 10))]
    if a.get("include_restricted"):
        argv.append("--restricted")
    return argv, None


def _t_read_memory(a: dict):
    return ["get", a["id"]], None


def _t_list_projects(a: dict):
    return ["projects"], None


def _t_get_operational_state(a: dict):
    argv = ["state", "--json"]
    if a.get("include_restricted"):
        argv.append("--include-restricted")
    if "timeline_window_days" in a:
        argv += ["--timeline-days", str(a["timeline_window_days"])]
    return argv, None


def _list_flags(a: dict, key: str) -> list[str]:
    values = a.get(key) or []
    return [f"--{key}", *values] if values else []


def _t_remember(a: dict):
    argv = ["remember", "--type", a["type"], "--title", a["title"], "--json"]
    if a.get("text"):
        argv += ["--text", a["text"]]
    argv += _list_flags(a, "tags") + _list_flags(a, "people") + _list_flags(a, "projects")
    if a.get("sensitivity"):
        argv += ["--sensitivity", a["sensitivity"]]
    if a.get("confidence"):
        argv += ["--confidence", a["confidence"]]
    # Default provenance to the connected client, same discipline as
    # write_handoff below — never left to silently say nothing about who
    # asked for this. An explicit `source` argument always wins.
    argv += ["--source", a.get("source") or f"mcp-bridge:{_CURRENT_CLIENT}"]
    return argv, None


def _t_create_memory_note(a: dict):
    argv = ["note", "create", "--type", a["type"], "--title", a["title"], "--json"]
    if a.get("text"):
        argv += ["--text", a["text"]]
    if a.get("area"):
        argv += ["--area", a["area"]]
    if a.get("doc_path"):
        argv += ["--doc-path", a["doc_path"]]
    argv += _list_flags(a, "tags") + _list_flags(a, "people") + _list_flags(a, "projects")
    if a.get("sensitivity"):
        argv += ["--sensitivity", a["sensitivity"]]
    if a.get("confidence"):
        argv += ["--confidence", a["confidence"]]
    argv += ["--source", a.get("source") or f"mcp-bridge:{_CURRENT_CLIENT}"]
    if a.get("source_date"):
        argv += ["--source-date", a["source_date"]]
    return argv, None


def _t_update_project_status(a: dict):
    return ["project", "update", a["id"], "--status", a["status"], "--json"], None


def _t_write_handoff(a: dict):
    payload = {
        "attempted": a.get("attempted", ""), "changed": a.get("changed", ""),
        "working_state": a.get("working_state", ""), "unresolved": a.get("unresolved", ""),
        "next_action": a.get("next_action", ""),
        "files_changed": a.get("files_changed", []), "decisions": a.get("decisions", []),
        # The whole point: a handoff written through this bridge names the
        # real connected client, not a hardcoded value (the exact gap
        # fix/handoff-provenance-source closed on the `brain` side).
        "source": a.get("source") or _CURRENT_CLIENT,
    }
    return ["handoff", "write", "--project", a["project_id"]], json.dumps(payload)


TRANSLATORS = {
    "search_memory": _t_search_memory,
    "get_context": _t_get_context,
    "read_memory": _t_read_memory,
    "list_projects": _t_list_projects,
    "get_operational_state": _t_get_operational_state,
    "remember": _t_remember,
    "create_memory_note": _t_create_memory_note,
    "update_project_status": _t_update_project_status,
    "write_handoff": _t_write_handoff,
}


def _execute_tool(name: str, arguments: dict) -> dict:
    if name not in TRANSLATORS:
        raise BridgeError(
            f"tool '{name}' is not yet supported by the MCP bridge (subprocess/CLI "
            "transport) — it exists (see tools/list) but has no CLI translation here "
            "yet. Use the in-process mcp_server.py, or `brain` directly, for now."
        )
    argv, stdin_data = TRANSLATORS[name](arguments)
    proc = _run_brain(argv, stdin_data=stdin_data)
    if proc.returncode != 0:
        raise BridgeError((proc.stderr or proc.stdout or f"brain exited {proc.returncode}").strip())

    if name == "read_memory":
        return {"id": arguments["id"], "content": proc.stdout}
    if name == "write_handoff":
        # `brain handoff write` has no --json (nothing else needs it) —
        # its one line of success output is a fixed, code-owned format:
        # "Handoff written: <path>\n". Parsed here rather than adding a
        # flag for a single caller, same discipline as list_projects below.
        prefix = "Handoff written: "
        line = proc.stdout.strip()
        if not line.startswith(prefix):
            raise BridgeError(f"unexpected output from `brain handoff write`: {line!r}")
        return {"updated_path": line[len(prefix):]}
    if name == "list_projects":
        # `brain projects` has no --json (not needed for anything else this
        # bridge does) — parsed here rather than adding one more CLI flag
        # for a single caller. Format: "{id}  [{status}]  {name}\n    {path}".
        projects = []
        lines = proc.stdout.splitlines()
        for i in range(0, len(lines) - 1, 2):
            head, path_line = lines[i], lines[i + 1]
            if "[" not in head or "]" not in head:
                continue
            id_part, rest = head.split("  [", 1)
            status, _, name_part = rest.partition("]  ")
            projects.append({
                "id": id_part.strip(), "status": status.strip(),
                "name": name_part.strip(), "path": path_line.strip(),
            })
        return {"projects": projects}

    text = proc.stdout.strip()
    if not text:
        return {}
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"brain produced non-JSON output for '{name}': {exc}") from exc

    if name == "update_project_status":
        status_change = result.get("status_change") or {}
        return {"id": result["id"], **status_change}
    return result


def handle_request(req: dict) -> dict | None:
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
                "serverInfo": {"name": SERVER_NAME, "version": BRIDGE_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return ok({"tools": _tools_list_payload()})
        if method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {}) or {}
            try:
                result = _execute_tool(name, arguments)
            except BridgeError as exc:
                return err(-32000, str(exc))
            return ok({"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False, default=str)}]})
        if method == "ping":
            return ok({})
        return err(-32601, f"method not found: {method}")
    except Exception as exc:  # noqa: BLE001 — surface to the MCP client, never crash the bridge
        return err(-32000, str(exc))


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(req)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
