"""Project session handoff/resume (Phase 5A).

One evolving Markdown document per project at
`<projects>/<STATUS>/<project-id>-handoff.md`, most-recent-session-first
(like the "next steps log" pattern many working repositories already
use on their own). Each `write()` call
prepends a new dated session section rather than overwriting — full
history stays in one file, plus git tracks every version anyway.

Only meaningful project work should produce a handoff — that judgement is
the /handoff skill's job, not this module's; this module just renders and
locates the document correctly once a Claude session decides to write one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .paths import Config
from .registry import load_registry

HANDOFF_TAG = "handoff"


class HandoffError(RuntimeError):
    pass


@dataclass
class HandoffSections:
    attempted: str = ""
    changed: str = ""
    working_state: str = ""
    unresolved: str = ""
    next_action: str = ""
    files_changed: list = field(default_factory=list)
    decisions: list = field(default_factory=list)


def _registry_entry(config: Config, project_id: str):
    entries = {e.id: e for e in load_registry(config)}
    entry = entries.get(project_id)
    if not entry:
        raise HandoffError(f"no registered project with id '{project_id}'")
    return entry


def handoff_path(config: Config, project_id: str) -> Path:
    entry = _registry_entry(config, project_id)
    folder = config.taxonomy.folder_for_status(entry.status)
    return config.projects_dir / folder / f"{project_id}-handoff.md"


def _render_session(sections: HandoffSections, session_date: str) -> str:
    files = "\n".join(f"- {f}" for f in sections.files_changed) or "- (none noted)"
    decisions = "\n".join(f"- {d}" for d in sections.decisions) or "- (none noted)"
    return (
        f"## Session {session_date}\n\n"
        f"### What was attempted\n\n{sections.attempted or '(not noted)'}\n\n"
        f"### What changed\n\n{sections.changed or '(not noted)'}\n\n"
        f"### Current working state\n\n{sections.working_state or '(not noted)'}\n\n"
        f"### Unresolved issues\n\n{sections.unresolved or '(none noted)'}\n\n"
        f"### Next logical action\n\n{sections.next_action or '(not noted)'}\n\n"
        f"### Files materially changed\n\n{files}\n\n"
        f"### Relevant Brain decisions\n\n{decisions}\n"
    )


def _frontmatter(project_id: str, entry, today: str) -> str:
    return (
        "---\n"
        f"id: handoff-{project_id}\n"
        "type: document\n"
        "status: current\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        "people: []\n"
        f"projects: [{project_id}]\n"
        f"tags: [{HANDOFF_TAG}]\n"
        "sensitivity: normal\n"
        "source: claude-session\n"
        f"source_date: {today}\n"
        "confidence: fact\n"
        "aliases: []\n"
        "---\n"
    )


PROSE_FIELDS = ("attempted", "changed", "working_state", "unresolved", "next_action")


def write(config: Config, project_id: str, sections: HandoffSections,
          session_date: str | None = None) -> Path:
    # A handoff whose every prose field is blank renders as five "(not noted)"
    # headings — it looks like a written handoff and carries nothing, which is
    # worse than no handoff at all because the next session trusts it. Refuse
    # rather than write one; the caller has the content and can pass it.
    if not any(str(getattr(sections, field, "") or "").strip() for field in PROSE_FIELDS):
        raise HandoffError(
            "refusing to write an empty handoff — at least one of "
            f"{', '.join(PROSE_FIELDS)} must have content"
        )

    entry = _registry_entry(config, project_id)
    today = session_date or date.today().isoformat()
    path = handoff_path(config, project_id)

    new_section = _render_session(sections, today)

    if path.exists():
        text = path.read_text(encoding="utf-8")
        if "\n---\n" in text[3:]:
            # Split off frontmatter, keep it (refresh `updated` date only),
            # prepend the new session under the title.
            head, _, body = text.partition("\n---\n")
            fm = head + "\n---\n"
            fm = _touch_updated(fm, today)
            title_line, _, rest = body.lstrip("\n").partition("\n")
            new_text = fm + "\n" + title_line + "\n\n" + new_section + "\n" + rest.lstrip("\n")
        else:
            new_text = _frontmatter(project_id, entry, today) + f"\n# Handoff — {entry.name}\n\n" + new_section + "\n" + text
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_text = _frontmatter(project_id, entry, today) + f"\n# Handoff — {entry.name}\n\n" + new_section

    path.write_text(new_text, encoding="utf-8")
    return path


def _touch_updated(frontmatter_block: str, today: str) -> str:
    lines = frontmatter_block.splitlines()
    out = []
    for line in lines:
        if line.startswith("updated:"):
            out.append(f"updated: {today}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def read_latest(config: Config, project_id: str) -> str | None:
    """Just the most recent session's section text, for /resume."""
    path = handoff_path(config, project_id)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    _, _, body = text.partition("\n---\n")
    if not body:
        return None
    _, _, after_title = body.lstrip("\n").partition("\n")
    marker = "\n## Session "
    first = after_title.find("## Session ")
    if first == -1:
        return None
    rest = after_title[first:]
    next_marker = rest.find(marker, len(marker))
    return rest if next_marker == -1 else rest[:next_marker]


def has_handoff(config: Config, project_id: str) -> bool:
    return handoff_path(config, project_id).exists()


def list_projects_with_handoffs(config: Config) -> list[str]:
    return [e.id for e in load_registry(config) if handoff_path(config, e.id).exists()]
