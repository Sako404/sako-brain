"""Central path/config resolution for the brain CLI.

Four layers, each able to live somewhere different (OSS-1):

    package (this code)  ->  user configuration  ->  vault  ->  runtime state

**The package never derives the vault from its own location on disk.** It used
to (`__file__` -> 90_SYSTEM -> vault root), which silently made the tool a
component *of* one vault rather than a program that operates *on* a vault.
A deployment wrapper may still say which vault to use — `bin/brain` exports
BRAIN_ROOT — but that is deployment knowledge, not library knowledge.

Every other module accepts an explicit `Config` rather than importing a
constant, so tests can point at a throwaway temp vault and temp state.
Callers that don't care use `default_config()`.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

def ensure_private_file(path: Path) -> None:
    """chmod a just-written file to owner-only (600).

    Phase 5B finding: files created via plain `open(path, "a")` inherit
    the process umask (commonly 022 -> 644, world-readable) regardless of
    how tightly the surrounding directories were chmod'd in Phase 4.2.
    Every log file this package writes calls this explicitly rather than
    relying on umask, which is easy to get wrong and doesn't self-heal if
    it's ever misconfigured.
    """
    try:
        path.chmod(0o600)
    except OSError:
        pass  # best-effort — must never break the caller's actual write


# The vault's system directory, and the relative location of a vault's own
# configuration file inside it. Its *physical* directory is what identifies a
# vault (see `default_config`), which is why this one name is NOT configurable:
# it has to be findable before any configuration has been read.
SYSTEM_DIRNAME = "90_SYSTEM"
VAULT_CONFIG_RELPATH = Path(SYSTEM_DIRNAME) / "config.yaml"


class VaultNotFoundError(RuntimeError):
    """Raised when no vault can be resolved from any configured source.

    Deliberately an error rather than a guess: guessing is how the tool ended
    up operating on the wrong vault in the first place.
    """


def user_config_path() -> Path:
    """Per-user configuration, which may name a default vault."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIRNAME / "config.yaml"


def find_vault_upwards(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default: cwd) looking for a vault marker.

    A directory is a vault if it contains `90_SYSTEM/config.yaml`. Mirrors how
    git finds a repository, and means `brain` works from anywhere inside a
    vault without configuration.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / VAULT_CONFIG_RELPATH).is_file():
            return candidate
    return None


def resolve_brain_root(brain_root: Path | str | None = None) -> Path:
    """Decide which vault to operate on. Never uses the package's location.

    Precedence, most explicit first:
      1. an explicit argument (the `--vault` flag, or a test)
      2. the BRAIN_ROOT environment variable
      3. a vault marker found by walking up from the current directory
      4. `vault_path:` in the per-user config file
      5. VaultNotFoundError
    """
    if brain_root:
        return Path(brain_root).expanduser()

    env_root = os.environ.get("BRAIN_ROOT")
    if env_root:
        return Path(env_root).expanduser()

    found = find_vault_upwards()
    if found is not None:
        return found

    user_config = user_config_path()
    if user_config.is_file():
        with open(user_config, "r", encoding="utf-8") as fh:
            user_data = yaml.safe_load(fh) or {}
        configured = user_data.get("vault_path")
        if configured:
            return Path(str(configured)).expanduser()

    raise VaultNotFoundError(
        "No Brain vault found. Point at one with `--vault PATH`, set BRAIN_ROOT, "
        f"run from inside a vault, or set `vault_path:` in {user_config}."
    )


# ---------------------------------------------------------------------------
# Identity (OSS-2/C8)
#
# One site, so the name appears in the package exactly once. Everything that
# carries it on a real system — XDG directories, systemd unit names, the MCP
# server name, the backup tag, the credential path — derives from here.
#
# The product and the vault are both "Sako Brain". Where the public code has
# to be distinguished from a user's private vault and data, it is the
# **Sako Brain Core** component of that product — a component name, not a
# second product, and not a name any on-disk artefact uses.
# ---------------------------------------------------------------------------

APP_NAME = "Sako Brain"          # display name, for user-facing text
APP_CORE_NAME = "Sako Brain Core"  # the public code/toolkit component
APP_DIRNAME = "sako-brain"       # slug: XDG dirs, unit names, tags, server name

