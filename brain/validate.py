"""brain doctor — health checks. Reports problems, never auto-fixes or deletes."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from . import backup, frontmatter
from . import identity, visibility
from . import paths as paths_mod
from .paths import Config
from .registry import find_duplicates, load_registry

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
MDLINK_RE = re.compile(r"\]\(([^)]+)\)")

SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("generic API key assignment", re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}")),
    ("password assignment", re.compile(r"(?i)\bpassword\b\s*[:=]\s*['\"]?\S{4,}")),
    ("bearer token", re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("restic/backup credential reference", re.compile(r"(?i)\bRESTIC_PASSWORD\b\s*[:=]")),
]

ALLOWED_SENSITIVITY = {"normal", "private", "restricted"}

# Filenames that should never appear inside the Brain — if they do, a
# credential has likely been dropped in the wrong place.
SECRET_FILENAME_GLOBS = [
    "restic-password", "*.pem", "id_rsa", "id_ed25519", "id_ecdsa", "*.key", "credentials.json",
]


@dataclass
class Problem:
    check: str
    message: str

    def __str__(self):
        return f"[{self.check}] {self.message}"


def load_all_notes(config: Config):
    """Parse every content note. Public — reused by brain.integrity as well
    as this module's own run_all()."""
    notes, parse_errors = [], []
    for path in frontmatter.iter_markdown_files(config.brain_root, config.content_dirs):
        try:
            notes.append(frontmatter.parse_file(path))
        except frontmatter.FrontmatterError as exc:
            parse_errors.append(Problem("invalid_yaml", str(exc)))
    return notes, parse_errors


def check_duplicate_ids(notes) -> list[Problem]:
    seen: dict[str, list[Path]] = {}
    for n in notes:
        if n.id:
            seen.setdefault(n.id, []).append(n.path)
    return [
        Problem("duplicate_ids", f"id '{id_}' used by {len(paths)} notes: {', '.join(str(p) for p in paths)}")
        for id_, paths in seen.items() if len(paths) > 1
    ]


def check_invalid_status(config: Config, notes) -> list[Problem]:
    problems = []
    for n in notes:
        allowed = config.vocabulary.statuses_for(n.type)
        if allowed and n.status and n.status not in allowed:
            problems.append(Problem(
                "invalid_status",
                f"{n.path}: status '{n.status}' not in {sorted(allowed)} for type '{n.type}'",
            ))
    return problems


def check_broken_links(notes) -> list[Problem]:
    ids = {n.id for n in notes if n.id}
    problems = []
    for n in notes:
        for m in WIKILINK_RE.finditer(n.body):
            target = m.group(1).strip()
            if target and target not in ids:
                problems.append(Problem("broken_links", f"{n.path}: [[{target}]] does not resolve to any note id"))
        for m in MDLINK_RE.finditer(n.body):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = (n.path.parent / target).resolve()
            if not target_path.exists():
                problems.append(Problem("broken_links", f"{n.path}: link to '{target}' does not exist"))
    return problems


def check_missing_project_dirs(config: Config, notes) -> list[Problem]:
    """A registered project path that Path.exists() can't find is only
    genuinely "missing" when this process could plausibly see it in the
    first place. On a restricted-SSH server dispatcher (config.remote_project_paths),
    every registered path is a desktop path this host never has and never
    will — reporting that as "missing" is a false positive baked into the
    architecture, not a real data problem. Report it as unverifiable
    instead: still visible in `brain doctor`'s output (nothing is hidden),
    but honestly labeled, and never mistaken for actual drift."""
    check_name = "project_path_unverifiable" if config.remote_project_paths else "missing_project_dirs"
    verb = "cannot verify from this host (not locally visible)" if config.remote_project_paths else "does not exist"
    problems = []
    for e in load_registry(config):
        if e.path and not Path(e.path).exists():
            problems.append(Problem(check_name, f"registry entry '{e.id}': path {verb}: {e.path}"))
    for n in notes:
        if n.type == "project":
            p = n.meta.get("path")
            if p and not Path(p).exists():
                problems.append(Problem(check_name, f"{n.path}: project path {verb}: {p}"))
    return problems


