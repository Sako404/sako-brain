"""Phase 5C tests — `brain ask` / `brain chat` (brain/assistant.py, plus the
CLI wiring in brain/cli.py).

Per the phase spec: do not assert on exact subjective prose wording from
Ollama (we never call a real model here — Ollama is always mocked).
Instead assert on the structural/policy guarantees: default model, the
restricted-data opt-in boundary, current-vs-historical policy content,
source-reference shape, the no-context short-circuit, Ollama-unavailable
handling, log redaction, and the pending-memory write boundary.
"""
import contextlib
import inspect
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain import assistant, cli
from tests.helpers import TempVault


# --- test isolation ---------------------------------------------------------
# Several tests below call assistant.ask() / answer_question() directly rather
# than through run_cli, and those paths resolve a vault from the current
# working directory. Run the suite from inside a real vault and this module's
# log lines land in that vault's real state directory. Caught immediately after
# OSS-1 moved runtime state out of the vault: the destination changed, but
# "tests write to a real location" did not. Module-level so a test added later
# cannot miss it.
_env_patch = None
_env_tmp = None


def setUpModule():
    global _env_patch, _env_tmp
    _env_tmp = tempfile.TemporaryDirectory()
    base = Path(_env_tmp.name)
    (base / "vault" / "90_SYSTEM").mkdir(parents=True)
    (base / "vault" / "90_SYSTEM" / "config.yaml").write_text("projects_root: /nonexistent\n")
    _env_patch = patch.dict(os.environ, {
        "BRAIN_ROOT": str(base / "vault"),
        "BRAIN_STATE_DIR": str(base / "state"),
    }, clear=False)
    _env_patch.start()


def tearDownModule():
    _env_patch.stop()
    _env_tmp.cleanup()



def run_cli(argv, vault):
    """See test_cli_phase5a.run_cli — BRAIN_STATE_DIR is required alongside
    BRAIN_ROOT since OSS-1 moved runtime state out of the vault."""
    out = io.StringIO()
    env = dict(os.environ)
    env["BRAIN_ROOT"] = str(vault.root)
    env["BRAIN_STATE_DIR"] = str(vault.state_dir)
    with patch.dict(os.environ, env, clear=False):
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
    return rc, out.getvalue()


def fake_context(notes=None, projects=None, timeline=None, restricted_omitted=0):
    return {
        "notes": notes or [], "projects": projects or [], "timeline": timeline or [],
        "restricted_omitted": restricted_omitted,
    }


NOTE_CURRENT = {
    "id": "fact-employment-evri-barnsley-hoyland", "type": "fact", "title": "Evri Barnsley placement",
    "snippet": "HGV placement confirmed.", "is_current": True, "source": "user statement",
    "source_date": "2026-07-19", "confidence": "fact", "sensitivity": "normal",
}
NOTE_HISTORICAL = {
    "id": "fact-old-job", "type": "fact", "title": "Old job", "snippet": "No longer current.",
    "is_current": False, "source": "user statement", "source_date": "2025-01-01",
    "confidence": "fact", "sensitivity": "normal",
}
NOTE_RESTRICTED = {
    "id": "fact-restricted-thing", "type": "fact", "title": "Restricted thing", "snippet": "sensitive.",
    "is_current": True, "source": "user statement", "source_date": "2026-01-01",
    "confidence": "fact", "sensitivity": "restricted",
}


