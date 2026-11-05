# SPDX-License-Identifier: MIT
"""Regression tests for the defects found in the 2.6.9 audit and in later field reports.

Everything here is hardware-independent. Tests that need Qt run the real window
in a child process (offscreen platform, temporary HOME) and are skipped when
PySide6 or its system libraries are not installed.
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
sys.path.insert(0, str(SRC))


def tray_functions(*names):
    """Compile selected top-level functions/assignments of optimus_tray.py without importing Qt."""
    source = (SRC / 'optimus_tray.py').read_text()
    tree = ast.parse(source)
    wanted = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            wanted.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            wanted.append(node)
    found = {getattr(n, 'name', None) or n.targets[0].id for n in wanted}
    missing = set(names) - found
    if missing:
        raise AssertionError(f'not found in optimus_tray.py: {sorted(missing)}')
    return wanted


def qt_available():
    try:
        probe = subprocess.run([sys.executable, '-I', '-c', 'import PySide6.QtWidgets'],
                               capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


QT = qt_available()

CHILD_PRELUDE = textwrap.dedent('''
    import json, os, sys, tempfile
    from pathlib import Path
    home = tempfile.mkdtemp(prefix='tpns-test-home-')
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=home, QT_QPA_PLATFORM='offscreen',
                      OPTIMUS_PCI='0000:01:00.0')
    config = Path(home) / '.config' / 'thinkpad-nvidia-switch'
    config.mkdir(parents=True)
    sys.path.insert(0, %r)
    sys.argv = ['optimus_tray.py']
''' % str(SRC))


def run_child(body, settings=None):
    """Run ``body`` after importing the real GUI module; return its JSON result and stderr."""
    script = CHILD_PRELUDE
    if settings is not None:
        script += f"(config / 'settings.json').write_text({json.dumps(json.dumps(settings))})\n"
    script += textwrap.dedent('''
        import optimus_tray as t
        from PySide6.QtWidgets import QApplication, QWidget, QFrame
        app = QApplication(sys.argv)
        if hasattr(t, 'dark_palette'):
            app.setPalette(t.dark_palette())
        app.setStyleSheet(t.GLOBAL_STYLE)
        def pump(n=10):
            for _ in range(n):
                app.processEvents()
        result = {}
    ''') + textwrap.dedent(body) + '\nprint("RESULT=" + json.dumps(result)); sys.stdout.flush(); os._exit(0)\n'
    done = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True, text=True, timeout=180)
    marker = [line for line in done.stdout.splitlines() if line.startswith('RESULT=')]
    if not marker:
        raise AssertionError(f'child produced no result.\nstdout:\n{done.stdout}\nstderr:\n{done.stderr}')
    return json.loads(marker[-1][len('RESULT='):]), done.stderr


@unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
class TabLayoutTests(unittest.TestCase):
    """The tab frame must hug the *selected* page: no blank area below GPU/Profiles."""

    BODY = '''
        w = t.MainWindow(); w.show(); result['rows'] = []
        for width in (720, 830, 1300):
            w.resize(width, 1000); pump()
            for idx, name in enumerate(('gpu', 'cpu', 'profiles')):
                w.tabs.setCurrentIndex(idx); pump()
                page = w.tabs.currentWidget()
                bottom = max(c.mapTo(page, c.rect().bottomLeft()).y()
                             for c in page.findChildren(QWidget) if c.isVisible())
                squeezed = []
                for card in page.findChildren(QFrame):
                    if card.objectName() != 'card' or not card.isVisible():
                        continue
                    lay = card.layout()
                    need = lay.totalHeightForWidth(card.width()) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
                    if card.height() < need:
                        squeezed.append([card.height(), need])
                lay = page.layout()
                page_need = lay.totalHeightForWidth(page.width()) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
                result['rows'].append({'width': width, 'tab': name, 'tab_height': w.tabs.height(),
                                       'blank': page.height() - bottom,
                                       'page_short_by': max(0, page_need - page.height()),
                                       'squeezed': squeezed})
    '''

    @classmethod
    def setUpClass(cls):
        cls.result, cls.stderr = run_child(cls.BODY)

    def test_no_startup_exceptions(self):
        self.assertNotIn('Traceback', self.stderr)

    def test_no_large_empty_area_below_content(self):
        for row in self.result['rows']:
            with self.subTest(row=row):
                # page margin (12) + a few pixels of safety; the old code left 366-667 px.
                self.assertLessEqual(row['blank'], 40)

    def test_tab_height_follows_selected_page(self):
        for width in (720, 830, 1300):
            heights = {r['tab']: r['tab_height'] for r in self.result['rows'] if r['width'] == width}
            self.assertLess(heights['gpu'], 400, heights)
            self.assertLess(heights['gpu'], heights['profiles'], heights)
            self.assertNotEqual(heights['cpu'], heights['profiles'], heights)

    def test_content_is_never_squeezed_or_clipped(self):
        for row in self.result['rows']:
            with self.subTest(row=row):
                self.assertEqual(row['squeezed'], [])
                self.assertEqual(row['page_short_by'], 0)


@unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
class WindowBehaviourTests(unittest.TestCase):
    def test_selected_tab_is_restored_and_not_overwritten_at_startup(self):
        result, stderr = run_child('''
            w = t.MainWindow(); w.show(); pump()
            result['index'] = w.tabs.currentIndex()
            result['saved'] = json.loads((config / 'settings.json').read_text())['selected_tab']
            w.tabs.setCurrentIndex(1); pump()
            result['after_switch'] = json.loads((config / 'settings.json').read_text())['selected_tab']
        ''', settings={'selected_tab': 2})
        self.assertNotIn('Traceback', stderr)
        self.assertEqual(result, {'index': 2, 'saved': 2, 'after_switch': 1})

    def test_unreadable_state_directory_does_not_break_status_checks(self):
        result, stderr = run_child('''
            from unittest.mock import patch
            real = Path.exists
            guarded = {str(p) for p in (t.NVIDIA_MODPROBE_STATE, t.DISCOVERY_STATE,
                                        t.DRIVER_OVERRIDE_STATE, t.PCI_MASK_STATE, t.DETACHED_MARKER)}
            def exists(self, *a, **k):
                if str(self) in guarded:
                    raise PermissionError(13, 'Permission denied', str(self))
                return real(self, *a, **k)
            t.PCI = Path(home) / 'no-such-pci-device'     # independent of the host's hardware
            # ... and of an installation of the app on the machine running the tests (its guard files are real)
            t.BASIC_BLOCK_FILE = Path(home) / 'no-such-basic-guard.conf'
            t.AGGRESSIVE_BLOCK_FILE = Path(home) / 'no-such-aggressive-guard.conf'
            with patch.object(Path, 'exists', exists):
                ctrl = t.Controller()
                result['any_blocked'] = ctrl.any_blocked()
                result['aggressive'] = ctrl.aggressive_blocked()
                result['state'] = ctrl.state()
        ''')
        self.assertEqual(result, {'any_blocked': False, 'aggressive': False, 'state': 'GPU device not found'})

    def test_profile_editor_rejects_a_name_used_by_another_profile(self):
        result, _ = run_child('''
            from unittest.mock import patch
            from profiles import validate_settings
            settings = {'gpu_state': 'keep', 'basic_guard': False, 'aggressive_guard': False,
                        'auto_gpu_sleep': False, 'disabled_cores': None, 'frequency_mhz': None,
                        'pl1_w': None, 'pl2_w': None, 'undervolt_cpu_mv': None,
                        'undervolt_cache_mv': None, 'undervolt_gpu_mv': None}
            editor = t.ProfileEditor({'name': 'Quiet', 'settings': settings}, taken_names=['Battery saver'])
            with patch.object(t.QMessageBox, 'warning') as warning:
                editor.name.setText('  battery SAVER ')
                editor.accept()
                result['duplicate_accepted'] = editor.result() == t.QDialog.DialogCode.Accepted
                result['warned'] = warning.called
                editor.name.setText('Gaming')
                editor.accept()
                result['unique_accepted'] = editor.result() == t.QDialog.DialogCode.Accepted
        ''')
        self.assertEqual(result, {'duplicate_accepted': False, 'warned': True, 'unique_accepted': True})

    def test_spin_boxes_and_profile_list_are_styled_dark(self):
        result, _ = run_child('''
            w = t.MainWindow(); w.show(); pump()
            def corner(widget):
                return widget.grab().toImage().pixelColor(widget.width() // 2, widget.height() // 2).lightness()
            result['spin'] = corner(w.core_count_spin)
            result['list'] = corner(w.profile_list)
        ''')
        # Unstyled widgets rendered white (lightness 255) on the dark window.
        self.assertLess(result['spin'], 90, result)
        self.assertLess(result['list'], 90, result)


class UndervoltLimitTests(unittest.TestCase):
    """Offsets may go down to -200 mV; below -100 mV the GUI asks for extra confirmation."""

    def test_the_limit_is_one_shared_constant(self):
        import undervolt_control as uv
        self.assertEqual((uv.MIN_OFFSET_MV, uv.WARN_BELOW_MV), (-200, -100))
        gui = (SRC / 'optimus_tray.py').read_text()
        self.assertIn('MIN_OFFSET_MV', gui)
        self.assertNotIn('setRange(-100, 0)', gui)      # no second, forgotten copy of the old limit

    def test_helper_and_profiles_accept_the_floor_and_nothing_below_it(self):
        import profiles
        import undervolt_control as uv
        self.assertEqual(uv.validate_mv((-200, -150, -100.5)), (-200, -150, -100.5))
        for bad in ((-200.01, 0, 0), (0, -201, 0), (0, 0, -1000)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                uv.validate_mv(bad)
        base = {'gpu_state': 'keep', 'basic_guard': False, 'aggressive_guard': False, 'auto_gpu_sleep': False,
                'disabled_cores': None, 'frequency_mhz': None, 'pl1_w': None, 'pl2_w': None,
                'undervolt_cpu_mv': -180, 'undervolt_cache_mv': -150, 'undervolt_gpu_mv': -200}
        self.assertEqual(profiles.validate_settings(base)['undervolt_cpu_mv'], -180)
        with self.assertRaises(ValueError):
            profiles.validate_settings({**base, 'undervolt_gpu_mv': -201})

    def test_root_helper_refuses_beyond_the_floor_before_touching_the_config(self):
        import undervolt_control as uv
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / 'intel-undervolt.conf'
            conf.write_text('undervolt 0 "CPU" 0\nundervolt 1 "GPU" 0\nundervolt 2 "CPU Cache" 0\n')
            before = conf.read_text()
            with patch('undervolt_control.run_undervolt') as tool, self.assertRaises(ValueError):
                uv.apply_voltages(-201, 0, 0, conf, Path(directory) / 'state.json')
            tool.assert_not_called()
            self.assertEqual(conf.read_text(), before)


@unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
class UndervoltWarningTests(unittest.TestCase):
    SETTINGS = """
        settings = {'gpu_state': 'keep', 'basic_guard': False, 'aggressive_guard': False,
                    'auto_gpu_sleep': False, 'disabled_cores': None, 'frequency_mhz': None,
                    'pl1_w': None, 'pl2_w': None, 'undervolt_cpu_mv': None,
                    'undervolt_cache_mv': None, 'undervolt_gpu_mv': None}
    """

    @classmethod
    def script(cls, body):
        return textwrap.dedent(cls.SETTINGS) + textwrap.dedent(body)

    def test_spin_boxes_in_both_places_reach_minus_200(self):
        result, _ = run_child(self.script("""
            w = t.MainWindow()
            editor = t.ProfileEditor({'name': 'x', 'settings': settings})
            result['cpu_tab'] = [s.minimum() for s in w.undervolt_spins]
            result['editor'] = [s.minimum() for s in editor.uv_controls]
            result['max'] = [s.maximum() for s in w.undervolt_spins]
        """))
        self.assertEqual(result, {'cpu_tab': [-200] * 3, 'editor': [-200] * 3, 'max': [0] * 3})

    def test_warning_dialog_defaults_to_no_and_names_the_threshold(self):
        result, _ = run_child("""
            box = t.aggressive_undervolt_box(None, (-150, -50, -120))
            result['icon_is_warning'] = box.icon() == t.QMessageBox.Icon.Warning
            result['default_is_no'] = box.defaultButton() is box.button(t.QMessageBox.StandardButton.No)
            result['escape_is_no'] = box.escapeButton() is box.button(t.QMessageBox.StandardButton.No)
            result['text'] = box.text()
            result['profile_text'] = t.aggressive_undervolt_box(None, (-150, 0, 0), for_profile=True).text()
        """)
        self.assertTrue(result['icon_is_warning'] and result['default_is_no'] and result['escape_is_no'])
        self.assertIn('2 of these are below -100 mV', result['text'])
        self.assertIn('-150', result['text'])
        self.assertIn('1 of these is below -100 mV', result['profile_text'])
        self.assertIn('every time', result['profile_text'])

    def test_applying_asks_the_right_question(self):
        result, _ = run_child("""
            from unittest.mock import patch, MagicMock
            w = t.MainWindow()
            ran = []
            def fake_run(command, **kw):
                ran.append(command[-3:])
                return MagicMock(returncode=0, stdout='{}', stderr='')
            def attempt(values, answer):
                for spin, value in zip(w.undervolt_spins, values):
                    spin.setValue(value)
                ran.clear()
                with patch.object(t.subprocess, 'run', fake_run), \
                        patch.object(t.QMessageBox, 'question', return_value=t.QMessageBox.StandardButton.Yes) as plain, \
                        patch.object(t, 'confirm_aggressive_undervolt', return_value=answer) as strong:
                    w.apply_undervolt()
                return {'plain': plain.called, 'strong': strong.called, 'ran': list(ran)}
            result['mild'] = attempt((-50, -100, 0), True)            # -100 itself is not "below" -100
            result['deep_declined'] = attempt((-150, -50, 0), False)
            result['deep_accepted'] = attempt((-200, -50, 0), True)
        """)
        self.assertEqual(result['mild'], {'plain': True, 'strong': False, 'ran': [['-50.0', '-100.0', '0.0']]})
        self.assertEqual(result['deep_declined'], {'plain': False, 'strong': True, 'ran': []})
        self.assertEqual(result['deep_accepted'], {'plain': False, 'strong': True, 'ran': [['-200.0', '-50.0', '0.0']]})

    def test_profile_editor_asks_only_for_newly_chosen_aggressive_offsets(self):
        result, _ = run_child(self.script("""
            from unittest.mock import patch
            def run(initial_offsets, new_values, answer=True, enable=True):
                start = dict(settings, undervolt_cpu_mv=initial_offsets[0], undervolt_cache_mv=initial_offsets[1],
                             undervolt_gpu_mv=initial_offsets[2])
                editor = t.ProfileEditor({'name': 'Quiet', 'settings': start})
                editor.use_uv.setChecked(enable)
                for spin, value in zip(editor.uv_controls, new_values):
                    spin.setValue(value)
                with patch.object(t, 'confirm_aggressive_undervolt', return_value=answer) as ask:
                    editor.accept()
                return {'asked': ask.called, 'accepted': editor.result() == t.QDialog.DialogCode.Accepted}
            none = (None, None, None)
            result['new_deep_yes'] = run(none, (-150, 0, 0), True)
            result['new_deep_no'] = run(none, (-150, 0, 0), False)
            result['new_mild'] = run(none, (-80, -80, -80))
            result['unchanged_deep'] = run((-150, 0, 0), (-150, 0, 0))
            result['raised_deeper'] = run((-150, 0, 0), (-190, 0, 0), False)
            result['deep_but_unchecked'] = run(none, (-150, 0, 0), enable=False)
        """))
        self.assertEqual(result['new_deep_yes'], {'asked': True, 'accepted': True})
        self.assertEqual(result['new_deep_no'], {'asked': True, 'accepted': False})
        self.assertEqual(result['new_mild'], {'asked': False, 'accepted': True})
        self.assertEqual(result['unchanged_deep'], {'asked': False, 'accepted': True})
        self.assertEqual(result['raised_deeper'], {'asked': True, 'accepted': False})
        self.assertEqual(result['deep_but_unchecked'], {'asked': False, 'accepted': True})


GPU_ON_FAILURE_STDERR = (
    "TPNS_DETAIL|FILE READ: /etc/thinkpad-nvidia-switch/gpu-pci-address -> 0000:02:00.0\n"
    "TPNS_DETAIL|ACTION START: gpu-on; pci=0000:02:00.0\n"
    "TPNS_DETAIL|COMMAND START: modprobe nvidia\n"
    "TPNS_DETAIL|COMMAND RESULT: exit=1: modprobe nvidia\n"
    "TPNS_DETAIL|COMMAND STDERR: modprobe: FATAL: Module nvidia not found in directory /lib/modules/7.3.0-4-cachyos\n"
    "Command failed: modprobe nvidia\n"
    "modprobe: FATAL: Module nvidia not found in directory /lib/modules/7.3.0-4-cachyos\n")

BATTERY_PROFILE = {
    'gpu_state': 'D3cold', 'basic_guard': True, 'aggressive_guard': True, 'auto_gpu_sleep': True,
    'disabled_cores': 2, 'frequency_mhz': 2000, 'pl1_w': None, 'pl2_w': None,
    'undervolt_cpu_mv': -95, 'undervolt_cache_mv': -95, 'undervolt_gpu_mv': 0,
}


class FrequencyCapRefusedAfterHotplugTests(unittest.TestCase):
    """Reproduces a user's log: ``cores-set failed: policy4 rejected frequency cap (3500000 kHz read back)``.

    The cores had already changed when the helper tried to re-apply the previous 2000 MHz speed cap
    and the kernel handed back the hardware maximum. The command used to fail after doing its job,
    and the old executor then skipped every remaining step of the profile.
    """

    def setUp(self):
        import contextlib
        import importlib.util
        import test_cpu_power as T

        class Case(T.CpuPowerTests):
            def runTest(self):
                pass

        self.case = Case()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.make_frequency_policies()
        self.cp = T.cp
        with patch.dict(sys.modules, {'cpu_power': T.cp}):
            spec = importlib.util.spec_from_file_location('tpns_cpu_helper_under_test', SRC / 'cpu_power_helper.py')
            self.helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.helper)
        self.last_write = {}
        real_read, real_write = Path.read_text, Path.write_text
        case = self.case

        def read(path, *args, **kwargs):
            text = real_read(path, *args, **kwargs)
            if (str(path).endswith('policy4/scaling_max_freq') and self.last_write.get(str(path)) == 2000000
                    and all(real_read(case.cpu / f'cpu{n}' / 'online').strip() == '1' for n in range(1, 8))):
                return '3500000\n'      # with every core back online the kernel reports the hardware maximum
            return text

        def write(path, data, *args, **kwargs):
            if str(path).endswith('scaling_max_freq'):
                self.last_write[str(path)] = int(data)
            return real_write(path, data, *args, **kwargs)

        for patcher in (patch.object(Path, 'read_text', read), patch.object(Path, 'write_text', write),
                        patch('os.geteuid', return_value=0), patch.object(T.cp, '_pause', lambda _seconds: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.contextlib = contextlib
        # state at the moment the user plugged the charger in: battery profile active (2 cores off, 2000 MHz cap)
        self.cp.set_disabled_cores(2)
        self.cp.set_frequency_max(2000)

    def online(self):
        return [n for n in range(8) if n == 0 or (self.case.cpu / f'cpu{n}' / 'online').read_text().strip() == '1']

    def run_helper(self, *argv):
        import io
        out = io.StringIO()
        with self.contextlib.redirect_stdout(out):
            self.helper.main(list(argv))
        return out.getvalue()

    def test_restoring_the_cores_succeeds_with_a_warning(self):
        output = self.run_helper('cores-set', '0')
        self.assertEqual(self.online(), list(range(8)))                    # the cores really are back
        self.assertIn('All app-managed physical cores restored.', output)
        self.assertIn('Warning: the CPU speed limit could not be re-applied', output)
        self.assertIn('policy4 rejected frequency cap (3500000 kHz read back)', output)

    def test_the_other_core_commands_behave_the_same_way(self):
        self.assertIn('Warning:', self.run_helper('cores-on'))
        self.assertEqual(self.online(), list(range(8)))
        self.assertIn('Warning:', self.run_helper('cores-set', '0'))   # nothing to do: still no error

    def test_a_refused_speed_limit_leaves_diagnostics_in_the_technical_log(self):
        import io
        errors = io.StringIO()
        with patch.object(self.helper, 'set_frequency_max', side_effect=RuntimeError('policy0 rejected frequency cap')), \
                self.contextlib.redirect_stderr(errors), self.assertRaises(RuntimeError):
            self.helper.main(['frequency-set', '2000'])
        lines = [l for l in errors.getvalue().splitlines() if l.startswith('TPNS_DETAIL|SPEED LIMIT DIAGNOSTICS: ')]
        self.assertTrue(any('policy0: cpus=0 min=800000' in l for l in lines), lines)
        self.assertTrue(any('power tools running:' in l for l in lines), lines)

    def test_a_clean_resync_prints_no_warning(self):
        self.last_write.clear()                                           # kernel behaves: nothing is refused
        self.assertNotIn('Warning', self.run_helper('cores-set', '0'))

    def test_the_whole_ac_profile_is_applied_and_the_warning_reaches_the_log(self):
        import profile_apply_helper as executor
        ac = dict(BATTERY_PROFILE, gpu_state='D0', basic_guard=False, aggressive_guard=False, auto_gpu_sleep=False,
                  disabled_cores=0, frequency_mhz=3500, undervolt_cpu_mv=None, undervolt_cache_mv=None, undervolt_gpu_mv=None)

        def run(cmd, **_kwargs):
            import io
            out, err, code = io.StringIO(), '', 0
            if cmd[0].endswith('cpu-helper'):
                try:
                    with self.contextlib.redirect_stdout(out):
                        self.helper.main(list(cmd[1:]))
                except Exception as exc:                                  # a real failure would exit 1
                    err, code = str(exc), 1
            return SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err)

        warnings = []
        applied = executor.apply_settings(ac, '0000:02:00.0', run=run, sleep=lambda _s: None, warnings=warnings)
        self.assertEqual(applied[:2], ['cores-set', 'frequency-set'])
        self.assertEqual(self.online(), list(range(8)))
        self.assertTrue(any(w.startswith('cores-set:') and 'policy4 rejected' in w for w in warnings), warnings)
        caps = {int((self.case.cpufreq / f'policy{n}/scaling_max_freq').read_text()) for n in range(8)}
        self.assertEqual(caps, {3500000})

    def test_executor_collects_warnings_from_successful_steps(self):
        import profile_apply_helper as executor

        def run(cmd, **_kwargs):
            note = 'cores changed. Warning: speed cap not re-applied' if cmd[1] == 'cores-set' else ''
            return SimpleNamespace(returncode=0, stdout=note, stderr='')

        warnings = []
        executor.apply_settings(BATTERY_PROFILE, '0000:02:00.0', run=run, sleep=lambda _s: None, warnings=warnings)
        self.assertEqual(warnings, ['cores-set: speed cap not re-applied'])


class MissingDriverFailureTests(unittest.TestCase):
    """Real failure from a user's log: `modprobe nvidia` cannot find the module for the running kernel."""

    AC = dict(BATTERY_PROFILE, gpu_state='D0', basic_guard=False, aggressive_guard=False, auto_gpu_sleep=False,
              disabled_cores=0, frequency_mhz=3000)

    def test_failure_reason_has_no_audit_lines_but_they_are_forwarded(self):
        import contextlib
        import io
        import profile_apply_helper as helper

        def run(cmd, **_kwargs):
            if len(cmd) > 1 and cmd[1] == 'gpu-on':
                return SimpleNamespace(returncode=1, stdout='', stderr=GPU_ON_FAILURE_STDERR)
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        forwarded = io.StringIO()
        with contextlib.redirect_stderr(forwarded), self.assertRaises(RuntimeError) as caught:
            helper.apply_settings(self.AC, '0000:02:00.0', run=run, sleep=lambda _s: None)
        message = str(caught.exception)
        self.assertNotIn('TPNS_DETAIL', message)
        self.assertIn('gpu-on failed: Command failed: modprobe nvidia modprobe: FATAL: Module nvidia not found', message)
        self.assertIn('Applied: cores-set, frequency-set, undervolt-set, aggressive-block-off, basic-block-off.', message)
        # ...but the technical log still gets every audit line, untouched
        self.assertEqual(forwarded.getvalue().count('TPNS_DETAIL|'), 5)

    def test_message_is_actionable_and_not_retried(self):
        import profiles
        detail = 'gpu-on failed: modprobe: FATAL: Module nvidia not found in directory /lib/modules/7.3.0-4-cachyos; prior changes: none.'
        self.assertIn('not installed for the Linux kernel that is running', profiles.friendly_profile_failure(detail))
        self.assertTrue(profiles.permanent_profile_failure(detail))
        short = profiles.short_profile_failure(detail)
        self.assertEqual(short.count('.'), 1)
        self.assertIn('kernel driver is not installed', short)
        busy = 'modprobe: FATAL: Module nvidia_drm is in use'
        self.assertFalse(profiles.permanent_profile_failure(busy))       # "in use" can clear up by itself


