"""Shared write-safety policy — the one place secret-pattern scanning and
restricted-sensitivity confirmation are decided, used by every write path
that can put free text into the Brain: the in-process MCP server
(mcp_server.py) and the CLI (cli.py), which the subprocess/CLI-transport
MCP bridge (mcp_bridge.py) shells out to for every one of its own write
tools. Before this module existed, mcp_server.py had its own private
copy of both checks and the CLI had neither — meaning a write routed
through mcp_bridge.py (Claude Code's and Codex's actual production path)
got no secret-scan and no restricted-confirmation gate at all, even
though the in-process server enforced both. Found in the v0.10.0
capability audit; closed here by giving both callers one real
implementation instead of one having it and the other not.

Secret-pattern scanning applies unconditionally to any write carrying
free text, everywhere — never a business decision, never opt-out; matches
`brain doctor`'s own `secret_pattern` check exactly (same SECRET_PATTERNS
list from validate.py) so a write can never land something doctor would
immediately flag anyway.
"""
from __future__ import annotations

from . import rolepolicy
from . import validate as validate_mod
from .paths import Config


class WritePolicyError(RuntimeError):
    """A write refused by shared policy — secret-shaped text, or a
    restricted-sensitivity write missing its required confirmation. Always
    surfaced to the caller as a clear, specific refusal, never swallowed."""


def scan_for_secrets(*texts: str) -> None:
    """Same patterns `brain doctor` flags as `secret_pattern` — refuse the
    write outright rather than let it land and rely on a later doctor run
    to notice. Raises WritePolicyError on the first match."""
    for text in texts:
        if not text:
            continue
        for line in text.splitlines():
            for label, pattern in validate_mod.SECRET_PATTERNS:
                if pattern.search(line):
                    raise WritePolicyError(
                        f"refusing to write: possible {label} found in the provided text. "
                        "Remove/rotate the credential — secrets must never be stored in the Brain."
                    )


def require_restricted_confirmation(config: Config, principal_id: str, sensitivity: str,
                                     confirm_restricted: bool) -> None:
    """sensitivity='restricted' requires an explicit, separate confirmation
    — a deliberate friction point so a restricted write can never happen as
    an unnoticed side effect of an automated call (an MCP tool call, or a
    write routed through the CLI on an automated caller's behalf, e.g. via
    mcp_bridge.py). Raises WritePolicyError if the confirmation is missing.

    Pre-onboarding hardening: also checks the acting principal's role
    policy (rolepolicy.py) — a role that disallows restricted writes
    outright is refused here too, independent of confirm_restricted. Both
    checks live in this one function for the same reason this module
    exists at all: one real implementation every write path goes through,
    never a second copy one caller has and another doesn't."""
    if sensitivity == "restricted" and not confirm_restricted:
        raise WritePolicyError(
            "sensitivity='restricted' requires confirm_restricted=true (MCP) or "
            "--confirm-restricted (CLI) — this is a deliberate friction point so a "
            "restricted write can never happen as an unnoticed side effect."
        )
    rolepolicy.require_restricted_write_allowed(config, principal_id, sensitivity)
