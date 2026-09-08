"""Build a deterministic synthetic vault (OSS-3).

The portability question OSS-1 and OSS-2 could not answer on their own is
whether the core works for *somebody else* — a different user, different
paths, a different taxonomy, a different vocabulary, none of it derived from
the vault this code happens to live in.

A generator answers that better than a checked-in fixture:

- it exercises the **creation** path, so it proves the core can be brought up
  in an empty directory rather than only read a vault someone already built;
- it cannot go stale silently — it runs whenever the tests run, and fails the
  moment the taxonomy or vocabulary model moves;
- a checked-in demo vault inside a real vault would be walked by
  `brain index` and `brain doctor`, polluting the host vault's note count and
  integrity report. A generator materialises into a throwaway directory.

Deterministic by construction: no clock, no randomness, no host lookups. The
same `today` produces byte-identical output, which is what makes "render twice,
compare" a meaningful assertion.

**This is not `brain init`.** No CLI command is wired to it. Making it the
basis of a future `brain init --demo` is OSS-4 input, recorded and not built.

Everything here is fictional. `Alex Example` is not a real person, and the
paths are whatever temporary directory the caller supplies.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# --- The synthetic identity. Obviously fake, on purpose. -------------------

DEMO_USER = "Alex Example"
DEMO_PERSON_ID = "person-alex-example"
DEMO_VAULT_NAME = "Example Brain"
DEMO_DATE = "2026-01-15"

# --- A taxonomy that deliberately does NOT use the shipped directory names.
#
# The point of OSS-2/C3 was that the code depends on roles, not spellings. A
# demo that reused `30_PROJECTS` would prove nothing about that. `90_SYSTEM`
# is absent because it is the vault marker, not a role.
DEMO_DIRECTORIES = {
    "inbox": "inbox",
    "people": "people",
    "areas": "areas",
    "projects": "work",
    "decisions": "choices",
    "timeline": "log",
    "knowledge": "library",
    "documents": "papers",
    "templates": "patterns",
}

# Remapped too, including a folder for the custom status below. `archive-box`
# stands in for ARCHIVED so the archive-bucket semantics are exercised under a
# different name than production uses.
DEMO_PROJECT_STATUS_FOLDERS = {
    "active": "current",
    "planned": "planned",
    "piloting": "pilot",
    "on-hold": "paused",
    "completed": "archive-box",
    "archived": "archive-box",
    "abandoned": "archive-box",
    "unknown": "archive-box",
}

# One custom note type and one custom project status, to prove the vocabulary
# is configuration. `project` and `decision` stay: the core requires them.
DEMO_NOTE_TYPES = (
    "person", "project", "area", "event", "decision", "fact", "document",
    "knowledge", "experiment",
)
DEMO_PROJECT_STATUSES = (
    "active", "planned", "piloting", "on-hold", "completed", "archived",
    "abandoned", "unknown",
)

DEMO_AREAS = ("Observatory", "Fieldwork")

DEMO_PROJECT_ID = "project-demo-observatory"
DEMO_PROJECT_NAME = "Demo Observatory"
DEMO_ARCHIVED_PROJECT_ID = "project-paper-telescope"
DEMO_ARCHIVED_PROJECT_NAME = "Paper Telescope"
DEMO_DECISION_ID = "decision-use-local-json-fixtures"


@dataclass(frozen=True)
class DemoVault:
    """Where a generated demo vault and its surroundings live."""

    root: Path                      # the vault itself
    project_roots: tuple[Path, ...]  # synthetic working-project directories
    state_dir: Path                 # runtime state — outside the vault
    git_dir: Path                   # git metadata — outside the vault


def _note(*, id: str, type: str, status: str = "", title: str, body: str,
          today: str = DEMO_DATE, people: list[str] | None = None,
          projects: list[str] | None = None, tags: list[str] | None = None,
          extra: dict | None = None) -> str:
    meta = {
        "id": id,
        "type": type,
        "status": status,
        "created": today,
        "updated": today,
        "people": people or [],
        "projects": projects or [],
        "tags": tags or [],
        "sensitivity": "normal",
        "source": "synthetic demo vault",
        "confidence": "fact",
        "aliases": [],
    }
    meta.update(extra or {})
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value if value != '' else ''}")
    lines += ["---", "", f"# {title}", "", body.rstrip(), ""]
    return "\n".join(lines)


def config_data(demo: DemoVault) -> dict:
    """The synthetic vault's config.yaml, as data.

    Every environment-specific value is supplied rather than derived from the
    host, and every OSS-2 configuration surface is exercised: a remapped
    taxonomy, remapped status folders, an extended vocabulary, several project
    roots given as a list, and an explicit external state and git directory.
    """
    return {
        "vault_name": DEMO_VAULT_NAME,
        "areas": list(DEMO_AREAS),
        "projects_roots": [str(p) for p in demo.project_roots],
        "state_dir": str(demo.state_dir),
        "git_dir": str(demo.git_dir),
        "directories": dict(DEMO_DIRECTORIES),
        "project_status_folders": dict(DEMO_PROJECT_STATUS_FOLDERS),
        "note_types": list(DEMO_NOTE_TYPES),
        "status_by_type": {
            "project": list(DEMO_PROJECT_STATUSES),
            "decision": ["proposed", "decided", "superseded"],
            "fact": ["current", "superseded"],
        },
    }


class DemoTargetError(RuntimeError):
    """The requested demo location is not safe to write into."""


def build(root: Path, *, project_roots_parent: Path | None = None,
          external: Path | None = None, today: str = DEMO_DATE,
          seed_content: bool = True) -> DemoVault:
    """Materialise a synthetic vault under `root`. Returns where things landed.

    `external` is where state and git metadata go — outside the vault, as the
    architecture requires. `project_roots_parent` holds the fake working
    project directories the registry points at.

    Both default (OSS-4) so a user-facing `brain init --demo PATH` needs only
    the path: the fake projects sit beside the vault and the state beside them,
    all under one parent, so a demo is one directory tree to delete. Tests keep
    passing them explicitly.

    Refuses a non-empty target, matching `brain init`'s rule that nothing this
    code did not create is ever written to.
    """
    root = Path(root).expanduser()
    if root.exists():
        if not root.is_dir():
            raise DemoTargetError(f"{root} exists and is not a directory")
        if any(root.iterdir()):
            raise DemoTargetError(
                f"{root} is not empty. The demo vault writes a fixed set of "
                "files and will not mix them into existing content — choose an "
                "empty or new directory."
            )
    if not root.parent.exists():
        raise DemoTargetError(f"parent directory does not exist: {root.parent}")

    if project_roots_parent is None:
        project_roots_parent = root.parent / f"{root.name}-projects"
    if external is None:
        external = root.parent / f"{root.name}-state"
    external = Path(external)
    project_roots = (
        Path(project_roots_parent) / "studio",
        Path(project_roots_parent) / "sideline",
    )
    demo = DemoVault(
        root=root,
        project_roots=project_roots,
        state_dir=external / "state",
        git_dir=external / "git" / "example-brain.git",
    )

    # The marker, and nothing else, is what makes this a vault.
    (root / "90_SYSTEM").mkdir(parents=True, exist_ok=True)
    (root / "90_SYSTEM" / "config.yaml").write_text(
        "# Synthetic demo vault — every value here is fictional.\n"
        + yaml.safe_dump(config_data(demo), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Working project directories the registry can legitimately point at.
    # `brain doctor` checks that registered project paths exist, so these are
    # real directories with a plausible file in them — never copies of anything.
    for project_root in project_roots:
        project_root.mkdir(parents=True, exist_ok=True)
    observatory = project_roots[0] / "demo-observatory"
    telescope = project_roots[1] / "paper-telescope"
    for path, blurb in ((observatory, "Demo Observatory"), (telescope, "Paper Telescope")):
        path.mkdir(parents=True, exist_ok=True)
        (path / "README.md").write_text(f"# {blurb}\n\nSynthetic project.\n", encoding="utf-8")

    if not seed_content:
        return demo

    def write(role: str, name: str, text: str, subdir: str = "") -> None:
        base = root / DEMO_DIRECTORIES[role]
        if subdir:
            base = base / subdir
        base.mkdir(parents=True, exist_ok=True)
        (base / name).write_text(text, encoding="utf-8")

    write("people", f"{DEMO_PERSON_ID}.md", _note(
        id=DEMO_PERSON_ID, type="person", title=DEMO_USER, today=today,
        body="The fictional owner of this synthetic vault. Not a real person.",
    ))

    write("areas", "area-observatory.md", _note(
        id="area-observatory", type="area", title="Observatory", today=today,
        body="An ongoing area of interest in the demo.",
    ))

    # Active project, in the folder its status maps to under the demo taxonomy.
    write("projects", f"{DEMO_PROJECT_ID}.md", _note(
        id=DEMO_PROJECT_ID, type="project", status="active",
        title=DEMO_PROJECT_NAME, today=today, people=[DEMO_PERSON_ID],
        extra={"path": str(observatory)},
        body=(
            "A synthetic project used to exercise the project lifecycle.\n\n"
            f"Decision on record: [[{DEMO_DECISION_ID}]]."
        ),
    ), subdir=DEMO_PROJECT_STATUS_FOLDERS["active"])

    # Archived project — proves the archive-bucket semantics under a folder
    # name that is not the production spelling.
    write("projects", f"{DEMO_ARCHIVED_PROJECT_ID}.md", _note(
        id=DEMO_ARCHIVED_PROJECT_ID, type="project", status="completed",
        title=DEMO_ARCHIVED_PROJECT_NAME, today=today,
        extra={"path": str(telescope)},
        body="A finished synthetic project. Its status stays `completed` in "
             "frontmatter while it physically lives in the archive bucket.",
    ), subdir=DEMO_PROJECT_STATUS_FOLDERS["completed"])

    write("decisions", f"{DEMO_DECISION_ID}.md", _note(
        id=DEMO_DECISION_ID, type="decision", status="decided",
        title="Use local JSON fixtures", today=today,
        projects=[DEMO_PROJECT_ID],
        body="The demo records a decision so decision records are exercised.",
    ))

    write("timeline", f"event-{today}-observatory-opened.md", _note(
        id=f"event-{today}-observatory-opened", type="event",
        title="Observatory opened", today=today, projects=[DEMO_PROJECT_ID],
        body="A dated synthetic event.",
    ))

    # Custom note type — only valid because the config extends the vocabulary.
    write("knowledge", "experiment-first-light.md", _note(
        id="experiment-first-light", type="experiment",
        title="First light", today=today,
        body="A note whose type exists only in this vault's configured "
             "vocabulary. Indexing and validation must accept it.",
    ))

    # Knowledge note with real internal links, so link validation has
    # something to resolve rather than an empty set.
    write("knowledge", "knowledge-demo-overview.md", _note(
        id="knowledge-demo-overview", type="knowledge", status="current",
        title="Demo overview", today=today,
        body=(
            "How the pieces relate:\n\n"
            f"- Owner: [[{DEMO_PERSON_ID}]]\n"
            f"- Active project: [[{DEMO_PROJECT_ID}]]\n"
            f"- Archived project: [[{DEMO_ARCHIVED_PROJECT_ID}]]\n"
            f"- Decision: [[{DEMO_DECISION_ID}]]\n"
            "- Custom type: [[experiment-first-light]]\n"
        ),
    ))

    write("documents", "document-observatory-plan.md", _note(
        id="document-observatory-plan", type="document", status="current",
        title="Observatory plan", today=today,
        body="A record *about* a document, not the document itself.",
    ))

    registry = {
        "projects": [
            {
                "id": DEMO_PROJECT_ID, "name": DEMO_PROJECT_NAME,
                "path": str(observatory), "status": "active",
                "category": "demo", "created": today, "updated": today,
                "aliases": ["observatory"],
            },
            {
                "id": DEMO_ARCHIVED_PROJECT_ID, "name": DEMO_ARCHIVED_PROJECT_NAME,
                "path": str(telescope), "status": "completed",
                "category": "demo", "created": today, "updated": today,
                "aliases": [],
            },
        ]
    }
    registry_path = root / DEMO_DIRECTORIES["projects"] / "_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")

    return demo


# Notes the generator seeds, for tests that assert on content rather than count.
SEEDED_NOTE_IDS = (
    DEMO_PERSON_ID,
    "area-observatory",
    DEMO_PROJECT_ID,
    DEMO_ARCHIVED_PROJECT_ID,
    DEMO_DECISION_ID,
    f"event-{DEMO_DATE}-observatory-opened",
    "experiment-first-light",
    "knowledge-demo-overview",
    "document-observatory-plan",
)
