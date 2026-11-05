# SPDX-License-Identifier: MIT
"""Regression coverage for passwordless helper dispatch and voltage display."""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from voltage_status import parse_voltage_report, format_voltage_status


class VoltageStatusTests(unittest.TestCase):
    def test_formats_all_domains_at_two_decimal_places(self):
        values = parse_voltage_report('{"cpu": -30.27, "cache": -25.0, "gpu": 0}')
        self.assertEqual(format_voltage_status(values),
                         'Voltage offsets: CPU -30.27 mV · Cache -25.00 mV · Intel GPU 0.00 mV')

    def test_refuses_missing_or_invalid_hardware_values(self):
        for bad in ('{}', '{"cpu": 0, "cache": 0}', '[]',
                    '{"cpu": true,"cache":0,"gpu":0}',
                    '{"cpu":NaN,"cache":0,"gpu":0}',
                    '{"cpu":-30,"cache":"-30","gpu":0}'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_voltage_report(bad)


class PasswordlessOperationsTests(unittest.TestCase):
    def test_manual_gpu_and_cpu_paths_use_noninteractive_sudo(self):
        source = (ROOT / 'src' / 'optimus_tray.py').read_text()
        tree = ast.parse(source)
        controller = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Controller')
        for name in ('call', 'call_cpu'):
            method = next(n for n in controller.body if isinstance(n, ast.FunctionDef) and n.name == name)
            method_text = ast.get_source_segment(source, method)
            self.assertIn("['sudo', '-n',", method_text)
            self.assertNotIn('pkexec', method_text)

    def test_installer_extends_existing_passwordless_sudoers(self):
        installer = (ROOT / 'install.sh').read_text()
        self.assertIn('"$HELPER_DEST" "$CPU_HELPER_DEST" | sudo tee "$SUDOERS_STAGE"', installer)
        self.assertIn('visudo', installer)
        self.assertIn('sudo chmod 0440 "$SUDOERS_STAGE"', installer)
        self.assertNotIn('require_command pkexec', installer)

    def test_dashboard_gets_automatic_readings_without_freezing_ui(self):
        source = (ROOT / 'src' / 'optimus_tray.py').read_text()
        self.assertIn("self.cpu_voltages_label = QLabel('Voltage offsets: checking…')", source)
        self.assertIn('cpu_box.addWidget(self.cpu_voltages_label)', source)
        self.assertIn('self.undervolt_refresh_timer.start(15000)', source)
        self.assertIn('process = QProcess(self)', source)
        self.assertIn("process.setArguments(['-n', str(UNDERVOLT_HELPER), 'read'])", source)
        self.assertIn('if not self.undervolt_dirty:', source)
        self.assertIn('format_voltage_status(values)', source)


if __name__ == '__main__':
    unittest.main()
