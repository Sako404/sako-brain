"""Project registry (`<projects>/_registry.yaml`) loading and checks."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

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
            created=p.get("created"),
            updated=p.get("updated"),
            aliases=p.get("aliases") or [],
        ))
    return entries


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
