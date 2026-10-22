"""brain.dispatch — the SSH forced-command dispatcher's security boundary.

Ported from the deployment-tooling copy the dispatcher lived in before
v0.15.0 (79 tests there). Routing cases now exercise
commandpolicy.is_allowed, which the dispatcher calls; delegation cases use
the explicit --delegation level that replaced the hardcoded client-name
sets.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from brain import dispatch
from brain import identity as identity_mod
from brain.commandpolicy import WRITE_ONLY_PAIRS, is_allowed
from brain.dispatch import (
    DelegationError,
    parse_delegated_command,
    principal_allowed,
    resolve_effective_principal,
)

BASE_ARGS = ["--vault", "/srv/vault", "--state-dir", "/srv/state"]


class TestReadModeBasics(unittest.TestCase):
    def test_read_allows_search(self):
        self.assertTrue(is_allowed("read", ["search", "widget"])[0])

    def test_read_allows_bare_timeline(self):
        self.assertTrue(is_allowed("read", ["timeline", "--limit", "5"])[0])

    def test_read_allows_list(self):
        """Without this `list` is refused over the SSH proxy, the MCP bridge
        and the remote gateway (found in the v0.14.6 review)."""
        self.assertTrue(is_allowed("read", ["list", "--type", "decision",
                                            "--status", "proposed", "--json"])[0])

    def test_list_has_no_write_subcommand_to_leak(self):
        """`list` is granted by top-level name, which is only safe while it
        has no mutating subcommand — one added later must join
        WRITE_ONLY_PAIRS the way `timeline add` did."""
        self.assertEqual([pair for pair in WRITE_ONLY_PAIRS if pair[0] == "list"], [])

    def test_capture_and_status_modes_do_not_gain_list(self):
        self.assertFalse(is_allowed("capture", ["list"])[0])
        self.assertFalse(is_allowed("status", ["list"])[0])

    def test_read_refuses_host_only_command(self):
        allowed, reason = is_allowed("read", ["backup"])
        self.assertFalse(allowed)
        self.assertIn("backup", reason)

    def test_read_refuses_empty_command(self):
        self.assertFalse(is_allowed("read", [])[0])


class TestProjectReadOnlySubcommandsGrantedToReadIdentity(unittest.TestCase):
    """project show/discover/sync write nothing and work under read."""

    def test_project_show_allowed_under_read(self):
        self.assertTrue(is_allowed("read", ["project", "show", "project-x"])[0])

    def test_project_discover_allowed_under_read(self):
        self.assertTrue(is_allowed("read", ["project", "discover"])[0])

    def test_project_sync_allowed_under_read(self):
        self.assertTrue(is_allowed("read", ["project", "sync", "project-x"])[0])


class TestProjectWriteSubcommandsStillRejectedUnderReadIdentity(unittest.TestCase):
    """Granting the three read-only project subcommands must not open any
    project subcommand that mutates state, nor any other write."""

    def test_project_create_refused_under_read(self):
        allowed, reason = is_allowed(
            "read", ["project", "create", "--id", "project-x", "--name", "X", "--path", "/tmp/x"])
        self.assertFalse(allowed)
        self.assertIn("project", reason)

    def test_project_update_refused_under_read(self):
        self.assertFalse(is_allowed("read", ["project", "update", "project-x", "--status", "on-hold"])[0])

    def test_project_close_refused_under_read(self):
        self.assertFalse(is_allowed("read", ["project", "close", "project-x"])[0])

    def test_project_section_update_refused_under_read(self):
        self.assertFalse(is_allowed(
            "read", ["project", "section-update", "project-x", "--section", "Next actions",
                     "--mode", "append", "--content", "x"])[0])

    def test_other_writes_still_refused_under_read(self):
        for argv in (["decision", "create", "--title", "x"],
                     ["remember", "--type", "fact", "--title", "x"],
                     ["timeline", "add", "--title", "x", "--date", "2026-01-01"],
                     ["handoff", "write", "--project", "project-x"],
                     ["note", "create", "--type", "fact", "--title", "x"],
                     ["update", "some-id", "--append-text", "x"]):
            with self.subTest(argv=argv):
                self.assertFalse(is_allowed("read", argv)[0])


class TestWriteMode(unittest.TestCase):
    def test_project_show_still_allowed_under_write(self):
        self.assertTrue(is_allowed("write", ["project", "show", "project-x"])[0])

    def test_project_create_allowed_under_write(self):
        self.assertTrue(is_allowed(
            "write", ["project", "create", "--id", "project-x", "--name", "X", "--path", "/tmp/x"])[0])

    def test_project_close_allowed_under_write(self):
        self.assertTrue(is_allowed("write", ["project", "close", "project-x"])[0])

    def test_timeline_add_allowed_under_write(self):
        self.assertTrue(is_allowed("write", ["timeline", "add", "--title", "x", "--date", "2026-01-01"])[0])

    def test_timeline_add_refused_under_capture(self):
        self.assertFalse(is_allowed("capture", ["timeline", "add", "--title", "x", "--date", "2026-01-01"])[0])


class TestSinglePrincipalCommandsNeverDispatched(unittest.TestCase):
    """memoryqueue.py's INVARIANT: the pending-memory queue has no ownership
    or visibility fields, so no key — above all a delegating one — may reach
    it through the dispatcher."""

    def test_memory_refused_in_every_mode(self):
        for mode in dispatch.commandpolicy.MODES:
            for argv in (["memory", "add", "--fact", "x"], ["memory", "pending"],
                         ["memory", "review"], ["memory", "accept", "id"]):
                with self.subTest(mode=mode, argv=argv):
                    allowed, reason = is_allowed(mode, argv)
                    self.assertFalse(allowed)
                    self.assertIn("single-principal", reason)


class TestCaptureMode(unittest.TestCase):
    """Exactly {remember, index}: no project read-only grant, no reads."""

    def test_project_show_refused_under_capture(self):
        self.assertFalse(is_allowed("capture", ["project", "show", "project-x"])[0])

    def test_remember_allowed_under_capture(self):
        self.assertTrue(is_allowed("capture", ["remember", "--type", "fact", "--title", "x"])[0])

    def test_search_refused_under_capture(self):
        self.assertFalse(is_allowed("capture", ["search", "x"])[0])


class TestStatusMode(unittest.TestCase):
    """Exactly {status}: narrower than read, no write path at all."""

    def test_status_allowed_under_status_mode(self):
        self.assertTrue(is_allowed("status", ["status"])[0])

    def test_everything_else_refused_under_status_mode(self):
        for argv in (["search", "x"], ["get", "some-id"],
                     ["remember", "--type", "fact", "--title", "x"],
                     ["project", "show", "project-x"], []):
            with self.subTest(argv=argv):
                self.assertFalse(is_allowed("status", argv)[0])


class TestPrincipalAllowed(unittest.TestCase):
    def test_active_principal_allowed(self):
        self.assertEqual(principal_allowed(lambda pid: True, "principal-alex"), (True, ""))

    def test_disabled_principal_refused(self):
        allowed, reason = principal_allowed(lambda pid: False, "principal-sam")
        self.assertFalse(allowed)
        self.assertIn("principal-sam", reason)
        self.assertIn("disabled", reason)

    def test_check_is_called_with_the_exact_principal_id_given(self):
        seen = []
        principal_allowed(lambda pid: seen.append(pid) or True, "principal-kim")
        self.assertEqual(seen, ["principal-kim"])


class TestPrincipalActiveFailsClosed(unittest.TestCase):
    def test_error_reading_the_vault_refuses(self):
        with mock.patch.object(dispatch.identity, "is_active", side_effect=OSError("gone")):
            self.assertFalse(dispatch._principal_active("/nonexistent", "principal-alex"))

    def test_unreadable_config_refuses(self):
        with mock.patch.object(dispatch, "default_config", side_effect=RuntimeError("bad")):
            self.assertFalse(dispatch._principal_active("/nonexistent", "principal-alex"))


class TestParser(unittest.TestCase):
    def test_vault_and_state_dir_are_required(self):
        with mock.patch.object(sys, "stderr"):
            with self.assertRaises(SystemExit):
                dispatch.build_parser().parse_args([])

    def test_defaults(self):
        args = dispatch.build_parser().parse_args(BASE_ARGS)
        self.assertEqual(args.mode, "read")
        self.assertEqual(args.delegation, "none")
        self.assertEqual(args.principal, dispatch.DEFAULT_ACTING_PRINCIPAL)
        self.assertEqual(args.client, "")
        self.assertFalse(args.remote_project_paths)

    def test_unknown_mode_or_delegation_rejected(self):
        for bad in (["--mode", "admin"], ["--delegation", "always"]):
            with self.subTest(bad=bad), mock.patch.object(sys, "stderr"):
                with self.assertRaises(SystemExit):
                    dispatch.build_parser().parse_args(BASE_ARGS + bad)


def run_main(*, argv, ssh_original_command, principal_active=True, brain_exists=True):
    """Runs dispatch.main() with SSH_ORIGINAL_COMMAND set and os.execv
    intercepted. Returns {'path', 'argv', 'env'} if execv was reached, {}
    if main() refused first. `principal_active` is what the live principal
    check reports for whichever principal main() checks."""
    captured = {}

    def fake_execv(path, argv_list):
        captured["path"] = path
        captured["argv"] = argv_list
        captured["env"] = dict(os.environ)

    env = {"SSH_ORIGINAL_COMMAND": ssh_original_command}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(dispatch, "_principal_active", lambda vault, pid: principal_active), \
         mock.patch.object(dispatch, "_brain_executable",
                           lambda: Path("/venv/bin/brain")), \
         mock.patch.object(Path, "is_file", lambda self: brain_exists), \
         mock.patch.object(dispatch.os, "execv", fake_execv), \
         mock.patch.object(sys, "stderr"):
        for key in ("BRAIN_CALLER_OAUTH_CLIENT_ID", "BRAIN_CALLER_OAUTH_CLIENT_NAME",
                    "BRAIN_REMOTE_PROJECT_PATHS"):
            os.environ.pop(key, None)
        try:
            dispatch.main(BASE_ARGS + argv)
        except SystemExit:
            pass
    return captured


class TestMainEnvironment(unittest.TestCase):
    """The underlying `brain` process learns its vault, state dir and
    caller only from the forced command's own flags."""

    def test_vault_and_state_dir_reach_env(self):
        captured = run_main(argv=[], ssh_original_command="status")
        self.assertEqual(captured["env"]["BRAIN_ROOT"], "/srv/vault")
        self.assertEqual(captured["env"]["BRAIN_STATE_DIR"], "/srv/state")
        self.assertEqual(captured["path"], "/venv/bin/brain")
        self.assertEqual(captured["argv"], ["/venv/bin/brain", "status"])

    def test_remote_project_paths_only_when_flagged(self):
        self.assertNotIn("BRAIN_REMOTE_PROJECT_PATHS",
                         run_main(argv=[], ssh_original_command="status")["env"])
        self.assertEqual(run_main(argv=["--remote-project-paths"],
                                  ssh_original_command="status")["env"]["BRAIN_REMOTE_PROJECT_PATHS"], "1")

    def test_leading_brain_word_is_dropped(self):
        captured = run_main(argv=[], ssh_original_command="brain search widget")
        self.assertEqual(captured["argv"][1:], ["search", "widget"])

    def test_fixed_principal_flag_reaches_env_as_caller_principal(self):
        captured = run_main(argv=["--principal", "principal-sam"], ssh_original_command="status")
        self.assertEqual(captured["env"]["BRAIN_CALLER_PRINCIPAL"], "principal-sam")

    def test_omitted_principal_uses_the_package_default(self):
        captured = run_main(argv=[], ssh_original_command="status")
        self.assertEqual(captured["env"]["BRAIN_CALLER_PRINCIPAL"], dispatch.DEFAULT_ACTING_PRINCIPAL)

    def test_disabled_principal_never_reaches_execv(self):
        self.assertEqual(run_main(argv=["--principal", "principal-sam"],
                                  ssh_original_command="status", principal_active=False), {})

    def test_ordinary_key_gets_ssh_transport_tag(self):
        captured = run_main(argv=["--client", "laptop"], ssh_original_command="status")
        self.assertEqual(captured["env"]["BRAIN_CALLER_TRANSPORT"], "ssh")
        self.assertEqual(captured["env"]["BRAIN_CALLER_CLIENT"], "laptop")

    def test_refused_command_never_reaches_execv(self):
        self.assertEqual(run_main(argv=["--mode", "read"],
                                  ssh_original_command="decision create --title x"), {})

    def test_malformed_shell_quoting_is_refused(self):
        self.assertEqual(run_main(argv=[], ssh_original_command='search "unterminated'), {})

    def test_missing_brain_executable_is_refused(self):
        self.assertEqual(run_main(argv=[], ssh_original_command="status", brain_exists=False), {})

    def test_shell_metacharacters_are_never_interpreted(self):
        captured = run_main(argv=[], ssh_original_command="search 'x; rm -rf /'")
        self.assertEqual(captured["argv"][1:], ["search", "x; rm -rf /"])


