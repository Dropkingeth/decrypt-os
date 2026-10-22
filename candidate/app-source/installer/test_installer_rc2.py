"""Local fixtures only. No browser, sudo, disk, VM or provider operation."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

apps = load('desktop_apps', HERE / 'desktop_apps.py')
notice = load('install_notice', HERE / 'install_notice.py')
clean = load('clean_base', HERE / 'clean_base.py')
guided = load('guided_rc2', ROOT / 'guided/guided_setup.py')


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.notice-test-', dir=HERE)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.path = self.folder / 'ACKNOWLEDGEMENT.json'
        self.binding = notice.runtime_binding('a' * 64, 'b' * 64)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(notice.sys, 'stdin', Mock(isatty=lambda: True)))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def accept(self, answer='I AGREE'):
        with patch('builtins.input', return_value=answer):
            return notice.acknowledge(self.path, 'runtime-setup', self.binding, 'customer')

    def test_decline_writes_nothing(self):
        self.assertIsNone(self.accept(''))
        self.assertFalse(self.path.exists())

    def test_acceptance_records_exact_version_text_target_account(self):
        value = self.accept()
        self.assertEqual(value['version'], notice.VERSION)
        self.assertEqual(value['binding'], self.binding)
        self.assertEqual(value['account'], 'customer')
        self.assertEqual(value['textSha256'], notice.TEXT_SHA256)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(notice.verify(self.path, 'runtime-setup', self.binding, 'customer'), value)

    def test_retry_same_plan_retains_original_acknowledgement(self):
        first = self.accept()
        before = self.path.read_bytes()
        with patch('builtins.input', side_effect=AssertionError('Do not reprompt accepted exact plan')):
            self.assertEqual(notice.acknowledge(self.path, 'runtime-setup', self.binding, 'customer'), first)
        self.assertEqual(self.path.read_bytes(), before)

    def test_changed_plan_account_stage_or_notice_refused(self):
        original = self.accept()
        for field, changed in [('version', 'old'), ('textSha256', '0' * 64), ('text', 'no risk'),
                               ('accepted', False), ('account', 'other'), ('stage', 'base-install'),
                               ('binding', {'machineSha256': 'x'})]:
            value = copy.deepcopy(original); value[field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                notice.validate(value, 'runtime-setup', self.binding, 'customer')

    def test_symlink_receipt_never_followed(self):
        outside = self.folder / 'outside'; outside.write_text('{}')
        self.path.symlink_to(outside)
        with self.assertRaises(ValueError): self.accept()
        self.assertEqual(outside.read_text(), '{}')

    def test_noninteractive_cannot_accept(self):
        with patch.object(notice.sys, 'stdin', Mock(isatty=lambda: False)), self.assertRaises(ValueError):
            self.accept()
        self.assertFalse(self.path.exists())

    def test_base_and_runtime_notice_bytes_match(self):
        self.assertEqual((ROOT / 'base/install_notice.py').read_bytes(), (HERE / 'install_notice.py').read_bytes())

    def test_root_install_rejects_missing_ack_before_runtime_or_journal(self):
        plan = {'attempt': str(self.folder), 'account': {'uid': os.getuid(), 'user': 'customer'},
                'machine': 'a' * 64, 'recipeSha256': 'b' * 64, 'noticeHelperSha256': guided.hash_file(HERE / 'install_notice.py')}
        runtime = Mock(); installer = Mock()
        with patch.object(guided.os, 'geteuid', return_value=0), patch.dict(os.environ, {'SUDO_UID': str(os.getuid())}), \
             patch.object(guided, 'load_plan', return_value=(plan,runtime,installer,{},{})), \
             patch.object(guided, 'notice_module', return_value=notice), patch.object(guided, 'journal_directory') as journal:
            with self.assertRaises(OSError): guided.root_apply(self.folder / 'PLAN.json', 'e' * 64)
        runtime.apply.assert_not_called(); journal.assert_not_called()


    def guided_fixture(self):
        work = self.folder / 'work'; work.mkdir(mode=0o700)
        attempt = work / 'attempt'; attempt.mkdir(mode=0o700)
        expected = 'c' * 64
        plan = {'attempt':str(attempt), 'account':{'uid':os.getuid(),'user':'customer'},
                'machine':'a'*64, 'recipeSha256':'b'*64, 'guidedSha256':'d'*64}
        guided.state_write(work, status='ready_for_review', attempt=str(attempt), planSha256=expected)
        runtime = Mock(); installer = Mock()
        installer.inspect_host.return_value = {'eligibleForInstall':True,'failures':[]}
        self.stack.enter_context(patch.object(guided,'load_plan',return_value=(plan,runtime,installer,{},{})))
        self.stack.enter_context(patch.object(guided,'JOURNALS',work/'journals'))
        self.stack.enter_context(patch.object(guided.os,'geteuid',return_value=1000))
        self.stack.enter_context(patch.object(guided,'notice_module',return_value=notice))
        return work, expected, plan

    def test_declined_runtime_notice_never_requests_sudo(self):
        work, expected, _ = self.guided_fixture()
        with patch('builtins.input',return_value=''), patch.object(guided.subprocess,'run') as run:
            self.assertTrue(guided.install(work,expected)['cancelled'])
        run.assert_not_called()
        self.assertEqual(guided.current_state(work)['status'],'ready_for_review')

    def test_failed_auth_reuses_exact_notice_and_reprompts_install_only(self):
        work, expected, plan = self.guided_fixture()
        with patch('builtins.input',side_effect=['I AGREE','INSTALL']) as ask, \
             patch.object(guided.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)):
            self.assertTrue(guided.install(work,expected)['safeToReopenPrompt'])
            self.assertEqual(ask.call_count,2)
        receipt = Path(plan['attempt'])/'ACKNOWLEDGEMENT.json'; before = receipt.read_bytes()
        with patch('builtins.input',return_value='INSTALL') as ask, \
             patch.object(guided.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)):
            self.assertTrue(guided.install(work,expected)['safeToReopenPrompt'])
            self.assertEqual(ask.call_count,1)
        self.assertEqual(receipt.read_bytes(),before)

    def test_direct_app_apply_requires_acceptance_before_dependencies(self):
        machine = clean.digest(b'fixture')
        with patch.object(clean.os,'geteuid',return_value=0), patch.object(clean,'verify_bundle',return_value=({},'a'*64)), \
             patch.object(clean,'inspect_host',return_value={'eligibleForInstall':True}), \
             patch.object(clean.Path,'read_text',return_value='fixture'), patch.object(clean,'dependency_status') as dependency:
            with self.assertRaisesRegex(clean.RecipeError,'installation notice'):
                clean.apply(HERE,'customer','a'*64,machine,'b'*64)
        dependency.assert_not_called()


class AppTests(unittest.TestCase):
    def setUp(self):
        self.profile = Path('/home/customer/.local/share/decrypt-browser/decrypt')
        self.config = apps.resolve_app('decrypt')
        self.window = {'address':'0xabcdef', 'pid':1234, 'class':'decrypt-os', 'mapped':True, 'workspace':{'id':1}}

    def test_arm_uses_explicit_web_app_never_native_desktop(self):
        for machine in ('aarch64', 'x86_64'):
            choice = apps.platform_choice('Linux', machine)
            self.assertEqual(choice['label'], 'TradingView (web app)')
            self.assertFalse(choice['nativeDesktopInstalled'])
        for system, arch in [('Darwin','arm64'),('Darwin','x86_64'),('Linux','armv7l'),('Linux','i686')]:
            with self.assertRaises(ValueError): apps.platform_choice(system,arch)

    def test_distinct_profiles_and_no_password_or_sandbox_override(self):
        decrypt = apps.browser_command(self.config, self.profile)
        tradingview_profile = self.profile.parent / 'tradingview'
        tradingview = apps.browser_command(apps.resolve_app('tradingview'), tradingview_profile)
        self.assertIn('--user-data-dir=' + str(self.profile), decrypt)
        self.assertIn('--app=https://www.tradingview.com/chart/', tradingview)
        self.assertNotEqual(decrypt, tradingview)
        for flag in ['--no-sandbox','--password-store=basic','--remote-debugging-port','--disable-web-security']:
            self.assertNotIn(flag, ' '.join(decrypt+tradingview))

    def test_workspace_assignment_matches_the_persistent_layout(self):
        self.assertEqual(apps.APPS['decrypt']['workspace'], 1)
        self.assertEqual(apps.APPS['trading-pipeline']['workspace'], 2)
        self.assertEqual(apps.APPS['tradingview']['workspace'], 2)
        self.assertEqual(apps.APPS['tradovate-bridge']['workspace'], 3)
        self.assertEqual(apps.APPS['tradovate-bridge']['url'], 'http://127.0.0.1:8779/bridge')
        self.assertEqual(set(apps.APPS), {'decrypt', 'trading-pipeline', 'tradingview', 'tradovate-bridge'})
        # Login windows and Bridge Advanced belong to the bridge module's own Hyprland rules.
        rules = (ROOT / 'app-source/modules/tradovate-bridge/desktop/hyprland-bridge-rules.lua').read_text()
        for window, workspace in (('chrome-127.0.0.1__bridge-Default', '3 silent'), ('tvbridge-login-a', '3 silent'),
                                  ('chrome-127.0.0.1__advanced-Default', '5'), ('tvbridge-login-b', '6 silent'),
                                  ('tvbridge-login-e', '9 silent')):
            self.assertIn('{class = "%s"}, {workspace = "%s"' % (window, workspace), rules)

    def test_existing_ordinary_browser_is_not_a_dedicated_app(self):
        ordinary = {**self.window, 'class':'chromium'}
        with patch.object(apps,'query',return_value=[ordinary]), patch.object(apps,'owns_process',return_value=True):
            self.assertEqual(apps.windows('decrypt',self.profile,self.config),[])

    def test_same_class_other_profile_is_ignored(self):
        with patch.object(apps,'query',return_value=[self.window]), patch.object(apps,'owns_process',return_value=False):
            self.assertEqual(apps.windows('decrypt',self.profile,self.config),[])

    def test_repeated_launch_focuses_existing_no_new_process(self):
        with patch.object(apps,'windows',return_value=[self.window]), patch.object(apps,'place_window') as place, \
             patch.object(apps.subprocess,'Popen') as popen:
            self.assertTrue(apps.open_window('decrypt',self.profile,self.config)['reusedWindow'])
        popen.assert_not_called(); place.assert_called_once()

    def test_closed_browser_launch_waits_for_matching_window(self):
        with patch.object(apps,'windows',side_effect=[[],[],[self.window]]), patch.object(apps,'place_window') as place, \
             patch.object(apps.subprocess,'Popen',return_value=Mock(poll=lambda:None)) as popen, patch.object(apps.time,'sleep'):
            self.assertFalse(apps.open_window('decrypt',self.profile,self.config)['reusedWindow'])
        self.assertEqual(popen.call_count,1); place.assert_called_once()

    def test_postlaunch_placement_verifies_actual_window(self):
        with patch.object(apps,'run') as run, patch.object(apps,'windows',return_value=[self.window]), \
             patch.object(apps,'query',side_effect=[{'address':'0xabcdef'},{'id':1}]):
            apps.place_window('decrypt',self.profile,self.window,self.config)
        self.assertTrue(all('address:0xabcdef' in str(call) for call in run.call_args_list))

    def test_workspace_mismatch_fails_without_another_launch(self):
        wrong = {**self.window,'workspace':{'id':2}}
        with patch.object(apps,'run'), patch.object(apps,'windows',return_value=[wrong]), \
             patch.object(apps,'query',side_effect=[{'address':'0xabcdef'},{'id':1}]), self.assertRaises(ValueError):
            apps.place_window('decrypt',self.profile,self.window,self.config)

    def test_duplicate_or_malicious_window_refused(self):
        for clients in [[self.window,self.window],[{**self.window,'address':'0x1\"}) evil'}]]:
            with patch.object(apps,'query',return_value=clients), patch.object(apps,'owns_process',return_value=True), self.assertRaises(ValueError):
                apps.windows('decrypt',self.profile,self.config)

    def test_only_known_apps_resolve(self):
        self.assertEqual(apps.resolve_app('decrypt')['profile_name'], 'decrypt')
        with self.assertRaises(ValueError): apps.resolve_app('bridge-login')

    def test_readiness_waits_for_local_api_before_launch(self):
        with patch.object(apps,'service_ready',side_effect=[False,True]) as ready, patch.object(apps.time,'sleep'):
            apps.wait_for_service()
        self.assertEqual(ready.call_count,2)

    def test_readiness_failure_is_bounded_and_never_opens_browser(self):
        with patch.object(apps,'service_ready',return_value=False), patch.object(apps.time,'monotonic',side_effect=[0,0,16]), \
             patch.object(apps.time,'sleep'), patch.object(apps.subprocess,'Popen') as popen, self.assertRaisesRegex(ValueError,'not ready'):
            apps.wait_for_service()
        popen.assert_not_called()

    def test_visible_error_uses_declared_terminal_without_shell(self):
        with patch.object(apps.subprocess,'Popen') as popen, contextlib.redirect_stderr(io.StringIO()):
            apps.show_error('tradingview','Could not confirm this window.')
        args=popen.call_args.args[0]
        self.assertEqual(args[0],'/usr/bin/foot')
        self.assertIn('TradingView: Could not confirm this window.',args)
        self.assertNotIn('shell',popen.call_args.kwargs)
        self.assertIn('foot',clean.DEPENDENCIES)
        self.assertIn('openssl',clean.DEPENDENCIES)

    def test_generated_desktop_entries_are_accessible_and_labelled(self):
        files = clean.generated_files('customer')
        desktop = files['usr/share/applications/decrypt-tradingview.desktop'][0].decode()
        self.assertIn('Name=TradingView (web app)',desktop)
        self.assertIn('Exec=/usr/local/bin/decrypt-tradingview',desktop)
        self.assertIn('StartupWMClass=decrypt-tradingview-chart',desktop)
        self.assertIn('installer/desktop_apps.py', files['usr/local/bin/decrypt-os'][0].decode())
        self.assertNotIn('xdg-open', files['usr/local/bin/decrypt-os'][0].decode())
        for app, binary in [('trading-pipeline','decrypt-trading-pipeline'),
                             ('tradovate-bridge','decrypt-tradovate-bridge')]:
            launcher = files['usr/local/bin/' + binary][0].decode()
            self.assertIn(repr(app), launcher)
            self.assertIn('usr/share/applications/' + binary + '.desktop', files)
        # WS4: the ICT assistant's terminal launcher and desktop entry.
        self.assertIn('app/ict-assistant/launcher.py', files['usr/local/bin/decrypt-ict-assistant'][0].decode())
        self.assertIn('usr/share/applications/decrypt-ict-assistant.desktop', files)
        # WS5: Bridge Advanced comes from the bridge module's own launcher, not a generated one.
        self.assertNotIn('usr/local/bin/decrypt-bridge-advanced', files)
        module = ROOT / 'app-source/modules/tradovate-bridge'
        shipped = (module / 'runtime-files.txt').read_text().split()
        self.assertIn('bin/decrypt-bridge-advanced', shipped)
        self.assertIn('desktop/decrypt-bridge-advanced.desktop', shipped)
        self.assertIn('Exec=/usr/local/lib/decrypt-os/modules/tradovate-bridge/bin/decrypt-bridge-advanced',
                      (module / 'desktop/decrypt-bridge-advanced.desktop').read_text())

    def test_tmp_janitor_unit_has_single_execstart_and_hourly_timer(self):
        files = clean.generated_files('customer')
        service = files['etc/systemd/user/decrypt-tmp-janitor.service'][0].decode()
        timer = files['etc/systemd/user/decrypt-tmp-janitor.timer'][0].decode()
        self.assertEqual(service.count('ExecStart='), 1)
        self.assertIn('installer/tmp_janitor.py --apply', service)
        self.assertIn('OnUnitActiveSec=1h', timer)
        self.assertIn('WantedBy=timers.target', timer)

    def test_desktop_layout_restore_unit_has_single_execstart_and_fast_timer(self):
        files = clean.generated_files('customer')
        service = files['etc/systemd/user/decrypt-desktop-layout.service'][0].decode()
        timer = files['etc/systemd/user/decrypt-desktop-layout.timer'][0].decode()
        self.assertEqual(service.count('ExecStart='), 1)
        self.assertIn('installer/desktop_layout_restore.py', service)
        self.assertIn('OnUnitActiveSec=20s', timer)


if __name__ == '__main__': unittest.main(verbosity=2)
