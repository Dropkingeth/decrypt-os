"""Cross-account anti-hedge entry guard, using only in-memory broker doubles."""
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge import protocol
from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.state import AccountState

OURS = 1
OTHER_A, OTHER_B = 7001, 7002
SECRET_NAMES = ('PROP-SECRET-A', 'PROP-SECRET-B')
MNQ, MES, NQ, CL = 2, 3, 4, 5
CONTRACTS = [{'id': MNQ, 'name': 'MNQZ1'}, {'id': MES, 'name': 'MESZ1'},
             {'id': NQ, 'name': 'NQZ1'}, {'id': CL, 'name': 'CLZ1'}]


def pos(pid, acct, cid, net, day=24):
    return {'id': pid, 'accountId': acct, 'contractId': cid, 'netPos': net,
            'tradeDate': {'year': 2021, 'month': 9, 'day': day}}


class PageStub:
    """Healthy page that answers orders and contract lookups; records every request."""

    def __init__(self, state):
        self.state, self.epoch = state, 'e1'
        self.requests = []
        self.lookup = {}  # contract id -> name answered by contract/item
        self.next_order = 500

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5, expected_epoch=None):
        self.requests.append((endpoint, body, query))
        if endpoint == 'contract/item':
            cid = int(query.split('=', 1)[1])
            if cid in self.lookup:
                return {'ok': True, 'sent': True, 'd': {'id': cid, 'name': self.lookup[cid]}}
            return {'ok': False, 'sent': True, 's': 404, 'code': 'not_found'}
        if endpoint == 'order/placeorder':
            self.next_order += 1
            return {'ok': True, 'sent': True, 'd': {'orderId': self.next_order}}
        if endpoint == 'order/cancelorder':
            return {'ok': True, 'sent': True, 'd': {'commandId': 1}}
        return {'ok': False, 'sent': False, 'code': 'unexpected'}

    def placed(self):
        return [r for r in self.requests if r[0] == 'order/placeorder']


class HedgeGuardBase(unittest.TestCase):
    config = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = AccountState()
        self.page = PageStub(self.state)
        self.journal = Journal(self.tmp.name)
        self.sync()
        self.executor = self.make_executor(self.config)
        self.executor.resolve()
        self.seq = 0

    def make_executor(self, extra):
        cfg = {'ticker': 'MNQZ2021', 'account_name': 'FAKE', 'account_id': OURS, 'soup_account_id': 'FAKE',
               'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0, 'entry_settle_s': 0,
               'kill_switch_file': self.tmp.name + '/ENTRIES-DISABLED'}
        cfg.update(extra)
        return Executor(self.page, self.state, self.journal, cfg)

    def sync(self, positions=(), orders=(), versions=(), fills=(), contracts=CONTRACTS):
        accounts = [{'id': OURS, 'name': 'FAKE'}, {'id': OTHER_A, 'name': SECRET_NAMES[0]},
                    {'id': OTHER_B, 'name': SECRET_NAMES[1]}]
        snap = {'accounts': accounts, 'positions': list(positions), 'orders': list(orders),
                'orderVersions': list(versions), 'fills': list(fills), 'contracts': list(contracts)}
        self.state.load_sync(accounts, snap, self.page.epoch)

    def payload(self, action='buy', side='long', order_type='market', **extra):
        self.seq += 1
        p = {'ticker': 'MNQZ2021', 'action': action, 'orderType': order_type, 'quantity': 1,
             'extras': {'soup_command_id': f'cmd-{self.seq}', 'soup_trade_id': f'test:MNQZ2021:{self.seq}:{side}'}}
        p.update(extra)
        return p

    def entry(self, side='long'):
        return self.executor.send(self.payload('buy' if side == 'long' else 'sell', side))

    def assert_refused(self, result, *fragments):
        outcome, receipt = result
        self.assertEqual(outcome, 'rejected', receipt)
        self.assertEqual(receipt.get('bridge'), 'refused')
        self.assertTrue(receipt['reason'].startswith('hedge guard:'), receipt['reason'])
        for fragment in fragments:
            self.assertIn(fragment, receipt['reason'])
        self.assertEqual(self.page.placed(), [])
        self.assert_private(receipt['reason'])
        return receipt['reason']

    def assert_accepted(self, result):
        outcome, receipt = result
        self.assertEqual(outcome, 'accepted', receipt)
        self.assertEqual(len(self.page.placed()), 1)

    def assert_private(self, text):
        for secret in SECRET_NAMES + (str(OTHER_A), str(OTHER_B)):
            self.assertNotIn(secret, text)


