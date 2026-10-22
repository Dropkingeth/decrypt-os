"""Durable customer instance and trusted-adapter boundary around Soup r4.

No endpoint, credential, publication grant or native truth is invented here.
The supplied adapters own authentication and native evidence acquisition.
"""
from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time

from .source import load_pair_sources, manifest, PAIR_ID, pair_spec, BRIDGE_ENGINES
from . import bridge_adapters
from . import customer_contract as native_contract

# No contract cap: the strategy alert sets the size.
# Every engine checks its own quantities are positive whole numbers that add up,
# and the bridge checks fills and position; nothing is refused for being large.
ENTRY_DECISION_MAX_AGE_MS = 120000


def route_digest(binding):
    return binding["routeSha256"] if native_contract.native(binding) else binding["endpointSha256"]


class RuntimeError(ValueError):
    pass


class Conflict(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def milliseconds():
    return time.time_ns() // 1000000


def _identifier(value):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value) is not None


def _private_directory(path):
    path = Path(path).absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    item = path.lstat()
    if (not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid()
            or stat.S_IMODE(item.st_mode) & 0o077):
        raise RuntimeError('runtime directory must be private and owned by this user')
    return path


def _private_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise RuntimeError('runtime state must be private regular single-link files')
    finally:
        os.close(fd)
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(path) + suffix)
        if side.exists() or side.is_symlink():
            info = side.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise RuntimeError('unsafe runtime database sidecar')


# A stored instance whose engine is no longer shipped (the pre-v2 strategy formats are retired).
# It is shown blocked and can be retired without loading its engine; it never opens, starts or trades.
RETIRED_ENGINE = 'retired_engine'
RETIRED_ENGINE_MESSAGE = 'Retired engine: this strategy now uses the new bridge format. Retire this instance.'


def _pair_supported(value):
    if native_contract.native(value):
        return native_contract.pair_supported(value)
    try:
        spec = pair_spec(value['pairId'])
    except (ValueError, KeyError, TypeError):
        return False
    return (value.get('engineSha256'), value.get('strategySha256')) == (spec['engineSha256'], spec['strategySha256'])


def validate_stored_binding(value):
    """(binding, retired): a stored binding, or one whose pair is retired (every other field still checked)."""
    if type(value) is dict and type(value.get('pairId')) is str and not _pair_supported(value):
        return validate_binding(value, retired_pair=True), True
    return validate_binding(value), False


def validate_binding(value, *, retired_pair=False):
    if native_contract.native(value):
        try:
            return native_contract.validate_binding(value, retired_pair=retired_pair)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
    fields = {'instanceId', 'customerId', 'installationId', 'pairId', 'engineSha256',
              'strategySha256', 'accountId', 'accountMode', 'routeId', 'subscriptionId',
              'endpointSha256', 'feedId', 'streamId', 'symbol', 'root', 'tickSize',
              'tp1LimitOffsetHandles', 'signalProducerId', 'feedProducerId', 'brokerProducerId'}
    if type(value) is not dict or set(value) != fields:
        raise RuntimeError('explicit instance binding fields required; secrets are not accepted')
    out = json.loads(canonical(value))
    for name in fields - {'pairId', 'engineSha256', 'strategySha256', 'endpointSha256',
                          'tickSize', 'tp1LimitOffsetHandles', 'accountMode'}:
        if not _identifier(out[name]):
            raise RuntimeError('invalid binding identity')
    if retired_pair:
        if (_pair_supported(out) or not _identifier(out['pairId'])
                or not all(type(out[k]) is str and re.fullmatch('[a-f0-9]{64}', out[k]) for k in ('engineSha256', 'strategySha256'))):
            raise RuntimeError('retired_pair_fields')
    else:
        try:
            spec = pair_spec(out['pairId'])
        except ValueError:
            raise RuntimeError('source pair has no supported runtime adapter') from None
        if (out['engineSha256'] != spec['engineSha256'] or out['strategySha256'] != spec['strategySha256']):
            raise RuntimeError('source pair has no supported runtime adapter')
    if out['accountMode'] != 'demo':
        raise RuntimeError('customer beta accepts demo accounts only')
    if (out['root'] != 'MNQ' or re.fullmatch(r'MNQ[HMUZ][0-9]{4}', out['symbol']) is None
            or type(out['tickSize']) not in (int, float) or out['tickSize'] != .25
            or re.fullmatch(r'[A-Za-z0-9_-]{1,48}', out['streamId']) is None):
        raise RuntimeError('explicit dated MNQ contract and stream required')
    offset = out['tp1LimitOffsetHandles']
    if (type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0
            or offset > 100 or offset / .25 != round(offset / .25)):
        raise RuntimeError('explicit tick-aligned TP1 limit offset required')
    if out['pairId'] != PAIR_ID and offset != 0:
        raise RuntimeError('paired native adapter requires exact strategy TP1 target without offset')
    if type(out['endpointSha256']) is not str or re.fullmatch('[a-f0-9]{64}', out['endpointSha256']) is None:
        raise RuntimeError('private route digest required')
    if len({out[k] for k in ('signalProducerId', 'feedProducerId', 'brokerProducerId')}) != 3:
        raise RuntimeError('signal, market and broker authorities must be separate')
    return out


