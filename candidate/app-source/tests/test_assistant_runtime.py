"""Claude-only customer runtime: config, auth status parsing and fixed actions.

Pure unit tests. No OpenClaw process, network, sudo or terminal is started.
"""
import importlib.util
import json
import types
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
APP = SOURCE / 'app'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load('customer_bootstrap', APP / 'harness/bootstrap.py')
sys.path.insert(0, str(APP / 'backend'))
bridge = load('harness_bridge_under_test', APP / 'backend/harness_bridge.py')


def auth(profile_type='token', status='static', expiry=None, provider='anthropic',
         profile_id='anthropic:customer', order=None):
    profile = {'profileId': profile_id, 'type': profile_type, 'status': status}
    if expiry is not None:
        profile['expiry'] = {'at': expiry}
    row = {'provider': provider, 'status': status, 'profiles': [profile]}
    if order is not None:
        row['profileOrder'] = order
    return {'providers': [row]}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = bootstrap.config()

    def test_claude_only_models(self):
        defaults = self.config['agents']['defaults']
        self.assertEqual(defaults['model'], {'primary': 'anthropic/claude-opus-5-5',
                                             'fallbacks': ['anthropic/claude-sonnet-5']})
        self.assertEqual(set(defaults['models']), {'anthropic/claude-opus-5-5', 'anthropic/claude-sonnet-5',
                                                   'anthropic/claude-haiku-4-5'})
        self.assertEqual(self.config['plugins'], {'allow': ['anthropic']})

    def test_heartbeat_isolated_and_light(self):
        heartbeat = self.config['agents']['defaults']['heartbeat']
        self.assertIs(heartbeat['isolatedSession'], True)
        self.assertEqual(heartbeat['every'], '0m')
        self.assertEqual(heartbeat['model'], 'anthropic/claude-haiku-4-5')
        self.assertEqual(self.config['agents']['defaults']['compaction']['model'], 'anthropic/claude-haiku-4-5')
        self.assertEqual(self.config['agents']['defaults']['utilityModel'], 'anthropic/claude-haiku-4-5')

    def test_no_other_provider_anywhere(self):
        text = json.dumps(self.config).lower() + (APP / 'harness/bootstrap.py').read_text().lower()
        for word in ('openai', 'codex', 'chatgpt', 'gpt-'):
            self.assertNotIn(word, text)

    def test_main_agent_has_no_tools_and_dangerous_groups_denied(self):
        main = self.config['agents']['entries']['main']
        self.assertEqual(main['tools'], {'deny': ['*']})
        self.assertNotIn('default', main)  # retired marker; conflicts with explicit ownership
        self.assertEqual(self.config['agents']['ownership'], 'explicit')
        self.assertEqual(main['skills'], ['decrypt-unslop', 'decrypt-frontend-design', 'review-skill-imports'])
        tools = self.config['tools']
        self.assertEqual(tools['profile'], 'minimal')
        for group in ('group:runtime', 'group:fs', 'group:ui', 'group:automation', 'group:nodes', 'group:messaging'):
            self.assertIn(group, tools['deny'])
        self.assertFalse(tools['elevated']['enabled'])

    def test_gateway_stays_loopback_without_http_model_endpoints(self):
        gateway = self.config['gateway']
        self.assertEqual(gateway['bind'], 'loopback')
        self.assertFalse(gateway['controlUi']['enabled'])
        self.assertFalse(gateway['http']['endpoints']['chatCompletions']['enabled'])


class AuthStatusTests(unittest.TestCase):
    def test_setup_token_and_api_key_without_expiry_are_usable(self):
        self.assertEqual(bootstrap.usable_auth(auth('token')), (True, None))
        self.assertEqual(bootstrap.usable_auth(auth('api_key')), (True, None))

    def test_expiry_must_be_in_future(self):
        self.assertEqual(bootstrap.usable_auth(auth('token', 'ok', 2000), now_ms=1000), (True, 2000))
        self.assertEqual(bootstrap.usable_auth(auth('token', 'ok', 500), now_ms=1000), (False, None))
        self.assertEqual(bootstrap.usable_auth(auth('oauth', 'ok'), now_ms=1000), (False, None))

    def test_other_provider_profile_or_state_refused(self):
        for value in (auth(provider='openai'), auth(profile_id='anthropic:manual'),
                      auth(status='expired'), auth(status='missing'), auth(order=['anthropic:other']),
                      {'unavailable': True}, None, {'providers': 'x'}):
            self.assertEqual(bootstrap.usable_auth(value)[0], False)


