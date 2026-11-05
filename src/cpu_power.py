#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Intel CPU topology/RAPL inspection and reversible, root-only actions.

No side effects on import; all sysfs writes originate from cpu_power_helper.py.
"""
from __future__ import annotations
import json
import os
import re
import stat
import time
from pathlib import Path

CPU_ROOT = Path('/sys/devices/system/cpu')
POWERCAP_ROOT = Path('/sys/class/powercap')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
STATE_DIR = Path('/var/lib/thinkpad-nvidia-switch')
CORES_STATE = STATE_DIR / 'cpu-offlined-cores.json'
LIMITS_STATE = STATE_DIR / 'cpu-original-limits.json'
FREQUENCY_STATE = STATE_DIR / 'cpu-original-frequency.json'
# Captured by the root-owned early-boot service, never inferred from app startup.
FIRMWARE_BOOT_LIMITS = STATE_DIR / 'cpu-firmware-boot-limits.json'
# Public, root-authored, boot-scoped status for the GUI. Never expose private CPU recovery state.
PUBLIC_FIRMWARE_BOOT_LIMITS = Path('/run/thinkpad-nvidia-switch-cpu-boot-defaults.json')
# Public, root-owned, read-only status for the unprivileged GUI. Recovery files stay private.
CORE_STATUS = Path('/run/thinkpad-nvidia-switch-cpu-cores.json')
CPU_REMEMBERED = STATE_DIR / 'cpu-remembered-settings.json'

# After writing a speed cap that reads back higher, something may still be rewriting the
# limit (another power tool, or the kernel settling after hotplug or a power-source change):
# wait briefly and write it again a few times before giving up.
# The kernel applies a new cpufreq limit from a work queue, so the value read straight
# after a write can still be the old one. Wait in growing steps; nothing is waited when
# the first read is already right.
FREQUENCY_SETTLE_DELAYS = (0.05, 0.2, 0.5, 1.0)
_pause = time.sleep  # replaced in tests
# Programs that are known to write CPU speed/performance limits (names as in /proc/<pid>/comm, 15 characters).
POWER_TOOL_PROCESSES = ('tlp', 'thermald', 'power-profiles-', 'tuned', 'auto-cpufreq', 'cpufreqd',
                        'powertop', 'org_kde_powerde', 'throttled', 'lenovo_fix', 'cpupower', 'laptop_mode')

# Explicit conservative, user-facing bounds; firmware may impose narrower limits.
PL1_RANGE = (5.0, 55.0)
PL2_RANGE = (5.0, 70.0)


def read(path, default=''):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return default


def parse_cpu_list(spec):
    result = set()
    for item in spec.split(','):
        item = item.strip()
        if not item:
            continue
        match = re.fullmatch(r'(\d+)(?:-(\d+))?', item)
        if not match:
            raise ValueError(f'Invalid CPU range: {item}')
        a, b = int(match.group(1)), int(match.group(2) or match.group(1))
        if b < a or b - a > 4096:
            raise ValueError(f'Invalid CPU range: {item}')
        result.update(range(a, b + 1))
    return sorted(result)


def _valid_saved_topology(root, present):
    """Retrieve the core map saved *before* hotplug, for this boot only.

    Linux may hide core_id/physical_package_id for offline logical CPUs. The
    immutable, root-authored snapshot avoids guessing which CPUs form cores.
    """
    try:
        record = json.loads(CORE_STATUS.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(record, dict) or record.get('boot_id') != boot_id():
        return None
    snapshot = record.get('topology')
    if not isinstance(snapshot, list):
        return None   # Pre-v2.5.1 markers contain only the offlined logical IDs.
    result, seen = {}, set()
    for entry in snapshot:
        if not (isinstance(entry, dict) and type(entry.get('package')) is int
                and type(entry.get('core')) is int and isinstance(entry.get('cpus'), list)):
            return None
        key = (entry['package'], entry['core'])
        siblings = entry['cpus']
        if (key in result or not siblings or any(type(c) is not int or c < 0 for c in siblings)
                or len(set(siblings)) != len(siblings) or seen.intersection(siblings)):
            return None
        result[key] = sorted(siblings)
        seen.update(siblings)
    return dict(sorted(result.items())) if seen == set(present) else None


def _online_topology(root, present):
    """Read only online CPUs' topology, which remains reliable during hotplug."""
    groups = {}
    for n in present:
        if not is_online(n, root):
            continue
        path = root / f'cpu{n}'
        pkg = read(path / 'topology/physical_package_id')
        core = read(path / 'topology/core_id')
        if not (pkg.isdecimal() and core.isdecimal()):
            return {}
        groups.setdefault((int(pkg), int(core)), []).append(n)
    return {key: sorted(ids) for key, ids in sorted(groups.items())}


def topology(root=None):
    root = Path(root) if root is not None else CPU_ROOT
    present = read(root / 'present')
    if not present:
        return {}
    cpus = parse_cpu_list(present)
    # The root helper captured topology while all CPUs were online. Prefer that
    # same-boot snapshot: offline CPUs can expose missing, -1 or stale IDs.
    saved = _valid_saved_topology(root, cpus)
    if saved:
        live = _online_topology(root, cpus)
        if live and all(set(ids).issubset(set(saved.get(key, []))) for key, ids in live.items()):
            return saved
    groups = {}
    all_valid = True
    for n in cpus:
        path = root / f'cpu{n}'
        if not path.exists():
            all_valid = False
            break
        pkg = read(path / 'topology/physical_package_id')
        core = read(path / 'topology/core_id')
        # On hotplug, sysfs may report -1 for offline cores.
        if not (pkg.isdecimal() and core.isdecimal()):
            all_valid = False
            break
        groups.setdefault((int(pkg), int(core)), []).append(n)
    if saved:
        # A matching-boot saved map exists, but live online CPUs disagreed.
        # Report unavailable rather than presenting an inconsistent core count.
        return {}
    if all_valid:
        return {key: sorted(ids) for key, ids in sorted(groups.items())}
    return {}

