#!/usr/bin/python3 -Es
# SPDX-License-Identifier: MIT
"""Root-owned, narrowly restricted automatic power profile executor.

The desktop user may invoke only these fixed commands via the installed
sudoers rule. All input is validated and no shell/path/user script is executed.
"""
from __future__ import annotations
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from profiles import validate_settings

GPU_CONFIG = Path('/etc/thinkpad-nvidia-switch/gpu-pci-address')
GPU_HELPER = '/usr/local/libexec/thinkpad-nvidia-switch-helper'
CPU_HELPER = '/usr/local/libexec/thinkpad-nvidia-switch-cpu-helper'
UNDERVOLT_HELPER = '/usr/local/libexec/thinkpad-nvidia-switch-undervolt'
CPU_RETRY_DELAY_SECONDS = 1.5
# A speed cap that is still refused after its quick retry is tried again, after the other
# steps were applied, with these pauses (seconds) in between.
SPEED_LATE_RETRY_DELAYS = (3.0, 5.0)
# Prefix of the audit lines the GPU helper writes to stderr (see optimus_helper.detail()).
DETAIL_PREFIX = 'TPNS_DETAIL|'

# Where the GPU helper keeps its guards (optimus_helper.py: BASIC_CONFIG, AGGRESSIVE_CONFIG and the recovery records
# that count as an active aggressive guard). A test keeps these lists identical to the helper's own.
PCI_DEVICES = Path('/sys/bus/pci/devices')
STATE_DIR = Path('/var/lib/thinkpad-nvidia-switch')
BASIC_GUARD_FILES = (Path('/etc/modprobe.d/optimus-tray-block.conf'),)
AGGRESSIVE_GUARD_FILES = (
    Path('/etc/modprobe.d/optimus-tray-probe-guard.conf'),
    STATE_DIR / 'nvidia-modprobe-modes.json',
    STATE_DIR / 'nvidia-discovery-modes.json',
    STATE_DIR / 'pci-driver-override.json',
    STATE_DIR / 'pci-config-mask.json',
)

# sudo keeps the *caller's* PATH unless the distribution sets secure_path. These
# helpers run as root on behalf of an unprivileged user, so they must never look
# anything up in an inherited, user-controlled PATH (a planted "modprobe" would
# otherwise run as root).
SAFE_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


def decode_payload(encoded):
    if not isinstance(encoded, str) or len(encoded) > 8192 or not re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', encoded):
        raise ValueError('Invalid profile payload.')
    try:
        raw = base64.urlsafe_b64decode(encoded)
        if len(raw) > 4096:
            raise ValueError('Profile payload is too large.')
        return validate_settings(json.loads(raw.decode('utf-8')))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Malformed profile payload.') from exc


def gpu_is_asleep(gpu_address):
    """True only when the kernel reports D3cold. If the state cannot be read, say "awake": the profile then
    never switches off a GPU it was not asked to switch off."""
    try:
        return (PCI_DEVICES / gpu_address / 'power_state').read_text(encoding='utf-8').strip().lower() == 'd3cold'
    except (OSError, UnicodeError):
        return False


def guard_is_on(kind):
    files = BASIC_GUARD_FILES if kind == 'basic' else AGGRESSIVE_GUARD_FILES
    for path in files:
        try:
            if path.exists():
                return True
        except OSError:
            continue
    return False