class Refusals(HedgeGuardBase):
    def test_opposite_position_other_account_same_contract(self):
        self.sync(positions=[pos(10, OTHER_A, MNQ, -2)])
        reason = self.assert_refused(self.entry('long'), 'short', 'MNQ', 'hedge group ES/MES/NQ/MNQ/YM/MYM/RTY/M2K',
                                     '1 other account(s)')
        self.assertNotIn('this account', reason)
        self.assertEqual(self.journal.for_trade('test:MNQZ2021:1:long'), [])

    def test_correlated_contract_other_account(self):
        self.sync(positions=[pos(10, OTHER_A, MES, -1)])
        self.assert_refused(self.entry('long'), 'short position', 'in MES')

    def test_mirror_short_entry_against_other_account_long_nq(self):
        self.sync(positions=[pos(10, OTHER_A, NQ, 3)])
        self.assert_refused(self.entry('short'), 'short MNQ entry refused', '(long)', 'in NQ')

    def test_same_account_other_contract(self):
        self.sync(positions=[pos(10, OURS, MES, -1)])
        self.assert_refused(self.entry('long'), 'this account: 1 short position(s) in MES')

    def test_same_account_other_month_of_same_product(self):
        contracts = CONTRACTS + [{'id': 9, 'name': 'MNQU1'}]
        self.sync(positions=[pos(10, OURS, 9, -1)], contracts=contracts)
        self.assert_refused(self.entry('long'), 'this account', 'in MNQ')

    def test_working_opposite_order_other_account(self):
        self.sync(orders=[{'id': 20, 'accountId': OTHER_B, 'contractId': MES, 'action': 'Sell',
                           'ordStatus': 'Working'}],
                  versions=[{'id': 20, 'orderId': 20, 'orderQty': 1}])
        self.assert_refused(self.entry('long'), 'working order(s) that could open or add to a short position')

    def test_order_that_would_reverse_other_account_through_flat(self):
        self.sync(positions=[pos(10, OTHER_A, NQ, 2)],
                  orders=[{'id': 20, 'accountId': OTHER_A, 'contractId': NQ, 'action': 'Sell', 'ordStatus': 'Working'}],
                  versions=[{'id': 20, 'orderId': 20, 'orderQty': 3}])
        self.assert_refused(self.entry('long'), 'could open or add to a short position')

    def test_order_with_unknown_side_is_refused(self):
        self.sync(orders=[{'id': 20, 'accountId': OTHER_A, 'contractId': MES, 'ordStatus': 'Working'}])
        self.assert_refused(self.entry('long'), 'unknown side or size')

    def test_reducing_order_with_unknown_size_is_refused(self):
        self.sync(positions=[pos(10, OTHER_A, MES, 1)],
                  orders=[{'id': 20, 'accountId': OTHER_A, 'contractId': MES, 'action': 'Sell', 'ordStatus': 'Working'}])
        self.assert_refused(self.entry('long'), 'unknown side or size')

    def test_counts_several_accounts_without_naming_them(self):
        self.sync(positions=[pos(10, OTHER_A, NQ, -1), pos(11, OTHER_B, MES, -2), pos(12, OURS, MES, -1)])
        reason = self.assert_refused(self.entry('long'), 'this account: 1 short position(s) in MES',
                                     '2 other account(s): 2 short position(s) in MES/NQ')
        self.assertNotIn('FAKE', reason)
        for path in Path(self.tmp.name).rglob('*'):
            if path.is_file():
                self.assert_private(path.read_text(errors='replace'))


class UnknownContracts(HedgeGuardBase):
    def test_unknown_contract_is_resolved_or_refused(self):
        self.sync(positions=[pos(10, OTHER_A, 99, -1)])
        self.assert_refused(self.entry('long'), 'could not be mapped to a product root')
        self.assertIn(('contract/item', None, 'id=99'), self.page.requests)

    def test_unknown_contract_resolved_to_group_member_is_refused(self):
        self.page.lookup[99] = 'NQZ1'
        self.sync(positions=[pos(10, OTHER_A, 99, -1)])
        self.assert_refused(self.entry('long'), 'in NQ')

    def test_unknown_contract_resolved_outside_group_is_allowed_and_cached(self):
        self.page.lookup[99] = 'CLZ1'
        self.sync(positions=[pos(10, OTHER_A, 99, -1)])
        self.assert_accepted(self.entry('long'))
        lookups = [r for r in self.page.requests if r[0] == 'contract/item']
        self.assertEqual(len(lookups), 1)
        self.assertEqual(self.executor.contract_names[99], 'CLZ1')

    def test_unparseable_contract_name_is_refused(self):
        contracts = CONTRACTS + [{'id': 9, 'name': 'MNQZ1 C21000'}]
        self.sync(positions=[pos(10, OTHER_A, 9, -1)], contracts=contracts)
        self.assert_refused(self.entry('long'), 'could not be mapped')

    def test_own_contract_named_from_the_pinned_symbol_after_resync_without_contracts(self):
        # Live syncs may omit contracts; the pinned contract id is still known.
        self.sync(positions=[pos(10, OTHER_A, MNQ, -1)], contracts=[])
        self.assert_refused(self.entry('long'), 'in MNQ')
        self.assertEqual([r for r in self.page.requests if r[0] == 'contract/item'], [])


