"""decrypt-bridge-v2 receiver/executor, runtime side: a registered v2 strategy (tests/bridge_v2_example_strategy)
on a native binding, through the real runtime and bridge route against the fake bridge (no network, synthetic
ids). `Book` plays the broker: it turns what the runtime sent into the bridge's broker evidence.

Every feature is tested on and off: an alert that switches a feature off must make the executor do nothing for it.
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
import bridge_v2_example_strategy as example  # noqa: E402
from bridge_v2_payloads import BASE, TRADE, entry, event  # noqa: E402
from decrypt_trading import RuntimeManager, RuntimeError as EngineError  # noqa: E402
from decrypt_trading import bridge_adapters, bridge_v2, source  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402

ADAPTER = example.STRATEGY.adapter_id
ACCOUNT = 30000042
ENGINE_SHA = hashlib.sha256(Path(example.__file__).read_bytes()).hexdigest()
STRATEGY_SHA = '6' * 64
TRADE2 = 'ex1:MNQZ2026:%d:long' % (BASE + 3_600_000)


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


class V2Base(unittest.TestCase):
    start = BASE + 300
    resting = 3

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for patcher in (patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')),
                        patch.dict(bridge_adapters.REGISTRY, {}), patch.dict(source.BRIDGE_ENGINES, {}),
                        patch.dict(contract.PAIRS, {ADAPTER: (ENGINE_SHA, STRATEGY_SHA)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Exactly what one line in bridge_adapters.V2_ADAPTERS and bridge_v2_registry.STRATEGIES register.
        bridge_adapters.register(bridge_adapters.Adapter(
            ADAPTER, 'Example v2', schemas=(bridge_adapters.V2_SCHEMA,), roots=('MNQ', 'MES'),
            kinds=bridge_adapters.V2_KINDS, take_profits=self.resting, profile=bridge_adapters.V2_PROFILE))
        source.register_bridge_engine(ADAPTER, module='bridge_v2_example_strategy',
                                      files={'bridge_v2_example_strategy.py': ENGINE_SHA}, strategy_sha256=STRATEGY_SHA)
        self.now, self.price = self.start, 21000.0
        self.root = Path(self.temp.name)
        self.binding = native_binding(name='v2-fixture', pair=ADAPTER, account=ACCOUNT)
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
        self.ingest('quote', {'event_id': 'q-%d' % self.quotes, 'symbol': 'MNQZ2026', 'time': self.now, 'price': price})
        if settle:
            self.settle()

    def bar(self, close_time, close):
        self.now = max(self.now, close_time + 100)
        self.bars += 1
        self.ingest('bar', {'event_id': 'b-%d' % self.bars, 'symbol': 'MNQZ2026', 'open_time': close_time - 5000,
                            'close_time': close_time, 'open': close, 'high': close, 'low': close, 'close': close})
        self.settle()

    def gate(self):
        self.transport.route.refresh()
        self.runtime.step()
        self.settle()

    def settle(self):
        """Broker evidence (the bridge republishes every 2 s) for whatever was sent, until nothing more is sent."""
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
        self.quote((value or entry())['entry']['price'], settle=False)
        out = self.alert(value or entry())
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


class Entries(V2Base):
    def test_bracket_entry_carries_the_alert_stop_and_first_limit_then_places_the_rest(self):
        self.assertEqual(self.enter(fill=False), 'entry_accepted_not_sent')
        bracket = self.bridge.sent[0]
        self.assertEqual((bracket['action'], bracket['orderType'], bracket['quantity']), ('buy', 'market', 4))
        self.assertEqual(bracket['extras'], {
            'adapter_id': ADAPTER, 'adapter_schema': 'decrypt-bridge-v2', 'command_id': self.book.entry,
            'trade_id': TRADE, 'signal_time': BASE, 'bridge_kind': 'bracket_entry', 'reference_price': 21000.0,
            'stop_price': 20980.0, 'tp1_price': 21010.0, 'tp1_quantity': 1})
        self.book.fill(self.book.entry, 21000.25)
        self.evidence()
        self.assertEqual(self.trade['phase'], 'running')
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])
        self.assertEqual((self.bridge.sent[1]['limitPrice'], self.bridge.sent[1]['quantity']), (21020.0, 1))
        self.quote(21005.0)
        self.assertEqual(len(self.bridge.sent), 2, 'nothing else: the runner holds')
        self.assert_all_valid()

    def test_no_contract_cap_the_alert_size_is_sent(self):
        self.enter(entry(entry={'qty': 40}, targets=[{'price': 21010.0, 'qty': 10, 'type': 'limit'}],
                         runner={'qty': 30}), fill=False)
        self.assertEqual(self.bridge.sent[0]['quantity'], 40)
        self.assertEqual(self.bridge.sent[0]['extras']['tp1_quantity'], 10)

    def test_stale_entry_and_unknown_field_are_refused(self):
        self.now = BASE + 40_001
        self.quote(21000.0, settle=False)
        self.assertEqual(self.alert(entry()), 'input_rejected')
        with self.assertRaisesRegex(ValueError, 'old'):
            self.engine.validate_pine(entry(), self.now)
        self.now = BASE + 1000
        bad = entry(trade_id=TRADE2)
        bad['size_cap'] = 10
        self.assertEqual(self.alert(bad), 'input_rejected')
        with self.assertRaisesRegex(ValueError, 'unknown field'):
            self.engine.validate_pine(bad, self.now)
        other = entry(trade_id=TRADE2)
        other['protocol'] = 'decrypt-bridge-v1'
        with self.assertRaisesRegex(EngineError, 'schema is not declared'):
            self.runtime.ingest('pine', json.dumps(other), producer_id=self.binding['signalProducerId'])
        self.assertEqual(self.bridge.sent, [])

    def test_one_trade_at_a_time_and_trade_ids_are_used_once(self):
        self.enter()
        second = entry(trade_id=TRADE2, ts=self.now)
        self.assertEqual(self.alert(second), 'refused:trade_open_or_pending')
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])

    def test_bar_close_stop_without_safety_sends_a_plain_entry_and_watches_closes(self):
        plan = entry(stop={'type': 'bar_close', 'price': 20980.0, 'timeframe_s': 60}, targets=[], runner={'qty': 4})
        self.enter(plan)
        self.assertEqual(self.kinds(), ['entry'])
        self.assertNotIn('stop_price', self.bridge.sent[0]['extras'])
        self.assertEqual(self.engine.market_bar_intervals(), (5000,))
        minute = (self.now // 60_000 + 1) * 60_000
        self.bar(minute - 5000, 20975.0)                    # beyond, but not the minute's close
        self.bar(minute, 20990.0)                           # the minute closes above: held
        self.assertEqual(self.kinds(), ['entry'])
        self.bar(minute + 55_000, 20975.0)
        self.bar(minute + 60_000, 20980.0)                  # closes at the level: exit everything
        self.assertEqual(self.kinds(), ['entry', 'flatten_all'])
        self.assertEqual(self.trade['phase'], 'closed')
        self.assertEqual(self.trade['closed_reason'], 'bar_close_stop')
        self.assert_all_valid()

    def test_bar_close_stop_with_a_safety_stop_rests_the_safety_price(self):
        plan = entry(stop={'type': 'bar_close', 'price': 20990.0, 'timeframe_s': 60, 'safety_price': 20965.0})
        self.enter(plan, fill=False)
        self.assertEqual(self.bridge.sent[0]['extras']['stop_price'], 20965.0)

    def test_resting_stop_fill_closes_the_trade_and_cancels_leftovers(self):
        self.enter()
        self.book.fill(self.book.entry + '#stop', 20980.0)
        self.evidence()
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'flatten_all'])
        self.assertEqual(self.trade['phase'], 'closed')


class Targets(V2Base):
    def test_limit_target_fill_shrinks_the_stop_and_break_even_off_does_nothing(self):
        self.enter()
        self.quote(21012.0)                                  # beyond the (off) break-even trigger price
        self.book.fill(self.book.entry + '#tp1', 21010.0)
        self.evidence()
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'modify_stop'])
        shrink = self.bridge.sent[-1]['extras']
        self.assertEqual((shrink['stop_quantity'], shrink['stop_price']), (3, 20980.0))
        self.quote(21015.0)
        self.assertEqual(len(self.bridge.sent), 3)
        self.assertTrue(self.trade['tp1_done'])
        self.assert_all_valid()

    def test_market_targets_shrink_the_stop_first_then_exit(self):
        plan = entry(targets=[{'price': 21010.0, 'qty': 1, 'type': 'market'}, {'price': 21020.0, 'qty': 1, 'type': 'market'}])
        self.enter(plan)
        self.assertEqual(self.kinds(), ['bracket_entry'])
        self.quote(21009.75)
        self.assertEqual(len(self.bridge.sent), 1)
        self.quote(21010.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'modify_stop', 'exit_market'])
        self.assertEqual(self.bridge.sent[1]['extras']['stop_quantity'], 3)
        self.assertEqual(self.bridge.sent[2]['quantity'], 1)
        self.quote(21020.0)
        self.assertEqual(self.kinds()[3:], ['modify_stop', 'exit_market'])
        self.assertEqual(self.book.position, 2)
        self.assertEqual(self.trade['phase'], 'running')
        self.assert_all_valid()

    def test_a_market_target_that_closes_the_position_flattens(self):
        plan = entry(targets=[{'price': 21010.0, 'qty': 4, 'type': 'market'}], runner={'qty': 0})
        self.enter(plan)
        self.quote(21011.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'flatten_all'])
        self.assertEqual(self.trade['phase'], 'closed')

    def test_a_market_target_waits_for_the_nearer_resting_limit(self):
        plan = entry(targets=[{'price': 21010.0, 'qty': 1, 'type': 'limit'}, {'price': 21020.0, 'qty': 1, 'type': 'market'}])
        self.enter(plan)
        self.quote(21021.0)
        self.assertEqual(self.kinds(), ['bracket_entry'], 'the TP1 limit still rests: no market exit')
        self.book.fill(self.book.entry + '#tp1', 21010.0)
        self.evidence()
        self.quote(21021.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'modify_stop', 'modify_stop', 'exit_market'])
        self.assertEqual(self.book.position, 2)

    def test_runner_target_flattens_and_runner_hold_does_not(self):
        self.enter(entry(runner={'mode': 'target', 'price': 21040.0}))
        self.quote(21039.75)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])
        self.quote(21040.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'flatten_all'])
        self.assertEqual(self.trade['closed_reason'], 'runner_target')

    def test_runner_trail_exits_the_runner_after_activation(self):
        plan = entry(targets=[{'price': 21010.0, 'qty': 2, 'type': 'market'}],
                     runner={'mode': 'trail', 'trail_activation_ticks': 80, 'trail_distance_ticks': 40})
        self.enter(plan)
        self.quote(21010.0)                                  # target: shrink + exit 2
        self.assertEqual(self.kinds(), ['bracket_entry', 'modify_stop', 'exit_market'])
        self.quote(21015.0)
        self.quote(21012.0)                                  # not yet activated (needs 20 points)
        self.assertEqual(len(self.bridge.sent), 3)
        self.quote(21030.0)                                  # activated; trail level 21020
        self.quote(21020.25)
        self.assertEqual(len(self.bridge.sent), 3)
        self.quote(21020.0)
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'runner_trail')


class BreakEven(V2Base):
    def test_touch_trigger_moves_the_stop_to_the_alert_price(self):
        self.enter(entry(break_even={'enabled': True, 'trigger_price': 21010.0, 'stop_price': 21002.0}))
        self.quote(21009.75)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])
        self.quote(21010.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'modify_stop'])
        be = self.bridge.sent[-1]['extras']
        self.assertEqual((be['stop_price'], be['stop_quantity']), (21002.0, 4))
        self.quote(21011.0)
        self.assertEqual(len(self.bridge.sent), 3, 'break-even is applied once')

    def test_target_fill_then_bar_close_uses_the_real_fill_plus_offset(self):
        plan = entry(break_even={'enabled': True, 'trigger_price': 0, 'trigger_target': 1, 'confirm': 'bar_close',
                                 'confirm_timeframe_s': 120, 'offset_ticks': 6})
        self.enter(plan, avg=21000.5)
        self.book.fill(self.book.entry + '#tp1', 21010.0)
        filled_at = self.now
        self.evidence()                                      # shrink to 3
        bar_end = (filled_at // 120_000 + 1) * 120_000
        self.now = bar_end - 1000
        self.quote(21011.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'modify_stop'])
        self.now = bar_end
        self.quote(21011.0)
        be = self.bridge.sent[-1]['extras']
        self.assertEqual((be['stop_price'], be['stop_quantity']), (21002.0, 3))   # 21000.5 + 6 ticks

    def test_price_through_the_level_flattens_or_skips_as_the_alert_says(self):
        self.enter(entry(break_even={'enabled': True, 'trigger_target': 1, 'trigger_price': 0, 'stop_price': 21006.0}))
        self.book.fill(self.book.entry + '#tp1', 21010.0)
        self.price = 21005.0
        self.evidence()
        self.quote(21005.0)                                  # back through the 21006.0 level
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'break_even_price_through')

    def test_skip_when_through(self):
        self.enter(entry(break_even={'enabled': True, 'trigger_target': 1, 'trigger_price': 0, 'stop_price': 21006.0,
                                     'if_beyond': 'skip'}))
        self.book.fill(self.book.entry + '#tp1', 21010.0)
        self.evidence()
        self.quote(21005.0)                                  # back through the 21006.0 level
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit', 'modify_stop'])
        self.assertEqual(self.trade['be']['state'], 'skipped_price_through')

    def test_all_needs_every_trigger(self):
        self.enter(entry(break_even={'enabled': True, 'trigger_price': 21005.0, 'trigger_target': 1,
                                     'trigger_mode': 'all', 'stop_price': 21001.0}))
        self.quote(21006.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])
        self.book.fill(self.book.entry + '#tp1', 21010.0)
        self.evidence()
        self.quote(21008.0)
        self.assertEqual(self.bridge.sent[-1]['extras']['stop_price'], 21001.0)

    def test_bar_close_break_even_moves_the_watched_level_only(self):
        plan = entry(stop={'type': 'bar_close', 'price': 20980.0, 'timeframe_s': 60}, targets=[], runner={'qty': 4},
                     break_even={'enabled': True, 'trigger_price': 21010.0, 'stop_price': 21002.0,
                                 'stop_type': 'bar_close', 'stop_timeframe_s': 30})
        self.enter(plan)
        self.quote(21010.0)
        self.assertEqual(self.kinds(), ['entry'])
        self.assertEqual(self.trade['close_stop'], {'price': 21002.0, 'tf': 30})
        half = (self.now // 30_000 + 1) * 30_000
        self.bar(half - 5000, 21003.0)
        self.bar(half, 21001.0)
        self.assertEqual(self.kinds(), ['entry', 'flatten_all'])


class Clock(V2Base):
    def test_end_of_day_on_flattens_at_the_alert_time(self):
        self.enter(entry(eod={'enabled': True, 'time': '15:30'}))
        eod = bridge_v2.next_clock(BASE, '15:30', 'America/New_York')
        self.assertEqual(eod - BASE, 5 * 60 * 60_000)
        self.now = eod - 1000
        self.evidence()
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])
        self.now = eod
        self.runtime.step()
        self.settle()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'eod')

    def test_end_of_day_off_does_nothing(self):
        self.enter(entry(eod={'enabled': False, 'time': '10:31'}))
        self.now += 120_000
        self.evidence()
        self.runtime.step()
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])

    def test_next_clock_and_day_key(self):
        evening = BASE + 9 * 3_600_000                       # 19:30 New York: EOD 15:30 is tomorrow
        self.assertEqual(bridge_v2.next_clock(evening, '15:30', 'America/New_York') - evening, 20 * 60 * 60_000)
        self.assertEqual(bridge_v2.day_key(BASE, 'America/New_York', '12:00'), '2021-06-08')
        self.assertEqual(bridge_v2.day_key(evening, 'America/New_York', '12:00'), '2021-06-09')
        self.assertEqual(bridge_v2.day_key(BASE, 'America/New_York', '00:00'), '2021-06-09')
        self.assertEqual(bridge_v2.day_key(evening, 'America/New_York', '00:00'), '2021-06-09')


class DailyLoss(V2Base):
    def lose(self, plan):
        """One losing trade: 4 MNQ stopped 20 points down (USD 160 + 4 x 1.00 fees)."""
        self.enter(plan)
        self.book.fill(self.book.entry + '#stop', 20980.0)
        self.evidence()
        self.assertEqual(self.trade['phase'], 'closed')

    def test_on_refuses_the_next_entry_for_the_day_and_allows_the_next_day(self):
        lock = {'enabled': True, 'limit_usd': 150}
        self.lose(entry(daily_loss=lock))
        self.assertAlmostEqual(self.engine.day_net(self.now, entry(daily_loss=lock)['daily_loss'], include_open=False),
                               -164.0)
        self.now += 60_000
        self.quote(21000.0, settle=False)
        self.assertEqual(self.alert(entry(trade_id=TRADE2, ts=self.now, daily_loss=lock)), 'refused:daily_loss_locked')
        tomorrow = BASE + 86_400_000
        self.now = tomorrow
        self.quote(21000.0, settle=False)
        third = 'ex1:MNQZ2026:%d:long' % tomorrow
        self.assertEqual(self.alert(entry(trade_id=third, ts=self.now, daily_loss=lock)), 'entry_accepted_not_sent')

    def test_off_allows_the_next_entry(self):
        self.lose(entry(daily_loss={'enabled': False, 'limit_usd': 150}))
        self.now += 60_000
        self.quote(21000.0, settle=False)
        self.assertEqual(self.alert(entry(trade_id=TRADE2, ts=self.now)), 'entry_accepted_not_sent')

    def test_flatten_with_open_loss_closes_the_trade(self):
        self.enter(entry(daily_loss={'enabled': True, 'limit_usd': 100, 'include_open': True, 'flatten': True}))
        self.quote(20990.0)                                  # open: -10 x 4 x 2 - fees = -84.00
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])
        self.quote(20987.0)                                  # -108.00
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'daily_loss')

    def test_missing_fill_prices_fail_closed(self):
        self.lose(entry(daily_loss={'enabled': True, 'limit_usd': 150}))
        self.engine.data['realized'][-1][1] = None
        self.now += 60_000
        self.quote(21000.0, settle=False)
        self.assertEqual(self.alert(entry(trade_id=TRADE2, ts=self.now, daily_loss={'enabled': True})),
                         'refused:daily_loss_pnl_unknown')


class Touch(V2Base):
    def plan(self, **touch):
        return entry(entry={'type': 'touch', 'price': 20995.0},
                     touch=dict({'enabled': True, 'price': 20995.0, 'valid_until': BASE + 3_600_000}, **touch),
                     stop={'price': 20975.0})

    def test_armed_then_sent_at_the_touch_with_the_touch_time(self):
        self.assertEqual(self.alert(self.plan()), 'touch_entry_armed')
        self.now += 600_000
        self.quote(20996.0)
        self.assertEqual(self.bridge.sent, [])
        self.quote(20995.0)
        touched = self.now
        self.gate()
        self.assertEqual(self.kinds(), ['bracket_entry'])
        extras = self.bridge.sent[0]['extras']
        self.assertEqual((extras['signal_time'], extras['reference_price'], extras['stop_price']),
                         (touched, 20995.0, 20975.0))
        self.assert_all_valid()

    def test_an_old_arm_is_accepted_until_valid_until(self):
        self.now = BASE + 1_800_000
        self.quote(21000.0, settle=False)
        self.assertEqual(self.alert(self.plan()), 'touch_entry_armed')

    def test_expiry_cancel_entry_and_stop_first_disarm(self):
        self.alert(self.plan())
        self.now = BASE + 3_600_000
        self.runtime.step()
        self.assertEqual((self.trade['phase'], self.trade['closed_reason']), ('closed', 'touch_expired'))
        second = TRADE2
        self.now = BASE + 3_600_500
        self.quote(21000.0, settle=False)
        self.alert(entry(trade_id=second, ts=self.now, entry={'type': 'touch', 'price': 20995.0}, stop={'price': 20975.0},
                         touch={'enabled': True, 'price': 20995.0, 'valid_until': self.now + 600_000}))
        self.ingest('pine', event('cancel_entry', trade_id=second, ts=self.now))
        self.assertEqual(self.detail(), 'touch_entry_disarmed')
        third = 'ex1:MNQZ2026:%d:long' % (self.now + 1)
        self.alert(entry(trade_id=third, ts=self.now, entry={'type': 'touch', 'price': 20995.0}, stop={'price': 20975.0},
                         touch={'enabled': True, 'price': 20995.0, 'valid_until': self.now + 600_000,
                                'cancel_if_stop_touched': True}))
        self.quote(21030.0)
        self.assertEqual(self.trade['phase'], 'armed')
        self.engine.data['trade']['payload']['stop']['price'] = 21030.0   # gap through the stop before the touch
        self.quote(21029.0)
        self.assertEqual(self.trade['closed_reason'], 'stop_touched_before_entry')
        self.assertEqual(self.bridge.sent, [])

    def test_a_restart_disarms(self):
        self.alert(self.plan())
        self.runtime.close()
        self.open()
        self.assertEqual(self.trade['closed_reason'], 'restart_touch_disarmed')


class Management(V2Base):
    def test_modify_stop_moves_the_resting_stop(self):
        self.enter()
        self.ingest('pine', event('modify_stop', ts=self.now, stop={'price': 20990.0, 'safety_price': 0}))
        self.assertEqual(self.detail(), 'modify_stop_accepted')
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'modify_stop')
        self.assertEqual(self.bridge.sent[-1]['extras']['stop_price'], 20990.0)

    def test_partial_exit_waits_for_no_resting_limits_and_full_exit_flattens(self):
        self.enter(entry(targets=[], runner={'qty': 4}))
        self.ingest('pine', event('exit', ts=self.now, qty=1))
        self.assertEqual(self.detail(), 'exit_accepted')
        self.quote(21001.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'modify_stop', 'exit_market'])
        self.assertEqual(self.book.position, 3)
        self.ingest('pine', event('exit', ts=self.now + 1, qty=3))
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['phase'], 'closed')

    def test_partial_exit_with_resting_limits_is_refused_visibly(self):
        self.enter()
        self.ingest('pine', event('exit', ts=self.now, qty=1))
        self.assertEqual(self.detail(), 'exit_refused_resting_targets_working')

    def test_flatten_and_bias_flip_on_or_off(self):
        self.enter()
        self.ingest('pine', event('bias_flip', ts=self.now))
        self.assertEqual(self.detail(), 'bias_flip_ignored_disabled_by_entry')
        self.ingest('pine', event('flatten', trade_id=TRADE2, ts=self.now))
        self.assertEqual(self.detail(), 'flatten_no_matching_trade')
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])
        self.ingest('pine', event('flatten', ts=self.now + 1))
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'flatten_all')

    def test_bias_flip_enabled_exits(self):
        self.enter(entry(bias_flip={'enabled': True}))
        self.ingest('pine', event('bias_flip', ts=self.now))
        self.assertEqual(self.detail(), 'bias_flip_requested')
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'bias_flip:test')


class FailClosed(V2Base):
    def test_fills_that_do_not_explain_the_position_hold_for_a_person(self):
        self.enter()
        self.book.position += 1                               # a foreign fill
        self.evidence()
        self.assertEqual(self.trade['phase'], 'recovery_required')
        self.quote(21011.0)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'], 'nothing more is sent')

    def test_unknown_outcome_holds_and_release_needs_flat_evidence(self):
        self.enter(fill=False)
        self.book.fill(self.book.entry, 21000.0)
        self.bridge.outcome = ('unknown', {})
        self.evidence()
        self.assertEqual(self.trade['phase'], 'recovery_required')
        with self.assertRaisesRegex(ValueError, 'flat'):
            self.engine.release_recovery(self.now)
        self.book.position = 0
        for row in self.book.rows.values():
            row['status'] = 'cancelled' if row['status'] == 'working' else row['status']
        self.book.rows[self.book.entry + '#stop'].update(status='filled', filled_qty=4)
        self.evidence()
        self.engine.release_recovery(self.now)
        self.assertEqual(self.trade['closed_reason'], 'recovered_by_person')

    def test_a_missing_protective_stop_holds(self):
        self.enter()
        self.book.rows[self.book.entry + '#stop']['status'] = 'cancelled'
        self.evidence()
        self.assertEqual(self.trade['recovery_reason'], 'protective_stop_missing')


if __name__ == '__main__':
    unittest.main()
