"""Project registry + record lifecycle: create, status transitions, close.

A project's status lives in three places at once — the record's own
frontmatter, the physical `30_PROJECTS/<STATUS>/` folder it sits in, and the
registry's `status:` field — and `brain doctor` already checks that all three
agree. These functions are the write side of that same invariant: every
status change here moves the file and updates the registry in one call, so
the three can never drift apart the way a hand-edit can leave them.

`_registry.yaml`'s header comments ARE its schema documentation (see the
file itself), and `pyproject.toml` keeps PyYAML as the *only* runtime
dependency, deliberately — so a full `yaml.safe_dump` of the whole registry
(which would lose every comment) is never used here. Every write below
touches only the one entry it means to change, via plain text splicing, and
leaves the rest of the file byte-identical.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover — POSIX only; see _project_write_lock
    fcntl = None

import yaml

from . import audit
from . import frontmatter
from . import indexer
from . import visibility
from .paths import Config
from .registry import find_project, load_registry
from .update import find_note_paths, resolve_write_target

_ENTRY_RE = re.compile(r"^  - id: (\S+)\s*$")


class ProjectWriteError(ValueError):
    """A project create/status-change request that cannot be applied safely."""


STATUS_WRITE_DENIED = "project status change is not permitted"


@dataclass
class StatusChangeResult:
    id: str
    old_status: str
    new_status: str
    old_path: Path
    new_path: Path
    moved: bool
    registry_updated: bool
    # Set when the project's handoff document had to travel with the record
    # (see _relocate_handoff) — None when there was no handoff to move.
    handoff_old_path: Path | None = None
    handoff_new_path: Path | None = None
    # False when the Markdown and registry were written correctly but the
    # search-index cache update failed — `brain index` repairs it. Never a
    # reason to roll a correct move back.
    index_updated: bool = True


# One lock for every write that touches a project record's LOCATION or the
# shared registry. Both are involved in each such write, so one lock is
# enough and a second would only add a deadlock to reason about.
#
# Why it exists: `_Journal` records each step's inverse at the moment the
# step succeeds, which is correct against a crash and useless against a
# concurrent writer. An independent review (synthetic regression) drove two status
# changes at one project: the slower one failed and replayed its inverses
# over bytes the faster one had since committed, leaving two record copies,
# a registry naming neither, and a handoff beside the wrong one. No journal
# can fix that; the operations have to not interleave.
#
# Lives in the state directory, outside the vault, for the same reason the
# index and the logs do (OSS-1) — it is runtime state, not content, and must
# never be synced or backed up as part of the Brain.
LOCK_FILENAME = "projects.write.lock"
_write_locks = threading.local()


@contextlib.contextmanager
def project_write_lock(config: Config):
    """Serialize project-record and registry writes across processes.

    `flock` on a dedicated lock file: advisory, released by the kernel if the
    holder dies (so a crashed write cannot wedge the vault the way a
    lockfile-with-stale-detection can), and visible to every process reaching
    this vault through this package — the desktop CLI, the MCP server, the
    bridge's subprocesses and the SSH dispatcher's `brain` alike.

    Reentrant within one thread so composed operations and public registry
    helpers can keep the same lock from discovery through rollback. Other
    threads open independent file descriptions and therefore contend just
    like other processes. Generic updates, handoffs and migrations use this
    lock too: each can otherwise recreate a record at a path just moved.

    On a platform with no `fcntl`, this refuses rather than running
    unsynchronised. A correctness guarantee that silently disappears is worse
    than one that is absent out loud — the same rule `validate.py`'s own
    fail-closed checks follow.
    """
    if fcntl is None:  # pragma: no cover — POSIX only
        raise ProjectWriteError(
            "project writes need file locking (fcntl.flock), which this platform does "
            "not provide — refusing rather than performing an unsynchronised write that "
            "could leave the record, the registry and the handoff disagreeing."
        )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = (config.state_dir / LOCK_FILENAME).resolve()
    held = getattr(_write_locks, "held", None)
    if held is None:
        held = _write_locks.held = {}
    if lock_path in held:
        yield
        return
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held[lock_path] = handle
        try:
            yield
        finally:
            del held[lock_path]
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _after_fork():
    # A forked child must neither reuse the parent's reentrancy marker nor
    # keep its lock alive after the parent exits. Closing the inherited copy
    # does not unlock the parent's still-open description.
    for handle in getattr(_write_locks, "held", {}).values():
        handle.close()
    _write_locks.held = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _dump_registry_entry(entry: dict) -> str:
    lines = yaml.safe_dump(entry, sort_keys=False, allow_unicode=True).rstrip("\n").splitlines()
    out = [f"  - {lines[0]}"]
    out.extend(f"    {line}" for line in lines[1:])
    return "\n".join(out) + "\n"


def _require_vault_paths(config: Config, *paths: Path) -> None:
    if any(not frontmatter.is_memory_path(path, config.brain_root) for path in paths):
        raise ProjectWriteError("project write location is outside the vault or is an unusable link")


def add_registry_entry(config: Config, entry: dict) -> None:
    """Append one entry to `_registry.yaml`. Every existing byte — including
    the header comments — is left untouched; this only ever adds lines."""
    with project_write_lock(config):
        _add_registry_entry_locked(config, entry)


def _add_registry_entry_locked(config: Config, entry: dict) -> None:
    _require_vault_paths(config, config.registry_path)
    if config.registry_path.exists():
        text = config.registry_path.read_text(encoding="utf-8")
    else:
        config.registry_path.parent.mkdir(parents=True, exist_ok=True)
        text = "projects:\n"
    if not text.endswith("\n"):
        text += "\n"
    # An empty inline list (`projects: []`, e.g. a freshly-initialized vault)
    # can't have block-style items appended after it — that's invalid YAML.
    # Rewrite it to block form first so the appended entry parses.
    text = re.sub(r"^projects:[ \t]*\[[ \t]*\][ \t]*$", "projects:", text, count=1, flags=re.MULTILINE)
    config.registry_path.write_text(text + _dump_registry_entry(entry), encoding="utf-8")


def _entry_span(lines: list[str], project_id: str) -> tuple[int, int]:
    start = None
    for i, line in enumerate(lines):
        m = _ENTRY_RE.match(line)
        if m and m.group(1) == project_id:
            start = i
            break
    if start is None:
        raise ProjectWriteError(f"no registry entry with id '{project_id}'")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _ENTRY_RE.match(lines[j]):
            end = j
            break
    return start, end


def set_registry_fields(config: Config, project_id: str, **fields) -> None:
    """Update one or more fields on exactly one registry entry, in place.
    Every other line in the file — every other entry, every comment — is
    left byte-identical. The touched entry itself is re-serialized whole
    (via `yaml.safe_dump`), so its own quoting/flow-vs-block style can shift
    cosmetically even for untouched fields — the same trade-off
    `update_mod.update_memory()` already makes for a note's frontmatter."""
    with project_write_lock(config):
        _set_registry_fields_locked(config, project_id, **fields)


