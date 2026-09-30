---
name: project-find
description: Locate a project — registered or not — by name, alias, or rough description. Use when the user references a project by a nickname, asks "where's the X project", or before /project-new to check for an existing registration.
---

# /project-find

1. `brain projects` — list all registered projects; check `name`/`aliases`
   for a match.
2. `brain search "<name>"` — catches project records and any other notes
   mentioning it.
3. If not found registered, run `brain project discover` to see if it exists
   as an unregistered candidate under a configured projects root.
4. If still not found, it may be genuinely new — offer `/project-new`.
5. Report the project's id, path, status, and record location (or that it's
   unregistered but exists on disk, or that it doesn't exist at all).
