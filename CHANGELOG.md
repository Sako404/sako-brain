# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) — with the pre-1.0 caveat that the
command line and configuration format may change between minor versions.

## 0.13.2 — 2026-10-01 — admin capability to remove a principal record

### Added

- `brain principal delete <id>` — permanently removes a principal record,
  for cleaning up a throwaway or mistaken one (e.g. a disposable test
  principal created during live verification). Unlike `set-status`, which
  every other lifecycle operation prefers, this is a genuine delete.
  Refuses to run if the principal is still a member of any group, so
  cleanup can never silently orphan a dangling audience reference.

938 tests passing (was 934).

## 0.13.1 — 2026-10-01 — fix gateway crash on an existing (pre-Stage-1) database

### Fixed

- `brain remote-gateway serve` (and anything else opening `Storage`)
  crashed outright against any database that predated v0.13.0 — i.e.
  every real deployed gateway. `CREATE INDEX idx_tokens_principal ON
  tokens(principal_id)` ran as part of the main schema script, before
  the migration step that adds that column to a pre-existing `tokens`
  table had run; `CREATE TABLE IF NOT EXISTS` is a no-op against a table
  that already exists, so the column genuinely wasn't there yet at that
  point for any real database, only for one built from nothing. v0.13.0's
  own tests didn't catch this because they only ever opened a `Storage`
  against a database that didn't have `tokens` pre-existing at all.
  Found live, redeploying against the real `mcp.sako.systems` gateway's
  actual database — reproduced locally against a database shaped
  identically to the real one (pre-existing tables, real client/token
  rows) before fixing the ordering. Fixed by creating that index after
  the column-migration step, not as part of the initial schema script.
  New regression coverage (`TestOpeningARealPreStage1DatabaseDoesNotCrash`)
  builds a database with every table already populated, the way every
  real deployment actually looks, specifically so a fresh-table
  assumption like this one can't hide again.

934 tests passing (was 929).

## 0.13.0 — 2026-10-01 — Identity + Authorization Foundation (Stage 1, multi-user)

First step of SAKO Brain multi-user support — identity/credential
plumbing only. **No family access is activated by this release**; every
existing identity becomes `principal-marcin` automatically, and nothing
in Brain's own content gains or loses visibility (that's Stage 2).

### Added

- **`brain principal`** / **`brain group`** — new CLI commands
  (`create`/`list`/`set-status`/`set-role`, and groups'
  `create`/`list`/`add-member`/`remove-member`) managing principal and
  group records. These are deliberately **not reachable via the SSH
  forced-command dispatcher under any mode** — no remote identity,
  including the ones Claude Code/Codex/the remote gateway use, can
  invoke them. Principal/group records live under `90_SYSTEM/identity/`,
  structurally outside every content directory `search`/`get`/`context`
  ever walk, so they can never surface through ordinary read paths —
  not a permission check, a fact about where the files are.
- **`brain principal break-glass-restore-admin`** — the one documented
  recovery path for a bad status/role change locking an admin out:
  unconditionally forces one principal back to `status=active` with a
  given role. Local-only by the same construction as every other
  principal command (never dispatcher-reachable); requires
  `--confirm-break-glass`; every call audited with a distinct,
  impossible-to-miss event name.
- **Remote MCP gateway, principal-aware**: OAuth tokens and
  authorization codes now bind `principal_id` alongside `client_id` —
  login is "sign in as a principal" (a principal field alongside the
  password), not a single shared owner password. `brain remote-gateway
  set-owner-password` gained `--principal` (defaults to
  `principal-marcin`, so nothing already configured needs resetting).
  New `brain remote-gateway revoke-principal` kills every token for one
  person across every client they've used, complementing the existing
  `revoke-client` (kill one compromised client without logging everyone
  else out) and `revoke-all` (kill switch). A gateway database from
  before this release migrates its single owner password to
  `principal-marcin`'s credential automatically, in place, the first
  time it's opened.
- **SSH dispatcher, principal-aware**: every forced command now also
  carries `--principal`/`--client` (fixed in `authorized_keys` at
  install time, exactly like the existing `--mode` — never anything the
  connecting client can send). Before executing anything, the dispatcher
  checks the named principal is `status: active` by reading the
  canonical vault's own principal record directly, in-process, every
  single connection — never cached — so disabling a principal takes
  effect on its very next connection regardless of whether its SSH key
  still exists or an OAuth token hasn't expired yet. `--principal`
  defaults to `principal-marcin`, so every pre-existing `authorized_keys`
  line keeps working unchanged.