def check_registry_duplicates(config: Config) -> list[Problem]:
    return [Problem("duplicate_registry_entries", msg) for msg in find_duplicates(load_registry(config))]


def check_backup_not_canonical(config: Config) -> list[Problem]:
    problems = []
    if config.backup_target is None:
        return problems  # no deprecated plaintext target configured — nothing to confuse
    if config.brain_root.resolve() == config.backup_target.resolve():
        problems.append(Problem(
            "backup_as_canonical",
            "brain_root and backup_target resolve to the SAME path — backup dir is being treated as canonical!",
        ))
    try:
        if config.backup_target.exists() and any(config.backup_target.iterdir()):
            # Non-empty backup existing is fine; only flag if it's actually being read as root elsewhere.
            pass
    except (FileNotFoundError, NotADirectoryError):
        pass
    return problems


def check_dangling_supersedes(notes) -> list[Problem]:
    """`supersedes` points at a prior note's id — flag it if that id doesn't exist,
    the same way check_broken_links does for [[wikilinks]]."""
    ids = {n.id for n in notes if n.id}
    problems = []
    for n in notes:
        raw = n.meta.get("supersedes")
        if not raw:
            continue
        targets = raw if isinstance(raw, list) else [raw]
        for target in targets:
            target = str(target).strip()
            if target and target not in ids:
                problems.append(Problem(
                    "dangling_supersedes",
                    f"{n.path}: supersedes '{target}' does not resolve to any note id",
                ))
    return problems


def _parse_date(value) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def check_stale_validity(notes) -> list[Problem]:
    """A fact marked status: current with a valid_to date already in the past
    is a sign it should probably be superseded instead — surfaced as a
    heads-up, not an error, since the user may just not have updated it yet."""
    today = dt.date.today()
    problems = []
    for n in notes:
        if n.type != "fact" or n.status != "current":
            continue
        valid_to = _parse_date(n.meta.get("valid_to"))
        if valid_to and valid_to < today:
            problems.append(Problem(
                "stale_validity",
                f"{n.path}: status is 'current' but valid_to ({valid_to}) is in the past — consider marking it superseded",
            ))
    return problems


def check_secrets(notes) -> list[Problem]:
    problems = []
    for n in notes:
        lines = n.body.splitlines()
        for i, line in enumerate(lines, start=1):
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    problems.append(Problem("secret_pattern", f"{n.path}:{i}: possible {label} found — remove and rotate the credential"))
    return problems


def check_invalid_sensitivity(notes) -> list[Problem]:
    problems = []
    for n in notes:
        s = n.meta.get("sensitivity")
        if s and s not in ALLOWED_SENSITIVITY:
            problems.append(Problem(
                "invalid_sensitivity",
                f"{n.path}: sensitivity '{s}' not in {sorted(ALLOWED_SENSITIVITY)}",
            ))
    return problems


def check_backup_secret_in_brain(config: Config) -> list[Problem]:
    """A backup/SSH/API credential must never live inside the Brain tree —
    it would end up in git history and (unencrypted, at the Brain's own
    plaintext trust level) in every future restic snapshot too."""
    problems = []
    seen = set()
    for pattern in SECRET_FILENAME_GLOBS:
        for path in config.brain_root.rglob(pattern):
            if ".git" in path.parts or path in seen:
                continue
            seen.add(path)
            problems.append(Problem(
                "backup_secret_in_brain",
                f"{path}: filename matches a common credential-file pattern ('{pattern}') — "
                "secrets must never live inside the Brain; move it out (e.g. to "
                f"{paths_mod.user_config_path().parent}/ or a password manager) "
                "and rotate it if it's real.",
            ))
    return problems


