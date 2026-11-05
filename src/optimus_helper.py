#!/usr/bin/python3 -Es
# SPDX-License-Identifier: MIT
"""Restricted privileged helper for ThinkPad Nvidia Switch."""
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASIC_CONFIG = Path('/etc/modprobe.d/optimus-tray-block.conf')
AGGRESSIVE_CONFIG = Path('/etc/modprobe.d/optimus-tray-probe-guard.conf')
STATE_DIR = Path('/var/lib/thinkpad-nvidia-switch')
STATE_FILE = STATE_DIR / 'saved-state.json'
# Left by v2.2 when it removed the GPU from the PCI bus. Current releases never remove PCI devices.
LEGACY_DETACHED_STATE = STATE_DIR / 'pci-detached.json'
NVIDIA_MODPROBE_STATE = STATE_DIR / 'nvidia-modprobe-modes.json'
DISCOVERY_STATE = STATE_DIR / 'nvidia-discovery-modes.json'
DRIVER_OVERRIDE_STATE = STATE_DIR / 'pci-driver-override.json'
PCI_MASK_STATE = STATE_DIR / 'pci-config-mask.json'
MODULES = ['nvidia_drm', 'nvidia_modeset', 'nvidia_uvm', 'nvidia']
PCI_BASE = Path('/sys/bus/pci/devices')
SYS_DEVICES = Path('/sys/devices')
DRM_CLASS = Path('/sys/class/drm')
SYS_MODULE = Path('/sys/module')
PCI_CONFIG = Path('/etc/thinkpad-nvidia-switch/gpu-pci-address')
PCI_RE = re.compile(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]')
DETAIL_PREFIX = 'TPNS_DETAIL|'
DETAIL_ENABLED = False

# sudo keeps the *caller's* PATH unless the distribution sets secure_path. These
# helpers run as root on behalf of an unprivileged user, so they must never look
# anything up in an inherited, user-controlled PATH (a planted "modprobe" would
# otherwise run as root).
SAFE_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'

# NVIDIA/graphics loader metadata that can cause userspace libraries to be loaded
# merely because an application enumerates Vulkan/EGL/OpenCL implementations.
DISCOVERY_DIRS = (
    Path('/etc/vulkan/icd.d'),
    Path('/usr/share/vulkan/icd.d'),
    Path('/usr/local/share/vulkan/icd.d'),
    Path('/etc/vulkan/implicit_layer.d'),
    Path('/usr/share/vulkan/implicit_layer.d'),
    Path('/usr/local/share/vulkan/implicit_layer.d'),
    Path('/etc/glvnd/egl_vendor.d'),
    Path('/usr/share/glvnd/egl_vendor.d'),
    Path('/usr/local/share/glvnd/egl_vendor.d'),
    Path('/etc/egl/egl_external_platform.d'),
    Path('/usr/share/egl/egl_external_platform.d'),
    Path('/usr/local/share/egl/egl_external_platform.d'),
    Path('/etc/OpenCL/vendors'),
    Path('/usr/share/OpenCL/vendors'),
    Path('/usr/local/share/OpenCL/vendors'),
)

BASIC_CONFIG_TEXT = '''# Managed by ThinkPad Nvidia Switch. Do not edit while the app is running.
# Basic guard: prevent PCI modalias requests and explicit modprobe calls from loading the NVIDIA stack.
blacklist nvidia
blacklist nvidia_uvm
blacklist nvidia_modeset
blacklist nvidia_drm
install nvidia /bin/false
install nvidia_uvm /bin/false
install nvidia_modeset /bin/false
install nvidia_drm /bin/false
'''

AGGRESSIVE_CONFIG_TEXT = '''# Managed by ThinkPad Nvidia Switch. Do not edit while the app is running.
# Aggressive Steam/probe guard: also keep the NVIDIA kernel stack from autoloading.
blacklist nvidia
blacklist nvidia_uvm
blacklist nvidia_modeset
blacklist nvidia_drm
install nvidia /bin/false
install nvidia_uvm /bin/false
install nvidia_modeset /bin/false
install nvidia_drm /bin/false
'''


def detail(message):
    """Emit a structured audit line when the GUI requested detailed tracing."""
    if DETAIL_ENABLED:
        print(f'{DETAIL_PREFIX}{message}', file=sys.stderr, flush=True)


def run(args):
    command = [str(x) for x in args]
    detail(f'COMMAND START: {shlex.join(command)}')
    p = subprocess.run(command, capture_output=True, text=True)
    detail(f'COMMAND RESULT: exit={p.returncode}: {shlex.join(command)}')
    if p.stdout.strip():
        for line in p.stdout.strip().splitlines():
            detail(f'COMMAND STDOUT: {line}')
    if p.stderr.strip():
        for line in p.stderr.strip().splitlines():
            detail(f'COMMAND STDERR: {line}')
    if p.returncode:
        command_error = (p.stderr or p.stdout or f'exit status {p.returncode}').strip()
        raise RuntimeError(f"Command failed: {shlex.join(command)}\n{command_error}")
    return p.stdout.strip()


