"""Synthetic regressions for access checks and honest metadata listings."""
import dataclasses
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brain import cli, frontmatter, handoff, listing, mcp_server, projectops, visibility
from tests.helpers import TempVault


class FixCase(unittest.TestCase):
    def setUp(self):
        self.vault = TempVault()
        self.addCleanup(self.vault.cleanup)
        self.config = self.vault.config()
        self.other = dataclasses.replace(self.config, acting_principal='principal-other')

    def snapshot(self):
        return {str(p.relative_to(self.vault.root)): p.read_bytes()
                for p in self.vault.root.rglob('*') if p.is_file()}

    def project(self):
        return projectops.create_project(self.config, 'project-example', 'Synthetic project',
                                         '/synthetic/example', audience=['principal:other'])

    def note(self, **kwargs):
        return self.vault.write_note('60_KNOWLEDGE', 'example.md', id='knowledge-example',
                                     type='knowledge', **kwargs)


class TestStatusWriteAuthorization(FixCase):
    def test_hidden_handoff_refuses_before_any_document_changes(self):
        self.project()
        target = handoff.write(self.other, 'project-example', handoff.HandoffSections(changed='original'))
        self.assertFalse(visibility.can_view_note(self.config, self.config.acting_principal,
                                                frontmatter.parse_file(target)))
        before = self.snapshot()
        for operation in (
                lambda: mcp_server.tool_update_project_status(self.config, 'project-example', 'on-hold'),
                lambda: mcp_server.tool_close_project(self.config, 'project-example', summary='close')):
            with self.assertRaises(projectops.ProjectWriteError) as caught:
                operation()
            self.assertEqual(str(caught.exception), projectops.STATUS_WRITE_DENIED)
            self.assertEqual(before, self.snapshot())
            self.assertFalse((self.config.projects_dir / 'ON-HOLD').exists())

    def test_visible_nonowned_handoff_has_the_same_refusal_as_hidden(self):
        self.project()
        p = handoff.write(self.other, 'project-example', handoff.HandoffSections(changed='original'),
                          audience=[visibility.id_to_ref(self.config.acting_principal)])
        self.assertTrue(visibility.can_view_note(self.config, self.config.acting_principal,
                                               frontmatter.parse_file(p)))
        before = self.snapshot()
        with self.assertRaises(projectops.ProjectWriteError) as caught:
            projectops.set_project_status(self.config, 'project-example', 'on-hold')
        self.assertEqual(str(caught.exception), projectops.STATUS_WRITE_DENIED)
        self.assertEqual(before, self.snapshot())

    def test_owned_handoff_moves_with_project_and_preserves_bytes(self):
        self.project()
        old = handoff.write(self.config, 'project-example', handoff.HandoffSections(changed='original'))
        before = old.read_bytes()
        result = mcp_server.tool_update_project_status(self.config, 'project-example', 'on-hold')
        self.assertIsNotNone(result['handoff_moved'])
        self.assertFalse(old.exists())
        self.assertEqual((self.vault.root / result['handoff_moved']).read_bytes(), before)

    def test_malformed_handoff_is_refused_without_partial_status_change(self):
        self.project()
        p = handoff.handoff_path(self.config, 'project-example')
        for text in ('raw unclassified content', '---\nid: handoff-project-example\naudience: 7\n---\ntext'):
            p.write_text(text)
            before = self.snapshot()
            with self.assertRaises(projectops.ProjectWriteError) as caught:
                projectops.set_project_status(self.config, 'project-example', 'on-hold')
            self.assertEqual(str(caught.exception), projectops.STATUS_WRITE_DENIED)
            self.assertEqual(before, self.snapshot())

    def test_unreadable_handoff_is_refused_without_paths(self):
        self.project()
        p = handoff.write(self.config, 'project-example', handoff.HandoffSections(changed='original'))
        parse = frontmatter.parse_file
        before = self.snapshot()

        def failing_read(path):
            if path == p:
                raise PermissionError('synthetic-private-diagnostic')
            return parse(path)

        with patch.object(frontmatter, 'parse_file', side_effect=failing_read):
            with self.assertRaises(projectops.ProjectWriteError) as caught:
                projectops.set_project_status(self.config, 'project-example', 'on-hold')
        self.assertEqual(str(caught.exception), projectops.STATUS_WRITE_DENIED)
        self.assertEqual(before, self.snapshot())

    def test_unrelated_document_at_handoff_path_is_not_moved(self):
        self.project()
        p = handoff.write(self.config, 'project-example', handoff.HandoffSections(changed='original'))
        note = frontmatter.parse_file(p)
        note.meta['id'] = 'unrelated-document'
        p.write_text(frontmatter.render(note))
        before = self.snapshot()
        with self.assertRaises(projectops.ProjectWriteError):
            projectops.set_project_status(self.config, 'project-example', 'on-hold')
        self.assertEqual(before, self.snapshot())


