# SPDX-License-Identifier: MIT
"""Remember when the charger was last unplugged and say how long ago that was.

Unprivileged and free of Qt. The Linux kernel does not record when the power source changed, so the
time comes from the moment this app *saw* the change (it checks every two seconds). The record is kept
in the user's configuration directory so that restarting the app, for example after an upgrade, does
not reset the timer.

When the app did not see the unplugging itself (it was started while already on battery, or it was not
running for a while) the timer starts from the first moment it knows about and says "at least".
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

STATE_VERSION = 1
# A record this old, or from another boot, cannot tell us what happened while the app was not running.
TRUST_SECONDS = 600
# While running, the "last seen" time is written at most this often (and on every change).
HEARTBEAT_SECONDS = 60
SOURCES = ('ac', 'battery')
BOOT_ID_FILE = Path('/proc/sys/kernel/random/boot_id')


def current_boot_id(path=BOOT_ID_FILE):
    try:
        return path.read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        return ''


def format_duration(seconds):
    """3725 -> '1:02:05'; a day or more -> '2 d 3:05:00'. Never negative."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{days} d {clock}' if days else clock


def _clean(data):
    """Return a valid record from untrusted JSON, or None."""
    if not isinstance(data, dict) or data.get('version') != STATE_VERSION:
        return None

    def number(value):
        return float(value) if type(value) in (int, float) and value == value and abs(value) < 1e12 else None

    unplugged = data.get('unplugged_at')
    unplugged = None if unplugged is None else number(unplugged)
    seen = number(data.get('seen'))
    source = data.get('source')
    boot = data.get('boot_id')
    if seen is None or (data.get('unplugged_at') is not None and unplugged is None):
        return None
    return {
        'version': STATE_VERSION,
        'unplugged_at': unplugged,
        'exact': bool(data.get('exact')) if type(data.get('exact')) is bool else False,
        'source': source if source in SOURCES else None,
        'boot_id': boot if isinstance(boot, str) else '',
        'seen': seen,
    }


class UnplugTimer:
    """Tracks the last AC-to-battery change. ``update()`` is called with every confirmed power reading."""

    def __init__(self, path, boot_id=None, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.boot_id = current_boot_id() if boot_id is None else boot_id
        self.record = self._load()
        self.started = False             # becomes True with the first reading of this run
        self.last_written = 0.0
        self.write_error = None

    # -- persistence -------------------------------------------------------------------------------

    def _load(self):
        try:
            return _clean(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError, ValueError, UnicodeError):
            return None

    def _save(self):
        record = self.record
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.unplug-', suffix='.tmp',
                                                 dir=self.path.parent, delete=False) as out:
                    temp = Path(out.name)
                    json.dump(record, out)
                    out.write('\n')
                os.replace(temp, self.path)          # NamedTemporaryFile files are created 0600
                temp = None
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
            self.write_error = None
        except OSError as exc:                       # a read-only home must not break the window
            self.write_error = str(exc)
        self.last_written = self.clock()

    # -- state machine -----------------------------------------------------------------------------

    def update(self, source):
        """Feed one confirmed reading: 'ac', 'battery' or None (not detected). Returns True if it changed."""
        if source not in SOURCES:
            return False
        now = self.clock()
        record = self.record
        changed = False
        if not self.started:
            self.started = True
            trusted = (record is not None and bool(self.boot_id) and record['boot_id'] == self.boot_id
                       and 0 <= now - record['seen'] <= TRUST_SECONDS)
            if not trusted:
                # No record, another boot, or a long gap: what was recorded says nothing about what happened
                # since, so start again. On battery the unplugging was at or before now ("at least"); on the
                # charger there is nothing to report until the next unplugging is seen.
                record = {'version': STATE_VERSION, 'unplugged_at': now if source == 'battery' else None,
                          'exact': False, 'source': source, 'boot_id': self.boot_id, 'seen': now}
                changed = True
            elif record['source'] != source:
                # Seen recently by an earlier run: the change happened while the app was restarting.
                record = dict(record, source=source, seen=now)
                if source == 'battery':
                    record.update(unplugged_at=now, exact=False)
                changed = True
        elif record['source'] != source:
            record = dict(record, source=source, seen=now)
            if source == 'battery':
                record.update(unplugged_at=now, exact=True)      # seen happening
            changed = True
        self.record = record
        if changed or now - self.last_written >= HEARTBEAT_SECONDS:
            record['seen'] = now
            self._save()
        return changed

    # -- display -----------------------------------------------------------------------------------

    def describe(self, source):
        """The text for the battery card, for the current source ('ac', 'battery' or None)."""
        if source not in SOURCES:
            return 'Unplugged: power source not detected'
        record = self.record
        if record is None or record['unplugged_at'] is None:
            return 'No unplugging seen yet'
        elapsed = format_duration(self.clock() - record['unplugged_at'])
        least = '' if record['exact'] else 'at least '
        prefix = 'Unplugged' if source == 'battery' else 'Last unplugged'
        return f'{prefix} {least}{elapsed} ago'
