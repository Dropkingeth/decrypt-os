"""Soup -> bridge mirror (tvbridge.mirror) against Soup's real runtime and storage.

Soup's own runtime.Service (engine, SQLite storage, _dispatch and message()) runs in a
temporary workspace. Its primary transport stands in for a webhook relay: in the browser
tests it is a bridge Executor on the primary account (101) whose snapshots are fed back
through Service like the live broker adapter. The mirror polls Soup's database read-only
and sends to a real bridge daemon handler whose Executor trades the mirror account (117)
on the same loopback fake exchange.

Clock: Soup, both executors, the fake exchange and the mirror share one clock (real time
+ FakeTradovate.skew_ms), pinned to 11:00 New York, as in test_replay.
"""
import json
import logging
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

from harness import Harness  # also puts the repository on sys.path
from soup_inputs import engine_db, named  # synthetic Soup input names -> the package's own

SOUP = Path(os.environ.get('TVBRIDGE_SOUP_PACKAGE',
                           ''))
if not (SOUP / 'engine' / 'core.py').is_file():  # the optional engine package is not bundled
    raise unittest.SkipTest('optional engine package not found; set TVBRIDGE_SOUP_PACKAGE')
for sub in ('engine', 'tests', 'tests/support'):
    sys.path.insert(0, str(SOUP / sub))
import test_pair as soup_fixtures  # noqa: E402
from runtime import Service  # noqa: E402
from storage import canonical  # noqa: E402

from tvbridge.bridge import Bridge  # noqa: E402
from tvbridge.daemon import Publisher, account_state, make_handler  # noqa: E402
from tvbridge.executor import Executor  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.mirror import JOURNAL_NAME, STATUS_NAME, Mirror, SoupReader  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402
from tvbridge.protocol import tradovate_symbol  # noqa: E402

logging.getLogger('tvbridge.mirror').addHandler(logging.NullHandler())  # keep test output readable

CONFIG = soup_fixtures.CONFIG  # the package's dated MNQ contract
SYMBOL = tradovate_symbol(CONFIG['ticker'])  # its Tradovate name
PRIMARY, MIRROR_ACCOUNT, CONTRACT = 101, 117, 556
LIMIT = 10
POLICY = {'id': 'account-original-allocation-micro-cap-v1', 'account_id': CONFIG['account_id'],
          'max_contracts': LIMIT, 'exit_allocation': 'original',
          'native_binding': {'broker_id': 'tvbridge-offline', 'account_value': 'DEMO-1',
                             'account_label': 'Offline fake exchange', 'account_url_sha256': '0' * 64}}
TOKEN = 'mirror-test-token-0123456789abcdef'
NY = ZoneInfo('America/New_York')
# Synthetic settings for this suite: 5 contracts = TP1 2 + parent runners 2 + EOD runner 1, stop 35,
# TP1 22 with its working limit 1.25 inside, break-even +9. Prices are rebased to BASE.
BASE = 10000.0
FIXTURE = {'initial_hard_stop_handles': 35, 'tp1_handles': 22, 'be_offset_handles': 9, 'fixture_int_07': 2,
           'fixture_int_06': 2, 'fixture_int_01': 0, 'fixture_int_04': 1}
QTY, TP1_QTY = 5, 2
RUNNERS = QTY - TP1_QTY
TP1_OFFSET = 1.25
PRICE_FIELDS = ('entry_price', 'tp1_price', 'break_even_price', 'runner_cbdr_target', 'initial_hard_stop_price',
                'catastrophe_stop_price', 'effective_pre_tp1_stop_price')


def entry_payload(side, changes=None, at=None):
    """The engine fixture's entry with this suite's settings; every price moved onto BASE."""
    p = soup_fixtures.fixture(side, named(soup_fixtures, dict(FIXTURE, **(changes or {}))), **({} if at is None else {'at': at}))[0]
    shift = BASE - p['entry_price']
    for k in PRICE_FIELDS:
        if p[k]:
            p[k] += shift
    return p


