"""Soup -> bridge mirror: a second account trades beside live Soup, with no change to Soup.

Run:  python3 -m tvbridge.mirror --config mirror.json
Env:  TVBRIDGE_TOKEN (the bridge daemon's order/state path secret, >= 24 chars)

Live Soup (runtime.Service._dispatch) persists every command with `started_at` in its
SQLite `state` row (engine.start) before it builds the webhook payload with
transport.message(). This process polls that row READ-ONLY, rebuilds the identical
payload with Soup's own message() and sends it to the local bridge daemon, which trades
the mirror account. Soup never sees the mirror: its route, evidence and decisions are
unchanged.

Rules:
- Commands already started when the mirror starts are recorded as `baseline_skipped`.
  A command first seen more than max_age_ms after its started_at is `stale_skipped`.
- Every send has an fsynced `intent` record first, then its `outcome`. A command id is
  never sent twice; an intent without an outcome is `unknown` and is never retried.
- The mirror account can diverge (a TP1 fills on Soup's account only, for example), so
  Soup's later exit sizes may not fit it. When a trade whose entry the mirror placed
  has ended in Soup (closed for reconcile_grace_ms, or replaced by a new trade), the
  mirror reads the bridge account (GET /state/<token>), cancels orders still working
  on the pinned contract, then closes exactly the remaining position (never more, never
  a flip), with its own command ids `mirror-cancel:<trade>` / `mirror-flatten:<trade>`.
  With `reconcile_mode: "liquidate"` (opt-in; the bridge daemon needs
  `flatten_all_enabled: true`) it sends one flatten_all (`mirror-liquidate:<trade>`,
  Tradovate liquidateposition: cancel working orders and close the position in one
  request) instead. If the bridge refuses it (rejected: definitely not sent), the default
  two-step cleanup runs; an unknown outcome is never retried.
  A position on the mirror account without a mirrored entry is only reported.
- The `cross-account-window` profile is for firm rules that allow opposite exposure across a
  trader's own accounts only briefly. The 15 s reconcile grace plus the cleanup steps are kept short
  for that (firing.py caps grace plus settle at 30 s for such accounts).
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import protocol
from .journal import _append
from .soup_transport import OUTCOMES, TradovateBridgeTransport

log = logging.getLogger('tvbridge.mirror')

DEFAULTS = {
    'bridge_url': 'http://127.0.0.1:8777',
    'poll_interval_ms': 100,
    'busy_timeout_ms': 50,
    'max_age_ms': 5000,           # first sight of a command later than this after started_at: skipped
    'max_send_delay_ms': 10000,   # a command held (reconciliation, slow bridge) longer than this: skipped
    # A closed Soup trade stays closed this long before reconciliation. Kept short so it fits the
    # `cross-account-window` profile's cap (grace plus settle at most 30 s).
    'reconcile_grace_ms': 15000,
    'reconcile_mode': 'two_step',  # or 'liquidate': one flatten_all (bridge flatten_all_enabled)
    'reconcile_settle_ms': 2000,  # never reconcile sooner than this after the mirror's last send for the trade
    'reconcile_deadline_ms': 60000,  # give up (report `stuck`) this long after the trade ended
    'reconcile_step_wait_ms': 5000,  # wait for a cancel or close to show in the account state
    'exit_barrier_wait_ms': 2000,  # before a mirrored market exit, wait this long for the mirror's cancel
    'send_timeout_s': 25,
    'status_interval_ms': 1000,
}
SKIP_TYPES = ('baseline_skipped', 'stale_skipped', 'skipped', 'payload_error')
RECONCILE_MODES = ('two_step', 'liquidate')
JOURNAL_NAME = 'mirror-journal.jsonl'
STATUS_NAME = 'status.json'
# Soup quantity policy markers (vendor soup_r4 core.validate_quantity_policy): 1 = account-fixed-one-tp1-v1,
# 10 = account-original-allocation-micro-cap-v1. The id and marker are kept for compatibility; the marker
# is never used as a size limit (no size cap in the OS: the strategy alert sets the size).
POLICY_MARKERS = (1, 10)
LOCK_NAME = '.mirror.lock'


def _now_ms():
    return time.time_ns() // 1_000_000


KIND_ORDER = {'entry': 0, 'tp1_limit': 1, 'cancel': 2, 'reduce': 3, 'flatten': 3}


def soup_sequence(trade, command):
    """Creation order of a command within its trade.

    Soup stores its state with sorted keys, so the `commands` object comes back in id
    order, not creation order. core.Engine.command() derives each id from
    '<trade id>:<counter>:<kind>', which recovers the counter exactly.
    """
    for n in range(1, int(trade.get('counter') or 0) + 1):
        key = f"{trade['id']}:{n}:{command['kind']}".encode()
        if command['id'] == 'soup-' + hashlib.sha256(key).hexdigest()[:32]:
            return n
    return 1000 + KIND_ORDER.get(command['kind'], 9)


def load_soup(engine_dir):
    """Import Soup's own transport.message and storage.digest from its read-only engine directory."""
    engine_dir = Path(engine_dir).expanduser().resolve()
    for name in ('transport.py', 'storage.py'):
        if not (engine_dir / name).is_file():
            raise ValueError(f'Soup engine directory lacks {name}: {engine_dir}')
    sys.dont_write_bytecode = True  # never leave a __pycache__ in Soup's release
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    modules = [importlib.import_module(name) for name in ('transport', 'storage')]
    for module in modules:
        if Path(module.__file__).resolve().parent != engine_dir:
            raise ValueError(f'module {module.__name__} was already loaded from {module.__file__}, '
                             f'not from {engine_dir}')
    return modules[0].message, modules[1].digest


