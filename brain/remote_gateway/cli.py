"""`brain remote-gateway ...` — operator commands for the remote MCP
gateway: configuring it, running it, managing the owner login, and the
revocation runbook (item 23). Business logic only; argparse wiring lives
in `brain/cli.py` alongside `setup`/`integration`, matching the v0.11.0
pattern."""
from __future__ import annotations

import getpass
import json
import sys

from . import config as config_mod
from . import owner_auth
from . import storage as storage_mod
from .storage import Storage


def cmd_init(args) -> int:
    path = config_mod.write_config(args.canonical_uri, host=args.host, port=args.port)
    print(f"Wrote {path}")
    print(f"\nNext: brain remote-gateway set-owner-password, then "
          f"brain remote-gateway serve.")
    return 0


def _storage_for_configured_gateway() -> Storage:
    """Every command that reads/writes gateway STATE (as opposed to
    `init`, which only writes the config file) must target the exact same
    database `serve`/`create_app` would use — resolved via
    `config_mod.load_config()`, never a bare `Storage()` default path.

    A bare `Storage()` resolves its path from whatever XDG_STATE_HOME
    happens to be in the *current* process's environment, which silently
    diverges from the configured gateway the moment this command runs
    somewhere other than the exact environment `serve` runs in (e.g. an
    operator's desktop instead of the gateway host/container) — a
    previously-shipped bug that let `set-owner-password` report success
    while writing a password hash to an unrelated local file the running
    server never reads. Routing through `load_config()` means a
    mistargeted run now fails loudly (`GatewayConfigError`) instead of
    silently succeeding against the wrong state."""
    gw_config = config_mod.load_config()
    print(f"Target gateway: {gw_config.canonical_uri}  (state: {gw_config.db_path})")
    return Storage(gw_config.db_path)


def cmd_set_owner_password(args) -> int:
    try:
        storage = _storage_for_configured_gateway()
    except config_mod.GatewayConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    principal_id = getattr(args, "principal", None) or storage_mod.LEGACY_OWNER_PRINCIPAL_ID
    password = args.password or getpass.getpass(f"New password for {principal_id}: ")
    if not password or len(password) < 12:
        print("Error: password must be at least 12 characters", file=sys.stderr)
        return 2
    if not args.password:
        confirm = getpass.getpass("Confirm: ")
        if confirm != password:
            print("Error: passwords did not match", file=sys.stderr)
            return 2
    password_hash, salt = owner_auth.hash_password(password)
    storage.set_credential(principal_id, password_hash, salt)
    print(f"Password set for {principal_id}.")
    return 0


def cmd_list_clients(args) -> int:
    try:
        storage = _storage_for_configured_gateway()
    except config_mod.GatewayConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    clients = storage.list_clients()
    if getattr(args, "json", False):
        print(json.dumps(clients, indent=2))
    else:
        if not clients:
            print("No registered clients.")
        for c in clients:
            print(f"{c['client_id']}  {c['client_name']}  redirect_uris={c['redirect_uris']}")
    return 0


def cmd_revoke_client(args) -> int:
    try:
        storage = _storage_for_configured_gateway()
    except config_mod.GatewayConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    n = storage.revoke_all_for_client(args.client_id)
    deleted = storage.delete_client(args.client_id) if getattr(args, "forget", False) else False
    print(f"Revoked {n} token(s) for client {args.client_id}."
          + (" Client registration removed." if deleted else ""))
    return 0


def cmd_revoke_principal(args) -> int:
    try:
        storage = _storage_for_configured_gateway()
    except config_mod.GatewayConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    n = storage.revoke_all_for_principal(args.principal)
    print(f"Revoked {n} outstanding token(s) for {args.principal}, across every client they used. "
          "Other principals are unaffected.")
    return 0


def cmd_revoke_all(args) -> int:
    try:
        storage = _storage_for_configured_gateway()
    except config_mod.GatewayConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    n = storage.revoke_all()
    print(f"Revoked {n} outstanding token(s). All remote (ChatGPT/Claude.ai/etc.) access is now cut off. "
          "Local Claude Code, Codex, and any other client using brain setup's SSH proxy are unaffected.")
    return 0


def cmd_serve(args) -> int:
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        print(
            "Error: the 'remote-gateway' extra is not installed. "
            "Run: pip install 'sako-brain[remote-gateway]'",
            file=sys.stderr,
        )
        return 2
    from . import config as config_mod
    from .app import create_app

    try:
        gw_config = config_mod.load_config()
    except config_mod.GatewayConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.host:
        gw_config.host = args.host
    if args.port:
        gw_config.port = args.port

    app = create_app(gw_config)
    print(f"Serving remote MCP gateway at {gw_config.canonical_uri} on {gw_config.host}:{gw_config.port}")
    waitress_serve(app, host=gw_config.host, port=gw_config.port)
    return 0
