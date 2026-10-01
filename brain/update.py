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
    """An update that cannot be applied safely — e.g. a sharing change
    (owner_principal/audience) attempted by someone other than the
    record's current owner."""


# Stage 2 (multi-user visibility): changing WHO can see a record is a
# privilege that belongs to its owner alone — write access to the record's
# other fields does not imply authority to reshare it. Checked here, the
# one place every caller (brain update, brain project update, the
# update_memory MCP tool) ultimately goes through, rather than in each of
# them separately.
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

    is_sharing_change = bool(_SHARING_FIELDS & set_fields.keys())
    if is_sharing_change:
        if principal_id is None:
            principal_id = config.acting_principal
        current_owner, _ = visibility.owner_and_audience(note.meta)
        if principal_id != current_owner:
            audit.log_event(
                config, event="note.sharing_change.denied", principal_id=principal_id,
                client_id=config.caller_client, transport=config.caller_transport,
                detail=f"id={note_id} owner={current_owner}",
            )
            raise UpdateError(
                f"only '{current_owner}' (this record's current owner) may change its "
                f"owner_principal/audience — '{principal_id}' is not permitted"
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
        config, event=event, principal_id=principal_id or config.acting_principal,
        client_id=config.caller_client, transport=config.caller_transport, detail=f"id={note_id}",
    )
    return path
