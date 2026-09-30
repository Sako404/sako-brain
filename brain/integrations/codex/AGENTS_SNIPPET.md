<!-- sako-brain:begin -->
## SAKO Brain integration

Canonical long-term memory is SAKO Brain, reached **only** through the
`brain` CLI (or an MCP tool backed by it) — never a local filesystem path.
No local copy of Brain data is authoritative; there is no fallback vault.

When a task may materially depend on the user's personal context,
preferences, projects, prior decisions, or history:

1. Prefer the `sako-brain` MCP server's tools (`get_context`,
   `search_memory`, `get_project`, etc. — see its `tools/list`) over
   shelling out to `brain` directly: the MCP server process is not subject
   to the same per-command sandbox a model-issued shell command is, so it
   works unmodified in every sandbox tier without needing any extra
   permission. `brain context`, `brain search`, `brain get`, `brain
   project`, `brain handoff` (the CLI directly) are an equally valid
   fallback — never read a local file instead — but a sandboxed shell
   command needs network access enabled for that sandbox tier to reach a
   remote canonical server at all.
2. Do not load the whole vault.
3. Treat Brain Markdown (as returned by these commands) as authoritative
   and its search index as a rebuildable cache.
4. For project work, verify the actual project repository and working-tree
   state. A Brain record or handoff is context, not proof of current state.
5. If no supporting information is found, say so instead of guessing.

Follow the Brain's own memory and write policy — secret-pattern scanning
and restricted-sensitivity confirmation apply to every write path
uniformly, and cannot be bypassed via any transport. Do not duplicate that
policy here.
<!-- sako-brain:end -->
