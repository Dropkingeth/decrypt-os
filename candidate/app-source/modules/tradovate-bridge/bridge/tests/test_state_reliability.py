"""Incomplete account inventory must never become a trusted flat account."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tvbridge.state import AccountState


def snapshot():
    return {'accounts': [{'id': 1, 'name': 'A'}], 'orders': [], 'fills': [], 'orderVersions': [],
            'positions': [{'id': 10, 'accountId': 1, 'contractId': 5, 'netPos': 2,
                           'tradeDate': {'year': 2021, 'month': 9, 'day': 24}}]}


class SyncReliability(unittest.TestCase):
    def setUp(self):
        self.state = AccountState()
        self.good = snapshot()
        self.state.load_sync(self.good['accounts'], self.good, 'old')

    def assert_blocked(self, accounts, data):
        previous = copy.deepcopy(self.state.entities)
        with self.assertRaises(ValueError):
            self.state.load_sync(accounts, data, 'new')
        self.assertFalse(self.state.synced)
        self.assertEqual(self.state.entities, previous)
        self.assertEqual(self.state.net_position(1, 5), 2)

    def test_missing_or_wrong_collection_is_unknown_not_flat(self):
        for key in ('accounts', 'positions', 'orders', 'fills', 'orderVersions'):
            for replacement in ('missing', None, {}, 'malformed'):
                with self.subTest(key=key, replacement=replacement):
                    bad = snapshot()
                    if replacement == 'missing':
                        del bad[key]
                    else:
                        bad[key] = replacement
                    self.assert_blocked(self.good['accounts'], bad)

    def test_partial_account_failure_revokes_old_epoch_without_partial_replace(self):
        self.assert_blocked([self.good['accounts'][0], {}], snapshot())

    def test_invalid_entities_and_duplicates_block(self):
        variants = [None, {}, {'id': True}, {'id': 11, 'accountId': 1, 'contractId': 5, 'netPos': '0'},
                    dict(self.good['positions'][0], netPos=False)]
        for row in variants:
            bad = snapshot()
            bad['positions'].append(row)
            self.assert_blocked(self.good['accounts'], bad)
        bad = snapshot()
        bad['positions'].append(copy.deepcopy(bad['positions'][0]))
        self.assert_blocked(self.good['accounts'], bad)

    def test_account_disagreement_does_not_preserve_missing_pin(self):
        bad = snapshot()
        bad['accounts'] = []
        self.assert_blocked(self.good['accounts'], bad)

    def test_valid_empty_inventory_and_missing_contracts_can_recover(self):
        self.state.mark_unsynced()
        data = snapshot()
        data['positions'] = []
        self.state.load_sync(data['accounts'], data, 'new')
        self.assertTrue(self.state.is_synced_for('new'))
        self.assertEqual(self.state.net_position(1, 5), 0)
        data['accounts'][0]['name'] = 'changed after load'
        self.assertEqual(self.state.account_by_name('A')['id'], 1)

    def test_malformed_position_update_revokes_sync_without_becoming_flat(self):
        before = copy.deepcopy(self.state.entities)
        with self.assertRaises(ValueError):
            self.state.apply_props({'entityType': 'position', 'entity':
                dict(self.good['positions'][0], netPos=None)})
        self.assertFalse(self.state.synced)
        self.assertEqual(self.state.entities, before)

    def test_unrecognized_order_status_cannot_be_treated_as_no_working_orders(self):
        with self.assertRaises(ValueError):
            self.state.apply_props({'entityType': 'order', 'entity':
                {'id': 20, 'accountId': 1, 'contractId': 5, 'ordStatus': 'UnknownNewStatus'}})
        self.assertFalse(self.state.synced)


if __name__ == '__main__':
    unittest.main()