class TestHandoffProjectVisibility(FixCase):
    def test_hidden_alias_and_missing_alias_have_identical_refusals(self):
        projectops.create_project(self.other, 'project-hidden', 'Synthetic hidden title',
                                  '/synthetic/hidden', aliases=['hidden-alias'])
        before = self.snapshot()
        outcomes = []
        for alias in ('missing-alias', 'hidden-alias'):
            with self.assertRaises(handoff.HandoffError) as caught:
                mcp_server.tool_write_handoff(self.config, alias, changed='synthetic change')
            outcomes.append(str(caught.exception).replace(alias, 'REQUEST'))
            self.assertEqual(before, self.snapshot())
        self.assertEqual(outcomes[0], outcomes[1])
        for forbidden in ('project-hidden', 'Synthetic hidden title', '/synthetic/hidden'):
            self.assertNotIn(forbidden, outcomes[1])

    def test_shared_project_allows_new_independently_owned_handoff(self):
        self.project()
        p = handoff.write(self.other, 'project-example', handoff.HandoffSections(changed='new'))
        self.assertEqual(frontmatter.parse_file(p).meta['owner_principal'], 'principal:other')
        self.assertFalse(visibility.can_view_note(self.config, self.config.acting_principal,
                                                frontmatter.parse_file(p)))

    def test_missing_or_malformed_project_record_does_not_authorize_creation(self):
        p = self.project()
        p.write_text('---\nowner_principal: [unclosed\n---\n')
        before = self.snapshot()
        with self.assertRaises(handoff.HandoffError):
            handoff.write(self.config, 'project-example', handoff.HandoffSections(changed='new'))
        self.assertEqual(before, self.snapshot())

    def test_unreadable_project_has_the_same_neutral_refusal(self):
        self.project()
        before = self.snapshot()
        with patch.object(handoff, 'find_visible_project',
                          side_effect=PermissionError('synthetic-private-diagnostic')):
            with self.assertRaises(handoff.HandoffError) as caught:
                handoff.write(self.config, 'project-example', handoff.HandoffSections(changed='new'))
        self.assertEqual(str(caught.exception), "no registered project with id or alias 'project-example'")
        self.assertEqual(before, self.snapshot())


