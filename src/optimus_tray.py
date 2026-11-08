#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Optimus Tray: monitor battery/GPU and manage NVIDIA module power state."""
from __future__ import annotations
import json, os, subprocess, sys, time, shlex, tempfile, base64
from types import SimpleNamespace
from collections import deque
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF, QLockFile, QProcess, QEvent, QSize
from PySide6.QtGui import QAction, QIcon, QPainter, QPen, QColor, QFont, QPixmap, QPalette
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from cpu_power import topology_status, limits_status, core_control_status, frequency_status, public_firmware_boot_limits
from battery_telemetry import battery_snapshot, battery_label_text
from voltage_status import parse_voltage_report, format_voltage_status
from undervolt_control import MIN_OFFSET_MV, WARN_BELOW_MV, validate_mv
from notification_rules import notification_settings, gpu_transition_message, power_source_message, gpu_failure_message
from layout_rules import responsive_columns, selected_tab_height
from profiles import friendly_profile_failure, short_profile_failure, permanent_profile_failure, power_source, read_store, write_store, validate_settings, new_profile, assignment, use_legacy_startup_restore
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QCheckBox, QSystemTrayIcon, QMenu, QMessageBox, QFrame, QComboBox, QDialog, QDialogButtonBox, QTextEdit, QScrollArea, QSizePolicy, QLayout, QTabWidget, QDoubleSpinBox, QSpinBox, QGridLayout, QListWidget, QLineEdit, QFormLayout)

def configured_gpu_address():
    # Installer writes this file after detecting the local NVIDIA display controller.
    # The environment override is retained for development/testing only.
    override = os.environ.get('OPTIMUS_PCI')
    if override:
        return override
    config_file = Path(__file__).resolve().parent / 'gpu-pci-address'
    try:
        address = config_file.read_text().strip().lower()
        if address:
            return address
    except OSError:
        pass
    # Source-tree development fallback: use the same strict detector as the installer.
    detector = Path(__file__).resolve().parent / 'detect_nvidia_gpu.py'
    try:
        result = subprocess.run([sys.executable, str(detector)], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return 'GPU_NOT_DETECTED'


GPU = configured_gpu_address()
PCI = Path('/sys/bus/pci/devices') / GPU
POWER_STATE = PCI / 'power_state'
POWER_CONTROL = PCI / 'power' / 'control'
BASIC_BLOCK_FILE = Path('/etc/modprobe.d/optimus-tray-block.conf')
AGGRESSIVE_BLOCK_FILE = Path('/etc/modprobe.d/optimus-tray-probe-guard.conf')
NVIDIA_MODPROBE_STATE = Path('/var/lib/thinkpad-nvidia-switch/nvidia-modprobe-modes.json')
DISCOVERY_STATE = Path('/var/lib/thinkpad-nvidia-switch/nvidia-discovery-modes.json')
DRIVER_OVERRIDE_STATE = Path('/var/lib/thinkpad-nvidia-switch/pci-driver-override.json')
PCI_MASK_STATE = Path('/var/lib/thinkpad-nvidia-switch/pci-config-mask.json')
DETACHED_MARKER = Path('/var/lib/thinkpad-nvidia-switch/pci-detached.json')
APP_NAME = 'ThinkPad Nvidia Switch'
APP_VERSION = '2.6.9'
APP_ICON = Path(__file__).resolve().parent.parent / 'assets' / 'thinkpad-nvidia-switch.svg'
ASSET_DIR = Path(__file__).resolve().parent.parent / 'assets'
CHECKBOX_CHECKMARK = (ASSET_DIR / 'checkbox-checkmark.png').as_posix()
SPIN_UP_ARROW = (ASSET_DIR / 'spin-up.png').as_posix()
SPIN_DOWN_ARROW = (ASSET_DIR / 'spin-down.png').as_posix()
MODULES = ['nvidia_drm', 'nvidia_modeset', 'nvidia_uvm', 'nvidia']
CONFIG_DIR = Path.home() / '.config' / 'thinkpad-nvidia-switch'
LOG_DIR = Path.home() / '.local' / 'state' / 'thinkpad-nvidia-switch'
LOG_FILE = LOG_DIR / 'operations.log'
DETAILED_LOG_FILE = LOG_DIR / 'detailed.log'
DETAIL_PREFIX = 'TPNS_DETAIL|'
PREFS_FILE = CONFIG_DIR / 'settings.json'
PROFILES_FILE = CONFIG_DIR / 'profiles.json'
PROFILE_HELPER = Path('/usr/local/libexec/thinkpad-nvidia-switch-profile-apply')
AUTOSTART_FILE = Path.home() / '.config' / 'autostart' / 'thinkpad-nvidia-switch.desktop'
INSTALLED_HELPER = Path('/usr/local/libexec/thinkpad-nvidia-switch-helper')
AUTO_RESTORE_WRAPPER = Path('/usr/local/libexec/thinkpad-nvidia-switch-auto-off')
RESTORE_WRAPPER = Path('/usr/local/libexec/thinkpad-nvidia-switch-restore')
CPU_HELPER = Path('/usr/local/libexec/thinkpad-nvidia-switch-cpu-helper')
UNDERVOLT_HELPER = Path('/usr/local/libexec/thinkpad-nvidia-switch-undervolt')
NVIDIA_DEVICE_PREFIXES = ('/dev/nvidia',)
# Outer margin of the scrolling content; the tab widget is this much narrower than the viewport.
CONTENT_MARGIN = 14
# How long to wait for a system-tray host to appear at startup (autostart can beat the panel).
TRAY_WAIT_SECONDS = 20.0
# A failed *automatic* profile is retried once after this delay (settling hotplug, other power tools).
AUTO_PROFILE_RETRY_MS = 10000

# One instance per desktop user. QLockFile provides the hard mutual-exclusion
# guarantee; QLocalServer lets a later launcher ask the existing instance to
# present its window instead of starting a second tray/controller process.
INSTANCE_KEY = f'thinkpad-nvidia-switch-{os.getuid()}'
INSTANCE_SERVER_NAME = INSTANCE_KEY


def instance_lock_file():
    """Per-user location of the single-instance lock.

    Prefer the session's runtime directory; never fall back to a shared,
    world-writable directory such as /tmp (another user could squat the name).
    """
    for candidate in (os.environ.get('XDG_RUNTIME_DIR'), f'/run/user/{os.getuid()}'):
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
            return Path(candidate) / f'{INSTANCE_KEY}.lock'
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    return CONFIG_DIR / f'{INSTANCE_KEY}.lock'


def _notify_existing_instance(show_window=True, timeout_ms=1500):
    """Tell an already-running instance to present itself, if reachable.

    The lock is authoritative. This IPC channel is only a convenience so a
    second click on the launcher raises the existing window. During the tiny
    startup interval before the first instance begins listening, retry briefly.
    """
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    payload = b'SHOW\n' if show_window else b'PING\n'
    while True:
        sock = QLocalSocket()
        sock.connectToServer(INSTANCE_SERVER_NAME)
        if sock.waitForConnected(120):
            sock.write(payload)
            sock.flush()
            sock.waitForBytesWritten(250)
            sock.disconnectFromServer()
            return True
        sock.abort()
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _configure_instance_server(server):
    """Restrict the local control socket to this user where Qt supports it."""
    try:
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    except (AttributeError, TypeError):
        pass


# Project-owned visual palette. The colors intentionally avoid reproducing
# NVIDIA Control Panel or Lenovo Vantage layouts/branding; they are simply
# restrained status accents for this application's own interface.
COLOR_BG = '#171a1f'
COLOR_SURFACE = '#20242a'
COLOR_SURFACE_ALT = '#262b32'
COLOR_BORDER = '#343b44'
COLOR_TEXT = '#f3f5f7'
COLOR_MUTED = '#98a2ad'
COLOR_RED = '#e33d45'
COLOR_GREEN = '#78bd43'
COLOR_AMBER = '#d9a441'

GLOBAL_STYLE = f"""
QMainWindow, QDialog {{
    background: {COLOR_BG};
    color: {COLOR_TEXT};
}}
QWidget {{
    color: {COLOR_TEXT};
    font-size: 13px;
}}
QMenuBar {{
    background: {COLOR_BG};
    color: {COLOR_TEXT};
    border-bottom: 1px solid {COLOR_BORDER};
    padding: 2px;
}}
QMenuBar::item {{
    padding: 6px 10px;
    border-radius: 5px;
}}
QMenuBar::item:selected {{ background: {COLOR_SURFACE_ALT}; }}
QMenu {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    padding: 5px;
}}
QMenu::item {{ padding: 7px 24px 7px 34px; border-radius: 4px; }}
QMenu::item:selected {{ background: {COLOR_SURFACE_ALT}; }}
QMenu::indicator {{
    width: 16px;
    height: 16px;
    margin-left: 7px;
    background: #2a3037;
    border: 2px solid #8793a0;
    border-radius: 4px;
}}
QMenu::indicator:non-exclusive:unchecked:selected {{
    background: #343b44;
    border-color: #c9d0d8;
}}
QMenu::indicator:non-exclusive:checked {{
    background: {COLOR_RED};
    border-color: #f06a72;
    image: url({CHECKBOX_CHECKMARK});
}}
QMenu::indicator:non-exclusive:checked:selected {{
    background: #ef4c54;
    border-color: #ff9499;
    image: url({CHECKBOX_CHECKMARK});
}}
QMenu::indicator:non-exclusive:disabled {{
    background: #1b1f24;
    border-color: #48515b;
}}
QTabWidget::pane {{
    background: {COLOR_BG};
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    top: -1px;
}}
QTabBar::tab {{
    color: {COLOR_MUTED};
    padding: 9px 32px;
    margin-right: 3px;
    border: 1px solid {COLOR_BORDER};
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    background: {COLOR_SURFACE};
}}
QTabBar::tab:selected {{
    color: {COLOR_TEXT};
    background: {COLOR_SURFACE_ALT};
    border-top: 2px solid {COLOR_RED};
}}
QSpinBox, QDoubleSpinBox, QLineEdit {{
    background: {COLOR_SURFACE_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px;
    min-height: 24px;
    selection-background-color: #4b2b31;
}}
QSpinBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled {{
    background: #1b1f24;
    color: #6b7580;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 22px;
    border-left: 1px solid {COLOR_BORDER};
    border-top-right-radius: 5px;
    background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 22px;
    border-left: 1px solid {COLOR_BORDER};
    border-bottom-right-radius: 5px;
    background: transparent;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background: #343b44; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({SPIN_UP_ARROW}); width: 12px; height: 12px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({SPIN_DOWN_ARROW}); width: 12px; height: 12px; }}
QListWidget {{
    background: #121519;
    border: 1px solid {COLOR_BORDER};
    border-radius: 7px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{ padding: 7px 9px; border-radius: 5px; }}
QListWidget::item:hover:!selected {{ background: {COLOR_SURFACE_ALT}; }}
QListWidget::item:selected {{
    background: #342328;
    color: {COLOR_TEXT};
}}
QScrollArea#mainScrollArea {{
    background: {COLOR_BG};
    border: none;
}}
QWidget#contentRoot {{ background: {COLOR_BG}; }}
QFrame#card {{
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 10px;
}}
QFrame#headerCard {{
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-left: 4px solid {COLOR_RED};
    border-radius: 10px;
}}
QLabel#eyebrow {{
    color: {COLOR_MUTED};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel#title {{ font-size: 23px; font-weight: 750; }}
QLabel#subtitle, QLabel#muted {{ color: {COLOR_MUTED}; }}
QLabel#sectionTitle {{ font-size: 14px; font-weight: 700; }}
QPushButton {{
    background: {COLOR_SURFACE_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 7px;
    padding: 8px 14px;
    min-height: 20px;
}}
QPushButton:hover {{ border-color: #58616d; background: #2c323a; }}
QPushButton:pressed {{ background: #1d2126; }}
QPushButton#powerOnButton {{
    background: {COLOR_GREEN};
    color: #10150d;
    border-color: {COLOR_GREEN};
    font-weight: 750;
}}
QPushButton#powerOnButton:hover {{ background: #86ca50; border-color: #86ca50; }}
QPushButton#powerOffButton {{
    background: {COLOR_RED};
    color: white;
    border-color: {COLOR_RED};
    font-weight: 750;
}}
QPushButton#powerOffButton:hover {{ background: #eb4a52; border-color: #eb4a52; }}
/* Do not change the font weight here: Qt sizes a button for its normal text, so a bold
   :checked label overflows its button and is clipped. Border and background mark the choice. */
QPushButton#logModeButton:checked {{
    border-color: {COLOR_RED};
    background: #342328;
}}
QComboBox {{
    background: {COLOR_SURFACE_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 20px;
}}
QComboBox:hover {{ border-color: #58616d; }}
QComboBox QAbstractItemView {{
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: #342328;
}}
QCheckBox {{
    spacing: 10px;
    font-weight: 600;
    min-height: 28px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    background: #2a3037;
    border: 2px solid #8793a0;
    border-radius: 5px;
}}
QCheckBox::indicator:unchecked:hover {{
    background: #343b44;
    border-color: #c9d0d8;
}}
QCheckBox::indicator:checked {{
    background: {COLOR_RED};
    border-color: #f06a72;
    image: url({CHECKBOX_CHECKMARK});
}}
QCheckBox::indicator:checked:hover {{
    background: #ef4c54;
    border-color: #ff9499;
    image: url({CHECKBOX_CHECKMARK});
}}
QCheckBox::indicator:disabled {{
    background: #1b1f24;
    border-color: #48515b;
}}
QCheckBox::indicator:focus {{
    border-color: #e7ebef;
}}
QCheckBox:focus {{ color: #ffffff; }}
QTextEdit {{
    background: #121519;
    border: 1px solid {COLOR_BORDER};
    border-radius: 7px;
    padding: 6px;
    selection-background-color: #4b2b31;
}}
QToolTip {{
    background: {COLOR_SURFACE_ALT};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    padding: 4px;
}}
"""


def dark_palette():
    """Palette matching GLOBAL_STYLE.

    The stylesheet restyles most widgets, but anything it does not cover (spin
    box arrows, message-box icons, tooltips, new widgets) is drawn by the
    platform style from the application palette. Without this, the dark window
    would inherit a light system palette: light text on a light field.
    """
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: COLOR_BG,
        QPalette.ColorRole.WindowText: COLOR_TEXT,
        QPalette.ColorRole.Base: '#121519',
        QPalette.ColorRole.AlternateBase: COLOR_SURFACE,
        QPalette.ColorRole.Text: COLOR_TEXT,
        QPalette.ColorRole.Button: COLOR_SURFACE_ALT,
        QPalette.ColorRole.ButtonText: COLOR_TEXT,
        QPalette.ColorRole.BrightText: '#ffffff',
        QPalette.ColorRole.ToolTipBase: COLOR_SURFACE_ALT,
        QPalette.ColorRole.ToolTipText: COLOR_TEXT,
        QPalette.ColorRole.PlaceholderText: COLOR_MUTED,
        QPalette.ColorRole.Highlight: '#4b2b31',
        QPalette.ColorRole.HighlightedText: '#ffffff',
        QPalette.ColorRole.Link: '#8ab4ff',
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#6b7580'))
    return palette


def _set_text(widget, text):
    """setText() only when the text differs: Qt repaints (and may re-lay-out) even for an identical string."""
    if widget.text() != text:
        widget.setText(text)


def _set_spin_range(spin, minimum, maximum):
    """setRange() only when the range differs: an identical range still re-lays-out the whole page."""
    if spin.minimum() != minimum or spin.maximum() != maximum:
        spin.setRange(minimum, maximum)


def _set_spin_value(spin, value):
    """setValue() only when the value differs (an identical value still repaints the spin box)."""
    if spin.value() != value:
        spin.setValue(value)


def _set_gpu_state_color(label, state):
    state_lower = str(state).strip().lower()
    if state == 'D0':
        color = COLOR_GREEN
    elif state_lower == 'd3cold':
        color = COLOR_RED
    else:
        color = COLOR_AMBER
    label.setStyleSheet(f'color:{color}; font-size:24px; font-weight:800;')


def _set_power_button_style(button, state):
    # The accent describes the action the button will perform next.
    button.setObjectName('powerOffButton' if state == 'D0' else 'powerOnButton')
    style = button.style()
    style.unpolish(button)
    style.polish(button)



def read_text(path: Path, default=''):
    try: return path.read_text().strip()
    except (OSError, PermissionError): return default


def path_exists(path):
    """Like Path.exists(), but False instead of raising for unreadable locations.

    Recovery records live in a root-owned directory under /var/lib. For the
    unprivileged GUI, Path.exists() raises PermissionError there when the
    directory is not world-searchable, which would abort every refresh.
    """
    try:
        return Path(path).exists()
    except OSError:
        return False


def battery_reading():
    """Return discharge watts (positive means discharging), charge %, and label."""
    root = Path('/sys/class/power_supply')
    for p in root.glob('*'):
        typ = read_text(p / 'type')
        if typ != 'Battery':
            continue
        cap = read_text(p / 'capacity')
        status = read_text(p / 'status', 'Unknown')
        power = read_text(p / 'power_now')
        if power:
            try:
                watts = abs(float(power)) / 1_000_000
            except ValueError:
                watts = None
            if watts is not None:
                return (watts if status == 'Discharging' else 0.0), (int(cap) if cap.isdigit() else None), status
        current = read_text(p / 'current_now')
        voltage = read_text(p / 'voltage_now')
        if current and voltage:
            try:
                watts = abs(float(current) * float(voltage)) / 1_000_000_000_000
            except ValueError:
                continue
            return (watts if status == 'Discharging' else 0.0), (int(cap) if cap.isdigit() else None), status
    return None, None, 'Battery telemetry unavailable'



def load_preferences():
    """Load UI settings, retaining old installations' GPU/autostart preferences.

    Ignore invalid or obsolete combo values rather than leaving the selectors
    blank or starting a timer with an unsupported sampling interval.
    """
    try:
        data = json.loads(PREFS_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError, TypeError):
        data = {}
    window = data.get('history_window')
    interval = data.get('history_interval')
    return {
        'autostart': bool(data.get('autostart', False)),
        'restore_state': bool(data.get('restore_state', False)),
        'auto_restore_apps': bool(data.get('auto_restore_apps', False)),
        'history_window': window if window in HistoryGraph.PERIODS else 'Last 5 minutes',
        'history_interval': interval if interval in HistoryGraph.SAMPLE_INTERVALS else '10 seconds',
        'selected_tab': data.get('selected_tab', 0) if data.get('selected_tab', 0) in (0, 1, 2) else 0,
        'restore_before_auto': data.get('restore_before_auto') if type(data.get('restore_before_auto')) is bool else None,
        'notifications': notification_settings(data.get('notifications')),
    }


def save_preferences(data):
    """Atomically persist UI settings with private permissions.

    Every change to the history selectors is saved immediately. Using a
    same-directory temporary file prevents an interrupted write from
    corrupting an otherwise valid settings.json.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    log_detail(f'FILE WRITE: {PREFS_FILE} (application preferences)')
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         prefix='.settings-', suffix='.tmp',
                                         dir=CONFIG_DIR, delete=False) as out:
            temp_path = Path(out.name)
            json.dump(data, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_path, PREFS_FILE)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


_LOG_LINE_COUNTS = {}
# Trim in batches rather than on every line once the cap is reached.
LOG_TRIM_SLACK = 0.1


def _trim_log(path: Path, max_lines: int):
    """Keep only the newest ``max_lines`` lines; returns the resulting line count."""
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        temp = path.with_name(path.name + '.tmp')
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                out.write('\n'.join(lines) + '\n')
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return len(lines)


def _append_log(path: Path, message: str, max_lines: int):
    """Append one timestamped line and cap the persistent log size.

    The line count is tracked in memory, so the file is only re-read when it
    has grown noticeably past the cap (and the count is then re-synchronised,
    which also covers the log being cleared from the log window).
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            LOG_DIR.chmod(0o700)
        except OSError:
            pass
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with path.open('a', encoding='utf-8') as fh:
            fh.write(f'[{stamp}] {message}\n')
        try:
            path.chmod(0o600)
        except OSError:
            pass
        try:
            count = _LOG_LINE_COUNTS.get(path)
            count = None if count is None else count + 1
            if count is None or count > max_lines * (1 + LOG_TRIM_SLACK):
                count = _trim_log(path, max_lines)
            _LOG_LINE_COUNTS[path] = count
        except OSError:
            _LOG_LINE_COUNTS.pop(path, None)
    except OSError:
        pass


def log_detail(message):
    """Append low-level diagnostic activity to the detailed log only."""
    _append_log(DETAILED_LOG_FILE, message, 20000)


def log_operation(message):
    """Keep the original concise log and mirror its events into detailed view."""
    _append_log(LOG_FILE, message, 5000)
    _append_log(DETAILED_LOG_FILE, f'EVENT: {message}', 20000)


def guard_label(basic, aggressive):
    """Name the active NVIDIA guards for the activity log: 'Basic', 'Aggressive', 'Basic+Aggressive' or 'none'."""
    return '+'.join(name for name, active in (('Basic', basic), ('Aggressive', aggressive)) if active) or 'none'


def deep_undervolt_values(values):
    """The offsets (mV) that are below the extra-confirmation threshold."""
    return [v for v in values if v is not None and v < WARN_BELOW_MV]


def aggressive_undervolt_box(parent, values, for_profile=False):
    """Build (but do not show) the warning for offsets below WARN_BELOW_MV; "No" is the default."""
    cpu, cache, gpu = values
    deep = len(deep_undervolt_values(values))
    summary = f'CPU {cpu} mV, cache {cache} mV and Intel graphics {gpu} mV'
    if for_profile:
        title = 'Save aggressive voltage offsets?'
        text = (f'This profile sets {summary}.\n\n'
                f'{deep} of these {"is" if deep == 1 else "are"} below {WARN_BELOW_MV:g} mV. Profiles can be applied automatically, '
                'at login and whenever the charger is connected or disconnected, so a setting that crashes this laptop '
                'will crash it every time. Test the offsets manually, with a stress test, before saving them in a profile.\n\n'
                'Save these offsets anyway?')
    else:
        title = 'Aggressive voltage offsets'
        text = (f'Apply {summary}?\n\n'
                f'WARNING: {deep} of these {"is" if deep == 1 else "are"} below {WARN_BELOW_MV:g} mV. Few processors are stable '
                'that far down. A failure usually means an instant freeze or reboot, and unsaved work or open files can be lost. '
                'Lower the voltage in small steps and stress-test each one instead of jumping here. '
                'Save your work before proceeding.\n\nApply anyway?')
    box = QMessageBox(QMessageBox.Icon.Warning, title, text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    box.setEscapeButton(QMessageBox.StandardButton.No)
    return box


def confirm_aggressive_undervolt(parent, values, for_profile=False):
    box = aggressive_undervolt_box(parent, values, for_profile)
    box.exec()
    return box.clickedButton() is box.button(QMessageBox.StandardButton.Yes)


def _split_helper_stderr(text):
    """Separate helper audit lines from normal user-facing stderr."""
    details, normal = [], []
    for line in (text or '').splitlines():
        if line.startswith(DETAIL_PREFIX):
            details.append(line[len(DETAIL_PREFIX):])
        else:
            normal.append(line)
    return details, '\n'.join(normal).strip()


def _log_command_result(command, process, context='background command'):
    """Record a subprocess result, including structured helper audit output."""
    log_detail(f'COMMAND RESULT ({context}): exit={process.returncode}: {shlex.join([str(x) for x in command])}')
    helper_details, normal_stderr = _split_helper_stderr(process.stderr or '')
    for entry in helper_details:
        log_detail(f'HELPER: {entry}')
    stdout = (process.stdout or '').strip()
    if stdout:
        for line in stdout.splitlines():
            log_detail(f'STDOUT: {line}')
    if normal_stderr:
        for line in normal_stderr.splitlines():
            log_detail(f'STDERR: {line}')
    return normal_stderr


def desktop_entry():
    script = Path(__file__).resolve()
    return f"""[Desktop Entry]\nType=Application\nName={APP_NAME}\nComment=Manage ThinkPad NVIDIA GPU power state\nExec="{sys.executable}" "{script}" --minimized\nIcon=thinkpad-nvidia-switch\nTerminal=false\nX-GNOME-Autostart-enabled=true\nStartupNotify=false\n"""


class BusyButton(QPushButton):
    """A button that shows a spinning arc beside its label while an operation is in progress.

    The arc is painted over the normal button, so the stylesheet (colours, hover) keeps working. The
    animation timer only runs while the button is busy.
    """
    FRAME_MS = 40
    ARC_SIZE = 18

    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self._busy = False
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._advance)

    def isBusy(self):
        return self._busy

    def setBusy(self, busy):
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._angle = 0
            self._timer.start()
            self.setCursor(Qt.CursorShape.BusyCursor)
        else:
            self._timer.stop()
            self.unsetCursor()
        self.update()

    def _advance(self):
        self._angle = (self._angle - 24) % 360          # clockwise
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._busy:
            return
        size = self.ARC_SIZE
        text_width = self.fontMetrics().horizontalAdvance(self.text())
        rect = QRectF((self.width() - text_width) / 2 - size - 12, (self.height() - size) / 2, size, size)
        color = self.palette().buttonText().color()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QColor(color)
        track.setAlpha(70)
        painter.setPen(QPen(track, 3))
        painter.drawEllipse(rect)
        painter.setPen(QPen(color, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, self._angle * 16, 100 * 16)
        painter.end()


class HistoryGraph(QWidget):
    """Battery discharge history with hover inspection and selectable time window."""
    PERIODS = {"Last 1 minute": 60, "Last 5 minutes": 300,
               "Last 10 minutes": 600, "Last 15 minutes": 900,
               "Last 30 minutes": 1800}
    SAMPLE_INTERVALS = {"2 seconds": 2000, "5 seconds": 5000,
                        "10 seconds": 10000, "15 seconds": 15000,
                        "30 seconds": 30000, "60 seconds": 60000}

    def __init__(self, title, color, suffix, parent=None):
        super().__init__(parent)
        self.title = title
        self.color = QColor(color)
        self.suffix = suffix
        self.values = deque(maxlen=1200)  # 40 minutes at the 2-second sampling interval
        self.period_seconds = 300
        self.hover_index = None
        self.plot_rect = None
        self.setMinimumHeight(170)
        self.setMouseTracking(True)
        self.setToolTip("Move the cursor over the graph to inspect a sample")

    def set_period(self, label):
        self.period_seconds = self.PERIODS.get(label, 300)
        self.hover_index = None
        self.update()

    def add_value(self, value):
        self.values.append((time.time(), None if value is None else max(0.0, float(value))))
        self.update()

    def _visible_samples(self):
        cutoff = time.time() - self.period_seconds
        return [(stamp, value) for stamp, value in self.values if stamp >= cutoff]

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(8, 8, -8, -8)
        p.setPen(QColor('#aab2bd'))
        p.drawText(r.left(), r.top() + 12, self.title)
        plot = r.adjusted(0, 22, 0, -20)
        self.plot_rect = plot
        p.setPen(QPen(QColor('#d5d9df'), 1, Qt.PenStyle.DashLine))
        for n in range(1, 4):
            y = plot.top() + plot.height() * n / 4
            p.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
        samples = self._visible_samples()
        self._paint_samples = samples
        if samples:
            valid = [value for _, value in samples if value is not None]
            maxv = max(max(valid, default=1.0), 1.0)
            pts = []
            count = len(samples)
            for i, (_, value) in enumerate(samples):
                if value is None:
                    pts.append(None)
                else:
                    x = plot.left() + i * plot.width() / max(1, count - 1)
                    y = plot.bottom() - (value / maxv) * plot.height()
                    pts.append(QPointF(x, y))
            # Stem chart: draw one vertical bar per sample instead of connecting
            # samples with a line. This makes each two-second update explicit.
            p.setPen(QPen(self.color, 2))
            for point in pts:
                if point is not None:
                    p.drawLine(QPointF(point.x(), plot.bottom()), point)
            p.setPen(QColor('#667085'))
            p.drawText(int(plot.left()), int(plot.bottom() + 14), f'{self.period_seconds // 60 if self.period_seconds >= 60 else 1} min window')
            p.drawText(int(plot.right() - 75), int(plot.top() + 10), f'{maxv:.1f} {self.suffix}')
            if self.hover_index is not None and 0 <= self.hover_index < len(samples):
                stamp, value = samples[self.hover_index]
                x = plot.left() + self.hover_index * plot.width() / max(1, len(samples) - 1)
                p.setPen(QPen(QColor('#344054'), 1, Qt.PenStyle.DashLine))
                p.drawLine(int(x), int(plot.top()), int(x), int(plot.bottom()))
                if value is not None:
                    y = plot.bottom() - (value / maxv) * plot.height()
                    p.setBrush(self.color); p.setPen(QPen(QColor('#ffffff'), 1))
                    p.drawEllipse(QPointF(x, y), 4, 4)
        else:
            p.setPen(QColor('#667085'))
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, 'Collecting battery history…')
        p.end()

    def mouseMoveEvent(self, event):
        samples = getattr(self, '_paint_samples', [])
        plot = self.plot_rect
        if samples and plot and plot.left() <= event.position().x() <= plot.right():
            ratio = (event.position().x() - plot.left()) / max(1, plot.width())
            index = round(ratio * (len(samples) - 1))
            self.hover_index = max(0, min(len(samples) - 1, index))
            stamp, value = samples[self.hover_index]
            when = time.strftime('%H:%M:%S', time.localtime(stamp))
            text = f'{when} — {"No reading" if value is None else f"{value:.2f} {self.suffix}"}'
            self.setToolTip(text)
            self.update()
        else:
            self.hover_index = None
            self.setToolTip('Move the cursor over the graph to inspect a sample')
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover_index = None
        self.update()
        super().leaveEvent(event)


class ContentSizedTabWidget(QTabWidget):
    """Size the tab frame from the *current page's layout*, not rendered geometry.

    Qt's ordinary QTabWidget bases its sizeHint on its largest page (the CPU
    page in this application). The old workaround repeatedly read the bottom
    of already-compressed child widgets and set a fixed height from it. That
    formed a shrink feedback loop, eventually overlapping controls. Here all
    geometry is advisory: no fixed heights are ever imposed on a tab page.

    QTabWidget also forwards height-for-width to its stacked pages and answers
    with the *tallest* page. A parent box layout trusts that answer over
    sizeHint(), which left a large empty area below the GPU and Profiles pages.
    Height-for-width is therefore disabled here; sizeHint() is the only source.
    """

    # Left + right border of the tab pane; the page is this much narrower than the tab widget.
    PAGE_FRAME_WIDTH = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._available_page_width = 0

    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, width):
        return -1

    def setAvailablePageWidth(self, width):
        """Width the tab widget itself will have (the parent's usable width)."""
        width = max(0, int(width))
        if self._available_page_width != width:
            self._available_page_width = width
            self.updateGeometry()

    def _current_page_height(self):
        page = self.currentWidget()
        if page is None or page.layout() is None:
            return None
        content_layout = page.layout()
        # Unlike QWidget.geometry(), sizeHint() does NOT collapse just because
        # the previous layout pass assigned too little height to the page.
        # This getter must not invalidate anything: every invalidate() queues a
        # LayoutRequest, the parent layout then asks for sizeHint() again, which
        # would invalidate again, a loop that kept the window repainting ~500 times
        # a second. Stale caches are refreshed by remeasure_page() instead.
        height = max(content_layout.minimumSize().height(),
                     content_layout.sizeHint().height())
        if self._available_page_width and content_layout.hasHeightForWidth():
            # Word-wrapped labels: the exact wrapped height at the page's real
            # width is tighter than QLabel's generous sizeHint, and (unlike a
            # rendered-geometry measurement) cannot feed back on itself.
            width = max(1, self._available_page_width - self.PAGE_FRAME_WIDTH)
            wrapped = content_layout.heightForWidth(width)
            if wrapped >= 0:
                height = wrapped
        return height

    def remeasure_page(self):
        """Throw away the current page's cached layout measurements (once, on an explicit trigger).

        A hidden page does not invalidate its layout when its labels change, so the cache can be
        stale when the page is shown, resized or selected; call this at those moments.
        """
        page = self.currentWidget()
        if page is not None and page.layout() is not None:
            page.layout().invalidate()

    def sizeHint(self):
        hint = super().sizeHint()
        height = self._current_page_height()
        if height is None:
            return hint
        # Account for the tab strip, inner tab frame, and style-dependent
        # margins. A few extra pixels are safer than clipping buttons.
        return QSize(hint.width(), selected_tab_height(height, self.tabBar().sizeHint().height()))

    def minimumSizeHint(self):
        hint = self.sizeHint()
        # The top-level scroll area handles vertical overflow; do not allow
        # non-selected tab pages to force their minimum size onto this one.
        return QSize(min(hint.width(), 440), hint.height())