class TestParseDelegatedCommand(unittest.TestCase):
    """Pure syntax; whether an assertion is honoured is tested separately."""

    def test_no_prefix_returns_none_and_original_argv(self):
        self.assertEqual(parse_delegated_command(["context", "widget"]),
                         (None, None, None, ["context", "widget"]))

    def test_well_formed_prefix_is_extracted(self):
        self.assertEqual(
            parse_delegated_command(["--acting-principal", "principal-sam", "--", "context", "widget"]),
            ("principal-sam", None, None, ["context", "widget"]))

    def test_missing_value_raises(self):
        with self.assertRaises(DelegationError):
            parse_delegated_command(["--acting-principal"])

    def test_missing_separator_raises(self):
        with self.assertRaises(DelegationError):
            parse_delegated_command(["--acting-principal", "principal-sam", "context"])

    def test_empty_remaining_command_is_not_itself_an_error(self):
        # An empty real command is refused by is_allowed, not by parsing.
        self.assertEqual(parse_delegated_command(["--acting-principal", "principal-sam", "--"]),
                         ("principal-sam", None, None, []))

    def test_client_id_and_name_are_extracted(self):
        self.assertEqual(parse_delegated_command([
            "--acting-principal", "principal-sam",
            "--acting-client-id", "oauth-client-abc123",
            "--acting-client-name", "Desktop Agent",
            "--", "context", "widget",
        ]), ("principal-sam", "oauth-client-abc123", "Desktop Agent", ["context", "widget"]))

    def test_client_id_alone_without_client_name_is_fine(self):
        self.assertEqual(parse_delegated_command([
            "--acting-principal", "principal-sam", "--acting-client-id", "oauth-client-abc123",
            "--", "status",
        ]), ("principal-sam", "oauth-client-abc123", None, ["status"]))

    def test_client_flags_in_reverse_order_are_accepted(self):
        _, client_id, client_name, _ = parse_delegated_command([
            "--acting-principal", "principal-sam",
            "--acting-client-name", "Web Assistant", "--acting-client-id", "oauth-client-xyz",
            "--", "status",
        ])
        self.assertEqual((client_id, client_name), ("oauth-client-xyz", "Web Assistant"))

    def test_acting_client_id_missing_value_raises(self):
        with self.assertRaises(DelegationError):
            parse_delegated_command(["--acting-principal", "principal-sam", "--acting-client-id"])

    def test_acting_client_id_without_leading_acting_principal_is_not_delegation(self):
        argv = ["--acting-client-id", "oauth-client-abc123", "status"]
        self.assertEqual(parse_delegated_command(argv), (None, None, None, argv))