def until(fn, timeout=10.0, what='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(what + ' not reached in time')


def dead_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def eod_start():
    """(hour, minute) New York where the fixture's EOD session starts ('HHMM-HHMM' input)."""
    def find(node):
        if isinstance(node, dict):
            if isinstance(node.get('eod_session'), str):
                return node['eod_session']
            node = list(node.values())
        if isinstance(node, list):
            for value in node:
                found = find(value)
                if found:
                    return found
        return None
    session = find(CONFIG) or find(soup_fixtures.fixture('long', at=int(time.time()) // 5 * 5000)[0])
    start = session.split('-')[0]
    return int(start[:2]), int(start[2:])


def before_eod(seconds):
    """(hour, minute, second) New York `seconds` before the fixture's EOD session starts."""
    hour, minute = eod_start()
    t = datetime(2000, 1, 3, hour, minute) - timedelta(seconds=seconds)
    return t.hour, t.minute, t.second


class Recorder:
    """Soup's primary transport stand-in (webhook relay): records the exact payload Soup sends."""

    def __init__(self, inner=None, outcome='accepted'):
        self.inner, self.outcome = inner, outcome
        self.sent = []

    def send(self, payload):
        self.sent.append(canonical(payload))
        if self.inner is not None:
            return self.inner.send(payload)
        return self.outcome, {'capture_only': True}


class SoupSide:
    """Soup's real runtime.Service in a temporary workspace, driven with an explicit clock."""

    def soup_setup(self, tmp, transport, clock):
        self.clock = clock
        self.workspace = tmp / 'soup'
        self.workspace.mkdir()
        (self.workspace / 'control.json').write_text('{"enabled":true,"locked":false}')
        self.primary_transport = transport
        self.service = Service(self.workspace, CONFIG, transport, tp1_limit_offset_handles=TP1_OFFSET,
                               quantity_policy=POLICY)
        self.addCleanup(self.service.close)
        self.soup_db = engine_db(self.workspace)
        (tmp / 'policy.json').write_text(json.dumps(POLICY))
        (tmp / 'pairing.json').write_text(json.dumps(CONFIG))

    def mirror_config(self, tmp, bridge_url, **overrides):
        cfg = {'soup_db': str(self.soup_db), 'soup_engine_dir': str(SOUP / 'engine'),
               'soup_quantity_policy': str(tmp / 'policy.json'), 'soup_pairing': str(tmp / 'pairing.json'),
               'ticker': CONFIG['ticker'], 'mirror_dir': str(tmp / 'mirror'), 'bridge_url': bridge_url,
               'reconcile_grace_ms': 300, 'reconcile_settle_ms': 300, 'reconcile_deadline_ms': 20000}
        cfg.update(overrides)
        return cfg

    @property
    def engine(self):
        return self.service.engine

    @property
    def trade(self):
        return self.engine.data['trade']

    def command(self, kind):
        return next(c for c in reversed(list(self.engine.data['commands'].values())) if c['kind'] == kind)

    def soup(self, channel, p):
        """Deliver one message through Soup's inbox, as its HTTP receiver does, then process it."""
        now = self.clock()
        seq = self.service.enqueue(channel, canonical(p).encode(), now)
        self.service.process_one(now)
        return self.engine.db.execute('SELECT status, detail FROM inbox WHERE seq=?', (seq,)).fetchone()

    def enter_soup(self, side, changes=None):
        entry = entry_payload(side, changes, at=self.clock() // 5000 * 5000)
        self.assertEqual(self.soup('pine', entry), ('processed', 'entry_accepted'))
        return entry

    def quote(self, price):
        last = self.engine.data['last_quote']
        at = self.clock()
        if last and at <= last['time']:
            at = last['time'] + 1
        return self.soup('quote', {'event_id': f'q{at}', 'symbol': CONFIG['ticker'], 'time': at, 'price': price})

    def started_ids(self):
        return [c['id'] for c in self.engine.data['commands'].values() if c['started_at'] is not None]

    # --- mirror side ---------------------------------------------------------------

    def records(self, mirror_dir=None):
        path = Path(mirror_dir or self.mirror_dir) / JOURNAL_NAME
        if not path.exists():
            return []
        out = []
        for line in path.read_text().split('\n'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass  # empty or still being written
        return out

    def by_type(self, kind):
        return [r for r in self.records() if r['type'] == kind]

    def outcomes(self):
        return {r['command_id']: r for r in self.by_type('outcome')}

    def settled(self, command_id):
        """The mirror has a final record for this Soup command (outcome or skip)."""
        return any(r.get('command_id') == command_id and r['type'] != 'intent' for r in self.records())

    def caught_up(self, timeout=10):
        until(lambda: all(self.settled(cid) for cid in self.started_ids()), timeout, 'mirror caught up')

    def final_reconcile(self, trade_id, timeout=15):
        return until(lambda: next((r for r in self.by_type('reconcile')
                                   if r['trade_id'] == trade_id and r['final']), None), timeout,
                     'final reconciliation of ' + trade_id)


# ----------------------------------------------------------------------------------------
# Browser-free: Soup's runtime and storage with capture transports.
# ----------------------------------------------------------------------------------------

class SoupOnly(SoupSide, unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='tvbridge-mirror-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.offset = 0
        self.soup_setup(self.tmp, Recorder(), lambda: time.time_ns() // 1_000_000 + self.offset)
        self.mirror_dir = self.tmp / 'mirror'

    def mirror(self, transport=None, **overrides):
        url = 'http://127.0.0.1:%d' % dead_port()
        m = Mirror(self.mirror_config(self.tmp, url, **overrides), TOKEN, clock_ms=self.clock,
                   transport=transport)
        self.addCleanup(m.close)
        return m

    def test_d1_startup_baseline_is_never_replayed(self):
        self.enter_soup('long')
        started = self.started_ids()
        self.assertEqual([self.engine.data['commands'][c]['kind'] for c in started], ['entry', 'tp1_limit'])
        sent = Recorder()
        m = self.mirror(sent)
        m.poll_once()
        self.assertEqual({r['command_id']: r['type'] for r in self.records()},
                         {c: 'baseline_skipped' for c in started})
        # Soup keeps going: the protective cancel barrier of a trade the mirror never entered.
        self.assertEqual(self.quote(self.trade['payload']['initial_hard_stop_price'])[0], 'processed')
        cancel = self.command('cancel')
        self.assertIsNotNone(cancel['started_at'])
        for _ in range(5):
            m.poll_once()
        self.assertEqual(sent.sent, [])
        skip = next(r for r in self.records() if r['command_id'] == cancel['id'])
        self.assertEqual((skip['type'], skip['reason']), ('skipped', 'entry_not_mirrored'))
        # A second start does not add records for commands it already knows.
        before = len(self.records())
        m.close()
        m2 = self.mirror(sent)
        m2.poll_once()
        self.assertEqual(len(self.records()), before)
        self.assertEqual(sent.sent, [])
        m2._write_status(force=True)
        status = json.loads((self.mirror_dir / STATUS_NAME).read_text())
        self.assertEqual((status['counts']['baseline_skipped'], status['counts']['mirrored']), (2, 0))
        self.assertEqual(status['soup']['trade_id'], self.trade['id'])
        self.assertEqual(os.stat(self.mirror_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.mirror_dir / JOURNAL_NAME).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.mirror_dir / STATUS_NAME).st_mode & 0o777, 0o600)

    def test_d2_stale_commands_first_seen_late_are_skipped(self):
        sent = Recorder()
        m = self.mirror(sent)
        m.poll_once()  # baseline on an idle Soup
        self.enter_soup('long')
        self.offset = 6000  # the mirror first sees them six seconds after Soup started them
        m.poll_once()
        recs = {r['command_id']: r for r in self.records()}
        self.assertEqual(sorted(r['type'] for r in recs.values()), ['stale_skipped', 'stale_skipped'])
        self.assertTrue(all(r['age_ms'] >= 6000 for r in recs.values()))
        self.assertEqual(sent.sent, [])

    def test_a0_payload_matches_soup_primary_bytes_and_latency(self):
        sent = Recorder()
        m = self.mirror(sent)
        m.poll_once()
        self.enter_soup('short')
        m.poll_once()
        self.assertEqual(sent.sent, self.primary_transport.sent)  # entry and TP1, byte for byte
        intents = self.by_type('intent')
        self.assertEqual([canonical(i['payload']) for i in intents], self.primary_transport.sent)
        for r in self.by_type('outcome'):
            self.assertLess(r['latency_ms'], 300, r)

    def test_i_bridge_down_outcomes_recorded_no_crash_no_retry(self):
        # Soup's own route refuses the entry, so Soup closes the trade at once.
        self.primary_transport.outcome = 'rejected'
        m = self.mirror(reconcile_grace_ms=50, reconcile_settle_ms=50, reconcile_deadline_ms=600)
        m.poll_once()
        self.enter_soup('long')
        self.assertEqual(self.trade['phase'], 'closed')
        entry = self.command('entry')
        m.poll_once()
        out = self.outcomes()[entry['id']]
        self.assertEqual((out['outcome'], out['receipt']), ('rejected', {'bridge': 'unreachable'}))
        # Reconciliation cannot read the bridge: it retries until its deadline, then reports.
        deadline = time.monotonic() + 3
        while not m.journal.reconciled and time.monotonic() < deadline:
            m.poll_once()
            time.sleep(0.05)
        final = m.journal.reconciled[self.trade['id']]
        self.assertEqual(final['result'], 'stuck')
        self.assertIn('bridge_state_unavailable', final['reason'])
        self.assertEqual(len(self.by_type('intent')), 1)  # never retried
        m._write_status(force=True)
        status = json.loads((self.mirror_dir / STATUS_NAME).read_text())
        self.assertEqual(status['counts']['rejected'], 1)
        self.assertIn('stuck', status['last_error']['error'])

    def test_i2_unknown_and_crash_mid_send_are_never_retried(self):
        # A bridge that accepts the connection and closes it without an answer.
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(8)
        self.addCleanup(srv.close)

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                conn.recv(65536)
                conn.close()
        threading.Thread(target=serve, daemon=True).start()
        url = 'http://127.0.0.1:%d' % srv.getsockname()[1]
        m = Mirror(self.mirror_config(self.tmp, url), TOKEN, clock_ms=self.clock)
        m.poll_once()
        self.enter_soup('long')
        m.poll_once()
        out = self.outcomes()
        self.assertEqual({r['outcome'] for r in out.values()}, {'unknown'})
        self.assertEqual(len(out), 2)  # entry unknown, so its TP1 is forwarded (the bridge decides)
        m.close()

        # Crash between the intent record and the answer: the next start marks it unknown.
        class Crash(BaseException):
            pass

        class Crashing:
            def send(self, payload):
                raise Crash()
        m2 = Mirror(self.mirror_config(self.tmp, url), TOKEN, clock_ms=self.clock, transport=Crashing())
        m2.poll_once()  # baseline: nothing new
        self.quote(self.trade['payload']['initial_hard_stop_price'])
        cancel = self.command('cancel')
        with self.assertRaises(Crash):
            m2.poll_once()
        m2.close()
        self.assertFalse(self.settled(cancel['id']))
        sent = Recorder()
        m3 = self.mirror(sent)
        self.assertEqual(self.outcomes()[cancel['id']]['receipt'], {'mirror': 'restarted_after_intent'})
        for _ in range(3):
            m3.poll_once()
        self.assertEqual(sent.sent, [])
        self.assertEqual(len(self.by_type('intent')), 3)

    def test_j_reads_while_soup_writes(self):
        sent = Recorder()
        m = self.mirror(sent, poll_interval_ms=2)
        before = sorted(p.name for p in self.workspace.iterdir())
        errors = []

        def loop():
            try:
                while not m.stopping.is_set():
                    m.poll_once()
                    time.sleep(0.002)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        results = []
        for i in range(300):
            if i == 150:
                self.enter_soup('long')
            results.append(self.quote(BASE + (i % 4) * 0.25))
        until(lambda: len(sent.sent) == 2, 5, 'mirrored entry and TP1')
        m.stopping.set()
        t.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(all(r[0] == 'processed' for r in results), [r for r in results if r[0] != 'processed'][:3])
        self.assertEqual(m.status['db_read_errors'], 0, m.status.get('last_db_error'))
        self.assertEqual(sent.sent, self.primary_transport.sent)
        # The mirror left no read transaction open: Soup can still checkpoint its WAL completely.
        busy, _, _ = self.engine.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        self.assertEqual(busy, 0)
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), before)
        # And Soup keeps writing normally with the mirror's connection still open.
        self.assertEqual(self.quote(BASE + 1)[0], 'processed')

    def test_j2_closed_database_is_not_opened(self):
        """Without -wal/-shm (Soup not running) the mirror does not open, and so creates nothing."""
        d = self.tmp / 'closed'
        d.mkdir()
        db = d / 'engine.sqlite'
        conn = sqlite3.connect(str(db))
        conn.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, body TEXT)')
        conn.commit()
        conn.close()
        reader = SoupReader(db)
        with self.assertRaises(Exception) as ctx:
            reader.state()
        self.assertEqual(type(ctx.exception).__name__, 'SoupNotOpen')
        self.assertEqual([p.name for p in d.iterdir()], ['engine.sqlite'])

    def test_config_refuses_mirror_dir_beside_soup(self):
        with self.assertRaises(ValueError):
            Mirror(self.mirror_config(self.tmp, 'http://127.0.0.1:8777', mirror_dir=str(self.workspace / 'm')),
                   TOKEN)

    def test_reconcile_mode_is_two_step_by_default_and_validated(self):
        m = self.mirror()
        self.assertEqual(m.cfg['reconcile_mode'], 'two_step')
        m.close()
        with self.assertRaises(ValueError):
            self.mirror(reconcile_mode='flatten')


# ----------------------------------------------------------------------------------------
# The bridge daemon's /state endpoint, with in-memory doubles.
# ----------------------------------------------------------------------------------------

class PageDouble:
    def __init__(self, state, net=2):
        self.state, self.epoch = state, 'e1'
        data = {'accounts': [{'id': 7, 'name': 'SECRET-NAME'}],
                'orders': [{'id': 50, 'accountId': 7, 'contractId': 2, 'ordStatus': 'Working', 'action': 'Sell'},
                           {'id': 51, 'accountId': 7, 'contractId': 2, 'ordStatus': 'Filled', 'action': 'Buy'}],
                'fills': [], 'orderVersions': [], 'contracts': [{'id': 2, 'name': 'MNQZ1'}],
                'positions': [{'id': 3, 'accountId': 7, 'contractId': 2, 'netPos': net,
                               'tradeDate': {'year': 2020, 'month': 2, 'day': 12}}]}
        state.load_sync(data['accounts'], data, self.epoch)

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, *a, **k):
        raise AssertionError('the state endpoint must not send anything')


class DaemonStateEndpoint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = AccountState()
        self.page = PageDouble(self.state)
        self.journal = Journal(tmp.name)
        self.ex = Executor(self.page, self.state, self.journal, {
            'ticker': 'MNQZ2021', 'account_name': 'SECRET-NAME', 'soup_account_id': 'X', 'max_contracts': 10,
            'tick_size': .25})
        self.ex.resolve()
        self.journal.start('e1', trade_id='t:1:long', kind='entry', side='long', quantity=2, at=1)
        self.journal.finish('e1', 'accepted', {}, '51')

        class Pub:
            last = {}
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.ex, Pub(), TOKEN))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = 'http://127.0.0.1:%d' % server.server_address[1]

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_state_reports_pinned_account_without_secrets(self):
        status, body = self.get('/state/' + TOKEN)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual({k: data[k] for k in ('account_id', 'contract', 'net_position', 'open_order_ids', 'synced')},
                         {'account_id': 7, 'contract': {'id': 2, 'name': 'MNQZ1'}, 'net_position': 2,
                          'open_order_ids': ['50'], 'synced': True})
        self.assertEqual((data['unseen_accepted_orders'], data['unresolved_commands']), ([], 0))
        self.assertEqual((data['healthy'], data['problems']), (True, []))
        self.assertNotIn(b'SECRET-NAME', body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn('trade_entry_outcome', data)
        _, body = self.get('/state/' + TOKEN + '?trade_id=t%3A1%3Along')
        self.assertEqual(json.loads(body)['trade_entry_outcome'], 'accepted')
        _, body = self.get('/state/' + TOKEN + '?trade_id=other%3Along')
        self.assertIsNone(json.loads(body)['trade_entry_outcome'])

    def test_state_refuses_bad_token_and_queries(self):
        for path in ('/state/' + 'x' * len(TOKEN), '/state', '/state/', '/state/' + TOKEN + '/x',
                     '/order/' + TOKEN):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(self.get('/state/' + TOKEN + '?other=1')[0], 400)
        self.assertEqual(self.get('/state/' + TOKEN + '?trade_id=a&trade_id=b')[0], 400)
        self.assertEqual(self.get('/health/' + TOKEN)[0], 200)

    def test_state_unsynced_after_connection_change(self):
        self.page.epoch = 'e2'  # the page reconnected; state from e1 is not trusted
        data = account_state(self.ex)
        self.assertFalse(data['synced'])
        self.assertFalse(data['healthy'])
        self.assertIn('not_synced_for_current_connection', data['problems'])


# ----------------------------------------------------------------------------------------
# Browser: real bridge daemon handler and executors against the loopback fake exchange.
# ----------------------------------------------------------------------------------------

class MirrorBase(SoupSide, unittest.TestCase):
    MIRROR_OVERRIDES = {}
    EXECUTOR_OVERRIDES = {}  # the mirror account's bridge daemon config

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.fake = self.h.fake
        self.fake.contracts[CONTRACT] = {'id': CONTRACT, 'name': SYMBOL}
        self.fake.set_price(BASE)
        self.fake.accounts.append({'id': MIRROR_ACCOUNT, 'name': 'ACCOUNT-A', 'userId': 7, 'active': True})
        self.ny_clock(11, 0, 0)
        self.state = AccountState()
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port, page_url_contains='/trader/',
                             trade_url_pattern=r'^ws://127\.0\.0\.1:\d+/v1/websocket$')
        self.addCleanup(self.bridge.detach)
        self.bridge.attach(reload=True)
        self.bridge.wait_ready(timeout=20)
        tmp = self.h.tmp
        base = dict(ticker=CONFIG['ticker'], soup_account_id=CONFIG['account_id'], max_contracts=LIMIT)
        self.primary_cfg = self.h.config(journal_dir=str(tmp / 'journal-primary'), **base)
        self.mirror_cfg = self.h.config(account_name='ACCOUNT-A', journal_dir=str(tmp / 'journal-mirror'),
                                        kill_switch_file=str(tmp / 'MIRROR-ENTRIES-DISABLED'),
                                        **dict(base, **self.EXECUTOR_OVERRIDES))
        self.primary = Executor(self.bridge, self.state, Journal(self.primary_cfg['journal_dir']), self.primary_cfg,
                                clock_ms=self.now)
        self.primary.resolve()
        self.mex = Executor(self.bridge, self.state, Journal(self.mirror_cfg['journal_dir']), self.mirror_cfg,
                            clock_ms=self.now)
        self.mex.resolve()
        self.assertEqual((self.primary.account_id, self.mex.account_id), (PRIMARY, MIRROR_ACCOUNT))
        # The mirror account's daemon: executor, evidence publisher and HTTP handler, as in tvbridge.daemon.
        self.publisher = Publisher(self.mex, self.mex.journal, self.state, 'evidence', None,
                                   tmp / 'mirror-snapshots.jsonl', interval_s=0.25, linger_s=5)
        self.mex.watchdogs.append(self.publisher.watchdog)
        threading.Thread(target=self.publisher.run, daemon=True).start()
        self.addCleanup(self.stop_publisher)
        self.received = []
        real_send = self.mex.send

        def daemon_send(payload):
            self.received.append(canonical(payload))
            return real_send(payload)
        self.mex.send = daemon_send
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.mex, self.publisher, TOKEN))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.bridge_url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        # Soup, with the primary executor standing in for a webhook relay.
        self.soup_setup(tmp, Recorder(self.primary), self.now)
        self.broker_results = []
        self.mirror_dir = tmp / 'mirror'
        self.start_mirror()

    def stop_publisher(self):
        self.publisher.stopping.set()
        with self.state.changed:
            self.state.changed.notify_all()

    def start_mirror(self):
        cfg = self.mirror_config(self.h.tmp, self.bridge_url, **self.MIRROR_OVERRIDES)
        self.mirror = Mirror(cfg, TOKEN, clock_ms=self.now)
        thread = threading.Thread(target=self.mirror.run, daemon=True)
        thread.start()
        self.mirror_thread = thread
        self.addCleanup(self.stop_mirror, self.mirror, thread)
        until(lambda: self.mirror.baselined, 5, 'mirror baseline')

    def stop_mirror(self, mirror=None, thread=None):
        mirror, thread = mirror or self.mirror, thread or self.mirror_thread
        mirror.stopping.set()
        thread.join(15)
        mirror.close()

    # --- clock ---------------------------------------------------------------------

    def now(self):
        return self.fake.now_ms()

    def skip_ahead(self, ms):
        self.fake.skew_ms += max(0, int(ms))

    def ny_clock(self, hour, minute, second):
        real = time.time_ns() // 1_000_000
        target = datetime.fromtimestamp(real / 1000, NY).replace(hour=hour, minute=minute, second=second,
                                                                 microsecond=0)
        self.fake.skew_ms = int(target.timestamp() * 1000) - real

    def ny_ms(self, hour, minute, second):
        target = datetime.fromtimestamp(self.now() / 1000, NY).replace(hour=hour, minute=minute, second=second,
                                                                       microsecond=0)
        return int(target.timestamp() * 1000)

    # --- Soup's broker evidence from the primary account ---------------------------------

    def conserved(self, snap):
        kinds = {c['command_id']: c['kind'] for c in self.primary.journal.for_trade(snap['trade_id'])}
        net = sum(o['filled_qty'] * (1 if kinds[o['command_id']] == 'entry' else -1) for o in snap['orders'])
        return net == snap['position_qty']

    def evidence(self, ready=None, timeout=6.0):
        deadline = time.monotonic() + timeout
        reason = None
        while time.monotonic() < deadline:
            snap, reason = self.primary.snapshot(self.trade['id'])
            if snap and self.conserved(snap) and (ready is None or ready(snap)):
                self.assertEqual(self.soup('broker', snap), ('processed', 'broker_reconciled'), snap)
                return snap
            time.sleep(0.05)
        self.fail('no settled snapshot: ' + str(reason))

    def run_until_closed(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while self.trade['phase'] != 'closed':
            self.assertLess(time.monotonic(), deadline, self.trade)
            self.service.dispatch(self.now())
            if self.trade['phase'] == 'closed':
                break
            self.evidence()
            time.sleep(0.05)

    def order_in(self, snap, command_id):
        return next(o for o in snap['orders'] if o['command_id'] == command_id)

    # --- lifecycle steps ------------------------------------------------------------------

    def enter(self, side):
        entry = self.enter_soup(side)
        self.assertEqual([json.loads(p)['action'] for p in self.primary_transport.sent[-2:]],
                         ['buy' if side == 'long' else 'sell', 'exit'])
        self.evidence(lambda s: s['position_qty'] == entry['quantity'] and s['open_orders'])
        self.caught_up()
        return entry

    def mirror_order(self, kind):
        """Bridge order id the mirror account got for the Soup command of this kind."""
        cid = self.command(kind)['id']
        return int(self.mex.journal.get(cid)['broker_order_id'])

    def primary_order(self, kind):
        return int(self.primary.journal.get(self.command(kind)['id'])['broker_order_id'])

    def fill_tp1_both(self):
        tp1 = self.command('tp1_limit')
        self.fake.set_price(tp1['price'])
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        self.assertTrue(self.trade['tp1_done'])
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) in (RUNNERS, -RUNNERS), 5, 'mirror TP1 fill')

    def break_even_exit(self):
        t = self.trade
        self.caught_up()
        self.skip_ahead(t['runner_not_before'] - self.now() + 100)
        be = t['payload']['break_even_price']
        self.fake.set_price(be)
        self.assertEqual(self.quote(be)[0], 'processed')
        self.assertEqual(self.trade['reductions'], {'parent': 'break_even', 'eod': 'break_even'})
        self.run_until_closed()

    def hard_stop(self):
        stop = self.trade['payload']['initial_hard_stop_price']
        self.fake.set_price(stop)
        self.assertEqual(self.quote(stop)[0], 'processed')
        self.assertEqual(self.trade['terminal_reason'], 'initial_hard_stop')
        self.run_until_closed()

    # --- comparison -----------------------------------------------------------------------

    def mirror_orders(self):
        with self.fake.lock:
            return {oid: o for oid, o in self.fake.orders.items() if o['accountId'] == MIRROR_ACCOUNT}

    def assert_mirror_flat(self):
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) == 0, 5, 'mirror flat')
        self.assertEqual(self.fake.positions[(MIRROR_ACCOUNT, CONTRACT)]['netPos'], 0)
        self.assertEqual([o for o in self.fake.working_orders() if o in self.mirror_orders()], [])

    def assert_never_opposite(self, side):
        sign = 1 if side == 'long' else -1
        path = [net for acct, _, net in self.fake.position_history if acct == MIRROR_ACCOUNT]
        self.assertTrue(path and all(sign * net >= 0 for net in path), path)

    def soup_intents(self):
        return [r for r in self.by_type('intent') if r['source'] == 'soup']

    def assert_identical_payloads(self):
        """What Soup sent its primary route, byte for byte, is what the mirror daemon received."""
        soup_ids = [json.loads(p)['extras']['soup_command_id'] for p in self.primary_transport.sent]
        mirrored = [p for p in self.received if not json.loads(p)['extras']['soup_command_id'].startswith('mirror-')]
        self.assertEqual(mirrored, self.primary_transport.sent)
        self.assertEqual([canonical(r['payload']) for r in self.soup_intents()], self.primary_transport.sent)
        self.assertEqual([r['command_id'] for r in self.soup_intents()], soup_ids)

    def latencies(self):
        out = self.outcomes()
        return {r['kind']: out[r['command_id']]['latency_ms'] for r in self.soup_intents()}


