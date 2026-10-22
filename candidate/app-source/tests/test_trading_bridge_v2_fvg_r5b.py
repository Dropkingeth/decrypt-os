"""Registered strategy module `bridge_v2_fvg_r5b`, adapter `fvg-r5b-bridge-v2`, through the real runtime and
bridge route against the fake bridge (no network, synthetic fixture values). Harness copied from
`test_trading_bridge_v2.py` (the shared engine's own feature matrix is proved there); this file covers the
adapter's payload shape and switches only: bar-close stop with a resting safety stop, three resting
limits, break-even on a target trigger or on a price trigger alone, EOD, the daily loss lock, the bias-flip
and flatten events, and the module's identity/extras checks (trade_id prefix, profile pattern).
"""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_fixture import FakeBridge, native_binding  # noqa: E402
from bridge_v2_fvg_r5b_payloads import BASE, CLOSE, STREAM, SYMBOL, TRADE, entry, event, price_trigger_only  # noqa: E402
from decrypt_trading import RuntimeManager  # noqa: E402
from decrypt_trading import bridge_adapters, bridge_v2, bridge_v2_fvg_r5b as fvg, source  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402

ADAPTER = fvg.STRATEGY.adapter_id
ACCOUNT = 30000099
ENGINE_SHA = hashlib.sha256(Path(fvg.__file__).read_bytes()).hexdigest()
STRATEGY_SHA = '7' * 64
TRADE2 = 'fvg1:%s:%d:long' % (SYMBOL, BASE + 3_600_000)


class Book:
    """The broker for one trade, driven by the orders the runtime sent to the fake bridge."""

    def __init__(self, test):
        self.test, self.rows, self.qty, self.limit = test, {}, {}, {}
        self.done, self.position, self.oid, self.entry, self.staged, self.seq = 0, 0, 100, None, None, 0

    def add(self, cid, **extra):
        self.oid += 1
        self.rows[cid] = dict({'command_id': cid, 'broker_order_id': str(self.oid), 'status': 'working',
                               'filled_qty': 0}, **extra)
        return self.rows[cid]

    def absorb(self):
        sent, new = self.test.bridge.sent, 0
        while self.done < len(sent):
            p = sent[self.done]
            self.done, new = self.done + 1, new + 1
            ex = p['extras']
            cid, kind = ex['command_id'], ex.get('bridge_kind')
            if p['action'] in ('buy', 'sell'):
                self.rows, self.position, self.staged = {}, 0, None
                self.entry, self.qty[cid] = cid, p['quantity']
                self.add(cid, role='entry')
                if kind == 'bracket_entry':
                    self.add(cid + '#stop', role='stop', quantity=p['quantity'], stop_price=ex['stop_price'])
                    if 'tp1_price' in ex:
                        self.staged = (ex['tp1_price'], ex['tp1_quantity'])
            elif p['action'] == 'exit' and p['orderType'] == 'limit':
                self.qty[cid], self.limit[cid] = p['quantity'], p['limitPrice']
                self.add(cid)
            elif kind == 'modify_stop':
                row = self.rows[self.entry + '#stop']
                row['quantity'], row['stop_price'] = ex['stop_quantity'], ex['stop_price']
            elif p['action'] == 'exit':
                self.qty[cid] = p['quantity']
                self.add(cid)
                self.fill(cid, self.test.price)
            elif kind == 'flatten_all':
                if self.position:
                    self.qty[cid] = self.position
                    self.add(cid, role='flatten', quantity=self.position)
                    self.fill(cid, self.test.price)
                for row in self.rows.values():
                    if row['status'] in ('working', 'partial'):
                        row['status'] = 'cancelled'
        return new

    def fill(self, cid, price, qty=None):
        row = self.rows[cid]
        size = row['quantity'] if 'quantity' in row and cid != self.entry else self.qty[cid]
        qty = size - row['filled_qty'] if qty is None else qty
        row['filled_qty'] += qty
        row['status'] = 'filled' if row['filled_qty'] == size else 'partial'
        row.update(avg_fill_price=price, fill_time=self.test.now)
        self.position += qty if cid == self.entry else -qty
        if cid == self.entry and row['status'] == 'filled' and self.staged:
            self.add(cid + '#tp1', role='tp1', quantity=self.staged[1])
            self.limit[cid + '#tp1'] = self.staged[0]

    def evidence(self):
        trade = self.test.trade
        self.seq += 1
        self.test.bridge.publish(self.seq, {
            'event_id': 'tvbridge-%d' % self.seq, 'sequence': self.seq, 'observed_at': self.test.now,
            'account_id': str(ACCOUNT), 'symbol': SYMBOL, 'trade_id': trade['id'], 'side': trade['side'],
            'position_qty': self.position, 'orders': copy.deepcopy(list(self.rows.values())),
            'open_orders': [r['broker_order_id'] for r in self.rows.values() if r['status'] in ('working', 'partial')]})
        self.test.runtime._bridge_tick()
        self.test.runtime.step()
        row = self.test.engine.db.execute("SELECT status,detail FROM inbox WHERE channel='broker' ORDER BY seq DESC").fetchone()
        self.test.assertEqual(row[0], 'processed', row)
        return row[1]


