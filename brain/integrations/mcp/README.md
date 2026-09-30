# SAKO Brain MCP server

Two MCP server implementations ship with `sako-brain`, both speaking the
same stdio JSON-RPC MCP protocol and exposing the same tool set:

- **`python -m brain.mcp_server`** — in-process. Talks directly to the
  Brain Python API against a locally resolved vault (`BRAIN_ROOT` /
  `--vault` / a `90_SYSTEM/config.yaml` found by walking up from the
  caller's working directory / the per-user client config). Use this when
  the client runs on the same machine as the vault.

- **`python -m brain.mcp_bridge`** — subprocess/CLI-transport. Shells out
  to the installed `brain` CLI for every tool call, so it automatically
  inherits whatever client configuration that CLI has (see `brain
  setup` / `~/.config/sako-brain/client.toml`) — including transparently
  proxying to a remote canonical Brain over SSH if one is configured. This
  is what `brain integration install claude-code` and `brain integration
  install codex` register by default, since it works identically whether
  Brain is local or remote.

Both enforce the same write-safety policy (`brain/writepolicy.py`): every
write is scanned for secret-shaped content, and any write with
`sensitivity=restricted` requires explicit confirmation. Neither transport
can bypass it.

## Manual registration

If you're wiring up a client `brain integration install` doesn't cover
yet, point it at:

```
command: <path to the Python interpreter of your sako-brain install>
args: ["-m", "brain.mcp_bridge"]
```

No environment variables are required — the bridge resolves the `brain`
executable from `PATH` and the client config from `~/.config/sako-brain/`
the same way the CLI itself does.
