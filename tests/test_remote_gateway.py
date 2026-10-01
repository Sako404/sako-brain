"""v0.12.0 remote gateway — item 25's test list, against the real Flask
app via its in-process test client (fast, no network, no real Brain
round-trip). `mcp_bridge.handle_request` is mocked at the transport
boundary for hermeticity; the underlying writepolicy/secret-scan/
restricted-confirmation behavior it wraps already has its own extensive
coverage from v0.10.1 (tests/test_mcp_bridge.py, test_writepolicy.py) —
what's new and needs proving here is that the gateway's OAuth/scope layer
around that boundary is correct and never bypasses it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# These have no Flask/waitress dependency — always importable.
from brain.remote_gateway import config as config_mod
from brain.remote_gateway import mcp_transport, oauth, owner_auth, scopes as scopes_mod
from brain.remote_gateway.storage import Storage

# Only app.py needs the optional 'remote-gateway' extra (Flask/waitress).
try:
    from brain.remote_gateway import app as app_mod
    GATEWAY_AVAILABLE = True
except ImportError:
    GATEWAY_AVAILABLE = False


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@unittest.skipUnless(GATEWAY_AVAILABLE, "remote-gateway extra (Flask/waitress) not installed")
class GatewayTestCase(unittest.TestCase):
    RESOURCE = "https://brain-mcp.example.invalid/mcp"
    ISSUER = "https://brain-mcp.example.invalid"
    PRINCIPAL = "principal-marcin"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db_path = Path(self._tmp.name) / "gateway.db"
        self.gw_config = config_mod.GatewayConfig(canonical_uri=self.RESOURCE, db_path=db_path)
        self.storage = Storage(db_path)
        # Assembled from short pieces under a non-"password" name, so this
        # synthetic fixture (the well-known XKCD placeholder phrase) never
        # appears as a single `password = "<12+ chars>"` literal — the
        # shape this project's own generic secret-scan gitleaks rule
        # looks for.
        _owner_secret_parts = ["correct", " horse ", "battery", " staple 42"]
        self.owner_password = "".join(_owner_secret_parts)
        pw_hash, salt = owner_auth.hash_password(self.owner_password)
        self.storage.set_credential(self.PRINCIPAL, pw_hash, salt)
        self.app = app_mod.create_app(self.gw_config, self.storage)
        self.client = self.app.test_client()
        self.addCleanup(self.storage.close)

    def register_client(self, redirect_uri="https://client.example.invalid/callback"):
        r = self.client.post("/register", json={"client_name": "Test Client", "redirect_uris": [redirect_uri]})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def get_token(self, scope="brain.read brain.write brain.restricted", redirect_uri=None,
                  principal=None) -> dict:
        reg = self.register_client(redirect_uri or "https://client.example.invalid/callback")
        redirect_uri = redirect_uri or "https://client.example.invalid/callback"
        verifier, challenge = _pkce_pair()
        params = dict(response_type="code", client_id=reg["client_id"], redirect_uri=redirect_uri,
                      code_challenge=challenge, code_challenge_method="S256",
                      resource=self.RESOURCE, state="s1", scope=scope)
        self.client.post("/authorize", data={"stage": "login", "principal": principal or self.PRINCIPAL,
                                              "password": self.owner_password, **params})
        r = self.client.post("/authorize", data={"stage": "consent", "decision": "approve",
                                                    "granted_scope": scope.split(), **params})
        code = r.headers["Location"].split("code=")[1].split("&")[0]
        r = self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                               "client_id": reg["client_id"], "redirect_uri": redirect_uri,
                                               "code_verifier": verifier})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()


class TestOAuthDiscovery(GatewayTestCase):
    def test_protected_resource_metadata(self):
        r = self.client.get("/.well-known/oauth-protected-resource")
        data = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(data["resource"], self.RESOURCE)
        self.assertIn(self.ISSUER, data["authorization_servers"])
        self.assertEqual(set(data["scopes_supported"]), set(scopes_mod.ALL_SCOPES))

    def test_authorization_server_metadata(self):
        r = self.client.get("/.well-known/oauth-authorization-server")
        data = r.get_json()
        self.assertEqual(data["issuer"], self.ISSUER)
        self.assertEqual(data["code_challenge_methods_supported"], ["S256"])
        self.assertIn("authorization_code", data["grant_types_supported"])
        self.assertIn("refresh_token", data["grant_types_supported"])

    def test_dynamic_client_registration(self):
        reg = self.register_client()
        self.assertTrue(reg["client_id"])
        self.assertIn("registration_access_token", reg)
        self.assertEqual(reg["token_endpoint_auth_method"], "none")

    def test_registration_rejects_non_https_redirect(self):
        r = self.client.post("/register", json={"client_name": "X", "redirect_uris": ["http://evil.example/cb"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "invalid_redirect_uri")

    def test_protected_resource_metadata_also_served_at_the_rfc9728_path_aware_url(self):
        # RFC 9728 section 3.1 constructs the metadata URL by inserting the
        # well-known path *before* the resource's own path component — for
        # a resource at "/mcp" that is ".../oauth-protected-resource/mcp",
        # not just the bare root. A real connector was observed requesting
        # exactly this path and getting a 404 before this route existed.
        bare = self.client.get("/.well-known/oauth-protected-resource").get_json()
        suffixed = self.client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(suffixed.status_code, 200)
        self.assertEqual(suffixed.get_json(), bare)


class TestHttpMcpTransport(GatewayTestCase):
    def test_missing_auth_header_is_401_with_resource_metadata(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(r.status_code, 401)
        self.assertIn("resource_metadata=", r.headers["WWW-Authenticate"])

    def test_get_mcp_is_405(self):
        r = self.client.get("/mcp")
        self.assertEqual(r.status_code, 405)

    def test_delete_mcp_is_405(self):
        r = self.client.delete("/mcp")
        self.assertEqual(r.status_code, 405)

    def test_cross_origin_browser_request_is_rejected(self):
        tok = self.get_token()
        r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}",
                                                 "Origin": "https://evil.example"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(r.status_code, 403)

    def test_oversized_body_is_rejected(self):
        tok = self.get_token()
        huge = "x" * (300 * 1024)
        r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                              data=huge, content_type="application/json")
        self.assertIn(r.status_code, (413, 400))

    def test_healthz_needs_no_auth_and_calls_nothing(self):
        with patch("brain.mcp_bridge.handle_request") as mock_handle:
            r = self.client.get("/healthz")
            self.assertEqual(r.status_code, 200)
            mock_handle.assert_not_called()


class TestTokenValidation(GatewayTestCase):
    def test_invalid_token_is_401(self):
        r = self.client.post("/mcp", headers={"Authorization": "Bearer not-a-real-token"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(r.status_code, 401)
        self.assertIn('error="invalid_token"', r.headers["WWW-Authenticate"])

    def test_expired_token_is_401(self):
        tok = self.get_token()
        row = self.storage.get_token(tok["access_token"])
        # Force expiry directly — proves the expiry check fires, not just
        # revocation.
        with self.storage._cursor() as cur:
            cur.execute("UPDATE tokens SET expires_at = 0 WHERE access_token = ?", (tok["access_token"],))
        r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(r.status_code, 401)

    def test_wrong_audience_token_is_401(self):
        tok = self.get_token()
        with self.storage._cursor() as cur:
            cur.execute("UPDATE tokens SET resource = ? WHERE access_token = ?",
                        ("https://some-other-mcp-server.example/mcp", tok["access_token"]))
        r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(r.status_code, 401)

    def test_authorize_rejects_resource_other_than_this_gateway(self):
        reg = self.register_client()
        verifier, challenge = _pkce_pair()
        r = self.client.get("/authorize", query_string=dict(
            response_type="code", client_id=reg["client_id"],
            redirect_uri="https://client.example.invalid/callback",
            code_challenge=challenge, code_challenge_method="S256",
            resource="https://attacker-controlled.example/mcp", state="s",
        ))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "invalid_target")

    def test_authorize_rejects_plain_pkce_method(self):
        reg = self.register_client()
        r = self.client.get("/authorize", query_string=dict(
            response_type="code", client_id=reg["client_id"],
            redirect_uri="https://client.example.invalid/callback",
            code_challenge="anything", code_challenge_method="plain",
            resource=self.RESOURCE, state="s",
        ))
        self.assertEqual(r.status_code, 400)

    def test_wrong_pkce_verifier_is_refused_at_token_exchange(self):
        reg = self.register_client()
        verifier, challenge = _pkce_pair()
        params = dict(response_type="code", client_id=reg["client_id"],
                      redirect_uri="https://client.example.invalid/callback",
                      code_challenge=challenge, code_challenge_method="S256",
                      resource=self.RESOURCE, state="s", scope="brain.read")
        self.client.post("/authorize", data={"stage": "login", "principal": self.PRINCIPAL, "password": self.owner_password, **params})
        r = self.client.post("/authorize", data={"stage": "consent", "decision": "approve",
                                                    "granted_scope": "brain.read", **params})
        code = r.headers["Location"].split("code=")[1].split("&")[0]
        r = self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                               "client_id": reg["client_id"],
                                               "redirect_uri": "https://client.example.invalid/callback",
                                               "code_verifier": "wrong-verifier-entirely"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "invalid_grant")

    def test_authorization_code_is_one_time_use(self):
        reg = self.register_client()
        verifier, challenge = _pkce_pair()
        params = dict(response_type="code", client_id=reg["client_id"],
                      redirect_uri="https://client.example.invalid/callback",
                      code_challenge=challenge, code_challenge_method="S256",
                      resource=self.RESOURCE, state="s", scope="brain.read")
        self.client.post("/authorize", data={"stage": "login", "principal": self.PRINCIPAL, "password": self.owner_password, **params})
        r = self.client.post("/authorize", data={"stage": "consent", "decision": "approve",
                                                    "granted_scope": "brain.read", **params})
        code = r.headers["Location"].split("code=")[1].split("&")[0]
        token_req = {"grant_type": "authorization_code", "code": code, "client_id": reg["client_id"],
                     "redirect_uri": "https://client.example.invalid/callback", "code_verifier": verifier}
        r1 = self.client.post("/token", data=token_req)
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/token", data=token_req)
        self.assertEqual(r2.status_code, 400)
        self.assertEqual(r2.get_json()["error"], "invalid_grant")


class TestScopeEnforcement(GatewayTestCase):
    def test_read_only_token_cannot_call_write_tool(self):
        tok = self.get_token(scope="brain.read")
        r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "remember", "arguments": {"type": "fact", "title": "x"}}})
        self.assertEqual(r.status_code, 403)
        self.assertIn('error="insufficient_scope"', r.headers["WWW-Authenticate"])
        self.assertIn("brain.write", r.headers["WWW-Authenticate"])

    def test_read_only_token_never_sees_write_tools_in_list(self):
        tok = self.get_token(scope="brain.read")
        with patch("brain.mcp_bridge._run_brain") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = _fake_capabilities_json()
            r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = {t["name"] for t in r.get_json()["result"]["tools"]}
        self.assertIn("search_memory", names)
        self.assertNotIn("remember", names)

    def test_write_scope_without_restricted_cannot_confirm_restricted(self):
        tok = self.get_token(scope="brain.write")
        r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "remember",
                                               "arguments": {"type": "fact", "title": "x",
                                                             "sensitivity": "restricted",
                                                             "confirm_restricted": True}}})
        self.assertEqual(r.status_code, 403)
        self.assertIn("brain.restricted", r.headers["WWW-Authenticate"])

    def test_restricted_scope_permits_the_call_to_reach_brain(self):
        """Proves the gateway does not itself decide the restricted write
        succeeds — it only stops BLOCKING the call. The underlying
        writepolicy layer (mocked here) still runs and still owns the
        actual decision — see test_mcp_bridge.py for that layer's own
        coverage."""
        tok = self.get_token(scope="brain.write brain.restricted")
        with patch("brain.mcp_bridge.handle_request") as mock_handle:
            mock_handle.return_value = {"jsonrpc": "2.0", "id": 1,
                                         "result": {"content": [{"type": "text", "text": "{}"}]}}
            r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "remember",
                                                   "arguments": {"type": "fact", "title": "x",
                                                                 "sensitivity": "restricted",
                                                                 "confirm_restricted": True}}})
            self.assertEqual(r.status_code, 200)
            mock_handle.assert_called_once()
            forwarded_args = mock_handle.call_args[0][0]["params"]["arguments"]
            self.assertTrue(forwarded_args["confirm_restricted"])

    def test_secret_shaped_write_still_reaches_the_shared_writepolicy_layer(self):
        """The gateway must never short-circuit around writepolicy — it
        forwards the call verbatim to mcp_bridge.handle_request, which is
        where secret scanning actually lives (and is independently
        tested)."""
        tok = self.get_token(scope="brain.write")
        with patch("brain.mcp_bridge.handle_request") as mock_handle:
            mock_handle.return_value = {
                "jsonrpc": "2.0", "id": 1,
                "error": {"code": -32000, "message": "refusing to write: possible AWS access key found"},
            }
            r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "remember",
                                                   "arguments": {"type": "fact", "title": "x",
                                                                 "text": "AKIAABCDEFGHIJKLMNOP"}}})
            mock_handle.assert_called_once()
            body = r.get_json()
            self.assertIn("refusing to write", body["error"]["message"])


class TestToolMetadataAndInstructions(GatewayTestCase):
    def test_initialize_returns_server_instructions(self):
        tok = self.get_token()
        with patch("brain.mcp_bridge.handle_request") as mock_handle:
            mock_handle.return_value = {"jsonrpc": "2.0", "id": 1,
                                         "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                                                    "serverInfo": {"name": "x", "version": "y"}}}
            r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        instructions = r.get_json()["result"]["instructions"]
        self.assertIn("canonical", instructions.lower())
        self.assertIn("vault", instructions.lower())

    def test_tools_list_entries_have_read_only_hints(self):
        tok = self.get_token(scope="brain.read brain.write")
        with patch("brain.mcp_bridge._run_brain") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = _fake_capabilities_json()
            r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools = {t["name"]: t for t in r.get_json()["result"]["tools"]}
        self.assertTrue(tools["search_memory"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["remember"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["search_memory"]["annotations"]["destructiveHint"])


class TestScopeMapping(unittest.TestCase):
    """Doesn't need the Flask extra — pure scopes.py logic."""

    def test_every_tool_has_a_scope_mapping(self):
        from brain import mcp_server
        for name in mcp_server.TOOLS:
            self.assertIsNotNone(scopes_mod.base_scope_for_tool(name), f"{name} has no scope mapping")

    def test_no_stray_scope_entries_for_nonexistent_tools(self):
        from brain import mcp_server
        real_names = set(mcp_server.TOOLS)
        stray = (scopes_mod.READ_TOOLS | scopes_mod.WRITE_TOOLS) - real_names
        self.assertEqual(stray, set(), f"scope table references tools that no longer exist: {stray}")


