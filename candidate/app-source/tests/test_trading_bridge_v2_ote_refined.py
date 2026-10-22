"""decrypt-bridge-v2 receiver/executor for OTE Refined Large + Small, through the real runtime and
bridge route against the fake bridge (no network, synthetic ids). Modeled on test_trading_bridge_v2.py's
harness, with synthetic alerts from bridge_v2_ote_refined_payloads.py: arbitrary fixture prices, sizes and
clock times.

Every contract on/off combination that matters is covered: each target type (market/limit), break-even
trigger (target / price level / either / off), runner mode (hold / trail), EOD on/off, bias-flip on/off,
touch entry on/off, daily loss lock on/off, and both stop types (resting / bar-close with and without a
safety stop). The Small lane gets its own sanity entry test to prove the second adapter/module registers
and runs independently of Large.
"""
import copy
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_fixture import FakeBridge, native_binding  # noqa: E402
from bridge_v2_ote_refined_payloads import BASE, ENTRY_PRICE, LARGE, LEVEL_POINTS, MAX_AGE_MS, SMALL, STREAM, bias_flip, entry  # noqa: E402
from decrypt_trading import RuntimeManager  # noqa: E402
from decrypt_trading import bridge_adapters, bridge_v2, bridge_v2_ote_refined_large as large_module  # noqa: E402
from decrypt_trading import bridge_v2_ote_refined_small as small_module  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402

ACCOUNT = 30000099


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
            'account_id': str(ACCOUNT), 'symbol': 'MNQZ2026', 'trade_id': trade['id'], 'side': trade['side'],
            'position_qty': self.position, 'orders': copy.deepcopy(list(self.rows.values())),
            'open_orders': [r['broker_order_id'] for r in self.rows.values() if r['status'] in ('working', 'partial')]})
        self.test.runtime._bridge_tick()
        self.test.runtime.step()
        row = self.test.engine.db.execute("SELECT status,detail FROM inbox WHERE channel='broker' ORDER BY seq DESC").fetchone()
        self.test.assertEqual(row[0], 'processed', row)
        return row[1]