def _set_registry_fields_locked(config: Config, project_id: str, **fields) -> None:
    _require_vault_paths(config, config.registry_path)
    if not config.registry_path.exists():
        raise ProjectWriteError(f"no registry file at {config.registry_path}")
    text = config.registry_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start, end = _entry_span(lines, project_id)

    block = "".join(lines[start:end])
    parsed = yaml.safe_load(block)
    entry = parsed[0] if isinstance(parsed, list) else parsed
    entry.update(fields)

    new_text = "".join(lines[:start]) + _dump_registry_entry(entry) + "".join(lines[end:])
    config.registry_path.write_text(new_text, encoding="utf-8")


PROJECT_BODY_TEMPLATE = (
    "# {name}\n\n"
    "## Purpose\n\n"
    "## Location\n\n"
    "## Status\n\n"
    "## Current state\n\n"
    "## Architecture / technologies\n\n"
    "## Important locations\n\n"
    "## Milestones\n\n"
    "## Decisions\n\n"
    "Links to `40_DECISIONS/` entries; summarize project-internal decision "
    "logs rather than duplicating every entry.\n\n"
    "## Problems / limitations\n\n"
    "## Next actions\n\n"
    "## Relationships\n\n"
    "## Sources\n"
)


def create_project(config: Config, id: str, name: str, path: str, status: str = "active",
                    category: str | None = None, aliases: list[str] | None = None,
                    created: str | None = None, audience: list[str] | None = None) -> Path:
    """Register a project and create its record. Never copies project source
    files into the vault — `path` is a reference only, exactly like the
    `/project-new` skill's own rule."""
    allowed = config.vocabulary.statuses_for("project")
    if allowed and status not in allowed:
        raise ProjectWriteError(f"status must be one of {allowed}")
    with project_write_lock(config):
        return _create_project_locked(config, id, name, path, status, category,
                                       aliases, created, audience)


