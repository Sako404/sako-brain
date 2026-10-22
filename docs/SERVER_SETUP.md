# Hosting a canonical Brain on a server

Use this when one machine holds the vault and other machines (a laptop,
an AI agent, the [remote gateway](REMOTE_ACCESS.md)) reach it over SSH.
If you only use Brain on one computer, you need none of this.

Clients connect with keys that can do one thing only: run the dispatcher
that ships in this package (`python -m brain.dispatch`). Each key's `authorized_keys` line fixes what
that key may run and as whom. A client never gets a shell.

## What runs where

| Machine | Runs | Configured with |
|---|---|---|
| Server | the vault and one install of this package | `authorized_keys` forced commands |
| Each client | `brain`, which proxies every command over SSH | `brain setup` (see the README's "Remote canonical Brain") |

## Server setup

1. Create a dedicated user to own the vault, for example `brain`. Every
   client key goes into this user's `~/.ssh/authorized_keys`.
2. Install the package for that user in a virtualenv, for example
   `python3 -m venv /home/brain/venv` then
   `/home/brain/venv/bin/pip install sako-brain`. The forced commands below
   run that venv's own interpreter (`/home/brain/venv/bin/python3 -m
   brain.dispatch`), and the dispatcher always runs the `brain` from the
   same venv, so the two can never come from different versions.
3. Create or adopt the vault and run `brain init` / `brain index` locally
   as that user. Pick a state directory, for example
   `/home/brain/.local/state/sako-brain`.
4. Create principals if more than one person uses the vault (README:
   "Multi-user"). Principal and group administration runs only in a local
   shell on the server; no key can reach it.

## One line per key

Each client key gets its own line. A laptop with a read key and a write
key needs two lines:

```
restrict,from="192.0.2.10",command="/home/brain/venv/bin/python3 -m brain.dispatch --mode read --vault /srv/brain/vault --state-dir /home/brain/.local/state/sako-brain --principal principal-alex --client laptop-read" ssh-ed25519 AAAA... laptop-read
restrict,from="192.0.2.10",command="/home/brain/venv/bin/python3 -m brain.dispatch --mode write --vault /srv/brain/vault --state-dir /home/brain/.local/state/sako-brain --principal principal-alex --client laptop-write" ssh-ed25519 AAAA... laptop-write
```

- `restrict` turns off port, agent and X11 forwarding and PTY allocation.
  Always use it.
- `from=` limits where the key may connect from. Use it whenever the
  client has a stable address.
- `command=` is where every flag below is set. The connecting client can't
  change any of them, because it only supplies the `brain` subcommand.

### Flags

| Flag | Meaning |
|---|---|
| `--mode read\|write\|capture\|status` | What the key may run (next section). Default `read`. |
| `--vault PATH` | The canonical vault on this server. Required. |
| `--state-dir PATH` | Brain's state directory on this server. Required. |
| `--principal ID` | Whom the key acts as. Default: the package's default principal. |
| `--client NAME` | A name for the key, recorded in the audit log. |
| `--delegation none\|optional\|mandatory` | Whether the key may act as another principal. Default `none`. |
| `--remote-project-paths` | Project paths in this vault point at client machines, not this server. Set it when clients register projects from their own disks. |

### Modes

| Mode | May run | For |
|---|---|---|
| `read` | `state`, `search`, `context`, `get`, `projects`, `timeline` (not `timeline add`), `status`, `doctor`, `capabilities`, `list`, and `project show`/`discover`/`sync` | Any client that only looks things up |
| `write` | Everything in `read`, plus `project`, `decision`, `update`, `index`, `note`, `handoff`, `remember`, `timeline add` | The key a client uses to record things |
| `capture` | `remember`, `index` | A bot that only drops new notes in |
| `status` | `status` | A health check that proves the chain works and reveals nothing else |

Some commands never go through any key:

- **Client configuration** (`setup`, `integration`, `init`,
  `remote-gateway`) always runs on the client itself.
- **Host-only** commands (`principal`, `group`, `role-policy`, `backup`,
  `integrity`, `git`, `agents-doc`, `ask`, `chat`, and the Stage 2
  migration) run only in a local shell on the server.
- **`memory`, the pending-memory queue**, is single-principal only. It has
  no per-person visibility yet, so the dispatcher refuses it from every key
  and the bundled `remember` skill asks the user instead.

`brain/commandpolicy.py` holds these lists. The client uses the same file
to choose which of its keys to present, and a test fails if any CLI
command is missing from it.

## Acting for someone else (delegation)

The remote gateway uses one pair of keys for everyone who signs in to it,
and tells the dispatcher per request whom it is acting for. Install such
keys with `--delegation`:

- `mandatory`: every request must name a principal. One that doesn't is
  refused, never run as the key's own `--principal`. Use this for the
  gateway.
- `optional`: a request may name a principal. Without one it runs as the
  key's own `--principal`. This suits a bot that is moving to per-person
  requests, because old and new versions of it both keep working.
- `none` (default): a request that names a principal is refused.

The named principal is checked exactly like a key's own: it must be
active. Delegation changes whom a request runs as, never what it may run,
because `--mode` still applies.

**A key with `--delegation optional` or `mandatory` can act as any active
person in the vault.** Give it to as few clients as possible. If such a key
leaks, treat it as identity spoofing, not as an ordinary credential leak.
Replace the keypair, replace its `authorized_keys` line (don't just add the
new key), and check the audit log for every request with
`transport=gateway` since the leak. [REMOTE_ACCESS.md](REMOTE_ACCESS.md)
has the full procedure for the gateway's keys.

## Turning someone off

On every connection the dispatcher reads the principal's record from the
vault itself, so `brain principal set-status <id> --status disabled` takes
effect on that person's next request, through every key and every gateway
token, without touching any key. If the check itself fails, the request is
refused.

## Upgrading

Upgrade the package in the server's venv. That upgrades the dispatcher with it,
so no separate step deploys or checks the dispatcher. Upgrade clients to
the same version: a client picks its key with the same `commandpolicy`
the server enforces.

## Moving from a separately deployed dispatcher

Servers set up before v0.15.0 ran a standalone `brain-dispatch.py` with the
vault path, state path, default principal and delegating client names
written into it. To move a key, edit its line:

1. Replace the script path: `.../python3 .../brain-dispatch.py` becomes
   `.../python3 -m brain.dispatch`, using the same venv's `python3`.
2. Add `--vault`, `--state-dir` and, if the old script set it,
   `--remote-project-paths`.
3. Keep the same `--mode`, `--principal` and `--client`. Spell out
   `--principal` even when it was the old default.
4. Give the gateway's two keys `--delegation mandatory`, and any key the
   old script treated as an optional delegator `--delegation optional`.
   Every other key needs nothing: `none` is the default.

Move one key at a time and leave the old script where it is until every
line has moved. For each key, run one command its mode allows (it must
work) and one it doesn't (it must print `refused: ...`). If a key stops
working, put its old line back.

## Reading a refusal

Every refusal is one line on stderr, starting with `refused:`:

| Message | Cause |
|---|---|
| `X is not an allowed read subcommand` | The key's mode doesn't include `X`. The client may have used its read key for a write. |
| `X is single-principal only ...` | `X` never goes through the dispatcher. Run it on the server. |
| `principal 'P' is disabled or does not exist` | `P` is disabled, missing, or the vault couldn't be read. |
| `identity 'C' is not permitted to assert an acting principal` | A key without `--delegation` sent a delegation prefix. |
| `identity 'C' must assert an acting principal for every request` | A `mandatory` key sent a request without one. |
| `malformed command` | The command couldn't be parsed (unbalanced quotes). |
