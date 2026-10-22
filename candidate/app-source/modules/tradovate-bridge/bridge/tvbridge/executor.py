"""Soup command execution and broker evidence through the Tradovate bridge.

`send()` accepts the exact webhook-shaped payload that Soup's transport
layer builds today and returns Soup's (outcome, receipt) contract:

- accepted: Tradovate acknowledged the order request (not a fill)
- rejected: definitely not placed (refused by a guard, never sent, or refused by Tradovate)
- unknown:  may or may not have been placed; never retried

Extended kinds (opt-in, OFF by default). They are reachable only through an explicit
`extras.bridge_kind` AND an enabling config flag; Soup's payloads never carry
`bridge_kind`, so they keep their exact current meaning:

- bracket_entry  (bracket_orders_enabled): market entry of N with a protective STOP for N,
                 optionally a TP1 LIMIT for M <= N. M == N: one order/placeoso with
                 bracket1 = stop, bracket2 = TP1 (Tradovate links them OCO). M < N: OSO legs
                 inherit the parent quantity (no orderQty field on a leg), so the bridge sends
                 order/placeoso(entry + stop) and, once the entry has completely filled, a
                 separate TP1 limit for M (sub-command `<id>#tp1`).
- modify_stop    (bracket_orders_enabled): order/modifyorder on the working protective stop:
                 new stop price and/or quantity, never above the live position.
- flatten_all    (flatten_all_enabled): order/liquidateposition for the pinned account and
                 contract (closes the position and cancels working orders there). Already flat
                 with nothing working: accepted no-op, nothing sent.

Soup native exits (1.0.6.2, only for trades whose entry is an accepted bracket_entry, so
nothing changes while bracket_orders_enabled is false):
- a bracket_entry the bridge definitely refused does not count as the trade's entry, so the
  same signal can fall back to a plain market entry;
- Soup's own TP1 limit and a partial market reduce may rest beside the trade's own bracket
  stop; a reduce only when stop + reduce cannot exceed the live position (no reversal);
- flatten_all cancels orders still working after the liquidation (liquidateposition's own
  cancel is unverified), and reports the stop/flatten quantities Soup needs as evidence;
- auto-protect also cancels any of the trade's orders left working when flat, and liquidates a
  position its own fills reversed through flat (and sets the kill switch).

Wire status: placeorder, modifyorder (Limit), cancelorder and liquidateposition
bodies/replies match what the Tradovate web trader sends. placeoso, a Stop modify and
liquidateposition cancelling working orders are documentation-based and unverified
until a bracket test on a demo account.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import adapters, protocol
from .fills import exact_avg_price
from .journal import ENTRY_KINDS, NO_ORDER_KINDS, _append
from .state import reject_text

log = logging.getLogger('tvbridge')

# 1.1.2: every refused command, one JSON line each, beside commands.jsonl (never inside it).
REFUSALS_NAME = 'refusals.jsonl'
# the latest fill price seen on the pinned contract, kept beside the journal so the bracket
# reference-price check still has a market price after a restart (Tradovate's sync may have no fill).
MARKET_PRICE_NAME = 'last-market-price.json'
REFERENCE_PRICE_MAX_AGE_S = 4 * 24 * 3600   # a weekend plus a holiday

# Product roots treated as one instrument for the anti-hedge rule (many prop firms ban
# opposite positions in correlated instruments, across accounts).
DEFAULT_HEDGE_GROUPS = (('ES', 'MES', 'NQ', 'MNQ', 'YM', 'MYM', 'RTY', 'M2K'),)
HEDGE_OFF_ACK = 'hedge_guard_off_acknowledged'

EXTENDED_KINDS = ('bracket_entry', 'modify_stop', 'flatten_all')
_BASE_EXTRAS = frozenset({'soup_command_id', 'soup_trade_id', 'bridge_kind'})
_EXTRA_KEYS = {
    'bracket_entry': _BASE_EXTRAS | {'reference_price', 'stop_price', 'tp1_price', 'tp1_quantity'},
    'modify_stop': _BASE_EXTRAS | {'stop_price', 'stop_quantity'},
    'flatten_all': _BASE_EXTRAS,
}
_ENABLE_FLAG = {'bracket_entry': 'bracket_orders_enabled', 'modify_stop': 'bracket_orders_enabled',
                'flatten_all': 'flatten_all_enabled'}


# No contract cap in the OS: the strategy alert sets the size. A bridge entry only needs a positive
# whole-number quantity, a flat account and broker confirmation; exits never exceed the position.
# `max_contracts` is a retired config key: configs that still carry a positive whole number keep
# loading (the value is ignored, never a limit); a malformed value is refused so a corrupt config
# never starts.


def max_contracts_problem(value):
    """None when the retired `max_contracts` key is absent or a legacy positive whole number (ignored)."""
    if value is None or (type(value) is int and value >= 1):
        return None
    return (f'max_contracts is retired (the strategy alert sets the size); remove it from the config '
            f'(found {value!r})')


def check_max_contracts(value):
    """Raise ValueError when the retired `max_contracts` key holds a malformed value."""
    problem = max_contracts_problem(value)
    if problem:
        raise ValueError(problem)
    return value


KILL_SWITCH_ON = 'kill_switch_on'


def pause_fields(source):
    """{'entries_disabled': bool} for /health and report rows, plus `entries_disabled_reason` only when the
    pause is a fail-closed one (switch missing, unusable or unreadable): a plain pause and an armed account
    answer exactly as before. `source` has entries_disabled_reason() (or only entries_disabled())."""
    reason_of = getattr(source, 'entries_disabled_reason', None)
    if reason_of is None:
        return {'entries_disabled': source.entries_disabled()}
    why = reason_of()
    out = {'entries_disabled': why is not None}
    if why is not None and not why.endswith(KILL_SWITCH_ON):
        out['entries_disabled_reason'] = why
    return out


def kill_switch_state(path, writable=True):
    """Why entries are paused by the kill-switch file at `path`, or None when they are armed.

    Fails closed: anything that is not a clearly absent file in a usable folder pauses entries.
    - no path configured, or not an absolute path: `kill_switch_not_configured`;
    - anything at the path (file, folder, broken symlink): `kill_switch_on`;
    - the path cannot be checked (permission, not a folder on the way): `kill_switch_unreadable:<errno>`;
    - its folder is missing, not a folder, or not readable (and, with `writable`, not writable, so
      auto-protect could not set the switch): `kill_switch_folder_<missing|unusable>`.
    """
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return 'kill_switch_not_configured'
    try:
        os.lstat(path)
        return KILL_SWITCH_ON
    except FileNotFoundError:
        pass
    except OSError as exc:
        return 'kill_switch_unreadable:' + (errno.errorcode.get(exc.errno) or type(exc).__name__)
    folder = os.path.dirname(path)
    try:
        st = os.stat(folder)
    except OSError:
        return 'kill_switch_folder_missing'
    mode = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if not stat.S_ISDIR(st.st_mode) or not os.access(folder, mode):
        return 'kill_switch_folder_unusable'
    return None


class Refused(Exception):
    def __init__(self, message, **extra):
        super().__init__(message)
        # 1.0.9.5.2: additive receipt fields (e.g. entry_reject_reason); empty for every other refusal.
        self.extra = extra


def _refusal(e):
    return dict({'bridge': 'refused', 'reason': str(e)}, **getattr(e, 'extra', {}))


def _with_reject_reason(detail, failure_reason, failure_text):
    """1.0.9.5.2: a rejected receipt gains `reject_reason` (Tradovate's reply text, bounded and
    sanitized) when Tradovate gave one; otherwise the receipt is returned unchanged."""
    text = reject_text(failure_reason, failure_text) if failure_reason else None
    return dict(detail, reject_reason=text) if text else detail


def _hedge_roots(groups, root):
    """Validated hedge groups -> ordered roots correlated with `root` (always includes root)."""
    if not isinstance(groups, (list, tuple)):
        raise ValueError('hedge_groups must be a list of lists of product roots')
    members = []
    for group in groups:
        if not isinstance(group, (list, tuple)) or not group or \
                not all(isinstance(r, str) and r.strip() for r in group):
            raise ValueError('hedge_groups must be a list of lists of product roots')
        group = [r.strip().upper() for r in group]
        if root in group:
            members += [r for r in group if r not in members]
    return members or [root]


def _now_ms():
    return time.time_ns() // 1_000_000


def _iso_ms(value):
    """Epoch ms of a Tradovate ISO timestamp ('2021-01-01T14:30:00.123Z'), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def client_tag(command_id):
    """Short, stable order tag derived from the Soup command id (Tradovate `text`)."""
    return 'DL' + hashlib.sha256(command_id.encode()).hexdigest()[:12]


def leg_command_id(command_id, role):
    """Stable id of a bracket leg or bridge sub-command: '<command id>#<role>'."""
    return command_id + '#' + role