def _create_project_locked(config: Config, id: str, name: str, path: str, status: str,
                            category: str | None, aliases: list[str] | None,
                            created: str | None, audience: list[str] | None) -> Path:
    """`create_project`'s body, under the write lock. The duplicate-id and
    duplicate-path checks it starts with are only meaningful while nothing
    else can append to the registry between the check and the write."""
    existing = load_registry(config)
    if any(e.id == id for e in existing):
        raise ProjectWriteError(f"registry already has an entry with id '{id}'")
    norm_path = str(Path(path))
    if any(e.path and str(Path(e.path)) == norm_path for e in existing):
        raise ProjectWriteError(f"registry already has an entry with path '{path}'")

    folder = config.taxonomy.folder_for_status(status)
    dest = config.projects_dir / folder / f"{id}.md"
    _require_vault_paths(config, dest, config.registry_path)
    if dest.exists():
        raise ProjectWriteError(f"project record already exists at {dest}")

    today = created or dt.date.today().isoformat()

    meta = {
        "id": id, "type": "project", "status": status,
        "created": today, "updated": today,
        "people": [], "projects": [], "tags": [], "sensitivity": "normal",
        "source": "", "confidence": "fact", "category": category or "",
        "parent_project": "", "technologies": [], "last_activity": today,
        "has_git": False, "aliases": aliases or [], "path": str(path),
        "owner_principal": visibility.id_to_ref(config.acting_principal),
        "audience": audience or [],
    }
    note = frontmatter.Note(path=dest, meta=meta, body=PROJECT_BODY_TEMPLATE.format(name=name))

    # Both preconditions are already checked above — write the record first
    # so a failure here never leaves a registry entry with no record behind it.
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(frontmatter.render(note), encoding="utf-8")

    add_registry_entry(config, {
        "id": id, "name": name, "path": str(path), "status": status,
        "category": category or "", "created": today, "updated": today,
        "aliases": aliases or [],
    })
    indexer.index_note(config, dest)
    audit.log_event(config, event="note.write", principal_id=config.acting_principal,
                     **audit.caller_provenance(config),
                     detail=f"id={id}")
    return dest


def _handoff_filename(project_id: str) -> str:
    return f"{project_id}-handoff.md"


def _existing_handoff_paths(config: Config, project_id: str) -> list[Path]:
    """Every status folder that currently holds a handoff for this project.

    Scans the folders rather than trusting the registry, because the whole
    point of this helper is to find a handoff that is NOT where the registry
    implies it should be — a legacy orphan left by a pre-v0.14.6 status
    change, or one stranded by a failure partway through this function.
    """
    filename = _handoff_filename(project_id)
    found = []
    for _status, folder in config.taxonomy.project_status_folders:
        candidate = config.projects_dir / folder / filename
        if candidate.exists() and candidate not in found:
            found.append(candidate)
    return sorted(found)


class _UndoSkipped(Exception):
    """An inverse that declined to run because running it would have made
    things worse. Reported like any other rollback failure, never silent."""


