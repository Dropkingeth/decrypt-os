"""Firing across Tradovate logins (1.1.0 multi-login), browser-free.

Two in-process fake bridge daemons (login a, primary, with the leader; login b, followers only) serve
GET /followers, GET /login, GET /follower/<key>/state and POST /follower/<key>/order|kill, each with
its own token. Soup is the real runtime.

- Config: `logins_file`, `accounts.<key>.login`, `cross_login_hedge_guard`, `hedge_groups`; no
  registry read at all without them.
- Each lane sends to its own login's daemon with that login's token (orders, state, kill switch).
- Pins per login: a follower missing from its own login's report is refused alone.
- Lane decisions read a cache of GET /login reports that a background poller refreshes
  (LoginReports.poll_once/start); the align loop never reads another login over the network.
- Login down (a non-primary login's daemon answers that its page is logged out): only that login's
  lanes skip new entries (`login_down:<id>`), and they resume on the next signal. An unreachable
  login does not skip (warned); primary-login lanes never check.
- Cross-login hedge check: opposite exposure on a counted account of another login (role main or
  follower; every account with peer_hedge_scope "all") skips the entry
  (`hedge_conflict_login:<id>:<root>`) unless it clears within align_wait_ms. An unreachable,
  stale or logged-out login or a malformed report fails CLOSED (`hedge_unknown_login:<id>:<why>`) and
  an opposite row on an unknown contract counts as a conflict, with one warning a minute.
- /login reads use the login's read token (registry read_token_env) when set; orders its order token.
- Grid grouped by login; one login keeps today's text.
"""
import json
import logging
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import Harness  # noqa: F401  (puts the repository on sys.path)
from test_mirror import CONFIG, SOUP, TOKEN, Recorder, SoupSide, dead_port  # noqa: E402

from tvbridge import firing as firing_mod
from tvbridge.firing import (ALERTS_NAME, Firing, FiringConfigError, LoginLink, LoginReports, multi_login,
                             resolve_logins, validate)
from tvbridge.mirror import JOURNAL_NAME

logging.getLogger('tvbridge.firing').addHandler(logging.NullHandler())
logging.getLogger('tvbridge.firing').propagate = False
logging.getLogger('tvbridge.mirror').propagate = False

TOKEN_B = 'login-b-test-token-0123456789abcdef'
LOGIN_SCHEMA = 'decrypt.tvbridge-login.v1'


