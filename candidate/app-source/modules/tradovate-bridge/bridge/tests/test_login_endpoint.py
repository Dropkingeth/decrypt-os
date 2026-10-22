"""1.1.0 multi-login: GET /login/<token> and the login fields of /health and /followers.

/login is read only and answers from the daemon's in-memory state (no Tradovate request): every
account visible on the login, configured ones marked with their role (and key for followers), each
with its positions and working orders by contract and product root. `page.logged_in` is computed
exactly as a /followers row computes `logged_in`, from the latest page status the daemon already
has (the bridge heartbeat and /health refresh it; /login itself never asks the page). With
`read_token_env`, a second token is accepted for GET /login and /health only. Without the new
config keys /health and /followers answer exactly as before. In-memory doubles and loopback HTTP only.
"""
import json
import os
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness  # noqa: F401  (puts the repository on sys.path)

from tvbridge import followers
from tvbridge.daemon import LoginView, make_handler
from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.logins import daemon_settings
from tvbridge.peers import PeerHedge
from tvbridge.state import AccountState
from test_follower_balances import Pub, follower_entry

TOKEN = 'login-endpoint-token-0123456789abcdef'
READ_TOKEN = 'login-endpoint-read-token-0123456789ab'
MNQ, ES, CL, UNKNOWN = 2, 3, 5, 6
ACCOUNTS = [{'id': 7, 'name': 'MAIN-ACCT'}, {'id': 8, 'name': 'F-ONE'}, {'id': 9, 'name': 'F-OFF'},
            {'id': 10, 'name': 'DEMO-OTHER'}]
TDATE = {'year': 2021, 'month': 10, 'day': 6}


def position(pid, acct, cid, net):
    return {'id': pid, 'accountId': acct, 'contractId': cid, 'netPos': net, 'tradeDate': TDATE}


class Page:
    def __init__(self, state):
        self.state, self.epoch, self.page_state = state, 'e1', 'authorized'
        self.requests = []
        self.status_calls = 0
        self.last_status = None  # as tvbridge.bridge.Bridge: the latest status() answer
        self.sync()

    def sync(self, epoch=None):
        self.epoch = epoch or self.epoch
        data = {'accounts': ACCOUNTS,
                'positions': [position(1, 7, MNQ, 0), position(2, 10, ES, -2), position(3, 10, CL, 1),
                              position(4, 8, UNKNOWN, 1)],
                'orders': [{'id': 501, 'accountId': 10, 'contractId': MNQ, 'action': 'Sell', 'ordStatus': 'Working'},
                           {'id': 502, 'accountId': 10, 'contractId': ES, 'action': 'Buy', 'ordStatus': 'Filled'},
                           {'id': 503, 'accountId': 9, 'contractId': MNQ, 'action': 'Buy', 'ordStatus': 'Working'}],
                'orderVersions': [{'id': 601, 'orderId': 501, 'orderQty': 3, 'orderType': 'Limit', 'price': 1.0}],
                'fills': [{'id': 701, 'orderId': 501, 'contractId': MNQ, 'qty': 1, 'price': 1.0, 'active': True,
                           'tradeDate': TDATE, 'action': 'Sell', 'timestamp': '2021-10-06T13:00:00.000Z'}],
                'contracts': [{'id': MNQ, 'name': 'MNQZ1'}, {'id': ES, 'name': 'ESZ1'}, {'id': CL, 'name': 'CLZ1'}]}
        self.state.load_sync(ACCOUNTS, data, self.epoch)

    def status(self):
        self.status_calls += 1
        st = {'state': self.page_state, 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}
        self.last_status = (time.monotonic(), st)
        return st

    def request(self, endpoint, body=None, query='', timeout=5.0, expected_epoch=None):
        self.requests.append(endpoint)
        return {'ok': False, 'sent': False, 'code': 'unexpected'}