# The Python distribution name (OSS-4). It IS the slug — packaging introduces
# no new identity. `pyproject.toml` has to repeat the literal, because static
# TOML cannot import Python; that single unavoidable duplicate is pinned to
# this constant by a test rather than left to drift.
DISTRIBUTION_NAME = APP_DIRNAME


def vault_slug(brain_root: Path) -> str:
    """A filesystem-safe, collision-free identifier for one vault.

    The vault's directory name (legible to a human debugging a state directory)
    plus 12 hex of the sha256 of its *resolved* path, so two vaults called
    `brain` in different places never share state or git metadata.
    """
    try:
        identity = str(brain_root.resolve())
    except OSError:  # unresolvable (broken symlink, missing parent) — use as given
        identity = str(brain_root)
    return f"{brain_root.name}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}"


def default_state_dir(brain_root: Path) -> Path:
    """Where this vault's runtime state (index, logs, integrity manifests) lives.

    Deliberately OUTSIDE the vault, for two independent reasons:

    1. **Privacy.** The FTS5 index contains full note bodies. Inside the vault
       it sits in a directory that reads like tooling, so it is the artefact
       most likely to be copied somewhere it does not belong. Outside, the
       vault is Markdown and configuration only, by construction rather than
       by `.gitignore`.
    2. **Sync safety.** A SQLite file rewritten on every `brain index`, living
       inside a Nextcloud/Dropbox/Syncthing-synced folder, is the same hazard
       as a live `.git/` — which `gitops.py` already goes out of its way to
       keep out of the synced tree. Runtime state deserves the same treatment.

    Keyed by a hash of the *resolved* vault path so several vaults on one
    machine can never share state, and prefixed with the vault's directory
    name so the mapping stays legible to a human debugging it.
    """
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / APP_DIRNAME / "vaults" / vault_slug(brain_root)


def default_git_dir(brain_root: Path) -> Path:
    """Where this vault's git metadata directory lives by default.

    OUTSIDE the vault for the reason `gitops.py` documents: git internals churn
    on every commit and must not be handed to a background sync client. Keyed
    the same way as `default_state_dir` so two vaults sharing a directory name
    can never collide on one machine.

    A deployment that already has a git-dir (this vault does) pins it with
    `git_dir:` in its own config.yaml; this default is what a fresh vault gets.
    """
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_DIRNAME / "git" / f"{vault_slug(brain_root)}.git"