class InteractiveTests(unittest.TestCase):
    def test_login_offers_setup_token_or_api_key_only(self):
        answers = iter(['3', '2'])
        with mock.patch('builtins.print'):
            self.assertEqual(bootstrap.choose_auth_method(lambda prompt: next(answers)), 'api-key')
        answers = iter(['x', 'y', 'z'])
        with mock.patch('builtins.print'), self.assertRaises(RuntimeError):
            bootstrap.choose_auth_method(lambda prompt: next(answers))

    def test_fixed_openclaw_arguments(self):
        args = bootstrap.interactive_args('login', 'setup-token')
        self.assertEqual(args, ['models', 'auth', 'login', '--agent', 'main', '--provider', 'anthropic',
                                '--method', 'setup-token', '--profile-id', 'anthropic:customer', '--force'])
        self.assertEqual(bootstrap.interactive_args('console'), ['tui', '--session', 'agent:main:main'])
        self.assertEqual(bootstrap.interactive_args('ict'), ['tui', '--session', 'agent:ict:main'])
        with self.assertRaises(RuntimeError):
            bootstrap.interactive_args('login', 'device-code')


class ClaudeCodeHelperTests(unittest.TestCase):
    """Claude Code is proprietary and never bundled; this is a strictly
    optional, user-initiated step for making a setup-token. Pure unit tests:
    subprocess.run is always mocked, so no real npm install or network call
    happens here."""

    def test_declining_installs_and_runs_nothing(self):
        with mock.patch('builtins.print'), \
             mock.patch.object(bootstrap, 'install_claude_code_helper') as install, \
             mock.patch.object(bootstrap, 'run_claude_setup_token') as setup:
            bootstrap.choose_claude_code_helper(lambda prompt: 'n')
        install.assert_not_called()
        setup.assert_not_called()

    def test_accepting_installs_then_runs_setup_token_in_order(self):
        with mock.patch('builtins.print'), \
             mock.patch.object(bootstrap, 'install_claude_code_helper') as install, \
             mock.patch.object(bootstrap, 'run_claude_setup_token') as setup:
            bootstrap.choose_claude_code_helper(lambda prompt: 'Y')
        install.assert_called_once_with()
        setup.assert_called_once_with()

    def fake_install(self, root):
        """What a successful npm ci leaves: the pinned package with its binary."""
        package = root / bootstrap.CLAUDE_CODE_DIR_NAME / 'node_modules' / bootstrap.CLAUDE_CODE_PACKAGE
        package.mkdir(parents=True, exist_ok=True)
        (package / 'package.json').write_text(json.dumps(
            {'name': bootstrap.CLAUDE_CODE_PACKAGE, 'version': bootstrap.CLAUDE_CODE_VERSION}))
        (package / 'claude').write_text('#!/bin/sh\n')
        (package / 'claude').chmod(0o755)
        return package / 'claude'

    def test_pin_is_exact_with_registry_integrity(self):
        import re
        package, lock = bootstrap.claude_code_manifest()
        self.assertEqual(package['dependencies'], {bootstrap.CLAUDE_CODE_PACKAGE: bootstrap.CLAUDE_CODE_VERSION})
        self.assertRegex(bootstrap.CLAUDE_CODE_VERSION, r'^\d+\.\d+\.\d+$')
        row = lock['packages']['node_modules/' + bootstrap.CLAUDE_CODE_PACKAGE]
        self.assertEqual(row['version'], bootstrap.CLAUDE_CODE_VERSION)
        self.assertRegex(row['integrity'], r'^sha512-[A-Za-z0-9+/]{86}==$')
        self.assertEqual(row['resolved'], 'https://registry.npmjs.org/' + bootstrap.CLAUDE_CODE_PACKAGE
                         + '/-/' + bootstrap.CLAUDE_CODE_PACKAGE.split('/')[1] + '-' + bootstrap.CLAUDE_CODE_VERSION + '.tgz')
        self.assertEqual((row['os'], row['cpu']), (['linux'], ['arm64']))
        self.assertEqual(set(lock['packages']), {'', 'node_modules/' + bootstrap.CLAUDE_CODE_PACKAGE})
        self.assertIsNone(re.search(r'[\^~*]', json.dumps(package)))

    def test_install_argv_env_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tmp/claude-helper-npm-cache').mkdir(parents=True)
            calls = []
            def fake_run(argv, **kw):
                calls.append((argv, kw)); self.fake_install(root)
                return mock.Mock(returncode=0)
            with mock.patch.object(bootstrap, 'ROOT', root), mock.patch.object(bootstrap.subprocess, 'run', fake_run):
                bootstrap.install_claude_code_helper()
            argv, kw = calls[0]
            prefix = root / bootstrap.CLAUDE_CODE_DIR_NAME
            self.assertEqual(argv[:3], [bootstrap.NODE, bootstrap.NPM_CLI, 'ci'])
            for flag in ('--ignore-scripts', '--omit=dev', '--strict-ssl=true', '--registry=https://registry.npmjs.org/',
                         '--userconfig=' + str(prefix / 'empty-user.npmrc'),
                         '--globalconfig=' + str(prefix / 'empty-global.npmrc')):
                self.assertIn(flag, argv)
            self.assertFalse(any(a in ('install', 'i', '--prefix', '-g', '--global') for a in argv))
            self.assertNotIn(bootstrap.CLAUDE_CODE_PACKAGE, argv, 'only the lockfile names the package')
            self.assertEqual(kw['cwd'], prefix)
            env = kw['env']
            self.assertFalse([k for k in env if 'proxy' in k.lower()], 'npm must not use the ICT web reader proxy')
            self.assertEqual(env['HOME'], str(root / 'home'))
            self.assertEqual(json.loads((prefix / 'package-lock.json').read_text()), bootstrap.claude_code_manifest()[1])
            self.assertEqual(json.loads((prefix / 'package.json').read_text()), bootstrap.claude_code_manifest()[0])
            self.assertEqual((prefix / 'empty-user.npmrc').read_text(), '')
            self.assertEqual(prefix.stat().st_mode & 0o777, 0o700)
            self.assertFalse((root / 'tmp/claude-helper-npm-cache').exists())

    def test_runtime_environment_still_proxies_the_ict_reader(self):
        self.assertIn('HTTPS_PROXY', bootstrap.environment())
        self.assertNotIn('HTTPS_PROXY', bootstrap.helper_environment())
        self.assertEqual(bootstrap.helper_environment()['DISABLE_AUTOUPDATER'], '1')

    def test_install_failure_raises_and_mentions_the_api_key_fallback(self):
        for code in (1, 0):  # npm failed, or npm "succeeded" without the pinned binary
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with mock.patch.object(bootstrap, 'ROOT', root), \
                     mock.patch.object(bootstrap.subprocess, 'run', return_value=mock.Mock(returncode=code)):
                    with self.assertRaisesRegex(RuntimeError, 'API key'):
                        bootstrap.install_claude_code_helper()

    def test_install_refuses_a_linked_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'elsewhere').mkdir()
            (root / bootstrap.CLAUDE_CODE_DIR_NAME).symlink_to(root / 'elsewhere')
            with mock.patch.object(bootstrap, 'ROOT', root), \
                 mock.patch.object(bootstrap.subprocess, 'run') as run:
                with self.assertRaises(RuntimeError):
                    bootstrap.install_claude_code_helper()
            run.assert_not_called()

    def test_setup_token_requires_the_binary_to_already_be_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(bootstrap, 'ROOT', root):
                with self.assertRaises(RuntimeError):
                    bootstrap.run_claude_setup_token()

    def test_setup_token_refuses_a_linked_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = self.fake_install(root)
            binary.unlink(); binary.symlink_to('/bin/sh')
            with mock.patch.object(bootstrap, 'ROOT', root), \
                 mock.patch.object(bootstrap.subprocess, 'run') as run:
                with self.assertRaises(RuntimeError):
                    bootstrap.run_claude_setup_token()
            run.assert_not_called()

    def test_setup_token_runs_the_installed_binary_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = self.fake_install(root)
            with mock.patch.object(bootstrap, 'ROOT', root), \
                 mock.patch.object(bootstrap.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
                bootstrap.run_claude_setup_token()
            self.assertEqual(run.call_args.args[0], [str(binary), 'setup-token'])
            env = run.call_args.kwargs['env']
            self.assertFalse([k for k in env if 'proxy' in k.lower()])
            self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(root / 'claude'))

    def test_helper_is_gated_on_the_setup_token_method(self):
        # choose_claude_code_helper() must only ever be offered for setup-token
        # logins, never for the API key path, inside interactive().
        source = (APP / 'harness/bootstrap.py').read_text()
        start = source.index('def interactive(')
        body = source[start:start + 2000]
        self.assertIn('method == "setup-token"', body)
        self.assertIn('choose_claude_code_helper()', body)


