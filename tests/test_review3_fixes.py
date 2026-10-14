"""Regressions from the independent review; every vault is synthetic."""
import contextlib
import dataclasses
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from brain import (capture, cli, decision, frontmatter, handoff, identity, indexer,
                   listing, memoryops, mcp_server, projectops, search, update, validate,
                   visibility, timeline)
from tests.helpers import TempVault


class Review3Fixes(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.addCleanup(self.vault.cleanup)
        self.config = self.vault.config()

    def note(self, name, **kwargs):
        return self.vault.write_note('40_DECISIONS', name + '.md',
                                    id=kwargs.pop('id', name), type='decision',
                                    status='proposed', **kwargs)

    def snapshot(self):
        return {str(p.relative_to(self.vault.root)): p.read_bytes()
                for p in self.vault.root.rglob('*') if p.is_file()}

    def test_identity_alias_is_neither_readable_nor_writable(self):
        identity.create_principal(self.config, display_name='Target',
                                  principal_id='principal-target')
        target = self.vault.root / '90_SYSTEM/identity/principals/principal-target.md'
        alias = self.vault.root / '40_DECISIONS/alias.md'
        alias.symlink_to(target)
        before = target.read_bytes()
        self.assertEqual(listing.list_notes(self.config).total_visible, 0)
        with self.assertRaises(FileNotFoundError):
            visibility.read_visible_note_text(self.config, 'principal-marcin', 'principal-target')
        with self.assertRaises(FileNotFoundError):
            update.update_memory(self.config, 'principal-target', set_fields={'status': 'disabled'})
        self.assertEqual(target.read_bytes(), before)
        self.assertIsNone(indexer.index_note(self.config, alias))
        identity.set_principal_status(self.config, 'principal-target', 'disabled')
        self.assertFalse(identity.is_active(self.config, 'principal-target'))

    def test_cached_alias_retargeted_to_identity_does_not_leak(self):
        alias = self.note('alias', body='uniqueword')
        indexer.rebuild(self.config)
        target = self.vault.write_note('90_SYSTEM/identity/principals', 'principal-target.md',
                                      id='principal-target', type='principal', status='active',
                                      body='uniqueword')
        alias.unlink()
        alias.symlink_to(target)
        self.assertEqual(search.search(self.config, 'uniqueword'), [])
        self.assertEqual(indexer.count_by_type(self.config), {})

    def test_direct_handoff_identity_alias_refuses_before_writing(self):
        projectops.create_project(self.config, 'project-x', 'X', '/synthetic/x')
        target = self.vault.write_note('90_SYSTEM/identity/principals', 'principal-target.md',
                                      id='principal-target', type='principal', status='active')
        alias = handoff.handoff_path(self.config, 'project-x')
        alias.symlink_to(target)
        before = target.read_bytes()
        self.assertIsNone(handoff.read_latest(self.config, 'project-x'))
        with self.assertRaises(handoff.HandoffError):
            handoff.write(self.config, 'project-x', handoff.HandoffSections(changed='new'))
        self.assertEqual(target.read_bytes(), before)

    def test_internal_content_alias_still_works_and_duplicates_block(self):
        target = self.note('visible')
        alias = self.vault.root / '60_KNOWLEDGE/alias.md'
        alias.symlink_to(target)
        self.assertEqual(listing.list_notes(self.config).total_visible, 2)
        with self.assertRaises(update.AmbiguousNoteIdError):
            update.update_memory(self.config, 'visible', append_text='new')

    def test_hidden_single_and_duplicates_match_missing_refusal(self):
        self.note('hidden', owner_principal='principal:other')
        for i in range(3):
            self.note('copy' + str(i), id='hidden-duplicate', owner_principal='principal:other')
        before = self.snapshot()
        outcomes = []
        for ident in ['absent', 'hidden', 'hidden-duplicate']:
            with self.assertRaises(FileNotFoundError) as error:
                update.update_memory(self.config, ident, append_text='new')
            outcomes.append(str(error.exception).replace(ident, 'ID'))
        self.assertEqual(len(set(outcomes)), 1)
        self.assertNotIn(str(self.vault.root), outcomes[0])
        self.assertNotIn('other', outcomes[0])
        self.assertEqual(self.snapshot(), before)

    def test_supersedes_hidden_target_leaves_no_new_record(self):
        self.note('hidden', owner_principal='principal:other')
        before = self.snapshot()
        with self.assertRaises(decision.DecisionError) as error:
            decision.create_decision(self.config, title='New', supersedes='hidden')
        self.assertNotIn('other', str(error.exception))
        self.assertEqual(self.snapshot(), before)

    def test_mixed_duplicates_block_but_visible_single_updates(self):
        mine = self.note('mine', id='shared')
        self.note('theirs', id='shared', owner_principal='principal:other')
        before = self.snapshot()
        with self.assertRaises(update.AmbiguousNoteIdError):
            update.update_memory(self.config, 'shared', append_text='new')
        self.assertEqual(self.snapshot(), before)
        own = self.note('own')
        update.update_memory(self.config, 'own', append_text='new')
        self.assertIn('new', own.read_text())
        self.assertTrue(mine.exists())

    def test_invalid_acl_types_only_prevent_verified_completeness(self):
        self.note('visible')
        baseline = listing.result_to_dict(listing.list_notes(self.config))
        path = self.note('hidden', owner_principal='principal:other')
        note = frontmatter.parse_file(path)
        for key, values in [('audience', [7, False, {}, {'principal:marcin': True}, [7], [False], [[]]]),
                            ('owner_principal', [7, False, [], {}, '', 'group:team'])]:
            for value in values:
                with self.subTest(key=key, value=value):
                    note.meta = {'id': 'hidden', 'type': 'decision', 'status': 'proposed',
                                 'owner_principal': 'principal:other', 'audience': []}
                    note.meta[key] = value
                    path.write_text(frontmatter.render(note))
                    self.assertFalse(visibility.can_view_note(self.config, 'principal-marcin', note))
                    # Invalid ACLs deny content but cannot establish a known
                    # hidden record. Only completeness may change; entries,
                    # counts and every other public field stay identical.
                    self.assertEqual({**baseline, 'complete': False, 'source_complete': False},
                                     listing.result_to_dict(listing.list_notes(self.config)))
                    with self.assertRaises(FileNotFoundError):
                        update.update_memory(self.config, 'hidden', append_text='new')

    def test_invalid_acl_diagnostics_are_admin_only(self):
        path = self.note('hidden', owner_principal='principal:other')
        note = frontmatter.parse_file(path)
        note.meta['audience'] = 7
        path.write_text(frontmatter.render(note))
        self.assertTrue(validate.check_invalid_acl([note]))
        self.assertIn("invalid_acl", [p.check for p in validate.run_all(self.config)])
        with patch.object(validate, 'is_admin_principal', return_value=False):
            problems = validate.run_all(self.config, principal_id='principal-stranger')
        self.assertEqual([p.check for p in problems], ['admin_required'])
        self.assertNotIn('hidden', str(problems))

    def test_cli_hidden_update_matches_missing(self):
        self.note('hidden', owner_principal='principal:other')
        outcomes = []
        for ident in ['absent', 'hidden']:
            err = io.StringIO()
            args = SimpleNamespace(id=ident, set=[], append_text='new')
            with contextlib.redirect_stderr(err):
                result = cli.cmd_update(self.config, args)
            outcomes.append((result, err.getvalue().replace(ident, 'ID')))
        self.assertEqual(outcomes[0], outcomes[1])

    def test_mcp_read_and_write_respect_the_same_boundary(self):
        identity.create_principal(self.config, display_name='Target',
                                  principal_id='principal-target')
        target = self.vault.root / '90_SYSTEM/identity/principals/principal-target.md'
        (self.vault.root / '40_DECISIONS/alias.md').symlink_to(target)
        self.note('hidden', owner_principal='principal:other')
        before = self.snapshot()
        for ident in ['principal-target', 'hidden', 'absent']:
            with self.subTest(id=ident):
                with self.assertRaises(FileNotFoundError):
                    mcp_server.tool_read_memory(self.config, ident)
                with self.assertRaises(FileNotFoundError):
                    mcp_server.tool_update_memory(self.config, ident, append_text='new')
        self.assertEqual(self.snapshot(), before)

    def test_looping_identity_directory_does_not_hide_ordinary_content(self):
        self.note('visible')
        (self.vault.root / '90_SYSTEM/identity').symlink_to('identity')
        self.assertEqual(listing.list_notes(self.config).total_visible, 1)

    def test_hidden_handoff_refusal_does_not_name_its_owner(self):
        projectops.create_project(self.config, 'project-x', 'X', '/synthetic/x')
        path = handoff.write(self.config, 'project-x', handoff.HandoffSections(changed='old'))
        note = frontmatter.parse_file(path)
        note.meta['owner_principal'] = 'principal:other'
        path.write_text(frontmatter.render(note))
        before = path.read_bytes()
        with self.assertRaises(handoff.HandoffError) as error:
            handoff.write(self.config, 'project-x', handoff.HandoffSections(changed='new'))
        self.assertNotIn('other', str(error.exception))
        self.assertEqual(path.read_bytes(), before)

    def test_creation_through_protected_directory_alias_is_refused(self):
        protected = self.vault.root / '90_SYSTEM/identity/principals'
        protected.mkdir(parents=True)
        cases = [
            ('00_INBOX', lambda: capture.capture(self.config, 'fact', 'New')),
            ('40_DECISIONS', lambda: decision.create_decision(self.config, 'New')),
            ('50_TIMELINE', lambda: timeline.create_event(self.config, 'New', '2026-01-01')),
            ('60_KNOWLEDGE', lambda: memoryops.create_memory(self.config, 'knowledge', 'New')),
        ]
        for directory, action in cases:
            with self.subTest(directory=directory):
                path = self.vault.root / directory
                path.rmdir()
                path.symlink_to(protected, target_is_directory=True)
                try:
                    with self.assertRaises(ValueError):
                        action()
                    self.assertEqual(list(protected.iterdir()), [])
                finally:
                    path.unlink()
                    path.mkdir()

    def test_hidden_inbox_encoding_fault_does_not_change_status_counter(self):
        path = self.vault.write_note('00_INBOX', 'hidden.md', id='hidden', type='fact',
                                     owner_principal='principal:other')
        self.assertEqual(indexer.count_inbox_pending(self.config), 0)
        path.write_bytes(path.read_bytes() + b'\xff')
        self.assertEqual(indexer.count_inbox_pending(self.config), 0)

    def test_hidden_project_write_alias_matches_absent_alias(self):
        other = dataclasses.replace(self.config, acting_principal='principal-other')
        projectops.create_project(other, 'project-secret', 'Secret', '/synthetic/secret',
                                  aliases=['hidden-alias'])
        before = self.snapshot()
        actions = [
            (projectops.ProjectWriteError,
             lambda ident: projectops.set_project_status(self.config, ident, 'on-hold')),
            (projectops.SectionEditError,
             lambda ident: projectops.update_section(self.config, ident,
                                                      'Current state', 'append', 'new')),
        ]
        for error_type, action in actions:
            outcomes = []
            for ident in ['absent-alias', 'hidden-alias']:
                with self.assertRaises(error_type) as error:
                    action(ident)
                outcomes.append(str(error.exception).replace(ident, 'ID'))
            self.assertEqual(outcomes[0], outcomes[1])
            self.assertNotIn('project-secret', outcomes[1])
            self.assertEqual(self.snapshot(), before)
