# SPDX-License-Identifier: MIT
"""Remember when the charger was last unplugged and say how long ago that was.

Unprivileged and free of Qt. The Linux kernel does not record when the power source changed, so the
time comes from the moment this app *saw* the change (it checks every two seconds). The record is kept
in the user's configuration directory so that it survives restarting the app and also a reboot or a
power-off: the time is wall-clock time since the unplugging, so it keeps counting while the laptop is
off, as long as the laptop stayed unplugged.

Whether it stayed unplugged while the app was not running cannot be seen directly. The record therefore
keeps the source and the battery level that were last seen, and the next start decides:

* last seen on battery, now on battery: still the same unplugging, unless the battery level has risen
  clearly (it was charged, so the charger was connected in between);
* last seen on the charger, now on battery: it was unplugged while the app was not running, so the time
  starts again and says "at least";
* last seen on battery, now on the charger: it was plugged in meanwhile; the last unplugging stays;
* a record from the future (the clock went back) or older than a week is not believed.

Known limits: a charger that was connected for so short a time that the battery level did not rise by more
than the tolerance cannot be told from one that never was, and a laptop that was unplugged and plugged in
again while the app was not running shows the older unplugging when it is on the charger at the next start.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

STATE_VERSION = 1
# A record older than this is not believed: too much can have happened.
MAX_GAP_SECONDS = 7 * 86400
# A battery that reads this many percentage points higher than at the last sighting was charged in between.
# (A fuel gauge can read a point or two higher after a rest, so a small rise is not charging.)
CHARGE_TOLERANCE_PERCENT = 3
# While running, the record is rewritten at most this often (and on every change).
HEARTBEAT_SECONDS = 60
SOURCES = ('ac', 'battery')


def format_duration(seconds):
    """3725 -> '1:02:05'; a day or more -> '2 d 3:05:00'. Never negative."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    clock = f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{days} d {clock}' if days else clock


def _number(value):
    return float(value) if type(value) in (int, float) and value == value and abs(value) < 1e12 else None


def _clean(data):
    """Return a valid record from untrusted JSON, or None. Older records (without a battery level) are fine."""
    if not isinstance(data, dict) or data.get('version') != STATE_VERSION:
        return None
    unplugged = data.get('unplugged_at')
    unplugged = None if unplugged is None else _number(unplugged)
    seen = _number(data.get('seen'))
    source = data.get('source')
    if seen is None or (data.get('unplugged_at') is not None and unplugged is None):
        return None
    capacity = _number(data.get('capacity'))
    return {
        'version': STATE_VERSION,
        'unplugged_at': unplugged,
        'exact': data['exact'] if type(data.get('exact')) is bool else False,
        'source': source if source in SOURCES else None,
        'seen': seen,
        'capacity': capacity if capacity is not None and 0 <= capacity <= 100 else None,
    }


class UnplugTimer:
    """Tracks the last AC-to-battery change. ``update()`` is called with every confirmed power reading.

    *capacity* is an optional function that returns the battery level in percent (or None); it is called only
    when the record is written and when the app starts.
    """

    def __init__(self, path, clock=time.time, capacity=None):
        self.path = Path(path)
        self.clock = clock
        self.capacity = capacity
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

    def _capacity(self):
        if self.capacity is None:
            return None
        try:
            value = _number(self.capacity())
        except Exception:                # a display feature must never break the window
            return None
        return value if value is not None and 0 <= value <= 100 else None

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

    def _fresh(self, source, now, level):
        """A new record: on battery the unplugging was at or before now ("at least"); on the charger there is
        nothing to report until the next unplugging is seen."""
        return {'version': STATE_VERSION, 'unplugged_at': now if source == 'battery' else None, 'exact': False,
                'source': source, 'seen': now, 'capacity': level}

    def _first_reading(self, record, source, now, level):
        """The record of an earlier run meets the first reading of this run. Returns (record, changed)."""
        if record is None:
            return self._fresh(source, now, level), True
        gap = now - record['seen']
        if gap < 0 or gap > MAX_GAP_SECONDS:         # the clock went back, or the record is much too old
            return self._fresh(source, now, level), True
        if record['source'] == 'ac':
            if source == 'battery':                  # unplugged while the app was not running
                return self._fresh(source, now, level), True
            return dict(record, seen=now, capacity=level), False
        if source == 'battery':
            before = record.get('capacity')
            if before is not None and level is not None and level > before + CHARGE_TOLERANCE_PERCENT:
                return self._fresh(source, now, level), True      # charged meanwhile: it was plugged in
            return dict(record, seen=now, capacity=level), False  # still the same unplugging (also across a reboot)
        return dict(record, source='ac', seen=now, capacity=level), True    # plugged in meanwhile

    def update(self, source):
        """Feed one confirmed reading: 'ac', 'battery' or None (not detected). Returns True if it changed."""
        if source not in SOURCES:
            return False
        now = self.clock()
        record = self.record
        level = None
        changed = False
        if not self.started:
            self.started = True
            level = self._capacity()
            record, changed = self._first_reading(record, source, now, level)
        elif record['source'] != source:
            record = dict(record, source=source, seen=now)
            if source == 'battery':
                record.update(unplugged_at=now, exact=True)      # seen happening
            changed = True
        self.record = record
        if changed or now - self.last_written >= HEARTBEAT_SECONDS or self.last_written == 0.0:
            record['seen'] = now
            record['capacity'] = level if level is not None else self._capacity()
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
