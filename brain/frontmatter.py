"""Parse and render YAML-frontmatter Markdown notes."""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import os
import stat
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_DELIM = "---"

# A UTF-8 byte-order mark decodes to this single character. Editors on other
# platforms add it routinely, and it is not part of the document — but it sits
# in front of the opening `---`, so a strict `startswith` check calls a
# perfectly good note malformed. Worse, in `listing.py`'s classifier it looked
# like "no frontmatter at all", i.e. a raw capture, so the note vanished from
# a listing that still reported itself complete (found by an independent
# review). Stripped at the one place every caller decodes.
BOM = "\ufeff"


class FrontmatterError(ValueError):
    """Raised when a note's frontmatter cannot be parsed — including when the
    file is not decodable text at all. A caller that handles malformed
    frontmatter is handling the same class of problem as a file with a stray
    byte in it, and should not have to catch `UnicodeDecodeError` separately
    to avoid taking a whole scan down with it."""


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
    if text.startswith(BOM):
        text = text[len(BOM):]
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
    """Decode and parse one note.

    `utf-8-sig` rather than `utf-8`: identical for ordinary files, and it
    drops a leading BOM so a note written by a BOM-adding editor is an
    ordinary note rather than an invisible one (see `BOM` above).

    A file that is not decodable at all raises `FrontmatterError`, not
    `UnicodeDecodeError`. Before this, one stray byte anywhere in any note
    took down every whole-vault scan that only caught `FrontmatterError` —
    `brain list`, `brain doctor` and `brain index` among them — instead of
    being reported as the one unreadable file it is.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FrontmatterError(f"{path}: not decodable as UTF-8 text — {exc}") from exc
    return parse_text(text, path)


def render(note: Note) -> str:
    yaml_block = yaml.safe_dump(note.meta, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"{FRONTMATTER_DELIM}\n{yaml_block}\n{FRONTMATTER_DELIM}\n\n{note.body.lstrip(chr(10))}"


def diagnostic_metadata(path: Path) -> dict | None:
    """ACL evidence for a file that could not be parsed in full.

    Read only the header, as bytes: undecodable body text must not discard
    a perfectly readable ACL. Raw captures use the normal missing-owner
    default. A malformed header uses that default only when YAML's scanner
    establishes that it contains no ACL scalar; otherwise ownership is
    unknown and no caller receives counters for it. This is diagnostic
    evidence only, never authorization to read or modify the file.
    """
    try:
        with path.open("rb") as handle:
            first = handle.readline().decode("utf-8-sig")
            if first.strip() != FRONTMATTER_DELIM:
                return {}
            lines = []
            for line in handle:
                decoded = line.decode("utf-8")
                if decoded.strip() == FRONTMATTER_DELIM:
                    break
                lines.append(decoded)
            raw = "".join(lines)
        try:
            meta = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            # ScalarToken values also decode quoted/escaped YAML keys. A
            # text search for owner_principal would miss those and leak.
            if any(isinstance(token, yaml.tokens.ScalarToken)
                   and token.value in {"owner_principal", "audience", "<<"}
                   for token in yaml.scan(raw)):
                return None
            return {}
        return meta if isinstance(meta, dict) else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def escapes_root(path: Path, root: Path) -> bool:
    """True when `path` resolves outside `root` — i.e. it is a symlink (or
    sits under one) that leaves the vault.

    The scan boundary is the vault, not "whatever the vault's directories
    happen to point at". File symlinks are followed when opened, so a link
    dropped into a content directory pulled an outside file into listings,
    the search index and `brain doctor`'s own integrity checks, with its
    content and ACL coming from a file nobody managing the vault can see.
    Made explicit here, once, for every caller — and surfaced to the
    operator by `validate.check_symlink_escapes_vault` rather than only
    skipped, so a vault that has one is told about it.
    """
    try:
        if path.is_symlink() and not path.exists():
            return True
        return not path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        # A broken or looping link resolves to nothing usable; treat it as
        # outside, which is the fail-closed answer.
        return True


def is_memory_path(path: Path, root: Path, *, on_error=None) -> bool:
    """Resolve aliases before enforcing the ordinary-memory boundary.

    Identity APIs intentionally read their own protected tree directly;
    memory discovery, cached reads and writes must never follow an alias
    into it. Check both the canonical location and its resolved target.
    """
    if on_error is not None:
        try:
            # Non-strict resolution can suppress access errors. Listing must
            # distinguish an excluded alias from an unverified source path.
            path.resolve(strict=True)
        except FileNotFoundError:
            return False  # broken links are outside the discovery contract
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return False  # a verified looping link is excluded by policy
            on_error(exc)
            return False
        except RuntimeError:
            return False  # looping links are excluded too
    if escapes_root(path, root):
        return False
    try:
        target = path.resolve()
        protected = root.resolve() / "90_SYSTEM" / "identity"
        if target.is_relative_to(protected):
            return False
        try:
            protected_target = protected.resolve()
        except (OSError, RuntimeError):
            # A broken identity-tree link must not hide unrelated content.
            return True
        return not target.is_relative_to(protected_target)
    except (OSError, RuntimeError):
        return False


def _report_uncovered_dir_link(link: Path, root: Path, content_dirs: tuple[str, ...],
                               on_error) -> None:
    """A directory link is never traversed. That loses nothing when its target
    is already scanned under its real path, and is policy when the target is
    outside the vault, protected identity state, broken or looping. Any other
    in-vault target holds notes this scan does not reach, so the caller must
    not report the source as complete."""
    if on_error is None or not is_memory_path(link, root):
        return
    try:
        target = link.resolve(strict=True)
    except (OSError, RuntimeError):
        return
    for dirname in content_dirs:
        base = root / dirname
        if base.is_symlink():
            continue
        try:
            if target.is_relative_to(base.resolve()):
                return
        except (OSError, RuntimeError):
            continue
    on_error(OSError(errno.ENOTDIR, "directory link to unscanned vault content not traversed"))


def iter_markdown_files(root: Path, content_dirs: tuple[str, ...], *, on_error=None):
    """Every Markdown file the vault itself contains, in a deterministic
    order. Files outside the vault or resolving into protected identity
    state are not ordinary memory and are never yielded. Directory links
    are not traversed. A completeness-sensitive caller must supply
    `on_error`: pathlib globbing silently suppresses traversal failures.
    The callback receives an OSError for diagnostics, never public output.
    Missing configured directories are normal in a partially populated vault.
    """
    for dirname in content_dirs:
        base = root / dirname
        try:
            mode = base.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            if on_error is not None:
                on_error(exc)
            continue
        if stat.S_ISLNK(mode):
            _report_uncovered_dir_link(base, root, content_dirs, on_error)
            continue
        if not is_memory_path(base, root, on_error=on_error):
            continue
        candidates = []
        for current, dirs, files in os.walk(base, onerror=on_error, followlinks=False):
            parent = Path(current)
            for d in dirs:
                if (parent / d).is_symlink():
                    _report_uncovered_dir_link(parent / d, root, content_dirs, on_error)
            dirs[:] = sorted(d for d in dirs
                             if not (parent / d).is_symlink()
                             and is_memory_path(parent / d, root, on_error=on_error))
            candidates.extend(parent / name for name in files
                              if name.endswith(".md")
                              and is_memory_path(parent / name, root, on_error=on_error))
        for path in sorted(candidates):
            yield path


def iter_external_links(root: Path, content_dirs: tuple[str, ...]):
    """Report excluded links without ever descending into outside content.

    Directory symlinks are not traversed (including internal ones); internal
    file aliases remain candidates. Broken and looping links are excluded.
    """
    for dirname in content_dirs:
        base = root / dirname
        if not is_memory_path(base, root):
            yield base
            continue
        if base.is_symlink():
            continue
        if not base.exists():
            continue
        for current, dirs, files in os.walk(base, followlinks=False):
            for name in sorted(dirs + files):
                path = Path(current) / name
                if path.is_symlink() and not is_memory_path(path, root):
                    yield path
