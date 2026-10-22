"""The Tradovate bridge as the Soup engine's order route: the Soup engine's bridge transport
(`--transport bridge`).

- Orders: Soup's exact message() payload goes to POST <bridge>/order/<TVBRIDGE_TOKEN>
  (bridge_transport.py is the bridge module's soup_transport.py, copied unchanged).
- Account check, before every send: the bridge's GET /state/<token> must report this binding's
  Tradovate account (id and SHA-256 of the name), Soup account, contract and publish mode
  `adapter-db`. A mismatch refuses the send; nothing is sent.
- First entry: a fresh bridge state must show the account synced, healthy, flat, with nothing
  working, unseen or unresolved, entries not paused at the bridge and no entry yet for the trade.
- Broker evidence: the bridge appends broker events to its adapter database. They are read
  read-only and queued for the engine (channel `broker`), like Soup's AdapterReader.
- Files the bridge reads, in the Soup folder (paths.soup_root()): broker-context.json (the bridge
  continues Soup's broker sequence from it), pairing.json, quantity-policy.json and the link
  `current` to the engine folder (the firing order reads the engine database read-only).

These are safety checks only: account identity, freshness and broker state. The strategy's
payload is never changed.

beta4 engines platform: the route also carries a registered strategy adapter's orders
(bridge_adapters; the pair id is the adapter id). The bridge config must enable exactly that
adapter, /state must name it, and only Soup gets the Soup files the firing order reads (`current`,
quantity-policy.json). Follower firing stays Soup-only.

No socket, thread or token read happens on import.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

from . import bridge_adapters as adapters
from . import bridge_feed
from . import customer_contract as contract
from . import paths
from .bridge_transport import TradovateBridgeTransport

STATUS_SCHEMA = 'decrypt.soup-v3.bridge-route.v1'
CONTEXT_SCHEMA = 'decrypt.soup-v3.broker-context.v1'
STATE_MAX_AGE_MS = 3000         # entry gate: bridge observation age limit
SEND_STATE_MAX_AGE_MS = 10000   # send guard: older evidence is refreshed first
ENTRY_DECISION_MAX_AGE_MS = 120000  # an entry still waiting after this is discarded, never replayed late
NATIVE_BLOCK = 'ENTRIES-BLOCKED-NATIVE-EXIT'


class BindingError(ValueError):
    pass


def milliseconds():
    return time.time_ns() // 1_000_000


def tradovate_symbol(ticker):
    """MNQZ2026 -> MNQZ6 (same rule as tvbridge.protocol.tradovate_symbol)."""
    m = re.fullmatch(r'([A-Z0-9]{1,5}?)([FGHJKMNQUVXZ])(20\d\d)', ticker or '')
    if not m:
        raise BindingError('unsupported ticker')
    return m.group(1) + m.group(2) + m.group(3)[-1]


def read_token(path, name='TVBRIDGE_TOKEN'):
    """Token from the bridge's private env file (KEY=VALUE lines, 0600, owned by this user)."""
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise BindingError('bridge_not_configured') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 8192):
            raise BindingError('bridge_env_not_private')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            text = handle.read(8193).decode()
    finally:
        os.close(fd)
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key.strip()] = value
    token = values.get(name, '')
    if len(token) < 24 or any(c.isspace() or c in '/?#%' for c in token):
        raise BindingError('bridge_token_invalid')
    return token


def bridge_settings(root=None):
    """Login A's bridge settings from its config.json (never the token)."""
    root = Path(root) if root else paths.tvbridge_root()
    try:
        cfg = json.loads((root / 'config.json').read_text())
        port = cfg.get('listen_port', 8777)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        return {'root': root, 'bridge_url': 'http://127.0.0.1:%d' % port, 'env_path': root / 'tvbridge.env',
                'soup_account_id': cfg['soup_account_id'], 'account_name': cfg['account_name'],
                'ticker': cfg['ticker'], 'publish_mode': cfg.get('publish_mode'),
                'adapter_db': Path(cfg['soup_adapter_db']).expanduser(),
                'context_path': Path(cfg['soup_context_path']).expanduser(),
                'kill_switch_file': cfg.get('kill_switch_file'),
                'adapter_ids': sorted(adapters.binding_adapters(cfg))}
    except adapters.AdapterError:
        raise BindingError('bridge_adapters_invalid') from None
    except (OSError, ValueError, KeyError, TypeError):
        raise BindingError('bridge_not_configured') from None


