"""Isolated app lifecycle using the packaged core; no provider or orders."""
from pathlib import Path
import json
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_fixture import FakeBridge  # noqa: E402
from guardian_service import GuardianService
from guardian_auth import GuardianError
from trading_service import TradingService, TradingError, PAIR_ID
from server import Store

RID = '11111111-2222-4333-8444-555555555555'
PASSPHRASE = 'FAKE-VAULT-FOR-ISOLATED-SERVICE-TEST'


class ForbiddenBrowser:
    def bind_market_browser(self, browser):
        pass  # The isolated service test never opens a real customer browser.

    def __getattr__(self, name):
        raise AssertionError('provider browser forbidden')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.guardian = GuardianService(self.root, browser=ForbiddenBrowser())
        self.addCleanup(self.guardian.close)
        self.guardian.vault.initialize(PASSPHRASE)
        # The bridge as setup.py leaves it for this account (fake /state and /order, no socket).
        self.bridge = FakeBridge(self.root / 'data', {'nativeAccountId': 12345, 'nativeAccountName': 'Fixture Account',
                                                      'accountId': '12345', 'symbol': 'MNQZ2026'},
                                 clock=lambda: int(time.time() * 1000))
        self.service = TradingService(self.root, self.guardian, self.store, bridge_opener=self.bridge.opener)
        self.addCleanup(self.service.close)
        self.data = {'provider': 'tradovate', 'requestId': RID, 'pairId': PAIR_ID, 'streamId': 'fixture-stream',
                     'feedId': 'fixture-feed', 'tp1LimitOffsetHandles': 0.75, 'nativeAccountId': 12345,
                     'nativeAccountName': 'Fixture Account', 'accountMode': 'demo', 'symbol': 'MNQZ2026',
                     'loginGroupId': 'login-1', 'acknowledgeNativeSetup': True}
        self.instance_id = 'engine_' + RID
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def install(self):
        return self.service.action('install', self.data)

    def test_installs_exact_core_held_without_secret_in_status_or_ledger(self):
        result = self.install()
        instance = result['instances'][0]
        self.assertTrue(instance['entriesHeld'])
        self.assertFalse(instance['entryReady'])
        self.assertEqual(instance['route'], 'tradovate_bridge')
        self.assertIn('bridge_state_unavailable', instance['blockers'], 'no fresh bridge state read yet')
        self.assertNotIn('Fixture Account', json.dumps({k: v for k, v in result.items() if k != 'instances'}))

    def test_durable_install_retry_is_idempotent(self):
        self.install()
        result = self.install()
        self.assertEqual(len(result['instances']), 1)

    def test_duplicate_account_cannot_get_second_engine(self):
        self.install()
        data = dict(self.data, requestId='21111111-2222-4333-8444-555555555555')
        with self.assertRaisesRegex(TradingError, 'account_in_use'):
            self.service.action('install', data)
        self.assertEqual(len(self.service.status()['instances']), 1)

    def test_unacknowledged_setup_refuses(self):
        with self.assertRaises(TradingError):
            self.service.action('install', dict(self.data, acknowledgeNativeSetup=False))

    def test_fresh_system_installs_without_decrypt_labs_registration(self):
        # Only strategies are gated (TradingView invite). The engine installs on any system.
        self.install()
        binding = self.service.status()['instances'][0]['binding']
        self.assertEqual((binding['customerId'], binding['installationId']), ('local', 'local'))
        self.assertNotIn('customer_registration_not_active', json.dumps(self.service.status()))

    def test_customer_cannot_assert_native_or_release_qualification(self):
        with self.assertRaises(GuardianError):
            self.service.action('install', dict(self.data, providerVerified=True))
        self.install()
        with self.assertRaisesRegex(TradingError, 'entry_checks_pending'):
            self.service.action('enable-entries', {'instanceId': self.instance_id, 'acknowledgeDemoTrading': True})
        self.assertTrue(self.service.status()['instances'][0]['entriesHeld'])

    def test_explicit_start_stays_held_and_no_untrusted_order_action_exists(self):
        self.install()
        result = self.service.action('start-held', {'instanceId': self.instance_id})
        self.assertEqual(result['instances'][0]['runtimeState'], 'running')
        self.assertTrue(result['instances'][0]['entriesHeld'])
        for action in ('ingest', 'publish-confirmation', 'flatten', 'delete-ledger'):
            with self.assertRaisesRegex(TradingError, 'not_found'):
                self.service.action(action, {})

    def test_health_records_transitions_without_poll_duplicates(self):
        self.service.check(); self.service.check()
        initial = len(self.service.status()['events'])
        self.guardian.vault.lock()
        self.service.check(); self.service.check()
        self.assertEqual(len(self.service.status()['events']), initial + 1)
        self.guardian.vault.unlock(PASSPHRASE)
        self.service.check()
        self.assertEqual(len(self.service.status()['events']), initial + 2)

    def test_restore_hold_prevents_open_and_automatic_restart(self):
        self.install(); self.service.close()
        marker = self.root / 'maintenance/hold.json'
        marker.parent.mkdir(mode=0o700)
        marker.write_text('{}')
        other = TradingService(self.root, self.guardian, self.store, bridge_opener=self.bridge.opener)
        self.addCleanup(other.close)
        other.start()
        self.assertEqual(other.runtime, {})
        with self.assertRaisesRegex(TradingError, 'restore_reconciliation_required'):
            other.action('start-held', {'instanceId': self.instance_id})
        self.assertIn('restore_reconciliation_required', other.status()['instances'][0]['blockers'])

    def test_the_app_installs_soup_only(self):
        # decrypt-bridge-v2 strategies are set up with the bridge's own setup; retired pairs are unknown.
        from decrypt_trading.source import PAIR_ID
        self.assertEqual(list(self.service.packages), [PAIR_ID])
        for pair in ('fvg-r5b-bridge-v2', 'fvg-ifvg-native-v1', 'ote-silver-native-v1'):
            with self.subTest(pair=pair), self.assertRaisesRegex(TradingError, 'pair_unsupported'):
                self.service.action('install', dict(self.data, pairId=pair, tp1LimitOffsetHandles=0))
        self.assertFalse(self.service.manager.list())

    def test_calendar_contract_hold_does_not_claim_ready(self):
        self.install()
        with patch.object(self.service.calendar, 'status', return_value={'entryAllowed':False, 'reason':'contract_roll_required'}):
            result = self.service.status()
        self.assertIn('contract_roll_required', result['instances'][0]['blockers'])
        self.assertFalse(result['instances'][0]['entryReady'])

    def test_optional_notifications_persist_without_sending_from_request(self):
        class NeverSend:
            def send(self, event):
                raise AssertionError('action must not send native notification')
        self.service.notification_transport = NeverSend()
        self.assertFalse(self.service.status()['notifications']['enabled'])
        result = self.service.action('configure-notifications', {'enabled':True})
        self.assertTrue(result['notifications']['enabled'])
        self.service.close()
        other = TradingService(self.root, self.guardian, self.store, bridge_opener=self.bridge.opener, notification_transport=NeverSend())
        self.addCleanup(other.close)
        self.assertTrue(other.status()['notifications']['enabled'])
        self.assertFalse(other.action('configure-notifications', {'enabled':False})['notifications']['enabled'])

    def test_entry_gate_needs_the_bridge_route_whatever_the_engine_controls_say(self):
        (self.bridge.tvbridge / 'config.json').unlink()
        result = self.install()
        self.assertIn('bridge_not_configured', result['instances'][0]['blockers'])
        runtime = self.service.runtime[self.instance_id]
        runtime.set_controls(enabled=True, locked=False)
        with patch.object(self.service.calendar, 'status', return_value={'entryAllowed': True, 'reason': None}):
            self.assertFalse(self.service._entry_gate(runtime.binding))
            # Once the bridge is set up, the service loop attaches the route.
            self.bridge.write_config()
            self.service.check()
            self.assertIsNotNone(runtime.bridge)
            self.assertTrue(self.service._entry_gate(runtime.binding))

    def test_old_native_bridge_actions_are_gone(self):
        self.install()
        for action in ('configure-native-bridge', 'enable-native-bridge', 'observe-native-bridge',
                       'import-qualification', 'install-module'):
            with self.assertRaisesRegex(TradingError, 'not_found'):
                self.service.action(action, {'instanceId': self.instance_id})

    def test_private_alert_address_requires_explicit_unlocked_request_and_never_rotates(self):
        self.install()
        class Listener:
            public_origin = 'https://alerts.customer.example'
            def close(self): pass
        self.service.ingress = Listener()
        body = {'instanceId':self.instance_id, 'acknowledgeOneTimeSecret':True}
        first = self.service.action('create-signal-key', body)['oneTimeIngress']['url']
        self.assertNotIn(first, json.dumps(self.service.status()))
        self.assertEqual(first, self.service.action('show-signal-address', body)['oneTimeIngress']['url'])
        with self.assertRaises(TradingError):
            self.service.action('show-signal-address', {**body, 'acknowledgeOneTimeSecret':False})
        self.guardian.vault.lock()
        with self.assertRaises(GuardianError):
            self.service.action('show-signal-address', body)