class MirrorLifecycles(MirrorBase):
    def lifecycle(self, side, exit_path):
        if exit_path == 'eod':
            self.ny_clock(*before_eod(30))  # just before the fixture's EOD session
        entry = self.enter(side)
        self.assertEqual(entry['quantity'], QTY)
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) in (QTY, -QTY), 5, 'mirror entry fill')
        if exit_path == 'hard_stop':
            self.hard_stop()
            expected = [('entry', QTY), ('tp1_limit', TP1_QTY), ('cancel', 0), ('flatten', QTY)]
        else:
            self.fill_tp1_both()
            if exit_path == 'break_even':
                self.break_even_exit()
                expected = [('entry', QTY), ('tp1_limit', TP1_QTY), ('cancel', 0), ('reduce', RUNNERS)]
            else:
                self.caught_up()
                self.skip_ahead(self.ny_ms(*eod_start(), 6) - self.now())
                with self.service.execution, self.service.mutex:
                    self.engine.tick(self.now())
                self.assertEqual(self.trade['terminal_reason'], 'eod')
                self.run_until_closed()
                expected = [('entry', QTY), ('tp1_limit', TP1_QTY), ('cancel', 0), ('flatten', RUNNERS)]
        self.assertEqual((self.trade['phase'], self.trade['remaining']), ('closed', 0))
        self.caught_up()
        self.assert_identical_payloads()
        commands = self.engine.data['commands']
        self.assertEqual([(commands[r['command_id']]['kind'], commands[r['command_id']]['quantity'])
                          for r in self.soup_intents()], expected)
        out = self.outcomes()
        self.assertEqual({out[r['command_id']]['outcome'] for r in self.soup_intents()}, {'accepted'})
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual((final['result'], final['actions']), ('flat', []))
        self.assert_mirror_flat()
        self.assert_never_opposite(side)
        lat = self.latencies()
        self.assertLessEqual(lat['entry'], 300, lat)
        self.assertTrue(all(v <= 600 for v in lat.values()), lat)
        self.assertEqual(self.mex.journal.unresolved(), [])
        status = json.loads((self.mirror_dir / STATUS_NAME).read_text())
        self.assertEqual(status['counts']['accepted'], len(expected))
        self.assertEqual(status['last_reconciliation']['result'], 'flat')
        return lat

    def test_b1_long_tp1_then_break_even_runners_mirrored_to_flat(self):
        self.lifecycle('long', 'break_even')

    def test_b2_short_tp1_then_eod_flatten_mirrored_to_flat(self):
        self.lifecycle('short', 'eod')

    def test_b3_long_hard_stop_cancels_tp1_and_flattens_all(self):
        self.lifecycle('long', 'hard_stop')
        tp1 = self.mirror_order('tp1_limit')
        self.assertEqual(self.fake.orders[tp1]['ordStatus'], 'Canceled')

    def test_e_restart_mid_trade_does_not_resend(self):
        self.enter('long')
        orders = dict(self.mirror_orders())
        self.assertEqual(len(orders), 2)  # entry and TP1 on the mirror account
        self.stop_mirror()
        records = len(self.records())
        self.start_mirror()
        time.sleep(0.5)
        self.assertEqual(len(self.records()), records)  # nothing new: no resend, no second baseline
        self.assertEqual(self.mirror_orders().keys(), orders.keys())
        self.fill_tp1_both()
        self.break_even_exit()
        self.caught_up()
        self.assert_identical_payloads()
        self.assertEqual(self.final_reconcile(self.trade['id'])['result'], 'flat')
        self.assert_mirror_flat()
        self.assertEqual(len(self.mirror_orders()), 3)  # entry, TP1, reduce
        tags = [self.fake.versions[o]['text'] for o in self.mirror_orders()]
        self.assertEqual(len(tags), len(set(tags)))


