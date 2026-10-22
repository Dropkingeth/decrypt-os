"""The tp1_limit entry-fill wait runs before
Executor.lock, refusals are logged and kept in refusals.jsonl (never commands.jsonl), and slow broker
requests and late fills are logged. In-memory doubles only: no browser, no network, no Tradovate."""
import json
import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge import bridge as bridge_mod  # noqa: E402
from tvbridge import fills  # noqa: E402
from tvbridge.executor import REFUSALS_NAME, Executor  # noqa: E402
from tvbridge.followers import FollowerExecutor  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402

TRADE = 'fixture-stream:MNQZ2021:1613679000000:long'
ENTRY_ID = 1000001
ORDER_ID = 1000002  # every request the page double accepts
FILL_ID = 1000003
FILL_MS = 1613679002000
TDATE = {'year': 2021, 'month': 2, 'day': 18}


def iso(ms):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ms // 1000)) + '.%03dZ' % (ms % 1000)


class Page:
    """Authorized page double, one epoch; every request is accepted as order ORDER_ID."""

    def __init__(self, state):
        self.state, self.epoch, self.wire = state, 'e1', []
        state.load_sync([{'id': 1, 'name': 'FAKE'}], {
            'accounts': [{'id': 1, 'name': 'FAKE'}], 'orders': [], 'fills': [], 'orderVersions': [],
            'positions': [{'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': 0, 'tradeDate': TDATE}],
            'contracts': [{'id': 2, 'name': 'MNQZ1'}]}, self.epoch)

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5, expected_epoch=None):
        self.wire.append((endpoint, body))
        return {'ok': True, 'sent': True, 'd': {'orderId': ORDER_ID}}


def capture_logs(test):
    records = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append
    log = logging.getLogger('tvbridge')
    old = log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    test.addCleanup(log.removeHandler, handler)
    test.addCleanup(log.setLevel, old)
    return records


