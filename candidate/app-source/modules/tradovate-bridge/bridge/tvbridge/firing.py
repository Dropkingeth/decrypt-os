"""Firing order: every Soup order fires on the leader (main) account, then on the followers in parallel.

Run:  python3 -m tvbridge.firing --config firing.json
Env:  TVBRIDGE_TOKEN (the bridge daemon's secret, >= 24 chars)

Soup (one brain) trades the leader account through the bridge exactly as before: its
payload goes to POST /order and it confirms against the leader's adapter-db evidence only.
This service never touches that route. It generalizes tvbridge.mirror: one lane per
enabled follower, each a Mirror that reads Soup's database READ-ONLY, rebuilds Soup's
exact payload with Soup's own message() and sends it to its follower account through the
daemon's /follower/<token>/<key>/order route (tvbridge.followers).

- Firing order: `firing_order[0]` is the leader (Soup's own route). Followers fire in
  parallel: every lane has its own thread, journal and reconciliation.
- Entry gate (`entry_gate: "main_accepted"`, default): a follower sends an entry only after
  Soup has recorded the leader's send as accepted. Otherwise the entry is skipped on every
  follower (`main_<outcome>`). Exits, cancels and TP1 are not delayed.
- Leader alignment: the leader is never blocked by a follower (the daemon
  exempts follower accounts from the leader's hedge guard). Before a follower takes a new
  entry, its lane reads the follower account; if anything is working or held on the pinned
  contract (an older trade, a divergence, or a position opened by hand) it cancels the
  working orders (`align-cancel:<trade>`), closes the exact position at market
  (`align-flatten:<trade>`, never a flip) and waits up to `align_wait_ms` for flat. A
  follower that is not flat by then, or whose kill switch is set, skips the entry; the
  fault is recorded and alerted, and the leader trades on.
- Per lane, unchanged from the mirror: fsynced intent before every send, a command id is
  never sent twice, an unknown is never retried, baseline/stale skip rules, and
  end-of-trade reconciliation (cancel working orders, then close the exact remainder).
- Followers the daemon reports as not pinned (name/id not in the page's own account sync)
  are refused at start: no lane, alert `follower_unavailable`, grid state MISMATCH.
- Sizes: the payload quantity as-is (`size_policy: "payload"`); there is no size cap (the strategy
  alert sets the size). Nothing is clipped or resized.
- Prop rules: `unrestricted` (the default when an account names none) or `cross-account-window`
  `apex` is refused.
- Grid alerts (tvbridge.grid): one monospace block per trade event in status.json `grid`.
- 1.0.8 native followers (`follower_exits: "native"`, default "mirror" = the 1.0.7 behaviour
  above): for a trade Soup pinned as native, each follower runs the leader's native exit flow on
  its own account (tvbridge.follower_native): its own bracket entry and TP1, break-even on ITS
  OWN TP1 fill, the leader's runner decisions with stop-shrink-before-reduce, its own exit-all
  with verification, fallback, kill switch and alerts. Soup's stop commands are never copied.
- 1.0.9: every leader terminal reason closes the followers, including Soup's
  `external_flat_unreconciled` and `native_broker_view_stale`. The leader closed outside the bridge
  (by hand or by the prop firm) closes the followers at once: the supervisor's LeaderFlatWatch reads the
  bridge's own view of the leader from the /followers report it already polls for the grid, and a
  confirmed fact goes to every lane (reason `leader_closed_externally`, alert once per follower).
  A follower entry refused or rejected with zero fill is skipped and logged as the informational
  alert `follower_entry_skipped` (level "info"), never a fault.
- 1.1.0 multi-login, only with a top-level `logins_file` or an
  `accounts.<key>.login`; without both, nothing below runs and behaviour is 1.0.9's. Each lane talks
  to its own login's daemon (bridge_url, and the order token from that login's `token_env`; the
  primary keeps the top-level bridge_url and TVBRIDGE_TOKEN). One /followers report per login
  daemon, merged (LoginReports) for pins, grid, LeaderFlatWatch (the primary's `main` row) and
  peers. A background poller keeps each login's GET /login report (read token from the registry's
  `read_token_env`, else the order token; every LOGIN_POLL_S, timeout LOGIN_CHECK_TIMEOUT_S); lane
  decisions read only that cache, and a report older than LOGIN_STALE_S counts as unreachable.
  A lane on a non-primary login whose daemon definitely reports the page logged out skips new
  entries (`login_down:<id>`); an unreachable login does not skip (the order itself fails safely),
  warned at most once a minute; primary-login lanes never check (main_accepted proves the login).
  Exits and native stops already at Tradovate are untouched, other logins go on, and entries
  resume on the next signal. `cross_login_hedge_guard` (default true): within align_wait_ms the
  lane skips the entry while a counted account on any OTHER login holds or works the opposite side
  in the ticker's hedge group (`hedge_conflict_login:<id>:<root>`). `peer_hedge_scope`
  "configured" (default) counts only accounts with role main or follower on their login, "all"
  every visible account. Rows with an unknown contract count as hedge-group members (warned).
  An unreachable, stale or logged-out login, a login without a token, or any error reading a report
  fails CLOSED: its exposure is unknown, so the follower entry is skipped
  (`hedge_unknown_login:<id>:<why>`), warned at most once a minute. Exits are never affected.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import protocol
from .follower_native import LeaderFlatWatch, NativeFollower, native_plan, validate_native
from .grid import GridTracker
from .journal import _append
from .mirror import (DEFAULTS as MIRROR_DEFAULTS, SKIP_TYPES, BridgeState, Mirror, SoupNotOpen, SoupReader,
                     _atomic_write, load_soup)
from .executor import DEFAULT_HEDGE_GROUPS, HEDGE_OFF_ACK, _hedge_roots
from .soup_transport import TradovateBridgeTransport

log = logging.getLogger('tvbridge.firing')

ENTRY_GATES = ('main_accepted', 'none')
FIRING_MODES = ('parallel',)
PROP_PROFILES = {'cross-account-window': {'max_grace_plus_settle_ms': 30000},
                 'unrestricted': {'max_grace_plus_settle_ms': None}}
REFUSED_PROFILES = {'apex': 'Apex accounts are never copied (Apex bans copy trading)'}
SIZE_POLICIES = ('payload',)
LANE_KEYS = frozenset(MIRROR_DEFAULTS) - {'bridge_url'}
TOP_KEYS = frozenset({'soup_db', 'soup_engine_dir', 'soup_quantity_policy', 'soup_pairing', 'ticker',
                      'bridge_url', 'firing_dir', 'firing_mode', 'firing_order', 'entry_gate',
                      'main_gate_timeout_ms', 'align_wait_ms', 'grid_settle_ms', 'grid_attention_ms',
                      'accounts', 'follower_exits', 'follower_native',
                      'logins_file', 'cross_login_hedge_guard', 'hedge_guard_off_acknowledged', 'hedge_groups',
                      'peer_hedge_scope'}) | LANE_KEYS
ACCOUNT_KEYS = frozenset({'role', 'label', 'enabled', 'prop_profile', 'size_policy', 'login'})
MAIN_STATUSES = ('accepted', 'rejected', 'unknown')
MAX_ALIGN_ATTEMPTS = 2  # per step, and only after a definite `rejected` (nothing was sent)
LOCK_NAME = '.firing.lock'
STATUS_NAME = 'status.json'
ALERTS_NAME = 'alerts.jsonl'
LOGIN_ID_RE = re.compile(r'^[a-z][a-z0-9-]{0,15}\Z')
LOGIN_CHECK_TIMEOUT_S = 1.0   # each background GET /login read (login state, cross-login hedge)
LOGIN_POLL_S = 1.0            # background GET /login interval; lanes read the cache only
LOGIN_STALE_S = 10.0          # a cached /login report older than this counts as unreachable
LOGIN_WARN_EVERY_S = 60.0     # an unreachable login is warned about at most once a minute
LOGIN_SCHEMA = 'decrypt.tvbridge-login.v1'
LOGIN_HEDGE_REASONS = ('hedge_conflict_login:', 'hedge_unknown_login:')   # skip reasons used as they are


def _now_ms():
    return time.time_ns() // 1_000_000


class FiringConfigError(ValueError):
    pass


def validate(cfg):
    """Check a firing config. Returns (main key, [enabled follower keys in firing order]). Sends nothing."""
    if not isinstance(cfg, dict):
        raise FiringConfigError('config must be a JSON object')
    extra = set(cfg) - TOP_KEYS
    if extra:
        raise FiringConfigError('unknown config keys: ' + ', '.join(sorted(extra)))
    for key in ('soup_db', 'soup_engine_dir', 'ticker', 'firing_dir', 'firing_order', 'accounts'):
        if not cfg.get(key):
            raise FiringConfigError(f'config {key} required')
    if cfg.get('firing_mode', 'parallel') not in FIRING_MODES:
        raise FiringConfigError('firing_mode must be "parallel" (main first, then followers in parallel)')
    if cfg.get('entry_gate', 'main_accepted') not in ENTRY_GATES:
        raise FiringConfigError('entry_gate must be one of ' + ', '.join(ENTRY_GATES))
    order, accounts = cfg['firing_order'], cfg['accounts']
    if not isinstance(order, list) or len(set(order)) != len(order) or not all(isinstance(k, str) for k in order):
        raise FiringConfigError('firing_order must be a list of distinct account keys')
    if not isinstance(accounts, dict) or set(order) != set(accounts):
        raise FiringConfigError('firing_order and accounts must name the same account keys')
    followers, profiles = [], {}
    for i, key in enumerate(order):
        a = accounts[key]
        if not isinstance(a, dict) or set(a) - ACCOUNT_KEYS:
            raise FiringConfigError(f'account {key}: unknown or invalid fields')
        profile = a.get('prop_profile', 'unrestricted')
        if isinstance(profile, str) and profile.lower() in REFUSED_PROFILES:
            raise FiringConfigError(f'account {key}: {REFUSED_PROFILES[profile.lower()]}')
        if profile not in PROP_PROFILES:
            raise FiringConfigError(f'account {key}: prop_profile must be one of ' + ', '.join(PROP_PROFILES))
        profiles[key] = profile
        role = a.get('role')
        if (i == 0) != (role == 'main') or role not in ('main', 'follower'):
            raise FiringConfigError('firing_order[0] must be the one account with role "main"; the rest are followers')
        if a.get('size_policy', 'payload') not in SIZE_POLICIES:
            raise FiringConfigError(f'account {key}: size_policy must be "payload" (quantity as-is, never resized)')
        if not isinstance(a.get('enabled', True), bool):
            raise FiringConfigError(f'account {key}: enabled must be true or false')
        if 'login' in a and not (isinstance(a['login'], str) and LOGIN_ID_RE.fullmatch(a['login'])):
            raise FiringConfigError(f'account {key}: login must be a login id (^[a-z][a-z0-9-]{{0,15}}$)')
        if role == 'follower' and a.get('enabled', True):
            followers.append(key)
    lane = dict(MIRROR_DEFAULTS, **{k: v for k, v in cfg.items() if k in LANE_KEYS})
    for key in sorted(LANE_KEYS - {'reconcile_mode'}):
        if type(lane[key]) not in (int, float) or lane[key] <= 0:
            raise FiringConfigError(f'{key} must be a positive number')
    if lane['reconcile_mode'] not in ('two_step', 'liquidate'):
        raise FiringConfigError('reconcile_mode must be "two_step" or "liquidate"')
    for key in followers:
        limit = PROP_PROFILES[profiles[key]]['max_grace_plus_settle_ms']
        if limit is not None and lane['reconcile_grace_ms'] + lane['reconcile_settle_ms'] > limit:
            raise FiringConfigError(f'reconcile_grace_ms + reconcile_settle_ms must be <= {limit} ms '
                                    f'(cross-account-window profile of {key})')
    for key, default in (('main_gate_timeout_ms', 5000), ('align_wait_ms', 4000), ('grid_settle_ms', 2000),
                         ('grid_attention_ms', 30000)):
        if type(cfg.get(key, default)) is not int or cfg.get(key, default) <= 0:
            raise FiringConfigError(f'{key} must be a positive integer (ms)')
    if cfg.get('main_gate_timeout_ms', 5000) + cfg.get('align_wait_ms', 4000) > lane['max_send_delay_ms'] - 1000:
        raise FiringConfigError('main_gate_timeout_ms + align_wait_ms must be at least 1000 ms below '
                                'max_send_delay_ms')
    try:
        validate_native(cfg)
    except ValueError as exc:
        raise FiringConfigError(str(exc))
    if 'cross_login_hedge_guard' in cfg and not isinstance(cfg['cross_login_hedge_guard'], bool):
        raise FiringConfigError('cross_login_hedge_guard must be true or false')
    if cfg.get('cross_login_hedge_guard') is False and cfg.get(HEDGE_OFF_ACK) is not True:
        raise FiringConfigError(f'cross_login_hedge_guard false also needs {HEDGE_OFF_ACK}: true')
    if cfg.get('peer_hedge_scope', 'configured') not in ('configured', 'all'):
        raise FiringConfigError('peer_hedge_scope must be "configured" or "all"')
    if 'hedge_groups' in cfg:
        try:
            _hedge_roots(cfg['hedge_groups'], 'MNQ')
        except ValueError as exc:
            raise FiringConfigError(str(exc))
    if 'logins_file' in cfg and not (isinstance(cfg['logins_file'], str) and cfg['logins_file'].strip()):
        raise FiringConfigError('logins_file must be a path')
    if multi_login(cfg):
        resolve_logins(cfg)  # reads the registry; sends nothing
    return order[0], followers


# --- 1.1.0 multi-login ------------------------------------------------------------------------

def multi_login(cfg):
    """True when the firing config names a login registry or assigns any account to a login."""
    return 'logins_file' in cfg or any(isinstance(a, dict) and 'login' in a for a in cfg['accounts'].values())


def _field(login, name, default=None):
    """A registry entry's field, whether tvbridge.logins hands out objects or dicts."""
    if isinstance(login, dict):
        return login.get(name, default)
    return getattr(login, name, default)


