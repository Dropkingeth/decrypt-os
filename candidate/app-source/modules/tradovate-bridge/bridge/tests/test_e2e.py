"""End-to-end: real headless Chrome, injected page script, fake Tradovate server.

Payloads come from Soup's own transport.message() so the shape is exactly what
the engine sends to a webhook relay.
"""
import os
import sys
import time
import unittest
from pathlib import Path

from harness import ROOT, Harness

# Soup's engine package is not bundled. Point TVBRIDGE_SOUP_PACKAGE at it (a browser test: Linux VM only).
SOUP = Path(os.environ.get('TVBRIDGE_SOUP_PACKAGE', '')) / 'engine'
if not os.environ.get('TVBRIDGE_SOUP_PACKAGE') or not (SOUP / 'transport.py').is_file():
    raise unittest.SkipTest('Soup engine package not found; set TVBRIDGE_SOUP_PACKAGE')
sys.path.insert(0, str(SOUP))
import transport as soup_transport  # noqa: E402

from tvbridge.bridge import Bridge  # noqa: E402
from tvbridge.executor import Executor  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402

TICKER = 'MNQZ2021'


def payload(kind, cid, trade_id, qty=0, price=10000.0, limit=None):
    side = trade_id.rsplit(':', 1)[1]
    command = {'kind': kind, 'id': cid, 'trade_id': trade_id, 'quantity': qty, 'price': price}
    if limit is not None:
        command['quantity_limit'] = limit
    return soup_transport.message(command, TICKER, side)


