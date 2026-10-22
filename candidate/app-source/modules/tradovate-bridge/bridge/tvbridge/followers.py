"""Follower accounts inside the bridge daemon (1.0.7 firing order, opt-in `followers` in config.json;
1.0.8: optional native exits per follower, `native_exits_enabled`; 1.0.9: the leader's own trade
facts in /followers, for the firing service's "leader closed outside the bridge" rule).

The bridge page opens its own Tradovate websocket with the login's access token and syncs
every account of that login (`account/list` + `user/syncrequest`); every order body names
its `accountId`. So accounts on the same Tradovate login need no browser, login or socket
of their own. Each follower gets its own Executor pinned to its account, sharing the main
executor's Bridge (one CDP session, one Tradovate socket) and AccountState.

What does not change for the main account: its Executor, journal, kill switch, publisher
and the routes /order, /state, /health and /account. Without `followers` in config.json
the daemon builds exactly the 1.0.6 handler.

Follower routes (same token as the main routes):
  POST /follower/<token>/<key>/order                    same payload and reply as /order
  GET  /follower/<token>/<key>/state[?trade_id=<id>]    same shape as /state (extended)
  GET  /followers/<token>[?trade_id=<id>]              the main account and every follower: position,
                                                        working orders (TP1 marked), login, kill switch;
                                                        with trade_id also per-command fills (grid alerts)
                                                        and (1.0.9) `main.leader`: the leader trade's entry
                                                        and exit fills and snapshot sequence (read-only);
                                                        (1.0.9.2) with `account_view_enabled`, each available
                                                        row's `cash_balance` (read-only, as /account's)
  POST /follower/<token>/<key>/kill                     1.0.8: set THIS follower's kill switch (risk-reducing
                                                        only; the firing service uses it after an unverified
                                                        native exit-all). Never touches another account.

Native exits per follower (1.0.8, `native_exits_enabled: true`, default false): the follower's
executor accepts bracket_entry / modify_stop / flatten_all (the same code as the leader's
1.0.6.2 native exits) and gets its own bracket auto-protect housekeeping thread (shrink an
oversized stop after a TP fill, cancel orphaned legs when flat, liquidate + kill switch on a
reversal). The per-follower state machine (break-even on the follower's OWN TP1 fill, runner
exits, exit-all with verification) runs in the firing service (tvbridge.follower_native).
With trade_id, GET /follower/.../state adds `trade`: that trade's orders on this follower
(status, quantity, fills, stop price), ids only.

Per follower:
- its own append-only fsynced journal (`journal_dir`, start before send, unknown never retried);
- its own kill switch file; the main kill switch also disables follower entries, and so does
  (1.1.0) `master_kill_switch_file`, another login's main kill switch, read only;
- no size cap: the follower takes the payload quantity as-is (the strategy alert sets the size);
  the retired `max_contracts` key may still hold a legacy positive whole number, which is ignored;
- the hedge guard is always on for followers and sees every account of the login;
- leader rule: the main account is the leader and is never blocked by a follower.
  build() puts the follower account ids in the main executor's `hedge_exempt_accounts`;
  every other account of the login is still checked. Followers align to the leader instead:
  before a new entry the firing service cancels a follower's working orders and closes its
  exact position with `align_flatten` (follower-only, market, never a flip);
- command ids are stored as `f:<key>:<soup id>`, so the Tradovate order text tag of a
  follower order never equals the main account's tag for the same Soup command.
A follower whose account is missing from the login is reported `available: false`; its
orders come back `rejected` (nothing sent). It never stops the daemon or the main account.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import threading
import urllib.parse

from . import protocol
from .daemon import account_state
from .executor import Executor, Refused, kill_switch_state, max_contracts_problem, pause_fields
from .journal import ENTRY_KINDS, Journal

log = logging.getLogger('tvbridge')

KEY_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}\Z')
# cross-account-window: for firm rules that allow opposite exposure across a trader's own accounts
#   only for a short window; firing.py caps the reconcile grace plus settle at 30 s for these accounts.
# unrestricted: no prop limits (for example a personal demo or paper account).
# apex: refused. Apex's user agreement forbids automated and copied orders on evaluation/PA accounts.
PROP_PROFILES = ('cross-account-window', 'unrestricted')
REFUSED_PROFILES = {'apex': 'Apex accounts are never copied (Apex bans copy trading)'}
# Settings a follower shares with the main account (same page, contract and order style).
INHERITED = ('ticker', 'tick_size', 'hedge_groups', 'time_in_force', 'order_timeout_s', 'max_silence_ms',
             'tag_wait_s', 'entry_settle_s', 'max_limit_deviation_pct', 'terminal_settle_ms',
             # 1.0.8 native followers: the same flatten_all timings as the leader
             'flatten_settle_s', 'flatten_cancel_grace_s', 'flatten_reconcile_after_s',
             # 1.1.0 multi-login: another login's main kill switch, read only (also stops follower entries)
             'master_kill_switch_file',
             # the binding's strategy adapter (tvbridge.adapters); a follower takes the same one
             'adapters')
FOLLOWER_KEYS = frozenset({'key', 'account_name', 'account_id', 'journal_dir', 'kill_switch_file',
                           'max_contracts', 'enabled', 'prop_profile', 'flatten_all_enabled', 'label',
                           'native_exits_enabled'})


class FollowerConfigError(ValueError):
    pass


def _resolved(path):
    return os.path.realpath(os.path.expanduser(path))


def parse(cfg):
    """Validate `cfg['followers']`. Returns [(key, executor config, enabled)]; raises FollowerConfigError.

    Sends nothing and touches no file.
    """
    raw = cfg.get('followers', [])
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise FollowerConfigError('followers must be a list')
    out, keys, names, ids, dirs, kills = [], set(), {cfg.get('account_name')}, set(), set(), set()
    if cfg.get('account_id') is not None:
        ids.add(cfg['account_id'])
    if isinstance(cfg.get('journal_dir'), str):
        dirs.add(_resolved(cfg['journal_dir']))
    if isinstance(cfg.get('kill_switch_file'), str):
        kills.add(_resolved(cfg['kill_switch_file']))
    for i, f in enumerate(raw):
        where = f'followers[{i}]'
        if not isinstance(f, dict):
            raise FollowerConfigError(where + ' must be an object')
        extra = set(f) - FOLLOWER_KEYS
        if extra:
            raise FollowerConfigError(f'{where} has unknown keys: {", ".join(sorted(extra))}')
        key = f.get('key')
        if not isinstance(key, str) or not KEY_RE.match(key) or key in keys:
            raise FollowerConfigError(f'{where}.key must be a unique lowercase slug')
        keys.add(key)
        where = f'follower {key}'
        profile = f.get('prop_profile')
        if isinstance(profile, str) and profile.lower() in REFUSED_PROFILES:
            raise FollowerConfigError(f'{where}: {REFUSED_PROFILES[profile.lower()]}')
        if profile not in PROP_PROFILES:
            raise FollowerConfigError(f'{where}.prop_profile must be one of {", ".join(PROP_PROFILES)}')
        name = f.get('account_name')
        if not isinstance(name, str) or not name.strip() or name in names:
            raise FollowerConfigError(f'{where}.account_name must be set and differ from every other account')
        names.add(name)
        acct = f.get('account_id')
        if acct is not None and (type(acct) is not int or acct <= 0 or acct in ids):
            raise FollowerConfigError(f'{where}.account_id must be null or a unique positive integer')
        if acct is not None:
            ids.add(acct)
        for field, seen in (('journal_dir', dirs), ('kill_switch_file', kills)):
            value = f.get(field)
            if not isinstance(value, str) or not value.startswith('/'):
                raise FollowerConfigError(f'{where}.{field} must be an absolute path')
            if _resolved(value) in seen:
                raise FollowerConfigError(f'{where}.{field} must differ from every other account')
            seen.add(_resolved(value))
        # Retired key: a legacy positive whole number is accepted and ignored (never a size limit).
        problem = max_contracts_problem(f.get('max_contracts'))
        if problem:
            raise FollowerConfigError(f'{where}.{problem}')
        enabled = f.get('enabled', True)
        native = f.get('native_exits_enabled', False)
        flatten = f.get('flatten_all_enabled', native)
        if not isinstance(enabled, bool) or not isinstance(flatten, bool) or not isinstance(native, bool):
            raise FollowerConfigError(f'{where}.enabled, flatten_all_enabled and native_exits_enabled '
                                      f'must be true or false')
        if native and not flatten:
            raise FollowerConfigError(f'{where}: native_exits_enabled needs flatten_all_enabled (exit-all)')
        ecfg = {k: cfg[k] for k in INHERITED if k in cfg}
        # Native exits (1.0.8): the leader's own 1.0.6.2 bracket code on this follower's account,
        # with its auto-protect. Off by default: then exactly the 1.0.7 follower executor.
        ecfg.update(account_name=name, account_id=acct, journal_dir=f['journal_dir'],
                    kill_switch_file=f['kill_switch_file'], hedge_guard=True,
                    bracket_orders_enabled=native, flatten_all_enabled=flatten, publish_mode='none',
                    soup_account_id=None, follower_key=key, prop_profile=profile)
        if native:
            ecfg.update(bracket_auto_protect=True, native_exits_enabled=True)
        out.append((key, ecfg, enabled))
    return out


ALIGN_ACTION = 'align_flatten'
_ALIGN_FIELDS = frozenset({'ticker', 'extras', 'action', 'quantity'})


class FollowerExecutor(Executor):
    """An Executor pinned to a follower account of the same login. Main-account code is unchanged.

    One extra command, follower-only: {"ticker", "extras": {soup_command_id, soup_trade_id},
    "action": "align_flatten", "quantity": N}. A market order that closes exactly the live
    position N on the pinned contract (Sell for a long, Buy for a short). Refused unless the
    contract has no working order, no unreported accepted order, and N equals |position|.
    Never blocked by a kill switch (it only reduces risk).
    """

    # 1.0.9.5: a contract closed by hand on a follower stays a fault the follower lane handles (unchanged).
    HAND_REDUCTIONS = False

    def __init__(self, bridge, state, journal, config, *, main_kill_switch=None, **kw):
        super().__init__(bridge, state, journal, config, **kw)
        self.key = config['follower_key']
        self.id_prefix = f'f:{self.key}:'
        self.main_kill_switch = main_kill_switch

    def _parse(self, p):
        if isinstance(p, dict) and p.get('action') == ALIGN_ACTION:
            if set(p) != _ALIGN_FIELDS or p.get('ticker') != self.cfg['ticker']:
                raise Refused('align_flatten payload must be {ticker, extras, action, quantity} on the pinned ticker')
            extras = p.get('extras')
            if not isinstance(extras, dict) or set(extras) != {'soup_command_id', 'soup_trade_id'}:
                raise Refused('align_flatten extras must be soup_command_id and soup_trade_id')
            cid, tid, qty = extras['soup_command_id'], extras['soup_trade_id'], p['quantity']
            if not isinstance(cid, str) or not cid or not isinstance(tid, str) or not tid:
                raise Refused('command/trade id missing')
            side = protocol.side_from_trade_id(tid)
            if side is None:
                raise Refused('trade id has no side')
            if type(qty) is not int or qty <= 0:
                raise Refused('invalid quantity')
            return {'command_id': self.id_prefix + cid, 'trade_id': tid, 'side': side, 'quantity': qty,
                    'kind': ALIGN_ACTION}
        cmd = super()._parse(p)
        # Namespaced journal id: the order text tag (client_tag) and the dedupe key are per account.
        cmd['command_id'] = self.id_prefix + cmd['command_id']
        return cmd

    def _guard(self, cmd):
        if cmd['kind'] != ALIGN_ACTION:
            return super()._guard(cmd)
        if self._unseen_accepted_orders():
            raise Refused('an accepted order has not been reported by Tradovate yet')
        with self.state.lock:
            net = self.state.net_position(self.account_id, self.contract_id)
            open_orders = self.state.open_orders(self.account_id, self.contract_id)
        if open_orders:
            raise Refused('working orders on the contract; cancel them first')
        if net == 0:
            raise Refused('already flat')
        if cmd['quantity'] != abs(net):
            raise Refused(f'align quantity {cmd["quantity"]} differs from the live position {abs(net)}')
        return {'action': 'Sell' if net > 0 else 'Buy', 'orderType': 'Market', 'quantity': abs(net)}

    # 1.0.9 (if it did not enter, leave it alone): after an entry that Tradovate rejected
    # or cancelled with zero fill on a flat follower, Tradovate cancels the OSO legs with the parent.
    # The follower's auto-protect (the leader's code) would race that with its own cancel of the
    # "orphaned" legs. It waits this long first; a leg still working after it is cancelled as before
    # (risk-reducing). The leader's auto-protect is unchanged.
    ZERO_FILL_LEG_GRACE_MS = 5000

    def _maintain_brackets(self):
        trade_id = self.journal.latest_trade_id(kinds=ENTRY_KINDS)
        entry = self._bracket_entry(trade_id) if trade_id else None
        if entry and entry.get('broker_order_id') and self.account_id is not None:
            oid = int(entry['broker_order_id'])
            with self.state.lock:
                order = self.state.order(oid)
                unfilled_flat = (not self.state.filled_qty(oid)
                                 and self.state.net_position(self.account_id, self.contract_id) == 0)
                in_flight = order is None or protocol.is_open(order.get('ordStatus'))
                age = None if in_flight else self.state.terminal_age_ms(oid)
            # Stand down while the unfilled entry is in flight (the base code does too, but it re-reads
            # the status later and could see the rejection before the legs' cancels) and for the grace
            # after a zero-fill end. Fills never go back, so a fill seen here stays a fill.
            if unfilled_flat and (in_flight or age is None or age < self.ZERO_FILL_LEG_GRACE_MS):
                return []
        return super()._maintain_brackets()

    def entries_disabled_reason(self):
        why = super().entries_disabled_reason()
        if why is None and self.main_kill_switch:
            # The main account's switch (read only here); fails closed like this account's own.
            main = kill_switch_state(self.main_kill_switch, writable=False)
            why = None if main is None else 'main_' + main
        return why


class Unavailable:
    """A configured follower that could not be pinned (disabled, or account not in this login)."""

    def __init__(self, key, reason):
        self.key, self.reason = key, reason


def build(bridge, state, cfg, main_executor, *, clock_ms=None):
    """Follower executors keyed by `key`. Never raises for a follower fault: it becomes Unavailable.

    1.1.0: `main_executor` is None on a followers_only login (no main account on that login)."""
    followers = {}
    for key, ecfg, enabled in parse(cfg):
        if not enabled:
            followers[key] = Unavailable(key, 'disabled_in_config')
            continue
        try:
            kw = {'clock_ms': clock_ms} if clock_ms else {}
            ex = FollowerExecutor(bridge, state, Journal(ecfg['journal_dir']), ecfg,
                                  main_kill_switch=cfg.get('kill_switch_file'), **kw)
            ex.resolve()
            if main_executor is not None and ex.account_id == main_executor.account_id:
                raise RuntimeError('follower account is the main account')
        except Exception as exc:  # noqa: BLE001 - a follower fault never stops the main account
            log.error('follower %s unavailable: %s', key, exc)
            followers[key] = Unavailable(key, 'unavailable:' + str(exc)[:160])
            continue
        log.info('follower %s ready: account id %s, %s%s', key, ex.account_id, ex.symbol,
                 ' (native exits: brackets, flatten_all, auto-protect)' if ex.bracket_protect else '')
        if ex.journal.unresolved():
            log.error('follower %s journal has unresolved commands; its entries stay blocked until reconciled', key)
        followers[key] = ex
    # Leader rule: a follower's position or orders never refuse the main account's entry.
    if main_executor is not None:
        main_executor.hedge_exempt_accounts = frozenset(
            ex.account_id for ex in followers.values() if not isinstance(ex, Unavailable))
    return followers


def _row(ex, trade_id=None, balance=False):
    """Grid row for one pinned account: never a name, only its hash. With `balance` (1.0.9.2), also
    the account's `cash_balance`."""
    s = account_state(ex)
    with ex.journal.lock:
        tp1 = {str(c.get('broker_order_id')) for c in ex.journal.commands.values()
               if c.get('kind') == 'tp1_limit' and c.get('outcome') == 'accepted'}
    orders = []
    with ex.state.lock:
        for oid in (ex.state.open_orders(ex.account_id, ex.contract_id) if s['synced'] else []):
            version = ex.state.order_version(oid) or {}
            qty = ex.state.order_qty(oid)
            orders.append({'id': str(oid), 'type': {'Limit': 'LMT', 'Stop': 'STP', 'Market': 'MKT',
                                                     'StopLimit': 'STL'}.get(version.get('orderType'), 'ORD'),
                           'qty': None if qty is None else qty - ex.state.filled_qty(oid),
                           'tp1': str(oid) in tp1,
                           # 1.0.8: Buy/Sell, so the firing service can wait out another account's
                           # bracket stop reported before its position (hedge-guard race).
                           'action': (ex.state.order(oid) or {}).get('action')})
        # 1.0.9.2: this account's Tradovate cash row, read-only: the same object as GET /account's
        # `account_data.cash_balance` (amount, amount_sod, realized_pnl, week_realized_pnl,
        # trade_date, timestamp), from the sync data the daemon already holds (no Tradovate request).
        # None when the row is not synced for the current connection or has no numeric balance.
        cash = ex.state.account_view(ex.account_id)['cash_balance'] if balance and s['synced'] else None
    name = ex.cfg.get('account_name')
    row = {'account_id': ex.account_id, 'available': True, 'synced': s['synced'], 'healthy': s['healthy'],
           'logged_in': not any(p.startswith('page_state:') or p == 'not_synced_for_current_connection'
                                for p in s['problems']) and s['synced'],
           'problems': s['problems'], 'net_position': s['net_position'], 'orders': orders,
           'unresolved_commands': s['unresolved_commands'], **pause_fields(ex),
           'native_exits': ex.cfg.get('native_exits_enabled') is True,
           'account_name_sha256': hashlib.sha256(name.encode()).hexdigest() if isinstance(name, str) else None}
    if balance:
        row['cash_balance'] = cash
    if trade_id is not None:
        fills = []
        for c in ex.journal.for_trade(trade_id):
            if c.get('outcome') != 'accepted' or not c.get('broker_order_id'):
                continue
            oid = int(c['broker_order_id'])
            with ex.state.lock:
                fills.append({'command_id': c['command_id'], 'kind': c.get('kind'), 'quantity': c.get('quantity'),
                              'filled': ex.state.filled_qty(oid), 'price': ex.state.avg_fill_price(oid),
                              'fill_ms': ex.state.last_fill_ms(oid)})
        row['trade'] = fills
    return row


