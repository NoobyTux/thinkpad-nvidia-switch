#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail

APP_NAME="ThinkPad Nvidia Switch"
DEST="${HOME}/.local/share/thinkpad-nvidia-switch"
ICON="${HOME}/.local/share/icons/hicolor/scalable/apps/thinkpad-nvidia-switch.svg"
DESKTOP="${HOME}/.local/share/applications/thinkpad-nvidia-switch.desktop"
STEAM_DESKTOP="${HOME}/.local/share/applications/thinkpad-nvidia-switch-steam-protected.desktop"
AUTOSTART="${HOME}/.config/autostart/thinkpad-nvidia-switch.desktop"
KWIN_FIX_SCRIPT="${HOME}/.config/plasma-workspace/env/thinkpad-nvidia-switch-kwin-nvidia-fix.sh"
PREFS="${HOME}/.config/thinkpad-nvidia-switch/settings.json"
LEGACY_PREFS="${HOME}/.config/thinkpad-nvidia-switch/preferences.json"
LOG_DIR="${HOME}/.local/state/thinkpad-nvidia-switch"

BASIC_BLOCK_FILE=/etc/modprobe.d/optimus-tray-block.conf
AGGRESSIVE_BLOCK_FILE=/etc/modprobe.d/optimus-tray-probe-guard.conf
STATE_DIR=/var/lib/thinkpad-nvidia-switch
NVIDIA_MODPROBE_STATE="$STATE_DIR/nvidia-modprobe-modes.json"
DISCOVERY_STATE="$STATE_DIR/nvidia-discovery-modes.json"
DRIVER_OVERRIDE_STATE="$STATE_DIR/pci-driver-override.json"
PCI_MASK_STATE="$STATE_DIR/pci-config-mask.json"
DETACHED_STATE="$STATE_DIR/pci-detached.json"
HELPER=/usr/local/libexec/thinkpad-nvidia-switch-helper
CPU_HELPER=/usr/local/libexec/thinkpad-nvidia-switch-cpu-helper
CPU_POWER_MODULE=/usr/local/libexec/cpu_power.py
PROFILE_HELPER=/usr/local/libexec/thinkpad-nvidia-switch-profile-apply
PROFILE_SCHEMA_MODULE=/usr/local/libexec/profiles.py
UNDERVOLT_HELPER=/usr/local/libexec/thinkpad-nvidia-switch-undervolt
UNDERVOLT_MODULE=/usr/local/libexec/undervolt_control.py
PROFILES="${HOME}/.config/thinkpad-nvidia-switch/profiles.json"
UNPLUG_STATE="${HOME}/.config/thinkpad-nvidia-switch/unplug-timer.json"
CPU_CORES_STATE="$STATE_DIR/cpu-offlined-cores.json"
CPU_LIMITS_STATE="$STATE_DIR/cpu-original-limits.json"
CPU_FREQUENCY_STATE="$STATE_DIR/cpu-original-frequency.json"
CPU_STATUS=/run/thinkpad-nvidia-switch-cpu-cores.json
BOOT_CAPTURE_UNIT=thinkpad-nvidia-switch-cpu-boot-defaults.service
RESTORE_WRAPPER=/usr/local/libexec/thinkpad-nvidia-switch-restore
AUTO_OFF_WRAPPER=/usr/local/libexec/thinkpad-nvidia-switch-auto-off
SUDOERS_FILE=/etc/sudoers.d/thinkpad-nvidia-switch-restore
SYSTEM_CONFIG_DIR=/etc/thinkpad-nvidia-switch
SYSTEM_GPU_CONFIG="$SYSTEM_CONFIG_DIR/gpu-pci-address"

printf 'Uninstalling %s...\n' "$APP_NAME"
if [[ "$(id -u)" -eq 0 ]]; then
    echo 'Error: run uninstall.sh as the desktop user that installed the application, not as root or with sudo.' >&2
    exit 1
fi
command -v sudo >/dev/null 2>&1 || { echo 'Error: sudo is required to restore system files safely.' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'Error: python3 is required to restore system files safely.' >&2; exit 1; }

# Quit the tray application first: it would otherwise keep running with its
# helpers and files removed underneath it.
RUNNING_APP_PIDS="$(python3 - <<'PY'
import os
from pathlib import Path

pids = []
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        raw = (entry / 'cmdline').read_bytes()
    except OSError:
        continue
    args = [part.decode('utf-8', 'replace') for part in raw.split(b'\0') if part]
    if (args and Path(args[0]).name.lower().startswith('python')
            and any(Path(arg).name == 'optimus_tray.py' for arg in args[1:])):
        pids.append(entry.name)
print(' '.join(pids))
PY
)"
if [[ -n "$RUNNING_APP_PIDS" ]]; then
    echo "Error: $APP_NAME is still running (PID(s): $RUNNING_APP_PIDS). Quit it from the tray menu, then run uninstall.sh again." >&2
    exit 1
fi

sudo -v

