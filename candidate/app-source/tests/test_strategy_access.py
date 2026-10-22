"""Strategy access: username-only request to Decrypt Labs. Loopback and fakes only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('strategy_access_server', ROOT / 'app/backend/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
access = server.strategy_access
OFFLINE = {'requestUrl': '', 'publicSite': 'https://decryptlabs.io'}
NO_PROXY = {'no_proxy': '*', 'NO_PROXY': '*', 'http_proxy': '', 'HTTP_PROXY': '', 'https_proxy': '', 'HTTPS_PROXY': ''}


class FakeSender:
    configured = True
    strategies_supported = False

    def __init__(self, error=None):
        self.calls, self.error = [], error

    def send(self, username, contact=None, strategies=None):
        self.calls.append((username, contact, strategies))
        if self.error:
            raise access.SendError(self.error)


class ConfigTests(unittest.TestCase):
    def write(self, value):
        path = Path(tempfile.mkdtemp(prefix='decrypt-access-config-')) / 'c.json'
        path.write_text(json.dumps(value))
        return path

    def test_shipped_default_sends_to_the_public_decrypt_labs_route(self):
        config = access.load_config(access.DEFAULT_CONFIG)
        self.assertEqual(config, {'requestUrl': 'https://access.decryptlabs.io/v1/strategy-access',
                                  'publicSite': 'https://decryptlabs.io', 'strategyFieldSupported': False})
        self.assertTrue(access.HttpSender(config['requestUrl']).configured)

    def test_strategy_field_supported_flag_is_read_and_validated(self):
        config = access.load_config(self.write({'requestUrl': '', 'strategyFieldSupported': True}))
        self.assertTrue(config['strategyFieldSupported'])
        for bad in ('true', 1, None):
            with self.subTest(bad=bad), self.assertRaises(access.ConfigError):
                access.load_config(self.write({'requestUrl': '', 'strategyFieldSupported': bad}))

    def test_empty_url_disables_sending(self):
        config = access.load_config(self.write(OFFLINE))
        self.assertEqual(config['requestUrl'], '')
        self.assertFalse(access.HttpSender(config['requestUrl']).configured)

    def test_only_plain_https_urls_are_accepted(self):
        for url in ('http://access.example.test/v1/x', 'https://user:pw@access.example.test/x',
                    'https://access.example.test/x?id=1', 'https://access.example.test/x#f', 'ftp://x', 'https:// x'):
            with self.subTest(url=url), self.assertRaises(access.ConfigError):
                access.load_config(self.write({'requestUrl': url}))
        with self.assertRaises(access.ConfigError):
            access.load_config(self.write({'requestUrl': '', 'installationId': 'x'}))

    def test_broken_config_degrades_to_manual_send(self):
        with patch.object(access, 'USER_CONFIG', Path('/nonexistent')), \
             patch.object(access, 'DEFAULT_CONFIG', self.write({'requestUrl': 'http://insecure.example.test'})):
            store = server.Store(tempfile.mkdtemp(prefix='decrypt-access-store-'))
        self.assertFalse(store.access_sender.configured)
        self.assertEqual(store.state()['strategyAccess']['publicSite'], 'https://decryptlabs.io')


class Recorder(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers['Content-Length'])
        self.server.seen.append({'path': self.path, 'headers': dict(self.headers), 'body': self.rfile.read(length)})
        status = self.server.status
        self.send_response(status)
        if status in (301, 302, 307, 308):
            self.send_header('Location', 'http://127.0.0.1:9/elsewhere')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'{}')


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, NO_PROXY)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        self.http.seen, self.http.status = [], 202
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.sender = access.HttpSender(f'http://127.0.0.1:{self.http.server_port}/v1/strategy-access', timeout=5)

    def test_body_holds_only_what_the_user_typed(self):
        self.sender.send('Fixture_User')
        self.sender.send('Fixture_User', 'fixture contact')
        first, second = (json.loads(item['body']) for item in self.http.seen)
        self.assertEqual(first, {'tradingViewUsername': 'Fixture_User'})
        self.assertEqual(second, {'tradingViewUsername': 'Fixture_User', 'contact': 'fixture contact'})
        headers = {k.lower(): v for k, v in self.http.seen[0]['headers'].items()}
        self.assertEqual(set(headers) - {'host', 'content-length', 'connection', 'accept-encoding'},
                         {'content-type', 'accept', 'user-agent'})
        self.assertEqual(headers['user-agent'], access.USER_AGENT)

    def test_failures_map_to_reason_codes(self):
        for status, reason in ((429, 'rate_limited'), (404, 'refused'), (400, 'refused'), (503, 'unavailable'),
                               (302, 'redirect_refused'), (204, 'unexpected_response')):
            self.http.status = status
            with self.subTest(status=status), self.assertRaises(access.SendError) as raised:
                self.sender.send('Fixture_User')
            self.assertEqual(str(raised.exception), reason)
        self.http.server_close()
        with self.assertRaises(access.SendError) as raised:
            access.HttpSender('http://127.0.0.1:9/closed', timeout=2).send('Fixture_User')
        self.assertEqual(str(raised.exception), 'unavailable')

    def test_unconfigured_sender_never_connects(self):
        with self.assertRaisesRegex(access.SendError, 'not_configured'):
            access.HttpSender('').send('Fixture_User')
        self.assertEqual(self.http.seen, [])

    def test_strategies_are_not_sent_while_unsupported(self):
        # The live service rejects unknown JSON keys today; a picked strategy
        # list must stay off the wire until strategies_supported flips on.
        self.sender.send('Fixture_User', strategies=['soup', 'fvg-ifvg'])
        body = json.loads(self.http.seen[-1]['body'])
        self.assertEqual(body, {'tradingViewUsername': 'Fixture_User'})

    def test_strategies_are_sent_once_supported(self):
        supported = access.HttpSender(self.sender.url, timeout=5, strategies_supported=True)
        supported.send('Fixture_User', strategies=['soup', 'fvg-ifvg'])
        body = json.loads(self.http.seen[-1]['body'])
        self.assertEqual(body, {'tradingViewUsername': 'Fixture_User', 'strategies': ['soup', 'fvg-ifvg']})

    def test_empty_strategies_list_is_never_sent(self):
        supported = access.HttpSender(self.sender.url, timeout=5, strategies_supported=True)
        supported.send('Fixture_User', strategies=[])
        body = json.loads(self.http.seen[-1]['body'])
        self.assertEqual(body, {'tradingViewUsername': 'Fixture_User'})


class CleanStrategiesTests(unittest.TestCase):
    ALLOWED = {'soup', 'fvg-ifvg', 'ote-silver-bullet'}

    def test_absent_value_is_an_empty_list(self):
        self.assertEqual(access.clean_strategies(None, self.ALLOWED), [])

    def test_known_ids_are_kept_in_order_and_deduped(self):
        self.assertEqual(access.clean_strategies(['fvg-ifvg', 'soup', 'fvg-ifvg'], self.ALLOWED),
                          ['fvg-ifvg', 'soup'])

    def test_unknown_id_fails_the_whole_list(self):
        self.assertIsNone(access.clean_strategies(['soup', 'not-a-strategy'], self.ALLOWED))

    def test_non_list_fails(self):
        for bad in ('soup', 7, {'soup': True}):
            with self.subTest(bad=bad):
                self.assertIsNone(access.clean_strategies(bad, self.ALLOWED))

    def test_over_the_limit_fails(self):
        self.assertIsNone(access.clean_strategies(['soup'] * (access.STRATEGIES_LIMIT + 1), {'soup'}))

    def test_bad_shaped_id_fails(self):
        for bad in ('Soup', 'has space', 'x' * 41, ''):
            with self.subTest(bad=bad):
                self.assertIsNone(access.clean_strategies([bad], {bad.lower() if bad else 'x'}))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='decrypt-access-')
        self.addCleanup(self.temp.cleanup)
        self.sender = FakeSender()
        self.store = server.Store(self.temp.name, OFFLINE, self.sender)

    def test_fresh_system_sends_username_without_any_registration(self):
        state = self.store.send_access_request({'tradingViewUsername': ' Fixture_User '})
        self.assertEqual(self.sender.calls, [('Fixture_User', None, None)])
        latest = state['strategyAccess']['latest']
        self.assertEqual((latest['status'], latest['reason'], latest['tradingViewUsername']), ('sent', None, 'Fixture_User'))
        self.assertTrue(latest['sentAt'])
        self.assertEqual(state['profile']['tradingViewUsername'], 'Fixture_User')
        for key in ('customerRegistry', 'supportOutbox', 'accessRequests', 'accessService'):
            self.assertNotIn(key, state)

    def test_optional_contact_is_sent_only_when_typed(self):
        self.store.send_access_request({'tradingViewUsername': 'Fixture_User', 'contact': ''})
        self.store.send_access_request({'tradingViewUsername': 'Other_User', 'contact': ' fixture contact '})
        self.assertEqual(self.sender.calls, [('Fixture_User', None, None), ('Other_User', 'fixture contact', None)])

    def test_only_username_and_contact_are_accepted(self):
        for extra in ({'installationId': 'x'}, {'strategyId': 'soup'}, {'consent': True}, {'accountId': '1'}):
            with self.subTest(extra=extra), self.assertRaisesRegex(server.ApiError, 'fields'):
                self.store.send_access_request({'tradingViewUsername': 'Fixture_User', **extra})
        for name in ('', 'a b', 'user@example.test', 'https://www.tradingview.com/u/x', 'x' * 65, None, 7):
            with self.subTest(name=name), self.assertRaises(server.ApiError):
                self.store.send_access_request({'tradingViewUsername': name})
        for contact in ('x' * 201, 'line\nbreak', 7):
            with self.subTest(contact=contact), self.assertRaises(server.ApiError):
                self.store.send_access_request({'tradingViewUsername': 'Fixture_User', 'contact': contact})
        self.assertEqual(self.sender.calls, [])

    def test_failed_send_keeps_username_for_manual_send(self):
        self.store.access_sender = FakeSender('refused')
        state = self.store.send_access_request({'tradingViewUsername': 'Fixture_User'})
        latest = state['strategyAccess']['latest']
        self.assertEqual((latest['status'], latest['reason'], latest['sentAt']), ('not_sent', 'refused', None))
        self.assertEqual(state['strategyAccess']['tradingViewUsername'], 'Fixture_User')
        # A failed send can be retried at once.
        self.store.access_sender = self.sender
        self.assertEqual(self.store.send_access_request({'tradingViewUsername': 'Fixture_User'})['strategyAccess']['latest']['status'], 'sent')

    def test_rate_limits(self):
        self.store.send_access_request({'tradingViewUsername': 'Fixture_User'}, now=1000)
        with self.assertRaises(server.ApiError) as raised:
            self.store.send_access_request({'tradingViewUsername': 'fixture_user'}, now=1100)
        self.assertEqual((raised.exception.code, raised.exception.status), ('recently_sent', 429))
        self.store.send_access_request({'tradingViewUsername': 'Fixture_User'}, now=1000 + access.RESEND_SECONDS)
        for index in range(3):
            self.store.send_access_request({'tradingViewUsername': f'Other_{index}'}, now=1700 + index)
        with self.assertRaises(server.ApiError) as raised:
            self.store.send_access_request({'tradingViewUsername': 'Another'}, now=1800)
        self.assertEqual(raised.exception.code, 'too_many_requests')
        self.assertEqual(len(self.sender.calls), 5)

    def test_activity_never_holds_username_or_contact(self):
        self.store.send_access_request({'tradingViewUsername': 'Fixture_User', 'contact': 'fixture contact'})
        text = json.dumps(self.store.state()['activity'])
        self.assertNotIn('Fixture_User', text)
        self.assertNotIn('fixture contact', text)

    def test_interrupted_send_is_offered_again(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO strategy_access_requests(username,contact,status,created_at,attempted) VALUES ('Fixture_User','','sending','2021-10-11T00:00:00Z',0)")
        latest = server.Store(self.temp.name, OFFLINE, self.sender).state()['strategyAccess']['latest']
        self.assertEqual((latest['status'], latest['reason']), ('not_sent', 'interrupted'))

    def test_picked_strategies_are_recorded_locally_even_when_not_sent(self):
        # FakeSender.strategies_supported defaults False, same as the live
        # service today: the request still succeeds and the picks are kept
        # for the UI to show back, even though send() (SenderTests above)
        # proves they never reach the wire while unsupported.
        state = self.store.send_access_request({'tradingViewUsername': 'Fixture_User', 'strategies': ['soup', 'fvg-ifvg']})
        latest = state['strategyAccess']['latest']
        self.assertEqual(latest['status'], 'sent')
        self.assertEqual(latest['strategies'], ['soup', 'fvg-ifvg'])
        self.assertEqual(self.sender.calls, [('Fixture_User', None, ['soup', 'fvg-ifvg'])])
        self.assertFalse(state['strategyAccess']['strategyFieldSupported'])

    def test_unknown_strategy_id_is_rejected(self):
        for bad in (['not-a-real-strategy'], ['soup', 'lumi-rth-gap'], 'soup', ['soup'] * 11):
            with self.subTest(bad=bad), self.assertRaisesRegex(server.ApiError, 'strategies'):
                self.store.send_access_request({'tradingViewUsername': 'Fixture_User', 'strategies': bad})
        self.assertEqual(self.sender.calls, [])

    def test_no_strategies_picked_is_still_a_valid_plain_request(self):
        state = self.store.send_access_request({'tradingViewUsername': 'Fixture_User'})
        self.assertEqual(state['strategyAccess']['latest']['strategies'], [])
        self.assertEqual(self.sender.calls, [('Fixture_User', None, None)])

    def test_requestable_strategies_exposed_in_state(self):
        self.assertEqual(set(self.store.state()['strategyAccess']['requestableStrategies']),
                          {'soup', 'fvg-ifvg', 'ote-refined-large', 'ote-refined-small', 'ote-silver-bullet'})

    def test_registration_era_state_is_removed_and_username_kept(self):
        path = Path(self.temp.name) / 'onboarding.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('''
            CREATE TABLE access_requests (id TEXT PRIMARY KEY, username TEXT);
            CREATE TABLE outbox (event_id TEXT PRIMARY KEY);
            CREATE TABLE customer_registry_cache (installation_id TEXT PRIMARY KEY, envelope TEXT);
            CREATE TABLE customer_sync_state (installation_id TEXT PRIMARY KEY);
            CREATE TABLE support_outbox (id TEXT PRIMARY KEY);
            CREATE TABLE legacy_access_history (request_id TEXT PRIMARY KEY);
            DELETE FROM settings WHERE key='tradingViewUsername';
            INSERT INTO settings VALUES ('boundTradingViewUsername','Bound_User');
            INSERT INTO settings VALUES ('usernameBindingReviewRequired','true');
            ''')
        state = server.Store(self.temp.name, OFFLINE, self.sender).state()
        self.assertEqual(state['profile']['tradingViewUsername'], 'Bound_User')
        with sqlite3.connect(path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            keys = {row[0] for row in db.execute('SELECT key FROM settings')}
        self.assertEqual(tables & {'access_requests', 'outbox', 'customer_registry_cache', 'customer_sync_state',
                                   'support_outbox', 'legacy_access_history'}, set())
        self.assertFalse(keys & {'boundTradingViewUsername', 'usernameBindingReviewRequired', 'enrollmentTradingViewUsername'})


if __name__ == '__main__':
    unittest.main()
