# SPDX-License-Identifier: MIT
"""The battery run-time timer: its rules, its persistence and its text (no Qt, no hardware)."""
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


class World:
    """The clocks and the battery of a fake laptop."""

    def __init__(self):
        self.wall = 1_000_000.0          # wall-clock time: also runs while the laptop is asleep or off
        self.mono = 5000.0               # seconds since boot without sleep (CLOCK_MONOTONIC)
        self.boots = 1
        self.level = None                # the battery level the fake gauge reports (None: unreadable)

    @property
    def boot(self):
        return f'boot-{self.boots}'

    def awake(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def asleep(self, seconds):
        self.wall += seconds             # the monotonic clock does not advance during sleep

    def reboot(self, off=30, booting=60):
        """Shut down, stay off for *off* seconds, boot and run for *booting* seconds until the app starts."""
        self.wall += off + booting
        self.mono = float(booting)
        self.boots += 1


class TimerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'config' / 'unplug-timer.json'
        self.world = World()

    def timer(self, boot=None):
        world = self.world
        return UnplugTimer(self.path, clock=lambda: world.wall, mono=lambda: world.mono,
                           capacity=lambda: world.level, boot_id=world.boot if boot is None else boot)

    def saved(self):
        return json.loads(self.path.read_text())

    def unplugged_for(self, minutes, level=80, timer=None):
        """A laptop that was on the charger, is unplugged and runs on battery for *minutes*, battery 0.5 point a minute."""
        timer = timer or self.timer()
        self.world.level = level
        timer.update('ac')
        self.world.awake(30)
        timer.update('battery')
        self.run_for(timer, minutes)
        return timer

    def run_for(self, timer, minutes):
        for _ in range(minutes):
            self.world.awake(60)
            if self.world.level is not None:
                self.world.level -= 0.5
            timer.update('battery')


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


class CountingTests(TimerTestCase):
    def test_the_time_on_battery_is_counted_exactly(self):
        timer = self.timer()
        self.assertTrue(timer.update('ac'))
        self.assertEqual(timer.describe('ac'), 'No unplugging seen yet')
        self.world.awake(120)
        self.assertTrue(timer.update('battery'))
        self.assertEqual(timer.describe('battery'), 'On battery for 0:00:00')
        self.world.awake(3725)
        self.assertEqual(timer.describe('battery'), 'On battery for 1:02:05')          # counts live, no update needed

    def test_on_the_charger_it_stops_and_shows_the_last_time_on_battery(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.world.awake(600)
        timer.update('ac')
        self.assertEqual(timer.describe('ac'), 'Last time on battery: 0:10:00')
        self.world.awake(3600)
        timer.update('ac')
        self.assertEqual(timer.describe('ac'), 'Last time on battery: 0:10:00')        # it does not keep counting

    def test_each_new_unplugging_starts_again_at_zero(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.world.awake(1000)
        timer.update('ac')
        self.world.awake(500)
        timer.update('battery')
        self.assertEqual(timer.describe('battery'), 'On battery for 0:00:00')
        self.world.awake(61)
        self.assertEqual(timer.describe('battery'), 'On battery for 0:01:01')

    def test_the_time_between_readings_belongs_to_the_previous_source(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.world.awake(100)
        timer.update('ac')                           # the 100 seconds before this reading were on battery
        self.assertEqual(timer.record['elapsed'], 100.0)
        self.world.awake(50)
        timer.update('battery')                      # the 50 seconds before this one were on the charger
        self.assertEqual(timer.record['elapsed'], 0.0)
        self.world.awake(10)
        timer.update('ac')
        self.assertEqual(timer.describe('ac'), 'Last time on battery: 0:00:10')

    def test_many_readings_add_up(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        for _ in range(100):
            self.world.awake(2)
            self.assertFalse(timer.update('battery'))
        self.assertEqual(timer.describe('battery'), 'On battery for 0:03:20')
        self.assertTrue(timer.record['exact'])

    def test_a_missing_power_reading_is_ignored(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.assertFalse(timer.update(None))
        self.assertEqual(timer.record['source'], 'battery')
        self.assertEqual(timer.describe(None), 'On battery: power source not detected')

    def test_starting_on_battery_says_at_least(self):
        timer = self.timer()
        timer.update('battery')
        self.assertFalse(timer.record['exact'])
        self.world.awake(90)
        self.assertEqual(timer.describe('battery'), 'On battery for at least 0:01:30')
        timer.update('ac')
        self.assertEqual(timer.describe('ac'), 'Last time on battery: at least 0:01:30')

    def test_a_later_unplugging_that_is_seen_is_exact_again(self):
        timer = self.timer()
        timer.update('battery')
        timer.update('ac')
        timer.update('battery')
        self.assertTrue(timer.record['exact'])
        self.assertEqual(timer.describe('battery'), 'On battery for 0:00:00')

    def test_sleep_is_not_counted(self):
        timer = self.timer()
        timer.update('ac')
        timer.update('battery')
        self.world.awake(600)
        timer.update('battery')
        self.world.asleep(3600)                      # the lid was closed for an hour
        self.assertEqual(timer.describe('battery'), 'On battery for 0:10:00')           # even before the next reading
        self.world.awake(60)
        timer.update('battery')
        self.assertEqual(timer.describe('battery'), 'On battery for 0:11:00')


class RestartTests(TimerTestCase):
    """The app is closed and started again while the laptop keeps running (for example an upgrade)."""

    def test_the_time_while_the_app_was_closed_counts_because_the_laptop_was_running(self):
        self.unplugged_for(30)
        self.world.awake(5)
        second = self.timer()
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'On battery for 0:30:05')
        self.assertTrue(second.record['exact'])

    def test_sleep_while_the_app_was_closed_does_not_count(self):
        self.unplugged_for(30)
        self.world.asleep(2 * 3600)
        self.world.awake(5)
        second = self.timer()
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'On battery for 0:30:05')

    def test_unplugging_while_the_app_was_not_running_starts_at_zero_and_says_at_least(self):
        first = self.timer()
        first.update('ac')
        self.world.awake(60)
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'On battery for at least 0:00:00')

    def test_plugging_in_while_the_app_was_not_running_keeps_the_total(self):
        first = self.timer()
        first.update('ac')
        first.update('battery')
        self.world.awake(300)
        first.update('battery')
        self.world.awake(40)
        second = self.timer()
        second.update('ac')
        self.assertTrue(second.record['exact'])
        self.assertEqual(second.describe('ac'), 'Last time on battery: 0:05:00')

    def test_closing_the_app_writes_the_latest_time(self):
        timer = self.unplugged_for(10)
        self.world.awake(25)                         # nothing wrote the record in these 25 seconds
        self.assertEqual(self.saved()['elapsed'], 600.0)
        timer.flush()
        data = self.saved()
        self.assertEqual(data['elapsed'], 625.0)
        self.assertEqual(data['uptime'], self.world.mono)
        self.assertEqual(data['seen'], self.world.wall)

    def test_closing_before_anything_was_seen_writes_nothing(self):
        self.timer().flush()
        self.assertFalse(self.path.exists())

    def test_a_reboot_after_a_flush_loses_nothing(self):
        timer = self.unplugged_for(10)
        self.world.awake(25)
        timer.flush()                                # the session manager closes the app at shutdown
        self.world.reboot(off=30, booting=60)
        second = self.timer()
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'On battery for 0:11:25')           # 10 min + 25 s + the boot


class RebootTests(TimerTestCase):
    """The laptop is rebooted or powered off. Reported: after 2.9.1 the time while the laptop was off was counted too."""

    def test_the_time_the_laptop_was_off_is_not_counted(self):
        self.unplugged_for(20)
        self.world.reboot(off=30, booting=60)        # off for 30 seconds, then booting and logging in for 60
        second = self.timer()
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'On battery for 0:21:00')           # 20 minutes + the boot, not the 30 s off
        self.assertTrue(second.record['exact'])                                          # it was seen happening

    def test_it_keeps_counting_after_the_reboot(self):
        self.unplugged_for(20)
        self.world.reboot()
        second = self.timer()
        second.update('battery')
        self.run_for(second, 10)
        self.assertEqual(second.describe('battery'), 'On battery for 0:31:00')

    def test_a_power_off_for_hours_does_not_count_at_all(self):
        self.unplugged_for(20, level=60)
        self.world.reboot(off=3 * 3600, booting=60)  # off for three hours; the battery only went down
        self.world.level = 49
        second = self.timer()
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'On battery for 0:21:00')

    def test_two_reboots_in_a_row(self):
        self.unplugged_for(20)
        self.world.reboot(off=10, booting=40)
        second = self.timer()
        second.update('battery')
        self.run_for(second, 5)
        self.world.reboot(off=600, booting=50)
        third = self.timer()
        third.update('battery')
        self.assertEqual(third.describe('battery'), 'On battery for 0:26:30')            # 20 min + 40 s + 5 min + 50 s

    def test_charging_while_the_laptop_was_off_starts_again(self):
        self.unplugged_for(20, level=40)
        self.world.reboot(off=2 * 3600)              # plugged in, charged, unplugged again, booted
        self.world.level = 100
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'On battery for at least 0:00:00')

    def test_a_small_rise_of_the_battery_level_is_not_charging(self):
        first = self.unplugged_for(20, level=50)
        self.world.reboot()
        self.world.level = first.record['capacity'] + unplug_timer.CHARGE_TOLERANCE_PERCENT      # a gauge reads a bit higher after a rest
        second = self.timer()
        second.update('battery')
        self.assertTrue(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'On battery for 0:21:00')

    def test_a_rise_beyond_the_tolerance_is_charging(self):
        first = self.unplugged_for(20, level=50)
        self.world.reboot()
        self.world.level = first.record['capacity'] + unplug_timer.CHARGE_TOLERANCE_PERCENT + 0.5
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.record['elapsed'], 0.0)

    def test_an_unreadable_battery_level_does_not_break_the_continuity(self):
        self.unplugged_for(20)
        self.world.reboot()
        self.world.level = None
        second = self.timer()
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'On battery for 0:21:00')
        third = UnplugTimer(self.path, clock=lambda: self.world.wall, mono=lambda: self.world.mono, boot_id=self.world.boot,
                            capacity=lambda: (_ for _ in ()).throw(OSError('no battery')))
        self.world.awake(60)
        third.update('battery')                      # a gauge that raises is the same as one that is unreadable
        self.assertEqual(third.describe('battery'), 'On battery for 0:22:00')

    def test_it_was_on_the_charger_when_the_laptop_went_off_and_unplugged_before_the_boot(self):
        first = self.timer()
        self.world.level = 100
        first.update('ac')
        self.world.reboot(off=3600)
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'On battery for at least 0:00:00')

    def test_it_was_plugged_in_before_the_reboot_and_the_last_total_stays(self):
        first = self.unplugged_for(20)
        self.world.awake(30)
        first.update('ac')
        self.world.reboot(off=1800)
        second = self.timer()
        second.update('ac')
        self.assertEqual(second.describe('ac'), 'Last time on battery: 0:20:30')         # the boot on the charger adds nothing
        self.assertTrue(second.record['exact'])

    def test_it_stays_on_the_charger_across_two_reboots(self):
        first = self.unplugged_for(20)
        first.update('ac')
        self.world.reboot()
        second = self.timer()
        second.update('ac')
        self.world.reboot()
        third = self.timer()
        third.update('ac')
        self.assertEqual(third.describe('ac'), 'Last time on battery: 0:20:00')

    def test_a_record_older_than_a_week_is_not_believed(self):
        self.unplugged_for(20)
        self.world.reboot(off=unplug_timer.MAX_GAP_SECONDS + 1)
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'On battery for at least 0:00:00')

    def test_just_under_a_week_is_still_believed(self):
        self.unplugged_for(20)
        self.world.reboot(off=unplug_timer.MAX_GAP_SECONDS - 100)
        second = self.timer()
        second.update('battery')
        self.assertTrue(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'On battery for 0:21:00')           # a week off adds nothing

    def test_a_clock_that_went_backwards_is_not_believed(self):
        self.unplugged_for(20)
        self.world.reboot()
        self.world.wall -= 7200                      # for example a clock that is wrong until it is synchronised
        second = self.timer()
        second.update('battery')
        self.assertFalse(second.record['exact'])
        self.assertEqual(second.describe('battery'), 'On battery for at least 0:00:00')

    def test_an_unknown_boot_adds_nothing_but_keeps_the_total(self):
        self.unplugged_for(20)
        self.world.reboot()
        second = self.timer(boot='')                 # /proc/sys/kernel/random/boot_id could not be read
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'On battery for 0:20:00')
        self.assertTrue(second.record['exact'])

    def test_a_monotonic_clock_that_went_backwards_on_the_same_boot_adds_nothing(self):
        self.unplugged_for(20)
        self.world.mono -= 1000                      # cannot happen; must not give a negative addition
        second = self.timer()
        second.update('battery')
        self.assertEqual(second.describe('battery'), 'On battery for 0:20:00')


