#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# ThinkPad Nvidia Switch installer.
#
# Run as your normal desktop user (not with sudo); it asks for administrator
# rights itself. Optional environment variables:
#   TPNS_KWIN_INTEL_ONLY=0   do not install the KWin "Intel only" drop-in (render node + display device)
#   TPNS_KWIN_VULKAN_FIX=0   do not install the KWIN_DISABLE_VULKAN=1 session file
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_VERSION="2.7.0"

SYSTEM_CONFIG_DIR=/etc/thinkpad-nvidia-switch
SYSTEM_GPU_CONFIG="$SYSTEM_CONFIG_DIR/gpu-pci-address"
STATE_DIR=/var/lib/thinkpad-nvidia-switch
DETACHED_STATE="$STATE_DIR/pci-detached.json"

HELPER_DEST=/usr/local/libexec/thinkpad-nvidia-switch-helper
CPU_HELPER_DEST=/usr/local/libexec/thinkpad-nvidia-switch-cpu-helper
CPU_POWER_MODULE=/usr/local/libexec/cpu_power.py
PROFILE_HELPER_DEST=/usr/local/libexec/thinkpad-nvidia-switch-profile-apply
PROFILE_SCHEMA_MODULE=/usr/local/libexec/profiles.py
UNDERVOLT_HELPER_DEST=/usr/local/libexec/thinkpad-nvidia-switch-undervolt
UNDERVOLT_MODULE=/usr/local/libexec/undervolt_control.py
RESTORE_WRAPPER=/usr/local/libexec/thinkpad-nvidia-switch-restore
AUTO_OFF_WRAPPER=/usr/local/libexec/thinkpad-nvidia-switch-auto-off
SUDOERS_FILE=/etc/sudoers.d/thinkpad-nvidia-switch-restore
BOOT_CAPTURE_UNIT=thinkpad-nvidia-switch-cpu-boot-defaults.service

BASIC_BLOCK=/etc/modprobe.d/optimus-tray-block.conf
AGGRESSIVE_BLOCK=/etc/modprobe.d/optimus-tray-probe-guard.conf
MODPROBE_STATE="$STATE_DIR/nvidia-modprobe-modes.json"
DISCOVERY_STATE="$STATE_DIR/nvidia-discovery-modes.json"
DRIVER_OVERRIDE_STATE="$STATE_DIR/pci-driver-override.json"
PCI_MASK_STATE="$STATE_DIR/pci-config-mask.json"

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "$1 is required."
}

# /var/lib/thinkpad-nvidia-switch is root-owned and, on installations made by
# older releases, not searchable by regular users. Always test it through sudo.
root_has() {
    sudo test -e "$1"
}

if [[ "$(id -u)" -eq 0 ]]; then
    fail 'Run install.sh as your normal desktop user, not as root or with sudo. It installs the application into your home directory and asks for administrator rights only where needed.'
fi

# Preflight first so a missing dependency does not leave a half-installed copy.
require_command python3
require_command sudo
require_command modprobe
require_command mount
require_command systemctl
require_command umount

VISUDO_BIN="$(command -v visudo || true)"
if [[ -z "$VISUDO_BIN" && -x /usr/sbin/visudo ]]; then
    VISUDO_BIN=/usr/sbin/visudo
fi
[[ -n "$VISUDO_BIN" ]] || fail 'visudo (part of sudo) is required.'

# The desktop application runs with the first python3 on PATH, while the
# root-owned helpers use the fixed interpreter named in their shebang line.
for interpreter in "$(command -v python3)" /usr/bin/python3; do
    [[ -x "$interpreter" ]] || fail 'The system Python (/usr/bin/python3) used by the root helpers was not found.'
    "$interpreter" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
        || fail "Python 3.9 or newer is required ($interpreter is older)."
done

if ! python3 - <<'PY' >/dev/null 2>&1
import PySide6  # noqa: F401
PY
then
    fail 'PySide6 is required. On Arch/CachyOS install it with: sudo pacman -S pyside6 (elsewhere: python3 -m pip install PySide6, inside a virtual environment if your distribution requires one, then run this script with that environment active).'
