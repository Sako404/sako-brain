---
name: brain-doctor
description: Run and interpret the Brain's health checks. Use when the user asks to check Brain health/integrity, after bulk edits or imports, or if search/index results seem wrong.
---

# /brain-doctor

1. Run `brain doctor`.
2. It checks: duplicate IDs, broken internal links (`[[wikilinks]]` and
   relative Markdown links), missing project directories, invalid YAML
   frontmatter, invalid `status` values, duplicate project registry entries,
   the Google Drive backup directory being mistaken for canonical, and
   credential-like patterns accidentally stored in notes.
3. **Never auto-fix or delete anything** — the tool only reports. Present
   the findings to the user grouped by check type, in plain language, and
   propose specific fixes for them to approve.
4. If it reports a possible credential leak, treat that as urgent: point out
   exactly which file/line, and recommend the user rotate that credential
   even after it's removed from the note (assume it may already be
   compromised once written to disk).
5. If it reports a missing project directory, don't delete the registry
   entry or record — the project may have moved; ask the user.
6. After fixes are applied (by the user or with their explicit approval),
   re-run `brain doctor` and `brain index` to confirm clean.
