"""brain.memoryops.file_note — moving an inbox capture to where its type
belongs, plus its CLI (`brain note file`) and MCP (`file_inbox_note`)
surfaces. Business logic first; the surfaces only check the wiring."""
from __future__ import annotations

import dataclasses
import json
import unittest

from brain import capture, commandpolicy, frontmatter, indexer, mcp_bridge, mcp_server, memoryops, timeline
from brain.remote_gateway import scopes
from tests.helpers import TempVault
from tests.test_cli_note_timeline import run_cli


class FileNoteTestCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.config = self.vault.config()
        (self.config.dir_for("areas") / "Family").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.vault.cleanup()

    def _as(self, principal_id: str):
        return dataclasses.replace(self.config, acting_principal=principal_id)

    def _capture(self, type_: str, title: str, config=None, **kwargs):
        path = capture.capture(config or self.config, type_=type_, title=title, **kwargs)
        return frontmatter.parse_file(path).id, path

    def _audit_lines(self) -> list[str]:
        log_files = list(self.config.logs_dir.glob("brain-audit-*.log"))
        return log_files[0].read_text(encoding="utf-8").splitlines() if log_files else []


class TestFileNote(FileNoteTestCase):
    def test_knowledge_moves_to_knowledge_dir_unchanged(self):
        note_id, src = self._capture("knowledge", "Widget internals", text="How it works.")
        before = src.read_text(encoding="utf-8")
        old, new = memoryops.file_note(self.config, note_id)
        self.assertEqual(old, src)
        self.assertEqual(new.parent, self.config.dir_for("knowledge"))
        self.assertFalse(src.exists())
        self.assertEqual(new.read_text(encoding="utf-8"), before)

    def test_fact_needs_an_existing_area(self):
        note_id, src = self._capture("fact", "Bins go out on Tuesday")
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.file_note(self.config, note_id)
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.file_note(self.config, note_id, area="Nope")
        self.assertTrue(src.exists())
        _, new = memoryops.file_note(self.config, note_id, area="Family")
        self.assertEqual(new.parent, self.config.dir_for("areas") / "Family")

    def test_event_moves_to_timeline(self):
        note_id, _ = self._capture("event", "Router replaced")
        _, new = memoryops.file_note(self.config, note_id)
        self.assertEqual(new.parent, self.config.dir_for("timeline"))

    def test_area_refused_for_fixed_destination_types(self):
        note_id, src = self._capture("knowledge", "Widget internals")
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.file_note(self.config, note_id, area="Family")
        self.assertTrue(src.exists())

    def test_decision_type_is_refused(self):
        note_id, src = self._capture("decision", "Pick a router")
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.file_note(self.config, note_id)
        self.assertTrue(src.exists())

    def test_note_outside_inbox_is_refused(self):
        path = memoryops.create_memory(self.config, "knowledge", "Already filed")
        note_id = frontmatter.parse_file(path).id
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.file_note(self.config, note_id)
        self.assertTrue(path.exists())

    def test_existing_destination_is_never_overwritten(self):
        note_id, src = self._capture("knowledge", "Widget internals")
        clash = self.config.dir_for("knowledge") / src.name
        clash.parent.mkdir(parents=True, exist_ok=True)
        clash.write_text("someone else's file\n", encoding="utf-8")
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.file_note(self.config, note_id)
        self.assertTrue(src.exists())
        self.assertEqual(clash.read_text(encoding="utf-8"), "someone else's file\n")

    def test_unknown_id(self):
        with self.assertRaises(FileNotFoundError):
            memoryops.file_note(self.config, "knowledge-does-not-exist")

    def test_index_follows_the_move(self):
        note_id, _ = self._capture("knowledge", "Widget internals")
        indexer.rebuild(self.config)
        _, new = memoryops.file_note(self.config, note_id)
        conn = indexer.connect(self.config)
        try:
            rows = conn.execute("SELECT path FROM notes WHERE id = ?", (note_id,)).fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], [str(new.relative_to(self.config.brain_root))])

    def test_move_is_audited(self):
        note_id, _ = self._capture("knowledge", "Widget internals")
        memoryops.file_note(self.config, note_id)
        self.assertTrue(any("event=note.move " in l and f"id={note_id}" in l
                            and "to=60_KNOWLEDGE/" in l for l in self._audit_lines()))


