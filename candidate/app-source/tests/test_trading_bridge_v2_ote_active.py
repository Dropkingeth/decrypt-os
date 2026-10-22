"""decrypt-bridge-v2 receiver/executor for OTE Active.

The alerts in bridge_v2_ote_active_fixtures.json are synthetic: arbitrary prices, sizes and times chosen
only to exercise the payload contract (PAYLOAD-V2.md) and the executor's code paths. Every contract
block is covered on and off: the stop (bar-close vs resting + a safety stop), each target (resting limit
vs market price-hit, including market TP1 followed by market TP2 since a limit cannot follow a market
target), the runner (hold, target, trail), break-even (TP1 fill vs the price level, a resting vs a
watched bar-close stop, flatten vs skip when already through), end of day (default and custom time),
the daily loss lock (on, disabled, and flatten-the-open-trade), the bias-flip event and an exit event
with an explicit qty.

`ote-active-bridge-v2` is a real registered line in bridge_v2_registry.STRATEGIES and
bridge_adapters.V2_ADAPTERS (not a test-only strategy), so it is already wired into
bridge_adapters.REGISTRY, source.BRIDGE_ENGINES and customer_contract.PAIRS at import time.
"""
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_fixture import FakeBridge, native_binding  # noqa: E402
from decrypt_trading import RuntimeManager  # noqa: E402
from decrypt_trading import bridge_adapters, bridge_v2_ote_active as ota, source  # noqa: E402
from decrypt_trading import bridge_v2_contract as v2  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402

FIXTURES = json.loads((Path(__file__).resolve().parent / 'bridge_v2_ote_active_fixtures.json').read_text())
ALERTS = {name: json.loads(row['raw']) for name, row in FIXTURES['alerts'].items()}
NOW_MS = FIXTURES['now_ms']
ADAPTER = ota.STRATEGY.adapter_id
STREAM = ALERTS['short_core']['stream_id']
ACCOUNT = 30000055


def alert(name):
    """A deep copy of the named synthetic example alert (parsed JSON)."""
    return json.loads(FIXTURES['alerts'][name]['raw'])


class Contract(unittest.TestCase):
    """Intake: every example is accepted, and the declared extras schema fails closed."""

    def test_registered_for_real_not_just_in_the_registry_table(self):
        self.assertIn(ADAPTER, bridge_adapters.REGISTRY)
        self.assertIn(ADAPTER, source.BRIDGE_ENGINES)
        self.assertIn(ADAPTER, contract.PAIRS)
        self.assertIs(source.load_pair_sources(ADAPTER)['core'], ota)

    def test_every_example_entry_and_event_is_accepted(self):
        for name, payload in ALERTS.items():
            with self.subTest(name=name):
                now = payload['ts'] + 1500
                v2.validate(payload, strategy=ota.STRATEGY, now_ms=now)

    def test_opaque_entry_extras_are_accepted_within_bounds(self):
        ok = alert('short_core')
        ok['extras'] = {'profile': 'fx-p1', 'aux_s': 'fx-q 1.5', 'aux_f': -0.5, 'aux_t': False, 'aux_e': '',
                        **{'aux_%d' % i: i for i in range(4)}}           # 8 opaque keys: the declared bound
        v2.validate(ok, strategy=ota.STRATEGY, now_ms=ok['ts'] + 1500)
        self.assertIsNone(ota.STRATEGY.check, 'opaque extras feed no format check or trading rule')

    def test_opaque_entry_extras_out_of_bounds_are_refused(self):
        cases = {
            'too_many': {'aux_%d' % i: i for i in range(9)},
            'bad_key': {'Aux-1': 1},
            'long_key': {'a' * 41: 1},
            'nested': {'aux_a': {'x': 1}},
            'list': {'aux_a': [1]},
            'null': {'aux_a': None},
            'long_str': {'aux_a': 'x' * 65},
            'odd_str': {'aux_a': '<x>'},
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                bad = alert('short_core')
                bad['extras'] = dict({'profile': 'fx-p1'}, **extra)
                with self.assertRaises(ValueError):
                    v2.validate(bad, strategy=ota.STRATEGY, now_ms=bad['ts'] + 1500)

    def test_unknown_extras_field_on_an_event_is_refused(self):
        bad = alert('flip_short')
        bad['extras']['unexpected_field'] = 1
        with self.assertRaises(ValueError):
            v2.validate(bad, strategy=ota.STRATEGY, now_ms=bad['ts'] + 1500)

    def test_bad_profile_is_refused(self):
        for profile in ('<bad>', ' leading space', 'P' * 41, '', 7):
            with self.subTest(profile=profile):
                bad = alert('short_core')
                bad['extras']['profile'] = profile
                with self.assertRaises(ValueError):
                    v2.validate(bad, strategy=ota.STRATEGY, now_ms=bad['ts'] + 1500)
        good = alert('short_core')
        good['extras']['profile'] = 'P' * 40
        v2.validate(good, strategy=ota.STRATEGY, now_ms=good['ts'] + 1500)

    def test_profile_is_required(self):
        bad = alert('short_core')
        del bad['extras']['profile']
        with self.assertRaises(ValueError):
            v2.validate(bad, strategy=ota.STRATEGY, now_ms=bad['ts'] + 1500)

    def test_continuous_symbol_without_a_typed_contract_is_refused(self):
        bad = alert('short_core')
        bad['symbol'] = 'MNQ1!'
        with self.assertRaises(ValueError):
            v2.validate(bad, strategy=ota.STRATEGY, now_ms=bad['ts'] + 1500)

    def test_stale_entry_is_refused(self):
        payload = alert('short_core')
        with self.assertRaises(ValueError):
            v2.validate(payload, strategy=ota.STRATEGY, now_ms=payload['ts'] + 46_000)   # max_age_ms 45000


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
            'position_qty': self.position, 'orders': [dict(r) for r in self.rows.values()],
            'open_orders': [r['broker_order_id'] for r in self.rows.values() if r['status'] in ('working', 'partial')]})
        self.test.runtime._bridge_tick()
        self.test.runtime.step()
        row = self.test.engine.db.execute("SELECT status,detail FROM inbox WHERE channel='broker' ORDER BY seq DESC").fetchone()
        self.test.assertEqual(row[0], 'processed', row)
        return row[1]


