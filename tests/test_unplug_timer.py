# SPDX-License-Identifier: MIT
"""The "time since unplugged" timer: its rules, its persistence and its text (no Qt, no hardware)."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import unplug_timer
from unplug_timer import UnplugTimer, format_duration


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TimerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'config' / 'unplug-timer.json'
        self.clock = Clock()

    def timer(self, boot='boot-1'):
        return UnplugTimer(self.path, boot_id=boot, clock=self.clock)

    def saved(self):
        return json.loads(self.path.read_text())


class FormattingTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(format_duration(0), '0:00:00')
        self.assertEqual(format_duration(59.9), '0:00:59')
        self.assertEqual(format_duration(3725), '1:02:05')
        self.assertEqual(format_duration(86399), '23:59:59')
        self.assertEqual(format_duration(86400), '1 d 0:00:00')
        self.assertEqual(format_duration(2 * 86400 + 3 * 3600 + 5 * 60), '2 d 3:05:00')

    def test_never_negative(self):
        self.assertEqual(format_duration(-5), '0:00:00')


class WatchingTheChargerTests(TimerTestCase):
    def test_an_unplugging_that_is_seen_is_timed_exactly(self):
        timer = self.timer()
        self.assertTrue(timer.update('ac'))
        self.assertEqual(timer.describe('ac'), 'No unplugging seen yet')
        self.clock.advance(120)
        self.assertTrue(timer.update('battery'))
        self.assertEqual(timer.describe('battery'), 'Unplugged 0:00:00 ago')
        self.clock.advance(3725)
        self.assertEqual(timer.describe('battery'), 'Unplugged 1:02:05 ago')           # counts live, no update needed

    def test_after_plugging_in_it_keeps_counting_from_the_last_unplugging(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.clock.advance(600)
        timer.update('ac')
        self.assertEqual(timer.describe('ac'), 'Last unplugged 0:10:00 ago')
        self.clock.advance(3600)
        self.assertEqual(timer.describe('ac'), 'Last unplugged 1:10:00 ago')

    def test_each_new_unplugging_starts_the_timer_again(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.clock.advance(1000)
        timer.update('ac')
        self.clock.advance(500)
        timer.update('battery')
        self.assertEqual(timer.describe('battery'), 'Unplugged 0:00:00 ago')
        self.clock.advance(61)
        self.assertEqual(timer.describe('battery'), 'Unplugged 0:01:01 ago')

    def test_repeated_readings_change_nothing(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        before = dict(timer.record)
        self.clock.advance(5)
        self.assertFalse(timer.update('battery'))
        self.assertEqual(timer.record['unplugged_at'], before['unplugged_at'])
        self.assertTrue(timer.record['exact'])

    def test_a_missing_power_reading_is_ignored(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.assertFalse(timer.update(None))
        self.assertEqual(timer.record['source'], 'battery')
        self.assertEqual(timer.describe(None), 'Unplugged: power source not detected')

    def test_starting_on_battery_says_at_least(self):
        timer = self.timer()
        timer.update('battery')
        self.assertFalse(timer.record['exact'])
        self.clock.advance(90)
        self.assertEqual(timer.describe('battery'), 'Unplugged at least 0:01:30 ago')
        timer.update('ac')
        self.assertEqual(timer.describe('ac'), 'Last unplugged at least 0:01:30 ago')

    def test_a_later_unplugging_that_is_seen_is_exact_again(self):
        timer = self.timer()
        timer.update('battery')
        timer.update('ac')
        timer.update('battery')
        self.assertTrue(timer.record['exact'])
        self.assertEqual(timer.describe('battery'), 'Unplugged 0:00:00 ago')


class RestartTests(TimerTestCase):
    def test_a_quick_restart_does_not_reset_the_timer(self):
        first = self.timer()
        first.update('ac')
        self.clock.advance(30)
        first.update('battery')
        for _ in range(30):                         # the app runs for half an hour; its readings keep the record fresh
            self.clock.advance(60)
            first.update('battery')
        self.clock.advance(5)
        second = self.timer()                       # for example the app was restarted by an upgrade
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'Unplugged 0:30:05 ago')
        self.assertTrue(second.record['exact'])

    def test_unplugging_while_the_app_restarted_is_a_lower_bound(self):
        first = self.timer()
        first.update('ac')
        self.clock.advance(60)
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'Unplugged at least 0:00:00 ago')

    def test_plugging_in_while_the_app_restarted_keeps_the_last_unplugging(self):
        first = self.timer()
        first.update('ac')
        first.update('battery')
        self.clock.advance(300)
        second = self.timer()
        second.update('ac')
        self.assertTrue(second.record['exact'])
        self.assertEqual(second.describe('ac'), 'Last unplugged 0:05:00 ago')

    def test_a_long_gap_starts_again(self):
        first = self.timer()
        first.update('ac')
        first.update('battery')
        self.clock.advance(unplug_timer.TRUST_SECONDS + 1)
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'Unplugged at least 0:00:00 ago')

    def test_a_long_gap_on_the_charger_forgets_the_old_unplugging(self):
        first = self.timer()
        first.update('ac')
        first.update('battery')
        first.update('ac')
        self.clock.advance(unplug_timer.TRUST_SECONDS + 1)
        second = self.timer()
        second.update('ac')
        self.assertEqual(second.describe('ac'), 'No unplugging seen yet')

    def test_another_boot_starts_again(self):
        first = self.timer(boot='boot-1')
        first.update('ac')
        first.update('battery')
        self.clock.advance(20)
        second = self.timer(boot='boot-2')
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'Unplugged at least 0:00:00 ago')

    def test_an_unknown_boot_is_never_trusted(self):
        first = self.timer(boot='')
        first.update('ac')
        first.update('battery')
        self.clock.advance(20)
        second = self.timer(boot='')
        second.update('battery')
        self.assertFalse(second.record['exact'])

    def test_a_clock_that_went_backwards_is_not_trusted(self):
        first = self.timer()
        first.update('ac')
        first.update('battery')
        self.clock.advance(-3600)
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'Unplugged at least 0:00:00 ago')

    def test_the_text_never_shows_negative_time(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.clock.advance(-100)
        self.assertEqual(timer.describe('battery'), 'Unplugged 0:00:00 ago')


class StorageTests(TimerTestCase):
    def test_changes_are_written_at_once_and_privately(self):
        timer = self.timer()
        timer.update('ac')
        self.assertEqual(self.saved()['source'], 'ac')
        self.clock.advance(10)
        timer.update('battery')
        data = self.saved()
        self.assertEqual((data['source'], data['exact'], data['unplugged_at']), ('battery', True, self.clock.now))
        self.assertEqual(data['version'], 1)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['unplug-timer.json'])   # no temp files left

    def test_the_last_seen_time_is_a_slow_heartbeat(self):
        timer = self.timer()
        timer.update('battery')
        written = self.saved()['seen']
        for _ in range(10):
            self.clock.advance(5)
            timer.update('battery')
        self.assertEqual(self.saved()['seen'], written)                       # 50 s: nothing written
        self.clock.advance(unplug_timer.HEARTBEAT_SECONDS)
        timer.update('battery')
        self.assertEqual(self.saved()['seen'], self.clock.now)

    def test_garbage_in_the_file_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        bad = ['not json', '[]', '{}', json.dumps({'version': 2, 'seen': 1}),
               json.dumps({'version': 1, 'seen': 'x', 'source': 'ac'}),
               json.dumps({'version': 1, 'seen': 1e300, 'source': 'ac'}),
               json.dumps({'version': 1, 'seen': self.clock.now, 'source': 'ac', 'unplugged_at': 'soon'}),
               json.dumps({'version': 1, 'seen': True, 'source': 'ac'}),
               '{"version": 1, "seen": NaN, "source": "ac"}']
        for text in bad:
            self.path.write_text(text)
            timer = self.timer()
            self.assertIsNone(timer.record, text)
            timer.update('battery')
            self.assertFalse(timer.record['exact'], text)

    def test_unknown_fields_and_sources_are_cleaned(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'version': 1, 'seen': self.clock.now, 'source': 'solar', 'exact': 'yes',
                                         'unplugged_at': None, 'boot_id': 7, 'extra': 'ignored'}))
        record = self.timer().record
        self.assertEqual(record, {'version': 1, 'unplugged_at': None, 'exact': False, 'source': None,
                                  'boot_id': '', 'seen': self.clock.now})

    def test_a_folder_that_cannot_be_written_does_not_break_the_timer(self):
        blocker = Path(self.directory.name) / 'blocker'
        blocker.write_text('a file where the config folder should be')
        timer = UnplugTimer(blocker / 'config' / 'unplug-timer.json', boot_id='b', clock=self.clock)
        timer.update('ac')
        self.clock.advance(5)
        timer.update('battery')
        self.assertIsNotNone(timer.write_error)
        self.assertEqual(timer.describe('battery'), 'Unplugged 0:00:00 ago')              # still works in memory

    @unittest.skipIf(os.geteuid() == 0, 'root can write anywhere')
    def test_a_read_only_folder_does_not_break_the_timer(self):
        folder = self.path.parent
        folder.mkdir(parents=True)
        folder.chmod(0o500)
        self.addCleanup(folder.chmod, 0o700)
        timer = self.timer()
        timer.update('battery')
        self.assertIsNotNone(timer.write_error)
        self.assertEqual(timer.describe('battery'), 'Unplugged at least 0:00:00 ago')

    def test_boot_id_is_read_from_the_kernel(self):
        path = Path(self.directory.name) / 'boot_id'
        path.write_text('abc-123\n')
        self.assertEqual(unplug_timer.current_boot_id(path), 'abc-123')
        self.assertEqual(unplug_timer.current_boot_id(Path(self.directory.name) / 'missing'), '')


if __name__ == '__main__':
    unittest.main()
