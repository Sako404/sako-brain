"""Parse and render YAML-frontmatter Markdown notes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_DELIM = "---"


class FrontmatterError(ValueError):
    """Raised when a note's frontmatter cannot be parsed."""


@dataclass
class Note:
    path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def id(self) -> str | None:
        return self.meta.get("id")

    @property
    def type(self) -> str | None:
        return self.meta.get("type")

    @property
    def status(self) -> str | None:
        return self.meta.get("status")

    @property
    def title(self) -> str:
        for line in self.body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return self.id or self.path.stem

    def list_field(self, name: str) -> list[str]:
        value = self.meta.get(name) or []
        if isinstance(value, str):
            return [value]
        return list(value)


def parse_text(text: str, path: Path) -> Note:
    if not text.startswith(FRONTMATTER_DELIM):
        raise FrontmatterError(f"{path}: missing YAML frontmatter (no leading '---')")

    parts = text.split("\n", 1)[1:]
    rest = parts[0] if parts else ""
    end_marker = f"\n{FRONTMATTER_DELIM}"
    idx = rest.find(end_marker)
    if idx == -1:
        raise FrontmatterError(f"{path}: unterminated YAML frontmatter block")

    raw_yaml = rest[:idx]
    body = rest[idx + len(end_marker):]
    if body.startswith("\n"):
        body = body[1:]

    try:
        meta = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"{path}: invalid YAML — {exc}") from exc

    if not isinstance(meta, dict):
        raise FrontmatterError(f"{path}: frontmatter must be a YAML mapping")

    return Note(path=path, meta=meta, body=body)


def parse_file(path: Path) -> Note:
    text = path.read_text(encoding="utf-8")
    return parse_text(text, path)


def render(note: Note) -> str:
    yaml_block = yaml.safe_dump(note.meta, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"{FRONTMATTER_DELIM}\n{yaml_block}\n{FRONTMATTER_DELIM}\n\n{note.body.lstrip(chr(10))}"


def iter_markdown_files(root: Path, content_dirs: tuple[str, ...]):
    for dirname in content_dirs:
        base = root / dirname
        if not base.exists():
            continue
        yield from sorted(base.rglob("*.md"))
