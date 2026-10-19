import subprocess
import unittest
from unittest.mock import patch

from brain import systemdstatus


def response(stdout='', code=0, stderr=''):
    return subprocess.CompletedProcess(['systemctl'], code, stdout, stderr)


class TestSystemdStatus(unittest.TestCase):
    def test_absence_requires_successful_load_state_query(self):
        with patch.object(systemdstatus, '_systemctl', return_value=response('not-found\n')) as query:
            status = systemdstatus.timer_status('synthetic.timer')
        self.assertEqual((status.enabled, status.active, status.diagnostic), ('not-found', 'unknown', ''))
        self.assertEqual(query.call_count, 1)

    def test_unavailable_manager_does_not_mean_missing_unit(self):
        for result in (None, response('', 1, '/private/path synthetic-sensitive-diagnostic'),
                       response('not-found', 1)):
            with self.subTest(result=result), patch.object(systemdstatus, '_systemctl', return_value=result):
                status = systemdstatus.timer_status('synthetic.timer')
                self.assertEqual((status.enabled, status.active), ('unknown', 'unknown'))
                self.assertEqual(status.diagnostic, 'manager-query-failed')
                self.assertNotIn('/private', repr(status))

    def test_normalized_states(self):
        cases = [('enabled', 0, 'active', 0, 'enabled', 'active'),
                 ('disabled', 1, 'inactive', 3, 'disabled', 'inactive'),
                 ('static', 0, 'failed', 3, 'disabled', 'failed'),
                 ('masked', 1, 'inactive', 3, 'disabled', 'inactive'),
                 ('linked', 1, 'inactive', 3, 'disabled', 'inactive'),
                 ('unexpected', 0, 'activating', 0, 'unknown', 'unknown'),
                 ('enabled', 1, 'active', 1, 'enabled', 'unknown')]
        for en, ec, ac, rc, expected_en, expected_ac in cases:
            with self.subTest(en=en, ac=ac), patch.object(systemdstatus, '_systemctl', side_effect=[
                response('loaded'), response(en, ec, '/private'), response(ac, rc, '/private')]):
                status = systemdstatus.timer_status('synthetic.timer')
                self.assertEqual((status.enabled, status.active), (expected_en, expected_ac))
                self.assertNotIn('/private', repr(status))

    def test_unusable_unit_and_failed_state_queries(self):
        with patch.object(systemdstatus, '_systemctl', return_value=response('error')):
            self.assertEqual(systemdstatus.timer_status('').diagnostic, 'unit-load-state-unavailable')
        with patch.object(systemdstatus, '_systemctl', side_effect=[response('loaded'), None, None]):
            self.assertEqual(systemdstatus.timer_status('').diagnostic, 'state-query-incomplete')

    def test_list_timers_does_not_claim_absence_when_query_fails(self):
        with patch.object(systemdstatus, '_systemctl', return_value=response('', 1, '/private')):
            result = systemdstatus.list_timers_raw()
        self.assertIn('could not be checked', result)
        self.assertNotIn('/private', result)


class TestSystemdIntegration(unittest.TestCase):
    def test_unknown_unit_on_running_user_manager(self):
        probe = systemdstatus._systemctl('show', '--property=Version', '--value')
        if probe is None or probe.returncode != 0:
            self.skipTest('integration requires a reachable systemd user manager')
        status = systemdstatus.timer_status('definitely-not-a-real-sako-brain-unit.timer')
        self.assertEqual(status.enabled, 'not-found')


class TestTimerUnitNames(unittest.TestCase):
    def test_expected_unit_names(self):
        self.assertEqual(systemdstatus.BACKUP_TIMER, 'sako-brain-backup.timer')
        self.assertEqual(systemdstatus.MAINTENANCE_TIMER, 'sako-brain-maintenance.timer')
        self.assertEqual(systemdstatus.TIMER_UNITS, (systemdstatus.BACKUP_TIMER, systemdstatus.MAINTENANCE_TIMER))