def _order_view(ex, oid):
    """One order on a follower account: status, size, fills, prices. Ids only; caller holds the state lock."""
    order = ex.state.order(oid) or {}
    version = ex.state.order_version(oid) or {}
    status = order.get('ordStatus')
    view = {'broker_order_id': str(oid), 'seen': bool(order), 'status': status,
            'working': bool(order) and protocol.is_open(status), 'order_type': version.get('orderType'),
            'action': order.get('action'), 'quantity': ex.state.order_qty(oid), 'filled': ex.state.filled_qty(oid),
            'price': version.get('price'), 'stop_price': version.get('stopPrice'),
            'fill_ms': ex.state.last_fill_ms(oid), 'avg_fill_price': ex.state.avg_fill_price(oid),
            'terminal_age_ms': ex.state.terminal_age_ms(oid)}
    if status == 'Rejected':
        # 1.0.9.5.2: Tradovate's rejection text, only when it sent one (the firing service's skip note).
        why = ex.reject_reason_of(oid) if hasattr(ex, 'reject_reason_of') else None
        if why:
            view['reject_reason'] = why
    return view


def trade_view(ex, trade_id):
    """1.0.8 native followers: this follower's orders for one trade, keyed by the firing service's
    command id (the `f:<key>:` prefix removed). `stop` is the trade's bracket stop leg, if any."""
    commands, stop = {}, None
    with ex.state.lock:
        for c in ex.journal.for_trade(trade_id):
            cid = c['command_id']
            if cid.startswith(ex.id_prefix):
                cid = cid[len(ex.id_prefix):]
            item = {'kind': c.get('kind'), 'outcome': c.get('outcome'), 'at': c.get('at')}
            if c.get('broker_order_id'):
                item.update(_order_view(ex, int(c['broker_order_id'])))
            commands[cid] = item
            leg = (c.get('legs') or {}).get('stop')
            if c.get('kind') == 'bracket_entry' and c.get('outcome') == 'accepted' and leg:
                stop = _order_view(ex, int(leg))
    return {'commands': commands, 'stop': stop}


