# SPDX-License-Identifier: MIT
"""Portable notification policy and regression coverage without Qt/hardware."""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from notification_rules import (
    NOTIFICATION_DEFAULTS, gpu_transition_message, notification_settings,
    power_source_message,
)


class NotificationPolicyTests(unittest.TestCase):
    def test_upgrade_keeps_old_gpu_notifications_without_enabling_new_ones(self):
        self.assertEqual(notification_settings(None), NOTIFICATION_DEFAULTS)
        self.assertTrue(notification_settings({})['gpu_state'])
        self.assertFalse(notification_settings({})['charger'])

    def test_toggle_preferences_validate_types_and_preserve_other_values(self):
        settings = notification_settings({'gpu_state': False, 'charger': True,
                                          'auto_profile': 1, 'startup_restore': 'true',
                                          'unexpected': True})
        self.assertFalse(settings['gpu_state'])
        self.assertTrue(settings['charger'])
        self.assertFalse(settings['auto_profile'])
        self.assertFalse(settings['startup_restore'])
        self.assertNotIn('unexpected', settings)

    def test_gpu_sleep_and_unknown_waker(self):
        self.assertIn('D3cold', gpu_transition_message('D0', 'D3cold'))
        self.assertIn('not identified', gpu_transition_message('D3cold', 'D0'))
        self.assertIsNone(gpu_transition_message('Unknown', 'D0'))
        self.assertIsNone(gpu_transition_message('D0', 'D0'))

    def test_gpu_names_indicate_usage_not_unproven_cause(self):
        msg = gpu_transition_message('D3cold', 'D0', ['Steam', 'Steam'])
        self.assertIn('In use by Steam', msg)
        self.assertEqual(msg.count('Steam'), 1)
        many = gpu_transition_message('D3cold', 'D0', ['Steam', 'Firefox'])
        self.assertIn('Firefox', many)
        self.assertIn('Steam', many)
        self.assertNotIn('activated by', many.lower())

    def test_no_charger_popup_at_login_or_unknown(self):
        self.assertIsNone(power_source_message(None, 'ac'))
        self.assertIsNone(power_source_message('unknown', 'battery'))
        self.assertIsNone(power_source_message('ac', 'ac'))

    def test_charger_transition_both_directions(self):
        self.assertIn('disconnected', power_source_message('ac', 'battery'))
        self.assertIn('connected', power_source_message('battery', 'ac'))

    def test_gui_wires_menu_and_restoration_only_on_success(self):
        text = (ROOT/'src/optimus_tray.py').read_text()
        ast.parse(text)
        self.assertIn("self.menuBar().addMenu('Notifications')", text)
        for event in ('gpu_state', 'charger', 'auto_profile', 'startup_restore'):
            self.assertIn("self.notify('" + event + "'", text)
        self.assertIn("'notifications': notification_settings(data.get('notifications'))", text)
        self.assertIn("previous_source = self.stable_power_source", text)
        self.assertIn("if self.profile_running_auto:", text)
        self.assertIn("if p.returncode != 0:", text)
        self.assertIn("self.notify('startup_restore', 'Saved GPU and CPU settings restored after login.')", text)
        self.assertIn('QTimer.singleShot(900, lambda t=token: self._finish_gpu_wake_notification(t))', text)

    def test_gui_never_shrinks_pages_from_previous_rendered_height(self):
        text = (ROOT/'src/optimus_tray.py').read_text()
        self.assertIn('class ContentSizedTabWidget(QTabWidget)', text)
        self.assertIn('content_layout.sizeHint().height()', text)
        self.assertIn('content_layout.minimumSize().height()', text)
        self.assertNotIn('card.mapTo(page, QPoint(0, card.height()))', text)
        self.assertNotIn('self.tabs.setFixedHeight(', text)
        self.assertNotIn('self.tabs.setMaximumHeight(', text)
        self.assertIn('self.tabs.updateGeometry()', text)



if __name__ == '__main__':
    unittest.main()
