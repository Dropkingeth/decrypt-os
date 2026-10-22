"""Read-only capture of Tradovate fill prices into each journal directory (1.0.9.3).

The Tradovate page already delivers `fill` entities (id, orderId, qty, price, timestamp,
action) into AccountState with every sync and update. Nothing recorded their prices, so
slippage could only be estimated. This module copies them, once each, into an append-only
sidecar `fills.jsonl` beside the command journal (`commands.jsonl`) of the account that owns
the order: the leader's `journal_dir` and each follower's own `journal_dir`.

Passive by construction:
- no Tradovate request, page call or order action; it reads the in-memory state only;
- its own thread (FillCapture) wakes on state changes; the order path never waits for it.
  It holds the state lock only to copy fill/order rows, and the journal lock only to copy
  the few fields it needs, never while writing;
- `commands.jsonl` and `snapshot-sequence` are never touched; tvbridge.journal, reconcile,
  install and rollback read only those, so a `fills.jsonl` beside them changes nothing;
- any error (disk full, unreadable sidecar, odd entity) is logged and counted and the fill is
  retried at the next state change. Nothing is raised into the daemon.

Attribution uses the journal only: a fill belongs to a command when its order id is the
command's broker order id or one of its bracket legs. Fills of orders the bridge did not
place (a hand trade, a prop-firm liquidation) are not recorded. One exception, marked
`attribution: "liquidation_match"`: a `flatten_all`/`auto_flatten` accepted or unknown without
an order id is matched to its exit order exactly as Executor._find_liquidation does
(this account and contract, closing side, Market, id above the dispatch baseline).

Record types (one JSON object per line, sorted keys, fsynced; `schema` 1):
- `fill`: first sighting of a fill id. `price` is null when Tradovate sent none (never invented).
- `fill_price`: a fill first recorded without a price later arrived with one (at most once).
- `fill_deleted`: Tradovate deleted a recorded fill (a bust or correction).
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from . import protocol
from .journal import _append

log = logging.getLogger('tvbridge')

FILLS_NAME = 'fills.jsonl'
SCHEMA = 1
ROLE_BY_KIND = {'entry': 'entry', 'bracket_entry': 'entry', 'tp1_limit': 'tp1', 'bracket_tp1': 'tp1',
                'exit_market': 'exit', 'flatten_all': 'flatten', 'auto_flatten': 'flatten',
                'align_flatten': 'align_flatten'}
FLATTEN_KINDS = ('flatten_all', 'auto_flatten')
MODIFY_KINDS = ('modify_stop', 'auto_modify_stop')
LIQUIDATION_MATCH_WINDOW_MS = 24 * 3600 * 1000  # only recent liquidations are matched by structure
INDEX_WINDOW_MS = 30 * 24 * 3600 * 1000  # commands older than this cannot receive new fills (Day orders)
SLOW_FILL_MS = 1500  # 1.1.2: a recorded fill first seen later than this after its fill time is logged


def finite_price(value):
    """A usable fill price: an int/float (never bool), finite. Else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def exact_avg_price(fills):
    """Quantity-weighted price over `fills`, only when every fill has a positive quantity and a
    finite price; otherwise None (a partly priced average would mislead). Rounded to 6 places."""
    qty = total = 0
    for f in fills:
        q, p = f.get('qty'), finite_price(f.get('price'))
        if type(q) is not int or q <= 0 or p is None:
            return None
        qty += q
        total += p * q
    return round(total / qty, 6) if qty else None


def _text(value):
    return value if isinstance(value, str) else None


