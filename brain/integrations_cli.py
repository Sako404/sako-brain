"""`brain setup` and `brain integration ...` — the public client-setup and
agent-integration installer (v0.11.0).

Three things a new user of the public project needs that used to require
copying private, hand-maintained files out of a specific person's desktop:

1. A way to point this client at a remote canonical Brain (`brain setup`) —
   see `remote.py` for the actual proxy mechanism this configures.
2. A way to install the official Brain skills + MCP registration into
   Claude Code (`brain integration install claude-code`).
3. The same for Codex (`brain integration install codex`).

Every install/uninstall operation here is idempotent and merge-safe: it
only ever touches the one key/section it owns in a shared config file, and
never destroys something it didn't create. Every check here is read-only
and prints no secret values.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import metadata
from importlib.resources import files as resource_files
from pathlib import Path

from . import __version__ as BRAIN_VERSION
from . import paths as paths_mod
from . import remote

SLUG = paths_mod.APP_DIRNAME

CLAUDE_SKILLS_DIR_DEFAULT = Path.home() / ".claude" / "skills"
CLAUDE_MCP_CONFIG_DEFAULT = Path.home() / ".claude.json"
CODEX_CONFIG_DEFAULT = Path.home() / ".codex" / "config.toml"
CODEX_AGENTS_MD_DEFAULT = Path.home() / ".codex" / "AGENTS.md"

MARKER_BEGIN = f"<!-- {SLUG}:begin -->"
MARKER_END = f"<!-- {SLUG}:end -->"

# A previous manually-maintained deployment might still have this pointed at
# a private wrapper script; the doctor check flags it, the installer never
# sets it (see mcp_bridge.py's own PATH-resolution fallback).
_LEGACY_BRIDGE_EXECUTABLE_ENV = "BRAIN_MCP_BRIDGE_EXECUTABLE"


# ---------------------------------------------------------------------------
# Shared resource access
# ---------------------------------------------------------------------------

def _integrations_root():
    return resource_files("brain") / "integrations"


def shipped_skill_names() -> list[str]:
    skills_dir = _integrations_root() / "claude-code" / "skills"
    return sorted(
        p.name for p in skills_dir.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    )


def _read_resource_text(*parts: str) -> str:
    node = _integrations_root()
    for p in parts:
        node = node / p
    return node.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# brain setup
# ---------------------------------------------------------------------------

def cmd_setup(args) -> int:
    if getattr(args, "show_host_key", False):
        if not args.server:
            print("Error: --show-host-key requires --server", file=sys.stderr)
            return 2
        try:
            report = remote.show_host_key(args.server, str(args.port or 22))
        except remote.RemoteConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(report)
        print(
            "\nVerify the fingerprint(s) above out-of-band (e.g. against what "
            "the server admin published, or by checking on the server itself) "
            "before trusting them. Save the key lines above to a file and pass "
            "it to `brain setup --known-hosts-file`."
        )
        return 0

    missing = [f"--{name.replace('_', '-')}" for name, val in (
        ("server", args.server), ("user", args.user),
        ("read_identity", args.read_identity), ("known_hosts_file", args.known_hosts_file),
    ) if not val]
    if missing:
        print(f"Error: setup requires {', '.join(missing)} (or --show-host-key alone)", file=sys.stderr)
        return 2

    read_identity = Path(args.read_identity).expanduser()
    if not read_identity.is_file():
        print(f"Error: --read-identity {read_identity} does not exist", file=sys.stderr)
        return 2
    write_identity = None
    if args.write_identity:
        write_identity = Path(args.write_identity).expanduser()
        if not write_identity.is_file():
            print(f"Error: --write-identity {write_identity} does not exist", file=sys.stderr)
            return 2

    known_hosts_src = Path(args.known_hosts_file).expanduser()
    if not known_hosts_src.is_file():
        print(f"Error: --known-hosts-file {known_hosts_src} does not exist", file=sys.stderr)
        return 2
    lines = [ln for ln in known_hosts_src.read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.strip().startswith("#")]
    if not lines or any(len(ln.split()) < 3 for ln in lines):
        print(
            f"Error: {known_hosts_src} does not look like an SSH known_hosts file "
            "(each non-comment line needs at least: host, key-type, key)",
            file=sys.stderr,
        )
        return 2

    for f in (read_identity, write_identity):
        if f is None:
            continue
        mode = f.stat().st_mode & 0o777
        if mode & 0o077:
            print(f"Warning: {f} is readable by group/other (mode {oct(mode)}) — "
                  f"consider `chmod 600 {f}`.", file=sys.stderr)

    cfg = remote.ClientConfig(
        server=args.server,
        ssh_user=args.user,
        ssh_port=str(args.port or 22),
        identity_file=str(read_identity),
        write_identity_file=str(write_identity) if write_identity else None,
        profile=args.profile,
    )

    cfg_dir = remote.config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    try:
        cfg_dir.chmod(0o700)
    except OSError:
        pass

    known_hosts_dst = remote.ssh_known_hosts_path()
    known_hosts_dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths_mod.ensure_private_file(known_hosts_dst)

    ssh_cfg_dst = remote.ssh_config_path()
    ssh_cfg_dst.write_text(remote.render_ssh_config(cfg, known_hosts_dst), encoding="utf-8")
    paths_mod.ensure_private_file(ssh_cfg_dst)

    client_toml_dst = remote.client_config_path()
    client_toml_dst.write_text(remote.render_client_toml(cfg), encoding="utf-8")
    paths_mod.ensure_private_file(client_toml_dst)

    result = {
        "client_config": str(client_toml_dst),
        "ssh_config": str(ssh_cfg_dst),
        "ssh_known_hosts": str(known_hosts_dst),
        "server": cfg.server,
        "read_identity": cfg.identity_file,
        "write_identity": cfg.effective_write_identity,
    }
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote {client_toml_dst}")
        print(f"Wrote {ssh_cfg_dst}")
        print(f"Wrote {known_hosts_dst} ({len(lines)} host key line(s))")
        print(f"\nCanonical Brain: {cfg.ssh_user}@{cfg.server}:{cfg.ssh_port}")
        print("\nNext: `brain state --json` to verify connectivity, then "
              "`brain integration install claude-code` / `brain integration install codex`.")
    return 0


# ---------------------------------------------------------------------------
# Claude Code integration
# ---------------------------------------------------------------------------

def _claude_mcp_entry() -> dict:
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "brain.mcp_bridge"],
    }


def install_claude_code(skills_dir: Path | None = None, mcp_config_path: Path | None = None,
                         force: bool = False) -> dict:
    skills_dir = skills_dir or CLAUDE_SKILLS_DIR_DEFAULT
    mcp_config_path = mcp_config_path or CLAUDE_MCP_CONFIG_DEFAULT
    skills_dir.mkdir(parents=True, exist_ok=True)

    src_root = _integrations_root() / "claude-code" / "skills"
    installed, skipped = [], []
    for name in shipped_skill_names():
        dest = skills_dir / name
        src = src_root / name
        # importlib.resources may hand back a real filesystem Path (the
        # normal case — an installed wheel unpacks to site-packages) or a
        # zip-backed Traversable; resolve to a real path for symlinking,
        # falling back to a materialized copy if the package is zipped.
        try:
            src_path = Path(str(src))
            src_is_real = src_path.is_dir()
        except Exception:
            src_is_real = False

        if dest.is_symlink():
            current_target = os.readlink(dest)
            if src_is_real and Path(current_target).resolve() == src_path.resolve():
                installed.append(name)  # already correctly installed
                continue
            if not force:
                skipped.append({"skill": name, "reason": f"{dest} already points elsewhere ({current_target})"})
                continue
            dest.unlink()
        elif dest.exists():
            if not force:
                skipped.append({"skill": name, "reason": f"{dest} exists and is not a Brain-managed symlink"})
                continue
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()

        if src_is_real:
            dest.symlink_to(src_path)
        else:
            shutil.copytree(str(src), dest)
        installed.append(name)

    mcp_result = _merge_json_mcp_server(mcp_config_path, SLUG, _claude_mcp_entry())

    return {
        "skills_dir": str(skills_dir),
        "skills_installed": installed,
        "skills_skipped": skipped,
        "mcp_config": mcp_result,
    }


def uninstall_claude_code(skills_dir: Path | None = None, mcp_config_path: Path | None = None) -> dict:
    skills_dir = skills_dir or CLAUDE_SKILLS_DIR_DEFAULT
    mcp_config_path = mcp_config_path or CLAUDE_MCP_CONFIG_DEFAULT
    src_root = _integrations_root() / "claude-code" / "skills"

    removed, left = [], []
    for name in shipped_skill_names():
        dest = skills_dir / name
        if not dest.is_symlink():
            continue
        try:
            target = Path(os.readlink(dest)).resolve()
            src_path = Path(str(src_root / name)).resolve()
        except Exception:
            left.append(name)
            continue
        if target == src_path or (SLUG in str(target) and "integrations" in str(target)):
            dest.unlink()
            removed.append(name)
        else:
            left.append(name)

    mcp_result = _remove_json_mcp_server(mcp_config_path, SLUG)
    return {"skills_removed": removed, "skills_left_untouched": left, "mcp_config": mcp_result}


def _merge_json_mcp_server(config_path: Path, server_name: str, entry: dict) -> dict:
    """Surgical merge: only `mcpServers.<server_name>` is touched. Every
    other key in the file — including other servers' secrets — is
    round-tripped byte-for-byte unaffected (loaded, patched, re-dumped)."""
    if config_path.is_file():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        existed = "mcpServers" in data and server_name in data.get("mcpServers", {})
    else:
        data = {}
        existed = False
    data.setdefault("mcpServers", {})
    data["mcpServers"][server_name] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"path": str(config_path), "action": "updated" if existed else "added"}


def _remove_json_mcp_server(config_path: Path, server_name: str) -> dict:
    if not config_path.is_file():
        return {"path": str(config_path), "action": "absent"}
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if server_name not in data.get("mcpServers", {}):
        return {"path": str(config_path), "action": "absent"}
    del data["mcpServers"][server_name]
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"path": str(config_path), "action": "removed"}


# ---------------------------------------------------------------------------
# Codex integration
# ---------------------------------------------------------------------------

def _codex_mcp_toml_block() -> str:
    return (
        "\n[mcp_servers." + SLUG + "]\n"
        f'command = "{sys.executable}"\n'
        'args = ["-m", "brain.mcp_bridge"]\n'
    )


def install_codex(config_path: Path | None = None, agents_md_path: Path | None = None,
                   force: bool = False) -> dict:
    config_path = config_path or CODEX_CONFIG_DEFAULT
    agents_md_path = agents_md_path or CODEX_AGENTS_MD_DEFAULT

    toml_result = _merge_codex_toml(config_path)
    md_result = _merge_marked_section(
        agents_md_path, MARKER_BEGIN, MARKER_END,
        _read_resource_text("codex", "AGENTS_SNIPPET.md"),
    )
    return {"config_toml": toml_result, "agents_md": md_result}


def uninstall_codex(config_path: Path | None = None, agents_md_path: Path | None = None) -> dict:
    config_path = config_path or CODEX_CONFIG_DEFAULT
    agents_md_path = agents_md_path or CODEX_AGENTS_MD_DEFAULT
    return {
        "config_toml": _remove_codex_toml_block(config_path),
        "agents_md": _remove_marked_section(agents_md_path, MARKER_BEGIN, MARKER_END),
    }


def _merge_codex_toml(config_path: Path) -> dict:
    """Line-level merge: replaces an existing `[mcp_servers.<slug>]`
    table if present, otherwise appends one. Every other line (other
    servers, other settings) is preserved verbatim — this never parses the
    file into a generic TOML object and re-serializes it, which would risk
    reformatting or reordering content that belongs to the user."""
    header = f"[mcp_servers.{SLUG}]"
    block = _codex_mcp_toml_block()
    if not config_path.is_file():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(block.lstrip("\n") + "\n", encoding="utf-8")
        return {"path": str(config_path), "action": "created"}

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    if start is None:
        new_text = text.rstrip("\n") + "\n" + block
        config_path.write_text(new_text, encoding="utf-8")
        return {"path": str(config_path), "action": "appended"}

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("[") and lines[i].strip() != header:
            end = i
            break
    new_lines = lines[:start] + [block.lstrip("\n") + "\n"] + lines[end:]
    config_path.write_text("".join(new_lines), encoding="utf-8")
    return {"path": str(config_path), "action": "replaced"}


def _remove_codex_toml_block(config_path: Path) -> dict:
    header = f"[mcp_servers.{SLUG}]"
    if not config_path.is_file():
        return {"path": str(config_path), "action": "absent"}
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
    if start is None:
        return {"path": str(config_path), "action": "absent"}
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("[") and lines[i].strip() != header:
            end = i
            break
    del lines[start:end]
    config_path.write_text("".join(lines), encoding="utf-8")
    return {"path": str(config_path), "action": "removed"}


def _merge_marked_section(path: Path, begin: str, end: str, content: str) -> dict:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "action": "created"}
    text = path.read_text(encoding="utf-8")
    if begin in text and end in text:
        pre = text.split(begin, 1)[0]
        post = text.split(end, 1)[1]
        new_text = pre + content + post
        action = "replaced"
    else:
        sep = "" if text.endswith("\n") or not text else "\n"
        new_text = text + sep + "\n" + content
        action = "appended"
    path.write_text(new_text, encoding="utf-8")
    return {"path": str(path), "action": action}


def _remove_marked_section(path: Path, begin: str, end: str) -> dict:
    if not path.is_file():
        return {"path": str(path), "action": "absent"}
    text = path.read_text(encoding="utf-8")
    if begin not in text or end not in text:
        return {"path": str(path), "action": "absent"}
    pre = text.split(begin, 1)[0]
    post = text.split(end, 1)[1]
    path.write_text(pre + post, encoding="utf-8")
    return {"path": str(path), "action": "removed"}


# ---------------------------------------------------------------------------
# brain integration install / uninstall / doctor
# ---------------------------------------------------------------------------

def cmd_integration(args) -> int:
    action = args.integration_command
    target = getattr(args, "target", None)
    as_json = getattr(args, "json", False)
    force = getattr(args, "force", False)

    if action == "install":
        if target == "claude-code":
            result = install_claude_code(force=force)
        elif target == "codex":
            result = install_codex(force=force)
        else:
            print("Error: specify a target — claude-code or codex", file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            _print_install_report(target, result)
        return 0

    if action == "uninstall":
        if target == "claude-code":
            result = uninstall_claude_code()
        elif target == "codex":
            result = uninstall_codex()
        else:
            print("Error: specify a target — claude-code or codex", file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result, indent=2))
        return 0

    if action == "doctor":
        if target == "remote":
            base_url = getattr(args, "base_url", None)
            if not base_url:
                print("Error: `brain integration doctor remote` requires --base-url", file=sys.stderr)
                return 2
            report = run_remote_doctor(base_url)
        else:
            report = run_doctor(target)
        if as_json:
            print(json.dumps(report, indent=2))
        else:
            _print_doctor_report(report)
        return 0 if report.get("ok") else 1

    print("Error: unknown integration command", file=sys.stderr)
    return 2


def _print_install_report(target: str, result: dict) -> None:
    print(f"{target} integration installed.")
    if "skills_installed" in result:
        print(f"  skills installed: {len(result['skills_installed'])}")
        for s in result["skills_skipped"]:
            print(f"  SKIPPED {s['skill']}: {s['reason']}")
        mcp = result["mcp_config"]
        print(f"  MCP config {mcp['action']}: {mcp['path']}")
    else:
        print(f"  config.toml: {result['config_toml']['action']} ({result['config_toml']['path']})")
        print(f"  AGENTS.md: {result['agents_md']['action']} ({result['agents_md']['path']})")


def _print_doctor_report(report: dict) -> None:
    for check in report["checks"]:
        status = "OK" if check["ok"] else "FAIL"
        print(f"[{status}] {check['name']}: {check['detail']}")
    print(f"\nOverall: {'OK' if report['ok'] else 'PROBLEMS FOUND'}")


# ---------------------------------------------------------------------------
# brain integration doctor
# ---------------------------------------------------------------------------

def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def _run_brain_capture(argv: list[str], timeout: float = 20.0):
    exe = shutil.which("brain") or "brain"
    try:
        return subprocess.run([exe, *argv], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return exc


def run_doctor(target: str | None = None) -> dict:
    checks: list[dict] = []

    checks.append(_check("brain_package_version", True, f"{SLUG} {BRAIN_VERSION}"))

    cfg_path = remote.client_config_path()
    try:
        client_cfg = remote.load_client_config()
    except remote.RemoteConfigError as exc:
        client_cfg = None
        checks.append(_check("client_config", False, str(exc)))
    else:
        if client_cfg is None:
            checks.append(_check("client_config", True,
                                  f"no remote configured at {cfg_path} — local-vault mode"))
        else:
            checks.append(_check("client_config", True,
                                  f"remote configured: {client_cfg.ssh_user}@{client_cfg.server}:{client_cfg.ssh_port}"))

    if client_cfg is not None:
        ssh_cfg = remote.ssh_config_path()
        checks.append(_check("ssh_config", ssh_cfg.is_file(),
                              str(ssh_cfg) if ssh_cfg.is_file() else f"missing: {ssh_cfg}"))
        known_hosts = remote.ssh_known_hosts_path()
        n = 0
        if known_hosts.is_file():
            n = len([ln for ln in known_hosts.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")])
        checks.append(_check("ssh_known_hosts", known_hosts.is_file() and n > 0,
                              f"{n} host key line(s) at {known_hosts}" if n else f"missing/empty: {known_hosts}"))

    legacy_env = os.environ.get(_LEGACY_BRIDGE_EXECUTABLE_ENV)
    checks.append(_check("no_legacy_bridge_executable_override", not legacy_env,
                          "not set" if not legacy_env else f"set to {legacy_env} — points at a private wrapper, unset it"))
    brain_local = os.environ.get("BRAIN_LOCAL")
    checks.append(_check("no_brain_local_fallback", not brain_local,
                          "not set" if not brain_local else f"BRAIN_LOCAL={brain_local} is set"))

    proc = _run_brain_capture(["state", "--json"])
    if isinstance(proc, Exception):
        checks.append(_check("canonical_brain_connectivity", False, str(proc)))
    else:
        ok = proc.returncode == 0
        detail = proc.stderr.strip()[-300:] if not ok else "reachable"
        if ok:
            try:
                state = json.loads(proc.stdout)
                detail = f"reachable, brain_version={state.get('brain_version')}"
            except json.JSONDecodeError:
                detail = "reachable (non-JSON response)"
        checks.append(_check("canonical_brain_connectivity", ok, detail))

    bridge_proc = subprocess.run(
        [sys.executable, "-m", "brain.mcp_bridge"],
        input='{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}\n'
              '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
              '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n',
        capture_output=True, text=True, timeout=30,
    )
    tool_count = 0
    for line in bridge_proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == 1 and "result" in obj:
            tool_count = len(obj["result"].get("tools", []))
    checks.append(_check("mcp_bridge_startup", bridge_proc.returncode == 0, "started" if bridge_proc.returncode == 0
                          else bridge_proc.stderr.strip()[-300:]))
    checks.append(_check("mcp_capabilities", tool_count > 0, f"{tool_count} tool(s) advertised"))

    if target in (None, "claude-code"):
        expected = set(shipped_skill_names())
        skills_dir = CLAUDE_SKILLS_DIR_DEFAULT
        installed = {p.name for p in skills_dir.glob("*") if p.is_symlink() and p.name in expected}
        checks.append(_check("claude_skills_installed", installed == expected,
                              f"{len(installed)}/{len(expected)} installed"
                              + ("" if installed == expected else f" — missing: {sorted(expected - installed)}")))
        mcp_registered = False
        if CLAUDE_MCP_CONFIG_DEFAULT.is_file():
            try:
                data = json.loads(CLAUDE_MCP_CONFIG_DEFAULT.read_text(encoding="utf-8"))
                mcp_registered = SLUG in data.get("mcpServers", {})
            except json.JSONDecodeError:
                pass
        checks.append(_check("claude_mcp_registered", mcp_registered,
                              "registered" if mcp_registered else f"not found in {CLAUDE_MCP_CONFIG_DEFAULT}"))

    if target in (None, "codex"):
        codex_registered = False
        if CODEX_CONFIG_DEFAULT.is_file():
            codex_registered = f"[mcp_servers.{SLUG}]" in CODEX_CONFIG_DEFAULT.read_text(encoding="utf-8")
        checks.append(_check("codex_mcp_registered", codex_registered,
                              "registered" if codex_registered else f"not found in {CODEX_CONFIG_DEFAULT}"))
        agents_present = False
        if CODEX_AGENTS_MD_DEFAULT.is_file():
            agents_present = MARKER_BEGIN in CODEX_AGENTS_MD_DEFAULT.read_text(encoding="utf-8")
        checks.append(_check("codex_agents_md_section", agents_present,
                              "present" if agents_present else f"not found in {CODEX_AGENTS_MD_DEFAULT}"))

    old_vault_env = os.environ.get("BRAIN_ROOT", "")
    looks_like_old_nextcloud = "Nextcloud" in old_vault_env and "_AI_Brain" in old_vault_env
    checks.append(_check("no_known_old_local_vault_path", not looks_like_old_nextcloud,
                          "clear" if not looks_like_old_nextcloud
                          else f"BRAIN_ROOT points at a retired local-vault-shaped path: {old_vault_env}"))

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


# ---------------------------------------------------------------------------
# brain integration doctor remote (v0.12.0, item 16) — checked purely as an
# external HTTP client, exactly the vantage point ChatGPT/Claude.ai have.
# stdlib `urllib.request` only: this is a client-side check, not part of the
# gateway itself, so it must work without the 'remote-gateway' extra.
# ---------------------------------------------------------------------------

class _CaseInsensitiveHeaders(dict):
    """`dict(http.client.HTTPMessage)` loses case-insensitive lookup —
    this keeps it, since server header casing (WWW-Authenticate vs.
    Www-Authenticate) isn't something a client should have to guess."""

    def __init__(self, message):
        super().__init__((k.lower(), v) for k, v in (message.items() if message else []))

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


