---
name: session-close
description: Close out a work session — save all durable project/session state to canonical SAKO Brain, write the Brain handoff, check every repo touched this session, and report status. Use on any clear closure signal — "session close", "wrap up for today", "close the session", or the explicit /session-close command.
---

# /session-close

**Canonical Brain (via the `brain` CLI or an equivalent Brain MCP
capability, however this client has it configured) is the only
authoritative source this workflow reads from or writes to — for both
directions.** There is no local vault fallback and no direct filesystem
access to canonical data — never a source for current state (step 1's
"what changed" and step 2's Brain reads included) and never a write
target, not even as a fallback. Editing a skill definition on disk (this
skill's own file included) is normal tooling maintenance, not a Brain read
or write — it is not an exception to the no-fallback rule, just outside its
scope. Local Claude memory (`~/.claude/projects/*/memory/`) is never a
substitute for Brain either — see step 8. Do not invent a third place to
put state.

## 0. Confirm Brain is reachable before touching anything

Run `brain status` (or `brain doctor`). If it fails — SSH/network down,
server unreachable — **STOP here**. Report that session closure is
incomplete and why. Do not fall back to reading or writing any local vault
copy, and do not silently substitute local Claude memory for the Brain
writes this workflow would otherwise make. Wait for Brain to come back, or
for the user to say how to proceed.

## 1. Inspect what actually changed this session

Review the conversation and, for every project/repo touched: `git log`
against the session's start point, `git status`, and anything written to
Brain already mid-session (search/get to confirm). Build a concrete list —
files changed, commands run against production/infrastructure, decisions
made, problems found, things explicitly deferred. Don't guess; if
something is unclear, say so rather than inventing a tidy narrative.

## 2. Update canonical Brain: project state, decisions, next actions

For each project touched, using the existing skills/capabilities — never a
direct file edit, never a new `brain` capability:

- **New or changed decision** → `/decision` (`brain decision create`, or
  `--supersedes` for one that reverses an earlier call).
- **Project status/current-state/next-actions changed** → `/project-update`
  (`brain project update --status ...` for a status move, `--set` /
  `--append-text` for frontmatter or a dated note, `brain project
  section-update` for a specific section like "Next actions").
- **A cross-project backlog item is now stale** (shipped, superseded,
  no longer accurate) → append a dated correction via the same
  `--append-text` mechanism; per this vault's append-only discipline,
  correct by annotation, never rewrite an old row to look like it was
  always accurate.
- If the only true statement is "nothing decision-worthy happened, just
  incremental progress," skip this step — don't manufacture a decision
  record for a session that didn't produce one.

If a needed mutation has no existing `brain` CLI/skill primitive for it:
**STOP and say so** — ask the user for a tooling decision. Do not hand-edit
a vault file to work around a missing capability.

## 3. Write/update the Brain handoff

Use `/handoff` for every project with **meaningful** work this session
(real progress, a decision, a blocker, a milestone — see `/handoff`'s own
"When to use"). Skip it for a trivial session. If a project has no handoff
yet, `brain handoff write --project <id>` creates the first one.

## 4. Check every repo touched this session

For each one: `git status --short` and recent `git log`. Note anything
uncommitted and why (in progress, deliberately held back, needs review).

## 5. Commit only complete, in-scope work

If something is finished, tested, and within the scope of what was asked —
and not already committed — commit it (see this environment's own git
safety rules: no `--no-verify`, no force-push, no amending published
commits, stage specific files rather than `-A` when anything sensitive is
nearby). Never commit unrelated or accidental changes just to reach a
clean tree.

## 6. Report intentionally uncommitted work

If anything is left uncommitted on purpose, say exactly what and why — do
not silently leave it and call the tree clean.

## 7. Verify canonical Brain writes actually landed

For every write in steps 2–3, read it back — `brain get <id>`, `brain
handoff show <id>`, `brain project show <id>` — and confirm the content
matches intent. Run `brain index` after the batch. A write you haven't
confirmed isn't done yet; don't report it as saved until you've re-read
it.

## 8. Only now, optionally: local Claude memory

Local memory (`~/.claude/projects/*/memory/`) may hold **only**: stable
user preferences, interaction/workflow preferences, and a short pointer to
an authoritative Brain record (id + one line of what it's about). It must
never hold an authoritative copy of project status, decisions, handoffs,
next actions, infrastructure state, security procedures, deployment
status, or any operational history that Brain step 2/3 already covers —
that's a second, silently-diverging copy of exactly the thing this
workflow exists to prevent. If in doubt whether something belongs in
local memory or Brain: it belongs in Brain, and at most a pointer goes
local.

## 9 & 10. No fallbacks, ever

Never read OR write Brain-covered data via any local vault copy because
Brain was briefly unreachable or a capability was missing (see step 0/2) —
not even "just this once, to check current status while the connection is
down." Never substitute local Claude memory for it either. Both are silent,
hard-to-notice ways to diverge from canonical state — the correct response
to any of these blockers is always: stop and report, not route around it.

## Closing report

End with a short status block:

```
BRAIN SAVED       — what was written/updated, confirmed read back
GIT STATUS        — per repo: clean, or exactly what's uncommitted and why
HANDOFF           — which project(s), new or appended
OPEN ITEMS        — genuinely open/deferred, not busywork
SESSION CLOSED    — or "SESSION CLOSE INCOMPLETE: <reason>" per step 0/2
```
