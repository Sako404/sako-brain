"""The shared state-audit procedure must actually reach the clients.

Pins the distinction between ranked search and metadata filtering at every
surface a client reads, not just in the repository:

  - the stdio MCP server's own tool descriptions (`tools/list`) — the only
    surface a client that reads no file at all still sees, Hermes Agent and
    any other remote-MCP consumer included;
  - the Codex `AGENTS.md` snippet, merged into `~/.codex/AGENTS.md`;
  - the Claude Code `brain-recall` skill, installed into the user's skills
    directory;
  - the vault's own rendered `AGENTS.md`.

Each must carry the one rule, and link the full procedure by a URL that
works from a pip install (the `docs/` directory does not ship in the wheel).
"""
import unittest
from pathlib import Path

from brain import agentsdoc, mcp_server
from tests.helpers import TempVault

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCEDURE = REPO_ROOT / "docs" / "STATE_AUDIT_PROCEDURE.md"
PROCEDURE_URL = "https://github.com/Sako404/sako-brain/blob/main/docs/STATE_AUDIT_PROCEDURE.md"

PACKAGED_CLIENT_DOCS = (
    REPO_ROOT / "brain" / "integrations" / "codex" / "AGENTS_SNIPPET.md",
    REPO_ROOT / "brain" / "integrations" / "claude-code" / "skills" / "brain-recall" / "SKILL.md",
)


class TestProcedureDocument(unittest.TestCase):
    def test_it_exists(self):
        self.assertTrue(PROCEDURE.is_file())

    def test_it_states_the_five_steps(self):
        text = PROCEDURE.read_text()
        for step in ("fresh snapshot", "by metadata", "full record",
                     "latest handoff", "later closures"):
            self.assertIn(step, text.lower(), f"procedure does not mention '{step}'")

    def test_it_names_the_four_kinds_of_claim(self):
        text = PROCEDURE.read_text().lower()
        for kind in ("metadata status", "content state", "production-verified", "unverified"):
            self.assertIn(kind, text)

    def test_it_names_the_confusions_that_caused_wrong_reports(self):
        text = PROCEDURE.read_text()
        for marker in ("mcp_bridge.py", "mcp_server.py", "remote_gateway",
                        "v0.12.3", "v0.14.5"):
            self.assertIn(marker, text)

    def test_it_refuses_to_call_unverifiable_things_failures(self):
        text = PROCEDURE.read_text()
        self.assertIn("unverified", text)
        self.assertIn(".git", text)


class TestTheRuleReachesEveryClientSurface(unittest.TestCase):
    """The rule is: `search` is ranked full-text with a limit and no filter
    syntax; `list` is the complete metadata listing. Everything else in the
    procedure is elaboration — this one line is what was missing."""

    def _assert_carries_the_rule(self, text: str, where: str):
        lowered = text.lower()
        self.assertIn("full-text", lowered, f"{where}: does not say search is full-text")
        self.assertTrue(
            "list_notes" in text or "brain list" in text,
            f"{where}: does not point at the complete-listing tool",
        )
        self.assertIn("status: proposed", text,
                       f"{where}: does not name the query that actually went wrong")

    def test_mcp_tool_descriptions_carry_it(self):
        search_doc = mcp_server.TOOLS["search_memory"][0].__doc__ or ""
        self._assert_carries_the_rule(search_doc, "search_memory tool description")

    def test_the_listing_tool_describes_its_own_completeness_fields(self):
        doc = mcp_server.TOOLS["list_notes"][0].__doc__ or ""
        for field in ("total_visible", "complete", "next_offset"):
            self.assertIn(field, doc)

    def test_every_tool_is_advertised_with_a_real_description(self):
        """`search_memory` shipped with no docstring at all, so `tools/list`
        advertised the bare name — which is how a client could not know what
        it was getting. No tool should fall back to its own name again."""
        for tool in mcp_server._tools_list_payload():
            self.assertNotEqual(
                tool["description"], tool["name"],
                f"{tool['name']} has no description — it falls back to its own name",
            )

    def test_packaged_client_docs_carry_it(self):
        for path in PACKAGED_CLIENT_DOCS:
            self._assert_carries_the_rule(path.read_text(), str(path.name))

    def test_rendered_vault_agents_doc_carries_it(self):
        vault = TempVault()
        try:
            rendered = agentsdoc.render(vault.config())
        finally:
            vault.cleanup()
        self._assert_carries_the_rule(rendered, "rendered AGENTS.md")

    def test_pointers_use_a_url_that_survives_a_wheel_install(self):
        """`docs/` is not package data, so a repository-relative path would
        be a dead reference for anyone who pip-installed."""
        for path in (*PACKAGED_CLIENT_DOCS, REPO_ROOT / "brain" / "templates" / "AGENTS.md.template"):
            self.assertIn(PROCEDURE_URL, path.read_text(), str(path.name))


if __name__ == "__main__":
    unittest.main()
