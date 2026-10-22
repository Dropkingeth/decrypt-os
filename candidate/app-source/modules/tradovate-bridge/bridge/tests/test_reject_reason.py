"""Tradovate's rejection text for a rejected order (observability only).

The bridge keeps Tradovate's rejection reason and surfaces it in the snapshot, receipts, follower
view, firing note and one log line only when Tradovate sent it. A risk rejection arrives on the
bridge's socket as one frame: order Created (PendingNew), command Created (id == order id),
orderVersion, commandReport {commandStatus RiskRejected, rejectReason, text}, command Updated,
order Updated (Rejected). Without a reason, every output matches the baseline that has no
`reject_reason` key. Synthetic fixture with in-memory doubles only: no browser, no network,
no Tradovate.
"""
import copy
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tvbridge import follower_native, followers  # noqa: E402
from tvbridge import state as state_mod  # noqa: E402
from tvbridge.executor import Executor, Refused, client_tag  # noqa: E402
from tvbridge.firing import AlertLog, FollowerLane  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.state import AccountState, reject_text  # noqa: E402
from harness import seed_market_price  # noqa: E402

# Synthetic fixture: a short bracket entry of 5 contracts, rejected 0/5 with its stop leg.
SIGNAL_MS = 1636644600000  # 2021-11-11T15:30:00Z
TRADE = 'fixture-stream:MNQZ2021:%d:short' % SIGNAL_MS
NEXT_TRADE = 'fixture-stream:MNQZ2021:%d:short' % (SIGNAL_MS + 600000)
ENTRY = 'soup-00000000000000000000000000000001'
TP1 = 'soup-00000000000000000000000000000002'
ENTRY_ID, STOP_ID = 1000001, 1000002
QTY, TP1_QTY = 5, 2
REFERENCE, STOP_PRICE, TP1_PRICE, TP1_LIMIT, BREAK_EVEN = 12000.0, 12006.0, 11985.0, 11985.5, 11996.0
STARTED = SIGNAL_MS + 2000
OBSERVED = SIGNAL_MS + 4000
ORDER_TS, REPORT_TS = '2021-11-11T15:30:02.250Z', '2021-11-11T15:30:02.500Z'
# Baseline snapshot of the rejected entry without a reason (Publisher.publish's bytes).
BASELINE_SNAPSHOT = (
    '{"account_id":"Account A","event_id":"tvbridge-1","observed_at":1636644604000,"open_orders":[],'
    '"orders":[{"broker_order_id":"1000001","command_id":"soup-00000000000000000000000000000001",'
    '"filled_qty":0,"role":"entry","status":"rejected"},{"broker_order_id":"1000002",'
    '"command_id":"soup-00000000000000000000000000000001#stop","filled_qty":0,'
    '"parent_broker_order_id":"1000001","quantity":5,"role":"stop","status":"rejected",'
    '"stop_price":12006.0}],"position_qty":0,"sequence":1,"side":"short","symbol":"MNQZ2021",'
    '"trade_id":"fixture-stream:MNQZ2021:1636644600000:short"}')
