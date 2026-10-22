"""Soup through the Tradovate bridge's /order route. Real Soup core, fake bridge, synthetic fixtures."""
import copy
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_fixture import FakeBridge, native_binding  # noqa: E402
import soup_fixture_contract  # noqa: E402,F401  (fixtures carry synthetic strategy constants)
from decrypt_trading import RuntimeManager, RuntimeError as EngineError  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402
from decrypt_trading import bridge_route  # noqa: E402
from decrypt_trading.bridge_route import BindingError, read_token  # noqa: E402

BASE = 1614783605000
ENTRY = json.loads(Path(__file__).with_name('test_trading_runtime_entry.json').read_text())
MODULE_TRANSPORT = (Path(__file__).resolve().parents[1]
                    / 'modules/tradovate-bridge/bridge/tvbridge/soup_transport.py')
COPIED_TRANSPORT = (Path(__file__).resolve().parents[1]
                    / 'app/trading-runtime/decrypt_trading/bridge_transport.py')


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        net = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        net.start()
        self.addCleanup(net.stop)
        self.now = BASE
        self.root = Path(self.temp.name)
        self.binding = native_binding(offset=.75)
        self.bridge = FakeBridge(self.root / 'data', self.binding, clock=lambda: self.now)


class RouteUnits(Base):
    def test_copied_bridge_transport_matches_the_module(self):
        self.assertEqual(COPIED_TRANSPORT.read_bytes(), MODULE_TRANSPORT.read_bytes())

    def test_bridge_must_report_exactly_this_account(self):
        expect = bridge_route.expected_account(self.binding)
        self.assertIsNone(bridge_route.binding_problem(expect, self.bridge.state()))
        for change, reason in (({'account_id': 999}, 'bridge_account_id_mismatch'),
                               ({'account_name_sha256': '0' * 64}, 'bridge_account_name_mismatch'),
                               ({'soup_account_id': 'other'}, 'bridge_soup_account_mismatch'),
                               ({'contract': {'name': 'MNQH7'}}, 'bridge_contract_mismatch'),
                               ({'publish_mode': 'evidence'}, 'bridge_publish_mode_mismatch')):
            self.bridge.state_override = change
            self.assertEqual(bridge_route.binding_problem(expect, self.bridge.state()), reason)
        self.assertEqual(bridge_route.binding_problem(expect, None), 'bridge_state_unavailable')

    def test_open_refuses_other_engines_and_mismatched_config(self):
        # A pinned pair without a registered bridge adapter has no route.
        with patch.dict(contract.PAIRS, {'unregistered-pair': ('1' * 64, '2' * 64)}), \
                self.assertRaisesRegex(BindingError, 'bridge_route_adapter_not_registered'):
            self.bridge.opener(native_binding(pair='unregistered-pair'))
        self.bridge.write_config(soup_account_id='someone-else')
        with self.assertRaisesRegex(BindingError, 'bridge_soup_account_mismatch'):
            self.bridge.opener(self.binding)
        self.bridge.write_config(publish_mode='evidence')
        with self.assertRaisesRegex(BindingError, 'bridge_publish_mode_mismatch'):
            self.bridge.opener(self.binding)
        (self.bridge.tvbridge / 'config.json').unlink()
        with self.assertRaisesRegex(BindingError, 'bridge_not_configured'):
            self.bridge.opener(self.binding)

    def test_token_file_must_be_private(self):
        env = self.bridge.tvbridge / 'tvbridge.env'
        self.assertTrue(read_token(env).startswith('fixture-order-token'))
        os.chmod(env, 0o644)
        with self.assertRaisesRegex(BindingError, 'bridge_env_not_private'):
            read_token(env)

    def test_send_refuses_on_account_mismatch_and_otherwise_sends_the_payload_unchanged(self):
        transport = self.bridge.opener(self.binding)
        payload = {'ticker': 'MNQZ2026', 'action': 'cancel', 'extras': {'soup_command_id': 'c', 'soup_trade_id': 't:long'}}
        self.bridge.state_override = {'account_id': 999}
        outcome, receipt = transport.send(payload)
        self.assertEqual((outcome, receipt['reason']), ('rejected', 'bridge_account_id_mismatch'))
        self.assertEqual(self.bridge.sent, [])
        self.bridge.state_override = {}
        transport.route.latest = None
        self.assertEqual(transport.send(payload)[0], 'accepted')
        self.assertEqual(self.bridge.sent, [payload])


