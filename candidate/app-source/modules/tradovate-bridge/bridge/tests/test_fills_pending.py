"""A partly filled exit order that the broker reports 'Filled' after the rest was removed.

Synthetic fixture (account FAKE, short 5, TP1 2): the bracket stop (5) fills 2, the engine's
flatten_all (liquidateposition) fills the other 3 through its own order, and the broker then
reports the stop with ordStatus 'Filled', orderQty 5 and only the 2-lot fill. The snapshot must
report the stop as cancelled with its real fill instead of withholding every snapshot as
`fills_pending:<stop>`, which would leave publisher health failed and every order refused.
In-memory doubles only: no browser, no network, no broker.
"""
import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rollover_publisher import Driver, Page, retired  # noqa: E402  (shared page and publisher doubles)
from tvbridge import daemon  # noqa: E402
from tvbridge.daemon import Publisher  # noqa: E402
from tvbridge.executor import Executor, client_tag  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402

AT = 1605717730000  # synthetic entry time (epoch ms)
TRADE = 'fixture-stream:MNQZ2021:%d:short' % (AT - 5000)
ENTRY, TP1, FLAT, NOOP = ('soup-00000000000000000000000000000001', 'soup-00000000000000000000000000000002',
                          'soup-00000000000000000000000000000003', 'soup-00000000000000000000000000000004')
ENTRY_ID, STOP_ID, TP1_ID, FLAT_ID = 1000001, 1000002, 1000003, 1000004
ENTRY_PX, STOP_PX, TP1_PX, FLAT_PX = 5000.0, 5017.5, 4972.5, 5018.25
NOW_MS = AT + 60_000
STEP_S, LINGER_S, GRACE_S = 2.0, 60.0, 10.0
TRADE_DATE = {'year': 2020, 'month': 11, 'day': 18}


def iso(at_ms):
    """Broker fill timestamp text for an epoch-ms time."""
    return datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + \
        '%03dZ' % (at_ms % 1000)


def journal_records(flatten=True):
    """The leader journal of the fixture trade (receipts shortened)."""
    records = [
        {'type': 'start', 'command_id': ENTRY, 'kind': 'bracket_entry', 'trade_id': TRADE, 'side': 'short',
         'quantity': 5, 'at': AT, 'tag': client_tag(ENTRY), 'reference_price': ENTRY_PX, 'stop_price': STOP_PX,
         'tp1_leg': None, 'tp1_price': None, 'tp1_quantity': None, 'exit_action': 'Buy'},
        {'type': 'result', 'command_id': ENTRY, 'outcome': 'accepted', 'receipt': {},
         'broker_order_id': str(ENTRY_ID), 'legs': {'stop': str(STOP_ID)}},
        {'type': 'start', 'command_id': TP1, 'kind': 'tp1_limit', 'trade_id': TRADE, 'side': 'short',
         'quantity': 2, 'at': AT + 250, 'tag': client_tag(TP1)},
        {'type': 'result', 'command_id': TP1, 'outcome': 'accepted', 'receipt': {}, 'broker_order_id': str(TP1_ID)},
    ]
    if flatten:
        records += [
            {'type': 'start', 'command_id': FLAT, 'kind': 'flatten_all', 'trade_id': TRADE, 'side': 'short',
             'quantity': 3, 'at': AT + 2000, 'tag': None, 'mode': 'liquidate', 'close_action': 'Buy',
             'baseline_order_id': TP1_ID},
            {'type': 'result', 'command_id': FLAT, 'outcome': 'accepted', 'broker_order_id': str(FLAT_ID),
             'receipt': {'mode': 'liquidate', 'matched_by_liquidation_order': True}},
            {'type': 'start', 'command_id': NOOP, 'kind': 'flatten_all', 'trade_id': TRADE, 'side': 'short',
             'quantity': None, 'at': AT + 6000, 'tag': None, 'mode': 'noop'},
            {'type': 'result', 'command_id': NOOP, 'outcome': 'accepted',
             'receipt': {'bridge': 'noop', 'reason': 'already flat with nothing working'}},
        ]
    return records


def order(oid, status):
    return {'id': oid, 'accountId': 1, 'contractId': 2, 'ordStatus': status}


def fill(fid, oid, qty, price, ts):
    return {'id': fid, 'orderId': oid, 'contractId': 2, 'qty': qty, 'price': price, 'timestamp': ts}


