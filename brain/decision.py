"""Create (and supersede) decision records.

Extracted from what the `/decision` skill already does today via direct
`Write`/`Edit` on `40_DECISIONS/decision-<date>-<slug>.md`, following the
`80_TEMPLATES/decision.md` shape exactly — this is the same business logic,
not a new design, so a skill migrated onto it produces byte-identical
records to what a human following the skill by hand would write.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import frontmatter
from .capture import slugify
from .paths import Config
from .update import find_note_path

SUPERSEDED_MARKER = "## Superseded by"


class DecisionError(ValueError):
    """A decision create/supersede request that cannot be applied safely."""


def create_decision(config: Config, title: str, context: str = "", options: str = "",
                     decision: str = "", reasoning: str = "", consequences: str = "",
                     status: str = "proposed", people: list[str] | None = None,
                     projects: list[str] | None = None, tags: list[str] | None = None,
                     sensitivity: str = "normal", source: str = "", source_date: str = "",
                     confidence: str = "fact", supersedes: str | None = None,
                     date: str | None = None) -> Path:
    allowed = config.vocabulary.statuses_for("decision")
    if allowed and status not in allowed:
        raise DecisionError(f"status must be one of {allowed}")

    old_path = None
    if supersedes:
        old_path = find_note_path(config, supersedes)
        if old_path is None:
            raise DecisionError(f"supersedes target '{supersedes}' not found")

    today = date or dt.date.today().isoformat()
    slug = slugify(title)
    note_id = f"decision-{today}-{slug}"
    dest = config.dir_for("decisions") / f"{note_id}.md"
    if dest.exists():
        raise DecisionError(f"{dest} already exists")

    meta = {
        "id": note_id, "type": "decision", "status": status,
        "created": today, "updated": today,
        "people": people or [], "projects": projects or [], "tags": tags or [],
        "sensitivity": sensitivity, "source": source, "source_date": source_date,
        "confidence": confidence, "supersedes": supersedes or "", "aliases": [],
    }
    body = (
        f"# Decision: {title}\n\n"
        f"## Context\n\n{context}\n\n"
        f"## Options considered\n\n{options}\n\n"
        f"## Decision\n\n{decision}\n\n"
        f"## Reasoning\n\n{reasoning}\n\n"
        f"## Consequences\n\n{consequences}\n\n"
        f"{SUPERSEDED_MARKER}\n\n"
        "<!-- link a newer decision-<id> here if this one is later superseded; "
        "never edit history in place -->\n"
    )
    note = frontmatter.Note(path=dest, meta=meta, body=body)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(frontmatter.render(note), encoding="utf-8")

    if old_path is not None:
        _mark_superseded(old_path, note_id)

    return dest


def _mark_superseded(old_path: Path, new_id: str) -> None:
    """Set the old decision's status and link forward — never touch its
    Context/Options/Decision/Reasoning: history stays intact."""
    note = frontmatter.parse_file(old_path)
    note.meta["status"] = "superseded"
    note.meta["updated"] = dt.date.today().isoformat()

    link_line = f"\nSuperseded by [[{new_id}]].\n"
    idx = note.body.find(SUPERSEDED_MARKER)
    if idx == -1:
        note.body = note.body.rstrip("\n") + f"\n\n{SUPERSEDED_MARKER}\n{link_line}"
    else:
        after = idx + len(SUPERSEDED_MARKER)
        note.body = note.body[:after] + "\n" + link_line + note.body[after:].lstrip("\n")

    old_path.write_text(frontmatter.render(note), encoding="utf-8")
