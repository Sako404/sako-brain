# Reporting Brain state

This procedure applies to CLI and MCP clients. All examples are synthetic:
project-example, a fictional service incident, and invented note counts.
No example describes a particular user's deployment or private records.

## 1. Five steps

### Step 1 — Take a fresh snapshot

Run `brain state --json` when the caller is authorized. Record its generation
time and inspect per-source errors. This admin-only aggregate includes
index-derived sections; a successful response does not prove the index is
fresh or that every canonical record was discovered. Do not use its counts
as a substitute for the metadata listing below.

Its `backlog` section lists the documents tagged `backlog` (status
`current`) — cross-project next-action lists whose items exist nowhere else
in the snapshot. Read each one before reporting what is open. `stale: true`
means it was last updated before the newest handoff (`newest_handoff`), so
a later session may have changed state without re-sorting it: report that,
and check the newer handoffs for what it misses.

### Step 2 — Enumerate by metadata

For an exhaustive question, use:

```sh
brain list --type decision --status proposed --json
```

`brain search` / `search_memory` performs ranked full-text search with a
limit, not metadata filtering. A query such as `status: proposed` matches
words in bodies. For example, a fictional vault could contain six proposed
decisions while a text query returns four hits, one of them already decided.
Use `brain list` / `list_notes` to answer the metadata question.

Read all three completeness fields:

- `source_complete`: the scan encountered no relevant unresolved source
  problem. It is false for visible malformed/unstructured records, traversal
  errors, or files whose visibility could not be established.
- `page_complete`: `entries` contains every verified match. Any positive
  offset makes this false, including the final page and an offset beyond the
  last match. A cutting limit also makes it false.
- `complete`: the conjunction of both fields. Only true means that this
  response's entries are the complete answer under the scan contract.

Continue with `next_offset` until it is null. Limits are capped at 1000;
requesting a larger limit cannot bypass pagination. With multiple pages,
check `source_complete` on each response and report the aggregation method.
Reads do not lock writers or provide a snapshot. If data changes during
pagination, restart or explicitly mark the aggregate unverified.

### Source completeness and privacy boundary

Known visibility is checked using live frontmatter before materializing
entries, validating listing metadata or counting faulty records. A known
hidden record does not affect `total_visible`, `unreadable`,
`unstructured_skipped`, ordering or completeness. Invalid ACLs, including
those in otherwise parseable YAML, have unknown visibility: they fail closed,
never grant default ownership and prevent confirmed source completeness.

Unknown visibility is different from a confirmed ACL denial. A failed
traversal or unreadable/unparseable header cannot prove there were no visible
matches. Such a failure sets `source_complete=false` and `complete=false`,
but adds no filename, ID, owner, raw error, hidden-document count or presumed
match to the response. Zero diagnostic counters with incomplete source means
verification was inconclusive, not that hidden records exist. This is a
source-verification signal, not an inventory of inaccessible data. Clients
must never derive a hidden-record count from it.

A readable header can still establish that an undecodable body belongs to
another principal; that known denial remains invisible to the caller. Raw
captures use the documented default-owner rule. Invalid visible `tags`,
`projects`, `id`, `type`, `status` or `updated` metadata is counted as
`unreadable`, omitted, and prevents source completeness. String shorthand
for list fields and YAML date values remain supported.

Configured content directories may be absent in a partially populated vault.
An absent directory is not an access failure. A directory that exists but
cannot be scanned, or an I/O error during enumeration, prevents verification.
Ask an administrator to inspect permissions, storage and `brain doctor`;
do not try to widen a principal's access to make an audit complete.

Discovery uses canonical Markdown rather than the index. Directory symlinks
are never traversed. Files outside the vault, broken/looping links, and
resolved aliases into `90_SYSTEM/identity` are excluded by policy. Dedicated
identity APIs still manage that protected tree. Internal file links to
ordinary memory remain candidates and may create ambiguous IDs. Admin-only
`brain doctor` reports excluded unsafe links and invalid ACLs. These explicit
exclusions are distinct from a failure to enumerate the configured source.

### Step 3 — Read the full record

Use `brain get <id>` / `read_memory`; snippets cannot establish a record's
current meaning. A fictional incident might have a closure section appended
after its original description while metadata still says `proposed`. Report
the disagreement instead of silently deciding which field to change.

