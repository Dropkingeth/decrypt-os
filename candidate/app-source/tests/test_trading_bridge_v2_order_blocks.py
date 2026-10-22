"""decrypt-bridge-v2 receiver/executor for the registered `order-blocks` adapter, through the
real runtime and bridge route against the fake bridge (no network). Alerts come from
tests/bridge_v2_order_blocks_payloads.py (synthetic fixture, literal arbitrary prices): a base alert with
no optional exit, then every optional exit switched on and off.

Unlike tests/test_trading_bridge_v2.py (which registers a throwaway 'example-v2' strategy), this
strategy is already for real in bridge_v2_registry.STRATEGIES, bridge_adapters.V2_ADAPTERS and
customer_contract.PAIRS (one line each, added with this file) -- importing decrypt_trading registers it
at import time. So setUp uses the adapter/engine/pair exactly as registered; it does not re-register.

`Book` (copied from test_trading_bridge_v2.py) plays the broker from what the runtime sent.
"""
import copy
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_fixture import FakeBridge, native_binding  # noqa: E402
from bridge_v2_order_blocks_payloads import BASE, STREAM, TRADE, entry, flatten  # noqa: E402
from decrypt_trading import RuntimeManager  # noqa: E402
from decrypt_trading import bridge_adapters, bridge_v2  # noqa: E402
from decrypt_trading import bridge_v2_order_blocks as strategy_module  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402

ADAPTER = strategy_module.STRATEGY.adapter_id
ACCOUNT = 30000043
TRADE2 = 'obx-2:long'


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
                self.rows, self.position, self.staged = {}, 0, None       # a new trade
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
            'account_id': str(ACCOUNT), 'symbol': 'MNQZ2026', 'trade_id': trade['id'], 'side': trade['side'],
            'position_qty': self.position, 'orders': copy.deepcopy(list(self.rows.values())),
            'open_orders': [r['broker_order_id'] for r in self.rows.values() if r['status'] in ('working', 'partial')]})
        self.test.runtime._bridge_tick()
        self.test.runtime.step()
        row = self.test.engine.db.execute("SELECT status,detail FROM inbox WHERE channel='broker' ORDER BY seq DESC").fetchone()
        self.test.assertEqual(row[0], 'processed', row)
        return row[1]


class Base(unittest.TestCase):
    start = BASE + 300
    resting = 1          # one optional take-profit target, matching V2_ADAPTERS' line for this strategy

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        patcher.start()
        self.addCleanup(patcher.stop)
        # ADAPTER is already registered for real (bridge_v2_registry.STRATEGIES, bridge_adapters.V2_ADAPTERS,
        # customer_contract.PAIRS); nothing to add here.
        self.assertEqual(bridge_adapters.REGISTRY[ADAPTER].take_profits, self.resting)
        self.now, self.price = self.start, 21000.0
        self.root = Path(self.temp.name)
        self.binding = native_binding(name='order-blocks-fixture', pair=ADAPTER, account=ACCOUNT)
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
        import json
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
        self.ingest('quote', {'event_id': 'q-%d' % self.quotes, 'symbol': 'MNQZ2026', 'time': self.now, 'price': price})
        if settle:
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
        plan = value if value is not None else entry()
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
            bridge_adapters.validate_command(payload, ticker='MNQZ2026', enabled=enabled)