# The state directory is root-owned and may not be searchable by regular users
# (older releases created it with mode 0700), so every test below goes through sudo.
root_has() { sudo test -e "$1"; }
root_has_file() { sudo test -f "$1"; }

GUARD_STATE_PRESENT=0
if [[ -e "$BASIC_BLOCK_FILE" || -e "$AGGRESSIVE_BLOCK_FILE" ]] \
    || root_has "$DETACHED_STATE" || root_has "$NVIDIA_MODPROBE_STATE" || root_has "$DISCOVERY_STATE" \
    || root_has "$DRIVER_OVERRIDE_STATE" || root_has "$PCI_MASK_STATE"; then
    GUARD_STATE_PRESENT=1
fi

# First use the installed helper when possible. If it is damaged or its config
# is missing/corrupt, continue to the independent recovery records below rather
# than deleting those records or leaving guarded files behind.
if (( GUARD_STATE_PRESENT )) && [[ -x "$HELPER" && -r "$SYSTEM_GPU_CONFIG" ]]; then
    GPU_PCI="$(tr '[:upper:]' '[:lower:]' < "$SYSTEM_GPU_CONFIG" | tr -d '[:space:]')"
    printf 'Restoring normal NVIDIA userspace/module-loading behavior...\n'
    if ! sudo "$HELPER" uninstall-restore "$GPU_PCI"; then
        printf 'Warning: helper-based restoration failed; trying saved permission recovery records.\n' >&2
    fi
fi

# Recover even if the helper is missing. Never delete the state directory while
# an app-owned PCI config mount is still active.
if root_has_file "$PCI_MASK_STATE"; then
    printf 'Restoring host PCI configuration access...\n'
    sudo python3 - "$PCI_MASK_STATE" <<'PYPCIMASK'
import json, os, re, stat, subprocess, sys
from pathlib import Path
state = Path(sys.argv[1])
try:
    record = json.loads(state.read_text())
    pci = record['pci']
    target = Path(record['target'])
    if (not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', pci)
            or record.get('source') != '/dev/null'
            or not target.is_relative_to('/sys/devices') or target.name != 'config'):
        raise ValueError('invalid PCI mask recovery state')
    mounted = any(line.split()[4] == str(target)
                  for line in Path('/proc/self/mountinfo').read_text().splitlines()
                  if len(line.split()) >= 5)
    if mounted:
        info = target.stat()
        null = Path('/dev/null').stat()
        if not stat.S_ISCHR(info.st_mode) or info.st_rdev != null.st_rdev:
            raise ValueError('mount changed by another tool; not removing it')
        subprocess.run(['/usr/bin/umount', str(target)], check=True)
    state.unlink()
except Exception as exc:
    print(f'Error restoring PCI config mount: {exc}', file=sys.stderr)
    sys.exit(1)
PYPCIMASK
fi

restore_mode_state() {
    local mode_state="$1"
    root_has_file "$mode_state" || return 0
    printf 'Restoring NVIDIA guard permissions from %s...\n' "$mode_state"
    sudo python3 - "$mode_state" <<'PYRESTORE'
import json
import os
import sys
from pathlib import Path

state = Path(sys.argv[1])
try:
    data = json.loads(state.read_text())
    if not isinstance(data, dict):
        raise ValueError('state is not a JSON object')
    errors = []
    for name, mode in data.items():
        path = Path(name)
        if not path.exists():
            continue
        try:
            os.chmod(path, int(mode))
        except OSError as exc:
            errors.append(f'{path}: {exc}')
    if errors:
        raise OSError('; '.join(errors))
    state.unlink(missing_ok=True)
except Exception as exc:
    print(f'Error: could not restore guarded file permissions from {state}: {exc}', file=sys.stderr)
    sys.exit(1)
PYRESTORE
}

# Restore a binding override even if the installed helper is missing or broken.
# Do this before deleting app records; unlike a chmod it is a volatile sysfs
# setting and must never be left at 'none' after uninstall.
if root_has_file "$DRIVER_OVERRIDE_STATE"; then
    printf 'Restoring original PCI driver binding policy...\n'
    sudo python3 - "$DRIVER_OVERRIDE_STATE" <<'PYOVERRIDE'
import json
import sys
from pathlib import Path
state = Path(sys.argv[1])
try:
    data = json.loads(state.read_text())
    pci, original = data['pci'], data['original']
    import re
    if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', pci) or not isinstance(original, str):
        raise ValueError('invalid recovery state')
    target = Path('/sys/bus/pci/devices') / pci / 'driver_override'
    if target.exists():
        current = target.read_text().strip()
        if current == '(null)':
            current = ''
        if current not in ('none', original):
            raise ValueError('override belongs to another tool; manual restoration needed')
        target.write_text(original + '\n')
    state.unlink()
except Exception as exc:
    print(f'Error restoring driver binding: {exc}', file=sys.stderr)
    sys.exit(1)
PYOVERRIDE
fi

