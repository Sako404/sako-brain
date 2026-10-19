"""Author verification for the second review, with controlled writer overlap.

All data is synthetic and all writes are in temporary vaults. The barrier is
at the actual handoff rename after A has changed the record and registry;
B must contend at flock before A is allowed to fail and undo those changes.
"""
import queue
import threading
import unittest
from pathlib import Path
from unittest import mock

from brain import (decision, frontmatter, handoff, indexer, listing,
                   migrate_stage2, projectops, registry, update, validate, visibility)
from tests.helpers import TempVault


class CompletionCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.addCleanup(self.vault.cleanup)
        self.config = self.vault.config()

    def project(self, name="x"):
        projectops.create_project(self.config, id=f"project-{name}", name=name,
                                  path=str(self.vault.projects_root / name))
        handoff.write(self.config, f"project-{name}",
                      handoff.HandoffSections(attempted=f"original handoff {name}"))

    def assert_consistent(self, name, status, body=None):
        nid = f"project-{name}"
        paths = update.find_note_paths(self.config, nid)
        self.assertEqual(len(paths), 1)
        note = frontmatter.parse_file(paths[0])
        self.assertEqual(note.id, nid)
        self.assertEqual(note.status, status)
        folder = self.config.taxonomy.folder_for_status(status)
        self.assertEqual(paths[0].parent.name, folder)
        self.assertEqual(registry.find_project(self.config, nid).status, status)
        if body is not None:
            self.assertEqual(note.body, body)
        handoffs = update.find_note_paths(self.config, f"handoff-{nid}")
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0].parent.name, folder)
        self.assertIn(f"original handoff {name}", frontmatter.parse_file(handoffs[0]).body)
        return note


class TestVisibleSourceContract(CompletionCase):
    def test_default_owner_raw_capture_alone_is_an_incomplete_source(self):
        (self.vault.root / "00_INBOX" / "raw.md").write_text("raw thought\n")
        owner = listing.list_notes(self.config, principal_id="principal-marcin")
        self.assertEqual(owner.unstructured_skipped, 1)
        self.assertEqual(owner.unreadable, 0)
        self.assertFalse(owner.source_complete)
        self.assertFalse(owner.complete)
        stranger = listing.list_notes(self.config, principal_id="principal-stranger")
        self.assertEqual(stranger.unstructured_skipped, 0)
        self.assertTrue(stranger.source_complete)

    def test_invalid_body_preserves_header_acl_for_counters(self):
        for owner, audience in [("other", []), ("other", ["principal:marcin"]),
                                 ("marcin", [])]:
            with self.subTest(owner=owner, audience=audience):
                p = self.vault.write_note("60_KNOWLEDGE", "bad.md", id="bad",
                     type="knowledge", owner_principal=f"principal:{owner}", audience=audience)
                p.write_bytes(p.read_bytes() + b"\xff")
                r = listing.list_notes(self.config)
                expected = int(owner == "marcin" or bool(audience))
                self.assertEqual(r.unreadable, expected)
                self.assertEqual(r.source_complete, expected == 0)
                self.assertEqual(r.unstructured_skipped, 0)

    def test_untrusted_acl_in_malformed_yaml_is_never_default_owned(self):
        for key in ['owner_principal', '"owner_\\u0070rincipal"']:
            with self.subTest(key=key):
                p = self.vault.root / "60_KNOWLEDGE" / "broken.md"
                p.write_text(f"---\n{key}: principal:other\nid: [unclosed\n---\nbody\n")
                before = listing.list_notes(self.config)
                # Unknown visibility cannot establish source completeness;
                # it must still contribute no principal-visible counters.
                self.assertFalse(before.complete)
                self.assertFalse(before.source_complete)
                self.assertEqual(before.unreadable, 0)
                self.assertIsNone(frontmatter.diagnostic_metadata(p))

    def test_bom_note_is_listed_as_an_ordinary_note(self):
        p = self.vault.write_note("40_DECISIONS", "bom.md", id="bom", type="decision",
                                  status="proposed")
        p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
        r = listing.list_notes(self.config, type="decision", status="proposed")
        self.assertEqual([e.id for e in r.entries], ["bom"])
        self.assertTrue(r.source_complete)
        self.assertEqual((r.unreadable, r.unstructured_skipped), (0, 0))