def check_unsafe_plaintext_backup(config: Config) -> list[Problem]:
    """The Phase 1 backup_target was a plain rsync mirror — no encryption.
    It predates restricted data and is superseded by the encrypted restic
    repository (config.backup_repo). Flag if it still has content, and
    flag loudly if any of that content is sensitivity: restricted."""
    problems = []
    target = config.backup_target
    if target is None:
        return problems
    try:
        if not target.exists():
            return problems
        files = [p for p in target.rglob("*") if p.is_file()]
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return problems

    if not files:
        return problems

    problems.append(Problem(
        "unsafe_plaintext_backup",
        f"deprecated plaintext backup target {target} is non-empty ({len(files)} file(s)) — "
        "predates restricted data and the encrypted backup; review and remove once "
        "'brain backup' is confirmed to cover everything.",
    ))

    for path in files:
        if path.suffix != ".md":
            continue
        try:
            note = frontmatter.parse_file(path)
        except frontmatter.FrontmatterError:
            continue
        if note.meta.get("sensitivity") == "restricted":
            problems.append(Problem(
                "unsafe_plaintext_backup",
                f"RESTRICTED note found in PLAINTEXT at {path} — remove this file immediately.",
            ))
    return problems


def check_sqlite_fts5(config: Config) -> list[Problem]:
    """The search index needs SQLite compiled with FTS5.

    Not a pip dependency and not installable by one — it is a property of the
    interpreter's own sqlite3 build. Present in essentially every mainstream
    build, which is exactly why it would otherwise be discovered as a confusing
    failure inside `brain index` on the one machine that lacks it (OSS-4).
    """
    import sqlite3

    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [Problem(
            "sqlite_fts5_missing",
            f"this Python's SQLite has no FTS5 support ({exc}) — `brain index` "
            "and `brain search` cannot work. FTS5 is a build option of SQLite "
            "itself, not something pip can install: use a Python whose sqlite3 "
            "was built with it.",
        )]
    return []


def check_state_dir_outside_vault(config: Config) -> list[Problem]:
    """Runtime state must not live inside the vault it describes.

    OSS-1 moved the index, logs and integrity manifests out of the vault for
    two reasons: the FTS5 index holds full note bodies, and a SQLite file
    rewritten on every `brain index` inside a synced folder is the same hazard
    as a live `.git/`. The code guarantees the *default*; nothing guarded a
    user pointing `state_dir:` (or BRAIN_STATE_DIR) back inside the vault,
    which silently undoes both.
    """
    problems = []
    try:
        state = config.state_dir.resolve()
        root = config.brain_root.resolve()
    except OSError:
        return problems

    if state == root or root in state.parents:
        problems.append(Problem(
            "state_dir_inside_vault",
            f"runtime state directory {state} is inside the vault ({root}) — the "
            "search index holds full note bodies and is rewritten on every "
            "'brain index', so it must live outside (default: "
            f"{paths_mod.default_state_dir(root)}). Fix `state_dir:` in "
            "90_SYSTEM/config.yaml or BRAIN_STATE_DIR, then re-run 'brain index'.",
        ))
    return problems


def check_password_file_not_backed_up(config: Config) -> list[Problem]:
    """The restic password file must never be inside a path that
    `backup_paths()` would hand to restic — otherwise the encrypted backup
    would (redundantly, and riskily) contain the key to decrypt itself."""
    problems = []
    settings = backup.default_settings(config)
    password_file = settings.password_file.resolve()

    for backed_up in (config.brain_root, config.git_dir):
        try:
            backed_up_resolved = backed_up.resolve()
        except OSError:
            continue
        if backed_up_resolved in password_file.parents or backed_up_resolved == password_file:
            problems.append(Problem(
                "password_file_in_backup_scope",
                f"restic password file {password_file} is inside a backed-up path "
                f"({backed_up_resolved}) — move it out immediately (e.g. back to "
                f"{paths_mod.user_config_path().parent}/) and rotate the repository password.",
            ))
    return problems


def is_admin_principal(config: Config, principal_id: str) -> bool:
    p = identity.get_principal(config, principal_id)
    if p is None:
        # No record for this id. Two different situations look identical
        # here: (a) this vault has no principal records at all yet (every
        # pre-Stage-1 identity's established "full access" default — see
        # Config.acting_principal — must not quietly become a new
        # restriction the day this check landed), vs (b) principal_id
        # names someone who was never created or was deleted, which must
        # NOT get admin output just by naming the default id. Distinguish
        # by whether ANY principal record exists in this vault at all.
        return not identity.list_principals(config)
    return p.is_active and p.role == "admin"


