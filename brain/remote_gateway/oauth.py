"""OAuth 2.1 authorization server for the remote gateway (v0.12.0).

Implements exactly the subset of OAuth 2.1 / RFC 7591 (Dynamic Client
Registration) / RFC 7636 (PKCE) / RFC 8707 (Resource Indicators) / RFC 9728
(Protected Resource Metadata) / RFC 8414 (Authorization Server Metadata)
that the MCP authorization spec requires — see
`modelcontextprotocol.io/specification/.../basic/authorization` (fetched
and followed verbatim during design).

On "don't invent your own cryptography": every actual cryptographic
operation here is a direct call into Python's standard library —
`secrets.token_urlsafe` (CSPRNG) for every code/token, `hashlib.sha256`
for PKCE S256 challenge verification exactly as RFC 7636 defines it, and
`secrets.compare_digest` for every credential comparison. No cipher,
signature scheme, or key-derivation function is designed here. What *is*
written here is OAuth *protocol bookkeeping* (which grant is valid when,
code/token lifetimes, one-time-use enforcement) — necessarily
implementation-specific for MCP's exact (and still-new) metadata
requirements, which no general-purpose OAuth library yet covers
end-to-end. Tokens are opaque and looked up server-side (not JWTs): this
is a single resource server validating tokens it alone issued, so there
is no signature-verification surface to get wrong, and revocation is a
row delete rather than a blocklist.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from . import scopes as scopes_mod
from .storage import Storage

ACCESS_TOKEN_TTL_SECONDS = 60 * 60          # 1 hour — short-lived per OAuth 2.1 guidance
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days, rotated on every use
AUTH_CODE_TTL_SECONDS = 120                  # 2 minutes — just long enough for the redirect


class OAuthError(Exception):
    """Carries the OAuth `error` code (RFC 6749 Section 5.2 / 4.1.2.1)."""

    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description
        self.status = status


@dataclass
class TokenInfo:
    client_id: str
    principal_id: str
    scope: frozenset[str]
    resource: str


class AuthorizationServer:
    def __init__(self, storage: Storage, issuer: str, resource: str):
        self.storage = storage
        self.issuer = issuer.rstrip("/")
        self.resource = resource.rstrip("/")

    # ---- Dynamic Client Registration (RFC 7591) ------------------------

    def register_client(self, metadata: dict) -> dict:
        redirect_uris = metadata.get("redirect_uris")
        if not redirect_uris or not isinstance(redirect_uris, list):
            raise OAuthError("invalid_client_metadata", "redirect_uris is required")
        for uri in redirect_uris:
            if not (uri.startswith("https://") or uri.startswith("http://127.0.0.1")
                    or uri.startswith("http://localhost")):
                raise OAuthError("invalid_redirect_uri",
                                  f"{uri!r} must be https:// or a loopback http:// URI")
        client_name = str(metadata.get("client_name") or "unnamed MCP client")[:200]

        client_id = secrets.token_urlsafe(16)
        registration_access_token = secrets.token_urlsafe(32)
        self.storage.create_client(
            client_id=client_id, client_name=client_name, redirect_uris=redirect_uris,
            token_endpoint_auth_method="none",
            registration_access_token_hash=_hash(registration_access_token),
        )
        return {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "registration_access_token": registration_access_token,
            "registration_client_uri": f"{self.issuer}/register/{client_id}",
            "client_id_issued_at": int(time.time()),
        }

    # ---- Authorization request validation -------------------------------

    def validate_authorize_request(self, params: dict) -> dict:
        response_type = params.get("response_type")
        client_id = params.get("client_id")
        redirect_uri = params.get("redirect_uri")
        code_challenge = params.get("code_challenge")
        code_challenge_method = params.get("code_challenge_method")
        resource = params.get("resource")

        if response_type != "code":
            raise OAuthError("unsupported_response_type", "only 'code' is supported")
        if not client_id:
            raise OAuthError("invalid_request", "client_id is required")
        client = self.storage.get_client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "unknown client_id", status=401)
        if not redirect_uri or redirect_uri not in client["redirect_uris"]:
            # Per spec: do NOT redirect on a bad/unregistered redirect_uri —
            # that is exactly the open-redirect vector PKCE/OAuth 2.1 closes.
            raise OAuthError("invalid_request", "redirect_uri not registered for this client")
        if not code_challenge:
            raise OAuthError("invalid_request", "code_challenge (PKCE) is required")
        if code_challenge_method != "S256":
            raise OAuthError("invalid_request", "only S256 code_challenge_method is supported")
        if not resource:
            raise OAuthError("invalid_target", "resource parameter (RFC 8707) is required")
        if resource.rstrip("/") != self.resource:
            raise OAuthError("invalid_target", f"resource must be {self.resource}")

        requested_scope = (params.get("scope") or scopes_mod.SCOPES_SUPPORTED).split()
        unknown = set(requested_scope) - set(scopes_mod.ALL_SCOPES)
        if unknown:
            raise OAuthError("invalid_scope", f"unknown scope(s): {' '.join(sorted(unknown))}")

        return {
            "client": client, "client_id": client_id, "redirect_uri": redirect_uri,
            "code_challenge": code_challenge, "code_challenge_method": code_challenge_method,
            "requested_scope": requested_scope, "resource": resource,
            "state": params.get("state"),
        }

    # ---- Owner consent -> authorization code -----------------------------

    def issue_code(self, client_id: str, principal_id: str, redirect_uri: str, code_challenge: str,
                   code_challenge_method: str, granted_scope: list[str], resource: str) -> str:
        code = secrets.token_urlsafe(32)
        self.storage.save_code(
            code=code, client_id=client_id, principal_id=principal_id, redirect_uri=redirect_uri,
            code_challenge=code_challenge, code_challenge_method=code_challenge_method,
            scope=" ".join(granted_scope), resource=resource, ttl_seconds=AUTH_CODE_TTL_SECONDS,
        )
        return code

    @staticmethod
    def build_redirect(redirect_uri: str, code: str | None = None, state: str | None = None,
                        error: str | None = None, error_description: str | None = None) -> str:
        params = {}
        if code is not None:
            params["code"] = code
        if error is not None:
            params["error"] = error
            if error_description:
                params["error_description"] = error_description
        if state is not None:
            params["state"] = state
        sep = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{sep}{urlencode(params)}"

    # ---- Token endpoint --------------------------------------------------

    def exchange_code(self, code: str, client_id: str, redirect_uri: str, code_verifier: str) -> dict:
        row = self.storage.consume_code(code)
        if row is None:
            raise OAuthError("invalid_grant", "unknown, expired, or already-used code")
        if row["client_id"] != client_id:
            raise OAuthError("invalid_grant", "code was not issued to this client")
        if row["redirect_uri"] != redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        if not _verify_pkce(code_verifier, row["code_challenge"], row["code_challenge_method"]):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        return self._issue_tokens(client_id, row["principal_id"], row["scope"].split(), row["resource"])

    def refresh(self, refresh_token: str, client_id: str, requested_scope: list[str] | None) -> dict:
        row = self.storage.get_by_refresh_token(refresh_token)
        if row is None or row["revoked_at"] is not None:
            raise OAuthError("invalid_grant", "unknown or revoked refresh token")
        if row["client_id"] != client_id:
            raise OAuthError("invalid_grant", "refresh token was not issued to this client")
        if row["refresh_expires_at"] and row["refresh_expires_at"] < time.time():
            raise OAuthError("invalid_grant", "refresh token expired")
        granted = row["scope"].split()
        if requested_scope:
            # A refresh MAY narrow scope, never widen it (RFC 6749 6).
            if set(requested_scope) - set(granted):
                raise OAuthError("invalid_scope", "cannot widen scope on refresh")
            granted = requested_scope
        # Rotation: the old refresh token is revoked the instant its
        # replacement is issued, whether or not the caller ever uses the
        # new one — an OAuth 2.1 MUST for public clients.
        self.storage.revoke_token(refresh_token)
        return self._issue_tokens(client_id, row["principal_id"], granted, row["resource"])

    def _issue_tokens(self, client_id: str, principal_id: str, scope: list[str], resource: str) -> dict:
        # Pre-onboarding hardening, P1 (2026-10-06): a usable token must
        # NEVER be issued with no principal bound, in multi-user mode or
        # otherwise — the permanent guard behind a real production
        # incident where a pre-Stage-1 refresh_token kept rotating an
        # empty principal_id forward indefinitely (storage.py's
        # _migrate_legacy_tokens is the one-time cure for rows that
        # already exist; this is what stops it from ever being reachable
        # again, from exchange_code, refresh, or any future grant type).
        # Every legitimate caller of this function already has a real
        # principal_id by construction (session-authenticated at login,
        # or carried forward from an already-valid row) — this should
        # never actually fire; it exists so a gap like this one fails
        # loudly instead of silently shipping a useless token.
        if not principal_id:
            raise OAuthError("server_error", "no principal bound to this grant — cannot issue a token", status=500)
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        self.storage.save_token(
            access_token=access_token, refresh_token=refresh_token, client_id=client_id,
            principal_id=principal_id, scope=" ".join(scope), resource=resource,
            access_ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
            refresh_ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "refresh_token": refresh_token,
            "scope": " ".join(scope),
        }

    # ---- Resource-server side: validating an incoming Bearer token ----

    def validate_token(self, access_token: str) -> TokenInfo:
        row = self.storage.get_token(access_token)
        if row is None:
            raise OAuthError("invalid_token", "unknown access token", status=401)
        if row["revoked_at"] is not None:
            raise OAuthError("invalid_token", "token revoked", status=401)
        if row["expires_at"] < time.time():
            raise OAuthError("invalid_token", "token expired", status=401)
        if row["resource"].rstrip("/") != self.resource:
            # Audience binding (RFC 8707 / MCP spec "Access Token Privilege
            # Restriction") — a token issued for a different resource must
            # never be accepted here, even if otherwise well-formed.
            raise OAuthError("invalid_token", "token audience does not match this resource", status=401)
        # Deliberately NOT a live principal.status check here — this
        # process has no vault access to do one honestly. The SSH
        # dispatcher is the authoritative enforcement point (hardening
        # requirement 3): it reads live canonical state and sits on every
        # real tool call this token could ever be used to make, so a
        # disabled principal's very next tool call is refused there
        # regardless of this token's own validity. Duplicating a
        # same-process "is active" flag here would only be a cache this
        # gateway cannot keep honestly fresh — see storage.py's SCHEMA
        # comment on the same point.
        if not row["principal_id"]:
            # Defense in depth, P1 (2026-10-06): _issue_tokens already
            # refuses to create a token like this going forward, and
            # storage.py's migration backfills every row that already
            # existed — this should be unreachable. If it ever is anyway
            # (a future bug, a hand-edited row), fail closed here rather
            # than handing mcp_transport a token nothing can meaningfully
            # act as.
            raise OAuthError("invalid_token", "token has no principal bound", status=401)
        self.storage.touch_token(access_token)
        return TokenInfo(client_id=row["client_id"], principal_id=row["principal_id"],
                          scope=frozenset(row["scope"].split()), resource=row["resource"])

    def revoke(self, token_value: str) -> None:
        self.storage.revoke_token(token_value)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method != "S256" or not code_verifier:
        return False
    import base64
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, code_challenge)


# ---- Discovery metadata documents (RFC 8414 / RFC 9728) -----------------

def authorization_server_metadata(issuer: str) -> dict:
    issuer = issuer.rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "registration_endpoint": f"{issuer}/register",
        "revocation_endpoint": f"{issuer}/revoke",
        "scopes_supported": list(scopes_mod.ALL_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def protected_resource_metadata(resource: str, issuer: str) -> dict:
    return {
        "resource": resource.rstrip("/"),
        "authorization_servers": [issuer.rstrip("/")],
        "scopes_supported": list(scopes_mod.ALL_SCOPES),
        "bearer_methods_supported": ["header"],
    }