def executors(followers):
    """The pinned follower executors (Unavailable entries left out)."""
    return [ex for ex in followers.values() if not isinstance(ex, Unavailable)]


class Housekeeping:
    """1.0.8: bracket auto-protect for native followers, one thread per follower.

    The leader's auto-protect runs in its publisher thread; a follower's runs here, so a slow
    follower request never delays the leader's housekeeping. Same executor code
    (Executor.maintain_brackets: risk-reducing only, each action journaled and never retried),
    on the follower's own journal, account and kill switch. Stops when the daemon stops.
    """

    def __init__(self, followers, state, interval_s=1.0):
        self.state, self.interval_s = state, interval_s
        self.stopping = threading.Event()
        self.threads = []
        for ex in executors(followers):
            if getattr(ex, 'bracket_protect', False) is True:
                t = threading.Thread(target=self._run, args=(ex,), name='follower-protect-' + ex.key, daemon=True)
                self.threads.append(t)

    def start(self):
        for t in self.threads:
            t.start()
        return self

    def _run(self, ex):
        version = -1
        while not self.stopping.is_set():
            try:
                version = self.state.wait_for_change(version, self.interval_s)
                if self.stopping.is_set() or getattr(ex.bridge, 'stop_reason', None):
                    return
                with ex.state.lock:
                    # Every auto-protect action needs a position or a working order on this follower's
                    # contract; skipping when neither exists avoids a page status call per account event.
                    idle = ex.account_id is None or (
                        ex.state.net_position(ex.account_id, ex.contract_id) == 0
                        and not ex.state.open_orders(ex.account_id, ex.contract_id))
                if idle:
                    continue
                with ex.lock:
                    ex.maintain_brackets()
            except Exception as exc:  # noqa: BLE001 - never stop protecting the follower
                log.error('follower %s housekeeping error (%s)', ex.key, type(exc).__name__)
                self.stopping.wait(1)

    def stop(self, timeout=5):
        self.stopping.set()
        with self.state.changed:
            self.state.changed.notify_all()
        for t in self.threads:
            if t.is_alive():
                t.join(timeout)