class ResponsiveSectionArea(QWidget):
    """Two columns only when each card has room; stacks again while shrinking.

    Use the *scroll viewport* width, not this widget's own width. The latter can
    retain its previous large minimum size after a two-column layout, which
    was the cause of clipped controls in v2.6.4.
    """

    def __init__(self, min_card_width=620, parent=None):
        super().__init__(parent)
        self.min_card_width = max(300, int(min_card_width))
        self._available_width = 0
        self._items = []
        self._columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(10)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def addSection(self, widget):
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._items.append(widget)
        self._reflow(force=True)

    def setAvailableWidth(self, width):
        self._available_width = max(0, int(width))
        self._reflow()

    def _reflow(self, force=False):
        columns = responsive_columns(self._available_width, self.min_card_width, self.grid.horizontalSpacing())
        if not force and columns == self._columns:
            return False
        # Clear all stretch rules from the *previous* arrangement. A dangling
        # row-stretch made the GPU/profile page distribute massive empty space.
        for row in range(self.grid.rowCount()):
            self.grid.setRowStretch(row, 0)
        for col in range(self.grid.columnCount()):
            self.grid.setColumnStretch(col, 0)
        while self.grid.count():
            self.grid.takeAt(0)
        self._columns = columns
        for index, widget in enumerate(self._items):
            row, col = divmod(index, columns)
            self.grid.addWidget(widget, row, col, Qt.AlignmentFlag.AlignTop)
        for col in range(columns):
            self.grid.setColumnStretch(col, 1)
        self.grid.invalidate()
        self.updateGeometry()
        return True


def _proc_pids():
    try:
        return [int(name) for name in os.listdir('/proc') if name.isdigit()]
    except OSError:
        return []


def _proc_comm(pid):
    return read_text(Path('/proc') / str(pid) / 'comm', '')


def _proc_ppid(pid):
    stat = read_text(Path('/proc') / str(pid) / 'stat', '')
    if not stat:
        return None
    try:
        # The comm field may contain spaces/parentheses, so split after the
        # final ')' before reading the state/PPID fields.
        tail = stat.rsplit(')', 1)[1].strip().split()
        return int(tail[1])
    except (ValueError, IndexError):
        return None


def _proc_starttime(pid):
    stat = read_text(Path('/proc') / str(pid) / 'stat', '')
    if not stat:
        return None
    try:
        tail = stat.rsplit(')', 1)[1].strip().split()
        return int(tail[19])
    except (ValueError, IndexError):
        return None


def _proc_cgroup(pid):
    text = read_text(Path('/proc') / str(pid) / 'cgroup', '')
    for line in text.splitlines():
        if line.startswith('0::'):
            return line[3:]
    return None


def _app_scope(path):
    """Return the nearest systemd app scope in a cgroup path, if present."""
    if not path:
        return None
    parts = [part for part in path.split('/') if part]
    # Prefer the outermost application scope so child/helper scopes remain
    # part of the same application's lifetime where possible.
    for i, part in enumerate(parts):
        if part.endswith('.scope') and part.startswith('app-'):
            return '/' + '/'.join(parts[:i + 1])
    return None