class Executor:
    def __init__(self, bridge, state, journal, config, clock_ms=_now_ms):
        self.bridge = bridge
        self.state = state
        self.journal = journal
        self.cfg = config
        self.clock_ms = clock_ms
        self.lock = threading.Lock()
        # The retired max_contracts key: a legacy positive value is ignored, a malformed one never starts.
        check_max_contracts(config.get('max_contracts'))
        # one dated contract of a known root (MNQ, MES) with its own tick size, and the
        # strategy adapters this binding enables (Soup only unless `adapters` says otherwise). Unknown
        # roots, ticks or adapters refuse to start.
        self.contract = adapters.check_contract(config.get('ticker'), config.get('tick_size'))
        self.adapters = adapters.binding_adapters(config)
        self.soup_enabled = adapters.SOUP in self.adapters
        self.symbol = protocol.tradovate_symbol(config['ticker'])
        self.account_id = None
        self.contract_id = None
        self.trade_url = None
        self.watchdogs = []  # callables returning a problem string or None
        self.hedge_guard = config.get('hedge_guard', True)
        if not isinstance(self.hedge_guard, bool):
            raise ValueError('hedge_guard must be true or false')
        if self.hedge_guard is False and config.get(HEDGE_OFF_ACK) is not True:
            # Turning the anti-hedge guard off is a deliberate act (prop firms ban hedging): it needs both keys.
            raise ValueError(f'hedge_guard false also needs {HEDGE_OFF_ACK}: true (opposite positions across '
                             f'accounts and correlated contracts are then not checked)')
        self.root = protocol.product_root(self.symbol)
        if self.root is None:
            raise ValueError(f'cannot derive a product root from {self.symbol}')
        self.hedge_members = _hedge_roots(config.get('hedge_groups', DEFAULT_HEDGE_GROUPS), self.root)
        self.contract_names = {}  # contract id -> name; ids are permanent at Tradovate
        # 1.0.7 firing order: follower account ids that never block THIS account's entries
        # (the leader). Empty unless tvbridge.followers pins followers; then the followers align
        # to the leader instead (tvbridge.firing). Every other account is still checked.
        self.hedge_exempt_accounts = frozenset()
        if not self.hedge_guard:
            log.warning('hedge_guard is disabled: entries will NOT be checked for opposite positions or '
                        'orders in other accounts or correlated contracts (prop-firm hedging rules)')
        for flag in ('bracket_orders_enabled', 'flatten_all_enabled', 'bracket_auto_protect'):
            if not isinstance(config.get(flag, False if flag != 'bracket_auto_protect' else True), bool):
                raise ValueError(flag + ' must be true or false')
        # 1.0.9.3 (opt-in, default false): each snapshot order with fills also carries
        # `avg_fill_price`, only when every one of its fills has a price. Off: snapshots are
        # exactly the 1.0.9.2 snapshots. The fill journal (tvbridge.fills) does not need it.
        self.snapshot_fill_prices = config.get('snapshot_fill_prices', False)
        if not isinstance(self.snapshot_fill_prices, bool):
            raise ValueError('snapshot_fill_prices must be true or false')
        age = config.get('reference_price_max_age_s', REFERENCE_PRICE_MAX_AGE_S)
        if type(age) not in (int, float) or not age > 0:
            raise ValueError('reference_price_max_age_s must be a positive number of seconds')
        self.reference_price_max_age_s = float(age)
        self._noted_price = None
        # Risk-reducing housekeeping for staged brackets (see maintain_brackets); only
        # meaningful when bracket orders are enabled.
        self.bracket_protect = config.get('bracket_orders_enabled', False) is True and \
            config.get('bracket_auto_protect', True) is True

    # --- setup and health ------------------------------------------------------

    def resolve(self):
        """Pin the exact account, contract and trading endpoint. Call after the bridge is ready."""
        account = self.state.account_by_name(self.cfg['account_name'])
        if not account:
            raise RuntimeError('configured Tradovate account is not visible in this login')
        expected_id = self.cfg.get('account_id')
        if expected_id is not None and account['id'] != expected_id:
            raise RuntimeError('Tradovate account id does not match the pinned id')
        self.account_id = account['id']
        cid = self.state.contract_id(self.symbol)
        if cid is None:
            r = self.bridge.request('contract/find', query='name=' + self.symbol)
            if not r.get('ok') or not isinstance(r.get('d'), dict) or r['d'].get('name') != self.symbol:
                raise RuntimeError(f'contract {self.symbol} not found: {r.get("code") or r.get("s")}')
            self.state.add_contract(r['d'])
            cid = r['d']['id']
        self.contract_id = cid
        self.trade_url = (self.bridge.status() or {}).get('url')

    def health(self, include_watchdogs=True):
        st = self.bridge.status() or {}
        problems = []
        if st.get('state') != 'authorized':
            problems.append('page_state:' + str(st.get('state')))
        age = st.get('lastRxAgoMs')
        if age is None or age > self.cfg.get('max_silence_ms', 10000):
            problems.append('tradovate_silent')
        if not self.state.is_synced_for(st.get('epoch')):
            problems.append('not_synced_for_current_connection')
        if self.account_id is None or self.contract_id is None:
            problems.append('not_resolved')
        elif not self.state.has_account(self.account_id):
            problems.append('pinned_account_missing_from_sync')
        if self.trade_url and st.get('url') != self.trade_url:
            problems.append('trading_endpoint_changed')
        for check in self.watchdogs if include_watchdogs else ():
            problem = check()
            if problem:
                problems.append(problem)
        return {'ok': not problems, 'problems': problems, 'page': st}

    def entries_disabled(self):
        return self.entries_disabled_reason() is not None

    def entries_disabled_reason(self):
        """Why new entries are paused (kill_switch_state of this account's switch, then the master
        switch), or None when armed. Fails closed: a missing, unusable or unreadable switch pauses."""
        why = kill_switch_state(self.cfg.get('kill_switch_file'))
        if why is None and self.master_kill_active():
            why = 'master_' + (self._master_kill_reason() or KILL_SWITCH_ON)
        return why

    def _master_kill_reason(self):
        path = self.cfg.get('master_kill_switch_file')
        return kill_switch_state(path, writable=False) if path else None

    def master_kill_active(self):
        """1.1.0: `master_kill_switch_file` (another login's main kill switch) is on, or cannot be
        checked (fails closed). Read only: the daemon never creates or removes it; auto-protect still
        writes only `kill_switch_file`. Not configured: not active."""
        return self._master_kill_reason() is not None

    # 1.1.0 multi-login (`peer_logins`): the daemon sets the tvbridge.peers.PeerHedge, asked after the
    # local hedge check passes. None (the default) checks this login only, as before. The leader
    # skips peer accounts with these roles (its followers on other logins).
    peer_hedge = None
    peer_exempt_roles = frozenset()

    # --- dispatch -----------------------------------------------------------------

    def send(self, payload):
        started = time.monotonic()
        try:
            cmd = self._parse(payload)
        except Exception:  # noqa: BLE001 - only for the pre-wait and the refusal record; _send parses again
            cmd = None
        settle_by = self._tp1_prewait(cmd)
        outcome, receipt = self._send(payload, settle_by)
        if outcome == 'rejected' and receipt.get('bridge') == 'refused' and not receipt.get('duplicate'):
            self._record_refusal(payload, cmd, receipt, started)
        return outcome, receipt

    def _tp1_prewait(self, cmd):
        """1.1.2: a tp1_limit waits for its entry to finish BEFORE taking self.lock, so the snapshot
        publisher and other commands are not blocked meanwhile (a multi-second wait under the
        lock would hold back every snapshot). Waits on state only, at most entry_settle_s from now, and
        returns that deadline: the guard under the lock shares it, so the total wait is unchanged.
        It never refuses; the guard decides as before. None for every other command."""
        if not cmd or cmd.get('kind') != 'tp1_limit':
            return None
        deadline = time.time() + self.cfg.get('entry_settle_s', 3.0)
        try:
            if getattr(self.bridge, 'stop_reason', None) or self.journal.get(cmd['command_id']):
                return deadline  # refused or answered from the journal under the lock, no wait needed
            entry = self._entry_for(cmd['trade_id'])
            if entry and entry['outcome'] == 'accepted' and entry.get('broker_order_id') and self.health()['ok']:
                self._wait_entry_terminal(int(entry['broker_order_id']), deadline)
                # The position update can trail the fill and order updates: let it catch up too.
                self._wait_consistent_held(entry, deadline)
        except Exception:  # noqa: BLE001 - the pre-wait only waits; the guard under the lock decides
            pass
        return deadline

    def _record_refusal(self, payload, cmd, receipt, started):
        """1.1.2: log a refusal and append it to `refusals.jsonl` beside commands.jsonl. Never raises.
        commands.jsonl stays as it was: snapshots, fill capture and duplicate detection read it."""
        try:
            extras = payload.get('extras') if isinstance(payload, dict) else None
            extras = extras if isinstance(extras, dict) else {}
            cid = cmd['command_id'] if cmd else extras.get('soup_command_id', extras.get('command_id'))
            tid = cmd['trade_id'] if cmd else extras.get('soup_trade_id', extras.get('trade_id'))
            kind = cmd.get('kind') if cmd else None
            reason = receipt.get('reason')
            ms = int((time.monotonic() - started) * 1000)
            log.warning('refused %s (%s) after %d ms: %s', cid, kind, ms, reason)
            _append(Path(self.journal.dir) / REFUSALS_NAME,
                    {'at': self.clock_ms(), 'command_id': cid if isinstance(cid, str) else None,
                     'trade_id': tid if isinstance(tid, str) else None, 'kind': kind, 'reason': reason})
        except Exception as exc:  # noqa: BLE001 - a refusal record never touches the order path
            log.error('refusal record not written (%s)', type(exc).__name__)

    def _send(self, payload, settle_by=None):
        with self.lock:
            try:
                cmd = self._parse(payload)
            except Refused as e:
                return 'rejected', _refusal(e)
            if settle_by is not None and cmd['kind'] == 'tp1_limit':
                cmd['settle_by'] = settle_by  # 1.1.2: the guard's wait ends with the pre-wait's deadline
            prior = self.journal.get(cmd['command_id'])
            if prior:
                # Never re-send. Report what actually happened the first time.
                return prior['outcome'], dict(prior.get('receipt') or {}, duplicate=True)
            stopping = getattr(self.bridge, 'stop_reason', None)
            if stopping:
                # The daemon is shutting down (tvbridge.daemon.Shutdown): nothing new starts.
                return 'rejected', {'bridge': 'refused', 'reason': 'daemon_stopping:' + str(stopping)}
            health = self.health()
            if not health['ok']:
                return 'rejected', {'bridge': 'refused', 'reason': 'unhealthy:' + ','.join(health['problems'])}
            expected_epoch = health['page']['epoch']
            self.auto_reconcile()
            self.note_market_price()
            try:
                plan = self._guard(cmd)
            except Refused as e:
                return 'rejected', _refusal(e)
            # A guard may wait for an entry fill. Never use its plan on a newer
            # connection, even when the replacement connection is healthy.
            current = self.health()
            if not current['ok'] or current['page'].get('epoch') != expected_epoch:
                return 'rejected', {'bridge': 'refused', 'reason': 'connection_changed_during_order_plan'}
            kind = cmd['kind']
            # liquidateposition has no text field (Tradovate tags the exit order "Exit").
            tag = None if kind in ('cancel', 'flatten_all') else client_tag(cmd['command_id'])
            self.journal.start(cmd['command_id'], trade_id=cmd['trade_id'], kind=kind, side=cmd['side'],
                               quantity=plan.get('quantity'), tag=tag, at=self.clock_ms(),
                               **self._journal_fields(kind, plan),
                               **({'adapter': cmd['adapter']} if cmd.get('adapter') else {}))
            legs = None
            try:
                if plan.get('noop'):
                    outcome, receipt, order_id = 'accepted', {'bridge': 'noop', 'reason': plan['noop']}, None
                elif kind == 'cancel':
                    outcome, receipt, order_id = self._cancel_all(expected_epoch)
                elif kind == 'bracket_entry':
                    outcome, receipt, order_id, legs = self._place_bracket(cmd, plan, tag, expected_epoch)
                elif kind == 'modify_stop':
                    outcome, receipt, order_id = self._modify_stop(plan, tag, expected_epoch)
                elif kind == 'flatten_all':
                    outcome, receipt, order_id = self._flatten_all(plan, expected_epoch)
                else:
                    outcome, receipt, order_id = self._place(plan, tag, expected_epoch)
            except Exception as e:  # noqa: BLE001 - after start, any failure is an unknown outcome
                outcome, receipt, order_id = 'unknown', {'bridge': 'exception_after_start', 'error': type(e).__name__}, None
            # Subclassed journals (operator runners) may predate `legs`; pass it only when set.
            self.journal.finish(cmd['command_id'], outcome, receipt, order_id, **({'legs': legs} if legs else {}))
            if kind == 'bracket_entry' and outcome == 'accepted' and plan.get('tp1_leg') == 'staged':
                receipt = dict(receipt, tp1=self._stage_tp1(cmd, plan, order_id, legs or {}, expected_epoch))
            return outcome, receipt

    @staticmethod
    def _journal_fields(kind, plan):
        """Plan details kept in the start record, so an unknown outcome can be resolved later."""
        keys = {'bracket_entry': ('reference_price', 'stop_price', 'tp1_price', 'tp1_quantity', 'tp1_leg',
                                  'exit_action'),
                'modify_stop': ('order_id', 'stop_price', 'previous', 'clamped'),
                'flatten_all': ('mode', 'close_action', 'baseline_order_id')}.get(kind, ())
        return {('target_order_id' if k == 'order_id' else k): plan[k] for k in keys if k in plan}

    def _parse(self, p):
        """Soup's own payloads (unchanged) or a registered adapter's command (tvbridge.adapters).

        An adapter command is checked for format, identity and freshness, then turned into the same
        order the bridge already knows; every order guard below applies to it exactly as to Soup's."""
        if adapters.is_adapter_command(p):
            try:
                adapter, kind = adapters.validate_command(p, ticker=self.cfg['ticker'], enabled=self.adapters,
                                                          now_ms=self.clock_ms())
            except adapters.AdapterError as e:
                raise Refused(str(e))
            cmd = self._parse_order(adapters.to_bridge_payload(p, kind))
            if cmd['kind'] != kind:
                raise Refused('adapter command shape does not match its order kind')
            cmd.update(adapter=adapter.adapter_id, take_profits=adapter.take_profits)
            return cmd
        if not self.soup_enabled:
            raise Refused('Soup orders are not enabled on this binding (see adapters in the bridge config)')
        return self._parse_order(p)

    def _parse_order(self, p):
        if not isinstance(p, dict) or p.get('ticker') != self.cfg['ticker']:
            raise Refused('ticker is not the pinned ticker')
        extras = p.get('extras')
        if not isinstance(extras, dict):
            raise Refused('extras missing')
        cid, tid = extras.get('soup_command_id'), extras.get('soup_trade_id')
        if not isinstance(cid, str) or not cid or not isinstance(tid, str) or not tid:
            raise Refused('command/trade id missing')
        side = protocol.side_from_trade_id(tid)
        if side is None:
            raise Refused('trade id has no side')
        if 'bridge_kind' in extras:
            # Explicit extended schema only; never inferred from a Soup payload.
            return self._parse_extended(p, extras, {'command_id': cid, 'trade_id': tid, 'side': side})
        action, order_type = p.get('action'), p.get('orderType')
        qty = p.get('quantity')
        if qty is not None and (type(qty) is not int or qty <= 0):
            raise Refused('invalid quantity')
        cmd = {'command_id': cid, 'trade_id': tid, 'side': side, 'quantity': qty}
        if action in ('buy', 'sell') and order_type == 'market':
            if protocol.entry_action(side).lower() != action:
                raise Refused('entry action does not match trade side')
            if qty is None:
                raise Refused('entry quantity required')
            cmd['kind'] = 'entry'
        elif action == 'exit' and order_type == 'limit':
            price = p.get('limitPrice')
            if qty is None or not protocol.on_tick(price, self.cfg['tick_size']):
                raise Refused('limit exit needs quantity and an on-tick limitPrice')
            cmd.update(kind='tp1_limit', price=price)
        elif action == 'exit' and order_type == 'market':
            cmd['kind'] = 'exit_market'
        elif action == 'cancel' and set(p) == {'ticker', 'extras', 'action'}:
            cmd['kind'] = 'cancel'
        else:
            raise Refused('unsupported action/orderType')
        if action == 'exit' and p.get('cancel') is not False:
            raise Refused('exit must carry cancel:false')
        return cmd

    def _parse_extended(self, p, extras, base):
        kind = extras.get('bridge_kind')
        if kind not in EXTENDED_KINDS:
            raise Refused('unsupported bridge_kind')
        flag = _ENABLE_FLAG[kind]
        if self.cfg.get(flag) is not True:
            raise Refused(f'{kind} is disabled; set {flag} in the bridge config')
        if set(extras) - _EXTRA_KEYS[kind]:
            raise Refused('unexpected extras for ' + kind)
        tick = self.cfg['tick_size']
        action = p.get('action')
        cmd = dict(base, kind=kind, quantity=None)
        if kind == 'bracket_entry':
            if set(p) != {'ticker', 'extras', 'action', 'orderType', 'quantity'}:
                raise Refused('bracket_entry payload has missing or unexpected fields')
            if p.get('orderType') != 'market' or action != protocol.entry_action(base['side']).lower():
                raise Refused('bracket_entry must be a market entry on the trade side')
            qty = p.get('quantity')
            if type(qty) is not int or qty <= 0:
                raise Refused('invalid quantity')
            ref, stop = extras.get('reference_price'), extras.get('stop_price')
            if not protocol.on_tick(ref, tick) or not protocol.on_tick(stop, tick):
                raise Refused('bracket_entry needs an on-tick reference_price and stop_price')
            tp, tp_qty = extras.get('tp1_price'), extras.get('tp1_quantity')
            if tp is None:
                if tp_qty is not None:
                    raise Refused('tp1_quantity needs tp1_price')
            else:
                if not protocol.on_tick(tp, tick):
                    raise Refused('tp1_price must be on-tick')
                tp_qty = qty if tp_qty is None else tp_qty
                if type(tp_qty) is not int or not 0 < tp_qty <= qty:
                    raise Refused('tp1_quantity must be between 1 and the entry quantity')
            cmd.update(quantity=qty, reference_price=ref, stop_price=stop, tp1_price=tp, tp1_quantity=tp_qty)
        elif kind == 'modify_stop':
            if set(p) != {'ticker', 'extras', 'action'} or action != 'modify_stop':
                raise Refused('modify_stop payload must be {ticker, extras, action: "modify_stop"}')
            price, qty = extras.get('stop_price'), extras.get('stop_quantity')
            if price is None and qty is None:
                raise Refused('modify_stop needs stop_price and/or stop_quantity')
            if price is not None and not protocol.on_tick(price, tick):
                raise Refused('stop_price must be on-tick')
            if qty is not None and (type(qty) is not int or qty <= 0):
                raise Refused('invalid stop_quantity')
            cmd.update(stop_price=price, stop_quantity=qty)
        else:
            if set(p) != {'ticker', 'extras', 'action'} or action != 'flatten_all':
                raise Refused('flatten_all payload must be {ticker, extras, action: "flatten_all"}')
        return cmd

    def _entry_for(self, trade_id):
        for c in self.journal.for_trade(trade_id):
            if c['kind'] == 'bracket_entry' and c['outcome'] == 'rejected':
                continue  # definitely not placed; the signal may fall back to a plain entry
            if c['kind'] in ENTRY_KINDS:
                return c
        return None

    def _own_stop(self, entry):
        """Order id of the trade's bracket stop leg, or None (plain entries have none)."""
        if not entry or entry.get('kind') != 'bracket_entry' or entry.get('outcome') != 'accepted':
            return None
        leg = (entry.get('legs') or {}).get('stop')
        return int(leg) if leg else None

    def _unseen_accepted_orders(self):
        """Accepted order ids from the current CME session that Tradovate has not reported yet.

        Orders are Day orders, so one from an earlier session has expired and cannot be
        working. Tradovate's sync may omit earlier days; counting those would block all
        trading after a session rollover. Records without a dispatch time still count.
        """
        start = protocol.session_start_ms(self.clock_ms())
        return [oid for oid, at in self.journal.accepted_orders()
                if (at is None or at >= start) and self.state.order(int(oid)) is None]

    def _entry_reject_reason(self, order_id, status):
        """1.0.9.5.2: {'entry_reject_reason': text} for an entry Tradovate rejected, when it said why; else {}."""
        text = self.reject_reason_of(order_id) if status == 'Rejected' else None
        return {'entry_reject_reason': text} if text else {}

    def reject_reason_of(self, order_id):
        """1.0.9.5.2: Tradovate's rejection text for an order, or None. Never raises: observability
        must not change an order decision, a receipt's outcome or a snapshot."""
        try:
            text = self.state.reject_reason(int(order_id))
        except Exception:  # noqa: BLE001
            return None
        return text if isinstance(text, str) and text else None

    def _wait_entry_terminal(self, order_id, deadline=None):
        """Wait briefly for a market entry to finish; returns (ordStatus, filled_qty).
        `deadline` (time.time()) defaults to entry_settle_s from now."""
        if deadline is None:
            deadline = time.time() + self.cfg.get('entry_settle_s', 3.0)
        while True:
            with self.state.lock:
                order = self.state.order(order_id)
                status = order.get('ordStatus') if order else None
                filled = self.state.filled_qty(order_id)
            if status and not protocol.is_open(status):
                return status, filled
            if time.time() >= deadline:
                return status, filled
            time.sleep(0.05)

    def _guard(self, cmd):
        """Return the order plan or raise Refused. Nothing has been sent yet."""
        kind, side = cmd['kind'], cmd['side']
        if kind == 'cancel':
            return {}
        if kind == 'entry':
            self._entry_preconditions(cmd)
            return {'action': protocol.entry_action(side), 'orderType': 'Market', 'quantity': cmd['quantity']}
        if kind == 'bracket_entry':
            return self._guard_bracket(cmd)
        if kind == 'modify_stop':
            return self._guard_modify(cmd)
        if kind == 'flatten_all':
            return self._guard_flatten()

        entry = self._entry_for(cmd['trade_id'])
        if not entry or entry['outcome'] != 'accepted':
            raise Refused('no accepted entry for this trade')
        if kind == 'tp1_limit':
            # Only rest a limit exit against contracts that actually filled; otherwise it could open a new position.
            # 1.1.2: send() already waited outside the lock; this wait shares its deadline (cmd['settle_by']).
            oid, settle_by = int(entry['broker_order_id']), cmd.get('settle_by')
            status, filled = self._wait_entry_terminal(oid) if settle_by is None else \
                self._wait_entry_terminal(oid, settle_by)
            if status != 'Filled' or filled != entry['quantity']:
                raise Refused(f'entry not completely filled ({status}, {filled}/{entry["quantity"]})',
                              **self._entry_reject_reason(entry['broker_order_id'], status))
            with self.state.lock:
                net = self.state.net_position(self.account_id, self.contract_id)
                open_orders = self.state.open_orders(self.account_id, self.contract_id)
            held = net if side == 'long' else -net
            own_stop = self._own_stop(entry)
            allowed = {own_stop} if own_stop else set()
            slots = cmd.get('take_profits', 1)
            if slots > 1:
                # an adapter whose profile declares more than TP1 may rest up to `slots`
                # take-profit limits of this trade; together they never exceed the live position.
                resting = self._resting_take_profits(cmd['trade_id'])
                if len(resting) >= slots:
                    raise Refused(f'the trade already has {len(resting)} resting take-profit(s); '
                                  f'its profile allows {slots}')
                allowed |= set(resting)
                held -= sum(resting.values())
            if cmd['quantity'] > held or set(open_orders) - allowed:
                raise Refused('limit exit exceeds live position or other orders are working')
            # Garbage-price guard: a limit far from the actual entry fill is an input error
            # (a sell limit far below the market, e.g. 40 on a ~7800 contract, fills at once at market).
            fill = self.state.avg_fill_price(int(entry['broker_order_id']))
            band = float(self.cfg.get('max_limit_deviation_pct', 5.0))
            if fill is None:
                raise Refused('entry fill price unknown; limit exit not sent')
            if abs(cmd['price'] - fill) > fill * band / 100.0:
                raise Refused(f'limit price {cmd["price"]} is more than {band}% from the entry fill {fill}')
            return {'action': protocol.exit_action(side), 'orderType': 'Limit', 'quantity': cmd['quantity'],
                    'price': cmd['price']}
        # Market exit: must reduce the live position toward flat, never through it.
        if self._unseen_accepted_orders():
            raise Refused('an accepted order has not been reported by Tradovate yet')
        own_stop = self._own_stop(entry)
        with self.state.lock:
            net = self.state.net_position(self.account_id, self.contract_id)
            open_orders = self.state.open_orders(self.account_id, self.contract_id)
            stop_left = None
            if own_stop is not None and own_stop in open_orders:
                stop_qty = self.state.order_qty(own_stop)
                stop_left = None if stop_qty is None else stop_qty - self.state.filled_qty(own_stop)
        if open_orders and (own_stop is None or set(open_orders) != {own_stop}):
            raise Refused('working orders on the contract; cancel barrier must complete first')
        held = net if side == 'long' else -net
        if held <= 0:
            raise Refused('no position on the trade side to exit')
        qty = cmd['quantity'] if cmd['quantity'] is not None else held
        if qty > held:
            raise Refused('exit quantity exceeds live position')
        if open_orders:
            # Only the trade's own resting stop is working: it must fit beside this exit.
            if cmd['quantity'] is None or stop_left is None or stop_left + qty > held:
                raise Refused('market exit would let the protective stop over-fill; shrink the stop first')
        return {'action': protocol.exit_action(side), 'orderType': 'Market', 'quantity': qty}

    def _resting_take_profits(self, trade_id):
        """{order id: unfilled quantity} of this trade's accepted take-profit limits still working."""
        out = {}
        with self.state.lock:
            for c in self.journal.for_trade(trade_id):
                if c['kind'] not in ('tp1_limit', 'bracket_tp1') or c['outcome'] != 'accepted' \
                        or not c.get('broker_order_id'):
                    continue
                oid = int(c['broker_order_id'])
                order = self.state.order(oid)
                if order and protocol.is_open(order.get('ordStatus')):
                    left = self._remaining_qty(order)
                    if left is None:
                        raise Refused('a resting take-profit has no reported size')
                    out[oid] = left
        return out

    def _entry_preconditions(self, cmd):
        """Guards shared by every entry kind. Raises Refused; sends nothing."""
        why = self.entries_disabled_reason()
        if why in (KILL_SWITCH_ON, 'master_' + KILL_SWITCH_ON, 'main_' + KILL_SWITCH_ON):
            raise Refused('entries disabled by kill switch')
        if why:
            raise Refused('entries disabled by kill switch: ' + why)
        # No size cap: the alert's quantity is used as sent (format checked at intake: a positive
        # whole number). Checked again here so no entry path can send zero, a fraction or a bool.
        if type(cmd['quantity']) is not int or cmd['quantity'] <= 0:
            raise Refused('entry quantity must be a positive whole number')
        if self.journal.unresolved():
            raise Refused('an earlier command has an unknown outcome; reconcile it first')
        if self._unseen_accepted_orders():
            raise Refused('an accepted order has not been reported by Tradovate yet')
        with self.state.lock:
            net = self.state.net_position(self.account_id, self.contract_id)
            open_orders = self.state.open_orders(self.account_id, self.contract_id)
        if net != 0 or open_orders:
            raise Refused('entry requires a flat contract with no working orders')
        if self._entry_for(cmd['trade_id']):
            raise Refused('trade already has an entry')
        if self.hedge_guard:
            conflict = self._hedge_conflict(cmd['side'])
            if not conflict and self.peer_hedge is not None:
                conflict = self.peer_hedge.conflict(cmd['side'], self.hedge_members, self.peer_exempt_roles)
            if conflict:
                raise Refused(conflict)

    # --- bracket guards ------------------------------------------------------------------

    def _band(self):
        return float(self.cfg.get('max_limit_deviation_pct', 5.0))

    def _guard_bracket(self, cmd):
        self._entry_preconditions(cmd)
        side = cmd['side']
        sign = 1 if side == 'long' else -1
        ref, stop, tp = cmd['reference_price'], cmd['stop_price'], cmd['tp1_price']
        band = self._band()
        if sign * (ref - stop) <= 0:
            raise Refused('stop %s must be %s the entry reference %s' % (stop, 'below' if sign > 0 else 'above', ref))
        if abs(stop - ref) > ref * band / 100.0:
            raise Refused(f'stop {stop} is more than {band}% from the entry reference {ref}')
        if tp is not None:
            if sign * (tp - ref) <= 0:
                raise Refused('tp1 %s must be %s the entry reference %s' % (tp, 'above' if sign > 0 else 'below', ref))
            if abs(tp - ref) > ref * band / 100.0:
                raise Refused(f'tp1 {tp} is more than {band}% from the entry reference {ref}')
        # Garbage-reference guard: the reference must be near the market Tradovate reported last.
        # After a restart with no fill in the sync, the last price this account saw (kept on disk, at
        # most reference_price_max_age_s old); with neither, the bracket is refused (fails closed) and
        # Soup sends its plain entry for the signal instead.
        last, source = self.market_price()
        if last is None:
            raise Refused('reference_price cannot be checked: no recent fill price is known on the contract '
                          '(for example right after a restart); bracket entry refused')
        if abs(ref - last) > last * band / 100.0:
            raise Refused(f'reference_price {ref} is more than {band}% from the {source} {last} on the contract')
        tp_leg = None if tp is None else ('oso' if cmd['tp1_quantity'] == cmd['quantity'] else 'staged')
        return {'action': protocol.entry_action(side), 'exit_action': protocol.exit_action(side),
                'orderType': 'Market', 'quantity': cmd['quantity'], 'reference_price': ref, 'stop_price': stop,
                'tp1_price': tp, 'tp1_quantity': cmd['tp1_quantity'], 'tp1_leg': tp_leg}

    # --- market price for the reference check --------------------------------------------

    def _market_price_path(self):
        d = getattr(self.journal, 'dir', None)
        return None if d is None else Path(d) / MARKET_PRICE_NAME

    def note_market_price(self):
        """Keep the latest fill price on the pinned contract on disk (only when it changed). Never raises."""
        try:
            fill = self.state.last_fill(self.contract_id) if self.contract_id is not None else None
            path = self._market_price_path()
            if fill is None or path is None:
                return
            key = (fill.get('id'), fill['price'])
            if key == self._noted_price:
                return
            at_ms = _iso_ms(fill.get('timestamp')) or self.clock_ms()
            data = json.dumps({'symbol': self.symbol, 'price': float(fill['price']), 'at_ms': at_ms})
            tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, data.encode())
            finally:
                os.close(fd)
            os.replace(tmp, path)
            self._noted_price = key
        except Exception as exc:  # noqa: BLE001 - bookkeeping only; the check fails closed without it
            log.warning('last market price not saved (%s)', type(exc).__name__)

    def market_price(self):
        """(price, source) for the reference check: the latest fill in the sync, else the saved one when
        it is for this contract and at most reference_price_max_age_s old; (None, None) if neither."""
        last = self.state.last_fill_price(self.contract_id)
        if last is not None:
            self.note_market_price()
            return last, 'latest fill'
        path = self._market_price_path()
        try:
            saved = json.loads(path.read_text()) if path is not None else None
        except (OSError, ValueError):
            saved = None
        if not isinstance(saved, dict) or saved.get('symbol') != self.symbol:
            return None, None
        price, at_ms = saved.get('price'), saved.get('at_ms')
        if type(price) not in (int, float) or not price > 0 or type(at_ms) not in (int, float):
            return None, None
        age_s = (self.clock_ms() - at_ms) / 1000.0
        if not 0 <= age_s <= self.reference_price_max_age_s:
            return None, None
        return float(price), 'last known fill'

    def _bracket_entry(self, trade_id):
        entry = self._entry_for(trade_id)
        if not entry or entry['kind'] != 'bracket_entry' or entry['outcome'] != 'accepted':
            return None
        return entry

    def _tp_orders(self, entry):
        """Order ids of the trade's take-profit: the OSO tp1 leg and/or the staged TP1 limit."""
        ids = []
        if (entry.get('legs') or {}).get('tp1'):
            ids.append(int(entry['legs']['tp1']))
        staged = self.journal.get(leg_command_id(entry['command_id'], 'tp1'))
        if staged and staged['outcome'] == 'accepted' and staged.get('broker_order_id'):
            ids.append(int(staged['broker_order_id']))
        return ids

    # 1.0.9.5: the leader may be closed partly by hand; see _closed_by_hand. Followers: False.
    HAND_REDUCTIONS = True

    def _consistent_held(self, entry):
        """Position on the trade side when this trade's own fills explain it, else None.

        A fill, its order update and the position update arrive as separate events, so the
        position alone can lag a TP fill. A foreign fill on the contract also returns None
        (fail closed: a human decides), except 1.0.9.5 on the leader: contracts closed by hand
        (_closed_by_hand) also explain it, so the stop can follow the smaller position.
        """
        sign = 1 if entry['side'] == 'long' else -1
        entry_id = int(entry['broker_order_id'])
        with self.state.lock:
            held = sign * self.state.net_position(self.account_id, self.contract_id)
            exits = {int(v) for v in (entry.get('legs') or {}).values()} | \
                {int(c['broker_order_id']) for c in self.journal.for_trade(entry['trade_id'])
                 if c['outcome'] == 'accepted' and c.get('broker_order_id')}
            exits.discard(entry_id)
            own = self.state.filled_qty(entry_id) - sum(self.state.filled_qty(oid) for oid in exits)
            if own == held:
                return held
            hand = self._closed_by_hand(entry, exits) if self.HAND_REDUCTIONS else None
            if hand and held >= 0 and own - hand == held:
                return held
            return None

    def _wait_consistent_held(self, entry, deadline):
        """Wait until this trade's fills explain the position (see _consistent_held), at most until
        `deadline` (time.time()). Never refuses; the caller's own check decides."""
        while self._consistent_held(entry) is None and time.time() < deadline:
            time.sleep(0.05)

    def _closed_by_hand(self, entry, exits):
        """Contracts of this trade closed outside the bridge (e.g. by hand in Tradovate), or None.

        Orders on the pinned account and contract placed after the entry and unknown to the journal.
        Only closing fills count. None (fail closed) when such an order is still working, when one
        added to the position, or while a command of the bridge is unresolved. Caller holds the lock.
        """
        if self.journal.unresolved():
            return None
        entry_id = int(entry['broker_order_id'])
        known = {int(oid) for oid in self.journal.accepted_order_ids()} | exits | {entry_id}
        closing = protocol.exit_action(entry['side'])
        total = 0
        for oid, order in self.state.entities['order'].items():
            if (order.get('accountId') != self.account_id or order.get('contractId') != self.contract_id
                    or oid <= entry_id or oid in known):
                continue
            if protocol.is_open(order.get('ordStatus')):
                return None
            filled = self.state.filled_qty(oid)
            if filled and order.get('action') != closing:
                return None
            total += filled
        return total

    def _guard_modify(self, cmd):
        entry = self._bracket_entry(cmd['trade_id'])
        if not entry or not (entry.get('legs') or {}).get('stop'):
            raise Refused('no accepted bracket entry with a protective stop for this trade')
        stop_id = int(entry['legs']['stop'])
        sign = 1 if cmd['side'] == 'long' else -1
        deadline = time.time() + self.cfg.get('entry_settle_s', 3.0)
        while self._consistent_held(entry) is None:
            if time.time() >= deadline:
                raise Refused('position does not match this trade\'s fills (events pending or a foreign fill)')
            time.sleep(0.05)
        with self.state.lock:
            order = self.state.order(stop_id)
            version = self.state.order_version(stop_id)
            stop_filled = self.state.filled_qty(stop_id)
            net = self.state.net_position(self.account_id, self.contract_id)
            tp_prices = []
            for oid in self._tp_orders(entry):
                o, v = self.state.order(oid), self.state.order_version(oid)
                if o and protocol.is_open(o.get('ordStatus')) and v and v.get('price') is not None:
                    tp_prices.append(v['price'])
            entry_fill = self.state.avg_fill_price(int(entry['broker_order_id']))
        if not order or order.get('ordStatus') != 'Working' or not version or version.get('orderType') != 'Stop':
            raise Refused('no working protective stop for this trade')
        if stop_filled:
            raise Refused('the protective stop is partially filled; not modified')
        held = sign * net
        if held <= 0:
            raise Refused('no position on the trade side to protect')
        current_qty, current_price = version.get('orderQty'), version.get('stopPrice')
        if type(current_qty) is not int or not protocol.on_tick(current_price, self.cfg['tick_size']):
            raise Refused('current stop quantity or price not reported by Tradovate')
        qty = cmd['stop_quantity'] if cmd['stop_quantity'] is not None else min(current_qty, held)
        if qty > held:
            raise Refused(f'stop quantity {qty} exceeds the live position {held}')
        price = cmd['stop_price'] if cmd['stop_price'] is not None else current_price
        for tp in tp_prices:
            if sign * (tp - price) <= 0:
                raise Refused(f'stop {price} must stay on the protective side of the working take-profit {tp}')
        if entry_fill is None:
            raise Refused('entry fill price unknown; stop not modified')
        band = self._band()
        if abs(price - entry_fill) > entry_fill * band / 100.0:
            raise Refused(f'stop {price} is more than {band}% from the entry fill {entry_fill}')
        plan = {'order_id': stop_id, 'quantity': qty, 'stop_price': price,
                'clamped': cmd['stop_quantity'] is None and qty < current_qty,
                'previous': {'quantity': current_qty, 'stop_price': current_price, 'version_id': version['id']}}
        if (qty, price) == (current_qty, current_price):
            plan['noop'] = 'stop already at the requested price and quantity'
        return plan

    def _guard_flatten(self):
        """flatten_all is an exit: never blocked by the kill switch or unresolved entries."""
        with self.state.lock:
            net = self.state.net_position(self.account_id, self.contract_id)
            open_orders = self.state.open_orders(self.account_id, self.contract_id)
            # 1.0.9.1: this account's own highest order id. Order ids are allocated per account, so
            # the login-wide maximum (1.0.9) hid this account's liquidation order whenever another
            # account of the same login had higher ids.
            baseline = self.state.max_order_id(self.account_id)
        unseen = self._unseen_accepted_orders()
        if net:
            return {'mode': 'liquidate', 'quantity': abs(net), 'close_action': 'Sell' if net > 0 else 'Buy',
                    'baseline_order_id': baseline}
        if open_orders or unseen:
            # Flat with orders working: cancel them one by one (verified cancelorder body);
            # liquidateposition on a flat contract is not verified.
            return {'mode': 'cancel'}
        return {'mode': 'noop', 'noop': 'already flat with nothing working'}

    # --- anti-hedge guard (entries only; exits and cancels only reduce risk) -----------

    def _remaining_qty(self, order):
        qty = self.state.order_qty(order['id'])
        if qty is None and type(order.get('orderQty')) is int:
            qty = order['orderQty']
        return None if qty is None else qty - self.state.filled_qty(order['id'])

    def _contract_root(self, contract_id, synced_name):
        """Product root for a contract id, resolving unknown names with Tradovate; None if unknown."""
        name = synced_name or self.contract_names.get(contract_id)
        if name is None and contract_id == self.contract_id:
            name = self.symbol
        if name is None and type(contract_id) is int:
            try:
                r = self.bridge.request('contract/item', query=f'id={contract_id}',
                                        timeout=self.cfg.get('order_timeout_s', 5))
            except Exception:  # noqa: BLE001 - an unresolved contract refuses the entry
                r = {}
            d = r.get('d') if isinstance(r, dict) and r.get('ok') else None
            if isinstance(d, dict) and d.get('id') == contract_id and isinstance(d.get('name'), str):
                name = d['name']
        if name:
            self.contract_names[contract_id] = name
        return protocol.product_root(name)

    def _hedge_conflict(self, side):
        """Refusal reason if a `side` entry would oppose exposure visible in this login, else None.

        Looks at every account and contract in the synced state except the pinned
        account's own contract (the flat requirement covers that). The reason carries
        counts, sides and product roots only, never another account's name or id.
        """
        opp = -1 if side == 'long' else 1
        opposite = 'short' if side == 'long' else 'long'
        own_pair = (self.account_id, self.contract_id)
        candidates = []  # (contract_id, account_id, kind): kind is position | order | unclassified
        with self.state.lock:
            nets = self.state.net_positions()
            for (acct, cid), net in nets.items():
                if (acct, cid) != own_pair and net * opp > 0 and acct not in self.hedge_exempt_accounts:
                    candidates.append((cid, acct, 'position'))
            for order in self.state.working_orders():
                acct, cid = order.get('accountId'), order.get('contractId')
                if (acct, cid) == own_pair or acct in self.hedge_exempt_accounts:
                    continue
                direction = {'Buy': 1, 'Sell': -1}.get(order.get('action'))
                if direction is None:
                    candidates.append((cid, acct, 'unclassified'))
                    continue
                if direction != opp:
                    continue
                held = -opp * nets.get((acct, cid), 0)  # contracts held on the entry's side
                if held <= 0:
                    candidates.append((cid, acct, 'order'))
                    continue
                remaining = self._remaining_qty(order)
                if remaining is None:
                    candidates.append((cid, acct, 'unclassified'))
                elif remaining > held:
                    candidates.append((cid, acct, 'order'))  # would reverse through flat
            names = {cid: self.state.contract_name(cid) for cid, _, _ in candidates}
        if not candidates:
            return None
        roots = {cid: self._contract_root(cid, name) for cid, name in names.items()}
        head = f'hedge guard: {side} {self.root} entry refused'
        unknown = sum(1 for root in roots.values() if root is None)
        if unknown:
            return (f'{head}; {unknown} contract(s) with possible {opposite} exposure in this login '
                    f'could not be mapped to a product root')
        hits = [(acct, kind, roots[cid]) for cid, acct, kind in candidates if roots[cid] in self.hedge_members]
        if not hits:
            return None
        parts = []
        for own in (True, False):
            mine = [h for h in hits if (h[0] == self.account_id) == own]
            if not mine:
                continue
            counts = {k: sum(1 for h in mine if h[1] == k) for k in ('position', 'order', 'unclassified')}
            what = []
            if counts['position']:
                what.append(f'{counts["position"]} {opposite} position(s)')
            if counts['order']:
                what.append(f'{counts["order"]} working order(s) that could open or add to a {opposite} position')
            if counts['unclassified']:
                what.append(f'{counts["unclassified"]} working order(s) with unknown side or size')
            where = 'this account' if own else f'{len({h[0] for h in mine})} other account(s)'
            products = '/'.join(sorted({h[2] for h in mine}))
            parts.append(f'{where}: {", ".join(what)} in {products}')
        return f'{head}; opposite ({opposite}) exposure in hedge group {"/".join(self.hedge_members)}: ' + \
            '; '.join(parts)

    def _place(self, plan, tag, expected_epoch):
        # Same body shape the Tradovate web trader sends, plus the
        # CME automated-order flag. `text` carries our client tag for correlation.
        body = {
            'accountId': self.account_id,
            'action': plan['action'],
            'symbol': self.symbol,
            'orderQty': plan['quantity'],
            'orderType': plan['orderType'],
            'timeInForce': self.cfg.get('time_in_force', 'Day'),
            'text': tag,
            'isAutomated': True,
        }
        if plan['orderType'] == 'Limit':
            body['price'] = plan['price']
        r = self.bridge.request('order/placeorder', body, timeout=self.cfg.get('order_timeout_s', 5),
                                expected_epoch=expected_epoch)
        d = r.get('d') if isinstance(r.get('d'), dict) else {}
        if r.get('ok') and isinstance(d.get('orderId'), int):
            oid = str(d['orderId'])
            return 'accepted', {'broker_order_id': oid, 'id': oid}, oid
        if not r.get('sent'):
            return 'rejected', {'bridge': 'not_sent', 'code': str(r.get('code'))[:120]}, None
        failure = d.get('failureReason') if r.get('ok') else None
        detail = {'code': str(r.get('code'))[:120], 's': r.get('s'),
                  'failureReason': str(failure)[:120] if failure else None,
                  'failureText': str(d.get('failureText', ''))[:240] if failure else None}
        # The reply is not a clean success. Look for our tagged order before deciding.
        found = self._wait_for_tag(tag, self.cfg.get('tag_wait_s', 1.5))
        if found is not None:
            oid = str(found)
            return 'accepted', {'broker_order_id': oid, 'id': oid, 'matched_by_tag': True, **detail}, oid
        if failure or r.get('s') in protocol.DEFINITE_REFUSAL_STATUSES:
            # Tradovate answered with a refusal and no tagged order appeared.
            # (Recon: definite rejections arrive as failureReason "UnknownReason" with the cause in failureText.)
            return 'rejected', _with_reject_reason(detail, failure, d.get('failureText')), None
        return 'unknown', dict(detail, transport='uncertain'), None

    def _wait_for_tag(self, tag, seconds):
        deadline = time.time() + seconds
        while True:
            found = self.state.order_id_for_tag(tag)
            if found is not None or time.time() >= deadline:
                return found
            time.sleep(0.05)

    # --- bracket orders (opt-in) ----------------------------------------------------------

    def _known_order_ids(self):
        return {int(oid) for oid in self.journal.accepted_order_ids()}

    def _leg_by_structure(self, role, rec, exclude):
        """A working/suspended leg on the pinned contract that matches the planned leg exactly.

        Fallback when neither the reply nor the leg's `text` tag identifies it. Safe because a
        bracket entry requires a flat contract with nothing working, and entries stay blocked
        while it is unresolved.
        """
        want_type, key = ('Stop', 'stopPrice') if role == 'stop' else ('Limit', 'price')
        price = rec['stop_price'] if role == 'stop' else rec['tp1_price']
        with self.state.lock:
            for oid in sorted(self.state.open_orders(self.account_id, self.contract_id)):
                if oid in exclude:
                    continue
                o, v = self.state.order(oid), self.state.order_version(oid)
                if o and v and o.get('action') == rec['exit_action'] and v.get('orderType') == want_type \
                        and v.get(key) == price:
                    return oid
        return None

    def _find_legs(self, rec, roles, entry_id, how=None):
        """Leg ids by their `text` tag, else by structure. `how` records which identified each."""
        found = {}
        exclude = self._known_order_ids() | {int(entry_id)}
        for role in roles:
            oid, source = self.state.order_id_for_tag(client_tag(leg_command_id(rec['command_id'], role))), 'tag'
            if oid is None:
                oid, source = self._leg_by_structure(role, rec, exclude), 'structure'
            if oid is not None:
                found[role] = oid
                exclude.add(oid)
                if how is not None:
                    how[role] = source
        return found

    def _place_bracket(self, cmd, plan, tag, expected_epoch):
        """order/placeoso: market entry, bracket1 = protective stop, bracket2 = TP1 when M == N.

        Documentation-based (Tradovate API PlaceOSO): legs have no orderQty and inherit the
        entry's; the reply is {orderId, oso1Id, oso2Id}. Unverified until a bracket test on a demo account.
        """
        cid, tif = cmd['command_id'], self.cfg.get('time_in_force', 'Day')
        body = {
            'accountId': self.account_id, 'action': plan['action'], 'symbol': self.symbol,
            'orderQty': plan['quantity'], 'orderType': 'Market', 'timeInForce': tif, 'text': tag,
            'isAutomated': True,
            'bracket1': {'action': plan['exit_action'], 'orderType': 'Stop', 'stopPrice': plan['stop_price'],
                         'timeInForce': tif, 'text': client_tag(leg_command_id(cid, 'stop'))},
        }
        roles = ['stop']
        if plan['tp1_leg'] == 'oso':
            body['bracket2'] = {'action': plan['exit_action'], 'orderType': 'Limit', 'price': plan['tp1_price'],
                                'timeInForce': tif, 'text': client_tag(leg_command_id(cid, 'tp1'))}
            roles.append('tp1')
        r = self.bridge.request('order/placeoso', body, timeout=self.cfg.get('order_timeout_s', 5),
                                expected_epoch=expected_epoch)
        d = r.get('d') if isinstance(r.get('d'), dict) else {}
        entry_id = d.get('orderId') if r.get('ok') and type(d.get('orderId')) is int else None
        legs, how = {}, {}
        if entry_id is not None:
            for role, key in zip(roles, ('oso1Id', 'oso2Id')):
                if type(d.get(key)) is int:
                    legs[role], how[role] = d[key], 'reply'
        if entry_id is None and not r.get('sent'):
            return 'rejected', {'bridge': 'not_sent', 'code': str(r.get('code'))[:120]}, None, None
        failure = d.get('failureReason') if r.get('ok') else None
        detail = {'code': str(r.get('code'))[:120], 's': r.get('s'),
                  'failureReason': str(failure)[:120] if failure else None,
                  'failureText': str(d.get('failureText', ''))[:240] if failure else None}
        matched = None
        if entry_id is None:
            # The reply is not a clean success. Look for our tagged entry before deciding.
            found = self._wait_for_tag(tag, self.cfg.get('tag_wait_s', 1.5))
            if found is None:
                if failure or r.get('s') in protocol.DEFINITE_REFUSAL_STATUSES:
                    return 'rejected', _with_reject_reason(detail, failure, d.get('failureText')), None, None
                return 'unknown', dict(detail, transport='uncertain'), None, None
            entry_id, matched = found, 'tag'
        deadline = time.time() + self.cfg.get('tag_wait_s', 1.5)
        while len(legs) < len(roles):
            legs.update(self._find_legs(dict(plan, command_id=cid), [x for x in roles if x not in legs], entry_id, how))
            if len(legs) == len(roles) or time.time() >= deadline:
                break
            time.sleep(0.05)
        oid = str(entry_id)
        # legs_from: which evidence identified each leg (reply oso ids, leg text tag, or structure).
        receipt = {'broker_order_id': oid, 'id': oid, 'legs': {k: str(v) for k, v in legs.items()}, 'legs_from': how}
        if matched:
            receipt.update(matched_by_tag=True, **detail)
        if len(legs) < len(roles):
            order = self.state.order(entry_id) or {}
            if order.get('ordStatus') == 'Rejected' and not self.state.filled_qty(entry_id):
                # Nothing filled, so nothing to protect.
                why = self.reject_reason_of(entry_id)  # 1.0.9.5.2: additive, only when known
                return 'accepted', dict(receipt, legs_missing=[x for x in roles if x not in legs],
                                        entry_status='Rejected', **({'reject_reason': why} if why else {})), \
                    oid, legs or None
            return 'unknown', dict(receipt, legs_unresolved=[x for x in roles if x not in legs],
                                   transport='bracket_legs_unresolved'), oid, None
        return 'accepted', receipt, oid, legs

    def _stage_tp1(self, cmd, plan, entry_oid, legs, expected_epoch):
        """TP1 for M < N contracts: a separate limit once the entry has completely filled."""
        side, qty = cmd['side'], plan['tp1_quantity']
        status, filled = self._wait_entry_terminal(int(entry_oid))
        reason, extra = None, {}
        if status != 'Filled' or filled != plan['quantity']:
            reason = f'entry not completely filled ({status}, {filled}/{plan["quantity"]})'
            extra = self._entry_reject_reason(entry_oid, status)  # 1.0.9.5.2
        else:
            # The fill, the order update and the position update arrive as separate events.
            sign = 1 if side == 'long' else -1
            deadline = time.time() + self.cfg.get('entry_settle_s', 3.0)
            while sign * self.state.net_position(self.account_id, self.contract_id) != filled \
                    and time.time() < deadline:
                time.sleep(0.05)
            with self.state.lock:
                net = self.state.net_position(self.account_id, self.contract_id)
                others = set(self.state.open_orders(self.account_id, self.contract_id)) - \
                    {int(v) for v in legs.values()}
                fill = self.state.avg_fill_price(int(entry_oid))
            held = net if side == 'long' else -net
            band = self._band()
            if qty > held:
                reason = f'tp1 quantity {qty} exceeds the live position {held}'
            elif others:
                reason = 'orders other than the bracket stop are working'
            elif fill is None:
                reason = 'entry fill price unknown'
            elif abs(plan['tp1_price'] - fill) > fill * band / 100.0:
                reason = f'tp1 {plan["tp1_price"]} is more than {band}% from the entry fill {fill}'
        current = self.health()
        if reason is None and (not current['ok'] or current['page'].get('epoch') != expected_epoch):
            reason = 'connection_changed_during_order_plan'
        if reason:
            log.warning('bracket %s: staged TP1 not sent: %s', cmd['command_id'], reason)
            return dict({'sent': False, 'reason': reason}, **extra)
        sub = leg_command_id(cmd['command_id'], 'tp1')
        tag = client_tag(sub)
        self.journal.start(sub, trade_id=cmd['trade_id'], kind='bracket_tp1', side=side, quantity=qty, tag=tag,
                           parent=cmd['command_id'], at=self.clock_ms())
        try:
            outcome, receipt, oid = self._place({'action': plan['exit_action'], 'orderType': 'Limit',
                                                 'quantity': qty, 'price': plan['tp1_price']}, tag, expected_epoch)
        except Exception as e:  # noqa: BLE001 - after start, any failure is an unknown outcome
            outcome, receipt, oid = 'unknown', {'bridge': 'exception_after_start', 'error': type(e).__name__}, None
        self.journal.finish(sub, outcome, receipt, oid)
        return dict(receipt, sent=True, command_id=sub, outcome=outcome)

    def _modify_applied(self, order_id, tag, quantity, stop_price, previous_version_id):
        """True when Tradovate shows the modify: a version with our tag, or the requested values."""
        if self.state.has_version_text(order_id, tag):
            return True
        v = self.state.order_version(order_id)
        return bool(v) and v['id'] != previous_version_id and v.get('orderQty') == quantity \
            and v.get('stopPrice') == stop_price

    def _modify_stop(self, plan, tag, expected_epoch):
        """order/modifyorder on the stop. Body shape is the verified Limit modify
        ({orderId, orderQty, orderType, price, timeInForce, text}) with orderType "Stop" and
        stopPrice (documentation-based), plus isAutomated. Reply: {commandId}."""
        oid = plan['order_id']
        body = {'orderId': oid, 'orderQty': plan['quantity'], 'orderType': 'Stop', 'stopPrice': plan['stop_price'],
                'timeInForce': self.cfg.get('time_in_force', 'Day'), 'text': tag, 'isAutomated': True}
        r = self.bridge.request('order/modifyorder', body, timeout=self.cfg.get('order_timeout_s', 5),
                                expected_epoch=expected_epoch)
        d = r.get('d') if isinstance(r.get('d'), dict) else {}
        base = {'modified_order_id': str(oid), 'quantity': plan['quantity'], 'stop_price': plan['stop_price']}
        if plan.get('clamped'):
            base['quantity_clamped_to_position'] = True
        failure = d.get('failureReason') if r.get('ok') else None
        if r.get('ok') and failure in (None, 'Success') and 'commandId' in d:
            return 'accepted', dict(base, commandId=str(d['commandId'])), None
        if not r.get('sent'):
            return 'rejected', {'bridge': 'not_sent', 'code': str(r.get('code'))[:120]}, None
        detail = dict(base, code=str(r.get('code'))[:120], s=r.get('s'),
                      failureReason=str(failure)[:120] if failure else None,
                      failureText=str(d.get('failureText', ''))[:240] if failure else None)
        deadline = time.time() + self.cfg.get('tag_wait_s', 1.5)
        while True:
            if self._modify_applied(oid, tag, plan['quantity'], plan['stop_price'], plan['previous']['version_id']):
                return 'accepted', dict(detail, matched_by_effect=True), None
            if time.time() >= deadline:
                break
            time.sleep(0.05)
        if failure or r.get('s') in protocol.DEFINITE_REFUSAL_STATUSES:
            return 'rejected', _with_reject_reason(detail, failure, d.get('failureText')), None
        return 'unknown', dict(detail, transport='uncertain'), None

    def _find_liquidation(self, rec):
        """The exit order a liquidateposition created: a new Market order on the pinned account and
        contract, closing side, id above the dispatch baseline (its text is "Exit").

        The baseline is the pinned account's highest order id at dispatch (1.0.9.1); order ids are
        only ordered within one account. Another account's orders never match (account filter)."""
        known = self._known_order_ids()
        with self.state.lock:
            for oid in sorted(self.state.entities['order']):
                o = self.state.order(oid)
                if oid <= (rec.get('baseline_order_id') or 0) or oid in known or \
                        o.get('accountId') != self.account_id or o.get('contractId') != self.contract_id or \
                        o.get('action') != rec.get('close_action'):
                    continue
                v = self.state.order_version(oid) or {}
                if v.get('orderType') == 'Market':
                    return oid
        return None

    def _flatten_all(self, plan, expected_epoch):
        """order/liquidateposition {accountId, contractId, admin:false} (verified body and
        {orderId} reply). It has no text field, so there is no client tag."""
        if plan['mode'] == 'cancel':
            outcome, receipt, _ = self._cancel_all(expected_epoch)
            return outcome, dict(receipt, mode='cancel_only'), None
        body = {'accountId': self.account_id, 'contractId': self.contract_id, 'admin': False}
        r = self.bridge.request('order/liquidateposition', body, timeout=self.cfg.get('order_timeout_s', 5),
                                expected_epoch=expected_epoch)
        d = r.get('d') if isinstance(r.get('d'), dict) else {}
        if r.get('ok') and type(d.get('orderId')) is int and not d.get('failureReason'):
            oid = str(d['orderId'])
            receipt = {'broker_order_id': oid, 'id': oid, 'mode': 'liquidate'}
            receipt.update(self._cancel_leftovers(int(oid), expected_epoch))
            return 'accepted', receipt, oid
        if not r.get('sent'):
            return 'rejected', {'bridge': 'not_sent', 'code': str(r.get('code'))[:120]}, None
        failure = d.get('failureReason') if r.get('ok') else None
        if r.get('ok') and not failure:
            # With orders working, the reply carries no orderId. Log the
            # reply's field names only (evidence of its shape), then match the exit order.
            log.info('flatten_all: liquidateposition reply without an order id (fields: %s); matching the '
                     'exit order on the account', ','.join(sorted(str(k)[:40] for k in d)[:10]) or '-')
        detail = {'mode': 'liquidate', 'code': str(r.get('code'))[:120], 's': r.get('s'),
                  'failureReason': str(failure)[:120] if failure else None,
                  'failureText': str(d.get('failureText', ''))[:240] if failure else None}
        deadline = time.time() + self.cfg.get('tag_wait_s', 1.5)
        while True:
            found = self._find_liquidation(plan)
            if found is not None:
                oid = str(found)
                detail.update(self._cancel_leftovers(found, expected_epoch))
                return 'accepted', dict(detail, broker_order_id=oid, id=oid, matched_by_liquidation_order=True), oid
            if time.time() >= deadline:
                break
            time.sleep(0.05)
        if failure or r.get('s') in protocol.DEFINITE_REFUSAL_STATUSES:
            return 'rejected', _with_reject_reason(detail, failure, d.get('failureText')), None
        return 'unknown', dict(detail, transport='uncertain'), None

    def _cancel_leftovers(self, liquidation_id, expected_epoch):
        """After a liquidation: wait (bounded) for flat, then cancel anything still working.

        Whether liquidateposition cancels working orders itself is documentation-based
        ("Exit at Mkt & Cxl") and unverified. A stop left working on a flat contract could open
        a new position, so it is cancelled here (verified cancelorder body). The receipt keeps
        what was found working, as wire evidence. Off (evidence only) with
        flatten_all_cancel_leftovers false, which a bracket test uses on purpose.
        """
        deadline = time.time() + float(self.cfg.get('flatten_settle_s', 3.0))
        grace_until = None
        while True:
            with self.state.lock:
                net = self.state.net_position(self.account_id, self.contract_id)
                left = [o for o in self.state.open_orders(self.account_id, self.contract_id) if o != liquidation_id]
            now = time.time()
            if net == 0 and not left:
                return {'left_working_after_liquidation': []}
            if net == 0 and grace_until is None:
                # Give Tradovate's own cancels a moment before sending ours.
                grace_until = now + float(self.cfg.get('flatten_cancel_grace_s', 0.5))
            if now >= deadline or (grace_until is not None and now >= grace_until):
                break
            time.sleep(0.05)
        out = {'left_working_after_liquidation': [str(o) for o in left], 'flat_after_liquidation': net == 0}
        if net != 0:
            # Not flat (yet): a working stop still protects what is left. Soup verifies and retries.
            log.error('flatten_all: position %d still open %.1f s after the liquidation', net,
                      float(self.cfg.get('flatten_settle_s', 3.0)))
        elif left and self.cfg.get('flatten_all_cancel_leftovers', True) is True:
            outcome, receipt, _ = self._cancel_all(expected_epoch)
            out['leftover_cancel'] = dict(receipt['cancel_targets'], outcome=outcome)
            if outcome != 'accepted':
                log.error('flatten_all: cancelling orders left working after the liquidation is uncertain: %s',
                          receipt['cancel_targets'])
        elif left:
            log.warning('flatten_all: %d order(s) still working after the liquidation; not cancelled '
                        '(flatten_all_cancel_leftovers is false)', len(left))
        return out

    def maintain_brackets(self):
        """Risk-reducing housekeeping for the latest bracket trade (bracket_auto_protect).

        Only when brackets are enabled, the connection is healthy and no command is unresolved.
        Acts only on this bridge's own bracket orders, at most once per condition, never retried:
        - flat on the trade: cancel its still-working stop/TP legs (an orphaned leg could open
          a new position);
        - position below the stop quantity (a TP filled): shrink the stop to the position.
        Returns the command ids it sent.
        """
        if not self.bracket_protect or self.account_id is None:
            return []
        try:
            return self._maintain_brackets()
        except Exception as e:  # noqa: BLE001 - never break the caller's loop; outcomes are journaled
            log.error('bracket maintenance error (%s)', type(e).__name__)
            return []

    def _maintain_brackets(self):
        trade_id = self.journal.latest_trade_id(kinds=ENTRY_KINDS)
        entry = self._bracket_entry(trade_id) if trade_id else None
        if not entry or not (entry.get('legs') or {}).get('stop') or self.journal.unresolved():
            return []
        health = self.health()
        if not health['ok']:
            return []
        epoch = health['page']['epoch']
        stop_id = int(entry['legs']['stop'])
        entry_id = int(entry['broker_order_id'])
        with self.state.lock:
            if self._unseen_accepted_orders():
                return []
            entry_order = self.state.order(entry_id)
            if not entry_order or protocol.is_open(entry_order.get('ordStatus')):
                return []  # the entry is still in flight; its legs are supposed to wait
            held = self._consistent_held(entry)  # act only when the trade's fills explain the position
            if held is None:
                return []
            legs = [stop_id] + self._tp_orders(entry)
            # Every order of this trade (Soup's own TP1 limit included), never another trade's.
            legs += sorted({int(c['broker_order_id']) for c in self.journal.for_trade(trade_id)
                            if c['outcome'] == 'accepted' and c.get('broker_order_id')} - set(legs) - {entry_id})
            working = [oid for oid in legs if protocol.is_open((self.state.order(oid) or {}).get('ordStatus'))]
            stop = self.state.order(stop_id) or {}
            version = self.state.order_version(stop_id) or {}
            stop_open = stop.get('ordStatus') == 'Working' and not self.state.filled_qty(stop_id)
        sent = []
        if held < 0:
            # This trade's own fills reversed the position through flat (e.g. a stop and a
            # take-profit both filled before the stop was shrunk). Close it and stop entries.
            cid = leg_command_id(entry['command_id'], 'auto-flatten')
            if self.journal.get(cid) is None:
                self._set_kill_switch('auto-protect: position reversed through flat')
                plan = self._guard_flatten()
                self.journal.start(cid, trade_id=trade_id, kind='auto_flatten', side=entry['side'],
                                   quantity=plan.get('quantity'), tag=None, at=self.clock_ms(),
                                   **self._journal_fields('flatten_all', plan))
                try:
                    if plan.get('noop'):
                        outcome, receipt, oid = 'accepted', {'bridge': 'noop'}, None
                    else:
                        outcome, receipt, oid = self._flatten_all(plan, epoch)
                except Exception as e:  # noqa: BLE001
                    outcome, receipt, oid = 'unknown', {'bridge': 'exception_after_start', 'error': type(e).__name__}, None
                self.journal.finish(cid, outcome, receipt, oid)
                log.error('bracket %s: position reversed through flat (%d); flatten_all -> %s; entries disabled',
                          entry['command_id'], held, outcome)
                sent.append(cid)
            return sent
        if held == 0 and working:
            cid = leg_command_id(entry['command_id'], 'auto-cancel')
            if self.journal.get(cid) is None:
                self.journal.start(cid, trade_id=trade_id, kind='auto_cancel', side=entry['side'], quantity=None,
                                   tag=None, at=self.clock_ms(), targets=[str(o) for o in working])
                results, uncertain = {}, False
                try:
                    for oid in working:
                        r = self.bridge.request('order/cancelorder', {'orderId': oid},
                                                timeout=self.cfg.get('order_timeout_s', 5), expected_epoch=epoch)
                        d = r.get('d') if isinstance(r.get('d'), dict) else {}
                        if r.get('ok') and d.get('failureReason') in (None, 'Success'):
                            results[str(oid)] = 'requested'
                        elif r.get('sent') is not False and \
                                not protocol.is_open((self.state.order(oid) or {}).get('ordStatus')):
                            results[str(oid)] = 'already_terminal'
                        else:
                            results[str(oid)] = 'uncertain'
                            uncertain = True
                except Exception as e:  # noqa: BLE001 - after start, any failure is an unknown outcome
                    results['error'], uncertain = type(e).__name__, True
                self.journal.finish(cid, 'unknown' if uncertain else 'accepted', {'cancel_targets': results})
                log.warning('bracket %s: flat, cancelled orphaned legs %s', entry['command_id'], results)
                sent.append(cid)
        elif 0 < held < (version.get('orderQty') or 0) and stop_open and version.get('orderType') == 'Stop':
            cid = leg_command_id(entry['command_id'], 'auto-shrink-%d' % held)
            if self.journal.get(cid) is None:
                tag = client_tag(cid)
                plan = {'order_id': stop_id, 'quantity': held, 'stop_price': version.get('stopPrice'),
                        'previous': {'quantity': version.get('orderQty'), 'stop_price': version.get('stopPrice'),
                                     'version_id': version.get('id')}}
                self.journal.start(cid, trade_id=trade_id, kind='auto_modify_stop', side=entry['side'],
                                   quantity=held, tag=tag, at=self.clock_ms(), **self._journal_fields('modify_stop', plan))
                try:
                    outcome, receipt, _ = self._modify_stop(plan, tag, epoch)
                except Exception as e:  # noqa: BLE001
                    outcome, receipt = 'unknown', {'bridge': 'exception_after_start', 'error': type(e).__name__}
                self.journal.finish(cid, outcome, receipt)
                log.warning('bracket %s: position %d below the stop quantity; shrink -> %s',
                            entry['command_id'], held, outcome)
                sent.append(cid)
        return sent

    def _set_kill_switch(self, why):
        """Create the configured kill-switch file (entries refused until a human removes it)."""
        path = self.cfg.get('kill_switch_file')
        if not path:
            return False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            log.error('kill switch set (%s): %s', why, path)
            return True
        except OSError as e:
            log.error('kill switch could not be set (%s): %s', why, type(e).__name__)
            return False

    def auto_reconcile(self):
        """Resolve unknown commands whose order (or effect) has since appeared at Tradovate."""
        resolved = []
        for c in self.journal.unresolved():
            if c['outcome'] != 'unknown':
                continue
            kind = c.get('kind')
            if kind == 'bracket_entry':
                entry_id = self.state.order_id_for_tag(c['tag']) if c.get('tag') else None
                if entry_id is None:
                    continue
                roles = ['stop'] + (['tp1'] if c.get('tp1_leg') == 'oso' else [])
                legs = self._find_legs(c, roles, entry_id)
                if len(legs) == len(roles):
                    self.journal.reconcile(c['command_id'], 'accepted', broker_order_id=str(entry_id),
                                           legs=legs, note='matched by client tag')
                    resolved.append(c['command_id'])
            elif kind in ('modify_stop', 'auto_modify_stop'):
                prev = c.get('previous') or {}
                if c.get('target_order_id') and self._modify_applied(c['target_order_id'], c.get('tag'), c.get('quantity'),
                                                                     c.get('stop_price'), prev.get('version_id')):
                    self.journal.reconcile(c['command_id'], 'accepted', note='modify seen at Tradovate')
                    resolved.append(c['command_id'])
            elif kind in ('flatten_all', 'manual_flatten'):
                # 1.1.3: an operator manual_flatten (tvbridge.manual) left unknown resolves only as flat and
                # idle, never by matching a liquidation order (one after it could be someone else's).
                found = self._find_liquidation(c) if kind == 'flatten_all' and c.get('mode') == 'liquidate' else None
                if found is not None:
                    self.journal.reconcile(c['command_id'], 'accepted', broker_order_id=str(found),
                                           note='matched liquidation order')
                    resolved.append(c['command_id'])
                elif self._flat_and_idle_since(c):
                    # No liquidation order appeared and the contract has stayed flat with nothing
                    # working: whatever this request did, it left nothing behind to report.
                    self.journal.reconcile(c['command_id'], 'accepted', note='flat with nothing working; no order')
                    resolved.append(c['command_id'])
            elif kind in NO_ORDER_KINDS:
                continue
            elif c.get('tag'):
                found = self.state.order_id_for_tag(c['tag'])
                if found is not None:
                    self.journal.reconcile(c['command_id'], 'accepted', broker_order_id=str(found),
                                           note='matched by client tag')
                    resolved.append(c['command_id'])
        return resolved

    def _flat_and_idle_since(self, rec):
        """Flat, nothing working or unseen, and the request is older than flatten_reconcile_after_s."""
        at = rec.get('at')
        wait_ms = float(self.cfg.get('flatten_reconcile_after_s', 5.0)) * 1000
        if type(at) is not int or self.clock_ms() - at < wait_ms:
            return False
        with self.state.lock:
            if self.state.net_position(self.account_id, self.contract_id) != 0 or \
                    self.state.open_orders(self.account_id, self.contract_id):
                return False
        return not self._unseen_accepted_orders()

    def _cancel_all(self, expected_epoch, targets=None):
        """Ticker-scoped cancel, like a webhook relay's: every working order on the contract,
        plus any accepted order Tradovate has not reported yet. 1.1.3: `targets` (order ids), when
        given, replaces that set (tvbridge.manual cancels non-protective orders before the stop)."""
        if targets is None:
            targets = set(self.state.open_orders(self.account_id, self.contract_id))
            targets.update(int(oid) for oid in self._unseen_accepted_orders())
        else:
            targets = {int(oid) for oid in targets}
        results = {}
        uncertain = False
        for oid in sorted(targets):
            r = self.bridge.request('order/cancelorder', {'orderId': oid}, timeout=self.cfg.get('order_timeout_s', 5),
                                    expected_epoch=expected_epoch)
            d = r.get('d') if isinstance(r.get('d'), dict) else {}
            if r.get('ok') and d.get('failureReason') in (None, 'Success'):
                results[str(oid)] = 'requested'
                continue
            order = self.state.order(oid) or {}
            if r.get('sent') is not False and order and not protocol.is_open(order.get('ordStatus')):
                results[str(oid)] = 'already_terminal'  # filled/cancelled in the race
                continue
            results[str(oid)] = 'uncertain:' + str(d.get('failureReason') or r.get('code') or r.get('s'))[:80]
            uncertain = True
        receipt = {'cancel_targets': results}
        return ('unknown' if uncertain else 'accepted'), receipt, None

    # --- broker evidence for Soup -----------------------------------------------------

    def snapshot(self, trade_id):
        """Build one Soup broker-verification snapshot, or (None, reason) when unsafe.

        Read under the state lock so orders, fills and position come from one moment.
        Terminal orders are only reported once their fills agree with them, because Soup
        treats terminal fills as immutable.
        """
        # A publisher fault blocks order dispatch, but must not block building
        # the next valid snapshot that can restore publication.
        health = self.health(include_watchdogs=False)
        if not health['ok']:
            return None, 'unhealthy:' + ','.join(health['problems'])
        side = protocol.side_from_trade_id(trade_id)
        settle_ms = self.cfg.get('terminal_settle_ms', 1500)
        with self.state.lock:
            if not self.state.is_synced_for(health['page'].get('epoch')):
                return None, 'connection_changed_before_snapshot'
            items = []  # (command id, broker order id, quantity, extra report fields)
            entry_ids = set()  # 1.0.9.4.1: the trade's entry orders (all other orders take contracts out)
            for c in self.journal.for_trade(trade_id):
                kind = c['kind']
                if kind in NO_ORDER_KINDS or c['outcome'] == 'rejected':
                    continue
                if kind in ENTRY_KINDS and c.get('broker_order_id'):
                    entry_ids.add(str(c['broker_order_id']))
                if kind == 'auto_flatten':
                    # Soup has no command for it: its evidence would never reconcile. A person
                    # reviews the reversed trade (kill switch set); nothing is published for it.
                    return None, 'auto_flatten_after_reversal:' + c['command_id']
                if kind == 'flatten_all' and c['outcome'] == 'accepted' and not c.get('broker_order_id'):
                    continue  # no-op or cancel-only: no order of its own
                if c['outcome'] != 'accepted' or not c.get('broker_order_id'):
                    return None, 'uncertain_dispatch:' + c['command_id']
                if kind == 'bracket_entry':
                    entry_id = c['broker_order_id']
                    items.append((c['command_id'], entry_id, c['quantity'], {'role': 'entry'}))
                    for role in ('stop', 'tp1'):
                        leg = (c.get('legs') or {}).get(role)
                        if leg:
                            qty = self.state.order_qty(int(leg))
                            if qty is None:
                                return None, 'order_not_seen_yet:' + leg
                            extra = {'role': role, 'parent_broker_order_id': entry_id, 'quantity': qty}
                            if role == 'stop':
                                # Soup tracks the resting stop's current size and price (modifies).
                                extra['stop_price'] = (self.state.order_version(int(leg)) or {}).get('stopPrice')
                            items.append((leg_command_id(c['command_id'], role), leg, qty, extra))
                elif kind == 'bracket_tp1':
                    parent = self.journal.get(c.get('parent') or '') or {}
                    items.append((c['command_id'], c['broker_order_id'], c['quantity'],
                                  {'role': 'tp1', 'parent_broker_order_id': parent.get('broker_order_id')}))
                elif kind == 'flatten_all':
                    qty = self.state.order_qty(int(c['broker_order_id']))
                    qty = qty if qty is not None else c['quantity']
                    items.append((c['command_id'], c['broker_order_id'], qty, {'role': 'flatten', 'quantity': qty}))
                else:
                    items.append((c['command_id'], c['broker_order_id'], c['quantity'], {}))
            orders = []
            ended = []  # 1.0.9.4.1: exit orders reported 'Filled' short of their size (see below)
            for command_id, broker_id, quantity, extra in items:
                oid = int(broker_id)
                order = self.state.order(oid)
                if order is None:
                    return None, 'order_not_seen_yet:' + broker_id
                filled = self.state.filled_qty(oid)
                status = protocol.soup_status(order.get('ordStatus'), filled)
                if status is None:
                    return None, 'unmapped_status:' + str(order.get('ordStatus'))
                if filled > (quantity or 0):
                    return None, 'overfill:' + broker_id
                if status == protocol.FILLED and filled != quantity:
                    # 1.0.9.4.1: Tradovate ends an order
                    # that filled in part and whose rest was removed (liquidateposition, cancel)
                    # with ordStatus 'Filled', the order's full orderQty and only the real fills.
                    # Such an exit order is reported as what it is, a partly filled order whose
                    # remainder was cancelled, with its actual fills (never more). Only when it
                    # takes contracts out (not an entry), has at least one fill, has been terminal
                    # for the settle time, and the whole trade's fills explain the position (checked
                    # below). Otherwise its fills may still be arriving: withheld as before.
                    age = self.state.terminal_age_ms(oid)
                    if broker_id in entry_ids or filled <= 0 or age is None or age < settle_ms:
                        return None, 'fills_pending:' + broker_id
                    status = protocol.CANCELLED
                    ended.append(broker_id)
                if status in (protocol.CANCELLED, protocol.REJECTED):
                    age = self.state.terminal_age_ms(oid)
                    if age is None or age < settle_ms:
                        return None, 'terminal_settling:' + broker_id
                    if status == protocol.REJECTED and filled:
                        return None, 'rejected_with_fills:' + broker_id
                item = {'command_id': command_id, 'broker_order_id': broker_id,
                        'status': status, 'filled_qty': filled}
                if filled:
                    item['fill_time'] = self.state.last_fill_ms(oid)
                    if getattr(self, 'snapshot_fill_prices', False) is True:
                        price = exact_avg_price(self.state.fills_for(oid))
                        if price is not None:
                            item['avg_fill_price'] = price
                item.update(extra)  # bracket trades only: role and OSO parent linkage
                if status == protocol.REJECTED:
                    # 1.0.9.5.2: Tradovate's own rejection text, additive and only when Tradovate sent one.
                    why = self.reject_reason_of(oid)
                    if why:
                        item['reject_reason'] = why
                orders.append(item)
            net = self.state.net_position(self.account_id, self.contract_id)
            open_orders = [str(o) for o in self.state.open_orders(self.account_id, self.contract_id)]
        held = net if side == 'long' else -net
        if held < 0:
            return None, 'position_opposite_to_trade'
        if ended:
            # 1.0.9.4.1: a missing fill would show here as entry fills - exit fills != position.
            entered = sum(o['filled_qty'] for o in orders if o['broker_order_id'] in entry_ids)
            exited = sum(o['filled_qty'] for o in orders if o['broker_order_id'] not in entry_ids)
            if entered - exited != held:
                return None, 'fills_pending:' + ended[0]
            logged = self.__dict__.setdefault('_ended_logged', set())
            for broker_id in ended:
                if broker_id not in logged:
                    logged.add(broker_id)
                    log.warning('snapshot: order %s ended Filled short of its size with the rest removed '
                                '(fills explain the position); reported cancelled with its actual fills', broker_id)
        seq = self.journal.next_sequence()
        return {
            'event_id': f'tvbridge-{seq}',
            'sequence': seq,
            'observed_at': self.clock_ms(),
            'account_id': self.cfg['soup_account_id'],
            'symbol': self.cfg['ticker'],
            'trade_id': trade_id,
            'side': side,
            'position_qty': held,
            'orders': orders,
            'open_orders': open_orders,
        }, None

    def trade_active(self, trade_id):
        """True while the trade has a position or any non-terminal order."""
        if self.state.net_position(self.account_id, self.contract_id) != 0:
            return True
        if self.state.open_orders(self.account_id, self.contract_id):
            return True
        return any(c['outcome'] in (None, 'unknown') for c in self.journal.for_trade(trade_id))