class TestResolveEffectivePrincipal(unittest.TestCase):
    """The delegation trust decision, against the pure function."""

    def test_delegating_key_with_assertion_is_honoured(self):
        for level in ("mandatory", "optional"):
            with self.subTest(level=level):
                self.assertEqual(resolve_effective_principal(
                    level, "gateway-read", "principal-alex", "principal-sam"), ("principal-sam", ""))

    def test_mandatory_key_with_no_assertion_is_refused_not_defaulted(self):
        # A gateway request that forgot to delegate must never run as the
        # key's own principal.
        effective, reason = resolve_effective_principal(
            "mandatory", "gateway-read", "principal-alex", None)
        self.assertIsNone(effective)
        self.assertIn("must assert an acting principal", reason)

    def test_optional_key_with_no_assertion_uses_its_own_principal(self):
        """What lets a delegating client start asserting without a lockstep
        server change: its unmodified traffic keeps working."""
        self.assertEqual(resolve_effective_principal(
            "optional", "assistant-query", "principal-alex", None), ("principal-alex", ""))

    def test_non_delegating_key_asserting_anything_is_refused(self):
        # Including a key whose *name* looks like a gateway's: rights come
        # from --delegation, never from the client name.
        for client in ("laptop", "", "gateway-read"):
            with self.subTest(client=client):
                effective, reason = resolve_effective_principal(
                    "none", client, "principal-alex", "principal-sam")
                self.assertIsNone(effective)
                self.assertIn("not permitted to assert", reason)

    def test_non_delegating_key_without_assertion_uses_its_own_principal(self):
        self.assertEqual(resolve_effective_principal(
            "none", "laptop", "principal-alex", None), ("principal-alex", ""))


