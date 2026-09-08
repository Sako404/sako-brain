# Sako Brain

A local-first personal knowledge and project-management CLI. Your notes are
plain Markdown files with YAML frontmatter, in directories you can read, edit
and back up with anything. The search index, integrity manifests and logs are a
rebuildable cache that lives **outside** your notes — so the vault stays
Markdown and configuration, and nothing else.

It works from a plain text editor with no AI, no account and no network.

**Current release: 0.1.0.** Pre-1.0 means the command line and configuration
format may still change; your notes will not — they are text.

## Why it exists

Notes accumulate faster than structure does. Most tools answer this by owning
your data: a database, a sync service, an account. That trade buys convenience
and costs control — you cannot grep it, you cannot diff it, and you cannot take
it somewhere else without an export feature someone else maintains.

Sako Brain takes the other side. The files are the system. Everything the
program adds — full-text search, link and integrity checking, project records,
decision records, a timeline, session handoffs — is derived from those files
and can be thrown away and rebuilt with one command. If the program disappears
tomorrow, you still have a directory of Markdown.

It also assumes AI agents will read and write your notes, and treats that as a
*client* concern rather than a feature: `brain init` generates an `AGENTS.md`
describing the vault's data model and rules in a provider-neutral way, which
any agent — or person — can read.

## What it does not do

Being explicit, because these are reasonable things to expect and none of them
is true:

- **No cloud service, no account, no telemetry.** Nothing leaves your machine.
- **No sync.** Put your vault in whatever you already sync, or don't.
- **No web or graphical interface.** It is a command-line tool.
- **No AI built in.** It produces context *for* agents; it does not call one.
- **Not a note-taking editor.** Use your own; the files are ordinary Markdown.
- **No automatic backup.** Backup is opt-in and unconfigured until you set it up.

## Requirements

- **Python 3.11 or newer.** Tested on 3.11 and 3.14.
- **SQLite built with FTS5**, which powers search. Present in essentially every
  mainstream build; `brain doctor` reports it if missing. It is a property of
  your Python's `sqlite3` module and cannot be installed by pip.

### Platform support

| | |
|---|---|
| **Linux** | Supported and tested. |
| **macOS** | Expected to work but not tested. Reports welcome. |
| **Windows** | Not supported. File permissions rely on POSIX mode bits. |
| Scheduled backups | Linux with systemd only. |

### Optional external tools

None is a Python dependency, and pip will never install them. Each enables one
feature; without it, that feature says so and everything else works.

| Tool | Enables |
|---|---|
| `git` | `brain git` — local version history for your vault |
| `restic` (and `rclone` for remote destinations) | `brain backup` — encrypted, versioned backups |
| `systemd` | scheduled backup and maintenance timers |

## Install

