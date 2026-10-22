"""Native exits through the bridge: the engine from the optional engine package decides, this
bridge executes against the fake exchange in headless Chrome, and every broker snapshot is
applied through the engine's own core.broker() validation.

Covers: bracket entry + resting stop + TP1, stop before TP1 -> exit-all, TP1 fill -> immediate
break-even move, BE stop -> exit-all, parent sweep (stop shrunk first), CBDR bar close, EOD,
placeoso rejected / brackets disabled -> synthetic fallback, stop modify rejected -> flatten,
liquidateposition leaving orders working, partial entry fill, restarts, reversal guard.
The optional engine package is not bundled: point TVBRIDGE_SOUP_NATIVE_PACKAGE at it, otherwise
this module is skipped.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import logging

from harness import Harness, seed_market_price
from soup_inputs import named  # synthetic Soup input names -> the package's own

SOUP = Path(os.environ.get('TVBRIDGE_SOUP_NATIVE_PACKAGE',
                           ''))
if not (SOUP / 'engine' / 'core.py').is_file() or 'validate_native_exits' not in (SOUP / 'engine/core.py').read_text():
    raise unittest.SkipTest('optional engine package (native exits) not found; set TVBRIDGE_SOUP_NATIVE_PACKAGE')
# Other suites load a different build of the optional engine package under the same module
# names. Import this package's modules in isolation and restore the table.
_SOUP_MODULES = ('core', 'transport', 'storage', 'runtime', 'test_pair', 'mechanical_payload_test',
                 'verify_payload_contract')
_saved = {n: sys.modules.pop(n) for n in _SOUP_MODULES if n in sys.modules}
_paths = [str(SOUP / sub) for sub in ('engine', 'tests', 'tests/support')]
sys.path[:0] = _paths
try:
    import test_pair as soup  # noqa: E402
    import core  # noqa: E402
    from core import Engine, Rejected, NATIVE_ID  # noqa: E402
    from transport import message  # noqa: E402
finally:
    for _n in _SOUP_MODULES:
        sys.modules.pop(_n, None)
    for _p in _paths:
        sys.path.remove(_p)
    sys.modules.update(_saved)

from tvbridge.bridge import Bridge  # noqa: E402
from tvbridge.executor import Executor, leg_command_id  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402
from tvbridge.protocol import tradovate_symbol  # noqa: E402

logging.getLogger('tvbridge').addHandler(logging.NullHandler())
CONFIG = soup.CONFIG  # the package's dated MNQ contract
SYMBOL = tradovate_symbol(CONFIG['ticker'])  # its Tradovate name
ACCOUNT, CONTRACT = 101, 556
# Synthetic settings for this suite: long 4 = TP1 2 + parent runner 1 + EOD runner 1, stop 22,
# TP1 31 with its working limit 2.5 inside, break-even +7. Prices are rebased to BASE.
BASE = 10000.0
FIXTURE = {'initial_hard_stop_handles': 22, 'tp1_handles': 31, 'be_offset_handles': 7, 'fixture_int_07': 2,
           'fixture_int_06': 1, 'fixture_int_01': 0, 'fixture_int_04': 1}
TP1_OFFSET = 2.5
PRICE_FIELDS = ('entry_price', 'tp1_price', 'break_even_price', 'runner_cbdr_target', 'initial_hard_stop_price',
                'catastrophe_stop_price', 'effective_pre_tp1_stop_price')
POLICY = {'id': 'account-original-allocation-micro-cap-v1', 'account_id': CONFIG['account_id'], 'max_contracts': 10,
          'exit_allocation': 'original',
          'native_binding': {'broker_id': 'b', 'account_value': 'v', 'account_label': 'l', 'account_url_sha256': '0' * 64}}
NATIVE = {'id': NATIVE_ID, 'account_id': CONFIG['account_id'], 'modify_confirm_ms': 2500, 'cancel_confirm_ms': 5000, 'stop_confirm_ms': 5000,
          'backstop_ms': 1500, 'exit_verify_ms': 4000, 'reduce_confirm_ms': 5000, 'max_exit_attempts': 3}
RESULTS = {}


def now_ms():
    return time.time_ns() // 1_000_000


def entry_payload(side, changes=None, at=None):
    """The engine fixture's entry with this suite's settings; every price moved onto BASE."""
    p = soup.fixture(side, named(soup, dict(FIXTURE, **(changes or {}))), **({} if at is None else {'at': at}))[0]
    shift = BASE - p['entry_price']
    for k in PRICE_FIELDS:
        if p[k]:
            p[k] += shift
    return p


def tp1_limit(p):
    """The TP1 working limit: TP1_OFFSET inside the strategy target."""
    return p['tp1_price'] - (TP1_OFFSET if p['side'] == 'long' else -TP1_OFFSET)


class NativeLoop(unittest.TestCase):
    CFG = {}
    AUTO_PROTECT = True  # emulate the daemon publisher's housekeeping in the loop

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.fake = self.h.fake
        self.fake.contracts[CONTRACT] = {'id': CONTRACT, 'name': SYMBOL}
        self.fake.set_price(BASE)
        self.cfg = self.h.config(**dict({'ticker': CONFIG['ticker'], 'soup_account_id': CONFIG['account_id'],
                                         'bracket_orders_enabled': True, 'flatten_all_enabled': True}, **self.CFG))
        self.state = AccountState()
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port, page_url_contains='/trader/',
                             trade_url_pattern=self.cfg['trade_url_pattern'])
        self.bridge.attach(reload=True)
        self.addCleanup(self.bridge.detach)
        self.bridge.wait_ready(timeout=20)
        self.ex = Executor(self.bridge, self.state, Journal(self.cfg['journal_dir']), self.cfg)
        self.ex.resolve()
        seed_market_price(self.ex, BASE)  # bracket entries need a known market price
        self.db = self.h.tmp / 'soup.sqlite'
        self.engine = self.open_engine()
        self.results, self.sent = [], []
        self.last_quote = 0

    def open_engine(self, native=NATIVE):
        e = Engine(self.db, CONFIG, tp1_limit_offset_handles=TP1_OFFSET, quantity_policy=POLICY, native_exits=native)
        self.addCleanup(e.close)
        return e

    # --- loop -------------------------------------------------------------------------------

    def trade(self):
        return self.engine.data['trade']

    def cmds(self, kind):
        return [c for c in self.engine.data['commands'].values() if c['kind'] == kind]

    def drain(self):
        while self.engine.ready():
            c = self.engine.start(self.engine.ready()[0]['id'], now_ms())
            if c is None:
                return
            payload = message(c, CONFIG['ticker'], self.trade()['side'], quantity_limit=10)
            outcome, receipt = self.ex.send(payload)
            self.sent.append((c['kind'], payload.get('action'), outcome, receipt))
            self.engine.transport_result(c['id'], outcome, receipt, now_ms())

    def evidence(self):
        snap, reason = self.ex.snapshot(self.trade()['id'])
        if snap:
            try:
                self.results.append(self.engine.apply('broker', snap, now_ms()))
            except Rejected as e:  # a snapshot between a dispatch and its echo; the next one reconciles
                self.results.append('rejected:' + str(e))
        return snap, reason

    def step(self):
        self.drain()
        if self.AUTO_PROTECT:
            with self.ex.lock:
                self.ex.maintain_brackets()
        self.evidence()
        self.engine.tick(now_ms())
        self.drain()

    def until(self, pred, timeout=20.0, what='condition'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.step()
            if pred():
                return True
            time.sleep(0.05)
        self.fail('%s not reached; trade=%s sent=%s results=%s' % (
            what, {k: self.trade().get(k) for k in ('phase', 'terminal_reason', 'remaining', 'native')},
            [(k, a, o) for k, a, o, _ in self.sent], self.results[-5:]))

    def quote(self, price):
        at = max(now_ms(), self.last_quote + 1)
        self.last_quote = at
        return self.engine.apply('quote', {'event_id': 'q%d' % at, 'symbol': CONFIG['ticker'], 'time': at,
                                           'price': price}, at)

    # --- scenario helpers ----------------------------------------------------------------------

    def net(self):
        return self.state.net_position(ACCOUNT, CONTRACT)

    def working(self):
        return self.state.open_orders(ACCOUNT, CONTRACT)

    def path(self, since=0):
        return [n for a, c, n in self.fake.position_history[since:] if (a, c) == (ACCOUNT, CONTRACT)]

    def requests_since(self, mark):
        return [e for e, _ in self.fake.bodies[mark:]]

    def enter(self):
        at = now_ms() // 1000 * 1000
        self.p = entry_payload('long', {'chart_seconds': 1, 'time': at - 1000}, at=at)
        self.assertEqual(self.engine.apply('pine', self.p, now_ms(), entries_enabled=True), 'entry_accepted')
        return self.p

    def running(self):
        p = self.enter()
        self.until(lambda: self.net() == 4 and len(self.working()) == 2 and
                   (self.trade().get('native') or {}).get('stop_confirmed'), what='bracket running')
        return p

    def stop_id(self):
        return int(self.trade()['broker_orders'][self.trade()['native']['stop_id']]['broker_order_id'])

    def at_break_even(self):
        p = self.running()
        mark = len(self.fake.bodies)
        self.fake.set_price(tp1_limit(p))  # the TP1 working limit
        tp1_fill = self.fake.fill_times[max(self.fake.fill_times)]
        self.until(lambda: self.fake.versions[self.stop_id()].get('stopPrice') == p['break_even_price'] and
                   self.fake.versions[self.stop_id()]['orderQty'] == 2 and
                   any(c['status'] == 'confirmed' for c in self.cmds('modify_stop')), what='break-even stop')
        return p, mark, tp1_fill

    def eod(self):
        """Soup's EOD fallback looks at the last completed bar, which must open after the entry."""
        decision = self.trade()['payload']['execution_context']['decision_time']
        while now_ms() // 1000 * 1000 - 1000 < decision:
            time.sleep(0.05)
        with patch.object(core, 'in_session', return_value=True):
            self.engine.tick(now_ms())
        self.assertEqual(self.trade()['terminal_reason'], 'eod')

    def closed(self, reason=None):
        self.until(lambda: self.trade()['phase'] == 'closed', what='trade closed')
        self.assertEqual((self.net(), self.working(), self.fake.working_orders(CONTRACT)), (0, [], []))
        if reason:
            self.assertEqual(self.trade()['terminal_reason'], reason)
        self.assertTrue(all(n >= 0 for n in self.path()), self.path())  # never reversed through flat
        self.assertTrue(all(0 < b.get('orderQty', 1) <= 10 for _, b in self.fake.bodies))