class RuntimeUnitHardeningTests(unittest.TestCase):
    runtime = load('customer_runtime_hardening', APP / 'harness/runtime.py')

    def test_sandbox_unit_restricts_namespaces_and_syscalls(self):
        for persistent in (False, True):
            props = self.runtime.unit_properties(persistent)
            for item in ('RestrictNamespaces=yes', 'SystemCallArchitectures=native',
                         'SystemCallFilter=@system-service', 'SystemCallErrorNumber=EPERM',
                         'NoNewPrivileges=yes', 'ProtectProc=invisible', 'CapabilityBoundingSet='):
                self.assertIn(item, props, (persistent, item))

    def test_nsenter_requires_the_initial_user_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = (Path(tmp) / n for n in 'abc')
            a.write_text(''); c.write_text('')
            b.symlink_to(a)
            self.assertTrue(self.runtime.same_namespace(a, b))
            self.assertFalse(self.runtime.same_namespace(a, c))
        source = (APP / 'harness/runtime.py').read_text()
        body = source[source.index('def verified_namespace_command('):source.index('def namespace_command(')]
        self.assertLess(body.index('same_namespace(proc / "ns/user", pathlib.Path("/proc/1/ns/user"))'),
                        body.index('"/usr/bin/nsenter"'))


class BridgeTests(unittest.TestCase):
    def test_actions_models_and_workspace(self):
        self.assertIn('ict', bridge.ACTIONS)
        self.assertEqual(bridge.MODELS, frozenset({'anthropic/claude-opus-5-5', 'anthropic/claude-sonnet-5'}))
        self.assertEqual(bridge.WORKSPACE, 4)
        self.assertEqual(bridge.APP_IDS['ict'], 'decrypt-os-ict-assistant')
        self.assertEqual(set(bridge.TITLES), set(bridge.APP_IDS))
        self.assertEqual(bridge._base()['provider'], 'Claude')

    def test_sanitize_rejects_unknown_model_and_status(self):
        value = bridge._sanitize({'status': 'connected', 'model': 'anthropic/claude-opus-5-5'})
        self.assertEqual((value['status'], value['model']), ('connected', 'anthropic/claude-opus-5-5'))
        value = bridge._sanitize({'status': 'pwned', 'model': 'other/model'})
        self.assertEqual((value['status'], value['model']), ('unavailable', None))

    def test_http_body_must_be_empty(self):
        with self.assertRaises(bridge.HarnessError):
            bridge.HarnessBridge().action('login', {'token': 'x'})
        with self.assertRaises(bridge.HarnessError):
            bridge.HarnessBridge().action('shell', {})

    def installed(self, unit_state='inactive', unit_start='', published=None):
        import tempfile
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        control = Path(tmp.name) / 'customer_control.py'; control.write_text('# fixed helper\n')
        status_file = Path(tmp.name) / 'harness-status.json'
        if published is not None:
            status_file.write_text(json.dumps({'schema': bridge.STATUS_SCHEMA, **published}))
        calls = []

        def fake_run(command, timeout):
            calls.append(command)
            if command[:2] == [bridge.SYSTEMCTL, 'show']:
                return types.SimpleNamespace(returncode=0, stdout='LoadState=loaded\nActiveState=%s\nActiveEnterTimestampMonotonic=%s\n' % (unit_state, unit_start))
            return types.SimpleNamespace(returncode=0, stdout='')
        for name, value in (('INSTALLED_CONTROL', control), ('STATUS_FILE', status_file)):
            patcher = mock.patch.object(bridge, name, value); patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(bridge.HarnessBridge, '_run', staticmethod(fake_run)); patcher.start(); self.addCleanup(patcher.stop)
        return calls

    def test_status_does_not_take_the_launch_lock(self):
        self.installed('inactive')
        harness = bridge.HarnessBridge()
        self.assertTrue(harness.lock.acquire(blocking=False))  # a launch in progress
        try:
            value = harness.action('status', {})
            self.assertEqual((value['status'], value['error']), ('not_started', None))
            self.assertEqual(harness.action('start', {})['error'], bridge.ERROR_TEXT['runtime_busy'])
        finally:
            harness.lock.release()

    def test_installed_status_never_runs_sudo_and_reads_the_unit(self):
        calls = self.installed('inactive')
        value = bridge.HarnessBridge().action('status', {})
        self.assertEqual((value['status'], value['passwordInTerminal'], value['error']), ('not_started', True, None))
        self.assertFalse(any('sudo' in str(c) for c in calls), calls)
        self.assertFalse(any(bridge.SYSTEMD_RUN in c for c in calls), calls)

    def test_installed_status_uses_the_published_file_for_the_current_unit_start(self):
        self.installed('active', '123', {'unitStart': '123', 'status': 'needs_login', 'model': None,
                                         'loginRunning': False, 'consoleRunning': False, 'scopeUpdatedAt': None,
                                         'credentialPersistence': 'encrypted',
                                         'storage': {'configured': True, 'unlocked': True, 'error': False}})
        value = bridge.HarnessBridge().action('status', {})
        self.assertEqual(value['status'], 'needs_login')
        self.assertEqual((value['storage']['configured'], value['storage']['unlocked']), (True, True))
        self.assertEqual(value['credentialPersistence'], 'encrypted')

    def test_installed_status_is_unavailable_when_the_file_is_from_an_older_start(self):
        self.installed('active', '456', {'unitStart': '123', 'status': 'ready', 'storage': {}})
        value = bridge.HarnessBridge().action('status', {})
        self.assertEqual(value['status'], 'unavailable')
        self.assertEqual(value['error'], bridge.ERROR_TEXT['status_after_terminal'])

    def test_installed_commands_use_interactive_sudo_and_the_connect_terminal(self):
        self.installed()
        self.assertEqual(bridge.HarnessBridge._command('login')[:5], ['/usr/bin/sudo', bridge.PYTHON, '-B', '-I', str(bridge.INSTALLED_CONTROL)])
        self.assertNotIn('-n', bridge.HarnessBridge._command('login'))
        for verb in ('start', 'login'):
            self.assertEqual(bridge.HarnessBridge._terminal_command(verb)[-2:], [str(bridge.LAUNCHER), '--connect'])
        self.assertEqual(bridge.HarnessBridge._terminal_command('verify')[-1], '--verify')
        self.assertEqual(bridge.HarnessBridge._terminal_command('stop')[-1], '--stop')
        self.assertEqual(bridge.HarnessBridge._terminal_command('ict')[-1], 'ict')

    def test_installed_start_and_stop_open_terminals_instead_of_running_sudo(self):
        self.installed('inactive')
        opened = []
        with mock.patch.object(bridge.HarnessBridge, '_open_terminal', lambda self, verb: opened.append(verb) or {}):
            bridge.HarnessBridge().action('start', {})
            bridge.HarnessBridge().action('login', {})
        self.assertEqual(opened, ['start', 'login'])
        value = bridge.HarnessBridge().action('stop', {})      # nothing to stop while the unit is off
        self.assertEqual(value['error'], bridge.ERROR_TEXT['stop_not_available'])

    def test_clean_base_accepts_the_shipped_bridge_module(self):
        # The build renders harness_bridge.py through clean_base.adapt_bridge; it must accept the real file.
        clean_base = load('clean_base_under_test', SOURCE / 'installer/clean_base.py')
        data = (APP / 'backend/harness_bridge.py').read_bytes()
        self.assertEqual(clean_base.adapt_bridge(data), data)
        with self.assertRaises(clean_base.RecipeError):
            clean_base.adapt_bridge(data.replace(b"['/usr/bin/sudo', PYTHON", b"['/usr/bin/sudo', '-n', PYTHON"))

    def test_installed_action_lists_match(self):
        control = load('customer_control_under_test', SOURCE / 'installer/customer_control.py')
        clean = (SOURCE / 'installer/clean_base.py').read_text()
        self.assertIn('ict', control.ACTIONS)
        self.assertIn("'console', 'ict', 'storage_setup'", clean)
        self.assertIn('hl.dsp.focus({workspace=\\"4\\"})', clean)
        runtime = (APP / 'harness/runtime.py').read_text()
        self.assertIn('"ict"', runtime)
        self.assertIn('/opt/decrypt-ict-assistant', runtime)


if __name__ == '__main__':
    unittest.main(verbosity=2)
