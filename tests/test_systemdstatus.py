import shutil
import unittest

from brain import systemdstatus

SYSTEMCTL_AVAILABLE = shutil.which("systemctl") is not None


@unittest.skipUnless(SYSTEMCTL_AVAILABLE, "systemctl not available")
class TestSystemdStatus(unittest.TestCase):
    def test_timer_status_reports_not_found_for_unknown_unit(self):
        status = systemdstatus.timer_status("definitely-not-a-real-sako-brain-unit.timer")
        self.assertEqual(status.enabled, "not-found")

    def test_timer_status_never_raises(self):
        # Should degrade gracefully even for nonsense input, never throw.
        status = systemdstatus.timer_status("")
        self.assertIsNotNone(status)

    def test_list_timers_raw_does_not_raise_when_none_installed(self):
        # Only assert it returns *something* — installed state depends on
        # the real machine's current systemd --user configuration, which
        # this test must not assume either way.
        result = systemdstatus.list_timers_raw()
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)


class TestTimerUnitNames(unittest.TestCase):
    def test_expected_unit_names(self):
        self.assertEqual(systemdstatus.BACKUP_TIMER, "sako-brain-backup.timer")
        self.assertEqual(systemdstatus.MAINTENANCE_TIMER, "sako-brain-maintenance.timer")
        self.assertEqual(systemdstatus.TIMER_UNITS, (systemdstatus.BACKUP_TIMER, systemdstatus.MAINTENANCE_TIMER))


if __name__ == "__main__":
    unittest.main()