class SoupNotOpen(Exception):
    """Soup's database is not open in WAL mode; opening it could create files beside it."""


class SoupReader:
    """Short read-only queries on Soup's database; never writes and never creates files.

    Each query finishes its statement at once, so no read transaction stays open to block
    Soup's WAL checkpoints. `PRAGMA data_version` skips re-parsing an unchanged state row.
    """

    def __init__(self, path, busy_timeout_ms=50):
        self.path = Path(path).expanduser()
        self.timeout = busy_timeout_ms / 1000.0
        self.conn = None
        self.version = None
        self.cached = None

    def _open(self):
        # A read-only connection to a WAL database may create -wal/-shm files when they are
        # missing. They exist exactly while Soup has the database open, which is the only
        # time there is anything to mirror.
        for suffix in ('', '-wal', '-shm'):
            if not os.path.isfile(str(self.path) + suffix):
                raise SoupNotOpen(f'{self.path.name}{suffix} not present')
        uri = self.path.resolve().as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=self.timeout, isolation_level=None,
                               check_same_thread=False)
        try:
            self._query(conn, 'PRAGMA query_only=1')
        except BaseException:
            conn.close()
            raise
        self.conn, self.version, self.cached = conn, None, None

    @staticmethod
    def _query(conn, sql, args=()):
        cur = conn.execute(sql, args)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def _run(self, sql, args=()):
        if self.conn is None:
            self._open()
        try:
            return self._query(self.conn, sql, args)
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc) and 'busy' not in str(exc):
                self.close()
            raise
        except sqlite3.DatabaseError:
            self.close()
            raise

    def state(self):
        version = self._run('PRAGMA data_version')[0][0]
        if version != self.version or self.cached is None:
            self.version = None
            rows = self._run('SELECT body FROM state WHERE id=1')
            self.cached = json.loads(rows[0][0]) if rows else {}
            self.version = version
        return self.cached

    def history(self, trade_id):
        rows = self._run('SELECT body FROM history WHERE trade_id=?', (trade_id,))
        return json.loads(rows[0][0]) if rows else None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn, self.version, self.cached = None, None, None


class BridgeState:
    """GET /state/<token> on the bridge daemon: the mirror account's position and working orders."""

    def __init__(self, base_url, token, timeout=3.0):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
            raise ValueError('bridge must be on loopback')
        if not isinstance(token, str) or len(token) < 24:
            raise ValueError('bridge token required')
        self.url = base_url.rstrip('/') + '/state/' + token
        self.timeout = timeout

    def get(self, trade_id=None):
        url = self.url + ('?' + urllib.parse.urlencode({'trade_id': trade_id}) if trade_id else '')
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            data = json.loads(r.read(65537))
        if (not isinstance(data, dict) or type(data.get('net_position')) is not int
                or not isinstance(data.get('open_order_ids'), list) or not isinstance(data.get('synced'), bool)
                or not isinstance(data.get('unseen_accepted_orders'), list)):
            raise ValueError('invalid bridge state response')
        return data