def is_online(n, root=None):
    root = Path(root) if root is not None else CPU_ROOT
    online = root / f'cpu{n}' / 'online'
    return read(online, '1') == '1'  # CPU0 may have no online attribute.


def topology_status(root=None):
    root = Path(root) if root is not None else CPU_ROOT
    groups = topology(root)
    if not groups:
        # Backwards compatibility: v2.5.0 offlined four threads before it
        # saved a topology snapshot. Only infer 4 physical cores when a
        # same-boot root helper marker confirms it disabled two *whole* cores,
        # and exactly two intact physical cores remain online (4 threads).
        try:
            present = parse_cpu_list(read(root / 'present'))
        except ValueError:
            present = []
        managed = managed_core_status()
        if (len(present) == 8 and len(managed) == 4
                and set(managed).issubset(present)
                and all(not is_online(n, root) for n in managed)):
            live = _online_topology(root, present)
            active = sorted(n for n in present if is_online(n, root))
            if (len(active) == 4 and len(live) == 2
                    and all(len(v) == 2 for v in live.values())
                    and {n for ids in live.values() for n in ids} == set(active)):
                return {'available': True, 'active_cores': 2, 'total_cores': 4,
                        'active_threads': 4, 'total_threads': 8, 'all_online': False}
        return {'available': False, 'active_cores': 0, 'total_cores': 0,
                'active_threads': 0, 'total_threads': 0, 'all_online': False}
    ids = sorted({cpu for siblings in groups.values() for cpu in siblings})
    online = {cpu: is_online(cpu, root) for cpu in ids}
    return {'available': True, 'active_cores': sum(any(online[c] for c in siblings) for siblings in groups.values()),
            'total_cores': len(groups), 'active_threads': sum(online.values()),
            'total_threads': len(ids), 'all_online': all(online.values())}

def rapl_limits(root=None):
    root = Path(root) if root is not None else POWERCAP_ROOT
    # The top-level package domain is named package-0. Never alter DRAM or core subdomains.
    candidates = sorted(set(root.glob('intel-rapl:*')) | set(root.glob('intel-rapl/intel-rapl:*')))
    for domain in candidates:
        if read(domain / 'name') != 'package-0':
            continue
        paths = {}
        for idx in range(8):
            name = read(domain / f'constraint_{idx}_name').lower().replace('-', '_')
            if name in ('long_term', 'long term'):
                paths['pl1'] = domain / f'constraint_{idx}_power_limit_uw'
            elif name in ('short_term', 'short term'):
                paths['pl2'] = domain / f'constraint_{idx}_power_limit_uw'
        if len(paths) == 2 and all(p.exists() for p in paths.values()):
            return paths
    return None


def limits_status(root=None):
    paths = rapl_limits(root)
    if paths is None:
        return {'available': False, 'pl1': None, 'pl2': None}
    try:
        values = {k: round(int(read(v)) / 1_000_000, 3) for k, v in paths.items()}
        return {'available': True, **values}
    except ValueError:
        return {'available': False, 'pl1': None, 'pl2': None}


def capture_firmware_boot_limits():
    """Capture firmware-initialized package PL1/PL2 before desktop restore.

    Only intended for the early systemd boot service. Never overwrite a record
    for this boot, or capture a supposed 'default' at ordinary app startup.
    An early-boot snapshot is a *platform boot baseline*, not proof of an
    immutable Intel factory PL2 setting.
    """
    if FIRMWARE_BOOT_LIMITS.exists():
        try:
            old = json.loads(FIRMWARE_BOOT_LIMITS.read_text())
        except (ValueError, OSError):
            raise RuntimeError('Firmware boot-limit record is corrupt; refusing to replace it.')
        if old.get('boot_id') == boot_id():
            raise RuntimeError('Boot power limits have already been captured; refusing to overwrite them.')
    paths = rapl_limits()
    if paths is None:
        raise RuntimeError('Package PL1/PL2 RAPL controls not available during early boot.')
    try:
        levels = {name: int(path.read_text().strip()) for name, path in paths.items()}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'Cannot read package power limits: {exc}') from exc
    if not (0 < levels['pl1'] <= levels['pl2'] <= 1_000_000_000):
        raise RuntimeError('Firmware boot limits are inconsistent; refusing to capture.')
    save_state(FIRMWARE_BOOT_LIMITS, {'version': 1, 'boot_id': boot_id(),
                                      'source': 'early-systemd-boot', 'limits': levels})
    # Publish a read-only, nonsecret copy for the desktop GUI. The authoritative
    # boot record remains in the private root-owned state directory.
    publish_firmware_boot_limits()
    return levels


def _validated_firmware_boot_limits(path):
    """Validate immutable boot snapshot metadata, not merely the numeric values."""
    try:
        record = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get('version') != 1
            or record.get('boot_id') != boot_id()
            or record.get('source') != 'early-systemd-boot'):
        return None
    levels = record.get('limits')
    if (not isinstance(levels, dict) or set(levels) != {'pl1', 'pl2'}
            or any(type(x) is not int for x in levels.values())
            or not 0 < levels['pl1'] <= levels['pl2'] <= 1_000_000_000):
        return None
    return levels


def firmware_boot_limits():
    """Authoritative root-owned snapshot used only by privileged operations."""
    return _validated_firmware_boot_limits(FIRMWARE_BOOT_LIMITS)