class RestingStop(NativeLoop):
    def test_entry_places_bracket_stop_and_tp1_rests(self):
        mark = len(self.fake.bodies)
        p = self.running()
        (e1, oso), (e2, tp1) = self.fake.bodies[mark:mark + 2]
        self.assertEqual((e1, oso['action'], oso['orderType'], oso['orderQty']), ('order/placeoso', 'Buy', 'Market', 4))
        self.assertEqual({k: oso['bracket1'][k] for k in ('action', 'orderType', 'stopPrice')},
                         {'action': 'Sell', 'orderType': 'Stop', 'stopPrice': p['initial_hard_stop_price']})
        self.assertNotIn('bracket2', oso)
        self.assertEqual((e2, tp1['orderType'], tp1['orderQty'], tp1['price']),
                         ('order/placeorder', 'Limit', 2, tp1_limit(p)))
        self.assertEqual(self.fake.versions[self.stop_id()]['orderQty'], 4)
        snap, _ = self.ex.snapshot(self.trade()['id'])
        stop = next(o for o in snap['orders'] if o.get('role') == 'stop')
        self.assertEqual((stop['command_id'], stop['quantity'], stop['stop_price'], stop['status']),
                         (self.trade()['native']['stop_id'], 4, p['initial_hard_stop_price'], 'working'))
        self.assertEqual([k for k, _, o, _ in self.sent], ['entry', 'tp1_limit'])

    def test_stop_hit_before_tp1_exit_all_verified_flat(self):
        p = self.running()
        self.fake.set_price(p['initial_hard_stop_price'])
        self.closed('broker_stop_initial')
        self.assertEqual(self.fake.orders[self.stop_id()]['ordStatus'], 'Filled')

    def test_stop_lost_outside_soup_falls_back_to_synthetic_exits(self):
        p = self.running()
        with self.fake.lock:  # someone cancels the resting stop at the broker
            self.fake._cancel(self.stop_id())
        self.until(lambda: self.trade()['native']['mode'] == 'synthetic', what='fallback after a lost stop')
        self.quote(p['initial_hard_stop_price'] - 1)
        self.closed('initial_hard_stop')

    def test_backstop_flattens_when_price_trades_through_but_the_stop_has_not_filled(self):
        p = self.running()
        started = time.monotonic()
        self.quote(p['initial_hard_stop_price'] - 1)  # Soup's feed sees it; the exchange stop has not fired
        self.assertEqual(self.trade()['native']['backstop']['reason'], 'initial_hard_stop')
        self.closed('backstop_initial_hard_stop')
        self.assertGreaterEqual(time.monotonic() - started, NATIVE['backstop_ms'] / 1000 - 0.1)
        self.assertIn('order/liquidateposition', [e for e, _ in self.fake.bodies])