class V2Base(unittest.TestCase):
    start = CLOSE + 300
    resting = 3

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Real registration: `fvg-r5b-bridge-v2` is already registered at import time by the one line each in
        # bridge_adapters.V2_ADAPTERS and bridge_v2_registry.STRATEGIES (source.py, customer_contract.py). Only
        # the network is blocked here; nothing is re-registered.
        patcher = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIn(ADAPTER, bridge_adapters.REGISTRY, 'V2_ADAPTERS must carry the fvg-r5b-bridge-v2 line')
        self.assertEqual(contract.PAIRS[ADAPTER][0], ENGINE_SHA, 'bridge_v2_registry.STRATEGIES module hash')
        self.now, self.price = self.start, 21000.0
        self.root = Path(self.temp.name)
        self.binding = native_binding(name='fvg-r5b-fixture', pair=ADAPTER, account=ACCOUNT)
        self.binding['streamId'] = STREAM
        self.binding['routeSha256'] = contract.route_digest(self.binding)
        self.bridge = FakeBridge(self.root / 'data', self.binding, clock=lambda: self.now)
        self.bridge.write_config(adapters={ADAPTER: {'enabled': True}})
        self.bridge.state_override = {'adapter_ids': [ADAPTER]}
        self.manager = RuntimeManager(self.root / 'trading')
        self.manager.install(self.binding)
        self.quotes = self.bars = 0
        self.book = Book(self)
        self.open()

    def open(self):
        self.transport = self.bridge.opener(self.binding)
        self.runtime = self.manager.open(self.binding['instanceId'], transport=self.transport, clock=lambda: self.now)
        self.addCleanup(self.runtime.close)

    @property
    def engine(self):
        return self.runtime.engine

    @property
    def trade(self):
        return self.engine.data['trade']

    def ingest(self, channel, value):
        producer = self.binding['signalProducerId' if channel == 'pine' else 'feedProducerId']
        self.runtime.ingest(channel, json.dumps(value), producer_id=producer)
        self.runtime.step()

    def detail(self):
        return self.engine.db.execute("SELECT detail FROM inbox WHERE channel='pine' ORDER BY seq DESC").fetchone()[0]

    def alert(self, value):
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', value)
        out = self.detail()
        self.settle()
        return out

    def quote(self, price, settle=True):
        last = self.engine.data['last_quote']
        if last and last['time'] >= self.now:
            self.now = last['time'] + 1
        self.quotes += 1
        self.price = price
        self.ingest('quote', {'event_id': 'q-%d' % self.quotes, 'symbol': SYMBOL, 'time': self.now, 'price': price})
        if settle:
            self.settle()

    def bar(self, close_time, close):
        self.now = max(self.now, close_time + 100)
        self.bars += 1
        self.ingest('bar', {'event_id': 'b-%d' % self.bars, 'symbol': SYMBOL, 'open_time': close_time - 5000,
                            'close_time': close_time, 'open': close, 'high': close, 'low': close, 'close': close})
        self.settle()

    def gate(self):
        self.transport.route.refresh()
        self.runtime.step()
        self.settle()

    def settle(self):
        for i in range(12):
            new = self.book.absorb()
            if (self.book.entry not in self.engine.data['commands'] or self.trade['phase'] in ('closed', 'armed')
                    or (i and not new)):
                return
            self.now += 50
            self.book.evidence()

    def evidence(self):
        self.now += 50
        out = self.book.evidence()
        self.settle()
        return out

    def kinds(self):
        return [p['extras'].get('bridge_kind') or ('entry' if p['action'] in ('buy', 'sell') else
                                                   'tp1_limit' if p.get('orderType') == 'limit' else
                                                   'exit_market' if p['action'] == 'exit' else p['action'])
                for p in self.bridge.sent]

    def enter(self, value=None, avg=None, fill=True):
        plan = value or entry()
        self.quote(plan['entry']['price'], settle=False)
        out = self.alert(plan)
        self.gate()
        if fill and self.book.entry:
            self.now += 200
            self.book.fill(self.book.entry, avg or self.price)
            self.evidence()
        return out

    def assert_all_valid(self):
        enabled = {ADAPTER: bridge_adapters.REGISTRY[ADAPTER]}
        for payload in self.bridge.sent:
            bridge_adapters.validate_command(payload, ticker=SYMBOL, enabled=enabled)


