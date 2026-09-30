---
name: project-close
description: Mark a project as complete and archive its Brain record. Use when the user says a project is done, finished, shipped, or should stop being tracked as active.
---

# /project-close

1. `brain project show <id>` to confirm which project and read its current
   record.
2. `brain project close <id> [--summary "what shipped, final state"]` —
   sets `status: archived`, moves the record to `30_PROJECTS/ARCHIVED/`,
   updates the registry entry to `status: archived` (the entry is kept,
   never deleted), and appends your summary if you gave one. Don't invent
   the summary — pass the user's own account of what shipped, or omit
   `--summary` and add it afterward with `brain project update <id>
   --append-text "..."` (see `/project-update`). Never add it as a direct
   edit of a vault file — canonical Brain is reached only through the
   `brain` CLI/MCP, however the client has it configured; there is no
   local fallback.
3. **Never delete the project's own working directory** and
   never delete the Brain record — archive, don't destroy (AGENTS.md rule 10).
4. Consider a `50_TIMELINE/` entry marking the completion.
5. Run `brain index`, then `brain doctor` to confirm nothing broke (e.g. no
   stale links to the old path).
6. Report the new location and registry state.
