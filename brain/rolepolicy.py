"""Role policy — pre-onboarding hardening (2026-10-02, Marcin's explicit
request): minimal, generic server-side enforcement of what a *role* may
do, layered on top of (never a replacement for) Stage 2's existing
owner/audience visibility model.

Before this module existed, `role` was consulted in exactly one place in
the whole codebase (`validate.is_admin_principal`, gating `brain doctor`/
`brain state` only) — every other role was a label with no behavioural
difference. That is not sufficient for a real family deployment: a
`restricted_child` must be functionally different from an `adult`, not
just named differently.

Deliberately NOT full RBAC — two capabilities only, exactly matching what
was asked for:

  - can_write_restricted: may a record owned by this role be written with
    sensitivity='restricted' at all. Independent of, and in addition to,
    the existing confirm_restricted friction gate (writepolicy.py) — that
    gate is "did you mean to write restricted content"; this one is "is
    this role allowed to write restricted content at all".
  - audience_allowlist: which audience targets (already-normalized
    principal-x / group-y ids) this role may name when sharing one of its
    OWN records. None means unrestricted (every role with no matching
    policy record behaves exactly as Stage 2 already does today — this is
    a pure addition, nothing already working changes unless an admin
    explicitly creates a policy that narrows it). An empty list means
    "may never share beyond private".

The engine has ZERO hardcoded knowledge of any specific role name —
"admin", "adult", "standard_child", "restricted_child" never appear
below. A role policy is deployment configuration, created the same way a
Group is: an ordinary record an admin sets up for THIS deployment, keyed
purely by whatever string happens to be in a Principal's own `role`
field. A different deployment could use entirely different role names
and this module would not need to change.

Groups and roles stay orthogonal: a group still decides who may SEE a
shared record (audience/visibility.py, unchanged); a role policy decides
what a principal may DO (write restricted content, which audiences it
may name) — never who may see what. Neither reads the other.

Stored under the same `90_SYSTEM/identity/` tree as principals/groups —
same structural, local-only isolation (never a content directory
`iter_markdown_files` walks, never added to the SSH dispatcher's
allowlist): administering role policy is itself an authorization
decision, exactly like administering principals/groups, not ordinary
content. `brain role-policy` is a local-CLI-only command tree for the
same reason `brain principal`/`brain group` already are.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

from . import audit
from . import frontmatter
from . import identity
from . import visibility
from .paths import Config

POLICIES_SUBDIR = "role-policies"


class RolePolicyError(ValueError):
    """A write refused by role policy — distinct from VisibilityError
    (which is about who can see what) and UpdateError/ProjectWriteError/etc.
    (which are about ownership) — this is specifically "your role doesn't
    permit this operation"."""


@dataclasses.dataclass(frozen=True)
class RolePolicy:
    can_write_restricted: bool
    audience_allowlist: list[str] | None  # normalized ids (principal-x/group-y); None = unrestricted


DEFAULT_POLICY = RolePolicy(can_write_restricted=True, audience_allowlist=None)

# Pre-onboarding hardening, round 2 (2026-10-02): the fully-permissive
# DEFAULT_POLICY above must NEVER be what a real, active, non-admin
# principal silently falls back to when its role fails to resolve a
# policy (typo, deleted policy, never configured, malformed record,
# deployment drift) -- that would turn a configuration error into a
# privilege expansion, exactly the failure Marcin named explicitly. This
# is the strictest point in the whole capability space: no restricted
# writes, no sharing beyond private. Resolving to this can therefore
# never make any role MORE permissive than its own intended policy,
# whatever that policy turns out to be.
FAIL_CLOSED_POLICY = RolePolicy(can_write_restricted=False, audience_allowlist=[])

# Roles exempt from needing an explicit policy record at all -- "admin"
# because validate.is_admin_principal already grants it full capability
# with no role-policy involvement whatsoever (same exemption, same
# reasoning, see validate.py), and "" (no role assigned) because that is
# every pre-Stage-1/pre-role-policy identity's own established default,
# never a new restriction. Anything else -- including a typo that merely
# LOOKS like one of these -- is a real role and must resolve a real policy.
_EXEMPT_FROM_POLICY_REQUIREMENT = {"", "admin"}


def _policies_dir(config: Config) -> Path:
    return config.brain_root / identity.IDENTITY_DIRNAME / POLICIES_SUBDIR


def _policy_path(config: Config, role: str) -> Path:
    return _policies_dir(config) / f"role-policy-{role}.md"


