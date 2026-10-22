"""The SSH forced-command dispatcher for a server that hosts a canonical
Brain other clients reach over SSH (see `brain setup` and
docs/SERVER_SETUP.md).

Each SSH key is installed in authorized_keys with a forced command that
runs this module, with flags fixed at install time:

    restrict,command="/path/to/venv/bin/python3 -I -m brain.dispatch
      --mode read --vault /srv/brain/vault --state-dir /srv/brain/state
      --principal principal-alex --client laptop" ssh-ed25519 AAAA...

A module rather than a console script or a `brain` subcommand: the
package ships a single entry point, and the dispatcher must never be
reachable through itself.

The connecting client only ever supplies SSH_ORIGINAL_COMMAND, which is
parsed with shlex (never handed to a shell) and must be a `brain`
subcommand its key's mode allows (brain.commandpolicy). Mode, principal,
client name and delegation rights come only from the forced command.

Before running anything the effective principal must be `status: active`
in the vault's own principal records, read live on every connection and
never cached, so disabling a principal takes effect on its next request
whether or not its key or any token issued to it still exists. Every
transport that reaches a server vault converges here, so one check covers
all of them. Any error in that check refuses the request (fail closed).

Delegation. A key installed with `--delegation mandatory` (the remote
gateway's keys) must prefix every request with
`--acting-principal <id> [--acting-client-id <id>] [--acting-client-name
<name>] --` and runs as that principal; a request without the prefix is
refused, never run as the key's own principal. `--delegation optional`
honours the prefix when present and otherwise runs as the key's own
--principal, so a delegating client can start asserting without a
lockstep change on the server. `--delegation none` (the default) refuses
any request that carries the prefix. Delegation changes who a request runs
as, never what it may run: --mode still applies.

SECURITY: a key installed with --delegation optional or mandatory can act
as any active principal in this vault. Treat its compromise as an
identity-spoofing incident: rotate the keypair, replace its authorized_keys
line and audit every delegated request since the suspected compromise.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from . import commandpolicy
from . import identity
from .paths import DEFAULT_ACTING_PRINCIPAL, default_config

DELEGATION_LEVELS = ("none", "optional", "mandatory")


class DelegationError(ValueError):
    """A delegation prefix is present but malformed — never raised for an
    absent prefix, which is the ordinary, non-delegated case."""


def parse_delegated_command(
    argv_all: list[str],
) -> tuple[str | None, str | None, str | None, list[str]]:
    """Splits a '--acting-principal <id> [--acting-client-id <id>]
    [--acting-client-name <name>] -- <real argv>' prefix off the front of
    the parsed SSH_ORIGINAL_COMMAND, if present. The two --acting-client-*
    flags carry the gateway's validated OAuth client for audit provenance;
    they are optional, order-independent, and never valid without a leading
    --acting-principal. Returns (principal, client_id, client_name,
    remaining_argv). Pure: whether an assertion is honoured is
    resolve_effective_principal()'s decision, not this parser's."""
    if argv_all[:1] != ["--acting-principal"]:
        return None, None, None, argv_all
    if len(argv_all) < 2:
        raise DelegationError("--acting-principal requires a value")
    asserted_principal = argv_all[1]
    rest = argv_all[2:]

    asserted_client_id: str | None = None
    asserted_client_name: str | None = None
    while rest[:1] in (["--acting-client-id"], ["--acting-client-name"]):
        flag = rest[0]
        if len(rest) < 2:
            raise DelegationError(f"{flag} requires a value")
        value = rest[1]
        if flag == "--acting-client-id":
            asserted_client_id = value
        else:
            asserted_client_name = value
        rest = rest[2:]

    if rest[:1] != ["--"]:
        raise DelegationError("--acting-principal must be followed by '--' then the real command")
    return asserted_principal, asserted_client_id, asserted_client_name, rest[1:]


def resolve_effective_principal(delegation: str, client: str, fixed_principal: str,
                                 asserted_principal: str | None) -> tuple[str | None, str]:
    """Which principal governs this request: (principal, "") or
    (None, reason) when refused. Pure; whether the result is an active
    principal is checked separately by the caller.

      - delegation "none" asserting anything: refused. An ordinary key can
        never act as someone else, however well-formed the prefix.
      - "mandatory" asserting nothing: refused, never run as the key's own
        principal (which for a gateway key is not the user it fronts for).
      - "optional" or "mandatory" asserting a principal: that principal.
      - otherwise: the key's own fixed --principal.
    """
    if asserted_principal is not None and delegation == "none":
        return None, f"identity '{client or '(unnamed)'}' is not permitted to assert an acting principal"
    if delegation == "mandatory" and asserted_principal is None:
        return None, f"identity '{client or '(unnamed)'}' must assert an acting principal for every request"
    if asserted_principal is not None:
        return asserted_principal, ""
    return fixed_principal, ""


def principal_allowed(is_active_fn, principal_id: str) -> tuple[bool, str]:
    """Same shape as commandpolicy.is_allowed(); `is_active_fn` is injected
    so the refusal logic is testable without a vault."""
    if not is_active_fn(principal_id):
        return False, f"principal '{principal_id}' is disabled or does not exist"
    return True, ""


def _principal_active(vault: str, principal_id: str) -> bool:
    """Reads the vault's own principal record, live, on every call. Fails
    closed: an enforcement check that degrades to "allow" on error is worse
    than none."""
    try:
        return identity.is_active(default_config(Path(vault)), principal_id)
    except Exception:  # noqa: BLE001 — any failure must refuse, not allow
        return False


def _brain_executable() -> Path:
    """The `brain` console script of the environment this module runs in,
    so the dispatcher can never run a different `brain` than the package
    it came from."""
    return Path(sys.executable).parent / "brain"


def _refuse(reason: str) -> None:
    sys.stderr.write(f"refused: {reason}\n")
    sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -I -m brain.dispatch",
        description="SSH forced-command dispatcher for a server-hosted canonical Brain. "
                    "Install as the command= of an authorized_keys line; never run interactively.",
    )
    parser.add_argument("--mode", choices=sorted(commandpolicy.MODES), default="read")
    parser.add_argument("--principal", default=DEFAULT_ACTING_PRINCIPAL,
                        help="Principal this key runs as when it does not delegate")
    parser.add_argument("--client", default="",
                        help="Name of this key's client, recorded for audit provenance")
    parser.add_argument("--delegation", choices=DELEGATION_LEVELS, default="none",
                        help="Whether this key may act as another principal (see module docs)")
    parser.add_argument("--vault", required=True, help="Path to the canonical vault on this host")
    parser.add_argument("--state-dir", required=True, help="Brain state directory on this host")
    parser.add_argument("--remote-project-paths", action="store_true",
                        help="Project paths in this vault point at client machines, not this host")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    raw = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        argv_all = shlex.split(raw)
    except ValueError:
        _refuse("malformed command")

    # Parsing a delegation prefix is harmless whoever sent it; honouring it
    # is resolve_effective_principal()'s decision below.
    try:
        asserted_principal, asserted_client_id, asserted_client_name, command = (
            parse_delegated_command(argv_all))
    except DelegationError as exc:
        _refuse(str(exc))
    if command and command[0] == "brain":
        command = command[1:]

    effective_principal, reason = resolve_effective_principal(
        args.delegation, args.client, args.principal, asserted_principal)
    if effective_principal is None:
        _refuse(reason)

    # Checked against the effective principal: a disabled principal is
    # refused whether it is the key's own or one a delegating key asserted.
    allowed, reason = principal_allowed(
        lambda pid: _principal_active(args.vault, pid), effective_principal)
    if not allowed:
        _refuse(reason)

    allowed, reason = commandpolicy.is_allowed(args.mode, command)
    if not allowed:
        _refuse(reason)

    brain_bin = _brain_executable()
    if not brain_bin.is_file():
        _refuse(f"no brain executable next to {sys.executable}")

    os.environ["BRAIN_ROOT"] = args.vault
    os.environ["BRAIN_STATE_DIR"] = args.state_dir
    if args.remote_project_paths:
        os.environ["BRAIN_REMOTE_PROJECT_PATHS"] = "1"
    # The only source of the caller identity every read path enforces.
    os.environ["BRAIN_CALLER_PRINCIPAL"] = effective_principal
    # Audit provenance: which key connected, and whether the principal came
    # from that key ("ssh") or from a delegated assertion ("gateway").
    os.environ["BRAIN_CALLER_CLIENT"] = args.client
    os.environ["BRAIN_CALLER_TRANSPORT"] = "gateway" if asserted_principal is not None else "ssh"
    # Only reachable with an assertion that resolve_effective_principal()
    # already accepted. Set only for delegated requests, so other callers
    # never get a forgeable default.
    if asserted_principal is not None:
        os.environ["BRAIN_CALLER_OAUTH_CLIENT_ID"] = asserted_client_id or ""
        os.environ["BRAIN_CALLER_OAUTH_CLIENT_NAME"] = asserted_client_name or ""
    os.execv(str(brain_bin), [str(brain_bin)] + command)


if __name__ == "__main__":
    main()