def resolve_logins(cfg):
    """Load the registry (tvbridge.logins) and place every account on its login.

    Returns {'primary': id, 'logins': [entry, ...] (registry order, primary first), 'assign': {account
    key: login id}}. The main account must be on the primary login (Soup routes only there), and a
    top-level bridge_url must be the primary's. Raises FiringConfigError.
    """
    from . import logins as registry  # the shared logins.json loader
    path = cfg.get('logins_file')
    try:
        entries = list(registry.load_logins(str(Path(path).expanduser()) if path else None))
        first = registry.primary(entries)
    except (ValueError, OSError) as exc:
        raise FiringConfigError(f'logins_file: {exc}')
    pid = _field(first, 'id')
    entries = [first] + [e for e in entries if _field(e, 'id') != pid]
    assign = {}
    for key, a in cfg['accounts'].items():
        lid = a.get('login', pid)
        try:
            found = registry.by_id(entries, lid)
        except (KeyError, ValueError, LookupError):
            found = None
        if found is None:
            raise FiringConfigError(f'account {key}: login {lid!r} is not in the login registry')
        assign[key] = lid
    if assign[cfg['firing_order'][0]] != pid:
        raise FiringConfigError('the main account must be on the primary login (Soup routes only to it)')
    top, own = cfg.get('bridge_url'), _field(first, 'bridge_url')
    if top and own and _endpoint(top) != _endpoint(own):
        raise FiringConfigError('bridge_url must be the primary login\'s bridge_url')
    return {'primary': pid, 'logins': entries, 'assign': assign}


