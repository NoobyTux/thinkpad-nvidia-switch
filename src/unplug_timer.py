# SPDX-License-Identifier: MIT
"""How long the laptop has been running on battery since the charger was last unplugged.

Unprivileged and free of Qt. The Linux kernel does not record when the power source changed, so the
unplugging is the moment this app *saw* the change (it checks every two seconds). From then on the timer
adds up the time the laptop is **running** on battery. Time while it is powered off or asleep is not
counted, and while it is on the charger the timer stops and shows the length of the last time on battery.

The running time is measured with the monotonic clock, which does not advance during sleep. The record is
kept in the user's configuration directory, so the total survives restarting the app, a reboot and a
power-off. Next to the total it keeps the power source, the battery level, the boot id and the monotonic
time of its last write, and the next start decides what happened in between:

* the same boot: the time since the last write was spent running, so it is added;
* another boot: the time the laptop was off is not counted, the time since this boot started is;
* last seen on battery, now on battery: still the same unplugging, unless the battery level has risen
  clearly (it was charged, so the charger was connected in between);
* last seen on the charger, now on battery: it was unplugged while the app was not running, so the total
  starts again at 0 and says "at least";
* last seen on battery, now on the charger: it was plugged in meanwhile; the total stays;
* a record from the future (the clock went back) or older than a week is not believed.

Known limits: a charger that was connected for so short a time that the battery level did not rise by more
than the tolerance cannot be told from one that never was, and up to a minute before a power-off (the time
since the record was last written) is lost unless the app is closed normally first.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

STATE_VERSION = 2
# A record older than this is not believed: too much can have happened.
MAX_GAP_SECONDS = 7 * 86400
# A battery that reads this many percentage points higher than at the last sighting was charged in between.
# (A fuel gauge can read a point or two higher after a rest, so a small rise is not charging.)
CHARGE_TOLERANCE_PERCENT = 3
# While running, the record is rewritten at most this often (and on every change and when the app quits).
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


def _number(value):
    return float(value) if type(value) in (int, float) and value == value and abs(value) < 1e12 else None


def _clean(data):
    """Return a valid record from untrusted JSON, or None.

    Records of versions 2.7.0 to 2.10.0 (version 1) held the wall-clock time of the unplugging, which also counted the
    time the laptop was off. They are kept only as far as they are still true: the source and the battery level; the
    total starts again and says "at least".
    """
    if not isinstance(data, dict):
        return None
    seen = _number(data.get('seen'))
    if seen is None:
        return None
    source = data.get('source')
    source = source if source in SOURCES else None
    capacity = _number(data.get('capacity'))
    capacity = capacity if capacity is not None and 0 <= capacity <= 100 else None
    version = data.get('version')
    if version == 1:
        unplugged = data.get('unplugged_at')
        if unplugged is not None and _number(unplugged) is None:
            return None
        return {'version': STATE_VERSION, 'elapsed': None if unplugged is None else 0.0, 'exact': False,
                'source': source, 'seen': seen, 'uptime': None, 'boot_id': None, 'capacity': capacity}
    if version != STATE_VERSION:
        return None
    elapsed = data.get('elapsed')
    if elapsed is not None:
        elapsed = _number(elapsed)
        if elapsed is None or elapsed < 0:
            return None
    uptime = _number(data.get('uptime'))
    boot = data.get('boot_id')
    return {
        'version': STATE_VERSION,
        'elapsed': elapsed,
        'exact': data['exact'] if type(data.get('exact')) is bool else False,
        'source': source,
        'seen': seen,
        'uptime': uptime if uptime is not None and uptime >= 0 else None,
        'boot_id': boot if isinstance(boot, str) and boot else None,
        'capacity': capacity,
    }


class UnplugTimer:
    """Adds up the running time on battery. ``update()`` is called with every confirmed power reading.

    *capacity* is an optional function that returns the battery level in percent (or None); it is called only
    when the record is written and when the app starts. *clock* is the wall clock, *mono* the monotonic clock
    (no sleep time) and *boot_id* identifies the boot; they are parameters so that tests can control them.
    """

    def __init__(self, path, clock=time.time, capacity=None, mono=time.monotonic, boot_id=None):
        self.path = Path(path)
        self.clock = clock
        self.mono = mono
        self.capacity = capacity
        self.boot_id = current_boot_id() if boot_id is None else boot_id
        self.record = self._load()
        self.started = False             # becomes True with the first reading of this run
        self.last_mono = None            # monotonic time up to which the running time has been added
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

    def _fresh(self, source, now, mono, level):
        """A new record: on battery the unplugging was at or before now ("at least"); on the charger there is
        nothing to report until the next unplugging is seen."""
        return {'version': STATE_VERSION, 'elapsed': 0.0 if source == 'battery' else None, 'exact': False,
                'source': source, 'seen': now, 'uptime': mono, 'boot_id': self.boot_id or None, 'capacity': level}

    def _running_since(self, record, mono):
        """Seconds the laptop has been running since *record* was written, without sleep and without power-off."""
        before = record['boot_id']
        if not self.boot_id or before is None:
            return 0.0                               # cannot tell: add nothing
        if before == self.boot_id:
            return max(0.0, mono - record['uptime']) if record['uptime'] is not None else 0.0
        return max(0.0, mono)                        # booted since: the time off is not counted, the boot itself is

    def _first_reading(self, record, source, now, mono, level):
        """The record of an earlier run meets the first reading of this run. Returns (record, changed)."""
        if record is None or record['source'] is None:
            return self._fresh(source, now, mono, level), True
        gap = now - record['seen']
        if gap < 0 or gap > MAX_GAP_SECONDS:         # the clock went back, or the record is much too old
            return self._fresh(source, now, mono, level), True
        carried = dict(record, seen=now, uptime=mono, boot_id=self.boot_id or None, capacity=level)
        if record['source'] == 'ac':
            if source == 'battery':                  # unplugged while the app was not running
                return self._fresh(source, now, mono, level), True
            return carried, False
        if source == 'battery':
            before = record.get('capacity')
            if before is not None and level is not None and level > before + CHARGE_TOLERANCE_PERCENT:
                return self._fresh(source, now, mono, level), True      # charged meanwhile: it was plugged in
            carried['elapsed'] = (record['elapsed'] or 0.0) + self._running_since(record, mono)
            return carried, False                    # still the same unplugging, also across a reboot
        return dict(carried, source='ac'), True      # plugged in meanwhile: the total of that time on battery stays

    def _advance(self, mono):
        """Add the running time since the last call to the total, if the laptop has been on battery."""
        record = self.record
        if record is not None and self.last_mono is not None and record['source'] == 'battery' and record['elapsed'] is not None:
            record['elapsed'] += max(0.0, mono - self.last_mono)
        self.last_mono = mono

    def update(self, source):
        """Feed one confirmed reading: 'ac', 'battery' or None (not detected). Returns True if it changed."""
        if source not in SOURCES:
            return False
        now, mono = self.clock(), self.mono()
        level = None
        changed = False
        if not self.started:
            self.started = True
            level = self._capacity()
            self.record, changed = self._first_reading(self.record, source, now, mono, level)
            self.last_mono = mono
        else:
            self._advance(mono)                      # the time since the last reading belongs to the previous source
            record = self.record
            if record['source'] != source:
                record['source'] = source
                if source == 'battery':
                    record.update(elapsed=0.0, exact=True)       # seen happening: a new time on battery
                changed = True
        record = self.record
        if changed or now - self.last_written >= HEARTBEAT_SECONDS or self.last_written == 0.0:
            record.update(seen=now, uptime=mono, boot_id=self.boot_id or None,
                          capacity=level if level is not None else self._capacity())
            self._save()
        return changed

    def flush(self):
        """Bring the record up to date and write it now (the app is closing, for example at shutdown)."""
        if not self.started or self.record is None:
            return
        now, mono = self.clock(), self.mono()
        self._advance(mono)
        self.record.update(seen=now, uptime=mono, boot_id=self.boot_id or None, capacity=self._capacity())
        self._save()

    # -- display -----------------------------------------------------------------------------------

    def elapsed(self):
        """The running time on battery in seconds (live), or None when no unplugging was seen."""
        record = self.record
        if record is None or record['elapsed'] is None:
            return None
        total = record['elapsed']
        if record['source'] == 'battery' and self.last_mono is not None:
            total += max(0.0, self.mono() - self.last_mono)
        return total

    def describe(self, source):
        """The text for the battery card, for the current source ('ac', 'battery' or None)."""
        if source not in SOURCES:
            return 'On battery: power source not detected'
        total = self.elapsed()
        if total is None:
            return 'No unplugging seen yet'
        least = '' if self.record['exact'] else 'at least '
        duration = format_duration(total)
        if source == 'battery':
            return f'On battery for {least}{duration}'
        return f'Last time on battery: {least}{duration}'
