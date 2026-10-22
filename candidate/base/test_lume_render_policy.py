"""Scope checks for the software-rendered customer startup and the new-user link entry point."""
from pathlib import Path
import unittest
from unittest.mock import patch

import desktop_layout
from guard import Refusal


class LumeRenderPolicy(unittest.TestCase):
    def test_factory_startup_carries_only_software_profile_workaround(self):
        import importlib.util
        import sys
        directory = Path(__file__).parent / 'unattended'
        sys.path.insert(0, str(directory))
        try:
            spec = importlib.util.spec_from_file_location('factory_phase_b_render', directory / 'phase_b.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            ordinary = module.startup_config(False)
            software = module.startup_config(True)
            self.assertEqual(software, b'hl.config({debug={vfr=false}})\n' + ordinary)
            # The software-rendering prefix only ever adds that one unconditional vfr setting;
            # everything after it (the VM-guest frame block, workspace persistence, default
            # focus, the session-start launch) is identical either way.
            self.assertTrue(ordinary.startswith(b'do\n  local virt = io.popen("/usr/bin/systemd-detect-virt --vm --quiet'))
            self.assertIn(b'for _, id in ipairs(', ordinary)
            self.assertIn(b'--session-start', software)
            self.assertEqual(ordinary.count(b'hl.config('), 1)      # inside the VM-guest block only
            self.assertEqual(software.count(b'hl.config('), 2)
        finally:
            sys.path.remove(str(directory))

    def test_no_startup_file_writer_remains(self):
        # The user's Hyprland startup comes from /etc/skel (phase_b); a per-user
        # ~/.config/default/hypr/autostart.lua would shadow Omarchy's own startup.
        source = Path(desktop_layout.__file__).read_text()
        self.assertNotIn('default/hypr', source)
        self.assertNotIn('autostart.lua', source)
        self.assertFalse(hasattr(desktop_layout, 'install_test_startup'))

    def test_wrong_guest_stops_entrypoint_before_any_layout_write(self):
        with patch('sys.argv', ['desktop_layout.py', '--apply-new-user-links']), \
                patch.object(desktop_layout, 'check_installed', side_effect=Refusal('Wrong guest')), \
                patch.object(desktop_layout, 'link_source') as link, \
                patch.object(desktop_layout, 'install_terminal_preference') as terminal, \
                self.assertRaises(Refusal):
            desktop_layout.main()
        link.assert_not_called()
        terminal.assert_not_called()


if __name__ == '__main__':
    unittest.main()