class Allowed(HedgeGuardBase):
    def test_flat_everywhere(self):
        self.sync(positions=[pos(10, OTHER_A, NQ, 0), pos(11, OURS, MES, 0)])
        self.assert_accepted(self.entry('long'))

    def test_latest_trade_date_wins(self):
        self.sync(positions=[pos(10, OTHER_A, NQ, -2, day=24), pos(11, OTHER_A, NQ, 0, day=25)])
        self.assert_accepted(self.entry('long'))

    def test_same_side_positions(self):
        self.sync(positions=[pos(10, OTHER_A, NQ, 2), pos(11, OURS, MES, 1), pos(12, OTHER_B, MNQ, 1)])
        self.assert_accepted(self.entry('long'))

    def test_protective_orders_on_same_side_position_do_not_count(self):
        # Another account is long MES with a bracket (stop and target, each for the whole position).
        self.sync(positions=[pos(10, OTHER_A, MES, 2)],
                  orders=[{'id': 20, 'accountId': OTHER_A, 'contractId': MES, 'action': 'Sell', 'ordStatus': 'Working'},
                          {'id': 21, 'accountId': OTHER_A, 'contractId': MES, 'action': 'Sell', 'ordStatus': 'Working'}],
                  versions=[{'id': 20, 'orderId': 20, 'orderQty': 2}, {'id': 21, 'orderId': 21, 'orderQty': 2}])
        self.assert_accepted(self.entry('long'))

    def test_terminal_and_same_side_orders_do_not_count(self):
        self.sync(orders=[{'id': 20, 'accountId': OTHER_A, 'contractId': NQ, 'action': 'Sell', 'ordStatus': 'Filled'},
                          {'id': 21, 'accountId': OTHER_A, 'contractId': NQ, 'action': 'Sell', 'ordStatus': 'Canceled'},
                          {'id': 22, 'accountId': OTHER_B, 'contractId': MES, 'action': 'Buy', 'ordStatus': 'Working'}],
                  versions=[{'id': 22, 'orderId': 22, 'orderQty': 1}])
        self.assert_accepted(self.entry('long'))

    def test_uncorrelated_product_is_allowed(self):
        self.sync(positions=[pos(10, OTHER_A, CL, -3)])
        self.assert_accepted(self.entry('long'))


class ExitsNeverBlocked(HedgeGuardBase):
    TRADE = 'test:MNQZ2021:900:long'

    def hedge_like_state(self, extra_orders=()):
        self.journal.start('entry', trade_id=self.TRADE, kind='entry', side='long', quantity=2, at=1)
        self.journal.finish('entry', 'accepted', {}, '8')
        self.sync(positions=[pos(10, OURS, MNQ, 2), pos(11, OTHER_A, NQ, -1), pos(12, OURS, MES, -1)],
                  orders=[{'id': 8, 'accountId': OURS, 'contractId': MNQ, 'action': 'Buy', 'ordStatus': 'Filled'},
                          *extra_orders],
                  versions=[{'id': 8, 'orderId': 8, 'orderQty': 2}],
                  fills=[{'id': 30, 'orderId': 8, 'qty': 2, 'price': 10000.0, 'timestamp': '2021-09-24T14:00:00.000Z'}])

    def exit_payload(self, order_type, **extra):
        return {'ticker': 'MNQZ2021', 'action': 'exit', 'orderType': order_type, 'cancel': False,
                'extras': {'soup_command_id': 'exit-' + order_type, 'soup_trade_id': self.TRADE}, **extra}

    def test_market_exit(self):
        self.hedge_like_state()
        outcome, receipt = self.executor.send(self.exit_payload('market', quantity=2))
        self.assertEqual(outcome, 'accepted', receipt)
        self.assertEqual(self.page.placed()[0][1]['action'], 'Sell')

    def test_tp1_limit_exit(self):
        self.hedge_like_state()
        outcome, receipt = self.executor.send(self.exit_payload('limit', quantity=1, limitPrice=10004.75))
        self.assertEqual(outcome, 'accepted', receipt)

    def test_cancel(self):
        self.hedge_like_state([{'id': 40, 'accountId': OURS, 'contractId': MNQ, 'action': 'Sell',
                                'ordStatus': 'Working'}])
        payload = {'ticker': 'MNQZ2021', 'action': 'cancel',
                   'extras': {'soup_command_id': 'cancel-1', 'soup_trade_id': self.TRADE}}
        outcome, receipt = self.executor.send(payload)
        self.assertEqual(outcome, 'accepted', receipt)
        self.assertEqual(self.page.requests, [('order/cancelorder', {'orderId': 40}, '')])

    def test_new_entry_is_still_refused_in_that_state(self):
        self.sync(positions=[pos(11, OTHER_A, NQ, -1)])
        self.assert_refused(self.entry('long'))