SYSTEM_PROCESS_NAMES = {
    'systemd', 'systemd-logind', 'plasmashell', 'kwin_wayland', 'kwin_x11',
    'Xwayland', 'Xorg', 'dbus-daemon', 'dbus-broker', 'dbus-broker-launch',
    'pipewire', 'wireplumber', 'pulseaudio', 'ksmserver', 'startplasma-x11',
    'startplasma-wayland', 'sddm-helper', 'sshd', 'bash', 'fish', 'zsh',
}


_SCOPE_DESCRIPTION_CACHE = {}


def _systemd_scope_description(scope):
    """Return the desktop-provided description for an application scope."""
    if not scope:
        return None
    if scope in _SCOPE_DESCRIPTION_CACHE:
        return _SCOPE_DESCRIPTION_CACHE[scope]
    unit = scope.rstrip('/').split('/')[-1]
    description = None
    try:
        command = ['systemctl', '--user', 'show', unit, '--property=Description', '--value']
        log_detail(f'COMMAND START (application identification): {shlex.join(command)}')
        p = subprocess.run(command, capture_output=True, text=True, timeout=0.4)
        _log_command_result(command, p, 'application identification')
        if p.returncode == 0:
            value = (p.stdout or '').strip()
            if value:
                description = value
    except (OSError, subprocess.TimeoutExpired):
        pass
    _SCOPE_DESCRIPTION_CACHE[scope] = description
    return description


def _friendly_process_name(name):
    """Convert common helper process names into human-friendly application names."""
    if not name:
        return None
    lower = name.lower()
    known = {
        'steam': 'Steam',
        'steamwebhelper': 'Steam',
        'steamwebhelper.sh': 'Steam',
        'firefox': 'Firefox',
        'firefox-bin': 'Firefox',
        'spectacle': 'Spectacle',
        'obs': 'OBS Studio',
        'obs64': 'OBS Studio',
        'vlc': 'VLC',
        'mpv': 'mpv',
    }
    if lower in known:
        return known[lower]
    return name


def _application_anchor(pid):
    """Identify the application session responsible for an NVIDIA user PID.

    Prefer the systemd app scope created by the desktop. This keeps Steam's
    helper processes associated with Steam even when the particular helper
    that touched /dev/nvidia disappears. Fall back to a stable process-tree
    root on desktops that do not expose app scopes.
    """
    current = pid
    seen = set()
    best = pid
    best_name = _proc_comm(pid) or f'PID {pid}'
    while current and current not in seen and current > 1:
        seen.add(current)
        cg = _proc_cgroup(current)
        scope = _app_scope(cg)
        if scope:
            description = _systemd_scope_description(scope)
            return {'scope': scope, 'root_pid': current,
                    'root_starttime': _proc_starttime(current),
                    'label': description or _friendly_process_name(best_name)}
        name = _proc_comm(current)
        if name and name not in SYSTEM_PROCESS_NAMES:
            best = current
            best_name = name
        parent = _proc_ppid(current)
        if parent is None or parent == current:
            break
        current = parent
    return {'scope': None, 'root_pid': best,
            'root_starttime': _proc_starttime(best),
            'label': _friendly_process_name(best_name)}


def _process_in_scope(pid, scope):
    cg = _proc_cgroup(pid)
    return bool(cg and (cg == scope or cg.startswith(scope.rstrip('/') + '/')))