class MirrorDivergence(MirrorBase):
    MIRROR_OVERRIDES = {'reconcile_grace_ms': 1500, 'reconcile_settle_ms': 800}

    def test_f1_tp1_fills_on_primary_only_reconciliation_closes_exact_remainder(self):
        self.enter('long')
        self.fake.fill_limit(self.primary_order('tp1_limit'), TP1_QTY)
        tp1 = self.command('tp1_limit')
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        self.assertEqual(self.state.net_position(MIRROR_ACCOUNT, CONTRACT), QTY)  # mirror TP1 still resting
        self.break_even_exit()  # Soup: cancel barrier (cancels the mirror's TP1 too), reduce the runners
        self.caught_up()
        out = self.outcomes()
        self.assertEqual([out[r['command_id']]['outcome'] for r in self.soup_intents()], ['accepted'] * 4)
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) == TP1_QTY, 5, 'mirror keeps its TP1 lots')
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual(final['result'], 'flat')
        self.assertEqual([(a['command_id'].split(':')[0], a['outcome'], a.get('quantity')) for a in final['actions']],
                         [('mirror-flatten', 'accepted', TP1_QTY)])
        self.assert_mirror_flat()
        self.assert_never_opposite('long')

    def test_f3_next_trade_waits_for_cleanup_of_the_replaced_one(self):
        self.mirror.cfg['reconcile_grace_ms'] = 120000  # only the replacement can trigger cleanup
        self.enter('long')
        trade_a = self.trade['id']
        self.fake.fill_limit(self.primary_order('tp1_limit'), TP1_QTY)
        tp1 = self.command('tp1_limit')
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        self.break_even_exit()
        self.caught_up()
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) == TP1_QTY, 5, 'mirror keeps its TP1 lots')
        time.sleep(0.5)
        self.assertFalse(self.by_type('reconcile'))  # closed, but inside the grace period
        self.skip_ahead(5000)  # next decision bar: a new Soup trade id
        self.enter('long')
        trade_b = self.trade['id']
        self.assertNotEqual(trade_a, trade_b)
        final = self.final_reconcile(trade_a)
        self.assertEqual((final['result'], [a.get('quantity') for a in final['actions']]), ('flat', [TP1_QTY]))
        intents = [r['command_id'] for r in self.by_type('intent')]
        entry_b = self.command('entry')['id']
        self.assertLess(intents.index('mirror-flatten:' + trade_a), intents.index(entry_b))
        self.assertEqual(self.outcomes()[entry_b]['outcome'], 'accepted')
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) == QTY, 5, 'mirror entered trade B')
        self.mirror.cfg['reconcile_grace_ms'] = 300
        self.hard_stop()
        self.caught_up()
        self.assertEqual(self.final_reconcile(trade_b)['result'], 'flat')
        self.assert_mirror_flat()
        self.assert_never_opposite('long')

    def test_f2_resting_tp1_at_trade_end_is_cancelled_then_remainder_closed(self):
        self.enter('long')
        mirror_tp1 = self.mirror_order('tp1_limit')
        self.fake.fill_limit(self.primary_order('tp1_limit'), TP1_QTY)
        tp1 = self.command('tp1_limit')
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        # The mirror daemon is briefly unhealthy: it refuses Soup's cancel barrier and exit.
        offline = {'on': True}
        self.mex.watchdogs.append(lambda: 'test_offline' if offline['on'] else None)
        self.break_even_exit()
        reduce_id = self.command('reduce')['id']
        until(lambda: self.settled(reduce_id), 10, 'mirror reduce outcome')
        out = self.outcomes()
        self.assertEqual(out[self.command('cancel')['id']]['outcome'], 'rejected')
        self.assertEqual(out[reduce_id]['outcome'], 'rejected')
        self.assertEqual(self.fake.orders[mirror_tp1]['ordStatus'], 'Working')
        self.assertEqual(self.state.net_position(MIRROR_ACCOUNT, CONTRACT), QTY)
        # Reconciliation waits for a healthy bridge instead of spending its one-shot commands.
        until(lambda: (self.mirror.status.get('reconcile_waiting') or {}).get('reason', '')
              .startswith('bridge_unhealthy:test_offline'), 10, 'reconciliation waiting for the bridge')
        self.assertFalse([r for r in self.by_type('intent') if r['source'] == 'reconcile'])
        offline['on'] = False
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual(final['result'], 'flat', final)
        self.assertEqual([(a['command_id'].split(':')[0], a['outcome']) for a in final['actions']],
                         [('mirror-cancel', 'accepted'), ('mirror-flatten', 'accepted')])
        self.assertEqual(final['actions'][0]['open_order_ids'], [str(mirror_tp1)])
        self.assertEqual(final['actions'][1]['quantity'], QTY)
        self.assertEqual(self.fake.orders[mirror_tp1]['ordStatus'], 'Canceled')
        self.assert_mirror_flat()
        self.assert_never_opposite('long')
        # The mirror's own commands went through the bridge journal under the trade's id.
        for prefix in ('mirror-cancel:', 'mirror-flatten:'):
            j = self.mex.journal.get(prefix + self.trade['id'])
            self.assertEqual((j['trade_id'], j['outcome']), (self.trade['id'], 'accepted'))

    def test_g_oversized_mirror_exit_refused_then_reconciled_flat(self):
        self.enter('long')
        self.fake.fill_limit(self.mirror_order('tp1_limit'), TP1_QTY)  # only the mirror's TP1 fills
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) == RUNNERS, 5, 'mirror TP1 fill')
        self.hard_stop()  # Soup: cancel its TP1, flatten all 5 on the primary account
        self.caught_up()
        flatten = self.command('flatten')
        self.assertEqual(flatten['quantity'], QTY)
        out = self.outcomes()[flatten['id']]
        self.assertEqual(out['outcome'], 'rejected')
        self.assertIn('exceeds live position', out['receipt']['reason'])
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual(final['result'], 'flat')
        self.assertEqual([(a['command_id'].split(':')[0], a.get('quantity')) for a in final['actions']],
                         [('mirror-flatten', RUNNERS)])
        self.assert_mirror_flat()
        self.assert_never_opposite('long')

    def test_h_foreign_position_is_untouched_and_reported(self):
        with self.fake.lock:
            self.fake._place({'accountId': MIRROR_ACCOUNT, 'action': 'Buy', 'symbol': SYMBOL, 'orderQty': 1,
                              'orderType': 'Market', 'text': 'manual'}, lambda *a, **k: None)
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) == 1, 5, 'foreign position')
        foreign = set(self.mirror_orders())
        self.enter('long')
        self.hard_stop()
        self.caught_up()
        entry = self.command('entry')
        self.assertEqual(self.outcomes()[entry['id']]['outcome'], 'rejected')
        skipped = [r for r in self.by_type('skipped') if r['trade_id'] == self.trade['id']]
        self.assertEqual(sorted(r['kind'] for r in skipped), ['cancel', 'flatten', 'tp1_limit'])
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual((final['result'], final['net_position']), ('foreign_position', 1))
        self.assertNotIn('actions', final)
        self.assertEqual(set(self.mirror_orders()), foreign)  # nothing sent to the mirror account
        self.assertEqual(self.state.net_position(MIRROR_ACCOUNT, CONTRACT), 1)
        status = json.loads((self.mirror_dir / STATUS_NAME).read_text())
        self.assertEqual(status['last_reconciliation']['result'], 'foreign_position')



