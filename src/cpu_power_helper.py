#!/usr/bin/python3 -Es
# SPDX-License-Identifier: MIT
"""Restricted, root-only CPU core hotplug and Intel RAPL helper.

A single fixed command vocabulary; no shell, arbitrary sysfs paths or commands.
"""
from __future__ import annotations
import os
import sys
from cpu_power import (frequency_diagnostics, set_disabled_cores, enable_managed_cores, set_limits,
                       restore_limits, remember_current_cpu, restore_remembered_cpu,
                       remember_cpu_cores, remember_cpu_limits, remember_cpu_frequency,
                       set_frequency_max, sync_frequency_after_hotplug,
                       restore_frequency_hardware_max, restore_firmware_boot_limits,
                       capture_firmware_boot_limits, publish_firmware_boot_limits)

# sudo keeps the *caller's* PATH unless the distribution sets secure_path. These
# helpers run as root on behalf of an unprivileged user, so they must never look
# anything up in an inherited, user-controlled PATH (a planted "modprobe" would
# otherwise run as root).
SAFE_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
# Prefix of audit lines for the technical log (see optimus_helper.detail()).
DETAIL_PREFIX = 'TPNS_DETAIL|'


def report_frequency_diagnostics():
    """After a refused speed cap, record what the system looks like (technical log only)."""
    try:
        for line in frequency_diagnostics():
            print(f'{DETAIL_PREFIX}SPEED LIMIT DIAGNOSTICS: {line}', file=sys.stderr, flush=True)
    except Exception as exc:  # diagnostics must never hide the real error
        print(f'{DETAIL_PREFIX}SPEED LIMIT DIAGNOSTICS unavailable: {exc}', file=sys.stderr, flush=True)


def resync_speed_limit():
    """Re-apply a managed CPU speed cap after cores changed; never fail the core change because of it.

    The cores have already been switched by the time this runs. If the kernel then refuses
    the old cap on some policy (for example a policy that reads back its hardware maximum
    right after a core came online), report that as a warning instead of an error: the
    next speed-limit command, or a profile's own speed setting, applies a cap again.
    Returns text to append to the command's output (empty when everything is fine).
    """
    try:
        sync_frequency_after_hotplug()
    except (RuntimeError, ValueError, OSError) as exc:
        return f' Warning: the CPU speed limit could not be re-applied to every core ({exc})'
    return ''


def main(argv=None):
    if os.geteuid() != 0:
        raise PermissionError('This command requires root privileges through the installed sudoers rule.')
    os.environ['PATH'] = SAFE_PATH
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == 'cores-set':
        if not argv[1].isdecimal():
            raise ValueError('Disabled-core count must be a non-negative integer.')
        count = int(argv[1])
        if count > 4096:
            raise ValueError('Disabled-core count exceeds the allowed maximum.')
        cpus = set_disabled_cores(count)
        remember_cpu_cores()
        warning = resync_speed_limit()
        print(('All app-managed physical cores restored.' if count == 0 else
               f'{count} physical cores disabled (logical CPUs: ' + ', '.join(map(str, cpus)) + ').') + warning)
    elif argv == ['cores-off']:
        # Legacy command retained for installs/scripts made with v2.5.1.
        cpus = set_disabled_cores(2)
        remember_cpu_cores()
        warning = resync_speed_limit()
        print('Two physical CPU cores disabled (logical CPUs: ' + ', '.join(map(str, cpus)) + ').' + warning)
    elif argv == ['cores-on']:
        cpus = enable_managed_cores()
        remember_cpu_cores()
        warning = resync_speed_limit()
        print((('Restored logical CPUs: ' + ', '.join(map(str, cpus)) + '.') if cpus else 'No app-managed offline cores to restore.') + warning)
    elif len(argv) == 3 and argv[0] == 'limits-set':
        try:
            a, b = float(argv[1]), float(argv[2])
        except ValueError:
            raise ValueError('Power limits must be numeric watts.')
        result = set_limits(a, b)
        remember_cpu_limits()
        print(f'CPU power limits applied: PL1={result["pl1"] / 1e6:.1f} W; PL2={result["pl2"] / 1e6:.1f} W.')
    elif argv == ['limits-restore']:
        result = restore_firmware_boot_limits()
        remember_cpu_limits()
        print(f'Platform boot limits restored: PL1={result["pl1"]/1e6:.1f} W; PL2={result["pl2"]/1e6:.1f} W.')
    elif len(argv) == 2 and argv[0] == 'frequency-set':
        if not argv[1].isdecimal():
            raise ValueError('Frequency must be a whole number of MHz.')
        mhz = int(argv[1])
        try:
            changed = set_frequency_max(mhz)
        except Exception:
            report_frequency_diagnostics()
            raise
        remember_cpu_frequency()
        print(f'Maximum CPU frequency set to {mhz} MHz on {len(changed)} online policies. Offline policies were skipped.')
    elif argv == ['frequency-restore']:
        try:
            pending = restore_frequency_hardware_max()
        except Exception:
            report_frequency_diagnostics()
            raise
        remember_cpu_frequency()
        print(('Hardware maximum restored for online CPUs; offline app-modified policies pending: ' + ', '.join(pending))
              if pending else 'Hardware maximum frequency restored for all online policies.')
    elif argv == ['firmware-boot-capture']:
        result = capture_firmware_boot_limits()
        print(f'Captured early boot PL1={result["pl1"]/1e6:.1f} W and PL2={result["pl2"]/1e6:.1f} W.')
    elif argv == ['firmware-boot-publish']:
        result = publish_firmware_boot_limits()
        print(f'Published same-boot PL1={result["pl1"]/1e6:.1f} W and PL2={result["pl2"]/1e6:.1f} W to the GUI status file.')
    elif argv == ['remember-current']:
        state = remember_current_cpu()
        print('CPU preferences remembered: ' + str(state))
    elif argv == ['restore-saved']:
        print(restore_remembered_cpu())
    elif argv == ['uninstall-restore']:
        # Restore both, but still attempt cores if RAPL fails.
        errors = []
        try: enable_managed_cores()
        except Exception as exc: errors.append(f'CPU cores: {exc}')
        try:
            pending = restore_frequency_hardware_max()
            if pending:
                errors.append('frequency limits on offline policies still pending: ' + ', '.join(pending))
        except Exception as exc: errors.append(f'frequency: {exc}')
        try:
            restore_firmware_boot_limits()
        except Exception as exc:
            # Without a same-boot firmware capture, fall back ONLY when there
            # is an app-owned RAPL recovery record. If the app never changed
            # RAPL, there is nothing to restore and uninstall must not fail.
            from cpu_power import LIMITS_STATE, session_state
            if session_state(LIMITS_STATE) is not None:
                try:
                    restore_limits()
                except Exception as fallback_exc:
                    errors.append(f'power limits: {exc}; fallback: {fallback_exc}')
        if errors:
            raise RuntimeError('; '.join(errors))
        print('CPU cores, frequency and power-limit settings restored.')
    else:
        raise ValueError('Invalid CPU helper action or argument count.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
