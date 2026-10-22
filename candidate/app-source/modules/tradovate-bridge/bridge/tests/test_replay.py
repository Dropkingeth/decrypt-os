"""Replay and comparison: Soup's real engine drives complete lifecycles through the bridge.

Soup's core.Engine makes every decision with the shared test fixture's settings and
account policy. Its payloads go
through Executor.send against the loopback fake exchange in headless Chrome, and
every published snapshot is applied through Soup's own core.broker().

Each lifecycle compares command IDs, kinds and quantities (Soup vs bridge journal
vs exchange orders), fills (exchange vs Soup's reconciled filled_qty), Soup's
acceptance of every snapshot, and the terminal state: flat, nothing working, and
no position ever on the opposite side of the trade.

Time: Soup, the executor and the fake's entity timestamps share one clock
(real time + FakeTradovate.skew_ms), pinned to 11:00 New York by default. Skipping it
forward replaces the runner-delay wait and places the EOD case just inside the fixture's
EOD session.
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from harness import Harness
from soup_inputs import named  # synthetic Soup input names -> the package's own

# The optional engine package is not bundled: point TVBRIDGE_SOUP_PACKAGE at it, otherwise this
# module is skipped.
SOUP = Path(os.environ.get('TVBRIDGE_SOUP_PACKAGE',
                           ''))
if not (SOUP / 'engine' / 'core.py').is_file():
    raise unittest.SkipTest('optional engine package not found; set TVBRIDGE_SOUP_PACKAGE')
for sub in ('engine', 'tests', 'tests/support'):
    sys.path.insert(0, str(SOUP / sub))
import test_pair as soup_fixtures  # noqa: E402
from core import Engine, Rejected  # noqa: E402
from transport import message  # noqa: E402

from tvbridge.bridge import Bridge  # noqa: E402
from tvbridge.daemon import Publisher, make_handler  # noqa: E402
from tvbridge.executor import Executor, client_tag  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402
from tvbridge.protocol import tradovate_symbol  # noqa: E402

CONFIG = soup_fixtures.CONFIG  # the package's dated MNQ contract
SYMBOL = tradovate_symbol(CONFIG['ticker'])  # its Tradovate name
ACCOUNT, CONTRACT = 101, 556
LIMIT = 10
POLICY = {'id': 'account-original-allocation-micro-cap-v1', 'account_id': CONFIG['account_id'],
          'max_contracts': LIMIT, 'exit_allocation': 'original',
          'native_binding': {'broker_id': 'tvbridge-offline', 'account_value': 'DEMO-1',
                             'account_label': 'Offline fake exchange', 'account_url_sha256': '0' * 64}}
NY = ZoneInfo('America/New_York')
# Synthetic settings for this suite: 5 contracts = TP1 2 + parent runner 1 + EOD runners 2, stop 40,
# TP1 16 with its working limit 0.75 inside, break-even +3. Prices are rebased to BASE.
BASE = 10000.0
FIXTURE = {'initial_hard_stop_handles': 40, 'tp1_handles': 16, 'be_offset_handles': 3, 'fixture_int_07': 2,
           'fixture_int_06': 1, 'fixture_int_01': 0, 'fixture_int_04': 2}
TP1_OFFSET = 0.75
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


BRIDGE_KIND = {'entry': 'entry', 'tp1_limit': 'tp1_limit', 'cancel': 'cancel',
               'reduce': 'exit_market', 'flatten': 'exit_market'}


class Killed(BaseException):
    """The daemon process dying mid-send. Not an Exception, so the executor cannot record a result."""


def until(fn, timeout=8.0, what='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(what + ' not reached in time')


class SoupBrokerEndpoint:
    """Loopback stand-in for Soup's /broker/<token> receiver.

    With status 202 each snapshot goes straight through Soup's core.broker(); any other
    status refuses delivery without applying it, like an unavailable Soup runtime. Like
    Soup's runtime, 202 is a delivery receipt: it is returned even when core.broker() refuses.
    """

    def __init__(self, test):
        endpoint = self
        self.status = 202
        self.received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                snap = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                status = endpoint.status
                endpoint.received.append((snap['sequence'], status))
                if status == 202:
                    test.soup_broker(snap)
                self.send_response(status)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d/broker/offline-test-token' % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ReplayBase(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.fake = self.h.fake
        self.fake.contracts[CONTRACT] = {'id': CONTRACT, 'name': SYMBOL}
        self.fake.set_price(BASE)
        self.cfg = self.h.config(ticker=CONFIG['ticker'], soup_account_id=CONFIG['account_id'],
                                 max_contracts=LIMIT)
        self.lock = threading.RLock()  # serializes Soup engine calls, like runtime.Service
        self.broker_results = []       # (sequence, trade id, Soup result, snapshot) for every applied snapshot
        self.applied = []              # snapshots Soup accepted
        self.quotes = 0
        self.connect()
        # Outside the fixture's EOD session whatever the wall clock says.
        self.ny_clock(11, 0, 0)
        self.engine = Engine(self.h.tmp / 'soup.sqlite', CONFIG, tp1_limit_offset_handles=TP1_OFFSET,
                             quantity_policy=POLICY)
        self.addCleanup(self.engine.close)

    # --- bridge side --------------------------------------------------------------

    def connect(self):
        """Start the bridge side as the daemon does: attach, wait for sync, load the journal, pin."""
        self.state = AccountState()
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port, page_url_contains='/trader/',
                             trade_url_pattern=self.cfg['trade_url_pattern'])
        self.addCleanup(self.bridge.detach)
        self.bridge.attach(reload=True)
        self.bridge.wait_ready(timeout=20)
        self.journal = Journal(self.cfg['journal_dir'])
        self.ex = Executor(self.bridge, self.state, self.journal, self.cfg, clock_ms=self.now)
        self.ex.resolve()

    def now(self):
        return self.fake.now_ms()

    def skip_ahead(self, ms):
        self.fake.skew_ms += max(0, int(ms))

    def ny_clock(self, hour, minute, second):
        """Set the shared clock to today's hh:mm:ss in New York."""
        real = time.time_ns() // 1_000_000
        target = datetime.fromtimestamp(real / 1000, NY).replace(hour=hour, minute=minute, second=second,
                                                                 microsecond=0)
        self.fake.skew_ms = int(target.timestamp() * 1000) - real

    def placeorders(self):
        return self.fake.requests.count('order/placeorder')

    # --- Soup side ----------------------------------------------------------------

    @property
    def trade(self):
        return self.engine.data['trade']

    def command(self, kind):
        return next(c for c in reversed(list(self.engine.data['commands'].values())) if c['kind'] == kind)

    def soup_broker(self, snap):
        with self.lock:
            try:
                result = self.engine.apply('broker', snap, self.now())
            except Rejected as e:
                result = 'rejected:' + str(e)
        self.broker_results.append((snap['sequence'], snap['trade_id'], result, snap))
        if result == 'broker_reconciled':
            self.applied.append(snap)
        return result

    def quote(self, price):
        with self.lock:
            last = self.engine.data['last_quote']
            at = self.now()
            if last and at <= last['time']:
                at = last['time'] + 1
            self.quotes += 1
            return self.engine.apply('quote', {'event_id': f'q{at}-{self.quotes}', 'symbol': CONFIG['ticker'],
                                               'time': at, 'price': price}, at)

    def drain(self, kill=None):
        """Dispatch every ready Soup command through the bridge, like runtime.Service._dispatch."""
        sent = []
        while True:
            with self.lock:
                ready = self.engine.ready()
                if not ready:
                    return sent
                c = self.engine.start(ready[0]['id'], self.now())
                if c is None:
                    if ready[0]['kind'] == 'entry':
                        continue
                    return sent
                payload = message(c, CONFIG['ticker'], self.trade['side'], quantity_limit=LIMIT)
            try:
                outcome, receipt = self.ex.send(payload)
            except Killed:
                # The daemon died with the request; Soup's HTTP transport sees a reset connection.
                outcome, receipt = 'unknown', {'transport': 'uncertain'}
            with self.lock:
                self.engine.transport_result(c['id'], outcome, dict(receipt, transport_outcome=outcome),
                                             self.now())
            record = {'id': c['id'], 'kind': c['kind'], 'quantity': c['quantity'], 'outcome': outcome,
                      'receipt': receipt, 'payload': payload}
            sent.append(record)
            if kill is not None and kill.get('fired'):
                return sent

    def conserved(self, snap):
        """Fill/position conservation for the trade's own orders (settled exchange state)."""
        kinds = {c['command_id']: c['kind'] for c in self.journal.for_trade(snap['trade_id'])}
        net = sum(o['filled_qty'] * (1 if kinds[o['command_id']] == 'entry' else -1) for o in snap['orders'])
        return net == snap['position_qty']

    def evidence(self, ready=None, timeout=6.0):
        """Apply the next settled bridge snapshot through Soup's core.broker(); it must be accepted.

        'Settled' skips a snapshot built between Tradovate's fill and position frames (see
        stale_rejections); the Publisher-driven test covers that stream unfiltered."""
        deadline = time.monotonic() + timeout
        reason = None
        while time.monotonic() < deadline:
            snap, reason = self.ex.snapshot(self.trade['id'])
            if snap and self.conserved(snap) and (ready is None or ready(snap)):
                result = self.soup_broker(snap)
                self.assertEqual(result, 'broker_reconciled', snap)
                return snap
            time.sleep(0.05)
        self.fail('no settled snapshot: ' + str(reason))

    @staticmethod
    def order_in(snap, command_id):
        return next(o for o in snap['orders'] if o['command_id'] == command_id)

    def run_until_closed(self, timeout=15.0):
        deadline = time.monotonic() + timeout
        while self.trade['phase'] != 'closed':
            self.assertLess(time.monotonic(), deadline, self.trade)
            self.drain()
            if self.trade['phase'] == 'closed':
                break
            self.evidence()
            time.sleep(0.05)

    # --- lifecycle steps ------------------------------------------------------------

    def enter(self, side, changes=None):
        entry = entry_payload(side, changes, at=self.now() // 5000 * 5000)
        with self.lock:
            self.assertEqual(self.engine.apply('pine', entry, self.now(), entries_enabled=True), 'entry_accepted')
        sent = self.drain()
        self.assertEqual([(s['kind'], s['quantity'], s['outcome']) for s in sent],
                         [('entry', entry['quantity'], 'accepted'), ('tp1_limit', entry['tp1_qty'], 'accepted')],
                         sent)
        tp1 = self.command('tp1_limit')
        sign = 1 if side == 'long' else -1
        offset = self.trade['execution_policy']['tp1_limit_offset_handles']  # pinned from the engine config
        self.assertEqual(offset, TP1_OFFSET)
        self.assertEqual(tp1['price'], entry['tp1_price'] - sign * offset)
        snap = self.evidence(lambda s: s['position_qty'] == entry['quantity'] and s['open_orders'])
        self.assertEqual(self.order_in(snap, tp1['id'])['status'], 'working')
        self.assertEqual(snap['open_orders'], [self.journal.get(tp1['id'])['broker_order_id']])
        self.assertEqual(self.trade['entry_filled'], entry['quantity'])
        return entry

    def fill_tp1(self):
        tp1 = self.command('tp1_limit')
        self.fake.set_price(tp1['price'])
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        self.assertEqual(self.command('tp1_limit')['status'], 'filled')
        self.assertTrue(self.trade['tp1_done'])
        self.assertEqual(self.trade['remaining'], self.trade['entry_filled'] - tp1['quantity'])

    def break_even_exit(self):
        """After Soup's runner delay, a quote at break-even closes the parent and EOD runners."""
        t = self.trade
        self.skip_ahead(t['runner_not_before'] - self.now() + 100)
        be = t['payload']['break_even_price']
        self.fake.set_price(be)
        self.assertEqual(self.quote(be), 'quote_processed')
        self.assertEqual(self.trade['reductions'], {'parent': 'break_even', 'eod': 'break_even'})
        self.run_until_closed()

    def hard_stop(self):
        stop = self.trade['payload']['initial_hard_stop_price']
        self.fake.set_price(stop)
        self.assertEqual(self.quote(stop), 'quote_processed')
        self.assertEqual(self.trade['terminal_reason'], 'initial_hard_stop')

    def stale_exit(self, qty, cid):
        """A Soup-shaped market exit carrying a stale (pre-fill) quantity."""
        c = {'kind': 'flatten', 'id': cid, 'trade_id': self.trade['id'], 'quantity': qty, 'price': BASE,
             'quantity_limit': LIMIT}
        return message(c, CONFIG['ticker'], self.trade['side'], quantity_limit=LIMIT)

    def synthetic(self, kind, cid, trade_id, qty):
        side = trade_id.rsplit(':', 1)[1]
        c = {'kind': kind, 'id': cid, 'trade_id': trade_id, 'quantity': qty, 'price': BASE,
             'quantity_limit': LIMIT}
        return message(c, CONFIG['ticker'], side, quantity_limit=LIMIT)

    def assert_refused_without_sending(self, payload, reason):
        before = self.placeorders()
        outcome, receipt = self.ex.send(payload)
        self.assertEqual(outcome, 'rejected', receipt)
        self.assertIn(reason, receipt.get('reason', ''))
        self.assertIsNone(self.journal.get(payload['extras']['soup_command_id']))
        self.assertEqual(self.placeorders(), before)

    # --- comparison -------------------------------------------------------------------

    def assert_reconciled(self, expected, history_from, unresolved=(), allow_stale=False):
        """Soup, the bridge journal and the exchange agree, and the trade ended flat."""
        t = self.trade
        side = t['side']
        self.assertEqual((t['phase'], t['remaining']), ('closed', 0), t)
        commands = self.engine.data['commands']
        sent = [c for c in commands.values() if c['status'] not in ('ready', 'superseded')]
        self.assertEqual([(c['kind'], c['quantity']) for c in sent], expected)
        journal = {c['command_id']: c for c in self.journal.for_trade(t['id'])}
        self.assertEqual(sorted(journal), sorted(c['id'] for c in sent), 'command IDs differ')
        for c in sent:
            j = journal[c['id']]
            self.assertEqual(j['kind'], BRIDGE_KIND[c['kind']])
            if c['kind'] == 'cancel':
                continue
            self.assertEqual((j['outcome'], j['quantity']), ('accepted', c['quantity']), j)
            oid = int(j['broker_order_id'])
            self.assertEqual(t['broker_orders'][c['id']]['broker_order_id'], j['broker_order_id'])
            version, order = self.fake.versions[oid], self.fake.orders[oid]
            self.assertEqual((version['orderQty'], version['text']), (c['quantity'], client_tag(c['id'])))
            self.assertEqual(self.fake.filled_qty(oid), c['filled_qty'], c)
            self.assertEqual(c['filled_qty'], c['quantity'] if c['status'] == 'filled' else c['filled_qty'])
            buys = (c['kind'] == 'entry') == (side == 'long')
            self.assertEqual(order['action'], 'Buy' if buys else 'Sell')
            self.assertIs(order['isAutomated'], True)
        entry = next(c for c in sent if c['kind'] == 'entry')
        exits = sum(c['filled_qty'] for c in sent if c['kind'] in ('tp1_limit', 'reduce', 'flatten'))
        self.assertEqual(entry['filled_qty'] - exits, 0)
        # Exactly one exchange order per order command: nothing retried or replayed.
        tags = [v['text'] for v in self.fake.versions.values()]
        self.assertEqual(len(tags), len(set(tags)))
        self.assertEqual(self.placeorders(), len(self.fake.orders))
        # Terminal state at the exchange and in the bridge.
        self.assertEqual(self.fake.positions[(ACCOUNT, CONTRACT)]['netPos'], 0)
        self.assertEqual(self.fake.working_orders(), [])
        with self.state.lock:
            self.assertEqual(self.state.net_position(ACCOUNT, CONTRACT), 0)
            self.assertEqual(self.state.open_orders(ACCOUNT, CONTRACT), [])
        self.assertEqual(sorted(c['command_id'] for c in self.journal.unresolved()), sorted(unresolved))
        if not unresolved:
            self.assertFalse(self.ex.trade_active(t['id']))
        # Never an opposite-side position during the trade.
        sign = 1 if side == 'long' else -1
        path = [net for _, _, net in self.fake.position_history[history_from:]]
        self.assertTrue(path and all(sign * net >= 0 for net in path), path)
        # Soup accepted every snapshot applied for this trade (asynchronous delivery: see stale_rejections).
        results = [r for _, tid, r, _ in self.broker_results if tid == t['id']]
        self.assertIn('broker_reconciled', results)
        if not allow_stale:
            self.assertTrue(all(r == 'broker_reconciled' for r in results), results)
        return self.stale_rejections(t['id'])

    def stale_rejections(self, trade_id):
        """Benign refusals of asynchronously published snapshots; Soup rolls each one back.

        1. Soup marks a command started before the bridge sees it. A snapshot built just before a
           dispatch but delivered after it lacks that command ('incomplete broker command
           inventory'); its orders must be an earlier prefix of the journal.
        2. Tradovate reports fill, order and position in separate frames. A snapshot built between
           them breaks fill/position conservation in itself, and Soup refuses it.
        Anything else fails. A later snapshot must have been accepted."""
        order = [c['command_id'] for c in self.journal.for_trade(trade_id) if c['kind'] != 'cancel']
        stale = 0
        for seq, tid, result, snap in self.broker_results:
            if tid != trade_id or result == 'broker_reconciled':
                continue
            if result == 'rejected:broker fill/position conservation mismatch':
                self.assertFalse(self.conserved(snap), snap)
            else:
                self.assertEqual(result, 'rejected:incomplete broker command inventory', snap)
                included = [o['command_id'] for o in snap['orders']]
                self.assertLess(len(included), len(order))
                self.assertEqual(included, order[:len(included)])
            self.assertTrue(any(s > seq and r == 'broker_reconciled' for s, t, r, _ in self.broker_results
                                if t == trade_id))
            stale += 1
        return stale


class Lifecycles(ReplayBase):
    def full_lifecycle(self, side, runner_exit):
        start = len(self.fake.position_history)
        if runner_exit == 'eod':
            self.ny_clock(*before_eod(30))  # just before the fixture's EOD session
        entry = self.enter(side)
        self.assertEqual((entry['quantity'], entry['tp1_qty'], entry['parent_runner_qty'],
                          entry['eod_runner_qty']), (5, 2, 1, 2))
        self.fill_tp1()
        self.assertEqual(self.engine.left(), {'tp1': 0, 'parent': 1, 'bias': 0, 'eod': 2})
        if runner_exit == 'break_even':
            self.break_even_exit()
            last = 'reduce'
        else:
            self.skip_ahead(self.ny_ms(*eod_start(), 6) - self.now())
            with self.lock:
                self.engine.tick(self.now())
            self.assertEqual(self.trade['terminal_reason'], 'eod')
            self.run_until_closed()
            last = 'flatten'
        self.assert_reconciled([('entry', 5), ('tp1_limit', 2), ('cancel', 0), (last, 3)], start)
        exit_cmd = self.command(last)
        self.assertEqual(exit_cmd['alloc'], {'parent': 1, 'eod': 2})
        self.assertEqual(self.journal.get(self.command('cancel')['id'])['receipt'], {'cancel_targets': {}})

    def ny_ms(self, hour, minute, second):
        now = self.now()
        target = datetime.fromtimestamp(now / 1000, NY).replace(hour=hour, minute=minute, second=second,
                                                                microsecond=0)
        return int(target.timestamp() * 1000)

    def test_1_long_entry_tp1_then_break_even_runners_to_flat(self):
        self.full_lifecycle('long', 'break_even')

    def test_1b_long_entry_tp1_then_eod_flatten_to_flat(self):
        self.full_lifecycle('long', 'eod')

    def test_2_short_entry_tp1_then_break_even_runners_to_flat(self):
        self.full_lifecycle('short', 'break_even')

    def test_2b_short_entry_tp1_then_eod_flatten_to_flat(self):
        self.full_lifecycle('short', 'eod')

    def test_7_back_to_back_trades_share_no_state(self):
        start_a = len(self.fake.position_history)
        self.enter('long')
        trade_a = self.trade['id']
        self.fill_tp1()
        self.break_even_exit()
        self.assert_reconciled([('entry', 5), ('tp1_limit', 2), ('cancel', 0), ('reduce', 3)], start_a)
        a_commands = set(self.engine.data['commands'])
        a_orders = {c['broker_order_id'] for c in self.journal.for_trade(trade_a) if c.get('broker_order_id')}
        a_snapshots = len(self.applied)

        self.skip_ahead(5000)  # next decision bar: a new Soup trade id
        start_b = len(self.fake.position_history)
        self.enter('short')
        trade_b = self.trade['id']
        self.assertNotEqual(trade_a, trade_b)
        self.assertEqual(self.journal.latest_trade_id(), trade_b)
        self.assertFalse(a_commands & set(self.engine.data['commands']))
        self.hard_stop()  # before TP1: cancel the working limit, then close all five
        self.run_until_closed()
        self.assert_reconciled([('entry', 5), ('tp1_limit', 2), ('cancel', 0), ('flatten', 5)], start_b)
        cancel = self.journal.get(self.command('cancel')['id'])
        self.assertEqual(list(cancel['receipt']['cancel_targets']),
                         [self.journal.get(self.command('tp1_limit')['id'])['broker_order_id']])
        self.assertEqual(self.command('tp1_limit')['status'], 'cancelled')
        # Trade B's evidence never mentioned trade A's commands or exchange orders.
        for snap in self.applied[a_snapshots:]:
            self.assertEqual(snap['trade_id'], trade_b)
            self.assertFalse({o['command_id'] for o in snap['orders']} & a_commands)
            self.assertFalse(({o['broker_order_id'] for o in snap['orders']} | set(snap['open_orders'])) & a_orders)
        sequences = [seq for seq, _, _, _ in self.broker_results]
        self.assertEqual(sequences, sorted(set(sequences)))


class PartialFills(ReplayBase):
    CHANGES = {'fixture_int_07': 3}  # 3 TP1 + 1 parent + 2 EOD runners: TP1 can fill partially

    def partial_tp1(self):
        start = len(self.fake.position_history)
        entry = self.enter('long', self.CHANGES)
        self.assertEqual((entry['quantity'], entry['tp1_qty']), (6, 3))
        tp1 = self.command('tp1_limit')
        oid = self.journal.get(tp1['id'])['broker_order_id']
        self.fake.fill_limit(int(oid), 1)
        snap = self.evidence(lambda s: self.order_in(s, tp1['id'])['filled_qty'] == 1)
        o = self.order_in(snap, tp1['id'])
        self.assertEqual((o['status'], o['filled_qty'], snap['position_qty'], snap['open_orders']),
                         ('partial', 1, 5, [oid]))
        self.assertEqual((self.command('tp1_limit')['status'], self.command('tp1_limit')['filled_qty']),
                         ('accepted', 1))
        self.assertFalse(self.trade['tp1_done'])
        self.assertEqual(self.trade['remaining'], 5)
        # A market exit while the rest of TP1 works would race it; the bridge refuses.
        self.assert_refused_without_sending(self.stale_exit(5, 'stale-exit-while-working'), 'cancel barrier')
        return start, tp1, oid

    def test_3a_partial_tp1_then_remainder_fills(self):
        start, tp1, oid = self.partial_tp1()
        self.fill_tp1()
        self.assertEqual(self.trade['remaining'], 3)
        self.assertEqual(len([f for f in self.fake.fills.values() if f['orderId'] == int(oid)]), 2)
        self.break_even_exit()
        self.assert_reconciled([('entry', 6), ('tp1_limit', 3), ('cancel', 0), ('reduce', 3)], start)

    def test_3b_partial_tp1_remainder_cancelled_by_barrier(self):
        start, tp1, oid = self.partial_tp1()
        self.hard_stop()
        sent = self.drain()
        self.assertEqual([(s['kind'], s['outcome']) for s in sent], [('cancel', 'accepted')])
        self.assertEqual(sent[0]['receipt'], {'cancel_targets': {oid: 'requested'}})
        snap = self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'cancelled')
        self.assertEqual((self.order_in(snap, tp1['id'])['filled_qty'], snap['position_qty'], snap['open_orders']),
                         (1, 5, []))
        flatten = self.command('flatten')
        self.assertEqual(flatten['status'], 'ready')
        # A pre-fill size (6) would flip the account; the bridge sizes only to the live 5.
        self.assert_refused_without_sending(self.stale_exit(6, 'stale-exit-6'), 'exceeds live position')
        self.run_until_closed()
        self.assert_reconciled([('entry', 6), ('tp1_limit', 3), ('cancel', 0), ('flatten', 5)], start)
        self.assertEqual(self.command('tp1_limit')['status'], 'cancelled')
        self.assertEqual(self.fake.orders[int(oid)]['ordStatus'], 'Canceled')
        self.assertEqual(self.fake.filled_qty(int(oid)), 1)