# Headings/markers a record's own body uses to say "this is finished". Kept
# deliberately small and literal: this is the input to an ADVISORY check, and
# a loose pattern would turn every passing mention of the word into a false
# "please reconcile".
_CLOSURE_HEADING_RE = re.compile(
    r"(?im)^\s*#{1,6}\s*(closure|closed|resolution|resolved|outcome)\b"
)
_CLOSURE_STATEMENT_RE = re.compile(
    r"(?im)^\s*\**status:?\**\s*\**\s*(closed|resolved|done|complete[d]?)\b"
)


def check_invalid_acl(notes: list[frontmatter.Note]) -> list[Problem]:
    """Admin-only diagnostics; invalid ACLs are invisible to ordinary callers."""
    problems = []
    for note in notes:
        try:
            visibility.owner_and_audience(note.meta)
        except visibility.VisibilityError:
            problems.append(Problem("invalid_acl", f"{note.path}: invalid owner/audience metadata"))
    return problems


def check_symlink_escapes_vault(config: Config) -> list[Problem]:
    """Markdown files inside a content directory that resolve OUTSIDE the vault.

    `frontmatter.iter_markdown_files` skips these — the scan boundary is the
    vault, not wherever its directories happen to point, and before that was
    explicit a link dropped into a content directory pulled an outside file
    into listings, the search index and this module's own integrity checks,
    carrying content and an ACL from a file nobody managing the vault can
    see. Skipping it silently would be its own version of the problem this
    release exists to fix, so the operator is told: a vault with one of these
    has either a mistake or an intent the tooling cannot honour.
    """
    problems = []
    root = config.brain_root
    for path in frontmatter.iter_external_links(root, config.content_dirs):
        problems.append(Problem(
            "symlink_escapes_vault",
            f"{path} is an outside, protected-identity, broken or looping link"
            " — it is NOT indexed, listed or integrity-checked as vault content. "
            "Either copy the note into the vault or remove the link; a Brain record "
            "must live in the Brain.",
        ))
    return problems


def check_handoff_in_wrong_status_folder(config: Config) -> list[Problem]:
    """Deterministic structural check: a project's handoff must sit in the
    same status folder as the project record every reader resolves it from.

    `handoff.handoff_path()` derives the location from the project's CURRENT
    status, so a handoff left behind by a status change is invisible to
    `brain handoff show`/`list` and to `/resume`, while still being found by
    id-based reads — and the next `handoff write` could create a second document
    under the same id. Pure filesystem/registry comparison: no content
    is read and no heuristic is involved, so this is a hard error, not an
    advisory.
    """
    problems = []
    projects_dir = config.projects_dir
    if not projects_dir.exists():
        return problems
    for entry in load_registry(config):
        expected_folder = config.taxonomy.folder_for_status(entry.status)
        filename = f"{entry.id}-handoff.md"
        expected = projects_dir / expected_folder / filename
        for _role, folder in config.taxonomy.project_status_folders:
            candidate = projects_dir / folder / filename
            if candidate == expected or not candidate.exists():
                continue
            problems.append(Problem(
                "handoff_in_wrong_status_folder",
                f"{candidate}: handoff for '{entry.id}' (status '{entry.status}') is in "
                f"{folder}/ but every reader resolves it from {expected_folder}/. "
                + ("Both copies exist — merge them by hand, keeping each one's unique "
                   "sessions, then remove the stale path. "
                   if expected.exists() else
                   "Move it to the correct folder. ")
                + "`brain project update-status` relocates it automatically as of v0.14.6; "
                  "this one predates that.",
            ))
    return problems


# Projects whose state is expected to move; a completed/archived project's
# record legitimately stops changing.
_CURRENT_STATE_STATUSES = ("active", "planned", "on-hold")


