"""Bracket entries, stop management and flatten_all (opt-in executor kinds).

Browser-free checks cover the payload schema, config gating and journal rules. The rest
runs the real page script in headless Chrome against the loopback fake exchange, whose
placeoso/modifyorder/liquidateposition follow Tradovate's documentation (see
fake_tradovate.py for which shapes are checked against Tradovate's web trader).
"""
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import logging

from harness import Harness, seed_market_price

from tvbridge import reconcile
from tvbridge.bridge import Bridge
from tvbridge.daemon import Publisher
from tvbridge.executor import Executor, client_tag, leg_command_id
from tvbridge.journal import Journal
from tvbridge.state import AccountState

TICKER = 'MNQZ2021'
ACCOUNT, OTHER, CONTRACT = 101, 102, 555
_seq = [0]
logging.getLogger('tvbridge').addHandler(logging.NullHandler())  # keep test output readable


def trade(side):
    _seq[0] += 1
    return 'bracket-test:MNQZ2021:%d:%s' % (1632147200000 + _seq[0] * 1000, side)


def bracket(cid, trade_id, qty, stop, tp=None, tp_qty=None, ref=10000.0):
    side = trade_id.rsplit(':', 1)[1]
    extras = {'soup_command_id': cid, 'soup_trade_id': trade_id, 'bridge_kind': 'bracket_entry',
              'reference_price': ref, 'stop_price': stop}
    if tp is not None:
        extras['tp1_price'] = tp
    if tp_qty is not None:
        extras['tp1_quantity'] = tp_qty
    return {'ticker': TICKER, 'action': 'buy' if side == 'long' else 'sell', 'orderType': 'market',
            'quantity': qty, 'extras': extras}


def modify(cid, trade_id, price=None, qty=None):
    extras = {'soup_command_id': cid, 'soup_trade_id': trade_id, 'bridge_kind': 'modify_stop'}
    if price is not None:
        extras['stop_price'] = price
    if qty is not None:
        extras['stop_quantity'] = qty
    return {'ticker': TICKER, 'action': 'modify_stop', 'extras': extras}


def flatten(cid, trade_id):
    return {'ticker': TICKER, 'action': 'flatten_all',
            'extras': {'soup_command_id': cid, 'soup_trade_id': trade_id, 'bridge_kind': 'flatten_all'}}


def legacy(kind, cid, trade_id, qty=None, price=None):
    """The webhook-shaped payloads Soup sends (see test_e2e for Soup's own builder)."""
    side = trade_id.rsplit(':', 1)[1]
    extras = {'soup_command_id': cid, 'soup_trade_id': trade_id}
    if kind == 'entry':
        return {'ticker': TICKER, 'action': 'buy' if side == 'long' else 'sell', 'orderType': 'market',
                'quantity': qty, 'quantityType': 'fixed_quantity', 'extras': extras}
    if kind == 'tp1_limit':
        return {'ticker': TICKER, 'action': 'exit', 'orderType': 'limit', 'limitPrice': price, 'quantity': qty,
                'quantityType': 'fixed_quantity', 'cancel': False, 'extras': extras}
    if kind == 'cancel':
        return {'ticker': TICKER, 'action': 'cancel', 'extras': extras}
    p = {'ticker': TICKER, 'action': 'exit', 'orderType': 'market', 'cancel': False, 'extras': extras}
    if qty is not None:
        p.update(quantity=qty, quantityType='fixed_quantity')
    return p


