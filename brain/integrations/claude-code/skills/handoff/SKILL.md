---
name: handoff
description: Write a session handoff for a registered project after meaningful work, so a future Claude session (or the user) can continue without reconstructing the conversation. Use at the end of a substantial project work session — not for trivial sessions (a quick question, a one-line fix, pure exploration with no changes).
---

# /handoff

## When to use

Only after **meaningful** project work: real progress, a decision made, a
blocker hit, a milestone reached. Skip it for trivial sessions — a
handoff nobody needs to read is worse than no handoff (it trains people
to skip reading them).

## What to gather

- **What was attempted** — the goal of this session, in one or two sentences.
- **What changed** — concretely, what's different now vs. session start.
- **Current working state** — does it work? Is it deployed? Tested?
- **Unresolved issues** — anything left broken, unclear, or blocking.
- **Next logical action** — the single most obvious next step. Don't
  invent a roadmap; just the next step.
- **Files materially changed** — real file paths, not "various files."
- **Relevant Brain decisions** — link any `decision-*` records created or
  referenced this session.

## How to write it

```bash
echo '{
  "attempted": "...",
  "changed": "...",
  "working_state": "...",
  "unresolved": "...",
  "next_action": "...",
  "files_changed": ["path/one", "path/two"],
  "decisions": ["decision-2026-07-27-example"]
}' | brain handoff write --project <project-id>
```

This prepends a new dated session section to
`30_PROJECTS/<STATUS>/<project-id>-handoff.md` — it never overwrites
earlier sessions, so the file is a running log. If the project has no
handoff yet, this creates one.

## After writing

- Run `brain index`.
- If this session also produced a Brain-worthy decision or milestone that
  isn't already captured, use `/decision` or update the project record
  directly first — the handoff should reference those, not duplicate
  their content.
- Report what was written, briefly (see the "Brain updated:" convention
  in `CLAUDE.md`).