def expected_account(binding):
    """What the bridge must report for this engine binding."""
    return {'tradovate_account_id': binding['nativeAccountId'], 'account_name': binding['nativeAccountName'],
            'soup_account_id': binding['accountId'], 'contract': tradovate_symbol(binding['symbol']),
            'adapter_id': binding['pairId']}


def settings_problem(binding, settings):
    """The bridge config must name this binding's account, contract and adapter-db publishing."""
    if settings['soup_account_id'] != binding['accountId']:
        return 'bridge_soup_account_mismatch'
    if settings['account_name'] != binding['nativeAccountName']:
        return 'bridge_account_name_mismatch'
    if settings['ticker'] != binding['symbol']:
        return 'bridge_contract_mismatch'
    if settings['publish_mode'] != 'adapter-db':
        return 'bridge_publish_mode_mismatch'
    if binding['pairId'] not in settings.get('adapter_ids', ()):
        return 'bridge_adapter_not_enabled'
    return None


def binding_problem(expect, state):
    """None when the bridge's reported pins are exactly this binding, else a reason."""
    if not isinstance(state, dict):
        return 'bridge_state_unavailable'
    contract_info = state.get('contract') if isinstance(state.get('contract'), dict) else {}
    checks = (
        ('bridge_account_id', type(state.get('account_id')) is int
         and state.get('account_id') == expect['tradovate_account_id']),
        ('bridge_account_name', state.get('account_name_sha256')
         == hashlib.sha256(expect['account_name'].encode()).hexdigest()),
        ('bridge_soup_account', state.get('soup_account_id') == expect['soup_account_id']),
        ('bridge_contract', contract_info.get('name') == expect['contract']),
        ('bridge_publish_mode', state.get('publish_mode') == 'adapter-db'),
    )
    for reason, ok in checks:
        if not ok:
            return reason + '_mismatch'
    adapter = expect.get('adapter_id', contract.SOUP)
    if 'adapter_ids' in state or adapter != contract.SOUP:
        # beta4: the bridge names the adapter it accepts; a non-Soup route needs it named.
        ids = state.get('adapter_ids')
        if not isinstance(ids, list) or adapter not in ids:
            return 'bridge_adapter_mismatch'
    return None


def flat(expect, state):
    """The bridge reports exactly this account, synced and healthy, flat with nothing working."""
    return bool(isinstance(state, dict) and binding_problem(expect, state) is None
                and state.get('synced') is True and state.get('healthy') is True
                and type(state.get('net_position')) is int and state['net_position'] == 0
                and state.get('open_order_ids') == [] and state.get('unseen_accepted_orders') == []
                and state.get('unresolved_commands') == 0)


def engine_entry_waiting(data, now, started_at, entry_kinds=('entry',)):
    """The engine side of the first-entry gate (Soup's entry_readiness.entry_ready).
    `entry_kinds`: Soup's entry command kind, or (adapters) both bridge entry kinds."""
    integer = lambda x: type(x) is int  # noqa: E731
    if not all(integer(x) for x in (now, started_at)) or now < started_at:
        return False
    trade, commands = data['trade'], data['commands']
    if not trade or trade['phase'] != 'entering':
        return False
    entries = [c for c in commands.values() if c['kind'] in entry_kinds]
    # Native exits: a bracket entry the bridge definitely refused leaves only its record; the
    # same signal's plain market entry then waits for this gate like any first entry.
    started = [c for c in commands.values() if c['started_at'] is not None
               and not (c['kind'] == 'bracket_entry' and c['status'] == 'rejected')]
    if (len(entries) != 1 or entries[0]['status'] != 'ready' or entries[0]['started_at'] is not None
            or started):
        return False
    qt = (data['last_quote'] or {}).get('time')
    return integer(qt) and started_at <= qt <= now and now - qt <= 2000


