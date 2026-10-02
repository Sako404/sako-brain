# Remote / Web AI Access

How to let ChatGPT web and Claude.ai web (or any other MCP-over-HTTPS
client) reach your canonical Brain live, without either ever touching
your vault filesystem or bypassing Brain's own write safety.

## Overview

```
ChatGPT web ─────┐
                 │
Claude.ai web ───┼── HTTPS / OAuth 2.1 / Streamable HTTP MCP
                 │
future MCP AI ───┘
                        │
                        ▼
              brain remote-gateway
           (OAuth authorization server +
            Streamable HTTP MCP transport)
                        │
                        ▼
        existing canonical Brain capabilities
          (brain.mcp_bridge → brain CLI →
           SSH forced-command dispatcher)
                        │
                        ▼
               your canonical Brain

```

The gateway is a **capability adapter, not a second Brain**: every tool
call it handles is forwarded, unmodified, to the exact same
`brain.mcp_bridge.handle_request` dispatch Claude Code and Codex already
use. It reaches canonical Brain through the same SSH forced-command
dispatcher every other client goes through — using its own dedicated
`brain setup` identity, never direct filesystem access to the vault.

A remote web client can never:

- see or request the vault filesystem,
- reach SSH, a database, or any internal service directly,
- bypass Brain's write-safety policy (secret scanning, restricted
  confirmation) — that policy lives in `brain.mcp_bridge`/the CLI, below
  the gateway, and applies identically regardless of transport.

## Architecture

- **Resource server + authorization server, co-located.** One small
  service (`brain remote-gateway serve`) plays both OAuth roles, which
  the [MCP authorization
  specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
  explicitly permits.
- **Opaque, server-issued tokens**, not JWTs — a single resource server
  validating tokens it alone issued has no signature-verification
  surface to get wrong, and revocation is a database row delete rather
  than a blocklist.
- **No hand-rolled cryptography.** Every cryptographic operation is a
  direct standard-library call: `secrets.token_urlsafe` for every
  code/token, `hashlib.sha256` for PKCE (RFC 7636) exactly as specified,
  `hashlib.scrypt` for the owner password, `hmac`/`secrets.compare_digest`
  for every comparison. What *is* hand-written is OAuth protocol
  bookkeeping (grant validity windows, one-time-use codes) — necessarily
  implementation-specific, since MCP's exact metadata requirements are
  new enough that no general-purpose OAuth library covers them
  end-to-end yet.
- **Least privilege by scope**, not by client identity. Three scopes —
  see below — and the owner chooses which to actually grant at consent
  time, independent of what a client requests.

## Security model

| | |
|---|---|
| Transport | HTTPS only; the gateway itself should sit behind a TLS-terminating reverse proxy or tunnel (see "Deployment" below) |
| Authentication | OAuth 2.1, Authorization Code + PKCE (S256 only) |
| Client registration | Dynamic Client Registration (RFC 7591) — open, since MCP clients and servers routinely have no prior relationship |
| Token audience | Every token is bound to this gateway's own resource URI (RFC 8707) and rejected if presented anywhere else, or if the resource on a request doesn't match |
| Token lifetime | Access tokens: 1 hour. Refresh tokens: 90 days, **rotated on every use** (the old one is immediately revoked when a new one is issued) |
| Authorization codes | One-time use, 2-minute expiry |
| Consent | A human (the Brain owner) reviews and approves every new client connection, choosing which of the requested scopes to actually grant |
| DNS rebinding | `Origin` header validated on every `/mcp` request |
| Request size | Capped (256 KB) — this is a JSON-RPC tool-call API, not a file upload endpoint |
| Logging | Structured, secret-free: tool names and outcomes, never token values, passwords, or full note content |

### Multi-user visibility (Stage 2) and the gateway's SSH credential

Every OAuth-authenticated principal's requests reach canonical Brain
through the SAME two SSH identities (`remote-gateway-read` /
`remote-gateway-write`) — one shared identity per mode, not one per
principal. Stage 2's visibility enforcement (which principal sees which
records) depends on the gateway correctly asserting, per request, which
principal it's acting for — a `--acting-principal <id>` prefix the
server-side dispatcher independently re-validates against the canonical
principal registry before honoring (see `brain-dispatch.py`'s
`resolve_effective_principal`). Only these two identities may make that
assertion; every other SSH identity (desktop, TRON, capture) is refused
outright if it tries.

**This means possession of either gateway SSH private key is equivalent to
being able to assert ANY active principal's identity to the vault** — not
merely "read the gateway's own data" the way compromising a single
family member's OAuth credential would be. Treat compromise or suspected
compromise of either key as a full identity-spoofing incident, not an
ordinary credential leak:

1. Regenerate the SSH keypair(s) for the affected identity immediately.
2. Replace the corresponding `authorized_keys` line(s) on the canonical
   server — do not just add a new key alongside the old one.
3. Restart/redeploy the gateway app so it starts using the new key.
4. Review the Brain-core audit log for every delegated request
   (`transport=gateway`) since the suspected compromise window, alongside
   the gateway's own OAuth audit log (login/consent/token events).
5. Record the incident in canonical Brain (a decision or timeline event)
   the same way any other security incident in this project is recorded.

## OAuth scopes

| Scope | Covers |
|---|---|
| `brain.read` | `search_memory`, `read_memory`, `get_context`, `list_projects`, `get_project`, `search_timeline`, `project_context`, `get_operational_state` |
| `brain.write` | `remember`, `update_memory`, `queue_memory`, `write_handoff`, `create_decision`, `create_project`, `update_project_status`, `close_project`, `update_project_section`, `create_memory_note`, `create_timeline_event` |
| `brain.restricted` | Required **in addition to** `brain.write` for any write whose effective sensitivity is `restricted` — mirrors the existing `confirm_restricted` mechanism one-for-one |

