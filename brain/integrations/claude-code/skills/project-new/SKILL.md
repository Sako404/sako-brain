---
name: project-new
description: Register a new project in the Brain — add it to the registry and create its project record. Use when the user wants to start tracking a project (existing or new) under a configured projects root in the Brain.
---

# /project-new

1. **Search first** — `brain projects` and `brain search "<name>"` — to make
   sure this project isn't already registered under a different name/alias.
2. Confirm the working directory under a configured projects root (see
   `brain status`) exists. If the
   user hasn't given a path, consider running `/project-find` or
   `brain project discover` to locate it — never guess a path.
3. Pick a stable `id` (`project-<slug>`).
4. Create it with `brain project create --id project-<slug> --name "<Name>"
   --path "<projects-root>/<dir>" [--status active|planned|on-hold]
   [--category <free-form>] [--aliases ...]` — this adds the registry entry
   and the record (from `80_TEMPLATES/project.md`, in the status folder
   matching the status you gave) in one atomic step; it refuses on a
   duplicate `id` or `path`.
5. Fill in whatever the user told you (purpose, current state, etc.) with
   `brain project update project-<slug> --append-text "..."`, or `brain
   project section-update project-<slug> --section <name> --mode replace
   --content "..."` for one of the four fixed sections (Current state,
   Milestones, Problems / limitations, Next actions) — leave sections
   blank rather than inventing content, and never edit a vault file
   directly; canonical Brain is reached only through the `brain` CLI/MCP,
   with no local fallback.
6. **Never copy project source files into the Brain.** The record only
   references the path.
7. Run `brain index`.
8. Report the created registry entry and record path.