class BridgeRoute:
    """Background bridge-state poller, send guard and first-entry gate."""

    def __init__(self, expect, token, base_url, *, fetch=None, clock=milliseconds,
                 poll_s=1.0, fast_poll_s=0.1, timeout_s=4.0):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path not in ('', '/'):
            raise BindingError('bridge_not_loopback')
        self.expect, self._token, self.base = expect, token, base_url.rstrip('/')
        self.clock = clock
        self.fetch = fetch or self._http_fetch
        self.poll_s, self.fast_poll_s, self.timeout_s = poll_s, fast_poll_s, timeout_s
        self.lock = threading.Lock()
        self.latest = None       # (fetched_at_ms, state dict, trade_id queried)
        self.last_error = None
        self.want_trade = None   # trade id whose entry outcome the entry gate needs
        self.want_at = 0         # last time the entry gate asked; fast polling stops 5 s later
        self.hurry = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.entry_kinds = ('entry',)   # open_route: both bridge entry kinds for a non-Soup adapter

    def _http_fetch(self, trade_id=None):
        url = self.base + '/state/' + self._token
        if trade_id is not None:
            url += '?' + urllib.parse.urlencode({'trade_id': trade_id})
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as response:
                raw = response.read(65537)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        try:
            data = json.loads(raw) if len(raw) <= 65536 else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def refresh(self):
        trade_id = self.want_trade
        state = self.fetch(trade_id)
        now = self.clock()
        with self.lock:
            if state is None:
                self.last_error = 'bridge_state_unavailable'
            else:
                self.latest = (now, state, trade_id)
                self.last_error = None
        return state

    def snapshot(self):
        with self.lock:
            return self.latest, self.last_error

    def problem(self, max_age_ms=SEND_STATE_MAX_AGE_MS):
        """Reason the route must refuse to send, or None. Refreshes stale evidence first."""
        latest, _ = self.snapshot()
        if latest is None or self.clock() - latest[0] > max_age_ms:
            if self.refresh() is None:
                return 'bridge_state_unavailable'
            latest, _ = self.snapshot()
        return binding_problem(self.expect, latest[1])

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='bridge-state', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        self.hurry.set()
        if self.thread:
            self.thread.join(timeout=self.timeout_s + 2)

    def _run(self):
        while not self.stopping.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the poller must survive; the gates fail closed
                with self.lock:
                    self.last_error = 'bridge_poller_error'
            if self.want_trade is not None and self.clock() - self.want_at > 5000:
                self.want_trade = None  # the entry was sent or discarded
            self.hurry.wait(self.fast_poll_s if self.want_trade is not None else self.poll_s)
            self.hurry.clear()

    def status(self):
        latest, error = self.snapshot()
        at, state, _ = latest if latest else (None, {}, None)
        reason = error or binding_problem(self.expect, state if latest else None)
        return {'schema': STATUS_SCHEMA, 'updated_at': self.clock(), 'binding_ok': reason is None,
                'reason': reason, 'fetched_at': at, 'state_observed_at': state.get('observed_at'),
                'synced': state.get('synced'), 'healthy': state.get('healthy'),
                'net_position': state.get('net_position'), 'open_order_ids': state.get('open_order_ids'),
                'unseen_accepted_orders': state.get('unseen_accepted_orders'),
                'unresolved_commands': state.get('unresolved_commands'),
                'entries_disabled': state.get('entries_disabled'), 'publish_mode': state.get('publish_mode')}

    def fresh(self, max_age_ms=15000):
        latest, error = self.snapshot()
        return bool(latest and not error and 0 <= self.clock() - latest[0] <= max_age_ms
                    and binding_problem(self.expect, latest[1]) is None)

    def entry_ready(self, data, now, started_at):
        """The bridge half of the first-entry gate. Never raises."""
        try:
            if not engine_entry_waiting(data, now, started_at, self.entry_kinds):
                return False
            trade_id = data['trade']['id']
            self.want_at = self.clock()
            if self.want_trade != trade_id:
                self.want_trade = trade_id
                self.hurry.set()
                return False
            latest, error = self.snapshot()
            if error or latest is None:
                return False
            fetched_at, s, queried = latest
            if queried != trade_id or fetched_at < started_at or not started_at <= fetched_at <= now + 1000:
                return False
            observed = s.get('observed_at')
            if type(observed) is not int or observed < started_at or abs(now - observed) > STATE_MAX_AGE_MS:
                return False
            if binding_problem(self.expect, s) is not None:
                return False
            return (s.get('synced') is True and s.get('healthy') is True
                    and s.get('net_position') == 0 and type(s.get('net_position')) is int
                    and s.get('open_order_ids') == [] and s.get('unseen_accepted_orders') == []
                    and s.get('unresolved_commands') == 0 and s.get('entries_disabled') is False
                    and 'trade_entry_outcome' in s and s['trade_entry_outcome'] is None)
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def flat_state(self):
        """A fresh bridge report that the bound account is flat with nothing working, else None.

        Used before retirement, key rotation and restore: never sends anything.
        """
        state = self.refresh()
        return state if flat(self.expect, state) else None