class _Journal:
    """A tiny undo log for the one multi-step write in this module.

    Not a transaction framework, and deliberately not trying to be: four
    filesystem steps that each have an exact inverse (restore these bytes,
    rename back), recorded as they succeed and replayed in reverse if a
    later one fails. An independent review reproduced all three ways the
    previous version could leave a split state — a handoff stranded in the
    old folder, a record whose frontmatter had been rewritten before a
    failed rename, and a registry that never got updated while the function
    still returned success.

    Undo is best-effort by construction: if an inverse itself fails there is
    nothing better to do than keep going and report the original error, so
    the operator sees the real cause rather than a cascade. What makes that
    acceptable is that `set_project_status` is also *converging* — running
    it again computes the deltas from what is actually on disk, so a partial
    state is repaired by a retry rather than needing this log to be perfect.
    """

    def __init__(self):
        self._undo = []

    def restore_text_of(self, path: Path) -> None:
        """Remember this file's current bytes, to put back on rollback.

        The inverse REPLACES content; it never creates the file. That
        distinction is the whole fix for a defect an independent review found
        (synthetic regression): when the record's rename-back failed, this step then
        wrote the original bytes at the old path anyway, so the vault ended up
        with the moved file AND a resurrected copy of it — two files under one
        id, which the converging retry could no longer repair because a write
        addressed by an ambiguous id is (correctly) refused. A rollback that
        can manufacture a duplicate is worse than one that stops early, so
        this reports instead.
        """
        original = path.read_text(encoding="utf-8")

        def undo():
            if not path.exists():
                raise _UndoSkipped(
                    f"{path} is no longer there, so its previous content was not "
                    "restored — recreating it would have made a second copy of a "
                    "record that still exists elsewhere"
                )
            path.write_text(original, encoding="utf-8")

        self._undo.append(undo)

    def reverse_rename(self, src: Path, dest: Path) -> None:
        self._undo.append(lambda: dest.rename(src))

    def rollback(self) -> list[str]:
        failures = []
        for undo in reversed(self._undo):
            try:
                undo()
            except (OSError, _UndoSkipped) as exc:  # noqa: PERF203 — independent inverses
                failures.append(str(exc))
        self._undo.clear()
        return failures


def set_project_status(config: Config, project_id: str, new_status: str) -> StatusChangeResult:
    """Move a project record between status folders and keep its frontmatter,
    its registry entry and its handoff document in sync — the one thing plain
    `brain update` cannot do, because it never moves files or touches the
    registry.

    Two properties this function has to hold, both added after an independent
    review broke the earlier version:

    **Converging.** Every decision below is taken from what is actually on
    disk — the folder the record sits in, and the folders that actually
    contain a handoff — never from the record's own `status:` field or the
    registry's. So running it again after a partial failure finishes the job
    instead of being confused by the half-applied state, and running it on an
    already-correct project is a no-op rather than an error. That also means
    it repairs a handoff orphaned by any *earlier* status change, including
    ones made before this function knew to move it.

    **All-or-nothing, or an explicit error.** The frontmatter write, the
    record rename, the registry update and the handoff relocation are
    journalled (see `_Journal`) and rolled back together if any of them
    fails. A failed registry update is fatal: the previous version swallowed
    it and returned a result whose `registry_updated=False` nobody read,
    leaving the record and its handoff in the new folder while every reader
    resolving the handoff from the registry looked in the old one.

    Reindexing is the one step that is NOT rolled back. The index is a
    rebuildable cache (see `indexer.py`) and the Markdown is already correct
    at that point, so discarding a correct move because a cache write failed
    would be the wrong trade — `index_updated=False` on the result says so,
    and `brain index` repairs it.
    """
    allowed = config.vocabulary.statuses_for("project")
    if allowed and new_status not in allowed:
        raise ProjectWriteError(f"status must be one of {allowed}")
    with project_write_lock(config):
        return _set_project_status_locked(config, project_id, new_status)


