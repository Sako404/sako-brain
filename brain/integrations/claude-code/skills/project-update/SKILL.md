---
name: project-update
description: Update a registered project's Brain record with new information the user provides directly (tasks, decisions, problems, next actions) — not derived from scanning the project directory. Use when the user tells you something new about a project. For pulling changes from the actual codebase/git history, use /project-sync instead.
---

# /project-update

**Canonical Brain is reached only through the `brain` CLI/MCP, however this
client has it configured — there is no direct filesystem access to the
vault, and no local fallback.** Never mutate a Brain record (project,
handoff, decision, or any other note) by editing a vault file directly, as
a substitute for a `brain` CLI write. "Edit the record" below always means:
find or ask for the right `brain` subcommand, never: open a vault file and
write to it directly.

**If the required mutation cannot be expressed by an available `brain` CLI
command, STOP and tell the user — request a tooling decision. Do not fall
back to editing the vault directly.**

1. `brain project show <id>` (or `/project-find` first if you don't have
   the id). Read the existing record before editing — don't overwrite
   sections you haven't been told to change.
2. Apply the update:
   - **Status change** (e.g. active → on-hold): `brain project update <id>
     --status <new-status>` — moves the record between
     `30_PROJECTS/<STATUS>/` folders and updates the registry entry in the
     same call, so the record, its folder, and the registry can never drift
     apart. Never use `--set status=...` for this — it's refused on
     purpose, precisely because it would only touch frontmatter.
   - **Other frontmatter field, or an appended note** (current tasks, a new
     problem or milestone, etc.): `brain project update <id> --set
     key=value... --append-text "..."` — appends as a new dated section,
     never deletes prior content. Can be combined with `--status` in the
     same call.
   - **A specific section** (e.g. inserting into or replacing "Current
     state", "Milestones", "Problems / limitations" or "Next actions"
     rather than only appending a dated note at the very end): `brain
     project section-update <id> --section <name> --mode append|replace
     --content "..."` — `--mode append` adds to the end of that section
     only, `--mode replace` overwrites it entirely; add `--if-match
     <sha256>` (from a prior read) as an optimistic-concurrency guard when
     it matters. This is the correct primitive — do not fall back to
     editing the record file directly for this case.
   - Significant decision: use `/decision` instead of inlining it here, then
     link it from the record's "Decisions" section.
3. Run `brain index`.
4. Report exactly what changed in the record.
