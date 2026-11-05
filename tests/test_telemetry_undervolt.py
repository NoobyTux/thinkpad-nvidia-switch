# SPDX-License-Identifier: MIT
"""Hardware-independent coverage for power estimates, voltage validation and migration."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from battery_telemetry import battery_snapshot, battery_label_text
from undervolt_control import read_offsets, updated_config, validate_mv, apply_voltages
from profiles import new_profile, read_store, write_store, empty_store, validate_settings


class BatteryTelemetryTests(unittest.TestCase):
    def create(self, base, *, status, power, energy, full):
        battery = base / 'BAT0'
        battery.mkdir()
        for key, value in [('type', 'Battery'), ('status', status), ('capacity', 60),
                           ('power_now', power), ('energy_now', energy), ('energy_full', full)]:
            (battery / key).write_text(str(value))
        return battery

    def test_discharge_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create(root, status='Discharging', power=20000000, energy=40000000, full=60000000)
            data = battery_snapshot(root)
            self.assertEqual(data['discharge_w'], 20)
            self.assertEqual(data['remaining'], '2 h 00 min')
            self.assertIsNone(data['charge_w'])
            self.assertIn('2 h 00 min left', battery_label_text(data))

    def test_charge_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create(root, status='Charging', power=10000000, energy=20000000, full=50000000)
            data = battery_snapshot(root)
            self.assertEqual(data['charge_w'], 10)
            self.assertEqual(data['to_full'], '3 h 00 min')
            self.assertIsNone(data['discharge_w'])
            self.assertIn('10.00 W into battery', battery_label_text(data))

    def test_current_voltage_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = self.create(root, status='Discharging', power=1, energy=40000000, full=60000000)
            (battery / 'power_now').unlink()
            (battery / 'current_now').write_text('1000000')
            (battery / 'voltage_now').write_text('12000000')
            self.assertEqual(battery_snapshot(root)['discharge_w'], 12)

    def test_unknown_time_never_invents_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.create(root, status='Discharging', power=0, energy=0, full=0)
            self.assertIsNone(battery_snapshot(root)['remaining'])
            self.assertIn('unavailable', battery_label_text(battery_snapshot(root)))

    def test_absent_battery_and_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(battery_snapshot(root)['capacity'])
            self.create(root, status='Full', power=0, energy=50000000, full=50000000)
            self.assertEqual(battery_snapshot(root)['to_full'], 'Fully charged')


class UndervoltTests(unittest.TestCase):
    def setUp(self):
        # The helper only edits a root-owned file; these tests build their own files as whoever runs them
        # (an ordinary user on CI), so that user counts as trusted here. The next class proves the real rule.
        patcher = patch('undervolt_control.TRUSTED_UID', os.getuid())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parser_recognizes_offsets(self):
        data = read_offsets('CPU (0): -30.27 mV\nGPU (1): -20.51 mV\nCPU Cache (2): -30.27 mV\n')
        self.assertEqual(data, {'cpu': -30.27, 'cache': -30.27, 'gpu': -20.51})

    def test_parser_rejects_partial_hardware(self):
        with self.assertRaises(ValueError):
            read_offsets('CPU (0): -30.00 mV')

    def test_voltage_bounds_and_injection(self):
        self.assertEqual(validate_mv((-30.27, -30.27, 0)), (-30.27, -30.27, 0))
        self.assertEqual(validate_mv((-200, -150.5, 0)), (-200, -150.5, 0))   # the floor itself is allowed
        for invalid in ((-200.01, 0, 0), (-201, 0, 0), (0, -999, 0), (1, 0, 0), ('-30', 0, 0), (True, 0, 0)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_mv(invalid)

    def test_preserve_config_non_voltage_and_comments(self):
        original = 'enable no\n# power package 40 30\nundervolt 0 "CPU" -30.27\nundervolt 3 "System Agent" 0\ninterval 5000\n'
        updated = updated_config(original, -40, -40, -10)
        self.assertIn('undervolt 0 "CPU" -40', updated)
        self.assertIn('undervolt 1 "GPU" -10', updated)
        self.assertIn('undervolt 2 "CPU Cache" -40', updated)
        self.assertIn('undervolt 3 "System Agent" 0', updated)
        self.assertIn('interval 5000', updated)

    def test_rejects_unintended_power_limit_writes(self):
        with self.assertRaisesRegex(ValueError, 'power/thermal'):
            updated_config('power package 45 35\n', -10, -10, 0)

    def test_reject_duplicate_voltage_entries(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            updated_config('undervolt 0 "CPU" 0\nundervolt 0 "CPU" -5\n', -10, -10, 0)

    def test_apply_and_rollback_on_failed_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / 'intel-undervolt.conf'
            state = root / 'saved.json'
            conf.write_text('undervolt 0 "CPU" -5\nundervolt 1 "GPU" 0\nundervolt 2 "CPU Cache" -5\n')
            original = conf.read_text()
            good = 'CPU (0): -20.00 mV\nGPU (1): 0.00 mV\nCPU Cache (2): -20.00 mV'
            with patch('undervolt_control.safe_binary', return_value='/usr/bin/intel-undervolt'), \
                    patch('undervolt_control.run_undervolt', side_effect=['ok', good]):
                self.assertEqual(apply_voltages(-20, -20, 0, conf, state)['cpu'], -20)
            self.assertTrue(state.exists())
            with patch('undervolt_control.safe_binary', return_value='/usr/bin/intel-undervolt'), \
                    patch('undervolt_control.run_undervolt', side_effect=['ok', 'CPU (0): 0.00 mV\nGPU (1): 0.00 mV\nCPU Cache (2): 0.00 mV', 'ok']):
                before = conf.read_text()
                with self.assertRaisesRegex(RuntimeError, 'not accepted'):
                    apply_voltages(-30, -30, 0, conf, state)
                self.assertEqual(conf.read_text(), before)
            self.assertIn(original, json.loads(state.read_text())['original'])


class ConfigOwnershipTests(unittest.TestCase):
    """The root helper edits /etc/intel-undervolt.conf only when root owns it and nobody else can write it.

    These tests must give the same answer whether they run as root or as an ordinary user (CI runs as a user).
    """

    def make_conf(self, mode=0o644):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        conf = Path(directory.name) / 'intel-undervolt.conf'
        conf.write_text('undervolt 0 "CPU" 0\n')
        conf.chmod(mode)
        return conf

    def test_a_file_owned_by_anyone_but_root_is_refused(self):
        from undervolt_control import check_conf, TRUSTED_UID
        self.assertEqual(TRUSTED_UID, 0)                      # production trusts root only
        conf = self.make_conf()
        if os.getuid() == 0:
            os.chown(conf, 12345, 12345)                      # a root test run: hand the file to somebody else
        with self.assertRaisesRegex(RuntimeError, 'must be root-owned'):
            check_conf(conf)

    def test_a_file_owned_by_the_trusted_user_is_read(self):
        from undervolt_control import check_conf
        conf = self.make_conf()
        with patch('undervolt_control.TRUSTED_UID', os.getuid()):
            self.assertIn('undervolt 0', check_conf(conf))

    def test_group_or_world_writable_files_are_refused_even_for_the_trusted_owner(self):
        from undervolt_control import check_conf
        for mode in (0o664, 0o646, 0o666):
            with self.subTest(mode=oct(mode)):
                conf = self.make_conf(mode)
                with patch('undervolt_control.TRUSTED_UID', os.getuid()):
                    with self.assertRaisesRegex(RuntimeError, 'not group/world writable'):
                        check_conf(conf)


class ProfileMigrationTests(unittest.TestCase):
    def test_v1_migrates_battery_fields_without_deleting_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = empty_store()
            s = {'gpu_state': 'D3cold', 'basic_guard': True, 'aggressive_guard': False,
                 'auto_gpu_sleep': False, 'disabled_cores': 2, 'frequency_mhz': 1900,
                 'pl1_w': 15.0, 'pl2_w': 20.0,
                 'history_window': 'Last 30 minutes', 'history_interval': '2 seconds'}
            p = new_profile('Battery test', {**{k: v for k, v in s.items() if not k.startswith('history_')},
                                            'undervolt_cpu_mv': None, 'undervolt_cache_mv': None, 'undervolt_gpu_mv': None})
            p['settings'] = s
            store['profiles'] = [p]
            store['auto_enabled'] = True
            store['battery_profile'] = p['id']
            store['version'] = 1
            path = Path(directory) / 'profiles.json'
            path.write_text(json.dumps(store))
            loaded = read_store(path)
            self.assertEqual(loaded['version'], 2)
            self.assertEqual(loaded['battery_profile'], p['id'])
            self.assertEqual(loaded['profiles'][0]['settings']['frequency_mhz'], 1900)
            self.assertNotIn('history_window', loaded['profiles'][0]['settings'])
            self.assertIsNone(loaded['profiles'][0]['settings']['undervolt_cpu_mv'])
            write_store(path, loaded)
            self.assertEqual(json.loads(path.read_text())['version'], 2)

    def test_partial_undervolt_profile_rejected(self):
        s = {'gpu_state': 'keep', 'basic_guard': False, 'aggressive_guard': False,
             'auto_gpu_sleep': False, 'disabled_cores': None, 'frequency_mhz': None,
             'pl1_w': None, 'pl2_w': None, 'undervolt_cpu_mv': -25,
             'undervolt_cache_mv': None, 'undervolt_gpu_mv': None}
        with self.assertRaisesRegex(ValueError, 'all three'):
            validate_settings(s)

    def test_previous_remember_option_saved_and_disabled_in_auto_mode(self):
        tray = (Path(__file__).resolve().parents[1] / 'src/optimus_tray.py').read_text()
        self.assertIn("self.preferences['restore_before_auto'] = self.preferences['restore_state']", tray)
        self.assertIn("self.restore_action.setEnabled(not auto)", tray)
        self.assertIn("self.preferences['restore_state'] = False", tray)
        self.assertIn('self.set_restore_state(True)', tray)


# Additional regression coverage for unattended profile changes and cleanup.
class UndervoltRecoveryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('undervolt_control.TRUSTED_UID', os.getuid())      # see UndervoltTests.setUp
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preserve_manual_changes_when_uninstalling(self):
        from undervolt_control import uninstall_restore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / 'intel-undervolt.conf'
            state = root / 'saved.json'
            conf.write_text('undervolt 0 "CPU" -45\n')
            state.write_text(json.dumps({'original': 'undervolt 0 "CPU" 0\n', 'last_hash': 'other-hash'}))
            self.assertIn('preserving', uninstall_restore(conf, state))
            self.assertIn('-45', conf.read_text())

    def test_restores_original_only_if_owned_content_unchanged(self):
        from undervolt_control import uninstall_restore
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conf = root / 'intel-undervolt.conf'
            state = root / 'saved.json'
            last = 'undervolt 0 "CPU" -30.27\n'
            original = 'undervolt 0 "CPU" 0\n'
            conf.write_text(last)
            state.write_text(json.dumps({'original': original, 'last_hash': hashlib.sha256(last.encode()).hexdigest()}))
            with patch('undervolt_control.run_undervolt', return_value='ok'):
                self.assertIn('restored', uninstall_restore(conf, state))
            self.assertEqual(conf.read_text(), original)

    def test_auto_profile_includes_voltage_only_when_explicitly_set(self):
        from unittest.mock import Mock
        from profile_apply_helper import apply_settings
        profile = {
            'gpu_state': 'keep', 'basic_guard': False, 'aggressive_guard': False,
            'auto_gpu_sleep': False, 'disabled_cores': 1, 'frequency_mhz': None,
            'pl1_w': None, 'pl2_w': None, 'undervolt_cpu_mv': -30.27,
            'undervolt_cache_mv': -30.27, 'undervolt_gpu_mv': 0.0,
        }
        mocked = Mock(return_value=Mock(returncode=0, stdout='', stderr=''))
        tasks = apply_settings(profile, '0000:02:00.0', run=mocked)
        self.assertEqual(tasks[0:2], ['cores-set', 'undervolt-set'])
        self.assertEqual(mocked.call_args_list[1].args[0][1:], ['set', '-30.27', '-30.27', '0.0'])


if __name__ == "__main__":
    unittest.main()