class Baseline(Base):
    """Base alert: 2 contracts, market entry, resting stop, no take profit, no break-even, no trail, no daily
    loss lock. EOD on."""

    def test_long_sends_a_plain_bracket_entry_with_the_alert_stop_and_no_optional_exits(self):
        self.assertEqual(self.enter(fill=False), 'entry_accepted_not_sent')
        bracket = self.bridge.sent[0]
        self.assertEqual((bracket['action'], bracket['orderType'], bracket['quantity']), ('buy', 'market', 2))
        self.assertEqual(bracket['extras']['stop_price'], 20983.0)
        self.assertNotIn('tp1_price', bracket['extras'], 'take profit is off: no resting target')
        self.book.fill(self.book.entry, 21000.0)
        self.evidence()
        self.assertEqual(self.kinds(), ['bracket_entry'], 'no TP leg, break-even off, trail off')
        self.quote(21050.0)
        self.assertEqual(len(self.bridge.sent), 1, 'the runner just holds: nothing else is sent')
        self.assert_all_valid()

    def test_short_mirrors_long(self):
        plan = entry(side='short', price=21000.0, stop=21017.0)
        self.enter(plan, fill=False)
        bracket = self.bridge.sent[0]
        self.assertEqual(bracket['action'], 'sell')
        self.assertEqual(bracket['extras']['stop_price'], 21017.0)

    def test_stop_fill_closes_the_trade(self):
        self.enter()
        self.book.fill(self.book.entry + '#stop', 20983.0)
        self.evidence()
        self.assertEqual(self.kinds(), ['bracket_entry'], 'nothing else is working to cancel: no flatten_all needed')
        self.assertEqual(self.trade['phase'], 'closed')
        self.assertEqual(self.trade['closed_reason'], 'stop_filled')

    def test_end_of_day_flattens_at_the_alert_time(self):
        self.enter()
        eod = bridge_v2.next_clock(BASE, '15:45', 'America/New_York')
        self.now = eod
        self.runtime.step()
        self.settle()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'eod')

    def test_end_of_day_off_does_nothing_at_the_same_clock(self):
        self.enter(entry(eod={'enabled': False}))
        self.now = bridge_v2.next_clock(BASE, '15:45', 'America/New_York')
        self.runtime.step()
        self.settle()
        self.assertNotIn('flatten_all', self.kinds())

    def test_strategy_flatten_notice_after_the_receivers_own_eod_flatten_is_a_harmless_duplicate(self):
        """A strategy-sent 'flatten' event can arrive after the receiver's own EOD clock has already
        flattened the trade; the duplicate notice must be a no-op."""
        self.enter()
        eod = bridge_v2.next_clock(BASE, '15:45', 'America/New_York')
        self.now = eod
        self.runtime.step()
        self.settle()
        self.assertEqual(self.trade['phase'], 'closed')
        sent_before = len(self.bridge.sent)
        self.ingest('pine', flatten('eod', ts=self.now + 1))
        self.assertEqual(self.detail(), 'flatten_no_matching_trade')
        self.assertEqual(len(self.bridge.sent), sent_before, 'nothing new is sent for an already-closed trade')


class TakeProfit(Base):
    def test_take_profit_on_rests_with_the_bracket_and_shrinks_the_stop_on_fill(self):
        """The adapter allows one optional target: it rests as part of the bracket itself, so filling it
        (not a separate sent order) is what matters here."""
        plan = entry(qty=4, targets=[{'price': 21011.0, 'qty': 3, 'type': 'limit'}])
        self.enter(plan)
        self.assertEqual(self.kinds(), ['bracket_entry'], 'the only target is staged with the bracket entry')
        bracket = self.bridge.sent[0]
        self.assertEqual((bracket['extras']['tp1_price'], bracket['extras']['tp1_quantity']), (21011.0, 3))
        self.book.fill(self.book.entry + '#tp1', 21011.0)
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'modify_stop')
        self.assertEqual(self.bridge.sent[-1]['extras']['stop_quantity'], 1)
        self.assertTrue(self.trade['tp1_done'])
        self.assert_all_valid()

    def test_take_profit_off_sends_no_resting_target(self):
        self.enter(entry(targets=[]))
        self.assertEqual(self.kinds(), ['bracket_entry'])


class BreakEven(Base):
    def test_break_even_on_moves_the_stop_to_fill_plus_offset_at_the_bar_close(self):
        """With break_even.confirm 'bar_close', a met trigger waits for the close of the confirm
        timeframe's bar (confirm_timeframe_s), not at once."""
        plan = entry(qty=1, break_even={'enabled': True, 'trigger_price': 21006.0, 'offset_ticks': 3})
        self.enter(plan, avg=21000.0)
        self.quote(21005.75)
        self.assertEqual(self.kinds(), ['bracket_entry'], 'not yet at the trigger price')
        self.quote(21006.0)
        met_at = self.now
        self.assertEqual(self.kinds(), ['bracket_entry'], 'triggered, but not confirmed until the bar closes')
        bar_end = (met_at // 240_000 + 1) * 240_000
        self.now = bar_end
        self.quote(21006.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'modify_stop'])
        be = self.bridge.sent[-1]['extras']
        self.assertEqual(be['stop_price'], 21000.75)   # 21000.0 fill + 3 ticks
        self.assert_all_valid()

    def test_break_even_off_does_nothing(self):
        self.enter(entry(break_even={'enabled': False}))
        self.quote(21050.0)
        self.assertEqual(self.kinds(), ['bracket_entry'])