@dataclass(frozen=True)
class Config:
    """Resolved configuration for one vault.

    OSS-2/C6: no field carries a default that names a home directory. Fields
    the program can derive (`git_dir`, `state_dir`) derive from the vault under
    the XDG base directories; fields that are pure deployment choices
    (`backup_target`, `backup_repo`) have **no default at all** and stay `None`
    until a config.yaml sets them. `None` here means "not configured", and
    every consumer treats it as "this deployment does not use that", never as
    a path to guess.
    """

    brain_root: Path
    # Where the user keeps working projects. A tuple, not a single path: one
    # root is an assumption about how one person organises a machine, and
    # `~/work` + `~/personal` is an ordinary setup (OSS-2/C5). Empty means no
    # projects root is configured — `brain project discover` says so rather
    # than scanning a guess.
    projects_roots: tuple[Path, ...] = ()
    # Deprecated plaintext rsync target (Phase 1) — do not use, see backup.sh.
    backup_target: Path | None = None
    # Runtime state root (index, logs, integrity manifests) and git metadata
    # dir. Never inside the vault — see `default_state_dir` / `default_git_dir`.
    # `None` means "derive it from brain_root", which `__post_init__` does
    # immediately, so both are always real Paths by the time a caller reads them.
    git_dir: Path | None = None
    backup_repo: Path | None = None   # encrypted restic repository, "local" backend
    # Restic "rclone" backend destination. Deployment identity, never derived:
    # empty means this vault has no rclone backup destination configured.
    backup_rclone_remote: str = ""
    backup_rclone_repo_path: str = ""
    state_dir: Path | None = None
    # Note types and per-type status words — the data model, as configuration.
    vocabulary: Vocabulary = field(default_factory=lambda: Vocabulary())
    # Role -> directory name. Code asks for a role; only this knows the layout.
    taxonomy: Taxonomy = field(default_factory=lambda: Taxonomy())
    # Descriptive, for generated documentation only (OSS-2/C7) — never used to
    # locate anything. `vault_name` falls back to the vault's directory name.
    configured_vault_name: str = ""
    areas: tuple[str, ...] = ()

    def __post_init__(self):
        # frozen dataclass — the documented way to fill a derived default.
        if self.state_dir is None:
            object.__setattr__(self, "state_dir", default_state_dir(self.brain_root))
        if self.git_dir is None:
            object.__setattr__(self, "git_dir", default_git_dir(self.brain_root))

    @property
    def vault_name(self) -> str:
        """Human-readable name for this vault, for generated documentation."""
        return self.configured_vault_name or self.brain_root.name

    def dir_for(self, role: str) -> Path:
        """The absolute directory for a taxonomy role (`inbox`, `projects`, ...)."""
        name = self.taxonomy.directory(role)
        if name is None:
            raise TaxonomyError(f"unknown directory role '{role}'")
        return self.brain_root / name

    @property
    def content_dirs(self) -> tuple[str, ...]:
        """Directory names holding notes with frontmatter, in shipped order."""
        return self.taxonomy.content_dirs()

    @property
    def inbox_dir(self) -> Path:
        return self.dir_for("inbox")

    @property
    def projects_dir(self) -> Path:
        return self.dir_for("projects")

    @property
    def timeline_dir(self) -> Path:
        return self.dir_for("timeline")

    @property
    def system_dir(self) -> Path:
        """The vault's own system directory — `config.yaml` and tooling.

        **Not runtime state.** The index, logs and integrity manifests moved
        out of the vault entirely; see `state_dir`.
        """
        return self.brain_root / SYSTEM_DIRNAME

    @property
    def db_path(self) -> Path:
        return self.state_dir / "brain.db"

    @property
    def registry_path(self) -> Path:
        return self.projects_dir / REGISTRY_FILENAME

    @property
    def templates_dir(self) -> Path:
        return self.dir_for("templates")

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def integrity_dir(self) -> Path:
        return self.state_dir / "integrity"


def _project_roots(data: dict) -> tuple[Path, ...]:
    """Read `projects_roots:` or `projects_root:`, scalar or list, in that order.

    Both spellings are accepted and either may be a single path or a list, so
    an existing single-root config keeps working untouched while a multi-root
    one needs no new vocabulary. Blank entries are dropped; there is no
    fallback to a default location.
    """
    raw = data.get("projects_roots")
    if raw is None:
        raw = data.get("projects_root")
    if raw is None:
        return ()
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    roots = []
    for value in values:
        path = _optional_path(value)
        if path is not None and path not in roots:
            roots.append(path)
    return tuple(roots)


def _optional_path(value) -> Path | None:
    """A configured path, or None when the key is absent/blank."""
    if value is None or str(value).strip() == "":
        return None
    return Path(str(value)).expanduser()


def default_config(brain_root: Path | None = None) -> Config:
    """Load 90_SYSTEM/config.yaml relative to the given (or detected) brain root."""
    root = resolve_brain_root(brain_root)
    config_file = root / VAULT_CONFIG_RELPATH
    data = {}
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    # Precedence: BRAIN_STATE_DIR env > config.yaml `state_dir:` > XDG default.
    # Unlike `brain_root`, a configured value IS honoured here: state has no
    # "physically found" location to contradict it, and pointing several
    # vaults or a recovery copy at chosen state directories is a legitimate need.
    state_dir_raw = os.environ.get("BRAIN_STATE_DIR") or data.get("state_dir")
    state_dir = Path(state_dir_raw).expanduser() if state_dir_raw else None

    return Config(
        # Deliberately `root`, NOT `data.get("brain_root", root)`. `root` is
        # where we *physically found* this config.yaml (via --vault, BRAIN_ROOT,
        # an upward search, or user config) — always correct, including for a
        # restored/copied/moved vault. config.yaml's own `brain_root:` field
        # is documentation only and must never override the real location;
        # doing so previously made a restic restore into a temp directory
        # silently re-index the *live* vault instead of the restored copy.
        brain_root=root,
        projects_roots=_project_roots(data),
        # Deployment-only paths: absent from config.yaml means "this deployment
        # doesn't have one", not "use the author's". Derived paths (git_dir,
        # state_dir) fall through to Config's own XDG defaults when unset.
        backup_target=_optional_path(data.get("backup_target")),
        git_dir=_optional_path(data.get("git_dir")),
        backup_repo=_optional_path(data.get("backup_repo")),
        backup_rclone_remote=str(data.get("backup_rclone_remote") or ""),
        backup_rclone_repo_path=str(data.get("backup_rclone_repo_path") or ""),
        state_dir=state_dir,
        vocabulary=_vocabulary(data),
        taxonomy=_taxonomy(data),
        configured_vault_name=str(data.get("vault_name") or ""),
        areas=tuple(str(a).strip() for a in (data.get("areas") or []) if str(a).strip()),
    )


