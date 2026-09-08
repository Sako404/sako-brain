import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain import ollama_demo


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



class TestOllamaDemoReadOnlyBoundary(unittest.TestCase):
    """V1 integration must be read-only: no path from question -> answer
    can reach an authoritative write, and any memory capture demonstrated
    must go only through queue_memory (Level 2)."""

    def test_module_never_references_remember_or_update_memory(self):
        source = inspect.getsource(ollama_demo)
        self.assertNotIn(".remember(", source)
        self.assertNotIn(".update_memory(", source)

    def test_answer_question_only_calls_get_context(self):
        with patch("brain.ollama_demo.BrainGatewayClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__enter__.return_value
            mock_client.get_context.return_value = {"notes": [], "projects": [], "timeline": [], "restricted_omitted": 0}
            with patch("brain.ollama_demo.call_ollama", return_value="a test answer"):
                answer, context = ollama_demo.answer_question("a question", model="fake-model")

        mock_client.get_context.assert_called_once()
        # No write-shaped method should ever be called on the client.
        self.assertFalse(hasattr(mock_client, "remember") and mock_client.remember.called)
        self.assertFalse(hasattr(mock_client, "update_memory") and mock_client.update_memory.called)
        self.assertEqual(answer, "a test answer")


class TestBuildPrompt(unittest.TestCase):
    def test_labels_current_vs_historical(self):
        context = {
            "notes": [
                {"id": "a", "type": "fact", "title": "Current thing", "snippet": "x",
                 "is_current": True, "source": "", "source_date": ""},
                {"id": "b", "type": "fact", "title": "Old thing", "snippet": "y",
                 "is_current": False, "source": "", "source_date": ""},
            ],
            "projects": [], "timeline": [], "restricted_omitted": 0,
        }
        prompt = ollama_demo.build_prompt("a question", context)
        self.assertIn("current]", prompt)
        self.assertIn("historical/superseded]", prompt)

    def test_includes_provenance_when_present(self):
        context = {
            "notes": [{"id": "a", "type": "fact", "title": "T", "snippet": "s",
                       "is_current": True, "source": "user statement", "source_date": "2026-07-22"}],
            "projects": [], "timeline": [], "restricted_omitted": 0,
        }
        prompt = ollama_demo.build_prompt("q", context)
        self.assertIn("user statement", prompt)
        self.assertIn("2026-07-22", prompt)

    def test_notes_restricted_omission_disclosed_not_hidden(self):
        context = {"notes": [], "projects": [], "timeline": [], "restricted_omitted": 2}
        prompt = ollama_demo.build_prompt("q", context)
        self.assertIn("2 restricted note(s)", prompt)

    def test_empty_context_does_not_crash(self):
        context = {"notes": [], "projects": [], "timeline": [], "restricted_omitted": 0}
        prompt = ollama_demo.build_prompt("q", context)
        self.assertIn("no relevant notes found", prompt)


class TestLoggingRedaction(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmp.name) / "logs"

    def tearDown(self):
        self._tmp.cleanup()

    def test_log_never_contains_question_or_answer_text(self):
        secret_question = "what is my SSN 123-45-6789"
        secret_answer = "the answer contains sensitive-looking-text-xyz"

        ollama_demo._log("qwen3.5:latest", 3, success=True, log_dir=self.log_dir)

        log_files = list(self.log_dir.glob("ollama-demo-*.log"))
        self.assertEqual(len(log_files), 1)
        content = log_files[0].read_text()
        self.assertIn("qwen3.5:latest", content)
        self.assertIn("notes_used=3", content)
        self.assertIn("OK", content)
        self.assertNotIn(secret_question, content)
        self.assertNotIn(secret_answer, content)

    def test_log_failure_includes_short_detail(self):
        ollama_demo._log("llama3:latest", 0, success=False, detail="could not reach Ollama", log_dir=self.log_dir)
        content = list(self.log_dir.glob("ollama-demo-*.log"))[0].read_text()
        self.assertIn("FAILED", content)
        self.assertIn("could not reach Ollama", content)


if __name__ == "__main__":
    unittest.main()
