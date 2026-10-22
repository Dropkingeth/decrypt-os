import contextlib
from pathlib import Path
import unittest
from unittest.mock import patch

import desktop_apps as apps
import desktop_layout_restore as restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.profile = Path('/home/customer/.local/share/decrypt-browser/decrypt')
        self.config = apps.resolve_app('decrypt')
        self.window = {'address': '0xabc', 'workspace': {'id': 1}}

    @contextlib.contextmanager
    def locked(self, _profile):
        yield

    def test_never_touches_manual_only_apps(self):
        self.assertNotIn('tradingview', restore.RESTORABLE)
        self.assertNotIn('bridge-login', restore.RESTORABLE)
        self.assertNotIn('bridge-advanced', restore.RESTORABLE)

    def test_present_on_correct_workspace_does_nothing(self):
        with patch.object(apps, 'windows', return_value=[self.window]), \
             patch.object(apps, 'profile_path', return_value=self.profile), \
             patch.object(apps, 'launch_lock', self.locked), \
             patch.object(restore, 'reposition_silent') as reposition, \
             patch.object(restore, 'launch_silent') as launch:
            result = restore.restore_one('decrypt', '/home/customer', 1000)
        self.assertEqual(result['action'], 'none')
        reposition.assert_not_called(); launch.assert_not_called()

    def test_present_on_wrong_workspace_is_repositioned_without_focus(self):
        wrong = {**self.window, 'workspace': {'id': 2}}
        with patch.object(apps, 'windows', return_value=[wrong]), \
             patch.object(apps, 'profile_path', return_value=self.profile), \
             patch.object(apps, 'launch_lock', self.locked), \
             patch.object(restore, 'reposition_silent') as reposition, \
             patch.object(restore, 'launch_silent') as launch, \
             patch.object(apps, 'run') as run:
            result = restore.restore_one('decrypt', '/home/customer', 1000)
        self.assertEqual(result['action'], 'repositioned')
        reposition.assert_called_once_with(self.config, wrong)
        launch.assert_not_called(); run.assert_not_called()

    def test_reposition_never_calls_focus_and_passes_follow_false(self):
        with patch.object(apps, 'run') as run:
            restore.reposition_silent(self.config, self.window)
        self.assertEqual(run.call_count, 1)
        dispatched = run.call_args.args[0][-1]
        self.assertIn('follow=false', dispatched)
        self.assertNotIn('hl.dsp.focus', dispatched)

    def test_missing_window_launches_silently_after_backend_ready(self):
        with patch.object(apps, 'windows', return_value=[]), \
             patch.object(apps, 'profile_path', return_value=self.profile), \
             patch.object(apps, 'launch_lock', self.locked), \
             patch.object(apps, 'ensure_backend_ready') as ready, \
             patch.object(restore, 'launch_silent') as launch:
            result = restore.restore_one('decrypt', '/home/customer', 1000)
        self.assertEqual(result['action'], 'launched')
        ready.assert_called_once_with('decrypt')
        launch.assert_called_once_with(self.config, self.profile)

    def test_launch_silent_uses_silent_workspace_and_never_focuses(self):
        with patch.object(apps, 'run') as run, patch.object(apps, 'browser_command', return_value=['/usr/bin/chromium', '--app=x']):
            restore.launch_silent(self.config, self.profile)
        dispatched = run.call_args.args[0][-1]
        self.assertIn('1 silent', dispatched)
        self.assertIn('no_initial_focus=true', dispatched)
        self.assertIn('hl.dsp.exec_cmd(', dispatched)
        self.assertNotIn('hl.dsp.focus', dispatched)

    def test_a_busy_lock_or_untrusted_window_list_is_skipped_not_raised(self):
        with patch.object(apps, 'profile_path', return_value=self.profile), \
             patch.object(apps, 'launch_lock', side_effect=ValueError('busy')):
            result = restore.restore_one('decrypt', '/home/customer', 1000)
        self.assertEqual(result['action'], 'skipped')

    def test_main_restores_the_ict_assistant_terminal_too(self):
        with patch.object(restore, 'restore_one') as one, \
             patch.object(restore, 'restore_ict_assistant') as ict, \
             patch('os.getuid', return_value=1000), patch('os.geteuid', return_value=1000), \
             patch('pwd.getpwuid') as getpwuid:
            getpwuid.return_value.pw_dir = '/home/customer'
            self.assertEqual(restore.main(), 0)
        self.assertEqual(one.call_count, len(restore.RESTORABLE))
        ict.assert_called_once_with()


class IctAssistantRestoreTests(unittest.TestCase):
    def setUp(self):
        self.window = {'address': '0xdef', 'class': restore.ICT_ASSISTANT_CLASS,
                        'mapped': True, 'workspace': {'id': 4}}

    def test_present_on_workspace_4_does_nothing(self):
        with patch.object(apps, 'query', return_value=[self.window]), patch.object(apps, 'run') as run:
            result = restore.restore_ict_assistant()
        self.assertEqual(result, {'app': 'ict-assistant', 'action': 'none'})
        run.assert_not_called()

    def test_present_on_wrong_workspace_is_repositioned_without_focus(self):
        wrong = {**self.window, 'workspace': {'id': 1}}
        with patch.object(apps, 'query', return_value=[wrong]), patch.object(apps, 'run') as run:
            result = restore.restore_ict_assistant()
        self.assertEqual(result, {'app': 'ict-assistant', 'action': 'repositioned'})
        dispatched = run.call_args.args[0][-1]
        self.assertIn('follow=false', dispatched)
        self.assertNotIn('hl.dsp.focus', dispatched)

    def test_missing_window_is_launched_silently_on_workspace_4(self):
        with patch.object(apps, 'query', return_value=[]), patch.object(apps, 'run') as run:
            result = restore.restore_ict_assistant()
        self.assertEqual(result, {'app': 'ict-assistant', 'action': 'launched'})
        dispatched = run.call_args.args[0][-1]
        self.assertIn('hl.dsp.exec_cmd(', dispatched)
        self.assertIn('4 silent', dispatched)
        self.assertIn('no_initial_focus=true', dispatched)
        self.assertIn('/usr/local/bin/decrypt-ict-assistant', dispatched)
        self.assertNotIn('hl.dsp.focus', dispatched)

    def test_launch_command_is_a_valid_lua_string(self):
        # The ICT command carries --title="ICT Assistant"; pasted raw into a
        # double-quoted Lua string it ended the string early and Hyprland
        # rejected the whole dispatch.
        with patch.object(apps, 'query', return_value=[]), patch.object(apps, 'run') as run:
            restore.restore_ict_assistant()
        dispatched = run.call_args.args[0][-1]
        self.assertIn('--title=\\"ICT Assistant\\"', dispatched)
        self.assertTrue(dispatched.startswith('hl.dsp.exec_cmd("/usr/bin/foot '))
        with self.assertRaises(ValueError):
            restore.lua_string('bad\ncommand')

    def test_ambiguous_window_list_is_skipped_not_raised(self):
        with patch.object(apps, 'query', return_value=[self.window, dict(self.window)]), \
             patch.object(apps, 'run') as run:
            result = restore.restore_ict_assistant()
        self.assertEqual(result, {'app': 'ict-assistant', 'action': 'skipped'})
        run.assert_not_called()

    def test_untrusted_query_is_skipped_not_raised(self):
        with patch.object(apps, 'query', side_effect=ValueError('bad desktop response')):
            result = restore.restore_ict_assistant()
        self.assertEqual(result, {'app': 'ict-assistant', 'action': 'skipped'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
