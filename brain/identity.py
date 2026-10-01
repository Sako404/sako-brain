"""Privileged identity/authorization state: principals and groups
(SAKO Brain multi-user, Stage 1 — Identity + Authorization Foundation).

Deliberately stored under `90_SYSTEM/identity/` — not a content directory.
`Taxonomy.__post_init__` (paths.py) already forbids `90_SYSTEM` from ever
being registered as a content role, so ordinary write commands (`remember`,
`update`, `note create`, `project create`, `decision create`, `timeline
add`) cannot reach these records even in principle: `find_note_path`/
`iter_markdown_files` only ever walk `config.content_dirs`, which can
never include this directory. There is no permission check being relied
on to keep ordinary writes out — the files are simply outside every path
ordinary code looks at. Only this module, and the dedicated `brain
principal`/`brain group` CLI commands built on it, ever read or write
here (hardening requirement 1: "ordinary write/update/note/project
capabilities MUST NOT be able to modify them").

For the same reason, these records are never written into `notes`/
`notes_fts` (indexer.py) — the tables `search()`/`get_context()` actually
query — so they can never surface through ordinary search/context/memory
retrieval (hardening requirement 5).

No SQLite projection at all: every function here reads live Markdown,
every time. At the scale this is designed for (a handful of principals,
a handful of groups) a cache would only add an invalidation problem
without a performance reason to accept one — see hardening requirement 2
("the index is never authoritative for security decisions... a stale
index must never broaden access"). `is_active()` in particular is the
one function every enforcement point (the SSH dispatcher, the gateway's
login step) calls, and it must never be able to report a stale answer.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from . import audit
from . import frontmatter
from .capture import slugify
from .paths import Config

IDENTITY_DIRNAME = "90_SYSTEM/identity"
PRINCIPALS_SUBDIR = "principals"
GROUPS_SUBDIR = "groups"

VALID_KINDS = ("human", "service")
VALID_STATUSES = ("active", "disabled")


class IdentityError(ValueError):
    """A principal/group operation refused by this module's own rules —
    never a filesystem/parse error, which propagates as-is."""


def _principals_dir(config: Config) -> Path:
    return config.brain_root / IDENTITY_DIRNAME / PRINCIPALS_SUBDIR


def _groups_dir(config: Config) -> Path:
    return config.brain_root / IDENTITY_DIRNAME / GROUPS_SUBDIR


def _principal_path(config: Config, principal_id: str) -> Path:
    return _principals_dir(config) / f"{principal_id}.md"


def _group_path(config: Config, group_id: str) -> Path:
    return _groups_dir(config) / f"{group_id}.md"


def _list_members(meta: dict) -> list[str]:
    members = meta.get("members") or []
    if isinstance(members, str):
        return [members]
    return list(members)


# ---- principals -----------------------------------------------------------

@dataclass
class Principal:
    id: str
    display_name: str
    kind: str          # human | service
    status: str         # active | disabled
    role: str            # a deployment-defined role id (e.g. "adult") — never
                          # enumerated here; the public engine knows nothing
                          # about what roles exist, only that one is named.
    created: str
    updated: str

    @property
    def is_active(self) -> bool:
        return self.status == "active"


def _principal_from_note(note: frontmatter.Note) -> Principal:
    return Principal(
        id=note.id, display_name=str(note.meta.get("display_name", "")),
        kind=str(note.meta.get("kind", "human")), status=str(note.meta.get("status", "active")),
        role=str(note.meta.get("role", "")),
        created=str(note.meta.get("created", "")), updated=str(note.meta.get("updated", "")),
    )


def create_principal(config: Config, *, display_name: str, kind: str = "human",
                      role: str = "", principal_id: str | None = None) -> Principal:
    if kind not in VALID_KINDS:
        raise IdentityError(f"kind must be one of {VALID_KINDS}")
    pid = principal_id or f"principal-{slugify(display_name)}"
    path = _principal_path(config, pid)
    if path.exists():
        raise IdentityError(f"principal '{pid}' already exists")
    today = dt.date.today().isoformat()
    meta = {
        "id": pid, "type": "principal", "display_name": display_name,
        "kind": kind, "status": "active", "role": role,
        "created": today, "updated": today,
    }
    note = frontmatter.Note(
        path=path, meta=meta,
        body=f"# {display_name}\n\nSAKO Brain principal record — identity/authorization "
             f"metadata only, never biographical content (see any linked `type: person` "
             f"record for that).\n",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.render(note), encoding="utf-8")
    audit.log_event(config, event="principal.create", principal_id=pid,
                     detail=f"kind={kind} role={role or '(none)'}")
    return _principal_from_note(note)


def get_principal(config: Config, principal_id: str) -> Principal | None:
    path = _principal_path(config, principal_id)
    if not path.exists():
        return None
    return _principal_from_note(frontmatter.parse_file(path))


def list_principals(config: Config) -> list[Principal]:
    d = _principals_dir(config)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            out.append(_principal_from_note(frontmatter.parse_file(p)))
        except frontmatter.FrontmatterError:
            continue
    return out


def set_principal_status(config: Config, principal_id: str, status: str) -> Principal:
    if status not in VALID_STATUSES:
        raise IdentityError(f"status must be one of {VALID_STATUSES}")
    path = _principal_path(config, principal_id)
    if not path.exists():
        raise IdentityError(f"no principal '{principal_id}'")
    note = frontmatter.parse_file(path)
    note.meta["status"] = status
    note.meta["updated"] = dt.date.today().isoformat()
    path.write_text(frontmatter.render(note), encoding="utf-8")
    audit.log_event(config, event="principal.status", principal_id=principal_id, detail=status)
    return _principal_from_note(note)


def set_principal_role(config: Config, principal_id: str, role: str) -> Principal:
    path = _principal_path(config, principal_id)
    if not path.exists():
        raise IdentityError(f"no principal '{principal_id}'")
    note = frontmatter.parse_file(path)
    note.meta["role"] = role
    note.meta["updated"] = dt.date.today().isoformat()
    path.write_text(frontmatter.render(note), encoding="utf-8")
    audit.log_event(config, event="principal.role", principal_id=principal_id, detail=role)
    return _principal_from_note(note)


def delete_principal(config: Config, principal_id: str) -> None:
    """Permanently removes a principal record — unlike every other
    identity mutation, which prefers a status change over deletion
    (`set_principal_status`), this is a genuine delete, for cleaning up
    throwaway/mistaken records (e.g. a test principal) that were never
    referenced by any real credential, audience, or group membership.
    Refuses to delete a principal that is still a member of any group,
    so cleanup never silently orphans a dangling audience reference —
    remove it from the group first."""
    path = _principal_path(config, principal_id)
    if not path.exists():
        raise IdentityError(f"no principal '{principal_id}'")
    referencing = [g.id for g in list_groups(config) if principal_id in g.members]
    if referencing:
        raise IdentityError(
            f"'{principal_id}' is still a member of {referencing} — remove from each group first"
        )
    path.unlink()
    audit.log_event(config, event="principal.delete", principal_id=principal_id)


def break_glass_restore_admin(config: Config, principal_id: str, role: str) -> Principal:
    """The one documented recovery path for a bad ACL/group/role change
    locking the admin out (hardening requirement 6). Unconditionally
    forces a principal back to status=active with the given role,
    bypassing nothing else — it does not touch any other principal,
    any group, or any record's audience, so it cannot be used as a
    general-purpose bypass.

    Deliberately NOT wired into a server deployment's SSH dispatcher
    allowlists under any mode — there is no SSH identity, including the
    vault owner's own, that can invoke this remotely. It is reachable
    only by running `brain` directly on the machine that holds the
    canonical vault, which is the "local/server-side, not exposed over
    public Remote MCP" requirement by construction rather than by a
    permission check this code enforces. Every call is audited with a
    distinct, impossible-to-miss event name."""
    if get_principal(config, principal_id) is None:
        create_principal(config, display_name=principal_id.removeprefix("principal-"),
                          role=role, principal_id=principal_id)
    path = _principal_path(config, principal_id)
    note = frontmatter.parse_file(path)
    note.meta["status"] = "active"
    note.meta["role"] = role
    note.meta["updated"] = dt.date.today().isoformat()
    path.write_text(frontmatter.render(note), encoding="utf-8")
    audit.log_event(config, event="principal.BREAK_GLASS", principal_id=principal_id,
                     transport="local", detail=f"role={role}")
    return _principal_from_note(note)


def is_active(config: Config, principal_id: str) -> bool:
    """True only if the principal exists AND is active — read live from
    canonical Markdown on every call, never cached. The single function
    every enforcement point (the SSH dispatcher, the gateway's login
    step) must call; see the module docstring."""
    p = get_principal(config, principal_id)
    return p is not None and p.is_active


# ---- groups -----------------------------------------------------------
# Sharing audience only — who may see content. Never an operational
# permission; see scopes/roles for that (hardening requirement 2:
# groups and roles are kept deliberately separate).

@dataclass
class Group:
    id: str
    display_name: str
    members: list[str]
    created: str
    updated: str


def _group_from_note(note: frontmatter.Note) -> Group:
    return Group(
        id=note.id, display_name=str(note.meta.get("display_name", "")),
        members=_list_members(note.meta),
        created=str(note.meta.get("created", "")), updated=str(note.meta.get("updated", "")),
    )


def create_group(config: Config, *, display_name: str, group_id: str | None = None) -> Group:
    gid = group_id or f"group-{slugify(display_name)}"
    path = _group_path(config, gid)
    if path.exists():
        raise IdentityError(f"group '{gid}' already exists")
    today = dt.date.today().isoformat()
    meta = {
        "id": gid, "type": "group", "display_name": display_name, "members": [],
        "created": today, "updated": today,
    }
    note = frontmatter.Note(
        path=path, meta=meta,
        body=f"# {display_name}\n\nSAKO Brain group — a sharing audience (who may see "
             f"content tagged with this group), never an operational role.\n",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.render(note), encoding="utf-8")
    audit.log_event(config, event="group.create", detail=gid)
    return _group_from_note(note)


def get_group(config: Config, group_id: str) -> Group | None:
    path = _group_path(config, group_id)
    if not path.exists():
        return None
    return _group_from_note(frontmatter.parse_file(path))


def list_groups(config: Config) -> list[Group]:
    d = _groups_dir(config)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            out.append(_group_from_note(frontmatter.parse_file(p)))
        except frontmatter.FrontmatterError:
            continue
    return out


def add_group_member(config: Config, group_id: str, principal_id: str) -> Group:
    if get_principal(config, principal_id) is None:
        raise IdentityError(f"no principal '{principal_id}' — cannot add to a group")
    path = _group_path(config, group_id)
    if not path.exists():
        raise IdentityError(f"no group '{group_id}'")
    note = frontmatter.parse_file(path)
    members = _list_members(note.meta)
    if principal_id not in members:
        members.append(principal_id)
    note.meta["members"] = members
    note.meta["updated"] = dt.date.today().isoformat()
    path.write_text(frontmatter.render(note), encoding="utf-8")
    audit.log_event(config, event="group.member.add", principal_id=principal_id, detail=group_id)
    return _group_from_note(note)


def remove_group_member(config: Config, group_id: str, principal_id: str) -> Group:
    path = _group_path(config, group_id)
    if not path.exists():
        raise IdentityError(f"no group '{group_id}'")
    note = frontmatter.parse_file(path)
    members = [m for m in _list_members(note.meta) if m != principal_id]
    note.meta["members"] = members
    note.meta["updated"] = dt.date.today().isoformat()
    path.write_text(frontmatter.render(note), encoding="utf-8")
    audit.log_event(config, event="group.member.remove", principal_id=principal_id, detail=group_id)
    return _group_from_note(note)


def groups_for_principal(config: Config, principal_id: str) -> list[str]:
    return [g.id for g in list_groups(config) if principal_id in g.members]