class BreakEven(NativeLoop):
    def test_tp1_fill_moves_stop_to_break_even_immediately(self):
        p, mark, tp1_fill = self.at_break_even()
        i = next(i for i, (e, b) in enumerate(self.fake.bodies) if e == 'order/modifyorder' and
                 b['stopPrice'] == p['break_even_price'])
        latency = self.fake.body_times[i] - tp1_fill
        RESULTS['tp1_fill_to_be_modify_s'] = round(latency, 3)
        self.assertLess(latency, 1.5)
        self.assertEqual(self.fake.bodies[i][1]['orderId'], self.stop_id())
        self.assertEqual(self.trade()['remaining'], 2)

    def test_break_even_stop_hit_exit_all(self):
        p, _, _ = self.at_break_even()
        self.fake.set_price(p['break_even_price'])
        self.closed('broker_stop_break_even')
        self.assertEqual(self.path()[-3:], [4, 2, 0])


class RunnerExits(NativeLoop):
    def wait_runner_window(self):
        self.until(lambda: now_ms() >= self.trade()['runner_not_before'] + 1000, what='runner window')

    def test_parent_sweep_resizes_stop_before_the_market_reduce(self):
        p, _, _ = self.at_break_even()
        self.wait_runner_window()
        mark = len(self.fake.bodies)
        at = now_ms() // 1000 * 1000
        self.assertEqual(self.engine.apply('pine', soup.facts(p, [{'kind': 'parent_sweep', 'close_side': 'long'}], at,
                                                                      price=BASE + 1),
                                           now_ms()), 'facts_processed')
        self.until(lambda: self.net() == 1 and self.trade()['reductions'] == {}, what='parent runner out')
        self.assertEqual(self.requests_since(mark), ['order/modifyorder', 'order/placeorder'])
        mod, red = self.fake.bodies[mark][1], self.fake.bodies[mark + 1][1]
        self.assertEqual((mod['orderQty'], mod['stopPrice']), (1, p['break_even_price']))
        self.assertEqual((red['orderType'], red['orderQty'], red['action']), ('Market', 1, 'Sell'))
        self.assertEqual(self.fake.versions[self.stop_id()]['orderQty'], 1)
        self.assertTrue(all(n >= 0 for n in self.path()))

    def test_cbdr_bar_close_exit_of_the_eod_runner(self):
        p, _, _ = self.at_break_even()
        self.wait_runner_window()
        mark = len(self.fake.bodies)
        b = now_ms() // 1000 * 1000
        t = p['runner_cbdr_target']
        self.assertEqual(self.engine.apply('bar', {'event_id': 'bar%d' % b, 'symbol': CONFIG['ticker'],
                                                   'open_time': b - 1000, 'close_time': b, 'open': t - 2,
                                                   'high': t + 1, 'low': t - 3, 'close': t}, now_ms()), 'bar_processed')
        self.until(lambda: self.net() == 1, what='EOD runner out')
        self.assertEqual(self.requests_since(mark), ['order/modifyorder', 'order/placeorder'])
        self.assertEqual(self.cmds('reduce')[-1]['reason'], 'cbdr')

    def test_eod_flatten_uses_one_liquidation(self):
        p, _, _ = self.at_break_even()
        mark = len(self.fake.bodies)
        self.eod()
        self.closed('eod')
        self.assertEqual(self.requests_since(mark), ['order/liquidateposition'])
        self.assertEqual(self.fake.orders[self.stop_id()]['ordStatus'], 'Canceled')


