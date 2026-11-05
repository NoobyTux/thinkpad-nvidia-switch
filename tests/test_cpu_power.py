# SPDX-License-Identifier: MIT
"""Mock sysfs tests; no real CPU devices are altered."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tpns_cpu_power_tests', ROOT / 'src/cpu_power.py')
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


class CpuPowerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cpu = self.root / 'cpus'
        self.cpu.mkdir()
        self.pow = self.root / 'powercap'
        self.pow.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.boot = self.root / 'boot_id'
        self.boot.write_text('test-boot-1\n')
        self.patches = [patch.object(cp, 'BOOT_ID', self.boot), patch.object(cp, 'STATE_DIR', self.state),
                        patch.object(cp, 'CPU_ROOT', self.cpu), patch.object(cp, 'POWERCAP_ROOT', self.pow),
                        patch.object(cp, 'CORES_STATE', self.state / 'cpu-offlined-cores.json'),
                        patch.object(cp, 'LIMITS_STATE', self.state / 'cpu-original-limits.json'),
                        patch.object(cp, 'FREQUENCY_STATE', self.state / 'cpu-original-frequency.json'),
                        patch.object(cp, 'FIRMWARE_BOOT_LIMITS', self.state / 'cpu-firmware-boot-limits.json'),
                        patch.object(cp, 'PUBLIC_FIRMWARE_BOOT_LIMITS', self.root / 'cpu-boot-public.json'),
                        patch.object(cp, 'CORE_STATUS', self.root / 'status.json'),
                        patch.object(cp, 'CPU_REMEMBERED', self.state / 'cpu-remembered-settings.json')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        (self.cpu / 'present').write_text('0-7\n')
        # 4 physical cores with 2 siblings, cpus 0-3 and 4-7 paired.
        for n in range(8):
            path = self.cpu / f'cpu{n}'
            (path / 'topology').mkdir(parents=True)
            (path / 'topology/physical_package_id').write_text('0\n')
            (path / 'topology/core_id').write_text(str(n % 4) + '\n')
            if n != 0:
                (path / 'online').write_text('1\n')
        self.package = self.pow / 'intel-rapl:0'
        self.package.mkdir()
        (self.package / 'name').write_text('package-0\n')
        (self.package / 'constraint_0_name').write_text('long_term\n')
        (self.package / 'constraint_1_name').write_text('short_term\n')
        (self.package / 'constraint_0_power_limit_uw').write_text('47000000\n')
        (self.package / 'constraint_1_power_limit_uw').write_text('58000000\n')

    def test_platform_limits_requires_early_boot_capture(self):
        cp.set_limits(15, 20)
        with self.assertRaisesRegex(RuntimeError, 'Reboot'):
            cp.restore_firmware_boot_limits()
        self.assertEqual(cp.limits_status()['pl1'], 15)
        self.assertEqual(cp.limits_status()['pl2'], 20)

    def test_platform_boot_capture_and_restore_ignore_prior_preapp_values(self):
        # Firmware boot values are 47/58 W, while the app starts after an
        # unrelated utility already imposed 37/47 W.
        self.assertEqual(cp.capture_firmware_boot_limits(), {'pl1': 47000000, 'pl2': 58000000})
        self.assertEqual(cp.firmware_boot_limits(), {'pl1': 47000000, 'pl2': 58000000})
        with self.assertRaisesRegex(RuntimeError, 'already been captured'):
            cp.capture_firmware_boot_limits()  # Cannot silently overwrite baseline.
        (self.package / 'constraint_0_power_limit_uw').write_text('37000000')
        (self.package / 'constraint_1_power_limit_uw').write_text('47000000')
        cp.set_limits(15, 20)
        self.assertEqual(json.loads(cp.LIMITS_STATE.read_text())['original'],
                         {'pl1': 37000000, 'pl2': 47000000})
        cp.restore_firmware_boot_limits()
        self.assertEqual(cp.limits_status()['pl1'], 47)
        self.assertEqual(cp.limits_status()['pl2'], 58)
        self.assertFalse(cp.LIMITS_STATE.exists())

    def test_older_boot_snapshot_is_never_accepted(self):
        cp.capture_firmware_boot_limits()
        self.boot.write_text('test-boot-2\n')
        self.assertIsNone(cp.firmware_boot_limits())
        with self.assertRaisesRegex(RuntimeError, 'Reboot'):
            cp.restore_firmware_boot_limits()

    def test_frequency_restore_to_cpuinfo_max_not_preapp(self):
        self.make_frequency_policies()
        # CPU was already limited by another utility before this app first
        # changes frequency. Restore must not bring back that old 1900 MHz cap.
        for n in range(8):
            (self.cpufreq / f'policy{n}/scaling_max_freq').write_text('1900000\n')
        cp.set_frequency_max(1600)
        cp.restore_frequency_hardware_max()
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})
        self.assertFalse(cp.FREQUENCY_STATE.exists())

    def test_frequency_defaults_pending_for_offline_app_modified_cores(self):
        self.make_frequency_policies()
        cp.set_frequency_max(1900)
        cp.set_disabled_cores(2)
        pending = cp.restore_frequency_hardware_max()
        self.assertEqual(set(pending), {'policy2', 'policy3', 'policy6', 'policy7'})
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})
        self.assertEqual(cp._frequency_recovery()['mode'], 'defaults')
        cp.set_disabled_cores(0)
        cp.sync_frequency_after_hotplug()
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})
        self.assertFalse(cp.FREQUENCY_STATE.exists())

    def test_frequency_hardware_restore_without_prior_app_changes(self):
        self.make_frequency_policies()
        (self.cpufreq / 'policy0/scaling_max_freq').write_text('1900000\n')
        self.assertEqual(cp.restore_frequency_hardware_max(), [])
        self.assertEqual(cp.frequency_status()['current_mhz'], 3500)

    def test_readonly_gui_and_boot_service_are_installed(self):
        tray = (ROOT / 'src/optimus_tray.py').read_text()
        install = (ROOT / 'install.sh').read_text()
        service = (ROOT / 'data/thinkpad-nvidia-switch-cpu-boot-defaults.service').read_text()
        self.assertIn('public_firmware_boot_limits()', tray)
        self.assertNotIn('defaults = firmware_boot_limits()', tray)
        self.assertIn('After=sysinit.target', service)
        self.assertIn('Before=basic.target', service)
        self.assertIn('firmware-boot-capture', service)
        self.assertIn('systemctl enable "$BOOT_CAPTURE_UNIT"', install)
        self.assertNotIn('systemctl enable --now "$BOOT_CAPTURE_UNIT"', install)


    def test_public_boot_capture_status_and_private_permissions(self):
        original = cp.capture_firmware_boot_limits()
        self.assertEqual(original, {'pl1': 47000000, 'pl2': 58000000})
        self.assertEqual(cp.public_firmware_boot_limits(), original)
        self.assertEqual(cp.FIRMWARE_BOOT_LIMITS.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cp.PUBLIC_FIRMWARE_BOOT_LIMITS.stat().st_mode & 0o777, 0o644)

    def test_gui_reads_public_file_not_private_root_state(self):
        cp.capture_firmware_boot_limits()
        # A private file cannot be read by the ordinary desktop user in a real install.
        # Simulate that denial: the public view must not touch the private path.
        from unittest.mock import patch as mock_patch
        old_read = Path.read_text
        def check_read(path, *args, **kwargs):
            if path == cp.FIRMWARE_BOOT_LIMITS:
                raise PermissionError('root-only snapshot')
            return old_read(path, *args, **kwargs)
        with mock_patch.object(Path, 'read_text', autospec=True, side_effect=check_read):
            self.assertEqual(cp.public_firmware_boot_limits(),
                             {'pl1': 47000000, 'pl2': 58000000})

    def test_upgrade_publishes_existing_boot_capture_without_sampling_live_limits(self):
        cp.capture_firmware_boot_limits()
        cp.PUBLIC_FIRMWARE_BOOT_LIMITS.unlink()
        # Simulate a different tool changing package power limits after capture.
        (self.package / 'constraint_0_power_limit_uw').write_text('15000000')
        (self.package / 'constraint_1_power_limit_uw').write_text('20000000')
        self.assertEqual(cp.publish_firmware_boot_limits(), {'pl1': 47000000, 'pl2': 58000000})
        self.assertEqual(cp.public_firmware_boot_limits(), {'pl1': 47000000, 'pl2': 58000000})

    def test_public_boot_snapshot_fails_closed_if_stale_or_corrupt(self):
        cp.capture_firmware_boot_limits()
        self.boot.write_text('next-boot')
        self.assertIsNone(cp.public_firmware_boot_limits())
        with self.assertRaisesRegex(RuntimeError, 'Reboot'):
            cp.publish_firmware_boot_limits()
        self.boot.write_text('test-boot-1')
        cp.PUBLIC_FIRMWARE_BOOT_LIMITS.write_text('{corrupt')
        self.assertIsNone(cp.public_firmware_boot_limits())
        self.assertEqual(cp.firmware_boot_limits(), {'pl1': 47000000, 'pl2': 58000000})

    def test_service_and_installer_support_publication(self):
        helper = (ROOT / 'src/cpu_power_helper.py').read_text()
        install = (ROOT / 'install.sh').read_text()
        self.assertIn("elif argv == ['firmware-boot-publish']:", helper)
        self.assertIn('sudo "$CPU_HELPER_DEST" firmware-boot-publish', install)
        self.assertIn('public_firmware_boot_limits()', (ROOT / 'src/optimus_tray.py').read_text())
        self.assertIn('sudo rm -f /run/thinkpad-nvidia-switch-cpu-boot-defaults.json',
                      (ROOT / 'uninstall.sh').read_text())

    def make_frequency_policies(self):
        self.cpufreq = self.cpu / 'cpufreq'
        self.cpufreq.mkdir(exist_ok=True)
        for n in range(8):
            policy = self.cpufreq / f'policy{n}'
            policy.mkdir(exist_ok=True)
            (policy / 'affected_cpus').write_text(str(n) + '\n')
            (policy / 'related_cpus').write_text(str(n) + '\n')
            (policy / 'scaling_max_freq').write_text('3500000\n')
            (policy / 'scaling_min_freq').write_text('800000\n')
            (policy / 'cpuinfo_min_freq').write_text('800000\n')
            (policy / 'cpuinfo_max_freq').write_text('3500000\n')

    def test_frequency_skips_offline_policies_without_treating_ebusy_as_failure(self):
        self.make_frequency_policies()
        cp.set_disabled_cores(2)
        # Simulate the user's T440p: policies 4-7 exist but all four CPUs are offline.
        self.assertEqual(cp.frequency_status()['current_mhz'], 3500)
        changed = cp.set_frequency_max(1900)
        self.assertEqual(set(changed), {'policy0','policy1','policy4','policy5'})
        for n in (0,1,4,5):
            self.assertEqual((self.cpufreq / f'policy{n}/scaling_max_freq').read_text().strip(), '1900000')
        for n in (2,3,6,7):
            self.assertEqual((self.cpufreq / f'policy{n}/scaling_max_freq').read_text().strip(), '3500000')
        self.assertEqual(cp.frequency_status()['current_mhz'], 1900)
        self.assertEqual(cp.restore_frequency_max(), [])
        self.assertEqual(cp.frequency_status()['current_mhz'], 3500)

    def test_frequency_reapplied_on_reenabling_cores_and_restored_across_all(self):
        self.make_frequency_policies()
        cp.set_disabled_cores(2)
        cp.set_frequency_max(1900)
        cp.set_disabled_cores(0)
        cp.sync_frequency_after_hotplug()
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {1900000})
        cp.restore_frequency_max()
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})
        self.assertFalse(cp.FREQUENCY_STATE.exists())

    def test_missing_online_frequency_policy_does_not_apply_partial_cap(self):
        import shutil
        self.make_frequency_policies()
        shutil.rmtree(self.cpufreq / 'policy5')
        self.assertFalse(cp.frequency_status()['available'])
        with self.assertRaisesRegex(RuntimeError, 'Cannot control CPU frequency'):
            cp.set_frequency_max(1900)
        self.assertFalse(cp.FREQUENCY_STATE.exists())

    def test_frequency_restore_offline_keeps_pending_originals(self):
        self.make_frequency_policies()
        cp.set_frequency_max(1900)
        cp.set_disabled_cores(2)
        pending = cp.restore_frequency_max()
        self.assertEqual(set(pending), {'policy2', 'policy3', 'policy6', 'policy7'})
        self.assertTrue(cp.FREQUENCY_STATE.exists())
        cp.set_disabled_cores(0)
        cp.sync_frequency_after_hotplug()
        self.assertFalse(cp.FREQUENCY_STATE.exists())
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})

    def test_cap_that_reads_back_high_is_rewritten_after_a_short_wait(self):
        self.make_frequency_policies()
        pauses, stale = [], {'left': 2}
        real_read = Path.read_text

        def read(path, *args, **kwargs):
            text = real_read(path, *args, **kwargs)
            if str(path).endswith('policy0/scaling_max_freq') and text.strip() == '1900000' and stale['left'] > 0:
                stale['left'] -= 1
                return '3500000\n'          # something rewrote the limit; it settles after a moment
            return text

        with patch.object(cp, '_pause', pauses.append), patch.object(Path, 'read_text', read):
            cp.set_frequency_max(1900)
        self.assertEqual(pauses, list(cp.FREQUENCY_SETTLE_DELAYS[:2]))
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {1900000})

    def test_cap_that_never_sticks_is_rolled_back_after_the_retries(self):
        self.make_frequency_policies()
        pauses = []
        real_read = Path.read_text

        def read(path, *args, **kwargs):
            text = real_read(path, *args, **kwargs)
            return '3500000\n' if str(path).endswith('policy0/scaling_max_freq') and text.strip() == '1900000' else text

        with patch.object(cp, '_pause', pauses.append), patch.object(Path, 'read_text', read):
            with self.assertRaisesRegex(RuntimeError, r'policy0 rejected frequency cap \(3500000 kHz read back\).*previous limits restored'):
                cp.set_frequency_max(1900)
        self.assertEqual(pauses, list(cp.FREQUENCY_SETTLE_DELAYS))
        self.assertFalse(cp.FREQUENCY_STATE.exists())
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})

    def stale_readback(self, policy, old_value, reads):
        """The kernel applies a written limit a moment later: for a few reads the file still shows the old one."""
        real_read = Path.read_text
        left = {'reads': reads}

        def read(path, *args, **kwargs):
            text = real_read(path, *args, **kwargs)
            if (str(path).endswith(f'{policy}/scaling_max_freq') and text.strip() != str(old_value)
                    and left['reads'] > 0):
                left['reads'] -= 1
                return f'{old_value}\n'
            return text

        return patch.object(Path, 'read_text', read)

    def test_restore_to_hardware_maximum_waits_for_the_kernel_to_apply_it(self):
        # The user's log: capped to 800 MHz, then "Restore CPU hardware maximum" read back 800000.
        self.make_frequency_policies()
        cp.set_frequency_max(800)
        pauses = []
        with patch.object(cp, '_pause', pauses.append), self.stale_readback('policy0', 800000, 1):
            self.assertEqual(cp.restore_frequency_hardware_max(), [])
        self.assertEqual(pauses, [cp.FREQUENCY_SETTLE_DELAYS[0]])
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})
        self.assertFalse(cp.FREQUENCY_STATE.exists())

    def test_restore_that_never_reaches_hardware_maximum_keeps_the_previous_cap(self):
        self.make_frequency_policies()
        cp.set_frequency_max(800)
        pauses = []
        with patch.object(cp, '_pause', pauses.append), self.stale_readback('policy0', 800000, 99):
            with self.assertRaisesRegex(RuntimeError, r'policy0 did not accept hardware maximum 3500000 kHz '
                                                     r'\(read back 800000\).*previous limits restored'):
                cp.restore_frequency_hardware_max()
        self.assertEqual(pauses, list(cp.FREQUENCY_SETTLE_DELAYS))
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {800000})
        self.assertEqual(cp._frequency_recovery()['mode'], 'cap')

    def test_restore_of_original_limits_waits_for_the_kernel_too(self):
        self.make_frequency_policies()
        cp.set_frequency_max(1900)
        pauses = []
        with patch.object(cp, '_pause', pauses.append), self.stale_readback('policy3', 1900000, 2):
            self.assertEqual(cp.restore_frequency_max(), [])
        self.assertEqual(pauses, list(cp.FREQUENCY_SETTLE_DELAYS[:2]))
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})
        self.assertFalse(cp.FREQUENCY_STATE.exists())

    def test_nothing_is_waited_when_the_first_read_is_already_right(self):
        self.make_frequency_policies()
        pauses = []
        with patch.object(cp, '_pause', pauses.append):
            cp.set_frequency_max(1900)
            cp.restore_frequency_hardware_max()
        self.assertEqual(pauses, [])

    def test_diagnostics_describe_policies_and_known_power_tools(self):
        self.make_frequency_policies()
        (self.cpufreq / 'policy0/scaling_driver').write_text('intel_pstate\n')
        (self.cpufreq / 'policy0/scaling_governor').write_text('powersave\n')
        (self.cpu / 'intel_pstate').mkdir()
        (self.cpu / 'intel_pstate/no_turbo').write_text('0\n')
        (self.cpu / 'intel_pstate/min_perf_pct').write_text('100\n')
        proc = self.root / 'proc'
        for pid, comm in ((10, 'tlp'), (11, 'bash'), (12, 'power-profiles-')):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / 'comm').write_text(comm + '\n')
        (proc / 'self').mkdir()
        lines = cp.frequency_diagnostics(proc=proc)
        self.assertIn('cpufreq driver=intel_pstate governor=powersave epp=n/a', lines)
        self.assertTrue(any(l.startswith('intel_pstate ') and 'no_turbo=0' in l and 'min_perf_pct=100' in l for l in lines), lines)
        self.assertIn('policy0: cpus=0 min=800000 max=3500000 hw_max=3500000', lines)
        self.assertEqual(sum(l.startswith('policy') for l in lines), 8)
        self.assertEqual(lines[-1], 'power tools running: power-profiles-, tlp')

    def test_frequency_invalid_value_does_not_modify_policies(self):
        self.make_frequency_policies()
        for bad in (0, 4000, 11000, -1, True, 1900.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cp.set_frequency_max(bad)
        self.assertFalse(cp.FREQUENCY_STATE.exists())

    def test_frequency_partial_write_failure_restores_previous_maxima(self):
        self.make_frequency_policies()
        old_write = Path.write_text
        def refuse(path, content, *args, **kwargs):
            if str(path).endswith('policy2/scaling_max_freq') and content.strip() == '1900000':
                raise OSError('simulated EBUSY')
            return old_write(path, content, *args, **kwargs)
        with patch.object(Path, 'write_text', refuse):
            with self.assertRaisesRegex(RuntimeError, 'previous limits restored'):
                cp.set_frequency_max(1900)
        self.assertEqual({v['max'] for v in cp.active_frequency_policies().values()}, {3500000})
        self.assertFalse(cp.FREQUENCY_STATE.exists())

    def test_separate_cpu_preference_domains_and_old_file_migration(self):
        self.make_frequency_policies()
        cp.CPU_REMEMBERED.write_text(json.dumps({'version': 2, 'disabled_core_count': 0,
            'limits': {'pl1_uw': 15000000, 'pl2_uw': 20000000}}))
        helper = self._cpu_helper_for_tests()
        with patch.object(helper.os, 'geteuid', return_value=0):
            helper.main(['cores-set','2'])
            record = json.loads(cp.CPU_REMEMBERED.read_text())
            self.assertEqual(record['disabled_core_count'], 2)
            self.assertEqual(record['limits']['pl1_uw'], 15000000)
            self.assertIsNone(record['max_freq_khz'])
            helper.main(['frequency-set','1900'])
            record = json.loads(cp.CPU_REMEMBERED.read_text())
            self.assertEqual(record['max_freq_khz'], 1900000)
            self.assertEqual(record['limits']['pl2_uw'], 20000000)
            helper.main(['limits-set','20','25'])
            record = json.loads(cp.CPU_REMEMBERED.read_text())
            self.assertEqual(record['disabled_core_count'], 2)
            self.assertEqual(record['max_freq_khz'], 1900000)
            self.assertEqual(record['limits']['pl2_uw'], 25000000)
            helper.main(['cores-on'])
            self.assertEqual(json.loads(cp.CPU_REMEMBERED.read_text())['limits']['pl1_uw'], 20000000)
        self.assertEqual(cp.frequency_status()['current_mhz'], 1900)

    def _cpu_helper_for_tests(self):
        spec = importlib.util.spec_from_file_location('tpns_cpu_power_helper_frequency_test',
                     ROOT / 'src/cpu_power_helper.py')
        import sys
        sys.modules['cpu_power'] = cp
        self.addCleanup(lambda: sys.modules.pop('cpu_power', None))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper

    def test_remember_and_restore_frequency_after_reboot(self):
        self.make_frequency_policies()
        cp.set_disabled_cores(2)
        cp.set_frequency_max(1900)
        record = cp.remember_current_cpu()
        self.assertEqual(record['max_freq_khz'], 1900000)
        self.boot.write_text('test-boot-2\n')
        for n in range(1,8):
            (self.cpu / f'cpu{n}/online').write_text('1\n')
        for n in range(8):
            (self.cpufreq / f'policy{n}/scaling_max_freq').write_text('3500000\n')
        cp.restore_remembered_cpu()
        self.assertEqual(cp.core_control_status()['disabled'], 2)
        self.assertEqual(cp.frequency_status()['current_mhz'], 1900)

    def test_topology_and_core_offline_restore(self):
        initial = cp.topology_status()
        self.assertEqual((initial['active_cores'], initial['total_cores']), (4, 4))
        self.assertEqual((initial['active_threads'], initial['total_threads']), (8, 8))
        offline = cp.disable_two_cores()
        self.assertEqual(offline, [2, 3, 6, 7])
        self.assertEqual(cp.managed_core_status(), offline)
        after = cp.topology_status()
        self.assertEqual((after['active_cores'], after['active_threads']), (2, 4))
        self.assertTrue(cp.is_online(0))
        self.assertEqual(cp.enable_managed_cores(), offline)
        self.assertEqual(cp.topology_status()['active_threads'], 8)
        self.assertFalse(cp.CORES_STATE.exists())
        self.assertFalse(cp.CORE_STATUS.exists())

    def _invalidate_offline_topology(self):
        # Kernel hotplug can make physical_package_id/core_id unavailable or -1.
        for n in (2, 3, 6, 7):
            (self.cpu / f'cpu{n}/topology/core_id').write_text('-1\n')
            (self.cpu / f'cpu{n}/topology/physical_package_id').write_text('-1\n')

    def test_offline_topology_uses_pre_hotplug_snapshot(self):
        cp.disable_two_cores()
        saved = json.loads(cp.CORE_STATUS.read_text())
        self.assertEqual(len(saved['topology']), 4)
        self._invalidate_offline_topology()
        self.assertEqual(cp.topology_status()['active_cores'], 2)
        self.assertEqual(cp.topology_status()['total_cores'], 4)
        self.assertEqual(cp.topology_status()['active_threads'], 4)
        self.assertEqual(cp.topology_status()['total_threads'], 8)
        self.assertEqual(len(cp.topology()), 4)

    def test_legacy_250_marker_recovers_core_counts(self):
        cp.disable_two_cores()
        # Existing 2.5.0 installations did not save a topology snapshot.
        saved = json.loads(cp.CORE_STATUS.read_text())
        saved.pop('topology')
        cp.CORE_STATUS.write_text(json.dumps(saved))
        self._invalidate_offline_topology()
        result = cp.topology_status()
        self.assertTrue(result['available'])
        self.assertEqual((result['active_cores'], result['total_cores']), (2, 4))
        self.assertEqual((result['active_threads'], result['total_threads']), (4, 8))
        # Never extrapolate from a marker from an earlier boot.
        self.boot.write_text('other-boot\n')
        self.assertFalse(cp.topology_status()['available'])

    def test_remember_and_restore_after_reboot(self):
        cp.disable_two_cores()
        self._invalidate_offline_topology()
        cp.set_limits(15, 20)
        remembered = cp.remember_current_cpu()
        self.assertEqual(remembered['disabled_core_count'], 2)
        self.assertEqual(remembered['limits'], {'pl1_uw': 15000000, 'pl2_uw': 20000000})
        self.assertFalse('boot_id' in remembered)  # Desired values outlive recovery records.
        self.boot.write_text('test-boot-2\n')
        for n in (2, 3, 6, 7):
            (self.cpu / f'cpu{n}/online').write_text('1\n')
            (self.cpu / f'cpu{n}/topology/core_id').write_text(str(n % 4) + '\n')
            (self.cpu / f'cpu{n}/topology/physical_package_id').write_text('0\n')
        (self.package / 'constraint_0_power_limit_uw').write_text('47000000\n')
        (self.package / 'constraint_1_power_limit_uw').write_text('58000000\n')
        output = cp.restore_remembered_cpu()
        self.assertIn('PL1/PL2 restored', output)
        self.assertEqual(cp.topology_status()['active_cores'], 2)
        self.assertEqual(cp.limits_status()['pl1'], 15.)
        self.assertEqual(cp.limits_status()['pl2'], 20.)
        # The current boot has a fresh original-limits record, not the old boot's.
        saved = cp.session_state(cp.LIMITS_STATE)
        self.assertEqual(saved['original']['pl1'], 47000000)
        self.assertIn('already', cp.restore_remembered_cpu())

    def test_remember_all_cores_and_default_limits(self):
        cp.disable_two_cores()
        cp.set_limits(15, 20)
        cp.enable_managed_cores()
        cp.restore_limits()
        saved = cp.remember_current_cpu()
        self.assertEqual(saved['disabled_core_count'], 0)
        self.assertEqual(saved['limits']['pl1_uw'], 47000000)
        self.assertIn('PL1/PL2 already correct', cp.restore_remembered_cpu())
        self.assertTrue(cp.topology_status()['all_online'])

    def test_invalid_remembered_limits_refused_without_changes(self):
        cp.CPU_REMEMBERED.write_text(json.dumps({
            'version': 1, 'cores_disabled': True,
            'limits': {'pl1_uw': 100000000, 'pl2_uw': 200000000}
        }))
        with self.assertRaises(RuntimeError):
            cp.restore_remembered_cpu()
        self.assertTrue(cp.topology_status()['all_online'])
        self.assertEqual(cp.limits_status()['pl1'], 47.)
        self.assertFalse(cp.CORES_STATE.exists())

    def test_remember_without_rapl_does_not_guess_power(self):
        (self.package/'constraint_1_name').write_text('unknown\n')
        saved = cp.remember_current_cpu()
        self.assertEqual(saved['disabled_core_count'], 0)
        self.assertIsNone(saved['limits'])
        self.assertIn('unavailable', cp.restore_remembered_cpu())

    def test_cpu_helper_updates_saved_preferences_after_core_and_limit_actions(self):
        spec = importlib.util.spec_from_file_location('tpns_cpu_power_helper_tests',
                         ROOT / 'src/cpu_power_helper.py')
        import sys
        sys.modules['cpu_power'] = cp
        self.addCleanup(lambda: sys.modules.pop('cpu_power', None))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with patch.object(helper.os, 'geteuid', return_value=0):
            helper.main(['cores-set', '2'])
            self.assertEqual(json.loads(cp.CPU_REMEMBERED.read_text())['disabled_core_count'], 2)
            helper.main(['limits-set', '15', '20'])
            self.assertEqual(json.loads(cp.CPU_REMEMBERED.read_text())['limits']['pl1_uw'], 15000000)
            helper.main(['cores-on'])
            self.assertEqual(json.loads(cp.CPU_REMEMBERED.read_text())['disabled_core_count'], 0)

    def test_refuses_existing_offline_threads_and_does_not_modify_them(self):
        (self.cpu / 'cpu5/online').write_text('0\n')
        with self.assertRaises(RuntimeError): cp.disable_two_cores()
        self.assertFalse(cp.CORES_STATE.exists())
        self.assertFalse(cp.is_online(5))

    def test_restores_only_managed_cores(self):
        cp.disable_two_cores()
        (self.cpu / 'cpu1/online').write_text('0\n')
        cp.enable_managed_cores()
        self.assertFalse(cp.is_online(1))
        self.assertEqual(cp.topology_status()['active_threads'], 7)

    def test_invalid_core_state_never_deleted(self):
        cp.CORES_STATE.write_text('not-json')
        with self.assertRaises(RuntimeError): cp.enable_managed_cores()
        self.assertTrue(cp.CORES_STATE.exists())

    def test_stale_cpu_state_cleared_after_reboot(self):
        cp.disable_two_cores()
        # Simulate a new boot (and online logical CPUs).
        self.boot.write_text('test-boot-2\n')
        for n in (2, 3, 6, 7):
            (self.cpu / f'cpu{n}/online').write_text('1\n')
        self.assertEqual(cp.enable_managed_cores(), [])
        self.assertFalse(cp.CORES_STATE.exists())

    def test_rapl_path_selection_and_current_values(self):
        self.assertEqual(cp.limits_status(), {'available': True, 'pl1': 47.0, 'pl2': 58.0})
        self.assertEqual(cp.rapl_limits()['pl1'].name, 'constraint_0_power_limit_uw')

    def test_apply_restore_preserves_first_original(self):
        result = cp.set_limits(15.0, 20.0)
        self.assertEqual(result, {'pl1': 15000000, 'pl2': 20000000})
        cp.set_limits(12, 18)
        self.assertEqual(json.loads(cp.LIMITS_STATE.read_text())['original'], {'pl1': 47000000, 'pl2': 58000000})
        cp.restore_limits()
        self.assertEqual(cp.limits_status()['pl1'], 47.0)
        self.assertEqual(cp.limits_status()['pl2'], 58.0)
        self.assertFalse(cp.LIMITS_STATE.exists())

    def test_rejects_pl2_below_pl1_and_too_high(self):
        for a,b in [(30,20),(100,100),(5,100),(-1,15),(float('nan'), 20)]:
            with self.subTest(a=a,b=b):
                with self.assertRaises(ValueError): cp.set_limits(a, b)
        self.assertFalse(cp.LIMITS_STATE.exists())

    def test_unavailable_rapl_returns_no_controls(self):
        (self.package/'constraint_1_name').write_text('unknown\n')
        self.assertFalse(cp.limits_status()['available'])
        with self.assertRaises(RuntimeError): cp.set_limits(15,15)

    def _set_core_count(self, cores, threads=2):
        # Recreate a mock topology of arbitrary size, with interleaved SMT siblings.
        for p in self.cpu.glob('cpu*'):
            import shutil
            shutil.rmtree(p)
        (self.cpu/'present').write_text(f'0-{cores*threads-1}\n')
        for n in range(cores * threads):
            path = self.cpu/f'cpu{n}'
            (path/'topology').mkdir(parents=True)
            (path/'topology/physical_package_id').write_text('0\n')
            (path/'topology/core_id').write_text(str(n % cores)+'\n')
            if n != 0:
                (path/'online').write_text('1\n')

    def test_dynamic_four_core_counts_and_restore(self):
        self.assertEqual(cp.core_control_status()['maximum'], 3)
        self.assertEqual(cp.set_disabled_cores(1), [3,7])
        self.assertEqual(cp.core_control_status()['disabled'], 1)
        self.assertEqual(cp.topology_status()['active_threads'], 6)
        self.assertEqual(cp.set_disabled_cores(3), [1,2,3,5,6,7])
        self.assertEqual(cp.topology_status()['active_cores'], 1)
        self.assertEqual(cp.set_disabled_cores(2), [2,3,6,7])
        self.assertTrue(cp.is_online(1))
        self.assertEqual(cp.core_control_status()['disabled'], 2)
        self.assertEqual(cp.set_disabled_cores(0), [2,3,6,7])
        self.assertTrue(cp.topology_status()['all_online'])
        self.assertFalse(cp.CORES_STATE.exists())

    def test_six_core_twelve_thread_selects_complete_physical_cores(self):
        self._set_core_count(6)
        self.assertEqual(cp.core_control_status()['maximum'], 5)
        self.assertEqual(cp.set_disabled_cores(3), [3,4,5,9,10,11])
        info = cp.topology_status()
        self.assertEqual((info['active_cores'],info['total_cores']), (3,6))
        self.assertEqual((info['active_threads'],info['total_threads']), (6,12))
        self.assertEqual(cp.set_disabled_cores(1), [5,11])
        self.assertEqual(cp.enable_managed_cores(), [5,11])
        self.assertTrue(cp.topology_status()['all_online'])

    def test_mixed_smt_topology_offlines_complete_groups(self):
        # Two 2-thread cores plus three 1-thread cores; emulate heterogeneous SMT.
        self._set_core_count(5, 1)
        (self.cpu/'present').write_text('0-6\n')
        for n, core in ((5,0),(6,1)):
            path = self.cpu/f'cpu{n}'
            (path/'topology').mkdir(parents=True)
            (path/'topology/physical_package_id').write_text('0\n')
            (path/'topology/core_id').write_text(str(core)+'\n')
            (path/'online').write_text('1\n')
        self.assertEqual(cp.topology_status()['total_cores'], 5)
        self.assertEqual(cp.set_disabled_cores(4), [1,2,3,4,6])
        self.assertEqual(cp.topology_status()['active_threads'], 2)
        self.assertTrue(cp.is_online(0))
        self.assertTrue(cp.is_online(5))
        cp.set_disabled_cores(0)
        self.assertEqual(cp.topology_status()['active_threads'], 7)

    def test_single_thread_cores_and_bounds(self):
        self._set_core_count(5, 1)
        self.assertEqual(cp.core_control_status()['maximum'], 4)
        self.assertEqual(cp.set_disabled_cores(4), [1,2,3,4])
        self.assertEqual(cp.topology_status()['active_threads'], 1)
        with self.assertRaises(ValueError): cp.set_disabled_cores(5)
        cp.enable_managed_cores()
        with self.assertRaises(ValueError): cp.set_disabled_cores(-1)
        with self.assertRaises(ValueError): cp.set_disabled_cores(True)

    def test_foreign_cpu_offline_blocks_resize_without_touching_it(self):
        cp.set_disabled_cores(1)
        (self.cpu/'cpu1/online').write_text('0\n')
        self.assertFalse(cp.core_control_status()['available'])
        with self.assertRaises(RuntimeError): cp.set_disabled_cores(2)
        self.assertFalse(cp.is_online(1))
        cp.enable_managed_cores()
        self.assertFalse(cp.is_online(1))

    def test_legacy_saved_preference_migrates_after_reboot(self):
        cp.CPU_REMEMBERED.write_text(json.dumps({'version': 1, 'cores_disabled': True, 'limits': None}))
        self.assertIn('logical CPUs', cp.restore_remembered_cpu())
        self.assertEqual(cp.core_control_status()['disabled'], 2)
        current = cp.remember_current_cpu()
        self.assertEqual(current['version'], 3)
        self.assertEqual(current['disabled_core_count'], 2)

    def test_saved_nondefault_core_count_restores_after_reboot(self):
        self._set_core_count(6)
        cp.set_disabled_cores(4)
        self.assertEqual(cp.remember_current_cpu()['disabled_core_count'], 4)
        self.boot.write_text('test-boot-2\n')
        for n in range(1,12):
            (self.cpu/f'cpu{n}/online').write_text('1\n')
        cp.restore_remembered_cpu()
        self.assertEqual(cp.topology_status()['active_cores'], 2)
        self.assertEqual(cp.core_control_status()['disabled'], 4)

    def test_old_hotplug_recovery_without_topology_can_still_restore(self):
        cp.set_disabled_cores(2)
        old = json.loads(cp.CORE_STATUS.read_text()); old.pop('topology')
        cp.CORE_STATUS.write_text(json.dumps(old))
        with self.assertRaises(RuntimeError): cp.set_disabled_cores(1)
        self.assertFalse(cp.core_control_status()['available'])
        self.assertTrue(cp.core_control_status()['can_restore'])
        cp.enable_managed_cores()
        self.assertTrue(cp.topology_status()['all_online'])

    def test_rejects_more_cores_than_found_after_machine_change(self):
        cp.CPU_REMEMBERED.write_text(json.dumps({'version': 2, 'disabled_core_count': 7, 'limits': None}))
        with self.assertRaises(RuntimeError): cp.restore_remembered_cpu()
        self.assertTrue(cp.topology_status()['all_online'])

    def test_failed_core_count_change_rolls_back_and_keeps_prior_selection(self):
        cp.set_disabled_cores(1)
        original_write = Path.write_text
        failed = {'once': False}
        def fail_once(path, content, *args, **kwargs):
            if str(path) == str(self.cpu/'cpu2/online') and content.strip() == '0' and not failed['once']:
                failed['once'] = True
                raise OSError('simulated kernel refusal')
            return original_write(path, content, *args, **kwargs)
        with patch.object(Path, 'write_text', fail_once):
            with self.assertRaisesRegex(RuntimeError, 'Previous count restored'):
                cp.set_disabled_cores(3)
        self.assertEqual(cp.core_control_status()['disabled'], 1)
        self.assertEqual(cp.managed_core_status(), [3,7])
        self.assertEqual(cp.topology_status()['active_cores'], 3)

    def test_root_helper_rejects_invalid_core_arguments(self):
        spec = importlib.util.spec_from_file_location('tpns_cpu_power_helper_validation',
                              ROOT / 'src/cpu_power_helper.py')
        import sys
        sys.modules['cpu_power'] = cp
        self.addCleanup(lambda: sys.modules.pop('cpu_power', None))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with patch.object(helper.os, 'geteuid', return_value=0):
            for args in (['cores-set','-2'], ['cores-set','2.2'], ['cores-set','5000'], ['cores-set']):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    helper.main(args)
        self.assertTrue(cp.topology_status()['all_online'])

    def test_parse_cpu_list(self):
        self.assertEqual(cp.parse_cpu_list('0-3,5,7-8'), [0,1,2,3,5,7,8])
        with self.assertRaises(ValueError): cp.parse_cpu_list('0-5000')


class CpuIntegrationChecks(unittest.TestCase):
    def test_installed_root_helper_and_uninstall_order(self):
        install = (ROOT/'install.sh').read_text()
        uninstall = (ROOT/'uninstall.sh').read_text()
        gui = (ROOT/'src/optimus_tray.py').read_text()
        self.assertIn('cpu_power_helper.py', install)
        self.assertIn('cpu_power.py', install)
        self.assertIn('"$CPU_HELPER" uninstall-restore', uninstall)
        self.assertLess(uninstall.index('"$CPU_HELPER" uninstall-restore'), uninstall.index('sudo rm -rf "$STATE_DIR"'))
        self.assertIn("self.tabs.addTab(gpu_tab, 'GPU')",gui)
        self.assertIn("self.tabs.addTab(cpu_tab, 'CPU')",gui)
        self.assertIn('QScrollArea',gui)
        self.assertIn('Remember and restore GPU + CPU settings', gui)
        self.assertIn('remember_current_cpu()', (ROOT/'src/optimus_helper.py').read_text())
        wrapper = (ROOT/'src/optimus_restore_wrapper.sh').read_text()
        self.assertIn('thinkpad-nvidia-switch-cpu-helper restore-saved', wrapper)
        self.assertIn('restore_state', (ROOT/'src/optimus_helper.py').read_text())


if __name__ == '__main__':
    unittest.main()