class CancelFillRace(ReplayBase):
    def stop_with_race(self, race):
        start = len(self.fake.position_history)
        self.enter('long')
        tp1 = self.command('tp1_limit')
        oid = self.journal.get(tp1['id'])['broker_order_id']
        self.fake.cancel_race = race
        self.hard_stop()
        sent = self.drain()
        self.assertEqual([s['kind'] for s in sent], ['cancel'])
        return start, tp1, oid, sent[0]

    def after_race(self, tp1):
        snap = self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled', timeout=8)
        self.assertEqual((snap['position_qty'], snap['open_orders']), (3, []))
        self.assertTrue(self.trade['tp1_done'])  # the fill is honoured even while closing
        flatten = self.command('flatten')
        self.assertEqual(flatten['status'], 'ready')
        self.assertEqual(flatten['alloc'], {'parent': 1, 'eod': 2})
        # Anything sized to the pre-race position (5) is refused before it reaches the exchange.
        self.assert_refused_without_sending(self.stale_exit(5, 'stale-exit-5'), 'exceeds live position')
        self.run_until_closed()

    def test_4a_tp1_fills_while_cancel_barrier_in_flight(self):
        start, tp1, oid, cancel = self.stop_with_race('fill_first')
        self.assertEqual((cancel['outcome'], cancel['receipt']), ('accepted', {'cancel_targets': {oid: 'already_terminal'}}))
        self.after_race(tp1)
        self.assert_reconciled([('entry', 5), ('tp1_limit', 2), ('cancel', 0), ('flatten', 3)], start)
        self.assertEqual(self.fake.orders[int(oid)]['ordStatus'], 'Filled')

    def test_4b_fill_events_arrive_after_cancel_refusal(self):
        """The broker says 'not working' before the fill reaches the bridge: the cancel is unknown."""
        start, tp1, oid, cancel = self.stop_with_race('fill_after_reply')
        self.assertEqual(cancel['outcome'], 'unknown')
        self.assertEqual(cancel['receipt'], {'cancel_targets': {oid: 'uncertain:UnknownReason'}})
        self.assertEqual(self.command('cancel')['status'], 'unknown')
        # Unknown is never replayed.
        before = self.fake.requests.count('order/cancelorder')
        outcome, receipt = self.ex.send(cancel['payload'])
        self.assertEqual((outcome, receipt.get('duplicate')), ('unknown', True))
        self.assertEqual(self.fake.requests.count('order/cancelorder'), before)
        # Protective exit still sizes to the live position once the fill is visible.
        self.after_race(tp1)
        self.assert_reconciled([('entry', 5), ('tp1_limit', 2), ('cancel', 0), ('flatten', 3)], start,
                               unresolved=[cancel['id']])
        # Entries stay blocked until a human resolves the cancel.
        later = 'soup-main:%s:%d:long' % (CONFIG['ticker'], self.now() // 5000 * 5000 + 5000)
        self.assert_refused_without_sending(self.synthetic('entry', 'entry-after-race', later, 1), 'unknown outcome')
        # `--cancel-settled`: the broker shows nothing working, so the cancel's job is done.
        self.journal.reconcile(cancel['id'], 'accepted', note='TP1 filled before the cancel; nothing left')
        self.assertEqual(self.journal.unresolved(), [])
        self.assertEqual(self.ex.send(self.synthetic('entry', 'entry-after-race-2', later, 1))[0], 'accepted')
        until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 1)
        self.assertEqual(self.ex.send(self.synthetic('flatten', 'exit-after-race-2', later, 1))[0], 'accepted')
        until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 0)