def apply_settings(settings, gpu_address, run=subprocess.run, sleep=time.sleep, warnings=None):
    """Apply a *validated* configuration group by group and report every failure.

    No automatic rollback: PCI drivers and CPU hotplug cannot be safely rolled
    back as a transaction. Independent groups (CPU cores, CPU speed, power
    limits, voltage offsets, NVIDIA) are all attempted even if an earlier one
    failed, so one hiccup cannot leave the rest of a battery profile unapplied.
    Inside the NVIDIA group the steps depend on each other, so it stops at its
    first failure. If anything failed a RuntimeError names every failed step and
    what had already been applied. A step that succeeded but printed a "Warning:" line
    (for example: cores changed, but the old speed cap could not be re-applied) adds
    that text to ``warnings`` when a list is passed in.

    "Leave the GPU power state unchanged" really leaves it: switching a guard ON also unloads the NVIDIA
    driver and puts the GPU to sleep, so while the GPU is awake such a profile does not switch guards on
    (a guard that is already on stays on; one that was asked for but is not on is reported as a warning).
    """
    settings = validate_settings(settings)
    if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', gpu_address):
        raise ValueError('Invalid installed NVIDIA address.')
    finished = []
    failures = []

    def invoke(cmd, label):
        result = run(cmd, capture_output=True, text=True, timeout=90, check=False)
        # Forward the helper's audit lines unchanged for the technical log, but keep
        # them out of the human-readable failure reason.
        normal = []
        for line in (result.stderr or '').splitlines():
            if line.startswith(DETAIL_PREFIX):
                print(line, file=sys.stderr, flush=True)
            elif line.strip():
                normal.append(line.strip())
        if result.returncode:
            reason = ' '.join(normal) or (result.stdout or '').strip() or f'Exit code {result.returncode}'
            raise RuntimeError(f'{label} failed: {reason}; prior changes: {", ".join(finished) or "none"}.')
        finished.append(label)
        if warnings is not None:
            for line in (result.stdout or '').splitlines():
                if 'Warning:' in line:
                    warnings.append(f'{label}: {line[line.index("Warning:") + len("Warning:"):].strip()}')

    def attempt(cmd, label, retries=0):
        """Run one step; keep going on failure. CPU steps may be retried once."""
        for number in range(retries + 1):
            try:
                invoke(cmd, label)
                return True
            except RuntimeError as exc:
                if number < retries:
                    sleep(CPU_RETRY_DELAY_SECONDS)
                    continue
                failures.append(str(exc))
        return False

    def cpu(action, *args):
        # Hotplug and cpufreq can still be settling right after the previous step, or
        # another power tool may be writing at the same moment: try once more.
        attempt([CPU_HELPER, action, *map(str, args)], action, retries=1)

    # CPU controls are independent of NVIDIA. Apply them first so an NVIDIA
    # module still used by the display server or a game cannot prevent the
    # battery-saving CPU portion of a profile from being applied.
    if settings['disabled_cores'] is not None:
        cpu('cores-set', settings['disabled_cores'])
    late_speed = None
    if settings['frequency_mhz'] is not None:
        speed_command = [CPU_HELPER, 'frequency-set', str(settings['frequency_mhz'])]
        known_failures = len(failures)
        if not attempt(speed_command, 'frequency-set', retries=1):
            # Do not hold up the other steps: take the failure back and try once more at the end.
            late_speed = (speed_command, failures.pop(known_failures))
    if settings['pl1_w'] is not None:
        cpu('limits-set', f'{settings["pl1_w"]:.1f}', f'{settings["pl2_w"]:.1f}')
    if settings['undervolt_cpu_mv'] is not None:
        attempt([UNDERVOLT_HELPER, 'set', str(settings['undervolt_cpu_mv']),
                 str(settings['undervolt_cache_mv']), str(settings['undervolt_gpu_mv'])], 'undervolt-set')

    # Restore host-wide guards before requesting an active NVIDIA GPU.
    # Otherwise a driver may fail to bind because the PCI config remains masked.
    def gpu(action):
        invoke([GPU_HELPER, action, gpu_address, '--detail'], action)

    try:
        if settings['gpu_state'] == 'D0':
            gpu('aggressive-block-off')
            gpu('basic-block-off')
            gpu('gpu-on')
        else:
            if settings['gpu_state'] == 'D3cold':
                gpu('gpu-off')
            # Disable stale guards before enabling the profile's chosen combination.
            # The actions are idempotent in the underlying NVIDIA helper.
            awake = None                                    # looked at only when a guard is to be switched on
            for kind in ('aggressive', 'basic'):
                action = f'{kind}-block-on'
                if not settings[f'{kind}_guard']:
                    gpu(f'{kind}-block-off')                # never changes the power state
                    continue
                if settings['gpu_state'] == 'keep':
                    if awake is None:
                        awake = not gpu_is_asleep(gpu_address)
                    if awake:
                        # '-block-on' unloads the driver and powers the GPU off: not "unchanged". Skip it.
                        if not guard_is_on(kind):
                            if warnings is not None:
                                warnings.append(f'{action}: not applied, because it would put the NVIDIA GPU to sleep '
                                                'and this profile leaves the GPU power state unchanged (the GPU is on).')
                        continue
                gpu(action)
    except RuntimeError as exc:
        failures.append(str(exc))

    if late_speed is not None:
        speed_command, last_error = late_speed
        for delay in SPEED_LATE_RETRY_DELAYS:
            sleep(delay)
            try:
                invoke(speed_command, 'frequency-set')
                last_error = None
                break
            except RuntimeError as exc:
                last_error = str(exc)
        if last_error is not None:
            failures.append(last_error)

    if failures:
        raise RuntimeError(' '.join(failures) + f' Applied: {", ".join(finished) or "nothing"}.')
    return finished


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.geteuid() != 0:
        raise PermissionError('This helper must run as root through the installed sudoers rule.')
    os.environ['PATH'] = SAFE_PATH
    if len(argv) != 2 or argv[0] != 'apply':
        raise ValueError('Expected: apply <validated-profile-payload>.')
    settings = decode_payload(argv[1])
    gpu_address = GPU_CONFIG.read_text(encoding='utf-8').strip().lower()
    warnings = []
    applied = apply_settings(settings, gpu_address, warnings=warnings)
    print('Profile settings applied: ' + ', '.join(applied)
          + (' Warnings: ' + ' | '.join(warnings) if warnings else ''))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
