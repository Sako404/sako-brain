# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) — with the pre-1.0 caveat that the
command line and configuration format may change between minor versions.

## 0.6.0 — 2026-09-26

### Added

Capability-specific create primitives for the three remaining skills that
had none (`/remember`'s "obvious final home" path, `/import`, `/timeline`)
— extending, not replacing, the V2 write primitives from 0.5.0. Both new
primitives take semantic intent (type, title, content, metadata); neither
accepts a client-supplied filesystem path — the destination is always
derived from fixed, validated rules, never arbitrary input.

- **`brain note create --type <person|knowledge|document|fact> --title
  "..." [--text "..."] [--area <name>] [--doc-path <path>]`** — creates a
  note at its canonical destination from the matching `80_TEMPLATES/`
  file. `person`/`knowledge`/`document` each have exactly one fixed bucket
  (`10_PEOPLE/`, `60_KNOWLEDGE/`, `70_DOCUMENTS/`); `fact` has none — real
  facts live under whichever `20_AREAS/<area>/` they belong to — so it
  requires `--area`, validated against the vault's actual existing area
  subdirectories (never lets a caller invent a new one). Any other type,
  or a `fact` with no matching area, is refused with a pointer to `brain
  remember` (unchanged inbox capture) instead of guessing.
- **`brain timeline add --title "..." --date <YYYY-MM-DD> [--what-happened
  "..."] [--why-it-matters "..."] ...`** — creates
  `50_TIMELINE/event-<date>-<slug>.md` from the event template. `--date`
  sets `valid_from` (what sorts the timeline) and is the event's own date,
  not necessarily today.
- New MCP tools `create_memory_note`, `create_timeline_event` — same
  secret-scan/`confirm_restricted` policy as the rest.
- Migrated `/remember`, `/import`, `/timeline` onto these (plus the
  existing `brain update` for edits) — all skills that write now go
  through a `brain` CLI primitive rather than a direct file edit, except
  `/project-sync`'s one still-unaddressed case (a mid-body named-section
  edit, called out in that skill's own text).

## 0.5.0 — 2026-09-26

### Added

V2 write primitives — the structured-content counterparts to `brain update`
(0.4.0's plain field-set/append), extracted from what the `/decision` and
`/project-new`/`/project-update`/`/project-close` skills already do today via
direct `Write`/`Edit`. None builds a generic arbitrary-filesystem-write API:
each is a fixed, validated operation over a known template and directory.

- **`brain decision create --title ... [--context ...] [--decision ...] ...`**
  — creates `40_DECISIONS/decision-<date>-<slug>.md` from the decision
  template. `--supersedes <old-id>` marks the old decision `superseded` and
  links forward, without ever touching its Context/Reasoning — history stays
  intact.
- **`brain project create --id ... --name ... --path ...`** — adds a
  registry entry and creates the project record in the status folder
  matching its status. Never copies project source files — `path` is a
  reference only.
- **`brain project update <id> --status <new-status>`** — the one thing
  plain `brain update` cannot do: moves the record between
  `30_PROJECTS/<STATUS>/` folders and updates the registry entry's status in
  the same call, so the record's frontmatter, its physical folder, and the
  registry can never drift apart. `--set`/`--append-text` on the same
  command still go through the existing `update_memory()` for everything
  else; `--set status=...` is refused with a pointer to `--status`.
- **`brain project close <id> [--summary ...]`** — archives a project
  (`set-status` to `archived`) and, if given, appends a closing summary via
  the existing append-text primitive. Never invents the summary text.
- New MCP tools `create_decision`, `create_project`, `update_project_status`,
  `close_project` — thin wrappers over the same functions the CLI commands
  above call, with the same secret-scan/`confirm_restricted` policy as
  `remember`/`update_memory`/`write_handoff`.
- `_registry.yaml` writes (`brain project create`/`update`/`close`) never
  re-serialize the whole file — every entry other than the one being
  changed, and every comment, is left byte-identical. Verified against a
  copy of a real 26-entry registry, not just a test fixture.

## 0.4.0 — 2026-09-26

### Added

- **`brain update <id> [--set key=value ...] [--append-text "..."]`** — a
  CLI surface over the existing `update_mod.update_memory()`, the same
  function the MCP `update_memory` tool already calls. Sets frontmatter
  fields and/or appends a dated `## Update` section to the body; `id` and
  `created` are never mutated. No new logic — same trust model as
  `brain remember` (a human typing the command is the confirmation).
- **New MCP tool `write_handoff`** — a thin wrapper over the existing
  `handoff.write()`, the same function `brain handoff write` already calls,
  including its existing refusal on an all-blank payload. First step of a
  broader "skills move off direct `Write`/`Edit`, onto CLI/MCP" migration —
  see the project's private decision log for the full audit and the
  remaining (larger, not-yet-built) primitives.

## 0.3.1 — 2026-09-26

### Fixed

- **`registry.load_registry()` now coerces `created`/`updated` to `str`.**
  An unquoted date-like YAML scalar (`created: 2026-01-15`, the common,
  human-written style) is auto-typed by PyYAML as `datetime.date`, not
  `str`, even though `ProjectEntry.created`/`updated` are documented
  `str | None`. Nothing before `brain state` ever JSON-serialized a
  `ProjectEntry`, so this was never triggered — `dataclasses.asdict()` on a
  real registry raised `TypeError: Object of type date is not JSON
  serializable`. Found running `brain state --json` against a real vault
  for the first time.

## 0.3.0 — 2026-09-26

### Added

- **`brain state` / `brain state --json`** — a deterministic, read-only
  operational-state snapshot: projects, open decisions, the pending-memory
  queue, projects with a handoff, `brain doctor` problems, recent timeline
  entries (sensitivity-filtered), systemd timer status, and a reference to
  the last saved `brain integrity` manifest. Exposed identically through a
  new `get_operational_state` MCP tool — CLI and MCP call the same function.
  No AI, no network calls, no new "stale"/"blocked" semantics: every section
  composes an existing function's own data. `schema_version: 1`.
- `indexer.count_by_type()` and `indexer.count_inbox_pending()`, extracted
  from `brain status`'s own inline logic so `brain state` (and any future
  caller) can reuse it instead of duplicating it. `brain status`'s output is
  unchanged.
- `scripts/check-release-identity.sh`, installable as a `pre-commit`/
  `pre-push` hook, refusing to commit or push when the clone's repo-local
  git identity is missing or is not the approved public address. See
  Security below.

### Security

- **The seven commits that built this release
  (`b6508f3`..`1740846`) were authored and committed with a private email
  address** instead of the approved public identity, because this clone had
  no repo-local git identity set and silently inherited the global one.
  Already pushed and merged to `main` before the mistake was found.
  **Not remediated by rewriting history** — a force-push to already-public
  `main` was judged a worse outcome than the exposure of an email address
  (not a credential): it cannot guarantee removal from any existing clone,
  fork, or cache, and this project's own standing rule is to never rewrite
  published history. Left in place, recorded here and in the private
  decision log. `scripts/check-release-identity.sh` (above) exists so this
  cannot happen a third time — the first incident, 2026-09-08, was a tag's
  tagger metadata, remediated by recreating the tag before it saw any
  meaningful distribution.

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