class V2OteActiveBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        no_network = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        no_network.start()
        self.addCleanup(no_network.stop)
        self.now, self.price = NOW_MS, 21000.0
        self.root = Path(self.temp.name)
        self.binding = native_binding(name='ote-active-fixture', pair=ADAPTER, account=ACCOUNT)
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

    def send(self, payload):
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', payload)
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

    def enter(self, name, fill=True, avg=None):
        payload = alert(name)
        self.now = payload['ts']
        self.quote(payload['entry']['price'], settle=False)
        out = self.send(payload)
        self.transport.route.refresh()
        self.runtime.step()
        self.settle()
        if fill and self.book.entry:
            self.now += 200
            self.book.fill(self.book.entry, avg or self.price)
            self.evidence()
        return out

    def assert_all_valid(self):
        enabled = {ADAPTER: bridge_adapters.REGISTRY[ADAPTER]}
        for payload in self.bridge.sent:
            bridge_adapters.validate_command(payload, ticker='MNQZ2026', enabled=enabled)

    def tp1_cmd(self):
        """The command_id of the resting TP1 limit. A plain bar-close-stop entry places it as its own command
        after the fill (no fixed suffix); a bracket entry carries it, so the broker rests it as '<entry>#tp1'."""
        return next((p['extras']['command_id'] for p in self.bridge.sent
                     if p.get('orderType') == 'limit' and p['action'] == 'exit'), self.book.entry + '#tp1')