# The journal records of that entry (commands.jsonl).
ENTRY_JOURNAL = [
    {"at": STARTED, "command_id": ENTRY, "exit_action": "Buy", "kind": "bracket_entry", "quantity": QTY,
     "reference_price": REFERENCE, "side": "short", "stop_price": STOP_PRICE, "tag": client_tag(ENTRY),
     "tp1_leg": None, "tp1_price": None, "tp1_quantity": None, "trade_id": TRADE, "type": "start"},
    {"broker_order_id": str(ENTRY_ID), "command_id": ENTRY, "legs": {"stop": str(STOP_ID)}, "outcome": "accepted",
     "receipt": {"broker_order_id": str(ENTRY_ID), "id": str(ENTRY_ID), "legs": {"stop": str(STOP_ID)},
                 "legs_from": {"stop": "reply"}}, "type": "result"},
]
BRACKET_ID = 1000011
FOLLOWER_ENTRY_ID, FOLLOWER_STOP_ID = 1000101, 1000102
NOT_FILLED = 'entry not completely filled (Rejected, 0/%d)' % QTY
# A synthetic risk rejection (code and text).
RISK_CODE = 'RiskLimitExceeded'
RISK_TEXT = 'Order rejected by a synthetic risk check.'
RISK_REASON = RISK_CODE + ': ' + RISK_TEXT
STOP_REASON = 'ParentRejected'
CONFIG = {'ticker': 'MNQZ2021', 'account_name': 'FAKE', 'account_id': 1, 'soup_account_id': 'Account A',
          'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0, 'entry_settle_s': 0, 'terminal_settle_ms': 0}


def props(kind, event, entity):
    return {'entityType': kind, 'eventType': event, 'entity': entity}


def order(oid, status, account=1):
    return {'id': oid, 'accountId': account, 'contractId': 2, 'action': 'Sell', 'ordStatus': status,
            'timestamp': ORDER_TS}


def command(cid, oid, ctype='New', status='Pending'):
    return {'id': cid, 'orderId': oid, 'timestamp': ORDER_TS, 'commandType': ctype,
            'commandStatus': status, 'senderId': 9}


def command_report(rid, cid, status='RiskRejected', code=RISK_CODE, text=RISK_TEXT):
    rep = {'id': rid, 'commandId': cid, 'timestamp': REPORT_TS, 'commandStatus': status}
    if code is not None:
        rep['rejectReason'] = code
    if text is not None:
        rep['text'] = text
    return rep


def risk_reject_sequence(oid, rid, code=RISK_CODE, text=RISK_TEXT, account=1, qty=QTY):
    """The frame Tradovate sends for one risk rejection."""
    return [props('order', 'Created', order(oid, 'PendingNew', account)),
            props('command', 'Created', command(oid, oid)),
            props('orderVersion', 'Created', {'id': oid, 'orderId': oid, 'orderQty': qty, 'orderType': 'Market'}),
            props('commandReport', 'Created', command_report(rid, oid, code=code, text=text)),
            props('command', 'Updated', command(oid, oid, status='RiskRejected')),
            props('order', 'Updated', order(oid, 'Rejected', account))]


def base_sync(orders=(), versions=(), extra=None):
    data = {'accounts': [{'id': 1, 'name': 'FAKE'}, {'id': 7, 'name': 'OTHER'}], 'orders': list(orders),
            'fills': [], 'positions': [{'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': 0,
                                        'tradeDate': {'year': 2021, 'month': 11, 'day': 11}}],
            'orderVersions': list(versions), 'contracts': [{'id': 2, 'name': 'MNQZ1'}]}
    data.update(extra or {})
    return data


def rejected_orders():
    return [order(ENTRY_ID, 'Rejected'), order(STOP_ID, 'Rejected')]


def rejected_versions():
    return [{'id': ENTRY_ID, 'orderId': ENTRY_ID, 'orderQty': QTY, 'orderType': 'Market'},
            {'id': STOP_ID, 'orderId': STOP_ID, 'orderQty': QTY, 'orderType': 'Stop', 'stopPrice': STOP_PRICE}]


def rejected_reports():
    """Sync collections as Tradovate holds them after the rejection (entry and its stop leg)."""
    return {'commands': [command(ENTRY_ID, ENTRY_ID, status='RiskRejected'),
                         command(STOP_ID, STOP_ID, status='RiskRejected')],
            'commandReports': [command_report(ENTRY_ID + 10, ENTRY_ID),
                               command_report(STOP_ID + 10, STOP_ID, code=STOP_REASON, text=None)],
            'executionReports': []}


class Page:
    """Loopback page double: authorized, one epoch; records requests and answers with `reply`."""

    def __init__(self, state, data, reply=None):
        self.state, self.epoch, self.wire = state, 'live', []
        self.reply = reply or {'ok': True, 'sent': True, 's': 200, 'd': {'orderId': 10}}
        state.load_sync(data['accounts'], data, self.epoch)

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5, expected_epoch=None):
        self.wire.append((endpoint, body))
        reply = self.reply(endpoint, body) if callable(self.reply) else self.reply
        return copy.deepcopy(reply)


def capture(test, level=logging.INFO):
    records = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append
    log = logging.getLogger('tvbridge')
    old = log.level
    log.addHandler(handler)
    log.setLevel(level)
    test.addCleanup(log.removeHandler, handler)
    test.addCleanup(log.setLevel, old)
    return records


def reject_lines(records):
    return [r.getMessage() for r in records if r.getMessage().startswith('Tradovate rejected order')]


# ----------------------------------------------------------------------------------------
# Sanitizing
# ----------------------------------------------------------------------------------------

class RejectText(unittest.TestCase):
    def test_code_and_text(self):
        self.assertEqual(reject_text(RISK_CODE, RISK_TEXT), RISK_REASON)

    def test_unknown_reason_code_gives_way_to_the_text(self):
        self.assertEqual(reject_text('UnknownReason', 'Order price is outside the price band'),
                         'Order price is outside the price band')
        self.assertEqual(reject_text('UnknownReason', ''), 'UnknownReason')

    def test_code_alone_text_alone_and_nothing(self):
        self.assertEqual(reject_text('MaxPosLimitReached', None), 'MaxPosLimitReached')
        self.assertEqual(reject_text(None, 'Account is locked'), 'Account is locked')
        self.assertEqual(reject_text('same', 'same'), 'same')
        for code, text in ((None, None), ('', ''), ('Success', None), ('Success', '  '), (3, {'x': 1}),
                           (['a'], b'bytes'), ('\n\t', '\x00\x07')):
            self.assertIsNone(reject_text(code, text), (code, text))

    def test_control_characters_and_whitespace_collapse_to_one_line(self):
        self.assertEqual(reject_text('RiskCheck', 'line one\r\nline\ttwo\x00\x1b[31m  end x'),
                         'RiskCheck: line one line two [31m end x')

    def test_bounded_to_300_characters(self):
        out = reject_text('UnknownReason', 'x' * 10_000)
        self.assertEqual((len(out), out[-3:]), (300, '...'))
        out = reject_text('C' * 400, 'y' * 400)
        self.assertLessEqual(len(out), state_mod.REJECT_REASON_MAX)
        self.assertEqual(len(reject_text(None, 'z' * 300)), 300)
        self.assertEqual(reject_text(None, 'z' * 300), 'z' * 300)


