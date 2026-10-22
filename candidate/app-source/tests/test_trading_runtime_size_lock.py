"""Size Lock module (default off): a Soup-only quantity-policy switch.

Disabled (default): the existing 'account-original-allocation-micro-cap-v1'
policy, unchanged (10 contracts, original tranche split). Enabled: the
engine's own 'account-fixed-one-tp1-v1' policy (1 contract, assigned to the
original TP1 trigger, original protective exits). There is no free-form or
arbitrary fixed-N size; this only switches between the engine's two reviewed
quantity policies (vendor/soup_r4/core.py, validate_quantity_policy).
"""
import tempfile
import unittest
from pathlib import Path

from decrypt_trading import customer_contract as native_contract
from decrypt_trading.runtime import RuntimeManager, RuntimeError as RuntimeManagerError


def native_binding(name='size-lock-fixture', login='login-1'):
    engine_sha, strategy_sha = native_contract.PAIRS[native_contract.SOUP]
    value = {
        'schema': native_contract.BINDING, 'provider': 'tradovate', 'loginGroupId': login,
        'instanceId': name, 'customerId': 'fixture-customer', 'installationId': 'fixture-install',
        'pairId': native_contract.SOUP, 'engineSha256': engine_sha, 'strategySha256': strategy_sha,
        'accountId': name + '-account', 'accountMode': 'demo', 'nativeAccountId': 12345,
        'nativeAccountName': 'Fixture Demo', 'routeId': name + '-route', 'routeSha256': '0' * 64,
        'feedId': 'fixture-feed', 'streamId': 'fixture-stream', 'symbol': 'MNQZ2026', 'root': 'MNQ',
        'tickSize': .25, 'tp1LimitOffsetHandles': 0, 'signalProducerId': 'fixture-signals',
        'feedProducerId': 'fixture-quotes', 'brokerProducerId': 'fixture-broker',
    }
    value['routeSha256'] = native_contract.route_digest(value)
    return value


class SizeLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = RuntimeManager(Path(self.temp.name) / 'instances')
        self.binding = native_binding()
        self.manager.install(self.binding)

    def test_default_is_off_and_uses_original_allocation_policy(self):
        self.assertFalse(self.manager.size_lock_enabled(self.binding['instanceId']))
        runtime = self.manager.open(self.binding['instanceId'])
        try:
            self.assertEqual(runtime.engine.quantity_policy['id'], 'account-original-allocation-micro-cap-v1')
            self.assertEqual(runtime.engine.quantity_policy['max_contracts'], 10)
            self.assertEqual(runtime.engine.quantity_policy['exit_allocation'], 'original')
            self.assertFalse(runtime.status()['sizeLockEnabled'])
        finally:
            runtime.close()

    def test_enabling_switches_to_fixed_one_tp1_only_policy_and_back(self):
        self.manager.set_size_lock(self.binding['instanceId'], True)
        self.assertTrue(self.manager.size_lock_enabled(self.binding['instanceId']))
        runtime = self.manager.open(self.binding['instanceId'])
        try:
            self.assertEqual(runtime.engine.quantity_policy['id'], 'account-fixed-one-tp1-v1')
            self.assertEqual(runtime.engine.quantity_policy['max_contracts'], 1)
            self.assertEqual(runtime.engine.quantity_policy['exit_allocation'], 'tp1_only')
            self.assertTrue(runtime.status()['sizeLockEnabled'])
        finally:
            runtime.close()
        # Disabling it again restores the original policy exactly.
        self.manager.set_size_lock(self.binding['instanceId'], False)
        runtime = self.manager.open(self.binding['instanceId'])
        try:
            self.assertEqual(runtime.engine.quantity_policy['id'], 'account-original-allocation-micro-cap-v1')
        finally:
            runtime.close()

    def test_non_boolean_value_is_rejected(self):
        with self.assertRaises(RuntimeManagerError):
            self.manager.set_size_lock(self.binding['instanceId'], 'yes')

    def test_unsupported_for_non_native_or_non_soup_binding(self):
        from test_trading_runtime import binding as generic_binding
        self.manager.install(generic_binding('generic-fixture'))
        with self.assertRaises(RuntimeManagerError):
            self.manager.set_size_lock('generic-fixture', True)


if __name__ == '__main__':
    unittest.main()