class ProfileExecutorResilienceTests(unittest.TestCase):
    """One failed step must not leave the rest of a battery profile unapplied."""

    def run_profile(self, failing=(), flaky=(), settings=BATTERY_PROFILE, fail_counts=None):
        """``failing``: steps that always fail. ``flaky``: steps whose first attempt fails.
        ``fail_counts``: {step: n} makes the first n attempts of a step fail."""
        import profile_apply_helper as helper
        calls, sleeps, seen = [], [], {}

        def run(cmd, **_kwargs):
            step = 'undervolt-set' if cmd[0].endswith('undervolt') else cmd[1]
            calls.append(step)
            seen[step] = seen.get(step, 0) + 1
            bad = (step in failing or (step in flaky and seen[step] == 1)
                   or seen[step] <= (fail_counts or {}).get(step, 0))
            return SimpleNamespace(returncode=1 if bad else 0, stdout='', stderr=f'{step} broke' if bad else '')

        try:
            return helper.apply_settings(settings, '0000:01:00.0', run=run, sleep=sleeps.append), calls, sleeps, None
        except RuntimeError as exc:
            return None, calls, sleeps, str(exc)

    def test_speed_failure_still_applies_voltage_and_nvidia(self):
        applied, calls, _sleeps, error = self.run_profile(failing={'frequency-set'})
        self.assertIsNone(applied)
        self.assertIn('frequency-set failed: frequency-set broke', error)
        self.assertEqual([c for c in calls if c != 'frequency-set'],
                         ['cores-set', 'undervolt-set', 'gpu-off', 'aggressive-block-on', 'basic-block-on'])
        self.assertIn('Applied: cores-set, undervolt-set, gpu-off, aggressive-block-on, basic-block-on.', error)

    def test_cpu_steps_are_retried_once_after_a_short_pause(self):
        _applied, calls, sleeps, error = self.run_profile(failing={'cores-set'})
        self.assertEqual(calls.count('cores-set'), 2)
        self.assertEqual(sleeps, [1.5])
        self.assertIn('cores-set failed', error)

    def test_a_refused_speed_cap_is_tried_again_after_the_other_steps(self):
        _applied, calls, sleeps, error = self.run_profile(failing={'frequency-set'})
        # quick retry right away, then the other steps, then two more tries after longer pauses
        self.assertEqual(calls, ['cores-set', 'frequency-set', 'frequency-set', 'undervolt-set', 'gpu-off',
                                 'aggressive-block-on', 'basic-block-on', 'frequency-set', 'frequency-set'])
        self.assertEqual(sleeps, [1.5, 3.0, 5.0])
        self.assertIn('frequency-set failed', error)
        self.assertEqual(error.count('frequency-set failed'), 1)          # reported once, not once per attempt

    def test_a_speed_cap_that_only_sticks_later_is_applied_without_any_failure(self):
        applied, calls, sleeps, error = self.run_profile(fail_counts={'frequency-set': 2})
        self.assertIsNone(error)
        self.assertEqual(applied, ['cores-set', 'undervolt-set', 'gpu-off', 'aggressive-block-on',
                                   'basic-block-on', 'frequency-set'])
        self.assertEqual(sleeps, [1.5, 3.0])

    def test_a_transient_cpu_failure_is_invisible_to_the_user(self):
        applied, calls, sleeps, error = self.run_profile(flaky={'frequency-set'})
        self.assertIsNone(error)
        self.assertEqual(applied, ['cores-set', 'frequency-set', 'undervolt-set', 'gpu-off',
                                   'aggressive-block-on', 'basic-block-on'])
        self.assertEqual(sleeps, [1.5])

    def test_voltage_is_never_retried(self):
        _applied, calls, _sleeps, error = self.run_profile(failing={'undervolt-set'})
        self.assertEqual(calls.count('undervolt-set'), 1)
        self.assertIn('undervolt-set failed', error)
        self.assertIn('gpu-off', calls)

    def test_nvidia_group_stops_at_its_first_failure_and_all_failures_are_reported(self):
        _applied, calls, _sleeps, error = self.run_profile(failing={'frequency-set', 'gpu-off'})
        self.assertNotIn('aggressive-block-on', calls)   # depends on gpu-off having worked
        self.assertNotIn('basic-block-on', calls)
        self.assertIn('frequency-set failed', error)
        self.assertIn('gpu-off failed', error)
        self.assertIn('cores-set', error.split('Applied:')[1])

    def test_clean_run_returns_every_step(self):
        applied, _calls, sleeps, error = self.run_profile()
        self.assertIsNone(error)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(applied), 6)


@unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
class AutomaticFailureVisibilityTests(unittest.TestCase):
    SETUP = """
        from unittest.mock import patch
        from PySide6.QtCore import QProcess
        import profiles
        w = t.MainWindow()
        profile = profiles.new_profile('Battery', %r)
        store = profiles.empty_store()
        store.update(profiles=[profile], auto_enabled=True, battery_profile=profile['id'])
        w.profile_store = store
        w.stable_power_source = 'battery'
        class FakeProcess:
            err = b'frequency-set failed: boom; prior changes: cores-set.'
            def readAllStandardOutput(self): return b''
            def readAllStandardError(self): return self.err
            def deleteLater(self): pass
        def finish(automatic, err=None):
            w.profile_process = FakeProcess()
            if err is not None:
                w.profile_process.err = err
            w.profile_running_name = 'Battery'
            w.profile_running_auto = automatic
            w.profile_run_settings = profile['settings']
            w._profile_finished(1, QProcess.ExitStatus.NormalExit)
        sig = ('battery', profile['id'], json.dumps(profile['settings'], sort_keys=True))
    """ % BATTERY_PROFILE

    def script(self, body):
        return textwrap.dedent(self.SETUP) + textwrap.dedent(body)

    def test_failed_automatic_apply_is_announced_and_retried_exactly_once(self):
        result, _ = run_child(self.script("""
            scheduled, shown, started = [], [], []
            w.last_auto_target = sig
            w.auto_retry_used = False
            with patch.object(t.QTimer, 'singleShot', lambda ms, fn: scheduled.append((ms, fn))), \
                    patch.object(w.tray, 'showMessage', lambda *a: shown.append(a[1])), \
                    patch.object(t.QMessageBox, 'warning') as dialog, \
                    patch.object(w, '_start_profile_apply', lambda prof, automatic: started.append((prof['name'], automatic))):
                finish(True)
                result['label'] = w.profile_status_label.text()
                result['tooltip'] = w.profile_status_label.toolTip()
                result['dialog_shown'] = dialog.called
                result['messages'] = list(shown)
                result['scheduled_ms'] = [ms for ms, _ in scheduled if ms >= 1000 and ms != 1000]
                for ms, fn in list(scheduled):
                    if ms == t.AUTO_PROFILE_RETRY_MS:
                        fn()
                result['started'] = list(started)
                shown.clear(); scheduled.clear()
                finish(True)                                   # the retry fails as well
                result['second_failure_retries'] = [ms for ms, _ in scheduled if ms == t.AUTO_PROFILE_RETRY_MS]
                result['second_failure_message'] = list(shown)
        """))
        self.assertEqual(result['label'], 'Could not finish: Battery')
        self.assertIn('frequency-set failed: boom', result['tooltip'])
        self.assertFalse(result['dialog_shown'])                       # unattended: no modal dialog
        self.assertEqual(len(result['messages']), 1)
        self.assertIn('Battery', result['messages'][0])
        self.assertIn('Retrying in 10 seconds', result['messages'][0])
        self.assertEqual(result['scheduled_ms'], [10000])
        self.assertEqual(result['started'], [['Battery', True]])
        self.assertEqual(result['second_failure_retries'], [])          # exactly one retry
        self.assertEqual(len(result['second_failure_message']), 1)
        self.assertNotIn('Retrying', result['second_failure_message'][0])

    def test_missing_driver_failure_is_readable_logged_in_detail_and_not_retried(self):
        result, _ = run_child(self.script("""
            scheduled, shown, detail = [], [], []
            w.last_auto_target = sig
            w.auto_retry_used = False
            blob = %r.encode()
            with patch.object(t.QTimer, 'singleShot', lambda ms, fn: scheduled.append(ms)), \\
                    patch.object(w.tray, 'showMessage', lambda *a: shown.append(a[1])), \\
                    patch.object(t, 'log_detail', lambda m: detail.append(m)):
                finish(True, blob)
            result['tooltip'] = w.profile_status_label.toolTip()
            result['message'] = shown[0] if shown else None
            result['retries'] = [ms for ms in scheduled if ms == t.AUTO_PROFILE_RETRY_MS]
            result['audit_lines_logged'] = sum(1 for m in detail if m.startswith('HELPER: '))
        """ % GPU_ON_FAILURE_STDERR))
        self.assertNotIn('TPNS_DETAIL', result['tooltip'])
        self.assertIn('Module nvidia not found', result['tooltip'])
        self.assertIn('kernel driver is not installed', result['message'])
        self.assertNotIn('Retrying', result['message'])
        self.assertEqual(result['retries'], [])
        self.assertEqual(result['audit_lines_logged'], 5)

    def test_manual_failure_keeps_the_dialog_and_never_retries(self):
        result, _ = run_child(self.script("""
            scheduled, shown = [], []
            with patch.object(t.QTimer, 'singleShot', lambda ms, fn: scheduled.append(ms)), \
                    patch.object(w.tray, 'showMessage', lambda *a: shown.append(a)), \
                    patch.object(t.QMessageBox, 'warning') as dialog:
                finish(False)
            result.update(dialog=dialog.called, tray=len(shown), retries=[ms for ms in scheduled if ms == t.AUTO_PROFILE_RETRY_MS])
        """))
        self.assertEqual(result, {'dialog': True, 'tray': 0, 'retries': []})

    def test_retry_is_skipped_when_the_situation_changed(self):
        result, _ = run_child(self.script("""
            started = []
            w.last_auto_target = sig
            with patch.object(w, '_start_profile_apply', lambda prof, automatic: started.append(1)):
                w.stable_power_source = 'ac'                    # charger was plugged in meanwhile
                w._retry_automatic_profile(sig)
                w.stable_power_source = 'battery'
                w.profile_store['auto_enabled'] = False         # auto switching switched off meanwhile
                w._retry_automatic_profile(sig)
                w.profile_store['auto_enabled'] = True
                w.last_auto_target = None                       # profile edited / assignment changed
                w._retry_automatic_profile(sig)
                result['skipped'] = len(started)
                w.last_auto_target = sig
                w._retry_automatic_profile(sig)                 # nothing changed: this one runs
                result['ran'] = len(started)
        """))
        self.assertEqual(result, {'skipped': 0, 'ran': 1})

    def test_success_clears_the_failure_tooltip(self):
        result, _ = run_child(self.script("""
            w.profile_status_label.setToolTip('old failure details')
            w.profile_process = FakeProcess()
            w.profile_running_name = 'Battery'
            w.profile_running_auto = False
            w.profile_run_settings = profile['settings']
            with patch.object(t.QTimer, 'singleShot', lambda *a: None):
                w._profile_finished(0, QProcess.ExitStatus.NormalExit)
            result['tooltip'] = w.profile_status_label.toolTip()
            result['label'] = w.profile_status_label.text()
        """))
        self.assertEqual(result, {'tooltip': '', 'label': 'Last applied: Battery'})


