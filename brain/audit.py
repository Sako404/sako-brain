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
               transport: str = "", detail: str = "") -> None:
    """One line per event. Never raises — audit logging must never be the
    reason a legitimate operation fails, matching `_log_call`'s own rule."""
    try:
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = config.logs_dir / f"brain-audit-{datetime.now():%Y-%m}.log"
        line = (
            f"{datetime.now().isoformat(timespec='seconds')}  event={event}  "
            f"principal={principal_id or '-'}  client={client_id or '-'}  "
            f"transport={transport or '-'}"
        )
        if detail:
            line += f"  detail={detail[:200]}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        paths_mod.ensure_private_file(log_path)
    except OSError:
        pass
