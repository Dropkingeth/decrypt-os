"""Lumi RTH Gap on decrypt-bridge-v2 (bridge_v2_lumi.py): a registered v2 strategy on a native MES binding,
through the real runtime and bridge route against the fake bridge (no network). Every one of the
synthetic alerts in tests/fixtures/bridge_v2_lumi/ (arbitrary values that follow the public payload contract
in modules/tradovate-bridge/bridge/PAYLOAD-V2.md) goes through intake and, for entries, the fake-broker
execution path. The plan-*.json files are one entry each, several combining features; management.json holds
one cancel_entry, flatten and modify_stop alert. Touch entries get their own freshness/valid_until tests.
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
from decrypt_trading import bridge_v2_lumi as lumi  # noqa: E402
import bridge_v2_lumi_payloads as payloads  # noqa: E402
from bridge_v2_lumi_payloads import BASE, BAR_CLOSE, CONTRACT, STREAM, TRADE, TRADE_SHORT, entry, touch_entry, event  # noqa: E402
from bridge_v2_lumi_payloads import ARRIVAL_MS, MAX_AGE_MS, TOUCH_WINDOW_MS, trade_ref  # noqa: E402
from decrypt_trading import RuntimeManager, RuntimeError as EngineError  # noqa: E402
from decrypt_trading import bridge_adapters, bridge_v2, bridge_v2_registry, source  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'bridge_v2_lumi'
ADAPTER = lumi.STRATEGY.adapter_id
ACCOUNT = 30000091
ENGINE_SHA = hashlib.sha256(Path(lumi.__file__).read_bytes()).hexdigest()
STRATEGY_SHA = bridge_v2_registry.STRATEGIES[ADAPTER][2]   # the registry's opaque strategy pair id


def load(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def managed(kind):
    """One management alert from management.json."""
    return load('management')[kind]


def exits_off():
    """A long market entry with one resting target, a runner target and every optional exit block off."""
    plan = entry()
    plan['targets'] = [dict(plan['targets'][1], qty=plan['entry']['qty'] - plan['runner']['qty'])]
    return plan


def mes_binding(**over):
    value = native_binding(name='lumi-v2-fixture', pair=ADAPTER, account=ACCOUNT)
    value.update(symbol=CONTRACT, root='MES', tickSize=0.25, streamId=STREAM, **over)
    value['routeSha256'] = contract.route_digest(value)
    return value


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
            'account_id': str(ACCOUNT), 'symbol': CONTRACT, 'trade_id': trade['id'], 'side': trade['side'],
            'position_qty': self.position, 'orders': copy.deepcopy(list(self.rows.values())),
            'open_orders': [r['broker_order_id'] for r in self.rows.values() if r['status'] in ('working', 'partial')]})
        self.test.runtime._bridge_tick()
        self.test.runtime.step()
        row = self.test.engine.db.execute("SELECT status,detail FROM inbox WHERE channel='broker' ORDER BY seq DESC").fetchone()
        self.test.assertEqual(row[0], 'processed', row)
        return row[1]


class V2LumiBase(unittest.TestCase):
    # The runtime "opens" (and its bridge-route first-entry gate measures freshness from) this moment.
    # It must be at or before every ts used anywhere in this file or the fixtures: the gate refuses an
    # entry whose signal time is earlier than when the route started watching.
    start = BASE
    resting = 3

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        patcher.start()
        self.addCleanup(patcher.stop)
        # lumi-gap-bridge-v2 is registered for real at import time: one line each in bridge_adapters.V2_ADAPTERS
        # and bridge_v2_registry.STRATEGIES (which also feeds customer_contract.PAIRS and source.BRIDGE_ENGINES).
        # Nothing is faked here; this is the actual production wiring under test.
        self.assertIn(ADAPTER, bridge_adapters.REGISTRY, 'lumi-gap-bridge-v2 must be in V2_ADAPTERS')
        self.assertIn(ADAPTER, source.BRIDGE_ENGINES, 'lumi-gap-bridge-v2 must be in bridge_v2_registry.STRATEGIES')
        self.assertEqual(contract.PAIRS[ADAPTER], (ENGINE_SHA, STRATEGY_SHA), 'registry pin must match the file on disk')
        self.now, self.price = self.start, 5000.0
        self.root = Path(self.temp.name)
        self.binding = mes_binding()
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
        """One sample of the 1-second MES feed, through the real path: the fake bridge's GET /feed, the route's
        feed reader (bridge_feed.py), the runtime's quote intake and Lumi's engine. The feed is taken to have been
        live just before it (an unbroken run), so these tests are not about warm-up; tests/test_bridge_feed_lumi.py
        covers fresh, stale, gap, wrong contract and restart."""
        f = self.engine.feed
        close = self.now // 1000 * 1000
        if f['last'] is not None and close <= f['last']:
            close = f['last'] + 1000
            self.now = close + 1
        if f['last'] is None or close - f['last'] != 1000:
            f['since'], f['last'] = close - 2000, close - 1000
        self.quotes += 1
        self.price = price
        self.bridge.feed_bars.append({'t': close - 1000, 'close_time': close, 'o': price, 'h': price, 'l': price,
                                      'c': price, 'trades': 1})
        self.transport.feed.refresh()
        self.runtime._bridge_tick()
        self.runtime.step()
        if settle:
            self.settle()

    def bar(self, close_time, close):
        self.now = max(self.now, close_time + 100)
        self.bars += 1
        self.ingest('bar', {'event_id': 'b-%d' % self.bars, 'symbol': CONTRACT, 'open_time': close_time - 5000,
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
        value = value or entry()
        if self.now < value['ts']:    # the alert "arrives" now; never move the clock backward
            self.now = value['ts']
        self.quote(value['entry']['price'], settle=False)
        out = self.alert(value)
        self.gate()
        if fill and self.book.entry:
            self.now += 200
            self.book.fill(self.book.entry, avg or self.price)
            self.evidence()
        return out

    def assert_all_valid(self):
        enabled = {ADAPTER: bridge_adapters.REGISTRY[ADAPTER]}
        for payload in self.bridge.sent:
            bridge_adapters.validate_command(payload, ticker=CONTRACT, enabled=enabled)


class FixtureIntake(V2LumiBase):
    """Every example in tests/fixtures/bridge_v2_lumi/ through intake, matched to the real runtime."""

    def test_builder_matches_the_plain_fixture(self):
        self.assertEqual(entry(side='short'), load('plan-short-market'))
        self.assertEqual(sorted(p.stem for p in FIXTURES.glob('*.json')),
                         ['management', 'plan-long-hit-exits', 'plan-long-touch', 'plan-short-market',
                          'plan-short-three-exits', 'plan-short-trail'])

    def test_market_fixtures_are_accepted_and_valid(self):
        for name in ('plan-short-market', 'plan-long-hit-exits', 'plan-short-trail', 'plan-short-three-exits'):
            with self.subTest(name=name):
                self.setUp()
                payload = load(name)
                self.now = payload['ts']
                out = self.enter(payload, fill=False)
                self.assertEqual(out, 'entry_accepted_not_sent', name)
                self.assertEqual(self.bridge.sent[0]['action'], 'buy' if payload['side'] == 'long' else 'sell')
                self.assert_all_valid()

    def test_touch_fixture_is_accepted_only_when_fresh(self):
        payload = load('plan-long-touch')
        self.now = payload['ts']
        self.quote(payload['entry']['price'], settle=False)
        self.assertEqual(self.alert(payload), 'touch_entry_armed')
        self.assertEqual(self.bridge.sent, [], 'a touch entry does not dispatch until the touch')
        self.now = payload['touch']['valid_until'] + 1
        self.runtime.step()
        self.assertEqual((self.trade['phase'], self.trade['closed_reason']), ('closed', 'touch_expired'))

    def test_cancel_disarms_the_matching_touch_on_either_side(self):
        long_touch = touch_entry(side='long', ts=BASE + ARRIVAL_MS, trade_id=TRADE, valid_until=BASE + TOUCH_WINDOW_MS)
        self.now = long_touch['ts']
        self.quote(long_touch['entry']['price'], settle=False)
        self.assertEqual(self.alert(long_touch), 'touch_entry_armed')
        cancel = managed('cancel_entry')
        self.assertEqual(cancel['trade_id'], TRADE)
        self.now = cancel['ts']
        self.ingest('pine', cancel)
        self.assertEqual(self.detail(), 'touch_entry_disarmed')

        self.setUp()
        short_touch = touch_entry(side='short', ts=BASE + ARRIVAL_MS, trade_id=TRADE_SHORT,
                                  valid_until=BASE + TOUCH_WINDOW_MS)
        self.now = short_touch['ts']
        self.quote(short_touch['entry']['price'], settle=False)
        self.assertEqual(self.alert(short_touch), 'touch_entry_armed')
        cancel_short = event('cancel_entry', trade_id=TRADE_SHORT, side='short', ts=short_touch['ts'] + 1000)
        self.now = cancel_short['ts']
        self.ingest('pine', cancel_short)
        self.assertEqual(self.detail(), 'touch_entry_disarmed')

    def test_flatten_closes_the_matching_open_trade(self):
        self.enter(load('plan-short-market'))
        flatten = managed('flatten')
        self.assertEqual(flatten['trade_id'], TRADE_SHORT)
        flatten['ts'] = self.now + 1   # management events carry no age check; just keep the clock moving forward
        self.ingest('pine', flatten)
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['phase'], 'closed')

    def test_modify_stop_moves_the_resting_stop(self):
        plan = load('plan-long-touch')
        self.enter(plan, fill=False)
        # plan-long-touch is a touch entry: touch it so the trade is open before modify_stop acts on it.
        touch = plan['touch']['price']
        self.now += 1000
        self.quote(touch)
        self.gate()
        self.now += 200
        self.book.fill(self.book.entry, touch)
        self.evidence()
        modify = managed('modify_stop')
        self.assertEqual(modify['trade_id'], TRADE)
        modify['ts'] = self.now + 1   # management events carry no age check; just keep the clock moving forward
        self.ingest('pine', modify)
        self.assertEqual(self.detail(), 'modify_stop_accepted')
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'modify_stop')
        self.assertEqual(self.bridge.sent[-1]['extras']['stop_price'], modify['stop']['price'])
        self.assert_all_valid()


class ProtectiveExits(V2LumiBase):
    def test_stop_to_entry_after_first_target_fill_and_bar_close_uses_the_real_fill(self):
        plan = entry(break_even={'enabled': True, 'trigger_price': 0, 'trigger_target': 1, 'confirm': 'bar_close',
                                 'confirm_timeframe_s': 30, 'offset_ticks': 8})
        self.enter(plan, avg=5000.0)
        self.book.fill(self.book.entry + '#tp1', plan['targets'][0]['price'])
        filled_at = self.now
        self.evidence()
        bar_end = (filled_at // 30_000 + 1) * 30_000
        self.now = bar_end
        self.quote(5005.0)
        be = self.bridge.sent[-1]['extras']
        left = plan['entry']['qty'] - plan['targets'][0]['qty']
        self.assertEqual((be['stop_price'], be['stop_quantity']), (5002.0, left))   # 5000 + 8 ticks (2 pts)

    def test_stop_to_entry_disabled_sends_nothing(self):
        self.enter(exits_off())
        self.quote(5001.0)   # above entry (5000.0) but below the resting target and the runner's target
        self.assertEqual(len(self.bridge.sent), 1, 'with the stop move disabled nothing extra is sent')

    def test_session_close_exit_only_when_enabled(self):
        self.enter(entry(eod={'enabled': True, 'time': '15:00'}))
        eod = bridge_v2.next_clock(BASE, '15:00', 'America/New_York')
        self.now = eod
        self.runtime.step()
        self.settle()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'eod')

        self.setUp()
        self.enter(exits_off())
        self.now += 86_400_000          # a full day later
        self.runtime.step()
        self.assertNotEqual(self.kinds()[-1] if self.kinds() else None, 'flatten_all')


class DayLossLimit(V2LumiBase):
    def lose(self, plan):
        self.enter(plan)
        self.book.fill(self.book.entry + '#stop', plan['stop']['price'])
        self.evidence()
        self.assertEqual(self.trade['phase'], 'closed')

    def test_closed_loss_past_the_limit_refuses_a_second_entry(self):
        # The builder's own stop (8 pts x 6 MES + fees = $242.40) is past its $110 limit: one stopped-out
        # trade locks the day.
        plan = entry(daily_loss={'enabled': True})
        self.assertEqual(plan['daily_loss']['limit_usd'], 110.0)
        self.lose(plan)
        second = entry(trade_id=trade_ref(2), ts=self.now + 60_000,
                       daily_loss={'enabled': True, 'limit_usd': 110.0})
        self.now += 60_000
        self.quote(5000.0, settle=False)
        self.assertEqual(self.alert(second), 'refused:daily_loss_locked')

    def test_open_loss_past_the_limit_closes_the_trade_when_flatten_is_on(self):
        plan = load('plan-long-hit-exits')   # long, entry 5000.0, stop 4992.0, limit $110, open loss counted
        self.assertTrue(plan['daily_loss']['include_open'] and plan['daily_loss']['flatten'])
        self.enter(plan)
        self.quote(4995.5)   # long: price down 4.5 pts x 6 MES + fees = $137.40 open loss, past the $110 limit
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'daily_loss')

    def test_limit_disabled_lets_a_second_entry_through(self):
        self.lose(entry(daily_loss={'enabled': False, 'limit_usd': 110.0}))
        second = entry(trade_id=trade_ref(2), ts=self.now + 60_000)
        self.now += 60_000
        self.quote(5000.0, settle=False)
        self.assertEqual(self.alert(second), 'entry_accepted_not_sent')


class RemainderAndTargets(V2LumiBase):
    def test_trailing_remainder_stays_open_at_the_first_target(self):
        plan = load('plan-short-trail')
        self.assertEqual(plan['runner']['mode'], 'trail')
        self.enter(plan)
        self.quote(plan['targets'][0]['price'])     # TP1 fill via limit, not quote; just check runner stays
        self.assertEqual(self.trade['phase'], 'running')

    def test_three_resting_targets_with_no_remainder(self):
        plan = load('plan-short-three-exits')
        self.assertEqual((plan['runner']['mode'], plan['runner']['qty']), ('hold', 0))
        self.assertEqual(len(plan['targets']), 3, 'every contract exits through one of three resting take-profit orders')
        self.enter(plan, fill=False)
        self.assertEqual(self.bridge.sent[0]['extras']['tp1_price'], plan['targets'][0]['price'])

    def test_price_hit_targets_exit_with_market_orders(self):
        plan = load('plan-long-hit-exits')
        self.assertEqual({t['type'] for t in plan['targets']}, {'market'})
        self.enter(plan)
        self.quote(plan['targets'][0]['price'])
        self.assertIn('exit_market', self.kinds())


class FailClosed(V2LumiBase):
    def test_unknown_field_and_stale_entry_are_refused(self):
        bad = entry(trade_id=trade_ref(3))
        bad['not_a_field'] = 1
        with self.assertRaisesRegex(ValueError, 'unknown field'):
            self.engine.validate_pine(bad, self.now)
        stale = entry(ts=BASE)
        self.now = BASE + MAX_AGE_MS + 1          # more than max_age_ms after ts
        self.quote(5000.0, settle=False)
        self.assertEqual(self.alert(stale), 'input_rejected')
        with self.assertRaisesRegex(ValueError, 'old'):
            self.engine.validate_pine(stale, self.now)


if __name__ == '__main__':
    unittest.main()
