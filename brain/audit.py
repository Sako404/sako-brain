"""Brain-core audit log (SAKO Brain multi-user, Stage 1).

Separate from the remote gateway's own authentication/transport audit
(`remote_gateway.storage`'s token table, and its own login/consent/
revoke log) — this is Brain-core state: principal/group lifecycle today
(Stage 1), joined by ACL/audience-change and write/sharing events once
that state exists (Stage 2). Hardening requirement 7's split, kept
literal: two logs, because only Brain-core code has vault filesystem
access to write this one, and only the gateway process can write its
own — not two independent re-implementations of the same thing, just
two different processes each logging what only they can see.

Append-only, redaction-safe by construction: this module never accepts a
record body or free-text content as a loggable field, only short,
already-known-safe identifiers (principal_id, client_id, transport,
event name) plus an optional short `detail` the caller must keep terse
and non-sensitive — mirrors mcp_server.py's own `_log_call` precedent
(same reasoning, same shape), generalized to a shared location every
write/identity path can call, not left stranded in the one module that
happens to be unused in production.
"""
from __future__ import annotations

from datetime import datetime

from . import paths as paths_mod
from .paths import Config


def log_event(config: Config, *, event: str, principal_id: str = "", client_id: str = "",
               client_name: str = "", transport: str = "", delegator: str = "",
               detail: str = "") -> None:
    """One line per event. Never raises — audit logging must never be the
    reason a legitimate operation fails, matching `_log_call`'s own rule.

    `client_name` and `delegator` are provenance-only additions (Stage 2
    remote-MCP per-client audit): `client_name` is optional human-readable
    display metadata (never an authorization input — see
    `caller_provenance()` below), and `delegator` is the trusted SSH
    identity that actually connected when it differs from `client_id`
    (e.g. `remote-gateway-write` fronting a real OAuth client). Both are
    omitted from the line entirely when empty, so every pre-existing call
    site's output is byte-for-byte unchanged."""
    try:
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = config.logs_dir / f"brain-audit-{datetime.now():%Y-%m}.log"
        line = (
            f"{datetime.now().isoformat(timespec='seconds')}  event={event}  "
            f"principal={principal_id or '-'}  client={client_id or '-'}  "
            f"transport={transport or '-'}"
        )
        if client_name:
            line += f"  client_name={client_name[:200]}"
        if delegator:
            line += f"  delegator={delegator}"
        if detail:
            line += f"  detail={detail[:200]}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        paths_mod.ensure_private_file(log_path)
    except OSError:
        pass


def caller_provenance(config: Config) -> dict:
    """The client_id/client_name/delegator/transport quartet for `config`'s
    own caller context, meant to be splatted straight into `log_event(...,
    **caller_provenance(config))`.

    Stage 2 remote-MCP per-client audit: a validated OAuth `client_id`
    (set only for gateway-delegated requests — see
    `Config.caller_oauth_client_id`) takes `client_id`'s place when
    present, and the SSH identity that actually connected
    (`remote-gateway-read`/`-write`) moves to `delegator` so it is never
    silently lost. Every other caller (desktop, Claude Code, Codex, local
    SSH, TRON) has no OAuth client at all, so `client_id` falls back to
    exactly what it always was — the connecting SSH identity — and
    `delegator` stays empty: zero behavior change for them.

    `client_name` is carried through purely as optional display metadata.
    Neither it nor `client_id` is ever an authorization input anywhere in
    this module or its callers — they are logged, never checked."""
    oauth_client_id = config.caller_oauth_client_id
    return {
        "client_id": oauth_client_id or config.caller_client,
        "client_name": config.caller_oauth_client_name,
        "delegator": config.caller_client if oauth_client_id else "",
        "transport": config.caller_transport,
    }
