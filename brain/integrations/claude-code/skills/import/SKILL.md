---
name: import
description: Import external content (a pasted document, email, transcript, article) into the Brain as one or more structured notes. Use when the user pastes or points to external material and wants its content captured, not just summarized in chat.
---

# /import

1. Read the source material fully before deciding structure.
2. **Scan for credential-like content first** — if the material contains
   passwords, API keys, tokens, or seed phrases, exclude those specific
   values entirely; note that they were redacted rather than silently
   dropping the fact that the source had credentials.
3. Break the content into the entities it actually represents (a person, a
   fact, a decision, knowledge, a document reference) rather than dumping it
   as one giant note — one entity type per note.
4. Search first for each entity to avoid duplicating something already in
   the Brain; update existing notes where appropriate (`brain update <id>
   --set key=value --append-text "..."`) instead of creating near-duplicates.
5. Set `source` to describe where this came from (e.g. "imported: email from
   X, 2026-07-27") and `confidence` appropriately (usually `assumption`,
   not `fact` — imported material is often someone else's claim, not
   necessarily verified) — both are `brain note create` flags.
6. Create each new entity with `brain note create --type <person|
   knowledge|document|fact> --title "..." --text "..." --source "imported:
   ..." --confidence assumption [--area <name>] [--doc-path <path>]` —
   never write the file directly; the command picks the template and final
   directory. `decision`/`project` content goes through `/decision`/
   `/project-new` instead. A `fact` with no established `20_AREAS/<area>`
   home yet, or any type this command refuses, falls back to `brain
   remember --type ... --title ...` (raw `00_INBOX/` capture).
7. If the source is itself a document worth referencing (not just mining
   facts from it), also create a `--type document` record via the same
   `brain note create` — pass `--doc-path` if it lives on disk outside the
   Brain, or leave it blank and describe it in `--text` if it doesn't.
8. Run `brain index`. Report what was created/updated and what (if
   anything) was redacted for containing credentials.