RETIRED_PAIR = ('fvg-ifvg-native-v1', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')


def make_retired(root, instance_id, trade=None, **change):
    """Rewrite a stopped instance's stored binding (and its ledger fingerprint) as an older release left it:
    an FVG native v1 engine, since retired."""
    import hashlib
    import sqlite3
    from decrypt_trading.runtime import canonical
    registry = sqlite3.connect(Path(root) / 'trading/instances.sqlite')
    binding = json.loads(registry.execute('SELECT binding FROM instances WHERE id=?', (instance_id,)).fetchone()[0])
    binding.update(pairId=RETIRED_PAIR[0], engineSha256=RETIRED_PAIR[1], strategySha256=RETIRED_PAIR[2],
                   tp1LimitOffsetHandles=0, **change)
    registry.execute('UPDATE instances SET binding=? WHERE id=?', (canonical(binding), instance_id))
    registry.commit()
    registry.close()
    ledger = sqlite3.connect(Path(root) / 'trading' / instance_id / 'engine.sqlite')
    ledger.execute("UPDATE customer_runtime_metadata SET value=? WHERE key='binding'",
                   (hashlib.sha256(canonical(binding).encode()).hexdigest(),))
    if trade is not None:
        state = json.loads(ledger.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        state['trade'] = trade
        ledger.execute('UPDATE state SET body=? WHERE id=1', (json.dumps(state),))
    ledger.commit()
    ledger.close()


class RetiredEngineTests(unittest.TestCase):
    """An instance installed from an older release whose engine is now retired (here FVG native v1, since
    removed): status keeps working, Soup still installs and runs, the old instance is shown blocked, can
    be retired without loading its engine, and can never open, start, trade or come back."""

    OLD_PAIR = RETIRED_PAIR
    OLD_RID = '31111111-2222-4333-8444-555555555555'

    def setUp(self):
        ServiceTests.setUp(self)
        self.old_id = 'engine_' + self.OLD_RID
        self.service.action('install', dict(self.data, requestId=self.OLD_RID, nativeAccountId=67890,
                                            nativeAccountName='Fixture Other Account', tp1LimitOffsetHandles=0))
        self.service.close()
        self.make_retired(self.old_id)
        self.service = TradingService(self.root, self.guardian, self.store, bridge_opener=self.bridge.opener)
        self.addCleanup(self.service.close)

    def make_retired(self, instance_id, trade=None, **change):
        make_retired(self.root, instance_id, trade=trade, **change)

    def rows(self, status):
        return {item['instanceId']: item for item in status['instances']}

    def test_status_shows_the_retired_engine_blocked_next_to_soup(self):
        self.service.action('install', self.data)
        rows = self.rows(self.service.status())
        old, soup = rows[self.old_id], rows['engine_' + RID]
        self.assertEqual((old['runtimeState'], old['blockers'], old['entriesHeld'], old['entryReady']),
                         ('retired_engine', ['retired_engine'], True, False))
        self.assertIn('new bridge format', old['message'])
        self.assertEqual(old['binding']['pairId'], self.OLD_PAIR[0])
        self.assertEqual(soup['route'], 'tradovate_bridge')
        self.assertNotIn('retired_engine', soup['blockers'])
        self.assertFalse(self.service.status()['entryAuthority'])

    def test_soup_still_installs_starts_and_runs(self):
        self.assertEqual(self.service.start(), True)        # the service start skips the retired engine
        result = self.service.action('install', self.data)
        result = self.service.action('start-held', {'instanceId': 'engine_' + RID})
        self.assertEqual(self.rows(result)['engine_' + RID]['runtimeState'], 'running')
        self.assertNotIn(self.old_id, self.service.runtime)

    def test_the_retired_engine_can_never_open_start_or_trade(self):
        for action, data in (('start-held', {}), ('enable-entries', {'acknowledgeDemoTrading': True})):
            with self.subTest(action=action), self.assertRaises(TradingError):
                self.service.action(action, dict(data, instanceId=self.old_id))
        for call in (self.service.manager.get, self.service.manager.open):
            with self.subTest(call=call.__name__), self.assertRaisesRegex(Exception, 'Retired engine'):
                call(self.old_id)
        self.assertNotIn(self.old_id, self.service.runtime)

    def test_the_retired_engine_retires_without_loading_its_engine_and_stays_retired(self):
        never = AssertionError('the retired engine must not load')
        with patch('decrypt_trading.source.load_pair_sources', side_effect=never), \
                patch('decrypt_trading.runtime.load_pair_sources', side_effect=never), \
                patch('decrypt_trading.runtime.Runtime', side_effect=never):
            result = self.service.action('retire', {'instanceId': self.old_id, 'acknowledgeRetirement': True})
        self.assertNotIn(self.old_id, self.rows(result))
        retired = {item['binding']['instanceId']: item for item in result['retiredInstances']}
        self.assertEqual(retired[self.old_id]['retirementBasis'], 'retired_engine_closed_ledger')
        with self.assertRaises(TradingError):
            self.service.action('reactivate', {'instanceId': self.old_id, 'acknowledgeReactivation': True})
        # Its account is free again for a supported engine.
        new = dict(self.data, requestId='41111111-2222-4333-8444-555555555555', nativeAccountId=67890,
                   nativeAccountName='Fixture Other Account')
        self.assertIn('engine_41111111-2222-4333-8444-555555555555', self.rows(self.service.action('install', new)))

    def test_retirement_refuses_a_ledger_with_an_open_trade(self):
        self.service.close()
        self.make_retired(self.old_id, trade={'id': 'old-trade:long', 'phase': 'open'})
        self.service = TradingService(self.root, self.guardian, self.store, bridge_opener=self.bridge.opener)
        self.addCleanup(self.service.close)
        with self.assertRaises(TradingError):
            self.service.action('retire', {'instanceId': self.old_id, 'acknowledgeRetirement': True})
        self.assertEqual(self.rows(self.service.status())[self.old_id]['tradePhase'], 'open')

    def test_a_malformed_retired_binding_still_fails_closed(self):
        self.service.close()
        self.make_retired(self.old_id, nativeAccountId=-1)
        from decrypt_trading import RuntimeManager, RuntimeError as EngineError
        with self.assertRaises(EngineError):
            RuntimeManager(self.root / 'trading').list()


if __name__ == '__main__':
    unittest.main()