For a project, its `## Current state` section (with `current_state_updated`,
also returned by `brain project show` / `get_project`) is where it stands;
the rest of the record is history. An empty section, or one dated before the
latest handoff, is a finding (`brain doctor` reports
`project_current_state_missing` / `project_current_state_stale`), not a
licence to promote an older line from elsewhere in the record to current.

If there is a duplicate-ID warning, reconcile the visible copies before
relying on a single history. Entirely hidden IDs and absent IDs have neutral
not-found responses; no visible result does not prove no record exists.

### Step 4 — Read the latest handoff

Use `brain handoff show <project-id>` or `project_context`. A fictional task
record might contain only a template while its handoff describes the actual
work, unresolved issues and next action. A handoff has its own ACL; seeing
the project does not imply seeing or owning its handoff.

### Step 5 — Check later closures and relevant live evidence

Look for later closures or corrections. Metadata status, content state and
production-verified behavior answer different questions. If production
access is unavailable, mark that claim unverified. A refusal or unavailable
client is not proof of a failed deployment. Do not write or restart anything
merely to complete a read-only report.

## 2. Four kinds of claim

| Kind | Evidence |
|---|---|
| Metadata status | Live frontmatter such as `status: proposed` |
| Content state | The full body, including appended corrections |
| Production-verified | An actual, authorized check in the target environment |
| Unverified | Missing access, unavailable sources or inconclusive evidence |

A record is evidence of a past observation, not proof of the current working
tree. Check the relevant repository for code claims. A path obtained from a
record does not prove the caller's machine can access it.

## 3. Writes and recovery

Project creation, status changes, close, section edits, generic updates,
handoff writes, registry helpers and owner migration share
`projects.write.lock`. All processes for one vault must use the same state
directory. The lock is advisory; edits by other programs are outside it.

A status change preflights all documents it will modify. Owning the project
does not authorize moving another principal's handoff. A failed authorization
returns a generic refusal before any record, registry or handoff change;
it must not disclose an invisible handoff's identity, owner or path.

Creating or updating a handoff requires a visible project record. Hidden,
missing or malformed project records cannot authorize a new handoff. A new
handoff is owned by its creator. Existing handoffs remain editable only by
their own owner; audience grants read access, not write permission.

Recoverable status-change failures roll back the journaled steps. This is
exception recovery, not a persistent power-loss/SIGKILL transaction journal.
After interruption, retry the same status operation only when authorized,
then inspect the record, registry and handoff. Missing content requires a
verified backup; a retry cannot reconstruct it. Index failures after correct
Markdown writes are reported separately as `index_updated=false`.

`duplicate_ids` and `handoff_in_wrong_status_folder` are structural findings.
`status_content_conflict` is advisory and never automatically closes a
record, rewrites history or blocks a snapshot. Report disagreements for
human reconciliation.

## 4. Integration boundaries

- `mcp_server.py` runs domain functions against a configured local vault.
- `mcp_bridge.py` invokes the CLI and inherits its selected transport. Its
  translators build argv; they are not a second implementation of the domain.
- `remote_gateway` applies OAuth and scope checks before using the bridge.
  Its authenticated principal is validated at the backend boundary as well.

Use the integration appropriate for the configured canonical server. Do not
point an integration at an obsolete local copy. Changing a gateway hostname
and migrating a vault are distinct operations and need separate evidence.

Version history also matters: v0.12.3 changed write-to-index consistency;
v0.14.5 added read-failure diagnostics. Do not describe diagnostic changes
as an indexing or ACL fix. Verify what the actual deployed artifact contains.

## 5. Environment-dependent checks

A missing backup task is not sufficient evidence of missing protection:
recursive snapshots, scripts, retention and off-site copies require their
own checks. A `.git` pointer file can be intentional; evaluate it in the
environment that runs the command. An unavailable systemd user manager means
unknown timer state, not an absent unit. Report unverified checks separately.

Never publish private record contents, deployment paths, incident details or
raw diagnostics in source, fixtures, commit messages, CI logs or release
artifacts. Use synthetic examples and keep deployment evidence outside the
repository with appropriate access restrictions.
