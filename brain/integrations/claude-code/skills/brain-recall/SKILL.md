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
3. **Retrieve only relevant records** — `brain get <id>` for the specific
   notes that matter. Do not dump the whole Brain into context; a
   handful of targeted notes is the goal, not exhaustive recall.
4. **If the request involves a registered project**, also inspect the
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
7. **Answer using the retrieved context** — cite what you found naturally
   (e.g. "per the Brain record, as of 2026-07-19..."), and say plainly
   when nothing relevant was found rather than guessing.

## If you write anything back to the Brain as a result

Follow the write policy in `AGENTS.md` / `/remember` — this skill is about
*reading* context, not persisting new facts. If the conversation surfaces
something worth remembering, that's a separate decision (see `/remember`
and the pending-memory queue).