class GuardLabelTests(unittest.TestCase):
    def test_labels_name_exactly_the_active_guards(self):
        ns = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=tray_functions('guard_label'), type_ignores=[])), '<g>', 'exec'), ns)
        label = ns['guard_label']
        self.assertEqual(label(True, False), 'Basic')          # used to read "Basic+Basic"
        self.assertEqual(label(False, True), 'Aggressive')
        self.assertEqual(label(True, True), 'Basic+Aggressive')
        self.assertEqual(label(False, False), 'none')


class LogTrimmingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name) / 'logs'
        body = tray_functions('_LOG_LINE_COUNTS', 'LOG_TRIM_SLACK', '_trim_log', '_append_log')
        self.ns = {'os': os, 'time': __import__('time'), 'Path': Path, 'LOG_DIR': self.dir}
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), '<log>', 'exec'), self.ns)

    def test_log_stays_bounded_with_private_permissions(self):
        log = self.dir / 'operations.log'
        for i in range(260):
            self.ns['_append_log'](log, f'event {i}', 100)
        lines = log.read_text().splitlines()
        self.assertLessEqual(len(lines), 110)
        self.assertGreaterEqual(len(lines), 100)
        self.assertTrue(lines[-1].endswith('event 259'))
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.dir.glob('*.tmp')), [])

    def test_file_is_not_reread_on_every_append(self):
        log = self.dir / 'detailed.log'
        calls = []
        real = self.ns['_trim_log']
        self.ns['_trim_log'] = lambda path, cap: calls.append(1) or real(path, cap)
        for i in range(3000):
            self.ns['_append_log'](log, f'line {i}', 500)
        # Trimming happens in ~10 % batches (about one rewrite per 50 appends here).
        # The old code re-read and rewrote the whole file on *every* append (~2500 times).
        self.assertLess(len(calls), 100, 'the log was trimmed (re-read and rewritten) far too often')

    def test_clearing_the_log_resynchronises_the_counter(self):
        log = self.dir / 'operations.log'
        for i in range(150):
            self.ns['_append_log'](log, f'event {i}', 100)
        log.unlink()
        for i in range(5):
            self.ns['_append_log'](log, f'fresh {i}', 100)
        self.assertEqual(len(log.read_text().splitlines()), 5)


class InstanceLockLocationTests(unittest.TestCase):
    def lock_file(self, config_dir):
        ns = {'os': os, 'Path': Path, 'CONFIG_DIR': Path(config_dir), 'INSTANCE_KEY': 'thinkpad-nvidia-switch-1000'}
        body = tray_functions('instance_lock_file')
        exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), '<lock>', 'exec'), ns)
        return ns['instance_lock_file']()

    def test_uses_the_session_runtime_directory(self):
        with tempfile.TemporaryDirectory() as runtime, tempfile.TemporaryDirectory() as config:
            with patch.dict(os.environ, {'XDG_RUNTIME_DIR': runtime}):
                self.assertEqual(self.lock_file(config), Path(runtime) / 'thinkpad-nvidia-switch-1000.lock')

    def test_never_falls_back_to_shared_tmp(self):
        with tempfile.TemporaryDirectory() as base:
            config = Path(base) / 'config'
            with patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(Path(base) / 'missing')}), \
                    patch('os.getuid', return_value=4_000_000_000):
                result = self.lock_file(config)
            self.assertEqual(result, config / 'thinkpad-nvidia-switch-1000.lock')
            self.assertTrue(config.is_dir())
            self.assertEqual(config.stat().st_mode & 0o777, 0o700)


class ReleaseHygieneTests(unittest.TestCase):
    def test_bytecode_caches_and_archives_are_git_ignored(self):
        ignored = (ROOT / '.gitignore').read_text().split()
        for pattern in ('__pycache__/', '*.py[cod]', '*.zip'):
            self.assertIn(pattern, ignored)

    def test_version_is_consistent_everywhere(self):
        import re
        version = re.search(r"^APP_VERSION = '([^']+)'", (SRC / 'optimus_tray.py').read_text(), re.M).group(1)
        self.assertIn(f'RELEASE_VERSION="{version}"', (ROOT / 'install.sh').read_text())
        self.assertIn(f'**Version {version}**', (ROOT / 'README.md').read_text())
        self.assertRegex((ROOT / 'CHANGELOG.md').read_text(), rf'(?m)^## {re.escape(version)}\b')

    def test_scripts_do_not_probe_root_only_state_as_the_user(self):
        # /var/lib/thinkpad-nvidia-switch may be unreadable to the desktop user; every
        # test of a path below it must go through sudo (root_has / root_has_file).
        import re
        for name in ('install.sh', 'uninstall.sh'):
            text = (ROOT / name).read_text()
            offenders = re.findall(r'\[\[[^\]]*-[efrdx]\s+"\$(?:[A-Z_]*STATE[A-Z_]*|STATE_DIR[^"]*|DETACHED_STATE|CPU_[A-Z_]+_STATE)"', text)
            self.assertEqual(offenders, [], f'{name} tests a root-only path as the desktop user: {offenders}')
            self.assertIn('root_has', text)

    def test_readme_in_page_links_all_point_at_a_heading(self):
        import re
        text = (ROOT / 'README.md').read_text()
        slugs = set()
        for heading in re.findall(r'(?m)^#{1,6}\s+(.+?)\s*$', text):
            slug = re.sub(r'[^\w\- ]', '', heading.lower()).replace(' ', '-')   # GitHub's anchor rule
            slugs.add(slug)
        links = set(re.findall(r'\]\(#([^)]+)\)', text))
        self.assertGreater(len(links), 10)
        self.assertEqual(sorted(links - slugs), [], 'README links to headings that do not exist')

    def test_readme_requirements_name_the_optional_package_and_how_to_uninstall(self):
        import re
        text = (ROOT / 'README.md').read_text()
        requirements = text[text.index('## Requirements'):text.index('## Install, upgrade and uninstall')]
        self.assertIn('**Optional.**', requirements)
        self.assertIn('intel-undervolt', requirements)
        # the copy-and-paste install block must include it, not only a table row
        block = re.search(r'```bash\n(.*?)```', requirements, re.S).group(1)
        self.assertIn('pacman -S --needed intel-undervolt', block)
        self.assertIn('[Uninstall](#uninstall)', requirements)
        for heading in ('### Install', '### Upgrade', '### Uninstall'):
            self.assertIn('\n' + heading + '\n', text)
        uninstall = text[text.index('### Uninstall'):text.index('## Using the app')]
        for needle in ('./uninstall.sh', 'Packages are not removed', 'pacman -Rns intel-undervolt', 'profiles.json'):
            self.assertIn(needle, uninstall)

    def test_installer_looks_for_the_undervolt_program_where_the_helper_does(self):
        installer = (ROOT / 'install.sh').read_text()
        self.assertIn('[[ ! -x /usr/bin/intel-undervolt && ! -x /usr/local/bin/intel-undervolt ]]', installer)
        self.assertIn('sudo pacman -S intel-undervolt', installer)
        helper = (SRC / 'undervolt_control.py').read_text()
        self.assertIn("('/usr/bin/intel-undervolt', '/usr/local/bin/intel-undervolt')", helper)
        self.assertIn('were not removed', (ROOT / 'uninstall.sh').read_text())

    def test_installer_creates_a_searchable_state_directory_and_refuses_root(self):
        text = (ROOT / 'install.sh').read_text()
        self.assertIn('sudo install -d -o root -g root -m 0755 "$STATE_DIR"', text)
        self.assertIn('not as root or with sudo', text)
        self.assertIn('TPNS_KWIN_VULKAN_FIX', text)
        self.assertIn('/usr/bin/python3', text)


class RootHelperEnvironmentTests(unittest.TestCase):
    """sudo keeps the caller's PATH unless secure_path is configured.

    A planted ``modprobe`` earlier in that PATH used to run as root through the
    passwordless sudoers rule. Every root entry point must pin PATH before it
    starts any subprocess.
    """

    def assert_pins_path(self, module, call):
        attacker = '/tmp/attacker-controlled:/opt/untrusted/bin:' + os.environ.get('PATH', '')
        with patch.dict(os.environ, {'PATH': attacker}), patch('os.geteuid', return_value=0), \
                patch.object(sys, 'argv', ['helper']):
            with self.assertRaises(Exception):
                call()          # a deliberately invalid request, rejected after PATH is set
            self.assertEqual(os.environ['PATH'], module.SAFE_PATH)

    def test_every_root_entry_point_pins_path(self):
        import cpu_power_helper
        import optimus_helper
        import profile_apply_helper
        import undervolt_control
        self.assert_pins_path(optimus_helper, optimus_helper.main)
        self.assert_pins_path(cpu_power_helper, lambda: cpu_power_helper.main(['no-such-action']))
        self.assert_pins_path(profile_apply_helper, lambda: profile_apply_helper.main(['no-such-action']))
        self.assert_pins_path(undervolt_control, lambda: undervolt_control.main(['no-such-action']))

    def test_safe_path_contains_only_absolute_system_directories(self):
        import optimus_helper
        for part in optimus_helper.SAFE_PATH.split(':'):
            self.assertTrue(part.startswith('/'), part)
            self.assertNotIn('untrusted', part)
            self.assertNotIn('tmp', part)

    def test_shell_wrappers_pin_path_before_calling_anything(self):
        for name in ('optimus_restore_wrapper.sh', 'optimus_auto_off_wrapper.sh'):
            text = (SRC / name).read_text()
            self.assertIn('export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', text)
            self.assertLess(text.index('export PATH='), text.index('/usr/local/libexec/'))


class StateFilePermissionTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o022)              # the root helper runs with at least this umask (sudo enforces it)
        self.addCleanup(os.umask, previous)     # and the test must not depend on the umask of whoever runs it
        import importlib.util
        spec = importlib.util.spec_from_file_location('tpns_cp_perm', SRC / 'cpu_power.py')
        self.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cp)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state'

    def test_state_directory_is_searchable_but_private_files_are_not_readable(self):
        with patch.object(self.cp, 'STATE_DIR', self.state):
            self.cp.save_state(self.state / 'private.json', {'a': 1})
            self.cp.save_state(self.state / 'public.json', {'a': 1}, public=True)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.state / 'private.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / 'public.json').stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['private.json', 'public.json'])

    def test_existing_root_only_directory_is_repaired(self):
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o700)
        with patch.object(self.cp, 'STATE_DIR', self.state):
            self.cp.save_state(self.state / 'x.json', {})
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)

    def test_failed_write_leaves_no_partial_file(self):
        class Unserializable:
            pass
        with patch.object(self.cp, 'STATE_DIR', self.state):
            with self.assertRaises(TypeError):
                self.cp.save_state(self.state / 'x.json', {'bad': Unserializable()})
        self.assertEqual(list(self.state.iterdir()), [])


