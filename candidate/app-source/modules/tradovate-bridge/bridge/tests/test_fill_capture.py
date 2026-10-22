"""Read-only fill-price capture into each journal dir (`fills.jsonl`), the opt-in
`avg_fill_price` in Soup snapshots, and scripts/fill_report.py.

No browser: an in-memory exchange double emits Tradovate-shaped entities (orders, orderVersions,
fills, positions) into AccountState, per-account id blocks as Tradovate allocates them. Orders go
through the real Executor / FollowerExecutor. The Soup check imports Soup's own engine and
offline suites from TVBRIDGE_SOUP_NATIVE_PACKAGE (a native-fixes build; not bundled),
imported from a temporary copy; the source is never written.
"""
import copy
import calendar
import hashlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import harness  # noqa: F401  (puts the repository on sys.path; no browser is started here)
from harness import seed_market_price  # noqa: E402

from tvbridge import daemon, followers
from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.state import AccountState

try:
    from tvbridge import fills
except ImportError:  # a build without fill capture (negative control)
    fills = None

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / 'scripts' / 'fill_report.py'
CONTRACT = 2
TDATE = {'year': 2021, 'month': 3, 'day': 5}
BASE_MS = calendar.timegm((2021, 3, 5, 15, 35, 0, 0, 0, 0)) * 1000
ACCOUNTS = [{'id': 7, 'name': 'MAIN-ACCT'}, {'id': 8, 'name': 'F-B'}, {'id': 9, 'name': 'F-C'}]
TICKER = 'MNQZ2021'
SOUP_DEFAULT = None   # the Soup package is not bundled: set TVBRIDGE_SOUP_NATIVE_PACKAGE
# Snapshot order keys without fill prices (bracket trades add role/parent/quantity/stop_price).
ORDER_KEYS_PLAIN = {'command_id', 'broker_order_id', 'status', 'filled_qty', 'fill_time', 'role',
                   'parent_broker_order_id', 'quantity', 'stop_price'}


def iso(ms):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ms // 1000)) + '.%03dZ' % (ms % 1000)


def need_fills(test):
    if fills is None:
        test.fail('tvbridge.fills is missing (fill capture not in this release)')


class Exchange:
    """Tradovate as the page sees it, for one login. Market orders fill on request; limits and
    stops fill when a test calls fill(). Every request is recorded."""

    def __init__(self, state, price=11000.0):
        self.state, self.price, self.epoch = state, price, 'e1'
        self.requests = []
        self.ids = {a['id']: a['id'] * 1_000_000 for a in ACCOUNTS}  # per-account id blocks (as Tradovate)
        self.orders, self.versions, self.fills, self.positions = {}, {}, {}, {}
        self.ms = BASE_MS
        self.priced = True             # False: fills arrive without a price
        self.next_parts = None         # [(qty, price), ...] for the next market fill
        self.liquidate_reply_id = True
        self.sync()

    # --- page double API used by Executor ---
    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def sync(self, epoch=None):
        self.epoch = epoch or self.epoch
        data = {'accounts': ACCOUNTS, 'orders': list(self.orders.values()), 'fills': list(self.fills.values()),
                'orderVersions': list(self.versions.values()), 'positions': list(self.positions.values()),
                'contracts': [{'id': CONTRACT, 'name': 'MNQZ1'}]}
        self.state.load_sync(ACCOUNTS, copy.deepcopy(data), self.epoch)

    def request(self, endpoint, body=None, query='', timeout=5, expected_epoch=None):
        self.requests.append((endpoint, copy.deepcopy(body)))
        if expected_epoch is not None and expected_epoch != self.epoch:
            return {'ok': False, 'sent': False, 'code': 'stale_epoch'}
        if endpoint == 'order/placeorder':
            oid = self.order(body['accountId'], body['action'], body['orderQty'], body['orderType'],
                             price=body.get('price'), stop=body.get('stopPrice'), text=body.get('text'))
            if body['orderType'] == 'Market':
                self.fill_market(oid)
            return {'ok': True, 'sent': True, 's': 200, 'd': {'orderId': oid}}
        if endpoint == 'order/placeoso':
            acct = body['accountId']
            oid = self.order(acct, body['action'], body['orderQty'], 'Market', text=body.get('text'))
            legs = [self.order(acct, b['action'], body['orderQty'], b['orderType'], price=b.get('price'),
                               stop=b.get('stopPrice'), text=b.get('text'), status='Suspended')
                    for b in (body.get('bracket1'), body.get('bracket2')) if b]
            self.fill_market(oid)
            for lid in legs:
                self.set_status(lid, 'Working')
            d = {'orderId': oid, 'oso1Id': legs[0]}
            if len(legs) > 1:
                d['oso2Id'] = legs[1]
            return {'ok': True, 'sent': True, 's': 200, 'd': d}
        if endpoint == 'order/liquidateposition':
            acct = body['accountId']
            net = self.net(acct)
            for o in list(self.orders.values()):
                if o['accountId'] == acct and o['ordStatus'] in ('Working', 'Suspended'):
                    self.set_status(o['id'], 'Canceled')
            oid = None
            if net:
                oid = self.order(acct, 'Sell' if net > 0 else 'Buy', abs(net), 'Market')
                self.fill_market(oid)
            return {'ok': True, 'sent': True, 's': 200, 'd': {'orderId': oid} if self.liquidate_reply_id and oid else {}}
        if endpoint == 'order/modifyorder':
            oid = body['orderId']
            acct = self.orders[oid]['accountId']
            v = dict(self.latest(oid), id=self.next_id(acct), orderQty=body['orderQty'], text=body.get('text'))
            if 'stopPrice' in body:
                v['stopPrice'] = body['stopPrice']
            self.emit('orderVersion', v)
            return {'ok': True, 'sent': True, 's': 200, 'd': {'commandId': self.next_id(acct)}}
        if endpoint == 'order/cancelorder':
            self.set_status(body['orderId'], 'Canceled')
            return {'ok': True, 'sent': True, 's': 200, 'd': {}}
        return {'ok': False, 'sent': False, 'code': 'unsupported'}

    # --- exchange internals ---
    def next_id(self, acct):
        self.ids[acct] += 1
        return self.ids[acct]

    def ts(self):
        self.ms += 250
        return iso(self.ms)

    def emit(self, kind, entity, event='Created'):
        store = {'order': self.orders, 'orderVersion': self.versions, 'fill': self.fills,
                 'position': self.positions}[kind]
        if event == 'Deleted':
            store.pop(entity['id'], None)
        else:
            store[entity['id']] = copy.deepcopy(entity)
        self.state.apply_props({'entityType': kind, 'eventType': event, 'entity': copy.deepcopy(entity)})

    def order(self, acct, action, qty, otype, price=None, stop=None, text=None, status='Working'):
        oid = self.next_id(acct)
        self.emit('order', {'id': oid, 'accountId': acct, 'contractId': CONTRACT, 'action': action,
                            'ordStatus': status, 'timestamp': self.ts()})
        v = {'id': self.next_id(acct), 'orderId': oid, 'orderQty': qty, 'orderType': otype}
        if price is not None:
            v['price'] = price
        if stop is not None:
            v['stopPrice'] = stop
        if text:
            v['text'] = text
        self.emit('orderVersion', v)
        return oid

    def latest(self, oid):
        return max((v for v in self.versions.values() if v['orderId'] == oid), key=lambda v: v['id'])

    def set_status(self, oid, status):
        self.emit('order', dict(self.orders[oid], ordStatus=status), 'Updated')

    def filled(self, oid):
        return sum(f['qty'] for f in self.fills.values() if f['orderId'] == oid)

    def net(self, acct):
        p = self.positions.get(acct * 10 + 1)
        return p['netPos'] if p else 0

    def fill_market(self, oid):
        parts, self.next_parts = self.next_parts, None
        self.fill(oid, parts or [(None, self.price)])

    def fill(self, oid, parts, priced=None):
        """parts: [(qty or None for the rest, price)]. Returns the new fill ids."""
        o = self.orders[oid]
        acct, total = o['accountId'], self.latest(oid)['orderQty']
        ids = []
        for qty, price in parts:
            qty = qty or total - self.filled(oid)
            fid = self.next_id(acct)
            f = {'id': fid, 'orderId': oid, 'contractId': CONTRACT, 'timestamp': self.ts(), 'tradeDate': TDATE,
                 'action': o['action'], 'qty': qty, 'active': True}
            if (self.priced if priced is None else priced) and price is not None:
                f['price'] = price
            self.emit('fill', f)
            ids.append(fid)
            sign = 1 if o['action'] == 'Buy' else -1
            self.emit('position', {'id': acct * 10 + 1, 'accountId': acct, 'contractId': CONTRACT,
                                   'netPos': self.net(acct) + sign * qty, 'tradeDate': TDATE,
                                   'timestamp': iso(self.ms)}, 'Updated')
        self.set_status(oid, 'Filled' if self.filled(oid) >= total else 'Working')
        return ids