class Base(unittest.TestCase):
    SETTLE_S = 1.0
    PREFIX = ''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = AccountState()
        self.page = Page(self.state)
        self.journal = Journal(self.tmp.name)
        self.executor = self.make_executor({
            'ticker': 'MNQZ2021', 'account_name': 'FAKE', 'account_id': 1, 'soup_account_id': 'FAKE',
            'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0, 'entry_settle_s': self.SETTLE_S})
        self.executor.resolve()
        self.logs = capture_logs(self)
        # A filled-or-not long entry of 4, accepted by Tradovate as order ENTRY_ID.
        self.journal.start(self.PREFIX + 'entry', trade_id=TRADE, kind='entry', side='long', quantity=4,
                           at=FILL_MS - 250)
        self.journal.finish(self.PREFIX + 'entry', 'accepted', {}, str(ENTRY_ID))

    def make_executor(self, cfg):
        return Executor(self.page, self.state, self.journal, cfg)

    def entry_status(self, status):
        self.state.apply_props({'entityType': 'order', 'eventType': 'Updated', 'entity': {
            'id': ENTRY_ID, 'accountId': 1, 'contractId': 2, 'action': 'Buy', 'ordStatus': status}})

    def fill_entry(self):
        self.state.apply_props({'entityType': 'fill', 'eventType': 'Created', 'entity': {
            'id': FILL_ID, 'orderId': ENTRY_ID, 'contractId': 2, 'qty': 4, 'price': 8000.0, 'action': 'Buy',
            'timestamp': iso(FILL_MS), 'active': True}})
        self.state.apply_props({'entityType': 'position', 'eventType': 'Updated', 'entity': {
            'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': 4, 'tradeDate': TDATE}})
        self.entry_status('Filled')

    @staticmethod
    def tp1(cid='tp1', ticker='MNQZ2021'):
        return {'ticker': ticker, 'action': 'exit', 'orderType': 'limit', 'quantity': 2, 'limitPrice': 8013.25,
                'cancel': False, 'extras': {'soup_command_id': cid, 'soup_trade_id': TRADE}}

    def spy_waits(self):
        """Record (lock held?, seconds) for every entry wait."""
        calls, entered = [], threading.Event()
        original = self.executor._wait_entry_terminal

        def spy(*a, **kw):
            held = self.executor.lock.locked()
            entered.set()
            t0 = time.monotonic()
            try:
                return original(*a, **kw)
            finally:
                calls.append((held, time.monotonic() - t0))
        self.executor._wait_entry_terminal = spy
        return calls, entered

    def refusals(self):
        path = Path(self.tmp.name) / REFUSALS_NAME
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class Tp1PreWait(Base):
    def test_lock_is_free_while_tp1_waits_and_a_fill_mid_wait_is_accepted(self):
        self.entry_status('PendingNew')
        calls, entered = self.spy_waits()
        result = {}

        def run():
            t0 = time.monotonic()
            result['out'] = self.executor.send(self.tp1())
            result['s'] = time.monotonic() - t0
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(entered.wait(2))
        time.sleep(0.1)
        # The snapshot publisher (daemon.SnapshotPublisher) takes this lock: it must not wait for the TP1.
        self.assertTrue(self.executor.lock.acquire(timeout=0.2), 'executor.lock held during the TP1 wait')
        self.executor.lock.release()
        snap, _ = self.executor.snapshot(TRADE)  # builds while the TP1 waits
        self.assertIsNotNone(snap)
        self.fill_entry()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        outcome, receipt = result['out']
        self.assertEqual((outcome, receipt), ('accepted', {'broker_order_id': str(ORDER_ID), 'id': str(ORDER_ID)}))
        self.assertLess(result['s'], self.SETTLE_S)
        self.assertEqual([c[0] for c in calls], [False, True])  # pre-wait unlocked, guard under the lock
        self.assertLess(calls[1][1], 0.2)  # the guard's own wait returns at once
        self.assertEqual([e for e, _ in self.page.wire], ['order/placeorder'])
        self.assertEqual(self.page.wire[0][1]['price'], 8013.25)
        self.assertEqual(self.journal.get(self.PREFIX + 'tp1')['outcome'], 'accepted')
        self.assertEqual(self.refusals(), [])

    def test_still_pending_at_the_deadline_gives_the_same_refusal_within_one_wait(self):
        self.entry_status('PendingNew')
        calls, _ = self.spy_waits()
        t0 = time.monotonic()
        outcome, receipt = self.executor.send(self.tp1())
        elapsed = time.monotonic() - t0
        self.assertEqual((outcome, receipt), ('rejected', {
            'bridge': 'refused', 'reason': 'entry not completely filled (PendingNew, 0/4)'}))
        self.assertGreaterEqual(elapsed, self.SETTLE_S - 0.05)
        self.assertLess(elapsed, self.SETTLE_S + 0.4)  # one shared deadline, not two waits
        self.assertEqual([c[0] for c in calls], [False, True])
        self.assertLess(calls[1][1], 0.2)  # under the lock: one final read
        self.assertEqual(self.page.wire, [])
        self.assertIsNone(self.journal.get(self.PREFIX + 'tp1'))

    def test_position_update_trailing_the_fill_is_waited_for(self):
        # Tradovate sends the fill, the order update and the position update as separate events:
        # the entry can show Filled while the position still says flat.
        self.state.apply_props({'entityType': 'fill', 'eventType': 'Created', 'entity': {
            'id': FILL_ID, 'orderId': ENTRY_ID, 'contractId': 2, 'qty': 4, 'price': 8000.0, 'action': 'Buy',
            'timestamp': iso(FILL_MS), 'active': True}})
        self.entry_status('Filled')

        def position():
            time.sleep(0.3)
            self.state.apply_props({'entityType': 'position', 'eventType': 'Updated', 'entity': {
                'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': 4, 'tradeDate': TDATE}})
        threading.Thread(target=position).start()
        outcome, receipt = self.executor.send(self.tp1())
        self.assertEqual((outcome, receipt), ('accepted', {'broker_order_id': str(ORDER_ID), 'id': str(ORDER_ID)}))
        self.assertEqual([e for e, _ in self.page.wire], ['order/placeorder'])

    def test_entry_not_seen_at_all_is_refused_as_before(self):
        outcome, receipt = self.executor.send(self.tp1())
        self.assertEqual((outcome, receipt), ('rejected', {
            'bridge': 'refused', 'reason': 'entry not completely filled (None, 0/4)'}))

    def test_rejected_entry_is_refused_at_once(self):
        self.entry_status('Rejected')
        t0 = time.monotonic()
        outcome, receipt = self.executor.send(self.tp1())
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertEqual((outcome, receipt), ('rejected', {
            'bridge': 'refused', 'reason': 'entry not completely filled (Rejected, 0/4)'}))
        self.assertEqual(self.page.wire, [])

    def test_cancelled_and_expired_entries_are_refused_at_once(self):
        for status in ('Canceled', 'Expired'):
            self.entry_status(status)
            t0 = time.monotonic()
            outcome, receipt = self.executor.send(self.tp1(cid='tp1-' + status))
            self.assertLess(time.monotonic() - t0, 0.3)
            self.assertEqual((outcome, receipt['reason']),
                             ('rejected', f'entry not completely filled ({status}, 0/4)'))

    def test_filled_entry_is_accepted_without_waiting(self):
        self.fill_entry()
        t0 = time.monotonic()
        self.assertEqual(self.executor.send(self.tp1())[0], 'accepted')
        self.assertLess(time.monotonic() - t0, 0.3)

    def test_no_pre_wait_for_other_kinds_or_while_stopping(self):
        calls, _ = self.spy_waits()
        self.entry_status('PendingNew')
        market = dict(self.tp1(cid='mx'), orderType='market')
        market.pop('limitPrice')
        self.assertEqual(self.executor.send(market)[0], 'rejected')
        self.assertEqual(calls, [])
        self.page.stop_reason = 'test'
        t0 = time.monotonic()
        self.assertEqual(self.executor.send(self.tp1())[1]['reason'], 'daemon_stopping:test')
        self.assertLess(time.monotonic() - t0, 0.3)

    def test_unhealthy_page_refuses_tp1_at_once(self):
        self.entry_status('PendingNew')
        self.page.status = lambda: {'state': 'authorized', 'epoch': 'e1', 'lastRxAgoMs': 60000, 'url': 'fake'}
        t0 = time.monotonic()
        outcome, receipt = self.executor.send(self.tp1())
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertEqual(receipt['reason'], 'unhealthy:tradovate_silent')

    def test_duplicate_tp1_is_answered_from_the_journal_without_waiting(self):
        self.fill_entry()
        self.assertEqual(self.executor.send(self.tp1())[0], 'accepted')
        self.entry_status('PendingNew')  # would wait if the pre-wait ran
        t0 = time.monotonic()
        outcome, receipt = self.executor.send(self.tp1())
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertEqual((outcome, receipt.get('duplicate')), ('accepted', True))
        self.assertEqual(len(self.page.wire), 1)


class FollowerTp1PreWait(Tp1PreWait):
    """FollowerExecutor inherits send(): same unlocked wait, namespaced command ids, own journal."""
    PREFIX = 'f:ab:'

    def make_executor(self, cfg):
        return FollowerExecutor(self.page, self.state, self.journal, dict(cfg, follower_key='ab'))

    def test_follower_refusal_is_recorded_with_its_namespaced_id(self):
        self.entry_status('Rejected')
        self.executor.send(self.tp1())
        self.assertEqual([(r['command_id'], r['kind']) for r in self.refusals()], [('f:ab:tp1', 'tp1_limit')])


class Refusals(Base):
    SETTLE_S = 0.2

    def test_refusal_is_logged_and_kept_in_refusals_jsonl_not_commands_jsonl(self):
        self.entry_status('Rejected')
        before = (Path(self.tmp.name) / 'commands.jsonl').read_bytes()
        self.executor.send(self.tp1())
        self.assertEqual((Path(self.tmp.name) / 'commands.jsonl').read_bytes(), before)
        self.assertIsNone(self.journal.get('tp1'))
        recs = self.refusals()
        self.assertEqual(len(recs), 1)
        self.assertEqual(set(recs[0]), {'at', 'command_id', 'trade_id', 'kind', 'reason'})
        self.assertEqual((recs[0]['command_id'], recs[0]['trade_id'], recs[0]['kind'], recs[0]['reason']),
                         ('tp1', TRADE, 'tp1_limit', 'entry not completely filled (Rejected, 0/4)'))
        self.assertIsInstance(recs[0]['at'], int)
        lines = [r.getMessage() for r in self.logs if r.levelno == logging.WARNING and 'refused' in r.getMessage()]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'^refused tp1 \(tp1_limit\) after \d+ ms: '
                                   r'entry not completely filled \(Rejected, 0/4\)$')
        # A reloaded journal (snapshots, fill capture, duplicate detection) does not see it.
        self.assertIsNone(Journal(self.tmp.name).get('tp1'))

    def test_parse_and_health_refusals_are_recorded_too(self):
        self.executor.send(self.tp1(cid='bad', ticker='ESZ2021'))
        self.page.status = lambda: {'state': 'loading', 'epoch': 'e1', 'lastRxAgoMs': 0, 'url': 'fake'}
        self.executor.send(self.tp1(cid='sick'))
        self.assertEqual([(r['command_id'], r['kind'], r['reason']) for r in self.refusals()], [
            ('bad', None, 'ticker is not the pinned ticker'),
            ('sick', 'tp1_limit', 'unhealthy:page_state:loading')])

    def test_accepted_tradovate_rejected_and_duplicates_are_not_refusals(self):
        self.fill_entry()
        self.executor.send(self.tp1())
        self.executor.send(self.tp1())
        self.page.request = lambda *a, **k: {'ok': True, 'sent': True, 's': 200,
                                             'd': {'failureReason': 'UnknownReason', 'failureText': 'no'}}
        outcome, receipt = self.executor.send(self.tp1(cid='tp1-b'))
        self.assertEqual((outcome, receipt.get('bridge')), ('rejected', None))
        self.assertEqual(self.refusals(), [])

    def test_unwritable_refusal_file_never_changes_the_answer(self):
        self.entry_status('Rejected')
        (Path(self.tmp.name) / REFUSALS_NAME).mkdir()  # a directory: the append fails
        outcome, receipt = self.executor.send(self.tp1())
        self.assertEqual((outcome, receipt), ('rejected', {
            'bridge': 'refused', 'reason': 'entry not completely filled (Rejected, 0/4)'}))
        self.assertTrue(any('refusal record not written' in r.getMessage() for r in self.logs))

    def test_odd_payloads_are_recorded_without_raising(self):
        for payload in (None, [], {'extras': 'x'}, {'ticker': 'MNQZ2021', 'extras': {'soup_command_id': 5}}):
            outcome, _ = self.executor.send(payload)
            self.assertEqual(outcome, 'rejected')
        self.assertEqual([r['command_id'] for r in self.refusals()], [None, None, None, None])


class SlowRequestLog(unittest.TestCase):
    class Session:
        def __init__(self, delay):
            self.delay = delay

        def evaluate(self, expr, timeout=None):
            time.sleep(self.delay)
            return {'ok': True, 'sent': True, 'd': {'orderId': ORDER_ID}}

    def bridge(self, delay):
        b = bridge_mod.Bridge.__new__(bridge_mod.Bridge)
        b.session, b.api, b.read_only, b._detached = self.Session(delay), 'api', False, None
        b.lease_lost = threading.Event()
        return b

    def test_slow_round_trip_logs_endpoint_and_ms_only(self):
        logs = capture_logs(self)
        with patch.object(bridge_mod, 'SLOW_REQUEST_S', 0.05):
            r = self.bridge(0.1).request('order/placeorder', {'secret': 'BODY-TOKEN'})
        self.assertEqual(r, {'ok': True, 'sent': True, 'd': {'orderId': ORDER_ID}})
        lines = [x.getMessage() for x in logs if x.levelno == logging.WARNING]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'^slow broker request order/placeorder: \d+ ms$')
        self.assertNotIn('BODY-TOKEN', lines[0])

    def test_fast_round_trip_and_bad_inputs_log_nothing_and_never_raise(self):
        logs = capture_logs(self)
        self.bridge(0).request('order/placeorder', {})
        bridge_mod._note_slow_request(object(), 'not a number')
        self.assertEqual([x for x in logs if x.levelno >= logging.WARNING], [])

    def test_default_threshold_is_one_second(self):
        self.assertEqual(bridge_mod.SLOW_REQUEST_S, 1.0)