class UndervoltUninstallTests(unittest.TestCase):
    def test_uninstall_does_not_fail_when_intel_undervolt_config_is_gone(self):
        import undervolt_control as uv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state.json'
            state.write_text(json.dumps({'original': 'undervolt 0 "CPU" 0\n', 'last_hash': 'abc'}))
            message = uv.uninstall_restore(root / 'missing.conf', state)
        self.assertIn('nothing to restore', message)


class BatterySignConventionTests(unittest.TestCase):
    def test_negative_rate_attributes_are_read_by_magnitude(self):
        from battery_telemetry import battery_snapshot
        with tempfile.TemporaryDirectory() as directory:
            bat = Path(directory) / 'BAT0'
            bat.mkdir()
            for key, value in (('type', 'Battery'), ('status', 'Discharging'), ('capacity', 50),
                               ('current_now', -1500000), ('voltage_now', 12000000),
                               ('energy_now', 36000000), ('energy_full', 72000000)):
                (bat / key).write_text(str(value))
            data = battery_snapshot(directory)
        self.assertAlmostEqual(data['discharge_w'], 18.0)
        self.assertEqual(data['remaining'], '2 h 00 min')


@unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
class LogModeButtonTests(unittest.TestCase):
    """The checked "Recent activity" / "Technical details" button was bold, but Qt sized it for regular text.

    Screenshots of the real window (and a photo from the T440p) showed a letter cut off the checked label.
    """

    def test_checked_state_does_not_change_the_font_weight(self):
        import re
        rule = re.search(r'QPushButton#logModeButton:checked\s*\{\{(.*?)\}\}', (SRC / 'optimus_tray.py').read_text(), re.S).group(1)
        self.assertNotIn('font-weight', rule)
        self.assertIn('border-color', rule)           # the selected button is still clearly marked

    def test_both_log_buttons_fit_their_label_in_either_state(self):
        result, stderr = run_child('''
            dialog = t.OperationLogDialog()
            dialog.show(); pump(20)
            def spare(button):
                return button.width() - (button.fontMetrics().horizontalAdvance(button.text()) + 2 * (14 + 1))
            result['simplified_checked'] = spare(dialog.simplified_btn)
            dialog.set_mode('detailed'); pump(20)
            result['detailed_checked'] = spare(dialog.detailed_btn)
            result['simplified_unchecked'] = spare(dialog.simplified_btn)
        ''')
        self.assertNotIn('Traceback', stderr)
        for key, room in result.items():
            self.assertGreaterEqual(room, 0, f'{key}: the label is {-room}px wider than its button (clipped)')


HELPER_BUSY = ('Command failed: modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia\n'
               'modprobe: FATAL: Module nvidia_drm is in use.\n'
               'NVIDIA is held open by: %s. Close them and try again; on KDE Wayland see the README section '
               '"KDE Wayland: keeping the desktop on Intel".')


@unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
class IdleCpuUseTests(unittest.TestCase):
    """A visible window used ~12 % of a core (a full core offscreen) while nothing was happening.

    ContentSizedTabWidget.sizeHint() invalidated the page layout on every call; each invalidation queued a
    LayoutRequest, the parent layout asked for sizeHint() again, and the window repainted ~500 times a second.
    """

    PRELUDE = '''
        import time
        from PySide6.QtCore import QObject, QEvent
        t.Controller.state = lambda self: 'D0'
        t.MainWindow.refresh_undervolt = lambda self: None
        w = t.MainWindow(); w.resize(960, 900); w.show()
        def wait(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                app.processEvents(); time.sleep(0.005)
        counts = {'paint': 0, 'layout': 0}
        class Spy(QObject):
            def eventFilter(self, obj, event):
                kind = event.type()
                if kind == QEvent.Type.Paint: counts['paint'] += 1
                elif kind == QEvent.Type.LayoutRequest: counts['layout'] += 1
                return False
    '''

    def scenario(self, body):
        result, stderr = run_child(textwrap.dedent(self.PRELUDE) + '\n' + textwrap.dedent(body))
        self.assertNotIn('Traceback', stderr)
        return result

    def test_a_visible_idle_window_does_not_keep_repainting_or_relaying_out(self):
        for tab in (0, 1, 2):
            with self.subTest(tab=tab):
                r = self.scenario(f'''
                    w.tabs.setCurrentIndex({tab}); wait(1.0)                 # settle
                    spy = Spy(); app.installEventFilter(spy)
                    wait(2.0)
                    result['per_second'] = (counts['paint'] + counts['layout']) / 2.0
                ''')
                # the loop produced hundreds of events a second; a healthy window does almost none
                # (the once-a-second refresh may legitimately repaint a label whose text changed)
                self.assertLess(r['per_second'], 15, r)

    def test_refreshing_unchanged_cpu_readings_does_not_repaint_or_relayout_the_page(self):
        # refresh_cpu() runs every second. Re-setting a spin box range/value or a label text to what it
        # already is still repaints and re-lays-out the whole CPU page in Qt (about 40 events per tick).
        r = self.scenario('''
            t.topology_status = lambda: {'available': True, 'active_cores': 4, 'total_cores': 4,
                                         'active_threads': 8, 'total_threads': 8}
            t.limits_status = lambda: {'available': True, 'pl1': 37.0, 'pl2': 47.0}
            t.frequency_status = lambda: {'available': True, 'min_mhz': 800, 'max_mhz': 3500,
                                          'current_mhz': 3500, 'mixed': False, 'reason': ''}
            t.core_control_status = lambda: {'available': True, 'maximum': 3, 'disabled': 0,
                                             'can_restore': False, 'reason': ''}
            t.public_firmware_boot_limits = lambda: {'pl1': 37e6, 'pl2': 47e6}
            w.tabs.setCurrentIndex(1); wait(1.0)
            w.timer.stop()                                   # only the explicit calls below refresh
            w.refresh_cpu(); wait(0.6)                       # the first call may legitimately set the values
            spy = Spy(); app.installEventFilter(spy)
            for _ in range(5):
                w.refresh_cpu()
            wait(0.6)
            result['events'] = counts['paint'] + counts['layout']
        ''')
        self.assertLess(r['events'], 5, r)

    def test_the_size_hint_getter_has_no_side_effects(self):
        source = (SRC / 'optimus_tray.py').read_text()
        body = source[source.index('    def _current_page_height(self):'):source.index('    def remeasure_page(self):')]
        code = '\n'.join(line for line in body.splitlines() if not line.lstrip().startswith('#'))
        self.assertNotIn('.invalidate(', code)
        self.assertNotIn('updateGeometry', code)
        # the explicit triggers still refresh stale measurements
        self.assertIn('self.tabs.remeasure_page()', source[source.index('    def _sync_tab_size(self):'):])

    def test_tabs_still_follow_their_page_when_switched_and_resized(self):
        r = self.scenario('''
            heights = {}
            for index, name in enumerate(('gpu', 'cpu', 'profiles')):
                w.tabs.setCurrentIndex(index); wait(0.4); heights[name] = w.tabs.height()
            result['distinct'] = len(set(heights.values())) == 3
            w.tabs.setCurrentIndex(0); wait(0.3); narrow_before = w.tabs.height()
            w.resize(560, 900); wait(0.5); narrow = w.tabs.height()
            w.resize(1300, 900); wait(0.5); wide = w.tabs.height()
            result['gpu_page_fits'] = all(narrow >= 90 and wide >= 90 for _ in (0,))
            page = w.tabs.currentWidget(); lay = page.layout()
            need = lay.totalHeightForWidth(page.width()) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
            result['short_by'] = max(0, need - page.height())
        ''')
        self.assertEqual(r, {'distinct': True, 'gpu_page_fits': True, 'short_by': 0})

    def test_state_style_is_applied_when_the_gpu_state_changes_not_on_every_tick(self):
        r = self.scenario('''
            from pathlib import Path
            calls = {'color': 0, 'button': 0}
            real_color, real_button = t._set_gpu_state_color, t._set_power_button_style
            t._set_gpu_state_color = lambda *a: (calls.__setitem__('color', calls['color'] + 1), real_color(*a))[1]
            t._set_power_button_style = lambda *a: (calls.__setitem__('button', calls['button'] + 1), real_button(*a))[1]
            w._styled_state = None
            states = ['D0']
            t.Controller.state = lambda self: states[0]
            for _ in range(6): w.refresh()
            result['after_6_ticks'] = dict(calls)
            result['d0'] = [w.power_btn.objectName(), t.COLOR_GREEN in w.state_label.styleSheet(), w.power_btn.text()]
            states[0] = 'D3cold'
            for _ in range(4): w.refresh()
            result['after_change'] = dict(calls)
            result['d3'] = [w.power_btn.objectName(), t.COLOR_RED in w.state_label.styleSheet(), w.power_btn.text()]
            states[0] = 'D0'
            w.refresh()
            result['back'] = [dict(calls), w.power_btn.objectName()]
        ''')
        self.assertEqual(r['after_6_ticks'], {'color': 1, 'button': 1})
        self.assertEqual(r['d0'], ['powerOffButton', True, 'Turn NVIDIA OFF'])
        self.assertEqual(r['after_change'], {'color': 2, 'button': 2})
        self.assertEqual(r['d3'], ['powerOnButton', True, 'Turn NVIDIA ON'])
        self.assertEqual(r['back'], [{'color': 3, 'button': 3}, 'powerOffButton'])

    def test_hidden_window_skips_only_the_read_outs_and_catches_up_when_shown(self):
        r = self.scenario('''
            states = ['D0']
            t.Controller.state = lambda self: states[0]
            battery = {'status': 'Discharging', 'capacity': 80}
            t.battery_snapshot = lambda: {'capacity': battery['capacity'], 'status': battery['status'], 'discharge_w': 9.0,
                                          'charge_w': None, 'remaining': '3 h 00 min', 'to_full': None}
            cpu_reads = []
            real_cpu = w.refresh_cpu
            w.refresh_cpu = lambda: (cpu_reads.append(1), real_cpu())[1]
            sent = []
            w.notify = lambda kind, text: sent.append((kind, text))
            w.refresh()                                                  # visible: label shows 80 %
            w.hide(); wait(0.2); cpu_reads.clear()
            battery['capacity'] = 79
            for _ in range(3): w.refresh()
            result['cpu_reads_while_hidden'] = len(cpu_reads)
            result['battery_label_while_hidden'] = w.battery_label.text()
            # a GPU state change while hidden must still be tracked, shown in the tray and notified
            states[0] = 'D3cold'; w.refresh()
            result['tray_state'] = w.tray_state.text()
            result['state_label'] = w.state_label.text()
            result['notified'] = [kind for kind, _text in sent]
            result['protection_checkbox_in_sync'] = w.tray_basic_block.isChecked() == w.basic_block_box.isChecked()
            w.show(); wait(0.4)
            result['cpu_reads_after_show'] = len(cpu_reads) >= 1
            result['battery_label_after_show'] = w.battery_label.text()
        ''')
        self.assertEqual(r['cpu_reads_while_hidden'], 0)
        self.assertIn('Battery 80%', r['battery_label_while_hidden'])        # not updated while hidden...
        self.assertEqual(r['tray_state'], 'GPU state: D3cold')               # ...but everything else was
        self.assertEqual(r['state_label'], 'D3cold')
        self.assertEqual(r['notified'], ['gpu_state'])
        self.assertTrue(r['protection_checkbox_in_sync'])
        self.assertTrue(r['cpu_reads_after_show'])
        self.assertIn('Battery 79%', r['battery_label_after_show'])          # ...and current the moment it is shown

    def test_voltage_poll_pauses_while_hidden_and_refreshes_when_shown(self):
        r = self.scenario('''
            reads = []
            w.refresh_undervolt = lambda: reads.append(1)
            w.hide(); wait(0.2); reads.clear()
            for _ in range(3): w._poll_undervolt()
            result['hidden'] = len(reads)
            w.show(); wait(0.3)
            result['after_show'] = len(reads)                  # refreshed straight away, not 15 s later
            reads.clear(); w._poll_undervolt()
            result['visible_poll'] = len(reads)
            result['timer_uses_the_guard'] = w.undervolt_refresh_timer.isActive()
        ''')
        self.assertEqual(r, {'hidden': 0, 'after_show': 1, 'visible_poll': 1, 'timer_uses_the_guard': True})
        self.assertIn('self.undervolt_refresh_timer.timeout.connect(self._poll_undervolt)', (SRC / 'optimus_tray.py').read_text())

    def test_process_scan_still_finds_a_program_holding_an_nvidia_device(self):
        r = self.scenario('''
            import os, tempfile
            marker = os.path.join(tempfile.mkdtemp(), 'nvidia0')
            open(marker, 'w').close()
            t.NVIDIA_DEVICE_PREFIXES = (marker,)
            holder = open(marker)                                       # this process now "holds an NVIDIA device"
            users = t.nvidia_users()
            result['found_self'] = os.getpid() in users and bool(users[os.getpid()])
            holder.close()
            result['gone_after_close'] = os.getpid() not in t.nvidia_users()
            result['pids_listed'] = os.getpid() in t._proc_pids()
        ''')
        self.assertEqual(r, {'found_self': True, 'gone_after_close': True, 'pids_listed': True})


