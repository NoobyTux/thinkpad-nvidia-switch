# SPDX-License-Identifier: MIT
"""Validated, local power profiles. No Qt and no privileged side effects."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

PROFILE_VERSION = 2
POWER_TYPES = frozenset({'Mains', 'USB', 'USB_C', 'USB_PD', 'USB_PD_DRP', 'USB_CDP', 'USB_DCP'})


def power_source(root=Path('/sys/class/power_supply')):
    """Return 'ac', 'battery', or None when the physical supply isn't known.

    Never infer battery mode from battery discharge measurements alone. Some
    hardware provides inaccurate charging states or no battery at all.
    """
    known = []
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            typ = (entry / 'type').read_text().strip()
            if typ not in POWER_TYPES:
                continue
            online = (entry / 'online').read_text().strip()
        except OSError:
            continue
        if online in ('0', '1'):
            known.append(online == '1')
    if not known:
        return None
    return 'ac' if any(known) else 'battery'


def validate_settings(data):
    """Strict allowlist, shared by the UI and *root-owned* apply helper.

    None for CPU fields means 'leave unchanged' (helps on machines without
    Intel RAPL, or when only GPU settings should be switched).
    """
    if not isinstance(data, dict):
        raise ValueError('Profile settings must be an object.')
    expected = {'gpu_state', 'basic_guard', 'aggressive_guard', 'auto_gpu_sleep',
                'disabled_cores', 'frequency_mhz', 'pl1_w', 'pl2_w',
                'undervolt_cpu_mv', 'undervolt_cache_mv', 'undervolt_gpu_mv'}
    if set(data) != expected:
        raise ValueError('Profile settings have unknown or missing keys.')
    state = data['gpu_state']
    if state not in ('keep', 'D0', 'D3cold'):
        raise ValueError('Invalid GPU target state.')
    for key in ('basic_guard', 'aggressive_guard', 'auto_gpu_sleep'):
        if type(data[key]) is not bool:
            raise ValueError(f'{key} must be true or false.')
    if state == 'D0' and (data['basic_guard'] or data['aggressive_guard']):
        raise ValueError('A profile that turns NVIDIA on cannot also enable protection.')
    cores = data['disabled_cores']
    if cores is not None and (type(cores) is not int or cores < 0 or cores > 4096):
        raise ValueError('Invalid number of disabled physical cores.')
    mhz = data['frequency_mhz']
    if mhz is not None and (type(mhz) is not int or not 100 <= mhz <= 10000):
        raise ValueError('CPU frequency must be 100–10000 MHz.')
    a, b = data['pl1_w'], data['pl2_w']
    if (a is None) != (b is None):
        raise ValueError('PL1 and PL2 must both be set, or both left unchanged.')
    if a is not None:
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in (a, b)):
            raise ValueError('CPU power limits must be finite numbers.')
        if not (5 <= a <= 55 and 5 <= b <= 70 and b >= a):
            raise ValueError('PL1 must be 5–55 W, PL2 5–70 W, and PL2 must be >= PL1.')
    volts = (data['undervolt_cpu_mv'], data['undervolt_cache_mv'], data['undervolt_gpu_mv'])
    if any(x is not None for x in volts):
        from undervolt_control import validate_mv
        if any(x is None for x in volts):
            raise ValueError('Set all three undervolt offsets together, or leave them unchanged.')
        validate_mv(volts)
    return dict(data)


def held_open_by(details):
    """The programs named by the helper's 'NVIDIA is held open by: …' line, or ''."""
    match = re.search(r'NVIDIA is held open by: ([^\n]+?)\. Close them', str(details))
    return match.group(1) if match else ''


