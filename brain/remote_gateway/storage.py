"""SQLite-backed storage for the remote gateway's OAuth state (v0.12.0).

Deliberately separate from the Brain vault and from the vault's own
runtime-state SQLite index — this is gateway operational state (clients,
codes, tokens), not Brain content, and lives under the gateway's own XDG
state directory. Opaque, server-generated tokens (not JWTs): this single
resource server validates its own tokens by lookup, so there is no key
material to manage, and revocation is a plain row delete rather than a
blocklist — see the module docstring in `oauth.py` for the full reasoning
on avoiding hand-rolled cryptography while still not depending on a
general-purpose OAuth framework.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .. import paths as paths_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,          -- JSON array
    token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
    client_secret_hash TEXT,              -- NULL for public clients
    registration_access_token_hash TEXT,  -- DCR client-management auth
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS authorization_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    principal_id TEXT NOT NULL DEFAULT '',
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    access_token TEXT PRIMARY KEY,
    refresh_token TEXT UNIQUE,
    client_id TEXT NOT NULL,
    principal_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL,
    resource TEXT NOT NULL,
    expires_at REAL NOT NULL,
    refresh_expires_at REAL,
    revoked_at REAL,
    created_at REAL NOT NULL,
    last_used_at REAL
);

-- SAKO Brain multi-user, Stage 1: one credential row per principal,
-- replacing the single-row "owner" table (kept below, untouched, for its
-- one-time migration — see Storage.__init__). principal_id is this
-- gateway's own local notion of who may log in; it is never the
-- authority on whether that principal still exists or is active — that
-- lives in the canonical vault (brain.identity) and is enforced at the
-- SSH dispatcher, the one place every real tool call actually passes
-- through, every time (hardening requirement 2/3).
CREATE TABLE IF NOT EXISTS credentials (
    principal_id TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Pre-Stage-1 schema, superseded by `credentials` above. Left in place
-- (never dropped) purely so Storage.__init__'s one-time migration has
-- something to read on an already-deployed database that still has a
-- row here — an empty/never-created table on a fresh install is just
-- unused weight, not a live concern.
CREATE TABLE IF NOT EXISTS owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS server_secret (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    secret TEXT NOT NULL
);

-- This gateway's own authentication/transport audit (hardening
-- requirement 7's "Gateway audit" half): login, consent, token
-- issuance/revoke, credential lifecycle. Never a record body or a
-- password — only identifiers and short, already-known-safe detail,
-- same discipline as brain.audit's Brain-core log.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    principal_id TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_client ON tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_codes_client ON authorization_codes(client_id);
"""
# idx_tokens_principal is deliberately NOT in SCHEMA above: on a database
# that predates Stage 1, `tokens` already exists without a `principal_id`
# column at the point executescript(SCHEMA) runs (CREATE TABLE IF NOT
# EXISTS is a no-op for an existing table — it never adds a column), so
# an unconditional CREATE INDEX on that column here would fail outright
# against any real pre-Stage-1 database. Created instead in __init__,
# after _ensure_column has guaranteed the column exists either way.
# Caught by reproducing a real legacy-shaped database locally — the
# hermetic gateway tests all build fresh v0.13.0-shaped databases from
# scratch, so this ordering bug had no way to surface there.
_PRINCIPAL_INDEX_DDL = "CREATE INDEX IF NOT EXISTS idx_tokens_principal ON tokens(principal_id)"

# The one principal every pre-Stage-1 identity has always implicitly
# been — matches brain-dispatch.py's own DEFAULT_PRINCIPAL and the
# Stage 1 rollout's explicit instruction ("existing identities become
# principal:marcin"), kept here (not imported) since the gateway has no
# dependency on the vault's own identity module — it only ever stores
# and checks an opaque principal_id string.
LEGACY_OWNER_PRINCIPAL_ID = "principal-marcin"