def check_project_current_state(config: Config, notes) -> list[Problem]:
    """Advisory: where a project stands must live in its record's `Current
    state` section (see projectops.CURRENT_STATE_SECTION). Flags an
    active/planned/on-hold project whose section is missing or empty
    (`project_current_state_missing`), and one whose section was last
    written before its latest handoff (`project_current_state_stale`) —
    the case where a session recorded new state in the handoff only.
    Deterministic: dates from frontmatter, no reading of prose."""
    from . import handoff as handoff_mod
    from .projectops import current_state

    problems = []
    for n in notes:
        if n.type != "project" or (n.status or "") not in _CURRENT_STATE_STATUSES:
            continue
        state = current_state(n)
        if not state["text"]:
            problems.append(Problem(
                "project_current_state_missing",
                f"{n.path}: no '## Current state' content — write it with "
                f"`brain project section-update {n.id} --section 'Current state' --mode replace`",
            ))
            continue
        try:
            handoff_path = handoff_mod.handoff_path(config, n.id)
        except Exception:
            continue
        if not handoff_path.exists():
            continue
        try:
            handoff_updated = _parse_date(frontmatter.parse_file(handoff_path).meta.get("updated"))
        except frontmatter.FrontmatterError:
            continue
        state_updated = _parse_date(state["updated"])
        if handoff_updated and (state_updated is None or state_updated < handoff_updated):
            problems.append(Problem(
                "project_current_state_stale",
                f"{n.path}: Current state last written {state['updated'] or 'never (by section-update)'} "
                f"but the handoff was updated {handoff_updated} — replace Current state with "
                f"where the project stands now",
            ))
    return problems


def check_status_content_conflict(config: Config, notes) -> list[Problem]:
    """Advisory mismatch detection; never changes or blocks a record.

    Synthetic example: a fictional service incident has an explicit closure
    heading while its metadata remains proposed. Report it for human review;
    a passing mention of closure in prose is not sufficient evidence.
    """
    problems = []
    for n in notes:
        if (n.status or "") != "proposed":
            continue
        body = n.body or ""
        heading = _CLOSURE_HEADING_RE.search(body)
        statement = _CLOSURE_STATEMENT_RE.search(body)
        if not heading and not statement:
            continue
        evidence = (heading or statement).group(0).strip()
        problems.append(Problem(
            "status_content_conflict",
            f"{n.path}: status is 'proposed' but the body contains {evidence!r} — "
            "TO RECONCILE (not auto-corrected): confirm against the project's latest "
            "handoff and any later record before changing the status, and leave it as "
            "it is if the evidence is not conclusive.",
        ))
    return problems


def run_all(config: Config, principal_id: str | None = None) -> list[Problem]:
    """Stage 2 (multi-user visibility): every check here walks the WHOLE
    vault (`load_all_notes`, `load_registry`, backup/state/secrets
    config) regardless of who's asking — integrity checks like
    `check_duplicate_ids` need the complete picture to be correct, the
    same reason registry.py's load_registry() stays unfiltered for
    write-path and integrity use. But the detailed OUTPUT (ids, paths,
    titles, and operational details like backup configuration) is
    admin-only — reachable over the SSH dispatcher's read mode like any
    other `brain doctor` call, so a non-admin caller gets told diagnostics
    exist but not their content, never the vault-wide detail a privacy
    policy this strict can't let a non-admin principal see just by asking."""
    if principal_id is None:
        principal_id = config.acting_principal
    if not is_admin_principal(config, principal_id):
        return [Problem(
            "admin_required",
            "brain doctor's detailed diagnostics require an admin principal — "
            "contact the vault admin if you believe there's a real problem.",
        )]

    notes, parse_errors = load_all_notes(config)
    problems: list[Problem] = list(parse_errors)
    problems += check_invalid_acl(notes)
    problems += check_duplicate_ids(notes)
    problems += check_invalid_status(config, notes)
    problems += check_broken_links(notes)
    problems += check_missing_project_dirs(config, notes)
    problems += check_registry_duplicates(config)
    problems += check_backup_not_canonical(config)
    problems += check_dangling_supersedes(notes)
    problems += check_stale_validity(notes)
    problems += check_secrets(notes)
    problems += check_invalid_sensitivity(notes)
    problems += check_backup_secret_in_brain(config)
    problems += check_state_dir_outside_vault(config)
    problems += check_sqlite_fts5(config)
    problems += check_unsafe_plaintext_backup(config)
    problems += check_password_file_not_backed_up(config)
    problems += check_handoff_in_wrong_status_folder(config)
    problems += check_symlink_escapes_vault(config)
    problems += check_project_current_state(config, notes)
    # Advisory, last: a heuristic belongs after every deterministic check,
    # and must never be added to gitops.BLOCKING_CHECKS.
    problems += check_status_content_conflict(config, notes)
    return problems