class FillLagLog(Base):
    def capture(self, observed_at, timestamp=iso(FILL_MS)):
        self.entry_status('Filled')
        self.state.apply_props({'entityType': 'fill', 'eventType': 'Created', 'entity': {
            'id': FILL_ID, 'orderId': ENTRY_ID, 'contractId': 2, 'qty': 4, 'price': 8000.0, 'action': 'Buy',
            'timestamp': timestamp, 'active': True}})
        rec = fills.FillRecorder(self.executor, 'main', clock_ms=lambda: observed_at)
        self.assertEqual(rec.capture(), 1)
        return [x.getMessage() for x in self.logs if x.levelno == logging.WARNING and 'fill capture' in x.getMessage()]

    def test_late_fill_is_logged(self):
        # A fill recorded 2,500 ms after its fill time.
        self.assertEqual(self.capture(FILL_MS + 2500),
                         ['fill capture main: fill %d observed 2500 ms after its fill time' % FILL_ID])

    def test_on_time_fill_is_not_logged(self):
        self.assertEqual(self.capture(FILL_MS + 1500), [])

    def test_fill_without_a_time_is_recorded_without_a_log(self):
        self.assertEqual(self.capture(FILL_MS + 9000, timestamp=None), [])

    def test_threshold(self):
        self.assertEqual(fills.SLOW_FILL_MS, 1500)


if __name__ == '__main__':
    unittest.main()
