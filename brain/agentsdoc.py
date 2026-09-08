"""Render a vault's AGENTS.md from a template (OSS-2/C7).

AGENTS.md is arguably the most valuable single file in a release: the data
model, the memory rules, the three-level write policy and the sensitivity
model, provider-neutral, in one readable page. It was also one of the most
personal — it hardcoded one vault's path, one backup destination, one projects
root and one person's list of life areas.

It is now generated. Everything the document says about *this* vault comes
from `Config`, so a remapped taxonomy, a second projects root or a different
tool name produce a correct document rather than a stale one.

`brain agents-doc` prints it; `--write` installs it at the vault root. A
future `brain init` calls `render()` for a new vault.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from . import paths as paths_mod
from .paths import CONTENT_ROLES, Config

TEMPLATE_PACKAGE = "brain"
TEMPLATE_RELPATH = "templates/AGENTS.md.template"


def template_text() -> str:
    """The shipped AGENTS.md template, read from the installed package.

    `importlib.resources` rather than a path relative to this file (OSS-4):
    once installed, the template is package *data*, and the only supported way
    to reach it is through the loader that owns the package. A source-relative
    path happens to work for an ordinary wheel and fails for anything else —
    and, more to the point, nothing would have noticed if the build had simply
    not shipped the file. `[tool.setuptools.package-data]` declares it, and an
    installed-wheel test proves it travelled.
    """
    return files(TEMPLATE_PACKAGE).joinpath(TEMPLATE_RELPATH).read_text(encoding="utf-8")

OUTPUT_FILENAME = "AGENTS.md"

# What each role's directory is *for*. Generic by design — the wording must
# hold for any vault, since only the directory names are configurable.
ROLE_PURPOSE = {
    "inbox": "Unsorted capture, triaged later. `{pending}` is the Level-2 pending-memory queue — not authoritative notes, review via `brain memory`.",
    "people": "Person records",
    "areas": "Ongoing life areas{areas}",
    "projects": "Project records, split into `{folders}/`, plus `{registry}`",
    "decisions": "Decision records (immutable once made; superseded, not edited)",
    "timeline": "Dated event entries",
    "knowledge": "Reference / evergreen knowledge notes",
    "documents": "Records *about* documents (metadata, not the binary itself, unless small and personal)",
    "templates": "Templates for every entity type",
}

SYSTEM_PURPOSE = (
    "The `brain` CLI/tooling, this vault's `config.yaml`, backup script. "
    "**No runtime state** — the index, logs and integrity manifests live "
    'outside the vault (see "Search index").'
)


def _project_status_folders(config: Config) -> list[str]:
    """Distinct registry folders, in the order the status vocabulary lists them."""
    folders = []
    for status in config.vocabulary.statuses_for("project") or ():
        folder = config.taxonomy.folder_for_status(status)
        if folder not in folders:
            folders.append(folder)
    return folders or [paths_mod.FALLBACK_PROJECT_FOLDER]


def _directory_map(config: Config) -> str:
    pending = f"{config.taxonomy.directory('inbox')}/memory/pending.yaml"
    areas = config.areas
    areas_suffix = f" ({', '.join(areas)})" if areas else ""

    rows = ["| Path | Purpose |", "|---|---|"]
    for role in (*CONTENT_ROLES, "templates"):
        purpose = ROLE_PURPOSE[role].format(
            pending=pending,
            areas=areas_suffix,
            folders="/".join(_project_status_folders(config)),
            registry=paths_mod.REGISTRY_FILENAME,
        )
        rows.append(f"| `{config.taxonomy.directory(role)}/` | {purpose} |")
    rows.append(f"| `{paths_mod.SYSTEM_DIRNAME}/` | {SYSTEM_PURPOSE} |")
    return "\n".join(rows)


def _status_by_type(config: Config) -> str:
    vocab = config.vocabulary
    archived = config.taxonomy.folder_for_status("completed")
    projects_dir = config.taxonomy.directory("projects")

    lines = ["Allowed `status` values by type:"]
    project_statuses = vocab.statuses_for("project")
    if project_statuses:
        # Statuses that lose their own folder by mapping into the archive
        # bucket. The status that *names* the bucket owns it rather than being
        # collapsed into it, so it is excluded.
        collapsed = [s for s in project_statuses
                     if config.taxonomy.folder_for_status(s) == archived
                     and s.upper() != archived.upper()]
        lines.append("- `project`: " + ", ".join(f"`{s}`" for s in project_statuses))
        if len(collapsed) > 1:
            lines.append(
                "  — " + "/".join(f"`{s}`" for s in collapsed) + " all physically live under\n"
                f"  `{projects_dir}/{archived}/` (there's no separate folder per status), but the\n"
                "  real status stays in frontmatter. Use `unknown` rather than guessing when\n"
                "  evidence doesn't clearly support a specific status."
            )
    for note_type in vocab.note_types:
        if note_type == "project":
            continue
        statuses = vocab.statuses_for(note_type)
        if statuses:
            lines.append(f"- `{note_type}`: " + ", ".join(f"`{s}`" for s in statuses))
    lines.append("- everything else: free-form or omitted")
    return "\n".join(lines)


def _projects_roots(config: Config) -> str:
    roots = config.projects_roots
    if not roots:
        return "the configured projects roots (see `brain status`)"
    return ", ".join(f"`{r}/`" for r in roots)


def _backup_line(config: Config) -> str:
    """Only stated when this vault actually has a plaintext backup mirror."""
    if config.backup_target is None:
        return ""
    return ("- Backup only, never authoritative, never sync source: "
            f"`{config.backup_target}/`.\n")


def render(config: Config, template_path: Path | None = None) -> str:
    template = template_path.read_text(encoding="utf-8") if template_path else template_text()
    values = {
        "VAULT_NAME": config.vault_name,
        "VAULT_PATH": str(config.brain_root),
        "BACKUP_LINE": _backup_line(config),
        "PROJECTS_ROOTS": _projects_roots(config),
        "DIRECTORY_MAP": _directory_map(config),
        "NOTE_TYPES_PIPE": "|".join(config.vocabulary.note_types),
        "STATUS_BY_TYPE": _status_by_type(config),
        "TIMELINE_DIR": config.taxonomy.directory("timeline"),
        "DECISIONS_DIR": config.taxonomy.directory("decisions"),
        "PROJECTS_DIR": config.taxonomy.directory("projects"),
        "ARCHIVED_PROJECTS_DIR": (
            f"{config.taxonomy.directory('projects')}/"
            f"{config.taxonomy.folder_for_status('completed')}"
        ),
        "PENDING_QUEUE": f"{config.taxonomy.directory('inbox')}/memory/pending.yaml",
        "APP_DIRNAME": paths_mod.APP_DIRNAME,
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)

    unresolved = [line for line in template.splitlines() if "{{" in line]
    if unresolved:
        raise ValueError(f"unresolved placeholders in AGENTS.md template: {unresolved}")
    return template


def output_path(config: Config) -> Path:
    return config.brain_root / OUTPUT_FILENAME
