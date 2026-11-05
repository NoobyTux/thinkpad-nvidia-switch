# SPDX-License-Identifier: MIT
"""Regression tests for the stable Intel-only KWin restriction (render node and display device)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import kwin_render_setup as ks


class RenderSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.by_path = self.root / 'dev' / 'dri' / 'by-path'
        self.pci = self.root / 'sys' / 'bus' / 'pci' / 'devices'
        self.by_path.mkdir(parents=True)
        self.pci.mkdir(parents=True)
        self.home.mkdir()
        self.mesa = self.root / '50_mesa.json'
        self.mesa.write_text('{}')
        self.add_device('0000:00:02.0', '0x8086', 'renderD128', 'card1')
        # The Lenovo Y520 log: NVIDIA registers card0 / renderD129, Intel card1 / renderD128.
        self.add_device('0000:02:00.0', '0x10de', 'renderD129', 'card0')

    def add_device(self, addr, vendor, render, card):
        d = self.pci / addr
        d.mkdir(parents=True, exist_ok=True)
        (d / 'vendor').write_text(vendor + '\n')
        (d / 'class').write_text('0x030000\n')
        links = {}
        for kind, name in (('render', render), ('card', card)):
            node = self.root / 'dev' / 'dri' / name
            node.touch(exist_ok=True)
            symlink = self.by_path / ('pci-' + addr + '-' + kind)
            if symlink.is_symlink():
                symlink.unlink()
            symlink.symlink_to(node)
            links[kind] = symlink
        return links['render']

    def test_select_only_intel_and_keep_pci_symlink(self):
        intel = ks.find_intel_render_link(self.by_path, self.pci)
        self.assertEqual(intel.name, 'pci-0000:00:02.0-render')
        message = ks.install(self.home, self.by_path, self.pci, self.mesa)
        alias, dropin = ks.locations(self.home)
        self.assertEqual(alias.readlink(), intel)
        self.assertIn('KWIN_RENDER_NODES=' + str(alias), dropin.read_text())
        self.assertIn('__EGL_VENDOR_LIBRARY_FILENAMES=', dropin.read_text())
        self.assertNotIn('renderD128', dropin.read_text())
        self.assertNotIn('renderD129', dropin.read_text())
        self.assertIn('KWin drop-in:', message)
        # Renumbering affects the PCI by-path target, NOT the link in the drop-in.
        newer = self.root / 'dev' / 'dri' / 'renderD135'
        newer.touch()
        intel.unlink()
        intel.symlink_to(newer)
        self.assertEqual(alias.resolve(), newer)

    def test_existing_manual_overrides_preserved(self):
        other = self.home / '.config/systemd/user/plasma-kwin_wayland.service.d/70-intel-render.conf'
        other.parent.mkdir(parents=True)
        other.write_text('[Service]\nEnvironment="KWIN_RENDER_NODES=/dev/dri/renderD128"\n')
        ks.install(self.home, self.by_path, self.pci, self.mesa)
        self.assertTrue(other.exists())
        # A second installer run should still be safe.
        ks.install(self.home, self.by_path, self.pci, self.mesa)
        self.assertEqual(other.read_text(), '[Service]\nEnvironment="KWIN_RENDER_NODES=/dev/dri/renderD128"\n')

    def test_refuse_existing_unrelated_dropin_and_alias(self):
        alias, dropin = ks.locations(self.home)
        dropin.parent.mkdir(parents=True)
        dropin.write_text('# manual\n')
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            ks.install(self.home, self.by_path, self.pci, self.mesa)
        dropin.unlink()
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.write_text('not a symlink')
        with self.assertRaisesRegex(ValueError, 'existing path'):
            ks.install(self.home, self.by_path, self.pci, self.mesa)

    def test_refuse_ambiguous_and_missing_intel(self):
        second = self.add_device('0000:01:00.0', '0x8086', 'renderD130', 'card2')
        with self.assertRaisesRegex(ValueError, 'detected 2'):
            ks.find_intel_render_link(self.by_path, self.pci)
        second.unlink()
        intel = self.by_path / 'pci-0000:00:02.0-render'
        intel.unlink()
        with self.assertRaisesRegex(ValueError, 'detected 0'):
            ks.find_intel_render_link(self.by_path, self.pci)

    def test_modified_dropin_not_removed(self):
        ks.install(self.home, self.by_path, self.pci, self.mesa)
        alias, dropin = ks.locations(self.home)
        dropin.write_text(dropin.read_text() + '# user tweak\n')
        with self.assertRaisesRegex(ValueError, 'modified'):
            ks.uninstall(self.home, self.by_path)
        self.assertTrue(alias.is_symlink())
        self.assertTrue(dropin.exists())

    def test_missing_mesa_refuses_without_touching_home(self):
        self.mesa.unlink()
        with self.assertRaisesRegex(ValueError, 'Mesa EGL vendor'):
            ks.install(self.home, self.by_path, self.pci, self.mesa)
        alias, dropin = ks.locations(self.home)
        self.assertFalse(alias.exists())
        self.assertFalse(dropin.exists())

    def test_non_intel_pci_vendor_not_selected(self):
        (self.pci / '0000:00:02.0' / 'vendor').write_text('0x10de\n')
        with self.assertRaisesRegex(ValueError, 'detected 0'):
            ks.find_intel_render_link(self.by_path, self.pci)

    def test_version_gate(self):
        mockrun = Mock(return_value=Mock(returncode=0, stdout='kwin 6.8.0', stderr=''))
        self.assertTrue(ks.kwin_supported(which=lambda _: '/usr/bin/kwin_wayland', run=mockrun))
        mockrun.return_value.stdout = 'kwin 6.7.3'
        self.assertFalse(ks.kwin_supported(which=lambda _: '/usr/bin/kwin_wayland', run=mockrun))
        self.assertFalse(ks.kwin_supported(which=lambda _: None, run=mockrun))

    def test_colon_in_home_rejected(self):
        alias, _ = ks.locations(self.root / 'someone:else')
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            ks.systemd_content(alias)

    def test_uninstall_keeps_no_unrelated_files(self):
        ks.install(self.home, self.by_path, self.pci, self.mesa)
        alias, dropin = ks.locations(self.home)
        other = dropin.parent / '50-mesa-only.conf'
        other.write_text('[Service]\nEnvironment="MESA=1"\n')
        self.assertIn('Removed', ks.uninstall(self.home, self.by_path))
        self.assertFalse(alias.is_symlink())
        self.assertFalse(dropin.exists())
        self.assertTrue(other.exists())


    def card_alias(self):
        return ks.card_location(self.home)

    def test_display_device_is_restricted_too(self):
        """Without KWIN_DRM_DEVICES KWin opens NVIDIA's card0 and nvidia_drm can never be unloaded."""
        intel_card = ks.find_intel_card_link(self.by_path, self.pci)
        self.assertEqual(intel_card.name, 'pci-0000:00:02.0-card')
        message = ks.install(self.home, self.by_path, self.pci, self.mesa)
        alias, dropin = ks.locations(self.home)
        text = dropin.read_text()
        self.assertEqual(self.card_alias().readlink(), intel_card)
        self.assertIn('Environment="KWIN_DRM_DEVICES=' + str(self.card_alias()) + '"', text)
        self.assertIn('Environment="KWIN_RENDER_NODES=' + str(alias) + '"', text)
        for raw in ('card0', 'card1', 'renderD128', 'renderD129'):
            self.assertNotIn(raw, text)                    # only app-owned, number-independent aliases
        self.assertNotIn(':', str(self.card_alias()))      # ':' separates the list in KWin
        self.assertIn('Intel display device:', message)
        # KWin canonicalises the entry, so it must end at Intel's card, not NVIDIA's
        self.assertEqual(self.card_alias().resolve(), self.root / 'dev' / 'dri' / 'card1')

    def test_missing_display_link_changes_nothing(self):
        (self.by_path / 'pci-0000:00:02.0-card').unlink()
        with self.assertRaisesRegex(ValueError, 'Intel graphics card path; detected 0'):
            ks.install(self.home, self.by_path, self.pci, self.mesa)
        alias, dropin = ks.locations(self.home)
        self.assertFalse(alias.is_symlink() or self.card_alias().is_symlink() or dropin.exists())

    def test_render_and_display_must_be_the_same_device(self):
        (self.by_path / 'pci-0000:00:02.0-card').unlink()
        other = self.pci / '0000:00:03.0'
        other.mkdir()
        (other / 'vendor').write_text('0x8086\n')
        (other / 'class').write_text('0x030000\n')
        (self.by_path / 'pci-0000:00:03.0-card').symlink_to(self.root / 'dev' / 'dri' / 'card1')
        with self.assertRaisesRegex(ValueError, 'different devices'):
            ks.install(self.home, self.by_path, self.pci, self.mesa)
        self.assertFalse(ks.locations(self.home)[1].exists())

    def test_blocked_display_alias_leaves_the_render_alias_and_dropin_untouched(self):
        self.card_alias().parent.mkdir(parents=True)
        self.card_alias().write_text('not a symlink')
        with self.assertRaisesRegex(ValueError, 'existing path'):
            ks.install(self.home, self.by_path, self.pci, self.mesa)
        alias, dropin = ks.locations(self.home)
        self.assertFalse(alias.is_symlink() or dropin.exists())      # everything is checked before anything changes

    def test_unrelated_display_symlink_is_not_replaced(self):
        self.card_alias().parent.mkdir(parents=True)
        self.card_alias().symlink_to('/etc/hostname')
        with self.assertRaisesRegex(ValueError, 'unrelated symlink'):
            ks.install(self.home, self.by_path, self.pci, self.mesa)

    def test_uninstall_removes_both_aliases(self):
        ks.install(self.home, self.by_path, self.pci, self.mesa)
        alias, dropin = ks.locations(self.home)
        ks.uninstall(self.home, self.by_path)
        self.assertFalse(alias.is_symlink() or self.card_alias().is_symlink() or dropin.exists())

    def test_modified_display_alias_is_not_removed(self):
        ks.install(self.home, self.by_path, self.pci, self.mesa)
        self.card_alias().unlink()
        self.card_alias().symlink_to('/etc/hostname')
        with self.assertRaisesRegex(ValueError, 'display alias was modified'):
            ks.uninstall(self.home, self.by_path)
        self.assertTrue(self.card_alias().is_symlink())

    def test_dropin_from_an_earlier_release_is_upgraded_and_can_be_removed(self):
        """Releases before 2.6.9 wrote only KWIN_RENDER_NODES and no display alias."""
        alias, dropin = ks.locations(self.home)
        alias.parent.mkdir(parents=True)
        dropin.parent.mkdir(parents=True)
        alias.symlink_to(self.by_path / 'pci-0000:00:02.0-render')
        old_text = ks.systemd_content(alias)
        self.assertNotIn('KWIN_DRM_DEVICES', old_text)
        dropin.write_text(old_text)
        # removing the old installation works (and does not need a display alias)
        self.assertIn('Removed', ks.uninstall(self.home, self.by_path))
        self.assertFalse(dropin.exists() or alias.is_symlink())
        # installing over an old installation upgrades it in place
        alias.symlink_to(self.by_path / 'pci-0000:00:02.0-render')
        dropin.write_text(old_text)
        ks.install(self.home, self.by_path, self.pci, self.mesa)
        self.assertIn('KWIN_DRM_DEVICES=', dropin.read_text())
        self.assertTrue(self.card_alias().is_symlink())

    def test_dropin_text_is_stable_for_the_documented_format(self):
        alias = Path('/home/u/.config/thinkpad-nvidia-switch/kwin-intel-render')
        card = Path('/home/u/.config/thinkpad-nvidia-switch/kwin-intel-card')
        self.assertEqual(ks.systemd_content(alias, card), ks.MARKER + '[Service]\n'
                         'Environment="KWIN_RENDER_NODES=' + str(alias) + '"\n'
                         'Environment="KWIN_DRM_DEVICES=' + str(card) + '"\n'
                         'Environment="__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json"\n')
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            ks.systemd_content(alias, Path('/home/a:b/kwin-intel-card'))


if __name__ == '__main__':
    unittest.main()