def _endpoint(url):
    """(scheme, host, port) of a bridge URL; localhost and 127.0.0.1 are the same host."""
    try:
        split = urllib.parse.urlsplit(str(url).strip())
        port = split.port
    except ValueError:
        return ('invalid', str(url))
    scheme = (split.scheme or '').lower()
    host = (split.hostname or '').lower()
    if host in ('localhost', '127.0.0.1', '::1'):
        host = '127.0.0.1'
    if port is None:
        port = {'http': 80, 'https': 443}.get(scheme)
    return scheme, host, port


def _loopback(base_url):
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('bridge must be on loopback')
    return base_url.rstrip('/')


class FollowerTransport(TradovateBridgeTransport):
    """POST /follower/<token>/<key>/order; same (outcome, receipt) contract as the main route."""

    def __init__(self, base_url, token, key, timeout=25):
        super().__init__(base_url, token, timeout=timeout)
        self.url = base_url.rstrip('/') + f'/follower/{token}/{key}/order'


class FollowerState(BridgeState):
    """GET /follower/<token>/<key>/state; same shape as the main /state."""

    def __init__(self, base_url, token, key, timeout=3.0):
        super().__init__(base_url, token, timeout=timeout)
        self.url = base_url.rstrip('/') + f'/follower/{token}/{key}/state'


class FollowersReport:
    """GET /followers/<token>[?trade_id=]: the leader and every follower (read-only)."""

    def __init__(self, base_url, token, timeout=3.0):
        if not isinstance(token, str) or len(token) < 24:
            raise ValueError('bridge token required')
        self.url = _loopback(base_url) + '/followers/' + token
        self.timeout = timeout

    def get(self, trade_id=None):
        url = self.url + ('?' + urllib.parse.urlencode({'trade_id': trade_id}) if trade_id else '')
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            data = json.loads(r.read(262145))
        if not isinstance(data, dict) or not isinstance(data.get('followers'), list):
            raise ValueError('invalid followers response')
        return data


