"""Remote SAKO Brain MCP gateway (v0.12.0) — optional component.

Not imported by the core `brain` package at all; only reachable via
`brain remote-gateway ...`, and only usable once the `remote-gateway`
extra is installed (`pip install sako-brain[remote-gateway]`). Keeps the
CLI's single-runtime-dependency footprint (PyYAML) intact for the vast
majority of installs that never self-host this.

See `app.py` for the ASGI application, `oauth.py` for the authorization
server, `scopes.py` for the OAuth-scope <-> MCP-tool mapping, and
`../../docs/REMOTE_ACCESS.md` for the deployment/security overview.
"""
