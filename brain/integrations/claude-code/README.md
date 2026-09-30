# SAKO Brain — Claude Code integration

Installs the official SAKO Brain skills into `~/.claude/skills/` and
registers the Brain MCP server with Claude Code, so Claude can search,
read, and write your Brain in any session without you re-explaining how.

## Install

```
brain integration install claude-code
```

This:

- symlinks each skill under `~/.claude/skills/<name>` to the copy shipped
  inside your installed `sako-brain` package (so skill content always
  matches the Brain version you have installed — see "Version
  compatibility" below),
- registers the `sako-brain` MCP server in Claude Code's global config
  (`~/.claude.json`, `mcpServers.sako-brain`), pointing at the Brain MCP
  bridge of the currently-running installation,
- never touches any other skill or any other MCP server entry already
  configured.

Safe to re-run any time (idempotent) — re-running after an upgrade just
repoints the symlinks and the MCP entry at the newer installed version.

If a skill name collides with an existing **non-Brain** skill you already
have (same name, different target), installation for that one skill is
skipped and reported — it never overwrites something you made.

## What gets installed

14 skills covering the day-to-day Brain workflow: `find`, `brain-recall`
(automatic context retrieval before answering), `remember`, `decision`,
`timeline`, `project-find`, `project-new`, `project-update`,
`project-sync`, `project-close`, `resume`, `handoff`, `session-close`,
`import`, and `brain-doctor`.

## Verify

```
brain integration doctor claude-code
```

## Uninstall

```
brain integration uninstall claude-code
```

Removes only the symlinks this installer created (skills whose target
still points inside your `sako-brain` package) and the `sako-brain` MCP
entry. Anything else in `~/.claude/skills/` or `~/.claude.json` is left
untouched.

## Version compatibility

Skills are shipped inside the `sako-brain` package itself and installed
from whichever version you currently have installed — there is no
separate skills repository or version to track. Upgrading `sako-brain`
and re-running `brain integration install claude-code` updates the skills
to match.