def page_data(net=0, stop_status='Filled', stop_fills=((1000012, 2, STOP_PX, iso(AT + 1500)),),
              tp1_status='Canceled', tp1_fills=(), flatten=True, entry_status='Filled', entry_fill_qty=5):
    """The broker sync as the page holds it after the liquidation fill."""
    orders = [order(ENTRY_ID, entry_status), order(STOP_ID, stop_status), order(TP1_ID, tp1_status)]
    fills = [fill(1000011, ENTRY_ID, entry_fill_qty, ENTRY_PX, iso(AT + 250))]
    fills += [fill(fid, STOP_ID, q, p, ts) for fid, q, p, ts in stop_fills]
    fills += [fill(fid, TP1_ID, q, p, ts) for fid, q, p, ts in tp1_fills]
    versions = [{'id': 31, 'orderId': ENTRY_ID, 'orderQty': 5, 'orderType': 'Market'},
                {'id': 32, 'orderId': STOP_ID, 'orderQty': 5, 'orderType': 'Stop', 'stopPrice': STOP_PX},
                {'id': 33, 'orderId': TP1_ID, 'orderQty': 2, 'orderType': 'Limit', 'price': TP1_PX}]
    if flatten:
        orders.append(order(FLAT_ID, 'Filled'))
        fills.append(fill(1000014, FLAT_ID, 3, FLAT_PX, iso(AT + 2500)))
        versions.append({'id': 34, 'orderId': FLAT_ID, 'orderQty': 3, 'orderType': 'Market'})
    return {'accounts': [{'id': 1, 'name': 'FAKE'}], 'orders': orders, 'fills': fills,
            'positions': [{'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': net, 'tradeDate': TRADE_DATE}],
            'orderVersions': versions, 'contracts': [{'id': 2, 'name': 'MNQZ1'}]}


