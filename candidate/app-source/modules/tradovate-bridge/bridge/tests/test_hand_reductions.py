"""Contracts of the leader's trade closed by hand in Tradovate explain the position.

Browser-free: an in-memory Tradovate state and a page double; nothing reaches a browser or the network.
The leader's auto-protect then shrinks the stop to the smaller position (and cancels the legs once flat),
and Soup's own stop modify is no longer refused. Anything else stays fail-closed: a hand order still
working, a hand fill that adds to the position, an unresolved command, a reversal, and follower accounts.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge.executor import Executor, leg_command_id
from tvbridge.followers import FollowerExecutor
from tvbridge.journal import Journal
from tvbridge.state import AccountState

ACCOUNT, CONTRACT = 1, 2
TRADE = 'hand:MNQZ2021:1618839600000:long'
CID = 'soup-entry'
ENTRY, STOP, TP1 = 100, 101, 102


class Page:
    def __init__(self, state):
        self.state, self.epoch, self.requests = state, 'e1', []

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5, expected_epoch=None):
        self.requests.append((endpoint, body))
        if endpoint == 'order/modifyorder':
            return {'ok': True, 'sent': True, 'd': {'commandId': 77}}
        return {'ok': True, 'sent': True, 'd': {}}


def order(oid, status, action, qty, kind='Market', **price):
    return ({'id': oid, 'accountId': ACCOUNT, 'contractId': CONTRACT, 'ordStatus': status, 'action': action},
            dict({'id': 1000 + oid, 'orderId': oid, 'orderQty': qty, 'orderType': kind}, **price))


class HandBase(unittest.TestCase):
    executor_class = Executor

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = AccountState()
        self.page = Page(self.state)
        self.journal = Journal(self.tmp.name)
        self.orders, self.versions, self.fills = {}, {}, []
        self.net = 5
        self.add(ENTRY, 'Filled', 'Buy', 5, fill=5, price=6000.0)
        self.add(STOP, 'Working', 'Sell', 5, kind='Stop', stopPrice=5992.75)
        self.add(TP1, 'Working', 'Sell', 2, kind='Limit', price=6013.5)
        self.sync()
        cfg = {'ticker': 'MNQZ2021', 'account_name': 'FAKE', 'account_id': ACCOUNT, 'soup_account_id': 'FAKE',
               'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0, 'entry_settle_s': 0.05,
               'bracket_orders_enabled': True, 'flatten_all_enabled': True}
        if self.executor_class is FollowerExecutor:
            cfg['follower_key'] = 'account-b'
        self.ex = self.executor_class(self.page, self.state, self.journal, cfg)
        self.ex.resolve()
        self.journal.start(CID, trade_id=TRADE, kind='bracket_entry', side='long', quantity=5, tag='t', at=None)
        self.journal.finish(CID, 'accepted', {'legs': {'stop': str(STOP)}}, str(ENTRY), legs={'stop': str(STOP)})
        tp = leg_command_id(CID, 'tp1')
        self.journal.start(tp, trade_id=TRADE, kind='bracket_tp1', side='long', quantity=2, tag='t1', at=None)
        self.journal.finish(tp, 'accepted', {}, str(TP1))
        self.entry = self.journal.get(CID)

    def add(self, oid, status, action, qty, kind='Market', fill=0, price=None, **extra):
        self.orders[oid], self.versions[oid] = order(oid, status, action, qty, kind, **extra)
        if fill:
            self.fills.append({'id': 5000 + len(self.fills), 'orderId': oid, 'contractId': CONTRACT, 'qty': fill,
                               'price': price or 6001.25, 'timestamp': '2021-04-19T13:45:00Z'})

    def sync(self):
        snap = {'accounts': [{'id': ACCOUNT, 'name': 'FAKE'}], 'orders': list(self.orders.values()),
                'fills': self.fills, 'orderVersions': list(self.versions.values()),
                'positions': [{'id': 3, 'accountId': ACCOUNT, 'contractId': CONTRACT, 'netPos': self.net,
                               'tradeDate': {'year': 2021, 'month': 4, 'day': 19}}],
                'contracts': [{'id': CONTRACT, 'name': 'MNQZ1'}]}
        self.state.load_sync(snap['accounts'], snap, self.page.epoch)

    def hand_sell(self, oid, qty, status='Filled'):
        """A person sells `qty` at market in Tradovate (an order the bridge never placed)."""
        self.add(oid, status, 'Sell', qty, fill=qty if status == 'Filled' else 0)
        if status == 'Filled':
            self.net -= qty
        self.sync()

    def modify(self, cid, qty=None, price=None):
        extras = {'soup_command_id': cid, 'soup_trade_id': TRADE, 'bridge_kind': 'modify_stop'}
        if qty is not None:
            extras['stop_quantity'] = qty
        if price is not None:
            extras['stop_price'] = price
        return self.ex.send({'ticker': 'MNQZ2021', 'action': 'modify_stop', 'extras': extras})


class LeaderHandReductions(HandBase):
    def test_no_hand_order_is_unchanged(self):
        self.assertEqual(self.ex._consistent_held(self.entry), 5)
        self.assertEqual(self.ex.maintain_brackets(), [])

    def test_partial_hand_close_shrinks_the_stop_and_allows_soups_modify(self):
        self.hand_sell(200, 2)
        self.assertEqual(self.ex._consistent_held(self.entry), 3)
        sent = self.ex.maintain_brackets()
        self.assertEqual(sent, [leg_command_id(CID, 'auto-shrink-3')])
        endpoint, body = self.page.requests[-1]
        self.assertEqual((endpoint, body['orderId'], body['orderQty'], body['stopPrice']),
                         ('order/modifyorder', STOP, 3, 5992.75))          # price untouched
        out, receipt = self.modify('soup-resize', qty=3, price=5996.0)      # e.g. a hand-moved price kept
        self.assertEqual(out, 'accepted', receipt)

    def test_full_hand_close_cancels_the_orphaned_legs(self):
        self.hand_sell(200, 5)
        self.assertEqual(self.ex._consistent_held(self.entry), 0)
        sent = self.ex.maintain_brackets()
        self.assertEqual(sent, [leg_command_id(CID, 'auto-cancel')])
        self.assertEqual(sorted(b['orderId'] for e, b in self.page.requests if e == 'order/cancelorder'), [STOP, TP1])

    def test_fail_closed_cases(self):
        cases = {'hand order partly filled and still working': lambda: (
                     self.add(200, 'Working', 'Sell', 3, fill=1), setattr(self, 'net', 4), self.sync()),
                 'hand fill adds to the position': lambda: (self.add(200, 'Filled', 'Buy', 1, fill=1),
                                                             setattr(self, 'net', 6), self.sync()),
                 'reversed through flat by hand': lambda: self.hand_sell(200, 6),
                 'unresolved bridge command': lambda: (self.hand_sell(200, 2),
                                                       self.journal.start('x', trade_id=TRADE, kind='cancel',
                                                                          side='long', quantity=None, at=None))}
        for name, make in cases.items():
            with self.subTest(name):
                self.setUp()
                make()
                self.assertIsNone(self.ex._consistent_held(self.entry))
                before = len(self.page.requests)
                self.assertEqual(self.ex.maintain_brackets(), [])
                self.assertEqual(self.page.requests[before:], [])

    def test_unfilled_hand_order_working_is_as_before(self):
        self.hand_sell(200, 2, status='Working')                  # nothing filled: the position is unchanged
        self.assertEqual(self.ex._consistent_held(self.entry), 5)  # unchanged result (Soup refuses the snapshot)

    def test_hand_order_before_the_entry_is_ignored(self):
        self.add(50, 'Filled', 'Sell', 1, fill=1)                 # an older order (lower id) of an earlier trade
        self.sync()
        self.assertEqual(self.ex._consistent_held(self.entry), 5)


class FollowerUnchanged(HandBase):
    executor_class = FollowerExecutor

    def test_follower_hand_close_stays_fail_closed(self):
        self.hand_sell(200, 2)
        self.assertIsNone(self.ex._consistent_held(self.entry))
        self.assertEqual(self.ex.maintain_brackets(), [])


if __name__ == '__main__':
    unittest.main()