class RuntimeManager:
    """Local package installation and immutable instance bindings.

    One account and dedicated route per instance are deliberate beta constraints:
    the preserved engine's cancel/flatten messages are account-scoped. Rebinding
    or deleting a used ledger is not supported. Close and archive it explicitly.
    """
    def __init__(self, root):
        self.root = _private_directory(root)
        self.path = self.root / 'instances.sqlite'
        _private_file(self.path)
        manifest()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS instances (id TEXT PRIMARY KEY, '
                       'account_id TEXT NOT NULL UNIQUE, route_id TEXT NOT NULL UNIQUE, '
                       'endpoint_sha256 TEXT NOT NULL UNIQUE, binding TEXT NOT NULL, created_ms INTEGER NOT NULL)')
            try:
                # Optional Size Lock module setting (default off); see
                # set_size_lock().
                db.execute('ALTER TABLE instances ADD COLUMN size_lock_enabled INTEGER NOT NULL DEFAULT 0')
            except sqlite3.OperationalError:
                pass
            from .retirement import initialize
            initialize(db)

    @contextmanager
    def _db(self):
        _private_file(self.path)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def install(self, binding):
        binding = validate_binding(binding)
        load_pair_sources(binding['pairId'])
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM retired_instances WHERE id=?',(binding['instanceId'],)).fetchone():
                raise Conflict('retired instance ID is preserved; use explicit reactivation')
            old = db.execute('SELECT binding FROM instances WHERE id=?', (binding['instanceId'],)).fetchone()
            if old:
                if old['binding'] != canonical(binding):
                    raise Conflict('installed identity is immutable; do not rebind a ledger')
                return self.get(binding['instanceId'])
            from .retirement import total, MAX_INSTANCES
            if total(db) >= MAX_INSTANCES:
                raise Conflict('maximum 32 retained instances; archives are not deleted or reused')
            try:
                db.execute('INSERT INTO instances (id, account_id, route_id, endpoint_sha256, binding, created_ms) VALUES (?,?,?,?,?,?)',
                           (binding['instanceId'], binding['accountId'], binding['routeId'],
                            route_digest(binding), canonical(binding), milliseconds()))
                _private_directory(self.root / binding['instanceId'])
            except sqlite3.IntegrityError as exc:
                raise Conflict('account or route is already assigned to another instance') from exc
        return self.get(binding['instanceId'])

    def get(self, instance_id, *, include_retired=False):
        if not _identifier(instance_id):
            raise RuntimeError('invalid instance identity')
        with self._db() as db:
            row = db.execute('SELECT binding FROM instances WHERE id=?', (instance_id,)).fetchone()
            if row is None and include_retired:
                row = db.execute('SELECT binding FROM retired_instances WHERE id=?',(instance_id,)).fetchone()
                if row is not None:
                    return json.loads(row['binding'])
        if row is None:
            raise RuntimeError('instance is not active')
        binding, retired = validate_stored_binding(json.loads(row['binding']))
        if retired:
            raise RuntimeError(RETIRED_ENGINE_MESSAGE)
        return binding

    def retired_engine(self, instance_id):
        """True when this active instance's engine is retired (shown blocked; retire it with retire_retired_engine)."""
        if not _identifier(instance_id):
            raise RuntimeError('invalid instance identity')
        with self._db() as db:
            row = db.execute('SELECT binding FROM instances WHERE id=?', (instance_id,)).fetchone()
        if row is None:
            raise RuntimeError('instance is not active')
        return validate_stored_binding(json.loads(row['binding']))[1]

    def size_lock_enabled(self, instance_id):
        if not _identifier(instance_id):
            raise RuntimeError('invalid instance identity')
        with self._db() as db:
            row = db.execute('SELECT size_lock_enabled FROM instances WHERE id=?', (instance_id,)).fetchone()
        if row is None:
            raise RuntimeError('instance is not active')
        return bool(row['size_lock_enabled'])

    def set_size_lock(self, instance_id, enabled):
        """Optional Size Lock module (default off), Soup only.

        Enabled: one contract, assigned to the original TP1 trigger, original
        protective exits (the engine's existing 'account-fixed-one-tp1-v1'
        quantity policy). Disabled (default): the existing original-allocation
        policy, unchanged. Never a free-form/arbitrary fixed-N size.
        """
        if type(enabled) is not bool:
            raise RuntimeError('size lock enabled must be an explicit boolean')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT binding FROM instances WHERE id=?', (instance_id,)).fetchone()
            if row is None:
                raise RuntimeError('instance is not active')
            binding = json.loads(row['binding'])
            if not (native_contract.native(binding) and binding['pairId'] == native_contract.SOUP):
                raise RuntimeError('size lock is only supported for the Soup engine')
            db.execute('UPDATE instances SET size_lock_enabled=? WHERE id=?', (1 if enabled else 0, instance_id))

    def list(self, *, include_retired=False):
        with self._db() as db:
            rows = db.execute('SELECT id,binding FROM instances ORDER BY id').fetchall()
            retired = db.execute('SELECT id,binding,retired_ms,evidence FROM retired_instances ORDER BY id').fetchall() if include_retired else []
        result = []
        for row in rows:
            binding, retired_pair = validate_stored_binding(json.loads(row['binding']))
            if retired_pair:
                # Shown, never opened: no engine is loaded for a retired pair.
                result.append({'binding': binding, 'packageInstalled': False, 'registryState': 'retired_engine',
                               'runtimeState': RETIRED_ENGINE, 'entriesHeld': True, 'entryReady': False,
                               'tradePhase': self._stored_trade_phase(row['id']), 'blockers': [RETIRED_ENGINE],
                               'message': RETIRED_ENGINE_MESSAGE, 'providerCommissioned': False,
                               'publicationVerified': False})
                continue
            result.append({'binding': binding, 'packageInstalled': True,
                           'registryState': 'active', 'runtimeState': 'not_observed', 'providerCommissioned': False,
                           'publicationVerified': False})
        result += [{'binding': json.loads(row['binding']), 'packageInstalled': True, 'registryState': 'retired',
                    'runtimeState': 'retired', 'entriesHeld': True, 'entryReady': False,
                    'retiredAt': row['retired_ms'], 'retirementBasis': json.loads(row['evidence'])['basis'],
                    'providerCommissioned': False, 'publicationVerified': False} for row in retired]
        return sorted(result, key=lambda row: row['binding']['instanceId'])

    def _stored_trade_phase(self, instance_id):
        """Read-only trade phase of a retired engine's ledger ('idle' when it never traded), for display only."""
        path = self.root / instance_id / 'engine.sqlite'
        try:
            db = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
            finally:
                db.close()
            trade = json.loads(row[0]).get('trade') if row else None
            return 'idle' if trade is None else str(trade.get('phase') or 'unobserved')
        except Exception:
            return 'unobserved'

    def open(self, instance_id, *, transport=None, clock=milliseconds, dispatch_guard=None, entry_guard=None):
        if not _identifier(instance_id):
            raise RuntimeError('invalid instance identity')
        # Registry lock → instance writer lock: retirement cannot race a new open.
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT binding,size_lock_enabled FROM instances WHERE id=?',(instance_id,)).fetchone()
            if row is None:
                raise RuntimeError('instance is not active; retired instances cannot open or ingest')
            binding, retired = validate_stored_binding(json.loads(row['binding']))
            if retired:
                raise RuntimeError(RETIRED_ENGINE_MESSAGE)
            return Runtime(self.root / instance_id, binding, transport=transport, clock=clock, dispatch_guard=dispatch_guard, entry_guard=entry_guard,
                           size_lock_enabled=bool(row['size_lock_enabled']))

    def retirement_precheck(self, runtime):
        from .retirement import precheck
        return precheck(self, runtime)

    def observation_attach_precheck(self, runtime):
        """Local closed-state check only; never claims broker flat or retirement."""
        from .retirement import precheck
        result = precheck(self, runtime, require_native_flat=False)
        return {key: value for key, value in result.items() if key != 'retirementAllowed'}

    def retire(self, runtime):
        from .retirement import retire
        return retire(self, runtime)

    def retire_retired_engine(self, instance_id, *, clock=milliseconds):
        """Retire an instance whose engine is retired, without loading that engine (ledger read only)."""
        from .retirement import retire_retired_engine
        return retire_retired_engine(self, instance_id, clock=clock)

    def reactivate(self, instance_id, *, clock=milliseconds):
        from .retirement import reactivate
        return reactivate(self, instance_id, clock=clock)