def wait_until(fn, timeout=5.0, what='condition'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(what + ' not met in time')


# ----------------------------------------------------------------------------------------
# Browser-free: schema, gating, journal and reconcile rules.
# ----------------------------------------------------------------------------------------

class PageDouble:
    def __init__(self, state):
        self.state = state
        data = {'accounts': [{'id': ACCOUNT, 'name': 'DEMO-1'}], 'orders': [], 'fills': [], 'orderVersions': [],
                'contracts': [{'id': CONTRACT, 'name': 'MNQZ1'}], 'positions': []}
        state.load_sync(data['accounts'], data, 'e1')
        self.sent = []

    def status(self):
        return {'state': 'authorized', 'epoch': 'e1', 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, *a, **k):
        self.sent.append(endpoint)
        return {'ok': False, 'sent': False, 'code': 'double'}


class SchemaAndGating(unittest.TestCase):
    def make(self, **cfg):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = AccountState()
        page = PageDouble(state)
        base = {'ticker': TICKER, 'account_name': 'DEMO-1', 'soup_account_id': 'X', 'max_contracts': 10,
                'tick_size': .25}
        ex = Executor(page, state, Journal(tmp.name), dict(base, **cfg))
        ex.resolve()
        return ex, page

    def test_off_by_default_and_nothing_sent(self):
        ex, page = self.make()
        t = trade('long')
        for p, flag in ((bracket('b1', t, 1, 9962.5), 'bracket_orders_enabled'),
                        (modify('m1', t, 9971.25), 'bracket_orders_enabled'),
                        (flatten('f1', t), 'flatten_all_enabled')):
            out, rec = ex.send(p)
            self.assertEqual(out, 'rejected')
            self.assertIn('disabled', rec['reason'])
            self.assertIn(flag, rec['reason'])
        # flatten_all alone does not enable brackets, and vice versa.
        ex, _ = self.make(flatten_all_enabled=True)
        self.assertIn('bracket_orders_enabled', ex.send(bracket('b2', t, 1, 9962.5))[1]['reason'])
        ex, _ = self.make(bracket_orders_enabled=True)
        self.assertIn('flatten_all_enabled', ex.send(flatten('f2', t))[1]['reason'])
        self.assertTrue(ex.bracket_protect)  # auto-protect defaults on inside bracket mode
        self.assertEqual(page.sent, [])
        self.assertEqual(ex.journal.commands, {})

    def test_soup_payloads_parse_identically_with_brackets_enabled(self):
        plain, _ = self.make()
        enabled, _ = self.make(bracket_orders_enabled=True, flatten_all_enabled=True)
        t = trade('short')
        for p in (legacy('entry', 'e', t, 3), legacy('tp1_limit', 'l', t, 1, 9993.75), legacy('cancel', 'c', t),
                  legacy('reduce', 'r', t, 2), legacy('flatten', 'f', t)):
            self.assertEqual(plain._parse(p), enabled._parse(p))
        # A null bridge_kind is still the extended schema (refused), never silently legacy.
        p = legacy('entry', 'e2', t, 1)
        p['extras']['bridge_kind'] = None
        with self.assertRaises(Exception):
            enabled._parse(p)

    def test_schema_is_strict(self):
        ex, page = self.make(bracket_orders_enabled=True, flatten_all_enabled=True)
        t = trade('long')
        bad = []
        p = bracket('s1', t, 2, 9962.5)
        p['extras']['bridge_kind'] = 'oco'
        bad.append((p, 'unsupported bridge_kind'))
        p = bracket('s2', t, 2, 9962.5)
        p['extras']['trail'] = 5
        bad.append((p, 'unexpected extras'))
        p = bracket('s3', t, 2, 9962.5)
        p['action'] = 'sell'
        bad.append((p, 'market entry on the trade side'))
        bad.append((bracket('s4', t, 2, 9962.6), 'on-tick'))
        bad.append((bracket('s5', t, 2, 9962.5, tp=10037.5, tp_qty=3), 'tp1_quantity'))
        bad.append((bracket('s6', t, 2, 9962.5, tp_qty=1), 'tp1_quantity needs tp1_price'))
        p = bracket('s7', t, 2, 9962.5)
        p['cancel'] = False
        bad.append((p, 'unexpected fields'))
        bad.append((modify('s8', t), 'stop_price and/or stop_quantity'))
        bad.append((modify('s9', t, qty=0), 'invalid stop_quantity'))
        p = flatten('s10', t)
        p['quantity'] = 1
        bad.append((p, 'flatten_all payload'))
        for p, why in bad:
            out, rec = ex.send(p)
            self.assertEqual(out, 'rejected', p)
            self.assertIn(why, rec['reason'])
        self.assertEqual(page.sent, [])

    def test_config_flags_must_be_booleans(self):
        for flag in ('bracket_orders_enabled', 'flatten_all_enabled', 'bracket_auto_protect'):
            with self.assertRaises(ValueError):
                self.make(**{flag: 'yes'})


class JournalRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.j = Journal(self.tmp.name)
        for cid, kind, extra in (('b', 'bracket_entry', {'tp1_leg': 'oso'}), ('b2', 'bracket_entry', {'tp1_leg': 'staged'}),
                                 ('m', 'modify_stop', {}), ('f', 'flatten_all', {'mode': 'liquidate'}),
                                 ('f2', 'flatten_all', {'mode': 'liquidate'}), ('a', 'auto_cancel', {})):
            self.j.start(cid, trade_id='t:long', kind=kind, side='long', quantity=1, tag=None, **extra)
            self.j.finish(cid, 'unknown', {})

    def cli(self, *args):
        with patch.object(sys, 'argv', ['reconcile', '--journal-dir', self.tmp.name, *args]), \
                redirect_stdout(io.StringIO()):
            reconcile.main()

    def test_legs_persist_and_count_as_accepted_orders(self):
        self.j.start('ok', trade_id='t2:long', kind='bracket_entry', side='long', quantity=2, tag='x', at=5)
        self.j.finish('ok', 'accepted', {}, '10', legs={'stop': 11, 'tp1': 12})
        again = Journal(self.tmp.name)
        self.assertEqual(again.get('ok')['legs'], {'stop': '11', 'tp1': '12'})
        self.assertEqual(sorted(again.accepted_order_ids()), ['10', '11', '12'])
        self.assertEqual(again.latest_trade_id(), None)
        self.assertEqual(again.latest_trade_id(kinds=('entry', 'bracket_entry')), 't2:long')

    def test_bracket_reconcile_needs_its_legs(self):
        with self.assertRaises(ValueError):
            self.j.reconcile('b', 'accepted', broker_order_id='1')
        with self.assertRaises(ValueError):
            self.j.reconcile('b', 'accepted', broker_order_id='1', legs={'stop': '2'})  # OSO tp1 leg missing
        with self.assertRaises(SystemExit):
            self.cli('--command', 'b', '--placed', '1', '--leg', 'stop=2', '--leg', 'stop=3')
        self.cli('--command', 'b', '--placed', '1', '--leg', 'stop=2', '--leg', 'tp1=3')
        self.assertEqual(Journal(self.tmp.name).get('b')['legs'], {'stop': '2', 'tp1': '3'})
        with self.assertRaises(SystemExit):
            self.cli('--command', 'b2', '--placed', '1', '--leg', 'stop=2', '--leg', 'tp1=3')  # staged: no tp1 leg
        self.cli('--command', 'b2', '--placed', '1', '--leg', 'stop=2')

    def test_no_order_kinds_and_flatten(self):
        with self.assertRaises(SystemExit):
            self.cli('--command', 'm', '--placed', '5')
        with self.assertRaises(SystemExit):
            self.cli('--command', 'm', '--cancel-settled')
        with self.assertRaises(SystemExit):
            self.cli('--command', 'b', '--settled')
        self.cli('--command', 'm', '--settled', '--note', 'stop shows the new price')
        self.cli('--command', 'a', '--not-placed')
        self.cli('--command', 'f', '--settled', '--note', 'flat, nothing working')
        self.cli('--command', 'f2', '--placed', '77')
        j = Journal(self.tmp.name)
        self.assertEqual([(j.get(c)['outcome'], j.get(c).get('broker_order_id')) for c in ('m', 'a', 'f', 'f2')],
                         [('accepted', None), ('rejected', None), ('accepted', None), ('accepted', '77')])


# ----------------------------------------------------------------------------------------
# Browser: real page script against the loopback fake exchange.
# ----------------------------------------------------------------------------------------

class BracketBase(unittest.TestCase):
    EXTRA = {}

    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.fake = cls.h.fake
        cls.cfg = cls.h.config(bracket_orders_enabled=True, flatten_all_enabled=True, **cls.EXTRA)
        cls.state = AccountState()
        cls.journal = Journal(cls.cfg['journal_dir'])
        cls.bridge = Bridge(cls.state, devtools_port=cls.h.devtools_port, page_url_contains='/trader/',
                            trade_url_pattern=cls.cfg['trade_url_pattern'])
        cls.bridge.attach(reload=True)
        cls.bridge.wait_ready(timeout=20)
        cls.ex = Executor(cls.bridge, cls.state, cls.journal, cls.cfg)
        cls.ex.resolve()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.detach()
        cls.h.close()

    def setUp(self):
        self.addCleanup(self.cleanup)
        # A bracket entry's reference check needs a known market price; the fresh fake has no fill yet.
        seed_market_price(self.ex, 10000.0)

    def cleanup(self):
        """Test-only: settle unknowns and flatten both accounts directly on the fake exchange."""
        for c in self.journal.unresolved():
            self.journal.reconcile(c['command_id'], 'rejected', note='test cleanup')
        with self.fake.lock:
            for oid in self.fake.working_orders():
                self.fake._cancel(oid)
            for (acct, cid), pos in list(self.fake.positions.items()):
                if pos['netPos']:
                    self.fake._place({'accountId': acct, 'symbol': 'MNQZ1', 'orderQty': abs(pos['netPos']),
                                      'orderType': 'Market', 'action': 'Sell' if pos['netPos'] > 0 else 'Buy'},
                                     lambda d, s=200: None)
            self.fake.price = 10000.0
        wait_until(lambda: not self.state.open_orders(ACCOUNT, CONTRACT) and
                   all(v == 0 for v in self.state.net_positions().values()), what='fake account clean')

    # --- helpers --------------------------------------------------------------------

    def net(self):
        return self.state.net_position(ACCOUNT, CONTRACT)

    def version(self, oid):
        return self.fake.versions[int(oid)]

    def status(self, oid):
        return self.fake.orders[int(oid)]['ordStatus']

    def requests_since(self, mark):
        return [r for r in self.fake.requests[mark:] if r.startswith('order/')]

    def snap(self, trade_id, ready=None, timeout=6.0):
        deadline = time.time() + timeout
        while True:
            snap, reason = self.ex.snapshot(trade_id)
            if snap and (ready is None or ready(snap)):
                return snap
            if time.time() > deadline:
                self.fail('no snapshot: %s %s' % (reason, snap))
            time.sleep(0.1)

    @staticmethod
    def conserved(snap):
        entry = sum(o['filled_qty'] for o in snap['orders'] if o.get('role') == 'entry')
        exits = sum(o['filled_qty'] for o in snap['orders'] if o.get('role') != 'entry')
        return entry - exits == snap['position_qty']

    def enter(self, side, qty, stop, tp=None, tp_qty=None):
        t = trade(side)
        cid = 'b-' + t.rsplit(':', 2)[1]
        out, rec = self.ex.send(bracket(cid, t, qty, stop, tp, tp_qty))
        self.assertEqual(out, 'accepted', rec)
        sign = 1 if side == 'long' else -1
        wait_until(lambda: self.net() == sign * qty, what='entry fill')
        stop_id = int(rec['legs']['stop'])
        wait_until(lambda: (self.state.order(stop_id) or {}).get('ordStatus') == 'Working', what='stop working')
        return t, cid, rec


class BracketLifecycle(BracketBase):
    def test_long_oso_bracket_tp_fills_and_oco_cancels_the_stop(self):
        mark = len(self.fake.requests)
        t, cid, rec = self.enter('long', 2, 9962.5, tp=10037.5)
        self.assertEqual(self.requests_since(mark), ['order/placeoso'])
        endpoint, body = self.fake.bodies[-1]
        self.assertEqual(endpoint, 'order/placeoso')
        self.assertEqual({k: body[k] for k in ('accountId', 'action', 'symbol', 'orderQty', 'orderType',
                                               'timeInForce', 'text', 'isAutomated')},
                         {'accountId': ACCOUNT, 'action': 'Buy', 'symbol': 'MNQZ1', 'orderQty': 2, 'orderType': 'Market',
                          'timeInForce': 'Day', 'text': client_tag(cid), 'isAutomated': True})
        self.assertEqual(body['bracket1'], {'action': 'Sell', 'orderType': 'Stop', 'stopPrice': 9962.5,
                                            'timeInForce': 'Day', 'text': client_tag(leg_command_id(cid, 'stop'))})
        self.assertEqual(body['bracket2'], {'action': 'Sell', 'orderType': 'Limit', 'price': 10037.5,
                                            'timeInForce': 'Day', 'text': client_tag(leg_command_id(cid, 'tp1'))})
        stop_id, tp_id = int(rec['legs']['stop']), int(rec['legs']['tp1'])
        self.assertNotIn('tp1', rec)  # M == N: no staged TP
        wait_until(lambda: self.status(tp_id) == 'Working')
        self.assertEqual((self.version(stop_id)['orderQty'], self.version(tp_id)['orderQty']), (2, 2))
        s = self.snap(t, lambda s: s['position_qty'] == 2 and len(s['open_orders']) == 2)
        roles = {o['role']: o for o in s['orders']}
        self.assertEqual(sorted(roles), ['entry', 'stop', 'tp1'])
        self.assertEqual((roles['entry']['status'], roles['entry']['filled_qty']), ('filled', 2))
        self.assertEqual((roles['stop']['status'], roles['stop']['broker_order_id'],
                          roles['stop']['parent_broker_order_id'], roles['stop']['command_id']),
                         ('working', str(stop_id), rec['broker_order_id'], leg_command_id(cid, 'stop')))
        self.assertEqual(roles['tp1']['parent_broker_order_id'], rec['broker_order_id'])
        self.assertEqual(sorted(s['open_orders']), sorted([str(stop_id), str(tp_id)]))
        self.assertTrue(self.conserved(s))
        self.assertEqual(self.ex.maintain_brackets(), [])  # nothing to do on a healthy bracket

        mark = len(self.fake.requests)
        self.fake.set_price(10037.5)
        wait_until(lambda: self.net() == 0, what='tp fill')
        s = self.snap(t, lambda s: s['open_orders'] == [] and
                      {o['role']: o['status'] for o in s['orders']}.get('stop') == 'cancelled')
        roles = {o['role']: o for o in s['orders']}
        self.assertEqual((roles['tp1']['status'], roles['tp1']['filled_qty']), ('filled', 2))
        self.assertEqual((roles['stop']['status'], roles['stop']['filled_qty']), ('cancelled', 0))
        self.assertTrue(self.conserved(s))
        self.assertEqual(self.requests_since(mark), [])  # the exchange's OCO did it, not the bridge
        self.assertFalse(self.ex.trade_active(t))

    def test_short_staged_tp1_is_a_separate_limit_after_the_fill(self):
        mark = len(self.fake.requests)
        t, cid, rec = self.enter('short', 3, 10037.5, tp=9962.5, tp_qty=1)
        self.assertEqual(self.requests_since(mark), ['order/placeoso', 'order/placeorder'])
        oso = [b for e, b in self.fake.bodies if e == 'order/placeoso'][-1]
        self.assertNotIn('bracket2', oso)
        self.assertEqual((oso['action'], oso['bracket1']['action'], oso['bracket1']['stopPrice']),
                         ('Sell', 'Buy', 10037.5))
        self.assertEqual(set(rec['legs']), {'stop'})
        tp1 = rec['tp1']
        self.assertEqual((tp1['sent'], tp1['outcome'], tp1['command_id']), (True, 'accepted', leg_command_id(cid, 'tp1')))
        tp_id = int(tp1['broker_order_id'])
        self.assertEqual({k: self.version(tp_id)[k] for k in ('orderQty', 'orderType', 'price', 'text')},
                         {'orderQty': 1, 'orderType': 'Limit', 'price': 9962.5,
                          'text': client_tag(leg_command_id(cid, 'tp1'))})
        self.assertEqual(self.fake.orders[tp_id]['action'], 'Buy')
        self.assertEqual(self.journal.get(leg_command_id(cid, 'tp1'))['kind'], 'bracket_tp1')
        s = self.snap(t, lambda s: len(s['open_orders']) == 2)
        roles = {o['role']: o for o in s['orders']}
        self.assertEqual((roles['tp1']['command_id'], roles['tp1']['parent_broker_order_id']),
                         (leg_command_id(cid, 'tp1'), rec['broker_order_id']))
        self.assertEqual(s['side'], 'short')
        self.assertTrue(self.conserved(s))

    def test_tp1_partial_then_stop_shrink_and_breakeven(self):
        history = len(self.fake.position_history)
        t, cid, rec = self.enter('long', 3, 9962.5, tp=10027.5, tp_qty=1)
        stop_id = int(rec['legs']['stop'])
        self.fake.set_price(10027.5)
        wait_until(lambda: self.net() == 2, what='TP1 fill')
        self.assertEqual(self.version(stop_id)['orderQty'], 3)  # oversized until someone shrinks it
        out, r = self.ex.send(modify('m-big-' + cid, t, qty=3))
        self.assertEqual(out, 'rejected')
        self.assertIn('exceeds the live position 2', r['reason'])
        entry_fill = self.state.avg_fill_price(int(rec['broker_order_id']))
        mark = len(self.fake.requests)
        out, r = self.ex.send(modify('m-be-' + cid, t, price=entry_fill, qty=2))
        self.assertEqual(out, 'accepted', r)
        self.assertEqual(self.requests_since(mark), ['order/modifyorder'])
        self.assertEqual(self.fake.bodies[-1], ('order/modifyorder', {
            'orderId': stop_id, 'orderQty': 2, 'orderType': 'Stop', 'stopPrice': entry_fill, 'timeInForce': 'Day',
            'text': client_tag('m-be-' + cid), 'isAutomated': True}))
        self.assertEqual((r['modified_order_id'], r['quantity'], r['stop_price']), (str(stop_id), 2, entry_fill))
        wait_until(lambda: (self.state.order_version(stop_id) or {}).get('orderQty') == 2)
        self.assertEqual(self.state.order_version(stop_id)['stopPrice'], 10000.0)
        # Price-only move keeps the (already fitting) quantity.
        out, r = self.ex.send(modify('m-trail-' + cid, t, price=10006.25))
        self.assertEqual((out, r['quantity']), ('accepted', 2), r)
        wait_until(lambda: self.state.order_version(stop_id)['stopPrice'] == 10006.25)
        # Same values again: accepted no-op, nothing sent.
        mark = len(self.fake.requests)
        out, r = self.ex.send(modify('m-same-' + cid, t, price=10006.25, qty=2))
        self.assertEqual((out, r['bridge']), ('accepted', 'noop'))
        self.assertEqual(self.requests_since(mark), [])
        # The runners stop out at the new level.
        self.fake.set_price(10006.25)
        wait_until(lambda: self.net() == 0, what='stop fill')
        s = self.snap(t, lambda s: s['open_orders'] == [] and s['position_qty'] == 0)
        roles = {o['role']: o for o in s['orders']}
        self.assertEqual((roles['stop']['status'], roles['stop']['filled_qty']), ('filled', 2))
        self.assertEqual((roles['tp1']['status'], roles['tp1']['filled_qty']), ('filled', 1))
        self.assertTrue(self.conserved(s))
        path = [net for a, _, net in self.fake.position_history[history:] if a == ACCOUNT]
        self.assertEqual(path, [3, 2, 0])  # never through flat

    def test_stop_hit_closes_all_and_oco_cancels_tp(self):
        t, cid, rec = self.enter('long', 2, 9962.5, tp=10037.5)
        mark = len(self.fake.requests)
        self.fake.set_price(9955.0)
        wait_until(lambda: self.net() == 0, what='stop fill')
        s = self.snap(t, lambda s: s['open_orders'] == [] and
                      {o['role']: o['status'] for o in s['orders']}.get('tp1') == 'cancelled')
        roles = {o['role']: o for o in s['orders']}
        self.assertEqual((roles['stop']['status'], roles['stop']['filled_qty']), ('filled', 2))
        self.assertEqual(roles['tp1']['filled_qty'], 0)
        self.assertEqual(self.requests_since(mark), [])
        self.assertEqual(self.fake.working_orders(), [])
        self.assertEqual(self.ex.maintain_brackets(), [])

    def test_staged_stop_hit_auto_protect_cancels_the_orphaned_tp(self):
        t, cid, rec = self.enter('long', 3, 9962.5, tp=10027.5, tp_qty=1)
        tp_id = int(rec['tp1']['broker_order_id'])
        self.fake.set_price(9955.0)
        wait_until(lambda: self.net() == 0, what='stop fill')
        self.assertEqual(self.status(tp_id), 'Working')  # no exchange link to the staged TP1
        wait_until(lambda: self.ex.maintain_brackets(), what='auto-protect action')
        auto = self.journal.get(leg_command_id(cid, 'auto-cancel'))
        self.assertEqual((auto['kind'], auto['outcome']), ('auto_cancel', 'accepted'))
        wait_until(lambda: self.status(tp_id) == 'Canceled')
        self.assertEqual(self.ex.maintain_brackets(), [])  # once, never repeated
        s = self.snap(t, lambda s: s['open_orders'] == [])
        self.assertTrue(self.conserved(s))

    def test_auto_protect_shrinks_an_oversized_stop_once(self):
        t, cid, rec = self.enter('long', 3, 9962.5, tp=10027.5, tp_qty=1)
        stop_id = int(rec['legs']['stop'])
        self.assertEqual(self.ex.maintain_brackets(), [])
        self.fake.set_price(10027.5)
        wait_until(lambda: self.net() == 2, what='TP1 fill')
        sent = wait_until(lambda: self.ex.maintain_brackets(), what='auto shrink')
        self.assertEqual(sent, [leg_command_id(cid, 'auto-shrink-2')])
        self.assertEqual(self.journal.get(sent[0])['outcome'], 'accepted')
        wait_until(lambda: self.state.order_version(stop_id)['orderQty'] == 2)
        self.assertEqual(self.state.order_version(stop_id)['stopPrice'], 9962.5)  # price untouched
        self.assertEqual(self.ex.maintain_brackets(), [])

    def test_foreign_fill_blocks_modify_and_auto_protect(self):
        t, cid, rec = self.enter('long', 2, 9962.5, tp=10037.5)
        with self.fake.lock:  # someone buys one more by hand on the same account and contract
            self.fake._place({'accountId': ACCOUNT, 'symbol': 'MNQZ1', 'orderQty': 1, 'orderType': 'Market',
                              'action': 'Buy', 'text': 'manual'}, lambda d, s=200: None)
        wait_until(lambda: self.net() == 3)
        mark = len(self.fake.requests)
        out, r = self.ex.send(modify('m-foreign-' + cid, t, qty=3))
        self.assertEqual(out, 'rejected')
        self.assertIn('does not match this trade', r['reason'])
        self.assertEqual(self.ex.maintain_brackets(), [])
        self.assertEqual(self.requests_since(mark), [])

    def test_modify_refusals(self):
        t0 = trade('long')
        out, r = self.ex.send(modify('m-none', t0, price=9993.75))
        self.assertIn('no accepted bracket entry', r['reason'])
        t, cid, rec = self.enter('long', 2, 9962.5, tp=10037.5)
        out, r = self.ex.send(modify('m-over-' + cid, t, qty=3))
        self.assertEqual(out, 'rejected')
        self.assertIn('exceeds the live position 2', r['reason'])
        out, r = self.ex.send(modify('m-cross-' + cid, t, price=10037.5))
        self.assertIn('protective side of the working take-profit', r['reason'])
        out, r = self.ex.send(modify('m-band-' + cid, t, price=9100.0))
        self.assertIn('more than 5.0% from the entry fill', r['reason'])
        self.assertEqual(self.ex.send(flatten('f-' + cid, t))[0], 'accepted')
        wait_until(lambda: self.net() == 0 and not self.state.open_orders(ACCOUNT, CONTRACT))
        mark = len(self.fake.requests)
        out, r = self.ex.send(modify('m-flat-' + cid, t, price=9971.25))
        self.assertEqual(out, 'rejected')
        self.assertIn('no working protective stop', r['reason'])
        self.assertEqual(self.requests_since(mark), [])


class FlattenAll(BracketBase):
    def test_flatten_all_with_stop_tp_and_position_is_one_request(self):
        t, cid, rec = self.enter('long', 2, 9962.5, tp=10037.5)
        stop_id, tp_id = int(rec['legs']['stop']), int(rec['legs']['tp1'])
        wait_until(lambda: len(self.state.open_orders(ACCOUNT, CONTRACT)) == 2)
        mark = len(self.fake.requests)
        out, r = self.ex.send(flatten('flat-' + cid, t))
        self.assertEqual(out, 'accepted', r)
        self.assertEqual(self.requests_since(mark), ['order/liquidateposition'])
        self.assertEqual(self.fake.bodies[-1], ('order/liquidateposition',
                                                {'accountId': ACCOUNT, 'contractId': CONTRACT, 'admin': False}))
        wait_until(lambda: self.net() == 0 and not self.state.open_orders(ACCOUNT, CONTRACT), what='flat')
        self.assertEqual((self.status(stop_id), self.status(tp_id)), ('Canceled', 'Canceled'))
        liq = int(r['broker_order_id'])
        self.assertEqual((self.fake.orders[liq]['action'], self.version(liq)['text'], self.fake.filled_qty(liq)),
                         ('Sell', 'Exit', 2))
        s = self.snap(t, lambda s: s['open_orders'] == [] and all(o['status'] in ('filled', 'cancelled')
                                                                  for o in s['orders']))
        roles = {o['role']: o for o in s['orders']}
        self.assertEqual((roles['flatten']['status'], roles['flatten']['filled_qty']), ('filled', 2))
        self.assertTrue(self.conserved(s))
        j = self.journal.get('flat-' + cid)
        self.assertEqual((j['kind'], j['outcome'], j['mode'], j['broker_order_id']),
                         ('flatten_all', 'accepted', 'liquidate', str(liq)))
        self.assertEqual(self.ex.maintain_brackets(), [])

    def test_flatten_all_when_flat_sends_nothing(self):
        t = trade('long')
        mark = len(self.fake.requests)
        out, r = self.ex.send(flatten('flat-noop', t))
        self.assertEqual((out, r['bridge']), ('accepted', 'noop'))
        self.assertEqual(self.requests_since(mark), [])
        self.assertEqual(self.journal.get('flat-noop')['mode'], 'noop')
        out, r = self.ex.send(flatten('flat-noop', t))
        self.assertEqual((out, r.get('duplicate')), ('accepted', True))
        snap, reason = self.ex.snapshot(t)
        self.assertEqual(snap['orders'], [])  # the no-op owns no order

    def test_flatten_all_flat_with_working_orders_cancels_them(self):
        with self.fake.lock:
            self.fake._place({'accountId': ACCOUNT, 'symbol': 'MNQZ1', 'orderQty': 1, 'orderType': 'Limit',
                              'price': 9250.0, 'action': 'Buy', 'text': 'manual'}, lambda d, s=200: None)
        wait_until(lambda: self.state.open_orders(ACCOUNT, CONTRACT))
        mark = len(self.fake.requests)
        out, r = self.ex.send(flatten('flat-cxl', trade('short')))
        self.assertEqual((out, r['mode']), ('accepted', 'cancel_only'), r)
        self.assertEqual(self.requests_since(mark), ['order/cancelorder'])
        wait_until(lambda: not self.state.open_orders(ACCOUNT, CONTRACT))

    def test_flatten_all_is_not_blocked_by_kill_switch_or_unknowns(self):
        t, cid, rec = self.enter('short', 1, 10037.5)
        self.journal.start('stray-unknown', trade_id='other:long', kind='entry', side='long', quantity=1, tag='none')
        self.journal.finish('stray-unknown', 'unknown', {})
        Path(self.cfg['kill_switch_file']).write_text('off')
        try:
            out, r = self.ex.send(flatten('flat-ks-' + cid, t))
        finally:
            Path(self.cfg['kill_switch_file']).unlink()
        self.assertEqual(out, 'accepted', r)
        wait_until(lambda: self.net() == 0 and not self.state.open_orders(ACCOUNT, CONTRACT))


class LostResponses(BracketBase):
    def test_lost_oso_reply_is_matched_by_tag_with_its_legs(self):
        t = trade('long')
        placed = self.fake.requests.count('order/placeoso')
        self.fake.drop_next.add('order/placeoso')
        out, rec = self.ex.send(bracket('lost-oso', t, 2, 9962.5, tp=10037.5))
        self.assertEqual((out, rec.get('matched_by_tag')), ('accepted', True), rec)
        self.assertEqual(set(rec['legs']), {'stop', 'tp1'})
        self.assertEqual(self.fake.requests.count('order/placeoso'), placed + 1)
        self.assertEqual(self.journal.get('lost-oso')['legs'], rec['legs'])

    def test_request_lost_is_unknown_blocks_entries_then_resolves_by_tag(self):
        t = trade('long')
        self.fake.swallow_endpoints.add('order/placeoso')
        out, rec = self.ex.send(bracket('gone-oso', t, 1, 9962.5))
        self.assertEqual(out, 'unknown', rec)
        snap, reason = self.ex.snapshot(t)
        self.assertIsNone(snap)
        self.assertTrue(reason.startswith('uncertain_dispatch'))
        for p in (bracket('gone-next', trade('long'), 1, 9962.5), legacy('entry', 'gone-legacy', trade('long'), 1)):
            out, r = self.ex.send(p)
            self.assertEqual(out, 'rejected')
            self.assertIn('unknown outcome', r['reason'])
        placed = self.fake.requests.count('order/placeoso')
        # The order turns up later (e.g. it was processed after all): resolved by tag, never re-sent.
        with self.fake.lock:
            self.fake._place_oso({'accountId': ACCOUNT, 'action': 'Buy', 'symbol': 'MNQZ1', 'orderQty': 1,
                                  'orderType': 'Market', 'text': client_tag('gone-oso'), 'isAutomated': True,
                                  'bracket1': {'action': 'Sell', 'orderType': 'Stop', 'stopPrice': 9962.5,
                                               'text': client_tag(leg_command_id('gone-oso', 'stop'))}},
                                 lambda d, s=200: None)
        wait_until(lambda: self.ex.auto_reconcile() or not self.journal.unresolved(), what='auto reconcile')
        j = self.journal.get('gone-oso')
        self.assertEqual((j['outcome'], set(j['legs'])), ('accepted', {'stop'}))
        self.assertEqual(self.fake.requests.count('order/placeoso'), placed)

    def test_lost_modify_reply_is_matched_by_effect_without_resend(self):
        t, cid, rec = self.enter('long', 2, 9962.5)
        modifies = self.fake.requests.count('order/modifyorder')
        self.fake.drop_next.add('order/modifyorder')
        out, r = self.ex.send(modify('lost-mod', t, price=9985.0))
        self.assertEqual((out, r.get('matched_by_effect')), ('accepted', True), r)
        self.assertEqual(self.fake.requests.count('order/modifyorder'), modifies + 1)
        # A modify that never arrived stays unknown and is resolved when its effect shows.
        self.fake.swallow_endpoints.add('order/modifyorder')
        out, r = self.ex.send(modify('gone-mod', t, price=9993.75))
        self.assertEqual(out, 'unknown', r)
        self.assertEqual(self.ex.auto_reconcile(), [])
        stop_id = int(rec['legs']['stop'])
        with self.fake.lock:
            self.fake._modify({'orderId': stop_id, 'orderQty': 2, 'orderType': 'Stop', 'stopPrice': 9993.75,
                               'text': client_tag('gone-mod')}, lambda d, s=200: None)
        wait_until(lambda: self.ex.auto_reconcile() == ['gone-mod'], what='modify resolved')
        self.assertEqual(self.fake.requests.count('order/modifyorder'), modifies + 2)

    def test_lost_liquidation_reply_is_matched_by_its_order(self):
        t, cid, rec = self.enter('short', 2, 10037.5, tp=9962.5)
        liquidations = self.fake.requests.count('order/liquidateposition')
        self.fake.drop_next.add('order/liquidateposition')
        out, r = self.ex.send(flatten('lost-liq', t))
        self.assertEqual((out, r.get('matched_by_liquidation_order')), ('accepted', True), r)
        self.assertEqual(self.fake.requests.count('order/liquidateposition'), liquidations + 1)
        wait_until(lambda: self.net() == 0 and not self.state.open_orders(ACCOUNT, CONTRACT))
        self.assertEqual(self.fake.orders[int(r['broker_order_id'])]['action'], 'Buy')


class EntryGuards(BracketBase):
    def test_kill_switch_unresolved_limits_and_prices_block_bracket_entry(self):
        with self.fake.lock:  # market evidence on the contract: a round trip in the other account
            for action in ('Buy', 'Sell'):
                self.fake._place({'accountId': OTHER, 'symbol': 'MNQZ1', 'orderQty': 1, 'orderType': 'Market',
                                  'action': action}, lambda d, s=200: None)
        wait_until(lambda: self.state.last_fill_price(CONTRACT) is not None)
        mark = len(self.fake.requests)
        t = trade('long')
        Path(self.cfg['kill_switch_file']).write_text('off')
        try:
            self.assertIn('kill switch', self.ex.send(bracket('g-ks', t, 1, 9962.5))[1]['reason'])
        finally:
            Path(self.cfg['kill_switch_file']).unlink()
        self.assertEqual(self.ex.send(bracket('g-max', t, 0, 9962.5))[0], 'rejected')  # format; no size cap
        cases = [(bracket('g-side', t, 1, 10006.25), 'must be below the entry reference'),
                 (bracket('g-far', t, 1, 9100.0), 'more than 5.0% from the entry reference'),
                 (bracket('g-tp', t, 1, 9962.5, tp=9993.75), 'must be above the entry reference'),
                 (bracket('g-ref', t, 1, 5962.5, ref=6000.0), 'from the latest fill'),
                 (bracket('g-short', trade('short'), 1, 9962.5), 'must be above the entry reference')]
        # A fill exists on the contract, so the reference is checked against it.
        self.assertIsNotNone(self.state.last_fill_price(CONTRACT))
        for p, why in cases:
            out, r = self.ex.send(p)
            self.assertEqual(out, 'rejected', p)
            self.assertIn(why, r['reason'])
        self.journal.start('g-unknown', trade_id='x:long', kind='entry', side='long', quantity=1, tag='none')
        self.journal.finish('g-unknown', 'unknown', {})
        self.assertIn('unknown outcome', self.ex.send(bracket('g-unres', t, 1, 9962.5))[1]['reason'])
        self.journal.reconcile('g-unknown', 'rejected', note='test')
        self.assertEqual(self.requests_since(mark), [])

    def test_hedge_guard_and_flat_requirement(self):
        with self.fake.lock:  # opposite exposure in another account of the same login
            self.fake._place({'accountId': OTHER, 'symbol': 'MNQZ1', 'orderQty': 1, 'orderType': 'Market',
                              'action': 'Sell'}, lambda d, s=200: None)
        wait_until(lambda: self.state.net_position(OTHER, CONTRACT) == -1)
        mark = len(self.fake.requests)
        out, r = self.ex.send(bracket('g-hedge', trade('long'), 1, 9962.5))
        self.assertEqual(out, 'rejected')
        self.assertIn('hedge guard', r['reason'])
        self.assertEqual(self.requests_since(mark), [])
        t, cid, rec = self.enter('short', 1, 10037.5)  # same direction as the other account: allowed
        out, r = self.ex.send(bracket('g-second', trade('short'), 1, 10037.5))
        self.assertIn('flat contract with no working orders', r['reason'])
        out, r = self.ex.send(bracket('g-again', t, 1, 10037.5))
        self.assertEqual(out, 'rejected')


class LegacyUnchanged(BracketBase):
    def test_soup_kinds_behave_as_before_with_brackets_enabled(self):
        t = trade('long')
        mark = len(self.fake.requests)
        self.assertEqual(self.ex.send(legacy('entry', 'l-e', t, 2))[0], 'accepted')
        wait_until(lambda: self.net() == 2)
        self.assertEqual(self.ex.send(legacy('tp1_limit', 'l-t', t, 1, 10075.0))[0], 'accepted')
        wait_until(lambda: self.state.open_orders(ACCOUNT, CONTRACT))
        self.assertEqual(self.ex.send(legacy('cancel', 'l-c', t))[0], 'accepted')
        wait_until(lambda: not self.state.open_orders(ACCOUNT, CONTRACT))
        self.assertEqual(self.ex.send(legacy('flatten', 'l-f', t))[0], 'accepted')
        wait_until(lambda: self.net() == 0)
        self.assertEqual(self.requests_since(mark), ['order/placeorder', 'order/placeorder', 'order/cancelorder',
                                                     'order/placeorder'])
        s = self.snap(t, lambda s: all(o['status'] in ('filled', 'cancelled') for o in s['orders']))
        self.assertEqual([sorted(o) for o in s['orders']],
                         [['broker_order_id', 'command_id', 'fill_time', 'filled_qty', 'status'],
                          ['broker_order_id', 'command_id', 'filled_qty', 'status'],
                          ['broker_order_id', 'command_id', 'fill_time', 'filled_qty', 'status']])
        self.assertEqual(self.ex.maintain_brackets(), [])  # the latest trade is not a bracket


class PublisherAutoProtect(BracketBase):
    def test_daemon_publisher_runs_bracket_housekeeping(self):
        publisher = Publisher(self.ex, self.journal, self.state, 'evidence', None,
                              self.h.tmp / 'snapshots.jsonl', interval_s=0.25, linger_s=5,
                              trade_kinds=('entry', 'bracket_entry'))
        thread = threading.Thread(target=publisher.run, daemon=True)
        thread.start()

        def stop():
            publisher.stopping.set()
            with self.state.changed:
                self.state.changed.notify_all()
            thread.join(5)
        self.addCleanup(stop)
        t, cid, rec = self.enter('long', 3, 9962.5, tp=10027.5, tp_qty=1)
        stop_id = int(rec['legs']['stop'])
        self.fake.set_price(10027.5)
        wait_until(lambda: (self.state.order_version(stop_id) or {}).get('orderQty') == 2, timeout=8,
                   what='publisher shrank the stop')
        self.assertEqual(self.journal.get(leg_command_id(cid, 'auto-shrink-2'))['outcome'], 'accepted')
        wait_until(lambda: publisher.last.get('published') == 'evidence_only', what='bracket snapshot published')
        lines = [json.loads(x) for x in (self.h.tmp / 'snapshots.jsonl').read_text().splitlines()]
        self.assertTrue(any(s['trade_id'] == t for s in lines))


class AutoProtectOff(BracketBase):
    EXTRA = {'bracket_auto_protect': False}

    def test_disabled_auto_protect_never_acts(self):
        self.assertFalse(self.ex.bracket_protect)
        t, cid, rec = self.enter('long', 3, 9962.5, tp=10027.5, tp_qty=1)
        self.fake.set_price(10027.5)
        wait_until(lambda: self.net() == 2)
        mark = len(self.fake.requests)
        self.assertEqual(self.ex.maintain_brackets(), [])
        self.assertEqual(self.requests_since(mark), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
