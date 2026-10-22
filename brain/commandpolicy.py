"""Which `brain` commands may be reached through the SSH forced-command
dispatcher, and with which identity — the single source of truth for both
sides of that connection.

Before v0.15.0 the dispatcher lived outside this package and the client
(`remote.py`) kept a hand-copied mirror of its allowlists. The two drifted
three times: `index` sent under the read identity (v0.11.1), a production
dispatcher older than the one in git (2026-10-02), and `memory`, which the
client sent under the write identity while the dispatcher refuses it in
every mode (2026-10-22). Both `brain.dispatch` and `remote.py` now read
these sets, and tests/test_commandpolicy.py fails the run that leaves any
CLI command unclassified.

Every top-level command is in exactly one of five categories:

  READ             safe for any identity in read mode or above
  WRITE            write mode only (plus the narrow capture mode, below)
  LOCAL_ONLY       configures this client; never sent anywhere
  SINGLE_PRINCIPAL never through the dispatcher in any mode: carries no
                   ownership/visibility of its own, so a delegating
                   identity (the remote gateway) could expose one
                   principal's data to another. See memoryqueue.py's
                   INVARIANT.
  HOST_ONLY        administration and host-local operations; runs only
                   where `brain` has direct filesystem access to the vault
                   (for a server, a local shell on that machine)

READ and WRITE are coarse, by top-level name; the two pair sets refine
them where one top-level name carries both kinds of subcommand.
"""
from __future__ import annotations

READ = frozenset({
    "state", "search", "context", "get", "projects", "timeline", "status",
    "doctor", "capabilities", "list",
})
WRITE = frozenset({
    "project", "decision", "update", "index", "note", "handoff", "remember",
})
LOCAL_ONLY = frozenset({"setup", "integration", "init", "remote-gateway"})
SINGLE_PRINCIPAL = frozenset({"memory"})
HOST_ONLY = frozenset({
    "principal", "group", "role-policy", "migrate-stage2-owner-principal",
    "agents-doc", "ask", "backup", "chat", "git", "integrity",
})

# A write under a READ top-level name: `timeline` lists, `timeline add` writes.
WRITE_ONLY_PAIRS = frozenset({("timeline", "add")})
# Reads under a WRITE top-level name, granted to read mode individually —
# never by adding all of `project` to READ, which would grant its writes too.
READ_ONLY_PAIRS = frozenset({
    ("project", "show"), ("project", "discover"), ("project", "sync"),
})

# Dispatcher modes. `capture` and `status` are deliberately narrower than
# read: an identity that exists to do one thing gets exactly that thing.
MODES = {
    "read": READ,
    "write": READ | WRITE,
    "capture": frozenset({"remember", "index"}),
    "status": frozenset({"status"}),
}


def is_allowed(mode: str, argv: list[str]) -> tuple[bool, str]:
    """(allowed, reason-if-refused) for running `argv` under `mode`. Pure —
    the dispatcher's routing decision, testable without a server."""
    if not argv:
        return False, f"(empty) is not an allowed {mode} subcommand"
    top = argv[0]
    pair = tuple(argv[:2]) if len(argv) >= 2 else None

    if top in SINGLE_PRINCIPAL:
        return False, (f"{top} is single-principal only and is never available "
                       "through the dispatcher; run it on the vault host")
    if mode == "read" and pair in READ_ONLY_PAIRS:
        return True, ""
    if top not in MODES[mode]:
        return False, f"{top} is not an allowed {mode} subcommand"
    if mode != "write" and pair in WRITE_ONLY_PAIRS:
        return False, f"'{' '.join(pair)}' is a write operation, not allowed for this identity"
    return True, ""


def uses_write_identity(argv: list[str]) -> bool:
    """Which of a client's two SSH identities to present for `argv`. Only a
    choice of key: the dispatcher enforces what each key may run. Anything
    that is not a write goes under the read identity — least privilege for
    commands the dispatcher will refuse anyway."""
    if not argv:
        return False
    pair = tuple(argv[:2]) if len(argv) >= 2 else None
    if pair in READ_ONLY_PAIRS:
        return False
    if pair in WRITE_ONLY_PAIRS:
        return True
    return argv[0] in WRITE
