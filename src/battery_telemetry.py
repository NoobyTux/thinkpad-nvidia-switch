# SPDX-License-Identifier: MIT
"""Unprivileged Linux power-supply telemetry; estimates are never fabricated."""
from pathlib import Path


def number(path, magnitude=False):
    """Read a numeric sysfs attribute; negative or unreadable values are None.

    Some drivers report ``power_now``/``current_now`` as a negative number while
    discharging. The battery ``status`` already says which way energy flows, so
    for those rate attributes (``magnitude=True``) only the size matters.
    """
    try:
        value = float(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None
    if magnitude:
        value = abs(value)
    return value if value >= 0 else None


def human_duration(hours):
    if hours is None or not 0 < hours <= 168:
        return None
    minutes = round(hours * 60)
    return f'{minutes // 60} h {minutes % 60:02} min' if minutes >= 60 else f'{minutes} min'


def battery_snapshot(root=Path('/sys/class/power_supply')):
    """Returns {capacity, status, discharge_w, charge_w, remaining, to_full}.

    Uses *battery-side* power measurements, not AC adapter input wattage.
    Prefer energy over charge units; only combine charge with measured voltage.
    No invented estimate if the hardware does not expose remaining capacity.
    """
    result = {'capacity': None, 'status': 'Battery telemetry unavailable',
              'discharge_w': None, 'charge_w': None, 'remaining': None, 'to_full': None}
    try:
        devices = sorted(Path(root).iterdir())
    except OSError:
        return result
    for p in devices:
        try:
            if (p / 'type').read_text().strip() != 'Battery':
                continue
        except OSError:
            continue
        try:
            status = (p / 'status').read_text().strip()
        except OSError:
            status = 'Unknown'
        cap = number(p / 'capacity')
        power_uw = number(p / 'power_now', magnitude=True)
        voltage_uv = number(p / 'voltage_now')
        current_ua = number(p / 'current_now', magnitude=True)
        if power_uw is None and current_ua is not None and voltage_uv is not None:
            power_uw = current_ua * voltage_uv / 1_000_000
        power_w = power_uw / 1_000_000 if power_uw is not None else None
        energy_now = number(p / 'energy_now')
        energy_full = number(p / 'energy_full')
        if energy_now is None and voltage_uv is not None:
            charge = number(p / 'charge_now')
            energy_now = charge * voltage_uv / 1_000_000 if charge is not None else None
        if energy_full is None and voltage_uv is not None:
            full_charge = number(p / 'charge_full')
            energy_full = full_charge * voltage_uv / 1_000_000 if full_charge is not None else None
        energy_wh = energy_now / 1_000_000 if energy_now is not None else None
        full_wh = energy_full / 1_000_000 if energy_full is not None else None
        result.update(capacity=int(cap) if cap is not None else None, status=status)
        # Hardware-provided time estimates are useful when energy/current is missing.
        time_empty = number(p / 'time_to_empty_now')
        time_full = number(p / 'time_to_full_now')
        if status == 'Discharging':
            result['discharge_w'] = power_w
            hours = energy_wh / power_w if energy_wh is not None and power_w and power_w > 0 else None
            result['remaining'] = human_duration(hours if hours is not None else time_empty / 3600 if time_empty else None)
        elif status == 'Charging':
            result['charge_w'] = power_w
            hours = max(0, full_wh - energy_wh) / power_w if (full_wh is not None and energy_wh is not None and power_w and power_w > 0) else None
            result['to_full'] = human_duration(hours if hours is not None else time_full / 3600 if time_full else None)
        elif status == 'Full':
            result['to_full'] = 'Fully charged'
        return result
    return result


def battery_label_text(data):
    capacity = f"{data['capacity']}%" if data['capacity'] is not None else '—'
    status = data['status']
    if status == 'Discharging':
        rate = f"{data['discharge_w']:.2f} W" if data['discharge_w'] is not None else 'rate unavailable'
        remaining = data['remaining'] or 'time unavailable'
        return f'Battery {capacity} · Discharging · {rate} · {remaining} left'
    if status == 'Charging':
        rate = f"{data['charge_w']:.2f} W" if data['charge_w'] is not None else 'rate unavailable'
        to_full = data['to_full'] or 'time unavailable'
        return f'Battery {capacity} · Charging · {rate} into battery · {to_full} to full'
    if status == 'Full':
        return f'Battery {capacity} · Fully charged'
    return f'Battery {capacity} · {status}'