class MirrorJournal:
    """Append-only, fsynced JSON lines. One file, mode 0600, in a 0700 directory."""

    def __init__(self, directory, clock_ms=_now_ms):
        self.dir = Path(directory)
        self.path = self.dir / JOURNAL_NAME
        self.clock_ms = clock_ms
        self.lock = threading.Lock()
        self.commands = {}    # command id -> merged record (type, trade_id, kind, outcome, ...)
        self.reconciled = {}  # trade id -> final reconcile record
        self.torn_tail = False
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        lines = self.path.read_text().split('\n')
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                if i >= len(lines) - 2:
                    self.torn_tail = True
                    continue
                raise
            kind = rec.get('type')
            if kind in SKIP_TYPES:
                self.commands.setdefault(rec['command_id'], dict(rec, outcome=None))
            elif kind == 'intent':
                self.commands[rec['command_id']] = dict(rec, outcome=None)
            elif kind == 'outcome' and rec['command_id'] in self.commands:
                self.commands[rec['command_id']].update(outcome=rec['outcome'], receipt=rec.get('receipt'))
            elif kind == 'reconcile' and rec.get('final'):
                self.reconciled[rec['trade_id']] = rec
        for cid, c in list(self.commands.items()):
            if c['type'] == 'intent' and c['outcome'] is None:
                # Sent or maybe sent before a crash: unknown, never retried.
                self.outcome(cid, 'unknown', {'mirror': 'restarted_after_intent'})

    def append(self, rec):
        with self.lock:
            _append(self.path, rec)

    def has(self, command_id):
        return command_id in self.commands

    def get(self, command_id):
        c = self.commands.get(command_id)
        return dict(c) if c else None

    def skip(self, kind, command, trade_id, **detail):
        rec = dict(detail, type=kind, command_id=command['id'], trade_id=trade_id, kind=command['kind'],
                   started_at=command.get('started_at'), at=self.clock_ms())
        self.commands[command['id']] = dict(rec, outcome=None)
        self.append(rec)
        return rec

    def intent(self, command_id, trade_id, kind, payload, started_at, sent_at, source):
        rec = {'type': 'intent', 'command_id': command_id, 'trade_id': trade_id, 'kind': kind,
               'payload': payload, 'started_at': started_at, 'sent_at': sent_at, 'source': source}
        # In memory first: even if the write fails, this id is never sent by this process.
        self.commands[command_id] = dict(rec, outcome=None)
        self.append(rec)

    def outcome(self, command_id, outcome, receipt, **timing):
        rec = dict(timing, type='outcome', command_id=command_id, outcome=outcome, receipt=receipt,
                   at=self.clock_ms())
        self.commands[command_id].update(outcome=outcome, receipt=receipt, **timing)
        self.append(rec)

    def reconcile(self, trade_id, result, final=False, **detail):
        rec = dict(detail, type='reconcile', trade_id=trade_id, result=result, final=final, at=self.clock_ms())
        if final:
            self.reconciled[trade_id] = rec
        self.append(rec)
        return rec

    def entry_for(self, trade_id):
        for c in self.commands.values():
            if c.get('trade_id') == trade_id and c.get('kind') == 'entry':
                return dict(c)
        return None

    def mirrored_trades(self):
        """Trade ids in journal order whose entry the mirror sent (any outcome)."""
        out = []
        for c in self.commands.values():
            if c['type'] == 'intent' and c.get('kind') == 'entry' and c['trade_id'] not in out:
                out.append(c['trade_id'])
        return out

    def counts(self):
        counts = {'mirrored': 0, 'accepted': 0, 'rejected': 0, 'unknown': 0, 'skipped': 0,
                  'baseline_skipped': 0, 'stale_skipped': 0, 'payload_error': 0, 'skipped_no_entry': 0}
        for c in self.commands.values():
            if c['type'] == 'intent':
                counts['mirrored'] += 1
                if c['outcome'] in OUTCOMES:
                    counts[c['outcome']] += 1
            else:
                counts['skipped'] += 1
                counts['skipped_no_entry' if c['type'] == 'skipped' else c['type']] += 1
        return counts


def _atomic_write(path, data):
    tmp = path.with_name(f'.{path.name}.tmp-{os.getpid()}')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


