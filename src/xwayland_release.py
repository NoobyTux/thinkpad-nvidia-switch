# SPDX-License-Identifier: MIT
"""Close Xwayland so that NVIDIA graphics buffers it kept after a game are released.

Unprivileged and free of Qt. After a game, Xwayland (the layer that runs X11 programs on a Wayland desktop) can
keep a graphics buffer that NVIDIA's display driver created. Nothing has an NVIDIA device open, but that one buffer
keeps ``nvidia_drm`` loaded, so the GPU cannot be switched off. Closing Xwayland releases it; the desktop starts
Xwayland again when an X11 program is launched. Only Xwayland processes of the current user are ever touched.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

PROC = Path('/proc')
NVIDIA_DRM_REFCNT = Path('/sys/module/nvidia_drm/refcnt')


def xwayland_pids(proc=PROC, uid=None):
    """PIDs of the Xwayland processes owned by *uid* (default: the current user)."""
    uid = os.getuid() if uid is None else uid
    found = []
    try:
        entries = list(Path(proc).iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if (entry / 'comm').read_text().strip() != 'Xwayland' or entry.stat().st_uid != uid:
                continue
        except (OSError, UnicodeError):
            continue
        found.append(int(entry.name))
    return sorted(found)


def process_alive(pid, proc=PROC):
    """True while the process exists; a zombie that only waits to be collected counts as gone."""
    try:
        text = (Path(proc) / str(pid) / 'stat').read_text()
        state = text.rsplit(')', 1)[1].split()[0]
    except (OSError, UnicodeError, IndexError):
        return False
    return state not in ('Z', 'X')


def nvidia_drm_references(path=NVIDIA_DRM_REFCNT):
    """The reference count of the nvidia_drm module (0 when it is not loaded or cannot be read)."""
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError, UnicodeError):
        return 0


def restart_possible(platform_name, session_type, pids):
    """Closing Xwayland is only offered on a Wayland desktop with a running Xwayland, and only when this application
    is itself a Wayland program: an application that runs on Xwayland would close itself."""
    return bool(pids) and str(platform_name).startswith('wayland') and str(session_type).lower() == 'wayland'


def terminate_xwayland(proc=PROC, uid=None, kill=os.kill):
    """Ask every Xwayland of the current user to exit (SIGTERM, the same as ``pkill -x Xwayland``); returns the PIDs."""
    asked = []
    for pid in xwayland_pids(proc, uid):
        try:
            kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        asked.append(pid)
    return asked


_SS_LINE = re.compile(r'^\S+\s+\S+\s+\d+\s+\d+\s+(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s*(.*)$')
_SS_USER = re.compile(r'\("([^"]*)",pid=(\d+)')


def parse_x11_clients(ss_output, xwayland):
    """Names of the programs connected to Xwayland, from ``ss -xpn`` output; *xwayland* is the set of its PIDs.

    Xwayland's sockets show the inode of the program at the other end; the lines of those inodes name the programs.
    """
    peers, owners = set(), {}
    for line in ss_output.splitlines():
        match = _SS_LINE.match(line.strip())
        if not match:
            continue
        local, inode, _peer, peer_inode, users = match.groups()
        pairs = [(name, int(pid)) for name, pid in _SS_USER.findall(users)]
        if '.X11-unix' in local and any(pid in xwayland for _name, pid in pairs):
            peers.add(peer_inode)
        owners[inode] = pairs
    names = set()
    for inode in peers:
        for name, pid in owners.get(inode, []):
            if pid not in xwayland and name:
                names.add(name)
    return sorted(names, key=str.casefold)


def x11_client_names(pids, run=subprocess.run):
    """The X11 programs that are running (they close with Xwayland), or None when that cannot be found out."""
    try:
        done = run(['ss', '-xpnH'], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return parse_x11_clients(done.stdout or '', set(pids))