# Display names for programs that commonly hold NVIDIA open.
FRIENDLY_APP_NAMES = {
    'steam': 'Steam', 'steamwebhelper': 'Steam', 'steamwebhelper.sh': 'Steam',
    'firefox': 'Firefox', 'firefox-bin': 'Firefox', 'rdd process': 'Firefox', 'gpu process': 'Firefox',
    'obs': 'OBS Studio', 'obs64': 'OBS Studio', 'vlc': 'VLC', 'mpv': 'mpv',
}
# The desktop session itself: not something the user can simply close.
DESKTOP_HOLDERS = frozenset({'kwin_wayland', 'kwin_x11', 'xwayland', 'xorg', 'plasmashell', 'gnome-shell',
                             'gnome-shell-wayland', 'mutter', 'sway', 'weston', 'sddm', 'gdm', 'lightdm'})
# These only keep a copy of a device the compositor opened (logind hands devices out, systemd stores them).
PLUMBING_HOLDERS = frozenset({'systemd', 'systemd-logind', 'systemd-userdbd'})


def held_open_names(details):
    """Program names from the helper's 'held open by' line, without process IDs."""
    summary = held_open_by(details)
    names = re.findall(r'([^,()]+?) \(PID [^)]*\)', summary)
    return [name.strip().replace('?', ' ') for name in names if name.strip()]


def classify_holders(names):
    """Split program names into (applications, desktop components), tidied for display.

    Names are de-duplicated; systemd/logind entries are dropped because they only mirror the compositor.
    """
    apps, desktop = [], []
    for raw in names:
        name = str(raw).replace('?', ' ').strip()
        low = name.lower()
        if not name or low in PLUMBING_HOLDERS:
            continue
        if low in DESKTOP_HOLDERS:
            target, shown = desktop, name
        else:
            target, shown = apps, FRIENDLY_APP_NAMES.get(low, name)
        if shown.casefold() not in {item.casefold() for item in target}:
            target.append(shown)
    return apps, desktop


def _join_names(names, limit=4):
    shown = list(names[:limit])
    if len(names) > limit:
        shown.append(f'{len(names) - limit} more')
    return shown[0] if len(shown) == 1 else ', '.join(shown[:-1]) + ' and ' + shown[-1]


README_KDE_SECTION = 'README section \u201cKDE Wayland: keeping the desktop on Intel\u201d'


def explain_busy(details, extra_names=(), again='try again'):
    """Say why NVIDIA cannot be unloaded: (reason fragment, advice).

    *reason* completes "NVIDIA cannot be turned off because …". Program names come from the helper's
    'held open by' line plus *extra_names* (programs the window itself saw using NVIDIA).
    """
    apps, desktop = classify_holders([*held_open_names(details), *extra_names])
    if apps and desktop:
        return (f'it is in use by {_join_names(apps)} and by the desktop session ({_join_names(desktop)})',
                f'Close the applications first, then {again}. If it still fails, the desktop session itself is '
                f'holding NVIDIA: see the {README_KDE_SECTION}.')
    if apps:
        return (f'it is in use by {_join_names(apps)}',
                f'Close {"it" if len(apps) == 1 else "them"}, then {again}.')
    if desktop:
        return (f'the desktop session ({_join_names(desktop)}) is holding it open',
                f'This is not a program you can close. See the {README_KDE_SECTION}.')
    return ('it is still in use, but no program holding it was found',
            'The display driver itself may be holding it. See View activity log and the README troubleshooting table.')


DRIVER_MISSING_TEXT = ('The NVIDIA kernel driver is not installed for the Linux kernel that is running, so the GPU cannot be '
                       'turned on. Rebuild or reinstall the NVIDIA driver for this kernel (for example with DKMS) and reboot.')


def is_busy_failure(details):
    low = str(details).lower()
    return 'in use' in low and ('nvidia' in low or 'module' in low)


def friendly_profile_failure(details):
    """Explain expected runtime failures without exposing privileged trace spam."""
    low = str(details).lower()
    if 'not found in directory' in low:
        return DRIVER_MISSING_TEXT + ' Other settings in the profile were still applied where possible.'
    if 'intel-undervolt is not installed' in low:
        return ('The voltage offsets in this profile could not be applied because the intel-undervolt package is not '
                'installed. Install it, or turn the voltage offsets off in the profile. '
                'The other settings were still applied where possible.')
    if is_busy_failure(details):
        reason, advice = explain_busy(details, again='apply the profile again')
        return (f'NVIDIA cannot be turned off because {reason}. {advice} '
                'CPU changes earlier in the profile may already be active.')
    if 'timed out' in low or 'timeout' in low:
        return ('The profile operation took too long. Some settings may have changed. '
                'Check the activity log and verify the live CPU and GPU status.')
    return ('The profile could not be fully applied. Some settings may have changed. '
            'Check the activity log for technical details and verify the live CPU and GPU status.')


