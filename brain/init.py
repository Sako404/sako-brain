"""First-run vault creation — `brain init` (OSS-4).

OSS-2 deliberately removed every private default, which left a new user with
correct-but-unhelpful messages about missing configuration. This module is the
positive path: it produces a vault that `brain doctor` calls healthy, and
nothing else.

Design rules, in priority order:

1. **Never destroy.** No file this module did not create is ever written to,
   moved or removed. Every action is create-if-absent.
2. **Explicit.** The vault path is given by the user; nothing is inferred from
   the current directory.
3. **Idempotent.** A second run reports what already existed and changes
   nothing, so a partial first run is repaired by running it again rather than
   by a rollback that could delete real content.
4. **Validate, then create.** Everything that can be checked is checked before
   the first write, so a refusal happens with nothing on disk.

What it deliberately does NOT do: write backup keys, create credentials,
initialise git (unless asked), create state or git directories (they are
derived and made on demand), install systemd units, or seed example content.
`brain init --demo` is a separate contract for that last one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import agentsdoc
from . import paths as paths_mod
from .paths import CONTENT_ROLES, SYSTEM_DIRNAME, Config, default_config

# Owner-only. A vault holds personal notes; the config may later name paths a
# user considers private. Applied to directories this run creates, never to
# anything that already existed.
VAULT_MODE = 0o700
CONFIG_MODE = 0o600

AGENTS_FILENAME = agentsdoc.OUTPUT_FILENAME


class InitError(RuntimeError):
    """A refusal: the target is unsafe or already a vault."""


@dataclass
class InitResult:
    vault: Path
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Neither created nor kept: locations the vault will USE, reported so a new
    # user learns where things live. Calling these "created" or "kept" would
    # both be untrue.
    locations: list[str] = field(default_factory=list)
    already_initialised: bool = False

    def note(self, what: str, was_created: bool) -> None:
        (self.created if was_created else self.skipped).append(what)

    def location(self, what: str) -> None:
        self.locations.append(what)


CONFIG_TEMPLATE = """\
# {app} vault configuration.
#
# The physical location of THIS file is what identifies the vault. A path
# written inside it is documentation, never an override — that distinction
# exists because a restored backup once silently re-indexed the live vault.
#
# Every key below is optional and commented out: the shipped defaults are the
# opinion this tool is built around, and an empty file is a valid vault.

vault_name: {vault_name}

# Where your working projects live, so `brain project discover` can propose
# them. A single path or a list; `projects_roots:` is accepted as a plural
# spelling. Unset means discovery reports that it has nothing to scan rather
# than guessing.
#
# projects_root: ~/projects

# Ongoing life areas, used only to generate AGENTS.md.
#
# areas: [Work, Home, Finance]

# Runtime state — the search index, logs and integrity manifests. Left unset it
# defaults to $XDG_STATE_HOME/{app_dir}/vaults/<vault>-<id>, deliberately
# OUTSIDE the vault: the index holds full note bodies, and a SQLite file
# rewritten on every `brain index` inside a synced folder is a hazard.
#
# state_dir: ~/.local/state/{app_dir}/vaults/my-vault

# Git metadata directory, also outside the vault by default. Only used if you
# run `brain git init`.
#
# git_dir: ~/.local/share/{app_dir}/git/my-vault.git

# Directory names, by ROLE. The code depends on the roles, not the spellings,
# so an existing vault can be remapped without forking. `{system}` is absent
# on purpose: it is the marker that identifies a vault and is not a role.
#
# directories:
#   inbox: {inbox}
#   people: {people}
#   areas: {areas_dir}
#   projects: {projects}
#   decisions: {decisions}
#   timeline: {timeline}
#   knowledge: {knowledge}
#   documents: {documents}
#   templates: {templates}

# The physical folder each project status resolves to.
#
# project_status_folders:
#   active: ACTIVE
#   planned: PLANNED
#   on-hold: ON-HOLD
#   completed: ARCHIVED

# The data model, as configuration. `note_types:` REPLACES the shipped list
# (copy it and add to it); `project` and `decision` cannot be removed, because
# dedicated commands are built on them. `status_by_type:` merges per type.
#
# note_types: [{note_types}]
# status_by_type:
#   project: [active, planned, on-hold, completed, archived, abandoned, unknown]

