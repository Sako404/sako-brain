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


def find_note_path(config: Config, note_id: str) -> Path | None:
    for path in frontmatter.iter_markdown_files(config.brain_root, config.content_dirs):
        try:
            note = frontmatter.parse_file(path)
        except frontmatter.FrontmatterError:
            continue
        if note.id == note_id:
            return path
    return None


def update_memory(config: Config, note_id: str, set_fields: dict | None = None,
                   append_text: str | None = None, principal_id: str | None = None) -> Path:
    path = find_note_path(config, note_id)
    if not path:
        raise FileNotFoundError(f"no note with id '{note_id}' found under {config.brain_root}")

    note = frontmatter.parse_file(path)
    set_fields = set_fields or {}
    if principal_id is None:
        principal_id = config.acting_principal

    is_sharing_change = bool(_SHARING_FIELDS & set_fields.keys())
    if not visibility.is_owner(config, principal_id, note.meta):
        current_owner, _ = visibility.owner_and_audience(note.meta)
        event = "note.sharing_change.denied" if is_sharing_change else "note.write.denied"
        audit.log_event(
            config, event=event, principal_id=principal_id,
            client_id=config.caller_client, transport=config.caller_transport,
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
        client_id=config.caller_client, transport=config.caller_transport, detail=f"id={note_id}",
    )
    return path
