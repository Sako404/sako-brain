"""Project registry (`<projects>/_registry.yaml`) loading and checks."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from . import frontmatter
from . import visibility
from .paths import Config

@dataclass
class ProjectEntry:
    id: str
    name: str
    path: str
    status: str
    category: str | None = None
    created: str | None = None
    updated: str | None = None
    aliases: list | None = None


def _str_or_none(value) -> str | None:
    """YAML auto-types an unquoted date-like scalar (`created: 2026-01-15`)
    as a real `datetime.date`, not a string — common in registries written
    by hand. `ProjectEntry.created`/`updated` are documented as `str | None`;
    coerce here so every caller (including JSON serialization, e.g.
    `brain state`) gets what the type actually promises."""
    return None if value is None else str(value)


def load_registry(config: Config) -> list[ProjectEntry]:
    if not config.registry_path.exists():
        return []
    with open(config.registry_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    entries = []
    for p in data.get("projects", []) or []:
        entries.append(ProjectEntry(
            id=p.get("id"),
            name=p.get("name"),
            path=p.get("path"),
            status=p.get("status"),
            category=p.get("category"),
            created=_str_or_none(p.get("created")),
            updated=_str_or_none(p.get("updated")),
            aliases=p.get("aliases") or [],
        ))
    return entries


def _project_note_visible(config: Config, principal_id: str, entry: ProjectEntry) -> bool:
    """Stage 2: a registry entry's visibility is decided by its OWN backing
    project note's owner_principal/audience — the registry.yaml row itself
    carries no such field. An entry with no resolvable backing note (a
    stale/orphaned registry row — `brain doctor` already flags these
    separately) fails closed, the same "a broken record never becomes an
    open one" rule applied everywhere else in this layer."""
    from .update import find_note_path  # local import: avoids a cycle risk

    if not entry.id:
        return False
    path = find_note_path(config, entry.id)
    if path is None:
        return False
    try:
        note = frontmatter.parse_file(path)
    except frontmatter.FrontmatterError:
        return False
    return visibility.can_view_note(config, principal_id, note)


def load_visible_registry(config: Config, principal_id: str | None = None) -> list[ProjectEntry]:
    """THE function every genuine read path (project listing, project
    context, get_context's own project matches) must call — never
    `load_registry` directly, which stays unfiltered on purpose for
    write-path duplicate-id/path checks (brain/projectops.py) and
    `brain doctor`'s integrity scan, both of which need the complete
    picture to do their job correctly regardless of who's asking."""
    if principal_id is None:
        principal_id = config.acting_principal
    return [e for e in load_registry(config) if _project_note_visible(config, principal_id, e)]


def find_visible_project(config: Config, principal_id: str, id_or_alias: str) -> ProjectEntry | None:
    """The authorized counterpart to find_project, for read paths — resolves
    by id OR alias, but only among entries `principal_id` may see."""
    entries = load_visible_registry(config, principal_id)
    for e in entries:
        if e.id == id_or_alias:
            return e
    for e in entries:
        if id_or_alias in (e.aliases or []):
            return e
    return None


def find_project(config: Config, id_or_alias: str) -> ProjectEntry | None:
    """Resolve a project registry entry by its canonical id OR any of its
    declared aliases — the one resolver every project/handoff operation
    should use, so a name that works in one command works in every other
    one that takes a project identifier. Exact-match only (case-sensitive,
    same discipline as id matching everywhere else in this vault) — never
    a fuzzy/substring match, which would make command behavior depend on
    what else happens to be registered. Canonical id takes priority: if
    id_or_alias equals one entry's id, that wins even if it also happens
    to be a different entry's alias — a real id is a stronger claim."""
    entries = load_registry(config)
    for e in entries:
        if e.id == id_or_alias:
            return e
    for e in entries:
        if id_or_alias in (e.aliases or []):
            return e
    return None


def find_duplicates(entries: list[ProjectEntry]) -> list[str]:
    problems = []
    seen_ids: dict[str, int] = {}
    seen_paths: dict[str, int] = {}
    for e in entries:
        seen_ids[e.id] = seen_ids.get(e.id, 0) + 1
        if e.path:
            norm = str(Path(e.path))
            seen_paths[norm] = seen_paths.get(norm, 0) + 1
    for id_, count in seen_ids.items():
        if count > 1:
            problems.append(f"duplicate registry id: {id_} ({count}x)")
    for path, count in seen_paths.items():
        if count > 1:
            problems.append(f"duplicate registry path: {path} ({count}x)")
    return problems
