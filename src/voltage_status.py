# SPDX-License-Identifier: MIT
"""Strict, unprivileged parsing and display of read-only Intel voltage offsets."""
import json
import math

DOMAINS = ('cpu', 'cache', 'gpu')


def parse_voltage_report(text):
    """Only accept a complete JSON result with finite numeric millivolt values."""
    report = json.loads(text)
    if not isinstance(report, dict):
        raise ValueError('Voltage report must be a JSON object')
    parsed = {}
    for key in DOMAINS:
        value = report.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'Invalid {key} voltage offset')
        parsed[key] = float(value)
    return parsed


def format_voltage_status(values):
    return ('Voltage offsets: CPU {cpu:.2f} mV · Cache {cache:.2f} mV · '
            'Intel GPU {gpu:.2f} mV').format(**values)