class LoginView:
    """GET /login/<token>: every account visible on one login, with positions and working orders (read-only)."""

    def __init__(self, base_url, token, timeout=LOGIN_CHECK_TIMEOUT_S):
        if not isinstance(token, str) or len(token) < 24:
            raise ValueError('bridge token required')
        self.url = _loopback(base_url) + '/login/' + token
        self.timeout = timeout

    def get(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, never a proxy
        with opener.open(self.url, timeout=self.timeout) as r:
            data = json.loads(r.read(2000001))
        if not isinstance(data, dict) or not isinstance(data.get('accounts'), list):
            raise ValueError('invalid login response')
        return data


def login_state(data):
    """CONNECTED, or LOGGED OUT when the login's report has rows and every one is logged out."""
    rows = ([data['main']] if isinstance(data.get('main'), dict) else []) + \
        [r for r in data.get('followers') or [] if isinstance(r, dict)]
    return 'LOGGED OUT' if rows and not any(r.get('logged_in') is True for r in rows) else 'CONNECTED'


class LoginLink:
    """One Tradovate login as the firing service sees it: its daemon, order token, report and /login view.

    The /login view uses `read_token` (the registry's read_token_env) when given, else the order token."""

    def __init__(self, entry, token, *, primary, url=None, report=None, view=None, read_token=None):
        self.id = _field(entry, 'id')
        self.label = _field(entry, 'label') or self.id
        self.primary = primary
        self.url = (url or _field(entry, 'bridge_url') or '').rstrip('/')
        self.token_ok = isinstance(token, str) and len(token) >= 24
        self.report = report or (FollowersReport(self.url, token) if self.token_ok else None)
        read = read_token if isinstance(read_token, str) and len(read_token) >= 24 else (token if self.token_ok else None)
        self.view = view or (LoginView(self.url, read) if read else None)
        self.token = token if self.token_ok else None


class LoginReports:
    """The firing service's view across logins (1.1.0).

    get() has GET /followers' shape for every existing caller: `main` and the other top-level fields
    from the primary login, `followers` merged from every login daemon that has firing accounts (a
    row counts only on the login its key is assigned to; each carries `login_id`), plus `logins`:
    {id: {state: CONNECTED | LOGGED OUT | UNREACHABLE}}. It raises only when no daemon answered, as
    a single unreachable daemon did before.

    login_down() and hedge_conflict() serve the lanes from a cache of GET /login reports that a
    background poller refreshes (start()/stop(); poll_once() for one synchronous pass). They never
    touch the network and never raise.
    """

    def __init__(self, links, assign, primary_id, *, hedge_members=(), hedge_guard=True, scope='configured',
                 poll_s=LOGIN_POLL_S, stale_s=LOGIN_STALE_S, clock=time.monotonic):
        self.links = list(links)
        self.by_id = {link.id: link for link in self.links}
        self.assign = dict(assign)
        self.primary_id = primary_id
        self.polled = [link for link in self.links
                       if link.primary or link.id in set(self.assign.values())]
        self.hedge_members = tuple(hedge_members)
        self.hedge_guard = hedge_guard
        self.scope = scope
        self.poll_s, self.stale_s, self.clock = poll_s, stale_s, clock
        # GET /login is read for every login when the hedge check spans logins, else only for the
        # non-primary logins that have lanes (their login state).
        lane_logins = set(self.assign.values()) - {primary_id}
        self.watched = [link for link in self.links
                        if (hedge_guard and len(self.links) > 1) or link.id in lane_logins]
        self.cache = {}       # login id -> (clock, report or None, error name or None)
        self.cache_lock = threading.Lock()
        self.warn_lock = threading.Lock()
        self.warned = {}
        self.stopping = threading.Event()
        self.thread = None

    def fetch(self, login_id, trade_id=None):
        link = self.by_id[login_id]
        if link.report is None:
            raise ValueError('login token missing')
        return link.report.get(trade_id)

    def get(self, trade_id=None):
        out, rows, states, last = None, [], {}, None
        for link in self.polled:
            try:
                data = self.fetch(link.id, trade_id)
                state = login_state(data)
                followers = [r for r in data.get('followers') or [] if isinstance(r, dict)]
            except Exception as exc:  # noqa: BLE001 - one login's bad answer never hides the others
                states[link.id] = {'state': 'UNREACHABLE', 'error': type(exc).__name__}
                last = exc
                continue
            states[link.id] = {'state': state}
            if link.primary:
                out = dict(data)
            for r in followers:
                if self.assign.get(r.get('key'), self.primary_id) == link.id:
                    rows.append(dict(r, login_id=link.id))
        if last is not None and all(v['state'] == 'UNREACHABLE' for v in states.values()):
            raise last if isinstance(last, (OSError, ValueError)) else ValueError('invalid followers report')
        return dict(out or {}, followers=rows, logins=states)

    # --- background GET /login cache ---------------------------------------------------------------

    def _read(self, link, out):
        try:
            if link.view is None:
                raise ValueError('login token missing')
            out[link.id] = (link.view.get(), None)
        except Exception as exc:  # noqa: BLE001 - any failure is a miss; the lanes fail open
            out[link.id] = (None, type(exc).__name__)

    def poll_once(self):
        """Read GET /login of every watched login once, in parallel, and refresh the cache (network)."""
        out, threads = {}, []
        for link in self.watched:
            t = threading.Thread(target=self._read, args=(link, out), name='login-' + link.id, daemon=True)
            t.start()
            threads.append((link, t))
        deadline = time.monotonic() + LOGIN_CHECK_TIMEOUT_S + 0.5
        for link, t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        now = self.clock()
        with self.cache_lock:
            for link, _ in threads:
                data, err = out.get(link.id, (None, 'timeout'))
                self.cache[link.id] = (now, data, err)

    def start(self):
        if self.watched and self.thread is None:
            self.thread = threading.Thread(target=self._run, name='firing-logins', daemon=True)
            self.thread.start()
        return self

    def _run(self):
        while not self.stopping.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - the lanes fail open on a stale cache
                log.error('login poll error (%s)', type(exc).__name__)
            self.stopping.wait(self.poll_s)

    def stop(self, timeout=3):
        self.stopping.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout)

    def cached(self, login_id):
        """(report, None) when the login's latest /login report is fresh and well formed, else (None, why)."""
        with self.cache_lock:
            entry = self.cache.get(login_id)
        if entry is None:
            return None, 'not_polled_yet'
        at, data, err = entry
        if self.clock() - at > self.stale_s:
            return None, 'stale'
        if data is None:
            return None, err or 'unreachable'
        if not isinstance(data, dict) or data.get('schema') != LOGIN_SCHEMA or not isinstance(data.get('page'), dict):
            return None, 'not_a_login_report'
        return data, None

    def _warn(self, login_id, what, kind='unreachable', text=None):
        now = time.monotonic()
        with self.warn_lock:
            last = self.warned.get((login_id, kind))
            if last is not None and now - last < LOGIN_WARN_EVERY_S:
                return
            self.warned[(login_id, kind)] = now
        log.warning(text or 'login %s %s: its exposure is unknown, so follower entries are skipped (fails closed) '
                    'until it answers', login_id, what)

    def login_down(self, login_id):
        """`login_down:<id>` only when the login's daemon answered (fresh cache) that its page is logged
        out. Never for the primary login (main_accepted proves it). Unknown state: None, warned."""
        try:
            link = self.by_id[login_id]
            if link.primary:
                return None
            data, why = self.cached(login_id)
            if data is None:
                self._warn(login_id, why, 'state', 'login %s state unknown (%s): its entries are not skipped; '
                           'an order to a daemon that is gone fails safely')
                return None
            page = data['page']
            unsure = {'page_status_unknown', 'page_status_stale'} & set(page.get('problems') or [])
            if page.get('logged_in') is False and not unsure:
                return 'login_down:' + login_id
            return None
        except Exception as exc:  # noqa: BLE001 - never breaks a lane
            self._warn(login_id, type(exc).__name__, 'state', 'login %s state check failed (%s): entries not skipped')
            return None

    def hedge_conflict(self, login_id, side):
        """`hedge_conflict_login:<id>:<root>` for the first counted account on another login with
        opposite exposure in the hedge group; `hedge_unknown_login:<id>:<why>` when another login's
        exposure is unknown (unreachable, stale, logged out, no token, a bad report: fails closed,
        warned at most once a minute per login); else None. Reads the cache only."""
        if not self.hedge_guard or side not in ('long', 'short'):
            return None
        from .peers import account_conflict, unknown_contracts  # shared with the daemon's peer_logins check
        unknown = None
        for link in self.links:
            if link.id == login_id:
                continue
            try:
                if link.view is None:
                    self._warn(link.id, 'has no token')
                    unknown = unknown or f'hedge_unknown_login:{link.id}:token_missing'
                    continue
                data, why = self.cached(link.id)
                if data is None:
                    self._warn(link.id, 'did not answer with a login report' if why == 'not_a_login_report'
                               else f'unreachable ({why})')
                    unknown = unknown or f'hedge_unknown_login:{link.id}:{why}'
                    continue
                if data['page'].get('logged_in') is not True:
                    self._warn(link.id, 'logged out')
                    unknown = unknown or f'hedge_unknown_login:{link.id}:logged_out'
                    continue
                # The daemon's own cross-login classification (peers.account_conflict): an opposite position,
                # or a working order that could open or add to one; protective orders within a position the
                # account holds on the entry's side do not count; rows with an unknown contract are skipped.
                root = account_conflict(data, side, set(self.hedge_members), scope=self.scope)
                if unknown_contracts(data, self.scope):
                    self._warn(link.id, 'unknown', 'contract_unknown',
                               'login %s reports a position or working order on an %s contract: an opposite one '
                               'skips follower entries (fails closed)')
                if root is not None:
                    return f'hedge_conflict_login:{link.id}:{root}'
            except Exception as exc:  # noqa: BLE001 - one bad report never breaks a lane's poll
                self._warn(link.id, f'check failed ({type(exc).__name__})')
                unknown = unknown or f'hedge_unknown_login:{link.id}:check_failed'
        return unknown


class LaneLogin:
    """What one lane needs from LoginReports: its login id, the login-down check and the hedge check."""

    def __init__(self, reports, login_id):
        self.reports, self.id = reports, login_id
        self.primary = login_id == reports.primary_id

    def down(self):
        try:
            return self.reports.login_down(self.id)
        except Exception:  # noqa: BLE001 - fail open
            return None

    def hedge_conflict(self, side):
        try:
            return self.reports.hedge_conflict(self.id, side)
        except Exception:  # noqa: BLE001 - fails closed: the entry is skipped, exits are untouched
            return 'hedge_unknown_login:check_failed'


class AlertLog:
    """Append-only, fsynced alert lines for external watchers, plus the last few in memory."""

    def __init__(self, directory, clock_ms=_now_ms):
        self.path = Path(directory) / ALERTS_NAME
        self.clock_ms = clock_ms
        self.lock = threading.Lock()
        self.recent = []

    def add(self, account, kind, **detail):
        rec = dict(detail, type='alert', account=account, kind=kind, at=self.clock_ms())
        with self.lock:
            try:
                _append(self.path, rec)
            except OSError as exc:
                log.error('alert write failed (%s)', type(exc).__name__)
            self.recent = (self.recent + [rec])[-20:]
        # 1.0.9: level "info" (follower_entry_skipped) is logged, never as an error.
        log.log(logging.INFO if detail.get('level') == 'info' else logging.ERROR, 'FIRING %s %s %s %s',
                'NOTE' if detail.get('level') == 'info' else 'ALERT', account, kind,
                json.dumps(detail, sort_keys=True)[:300])
        return rec