class RestartWithUnresolved(ReplayBase):
    def kill_during_tp1(self, reached_exchange):
        """Soup enters; the daemon dies while sending TP1, after the journal start record."""
        entry = entry_payload('long', at=self.now() // 5000 * 5000)
        with self.lock:
            self.assertEqual(self.engine.apply('pine', entry, self.now(), entries_enabled=True), 'entry_accepted')
        real = self.bridge.request
        kill = {}

        def request(endpoint, body=None, **kw):
            if endpoint == 'order/placeorder' and body['orderType'] == 'Limit':
                kill['fired'] = True
                if reached_exchange:
                    real(endpoint, body, **kw)
                raise Killed()
            return real(endpoint, body, **kw)

        self.bridge.request = request
        sent = self.drain(kill=kill)
        self.assertEqual([(s['kind'], s['outcome']) for s in sent], [('entry', 'accepted'), ('tp1_limit', 'unknown')])
        tp1 = self.command('tp1_limit')
        self.assertEqual(self.journal.get(tp1['id'])['outcome'], None)  # start record, no result
        # Process death: no result record, no detach. The new daemon reloads the page and re-reads the journal.
        self.bridge._heartbeat_stop.set()
        self.bridge.session.close()
        self.connect()
        c = self.journal.get(tp1['id'])
        self.assertEqual((c['outcome'], c['receipt']),
                         ('unknown', {'transport': 'bridge_restarted_after_dispatch_start'}))
        self.assertEqual([u['command_id'] for u in self.journal.unresolved()], [tp1['id']])
        until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 5)
        # Evidence is withheld, not guessed.
        snap, reason = self.ex.snapshot(self.trade['id'])
        self.assertIsNone(snap)
        self.assertEqual(reason, 'uncertain_dispatch:' + tp1['id'])
        return tp1, next(s for s in sent if s['kind'] == 'tp1_limit')['payload']

    def test_5a_unresolved_tp1_never_sent_blocks_entries_not_protective_exits(self):
        tp1, payload = self.kill_during_tp1(reached_exchange=False)
        self.assertEqual(self.fake.working_orders(), [])
        self.assertIsNone(self.state.order_id_for_tag(client_tag(tp1['id'])))
        orders = self.placeorders()
        # Neither side retries: the bridge reports the first outcome, Soup keeps it unknown.
        outcome, receipt = self.ex.send(payload)
        self.assertEqual((outcome, receipt.get('duplicate')), ('unknown', True))
        self.assertEqual(self.placeorders(), orders)
        self.assertEqual(self.engine.ready(), [])
        self.assertEqual(self.command('tp1_limit')['status'], 'unknown')
        later = 'soup-main:%s:%d:long' % (CONFIG['ticker'], self.now() // 5000 * 5000 + 5000)
        self.assert_refused_without_sending(self.synthetic('entry', 'entry-while-unknown', later, 1),
                                            'unknown outcome')
        # Soup's protective path: the cancel barrier goes out, but Soup then needs broker evidence
        # the bridge will not publish while a command is unknown. Soup waits; it sends no close.
        self.hard_stop()
        sent = self.drain()
        self.assertEqual([(s['kind'], s['outcome'], s['receipt']) for s in sent],
                         [('cancel', 'accepted', {'cancel_targets': {}})])
        self.assertIsNone(self.ex.snapshot(self.trade['id'])[0])
        self.assertEqual(self.engine.ready(), [])
        # Existing design: the unknown command does not block a protective market exit sized to the
        # live position (operator-issued here, since Soup is waiting for evidence).
        trade = self.trade['id']
        self.assert_refused_without_sending(self.synthetic('flatten', 'operator-exit-6', trade, 6),
                                            'exceeds live position')
        self.assertEqual(self.ex.send(self.synthetic('flatten', 'operator-exit', trade, 5))[0], 'accepted')
        until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 0)
        self.assertEqual(self.fake.working_orders(), [])
        self.assertTrue(all(sign >= 0 for _, _, sign in self.fake.position_history))
        # Flat, but entries stay blocked until the unknown command is reconciled by hand.
        self.assert_refused_without_sending(self.synthetic('entry', 'entry-flat-unknown', later, 1),
                                            'unknown outcome')
        self.journal.reconcile(tp1['id'], 'rejected', note='no order with its client tag at the broker')
        snap = until(lambda: self.ex.snapshot(trade)[0], what='snapshot after reconcile')
        self.assertEqual(snap['position_qty'], 0)
        self.assertEqual([(o['command_id'], o['status'], o['filled_qty']) for o in snap['orders']],
                         [(self.command('entry')['id'], 'filled', 5), ('operator-exit', 'filled', 5)])
        # Soup still holds its own TP1 as unknown and refuses evidence with an order it did not send.
        with self.lock:
            with self.assertRaisesRegex(Rejected, 'unknown/duplicate broker command'):
                self.engine.apply('broker', snap, self.now())
        self.assertEqual(self.ex.send(self.synthetic('entry', 'entry-after-reconcile', later, 1))[0], 'accepted')
        until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 1)
        self.assertEqual(self.ex.send(self.synthetic('flatten', 'exit-after-reconcile', later, 1))[0], 'accepted')
        until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 0)
        self.assertEqual(len(self.fake.orders), 4)  # entry, operator exit, later entry and exit; no TP1

    def test_5b_unresolved_tp1_that_reached_exchange_is_matched_by_tag_not_resent(self):
        start = len(self.fake.position_history)
        tp1, payload = self.kill_during_tp1(reached_exchange=True)
        tagged = [oid for oid, v in self.fake.versions.items() if v['text'] == client_tag(tp1['id'])]
        self.assertEqual(len(tagged), 1)
        # The publisher resolves tagged unknowns under the executor lock before each snapshot.
        with self.ex.lock:
            self.assertEqual(self.ex.auto_reconcile(), [tp1['id']])
        c = self.journal.get(tp1['id'])
        self.assertEqual((c['outcome'], c['broker_order_id']), ('accepted', str(tagged[0])))
        orders = self.placeorders()
        outcome, receipt = self.ex.send(payload)
        self.assertEqual((outcome, receipt.get('duplicate')), ('accepted', True))
        self.assertEqual(self.placeorders(), orders)
        snap = self.evidence(lambda s: s['open_orders'])
        self.assertEqual(snap['open_orders'], [str(tagged[0])])
        self.assertEqual(self.command('tp1_limit')['status'], 'accepted')  # Soup's unknown is now evidenced
        self.fill_tp1()
        self.break_even_exit()
        self.assert_reconciled([('entry', 5), ('tp1_limit', 2), ('cancel', 0), ('reduce', 3)], start)