def extras(cid, tid, **kw):
    return dict({'soup_command_id': cid, 'soup_trade_id': tid}, **kw)


def p_bracket(cid, tid, qty=4, ref=11000.0, stop=10985.5, tp=11024.0, tp_qty=2):
    return {'ticker': TICKER, 'action': 'buy', 'orderType': 'market', 'quantity': qty,
            'extras': extras(cid, tid, bridge_kind='bracket_entry', reference_price=ref, stop_price=stop,
                             tp1_price=tp, tp1_quantity=tp_qty)}


def p_modify(cid, tid, price, qty=None):
    e = extras(cid, tid, bridge_kind='modify_stop', stop_price=price)
    if qty is not None:
        e['stop_quantity'] = qty
    return {'ticker': TICKER, 'action': 'modify_stop', 'extras': e}


def p_flatten(cid, tid):
    return {'ticker': TICKER, 'action': 'flatten_all', 'extras': extras(cid, tid, bridge_kind='flatten_all')}


def p_entry(cid, tid, qty):
    return {'ticker': TICKER, 'action': 'buy', 'orderType': 'market', 'quantity': qty, 'extras': extras(cid, tid)}


def p_tp1(cid, tid, qty, price):
    return {'ticker': TICKER, 'action': 'exit', 'orderType': 'limit', 'limitPrice': price, 'quantity': qty,
            'cancel': False, 'extras': extras(cid, tid)}


def p_exit(cid, tid, qty):
    return {'ticker': TICKER, 'action': 'exit', 'orderType': 'market', 'quantity': qty, 'cancel': False,
            'extras': extras(cid, tid)}


def read_fills(directory):
    path = Path(directory) / 'fills.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class Bench:
    """One login: the leader (7) and, optionally, followers B-style (8) and C-style (9)."""

    def __init__(self, test, with_followers=False, **cfg_overrides):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = AccountState()
        self.ex = Exchange(self.state)
        self.cfg = {'ticker': TICKER, 'account_name': 'MAIN-ACCT', 'account_id': 7, 'soup_account_id': 'Account A',
                    'max_contracts': 10, 'tick_size': .25, 'journal_dir': str(self.tmp / 'journal'),
                    'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED'), 'bracket_orders_enabled': True,
                    'flatten_all_enabled': True, 'tag_wait_s': 0.2, 'entry_settle_s': 0.5, 'flatten_settle_s': 0.3,
                    'flatten_cancel_grace_s': 0.05, 'terminal_settle_ms': 0, 'publish_mode': 'evidence'}
        self.cfg.update(cfg_overrides)
        if with_followers:
            self.cfg['followers'] = [
                {'key': 'account-b', 'account_name': 'F-B', 'account_id': 8,
                 'journal_dir': str(self.tmp / 'journal-account-b'),
                 'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED-account-b'), 'max_contracts': 10,
                 'prop_profile': 'cross-account-window', 'native_exits_enabled': True},
                {'key': 'account-c', 'account_name': 'F-C', 'account_id': 9,
                 'journal_dir': str(self.tmp / 'journal-account-c'),
                 'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED-account-c'), 'max_contracts': 10,
                 'prop_profile': 'unrestricted', 'native_exits_enabled': True}]
        self.main = Executor(self.ex, self.state, Journal(self.cfg['journal_dir']), self.cfg)
        self.main.resolve()
        self.fx = followers.build(self.ex, self.state, self.cfg, self.main) if with_followers else {}
        for ex in [self.main] + [f for f in self.fx.values() if isinstance(f, Executor)]:
            seed_market_price(ex, 11000.0)

    def capture(self):
        return fills.FillCapture.for_daemon(self.state, self.main, self.fx)

    def oid(self, executor, cid):
        return int(executor.journal.get(cid)['broker_order_id'])

    def leg(self, executor, cid, role):
        return int(executor.journal.get(cid)['legs'][role])


TID = 'soup:MNQZ2021:1:long'
TID2 = 'soup:MNQZ2021:2:long'