def wait_until(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError('condition not met in time')


class ContractChecker:
    """The Soup core.broker() rules that a snapshot stream must satisfy."""

    def __init__(self, decision_time):
        self.decision_time = decision_time
        self.seq = 0
        self.observed = 0
        self.orders = {}

    def check(self, snap, commands):
        assert snap['sequence'] > self.seq, 'sequence must increase'
        assert snap['observed_at'] >= self.observed, 'observed_at regressed'
        assert type(snap['position_qty']) is int and snap['position_qty'] >= 0
        ids = [o['broker_order_id'] for o in snap['orders']]
        assert len(ids) == len(set(ids)), 'broker order reused'
        for o in snap['orders']:
            c = commands[o['command_id']]
            assert o['status'] in ('working', 'partial', 'filled', 'cancelled', 'rejected')
            assert 0 <= o['filled_qty'] <= c['quantity']
            assert o['status'] != 'filled' or o['filled_qty'] == c['quantity']
            prev = self.orders.get(o['command_id'])
            if prev:
                assert prev['broker_order_id'] == o['broker_order_id']
                assert o['filled_qty'] >= prev['filled_qty']
                if prev['status'] in ('filled', 'cancelled', 'rejected'):
                    assert o['status'] == prev['status'] and o['filled_qty'] == prev['filled_qty']
                if prev['filled_qty'] and o['filled_qty'] == prev['filled_qty']:
                    assert o['fill_time'] == prev['fill_time'], 'fill_time must be stable'
            if o['filled_qty']:
                assert self.decision_time <= o['fill_time'] <= snap['observed_at']
            self.orders[o['command_id']] = o
        expected_open = sorted(o['broker_order_id'] for o in self.orders.values() if o['status'] in ('working', 'partial'))
        assert sorted(snap['open_orders']) == expected_open, 'open-order inventory mismatch'
        entry = next(o for o in self.orders.values() if commands[o['command_id']]['kind'] == 'entry')
        exits = sum(o['filled_qty'] for o in self.orders.values() if commands[o['command_id']]['kind'] != 'entry')
        assert entry['filled_qty'] - exits == snap['position_qty'], 'fill/position conservation'
        self.seq, self.observed = snap['sequence'], snap['observed_at']


class BridgeE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.cfg = cls.h.config()
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
        cls.bridge.session.close()
        cls.h.close()

    def snap(self, trade_id, timeout=5.0):
        """Snapshots can be withheld briefly (terminal settle window); wait for one."""
        deadline = time.time() + timeout
        while True:
            snap, reason = self.ex.snapshot(trade_id)
            if snap or time.time() > deadline:
                self.assertIsNotNone(snap, reason)
                return snap
            time.sleep(0.1)

    def flatten_fake(self, account=101, contract=555):
        """Test-only cleanup directly on the fake exchange."""
        net = self.state.net_position(account, contract)
        if net:
            with self.h.fake.lock:
                self.h.fake._place({'accountId': account, 'accountSpec': 'DEMO-1', 'symbol': 'MNQZ1',
                                    'orderQty': abs(net), 'orderType': 'Market',
                                    'action': 'Sell' if net > 0 else 'Buy'}, lambda d, s=200: None)
            wait_until(lambda: self.state.net_position(account, contract) == 0)

    def reconcile_unknowns(self):
        for c in self.journal.unresolved():
            self.journal.reconcile(c['command_id'], 'rejected', note='test cleanup')

    def commands(self, trade_id):
        return {c['command_id']: c for c in self.journal.for_trade(trade_id)}

    def test_1_full_trade_lifecycle(self):
        trade = 'soup-main:MNQZ2021:1632147200000:long'
        checker = ContractChecker(decision_time=int(time.time() * 1000) - 1000)

        # Entry: market buy 3.
        out, rec = self.ex.send(payload('entry', 'c-entry', trade, 3))
        self.assertEqual(out, 'accepted', rec)
        wait_until(lambda: self.state.net_position(101, 555) == 3)
        s = self.snap(trade)
        checker.check(s, self.commands(trade))
        self.assertEqual((s['position_qty'], s['orders'][0]['status']), (3, 'filled'))
        self.assertEqual((s['account_id'], s['symbol'], s['side']), ('PAPER-TEST', TICKER, 'long'))

        # Duplicate command id never re-sends.
        before = self.h.fake.order_count()
        out2, rec2 = self.ex.send(payload('entry', 'c-entry', trade, 3))
        self.assertEqual((out2, rec2.get('duplicate')), ('accepted', True))
        self.assertEqual(self.h.fake.order_count(), before)

        # A second entry while in a position is refused before sending.
        out, rec = self.ex.send(payload('entry', 'c-other', 'soup-main:MNQZ2021:1632147205000:long', 1))
        self.assertEqual(out, 'rejected')
        self.assertIn('flat', rec['reason'])

        # TP1 working limit for 1.
        out, rec = self.ex.send(payload('tp1_limit', 'c-tp1', trade, 1, price=10006.5))
        self.assertEqual(out, 'accepted', rec)
        tp1_id = rec['broker_order_id']
        wait_until(lambda: self.state.open_orders(101, 555))
        s = self.snap(trade)
        checker.check(s, self.commands(trade))
        self.assertEqual(s['open_orders'], [tp1_id])

        # Price reaches TP1.
        self.h.fake.set_price(10006.5)
        wait_until(lambda: self.state.net_position(101, 555) == 2)
        s = self.snap(trade)
        checker.check(s, self.commands(trade))
        tp1 = next(o for o in s['orders'] if o['command_id'] == 'c-tp1')
        self.assertEqual((tp1['status'], tp1['filled_qty'], s['position_qty']), ('filled', 1, 2))

        # Oversized market exit is refused; it would flip the position.
        out, rec = self.ex.send(payload('reduce', 'c-big', trade, 5))
        self.assertEqual(out, 'rejected')
        self.assertIn('exceeds', rec['reason'])

        # Tranche reduction of 1.
        out, rec = self.ex.send(payload('reduce', 'c-red', trade, 1))
        self.assertEqual(out, 'accepted', rec)
        wait_until(lambda: self.state.net_position(101, 555) == 1)
        checker.check(self.snap(trade), self.commands(trade))

        # A resting limit, then the ticker-scoped cancel barrier.
        out, rec = self.ex.send(payload('tp1_limit', 'c-rest', trade, 1, price=10037.5))
        self.assertEqual(out, 'accepted', rec)
        wait_until(lambda: self.state.open_orders(101, 555))
        out, rec = self.ex.send(payload('cancel', 'c-cancel', trade))
        self.assertEqual(out, 'accepted', rec)
        wait_until(lambda: not self.state.open_orders(101, 555))
        s = self.snap(trade)
        checker.check(s, self.commands(trade))
        self.assertEqual(next(o for o in s['orders'] if o['command_id'] == 'c-rest')['status'], 'cancelled')

        # Full close with the quantity omitted (the webhook flatten form).
        out, rec = self.ex.send(payload('flatten', 'c-flat', trade, 1))
        self.assertEqual(out, 'accepted', rec)
        wait_until(lambda: self.state.net_position(101, 555) == 0)
        s = self.snap(trade)
        checker.check(s, self.commands(trade))
        self.assertEqual((s['position_qty'], s['open_orders']), (0, []))
        self.assertFalse(self.ex.trade_active(trade))

        # Exiting when flat is refused, not silently accepted.
        out, rec = self.ex.send(payload('flatten', 'c-flat-2', trade, 1))
        self.assertEqual(out, 'rejected')

        # Every order carried the automated-order flag.
        self.assertTrue(all(o['isAutomated'] is True for o in self.h.fake.orders.values()))

    def test_2_guards(self):
        trade = 'soup-main:MNQZ2021:1632147300000:short'
        # Wrong ticker.
        p = payload('entry', 'g-1', trade, 1)
        p['ticker'] = 'MESZ2021'
        self.assertEqual(self.ex.send(p)[0], 'rejected')
        # A zero quantity (format check; there is no size cap).
        self.assertEqual(self.ex.send(payload('entry', 'g-2', trade, 0))[0], 'rejected')
        # Exit for a trade with no entry.
        self.assertEqual(self.ex.send(payload('tp1_limit', 'g-3', trade, 1, price=9993.75))[0], 'rejected')
        # Off-tick limit price.
        p = payload('tp1_limit', 'g-4', trade, 1, price=9993.75)
        p['limitPrice'] = 9993.8
        self.assertEqual(self.ex.send(p)[0], 'rejected')
        # Kill switch blocks entries.
        Path(self.cfg['kill_switch_file']).write_text('off')
        try:
            out, rec = self.ex.send(payload('entry', 'g-5', trade, 1))
            self.assertEqual(out, 'rejected')
            self.assertIn('kill switch', rec['reason'])
        finally:
            Path(self.cfg['kill_switch_file']).unlink()
        # Nothing reached the fake exchange.
        self.assertFalse(any(c.startswith('g-') for c in self.journal.commands))

    def test_3_short_trade_and_lost_response(self):
        trade = 'soup-main:MNQZ2021:1632147400000:short'
        out, rec = self.ex.send(payload('entry', 's-entry', trade, 2))
        self.assertEqual(out, 'accepted', rec)
        wait_until(lambda: self.state.net_position(101, 555) == -2)
        s = self.snap(trade)
        self.assertEqual((s['side'], s['position_qty']), ('short', 2))
        out, rec = self.ex.send(payload('flatten', 's-flat', trade, 2))
        self.assertEqual(out, 'accepted', rec)
        wait_until(lambda: self.state.net_position(101, 555) == 0)

        # Order placed but the reply is lost: the client tag finds the real order.
        trade2 = 'soup-main:MNQZ2021:1632147500000:long'
        self.h.fake.drop_next.add('order/placeorder')
        out, rec = self.ex.send(payload('entry', 'u-entry', trade2, 1))
        self.assertEqual((out, rec.get('matched_by_tag')), ('accepted', True), rec)
        self.flatten_fake()

        # Request lost entirely: truly unknown, snapshot withheld, entries blocked.
        trade3 = 'soup-main:MNQZ2021:1632147510000:long'
        self.h.fake.swallow_next = True
        out, rec = self.ex.send(payload('entry', 'u-lost', trade3, 1))
        self.assertEqual(out, 'unknown', rec)
        snap, reason = self.ex.snapshot(trade3)
        self.assertIsNone(snap)
        self.assertTrue(reason.startswith('uncertain_dispatch'))
        self.assertTrue(self.ex.trade_active(trade3))
        out, rec = self.ex.send(payload('entry', 'u-next', 'soup-main:MNQZ2021:1632147550000:long', 1))
        self.assertEqual(out, 'rejected')
        self.assertIn('unknown outcome', rec['reason'])
        # If the tagged order shows up later, the bridge resolves it by itself.
        from tvbridge.executor import client_tag
        with self.h.fake.lock:
            self.h.fake._place({'accountId': 101, 'symbol': 'MNQZ1', 'orderQty': 1, 'orderType': 'Limit',
                                'price': 8500.0, 'action': 'Buy', 'text': client_tag('u-lost')},
                               lambda d, s=200: None)
        wait_until(lambda: self.ex.auto_reconcile() or not self.journal.unresolved())
        self.assertEqual(self.journal.get('u-lost')['outcome'], 'accepted')
        # Clean up the resting order.
        self.assertEqual(self.ex.send(payload('cancel', 'u-cancel', trade3))[0], 'accepted')
        wait_until(lambda: not self.state.open_orders(101, 555))

    def test_4b_definite_refusal_is_rejected(self):
        # Tradovate refuses with failureReason "UnknownReason"; no order exists, so it is rejected.
        self.h.fake.reject_next = True
        out, rec = self.ex.send(payload('entry', 'rj-entry', 'soup-main:MNQZ2021:1632147650000:long', 1))
        self.assertEqual(out, 'rejected', rec)
        self.assertIn('data feed', rec['failureText'])
        self.assertEqual(self.journal.unresolved(), [])

    def test_4c_risk_rejected_after_order_id(self):
        # Tradovate: placeorder returns an orderId, then the order goes PendingNew -> Rejected.
        trade = 'soup-main:MNQZ2021:1632147660000:long'
        self.h.fake.risk_reject_next = True
        out, rec = self.ex.send(payload('entry', 'rr-entry', trade, 1))
        self.assertEqual(out, 'accepted', rec)
        s = self.snap(trade)
        self.assertEqual((s['orders'][0]['status'], s['orders'][0]['filled_qty'], s['position_qty']),
                         ('rejected', 0, 0))
        out, rec = self.ex.send(payload('tp1_limit', 'rr-tp1', trade, 1, price=10006.5))
        self.assertEqual(out, 'rejected')
        self.assertIn('not completely filled', rec['reason'])

    def test_5_server_error_after_processing_is_matched_by_tag(self):
        trade = 'soup-main:MNQZ2021:1632147700000:long'
        self.h.fake.next_place_status = 500
        out, rec = self.ex.send(payload('entry', 'e500', trade, 1))
        # The order was in fact placed despite the error reply; the tag proves it.
        self.assertEqual((out, rec.get('matched_by_tag'), rec.get('s')), ('accepted', True, 500), rec)
        self.flatten_fake()

    def test_6_partial_entry_blocks_tp1(self):
        trade = 'soup-main:MNQZ2021:1632147800000:long'
        self.h.fake.next_market_partial = 1
        out, rec = self.ex.send(payload('entry', 'p-entry', trade, 2))
        self.assertEqual(out, 'accepted', rec)
        # Entry filled 1 of 2 then cancelled: a TP1 for 2 would flip the account short.
        out, rec = self.ex.send(payload('tp1_limit', 'p-tp1', trade, 2, price=10006.5))
        self.assertEqual(out, 'rejected')
        self.assertIn('not completely filled', rec['reason'])
        s = self.snap(trade)
        self.assertEqual((s['orders'][0]['status'], s['orders'][0]['filled_qty'], s['position_qty']),
                         ('cancelled', 1, 1))
        out, rec = self.ex.send(payload('flatten', 'p-flat', trade, 1))
        self.assertEqual(out, 'accepted', rec)
        wait_until(lambda: self.state.net_position(101, 555) == 0)

    def test_7_market_exit_refused_while_orders_work(self):
        trade = 'soup-main:MNQZ2021:1632147900000:long'
        self.assertEqual(self.ex.send(payload('entry', 'w-entry', trade, 2))[0], 'accepted')
        self.assertEqual(self.ex.send(payload('tp1_limit', 'w-tp1', trade, 1, price=10037.5))[0], 'accepted')
        wait_until(lambda: self.state.open_orders(101, 555))
        out, rec = self.ex.send(payload('reduce', 'w-red', trade, 1))
        self.assertEqual(out, 'rejected')
        self.assertIn('cancel barrier', rec['reason'])
        self.assertEqual(self.ex.send(payload('cancel', 'w-cancel', trade))[0], 'accepted')
        wait_until(lambda: not self.state.open_orders(101, 555))
        self.assertEqual(self.ex.send(payload('flatten', 'w-flat', trade, 2))[0], 'accepted')
        wait_until(lambda: self.state.net_position(101, 555) == 0)

    def test_8_page_reload_invalidates_state_until_new_sync(self):
        old_epoch = self.bridge.status()['epoch']
        self.bridge.session.call('Page.reload')
        wait_until(lambda: not self.ex.health()['ok'], timeout=5)
        out, rec = self.ex.send(payload('entry', 'rl-entry', 'soup-main:MNQZ2021:1632148000000:long', 1))
        self.assertEqual(out, 'rejected')
        wait_until(lambda: self.ex.health()['ok'], timeout=15)
        self.assertNotEqual(self.bridge.status()['epoch'], old_epoch)

    def test_9_page_globals_are_not_guessable(self):
        names = self.bridge.session.evaluate('Object.keys(window).filter(k => k.startsWith("__dlb"))')
        self.assertEqual(names, [])

    def test_4_reconnect_and_resync(self):
        auth_before = self.h.fake.authorize_count
        self.h.fake.disconnect_all()
        wait_until(lambda: not self.ex.health()['ok'], timeout=5)
        # Refused while the bridge cannot see the account.
        out, rec = self.ex.send(payload('entry', 'r-entry', 'soup-main:MNQZ2021:1632147600000:long', 1))
        self.assertEqual(out, 'rejected')
        self.assertIn('unhealthy', rec['reason'])
        wait_until(lambda: self.ex.health()['ok'], timeout=15)
        self.assertGreater(self.h.fake.authorize_count, auth_before)
        # State after resync still reflects the account.
        self.assertEqual(self.state.net_position(101, 555), 0)


class ContractLookup(unittest.TestCase):
    def test_contract_find_when_not_in_sync(self):
        h = Harness(include_contract_in_sync=False)
        try:
            cfg = h.config()
            state = AccountState()
            bridge = Bridge(state, devtools_port=h.devtools_port, page_url_contains='/trader/',
                            trade_url_pattern=cfg['trade_url_pattern'])
            bridge.attach(reload=True)
            bridge.wait_ready(timeout=20)
            ex = Executor(bridge, state, Journal(cfg['journal_dir']), cfg)
            ex.resolve()
            self.assertEqual(ex.contract_id, 555)
            self.assertIn('contract/find', h.fake.requests)
            bad = Executor(bridge, state, Journal(cfg['journal_dir'] + '-2'), dict(cfg, account_name='NOPE'))
            with self.assertRaises(RuntimeError):
                bad.resolve()
            bridge.session.close()
        finally:
            h.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