class GpuFailureMessageTests(unittest.TestCase):
    """A failed manual switch showed nothing at all, and the profile text was vague about *what* used the GPU."""

    def message(self, holders, action='gpu-off', in_use=()):
        from notification_rules import gpu_failure_message
        return gpu_failure_message(action, HELPER_BUSY % holders, in_use)

    def test_names_the_application_and_says_what_to_do(self):
        title, text = self.message('blender (PID 4242)')
        self.assertEqual(title, "Can't turn NVIDIA off")
        self.assertEqual(text, 'NVIDIA cannot be turned off because it is in use by blender.\n\nClose it, then try again.')

    def test_never_shows_the_raw_helper_output_or_process_ids(self):
        for holders in ('blender (PID 4242)', 'steam (PID 5), RDD?Process (PID 9, 10)', 'kwin_wayland (PID 960)'):
            _title, text = self.message(holders)
            for leftover in ('modprobe', 'Command failed', 'FATAL', 'PID', 'nvidia_drm'):
                self.assertNotIn(leftover, text)

    def test_programs_the_window_saw_are_merged_without_duplicates(self):
        _title, text = self.message('steam (PID 5), RDD?Process (PID 9)', in_use=['Steam', 'blender'])
        self.assertIn('it is in use by Steam, Firefox and blender.', text)

    def test_the_desktop_is_not_presented_as_a_program_to_close(self):
        _title, text = self.message('systemd (PID 1), systemd-logind (PID 583), kwin_wayland (PID 960), Xwayland (PID 1052)')
        self.assertIn('the desktop session (kwin_wayland and Xwayland) is holding it open', text)
        self.assertNotIn('systemd', text)
        self.assertIn('This is not a program you can close', text)
        self.assertIn('KDE Wayland: keeping the desktop on Intel', text)

    def test_programs_and_desktop_together(self):
        _title, text = self.message('kwin_wayland (PID 960), blender (PID 4242)')
        self.assertIn('it is in use by blender and by the desktop session (kwin_wayland)', text)
        self.assertIn('Close the applications first, then try again', text)

    def test_nothing_found_says_so_instead_of_blaming_an_app(self):
        from notification_rules import gpu_failure_message
        _title, text = gpu_failure_message('gpu-off', 'Command failed: modprobe -r nvidia\nmodprobe: FATAL: Module nvidia is in use.')
        self.assertIn('it is still in use, but no program holding it was found', text)

    def test_other_failures_get_their_own_short_reason(self):
        from notification_rules import gpu_failure_message
        missing = 'Command failed: modprobe nvidia\nmodprobe: FATAL: Module nvidia not found in directory /lib/modules/7.3.0'
        title, text = gpu_failure_message('gpu-on', missing)
        self.assertEqual(title, 'NVIDIA driver not available')
        self.assertIn('not installed for the Linux kernel that is running', text)
        title, text = gpu_failure_message('gpu-on', 'Command failed: modprobe nvidia\nmodprobe: ERROR: ../libkmod/libkmod-module.c: something odd')
        self.assertEqual(title, "Couldn't turn NVIDIA on")
        self.assertTrue(text.startswith('modprobe: ERROR: ../libkmod'))
        self.assertIn('View activity log', text)
        title, text = gpu_failure_message('gpu-off', 'The privileged operation timed out.')
        self.assertIn('timed out', text)

    def test_protection_switches_use_the_same_explanation(self):
        title, text = self.message('blender (PID 4242)', action='aggressive-block-on')
        self.assertEqual(title, "Couldn't turn on Stronger protection")
        self.assertIn('Could not turn on Stronger protection because it is in use by blender.', text)

    def test_names_are_parsed_from_the_helper_line(self):
        import profiles
        details = HELPER_BUSY % 'kwin_wayland (PID 960), RDD?Process (PID 9, 10), and 2 more'
        self.assertEqual(profiles.held_open_names(details), ['kwin_wayland', 'RDD Process'])
        self.assertEqual(profiles.classify_holders(['systemd-logind', 'rdd process', 'Firefox', 'Xwayland']),
                         (['Firefox'], ['Xwayland']))

    def test_profile_balloon_is_one_clear_sentence(self):
        import profiles
        failure = 'gpu-off failed: ' + HELPER_BUSY % 'steam (PID 5), blender (PID 6)'
        self.assertEqual(profiles.short_profile_failure(failure), 'NVIDIA cannot be turned off because it is in use by Steam and blender.')


@unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
class BusyButtonTests(unittest.TestCase):
    def test_spinner_is_drawn_animates_and_goes_away(self):
        result, stderr = run_child('''
            import time
            button = t.BusyButton('Turn NVIDIA OFF'); button.resize(420, 56); button.show(); pump()
            idle = button.grab().toImage()
            button.setBusy(True); pump()
            first = button.grab().toImage()
            time.sleep(0.25); pump()
            later = button.grab().toImage()
            result['busy'] = button.isBusy()
            result['spinner_drawn'] = first != idle
            result['animates'] = later != first
            result['timer_running'] = button._timer.isActive()
            button.setBusy(False); pump()
            result['restored'] = button.grab().toImage() == idle
            result['timer_stopped'] = not button._timer.isActive()
            result['text_untouched'] = button.text()
        ''')
        self.assertNotIn('Traceback', stderr)
        self.assertEqual(result, {'busy': True, 'spinner_drawn': True, 'animates': True, 'timer_running': True,
                                  'restored': True, 'timer_stopped': True, 'text_untouched': 'Turn NVIDIA OFF'})


@unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
class GpuSwitchTests(unittest.TestCase):
    """The ON/OFF button: non-blocking, spinning until Linux reports the new state, and explaining failures."""

    SETUP = '''
        import time
        from PySide6.QtWidgets import QMessageBox
        import profiles
        store = profiles.empty_store()
        item = profiles.new_profile('Quiet', dict(gpu_state='keep', basic_guard=False, aggressive_guard=False, auto_gpu_sleep=False,
                                    disabled_cores=None, frequency_mhz=None, pl1_w=None, pl2_w=None,
                                    undervolt_cpu_mv=None, undervolt_cache_mv=None, undervolt_gpu_mv=None))
        store['profiles'] = [item]
        profiles.write_store(config / 'profiles.json', store)
        marker = Path(home) / 'pci-state'; marker.write_text('D0')
        counter = Path(home) / 'runs'; counter.write_text('')
        t.Controller.state = lambda self: marker.read_text().strip()
        t.MainWindow.current_nvidia_usage = lambda self: [('Steam', True)]
        shown, balloons = [], []
        QMessageBox.warning = lambda *a, **k: shown.append((a[1], a[2]))
        QMessageBox.information = lambda *a, **k: shown.append(('info', a[2]))
        w = t.MainWindow(); w.show(); pump()
        w.notify_problem = balloons.append
        def until(condition, seconds=6.0):
            end = time.monotonic() + seconds
            while time.monotonic() < end and not condition():
                app.processEvents(); time.sleep(0.01)
            return condition()
        def helper(script):
            w._gpu_command = lambda action: ['sh', '-c', script.replace('MARK', str(marker)).replace('COUNT', str(counter))]
    '''

    def scenario(self, body):
        result, stderr = run_child(textwrap.dedent(self.SETUP) + '\n' + textwrap.dedent(body))
        self.assertNotIn('Traceback', stderr)
        return result

    def test_switch_does_not_block_and_spins_until_the_state_changes(self):
        r = self.scenario('''
            helper('sleep 0.4; echo D3cold > MARK; echo "NVIDIA modules unloaded; GPU reached D3cold."')
            before = time.monotonic(); w.toggle_gpu(); result['returned_in_time'] = time.monotonic() - before < 0.25
            result['busy'] = w.power_btn.isBusy()
            result['label'] = w.power_btn.text()
            result['controls_locked'] = not (w.tray_toggle.isEnabled() or w.basic_block_box.isEnabled()
                                             or w.aggressive_block_box.isEnabled() or w.profile_apply_button.isEnabled())
            result['finished'] = until(lambda: not w.power_btn.isBusy())
            result['label_after'] = w.power_btn.text()
            result['controls_back'] = w.tray_toggle.isEnabled() and w.basic_block_box.isEnabled() and w.profile_apply_button.isEnabled()
            result['dialogs'] = len(shown)
        ''')
        self.assertEqual(r, {'returned_in_time': True, 'busy': True, 'label': 'Turning NVIDIA off…', 'controls_locked': True,
                             'finished': True, 'label_after': 'Turn NVIDIA ON', 'controls_back': True, 'dialogs': 0})

    def test_spinner_outlives_the_helper_until_linux_reports_the_new_state(self):
        r = self.scenario('''
            helper('echo ok; (sleep 0.9; echo D3cold > MARK) >/dev/null 2>&1 &')
            w.toggle_gpu()
            until(lambda: w.gpu_process is None, 3)                    # the helper itself has finished...
            result['helper_done'] = w.gpu_process is None
            result['still_busy'] = w.power_btn.isBusy()                # ...but the GPU has not changed state yet
            result['finished'] = until(lambda: not w.power_btn.isBusy(), 6)
            result['label_after'] = w.power_btn.text()
        ''')
        self.assertEqual(r, {'helper_done': True, 'still_busy': True, 'finished': True, 'label_after': 'Turn NVIDIA ON'})

    def test_a_second_click_while_busy_starts_nothing(self):
        r = self.scenario('''
            helper('echo x >> COUNT; sleep 0.4; echo D3cold > MARK')
            w.toggle_gpu(); w.toggle_gpu(); w.power_btn.click()
            until(lambda: not w.power_btn.isBusy())
            result['runs'] = len(counter.read_text().split())
        ''')
        self.assertEqual(r, {'runs': 1})

    def test_failure_names_what_uses_the_gpu_in_a_dialog_and_stops_spinning(self):
        r = self.scenario('''
            helper('echo "Command failed: modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia" >&2; '
                   'echo "modprobe: FATAL: Module nvidia_drm is in use." >&2; '
                   'echo "NVIDIA is held open by: blender (PID 4242). Close them and try again; on KDE Wayland see the README." >&2; exit 1')
            w.toggle_gpu()
            result['finished'] = until(lambda: not w.power_btn.isBusy())
            result['label_after'] = w.power_btn.text()                # state did not change: still offers OFF
            result['title'] = shown[0][0]; result['text'] = shown[0][1]
            result['balloons'] = len(balloons)
        ''')
        self.assertTrue(r['finished'])
        self.assertEqual(r['label_after'], 'Turn NVIDIA OFF')
        self.assertEqual(r['title'], "Can't turn NVIDIA off")
        self.assertIn('it is in use by blender and Steam.', r['text'])      # the helper's list plus the window's own
        self.assertIn('Close them, then try again.', r['text'])
        self.assertIn('Technical details are in View activity log.', r['text'])
        for leftover in ('modprobe', 'FATAL', 'PID', 'Command failed'):
            self.assertNotIn(leftover, r['text'])
        self.assertEqual(r['balloons'], 0)

    def test_failure_from_the_tray_with_a_hidden_window_is_a_notification(self):
        r = self.scenario('''
            helper('echo "modprobe: FATAL: Module nvidia_drm is in use." >&2; '
                   'echo "NVIDIA is held open by: blender (PID 4242). Close them and try again." >&2; exit 1')
            w.hide(); pump()
            w.tray_toggle.trigger()
            result['finished'] = until(lambda: not w.power_btn.isBusy())
            result['dialogs'] = len(shown)
            result['balloon'] = balloons[0] if balloons else ''
        ''')
        self.assertTrue(r['finished'])
        self.assertEqual(r['dialogs'], 0)
        self.assertEqual(r['balloon'], "Can't turn NVIDIA off. NVIDIA cannot be turned off because it is in use by blender and Steam.")

    def test_switch_is_refused_while_a_profile_is_being_applied(self):
        r = self.scenario('''
            helper('echo x >> COUNT')
            w.profile_process = object()
            w.toggle_gpu()
            result['started'] = len(counter.read_text().split()); result['busy'] = w.power_btn.isBusy()
            result['told_user'] = bool(shown) and 'power profile is being applied' in shown[0][1]
            w.profile_process = None
        ''')
        self.assertEqual(r, {'started': 0, 'busy': False, 'told_user': True})

    def test_automatic_profile_waits_for_a_running_switch(self):
        r = self.scenario('''
            applied = []
            w._start_profile_apply = lambda *a, **k: applied.append(1)
            w.profile_auto_ready = True
            w.profile_store['auto_enabled'] = True
            w.profile_store['ac_profile'] = w.profile_store['battery_profile'] = w.profile_store['profiles'][0]['id']
            t.power_source = lambda: 'ac'
            helper('sleep 0.5; echo D3cold > MARK')
            w.toggle_gpu()
            for _ in range(4): w.check_power_profile()
            result['applied_while_busy'] = len(applied)
            until(lambda: not w.power_btn.isBusy())
            for _ in range(3): w.check_power_profile()
            result['applied_after'] = len(applied)
        ''')
        self.assertEqual(r, {'applied_while_busy': 0, 'applied_after': 1})        # not lost: it ran as soon as the switch finished

    def test_toggle_no_longer_calls_the_privileged_helper_synchronously(self):
        source = (SRC / 'optimus_tray.py').read_text()
        body = source[source.index('    def toggle_gpu(self):'):source.index('    def _gpu_command(self')]
        self.assertNotIn('self.ctrl.call(', body)
        self.assertNotIn('subprocess.run', body)


