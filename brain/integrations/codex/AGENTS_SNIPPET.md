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
5. If no supporting information is found, say so instead of guessing. A read
   denied by visibility is indistinguishable from a missing record by design,
   so "nothing visible to this principal" is never "nothing exists".

When you report on Brain state (how many decisions are open, whether
something is finished), pick the instrument deliberately:

- `brain state` / `get_operational_state` — the deterministic, complete
  operational snapshot. Start there.
- `brain list` / `list_notes` — the complete listing **by metadata**
  (`type`/`status`/`project`/`tag`), read from canonical Markdown, not the
  search index. Check `complete` before quoting a number: it is false for
  any page (including the last) and false when a file claiming to be a note
  could not be read as one.
- `brain search` / `search_memory` — ranked **full-text** search with a
  limit, and no filter syntax. `status: proposed` is matched as the words
  "status" and "proposed", so it returns `decided` records and misses
  `proposed` ones. It cannot answer "how many are open".
- `brain project show` / `get_project` for where a project stands: its
  record's `Current state` section and the date it was written. The rest of
  a project record is history; a line there ("broken", "TODO") is not current
  unless `Current state` or a newer handoff says so.
- `brain get` / `read_memory` for a full record (in older records, closure
  sections and later corrections live at the *bottom*), and `brain handoff
  show` for the detail a deliberately-thin record omits.

Keep apart, and label, the metadata `status`, what the content says, what was
verified in production, and what is unverified. Metadata and content
disagreeing is a finding to report, not a licence to change a status. The
full procedure is at
https://github.com/Sako404/sako-brain/blob/main/docs/STATE_AUDIT_PROCEDURE.md

Follow the Brain's own memory and write policy — secret-pattern scanning
and restricted-sensitivity confirmation apply to every write path
uniformly, and cannot be bypassed via any transport. Do not duplicate that
policy here.
<!-- sako-brain:end -->
