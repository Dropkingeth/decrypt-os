"""Local customer runtime lifecycle. Browser requests cannot assert broker truth.

An engine installs and opens held. Soup's orders go to the Tradovate bridge module's /order route
(decrypt_trading.bridge_route), checked against the bridge's fresh account state. New entries need
an explicit release. No action accepts an arbitrary command, price, native fill or source filename.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import json

from guardian_auth import require
from trading_ingress_secrets import IngressSecrets

PACKAGE = Path(__file__).resolve().parents[1] / 'trading-runtime'
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))
from decrypt_trading import RuntimeManager, RuntimeError as EngineError
from decrypt_trading.source import package_catalog, pair_spec, PAIR_ID
from decrypt_trading import customer_contract as native_contract
from decrypt_trading.bridge_route import BindingError, open_route
from trading_calendar import CalendarGate
from customer_health import CustomerHealth, NativeNotifications

# The binding schema keeps owner fields; the person running this system owns it.
# No Decrypt Labs registration exists, and these values never leave the machine.
LOCAL_OWNER = {'customerId': 'local', 'installationId': 'local'}
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')


class TradingError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


def ensure(value, code, message):
    if not value:
        raise TradingError(code, message)


def disabled_status():
    return {'enabled': False, 'mode': 'customer_demo_only', 'instances': [],
            'package': None, 'packages': [], 'account': None, 'health': [], 'entryAuthority': False,
            }


class TradingService:
    def __init__(self, state_dir, guardian, store, *, clock=time.time, notification_transport=None, market_browser=None, bridge_opener=None):
        self.root = Path(state_dir) / 'trading'
        self.restore_hold = Path(state_dir) / 'maintenance' / 'hold.json'
        self.guardian, self.store = guardian, store
        self.manager = RuntimeManager(self.root)
        self.ingress_secrets = IngressSecrets(guardian.vault)
        self.clock = clock
        self.packages = {item['pairId']: item for item in package_catalog()}
        self.calendar = CalendarGate()
        self.customer_health = CustomerHealth(state_dir, clock=clock)
        self.notification_transport = notification_transport or NativeNotifications()
        # Soup's orders go to the Tradovate bridge module's /order route (decrypt_trading.bridge_route).
        self.bridge_opener = bridge_opener or open_route
        self.runtime, self.faults, self.route_problems = {}, {}, {}
        self.feeds = {}
        from trading_browser import MarketBrowser
        self.market_browser = market_browser or MarketBrowser(state_dir)
        with self.guardian.mutex:
            self.guardian.browser.bind_market_browser(self.market_browser)
        self.mutex = threading.RLock()
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.worker, self.closed = None, False
        self.ingress = None
        self.ingress_state = {'state': 'not_configured', 'publicTlsVerified': False}
        self.health_path = self.root / 'service.sqlite'
        # The same reviewed filesystem checks as the runtime ledgers.
        from decrypt_trading.runtime import _private_file
        _private_file(self.health_path)
        with self._db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS health_current (source TEXT PRIMARY KEY, state TEXT NOT NULL, changed INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS health_events (id INTEGER PRIMARY KEY, source TEXT NOT NULL, state TEXT NOT NULL, observed INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS configuration (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            saved = db.execute("SELECT value FROM configuration WHERE name='notifications'").fetchone()
        self.notifications_enabled = bool(saved and json.loads(saved['value']).get('enabled') is True)
        if self.notifications_enabled:
            self.customer_health.transport = self.notification_transport

    @contextmanager
    def _db(self):
        from decrypt_trading.runtime import _private_file
        _private_file(self.health_path)
        db = sqlite3.connect(self.health_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _health(self, source, state):
        with self._db() as db:
            old = db.execute('SELECT state FROM health_current WHERE source=?', (source,)).fetchone()
            if old is None or old['state'] != state:
                at = int(self.clock() * 1000)
                db.execute('INSERT OR REPLACE INTO health_current VALUES (?,?,?)', (source, state, at))
                db.execute('INSERT INTO health_events(source,state,observed) VALUES (?,?,?)', (source, state, at))
                db.execute('DELETE FROM health_events WHERE id NOT IN (SELECT id FROM health_events ORDER BY id DESC LIMIT 2000)')

    def _open(self, instance_id):
        ensure(not self._restore_held(), 'restore_reconciliation_required',
               'Restored trading records need fresh broker reconciliation before a worker can open them.')
        if instance_id in self.runtime and self.runtime[instance_id].closed:
            self.runtime.pop(instance_id)
        if instance_id not in self.runtime:
            binding = self.manager.get(instance_id)
            runtime = self.manager.open(instance_id, transport=self._bridge_route(binding),
                                        dispatch_guard=lambda: not self._restore_held(),
                                        entry_guard=lambda: self._entry_gate(binding))
            self.runtime[instance_id] = runtime
        return self.runtime[instance_id]

    def _bridge_route(self, binding):
        """The bridge route for this binding, or None with the reason kept for status."""
        instance_id = binding['instanceId']
        if binding['pairId'] != native_contract.SOUP:
            self.route_problems[instance_id] = 'bridge_route_soup_only'
            return None
        try:
            transport = self.bridge_opener(binding)
        except BindingError as exc:
            self.route_problems[instance_id] = str(exc)
            return None
        self.route_problems.pop(instance_id, None)
        return transport

    def _attach_bridge(self, runtime):
        """Attach the route once the bridge is set up; the engine may be installed first."""
        if runtime.bridge is not None or runtime.closed or runtime.binding['pairId'] != native_contract.SOUP:
            return
        transport = self._bridge_route(runtime.binding)
        if transport is None:
            return
        try:
            runtime.attach_bridge(transport)
        except EngineError:
            transport.close()
            self.route_problems[runtime.binding['instanceId']] = 'bridge_route_refused'

    def _entry_gate(self, binding):
        """Runs under the engine lock; never acquires the service lock.

        Safety checks only: the engine is routed to the bridge and the exchange calendar
        allows entries. They never stop an accepted trade's management.
        """
        runtime = self.runtime.get(binding['instanceId'])
        return bool(binding['pairId'] in self.packages and runtime is not None and runtime.bridge is not None
                    and self.calendar.status(binding, int(self.clock() * 1000))['entryAllowed'])

    def _restore_held(self):
        return self.restore_hold.exists() or self.restore_hold.is_symlink()

    def _blockers(self, binding, runtime):
        blockers = []
        if self._restore_held():
            blockers.append('restore_reconciliation_required')
        if runtime is None or runtime.bridge is None:
            blockers.append(self.route_problems.get(binding['instanceId'], 'bridge_route_unavailable'))
        elif not runtime.status()['nativeFresh']:
            blockers.append('bridge_state_unavailable')
        calendar = self.calendar.status(binding, int(self.clock() * 1000))
        if not calendar['entryAllowed']:
            blockers.append(calendar['reason'])
        if binding['instanceId'] in self.faults:
            blockers.append(self.faults[binding['instanceId']])
        return blockers

    def guardian_action(self, action, data):
        """Guardian keeps TradingView sign-ins only; trading never depends on it."""
        ensure(type(data) is dict, 'invalid_fields', 'Use a valid connection request.')
        return self.guardian.action(action, data)

    def status(self):
        with self.mutex:
            packages = list(self.packages.values())
            # Each native engine carries its own account binding; there is no
            # separate single "watched account" projection to report here.
            account = None
            instances = []
            for item in self.manager.list():
                binding = item['binding']
                if item['registryState'] == 'retired_engine':
                    # A retired engine is shown blocked with a plain message; it never opens or trades.
                    instances.append({'instanceId': binding['instanceId'], 'packageInstalled': False,
                                      'runtimeState': item['runtimeState'], 'entriesHeld': True, 'entryReady': False,
                                      'nativeFresh': False, 'tradePhase': item['tradePhase'], 'pendingCommands': None,
                                      'calendar': {'entryAllowed': False, 'reason': item['runtimeState']},
                                      'feed': {'state': 'not_started', 'nativeVerified': False},
                                      'binding': binding, 'blockers': list(item['blockers']), 'message': item['message']})
                    continue
                runtime = self.runtime.get(binding['instanceId'])
                snapshot = runtime.status() if runtime else {
                    'instanceId': binding['instanceId'], 'packageInstalled': True,
                    'runtimeState': 'stopped', 'entriesHeld': True, 'entryReady': False,
                    'nativeFresh': False, 'tradePhase': 'unobserved', 'pendingCommands': None}
                blockers = self._blockers(binding, runtime)
                feed = self.feeds.get(binding['instanceId'])
                instances.append({**snapshot, 'calendar': self.calendar.status(binding, int(self.clock() * 1000)), 'feed': feed.status() if feed else {'state': 'not_started', 'nativeVerified': False},
                                  'entryReady': snapshot.get('entryReady') is True and not blockers,
                                  'binding': binding, 'blockers': blockers})
            with self._db() as db:
                health = [dict(row) for row in db.execute('SELECT source,state,changed FROM health_current ORDER BY source')]
                events = [dict(row) for row in db.execute('SELECT source,state,observed FROM health_events ORDER BY id DESC LIMIT 20')]
            retained = self.manager.list(include_retired=True)
            from tradovate_service import bridge_status
            return {'enabled': True, 'mode': 'customer_provider_bound', 'bridge': bridge_status(),
                    'package': self.packages.get(PAIR_ID), 'packages': packages,
                    'notifications': {**self.customer_health.status(), 'enabled': self.notifications_enabled},
                    'account': account, 'instances': instances,
                    'retiredInstances': [item for item in retained if item['registryState'] == 'retired'],
                    'ingress': dict(self.ingress_state), 'ingressKeys': self.ingress_secrets.metadata(),
                    'health': health, 'events': events,
                    'entryAuthority': any(item.get('entryReady') is True for item in instances)}

    def _assert_account_available(self, binding, *, retained=None):
        # Preserve the one-active-binding-per-account boundary across
        # adapters. Only established exact ID strings are comparable here;
        # do not infer equivalence between different provider namespaces.
        def account(value):
            return str(value['nativeAccountId'] if native_contract.native(value) else value['accountId'])
        for item in (self.manager.list(include_retired=True) if retained is None else retained):
            if item['registryState'] not in ('active', 'retired_engine'):
                continue
            other = item['binding']
            if other['instanceId'] == binding['instanceId']:
                ensure(other == binding, 'binding_immutable', 'An installed engine cannot be rebound to another account or route.')
            else:
                ensure(account(other) != account(binding), 'account_in_use',
                       'This account already belongs to an installed engine, including another provider route.')

    def _prepare_install(self, data):
        ensure(type(data) is dict and data.get('provider') == 'tradovate', 'native_route_required',
               'New engines use the Tradovate bridge.')
        return self._prepare_native_install(data)

    def _prepare_native_install(self, data):
        require(data, ('provider', 'requestId', 'pairId', 'streamId', 'feedId', 'tp1LimitOffsetHandles',
                       'nativeAccountId', 'nativeAccountName', 'accountMode', 'symbol', 'loginGroupId', 'acknowledgeNativeSetup'))
        ensure(data['provider'] == 'tradovate' and data['acknowledgeNativeSetup'] is True,
               'acknowledgment_required', 'Confirm the exact native account and that installation leaves the bridge disabled.')
        ensure(type(data['requestId']) is str and UUID.fullmatch(data['requestId']), 'invalid_request_id', 'Use a new installation request.')
        ensure(type(data['pairId']) is str and data['pairId'] in self.packages, 'pair_unsupported', 'Choose a supported packaged engine.')
        request = data['requestId']
        binding = {'schema': native_contract.BINDING, 'provider': 'tradovate', 'loginGroupId': data['loginGroupId'],
                   'instanceId': 'engine_' + request, **LOCAL_OWNER, **pair_spec(data['pairId']),
                   'accountId': str(data['nativeAccountId']),
                   'accountMode': data['accountMode'], 'nativeAccountId': data['nativeAccountId'],
                   'nativeAccountName': data['nativeAccountName'], 'routeId': 'route_' + request,
                   'feedId': data['feedId'], 'streamId': data['streamId'], 'symbol': data['symbol'],
                   'root': 'MNQ', 'tickSize': .25, 'tp1LimitOffsetHandles': data['tp1LimitOffsetHandles'],
                   'signalProducerId': 'signal_' + request, 'feedProducerId': 'feed_' + request,
                   'brokerProducerId': 'tradovate_' + request}
        try:
            binding['routeSha256'] = native_contract.route_digest(binding)
            binding = native_contract.validate_binding(binding)
        except (ValueError, TypeError, KeyError):
            raise TradingError('native_binding_invalid', 'Enter the exact account, environment, dated MNQ contract and paired strategy settings.') from None
        retained = self.manager.list(include_retired=True)
        self._assert_account_available(binding, retained=retained)
        same = [item for item in retained if item['binding']['instanceId'] == binding['instanceId']]
        ensure(len(retained) < 32 or bool(same), 'retained_instance_limit', 'All retained ledgers are preserved; the instance limit has been reached.')
        if same:
            ensure(same[0]['registryState'] == 'active' and same[0]['binding'] == binding,
                   'binding_immutable', 'An existing or retired instance cannot be rebound.')
        return binding

    def _install(self, data):
        binding = self._prepare_install(data)
        instance_id = binding['instanceId']
        self.manager.install(binding)
        self._open(instance_id)
        self._health(instance_id, 'installed_held')

    def action(self, action, data):
        with self.mutex:
            ensure(not self.closed, 'service_stopped', 'The trading service is stopped.')
            try:
                if action == 'status':
                    require(data)
                elif action == 'retire':
                    require(data, ('instanceId', 'acknowledgeRetirement'))
                    ensure(data['acknowledgeRetirement'] is True, 'acknowledgment_required', 'Confirm retirement with the original ledger retained.')
                    self._retire_instance(data['instanceId'])
                elif action == 'reactivate':
                    require(data, ('instanceId', 'acknowledgeReactivation'))
                    ensure(data['acknowledgeReactivation'] is True and not self._restore_held(), 'hold_required', 'Finish any restore reconciliation before reactivating an archived engine.')
                    self._assert_account_available(self.manager.get(data['instanceId'], include_retired=True))
                    self.manager.reactivate(data['instanceId'])
                    self._health(data['instanceId'], 'reactivated_held_not_started')
                elif action == 'configure-notifications':
                    require(data, ('enabled',))
                    ensure(type(data['enabled']) is bool, 'invalid_fields', 'Choose whether desktop health notifications are enabled.')
                    with self.customer_health.mutex, self._db() as db:
                        db.execute('INSERT OR REPLACE INTO configuration VALUES (?,?)', ('notifications', json.dumps({'enabled': data['enabled']})))
                        self.notifications_enabled = data['enabled']
                        self.customer_health.transport = self.notification_transport if data['enabled'] else None
                elif action == 'install':
                    self._install(data)
                elif action in ('bridge-setup', 'bridge-enable', 'bridge-disable', 'bridge-login'):
                    # The guided bridge setup: the module's own setup.py with fixed arguments, and a
                    # read-only look at what the running bridge sees. Nothing here places an order.
                    import tradovate_service as bridge
                    try:
                        if action == 'bridge-setup':
                            extra = {'bridgeSetup': bridge.setup_bridge(data)}
                        else:
                            require(data)
                            extra = ({'bridgeUnits': bridge.enable_bridge()} if action == 'bridge-enable'
                                     else {'bridgeUnits': bridge.disable_bridge()} if action == 'bridge-disable'
                                     else {'bridgeLogin': bridge.login_status()})
                    except bridge.BridgeError as exc:
                        raise TradingError(exc.code, exc.message, exc.status) from None
                    return {**self.status(), **extra}
                elif action == 'configure-feed':
                    require(data, ('instanceId', 'chartUrl', 'resumeOnLogin'))
                    ensure(type(data['resumeOnLogin']) is bool, 'invalid_fields', 'Choose whether this feed reconnects after login.')
                    ensure(type(data['chartUrl']) is str and re.fullmatch(r'https://www\.tradingview\.com/chart/[A-Za-z0-9]{6,24}/', data['chartUrl']),
                           'invalid_chart', 'Use the exact saved TradingView chart address for this dated contract.')
                    runtime = self._open(data['instanceId'])
                    ensure(self.market_browser.chart_url in (None, data['chartUrl']), 'market_chart_rebind_required',
                           'The current feed is bound to another chart. Turn off its reopen-after-login option, then restart this held service before selecting a different saved chart. The existing feed was not changed.')
                    with self._db() as db:
                        old = db.execute('SELECT value FROM configuration WHERE name=?', ('feed:' + runtime.binding['instanceId'],)).fetchone()
                    runtime.hold()
                    ensure(runtime.status()['tradePhase'] in ('idle', 'closed') or (old and json.loads(old['value'])['chartUrl'] == data['chartUrl']),
                           'active_supervision_required', 'Finish or reconcile the active trade before changing its chart source.')
                    self._start_feed(runtime.binding['instanceId'], data['chartUrl'])
                    with self._db() as db:
                        db.execute('INSERT OR REPLACE INTO configuration VALUES (?,?)',
                                   ('feed:' + runtime.binding['instanceId'], json.dumps({'chartUrl': data['chartUrl'], 'resumeOnLogin': data['resumeOnLogin']})))
                elif action == 'configure-size-lock':
                    require(data, ('instanceId', 'enabled'))
                    ensure(type(data['enabled']) is bool, 'invalid_fields', 'Choose whether the Size Lock module is enabled.')
                    binding = self.manager.get(data['instanceId'])
                    ensure(native_contract.native(binding) and binding['pairId'] == native_contract.SOUP,
                           'size_lock_unsupported', 'Size Lock is an optional module for the Soup engine only.')
                    runtime = self._open(binding['instanceId'])
                    runtime.hold()
                    ensure(runtime.status()['tradePhase'] in ('idle', 'closed'),
                           'active_supervision_required', 'Finish or reconcile the active trade before changing the sizing policy.')
                    self.manager.set_size_lock(binding['instanceId'], data['enabled'])
                    # Reopen so the new quantity policy is read immediately; the
                    # engine itself still refuses to apply it to a trade already
                    # in flight (quantity_policy is pinned once a trade opens).
                    runtime.close()
                    self.runtime.pop(binding['instanceId'], None)
                    self._open(binding['instanceId'])
                    self._health(binding['instanceId'], 'size_lock_enabled' if data['enabled'] else 'size_lock_disabled')
                elif action == 'configure-ingress':
                    require(data, ('publicOrigin', 'acknowledgeOwnsOrigin'))
                    ensure(data['acknowledgeOwnsOrigin'] is True, 'origin_acknowledgment_required', 'Use an HTTPS address you control.')
                    ensure(all(r.status()['entriesHeld'] and r.status()['tradePhase'] in ('idle', 'closed') for r in self.runtime.values()),
                           'active_supervision_required', 'Hold entries and finish active trades before changing alert delivery.')
                    self._start_ingress(data['publicOrigin'])
                    with self._db() as db:
                        db.execute('INSERT OR REPLACE INTO configuration VALUES (?,?)', ('ingress', json.dumps({'publicOrigin': self.ingress.public_origin})))
                elif action in ('create-signal-key', 'show-signal-address', 'rotate-signal-key'):
                    require(data, ('instanceId', 'acknowledgeOneTimeSecret') + (('expectedGeneration',) if action == 'rotate-signal-key' else ()))
                    runtime = self._open(data['instanceId'])
                    ensure(data['acknowledgeOneTimeSecret'] is True and runtime.status()['entriesHeld'],
                           'hold_required', 'Hold new entries and confirm the one-time private alert key.')
                    ensure(self.ingress is not None, 'ingress_not_configured', 'Configure your customer-owned HTTPS ingress first.')
                    with runtime.mutex:
                        if action == 'rotate-signal-key':
                            self._settled_module_runtime(runtime, native_flat=True)
                            token, generation = self.ingress_secrets.rotate(runtime.binding['instanceId'], data['expectedGeneration'])
                        elif action == 'create-signal-key':
                            token = self.ingress_secrets.create(runtime.binding['instanceId'], 'pine')
                            generation = 1
                        else:
                            with self.ingress_secrets.lease(runtime.binding['instanceId'], 'pine') as saved:
                                token = saved
                            generation = next(p['generation'] for p in self.ingress_secrets.metadata()['producers']
                                              if p['instanceId'] == runtime.binding['instanceId'] and p['channel'] == 'pine')
                    result = self.status()
                    result['oneTimeIngress'] = {'instanceId': runtime.binding['instanceId'],
                                               'url': self.ingress.public_origin + '/signal/' + runtime.binding['instanceId'] + '/' + token,
                                               'generation': generation}
                    return result
                elif action in ('start-held', 'hold', 'enable-entries'):
                    require(data, ('instanceId',), ('acknowledgeDemoTrading', 'acknowledgeLiveTrading') if action == 'enable-entries' else ())
                    binding = self.manager.get(data['instanceId'])
                    runtime = self._open(binding['instanceId'])
                    if action == 'hold':
                        runtime.hold()
                    elif action == 'start-held':
                        runtime.hold()
                        if runtime.worker is None:
                            runtime.start()
                    else:
                        live = native_contract.native(binding) and binding['accountMode'] == 'live'
                        ensure(data.get('acknowledgeLiveTrading' if live else 'acknowledgeDemoTrading') is True,
                               'acknowledgment_required', 'Confirm new entries for this exact account environment.')
                        ensure(not self._blockers(binding, runtime), 'entry_checks_pending', 'Complete the listed bridge, account and calendar checks first.')
                        runtime.set_controls(enabled=True, locked=False)
                        if runtime.worker is None:
                            runtime.start()
                    self._health(binding['instanceId'], 'entries_held' if runtime.status()['entriesHeld'] else 'entries_enabled')
                else:
                    raise TradingError('not_found', 'This trading action is not available.', 404)
            except EngineError:
                raise TradingError('runtime_rejected', 'The runtime refused this configuration or lifecycle change. Its existing ledger is retained.') from None
            self.wake.set()
            return self.status()

    def _retire_instance(self, instance_id):
        if self.manager.retired_engine(instance_id):
            # Its engine is retired: there is no runtime to open, hold or close. The ledger is checked read only.
            self.manager.retire_retired_engine(instance_id)
            self.runtime.pop(instance_id, None)
            self.faults.pop(instance_id, None)
            self.route_problems.pop(instance_id, None)
            with self._db() as db:
                db.execute('DELETE FROM configuration WHERE name=?', ('feed:' + instance_id,))
            self._health(instance_id, 'retired_ledger_retained')
            return
        runtime = self._open(instance_id)
        # Refuse before stopping active management; never drain or dispatch here.
        runtime.hold()
        self.manager.retirement_precheck(runtime)
        feed = self.feeds.get(instance_id)
        if feed:
            feed.close()
            self.feeds.pop(instance_id)
        runtime.close()
        self.runtime.pop(instance_id, None)
        self.faults.pop(instance_id, None)
        self.manager.retire(runtime)
        with self._db() as db:
            db.execute('DELETE FROM configuration WHERE name=?', ('feed:' + instance_id,))
        self._health(instance_id, 'retired_ledger_retained')

    def _settled_module_runtime(self, runtime, *, native_flat):
        ensure(runtime.enabled is False and runtime.locked is True,
               'module_settled_required', 'Hold entries and finish or reconcile the current trade before this change.')
        try:
            if native_flat:
                return self.manager.retirement_precheck(runtime)
            return self.manager.observation_attach_precheck(runtime)
        except EngineError:
            raise TradingError('module_settled_required', 'Pending inputs, unresolved commands or native account evidence prevent this change.') from None

    def _start_feed(self, instance_id, chart_url):
        from trading_feed import FeedWorker
        runtime = self._open(instance_id)
        prior = self.feeds.get(instance_id)
        if prior:
            prior.close()
        feed = FeedWorker(runtime, self.market_browser, chart_url)
        self.feeds[instance_id] = feed
        feed.start()

    def _start_ingress(self, public_origin):
        from decrypt_trading import IngressListener
        # No automatic opening of an unobserved ledger from an incoming request.
        # Reader takes a stable reference; Runtime itself serializes admission.
        listener = IngressListener(lambda identity: self.runtime.get(identity), self.ingress_secrets.lease, public_origin)
        if self.ingress is not None:
            self.ingress.close()
            self.ingress = None
        try:
            self.ingress_state = {**listener.start(), 'publicOrigin': listener.public_origin}
            self.ingress = listener
        except Exception:
            listener.close()
            self.ingress_state = {'state': 'listener_unavailable', 'publicTlsVerified': False}
            raise TradingError('ingress_unavailable', 'The local alert receiver could not start. No public HTTPS connection was verified.') from None

    def check(self):
        with self.mutex:
            if self.closed:
                return
            vault = self.guardian.status()['vault']
            self._health('guardian', 'unlocked' if vault['unlocked'] else 'locked' if vault['configured'] else 'not_configured')
            for item in self.manager.list():
                binding = item['binding']
                instance_id = binding['instanceId']
                runtime = self.runtime.get(instance_id)
                if runtime is None:
                    continue
                if self._restore_held():
                    runtime.hold()
                    runtime.invalidate_confirmation('restore_reconciliation_required', producer_id=binding['brokerProducerId'])
                    self._health(instance_id, 'restore_reconciliation_required')
                    continue
                self._attach_bridge(runtime)
                snapshot = runtime.status()
                if snapshot.get('bridgeError'):
                    # Reason codes only: no provider body, URL or exception text in status.
                    self.faults[instance_id] = snapshot['bridgeError']
                else:
                    self.faults.pop(instance_id, None)
                state = 'failed' if snapshot.get('fault') else 'bridge_unknown' if not snapshot['nativeFresh'] else 'entries_held' if snapshot['entriesHeld'] else 'monitoring'
                self._health(instance_id, state)
            self._health('service', 'healthy')
            self.customer_health.observe(guardian=self.guardian.status(), trading=self.status())

    def start(self):
        with self.mutex:
            if self.worker is not None or self.closed:
                return False
            if not self._restore_held():
                for item in self.manager.list():
                    if item['registryState'] == 'active':
                        self._open(item['binding']['instanceId']).start()
                with self._db() as db:
                    ingress = db.execute('SELECT value FROM configuration WHERE name=?', ('ingress',)).fetchone()
                    feeds = list(db.execute("SELECT name,value FROM configuration WHERE name LIKE 'feed:%'"))
                for row in feeds:
                    value = json.loads(row['value'])
                    if value.get('resumeOnLogin') is True and row['name'][5:] in self.runtime:
                        self._start_feed(row['name'][5:], value['chartUrl'])
                if ingress:
                    try:
                        self._start_ingress(json.loads(ingress['value'])['publicOrigin'])
                    except Exception:
                        self.ingress_state = {'state': 'listener_unavailable', 'publicTlsVerified': False}
            def work():
                while not self.stopping.is_set():
                    try:
                        self.check()
                    except Exception:
                        self._health('service', 'check_failed')
                        try:
                            self.customer_health.observe(guardian=self.guardian.status(), trading=self.status())
                        except Exception:
                            pass
                    # Bounded native desktop delivery never runs under service,
                    # Guardian or engine locks. It does not change trade state.
                    try:
                        self.customer_health.dispatch_one()
                    except Exception:
                        self._health('notifications', 'delivery_check_failed')
                    self.wake.wait(2)
                    self.wake.clear()
            self.worker = threading.Thread(target=work, name='customer-trading-monitor', daemon=True)
            self.worker.start()
            return True

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.ingress is not None:
            self.ingress.close()
            self.ingress = None
        if self.worker:
            self.worker.join(timeout=30)
            ensure(not self.worker.is_alive(), 'service_busy', 'The trading worker has not stopped.')
        with self.mutex:
            for feed in self.feeds.values():
                feed.close()
            for runtime in self.runtime.values():
                runtime.close()
            self.closed = True