# Backup is optional and has NO default destination: an unconfigured vault gets
# an explicit error naming the key, never somebody else's repository. Configure
# these only when you actually want `brain backup`.
#
# backup_repo: /path/to/restic/repo          # "local" backend
# backup_rclone_remote: MY_REMOTE            # "rclone" backend (the default)
# backup_rclone_repo_path: backups/brain
"""


def render_config(vault: Path) -> str:
    """The commented first-run config. Comments only — every value is a default."""
    directories = dict(paths_mod.DEFAULT_DIRECTORIES)
    return CONFIG_TEMPLATE.format(
        app=paths_mod.APP_NAME,
        app_dir=paths_mod.APP_DIRNAME,
        system=SYSTEM_DIRNAME,
        vault_name=vault.name,
        note_types=", ".join(paths_mod.DEFAULT_NOTE_TYPES),
        inbox=directories["inbox"], people=directories["people"],
        areas_dir=directories["areas"], projects=directories["projects"],
        decisions=directories["decisions"], timeline=directories["timeline"],
        knowledge=directories["knowledge"], documents=directories["documents"],
        templates=directories["templates"],
    )


def _validate(vault: Path, force: bool) -> bool:
    """Check everything before writing anything. Returns already_initialised."""
    if vault.exists() and not vault.is_dir():
        raise InitError(f"{vault} exists and is not a directory")

    marker = vault / paths_mod.VAULT_CONFIG_RELPATH
    if marker.is_file():
        if not force:
            raise InitError(
                f"{vault} is already a vault ({marker} exists). Nothing was "
                "changed. Re-run with --force to fill in anything missing; the "
                "existing configuration is never overwritten either way."
            )
        return True

    parent = vault.parent
    if not parent.exists():
        raise InitError(f"parent directory does not exist: {parent}")
    return False


def initialise(vault: Path, *, with_agents: bool = False, with_git: bool = False,
               force: bool = False) -> InitResult:
    """Create (or complete) a vault at `vault`. Never overwrites."""
    vault = Path(vault).expanduser()
    try:
        resolved = vault.resolve()
    except OSError as exc:
        raise InitError(f"cannot resolve {vault}: {exc}") from None

    already = _validate(resolved, force)
    result = InitResult(vault=resolved, already_initialised=already)

    created_root = not resolved.exists()
    resolved.mkdir(parents=True, exist_ok=True)
    if created_root:
        _chmod(resolved, VAULT_MODE)
    result.note(f"{resolved}/", created_root)

    system_dir = resolved / SYSTEM_DIRNAME
    result.note(f"{SYSTEM_DIRNAME}/", _mkdir(system_dir))

    config_path = resolved / paths_mod.VAULT_CONFIG_RELPATH
    if config_path.exists():
        result.note(str(paths_mod.VAULT_CONFIG_RELPATH), False)
    else:
        config_path.write_text(render_config(resolved), encoding="utf-8")
        _chmod(config_path, CONFIG_MODE)
        result.note(str(paths_mod.VAULT_CONFIG_RELPATH), True)

    # Config now exists, so the vault resolves and its taxonomy is authoritative
    # — a --force run over a remapped vault creates the user's directories, not
    # the shipped ones.
    config = default_config(resolved)
    for role in CONTENT_ROLES:
        result.note(f"{config.taxonomy.directory(role)}/", _mkdir(config.dir_for(role)))

    # Off by default: an agent rules file is a client concern, and the vault is
    # complete and doctor-clean without one. `brain agents-doc --write` renders
    # it on demand, for whoever actually wants it.
    if with_agents:
        agents_path = resolved / AGENTS_FILENAME
        if agents_path.exists():
            result.note(AGENTS_FILENAME, False)
        else:
            agents_path.write_text(agentsdoc.render(config), encoding="utf-8")
            result.note(AGENTS_FILENAME, True)

    if with_git:
        from . import gitops

        if gitops.is_initialized(config):
            result.note("git history (already initialised)", False)
        else:
            gitops.init_repo(config)
            result.note(f"git history at {config.git_dir}", True)

    return result


def initialise_demo(vault: Path, *, with_agents: bool = False) -> InitResult:
    """Create the synthetic Example Brain.

    A thin wrapper over `demo.build()` — the OSS-3 generator is the single
    implementation and is not duplicated here. This adds only what a user-facing
    command owes: reporting what was created, and optionally AGENTS.md, which
    the demo generator has no business deciding about.

    Deliberately separate from `initialise()`: a real user's first vault must
    never be seeded with fictional content, so the two contracts do not mix.
    """
    from . import demo

    vault = Path(vault).expanduser()
    try:
        built = demo.build(vault)
    except demo.DemoTargetError as exc:
        raise InitError(str(exc)) from None

    result = InitResult(vault=built.root)
    result.note(f"{built.root}/  (synthetic vault: {demo.DEMO_VAULT_NAME})", True)
    for root in built.project_roots:
        result.note(f"{root}/  (synthetic project root)", True)
    # Not created here: state is made on demand by whatever first needs it.
    # Reporting it as created would be a small lie in the first thing a new
    # user reads.
    result.location(f"{built.state_dir}/  (runtime state, outside the vault)")
    result.note(f"{len(demo.SEEDED_NOTE_IDS)} example notes", True)

    if with_agents:
        config = default_config(built.root)
        agents_path = built.root / AGENTS_FILENAME
        if agents_path.exists():
            result.note(AGENTS_FILENAME, False)
        else:
            agents_path.write_text(agentsdoc.render(config), encoding="utf-8")
            result.note(AGENTS_FILENAME, True)

    return result


def _mkdir(path: Path) -> bool:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    return not existed


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass  # best-effort: a filesystem without POSIX modes must not fail init
