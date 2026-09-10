# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) — with the pre-1.0 caveat that the
command line and configuration format may change between minor versions.

## 0.2.0 — 2026-09-10

### Changed

- **`brain init` no longer creates `AGENTS.md`.** An agent rules file is a
  client concern, and the project stays neutral toward any particular agent,
  model or tooling. A vault without one is complete and passes `brain doctor`.
  **This is a deliberate breaking change to a default**, made while the project
  is pre-1.0.
- Whoever wants the file runs the command that already existed:
  `brain agents-doc --write`. It is unchanged, and it renders from the vault's
  own configuration exactly as before.
- `initialise()` and `initialise_demo()` now default `with_agents=False`. The
  parameter itself is unchanged, so a programmatic caller can still opt in.

### Deprecated

- `brain init --no-agents` is accepted and ignored. It has nothing left to
  switch off, and is kept deliberately so scripts written against 0.1.0 keep
  working unchanged. There is no plan to remove it.

### Documentation

- The project is introduced as **SAKO Brain — Structured Augmented Knowledge
  Orchestrator**, giving the name a standalone technical meaning alongside its
  origin.

## 0.1.0 — 2026-09-08

First public release. The project was developed privately before this point;
that history is not part of the public repository, so this changelog starts
here rather than reconstructing it.

### Vault and notes

- Markdown files with YAML frontmatter are the single source of truth. Every
  derived artefact is rebuildable from them.
- A vault is identified by one file, `90_SYSTEM/config.yaml`. Vault resolution
  is explicit — `--vault`, `BRAIN_ROOT`, an upward search, then a per-user
  default — and refuses rather than guessing.
- Directory layout, note types and per-type status vocabularies are
  configurable by **role**, so an existing notes folder can be adopted without
  renaming anything. The numbered-prefix layout is the shipped default.

### Commands

- `brain init` — create a vault: directories, a commented configuration,
  `AGENTS.md`, then a health check. Never overwrites; `--force` fills in only
  what is missing. `--no-agents` and `--git` for the optional parts.
- `brain init --demo` — a small synthetic example vault to explore.
- `brain index`, `search`, `context`, `get` — SQLite FTS5 full-text search over
  the vault, with JSON output for agents.
- `brain remember` — deterministic capture into the inbox.
- `brain projects`, `project` — project records and a registry, with discovery
  of candidate project directories.
- `brain timeline`, `handoff`, `memory` — dated events, session handoffs and a
  staged pending-memory queue.
- `brain doctor`, `integrity` — health checks (duplicate ids, broken links,
  invalid statuses, secret-like patterns, runtime-state placement, SQLite FTS5
  availability) and a point-in-time report with a file manifest.
- `brain agents-doc` — generate `AGENTS.md` from the vault's own configuration.
- `brain git`, `backup` — optional local version history and optional
  encrypted restic backups.

### Privacy and safety

- Runtime state — the search index, logs and integrity manifests — lives
  **outside** the vault, under the XDG state directory. The index contains full
  note bodies, and a churning SQLite file inside a synced folder is a hazard.
- Git metadata also lives outside the vault, leaving only a pointer file.
- Backup has no default destination: an unconfigured vault gets an explicit
  error naming the setting, never a fallback.
- `brain init` and the demo generator never overwrite a file they did not
  create, and never write a credential.

### Packaging

- Installable wheel and sdist. One runtime dependency: PyYAML.
- Console entry point `brain`. Requires Python 3.11 or newer.
- Linux is supported and tested; macOS is untested; Windows is unsupported.