class Mirror:
    def __init__(self, cfg, token, *, clock_ms=_now_ms, transport=None, bridge_state=None, soup=None):
        for key in ('soup_db', 'soup_engine_dir', 'ticker', 'mirror_dir'):
            if not cfg.get(key):
                raise ValueError(f'config {key} required')
        self.cfg = dict(DEFAULTS, **cfg)
        if self.cfg['reconcile_mode'] not in RECONCILE_MODES:
            raise ValueError('reconcile_mode must be one of ' + ', '.join(RECONCILE_MODES))
        self.clock_ms = clock_ms
        self.ticker = cfg['ticker']
        protocol.tradovate_symbol(self.ticker)  # validates the ticker shape
        self.message, digest = soup or load_soup(cfg['soup_engine_dir'])
        db = Path(cfg['soup_db']).expanduser().resolve()
        self.dir = Path(cfg['mirror_dir']).expanduser().resolve()
        if self.dir == db.parent or db.parent in self.dir.parents:
            raise ValueError('mirror_dir must not be inside the Soup database directory')
        self.pairing_digest = None
        if cfg.get('soup_pairing'):
            pairing = json.loads(Path(cfg['soup_pairing']).expanduser().read_text())
            if pairing.get('ticker') != self.ticker:
                raise ValueError('Soup pairing ticker differs from the configured ticker')
            self.pairing_digest = digest(pairing)
        self.policy = None
        if cfg.get('soup_quantity_policy'):
            self.policy = json.loads(Path(cfg['soup_quantity_policy']).expanduser().read_text())
            # Soup's policy shape keeps `max_contracts` as its policy marker (1 = fixed one contract,
            # 10 = the alert's original allocation). It is not a size limit: there is no size cap.
            if not isinstance(self.policy, dict) or type(self.policy.get('max_contracts')) is not int \
                    or self.policy['max_contracts'] not in POLICY_MARKERS:
                raise ValueError('Soup quantity policy needs its policy marker max_contracts '
                                 '(1 = fixed one contract, 10 = original allocation)')

        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.dir, 0o700)
        self.lock_fd = os.open(self.dir / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise RuntimeError('another mirror is running on this mirror_dir')
        self.journal = MirrorJournal(self.dir, clock_ms)
        self.reader = SoupReader(db, self.cfg['busy_timeout_ms'])
        self.transport = transport or TradovateBridgeTransport(self.cfg['bridge_url'], token,
                                                               timeout=self.cfg['send_timeout_s'])
        self.bridge_state = bridge_state or BridgeState(self.cfg['bridge_url'], token)
        self.stopping = threading.Event()
        self.baselined = False
        self.current = None   # Soup's current trade id
        self.trades = {}      # trade id -> tracking dict
        self.queue = []       # (started_at, order, trade info, command) waiting to be sent
        self.queued = set()
        self.last_status_write = None
        self.status = {'pid': os.getpid(), 'started_at': clock_ms(), 'bridge_url': self.cfg['bridge_url'],
                       'last_poll_at': None, 'last_read_at': None, 'last_poll_duration_ms': None,
                       'db_read_errors': 0, 'soup': None, 'last_latency_ms': None, 'last_command': None,
                       'last_reconciliation': None, 'last_error': None, 'baseline_done': False,
                       'journal_torn_tail': self.journal.torn_tail}

    # --- tracking -------------------------------------------------------------------

    def _track(self, trade_id, side=None):
        tr = self.trades.get(trade_id)
        if tr is None:
            tr = self.trades[trade_id] = {'id': trade_id, 'seq': len(self.trades), 'ended_at': None,
                                          'replaced': False, 'last_send_at': None, 'cancel_sent': False,
                                          'next_try': 0, 'reason': None}
        if side:
            tr['side'] = side
        return tr

    @staticmethod
    def _trade_info(trade):
        return {'id': trade['id'], 'side': trade.get('side'), 'symbol': (trade.get('payload') or {}).get('symbol'),
                'quantity_policy': copy.deepcopy(trade.get('quantity_policy'))}

    def _error(self, text):
        self.status['last_error'] = {'at': self.clock_ms(), 'error': str(text)[:300]}
        log.error('%s', text)

    # --- polling ----------------------------------------------------------------------

    def poll_once(self):
        started = time.monotonic()
        now = self.clock_ms()
        self.status['last_poll_at'] = now
        try:
            data = self.reader.state()
        except SoupNotOpen as exc:
            self.status['soup'] = {'db': 'not_open', 'detail': str(exc)}
            return
        except (sqlite3.Error, ValueError) as exc:
            self.status['db_read_errors'] += 1
            self.status['last_db_error'] = {'at': now, 'error': type(exc).__name__ + ': ' + str(exc)[:120]}
            return
        self.status['last_read_at'] = now
        if self.pairing_digest is not None and data and data.get('pairing') != self.pairing_digest:
            self._error('Soup state pairing differs from the configured pairing file; nothing sent')
            return
        trade = data.get('trade') if data else None
        self.status['soup'] = {'db': 'open', 'trade_id': trade['id'] if trade else None,
                               'phase': trade['phase'] if trade else None}
        if not self.baselined:
            self._baseline(data or {}, now)
        else:
            self._observe(data or {}, now)
        self._reconcile_due()
        self._dispatch()
        self.status['last_poll_duration_ms'] = round((time.monotonic() - started) * 1000, 1)

    def _started(self, commands):
        return [c for c in (commands or {}).values() if c.get('started_at') is not None]

    def _baseline(self, data, now):
        trade = data.get('trade')
        for c in self._started(data.get('commands')):
            if not self.journal.has(c['id']):
                self.journal.skip('baseline_skipped', c, c['trade_id'])
        # Trades mirrored by an earlier run and not reconciled: they ended while we were away.
        for tid in self.journal.mirrored_trades():
            if tid not in self.journal.reconciled and (not trade or tid != trade['id']):
                tr = self._track(tid)
                tr['ended_at'], tr['replaced'] = now, True
                tr['last_send_at'] = max((c.get('sent_at') or 0 for c in self.journal.commands.values()
                                          if c.get('trade_id') == tid and c['type'] == 'intent'), default=0) or None
        if trade:
            tr = self._track(trade['id'], trade.get('side'))
            if trade['phase'] == 'closed':
                tr['ended_at'] = now
        self.current = trade['id'] if trade else None
        self.baselined = self.status['baseline_done'] = True
        log.info('baseline: Soup trade %s, %d command(s) already started and skipped', self.current,
                 len(self._started(data.get('commands'))))

    def _observe(self, data, now):
        trade = data.get('trade')
        tid = trade['id'] if trade else None
        if tid != self.current:
            if self.current is not None:
                old = self.current
                try:
                    body = self.reader.history(old)
                except (sqlite3.Error, ValueError) as exc:
                    body = None
                    self._error(f'history read for replaced trade failed ({type(exc).__name__})')
                if body and body.get('trade'):
                    self._collect(body['trade'], body.get('commands'), now)
                tr = self._track(old)
                tr['replaced'] = True
                if tr['ended_at'] is None:
                    tr['ended_at'] = now
            self.current = tid
        if trade:
            tr = self._track(tid, trade.get('side'))
            self._collect(trade, data.get('commands'), now)
            if trade['phase'] == 'closed' and tr['ended_at'] is None:
                tr['ended_at'] = now

    def _collect(self, trade, commands, now):
        info = self._trade_info(trade)
        added = False
        fresh = sorted(self._started(commands), key=lambda c: (c['started_at'], soup_sequence(trade, c)))
        for c in fresh:
            if self.journal.has(c['id']) or c['id'] in self.queued:
                continue
            age = now - c['started_at']
            if age > self.cfg['max_age_ms']:
                self.journal.skip('stale_skipped', c, trade['id'], age_ms=age)
                log.warning('stale command %s (%s) skipped: first seen %d ms after it started',
                            c['id'], c['kind'], age)
                continue
            self.queue.append((c['started_at'], soup_sequence(trade, c), info, copy.deepcopy(c)))
            self.queued.add(c['id'])
            added = True
        if added:
            # Soup dispatches one command at a time, so start time then creation order is its send order
            # (a replaced trade's last commands started before the next trade's entry).
            self.queue.sort(key=lambda item: (item[0], item[1]))

    # --- dispatch ---------------------------------------------------------------------

    def _blocked(self, trade_id):
        """An older ended trade is still being reconciled: its cleanup goes first."""
        mine = self.trades.get(trade_id)
        for tr in self.trades.values():
            if tr['id'] == trade_id or tr['ended_at'] is None or tr['id'] in self.journal.reconciled:
                continue
            if mine is None or tr['seq'] < mine['seq']:
                return True
        return False

    def _dispatch(self):
        while self.queue and not self.stopping.is_set():
            _, _, info, c = self.queue[0]
            if self._blocked(info['id']):
                return
            self.queue.pop(0)
            self.queued.discard(c['id'])
            self._send_command(info, c)

    def _quantity_limit(self, info):
        """The trade's pinned Soup policy marker (1 or 10, see POLICY_MARKERS), handed to Soup's own
        message builder exactly as runtime.Service does. A marker, never a size cap."""
        pinned = info['quantity_policy']
        if self.policy is not None and pinned != self.policy:
            raise ValueError('trade quantity policy differs from the configured policy file')
        return pinned['max_contracts'] if pinned else None

    def _send_command(self, info, c):
        tid = info['id']
        tr = self._track(tid, info.get('side'))
        if c['kind'] != 'entry':
            entry = self.journal.entry_for(tid)
            if not entry or entry['type'] != 'intent' or entry['outcome'] not in ('accepted', 'unknown'):
                self.journal.skip('skipped', c, tid, reason='entry_not_mirrored',
                                  entry=(entry or {}).get('outcome') or (entry or {}).get('type'))
                return
        delay = self.clock_ms() - c['started_at']
        if delay > self.cfg['max_send_delay_ms']:
            self.journal.skip('stale_skipped', c, tid, age_ms=delay, reason='held_too_long')
            self._error(f'command {c["id"]} ({c["kind"]}) held {delay} ms; skipped')
            return
        try:
            if info['symbol'] != self.ticker:
                raise ValueError(f'Soup trade symbol {info["symbol"]} is not the configured ticker')
            # Exactly runtime.Service._dispatch: message(c, ticker, trade side, quantity_limit=policy max).
            payload = self.message(c, self.ticker, info['side'],
                                   quantity_limit=self._quantity_limit(info))
        except (ValueError, TypeError, KeyError) as exc:
            self.journal.skip('payload_error', c, tid, error=str(exc)[:200])
            self._error(f'payload for {c["id"]} not rebuilt: {exc}')
            return
        if c['kind'] in ('reduce', 'flatten') and tr['cancel_sent']:
            # Soup waits for cancel evidence on its own account before an exit; do the same here.
            self._wait_bridge(None, lambda s: s['synced'] and not s['open_order_ids']
                              and not s['unseen_accepted_orders'], self.cfg['exit_barrier_wait_ms'])
        self._send(tr, c['id'], c['kind'], payload, c['started_at'], 'soup')

    def _send(self, tr, command_id, kind, payload, started_at, source):
        sent_at = self.clock_ms()
        self.journal.intent(command_id, tr['id'], kind, payload, started_at, sent_at, source)
        qty = payload.get('quantity') if isinstance(payload, dict) else None
        if kind == 'entry' and (type(qty) is not int or qty < 1):
            # Format only (no size cap; the daemon checks it as well): nothing is sent.
            receipt = {'mirror': 'refused', 'reason': f'entry quantity {qty!r} is not a positive whole number'}
            self.journal.outcome(command_id, 'rejected', receipt)
            log.warning('entry %s refused before sending: %s', command_id, receipt['reason'])
            return 'rejected', receipt
        t0 = time.monotonic()
        try:
            outcome, receipt = self.transport.send(payload)
        except Exception as exc:  # noqa: BLE001 - after the intent, any failure is an unknown outcome
            outcome, receipt = 'unknown', {'mirror': 'transport_exception', 'error': type(exc).__name__}
        if outcome not in OUTCOMES or not isinstance(receipt, dict):
            outcome, receipt = 'unknown', {'mirror': 'invalid_transport_result'}
        rtt = round((time.monotonic() - t0) * 1000, 1)
        timing = {'rtt_ms': rtt}
        if started_at is not None:
            timing['latency_ms'] = sent_at - started_at
            self.status['last_latency_ms'] = sent_at - started_at
        self.journal.outcome(command_id, outcome, receipt, **timing)
        tr['last_send_at'] = self.clock_ms()
        if kind == 'cancel':
            tr['cancel_sent'] = True
        self.status['last_command'] = dict(timing, command_id=command_id, kind=kind, trade_id=tr['id'],
                                           outcome=outcome, source=source, sent_at=sent_at)
        level = logging.INFO if outcome == 'accepted' else logging.WARNING
        log.log(level, 'mirrored %s %s -> %s (%s ms after Soup started it)', kind, command_id, outcome,
                timing.get('latency_ms', '-'))
        self._write_status(force=True)
        return outcome, receipt

    # --- end-of-trade reconciliation --------------------------------------------------

    def _wait_bridge(self, trade_id, done, timeout_ms):
        """Poll the bridge account state until done(state) or timeout. Returns the last state or None."""
        deadline = time.monotonic() + timeout_ms / 1000.0
        last = None
        while True:
            try:
                last = self.bridge_state.get(trade_id)
                if done(last):
                    return last
            except (urllib.error.URLError, OSError, ValueError):
                last = None
            if time.monotonic() >= deadline or self.stopping.is_set():
                return last
            time.sleep(0.1)

    def _finish(self, tr, result, **detail):
        rec = self.journal.reconcile(tr['id'], result, final=True, **detail)
        self.status['last_reconciliation'] = rec
        if result in ('stuck', 'opposite_position'):
            self._error(f'reconciliation of {tr["id"]}: {result} {detail}')
        elif result == 'foreign_position':
            log.warning('foreign position on the mirror account after %s: %s (left untouched)', tr['id'], detail)
        else:
            log.info('reconciled %s: %s', tr['id'], result)
        self._write_status(force=True)

    def _newer_mirrored(self, tr):
        for other in self.trades.values():
            if other['seq'] > tr['seq']:
                entry = self.journal.entry_for(other['id'])
                if entry and entry['type'] == 'intent' and entry['outcome'] in ('accepted', 'unknown'):
                    return other['id']
        return None

    def _reconcile_due(self):
        for tr in sorted(self.trades.values(), key=lambda t: t['seq']):
            if tr['id'] in self.journal.reconciled or tr['ended_at'] is None or self.stopping.is_set():
                continue
            now = self.clock_ms()
            newer = self._newer_mirrored(tr)
            if newer:
                # The newer entry needed a flat account with nothing working, so this trade is done.
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

    def _retry(self, tr, reason):
        tr['reason'] = reason
        tr['next_try'] = self.clock_ms() + 500
        self.status['reconcile_waiting'] = {'trade_id': tr['id'], 'reason': reason}

    def _reconcile(self, tr):
        tid = tr['id']
        try:
            s = self.bridge_state.get(tid)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return self._retry(tr, 'bridge_state_unavailable:' + type(exc).__name__)
        if not s['synced'] or s['unseen_accepted_orders']:
            return self._retry(tr, 'bridge_not_synced' if not s['synced'] else 'orders_not_reported_yet')
        entry = self.journal.entry_for(tid)
        mirrored = (entry is not None and entry['type'] == 'intent' and entry['outcome'] == 'accepted') \
            or s.get('trade_entry_outcome') == 'accepted'
        view = {'net_position': s['net_position'], 'open_order_ids': s['open_order_ids']}
        if not mirrored:
            if s['net_position'] or s['open_order_ids']:
                return self._finish(tr, 'foreign_position', entry=(entry or {}).get('outcome') or
                                    (entry or {}).get('type'), **view)
            return self._finish(tr, 'nothing_to_do', entry=(entry or {}).get('outcome') or (entry or {}).get('type'))
        if not s.get('healthy', True) and (s['net_position'] or s['open_order_ids']):
            # The bridge would refuse the cleanup, and mirror commands are never retried: wait.
            return self._retry(tr, 'bridge_unhealthy:' + ','.join(s.get('problems') or [])[:120])
        side =tr.get('side') or protocol.side_from_trade_id(tid)
        sign = 1 if side == 'long' else -1 if side == 'short' else 0
        if sign == 0 or protocol.side_from_trade_id(tid) != side:
            return self._finish(tr, 'stuck', reason='trade side unknown', **view)
        if sign * s['net_position'] < 0:
            return self._finish(tr, 'opposite_position', **view)  # never flip; a human decides
        actions = tr.setdefault('actions', [])
        if self.cfg['reconcile_mode'] == 'liquidate' and not tr.get('liquidate_refused') \
                and (s['open_order_ids'] or s['net_position']):
            done = self._liquidate(tr, s, view, actions)
            if done:
                return
            try:  # refused by the bridge: fall back to the two-step cleanup
                s = self.bridge_state.get(tid)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                return self._retry(tr, 'bridge_state_unavailable:' + type(exc).__name__)
            view = {'net_position': s['net_position'], 'open_order_ids': s['open_order_ids']}
            if not s['synced'] or s['unseen_accepted_orders']:
                return self._retry(tr, 'bridge_not_synced')
        if s['open_order_ids']:
            cid = 'mirror-cancel:' + tid
            if self.journal.has(cid):
                return self._retry(tr, 'orders_still_working_after_cancel')
            payload = {'ticker': self.ticker, 'extras': {'soup_command_id': cid, 'soup_trade_id': tid},
                       'action': 'cancel'}
            outcome, _ = self._send(tr, cid, 'cancel', payload, None, 'reconcile')
            actions.append({'command_id': cid, 'outcome': outcome, 'open_order_ids': s['open_order_ids']})
            self.journal.reconcile(tid, 'cancel_sent', outcome=outcome, **view)
            s = self._wait_bridge(tid, lambda x: x['synced'] and not x['open_order_ids']
                                  and not x['unseen_accepted_orders'], self.cfg['reconcile_step_wait_ms'])
            if s is None or s['open_order_ids'] or not s['synced']:
                return self._retry(tr, 'orders_still_working_after_cancel')
            view = {'net_position': s['net_position'], 'open_order_ids': s['open_order_ids']}
            if sign * s['net_position'] < 0:
                return self._finish(tr, 'opposite_position', actions=actions, **view)
        held = sign * s['net_position']
        if held > 0:
            cid = 'mirror-flatten:' + tid
            prior = self.journal.get(cid)
            if prior is not None:
                if prior['outcome'] == 'accepted':
                    return self._retry(tr, 'waiting_for_close_fill')
                return self._finish(tr, 'stuck', reason='close_' + str(prior['outcome']), actions=actions, **view)
            payload = {'ticker': self.ticker, 'extras': {'soup_command_id': cid, 'soup_trade_id': tid},
                       'action': 'exit', 'orderType': 'market', 'quantity': held,
                       'quantityType': 'fixed_quantity', 'cancel': False}
            outcome, receipt = self._send(tr, cid, 'flatten', payload, None, 'reconcile')
            actions.append({'command_id': cid, 'outcome': outcome, 'quantity': held})
            self.journal.reconcile(tid, 'flatten_sent', outcome=outcome, quantity=held, **view)
            if outcome != 'accepted':
                return self._finish(tr, 'stuck', reason='close_' + outcome, receipt=receipt, actions=actions, **view)
            s = self._wait_bridge(tid, lambda x: x['synced'] and x['net_position'] == 0
                                  and not x['open_order_ids'] and not x['unseen_accepted_orders'],
                                  self.cfg['reconcile_step_wait_ms'])
            if s is None or s['net_position'] != 0 or s['open_order_ids']:
                return self._retry(tr, 'waiting_for_close_fill')
            view = {'net_position': s['net_position'], 'open_order_ids': s['open_order_ids']}
        self._finish(tr, 'flat', actions=actions, **view)

    def _liquidate(self, tr, s, view, actions):
        """One flatten_all for the whole cleanup. True when this pass is finished (flat, waiting or
        stuck); False when the bridge refused it and the two-step cleanup should run instead."""
        tid = tr['id']
        cid = 'mirror-liquidate:' + tid
        prior = self.journal.get(cid)
        if prior is not None:
            if prior['outcome'] == 'accepted':
                self._retry(tr, 'waiting_for_liquidation')
                return True
            self._finish(tr, 'stuck', reason='liquidate_' + str(prior['outcome']), actions=actions, **view)
            return True
        payload = {'ticker': self.ticker, 'action': 'flatten_all',
                   'extras': {'soup_command_id': cid, 'soup_trade_id': tid, 'bridge_kind': 'flatten_all'}}
        outcome, receipt = self._send(tr, cid, 'flatten_all', payload, None, 'reconcile')
        actions.append({'command_id': cid, 'outcome': outcome, 'open_order_ids': s['open_order_ids'],
                        'quantity': abs(s['net_position'])})
        self.journal.reconcile(tid, 'liquidate_sent', outcome=outcome, **view)
        if outcome == 'rejected':
            tr['liquidate_refused'] = True
            log.warning('bridge refused flatten_all for %s (%s); using cancel + exit',
                        tid, str(receipt.get('reason') or receipt.get('bridge') or '')[:120])
            return False
        if outcome != 'accepted':
            self._finish(tr, 'stuck', reason='liquidate_' + outcome, receipt=receipt, actions=actions, **view)
            return True
        s = self._wait_bridge(tid, lambda x: x['synced'] and x['net_position'] == 0
                              and not x['open_order_ids'] and not x['unseen_accepted_orders'],
                              self.cfg['reconcile_step_wait_ms'])
        if s is None or s['net_position'] != 0 or s['open_order_ids']:
            self._retry(tr, 'waiting_for_liquidation')
            return True
        self._finish(tr, 'flat', actions=actions, net_position=s['net_position'], open_order_ids=s['open_order_ids'])
        return True

    # --- status and main loop ---------------------------------------------------------

    def _write_status(self, force=False):
        now = time.monotonic()
        if not force and self.last_status_write is not None and \
                now - self.last_status_write < self.cfg['status_interval_ms'] / 1000.0:
            return
        self.last_status_write = now
        clock = self.clock_ms()
        status = dict(self.status, updated_at=clock, counts=self.journal.counts(),
                      poll_lag_ms=None if self.status['last_read_at'] is None else clock - self.status['last_read_at'],
                      queued=[c['id'] for *_, c in self.queue],
                      pending_reconciliations=[t['id'] for t in self.trades.values()
                                               if t['ended_at'] is not None and t['id'] not in self.journal.reconciled])
        try:
            _atomic_write(self.dir / STATUS_NAME, json.dumps(status, sort_keys=True, indent=1) + '\n')
        except OSError as exc:
            log.error('status write failed (%s)', type(exc).__name__)

    def run(self):
        interval = self.cfg['poll_interval_ms'] / 1000.0
        while not self.stopping.is_set():
            t0 = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - one bad poll must not stop the mirror
                self._error(f'poll failed: {type(exc).__name__}: {exc}')
                log.exception('poll failed')
            self._write_status()
            self.stopping.wait(max(0.0, interval - (time.monotonic() - t0)))
        self._write_status(force=True)

    def close(self):
        self.stopping.set()
        self.reader.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


def main(argv=None):
    ap = argparse.ArgumentParser(description='Mirror live Soup commands onto the bridge account.')
    ap.add_argument('--config', required=True)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = json.loads(Path(args.config).read_text())
    token = os.environ.get('TVBRIDGE_TOKEN', '')
    if len(token) < 24:
        raise SystemExit('TVBRIDGE_TOKEN of at least 24 characters required')
    mirror = Mirror(cfg, token)
    signal.signal(signal.SIGTERM, lambda *_: mirror.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: mirror.stopping.set())
    log.info('mirroring Soup database %s (%s) to %s; journal %s', cfg['soup_db'], cfg['ticker'],
             mirror.cfg['bridge_url'], mirror.dir / JOURNAL_NAME)
    try:
        mirror.run()
    finally:
        mirror.close()


if __name__ == '__main__':
    main()