class Runtime:
    def __init__(self, directory, binding, *, transport=None, clock=milliseconds, dispatch_guard=None, entry_guard=None, size_lock_enabled=False):
        self.binding = validate_binding(binding)
        self.size_lock_enabled = bool(size_lock_enabled)
        self.directory = _private_directory(directory)
        self.modules = load_pair_sources(binding['pairId'])
        self.clock = clock
        if dispatch_guard is not None and not callable(dispatch_guard):
            raise RuntimeError('internal dispatch guard must be callable')
        self.dispatch_guard = dispatch_guard
        if entry_guard is not None and not callable(entry_guard):
            raise RuntimeError('internal entry guard must be callable')
        self.entry_guard = entry_guard
        self.opened_at = clock()
        self.market_invalidated_at = self.opened_at - 1
        self.enabled, self.locked = False, True
        self.transport = transport
        # The Tradovate bridge route (bridge_route.BridgeTransport): Soup only, see bridge_route.py.
        self.bridge = transport if getattr(transport, 'sends_payload', False) else None
        self.bridge_error = None
        self.proof = None
        self.confirmation_invalid_reason = None
        self.market_invalid_reason = None
        self.mutex = threading.RLock()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.worker = None
        self.closed = False
        self._closing = False
        self._engine_connection_closed = False
        self.fault = None
        self.writer = self.modules['storage'].WriterLock(self.directory)
        try:
            path = self.directory / 'engine.sqlite'
            _private_file(path)
            if path.stat().st_size:
                check = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
                try:
                    tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if 'customer_runtime_metadata' not in tables:
                        raise Conflict('unbound database cannot be adopted as a customer instance')
                    old = check.execute("SELECT value FROM customer_runtime_metadata WHERE key='binding'").fetchone()
                    if old is None or old[0] != hashlib.sha256(canonical(binding).encode()).hexdigest():
                        raise Conflict('database binding differs; no migration was attempted')
                finally:
                    check.close()
            config = {'account_id': binding['accountId'], 'ticker': binding['symbol'],
                      'root': binding['root'], 'mintick': binding['tickSize'], 'stream_id': binding['streamId']}
            native_options = {}
            if native_contract.native(binding) and binding['pairId'] == native_contract.SOUP:
                native_binding = {'broker_id': 'tradovate', 'account_value': str(binding['nativeAccountId']),
                    'account_label': binding['nativeAccountName'], 'account_url_sha256': binding['routeSha256']}
                # Optional Size Lock module (default off): one contract, assigned
                # to the original TP1 trigger, original protective exits. Off by
                # default keeps the existing original-allocation policy exactly
                # as it was. See set_size_lock().
                if self.size_lock_enabled:
                    # The engine's native_exits feature (resting broker stop,
                    # BE move via native orders) requires the 'original'
                    # allocation (vendor/soup_r4/core.py, validate_native_exits);
                    # it refuses 'tp1_only'. Size Lock falls back to the
                    # engine's own non-native protective exits instead, which
                    # is exactly the "original protective exits" the module's
                    # one-contract design preserves.
                    native_options = {'quantity_policy': {'id': 'account-fixed-one-tp1-v1', 'account_id': binding['accountId'],
                        'max_contracts': 1, 'exit_allocation': 'tp1_only', 'native_binding': native_binding}}
                else:
                    native_options = {
                        'quantity_policy': {'id': 'account-original-allocation-micro-cap-v1', 'account_id': binding['accountId'],
                            'max_contracts': 10, 'exit_allocation': 'original', 'native_binding': native_binding},
                        'native_exits': {'id': 'soup-native-exits-v1', 'account_id': binding['accountId'],
                            'modify_confirm_ms': 2500, 'cancel_confirm_ms': 5000, 'stop_confirm_ms': 5000,
                            'backstop_ms': 2000, 'exit_verify_ms': 5000, 'reduce_confirm_ms': 5000, 'max_exit_attempts': 3}}
            self.engine = self.modules['core'].Engine(path, config,
                tp1_limit_offset_handles=binding['tp1LimitOffsetHandles'], **native_options)
            self.engine.db.executescript('''
              CREATE TABLE IF NOT EXISTS customer_runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS customer_runtime_audit (seq INTEGER PRIMARY KEY, at INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS customer_runtime_inputs (producer TEXT NOT NULL, channel TEXT NOT NULL, event_id TEXT NOT NULL, digest TEXT NOT NULL, inbox_seq INTEGER NOT NULL, PRIMARY KEY(producer,channel,event_id));
              CREATE TABLE IF NOT EXISTS customer_runtime_cursors (producer TEXT NOT NULL, channel TEXT NOT NULL, sequence INTEGER NOT NULL, PRIMARY KEY(producer,channel));
            ''')
            digest = hashlib.sha256(canonical(binding).encode()).hexdigest()
            existing = self.engine.db.execute("SELECT value FROM customer_runtime_metadata WHERE key='binding'").fetchone()
            if existing and existing[0] != digest:
                raise Conflict('runtime database belongs to another immutable binding')
            self.engine.db.execute("INSERT OR IGNORE INTO customer_runtime_metadata VALUES ('binding',?)", (digest,))
            self.engine.db.execute("INSERT OR IGNORE INTO customer_runtime_metadata VALUES ('broker_sequence','0')")
            self.engine.db.execute("INSERT OR IGNORE INTO customer_runtime_metadata VALUES ('bridge_cursor','0')")
            # Entries accepted before this process lifetime must not become new
            # orders after a restart. Management and uncertain commands persist.
            for row in self.engine.db.execute("SELECT seq,body FROM inbox WHERE status='pending' AND channel='pine'").fetchall():
                payload = self.modules['storage'].strict_json(row[1])
                if not self.is_entry_payload(payload):
                    continue
                trade_id = self.input_identity(payload)
                if type(trade_id) is str and 0 < len(trade_id) <= 240:
                    self.engine.db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)',
                                           ('pine:' + trade_id, self.modules['storage'].digest(payload), 'entry_disabled_on_restart'))
                self.engine.db.execute("UPDATE inbox SET status='rejected',detail='restart_entry_not_replayed' WHERE seq=?", (row[0],))
            self.engine.db.commit()
            if any(c['kind'] == 'entry' and c['status'] == 'ready' for c in self.engine.data['commands'].values()):
                self.engine.discard_entry('restart_entry_not_replayed')
            self._audit('opened_held', 'native confirmation and explicit release required')
            self.open_generation = self.engine.db.execute("SELECT max(seq) FROM customer_runtime_audit WHERE kind='opened_held'").fetchone()[0]
            if self.bridge is not None:
                self._check_bridge_binding(self.bridge)
                self.bridge.link(self)
        except BaseException:
            if hasattr(self, 'engine'):
                self.engine.close()
            self.writer.close()
            raise

    def _audit(self, kind, detail):
        self.engine.db.execute('INSERT INTO customer_runtime_audit(at,kind,detail) VALUES (?,?,?)',
                               (self.clock(), kind, detail))
        self.engine.db.commit()

    def _check(self):
        if self.closed or self._closing or self.fault:
            raise RuntimeError('runtime is unavailable')
        self.writer.validate()

    def _adapter_route(self):
        """beta4: this instance is a registered, pinned strategy adapter on the bridge (not Soup)."""
        return self.binding['pairId'] != PAIR_ID and self.binding['pairId'] in BRIDGE_ENGINES

    def _check_bridge_binding(self, transport):
        if not (native_contract.native(self.binding) and (self.binding['pairId'] == PAIR_ID or self._adapter_route())):
            raise RuntimeError('the bridge route carries Soup or registered adapter orders only')
        if getattr(transport, 'binding_sha256', None) != native_contract.digest(self.binding):
            raise RuntimeError('bridge route belongs to another binding')

    def attach_bridge(self, transport):
        """Attach the bridge route once the bridge is set up (the engine may be installed first)."""
        with self.mutex:
            self._check()
            if self.bridge is not None:
                raise RuntimeError('bridge route already attached')
            self._check_bridge_binding(transport)
            transport.link(self)
            self.transport = self.bridge = transport
            self._audit('bridge_route_attached', 'orders go to the Tradovate bridge')
        self.wake.set()

    def bridge_cursor(self):
        with self.mutex:
            return int(self.engine.db.execute("SELECT value FROM customer_runtime_metadata WHERE key='bridge_cursor'").fetchone()[0])

    def admit_bridge_event(self, seq, raw, *, deliver):
        """Queue one broker event read from the bridge's adapter database and advance its cursor.

        Stale events (deliver False) only advance the cursor; they are never replayed as fresh.
        """
        if self.bridge is None:
            raise RuntimeError('broker events need the bridge route')
        if type(seq) is not int or seq <= 0:
            raise RuntimeError('invalid adapter sequence')
        payload = self.modules['storage'].strict_json(raw, max_bytes=40960) if deliver else None
        with self.mutex:
            self._check()
            db = self.engine.db
            db.execute('BEGIN IMMEDIATE')
            try:
                cursor = int(db.execute("SELECT value FROM customer_runtime_metadata WHERE key='bridge_cursor'").fetchone()[0])
                if cursor and seq != cursor + 1:
                    raise RuntimeError('adapter sequence gap; review required')
                if deliver:
                    if db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0] >= 10000:
                        raise RuntimeError('bounded inbox is full')
                    db.execute('INSERT INTO inbox(channel,received_ms,body) VALUES (?,?,?)',
                               ('broker', self.clock(), canonical(payload)))
                db.execute("UPDATE customer_runtime_metadata SET value=? WHERE key='bridge_cursor'", (str(seq),))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        self.wake.set()

    def set_controls(self, *, enabled, locked):
        if type(enabled) is not bool or type(locked) is not bool:
            raise RuntimeError('controls require explicit booleans')
        with self.mutex:
            self._check()
            self.enabled, self.locked = enabled, locked
            self._audit('entry_controls', 'enabled' if enabled and not locked else 'held')
        self.wake.set()
        return self.status()

    def hold(self):
        return self.set_controls(enabled=False, locked=True)

    def _producer(self, channel, producer_id):
        field = {'pine': 'signalProducerId', 'quote': 'feedProducerId', 'bar': 'feedProducerId', 'broker': 'brokerProducerId'}
        if channel not in field or producer_id != self.binding[field[channel]]:
            raise RuntimeError('authenticated producer does not own this channel')

    def ingest(self, channel, raw, *, producer_id):
        """Internal adapter API. Authenticate producer BEFORE invoking this method.

        ``producer_id`` must be server-derived, not taken from client JSON. Broker
        inputs use publish_confirmation(), not this raw engine-event endpoint.
        """
        self._producer(channel, producer_id)
        if channel == 'broker':
            raise RuntimeError('broker events require complete native confirmation')
        payload = self.modules['storage'].strict_json(raw, max_bytes=40960)
        if payload.get('symbol', payload.get('ticker')) != self.binding['symbol']:
            raise RuntimeError('input instrument mismatch')
        if channel == 'pine' and self.binding['pairId'] == PAIR_ID and payload.get('stream_id') != self.binding['streamId']:
            raise RuntimeError('input stream mismatch')
        if channel == 'pine':
            self._check_payload_schema(payload)
        with self.mutex:
            self._check()
            if self.engine.db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0] >= 10000:
                raise RuntimeError('bounded inbox is full')
            cursor = self.engine.db.execute('INSERT INTO inbox(channel,received_ms,body) VALUES (?,?,?)',
                                            (channel, self.clock(), canonical(payload)))
            self.engine.db.commit()
            seq = cursor.lastrowid
        self.wake.set()
        return {'receipt': seq, 'meaning': 'durable input only; not execution or fill'}

    def publish_confirmation(self, snapshot, *, producer_id):
        """Internal native adapter contract, not a customer-editable assertion.

        The adapter must correlate native order IDs through retained placement
        receipts. Passing this schema does not authenticate its producer.
        """
        self._producer('broker', producer_id)
        if self.bridge is not None:
            raise RuntimeError('the bridge route confirms through the bridge adapter database')
        # A new unknown or conflicting native result invalidates the old green
        # projection immediately, even when its previous TTL has not elapsed.
        with self.mutex:
            self._check()
            self.proof = None
        if native_contract.native(self.binding):
            try:
                native_contract.validate_complete_confirmation(snapshot, self.binding, at=self.clock(), not_before=self.opened_at)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from None
            snapshot = json.loads(canonical(snapshot))
        else:
            fields = {'schema', 'instanceId', 'accountId', 'pairId', 'symbol', 'sequence', 'observedAt',
                      'positionsComplete', 'ordersComplete', 'pendingComplete', 'correlationComplete',
                      'routeVerified', 'accountMode', 'routeId', 'subscriptionId', 'endpointSha256',
                      'entryRecipients', 'positionQuantity', 'workingOrderIds', 'pendingOrderIds',
                      'unattributedOrderIds', 'otherContractExposure', 'engineEvent'}
            if type(snapshot) is not dict or set(snapshot) != fields:
                raise RuntimeError('complete native confirmation envelope required')
            snapshot = json.loads(canonical(snapshot))
            for key in ('instanceId', 'accountId', 'pairId', 'symbol', 'accountMode', 'routeId', 'subscriptionId', 'endpointSha256'):
                if snapshot[key] != self.binding[key]:
                    raise RuntimeError('native confirmation binding mismatch')
            if snapshot['schema'] != 'decrypt.customer-broker-confirmation.v1':
                raise RuntimeError('unsupported native confirmation')
            if snapshot['entryRecipients'] != [self.binding['accountId']]:
                raise RuntimeError('route is not exclusive to the intended demo account')
        for key in ('positionsComplete', 'ordersComplete', 'pendingComplete', 'correlationComplete', 'routeVerified'):
            if snapshot[key] is not True:
                raise RuntimeError('native confirmation remains incomplete or unknown')
        if (type(snapshot['sequence']) is not int or snapshot['sequence'] <= 0
                or type(snapshot['observedAt']) is not int or type(snapshot['positionQuantity']) is not int
                or snapshot['positionQuantity'] < 0 or snapshot['otherContractExposure'] is not False):
            raise RuntimeError('invalid or cross-contract native evidence')
        for key in ('workingOrderIds', 'pendingOrderIds', 'unattributedOrderIds'):
            values = snapshot[key]
            if (type(values) is not list or len(values) > 10000
                    or any(type(v) is not str or not 0 < len(v) <= 240 for v in values)
                    or len(values) != len(set(values))):
                raise RuntimeError('invalid native order inventory')
        if snapshot['unattributedOrderIds']:
            raise RuntimeError('unattributed native orders need reconciliation')
        with self.mutex:
            self._check()
            now = self.clock()
            if not self.opened_at <= snapshot['observedAt'] <= now or now - snapshot['observedAt'] > 12000:
                raise RuntimeError('native confirmation is stale or from a previous runtime')
            previous = int(self.engine.db.execute("SELECT value FROM customer_runtime_metadata WHERE key='broker_sequence'").fetchone()[0])
            if snapshot['sequence'] <= previous:
                raise RuntimeError('native confirmation sequence must advance across restart')
            event = snapshot['engineEvent']
            trade = self.engine.data['trade']
            if trade and trade['phase'] != 'closed':
                if type(event) is not dict:
                    raise RuntimeError('active trade requires native command-correlated engine evidence')
                if set(event) != {'event_id', 'sequence', 'observed_at', 'account_id', 'symbol',
                                  'trade_id', 'side', 'position_qty', 'orders', 'open_orders'}:
                    raise RuntimeError('native engine event fields are not allowlisted')
                if type(event['orders']) is not list or len(event['orders']) > 10000:
                    raise RuntimeError('native engine order inventory exceeds bounds')
                # Native confirmations were strictly checked above by the shared
                # contract, including bracket roles, parent/stop evidence and
                # child IDs. Keep the legacy shape confined to legacy routes.
                if not native_contract.native(self.binding):
                    for order in event['orders']:
                        if type(order) is not dict or set(order) != {'command_id', 'broker_order_id', 'status', 'filled_qty', 'fill_time'}:
                            raise RuntimeError('native order evidence fields are not allowlisted')
                        if any(type(order[k]) is not str or re.fullmatch(r'[A-Za-z0-9_.:@+-]{1,240}', order[k]) is None
                               for k in ('command_id', 'broker_order_id')):
                            raise RuntimeError('native order identity is malformed')
                if (event.get('account_id') != snapshot['accountId'] or event.get('symbol') != snapshot['symbol']
                        or event.get('trade_id') != trade['id'] or event.get('position_qty') != snapshot['positionQuantity']
                        or event.get('observed_at') != snapshot['observedAt'] or event.get('sequence') != snapshot['sequence']
                        or sorted(event.get('open_orders', [])) != sorted(snapshot['workingOrderIds'])
                        or snapshot['pendingOrderIds']):
                    raise RuntimeError('native engine/inventory evidence disagree')
                self.engine.apply('broker', event, now, entries_enabled=self.enabled, locked=self.locked)
            elif event is not None:
                raise RuntimeError('idle account cannot import another trade event')
            self.engine.db.execute("UPDATE customer_runtime_metadata SET value=? WHERE key='broker_sequence'", (str(snapshot['sequence']),))
            self.engine.db.commit()
            self.proof = snapshot
            self.confirmation_invalid_reason = None
            self._audit('native_confirmation', 'complete account-bound snapshot accepted')
        self.wake.set()
        return self.status()

    def admit_authenticated(self, channel, raw, *, producer_id, event_id, sequence=None):
        """Authenticated adapters only: atomically deduplicate and append an input.

        A sequence here is the producer/relay acquisition counter, not a market
        or broker order sequence. It may have gaps but cannot rewind. Raw Pine
        alerts use their exact source identity and do not invent a counter.
        """
        self._producer(channel, producer_id)
        if channel not in ('pine', 'quote', 'bar'):
            raise RuntimeError('unsupported authenticated input channel')
        if type(event_id) is not str or re.fullmatch(r'[A-Za-z0-9_.:@+-]{1,240}', event_id) is None:
            raise RuntimeError('bounded input identity required')
        if sequence is not None and (type(sequence) is not int or not 0 < sequence < 2**63):
            raise RuntimeError('invalid producer acquisition sequence')
        payload = self.modules['storage'].strict_json(raw, max_bytes=40960)
        if type(payload) is not dict or payload.get('symbol', payload.get('ticker')) != self.binding['symbol']:
            raise RuntimeError('input instrument mismatch')
        body, digest = canonical(payload), self.modules['storage'].digest(payload)
        with self.mutex:
            self._check()
            old = self.engine.db.execute('SELECT digest,inbox_seq FROM customer_runtime_inputs WHERE producer=? AND channel=? AND event_id=?',
                                         (producer_id, channel, event_id)).fetchone()
            if old:
                if old[0] != digest:
                    raise Conflict('authenticated input identity changed content')
                return {'receipt': old[1], 'duplicate': True, 'meaning': 'durable input only; not execution or fill'}
            if channel == 'pine':
                self._check_payload_schema(payload)
                actual_id = self.input_identity(payload)
                if event_id != actual_id:
                    raise RuntimeError('authenticated Pine identity mismatch')
                self.engine.validate_pine(payload, self.clock())
            else:
                fields = {'event_id', 'symbol', 'time', 'price'} if channel == 'quote' else {'event_id', 'symbol', 'open_time', 'close_time', 'open', 'high', 'low', 'close'}
                if set(payload) != fields or payload.get('event_id') != event_id:
                    raise RuntimeError('authenticated feed fields or identity mismatch')
                if any(type(payload[k]) not in (int, float) or not math.isfinite(payload[k]) or payload[k] <= 0
                       for k in ({'price'} if channel == 'quote' else {'open', 'high', 'low', 'close'})):
                    raise RuntimeError('invalid market price')
                at = payload.get('time') if channel == 'quote' else payload.get('close_time')
                if type(at) is not int or not 0 <= self.clock()-at <= (2000 if channel == 'quote' else 15000):
                    raise RuntimeError('stale or future market input')
                if channel == 'bar':
                    start = payload.get('open_time')
                    if (type(start) is not int or at-start not in self.market_bar_intervals() or start % (at-start)
                            or not payload['low'] <= min(payload['open'], payload['close']) <= max(payload['open'], payload['close']) <= payload['high']):
                        raise RuntimeError('invalid completed native bar')
            db = self.engine.db
            db.execute('BEGIN IMMEDIATE')
            try:
                cursor = db.execute('SELECT sequence FROM customer_runtime_cursors WHERE producer=? AND channel=?', (producer_id, channel)).fetchone()
                if sequence is not None and cursor and sequence <= cursor[0]:
                    raise Conflict('producer acquisition sequence did not advance')
                if db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0] >= 10000:
                    raise RuntimeError('bounded inbox is full')
                held = channel == 'pine' and self.is_entry_payload(payload) and (not self.enabled or self.locked or not self._entry_allowed())
                result = db.execute('INSERT INTO inbox(channel,received_ms,body,status,detail) VALUES (?,?,?,?,?)',
                                    (channel, self.clock(), body, 'rejected' if held else 'pending', 'entry_held_at_admission' if held else None))
                receipt = result.lastrowid
                if held:
                    db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?)', ('pine:'+event_id, digest, 'entry_disabled_at_admission'))
                db.execute('INSERT INTO customer_runtime_inputs VALUES (?,?,?,?,?)', (producer_id, channel, event_id, digest, receipt))
                if sequence is not None:
                    db.execute('INSERT OR REPLACE INTO customer_runtime_cursors VALUES (?,?,?)', (producer_id, channel, sequence))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        self.wake.set()
        return {'receipt': receipt, 'duplicate': False, 'held': held, 'meaning': 'durable input only; not execution or fill'}

    def _check_payload_schema(self, payload):
        """beta4: a registered adapter accepts only the alert schemas (format versions) it declares."""
        if self.binding['pairId'] not in BRIDGE_ENGINES:
            return
        try:
            bridge_adapters.lookup(self.binding['pairId'], self.modules['core'].payload_schema(payload))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RuntimeError('alert schema is not declared by this adapter') from None

    def input_identity(self, payload):
        if hasattr(self.modules['core'], 'input_identity'):
            return self.modules['core'].input_identity(payload)
        return payload.get('trade_id') if payload.get('event') == 'entry' else payload.get('signalId')

    def is_entry_payload(self, payload):
        if hasattr(self.modules['core'], 'is_entry_payload'):
            return self.modules['core'].is_entry_payload(payload)
        return payload.get('event') == 'entry'

    def market_bar_intervals(self):
        if hasattr(self.engine, 'market_bar_intervals'):
            return tuple(self.engine.market_bar_intervals())
        trade = self.engine.data['trade']
        if trade and trade['phase'] != 'closed':
            return (trade['payload']['execution_context']['chart_seconds'] * 1000,)
        return ()

    def invalidate_confirmation(self, reason, *, producer_id):
        """Withdraw a previous native observation without inventing a new one."""
        self._producer('broker', producer_id)
        if type(reason) is not str or re.fullmatch(r'[a-z][a-z0-9_]{0,95}', reason) is None:
            raise RuntimeError('bounded non-secret reason code required')
        with self.mutex:
            self._check()
            self.proof = None
            if reason != self.confirmation_invalid_reason:
                self._audit('native_confirmation_invalidated', reason)
            self.confirmation_invalid_reason = reason
        return self.status()

    def invalidate_market(self, reason, *, producer_id):
        self._producer('quote', producer_id)
        if type(reason) is not str or re.fullmatch(r'[a-z][a-z0-9_]{0,95}', reason) is None:
            raise RuntimeError('bounded non-secret reason code required')
        with self.mutex:
            self._check()
            self.market_invalidated_at = self.clock()
            if reason != self.market_invalid_reason:
                self._audit('market_invalidated', reason)
            self.market_invalid_reason = reason
        return self.status()

    def _route_identity(self):
        return bool(self.transport is not None
                    and getattr(self.transport, 'account_id', None) == self.binding['accountId']
                    and getattr(self.transport, 'route_id', None) == self.binding['routeId']
                    and getattr(self.transport, 'route_sha256', None) == self.binding.get('routeSha256')
                    and getattr(self.transport, 'binding_sha256', None) == native_contract.digest(self.binding))

    def _route_ready(self):
        if self.bridge is not None:
            # Fresh bridge account state is checked by the route before every send.
            return self._route_identity()
        return bool(self.proof and self.transport is not None
                    and getattr(self.transport, 'account_id', None) == self.binding['accountId']
                    and getattr(self.transport, 'route_id', None) == self.binding['routeId']
                    and ((native_contract.native(self.binding)
                          and getattr(self.transport, 'route_sha256', None) == self.binding['routeSha256']
                          and getattr(self.transport, 'binding_sha256', None) == native_contract.digest(self.binding))
                         or (not native_contract.native(self.binding)
                             and getattr(self.transport, 'endpoint_sha256', None) == self.binding['endpointSha256'])))

    def _entry_ready(self, now):
        if self.bridge is not None:
            # Bridge route (the Soup engine's bridge transport): controls and guards here; the fresh bridge
            # account state is the dispatch gate (_bridge_entry_ready).
            return bool(self._entry_allowed() and self._dispatch_allowed() and self._route_identity()
                        and not self.bridge.entry_blocked())
        proof = self.proof
        quote = self.engine.data['last_quote'] or {}
        source_ready = not hasattr(self.engine, 'entry_source_ready') or self.engine.entry_source_ready(now, self.opened_at)
        return bool(source_ready and self._entry_allowed() and self._dispatch_allowed() and self._route_ready() and proof and 0 <= now - proof['observedAt'] <= 12000
                    and proof['positionQuantity'] == 0 and not proof['workingOrderIds']
                    and not proof['pendingOrderIds'] and self.opened_at <= quote.get('time', -1) <= now
                    and quote.get('time', -1) > self.market_invalidated_at
                    and now - quote['time'] <= 2000)

    def _bridge_entry_ready(self, now):
        quote = self.engine.data['last_quote'] or {}
        source_ready = not hasattr(self.engine, 'entry_source_ready') or self.engine.entry_source_ready(now, self.opened_at)
        return bool(source_ready and quote.get('time', -1) > self.market_invalidated_at
                    and self.bridge.route.entry_ready(self.engine.data, now, self.opened_at))

    def _dispatch_allowed(self):
        try:
            return self.dispatch_guard is None or self.dispatch_guard() is True
        except Exception:
            return False

    def _entry_allowed(self):
        try:
            return self.entry_guard is None or self.entry_guard() is True
        except Exception:
            return False

    def step(self):
        """Process one input, tick strategy time and dispatch in original order."""
        with self.mutex:
            self._check()
            now = self.clock()
            row = self.engine.db.execute("SELECT seq,channel,received_ms,body FROM inbox WHERE status='pending' ORDER BY seq LIMIT 1").fetchone()
            if row:
                seq, channel, received, body = row
                payload = self.modules['storage'].strict_json(body)
                try:
                    outcome = self.engine.apply(channel, payload, now, entries_enabled=self.enabled,
                                                locked=self.locked or (channel == 'pine' and self.is_entry_payload(payload) and not self._entry_ready(now)))
                    if channel == 'quote' and payload.get('time', -1) > self.market_invalidated_at:
                        self.market_invalid_reason = None
                    result = 'processed'
                except (ValueError, TypeError, KeyError, OverflowError):
                    outcome, result = 'input_rejected', 'rejected'
                self.engine.db.execute('UPDATE inbox SET status=?,detail=? WHERE seq=?', (result, outcome, seq))
                self.engine.db.commit()
                # Dispatch immediately after each input, before later inbox work.
                self._dispatch(now)
            self.engine.tick(now)
            self._dispatch(now)
            return bool(row)

    def _dispatch(self, now):
        while self.engine.ready():
            if not self._dispatch_allowed():
                self.enabled, self.locked = False, True
                if any(c['kind'] == 'entry' for c in self.engine.ready()):
                    self.engine.discard_entry('internal_dispatch_guard_held')
                return
            command = self.engine.ready()[0]
            adapter_route = self.bridge is not None and self._adapter_route()
            if command['kind'] == 'entry' or (adapter_route and command['kind'] in bridge_adapters.ENTRY_KINDS):
                if not self.enabled or self.locked or not self._entry_ready(now):
                    self.engine.discard_entry('entry_readiness_not_confirmed')
                    continue
                if adapter_route and now - self.modules['core'].signal_time(self.engine.data['trade']) \
                        > ENTRY_DECISION_MAX_AGE_MS:
                    # The alert is older than the freshness limit: never sent late, never replayed.
                    self.engine.discard_entry('stale_entry_not_replayed')
                    continue
                if self.bridge is not None and not self._bridge_entry_ready(now):
                    # Keep the entry durable while fresh bridge state arrives. The original
                    # decision deadline still forbids a late replay.
                    if adapter_route:
                        decision = self.modules['core'].signal_time(self.engine.data['trade'])
                    else:
                        decision = self.engine.data['trade']['payload']['execution_context']['decision_time']
                    if now - decision > ENTRY_DECISION_MAX_AGE_MS:
                        self.engine.discard_entry('stale_entry_not_replayed')
                        continue
                    return
            elif not self._route_ready():
                return
            if self.bridge is not None:
                self._dispatch_bridge(command, now)
                continue
            payload = self.modules['transport'].message(command, self.binding['symbol'], self.engine.data['trade']['side'])
            if native_contract.native(self.binding):
                envelope = native_contract.envelope(self.binding, command, self.engine.data['trade']['side'], payload)
            else:
                envelope = {'instanceId': self.binding['instanceId'], 'accountId': self.binding['accountId'],
                            'routeId': self.binding['routeId'], 'endpointSha256': self.binding['endpointSha256'],
                            'commandId': command['id'], 'tradeId': command['trade_id'], 'payload': payload}
            # Credential acquisition is known-unsent. Hold a real private lease
            # from BEFORE durable start through bounded send: no unlock race.
            stack = ExitStack()
            try:
                if hasattr(self.transport, 'prepare'):
                    send = stack.enter_context(self.transport.prepare(envelope))
                else:
                    send = lambda: self.transport.send(envelope)
            except Exception:
                stack.close()
                self.enabled, self.locked = False, True
                if command['kind'] == 'entry':
                    self.engine.discard_entry('route_unavailable_before_attempt')
                self._audit('route_unavailable', 'known_unsent; entries held')
                return
            command = self.engine.start(command['id'], now)
            if command is None:
                stack.close()
                return
            try:
                outcome, receipt = send()
                if outcome not in ('accepted', 'rejected', 'unknown') or type(receipt) is not dict:
                    raise ValueError('invalid adapter result')
                # Persist only known non-secret fields. An adapter's message body
                # must not put a secret endpoint or credential into the ledger.
                clean = {'transport_outcome': outcome}
                if native_contract.native(self.binding):
                    clean.update(native_contract.safe_native_receipt(receipt, outcome))
                for key in (('id', 'broker_order_id', 'messageCode') if native_contract.native(self.binding) else ('id', 'logId', 'messageCode')):
                    value = receipt.get(key)
                    if type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,240}', value):
                        clean[key] = value
                if type(receipt.get('http_status')) is int:
                    clean['http_status'] = receipt['http_status']
            except Exception:
                outcome, clean = 'unknown', {'transport_outcome': 'unknown', 'reason': 'adapter_outcome_unknown'}
            finally:
                stack.close()
            self.engine.transport_result(command['id'], outcome, clean, self.clock())

    def _dispatch_bridge(self, command, now):
        """Soup's exact payload to the bridge's /order route (the Soup engine's bridge transport, --transport bridge)."""
        if self._adapter_route():
            return self._dispatch_adapter(command, now)
        # Soup's policy marker (1 = fixed one contract, 10 = original allocation), never a size cap.
        limit = (self.engine.quantity_policy or {}).get('max_contracts')
        payload = self.modules['transport'].message(command, self.binding['symbol'], self.engine.data['trade']['side'],
                                                    quantity_limit=limit)
        command = self.engine.start(command['id'], now)
        if command is None:
            return
        try:
            # The same second boundary check as the guarded bridge transport.
            self.modules['transport'].validate_quantity_message(payload, limit)
            outcome, receipt = self.transport.send(payload)
            if outcome not in ('accepted', 'rejected', 'unknown') or type(receipt) is not dict:
                raise ValueError('invalid adapter result')
            receipt = json.loads(canonical(receipt))
            if len(canonical(receipt)) > 8192:
                receipt = {'reason': 'receipt_too_large'}
        except Exception:
            outcome, receipt = 'unknown', {'transport': 'adapter_exception'}
        self.engine.transport_result(command['id'], outcome, dict(receipt, transport_outcome=outcome), self.clock())

    def _dispatch_adapter(self, command, now):
        """beta4: a registered adapter's command to the bridge's /order route.

        The adapter module builds the command (bridge_message); it is checked against the registry's
        exact format, this binding's contract and adapter, the command id and trade id, and the
        freshness limit before it is sent. A command that fails the check is never sent and is recorded
        as refused (known unsent), so the adapter's own lifecycle holds for a human."""
        trade = self.engine.data['trade']
        symbol, pair = self.binding['symbol'], self.binding['pairId']
        try:
            payload = self.modules['core'].bridge_message(copy.deepcopy(command), symbol, trade['side'],
                                                          copy.deepcopy(trade))
            payload = json.loads(canonical(payload))
            spec = bridge_adapters.lookup(pair)
            bridge_adapters.validate_command(payload, ticker=symbol, enabled={pair: spec}, now_ms=self.clock())
            extras = payload['extras']
            if extras['adapter_id'] != pair or extras['command_id'] != command['id'] \
                    or extras['trade_id'].rsplit(':', 1)[-1] != trade['side']:
                raise ValueError('adapter command does not match the engine command')
            if len(canonical(payload).encode()) > 4096:
                raise ValueError('adapter command too large')
            problem = None
        except (ValueError, TypeError, KeyError) as exc:
            problem = 'adapter_command_refused: ' + str(exc)[:200]
        command = self.engine.start(command['id'], now)
        if command is None:
            return
        if problem:
            self._audit('adapter_command_refused', problem)
            self.engine.transport_result(command['id'], 'rejected',
                                         {'bridge': 'refused', 'reason': problem, 'transport_outcome': 'rejected'},
                                         self.clock())
            return
        try:
            outcome, receipt = self.transport.send(payload)
            if outcome not in ('accepted', 'rejected', 'unknown') or type(receipt) is not dict:
                raise ValueError('invalid adapter result')
            receipt = json.loads(canonical(receipt))
            if len(canonical(receipt)) > 8192:
                receipt = {'reason': 'receipt_too_large'}
        except Exception:
            outcome, receipt = 'unknown', {'transport': 'adapter_exception'}
        self.engine.transport_result(command['id'], outcome, dict(receipt, transport_outcome=outcome), self.clock())

    def status(self):
        with self.mutex:
            now = self.clock()
            trade = self.engine.data['trade']
            if self.bridge is not None:
                fresh = bool(not self.closed and self.bridge.route.fresh())
                broker_at = (trade or {}).get('broker_time')
                remaining = trade['remaining'] if trade and type(broker_at) is int and 0 <= now - broker_at <= 15000 else None
            else:
                fresh = bool(not self.closed and self.proof and 0 <= now - self.proof['observedAt'] <= 12000)
                remaining = trade['remaining'] if fresh and trade and trade.get('broker_time') == self.proof['observedAt'] else None
            return {'instanceId': self.binding['instanceId'], 'pairId': self.binding['pairId'],
                    'packageInstalled': True, 'mode': ('customer_native_' + self.binding['accountMode']) if native_contract.native(self.binding) else 'customer_demo_only',
                    'runtimeState': 'closed' if self.closed else 'failed' if self.fault else 'running' if self.worker and self.worker.is_alive() else 'open',
                    'entriesHeld': not self.enabled or self.locked, 'entryReady': not self.closed and not self.fault and self.enabled and not self.locked and self._entry_ready(now),
                    'tradePhase': trade['phase'] if trade else 'idle',
                    'confirmedRemaining': remaining,
                    'nativeFresh': fresh,
                    'tp1State': ('waiting_for_entry_fill' if ((trade or {}).get('native') or {}).get('tp1_retry',{}).get('awaiting') else 'resent_after_entry_fill' if ((trade or {}).get('native') or {}).get('tp1_retry',{}).get('resent') else 'filled' if trade and trade.get('tp1_done') else 'not_filled'),
                    'nativeAlerts': [{'code':a['code'],'at':a.get('at')} for a in (((trade or {}).get('native') or {}).get('alerts') or [])[-20:] if type(a) is dict and type(a.get('code')) is str],
                    'publicationVerified': False, 'providerCommissioned': False,
                    'pendingCommands': sum(c['status'] in ('ready', 'started', 'unknown', 'accepted') for c in self.engine.data['commands'].values()),
                    'sizeLockEnabled': self.size_lock_enabled,
                    'route': 'tradovate_bridge' if self.bridge is not None else None,
                    'bridgeError': self.bridge_error,
                    'fault': self.fault}

    def adapter_snapshot(self):
        """Internal broker-adapter input. Contains order IDs/receipts, no secrets."""
        with self.mutex:
            self._check()
            return {'binding': copy.deepcopy(self.binding), 'openedAt': self.opened_at,
                    'brokerSequence': int(self.engine.db.execute("SELECT value FROM customer_runtime_metadata WHERE key='broker_sequence'").fetchone()[0]),
                    'engine': copy.deepcopy(self.engine.data)}

    def start(self):
        with self.mutex:
            self._check()
            if self.worker is not None:
                raise RuntimeError('runtime worker already started')
            def work():
                try:
                    while not self.stopping.is_set():
                        if self.bridge is not None:
                            self._bridge_tick()
                        if not self.step():
                            active = self.bridge is not None and self.engine.data['trade'] and self.engine.data['trade']['phase'] != 'closed'
                            self.wake.wait(.1 if active else .25)
                            self.wake.clear()
                except BaseException:
                    self.fault = 'worker_failed_reconcile_before_restart'
                    self.enabled, self.locked = False, True
            self.worker = threading.Thread(target=work, name='customer-engine-' + self.binding['instanceId'], daemon=True)
            self.worker.start()
        return self.status()

    def _bridge_tick(self):
        try:
            self.bridge.tick(self)
            self.bridge_error = self.bridge.events.status.get('error')
        except Exception:  # noqa: BLE001 - the route fails closed; the status shows it
            self.bridge_error = 'bridge_tick_failed'

    def close(self):
        self.stopping.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=25)
            if self.worker.is_alive():
                raise RuntimeError('runtime worker did not stop; do not open a second writer')
        with self.mutex:
            if self.closed:
                return
            self.enabled, self.locked = False, True
            self._closing = True
            self.writer.validate()
            if not self._engine_connection_closed:
                cursor = self.engine.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                checkpoint = cursor.fetchone(); cursor.close()
                if not checkpoint or checkpoint[0] != 0:
                    raise RuntimeError('runtime SQLite readers remain; close them before retirement')
                self.engine.close()
                self._engine_connection_closed = True
            from .retirement import settle_owned_sqlite
            if self.bridge is not None:
                self.bridge.close()
            try:
                settle_owned_sqlite(self.directory / 'engine.sqlite')
            except RuntimeError:
                # The bridge's firing order may still hold a read-only connection; SQLite keeps
                # its journal files until that reader closes. Retirement checks again.
                if self.bridge is None:
                    raise
            market_path = self.directory / 'market.sqlite'
            if market_path.exists():
                settle_owned_sqlite(market_path)
            self.writer.close()
            self.closed = True
