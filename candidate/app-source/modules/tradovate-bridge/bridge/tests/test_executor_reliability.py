"""Connection-epoch dispatch races, using only in-memory broker doubles."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.state import AccountState

TRADE = 'test:MNQZ2021:1:long'


def snapshot(net=0):
    return {'accounts': [{'id': 1, 'name': 'FAKE'}], 'orders': [], 'fills': [],
            'positions': [{'id': 3, 'accountId': 1, 'contractId': 2, 'netPos': net,
                           'tradeDate': {'year': 2021, 'month': 9, 'day': 24}}],
            'orderVersions': [], 'contracts': [{'id': 2, 'name': 'MNQZ1'}]}


class PageDouble:
    def __init__(self, state):
        self.state, self.epoch = state, 'first'
        self.requests, self.wire = [], []
        self.uncertain = False
        self.resync(0)

    def resync(self, net, epoch=None):
        self.epoch = epoch or self.epoch
        data = snapshot(net)
        self.state.load_sync(data['accounts'], data, self.epoch)

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5, expected_epoch=None):
        self.requests.append((endpoint, body, expected_epoch))
        if expected_epoch != self.epoch or not self.state.is_synced_for(self.epoch):
            return {'ok': False, 'sent': False, 'code': 'stale_epoch_or_unsynced'}
        self.wire.append((endpoint, body))
        if self.uncertain:
            return {'ok': False, 'sent': True, 'code': 'devtools_error'}
        return {'ok': True, 'sent': True, 'd': {'orderId': 10}}


class EpochDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = AccountState()
        self.page = PageDouble(self.state)
        self.journal = Journal(self.tmp.name)
        self.executor = Executor(self.page, self.state, self.journal, {
            'ticker': 'MNQZ2021', 'account_name': 'FAKE', 'account_id': 1,
            'soup_account_id': 'FAKE', 'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0,
            'kill_switch_file': self.tmp.name + '/ENTRIES-DISABLED'})
        self.executor.resolve()

    def command(self, action='buy', cid='new'):
        value = {'ticker': 'MNQZ2021', 'action': action, 'orderType': 'market', 'quantity': 2,
                 'extras': {'soup_command_id': cid, 'soup_trade_id': TRADE}}
        if action == 'exit':
            value['cancel'] = False
        return value

    def filled_entry(self):
        self.journal.start('entry', trade_id=TRADE, kind='entry', side='long', quantity=2, at=1)
        self.journal.finish('entry', 'accepted', {}, '8')
        self.page.resync(2)
        self.state.apply_props({'entityType': 'order', 'eventType': 'Created', 'entity': {
            'id': 8, 'accountId': 1, 'contractId': 2, 'ordStatus': 'Filled'}})

    def test_reconnect_after_exit_plan_refuses_without_dispatch_or_journal_start(self):
        self.filled_entry()
        guard = self.executor._guard

        def reconnect_after_plan(command):
            plan = guard(command)
            self.assertEqual(plan['quantity'], 2)
            self.page.resync(0, epoch='second')
            return plan

        self.executor._guard = reconnect_after_plan
        outcome, receipt = self.executor.send(self.command('exit'))
        self.assertEqual(outcome, 'rejected')
        self.assertEqual(receipt['reason'], 'connection_changed_during_order_plan')
        self.assertEqual(self.page.requests, [])
        self.assertIsNone(self.journal.get('new'))

    def test_reconnect_after_journal_start_is_rejected_by_page_without_wire_order(self):
        start = self.journal.start

        def reconnect_after_start(*args, **kwargs):
            start(*args, **kwargs)
            self.page.resync(1, epoch='second')

        self.journal.start = reconnect_after_start
        outcome, receipt = self.executor.send(self.command())
        self.assertEqual(outcome, 'rejected')
        self.assertEqual(receipt['code'], 'stale_epoch_or_unsynced')
        self.assertEqual(self.page.wire, [])
        self.assertEqual(self.page.requests[0][2], 'first')
        self.assertEqual(self.journal.get('new')['outcome'], 'rejected')

    def test_transport_uncertainty_after_start_stays_unknown_without_replay(self):
        self.page.uncertain = True
        outcome, _ = self.executor.send(self.command())
        self.assertEqual(outcome, 'unknown')
        outcome, receipt = self.executor.send(self.command())
        self.assertEqual(outcome, 'unknown')
        self.assertTrue(receipt['duplicate'])
        self.assertEqual(len(self.page.wire), 1)

    def test_healthy_dispatch_passes_epoch(self):
        outcome, _ = self.executor.send(self.command())
        self.assertEqual(outcome, 'accepted')
        self.assertEqual(self.page.requests[0][2], 'first')
        self.assertEqual(len(self.page.wire), 1)

    def test_snapshot_rechecks_invalidated_state_after_health(self):
        health = self.executor.health

        def invalidate_after_health(**kwargs):
            result = health(**kwargs)
            self.state.mark_unsynced()
            return result

        self.executor.health = invalidate_after_health
        value, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(value)
        self.assertEqual(reason, 'connection_changed_before_snapshot')

    def test_publisher_fault_blocks_dispatch_but_allows_recovery_snapshot(self):
        self.executor.watchdogs.append(lambda: 'snapshot_publisher_failing')
        outcome, _ = self.executor.send(self.command())
        self.assertEqual(outcome, 'rejected')
        self.assertEqual(self.page.wire, [])
        value, reason = self.executor.snapshot(TRADE)
        self.assertIsNone(reason)
        self.assertEqual(value['position_qty'], 0)

    def test_cancellation_passes_expected_epoch(self):
        self.state.apply_props({'entityType': 'order', 'eventType': 'Created', 'entity': {
            'id': 8, 'accountId': 1, 'contractId': 2, 'ordStatus': 'Working'}})
        command = self.command('cancel')
        del command['orderType'], command['quantity']
        outcome, _ = self.executor.send(command)
        self.assertEqual(outcome, 'accepted')
        self.assertEqual(self.page.requests[0], ('order/cancelorder', {'orderId': 8}, 'first'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