class Entries(V2Base):
    def test_bracket_entry_carries_the_bar_close_stop_safety_stop_and_three_targets(self):
        self.assertEqual(self.enter(fill=False), 'entry_accepted_not_sent')
        bracket = self.bridge.sent[0]
        self.assertEqual((bracket['action'], bracket['orderType'], bracket['quantity']), ('buy', 'market', 8))
        self.assertEqual(bracket['extras']['stop_price'], 20985.0, 'the resting safety stop, not the bar-close level')
        self.assertEqual(bracket['extras']['tp1_price'], 21010.0)
        self.book.fill(self.book.entry, 21000.0)
        self.evidence()
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'tp1_limit'], 'TP1 is staged with the '
                         'bracket; TP2 and TP3 are placed once the entry has filled')
        self.assertEqual((self.bridge.sent[1]['limitPrice'], self.bridge.sent[1]['quantity']), (21020.0, 2), 'TP2')
        self.assertEqual((self.bridge.sent[2]['limitPrice'], self.bridge.sent[2]['quantity']), (21030.0, 2), 'TP3')
        self.assert_all_valid()

    def test_no_cap_the_alerts_own_size_is_sent(self):
        plan = entry(entry={'qty': 40}, targets=[{'price': 21010.0, 'qty': 20, 'type': 'limit'}],
                    runner={'qty': 20})
        self.enter(plan, fill=False)
        self.assertEqual(self.bridge.sent[0]['quantity'], 40)

    def test_price_trigger_with_no_target_moves_the_stop(self):
        self.enter(price_trigger_only())
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'],
                         'two targets: the first is staged with the bracket, the second placed after fill')
        self.quote(21005.75)
        self.assertEqual(len(self.bridge.sent), 2)
        self.quote(21006.0)                                  # the price trigger alone
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'modify_stop'])
        be = self.bridge.sent[-1]['extras']
        self.assertEqual((be['stop_price'], be['stop_quantity']), (21002.0, 6))

    def test_bad_trade_id_prefix_is_refused(self):
        bad = entry(trade_id='MNQZ2026_%d_long' % BASE)
        bad['alert_id'] = bad['trade_id'] + ':entry'
        self.assertEqual(self.alert(bad), 'input_rejected')
        with self.assertRaisesRegex(ValueError, 'trade_id'):
            self.engine.validate_pine(bad, self.now)

    def test_bad_profile_extras_is_refused(self):
        bad = entry(extras={'profile': 'bad profile!', 'risk_ticks': 40.0})
        self.assertEqual(self.alert(bad), 'input_rejected')
        with self.assertRaisesRegex(ValueError, 'profile'):
            self.engine.validate_pine(bad, self.now)


