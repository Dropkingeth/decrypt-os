"""The anti-hedge safety check with the config setup.py writes (no browser, in-memory broker doubles).

The bridge refuses an entry that would open the opposite direction to an open position (or a working
order that could open one) on the same instrument or a correlated one: in the same account, in another
account of the same login, and on a peer login. It only refuses; it never changes or resizes an order.
"""
import argparse
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE / 'bridge'))
sys.path.insert(0, str(MODULE / 'bridge' / 'tests'))

from test_hedge_guard import HedgeGuardBase, MES, MNQ, OTHER_A, OURS, pos  # noqa: E402
from tvbridge.peers import account_conflict  # noqa: E402


def setup_module_config():
    spec = importlib.util.spec_from_file_location('decrypt_bridge_setup', MODULE / 'setup.py')
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    args = argparse.Namespace(account_name='SIM-ACCOUNT-1', soup_account_id='12345', ticker='MNQZ2021',
                              tick_size=.25, max_contracts=10, publish_mode='adapter-db',
                              soup_state='/tmp/decrypt-os-test/soup')
    return setup.daemon_config(args)


DEFAULTS = setup_module_config()


class SetupDefaults(unittest.TestCase):
    def test_setup_turns_the_guard_on_for_the_correlated_index_group(self):
        self.assertIs(DEFAULTS['hedge_guard'], True)
        self.assertIn(['ES', 'MES', 'NQ', 'MNQ', 'YM', 'MYM', 'RTY', 'M2K'], DEFAULTS['hedge_groups'])


class WithSetupDefaults(HedgeGuardBase):
    config = {k: DEFAULTS[k] for k in ('hedge_guard', 'hedge_groups')}

    def test_opposite_position_in_another_account_refuses_the_entry(self):
        self.sync(positions=[pos(10, OTHER_A, MNQ, -2)])
        self.assert_refused(self.entry('long'), 'short', '1 other account(s)')

    def test_opposite_position_on_a_correlated_contract_in_the_same_account_refuses(self):
        self.sync(positions=[pos(10, OURS, MES, -1)])
        self.assert_refused(self.entry('long'), 'this account: 1 short position(s) in MES')

    def test_open_position_on_the_same_contract_refuses_any_new_entry(self):
        self.sync(positions=[pos(10, OURS, MNQ, -1)])
        outcome, receipt = self.entry('long')
        self.assertEqual((outcome, receipt['reason']), ('rejected', 'entry requires a flat contract with no working orders'))
        self.assertEqual(self.page.placed(), [])

    def test_same_direction_elsewhere_is_allowed(self):
        self.sync(positions=[pos(10, OTHER_A, MNQ, 2)])
        self.assert_accepted(self.entry('long'))


class PeerLogins(unittest.TestCase):
    """Accounts on another login (peer_logins, written by Add login) count the same way."""

    def test_opposite_exposure_on_a_peer_login_conflicts(self):
        members = set(DEFAULTS['hedge_groups'][0])
        peer = {'accounts': [{'role': 'follower', 'configured': True,
                              'positions': [{'contract': 'NQZ6', 'root': 'NQ', 'net': -1}], 'working': []}]}
        self.assertEqual(account_conflict(peer, 'long', members), 'NQ')
        self.assertIsNone(account_conflict(peer, 'short', members))


if __name__ == '__main__':
    unittest.main()