class MirrorLiquidate(MirrorBase):
    """reconcile_mode "liquidate": one flatten_all (liquidateposition) instead of cancel + exit."""
    MIRROR_OVERRIDES = {'reconcile_grace_ms': 1500, 'reconcile_settle_ms': 800, 'reconcile_mode': 'liquidate'}
    EXECUTOR_OVERRIDES = {'flatten_all_enabled': True}

    def reconcile_requests(self, before):
        return [r for r in self.fake.requests[before:] if r.startswith('order/')]

    def test_l1_resting_tp1_and_position_closed_by_one_liquidation(self):
        self.enter('long')
        mirror_tp1 = self.mirror_order('tp1_limit')
        self.fake.fill_limit(self.primary_order('tp1_limit'), TP1_QTY)
        tp1 = self.command('tp1_limit')
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        offline = {'on': True}  # the mirror daemon refuses Soup's cancel barrier and exit
        self.mex.watchdogs.append(lambda: 'test_offline' if offline['on'] else None)
        self.break_even_exit()
        until(lambda: self.settled(self.command('reduce')['id']), 10, 'mirror reduce outcome')
        until(lambda: (self.mirror.status.get('reconcile_waiting') or {}).get('reason', '')
              .startswith('bridge_unhealthy'), 10, 'reconciliation waiting for the bridge')
        before = len(self.fake.requests)
        offline['on'] = False
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual(final['result'], 'flat', final)
        self.assertEqual([(a['command_id'].split(':')[0], a['outcome'], a['quantity'], a['open_order_ids'])
                          for a in final['actions']],
                         [('mirror-liquidate', 'accepted', QTY, [str(mirror_tp1)])])
        # One request did it: no mirror cancel, no mirror market exit.
        self.assertEqual(self.reconcile_requests(before).count('order/liquidateposition'), 1)
        self.assertEqual([r for r in self.reconcile_requests(before) if r != 'order/liquidateposition'], [])
        self.assertEqual(self.fake.orders[mirror_tp1]['ordStatus'], 'Canceled')
        self.assert_mirror_flat()
        self.assert_never_opposite('long')
        j = self.mex.journal.get('mirror-liquidate:' + self.trade['id'])
        self.assertEqual((j['kind'], j['outcome'], j['mode']), ('flatten_all', 'accepted', 'liquidate'))
        self.assertFalse([r for r in self.by_type('intent')
                          if r['command_id'].startswith(('mirror-cancel:', 'mirror-flatten:'))])

    def test_l2_tp1_on_primary_only_remainder_liquidated(self):
        self.enter('long')
        self.fake.fill_limit(self.primary_order('tp1_limit'), TP1_QTY)
        tp1 = self.command('tp1_limit')
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        self.break_even_exit()  # Soup's cancel barrier also cancels the mirror's TP1; reduce the runners
        self.caught_up()
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) == TP1_QTY, 5, 'mirror keeps its TP1 lots')
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual(final['result'], 'flat')
        self.assertEqual([(a['command_id'].split(':')[0], a['outcome'], a['quantity']) for a in final['actions']],
                         [('mirror-liquidate', 'accepted', TP1_QTY)])
        self.assert_mirror_flat()
        self.assert_never_opposite('long')

    def test_l3_clean_trade_needs_no_liquidation(self):
        self.enter('long')
        self.fill_tp1_both()
        self.break_even_exit()
        self.caught_up()
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual((final['result'], final['actions']), ('flat', []))
        self.assertNotIn('order/liquidateposition', self.fake.requests)