# ---------------------------------------------------------------------------
# Vocabularies (OSS-2/C4)
#
# The note types and per-type status words ARE the data model, so they ship as
# opinionated defaults — a "bring your own schema" knowledge tool is a worse
# product than an opinionated one. But they are a *user's* data model, and
# someone with an existing vault must be able to extend or rename without
# forking, so they are configuration with these values as the default.
# ---------------------------------------------------------------------------

DEFAULT_NOTE_TYPES = (
    "person", "project", "area", "event", "decision", "fact", "document", "knowledge",
)

# Dedicated CLI commands (`brain projects`, `brain project ...`, `brain
# decision ...`, the registry, handoffs) are built on these two, so a config
# that drops them would break commands rather than customise them.
REQUIRED_NOTE_TYPES = ("project", "decision")

DEFAULT_STATUS_BY_TYPE = {
    "project": ("active", "planned", "on-hold", "completed", "archived", "abandoned", "unknown"),
    "decision": ("proposed", "decided", "superseded"),
    "fact": ("current", "superseded"),
}


class VocabularyError(ValueError):
    """A configured vocabulary that would break the tool rather than adapt it."""


@dataclass(frozen=True)
class Vocabulary:
    """The note types, and the status words allowed for each type.

    Stored as tuples rather than sets/dicts so `Config` stays a hashable frozen
    dataclass. Order is the shipped order, which is also display order.
    """

    note_types: tuple[str, ...] = DEFAULT_NOTE_TYPES
    status_vocabularies: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
        (t, v) for t, v in DEFAULT_STATUS_BY_TYPE.items()
    )

    def __post_init__(self):
        missing = [t for t in REQUIRED_NOTE_TYPES if t not in self.note_types]
        if missing:
            raise VocabularyError(
                f"note_types must include {list(REQUIRED_NOTE_TYPES)} — dedicated "
                f"commands depend on them; missing: {missing}"
            )

    def statuses_for(self, note_type: str) -> tuple[str, ...] | None:
        """Allowed statuses for a type, or None when the type is unconstrained."""
        for name, statuses in self.status_vocabularies:
            if name == note_type:
                return statuses
        return None


def _vocabulary(data: dict) -> Vocabulary:
    """Build a Vocabulary from config.yaml data.

    `note_types:` replaces the shipped list outright (copy the default and add
    to it). `status_by_type:` merges per type, so overriding `project` leaves
    `decision` and `fact` at their defaults.
    """
    raw_types = data.get("note_types")
    if raw_types:
        note_types = tuple(dict.fromkeys(str(t).strip() for t in raw_types if str(t).strip()))
    else:
        note_types = DEFAULT_NOTE_TYPES

    statuses = dict(DEFAULT_STATUS_BY_TYPE)
    for name, values in (data.get("status_by_type") or {}).items():
        cleaned = tuple(dict.fromkeys(str(v).strip() for v in (values or []) if str(v).strip()))
        if cleaned:
            statuses[str(name)] = cleaned
        else:
            statuses.pop(str(name), None)  # an explicitly empty list means "unconstrained"

    return Vocabulary(
        note_types=note_types,
        status_vocabularies=tuple(statuses.items()),
    )


# ---------------------------------------------------------------------------
# Taxonomy (OSS-2/C3)
#
# The numbered-prefix layout stays the shipped default — an opinionated
# knowledge tool is a better product than a "bring your own taxonomy" one, and
# the opinion IS the value. But the code depends on ROLES, not on directory
# names, so someone with an existing vault can remap without forking.
#
# `90_SYSTEM` is the one exception and is NOT a role: `90_SYSTEM/config.yaml`
# is the marker that identifies a vault at all (see `find_vault_upwards`), so
# it has to be findable before any configuration has been read.
# ---------------------------------------------------------------------------