class Fallbacks(NativeLoop):
    AUTO_PROTECT = False

    def test_placeoso_rejected_falls_back_to_synthetic_entry_and_exits(self):
        self.fake.reject_next = True
        p = self.enter()
        self.until(lambda: self.net() == 4 and len(self.working()) == 1, what='plain entry and TP1')
        self.assertEqual([e for e, _ in self.fake.bodies[:3]], ['order/placeoso', 'order/placeorder', 'order/placeorder'])
        self.assertEqual(self.trade()['native']['fallback'], 'bracket_entry_rejected')
        self.until(lambda: self.trade()['entry_filled'] == 4, what='Soup reconciled the plain entry')
        self.quote(p['initial_hard_stop_price'])  # synthetic: immediate terminal on the touch
        self.fake.set_price(p['initial_hard_stop_price'])
        self.closed('initial_hard_stop')
        self.assertEqual([e for e, _ in self.fake.bodies[3:]], ['order/cancelorder', 'order/placeorder'])

    def test_partial_entry_fill_exits_all(self):
        self.fake.next_market_partial = 2
        self.enter()
        self.until(lambda: self.trade()['terminal_reason'] is not None, what='incomplete entry seen')
        self.closed()
        self.assertIn(self.trade()['terminal_reason'], ('entry_incomplete', 'tp1_order_rejected'))
        self.assertEqual(max(self.path()), 2)