class TestDelegationEndToEnd(unittest.TestCase):
    """main() with the adversarial cases expressed as SSH_ORIGINAL_COMMAND
    content plus the forced command's flags."""

    GATEWAY = ["--client", "gateway-read", "--delegation", "mandatory"]
    ASSISTANT = ["--client", "assistant-capture", "--delegation", "optional", "--mode", "capture"]

    def test_mandatory_delegation_honoured(self):
        captured = run_main(argv=self.GATEWAY,
                            ssh_original_command="--acting-principal principal-sam -- status")
        self.assertEqual(captured["env"]["BRAIN_CALLER_PRINCIPAL"], "principal-sam")
        self.assertEqual(captured["env"]["BRAIN_CALLER_TRANSPORT"], "gateway")
        self.assertEqual(captured["env"]["BRAIN_CALLER_CLIENT"], "gateway-read")
        self.assertEqual(captured["argv"][1:], ["status"])

    def test_asserting_a_disabled_principal_is_refused(self):
        self.assertEqual(run_main(argv=self.GATEWAY,
                                  ssh_original_command="--acting-principal principal-sam -- status",
                                  principal_active=False), {})

    def test_mandatory_key_without_assertion_is_refused(self):
        self.assertEqual(run_main(argv=self.GATEWAY, ssh_original_command="status"), {})

    def test_ordinary_key_cannot_forge_an_assertion(self):
        self.assertEqual(run_main(argv=["--client", "laptop"],
                                  ssh_original_command="--acting-principal principal-sam -- status"), {})

    def test_delegation_never_widens_the_mode(self):
        self.assertEqual(run_main(
            argv=self.GATEWAY + ["--mode", "read"],
            ssh_original_command="--acting-principal principal-sam -- decision create --title x"), {})
        self.assertEqual(run_main(
            argv=self.ASSISTANT,
            ssh_original_command="--acting-principal principal-sam -- decision create --title x"), {})

    def test_delegating_key_still_cannot_reach_the_memory_queue(self):
        self.assertEqual(run_main(
            argv=["--client", "gateway-write", "--delegation", "mandatory", "--mode", "write"],
            ssh_original_command="--acting-principal principal-sam -- memory review"), {})

    def test_optional_key_without_assertion_runs_as_itself(self):
        captured = run_main(argv=self.ASSISTANT + ["--principal", "principal-alex"],
                            ssh_original_command="remember --type knowledge --title x --text y --json")
        self.assertEqual(captured["env"]["BRAIN_CALLER_PRINCIPAL"], "principal-alex")
        self.assertEqual(captured["env"]["BRAIN_CALLER_TRANSPORT"], "ssh")

    def test_malformed_delegation_prefix_is_refused(self):
        self.assertEqual(run_main(argv=self.GATEWAY,
                                  ssh_original_command="--acting-principal principal-sam status"), {})


