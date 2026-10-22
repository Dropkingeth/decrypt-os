"""Real preserved core, private temporary state, synthetic native/transport inputs.

These tests forbid network and establish no native provider qualification.
"""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/trading-runtime'))
from decrypt_trading import RuntimeManager, RuntimeError, Conflict
from decrypt_trading.source import manifest, load_sources, PINS, STRATEGY_PIN, PAIR_ID
sys.path.insert(0, str(Path(__file__).resolve().parent))
import soup_fixture_contract  # noqa: E402,F401  (fixtures carry synthetic strategy constants)

BASE = 1614783605000
ENTRY = json.loads(Path(__file__).with_name('test_trading_runtime_entry.json').read_text())


def binding(name='fixture-one'):
    return {'instanceId': name, 'customerId': 'fixture-customer', 'installationId': 'fixture-install',
            'pairId': PAIR_ID, 'engineSha256': PINS['core.py'], 'strategySha256': STRATEGY_PIN,
            'accountId': name + '-account', 'accountMode': 'demo', 'routeId': name + '-route',
            'subscriptionId': name + '-subscription', 'endpointSha256': hashlib.sha256(name.encode()).hexdigest(),
            'feedId': 'fixture-feed', 'streamId': 'fixture-stream', 'symbol': 'MNQZ2026', 'root': 'MNQ',
            'tickSize': .25, 'tp1LimitOffsetHandles': .75, 'signalProducerId': 'fixture-signals',
            'feedProducerId': 'fixture-quotes', 'brokerProducerId': 'fixture-broker'}