# ----------------------------------------------------------------------------------------
# Account state
# ----------------------------------------------------------------------------------------

class StateCapture(unittest.TestCase):
    def fresh(self, data=None):
        st = AccountState(clock=lambda: OBSERVED / 1000)
        st.load_sync((data or base_sync())['accounts'], data or base_sync(), 'e1')
        return st

    def test_risk_reject_sequence_records_the_reason(self):
        st = self.fresh()
        for p in risk_reject_sequence(ENTRY_ID, ENTRY_ID + 1):
            st.apply_props(p)
        self.assertEqual(st.reject_reason(ENTRY_ID), RISK_REASON)
        self.assertTrue(st.synced)

    def test_trading_state_and_version_are_exactly_as_without_the_reports(self):
        with_reports, without = self.fresh(), self.fresh()
        for p in risk_reject_sequence(ENTRY_ID, ENTRY_ID + 1):
            with_reports.apply_props(p)
            if p['entityType'] not in state_mod.REPORT_KINDS:
                without.apply_props(p)
        self.assertEqual(with_reports.version, without.version)
        self.assertEqual(with_reports.entities, without.entities)
        self.assertEqual(with_reports.terminal_seen_ms, without.terminal_seen_ms)
        self.assertEqual(with_reports.anomalies, without.anomalies)
        self.assertIsNone(without.reject_reason(ENTRY_ID))

    def test_one_log_line_per_rejected_order_with_account_and_text(self):
        records = capture(self)
        st = self.fresh()
        for p in risk_reject_sequence(ENTRY_ID, ENTRY_ID + 1):
            st.apply_props(p)
        st.apply_props(props('order', 'Updated', order(ENTRY_ID, 'Rejected')))  # repeated update
        self.assertEqual(reject_lines(records),
                         ['Tradovate rejected order %d (account 1): %s' % (ENTRY_ID, RISK_REASON)])

    def test_report_after_the_status_still_logs_once(self):
        records = capture(self)
        st = self.fresh()
        st.apply_props(props('order', 'Created', order(ENTRY_ID, 'PendingNew', account=7)))
        st.apply_props(props('order', 'Updated', order(ENTRY_ID, 'Rejected', account=7)))
        self.assertEqual(reject_lines(records), [])  # no text yet: nothing to say
        st.apply_props(props('commandReport', 'Created', command_report(ENTRY_ID + 1, ENTRY_ID)))
        st.apply_props(props('command', 'Created', command(ENTRY_ID, ENTRY_ID)))
        self.assertEqual(reject_lines(records),
                         ['Tradovate rejected order %d (account 7): %s' % (ENTRY_ID, RISK_REASON)])
        self.assertEqual(st.reject_reason(ENTRY_ID), RISK_REASON)

    def test_execution_report_rejection(self):
        st = self.fresh()
        st.apply_props(props('order', 'Created', order(ENTRY_ID, 'Rejected')))
        st.apply_props(props('executionReport', 'Created', {
            'id': 99, 'commandId': ENTRY_ID, 'orderId': ENTRY_ID, 'accountId': 1, 'contractId': 2,
            'execType': 'Rejected', 'ordStatus': 'Rejected', 'action': 'Sell', 'rejectReason': 'OtherExecutionRelated',
            'text': 'Exchange rejected: price outside limits', 'timestamp': REPORT_TS}))
        self.assertEqual(st.reject_reason(ENTRY_ID), 'OtherExecutionRelated: Exchange rejected: price outside limits')

    def test_non_rejections_are_ignored(self):
        st = self.fresh()
        oid = ENTRY_ID
        for p in [props('order', 'Created', order(oid, 'PendingNew')), props('command', 'Created', command(oid, oid)),
                  props('commandReport', 'Created', command_report(oid + 1, oid, 'RiskPassed', None, None)),
                  props('commandReport', 'Created', command_report(oid + 2, oid, 'AtExecution', None, None)),
                  props('executionReport', 'Created', {'id': oid + 2, 'commandId': oid, 'orderId': oid, 'execType': 'New',
                                                       'ordStatus': 'Working', 'text': 'not a rejection'}),
                  props('executionReport', 'Created', {'id': oid + 3, 'commandId': oid, 'orderId': oid,
                                                       'execType': 'Trade', 'ordStatus': 'Filled', 'cumQty': QTY})]:
            st.apply_props(p)
        self.assertIsNone(st.reject_reason(oid))

    def test_a_rejected_cancel_or_modify_is_not_the_orders_rejection(self):
        records = capture(self)
        st = self.fresh()
        oid, cancel, modify = ENTRY_ID, ENTRY_ID + 5, ENTRY_ID + 7
        st.apply_props(props('order', 'Created', order(oid, 'Rejected')))
        st.apply_props(props('command', 'Created', command(oid, oid)))
        st.apply_props(props('commandReport', 'Created', command_report(cancel + 1, cancel, 'ExecutionRejected',
                                                                        'TooLate', 'Too late to cancel')))
        st.apply_props(props('command', 'Created', command(cancel, oid, ctype='Cancel')))
        st.apply_props(props('command', 'Created', command(modify, oid, ctype='Modify')))
        st.apply_props(props('commandReport', 'Created', command_report(modify + 1, modify, 'RiskRejected',
                                                                        'RiskCheck', 'Modify refused')))
        self.assertIsNone(st.reject_reason(oid))
        self.assertEqual(reject_lines(records), [])

    def test_sync_collections_rebuild_the_reason_after_a_reconnect(self):
        st = self.fresh(base_sync(rejected_orders(), rejected_versions(), rejected_reports()))
        self.assertEqual((st.reject_reason(ENTRY_ID), st.reject_reason(STOP_ID)), (RISK_REASON, STOP_REASON))
        st.load_sync(base_sync()['accounts'], base_sync(rejected_orders(), rejected_versions()), 'e2')
        self.assertIsNone(st.reject_reason(ENTRY_ID))  # a sync replaces it, like every entity

    def test_sync_without_logging(self):
        records = capture(self)
        self.fresh(base_sync(rejected_orders(), rejected_versions(), rejected_reports()))
        self.assertEqual(reject_lines(records), [])

    def test_malformed_sync_reports_never_fail_the_sync_or_change_trading_state(self):
        bad = {'commands': [None, 5, {'id': 'x'}, {'id': -1, 'orderId': 1}, {'id': 4, 'orderId': 'y'}],
               'commandReports': [{'id': 1}, {'id': 2, 'commandId': None, 'commandStatus': 'RiskRejected', 'text': 'a'},
                                  {'id': 3, 'commandId': ENTRY_ID, 'commandStatus': 'RiskRejected',
                                   'rejectReason': {'nested': 1}, 'text': ['list']}, 'junk'],
               'executionReports': {'not': 'a list'}}
        good = self.fresh(base_sync(rejected_orders(), rejected_versions()))
        st = self.fresh(base_sync(rejected_orders(), rejected_versions(), bad))
        self.assertTrue(st.synced)
        self.assertEqual(st.entities, good.entities)
        self.assertIsNone(st.reject_reason(ENTRY_ID))

    def test_malformed_report_props_never_raise_unsync_or_bump_the_version(self):
        st = self.fresh()
        version = st.version
        for entity in (None, 'x', [], {}, {'id': 0}, {'id': True}, {'id': 5, 'commandId': 'a'},
                       {'id': 6, 'commandId': 1, 'commandStatus': 'RiskRejected', 'text': 7, 'rejectReason': None},
                       {'id': 7, 'orderId': -3, 'execType': 'Rejected', 'text': 'x'}):
            for kind in sorted(state_mod.REPORT_KINDS):
                st.apply_props(props(kind, 'Created', entity))
                st.apply_props(props(kind, 'Deleted', entity))
        self.assertTrue(st.synced)
        self.assertEqual(st.version, version)

    def test_memory_is_bounded(self):
        st = self.fresh()
        for i in range(1, 3 * state_mod._REPORTS_MAX):
            st.apply_props(props('command', 'Created', command(10 * i, 10 * i)))
            st.apply_props(props('commandReport', 'Created', command_report(10 * i + 1, 10 * i)))
            st.apply_props(props('executionReport', 'Created', {'id': 10 * i + 2, 'orderId': 10 * i,
                                                                'execType': 'Rejected', 'text': 't'}))
        self.assertLessEqual(max(len(st._commands), len(st._command_rejects), len(st._execution_rejects)),
                             state_mod._REPORTS_MAX)
        last = 10 * (3 * state_mod._REPORTS_MAX - 1)
        self.assertEqual(st.reject_reason(last), 't')  # the latest report (the executionReport) wins
        self.assertIsNone(st.reject_reason(10))  # dropped first


