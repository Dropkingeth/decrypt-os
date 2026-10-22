import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest

import desktop_layout
from guard import Refusal


PINNED = b'''--ozone-platform=wayland
--ozone-platform-hint=wayland
--password-store=gnome-libsecret
--enable-features=TouchpadOverscrollHistoryNavigation
--load-extension=/usr/share/omarchy/default/chromium/extensions/copy-url,/usr/share/omarchy/default/chromium/extensions/yt-dlp,/usr/share/omarchy/default/chromium/extensions/whatsapp-slim
'''


class ChromiumFirstRun(unittest.TestCase):
    def fixture(self, root):
        home = Path(root) / 'customer'
        config = home / '.config'
        config.mkdir(parents=True)
        path = config / 'chromium-flags.conf'
        path.write_bytes(PINNED)
        path.chmod(0o644)
        return home, path

    def test_exact_append_preserves_mode_and_existing_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, path = self.fixture(tmp)
            desktop_layout.configure_test_chromium(home)
            self.assertEqual(path.read_bytes(), PINNED + desktop_layout.TEST_CHROMIUM_FLAGS)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             desktop_layout.TEST_CHROMIUM_FLAGS_SHA256)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_changed_source_refused_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, path = self.fixture(tmp)
            changed = PINNED.replace(b'wayland', b'invalid', 1)
            path.write_bytes(changed)
            with self.assertRaises(Refusal):
                desktop_layout.configure_test_chromium(home)
            self.assertEqual(path.read_bytes(), changed)

    def test_already_applied_refused_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, path = self.fixture(tmp)
            desktop_layout.configure_test_chromium(home)
            before = path.read_bytes()
            with self.assertRaises(Refusal):
                desktop_layout.configure_test_chromium(home)
            self.assertEqual(path.read_bytes(), before)

    def test_hardlink_refused_without_changing_either_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, path = self.fixture(tmp)
            other = Path(tmp) / 'outside'
            os.link(path, other)
            with self.assertRaises(Refusal):
                desktop_layout.configure_test_chromium(home)
            self.assertEqual(other.read_bytes(), PINNED)

    def test_symlink_file_refused_without_changing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, path = self.fixture(tmp)
            other = Path(tmp) / 'outside'
            path.rename(other)
            path.symlink_to(other)
            with self.assertRaises(OSError):
                desktop_layout.configure_test_chromium(home)
            self.assertEqual(other.read_bytes(), PINNED)

    def test_linked_config_parent_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, path = self.fixture(tmp)
            other = Path(tmp) / 'outside-config'
            path.parent.rename(other)
            path.parent.symlink_to(other, target_is_directory=True)
            with self.assertRaises(Refusal):
                desktop_layout.configure_test_chromium(home)
            self.assertEqual((other / path.name).read_bytes(), PINNED)


if __name__ == '__main__':
    unittest.main()
