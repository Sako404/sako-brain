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

A durable fact the user mentions in passing, without asking for it to be \
saved, goes to queue_memory for their review rather than remember. \
Accept or reject a queued entry only when the user says so.

If Brain is unavailable, say so plainly rather than inventing an answer \
or falling back to a stale or cached version of its content.

Expert knowledge, if this Brain has it: reusable standards, playbooks and \
checklists are knowledge notes tagged kb:standard, kb:playbook or \
kb:checklist. When a task falls in a domain they cover, find only the \
ones relevant to it with get_context (a short query, tag set to one kb \
category) and list_notes (tag and status). Do not load the whole library \
for every task.
- Check each note's status. status current is approved guidance. status \
open is an unapproved draft: you may use it as supporting material, but \
say that it is a draft. \
Skip superseded notes in normal work. is_current in results only means \
"not historical"; it is not a substitute for status current.
- Cite the note and the stable rule ID you rely on (for example \
KB-WEB-STD L-13) together with the source the note gives for it. Keep law \
apart from regulator guidance, quality standards and rules marked \
HYPOTHESIS; only a rule the note states as law is a legal obligation.
- A decision recorded for the specific project takes precedence over a \
general kb recommendation, unless following it would break the law or a \
security requirement.
- Use your own tools and skills to do the work: these notes say what is \
required and how to check it. Do not copy them into your own instructions \
or configuration; read them from Brain when needed.
- If the knowledge you need is missing, only a draft, or past its \
review_by date, say so. Where queue_memory is available, offer to queue the \
gap for the user's review; never write or approve a kb note yourself.\
"""
