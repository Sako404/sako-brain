"""Vendor-neutral MCP server instructions (v0.12.0, item 10).

Returned in the `initialize` response's `instructions` field so any
compliant client — ChatGPT, Claude.ai, or a future one — gets the same
steering regardless of vendor, without per-client special-casing.
"""

SERVER_INSTRUCTIONS = """\
SAKO Brain is the user's canonical durable knowledge store: projects, \
decisions, session handoffs, facts, and a dated timeline.

For any question that may depend on prior projects, decisions, handoffs, \
or established facts, query Brain before answering when it would \
materially improve the answer — do not ask the user to repeat context \
Brain already has.

Never treat your own model memory as authoritative when Brain contains \
the relevant current state. Brain's content is always the more current, \
more trustworthy source for anything about the user's own projects, \
decisions, and history.

Never attempt to access a filesystem or vault path directly — only use \
the tools this server provides. There is no local or cached copy that \
is ever more current than what these tools return.

Every write tool enforces the same safety policy regardless of which \
client calls it: secret-shaped content is refused outright, and a write \
marked sensitivity=restricted is refused unless explicitly confirmed. \
This cannot be bypassed by asking differently.

If Brain is unavailable, say so plainly rather than inventing an answer \
or falling back to a stale or cached version of its content.\
"""
