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
        self.assertNotIn('subprocess', source)
        self.assertNotIn('sudo', source)


if __name__ == '__main__':
    unittest.main()