def device_path(pci):
    return PCI_BASE / pci


def is_loaded(name):
    try:
        return any(line.split()[0] == name for line in Path('/proc/modules').read_text().splitlines() if line.split())
    except OSError:
        return False


def validate_device(pci):
    device = device_path(pci)
    detail(f'DEVICE VALIDATE: {device}')
    try:
        vendor_path = device / 'vendor'
        class_path = device / 'class'
        vendor = vendor_path.read_text().strip().lower()
        cls = class_path.read_text().strip().lower()
        detail(f'FILE READ: {vendor_path} -> {vendor}')
        detail(f'FILE READ: {class_path} -> {cls}')
    except OSError:
        if LEGACY_DETACHED_STATE.exists():
            raise RuntimeError(
                f'Configured NVIDIA PCI device {pci} is still absent because v2.2 removed it from the PCI bus. '
                'The tested T440p did not rediscover it with PCI rescan. Reboot once; current releases do not use PCI removal.'
            )
        raise RuntimeError(f'Configured NVIDIA PCI device {pci} is not present.')
    if vendor != '0x10de' or not re.fullmatch(r'0x03[0-9a-f]{4}', cls):
        raise RuntimeError(f'Configured PCI device {pci} is not an NVIDIA display controller.')
    # If a reboot restored a device that v2.2 had detached, the old marker is now stale.
    if LEGACY_DETACHED_STATE.exists():
        detail(f'FILE REMOVE: {LEGACY_DETACHED_STATE} (stale legacy PCI-detach marker)')
    LEGACY_DETACHED_STATE.unlink(missing_ok=True)


def set_power_auto(pci):
    control = device_path(pci) / 'power' / 'control'
    if not control.exists():
        raise RuntimeError(f'PCI power control not found: {control}')
    detail(f'FILE WRITE: {control} <- auto (enable PCI runtime power management)')
    control.write_text('auto')


def wait_for_state(pci, expected, timeout=8.0):
    state_file = device_path(pci) / 'power_state'
    deadline = time.monotonic() + timeout
    state = 'Unknown'
    detail(f'WAIT: {state_file} -> {expected} (timeout {timeout:.1f}s)')
    while time.monotonic() < deadline:
        try:
            state = state_file.read_text().strip()
        except OSError:
            state = 'Unknown'
        if state.lower() == expected.lower():
            detail(f'WAIT COMPLETE: {state_file} -> {state}')
            return state
        time.sleep(0.25)
    detail(f'WAIT TIMEOUT: {state_file} last state={state}')
    return state


def power_off(pci):
    set_power_auto(pci)
    state = wait_for_state(pci, 'D3cold')
    if state.lower() != 'd3cold':
        raise RuntimeError(
            f'NVIDIA modules unloaded and runtime power saving requested, but the GPU state is still {state} (expected D3cold). '
            'The device may need more time or may not be entering D3cold. Wait a little and retry; if needed, log out and back in.'
        )
    return state


def write_basic_block():
    detail(f'FILE WRITE: {BASIC_CONFIG} (basic modprobe block rules)')
    BASIC_CONFIG.write_text(BASIC_CONFIG_TEXT)


def remove_basic_block():
    if BASIC_CONFIG.exists():
        detail(f'FILE REMOVE: {BASIC_CONFIG} (disable basic modprobe block)')
    BASIC_CONFIG.unlink(missing_ok=True)


def write_aggressive_module_block():
    detail(f'FILE WRITE: {AGGRESSIVE_CONFIG} (aggressive probe-guard modprobe rules)')
    AGGRESSIVE_CONFIG.write_text(AGGRESSIVE_CONFIG_TEXT)


def remove_aggressive_module_block():
    if AGGRESSIVE_CONFIG.exists():
        detail(f'FILE REMOVE: {AGGRESSIVE_CONFIG} (disable aggressive modprobe block)')
    AGGRESSIVE_CONFIG.unlink(missing_ok=True)


def basic_blocked():
    return BASIC_CONFIG.exists()


def aggressive_blocked():
    # Treat recovery records as an active/incomplete aggressive guard too. This
    # prevents the UI/state restore logic from silently ignoring permissions
    # that still need to be restored after an interrupted operation.
    return (AGGRESSIVE_CONFIG.exists() or NVIDIA_MODPROBE_STATE.exists()
            or DISCOVERY_STATE.exists() or DRIVER_OVERRIDE_STATE.exists()
            or PCI_MASK_STATE.exists())


def _load_mode_state(path):
    if not path.exists():
        return {}
    try:
        detail(f'FILE READ: {path} (permission/state recovery data)')
        raw = json.loads(path.read_text())
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f'Cannot read guard state {path}: {exc}')
    if not isinstance(raw, dict):
        raise RuntimeError(f'Cannot read guard state {path}: invalid state file.')
    out = {}
    for name, mode in raw.items():
        try:
            out[str(name)] = int(mode)
        except (ValueError, TypeError):
            raise RuntimeError(f'Cannot read guard state {path}: invalid mode for {name}.')
    return out