def _policy_from_note(note: frontmatter.Note) -> RolePolicy:
    """Raises visibility.VisibilityError on a malformed stored ref -- never
    silently drops it. Both callers below decide what "malformed" means
    for their own purpose (resolve_role_policy treats it as no-policy-at-
    all/fail closed; get_role_policy, a display/admin convenience, treats
    it as not-yet-configured)."""
    raw_allowlist = note.meta.get("audience_allowlist")
    if raw_allowlist is None:
        allowlist = None
    else:
        if isinstance(raw_allowlist, str):
            raw_allowlist = [raw_allowlist]
        allowlist = [visibility._ref_to_id(a) for a in raw_allowlist]
    return RolePolicy(
        can_write_restricted=bool(note.meta.get("can_write_restricted", True)),
        audience_allowlist=allowlist,
    )


def resolve_role_policy(config: Config, role: str) -> RolePolicy | None:
    """The STRICT resolver: None means "no valid policy for this role at
    all" (no record, unparseable record, or a malformed stored ref) --
    never DEFAULT_POLICY. Used wherever the answer feeds a security
    decision (policy_for_principal, principal activation). Never caches:
    same live-read discipline as identity.is_active()/visibility.py, so a
    policy deleted or corrupted after a credential was issued takes effect
    on that principal's very next request, not just its next login."""
    if not role:
        return None
    path = _policy_path(config, role)
    if not path.exists():
        return None
    try:
        return _policy_from_note(frontmatter.parse_file(path))
    except (frontmatter.FrontmatterError, visibility.VisibilityError):
        return None


def set_role_policy(config: Config, *, role: str, can_write_restricted: bool = True,
                     audience_allowlist: list[str] | None = None) -> RolePolicy:
    """Create or replace the policy for one role string. `audience_allowlist`
    takes the same type-prefixed ref form as a record's own `audience`
    field (['group:household', 'principal:ania']); pass [] (not None) to
    mean "may never share beyond private", and omit/None for unrestricted."""
    if not role:
        raise RolePolicyError("role must be non-empty")
    path = _policy_path(config, role)
    today = dt.date.today().isoformat()
    # Normalize + validate refs up front, same discipline as every other
    # audience-accepting write path — a malformed ref must fail the write,
    # never land and silently fail open at enforcement time.
    normalized = None if audience_allowlist is None else [visibility._ref_to_id(a) for a in audience_allowlist]
    meta = {
        "id": f"role-policy-{role}", "type": "role-policy", "role": role,
        "can_write_restricted": can_write_restricted,
        "audience_allowlist": list(audience_allowlist) if audience_allowlist is not None else None,
        "created": today, "updated": today,
    }
    if path.exists():
        existing = frontmatter.parse_file(path)
        meta["created"] = existing.meta.get("created", today)
    note = frontmatter.Note(
        path=path, meta=meta,
        body=f"# Role policy — {role}\n\nSAKO Brain role policy — authorization metadata only, "
             f"never content. Applies to every principal whose own `role` field equals `{role}`.\n",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.render(note), encoding="utf-8")
    audit.log_event(config, event="role_policy.set", detail=role)
    return RolePolicy(can_write_restricted=can_write_restricted, audience_allowlist=normalized)


def get_role_policy(config: Config, role: str) -> RolePolicy:
    """Display/admin convenience only (e.g. `brain role-policy show`) —
    reports DEFAULT_POLICY for an empty role, a role with no policy record
    yet, or a malformed one, so an admin inspecting a role always gets an
    answer rather than an exception. NEVER call this for a security
    decision — use resolve_role_policy (strict) via policy_for_principal,
    which treats exactly the same three cases as fail-closed instead."""
    resolved = resolve_role_policy(config, role)
    return resolved if resolved is not None else DEFAULT_POLICY


def list_role_policies(config: Config) -> list[tuple[str, RolePolicy]]:
    d = _policies_dir(config)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        try:
            note = frontmatter.parse_file(p)
        except frontmatter.FrontmatterError:
            continue
        role = str(note.meta.get("role", ""))
        if role:
            out.append((role, _policy_from_note(note)))
    return out


def delete_role_policy(config: Config, role: str) -> None:
    path = _policy_path(config, role)
    if not path.exists():
        raise RolePolicyError(f"no role policy for '{role}'")
    path.unlink()
    audit.log_event(config, event="role_policy.delete", detail=role)


