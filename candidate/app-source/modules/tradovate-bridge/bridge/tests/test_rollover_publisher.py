"""The snapshot publisher after a daemon restart in a later CME session.

When the daemon restarts after the 18:00 New York session start, the reloaded page no longer
lists the previous session's orders. A closed, flat trade from that session is withheld with
`order_not_seen_yet:<stop id>` while it lingers; after the linger window the publisher retires
it once (one WARNING) and health recovers. Active trades, unknown or uncertain commands,
same-session gaps, page problems and publisher exceptions keep failing closed. Synthetic
fixture with in-memory doubles only: no browser, no network, no Tradovate.
"""
import json
import logging
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge.daemon import Publisher
from tvbridge.executor import Executor, client_tag
from tvbridge.journal import Journal
from tvbridge.state import AccountState

NY = ZoneInfo('America/New_York')
# Synthetic fixture: a long bracket entry of 6 contracts with a 2-contract TP1, all on one Monday.
DAY = (2021, 11, 15)
TRADE = 'fixture-stream:MNQZ2021:1636992898000:long'   # signal 2 s before the entry
NEXT_TRADE = 'fixture-stream:MNQZ2021:1637022660000:long'
ENTRY, TP1, NOOP = ('soup-00000000000000000000000000000001', 'soup-00000000000000000000000000000002',
                    'soup-00000000000000000000000000000003')
ENTRY_ID, STOP_ID, TP1_ID = 1000001, 1000002, 1000005
QTY, TP1_QTY = 6, 2
REFERENCE, STOP_PRICE, TP1_FILL = 10000.0, 9992.0, 10011.25
PREVIOUS_SESSION_MS = int(datetime(*DAY, 11, 15, tzinfo=NY).timestamp() * 1000)
SAME_SESSION_MS = int(datetime(*DAY, 18, 45, tzinfo=NY).timestamp() * 1000)
RESTART_MS = int(datetime(*DAY, 19, 30, tzinfo=NY).timestamp() * 1000)
TRADE_DATE = {'year': DAY[0], 'month': DAY[1], 'day': DAY[2]}
STEP_S = 2.0       # publisher interval
LINGER_S = 60.0    # daemon default
GRACE_S = 10.0     # Publisher.HOLD_GRACE_S


def trade_records(at, extra=()):
    """A closed trade as the journal holds it (receipts shortened)."""
    records = [
        {'type': 'start', 'command_id': ENTRY, 'kind': 'bracket_entry', 'trade_id': TRADE, 'side': 'long',
         'quantity': QTY, 'at': at, 'tag': client_tag(ENTRY), 'reference_price': REFERENCE, 'stop_price': STOP_PRICE,
         'tp1_leg': None, 'tp1_price': None, 'tp1_quantity': None, 'exit_action': 'Sell'},
        {'type': 'result', 'command_id': ENTRY, 'outcome': 'accepted', 'receipt': {},
         'broker_order_id': str(ENTRY_ID), 'legs': {'stop': str(STOP_ID)}},
        {'type': 'start', 'command_id': TP1, 'kind': 'tp1_limit', 'trade_id': TRADE, 'side': 'long',
         'quantity': TP1_QTY, 'at': at + 500, 'tag': client_tag(TP1)},
        {'type': 'result', 'command_id': TP1, 'outcome': 'accepted', 'receipt': {}, 'broker_order_id': str(TP1_ID)},
        {'type': 'start', 'command_id': ENTRY + '#auto-cancel', 'kind': 'auto_cancel', 'trade_id': TRADE,
         'side': 'long', 'quantity': None, 'at': at + 45000, 'tag': None, 'targets': [str(TP1_ID)]},
        {'type': 'result', 'command_id': ENTRY + '#auto-cancel', 'outcome': 'accepted', 'receipt': {}},
        {'type': 'start', 'command_id': NOOP, 'kind': 'flatten_all', 'trade_id': TRADE, 'side': 'long',
         'quantity': None, 'at': at + 46000, 'tag': None, 'mode': 'noop'},
        {'type': 'result', 'command_id': NOOP, 'outcome': 'accepted', 'receipt': {}},
    ]
    return records + list(extra)


