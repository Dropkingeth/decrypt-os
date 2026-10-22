"""decrypt-bridge-v2 on the bridge: a v2 adapter (one V2_ADAPTERS line) sends the receiver's order sequences
through Soup's own order guards. Browser-free: test_fill_capture's exchange double; synthetic ids only."""
import unittest
from unittest import mock

import harness  # noqa: F401  (puts the repository on sys.path; no browser is started here)
from test_adapters_platform import MesBench, cmd
from tvbridge import adapters

ADAPTER = 'synthetic-bridge-v2'
TID = '3000:MESZ2021:1632147200000:long'


def v2(kind, cid, **fields):
    return cmd(kind, cid, schema=adapters.V2_SCHEMA, adapter_id=ADAPTER, **fields)


class BridgeV2(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(adapters.REGISTRY, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Exactly what one line in V2_ADAPTERS registers: every kind, the alert's own resting targets (3 here).
        adapters.register(adapters.Adapter(ADAPTER, 'Synthetic v2', schemas=(adapters.V2_SCHEMA,), roots=('MES', 'MNQ'),
                                           kinds=adapters.V2_KINDS, take_profits=3, profile=adapters.V2_PROFILE))
        self.b = MesBench(self, adapters={ADAPTER: {'enabled': True}})
        self.main = self.b.main

    def ok(self, payload):
        outcome, receipt = self.main.send(payload)
        self.assertEqual(outcome, 'accepted', receipt)
        return receipt

    def refused(self, payload, fragment):
        outcome, receipt = self.main.send(payload)
        self.assertEqual(outcome, 'rejected', receipt)
        self.assertIn(fragment, receipt['reason'])

    def test_v2_registration_shape(self):
        adapter = adapters.lookup(ADAPTER, 'decrypt-bridge-v2')
        self.assertEqual((adapter.kinds, adapter.take_profits), (adapters.ORDER_KINDS, 3))
        # The core itself registers nothing (checked when the bridge-v2 core landed); strategies
        # add their own line afterward (one per real strategy, PAYLOAD-V2.md "Adding a strategy"). Each line
        # must stay well-formed: a unique lowercase-dash id, a label, a roots tuple, 1-3 resting targets.
        seen = set()
        for adapter_id, label, roots, resting in adapters.V2_ADAPTERS:
            self.assertNotIn(adapter_id, seen, 'duplicate V2_ADAPTERS id')
            seen.add(adapter_id)
            self.assertTrue(label.strip())
            self.assertTrue(roots and all(isinstance(r, str) for r in roots))
            self.assertIn(resting, (1, 2, 3))
        # Regression pins for each strategy's exact registered line.
        self.assertIn(('fvg-r5b-bridge-v2', 'FVG/IFVG', ('MNQ',), 3), adapters.V2_ADAPTERS)
        self.assertIn(('lumi-gap-bridge-v2', 'Lumi RTH Gap', ('MES',), 3), adapters.V2_ADAPTERS)
        self.assertIn(('ote-refined-large-bridge-v2', 'OTE Refined Large (v2)', ('MNQ',), 2), adapters.V2_ADAPTERS)
        self.assertIn(('ote-refined-small-bridge-v2', 'OTE Refined Small (v2)', ('MNQ',), 2), adapters.V2_ADAPTERS)
        self.assertIn(('ote-active-bridge-v2', 'OTE Active (Silver Bullet)', ('MNQ',), 2), adapters.V2_ADAPTERS)
        self.assertIn(('order-blocks-bridge-v2', 'Order Blocks Decoded', ('MNQ',), 1),
                      adapters.V2_ADAPTERS)

    def test_bracket_with_three_resting_targets_stop_moves_and_flatten(self):
        receipt = self.ok(v2('bracket_entry', 'v-1', quantity=4, tp1_price=5006.25, tp1_quantity=1))
        self.assertEqual(receipt['tp1']['outcome'], 'accepted')
        self.ok(v2('tp1_limit', 'v-2', quantity=1, price=5011.25))
        self.ok(v2('tp1_limit', 'v-3', quantity=1, price=5017.5))
        self.refused(v2('tp1_limit', 'v-4', quantity=1, price=5023.75), 'its profile allows 3')
        # The receiver never sends a market exit while a take-profit limit rests: the bridge would refuse it.
        self.refused(v2('exit_market', 'v-5', quantity=1), 'working orders')
        self.ok(v2('modify_stop', 'v-6', stop_price=4997.5, stop_quantity=4))       # break-even style move
        self.assertEqual(self.ok(v2('flatten_all', 'v-7'))['mode'], 'liquidate')
        self.assertEqual(self.b.ex.net(7), 0)

    def test_market_target_shrinks_the_stop_first_then_exits(self):
        self.ok(v2('bracket_entry', 'm-1', quantity=4))
        self.refused(v2('exit_market', 'm-2', quantity=1), 'shrink the stop first')
        self.ok(v2('modify_stop', 'm-3', stop_price=4992.5, stop_quantity=3))
        self.ok(v2('exit_market', 'm-4', quantity=1))
        self.assertEqual(self.b.ex.net(7), 3)

    def test_bar_close_plan_uses_a_plain_entry_and_limits(self):
        self.ok(v2('entry', 'p-1', quantity=3))
        self.ok(v2('tp1_limit', 'p-2', quantity=1, price=5006.25))
        self.ok(v2('tp1_limit', 'p-3', quantity=1, price=5017.5))
        self.ok(v2('flatten_all', 'p-4'))
        self.assertEqual(self.b.ex.net(7), 0)

    def test_no_contract_cap_on_v2_entries(self):
        self.ok(v2('bracket_entry', 'c-1', quantity=40))
        self.assertEqual(self.b.ex.net(7), 40)



class ShippedAdapters(unittest.TestCase):
    """The real registry: decrypt-bridge-v2 is the only non-Soup format; Lumi's v2 adapter is MES only and off."""

    LUMI = 'lumi-gap-bridge-v2'

    def test_only_soup_and_v2_adapters_are_registered(self):
        others = {a.adapter_id: a.schemas for a in adapters.REGISTRY.values() if not a.reference}
        self.assertEqual(set(others), {line[0] for line in adapters.V2_ADAPTERS})
        self.assertEqual(set(others.values()), {(adapters.V2_SCHEMA,)})
        self.assertEqual(adapters.lookup(adapters.SOUP).schemas,
                         ('decrypt-soup-entry-v3', 'decrypt-soup-entry-v4', 'decrypt-soup-management-v3'))

    def test_lumi_is_mes_only_and_off_unless_the_binding_enables_it(self):
        adapter = adapters.lookup(self.LUMI, adapters.V2_SCHEMA)
        self.assertEqual((adapter.roots, adapter.reference), (('MES',), False))
        self.assertEqual(adapters.binding_adapters({'ticker': 'MESZ2021', 'adapters': {self.LUMI: {'enabled': False}}}), {})
        with self.assertRaisesRegex(adapters.AdapterError, 'does not trade MNQ'):
            adapters.binding_adapters({'ticker': 'MNQZ2021', 'adapters': {self.LUMI: {'enabled': True}}})

    def test_setup_writes_an_enabled_mes_binding(self):
        import argparse
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location('v2_setup_under_test', Path(__file__).resolve().parents[2] / 'setup.py')
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        self.assertIsNone(setup.contract_problem('MESZ2021', .25, self.LUMI))
        self.assertIn('does not trade MNQ', setup.contract_problem('MNQZ2021', .25, self.LUMI))
        cfg = setup.daemon_config(argparse.Namespace(
            account_name='SIM-ACCOUNT-1', soup_account_id='30000001', ticker='MESZ2021', tick_size=.25,
            publish_mode='adapter-db', soup_state='/tmp/decrypt-os-test/soup', adapter=self.LUMI))
        self.assertEqual(cfg['adapters'], {self.LUMI: {'enabled': True}})
        self.assertEqual(list(adapters.binding_adapters(cfg)), [self.LUMI])

    def test_unregistered_adapter_ids_are_refused(self):
        for adapter_id in ('example-native-v1', 'example-bridge-v1', 'fvg-r5b-bridge-v3'):
            with self.subTest(adapter_id=adapter_id), self.assertRaisesRegex(adapters.AdapterError, 'not a registered adapter'):
                adapters.binding_adapters({'ticker': 'MNQZ2021', 'adapters': {adapter_id: {'enabled': True}}})


if __name__ == '__main__':
    unittest.main()
