"""Flask application for the remote MCP gateway (v0.12.0).

Wires together `storage.py` (state), `oauth.py` (protocol bookkeeping),
`mcp_transport.py` (the actual Brain capability surface, via
`brain.mcp_bridge` — never direct vault access, see that module's
docstring), and `scopes.py` (the least-privilege policy) into the HTTP
routes ChatGPT web, Claude.ai web, and any other MCP-over-HTTPS client
expect:

    GET  /.well-known/oauth-protected-resource   (RFC 9728)
    GET  /.well-known/oauth-authorization-server (RFC 8414)
    POST /register                               (RFC 7591 DCR, open)
    GET  /authorize                               (owner login + consent)
    POST /authorize
    POST /token                                   (authorization_code, refresh_token)
    POST /revoke                                   (RFC 7009)
    POST /mcp                                      (Streamable HTTP MCP, Bearer-protected)
    GET  /healthz                                  (liveness only — no auth, no Brain call)

No route here ever touches the vault filesystem directly, sets
BRAIN_LOCAL, or bypasses Brain's own write-safety policy — every tool
call goes through `mcp_transport.handle_mcp_body`, which defers to
`brain.mcp_bridge.handle_request` for all actual Brain access.
"""
from __future__ import annotations

import html
import logging
from urllib.parse import urlencode, urlsplit

from flask import Flask, Response, jsonify, redirect, request, session

from . import config as config_mod
from . import mcp_transport
from . import owner_auth
from . import scopes as scopes_mod
from .oauth import AuthorizationServer, OAuthError
from .storage import Storage

log = logging.getLogger(__name__)