def run_bracket_trade(b, executor=None, prefix=''):
    """4-lot bracket: entry in two fills, staged TP1 2 lots, stop raised above the entry for 2, stop
    fills in two parts with slippage. Returns {role: order id}."""
    ex = executor or b.main
    cid = 'c-entry'
    b.ex.next_parts = [(1, 11000.25), (3, 11000.75)]
    out, receipt = ex.send(p_bracket(cid, TID))
    assert out == 'accepted', receipt
    assert receipt['tp1']['outcome'] == 'accepted', receipt
    full = prefix + cid
    ids = {'entry': b.oid(ex, full), 'stop': b.leg(ex, full, 'stop'), 'tp1': b.oid(ex, full + '#tp1')}
    b.ex.fill(ids['tp1'], [(None, 11024.0)])
    out, receipt = ex.send(p_modify('c-be', TID, 11001.5, 2))
    assert out == 'accepted', receipt
    b.ex.fill(ids['stop'], [(1, 11001.5), (None, 11000.75)])
    return ids


class LeaderFills(unittest.TestCase):
    def setUp(self):
        need_fills(self)
        self.b = Bench(self)
        self.cap = self.b.capture()

    def test_bracket_trade_entry_tp1_modified_stop_each_fill_once(self):
        ids = run_bracket_trade(self.b)
        self.assertEqual(self.cap.capture_once(), 5)
        recs = read_fills(self.b.cfg['journal_dir'])
        self.assertEqual([r['type'] for r in recs], ['fill'] * 5)
        self.assertEqual(len({r['fill_id'] for r in recs}), 5)
        by_role = {}
        for r in recs:
            by_role.setdefault(r['role'], []).append(r)
            self.assertEqual((r['trade_id'], r['account'], r['account_id'], r['schema']), (TID, 'main', 7, 1))
            self.assertEqual(r['attribution'], 'journal')
            self.assertIsInstance(r['observed_at'], int)
            self.assertEqual(r['fill_ms'], calendar.timegm(time.strptime(r['timestamp'][:19], '%Y-%m-%dT%H:%M:%S'))
                             * 1000 + int(r['timestamp'][20:23]))
        entry = by_role['entry']
        self.assertEqual([(r['qty'], r['price'], r['action']) for r in entry], [(1, 11000.25, 'Buy'), (3, 11000.75, 'Buy')])
        self.assertEqual({r['command_id'] for r in entry}, {'c-entry'})
        self.assertEqual({r['broker_order_id'] for r in entry}, {str(ids['entry'])})
        self.assertEqual(entry[0]['journal_price'], 11000.0)  # Soup's reference quote
        self.assertEqual(entry[0]['kind'], 'bracket_entry')
        tp1, = by_role['tp1']
        self.assertEqual((tp1['command_id'], tp1['kind'], tp1['parent_command_id'], tp1['price'], tp1['action'],
                          tp1['order_type'], tp1['order_price'], tp1['journal_price']),
                         ('c-entry#tp1', 'bracket_tp1', 'c-entry', 11024.0, 'Sell', 'Limit', 11024.0, 11024.0))
        stop = by_role['stop']
        self.assertEqual([(r['qty'], r['price']) for r in stop], [(1, 11001.5), (1, 11000.75)])
        for r in stop:
            self.assertEqual((r['command_id'], r['parent_command_id'], r['kind'], r['order_type'], r['order_stop_price'],
                              r['journal_price'], r['modify_command_id'], r['modify_stop_price'], r['broker_order_id']),
                             ('c-entry#stop', 'c-entry', 'bracket_entry', 'Stop', 11001.5, 10985.5, 'c-be', 11001.5,
                              str(ids['stop'])))
        # Redelivery: repeated passes, a resync on a new connection, a restarted recorder.
        self.assertEqual(self.cap.capture_once(), 0)
        self.b.ex.sync(epoch='e2')
        self.assertEqual(self.cap.capture_once(), 0)
        again = self.b.capture()
        self.assertEqual(again.capture_once(), 0)
        for fid in list(self.b.ex.fills):
            self.b.ex.emit('fill', self.b.ex.fills[fid], 'Updated')
        self.assertEqual(again.capture_once() + self.cap.capture_once(), 0)
        self.assertEqual(len(read_fills(self.b.cfg['journal_dir'])), 5)

    def test_flatten_all_liquidation_fills(self):
        b = self.b
        out, _ = b.main.send(p_bracket('c-e2', TID2, qty=2, tp=None, tp_qty=None))
        self.assertEqual(out, 'accepted')
        b.ex.price = 10991.25
        out, receipt = b.main.send(p_flatten('c-flat', TID2))
        self.assertEqual(out, 'accepted', receipt)
        self.cap.capture_once()
        recs = [r for r in read_fills(b.cfg['journal_dir']) if r['role'] == 'flatten']
        self.assertEqual([(r['command_id'], r['kind'], r['qty'], r['price'], r['action'], r['order_type'],
                           r['attribution'], r['broker_order_id']) for r in recs],
                         [('c-flat', 'flatten_all', 2, 10991.25, 'Sell', 'Market', 'journal', receipt['broker_order_id'])])

    def test_liquidation_reply_without_order_id_and_unknown_liquidation(self):
        b = self.b
        b.main.send(p_bracket('c-e3', TID2, qty=1, tp=None, tp_qty=None))
        b.ex.liquidate_reply_id = False
        out, receipt = b.main.send(p_flatten('c-flat3', TID2))
        self.assertEqual((out, receipt.get('matched_by_liquidation_order')), ('accepted', True))
        # A liquidation whose outcome stayed unknown without an order id (an older journal shape).
        tid = 'soup:MNQZ2021:9:long'
        b.main.journal.start('c-e4', trade_id=tid, kind='entry', side='long', quantity=2, tag=None, at=b.main.clock_ms())
        eid = b.ex.order(7, 'Buy', 2, 'Market')
        b.main.journal.finish('c-e4', 'accepted', {}, str(eid))
        b.ex.fill(eid, [(None, 11002.0)])
        baseline = b.state.max_order_id(7)
        b.main.journal.start('c-flat4', trade_id=tid, kind='flatten_all', side='long', quantity=2, tag=None,
                             at=b.main.clock_ms(), mode='liquidate', close_action='Sell', baseline_order_id=baseline)
        b.main.journal.finish('c-flat4', 'unknown', {'transport': 'uncertain'})
        liq = b.ex.order(7, 'Sell', 2, 'Market')
        b.ex.fill(liq, [(None, 10994.5)])
        self.cap.capture_once()
        recs = {r['command_id']: r for r in read_fills(b.cfg['journal_dir']) if r['role'] == 'flatten'}
        self.assertEqual(recs['c-flat3']['attribution'], 'journal')
        self.assertEqual((recs['c-flat4']['attribution'], recs['c-flat4']['broker_order_id'], recs['c-flat4']['price']),
                         ('liquidation_match', str(liq), 10994.5))
        self.assertEqual(recs['c-flat4']['trade_id'], tid)

    def test_fill_without_price_is_recorded_without_price_then_amended_once(self):
        b = self.b
        b.ex.priced = False
        out, _ = b.main.send(p_entry('c-plain', TID, 1))
        self.assertEqual(out, 'accepted')
        self.cap.capture_once()
        recs = read_fills(b.cfg['journal_dir'])
        self.assertEqual([(r['type'], r['price'], r['qty']) for r in recs], [('fill', None, 1)])
        self.assertNotIn('price', b.ex.fills[int(recs[0]['fill_id'])])  # nothing invented
        self.assertEqual(self.cap.capture_once(), 0)
        fid = int(recs[0]['fill_id'])
        b.ex.emit('fill', dict(b.ex.fills[fid], price=11003.75), 'Updated')
        self.assertEqual(self.cap.capture_once(), 1)
        self.assertEqual(self.cap.capture_once(), 0)
        self.assertEqual(self.b.capture().capture_once(), 0)  # restart: the amendment is known
        recs = read_fills(b.cfg['journal_dir'])
        self.assertEqual([(r['type'], r.get('price')) for r in recs], [('fill', None), ('fill_price', 11003.75)])
        self.assertEqual(recs[1]['command_id'], 'c-plain')

    def test_soup_plain_kinds_partial_tp1_and_market_reduce(self):
        b = self.b
        b.ex.next_parts = [(1, 11000.0), (2, 11000.5), (1, 11001.0)]
        self.assertEqual(b.main.send(p_entry('c-in', TID, 4))[0], 'accepted')
        self.assertEqual(b.main.send(p_tp1('c-tp', TID, 2, 11012.5))[0], 'accepted')
        tp = b.oid(b.main, 'c-tp')
        b.ex.fill(tp, [(1, 11012.5)])  # partial
        self.cap.capture_once()
        b.ex.fill(tp, [(None, 11012.5)])
        b.ex.price = 11006.25
        self.assertEqual(b.main.send(p_exit('c-out', TID, 2))[0], 'accepted')
        self.cap.capture_once()
        recs = read_fills(b.cfg['journal_dir'])
        got = [(r['command_id'], r['kind'], r['role'], r['qty'], r['price']) for r in recs]
        self.assertEqual(got, [('c-in', 'entry', 'entry', 1, 11000.0), ('c-in', 'entry', 'entry', 2, 11000.5),
                               ('c-in', 'entry', 'entry', 1, 11001.0), ('c-tp', 'tp1_limit', 'tp1', 1, 11012.5),
                               ('c-tp', 'tp1_limit', 'tp1', 1, 11012.5), ('c-out', 'exit_market', 'exit', 2, 11006.25)])
        self.assertEqual(fills.exact_avg_price(b.state.fills_for(b.oid(b.main, 'c-in'))), 11000.5)

    def test_foreign_and_hand_fills_are_not_recorded_and_deleted_fill_is_noted(self):
        b = self.b
        hand = b.ex.order(7, 'Buy', 1, 'Market')          # a hand order on the leader: not journaled
        b.ex.fill(hand, [(None, 11000.0)])
        other = b.ex.order(8, 'Buy', 1, 'Market')         # another account of the login
        b.ex.fill(other, [(None, 11000.0)])
        b.ex.emit('position', {'id': 71, 'accountId': 7, 'contractId': CONTRACT, 'netPos': 0, 'tradeDate': TDATE,
                               'timestamp': iso(b.ex.ms)}, 'Updated')
        self.assertEqual(self.cap.capture_once(), 0)
        self.assertEqual(read_fills(b.cfg['journal_dir']), [])
        b.ex.emit('position', {'id': 81, 'accountId': 8, 'contractId': CONTRACT, 'netPos': 0, 'tradeDate': TDATE,
                               'timestamp': iso(b.ex.ms)}, 'Updated')
        self.assertEqual(b.main.send(p_entry('c-one', TID, 1))[0], 'accepted')
        self.cap.capture_once()
        fid = int(read_fills(b.cfg['journal_dir'])[0]['fill_id'])
        b.ex.emit('fill', {'id': fid}, 'Deleted')
        self.assertEqual(self.cap.capture_once(), 1)
        self.assertEqual([r['type'] for r in read_fills(b.cfg['journal_dir'])], ['fill', 'fill_deleted'])

    def test_capture_never_calls_the_page(self):
        run_bracket_trade(self.b)
        before = list(self.b.ex.requests)

        class Tripwire:
            def __getattr__(self, name):
                raise AssertionError('fill capture touched the page: ' + name)

        self.b.main.bridge = Tripwire()
        try:
            self.assertEqual(self.cap.capture_once(), 5)
        finally:
            self.b.main.bridge = self.b.ex
        self.assertEqual(self.b.ex.requests, before)