class TestUnverifiedSource(FixCase):
    def assert_unverified(self, result):
        self.assertFalse(result.source_complete)
        self.assertFalse(result.complete)
        self.assertEqual(result.unreadable, 0)
        self.assertEqual(result.unstructured_skipped, 0)

    @unittest.skipIf(os.geteuid() == 0, 'permission test needs an unprivileged user')
    def test_unreadable_subdirectory_never_claims_completeness(self):
        p = self.vault.write_note('60_KNOWLEDGE/nested', 'example.md',
                                  id='knowledge-example', type='knowledge')
        self.assertEqual(listing.list_notes(self.config).total_visible, 1)
        p.parent.chmod(0)
        try:
            result = listing.list_notes(self.config)
        finally:
            p.parent.chmod(0o700)
        self.assertEqual(result.total_visible, 0)
        self.assert_unverified(result)

    def test_traversal_error_reveals_no_path_count_or_diagnostic(self):
        p = self.note()
        real_scandir = os.scandir

        def fail(path):
            if Path(path) == p.parent:
                raise PermissionError('synthetic-private-diagnostic')
            return real_scandir(path)

        with patch.object(os, 'scandir', side_effect=fail):
            result = listing.list_notes(self.config)
            payload = mcp_server.tool_list_notes(self.config)
        self.assert_unverified(result)
        serialized = json.dumps(payload)
        self.assertNotIn('synthetic-private-diagnostic', serialized)
        self.assertNotIn(str(p), serialized)
        self.assertNotIn(p.name, serialized)

    def test_top_level_stat_failure_is_unverified(self):
        self.note()
        real_stat = Path.lstat
        target = self.config.brain_root / '60_KNOWLEDGE'

        def fail(path):
            if path == target:
                raise PermissionError('synthetic-private-diagnostic')
            return real_stat(path)

        with patch.object(Path, 'lstat', fail):
            self.assert_unverified(listing.list_notes(self.config))

    def test_unreadable_file_with_unknown_acl_is_not_counted_or_complete(self):
        p = self.note(owner_principal='principal:other')
        real_open = Path.open

        def fail(path, *args, **kwargs):
            if path == p:
                raise PermissionError('synthetic-private-diagnostic')
            return real_open(path, *args, **kwargs)

        with patch.object(Path, 'open', fail):
            self.assert_unverified(listing.list_notes(self.config))

    def test_malformed_unknown_acl_is_not_assigned_to_default_owner(self):
        p = self.note()
        p.write_text('---\nowner_principal: principal:other\nid: [unclosed\n---\n')
        result = listing.list_notes(self.config)
        self.assert_unverified(result)
        self.assertEqual(result.entries, [])

    def test_parseable_yaml_with_invalid_acl_is_unverified_not_a_known_denial(self):
        p = self.note()
        original = frontmatter.parse_file(p)
        for field, value in (('audience', 7), ('owner_principal', [])):
            for bad_body in (False, True):
                with self.subTest(field=field, bad_body=bad_body):
                    note = dataclasses.replace(original, meta={**original.meta, field: value})
                    p.write_bytes(frontmatter.render(note).encode() + (b'\xff' if bad_body else b''))
                    self.assert_unverified(listing.list_notes(self.config))

    def test_resolution_access_failure_is_unverified(self):
        p = self.note()
        real_resolve = Path.resolve

        def fail(path, *args, **kwargs):
            if path == p:
                raise PermissionError('synthetic-private-diagnostic')
            return real_resolve(path, *args, **kwargs)

        with patch.object(Path, 'resolve', fail):
            self.assert_unverified(listing.list_notes(self.config))

    def test_known_hidden_acl_keeps_listing_identical_despite_bad_body(self):
        baseline = listing.result_to_dict(listing.list_notes(self.config))
        p = self.note(owner_principal='principal:other')
        p.write_bytes(p.read_bytes() + b'\xff')
        self.assertEqual(listing.result_to_dict(listing.list_notes(self.config)), baseline)

    def test_cli_unknown_source_does_not_imply_all_errors_are_visible(self):
        self.note().write_text('---\nowner_principal: [unclosed\n---\n')
        stdout, stderr = io.StringIO(), io.StringIO()
        args = SimpleNamespace(type=None, status=None, project=None, tag=None,
                               limit=100, offset=0, json=False)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(cli.cmd_list(self.config, args), 0)
        self.assertIn('NOT THE FULL LIST', stdout.getvalue())
        self.assertIn('could not be verified', stderr.getvalue())
        self.assertNotIn(str(self.vault.root), stderr.getvalue())


class TestMalformedListingMetadata(FixCase):
    def test_visible_invalid_metadata_is_counted_without_crashing(self):
        p = self.note()
        original = frontmatter.parse_file(p)
        for field, values in {
                'tags': [7, False, {}, [7], ['ok', None], [['nested']]],
                'projects': [7, False, {}, [7], ['ok', None]],
                'id': [7, True, [], {}, '', ' '],
                'type': [7, [], {}], 'status': [False, [], {}],
                'updated': [7, [], {}]}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    note = dataclasses.replace(original, meta={**original.meta, field: value})
                    p.write_text(frontmatter.render(note))
                    result = listing.list_notes(self.config)
                    self.assertEqual(result.entries, [])
                    self.assertEqual(result.unreadable, 1)
                    self.assertFalse(result.source_complete)
                    self.assertFalse(result.complete)

    def test_hidden_invalid_metadata_never_changes_visible_response(self):
        baseline = mcp_server.tool_list_notes(self.config)
        p = self.note(owner_principal='principal:other')
        note = frontmatter.parse_file(p)
        for field in ('tags', 'projects', 'id', 'type', 'status', 'updated'):
            with self.subTest(field=field):
                damaged = dataclasses.replace(note, meta={**note.meta, field: {'invalid': 7}})
                p.write_text(frontmatter.render(damaged))
                self.assertEqual(mcp_server.tool_list_notes(self.config), baseline)

    def test_bad_record_does_not_hide_good_entries_or_invent_filter_matches(self):
        p = self.note(tags=['target'])
        note = frontmatter.parse_file(p)
        note.meta['tags'] = {'target': True}
        p.write_text(frontmatter.render(note))
        self.vault.write_note('60_KNOWLEDGE', 'good.md', id='knowledge-good', type='knowledge', tags=['target'])
        result = listing.list_notes(self.config, tag='target')
        self.assertEqual([e.id for e in result.entries], ['knowledge-good'])
        self.assertEqual(result.unreadable, 1)
        self.assertFalse(result.source_complete)

    def test_yaml_dates_and_string_list_shorthand_remain_supported(self):
        p = self.note()
        note = frontmatter.parse_file(p)
        note.meta.update(tags='target', projects='project-example')
        p.write_text(frontmatter.render(note))
        result = listing.list_notes(self.config, tag='target', project='project-example')
        self.assertTrue(result.complete)
        self.assertEqual(result.entries[0].tags, ['target'])
        self.assertEqual(result.entries[0].projects, ['project-example'])