class BreakEvenMoveFails(NativeLoop):
    """A failed break-even move cancels the resting stop, proves it gone, then runs
    today's synthetic break-even watcher. A failed cancel exits all. Never a reversed position."""
    AUTO_PROTECT = False

    def synthetic_break_even_exit(self, p):
        self.until(lambda: now_ms() >= self.trade()['runner_not_before'] + 200, what='runner window')
        mark = len(self.fake.bodies)
        self.fake.set_price(p['break_even_price'])  # no broker stop left: nothing fills at the exchange
        self.quote(p['break_even_price'])
        self.assertEqual(self.trade()['reductions'], {'parent': 'break_even', 'eod': 'break_even'})
        self.closed()
        self.assertEqual(self.requests_since(mark), ['order/placeorder'])  # the synthetic market reduce of 2
        self.assertEqual((self.fake.bodies[-1][1]['orderType'], self.fake.bodies[-1][1]['orderQty']), ('Market', 2))

    def test_modify_rejected_cancel_confirmed_then_synthetic_break_even_exit(self):
        p = self.running()
        self.fake.reject_modify_next = True
        mark = len(self.fake.bodies)
        self.fake.set_price(tp1_limit(p))
        self.until(lambda: self.trade()['native']['mode'] == 'synthetic', what='synthetic after the stop cancel')
        self.assertEqual(self.requests_since(mark), ['order/modifyorder', 'order/cancelorder'])
        self.assertEqual(self.fake.orders[self.stop_id()]['ordStatus'], 'Canceled')
        self.assertEqual(self.trade()['native']['fallback'], 'stop_move_failed:stop_modify_rejected')
        self.assertIsNone(self.trade()['terminal_reason'])
        self.assertEqual(self.net(), 2)
        self.synthetic_break_even_exit(p)
        self.assertEqual(self.path()[-3:], [4, 2, 0])

    def test_modify_unconfirmed_cancel_confirmed_then_synthetic(self):
        p = self.running()
        self.fake.swallow_endpoints.add('order/modifyorder')  # the request is lost: outcome unknown
        self.fake.set_price(tp1_limit(p))
        self.until(lambda: self.trade()['native']['mode'] == 'synthetic', what='synthetic after the stop cancel')
        self.assertEqual(self.trade()['native']['fallback'], 'stop_move_failed:stop_modify_unconfirmed')
        self.assertEqual(self.fake.orders[self.stop_id()]['ordStatus'], 'Canceled')
        self.synthetic_break_even_exit(p)

    def test_modify_and_cancel_fail_exit_all(self):
        p = self.running()
        self.fake.reject_modify_next = True
        self.fake.reject_cancel_next = True
        self.fake.set_price(tp1_limit(p))
        self.closed('native_stop_cancel_unconfirmed')
        self.assertIn('order/liquidateposition', [e for e, _ in self.fake.bodies])
        self.assertEqual(self.path()[-3:], [4, 2, 0])

    def test_oversized_stop_filling_during_the_cancel_is_flattened_and_blocked(self):
        """No auto-protect shrink and the modify rejected: the stop is still 4 against 2. It fills
        while the cancel is in flight (short 2). Soup never accepts that as flat: the cancel bound
        expires, flatten_all liquidates the short, and the unverifiable trade blocks entries."""
        p = self.running()
        self.fake.reject_modify_next = True
        self.fake.cancel_race = 'fill_first'
        self.fake.set_price(tp1_limit(p))
        self.until(lambda: (self.trade()['native'] or {}).get('failed'), timeout=40, what='exit-all failure recorded')
        self.assertEqual((self.net(), self.fake.working_orders(CONTRACT)), (0, []))
        self.assertEqual(self.path()[-4:], [4, 2, -2, 0])
        self.assertEqual(self.trade()['terminal_reason'], 'native_stop_cancel_unconfirmed')
        self.assertNotEqual(self.trade()['phase'], 'closed')  # entries stay blocked until a person reconciles


