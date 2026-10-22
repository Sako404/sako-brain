---
name: timeline
description: View or add to the Brain's timeline of dated events. Use when the user asks "what happened when", wants a chronological view, or describes something that should be logged as a dated event.
---

# /timeline

**Viewing:** run `brain timeline` (add `--limit N`) for a newest-first list.
Cross-reference `brain get <id>` for full detail on any entry. An entry
marked `[superseded by <id>]` describes a past state; report the newer
entry, not the old title.

**Adding an event:**
1. Search first to avoid a duplicate entry for the same event.
2. `brain timeline add --title "..." --date <YYYY-MM-DD> [--what-happened
   "..."] [--why-it-matters "..."] [--people ...] [--projects ...]` —
   creates `50_TIMELINE/event-<date>-<slug>.md` from the event template.
   `--date` is the event's own date (sets `valid_from`, what sorts the
   timeline) — not necessarily today.
   If this event replaces the state an older one described (a problem
   logged earlier is now fixed), add `--supersedes <old-event-id>`: the
   old event is marked `status: superseded` and linked forward, and
   `brain timeline` / `brain state` show it as superseded.
3. Run `brain index`. Report the created file.
