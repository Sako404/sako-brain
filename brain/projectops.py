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

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import audit
from . import frontmatter
from . import indexer
from . import visibility
from .paths import Config
from .registry import find_project, load_registry
from .update import find_note_path

_ENTRY_RE = re.compile(r"^  - id: (\S+)\s*$")


class ProjectWriteError(ValueError):
    """A project create/status-change request that cannot be applied safely."""


@dataclass
class StatusChangeResult:
    id: str
    old_status: str
    new_status: str
    old_path: Path
    new_path: Path
    moved: bool
    registry_updated: bool


def _dump_registry_entry(entry: dict) -> str:
    lines = yaml.safe_dump(entry, sort_keys=False, allow_unicode=True).rstrip("\n").splitlines()
    out = [f"  - {lines[0]}"]
    out.extend(f"    {line}" for line in lines[1:])
    return "\n".join(out) + "\n"


def add_registry_entry(config: Config, entry: dict) -> None:
    """Append one entry to `_registry.yaml`. Every existing byte — including
    the header comments — is left untouched; this only ever adds lines."""
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

    existing = load_registry(config)
    if any(e.id == id for e in existing):
        raise ProjectWriteError(f"registry already has an entry with id '{id}'")
    norm_path = str(Path(path))
    if any(e.path and str(Path(e.path)) == norm_path for e in existing):
        raise ProjectWriteError(f"registry already has an entry with path '{path}'")

    folder = config.taxonomy.folder_for_status(status)
    dest = config.projects_dir / folder / f"{id}.md"
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
                     client_id=config.caller_client, transport=config.caller_transport,
                     detail=f"id={id}")
    return dest


def set_project_status(config: Config, project_id: str, new_status: str) -> StatusChangeResult:
    """Move a project record between status folders and keep its frontmatter
    and registry entry in sync — the one thing plain `brain update` cannot do,
    because it never moves files or touches the registry."""
    allowed = config.vocabulary.statuses_for("project")
    if allowed and new_status not in allowed:
        raise ProjectWriteError(f"status must be one of {allowed}")

    # Resolve once, up front — project_id may be an alias; everything below
    # (file lookup, registry update, the result's own id) must use the one
    # canonical id, never the alias, or the two would silently diverge.
    entry = find_project(config, project_id)
    if entry is None:
        raise ProjectWriteError(f"no project record with id or alias '{project_id}'")
    project_id = entry.id

    old_path = find_note_path(config, project_id)
    if old_path is None:
        raise ProjectWriteError(f"no project record with id '{project_id}'")

    note = frontmatter.parse_file(old_path)
    old_status = note.status or "unknown"

    new_folder = config.taxonomy.folder_for_status(new_status)
    new_path = config.projects_dir / new_folder / old_path.name

    if new_path != old_path and new_path.exists():
        raise ProjectWriteError(f"a file already exists at the target location {new_path}")

    note.meta["status"] = new_status
    note.meta["updated"] = dt.date.today().isoformat()
    old_path.write_text(frontmatter.render(note), encoding="utf-8")

    moved = new_path != old_path
    if moved:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)

    registry_updated = False
    try:
        set_registry_fields(config, project_id, status=new_status, updated=note.meta["updated"])
        registry_updated = True
    except ProjectWriteError:
        registry_updated = False  # no registry entry for this id — reported, not fatal

    indexer.index_note(config, new_path)
    audit.log_event(config, event="note.write", principal_id=config.acting_principal,
                     client_id=config.caller_client, transport=config.caller_transport,
                     detail=f"id={project_id} status={old_status}->{new_status}")
    return StatusChangeResult(
        id=project_id, old_status=old_status, new_status=new_status,
        old_path=old_path, new_path=new_path, moved=moved,
        registry_updated=registry_updated,
    )


def close_project(config: Config, project_id: str, summary: str = "") -> StatusChangeResult:
    """Archive a project: status -> archived, moved + registry-synced, and
    (if given) a closing summary appended via the existing update primitive —
    never invents content, never deletes the record or prior sections."""
    result = set_project_status(config, project_id, "archived")
    if summary:
        from . import update as update_mod
        # result.id is always the canonical id, even when project_id (the
        # caller's argument) was an alias.
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

    # project_id may be an alias — resolve to the canonical id before any
    # file lookup, same discipline as set_project_status.
    entry = find_project(config, project_id)
    if entry is not None:
        project_id = entry.id

    path = find_note_path(config, project_id)
    if path is None:
        raise SectionEditError(f"no project record with id or alias '{project_id}'")
    note = frontmatter.parse_file(path)
    if note.type != "project":
        raise SectionEditError(f"'{project_id}' is not a project record (type={note.type!r})")

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
                     client_id=config.caller_client, transport=config.caller_transport,
                     detail=f"id={project_id} section={section}")
    return path