class BreakEvenMoveFailsAfterAutoShrink(NativeLoop):
    AUTO_PROTECT = True

    def test_stop_fills_during_the_cancel_exit_all_without_reversal(self):
        p = self.running()
        self.fake.reject_modify_when = lambda body: body.get('stopPrice') == p['break_even_price']
        self.fake.cancel_race = 'fill_first'
        self.fake.set_price(tp1_limit(p))
        self.closed('broker_stop_initial')  # the auto-shrunk stop (2) filled: flat, never short
        self.assertEqual(self.path()[-3:], [4, 2, 0])
        self.assertEqual(self.fake.versions[self.stop_id()]['orderQty'], 2)
        self.assertEqual(self.trade()['native']['mode'], 'native')


class BracketsDisabledAtTheBridge(NativeLoop):
    CFG = {'bracket_orders_enabled': False}
    AUTO_PROTECT = False

    def test_refused_bracket_falls_back_without_any_bracket_request(self):
        self.enter()
        self.until(lambda: self.net() == 4 and len(self.working()) == 1, what='plain entry and TP1')
        self.assertNotIn('order/placeoso', [e for e, _ in self.fake.bodies])
        self.assertEqual(self.sent[0][2:], ('rejected', self.sent[0][3]))
        self.assertIn('bracket_orders_enabled', self.sent[0][3]['reason'])


class LiquidationLeavesOrders(NativeLoop):
    def test_bridge_cancels_what_liquidateposition_left_working(self):
        self.fake.liquidate_cancels_orders = False
        p = self.running()
        self.eod()
        self.closed('eod')
        receipt = next(r for k, a, o, r in self.sent if a == 'flatten_all')
        self.assertEqual(sorted(receipt['left_working_after_liquidation']),
                         sorted(str(o) for o in (self.stop_id(), int(self.cmds('tp1_limit')[0]['receipt']['broker_order_id']))))
        self.assertEqual(receipt['leftover_cancel']['outcome'], 'accepted')