def _set_project_status_locked(config: Config, project_id: str,
                                new_status: str) -> StatusChangeResult:
    """The body of `set_project_status`, which holds the write lock for its
    whole duration. Split out only so the lock is acquired in exactly one
    place and cannot be forgotten by a future caller — never call this
    directly."""
    # Resolve once, up front — project_id may be an alias; everything below
    # (file lookup, registry update, the result's own id) must use the one
    # canonical id, never the alias, or the two would silently diverge.
    requested_id = project_id
    missing = f"no project record with id or alias '{requested_id}'"
    entry = find_project(config, project_id)
    if entry is None:
        raise ProjectWriteError(missing)
    project_id = entry.id

    principal_id = config.acting_principal
    try:
        record_path = resolve_write_target(config, project_id, principal_id)
    except FileNotFoundError:
        # Do not reveal that an invisible alias resolved to a canonical ID.
        raise ProjectWriteError(missing) from None
    except (OSError, frontmatter.FrontmatterError):
        # Resolving a project scans other records too. An unreadable record
        # must not leak its path through a raw filesystem exception.
        raise ProjectWriteError(STATUS_WRITE_DENIED) from None

    note = frontmatter.parse_file(record_path)
    # The status the record CLAIMS, kept only for the audit line and the
    # returned result. Nothing below branches on it — see the docstring.
    old_status = note.status or "unknown"

    if not visibility.is_owner(config, principal_id, note.meta):
        current_owner, _ = visibility.owner_and_audience(note.meta)
        audit.log_event(config, event="note.write.denied", principal_id=principal_id,
                         **audit.caller_provenance(config),
                         detail=f"id={project_id} owner={current_owner}")
        raise ProjectWriteError(STATUS_WRITE_DENIED)

    new_folder = config.taxonomy.folder_for_status(new_status)
    target_record = config.projects_dir / new_folder / record_path.name
    target_handoff = config.projects_dir / new_folder / _handoff_filename(project_id)
    _require_vault_paths(config, target_record, target_handoff, config.registry_path)

    # Every refusal happens before the first byte is written.
    if target_record != record_path and target_record.exists():
        raise ProjectWriteError(f"a file already exists at the target location {target_record}")

    stranded_handoffs = [p for p in _existing_handoff_paths(config, project_id)
                          if p != target_handoff]
    _require_vault_paths(config, *stranded_handoffs)
    if stranded_handoffs and target_handoff.exists():
        # Two copies already exist. Merging them is a content decision for a
        # human (`brain doctor`'s duplicate_ids and
        # handoff_in_wrong_status_folder both report it); this must not pick
        # one, and must not block the status change either.
        handoff_to_move = None
    elif len(stranded_handoffs) > 1:
        # More than one stranded copy and an empty target: still ambiguous,
        # still not this function's call to make.
        handoff_to_move = None
    else:
        handoff_to_move = stranded_handoffs[0] if stranded_handoffs else None

    if handoff_to_move is not None:
        # Authorize every document to be mutated before the first mkdir,
        # rename or write. A handoff has its own ACL; owning its project
        # grants no authority over another principal's document.
        try:
            handoff_note = frontmatter.parse_file(handoff_to_move)
            permitted = (handoff_note.id == f"handoff-{project_id}"
                         and visibility.can_view_note(config, principal_id, handoff_note)
                         and visibility.is_owner(config, principal_id, handoff_note.meta))
        except (frontmatter.FrontmatterError, OSError, visibility.VisibilityError):
            permitted = False
        if not permitted:
            audit.log_event(config, event="project.status_change.denied", principal_id=principal_id,
                            **audit.caller_provenance(config), detail=f"id={project_id}")
            raise ProjectWriteError(STATUS_WRITE_DENIED)

    journal = _Journal()
    moved = False
    handoff_moved_from = None
    try:
        # Order matters, and this is the order: MOVE first, then edit the
        # frontmatter at the file's final location. The reverse order meant
        # the content-restoring inverse ran against the file's OLD path, and
        # if the rename-back had already failed, restoring content there
        # created a second copy of a record that still existed at the new
        # path. With the move first, every inverse acts on a path the file is
        # actually at, and none of them can bring a file into existence.
        if target_record != record_path:
            target_record.parent.mkdir(parents=True, exist_ok=True)
            record_path.rename(target_record)
            journal.reverse_rename(record_path, target_record)
            moved = True

        if note.meta.get("status") != new_status:
            journal.restore_text_of(target_record)
            note.meta["status"] = new_status
            note.meta["updated"] = dt.date.today().isoformat()
            target_record.write_text(frontmatter.render(note), encoding="utf-8")

        # Fatal on failure, and rolled back with everything else.
        journal.restore_text_of(config.registry_path)
        set_registry_fields(config, project_id,
                             status=new_status, updated=note.meta["updated"])

        if handoff_to_move is not None:
            target_handoff.parent.mkdir(parents=True, exist_ok=True)
            handoff_to_move.rename(target_handoff)
            journal.reverse_rename(handoff_to_move, target_handoff)
            handoff_moved_from = handoff_to_move
    except Exception as exc:  # noqa: BLE001 — every step's failure is handled identically
        undo_failures = journal.rollback()
        audit.log_event(config, event="project.status_change.failed", principal_id=principal_id,
                         **audit.caller_provenance(config),
                         detail=f"id={project_id} {old_status}->{new_status} "
                                f"rolled_back={not undo_failures}")
        suffix = ""
        if undo_failures:
            # Be specific about what recovery is actually available. Re-running
            # converges a vault that still holds exactly one copy of the
            # record; it cannot converge one that holds two, because a write
            # addressed by an ambiguous id is refused by design. Saying "just
            # re-run" in both cases was the misleading half of this message.
            copies = len(find_note_paths(config, project_id))
            recovery = (
                "Re-run this exact command to converge the vault, then `brain doctor`."
                if copies == 1 else
                "No readable record remains. Restore it from a verified backup before retrying."
                if copies == 0 else
                "The record now resolves to more than one file, so re-running CANNOT "
                "repair it — a write addressed by an ambiguous id is refused. Reconcile "
                "the copies by hand first (`brain doctor`, duplicate_ids), keeping the "
                "one in the folder the registry names."
            )
            suffix = (" The rollback itself did not fully succeed, so the vault is in a "
                      f"partial state: {' '.join(f for f in undo_failures)} {recovery}")
        outcome = "rollback incomplete" if undo_failures else "was rolled back"
        raise ProjectWriteError(
            f"status change for '{project_id}' failed; {outcome} ({exc}).{suffix}"
        ) from exc

    # Past this point the Markdown and the registry agree. The index is a
    # cache, so its failure is reported, never a reason to undo correct data.
    index_updated = True
    try:
        indexer.index_note(config, target_record)
        if handoff_moved_from is not None:
            indexer.index_note(config, target_handoff)
    except Exception:  # noqa: BLE001 — a cache write, not the record
        index_updated = False

    audit.log_event(config, event="note.write", principal_id=config.acting_principal,
                     **audit.caller_provenance(config),
                     detail=f"id={project_id} status={old_status}->{new_status}")
    return StatusChangeResult(
        id=project_id, old_status=old_status, new_status=new_status,
        old_path=record_path, new_path=target_record, moved=moved,
        registry_updated=True,
        handoff_old_path=handoff_moved_from,
        handoff_new_path=target_handoff if handoff_moved_from is not None else None,
        index_updated=index_updated,
    )