def public_firmware_boot_limits():
    """Read-only GUI view; same-boot values published by the root helper."""
    return _validated_firmware_boot_limits(PUBLIC_FIRMWARE_BOOT_LIMITS)


def publish_firmware_boot_limits():
    """Republish *only* the verified private snapshot, never live RAPL values.

    This also migrates v2.5.4 boot snapshots immediately during installation,
    without requiring an extra reboot or weakening private-file permissions.
    """
    levels = firmware_boot_limits()
    if levels is None:
        raise RuntimeError('No verified PL1/PL2 capture for this boot. Reboot with the early-boot service enabled.')
    payload = {'version': 1, 'boot_id': boot_id(),
               'source': 'early-systemd-boot', 'limits': dict(levels)}
    save_state(PUBLIC_FIRMWARE_BOOT_LIMITS, payload, public=True)
    return levels


def restore_firmware_boot_limits(root=None):
    """Restore measured platform boot PL1/PL2, never an arbitrary maximum.

    This does not restore whatever limit an unrelated utility happened to set
    immediately before this app first wrote a limit.
    """
    target = firmware_boot_limits()
    if target is None:
        raise RuntimeError('Verified platform boot PL1/PL2 unavailable. Reboot after installing '
                           'the early-boot capture service, then check its status. '
                           'Refusing to guess default power limits.')
    paths = rapl_limits(root)
    if paths is None:
        raise RuntimeError('Intel package RAPL controls unavailable.')
    try:
        before = {key: int(path.read_text().strip()) for key, path in paths.items()}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'Cannot read current PL1/PL2: {exc}') from exc
    order = ('pl1', 'pl2') if target['pl1'] <= before['pl1'] else ('pl2', 'pl1')
    try:
        for name in order:
            paths[name].write_text(str(target[name]) + '\n')
        applied = {key: int(path.read_text().strip()) for key, path in paths.items()}
        if any(abs(applied[k] - target[k]) > 500_000 for k in target):
            raise RuntimeError('Firmware rejected one or both of the saved boot limits.')
    except Exception as exc:
        errors = []
        reverse_order = ('pl1', 'pl2') if before['pl1'] <= target['pl1'] else ('pl2', 'pl1')
        for name in reverse_order:
            try:
                paths[name].write_text(str(before[name]) + '\n')
            except OSError as e:
                errors.append(f'{name}: {e}')
        raise RuntimeError(f'Could not restore platform boot limits: {exc}' +
                           ('; rollback problems: ' + ', '.join(errors) if errors else '; previous limits restored.')) from exc
    LIMITS_STATE.unlink(missing_ok=True)  # A new change must not recover stale pre-app values.
    return applied


def boot_id():
    return read(BOOT_ID)