def create_app(gw_config: config_mod.GatewayConfig | None = None,
               storage: Storage | None = None) -> Flask:
    gw_config = gw_config or config_mod.load_config()
    storage = storage or Storage(gw_config.db_path)

    oauth = AuthorizationServer(storage, issuer=gw_config.issuer, resource=gw_config.canonical_uri)

    app = Flask(__name__)
    app.secret_key = storage.get_or_create_server_secret()
    app.config["JSON_SORT_KEYS"] = False
    # Bound request bodies (item 17) — a consent form post and a single
    # MCP JSON-RPC message are both small; this is not a file-upload API.
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

    def _resource_metadata_url() -> str:
        return f"{gw_config.issuer}/.well-known/oauth-protected-resource"

    def _unauthorized(error: str | None = None, scope: str | None = None):
        parts = [f'resource_metadata="{_resource_metadata_url()}"']
        if error:
            parts.append(f'error="{error}"')
        if scope:
            parts.append(f'scope="{scope}"')
        resp = jsonify({"error": error or "unauthorized"})
        resp.status_code = 401
        resp.headers["WWW-Authenticate"] = "Bearer " + ", ".join(parts)
        return resp

    def _origin_allowed() -> bool:
        # DNS-rebinding protection (MCP transport spec, Security Warning):
        # a browser-originated request carries Origin; reject it unless it
        # matches this gateway's own origin. Server-to-server callers
        # (ChatGPT/Claude's own backends calling this HTTPS endpoint)
        # generally omit Origin entirely, which is allowed through.
        origin = request.headers.get("Origin")
        if not origin or origin == "null":
            return True
        gw_origin = f"{urlsplit(gw_config.canonical_uri).scheme}://{urlsplit(gw_config.canonical_uri).netloc}"
        return origin.rstrip("/") == gw_origin.rstrip("/")

    # ---- Discovery metadata (RFC 9728 / RFC 8414) --------------------------

    @app.get("/.well-known/oauth-protected-resource")
    def protected_resource_metadata():
        from .oauth import protected_resource_metadata as prm
        return jsonify(prm(gw_config.canonical_uri, gw_config.issuer))

    # RFC 9728 section 3.1's own construction rule inserts the well-known
    # path *before* the resource's path component — for a resource at
    # "/mcp" that is this same document again at ".../oauth-protected-
    # resource/mcp". Some MCP clients request this form directly (observed
    # live from a real connector attempt) instead of the bare root form
    # above; both must return the identical document since they describe
    # the same single resource.
    @app.get("/.well-known/oauth-protected-resource/mcp")
    def protected_resource_metadata_mcp_suffixed():
        return protected_resource_metadata()

    @app.get("/.well-known/oauth-authorization-server")
    def authorization_server_metadata():
        from .oauth import authorization_server_metadata as asm
        return jsonify(asm(gw_config.issuer))

    # ---- Dynamic Client Registration (RFC 7591, open) ----------------------

    @app.post("/register")
    def register_client():
        payload = request.get_json(silent=True) or {}
        try:
            result = oauth.register_client(payload)
        except OAuthError as exc:
            return jsonify({"error": exc.error, "error_description": exc.description}), exc.status
        return jsonify(result), 201

    # ---- Authorization endpoint: owner login + consent ---------------------

    def _owner_logged_in() -> bool:
        return session.get("owner_authenticated") is True

    @app.get("/authorize")
    def authorize_get():
        params = request.args.to_dict()
        try:
            ctx = oauth.validate_authorize_request(params)
        except OAuthError as exc:
            return jsonify({"error": exc.error, "error_description": exc.description}), exc.status

        if storage.get_owner() is None:
            return ("This gateway has no owner account configured yet. Run "
                    "`brain remote-gateway set-owner-password` first."), 503

        if not _owner_logged_in():
            return _render_login(params)
        return _render_consent(ctx, params)

    @app.post("/authorize")
    def authorize_post():
        form = request.form
        stage = form.get("stage")

        if stage == "login":
            password = form.get("password", "")
            owner = storage.get_owner()
            if owner is None or not owner_auth.verify_password(
                password, owner["password_hash"], owner["salt"]
            ):
                params = {k: v for k, v in form.items() if k not in ("stage", "password")}
                return _render_login(params, error="Incorrect password."), 401
            session["owner_authenticated"] = True
            session.permanent = False
            params = {k: v for k, v in form.items() if k not in ("stage", "password")}
            try:
                ctx = oauth.validate_authorize_request(params)
            except OAuthError as exc:
                return jsonify({"error": exc.error, "error_description": exc.description}), exc.status
            return _render_consent(ctx, params)

        if stage == "consent":
            params = {k: v for k, v in form.items() if k not in ("stage", "decision", "granted_scope")}
            try:
                ctx = oauth.validate_authorize_request(params)
            except OAuthError as exc:
                return jsonify({"error": exc.error, "error_description": exc.description}), exc.status

            if not _owner_logged_in():
                return _render_login(params)

            redirect_uri = ctx["redirect_uri"]
            if form.get("decision") != "approve":
                return redirect(oauth.build_redirect(
                    redirect_uri, error="access_denied",
                    error_description="Owner denied the request.", state=ctx["state"],
                ))
            granted = request.form.getlist("granted_scope") or ctx["requested_scope"]
            granted = [s for s in granted if s in ctx["requested_scope"]]
            if not granted:
                return redirect(oauth.build_redirect(
                    redirect_uri, error="access_denied",
                    error_description="no scope granted", state=ctx["state"],
                ))
            code = oauth.issue_code(
                client_id=ctx["client_id"], redirect_uri=redirect_uri,
                code_challenge=ctx["code_challenge"], code_challenge_method=ctx["code_challenge_method"],
                granted_scope=granted, resource=ctx["resource"],
            )
            return redirect(oauth.build_redirect(redirect_uri, code=code, state=ctx["state"]))

        return jsonify({"error": "invalid_request", "error_description": "missing stage"}), 400

    def _render_login(params: dict, error: str | None = None) -> Response:
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
            for k, v in params.items()
        )
        error_html = f'<p style="color:#b00">{html.escape(error)}</p>' if error else ""
        body = f"""<!doctype html><html><head><title>SAKO Brain — sign in</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem">
<h1>SAKO Brain</h1>
<p>Sign in as the Brain owner to review this connection request.</p>
{error_html}
<form method="post" action="/authorize">
<input type="hidden" name="stage" value="login">
{hidden}
<input type="password" name="password" placeholder="Owner password" autofocus required
       style="width:100%;padding:0.5rem;margin:0.5rem 0">
<button type="submit" style="padding:0.5rem 1rem">Sign in</button>
</form>
</body></html>"""
        return Response(body, mimetype="text/html")

    def _render_consent(ctx: dict, params: dict) -> Response:
        client_name = html.escape(ctx["client"]["client_name"])
        scope_labels = {
            "brain.read": "Read your projects, decisions, handoffs, timeline, and notes",
            "brain.write": "Create/update ordinary (non-restricted) records",
            "brain.restricted": "Create/update records marked restricted-sensitivity",
        }
        scope_items = "".join(
            f'<label style="display:block;margin:.4rem 0"><input type="checkbox" name="granted_scope" '
            f'value="{html.escape(s)}" checked> <code>{html.escape(s)}</code> — '
            f'{html.escape(scope_labels.get(s, s))}</label>'
            for s in ctx["requested_scope"]
        )
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
            for k, v in params.items()
        )
        body = f"""<!doctype html><html><head><title>SAKO Brain — authorize {client_name}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem">
<h1>Authorize {client_name}</h1>
<p>This will let <strong>{client_name}</strong> access your canonical SAKO Brain
with the scopes below, until you revoke it.</p>
<form method="post" action="/authorize">
<input type="hidden" name="stage" value="consent">
{hidden}
<fieldset style="border:1px solid #ccc;border-radius:6px;padding:.75rem">
<legend>Permissions</legend>
{scope_items}
</fieldset>
<div style="margin-top:1rem;display:flex;gap:.5rem">
<button type="submit" name="decision" value="approve" style="padding:0.5rem 1rem">Approve</button>
<button type="submit" name="decision" value="deny" style="padding:0.5rem 1rem">Deny</button>
</div>
</form>
</body></html>"""
        return Response(body, mimetype="text/html")

    # ---- Token endpoint -----------------------------------------------------

    @app.post("/token")
    def token():
        form = request.form
        grant_type = form.get("grant_type")
        client_id = form.get("client_id", "")
        try:
            if grant_type == "authorization_code":
                result = oauth.exchange_code(
                    code=form.get("code", ""), client_id=client_id,
                    redirect_uri=form.get("redirect_uri", ""),
                    code_verifier=form.get("code_verifier", ""),
                )
            elif grant_type == "refresh_token":
                scope = form.get("scope")
                result = oauth.refresh(
                    refresh_token=form.get("refresh_token", ""), client_id=client_id,
                    requested_scope=scope.split() if scope else None,
                )
            else:
                return jsonify({"error": "unsupported_grant_type"}), 400
        except OAuthError as exc:
            return jsonify({"error": exc.error, "error_description": exc.description}), exc.status
        resp = jsonify(result)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
        return resp

    # ---- Revocation (RFC 7009) ----------------------------------------------

    @app.post("/revoke")
    def revoke():
        token_value = request.form.get("token", "")
        if token_value:
            oauth.revoke(token_value)
        return "", 200  # always 200, whether or not the token existed (RFC 7009 2.2)

    # ---- MCP endpoint (Streamable HTTP) --------------------------------------

    @app.post("/mcp")
    def mcp_endpoint():
        if not _origin_allowed():
            return "", 403

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _unauthorized()
        try:
            info = oauth.validate_token(auth[len("Bearer "):])
        except OAuthError as exc:
            return _unauthorized(error=exc.error if exc.error == "invalid_token" else None)

        result, status, missing_scope = mcp_transport.handle_mcp_body(request.get_data(), info.scope)
        if status == 403:
            resp = jsonify(result)
            resp.status_code = 403
            scope_part = f', scope="{missing_scope}"' if missing_scope else ""
            resp.headers["WWW-Authenticate"] = (
                f'Bearer error="insufficient_scope", '
                f'resource_metadata="{_resource_metadata_url()}"{scope_part}'
            )
            return resp
        if result is None:
            return "", status
        return jsonify(result), status

    @app.get("/mcp")
    def mcp_get():
        return "", 405  # no server-initiated SSE stream offered

    @app.delete("/mcp")
    def mcp_delete():
        return "", 405  # stateless; nothing to terminate

    @app.get("/healthz")
    def healthz():
        return "ok", 200

    app.extensions["gateway_storage"] = storage
    app.extensions["gateway_oauth"] = oauth
    app.extensions["gateway_config"] = gw_config
    return app