class UnloadOutcomeTests(unittest.TestCase):
    """ThinkPad T440p log: `modprobe -r` exited 1 with "Module i915 is in use" although all NVIDIA modules were gone.

    The GPU then slept by itself, the automatic retry found nothing left to unload and succeeded, but the user got a
    failure warning for a profile that had worked.
    """

    PCI = '0000:02:00.0'
    I915_IN_USE = ('Command failed: modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia\n'
                   'modprobe: FATAL: Module i915 is in use.')

    def setUp(self):
        spec = importlib.util.spec_from_file_location('tpns_helper_outcome', SRC / 'optimus_helper.py')
        self.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.h)
        self.loaded = set(self.h.MODULES)

    def run_that_removes(self, *removed, error=I915_IN_USE):
        def run(_command):
            self.loaded.difference_update(removed)
            raise RuntimeError(error)
        return run

    def traced(self):
        import contextlib
        import io
        self.h.DETAIL_ENABLED = True
        self.addCleanup(setattr, self.h, 'DETAIL_ENABLED', False)
        stream = io.StringIO()
        return stream, contextlib.redirect_stderr(stream)

    def test_error_about_another_module_is_ignored_once_every_nvidia_module_is_gone(self):
        stream, redirect = self.traced()
        with patch.object(self.h, 'is_loaded', side_effect=lambda name: name in self.loaded), \
                patch.object(self.h, 'run', side_effect=self.run_that_removes(*self.h.MODULES)), \
                patch.object(self.h, 'log_removal_plan'), redirect:
            self.assertIsNone(self.h.unload_stack(self.PCI))             # no exception: the unload worked
        log = stream.getvalue()
        self.assertIn('EVERY NVIDIA MODULE IS UNLOADED', log)
        self.assertIn('Module i915 is in use', log)                       # still visible in the technical log
        self.assertNotIn('NVIDIA HOLDER', log)

    def test_a_module_that_is_really_still_loaded_is_still_a_failure(self):
        with patch.object(self.h, 'is_loaded', side_effect=lambda name: name in self.loaded), \
                patch.object(self.h, 'run', side_effect=self.run_that_removes('nvidia_drm', 'nvidia_modeset')), \
                patch.object(self.h, 'log_removal_plan'), \
                patch.object(self.h, 'nvidia_holders', return_value={}), patch.object(self.h, 'module_diagnostics', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, '(?s)Module i915 is in use.*No program was found holding'):
                self.h.unload_stack(self.PCI)
        self.assertEqual(self.loaded, {'nvidia_uvm', 'nvidia'})

    def test_other_errors_with_modules_left_behind_are_raised_unchanged(self):
        other = 'Command failed: modprobe -r nvidia\nmodprobe: ERROR: could not remove module'
        with patch.object(self.h, 'is_loaded', side_effect=lambda name: name in self.loaded), \
                patch.object(self.h, 'run', side_effect=self.run_that_removes(error=other)), \
                patch.object(self.h, 'log_removal_plan'):
            with self.assertRaises(RuntimeError) as caught:
                self.h.unload_stack(self.PCI)
        self.assertEqual(str(caught.exception), other)

    def test_nothing_is_run_when_no_nvidia_module_is_loaded(self):
        self.loaded.clear()
        with patch.object(self.h, 'is_loaded', side_effect=lambda name: name in self.loaded), \
                patch.object(self.h, 'run') as run, patch.object(self.h, 'log_removal_plan') as plan:
            self.h.unload_stack(self.PCI)
        run.assert_not_called()
        plan.assert_not_called()

    def test_removal_plan_goes_to_the_technical_log_only_when_tracing(self):
        fake = SimpleNamespace(returncode=1, stdout='rmmod nvidia_drm\nrmmod i915\n',
                               stderr='modprobe: FATAL: Module i915 is in use.\n')
        with patch.object(self.h.subprocess, 'run', return_value=fake) as call:
            self.h.log_removal_plan(['nvidia_drm', 'nvidia'])          # tracing is off: nothing runs
            call.assert_not_called()
            stream, redirect = self.traced()
            with redirect:
                self.h.log_removal_plan(['nvidia_drm', 'nvidia'])
        self.assertEqual(call.call_args.args[0], ['modprobe', '--dry-run', '--verbose', '-r', 'nvidia_drm', 'nvidia'])
        log = stream.getvalue()
        self.assertIn('TPNS_DETAIL|MODPROBE PLAN: rmmod nvidia_drm', log)
        self.assertIn('TPNS_DETAIL|MODPROBE PLAN: rmmod i915', log)
        with patch.object(self.h.subprocess, 'run', side_effect=OSError('no modprobe')):
            stream, redirect = self.traced()
            with redirect:
                self.h.log_removal_plan(['nvidia'])                    # diagnostics never break the unload
            self.assertIn('MODPROBE PLAN unavailable', stream.getvalue())