class FollowerFills(unittest.TestCase):
    def setUp(self):
        need_fills(self)
        self.b = Bench(self, with_followers=True)
        self.cap = self.b.capture()

    def test_each_lane_journals_its_own_account_only(self):
        b = self.b
        fb, fc = b.fx['account-b'], b.fx['account-c']
        self.assertEqual([r.account for r in self.cap.recorders], ['main', 'account-b', 'account-c'])
        run_bracket_trade(b)
        run_bracket_trade(b, fb, prefix='f:account-b:')
        b.ex.next_parts = [(None, 11003.5)]
        self.assertEqual(fc.send(p_entry('c-entry', TID, 3))[0], 'accepted')
        # C aligns a hand-opened extra contract away: its own align_flatten is recorded, the hand order is not.
        hand = b.ex.order(9, 'Buy', 1, 'Market')
        b.ex.fill(hand, [(None, 11004.25)])
        b.ex.price = 11002.75
        out, receipt = fc.send({'ticker': TICKER, 'action': 'align_flatten', 'quantity': 4,
                                 'extras': {'soup_command_id': 'align-flatten:x', 'soup_trade_id': TID}})
        self.assertEqual(out, 'accepted', receipt)
        self.cap.capture_once()
        main, rb, rc = (read_fills(d) for d in (b.cfg['journal_dir'], b.tmp / 'journal-account-b',
                                                   b.tmp / 'journal-account-c'))
        self.assertEqual(({r['account_id'] for r in main}, {r['account_id'] for r in rb}, {r['account_id'] for r in rc}),
                         ({7}, {8}, {9}))
        self.assertEqual(({r['account'] for r in rb}, {r['account'] for r in rc}), ({'account-b'}, {'account-c'}))
        self.assertEqual(len(main), 5)
        self.assertEqual(len(rb), 5)
        self.assertTrue(all(r['command_id'].startswith('f:account-b:c-') for r in rb))
        self.assertEqual({r['role'] for r in rb}, {'entry', 'tp1', 'stop'})
        self.assertEqual([(r['command_id'], r['kind'], r['qty'], r['price']) for r in rc],
                         [('f:account-c:c-entry', 'entry', 3, 11003.5),
                          ('f:account-c:align-flatten:x', 'align_flatten', 4, 11002.75)])
        ids = [r['fill_id'] for r in main + rb + rc]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(str(hand), {r['broker_order_id'] for r in rc})

    def test_for_daemon_skips_unavailable_followers(self):
        cfg = dict(self.b.cfg, followers=self.b.cfg['followers'] + [
            {'key': 'gone', 'account_name': 'NOT-IN-LOGIN', 'journal_dir': str(self.b.tmp / 'journal-gone'),
             'kill_switch_file': str(self.b.tmp / 'K-gone'), 'max_contracts': 1, 'prop_profile': 'cross-account-window'}])
        fx = followers.build(self.b.ex, self.b.state, cfg, self.b.main)
        self.assertIsInstance(fx['gone'], followers.Unavailable)
        cap = fills.FillCapture.for_daemon(self.b.state, self.b.main,
                                           {k: e for k, e in fx.items() if isinstance(e, Executor)})
        self.assertEqual([r.account for r in cap.recorders], ['main', 'account-b', 'account-c'])


