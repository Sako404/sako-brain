"""Update an existing note in place — used by update_memory (MCP) and /remember.

Never rewrites history: this only sets/merges frontmatter fields and can
append to the body. It does not delete existing body content.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import audit
from . import frontmatter
from . import indexer
from . import visibility
from .paths import Config


class UpdateError(ValueError):
    """An update that cannot be applied safely — e.g. a modification
    attempted by someone other than the record's current owner."""


# Stage 2 V1 write rule, locked per Marcin's own explicit decision:
# owner_principal may modify a record; audience grants READ visibility
# only. update_memory refuses ANY change (not just owner_principal/
# audience) from a non-owner — audience membership alone must never grant
# edit/update/delete rights, and this is the one place every caller
# (brain update, brain project update, the update_memory MCP tool)
# ultimately goes through. _SHARING_FIELDS is kept separate only to label
# the audit event distinctly (note.sharing_change.denied vs
# note.write.denied) — both are refused the same way.
_SHARING_FIELDS = {"owner_principal", "audience"}


class AmbiguousNoteIdError(UpdateError):
    """Two or more notes on disk claim the same id, so a write addressed by
    that id has no single correct target.

    `brain doctor` reports duplicate IDs. Writes must refuse ambiguity:
    selecting a different copy for each operation would split a record's
    history. A synthetic example is a handoff copied into two status folders.
    Writes fail closed here instead.

    The message carried to the caller is deliberately contentless. The first
    version of this guard named every colliding path and their count, which
    an independent review reproduced as an information leak: one of the
    copies can belong to another principal and be invisible to the caller,
    and the refusal was announcing its path. Ambiguity has to be detected
    before the ACL check (a hidden copy must still block the write — picking
    the visible one is exactly the history-splitting bug), so the only thing
    that can be fixed is what gets *said*: the caller learns that the id is
    not writable, and nothing else. Paths, counts and owners go to the
    operator's own audit log, which lives outside the vault at 0600 (see
    `audit.log_event` and `paths.ensure_private_file`)."""


def find_note_paths(config: Config, note_id: str) -> list[Path]:
    """Every file claiming `note_id`, in a deterministic order. Normally
    one; more than one is a structural fault, not something to resolve by
    preference."""
    found = []
    for path in frontmatter.iter_markdown_files(config.brain_root, config.content_dirs):
        try:
            note = frontmatter.parse_file(path)
        except frontmatter.FrontmatterError:
            continue
        if note.id == note_id:
            found.append(path)
    return found


def find_note_path(config: Config, note_id: str) -> Path | None:
    """First match — the long-standing permissive resolver, unchanged.

    Read paths keep using this on purpose: a duplicate id must not make a
    record unreadable, and `read_visible_note_text`'s own contract (one
    identical FileNotFoundError for missing / malformed / denied) has no
    room for a fourth outcome. Write paths use `resolve_write_target`.
    """
    paths = find_note_paths(config, note_id)
    return paths[0] if paths else None


# One wording, used by every write-by-id path, so no caller can accidentally
# ship a chattier version of it.
#
# The missing-note wording is here too, and is shared deliberately: a caller
# who can see NONE of several colliding copies must get the identical answer
# a nonexistent id gets. Otherwise the refusal itself is the signal — an
# independent review (synthetic regression) showed that an unknown id raised
# FileNotFoundError while an id held by three invisible copies raised the
# ambiguity error, so the pair of messages told a stranger that the record
# exists. Same text, same exception type, no distinction to read.
MISSING_ID_MESSAGE = "no note with id '{note_id}'"

AMBIGUOUS_ID_MESSAGE = (
    "id {note_id!r} does not resolve to a single record, so this write is refused: "
    "writing to one of several files that claim one id is what silently splits a "
    "record's history. Nothing was modified. A vault admin can find and reconcile "
    "the duplicates with `brain doctor` (duplicate_ids); the details are in the "
    "server's own audit log. Reads are unaffected."
)


