"""The guided bridge setup: validation, the real module setup.py in a temporary data root, and the
read-only sign-in check against a fake loopback daemon. No systemctl, no browser, no real account."""
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/backend'))
import tradovate_service as bridge  # noqa: E402

FORM = {'accountName': 'SIM-ACCOUNT-1', 'accountNumber': '123456', 'contract': 'MNQZ2026'}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='dos-bridge-setup-'))
        self.addCleanup(__import__('shutil').rmtree, self.home, True)
        self.data = self.home / 'data'
        env = patch.dict(os.environ, {'HOME': str(self.home), 'DECRYPT_OS_DATA': str(self.data),
                                      'DECRYPT_BRIDGE_MODULE': str(ROOT / 'modules/tradovate-bridge')})
        env.start(); self.addCleanup(env.stop)
        for key in ('TVBRIDGE_ROOT', 'TVBRIDGE_STATE', 'DECRYPT_SOUP_ROOT'):
            os.environ.pop(key, None)
        self.root = self.data / 'tvbridge'

    def test_refuses_bad_values_before_running_anything(self):
        bad = [({**FORM, 'accountName': 'DEMO-ACCOUNT-NAME-FROM-RECON'}, 'invalid_text'),
               ({**FORM, 'accountName': '<your account>'}, 'invalid_text'),
               ({**FORM, 'accountName': '--help'}, 'invalid_text'),
               ({**FORM, 'accountName': 'a\nb'}, 'invalid_text'),
               ({**FORM, 'accountNumber': 'ABC'}, 'invalid_account_number'),
               ({**FORM, 'accountNumber': 123456}, 'invalid_account_number'),
               ({**FORM, 'contract': 'MNQ1!'}, 'invalid_contract'),
               ({**FORM, 'contract': 'ESZ2026'}, 'invalid_contract'),
               ({**FORM, 'extra': 1}, 'invalid_fields'), ({'accountName': 'x'}, 'invalid_fields'), ('x', 'invalid_fields')]
        with patch.object(bridge, 'run_setup', side_effect=AssertionError('must not run')):
            for data, code in bad:
                with self.subTest(data=data), self.assertRaises(bridge.BridgeError) as caught:
                    bridge.setup_bridge(data)
                self.assertEqual((caught.exception.code, caught.exception.status), (code, 400))
        self.assertFalse(self.root.exists())

    def test_setup_arguments_are_fixed_and_exact(self):
        self.assertEqual(bridge.setup_arguments({**FORM, 'accountName': '  My Sim 1 '}),
                         ['init', '--account-name', 'My Sim 1', '--soup-account-id', '123456', '--ticker', 'MNQZ2026'])

    def test_setup_creates_a_paused_login_a_and_returns_no_token(self):
        result = bridge.setup_bridge(FORM)
        self.assertEqual((result['paused'], result['firingEngineFound']), (True, False))
        self.assertGreater(result['created'], 5)
        cfg = json.loads((self.root / 'config.json').read_text())
        self.assertEqual((cfg['account_name'], cfg['soup_account_id'], cfg['ticker']), ('SIM-ACCOUNT-1', '123456', 'MNQZ2026'))
        self.assertTrue((self.root / 'ENTRIES-DISABLED').exists())
        tokens = [ln.split('=', 1)[1] for name in ('tvbridge.env', 'logins/peers.env', 'logins/manual-a.env')
                  for ln in (self.root / name).read_text().splitlines() if '=' in ln and not ln.startswith('#')]
        self.assertEqual(len(tokens), 3)
        for token in tokens:
            self.assertNotIn(token, json.dumps(result))
        status = bridge.bridge_status()
        self.assertEqual((status['configured'], status['paused'], status['firing']), (True, True, True))
        self.assertEqual([lg['id'] for lg in status['logins']], ['a'])

    def test_setup_is_idempotent_and_refuses_another_account(self):
        bridge.setup_bridge(FORM)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(bridge.setup_bridge(FORM)['created'], 0)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        with self.assertRaises(bridge.BridgeError) as caught:
            bridge.setup_bridge({**FORM, 'accountName': 'SIM-ACCOUNT-2'})
        self.assertEqual(caught.exception.code, 'bridge_setup_refused')
        self.assertIn('already names another account', caught.exception.message)
        self.assertNotIn('refused:', caught.exception.message)

    @unittest.skipIf(sys.platform == 'linux', 'enable calls systemctl on Linux')
    def test_enable_and_disable_refuse_outside_linux(self):
        bridge.setup_bridge(FORM)
        for call in (bridge.enable_bridge, bridge.disable_bridge):
            with self.assertRaises(bridge.BridgeError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'bridge_linux_only')

    def test_missing_module_is_reported(self):
        with patch.dict(os.environ, {'DECRYPT_BRIDGE_MODULE': str(self.home / 'nowhere')}):
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.setup_bridge(FORM)
        self.assertEqual((caught.exception.code, caught.exception.status), ('bridge_module_missing', 503))


class FakeDaemon(BaseHTTPRequestHandler):
    reply = {}
    token = ''

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/login/' + self.token:
            body = json.dumps(self.reply).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()