def script(b, executor):
    """The same order sequence for the with/without-capture comparison."""
    outcomes = []
    b.ex.next_parts = [(1, 11000.25), (3, 11000.75)]
    outcomes.append(executor.send(p_bracket('c-entry', TID)))
    tp1 = b.oid(executor, 'c-entry#tp1')
    b.ex.fill(tp1, [(None, 11024.0)])
    outcomes.append(executor.send(p_modify('c-be', TID, 11001.5, 2)))
    outcomes.append(executor.send(p_modify('c-be', TID, 11001.5, 2)))  # duplicate id: reported, never re-sent
    b.ex.fill(b.leg(executor, 'c-entry', 'stop'), [(None, 11000.75)])
    snap = executor.snapshot(TID)[0]
    outcomes.append(executor.send(p_bracket('c-e2', TID2, qty=2, tp=None, tp_qty=None)))
    outcomes.append(executor.send(p_flatten('c-flat', TID2)))
    return outcomes, snap


def journal_view(directory):
    recs = [json.loads(line) for line in (Path(directory) / 'commands.jsonl').read_text().splitlines()]
    for r in recs:
        r.pop('at', None)
    return recs


class CaptureNeverAffectsOrders(unittest.TestCase):
    def setUp(self):
        need_fills(self)

    def run_script(self, broken=None):
        b = Bench(self)
        cap = None
        if broken:
            cap = b.capture()
            broken(b, cap)
            cap.start()
        outcomes, snap = script(b, b.main)
        time.sleep(0.2)
        if cap:
            self.assertTrue(cap.thread.is_alive())
            cap.stop()
        snap = dict(snap, observed_at=None)
        return b, cap, outcomes, snap

    def test_sidecar_write_failure_and_capture_exceptions_change_nothing(self):
        base, _, out0, snap0 = self.run_script()

        def break_it(b, cap):
            os.mkdir(Path(b.cfg['journal_dir']) / 'fills.jsonl')  # every append fails (IsADirectoryError)
            extra = fills.FillRecorder(b.main, 'raising')
            extra._capture = lambda: 1 / 0
            cap.recorders.append(extra)

        with self.assertLogs('tvbridge', level='ERROR') as logs:
            b, cap, out1, snap1 = self.run_script(break_it)
        self.assertEqual(out1, out0)
        self.assertEqual(snap1, snap0)
        self.assertEqual(b.ex.requests, base.ex.requests)
        self.assertEqual(journal_view(b.cfg['journal_dir']), journal_view(base.cfg['journal_dir']))
        self.assertGreater(cap.recorders[0].errors, 0)
        self.assertEqual(cap.recorders[0].written, 0)
        self.assertGreater(cap.recorders[1].errors, 0)
        self.assertTrue(any('orders are unaffected' in line for line in logs.output))
        self.assertTrue(Path(b.cfg['journal_dir'], 'fills.jsonl').is_dir())
        self.assertEqual(sorted(p.name for p in Path(b.cfg['journal_dir']).iterdir()),
                         ['commands.jsonl', 'fills.jsonl', 'last-market-price.json', 'snapshot-sequence'])

    def test_working_capture_changes_nothing_either(self):
        base, _, out0, snap0 = self.run_script()
        b, cap, out1, snap1 = self.run_script(lambda b, cap: None)
        self.assertEqual((out1, snap1, b.ex.requests), (out0, snap0, base.ex.requests))
        self.assertEqual(journal_view(b.cfg['journal_dir']), journal_view(base.cfg['journal_dir']))
        self.assertEqual(len(read_fills(b.cfg['journal_dir'])), 6)

    def test_a_blocked_sidecar_write_holds_no_lock_the_order_path_needs(self):
        b = Bench(self)
        cap = b.capture()
        entered, release = threading.Event(), threading.Event()
        real = fills._append

        def slow_append(path, record):
            entered.set()
            release.wait(10)
            real(path, record)

        with patch.object(fills, '_append', slow_append):
            cap.start()
            self.addCleanup(cap.stop)
            self.addCleanup(release.set)
            self.assertEqual(b.main.send(p_entry('c-a', TID, 1))[0], 'accepted')
            self.assertTrue(entered.wait(5), 'capture thread did not start writing')
            started = time.monotonic()
            self.assertEqual(b.main.send(p_exit('c-b', TID, 1))[0], 'accepted')
            self.assertIsNotNone(b.main.snapshot(TID)[0])
            self.assertLess(time.monotonic() - started, 2.0)
            release.set()
        deadline = time.time() + 5
        while len(read_fills(b.cfg['journal_dir'])) < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual([r['command_id'] for r in read_fills(b.cfg['journal_dir'])], ['c-a', 'c-b'])