class LiquidationLeavesOrdersSoupRetries(NativeLoop):
    CFG = {'flatten_all_cancel_leftovers': False}
    AUTO_PROTECT = False

    def test_soup_verifies_and_sends_a_second_exit_all(self):
        self.fake.liquidate_cancels_orders = False
        self.running()
        self.eod()
        self.closed('eod')
        self.assertEqual(self.trade()['native']['exit_attempts'], 2)
        flats = [r for k, a, o, r in self.sent if a == 'flatten_all']
        self.assertEqual((flats[0]['mode'], flats[1]['mode']), ('liquidate', 'cancel_only'))


class Restarts(NativeLoop):
    AUTO_PROTECT = True

    def test_soup_crash_with_modify_started_is_never_replayed(self):
        p = self.running()
        self.fake.set_price(tp1_limit(p))
        deadline = time.monotonic() + 10
        while not any(c['status'] == 'ready' for c in self.cmds('modify_stop')):  # evidence only, no dispatch
            self.assertLess(time.monotonic(), deadline)
            self.evidence()
            time.sleep(0.05)
        c = self.engine.start(self.engine.ready()[0]['id'], now_ms())  # crash before it is sent
        self.engine.close()
        self.engine = self.open_engine()
        self.assertEqual(self.cmds('modify_stop')[0]['status'], 'unknown')
        # Unconfirmed after the bound: cancel the stop, prove it gone, continue synthetic.
        self.until(lambda: self.trade()['native']['mode'] == 'synthetic', what='synthetic after the stop cancel')
        self.assertEqual(self.trade()['native']['fallback'], 'stop_move_failed:stop_modify_unconfirmed')
        self.assertEqual((self.fake.orders[self.stop_id()]['ordStatus'], self.net()), ('Canceled', 2))
        self.assertNotIn(c['id'], self.ex.journal.commands)  # the bridge never saw it: never replayed
        self.assertEqual(len([b for e, b in self.fake.bodies if e == 'order/modifyorder'
                              and b.get('stopPrice') == p['break_even_price']]), 0)

    def test_bridge_restart_mid_trade_keeps_legs_and_refuses_duplicates(self):
        p = self.running()
        entry = self.cmds('entry')[0]
        self.ex = Executor(self.bridge, self.state, Journal(self.cfg['journal_dir']), self.cfg)
        self.ex.resolve()
        mark = len(self.fake.bodies)
        out, rec = self.ex.send(message(entry, CONFIG['ticker'], 'long', quantity_limit=10))
        self.assertEqual((out, rec.get('duplicate')), ('accepted', True))
        self.assertEqual(self.fake.bodies[mark:], [])
        self.fake.set_price(tp1_limit(p))
        self.until(lambda: self.fake.versions[self.stop_id()].get('stopPrice') == p['break_even_price'], what='BE')
        self.fake.set_price(p['break_even_price'])
        self.closed('broker_stop_break_even')


class ReversalGuard(NativeLoop):
    def test_tp1_and_full_stop_fill_before_the_shrink_is_liquidated_and_blocks_entries(self):
        p = self.running()
        mark = len(self.fake.position_history)
        with self.fake.lock:  # TP1 fills, then the market collapses through the unshrunk stop
            self.fake.set_price(tp1_limit(p))
            self.fake.set_price(p['initial_hard_stop_price'] - 1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (self.net() == 0 and os.path.exists(self.cfg['kill_switch_file'])):
            with self.ex.lock:
                self.ex.maintain_brackets()
            time.sleep(0.1)
        self.assertEqual(self.path(mark), [2, -2, 0])
        self.assertTrue(os.path.exists(self.cfg['kill_switch_file']))
        auto = self.ex.journal.get(leg_command_id(self.cmds('entry')[0]['id'], 'auto-flatten'))
        self.assertEqual((auto['kind'], auto['outcome']), ('auto_flatten', 'accepted'))
        snap, reason = self.ex.snapshot(self.trade()['id'])
        self.assertIsNone(snap)
        self.assertTrue(reason.startswith('auto_flatten_after_reversal'))


def tearDownModule():
    print('NATIVE-RESULTS ' + json.dumps(RESULTS, sort_keys=True), file=sys.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