class RuntimeOnBridge(Base):
    def setUp(self):
        super().setUp()
        self.manager = RuntimeManager(self.root / 'trading')
        self.manager.install(self.binding)
        self.transport = self.bridge.opener(self.binding)
        self.runtime = self.manager.open(self.binding['instanceId'], transport=self.transport, clock=lambda: self.now)
        self.addCleanup(self.runtime.close)

    def ingest(self, channel, value):
        producer = self.binding['signalProducerId' if channel == 'pine' else 'feedProducerId']
        self.runtime.ingest(channel, json.dumps(value), producer_id=producer)
        self.runtime.step()

    def quote(self, price=10000.):
        self.ingest('quote', {'event_id': 'quote-%d' % self.now, 'symbol': 'MNQZ2026', 'time': self.now, 'price': price})

    def enter(self):
        self.quote()
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', copy.deepcopy(ENTRY))

    def test_open_links_the_soup_files_the_bridge_reads(self):
        soup = self.bridge.soup
        self.assertEqual(os.readlink(soup / 'current'), str(self.runtime.directory))
        self.assertEqual(json.loads((soup / 'pairing.json').read_text()), self.runtime.engine.config)
        self.assertEqual(json.loads((soup / 'quantity-policy.json').read_text())['max_contracts'], 10)
        self.transport.tick(self.runtime)
        context = json.loads((soup / 'broker-context.json').read_text())
        self.assertEqual(context['pairing']['account_id'], self.binding['accountId'])
        self.assertEqual(context['engine']['broker_seq'], 0)
        self.assertEqual(json.loads((soup / 'control.json').read_text()), {'enabled': False, 'locked': True})

    def test_entry_waits_for_fresh_flat_bridge_state_then_sends_soups_exact_payload(self):
        self.enter()
        self.assertEqual(self.bridge.sent, [], 'the first gate call only asks for fresh state')
        self.transport.route.refresh()
        self.runtime.step()
        commands = {c['kind']: c for c in self.runtime.engine.data['commands'].values()}
        message = self.runtime.modules['transport'].message
        # The bracket entry, then Soup's own TP1 limit (strategy target less the 0.75-handle offset).
        self.assertEqual(self.bridge.sent, [message(commands[k], 'MNQZ2026', 'long', quantity_limit=10)
                                            for k in ('entry', 'tp1_limit')])
        self.assertEqual(self.bridge.sent[0]['extras']['bridge_kind'], 'bracket_entry')
        self.assertEqual(self.bridge.sent[1]['limitPrice'], 10016.25)
        self.assertEqual(commands['entry']['status'], 'accepted')
        self.assertEqual(commands['entry']['receipt'], {'broker_order_id': '9001', 'transport_outcome': 'accepted'})

    def test_alert_sizes_are_sent_as_is_with_native_exits(self):
        # No contract cap: a large alert size is sent as is, even when it differs from the snapshot's tranche settings.
        big = copy.deepcopy(ENTRY)
        big.update(quantity=17, tp1_qty=15)
        self.quote()
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', big)
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual([(x['extras'].get('bridge_kind'), x.get('quantity')) for x in self.bridge.sent],
                         [('bracket_entry', 17), (None, 15)])
        self.assertEqual(self.runtime.engine.data['trade']['alloc'], {'tp1': 15, 'parent': 1, 'bias': 0, 'eod': 1})

    def test_entry_is_held_while_the_bridge_is_not_flat_and_dropped_after_the_decision_deadline(self):
        self.bridge.state_override = {'net_position': 1}
        self.enter()
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual(self.bridge.sent, [])
        self.now += 121000
        self.quote()
        self.assertEqual(self.bridge.sent, [])
        self.assertEqual(self.runtime.engine.data['trade']['phase'], 'closed')

    def test_paused_bridge_or_unreachable_state_never_sends_an_entry(self):
        for override in ({'entries_disabled': True}, {'unseen_accepted_orders': ['1']}):
            self.bridge.state_override = override
            self.assertFalse(self.transport.route.entry_ready({'trade': None, 'commands': {}, 'last_quote': None}, self.now, BASE))
        self.bridge.state_override = {}
        self.bridge.available = False
        self.enter()
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual(self.bridge.sent, [])

    def test_broker_events_from_the_adapter_database_reach_the_engine_once(self):
        self.enter()
        self.transport.route.refresh()
        self.runtime.step()
        trade = self.runtime.engine.data['trade']
        event = {'event_id': 'tvbridge-1', 'sequence': 1, 'observed_at': self.now, 'account_id': self.binding['accountId'],
                 'symbol': 'MNQZ2026', 'trade_id': trade['id'], 'side': 'long', 'position_qty': 0,
                 'orders': [], 'open_orders': []}
        self.bridge.publish(1, event)
        self.bridge.publish(2, dict(event, sequence=2, event_id='tvbridge-2'), observed_at=self.now - 60000)
        self.transport.tick(self.runtime)
        self.assertEqual(self.runtime.bridge_cursor(), 2)
        rows = self.runtime.engine.db.execute("SELECT body FROM inbox WHERE channel='broker'").fetchall()
        self.assertEqual([json.loads(r[0])['event_id'] for r in rows], ['tvbridge-1'], 'the stale event is skipped')
        self.transport.tick(self.runtime)
        self.assertEqual(len(self.runtime.engine.db.execute("SELECT 1 FROM inbox WHERE channel='broker'").fetchall()), 1)

    def test_adapter_sequence_gap_stops_reading(self):
        self.bridge.publish(1, {'event_id': 'x'})
        self.transport.tick(self.runtime)
        self.bridge.publish(3, {'event_id': 'y'})
        self.transport.tick(self.runtime)
        self.assertEqual(self.runtime.bridge_cursor(), 1)
        self.assertIn('gap', self.transport.events.status['error'])

    def test_failed_native_exit_holds_entries_and_pauses_the_bridge(self):
        self.enter()
        self.runtime.engine.data['trade']['native'] = {'failed': {'reason': 'exit_unverified'}}
        self.runtime.set_controls(enabled=True, locked=False)
        self.transport.tick(self.runtime)
        self.assertTrue(self.runtime.status()['entriesHeld'])
        self.assertTrue(self.bridge.kill_switch.exists())
        self.assertTrue((self.bridge.soup / 'ENTRIES-BLOCKED-NATIVE-EXIT').exists())
        self.assertFalse(self.runtime._entry_ready(self.now))

    def test_bridge_route_refuses_other_confirmation_paths(self):
        with self.assertRaises(EngineError):
            self.runtime.publish_confirmation({}, producer_id=self.binding['brokerProducerId'])
        with self.assertRaises(EngineError):
            self.runtime.ingest('broker', b'{}', producer_id=self.binding['brokerProducerId'])

    def test_flat_state_is_the_retirement_evidence(self):
        self.assertIsNotNone(self.transport.route.flat_state())
        self.bridge.state_override = {'open_order_ids': ['7']}
        self.assertIsNone(self.transport.route.flat_state())


class AttachLater(Base):
    def test_engine_installed_before_the_bridge_attaches_its_route_later(self):
        manager = RuntimeManager(self.root / 'trading')
        manager.install(self.binding)
        runtime = manager.open(self.binding['instanceId'], clock=lambda: self.now)
        self.addCleanup(runtime.close)
        self.assertIsNone(runtime.bridge)
        self.assertFalse(runtime._route_ready())
        runtime.attach_bridge(self.bridge.opener(self.binding))
        self.assertTrue(runtime._route_ready())
        other = native_binding('other', account=777)
        with self.assertRaises(EngineError):
            runtime.attach_bridge(self.bridge.opener(self.binding))
        with self.assertRaises(BindingError):
            self.bridge.opener(other)


if __name__ == '__main__':
    unittest.main()