def resolve_write_target(config: Config, note_id: str,
                          principal_id: str | None = None, *,
                          denial_event: str = "note.write.denied") -> Path:
    """The resolver every write-by-id path must use: exactly one target, or a
    refusal that says only that.

    Ambiguity is checked before anything else, including ownership — a copy
    the caller cannot see must still block the write, because quietly
    choosing the visible one is the original bug. The candidates themselves
    are never put in the exception; they are audited instead.
    """
    if principal_id is None:
        principal_id = config.acting_principal
    missing = MISSING_ID_MESSAGE.format(note_id=note_id, root=config.brain_root)

    paths = find_note_paths(config, note_id)
    if not paths:
        raise FileNotFoundError(missing)
    if len(paths) > 1:
        audit_ambiguous_id(config, note_id, paths, principal_id)
        # Visible to this caller or not, the write is refused — a hidden copy
        # must still block it. The only question is which refusal is honest
        # to send back: a caller who can see at least one copy already knows
        # the record exists, so the ambiguity error tells them nothing new. A
        # caller who can see none of them learns nothing at all.
        if not visibility.visible_note_paths(config, principal_id, note_id):
            raise FileNotFoundError(missing)
        raise AmbiguousNoteIdError(AMBIGUOUS_ID_MESSAGE.format(note_id=note_id))
    if not visibility.visible_note_paths(config, principal_id, note_id):
        meta = frontmatter.parse_file(paths[0]).meta
        try:
            owner, _ = visibility.owner_and_audience(meta)
        except visibility.VisibilityError:
            owner = "(malformed)"
        audit.log_event(config, event=denial_event, principal_id=principal_id,
                        **audit.caller_provenance(config),
                        detail=f"id={note_id} owner={owner}")
        raise FileNotFoundError(missing)
    return paths[0]


def audit_ambiguous_id(config: Config, note_id: str, paths: list[Path],
                        principal_id: str) -> None:
    """The operator-facing half of an ambiguity refusal: which files, and how
    many, into the 0600 audit log outside the vault. Shared so every write
    path that can hit this records it identically."""
    try:
        relative = ",".join(sorted(str(p.relative_to(config.brain_root)) for p in paths))
    except ValueError:  # a path outside the vault root — log the names only
        relative = ",".join(sorted(p.name for p in paths))
    audit.log_event(
        config, event="note.write.ambiguous_id", principal_id=principal_id,
        **audit.caller_provenance(config),
        detail=f"id={note_id} copies={len(paths)} paths={relative}",
    )


def update_memory(config: Config, note_id: str, set_fields: dict | None = None,
                   append_text: str | None = None, principal_id: str | None = None) -> Path:
    from .projectops import project_write_lock

    with project_write_lock(config):
        return _update_memory_locked(config, note_id, set_fields, append_text, principal_id)


def _update_memory_locked(config: Config, note_id: str, set_fields: dict | None,
                          append_text: str | None, principal_id: str | None) -> Path:
    if principal_id is None:
        principal_id = config.acting_principal
    set_fields = set_fields or {}
    is_sharing_change = bool(_SHARING_FIELDS & set_fields.keys())
    path = resolve_write_target(
        config, note_id, principal_id,
        denial_event="note.sharing_change.denied" if is_sharing_change else "note.write.denied",
    )
    note = frontmatter.parse_file(path)
    if not visibility.is_owner(config, principal_id, note.meta):
        current_owner, _ = visibility.owner_and_audience(note.meta)
        event = "note.sharing_change.denied" if is_sharing_change else "note.write.denied"
        audit.log_event(
            config, event=event, principal_id=principal_id,
            **audit.caller_provenance(config),
            detail=f"id={note_id} owner={current_owner}",
        )
        raise UpdateError(
            f"only '{current_owner}' (this record's current owner) may modify it "
            f"— '{principal_id}' is not permitted"
        )

    for key, value in set_fields.items():
        if key in {"id", "created"}:
            continue  # never mutate identity or original creation date
        note.meta[key] = value

    note.meta["updated"] = dt.date.today().isoformat()

    if append_text:
        stamp = dt.date.today().isoformat()
        note.body = note.body.rstrip("\n") + f"\n\n## Update ({stamp})\n\n{append_text}\n"

    path.write_text(frontmatter.render(note), encoding="utf-8")
    indexer.index_note(config, path)

    event = "note.sharing_change" if is_sharing_change else "note.write"
    audit.log_event(
        config, event=event, principal_id=principal_id,
        **audit.caller_provenance(config), detail=f"id={note_id}",
    )
    return path