def records(path):
    out = []
    if Path(path).exists():
        for line in Path(path).read_text().split('\n'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def write_registry(tmp, url_a, url_b=None, name='logins.json', read_tokens=False):
    def entry(lid, url, primary, token_env, port, suffix):
        e = {'id': lid, 'label': 'Login ' + lid.upper(), 'primary': primary,
             'config': str(tmp / f'config-{lid}.json'), 'bridge_url': url, 'token_env': token_env,
             'devtools_port': port, 'browser_unit': f'tvbridge-browser{suffix}.service',
             'daemon_unit': f'tvbridge-daemon{suffix}.service', 'guard_unit': f'tvbridge-login-guard{suffix}.service',
             'trigger_unit': f'tvbridge-login-trigger{suffix}.service', 'guard_state_dir': str(tmp / f'guard-{lid}')}
        if read_tokens:
            e['read_token_env'] = 'TVBRIDGE_READ_TOKEN_' + lid.upper()
        return e
    logins = [entry('a', url_a, True, 'TVBRIDGE_TOKEN', 9555, '')]
    if url_b:
        logins.append(entry('b', url_b, False, 'TVBRIDGE_TOKEN_B', 9557, '@b'))
    p = tmp / name
    p.write_text(json.dumps({'schema': 'decrypt.tvbridge-logins.v1', 'logins': logins}))
    return str(p)


class FakeDaemon:
    """One login's bridge daemon, reduced to the routes the firing service uses. Wrong token: 404."""

    def __init__(self, token, login_id, keys=(), main=False, read_token=None):
        self.token, self.login_id, self.read_token = token, login_id, read_token
        self.logged_in = True
        self.main = main
        self.rows = {k: {'key': k, 'available': True, 'entries_disabled': False, 'net_position': 0, 'orders': [],
                         'native_exits': True} for k in keys}
        self.accounts = []          # GET /login accounts
        self.login_reads = 0
        self.on_login_read = None   # optional hook(self) before each /login answer
        self.orders, self.kills, self.paths = [], [], []
        self.lock = threading.Lock()
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parts = self.path.split('?')[0].split('/')
                read = daemon.read_token is not None and len(parts) == 3 and parts[2] == daemon.read_token \
                    and parts[1] in ('login', 'health')
                with daemon.lock:
                    daemon.paths.append(('GET', parts[1] if len(parts) > 1 else '') + (('read',) if read else ()))
                if read:
                    return self.answer(200, daemon.login())
                if len(parts) < 3 or parts[2] != daemon.token:
                    return self.answer(404, {'error': 'not found'})
                if parts[1] == 'followers' and len(parts) == 3:
                    return self.answer(200, daemon.followers())
                if parts[1] == 'login' and len(parts) == 3:
                    return self.answer(200, daemon.login())
                if parts[1] == 'follower' and len(parts) == 5 and parts[4] == 'state':
                    return self.answer(200, {'net_position': 0, 'open_order_ids': [], 'synced': True,
                                             'healthy': True, 'unseen_accepted_orders': [], 'entries_disabled': False,
                                             'trade_entry_outcome': None})
                return self.answer(404, {'error': 'not found'})

            def do_POST(self):
                parts = self.path.split('/')
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                if len(parts) != 5 or parts[1] != 'follower' or parts[2] != daemon.token:
                    return self.answer(404, {'error': 'not found'})
                with daemon.lock:
                    if parts[4] == 'order':
                        daemon.orders.append((parts[3], body))
                        return self.answer(200, {'outcome': 'accepted', 'receipt': {'bridge': 'accepted'}})
                    if parts[4] == 'kill':
                        daemon.kills.append(parts[3])
                        return self.answer(200, {'entries_disabled': True})
                return self.answer(404, {'error': 'not found'})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def followers(self):
        rows = [dict(r, logged_in=self.logged_in) for r in self.rows.values()]
        out = {'followers': rows}
        if self.main:
            out['main'] = {'key': 'main', 'available': True, 'logged_in': self.logged_in, 'entries_disabled': False,
                           'net_position': 0, 'orders': [], 'synced': True}
        return out

    def login(self):
        with self.lock:
            self.login_reads += 1
        if self.on_login_read:
            self.on_login_read(self)
        return {'schema': LOGIN_SCHEMA, 'login_id': self.login_id, 'label': 'Login ' + self.login_id.upper(),
                'mode': 'main' if self.main else 'followers_only',
                'page': {'state': 'authorized', 'synced': True, 'logged_in': self.logged_in, 'problems': []},
                'observed_at': 0, 'accounts': list(self.accounts)}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def short_account(name, root='MNQ', contract='MNQZ1', net=-2, working=(), role='follower'):
    return {'name': name, 'id': 9, 'configured': role is not None, 'role': role,
            'positions': [{'contract': contract, 'root': root, 'net': net}], 'working': list(working)}


# ----------------------------------------------------------------------------------------
# Config (no Soup, no daemon)
# ----------------------------------------------------------------------------------------

class MultiLoginConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fml-cfg-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.reg = write_registry(self.tmp, 'http://127.0.0.1:8777', 'http://127.0.0.1:8778')

    def cfg(self, **over):
        c = {'soup_db': '/x/engine.sqlite', 'soup_engine_dir': '/x/engine', 'ticker': 'MNQZ2021',
             'firing_dir': '/y/firing', 'firing_order': ['aa', 'ac', 'ab'], 'logins_file': self.reg,
             'accounts': {'aa': {'role': 'main'}, 'ac': {'role': 'follower'},
                          'ab': {'role': 'follower', 'login': 'b'}}}
        c.update(over)
        return c

    def test_logins_place_accounts_and_default_to_the_primary(self):
        self.assertEqual(validate(self.cfg()), ('aa', ['ac', 'ab']))
        r = resolve_logins(self.cfg())
        self.assertEqual((r['primary'], r['assign']), ('a', {'aa': 'a', 'ac': 'a', 'ab': 'b'}))
        self.assertTrue(multi_login(self.cfg()))
        c = self.cfg()
        del c['logins_file']
        self.assertTrue(multi_login(c))  # a `login` key alone turns it on (registry from TVBRIDGE_LOGINS/default)
        del c['accounts']['ab']['login']
        self.assertFalse(multi_login(c))

    def test_refusals(self):
        def acct(key, **fields):
            c = self.cfg()
            c['accounts'][key].update(fields)
            return c
        bad = [
            (acct('ab', login='c'), "login 'c' is not in the login registry"),
            (acct('ab', login='B'), 'login must be a login id'),
            (acct('aa', login='b'), 'main account must be on the primary login'),
            (self.cfg(cross_login_hedge_guard='yes'), 'cross_login_hedge_guard'),
            (self.cfg(hedge_groups=['MNQ']), 'hedge_groups'),
            (self.cfg(logins_file=''), 'logins_file'),
            (self.cfg(bridge_url='http://127.0.0.1:9999'), "primary login's bridge_url"),
            (self.cfg(logins_file=str(self.tmp / 'nope.json')), "login 'b' is not in the login registry"),
            (self.cfg(peer_logins=[]), 'unknown config keys'),
        ]
        broken = self.tmp / 'broken.json'
        broken.write_text('{')
        bad.append((self.cfg(logins_file=str(broken)), 'logins_file'))
        for c, text in bad:
            with self.assertRaises(FiringConfigError, msg=text) as ctx:
                validate(c)
            self.assertIn(text, str(ctx.exception))
        self.assertEqual(validate(self.cfg(bridge_url='http://127.0.0.1:8777/')), ('aa', ['ac', 'ab']))
        self.assertEqual(validate(self.cfg(bridge_url='http://localhost:8777')), ('aa', ['ac', 'ab']))
        self.assertEqual(validate(self.cfg(bridge_url='HTTP://LOCALHOST:8777/')), ('aa', ['ac', 'ab']))
        for url in ('https://127.0.0.1:8777', 'http://127.0.0.1:8778', 'http://127.0.0.2:8777'):
            with self.assertRaises(FiringConfigError, msg=url):
                validate(self.cfg(bridge_url=url))
        with self.assertRaises(FiringConfigError):
            validate(self.cfg(peer_hedge_scope='peers'))
        self.assertEqual(validate(self.cfg(peer_hedge_scope='all')), ('aa', ['ac', 'ab']))

    def test_without_logins_the_registry_is_never_read(self):
        c = self.cfg()
        del c['logins_file']
        del c['accounts']['ab']['login']
        with mock.patch('tvbridge.logins.load_logins', side_effect=AssertionError('registry read')):
            self.assertEqual(validate(c), ('aa', ['ac', 'ab']))

    def test_multilogin_example_is_valid_with_its_login_b_follower_disabled(self):
        example = json.loads((Path(__file__).resolve().parent.parent / 'firing.multilogin.example.json').read_text())
        example['logins_file'] = self.reg
        self.assertEqual(validate(example), ('account-a', []))
        self.assertEqual(resolve_logins(example)['assign']['login-b-1'], 'b')
        plain = json.loads((Path(__file__).resolve().parent.parent / 'firing.example.json').read_text())
        extra = set(example) - set(plain)
        self.assertEqual(extra, {'logins_file', 'cross_login_hedge_guard', 'hedge_groups', 'peer_hedge_scope'})

    def test_check_cli_reports_logins(self):
        import io
        from contextlib import redirect_stdout
        p = self.tmp / 'firing.json'
        p.write_text(json.dumps(self.cfg()))
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch('logging.basicConfig'):
            firing_mod.main(['--config', str(p), '--check'])
        out = json.loads(buf.getvalue())
        self.assertEqual((out['ok'], out['logins']), (True, {'aa': 'a', 'ac': 'a', 'ab': 'b'}))

    def test_check_cli_fails_without_starting(self):
        import io
        from contextlib import redirect_stdout
        cases = [self.cfg(logins_file=str(self.tmp / 'nope.json')),                 # ab's login b missing
                 dict(self.cfg(), accounts=dict(self.cfg()['accounts'], aa={'role': 'main', 'login': 'b'})),
                 self.cfg(bridge_url='http://127.0.0.1:9999')]
        broken = self.tmp / 'broken.json'
        broken.write_text('{')
        cases.append(self.cfg(logins_file=str(broken)))
        for n, cfg in enumerate(cases):
            p = self.tmp / f'firing-{n}.json'
            p.write_text(json.dumps(cfg))
            buf = io.StringIO()
            with self.subTest(n=n), redirect_stdout(buf), mock.patch('logging.basicConfig'), \
                    mock.patch.object(firing_mod, 'Firing', side_effect=AssertionError('started')):
                with self.assertRaises(SystemExit) as ctx:
                    firing_mod.main(['--config', str(p), '--check'])
                self.assertIn('config invalid', str(ctx.exception.code))
                self.assertEqual(buf.getvalue(), '')


# ----------------------------------------------------------------------------------------
# LoginReports against two fake daemons (no Soup)
# ----------------------------------------------------------------------------------------

class LoginReportsHttp(unittest.TestCase):
    def setUp(self):
        self.a = FakeDaemon(TOKEN, 'a', ['ac', 'ab'], main=True)  # ab also on a: must not count there
        self.b = FakeDaemon(TOKEN_B, 'b', ['ab'])
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def reports(self, token_b=TOKEN_B, url_b=None, poll=True, **kw):
        links = [LoginLink({'id': 'a', 'label': 'Login A', 'bridge_url': self.a.url}, TOKEN, primary=True),
                 LoginLink({'id': 'b', 'label': 'Login B', 'bridge_url': url_b or self.b.url}, token_b, primary=False)]
        r = LoginReports(links, {'aa': 'a', 'ac': 'a', 'ab': 'b'}, 'a',
                         hedge_members=('ES', 'MES', 'NQ', 'MNQ', 'YM', 'MYM', 'RTY', 'M2K'), **kw)
        if poll:
            r.poll_once()  # what the background poller does every second
        return r

    def test_merged_report_keeps_each_row_on_its_own_login(self):
        self.a.rows['ab']['net_position'] = 99
        data = self.reports().get()
        self.assertEqual(data['main']['key'], 'main')
        self.assertEqual([(r['key'], r['login_id'], r['net_position']) for r in data['followers']],
                         [('ac', 'a', 0), ('ab', 'b', 0)])
        self.assertEqual(data['logins'], {'a': {'state': 'CONNECTED'}, 'b': {'state': 'CONNECTED'}})
        self.b.logged_in = False
        self.assertEqual(self.reports().get()['logins']['b'], {'state': 'LOGGED OUT'})

    def test_one_login_down_is_reported_both_down_raises(self):
        data = self.reports(url_b='http://127.0.0.1:%d' % dead_port()).get()
        self.assertEqual([r['key'] for r in data['followers']], ['ac'])
        self.assertEqual(data['logins']['b']['state'], 'UNREACHABLE')
        # Login b's daemon answers only its own token.
        self.assertEqual(self.reports(token_b=TOKEN).get()['logins']['b']['state'], 'UNREACHABLE')
        self.a.close()
        data = self.reports().get()  # primary down: no main row, login b still served
        self.assertNotIn('main', data)
        self.assertEqual(data['logins']['a']['state'], 'UNREACHABLE')
        self.b.close()
        with self.assertRaises(OSError):
            self.reports().get()

    def test_login_down_and_back(self):
        r = self.reports()
        self.assertIsNone(r.login_down('b'))
        self.b.logged_in = False
        self.assertIsNone(r.login_down('b'), 'the cache is read, not the daemon')
        r.poll_once()
        self.assertEqual(r.login_down('b'), 'login_down:b')
        self.b.logged_in = True
        r.poll_once()
        self.assertIsNone(r.login_down('b'))

    def test_only_a_definite_logged_out_answer_is_down(self):
        with self.assertLogs('tvbridge.firing', 'WARNING') as logs:
            self.assertIsNone(self.reports(url_b='http://127.0.0.1:%d' % dead_port()).login_down('b'))
            self.assertIsNone(self.reports(token_b='short').login_down('b'))
            self.assertIsNone(self.reports(poll=False).login_down('b'))
        self.assertIn('login b state unknown', logs.output[0])
        self.assertNotIn(TOKEN_B, ''.join(logs.output))
        self.b.logged_in = False
        r = self.reports()
        r.cache['b'] = (r.cache['b'][0], dict(r.cache['b'][1], page={'logged_in': False,
                                                                      'problems': ['page_status_unknown']}), None)
        self.assertIsNone(r.login_down('b'), 'the daemon does not know its page yet')
        r = self.reports()
        r.clock = lambda: time.monotonic() + 11
        self.assertIsNone(r.login_down('b'), 'stale')

    def test_primary_login_is_never_down(self):
        self.a.logged_in = False
        r = self.reports()
        self.assertIsNone(r.login_down('a'))
        self.a.close()
        self.assertIsNone(self.reports().login_down('a'))

    def test_cross_login_hedge_classification(self):
        r = self.reports()
        self.assertIsNone(r.hedge_conflict('a', 'long'))
        cases = [
            ([short_account('BX-1')], 'long', 'hedge_conflict_login:b:MNQ'),
            ([short_account('BX-1')], 'short', None),                                 # same side
            ([short_account('BX-2', root='ES', contract='ESZ1')], 'long', 'hedge_conflict_login:b:ES'),
            ([short_account('BX-3', root='CL', contract='CLZ1')], 'long', None),      # other hedge group
            ([short_account('BX-4', net=2, working=[{'contract': 'MNQZ1', 'root': 'MNQ', 'side': 'Sell',
                                                      'qty': 2}])], 'long', None),     # protective stop
            ([short_account('BX-5', net=0, working=[{'contract': 'MNQZ1', 'root': 'MNQ', 'side': 'Sell',
                                                      'qty': 1}])], 'long', 'hedge_conflict_login:b:MNQ'),
            ([short_account('BX-6', role='main')], 'long', 'hedge_conflict_login:b:MNQ'),
            ([short_account('DEMO-UNASSIGNED', role=None)], 'long', None),         # scope "configured"
        ]
        for accounts, side, want in cases:
            self.b.accounts = accounts
            r.poll_once()
            self.assertEqual(r.hedge_conflict('a', side), want, msg=accounts[0]['name'])
        self.b.accounts = [short_account('BX-1')]
        r.poll_once()
        self.assertIsNone(r.hedge_conflict('b', 'long'))  # only OTHER logins are read
        self.assertIsNone(self.reports(hedge_guard=False).hedge_conflict('a', 'long'))
        self.b.accounts = [short_account('DEMO-UNASSIGNED', role=None)]
        self.assertEqual(self.reports(scope='all').hedge_conflict('a', 'long'), 'hedge_conflict_login:b:MNQ')

    def test_the_check_reads_the_cache_only(self):
        self.b.accounts = [short_account('BX-1')]
        r = self.reports()
        reads = self.b.login_reads
        for _ in range(5):
            self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_conflict_login:b:MNQ')
            r.login_down('b')
        self.assertEqual(self.b.login_reads, reads)
        r.clock = lambda: time.monotonic() + 11  # no poll for over 10 s: stale, fail closed
        with self.assertLogs('tvbridge.firing', 'WARNING') as logs:
            self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_unknown_login:b:stale')
        self.assertIn('unreachable (stale)', logs.output[0])

    def test_unknown_contract_or_bad_report_fails_closed_with_a_warning(self):
        self.b.accounts = [short_account('BX-1', root=None, contract=None)]
        r = self.reports()
        with self.assertLogs('tvbridge.firing', 'WARNING') as logs:
            self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_conflict_login:b:unknown')
            self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_conflict_login:b:unknown')
            self.assertIsNone(r.hedge_conflict('a', 'short'))
            logging.getLogger('tvbridge.firing').warning('marker')
        self.assertEqual(len(logs.records), 2)
        self.assertIn('unknown contract', logs.output[0])
        self.b.accounts = [dict(short_account('BX-1'), positions=7)]  # not a list: account_conflict raises
        r = self.reports()
        with self.assertLogs('tvbridge.firing', 'WARNING') as logs:
            self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_unknown_login:b:check_failed')
        self.assertIn('check failed (TypeError)', logs.output[0])

    def test_unreachable_or_logged_out_login_fails_closed_with_one_warning_a_minute(self):
        r = self.reports(url_b='http://127.0.0.1:%d' % dead_port())
        with self.assertLogs('tvbridge.firing', 'WARNING') as logs:
            self.assertTrue(r.hedge_conflict('a', 'long').startswith('hedge_unknown_login:b:'))
            self.assertTrue(r.hedge_conflict('a', 'short').startswith('hedge_unknown_login:b:'))
            logging.getLogger('tvbridge.firing').warning('marker')
        self.assertEqual(len(logs.records), 2)
        self.assertIn('login b unreachable', logs.output[0])
        self.assertNotIn(TOKEN, ''.join(logs.output))
        self.b.accounts, self.b.logged_in = [short_account('BX-1')], False
        self.assertEqual(self.reports().hedge_conflict('a', 'long'), 'hedge_unknown_login:b:logged_out')

    def test_login_reads_use_the_read_token(self):
        read_b = 'login-b-read-token-0123456789abcdef'
        b = FakeDaemon(TOKEN_B, 'b', ['ab'], read_token=read_b)
        self.addCleanup(b.close)
        b.accounts = [short_account('BX-1')]
        link = LoginLink({'id': 'b', 'label': 'Login B', 'bridge_url': b.url}, TOKEN_B, primary=False,
                         read_token=read_b)
        r = LoginReports([LoginLink({'id': 'a', 'bridge_url': self.a.url}, TOKEN, primary=True), link],
                         {'aa': 'a', 'ab': 'b'}, 'a', hedge_members=('MNQ',))
        r.poll_once()
        self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_conflict_login:b:MNQ')
        self.assertIn(('GET', 'login', 'read'), b.paths)
        self.assertNotIn(('GET', 'login'), b.paths)
        self.assertEqual(r.fetch('b')['followers'][0]['key'], 'ab')  # /followers: the order token


# ----------------------------------------------------------------------------------------
# Lanes against Soup's real runtime and two fake daemons
# ----------------------------------------------------------------------------------------

class MultiLoginLanes(SoupSide, unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fml-lanes-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.offset = 0
        self.main_route = Recorder()
        self.soup_setup(self.tmp, self.main_route, self.now)
        self.a = FakeDaemon(TOKEN, 'a', ['ac'], main=True)
        self.b = FakeDaemon(TOKEN_B, 'b', ['ab'])
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def now(self):
        return time.time_ns() // 1_000_000 + self.offset

    def firing_cfg(self, url_b=None, **over):
        c = {'soup_db': str(self.soup_db), 'soup_engine_dir': str(SOUP / 'engine'),
             'soup_quantity_policy': str(self.tmp / 'policy.json'), 'soup_pairing': str(self.tmp / 'pairing.json'),
             'ticker': CONFIG['ticker'], 'bridge_url': self.a.url, 'firing_dir': str(self.tmp / 'firing'),
             'logins_file': write_registry(self.tmp, self.a.url, url_b or self.b.url),
             'firing_order': ['aa', 'ac', 'ab'],
             'accounts': {'aa': {'role': 'main'}, 'ac': {'role': 'follower'},
                          'ab': {'role': 'follower', 'login': 'b'}},
             'reconcile_grace_ms': 300, 'reconcile_settle_ms': 300, 'reconcile_deadline_ms': 2000,
             'main_gate_timeout_ms': 3000, 'align_wait_ms': 600}
        c.update(over)
        return c

    def firing(self, cfg=None, **over):
        f = Firing(cfg or self.firing_cfg(**over), TOKEN, clock_ms=self.now, env={'TVBRIDGE_TOKEN_B': TOKEN_B})
        self.addCleanup(f.close)
        return f

    def poll(self, f):
        if f.logins is not None:
            f.logins.poll_once()  # the background /login poller's pass (Firing.start runs it every second)
        for lane in f.lanes.values():
            lane.poll_once()

    def entry_skip(self, key, trade_id=None):
        return next((r for r in records(self.tmp / 'firing' / key / JOURNAL_NAME)
                     if r.get('kind') == 'entry' and (trade_id is None or r.get('trade_id') == trade_id)
                     and r.get('type') == 'skipped'), None)

    def sent(self, daemon, key):
        return [body for k, body in daemon.orders if k == key]

    def main_payloads(self):
        return [json.loads(p) for p in self.main_route.sent]

    def test_each_lane_uses_its_own_logins_daemon_and_token(self):
        f = self.firing()
        self.assertEqual(f.lanes['ac'].transport.url, f'{self.a.url}/follower/{TOKEN}/ac/order')
        self.assertEqual(f.lanes['ab'].transport.url, f'{self.b.url}/follower/{TOKEN_B}/ab/order')
        self.assertEqual(f.lanes['ab'].bridge_state.url, f'{self.b.url}/follower/{TOKEN_B}/ab/state')
        self.poll(f)
        self.enter_soup('long')
        self.poll(f)
        self.assertEqual(self.sent(self.a, 'ac'), self.main_payloads())  # entry and TP1, Soup's own
        self.assertEqual(self.sent(self.b, 'ab'), self.main_payloads())
        self.assertEqual(len(self.main_payloads()), 2)
        self.assertEqual(([k for k, _ in self.a.orders if k == 'ab'], [k for k, _ in self.b.orders if k == 'ac']),
                         ([], []))
        self.assertTrue(f.lanes['ab']._native_kill('test'))  # its own kill switch, on its own daemon
        self.assertEqual((self.a.kills, self.b.kills), ([], ['ab']))
        self.assertGreater(self.a.login_reads + self.b.login_reads, 0)  # the cross-login check ran both ways
        status = f.status()
        self.assertEqual((status['lanes']['ac']['login'], status['lanes']['ab']['login']), ('a', 'b'))
        self.assertEqual([(x['id'], x['accounts']) for x in status['logins']],
                         [('a', ['aa', 'ac']), ('b', ['ab'])])

    def test_follower_missing_from_its_own_logins_report_is_refused_alone(self):
        del self.b.rows['ab']
        self.a.rows['ab'] = dict(self.a.rows['ac'], key='ab')  # on the wrong login: does not count
        f = self.firing()
        self.assertEqual((list(f.lanes), f.unavailable), (['ac'], {'ab': 'not_configured_in_daemon'}))
        self.assertEqual([(a['account'], a['kind'], a['reason']) for a in records(self.tmp / 'firing' / ALERTS_NAME)],
                         [('ab', 'follower_unavailable', 'not_configured_in_daemon')])

    def test_login_without_a_token_refuses_only_its_lanes(self):
        f = Firing(self.firing_cfg(), TOKEN, clock_ms=self.now, env={})
        self.addCleanup(f.close)
        self.assertEqual((list(f.lanes), f.unavailable), (['ac'], {'ab': 'login_token_missing:b'}))

    def test_primary_login_lanes_never_check_login_down(self):
        f = self.firing()
        asked, real = [], f.logins.login_down
        f.logins.login_down = lambda lid: asked.append(lid) or real(lid)
        self.a.logged_in = False  # the leader was accepted on login a: that proves the login
        self.poll(f)
        self.enter_soup('long')
        self.poll(f)
        self.assertEqual(self.sent(self.a, 'ac'), self.main_payloads())
        self.assertIsNone(self.entry_skip('ac'))
        self.assertEqual(asked, ['b'])  # login b's lane only
        self.assertIsNone(real('a'))

    def test_logged_out_login_skips_its_lanes_and_fails_the_hedge_check_closed(self):
        f = self.firing()
        self.poll(f)
        self.b.logged_in = False
        self.enter_soup('long')
        self.poll(f)
        # login b's exposure is unknown while it is logged out, so login a's follower skips too
        self.assertEqual(self.sent(self.a, 'ac'), [])
        self.assertEqual(self.entry_skip('ac')['reason'], 'hedge_unknown_login:b:logged_out')
        self.assertEqual(self.sent(self.b, 'ab'), [])
        self.assertEqual(self.entry_skip('ab')['reason'], 'login_down:b')
        self.assertIn(('ab', 'entry_not_fired', 'login_down:b'),
                      [(a['account'], a['kind'], a.get('reason')) for a in records(self.tmp / 'firing' / ALERTS_NAME)])
        self.assertTrue(f.lanes['ab'].entry_skipped(self.trade['id']))
        f.update_grid()
        self.assertIn('b: Login B  LOGGED OUT\nAB', f.grid.status()['now'])
        # Soup's hard stop: neither follower entered, so neither lane has anything to close.
        self.assertEqual(self.quote(self.trade['payload']['initial_hard_stop_price'])[0], 'processed')
        self.poll(f)
        self.assertEqual(self.sent(self.a, 'ac'), [])
        self.assertEqual(self.sent(self.b, 'ab'), [])

    def test_login_signed_back_in_fires_on_the_next_signal_without_a_restart(self):
        self.b.logged_in = False
        f = self.firing()
        self.poll(f)
        f.update_grid()
        self.assertIn('b: Login B  LOGGED OUT', f.grid.status()['now'])
        self.assertEqual(f.lanes['ab'].login.down(), 'login_down:b')
        self.b.logged_in = True  # the operator signs login b in again; nothing in the firing service is latched
        self.enter_soup('long')
        self.poll(f)
        self.assertEqual(self.sent(self.b, 'ab'), self.main_payloads())
        self.assertIsNone(self.entry_skip('ab'))
        f.update_grid()
        self.assertIn('b: Login B  CONNECTED', f.grid.status()['now'])

    def test_unreachable_login_does_not_skip_on_login_state_and_the_grid_says_so(self):
        f = self.firing(url_b='http://127.0.0.1:%d' % dead_port())
        self.assertEqual(sorted(f.lanes), ['ab', 'ac'])  # unreachable at start: refuses nothing
        with self.assertLogs('tvbridge.firing', 'WARNING') as logs:
            self.poll(f)
            self.enter_soup('long')
            self.poll(f)
        # login b's exposure is unknown, so ac's cross-login check fails closed: no entry
        self.assertEqual(self.sent(self.a, 'ac'), [])
        self.assertTrue(self.entry_skip('ac')['reason'].startswith('hedge_unknown_login:b:'))
        # Not `login_down`: the lane goes on and its own daemon (gone) cannot confirm anything, so
        # nothing is sent; an order to a daemon that is gone fails safely.
        self.assertEqual(self.entry_skip('ab')['reason'], 'align_timeout:follower_state_unavailable')
        self.assertTrue(any('login b state unknown' in line for line in logs.output))
        f.update_grid()
        self.assertIn('b: Login B  UNREACHABLE\nAB    ?    -        UNREACHABLE', f.grid.status()['now'])

    def test_opposite_exposure_on_another_login_skips_the_entry(self):
        self.b.accounts = [short_account('BX-1')]
        f = self.firing()
        self.poll(f)
        self.enter_soup('long')
        self.poll(f)
        self.assertEqual(self.sent(self.a, 'ac'), [])
        self.assertEqual(self.entry_skip('ac')['reason'], 'hedge_conflict_login:b:MNQ')
        self.assertEqual(f.lanes['ac'].status['last_alignment']['result'], 'hedge_conflict_login:b:MNQ')
        self.assertEqual(self.sent(self.b, 'ab'), self.main_payloads())  # login a shows nothing opposite
        self.assertEqual([p['action'] for p in self.main_payloads()], ['buy', 'exit'])  # leader unaffected

    def test_cross_login_guard_off_fires(self):
        self.b.accounts = [short_account('BX-1')]
        with self.assertRaises(FiringConfigError):   # false alone is refused
            validate(self.firing_cfg(cross_login_hedge_guard=False))
        f = self.firing(cross_login_hedge_guard=False, hedge_guard_off_acknowledged=True)
        self.poll(f)
        self.enter_soup('long')
        self.poll(f)
        self.assertEqual(self.sent(self.a, 'ac'), self.main_payloads())
        self.assertEqual(self.a.login_reads, 0)  # login b is read only for its own lanes' login state
        self.assertEqual([link.id for link in f.logins.watched], ['b'])

    def test_opposite_exposure_that_clears_within_align_wait_lets_the_entry_go(self):
        self.b.accounts = [short_account('BX-1')]
        f = self.firing(align_wait_ms=3000)
        f.logins.poll_s = 0.05
        f.logins.start()  # the background poller; the align loop reads only its cache
        self.addCleanup(f.logins.stop)
        self.poll(f)
        self.enter_soup('long')
        # The other login's follower finishes aligning 0.4 s into this lane's wait.
        timer = threading.Timer(0.4, lambda: setattr(self.b, 'accounts', []))
        timer.start()
        self.addCleanup(timer.cancel)
        self.poll(f)
        self.assertEqual(self.sent(self.a, 'ac'), self.main_payloads())
        self.assertEqual(f.lanes['ac'].status['last_alignment']['result'], 'aligned')
        self.assertGreaterEqual(f.lanes['ac'].status['last_alignment']['waited_ms'], 300)

    def test_firing_reads_login_b_with_its_read_token(self):
        read_a, read_b = 'login-a-read-token-0123456789abcdef', 'login-b-read-token-0123456789abcdef'
        self.a.read_token, self.b.read_token = read_a, read_b
        cfg = self.firing_cfg()
        cfg['logins_file'] = write_registry(self.tmp, self.a.url, self.b.url, name='logins-read.json',
                                            read_tokens=True)
        f = Firing(cfg, TOKEN, clock_ms=self.now, env={'TVBRIDGE_TOKEN_B': TOKEN_B, 'TVBRIDGE_READ_TOKEN_A': read_a,
                                                        'TVBRIDGE_READ_TOKEN_B': read_b})
        self.addCleanup(f.close)
        self.poll(f)
        self.assertIn(('GET', 'login', 'read'), self.b.paths)
        self.assertIn(('GET', 'login', 'read'), self.a.paths)
        self.assertNotIn(('GET', 'login'), self.b.paths + self.a.paths)
        self.enter_soup('long')
        self.poll(f)
        self.assertEqual(self.sent(self.b, 'ab'), self.main_payloads())  # orders: login b's order token

    def test_grid_groups_by_login_and_one_login_keeps_todays_text(self):
        f = self.firing()
        f.update_grid()
        self.assertEqual(f.grid.status()['now'], 'ACCT  POS  ORDERS   STATE\n'
                                                 'a: Login A  CONNECTED\n'
                                                 'AA    0    -        OK\n'
                                                 'AC    0    -        OK\n'
                                                 'b: Login B  CONNECTED\n'
                                                 'AB    0    -        OK')
        f.close()
        # Both followers on login a (the registry still lists b): exactly the one-login grid.
        self.a.rows['ab'] = dict(self.a.rows['ac'], key='ab')
        one = self.firing(accounts={'aa': {'role': 'main', 'login': 'a'}, 'ac': {'role': 'follower'},
                                    'ab': {'role': 'follower'}})
        one.update_grid()
        multi_text = one.grid.status()['now']
        self.assertEqual(one.logins.polled[0].id, 'a')
        self.assertEqual(len(one.logins.polled), 1)
        one.close()
        cfg = self.firing_cfg(accounts={'aa': {'role': 'main'}, 'ac': {'role': 'follower'},
                                        'ab': {'role': 'follower'}})
        del cfg['logins_file']
        plain = self.firing(cfg)
        self.assertIsNone(plain.logins)
        plain.update_grid()
        self.assertEqual(multi_text, plain.grid.status()['now'])
        self.assertEqual(plain.grid.status()['now'], 'ACCT  POS  ORDERS   STATE\nAA    0    -        OK\n'
                                                     'AC    0    -        OK\nAB    0    -        OK')
        self.assertNotIn('logins', plain.status())
        self.assertNotIn('login', plain.status()['lanes']['ac'])
        self.assertIsNone(plain.lanes['ac'].login)


if __name__ == '__main__':
    unittest.main()