def policy_for_principal(config: Config, principal_id: str) -> RolePolicy:
    """What policy actually governs this principal right now — reads the
    principal's live `role` field, then that role's live policy record.
    Never cached, same discipline as identity.is_active()/visibility.py's
    own live-read rule: a stale answer here could broaden access.

    Fail-closed invariant (Marcin's own explicit security requirement,
    2026-10-02): an active principal whose role is neither exempt ("",
    "admin") nor resolves a real policy gets FAIL_CLOSED_POLICY, never
    DEFAULT_POLICY. A typo, a deleted/malformed policy record, or
    deployment drift must never silently become a privilege expansion.
    An inactive principal gets the same fail-closed treatment — visibility
    enforcement handles "disabled" at the read/write-ownership layer, but
    nothing here should wait for that; defense in depth costs nothing.

    The one true exemption from needing a principal record at all is the
    same one validate.is_admin_principal already relies on: a vault with
    NO principal records whatsoever hasn't bootstrapped multi-user yet,
    so it stays fully permissive (this module existing must never, by
    itself, break a single-user vault). A *specific* unknown id in an
    already-populated vault (deleted principal, typo'd id) is a different
    situation entirely and fails closed, exactly like is_admin_principal's
    own split."""
    p = identity.get_principal(config, principal_id)
    if p is None:
        return DEFAULT_POLICY if not identity.list_principals(config) else FAIL_CLOSED_POLICY
    if not p.is_active:
        return FAIL_CLOSED_POLICY
    if p.role in _EXEMPT_FROM_POLICY_REQUIREMENT:
        # "admin": same exemption as validate.is_admin_principal, which has
        # never consulted a role-policy record either — principal-marcin's
        # own backward-compatibility case is fully covered here, since
        # production bootstraps marcin with role="admin" explicitly
        # (10-setup-brain.sh); no separate legacy carve-out is needed.
        # "": every pre-Stage-1/pre-role-policy identity's own established
        # default — never a new restriction on a principal nobody has
        # assigned a role to at all.
        return DEFAULT_POLICY
    resolved = resolve_role_policy(config, p.role)
    return resolved if resolved is not None else FAIL_CLOSED_POLICY


def require_valid_policy_for_activation(config: Config, role: str) -> None:
    """The create-time/activation-time half of the fail-closed invariant —
    policy_for_principal is the runtime half, covering drift that happens
    AFTER activation (a policy deleted or corrupted later, which no check
    made at creation time can see into the future to prevent). Called by
    identity.create_principal (when creating directly as active) and
    identity.set_principal_status (on every transition INTO status=active)
    so there is no interval where an active, non-exempt-role principal
    exists without a resolvable policy already in place. Raises
    RolePolicyError -- callers in identity.py wrap it as IdentityError to
    keep that module's own exception contract intact."""
    if role in _EXEMPT_FROM_POLICY_REQUIREMENT:
        return
    if resolve_role_policy(config, role) is None:
        raise RolePolicyError(
            f"role '{role}' has no valid role policy -- create one first with "
            f"'brain role-policy set {role} ...' before this principal can be active"
        )


def require_restricted_write_allowed(config: Config, principal_id: str, sensitivity: str) -> None:
    """Refuses a sensitivity='restricted' write outright when the acting
    principal's role policy says no — independent of, and checked in
    addition to, the existing confirm_restricted friction gate."""
    if sensitivity != "restricted":
        return
    policy = policy_for_principal(config, principal_id)
    if not policy.can_write_restricted:
        raise RolePolicyError(
            f"'{principal_id}'s role does not permit writing sensitivity='restricted' content"
        )


def require_audience_allowed(config: Config, principal_id: str, audience: list[str] | None) -> None:
    """Refuses sharing to any audience target outside the acting principal's
    own role-policy allowlist. A malformed ref is refused the same way
    visibility.py refuses one at read time — denies rather than raising
    past the caller with something unreadable."""
    if not audience:
        return
    policy = policy_for_principal(config, principal_id)
    if policy.audience_allowlist is None:
        return
    allowed = set(policy.audience_allowlist)
    requested_ids = []
    for ref in audience:
        try:
            requested_ids.append(visibility._ref_to_id(ref))
        except visibility.VisibilityError:
            requested_ids.append(str(ref))
    disallowed = [r for r in requested_ids if r not in allowed]
    if disallowed:
        raise RolePolicyError(
            f"'{principal_id}'s role may not share with {disallowed} — "
            f"allowed targets: {sorted(allowed) or '(none)'}"
        )