REGISTRY_FILENAME = "_registry.yaml"

DEFAULT_DIRECTORIES = {
    "inbox": "00_INBOX",
    "people": "10_PEOPLE",
    "areas": "20_AREAS",
    "projects": "30_PROJECTS",
    "decisions": "40_DECISIONS",
    "timeline": "50_TIMELINE",
    "knowledge": "60_KNOWLEDGE",
    "documents": "70_DOCUMENTS",
    "templates": "80_TEMPLATES",
}

# Roles whose directories hold notes with frontmatter — indexed and validated.
# `templates` is deliberately absent: templates are not notes.
CONTENT_ROLES = (
    "inbox", "people", "areas", "projects", "decisions", "timeline",
    "knowledge", "documents",
)

# Physical registry folder a project status resolves to. completed/abandoned/
# unknown all live under ARCHIVED (a "not currently active" bucket) while
# keeping their real status in frontmatter — there is no ARCHIVED/COMPLETED
# split on disk.
DEFAULT_PROJECT_STATUS_FOLDERS = {
    "active": "ACTIVE",
    "planned": "PLANNED",
    "on-hold": "ON-HOLD",
    "completed": "ARCHIVED",
    "archived": "ARCHIVED",
    "abandoned": "ARCHIVED",
    "unknown": "ARCHIVED",
}

FALLBACK_PROJECT_FOLDER = "ARCHIVED"


class TaxonomyError(ValueError):
    """A configured taxonomy that would break the tool rather than adapt it."""


@dataclass(frozen=True)
class Taxonomy:
    """Role -> directory name, and project status -> registry folder.

    Tuples rather than dicts so `Config` stays a hashable frozen dataclass.
    """

    directories: tuple[tuple[str, str], ...] = tuple(DEFAULT_DIRECTORIES.items())
    project_status_folders: tuple[tuple[str, str], ...] = tuple(
        DEFAULT_PROJECT_STATUS_FOLDERS.items()
    )

    def __post_init__(self):
        missing = [r for r in (*CONTENT_ROLES, "templates") if self.directory(r) is None]
        if missing:
            raise TaxonomyError(f"taxonomy is missing a directory for role(s): {missing}")
        names = [name for _, name in self.directories]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise TaxonomyError(f"two roles cannot share a directory: {duplicates}")
        if SYSTEM_DIRNAME in names:
            raise TaxonomyError(
                f"{SYSTEM_DIRNAME}/ is the vault marker and cannot also be a content role"
            )

    def directory(self, role: str) -> str | None:
        for name, value in self.directories:
            if name == role:
                return value
        return None

    def content_dirs(self) -> tuple[str, ...]:
        return tuple(self.directory(role) for role in CONTENT_ROLES)

    def folder_for_status(self, status: str) -> str:
        for name, folder in self.project_status_folders:
            if name == status:
                return folder
        return FALLBACK_PROJECT_FOLDER


def _taxonomy(data: dict) -> Taxonomy:
    """Build a Taxonomy from config.yaml. Both maps merge over the defaults."""
    directories = dict(DEFAULT_DIRECTORIES)
    for role, name in (data.get("directories") or {}).items():
        role, name = str(role).strip(), str(name).strip().strip("/")
        if not name:
            continue
        if role not in directories:
            raise TaxonomyError(
                f"unknown directory role '{role}' — known roles: {sorted(directories)}"
            )
        directories[role] = name

    folders = dict(DEFAULT_PROJECT_STATUS_FOLDERS)
    for status, folder in (data.get("project_status_folders") or {}).items():
        folder = str(folder).strip().strip("/")
        if folder:
            folders[str(status).strip()] = folder

    return Taxonomy(
        directories=tuple(directories.items()),
        project_status_folders=tuple(folders.items()),
    )


# The shipped content directories, for callers that need a default before a
# vault is resolved (tests building a throwaway vault, mainly). The authority
# at runtime is `config.content_dirs`.
CONTENT_DIRS = tuple(DEFAULT_DIRECTORIES[role] for role in CONTENT_ROLES)