Sako Brain is **not on PyPI**. Install the wheel from the
[latest release](https://github.com/Sako404/sako-brain/releases/latest):

```sh
pipx install ./sako_brain-0.1.0-py3-none-any.whl
```

Each release lists the SHA-256 of its artefacts, so you can check what you
downloaded:

```sh
sha256sum ./sako_brain-0.1.0-py3-none-any.whl
```

Or build from source:

```sh
git clone https://github.com/Sako404/sako-brain.git
cd sako-brain
python -m build
pipx install ./dist/sako_brain-0.1.0-py3-none-any.whl
```

`pipx` puts `brain` on your PATH and keeps it isolated. A plain virtualenv
works too:

```sh
python -m venv ~/.venvs/brain
~/.venvs/brain/bin/pip install ./dist/sako_brain-0.1.0-py3-none-any.whl
```

Either way, exactly one dependency is installed: PyYAML.

### Update and uninstall

```sh
pipx install --force ./dist/sako_brain-0.1.0-py3-none-any.whl
pipx uninstall sako-brain
```

Uninstalling removes the program. It never touches a vault, its runtime state
or its backups.

## First run

```sh
brain init ~/brain
```

Creates the vault, its directories, a commented configuration file and
`AGENTS.md`, then runs `brain doctor` and prints the result.

It never overwrites anything. Run it again on an existing vault and it refuses;
with `--force` it fills in only what is missing and still leaves your
configuration, `AGENTS.md` and notes untouched.

Useful flags: `--no-agents` skips `AGENTS.md`; `--git` also initialises version
history.

### Look around first

```sh
brain init --demo /tmp/example-brain
brain --vault /tmp/example-brain index
brain --vault /tmp/example-brain search observatory
```

Creates a small synthetic vault — fictional content, a deliberately different
directory layout, a custom note type — so you can see the shape of the thing
before committing to one. Safe to delete. Do not use `--demo` for a vault you
intend to keep.

## Everyday use

```sh
brain remember --type fact --title "Postgres upgrade window is Sunday 02:00"
brain index                 # rebuild the search index
brain search postgres
brain status                # counts, inbox, projects
brain doctor                # health checks: links, duplicates, secrets, layout
brain integrity             # point-in-time report with a file manifest
```

Also: `brain projects` / `brain project` for project records, `brain timeline`
for dated events, `brain handoff` for session notes, `brain agents-doc` to
regenerate `AGENTS.md`, and `brain context --json` for feeding an agent.

## Where things live

| | |
|---|---|
| Vault | wherever you create it — Markdown and configuration only |
| Vault configuration | `<vault>/90_SYSTEM/config.yaml` |
| Runtime state (index, logs, manifests) | `$XDG_STATE_HOME/sako-brain/vaults/<vault>-<id>/` |
| Git metadata | `$XDG_DATA_HOME/sako-brain/git/<vault>-<id>.git` |
| Default vault (optional) | `$XDG_CONFIG_HOME/sako-brain/config.yaml` |

Runtime state is outside the vault deliberately: the search index contains full
note bodies, and a SQLite file rewritten on every `brain index` inside a synced
folder is the same hazard as a live `.git/`.

Which vault a command acts on, most explicit first: `--vault PATH`, the
`BRAIN_ROOT` environment variable, a `90_SYSTEM/config.yaml` found by walking
up from the current directory, then `vault_path:` in the per-user config. If
none of those answers, it refuses rather than guessing.

The directory layout, note types and status vocabularies are all configurable
by role, so an existing notes folder can be adopted without renaming anything.

## Optional features

**Git.** `brain git init` starts version history in a directory outside the
vault, leaving only a small pointer file inside it. Entirely optional — the
vault works with git absent from the machine.

**Backup.** `brain backup` wraps [restic](https://restic.net/) for encrypted,
deduplicated, versioned backups. It ships **no default destination**: set
`backup_repo:` (a filesystem path) or `backup_rclone_remote:` plus
`backup_rclone_repo_path:` in your vault's config, and keep the repository
password in a file outside the vault. Until you do, `brain backup` tells you
which key is missing. It never runs on its own.

## Roadmap

Not implemented, and not promised for any date: an MCP server for agent
integration, editor- and agent-specific rule packs, macOS verification, and
PyPI distribution. Everything described above this section exists today.

## Contributing

Contributions are welcome, though this is a small project and review may be
slow. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and expectations.

Source: <https://github.com/Sako404/sako-brain>

## Support

Best-effort. This is a personal project shared publicly: there is no service
level, no guaranteed response time and no commercial support. Bug reports and
questions go to [Issues](https://github.com/Sako404/sako-brain/issues) and are
read.

For **security issues**, do not open a public report — see
[SECURITY.md](SECURITY.md).

## Licence

Copyright (C) 2026 Marcin Sakowski.

Licensed under the **GNU Affero General Public License, version 3 or later**.
In short: you may use, study, modify and redistribute it, and if you distribute
a modified version — including running it as a network service — you must offer
the corresponding source under the same licence. See [LICENSE](LICENSE) for the
full text.

The name is not covered by that licence; see [TRADEMARK.md](TRADEMARK.md).