# 1.0.9: leader journal kinds whose fills take contracts out of a trade (plus the bracket stop leg).
LEADER_EXIT_KINDS = ('tp1_limit', 'bracket_tp1', 'exit_market', 'flatten_all', 'auto_flatten')
LEADER_ENTRY_KINDS = ('entry', 'bracket_entry')


def leader_facts(ex, trade_id, publisher=None):
    """1.0.9: the bridge's own view of the leader's trade, for the firing service's
    `leader_closed_externally` rule. Read-only, in memory (no page call).

    entry_filled: contracts the trade's entry filled; exit_filled: contracts the bridge's own orders
    for this trade took out (TP1, market exits, flatten_all, auto-flatten, the bracket's stop and TP1 legs);
    unresolved: commands of this trade without a definite outcome; open_orders / unseen: working or
    accepted-but-unreported orders on the pinned contract; snapshot_sequence: the sequence of the
    publisher's latest evidence snapshot (the snapshots Soup checks), or None.
    """
    entry = exits = 0
    unresolved = own_liquidation = 0
    with ex.state.lock:
        for c in ex.journal.for_trade(trade_id):
            if c.get('outcome') in (None, 'unknown'):
                unresolved += 1
            if c.get('outcome') != 'accepted':
                continue
            kind = c.get('kind')
            if kind in ('flatten_all', 'auto_flatten') and c.get('mode') == 'liquidate' and \
                    not c.get('broker_order_id'):
                # 1.0.9.1: the bridge's own liquidation, accepted (or reconciled) without an identified
                # exit order. liquidateposition closes whatever is open, so it explains a flat leader.
                own_liquidation += 1
            if kind in LEADER_ENTRY_KINDS and c.get('broker_order_id'):
                entry += ex.state.filled_qty(int(c['broker_order_id']))
                if kind == 'bracket_entry':
                    for leg in ((c.get('legs') or {}).get(role) for role in ('stop', 'tp1')):
                        if leg:
                            exits += ex.state.filled_qty(int(leg))
            elif kind in LEADER_EXIT_KINDS and c.get('broker_order_id'):
                exits += ex.state.filled_qty(int(c['broker_order_id']))
        resolved = ex.account_id is not None and ex.contract_id is not None
        net = ex.state.net_position(ex.account_id, ex.contract_id) if resolved else None
        open_orders = len(ex.state.open_orders(ex.account_id, ex.contract_id)) if resolved else None
    last = getattr(publisher, 'last', None)
    seq = last.get('sequence') if isinstance(last, dict) else None
    facts = {'trade_id': trade_id, 'entry_filled': entry, 'exit_filled': exits, 'unresolved': unresolved,
             'net_position': net, 'open_orders': open_orders,
             'unseen': len(ex._unseen_accepted_orders()) if resolved else None,
             'snapshot_sequence': seq if type(seq) is int else None}
    if own_liquidation:
        # 1.0.9.1: present only when non-zero, so every other report is unchanged.
        facts['own_liquidation'] = own_liquidation
    return facts


