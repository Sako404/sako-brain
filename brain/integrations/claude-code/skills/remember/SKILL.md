---
name: remember
description: Capture new information into the Brain — a fact, event, decision, person detail, or project update the user just told you. Use whenever the user says "remember this", shares a fact/update in passing, or gives info worth persisting across conversations.
---

# /remember

See `AGENTS.md` "Memory rules" for the policy this implements. This skill
covers *how* to write; `brain-recall` covers reading/searching before you
answer a question.

## Step 0 — which write level is this?

Before anything else, classify the candidate information. Don't skip
this — it's the difference between "Claude quietly rewrites my life story"
and a Brain the user actually trusts.

**Level 1 — safe automatic write.** Proceed directly (steps 1-11 below).
Applies only to:
- updating a project record after work *you yourself just performed*,
- recording an objective project milestone,
- recording a technical decision explicitly made *during this session*,
- correcting stale Brain data using clear primary-source project evidence
  (e.g. the project's own git log contradicts what the Brain says).

**Level 2 — user-derived durable information.** Things like "today I
bought the Honda," "I no longer work at Evri," "Anna's new job is...," "we
moved..." — plausible, durable, but not explicitly "remember this."
- If the user's message explicitly says remember/save/store/note this
  **permanently** → treat as authorized, proceed to step 1.
- Otherwise, **do not write an authoritative record**. Queue it instead:
  `brain memory add --fact "..." --entities ... --source "user, <date>
  conversation" --destination "<hint>" --sensitivity normal|private
  --confidence assumption --reason "..."`. Mention briefly that you've
  queued it for review (`brain memory pending` to see the queue) — don't
  make a big deal of it, one line is enough.
- Exception: if it's sensitive or consequential enough that writing it
  even to the *pending* queue feels presumptuous, ask a brief
  confirming question instead of queueing silently. Use judgement — don't
  interrupt for trivial cases.

**Level 3 — restricted/sensitive.** Medical, disability, detailed child
support, financial balances, identity documents, or similarly sensitive
facts. **Never write a permanent record merely because it appeared in
conversation** — not even to the pending queue — unless the user's intent
to persist it is explicit and direct ("remember my new diagnosis," not
just mentioning it in passing). When you do write it, `sensitivity:
restricted`, and keep it in a separate restricted note rather than mixed
into an ordinary profile (see existing `10_PEOPLE/*/restricted-*.md`
files for the pattern).

**Poor candidates — don't queue or write these at all:** casual
conversation, one-off commands, temporary debugging output, speculative
ideas that weren't adopted, generic technical explanations, information
already captured, routine git commits, minor code edits.

## Steps 1-11 (Level 1, or Level 2/3 with explicit permission)

1. **Interpret the information.** What is actually being asserted? Separate
   fact from assumption/opinion (`confidence` field).
2. **Identify relevant entities** — people, projects, areas mentioned.
3. **Search the Brain first**: `brain search "<keywords>"` for each
   entity/topic. Never skip this step — this is also how you avoid
   queueing/writing a near-duplicate of something already there.
4. **Check for duplicates.** If an existing note already covers this, prefer
   updating it over creating a new one.
5. **Decide create vs. update:**
   - Update: `brain update <id> --set key=value --append-text "..."`. Bumps
     `updated` automatically. If the new info *changes* a prior fact rather
     than adding to it, don't delete the old value — mark it superseded
     (`--set status=superseded --set valid_to=<date>`) and create the new
     fact as a separate note (see Create below), per rule 5/6 in AGENTS.md
     ("preserve historical information").
   - Create: for `person`/`knowledge`/`document`, or `fact` with a known
     `20_AREAS/<area>` home: `brain note create --type <type> --title "..."
     --text "..." [--area <name>] [--doc-path <path>]` — picks the
     template and final directory for you (never write these directly).
     For `decision`/`project` content, use `/decision`/`/project-new`
     instead of this skill. For anything else — an `area` type itself, or a
     `fact` with no established area yet — `brain remember --type ... --title
     ... --text ...` raw-captures into `00_INBOX/` instead; inbox is for
     genuinely unsorted capture only, never a default.
6. **Preserve provenance** — fill `source` (e.g. "user, 2026-07-27 conversation")
   and `source_date` (when the underlying fact/event happened or was
   stated, which may differ from today).
7. **Update timestamps** on anything touched.
8. **Add a timeline entry** (`brain timeline add --title "..." --date
   <date>`) if this represents a meaningful event (not every small fact
   needs one — use judgement).
9. **Never store credentials** — passwords, API keys, tokens, seed phrases.
   If the user tries to hand you one, refuse to write it down and say why.
10. **Reindex**: run `brain index` after writing.
11. **Report exactly what changed** — compact form, e.g.:
    ```
    Brain updated:
    - person-anna-sakowska: updated (new job)
    - event-2026-08-...: added
    ```
    The user should never have to wonder whether something was silently
    changed.

If it's ambiguous whether this is the same entity as an existing note, ask
the user rather than guessing (AGENTS.md rule 15).
