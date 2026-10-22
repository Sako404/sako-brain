---
name: brain-recall
description: Use BEFORE answering any request that could materially depend on prior personal, project, decision, preference, or historical context — e.g. "where did we finish X", "what did we decide about Y", "continue Z work", "what was wrong with W", "what hours was I doing at V", "continue what we were doing yesterday". Do NOT use for generic programming questions, general-knowledge questions, or anything with no plausible connection to the user's own history — searching Brain for those just adds noise and latency.
---

# brain-recall — automatic context retrieval

The user should never have to type `/find` before an ordinary question
that depends on things already in the Brain. This skill is that lookup,
run silently before you answer.

## When this applies

A request references prior context if it's about: a specific project,
person, decision, employment/vehicle/financial history, a preference
stated before, "continue"/"resume" language, or anything phrased as if
you already have shared history with the user. If the request is a
one-off technical/programming/general-knowledge question with no personal
angle, skip this — searching Brain for "how do I center a div" is just
noise.

## Workflow

1. **Identify likely entities/topics** from the request — names, project
   names, areas (work, vehicles, finance, health), dates.
2. **Search Brain**: `brain search "<terms>"` — try the most specific
   phrasing first (e.g. the project's actual name), then broaden if
   nothing useful comes back. Multiple targeted searches beat one vague
   one.

   `brain search` is ranked **full-text** search with a limit, and it has
   no filter syntax — it is the right tool for "which notes mention this?"
   and the wrong one for "list everything that is still open". For that,
   use `brain list --type decision --status proposed` (a complete listing
   by metadata, which reports its own `total_visible` and `complete` — and
   `complete` is false for any page, the last one included, and false when
   a file claiming to be a note could not be read), or
   `brain state` for the whole operational snapshot. A query like
   `"status: proposed"` typed into `search` matches the *words*, so it
   returns decided records and misses proposed ones — this has produced a
   wrong state report before.
3. **Retrieve only relevant records** — `brain get <id>` for the specific
   notes that matter. Do not dump the whole Brain into context; a
   handful of targeted notes is the goal, not exhaustive recall.
4. **For a registered project, start from `Current state`** — `brain
   project show <id>` prints it with its date. The rest of the record is
   history: never report an older line from it (a "PILNE", a "broken", a
   "TODO") as current without checking `Current state` and the latest
   handoff. A negative answer ("Brain has no record of X") needs a search
   of the whole record and handoff, not of a fragment.
   Also, if the request involves a registered project, inspect the
   actual project directory on disk (see the `/project-sync`-style
   pattern) — the Brain record is a summary, not a substitute for current
   filesystem/git truth when the user is asking "what's the state of X."
5. **Distinguish current vs. historical information** — check `status`,
   `valid_from`/`valid_to`, and whether a fact has been superseded
   (`supersedes` chains). A `fact-employment-*` marked historical is not
   the user's current job; say so if it's relevant.
6. **Prefer strong/newer evidence** when sources disagree — see AGENTS.md
   "Source priority." A dated, explicit statement beats an undated
   inference; current project files beat an old Brain snapshot.
7. **Before reporting on Brain's own state** (counts of open items,
   whether work is finished, what is left to do), follow
   the shared state-audit procedure
   (https://github.com/Sako404/sako-brain/blob/main/docs/STATE_AUDIT_PROCEDURE.md): fresh
   snapshot → complete metadata listing → full record → latest handoff →
   later closures → production check if available and needed. Label each
   claim as metadata status, content state, production-verified, or
   unverified — and never treat an old "closed" mention inside a long
   record as a ruling on the record itself.
8. **Answer using the retrieved context** — cite what you found naturally
   (e.g. "per the Brain record, as of 2026-07-19..."), and say plainly
   when nothing relevant was found rather than guessing.

## If you write anything back to the Brain as a result

Follow the write policy in `AGENTS.md` / `/remember` — this skill is about
*reading* context, not persisting new facts. If the conversation surfaces
something worth remembering, that's a separate decision (see `/remember`
and the pending-memory queue).