class CoreFixture(V2OteActiveBase):
    """The `short_core` and `long_watch` fixture alerts through the executor."""

    def test_entry_rests_the_safety_stop_with_a_resting_tp1(self):
        self.assertEqual(self.enter('short_core', fill=True), 'entry_accepted_not_sent')
        self.assertEqual(self.kinds(), ['bracket_entry'], 'TP1 rests with the bracket')
        extras = self.bridge.sent[0]['extras']
        self.assertEqual(extras['stop_price'], 21020, 'bar-close stop: only the safety stop rests')
        self.assertEqual((extras['tp1_price'], extras['tp1_quantity']), (20990, 3))
        self.assertEqual(self.trade['close_stop'], {'price': 21012, 'tf': 240})
        self.assertIn(self.tp1_cmd(), self.book.rows)
        self.assert_all_valid()

    def test_break_even_on_tp1_fill_moves_the_resting_stop(self):
        self.enter('short_core', fill=True)
        self.book.fill(self.tp1_cmd(), 20990)
        self.quote(20990, settle=False)                       # price has reached TP1: not already past the BE level
        self.evidence()
        self.assertEqual(self.trade['resting_stop'], 20998.0)
        self.assertEqual(self.trade['close_stop'], {'price': 21012, 'tf': 240}, 'the watched level is not the BE stop')
        self.assertEqual(self.book.rows[self.book.entry + '#stop']['stop_price'], 20998.0)
        self.assert_all_valid()

    def test_plain_bar_close_stop_entry_with_a_resting_tp1(self):
        self.enter('long_watch', fill=True)
        self.assertEqual(self.kinds(), ['entry', 'tp1_limit'])
        self.assertNotIn('stop_price', self.bridge.sent[0]['extras'], 'bar-close stop: no resting broker stop')
        self.assertEqual((self.bridge.sent[1]['limitPrice'], self.bridge.sent[1]['quantity']), (21010, 4))
        self.assert_all_valid()

    def test_break_even_on_tp1_fill_moves_the_watched_bar_close_level(self):
        self.enter('long_watch', fill=True)
        self.book.fill(self.tp1_cmd(), 21010)
        self.quote(21010, settle=False)                       # price has reached TP1: not already past the BE level
        self.evidence()
        self.assertEqual(self.trade['close_stop'], {'price': 21001.75, 'tf': 120})

    def eod_flatten(self, name):
        from decrypt_trading import bridge_v2
        payload = alert(name)
        self.enter(name, fill=True)
        eod = bridge_v2.next_clock(payload['ts'], payload['eod']['time'], payload['eod']['tz'])
        self.assertGreater(eod, self.now)
        self.now = eod
        self.runtime.step()
        self.settle()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'eod')

    def test_end_of_day_flattens_at_the_alert_time(self):
        self.eod_flatten('short_core')

    def test_custom_end_of_day_time(self):
        self.eod_flatten('long_watch')


class TargetsAndRunner(V2OteActiveBase):
    def test_tp1_market_with_tp2_market(self):
        self.enter('short_market_targets', fill=True)
        self.assertEqual(self.kinds(), ['bracket_entry'] if 'stop_price' in self.bridge.sent[0]['extras'] else ['entry'],
                         'no resting limit: nothing but the entry (and its bar-close stop) rests')
        self.quote(20990)
        self.assertIn('exit_market', self.kinds())
        self.assert_all_valid()

    def test_runner_target_flattens(self):
        self.enter('short_core', fill=True)
        self.book.fill(self.tp1_cmd(), 20990)
        self.quote(20990, settle=False)                       # price has reached TP1: BE applies, does not flatten
        self.evidence()
        self.quote(20970.25)
        self.assertNotEqual(self.trade['phase'], 'closed')
        self.quote(20970)
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'runner_target')

    def test_resting_stop_with_two_resting_targets_and_trailing_runner(self):
        self.enter('short_trail', fill=True)
        self.assertEqual(self.kinds(), ['bracket_entry', 'tp1_limit'])
        self.assertEqual(self.bridge.sent[0]['extras']['stop_price'], 21010)
        self.assertEqual(self.trade['payload']['runner']['mode'], 'trail')
        self.assertEqual((self.trade['payload']['runner']['trail_activation_ticks'],
                          self.trade['payload']['runner']['trail_distance_ticks']), (64, 14))
        self.assert_all_valid()

    def test_trailing_runner_exits_after_activation(self):
        plan = alert('short_trail')
        plan['targets'] = [{'price': 20990, 'qty': 3, 'type': 'market'}]   # one market target, then only the runner left
        plan['runner'] = {'qty': 4, 'mode': 'trail', 'price': 0, 'trail_activation_ticks': 24, 'trail_distance_ticks': 12}
        plan['break_even']['trigger_target'] = 1
        self.now = plan['ts']
        self.quote(plan['entry']['price'], settle=False)
        self.send(plan)
        self.transport.route.refresh()
        self.runtime.step()
        self.settle()
        self.now += 200
        self.book.fill(self.book.entry, self.price)
        self.evidence()
        self.quote(20989.5)                                   # target: shrink + exit 3, runner (4) remains
        self.assertIn('exit_market', self.kinds())
        self.quote(20984.0)                                   # past the 24-tick activation: trail low 20984
        self.quote(20986.75)
        self.assertNotEqual(self.kinds()[-1], 'flatten_all')
        self.quote(20987.0)                                   # 12 ticks back from 20984: trail exits
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'runner_trail')

    def test_switches_off_runner_holds_to_stop_or_eod_only(self):
        self.enter('short_exits_off', fill=True)
        self.assertFalse(self.trade['payload']['break_even']['enabled'])
        self.assertFalse(self.trade['payload']['eod']['enabled'])
        self.assertFalse(self.trade['payload']['bias_flip']['enabled'])
        self.quote(20900)                                      # far through both targets
        self.assertEqual(self.kinds(), ['entry', 'tp1_limit'], 'no runner target sent: runner holds')