class TestOAuthClientProvenance(unittest.TestCase):
    """The gateway's validated OAuth client reaches the `brain` process as
    BRAIN_CALLER_OAUTH_CLIENT_ID/_NAME; BRAIN_CALLER_CLIENT stays the key
    that connected."""

    GATEWAY = ["--client", "gateway-write", "--delegation", "mandatory"]

    def test_delegated_request_carries_oauth_client_id_and_name(self):
        captured = run_main(argv=self.GATEWAY, ssh_original_command=(
            "--acting-principal principal-sam --acting-client-id oauth-client-1 "
            '--acting-client-name "Desktop Agent (Sam)" -- status'))
        self.assertEqual(captured["env"]["BRAIN_CALLER_OAUTH_CLIENT_ID"], "oauth-client-1")
        self.assertEqual(captured["env"]["BRAIN_CALLER_OAUTH_CLIENT_NAME"], "Desktop Agent (Sam)")
        self.assertEqual(captured["env"]["BRAIN_CALLER_CLIENT"], "gateway-write")
        self.assertEqual(captured["env"]["BRAIN_CALLER_PRINCIPAL"], "principal-sam")

    def test_missing_client_fields_do_not_break_the_request(self):
        captured = run_main(argv=self.GATEWAY,
                            ssh_original_command="--acting-principal principal-sam -- status")
        self.assertEqual(captured["env"]["BRAIN_CALLER_OAUTH_CLIENT_ID"], "")
        self.assertEqual(captured["env"]["BRAIN_CALLER_OAUTH_CLIENT_NAME"], "")

    def test_ordinary_key_cannot_forge_oauth_client_id(self):
        self.assertEqual(run_main(argv=["--client", "laptop"], ssh_original_command=(
            "--acting-principal principal-sam --acting-client-id forged -- status")), {})

    def test_non_delegated_caller_gets_no_oauth_client_env_vars(self):
        captured = run_main(argv=["--client", "laptop"], ssh_original_command="status")
        self.assertNotIn("BRAIN_CALLER_OAUTH_CLIENT_ID", captured["env"])
        self.assertNotIn("BRAIN_CALLER_OAUTH_CLIENT_NAME", captured["env"])