class OldRecordTests(TimerTestCase):
    def write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))

    def test_a_record_of_2_10_0_and_earlier_starts_the_total_again(self):
        # They held the wall-clock time of the unplugging, which also counted the time the laptop was off.
        self.write({'version': 1, 'unplugged_at': self.world.wall - 5 * 3600, 'exact': True, 'source': 'battery',
                    'seen': self.world.wall - 60, 'capacity': 60})
        self.world.level = 58
        timer = self.timer()
        timer.update('battery')
        self.assertEqual(timer.describe('battery'), 'On battery for at least 0:00:00')
        self.world.awake(120)
        self.assertEqual(timer.describe('battery'), 'On battery for at least 0:02:00')
        data = self.saved()
        self.assertEqual((data['version'], data['elapsed'], data['boot_id']), (2, 0.0, self.world.boot))
        self.assertNotIn('unplugged_at', data)

    def test_an_old_record_of_a_laptop_on_the_charger_has_nothing_to_report(self):
        self.write({'version': 1, 'unplugged_at': None, 'exact': False, 'source': 'ac', 'seen': self.world.wall - 60})
        timer = self.timer()
        timer.update('ac')
        self.assertEqual(timer.describe('ac'), 'No unplugging seen yet')

    def test_an_old_record_of_a_laptop_that_was_charged_starts_again_too(self):
        self.write({'version': 1, 'unplugged_at': self.world.wall - 3600, 'exact': True, 'source': 'battery',
                    'seen': self.world.wall - 60, 'capacity': 30})
        self.world.level = 100
        timer = self.timer()
        timer.update('battery')
        self.assertFalse(timer.record['exact'])

    def test_an_invalid_old_record_is_ignored(self):
        self.write({'version': 1, 'unplugged_at': 'soon', 'source': 'battery', 'seen': self.world.wall})
        self.assertIsNone(self.timer().record)


