---
name: decision
description: Record a significant decision in the Brain. Use when the user makes or describes a real decision (not a minor preference) worth remembering — especially ones that might otherwise get re-litigated later.
---

# /decision

1. Search first (`brain search`) — check this decision (or an earlier
   version of it) isn't already recorded.
2. Create it with `brain decision create --title "..." [--context "..."]
   [--options "..."] [--decision "..."] [--reasoning "..."]
   [--consequences "..."] [--status proposed|decided] [--people ...]
   [--projects ...] [--supersedes <old-decision-id>]`.
   - `status: decided` if final, `proposed` if still open.
   - If this **supersedes** an earlier decision, pass `--supersedes
     <old-id>` — the command marks the old one `status: superseded` and
     links forward on its own; never edit the old decision's
     Context/Reasoning to match the new outcome by hand — history stays
     intact.
3. Link the decision from the relevant project record's "Decisions" section,
   if applicable: `brain project update <project-id> --append-text "See
   [[decision-...]]"`. `"Decisions"` is not on `project section-update`'s
   section allowlist, so a precise mid-section insertion has no CLI
   primitive — `--append-text` (a new dated note referencing the decision)
   is the correct fallback. Never edit a vault file directly as a
   substitute; canonical Brain is reached only through the `brain`
   CLI/MCP, with no local fallback. If the user specifically needs the link placed inside the
   "Decisions" section itself rather than appended, stop and flag that as
   a missing tooling primitive instead of hand-editing the file.
4. Consider adding a `50_TIMELINE/` entry.
5. Run `brain index`. Report the created file and any superseded link.