def close_project(config: Config, project_id: str, summary: str = "") -> StatusChangeResult:
    """Archive a project: status -> archived, moved + registry-synced, and
    (if given) a closing summary appended via the existing update primitive —
    never invents content, never deletes the record or prior sections."""
    with project_write_lock(config):
        result = set_project_status(config, project_id, "archived")
        if summary:
            from . import update as update_mod
            update_mod.update_memory(config, result.id, append_text=summary)
        return result


# Extracted from what /project-sync already does today via a direct Edit on
# specific named body sections — never the whole body, never an arbitrary
# one. Restricted to the sections that skill actually touches; "Decisions"
# is deliberately excluded (the skill's own rule: leave it alone, use
# /decision instead), and every other project-template section (Purpose,
# Location, Architecture, Important locations, Relationships, Sources) has
# no sync-driven write need today, so is left out rather than allowlisted
# "just in case".
PROJECT_SECTION_ALLOWLIST = ("Current state", "Milestones", "Problems / limitations", "Next actions")


class SectionEditError(ValueError):
    """A named-section edit that cannot be applied safely."""


def _section_span(lines: list[str], section: str) -> tuple[int, int]:
    """Line range [start, end) of one '## <section>' block's CONTENT,
    excluding the header line itself, up to the next '## ' header or EOF."""
    header = f"## {section}"
    start = None
    for i, line in enumerate(lines):
        if line.rstrip("\n") == header:
            start = i + 1
            break
    if start is None:
        raise SectionEditError(f"section '{section}' not found in this note's body")
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return start, end