class TestRecoveryContents(CompletionCase):
    def test_failed_reverse_rename_and_failed_restore_each_converge(self):
        for failed_inverse in ("rename", "record", "registry", "all"):
            with self.subTest(failed_inverse=failed_inverse):
                v = TempVault()
                self.addCleanup(v.cleanup)
                self.vault, self.config = v, v.config()
                self.project()
                original = frontmatter.parse_file(update.find_note_paths(self.config, "project-x")[0])
                original_handoff = handoff.handoff_path(self.config, "project-x").read_bytes()
                real_rename, real_write = Path.rename, Path.write_text
                undoing = False

                def rename(path, dest):
                    nonlocal undoing
                    if path.name == "project-x-handoff.md":
                        undoing = True
                        raise OSError("injected handoff failure")
                    if undoing and failed_inverse in ("rename", "all"):
                        raise OSError("injected reverse rename failure")
                    return real_rename(path, dest)

                def write(path, *args, **kw):
                    if undoing and (failed_inverse == "all" or
                         failed_inverse == "record" and path.name == "project-x.md" or
                         failed_inverse == "registry" and path == self.config.registry_path):
                        raise OSError("injected restore failure")
                    return real_write(path, *args, **kw)

                with mock.patch.object(Path, "rename", rename), mock.patch.object(Path, "write_text", write):
                    with self.assertRaises(projectops.ProjectWriteError) as raised:
                        projectops.set_project_status(self.config, "project-x", "on-hold")
                self.assertIn("partial state", str(raised.exception))
                records = update.find_note_paths(self.config, "project-x")
                self.assertEqual(len(records), 1)
                self.assertEqual(frontmatter.parse_file(records[0]).body, original.body)
                self.assertEqual(len(registry.load_registry(self.config)), 1)
                partial_handoffs = update.find_note_paths(self.config, "handoff-project-x")
                self.assertEqual(len(partial_handoffs), 1)
                self.assertEqual(partial_handoffs[0].read_bytes(), original_handoff)
                projectops.set_project_status(self.config, "project-x", "on-hold")
                self.assert_consistent("x", "on-hold", original.body)
                self.assertEqual(handoff.handoff_path(self.config, "project-x").read_bytes(), original_handoff)
                self.assertEqual(validate.check_handoff_in_wrong_status_folder(self.config), [])