class StorageTests(TimerTestCase):
    def test_changes_are_written_at_once_and_privately(self):
        timer = self.timer()
        timer.update('ac')
        self.assertEqual(self.saved()['source'], 'ac')
        self.world.awake(10)
        timer.update('battery')
        data = self.saved()
        self.assertEqual((data['source'], data['exact'], data['elapsed']), ('battery', True, 0.0))
        self.assertEqual((data['version'], data['boot_id'], data['uptime'], data['seen']),
                         (2, 'boot-1', self.world.mono, self.world.wall))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['unplug-timer.json'])   # no temp files left

    def test_the_record_is_a_slow_heartbeat(self):
        timer = self.timer()
        timer.update('battery')
        written = self.saved()['seen']
        for _ in range(10):
            self.world.awake(5)
            timer.update('battery')
        self.assertEqual(self.saved()['seen'], written)                       # 50 s: nothing written
        self.world.awake(unplug_timer.HEARTBEAT_SECONDS)
        timer.update('battery')
        data = self.saved()
        self.assertEqual(data['seen'], self.world.wall)
        self.assertEqual(data['elapsed'], 110.0)                              # the time up to this write

    def test_the_battery_level_is_saved_with_the_record(self):
        timer = self.timer()
        self.world.level = 73
        timer.update('ac')
        self.assertEqual(self.saved()['capacity'], 73)
        self.world.awake(61)
        self.world.level = 72
        timer.update('ac')
        self.assertEqual(self.saved()['capacity'], 72)                        # refreshed with each heartbeat
        self.world.level = None
        self.world.awake(61)
        timer.update('ac')
        self.assertIsNone(self.saved()['capacity'])

    def test_garbage_in_the_file_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        base = {'version': 2, 'elapsed': 5.0, 'exact': True, 'source': 'battery', 'seen': self.world.wall, 'uptime': 5.0, 'boot_id': 'b'}
        bad = ['not json', '[]', '{}', json.dumps({'version': 3, 'seen': 1}),
               json.dumps(dict(base, seen='x')), json.dumps(dict(base, seen=1e300)), json.dumps(dict(base, seen=True)),
               json.dumps(dict(base, elapsed='long')), json.dumps(dict(base, elapsed=-1)), json.dumps(dict(base, elapsed=1e300)),
               '{"version": 2, "seen": NaN, "source": "ac"}']
        for text in bad:
            self.path.write_text(text)
            timer = self.timer()
            self.assertIsNone(timer.record, text)
            timer.update('battery')
            self.assertFalse(timer.record['exact'], text)

    def test_unknown_fields_and_sources_are_cleaned(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({'version': 2, 'seen': self.world.wall, 'source': 'solar', 'exact': 'yes', 'elapsed': None,
                                         'boot_id': 7, 'uptime': -4, 'capacity': 150, 'extra': 'ignored'}))
        record = self.timer().record
        self.assertEqual(record, {'version': 2, 'elapsed': None, 'exact': False, 'source': None, 'seen': self.world.wall,
                                  'uptime': None, 'boot_id': None, 'capacity': None})

    def test_an_impossible_battery_level_is_dropped(self):
        self.path.parent.mkdir(parents=True)
        for bad in (150, -5, 'full', True, float('nan')):
            self.path.write_text(json.dumps({'version': 2, 'seen': self.world.wall, 'source': 'ac', 'elapsed': None, 'capacity': bad}))
            self.assertIsNone(self.timer().record['capacity'], bad)

    def test_a_folder_that_cannot_be_written_does_not_break_the_timer(self):
        blocker = Path(self.directory.name) / 'blocker'
        blocker.write_text('a file where the config folder should be')
        world = self.world
        timer = UnplugTimer(blocker / 'config' / 'unplug-timer.json', clock=lambda: world.wall, mono=lambda: world.mono, boot_id='b')
        timer.update('ac')
        world.awake(5)
        timer.update('battery')
        self.assertIsNotNone(timer.write_error)
        world.awake(7)
        self.assertEqual(timer.describe('battery'), 'On battery for 0:00:07')             # still works in memory

    @unittest.skipIf(os.geteuid() == 0, 'root can write anywhere')
    def test_a_read_only_folder_does_not_break_the_timer(self):
        folder = self.path.parent
        folder.mkdir(parents=True)
        folder.chmod(0o500)
        self.addCleanup(folder.chmod, 0o700)
        timer = self.timer()
        timer.update('battery')
        self.assertIsNotNone(timer.write_error)
        self.assertEqual(timer.describe('battery'), 'On battery for at least 0:00:00')

    def test_boot_id_is_read_from_the_kernel(self):
        path = Path(self.directory.name) / 'boot_id'
        path.write_text('abc-123\n')
        self.assertEqual(unplug_timer.current_boot_id(path), 'abc-123')
        self.assertEqual(unplug_timer.current_boot_id(Path(self.directory.name) / 'missing'), '')


if __name__ == '__main__':
    unittest.main()