def _save_json_atomic(path, data):
    """Durably record original system state before changing installed files.

    A partial/truncated JSON state file would make recovery impossible. The
    temporary file lives in the same root-owned directory for atomic replace.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the directory searchable (files inside are 0600): the unprivileged GUI
    # checks which recovery records exist, and cpu_power.ensure_state_dir() agrees.
    if stat.S_IMODE(STATE_DIR.stat().st_mode) & 0o005 != 0o005:
        # The directory must be searchable so the GUI can tell recovery records exist; the files inside are 0600.
        os.chmod(STATE_DIR, 0o755)  # nosec B103
    detail(f'DIRECTORY ENSURE: {STATE_DIR}')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', prefix=f'.{path.name}.',
            dir=STATE_DIR, delete=False,
        ) as fh:
            temporary = Path(fh.name)
            detail(f'FILE CREATE: {temporary} (temporary atomic state file)')
            os.fchmod(fh.fileno(), 0o600)
            detail(f'PERMISSION CHANGE: {temporary} -> 0600')
            fh.write(json.dumps(data, indent=2, sort_keys=True) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
        detail(f'FILE REPLACE: {temporary} -> {path} (atomic state update)')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            if temporary.exists():
                detail(f'FILE REMOVE: {temporary} (temporary cleanup)')
            temporary.unlink(missing_ok=True)


def _save_mode_state(path, data):
    _save_json_atomic(path, data)


def _restore_modes(path):
    if not path.exists():
        return
    saved = _load_mode_state(path)
    errors = []
    for name, mode in saved.items():
        target = Path(name)
        if not target.exists():
            continue
        try:
            detail(f'PERMISSION RESTORE: {target} -> {mode:#06o}')
            os.chmod(target, mode)
        except OSError as exc:
            errors.append(f'{target}: {exc}')
    if errors:
        raise RuntimeError('Could not restore guarded file permissions: ' + '; '.join(errors))
    detail(f'FILE REMOVE: {path} (permission recovery state no longer needed)')
    path.unlink(missing_ok=True)


def _nvidia_modprobe_paths():
    candidates = (
        '/usr/bin/nvidia-modprobe', '/usr/local/bin/nvidia-modprobe', '/usr/sbin/nvidia-modprobe',
        '/bin/nvidia-modprobe', '/sbin/nvidia-modprobe',
    )
    paths, seen = [], set()
    for candidate in candidates:
        path = Path(candidate)
        try:
            if not path.exists():
                continue
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.is_file():
            continue
        seen.add(key)
        paths.append(resolved)
    return paths


def disable_nvidia_modprobe():
    saved = _load_mode_state(NVIDIA_MODPROBE_STATE)
    for path in _nvidia_modprobe_paths():
        current = stat.S_IMODE(path.stat().st_mode)
        key = str(path)
        if key not in saved:
            saved[key] = current
            # Persist the recovery record BEFORE modifying this file. In the
            # event of interruption, the next startup/uninstall can recover.
            _save_mode_state(NVIDIA_MODPROBE_STATE, saved)
        if current & 0o111:
            new_mode = current & ~0o111
            detail(f'PERMISSION CHANGE: {path} {current:#06o} -> {new_mode:#06o} (disable nvidia-modprobe execution)')
            os.chmod(path, new_mode)


def restore_nvidia_modprobe():
    _restore_modes(NVIDIA_MODPROBE_STATE)


def _looks_like_nvidia_manifest(path):
    lname = path.name.lower()
    if 'nvidia' in lname:
        return True
    try:
        # Loader metadata files are tiny. Limit the read anyway.
        with path.open('rb') as fh:
            data = fh.read(131072).lower()
        return b'nvidia' in data
    except OSError:
        return False


def _nvidia_discovery_files():
    found, seen = [], set()
    for directory in DISCOVERY_DIRS:
        if not directory.is_dir():
            continue
        for pattern in ('*.json', '*.icd'):
            for entry in directory.glob(pattern):
                try:
                    resolved = entry.resolve(strict=True)
                except OSError:
                    continue
                key = str(resolved)
                if key in seen or not resolved.is_file():
                    continue
                if not _looks_like_nvidia_manifest(resolved):
                    continue
                seen.add(key)
                found.append(resolved)
    result = sorted(found, key=lambda p: str(p))
    detail('DISCOVERY SCAN: NVIDIA loader metadata: ' + (', '.join(map(str, result)) if result else 'none found'))
    return result


def disable_nvidia_discovery():
    """Make NVIDIA Vulkan/EGL/OpenCL loader manifests unreadable to normal apps.

    This keeps the PCI function present and recoverable while preventing ordinary
    loader-based probes (notably Steam's graphics capability discovery) from
    loading NVIDIA userspace libraries solely to enumerate the sleeping dGPU.
    Exact original modes are restored when the block is disabled.
    """
    saved = _load_mode_state(DISCOVERY_STATE)
    for path in _nvidia_discovery_files():
        current = stat.S_IMODE(path.stat().st_mode)
        key = str(path)
        if key not in saved:
            saved[key] = current
            _save_mode_state(DISCOVERY_STATE, saved)
        if current != 0:
            detail(f'PERMISSION CHANGE: {path} {current:#06o} -> 0o0000 (hide NVIDIA loader metadata from userspace probes)')
            os.chmod(path, 0)


def restore_nvidia_discovery():
    _restore_modes(DISCOVERY_STATE)


def _read_driver_override(path):
    # Kernel sysfs may render a NULL driver_override as '(null)'.
    value = path.read_text().strip()
    return '' if value in ('', '(null)') else value


def protect_pci_driver_binding(pci):
    """Prevent a fresh kernel driver bind while Aggressive is selected.

    Keep PCI enumerated: unlike PCI remove/rescan, this only changes the
    kernel's binding-match policy. It is NOT a PCI power-on prohibition.
    Persist the prior override first so a crash/uninstall can restore it.
    """
    override = device_path(pci) / 'driver_override'
    if not override.exists():
        raise RuntimeError(f'PCI driver_override is not supported at {override}')
    current = _read_driver_override(override)
    if DRIVER_OVERRIDE_STATE.exists():
        data = json.loads(DRIVER_OVERRIDE_STATE.read_text())
        if data.get('pci') != pci or not isinstance(data.get('original'), str):
            raise RuntimeError('PCI override recovery state does not match the configured GPU.')
        if current not in ('none', data['original']):
            raise RuntimeError(f'Unexpected PCI driver override {current!r}; refusing to overwrite it.')
    else:
        if current not in ('', 'none'):
            raise RuntimeError(f'PCI driver binding is already managed by another tool ({current!r}).')
        # If a user independently configured "none", don't take ownership of it.
        if current == 'none':
            detail('PCI DRIVER OVERRIDE: pre-existing none; left unchanged (not app-owned)')
            return
        _save_json_atomic(DRIVER_OVERRIDE_STATE, {'pci': pci, 'original': current})
    detail(f'FILE WRITE: {override} <- none (refuse future driver binding)')
    override.write_text('none\n')


def restore_pci_driver_binding(pci):
    """Restore only the exact override that this application changed."""
    if not DRIVER_OVERRIDE_STATE.exists():
        return
    data = json.loads(DRIVER_OVERRIDE_STATE.read_text())
    if data.get('pci') != pci or not isinstance(data.get('original'), str):
        raise RuntimeError('Invalid PCI driver override recovery state; refusing unsafe restore.')
    override = device_path(pci) / 'driver_override'
    if override.exists():
        current = _read_driver_override(override)
        if current not in ('none', data['original']):
            raise RuntimeError(f'PCI driver override was changed by another tool ({current!r}); leaving recovery data.')
        detail(f'FILE WRITE: {override} <- {data["original"]!r} (restore original driver binding)')
        override.write_text(data['original'] + '\n')
    else:
        # The sysfs setting is volatile: a missing PCI device cannot retain it.
        detail(f'PCI DRIVER OVERRIDE: {override} absent; no binding policy to restore')
    DRIVER_OVERRIDE_STATE.unlink()



def _pci_config_target(pci):
    """Resolve the device sysfs symlink; never mount over the alias path."""
    device = device_path(pci)
    target = (device / 'config').resolve(strict=True)
    sys_devices = SYS_DEVICES.resolve()
    # Reject a changed/compromised path or an unexpected non-file endpoint.
    if not target.is_relative_to(sys_devices) or target.name != 'config':
        raise RuntimeError(f'Refusing to mask unexpected PCI config path: {target}')
    if not target.is_file():
        # Our own active /dev/null bind mount exposes a character device here.
        # A repeated Aggressive-on action must be idempotent.
        if not (PCI_MASK_STATE.exists() and _null_mask_active(target)):
            raise RuntimeError(f'PCI config endpoint is not a regular file: {target}')
    return target


def _mountpoint_is_exact(target):
    """Check exact mountpoint using mountinfo, not mountpoint(1) on sysfs."""
    try:
        # The target path contains only safe PCI/sysfs characters, not spaces.
        return any(line.split()[4] == str(target)
                   for line in Path('/proc/self/mountinfo').read_text().splitlines()
                   if len(line.split()) >= 5)
    except OSError as exc:
        raise RuntimeError(f'Cannot inspect mount namespaces: {exc}')


def _null_mask_active(target):
    if not _mountpoint_is_exact(target):
        return False
    try:
        info = target.stat()
        reference = Path('/dev/null').stat()
        return (stat.S_ISCHR(info.st_mode) and info.st_rdev == reference.st_rdev)
    except OSError:
        return False


def enable_global_pci_config_mask(pci):
    """Mask config reads across this mount namespace, not the PCI function.

    Root-managed mount remains after the GUI exits. Unlike a process-local bwrap
    mask, apps launched normally inherit this mount from the host namespace.
    Do NOT remove the device from PCI or write to kernel power management here.
    """
    target = _pci_config_target(pci)
    if PCI_MASK_STATE.exists():
        try:
            record = json.loads(PCI_MASK_STATE.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'PCI mask recovery data is invalid: {exc}')
        if record != {'pci': pci, 'target': str(target), 'source': '/dev/null'}:
            raise RuntimeError('PCI mask recovery data refers to another device; refusing to overwrite.')
        if _null_mask_active(target):
            detail(f'PCI MASK: verified already active at {target}')
            return
        if _mountpoint_is_exact(target):
            raise RuntimeError(f'PCI config was mounted by another tool: {target}')
        # Stale saved record after a reboot: the mount did not survive reboot.
    elif _mountpoint_is_exact(target):
        raise RuntimeError(f'PCI config already has a mountpoint managed elsewhere: {target}')

    _save_json_atomic(PCI_MASK_STATE, {'pci': pci, 'target': str(target), 'source': '/dev/null'})
    try:
        run(['/usr/bin/mount', '--bind', '/dev/null', str(target)])
        if not _null_mask_active(target):
            raise RuntimeError(f'PCI config masking did not take effect: {target}')
        detail(f'PCI MASK ENABLED: /dev/null -> {target} (host mount namespace; no PCI detach)')
    except Exception:
        # If mount succeeded but validation/confirmation failed, don't leave
        # an untracked mount. Leave the recovery file if cleanup fails.
        if _null_mask_active(target):
            run(['/usr/bin/umount', str(target)])
        # If an unexpected mount replaced our target, never discard recovery data.
        if not _mountpoint_is_exact(target):
            PCI_MASK_STATE.unlink(missing_ok=True)
        raise


def disable_global_pci_config_mask(pci):
    """Unmask only the app-owned mount, never an unrelated existing mount."""
    if not PCI_MASK_STATE.exists():
        return
    try:
        record = json.loads(PCI_MASK_STATE.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'Cannot restore PCI config mask without recovery data: {exc}')
    if record.get('pci') != pci or record.get('source') != '/dev/null':
        raise RuntimeError('PCI mask recovery data does not match GPU; manual recovery required.')
    target = Path(record.get('target', ''))
    if (target.name != 'config' or not target.is_relative_to(SYS_DEVICES)
            or not PCI_RE.fullmatch(pci)):
        raise RuntimeError(f'Refusing to unmount an unexpected PCI path: {target}')
    if _mountpoint_is_exact(target):
        if not _null_mask_active(target):
            raise RuntimeError(f'PCI config mount was changed externally; refusing to unmount: {target}')
        run(['/usr/bin/umount', str(target)])
        if _mountpoint_is_exact(target):
            raise RuntimeError(f'PCI mask still mounted after unmount: {target}')
    # If the system rebooted, the saved mount no longer exists.
    PCI_MASK_STATE.unlink()
    detail(f'PCI MASK RESTORED: {target} (normal PCI config reads available again)')


def enable_basic_block():
    """Enable only the original module-autoload guard."""
    write_basic_block()


def disable_basic_block():
    remove_basic_block()


def enable_aggressive_guards(pci):
    """Enable the extended Steam/probe guard introduced in v2.3.

    This is self-contained: it has its own module-autoload rules in addition
    to suppressing nvidia-modprobe and NVIDIA loader discovery metadata.
    Therefore it works even when the separate Basic block checkbox is off.
    """
    # Order matters: stop module autoload first, then helper-based and
    # graphics-loader discovery before unloading the current driver stack.
    write_aggressive_module_block()
    try:
        disable_nvidia_modprobe()
        disable_nvidia_discovery()
        protect_pci_driver_binding(pci)
        enable_global_pci_config_mask(pci)
    except Exception:
        # Best-effort rollback to normal behavior if guard setup is incomplete.
        try:
            disable_global_pci_config_mask(pci)
        finally:
            try:
                restore_nvidia_discovery()
            finally:
                try:
                    restore_nvidia_modprobe()
                finally:
                    try:
                        restore_pci_driver_binding(pci)
                    finally:
                        remove_aggressive_module_block()
        raise


def disable_aggressive_guards(pci):
    # Restore userspace discovery and nvidia-modprobe before removing this
    # guard's module-autoload rules. The independent Basic block is untouched.
    disable_global_pci_config_mask(pci)
    restore_nvidia_discovery()
    restore_nvidia_modprobe()
    restore_pci_driver_binding(pci)
    remove_aggressive_module_block()


def disable_all_guards(pci):
    disable_aggressive_guards(pci)
    disable_basic_block()



def guard_summary():
    try:
        modprobe_count = len(_load_mode_state(NVIDIA_MODPROBE_STATE))
    except Exception:
        modprobe_count = 0
    try:
        discovery = _load_mode_state(DISCOVERY_STATE)
    except Exception:
        discovery = {}
    names = ', '.join(sorted(discovery))
    suffix = f' [{names}]' if names else ''
    return (f'nvidia-modprobe binaries guarded: {modprobe_count}; '
            f'NVIDIA discovery files guarded: {len(discovery)}{suffix}; '
            f'PCI config masked system-wide: {PCI_MASK_STATE.exists()}')

def log_removal_plan(modules):
    """Technical log only: what `modprobe -r` would remove, soft dependencies included.

    modprobe also removes dependencies that became unused and processes the soft
    dependencies configured in modprobe.d, so it can touch (or complain about) modules
    other than the ones named. A dry run shows that without changing anything.
    """
    if not DETAIL_ENABLED:
        return
    try:
        plan = subprocess.run(['modprobe', '--dry-run', '--verbose', '-r', *modules],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        detail(f'MODPROBE PLAN unavailable: {exc}')
        return
    for line in (plan.stdout + '\n' + plan.stderr).splitlines():
        if line.strip():
            detail(f'MODPROBE PLAN: {line.strip()}')


def unload_stack(pci=None):
    loaded = [name for name in MODULES if is_loaded(name)]
    if not loaded:
        return
    log_removal_plan(loaded)
    try:
        run(['modprobe', '-r', *loaded])
    except RuntimeError as exc:
        # Judge by the outcome, not by modprobe's exit status: it also handles unused
        # dependencies and soft dependencies, and can fail on one of those (for example
        # "Module i915 is in use") after every NVIDIA module has already been removed.
        remaining = [name for name in MODULES if is_loaded(name)]
        if not remaining:
            detail('MODPROBE REPORTED A PROBLEM BUT EVERY NVIDIA MODULE IS UNLOADED; treating the unload as done: '
                   + str(exc).replace('\n', ' '))
            return
        detail('NVIDIA MODULES STILL LOADED: ' + ', '.join(remaining))
        if 'in use' not in str(exc).lower():
            raise
        raise RuntimeError(f'{exc}\n{explain_driver_in_use(pci)}') from None


def load_stack():
    for module in ['nvidia', 'nvidia_uvm', 'nvidia_modeset', 'nvidia_drm']:
        run(['modprobe', module])


def nvidia_device_users():
    users = set()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        pid = proc.name
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith('/dev/nvidia'):
                    users.add(int(pid))
                    break
        except OSError:
            continue
    detail('NVIDIA DEVICE USERS: ' + (', '.join(map(str, sorted(users))) if users else 'none'))
    return users


def nvidia_drm_nodes(pci, drm_class=DRM_CLASS):
    """Device nodes (/dev/dri/cardN, renderDN) that belong to the NVIDIA GPU at *pci*.

    A compositor that keeps one of these open blocks unloading nvidia_drm without ever
    opening /dev/nvidia*, so a scan for /dev/nvidia* alone misses it.
    """
    nodes = set()
    try:
        entries = list(drm_class.iterdir())
    except OSError:
        return nodes
    for entry in entries:
        if not re.fullmatch(r'(?:card|renderD)[0-9]+', entry.name):
            continue
        try:
            owner = os.path.basename(os.path.realpath(entry / 'device'))
        except OSError:
            continue
        if owner.lower() == pci.lower():
            nodes.add(f'/dev/dri/{entry.name}')
    return nodes


def nvidia_holders(pci, proc=Path('/proc'), nodes=None):
    """Processes that have an NVIDIA device open: {pid: (name, [device paths])}."""
    nodes = nvidia_drm_nodes(pci) if nodes is None else nodes
    found = {}
    try:
        entries = list(Path(proc).iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            descriptors = list((entry / 'fd').iterdir())
        except OSError:
            continue
        paths = set()
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith('/dev/nvidia') or target in nodes:
                paths.add(target)
        if paths:
            try:
                name = (entry / 'comm').read_text().strip()
            except OSError:
                name = ''
            found[int(entry.name)] = (re.sub(r'[^\w.+@:-]', '?', name) or '?', sorted(paths))
    return found


def holder_summary(holders, limit=6):
    """'kwin_wayland (PID 812), Xwayland (PID 990, 991)': one entry per program name."""
    by_name = {}
    for pid, (name, _paths) in sorted(holders.items()):
        by_name.setdefault(name, []).append(pid)
    items = []
    for name, pids in sorted(by_name.items(), key=lambda item: item[1][0])[:limit]:
        shown = ', '.join(map(str, pids[:3])) + (', …' if len(pids) > 3 else '')
        items.append(f'{name} (PID {shown})')
    if len(by_name) > limit:
        items.append(f'and {len(by_name) - limit} more')
    return ', '.join(items)


def module_diagnostics(sys_module=SYS_MODULE):
    """Kernel-side view of the NVIDIA modules, for the technical log when unloading fails."""
    lines = []
    for name in MODULES:
        base = sys_module / name
        if not base.is_dir():
            continue
        try:
            refcnt = (base / 'refcnt').read_text().strip()
        except OSError:
            refcnt = 'n/a'
        try:
            users = sorted(p.name for p in (base / 'holders').iterdir())
        except OSError:
            users = []
        parts = [f'refcnt={refcnt}', 'used-by-modules=' + (','.join(users) if users else 'none')]
        for parameter in ('modeset', 'fbdev'):
            try:
                parts.append(f'{parameter}={(base / "parameters" / parameter).read_text().strip()}')
            except OSError:
                pass
        lines.append(f'{name}: ' + ' '.join(parts))
    return lines


def explain_driver_in_use(pci):
    """One line for the user after 'Module nvidia_drm is in use', plus audit lines."""
    holders = nvidia_holders(pci) if pci else {}
    for pid, (name, paths) in sorted(holders.items()):
        detail(f'NVIDIA HOLDER: PID {pid} {name}: ' + ', '.join(paths))
    for line in module_diagnostics():
        detail(f'NVIDIA MODULE {line}')
    if holders:
        text = (f'NVIDIA is held open by: {holder_summary(holders)}. Close them and try again; on KDE Wayland see the '
                'README section "KDE Wayland: keeping the desktop on Intel".')
        if any(name == 'kwin_wayland' for name, _paths in holders.values()):
            text += (' KWin itself holds NVIDIA: run ./install.sh from the latest release again (it limits KWin to the '
                     'Intel GPU), then log out and back in.')
        return text
    return ('No program was found holding an NVIDIA device open, so the driver may be held by a kernel component '
            '(for example display mode setting). Details are in the technical log; see the README troubleshooting table.')


def save_state(pci):
    validate_device(pci)
    try:
        gpu_state_path = device_path(pci) / 'power_state'
        gpu_state = gpu_state_path.read_text().strip()
        detail(f'FILE READ: {gpu_state_path} -> {gpu_state}')
    except OSError:
        gpu_state = 'Unknown'
    if gpu_state.lower() not in ('d0', 'd3cold'):
        raise RuntimeError(f'Cannot save GPU state because the current state is {gpu_state}. Turn the GPU on or off first.')
    _save_mode_state(STATE_FILE, {
        'gpu_state': gpu_state,
        'basic_blocked': basic_blocked(),
        'aggressive_blocked': aggressive_blocked(),
    })


def _saved_guard_flags(saved):
    """Return (basic, aggressive), including v2.3.x saved-state migration."""
    if 'basic_blocked' in saved or 'aggressive_blocked' in saved:
        return bool(saved.get('basic_blocked')), bool(saved.get('aggressive_blocked'))
    # v2.3.x had one checkbox named `blocked`; that checkbox represented the
    # full aggressive Steam/probe guard, not the old module-only variant.
    return False, bool(saved.get('blocked'))


def _apply_saved_guards(pci, want_basic, want_aggressive):
    # First clear each independently so a changed saved choice cannot leave a
    # stale guard behind, then enable the exact saved combination.
    disable_aggressive_guards(pci)
    disable_basic_block()
    if want_basic:
        enable_basic_block()
    if want_aggressive:
        enable_aggressive_guards(pci)


def restore_state(pci):
    try:
        saved = json.loads(STATE_FILE.read_text())
        gpu_state = saved.get('gpu_state')
        basic, aggressive = _saved_guard_flags(saved)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f'No valid saved GPU state is available: {exc}')

    validate_device(pci)
    _apply_saved_guards(pci, basic, aggressive)

    if basic or aggressive:
        unload_stack(pci)
        state = power_off(pci)
        save_state(pci)
        modes = []
        if basic:
            modes.append('basic module-autoload block')
        if aggressive:
            modes.append('aggressive Steam/probe guard')
        mode_summary = '; '.join(modes)
        extra = (' ' + guard_summary()) if aggressive else ''
        print(f'Restored GPU state: {state}; enabled: {mode_summary}.{extra}')
    elif gpu_state == 'D3cold':
        unload_stack(pci)
        state = power_off(pci)
        save_state(pci)
        print(f'Restored GPU state: {state}; NVIDIA automatic loading/probing is allowed.')
    elif gpu_state == 'D0':
        load_stack()
        save_state(pci)
        print('Restored GPU state: D0; NVIDIA automatic loading/probing is allowed.')
    else:
        raise RuntimeError(f'Unsupported saved GPU state: {gpu_state!r}')


def main():
    global DETAIL_ENABLED
    if os.geteuid() != 0:
        raise RuntimeError('This helper must run as root through the installed privileged command rule.')
    os.environ['PATH'] = SAFE_PATH
    if len(sys.argv) not in (3, 4):
        raise RuntimeError('Usage: optimus_helper.py ACTION PCI_ADDRESS [--detail]')
    action, pci = sys.argv[1:3]
    if len(sys.argv) == 4:
        if sys.argv[3] != '--detail':
            raise RuntimeError('Unknown helper option.')
        DETAIL_ENABLED = True
    if not PCI_RE.fullmatch(pci):
        raise RuntimeError('Invalid PCI address.')
    try:
        configured = PCI_CONFIG.read_text().strip().lower()
        detail(f'FILE READ: {PCI_CONFIG} -> {configured}')
    except OSError:
        raise RuntimeError('Installed GPU PCI configuration is missing; reinstall the application.')
    if pci.lower() != configured:
        raise RuntimeError(f'Requested PCI address {pci} does not match the installed GPU configuration {configured}.')
    pci = configured
    detail(f'ACTION START: {action}; pci={pci}')

    if action == 'save-state':
        save_state(pci)
        # The GPU save-state action already runs as root under the installed sudoers rule.
        # Capture CPU preferences here too, avoiding a second prompt.
        from cpu_power import remember_current_cpu
        cpu_state = remember_current_cpu()
        print('Saved GPU state, NVIDIA guards and CPU preferences: ' + str(cpu_state))

    elif action == 'restore-state':
        restore_state(pci)

    elif action == 'gpu-off':
        validate_device(pci)
        unload_stack(pci)
        state = power_off(pci)
        save_state(pci)
        print(f'NVIDIA modules unloaded; GPU reached {state}.')

    elif action == 'gpu-on':
        validate_device(pci)
        was_basic = basic_blocked()
        was_aggressive = aggressive_blocked()
        disable_all_guards(pci)
        try:
            load_stack()
        except Exception:
            # Restore the exact previous guard combination if loading fails.
            try:
                if was_basic:
                    enable_basic_block()
                if was_aggressive:
                    enable_aggressive_guards(pci)
                if was_basic or was_aggressive:
                    unload_stack(pci)
                    power_off(pci)
            except Exception:
                pass
            raise
        save_state(pci)
        print('NVIDIA modules loaded. Basic and aggressive automatic-loading/probe guards were removed.')

    elif action == 'basic-block-on':
        validate_device(pci)
        already = basic_blocked()
        enable_basic_block()
        try:
            unload_stack(pci)
            state = power_off(pci)
            save_state(pci)
        except Exception:
            if not already:
                disable_basic_block()
            raise
        print(f'Basic NVIDIA module-loading block enabled; GPU reached {state}. Only module autoload is blocked.')

    elif action == 'basic-block-off':
        validate_device(pci)
        disable_basic_block()
        save_state(pci)
        if aggressive_blocked():
            print('Basic module-loading block disabled; aggressive Steam/probe guard remains enabled.')
        else:
            print('Basic NVIDIA module-loading block disabled.')

    elif action == 'aggressive-block-on':
        validate_device(pci)
        already = aggressive_blocked()
        enable_aggressive_guards(pci)
        try:
            unload_stack(pci)
            state = power_off(pci)
            save_state(pci)
        except Exception:
            if not already:
                disable_aggressive_guards(pci)
            raise
        print(
            f'Aggressive NVIDIA Steam/probe guard enabled. GPU reached {state}; the PCI device remains present, '
            'while system-wide userspace PCI config reads, PCI driver binding, NVIDIA module loading, nvidia-modprobe, and NVIDIA Vulkan/EGL/OpenCL discovery metadata are guarded. ' + guard_summary()
        )

    elif action == 'aggressive-block-off':
        validate_device(pci)
        disable_aggressive_guards(pci)
        save_state(pci)
        if basic_blocked():
            print('Aggressive Steam/probe guard disabled; basic module-loading block remains enabled.')
        else:
            print('Aggressive NVIDIA Steam/probe guard disabled; NVIDIA probing is allowed again.')

    elif action == 'auto-off':
        validate_device(pci)
        users = nvidia_device_users()
        if users:
            raise RuntimeError(f'NVIDIA device is still in use by PID(s): {", ".join(map(str, sorted(users)))}')
        if is_loaded('nvidia') or is_loaded('nvidia_drm'):
            unload_stack(pci)
        state = power_off(pci)
        print(f'NVIDIA application use ended; GPU restored to {state}.')

    elif action == 'uninstall-restore':
        # Current releases never remove the PCI device. Restore all software guards even if a
        # previous v2.2 session left the GPU absent; a reboot will re-enumerate it.
        disable_all_guards(pci)
        if LEGACY_DETACHED_STATE.exists():
            detail(f'FILE REMOVE: {LEGACY_DETACHED_STATE} (legacy uninstall cleanup)')
        LEGACY_DETACHED_STATE.unlink(missing_ok=True)
        print('Normal NVIDIA userspace/module-loading behavior restored for uninstall.')

    else:
        raise RuntimeError('Unknown action.')

    detail(f'ACTION COMPLETE: {action}')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