@unittest.skipIf(projectops.fcntl is None, "requires POSIX flock")
class TestControlledWriterOverlap(CompletionCase):
    def overlap(self, action):
        paused, release = threading.Event(), threading.Event()
        events, results = queue.Queue(), {}
        real_rename, real_flock = Path.rename, projectops.fcntl.flock

        def rename(path, dest):
            if threading.current_thread().name == "failing-A" and path.name == "project-x-handoff.md":
                paused.set()
                if not release.wait(10):
                    raise RuntimeError("test barrier timed out")
                raise OSError("injected forward failure after registry write")
            return real_rename(path, dest)

        def flock(fd, operation):
            if threading.current_thread().name == "writer-B" and operation == projectops.fcntl.LOCK_EX:
                try:
                    real_flock(fd, operation | projectops.fcntl.LOCK_NB)
                except BlockingIOError:
                    events.put("blocked")
                else:
                    events.put("acquired-before-A-rollback")
                    return
            return real_flock(fd, operation)

        def run(label, fn):
            try:
                fn()
                results[label] = "success"
            except Exception as exc:
                results[label] = type(exc).__name__
            finally:
                if label == "B":
                    events.put("done")

        a = threading.Thread(name="failing-A", target=run,
             args=("A", lambda: projectops.set_project_status(self.config, "project-x", "on-hold")))
        b = threading.Thread(name="writer-B", target=run, args=("B", action))
        with mock.patch.object(Path, "rename", rename), mock.patch.object(projectops.fcntl, "flock", flock):
            a.start()
            try:
                self.assertTrue(paused.wait(10), "A did not reach its mutation barrier")
                b.start()
                self.assertEqual(events.get(timeout=10), "blocked", "B bypassed the shared lock")
            finally:
                release.set()
                a.join(10)
                if b.ident is not None:
                    b.join(10)
        self.assertFalse(a.is_alive())
        self.assertFalse(b.is_alive())
        self.assertEqual(results, {"A": "ProjectWriteError", "B": "success"})

    def test_same_project_success_survives_older_operation_rollback(self):
        self.project()
        original = frontmatter.parse_file(update.find_note_paths(self.config, "project-x")[0]).body
        self.overlap(lambda: projectops.set_project_status(self.config, "project-x", "completed"))
        self.assert_consistent("x", "completed", original)

    def test_different_projects_share_registry_without_lost_success(self):
        self.project()
        self.project("y")
        bodies = {n: frontmatter.parse_file(update.find_note_paths(self.config, f"project-{n}")[0]).body
                  for n in ("x", "y")}
        self.overlap(lambda: projectops.set_project_status(self.config, "project-y", "completed"))
        self.assert_consistent("x", "active", bodies["x"])
        self.assert_consistent("y", "completed", bodies["y"])

    def test_actual_write_entry_points_contend_before_reading_or_mutating(self):
        actions = {
            "generic-update": lambda: update.update_memory(self.config, "project-x", append_text="kept B"),
            "section": lambda: projectops.update_section(self.config, "project-x", "Current state", "append", "kept B"),
            "handoff": lambda: handoff.write(self.config, "project-x", handoff.HandoffSections(changed="kept B")),
            "create": lambda: projectops.create_project(self.config, "project-y", "Y", str(self.vault.projects_root / "y")),
            "registry-set": lambda: projectops.set_registry_fields(self.config, "project-x", category="kept B"),
            "registry-add": lambda: projectops.add_registry_entry(self.config, {"id": "project-y", "name": "Y", "status": "planned"}),
            "close": lambda: projectops.close_project(self.config, "project-x", "kept B"),
            "migration": lambda: migrate_stage2.migrate_owner_principal(self.config),
        }
        for name, action in actions.items():
            with self.subTest(entry_point=name):
                v = TempVault()
                self.addCleanup(v.cleanup)
                self.vault, self.config = v, v.config()
                self.project()
                record = update.find_note_paths(self.config, "project-x")[0]
                original = frontmatter.parse_file(record).body
                if name == "migration":
                    note = frontmatter.parse_file(record)
                    del note.meta["owner_principal"]
                    record.write_text(frontmatter.render(note))
                self.overlap(action)
                note = self.assert_consistent("x", "archived" if name == "close" else "active")
                if name in ("generic-update", "section", "close"):
                    self.assertIn("kept B", note.body)
                else:
                    self.assertEqual(note.body, original)
                if name == "handoff":
                    self.assertIn("kept B", handoff.handoff_path(self.config, "project-x").read_text())
                if name == "registry-set":
                    self.assertEqual(registry.find_project(self.config, "project-x").category, "kept B")
                if name in ("create", "registry-add"):
                    self.assertEqual({e.id for e in registry.load_registry(self.config)}, {"project-x", "project-y"})
                if name == "migration":
                    self.assertEqual(note.meta["owner_principal"], "principal:marcin")