class FilledShort(unittest.TestCase):
    def build(self, data, records=None, settle_ms=0, state_clock=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name) / 'journal'
        d.mkdir()
        (d / 'commands.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in (records or journal_records())))
        self.journal_bytes = (d / 'commands.jsonl').read_bytes()
        self.journal_path = d / 'commands.jsonl'
        self.journal = Journal(d)
        self.state = AccountState(clock=state_clock) if state_clock else AccountState()
        self.page = Page(self.state, data)
        self.executor = Executor(self.page, self.state, self.journal, {
            'ticker': 'MNQZ2021', 'account_name': 'FAKE', 'account_id': 1, 'soup_account_id': 'Account A',
            'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0, 'terminal_settle_ms': settle_ms},
            clock_ms=lambda: NOW_MS)
        self.executor.resolve()

    def capture(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        log = logging.getLogger('tvbridge')
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        return records

    def run_publisher(self, seconds):
        clock = [1000.0]
        start = clock[0]
        seen, reasons = [], []
        real = self.executor.snapshot

        def counted(trade_id):
            snap, reason = real(trade_id)
            reasons.append(reason)
            return snap, reason
        self.executor.snapshot = counted

        def observe(now):
            seen.append((now - start, self.publisher.watchdog(), self.executor.health()['ok']))
        driver = Driver(clock, int(seconds / STEP_S), observe)
        self.evidence = Path(self.tmp.name) / 'snapshots.jsonl'
        self.publisher = Publisher(self.executor, self.journal, driver, 'evidence', evidence_path=str(self.evidence),
                                   interval_s=STEP_S, linger_s=LINGER_S, clock=lambda: clock[0],
                                   trade_kinds=('entry', 'bracket_entry'))
        self.executor.watchdogs.append(self.publisher.watchdog)
        driver.publisher = self.publisher
        self.publisher.run()
        return seen, reasons

    def published(self):
        if not self.evidence.exists():
            return []
        return [json.loads(line) for line in self.evidence.read_text().splitlines()]

    def by_id(self, snap):
        return {o['broker_order_id']: o for o in snap['orders']}

    # --- the fixture trade -------------------------------------------------------------------

    def test_liquidated_stop_is_reported_cancelled_with_its_real_fill(self):
        self.build(page_data())
        logs = self.capture()
        snap, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(reason)
        self.assertEqual(snap['position_qty'], 0)
        self.assertEqual(snap['open_orders'], [])
        orders = self.by_id(snap)
        self.assertEqual(set(orders), {str(ENTRY_ID), str(STOP_ID), str(TP1_ID), str(FLAT_ID)})  # noop: no order
        stop = orders[str(STOP_ID)]
        self.assertEqual((stop['status'], stop['filled_qty'], stop['quantity'], stop['stop_price'], stop['role']),
                         ('cancelled', 2, 5, STOP_PX, 'stop'))
        self.assertEqual(stop['fill_time'], AT + 1500)
        self.assertEqual(stop['command_id'], ENTRY + '#stop')
        self.assertEqual((orders[str(ENTRY_ID)]['status'], orders[str(ENTRY_ID)]['filled_qty']), ('filled', 5))
        self.assertEqual((orders[str(TP1_ID)]['status'], orders[str(TP1_ID)]['filled_qty']), ('cancelled', 0))
        flat = orders[str(FLAT_ID)]
        self.assertEqual((flat['status'], flat['filled_qty'], flat['quantity'], flat['role'], flat['fill_time']),
                         ('filled', 3, 3, 'flatten', AT + 2500))
        # Conservation as the engine checks it: entry 5 - (stop 2 + TP1 0 + flatten 3) = position 0.
        self.assertEqual(5 - sum(o['filled_qty'] for k, o in orders.items() if k != str(ENTRY_ID)), 0)
        # One WARNING per order, however often the snapshot is rebuilt.
        self.executor.snapshot(TRADE)
        warned = [r for r in logs if r.levelno == logging.WARNING and str(STOP_ID) in r.getMessage()]
        self.assertEqual(len(warned), 1)

    def test_fixture_has_the_filled_but_partly_filled_shape(self):
        """Guard for the test itself: the page data really has a 'Filled' stop with a partial fill."""
        self.build(page_data())
        with self.state.lock:
            self.assertEqual(self.state.order(STOP_ID)['ordStatus'], 'Filled')
            self.assertEqual((self.state.order_qty(STOP_ID), self.state.filled_qty(STOP_ID)), (5, 2))

    def test_publisher_publishes_and_health_stays_ok(self):
        self.build(page_data())
        seen, reasons = self.run_publisher(LINGER_S + 30)
        self.assertTrue(all(r is None for r in reasons), reasons)
        self.assertTrue(all(problem is None and ok for _, problem, ok in seen), seen)
        snaps = self.published()
        self.assertTrue(snaps)
        self.assertEqual({s['trade_id'] for s in snaps}, {TRADE})
        self.assertTrue(all(self.by_id(s)[str(STOP_ID)]['status'] == 'cancelled' for s in snaps))
        # Linger rule unchanged: nothing after the 60 s window for an inactive trade.
        self.assertLessEqual(len(snaps), int(LINGER_S / STEP_S) + 1)
        self.assertEqual(self.journal_path.read_bytes(), self.journal_bytes)   # no journal write

    def test_partial_stop_then_soup_cancel_with_position_left(self):
        """The rest removed by a cancel while contracts remain: still exact (5 - 2 = 3 held, TP1 working)."""
        self.build(page_data(net=-3, flatten=False, tp1_status='Working'),
                   records=journal_records(flatten=False))
        snap, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(reason)
        self.assertEqual(snap['position_qty'], 3)
        self.assertEqual(snap['open_orders'], [str(TP1_ID)])
        stop = self.by_id(snap)[str(STOP_ID)]
        self.assertEqual((stop['status'], stop['filled_qty'], stop['quantity']), ('cancelled', 2, 5))

    # --- still withheld -------------------------------------------------------------------

    def test_fills_still_arriving_within_settle_time_are_withheld(self):
        t = [NOW_MS / 1000.0]
        self.build(page_data(), settle_ms=1500, state_clock=lambda: t[0])
        snap, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(snap)
        self.assertEqual(reason, 'fills_pending:%d' % STOP_ID)
        t[0] += 1.6
        snap, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(reason)
        self.assertEqual(self.by_id(snap)[str(STOP_ID)]['status'], 'cancelled')

    def test_missing_fill_is_withheld_not_invented(self):
        """Stop really filled 5 (no liquidation), only the 2-lot fill arrived, position already flat."""
        self.build(page_data(flatten=False), records=journal_records(flatten=False))
        snap, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(snap)
        self.assertEqual(reason, 'fills_pending:%d' % STOP_ID)

    def test_fills_and_position_both_lagging_are_withheld_for_the_settle_time(self):
        """Stop filled 5; the page has its status, the 2-lot fill and a position not yet updated (-3).
        The fills would explain that stale position, so only the settle time protects here (the
        same rule that applies to a Canceled order with fills)."""
        t = [NOW_MS / 1000.0]
        self.build(page_data(net=-3, flatten=False, tp1_status='Working'), records=journal_records(flatten=False),
                   settle_ms=1500, state_clock=lambda: t[0])
        self.assertEqual(self.executor.snapshot(TRADE), (None, 'fills_pending:%d' % STOP_ID))
        # The missing fills and position arrive within the settle time: reported filled.
        self.state.apply_props({'entityType': 'fill', 'eventType': 'Created',
                                'entity': fill(1000013, STOP_ID, 3, STOP_PX, iso(AT + 2000))})
        self.state.apply_props({'entityType': 'position', 'eventType': 'Updated',
                                'entity': {'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': 0,
                                           'tradeDate': TRADE_DATE}})
        t[0] += 1.6
        snap, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(reason)
        stop = self.by_id(snap)[str(STOP_ID)]
        self.assertEqual((stop['status'], stop['filled_qty']), ('filled', 5))

    def test_filled_without_any_fill_is_withheld(self):
        self.build(page_data(stop_fills=()))
        self.assertEqual(self.executor.snapshot(TRADE), (None, 'fills_pending:%d' % STOP_ID))

    def test_entry_filled_short_is_withheld(self):
        self.build(page_data(net=-2, entry_fill_qty=2, stop_fills=(), stop_status='Working', flatten=False,
                             tp1_status='Working'), records=journal_records(flatten=False))
        self.assertEqual(self.executor.snapshot(TRADE), (None, 'fills_pending:%d' % ENTRY_ID))

    def test_complete_fills_unchanged(self):
        self.build(page_data(stop_fills=((1000012, 2, STOP_PX, iso(AT + 1500)),
                                         (1000013, 3, STOP_PX, iso(AT + 2000))), flatten=False),
                   records=journal_records(flatten=False))
        snap, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(reason)
        stop = self.by_id(snap)[str(STOP_ID)]
        self.assertEqual((stop['status'], stop['filled_qty']), ('filled', 5))

    def test_overfill_still_refused(self):
        self.build(page_data(tp1_status='Filled', tp1_fills=((1000019, 3, TP1_PX, iso(AT + 1000)),)))
        self.assertEqual(self.executor.snapshot(TRADE), (None, 'overfill:%d' % TP1_ID))

    # --- the publisher on a flat, clean trade that stays withheld ---------------------------

    def test_inactive_trade_withheld_as_fills_pending_retires_after_linger(self):
        self.build(page_data(flatten=False), records=journal_records(flatten=False))  # missing fill, flat
        logs = self.capture()
        seen, reasons = self.run_publisher(LINGER_S + 40)
        self.assertTrue(all(r == 'fills_pending:%d' % STOP_ID for r in reasons), reasons)
        # Grace, then failed, as before, until the linger window ends ...
        self.assertTrue(any(problem == 'snapshot_publisher_failed:snapshot_unavailable' for t, problem, _ in seen
                            if GRACE_S + STEP_S < t < LINGER_S))
        # ... then retired: health ok, nothing published, no journal write.
        tail = [s for s in seen if s[0] > LINGER_S + 2 * STEP_S]
        self.assertTrue(tail and all(problem is None and ok for _, problem, ok in tail), tail)
        self.assertEqual(retired(self.publisher), {TRADE})
        self.assertEqual(self.publisher.last['reason'], 'inactive_trade_fills_incomplete')
        self.assertEqual(self.publisher.last['withheld'], 'fills_pending:%d' % STOP_ID)
        self.assertEqual(self.published(), [])
        self.assertEqual(self.journal_path.read_bytes(), self.journal_bytes)
        self.assertEqual(len([r for r in logs if r.levelno == logging.WARNING and 'not republishing' in r.getMessage()]), 1)

    def test_active_trade_withheld_as_fills_pending_never_retires(self):
        self.build(page_data(net=-2, entry_fill_qty=2, stop_fills=(), stop_status='Working', flatten=False,
                             tp1_status='Working'), records=journal_records(flatten=False))
        seen, reasons = self.run_publisher(LINGER_S + 40)
        self.assertEqual(retired(self.publisher), set())
        self.assertEqual(seen[-1][1], 'snapshot_publisher_failed:snapshot_unavailable')

    def test_real_inconsistency_elsewhere_blocks_the_retire(self):
        """Withheld at the stop (Filled, no fill) while TP1 was Rejected with a fill: stays failed."""
        self.build(page_data(stop_fills=(), tp1_status='Rejected',
                             tp1_fills=((1000019, 1, TP1_PX, iso(AT + 1000)),), flatten=False),
                   records=journal_records(flatten=False))
        self.assertEqual(daemon._order_refusal(self.executor, TRADE), 'rejected_with_fills:%d' % TP1_ID)
        seen, reasons = self.run_publisher(LINGER_S + 40)
        self.assertTrue(all(r == 'fills_pending:%d' % STOP_ID for r in reasons), reasons)
        self.assertEqual(retired(self.publisher), set())
        self.assertEqual(seen[-1][1], 'snapshot_publisher_failed:snapshot_unavailable')

    def test_other_refusals_never_retire(self):
        """overfill is not a retire reason."""
        self.build(page_data(tp1_status='Filled', tp1_fills=((1000019, 3, TP1_PX, iso(AT + 1000)),)))
        seen, reasons = self.run_publisher(LINGER_S + 40)
        self.assertEqual(retired(self.publisher), set())
        self.assertTrue(reasons[-1].startswith('overfill:'))
        self.assertEqual(seen[-1][1], 'snapshot_publisher_failed:snapshot_unavailable')


if __name__ == '__main__':
    unittest.main()