class FillRecorder:
    """Fills of one pinned account (one Executor) into `<journal_dir>/fills.jsonl`."""

    def __init__(self, executor, account='main', clock_ms=None):
        self.ex = executor
        self.account = account
        self.path = Path(executor.journal.dir) / FILLS_NAME
        self.clock_ms = clock_ms or executor.clock_ms
        self.recorded = {}     # fill id -> True when recorded with a price
        self.deleted = set()   # fill ids recorded as deleted
        self.foreign = set()   # fill ids of another account's orders (never ours)
        self._index = self._index_key = None
        self._anomaly_pos = len(getattr(executor.state, 'anomalies', []))
        self.written = self.errors = 0
        self.last_error = None
        self._last_log = None
        self._loaded = False  # the sidecar is read on the capture thread, never at daemon start

    # --- sidecar --------------------------------------------------------------------------

    def _load(self):
        """Fill ids already in the sidecar (dedupe across restarts). Never raises."""
        try:
            if not self.path.exists():
                return
            with open(self.path, 'rb') as fh:
                raw = fh.read()
        except OSError as exc:
            self._error('read', exc)
            return
        for line in raw.split(b'\n'):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                fid = int(rec['fill_id'])
                kind = rec.get('type')
            except (ValueError, KeyError, TypeError):
                continue  # a torn or foreign line is skipped; the report skips it too
            if kind == 'fill':
                self.recorded[fid] = self.recorded.get(fid, False) or finite_price(rec.get('price')) is not None
            elif kind == 'fill_price':
                self.recorded[fid] = True
            elif kind == 'fill_deleted':
                self.deleted.add(fid)

    def _write(self, record):
        try:
            _append(self.path, record)
        except OSError as exc:
            self._error('write', exc)
            return False
        self.written += 1
        return True

    def _error(self, what, exc):
        self.errors += 1
        self.last_error = '%s:%s' % (what, type(exc).__name__)
        now = time.monotonic()
        if self._last_log is None or now - self._last_log >= 60:  # a persistent fault logs once a minute
            self._last_log = now
            log.error('fill capture %s: %s failed (%s); orders are unaffected, retried at the next update',
                      self.account, what, type(exc).__name__)

    # --- journal index ----------------------------------------------------------------------

    def _journal_key(self):
        try:
            st = os.stat(self.ex.journal.path)
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None  # no journal yet, or unreadable: rebuild every time

    def _build_index(self):
        """order id -> attribution, from the fields of this account's journal (copied under its lock)."""
        journal = self.ex.journal
        keep = ('command_id', 'trade_id', 'kind', 'side', 'outcome', 'broker_order_id', 'legs', 'parent', 'at',
                'reference_price', 'stop_price', 'tp1_price', 'mode', 'close_action', 'baseline_order_id',
                'target_order_id')
        oldest = self.clock_ms() - INDEX_WINDOW_MS
        with journal.lock:  # copy only what attribution needs; never held while writing
            commands = [{k: c.get(k) for k in keep} for c in journal.commands.values()
                        if not (type(c.get('at')) is int and c['at'] < oldest)
                        and (c.get('broker_order_id') or c.get('legs') or c.get('kind') in FLATTEN_KINDS + MODIFY_KINDS
                             or c.get('kind') == 'bracket_tp1')]
        by_cid = {c['command_id']: c for c in commands}
        index, modifies, liquidations = {}, {}, []

        def add(oid, c, command_id, role, journal_price, parent=None):
            try:
                oid = int(oid)
            except (TypeError, ValueError):
                return
            item = {'command_id': command_id, 'trade_id': c.get('trade_id'), 'kind': c.get('kind'), 'role': role,
                    'side': c.get('side'), 'journal_price': finite_price(journal_price), 'attribution': 'journal'}
            if parent:
                item['parent_command_id'] = parent
            index.setdefault(oid, item)

        for c in commands:
            kind = c.get('kind')
            if kind in MODIFY_KINDS:
                if c.get('outcome') == 'accepted' and c.get('target_order_id') is not None:
                    modifies[str(c['target_order_id'])] = (c['command_id'], finite_price(c.get('stop_price')))
                continue
            if c.get('outcome') == 'rejected':
                continue
            if kind == 'bracket_entry':
                if c.get('broker_order_id'):
                    add(c['broker_order_id'], c, c['command_id'], 'entry', c.get('reference_price'))
                for role, leg in sorted((c.get('legs') or {}).items()):
                    add(leg, c, c['command_id'] + '#' + role, role,
                        c.get('stop_price') if role == 'stop' else c.get('tp1_price'), parent=c['command_id'])
            elif kind == 'bracket_tp1':
                parent = by_cid.get(c.get('parent') or '') or {}
                if c.get('broker_order_id'):
                    add(c['broker_order_id'], c, c['command_id'], 'tp1', parent.get('tp1_price'), parent=c.get('parent'))
            elif c.get('broker_order_id'):
                add(c['broker_order_id'], c, c['command_id'], ROLE_BY_KIND.get(kind, kind or 'order'), None)
            elif kind in FLATTEN_KINDS and c.get('mode') == 'liquidate' and c.get('outcome') in ('accepted', 'unknown'):
                liquidations.append(c)
        for oid, item in index.items():
            if item['role'] == 'stop' and str(oid) in modifies:
                item['modify_command_id'], item['modify_stop_price'] = modifies[str(oid)]
        return index, liquidations

    def _match_liquidations(self, index, liquidations, orders, versions):
        """The exit order of a liquidation accepted/unknown without an id (Executor._find_liquidation's rule)."""
        now = self.clock_ms()
        for c in liquidations:
            at = c.get('at')
            if type(at) is not int or now - at > LIQUIDATION_MATCH_WINDOW_MS:
                continue
            for oid in sorted(orders):
                o = orders[oid]
                if oid <= (c.get('baseline_order_id') or 0) or oid in index or \
                        o.get('accountId') != self.ex.account_id or o.get('contractId') != self.ex.contract_id or \
                        o.get('action') != c.get('close_action'):
                    continue
                if (versions.get(oid) or {}).get('orderType') == 'Market':
                    index[oid] = {'command_id': c['command_id'], 'trade_id': c.get('trade_id'), 'kind': c.get('kind'),
                                  'role': 'flatten', 'side': c.get('side'), 'journal_price': None,
                                  'attribution': 'liquidation_match'}
                    break

    # --- capture ----------------------------------------------------------------------------

    def capture(self):
        """One pass: record new fills of this account's journaled orders. Returns records written. Never raises."""
        try:
            return self._capture()
        except Exception as exc:  # noqa: BLE001 - fill capture must never disturb the daemon
            self._error('capture', exc)
            return 0

    def _capture(self):
        if not self._loaded:
            self._loaded = True
            self._load()
        ex, state = self.ex, self.ex.state
        if ex.account_id is None:
            return 0
        candidates, anomalies = [], []
        orders = versions = None
        with state.lock:
            for fid, f in state.entities['fill'].items():
                if fid in self.foreign or fid in self.deleted or self.recorded.get(fid) is True:
                    continue
                if fid in self.recorded and finite_price(f.get('price')) is None:
                    continue  # recorded without a price and still none
                if f.get('active', True) is False:
                    continue
                order = state.entities['order'].get(f.get('orderId'))
                if order is None:
                    continue  # its order has not arrived yet; looked at again next time
                if order.get('accountId') != ex.account_id:
                    self.foreign.add(fid)
                    continue
                candidates.append((dict(f), dict(order)))
            fresh = state.anomalies[self._anomaly_pos:]
            self._anomaly_pos += len(fresh)
            anomalies = [dict(a) for a in fresh if a.get('kind') == 'fill_deleted']
            if candidates:
                own = [f['orderId'] for f, _ in candidates]
                versions = {}
                for v in state.entities['orderVersion'].values():
                    oid = v.get('orderId')
                    if oid is not None and (oid not in versions or v['id'] > versions[oid]['id']):
                        versions[oid] = v
                orders = {oid: dict(o) for oid, o in state.entities['order'].items()
                          if o.get('accountId') == ex.account_id}
                versions = {oid: dict(v) for oid, v in versions.items() if oid in orders or oid in own}
        written = 0
        if candidates:
            key = self._journal_key()
            if self._index is None or key is None or key != self._index_key:
                self._index = self._build_index()
                self._index_key = key
            index, liquidations = self._index
            index = dict(index)
            if liquidations and any(f['orderId'] not in index for f, _ in candidates):
                self._match_liquidations(index, liquidations, orders, versions)
            for f, order in sorted(candidates, key=lambda x: x[0]['id']):
                item = index.get(f['orderId'])
                if item is None:
                    continue  # not (yet) a journaled order of this account
                written += self._record(f, order, versions.get(f['orderId']) or {}, item)
        for a in anomalies:
            fid = a.get('fill_id')
            if fid in self.recorded and fid not in self.deleted:
                if self._write({'type': 'fill_deleted', 'schema': SCHEMA, 'account': self.account,
                                'account_id': ex.account_id, 'fill_id': str(fid), 'observed_at': a.get('at')}):
                    self.deleted.add(fid)
                    written += 1
        return written

    def _record(self, f, order, version, item):
        fid = f['id']
        price = finite_price(f.get('price'))
        now = self.clock_ms()
        if fid in self.recorded:
            # Recorded without a price; Tradovate has now sent one.
            ok = self._write({'type': 'fill_price', 'schema': SCHEMA, 'account': self.account,
                              'account_id': self.ex.account_id, 'fill_id': str(fid),
                              'broker_order_id': str(f['orderId']), 'command_id': item['command_id'],
                              'trade_id': item['trade_id'], 'price': price, 'observed_at': now})
            if ok:
                self.recorded[fid] = True
            return int(ok)
        try:
            fill_ms = protocol.to_ms(f.get('timestamp'))
        except (TypeError, ValueError):
            fill_ms = None
        date = f.get('tradeDate')
        rec = dict(item, **{
            'type': 'fill', 'schema': SCHEMA, 'account': self.account, 'account_id': self.ex.account_id,
            'fill_id': str(fid), 'broker_order_id': str(f['orderId']),
            'contract_id': f.get('contractId') if type(f.get('contractId')) is int else order.get('contractId'),
            'action': _text(f.get('action')) or _text(order.get('action')), 'qty': f.get('qty'), 'price': price,
            'timestamp': _text(f.get('timestamp')), 'fill_ms': fill_ms,
            'trade_date': ({k: date.get(k) for k in ('year', 'month', 'day')} if isinstance(date, dict) else None),
            'order_type': _text(version.get('orderType')), 'order_price': finite_price(version.get('price')),
            'order_stop_price': finite_price(version.get('stopPrice')), 'observed_at': now})
        ok = self._write(rec)
        if ok:
            self.recorded[fid] = price is not None
            self._note_lag(fid, fill_ms, now)
        return int(ok)

    def _note_lag(self, fid, fill_ms, observed_at):
        """1.1.2: warn when a fill reached the bridge late (Tradovate's fill time vs first sighting here),
        so a Tradovate-side delay can be told from a page stall. Observability only; never raises."""
        try:
            lag = observed_at - fill_ms
            if lag > SLOW_FILL_MS:
                log.warning('fill capture %s: fill %s observed %d ms after its fill time', self.account, fid, lag)
        except Exception:  # noqa: BLE001 - e.g. no fill time
            pass

    def status(self):
        return {'account': self.account, 'path': str(self.path), 'written': self.written, 'errors': self.errors,
                'last_error': self.last_error}


