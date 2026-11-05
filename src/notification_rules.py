# SPDX-License-Identifier: MIT
"""Unprivileged, side-effect-free notification defaults and message policy.

Linux PCI states and currently open device nodes do not prove which process
caused a wakeup; attribution deliberately says 'using' rather than 'caused'.
"""

from profiles import DRIVER_MISSING_TEXT, explain_busy, is_busy_failure

NOTIFICATION_DEFAULTS = {
    'gpu_state': True,
    'charger': False,
    'auto_profile': False,
    'startup_restore': False,
}


def notification_settings(saved):
    """Merge a legacy or partially valid preferences file without surprises."""
    result = dict(NOTIFICATION_DEFAULTS)
    if isinstance(saved, dict):
        for key in result:
            if type(saved.get(key)) is bool:
                result[key] = saved[key]
    return result


def gpu_transition_message(previous, current, users=()):
    """Return text for a genuine wake/sleep transition, never guess the cause."""
    if (previous, current) not in (('D3cold', 'D0'), ('D0', 'D3cold')):
        return None
    if current == 'D3cold':
        return 'NVIDIA GPU went to sleep (D3cold).'
    labels = sorted({str(label).replace('\n', ' ').replace('\r', ' ').strip()[:60] for label in users if str(label).strip()}, key=str.casefold)
    if len(labels) == 1:
        return f'NVIDIA GPU woke up (D0) · In use by {labels[0]}.'
    if len(labels) > 1:
        return f'NVIDIA GPU woke up (D0) · In use by {", ".join(labels[:3])}' + (', and others.' if len(labels) > 3 else '.')
    return 'NVIDIA GPU woke up (D0) · Activating application not identified.'


def power_source_message(previous, current):
    """No synthetic plugged/unplugged event at initial desktop startup."""
    if previous not in ('ac', 'battery') or current not in ('ac', 'battery') or previous == current:
        return None
    return 'Charger connected · Running on AC power.' if current == 'ac' else 'Charger disconnected · Running on battery.'


_ACTION_TITLES = {
    'gpu-off': ("Can't turn NVIDIA off", 'turn NVIDIA off'),
    'gpu-on': ("Couldn't turn NVIDIA on", 'turn NVIDIA on'),
    'basic-block-on': ("Couldn't turn on Basic protection", 'turn on Basic protection'),
    'aggressive-block-on': ("Couldn't turn on Stronger protection", 'turn on Stronger protection'),
}


def _last_reason(details):
    """The most useful single line of a helper error, without the echoed command."""
    lines = [line.strip() for line in str(details).splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith('Command failed:')] or lines
    return (lines[-1] if lines else 'The operation failed.')[:220]


def gpu_failure_message(action, details, in_use=()):
    """(title, text) for a failed manual GPU or protection switch; never the raw helper output.

    *in_use* holds the names of programs the window itself currently sees using NVIDIA.
    """
    title, verb = _ACTION_TITLES.get(action, ('Could not change the NVIDIA setting', 'change the NVIDIA setting'))
    low = str(details).lower()
    if 'not found in directory' in low:
        return 'NVIDIA driver not available', DRIVER_MISSING_TEXT
    if is_busy_failure(details) and action != 'gpu-on':
        reason, advice = explain_busy(details, in_use)
        subject = 'NVIDIA cannot be turned off' if action == 'gpu-off' else f'Could not {verb}'
        return title, f'{subject} because {reason}.\n\n{advice}'
    return title, f'{_last_reason(details)}\n\nSee View activity log for technical details.'