def summary(followers, main_executor=None, trade_id=None, publisher=None, *, login_id=None, balances=None,
            clock_ms=None):
    """1.1.0: with `login_id` (only when config.json names its login, or on a followers_only login)
    the report and every row carry it; without it the report is exactly the 1.0.9.5.2 report.
    `balances`/`clock_ms` stand in for the main executor's on a followers_only login."""
    # 1.0.9.2: balances only with config.json's existing `account_view_enabled` (the switch for
    # GET /account); without it every row is exactly the 1.0.9.1 row.
    if balances is None:
        balances = main_executor is not None and main_executor.cfg.get('account_view_enabled') is True
    rows = []
    for key, ex in followers.items():
        if isinstance(ex, Unavailable):
            rows.append({'key': key, 'available': False, 'reason': ex.reason})
            continue
        rows.append(dict(_row(ex, trade_id, balances), key=key))
    out = {'schema': 'tvbridge.followers.v2', 'observed_at': None, 'followers': rows}
    if main_executor is not None:
        out['main'] = dict(_row(main_executor, trade_id, balances), key='main')
        if trade_id is not None:
            out['main']['leader'] = leader_facts(main_executor, trade_id, publisher)
        out['observed_at'] = main_executor.clock_ms()
    elif clock_ms is not None:
        out['observed_at'] = clock_ms()
    if login_id is not None:
        out['login_id'] = login_id
        for row in rows + ([out['main']] if 'main' in out else []):
            row['login_id'] = login_id
    return out


