---
name: project-sync
description: Sync a Brain project record with the real state of its working directory — pulls in git history, README, and file changes since the record was last updated. Use when the user asks to "sync" a project, or before relying on a project record that might be stale.
---

# /project-sync

This is the most important project workflow — follow it precisely.

1. **Read the Brain project record**: `brain project show <id>`, then `brain
   get <id>` to see the full record (purpose, state, milestones, decisions,
   problems, `updated` date).
2. **Read the real project directory**: `brain project sync <id>` runs
   read-only against the project's own working directory and returns JSON facts —
   git last-commit date, last 10 commits, working-tree dirty flag, README
   excerpt, top-level entries. It does **not** modify anything inside the
   project directory.
3. **Determine what's meaningful** since the record's `updated` date:
   compare commit dates/messages against that date, and compare the README
   excerpt / top-level structure against the record's "Architecture summary"
   and "Important locations" sections. Ignore noise (formatting-only
   commits, lockfile bumps) unless the user cares.
4. **Update the Brain record** with `brain project section-update <id>
   --section "<name>" --mode replace|append --content "..."` — never a
   direct file edit. Only these four sections are writable this way:
   - `"Current state"` — `--mode replace`, since it should reflect reality
     as of now, not accumulate every past sync.
   - `"Milestones"` — `--mode append` for meaningful completed work; never
     delete old milestones (the primitive itself only ever adds to this
     section, never removes).
   - `"Problems / limitations"` / `"Next actions"` — `--mode replace` if
     the facts suggest they've changed; don't invent problems that aren't
     evidenced.
   - `"Decisions"` is **not** on this primitive's allowlist and will be
     refused — leave it alone unless the user explicitly describes a new
     decision (use `/decision` for that instead).
5. **Do not dump source code into the Brain.** Summarize, don't paste raw
   diffs or full file contents.
6. **Do not copy the project into the Brain.** Only the summary lives here.
7. Run `brain index`.
8. **Do not modify the working project** in its own directory unless
   the user explicitly asks for a change to that project separately from
   this sync.
9. **Report what changed** — a short diff-style summary of what was updated
   in the record and why (which commits/facts triggered it).