class SnapshotFillPrices(unittest.TestCase):
    def snaps(self, b, trade):
        out = {}
        for flag in (False, True):
            b.main.snapshot_fill_prices = flag
            s, reason = b.main.snapshot(trade)
            self.assertIsNotNone(s, reason)
            out[flag] = s
        b.main.snapshot_fill_prices = False
        return out

    def test_default_off_snapshot_has_no_fill_prices(self):
        b = Bench(self)
        run_bracket_trade(b)
        self.assertFalse(getattr(b.main, 'snapshot_fill_prices', False))
        s, _ = b.main.snapshot(TID)
        for o in s['orders']:
            self.assertLessEqual(set(o), ORDER_KEYS_PLAIN)
        self.assertEqual(set(s), {'event_id', 'sequence', 'observed_at', 'account_id', 'symbol', 'trade_id', 'side',
                                  'position_qty', 'orders', 'open_orders'})

    def test_flag_on_adds_only_avg_fill_price_when_every_fill_is_priced(self):
        need_fills(self)
        b = Bench(self, snapshot_fill_prices=True)
        self.assertTrue(b.main.snapshot_fill_prices)
        with self.assertRaises(ValueError):
            Bench(self, snapshot_fill_prices='yes')
        ids = run_bracket_trade(b)
        s = self.snaps(b, TID)
        for plain, priced in zip(s[False]['orders'], s[True]['orders']):
            extra = {k: v for k, v in priced.items() if k not in plain}
            self.assertEqual({k: v for k, v in priced.items() if k in plain}, plain)
            self.assertEqual(set(extra), {'avg_fill_price'} if plain['filled_qty'] else set())
        prices = {o['role']: o.get('avg_fill_price') for o in s[True]['orders']}
        self.assertEqual(prices, {'entry': 11000.625, 'stop': 11001.125, 'tp1': 11024.0})
        del ids
        strip = lambda snap: dict(snap, sequence=0, event_id='', observed_at=0,  # noqa: E731
                                  orders=[{k: v for k, v in o.items() if k != 'avg_fill_price'} for o in snap['orders']])
        self.assertEqual(strip(s[True]), strip(s[False]))

    def test_unpriced_fills_leave_the_snapshot_unchanged_until_the_price_arrives(self):
        need_fills(self)
        b = Bench(self)
        b.ex.priced = False
        b.main.send(p_entry('c-np', TID, 3))
        s = self.snaps(b, TID)
        self.assertEqual([set(o) for o in s[True]['orders']], [set(o) for o in s[False]['orders']])
        self.assertEqual(dict(s[True], sequence=0, event_id='', observed_at=0),
                         dict(s[False], sequence=0, event_id='', observed_at=0))
        fid = next(iter(b.ex.fills))
        b.ex.emit('fill', dict(b.ex.fills[fid], price=11002.25), 'Updated')
        s2 = self.snaps(b, TID)
        entry = s2[True]['orders'][0]
        self.assertEqual((entry['avg_fill_price'], entry['filled_qty'], entry['fill_time']),
                         (11002.25, 3, s[True]['orders'][0]['fill_time']))  # fill_time stable
        # No fills at all: never a key, flag on or off.
        b2 = Bench(self)
        b2.main.journal.start('c-w', trade_id=TID, kind='entry', side='long', quantity=1, tag=None, at=1)
        wid = b2.ex.order(7, 'Buy', 1, 'Limit', price=10900.0)
        b2.main.journal.finish('c-w', 'accepted', {}, str(wid))
        s3 = self.snaps(b2, TID)
        self.assertEqual([set(o) for o in s3[True]['orders']], [{'command_id', 'broker_order_id', 'status', 'filled_qty'}])


def find_soup():
    """The native-exits Soup package, as test_native_exits finds it."""
    for p in (os.environ.get('TVBRIDGE_SOUP_NATIVE_PACKAGE'), SOUP_DEFAULT):
        core = Path(p or '') / 'engine' / 'core.py'
        if p and core.is_file() and 'validate_native_exits' in core.read_text() \
                and (Path(p) / 'tests' / 'test_native_exits.py').is_file():
            return Path(p)
    return None


SOUP_SUITES = ('test_pair', 'test_core_audit', 'test_late_price_crossings', 'test_tp1_offset', 'test_native_exits',
               'test_native_fixes', 'test_one_contract', 'test_final_integration', 'test_action_alerts')


def augment(p):
    """Soup broker payload -> the same with `avg_fill_price` on filled orders, absent on every third
    sequence (a price that arrives later, or disappears). Deterministic for identical payloads."""
    if not isinstance(p, dict) or not isinstance(p.get('orders'), list):
        return p, 0
    q, added = copy.deepcopy(p), 0
    seq = q.get('sequence')
    for o in q['orders']:
        if not isinstance(o, dict):
            continue
        if type(o.get('filled_qty')) is int and o['filled_qty'] > 0 and not (type(seq) is int and seq % 3 == 0):
            o['avg_fill_price'] = 10000.25 + 0.25 * o['filled_qty'] + (1 / 3)
            added += 1
        else:
            o.pop('avg_fill_price', None)
    return q, added


def strip_payload(p):
    if not isinstance(p, dict) or not isinstance(p.get('orders'), list):
        return p
    q = copy.deepcopy(p)
    for o in q['orders']:
        if isinstance(o, dict):
            o.pop('avg_fill_price', None)
    return q


def strip_state(data):
    d = copy.deepcopy(data)
    t = d.get('trade')
    if isinstance(t, dict):
        for o in (t.get('broker_orders') or {}).values():
            o.pop('avg_fill_price', None)
    return d


