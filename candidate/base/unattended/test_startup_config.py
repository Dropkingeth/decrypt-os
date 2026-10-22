"""startup_config() regression. No device, network, chroot or package operation."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase_b import startup_config


class StartupConfigTests(unittest.TestCase):
    def test_persistent_workspaces_and_default_focus(self):
        for software_rendering in (False, True):
            text = startup_config(software_rendering).decode()
            self.assertIn('{"1", "2", "3", "4", "5"}', text)
            self.assertIn('persistent = true', text)
            self.assertIn('hl.dsp.focus({workspace="1"})', text)
            self.assertIn('--session-start', text)
            # Hyprland 0.56 broke the old `dispatch workspace N` CLI form; only
            # the Lua hl.* dispatch API is ever written here.
            self.assertNotIn('dispatch workspace', text)
            self.assertEqual(text.startswith('hl.config({debug={vfr=false}})\n'), software_rendering)
            # VM guests get continuous frames (Chromium app windows stall under VFR there).
            self.assertIn('systemd-detect-virt --vm --quiet', text)
            self.assertIn('if vm:match("yes") then hl.config({debug={vfr=false}}) end', text)

    def test_session_start_runs_before_the_default_focus_switch(self):
        # Focusing WS1 before desktop_apps.py's --session-start environment
        # import would race the import against whichever workspace happens to
        # be active; keep the ordering fixed.
        text = startup_config(False).decode()
        self.assertLess(text.index('--session-start'), text.index('hl.dsp.focus'))

    def test_assistant_terminals_land_on_workspace_4_silently(self):
        text = startup_config(False).decode()
        for name in ('decrypt-os-ict-assistant', 'decrypt-os-assistant', 'decrypt-os-claude-signin',
                     'decrypt-os-storage-setup', 'decrypt-os-storage-unlock'):
            self.assertIn(
                'o.window({class = "%s"}, {workspace = "4 silent", tile = true, no_initial_focus = true})' % name,
                text)

    def test_bridge_window_rules_are_sourced_if_present(self):
        text = startup_config(False).decode()
        self.assertIn(
            'pcall(dofile, "/usr/local/lib/decrypt-os/modules/tradovate-bridge/desktop/hyprland-bridge-rules.lua")',
            text)
        # A bridge-less image must still produce a config Hyprland can load;
        # pcall is what keeps a missing file from being a startup error.
        self.assertIn('pcall(dofile,', text)

    def test_ict_assistant_autostarts_silently_on_workspace_4(self):
        text = startup_config(False).decode()
        self.assertIn('/usr/local/bin/decrypt-ict-assistant', text)
        self.assertIn('--app-id=decrypt-os-ict-assistant', text)
        self.assertIn('{workspace="4 silent", no_initial_focus=true}', text)
        # It must be reachable from hyprland.start, not left dangling outside it.
        start = text.index('hl.on("hyprland.start"')
        end = text.index('end)', start)
        self.assertIn('decrypt-ict-assistant', text[start:end])


if __name__ == '__main__':
    unittest.main(verbosity=2)


class QuietFirstRunCase(unittest.TestCase):
    """quiet_first_run() edits only the three named first-run steps of the unpacked Omarchy tree."""

    def tree(self, files):
        import tempfile
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(root))
        first_run = root / 'install/user/first-run'
        first_run.mkdir(parents=True)
        for name, text in files.items():
            (first_run / name).write_bytes(text)
        return root

    def test_replaces_the_toast_and_unit_steps_only(self):
        from phase_b import quiet_first_run
        root = self.tree({'welcome.sh': b'omarchy-notification-send -u critical "Learn Keybindings"\n',
                          'wifi.sh': b'notify_update() {\n  omarchy-notification-send "Update System"\n}\n',
                          'enable-user-units.sh': b'#!/bin/bash\nsystemctl --user enable --now bt-agent.service\n',
                          'gnome-theme.sh': b'gsettings set org.gnome.desktop.interface color-scheme "prefer-dark"\n'})
        quiet_first_run(root)
        first_run = root / 'install/user/first-run'
        for name in ('welcome.sh', 'wifi.sh', 'enable-user-units.sh'):
            text = (first_run / name).read_bytes()
            self.assertNotIn(b'omarchy-notification-send', text, name)
            self.assertNotIn(b'systemctl --user enable', text, name)
            self.assertIn(b'Decrypt OS', text, name)
        self.assertTrue((first_run / 'enable-user-units.sh').read_bytes().endswith(b'exit 0\n'))
        self.assertEqual((first_run / 'gnome-theme.sh').read_bytes(),
                         b'gsettings set org.gnome.desktop.interface color-scheme "prefer-dark"\n')

    def test_refuses_an_unexpected_pin(self):
        from common import Refused
        from phase_b import quiet_first_run
        root = self.tree({'welcome.sh': b'echo hello\n', 'wifi.sh': b'omarchy-notification-send x\n',
                          'enable-user-units.sh': b'systemctl --user enable x\n'})
        with self.assertRaises(Refused):
            quiet_first_run(root)

    def test_no_automatic_update_check(self):
        from common import Refused
        from phase_b import no_automatic_omarchy_update
        import tempfile
        target = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(target))
        for directory in ('usr/lib/systemd/system', 'usr/lib/systemd/user', 'etc/systemd/system', 'etc/systemd/user',
                          'usr/share/libalpm/hooks', 'etc/pacman.d/hooks'):
            (target / directory).mkdir(parents=True)
        no_automatic_omarchy_update(target)
        (target / 'usr/lib/systemd/user/omarchy-migrate-notify.service').write_bytes(b'[Unit]\n')
        with self.assertRaises(Refused):
            no_automatic_omarchy_update(target)


class BarIndicatorsCase(unittest.TestCase):
    def test_indicators_widget_gets_an_explicit_list_without_passwordless_sudo(self):
        import json
        import tempfile
        from phase_b import BAR_INDICATORS, drop_passwordless_sudo_indicator
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(root))
        path = root / 'shell.json'
        path.write_text(json.dumps({'version': 1, 'bar': {'layout': {'left': [{'id': 'omarchy.menu'}],
                                    'center': [{'id': 'omarchy.indicators'}, {'id': 'omarchy.clock', 'format': 'HH:mm'}]}}}))
        drop_passwordless_sudo_indicator(path)
        config = json.loads(path.read_text())
        self.assertEqual(config['bar']['layout']['center'][0], {'id': 'omarchy.indicators', 'items': BAR_INDICATORS})
        self.assertEqual(config['bar']['layout']['center'][1], {'id': 'omarchy.clock', 'format': 'HH:mm'})
        self.assertNotIn('PasswordlessSudo', BAR_INDICATORS)

    def test_idle_screensaver_is_turned_off_and_lock_kept(self):
        import json
        import tempfile
        from common import Refused
        from phase_b import turn_off_idle_screensaver
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(root))
        path = root / 'shell.json'
        path.write_text(json.dumps({'version': 1, 'idle': {'screensaver': 150, 'lock': 300}, 'bar': {}}))
        turn_off_idle_screensaver(path)
        config = json.loads(path.read_text())
        self.assertEqual(config['idle'], {'screensaver': 0, 'lock': 300})
        self.assertEqual((config['version'], config['bar']), (1, {}))
        path.write_text(json.dumps({'version': 1, 'bar': {}}))
        with self.assertRaises(Refused):
            turn_off_idle_screensaver(path)