# Exact original modes were recorded before any chmod. Do not remove the state
# directory unless all remaining records can be restored successfully.
restore_mode_state "$NVIDIA_MODPROBE_STATE"
restore_mode_state "$DISCOVERY_STATE"

# Remove both app-owned module-loading guards. Leaving either file behind would
# continue blocking NVIDIA modules after the application was removed.
if [[ -e "$BASIC_BLOCK_FILE" || -e "$AGGRESSIVE_BLOCK_FILE" ]]; then
    printf 'Removing NVIDIA module-loading guards...\n'
    sudo rm -f "$BASIC_BLOCK_FILE" "$AGGRESSIVE_BLOCK_FILE"
fi

# Restore CPU adjustments BEFORE deleting privileged helper and recovery records.
# Never silently erase restore state if a CPU recovery fails.
if [[ -e "$CPU_STATUS" ]] || root_has "$CPU_CORES_STATE" || root_has "$CPU_LIMITS_STATE" || root_has "$CPU_FREQUENCY_STATE"; then
    if [[ ! -x "$CPU_HELPER" ]]; then
        printf 'Error: CPU restore helper is missing. Keep the installation and reboot before retrying.\n' >&2
        exit 1
    fi
    printf 'Restoring app-managed CPU cores and available CPU boot/hardware defaults...\n'
    if ! sudo "$CPU_HELPER" uninstall-restore; then
        printf 'Error: CPU restore failed. Nothing has deleted the recovery records. Restore manually or reboot, then uninstall.\n' >&2
        exit 1
    fi
fi
# Restore an untouched app-managed intel-undervolt configuration, but never
# overwrite a config that another program or user modified after our last set.
if [[ -x "$UNDERVOLT_HELPER" ]]; then
    if ! sudo "$UNDERVOLT_HELPER" uninstall-restore; then
        printf 'Warning: voltage configuration could not be restored. Review /etc/intel-undervolt.conf before removing the application.\n' >&2
        exit 1
    fi
fi
printf 'Removing privileged helper and sudoers rule...\n'
sudo systemctl disable --now "$BOOT_CAPTURE_UNIT" >/dev/null 2>&1 || true
sudo rm -f "/etc/systemd/system/$BOOT_CAPTURE_UNIT"
sudo systemctl daemon-reload || true
sudo rm -f "$SUDOERS_FILE" "$HELPER" "$CPU_HELPER" "$CPU_POWER_MODULE" "$RESTORE_WRAPPER" "$AUTO_OFF_WRAPPER" "$PROFILE_HELPER" "$PROFILE_SCHEMA_MODULE" "$UNDERVOLT_HELPER" "$UNDERVOLT_MODULE"
# Python caches that root created next to the helper modules (only ours; the
# directory is removed again only if nothing else uses it).
sudo rm -f /usr/local/libexec/__pycache__/cpu_power.*.pyc /usr/local/libexec/__pycache__/profiles.*.pyc /usr/local/libexec/__pycache__/undervolt_control.*.pyc
sudo rmdir /usr/local/libexec/__pycache__ 2>/dev/null || true
sudo rm -rf "$STATE_DIR" "$SYSTEM_CONFIG_DIR"
# Public same-boot UI status was published by this app and lives in /run.
sudo rm -f /run/thinkpad-nvidia-switch-cpu-boot-defaults.json

# Remove only the drop-in and symlink created by this project. The user's
# separate Mesa EGL or experimental 70-intel-render.conf files are NOT ours.
if [[ -f "$DEST/src/kwin_render_setup.py" ]]; then
    if ! python3 "$DEST/src/kwin_render_setup.py" uninstall; then
        printf 'Warning: KWin render drop-in is modified or cannot be removed; review it manually.\n' >&2
    fi
else
    printf 'Note: app KWin setup helper is missing; check the app-owned 90-... render drop-in manually.\n' >&2
fi
systemctl --user daemon-reload || true

printf 'Removing user files...\n'
rm -f "$AUTOSTART" "$DESKTOP" "$STEAM_DESKTOP" "$ICON" "$PREFS" "$LEGACY_PREFS" "$PROFILES" "$UNPLUG_STATE" "$KWIN_FIX_SCRIPT"
rmdir "${HOME}/.config/thinkpad-nvidia-switch" 2>/dev/null || true
rm -rf "$LOG_DIR" "$DEST"

if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 >/dev/null 2>&1 || true
fi

printf '%s has been uninstalled.\n' "$APP_NAME"
printf 'The app-managed KWin Vulkan and Intel-render workarounds were removed where unmodified. Log out and back in.\n'
printf 'Any manually created KWin systemd overrides (for example 50-mesa-only.conf or 70-intel-render.conf) were left intact.\n'
printf 'If the application was still running, close it and start a new desktop session if Plasma still shows its old launcher entry.\n'
printf 'Packages you installed yourself (PySide6, intel-undervolt, the NVIDIA driver) were not removed; remove them with your package manager if you no longer need them.\n'