@unittest.skipIf(find_soup() is None, 'Soup native-exits package not found; set TVBRIDGE_SOUP_NATIVE_PACKAGE')
class SoupAcceptsFillPrices(unittest.TestCase):
    """Soup's own offline suites, with every broker snapshot carrying `avg_fill_price`. Each
    Engine.broker() call is also replayed without the key: result, error and state must match."""

    def test_soup_suites_pass_and_decide_identically_with_avg_fill_price(self):
        src = find_soup()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = tmp / 'package'
        shutil.copytree(src, pkg, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        dirs = [pkg / 'engine', pkg / 'tests', pkg / 'tests' / 'support']
        names = {p.stem for d in dirs for p in d.glob('*.py')}
        saved_modules = {n: sys.modules.pop(n) for n in list(sys.modules) if n in names}
        saved_path, saved_bytecode = list(sys.path), sys.dont_write_bytecode
        sys.path[:0] = [str(d) for d in dirs]
        sys.dont_write_bytecode = True
        stats = {'calls': 0, 'augmented': 0, 'divergent': []}
        try:
            core = importlib.import_module('core')
            orig_apply, orig_broker = core.Engine.apply, core.Engine.broker

            def apply(engine, channel, p, now_ms, **kw):
                if channel == 'broker':
                    p, added = augment(p)
                    stats['augmented'] += added
                return orig_apply(engine, channel, p, now_ms, **kw)

            def broker(engine, p, now):
                # Shadow first: the payload without the key, on a copy of the state. Then the real
                # call (with the key) on the engine's own state object, so references tests hold stay valid.
                stats['calls'] += 1
                own, ev0 = engine.data, engine.refusal_evidence
                engine.data = copy.deepcopy(own)
                err2 = None
                try:
                    r2 = orig_broker(engine, strip_payload(p), now)
                except Exception as e:  # noqa: BLE001
                    err2, r2 = e, None
                shadow, ev2 = engine.data, engine.refusal_evidence
                engine.data, engine.refusal_evidence = own, ev0
                err1 = None
                try:
                    r1 = orig_broker(engine, p, now)
                except Exception as e:  # noqa: BLE001
                    err1, r1 = e, None
                mine, theirs = strip_state(engine.data), strip_state(shadow)  # earlier snapshots' keys too
                same = (r1 == r2 and type(err1) is type(err2) and str(err1) == str(err2)
                        and mine == theirs and engine.refusal_evidence == ev2)
                if not same:
                    stats['divergent'].append({'r1': r1, 'r2': r2, 'e1': repr(err1), 'e2': repr(err2),
                                               'state_differs': sorted(k for k in theirs if mine.get(k) != theirs.get(k)),
                                               'evidence_same': engine.refusal_evidence == ev2})
                if err1 is not None:
                    raise err1
                return r1

            with patch.object(core.Engine, 'apply', apply), patch.object(core.Engine, 'broker', broker):
                loader = unittest.TestLoader()
                present = [n for n in SOUP_SUITES if (pkg / 'tests' / (n + '.py')).is_file()]
                suite = unittest.TestSuite(loader.loadTestsFromModule(importlib.import_module(n)) for n in present)
                stream = io.StringIO()
                result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        finally:
            for n in [n for n in list(sys.modules) if n in names]:
                del sys.modules[n]
            sys.modules.update(saved_modules)
            sys.path[:], sys.dont_write_bytecode = saved_path, saved_bytecode
        self.assertTrue(result.wasSuccessful(), stream.getvalue()[-4000:])
        self.assertIn('test_native_exits', present)
        self.assertGreater(result.testsRun, 100)
        self.assertGreater(stats['calls'], 50)
        self.assertGreater(stats['augmented'], 50)
        self.assertEqual(stats['divergent'], [])
        print('\n  Soup suites with avg_fill_price: %d tests OK; broker() calls %d, orders priced %d, divergences 0'
              % (result.testsRun, stats['calls'], stats['augmented']), file=sys.stderr)


class DaemonFlags(unittest.TestCase):
    def test_invalid_flags_stop_the_daemon_before_the_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ({'fill_capture_enabled': 'yes'}, {'snapshot_fill_prices': 1}):
                cfg = Path(tmp) / 'config.json'
                cfg.write_text(json.dumps(dict({'ticker': TICKER, 'journal_dir': tmp, 'max_contracts': 10}, **bad)))
                with patch.object(sys, 'argv', ['daemon', '--config', str(cfg)]), \
                        patch.dict(os.environ, {'TVBRIDGE_TOKEN': 'x' * 32}), \
                        patch.object(daemon, 'Bridge', side_effect=AssertionError('browser touched')):
                    with self.assertRaises(SystemExit) as cm:
                        daemon.main()
                self.assertIn('must be true or false', str(cm.exception))


class PageBridge:
    """Stands in for tvbridge.bridge.Bridge inside daemon.main(): the in-memory Exchange on the
    daemon's own AccountState. No browser, no socket to Tradovate."""
    instances = []

    def __init__(self, state, **kw):
        self.ex = Exchange(state)
        self.lease_lost = threading.Event()
        self.lease_lost_reason = self.stop_reason = None
        PageBridge.instances.append(self)

    def attach(self, reload=True):
        pass

    def wait_ready(self, timeout=60):
        return self.ex.status()

    def status(self):
        return self.ex.status()

    def request(self, *a, **kw):
        return self.ex.request(*a, **kw)

    def detach(self):
        return True


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def wait_until(pred, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


DAEMON_TOKEN = 'fill-capture-daemon-token-0123456789'


class DaemonWiring(unittest.TestCase):
    """daemon.main() end to end with the page double: capture starts after the followers are built,
    records each account into its own journal dir, stops with the daemon, and a capture that
    cannot start never stops the daemon or an order."""

    def run_daemon(self, extra_cfg=None, broken=False):
        import signal
        import urllib.request
        PageBridge.instances = []
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        port = free_port()
        cfg = {'account_name': 'MAIN-ACCT', 'account_id': 7, 'soup_account_id': 'Account A', 'ticker': TICKER,
               'tick_size': .25, 'max_contracts': 10, 'journal_dir': str(tmp / 'journal'),
               'kill_switch_file': str(tmp / 'ENTRIES-DISABLED'), 'devtools_port': 1, 'page_url_contains': 'x',
               'trade_url_pattern': 'x', 'publish_mode': 'evidence', 'listen_port': port, 'shutdown_drain_s': 2,
               'bracket_orders_enabled': True, 'flatten_all_enabled': True, 'tag_wait_s': 0.2,
               'entry_settle_s': 0.5, 'terminal_settle_ms': 0,
               'followers': [{'key': 'account-b', 'account_name': 'F-B', 'account_id': 8,
                              'journal_dir': str(tmp / 'journal-account-b'),
                              'kill_switch_file': str(tmp / 'ENTRIES-DISABLED-account-b'), 'max_contracts': 10,
                              'prop_profile': 'cross-account-window'}]}
        cfg.update(extra_cfg or {})
        (tmp / 'config.json').write_text(json.dumps(cfg))
        # the leader's bracket needs a known market price; a fresh fake exchange has no fill yet.
        from tvbridge import protocol
        from tvbridge.executor import MARKET_PRICE_NAME
        (tmp / 'journal').mkdir(exist_ok=True)
        (tmp / 'journal' / MARKET_PRICE_NAME).write_text(json.dumps(
            {'symbol': protocol.tradovate_symbol(TICKER), 'price': 11000.0, 'at_ms': int(time.time() * 1000)}))
        results = {}

        def post(path, payload):
            req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=json.dumps(payload).encode(),
                                         method='POST', headers={'Content-Type': 'application/json'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=10) as r:
                return json.loads(r.read())

        def drive(before):
            try:
                assert wait_until(lambda: signal.getsignal(signal.SIGTERM) is not before)
                results['leader'] = post('/order/' + DAEMON_TOKEN, p_bracket('c-d1', TID, qty=3, tp=None, tp_qty=None))
                results['follower'] = post('/follower/%s/account-b/order' % DAEMON_TOKEN, p_entry('c-d1', TID, 2))
                results['threads'] = sorted(t.name for t in threading.enumerate())
                wait_until(lambda: len(read_fills(tmp / 'journal')) >= 1 and len(read_fills(tmp / 'journal-account-b')) >= 1,
                           3 if broken or (extra_cfg or {}).get('fill_capture_enabled') is False else 10)
            finally:
                os.kill(os.getpid(), signal.SIGTERM)

        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        threading.Thread(target=drive, args=(before,), daemon=True).start()
        patches = [patch.object(daemon, 'Bridge', PageBridge), patch.object(sys, 'argv', ['daemon', '--config', str(tmp / 'config.json')]),
                   patch.dict(os.environ, {'TVBRIDGE_TOKEN': DAEMON_TOKEN}), patch('logging.basicConfig')]
        if broken:
            patches.append(patch.object(fills.FillCapture, 'for_daemon', side_effect=RuntimeError('cannot start')))
        for pt in patches:
            pt.start()
        try:
            with self.assertLogs('tvbridge', level='INFO') as logs:
                self.assertIsNone(daemon.main())
        finally:
            for pt in reversed(patches):
                pt.stop()
        results['logs'] = logs.output
        results['after'] = sorted(t.name for t in threading.enumerate())
        return tmp, results

    def test_daemon_records_leader_and_follower_fills_in_their_own_dirs(self):
        need_fills(self)
        tmp, r = self.run_daemon()
        self.assertEqual((r['leader']['outcome'], r['follower']['outcome']), ('accepted', 'accepted'))
        self.assertIn('fill-capture', r['threads'])
        self.assertNotIn('fill-capture', r['after'])
        self.assertTrue(any('fill capture on: main, account-b' in line for line in r['logs']))
        lead, fol = read_fills(tmp / 'journal'), read_fills(tmp / 'journal-account-b')
        self.assertEqual([(x['command_id'], x['account_id'], x['qty'], x['price']) for x in lead],
                         [('c-d1', 7, 3, 11000.0)])
        self.assertEqual([(x['command_id'], x['account_id'], x['qty'], x['price']) for x in fol],
                         [('f:account-b:c-d1', 8, 2, 11000.0)])

    def test_capture_that_cannot_start_or_is_off_changes_nothing_for_orders(self):
        need_fills(self)
        for extra, broken in (({}, True), ({'fill_capture_enabled': False}, False)):
            with self.subTest(broken=broken, **extra):
                tmp, r = self.run_daemon(extra, broken)
                self.assertEqual((r['leader']['outcome'], r['follower']['outcome']), ('accepted', 'accepted'))
                self.assertNotIn('fill-capture', r['threads'])
                self.assertFalse((tmp / 'journal' / 'fills.jsonl').exists())
                self.assertFalse((tmp / 'journal-account-b' / 'fills.jsonl').exists())
                self.assertEqual(any('fill capture not started (RuntimeError)' in line for line in r['logs']), broken)


def tree_digest(root):
    out = {}
    for p in sorted(Path(root).rglob('*')):
        out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'dir'
    return out


class FillReport(unittest.TestCase):
    def setUp(self):
        need_fills(self)
        if not REPORT.is_file():
            self.fail('scripts/fill_report.py is missing')
        self.b = Bench(self, with_followers=True)
        run_bracket_trade(self.b)
        run_bracket_trade(self.b, self.b.fx['account-b'], prefix='f:account-b:')
        self.b.capture().capture_once()
        old = self.b.tmp / 'journal-account-c'   # C traded before fill capture: journal, no fills.jsonl
        j = Journal(old)
        j.start('f:account-c:c-old', trade_id=TID, kind='entry', side='long', quantity=2, tag='DLx', at=1)
        j.finish('f:account-c:c-old', 'accepted', {'broker_order_id': '9000001'}, '9000001')
        self.dirs = [('main', self.b.tmp / 'journal'), ('account-b', self.b.tmp / 'journal-account-b'),
                     ('account-c', old)]

    def run_report(self, *args):
        argv = [sys.executable, str(REPORT)]
        for label, d in self.dirs:
            argv += ['--journal', '%s=%s' % (label, d)]
        proc = subprocess.run(argv + list(args), capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_json_rows_avg_and_slippage(self):
        before = tree_digest(self.b.tmp)
        report = json.loads(self.run_report('--json', '--last', '0'))
        self.assertEqual(tree_digest(self.b.tmp), before)  # read-only: nothing written or created
        self.assertEqual([(a['label'], a['fills_file'], a['fill_records']) for a in report['accounts']],
                         [('main', True, 5), ('account-b', True, 5), ('account-c', False, 0)])
        trade, = report['trades']
        self.assertEqual(trade['trade_id'], TID)
        acct = {a['label']: {r['command_id']: r for r in a['commands']} for a in trade['accounts']}
        for label, pre in (('main', ''), ('account-b', 'f:account-b:')):
            rows = acct[label]
            e, s, t, m = (rows[pre + 'c-entry'], rows[pre + 'c-entry#stop'], rows[pre + 'c-entry#tp1'], rows[pre + 'c-be'])
            self.assertEqual((e['filled_qty'], e['avg_fill_price'], e['order_price'], e['slippage_points'],
                              e['slippage_points_total']), (4, 11000.625, 11000.0, 0.625, 2.5))
            self.assertEqual((s['filled_qty'], s['avg_fill_price'], s['order_price'], s['order_price_basis'],
                              s['slippage_points'], s['slippage_points_total']),
                             (2, 11001.125, 11001.5, 'stop (order)', 0.375, 0.75))
            self.assertEqual((t['avg_fill_price'], t['order_price'], t['slippage_points']), (11024.0, 11024.0, 0.0))
            self.assertEqual((m['kind'], m['order_price'], m['fills']), ('modify_stop', 11001.5, []))
            self.assertEqual([(f['qty'], f['price']) for f in e['fills']], [(1, 11000.25), (3, 11000.75)])
        old = acct['account-c']['f:account-c:c-old']
        self.assertEqual((old['fills'], old['avg_fill_price'], old['slippage_points']), ([], None, None))

    def test_text_output_and_old_journal(self):
        text = self.run_report('--utc')
        self.assertIn('account account-c', text)
        self.assertIn('no fill prices recorded', text)
        self.assertIn('=== trade ' + TID, text)
        self.assertIn('avg 11000.625', text)
        self.assertIn('slippage +0.375 pts/contract', text)
        self.assertIn('@ 11000.75', text)
        self.assertIn('Z', text)
        only_old = subprocess.run([sys.executable, str(REPORT), '--journal', 'old=%s' % self.dirs[2][1]],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(only_old.returncode, 0, only_old.stderr)
        self.assertIn('no fill prices recorded', only_old.stdout)
        root = subprocess.run([sys.executable, str(REPORT), '--root', str(self.b.tmp), '--json'],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(root.returncode, 0, root.stderr)
        self.assertEqual([a['label'] for a in json.loads(root.stdout)['accounts']], ['main', 'account-b', 'account-c'])


if __name__ == '__main__':
    unittest.main()