def permanent_profile_failure(details):
    """True when trying again cannot help (nothing will change by itself within seconds)."""
    low = str(details).lower()
    return 'not found in directory' in low or 'intel-undervolt is not installed' in low


def short_profile_failure(details):
    """One sentence for a notification balloon."""
    text = friendly_profile_failure(details)
    return text.split('. ')[0].rstrip('.') + '.'


def empty_store():
    return {'version': PROFILE_VERSION, 'profiles': [], 'auto_enabled': False,
            'ac_profile': None, 'battery_profile': None}


def validate_store(data):
    if not isinstance(data, dict) or data.get('version') not in (1, PROFILE_VERSION):
        raise ValueError('Unsupported profile file format.')
    legacy = data['version'] == 1
    if set(data) != set(empty_store()):
        raise ValueError('Invalid profile store fields.')
    if type(data['auto_enabled']) is not bool or not isinstance(data['profiles'], list):
        raise ValueError('Invalid profile auto-switch setting.')
    ids, names = set(), set()
    profiles = []
    for item in data['profiles']:
        if not isinstance(item, dict) or set(item) != {'id', 'name', 'settings'}:
            raise ValueError('Invalid saved profile record.')
        pid, name = item['id'], item['name']
        if not isinstance(pid, str) or not re.fullmatch(r'[0-9a-f]{32}', pid):
            raise ValueError('Invalid profile ID.')
        if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(x) < 32 for x in name):
            raise ValueError('A profile name must be 1–120 printable characters.')
        if pid in ids or name.strip().casefold() in names:
            raise ValueError('Profile names and identifiers must be unique.')
        ids.add(pid)
        names.add(name.strip().casefold())
        settings = dict(item['settings'])
        if legacy:
            settings.pop('history_window', None)
            settings.pop('history_interval', None)
            for key in ('undervolt_cpu_mv', 'undervolt_cache_mv', 'undervolt_gpu_mv'):
                settings[key] = None
        profiles.append({'id': pid, 'name': name.strip(), 'settings': validate_settings(settings)})
    for field in ('ac_profile', 'battery_profile'):
        if data[field] is not None and data[field] not in ids:
            raise ValueError(f'{field} references a profile that does not exist.')
    return {'version': PROFILE_VERSION, 'profiles': profiles,
            'auto_enabled': data['auto_enabled'],
            'ac_profile': data['ac_profile'], 'battery_profile': data['battery_profile']}


def read_store(path):
    path = Path(path)
    try:
        return validate_store(json.loads(path.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return empty_store()
    # Bad files are NOT overwritten by the caller automatically. They contain
    # the user's work and should be backed up or corrected, not discarded.


def write_store(path, data):
    data = validate_store(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.profiles-', delete=False) as out:
            tmp = Path(out.name)
            json.dump(data, out, indent=2, ensure_ascii=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def new_profile(name, settings):
    return {'id': uuid4().hex, 'name': name.strip(), 'settings': validate_settings(settings)}


def use_legacy_startup_restore(store, remember_enabled):
    """Never reapply an old CPU/GPU snapshot ahead of an automatic profile.

    A failed profile-file read (None) is treated as unsafe/unknown, rather
    than defaulting to an old hardware configuration. An empty, valid store
    allows the original remember-setting feature to operate as before.
    """
    return store is not None and not store['auto_enabled'] and bool(remember_enabled)


def assignment(store, source):
    if source not in ('ac', 'battery'):
        return None
    pid = store['ac_profile'] if source == 'ac' else store['battery_profile']
    return next((p for p in store['profiles'] if p['id'] == pid), None)
