"""Soup r4 alert intake: the synthetic strategy alerts through the real engine intake.

`soup_alerts_fixture/` holds synthetic fixtures in the Soup alert format (JSON only; no Pine is in this repo;
arbitrary settings and the synthetic constants of soup_fixture_contract.py): v3 entries, v3 management
(`pine_facts`) alerts covering every fact kind (one alert batches two facts), and v4 entries (v3 plus the
strategy's own `tp1_limit_offset_handles`). v3 must keep
working unchanged; v4 is the Decrypt OS addition. Every alert goes through `Runtime.admit_authenticated`,
exactly what `ingress.SignalIngress` calls for a registered adapter: the adapter's declared schema lookup
(`bridge_adapters.lookup`) plus the engine's `validate_pine` contract check. No network, no browser, no real
accounts.
"""
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soup_fixture_contract  # noqa: E402,F401  (fixtures carry synthetic strategy constants)
from test_trading_runtime import binding as soup_binding  # noqa: E402
from decrypt_trading import RuntimeManager  # noqa: E402

EXAMPLES = Path(__file__).with_name('soup_alerts_fixture')
V3_ENTRIES = ['entry-v3-long-basic.json', 'entry-v3-short-single.json', 'entry-v3-short-under-snapshot.json',
              'entry-v3-long-large.json', 'entry-v3-short-runners.json', 'entry-v3-long-no-hard-stop.json']
V3_FACTS = ['facts-bias-flip.json', 'facts-stop-bias-flip.json', 'facts-sweep-and-ifvg.json',
            'facts-progress-fail.json']
V4_ENTRIES = ['entry-v4-short-quarter.json', 'entry-v4-long-large.json', 'entry-v4-short-runners.json']
ALL = V3_ENTRIES + V3_FACTS + V4_ENTRIES


def example(name):
    return json.loads((EXAMPLES / name).read_text())


def admit(runtime, payload):
    """The production Pine intake (ingress.SignalIngress) without the webhook secret check."""
    return runtime.admit_authenticated('pine', json.dumps(payload), producer_id=runtime.binding['signalProducerId'],
                                       event_id=runtime.input_identity(payload))


class SoupBridgeV2Examples(unittest.TestCase):
    """Intake only: every example through the real adapter lookup and validate_pine."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.count = 0

    def runtime(self, config, now):
        self.count += 1
        manager = RuntimeManager(Path(self.temp.name) / ('i%d' % self.count))
        manager.install(config)
        runtime = manager.open(config['instanceId'], clock=lambda: now)
        self.addCleanup(runtime.close)
        return runtime

    def admitted(self, name):
        p = example(name)
        config = soup_binding('soup-' + name)
        config['streamId'], config['symbol'] = p['stream_id'], p['symbol']
        now = p['execution_context']['decision_time'] + 100
        runtime = self.runtime(config, now)
        receipt = admit(runtime, p)
        self.assertFalse(receipt['duplicate'])

    def test_fixture_set_is_complete(self):
        names = {p.name for p in EXAMPLES.glob('*.json')}
        self.assertEqual(names, set(ALL))

    def test_v3_entries_accepted(self):
        for name in V3_ENTRIES:
            with self.subTest(name=name):
                self.admitted(name)

    def test_v3_management_facts_accepted(self):
        for name in V3_FACTS:
            with self.subTest(name=name):
                self.admitted(name)

    def test_v4_entries_accepted(self):
        for name in V4_ENTRIES:
            with self.subTest(name=name):
                self.admitted(name)

    def test_soup_adapter_declares_both_entry_schemas_and_management_v3(self):
        from decrypt_trading import bridge_adapters
        pair_id = soup_binding('soup-lookup')['pairId']
        for schema in ('decrypt-soup-entry-v3', 'decrypt-soup-entry-v4', 'decrypt-soup-management-v3'):
            with self.subTest(schema=schema):
                bridge_adapters.lookup(pair_id, schema)  # raises if undeclared
        with self.assertRaises((ValueError, TypeError, KeyError, AttributeError)):
            bridge_adapters.lookup(pair_id, 'decrypt-soup-entry-v2')


if __name__ == '__main__':
    unittest.main()
