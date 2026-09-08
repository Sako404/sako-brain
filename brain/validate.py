"""brain doctor — health checks. Reports problems, never auto-fixes or deletes."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from . import backup, frontmatter
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
    problems = []
    for e in load_registry(config):
        if e.path and not Path(e.path).exists():
            problems.append(Problem("missing_project_dirs", f"registry entry '{e.id}': path does not exist: {e.path}"))
    for n in notes:
        if n.type == "project":
            p = n.meta.get("path")
            if p and not Path(p).exists():
                problems.append(Problem("missing_project_dirs", f"{n.path}: project path does not exist: {p}"))
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


def run_all(config: Config) -> list[Problem]:
    notes, parse_errors = load_all_notes(config)
    problems: list[Problem] = list(parse_errors)
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
    return problems