class TestFileNoteOwnership(FileNoteTestCase):
    def test_someone_elses_private_note_does_not_exist_for_the_caller(self):
        note_id, src = self._capture("knowledge", "Ania's note", config=self._as("principal-ania"))
        with self.assertRaises(FileNotFoundError):
            memoryops.file_note(self._as("principal-marcin"), note_id)
        self.assertTrue(src.exists())

    def test_shared_note_is_still_owner_only(self):
        note_id, src = self._capture("knowledge", "Ania's shared note", config=self._as("principal-ania"),
                                     audience=["principal:marcin"])
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.file_note(self._as("principal-marcin"), note_id)
        self.assertTrue(src.exists())
        self.assertTrue(any("event=note.move.denied" in l and f"id={note_id}" in l
                            for l in self._audit_lines()))

    def test_owner_can_move_their_own(self):
        ania = self._as("principal-ania")
        note_id, _ = self._capture("knowledge", "Ania's note", config=ania)
        _, new = memoryops.file_note(ania, note_id)
        self.assertEqual(new.parent, self.config.dir_for("knowledge"))


class TestCreateMemoryUnchanged(FileNoteTestCase):
    def test_event_still_has_no_create_primitive(self):
        with self.assertRaises(memoryops.MemoryWriteError):
            memoryops.create_memory(self.config, "event", "x")


class TestNoteFileCli(FileNoteTestCase):
    def test_json_output(self):
        note_id, src = self._capture("knowledge", "Widget internals")
        rc, out = run_cli(["note", "file", note_id, "--json"], self.vault)
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertEqual(result["moved_from"], str(src.relative_to(self.config.brain_root)))
        self.assertTrue(result["filed_path"].startswith("60_KNOWLEDGE/"))

    def test_fact_with_area(self):
        note_id, _ = self._capture("fact", "Bins go out on Tuesday")
        rc, out = run_cli(["note", "file", note_id, "--area", "Family"], self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("Filed 00_INBOX/", out)

    def test_refusal_is_exit_1(self):
        rc, _ = run_cli(["note", "file", "knowledge-does-not-exist"], self.vault)
        self.assertEqual(rc, 1)


class TestFileInboxNoteMcp(FileNoteTestCase):
    def _call(self, arguments):
        return mcp_server.handle_request(self.config, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "file_inbox_note", "arguments": arguments},
        })

    def test_tool_moves_the_note(self):
        note_id, _ = self._capture("event", "Router replaced")
        resp = self._call({"id": note_id})
        result = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(result["filed_path"].startswith(self.config.dir_for("timeline").name + "/"))

    def test_tool_refusal_is_an_error(self):
        resp = self._call({"id": "knowledge-does-not-exist"})
        self.assertIn("error", resp)

    def test_bridge_translation(self):
        argv, stdin = mcp_bridge._t_file_inbox_note({"id": "fact-x", "area": "Family"})
        self.assertEqual(argv, ["note", "file", "fact-x", "--json", "--area", "Family"])
        self.assertIsNone(stdin)

    def test_gateway_needs_write_scope(self):
        self.assertEqual(scopes.base_scope_for_tool("file_inbox_note"), scopes.SCOPE_WRITE)

    def test_dispatcher_allows_write_identity_only(self):
        argv = ["note", "file", "knowledge-x", "--json"]
        self.assertTrue(commandpolicy.is_allowed("write", argv)[0])
        for mode in ("read", "capture", "status"):
            self.assertFalse(commandpolicy.is_allowed(mode, argv)[0], mode)
        self.assertTrue(commandpolicy.uses_write_identity(argv))


if __name__ == "__main__":
    unittest.main()
