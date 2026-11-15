# SPDX-License-Identifier: MIT
"""Closing Xwayland to release NVIDIA graphics buffers: the process and kernel helpers (no Qt, no real processes)."""
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import xwayland_release as xr


class FakeProc(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)

    def process(self, pid, comm, stat_state='S'):
        folder = self.proc / str(pid)
        folder.mkdir()
        (folder / 'comm').write_text(comm + '\n')
        (folder / 'stat').write_text(f'{pid} ({comm}) {stat_state} 1 1 1 0 -1 4194560\n')
        return folder


class XwaylandProcessTests(FakeProc):
    def test_only_xwayland_of_this_user_is_found(self):
        self.process(100, 'Xwayland')
        self.process(101, 'Xwayland')
        self.process(102, 'kwin_wayland')
        self.process(103, 'Xwayland-wrapper')            # not the exact name
        self.process(104, 'xwayland')                    # names are case-sensitive: Xwayland
        (self.proc / 'self').mkdir()
        (self.proc / 'notapid').mkdir()
        self.assertEqual(xr.xwayland_pids(self.proc), [100, 101])
        self.assertEqual(xr.xwayland_pids(self.proc, uid=os.getuid()), [100, 101])

    def test_another_users_xwayland_is_left_alone(self):
        self.process(100, 'Xwayland')
        self.assertEqual(xr.xwayland_pids(self.proc, uid=os.getuid() + 1), [])

    def test_a_missing_or_unreadable_proc_means_none(self):
        self.assertEqual(xr.xwayland_pids(self.proc / 'missing'), [])
        folder = self.process(100, 'Xwayland')
        (folder / 'comm').unlink()                       # the process vanished while we looked
        self.assertEqual(xr.xwayland_pids(self.proc), [])

    def test_a_process_is_alive_until_it_is_gone_and_a_zombie_counts_as_gone(self):
        self.process(100, 'Xwayland', 'S')
        self.process(101, 'Xwayland', 'Z')
        self.process(102, 'Weird) name (x', 'R')         # parentheses in the name must not confuse the parser
        self.assertTrue(xr.process_alive(100, self.proc))
        self.assertFalse(xr.process_alive(101, self.proc))
        self.assertTrue(xr.process_alive(102, self.proc))
        self.assertFalse(xr.process_alive(999, self.proc))

    def test_terminate_asks_each_xwayland_to_exit_with_sigterm_only(self):
        self.process(100, 'Xwayland')
        self.process(101, 'Xwayland')
        self.process(102, 'firefox')
        sent = []

        def kill(pid, sig):
            if pid == 101:
                raise ProcessLookupError                  # it exited a moment ago
            sent.append((pid, sig))

        self.assertEqual(xr.terminate_xwayland(self.proc, kill=kill), [100])
        self.assertEqual(sent, [(100, signal.SIGTERM)])

    def test_terminate_survives_permission_errors(self):
        self.process(100, 'Xwayland')

        def kill(_pid, _sig):
            raise PermissionError

        self.assertEqual(xr.terminate_xwayland(self.proc, kill=kill), [])


class KernelAndSessionTests(FakeProc):
    def test_the_reference_count_is_read_from_the_module(self):
        path = self.proc / 'refcnt'
        path.write_text('1\n')
        self.assertEqual(xr.nvidia_drm_references(path), 1)
        path.write_text('0\n')
        self.assertEqual(xr.nvidia_drm_references(path), 0)

    def test_an_unloaded_module_or_odd_content_counts_as_zero(self):
        self.assertEqual(xr.nvidia_drm_references(self.proc / 'missing'), 0)
        path = self.proc / 'refcnt'
        path.write_text('lots\n')
        self.assertEqual(xr.nvidia_drm_references(path), 0)

    def test_closing_xwayland_is_only_offered_to_a_wayland_application_on_a_wayland_desktop(self):
        self.assertTrue(xr.restart_possible('wayland', 'wayland', [123]))
        self.assertTrue(xr.restart_possible('wayland-egl', 'Wayland', [123]))
        self.assertFalse(xr.restart_possible('xcb', 'wayland', [123]))          # this app would close itself
        self.assertFalse(xr.restart_possible('offscreen', 'wayland', [123]))
        self.assertFalse(xr.restart_possible('wayland', 'x11', [123]))
        self.assertFalse(xr.restart_possible('wayland', '', [123]))
        self.assertFalse(xr.restart_possible('wayland', 'wayland', []))        # nothing to close

    def test_the_module_only_touches_xwayland(self):
        source = (ROOT / 'src' / 'xwayland_release.py').read_text()
        self.assertIn("!= 'Xwayland'", source)
        self.assertNotIn('SIGKILL', source)
        self.assertNotIn('sudo', source)
        self.assertNotIn('shell=True', source)
        self.assertEqual(source.count("['ss', '-xpnH']"), 1)                  # the only program it runs: ss, to list X11 programs


SS_SAMPLE = """\
u_str ESTAB 0 0 /tmp/.X11-unix/X0 40123 * 38999 users:(("Xwayland",pid=1285,fd=21))
u_str ESTAB 0 0 * 38999 * 40123 users:(("steam",pid=5000,fd=12))
u_str ESTAB 0 0 @/tmp/.X11-unix/X0 40001 * 39000 users:(("Xwayland",pid=1285,fd=9))
u_str ESTAB 0 0 * 39000 * 40001 users:(("Discord",pid=6000,fd=7),("Discord",pid=6001,fd=7))
u_str ESTAB 0 0 /run/user/1000/bus 777 * 778 users:(("dbus-daemon",pid=900,fd=3))
u_str ESTAB 0 0 * 778 * 777 users:(("firefox",pid=4257,fd=4))
u_str ESTAB 0 0 * 5555 * 6666
not a socket line at all
"""


class X11ProgramListTests(unittest.TestCase):
    def test_the_programs_connected_to_xwayland_are_named(self):
        self.assertEqual(xr.parse_x11_clients(SS_SAMPLE, {1285}), ['Discord', 'steam'])    # not Firefox, not the D-Bus daemon

    def test_nothing_connected_or_another_xwayland(self):
        self.assertEqual(xr.parse_x11_clients(SS_SAMPLE, {9999}), [])
        self.assertEqual(xr.parse_x11_clients('', {1285}), [])
        self.assertEqual(xr.parse_x11_clients('garbage\n\n', {1285}), [])

    def test_the_command_is_run_without_privileges_and_failures_mean_unknown(self):
        import subprocess
        from types import SimpleNamespace
        seen = []

        def run(command, **kwargs):
            seen.append((command, kwargs.get('timeout')))
            return SimpleNamespace(returncode=0, stdout=SS_SAMPLE)

        self.assertEqual(xr.x11_client_names([1285], run=run), ['Discord', 'steam'])
        self.assertEqual(seen, [(['ss', '-xpnH'], 3)])
        self.assertIsNone(xr.x11_client_names([1285], run=lambda *a, **k: SimpleNamespace(returncode=1, stdout='')))

        def missing(*a, **k):
            raise FileNotFoundError

        def slow(*a, **k):
            raise subprocess.TimeoutExpired('ss', 3)

        self.assertIsNone(xr.x11_client_names([1285], run=missing))
        self.assertIsNone(xr.x11_client_names([1285], run=slow))


if __name__ == '__main__':
    unittest.main()