def _anchor_alive(anchor):
    pid = anchor.get('root_pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    scope = anchor.get('scope')
    if scope:
        # A scope can contain multiple processes; it is considered alive if
        # any process still belongs to that scope or one of its child cgroups.
        for candidate in _proc_pids():
            if _process_in_scope(candidate, scope):
                return True
        return False
    return _proc_starttime(pid) == anchor.get('root_starttime')


def _anchor_with_origin(anchor, pid, name):
    """Attach the first NVIDIA-using process to an application anchor."""
    if 'origin_pid' not in anchor:
        anchor = dict(anchor)
        anchor['origin_pid'] = pid
        anchor['origin_starttime'] = _proc_starttime(pid)
        anchor['origin_name'] = _friendly_process_name(name) or name or f'PID {pid}'
    return anchor


def _anchor_key(anchor):
    if anchor.get('scope'):
        return (anchor.get('scope'),)
    return (None, anchor.get('root_pid'), anchor.get('root_starttime'))


def nvidia_users():
    """Return {pid: process_name} for processes holding NVIDIA device nodes."""
    users = {}
    for pid in _proc_pids():
        fd_dir = f'/proc/{pid}/fd'
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            try:
                target = os.readlink(f'{fd_dir}/{name}')
            except OSError:
                continue
            if target.startswith(NVIDIA_DEVICE_PREFIXES):
                users[pid] = _proc_comm(pid) or f'PID {pid}'
                break
    return users


class Controller:
    def call(self, action):
        command = ['sudo', '-n', str(INSTALLED_HELPER), action, GPU, '--detail']
        log_detail(f'COMMAND START (privileged action {action}): {shlex.join(command)}')
        try:
            p=subprocess.run(command, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            log_detail(f'COMMAND TIMEOUT (privileged action {action}): {shlex.join(command)}')
            return False, 'The privileged operation timed out.'
        except OSError as e:
            log_detail(f'COMMAND ERROR (privileged action {action}): {e}')
            return False, str(e)
        normal_err = _log_command_result(command, p, f'privileged action {action}')
        out=(p.stdout or '').strip(); err=normal_err
        msg=out or err or ('Operation completed.' if p.returncode == 0 else f'Operation failed (exit {p.returncode}).')
        return p.returncode == 0, msg

    def call_cpu(self, action, *arguments):
        # Restricted helper: the UI never sends arbitrary paths or commands.
        command = ['sudo', '-n', str(CPU_HELPER), action, *map(str, arguments)]
        log_detail(f'COMMAND START (CPU action {action}): {shlex.join(command)}')
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_detail(f'CPU action failed to run: {exc}')
            return False, str(exc)
        err = _log_command_result(command, p, f'CPU action {action}')
        return p.returncode == 0, ((p.stdout or '').strip() or err or f'Exit code {p.returncode}')

    def basic_blocked(self):
        # The app owns this exact config path. Reading requires no root access.
        return path_exists(BASIC_BLOCK_FILE)

    def aggressive_blocked(self):
        # Recovery records count as guarded too, so an interrupted operation is
        # visible rather than silently appearing disabled.
        return (path_exists(AGGRESSIVE_BLOCK_FILE) or path_exists(NVIDIA_MODPROBE_STATE)
                or path_exists(DISCOVERY_STATE) or path_exists(DRIVER_OVERRIDE_STATE)
                or path_exists(PCI_MASK_STATE))

    def any_blocked(self):
        return self.basic_blocked() or self.aggressive_blocked()

    def state(self):
        # Current releases never remove the PCI device. If the legacy v2.2 marker remains
        # and the device is absent, this machine needs one reboot to re-enumerate it.
        if not path_exists(PCI):
            if path_exists(DETACHED_MARKER):
                return 'Reboot required (v2.2 PCI detach)'
            return 'GPU device not found'
        s=read_text(POWER_STATE, 'Unknown')
        if s:
            return s
        return 'Unknown'


class OperationLogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'{APP_NAME} — Activity Log')
        self.resize(900, 580)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.mode = 'simplified'
        layout = QVBoxLayout(self)

        view_buttons = QHBoxLayout()
        self.simplified_btn = QPushButton('Recent activity')
        self.detailed_btn = QPushButton('Technical details')
        for button in (self.simplified_btn, self.detailed_btn):
            button.setCheckable(True)
            button.setObjectName('logModeButton')
        self.simplified_btn.clicked.connect(lambda: self.set_mode('simplified'))
        self.detailed_btn.clicked.connect(lambda: self.set_mode('detailed'))
        view_buttons.addWidget(self.simplified_btn)
        view_buttons.addWidget(self.detailed_btn)
        view_buttons.addStretch()
        layout.addLayout(view_buttons)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFont('Monospace'))
        layout.addWidget(self.text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        clear = buttons.addButton('Clear history', QDialogButtonBox.ButtonRole.ActionRole)
        refresh = buttons.addButton('Refresh', QDialogButtonBox.ButtonRole.ActionRole)
        clear.clicked.connect(self.clear_logs)
        refresh.clicked.connect(self.load_log)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.set_mode('simplified')

    def set_mode(self, mode):
        self.mode = 'detailed' if mode == 'detailed' else 'simplified'
        self.simplified_btn.setChecked(self.mode == 'simplified')
        self.detailed_btn.setChecked(self.mode == 'detailed')
        self.load_log()

    def current_log_path(self):
        return DETAILED_LOG_FILE if self.mode == 'detailed' else LOG_FILE

    def load_log(self):
        path = self.current_log_path()
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            content = ''
        if not content:
            content = ('No detailed operations have been logged yet.' if self.mode == 'detailed'
                       else 'No operations have been logged yet.')
        self.text.setPlainText(content)
        cursor = self.text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.text.setTextCursor(cursor)

    def clear_logs(self):
        answer = QMessageBox.question(
            self, 'Clear activity history',
            'Clear both the activity history and technical details?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for path in (LOG_FILE, DETAILED_LOG_FILE):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, 'Could not clear log', f'Could not remove {path}:\n{exc}')
                return
        self.load_log()


class ProfileEditor(QDialog):
    """Create or edit a complete profile without changing live hardware."""
    def __init__(self, initial, parent=None, taken_names=()):
        super().__init__(parent)
        # Names of the *other* profiles; the store rejects duplicates only after
        # the dialog has closed, which would throw the user's edits away.
        self.taken_names = {str(name).strip().casefold() for name in taken_names}
        self.setWindowTitle('Edit power profile')
        self.setMinimumWidth(490)
        self.resize(560, 650)
        # Keep the action buttons reachable on short laptop screens.
        main_layout = QVBoxLayout(self)
        profile_scroll = QScrollArea(self)
        profile_scroll.setWidgetResizable(True)
        profile_scroll.setFrameShape(QFrame.Shape.NoFrame)
        profile_content = QWidget()
        form = QFormLayout(profile_content)
        profile_scroll.setWidget(profile_content)
        main_layout.addWidget(profile_scroll)
        self.name = QLineEdit(initial.get('name', ''))
        self.name.setMaxLength(120)
        form.addRow('Profile name', self.name)
        settings = initial['settings']
        self.gpu_state = QComboBox()
        self.gpu_state.addItem('Leave GPU power state unchanged', 'keep')
        self.gpu_state.addItem('Put NVIDIA to sleep (D3cold)', 'D3cold')
        self.gpu_state.addItem('Wake NVIDIA (D0)', 'D0')
        self.gpu_state.setCurrentIndex(max(0, self.gpu_state.findData(settings['gpu_state'])))
        form.addRow('NVIDIA power', self.gpu_state)
        self.basic = QCheckBox('Basic protection — stop NVIDIA driver auto-loading')
        self.basic.setChecked(settings['basic_guard'])
        self.basic.setToolTip('Switching a protection on also puts the NVIDIA GPU to sleep. With "Leave GPU power state unchanged" '
                              'it is only switched on when the GPU is already asleep; an awake GPU is never switched off by it.')
        form.addRow(self.basic)
        self.aggressive = QCheckBox('Stronger protection — block background GPU checks')
        self.aggressive.setChecked(settings['aggressive_guard'])
        self.aggressive.setToolTip(self.basic.toolTip())
        form.addRow(self.aggressive)
        self.sleep_apps = QCheckBox('Put NVIDIA back to sleep after NVIDIA apps close')
        self.sleep_apps.setChecked(settings['auto_gpu_sleep'])
        form.addRow(self.sleep_apps)

        self.use_cores = QCheckBox('Change active CPU core count')
        self.use_cores.setChecked(settings['disabled_cores'] is not None)
        self.cores = QSpinBox()
        self.cores.setRange(0, 4096)
        detected = core_control_status()
        if detected['available']:
            self.cores.setMaximum(detected['maximum'])
        self.cores.setValue(settings['disabled_cores'] or 0)
        self.use_cores.toggled.connect(self.cores.setEnabled)
        self.cores.setEnabled(self.use_cores.isChecked())
        form.addRow(self.use_cores, self.cores)

        self.use_freq = QCheckBox('Set maximum CPU speed')
        self.use_freq.setChecked(settings['frequency_mhz'] is not None)
        self.freq = QSpinBox()
        freq = frequency_status()
        self.freq.setRange(freq['min_mhz'], freq['max_mhz']) if freq['available'] else self.freq.setRange(100, 10000)
        self.freq.setSuffix(' MHz')
        self.freq.setValue(settings['frequency_mhz'] or (freq['current_mhz'] if freq['available'] and freq['current_mhz'] else 1900))
        self.use_freq.toggled.connect(self.freq.setEnabled)
        self.freq.setEnabled(self.use_freq.isChecked())
        form.addRow(self.use_freq, self.freq)

        self.use_limits = QCheckBox('Set CPU power limits')
        self.use_limits.setChecked(settings['pl1_w'] is not None)
        self.pl1, self.pl2 = QDoubleSpinBox(), QDoubleSpinBox()
        self.pl1.setRange(5., 55.)
        self.pl2.setRange(5., 70.)
        for spin, value in ((self.pl1, settings['pl1_w'] or 15.), (self.pl2, settings['pl2_w'] or 20.)):
            spin.setDecimals(1)
            spin.setSuffix(' W')
            spin.setValue(value)
            spin.setEnabled(self.use_limits.isChecked())
        self.use_limits.toggled.connect(self.pl1.setEnabled)
        self.use_limits.toggled.connect(self.pl2.setEnabled)
        limits_row = QHBoxLayout()
        limits_row.addWidget(QLabel('PL1 — long term'))
        limits_row.addWidget(self.pl1)
        limits_row.addWidget(QLabel('PL2 — boost'))
        limits_row.addWidget(self.pl2)
        form.addRow(self.use_limits)
        form.addRow(limits_row)

        self._initial_offsets = (settings['undervolt_cpu_mv'], settings['undervolt_cache_mv'], settings['undervolt_gpu_mv'])
        self.use_uv = QCheckBox('Also set CPU and Intel graphics voltage offsets (advanced)')
        self.use_uv.setChecked(settings['undervolt_cpu_mv'] is not None)
        form.addRow(self.use_uv)
        self.uv_controls = []
        for name, key in (('CPU cores', 'undervolt_cpu_mv'), ('CPU cache', 'undervolt_cache_mv'),
                          ('Intel graphics', 'undervolt_gpu_mv')):
            spin = QDoubleSpinBox()
            spin.setRange(MIN_OFFSET_MV, 0)
            spin.setDecimals(2)
            spin.setSuffix(' mV')
            spin.setSingleStep(5)
            spin.setValue(settings[key] if settings[key] is not None else 0)
            spin.setEnabled(self.use_uv.isChecked())
            self.use_uv.toggled.connect(spin.setEnabled)
            self.uv_controls.append(spin)
            form.addRow(name, spin)
        warning = QLabel('Advanced: Too much undervolting can freeze or crash the laptop. '
                         'Check this option only after testing the offsets manually. '
                         f'Changing offsets to below {WARN_BELOW_MV:g} mV asks for an extra confirmation. '
                         'Profiles never modify battery chart preferences.')
        warning.setWordWrap(True)
        warning.setObjectName('muted')
        form.addRow(warning)
        note = QLabel('An unchecked CPU option is left unchanged when the profile runs. '
                      'Automatic profiles temporarily disable the global Remember setting.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        main_layout.addWidget(buttons)

    def settings(self):
        return validate_settings({
            'gpu_state': self.gpu_state.currentData(),
            'basic_guard': self.basic.isChecked(),
            'aggressive_guard': self.aggressive.isChecked(),
            'auto_gpu_sleep': self.sleep_apps.isChecked(),
            'disabled_cores': self.cores.value() if self.use_cores.isChecked() else None,
            'frequency_mhz': self.freq.value() if self.use_freq.isChecked() else None,
            'pl1_w': self.pl1.value() if self.use_limits.isChecked() else None,
            'pl2_w': self.pl2.value() if self.use_limits.isChecked() else None,
            'undervolt_cpu_mv': self.uv_controls[0].value() if self.use_uv.isChecked() else None,
            'undervolt_cache_mv': self.uv_controls[1].value() if self.use_uv.isChecked() else None,
            'undervolt_gpu_mv': self.uv_controls[2].value() if self.use_uv.isChecked() else None,
        })

    def accept(self):
        try:
            if not self.name.text().strip():
                raise ValueError('Enter a name for this profile.')
            if self.name.text().strip().casefold() in self.taken_names:
                raise ValueError('Another profile already uses this name. Choose a different name.')
            self.settings()
        except ValueError as exc:
            QMessageBox.warning(self, 'Profile settings', str(exc))
            return
        # Profiles can run unattended, so newly chosen aggressive offsets need an explicit yes.
        # Offsets that were already in the profile (or read from the live hardware) are not re-asked.
        offsets = tuple(spin.value() for spin in self.uv_controls)
        if (self.use_uv.isChecked() and deep_undervolt_values(offsets)
                and offsets != tuple(self._initial_offsets)
                and not confirm_aggressive_undervolt(self, offsets, for_profile=True)):
            return
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.ctrl = Controller()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(830, 810)
        # Allow a short main window. The content itself keeps its natural height
        # inside a scroll area instead of compressing cards, graph or controls.
        self.setMinimumSize(720, 310)

        self.preferences = load_preferences()
        self.history_b = HistoryGraph('Battery discharge history', COLOR_AMBER, 'W')
        self.period_combo = QComboBox()
        self.period_combo.addItems(list(HistoryGraph.PERIODS.keys()))
        self.period_combo.setCurrentText(self.preferences['history_window'])
        self.period_combo.setToolTip('How much battery history the graph displays')
        self.history_b.set_period(self.period_combo.currentText())
        self.period_combo.currentTextChanged.connect(self.set_history_window)
        self.sample_combo = QComboBox()
        self.sample_combo.addItems(list(HistoryGraph.SAMPLE_INTERVALS.keys()))
        self.sample_combo.setCurrentText(self.preferences['history_interval'])
        self.sample_combo.setToolTip('How often the app measures battery discharge')
        self.sample_combo.currentTextChanged.connect(self.set_sample_interval)

        # The scroll area fills the main window. The inner widget sizes itself
        # to its cards, and the vertical scrollbar takes over on resize.
        scroll_area = QScrollArea(self)
        scroll_area.setObjectName('mainScrollArea')
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setCentralWidget(scroll_area)
        # On resize, use the actual visible width, not a stale child sizeHint.
        self.main_scroll_viewport = scroll_area.viewport()
        self.main_scroll_viewport.installEventFilter(self)

        root = QWidget()
        root.setObjectName('contentRoot')
        root.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout = QVBoxLayout(root)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN)
        layout.setSpacing(10)

        # Persistent top dashboard: GPU and CPU side-by-side, before battery/history.
        header_card = QFrame()
        header_card.setObjectName('headerCard')
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(8)
        title = QLabel(APP_NAME)
        title.setObjectName('title')
        subtitle = QLabel('GPU & CPU power control · live battery monitoring')
        subtitle.setObjectName('subtitle')
        # Activity Log belongs to the shared dashboard, not the GPU-only tab.
        # Both tabs can access it, and it remains modeless.
        header_top = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header_top.addLayout(heading, 1)
        self.log_btn = QPushButton('View activity log')
        self.log_btn.setToolTip('See recent changes and technical details. You can keep the log open while using the app.')
        self.log_btn.setAccessibleName('Open activity log')
        self.log_btn.clicked.connect(self.show_operation_log)
        header_top.addWidget(self.log_btn, 0, Qt.AlignmentFlag.AlignTop)
        header_layout.addLayout(header_top)

        dashboard = QHBoxLayout()
        dashboard.setSpacing(12)
        gpu_box = QVBoxLayout()
        gpu_box.setSpacing(2)
        gpu_caption = QLabel('NVIDIA GPU · CURRENT STATUS')
        gpu_caption.setObjectName('eyebrow')
        self.state_label = QLabel('Checking…')
        self.state_label.setToolTip('D3cold means the GPU is sleeping. D0 means it is awake. These are PCI power states reported by Linux.')
        self.gpu_usage_label = QLabel('')
        self.gpu_usage_label.setObjectName('muted')
        self.gpu_usage_label.setWordWrap(True)
        self.gpu_usage_label.setStyleSheet(f'color:{COLOR_MUTED};font-size:11px;')
        gpu_box.addWidget(gpu_caption)
        gpu_box.addWidget(self.state_label)
        gpu_box.addWidget(self.gpu_usage_label)
        gpu_box.addStretch()
        dashboard.addLayout(gpu_box, 1)

        cpu_box = QVBoxLayout()
        cpu_box.setSpacing(2)
        cpu_caption = QLabel('CPU · ACTIVE CORES AND POWER')
        cpu_caption.setObjectName('eyebrow')
        self.cpu_state_label = QLabel('Checking…')
        self.cpu_state_label.setStyleSheet(f'color:{COLOR_GREEN};font-size:18px;font-weight:750;')
        self.cpu_state_label.setWordWrap(True)
        self.cpu_limits_label = QLabel('PL1 —  ·  PL2 —')
        self.cpu_limits_label.setObjectName('muted')
        self.cpu_limits_label.setWordWrap(True)
        cpu_box.addWidget(cpu_caption)
        cpu_box.addWidget(self.cpu_state_label)
        cpu_box.addWidget(self.cpu_limits_label)
        self.cpu_voltages_label = QLabel('Voltage offsets: checking…')
        self.cpu_voltages_label.setObjectName('muted')
        self.cpu_voltages_label.setWordWrap(True)
        self.cpu_voltages_label.setToolTip('Current hardware voltage offsets for CPU cores, CPU cache and Intel graphics.')
        cpu_box.addWidget(self.cpu_voltages_label)
        cpu_box.addStretch()
        dashboard.addLayout(cpu_box, 1)
        header_layout.addLayout(dashboard)
        header_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(header_card)

        # Battery card.
        battery_card = QFrame()
        battery_card.setObjectName('card')
        battery_layout = QVBoxLayout(battery_card)
        battery_layout.setContentsMargins(14, 10, 14, 10)
        battery_layout.setSpacing(5)
        battery_caption = QLabel('BATTERY')
        battery_caption.setObjectName('eyebrow')
        self.battery_label = QLabel('Battery —')
        self.battery_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.battery_label.setStyleSheet('font-size:15px;font-weight:600;')
        self.battery_label.setWordWrap(True)
        battery_layout.addWidget(battery_caption)
        battery_layout.addWidget(self.battery_label)
        meta_row = QHBoxLayout()
        meta_row.setSpacing(14)
        self.power_source_label = QLabel('Power source: checking…')
        self.power_source_label.setObjectName('muted')
        self.power_source_label.setWordWrap(True)
        self.profile_status_label = QLabel('Power profile: none applied yet')
        self.profile_status_label.setObjectName('muted')
        self.profile_status_label.setWordWrap(True)
        meta_row.addWidget(self.power_source_label, 1)
        meta_row.addWidget(self.profile_status_label, 1)
        battery_layout.addLayout(meta_row)
        battery_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(battery_card)

        # Battery graph card.
        graph_card = QFrame()
        graph_card.setObjectName('card')
        graph_layout = QVBoxLayout(graph_card)
        graph_layout.setContentsMargins(14, 11, 14, 12)
        graph_layout.setSpacing(8)
        graph_top = QHBoxLayout()
        graph_title = QLabel('Battery history')
        graph_title.setObjectName('sectionTitle')
        graph_top.addWidget(graph_title)
        graph_top.addStretch()
        graph_top.addWidget(QLabel('Show'))
        graph_top.addWidget(self.period_combo)
        graph_top.addSpacing(8)
        graph_top.addWidget(QLabel('Update every'))
        graph_top.addWidget(self.sample_combo)
        graph_layout.addLayout(graph_top)
        graph_layout.addWidget(self.history_b)
        # Don't let the battery chart collapse when the user reduces height.
        # Its size is preserved and the scroll area handles overflow instead.
        graph_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(graph_card)

        # Bottom browser-like tabs: existing NVIDIA functions, plus Intel CPU controls.
        self.tabs = ContentSizedTabWidget()
        self.tabs.setDocumentMode(False)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        # Set the tab height from its *selected* page, never the tallest CPU page.
        self.tabs.setMinimumHeight(0)

        gpu_tab = QWidget()
        gpu_tab_layout = QVBoxLayout(gpu_tab)
        gpu_tab_layout.setContentsMargins(12, 12, 12, 12)
        gpu_tab_layout.setSpacing(10)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.power_btn = BusyButton('Turn NVIDIA on or off')
        self.power_btn.setToolTip('NVIDIA can only be turned off when its drivers are no longer in use.')
        self.power_btn.clicked.connect(self.toggle_gpu)
        controls.addWidget(self.power_btn, 1)
        gpu_tab_layout.addLayout(controls)

        protection_card = QFrame()
        protection_card.setObjectName('card')
        protection_layout = QVBoxLayout(protection_card)
        protection_layout.setContentsMargins(14, 11, 14, 12)
        protection_layout.setSpacing(5)
        protection_title = QLabel('Keep NVIDIA asleep')
        protection_title.setObjectName('sectionTitle')
        protection_layout.addWidget(protection_title)

        self.basic_block_box = QCheckBox('Basic protection — block automatic driver loading')
        self.basic_block_box.stateChanged.connect(self.toggle_basic_block)
        self.basic_block_box.setToolTip('Basic: prevents automatic NVIDIA module loading through modprobe rules.')
        protection_layout.addWidget(self.basic_block_box)
        basic_desc = QLabel('Stops most automatic driver loading. Other applications may still briefly wake the GPU.')
        basic_desc.setObjectName('muted')
        basic_desc.setContentsMargins(26, 0, 0, 3)
        basic_desc.setWordWrap(True)
        protection_layout.addWidget(basic_desc)

        self.aggressive_block_box = QCheckBox('Stronger protection — block background GPU checks')
        self.aggressive_block_box.stateChanged.connect(self.toggle_aggressive_block)
        self.aggressive_block_box.setToolTip('Masks NVIDIA PCI configuration, protects driver binding and NVIDIA module loading.')
        protection_layout.addWidget(self.aggressive_block_box)
        aggressive_desc = QLabel('Helps stop Steam and other apps from waking NVIDIA. Also changes system-wide PCI access; turn it off before using NVIDIA games or updating drivers.')
        aggressive_desc.setObjectName('muted')
        aggressive_desc.setContentsMargins(26, 0, 0, 0)
        aggressive_desc.setWordWrap(True)
        protection_layout.addWidget(aggressive_desc)
        gpu_tab_layout.addWidget(protection_card)
        protection_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        gpu_tab_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.tabs.addTab(gpu_tab, 'GPU')

        cpu_tab = QWidget()
        cpu_layout = QVBoxLayout(cpu_tab)
        cpu_layout.setContentsMargins(12, 12, 12, 12)
        cpu_layout.setSpacing(9)
        cpu_sections = ResponsiveSectionArea(min_card_width=620)
        self.cpu_sections = cpu_sections

        cores_card = QFrame()
        cores_card.setObjectName('card')
        cores_layout = QVBoxLayout(cores_card)
        cores_layout.setContentsMargins(14, 10, 14, 10)
        cores_title = QLabel('CPU cores')
        cores_title.setObjectName('sectionTitle')
        cores_layout.addWidget(cores_title)
        core_row = QHBoxLayout()
        core_row.addWidget(QLabel('Physical cores to turn off'))
        self.core_count_spin = QSpinBox()
        self.core_count_spin.setRange(0, 0)  # Recomputed from detected physical cores.
        self.core_count_spin.setToolTip('0 restores app-managed cores. The CPU0 physical core always remains online.')
        self.core_count_spin.valueChanged.connect(self._mark_core_count_dirty)
        core_row.addWidget(self.core_count_spin)
        self.apply_cores_button = QPushButton('Apply core setting')
        self.apply_cores_button.clicked.connect(self.apply_core_count)
        self.restore_cores_button = QPushButton('Turn cores back on')
        self.restore_cores_button.clicked.connect(self.restore_managed_cores)
        cores_layout.addLayout(core_row)
        core_buttons = QHBoxLayout()
        core_buttons.addWidget(self.apply_cores_button)
        core_buttons.addWidget(self.restore_cores_button)
        cores_layout.addLayout(core_buttons)
        self.cores_desc = QLabel('Choose how many whole CPU cores to turn off. Setting this to 0 turns on any cores disabled by this app. At least one physical core always stays on. Performance may decrease.')
        self.cores_desc.setObjectName('muted')
        self.cores_desc.setWordWrap(True)
        cores_layout.addWidget(self.cores_desc)
        cpu_sections.addSection(cores_card)

        # Limit maximum CPU frequency independently from physical-core hotplug
        # and from package PL1/PL2. Sysfs uses kHz; UI uses whole MHz.
        frequency_card = QFrame()
        frequency_card.setObjectName('card')
        frequency_layout = QVBoxLayout(frequency_card)
        frequency_layout.setContentsMargins(14, 10, 14, 10)
        frequency_layout.setSpacing(6)
        frequency_title = QLabel('CPU speed limit')
        frequency_title.setObjectName('sectionTitle')
        frequency_layout.addWidget(frequency_title)
        frequency_row = QHBoxLayout()
        frequency_row.addWidget(QLabel('Highest allowed speed'))
        self.frequency_spin = QSpinBox()
        self.frequency_spin.setRange(100, 10000)
        self.frequency_spin.setSingleStep(100)
        self.frequency_spin.setSuffix(' MHz')
        self.frequency_spin.valueChanged.connect(self._mark_cpu_frequency_dirty)
        frequency_row.addWidget(self.frequency_spin)
        self.apply_frequency_button = QPushButton('Apply speed limit')
        self.apply_frequency_button.clicked.connect(self.apply_cpu_frequency)
        self.restore_frequency_button = QPushButton('Restore maximum speed')
        self.restore_frequency_button.clicked.connect(self.restore_cpu_frequency)
        frequency_layout.addLayout(frequency_row)
        frequency_buttons = QHBoxLayout()
        frequency_buttons.addWidget(self.apply_frequency_button)
        frequency_buttons.addWidget(self.restore_frequency_button)
        frequency_layout.addLayout(frequency_buttons)
        self.frequency_desc = QLabel('Choose how fast the enabled CPU cores are allowed to run. For example, 1900 MHz is 1.9 GHz. Lower speeds may use less power but can slow the laptop.')
        self.frequency_desc.setObjectName('muted')
        self.frequency_desc.setWordWrap(True)
        frequency_layout.addWidget(self.frequency_desc)
        cpu_sections.addSection(frequency_card)

        limits_card = QFrame()
        limits_card.setObjectName('card')
        limits_layout = QVBoxLayout(limits_card)
        limits_layout.setContentsMargins(14, 10, 14, 10)
        limits_layout.setSpacing(7)
        limits_title = QLabel('CPU power limits')
        limits_title.setObjectName('sectionTitle')
        limits_layout.addWidget(limits_title)
        limits_expl = QLabel('<b>Long-term limit (PL1):</b> the average power the CPU can use over time. '
                             '<b>Short boost limit (PL2):</b> extra power allowed briefly for a speed boost. '
                             'These are ceilings, not targets. Lower settings may reduce heat and performance.')
        limits_expl.setWordWrap(True)
        limits_layout.addWidget(limits_expl)
        limits_grid = QGridLayout()
        limits_grid.addWidget(QLabel('Long-term (PL1)'), 0, 0)
        self.pl1_spin = QDoubleSpinBox()
        self.pl1_spin.setRange(5.0, 55.0)
        self.pl1_spin.setDecimals(1)
        self.pl1_spin.setSingleStep(1.0)
        self.pl1_spin.setSuffix(' W')
        self.pl1_spin.valueChanged.connect(self._mark_cpu_power_dirty)
        limits_grid.addWidget(self.pl1_spin, 0, 1)
        limits_grid.addWidget(QLabel('Short boost (PL2)'), 0, 2)
        self.pl2_spin = QDoubleSpinBox()
        self.pl2_spin.setRange(5.0, 70.0)
        self.pl2_spin.setDecimals(1)
        self.pl2_spin.setSingleStep(1.0)
        self.pl2_spin.setSuffix(' W')
        self.pl2_spin.valueChanged.connect(self._mark_cpu_power_dirty)
        limits_grid.addWidget(self.pl2_spin, 0, 3)
        limits_layout.addLayout(limits_grid)
        limits_buttons = QHBoxLayout()
        self.apply_limits_button = QPushButton('Apply power limits')
        self.apply_limits_button.clicked.connect(self.apply_cpu_limits)
        self.restore_limits_button = QPushButton('Restore startup power limits')
        self.restore_limits_button.clicked.connect(self.restore_cpu_limits)
        limits_buttons.addWidget(self.apply_limits_button)
        limits_buttons.addWidget(self.restore_limits_button)
        limits_layout.addLayout(limits_buttons)
        self.cpu_info_label = QLabel('Original startup power limits are recorded when the laptop boots. Reboot once after installing if they are not available.')
        self.cpu_info_label.setObjectName('muted')
        self.cpu_info_label.setWordWrap(True)
        limits_layout.addWidget(self.cpu_info_label)
        cpu_sections.addSection(limits_card)

        voltage_card = QFrame()
        voltage_card.setObjectName('card')
        uv_layout = QVBoxLayout(voltage_card)
        uv_layout.setContentsMargins(14, 10, 14, 10)
        uv_title = QLabel('Undervolting · advanced')
        uv_title.setObjectName('sectionTitle')
        uv_layout.addWidget(uv_title)
        uv_warning = QLabel('Lowering voltage may save power and heat, but unstable settings can crash or freeze the laptop. '
                            f'Start with small changes; values below {WARN_BELOW_MV:g} mV ask for an extra confirmation. '
                            'Requires intel-undervolt and an unlocked processor.')
        uv_warning.setWordWrap(True)
        uv_layout.addWidget(uv_warning)
        uv_row = QGridLayout()
        self.undervolt_spins = []
        for column, name in enumerate(('CPU cores', 'CPU cache', 'Intel graphics')):
            uv_row.addWidget(QLabel(name), 0, column)
            spin = QDoubleSpinBox()
            spin.setRange(MIN_OFFSET_MV, 0)
            spin.setDecimals(2)
            spin.setSingleStep(5)
            spin.setSuffix(' mV')
            spin.setToolTip(f'0 mV means no voltage offset. Negative values reduce voltage; start with -10 mV. '
                            f'Values below {WARN_BELOW_MV:g} mV are rarely stable and need an extra confirmation. '
                            f'The lowest allowed value is {MIN_OFFSET_MV:g} mV.')
            uv_row.addWidget(spin, 1, column)
            self.undervolt_spins.append(spin)
            spin.valueChanged.connect(self._mark_undervolt_dirty)
        uv_layout.addLayout(uv_row)
        uv_buttons = QHBoxLayout()
        self.uv_apply_button = QPushButton('Apply voltage offsets')
        self.uv_apply_button.clicked.connect(self.apply_undervolt)
        self.uv_reset_button = QPushButton('Reset offsets to 0 mV')
        self.uv_reset_button.clicked.connect(self.reset_undervolt)
        uv_buttons.addWidget(self.uv_apply_button)
        uv_buttons.addWidget(self.uv_reset_button)
        uv_layout.addLayout(uv_buttons)
        self.uv_status_label = QLabel('Current offsets: checking…')
        self.uv_status_label.setObjectName('muted')
        self.uv_status_label.setWordWrap(True)
        uv_layout.addWidget(self.uv_status_label)
        cpu_sections.addSection(voltage_card)
        cpu_layout.addWidget(cpu_sections)
        self.tabs.addTab(cpu_tab, 'CPU')
        # Profiles are always visible as a third tab; they never replace GPU/CPU tools.
        profiles_tab = QWidget()
        profiles_layout = QVBoxLayout(profiles_tab)
        profiles_layout.setContentsMargins(12, 12, 12, 12)
        profiles_layout.setSpacing(9)
        profiles_sections = ResponsiveSectionArea(min_card_width=635)
        self.profiles_sections = profiles_sections

        profile_list_card = QFrame()
        profile_list_card.setObjectName('card')
        profile_list_layout = QVBoxLayout(profile_list_card)
        profile_list_layout.setContentsMargins(14, 10, 14, 10)
        profile_list_layout.setSpacing(9)
        profiles_intro = QLabel('Save any number of named setups. Apply one yourself, or let the app switch when you connect or disconnect power.')
        profiles_intro.setObjectName('muted')
        profiles_intro.setWordWrap(True)
        profile_list_layout.addWidget(profiles_intro)
        self.profile_list = QListWidget()
        self.profile_list.setFixedHeight(175)
        profile_list_layout.addWidget(self.profile_list)
        buttons = QHBoxLayout()
        self.profile_new_button = QPushButton('Save current as new…')
        self.profile_edit_button = QPushButton('Edit…')
        self.profile_delete_button = QPushButton('Delete')
        self.profile_apply_button = QPushButton('Apply selected profile')
        for widget in (self.profile_new_button, self.profile_edit_button,
                       self.profile_delete_button, self.profile_apply_button):
            buttons.addWidget(widget)
        profile_list_layout.addLayout(buttons)
        self.profile_new_button.clicked.connect(self.create_profile)
        self.profile_edit_button.clicked.connect(self.edit_profile)
        self.profile_delete_button.clicked.connect(self.delete_profile)
        self.profile_apply_button.clicked.connect(self.apply_selected_profile)
        self.profile_list.currentRowChanged.connect(self.update_profile_controls)
        profiles_sections.addSection(profile_list_card)

        auto_card = QFrame()
        auto_card.setObjectName('card')
        auto_layout = QVBoxLayout(auto_card)
        auto_layout.setContentsMargins(14, 10, 14, 10)
        auto_heading = QLabel('Automatic switching')
        auto_heading.setObjectName('sectionTitle')
        auto_layout.addWidget(auto_heading)
        self.auto_profiles_box = QCheckBox('Switch profiles automatically with power source')
        auto_layout.addWidget(self.auto_profiles_box)
        source_grid = QGridLayout()
        source_grid.addWidget(QLabel('When plugged in'), 0, 0)
        self.ac_profile_combo = QComboBox()
        source_grid.addWidget(self.ac_profile_combo, 0, 1)
        source_grid.addWidget(QLabel('When running on battery'), 1, 0)
        self.battery_profile_combo = QComboBox()
        source_grid.addWidget(self.battery_profile_combo, 1, 1)
        auto_layout.addLayout(source_grid)
        auto_note = QLabel('Profiles switch after you connect or disconnect the charger, while the app runs in your desktop session (including the tray). At login, automatic mode overrides the previous remembered CPU/GPU state and applies only the profile for the detected power source. Unassigned modes leave settings unchanged. Errors appear in the Activity Log.')
        auto_note.setWordWrap(True)
        auto_note.setObjectName('muted')
        auto_layout.addWidget(auto_note)
        profiles_sections.addSection(auto_card)
        profiles_layout.addWidget(profiles_sections)
        profiles_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.tabs.addTab(profiles_tab, 'Profiles')
        self.auto_profiles_box.toggled.connect(self.set_auto_profiles)
        self.ac_profile_combo.currentIndexChanged.connect(self.set_power_assignments)
        self.battery_profile_combo.currentIndexChanged.connect(self.set_power_assignments)
        layout.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignTop)
        self._layout_responsive_sections(self.main_scroll_viewport.width())
        self.power_controls_dirty = False
        self.frequency_controls_dirty = False
        self.core_count_dirty = False
        self.undervolt_dirty = False
        self.last_voltage_problem = None
        self.voltage_read_process = None
        self.voltage_read_timeout = None
        self.last_cpu_info = None
        try:
            self.profile_store = read_store(PROFILES_FILE)
            self.profile_store_error = None
        except (OSError, ValueError) as exc:
            self.profile_store = None
            self.profile_store_error = str(exc)
            log_operation(f'Power profiles could not be loaded: {exc}. Automatic profile switching and old startup restoration are suspended until the profiles file is repaired.')
        # Upgrade/migration: auto profiles take ownership of startup state,
        # but preserve the user's prior Remember choice for later restoration.
        if self.profile_store and self.profile_store['auto_enabled']:
            if self.preferences['restore_before_auto'] is None:
                self.preferences['restore_before_auto'] = self.preferences['restore_state']
            self.preferences['restore_state'] = False
            try:
                save_preferences(self.preferences)
            except OSError as exc:
                log_operation(f'Could not save remember-setting migration: {exc}')
        self.last_applied_profile = None
        self.last_auto_target = None
        self.auto_retry_used = False
        self.power_candidate = None
        self.power_candidate_count = 0
        self.stable_power_source = None
        self.profile_process = None
        self.profile_running_name = None
        # A manual GPU switch runs in the background so the window keeps animating.
        self.gpu_busy = False
        self.gpu_process = None
        self.gpu_action = None
        self.gpu_timed_out = False
        self.gpu_wait_target = None
        self.gpu_wait_deadline = 0.0
        self.gpu_timeout = QTimer(self)
        self.gpu_timeout.setSingleShot(True)
        self.gpu_timeout.timeout.connect(self._gpu_action_timed_out)
        self.gpu_settle_timer = QTimer(self)
        self.gpu_settle_timer.setInterval(200)
        self.gpu_settle_timer.timeout.connect(self._gpu_settle_tick)
        self.refresh_profile_list()
        self.tabs.setCurrentIndex(self.preferences.get('selected_tab', 0))
        # Connect only now: addTab() above emits currentChanged for the first page,
        # which would overwrite the remembered tab before it could be restored.
        self.tabs.currentChanged.connect(self._store_selected_tab)
        self._sync_tab_size()

        note = QLabel('GPU power state is reported by Linux. Battery readings are estimates and depend on laptop support.')
        note.setObjectName('muted')
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setStyleSheet(f'color:{COLOR_MUTED};font-size:10px;')
        layout.addWidget(note)
        # Any spare scroll area height remains *outside* the cards, never
        # between controls within the current tab.
        layout.addStretch()
        # Qt requires the content layout to exist before setWidget(). Add the
        # finished tree so the scroll area can calculate its true size hint.
        scroll_area.setWidget(root)

        # Automatic app-close restoration state. It only arms after the app
        # observes a D3cold -> D0 transition, so manually turning NVIDIA on is
        # not immediately undone.
        self.auto_restore_armed = False
        self.auto_restore_waiting = False
        self.auto_restore_seen_users = set()
        self.auto_restore_last_users = {}
        self.auto_restore_anchors = {}
        self.auto_restore_started_at = None
        self.auto_restore_empty_since = None
        self.auto_restore_manual_until = 0.0
        # Application sessions currently/recently observed using NVIDIA.
        self.display_app_anchors = {}
        self.last_gpu_state = self.ctrl.state()
        self._tray_state = None
        self._styled_state = None
        self.last_notified_gpu_state = self.last_gpu_state
        self.gpu_notification_token = 0
        self.operation_log_dialog = None
        self.last_guarded_probe_state = self.last_gpu_state
        log_operation(f'Application v{APP_VERSION} started; GPU power state: {self.last_gpu_state}')
        log_detail(f'CONFIG: application version: {APP_VERSION}')
        log_detail(f'CONFIG: NVIDIA PCI address: {GPU}')
        log_detail(f'CONFIG: installed privileged helper: {INSTALLED_HELPER}')
        log_detail('BACKGROUND: UI state refresh every 1 second; NVIDIA application tracking every 250 ms; battery sampling uses the selected interval.')
        options_menu = self.menuBar().addMenu('Options')
        self.autostart_action = QAction('Start at login, minimized to tray', self)
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(self.preferences['autostart'])
        self.autostart_action.toggled.connect(self.set_autostart)
        options_menu.addAction(self.autostart_action)
        self.restore_action = QAction('Remember and restore GPU + CPU settings when the app starts', self)
        self.restore_action.setCheckable(True)
        self.restore_action.setChecked(self.preferences['restore_state'])
        self.restore_action.toggled.connect(self.set_restore_state)
        options_menu.addAction(self.restore_action)
        self.restore_action.setToolTip('Unavailable during automatic AC/battery profile switching. Your previous choice returns when automatic switching is disabled.')
        self.sync_remember_controls()
        self.auto_restore_action = QAction('Put NVIDIA back to sleep after NVIDIA apps close', self)
        self.auto_restore_action.setCheckable(True)
        self.auto_restore_action.setChecked(self.preferences['auto_restore_apps'])
        self.auto_restore_action.toggled.connect(self.set_auto_restore_apps)
        options_menu.addAction(self.auto_restore_action)
        self.auto_restore_action.setToolTip('When NVIDIA use wakes the GPU, track the owning application session and restore D3cold only after that application and its helpers have closed.')
        # Notification preferences apply immediately and survive application
        # restarts. Existing installations keep their GPU state alerts by default.
        notifications_menu = self.menuBar().addMenu('Notifications')
        choices = (
            ('gpu_state', 'NVIDIA GPU wakes up or goes to sleep'),
            ('charger', 'Charger connected or disconnected'),
            ('auto_profile', 'Automatic power profile applied'),
            ('startup_restore', 'Saved CPU/GPU settings restored at startup'),
        )
        self.notification_actions = {}
        for kind, label in choices:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self.preferences['notifications'][kind])
            action.toggled.connect(lambda enabled, key=kind: self.set_notification_preference(key, enabled))
            notifications_menu.addAction(action)
            self.notification_actions[kind] = action
        self.tray=QSystemTrayIcon(QIcon(str(APP_ICON)),self); self.tray.setToolTip(APP_NAME)
        self.menu=QMenu(); self.tray_state=QAction('GPU: checking…',self); self.tray_state.setEnabled(False); self.menu.addAction(self.tray_state)
        self.tray_toggle=QAction('Toggle NVIDIA ON / OFF',self); self.tray_toggle.triggered.connect(self.toggle_gpu); self.menu.addAction(self.tray_toggle)
        self.tray_basic_block=QAction('Basic NVIDIA protection',self); self.tray_basic_block.setCheckable(True); self.tray_basic_block.triggered.connect(self.toggle_basic_block_from_tray); self.menu.addAction(self.tray_basic_block)
        self.tray_aggressive_block=QAction('Stronger NVIDIA protection (system-wide)',self); self.tray_aggressive_block.setCheckable(True); self.tray_aggressive_block.triggered.connect(self.toggle_aggressive_block_from_tray); self.menu.addAction(self.tray_aggressive_block)
        self.menu.addSeparator()
        open_profiles = QAction('Power profiles…', self)
        open_profiles.triggered.connect(self.present_profiles)
        self.menu.addAction(open_profiles)
        activity_action = QAction('View activity log', self)
        activity_action.triggered.connect(self.show_operation_log)
        self.menu.addAction(activity_action)
        self.menu.addSeparator(); open_action=QAction('Open ThinkPad Nvidia Switch',self); open_action.triggered.connect(self.present_window); self.menu.addAction(open_action)
        quit_action=QAction('Quit',self); quit_action.triggered.connect(QApplication.quit); self.menu.addAction(quit_action)
        self.tray.setContextMenu(self.menu); self.tray.activated.connect(self.tray_activated); self.tray.show()
        # Refresh status/UI frequently, but sample the battery only at the selected interval.
        self.timer=QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(1000)
        # Process tracking runs faster than the UI refresh so short-lived
        # Steam/Proton helper processes are less likely to be missed.
        self.auto_timer=QTimer(self); self.auto_timer.timeout.connect(self.auto_restore_tick); self.auto_timer.start(250)
        self.sample_timer=QTimer(self); self.sample_timer.timeout.connect(self.sample_battery)
        self.sample_timer.start(HistoryGraph.SAMPLE_INTERVALS[self.preferences['history_interval']])
        self.refresh(); self.sample_battery()
        # Query offsets automatically without blocking the UI or requiring a password.
        # The hardware may be changed by external utilities or power profiles.
        self.undervolt_refresh_timer = QTimer(self)
        self.undervolt_refresh_timer.timeout.connect(self._poll_undervolt)
        self.undervolt_refresh_timer.start(15000)
        QTimer.singleShot(2000, self.refresh_undervolt)
        self.power_source_timer = QTimer(self)
        self.power_source_timer.timeout.connect(self.check_power_profile)
        self.power_source_timer.start(2000)
        # Wait for session startup before applying boot power profile.
        self.profile_auto_ready = False
        QTimer.singleShot(15000, self.enable_profile_autoswitch)
        # Automatic AC/battery profiles take exclusive startup priority. Do
        # not even schedule the old last-used snapshot in automatic mode.
        if use_legacy_startup_restore(self.profile_store, self.preferences['restore_state']):
            QTimer.singleShot(15000, self.restore_saved_state)  # let KDE finish session startup first
        if '--minimized' in sys.argv:
            self.hide()
    def set_notification_preference(self, kind, enabled):
        if kind not in self.preferences['notifications']:
            return
        old = self.preferences['notifications'][kind]
        self.preferences['notifications'][kind] = bool(enabled)
        try:
            save_preferences(self.preferences)
        except OSError as exc:
            self.preferences['notifications'][kind] = old
            self.notification_actions[kind].blockSignals(True)
            self.notification_actions[kind].setChecked(old)
            self.notification_actions[kind].blockSignals(False)
            log_operation(f'Cannot save notification preference {kind}: {exc}')

    def notify(self, kind, text):
        if text and self.preferences['notifications'].get(kind, False):
            self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.MessageIcon.Information, 4500)

    def _gpu_wake_owners(self):
        # An NVIDIA file descriptor is evidence of current use, not proof of
        # what originally triggered PCI D0. Include its DRM render node for
        # Vulkan/Wayland apps that never open /dev/nvidia0 directly.
        users = nvidia_users()
        link = Path('/dev/dri/by-path') / f'pci-{GPU}-render'
        try:
            render_node = str(link.resolve(strict=True))
            if not render_node.startswith('/dev/dri/renderD'):
                render_node = None
        except OSError:
            render_node = None
        if render_node is not None:
            for pid in _proc_pids():
                if pid in users:
                    continue
                fd_dir = f'/proc/{pid}/fd'
                try:
                    names = os.listdir(fd_dir)
                except OSError:
                    continue
                for name in names:
                    try:
                        if os.readlink(f'{fd_dir}/{name}') == render_node:
                            users[pid] = _proc_comm(pid) or f'PID {pid}'
                            break
                    except OSError:
                        continue
        result = []
        for pid, name in users.items():
            anchor = _application_anchor(pid)
            label = anchor.get('label') or _friendly_process_name(name)
            if label:
                result.append(label)
        return result

    def _finish_gpu_wake_notification(self, token):
        if token != self.gpu_notification_token:
            return
        self.gpu_notification_token += 1
        if not self.preferences['notifications']['gpu_state']:
            return
        owners = self._gpu_wake_owners()
        self.notify('gpu_state', gpu_transition_message('D3cold', 'D0', owners))

    def set_autostart(self, enabled):
        self.preferences['autostart'] = bool(enabled)
        log_operation('Start at login, minimized to tray: ' + ('enabled.' if enabled else 'disabled.'))
        try:
            if enabled:
                AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
                log_detail(f'FILE WRITE: {AUTOSTART_FILE} (desktop autostart entry)')
                AUTOSTART_FILE.write_text(desktop_entry())
            else:
                log_detail(f'FILE REMOVE: {AUTOSTART_FILE} (disable desktop autostart)')
                AUTOSTART_FILE.unlink(missing_ok=True)
            save_preferences(self.preferences)
        except OSError as e:
            self.autostart_action.blockSignals(True)
            self.autostart_action.setChecked(not enabled)
            self.autostart_action.blockSignals(False)
            log_operation(f'Could not update autostart: {e}')

    def sync_remember_controls(self):
        auto = bool(self.profile_store and self.profile_store['auto_enabled'])
        self.restore_action.blockSignals(True)
        self.restore_action.setEnabled(not auto)
        self.restore_action.setChecked(bool(self.preferences['restore_state']) and not auto)
        self.restore_action.blockSignals(False)

    def set_restore_state(self, enabled):
        if enabled and self.profile_store and self.profile_store['auto_enabled']:
            self.sync_remember_controls()
            return
        if enabled:
            if not INSTALLED_HELPER.exists() or not RESTORE_WRAPPER.exists() or not CPU_HELPER.exists():
                log_operation('Cannot enable saved-state restoration: the installed helper/wrapper is missing. Re-run install.sh.')
                self.restore_action.blockSignals(True); self.restore_action.setChecked(False); self.restore_action.blockSignals(False)
                return
            ok, msg = self.ctrl.call('save-state')
            if not ok:
                self.restore_action.blockSignals(True); self.restore_action.setChecked(False); self.restore_action.blockSignals(False)
                log_operation(f'Could not save GPU/CPU settings: {msg}')
                return
            self.preferences['restore_state'] = True
            save_preferences(self.preferences)
            log_operation('Saved GPU state, guards, disabled-core count, CPU frequency cap, and PL1/PL2 limits.')
        else:
            self.preferences['restore_state'] = False
            save_preferences(self.preferences)
            log_operation('Remember and restore GPU + CPU settings: disabled.')

    def set_auto_restore_apps(self, enabled):
        self.preferences['auto_restore_apps'] = bool(enabled)
        save_preferences(self.preferences)
        self.auto_restore_armed = False
        self.auto_restore_waiting = False
        self.auto_restore_seen_users.clear()
        self.auto_restore_anchors.clear()
        self.auto_restore_empty_since = None
        self.auto_restore_started_at = None
        log_operation('Automatic D3cold restoration after NVIDIA applications was ' + ('enabled.' if enabled else 'disabled.'))

    def current_nvidia_usage(self):
        """Return [(application, actively_using)] for tracked NVIDIA applications."""
        users = nvidia_users()
        active_keys = set()

        # Learn about new NVIDIA users even when automatic D3cold restoration
        # is disabled. The first process seen for an application is retained
        # as the origin process for diagnostics, while current device users
        # determine whether the application is actively using the GPU now.
        for pid, name in users.items():
            anchor = _anchor_with_origin(_application_anchor(pid), pid, name)
            key = _anchor_key(anchor)
            active_keys.add(key)
            label = anchor.get('label') or _friendly_process_name(name) or f'PID {pid}'
            self.display_app_anchors[key] = anchor | {'label': label}

        # The automatic-restoration tracker may already know about an app even
        # if the particular NVIDIA helper currently has no device open.
        for key, anchor in self.auto_restore_anchors.items():
            self.display_app_anchors[key] = anchor

        alive = {}
        for key, anchor in self.display_app_anchors.items():
            if _anchor_alive(anchor):
                alive[key] = anchor
        self.display_app_anchors = alive

        return sorted(
            [(a.get('label'), _anchor_key(a) in active_keys) for a in self.display_app_anchors.values() if a.get('label')],
            key=lambda item: item[0].casefold()
        )

    def _reset_auto_restore_tracking(self):
        self.auto_restore_armed = False
        self.auto_restore_waiting = False
        self.auto_restore_seen_users.clear()
        self.auto_restore_anchors.clear()
        self.auto_restore_last_users = {}
        self.auto_restore_empty_since = None
        self.auto_restore_started_at = None

    def auto_restore_tick(self):
        state = self.ctrl.state()
        # Passive 250 ms wake-event diagnostics: the 1 s UI refresh can miss
        # momentary Steam/PCI wakeups. Never run lspci/nvidia-smi here, as
        # those would themselves be possible sources of a GPU wakeup.
        if self.ctrl.any_blocked():
            previous = self.last_guarded_probe_state
            if previous == 'D3cold' and state == 'D0':
                loaded = []
                try:
                    names = {line.split()[0] for line in Path('/proc/modules').read_text().splitlines() if line.split()}
                    loaded = [name for name in MODULES if name in names]
                except OSError:
                    pass
                try:
                    driver = (PCI / 'driver').resolve(strict=True).name if (PCI / 'driver').is_symlink() else 'unbound'
                except OSError:
                    driver = 'unknown'
                runtime_status = read_text(PCI / 'power' / 'runtime_status', 'unknown')
                control = read_text(POWER_CONTROL, 'unknown')
                override = read_text(PCI / 'driver_override', 'unavailable').strip()
                if override in ('(null)', ''):
                    override = '(unset)'
                guard = guard_label(self.ctrl.basic_blocked(), self.ctrl.aggressive_blocked())
                log_operation(f'Guarded GPU wake detected (D3cold → D0; {guard}). See Detailed Log for diagnostics.')
                log_detail(f'GPU WAKE SNAPSHOT: guard={guard}; modules={loaded or "none"}; '
                           f'bound_driver={driver}; runtime_status={runtime_status}; power_control={control}; '
                           f'driver_override={override}; note=PCI wake cause not yet attributed')
            self.last_guarded_probe_state = state
        else:
            self.last_guarded_probe_state = state

        # Either explicit guard is authoritative. Both are intended to prevent
        # automatic NVIDIA activation, so the application-close tracker must not
        # reactively cycle D0 -> D3cold while either guard is enabled.
        if self.ctrl.any_blocked():
            if self.auto_restore_armed or self.auto_restore_anchors:
                self._reset_auto_restore_tracking()
            self.last_gpu_state = state
            return

        if not self.preferences.get('auto_restore_apps', False):
            self.last_gpu_state = state
            return

        now = time.monotonic()

        if now < self.auto_restore_manual_until:
            self.last_gpu_state = state
            return

        if state == 'D3cold' and self.last_gpu_state != 'D3cold':
            self._reset_auto_restore_tracking()

        if self.last_gpu_state == 'D3cold' and state == 'D0':
            self.auto_restore_armed = True
            self.auto_restore_waiting = True
            self.auto_restore_seen_users.clear()
            self.auto_restore_anchors.clear()
            self.auto_restore_empty_since = None
            self.auto_restore_started_at = now
            self.auto_restore_last_users = {}
            log_operation('GPU changed D3cold → D0; starting NVIDIA application tracking.')

        if self.auto_restore_armed and state == 'D0':
            users = nvidia_users()
            self.auto_restore_last_users = users

            # The important change from v1.4: once we see an NVIDIA user, we
            # remember its application session rather than treating that one
            # PID as the lifetime of the application. Steam, Spectacle and
            # similar programs routinely create short-lived helper processes.
            for pid, name in users.items():
                self.auto_restore_seen_users.add(pid)
                anchor = _anchor_with_origin(_application_anchor(pid), pid, name)
                key = ((anchor.get('scope'),) if anchor.get('scope') else
                       (None, anchor.get('root_pid'), anchor.get('root_starttime')))
                if key not in self.auto_restore_anchors:
                    self.auto_restore_anchors[key] = anchor
                    log_operation(f'Tracking NVIDIA application session: {anchor.get("label", name)}')

            if users:
                self.auto_restore_waiting = False
                self.auto_restore_empty_since = None

            # Keep the GPU on while any application session that was observed
            # using NVIDIA is still alive, even if its original helper PID has
            # already exited.
            alive_anchors = [a for a in self.auto_restore_anchors.values() if _anchor_alive(a)]
            if alive_anchors:
                self.auto_restore_empty_since = None
            elif self.auto_restore_anchors and not users:
                if self.auto_restore_empty_since is None:
                    self.auto_restore_empty_since = now
                elif now - self.auto_restore_empty_since >= 2.0:
                    self._reset_auto_restore_tracking()
                    self._auto_restore_gpu_off()
            elif not self.auto_restore_anchors and self.auto_restore_started_at is not None:
                # Give the application's startup sequence time to expose a
                # device. If nothing ever uses NVIDIA, do not power it off
                # merely because some unrelated kernel activity loaded modules.
                if now - self.auto_restore_started_at > 15.0:
                    self._reset_auto_restore_tracking()

        self.last_gpu_state = state

    def _auto_restore_gpu_off(self):
        if not AUTO_RESTORE_WRAPPER.exists():
            log_operation('Automatic D3cold restoration failed: installed auto-off helper is missing.')
            return
        command = ['sudo', '-n', str(AUTO_RESTORE_WRAPPER)]
        log_detail(f'COMMAND START (automatic D3cold restore): {shlex.join(command)}')
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            log_detail(f'COMMAND ERROR (automatic D3cold restore): {e}')
            log_operation(f'Automatic D3cold restoration failed: {e}')
            return
        normal_err = _log_command_result(command, p, 'automatic D3cold restore')
        if p.returncode == 0:
            log_operation('Automatic D3cold restoration command completed successfully. ' + (p.stdout or '').strip())
        else:
            msg = (normal_err or p.stdout or 'The NVIDIA GPU could not be restored to D3cold.').strip()
            log_operation(f'Automatic D3cold restoration failed: {msg}')
        self.refresh()

    def restore_saved_state(self):
        # Profiles always take precedence; two startup controllers must never race.
        if not use_legacy_startup_restore(self.profile_store, self.preferences.get('restore_state')):
            return
        if self.profile_process is not None:
            return
        command = ['sudo', '-n', str(RESTORE_WRAPPER)]
        log_detail(f'COMMAND START (saved-state restore): {shlex.join(command)}')
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=90)
            normal_err = _log_command_result(command, p, 'saved-state restore')
            if p.returncode != 0:
                msg = (normal_err or p.stdout or 'Saved GPU/CPU settings could not be restored.').strip()
                log_operation(f'Saved-state restoration failed: {msg}')
            else:
                log_operation('Saved GPU/CPU settings restoration completed. ' + (p.stdout or '').strip())
                self.notify('startup_restore', 'Saved GPU and CPU settings restored after login.')
            self.refresh()
        except (OSError, subprocess.TimeoutExpired) as e:
            log_detail(f'COMMAND ERROR (saved-state restore): {e}')
            log_operation(f'Could not restore saved GPU settings: {e}')

    def present_profiles(self):
        self.tabs.setCurrentIndex(2)
        self.present_window()

    def _profile_selected(self):
        if not self.profile_store:
            return None
        item = self.profile_list.currentItem()
        return next((p for p in self.profile_store['profiles']
                     if p['id'] == item.data(Qt.ItemDataRole.UserRole)), None) if item else None

    def refresh_profile_list(self, select_id=None):
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        if self.profile_store is None:
            self.profile_list.addItem('Could not load saved profiles — see activity log')
        else:
            for profile in self.profile_store['profiles']:
                self.profile_list.addItem(profile['name'])
                self.profile_list.item(self.profile_list.count()-1).setData(Qt.ItemDataRole.UserRole, profile['id'])
        self.profile_list.blockSignals(False)
        if select_id:
            for index in range(self.profile_list.count()):
                if self.profile_list.item(index).data(Qt.ItemDataRole.UserRole) == select_id:
                    self.profile_list.setCurrentRow(index)
                    break
        elif self.profile_list.count() and self.profile_store and self.profile_store['profiles']:
            self.profile_list.setCurrentRow(0)
        for combo, selected in ((self.ac_profile_combo, self.profile_store['ac_profile'] if self.profile_store else None),
                                (self.battery_profile_combo, self.profile_store['battery_profile'] if self.profile_store else None)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem('Do nothing', None)
            if self.profile_store:
                for p in self.profile_store['profiles']:
                    combo.addItem(p['name'], p['id'])
            combo.setCurrentIndex(max(0, combo.findData(selected)))
            combo.blockSignals(False)
        self.auto_profiles_box.blockSignals(True)
        self.auto_profiles_box.setChecked(bool(self.profile_store and self.profile_store['auto_enabled']))
        self.auto_profiles_box.blockSignals(False)
        self.update_profile_controls()

    def update_profile_controls(self, *_args):
        enabled = self._profile_selected() is not None
        for button in (self.profile_edit_button, self.profile_delete_button, self.profile_apply_button):
            button.setEnabled(enabled and self.profile_process is None and not self.gpu_busy)
        self.profile_new_button.setEnabled(self.profile_store is not None and self.profile_process is None and not self.gpu_busy)

    def _save_profile_store(self):
        try:
            write_store(PROFILES_FILE, self.profile_store)
            return True
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Cannot save profiles', str(exc))
            log_operation(f'Could not save power profiles: {exc}')
            return False

    def _read_current_voltage_offsets(self):
        """Read real hardware offsets through the root-owned tool; never guess."""
        if not UNDERVOLT_HELPER.exists():
            return None
        try:
            result = subprocess.run(['sudo', '-n', str(UNDERVOLT_HELPER), 'read'],
                                    capture_output=True, text=True, timeout=8)
            if result.returncode:
                return None
            values = json.loads(result.stdout)
            offsets = tuple(round(values[key], 2) for key in ('cpu', 'cache', 'gpu'))
            validate_mv(offsets)
            return offsets
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.TimeoutExpired):
            return None

    def _snapshot_live_profile(self):
        cores, limits, freq = core_control_status(), limits_status(), frequency_status()
        voltages = self._read_current_voltage_offsets()
        state = self.ctrl.state()
        return {
            'gpu_state': state if state in ('D0', 'D3cold') else 'keep',
            'basic_guard': self.ctrl.basic_blocked(),
            'aggressive_guard': self.ctrl.aggressive_blocked(),
            'auto_gpu_sleep': bool(self.preferences['auto_restore_apps']),
            'disabled_cores': cores['disabled'] if cores['available'] else None,
            'frequency_mhz': freq['current_mhz'] if freq['available'] and not freq['mixed'] else None,
            'pl1_w': round(limits['pl1'], 1) if limits['available'] else None,
            'pl2_w': round(limits['pl2'], 1) if limits['available'] else None,
            'undervolt_cpu_mv': voltages[0] if voltages is not None else None,
            'undervolt_cache_mv': voltages[1] if voltages is not None else None,
            'undervolt_gpu_mv': voltages[2] if voltages is not None else None,
        }

    def create_profile(self):
        if self.profile_store is None:
            return
        existing = [p['name'] for p in self.profile_store['profiles']]
        number = len(existing) + 1
        while f'Profile {number}'.casefold() in {name.strip().casefold() for name in existing}:
            number += 1
        initial = {'name': f'Profile {number}',
                   'settings': self._snapshot_live_profile()}
        # A driverless wake with guards active can transiently report D0;
        # do not silently create an invalid "wake and guard" profile.
        if initial['settings']['gpu_state'] == 'D0' and (initial['settings']['basic_guard'] or initial['settings']['aggressive_guard']):
            initial['settings']['gpu_state'] = 'keep'
        editor = ProfileEditor(initial, self, taken_names=existing)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        profile = new_profile(editor.name.text(), editor.settings())
        new_store = dict(self.profile_store)
        new_store['profiles'] = [*self.profile_store['profiles'], profile]
        old = self.profile_store
        self.profile_store = new_store
        if not self._save_profile_store():
            self.profile_store = old
            return
        self.refresh_profile_list(profile['id'])
        log_operation(f'Created power profile: {profile["name"]}')

    def edit_profile(self):
        profile = self._profile_selected()
        if not profile:
            return
        editor = ProfileEditor(profile, self, taken_names=[p['name'] for p in self.profile_store['profiles'] if p['id'] != profile['id']])
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        changed = dict(profile, name=editor.name.text().strip(), settings=editor.settings())
        new_store = dict(self.profile_store)
        new_store['profiles'] = [changed if x['id'] == profile['id'] else x for x in self.profile_store['profiles']]
        previous = self.profile_store
        self.profile_store = new_store
        if not self._save_profile_store():
            self.profile_store = previous
            return
        self.last_auto_target = None
        self.refresh_profile_list(profile['id'])
        log_operation(f'Updated power profile: {changed["name"]}')

    def delete_profile(self):
        profile = self._profile_selected()
        if not profile:
            return
        answer = QMessageBox.question(self, 'Delete power profile', f'Delete “{profile["name"]}”?')
        if answer != QMessageBox.StandardButton.Yes:
            return
        store = dict(self.profile_store)
        store['profiles'] = [x for x in store['profiles'] if x['id'] != profile['id']]
        for key in ('ac_profile', 'battery_profile'):
            if store[key] == profile['id']:
                store[key] = None
        previous = self.profile_store
        self.profile_store = store
        if not self._save_profile_store():
            self.profile_store = previous
            return
        self.last_auto_target = None
        self.refresh_profile_list()
        log_operation(f'Deleted power profile: {profile["name"]}')

    def set_auto_profiles(self, enabled):
        if self.profile_store is None:
            return
        old = self.profile_store['auto_enabled']
        self.profile_store['auto_enabled'] = bool(enabled)
        if not self._save_profile_store():
            self.profile_store['auto_enabled'] = old
            self.refresh_profile_list()
            return
        self.last_auto_target = None
        if enabled:
            self.preferences['restore_before_auto'] = self.preferences['restore_state']
            self.preferences['restore_state'] = False
        else:
            old_remember = self.preferences['restore_before_auto']
            self.preferences['restore_before_auto'] = None
            # Enabling again intentionally captures the user's current choices.
            if old_remember:
                self.set_restore_state(True)
        try:
            save_preferences(self.preferences)
        except OSError as exc:
            log_operation(f'Could not persist Remember/Profiles link: {exc}')
        self.sync_remember_controls()
        log_operation('Automatic power profiles ' + ('enabled.' if enabled else 'disabled.'))
        if enabled:
            self.check_power_profile()

    def set_power_assignments(self, *_args):
        if self.profile_store is None:
            return
        before = (self.profile_store['ac_profile'], self.profile_store['battery_profile'])
        self.profile_store['ac_profile'] = self.ac_profile_combo.currentData()
        self.profile_store['battery_profile'] = self.battery_profile_combo.currentData()
        if not self._save_profile_store():
            self.profile_store['ac_profile'], self.profile_store['battery_profile'] = before
            self.refresh_profile_list()
            return
        self.last_auto_target = None
        self.check_power_profile()

    def enable_profile_autoswitch(self):
        self.profile_auto_ready = True
        self.check_power_profile()

    def check_power_profile(self):
        observed = power_source()
        if observed == self.power_candidate:
            self.power_candidate_count += 1
        else:
            self.power_candidate, self.power_candidate_count = observed, 1
        # Require two consistent checks, at least two seconds apart, rather
        # than switching due to one transient AC/USB-C power reading.
        if self.power_candidate_count < 2:
            return
        previous_source = self.stable_power_source
        self.stable_power_source = observed
        self.power_source_label.setText('Power: ' + ({'ac': 'Plugged in', 'battery': 'On battery'}.get(observed, 'Not detected')))
        # Never treat the first valid reading after login as unplugging or
        # plugging in; only notify on confirmed transitions between sources.
        self.notify('charger', power_source_message(previous_source, observed))
        if (not self.profile_auto_ready or self.profile_process is not None or self.gpu_busy or
                not self.profile_store or not self.profile_store['auto_enabled']):
            return
        choice = assignment(self.profile_store, observed)
        if choice is None:
            return
        signature = (observed, choice['id'], json.dumps(choice['settings'], sort_keys=True))
        if self.last_auto_target == signature:
            return
        self.last_auto_target = signature  # even on failure: never retry in a loop
        self.auto_retry_used = False       # a new transition earns one delayed retry
        self._start_profile_apply(choice, automatic=True)

    def apply_selected_profile(self):
        selected = self._profile_selected()
        if selected:
            self._start_profile_apply(selected, automatic=False)

    def _start_profile_apply(self, profile, automatic):
        if self.gpu_busy:
            log_operation('A GPU switch is still running; this profile request was skipped.')
            return
        if self.profile_process is not None:
            log_operation('Another profile is still applying; this request was skipped.')
            return
        if not PROFILE_HELPER.exists():
            QMessageBox.warning(self, 'Profiles unavailable', 'Profile helper missing. Re-run install.sh.') if not automatic else None
            log_operation('Cannot apply profile: privileged profile helper is not installed.')
            return
        if self.profile_store is None:
            return
        try:
            settings = validate_settings(profile['settings'])
        except ValueError as exc:
            log_operation(f'Cannot apply profile {profile["name"]}: {exc}')
            return
        payload = base64.urlsafe_b64encode(json.dumps(settings, separators=(',', ':')).encode('utf-8')).decode('ascii')
        self.profile_process = QProcess(self)
        self.profile_running_name = profile['name']
        self.profile_run_settings = settings
        self.profile_running_auto = automatic
        self.profile_status_label.setText('Applying profile: ' + profile['name'])
        if not automatic and self.profile_store['auto_enabled']:
            source = self.stable_power_source
            assigned = assignment(self.profile_store, source)
            if assigned:
                self.last_auto_target = (source, assigned['id'], json.dumps(assigned['settings'], sort_keys=True))
        process = self.profile_process
        process.setProgram('sudo')
        process.setArguments(['-n', str(PROFILE_HELPER), 'apply', payload])
        process.finished.connect(self._profile_finished)
        process.errorOccurred.connect(self._profile_process_error)
        process.start()
        self.profile_timeout = QTimer(self)
        self.profile_timeout.setSingleShot(True)
        self.profile_timeout.timeout.connect(self._profile_apply_timed_out)
        self.profile_timeout.start(180000)
        self.update_profile_controls()
        log_operation(f'{"Automatic" if automatic else "Manual"} profile apply started: {profile["name"]}')

    def notify_problem(self, text):
        """Warn about something the user cannot otherwise see; not tied to the optional notification switches."""
        self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.MessageIcon.Warning, 9000)

    def _report_automatic_failure(self, profile_name, details):
        """An unattended apply failed: say so, and try once more after a short delay."""
        retry = not self.auto_retry_used and not permanent_profile_failure(details)
        text = f'Automatic power profile \u201c{profile_name}\u201d was not fully applied. {short_profile_failure(details)} '
        if retry:
            self.auto_retry_used = True
            signature = self.last_auto_target
            QTimer.singleShot(AUTO_PROFILE_RETRY_MS, lambda sig=signature: self._retry_automatic_profile(sig))
            text += f'Retrying in {AUTO_PROFILE_RETRY_MS // 1000} seconds. '
            log_operation(f'Automatic profile {profile_name} will be retried once in {AUTO_PROFILE_RETRY_MS // 1000} seconds.')
        self.notify_problem(text + 'See View activity log.')

    def _retry_automatic_profile(self, signature):
        """Repeat the automatic apply, but only if nothing relevant changed in the meantime."""
        if (self.profile_process is not None or not self.profile_store or not self.profile_store['auto_enabled']
                or self.last_auto_target != signature):
            return
        choice = assignment(self.profile_store, self.stable_power_source)
        if choice is None or (self.stable_power_source, choice['id'],
                              json.dumps(choice['settings'], sort_keys=True)) != signature:
            return
        log_operation(f'Retrying automatic profile {choice["name"]}.')
        self._start_profile_apply(choice, automatic=True)

    def _profile_process_error(self, error):
        log_detail(f'Profile helper process error: {error}')
        # FailedToStart does not reliably emit finished() on every Qt/runtime
        # combination. Return the UI to an actionable state instead of leaving
        # the profile Apply button stuck disabled for this session.
        if error == QProcess.ProcessError.FailedToStart:
            QTimer.singleShot(0, lambda: self._profile_finished(-1, QProcess.ExitStatus.CrashExit))

    def _profile_apply_timed_out(self):
        if self.profile_process is not None and self.profile_process.state() != QProcess.ProcessState.NotRunning:
            log_operation(f'Profile {self.profile_running_name} timed out; changes may be partial. See detailed log.')
            self.profile_process.kill()

    def _profile_finished(self, exit_code, status):
        process = self.profile_process
        if process is None:
            return
        stdout = bytes(process.readAllStandardOutput()).decode('utf-8', 'replace').strip()
        stderr_raw = bytes(process.readAllStandardError()).decode('utf-8', 'replace').strip()
        # Helper audit lines belong in the technical log, not in the failure text shown to the user.
        audit_lines, stderr = _split_helper_stderr(stderr_raw)
        for entry in audit_lines:
            log_detail(f'HELPER: {entry}')
        profile_name = self.profile_running_name
        ok = exit_code == 0 and status == QProcess.ExitStatus.NormalExit
        if hasattr(self, 'profile_timeout'):
            self.profile_timeout.stop()
        log_detail(f'PROFILE APPLY: name={profile_name!r} exit={exit_code}; stdout={stdout}; stderr={stderr}')
        if ok:
            settings = self.profile_run_settings
            self.last_applied_profile = profile_name
            self.profile_status_label.setText('Last applied: ' + profile_name)
            self.profile_status_label.setToolTip('')
            if settings['auto_gpu_sleep'] != self.preferences['auto_restore_apps']:
                self.auto_restore_action.setChecked(settings['auto_gpu_sleep'])
            self.power_controls_dirty = False
            self.frequency_controls_dirty = False
            self.core_count_dirty = False
            log_operation(f'Applied power profile {profile_name}. {stdout}')
            if self.profile_running_auto:
                source = {'ac': 'plugged in', 'battery': 'on battery'}.get(self.stable_power_source)
                suffix = f' ({source})' if source else ''
                self.notify('auto_profile', f'Automatically applied power profile: {profile_name}{suffix}.')
        else:
            details = stderr or stdout or 'Profile helper failed.'
            self.profile_status_label.setText('Could not finish: ' + profile_name)
            self.profile_status_label.setToolTip(details[:600])
            log_operation(f'Could not fully apply profile {profile_name}: {details}. Some changes may already have been made.')
            if not self.profile_running_auto:
                QMessageBox.warning(self, 'Profile could not finish',
                    friendly_profile_failure(details) + '\n\nTechnical details are in View activity log.')
            else:
                self._report_automatic_failure(profile_name, details)
        process.deleteLater()
        self.profile_process = None
        self.profile_running_name = None
        self.update_profile_controls()
        self.refresh()
        if ok:
            self.undervolt_dirty = False
            QTimer.singleShot(1000, self.refresh_undervolt)
        # A power-source change while applying an older profile must be honored.
        if self.profile_store and self.profile_store['auto_enabled']:
            self.check_power_profile()

    def icon_for(self,state):
        # Draw a distinct chip icon ourselves; avoids misleading theme icons.
        pixmap=QPixmap(32,32); pixmap.fill(Qt.GlobalColor.transparent)
        painter=QPainter(pixmap); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if state == 'D0':
            fill=QColor('#16803c'); label='ON'
        elif str(state).strip().lower() == 'd3cold':
            fill=QColor(COLOR_RED); label='OFF'
        else:
            fill=QColor('#b7791f'); label='?'
        painter.setPen(QPen(fill.darker(130),1)); painter.setBrush(fill)
        painter.drawRoundedRect(6,6,20,20,4,4)
        painter.setPen(QPen(fill,2))
        for i in (9,14,19,24):
            painter.drawLine(i,2,i,6); painter.drawLine(i,26,i,30)
            painter.drawLine(2,i,6,i); painter.drawLine(26,i,30,i)
        painter.setPen(QColor('white')); painter.setFont(QFont('Sans Serif', 6, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(),Qt.AlignmentFlag.AlignCenter,label)
        painter.end()
        return QIcon(pixmap)
    def present_window(self):
        """Show and request focus for the already-running main window."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def tray_activated(self,reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick):
            self.present_window()
    def refresh(self):
        state=self.ctrl.state(); basic_blocked=self.ctrl.basic_blocked(); aggressive_blocked=self.ctrl.aggressive_blocked()
        previous = self.last_notified_gpu_state
        if state != previous:
            if {str(previous).lower(), str(state).lower()} == {'d0', 'd3cold'}:
                log_operation(f'GPU power state changed: {previous} → {state}')
                if state == 'D0':
                    # Give the waking app a moment to open NVIDIA device files.
                    # Delayed attribution is more useful than an immediate guess.
                    self.gpu_notification_token += 1
                    token = self.gpu_notification_token
                    QTimer.singleShot(900, lambda t=token: self._finish_gpu_wake_notification(t))
                else:
                    self.gpu_notification_token += 1
                    self.notify('gpu_state', gpu_transition_message('D0', 'D3cold'))
            elif state not in ('Unknown', 'GPU device not found'):
                log_operation(f'GPU power state changed/updated: {previous} → {state}')
            self.last_notified_gpu_state = state
        # Show only the actual GPU power state; guard status is represented by
        # the existing block checkbox, not appended to the power-state label.
        self.state_label.setText(str(state))
        if state != self._styled_state:
            # Re-applying a style sheet and re-polishing the button restyles and re-lays-out the whole
            # visible window, so do it when the state changes, not on every 1-second tick.
            self._styled_state = state
            _set_gpu_state_color(self.state_label, state)
            _set_power_button_style(self.power_btn, state)
        if state == 'D0':
            usage = self.current_nvidia_usage()
            active = sorted({name for name, is_active in usage if is_active})
            powered = sorted({name for name, is_active in usage if not is_active})
            if active:
                self.gpu_usage_label.setText('In use by: ' + ', '.join(active))
            elif powered:
                self.gpu_usage_label.setText('Powered on by ' + ', '.join(powered) + ' — not currently in use')
            else:
                self.gpu_usage_label.setText('NVIDIA is on; no application is currently using the GPU.')
        else:
            self.gpu_usage_label.setText('Sleeping — NVIDIA main power is off.' if state == 'D3cold' else 'Linux cannot confirm the current NVIDIA power state.')
        if state != self._tray_state:
            # Redrawing the chip and re-sending the tray icon every second is
            # wasted work (and D-Bus chatter on StatusNotifier trays).
            self._tray_state = state
            state_icon = self.icon_for(state)
            self.setWindowIcon(state_icon)
            self.tray_state.setText(f'GPU state: {state}'); self.tray.setIcon(state_icon); self.tray.setToolTip(f'{APP_NAME} — GPU {state}')
        # The battery and CPU read-outs only feed widgets (about 140 sysfs reads a second for the CPU page),
        # so skip them while the window is hidden in the tray; showEvent() brings them up to date at once.
        if self.isVisible():
            self.battery_label.setText(battery_label_text(battery_snapshot()))
        self.basic_block_box.blockSignals(True); self.basic_block_box.setChecked(basic_blocked); self.basic_block_box.blockSignals(False)
        self.aggressive_block_box.blockSignals(True); self.aggressive_block_box.setChecked(aggressive_blocked); self.aggressive_block_box.blockSignals(False)
        self.tray_basic_block.blockSignals(True); self.tray_basic_block.setChecked(basic_blocked); self.tray_basic_block.blockSignals(False)
        self.tray_aggressive_block.blockSignals(True); self.tray_aggressive_block.setChecked(aggressive_blocked); self.tray_aggressive_block.blockSignals(False)
        if not self.gpu_busy:
            self.power_btn.setText('Turn NVIDIA OFF' if state == 'D0' else 'Turn NVIDIA ON')
        if self.isVisible():
            self.refresh_cpu()

    def refresh_cpu(self):
        info = topology_status()
        limits = limits_status()
        if info['available']:
            _set_text(self.cpu_state_label, f"{info['active_cores']}/{info['total_cores']} physical cores · {info['active_threads']}/{info['total_threads']} threads")
        else:
            _set_text(self.cpu_state_label, 'CPU core information unavailable')
        freq = frequency_status()
        if limits['available']:
            detail = f"Long-term {limits['pl1']:.1f} W  ·  Boost {limits['pl2']:.1f} W"
        else:
            detail = 'CPU power limits unavailable'
        if freq['available']:
            detail += ('  ·  Different speed limits' if freq['mixed']
                       else f"  ·  Max {freq['current_mhz']} MHz")
        _set_text(self.cpu_limits_label, detail)
        if freq['available']:
            self.frequency_spin.blockSignals(True)
            _set_spin_range(self.frequency_spin, freq['min_mhz'], freq['max_mhz'])
            if not self.frequency_controls_dirty and freq['current_mhz'] is not None:
                _set_spin_value(self.frequency_spin, freq['current_mhz'])
            self.frequency_spin.blockSignals(False)
            _set_text(self.frequency_desc,
                f"Choose a speed between {freq['min_mhz']} and {freq['max_mhz']} MHz. "
                + ('The enabled cores have different speed limits.' if freq['mixed'] else
                   'Changing this does not change the CPU power limits.'))
        else:
            _set_text(self.frequency_desc, freq['reason'])
        self.frequency_spin.setEnabled(freq['available'])
        self.apply_frequency_button.setEnabled(freq['available'])
        self.restore_frequency_button.setEnabled(freq['available'])
        core = core_control_status()
        can_change = bool(core['available'] and core['maximum'] >= 1)
        self.core_count_spin.blockSignals(True)
        _set_spin_range(self.core_count_spin, 0, max(0, core['maximum']))
        if not self.core_count_dirty:
            _set_spin_value(self.core_count_spin, core['disabled'])
        self.core_count_spin.blockSignals(False)
        self.core_count_spin.setEnabled(can_change)
        self.apply_cores_button.setEnabled(can_change and self.core_count_spin.value() != core['disabled'])
        self.restore_cores_button.setEnabled(core['can_restore'])
        _set_text(self.cores_desc,
            (f"{info['total_cores']-core['disabled']} of {info['total_cores']} physical cores are on. "
             f"You can turn off up to {core['maximum']} cores. "
             'All threads of a chosen core change together; the first core stays on.')
            if core['available'] else core['reason'])
        self.apply_limits_button.setEnabled(limits['available'])
        defaults = public_firmware_boot_limits()
        self.restore_limits_button.setEnabled(limits['available'] and defaults is not None)
        if defaults:
            _set_text(self.cpu_info_label,
                f"Power limits recorded at startup: long-term {defaults['pl1']/1e6:.1f} W · "
                f"short boost {defaults['pl2']/1e6:.1f} W. Restore uses these recorded values, "
                "not the previous settings. Other power tools may change them.")
        else:
            _set_text(self.cpu_info_label,
                'Startup power limits have not been recorded. Reboot once after installation. '
                'The Restore button stays unavailable until real startup values are captured.')
        if limits['available'] and not self.power_controls_dirty:
            self.pl1_spin.blockSignals(True)
            self.pl2_spin.blockSignals(True)
            # Clamp to controls' editable range, but show actual value above.
            self.pl1_spin.setValue(max(5., min(55., limits['pl1'])))
            self.pl2_spin.setValue(max(5., min(70., limits['pl2'])))
            self.pl1_spin.blockSignals(False)
            self.pl2_spin.blockSignals(False)
        self.pl1_spin.setEnabled(limits['available'])
        self.pl2_spin.setEnabled(limits['available'])

    def _mark_cpu_power_dirty(self, *_args):
        self.power_controls_dirty = True

    def _mark_cpu_frequency_dirty(self, *_args):
        self.frequency_controls_dirty = True

    def apply_cpu_frequency(self):
        mhz = self.frequency_spin.value()
        ok, msg = self.ctrl.call_cpu('frequency-set', str(mhz))
        if ok:
            self.frequency_controls_dirty = False
        self.report(ok, msg, 'cpu-frequency-set')
        if not ok:
            QMessageBox.warning(self, 'CPU frequency limit', msg)

    def restore_cpu_frequency(self):
        ok, msg = self.ctrl.call_cpu('frequency-restore')
        if ok:
            self.frequency_controls_dirty = False
        self.report(ok, msg, 'cpu-frequency-restore')
        if not ok:
            QMessageBox.warning(self, 'Restore CPU hardware maximum', msg)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_tab_size)
        QTimer.singleShot(0, self.refresh)                # the window read-outs are not updated while hidden
        QTimer.singleShot(0, self.refresh_undervolt)      # neither is the periodic voltage poll

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'main_scroll_viewport', None) and event.type() == QEvent.Type.Resize:
            self._layout_responsive_sections(event.size().width())
            QTimer.singleShot(0, self._sync_tab_size)
        return super().eventFilter(watched, event)

    def _layout_responsive_sections(self, viewport_width):
        # Keep breakpoints tied to the actual visible width. Both cards must
        # fit comfortably before enabling two columns.
        usable_width = max(0, int(viewport_width) - 64)
        for area_name in ('cpu_sections', 'profiles_sections'):
            area = getattr(self, area_name, None)
            if area is not None:
                area.setAvailableWidth(usable_width)
        if hasattr(self, 'tabs'):
            self._sync_tab_size()

    def _sync_tab_size(self):
        """Request a new natural size; never force a height based on geometry.

        Repeated requests are harmless because the selected page's layout
        sizeHint is independent of previously assigned widget heights.
        """
        if not hasattr(self, 'tabs') or self.tabs.currentWidget() is None:
            return
        if hasattr(self, 'main_scroll_viewport'):
            self.tabs.setAvailablePageWidth(max(0, self.main_scroll_viewport.width() - 2 * CONTENT_MARGIN))
        self.tabs.remeasure_page()
        self.tabs.updateGeometry()
        parent = self.tabs.parentWidget()
        parent_layout = parent.layout() if parent is not None else None
        if parent_layout is not None:
            parent_layout.invalidate()

    def _store_selected_tab(self, index):
        self.preferences['selected_tab'] = index
        try:
            save_preferences(self.preferences)
        except OSError as exc:
            log_operation(f'Could not remember selected tab: {exc}')
        self._sync_tab_size()
        QTimer.singleShot(0, self._sync_tab_size)
        if index == 1:
            QTimer.singleShot(0, self.refresh_undervolt)

    def _mark_core_count_dirty(self, *_args):
        self.core_count_dirty = True
        self.apply_cores_button.setEnabled(True)

    def apply_core_count(self):
        count = self.core_count_spin.value()
        ok, msg = self.ctrl.call_cpu('cores-set', str(count))
        if ok:
            self.core_count_dirty = False
        self.report(ok, msg, f'cpu-cores-set-{count}')
        if not ok:
            QMessageBox.warning(self, 'CPU core control', msg)

    def restore_managed_cores(self):
        ok, msg = self.ctrl.call_cpu('cores-on')
        if ok:
            self.core_count_dirty = False
        self.report(ok, msg, 'cpu-cores-on')
        if not ok:
            QMessageBox.warning(self, 'Restore CPU cores', msg)

    def _mark_undervolt_dirty(self, *_args):
        # A background poll must not overwrite unsaved values the user is editing.
        self.undervolt_dirty = True

    def _set_voltage_status(self, message):
        self.cpu_voltages_label.setText(message)
        self.uv_status_label.setText(message)

    def _set_undervolt_available(self, available):
        """Grey out the voltage buttons while the intel-undervolt package is missing."""
        reason = '' if available else 'Install the intel-undervolt package first; this card re-checks every 15 seconds.'
        for button in (self.uv_apply_button, self.uv_reset_button):
            button.setEnabled(available)
            button.setToolTip(reason)

    def _poll_undervolt(self):
        """Periodic refresh of the on-screen voltage offsets.

        The numbers are display-only, and every read starts sudo, a Python helper and intel-undervolt, so
        nothing is read while the window is hidden in the tray. showEvent() refreshes as soon as it is shown.
        """
        if self.isVisible():
            self.refresh_undervolt()

    def refresh_undervolt(self):
        """Read real offsets asynchronously using the installed read-only command.

        sudo -n never requests a password. A missing or invalid sudoers rule
        is reported rather than falling back to an interactive prompt.
        """
        if self.voltage_read_process is not None:
            return
        if not UNDERVOLT_HELPER.exists():
            self._set_voltage_status('Voltage offsets unavailable (helper not installed).')
            return
        process = QProcess(self)
        self.voltage_read_process = process
        process.setProgram('sudo')
        process.setArguments(['-n', str(UNDERVOLT_HELPER), 'read'])
        process.finished.connect(self._voltage_read_finished)
        process.errorOccurred.connect(self._voltage_read_error)
        self.voltage_read_timeout = QTimer(self)
        self.voltage_read_timeout.setSingleShot(True)
        self.voltage_read_timeout.timeout.connect(self._voltage_read_timed_out)
        process.start()
        self.voltage_read_timeout.start(10000)

    def _voltage_read_timed_out(self):
        process = self.voltage_read_process
        if process is not None and process.state() != QProcess.ProcessState.NotRunning:
            self._set_voltage_status('Voltage offsets unavailable (read timed out).')
            process.kill()

    def _voltage_read_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._finish_voltage_read_cleanup()
            self._set_voltage_status('Voltage offsets unavailable (cannot start sudo).')

    def _finish_voltage_read_cleanup(self):
        if self.voltage_read_timeout is not None:
            self.voltage_read_timeout.stop()
            self.voltage_read_timeout.deleteLater()
            self.voltage_read_timeout = None
        process = self.voltage_read_process
        self.voltage_read_process = None
        if process is not None:
            process.deleteLater()

    def _log_voltage_problem(self, message):
        # The read repeats every 15 seconds; write each distinct problem to the log once.
        if message != self.last_voltage_problem:
            self.last_voltage_problem = message
            log_detail(message)

    def _voltage_read_finished(self, exit_code, status):
        process = self.voltage_read_process
        if process is None:
            return
        stdout = bytes(process.readAllStandardOutput()).decode('utf-8', 'replace')
        stderr = bytes(process.readAllStandardError()).decode('utf-8', 'replace')
        self._finish_voltage_read_cleanup()
        if exit_code != 0 or status != QProcess.ExitStatus.NormalExit:
            detail = (stderr or stdout).strip()
            if 'password' in detail.lower() or 'not allowed' in detail.lower():
                self._set_voltage_status('Voltage offsets unavailable (reinstall to update sudoers permissions).')
            elif 'not installed' in detail.lower():
                self._set_voltage_status('Voltage offsets unavailable (the intel-undervolt package is not installed).')
            else:
                self._set_voltage_status('Voltage offsets unavailable (see Activity Log).')
            self._set_undervolt_available('intel-undervolt is not installed' not in detail)
            if detail:
                self._log_voltage_problem('Voltage read failed: ' + detail[:400])
            return
        self.last_voltage_problem = None
        self._set_undervolt_available(True)
        try:
            values = parse_voltage_report(stdout)
            status_text = format_voltage_status(values)
            self._set_voltage_status(status_text)
            if not self.undervolt_dirty:
                for spin, name in zip(self.undervolt_spins, ('cpu', 'cache', 'gpu')):
                    spin.blockSignals(True)
                    spin.setValue(max(MIN_OFFSET_MV, min(0, values[name])))
                    spin.blockSignals(False)
        except (ValueError, TypeError) as exc:
            self._set_voltage_status('Voltage offsets unavailable (invalid hardware report).')
            log_detail('Voltage read returned invalid data: ' + str(exc))

    def apply_undervolt(self):
        values = [spin.value() for spin in self.undervolt_spins]
        if deep_undervolt_values(values):
            if not confirm_aggressive_undervolt(self, values):
                return
        else:
            question = ('Apply CPU {0} mV, cache {1} mV and Intel GPU {2} mV?\n\n'
                        'An unstable undervolt can immediately freeze or restart the laptop. '
                        'Save your work before proceeding.').format(*values)
            if QMessageBox.question(self, 'Confirm voltage changes', question) != QMessageBox.StandardButton.Yes:
                return
        try:
            result = subprocess.run(['sudo', '-n', str(UNDERVOLT_HELPER), 'set', *map(str, values)],
                                    capture_output=True, text=True, timeout=25)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout).strip()[:400])
            log_operation(f'Intel voltage offsets applied: CPU {values[0]} mV, cache {values[1]} mV, GPU {values[2]} mV.')
            self.undervolt_dirty = False
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            log_operation(f'Intel voltage control failed: {exc}')
            QMessageBox.warning(self, 'Could not change voltage', str(exc))
        self.refresh_undervolt()

    def reset_undervolt(self):
        for spin in self.undervolt_spins:
            spin.setValue(0)
        self.apply_undervolt()

    def apply_cpu_limits(self):
        pl1, pl2 = self.pl1_spin.value(), self.pl2_spin.value()
        if pl2 < pl1:
            QMessageBox.warning(self, 'Invalid CPU limits', 'PL2 must be greater than or equal to PL1.')
            return
        ok, msg = self.ctrl.call_cpu('limits-set', f'{pl1:.1f}', f'{pl2:.1f}')
        if ok:
            self.power_controls_dirty = False
        self.report(ok, msg, 'cpu-limits-set')
        if not ok:
            QMessageBox.warning(self, 'CPU power limits', msg)

    def restore_cpu_limits(self):
        ok, msg = self.ctrl.call_cpu('limits-restore')
        if ok:
            self.power_controls_dirty = False
        self.report(ok, msg, 'cpu-limits-restore')
        if not ok:
            QMessageBox.warning(self, 'Restore CPU limits', msg)

    def show_operation_log(self):
        if self.operation_log_dialog is None:
            log_operation('Operation log opened.')
            log_detail(f'LOG VIEW: simplified={LOG_FILE}; detailed={DETAILED_LOG_FILE}')
            dialog = OperationLogDialog(self)
            self.operation_log_dialog = dialog
            dialog.destroyed.connect(self._log_dialog_destroyed)
        else:
            dialog = self.operation_log_dialog
            dialog.load_log()
        # show(), unlike exec(), is modeless: all main controls remain usable.
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _log_dialog_destroyed(self, *_args):
        self.operation_log_dialog = None

    def sample_battery(self):
        watts, _, _ = battery_reading()
        self.history_b.add_value(watts)

    def _store_history_preference(self, key, label):
        self.preferences[key] = label
        try:
            save_preferences(self.preferences)
        except OSError as exc:
            log_operation(f'Could not save battery history settings: {exc}')

    def set_history_window(self, label):
        if label not in HistoryGraph.PERIODS:
            return
        self.history_b.set_period(label)
        self._store_history_preference('history_window', label)

    def set_sample_interval(self, label):
        if label not in HistoryGraph.SAMPLE_INTERVALS:
            return
        self.sample_timer.setInterval(HistoryGraph.SAMPLE_INTERVALS[label])
        self._store_history_preference('history_interval', label)

    def report(self, ok, msg, action):
        if ok:
            log_operation(f'Operation succeeded: {action}. {msg}')
        else:
            log_operation(f'Operation failed: {action}. {msg}')
        self.refresh()

    def toggle_gpu(self):
        if self.gpu_busy:
            return                                   # the button is already showing that a switch is running
        if self.profile_process is not None:
            QMessageBox.information(self, APP_NAME, 'A power profile is being applied. Try again when it has finished.')
            return
        state = self.ctrl.state()
        action = 'gpu-off' if state == 'D0' else 'gpu-on'
        if action == 'gpu-on':
            self.auto_restore_manual_until = time.monotonic() + 20.0
            self.auto_restore_armed = False
            self.auto_restore_seen_users.clear()
        self._start_gpu_action(action)

    def _gpu_command(self, action):
        return ['sudo', '-n', str(INSTALLED_HELPER), action, GPU, '--detail']

    def _set_gpu_busy(self, busy):
        """Spinner on the button, and keep conflicting controls from starting a second privileged operation."""
        self.gpu_busy = busy
        if busy:
            self.power_btn.setText('Turning NVIDIA off…' if self.gpu_action == 'gpu-off' else 'Turning NVIDIA on…')
        self.power_btn.setBusy(busy)
        self.tray_toggle.setEnabled(not busy)
        self.tray_toggle.setText('Working…' if busy else 'Toggle NVIDIA ON / OFF')
        for control in (self.basic_block_box, self.aggressive_block_box, self.tray_basic_block, self.tray_aggressive_block):
            control.setEnabled(not busy)
        self.update_profile_controls()
        if not busy:
            self.refresh()                           # restores the normal label and colour

    def _start_gpu_action(self, action):
        command = self._gpu_command(action)
        log_detail(f'COMMAND START (privileged action {action}): {shlex.join(command)}')
        self.gpu_action = action
        self.gpu_timed_out = False
        self.gpu_wait_target = None
        self.gpu_process = QProcess(self)
        self.gpu_process.finished.connect(self._gpu_action_finished)
        self.gpu_process.errorOccurred.connect(self._gpu_process_error)
        self._set_gpu_busy(True)
        self.gpu_process.start(command[0], command[1:])
        self.gpu_timeout.start(90000)

    def _gpu_process_error(self, error):
        log_detail(f'GPU helper process error: {error}')
        if error == QProcess.ProcessError.FailedToStart:
            # finished() is not reliably emitted for a process that never started.
            QTimer.singleShot(0, lambda: self._gpu_action_finished(-1, QProcess.ExitStatus.CrashExit))

    def _gpu_action_timed_out(self):
        if self.gpu_process is not None and self.gpu_process.state() != QProcess.ProcessState.NotRunning:
            self.gpu_timed_out = True
            log_operation(f'GPU operation {self.gpu_action} timed out.')
            self.gpu_process.kill()

    def _gpu_action_finished(self, exit_code, status):
        process = self.gpu_process
        if process is None:
            return
        self.gpu_timeout.stop()
        action = self.gpu_action
        stdout = bytes(process.readAllStandardOutput()).decode('utf-8', 'replace').strip()
        stderr = bytes(process.readAllStandardError()).decode('utf-8', 'replace')
        ok = exit_code == 0 and status == QProcess.ExitStatus.NormalExit
        normal_err = _log_command_result(self._gpu_command(action), SimpleNamespace(returncode=exit_code, stdout=stdout, stderr=stderr),
                                         f'privileged action {action}')
        process.deleteLater()
        self.gpu_process = None
        if ok:
            self.report(True, stdout or normal_err or 'Operation completed.', action)
            if action == 'gpu-on':
                self.last_gpu_state = 'D0'
            # Keep spinning until Linux reports the new power state (normally immediate).
            self.gpu_wait_target = 'D0' if action == 'gpu-on' else 'D3cold'
            self.gpu_wait_deadline = time.monotonic() + 10.0
            if self.ctrl.state() == self.gpu_wait_target:
                self._finish_gpu_wait()
            else:
                self.gpu_settle_timer.start()
            return
        details = ('The privileged operation timed out.' if self.gpu_timed_out
                   else normal_err or stdout or f'Operation failed (exit {exit_code}).')
        self.report(False, details, action)
        self._set_gpu_busy(False)
        self._show_gpu_failure(action, details)

    def _gpu_settle_tick(self):
        if self.ctrl.state() == self.gpu_wait_target or time.monotonic() > self.gpu_wait_deadline:
            self._finish_gpu_wait()

    def _finish_gpu_wait(self):
        self.gpu_settle_timer.stop()
        self.gpu_wait_target = None
        self._set_gpu_busy(False)

    def _show_gpu_failure(self, action, details):
        """Tell the user why a GPU switch failed, in plain words (a dialog, or a tray balloon when the window is hidden)."""
        in_use = sorted({name for name, active in self.current_nvidia_usage() if active}, key=str.casefold)
        title, text = gpu_failure_message(action, details, in_use)
        if self.isVisible():
            QMessageBox.warning(self, title, text + '\n\nTechnical details are in View activity log.')
        else:
            self.notify_problem(f'{title}. {text.split(chr(10) * 2)[0]}')

    def toggle_basic_block(self, checked): self._set_basic_block(bool(checked))
    def toggle_basic_block_from_tray(self, checked): self._set_basic_block(bool(checked))
    def _set_basic_block(self, enable):
        action='basic-block-on' if enable else 'basic-block-off'
        ok,msg=self.ctrl.call(action); self.report(ok,msg,action)
        if not ok:
            self._show_gpu_failure(action, msg)

    def toggle_aggressive_block(self, checked): self._set_aggressive_block(bool(checked))
    def toggle_aggressive_block_from_tray(self, checked): self._set_aggressive_block(bool(checked))
    def _set_aggressive_block(self, enable):
        action='aggressive-block-on' if enable else 'aggressive-block-off'
        ok,msg=self.ctrl.call(action); self.report(ok,msg,action)
        if not ok:
            self._show_gpu_failure(action, msg)
    def closeEvent(self,event):
        # Closing the window only hides it, but never veto a session shutdown:
        # a window that refuses to close can make the desktop cancel the logout.
        try:
            if QApplication.instance().isSavingSession():
                event.accept()
                return
        except AttributeError:
            pass
        event.ignore()
        self.hide()
        log_operation('Main window hidden; application continues running in the system tray.')


def main():
    app=QApplication(sys.argv); app.setApplicationName(APP_NAME); app.setDesktopFileName('thinkpad-nvidia-switch'); app.setWindowIcon(QIcon(str(APP_ICON))); app.setQuitOnLastWindowClosed(False); app.setPalette(dark_palette()); app.setStyleSheet(GLOBAL_STYLE)

    # Prevent multiple controller/tray instances. A second normal launch asks
    # the existing process to show its window; duplicate --minimized launches
    # exit quietly without changing the existing window state.
    lock_path = instance_lock_file()
    instance_lock = QLockFile(str(lock_path))
    if not instance_lock.tryLock(0):
        if instance_lock.error() == QLockFile.LockError.LockFailedError:
            _notify_existing_instance(show_window='--minimized' not in sys.argv)
            return 0
        # Not "already running": the lock itself could not be created. Say so
        # instead of exiting silently, which would look like the app never started.
        QMessageBox.critical(None, APP_NAME,
            f'Could not create the single-instance lock file:\n{lock_path}\n\n'
            'The application will not start because running multiple controller instances is unsafe.')
        return 1

    # We own the lock, so removing a stale endpoint from a previous crash is
    # safe: no other current release can legitimately be using this name.
    QLocalServer.removeServer(INSTANCE_SERVER_NAME)
    instance_server = QLocalServer(app)
    _configure_instance_server(instance_server)
    if not instance_server.listen(INSTANCE_SERVER_NAME):
        QMessageBox.critical(None, APP_NAME,
            'Could not create the single-instance control socket.\n'
            'The application will not start because running multiple controller instances is unsafe.')
        return 1

    # When started by autostart at login the panel's tray host can come up a few
    # seconds after this process; give it time instead of failing immediately.
    waited = 0.0
    while not QSystemTrayIcon.isSystemTrayAvailable() and waited < TRAY_WAIT_SECONDS:
        app.processEvents()
        time.sleep(0.25)
        waited += 0.25
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None,APP_NAME,'No system tray is available in this desktop session.'); return 1
    w=MainWindow()

    def process_instance_message(sock):
        data = bytes(sock.readAll()).decode('utf-8', errors='replace').strip().upper()
        if data == 'SHOW':
            log_operation('Additional launch request received; existing application window brought to the foreground.')
            w.present_window()
        elif data:
            log_detail(f'INSTANCE IPC: received {data!r} from a duplicate launcher.')
        sock.disconnectFromServer()

    def accept_instance_connections():
        while instance_server.hasPendingConnections():
            sock = instance_server.nextPendingConnection()
            if sock is None:
                continue
            sock.setParent(instance_server)
            sock.readyRead.connect(lambda s=sock: process_instance_message(s))
            sock.disconnected.connect(sock.deleteLater)
            if sock.bytesAvailable():
                process_instance_message(sock)

    instance_server.newConnection.connect(accept_instance_connections)
    app.aboutToQuit.connect(instance_server.close)

    if '--minimized' not in sys.argv: w.show()
    return app.exec()
if __name__ == '__main__': raise SystemExit(main())