def _curl_available() -> bool:
    return shutil.which("curl") is not None


def _curl_request(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                   timeout: float = 10.0):
    """Shells out to curl rather than using urllib directly: some Cloudflare
    zones' bot-fight/WAF rules (error 1010) key off TLS-handshake
    fingerprinting and flag Python's own TLS stack specifically — a real
    production gateway behind such a zone reachable fine via curl (and,
    in practice, via the real HTTP clients ChatGPT/Claude.ai's backends
    use) was still erroneously blocked when this check used urllib
    directly. curl is the same tool `brain setup`'s own wheel download
    already assumes is present (see docs/REMOTE_ACCESS.md), so this adds
    no new assumption."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".headers") as header_file:
        # No custom User-Agent: curl's own default is what proved reachable
        # against a real Cloudflare-fronted deployment during development —
        # some zones' Bot Fight Mode blocks an unrecognized custom UA string
        # (error 1010) while leaving curl's own identifiable default alone.
        argv = ["curl", "-s", "-S", "-m", str(timeout), "-D", header_file.name,
                "-X", method, "-o", "-"]
        for k, v in (headers or {}).items():
            argv += ["-H", f"{k}: {v}"]
        if body is not None:
            argv += ["--data-binary", "@-"]
        argv.append(url)
        try:
            proc = subprocess.run(argv, input=body, capture_output=True, timeout=timeout + 5)
        except subprocess.TimeoutExpired as exc:
            return None, _CaseInsensitiveHeaders(None), str(exc).encode()
        if proc.returncode != 0:
            return None, _CaseInsensitiveHeaders(None), (proc.stderr or b"curl failed")
        header_text = Path(header_file.name).read_text(errors="replace")
    status = 0
    resp_headers: dict[str, str] = {}
    for line in header_text.splitlines():
        if line.startswith("HTTP/"):
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
            resp_headers = {}  # a redirect leaves multiple status blocks — keep only the last
        elif ":" in line:
            k, _, v = line.partition(":")
            resp_headers[k.strip()] = v.strip()
    return status, _CaseInsensitiveHeaders(resp_headers), proc.stdout


def _http_get(url: str, headers: dict | None = None, timeout: float = 10.0):
    if _curl_available():
        return _curl_request("GET", url, headers, timeout=timeout)

    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), _CaseInsensitiveHeaders(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _CaseInsensitiveHeaders(exc.headers), exc.read()
    except Exception as exc:  # noqa: BLE001 — network/TLS failures, reported as a failed check
        return None, _CaseInsensitiveHeaders(None), str(exc).encode()


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 10.0):
    body = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", **(headers or {})}

    if _curl_available():
        return _curl_request("POST", url, req_headers, body=body, timeout=timeout)

    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), _CaseInsensitiveHeaders(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _CaseInsensitiveHeaders(exc.headers), exc.read()
    except Exception as exc:  # noqa: BLE001
        return None, _CaseInsensitiveHeaders(None), str(exc).encode()


def run_remote_doctor(base_url: str) -> dict:
    checks: list[dict] = []
    base_url = base_url.rstrip("/")
    mcp_url = f"{base_url}/mcp"

    is_https = base_url.startswith("https://")
    checks.append(_check("https_endpoint", is_https,
                          base_url if is_https else f"{base_url} is not HTTPS"))

    status, _headers, body = _http_get(f"{base_url}/healthz")
    checks.append(_check("endpoint_reachable", status == 200,
                          "reachable" if status == 200 else f"status={status} body={body[:200]!r}"))

    status, headers, body = _http_get(f"{base_url}/.well-known/oauth-protected-resource")
    prm_ok = status == 200
    prm = {}
    if prm_ok:
        try:
            prm = json.loads(body)
            prm_ok = "authorization_servers" in prm and prm.get("resource", "").rstrip("/") == mcp_url
        except json.JSONDecodeError:
            prm_ok = False
    checks.append(_check("protected_resource_metadata", prm_ok,
                          f"resource={prm.get('resource')!r}" if prm_ok else f"status={status}"))

    as_url = prm.get("authorization_servers", [base_url])[0] if prm_ok else base_url
    status, _headers, body = _http_get(f"{as_url}/.well-known/oauth-authorization-server")
    asm_ok = status == 200
    asm = {}
    if asm_ok:
        try:
            asm = json.loads(body)
            asm_ok = "authorization_endpoint" in asm and "token_endpoint" in asm
        except json.JSONDecodeError:
            asm_ok = False
    checks.append(_check("authorization_server_metadata", asm_ok,
                          f"endpoints present" if asm_ok else f"status={status}"))

    scopes_ok = asm_ok and set(asm.get("scopes_supported", [])) == {"brain.read", "brain.write", "brain.restricted"}
    checks.append(_check("scopes_advertised", scopes_ok,
                          str(asm.get("scopes_supported")) if asm_ok else "n/a"))

    pkce_ok = asm_ok and asm.get("code_challenge_methods_supported") == ["S256"]
    checks.append(_check("pkce_s256_only", pkce_ok,
                          str(asm.get("code_challenge_methods_supported")) if asm_ok else "n/a"))

    status, headers, body = _http_post_json(mcp_url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                         "params": {}})
    unauth_ok = status == 401 and "WWW-Authenticate" in headers and "resource_metadata" in headers.get("WWW-Authenticate", "")
    checks.append(_check("rejects_unauthenticated", unauth_ok,
                          headers.get("WWW-Authenticate", f"status={status}") if status else str(body[:200])))

    status, _headers, body = _http_post_json(mcp_url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                          "params": {}},
                                               headers={"Authorization": "Bearer not-a-real-token"})
    invalid_ok = status == 401
    checks.append(_check("rejects_invalid_token", invalid_ok, f"status={status}"))

    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "note": "read/write/restricted-scope live calls and server-instructions/tool-annotation checks "
                    "require a real access token — see docs/REMOTE_ACCESS.md's acceptance runbook for the "
                    "full authenticated pass; this unauthenticated check proves discovery and the fail-closed "
                    "boundary from a genuine external vantage point."}