def default_db_path() -> Path:
    import os
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / paths_mod.APP_DIRNAME / "remote-gateway" / "gateway.db"


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    """ALTER TABLE ... ADD COLUMN, but idempotent — CREATE TABLE IF NOT
    EXISTS never retroactively adds a column to a table that already
    existed on disk (an already-deployed gateway.db predates
    `principal_id` on `tokens`/`authorization_codes`), and SQLite has no
    `ADD COLUMN IF NOT EXISTS` of its own."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


class Storage:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        _ensure_column(self._conn, "tokens", "principal_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(self._conn, "authorization_codes", "principal_id", "TEXT NOT NULL DEFAULT ''")
        self._conn.execute(_PRINCIPAL_INDEX_DDL)
        self._migrate_legacy_owner()
        self._conn.commit()
        paths_mod.ensure_private_file(self.db_path)

    def _migrate_legacy_owner(self) -> None:
        """One-time, idempotent: an already-set owner password (every
        gateway deployed before Stage 1 has exactly one) becomes
        principal-marcin's credential, so Marcin's already-configured
        password keeps working rather than needing to be re-set. Never
        overwrites a `credentials` row that already exists — once
        migrated (or once a real Stage-1-native credential is set), this
        is a no-op forever."""
        existing = self._conn.execute(
            "SELECT 1 FROM credentials WHERE principal_id = ?", (LEGACY_OWNER_PRINCIPAL_ID,)
        ).fetchone()
        if existing:
            return
        legacy = self._conn.execute(
            "SELECT password_hash, salt, updated_at FROM owner WHERE id = 1"
        ).fetchone()
        if legacy is None:
            return
        self._conn.execute(
            "INSERT INTO credentials (principal_id, password_hash, salt, updated_at) VALUES (?, ?, ?, ?)",
            (LEGACY_OWNER_PRINCIPAL_ID, legacy["password_hash"], legacy["salt"], legacy["updated_at"]),
        )

    @contextmanager
    def _cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # ---- clients ----------------------------------------------------

    def create_client(self, client_id: str, client_name: str, redirect_uris: list[str],
                       token_endpoint_auth_method: str = "none",
                       client_secret_hash: str | None = None,
                       registration_access_token_hash: str | None = None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO oauth_clients "
                "(client_id, client_name, redirect_uris, token_endpoint_auth_method, "
                " client_secret_hash, registration_access_token_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (client_id, client_name, json.dumps(redirect_uris), token_endpoint_auth_method,
                 client_secret_hash, registration_access_token_hash, time.time()),
            )

    def get_client(self, client_id: str) -> dict | None:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["redirect_uris"] = json.loads(d["redirect_uris"])
        return d

    def list_clients(self) -> list[dict]:
        with self._cursor() as cur:
            rows = cur.execute("SELECT client_id, client_name, redirect_uris, created_at "
                                "FROM oauth_clients ORDER BY created_at DESC").fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["redirect_uris"] = json.loads(d["redirect_uris"])
            out.append(d)
        return out

    def delete_client(self, client_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute("DELETE FROM tokens WHERE client_id = ?", (client_id,))
            cur.execute("DELETE FROM authorization_codes WHERE client_id = ?", (client_id,))
            cur.execute("DELETE FROM oauth_clients WHERE client_id = ?", (client_id,))
            return cur.rowcount > 0

    # ---- authorization codes -----------------------------------------

    def save_code(self, code: str, client_id: str, principal_id: str, redirect_uri: str,
                  code_challenge: str, code_challenge_method: str, scope: str, resource: str,
                  ttl_seconds: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO authorization_codes "
                "(code, client_id, principal_id, redirect_uri, code_challenge, code_challenge_method, "
                " scope, resource, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (code, client_id, principal_id, redirect_uri, code_challenge, code_challenge_method,
                 scope, resource, time.time() + ttl_seconds, time.time()),
            )

    def consume_code(self, code: str) -> dict | None:
        """One-time use: returns the code row if it was valid and unused,
        atomically marking it used in the same transaction. None if it
        never existed, already used, or expired."""
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM authorization_codes WHERE code = ?", (code,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            if d["used"] or d["expires_at"] < time.time():
                return None
            cur.execute("UPDATE authorization_codes SET used = 1 WHERE code = ?", (code,))
            return d

    # ---- tokens -------------------------------------------------------

    def save_token(self, access_token: str, refresh_token: str | None, client_id: str,
                   principal_id: str, scope: str, resource: str, access_ttl_seconds: int,
                   refresh_ttl_seconds: int | None) -> None:
        now = time.time()
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO tokens (access_token, refresh_token, client_id, principal_id, scope, "
                " resource, expires_at, refresh_expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (access_token, refresh_token, client_id, principal_id, scope, resource,
                 now + access_ttl_seconds,
                 (now + refresh_ttl_seconds) if refresh_ttl_seconds else None, now),
            )

    def get_token(self, access_token: str) -> dict | None:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM tokens WHERE access_token = ?", (access_token,)).fetchone()
        return dict(row) if row else None

    def get_by_refresh_token(self, refresh_token: str) -> dict | None:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM tokens WHERE refresh_token = ?", (refresh_token,)).fetchone()
        return dict(row) if row else None

    def touch_token(self, access_token: str) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE tokens SET last_used_at = ? WHERE access_token = ?",
                        (time.time(), access_token))

    def revoke_token(self, token_value: str) -> bool:
        """Revokes whichever token (access or refresh) matches — RFC 7009
        doesn't distinguish at the API boundary, and revoking either side
        of a pair should kill both (so a leaked refresh token can't keep
        minting fresh access tokens after the access token is revoked)."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE tokens SET revoked_at = ? WHERE (access_token = ? OR refresh_token = ?) "
                "AND revoked_at IS NULL",
                (time.time(), token_value, token_value),
            )
            return cur.rowcount > 0

    def revoke_all_for_client(self, client_id: str) -> int:
        with self._cursor() as cur:
            cur.execute("UPDATE tokens SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL",
                        (time.time(), client_id))
            return cur.rowcount

    def revoke_all_for_principal(self, principal_id: str) -> int:
        """Kill every outstanding token for one person, across every
        client they've ever connected — the "disable Wiktor entirely"
        runbook step, complementing revoke_all_for_client's "disable one
        compromised client without logging everyone else out" (hardening
        requirement 8's granularity, the other direction)."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE tokens SET revoked_at = ? WHERE principal_id = ? AND revoked_at IS NULL",
                (time.time(), principal_id),
            )
            return cur.rowcount

    def revoke_all(self) -> int:
        """Kill-switch: revoke every outstanding token, regardless of
        client — the `disable all remote access` runbook step."""
        with self._cursor() as cur:
            cur.execute("UPDATE tokens SET revoked_at = ? WHERE revoked_at IS NULL", (time.time(),))
            return cur.rowcount

    def purge_expired(self) -> int:
        now = time.time()
        with self._cursor() as cur:
            cur.execute("DELETE FROM authorization_codes WHERE expires_at < ?", (now,))
            cur.execute(
                "DELETE FROM tokens WHERE expires_at < ? AND "
                "(refresh_expires_at IS NULL OR refresh_expires_at < ?)", (now, now),
            )
            return cur.rowcount

    # ---- credentials (principal login for the consent screen) --------
    #
    # This table is this gateway's own, local notion of "who may log in
    # and with what password" — it is never the authority on whether a
    # principal_id still exists or is active (see the SCHEMA comment
    # above); only on whether the password presented for it is correct.

    def set_credential(self, principal_id: str, password_hash: str, salt: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO credentials (principal_id, password_hash, salt, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(principal_id) DO UPDATE SET "
                "password_hash=excluded.password_hash, salt=excluded.salt, updated_at=excluded.updated_at",
                (principal_id, password_hash, salt, time.time()),
            )

    def get_credential(self, principal_id: str) -> dict | None:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM credentials WHERE principal_id = ?", (principal_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_credential_principals(self) -> list[str]:
        """Every principal_id this gateway can authenticate a login for —
        used only to render the login form's principal picker, never for
        an authorization decision."""
        with self._cursor() as cur:
            rows = cur.execute("SELECT principal_id FROM credentials ORDER BY principal_id").fetchall()
        return [r["principal_id"] for r in rows]

    # ---- authentication/transport audit --------------------------------

    def log_event(self, *, event: str, principal_id: str = "", client_id: str = "", detail: str = "") -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (event, principal_id, client_id, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event, principal_id, client_id, detail[:200], time.time()),
            )

    def list_audit_events(self, limit: int = 100) -> list[dict]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- server secret (HMAC key for the owner-login session cookie) --

    def get_or_create_server_secret(self) -> str:
        import secrets
        with self._cursor() as cur:
            row = cur.execute("SELECT secret FROM server_secret WHERE id = 1").fetchone()
            if row:
                return row["secret"]
            secret = secrets.token_urlsafe(32)
            cur.execute("INSERT INTO server_secret (id, secret) VALUES (1, ?)", (secret,))
            return secret
