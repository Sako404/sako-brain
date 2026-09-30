"""OAuth scope <-> MCP tool mapping for the remote gateway (v0.12.0).

Three scopes, least-privilege by design:

    brain.read        — every tool that only reads.
    brain.write        — every tool that performs a normal (non-restricted)
                         canonical write. Existing Brain write-safety
                         (secret scanning, provenance) still applies
                         underneath this — the scope check is an *outer*
                         gate, never a replacement for it.
    brain.restricted   — required, *in addition to* brain.write, for any
                         write whose effective sensitivity is "restricted".
                         Mirrors the existing confirm_restricted mechanism
                         one-for-one: a token without this scope can never
                         cause confirm_restricted=true to reach a tool,
                         regardless of what the caller asks for.

The mapping is a single explicit table (not introspected from TOOLS)
because scope assignment is a *policy* decision — which tools are
sensitive enough to gate — not a mechanical property the code can derive
on its own. A test (`test_every_tool_has_a_scope_mapping`) pins this
table against `brain.mcp_server.TOOLS` so a newly added tool fails loudly
instead of silently defaulting to open or silently unreachable.
"""
from __future__ import annotations

SCOPE_READ = "brain.read"
SCOPE_WRITE = "brain.write"
SCOPE_RESTRICTED = "brain.restricted"

ALL_SCOPES = (SCOPE_READ, SCOPE_WRITE, SCOPE_RESTRICTED)
SCOPES_SUPPORTED = " ".join(ALL_SCOPES)

READ_TOOLS = frozenset({
    "search_memory", "read_memory", "get_context", "list_projects",
    "get_project", "search_timeline", "project_context", "get_operational_state",
})

WRITE_TOOLS = frozenset({
    "remember", "update_memory", "queue_memory", "write_handoff",
    "create_decision", "create_project", "update_project_status",
    "close_project", "update_project_section", "create_memory_note",
    "create_timeline_event",
})


def base_scope_for_tool(name: str) -> str | None:
    """The scope minimally required to invoke this tool at all. None for
    an unknown tool (caller must treat that as a hard refusal, not a
    default-allow)."""
    if name in READ_TOOLS:
        return SCOPE_READ
    if name in WRITE_TOOLS:
        return SCOPE_WRITE
    return None


def _requests_restricted(arguments: dict) -> bool:
    if arguments.get("confirm_restricted"):
        return True
    if arguments.get("sensitivity") == "restricted":
        return True
    set_fields = arguments.get("set_fields")
    if isinstance(set_fields, dict) and set_fields.get("sensitivity") == "restricted":
        return True
    return False


def required_scopes(name: str, arguments: dict) -> frozenset[str] | None:
    """Scopes a token must hold to perform this exact call. None if the
    tool is unknown (refuse, don't guess)."""
    base = base_scope_for_tool(name)
    if base is None:
        return None
    scopes = {base}
    if base == SCOPE_WRITE and _requests_restricted(arguments):
        scopes.add(SCOPE_RESTRICTED)
    return frozenset(scopes)


def missing_scopes(name: str, arguments: dict, granted: frozenset[str]) -> frozenset[str] | None:
    """None if the call is fully authorized; otherwise the scopes still
    needed. Also None (meaning: not an authorization problem) for an
    unknown tool — that fails at dispatch, not here."""
    needed = required_scopes(name, arguments)
    if needed is None:
        return None
    return needed - granted or None


def tool_annotations(name: str) -> dict:
    """readOnlyHint/destructiveHint per the MCP tool-annotation convention
    (item 9), computed from this same table so it can never drift from the
    scope it implies. No Brain tool ever deletes data (archive/append-only
    by design), so destructiveHint is false for all of them."""
    is_read = name in READ_TOOLS
    return {
        "readOnlyHint": is_read,
        "destructiveHint": False,
        "idempotentHint": is_read,
        "openWorldHint": False,
    }