fi
# The launcher records the exact interpreter that was just verified to have PySide6
# (a virtual environment or pyenv python is not on the desktop session's PATH).
PYTHON_BIN="$(command -v python3)"

# Replacing files under a live Python process is confusing during upgrades, and
# old releases did not enforce single-instance behavior. Refuse the upgrade until
# every existing tray/controller process has been quit.
# Only real interpreter processes count; an editor or grep that merely has the
# file name as an argument must not block the installer.
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
    fail "ThinkPad Nvidia Switch is currently running (PID(s): $RUNNING_APP_PIDS). Quit every existing tray instance, then run install.sh again."
fi

# Authenticate before touching user/system installation paths or inspecting
# root-only recovery records. This also catches a cancelled/failed sudo request
# before an existing installation is replaced.
printf 'Administrator authentication is required once to install root-owned helpers and their passwordless runtime sudoers rule.\n'
sudo -v

# v2.2 could remove the NVIDIA PCI function. On the tested T440p a runtime PCI
# rescan could not bring it back, so require one reboot instead of pretending an
# in-place upgrade can recover it. Current releases never use PCI remove.
if [[ -r "$SYSTEM_GPU_CONFIG" ]] && root_has "$DETACHED_STATE"; then
    OLD_GPU_PCI="$(tr '[:upper:]' '[:lower:]' < "$SYSTEM_GPU_CONFIG" | tr -d '[:space:]')"
    if [[ "$OLD_GPU_PCI" =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ && ! -e "/sys/bus/pci/devices/$OLD_GPU_PCI" ]]; then
        printf 'An old v2.2 installation left NVIDIA (%s) detached from this PCI session.\n' "$OLD_GPU_PCI" >&2
        printf 'Reboot once, then run this installer again. Current releases do not use PCI removal.\n' >&2
        exit 2
    fi
fi

if ! GPU_PCI="$(python3 "$ROOT/src/detect_nvidia_gpu.py")"; then
    fail 'NVIDIA GPU detection failed; installation was not changed.'
fi
printf 'Detected NVIDIA display controller at PCI address: %s\n' "$GPU_PCI"

DEST="${HOME}/.local/share/thinkpad-nvidia-switch"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"
APP_DIR="${HOME}/.local/share/applications"
AUTOSTART_DIR="${HOME}/.config/autostart"
KDE_ENV_DIR="${HOME}/.config/plasma-workspace/env"
KWIN_FIX_SCRIPT="${KDE_ENV_DIR}/thinkpad-nvidia-switch-kwin-nvidia-fix.sh"
USER_NAME="$(id -un)"

# Snapshot the old guard layout BEFORE replacing the helper. v2.3.x used the
# legacy BASIC_BLOCK path for its single aggressive guard. Split-guard builds
# use AGGRESSIVE_BLOCK separately; preserving both files preserves both choices.
HAD_BASIC_BLOCK=0
HAD_AGGRESSIVE_BLOCK=0
HAD_AGGRESSIVE_RECOVERY=0
[[ -e "$BASIC_BLOCK" ]] && HAD_BASIC_BLOCK=1
[[ -e "$AGGRESSIVE_BLOCK" ]] && HAD_AGGRESSIVE_BLOCK=1
if root_has "$MODPROBE_STATE" || root_has "$DISCOVERY_STATE" || root_has "$DRIVER_OVERRIDE_STATE" || root_has "$PCI_MASK_STATE"; then
    HAD_AGGRESSIVE_RECOVERY=1
fi