class TestRunsAsARealModule(unittest.TestCase):
    """`python -m brain.dispatch` in a real subprocess, no mocks: the exact
    form an authorized_keys forced command uses."""

    def setUp(self):
        from tests.helpers import TempVault
        self.vault = TempVault()
        self.addCleanup(self.vault.cleanup)
        self.principal = identity_mod.create_principal(
            self.vault.config(), display_name="Alex", role="admin").id

    def _run(self, command, *flags, isolated=False):
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith("BRAIN_")}
        env["SSH_ORIGINAL_COMMAND"] = command
        return subprocess.run(
            [sys.executable, *(["-I"] if isolated else []), "-m", "brain.dispatch",
             "--vault", str(self.vault.root),
             "--state-dir", str(self.vault.state_dir), *flags],
            env=env, capture_output=True, text=True, timeout=60,
            cwd=Path(__file__).resolve().parent.parent)

    def test_unknown_principal_is_refused(self):
        result = self._run("status", "--principal", "principal-nobody")
        self.assertEqual(result.returncode, 2)
        self.assertIn("refused: principal 'principal-nobody'", result.stderr)

    def test_memory_queue_is_refused_for_a_real_principal(self):
        result = self._run("memory review", "--mode", "write", "--principal", self.principal)
        self.assertEqual(result.returncode, 2)
        self.assertIn("single-principal", result.stderr)

    @unittest.skipUnless((Path(sys.executable).parent / "brain").is_file(),
                         "package not installed in this interpreter's environment")
    def test_allowed_command_runs_the_installed_brain(self):
        # -I exactly as a forced command runs it; needs the installed package.
        result = self._run("status", "--principal", self.principal, isolated=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
