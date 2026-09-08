"""BrainGatewayClient — the provider-neutral local client interface (Phase 5B).

    AI client -> BrainGatewayClient -> MCP server (stdio JSON-RPC) -> Markdown/YAML Brain

Any local process — not just Claude Code's own built-in MCP handling —
can talk to the Brain this way. This client never reads Markdown files
directly; every operation goes through the same MCP tool functions, so
the same secret-detection, provenance, and sensitivity rules apply
uniformly no matter which client is asking (see `mcp_server.py`).

Transport: stdio, via a subprocess running `python3 -m brain.mcp_server`
— one dedicated pipe per client instance. Deliberately not a Unix socket
or localhost HTTP server (see `90_SYSTEM/mcp/README.md` "Transport
evaluation" for why): nothing here listens on anything, so there is no
discoverable local endpoint at all, which is a stronger security property
than a socket file or a bound port.

Usage:

    from brain.gateway_client import BrainGatewayClient

    with BrainGatewayClient(client_name="my-tool") as brain:
        context = brain.get_context("Family Command Center")
        brain.queue_memory("candidate fact", source="my-tool")
"""
from __future__ import annotations

import json
import os
from importlib import metadata
import subprocess
import sys
import threading
from pathlib import Path

from . import paths as paths_mod

DEFAULT_SERVER_MODULE = "brain.mcp_server"


class BrainGatewayError(RuntimeError):
    pass


def _is_installed() -> bool:
    """True when an installed distribution provides this package.

    Asked of the packaging metadata rather than of `sys.path`: a checkout that
    happens to be importable in *this* process says nothing about whether a
    freshly spawned interpreter could import it. Distribution metadata exists
    only for a real install (editable installs included, which is correct —
    they put the source on the subprocess's path too).
    """
    try:
        metadata.distribution(paths_mod.DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return False
    return True


class BrainGatewayClient:
    def __init__(self, client_name: str = "brain-gateway-client",
                 brain_root: Path | str | None = None,
                 state_dir: Path | str | None = None,
                 python_path: Path | str | None = None,
                 timeout: float = 30.0):
        self._client_name = client_name
        self._timeout = timeout

        env = dict(os.environ)
        if python_path:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(python_path) + (f":{existing}" if existing else "")
        elif "PYTHONPATH" not in env and not _is_installed():
            # Source checkout only: the subprocess needs the package's parent
            # directory on its path because nothing installed `brain`.
            #
            # An INSTALLED package must never get this (OSS-4): it imports
            # because it is installed, and synthesising a PYTHONPATH pointing
            # at site-packages would be a workaround dressed as a default —
            # exactly the kind of thing that makes an install look like it
            # works while depending on a checkout.
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        if brain_root:
            env["BRAIN_ROOT"] = str(brain_root)
        if state_dir:
            # Runtime state lives outside the vault (OSS-1), so pointing the
            # subprocess at a vault is not enough — it would otherwise derive
            # its own default state directory and open a different, empty
            # index. Anything not passed here is inherited from os.environ.
            env["BRAIN_STATE_DIR"] = str(state_dir)

        self._proc = subprocess.Popen(
            [sys.executable, "-m", DEFAULT_SERVER_MODULE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self._id_counter = 0
        self._lock = threading.Lock()
        self._initialize()

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _send(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            if self._proc.poll() is not None:
                stderr = self._proc.stderr.read() if self._proc.stderr else ""
                raise BrainGatewayError(f"MCP server process has exited. stderr: {stderr[:500]}")

            req_id = self._next_id()
            req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._proc.stdin.write(json.dumps(req) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                stderr = self._proc.stderr.read() if self._proc.stderr else ""
                raise BrainGatewayError(f"MCP server closed the connection unexpectedly. stderr: {stderr[:500]}")
            resp = json.loads(line)
            if "error" in resp:
                raise BrainGatewayError(resp["error"].get("message", str(resp["error"])))
            return resp.get("result", {})

    def _initialize(self) -> None:
        self._send("initialize", {"clientInfo": {"name": self._client_name}})

    def _call_tool(self, name: str, **arguments) -> dict:
        # Drop None-valued kwargs so tool defaults apply cleanly.
        arguments = {k: v for k, v in arguments.items() if v is not None}
        result = self._send("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content") or []
        if not content:
            return {}
        return json.loads(content[0].get("text", "{}"))

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if pipe and not pipe.closed:
                pipe.close()

    def __enter__(self) -> "BrainGatewayClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- provider-neutral operations ---------------------------------

    def search(self, query: str, limit: int = 20) -> list[dict]:
        return self._call_tool("search_memory", query=query, limit=limit).get("results", [])

    def read(self, note_id: str) -> str:
        return self._call_tool("read_memory", id=note_id).get("content", "")

    def get_context(self, query: str, limit: int = 10, include_projects: bool = True,
                     include_timeline: bool = True, include_restricted: bool = False) -> dict:
        """The recommended entry point for a new/local-model client — one
        call, concise structured results, current-vs-historical flagged,
        restricted notes excluded unless explicitly requested."""
        return self._call_tool(
            "get_context", query=query, limit=limit, include_projects=include_projects,
            include_timeline=include_timeline, include_restricted=include_restricted,
        )

    def list_projects(self) -> list[dict]:
        return self._call_tool("list_projects").get("projects", [])

    def get_project(self, project_id: str) -> dict:
        return self._call_tool("get_project", id=project_id)

    def get_project_path(self, project_id: str) -> str:
        return self._call_tool("get_project_path", id=project_id).get("path", "")

    def get_project_context(self, project_id: str) -> dict:
        return self._call_tool("project_context", id=project_id)

    def search_timeline(self, query: str = "", limit: int = 50) -> list[dict]:
        return self._call_tool("search_timeline", query=query, limit=limit).get("entries", [])

    # ---- controlled writes — same Phase 5A policy as every other client ----

    def queue_memory(self, candidate_fact: str, entities: list[str] | None = None,
                      source: str = "", source_date: str = "", proposed_destination: str = "",
                      proposed_type: str = "fact", sensitivity: str = "normal",
                      confidence: str = "assumption", reason: str = "") -> dict:
        """Level 2 write policy: stages a candidate fact for human review —
        never writes an authoritative record directly."""
        return self._call_tool(
            "queue_memory", candidate_fact=candidate_fact, entities=entities, source=source,
            source_date=source_date, proposed_destination=proposed_destination,
            proposed_type=proposed_type, sensitivity=sensitivity, confidence=confidence, reason=reason,
        )

    def remember(self, type: str, title: str, text: str = "", tags: list[str] | None = None,
                 people: list[str] | None = None, projects: list[str] | None = None,
                 sensitivity: str = "normal", confidence: str = "fact", source: str = "",
                 source_date: str = "", confirm_restricted: bool = False) -> dict:
        """Direct authoritative write. Refused server-side on a detected
        secret pattern; refused on sensitivity='restricted' unless
        confirm_restricted=True — this client cannot bypass either check."""
        return self._call_tool(
            "remember", type=type, title=title, text=text, tags=tags, people=people,
            projects=projects, sensitivity=sensitivity, confidence=confidence, source=source,
            source_date=source_date, confirm_restricted=confirm_restricted,
        )

    def update_memory(self, note_id: str, set_fields: dict | None = None,
                       append_text: str | None = None, confirm_restricted: bool = False) -> dict:
        return self._call_tool(
            "update_memory", id=note_id, set_fields=set_fields, append_text=append_text,
            confirm_restricted=confirm_restricted,
        )