class FakeTransport:
    def __init__(self, config):
        self.account_id, self.route_id = config['accountId'], config['routeId']
        self.endpoint_sha256 = config['endpointSha256']
        self.sent = []
        self.outcome = 'accepted'
        self.receipt = None
    def send(self, envelope):
        self.sent.append(copy.deepcopy(envelope))
        if self.outcome == 'timeout':
            raise TimeoutError('never reveal transport diagnostics')
        return self.outcome, self.receipt or {'id': 'fixture-receipt-' + str(len(self.sent))}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.net = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.now = BASE
        self.config = binding()
        self.manager = RuntimeManager(Path(self.temp.name) / 'instances')
        self.manager.install(self.config)
        self.transport = FakeTransport(self.config)
        self.runtime = self.manager.open(self.config['instanceId'], transport=self.transport, clock=lambda: self.now)
        self.addCleanup(lambda: self.runtime.close())
        self.seq = 0
        self.orders = {}
        self.entry = copy.deepcopy(ENTRY)

    def confirmation(self, **changes):
        self.seq += 1
        out = {'schema': 'decrypt.customer-broker-confirmation.v1',
               **{k: self.config[k] for k in ('instanceId', 'accountId', 'pairId', 'symbol', 'accountMode', 'routeId', 'subscriptionId', 'endpointSha256')},
               'sequence': self.seq, 'observedAt': self.now, 'positionsComplete': True, 'ordersComplete': True,
               'pendingComplete': True, 'correlationComplete': True, 'routeVerified': True,
               'entryRecipients': [self.config['accountId']], 'positionQuantity': 0, 'workingOrderIds': [],
               'pendingOrderIds': [], 'unattributedOrderIds': [], 'otherContractExposure': False, 'engineEvent': None}
        out.update(changes)
        return out

    def publish(self, value):
        return self.runtime.publish_confirmation(value, producer_id=self.config['brokerProducerId'])

    def ingest(self, channel, value):
        return self.runtime.ingest(channel, json.dumps(value), producer_id=self.config['signalProducerId' if channel == 'pine' else 'feedProducerId'])

    def quote(self, price=10000.):
        self.ingest('quote', {'event_id': 'quote-' + str(self.now), 'symbol': self.config['symbol'], 'time': self.now, 'price': price})
        self.runtime.step()

    def prepare(self):
        self.publish(self.confirmation())
        self.quote()
        self.runtime.set_controls(enabled=True, locked=False)

    def enter(self):
        self.prepare()
        self.ingest('pine', self.entry)
        self.runtime.step()

    def command(self, kind):
        return next(c for c in reversed(list(self.runtime.engine.data['commands'].values())) if c['kind'] == kind)

    def native(self, updates):
        for kind, status, quantity in updates:
            c = self.command(kind)
            prior = self.orders.get(c['id'])
            fill_time = prior['fill_time'] if prior and prior['filled_qty'] == quantity else self.now
            self.orders[c['id']] = {'command_id': c['id'], 'broker_order_id': 'native-' + c['id'],
                                   'status': status, 'filled_qty': quantity, 'fill_time': fill_time}
        position = sum(o['filled_qty'] * (1 if self.runtime.engine.data['commands'][cid]['kind'] == 'entry' else -1)
                       for cid, o in self.orders.items())
        working = [o['broker_order_id'] for o in self.orders.values() if o['status'] in ('working', 'partial')]
        snapshot = self.confirmation(positionQuantity=position, workingOrderIds=working)
        snapshot['engineEvent'] = {'event_id': 'broker-' + str(self.seq), 'sequence': self.seq, 'observed_at': self.now,
                                   'account_id': self.config['accountId'], 'symbol': self.config['symbol'],
                                   'trade_id': self.entry['trade_id'], 'side': self.entry['side'], 'position_qty': position,
                                   'orders': list(self.orders.values()), 'open_orders': working}
        result = self.publish(snapshot)
        self.runtime.step()
        return result

    def facts(self, kind='stop_bias_flip'):
        p = copy.deepcopy(self.entry)
        fields = self.runtime.engine.rules['facts_fields']
        p = {k: v for k, v in p.items() if k in fields}
        p.update(schema_version='decrypt-soup-management-v3', event='pine_facts', signalId='facts-' + str(self.now),
                 facts=[{'kind': kind, 'close_side': self.entry['side']}], timestamp=self.now - 5000,
                 root=self.config['root'], price=10000.)
        p['execution_context'].update(decision_time=self.now, entry_bar_open=self.now - 5000)
        self.ingest('pine', p)
        self.runtime.step()

    def test_manifest_preserves_exact_source_bytes(self):
        value = manifest()
        self.assertNotIn('historicalPairs', value)   # the pre-v2 historical pairs are retired
        sources = load_sources()
        self.assertEqual(sources['core'].REVISION, 'soup-pine-authority-2026-09-13-r4')

    def test_locked_route_before_entry_is_known_unsent_not_unknown(self):
        @contextmanager
        def locked(envelope):
            raise ValueError('vault_locked')
            yield
        self.transport.prepare = locked
        self.enter()
        self.assertEqual(self.transport.sent, [])
        self.assertTrue(self.runtime.status()['entriesHeld'])
        self.assertFalse(any(c['status'] in ('started', 'unknown') for c in self.runtime.engine.data['commands'].values()))

    def test_locked_route_preserves_unsent_management_until_unlock(self):
        self.enter()
        self.native([('entry', 'filled', 4), ('tp1_limit', 'working', 0)])
        @contextmanager
        def locked(envelope):
            raise ValueError('vault_locked')
            yield
        self.transport.prepare = locked
        self.now += 5000
        self.facts()
        self.assertEqual(self.command('cancel')['status'], 'ready')
        self.assertTrue(self.runtime.status()['entriesHeld'])
        del self.transport.prepare
        self.runtime.step()
        self.assertEqual(self.command('cancel')['status'], 'accepted')

    def test_repeated_native_invalidation_does_not_grow_audit(self):
        self.publish(self.confirmation())
        for _ in range(4):
            self.runtime.invalidate_confirmation('qualification_missing', producer_id=self.config['brokerProducerId'])
        rows = self.runtime.engine.db.execute("SELECT count(*) FROM customer_runtime_audit WHERE kind='native_confirmation_invalidated'").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertFalse(self.runtime.status()['nativeFresh'])

    def test_internal_dispatch_guard_blocks_ready_management_before_send(self):
        self.enter()
        self.native([('entry', 'filled', 4), ('tp1_limit', 'working', 0)])
        self.runtime.dispatch_guard = lambda: False
        self.now += 5000
        self.facts()
        self.assertEqual(self.command('cancel')['status'], 'ready')
        self.assertEqual(len(self.transport.sent), 2)
        self.assertTrue(self.runtime.status()['entriesHeld'])
        self.runtime.dispatch_guard = lambda: True
        self.runtime.step()
        self.assertEqual(len(self.transport.sent), 3)

    def test_internal_dispatch_guard_exception_is_not_permission(self):
        def failed(): raise OSError('guard unavailable')
        self.runtime.dispatch_guard = failed
        self.enter()
        self.assertEqual(self.transport.sent, [])

    def test_calendar_entry_guard_consumes_new_entry_not_active_management(self):
        self.prepare()
        self.runtime.entry_guard = lambda: False
        result = self.runtime.admit_authenticated('pine', json.dumps(self.entry), producer_id=self.config['signalProducerId'], event_id=self.entry['trade_id'])
        self.assertTrue(result['held'])
        self.runtime.entry_guard = lambda: True
        self.runtime.step()
        self.assertEqual(self.transport.sent, [])
        self.entry['trade_id'] = 'another-calendar-fixture'
        self.ingest('pine', self.entry); self.runtime.step()
        self.native([('entry', 'filled', 4), ('tp1_limit', 'working', 0)])
        self.runtime.entry_guard = lambda: False
        self.now += 5000; self.facts()
        self.assertEqual(self.command('cancel')['status'], 'accepted')

    def test_modified_core_is_not_loaded(self):
        original = Path.read_bytes
        def changed(path):
            raw = original(path)
            return raw + b'\n# changed\n' if path.name == 'core.py' and 'vendor' in path.parts else raw
        with patch.object(Path, 'read_bytes', changed), self.assertRaises(ValueError):
            load_sources()

    def test_installed_list_distinguishes_unverified_runtime(self):
        row = self.manager.list()[0]
        self.assertTrue(row['packageInstalled'])
        self.assertEqual(row['runtimeState'], 'not_observed')
        self.assertFalse(row['providerCommissioned'])
        self.assertTrue(self.runtime.status()['entriesHeld'])

    def test_live_and_unknown_pair_and_secret_fields_rejected(self):
        for change in ({'accountMode': 'live'}, {'pairId': 'historical-ote-refined-small-v1'},
                       {'webhookUrl': 'https://example.invalid/private'}, {'engineSha256': 'f' * 64}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.manager.install(dict(binding('fixture-two'), **change))

    def test_binding_is_immutable(self):
        with self.assertRaises(Conflict):
            self.manager.install(dict(self.config, symbol='MNQH2027'))

    def test_account_route_and_endpoint_exclusivity(self):
        for field in ('accountId', 'routeId', 'endpointSha256'):
            second = binding('fixture-two')
            second[field] = self.config[field]
            with self.subTest(field=field), self.assertRaises(Conflict):
                self.manager.install(second)

    def test_single_writer_and_independent_instance(self):
        with self.assertRaises((BlockingIOError, OSError)):
            self.manager.open(self.config['instanceId'])
        second = binding('fixture-two')
        self.manager.install(second)
        r = self.manager.open(second['instanceId'], clock=lambda: self.now)
        r.close()

    def test_separate_producers_and_symbol_stream_guards(self):
        with self.assertRaises(RuntimeError):
            self.runtime.ingest('pine', json.dumps(self.entry), producer_id=self.config['brokerProducerId'])
        for key, value in (('symbol', 'MNQH2027'), ('stream_id', 'another-stream')):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.ingest('pine', dict(self.entry, **{key: value}))

    def test_raw_broker_channel_cannot_skip_confirmation(self):
        with self.assertRaises(RuntimeError):
            self.runtime.ingest('broker', '{}', producer_id=self.config['brokerProducerId'])

    def test_held_entry_is_consumed_not_replayed_on_enable(self):
        self.ingest('pine', self.entry)
        self.runtime.step()
        self.prepare()
        self.ingest('pine', self.entry)
        self.runtime.step()
        self.assertEqual(self.transport.sent, [])

    def test_actual_core_entry_then_tp1_preserves_quantities_and_offset(self):
        self.enter()
        messages = [x['payload'] for x in self.transport.sent]
        self.assertEqual([(m['action'], m['quantity']) for m in messages], [('buy', 4), ('exit', 2)])
        self.assertEqual(messages[1]['limitPrice'], self.entry['tp1_price'] - .75)
        self.assertEqual(self.runtime.engine.data['trade']['payload'], self.entry)
        self.assertEqual(self.runtime.engine.data['trade']['alloc'], {'tp1': 2, 'parent': 1, 'bias': 0, 'eod': 1})
        self.assertIsNone(self.runtime.status()['confirmedRemaining'])

    def test_v4_entry_uses_its_own_tp1_offset_not_the_paired_setting(self):
        # The binding's paired tp1LimitOffsetHandles is 0.75 (see test_actual_core_entry_then_tp1_preserves_
        # quantities_and_offset); a v4 alert carries its own offset and the engine uses that instead.
        self.entry['schema_version'] = 'decrypt-soup-entry-v4'
        self.entry['tp1_limit_offset_handles'] = 3.0
        self.enter()
        messages = [x['payload'] for x in self.transport.sent]
        self.assertEqual(messages[1]['limitPrice'], self.entry['tp1_price'] - 3.0)

    def test_v4_entry_zero_offset_rests_exactly_at_target(self):
        self.entry['schema_version'] = 'decrypt-soup-entry-v4'
        self.entry['tp1_limit_offset_handles'] = 0.0
        self.enter()
        messages = [x['payload'] for x in self.transport.sent]
        self.assertEqual(messages[1]['limitPrice'], self.entry['tp1_price'])

    def test_v3_entry_still_uses_the_paired_offset_unchanged(self):
        # Belt and suspenders alongside test_actual_core_entry_then_tp1_preserves_quantities_and_offset:
        # a v3 alert never carries tp1_limit_offset_handles, so the v4 schema fields check must reject it
        # if present, and the paired binding offset (0.75) is what is used.
        self.assertNotIn('tp1_limit_offset_handles', self.entry)
        self.enter()
        messages = [x['payload'] for x in self.transport.sent]
        self.assertEqual(messages[1]['limitPrice'], self.entry['tp1_price'] - .75)

    def test_v4_entry_rejects_negative_or_non_tick_aligned_offset(self):
        # validate_pine raises before any trade-state mutation, so each bad alert can be retried
        # in place against the same prepared runtime.
        for bad in (-1.0, 0.1):
            with self.subTest(offset=bad):
                e = copy.deepcopy(self.entry)
                e['schema_version'] = 'decrypt-soup-entry-v4'
                e['tp1_limit_offset_handles'] = bad
                e['trade_id'] = 'fixture-trade-bad-%s' % bad
                self.prepare()
                self.ingest('pine', e)
                self.runtime.step()
                self.assertEqual(self.transport.sent, [])

    def test_v3_entry_carrying_the_v4_field_is_rejected(self):
        # A v3 alert's field set must stay exactly entry_fields; it cannot also carry the v4 field.
        self.entry['tp1_limit_offset_handles'] = 2.0
        self.enter()
        self.assertEqual(self.transport.sent, [])

    def test_source_allocation_is_not_clamped(self):
        self.entry['quantity'] = 8
        self.entry['parent_runner_qty'] = 5
        self.entry['strategy_snapshot']['inputs']['fixture_int_06'] = 13
        self.enter()
        self.assertEqual(self.transport.sent[0]['payload']['quantity'], 8)

    def test_no_native_proof_or_stale_feed_cannot_enter(self):
        self.quote()
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', self.entry)
        self.runtime.step()
        self.assertEqual(self.transport.sent, [])

    def test_stale_quote_and_old_confirmation_cannot_enter(self):
        self.prepare()
        self.now += 3000
        self.ingest('pine', self.entry)
        self.runtime.step()
        self.assertEqual(self.transport.sent, [])

    def test_pending_and_unattributed_and_cross_contract_exposure_fail_closed(self):
        self.prepare()
        for change in ({'ordersComplete': False}, {'accountMode': 'live'}, {'otherContractExposure': True},
                       {'unattributedOrderIds': ['unattributed']}, {'entryRecipients': [self.config['accountId'], 'other-account']}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.publish(self.confirmation(**change))
            self.assertFalse(self.runtime.status()['entryReady'])
            self.assertFalse(self.runtime.status()['nativeFresh'])

    def test_pending_approval_does_not_allow_entry(self):
        self.publish(self.confirmation(pendingOrderIds=['native-pending']))
        self.quote()
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', self.entry)
        self.runtime.step()
        self.assertEqual(self.transport.sent, [])

    def test_route_transport_binding_is_exact(self):
        self.transport.account_id = 'another-account'
        self.enter()
        self.assertEqual(self.transport.sent, [])

    def test_complete_native_fixture_lifecycle_and_cancel_not_a_fill(self):
        self.enter()
        self.native([('entry', 'filled', 4), ('tp1_limit', 'working', 0)])
        self.assertEqual(self.runtime.status()['confirmedRemaining'], 4)
        self.now += 15000
        self.facts()
        self.assertEqual(self.transport.sent[-1]['payload']['action'], 'cancel')
        self.assertEqual(len(self.transport.sent), 3)
        self.now += 10
        self.native([('tp1_limit', 'cancelled', 0)])
        self.assertEqual(self.command('tp1_limit')['filled_qty'], 0)
        self.assertEqual(self.transport.sent[-1]['payload']['action'], 'exit')
        self.assertEqual(self.runtime.status()['confirmedRemaining'], 4)
        self.now += 10
        self.native([('flatten', 'filled', 4)])
        self.assertEqual(self.runtime.status()['tradePhase'], 'closed')
        self.assertEqual(self.runtime.status()['confirmedRemaining'], 0)
        self.assertEqual(len(self.transport.sent), 4)

    def test_held_entries_keep_existing_trade_management(self):
        self.enter()
        self.native([('entry', 'filled', 4), ('tp1_limit', 'working', 0)])
        self.runtime.hold()
        self.now += 15000
        self.facts()
        self.assertEqual(self.transport.sent[-1]['payload']['action'], 'cancel')

    def test_partial_target_keeps_runner_intent(self):
        self.enter()
        self.native([('entry', 'filled', 4), ('tp1_limit', 'working', 0)])
        self.now += 100
        self.native([('tp1_limit', 'filled', 2)])
        self.assertEqual(self.runtime.engine.left(), {'tp1': 0, 'parent': 1, 'bias': 0, 'eod': 1})
        self.assertEqual(self.runtime.status()['confirmedRemaining'], 2)

    def test_native_sequence_survives_reopen(self):
        self.publish(self.confirmation())
        self.runtime.close()
        self.runtime = self.manager.open(self.config['instanceId'], transport=self.transport, clock=lambda: self.now)
        old = self.confirmation(sequence=1)
        with self.assertRaises(RuntimeError):
            self.publish(old)
        self.assertTrue(self.runtime.status()['entriesHeld'])
        self.assertFalse(self.runtime.status()['nativeFresh'])

    def test_timeout_is_unknown_and_never_resent(self):
        self.transport.outcome = 'timeout'
        self.enter()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.command('entry')['status'], 'unknown')
        self.runtime.step()
        self.runtime.close()
        self.runtime = self.manager.open(self.config['instanceId'], transport=self.transport, clock=lambda: self.now)
        self.runtime.step()
        self.assertEqual(len(self.transport.sent), 1)

    def test_pending_pre_restart_entry_is_not_replayed(self):
        self.prepare()
        self.ingest('pine', self.entry)
        self.runtime.close()
        self.runtime = self.manager.open(self.config['instanceId'], transport=self.transport, clock=lambda: self.now)
        self.prepare()
        self.runtime.step()
        self.assertEqual(self.transport.sent, [])
        self.ingest('pine', self.entry)
        self.runtime.step()
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.runtime.engine.db.execute("SELECT count(*) FROM inbox WHERE detail='restart_entry_not_replayed'").fetchone()[0], 1)

    def test_receipts_do_not_store_arbitrary_provider_response(self):
        secret = 'https://private.example.invalid/credential-do-not-persist'
        self.transport.receipt = {'id': secret, 'endpoint': secret, 'password': secret, 'logId': 'safe-receipt'}
        self.enter()
        self.assertNotIn(secret, canonical(self.runtime.adapter_snapshot()))
        self.assertNotIn(secret.encode(), (self.runtime.directory / 'engine.sqlite-wal').read_bytes())

    def test_private_files_and_symlink_rejected(self):
        self.assertEqual((self.runtime.directory / 'engine.sqlite').stat().st_mode & 0o777, 0o600)
        link = Path(self.temp.name) / 'link'
        link.symlink_to(self.manager.root, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            RuntimeManager(link)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


if __name__ == '__main__':
    unittest.main()
