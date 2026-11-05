# SPDX-License-Identifier: MIT
"""Safe, hardware-independent tests of PCI detection and reversible guards."""
from __future__ import annotations

import importlib.util
import ast
import json
import re
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_source(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / 'src' / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.detector = load_source('detect_nvidia_gpu.py', 'tpns_detector_test')

    def make_device(self, address: str, vendor: str, cls: str):
        target = self.root / address
        target.mkdir()
        (target / 'vendor').write_text(vendor + '\n')
        (target / 'class').write_text(cls + '\n')

    def test_detects_single_nvidia_display_controller(self):
        self.make_device('0000:02:00.0', '0x10de', '0x030000')
        self.make_device('0000:00:02.0', '0x8086', '0x030000')
        self.make_device('0000:02:00.1', '0x10de', '0x040300')
        self.assertEqual(self.detector.detect(self.root), ['0000:02:00.0'])

    def test_detects_multiple_gpus_without_guessing(self):
        self.make_device('0000:02:00.0', '0x10de', '0x030000')
        self.make_device('0000:03:00.0', '0x10de', '0x030200')
        self.assertEqual(len(self.detector.detect(self.root)), 2)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = load_source('optimus_helper.py', 'tpns_helper_test')
        self.state_dir = self.root / 'state'
        self.manifests = self.root / 'icd.d'
        self.manifests.mkdir()
        self.nvidia_bin = self.root / 'nvidia-modprobe'
        self.nvidia_bin.write_text('#!/bin/sh\nexit 0\n')
        self.nvidia_bin.chmod(0o755)
        self.nvidia_manifest = self.manifests / 'nvidia_icd.json'
        self.nvidia_manifest.write_text('{"ICD": {"library_path": "libGLX_nvidia.so.0"}}\n')
        self.nvidia_manifest.chmod(0o644)
        self.intel_manifest = self.manifests / 'intel_icd.json'
        self.intel_manifest.write_text('{"ICD": {"library_path": "libvulkan_intel.so"}}\n')
        self.intel_manifest.chmod(0o644)
        h = self.helper
        h.STATE_DIR = self.state_dir
        h.NVIDIA_MODPROBE_STATE = self.state_dir / 'nvidia-modprobe-modes.json'
        h.DISCOVERY_STATE = self.state_dir / 'nvidia-discovery-modes.json'
        h.DRIVER_OVERRIDE_STATE = self.state_dir / 'pci-driver-override.json'
        h.PCI_BASE = self.root / 'devices'
        h.SYS_DEVICES = h.PCI_BASE
        self.pci = '0000:02:00.0'
        (h.PCI_BASE / self.pci).mkdir(parents=True)
        self.override = h.PCI_BASE / self.pci / 'driver_override'
        self.override.write_text('\n')
        h.BASIC_CONFIG = self.root / 'optimus-tray-block.conf'
        h.AGGRESSIVE_CONFIG = self.root / 'optimus-tray-probe-guard.conf'
        h.DISCOVERY_DIRS = (self.manifests,)
        h._nvidia_modprobe_paths = lambda: [self.nvidia_bin]
        # Simulate mount namespace effects; never modify host mounts in tests.
        h.PCI_MASK_STATE = self.state_dir / 'pci-config-mask.json'
        self.config_file = h.PCI_BASE / self.pci / 'config'
        self.config_file.write_bytes(b'fake PCI config')
        self.mount_active = False
        self.foreign_mount = False
        h._pci_config_target = lambda _pci: self.config_file
        h._mountpoint_is_exact = lambda _path: self.mount_active or self.foreign_mount
        h._null_mask_active = lambda _path: self.mount_active
        def fake_mount_run(args):
            if args[:2] == ['/usr/bin/mount', '--bind']:
                self.mount_active = True
            elif args[0] == '/usr/bin/umount':
                self.mount_active = False
            else:
                raise AssertionError('unexpected command: ' + repr(args))
            return ''
        h.run = fake_mount_run

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_global_pci_mask_roundtrip_and_repeated_enable(self):
        h = self.helper
        h.enable_global_pci_config_mask(self.pci)
        self.assertTrue(self.mount_active)
        self.assertTrue(h.PCI_MASK_STATE.exists())
        data = json.loads(h.PCI_MASK_STATE.read_text())
        self.assertEqual(data['pci'], self.pci)
        self.assertEqual(data['source'], '/dev/null')
        h.enable_global_pci_config_mask(self.pci)
        h.disable_global_pci_config_mask(self.pci)
        self.assertFalse(self.mount_active)
        self.assertFalse(h.PCI_MASK_STATE.exists())

    def test_global_mask_rejects_foreign_mount_without_touching_it(self):
        h = self.helper
        self.foreign_mount = True
        with self.assertRaisesRegex(RuntimeError, 'managed elsewhere'):
            h.enable_global_pci_config_mask(self.pci)
        self.assertTrue(self.foreign_mount)
        self.assertFalse(h.PCI_MASK_STATE.exists())

    def test_reboot_stale_pci_mask_record_is_safely_restored(self):
        h = self.helper
        h.enable_global_pci_config_mask(self.pci)
        self.mount_active = False
        h.disable_global_pci_config_mask(self.pci)
        self.assertFalse(h.PCI_MASK_STATE.exists())

    def test_aggressive_guard_mask_is_disabled_on_uncheck(self):
        h = self.helper
        h.enable_aggressive_guards(self.pci)
        self.assertTrue(self.mount_active)
        self.assertTrue(h.aggressive_blocked())
        h.disable_aggressive_guards(self.pci)
        self.assertFalse(self.mount_active)
        self.assertFalse(h.aggressive_blocked())

    def test_basic_guard_only_writes_modprobe_rules(self):
        h = self.helper
        h.enable_basic_block()
        self.assertTrue(h.BASIC_CONFIG.exists())
        self.assertFalse(h.AGGRESSIVE_CONFIG.exists())
        self.assertEqual(self.mode(self.nvidia_bin), 0o755)
        self.assertEqual(self.mode(self.nvidia_manifest), 0o644)
        self.assertFalse(h.NVIDIA_MODPROBE_STATE.exists())
        self.assertFalse(h.DISCOVERY_STATE.exists())
        self.assertEqual(self.override.read_text().strip(), '')
        h.disable_basic_block()
        self.assertFalse(h.BASIC_CONFIG.exists())

    def test_aggressive_guard_is_self_contained_and_restores_only_nvidia(self):
        h = self.helper
        h.enable_aggressive_guards(self.pci)
        self.assertFalse(h.BASIC_CONFIG.exists())
        self.assertTrue(h.AGGRESSIVE_CONFIG.exists())
        self.assertEqual(self.mode(self.nvidia_bin), 0o644)
        self.assertEqual(self.mode(self.nvidia_manifest), 0)
        self.assertEqual(self.mode(self.intel_manifest), 0o644)
        self.assertEqual(self.override.read_text().strip(), 'none')
        self.assertEqual(json.loads(h.DRIVER_OVERRIDE_STATE.read_text())['original'], '')
        self.assertEqual(self.mode(h.NVIDIA_MODPROBE_STATE), 0o600)
        self.assertEqual(self.mode(h.DISCOVERY_STATE), 0o600)
        h.disable_aggressive_guards(self.pci)
        self.assertFalse(h.AGGRESSIVE_CONFIG.exists())
        self.assertEqual(self.mode(self.nvidia_bin), 0o755)
        self.assertEqual(self.mode(self.nvidia_manifest), 0o644)
        self.assertFalse(h.NVIDIA_MODPROBE_STATE.exists())
        self.assertFalse(h.DISCOVERY_STATE.exists())
        self.assertFalse(h.DRIVER_OVERRIDE_STATE.exists())
        self.assertEqual(self.override.read_text().strip(), '')

    def test_basic_and_aggressive_guards_are_independent(self):
        h = self.helper
        h.enable_basic_block()
        h.enable_aggressive_guards(self.pci)
        self.assertTrue(h.BASIC_CONFIG.exists())
        self.assertTrue(h.AGGRESSIVE_CONFIG.exists())
        h.disable_aggressive_guards(self.pci)
        self.assertTrue(h.BASIC_CONFIG.exists())
        self.assertFalse(h.AGGRESSIVE_CONFIG.exists())
        self.assertEqual(self.mode(self.nvidia_bin), 0o755)
        self.assertEqual(self.mode(self.nvidia_manifest), 0o644)
        h.disable_basic_block()
        self.assertFalse(h.BASIC_CONFIG.exists())

    def test_aggressive_guard_is_idempotent(self):
        h = self.helper
        h.enable_aggressive_guards(self.pci)
        before = json.loads(h.DISCOVERY_STATE.read_text())
        h.enable_aggressive_guards(self.pci)
        self.assertEqual(json.loads(h.DISCOVERY_STATE.read_text()), before)
        h.disable_aggressive_guards(self.pci)
        h.disable_aggressive_guards(self.pci)
        self.assertEqual(self.mode(self.nvidia_manifest), 0o644)

    def test_default_kernel_null_driver_override_is_supported(self):
        h = self.helper
        self.override.write_text('(null)\n')
        h.protect_pci_driver_binding(self.pci)
        self.assertEqual(self.override.read_text().strip(), 'none')
        self.assertEqual(json.loads(h.DRIVER_OVERRIDE_STATE.read_text())['original'], '')
        h.restore_pci_driver_binding(self.pci)
        self.assertEqual(self.override.read_text().strip(), '')

    def test_external_driver_override_is_never_overwritten(self):
        h = self.helper
        self.override.write_text('vfio-pci\n')
        with self.assertRaisesRegex(RuntimeError, 'another tool'):
            h.protect_pci_driver_binding(self.pci)
        self.assertEqual(self.override.read_text().strip(), 'vfio-pci')
        self.assertFalse(h.DRIVER_OVERRIDE_STATE.exists())

    def test_interrupted_driver_override_is_recovered(self):
        h = self.helper
        h.protect_pci_driver_binding(self.pci)
        self.assertEqual(self.override.read_text().strip(), 'none')
        h.restore_pci_driver_binding(self.pci)
        self.assertEqual(self.override.read_text().strip(), '')
        self.assertFalse(h.DRIVER_OVERRIDE_STATE.exists())

    def test_legacy_saved_block_maps_to_aggressive_only(self):
        h = self.helper
        self.assertEqual(h._saved_guard_flags({'blocked': True}), (False, True))
        self.assertEqual(h._saved_guard_flags({'blocked': False}), (False, False))
        self.assertEqual(h._saved_guard_flags({'basic_blocked': True, 'aggressive_blocked': False}), (True, False))

    def test_modprobe_special_mode_bits_round_trip(self):
        h = self.helper
        self.nvidia_bin.chmod(0o4755)
        self.assertEqual(self.mode(self.nvidia_bin), 0o4755)
        h.disable_nvidia_modprobe()
        self.assertEqual(self.mode(self.nvidia_bin) & 0o111, 0)
        h.restore_nvidia_modprobe()
        self.assertEqual(self.mode(self.nvidia_bin), 0o4755)

    def test_manifest_recovery_record_exists_before_chmod(self):
        h = self.helper
        original_chmod = os.chmod

        def interrupted_chmod(path, mode):
            original_chmod(path, mode)
            if Path(path) == self.nvidia_manifest:
                # Simulate an exception after the OS has applied the change.
                raise OSError('simulated interruption')

        with patch.object(h.os, 'chmod', side_effect=interrupted_chmod):
            with self.assertRaisesRegex(OSError, 'simulated interruption'):
                h.disable_nvidia_discovery()
        self.assertEqual(self.mode(self.nvidia_manifest), 0)
        self.assertEqual(json.loads(h.DISCOVERY_STATE.read_text())[str(self.nvidia_manifest)], 0o644)
        h.restore_nvidia_discovery()
        self.assertEqual(self.mode(self.nvidia_manifest), 0o644)

    def test_modprobe_recovery_record_exists_before_chmod(self):
        h = self.helper
        original_chmod = os.chmod

        def interrupted_chmod(path, mode):
            original_chmod(path, mode)
            if Path(path) == self.nvidia_bin:
                raise OSError('simulated interruption')

        with patch.object(h.os, 'chmod', side_effect=interrupted_chmod):
            with self.assertRaisesRegex(OSError, 'simulated interruption'):
                h.disable_nvidia_modprobe()
        self.assertEqual(self.mode(self.nvidia_bin), 0o644)
        self.assertEqual(json.loads(h.NVIDIA_MODPROBE_STATE.read_text())[str(self.nvidia_bin)], 0o755)
        h.restore_nvidia_modprobe()
        self.assertEqual(self.mode(self.nvidia_bin), 0o755)


class PrivilegeBoundaryTests(unittest.TestCase):
    def test_passwordless_wrappers_use_fixed_interpreters_and_actions(self):
        expected = {
            'optimus_restore_wrapper.sh': ' restore-state ',
            'optimus_auto_off_wrapper.sh': ' auto-off ',
        }
        for filename, action in expected.items():
            text = (ROOT / 'src' / filename).read_text()
            self.assertTrue(text.startswith('#!/bin/bash\n'))
            self.assertIn('/usr/local/libexec/thinkpad-nvidia-switch-helper', text)
            self.assertIn(action, text)
            self.assertNotIn('/usr/bin/env bash', text)
            self.assertNotIn('$@', text)
            self.assertNotIn('eval ', text)

    def test_privileged_python_helper_uses_absolute_system_python(self):
        text = (ROOT / 'src' / 'optimus_helper.py').read_text()
        self.assertTrue(text.startswith('#!/usr/bin/python3 -Es\n'))

    def test_every_root_python_entry_point_ignores_the_callers_python_environment(self):
        # sudo normally resets the environment, but a distribution may keep variables such as PYTHONPATH.
        # -E ignores PYTHON* variables and -s the user site, so the caller cannot make a helper import its code as root.
        for name in ('optimus_helper.py', 'cpu_power_helper.py', 'profile_apply_helper.py', 'undervolt_helper.py'):
            with self.subTest(helper=name):
                self.assertEqual((ROOT / 'src' / name).read_text().splitlines()[0], '#!/usr/bin/python3 -Es')



class SingleInstanceSourceTests(unittest.TestCase):
    def test_gui_has_lock_and_local_ipc_single_instance_guard(self):
        text = (ROOT / 'src' / 'optimus_tray.py').read_text()
        self.assertIn('QLockFile', text)
        self.assertIn('QLocalServer', text)
        self.assertIn('QLocalSocket', text)
        self.assertIn('instance_lock.tryLock(0)', text)
        self.assertIn("data == 'SHOW'", text)
        self.assertIn('w.present_window()', text)


class SteamLauncherRemovalTests(unittest.TestCase):
    def test_old_steam_launcher_replaced_by_global_checkbox(self):
        app = (ROOT / 'src' / 'optimus_tray.py').read_text()
        helper = (ROOT / 'src' / 'optimus_helper.py').read_text()
        installer = (ROOT / 'install.sh').read_text()
        self.assertNotIn('launch_protected_steam', app)
        self.assertNotIn('steam_pci_guard', app)
        self.assertIn('Stronger protection — block background GPU checks', app)
        self.assertIn('enable_global_pci_config_mask(pci)', helper)
        self.assertIn('disable_global_pci_config_mask(pci)', helper)
        self.assertIn('rm -f "$APP_DIR/thinkpad-nvidia-switch-steam-protected.desktop"', installer)



class HistoryUiTests(unittest.TestCase):
    """Source-derived UI checks run without PySide6 or NVIDIA hardware."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = (ROOT / 'src' / 'optimus_tray.py').read_text()
        tree = ast.parse(self.source)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('load_preferences', 'save_preferences')]
        window_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                            and node.name == 'MainWindow')
        functions.extend(node for node in window_class.body
                         if isinstance(node, ast.FunctionDef) and node.name in (
                             '_store_history_preference', 'set_history_window', 'set_sample_interval'))

        class History:
            PERIODS = {'Last 1 minute': 60, 'Last 5 minutes': 300, 'Last 15 minutes': 900}
            SAMPLE_INTERVALS = {'2 seconds': 2000, '10 seconds': 10000, '30 seconds': 30000}

        self.config_dir = Path(self.temp.name) / 'config'
        self.file = self.config_dir / 'settings.json'
        namespace = {
            'json': json,
            'os': os,
            'tempfile': tempfile,
            'Path': Path,
            'CONFIG_DIR': self.config_dir,
            'PREFS_FILE': self.file,
            'HistoryGraph': History,
            'notification_settings': __import__('notification_rules').notification_settings,
            'log_detail': lambda *_: None,
            'log_operation': lambda *_: None,
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                     '<extracted UI logic>', 'exec'), namespace)
        self.logic = namespace

    def test_history_settings_roundtrip_preserves_old_options(self):
        class Graph:
            period = None
            def set_period(self, val):
                self.period = val

        class Timer:
            interval = None
            def setInterval(self, val):
                self.interval = val

        class Window:
            _store_history_preference = self.logic['_store_history_preference']
            set_history_window = self.logic['set_history_window']
            set_sample_interval = self.logic['set_sample_interval']

        window = Window()
        window.preferences = self.logic['load_preferences']()
        window.preferences['autostart'] = True
        window.preferences['restore_state'] = True
        window.history_b = Graph()
        window.sample_timer = Timer()
        window.set_history_window('Last 15 minutes')
        window.set_sample_interval('2 seconds')

        loaded = self.logic['load_preferences']()
        self.assertEqual(loaded['history_window'], 'Last 15 minutes')
        self.assertEqual(loaded['history_interval'], '2 seconds')
        self.assertTrue(loaded['autostart'])
        self.assertTrue(loaded['restore_state'])
        self.assertEqual(window.history_b.period, 'Last 15 minutes')
        self.assertEqual(window.sample_timer.interval, 2000)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.config_dir.glob('.settings-*.tmp')), [])

    def test_notification_preferences_roundtrip(self):
        prefs = self.logic['load_preferences']()
        self.assertEqual(prefs['notifications']['gpu_state'], True)
        self.assertEqual(prefs['notifications']['charger'], False)
        prefs['notifications']['gpu_state'] = False
        prefs['notifications']['charger'] = True
        prefs['notifications']['auto_profile'] = True
        self.logic['save_preferences'](prefs)
        restored = self.logic['load_preferences']()
        self.assertFalse(restored['notifications']['gpu_state'])
        self.assertTrue(restored['notifications']['charger'])
        self.assertTrue(restored['notifications']['auto_profile'])
        self.assertFalse(restored['notifications']['startup_restore'])

    def test_invalid_history_preferences_fall_back_without_resetting_other_flags(self):
        self.config_dir.mkdir(parents=True)
        self.file.write_text(json.dumps({
            'autostart': True, 'auto_restore_apps': True,
            'history_window': 'Yesterday', 'history_interval': '0 milliseconds',
        }))
        prefs = self.logic['load_preferences']()
        self.assertEqual(prefs['history_window'], 'Last 5 minutes')
        self.assertEqual(prefs['history_interval'], '10 seconds')
        self.assertTrue(prefs['autostart'])
        self.assertTrue(prefs['auto_restore_apps'])
        self.file.write_text('[]')
        self.assertEqual(self.logic['load_preferences']()['history_window'], 'Last 5 minutes')

    def test_scroll_area_and_timer_use_saved_values(self):
        self.assertIn('scroll_area = QScrollArea(self)', self.source)
        self.assertIn('scroll_area.setWidgetResizable(True)', self.source)
        self.assertGreater(self.source.index('scroll_area.setWidget(root)'),
                           self.source.index('layout.addStretch()'))
        self.assertIn('scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)', self.source)
        self.assertIn('layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)', self.source)
        self.assertIn('graph_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)', self.source)
        self.assertIn("self.history_b.set_period(self.period_combo.currentText())", self.source)
        self.assertIn("self.sample_timer.start(HistoryGraph.SAMPLE_INTERVALS[self.preferences['history_interval']])", self.source)


class DashboardAndPublishingTests(unittest.TestCase):
    """Layout, guard-recovery and publication regression checks."""

    def test_activity_log_is_shared_not_gpu_tab_local(self):
        source = (ROOT / 'src' / 'optimus_tray.py').read_text()
        self.assertEqual(source.count("self.log_btn = QPushButton('View activity log')"), 1)
        self.assertLess(source.index("self.log_btn = QPushButton('View activity log')"),
                        source.index('self.tabs = ContentSizedTabWidget()'))
        self.assertIn('header_layout.addLayout(header_top)', source)
        self.assertNotIn('gpu_tab_layout.addWidget(self.log_btn)', source)
        self.assertIn('activity_action.triggered.connect(self.show_operation_log)', source)
        self.assertIn('self.setWindowModality(Qt.WindowModality.NonModal)', source)
        self.assertIn('dialog.show()', source)

    def test_pci_mask_recovery_alone_counts_as_active_guard(self):
        source = (ROOT / 'src' / 'optimus_tray.py').read_text()
        tree = ast.parse(source)
        controller = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Controller')
        method = next(n for n in controller.body if isinstance(n, ast.FunctionDef)
                      and n.name == 'aggressive_blocked')
        exists_helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                             and n.name == 'path_exists')
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            ns = {name: root / name for name in ('AGGRESSIVE_BLOCK_FILE',
                  'NVIDIA_MODPROBE_STATE', 'DISCOVERY_STATE', 'DRIVER_OVERRIDE_STATE',
                  'PCI_MASK_STATE')}
            ns['Path'] = Path
            exec(compile(ast.fix_missing_locations(ast.Module(body=[exists_helper, method], type_ignores=[])),
                         '<guard status>', 'exec'), ns)
            assert not ns['aggressive_blocked'](None)
            ns['PCI_MASK_STATE'].touch()
            assert ns['aggressive_blocked'](None)

    def test_github_publication_files_present(self):
        for path in ('LICENSE', '.gitignore', 'README.md', 'CHANGELOG.md',
                     'SECURITY.md', 'CONTRIBUTING.md', '.github/workflows/tests.yml',
                     '.github/ISSUE_TEMPLATE/bug_report.yml'):
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).is_file())


class ReleaseMetadataTests(unittest.TestCase):
    def test_release_version_is_consistent(self):
        tray = (ROOT / 'src' / 'optimus_tray.py').read_text()
        version = re.search(r"^APP_VERSION = '([^']+)'", tray, re.M).group(1)
        installer = (ROOT / 'install.sh').read_text()
        readme = (ROOT / 'README.md').read_text()
        self.assertIn(f"APP_VERSION = '{version}'", tray)
        self.assertIn(f'RELEASE_VERSION="{version}"', installer)
        self.assertIn(f'**Version {version}**', readme)



if __name__ == '__main__':
    unittest.main()