def sync(net=0, orders=(), fills=(), versions=()):
    return {'accounts': [{'id': 1, 'name': 'FAKE'}], 'orders': list(orders), 'fills': list(fills),
            'positions': [{'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': net,
                           'tradeDate': TRADE_DATE}],
            'orderVersions': list(versions), 'contracts': [{'id': 2, 'name': 'MNQZ1'}]}


def trade_visible_on_page():
    """The same trade while the page still lists it (same session): filled, stop cancelled, flat."""
    orders = [{'id': ENTRY_ID, 'accountId': 1, 'contractId': 2, 'ordStatus': 'Filled'},
              {'id': STOP_ID, 'accountId': 1, 'contractId': 2, 'ordStatus': 'Canceled'},
              {'id': TP1_ID, 'accountId': 1, 'contractId': 2, 'ordStatus': 'Filled'}]
    fills = [{'id': 11, 'orderId': ENTRY_ID, 'qty': QTY, 'price': REFERENCE, 'timestamp': '2021-11-15T23:45:02.000Z'},
             {'id': 12, 'orderId': TP1_ID, 'qty': TP1_QTY, 'price': TP1_FILL, 'timestamp': '2021-11-15T23:45:30.000Z'}]
    versions = [{'id': 21, 'orderId': STOP_ID, 'orderQty': QTY - TP1_QTY, 'stopPrice': STOP_PRICE}]
    return sync(0, orders, fills, versions)


def retired(publisher):
    """Trades the publisher stopped following (empty when the publisher keeps no such set)."""
    return getattr(publisher, 'retired', set())


class Page:
    """Loopback page double: authorized, fresh, one connection epoch; never sends anything."""

    def __init__(self, state, data):
        self.state, self.epoch, self.wire = state, 'after-restart', []
        state.load_sync(data['accounts'], data, self.epoch)

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5, expected_epoch=None):
        self.wire.append((endpoint, body))
        return {'ok': True, 'sent': True, 'd': {'orderId': 10}}


class Driver:
    """Stands in for AccountState.wait_for_change: each wait is one publisher interval."""

    def __init__(self, clock, steps, observe):
        self.clock, self.steps, self.observe, self.n = clock, steps, observe, 0
        self.publisher = None
        self.changed = threading.Condition()

    def wait_for_change(self, version, timeout):
        self.clock[0] += STEP_S
        self.n += 1
        self.observe(self.clock[0])
        if self.n >= self.steps:
            self.publisher.stopping.set()
        return version + 1