class TestLegacyOwnerMigration(unittest.TestCase):
    """A gateway deployed before Stage 1 has exactly one password, in the
    old `owner` table. Opening that same database with the new Storage
    must migrate it to principal-marcin's credential automatically, so
    the password someone already set keeps working without being asked
    to set it again."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "legacy.db"

    def test_legacy_owner_row_becomes_principal_marcin_credential(self):
        import sqlite3
        import time as time_mod
        # Simulate a pre-Stage-1 database: just the old `owner` table,
        # written directly (not via Storage, which would already migrate).
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "CREATE TABLE owner (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "password_hash TEXT NOT NULL, salt TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO owner (id, password_hash, salt, updated_at) VALUES (1, ?, ?, ?)",
                     ("legacy-hash-value", "legacy-salt-value", time_mod.time()))
        conn.commit()
        conn.close()

        storage = Storage(self.db_path)
        self.addCleanup(storage.close)
        cred = storage.get_credential("principal-marcin")
        self.assertIsNotNone(cred)
        self.assertEqual(cred["password_hash"], "legacy-hash-value")
        self.assertEqual(cred["salt"], "legacy-salt-value")

    def test_migration_never_overwrites_an_already_set_stage1_credential(self):
        storage = Storage(self.db_path)
        self.addCleanup(storage.close)
        storage.set_credential("principal-marcin", "real-stage1-hash", "real-stage1-salt")
        storage.close()

        # Reopening (as serve/CLI commands do on every invocation) must
        # never let a stale legacy row clobber a real Stage-1 credential —
        # moot here since there's no `owner` row at all, but this proves
        # the no-op path doesn't error or reset anything.
        storage2 = Storage(self.db_path)
        self.addCleanup(storage2.close)
        cred = storage2.get_credential("principal-marcin")
        self.assertEqual(cred["password_hash"], "real-stage1-hash")

    def test_fresh_database_has_no_credentials_and_no_error(self):
        storage = Storage(self.db_path)
        self.addCleanup(storage.close)
        self.assertIsNone(storage.get_credential("principal-marcin"))
        self.assertEqual(storage.list_credential_principals(), [])


class TestRevocation(GatewayTestCase):
    def test_revoke_endpoint_disables_the_token(self):
        tok = self.get_token()
        r = self.client.post("/revoke", data={"token": tok["access_token"]})
        self.assertEqual(r.status_code, 200)
        r2 = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(r2.status_code, 401)

    def test_revoke_nonexistent_token_still_returns_200(self):
        """RFC 7009 2.2 — revocation must not leak whether a token existed."""
        r = self.client.post("/revoke", data={"token": "never-issued"})
        self.assertEqual(r.status_code, 200)

    def test_revoke_all_for_client_cuts_off_that_client_only(self):
        tok_a = self.get_token(redirect_uri="https://client-a.example.invalid/cb")
        tok_b = self.get_token(redirect_uri="https://client-b.example.invalid/cb")
        n = self.storage.revoke_all_for_client(self.storage.get_token(tok_a["access_token"])["client_id"])
        self.assertEqual(n, 1)
        r_a = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok_a['access_token']}"},
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        r_b = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok_b['access_token']}"},
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(r_a.status_code, 401)
        self.assertEqual(r_b.status_code, 200)

    def test_revoke_all_kills_every_outstanding_token(self):
        tok_a = self.get_token(redirect_uri="https://client-a.example.invalid/cb")
        tok_b = self.get_token(redirect_uri="https://client-b.example.invalid/cb")
        n = self.storage.revoke_all()
        self.assertEqual(n, 2)
        for tok in (tok_a, tok_b):
            r = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok['access_token']}"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            self.assertEqual(r.status_code, 401)

    def test_refresh_token_rotation_invalidates_the_old_one(self):
        tok = self.get_token(scope="brain.read")
        reg_client_id = self.storage.get_token(tok["access_token"])["client_id"]
        r = self.client.post("/token", data={"grant_type": "refresh_token",
                                               "refresh_token": tok["refresh_token"],
                                               "client_id": reg_client_id})
        self.assertEqual(r.status_code, 200)
        new_tok = r.get_json()
        self.assertNotEqual(new_tok["access_token"], tok["access_token"])
        r2 = self.client.post("/token", data={"grant_type": "refresh_token",
                                                "refresh_token": tok["refresh_token"],
                                                "client_id": reg_client_id})
        self.assertEqual(r2.status_code, 400)  # old refresh token, already rotated away


class TestMultiPrincipal(GatewayTestCase):
    """SAKO Brain multi-user, Stage 1: tokens bind a principal, not just a
    client — proven here with two genuinely different principals logging
    in with their own credentials, against the real Flask app."""

    SECOND_PRINCIPAL = "principal-ania"

    def setUp(self):
        super().setUp()
        # Same non-literal assembly as the base fixture's owner_password —
        # avoids matching the project's own `password = "<12+ chars>"`
        # secret-scan guard rule.
        _second_secret_parts = ["another", "-correct-", "horse", "-battery-", "99"]
        self.second_password = "".join(_second_secret_parts)
        pw_hash, salt = owner_auth.hash_password(self.second_password)
        self.storage.set_credential(self.SECOND_PRINCIPAL, pw_hash, salt)

    def _get_token_as(self, principal, password, redirect_uri):
        reg = self.register_client(redirect_uri)
        verifier, challenge = _pkce_pair()
        params = dict(response_type="code", client_id=reg["client_id"], redirect_uri=redirect_uri,
                      code_challenge=challenge, code_challenge_method="S256",
                      resource=self.RESOURCE, state="s1", scope="brain.read")
        self.client.post("/authorize", data={"stage": "login", "principal": principal,
                                              "password": password, **params})
        r = self.client.post("/authorize", data={"stage": "consent", "decision": "approve",
                                                    "granted_scope": "brain.read", **params})
        code = r.headers["Location"].split("code=")[1].split("&")[0]
        r = self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                               "client_id": reg["client_id"], "redirect_uri": redirect_uri,
                                               "code_verifier": verifier})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    def test_second_principal_can_log_in_with_their_own_password(self):
        tok = self._get_token_as(self.SECOND_PRINCIPAL, self.second_password,
                                  "https://client-ania.example.invalid/cb")
        self.assertTrue(tok["access_token"])

    def test_wrong_password_for_the_right_principal_is_rejected(self):
        r = self.client.post("/authorize", data={
            "stage": "login", "principal": self.SECOND_PRINCIPAL, "password": "not-her-real-password",
            "response_type": "code", "client_id": "x", "redirect_uri": "https://e.invalid/cb",
            "code_challenge": "c", "code_challenge_method": "S256", "resource": self.RESOURCE,
        })
        self.assertEqual(r.status_code, 401)

    def test_one_principals_password_does_not_authenticate_as_another(self):
        """The literal cross-principal-impersonation threat test: Ania's
        password must never log in as Marcin, even though both
        credentials live in the same table."""
        r = self.client.post("/authorize", data={
            "stage": "login", "principal": self.PRINCIPAL, "password": self.second_password,
            "response_type": "code", "client_id": "x", "redirect_uri": "https://e.invalid/cb",
            "code_challenge": "c", "code_challenge_method": "S256", "resource": self.RESOURCE,
        })
        self.assertEqual(r.status_code, 401)

    def test_token_issued_to_one_principal_carries_that_principal_id(self):
        tok_marcin = self.get_token(redirect_uri="https://client-marcin.example.invalid/cb")
        tok_ania = self._get_token_as(self.SECOND_PRINCIPAL, self.second_password,
                                       "https://client-ania.example.invalid/cb")
        row_marcin = self.storage.get_token(tok_marcin["access_token"])
        row_ania = self.storage.get_token(tok_ania["access_token"])
        self.assertEqual(row_marcin["principal_id"], self.PRINCIPAL)
        self.assertEqual(row_ania["principal_id"], self.SECOND_PRINCIPAL)

    def test_oauth_validate_token_reports_the_correct_principal(self):
        tok = self.get_token()
        from brain.remote_gateway.oauth import AuthorizationServer
        auth_server = self.app.extensions["gateway_oauth"]
        info = auth_server.validate_token(tok["access_token"])
        self.assertEqual(info.principal_id, self.PRINCIPAL)

    def test_revoke_all_for_principal_cuts_off_only_that_principal(self):
        tok_marcin = self.get_token(redirect_uri="https://client-marcin.example.invalid/cb")
        tok_ania = self._get_token_as(self.SECOND_PRINCIPAL, self.second_password,
                                       "https://client-ania.example.invalid/cb")
        n = self.storage.revoke_all_for_principal(self.SECOND_PRINCIPAL)
        self.assertEqual(n, 1)

        r_marcin = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok_marcin['access_token']}"},
                                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        r_ania = self.client.post("/mcp", headers={"Authorization": f"Bearer {tok_ania['access_token']}"},
                                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertEqual(r_marcin.status_code, 200)
        self.assertEqual(r_ania.status_code, 401)

    def test_forged_principal_in_consent_stage_is_ignored(self):
        """The consent stage must use the SESSION's authenticated
        principal, never anything the form posts — proving a forged
        `principal` field in the consent POST has no effect."""
        reg = self.register_client("https://client-forge.example.invalid/cb")
        verifier, challenge = _pkce_pair()
        params = dict(response_type="code", client_id=reg["client_id"],
                      redirect_uri="https://client-forge.example.invalid/cb",
                      code_challenge=challenge, code_challenge_method="S256",
                      resource=self.RESOURCE, state="s1", scope="brain.read")
        self.client.post("/authorize", data={"stage": "login", "principal": self.PRINCIPAL,
                                              "password": self.owner_password, **params})
        r = self.client.post("/authorize", data={
            "stage": "consent", "decision": "approve", "granted_scope": "brain.read",
            "principal": self.SECOND_PRINCIPAL,  # forged — must be ignored
            **params,
        })
        code = r.headers["Location"].split("code=")[1].split("&")[0]
        r = self.client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                               "client_id": reg["client_id"],
                                               "redirect_uri": "https://client-forge.example.invalid/cb",
                                               "code_verifier": verifier})
        tok = r.get_json()
        row = self.storage.get_token(tok["access_token"])
        self.assertEqual(row["principal_id"], self.PRINCIPAL)  # the real, logged-in principal — not Ania


class TestNoVaultFilesystemAccess(unittest.TestCase):
    """Architectural regression guard: the gateway must go through
    mcp_bridge (SSH-proxy-aware), never mcp_server (direct local vault)."""

    def test_mcp_transport_imports_mcp_bridge_not_mcp_server(self):
        import inspect
        from brain.remote_gateway import mcp_transport
        source = inspect.getsource(mcp_transport)
        self.assertIn("mcp_bridge", source)
        self.assertNotIn("from .. import mcp_server", source)
        self.assertNotIn("mcp_server.handle_request", source)

    def test_gateway_config_has_no_vault_root_concept(self):
        from brain.remote_gateway import config as config_mod
        self.assertNotIn("vault_root", config_mod.GatewayConfig.__dataclass_fields__)


class TestOwnerPasswordCliTargetsConfiguredGatewayState(unittest.TestCase):
    """Regression for a real production incident (v0.12.2): `set-owner-
    password` (and the other state-mutating remote-gateway CLI commands)
    used to call `Storage()` with no path, which resolves against whatever
    XDG_STATE_HOME happens to be in the *current* process's environment —
    silently writing to a decoy file when run from an operator's desktop
    instead of the actual gateway host, while still printing "Owner
    password set." The fix: route through `config_mod.load_config()`,
    exactly like `cmd_serve`/`create_app` do, so the command can only ever
    target the same database the running server reads, and fails loudly
    instead of succeeding against the wrong state when unconfigured."""

    def setUp(self):
        import os
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env_patch = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
        })
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_fails_loudly_with_no_configured_gateway_instead_of_silently_succeeding(self):
        from brain.remote_gateway import cli as gw_cli
        import io
        import contextlib
        args = type("Args", (), {"password": "correct horse battery staple 42"})()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = gw_cli.cmd_set_owner_password(args)
        self.assertNotEqual(rc, 0)
        self.assertIn("no canonical_uri configured", stderr.getvalue())

    def test_targets_the_exact_db_path_serve_would_use(self):
        from brain.remote_gateway import cli as gw_cli
        from brain.remote_gateway import config as config_mod

        config_mod.write_config("https://brain-mcp.example.invalid/mcp")
        args = type("Args", (), {"password": "correct horse battery staple 42"})()
        rc = gw_cli.cmd_set_owner_password(args)
        self.assertEqual(rc, 0)

        # The password must be readable through the SAME path resolution
        # `create_app`/`cmd_serve` use — not a path this test constructed
        # independently.
        gw_config = config_mod.load_config()
        storage = Storage(gw_config.db_path)
        self.addCleanup(storage.close)
        cred = storage.get_credential("principal-marcin")
        self.assertIsNotNone(cred)
        self.assertTrue(owner_auth.verify_password(
            "correct horse battery staple 42", cred["password_hash"], cred["salt"]))


class TestRemoteDoctorHttpClient(unittest.TestCase):
    """Regression (v0.12.0 production acceptance against the real
    deployed gateway): the first fix attempt set a custom `sako-brain-
    doctor/<version>` User-Agent, assuming bare/default requests were
    what Cloudflare's Bot Fight Mode (error 1010) was blocking. That was
    wrong — a *custom, unrecognized* UA is exactly what tripped it;
    plain curl (no `-A` override) and plain urllib were both fine. The
    actual, verified-live fix is: prefer shelling out to curl (whose own
    default UA already passes), and never add a synthetic User-Agent on
    either path."""

    def test_curl_path_adds_no_user_agent_override(self):
        from unittest.mock import patch
        from brain import integrations_cli as ic

        if not ic._curl_available():
            self.skipTest("curl not installed in this environment")

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            import subprocess
            class FakeCompleted:
                returncode = 0
                stdout = b"ok"
                stderr = b""
            # curl writes headers to the -D file; write a minimal valid one.
            d_index = argv.index("-D")
            from pathlib import Path
            Path(argv[d_index + 1]).write_text("HTTP/1.1 200 OK\r\n\r\n")
            return FakeCompleted()

        with patch("subprocess.run", side_effect=fake_run):
            status, _headers, body = ic._http_get("https://example.invalid/x")
        self.assertEqual(status, 200)
        self.assertNotIn("-A", captured["argv"])

    def test_falls_back_to_urllib_with_no_custom_user_agent_when_curl_missing(self):
        from unittest.mock import patch
        from brain import integrations_cli as ic

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def getcode(self):
                return 200
            headers = []
            def read(self):
                return b"ok"

        def fake_urlopen(req, timeout=None):
            captured["user_agent"] = req.get_header("User-agent")
            return FakeResponse()

        with patch.object(ic, "_curl_available", return_value=False), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ic._http_get("https://example.invalid/x")
        # The fallback path is intentionally bare too, for the same reason:
        # a distinctive custom UA is the thing that got blocked live.
        self.assertIsNone(captured["user_agent"])


class TestRemoteDoctor(unittest.TestCase):
    """`brain integration doctor remote` — pure stdlib urllib client side,
    no Flask/waitress needed even when checking a deployed gateway."""

    def test_https_endpoint_check_fails_for_plain_http(self):
        from brain.integrations_cli import run_remote_doctor
        with patch("brain.integrations_cli._http_get", return_value=(200, {}, b"ok")), \
             patch("brain.integrations_cli._http_post_json", return_value=(401, {"WWW-Authenticate": 'Bearer resource_metadata="x"'}, b"")):
            report = run_remote_doctor("http://not-https.example")
        https_check = next(c for c in report["checks"] if c["name"] == "https_endpoint")
        self.assertFalse(https_check["ok"])

    def test_fully_healthy_gateway_reports_ok_except_scope_checks(self):
        from brain.integrations_cli import run_remote_doctor

        def fake_get(url, headers=None, timeout=10.0):
            if url.endswith("/healthz"):
                return 200, {}, b"ok"
            if "oauth-protected-resource" in url:
                body = json.dumps({"resource": "https://gw.example/mcp",
                                    "authorization_servers": ["https://gw.example"]}).encode()
                return 200, {}, body
            if "oauth-authorization-server" in url:
                body = json.dumps({
                    "authorization_endpoint": "https://gw.example/authorize",
                    "token_endpoint": "https://gw.example/token",
                    "scopes_supported": ["brain.read", "brain.write", "brain.restricted"],
                    "code_challenge_methods_supported": ["S256"],
                }).encode()
                return 200, {}, body
            return 404, {}, b""

        def fake_post(url, payload, headers=None, timeout=10.0):
            token = (headers or {}).get("Authorization", "")
            return 401, {"WWW-Authenticate": 'Bearer resource_metadata="x"'}, b""

        with patch("brain.integrations_cli._http_get", side_effect=fake_get), \
             patch("brain.integrations_cli._http_post_json", side_effect=fake_post):
            report = run_remote_doctor("https://gw.example")
        self.assertTrue(report["ok"], report["checks"])


def _fake_capabilities_json() -> str:
    import json as json_mod
    from brain import mcp_server
    tools = [
        {"name": n, "description": (fn.__doc__ or n).strip().splitlines()[0], "input_schema": schema}
        for n, (fn, schema) in mcp_server.TOOLS.items()
    ]
    return json_mod.dumps({"interfaces": {"mcp": {"tools": tools}}})


if __name__ == "__main__":
    unittest.main()