class Trail(Base):
    def test_trail_on_exits_the_runner_after_activation(self):
        plan = entry(qty=1, runner={'mode': 'trail', 'trail_activation_ticks': 24, 'trail_distance_ticks': 12})
        self.enter(plan, avg=21000.0)
        self.assertEqual(self.kinds(), ['bracket_entry'])
        self.quote(21006.0)                                  # +24 ticks: trail activates, best = 21006.0
        self.quote(21009.0)                                  # best improves to 21009.0, trail level 21006.0
        self.quote(21006.25)
        self.assertEqual(len(self.bridge.sent), 1, 'not pulled back 12 ticks yet')
        self.quote(21006.0)                                  # pulled back exactly 12 ticks from the best
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'runner_trail')
        self.assert_all_valid()

    def test_trail_off_leaves_the_runner_on_hold(self):
        self.enter(entry(runner={'mode': 'hold'}))
        self.quote(21100.0)
        self.assertEqual(self.kinds(), ['bracket_entry'])


class DailyLoss(Base):
    def lose(self, plan):
        self.enter(plan)
        self.book.fill(self.book.entry + '#stop', 20983.0)
        self.evidence()
        self.assertEqual(self.trade['phase'], 'closed')

    def test_on_without_flatten_open_only_blocks_the_next_entry(self):
        """2 contracts stopped 17 points down is a realized loss of about USD 70 (point value + fee).
        A limit below that locks the day; one well above it (later test) does not."""
        lock = dict(daily_loss={'enabled': True, 'limit_usd': 32.0})
        self.lose(entry(**lock))
        self.now += 60_000
        self.quote(21000.0, settle=False)
        refused = self.alert(entry(trade_id=TRADE2, ts=self.now, **lock))
        self.assertEqual(refused, 'refused:daily_loss_locked')

    def test_off_allows_the_next_entry(self):
        self.lose(entry(daily_loss={'enabled': False}))
        self.now += 60_000
        self.quote(21000.0, settle=False)
        self.assertEqual(self.alert(entry(trade_id=TRADE2, ts=self.now)), 'entry_accepted_not_sent')

    def test_limit_above_the_loss_does_not_lock(self):
        self.lose(entry(daily_loss={'enabled': True, 'limit_usd': 100.0}))
        self.now += 60_000
        self.quote(21000.0, settle=False)
        self.assertEqual(self.alert(entry(trade_id=TRADE2, ts=self.now, daily_loss={'enabled': True, 'limit_usd': 100.0})),
                         'entry_accepted_not_sent')

    def test_on_with_flatten_open_closes_the_open_trade_at_the_limit(self):
        plan = entry(qty=1, daily_loss={'enabled': True, 'limit_usd': 25.0, 'include_open': True, 'flatten': True})
        self.enter(plan, avg=21000.0)
        self.quote(20995.0)                                  # open loss (1 x 2 x 5 = 10): not past the limit
        self.assertEqual(self.kinds(), ['bracket_entry'])
        self.quote(20987.0)                                  # open loss 1 x 2 x 13 = 26 > 25
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'daily_loss')


class AllFeatures(Base):
    def test_every_optional_exit_on_at_once_is_accepted_and_valid(self):
        plan = entry(qty=5, targets=[{'price': 21011.0, 'qty': 2, 'type': 'limit'}],
                     break_even={'enabled': True, 'trigger_price': 21006.0, 'offset_ticks': 3},
                     runner={'mode': 'trail', 'trail_activation_ticks': 24, 'trail_distance_ticks': 12},
                     daily_loss={'enabled': True, 'limit_usd': 100.0, 'include_open': True})
        self.enter(plan, avg=21000.0)
        self.assertEqual(self.kinds(), ['bracket_entry'])
        self.assertEqual(self.bridge.sent[0]['extras']['tp1_price'], 21011.0)
        self.assert_all_valid()


if __name__ == '__main__':
    unittest.main()