class RolloverPublisher(unittest.TestCase):
    def build(self, records, data, now_ms=RESTART_MS):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'journal'
        self.dir.mkdir()
        (self.dir / 'commands.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
        self.journal_bytes = (self.dir / 'commands.jsonl').read_bytes()
        self.journal = Journal(self.dir)        # loaded from disk, as after a daemon restart
        self.state = AccountState()
        self.page = Page(self.state, data)
        self.executor = Executor(self.page, self.state, self.journal, {
            'ticker': 'MNQZ2021', 'account_name': 'FAKE', 'account_id': 1, 'soup_account_id': 'FAKE',
            'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0, 'terminal_settle_ms': 0,
            'kill_switch_file': str(self.dir / 'ENTRIES-DISABLED')},
            clock_ms=lambda: now_ms)
        self.executor.resolve()
        self.snapshot_reasons = []
        real = self.executor.snapshot

        def counted(trade_id):
            snap, reason = real(trade_id)
            self.snapshot_reasons.append(reason)
            return snap, reason
        self.executor.snapshot = counted
        self.evidence = Path(self.tmp.name) / 'snapshots.jsonl'

    def run_publisher(self, seconds, again=False, prepare=None):
        """Run the real Publisher.run on a fake clock; returns [(t since start, watchdog, health ok)].

        `again`: keep the same publisher (and its clock) and run it for another `seconds`."""
        if not again:
            self.clock = [1000.0]
        clock = self.clock
        start = clock[0]
        seen = []

        def observe(now):
            seen.append((now - start, self.publisher.watchdog(), self.executor.health()['ok']))
        driver = Driver(clock, int(seconds / STEP_S), observe)
        if again:
            self.publisher.state = driver
            self.publisher.stopping.clear()
        else:
            self.publisher = Publisher(self.executor, self.journal, driver, 'evidence',
                                       evidence_path=str(self.evidence), interval_s=STEP_S, linger_s=LINGER_S,
                                       clock=lambda: clock[0], trade_kinds=('entry', 'bracket_entry'))
            self.executor.watchdogs.append(self.publisher.watchdog)
        driver.publisher = self.publisher
        if prepare:
            prepare(self.publisher)
        self.publisher.run()
        return seen

    def capture(self):
        """Collect the daemon log records without asserting on them (health is checked first)."""
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        log = logging.getLogger('tvbridge')
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        return records

    @staticmethod
    def retire_warnings(records):
        return [r for r in records if r.levelno == logging.WARNING and 'not republishing' in r.getMessage()]

    def assert_fails_throughout(self, seen, reason_prefix):
        after_grace = [s for s in seen if s[0] > GRACE_S + STEP_S]
        self.assertTrue(after_grace)
        for t, problem, ok in after_grace:
            self.assertEqual(problem, 'snapshot_publisher_failed:snapshot_unavailable', t)
            self.assertFalse(ok, t)
        self.assertTrue(self.snapshot_reasons[-1].startswith(reason_prefix), self.snapshot_reasons[-1])
        self.assertEqual(retired(self.publisher), set())

    # --- previous-session trade after a restart ---------------------------------------------

    def test_restart_after_rollover_inactive_closed_trade_recovers_after_linger(self):
        self.build(trade_records(PREVIOUS_SESSION_MS), sync(0))   # page lists none of its orders
        logs = self.capture()
        seen = self.run_publisher(LINGER_S + 40)
        # Inside the linger window: withheld, grace, then failed.
        self.assertEqual(self.snapshot_reasons[0], 'order_not_seen_yet:%d' % STOP_ID)
        for t, problem, ok in seen:
            if t <= GRACE_S:
                self.assertIsNone(problem, t)
            elif GRACE_S + STEP_S < t <= LINGER_S:
                self.assertEqual(problem, 'snapshot_publisher_failed:snapshot_unavailable', t)
                self.assertFalse(ok, t)
            elif t > LINGER_S + 2 * STEP_S:
                self.assertIsNone(problem, t)
                self.assertTrue(ok, t)
        # Retired once, logged once at WARNING with the trade id and the reason.
        warnings = self.retire_warnings(logs)
        self.assertEqual(len(warnings), 1)
        self.assertIn(TRADE, warnings[0].getMessage())
        self.assertIn('order_not_seen_yet:%d' % STOP_ID, warnings[0].getMessage())
        self.assertEqual(retired(self.publisher), {TRADE})
        self.assertEqual(self.publisher.last['reason'], 'inactive_trade_orders_not_visible')
        self.assertEqual(self.publisher.last['published'], 'skipped')
        # No further snapshot attempts once retired; nothing published, no journal write.
        attempts = len(self.snapshot_reasons)
        self.assertLessEqual(attempts, LINGER_S / STEP_S + 2)
        self.assertFalse(self.evidence.exists())
        self.assertFalse((self.dir / 'snapshot-sequence').exists())
        self.assertEqual((self.dir / 'commands.jsonl').read_bytes(), self.journal_bytes)
        self.assertEqual(self.page.wire, [])
        self.assertTrue(self.executor.health()['ok'])

    def test_after_recovery_orders_are_no_longer_refused_as_unhealthy(self):
        self.build(trade_records(PREVIOUS_SESSION_MS), sync(0))
        logs = self.capture()
        self.run_publisher(LINGER_S + 10)
        outcome, receipt = self.executor.send({
            'ticker': 'MNQZ2021', 'action': 'buy', 'orderType': 'market', 'quantity': 1,
            'extras': {'soup_command_id': 'next', 'soup_trade_id': NEXT_TRADE}})
        self.assertNotIn('unhealthy', str(receipt.get('reason')))
        self.assertEqual(outcome, 'accepted')
        self.assertEqual(len(self.retire_warnings(logs)), 1)
        self.assertEqual(len(self.page.wire), 1)       # the new order only; nothing for the old trade

    def test_retired_trade_that_becomes_active_again_is_followed_again(self):
        self.build(trade_records(PREVIOUS_SESSION_MS), sync(0))
        logs = self.capture()
        self.run_publisher(LINGER_S + 10)
        self.assertEqual(retired(self.publisher), {TRADE})
        self.assertEqual(len(self.retire_warnings(logs)), 1)
        # A position on the contract makes the trade active: the publisher fails closed again.
        self.state.apply_props({'entityType': 'position', 'eventType': 'Updated', 'entity': {
            'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': 1, 'tradeDate': TRADE_DATE}})
        seen = self.run_publisher(40, again=True)
        self.assert_fails_throughout(seen, 'order_not_seen_yet:')

    # --- real problems keep failing -----------------------------------------------------------

    def test_active_trade_with_missing_orders_still_fails(self):
        for label, data in (('position open', sync(QTY)),
                            ('order working', sync(0, orders=[{'id': 1000020, 'accountId': 1, 'contractId': 2,
                                                                'ordStatus': 'Working'}]))):
            with self.subTest(label):
                self.build(trade_records(PREVIOUS_SESSION_MS), data)
                seen = self.run_publisher(LINGER_S + 40)
                self.assert_fails_throughout(seen, 'order_not_seen_yet:')

    def test_unknown_command_still_fails(self):
        extra = [{'type': 'start', 'command_id': 'soup-exit', 'kind': 'exit_market', 'trade_id': TRADE,
                  'side': 'long', 'quantity': QTY - TP1_QTY, 'at': PREVIOUS_SESSION_MS + 60000, 'tag': 'DLexit'}]
        self.build(trade_records(PREVIOUS_SESSION_MS, extra), sync(0))   # no result: unknown after restart
        seen = self.run_publisher(LINGER_S + 40)
        self.assert_fails_throughout(seen, 'order_not_seen_yet:')

    def test_uncertain_dispatch_after_the_missing_order_still_fails(self):
        # Accepted without an order id: the snapshot never reaches it (it stops at the missing
        # stop leg), so the publisher must check the journal itself.
        extra = [{'type': 'start', 'command_id': 'soup-exit', 'kind': 'exit_market', 'trade_id': TRADE,
                  'side': 'long', 'quantity': QTY - TP1_QTY, 'at': PREVIOUS_SESSION_MS + 60000, 'tag': 'DLexit'},
                 {'type': 'result', 'command_id': 'soup-exit', 'outcome': 'accepted', 'receipt': {}}]
        self.build(trade_records(PREVIOUS_SESSION_MS, extra), sync(0))
        seen = self.run_publisher(LINGER_S + 40)
        self.assert_fails_throughout(seen, 'order_not_seen_yet:')

    def test_uncertain_dispatch_reported_by_snapshot_still_fails(self):
        records = [{'type': 'start', 'command_id': 'plain', 'kind': 'entry', 'trade_id': TRADE, 'side': 'long',
                    'quantity': 1, 'at': PREVIOUS_SESSION_MS, 'tag': 'DLplain'},
                   {'type': 'result', 'command_id': 'plain', 'outcome': 'accepted', 'receipt': {}}]
        self.build(records, sync(0))
        with self.assertLogs('tvbridge', level='ERROR'):
            seen = self.run_publisher(LINGER_S + 40)
        self.assert_fails_throughout(seen, 'uncertain_dispatch:plain')

    def test_auto_flatten_after_reversal_still_fails(self):
        extra = [{'type': 'start', 'command_id': ENTRY + '#auto-flatten', 'kind': 'auto_flatten', 'trade_id': TRADE,
                  'side': 'long', 'quantity': None, 'at': PREVIOUS_SESSION_MS + 75000, 'tag': None,
                  'mode': 'liquidate'},
                 {'type': 'result', 'command_id': ENTRY + '#auto-flatten', 'outcome': 'accepted', 'receipt': {},
                  'broker_order_id': '1000009'}]
        self.build(trade_records(PREVIOUS_SESSION_MS, extra), sync(0))
        seen = self.run_publisher(LINGER_S + 40)
        self.assert_fails_throughout(seen, 'order_not_seen_yet:')

    def test_same_session_order_missing_from_page_still_fails(self):
        # Orders of the current CME session must be on the page; their absence is a page/sync
        # problem, not an expired Day order.
        self.build(trade_records(SAME_SESSION_MS), sync(0))
        seen = self.run_publisher(LINGER_S + 40)
        self.assert_fails_throughout(seen, 'order_not_seen_yet:')

    def test_before_the_new_session_starts_it_still_fails(self):
        # Restart in the 17:00-18:00 New York break: the trade is still in the current session.
        self.build(trade_records(PREVIOUS_SESSION_MS), sync(0),
                   now_ms=int(datetime(*DAY, 17, 15, tzinfo=NY).timestamp() * 1000))
        seen = self.run_publisher(LINGER_S + 40)
        self.assert_fails_throughout(seen, 'order_not_seen_yet:')

    def test_page_problem_still_fails(self):
        self.build(trade_records(PREVIOUS_SESSION_MS), sync(0))
        self.state.mark_unsynced()
        seen = self.run_publisher(LINGER_S + 40)
        after_grace = [s for s in seen if s[0] > GRACE_S + STEP_S]
        self.assertTrue(all(p == 'snapshot_publisher_failed:snapshot_unavailable' and not ok
                            for _, p, ok in after_grace))
        self.assertTrue(self.snapshot_reasons[-1].startswith('unhealthy:'))
        self.assertEqual(retired(self.publisher), set())

    def test_publisher_exception_still_fails(self):
        self.build(trade_records(PREVIOUS_SESSION_MS), sync(0))
        self.executor.snapshot = lambda trade_id: (_ for _ in ()).throw(RuntimeError('boom'))
        def no_backoff(publisher):      # the 1 s back-off after an exception is real time
            publisher.stopping.wait = lambda timeout=None: publisher.stopping.is_set()
        with self.assertLogs('tvbridge', level='ERROR'):
            seen = self.run_publisher(LINGER_S + 40, prepare=no_backoff)
        self.assertGreater(seen[-1][0], LINGER_S + 30)
        self.assertTrue(all(p == 'snapshot_publisher_failed:publisher_exception' and not ok
                            for t, p, ok in seen if t > STEP_S))
        self.assertEqual(retired(self.publisher), set())

    # --- normal behaviour unchanged ----------------------------------------------------------

    def test_same_session_restart_republishes_then_lingers_out(self):
        # A restart in the same session: the page still lists the trade's orders.
        self.build(trade_records(SAME_SESSION_MS), trade_visible_on_page())
        seen = self.run_publisher(LINGER_S + 40)
        self.assertTrue(all(problem is None and ok for _, problem, ok in seen))
        self.assertTrue(all(r is None for r in self.snapshot_reasons))
        lines = [json.loads(x) for x in self.evidence.read_text().splitlines()]
        # Published every interval inside the linger window, then stopped (linger rule).
        self.assertGreaterEqual(len(lines), LINGER_S / STEP_S - 1)
        self.assertLessEqual(len(lines), LINGER_S / STEP_S + 2)
        last = lines[-1]
        self.assertEqual(last['trade_id'], TRADE)
        self.assertEqual(last['position_qty'], 0)
        self.assertEqual({o['broker_order_id']: o['status'] for o in last['orders']},
                         {str(ENTRY_ID): 'filled', str(STOP_ID): 'cancelled', str(TP1_ID): 'filled'})
        self.assertEqual(retired(self.publisher), set())
        self.assertEqual(self.publisher.last['published'], 'evidence_only')

    def test_active_trade_republishes_every_interval(self):
        self.build(trade_records(SAME_SESSION_MS), dict(trade_visible_on_page(),
                   positions=[{'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': QTY - TP1_QTY,
                               'tradeDate': TRADE_DATE}]))
        seen = self.run_publisher(LINGER_S + 40)
        self.assertTrue(all(problem is None and ok for _, problem, ok in seen))
        lines = self.evidence.read_text().splitlines()
        self.assertEqual(len(lines), len(self.snapshot_reasons))
        self.assertGreaterEqual(len(lines), (LINGER_S + 40) / STEP_S - 1)
        self.assertEqual(json.loads(lines[-1])['position_qty'], QTY - TP1_QTY)


if __name__ == '__main__':
    unittest.main(verbosity=2)