class LoginEndpoint(unittest.TestCase):
    def start(self, **extra):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = AccountState()
        self.page = Page(self.state)
        self.cfg = {'ticker': 'MNQZ2021', 'account_name': 'MAIN-ACCT', 'account_id': 7, 'soup_account_id': 'S',
                    'max_contracts': 10, 'tick_size': .25, 'journal_dir': str(self.tmp / 'journal'),
                    'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED'),
                    'followers': [follower_entry('f1', 'F-ONE', self.tmp),
                                  follower_entry('off', 'F-OFF', self.tmp, enabled=False),
                                  follower_entry('gone', 'NOT-HERE', self.tmp)]}
        self.cfg.update(extra)
        settings = daemon_settings(self.cfg)
        read_token = READ_TOKEN if settings['read_token_env'] else None  # main() reads it from that env var
        self.main = Executor(self.page, self.state, Journal(self.cfg['journal_dir']), self.cfg)
        self.main.resolve()
        self.fx = followers.build(self.page, self.state, self.cfg, self.main)
        peers = PeerHedge(settings['peers'], environ={}) if settings['peers'] else None
        self.view = LoginView(settings, self.page, self.state, self.cfg, self.main, self.fx, peers=peers,
                              clock_ms=lambda: 1633529874244)
        kw = {'login_id': settings['login_id']} if settings['explicit'] else {}
        server = ThreadingHTTPServer(('127.0.0.1', 0), followers.make_follower_handler(
            make_handler(self.main, Pub(), TOKEN, False, self.view, read_token), self.fx, TOKEN, self.main, **kw))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = 'http://127.0.0.1:%d' % server.server_address[1]
        self.page.status()  # the bridge heartbeat has seen the page at least once
        self.requests_before = list(self.page.requests)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def login(self):
        status, body = self.get('/login/' + TOKEN)
        self.assertEqual(status, 200)
        return body

    def test_report_lists_every_visible_account_with_roles(self):
        self.start()
        body = self.login()
        self.assertEqual({k: body[k] for k in ('schema', 'login_id', 'label', 'mode', 'observed_at')},
                         {'schema': 'decrypt.tvbridge-login.v1', 'login_id': 'a', 'label': 'Login A', 'mode': 'main',
                          'observed_at': 1633529874244})
        self.assertEqual(body['page'], {'state': 'authorized', 'synced': True, 'logged_in': True, 'problems': []})
        rows = {a['id']: a for a in body['accounts']}
        self.assertEqual([a['id'] for a in body['accounts']], [7, 8, 9, 10])
        self.assertEqual({i: (r['name'], r['configured'], r['role'], r['key']) for i, r in rows.items()},
                         {7: ('MAIN-ACCT', True, 'main', None), 8: ('F-ONE', True, 'follower', 'f1'),
                          9: ('F-OFF', True, 'follower', 'off'), 10: ('DEMO-OTHER', False, None, None)})
        for r in body['accounts']:
            self.assertEqual(set(r), {'name', 'id', 'configured', 'role', 'key', 'positions', 'working'})
        self.assertEqual(rows[10]['positions'], [{'contract': 'ESZ1', 'root': 'ES', 'net': -2},
                                                 {'contract': 'CLZ1', 'root': 'CL', 'net': 1}])
        self.assertEqual(rows[10]['working'], [{'contract': 'MNQZ1', 'root': 'MNQ', 'side': 'Sell', 'qty': 2}])
        self.assertEqual(rows[9]['working'], [{'contract': 'MNQZ1', 'root': 'MNQ', 'side': 'Buy', 'qty': None}])
        self.assertEqual(rows[8]['positions'], [{'contract': None, 'root': None, 'net': 1}])
        self.assertEqual(rows[7]['positions'], [{'contract': 'MNQZ1', 'root': 'MNQ', 'net': 0}])
        self.assertEqual(self.page.requests, self.requests_before, '/login never sends a Tradovate request')

    def test_contract_names_come_from_memory_caches(self):
        self.start()
        self.main.contract_names[UNKNOWN] = 'RTYZ1'  # an earlier lookup by an executor
        rows = {a['id']: a for a in self.login()['accounts']}
        self.assertEqual(rows[8]['positions'], [{'contract': 'RTYZ1', 'root': 'RTY', 'net': 1}])
        self.assertEqual(self.page.requests, self.requests_before, 'no contract lookup on the request path')

    def test_login_never_asks_the_page(self):
        self.start()
        calls = self.page.status_calls
        for _ in range(3):
            self.login()
        self.assertEqual(self.page.status_calls, calls)

    def test_no_known_page_status_is_not_logged_in(self):
        self.start()
        self.page.last_status = None
        page = self.login()['page']
        self.assertEqual(page, {'state': None, 'synced': False, 'logged_in': False, 'problems': ['page_status_unknown']})
        self.get('/health/' + TOKEN)  # the health check refreshes it
        self.assertTrue(self.login()['page']['logged_in'])

    def test_stale_page_status_is_not_logged_in(self):
        self.start()
        at, st = self.page.last_status
        self.page.last_status = (at - 31, st)
        self.view._page = None
        page = self.login()['page']
        self.assertFalse(page['logged_in'])
        self.assertIn('page_status_stale', page['problems'])
        self.assertIn('tradovate_silent', page['problems'])

    def test_read_token_reads_login_and_health_only(self):
        self.start(read_token_env='TVBRIDGE_READ_TOKEN_A')
        self.assertEqual(self.get('/login/' + READ_TOKEN)[0], 200)
        self.assertEqual(self.get('/health/' + READ_TOKEN)[0], 200)
        for path in ('/followers/' + READ_TOKEN, '/state/' + READ_TOKEN, '/follower/' + READ_TOKEN + '/f1/state'):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)
        req = urllib.request.Request(self.base + '/order/' + READ_TOKEN, data=b'{}', method='POST')
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)
        for path in ('/follower/' + READ_TOKEN + '/f1/order', '/follower/' + READ_TOKEN + '/f1/kill'):
            req = urllib.request.Request(self.base + path, data=b'{}', method='POST')
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 404)
        self.assertFalse(os.path.exists(self.tmp / 'f1' / 'ENTRIES-DISABLED'))
        # The order token still works everywhere.
        self.assertEqual(self.get('/login/' + TOKEN)[0], 200)
        self.assertEqual(self.get('/followers/' + TOKEN)[0], 200)

    def test_token_auth(self):
        self.start()
        for path in ('/login/' + 'x' * len(TOKEN), '/login/', '/login', '/login/' + TOKEN + '/x'):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)

    def test_logged_in_matches_follower_rows(self):
        self.start()
        scenarios = [('authorized', None, True), ('connecting', None, False), ('authorized', 'e2', False)]
        for page_state, new_epoch, expected in scenarios:
            with self.subTest(page_state=page_state, epoch=new_epoch):
                self.page.page_state = page_state
                if new_epoch:
                    self.page.epoch = new_epoch  # a new connection the state has not synced yet
                self.page.status()  # the heartbeat sees the change
                page = self.login()['page']
                rows = self.get('/followers/' + TOKEN)[1]
                self.assertEqual(page['logged_in'], expected)
                self.assertEqual(rows['main']['logged_in'], expected)
                self.assertEqual(rows['followers'][0]['logged_in'], expected)
                if not expected:
                    self.assertTrue(page['problems'])
                self.page.page_state, self.page.epoch = 'authorized', 'e1'

    def test_without_new_keys_health_and_followers_are_unchanged(self):
        self.start()
        status, health = self.get('/health/' + TOKEN)
        self.assertEqual((status, set(health)), (200, {'health', 'publisher', 'entries_disabled'}))
        report = self.get('/followers/' + TOKEN)[1]
        self.assertNotIn('login_id', report)
        self.assertNotIn('login_id', report['main'])
        for row in report['followers']:
            self.assertNotIn('login_id', row)

    def test_named_login_adds_login_id(self):
        self.start(login_id='a', login_label='Login A')
        self.assertEqual(self.get('/health/' + TOKEN)[1]['login_id'], 'a')
        report = self.get('/followers/' + TOKEN)[1]
        self.assertEqual(report['login_id'], 'a')
        self.assertEqual({r['login_id'] for r in report['followers'] + [report['main']]}, {'a'})
        self.assertEqual(self.login()['login_id'], 'a')

    def test_peer_notes_in_health_not_problems(self):
        self.start(peer_logins=[{'id': 'b', 'url': 'http://127.0.0.1:1', 'token_env': 'TVBRIDGE_TOKEN_B'}])
        status, health = self.get('/health/' + TOKEN)
        self.assertEqual(health['notes'], ['peer_unreachable:b'])  # not polled yet
        with self.assertLogs('tvbridge', level='WARNING'):
            self.view.peers.poll_once()
        health = self.get('/health/' + TOKEN)[1]
        self.assertEqual(health['notes'], ['peer_unreachable:b'])
        self.assertNotIn('peer_unreachable:b', health['health']['problems'])
        self.assertTrue(health['health']['ok'])


if __name__ == '__main__':
    unittest.main()