class FollowerLane(NativeFollower, Mirror):
    """One follower: the mirror, pointed at the follower route, with the entry gate and leader alignment.
    With a native policy (1.0.8), native Soup trades run the follower's own native exit flow."""

    def __init__(self, cfg, token, key, *, alerts, entry_gate='main_accepted', gate_timeout_ms=5000,
                 align_wait_ms=4000, peers=(), report=None, transport=None, bridge_state=None,
                 native_policy=None, login=None, **kw):
        url = cfg.get('bridge_url', MIRROR_DEFAULTS['bridge_url'])  # 1.1.0: the lane's own login daemon
        self.key = key
        self.login = login  # 1.1.0 LaneLogin, or None (one login: 1.0.9 behaviour)
        self.token = token
        self.alerts = alerts
        self.entry_gate = entry_gate
        self.gate_timeout_ms = gate_timeout_ms
        self.align_wait_ms = align_wait_ms
        self.peers = tuple(peers)
        self.report = report
        super().__init__(cfg, token,
                         transport=transport or FollowerTransport(url, token, key,
                                                                  timeout=cfg.get('send_timeout_s', 25)),
                         bridge_state=bridge_state or FollowerState(url, token, key), **kw)
        self.status.update(follower=key, entry_gate=entry_gate, last_main_gate=None, last_alignment=None,
                           follower_exits='native' if native_policy else 'mirror')
        self._native_init(native_policy)

    # --- 1.0.8 native followers: hooks into the mirror loop ---------------------------------

    def _trade_info(self, trade):
        info = Mirror._trade_info(trade)
        info['native'] = native_plan(trade) if self.native_policy is not None else None
        return info

    def _baseline(self, data, now):
        super()._baseline(data, now)
        if self.native_policy is not None:
            self._native_observe(data, now)

    def _observe(self, data, now):
        super()._observe(data, now)
        if self.native_policy is not None:
            self._native_observe(data, now)

    def poll_once(self):
        super().poll_once()
        if self.native_policy is not None:
            self._native_poll()  # also while Soup's database is unreadable: the follower's own flow

    def _reconcile_due(self):
        """As the mirror, except native trades: they end through their own verified exit-all."""
        if self.native_policy is None:
            return super()._reconcile_due()
        for tr in sorted(self.trades.values(), key=lambda t: t['seq']):
            if tr['id'] in self.nat or tr['id'] in self.journal.reconciled or tr['ended_at'] is None \
                    or self.stopping.is_set():
                continue
            now = self.clock_ms()
            newer = self._newer_mirrored(tr)
            if newer:
                self._finish(tr, 'superseded', by=newer)
                continue
            due = tr['ended_at'] + (0 if tr['replaced'] else self.cfg['reconcile_grace_ms'])
            if tr['last_send_at'] is not None:
                due = max(due, tr['last_send_at'] + self.cfg['reconcile_settle_ms'])
            if now < due or now < tr['next_try']:
                continue
            if now > tr['ended_at'] + self.cfg['reconcile_deadline_ms']:
                self._finish(tr, 'stuck', reason=tr['reason'] or 'deadline')
                continue
            self._reconcile(tr)

    # --- entry gate -------------------------------------------------------------------

    @staticmethod
    def main_outcome(c):
        """Soup's recorded outcome of the leader send, or None while it is not answered yet.

        Soup stores the transport outcome in the command's receipt (`transport_outcome`) and
        its status (accepted/rejected/unknown); broker evidence later moves the status on
        (filled, cancelled, rejected). An entry the broker already rejected or cancelled
        is not fired.
        """
        if not c:
            return None
        status = c.get('status')
        transport = (c.get('receipt') or {}).get('transport_outcome')
        if transport not in MAIN_STATUSES:
            transport = status if status in MAIN_STATUSES else None
        if transport is None:
            return None if status in (None, 'ready', 'started') else 'status_' + str(status)
        if transport == 'accepted' and status in ('rejected', 'cancelled'):
            return 'broker_' + status
        return transport

    def _main_command(self, command_id, trade_id):
        data = self.reader.state() or {}
        c = (data.get('commands') or {}).get(command_id)
        if c is None:
            body = self.reader.history(trade_id)  # the trade was already replaced
            c = ((body or {}).get('commands') or {}).get(command_id)
        return c

    def _wait_main(self, command_id, trade_id):
        deadline = time.monotonic() + self.gate_timeout_ms / 1000.0
        c = None
        while True:
            try:
                c = self._main_command(command_id, trade_id)
            except (SoupNotOpen, sqlite3.Error, ValueError):
                pass
            outcome = self.main_outcome(c)
            if outcome is not None or time.monotonic() >= deadline or self.stopping.is_set():
                return outcome, (c or {}).get('status')
            time.sleep(0.01)

    # --- leader alignment ---------------------------------------------------------------

    def _blocked(self, trade_id):
        # A new entry aligns the follower itself; it never waits for an older trade's cleanup.
        if self.queue and self.queue[0][2]['id'] == trade_id and self.queue[0][3]['kind'] == 'entry':
            return False
        return super()._blocked(trade_id)

    def _align_step(self, tr, tid, step, payload_fn):
        """Send one alignment command unless it is already out. Returns None, or a skip reason."""
        attempts = [self.journal.get(f'align-{step}:{tid}' + ('' if n == 0 else f':{n + 1}'))
                    for n in range(MAX_ALIGN_ATTEMPTS)]
        sent = [a for a in attempts if a is not None]
        if any(a['outcome'] in ('accepted', None) for a in sent):
            return None  # out; wait for its effect
        if any(a['outcome'] == 'unknown' for a in sent):
            return f'align_{step}_unknown'
        if len(sent) >= MAX_ALIGN_ATTEMPTS:
            return f'align_{step}_refused'
        cid = f'align-{step}:{tid}' + ('' if not sent else f':{len(sent) + 1}')
        outcome, receipt = self._send(tr, cid, 'align_' + step, payload_fn(cid), None, 'align')
        tr.setdefault('alignment', []).append({'command_id': cid, 'outcome': outcome,
                                               'reason': str(receipt.get('reason') or '')[:160]})
        return f'align_{step}_unknown' if outcome == 'unknown' else None

    def _align(self, info):
        """Make the follower flat with nothing working on the pinned contract before a new entry.

        Returns None when the entry may go, else the reason to skip it. Bounded by align_wait_ms.
        """
        tid, side = info['id'], info.get('side')
        tr = self._track(tid, side)
        deadline = time.monotonic() + self.align_wait_ms / 1000.0
        before, last = None, 'follower_state_unavailable'
        while True:
            try:
                s = self.bridge_state.get(tid)
            except (urllib.error.URLError, OSError, ValueError):
                s = None
            if s is not None:
                if before is None:
                    before = {'net_position': s['net_position'], 'open_order_ids': s['open_order_ids']}
                if s.get('entries_disabled'):
                    return 'follower_killed', before
                if not s['synced'] or not s.get('healthy', True):
                    last = 'follower_not_ready'
                elif s['unseen_accepted_orders']:
                    last = 'orders_not_reported_yet'
                elif s['open_order_ids']:
                    last = 'orders_still_working'
                    reason = self._align_step(tr, tid, 'cancel', lambda cid: {
                        'ticker': self.ticker, 'action': 'cancel',
                        'extras': {'soup_command_id': cid, 'soup_trade_id': tid}})
                    if reason:
                        return reason, before
                elif s['net_position']:
                    last = 'position_not_closed'
                    qty = abs(s['net_position'])
                    reason = self._align_step(tr, tid, 'flatten', lambda cid: {
                        'ticker': self.ticker, 'action': 'align_flatten', 'quantity': qty,
                        'extras': {'soup_command_id': cid, 'soup_trade_id': tid}})
                    if reason:
                        return reason, before
                else:
                    conflict = self._login_hedge_conflict(side)
                    late = time.monotonic() >= deadline
                    if conflict is None and (self._peers_clear(side) or late):
                        return None, before
                    if conflict is not None and late:
                        return conflict, before  # 1.1.0: another login still opposes the entry
                    last = conflict or 'peer_opposite_position'
            if time.monotonic() >= deadline or self.stopping.is_set():
                return ('' if last.startswith(LOGIN_HEDGE_REASONS) else 'align_timeout:') + last, before
            time.sleep(0.1)

    def _login_hedge_conflict(self, side):
        """1.1.0: opposite exposure on another login's accounts (the cached GET /login reports; no
        network here), or None. Waited for within align_wait_ms like a peer's opposite position, then
        the entry is skipped."""
        return None if self.login is None else self.login.hedge_conflict(side)

    def _peers_clear(self, side):
        """True when no other follower of this firing order holds a position opposite to `side`.

        Followers align in parallel; waiting (bounded) for the others keeps this follower's own
        hedge guard, which still checks every account but the leader's exemption, from refusing.
        Native followers (1.0.8) also wait for any single opposite working order (a bracket stop
        of the leader or a peer) to be covered by that account's reported position: a stop leg
        can be reported a moment before the entry fill's position update.
        """
        if (not self.peers and self.native_policy is None) or self.report is None:
            return True
        try:
            data = self.report.get()
        except (urllib.error.URLError, OSError, ValueError):
            return True  # the follower's own hedge guard decides
        rows = {r.get('key'): r for r in data.get('followers') or []}
        sign = 1 if side == 'long' else -1
        if not all(sign * ((rows.get(k) or {}).get('net_position') or 0) >= 0 for k in self.peers):
            return False
        if self.native_policy is not None:
            opposite = 'Sell' if side == 'long' else 'Buy'
            for r in [data.get('main') or {}] + [rows.get(k) or {} for k in self.peers]:
                held = max(0, sign * (r.get('net_position') or 0))
                if any(o.get('action') == opposite and (o.get('qty') or 0) > held for o in r.get('orders') or []):
                    return False
        return True

    # --- dispatch -----------------------------------------------------------------------

    def _skip_entry(self, info, c, reason, **detail):
        self.journal.skip('skipped', c, info['id'], reason=reason, **detail)
        self.alerts.add(self.key, 'entry_not_fired', trade_id=info['id'], command_id=c['id'], reason=reason)

    def _gate_and_align(self, info, c):
        """The entry gate, then leader alignment. True when the entry may be sent (else it is skipped)."""
        if self.entry_gate == 'main_accepted':
            t0 = time.monotonic()
            outcome, status = self._wait_main(c['id'], info['id'])
            waited = round((time.monotonic() - t0) * 1000, 1)
            self.status['last_main_gate'] = {'command_id': c['id'], 'main_outcome': outcome,
                                             'main_status': status, 'waited_ms': waited}
            if outcome != 'accepted':
                if info.get('native') is not None and self.native_policy is not None and outcome == 'rejected' \
                        and self._main_kind(c['id'], info['id']) == 'bracket_entry':
                    # Soup's leader bracket was refused: Soup sends a plain entry for the same signal
                    # next, and the followers take their own bracket on that one. Not a fault here.
                    self.journal.skip('skipped', c, info['id'], reason='main_bracket_rejected_plain_entry_follows',
                                      main_status=status, waited_ms=waited)
                    return False
                self._skip_entry(info, c, 'main_' + (outcome or 'gate_timeout'), main_status=status, waited_ms=waited)
                return False
        if self.login is not None and not self.login.primary:
            # 1.1.0: this lane's login (not the primary: main_accepted already proves that one) answered
            # that its page is logged out: no alignment, no entry. Exits and native stops already working
            # at Tradovate stay; the next signal checks again. Unknown state (cache only) never skips.
            down = self.login.down()
            if down:
                self._skip_entry(info, c, down)
                return False
        if info.get('native') is not None and self.native_policy is not None:
            self._native_supersede(info['id'])
        t0 = time.monotonic()
        reason, before = self._align(info)
        tr = self.trades.get(info['id']) or {}
        self.status['last_alignment'] = {'trade_id': info['id'], 'before': before, 'result': reason or 'aligned',
                                         'actions': tr.get('alignment', []),
                                         'waited_ms': round((time.monotonic() - t0) * 1000, 1)}
        if reason:
            self._skip_entry(info, c, reason, before=before)
            return False
        return True

    def _main_kind(self, command_id, trade_id):
        try:
            return (self._main_command(command_id, trade_id) or {}).get('kind')
        except (SoupNotOpen, sqlite3.Error, ValueError):
            return None

    def _send_command(self, info, c):
        if info.get('native') is not None and self.native_policy is not None:
            return self._native_command(info, c)
        if c['kind'] == 'entry' and not self.journal.has(c['id']):
            if not self._gate_and_align(info, c):
                return
        super()._send_command(info, c)

    def _native_command(self, info, c):
        """A Soup command of a native trade. Only the entry is fired; the follower sends its own
        TP1, stop moves, runner exits and exit-all (tvbridge.follower_native)."""
        tid = info['id']
        if c['kind'] != 'entry':
            reason = 'native_follower_own_tp1' if c['kind'] == 'tp1_limit' else 'native_leader_only'
            self.journal.skip('skipped', c, tid, reason=reason)
            return
        if tid in self.nat:
            self.journal.skip('skipped', c, tid, reason='native_entry_already_sent')
            return
        if self.block_path.exists():
            # An unverified exit-all left this follower for a person: no alignment, no entry.
            return self._skip_entry(info, c, 'follower_blocked_native_exit')
        if not self._gate_and_align(info, c):
            return
        delay = self.clock_ms() - c['started_at']
        if delay > self.cfg['max_send_delay_ms']:
            self.journal.skip('stale_skipped', c, tid, age_ms=delay, reason='held_too_long')
            self.alerts.add(self.key, 'entry_not_fired', trade_id=tid, command_id=c['id'], reason='held_too_long')
            return
        if info['symbol'] != self.ticker:
            self.journal.skip('payload_error', c, tid, error='Soup trade symbol is not the configured ticker')
            return
        self._native_entry(info, c)

    # --- alerts (record + alert; never block the leader) --------------------------------

    def _send(self, tr, command_id, kind, payload, started_at, source):
        outcome, receipt = super()._send(tr, command_id, kind, payload, started_at, source)
        if outcome != 'accepted':
            self.alerts.add(self.key, 'order_' + outcome, trade_id=tr['id'], command_id=command_id,
                            order_kind=kind, source=source,
                            reason=str(receipt.get('reason') or receipt.get('bridge') or
                                       receipt.get('transport') or '')[:200])
        return outcome, receipt

    def _error(self, text):
        super()._error(text)
        self.alerts.add(self.key, 'error', error=str(text)[:300])

    def _finish(self, tr, result, **detail):
        super()._finish(tr, result, **detail)
        if result == 'foreign_position':
            self.alerts.add(self.key, 'foreign_position', trade_id=tr['id'],
                            net_position=detail.get('net_position'))

    def entry_skipped(self, trade_id):
        """True when this lane did not take `trade_id`'s entry (skipped or refused)."""
        st = getattr(self, 'nat', {}).get(trade_id)
        if st is not None:
            if st.get('skipped'):
                return True  # 1.0.9: refused or rejected with zero fill
            return (self.journal.get(st['entry_cid']) or {}).get('outcome') not in ('accepted', 'unknown')
        e = self.journal.entry_for(trade_id)
        return bool(e) and (e['type'] in SKIP_TYPES or e.get('outcome') == 'rejected')