# ----------------------------------------------------------------------------------------
# Snapshot to Soup: the rejected trade
# ----------------------------------------------------------------------------------------

class RejectedSnapshot(unittest.TestCase):
    def build(self, reports=None, journal=None, reply=None, orders=None, versions=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name) / 'journal'
        d.mkdir()
        (d / 'commands.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in (ENTRY_JOURNAL if journal is None else journal)))
        (d / 'snapshot-sequence').write_text('0')
        self.journal_bytes = (d / 'commands.jsonl').read_bytes()
        self.journal_path = d / 'commands.jsonl'
        self.journal = Journal(d)
        self.state = AccountState(clock=lambda: OBSERVED / 1000)
        data = base_sync(rejected_orders() if orders is None else orders, rejected_versions() if versions is None else versions,
                         reports)
        self.page = Page(self.state, data, reply)
        self.executor = Executor(self.page, self.state, self.journal,
                                 dict(CONFIG, kill_switch_file=str(d / 'ENTRIES-DISABLED')), clock_ms=lambda: OBSERVED)
        self.executor.resolve()
        seed_market_price(self.executor, REFERENCE)

    def published(self):
        snap, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(reason)
        return json.dumps(snap, sort_keys=True, separators=(',', ':'))  # Publisher.publish's bytes

    def test_without_a_reason_the_snapshot_matches_the_baseline(self):
        self.build()
        self.assertEqual(self.published(), BASELINE_SNAPSHOT)

    def test_with_the_reason_only_reject_reason_is_added(self):
        self.build(rejected_reports())
        body = self.published()
        snap = json.loads(body)
        self.assertEqual([o.get('reject_reason') for o in snap['orders']], [RISK_REASON, STOP_REASON])
        for o in snap['orders']:
            del o['reject_reason']
        self.assertEqual(json.dumps(snap, sort_keys=True, separators=(',', ':')), BASELINE_SNAPSHOT)
        self.assertEqual(self.journal_path.read_bytes(), self.journal_bytes)

    def test_soup_adapter_payload_keeps_every_existing_key_in_place(self):
        self.build(rejected_reports())
        snap, _ = self.executor.snapshot(TRADE)
        baseline = json.loads(BASELINE_SNAPSHOT)
        for new, old in zip(snap['orders'], baseline['orders']):
            self.assertEqual(list(new)[:len(old)], [k for k in list(new) if k in old])  # existing keys first
            self.assertEqual({k: new[k] for k in old}, old)
            self.assertEqual(set(new) - set(old), {'reject_reason'})

    def test_only_rejected_orders_carry_it(self):
        # A cancelled order with a stray rejection report (e.g. its cancel was refused) gets nothing.
        self.build(rejected_reports(), orders=[order(ENTRY_ID, 'Rejected'), order(STOP_ID, 'Canceled')])
        snap = json.loads(self.published())
        self.assertEqual([('reject_reason' in o, o['status']) for o in snap['orders']],
                         [(True, 'rejected'), (False, 'cancelled')])

    def test_a_failing_lookup_never_touches_the_snapshot(self):
        self.build(rejected_reports())

        def boom(oid):
            raise RuntimeError('lookup failed')
        self.state.reject_reason = boom
        self.assertEqual(self.published(), BASELINE_SNAPSHOT)
        self.build(rejected_reports())
        self.state.reject_reason = lambda oid: 12345  # not text
        self.assertEqual(self.published(), BASELINE_SNAPSHOT)


# ----------------------------------------------------------------------------------------
# Receipts to Soup (and to the firing service)
# ----------------------------------------------------------------------------------------

def entry_payload(cid='soup-new-entry', tid=NEXT_TRADE, qty=QTY):
    return {'ticker': 'MNQZ2021', 'action': 'sell', 'orderType': 'market', 'quantity': qty,
            'extras': {'soup_command_id': cid, 'soup_trade_id': tid}}


def tp1_payload(cid=TP1, tid=TRADE, qty=TP1_QTY, price=TP1_LIMIT):
    return {'ticker': 'MNQZ2021', 'action': 'exit', 'orderType': 'limit', 'quantity': qty, 'limitPrice': price,
            'cancel': False, 'extras': {'soup_command_id': cid, 'soup_trade_id': tid}}


def bracket_payload(cid='soup-bracket', tid=NEXT_TRADE):
    return {'ticker': 'MNQZ2021', 'action': 'sell', 'orderType': 'market', 'quantity': QTY,
            'extras': {'soup_command_id': cid, 'soup_trade_id': tid, 'bridge_kind': 'bracket_entry',
                       'reference_price': REFERENCE, 'stop_price': STOP_PRICE}}


REFUSAL = {'ok': True, 'sent': True, 's': 200,
           'd': {'failureReason': 'UnknownReason', 'failureText': 'Order quantity exceeds the account\nlimit\x00'}}


class Receipts(RejectedSnapshot):
    def test_definite_refusal_gains_reject_reason_and_nothing_else_changes(self):
        self.build(journal=[], orders=[], versions=[], reply=REFUSAL)
        outcome, receipt = self.executor.send(entry_payload())
        self.assertEqual(outcome, 'rejected')
        self.assertEqual(receipt, {'code': 'None', 's': 200, 'failureReason': 'UnknownReason',
                                   'failureText': 'Order quantity exceeds the account\nlimit\x00',
                                   'reject_reason': 'Order quantity exceeds the account limit'})

    def test_code_only_refusal(self):
        self.build(journal=[], orders=[], versions=[],
                   reply={'ok': True, 'sent': True, 's': 200, 'd': {'failureReason': 'TradingLocked'}})
        outcome, receipt = self.executor.send(entry_payload())
        self.assertEqual((outcome, receipt['reject_reason'], receipt['failureText']), ('rejected', 'TradingLocked', ''))

    def test_refusal_without_tradovate_text_is_unchanged(self):
        self.build(journal=[], orders=[], versions=[], reply={'ok': False, 'sent': True, 's': 400, 'code': None})
        outcome, receipt = self.executor.send(entry_payload())
        self.assertEqual((outcome, receipt), ('rejected', {'code': 'None', 's': 400, 'failureReason': None,
                                                           'failureText': None}))

    def test_accepted_receipt_is_unchanged(self):
        self.build(journal=[], orders=[], versions=[])
        self.assertEqual(self.executor.send(entry_payload()), ('accepted', {'broker_order_id': '10', 'id': '10'}))

    def test_bridge_refusals_are_unchanged(self):
        self.build(journal=[], orders=[], versions=[])
        self.assertEqual(self.executor.send({'ticker': 'ESZ2021'}),
                         ('rejected', {'bridge': 'refused', 'reason': 'ticker is not the pinned ticker'}))
        e = Refused('plain')
        self.assertEqual((str(e), e.extra), ('plain', {}))

    def test_tp1_refused_after_the_rejection_carries_the_entrys_reason(self):
        self.build(rejected_reports())
        outcome, receipt = self.executor.send(tp1_payload())
        self.assertEqual((outcome, receipt), ('rejected', {
            'bridge': 'refused', 'reason': NOT_FILLED,
            'entry_reject_reason': RISK_REASON}))
        self.assertEqual(self.page.wire, [])

    def test_tp1_refused_without_a_reason_is_the_baseline_receipt(self):
        self.build()
        self.assertEqual(self.executor.send(tp1_payload()), ('rejected', {
            'bridge': 'refused', 'reason': NOT_FILLED}))

    def test_bracket_entry_rejected_before_its_legs_appear(self):
        def reply(endpoint, body):
            # orderId only (no oso ids); the order is already Rejected on the page with its report.
            self.state.apply_props(props('order', 'Created', order(BRACKET_ID, 'PendingNew')))
            self.state.apply_props(props('command', 'Created', command(BRACKET_ID, BRACKET_ID)))
            self.state.apply_props(props('commandReport', 'Created', command_report(BRACKET_ID + 1, BRACKET_ID)))
            self.state.apply_props(props('order', 'Updated', order(BRACKET_ID, 'Rejected')))
            return {'ok': True, 'sent': True, 's': 200, 'd': {'orderId': BRACKET_ID}}
        self.build(journal=[], orders=[], versions=[], reply=reply)
        self.executor.cfg['bracket_orders_enabled'] = True
        outcome, receipt = self.executor.send(bracket_payload())
        self.assertEqual((outcome, receipt), ('accepted', {
            'broker_order_id': str(BRACKET_ID), 'id': str(BRACKET_ID), 'legs': {}, 'legs_from': {}, 'legs_missing': ['stop'],
            'entry_status': 'Rejected', 'reject_reason': RISK_REASON}))

    def test_bracket_entry_rejected_without_a_reason_is_unchanged(self):
        def reply(endpoint, body):
            self.state.apply_props(props('order', 'Created', order(BRACKET_ID, 'Rejected')))
            return {'ok': True, 'sent': True, 's': 200, 'd': {'orderId': BRACKET_ID}}
        self.build(journal=[], orders=[], versions=[], reply=reply)
        self.executor.cfg['bracket_orders_enabled'] = True
        self.assertEqual(self.executor.send(bracket_payload()), ('accepted', {
            'broker_order_id': str(BRACKET_ID), 'id': str(BRACKET_ID), 'legs': {}, 'legs_from': {}, 'legs_missing': ['stop'],
            'entry_status': 'Rejected'}))

    def test_staged_tp1_not_sent_after_a_rejection_carries_the_reason(self):
        self.build(rejected_reports())
        cmd = {'command_id': ENTRY, 'side': 'short', 'trade_id': TRADE}
        plan = {'quantity': QTY, 'tp1_quantity': TP1_QTY, 'tp1_price': TP1_LIMIT, 'exit_action': 'Buy'}
        self.assertEqual(self.executor._stage_tp1(cmd, plan, str(ENTRY_ID), {'stop': STOP_ID}, 'live'), {
            'sent': False, 'reason': NOT_FILLED, 'entry_reject_reason': RISK_REASON})
        self.build()
        self.assertEqual(self.executor._stage_tp1(cmd, plan, str(ENTRY_ID), {'stop': STOP_ID}, 'live'), {
            'sent': False, 'reason': NOT_FILLED})


# ----------------------------------------------------------------------------------------
# Followers: the daemon's order view and the firing service's skip note
# ----------------------------------------------------------------------------------------

class FollowerView(RejectedSnapshot):
    def test_order_view_adds_the_reason_only_for_a_rejected_order(self):
        self.build(rejected_reports())
        with self.state.lock:
            view = followers._order_view(self.executor, ENTRY_ID)
        self.assertEqual(view['reject_reason'], RISK_REASON)
        self.build()
        with self.state.lock:
            plain = followers._order_view(self.executor, ENTRY_ID)
        self.assertNotIn('reject_reason', plain)
        self.assertEqual({k: v for k, v in view.items() if k != 'reject_reason'}, plain)
        self.assertEqual(list(plain), ['broker_order_id', 'seen', 'status', 'working', 'order_type', 'action',
                                       'quantity', 'filled', 'price', 'stop_price', 'fill_ms', 'avg_fill_price',
                                       'terminal_age_ms'])

    def test_view_of_a_cancelled_order_never_has_it(self):
        self.build(rejected_reports(), orders=[order(ENTRY_ID, 'Canceled'), order(STOP_ID, 'Rejected')])
        with self.state.lock:
            self.assertNotIn('reject_reason', followers._order_view(self.executor, ENTRY_ID))
            self.assertEqual(followers._order_view(self.executor, STOP_ID)['reject_reason'], STOP_REASON)


class Clock:
    def __init__(self, t=SIGNAL_MS + 2500):
        self.t = t

    def __call__(self):
        return self.t


class StateStub:
    def __init__(self):
        self.value = None

    def get(self, trade_id=None):
        return json.loads(json.dumps(self.value))


class Transport:
    def __init__(self, reply=None):
        self.sent, self.reply = [], reply

    def send(self, payload):
        self.sent.append(payload)
        return self.reply or ('accepted', {'broker_order_id': str(FOLLOWER_ENTRY_ID), 'id': str(FOLLOWER_ENTRY_ID),
                                           'legs': {'stop': str(FOLLOWER_STOP_ID)}, 'legs_from': {'stop': 'reply'}})


def soup_trade():
    """Soup's trade as the firing service reads it (the follower plan matches the leader's start record)."""
    payload = {'trade_id': TRADE, 'symbol': 'MNQZ2021', 'initial_hard_stop_enabled': True,
               'initial_hard_stop_price': STOP_PRICE, 'break_even_price': BREAK_EVEN, 'tp1_price': TP1_PRICE,
               'execution_context': {'chart_seconds': 1}}
    return {'id': TRADE, 'side': 'short', 'phase': 'running', 'payload': payload, 'native': {'mode': 'native'},
            'alloc': {'tp1': TP1_QTY, 'parent': 1, 'bias': 1, 'eod': 1},
            'quantity_policy': {'exit_allocation': 'original', 'max_contracts': 10},
            'execution_policy': {'tp1_working_limit': TP1_LIMIT}, 'reductions': {}, 'terminal_reason': None}


class FiringNote(unittest.TestCase):
    """The lane's native step with a stubbed bridge view (no browser, no Soup process)."""

    def lane(self, transport=None):
        tmp = Path(tempfile.mkdtemp(prefix='reject-reason-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        (tmp / 'soup').mkdir()
        cfg = {'soup_db': str(tmp / 'soup' / 'engine.sqlite'), 'soup_engine_dir': str(tmp / 'engine'),
               'ticker': 'MNQZ2021', 'mirror_dir': str(tmp / 'follower-1'), 'bridge_url': 'http://127.0.0.1:9'}
        self.clock, self.state = Clock(), StateStub()
        self.transport = transport or Transport()
        self.alerts = AlertLog(tmp, self.clock)
        lane = FollowerLane(cfg, 'reject-reason-token-0123456789abcdef', 'follower-1', alerts=self.alerts,
                            transport=self.transport, bridge_state=self.state,
                            soup=(lambda *a, **k: None, lambda value: 'digest'), report=None, clock_ms=self.clock,
                            native_policy=dict(follower_native.NATIVE_DEFAULTS))
        self.addCleanup(lane.close)
        lane._native_kill = lambda reason: self.fail('kill switch requested: ' + reason)
        c = {'id': ENTRY, 'trade_id': TRADE, 'kind': 'entry', 'quantity': QTY, 'price': REFERENCE,
             'started_at': STARTED}
        info = dict(FollowerLane._trade_info(lane, soup_trade()), symbol='MNQZ2021')
        lane._native_entry(info, c)
        return lane

    def view(self, reason=None):
        ent = {'kind': 'bracket_entry', 'outcome': 'accepted', 'seen': True, 'working': False, 'status': 'Rejected',
               'filled': 0, 'quantity': QTY}
        if reason is not None:
            ent['reject_reason'] = reason
        self.state.value = {'synced': True, 'net_position': 0, 'open_order_ids': [], 'unseen_accepted_orders': [],
                            'observed_at': self.clock(),
                            'trade': {'commands': {ENTRY: ent},
                                      'stop': {'broker_order_id': str(FOLLOWER_STOP_ID), 'seen': True, 'status': 'Rejected',
                                               'working': False, 'quantity': QTY, 'filled': 0}}}

    def test_skip_note_carries_tradovate_reason(self):
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        flog = logging.getLogger('tvbridge.firing')
        old = flog.level
        flog.addHandler(handler)
        flog.setLevel(logging.INFO)
        self.addCleanup(flog.removeHandler, handler)
        self.addCleanup(flog.setLevel, old)
        lane = self.lane()
        self.view(RISK_REASON)
        lane._native_step(lane.nat[TRADE])
        (alert,) = self.alerts.recent
        self.assertEqual({k: v for k, v in alert.items() if k != 'at'}, {
            'account': 'follower-1', 'command_id': ENTRY, 'kind': 'follower_entry_skipped', 'level': 'info',
            'reason': 'entry_rejected_no_fill', 'status': 'Rejected', 'trade_id': TRADE, 'type': 'alert',
            'reject_reason': RISK_REASON})
        notes = [r.getMessage() for r in records if 'follower_entry_skipped' in r.getMessage()]
        self.assertEqual(len(notes), 1)
        self.assertIn('"reject_reason": "%s' % RISK_REASON, notes[0])
        self.assertEqual(lane.nat[TRADE]['skipped'], 'entry_rejected_no_fill')

    def test_skip_note_without_a_reason_is_the_baseline_note(self):
        lane = self.lane()
        self.view()
        lane._native_step(lane.nat[TRADE])
        (alert,) = self.alerts.recent
        # The baseline alert without a reason (firing/alerts.jsonl), the time aside.
        expected = {"account": "follower-1", "command_id": ENTRY, "kind": "follower_entry_skipped", "level": "info",
                "reason": "entry_rejected_no_fill", "status": "Rejected", "trade_id": TRADE, "type": "alert"}
        self.assertEqual({k: v for k, v in alert.items() if k != 'at'}, expected)
        self.assertEqual(sorted(alert), sorted(list(expected) + ['at']))

    def test_entry_refused_with_tradovate_text(self):
        lane = self.lane(Transport(('rejected', {'code': 'None', 's': 200, 'failureReason': 'UnknownReason',
                                                 'failureText': 'Account is locked', 'reject_reason': 'Account is locked',
                                                 'follower': 'follower-1'})))
        (alert,) = self.alerts.recent
        self.assertEqual((alert['reason'], alert['refusal'], alert['reject_reason']),
                         ('entry_refused', 'Account is locked', 'Account is locked'))
        self.assertTrue(lane.nat[TRADE]['done'])

    def test_entry_refused_by_the_bridge_is_unchanged(self):
        self.lane(Transport(('rejected', {'bridge': 'refused', 'reason': 'entries disabled by kill switch'})))
        (alert,) = self.alerts.recent
        self.assertEqual({k: v for k, v in alert.items() if k != 'at'}, {
            'account': 'follower-1', 'command_id': ENTRY, 'kind': 'follower_entry_skipped', 'level': 'info',
            'reason': 'entry_refused', 'refusal': 'entries disabled by kill switch', 'trade_id': TRADE,
            'type': 'alert'})

    def test_reject_detail_bounds_and_ignores_junk(self):
        self.assertEqual(follower_native._reject_detail({'reject_reason': ' a \n b ' + 'c' * 400}),
                         {'reject_reason': ('a b ' + 'c' * 400)[:300]})
        for junk in (None, {}, {'reject_reason': None}, {'reject_reason': '  '}, {'reject_reason': 5}, 'x'):
            self.assertEqual(follower_native._reject_detail(junk), {})


if __name__ == '__main__':
    unittest.main()