class TestAskCoreBoundary(unittest.TestCase):
    """`ask()` must never write authoritative Brain records — only
    `get_context` (read) and `queue_memory` (Level 2, staged) may be
    called on the gateway client."""

    def test_module_never_references_remember_or_update_memory(self):
        source = inspect.getsource(assistant)
        self.assertNotIn(".remember(", source)
        self.assertNotIn(".update_memory(", source)

    def test_ask_calls_get_context_and_ollama(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_client = mock_cls.return_value.__enter__.return_value
            mock_client.get_context.return_value = fake_context([NOTE_CURRENT])
            with patch("brain.assistant.call_ollama", return_value="an answer") as mock_ollama:
                result = assistant.ask("a question", model="fake-model")

        mock_client.get_context.assert_called_once()
        mock_ollama.assert_called_once()
        self.assertEqual(result.answer, "an answer")
        self.assertFalse(hasattr(mock_client, "remember") and mock_client.remember.called)
        self.assertFalse(hasattr(mock_client, "update_memory") and mock_client.update_memory.called)


class TestDefaultAndOverrideModel(unittest.TestCase):
    def test_default_model_is_qwen(self):
        self.assertEqual(assistant.DEFAULT_MODEL, "qwen3.5:latest")

    def test_ask_uses_default_model_when_unspecified(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_cls.return_value.__enter__.return_value.get_context.return_value = fake_context([NOTE_CURRENT])
            with patch("brain.assistant.call_ollama", return_value="a") as mock_ollama:
                assistant.ask("q")
        self.assertEqual(mock_ollama.call_args.kwargs.get("model"), assistant.DEFAULT_MODEL)

    def test_ask_passes_through_model_override(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_cls.return_value.__enter__.return_value.get_context.return_value = fake_context([NOTE_CURRENT])
            with patch("brain.assistant.call_ollama", return_value="a") as mock_ollama:
                assistant.ask("q", model="mixtral:latest")
        self.assertEqual(mock_ollama.call_args.kwargs.get("model"), "mixtral:latest")

    def test_cli_ask_default_model_flows_to_assistant(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask") as mock_ask:
                mock_ask.return_value = assistant.AskResult(answer="ok", sources=[])
                rc, out = run_cli(["ask", "a question"], vault)
            self.assertEqual(rc, 0)
            self.assertEqual(mock_ask.call_args.kwargs.get("model"), assistant.DEFAULT_MODEL)
        finally:
            vault.cleanup()

    def test_cli_ask_model_override_flows_to_assistant(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask") as mock_ask:
                mock_ask.return_value = assistant.AskResult(answer="ok", sources=[])
                rc, out = run_cli(["ask", "--model", "llama3:latest", "a question"], vault)
            self.assertEqual(rc, 0)
            self.assertEqual(mock_ask.call_args.kwargs.get("model"), "llama3:latest")
        finally:
            vault.cleanup()


class TestRestrictedBoundary(unittest.TestCase):
    def test_ask_default_excludes_restricted_from_get_context_call(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_client = mock_cls.return_value.__enter__.return_value
            mock_client.get_context.return_value = fake_context([NOTE_CURRENT])
            with patch("brain.assistant.call_ollama", return_value="a"):
                result = assistant.ask("q")
        self.assertEqual(mock_client.get_context.call_args.kwargs.get("include_restricted"), False)
        self.assertFalse(result.restricted_used)

    def test_ask_restricted_flag_requests_restricted_context_and_flags_result(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_client = mock_cls.return_value.__enter__.return_value
            mock_client.get_context.return_value = fake_context([NOTE_RESTRICTED])
            with patch("brain.assistant.call_ollama", return_value="a"):
                result = assistant.ask("q", include_restricted=True)
        self.assertEqual(mock_client.get_context.call_args.kwargs.get("include_restricted"), True)
        self.assertTrue(result.restricted_used)

    def test_cli_prints_restricted_notice_only_when_used(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask") as mock_ask:
                mock_ask.return_value = assistant.AskResult(answer="ok", sources=[], restricted_used=True)
                rc, out = run_cli(["ask", "--restricted", "q"], vault)
            self.assertIn("Restricted Brain context was included", out)
        finally:
            vault.cleanup()

    def test_cli_no_restricted_notice_by_default(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask") as mock_ask:
                mock_ask.return_value = assistant.AskResult(answer="ok", sources=[], restricted_used=False)
                rc, out = run_cli(["ask", "q"], vault)
            self.assertNotIn("Restricted Brain context was included", out)
        finally:
            vault.cleanup()


class TestCurrentVsHistoricalPolicy(unittest.TestCase):
    """Do not assert exact prose — just that the required policy *concepts*
    are present in the prompt sent to the model, and that current/historical
    notes are visibly distinguished."""

    def test_policy_preamble_covers_required_concepts(self):
        preamble = assistant.POLICY_PREAMBLE.lower()
        self.assertIn("newer evidence", preamble)
        self.assertIn("historical", preamble)
        self.assertIn("latest known", preamble)
        self.assertIn("researched or considered", preamble)
        self.assertIn("decision", preamble)
        self.assertIn("say so plainly", preamble)

    def test_build_prompt_labels_current_and_historical_notes(self):
        context = fake_context([NOTE_CURRENT, NOTE_HISTORICAL])
        prompt = assistant.build_prompt("q", context)
        self.assertIn("| current]", prompt)
        self.assertIn("| historical/superseded]", prompt)
        self.assertIn(assistant.POLICY_PREAMBLE, prompt)

    def test_build_prompt_includes_provenance(self):
        context = fake_context([NOTE_CURRENT])
        prompt = assistant.build_prompt("q", context)
        self.assertIn("user statement", prompt)
        self.assertIn("2026-07-19", prompt)
        self.assertIn("fact", prompt)

    def test_build_prompt_discloses_restricted_omission(self):
        context = fake_context([], restricted_omitted=2)
        prompt = assistant.build_prompt("q", context)
        self.assertIn("2 restricted note(s)", prompt)


class TestSourceReferences(unittest.TestCase):
    def test_format_sources_compact_with_metadata(self):
        text = assistant.format_sources([NOTE_CURRENT])
        self.assertIn("Sources:", text)
        self.assertIn("fact-employment-evri-barnsley-hoyland", text)
        self.assertIn("2026-07-19", text)
        self.assertIn("fact", text)
        # compact -- must not dump raw YAML frontmatter keys
        self.assertNotIn("sensitivity:", text)

    def test_format_sources_empty(self):
        self.assertEqual(assistant.format_sources([]), "Sources: (none)")

    def test_cli_prints_sources_after_answer(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask") as mock_ask:
                mock_ask.return_value = assistant.AskResult(answer="the answer", sources=[NOTE_CURRENT])
                rc, out = run_cli(["ask", "q"], vault)
            self.assertIn("the answer", out)
            self.assertIn("Sources:", out)
            self.assertIn("fact-employment-evri-barnsley-hoyland", out)
        finally:
            vault.cleanup()


class TestNoContextHandling(unittest.TestCase):
    def test_ask_short_circuits_without_calling_ollama(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_cls.return_value.__enter__.return_value.get_context.return_value = fake_context()
            with patch("brain.assistant.call_ollama") as mock_ollama:
                result = assistant.ask("nothing relevant")
        mock_ollama.assert_not_called()
        self.assertTrue(result.no_context)
        self.assertEqual(result.sources, [])

    def test_cli_no_context_answer_has_no_sources_footer(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask") as mock_ask:
                mock_ask.return_value = assistant.AskResult(
                    answer="I couldn't find any relevant Brain context for this question, so I'm not going to guess.",
                    sources=[], no_context=True,
                )
                rc, out = run_cli(["ask", "q"], vault)
            self.assertNotIn("Sources:", out)
        finally:
            vault.cleanup()


class TestOllamaUnavailable(unittest.TestCase):
    def test_ask_propagates_ollama_error_and_logs_failure(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_cls.return_value.__enter__.return_value.get_context.return_value = fake_context([NOTE_CURRENT])
            with patch("brain.assistant.call_ollama", side_effect=assistant.OllamaError("could not reach Ollama")):
                with patch("brain.assistant._log") as mock_log:
                    with self.assertRaises(assistant.OllamaError):
                        assistant.ask("q")
        self.assertEqual(mock_log.call_args.kwargs.get("success"), False)

    def test_cli_ask_reports_ollama_error_cleanly(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask", side_effect=assistant.OllamaError("could not reach Ollama at ...")):
                rc, out = run_cli(["ask", "q"], vault)
            self.assertEqual(rc, 1)
        finally:
            vault.cleanup()

    def test_cli_ask_reports_model_not_found_cleanly(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask", side_effect=assistant.ModelNotFoundError("model missing")):
                rc, out = run_cli(["ask", "q"], vault)
            self.assertEqual(rc, 1)
        finally:
            vault.cleanup()


class TestLoggingRedaction(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmp.name) / "logs"

    def tearDown(self):
        self._tmp.cleanup()

    def test_log_contains_only_shape_not_content(self):
        secret_question = "what is my SSN 123-45-6789"
        secret_answer = "the answer contains sensitive-looking-text-xyz"

        assistant._log("qwen3.5:latest", 3, success=True, duration=1.23, log_dir=self.log_dir)

        log_files = list(self.log_dir.glob("assistant-*.log"))
        self.assertEqual(len(log_files), 1)
        content = log_files[0].read_text()
        self.assertIn("qwen3.5:latest", content)
        self.assertIn("notes=3", content)
        self.assertIn("duration=1.23s", content)
        self.assertIn("OK", content)
        self.assertNotIn(secret_question, content)
        self.assertNotIn(secret_answer, content)

    def test_log_file_is_private(self):
        assistant._log("qwen3.5:latest", 1, success=True, duration=0.1, log_dir=self.log_dir)
        log_path = list(self.log_dir.glob("assistant-*.log"))[0]
        import stat
        mode = log_path.stat().st_mode
        self.assertEqual(mode & stat.S_IRWXG, 0)
        self.assertEqual(mode & stat.S_IRWXO, 0)

    def test_ask_end_to_end_never_logs_question_or_answer(self):
        secret_question = "what is my SSN 123-45-6789"
        secret_answer = "the confidential answer text"
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_cls.return_value.__enter__.return_value.get_context.return_value = fake_context([NOTE_CURRENT])
            with patch("brain.assistant.call_ollama", return_value=secret_answer):
                with patch("brain.assistant._log") as mock_log:
                    assistant.ask(secret_question)
        for call in mock_log.call_args_list:
            for arg in list(call.args) + list(call.kwargs.values()):
                if isinstance(arg, str):
                    self.assertNotIn(secret_question, arg)
                    self.assertNotIn(secret_answer, arg)


class TestPendingMemoryBoundary(unittest.TestCase):
    def test_durable_statement_detected(self):
        self.assertIsNotNone(assistant.detect_memory_candidate("I sold the Fiat today"))
        self.assertIsNotNone(assistant.detect_memory_candidate("I quit my job at Evri"))
        self.assertIsNotNone(assistant.detect_memory_candidate("we moved to a new house"))

    def test_non_durable_question_not_detected(self):
        self.assertIsNone(assistant.detect_memory_candidate("What is my latest known HGV employment?"))
        self.assertIsNone(assistant.detect_memory_candidate("Tell me about the Fiat"))

    def test_detected_candidate_queued_via_queue_memory_only(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_client = mock_cls.return_value.__enter__.return_value
            mock_client.get_context.return_value = fake_context([NOTE_CURRENT])
            mock_client.queue_memory.return_value = {"queued_id": "mem-test123"}
            with patch("brain.assistant.call_ollama", return_value="an answer"):
                result = assistant.ask("I sold the Fiat today, what should I do about insurance?")

        mock_client.queue_memory.assert_called_once()
        candidate_fact = mock_client.queue_memory.call_args.kwargs.get("candidate_fact")
        self.assertIn("sold the Fiat", candidate_fact)
        self.assertEqual(result.queued_memory, {"queued_id": "mem-test123"})
        self.assertFalse(hasattr(mock_client, "remember") and mock_client.remember.called)
        self.assertFalse(hasattr(mock_client, "update_memory") and mock_client.update_memory.called)

    def test_non_durable_question_does_not_queue(self):
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_client = mock_cls.return_value.__enter__.return_value
            mock_client.get_context.return_value = fake_context([NOTE_CURRENT])
            with patch("brain.assistant.call_ollama", return_value="an answer"):
                result = assistant.ask("What is my latest known HGV employment?")

        mock_client.queue_memory.assert_not_called()
        self.assertIsNone(result.queued_memory)

    def test_queue_memory_failure_does_not_break_the_answer(self):
        from brain.gateway_client import BrainGatewayError
        with patch("brain.assistant.BrainGatewayClient") as mock_cls:
            mock_client = mock_cls.return_value.__enter__.return_value
            mock_client.get_context.return_value = fake_context([NOTE_CURRENT])
            mock_client.queue_memory.side_effect = BrainGatewayError("boom")
            with patch("brain.assistant.call_ollama", return_value="an answer"):
                result = assistant.ask("I sold the Fiat today")
        self.assertEqual(result.answer, "an answer")
        self.assertIsNone(result.queued_memory)

    def test_cli_prints_queued_memory_footer(self):
        vault = TempVault()
        try:
            with patch("brain.cli.assistant.ask") as mock_ask:
                mock_ask.return_value = assistant.AskResult(
                    answer="ok", sources=[], queued_memory={"queued_id": "mem-abc123"},
                )
                rc, out = run_cli(["ask", "I sold the Fiat today"], vault)
            self.assertIn("Queued pending-memory candidate: mem-abc123", out)
        finally:
            vault.cleanup()


class TestChatRepl(unittest.TestCase):
    def _run(self, inputs, **kwargs):
        it = iter(inputs)
        def fake_input(prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        printed = []
        def fake_print(text=""):
            printed.append(text)
        history = assistant.run_chat(input_fn=fake_input, print_fn=fake_print, **kwargs)
        return history, printed

    def test_exit_immediately_never_calls_ask(self):
        with patch("brain.assistant.ask") as mock_ask:
            history, printed = self._run(["/exit"])
        mock_ask.assert_not_called()
        self.assertEqual(history, [])

    def test_model_and_restricted_commands_do_not_call_ask(self):
        with patch("brain.assistant.ask") as mock_ask:
            history, printed = self._run(["/model llama3:latest", "/restricted on", "/restricted", "/exit"])
        mock_ask.assert_not_called()
        joined = "\n".join(printed)
        self.assertIn("llama3:latest", joined)
        self.assertIn("Restricted context: on", joined)

    def test_question_calls_ask_fresh_each_time_not_fed_history(self):
        result1 = assistant.AskResult(answer="first answer", sources=[NOTE_CURRENT])
        result2 = assistant.AskResult(answer="second answer", sources=[NOTE_HISTORICAL])
        with patch("brain.assistant.ask", side_effect=[result1, result2]) as mock_ask:
            history, printed = self._run(["first question", "second question", "/exit"])
        self.assertEqual(mock_ask.call_count, 2)
        for call in mock_ask.call_args_list:
            self.assertNotIn("history", call.kwargs)
            self.assertNotIn("previous", call.kwargs)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["answer"], "first answer")
        self.assertEqual(history[1]["answer"], "second answer")

    def test_sources_command_reflects_last_answer(self):
        result = assistant.AskResult(answer="an answer", sources=[NOTE_CURRENT])
        with patch("brain.assistant.ask", return_value=result):
            history, printed = self._run(["a question", "/sources", "/exit"])
        joined = "\n".join(printed)
        self.assertIn("fact-employment-evri-barnsley-hoyland", joined)

    def test_chat_never_persists_to_disk(self):
        """Nothing in run_chat should touch the filesystem beyond what ask()
        itself does (which is separately tested/logged) -- the REPL loop
        must not open any file of its own."""
        source = inspect.getsource(assistant.run_chat)
        self.assertNotIn("open(", source)
        self.assertNotIn(".write_text(", source)


if __name__ == "__main__":
    unittest.main()