- **Brain-core audit log** (`brain/audit.py`) — append-only,
  redaction-safe (principal/client/transport/event only, never a record
  body or a secret), logging principal/group lifecycle events. The
  gateway gained its own separate `audit_log` table for
  login/consent/token/revoke events — two logs because only each
  process can honestly write the one it's closest to, not two
  re-implementations of the same thing.

### Fixed

- `brain-mcp.marcinsakowski.com`, the pre-`mcp.sako.systems` rollback
  endpoint, decommissioned (Stage 0B) after re-confirming
  `mcp.sako.systems` was healthy and the ChatGPT/Claude.ai integration
  still valid. Only its TrueNAS app, Cloudflare Tunnel ingress rule, and
  DNS record were removed — its ZFS datasets (including the old OAuth
  database) were left on disk, not deleted. `marcinsakowski.com`'s other
  seven hostnames are unaffected, verified live.

## 0.12.3 — 2026-10-01 — write-to-index consistency (Stage 0A, multi-user groundwork)

### Fixed

- Every write primitive (`remember`, `update`, `project create/update/
  close/section-update`, `decision create` — including its superseded-
  record side-write, `timeline add`, `note create`, `memory accept`,
  `handoff write`) left the SQLite FTS5 search index untouched after
  writing, so a note existed on disk immediately but `search`/`context`/
  MCP `search_memory` wouldn't see it until a later, separate `brain
  index` ran. Found live during a staleness-test acceptance pass — a
  client read a record moments after writing it and got "not found".
  Fixed by having every write primitive incrementally index exactly the
  note(s) it touched (`indexer.index_note()`, factored out of
  `rebuild()`'s per-note logic so there is one implementation, not two)
  immediately after writing — never a full reindex per write.
- `brain get <id>` depended on the SQLite index to resolve id -> path
  (and refused with "Try `brain index` first" when it lagged), while
  `read_memory`'s in-process equivalent already resolved ids by walking
  Markdown directly. Unified: `brain get` now uses the same
  index-independent resolution, so direct reads by id never depend on
  indexing timing at all, structurally rather than by being fast enough.

The index remains exactly what it always was — a rebuildable cache,
never the authoritative copy of anything; this only shrinks the window
during which it can lag the Markdown that actually matters, and removes
one read path's dependency on it entirely.

## 0.12.2 — 2026-10-01 — owner-password CLI mistargeting + RFC 9728 path-aware metadata

### Fixed

- `brain remote-gateway set-owner-password` (and `list-clients`,
  `revoke-client`, `revoke-all`) called `Storage()` with no explicit
  path, which resolves the database location from whatever
  `XDG_STATE_HOME` happens to be in the *current* process's environment
  — not from the gateway's own configured `canonical_uri`/`db_path`.
  Run from an operator's desktop instead of the gateway host itself,
  this silently wrote (or read) a decoy local database while printing
  "Owner password set." — a false success against the wrong state while
  the real production gateway kept serving its old owner credential.
  Found via live production acceptance: a password set this way was
  rejected by the real `/authorize` login page. Fixed by routing every
  state-mutating command through `config_mod.load_config()`, exactly
  like `cmd_serve`/`create_app` already do, so these commands now print
  which gateway/database they are targeting and fail loudly
  (`GatewayConfigError`) instead of silently succeeding elsewhere.
- Added `GET /.well-known/oauth-protected-resource/mcp` alongside the
  existing bare `/.well-known/oauth-protected-resource` route. RFC 9728
  section 3.1 constructs the metadata URL by inserting the well-known
  path *before* the resource's own path component — for a resource at
  `/mcp` that is the suffixed form, not only the bare root. Observed
  live: a real ChatGPT connector attempt requested exactly this
  suffixed path and got a 404. Both routes now serve the identical
  document.

### Diagnosed, not code bugs

- ChatGPT's connector handshake (discovery + Dynamic Client
  Registration) was intermittently rejected by Cloudflare's zone-wide
  **AI Bots** blocking and **Bot Fight Mode** (both confirmed live via
  Cloudflare's own request analytics: `POST /register` and `POST /mcp`
  from OpenAI's infrastructure returning 403 before reaching the
  gateway at all, interleaved with other requests on the same path that
  succeeded). Both products are zone-wide on marcinsakowski.com's Free
  plan and, per Cloudflare's own documentation, cannot be scoped to a
  single hostname via a WAF custom rule on this plan — only disabling
  them zone-wide, or upgrading to a plan with Super Bot Fight Mode
  (which does support a narrow per-host *Skip* rule), resolves it. This
  is a deliberate security-posture / cost decision, not something this
  release changes unilaterally.

## 0.12.1 — 2026-09-30 — doctor HTTP client fix (Cloudflare Bot Fight Mode)

### Fixed

- `brain integration doctor remote` was rejected outright (Cloudflare
  error 1010, Bot Fight Mode) against the real production gateway
  deployed behind Cloudflare. Root cause, found by isolating each
  variable live: it was not the *absence* of a `User-Agent` — Python's
  own `urllib` default UA and plain `curl`'s default UA were both fine —
  it was setting a *distinctive, unrecognized custom* UA string
  (`sako-brain-doctor/<version>`) that got the request flagged as an
  unknown bot. Fixed by preferring to shell out to `curl` (whose own
  default UA already passes cleanly) and never adding a synthetic
  User-Agent override on either the curl or urllib fallback path.
  Verified live against the real deployed gateway after the fix: all
  checks pass through the production Cloudflare Tunnel.

## 0.12.0 — 2026-09-30 — REMOTE / WEB AI ACCESS

A single, vendor-neutral remote MCP gateway so ChatGPT web and Claude.ai
web can reach canonical Brain over HTTPS, live — without either ever
touching the vault filesystem, SSH, or bypassing Brain's own write
safety. Full write-up: canonical Brain decision
`decision-2026-09-30-sako-brain-v0-12-0-remote-web-ai-access`.

### Added

- **`brain remote-gateway`** — an optional, self-hosted component
  (`pip install sako-brain[remote-gateway]`) implementing:
  - A standards-compliant **OAuth 2.1 authorization server**: PKCE
    (S256-only), Dynamic Client Registration (RFC 7591), Protected
    Resource Metadata (RFC 9728), Authorization Server Metadata
    (RFC 8414), Resource Indicators (RFC 8707) for audience binding,
    refresh-token rotation, and revocation (RFC 7009-shaped). Every
    cryptographic primitive is stdlib (`secrets`, `hashlib`) — no
    hand-rolled cipher or signature scheme; see
    `remote_gateway/oauth.py`'s module docstring for the full reasoning.
  - A **Streamable HTTP MCP transport** (`POST /mcp`) that defers every
    tool call to the *existing* `brain.mcp_bridge.handle_request` —
    the same dispatch Claude Code and Codex already use — so this is a
    new transport around the existing capability surface, never a
    second implementation of Brain's tools. Critically, this reaches
    canonical Brain through the same SSH forced-command dispatcher every
    other client uses (via this process's own `brain setup`-configured
    identity) — the gateway never gets direct vault filesystem access.
  - Three OAuth scopes — `brain.read`, `brain.write`,
    `brain.restricted` — mapped to the 19 existing MCP tools by policy
    (`remote_gateway/scopes.py`), enforced as an *outer* gate in front
    of Brain's own unchanged writepolicy (secret scanning, restricted
    confirmation), never a replacement for it. `tools/list` is filtered
    to what the presented token can actually call.
  - A minimal owner-login + consent screen (`GET/POST /authorize`) —
    password hashed with `hashlib.scrypt`, a session cookie HMAC-signed
    with a server secret generated once into the gateway's own SQLite.
  - Vendor-neutral MCP server instructions (`initialize`'s
    `instructions` field) steering any compliant client — ChatGPT,
    Claude.ai, or a future one — toward querying Brain before answering
    when relevant, without per-vendor special-casing.
  - `brain remote-gateway init / serve / set-owner-password /
    list-clients / revoke-client / revoke-all` — the full operator
    surface, including a one-command kill-switch that cuts off every
    remote (ChatGPT/Claude.ai/etc.) token without touching Claude Code,
    Codex, or any other client using `brain setup`'s own SSH proxy.
- **`brain integration doctor remote --base-url <url>`** — checks a
  deployed gateway from a genuine external HTTP vantage point (HTTPS,
  reachability, protected-resource/authorization-server metadata,
  scopes advertised, PKCE method, fail-closed on missing/invalid
  tokens) using only stdlib `urllib` — no extra dependency needed just
  to check a remote deployment.
- New test suite (`tests/test_remote_gateway.py`, 35 tests): HTTP MCP
  transport, OAuth discovery, invalid/expired/wrong-audience tokens,
  missing/read/write/restricted scope enforcement, PKCE verification,
  one-time-use authorization codes, refresh-token rotation, revocation
  (single client and kill-switch), tool annotations, server
  instructions, an explicit regression guard proving the gateway never
  imports the direct-vault `mcp_server` module, and that a secret-shaped
  write still reaches the shared writepolicy layer unmodified.
- New packaging regression tests confirming `brain.remote_gateway`
  travels in the wheel, and that the core CLI still imports cleanly
  with zero hard dependency on the optional Flask/waitress extra.

### Notes

- Out of scope, deliberately untouched: Hermes, People, Tasks, Planner,
  Calendar, family multi-user, Matrix/TRON redesign, E2EE, mobile app, a
  general plugin/marketplace framework.
- 873 tests passing (was 836 at v0.11.1).

## 0.11.1 — 2026-09-30 — client write-identity fix for `brain index`

### Fixed

- `brain setup`'s client-side identity selection classified `index` as a
  read operation and sent it under the read identity, but the server
  dispatcher's `READ_ALLOWED` does not include `index` (only
  `WRITE_ALLOWED` does) — so every `brain index` call against a remote
  canonical Brain was refused, regardless of which identity was actually
  configured. Every shipped skill recommends running `brain index` after
  a write, so this affected the core day-to-day workflow. Found live
  during this session's own `/session-close` acceptance check, right
  after v0.11.0 shipped.

## 0.11.0 — 2026-09-30 — PUBLIC AGENT INTEGRATIONS & CLIENT SETUP

The public product can now be installed, configured, and wired into
Claude Code and Codex end-to-end by a new user with no access to any
private file — the last remaining pieces of day-to-day tooling that only
existed as hand-maintained files outside any repository. Full write-up:
canonical Brain decision `decision-2026-09-30-sako-brain-v0-11-0-public-agent-integrations-client-setup`.

### Added

- **`brain setup`** — configures this client to reach a remote canonical
  Brain over SSH: generates a brain-owned, strict SSH config (`ssh -F`,
  dedicated known_hosts, no host-key relaxation, no forwarding) plus
  `~/.config/sako-brain/client.toml`, and transparently proxies every
  command (other than `setup`/`integration`/`init`) over it — no external
  wrapper script required. `--show-host-key` helps verify a new server's
  host key out-of-band before trusting it.
- **`brain integration install claude-code` / `... codex`** — the
  official agent-integration installer. Symlinks 15 generic Claude Code
  skills into `~/.claude/skills/` and registers the MCP bridge in
  `~/.claude.json`; registers the MCP bridge in `~/.codex/config.toml`
  and merges a generic Brain-usage section into `~/.codex/AGENTS.md`
  between marker comments. Idempotent, merge-safe — never destroys a
  skill, MCP entry, or config section it did not itself create.
- **`brain integration doctor [claude-code|codex] [--json]`** — checks
  package version, client config, SSH config, canonical Brain
  connectivity, MCP bridge startup and advertised capabilities, skills
  installed, and both agents' registration. Prints no secret values.
- **`brain integration uninstall claude-code` / `... codex`** — removes
  only what the installer added.
- 15 generic Claude Code skills now ship inside the package itself
  (`brain/integrations/claude-code/skills/`), generalized from a
  previously private, per-deployment set: no assumption of a specific
  username, host, IP, SSH key name, or infrastructure provider survived
  the generalization — see the new skills↔CLI contract test below for
  the regression guard.
- A generic Codex `AGENTS.md` integration section and MCP registration
  snippet (`brain/integrations/codex/`).
- Skills, templates, and integration docs are packaged into both the
  wheel and the sdist and covered by a new packaging regression test
  (`tests/test_oss4_installed.py::TestInstalledResources::
  test_the_wheel_contains_every_shipped_skill_and_integration_doc`) —
  confirmed against a real built wheel, not just pyproject.toml
  declarations.
- **Skills↔CLI contract test** (`tests/test_skills_cli_contract.py`):
  every `brain <command>` and `--flag` a shipped skill references is
  checked against `cli.build_parser()` directly — authoritative, not a
  hand-kept list or a live subprocess call — plus a generic-content guard
  (no private host/IP/username pattern, no `BRAIN_LOCAL` fallback
  instruction, no direct-vault-edit instruction).
- **Fresh-machine acceptance**: a full clean-HOME, clean-venv acceptance
  suite (`tests/test_oss4_installed.py::
  TestInstalledClientSetupAndIntegrations`) proving `brain setup` and
  both `brain integration install` commands work with zero dependency on
  any specific user's home directory, `sako-brain-tooling`, or any
  pre-existing agent config.

### Changed

- 834 tests passing (was 786 at v0.10.1).

### Notes

- Skills are versioned with the Brain release that ships them — there is
  no separate skills repository or compatibility matrix.
- Out of scope, deliberately untouched: `queue_memory`, People, Tasks,
  Planner, Calendar, Hermes integration, TRON/Matrix redesign, E2EE,
  Remote/Web MCP, a general plugin framework.

## 0.10.1 — 2026-09-30 — CORE HARDENING / FINAL CLIENT ACCEPTANCE

Closes the two real gaps v0.10.0's own acceptance testing found: Codex
could not reach canonical Brain at all under its sandbox, and the CLI/
bridge write path had no secret-scan or restricted-confirmation
protection even though the in-process MCP server did. Full write-up:
canonical Brain decision `decision-2026-09-30-sako-brain-v0-10-1-core-hardening-final-client-acceptance`.

### Fixed

- **Codex acceptance.** Root cause was two independent, stacked issues,
  both confirmed by direct measurement, neither a Brain/bridge defect:
  (1) Codex's sandbox presents `/etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf`
  (pulled in by `/etc/ssh/ssh_config`'s own `Include`) as owned by
  `nobody:nobody`, mode `777` — a sandbox filesystem-virtualization
  artifact (the real file is `root:root`, `0644`) — which OpenSSH
  correctly refuses to trust. Fixed with a minimal, brain-owned
  `~/.config/sako-brain/ssh_config` (`ssh -F`, which replaces rather than
  supplements the system config per `ssh(1)`) pinned to the 3
  already-trusted host keys for the brain SSH proxy port — no blind
  trust-on-first-use, no system permission changes. (2) Codex's sandbox
  additionally blocks outbound sockets by default in every tier except
  with `sandbox_workspace_write.network_access=true` explicitly set —
  unrelated to (1), and **not changed globally**: the `sako-brain` MCP
  server process itself is not subject to this per-shell-command sandbox
  at all (proven: it worked with zero configuration changes, in every
  sandbox tier, throughout this investigation), so the actual, intended
  integration path was never blocked. `~/.codex/AGENTS.md` updated to
  prefer the MCP tool path explicitly.
- **Shared write security.** `mcp_server.py` had secret-pattern scanning
  and `sensitivity: restricted` confirmation; the CLI (and therefore
  `mcp_bridge.py`, which shells out to the CLI for every write tool) had
  neither — found in the v0.10.0 audit, closed here. New `writepolicy.py`
  is the one shared implementation both transports call:
  - Secret-pattern scanning is now unconditional on every free-text CLI
    write command (`remember`, `update`, `project create/update/close/
    section-update`, `decision create`, `timeline add`, `note create`,
    `handoff write`) — exactly the same `SECRET_PATTERNS` `brain doctor`
    already used.
  - A new `--confirm-restricted` CLI flag, required whenever
    `--sensitivity restricted` (or `--set sensitivity=restricted`) is
    used — on `remember`, `note create`, `decision create`, `timeline
    add`, `update`, `project update`. This is a real behavior change:
    "a human typed this command" is no longer treated as implicit
    confirmation, because the same CLI command is also how the bridge
    writes on an automated caller's behalf. `mcp_bridge.py` forwards the
    MCP tool call's own `confirm_restricted` argument to this flag —
    pure relay, the bridge never sets it on a caller's behalf and has no
    other way to reach Brain than this same CLI command, so it cannot
    bypass the policy by omission or otherwise.
  - Provenance: reviewed against the existing data model rather than
    adding new schema — a restricted note's own `sensitivity: restricted`
    frontmatter field, combined with the fact that writing it now
    unconditionally requires confirmation, already is the provenance
    record. No new field added.
- **Least privilege: `project show`.** `project show`/`discover`/`sync`
  are genuinely read-only (print a registry entry / propose unregistered
  candidates / gather+print live filesystem facts — none mutate
  anything) but were only reachable via the WRITE SSH identity, because
  the server dispatcher's allowlist bundles all of `project` together.
  `brain-dispatch.py` (now version-controlled in `sako-brain-tooling`,
  previously server-only with no history) gained `READ_ONLY_SUBCOMMANDS`
  — the mirror image of the existing `WRITE_ONLY_SUBCOMMANDS` mechanism
  — granting exactly these three `(top, sub)` pairs to the READ identity
  without touching `WRITE_ALLOWED` or opening any actual write
  subcommand. Negative tests confirm every real write subcommand stays
  refused under the read identity.
- **`project_context` alias bug**, found live during this pass's own
  Codex acceptance testing: the bridge's composed implementation used the
  caller's raw id for both of its two `brain` calls, but only the first
  (`project show`) resolves aliases — the second (`get`) does an exact
  note-id match, so calling `project_context` by alias 404'd on the
  record half even though the project itself was found. Fixed to reuse
  the first call's own resolved canonical id for the second.

### Notes

- `queue_memory` untouched, as instructed — not part of this pass.
- 785 tests passing (up from 734 at v0.10.0), covering: the new
  `writepolicy.py` module directly; CLI secret-scan and
  restricted-confirmation parity (positive + negative, per command); the
  bridge's `confirm_restricted`-forwarding per translator; real
  (non-mocked) end-to-end bridge integration against an actual `brain`
  CLI subprocess; and the previously-untested in-process MCP
  `create_timeline_event` restricted-confirmation path. Plus 25 dispatcher
  routing tests in `sako-brain-tooling` (a separate repo, not counted in
  this total).

## 0.10.0 — 2026-09-30 — CORE COMPLETE

The v0.10.0 push: Claude Code and Codex can now do the full day-to-day
Brain workflow (search/read, context/state, list/get project, project
context, timeline read/search, remember/create/update notes, decisions,
projects, handoffs, timeline events) entirely through the canonical
`brain` CLI/MCP bridge — no filesystem workarounds, no "not yet
supported". A full capability audit, ahead of implementation, is recorded
in canonical Brain's `decision-2026-09-30-sako-brain-v0-10-0-core-complete-capability-audit`.

### Added

- Stable `--json` output on 8 CLI commands that previously only printed
  human-readable text: `project show`, `project create`, `project close`,
  `project section-update`, `decision create`, `timeline` (bare list, now
  also takes `--query` for a substring filter) and `timeline add`, plus
  the generic `update`. Each shape matches the equivalent MCP tool's
  existing JSON contract exactly (verified by test and live).
- `mcp_bridge.py` translators for 9 more tools using those new `--json`
  contracts: `get_project`, `search_timeline`, `update_memory`,
  `create_decision`, `create_project`, `close_project`,
  `update_project_section`, `create_timeline_event`, and `project_context`
  (a deliberate exception — composes `project show` + `get`, two calls,
  rather than a 1:1 CLI mapping, and always returns `filesystem_facts:
  null` over the bridge: the canonical server cannot see a desktop
  project's git state, and returning stale/wrong facts would be worse
  than being honest that this bridge can't answer that part). Bridge
  coverage is now 17 of Brain's real MCP tools (up from 9 in 0.9.2).
- `registry.find_project()` — the one canonical project-identifier
  resolver (id or any declared alias), now used everywhere a project
  identifier is accepted: `project show`/`sync`/`update`/`close`/
  `section-update`, `handoff write`/`show`/`list`, and the equivalent MCP
  tools. Previously only some commands checked aliases and others
  silently required the exact registry id — the same name now works
  everywhere, or fails everywhere with the same clear message.
- `brain doctor`'s `missing_project_dirs` check now distinguishes a
  genuinely missing project directory from one this process simply cannot
  see from here: a new `Config.remote_project_paths` flag (set via
  `BRAIN_REMOTE_PROJECT_PATHS=1`, the same deployment-signal pattern as
  `BRAIN_ROOT`/`BRAIN_STATE_DIR`) makes the check report
  `project_path_unverifiable` instead of the misleading `missing_project_dirs`
  when running on a host that was never going to see desktop-local project
  paths in the first place (the canonical server, via the restricted SSH
  dispatcher). Neither check is blocking; this only fixes the label.
- `brain capabilities`'s CLI command enumeration now lists a command with
  an *optional* sub-subcommand (e.g. bare `timeline`, distinct from
  `timeline add`) as its own entry — previously invisible from
  self-description entirely, a real gap for any client trying to
  discover what's callable.

### Removed

- `get_project_path` MCP tool (and `BrainGatewayClient.get_project_path()`).
  A pure subset of `get_project`'s own `registry.path` field, with no real
  caller anywhere in this codebase — deprecated rather than bridged, per
  the audit's own "don't build parity for parity's sake" instruction.

### Fixed

- `mcp_bridge.py`'s own module docstring and this README's "MCP servers"
  section, both still describing 0.9.2's partial (9-tool) coverage.

### Notes

- `queue_memory` remains unsupported over the bridge, deliberately: the
  server dispatcher's security boundary doesn't allow `brain memory` at
  all (confirmed by reading the dispatcher directly), and extending that
  boundary "for parity" was explicitly out of this pass's scope.
- The CLI layer (unlike `mcp_server.py`'s in-process tool wrappers) does
  no secret-pattern scanning and no `sensitivity: restricted` confirmation
  gate — this was already true for every previously-bridged write tool
  (`remember`, `create_memory_note`, `update_project_status`), not
  introduced by this pass, but is now true for every bridged write tool.
  Flagged as a real, separate follow-up, not fixed here.

## 0.9.2 — 2026-09-30

Maintenance pass: no new Brain business capabilities. Prompted by a full
`mcp_bridge` parity audit (advertised MCP tools vs. what the bridge can
actually execute).

### Fixed

- `mcp_bridge.py`'s `tools/list` advertised all 20 of Brain's real MCP
  tools (from `brain capabilities`) while only 9 had a CLI translator
  wired up — a client could see, and attempt to call, 11 tools that would
  always fail with "not yet supported". `tools/list` now filters to
  exactly the tool names `TRANSLATORS` covers, restoring the bridge's own
  documented invariant: never advertise a tool `tools/call` cannot
  execute. Added regression coverage (`TestToolsListAdvertisedMatchesExecutable`
  in `tests/test_mcp_bridge.py`) asserting this for any shape `brain
  capabilities` returns, not just today's 9-vs-20 split.
- The remaining gap (4 read tools Claude/Codex would want —
  `get_project`, `get_project_path`, `project_context`,
  `search_timeline` — have no `--json` CLI output or, for the latter two,
  no single CLI command to shell out to at all) is not fixed in this pass;
  it needs a scoped decision on its own, not a rushed one bundled into a
  maintenance release. See the full advertised/mapping/read-write parity
  table from this audit for exact gaps.

### Documentation

- README: current release corrected 0.7.0 → 0.9.1 (now 0.9.2), all
  install-example wheel filenames corrected to match, and a new "MCP
  servers" section explains `mcp_server.py` (in-process, shipped 0.1.0)
  vs. `mcp_bridge.py` (subprocess/CLI-only, shipped 0.9.0) side by side —
  previously the Roadmap section mentioned only the former, making the
  latter's existence undiscoverable from the README.

## 0.9.1 — 2026-09-28

### Fixed

- `mcp_bridge.py`'s `write_handoff` tried to JSON-parse `brain handoff
  write`'s plain-text success line (that command has no `--json`, unlike
  every other write command the bridge calls) and failed on every real
  call. Found during Claude/Codex live acceptance. Parsed instead, same
  discipline `list_projects`/`read_memory` already use for CLI output with
  no JSON mode.

## 0.9.0 — 2026-09-28

### Added

- **`python3 -m brain.mcp_bridge`** — an MCP server that reaches Brain
  *exclusively* through the already-installed `brain` CLI executable, as a
  subprocess. Distinct from `mcp_server.py` (which calls Brain's Python
  business logic in-process against a locally resolved vault): this bridge
  is for the common case where the real vault is reached only through the
  `brain` client wrapper's own transparent proxy, and `mcp_server.py` run
  directly would either find no vault or silently serve a stale local
  copy. Zero new credentials, zero new server-side surface, zero direct
  vault access, zero business logic of its own — `tools/list` is derived
  live from `brain capabilities`, so its schema can never drift from a
  second hand-kept copy. Coverage in this first version: search, context,
  read, remember, note create, a controlled project-status update, and
  session handoffs (provenance defaults to the connected client's own
  name). Anything else returns a clear "not yet supported" error rather
  than guessing. Local-override env vars (`BRAIN_ROOT`, `BRAIN_LOCAL`,
  `BRAIN_STATE_DIR`, `BRAIN_VAULT`) are stripped from every subprocess
  call it makes.
- `--json` on `search`/`remember`/`note create`/`project update` — the
  stable, machine-readable contract the bridge needs, matching the shape
  each command's equivalent MCP tool already returns. Human-text output is
  unchanged when the flag is absent.

## 0.8.0 — 2026-09-28

### Added

- **`brain capabilities`** (JSON) — self-description for any client (Claude
  Code, Codex, a phone quick-capture flow, a future TRON-side agent):
  version, every CLI command (derived from the real parser, never a second
  hand-kept list), every MCP tool and its input schema (from the real
  `mcp_server.TOOLS`). Deliberately does not predict read-vs-write
  permission per command — that split is enforced server-side by the
  dispatcher, per connected identity; a disallowed command still gets a
  clear refusal naming the reason.
- `get_context()` now prefers a note's *current* dated section
  (`"## Session <date>"` from a handoff, or `"## Update (<date>)"` from
  `brain update --append-text`) over whichever window SQLite's `snippet()`
  happened to match, and sorts current-first. Read-time only — no storage
  change, no migration, no history rewritten. `ContextItem` gains `updated`
  and `snippet_from_latest_section`; `brain context` (text mode) now shows
  `[historical/superseded]` and `(updated <date>)` inline. Found during a
  cross-project Brain/TRON reconciliation: a plain search could surface an
  old handoff session's text over a newer, contradicting one purely because
  it scored better textually.

### Fixed

- Handoff frontmatter no longer hardcodes `source: claude-session`
  regardless of who actually wrote it. `brain handoff write` reads an
  optional `"source"` field from its JSON payload (default `"cli"`); the
  MCP `write_handoff` tool defaults it to the connected client's own
  reported name instead.
- README: "Current release" said 0.2.0, never updated across five releases;
  the Roadmap section claimed no MCP server existed, which has been wrong
  since v0.1.0 (2026-09-08, the first public release) — `brain/mcp_server.py`
  has shipped every release since.

## 0.7.0 — 2026-09-26

### Added

- **`brain project section-update <id> --section <name> --mode replace|append --content "..." [--if-match <sha256>]`**
  — closes the last direct-write gap: `/project-sync`'s named-body-section
  edit. Restricted to four allowlisted project-template sections
  (`Current state`, `Milestones`, `Problems / limitations`, `Next
  actions`) — `Decisions` is deliberately excluded (use `brain decision
  create` instead). The section's position is found by parsing the note's
  own `## ` headers server-side; the caller never supplies a path or line
  range. `--if-match` is an optional optimistic-concurrency guard: pass
  the sha256 of the section's content as last read, and the write is
  refused if it changed since — omit it for the normal single-agent case.
- New MCP tool `update_project_section`, same secret-scan policy as the
  rest.
- Migrated `/project-sync` onto it — every skill that writes now goes
  through a `brain` CLI primitive, no remaining direct-write exceptions.

## 0.6.1 — 2026-09-26

### Fixed

- **Security**: the SQLite FTS5 index (`brain.db`) was created with the
  process's default umask — commonly world-readable — the same class of
  bug `ensure_private_file()` already exists to fix for the MCP log files,
  just never applied to the index itself. The index holds full note
  bodies, restricted ones included, so this is a real information
  exposure on any multi-user or multi-tenant host. `indexer.connect()`
  (the one choke point every caller already goes through) now chmods the
  file to owner-only right after opening it. Found during a server
  hardening review — a deployed instance's real `brain.db` was confirmed
  world-readable before this fix, not just theorized.

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