class TestReviewFollowUps(FixCase):
    """Review of PR #49: refusals the new resolver raises must reach the CLI
    user as a message, not a traceback, and `project close` must report what
    `project update --status` reports."""

    def run_cli(self, argv, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        env = {'BRAIN_ROOT': str(self.vault.root), 'BRAIN_STATE_DIR': str(self.vault.state_dir)}
        with patch.dict(os.environ, env), patch('sys.stdin', io.StringIO(stdin)), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def duplicate(self, path, folder):
        copy = self.config.projects_dir / folder / path.name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(path.read_bytes())
        return copy

    def test_stray_handoff_is_a_cli_refusal_not_a_traceback(self):
        self.project()
        self.duplicate(handoff.write(self.config, 'project-example',
                                     handoff.HandoffSections(changed='original')), 'ON-HOLD')
        handoff.handoff_path(self.config, 'project-example').unlink()
        before = self.snapshot()
        rc, _, err = self.run_cli(['handoff', 'write', '--project', 'project-example'],
                                  stdin=json.dumps({'changed': 'new'}))
        self.assertEqual(rc, 1)
        self.assertIn('Nothing was written', err)
        self.assertEqual(before, self.snapshot())

    def test_stray_handoff_hidden_from_caller_is_a_handoff_error(self):
        self.project()
        self.duplicate(handoff.write(self.other, 'project-example',
                                     handoff.HandoffSections(changed='original')), 'ON-HOLD')
        handoff.handoff_path(self.config, 'project-example').unlink()
        with self.assertRaises(handoff.HandoffError):
            handoff.write(self.config, 'project-example', handoff.HandoffSections(changed='new'))

    def test_duplicate_record_id_is_a_cli_refusal_for_close_and_section_update(self):
        record = self.project()
        self.duplicate(record, 'ON-HOLD')
        before = self.snapshot()
        for argv in (['project', 'close', 'project-example'],
                     ['project', 'section-update', 'project-example', '--section',
                      'Next actions', '--mode', 'append', '--content', 'x']):
            rc, _, err = self.run_cli(argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn('does not resolve to a single record', err)
        self.assertEqual(before, self.snapshot())

    def test_unreadable_scan_during_section_update_reveals_no_path(self):
        self.project()
        with patch.object(projectops, 'resolve_write_target',
                          side_effect=OSError(f'{self.vault.root}/secret.md')):
            with self.assertRaises(projectops.SectionEditError) as caught:
                projectops.update_section(self.config, 'project-example', 'Next actions',
                                          'append', 'x')
        self.assertEqual(str(caught.exception), projectops.SECTION_WRITE_DENIED)

    def test_converge_repairs_a_record_without_updated(self):
        record = self.project()
        text = record.read_text()
        text = text.replace('status: active', 'status: archived')
        text = '\n'.join(l for l in text.splitlines() if not l.startswith('updated:')) + '\n'
        record.write_text(text)
        result = projectops.set_project_status(self.config, 'project-example', 'archived')
        self.assertTrue(result.moved)
        self.assertTrue(frontmatter.parse_file(result.new_path).meta.get('updated'))

    def test_close_reports_index_and_handoff_like_update(self):
        self.project()
        handoff.write(self.config, 'project-example', handoff.HandoffSections(changed='original'))
        with patch.object(projectops.indexer, 'index_note', side_effect=RuntimeError('simulated')):
            rc, out, _ = self.run_cli(['project', 'close', 'project-example', '--json'])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertFalse(payload['index_updated'])
        self.assertTrue(payload['handoff_moved'].endswith('project-example-handoff.md'))

    def test_close_text_output_warns_when_only_the_index_is_behind(self):
        self.project()
        with patch.object(projectops.indexer, 'index_note', side_effect=RuntimeError('simulated')):
            rc, out, _ = self.run_cli(['project', 'close', 'project-example'])
        self.assertEqual(rc, 0)
        self.assertIn('run `brain index`', out)