class MirrorLiquidateRefused(MirrorBase):
    """liquidate mode against a daemon without flatten_all_enabled: refused, so the two-step cleanup runs."""
    MIRROR_OVERRIDES = {'reconcile_grace_ms': 1500, 'reconcile_settle_ms': 800, 'reconcile_mode': 'liquidate'}

    def test_refused_liquidation_falls_back_to_cancel_and_exit(self):
        self.enter('long')
        self.fake.fill_limit(self.primary_order('tp1_limit'), TP1_QTY)
        tp1 = self.command('tp1_limit')
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        self.break_even_exit()
        self.caught_up()
        until(lambda: self.state.net_position(MIRROR_ACCOUNT, CONTRACT) == TP1_QTY, 5, 'mirror keeps its TP1 lots')
        final = self.final_reconcile(self.trade['id'])
        self.assertEqual(final['result'], 'flat', final)
        self.assertEqual([(a['command_id'].split(':')[0], a['outcome']) for a in final['actions']],
                         [('mirror-liquidate', 'rejected'), ('mirror-flatten', 'accepted')])
        self.assertIn('flatten_all_enabled', self.outcomes()['mirror-liquidate:' + self.trade['id']]
                      ['receipt']['reason'])
        self.assertNotIn('order/liquidateposition', self.fake.requests)
        self.assert_mirror_flat()
        self.assert_never_opposite('long')


if __name__ == '__main__':
    unittest.main(verbosity=2)