class FillCapture:
    """One daemon thread for every pinned account's FillRecorder. Wakes on AccountState changes
    (and every `interval_s`); never holds a lock the order path needs while writing."""

    def __init__(self, state, recorders, interval_s=1.0):
        self.state, self.recorders, self.interval_s = state, list(recorders), interval_s
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name='fill-capture', daemon=True)

    @classmethod
    def for_daemon(cls, state, main_executor, followers=None):
        # 1.1.0: main_executor is None on a followers_only login (followers only).
        recorders = [] if main_executor is None else [FillRecorder(main_executor, 'main')]
        for key, ex in (followers or {}).items():
            if getattr(ex, 'journal', None) is not None and getattr(ex, 'account_id', None) is not None:
                recorders.append(FillRecorder(ex, key))
        return cls(state, recorders)

    def start(self):
        self.thread.start()
        return self

    def capture_once(self):
        return sum(r.capture() for r in self.recorders)

    def run(self):
        version = -1
        while not self.stopping.is_set():
            try:
                version = self.state.wait_for_change(version, self.interval_s)
                if self.stopping.is_set():
                    return
                self.capture_once()
            except Exception as exc:  # noqa: BLE001 - never stop; never touch orders
                log.error('fill capture loop error (%s)', type(exc).__name__)
                self.stopping.wait(1)

    def stop(self, timeout=5):
        self.stopping.set()
        with self.state.changed:
            self.state.changed.notify_all()
        if self.thread.is_alive():
            self.thread.join(timeout)
