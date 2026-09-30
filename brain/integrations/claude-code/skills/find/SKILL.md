---
name: find
description: Search the Brain for existing knowledge — people, projects, facts, decisions, timeline entries. Use whenever the user asks "what do we know about X", "do I have notes on Y", or before creating any new note (search-before-create).
---

# /find

This is the explicit, user-invoked version of a lookup. For automatically
searching Brain before answering an ordinary question that touches prior
context, see `brain-recall` instead — you shouldn't need the user to type
`/find` for that case.

1. Run `brain search "<query>"`. If the index looks stale or empty, run
   `brain index` first.
2. If results are ambiguous or too broad, narrow the query (try entity
   names, ids, tags) rather than dumping everything.
3. For a specific known id, use `brain get <id>` to print the full note.
4. Summarize findings for the user with file paths (`path:line` not needed
   for whole-note references — just the relative path is enough) so they can
   open the note directly.
5. If nothing is found, say so plainly rather than guessing — don't
   fabricate an answer from general knowledge when the user is asking about
   *their* Brain contents.