class BridgeEvents:
    """Read-only reader of the bridge's adapter database (Soup's AdapterReader, broker channel).

    The cursor is stored in the engine database in the same transaction as the queued event, so a
    crash never skips or double-queues an event. Old events are skipped, never replayed as fresh.
    """

    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        self.status = {'healthy': False, 'error': None, 'cursor': None, 'last_event_ms': None}

    def poll(self, runtime):
        try:
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError('adapter database missing')
            cursor = runtime.bridge_cursor()
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.05)) as db:
                low, high = db.execute('SELECT min(seq),max(seq) FROM events').fetchone()
                if cursor and (high is None or high < cursor or (low is not None and low > cursor + 1)):
                    raise ValueError('adapter sequence gap; review required')
                rows = db.execute('SELECT seq,channel,payload,observed_at FROM events WHERE seq>? ORDER BY seq LIMIT 128',
                                  (cursor,)).fetchall()
            for seq, channel, raw, observed in rows:
                if type(seq) is not int or seq <= 0 or (cursor and seq != cursor + 1):
                    raise ValueError('adapter sequence gap; review required')
                if channel != 'broker':
                    raise ValueError('adapter channel rejected')
                now = runtime.clock()
                fresh = type(observed) is int and -1000 <= now - observed <= 15000
                runtime.admit_bridge_event(seq, raw, deliver=fresh)
                cursor = seq
                self.status.update(cursor=seq, last_event_ms=observed)
            self.status.update(healthy=True, error=None)
        except (OSError, ValueError, sqlite3.Error, TypeError) as exc:
            self.status.update(healthy=False, error=str(exc) if isinstance(exc, ValueError) else 'adapter_unavailable')