def update_section(config: Config, project_id: str, section: str, mode: str, content: str,
                    if_match: str | None = None) -> Path:
    """Replace or append the content of one allowlisted section in a
    project record. Never touches any other section, never accepts an
    arbitrary path or line range — `section` must be on
    `PROJECT_SECTION_ALLOWLIST`, and the section's real position is found
    by parsing the note's own headers, not supplied by the caller.

    `if_match`, if given, is the sha256 of the section's content as the
    caller last read it — an optimistic-concurrency guard against two
    callers syncing the same project at once; omit it for the normal
    single-agent case."""
    if section not in PROJECT_SECTION_ALLOWLIST:
        raise SectionEditError(f"section must be one of {PROJECT_SECTION_ALLOWLIST}")
    if mode not in ("replace", "append"):
        raise SectionEditError("mode must be 'replace' or 'append'")
    # Takes the same lock as the status change: this resolves the record's
    # path and then writes it back, and a status change moving that file in
    # between would send the edit to a path that no longer exists (or, worse,
    # recreate one).
    with project_write_lock(config):
        return _update_section_locked(config, project_id, section, mode, content, if_match)


def _update_section_locked(config: Config, project_id: str, section: str, mode: str,
                            content: str, if_match: str | None) -> Path:
    """`update_section`'s body, under the write lock. Never call directly."""
    # project_id may be an alias — resolve to the canonical id before any
    # file lookup, same discipline as set_project_status.
    requested_id = project_id
    entry = find_project(config, project_id)
    if entry is not None:
        project_id = entry.id

    try:
        path = resolve_write_target(config, project_id)
    except FileNotFoundError:
        raise SectionEditError(f"no project record with id or alias '{requested_id}'") from None
    note = frontmatter.parse_file(path)
    if note.type != "project":
        raise SectionEditError(f"'{project_id}' is not a project record (type={note.type!r})")

    principal_id = config.acting_principal
    if not visibility.is_owner(config, principal_id, note.meta):
        current_owner, _ = visibility.owner_and_audience(note.meta)
        audit.log_event(config, event="note.write.denied", principal_id=principal_id,
                         **audit.caller_provenance(config),
                         detail=f"id={project_id} owner={current_owner}")
        raise SectionEditError(
            f"only '{current_owner}' (this record's current owner) may edit its sections "
            f"— '{principal_id}' is not permitted"
        )

    lines = note.body.splitlines(keepends=True)
    start, end = _section_span(lines, section)
    current_block = "".join(lines[start:end])

    if if_match is not None:
        actual_hash = hashlib.sha256(current_block.encode("utf-8")).hexdigest()
        if actual_hash != if_match:
            raise SectionEditError(
                f"section content changed since it was last read (expected hash {if_match}, "
                f"got {actual_hash}) — re-read the section and retry"
            )

    content = content.rstrip("\n")
    if mode == "replace":
        new_block = f"{content}\n\n" if content else "\n"
    else:
        existing = current_block.rstrip("\n")
        new_block = f"{existing}\n\n{content}\n\n" if existing else f"{content}\n\n"

    note.body = "".join(lines[:start]) + new_block + "".join(lines[end:])
    note.meta["updated"] = dt.date.today().isoformat()
    path.write_text(frontmatter.render(note), encoding="utf-8")
    indexer.index_note(config, path)
    audit.log_event(config, event="note.write", principal_id=config.acting_principal,
                     **audit.caller_provenance(config),
                     detail=f"id={project_id} section={section}")
    return path