class BreakEven(V2Base):
    def test_touch_trigger_moves_the_stop_to_the_be_price(self):
        self.enter()
        self.quote(21009.75)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'tp1_limit'])
        self.quote(21010.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'tp1_limit', 'modify_stop'])
        be = self.bridge.sent[-1]['extras']
        self.assertEqual((be['stop_price'], be['stop_quantity']), (21002.0, 8))
        self.quote(21014.0)
        self.assertEqual(len(self.bridge.sent), 4, 'break-even is applied once')
        self.assert_all_valid()

    def test_break_even_off_does_nothing(self):
        self.enter(entry(break_even={'enabled': False}))
        self.quote(21010.0)
        self.quote(21040.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'tp1_limit'])


class Clock(V2Base):
    def test_end_of_day_on_flattens_at_the_alert_time(self):
        self.enter(entry(eod={'enabled': True, 'time': '15:30'}))
        eod = bridge_v2.next_clock(CLOSE, '15:30', 'America/New_York')
        self.now = eod
        self.runtime.step()
        self.settle()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'eod')

    def test_end_of_day_off_does_nothing(self):
        self.enter(entry(eod={'enabled': False, 'time': '10:30'}))
        self.now += 120_000
        self.evidence()
        self.runtime.step()
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'tp1_limit'])


class DailyLoss(V2Base):
    def lose(self, plan):
        self.enter(plan)
        self.book.fill(self.book.entry + '#stop', 20985.0)
        self.evidence()
        self.assertEqual(self.trade['phase'], 'closed')

    def test_on_locks_the_day_and_flattens_an_open_loser(self):
        self.enter(entry(daily_loss={'enabled': True, 'limit_usd': 50, 'include_open': True, 'flatten': True}))
        self.quote(20996.0)                                  # -4 pts x 8 contracts x $2 - $8 fees = -$72: beyond
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'daily_loss')

    def test_off_does_not_lock_the_day(self):
        self.lose(entry(daily_loss={'enabled': False, 'limit_usd': 50}))   # -15 pts x 8 x $2 - $8 fees = -$248
        self.now += 60_000
        self.quote(21000.0, settle=False)
        second = 'fvg1:%s:%d:long' % (SYMBOL, self.now - 60_000)
        self.assertEqual(self.alert(entry(trade_id=second, ts=self.now, daily_loss={'enabled': False})),
                         'entry_accepted_not_sent')


class Management(V2Base):
    def test_bias_flip_on_exits_bias_flip_off_ignores(self):
        self.enter(entry(bias_flip={'enabled': True}))
        self.ingest('pine', event('bias_flip', ts=self.now, reason='bias_flip', ref_price=21003.0))
        self.assertEqual(self.detail(), 'bias_flip_requested')
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'bias_flip:bias_flip')

    def test_bias_flip_disabled_by_entry_is_ignored(self):
        self.enter(entry(bias_flip={'enabled': False}))
        self.ingest('pine', event('bias_flip', ts=self.now, reason='bias_flip'))
        self.assertEqual(self.detail(), 'bias_flip_ignored_disabled_by_entry')

    def test_flatten_event_closes_the_trade(self):
        self.enter()
        self.ingest('pine', event('flatten', ts=self.now, reason='fixture_flatten', ref_price=20988.0))
        self.assertEqual(self.detail(), 'flatten_requested')
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'flatten:fixture_flatten')


if __name__ == '__main__':
    unittest.main()