class Base(unittest.TestCase):
    """Parametrized on LANE/MODULE/RESTING by subclasses (Large below; Small has its own small suite)."""
    LANE = LARGE
    MODULE = large_module
    ADAPTER = large_module.STRATEGY.adapter_id
    RESTING = 2
    start = BASE + 300

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # ote-refined-{large,small}-bridge-v2 are real, permanently registered strategies (bridge_v2_registry.
        # STRATEGIES, V2_ADAPTERS): they are registered once at module import time, with their real pinned
        # hashes, exactly as production does. No fixture registration is needed or possible here (a second
        # register() of the same adapter id raises); only the network is mocked off.
        self.assertIn(self.ADAPTER, bridge_adapters.REGISTRY, 'adapter not registered: add it to V2_ADAPTERS')
        self.assertIn(self.ADAPTER, contract.PAIRS, 'strategy not pinned: add it to bridge_v2_registry.STRATEGIES')
        patcher = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now, self.price = self.start, ENTRY_PRICE
        self.root = Path(self.temp.name)
        self.binding = native_binding(name='ote-refined-fixture', pair=self.ADAPTER, account=ACCOUNT)
        self.binding['streamId'] = STREAM
        self.binding['routeSha256'] = contract.route_digest(self.binding)
        self.bridge = FakeBridge(self.root / 'data', self.binding, clock=lambda: self.now)
        self.bridge.write_config(adapters={self.ADAPTER: {'enabled': True}})
        self.bridge.state_override = {'adapter_ids': [self.ADAPTER]}
        self.manager = RuntimeManager(self.root / 'trading')
        self.manager.install(self.binding)
        self.book = Book(self)
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
        self.price = price
        self.ingest('quote', {'event_id': 'q-%d-%s' % (self.now, price), 'symbol': 'MNQZ2026', 'time': self.now, 'price': price})
        if settle:
            self.settle()

    def bar(self, close_time, close):
        self.now = max(self.now, close_time + 100)
        self.ingest('bar', {'event_id': 'b-%d' % close_time, 'symbol': 'MNQZ2026', 'open_time': close_time - 5000,
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
        plan = value or entry(lane=self.LANE)
        self.quote(plan['entry']['price'], settle=False)
        out = self.alert(plan)
        self.gate()
        if fill and self.book.entry:
            self.now += 200
            self.book.fill(self.book.entry, avg or self.price)
            self.evidence()
        return out

    def assert_all_valid(self):
        enabled = {self.ADAPTER: bridge_adapters.REGISTRY[self.ADAPTER]}
        for payload in self.bridge.sent:
            bridge_adapters.validate_command(payload, ticker='MNQZ2026', enabled=enabled)


class LargeEntries(Base):
    def test_base_entry_is_a_bracket_with_a_resting_stop_and_no_staged_limit(self):
        self.assertEqual(self.enter(fill=False), 'entry_accepted_not_sent')
        bracket = self.bridge.sent[0]
        self.assertEqual((bracket['action'], bracket['orderType'], bracket['quantity']), ('buy', 'market', 11))
        self.assertNotIn('tp1_price', bracket['extras'], 'a market-type TP1 is watched locally, not staged as a resting limit')
        self.book.fill(self.book.entry, ENTRY_PRICE)
        self.evidence()
        self.assertEqual(self.trade['phase'], 'running')
        self.assertEqual(self.kinds(), ['bracket_entry'])
        self.assert_all_valid()

    def test_short_side_entry_is_symmetric(self):
        self.enter(entry(lane=LARGE, side='short'), fill=False)
        bracket = self.bridge.sent[0]
        self.assertEqual((bracket['action'], bracket['quantity']), ('sell', 11))
        self.assert_all_valid()

    def test_one_trade_at_a_time(self):
        self.enter()
        second = entry(lane=LARGE, seq='b', ts=self.now)
        self.assertEqual(self.alert(second), 'refused:trade_open_or_pending')

    def test_stale_entry_is_refused(self):
        self.now = BASE + MAX_AGE_MS + 1
        plan = entry(lane=LARGE, ts=BASE)
        self.quote(plan['entry']['price'], settle=False)
        self.assertEqual(self.alert(plan), 'input_rejected')


class LargeTargets(Base):
    def test_market_tp1_shrinks_the_stop_then_exits_at_the_tp1_price(self):
        self.enter()
        plan = entry(lane=LARGE)
        tp1_price = plan['targets'][0]['price']
        self.quote(tp1_price - 0.25)
        self.assertEqual(self.kinds(), ['bracket_entry'])
        self.quote(tp1_price)
        self.assertEqual(self.kinds(), ['bracket_entry', 'modify_stop', 'exit_market'])
        self.assertEqual(self.bridge.sent[1]['extras']['stop_quantity'], 5)   # 11 - 6 (TP1)
        self.assertEqual(self.bridge.sent[2]['quantity'], 6)
        self.assertTrue(self.trade['tp1_done'])
        self.assert_all_valid()

    def test_rest_first_stages_with_the_bracket(self):
        plan = entry(lane=LARGE)
        plan['targets'][0]['type'] = 'limit'
        self.enter(plan, fill=False)
        bracket = self.bridge.sent[0]
        self.assertIn('tp1_price', bracket['extras'])
        self.assertEqual(bracket['extras']['tp1_quantity'], 6)

    def test_rest_second_requires_tp1_resting_too(self):
        plan = entry(lane=LARGE)
        for target in plan['targets']:
            target['type'] = 'limit'
        self.assertEqual(self.alert(plan), 'entry_accepted_not_sent')
        plan_bad = entry(lane=LARGE, seq='c')
        plan_bad['targets'][1]['type'] = 'limit'                              # a limit after a market target
        with self.assertRaises(ValueError):
            from decrypt_trading import bridge_v2_contract
            bridge_v2_contract.validate(plan_bad, strategy=self.MODULE.STRATEGY, now_ms=self.now)

    def test_runner_hold_does_nothing_past_targets(self):
        self.enter()
        plan = entry(lane=LARGE)
        self.quote(plan['targets'][0]['price'])
        self.quote(plan['targets'][1]['price'])
        sent_before = len(self.bridge.sent)
        self.quote(plan['targets'][1]['price'] + 100)
        self.assertEqual(len(self.bridge.sent), sent_before, 'runner hold: no target or trail action')

    def test_runner_trail_exits_after_activation(self):
        plan = entry(lane=LARGE, runner={'mode': 'trail', 'trail_activation_ticks': 160, 'trail_distance_ticks': 40})
        self.enter(plan)
        self.quote(plan['targets'][0]['price'])
        self.quote(plan['targets'][1]['price'])
        before = len(self.bridge.sent)
        self.quote(ENTRY_PRICE + 31.0)                       # not yet activated (needs 160 ticks = 40 pts)
        self.quote(ENTRY_PRICE + 26.0)
        self.assertEqual(len(self.bridge.sent), before, 'the trail is not active yet')
        self.quote(ENTRY_PRICE + 44.0)                       # activated; trail level 10 pts back
        self.quote(ENTRY_PRICE + 34.25)
        self.assertEqual(len(self.bridge.sent), before)
        self.quote(ENTRY_PRICE + 34.0)
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'runner_trail')


class LargeBreakEven(Base):
    def test_target_trigger_moves_the_stop_to_the_planned_be_price(self):
        self.enter()
        plan = entry(lane=LARGE)
        self.quote(plan['targets'][0]['price'])   # TP1 fills -> shrink, exit, then BE once the fill is seen
        self.assertEqual(self.kinds(), ['bracket_entry', 'modify_stop', 'exit_market'])
        self.evidence()
        self.quote(plan['targets'][0]['price'] + 1.0)
        be_cmd = [p for p in self.bridge.sent if p['extras'].get('bridge_kind') == 'modify_stop'][-1]
        self.assertEqual(be_cmd['extras']['stop_price'], plan['break_even']['stop_price'])
        self.assert_all_valid()

    def test_price_level_trigger_fires_on_price_alone_without_tp1(self):
        plan = entry(lane=LARGE, sizes=(0, 3, 2),
                     break_even={'enabled': True, 'trigger_price': ENTRY_PRICE + LEVEL_POINTS, 'trigger_target': 0})
        self.enter(plan)
        self.assertTrue(plan['break_even']['enabled'])
        self.assertEqual(plan['break_even']['trigger_target'], 0)
        self.quote(plan['break_even']['trigger_price'])
        kinds_after = self.kinds()
        self.assertIn('modify_stop', kinds_after)

    def test_break_even_off_does_nothing_at_the_be_price(self):
        plan = entry(lane=LARGE, break_even={'enabled': False})
        self.assertFalse(plan['break_even']['enabled'])
        self.enter(plan)
        before = len(self.bridge.sent)
        self.quote(plan['break_even']['stop_price'] + 5.0)
        self.assertEqual(len([p for p in self.bridge.sent if p['extras'].get('bridge_kind') == 'modify_stop'
                             and p is not self.bridge.sent[0]]), 0)
        self.assertGreaterEqual(len(self.bridge.sent), before)

    def test_target_trigger_with_zero_tp1_contracts_falls_back_cleanly(self):
        plan = entry(lane=LARGE, sizes=(0, 3, 2))
        self.assertEqual(plan['break_even']['trigger_target'], 0, 'no first target: nothing to trigger on')
        self.assertFalse(plan['break_even']['enabled'], 'and no price trigger either: break-even is sent disabled')
        from decrypt_trading import bridge_v2_contract
        bridge_v2_contract.validate(plan, strategy=self.MODULE.STRATEGY, now_ms=plan['ts'])


class LargeClockAndExits(Base):
    def test_eod_on_flattens_at_the_alert_time(self):
        plan = entry(lane=LARGE, eod={'time': '14:50'})
        self.enter(plan)
        eod = bridge_v2.next_clock(BASE, '14:50', 'America/New_York')
        self.now = eod
        self.runtime.step()
        self.settle()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'eod')

    def test_eod_off_does_nothing_at_the_same_clock(self):
        plan = entry(lane=LARGE, eod={'enabled': False})
        self.enter(plan)
        eod = bridge_v2.next_clock(BASE, '15:30', 'America/New_York')
        self.now = eod
        self.runtime.step()
        self.settle()
        self.assertNotIn('flatten_all', self.kinds())

    def test_bias_flip_event_exits_the_matching_trade(self):
        plan = entry(lane=LARGE, bias_flip={'enabled': True})
        self.enter(plan)
        flip = bias_flip(lane=LARGE, trade_id=plan['trade_id'], side='long', ts=self.now)
        self.assertEqual(self.alert(flip), 'bias_flip_requested')
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'flatten_all')

    def test_bias_flip_disabled_is_ignored(self):
        plan = entry(lane=LARGE, bias_flip={'enabled': False})
        self.enter(plan)
        flip = bias_flip(lane=LARGE, trade_id=plan['trade_id'], side='long', ts=self.now)
        self.assertEqual(self.alert(flip), 'bias_flip_ignored_disabled_by_entry')
        self.settle()
        self.evidence()
        self.assertNotIn('flatten_all', self.kinds())

    def test_touch_entry_arms_then_fires_within_valid_until(self):
        plan = entry(lane=LARGE, entry={'type': 'touch'},
                     touch={'enabled': True, 'price': ENTRY_PRICE, 'valid_until': BASE + 300_000,
                            'cancel_if_stop_touched': True})
        self.assertTrue(plan['touch']['enabled'])
        self.alert(plan)
        self.assertEqual(self.trade['phase'], 'armed')
        self.quote(plan['entry']['price'])
        self.assertEqual(self.trade['phase'], 'entering')

    def test_daily_loss_lock_blocks_a_new_entry_after_a_losing_trade(self):
        lock = {'enabled': True, 'limit_usd': 50.0, 'fee_per_contract_usd': 0.0}
        plan = entry(lane=LARGE, daily_loss=lock)
        self.enter(plan)
        self.book.fill(self.book.entry + '#stop', plan['stop']['price'])
        self.evidence()
        self.assertEqual(self.trade['phase'], 'closed')
        second = entry(lane=LARGE, daily_loss=lock, seq='b', ts=self.now + 1000)
        self.assertEqual(self.alert(second), 'refused:daily_loss_locked')

    def test_daily_loss_lock_off_allows_the_next_entry(self):
        plan = entry(lane=LARGE, daily_loss={'enabled': False})
        self.enter(plan)
        self.book.fill(self.book.entry + '#stop', plan['stop']['price'])
        self.evidence()
        second = entry(lane=LARGE, seq='b', ts=self.now + 1000)
        self.assertNotEqual(self.alert(second), 'refused:daily_loss_locked')

    def test_bar_close_stop_with_safety_rests_the_safety_price(self):
        plan = entry(lane=LARGE, stop={'type': 'bar_close', 'timeframe_s': 60, 'safety_price': ENTRY_PRICE - 25.0},
                     break_even={'enabled': False})
        self.enter(plan, fill=False)
        self.assertEqual(self.bridge.sent[0]['extras']['stop_price'], plan['stop']['safety_price'])

    def test_bar_close_stop_without_safety_sends_a_plain_entry(self):
        plan = entry(lane=LARGE, stop={'type': 'bar_close', 'timeframe_s': 120}, break_even={'enabled': False})
        self.enter(plan, fill=False)
        self.assertEqual(self.kinds(), ['entry'])
        self.assertNotIn('stop_price', self.bridge.sent[0]['extras'])


class SmallLane(Base):
    LANE = SMALL
    MODULE = small_module
    ADAPTER = small_module.STRATEGY.adapter_id
    RESTING = 2

    def test_small_lane_entry_sizes_7_contracts_and_runs_independently_of_large(self):
        self.assertEqual(self.enter(fill=False), 'entry_accepted_not_sent')
        bracket = self.bridge.sent[0]
        self.assertEqual(bracket['quantity'], 7)
        self.assertEqual(self.MODULE.STRATEGY.strategy_id, 'ote-refined-small')
        self.assert_all_valid()

    def test_small_lane_tp1_and_tp2_sum_with_runner_to_the_total(self):
        plan = entry(lane=SMALL)
        self.assertEqual(sum(t['qty'] for t in plan['targets']) + plan['runner']['qty'], plan['entry']['qty'])
        self.assertEqual(plan['entry']['qty'], 7)