class PublicationFailure(ReplayBase):
    def test_6_publisher_delivery_fails_mid_trade_then_recovers(self):
        endpoint = SoupBrokerEndpoint(self)
        self.addCleanup(endpoint.close)
        publisher = Publisher(self.ex, self.journal, self.state, 'soup', endpoint.url, None,
                              interval_s=0.25, linger_s=5)
        self.ex.watchdogs.append(publisher.watchdog)
        thread = threading.Thread(target=publisher.run, daemon=True)
        thread.start()

        def stop():
            publisher.stopping.set()
            with self.state.changed:
                self.state.changed.notify_all()
            thread.join(7)
        self.addCleanup(stop)

        start = len(self.fake.position_history)
        entry = entry_payload('long', at=self.now() // 5000 * 5000)
        with self.lock:
            self.assertEqual(self.engine.apply('pine', entry, self.now(), entries_enabled=True), 'entry_accepted')
        sent = self.drain()
        self.assertEqual([(s['kind'], s['outcome']) for s in sent], [('entry', 'accepted'), ('tp1_limit', 'accepted')],
                         sent)
        tp1 = self.command('tp1_limit')
        until(lambda: self.trade['entry_filled'] == 5 and tp1['id'] in self.trade['broker_orders']
              and publisher.failure is None, what='entry evidence delivered')

        # Soup's receiver refuses delivery while TP1 fills.
        endpoint.status = 503
        self.fake.set_price(tp1['price'])
        until(lambda: publisher.last.get('http_status') == 503 and self.state.net_position(ACCOUNT, CONTRACT) == 3,
              what='refused delivery')
        self.assertEqual(publisher.watchdog(), 'snapshot_publisher_failed:publish_not_acknowledged')
        health = self.ex.health()
        self.assertFalse(health['ok'])
        self.assertIn('snapshot_publisher_failed:publish_not_acknowledged', health['problems'])
        self.assertFalse(publisher.last['published'])
        # The daemon's health endpoint shows the same.
        token = 'h' * 32
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.ex, publisher, token))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:%d/health/%s' % (server.server_address[1], token)
        with urllib.request.urlopen(url, timeout=5) as r:
            report = json.loads(r.read())
        self.assertFalse(report['health']['ok'])
        self.assertIn('snapshot_publisher_failed:publish_not_acknowledged', report['health']['problems'])
        self.assertEqual((report['publisher']['published'], report['publisher'].get('http_status')), (False, 503))
        self.assertFalse(self.trade['tp1_done'])  # Soup has not seen the fill

        # Then Soup is unreachable altogether.
        dead = socket.socket()
        dead.bind(('127.0.0.1', 0))
        dead_url = 'http://127.0.0.1:%d/broker/offline-test-token' % dead.getsockname()[1]
        dead.close()
        last_refused = publisher.last['sequence']
        publisher.soup_url = dead_url
        until(lambda: publisher.last.get('sequence', 0) > last_refused and 'http_status' in publisher.last
              and publisher.last['http_status'] is None, what='unreachable delivery')
        self.assertFalse(publisher.last['published'])
        self.assertEqual(publisher.watchdog(), 'snapshot_publisher_failed:publish_not_acknowledged')
        # While evidence is not being delivered, the bridge refuses orders before sending anything.
        self.assert_refused_without_sending(self.stale_exit(1, 'exit-while-unpublished'),
                                            'snapshot_publisher_failed')

        refused = {seq for seq, status in endpoint.received if status != 202}
        self.assertTrue(refused)
        publisher.soup_url = endpoint.url
        endpoint.status = 202
        until(lambda: self.trade['tp1_done'] and publisher.failure is None, what='recovered delivery')
        self.assertIsNone(publisher.watchdog())
        self.assertTrue(self.ex.health()['ok'])
        self.assertEqual(self.trade['remaining'], 3)

        # The rest of the trade runs on published evidence only.
        t = self.trade
        self.skip_ahead(t['runner_not_before'] - self.now() + 100)
        be = t['payload']['break_even_price']
        self.fake.set_price(be)
        self.quote(be)
        deadline = time.monotonic() + 15
        while self.trade['phase'] != 'closed':
            self.assertLess(time.monotonic(), deadline, self.trade)
            self.drain()
            time.sleep(0.1)
        until(lambda: publisher.failure is None, what='final delivery')
        stale = self.assert_reconciled([('entry', 5), ('tp1_limit', 2), ('cancel', 0), ('reduce', 3)], start,
                                       allow_stale=True)
        applied = [seq for seq, _, _, _ in self.broker_results]
        self.assertEqual(applied, sorted(set(applied)))
        self.assertFalse(refused & set(applied))  # refused deliveries were never applied late
        accepted = sum(r == 'broker_reconciled' for _, _, r, _ in self.broker_results)
        self.assertEqual(accepted + stale, len(self.broker_results))


if __name__ == '__main__':
    unittest.main(verbosity=2)
