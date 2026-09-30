# SAKO Brain — Codex integration

Registers the Brain MCP server with Codex and adds generic Brain-usage
instructions to your Codex `AGENTS.md`, so Codex can search, read, and
write your Brain without shelling out through a sandboxed subprocess.

## Install

```
brain integration install codex
```

This:

- adds/updates an `[mcp_servers.sako-brain]` block in `~/.codex/config.toml`,
  pointing at the Brain MCP bridge of the currently-running installation —
  every other key in `config.toml` is left untouched,
- merges a generic "SAKO Brain integration" section into `~/.codex/AGENTS.md`
  between `<!-- sako-brain:begin -->` / `<!-- sako-brain:end -->` markers —
  anything else in that file, before or after the markers, is left
  untouched. If the markers aren't present yet, the section is appended
  once; if they are, the section between them is replaced in place (so
  re-running after an upgrade updates the instructions without
  duplicating them).

Safe to re-run any time (idempotent).

## Why the MCP path

Codex's per-shell-command sandbox does not apply to the MCP server's own
child process, so `sako-brain` MCP tools work in every sandbox tier
(including the default, network-blocked one) with no extra configuration.
A model-issued shell command running `brain` directly is still subject to
that sandbox and needs network access enabled for that invocation to reach
a remote canonical Brain — the generic `AGENTS.md` section documents this
and steers Codex toward the MCP tools first.

## Verify

```
brain integration doctor codex
```

## Uninstall

```
brain integration uninstall codex
```

Removes the `[mcp_servers.sako-brain]` block from `config.toml` and the
marked section from `AGENTS.md`. Everything else in both files is left
untouched.