def engine_status(data, now, runtime):
    """The Soup status fields the Bridge window shows (the field names the bridge module reads)."""
    trade = data.get('trade')
    commands = list((data.get('commands') or {}).values())
    receipts = [c.get('receipt') for c in commands if isinstance(c.get('receipt'), dict)]
    quote_time = (data.get('last_quote') or {}).get('time')
    native = (trade or {}).get('native') or {}
    return {'schema': 'decrypt.soup-v3.status.v1', 'updated_at': now, 'mode': 'bridge',
            'enabled': runtime.enabled and not runtime.locked, 'manual_enabled': runtime.enabled,
            'locked': runtime.locked, 'worker_ok': not runtime.fault and bool(runtime.worker and runtime.worker.is_alive()),
            'phase': trade['phase'] if trade else 'flat', 'remaining_qty': trade['remaining'] if trade else 0,
            'outbound_attempts': sum(c.get('started_at') is not None for c in commands),
            'accepted_receipts': sum(r.get('transport_outcome') == 'accepted' for r in receipts),
            'rejected_receipts': sum(r.get('transport_outcome') == 'rejected' for r in receipts),
            'unknown_receipts': sum(r.get('transport_outcome') == 'unknown' for r in receipts),
            'quote_fresh': type(quote_time) is int and 0 <= now - quote_time <= 2000,
            'native_exits': {'trade_mode': native.get('mode'), 'stop_confirmed': native.get('stop_confirmed'),
                             'failed': native.get('failed'), 'alerts': native.get('alerts', [])} if native else None,
            'market_session': None}


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    fd, name = tempfile.mkstemp(prefix='.soup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SoupFiles:
    """The Soup folder the bridge module reads: the file names the bridge module expects."""

    def __init__(self, root=None, context_path=None):
        self.root = Path(root) if root else paths.soup_root()
        self.context_path = Path(context_path) if context_path else self.root / 'broker-context.json'
        self._last_context = None
        self._last_files = {}

    def link(self, engine_directory, config, quantity_policy, soup=True):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_json(self.root / 'pairing.json', config)
        if not soup:
            # A non-Soup adapter: the bridge only needs pairing and broker-context.json. No `current`
            # link, so the Soup firing order never reads another engine's database.
            return
        if quantity_policy is not None:
            _atomic_json(self.root / 'quantity-policy.json', quantity_policy)
        current = self.root / 'current'
        target = str(Path(engine_directory).absolute())
        if os.path.islink(current) and os.readlink(current) == target:
            return
        if os.path.lexists(current) and not os.path.islink(current):
            raise BindingError('soup_current_not_a_link')
        temporary = self.root / ('.current-' + os.urandom(4).hex())
        os.symlink(target, temporary)
        os.replace(temporary, current)

    def context(self, config, data, now):
        body = json.dumps(data, sort_keys=True, separators=(',', ':'))
        if body == self._last_context:
            return
        _atomic_json(self.context_path, {'schema': CONTEXT_SCHEMA, 'updated_at': now, 'pairing': config, 'engine': data})
        self._last_context = body

    def status(self, status, control):
        """status.json (read by the Bridge window) and control.json (shown in Bridge Advanced).

        control.json mirrors the engine's own entry controls; the engine does not read it back.
        """
        for name, value in (('status.json', status), ('control.json', control)):
            body = json.dumps(value, sort_keys=True, separators=(',', ':'))
            if self._last_files.get(name) != body:
                _atomic_json(self.root / name, value)
                self._last_files[name] = body

    def block_path(self):
        return self.root / NATIVE_BLOCK

    def blocked(self):
        return os.path.lexists(self.block_path())


class BridgeTransport:
    """Soup's order route through the bridge: account check, then the unchanged payload to /order."""
    sends_payload = True

    def __init__(self, binding, route, native, events, files, *, kill_switch_file=None, feed=None):
        self.binding = contract.validate_binding(binding)
        self.feed = feed   # beta4: bridge_feed.BridgeSecondFeed for a strategy that requires the 1-second feed
        # Identity the runtime checks before any dispatch (same names as every adapter).
        self.account_id, self.route_id = binding['accountId'], binding['routeId']
        self.route_sha256, self.binding_sha256 = binding['routeSha256'], contract.digest(self.binding)
        self.route, self.native, self.events, self.files = route, native, events, files
        self.kill_switch_file = kill_switch_file
        self._blocked_trades = set()

    def problem(self):
        return self.route.problem()

    def send(self, payload):
        problem = self.route.problem()
        if problem:
            return 'rejected', {'bridge': 'binding_refused', 'reason': problem}
        return self.native.send(payload)

    def entry_blocked(self):
        return self.files.blocked()

    def link(self, runtime):
        soup = self.binding['pairId'] == contract.SOUP
        self.files.link(runtime.directory, runtime.engine.config, getattr(runtime.engine, 'quantity_policy', None),
                        soup=soup)

    def tick(self, runtime):
        """Called by the runtime worker: queue new broker events, publish the broker context, guard exits."""
        self.events.poll(runtime)
        with runtime.mutex:
            config, data = dict(runtime.engine.config), json.loads(json.dumps(runtime.engine.data))
        now = runtime.clock()
        self.files.context(config, data, now)
        status = engine_status(data, now, runtime)
        if self.feed is not None:
            status['price_feed'] = self.feed.status(now)
        self.files.status(status, {'enabled': runtime.enabled, 'locked': runtime.locked})
        self._native_guard(runtime, data)
        if self.feed is not None:
            # After the exit guard and the status write: a feed error can never skip them. It only means no
            # new price this tick (the engine's own freshness check then fails closed).
            try:
                self.feed.tick(runtime)
            except Exception:  # noqa: BLE001
                self.feed.counts['tick_errors'] = self.feed.counts.get('tick_errors', 0) + 1

    def _native_guard(self, runtime, data):
        """Exit-all could not be verified: hold entries here and pause the bridge, once per trade.

        Released by hand after Tradovate shows flat with nothing working: delete the block file
        in the Soup folder and press Resume in Bridge Advanced.
        """
        trade = data.get('trade')
        failed = trade and (trade.get('native') or {}).get('failed')
        if not failed or trade['phase'] == 'closed' or (trade['id'], failed.get('reason')) in self._blocked_trades:
            return
        runtime.hold()
        _atomic_json(self.files.block_path(), {'trade_id': trade['id'], 'reason': failed.get('reason'),
                                               'at': runtime.clock(),
                                               'release': 'verify flat with nothing working in Tradovate, then delete'})
        if self.kill_switch_file:
            fd = os.open(self.kill_switch_file, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        self._blocked_trades.add((trade['id'], failed.get('reason')))

    def status(self):
        value = dict(self.route.status(), events=dict(self.events.status), entriesBlocked=self.entry_blocked())
        if self.feed is not None:
            value['priceFeed'] = self.feed.status()
        return value

    def close(self):
        self.route.stop()
        if self.feed is not None:
            self.feed.stop()


def open_route(binding, *, root=None, soup_root=None, fetch=None, clock=milliseconds, start=True,
               feed_fetch=None, check_feed=None):
    """Build Soup's bridge route for one native Soup binding. Raises BindingError(reason).

    beta4: a strategy that requires the 1-second feed (bridge_feed.REQUIRED_BY, Lumi) also gets the feed
    reader. When the route starts (or `check_feed` is true) the feed must be live and fresh for the
    binding's contract right now; otherwise the strategy does not start (BindingError, plain message)."""
    binding = contract.validate_binding(binding)
    try:
        adapter = adapters.lookup(binding['pairId'])
    except adapters.AdapterError:
        raise BindingError('bridge_route_adapter_not_registered') from None
    settings = bridge_settings(root)
    problem = settings_problem(binding, settings)
    if problem:
        raise BindingError(problem)
    token = read_token(settings['env_path'])
    route = BridgeRoute(expected_account(binding), token, settings['bridge_url'], fetch=fetch, clock=clock)
    if not adapter.reference:
        route.entry_kinds = adapters.ENTRY_KINDS
    native = TradovateBridgeTransport(route.base, token)
    files = SoupFiles(soup_root, settings['context_path'])
    feed = None
    if binding['pairId'] in bridge_feed.REQUIRED_BY:
        label = bridge_feed.REQUIRED_BY[binding['pairId']]
        feed = bridge_feed.BridgeSecondFeed(binding['symbol'], token, settings['bridge_url'], label=label,
                                            fetch=feed_fetch, clock=clock)
        if (start if check_feed is None else check_feed):
            problem = feed.check_start()
            if problem is not None:
                raise BindingError(bridge_feed.refusal(label, problem))
    transport = BridgeTransport(binding, route, native, BridgeEvents(settings['adapter_db']), files,
                                kill_switch_file=settings['kill_switch_file'], feed=feed)
    if start:
        route.start()
        if feed is not None:
            feed.start()
    return transport
