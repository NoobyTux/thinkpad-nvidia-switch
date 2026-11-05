# SPDX-License-Identifier: MIT
"""Profile validation, safe root command construction, and power-source tests."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'src'))
import profiles
import profile_apply_helper as helper


def valid_settings():
    return {
        'gpu_state': 'D3cold', 'basic_guard': False, 'aggressive_guard': True,
        'auto_gpu_sleep': False, 'disabled_cores': 2, 'frequency_mhz': 1900,
        'pl1_w': 15.0, 'pl2_w': 20.0,
        'undervolt_cpu_mv': None, 'undervolt_cache_mv': None, 'undervolt_gpu_mv': None,
    }


class ProfilesTests(unittest.TestCase):
    def test_profile_names_unlimited_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prefs/profiles.json'
            store = profiles.empty_store()
            store['profiles'] = [profiles.new_profile(f'Profile-{i}', valid_settings()) for i in range(110)]
            store['auto_enabled'] = True
            store['ac_profile'] = store['profiles'][0]['id']
            store['battery_profile'] = store['profiles'][109]['id']
            profiles.write_store(path, store)
            self.assertEqual(len(profiles.read_store(path)['profiles']), 110)
            self.assertEqual(profiles.assignment(profiles.read_store(path), 'battery')['name'], 'Profile-109')

    def test_invalid_duplicate_names_detected(self):
        a = profiles.new_profile('Office', valid_settings())
        b = profiles.new_profile('office', valid_settings())
        store = profiles.empty_store()
        store['profiles'] = [a, b]
        with self.assertRaisesRegex(ValueError, 'unique'):
            profiles.validate_store(store)

    def test_invalid_store_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profiles.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                profiles.read_store(path)
            self.assertEqual(path.read_text(), '{broken')

    def test_missing_store_empty(self):
        self.assertEqual(profiles.read_store('/no/such/profiles.json'), profiles.empty_store())

    def test_ac_detection_and_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / 'BAT0'
            battery.mkdir()
            (battery / 'type').write_text('Battery')
            self.assertIsNone(profiles.power_source(root))
            ac = root / 'AC'
            ac.mkdir()
            (ac / 'type').write_text('Mains')
            (ac / 'online').write_text('0')
            self.assertEqual(profiles.power_source(root), 'battery')
            (ac / 'online').write_text('1')
            self.assertEqual(profiles.power_source(root), 'ac')

    def test_startup_restoration_profile_priority(self):
        """Even with a remembered snapshot, AC/battery auto mode wins."""
        store = profiles.empty_store()
        self.assertTrue(profiles.use_legacy_startup_restore(store, True))
        self.assertFalse(profiles.use_legacy_startup_restore(store, False))
        store['auto_enabled'] = True
        self.assertFalse(profiles.use_legacy_startup_restore(store, True))
        # Missing assignment must never cause a last-used restore either.
        self.assertIsNone(profiles.assignment(store, 'ac'))
        self.assertIsNone(profiles.assignment(store, 'battery'))
        # Malformed/unreadable profile database: fail closed, not stale restore.
        self.assertFalse(profiles.use_legacy_startup_restore(None, True))

    def test_power_source_multiple_connectors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, typ, state in [('AC', 'Mains', '0'),
                                     ('USBC', 'USB_PD', '1'),
                                     ('BAT0', 'Battery', '0')]:
                d = root / name
                d.mkdir()
                (d / 'type').write_text(typ)
                (d / 'online').write_text(state)
            self.assertEqual(profiles.power_source(root), 'ac')
            (root / 'USBC' / 'online').write_text('0')
            self.assertEqual(profiles.power_source(root), 'battery')
            (root / 'USBC' / 'online').write_text('unknown')
            self.assertEqual(profiles.power_source(root), 'battery')

    def test_gui_has_profile_precedence_guards(self):
        tray = (ROOT / 'src/optimus_tray.py').read_text()
        self.assertIn('use_legacy_startup_restore(self.profile_store', tray)
        self.assertIn('self.power_source_timer.start(2000)', tray)
        self.assertIn('QTimer.singleShot(15000, self.enable_profile_autoswitch)', tray)
        self.assertIn('self.tabs.addTab(profiles_tab', tray)

    def test_invalid_settings(self):
        bad = valid_settings()
        bad['gpu_state'] = 'D0'
        with self.assertRaisesRegex(ValueError, 'turns NVIDIA on'):
            profiles.validate_settings(bad)
        bad = valid_settings()
        bad['pl1_w'] = 51
        bad['pl2_w'] = 20
        with self.assertRaises(ValueError):
            profiles.validate_settings(bad)
        bad = valid_settings()
        bad['unknown_root_command'] = 'touch /root/file'
        with self.assertRaises(ValueError):
            profiles.validate_settings(bad)

    def test_cpu_optional_fields(self):
        s = valid_settings()
        s['disabled_cores'] = s['frequency_mhz'] = s['pl1_w'] = s['pl2_w'] = None
        self.assertIsNone(profiles.validate_settings(s)['disabled_cores'])

    def test_root_payload_rejects_injection(self):
        for value in ('../../../etc/passwd', '%%%a=', 'A' * 8193):
            with self.assertRaises(ValueError):
                helper.decode_payload(value)
        raw = base64.urlsafe_b64encode(json.dumps(valid_settings()).encode()).decode()
        self.assertEqual(helper.decode_payload(raw)['frequency_mhz'], 1900)

    def test_root_helper_offline_order(self):
        run = Mock(return_value=Mock(returncode=0, stderr='', stdout='ok'))
        commands = helper.apply_settings(valid_settings(), '0000:02:00.0', run=run)
        self.assertEqual(commands, ['cores-set', 'frequency-set', 'limits-set',
                                   'gpu-off', 'aggressive-block-on', 'basic-block-off'])
        cmd = [x.args[0] for x in run.call_args_list]
        self.assertEqual(cmd[0][1:], ['cores-set', '2'])
        self.assertEqual(cmd[1][1:], ['frequency-set', '1900'])
        self.assertEqual(cmd[2][1:], ['limits-set', '15.0', '20.0'])
        self.assertEqual(cmd[3][1], 'gpu-off')
        self.assertTrue(all(x.kwargs.get('capture_output') for x in run.call_args_list))

    def test_root_helper_gpu_on_removes_protection_first(self):
        s = valid_settings()
        s['gpu_state'] = 'D0'
        s['aggressive_guard'] = False
        run = Mock(return_value=Mock(returncode=0, stderr='', stdout=''))
        commands = helper.apply_settings(s, '0000:02:00.0', run=run)
        self.assertEqual(commands[-3:], ['aggressive-block-off', 'basic-block-off', 'gpu-on'])

    def test_failure_clearly_reports_partial_change(self):
        statuses = [Mock(returncode=0, stderr='', stdout=''),
                    Mock(returncode=0, stderr='', stdout=''),
                    Mock(returncode=0, stderr='', stdout=''),
                    Mock(returncode=1, stderr='Module nvidia_drm is in use', stdout='')]
        run = Mock(side_effect=statuses)
        with self.assertRaisesRegex(RuntimeError, 'prior changes: cores-set, frequency-set, limits-set'):
            helper.apply_settings(valid_settings(), '0000:02:00.0', run=run)
        self.assertEqual(run.call_count, 4)

    def test_gpu_shutdown_failure_still_keeps_cpu_changes(self):
        run = Mock(side_effect=[Mock(returncode=0, stderr='', stdout='') for _ in range(3)] +
                        [Mock(returncode=1, stderr='modprobe: FATAL: Module nvidia_drm is in use.', stdout='')])
        with self.assertRaises(RuntimeError):
            helper.apply_settings(valid_settings(), '0000:02:00.0', run=run)
        cmds = [call.args[0][1] for call in run.call_args_list]
        self.assertEqual(cmds, ['cores-set', 'frequency-set', 'limits-set', 'gpu-off'])
        text = profiles.friendly_profile_failure('modprobe: FATAL: Module nvidia_drm is in use.')
        self.assertIn('NVIDIA cannot be turned off because it is still in use', text)
        self.assertIn('CPU changes earlier in the profile may already be active', text)

    def test_only_gpu_profile_still_runs_gpu_first(self):
        s = valid_settings()
        s['disabled_cores'] = s['frequency_mhz'] = s['pl1_w'] = s['pl2_w'] = None
        run = Mock(return_value=Mock(returncode=0, stderr='', stdout=''))
        self.assertEqual(helper.apply_settings(s, '0000:02:00.0', run=run),
                         ['gpu-off', 'aggressive-block-on', 'basic-block-off'])

    def test_gui_popups_do_not_display_privileged_audit_trace(self):
        tray = (ROOT / 'src/optimus_tray.py').read_text()
        self.assertIn('friendly_profile_failure(details)', tray)
        self.assertIn('PROFILE APPLY: name=', tray)


if __name__ == '__main__':
    unittest.main()