class LoginStatusTests(SetupTests):
    def setUp(self):
        super().setUp()
        self.state = self.data / 'tvbridge-state'

    def read_token(self):
        return bridge._env_value(self.root / 'logins/peers.env', 'TVBRIDGE_READ_TOKEN_A')

    def serve(self, reply):
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeDaemon)
        FakeDaemon.reply, FakeDaemon.token = reply, self.read_token()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        cfg = json.loads((self.root / 'config.json').read_text())
        cfg['listen_port'] = server.server_port
        (self.root / 'config.json').write_text(json.dumps(cfg))

    def guard(self, state, reason):
        (self.state / 'login-guard').mkdir(parents=True, exist_ok=True)
        (self.state / 'login-guard' / 'status.json').write_text(json.dumps({'state': state, 'reason': reason}))

    def test_not_configured(self):
        out = bridge.login_status()
        self.assertEqual((out['configured'], out['daemon'], out['signIn'], out['accounts']), (False, 'unreachable', 'unknown', []))

    def test_daemon_not_answering(self):
        bridge.setup_bridge(FORM)
        with socket.socket() as free:
            free.bind(('127.0.0.1', 0)); port = free.getsockname()[1]
        cfg = json.loads((self.root / 'config.json').read_text()); cfg['listen_port'] = port
        (self.root / 'config.json').write_text(json.dumps(cfg))
        out = bridge.login_status()
        self.assertEqual((out['configured'], out['daemon'], out['signIn']), (True, 'unreachable', 'unknown'))
        self.assertEqual(out['account'], {'name': 'SIM-ACCOUNT-1', 'number': '123456', 'contract': 'MNQZ2026'})
        self.assertIn('Turn it on', out['detail'])
        self.guard('needs_human', 'captcha')
        out = bridge.login_status()
        self.assertEqual(out['signIn'], 'needs_human')
        self.assertIn('captcha', out['detail'])

    def test_signed_in_lists_accounts_and_matches_the_setup(self):
        bridge.setup_bridge(FORM)
        self.serve({'page': {'state': 'authorized', 'logged_in': True, 'synced': True, 'problems': []},
                    'accounts': [{'name': 'SIM-ACCOUNT-1', 'id': 123456, 'configured': True, 'role': 'main', 'positions': [], 'working': []},
                                 {'name': 'SIM-ACCOUNT-9', 'id': 999, 'configured': False, 'role': None}]})
        out = bridge.login_status()
        self.assertEqual((out['daemon'], out['signIn'], out['nameMatch'], out['numberMatch']), ('reachable', 'connected', True, True))
        self.assertEqual(out['accounts'], [{'id': 123456, 'name': 'SIM-ACCOUNT-1', 'configured': True},
                                           {'id': 999, 'name': 'SIM-ACCOUNT-9', 'configured': False}])
        self.assertIn('2 accounts', out['detail'])
        self.assertNotIn(self.read_token(), json.dumps(out))

    def test_signed_in_but_name_or_number_differs(self):
        bridge.setup_bridge(FORM)
        self.serve({'page': {'state': 'authorized', 'logged_in': True}, 'accounts': [{'name': 'SIM-ACCOUNT-1', 'id': 7}]})
        out = bridge.login_status()
        self.assertEqual((out['nameMatch'], out['numberMatch']), (True, False))
        FakeDaemon.reply = {'page': {'state': 'authorized', 'logged_in': True}, 'accounts': [{'name': 'OTHER', 'id': 123456}]}
        out = bridge.login_status()
        self.assertEqual((out['nameMatch'], out['numberMatch']), (False, False))

    def test_logged_out_page(self):
        bridge.setup_bridge(FORM)
        self.serve({'page': {'state': 'login_page', 'logged_in': False}, 'accounts': []})
        out = bridge.login_status()
        self.assertEqual((out['daemon'], out['signIn'], out['nameMatch']), ('reachable', 'logged_out', None))
        self.guard('needs_human', 'second_factor')
        self.assertEqual(bridge.login_status()['signIn'], 'needs_human')

    def test_wrong_token_file_is_treated_as_unreachable(self):
        bridge.setup_bridge(FORM)
        self.serve({'page': {'state': 'authorized', 'logged_in': True}, 'accounts': []})
        (self.root / 'logins/peers.env').write_text('TVBRIDGE_READ_TOKEN_A=short\n')
        self.assertEqual(bridge.login_status()['daemon'], 'unreachable')


class ServiceDispatchTests(unittest.TestCase):
    """The trading service maps the bridge actions and their refusals; nothing else runs."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_trading_service import ServiceTests
        self.inner = ServiceTests('test_installs_exact_core_held_without_secret_in_status_or_ledger')
        self.inner.setUp()
        self.addCleanup(self.inner.doCleanups)
        self.service = self.inner.service

    def test_actions_return_status_plus_their_report(self):
        with patch.object(bridge, 'setup_bridge', return_value={'created': 9, 'paused': True, 'firingEngineFound': False}) as setup, \
                patch.object(bridge, 'login_status', return_value={'configured': True, 'signIn': 'unknown'}):
            result = self.service.action('bridge-setup', FORM)
            setup.assert_called_once_with(FORM)
            self.assertEqual(result['bridgeSetup']['paused'], True)
            self.assertIn('instances', result)
            self.assertEqual(self.service.action('bridge-login', {})['bridgeLogin']['signIn'], 'unknown')

    def test_refusals_become_trading_errors_with_the_same_code(self):
        from trading_service import TradingError
        with patch.object(bridge, 'run_setup', side_effect=bridge.BridgeError('bridge_linux_only', 'Linux only', 409)):
            with self.assertRaises(TradingError) as caught:
                self.service.action('bridge-enable', {})
            self.assertEqual((caught.exception.code, caught.exception.status), ('bridge_linux_only', 409))
        with self.assertRaises(TradingError) as caught:
            self.service.action('bridge-setup', {**FORM, 'accountNumber': 'x'})
        self.assertEqual(caught.exception.code, 'invalid_account_number')
        for action in ('bridge-enable', 'bridge-disable', 'bridge-login'):
            with self.assertRaises(Exception):
                self.service.action(action, {'extra': True})


if __name__ == '__main__':
    unittest.main()
