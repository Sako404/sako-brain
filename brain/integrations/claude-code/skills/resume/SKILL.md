---
name: resume
description: Resume work on a registered project using its latest handoff, Brain record, and current filesystem/git state. Use when the user says "continue X", "resume Y", "where did we leave off with Z", or picks a project back up after a gap.
---

# /resume

## Workflow

1. **Resolve the project** via the registry — `brain project show <id>`, or
   `/project-find` if you only have a name/alias.
2. **Read the latest handoff**, if one exists: `brain handoff show <id>`.
   This gives you what was attempted, what changed, unresolved issues, and
   the next logical action from the last session — treat "next logical
   action" as a strong starting hypothesis, not a fixed instruction.
3. **Read the full Brain project record** — `brain get <id>` — for the
   broader picture the handoff doesn't repeat (purpose, architecture,
   milestones, relationships). `brain project show <id>` from step 1 is
   registry metadata only (status, path, category) — it is not this
   record's actual content. Always via `brain get`, never by opening
   `30_PROJECTS/<STATUS>/<id>.md` directly — that path names where the
   record lives inside the vault, not a local file to open; there is no
   local fallback.
4. **Inspect current project filesystem/git state** — the handoff and
   Brain record are snapshots; the working directory is ground truth.
   Check recent git log/status, and skim anything the handoff flagged as
   unresolved to see if it's since changed.
5. **Reconcile**: if the filesystem has moved on since the handoff (new
   commits, different state than described), say so — don't silently
   trust a stale handoff over current evidence. This is the same
   source-priority rule as everywhere else in the Brain: current
   project evidence beats an older Brain snapshot.
6. **Summarize where things stand** for the user in a few sentences
   before diving in, so they can redirect you if the assumed next step is
   wrong.

## If no handoff exists

Fall back to the Brain project record alone plus a fresh look at the
project directory (same pattern as `/project-sync`). Say plainly that
there's no session handoff to work from, rather than implying continuity
that isn't there.