class TestPrivacyAndScanBoundary(CompletionCase):
    def test_hidden_duplicates_equal_missing_and_visible_mixed_write_is_refused(self):
        for name in ("a", "b", "c"):
            self.vault.write_note("60_KNOWLEDGE", f"{name}.md", id="hidden", type="knowledge",
                                   owner_principal="principal:other")
        def refusal(nid):
            try:
                update.update_memory(self.config, nid, append_text="refused")
            except Exception as exc:
                return type(exc), str(exc).replace(nid, "ID")
            self.fail("write succeeded")
        self.assertEqual(refusal("hidden"), refusal("missing"))
        visible = self.vault.write_note("60_KNOWLEDGE", "visible.md", id="hidden", type="knowledge")
        before = {p: p.read_bytes() for p in self.vault.root.rglob("*.md")}
        with self.assertRaises(update.AmbiguousNoteIdError) as ctx:
            update.update_memory(self.config, "hidden", append_text="refused")
        self.assertNotIn("other", str(ctx.exception))
        self.assertNotIn(str(visible), str(ctx.exception))
        self.assertEqual(before, {p: p.read_bytes() for p in self.vault.root.rglob("*.md")})
        self.assertIn("Title", visibility.read_visible_note_text(self.config, "principal-marcin", "hidden"))

    def test_orphan_is_refused_then_status_retry_preserves_history(self):
        self.project()
        old = handoff.handoff_path(self.config, "project-x")
        original = old.read_bytes()
        orphan = self.config.projects_dir / "ON-HOLD" / old.name
        orphan.parent.mkdir(exist_ok=True)
        old.rename(orphan)
        with self.assertRaises(update.AmbiguousNoteIdError):
            handoff.write(self.config, "project-x", handoff.HandoffSections(changed="second"))
        self.assertFalse(old.exists())
        self.assertEqual(orphan.read_bytes(), original)
        projectops.set_project_status(self.config, "project-x", "active")
        handoff.write(self.config, "project-x", handoff.HandoffSections(changed="second"))
        self.assert_consistent("x", "active")
        self.assertIn("second", old.read_text())

    def test_outside_directory_and_loop_links_are_not_scanned_but_reported(self):
        external = self.vault.state_dir / "outside"
        external.mkdir(parents=True)
        (external / "outside.md").write_text("---\nid: outside\ntype: knowledge\n---\nbody")
        base = self.vault.root / "60_KNOWLEDGE"
        (base / "directory").symlink_to(external, target_is_directory=True)
        (base / "loop.md").symlink_to(base / "loop.md")
        self.assertEqual(listing.list_notes(self.config).entries, [])
        self.assertEqual(indexer.rebuild(self.config)["indexed"], 0)
        self.assertEqual(len(validate.check_symlink_escapes_vault(self.config)), 2)

    def test_external_handoff_is_neither_read_nor_written(self):
        self.project()
        p = handoff.handoff_path(self.config, "project-x")
        external = self.vault.state_dir / "outside-handoff.md"
        external.write_bytes(p.read_bytes())
        p.unlink()
        p.symlink_to(external)
        before = external.read_bytes()
        self.assertIsNone(handoff.read_latest(self.config, "project-x"))
        with self.assertRaises(handoff.HandoffError):
            handoff.write(self.config, "project-x", handoff.HandoffSections(changed="refused"))
        self.assertEqual(external.read_bytes(), before)

    def test_status_target_outside_vault_refuses_before_any_move(self):
        self.project()
        external = self.vault.state_dir / "outside-folder"
        external.mkdir()
        (self.config.projects_dir / "ON-HOLD").symlink_to(external, target_is_directory=True)
        before = self.config.registry_path.read_bytes()
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assert_consistent("x", "active")
        self.assertEqual(self.config.registry_path.read_bytes(), before)
        self.assertEqual(list(external.iterdir()), [])

    def test_registry_link_outside_vault_refuses_before_writing(self):
        self.project()
        path = self.config.registry_path
        external = self.vault.state_dir / "outside-registry.yaml"
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
        before = external.read_bytes()
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_registry_fields(self.config, "project-x", category="refused")
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_status(self.config, "project-x", "on-hold")
        self.assertEqual(external.read_bytes(), before)
        self.assert_consistent("x", "active")

    def test_content_directory_link_is_not_traversed(self):
        external = self.vault.state_dir / "outside-directory"
        external.mkdir(parents=True)
        (external / "outside.md").write_text("---\nid: outside\ntype: knowledge\n---\nbody")
        base = self.vault.root / "60_KNOWLEDGE"
        base.rmdir()
        base.symlink_to(external, target_is_directory=True)
        self.assertEqual(listing.list_notes(self.config).entries, [])
        self.assertEqual(indexer.rebuild(self.config)["indexed"], 0)
        problems = validate.check_symlink_escapes_vault(self.config)
        self.assertEqual(len(problems), 1)
        self.assertIn(str(base), problems[0].message)

    def test_supersedes_cannot_write_a_project_via_a_decision_command(self):
        self.project()
        before = {p: p.read_bytes() for p in self.vault.root.rglob("*.md")}
        with self.assertRaises(decision.DecisionError):
            decision.create_decision(self.config, "refused", supersedes="project-x")
        self.assertEqual(before, {p: p.read_bytes() for p in self.vault.root.rglob("*.md")})


if __name__ == "__main__":
    unittest.main()