def make_follower_handler(base, followers, token, main_executor=None, publisher=None, *, login_id=None,
                          balances=None, clock_ms=None, read_token=None):
    """Wrap the 1.0.6 handler class: /follower and /followers paths here, everything else unchanged.
    1.0.9: `publisher` (the daemon's evidence publisher, read-only) numbers the leader's snapshots.
    1.1.0: `login_id`, `balances`, `clock_ms` go to summary() (all None: the 1.0.9.5.2 report).
    1.1.0: `read_token` (config read_token_env) is also accepted for the read-only GET /followers report,
    never for /follower/... routes."""

    class Handler(base):
        def _follower(self, parts):
            # /follower/<token>/<key>/<route>
            if len(parts) != 5 or parts[1] != 'follower' or not protocol.same_secret(parts[2], token):
                return None, None
            return followers.get(parts[3]), parts[4]

        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            parts = split.path.split('/')
            if len(parts) > 1 and parts[1] == 'followers':
                ok = len(parts) == 3 and protocol.same_secret(parts[2], token)
                if len(parts) == 3 and read_token:
                    ok = protocol.same_secret(parts[2], read_token) or ok
                if not ok:
                    return self.respond(404, {'error': 'not found'})
                try:
                    query = urllib.parse.parse_qs(split.query, max_num_fields=2)
                except ValueError:
                    return self.respond(400, {'error': 'invalid request'})
                trade_ids = query.get('trade_id')
                if set(query) - {'trade_id'} or (trade_ids and (len(trade_ids) != 1 or len(trade_ids[0]) > 240)):
                    return self.respond(400, {'error': 'invalid request'})
                return self.respond(200, summary(followers, main_executor, trade_ids[0] if trade_ids else None,
                                                          publisher, login_id=login_id, balances=balances,
                                                          clock_ms=clock_ms))
            if len(parts) > 1 and parts[1] == 'follower':
                ex, route = self._follower(parts)
                if ex is None or route != 'state':
                    return self.respond(404, {'error': 'not found'})
                try:
                    query = urllib.parse.parse_qs(split.query, max_num_fields=2)
                except ValueError:
                    return self.respond(400, {'error': 'invalid request'})
                trade_ids = query.get('trade_id')
                if set(query) - {'trade_id'} or (trade_ids and (len(trade_ids) != 1 or len(trade_ids[0]) > 240)):
                    return self.respond(400, {'error': 'invalid request'})
                if isinstance(ex, Unavailable):
                    return self.respond(503, {'error': 'follower unavailable', 'reason': ex.reason})
                report = account_state(ex, trade_ids[0] if trade_ids else None, extended=True)
                report['follower'] = ex.key
                if trade_ids:
                    report['trade'] = trade_view(ex, trade_ids[0])
                return self.respond(200, report)
            return super().do_GET()

        def do_POST(self):
            parts = self.path.split('/')
            if len(parts) > 1 and parts[1] == 'follower':
                ex, route = self._follower(parts)
                if ex is None or route not in ('order', 'kill'):
                    return self.respond(404, {'error': 'not found'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 40960:
                        raise ValueError('body size')
                    payload = json.loads(self.rfile.read(size))
                except (ValueError, OSError):
                    return self.respond(400, {'error': 'invalid request'})
                if route == 'kill':
                    # 1.0.8: this follower's kill switch only (risk-reducing; entries refused until a
                    # person removes the file). The reason is logged, never an order body.
                    reason = payload.get('reason') if isinstance(payload, dict) else None
                    if not isinstance(reason, str) or not 0 < len(reason) <= 200:
                        return self.respond(400, {'error': 'invalid request'})
                    if isinstance(ex, Unavailable):
                        return self.respond(503, {'error': 'follower unavailable', 'reason': ex.reason})
                    done = ex._set_kill_switch('firing: ' + reason)
                    return self.respond(200, {'kill_switch': done, 'entries_disabled': ex.entries_disabled(),
                                              'follower': ex.key})
                if isinstance(ex, Unavailable):
                    return self.respond(200, {'outcome': 'rejected',
                                              'receipt': {'bridge': 'refused', 'reason': 'follower_' + ex.reason}})
                outcome, receipt = ex.send(payload)
                return self.respond(200, {'outcome': outcome, 'receipt': dict(receipt, follower=ex.key)})
            return super().do_POST()

    Handler.__name__ = 'FollowerHandler'
    return Handler


def verify(cfg, report):
    """Read-only check that each configured follower is pinned in the page's own account sync:
    available, the configured account id, and the configured name (by SHA-256). Returns (ok, rows)."""
    rows, ok = [], True
    live = {r['key']: r for r in report.get('followers', [])}
    for key, ecfg, enabled in parse(cfg):
        r = live.get(key) or {}
        name_hash = hashlib.sha256(ecfg['account_name'].encode()).hexdigest()
        checks = {'available': r.get('available') is True,
                  'account_id': ecfg['account_id'] is None or r.get('account_id') == ecfg['account_id'],
                  'account_name': r.get('account_name_sha256') == name_hash,
                  'synced': r.get('synced') is True}
        good = all(checks.values())
        ok = ok and (good or not enabled)
        rows.append({'key': key, 'enabled': enabled, 'ok': good, 'checks': checks,
                     'account_id': r.get('account_id'), 'reason': r.get('reason')})
    return ok, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description='Read-only follower checks against the running bridge daemon.')
    ap.add_argument('--config', required=True, help='the daemon config.json')
    ap.add_argument('--verify', action='store_true', required=True,
                    help='GET /followers and prove each follower name/id pairing from the page sync')
    ap.add_argument('--base', default='http://127.0.0.1:8777')
    args = ap.parse_args(argv)
    cfg = json.loads(open(args.config).read())
    token = os.environ.get('TVBRIDGE_TOKEN', '')
    if len(token) < 24:
        raise SystemExit('TVBRIDGE_TOKEN of at least 24 characters required')
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(args.base.rstrip('/') + '/followers/' + token, timeout=5) as r:
        report = json.loads(r.read(262145))
    ok, rows = verify(cfg, report)
    print(json.dumps({'ok': ok, 'followers': rows}, indent=1, sort_keys=True))
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