class DailyLoss(V2OteActiveBase):
    def test_core_fixture_sends_the_lock_disabled(self):
        payload = alert('short_core')
        self.assertFalse(payload['daily_loss']['enabled'])

    def test_lock_block_disabled(self):
        payload = alert('long_watch')
        self.assertIn('limit_usd', payload['daily_loss'])
        self.assertFalse(payload['daily_loss']['enabled'], 'the daily_loss block is sent disabled for this alert')

    def test_daily_loss_lock_refuses_the_next_entry_same_day(self):
        # The fixture's own $80 limit: one 6-contract bar-close stop-out (12 pts x 6 x $2 + fees, about $148)
        # reaches it. This checks the lock wiring (fixtures are synthetic).
        first = alert('short_lock')
        self.assertEqual(first['daily_loss']['limit_usd'], 80.0)
        self.now = first['ts']
        self.quote(first['entry']['price'], settle=False)
        self.send(first)
        self.transport.route.refresh()
        self.runtime.step()
        self.settle()
        self.now += 200
        self.book.fill(self.book.entry, self.price)
        self.evidence()
        size = first['stop']['timeframe_s'] * 1000
        boundary = (self.now // size + 1) * size               # the bar-close stop (21010) is watched, not resting
        self.price = 21012.0                                   # the harness's realized-fill price for the flatten
        self.bar(boundary - 5000, 21014.0)                     # beyond, but not the bar close
        self.assertNotEqual(self.trade['phase'], 'closed')
        self.bar(boundary, 21012.0)
        self.assertEqual(self.trade['phase'], 'closed')
        second = alert('short_lock')
        second['trade_id'] = 'fxa.lock.10:short'
        second['alert_id'] = second['trade_id'] + '@open'
        second['ts'] = self.now + 60_000
        self.now = second['ts']
        self.quote(second['entry']['price'], settle=False)
        self.assertEqual(self.send(second), 'refused:daily_loss_locked')

    def test_lock_flatten_closes_the_open_trade_on_the_limit(self):
        self.enter('long_lock_flatten', fill=True)
        self.quote(20997)                                     # 3 pts x 7 x $2 + fees: short of the $80 limit
        self.assertEqual(self.kinds(), ['entry', 'tp1_limit'])
        self.quote(20994)                                     # 6 pts x 7 x $2 + fees: past the $80 limit
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'daily_loss')


class Events(V2OteActiveBase):
    def test_full_bias_flip_exits_everything(self):
        self.enter('short_core', fill=True)
        flip = alert('flip_short')
        flip['trade_id'] = self.trade['id']
        flip['alert_id'] = flip['trade_id'] + '@bias-flip.9'
        flip['ts'] = self.now + 1000
        self.ingest('pine', flip)
        self.settle()
        self.evidence()
        self.assertEqual(self.kinds()[-1], 'flatten_all')
        self.assertEqual(self.trade['closed_reason'], 'bias_flip:bias_flip')

    def test_exit_event_closes_its_qty_only(self):
        self.enter('long_watch', fill=True)
        # The resting limit (4) fills first: the bridge refuses a market exit qty while a limit rests.
        self.book.fill(self.tp1_cmd(), 21010.0)
        self.quote(21010.0, settle=False)                     # price has reached the target: BE applies, does not flatten
        self.evidence()
        exit_payload = alert('exit_long_qty')                  # qty 1 of the 5 still open
        exit_payload['trade_id'] = self.trade['id']
        exit_payload['alert_id'] = exit_payload['trade_id'] + '@exit.9'
        exit_payload['ts'] = self.now + 1000
        self.ingest('pine', exit_payload)
        self.assertEqual(self.detail(), 'exit_accepted')
        self.settle()
        self.evidence()
        self.assertEqual(self.book.position, 4)
        self.assertNotEqual(self.trade['phase'], 'closed', 'contracts remain open after an exit of 1')


if __name__ == '__main__':
    unittest.main()