def ensure_state_dir():
    """Create the root-owned state directory so unprivileged users can search it.

    The recovery files inside are 0600 and stay private. The directory itself
    must be searchable: the GUI and the installer test for the *existence* of
    recovery records, and Path.exists() raises PermissionError inside a 0700
    directory.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(STATE_DIR.stat().st_mode) & 0o005 != 0o005:
        # The directory must be searchable so the GUI can tell recovery records exist; the files inside are 0600.
        os.chmod(STATE_DIR, 0o755)  # nosec B103


def save_state(path, data, public=False):
    ensure_state_dir()
    path = Path(path)
    tmp = Path(str(path) + '.new')
    # Create with the final permissions: never expose a private file under the
    # process umask, even briefly.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644 if public else 0o600)
    try:
        os.fchmod(fd, 0o644 if public else 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            fd = None
            json.dump(data, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def session_state(path):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('Invalid saved state')
        if data.get('boot_id') != boot_id():
            return None  # A reboot already reset volatile CPU state.
        return data
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(f'Invalid CPU recovery record {path}: {exc}') from exc


def managed_core_status(path=None):
    """Read the root-owned nonsecret status marker, not the private recovery file."""
    path = Path(path) if path is not None else CORE_STATUS
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, dict) or data.get('boot_id') != boot_id():
        return []
    cpus = data.get('cpus', [])
    if isinstance(cpus, list) and all(type(c) is int and 0 < c < 4096 for c in cpus):
        return cpus
    return []


def _discard_stale(path):
    if path.exists() and session_state(path) is None:
        path.unlink()


def core_control_status(root=None):
    """Read-only UI bounds and the number of app-managed *physical* cores."""
    root = Path(root) if root is not None else CPU_ROOT
    groups = topology(root)
    managed_cpus = set(managed_core_status())
    if not groups:
        return {'available': False, 'disabled': 0, 'maximum': 0,
                'can_restore': bool(managed_cpus), 'reason': 'CPU topology is unavailable. Restore managed cores before changing the count.'}
    eligible = [ids for ids in groups.values() if 0 not in ids and
                all((root / f'cpu{c}' / 'online').exists() for c in ids)]
    maximum = min(len(groups)-1, len(eligible))
    owned = [ids for ids in groups.values() if set(ids).issubset(managed_cpus)]
    all_owned = {c for ids in owned for c in ids} == managed_cpus
    managed_offline = all(not is_online(c, root) for c in managed_cpus)
    external_offline = any(not is_online(c, root) for ids in groups.values() for c in ids if c not in managed_cpus)
    # Older versions recorded only logical CPUs, not the original physical topology.
    # Allow a safe Restore action, but never adjust their core count in-place.
    present = parse_cpu_list(read(root / 'present'))
    has_snapshot = not managed_cpus or bool(_valid_saved_topology(root, present))
    available = all_owned and managed_offline and not external_offline and has_snapshot
    return {'available': available, 'disabled': len(owned), 'maximum': maximum,
            'can_restore': bool(managed_cpus),
            'reason': '' if available else 'Other CPUs are offline, or the saved core layout is incomplete. Restore managed cores first.'}


def set_disabled_cores(count, root=None, state_file=None):
    """Set app-managed physical cores offline, reversibly, without touching CPU0.

    Only change complete core sibling groups, and only when all *other* CPUs
    are already online. Keep recovery records in sync with changing selections.
    A failed adjustment attempts to revert to the previous count; if rollback
    fails, an expanded recovery list permits `enable_managed_cores()` to repair.
    """
    root = Path(root) if root is not None else CPU_ROOT
    state_file = Path(state_file) if state_file is not None else CORES_STATE
    if type(count) is not int or count < 0:
        raise ValueError('Core count must be a non-negative integer.')
    if count == 0:
        return enable_managed_cores(root, state_file)
    _discard_stale(state_file)
    old = session_state(state_file)
    old_cpus = set()
    if old is not None:
        old_cpus = old.get('cpus')
        if not (isinstance(old_cpus, list) and len(set(old_cpus)) == len(old_cpus)
                and all(type(c) is int and 0 < c < 4096 for c in old_cpus)):
            raise RuntimeError('Invalid managed-core recovery record.')
        old_cpus = set(old_cpus)
        if not _valid_saved_topology(root, parse_cpu_list(read(root / 'present'))):
            raise RuntimeError('Older CPU hotplug state lacks a reliable topology snapshot. Restore all managed cores before changing the count.')
    groups = topology(root)
    if len(groups) < 2:
        raise RuntimeError('At least two reliably detected physical cores are required.')
    # These are physical core groups, not logical-thread indexes; leave the
    # bootstrap CPU0 group intact, even on non-SMT or uneven-SMT processors.
    eligible = [key for key, ids in groups.items() if 0 not in ids and
                all((root / f'cpu{c}' / 'online').exists() for c in ids)]
    maximum = min(len(groups)-1, len(eligible))
    if count > maximum:
        raise ValueError(f'Choose between 0 and {maximum} physical cores to disable.')
    # Refuse unrelated offlining, or a partial previous operation; never take
    # ownership of cores another tool disabled.
    owned_groups = {key for key, ids in groups.items() if set(ids).issubset(old_cpus)}
    if {c for key in owned_groups for c in groups[key]} != old_cpus:
        raise RuntimeError('Saved CPU list does not contain complete physical cores.')
    for ids in groups.values():
        for c in ids:
            if is_online(c, root) == (c in old_cpus):
                raise RuntimeError('Unexpected offline/online CPU outside the managed core set; refusing to change it.')
    chosen = eligible[-count:]
    target = {c for key in chosen for c in groups[key]}
    if target == old_cpus:
        return sorted(target)
    snapshot = (old or {}).get('topology')
    if snapshot is None:
        snapshot = [{'package': pkg, 'core': core, 'cpus': ids}
                    for (pkg, core), ids in groups.items()]
    expanded = old_cpus | target
    def record(cpus):
        if cpus:
            payload = {'boot_id': boot_id(), 'cpus': sorted(cpus), 'topology': snapshot}
            save_state(state_file, payload)
            save_state(CORE_STATUS, payload, public=True)
        else:
            state_file.unlink(missing_ok=True)
            CORE_STATUS.unlink(missing_ok=True)
    # Save a superset before the first write, to enable crash recovery.
    record(expanded)
    changed = []
    try:
        for c in sorted(target - old_cpus, reverse=True):
            (root / f'cpu{c}' / 'online').write_text('0\n')
            if is_online(c, root):
                raise RuntimeError(f'CPU{c} refused to go offline.')
            changed.append((c, False))
        for c in sorted(old_cpus - target):
            (root / f'cpu{c}' / 'online').write_text('1\n')
            if not is_online(c, root):
                raise RuntimeError(f'CPU{c} refused to come online.')
            changed.append((c, True))
        record(target)
    except Exception as exc:
        errors = []
        for c, made_online in reversed(changed):
            try:
                (root / f'cpu{c}' / 'online').write_text('0\n' if made_online else '1\n')
            except OSError as e:
                errors.append(f'CPU{c}: {e}')
        if not errors:
            try:
                record(old_cpus)
            except OSError as e:
                errors.append(f'recovery file: {e}')
        raise RuntimeError(f'Could not change disabled core count: {exc}. ' +
                           ('Recovery retained for manual restore: ' + '; '.join(errors) if errors else 'Previous count restored.')) from exc
    return sorted(target)


def disable_two_cores(root=None, state_file=None):
    """Compatibility entry point for pre-v2.5.2 tests/integrations."""
    return set_disabled_cores(2, root, state_file)


def enable_managed_cores(root=None, state_file=None):
    root = Path(root) if root is not None else CPU_ROOT
    state_file = Path(state_file) if state_file is not None else CORES_STATE
    state = session_state(state_file)
    if state is None:
        _discard_stale(state_file)
        CORE_STATUS.unlink(missing_ok=True)
        return []
    cpus = state.get('cpus')
    if not isinstance(cpus, list) or not cpus or not all(type(c) is int and 0 < c < 4096 for c in cpus):
        raise RuntimeError('Invalid saved CPU hotplug state; manual intervention required.')
    for cpu in sorted(set(cpus)):
        target = root / f'cpu{cpu}' / 'online'
        if not target.exists():
            raise RuntimeError(f'CPU{cpu} no longer exists. Recovery state retained.')
        if not is_online(cpu, root):
            target.write_text('1\n')
            if not is_online(cpu, root):
                raise RuntimeError(f'CPU{cpu} did not come online; recovery state retained.')
    state_file.unlink(missing_ok=True)
    CORE_STATUS.unlink(missing_ok=True)
    return sorted(set(cpus))


def set_limits(pl1, pl2, root=None, state_file=None):
    state_file = Path(state_file) if state_file is not None else LIMITS_STATE
    # Enforce a limit on maximum settings to avoid dangerous experimental values.
    if not (PL1_RANGE[0] <= pl1 <= PL1_RANGE[1] and PL2_RANGE[0] <= pl2 <= PL2_RANGE[1] and pl2 >= pl1):
        raise ValueError('Invalid limits. PL1: 5–55 W; PL2: 5–70 W; PL2 must be >= PL1.')
    paths = rapl_limits(root)
    if paths is None:
        raise RuntimeError('Intel package RAPL PL1/PL2 controls are unavailable.')
    before = {key: int(read(path)) for key, path in paths.items()}
    _discard_stale(state_file)
    fresh = session_state(state_file) is None
    if fresh:
        save_state(state_file, {'boot_id': boot_id(), 'original': before})
    target = {'pl1': round(pl1 * 1_000_000), 'pl2': round(pl2 * 1_000_000)}
    try:
        # Set PL2 before raising PL1, and lower PL1 before lowering PL2.
        order = ('pl1', 'pl2') if target['pl1'] <= before['pl1'] else ('pl2', 'pl1')
        for name in order:
            paths[name].write_text(str(target[name]) + '\n')
        # Read the values back: firmware may silently enforce different values.
        applied = {key: int(read(path)) for key, path in paths.items()}
        if any(abs(applied[k] - target[k]) > 500_000 for k in target):
            raise RuntimeError('The firmware did not accept the requested limits.')
    except Exception:
        errors = []
        for name in ('pl1', 'pl2'):
            try:
                paths[name].write_text(str(before[name]) + '\n')
            except OSError as e:
                errors.append(str(e))
        if fresh and not errors:
            state_file.unlink(missing_ok=True)
        raise RuntimeError('Could not apply both power limits. ' + ('Restore failed: ' + '; '.join(errors) if errors else 'Previous values restored.'))
    return applied


def restore_limits(root=None, state_file=None):
    state_file = Path(state_file) if state_file is not None else LIMITS_STATE
    state = session_state(state_file)
    if state is None:
        _discard_stale(state_file)
        return None
    original = state.get('original')
    if (not isinstance(original, dict) or set(original) != {'pl1', 'pl2'}
            or any(type(v) is not int or v < 0 for v in original.values())):
        raise RuntimeError('Invalid saved power-limit state.')
    paths = rapl_limits(root)
    if paths is None:
        raise RuntimeError('Intel RAPL control unavailable; original limits not restored.')
    current = {key: int(read(path)) for key, path in paths.items()}
    order = ('pl1', 'pl2') if original['pl1'] <= current['pl1'] else ('pl2', 'pl1')
    for name in order:
        paths[name].write_text(str(original[name]) + '\n')
    state_file.unlink(missing_ok=True)
    return original


# CPU frequency control is independent of the RAPL package power ceilings.
# The kernel exposes scaling_max_freq in kHz per cpufreq policy. Offline CPU
# policies may keep their directory but reject writes with EBUSY. They must be
# excluded, not treated as a failure of the requested online CPU change.
def active_frequency_policies(root=None):
    root = Path(root) if root is not None else CPU_ROOT
    result = {}
    for policy in sorted((root / 'cpufreq').glob('policy*')):
        if not re.fullmatch(r'policy[0-9]+', policy.name):
            continue
        try:
            # affected_cpus lists CPUs currently sharing an active policy.
            # If unavailable (older drivers), use related_cpus intersected
            # with the actual online CPU set.
            membership = policy / 'affected_cpus'
            if membership.exists():
                cpus = parse_cpu_list(read(membership))
            else:
                cpus = [n for n in parse_cpu_list(read(policy / 'related_cpus'))
                        if is_online(n, root)]
            if not cpus or not any(is_online(n, root) for n in cpus):
                continue  # all members offlined; accessing scaling_max_freq may raise EBUSY
            paths = {key: policy / key for key in
                     ('scaling_max_freq', 'scaling_min_freq', 'cpuinfo_min_freq', 'cpuinfo_max_freq')}
            values = {key: int(path.read_text().strip()) for key, path in paths.items()}
            if any(n <= 0 for n in values.values()):
                raise ValueError('non-positive frequency')
            result[policy.name] = {'path': paths['scaling_max_freq'], 'cpus': cpus,
                                   'max': values['scaling_max_freq'],
                                   'minimum': max(values['scaling_min_freq'], values['cpuinfo_min_freq']),
                                   'hardware_max': values['cpuinfo_max_freq']}
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'Cannot inspect active CPU frequency policy {policy.name}: {exc}') from exc
    # Do not silently leave some online CPUs unconstrained on partially
    # supported or unusual platforms. A cpufreq policy can cover many CPUs,
    # so compare the union of active memberships, not policy count.
    present_spec = read(root / 'present')
    if present_spec:
        online = {n for n in parse_cpu_list(present_spec) if is_online(n, root)}
        covered = {n for item in result.values() for n in item['cpus'] if is_online(n, root)}
        if result and not online.issubset(covered):
            missing = sorted(online - covered)
            raise RuntimeError('Cannot control CPU frequency for all online threads: '
                               + ', '.join(map(str, missing)))
    return result


def frequency_status(root=None):
    try:
        policies = active_frequency_policies(root)
        if not policies:
            raise RuntimeError('No online CPU frequency policies were found.')
        lower = max(v['minimum'] for v in policies.values())
        upper = min(v['hardware_max'] for v in policies.values())
        if lower > upper:
            raise RuntimeError('Active CPU policies have incompatible frequency ranges.')
        values = {v['max'] for v in policies.values()}
        return {'available': True, 'min_mhz': (lower + 999) // 1000,
                'max_mhz': upper // 1000,
                'current_mhz': next(iter(values)) // 1000 if len(values) == 1 else None,
                'mixed': len(values) != 1, 'reason': ''}
    except RuntimeError as exc:
        return {'available': False, 'min_mhz': 0, 'max_mhz': 0,
                'current_mhz': None, 'mixed': False, 'reason': str(exc)}


def _frequency_recovery():
    record = session_state(FREQUENCY_STATE)
    if record is None:
        _discard_stale(FREQUENCY_STATE)
        return None
    if not isinstance(record.get('original'), dict) or record.get('mode') not in ('cap', 'restore', 'defaults'):
        raise RuntimeError('Invalid CPU frequency recovery record.')
    if any(not re.fullmatch(r'policy[0-9]+', k) or type(v) is not int or v <= 0
           for k, v in record['original'].items()):
        raise RuntimeError('Invalid original CPU frequency values.')
    target = record.get('target_khz')
    if record['mode'] == 'cap' and (type(target) is not int or target <= 0):
        raise RuntimeError('Invalid saved CPU frequency cap.')
    return record


def _settle_frequency(path, wanted, accept):
    """Read a policy's maximum back, giving the kernel time to apply a fresh write.

    While the value is not acceptable, wait a little, write it again (in case another
    program replaced it meanwhile) and re-read. Returns the last value read; the caller
    decides what an unacceptable value means.
    """
    observed = int(path.read_text().strip())
    for delay in FREQUENCY_SETTLE_DELAYS:
        if accept(observed):
            break
        _pause(delay)
        path.write_text(f'{wanted}\n')
        observed = int(path.read_text().strip())
    return observed


def set_frequency_max(mhz, root=None):
    """Limit all ONLINE cpufreq policies, retaining originals for undo.

    Does not write to offline CPU policies: that was the source of the user's
    partial-success EBUSY shell error. Newly online policies are handled by
    sync_frequency_after_hotplug() when the app changes core count.
    """
    if type(mhz) is not int or mhz < 100 or mhz > 10000:
        raise ValueError('Maximum CPU frequency must be a whole number of MHz.')
    target = mhz * 1000
    root = Path(root) if root is not None else CPU_ROOT
    policies = active_frequency_policies(root)
    if not policies:
        raise RuntimeError('CPU frequency scaling controls are unavailable.')
    for name, info in policies.items():
        if not info['minimum'] <= target <= info['hardware_max']:
            raise ValueError(f'{mhz} MHz is outside the allowed range for {name} '
                             f'({info["minimum"]//1000}–{info["hardware_max"]//1000} MHz).')
    record = _frequency_recovery()
    if record and record['mode'] in ('restore', 'defaults'):
        # Finish any pending restoration before applying a new cap.
        (_restore_frequency_defaults_online(root, record) if record['mode'] == 'defaults'
         else _restore_frequency_online(root, record))
        record = _frequency_recovery()
        if record:
            raise RuntimeError('Original frequency limits pending for offline CPUs; '
                               're-enable those CPUs before applying a new cap.')
    original = dict(record['original']) if record else {}
    for name, info in policies.items():
        original.setdefault(name, info['max'])
    before = {name: info['max'] for name, info in policies.items()}
    # Save before the first write to preserve a rollback record after a crash.
    save_state(FREQUENCY_STATE, {'boot_id': boot_id(), 'mode': 'cap',
                                 'target_khz': target, 'original': original})
    changed = []
    try:
        for name, info in policies.items():
            if info['max'] != target:
                info['path'].write_text(str(target) + '\n')
                changed.append(name)
            observed = _settle_frequency(info['path'], target, lambda value: value <= target)
            if observed > target or observed < info['minimum']:
                raise RuntimeError(f'{name} rejected frequency cap ({observed} kHz read back).')
    except Exception as exc:
        errors = []
        for name in reversed(changed):
            try:
                policies[name]['path'].write_text(str(before[name]) + '\n')
            except OSError as recovery_exc:
                errors.append(f'{name}: {recovery_exc}')
        if not errors:
            if record:
                save_state(FREQUENCY_STATE, record)
            else:
                FREQUENCY_STATE.unlink(missing_ok=True)
        raise RuntimeError('CPU frequency change failed: ' + str(exc) +
                           ('; rollback failed: ' + '; '.join(errors) if errors else '; previous limits restored.')) from exc
    return {name: int(info['path'].read_text().strip()) for name, info in policies.items()}


def frequency_diagnostics(root=None, proc=Path('/proc')):
    """Read-only snapshot for the technical log when a speed cap is refused.

    Lists what the kernel reports for every cpufreq policy and which power-management
    programs are running, so a refused cap can be traced to its cause.
    """
    root = Path(root) if root is not None else CPU_ROOT
    lines = []
    policies = sorted((root / 'cpufreq').glob('policy[0-9]*'), key=lambda p: int(p.name[6:]))
    if policies:
        first = policies[0]
        lines.append(f'cpufreq driver={read(first / "scaling_driver", "n/a")} governor={read(first / "scaling_governor", "n/a")} '
                     f'epp={read(first / "energy_performance_preference", "n/a")}')
    pstate = root / 'intel_pstate'
    if pstate.is_dir():
        lines.append('intel_pstate ' + ' '.join(f'{key}={read(pstate / key, "n/a")}' for key in
                                                 ('status', 'no_turbo', 'min_perf_pct', 'max_perf_pct', 'hwp_dynamic_boost')))
    for policy in policies:
        cpus = read(policy / 'affected_cpus', '') or 'offline'
        lines.append(f'{policy.name}: cpus={cpus} min={read(policy / "scaling_min_freq", "n/a")} '
                     f'max={read(policy / "scaling_max_freq", "n/a")} hw_max={read(policy / "cpuinfo_max_freq", "n/a")}')
    found = set()
    try:
        for entry in Path(proc).iterdir():
            if entry.name.isdigit():
                comm = read(entry / 'comm')
                if comm.startswith(POWER_TOOL_PROCESSES):
                    found.add(comm)
    except OSError:
        pass
    lines.append('power tools running: ' + (', '.join(sorted(found)) if found else 'none of the known ones'))
    return lines


def _restore_frequency_online(root, record):
    """Restore only app-modified active policies; preserve offline pending ones."""
    policies = active_frequency_policies(root)
    original = dict(record['original'])
    errors = []
    for name, info in policies.items():
        if name not in original:
            continue
        try:
            # A policy's minimum might have been raised by another tool.
            if original[name] < info['minimum'] or original[name] > info['hardware_max']:
                raise RuntimeError('original frequency is outside current policy bounds')
            info['path'].write_text(str(original[name]) + '\n')
            if _settle_frequency(info['path'], original[name],
                                 lambda value: value == original[name]) != original[name]:
                raise RuntimeError('driver did not restore original maximum')
            del original[name]
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(f'{name}: {exc}')
    if original:
        save_state(FREQUENCY_STATE, {'boot_id': boot_id(), 'mode': 'restore',
                                     'target_khz': None, 'original': original})
    elif errors:
        save_state(FREQUENCY_STATE, {'boot_id': boot_id(), 'mode': 'restore',
                                     'target_khz': None, 'original': record['original']})
    else:
        FREQUENCY_STATE.unlink(missing_ok=True)
    if errors:
        raise RuntimeError('; '.join(errors))
    return sorted(original)


def restore_frequency_max(root=None):
    root = Path(root) if root is not None else CPU_ROOT
    record = _frequency_recovery()
    if not record:
        return []
    pending = _restore_frequency_online(root, record)
    return pending


def _restore_frequency_defaults_online(root, record):
    """Uncap active CPU policies to their advertised hardware maxima.

    Defer policies which the app previously capped but which are now offline.
    They will be restored when the app brings those cores back online.
    """
    policies = active_frequency_policies(root)
    if not policies:
        raise RuntimeError('No active CPU frequency policies available.')
    # Preserve the previous values for atomic rollback on unexpected sysfs errors.
    before = {name: info['max'] for name, info in policies.items()}
    changed = []
    try:
        for name, info in policies.items():
            if info['minimum'] > info['hardware_max']:
                raise RuntimeError(f'{name} has an incompatible minimum and hardware maximum.')
            if info['max'] != info['hardware_max']:
                info['path'].write_text(str(info['hardware_max']) + '\n')
                changed.append(name)
            actual = _settle_frequency(info['path'], info['hardware_max'],
                                       lambda value: value == info['hardware_max'])
            if actual != info['hardware_max']:
                raise RuntimeError(f'{name} did not accept hardware maximum '
                                   f'{info["hardware_max"]} kHz (read back {actual}). '
                                   'A global cap or disabled turbo may prevent it.')
    except Exception as exc:
        errors = []
        for name in reversed(changed):
            try:
                policies[name]['path'].write_text(str(before[name]) + '\n')
            except OSError as e:
                errors.append(f'{name}: {e}')
        raise RuntimeError(f'Could not restore hardware maximum frequencies: {exc}' +
                           ('; rollback problems: ' + ', '.join(errors) if errors else '; previous limits restored.')) from exc
    pending = set(record.get('original', {})) - set(policies)
    if pending:
        save_state(FREQUENCY_STATE, {'boot_id': boot_id(), 'mode': 'defaults',
                                    'target_khz': None,
                                    'original': {name: record['original'][name] for name in sorted(pending)}})
    else:
        FREQUENCY_STATE.unlink(missing_ok=True)
    return sorted(pending)


def restore_frequency_hardware_max(root=None):
    """Restore *hardware supported* cpufreq maximum, not last pre-app limit."""
    root = Path(root) if root is not None else CPU_ROOT
    record = _frequency_recovery()
    if record and record['mode'] == 'restore':
        raise RuntimeError('Previous frequency restoration is pending; re-enable managed cores first.')
    return _restore_frequency_defaults_online(root, record or {'original': {}})


def sync_frequency_after_hotplug(root=None):
    """After the app re-enables CPUs, apply a managed cap or pending restoration.

    This does NOT update CPU preferences; no domain may overwrite another's
    saved settings. Do nothing unless the app owns an active frequency record.
    """
    root = Path(root) if root is not None else CPU_ROOT
    record = _frequency_recovery()
    if not record:
        return
    if record['mode'] == 'restore':
        _restore_frequency_online(root, record)
    elif record['mode'] == 'defaults':
        _restore_frequency_defaults_online(root, record)
    else:
        set_frequency_max(record['target_khz'] // 1000, root)


# Remembered preferences are separate from same-boot recovery records. Updating
# one domain must NEVER silently recapture another domain's current values.
def _load_remembered():
    try:
        record = json.loads(CPU_REMEMBERED.read_text())
    except FileNotFoundError:
        return {'version': 3, 'disabled_core_count': None, 'limits': None, 'max_freq_khz': None}
    if not isinstance(record, dict) or record.get('version') not in (1, 2, 3):
        raise RuntimeError('Invalid remembered CPU preferences record.')
    if record['version'] == 1:
        legacy = record.get('cores_disabled')
        if legacy is not None and type(legacy) is not bool:
            raise RuntimeError('Invalid remembered legacy core preference.')
        count = (2 if legacy else 0) if legacy is not None else None
    else:
        count = record.get('disabled_core_count')
    if count is not None and (type(count) is not int or count < 0):
        raise RuntimeError('Invalid remembered physical core count.')
    power = record.get('limits')
    if power is not None:
        if (not isinstance(power, dict) or set(power) != {'pl1_uw', 'pl2_uw'}
                or any(type(x) is not int for x in power.values())):
            raise RuntimeError('Invalid remembered PL1/PL2 settings.')
        a, b = power['pl1_uw'], power['pl2_uw']
        if not (PL1_RANGE[0]*1e6 <= a <= PL1_RANGE[1]*1e6
                and PL2_RANGE[0]*1e6 <= b <= PL2_RANGE[1]*1e6 and b >= a):
            raise RuntimeError('Remembered power limits are out of supported bounds.')
    freq = record.get('max_freq_khz')
    if freq is not None and (type(freq) is not int or not 100000 <= freq <= 10000000):
        raise RuntimeError('Invalid remembered CPU frequency limit.')
    return {'version': 3, 'disabled_core_count': count, 'limits': power,
            'max_freq_khz': freq}


def _remember_domain(name, value):
    record = _load_remembered()
    record[name] = value
    save_state(CPU_REMEMBERED, record)
    return record


def _current_core_count():
    info = topology_status()
    managed = managed_core_status()
    if managed:
        if not all(not is_online(n) for n in managed):
            raise RuntimeError('Partially offlined CPU cores cannot be remembered.')
        control = core_control_status()
        if control['available']:
            return control['disabled']
        if info['available'] and info['total_cores'] == 4 and len(managed) == 4:
            return 2  # Legacy v2.5.0 hotplug marker.
        return None
    if info['available'] and info['all_online']:
        return 0
    return None


def _current_power_preference():
    paths = rapl_limits()
    if not paths:
        return None
    try:
        pl1, pl2 = (int(read(paths[key])) for key in ('pl1', 'pl2'))
    except ValueError:
        return None
    if (PL1_RANGE[0]*1e6 <= pl1 <= PL1_RANGE[1]*1e6
            and PL2_RANGE[0]*1e6 <= pl2 <= PL2_RANGE[1]*1e6 and pl2 >= pl1):
        return {'pl1_uw': pl1, 'pl2_uw': pl2}
    return None


def _current_frequency_preference():
    try:
        policies = active_frequency_policies()
    except RuntimeError:
        return None
    values = {v['max'] for v in policies.values()}
    return next(iter(values)) if len(values) == 1 else None


def remember_cpu_cores():
    return _remember_domain('disabled_core_count', _current_core_count())


def remember_cpu_limits():
    return _remember_domain('limits', _current_power_preference())


def remember_cpu_frequency():
    return _remember_domain('max_freq_khz', _current_frequency_preference())


def remember_current_cpu():
    """Explicit all-domain snapshot used ONLY for 'Remember and restore' on."""
    record = {'version': 3, 'disabled_core_count': _current_core_count(),
              'limits': _current_power_preference(),
              'max_freq_khz': _current_frequency_preference()}
    save_state(CPU_REMEMBERED, record)
    return record


def restore_remembered_cpu():
    if not CPU_REMEMBERED.exists():
        return 'No saved CPU preferences yet.'
    record = _load_remembered()
    desired_count = record['disabled_core_count']
    power = record['limits']
    frequency = record['max_freq_khz']
    messages, errors = [], []
    # Core changes first: active cpufreq policies can change after hotplug.
    try:
        if desired_count is not None:
            current = core_control_status()
            if desired_count == 0:
                restored = enable_managed_cores()
                messages.append(f'Restored managed CPUs {restored}.' if restored else 'Core count already correct.')
            elif current['available'] and current['disabled'] == desired_count:
                messages.append(f'{desired_count} physical cores already disabled.')
            else:
                disabled = set_disabled_cores(desired_count)
                messages.append(f'Set {desired_count} disabled physical cores (logical CPUs {disabled}).')
        else:
            messages.append('Core preference unavailable; leaving cores unchanged.')
    except Exception as exc:
        errors.append(f'cores: {exc}')
    if power is not None:
        try:
            paths = rapl_limits()
            if not paths:
                raise RuntimeError('Intel RAPL unavailable.')
            current = {key: int(read(paths[key])) for key in ('pl1', 'pl2')}
            if any(abs(current[k] - power[k+'_uw']) > 100_000 for k in ('pl1', 'pl2')):
                set_limits(power['pl1_uw']/1e6, power['pl2_uw']/1e6)
                messages.append('PL1/PL2 restored.')
            else:
                messages.append('PL1/PL2 already correct.')
        except Exception as exc:
            errors.append(f'PL1/PL2: {exc}')
    else:
        messages.append('Power-limit preference unavailable; leaving limits unchanged.')
    if frequency is not None:
        try:
            target_mhz = frequency // 1000
            current = active_frequency_policies()
            if not current:
                raise RuntimeError('CPU frequency scaling unavailable.')
            if any(v['max'] != frequency for v in current.values()):
                set_frequency_max(target_mhz)
                messages.append(f'Maximum CPU frequency restored to {target_mhz} MHz.')
            else:
                messages.append(f'Maximum CPU frequency already {target_mhz} MHz.')
        except Exception as exc:
            errors.append(f'frequency: {exc}')
    else:
        messages.append('No remembered frequency cap; leaving frequency unchanged.')
    if errors:
        raise RuntimeError('CPU settings partially restored: ' + '; '.join(errors))
    return ' '.join(messages)