class Configuration(HedgeGuardBase):
    def test_disabled_guard_needs_an_acknowledgement(self):
        with self.assertRaises(ValueError) as ctx:   # false alone is refused
            self.make_executor({'hedge_guard': False})
        self.assertIn('hedge_guard_off_acknowledged', str(ctx.exception))
        with self.assertRaises(ValueError):
            self.make_executor({'hedge_guard': False, 'hedge_guard_off_acknowledged': 'yes'})

    def test_disabled_guard_warns_and_allows(self):
        with self.assertLogs('tvbridge', 'WARNING') as logs:
            executor = self.make_executor({'hedge_guard': False, 'hedge_guard_off_acknowledged': True})
        self.assertIn('hedge_guard is disabled', logs.output[0])
        executor.resolve()
        self.sync(positions=[pos(10, OTHER_A, MNQ, -2)])
        outcome, receipt = executor.send(self.payload())
        self.assertEqual(outcome, 'accepted', receipt)

    def test_enabled_by_default_without_warning(self):
        self.assertTrue(self.executor.hedge_guard)
        with self.assertLogs('tvbridge', 'DEBUG') as logs:
            logging.getLogger('tvbridge').debug('marker')
            self.make_executor({})
            self.make_executor({'hedge_guard': True})
        self.assertEqual(len(logs.records), 1)

    def test_invalid_settings_fail_construction(self):
        for bad in ({'hedge_guard': 'false'}, {'hedge_guard': 0}, {'hedge_groups': 'ES,NQ'},
                    {'hedge_groups': [['ES', '']]}, {'hedge_groups': [[]]}, {'hedge_groups': [['ES', 5]]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.make_executor(bad)

    def test_custom_groups(self):
        executor = self.make_executor({'hedge_groups': [['mnq', 'NQ'], ['ES', 'MES']]})
        executor.resolve()
        self.assertEqual(executor.hedge_members, ['MNQ', 'NQ'])
        self.sync(positions=[pos(10, OTHER_A, NQ, -1)])
        outcome, receipt = executor.send(self.payload())
        self.assertEqual(outcome, 'rejected')
        self.assertIn('hedge group MNQ/NQ', receipt['reason'])
        self.sync(positions=[pos(10, OTHER_A, MES, -1)])
        self.assertEqual(executor.send(self.payload())[0], 'accepted')

    def test_product_outside_every_group_still_guards_its_own_root(self):
        executor = self.make_executor({'hedge_groups': []})
        executor.resolve()
        self.assertEqual(executor.hedge_members, ['MNQ'])
        self.sync(positions=[pos(10, OTHER_A, MNQ, -1)])
        self.assertEqual(executor.send(self.payload())[0], 'rejected')
        self.sync(positions=[pos(10, OTHER_A, NQ, -1)])
        self.assertEqual(executor.send(self.payload())[0], 'accepted')


class Helpers(unittest.TestCase):
    def test_product_root(self):
        cases = {'MNQZ1': 'MNQ', 'MESZ2021': 'MES', 'MNQZ21': 'MNQ', 'NQH2': 'NQ', 'ESU1': 'ES', 'YMM1': 'YM',
                 'M2KZ1': 'M2K', 'RTYZ1': 'RTY', 'MYMH2022': 'MYM', '6EZ1': '6E', 'CLZ1': 'CL'}
        for name, root in cases.items():
            self.assertEqual(protocol.product_root(name), root, name)
        for name in (None, '', 'MNQ', 'MNQZ', 'MNQZ1 C21000', 'mnqz6', 'MNQA6', 5):
            self.assertIsNone(protocol.product_root(name), name)

    def test_net_positions_uses_latest_trade_date_per_pair(self):
        state = AccountState()
        accounts = [{'id': 1}, {'id': 2}]
        state.load_sync(accounts, {'accounts': accounts, 'orders': [], 'fills': [], 'orderVersions': [],
                                   'positions': [pos(10, 1, 5, 3, day=24), pos(11, 1, 5, -1, day=25),
                                                 pos(12, 2, 5, 2, day=25), pos(13, 2, 6, 0, day=25)]}, 'e')
        self.assertEqual(state.net_positions(), {(1, 5): -1, (2, 5): 2, (2, 6): 0})
        self.assertEqual(state.net_positions()[(1, 5)], state.net_position(1, 5))


if __name__ == '__main__':
    unittest.main(verbosity=2)