A token missing a required scope gets a clean `403` with
`WWW-Authenticate: Bearer error="insufficient_scope", scope="..."` naming
exactly what's missing — never a silent failure, never a tool advertised
in `tools/list` that a call would then refuse.

## Deployment

The gateway is one Python process (`brain remote-gateway serve`) that
needs:

1. **A public HTTPS hostname** pointing at it. Preferred: an
   already-running reverse-proxy tunnel (e.g. Cloudflare Tunnel,
   `ngrok`, or your own nginx + Let's Encrypt) — no new inbound firewall
   rule, and the gateway process itself can keep listening on a private
   address. Avoid exposing raw SSH, a database port, or any other
   internal service on the same path.
2. **Its own dedicated Brain identity**, configured exactly like any
   other client: `brain setup --server ... --read-identity ...
   --write-identity ... --known-hosts-file ...` (see the main README's
   "Remote canonical Brain" section). Use a **separate** keypair from
   your desktop/CI/other clients — this is what makes "revoke all remote
   web AI access" (below) not touch anything else.
3. **Its own small config**: `brain remote-gateway init --canonical-uri
   https://<your-hostname>/mcp` writes `~/.config/sako-brain/remote-gateway.toml`.
4. **An owner password**: `brain remote-gateway set-owner-password` —
   this is a *separate* login from the OAuth flow; it's who gets to
   approve new client connections in the browser consent screen.
5. Then: `brain remote-gateway serve` (or put it behind a process
   supervisor / container restart policy of your choice).

Install with the optional extra: `pip install 'sako-brain[remote-gateway]'`
(pulls in Flask and waitress — not required for, and not installed by,
a normal CLI/MCP-only install).

Verify from the outside, exactly the vantage point a web AI has:

```sh
brain integration doctor remote --base-url https://your-hostname --json
```

## Connecting ChatGPT web

Requires **Developer Mode** (beta, at the time of writing): ChatGPT
Settings → Apps & Connectors → enable Developer Mode, then add a custom
connector pointing at `https://your-hostname/mcp` (the `/mcp` path is
required). ChatGPT completes the OAuth flow itself — you'll see the
owner-login and consent screens described above in a browser popup.

**Account/plan note:** at the time of writing, full write-capable custom
MCP connectors are available on Business/Enterprise/Edu ChatGPT
workspaces; Plus/Pro individual accounts are limited to read-only custom
connectors even with Developer Mode enabled. This is a ChatGPT product
entitlement, not a limitation of this gateway — the gateway offers the
same scopes regardless of which surface is asking, and grants only what
the owner actually approves at consent time either way.

## Connecting Claude.ai web

Settings → Connectors → Add custom connector → paste
`https://your-hostname/mcp`. Claude.ai connects from Anthropic's own
cloud infrastructure, not your browser — so if your gateway sits behind
a firewall, it must be reachable from the public internet (again, a
tunnel is the simplest way to achieve this without a new inbound rule).
Claude completes the OAuth flow the same way ChatGPT does.

## Revocation

```sh
brain remote-gateway list-clients                 # see what's connected
brain remote-gateway revoke-client <client_id>     # cut off one client
brain remote-gateway revoke-client <client_id> --forget   # ...and forget it entirely
brain remote-gateway revoke-all                    # kill-switch: every remote token, immediately
```

`revoke-all` cuts off **every** web AI connection at once and never
touches Claude Code, Codex, or any other client using `brain setup`'s
own SSH-proxy mechanism — those are a completely separate identity.

To rotate the gateway's own credentials (e.g. after a suspected
compromise): generate a fresh SSH keypair, add it to your canonical
server's authorized identities, point `brain setup` at the new key, and
remove the old key from the server — the same procedure as rotating any
other client's identity.

## Troubleshooting

- **`brain integration doctor remote` fails `https_endpoint`** — you're
  pointing it at a plain `http://` URL; OAuth 2.1 requires HTTPS for
  every authorization-server endpoint.
- **A client gets `401` immediately** — check `brain remote-gateway
  list-clients`; the client may have been revoked, or its token expired
  (access tokens last 1 hour — a client should transparently refresh).
- **A write is refused with a scope error** — the owner didn't grant
  that scope at consent time. Re-add the connector to re-trigger
  consent, and check the box for the scope you want to allow.
- **A write is refused with a secret-scan or restricted-confirmation
  error** — this is Brain's own write-safety policy, unchanged and
  unaffected by the gateway; see the main README.

## Privacy implications

Whatever a web AI's tool call returns becomes visible to that AI
provider, subject to their own data-handling terms — the same as any
other tool result you'd paste into a chat. Granting `brain.read` means
the connected AI can retrieve the content of any note, project, decision,
or handoff it asks for (restricted-sensitivity content is a separate,
smaller surface — see below). Think about what you're granting a scope
to, the same way you would for any other OAuth-connected app.

`brain.restricted` gates content you've explicitly marked sensitive
(`sensitivity: restricted`) — grant it only if you specifically want a
web AI to be able to read or write that tier. Read access to restricted
content still goes through the normal `brain.read` tools; `brain.restricted`
specifically governs *writing* new restricted-sensitivity records, not
reading existing ones — if you don't want a connected AI reading your
restricted notes at all, that's a broader Brain-side access decision
(see the main README/`AGENTS.md`'s own sensitivity model), independent of
this gateway.