class UndervoltMissingPackageTests(unittest.TestCase):
    """Both fresh CachyOS installs showed "Expected a regular, root-owned /etc/intel-undervolt.conf file."

    Without the package there is no config file either, and the config was checked before the program,
    so the real cause ("not installed") was hidden behind a misleading message.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        import undervolt_control as uv
        self.uv = uv

    def test_missing_package_is_reported_as_such_and_nothing_is_written(self):
        conf, state = self.dir / 'intel-undervolt.conf', self.dir / 'state.json'      # neither exists
        with patch.object(self.uv, 'safe_binary', side_effect=RuntimeError(
                'intel-undervolt is not installed. Install the intel-undervolt package first.')):
            with self.assertRaisesRegex(RuntimeError, 'intel-undervolt is not installed'):
                self.uv.apply_voltages(-50, -50, 0, conf, state)
        self.assertFalse(conf.exists() or state.exists())

    def test_each_config_problem_has_its_own_message(self):
        missing = self.dir / 'missing.conf'
        with self.assertRaisesRegex(RuntimeError, r'missing\.conf does not exist.*reinstall the package'):
            self.uv.check_conf(missing)
        real = self.dir / 'real.conf'
        real.write_text('undervolt 0 "CPU" 0\n')
        link = self.dir / 'link.conf'
        link.symlink_to(real)
        with self.assertRaisesRegex(RuntimeError, r'link\.conf is a symbolic link'):
            self.uv.check_conf(link)
        folder = self.dir / 'folder.conf'
        folder.mkdir()
        with self.assertRaisesRegex(RuntimeError, r'folder\.conf is not a regular file'):
            self.uv.check_conf(folder)

    def test_binary_check_comes_before_the_config_check(self):
        calls = []
        with patch.object(self.uv, 'safe_binary', side_effect=lambda: calls.append('binary') or '/usr/bin/intel-undervolt'), \
                patch.object(self.uv, 'check_conf', side_effect=lambda *_a: calls.append('config') or (_ for _ in ()).throw(RuntimeError('x'))):
            with self.assertRaises(RuntimeError):
                self.uv.apply_voltages(-50, -50, 0, self.dir / 'c', self.dir / 's')
        self.assertEqual(calls, ['binary', 'config'])

    def test_profiles_explain_it_and_do_not_retry(self):
        import profiles
        failure = ('undervolt-set failed: intel-undervolt is not installed. Install the intel-undervolt package first.; '
                   'prior changes: cores-set. Applied: cores-set.')
        text = profiles.friendly_profile_failure(failure)
        self.assertIn('intel-undervolt package is not installed', text)
        self.assertIn('other settings were still applied', text)
        self.assertTrue(profiles.permanent_profile_failure(failure))     # installing a package is not something that fixes itself in 10 s

    @unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
    def test_buttons_are_disabled_only_while_the_package_is_missing(self):
        result, stderr = run_child('''
            from PySide6.QtCore import QProcess
            w = t.MainWindow(); w.show(); pump()
            t.log_detail = lambda text: None
            class Fake:
                def __init__(self, out, err): self.out, self.err = out, err
                def readAllStandardOutput(self): return self.out
                def readAllStandardError(self): return self.err
                def deleteLater(self): pass
            def finish(out, err, code=1):
                w.voltage_read_process = Fake(out, err)
                w._voltage_read_finished(code, QProcess.ExitStatus.NormalExit)
                return [w.uv_apply_button.isEnabled(), w.uv_reset_button.isEnabled()]
            result['initial'] = [w.uv_apply_button.isEnabled(), w.uv_reset_button.isEnabled()]
            result['missing'] = finish(b'', b'intel-undervolt is not installed. Install the intel-undervolt package first.')
            result['tooltip'] = w.uv_apply_button.toolTip()
            result['other_failure'] = finish(b'', b'sudo: a password is required')
            result['missing_again'] = finish(b'', b'intel-undervolt is not installed. Install the intel-undervolt package first.')
            good = b'{"cpu": -20.0, "cache": -20.0, "gpu": 0.0}'
            result['installed'] = finish(good, b'', 0)
            result['tooltip_after'] = w.uv_apply_button.toolTip()
        ''')
        self.assertNotIn('Traceback', stderr)
        self.assertEqual(result['initial'], [True, True])
        self.assertEqual(result['missing'], [False, False])
        self.assertIn('Install the intel-undervolt package', result['tooltip'])
        self.assertEqual(result['other_failure'], [True, True])          # a password or hardware problem must not look like a missing package
        self.assertEqual(result['missing_again'], [False, False])
        self.assertEqual(result['installed'], [True, True])
        self.assertEqual(result['tooltip_after'], '')


MODPROBE_IN_USE = ('Command failed: modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia\n'
                   'modprobe: FATAL: Module nvidia_drm is in use.')


class DriverInUseExplanationTests(unittest.TestCase):
    """Real failure from a Lenovo Y520 log: `modprobe -r` said "nvidia_drm is in use" and nothing else."""

    PCI = '0000:01:00.0'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('tpns_helper_in_use', SRC / 'optimus_helper.py')
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)

    def make_drm(self):
        """/sys/class/drm with Intel as card0 and NVIDIA as card1 / renderD129, plus a connector entry."""
        pci = self.root / 'pci'
        for address in ('0000:00:02.0', self.PCI):
            (pci / address).mkdir(parents=True)
        drm = self.root / 'drm'
        for node, owner in (('card0', '0000:00:02.0'), ('renderD128', '0000:00:02.0'),
                            ('card1', self.PCI), ('renderD129', self.PCI)):
            (drm / node).mkdir(parents=True)
            (drm / node / 'device').symlink_to(pci / owner)
        (drm / 'card1-HDMI-A-1').mkdir()
        return drm

    def make_proc(self):
        proc = self.root / 'proc'
        for pid, comm, targets in ((100, 'kwin_wayland', ['/dev/dri/card1', '/dev/dri/card0']),
                                   (101, 'nvidia-persiste', ['/dev/nvidiactl']),
                                   (102, 'plasmashell', ['/dev/dri/card0', 'socket:[123]'])):
            (proc / str(pid) / 'fd').mkdir(parents=True)
            (proc / str(pid) / 'comm').write_text(comm + '\n')
            for number, target in enumerate(targets, 3):
                (proc / str(pid) / 'fd' / str(number)).symlink_to(target)
        (proc / 'self').mkdir()
        return proc

    def test_drm_nodes_of_the_nvidia_gpu_are_found_by_pci_address(self):
        nodes = self.helper.nvidia_drm_nodes(self.PCI, drm_class=self.make_drm())
        self.assertEqual(nodes, {'/dev/dri/card1', '/dev/dri/renderD129'})

    def test_holders_include_display_nodes_not_only_dev_nvidia(self):
        holders = self.helper.nvidia_holders(self.PCI, proc=self.make_proc(), nodes={'/dev/dri/card1', '/dev/dri/renderD129'})
        self.assertEqual(holders, {100: ('kwin_wayland', ['/dev/dri/card1']),
                                   101: ('nvidia-persiste', ['/dev/nvidiactl'])})   # Intel's card0 holders are ignored
        # the old scan only looked at /dev/nvidia* and could not see the compositor
        with patch('pathlib.Path.iterdir', side_effect=OSError):
            self.assertEqual(self.helper.nvidia_holders(self.PCI, proc=self.root / 'missing', nodes=set()), {})

    def test_summary_groups_processes_by_program(self):
        holders = {10: ('Xwayland', []), 11: ('Xwayland', []), 5: ('kwin_wayland', []), 12: ('a b', [])}
        self.assertEqual(self.helper.holder_summary(holders), 'kwin_wayland (PID 5), Xwayland (PID 10, 11), a b (PID 12)')
        many = {n: (f'app{n}', []) for n in range(1, 9)}
        self.assertTrue(self.helper.holder_summary(many).endswith('and 2 more'))

    def test_unload_failure_names_what_holds_nvidia(self):
        h = self.helper
        with patch.object(h, 'is_loaded', return_value=True), patch.object(h, 'run', side_effect=RuntimeError(MODPROBE_IN_USE)), \
                patch.object(h, 'nvidia_holders', return_value={812: ('kwin_wayland', ['/dev/dri/card1'])}), \
                patch.object(h, 'module_diagnostics', return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                h.unload_stack(self.PCI)
        message = str(caught.exception)
        self.assertIn('Module nvidia_drm is in use', message)              # original text stays (other code matches it)
        self.assertIn('NVIDIA is held open by: kwin_wayland (PID 812). Close them', message)
        self.assertIn('KDE Wayland: keeping the desktop on Intel', message)

    def test_kwin_among_the_holders_gets_a_specific_instruction(self):
        h = self.helper
        with patch.object(h, 'nvidia_holders', return_value={960: ('kwin_wayland', ['/dev/dri/card0'])}), \
                patch.object(h, 'module_diagnostics', return_value=[]):
            message = h.explain_driver_in_use(self.PCI)
        self.assertIn('KWin itself holds NVIDIA: run ./install.sh from the latest release again', message)
        with patch.object(h, 'nvidia_holders', return_value={7: ('firefox', ['/dev/dri/renderD129'])}), \
                patch.object(h, 'module_diagnostics', return_value=[]):
            self.assertNotIn('KWin itself', h.explain_driver_in_use(self.PCI))
        import profiles
        failure = 'gpu-off failed: ' + MODPROBE_IN_USE + '\n' + message
        self.assertIn('the desktop session (kwin_wayland) is holding it open', profiles.friendly_profile_failure(failure))

    def test_unload_failure_without_any_holder_points_to_the_kernel_side(self):
        h = self.helper
        with patch.object(h, 'is_loaded', return_value=True), patch.object(h, 'run', side_effect=RuntimeError(MODPROBE_IN_USE)), \
                patch.object(h, 'nvidia_holders', return_value={}), patch.object(h, 'module_diagnostics', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'No program was found holding an NVIDIA device open'):
                h.unload_stack(self.PCI)

    def test_other_modprobe_failures_are_not_rewritten(self):
        h = self.helper
        other = RuntimeError('Command failed: modprobe -r nvidia\nmodprobe: ERROR: could not remove')
        with patch.object(h, 'is_loaded', return_value=True), patch.object(h, 'run', side_effect=other), \
                patch.object(h, 'nvidia_holders') as scan:
            with self.assertRaises(RuntimeError) as caught:
                h.unload_stack(self.PCI)
        self.assertIs(caught.exception, other)
        scan.assert_not_called()

    def test_every_unload_call_knows_the_gpu_address(self):
        calls = [line.strip() for line in (SRC / 'optimus_helper.py').read_text().splitlines() if 'unload_stack(' in line]
        self.assertEqual([c for c in calls if not c.startswith('def ') and c != 'unload_stack(pci)'], [])

    def test_module_state_is_logged_for_the_technical_log(self):
        base = self.root / 'module'
        (base / 'nvidia_drm' / 'holders').mkdir(parents=True)
        (base / 'nvidia_drm' / 'parameters').mkdir()
        (base / 'nvidia_drm' / 'refcnt').write_text('2\n')
        (base / 'nvidia_drm' / 'parameters' / 'modeset').write_text('Y\n')
        (base / 'nvidia_drm' / 'parameters' / 'fbdev').write_text('Y\n')
        (base / 'nvidia' / 'holders').mkdir(parents=True)
        (base / 'nvidia' / 'holders' / 'nvidia_modeset').mkdir()
        (base / 'nvidia' / 'refcnt').write_text('0\n')
        lines = self.helper.module_diagnostics(sys_module=base)
        self.assertEqual(lines, ['nvidia_drm: refcnt=2 used-by-modules=none modeset=Y fbdev=Y',
                                 'nvidia: refcnt=0 used-by-modules=nvidia_modeset'])

    def test_profile_message_names_the_holder_and_stays_in_sync_with_the_helper(self):
        import profiles
        h = self.helper
        with patch.object(h, 'is_loaded', return_value=True), patch.object(h, 'run', side_effect=RuntimeError(MODPROBE_IN_USE)), \
                patch.object(h, 'nvidia_holders', return_value={812: ('kwin_wayland', []), 900: ('Xwayland', [])}), \
                patch.object(h, 'module_diagnostics', return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                h.unload_stack(self.PCI)
        failure = f'gpu-off failed: {caught.exception}; prior changes: none.'
        friendly = profiles.friendly_profile_failure(failure)
        # names only (no process IDs), and the desktop is described as such rather than as a program to close
        self.assertIn('NVIDIA cannot be turned off because the desktop session (kwin_wayland and Xwayland) is holding it open.', friendly)
        self.assertNotIn('PID', friendly)
        self.assertIn('This is not a program you can close', friendly)
        self.assertEqual(profiles.short_profile_failure(failure),
                         'NVIDIA cannot be turned off because the desktop session (kwin_wayland and Xwayland) is holding it open.')
        # without the helper's extra line there is nothing to name
        plain = profiles.friendly_profile_failure('gpu-off failed: ' + MODPROBE_IN_USE)
        self.assertIn('no program holding it was found', plain)
        self.assertFalse(profiles.permanent_profile_failure(failure))


class RepeatedVoltageErrorTests(unittest.TestCase):
    """The log showed "Voltage read failed: intel-undervolt is not installed" every 15 seconds."""

    def test_each_distinct_problem_is_logged_once(self):
        ns = {}
        source = (SRC / 'optimus_tray.py').read_text()
        tree = ast.parse(source)
        method = next(f for c in tree.body if isinstance(c, ast.ClassDef)
                      for f in c.body if isinstance(f, ast.FunctionDef) and f.name == '_log_voltage_problem')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '<v>', 'exec'),
             {'log_detail': lambda text: logged.append(text)}, ns)
        logged = []
        owner = SimpleNamespace(last_voltage_problem=None)
        for _ in range(5):
            ns['_log_voltage_problem'](owner, 'Voltage read failed: intel-undervolt is not installed.')
        self.assertEqual(logged, ['Voltage read failed: intel-undervolt is not installed.'])
        ns['_log_voltage_problem'](owner, 'Voltage read failed: sudo: a password is required')
        owner.last_voltage_problem = None                      # a successful read resets it
        ns['_log_voltage_problem'](owner, 'Voltage read failed: sudo: a password is required')
        self.assertEqual(len(logged), 3)

    def test_success_resets_the_memory_and_missing_package_has_its_own_status(self):
        source = (SRC / 'optimus_tray.py').read_text()
        self.assertIn('self.last_voltage_problem = None', source)
        self.assertIn("the intel-undervolt package is not installed", source)

    @unittest.skipUnless(QT, 'PySide6 (and its system libraries) is not available')
    def test_real_window_logs_a_missing_package_once_and_says_so(self):
        result, stderr = run_child('''
            from PySide6.QtCore import QProcess
            w = t.MainWindow(); w.show(); pump()
            logged = []
            t.log_detail = logged.append
            class Fake:
                def __init__(self, out, err): self.out, self.err = out, err
                def readAllStandardOutput(self): return self.out
                def readAllStandardError(self): return self.err
                def deleteLater(self): pass
            missing = b'intel-undervolt is not installed. Install the intel-undervolt package first.\\n'
            for _ in range(5):
                w.voltage_read_process = Fake(b'', missing)
                w._voltage_read_finished(1, QProcess.ExitStatus.NormalExit)
            result['logged'] = len(logged)
            result['status'] = w.uv_status_label.text()
            w.voltage_read_process = Fake(b'', b'sudo: a password is required\\n')
            w._voltage_read_finished(1, QProcess.ExitStatus.NormalExit)
            result['logged_after_new_problem'] = len(logged)
            result['memory'] = w.last_voltage_problem is not None
        ''')
        self.assertNotIn('Traceback', stderr)
        self.assertEqual(result, {'logged': 1, 'logged_after_new_problem': 2, 'memory': True,
                                  'status': 'Voltage offsets unavailable (the intel-undervolt package is not installed).'})


if __name__ == '__main__':
    unittest.main()
