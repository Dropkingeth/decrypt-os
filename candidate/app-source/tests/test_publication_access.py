"""Strategy catalog publication identity. Catalog data only; no requests."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publication_server', ROOT / 'app/backend/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
STRATEGIES = {item['id']: item for item in server.CATALOG['strategies']}


class PublicationCatalogTests(unittest.TestCase):
    def test_exact_publication_without_invented_version_or_engine_qualification(self):
        item = STRATEGIES['fvg-ifvg']
        self.assertEqual(item['name'], 'Determining Order Flow Engine [FVG+IFVG]')
        self.assertEqual(item['label'], item['name'])
        self.assertEqual(item['publicationId'], 'BhpDI6NS')
        self.assertNotIn('publicationAuthor', item)
        self.assertEqual(item['publicationUrl'], 'https://www.tradingview.com/script/BhpDI6NS-Determining-Order-Flow-Engine-FVG-IFVG/')
        self.assertEqual(item['tradingviewUrl'], item['publicationUrl'])
        self.assertEqual(item['publicationStatus'], 'published_invite_only')
        self.assertIsNone(item['version'])
        self.assertEqual(item['publicationVersionStatus'], 'not_established')
        self.assertEqual(item['customerEngineStatus'], 'bridge_adapter_off_by_default')
        self.assertEqual(item['runtimePairId'], 'fvg-r5b-bridge-v2')
        self.assertFalse(item['commissioningCandidate'])
        self.assertEqual(item['engineStatus'], 'hold')

    def test_soup_is_open_to_requests_without_an_invented_publication(self):
        item = STRATEGIES['soup']
        self.assertEqual(item['publicationStatus'], 'invite_only_publishing')
        self.assertIs(item['accessRequestAvailable'], True)
        for field in ('publicationUrl', 'publicationId', 'publicationAuthor'):
            self.assertNotIn(field, item)
        self.assertFalse(item['commissioningCandidate'])
        self.assertEqual(item['engineStatus'], 'live')
        self.assertEqual(item['tradingviewUrl'], 'https://www.tradingview.com/script/ulGrC1sn-ICT-Turtle-Soup-Strategy/')
        self.assertIs(item['listed'], True)

    def test_schema_is_v3_with_seven_strategies(self):
        self.assertEqual(server.CATALOG['schema'], 'decrypt.strategy-catalog.v3')
        self.assertEqual(len(server.CATALOG['strategies']), 7)
        self.assertEqual(set(STRATEGIES), {
            'soup', 'fvg-ifvg', 'lumi-rth-gap', 'ote-refined-large', 'ote-refined-small',
            'ote-silver-bullet', 'order-blocks-decoded'})

    def test_every_strategy_has_a_tradingviewurl_and_listed_field(self):
        for strategy_id, item in STRATEGIES.items():
            with self.subTest(strategy_id=strategy_id):
                self.assertIsInstance(item['tradingviewUrl'], str)
                self.assertIn(item['listed'], (True, False))

    def test_a_strategy_without_a_tradingview_url_is_not_requestable(self):
        # The request button is hidden with no tradingviewUrl; the catalog
        # itself must agree so no reader can show it anyway.
        for strategy_id, item in STRATEGIES.items():
            with self.subTest(strategy_id=strategy_id):
                if not item['tradingviewUrl']:
                    self.assertFalse(item['accessRequestAvailable'])

    def test_lumi_and_order_blocks_have_no_tradingview_link_yet(self):
        for strategy_id in ('lumi-rth-gap', 'order-blocks-decoded'):
            item = STRATEGIES[strategy_id]
            self.assertEqual(item['tradingviewUrl'], '')
            self.assertFalse(item['accessRequestAvailable'])
            self.assertEqual(item['engineStatus'], 'in_development')

    def test_ote_refined_ids_stay_stable_and_map_to_l_and_s(self):
        large, small = STRATEGIES['ote-refined-large'], STRATEGIES['ote-refined-small']
        self.assertIn('Refined L', large['name'])
        self.assertIn('Refined S', small['name'])
        self.assertEqual(large['tradingviewUrl'], 'https://www.tradingview.com/script/tFnhM8a2-Optimal-Trade-Entry-Limit-Refined-L/')
        self.assertEqual(small['tradingviewUrl'], 'https://www.tradingview.com/script/u4f1NySp-Optimal-Trade-Entry-Limit-Refined-S/')
        self.assertTrue(large['accessRequestAvailable'])
        self.assertTrue(small['accessRequestAvailable'])
        self.assertEqual(large['engineStatus'], 'in_development')
        self.assertEqual(small['engineStatus'], 'in_development')

    def test_ote_active_silver_bullet_is_parked_but_still_listed_and_requestable(self):
        item = STRATEGIES['ote-silver-bullet']
        self.assertEqual(item['engineStatus'], 'parked')
        self.assertIs(item['listed'], True)
        self.assertTrue(item['accessRequestAvailable'])
        self.assertEqual(item['tradingviewUrl'], 'https://www.tradingview.com/script/szwjifCr-Optimal-Trade-Entry-Active-Silver-Bullet/')



class BridgeCatalogTests(unittest.TestCase):
    """decrypt-bridge-v2 is the only shipped non-Soup path: the catalogs name exactly the registered adapters."""

    def setUp(self):
        import json
        import sys
        sys.path.insert(0, str(ROOT / 'app/trading-runtime'))
        from decrypt_trading import bridge_adapters, bridge_v2_registry
        self.adapters, self.registry = bridge_adapters, bridge_v2_registry
        self.engines = json.loads((ROOT / 'app/engine-module-catalog.json').read_text())

    def test_every_strategy_points_to_its_registered_adapter_with_alert_setup_notes(self):
        v2 = {line[0]: line[2] for line in self.adapters.V2_ADAPTERS}
        self.assertEqual(set(self.registry.STRATEGIES), set(v2))
        for strategy_id, item in STRATEGIES.items():
            with self.subTest(strategy_id=strategy_id):
                bridge = item['bridge']
                self.assertEqual(item['runtimePairId'], bridge['adapterId'])
                self.assertTrue(bridge['chart'] and bridge['alertSetup'])
                self.assertTrue(all(isinstance(n, str) and n.strip() for n in bridge['alertSetup']))
                if strategy_id == 'soup':
                    self.assertEqual(bridge['adapterId'], self.adapters.SOUP)
                    continue
                self.assertEqual(bridge['alertFormat'], self.adapters.V2_SCHEMA)
                self.assertIn(bridge['adapterId'], v2)
                self.assertEqual((bridge['contractRoot'],), v2[bridge['adapterId']])
        self.assertEqual({s['bridge']['adapterId'] for s in STRATEGIES.values()} - {self.adapters.SOUP}, set(v2))

    def test_no_retired_format_is_named(self):
        import re
        retired = re.compile(r'native-v1|bridge-v1|ict-ote-refined|decrypt-lumi-(entry|management)|09-11')
        for name in ('app/strategy-catalog.json', 'app/engine-module-catalog.json'):
            with self.subTest(name=name):
                self.assertIsNone(retired.search((ROOT / name).read_text()))

    def test_engine_catalog_pins_match_the_registry(self):
        pairs = {p['id']: p for p in self.engines['sourcePairs']}
        for engine in self.engines['engines']:
            if engine['id'] == 'soup':
                continue
            with self.subTest(engine=engine['id']):
                adapter_id = engine['bridgeAdapter']['adapterId']
                self.assertEqual(engine['sourcePairId'], adapter_id)
                self.assertEqual(STRATEGIES[engine['strategyId']]['runtimePairId'], adapter_id)
                _, engine_sha, strategy_sha = self.registry.STRATEGIES[adapter_id]
                pins = {a['role']: a['sha256'] for a in pairs[adapter_id]['artifacts']}
                self.assertEqual(pins, {'engine': engine_sha, 'strategy': strategy_sha})


if __name__ == '__main__':
    unittest.main()