class Firing:
    """Supervisor: one FollowerLane thread per enabled follower, combined status, alerts, grid."""

    def __init__(self, cfg, token, *, clock_ms=_now_ms, soup=None, transports=None, states=None, report=None,
                 login_views=None, env=None):
        """report: the /followers reader (one login), or with multi-login {login id: reader}; login_views:
        {login id: GET /login reader}; env: where token_env names are looked up (default os.environ)."""
        self.main, self.follower_keys = validate(cfg)
        self.follower_exits, self.native_policy = validate_native(cfg)
        self.cfg = cfg
        self.clock_ms = clock_ms
        self.dir = Path(cfg['firing_dir']).expanduser().resolve()
        db = Path(cfg['soup_db']).expanduser().resolve()
        if self.dir == db.parent or db.parent in self.dir.parents:
            raise FiringConfigError('firing_dir must not be inside the Soup database directory')
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.dir, 0o700)
        self.lock_fd = os.open(self.dir / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise RuntimeError('another firing service is running on this firing_dir')
        self.alerts = AlertLog(self.dir, clock_ms)
        self.stopping = threading.Event()
        self.threads = {}
        self.lanes = {}
        self.unavailable = {}
        self.grid_reader = None
        self.grid_lock = threading.Lock()
        self.leader_watch = LeaderFlatWatch()  # 1.0.9: the leader closed outside the bridge
        self.leader_external = {}
        self.logins = None  # 1.1.0 LoginReports when the config spans logins
        self.lane_login = {}
        try:
            if multi_login(cfg):
                self.report = self.logins = self._login_reports(cfg, token, report, login_views,
                                                                os.environ if env is None else env)
            else:
                self.report = report or FollowersReport(cfg.get('bridge_url', MIRROR_DEFAULTS['bridge_url']), token)
            self._check_pins()
            if self.native_policy is not None and soup is None and \
                    'def native_message' not in (Path(cfg['soup_engine_dir']).expanduser() / 'transport.py').read_text():
                raise FiringConfigError('follower_exits "native" needs the Soup native-exits engine in soup_engine_dir')
            soup = soup or load_soup(cfg['soup_engine_dir'])
            base = {k: v for k, v in cfg.items() if k in LANE_KEYS or k in (
                'soup_db', 'soup_engine_dir', 'soup_quantity_policy', 'soup_pairing', 'ticker', 'bridge_url')}
            running = [k for k in self.follower_keys if k not in self.unavailable]
            for key in running:
                lane_cfg, lane_token, login = dict(base, mirror_dir=str(self.dir / key)), token, None
                if self.logins is not None:
                    link = self.logins.by_id[self.logins.assign[key]]
                    lane_cfg['bridge_url'], lane_token = link.url, link.token
                    login = self.lane_login[key] = LaneLogin(self.logins, link.id)
                self.lanes[key] = FollowerLane(
                    lane_cfg, lane_token, key, alerts=self.alerts, clock_ms=clock_ms, soup=soup, login=login,
                    entry_gate=cfg.get('entry_gate', 'main_accepted'),
                    gate_timeout_ms=cfg.get('main_gate_timeout_ms', 5000),
                    align_wait_ms=cfg.get('align_wait_ms', 4000),
                    peers=[k for k in running if k != key], report=self.report,
                    transport=(transports or {}).get(key), bridge_state=(states or {}).get(key),
                    native_policy=self.native_policy)
            root = protocol.product_root(protocol.tradovate_symbol(cfg['ticker']))
            groups = None
            if self.logins is not None:
                groups = [(link.id, link.label, [k for k in self.follower_keys if self.logins.assign[k] == link.id])
                          for link in self.logins.links]
                groups = [g for g in groups if g[0] == self.logins.primary_id or g[2]]
            self.grid = GridTracker(root, self.main, self.follower_keys,
                                    settle_ms=cfg.get('grid_settle_ms', 2000),
                                    attention_ms=cfg.get('grid_attention_ms', 30000), logins=groups)
            self.grid_reader = SoupReader(db, cfg.get('busy_timeout_ms', MIRROR_DEFAULTS['busy_timeout_ms']))
        except BaseException:
            self.close()
            raise

    def _login_reports(self, cfg, token, reports, views, env):
        """1.1.0: one LoginLink per registry login. The primary keeps the given token (TVBRIDGE_TOKEN) and the
        top-level bridge_url; every other login's order token comes from its token_env, and each login's
        /login reads use its read_token_env when set (never logged)."""
        resolved = resolve_logins(cfg)
        if not isinstance(reports, dict):
            reports = {resolved['primary']: reports} if reports is not None else {}
        views, links = views or {}, []
        for entry in resolved['logins']:
            lid = _field(entry, 'id')
            primary = lid == resolved['primary']
            lt = token if primary else env.get(_field(entry, 'token_env') or '', '')
            read_env = _field(entry, 'read_token_env')
            links.append(LoginLink(entry, lt, primary=primary,
                                   url=cfg.get('bridge_url') if primary else None,
                                   report=reports.get(lid), view=views.get(lid),
                                   read_token=env.get(read_env, '') if read_env else None))
        root = protocol.product_root(protocol.tradovate_symbol(cfg['ticker']))
        return LoginReports(links, resolved['assign'], resolved['primary'],
                            hedge_members=_hedge_roots(cfg.get('hedge_groups', DEFAULT_HEDGE_GROUPS), root),
                            hedge_guard=cfg.get('cross_login_hedge_guard', True),
                            scope=cfg.get('peer_hedge_scope', 'configured'))

    def _check_pins(self):
        """Refuse a follower the daemon has not pinned (name/id not in the page's own account sync).
        1.1.0: each login's followers against its own daemon; a login that does not answer at start
        refuses nothing (its daemon still refuses orders for followers it has not pinned)."""
        if self.logins is None:
            return self._check_rows(self.report, None, self.follower_keys)
        for link in self.logins.links:
            keys = [k for k in self.follower_keys if self.logins.assign[k] == link.id]
            if not keys:
                continue
            if not link.token_ok:
                for key in keys:
                    self.unavailable[key] = 'login_token_missing:' + link.id
                    self.alerts.add(key, 'follower_unavailable', reason=self.unavailable[key])
                continue
            self._check_rows(link.report, link.id, keys)

    def _check_rows(self, report, login_id, keys):
        try:
            rows = {r.get('key'): r for r in report.get().get('followers') or []}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning('daemon /followers unreachable at start (%s)%s; lanes start, the daemon still refuses '
                        'orders for followers it has not pinned', type(exc).__name__,
                        '' if login_id is None else f' on login {login_id}')
            return
        for key in keys:
            row = rows.get(key)
            if not row or row.get('available') is not True:
                reason = (row or {}).get('reason') or 'not_configured_in_daemon'
                self.unavailable[key] = reason
                self.alerts.add(key, 'follower_unavailable', reason=reason)
            elif self.native_policy is not None and row.get('native_exits') is not True:
                # 1.0.8: native followers need the daemon's native executor (brackets, flatten_all, auto-protect).
                self.unavailable[key] = 'native_exits_not_enabled_in_daemon'
                self.alerts.add(key, 'follower_unavailable', reason='native_exits_not_enabled_in_daemon')

    # --- grid -----------------------------------------------------------------------------

    def update_grid(self):
        with self.grid_lock:
            return self._update_grid()

    def _update_grid(self):
        data = self.grid_reader.state() or {}
        trade = data.get('trade')
        report = self.report.get(trade['id'] if trade else None)
        self.observe_leader(trade, report)
        skipped = {k for k, lane in self.lanes.items() if trade and lane.entry_skipped(trade['id'])}
        events = self.grid.update(trade, data.get('commands') or {}, report, self.clock_ms(), skipped,
                                  self.unavailable)
        for ev in events:
            log.info('GRID %s\n%s', ev['kind'], ev['text'])
        return events

    def observe_leader(self, trade, report):
        """1.0.9: the leader flat outside the bridge while Soup's trade is still open,
        on 2 consecutive leader snapshots: every native lane in that trade exits at once. Sends nothing
        itself; a lane with no open native trade for this id ignores the fact."""
        if self.native_policy is None:
            return None
        try:
            fact = self.leader_watch.observe(trade, report, self.clock_ms())
        except Exception as exc:  # noqa: BLE001 - never stop the grid or the lanes; Soup's own rule remains
            log.warning('leader watch failed (%s)', type(exc).__name__)
            return None
        if fact is None:
            return None
        self.leader_external = {fact['trade_id']: fact}
        log.error('FIRING leader %s closed outside the bridge (bridge view): entry %s, bridge exits %s, snapshots %s; '
                  'followers exit now', self.main, fact['entry_filled'], fact['bridge_exit_filled'], fact['snapshots'])
        for lane in self.lanes.values():
            if lane.native_policy is not None:
                lane.leader_external[fact['trade_id']] = fact
        return fact

    # --- status and loop ------------------------------------------------------------------

    def status(self):
        lanes = {}
        for key in self.cfg['firing_order'][1:]:
            lane = self.lanes.get(key)
            if lane is None:
                lanes[key] = {'enabled': key in self.follower_keys, 'running': False,
                              'unavailable': self.unavailable.get(key)}
                continue
            s = lane.status
            thread = self.threads.get(key)
            lanes[key] = {'enabled': True, 'running': bool(thread and thread.is_alive()),
                          'baseline_done': s.get('baseline_done'), 'counts': lane.journal.counts(),
                          'last_command': s.get('last_command'), 'last_main_gate': s.get('last_main_gate'),
                          'last_alignment': s.get('last_alignment'),
                          'last_reconciliation': s.get('last_reconciliation'), 'last_error': s.get('last_error'),
                          'soup': s.get('soup'), 'last_poll_at': s.get('last_poll_at'),
                          'native': lane.native_status() if lane.native_policy is not None else None,
                          'pending_reconciliations': [t['id'] for t in lane.trades.values()
                                                      if t['ended_at'] is not None
                                                      and t['id'] not in lane.journal.reconciled]}
        out = {'schema': 'tvbridge.firing.v2', 'pid': os.getpid(), 'updated_at': self.clock_ms(),
               'firing_order': self.cfg['firing_order'], 'main': self.main, 'firing_mode': 'parallel',
               'entry_gate': self.cfg.get('entry_gate', 'main_accepted'), 'follower_exits': self.follower_exits,
               'lanes': lanes, 'leader_external': dict(self.leader_external),
               'grid': self.grid.status(), 'alerts': list(self.alerts.recent)}
        if self.logins is not None:  # 1.1.0 only: the one-login status file is unchanged
            for key, lane in lanes.items():
                lane['login'] = self.logins.assign.get(key)
            out['logins'] = [{'id': link.id, 'label': link.label, 'primary': link.primary,
                              'accounts': [k for k in self.cfg['firing_order'] if self.logins.assign.get(k) == link.id]}
                             for link in self.logins.links]
            out['cross_login_hedge_guard'] = self.logins.hedge_guard
        return out

    def write_status(self):
        try:
            _atomic_write(self.dir / STATUS_NAME, json.dumps(self.status(), sort_keys=True, indent=1) + '\n')
        except OSError as exc:
            log.error('status write failed (%s)', type(exc).__name__)

    def start(self):
        if self.logins is not None:
            # 1.1.0: the /login cache the lanes read; filled once before the lanes start.
            try:
                self.logins.poll_once()
            except Exception as exc:  # noqa: BLE001 - the lanes fail open on an empty cache
                log.warning('first login poll failed (%s)', type(exc).__name__)
            self.logins.start()
        try:
            self.update_grid()  # baseline: events that already happened are never re-sent
        except Exception as exc:  # noqa: BLE001 - the grid never stops the lanes
            log.warning('grid baseline failed (%s)', type(exc).__name__)
        for key, lane in self.lanes.items():
            t = threading.Thread(target=lane.run, name='firing-' + key, daemon=True)
            t.start()
            self.threads[key] = t

    def run(self):
        self.start()
        last_status = 0.0
        try:
            while not self.stopping.wait(0.5):
                for key, t in self.threads.items():
                    if not t.is_alive() and not self.lanes[key].stopping.is_set():
                        self.alerts.add(key, 'lane_stopped')
                        self.lanes[key].stopping.set()
                events = []
                try:
                    events = self.update_grid()
                except Exception as exc:  # noqa: BLE001 - the grid never stops the lanes
                    log.warning('grid update failed (%s)', type(exc).__name__)
                if events or time.monotonic() - last_status >= 1.0:
                    self.write_status()
                    last_status = time.monotonic()
        finally:
            self.stop()

    def stop(self):
        self.stopping.set()
        if self.logins is not None:
            self.logins.stop()
        for lane in self.lanes.values():
            lane.stopping.set()
        for t in self.threads.values():
            t.join(30)
        self.write_status()

    def close(self):
        self.stopping.set()
        if self.logins is not None:
            self.logins.stop()
        for lane in self.lanes.values():
            lane.close()
        if self.grid_reader is not None:
            self.grid_reader.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


def main(argv=None):
    ap = argparse.ArgumentParser(description='Fire Soup commands to follower accounts after the leader account.')
    ap.add_argument('--config', required=True)
    ap.add_argument('--check', action='store_true', help='validate the config and exit (sends nothing)')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.check:
        # Validates the config, and with multi-login the registry and every account's login; starts
        # nothing and sends nothing. Any error: a message on stderr and exit status 1.
        try:
            cfg = json.loads(Path(args.config).read_text())
            if not isinstance(cfg, dict):
                raise FiringConfigError('config must be a JSON object')
            main_key, followers = validate(cfg)
            mode, policy = validate_native(cfg)
            out = {'ok': True, 'main': main_key, 'followers_enabled': followers,
                   'entry_gate': cfg.get('entry_gate', 'main_accepted'), 'follower_exits': mode,
                   'follower_native': policy}
            if multi_login(cfg):
                out['logins'] = resolve_logins(cfg)['assign']
        except (FiringConfigError, ValueError, OSError, KeyError, TypeError) as exc:
            raise SystemExit(f'config invalid: {exc}')
        print(json.dumps(out))
        return
    cfg = json.loads(Path(args.config).read_text())
    token = os.environ.get('TVBRIDGE_TOKEN', '')
    if len(token) < 24:
        raise SystemExit('TVBRIDGE_TOKEN of at least 24 characters required')
    firing = Firing(cfg, token)
    signal.signal(signal.SIGTERM, lambda *_: firing.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: firing.stopping.set())
    log.info('firing order %s: leader %s (Soup route, untouched), followers %s in parallel (refused: %s); '
             'entry gate %s; follower exits %s', cfg['firing_order'], firing.main, list(firing.lanes) or 'none running',
             firing.unavailable or 'none', cfg.get('entry_gate', 'main_accepted'), firing.follower_exits)
    try:
        firing.run()
    finally:
        firing.close()


if __name__ == '__main__':
    main()