# The path is substituted into a desktop entry's Exec line; refuse characters that
# would need special quoting there rather than write a broken launcher.
for unsafe_path in "$DEST" "$PYTHON_BIN"; do
    if [[ "$unsafe_path" == *[\"\`\$\\%\&\|]* ]]; then
        fail "The path contains characters that are not supported in a desktop entry: $unsafe_path"
    fi
done

# Replace only app-owned installed files so stale preview assets cannot survive
# an upgrade. Preferences/logs live elsewhere and are left untouched.
rm -rf "$DEST"
mkdir -p "$DEST" "$ICON_DIR" "$APP_DIR" "$AUTOSTART_DIR"
cp -a "$ROOT/src" "$ROOT/assets" "$ROOT/LICENSE" "$ROOT/README.md" "$DEST/"
find "$DEST" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true
printf '%s\n' "$GPU_PCI" > "$DEST/src/gpu-pci-address"
chmod +x "$DEST/src/optimus_tray.py" "$DEST/src/optimus_helper.py" "$DEST/src/detect_nvidia_gpu.py"
install -m 0755 "$ROOT/uninstall.sh" "$DEST/uninstall.sh"
install -m 0644 "$ROOT/assets/thinkpad-nvidia-switch.svg" "$ICON_DIR/thinkpad-nvidia-switch.svg"
sed -e "s|APP_INSTALL_DIR|$DEST|g" -e "s|APP_PYTHON|$PYTHON_BIN|g" "$ROOT/data/thinkpad-nvidia-switch.desktop" > "$APP_DIR/thinkpad-nvidia-switch.desktop"
chmod 0644 "$APP_DIR/thinkpad-nvidia-switch.desktop"
# Remove the v2.4.3 one-off Steam launcher. Aggressive protection is now automatic.
rm -f "$APP_DIR/thinkpad-nvidia-switch-steam-protected.desktop"

# NVIDIA 470 on some hybrid-GPU Plasma systems can be probed by KWin's Vulkan
# backend. Keep the tested workaround, but only where KWin is installed and
# unless the user opted out (it is a session-wide environment setting).
if [[ "${TPNS_KWIN_VULKAN_FIX:-1}" == "0" ]]; then
    printf 'KWin Vulkan workaround skipped at user request (TPNS_KWIN_VULKAN_FIX=0).\n'
elif command -v kwin_wayland >/dev/null 2>&1 || command -v kwin_x11 >/dev/null 2>&1; then
    mkdir -p "$KDE_ENV_DIR"
    cat > "$KWIN_FIX_SCRIPT" <<'KWIN_EOF'
#!/bin/sh
export KWIN_DISABLE_VULKAN=1
KWIN_EOF
    chmod 0755 "$KWIN_FIX_SCRIPT"
    printf 'Installed KWin hybrid-GPU workaround: %s\n' "$KWIN_FIX_SCRIPT"
    printf 'Log out and back in (or reboot) for the KWin environment change to take effect.\n'
else
    printf 'KWin was not found; the KDE session workaround was not installed.\n'
fi

# KWin >= 6.7.4 can keep NVIDIA's render node open even when NVIDIA is not
# rendering the desktop. Restrict only KWin to the detected Intel render node.
# The installer uses an app-owned, colon-free alias pointing at the stable PCI
# by-path link; never hardcode renderD128/card1 or modify global EGL settings.
if [[ "${TPNS_KWIN_INTEL_ONLY:-1}" == "0" ]]; then
    printf 'KWin Intel-only restriction skipped at user request (TPNS_KWIN_INTEL_ONLY=0).\n'
else
    printf 'KWin notice: KWin will be limited to the Intel GPU (render node and display device) so NVIDIA can be switched off; monitors wired to NVIDIA will not work in the KDE session. To opt out: TPNS_KWIN_INTEL_ONLY=0 ./install.sh\n'
    if python3 "$ROOT/src/kwin_render_setup.py" install; then
        systemctl --user daemon-reload || printf 'Warning: the user systemd manager was unavailable. Log out and back in before checking KWin.\n' >&2
    else
        printf 'Warning: KWin render setup was skipped or failed; other app features will still be installed. See the README for manual checks.\n' >&2
    fi
fi

# Root-owned config is the authority used by the privileged helper and wrappers.
printf 'Installing restricted root helper and detected GPU configuration...\n'
sudo install -d -o root -g root -m 0755 "$SYSTEM_CONFIG_DIR"
# The recovery files inside are 0600. The directory itself must be searchable so
# the unprivileged GUI can tell whether recovery records exist (this also repairs
# a 0700 directory created by an older release).
sudo install -d -o root -g root -m 0755 "$STATE_DIR"
printf '%s\n' "$GPU_PCI" | sudo tee "$SYSTEM_GPU_CONFIG" >/dev/null
sudo chown root:root "$SYSTEM_GPU_CONFIG"
sudo chmod 0644 "$SYSTEM_GPU_CONFIG"
sudo install -D -o root -g root -m 0755 "$ROOT/src/optimus_helper.py" "$HELPER_DEST"
# The CPU privilege boundary lives outside the user-writable application directory.
sudo install -D -o root -g root -m 0755 "$ROOT/src/cpu_power_helper.py" "$CPU_HELPER_DEST"
sudo install -D -o root -g root -m 0644 "$ROOT/src/cpu_power.py" "$CPU_POWER_MODULE"
# Validated profile executor. Passwordless access is needed for AC/battery
# transitions while the GUI is minimized, but all arguments are restricted.
sudo install -D -o root -g root -m 0644 "$ROOT/src/profiles.py" "$PROFILE_SCHEMA_MODULE"
sudo install -D -o root -g root -m 0755 "$ROOT/src/profile_apply_helper.py" "$PROFILE_HELPER_DEST"
# No new kernel-driver writes: use the established intel-undervolt utility.
# The helper exposes only read and strictly bounded CPU/cache/Intel-GPU offsets.
sudo install -D -o root -g root -m 0644 "$ROOT/src/undervolt_control.py" "$UNDERVOLT_MODULE"
sudo install -D -o root -g root -m 0755 "$ROOT/src/undervolt_helper.py" "$UNDERVOLT_HELPER_DEST"
# The root helper looks for the program in exactly these two places.
if [[ ! -x /usr/bin/intel-undervolt && ! -x /usr/local/bin/intel-undervolt ]]; then
    printf 'Note: the optional voltage offsets (CPU tab) need the intel-undervolt package, which is not installed.\n'
    printf '      On Arch-based systems: sudo pacman -S intel-undervolt   (everything else works without it)\n'
fi
# Capture the machine's early-boot RAPL values BEFORE user-session restoration.
# Do NOT start the service now: a live system may already have modified limits.
sudo install -D -o root -g root -m 0644 "$ROOT/data/$BOOT_CAPTURE_UNIT" "/etc/systemd/system/$BOOT_CAPTURE_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$BOOT_CAPTURE_UNIT"
# v2.5.4 already captured the same-boot baseline but kept it root-private.
# Republish that verified snapshot for the GUI. Never sample *current* PL1/PL2
# at upgrade time, as the user or other utilities may have changed them.
if sudo "$CPU_HELPER_DEST" firmware-boot-publish; then
    printf 'Existing same-boot PL1/PL2 snapshot is now visible to the GUI; no reboot needed.\n'
else
    printf 'Boot defaults not captured on this boot; reboot once so the early-boot capture service can publish them.\n' >&2
fi
sudo install -D -o root -g root -m 0755 "$ROOT/src/optimus_restore_wrapper.sh" "$RESTORE_WRAPPER"
sudo install -D -o root -g root -m 0755 "$ROOT/src/optimus_auto_off_wrapper.sh" "$AUTO_OFF_WRAPPER"

# Migrating from 2.5.0: its GPU-only remember switch may already be enabled.
# Capture CPU state once so the expanded option has an initial CPU snapshot.
# Never overwrite preferences saved by an existing 2.5.1 installation.
if ! root_has "$STATE_DIR/cpu-remembered-settings.json"; then
    if ! sudo "$CPU_HELPER_DEST" remember-current; then
        printf 'Warning: initial CPU preferences could not be recorded. Toggle Remember and restore off/on inside the app after installation.\n' >&2
    fi
fi

# Preserve/migrate an existing guard state.
if (( HAD_AGGRESSIVE_BLOCK )); then
    # Current split-guard layout: preserve the aggressive guard, and preserve
    # the Basic guard independently if it was also selected.
    printf 'Existing aggressive NVIDIA probe guard detected; preserving it...\n'
    if ! sudo "$HELPER_DEST" aggressive-block-on "$GPU_PCI"; then
        printf 'Warning: the aggressive guard could not be fully reapplied. Close NVIDIA-using applications and toggle it off/on once after installation.\n' >&2
    fi
    if (( HAD_BASIC_BLOCK )); then
        printf 'Existing Basic NVIDIA module block also detected; preserving it...\n'
        sudo "$HELPER_DEST" basic-block-on "$GPU_PCI" || printf 'Warning: the Basic block could not be fully reapplied.\n' >&2
    fi
elif (( HAD_AGGRESSIVE_RECOVERY )); then
    # v2.3.x single-checkbox layout: recovery records identify the old aggressive
    # guard. Map it to the new Aggressive checkbox only.
    printf 'Existing v2.3.x NVIDIA probe guard detected; migrating it to the Aggressive guard...\n'
    if sudo "$HELPER_DEST" aggressive-block-on "$GPU_PCI"; then
        if (( HAD_BASIC_BLOCK )); then
            sudo "$HELPER_DEST" basic-block-off "$GPU_PCI" || true
        fi
    else
        printf 'Warning: the existing aggressive guard could not be fully preserved right now. Close NVIDIA-using applications and toggle the aggressive guard off/on once after installation.\n' >&2
    fi
elif (( HAD_BASIC_BLOCK )); then
    printf 'Existing module-only NVIDIA block detected; preserving it as the Basic block...\n'
    if ! sudo "$HELPER_DEST" basic-block-on "$GPU_PCI"; then
        printf 'Warning: the existing Basic block could not be fully reapplied right now. Toggle it off/on once after installation.\n' >&2
    fi
fi

# Validate a staged rule before placing it in sudoers.d. A malformed live rule
# could break sudo for the machine; never write it into place first.
SUDOERS_STAGE="$(sudo mktemp /etc/sudoers.d/.thinkpad-nvidia-switch-XXXXXXXX)"
cleanup_sudoers_stage() { sudo rm -f -- "$SUDOERS_STAGE"; }
trap cleanup_sudoers_stage EXIT
printf '%s ALL=(root) NOPASSWD: %s, %s, %s, %s, %s, %s\n' "$USER_NAME" "$RESTORE_WRAPPER" "$AUTO_OFF_WRAPPER" "$PROFILE_HELPER_DEST" "$UNDERVOLT_HELPER_DEST" "$HELPER_DEST" "$CPU_HELPER_DEST" | sudo tee "$SUDOERS_STAGE" >/dev/null
sudo chmod 0440 "$SUDOERS_STAGE"
sudo "$VISUDO_BIN" -cf "$SUDOERS_STAGE"
sudo mv -f -- "$SUDOERS_STAGE" "$SUDOERS_FILE"
trap - EXIT

if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 >/dev/null 2>&1 || true
fi

printf '\nInstalled ThinkPad Nvidia Switch v%s.\n' "$RELEASE_VERSION"
printf 'Detected GPU: %s\n' "$GPU_PCI"
printf 'App files: %s\n' "$DEST"
printf 'System GPU config: %s\n' "$SYSTEM_GPU_CONFIG"
printf 'Launch the app from the application menu.\n'
printf 'Profiles: saved under ~/.config/thinkpad-nvidia-switch/profiles.json. Auto switching runs only while the app is running.\n'
printf 'CPU: installation only reads current CPU settings. Remember and restore will reapply saved CPU preferences when the app starts after login.\n'

printf 'CPU boot PL1/PL2 restore is available now if a valid same-boot capture exists; otherwise reboot once.\n'
printf 'KWin render restrictions take effect after a new KDE login, not immediately.\n'

# Confirm the sudoers rule works without a password. Cached credentials would
# hide a problem, so forget them first, then run a harmless invalid action: the
# CPU helper rejects it with its own message and changes nothing. A failure here
# is only a warning.
sudo -k
verify_output="$(sudo -n "$CPU_HELPER_DEST" selftest 2>&1 || true)"
if [[ "$verify_output" == *'Invalid CPU helper action'* ]]; then
    printf 'Verified: the passwordless helper rule is active.\n'
else
    printf 'Warning: the passwordless helper rule did not work (%s). The GUI controls will report errors; check %s.\n' "${verify_output:-no output}" "$SUDOERS_FILE" >&2
fi
