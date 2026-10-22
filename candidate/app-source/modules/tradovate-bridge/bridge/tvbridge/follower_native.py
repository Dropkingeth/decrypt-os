"""Native exits for follower accounts (1.0.8; 1.0.9): each follower runs the leader's native exit
flow on its OWN account, independently of the leader and of the other followers.

Soup trades only the leader account and knows nothing about followers. For a trade that
Soup pinned as native (`trade.native`, Soup's native-exits release), a follower lane in
`follower_exits: "native"` mode does not copy Soup's stop commands. It reads Soup's decisions
(entry, runner exits, terminal reasons, the end of the trade) from Soup's database READ-ONLY,
and runs the same state machine as Soup's advance_native() against its own account's fills:

1. Entry: after the entry gate (the leader's send accepted) and alignment, one bracket_entry
   (market entry + resting STOP at the payload initial_hard_stop_price, sized to the follower's
   quantity), then its own TP1 limit (at the same working price as the leader's). A follower
   bracket the bridge refuses is not replaced by a plain entry: that follower skips the trade. The leader and the other followers are unaffected.
   1.0.9 (if it did not enter, leave it alone): an entry refused or rejected with zero fill
   (refused by the bridge, or rejected by Tradovate after it was accepted) sends nothing more for
   that trade on that follower, sets no kill switch or block file, and is logged once as the
   informational alert `follower_entry_skipped` (level "info"). The TP1 limit is sent only once
   the follower's own entry shows a fill (the bridge holds the leader's TP1 for the same fill).
   Anything after any fill stays a normal fault path.
2. Stop proven working at the payload price for the filled quantity within stop_confirm_ms,
   else this follower continues synthetically (alert).
3. The follower's OWN TP1 fill (not the leader's): one modify_stop to (break_even_price, its own
   remaining runners), proven from evidence within modify_confirm_ms. The bridge's auto-protect
   housekeeping for the follower also shrinks an oversized stop within milliseconds.
3a. A failed break-even move: cancel the follower's stop, prove it gone within
   cancel_confirm_ms, then continue synthetically (break-even watcher on Soup's recorded quotes
   and bars). A failed cancel: exit-all. Before the follower's TP1 a failed resize exits all.
4. Runner exits: the leader's runner decisions (Soup's recorded reductions and reduce commands)
   apply to the same tranches on the follower once the follower's own TP1 has filled at or before
   the decision. Stop shrink first (proven), then the market reduce. The last runner, any leader
   terminal reason (EOD, strategy exits before TP1, the leader's stop fills, a leader exit-all,
   1.0.9: Soup's `external_flat_unreconciled` and `native_broker_view_stale`), and the leader's
   trade ending all go through the follower's own exit-all.
4a. 1.0.9: the leader closed outside the bridge (by hand, or by the prop firm) while this
   follower is in the same trade: exit-all at once, alert `native_exit` with the reason
   `leader_closed_externally` (once per follower and trade). Trigger on whichever comes first:
   Soup's terminal `external_flat_unreconciled`, or the firing service's own view of the leader
   (LeaderFlatWatch: flat, nothing working, the bridge's own fills do not explain it, Soup's trade
   still open) on 2 consecutive leader account snapshots. It does not wait for Soup.
5. Exit-all: flatten_all on the follower account, verify flat with nothing working from the
   follower's own fresh state, up to max_exit_attempts; else the follower's kill switch (daemon
   route), a lane block file (ENTRIES-BLOCKED-NATIVE-EXIT) and an alert. Never the leader's.
6. Backstop: a quote/bar through the follower's initial stop (before its TP1) or through
   break-even (after its TP1 fill) with the position still open after backstop_ms: exit-all.

Every command has a deterministic id (`native-<step>:<trade>[:n]`), an fsynced intent before it
is sent and is never sent twice; an unknown outcome is never retried under the same id. State
changes are journaled as `native` records, so a restarted service resumes without replaying.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger('tvbridge.firing')


def _reject_detail(view):
    """1.0.9.5.2: {'reject_reason': Tradovate's text} from a bridge receipt or order view, else {}.
    The bridge already bounds and sanitizes it; re-bounded here so a note stays one short line."""
    why = view.get('reject_reason') if isinstance(view, dict) else None
    if not isinstance(why, str) or not why.strip():
        return {}
    return {'reject_reason': ' '.join(why.split())[:300]}


def _mirror_send(lane, tr, command_id, kind, payload, started_at, source):
    """The mirror's own send (fsynced intent, outcome), without FollowerLane's alert."""
    from .mirror import Mirror
    return Mirror._send(lane, tr, command_id, kind, payload, started_at, source)

FOLLOWER_EXITS = ('mirror', 'native')
# Soup's own bounds (core.NATIVE_BOUNDS); the leader's configured values live in native-exits.json.
NATIVE_BOUNDS = {'modify_confirm_ms': (500, 10000), 'cancel_confirm_ms': (2000, 30000),
                 'stop_confirm_ms': (1000, 30000), 'backstop_ms': (250, 10000), 'exit_verify_ms': (1000, 30000),
                 'reduce_confirm_ms': (1000, 30000), 'max_exit_attempts': (1, 5)}
NATIVE_DEFAULTS = {'modify_confirm_ms': 2500, 'cancel_confirm_ms': 5000, 'stop_confirm_ms': 5000,
                   'backstop_ms': 2000, 'exit_verify_ms': 5000, 'reduce_confirm_ms': 5000, 'max_exit_attempts': 3}
TRANCHES = ('tp1', 'parent', 'bias', 'eod')
BLOCK_NAME = 'ENTRIES-BLOCKED-NATIVE-EXIT'
# 1.0.9: the leader closed outside the bridge (Soup's reason, and the follower exit reason for it).
SOUP_EXTERNAL_CLOSE = 'external_flat_unreconciled'
LEADER_CLOSED_EXTERNALLY = 'leader_closed_externally'
EXTERNAL_SNAPSHOTS = 2          # consecutive leader snapshots, as Soup's EXTERNAL_CLOSE_SNAPSHOTS (fix a)
SNAPSHOT_FALLBACK_MS = 2000     # without a snapshot sequence: reads this far apart count as distinct
ENTRY_SKIPPED = 'follower_entry_skipped'  # informational alert kind (level "info"), never a fault
# Exit reasons that are the normal end of a trade; every other reason is a follower fault and alerts.
NORMAL_EXITS = ('leader:', 'broker_stop_', 'flat_after_exits', 'leader_trade_ended', 'eod')
# Follower journal kinds whose fills take contracts out of the trade (with the stop leg's fills).
OUT_KINDS = ('tp1_limit', 'bracket_tp1', 'exit_market', 'flatten_all', 'auto_flatten')


def validate_native(cfg):
    """(`follower_exits` mode, native policy or None). Raises ValueError. Sends nothing."""
    mode = cfg.get('follower_exits', 'mirror')
    if mode not in FOLLOWER_EXITS:
        raise ValueError('follower_exits must be "mirror" or "native"')
    raw = cfg.get('follower_native')
    if mode == 'mirror':
        if raw is not None:
            raise ValueError('follower_native is only valid with follower_exits "native"')
        return mode, None
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - set(NATIVE_BOUNDS):
        raise ValueError('follower_native: unknown or invalid fields')
    policy = dict(NATIVE_DEFAULTS, **raw)
    for key, (low, high) in NATIVE_BOUNDS.items():
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise ValueError(f'follower_native.{key} must be an integer from {low} to {high}')
    return mode, policy


def native_plan(trade):
    """The follower's native plan from Soup's trade, or None (the trade is then mirrored as in 1.0.7).

    Only trades Soup itself pinned as native (Soup's native_plan passed: an enabled initial hard
    stop, a TP1 tranche, the original allocation). Payload values are used exactly.
    """
    if not isinstance(trade, dict) or not isinstance(trade.get('native'), dict):
        return None
    p = trade.get('payload') or {}
    alloc = trade.get('alloc') or {}
    qp = trade.get('quantity_policy') or {}
    try:
        ok = (p.get('initial_hard_stop_enabled') is True and float(p['initial_hard_stop_price']) > 0
              and float(p['break_even_price']) > 0 and int(alloc.get('tp1', 0)) > 0
              and qp.get('exit_allocation') == 'original' and trade.get('side') in ('long', 'short'))
    except (KeyError, TypeError, ValueError):
        return None
    if not ok or set(alloc) - set(TRANCHES):
        return None
    policy = trade.get('execution_policy') or {}
    return {'side': trade['side'], 'initial': p['initial_hard_stop_price'], 'break_even': p['break_even_price'],
            'tp1_limit': policy.get('tp1_working_limit', p['tp1_price']), 'tp1_price': p['tp1_price'],
            'alloc': {k: int(alloc.get(k, 0)) for k in TRANCHES},
            'chart_ms': int((p.get('execution_context') or {}).get('chart_seconds') or 5) * 1000}


def leader_view(trade, commands, now):
    """What the follower needs from Soup's current trade: decisions only, never Soup's stop commands."""
    decisions = {}
    for c in sorted((commands or {}).values(), key=lambda c: c.get('started_at') or 0):
        if c.get('kind') == 'reduce' and c.get('started_at') is not None:
            for k, v in (c.get('alloc') or {}).items():
                if v:
                    decisions.setdefault(k, (c.get('reason') or 'reduce', c.get('created_at') or c['started_at']))
    for k, reason in sorted((trade.get('reductions') or {}).items()):
        decisions.setdefault(k, (reason, now))
    return {'terminal_reason': trade.get('terminal_reason'), 'phase': trade.get('phase'),
            'flatten': any(c.get('kind') == 'flatten' and c.get('started_at') is not None
                           for c in (commands or {}).values()),
            'decisions': decisions}


def leader_flat_candidate(trade, report):
    """The bridge's own view (GET /followers?trade_id=, `main` row and `main.leader`): True when the
    leader is flat with nothing working, the bridge's own fills for this trade do not explain it
    (entry filled, fewer contracts out through the bridge than in, no reversal), no command of the
    trade is unresolved, and Soup's trade is still open. 1.0.9.1: never after the bridge's own accepted
    liquidation of this trade (`own_liquidation`): liquidateposition closes whatever is open, so a flat
    leader is explained by the bridge even when the exit order itself could not be identified."""
    if not isinstance(trade, dict) or trade.get('phase') == 'closed' or not isinstance(report, dict):
        return False
    main = report.get('main') or {}
    lf = main.get('leader') or {}
    if lf.get('trade_id') != trade.get('id') or main.get('synced') is not True:
        return False
    if lf.get('own_liquidation'):
        return False
    if main.get('net_position') != 0 or lf.get('net_position') != 0 or main.get('orders') or lf.get('open_orders') \
            or lf.get('unseen') or lf.get('unresolved'):
        return False
    entry, out = lf.get('entry_filled'), lf.get('exit_filled')
    return type(entry) is int and type(out) is int and entry > 0 and 0 <= out < entry


class LeaderFlatWatch:
    """1.0.9: the leader went flat outside the bridge, from the bridge's own view.

    One instance in the firing supervisor. observe() is fed every (Soup trade, /followers report)
    pair it reads. A candidate must hold on EXTERNAL_SNAPSHOTS consecutive leader account snapshots
    (distinct publisher snapshot sequences; without one, reads SNAPSHOT_FALLBACK_MS apart), with
    every read in between a candidate too: the same debounce as Soup's external-close fix (a).
    Returns the fact once per trade, else None. Sends nothing.
    """

    def __init__(self, snapshots=EXTERNAL_SNAPSHOTS, fallback_ms=SNAPSHOT_FALLBACK_MS):
        self.snapshots, self.fallback_ms = snapshots, fallback_ms
        self.tid, self.seen, self.fired = None, [], {}

    def observe(self, trade, report, now):
        tid = trade.get('id') if isinstance(trade, dict) else None
        if tid != self.tid:
            self.tid, self.seen = tid, []
        if tid is None or tid in self.fired:
            return None
        if not leader_flat_candidate(trade, report):
            self.seen = []
            return None
        lf = report['main']['leader']
        seq = lf.get('snapshot_sequence')
        last = self.seen[-1] if self.seen else None
        if last is None or (seq is not None and seq != last['sequence']) or \
                (seq is None and now - last['at'] >= self.fallback_ms):
            self.seen.append({'sequence': seq, 'at': now})
        if len(self.seen) < self.snapshots:
            return None
        fact = {'trade_id': tid, 'at': now, 'source': 'bridge_view', 'entry_filled': lf['entry_filled'],
                'bridge_exit_filled': lf['exit_filled'], 'external': lf['entry_filled'] - lf['exit_filled'],
                'snapshots': [x['sequence'] for x in self.seen], 'first_at': self.seen[0]['at']}
        self.fired[tid] = fact
        return fact


class NativeLog:
    """The `native` records of a lane journal (MirrorJournal ignores them): state per trade."""

    def __init__(self, path):
        self.by_trade = {}
        path = Path(path)
        if not path.exists():
            return
        for line in path.read_text().split('\n'):
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # MirrorJournal already refused a torn line in the middle
            if isinstance(rec, dict) and rec.get('type') == 'native':
                self.by_trade.setdefault(rec['trade_id'], []).append(rec)


class NativeFollower:
    """Mixin for tvbridge.firing.FollowerLane: the per-follower native state machine."""

    native_policy = None  # set by FollowerLane when follower_exits is "native"

    # --- setup, persistence -------------------------------------------------------------

    def _native_init(self, policy):
        self.native_policy = policy
        self.nat = {}          # trade id -> state
        self.leader = {}       # trade id -> leader_view(...) of Soup's trade
        self.market = {}       # Soup's last_quote / last_bar
        self.leader_external = {}  # 1.0.9: trade id -> LeaderFlatWatch fact (set by the firing supervisor)
        self.block_path = self.dir / BLOCK_NAME
        if policy is None:
            return
        for tid, recs in NativeLog(self.journal.path).by_trade.items():
            if tid in self.journal.reconciled:
                continue
            st = None
            for rec in recs:
                st = self._native_apply(st, rec)
            if st is not None and not st['done']:
                self.nat[tid] = st
                log.info('follower %s: resuming native trade %s (mode %s, exit %s)', self.key, tid,
                         st['mode'], (st['exit'] or {}).get('reason'))

    def _native_record(self, st, event, **detail):
        rec = dict(detail, type='native', trade_id=st['tid'] if st else detail['trade_id'], event=event,
                   at=self.clock_ms())
        self.journal.append(rec)
        return self._native_apply(st, rec)

    @staticmethod
    def _native_apply(st, rec):
        """Fold one native record into the trade state (also used to rebuild after a restart)."""
        ev = rec['event']
        if ev == 'start':
            return {'tid': rec['trade_id'], 'plan': rec['plan'], 'entry_cid': rec['entry_cid'],
                    'quantity': rec['quantity'], 'started_at': rec['at'], 'mode': 'native', 'fallback': None,
                    'stop_confirmed': False, 'tp1_cid': None, 'modify': None, 'stop_cancel': None, 'reduce': None,
                    'reduced': {k: 0 for k in TRANCHES}, 'done_decisions': {}, 'backstop': None, 'exit': None,
                    'n': 0, 'done': False, 'failed': None, 'skipped': None}
        if st is None:
            return None
        if ev == 'entry_retry':
            st['entry_cid'] = rec['command_id']
        elif ev == 'stop_confirmed':
            st['stop_confirmed'] = True
        elif ev == 'fallback':
            st['mode'], st['fallback'], st['backstop'] = 'synthetic', rec['code'], None
        elif ev == 'tp1':
            st['tp1_cid'] = rec['command_id']
        elif ev == 'modify':
            st['modify'] = {'cid': rec['command_id'], 'qty': rec['quantity'], 'price': rec['price'],
                            'sent_at': rec['at'], 'confirmed': False}
        elif ev == 'modify_confirmed':
            if st['modify']:
                st['modify']['confirmed'] = True
        elif ev == 'stop_cancel':
            st['stop_cancel'] = {'cid': rec['command_id'], 'reason': rec['reason'], 'sent_at': rec['at']}
        elif ev == 'reduce':
            st['reduce'] = {'cid': rec['command_id'], 'qty': rec['quantity'], 'tranches': rec['tranches'],
                            'sent_at': rec['at']}
        elif ev == 'reduce_filled':
            for k, q in (rec.get('tranches') or {}).items():
                st['reduced'][k] = st['reduced'].get(k, 0) + q
            st['reduce'] = None
        elif ev == 'decision':
            st['done_decisions'][rec['tranche']] = rec['result']
        elif ev == 'backstop':
            st['backstop'] = {'reason': rec['reason'], 'at': rec['crossed_at']}
        elif ev == 'exit':
            st['exit'] = {'reason': rec['reason'], 'at': rec['at'], 'attempts': []}
        elif ev == 'skipped':
            st['skipped'] = rec.get('reason') or 'entry_skipped'
        elif ev == 'exit_attempt':
            if st['exit'] is not None:
                st['exit']['attempts'].append(rec['command_id'])
        elif ev == 'failed':
            st['failed'] = rec.get('reason')
        elif ev == 'done':
            st['done'] = True
        if ev in ('modify', 'stop_cancel', 'reduce', 'exit_attempt', 'tp1'):
            st['n'] += 1
        return st

    # --- Soup side ------------------------------------------------------------------------

    def _native_observe(self, data, now):
        """Called after every Soup read: the leader's decisions for the current trade, market data."""
        trade = (data or {}).get('trade')
        self.market = {'quote': (data or {}).get('last_quote'), 'bar': (data or {}).get('last_bar')}
        if trade:
            prev = self.leader.get(trade['id']) or {}
            view = leader_view(trade, data.get('commands'), now)
            # A decision keeps the time it was first seen (the Soup reduce command's own time if known).
            for k, d in (prev.get('decisions') or {}).items():
                if k in view['decisions'] and d[1] <= view['decisions'][k][1]:
                    view['decisions'][k] = d
            self.leader[trade['id']] = view
        for tid in list(self.nat):
            tr = self.trades.get(tid)
            if tid not in self.leader or (trade and tid != trade['id']) or (tr and tr.get('replaced')):
                v = self.leader.setdefault(tid, {'terminal_reason': None, 'phase': None, 'flatten': False,
                                                 'decisions': {}})
                if not trade or tid != trade['id']:
                    v['replaced'] = True

    # --- entry ----------------------------------------------------------------------------

    def _native_entry(self, info, c):
        """The follower's bracket entry for a native Soup trade (after the gate and alignment)."""
        tid, plan = info['id'], info['native']
        tr = self._track(tid, info.get('side'))
        if tid in self.nat or any(r['event'] == 'start' for r in NativeLog(self.journal.path).by_trade.get(tid, [])):
            return self.journal.skip('skipped', c, tid, reason='native_entry_already_sent')
        qty = c.get('quantity')
        marker = self._quantity_limit(info)  # the pinned policy marker; raises if the policy file differs
        if type(qty) is not int or qty <= 0 or (marker == 1 and qty != 1) or \
                qty != sum(plan['alloc'].values()):
            self.journal.skip('payload_error', c, tid, error='native entry quantity does not match the allocation')
            return self._error(f'native entry {c["id"]}: quantity {qty} does not match the allocation')
        payload = {'ticker': self.ticker, 'action': 'buy' if plan['side'] == 'long' else 'sell',
                   'orderType': 'market', 'quantity': qty,
                   'extras': {'soup_command_id': c['id'], 'soup_trade_id': tid, 'bridge_kind': 'bracket_entry',
                              'reference_price': c['price'], 'stop_price': plan['initial']}}
        # The start record (the plan) is fsynced before the entry intent: a restart always knows the plan.
        st = self._native_record(None, 'start', trade_id=tid, plan=plan, entry_cid=c['id'], quantity=qty)
        self.nat[tid] = st
        # First attempt without the lane's automatic alert: a hedge-guard refusal here is usually the
        # race below, and the retry decides. Any other refusal or an unknown is alerted as usual.
        outcome, receipt = _mirror_send(self, tr, c['id'], 'entry', payload, c['started_at'], 'native')
        if outcome == 'rejected' and receipt.get('bridge') == 'refused' and \
                str(receipt.get('reason') or '').startswith('hedge guard:'):
            # Followers fire in parallel. A peer's (or the leader's) bracket stop leg can be reported a
            # moment before its entry fill's position, and this follower's hedge guard then sees a
            # "working order that could open a short". The guard refused before sending anything, so
            # one retry under a new id is safe: after the exposure clears, within align_wait_ms and
            # the send-delay budget. A real conflict (another account) refuses again: skipped.
            deadline = time.monotonic() + min(self.align_wait_ms, max(
                0, self.cfg['max_send_delay_ms'] - (self.clock_ms() - c['started_at']) - 500)) / 1000.0
            while not self._peers_clear(plan['side']) and time.monotonic() < deadline and not self.stopping.is_set():
                time.sleep(0.05)
            retry = c['id'] + ':retry'
            payload = dict(payload, extras=dict(payload['extras'], soup_command_id=retry))
            st = self._native_record(st, 'entry_retry', command_id=retry,
                                     first_reason=str(receipt.get('reason'))[:200])
            outcome, receipt = _mirror_send(self, tr, retry, 'entry', payload, c['started_at'], 'native')
        if outcome == 'unknown':
            # Maybe placed: a normal fault path (the step exits all with `entry_unknown`).
            self.alerts.add(self.key, 'order_unknown', trade_id=tid, command_id=st['entry_cid'], order_kind='entry',
                            source='native', reason=str(receipt.get('reason') or receipt.get('bridge') or
                                                        receipt.get('transport') or '')[:200])
        elif outcome == 'rejected':
            # Definitely not placed. Unlike Soup's leader, a follower does not fall back to a plain
            # (unprotected) entry: it skips this trade. 1.0.9: informational, never a fault.
            self._native_skip(st, 'entry_refused', command_id=st['entry_cid'],
                              refusal=str(receipt.get('reason') or receipt.get('failureText')
                                          or receipt.get('bridge') or '')[:160],
                              **_reject_detail(receipt))
        return outcome

    def _native_skip(self, st, reason, **detail):
        """1.0.9: the follower's entry was refused or rejected with zero fill. Send nothing
        more for this trade on this follower, set no kill switch or block file, tell once (info)."""
        if st['done']:
            return
        if not st.get('skipped'):
            self._native_record(st, 'skipped', reason=reason, **detail)
            self.alerts.add(self.key, ENTRY_SKIPPED, level='info', trade_id=st['tid'], reason=reason, **detail)
        self._native_finish(st, 'entry_skipped', skip_reason=reason, **detail)

    # --- the step --------------------------------------------------------------------------

    def _native_poll(self):
        for tid, st in list(self.nat.items()):
            if st['done'] or self.stopping.is_set():
                continue
            try:
                self._native_step(st)
            except Exception as exc:  # noqa: BLE001 - one bad step must not stop the lane
                self._error(f'native step {tid} failed: {type(exc).__name__}: {exc}')
                log.exception('native step failed')

    def _state(self, tid):
        try:
            s = self.bridge_state.get(tid)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return s

    def _native_step(self, st):
        tid, pol, plan = st['tid'], self.native_policy, st['plan']
        tr = self._track(tid, plan['side'])
        now = self.clock_ms()
        entry = self.journal.get(st['entry_cid'])
        if entry is None or entry.get('type') != 'intent':
            return self._native_finish(st, 'entry_not_sent')
        if entry['outcome'] == 'rejected':
            return self._native_skip(st, 'entry_refused', command_id=st['entry_cid'])
        s = self._state(tid)
        fresh = bool(s) and s['synced']
        if st['exit']:
            return self._native_exit_step(st, s, now)
        view = (s or {}).get('trade') or {}
        cmds = view.get('commands') or {}
        stop = view.get('stop')
        sign = 1 if plan['side'] == 'long' else -1
        held = sign * s['net_position'] if fresh else None
        ent = cmds.get(st['entry_cid']) or {}
        tp1 = cmds.get(st['tp1_cid']) if st['tp1_cid'] else None
        tp1_done = bool(tp1) and bool(tp1.get('quantity')) and tp1.get('filled') == tp1.get('quantity')
        leader = self.leader.get(tid) or {}
        # A fill, its order update and the position update arrive as separate events. Act on the
        # position only when this trade's own fills explain it (as the bridge's _consistent_held).
        out_qty = (stop or {}).get('filled') or 0
        out_qty += sum(c.get('filled') or 0 for c in cmds.values() if c.get('kind') in OUT_KINDS)
        consistent = fresh and (ent.get('filled') or 0) - out_qty == held
        if fresh and not consistent:
            st.setdefault('inconsistent_since', now)
        else:
            st.pop('inconsistent_since', None)

        # 0. 1.0.9: an entry Tradovate rejected (or cancelled) with zero fill, on a flat account with
        # nothing working: the follower did not enter. Leave it alone: no exit-all, no alert fault.
        # A bracket leg still working, or a position, is waited out for stop_confirm_ms, then the
        # normal fault path below decides.
        if entry['outcome'] == 'accepted' and fresh and ent.get('seen') and not ent.get('working') \
                and not (ent.get('filled') or 0) and not out_qty:
            if held == 0 and not s['open_order_ids'] and not s['unseen_accepted_orders']:
                return self._native_skip(st, 'entry_rejected_no_fill', command_id=st['entry_cid'],
                                         status=str(ent.get('status'))[:40], **_reject_detail(ent))
            if now - st['started_at'] <= pol['stop_confirm_ms']:
                return

        # 1. Terminal conditions: exit-all.
        reason, detail = None, {}
        external = self.leader_external.get(tid)
        if entry['outcome'] == 'unknown':
            reason = 'entry_unknown'
        elif leader.get('terminal_reason') == SOUP_EXTERNAL_CLOSE:
            reason, detail = LEADER_CLOSED_EXTERNALLY, {'source': 'soup'}
        elif external:
            reason, detail = LEADER_CLOSED_EXTERNALLY, {'source': external.get('source', 'bridge_view'),
                                                        'snapshots': external.get('snapshots')}
        elif leader.get('terminal_reason'):
            reason = 'leader:' + str(leader['terminal_reason'])
        elif leader.get('flatten'):
            reason = 'leader:flatten'
        elif leader.get('replaced') or leader.get('phase') == 'closed':
            reason = 'leader_trade_ended'
        elif fresh:
            if stop and (stop.get('filled') or 0) > 0:
                reason = 'broker_stop_' + ('break_even' if stop.get('stop_price') == plan['break_even'] else 'initial')
            elif held is not None and held < 0:
                reason = 'position_reversed'
            elif ent.get('seen') and not ent.get('working') and (ent.get('filled') or 0) < st['quantity']:
                reason = 'entry_incomplete'
            elif tp1 and tp1.get('seen') and not tp1.get('working') and not tp1_done:
                reason = 'tp1_order_' + str(tp1.get('status')).lower()
            elif st['tp1_cid'] and (self.journal.get(st['tp1_cid']) or {}).get('outcome') in ('rejected', 'unknown'):
                reason = 'tp1_' + self.journal.get(st['tp1_cid'])['outcome']
            elif consistent and held == 0 and (ent.get('filled') or 0) > 0 and not st['reduce']:
                reason = 'flat_after_exits'
            elif st.get('inconsistent_since') is not None and \
                    now - st['inconsistent_since'] > pol['reduce_confirm_ms']:
                reason = 'position_mismatch'  # a fill this trade did not make (e.g. by hand): close it
        if reason is None and st['backstop'] and held and held > 0 and \
                now - st['backstop']['at'] >= pol['backstop_ms'] and st['mode'] == 'native':
            reason = 'backstop_' + st['backstop']['reason']
        if reason:
            return self._native_exit(st, reason, s, now, **detail)

        # 2. The follower's own TP1, once its entry is accepted AND shows a fill (Soup sends the leader's
        # at the entry receipt; the bridge's guard then holds it for the entry's fill). 1.0.9: so a
        # zero-fill rejection never gets a TP1 request.
        if st['tp1_cid'] is None and entry['outcome'] == 'accepted' and fresh and (ent.get('filled') or 0) > 0:
            return self._native_send_tp1(st, tr)
        if not consistent or (ent.get('filled') or 0) <= 0:
            if st['mode'] == 'native' and not st['stop_confirmed'] and \
                    now - st['started_at'] > pol['stop_confirm_ms']:
                self._native_fallback(st, 'stop_not_confirmed_working')
            return
        tp1_at = tp1.get('fill_ms') if tp1_done else None
        self._native_crossings(st, held, tp1_done, tp1_at, now)
        if st['mode'] == 'native':
            return self._native_manage(st, tr, s, stop, ent, held, tp1_done, tp1_at, now)
        return self._native_synthetic(st, tr, s, held, tp1_done, tp1_at, now)

    # --- native mode -----------------------------------------------------------------------

    def _native_manage(self, st, tr, s, stop, ent, held, tp1_done, tp1_at, now):
        pol, plan = self.native_policy, st['plan']
        working = bool(stop) and stop.get('status') == 'Working' and not stop.get('filled')
        # 1. The resting stop must be proven working at the payload price for the filled quantity.
        if not st['stop_confirmed']:
            if working and stop.get('quantity') == ent.get('filled') and stop.get('stop_price') == plan['initial']:
                self._native_record(st, 'stop_confirmed', quantity=stop.get('quantity'), price=stop.get('stop_price'))
            elif (stop and stop.get('seen') and not stop.get('working')) or \
                    (stop is None and ent.get('seen') and not ent.get('working')) or \
                    now - st['started_at'] > pol['stop_confirm_ms']:
                return self._native_fallback(st, 'stop_not_confirmed_working')
            else:
                return
        # 3a. A failed stop move after TP1: the stop is being cancelled, then synthetic.
        cancel = st['stop_cancel']
        if cancel:
            out = (self.journal.get(cancel['cid']) or {}).get('outcome')
            gone = bool(stop) and stop.get('status') in ('Canceled', 'Rejected', 'Expired') and not stop.get('filled') \
                and stop['broker_order_id'] not in s['open_order_ids']
            if gone and s['observed_at'] >= cancel['sent_at']:
                return self._native_fallback(st, 'stop_move_failed:' + cancel['reason'])
            if out == 'rejected':
                return self._native_exit(st, 'native_stop_cancel_rejected', s, now)
            if now - cancel['sent_at'] > pol['cancel_confirm_ms']:
                return self._native_exit(st, 'native_stop_cancel_unconfirmed', s, now)
            return
        # 3. One stop modify at a time; confirmed only by evidence.
        m = st['modify']
        if m and not m['confirmed']:
            out = (self.journal.get(m['cid']) or {}).get('outcome')
            if working and stop.get('quantity') == m['qty'] and stop.get('stop_price') == m['price'] and \
                    s['observed_at'] >= m['sent_at'] and out in ('accepted', 'unknown'):
                self._native_record(st, 'modify_confirmed', command_id=m['cid'])
            elif out == 'rejected':
                return self._native_modify_failed(st, 'stop_modify_rejected', tp1_done, s, now)
            elif now - m['sent_at'] > pol['modify_confirm_ms']:
                return self._native_modify_failed(st, 'stop_modify_unconfirmed', tp1_done, s, now)
            else:
                return
        # 4. A market reduce in flight (the stop was shrunk for it first).
        if st['reduce']:
            return self._native_reduce_check(st, s, now)
        if not working:
            if stop and stop.get('status') in ('Canceled', 'Rejected', 'Expired'):
                return self._native_fallback(st, 'stop_no_longer_working')
            return
        # 5. Desired stop: own remaining runners minus pending runner exits, at break-even after OWN TP1.
        reserved, tranches, why = self._native_pending_exits(st, held, tp1_done, tp1_at, now)
        if reserved >= held:
            return self._native_exit(st, why, s, now)  # the planned exit takes every remaining contract
        want_qty = held - reserved
        want_price = plan['break_even'] if tp1_done else plan['initial']
        if (stop.get('quantity') - (stop.get('filled') or 0), stop.get('stop_price')) != (want_qty, want_price):
            return self._native_send_modify(st, tr, want_qty, want_price,
                                            'break_even' if want_price != stop.get('stop_price') else 'resize')
        if reserved:
            return self._native_send_reduce(st, tr, tranches, why)

    def _native_modify_failed(self, st, reason, tp1_done, s, now):
        """As Soup does: after the TP1 fill a failed stop move cancels the stop, proves it
        gone, then continues synthetically. Before TP1 the trade exits all."""
        self.alerts.add(self.key, 'native_fallback', trade_id=st['tid'], code=reason)
        if not tp1_done:
            return self._native_exit(st, 'native_' + reason, s, now)
        cid = f'native-stop-cancel:{st["tid"]}'
        if self.journal.has(cid):
            return self._native_exit(st, 'native_stop_cancel_repeat', s, now)
        payload = {'ticker': self.ticker, 'action': 'cancel',
                   'extras': {'soup_command_id': cid, 'soup_trade_id': st['tid']}}
        self._native_record(st, 'stop_cancel', command_id=cid, reason=reason)
        self._send(self._track(st['tid']), cid, 'cancel', payload, None, 'native')

    def _native_pending_exits(self, st, held, tp1_done, tp1_at, now):
        """(contracts to exit now, {tranche: qty}, reason) from the leader's eligible runner decisions.

        A decision applies once the follower's OWN TP1 has filled at or before it (Soup drops a
        runner action that precedes the verified TP1 fill; so does the follower).
        """
        plan, leader = st['plan'], self.leader.get(st['tid']) or {}
        left = self._native_left(st, tp1_done)
        tranches, reasons = {}, set()
        for k, (reason, at) in sorted((leader.get('decisions') or {}).items()):
            if k not in TRANCHES or k == 'tp1' or st['done_decisions'].get(k) or left.get(k, 0) <= 0:
                continue
            if not tp1_done:
                continue  # wait: an earlier own TP1 fill may not be reported yet
            if tp1_at is None or at < tp1_at:
                self._native_record(st, 'decision', tranche=k, result='ignored_before_own_tp1', reason=reason,
                                    decided_at=at, tp1_filled_at=tp1_at)
                continue
            tranches[k] = left[k]
            reasons.add(reason)
        for k, (reason, _) in (st.get('synthetic_decisions') or {}).items():
            if left.get(k, 0) > 0 and k not in tranches and not st['done_decisions'].get(k):
                tranches[k] = left[k]
                reasons.add(reason)
        return sum(tranches.values()), tranches, ';'.join(sorted(reasons)) or 'runner_exit'

    @staticmethod
    def _native_left(st, tp1_done):
        alloc = st['plan']['alloc']
        left = {k: alloc.get(k, 0) - st['reduced'].get(k, 0) for k in TRANCHES}
        left['tp1'] = 0 if tp1_done else alloc.get('tp1', 0)
        return left

    def _native_send_tp1(self, st, tr):
        plan = st['plan']
        cid = f'native-tp1:{st["tid"]}'
        qty, price = plan['alloc']['tp1'], plan['tp1_limit']
        payload = {'ticker': self.ticker, 'extras': {'soup_command_id': cid, 'soup_trade_id': st['tid']},
                   'action': 'exit', 'orderType': 'limit', 'quantity': qty, 'quantityType': 'fixed_quantity',
                   'limitPrice': price, 'signalPrice': price, 'cancel': False}
        self._native_record(st, 'tp1', command_id=cid, quantity=qty, price=price)
        self._send(tr, cid, 'tp1_limit', payload, None, 'native')

    def _native_send_modify(self, st, tr, qty, price, why):
        cid = f'native-stop:{st["tid"]}:{st["n"] + 1}'
        payload = {'ticker': self.ticker, 'action': 'modify_stop',
                   'extras': {'soup_command_id': cid, 'soup_trade_id': st['tid'], 'bridge_kind': 'modify_stop',
                              'stop_price': price, 'stop_quantity': qty}}
        self._native_record(st, 'modify', command_id=cid, quantity=qty, price=price, why=why)
        self._send(tr, cid, 'modify_stop', payload, None, 'native')

    def _native_send_reduce(self, st, tr, tranches, why):
        cid = f'native-reduce:{st["tid"]}:{st["n"] + 1}'
        qty = sum(tranches.values())
        payload = {'ticker': self.ticker, 'extras': {'soup_command_id': cid, 'soup_trade_id': st['tid']},
                   'action': 'exit', 'orderType': 'market', 'quantity': qty, 'quantityType': 'fixed_quantity',
                   'cancel': False}
        self._native_record(st, 'reduce', command_id=cid, quantity=qty, tranches=tranches, why=why)
        self._send(tr, cid, 'reduce', payload, None, 'native')

    def _native_reduce_check(self, st, s, now):
        r = st['reduce']
        out = (self.journal.get(r['cid']) or {}).get('outcome')
        o = ((s.get('trade') or {}).get('commands') or {}).get(r['cid']) or {}
        if out == 'rejected':
            return self._native_exit(st, 'native_reduce_rejected', s, now)
        if o.get('filled') == r['qty']:
            self._native_record(st, 'reduce_filled', command_id=r['cid'], tranches=r['tranches'])
            for k in r['tranches']:
                self._native_record(st, 'decision', tranche=k, result='applied')
            return
        if o.get('seen') and not o.get('working'):
            return self._native_exit(st, 'native_reduce_incomplete', s, now)
        if now - r['sent_at'] > self.native_policy['reduce_confirm_ms']:
            return self._native_exit(st, 'native_reduce_unconfirmed', s, now)

    # --- synthetic fallback (Soup's watchers, on Soup's recorded quotes and bars) ------------

    def _native_crossings(self, st, held, tp1_done, tp1_at, now):
        """Backstop (native) or synthetic exits from Soup's last quote and last bar."""
        plan = st['plan']
        sign = 1 if plan['side'] == 'long' else -1
        since = st['started_at']
        touches = []
        q, b = self.market.get('quote'), self.market.get('bar')
        if isinstance(q, dict) and isinstance(q.get('time'), int) and q['time'] >= since:
            touches.append((q['time'], q['price'], q['price']))
        if isinstance(b, dict) and isinstance(b.get('close_time'), int) and b['close_time'] > since:
            touches.append((b['close_time'], b['low'] if sign > 0 else b['high'], b['close']))
        for at, extreme, _ in touches:
            if not tp1_done and sign * (extreme - plan['initial']) <= 0:
                crossed = ('initial_hard_stop', at)
            elif tp1_done and tp1_at is not None and at >= tp1_at and sign * (extreme - plan['break_even']) <= 0:
                crossed = ('break_even', at)
            else:
                continue
            if st['mode'] == 'native':
                if st['backstop'] is None and held and held > 0:
                    self._native_record(st, 'backstop', reason=crossed[0], crossed_at=crossed[1])
            elif crossed[0] == 'initial_hard_stop':
                st['synthetic_exit'] = 'initial_hard_stop'
            else:
                rnb = (tp1_at // plan['chart_ms'] + 2) * plan['chart_ms']
                if at >= rnb:
                    st.setdefault('synthetic_decisions', {}).update(
                        {k: ('break_even', at) for k in ('parent', 'eod')})

    def _native_synthetic(self, st, tr, s, held, tp1_done, tp1_at, now):
        if st.get('synthetic_exit'):
            return self._native_exit(st, st['synthetic_exit'], s, now)
        if st['reduce']:
            return self._native_reduce_check(st, s, now)
        reserved, tranches, why = self._native_pending_exits(st, held, tp1_done, tp1_at, now)
        if not reserved:
            return
        if reserved >= held:
            return self._native_exit(st, why, s, now)
        return self._native_send_reduce(st, tr, tranches, why)

    def _native_fallback(self, st, code):
        if st['mode'] != 'native':
            return
        self._native_record(st, 'fallback', code=code)
        self.alerts.add(self.key, 'native_fallback', trade_id=st['tid'], code='fallback:' + code)

    # --- exit-all -------------------------------------------------------------------------

    def _native_exit(self, st, reason, s, now, **detail):
        if st['exit'] is None:
            self._native_record(st, 'exit', reason=reason, **detail)
            if not reason.startswith(NORMAL_EXITS):
                self.alerts.add(self.key, 'native_exit', trade_id=st['tid'], reason=reason, **detail)
        return self._native_exit_step(st, s, now)

    def _native_exit_step(self, st, s, now):
        pol, ex = self.native_policy, st['exit']
        last_cid = ex['attempts'][-1] if ex['attempts'] else None
        last = self.journal.get(last_cid) if last_cid else None
        since = (last or {}).get('sent_at') or ex['at']
        if s is not None and s['synced'] and s['observed_at'] >= since and s['net_position'] == 0 \
                and not s['open_order_ids'] and not s['unseen_accepted_orders']:
            return self._native_finish(st, 'flat', exit_reason=ex['reason'], attempts=list(ex['attempts']))
        if last is not None:
            window = min(pol['exit_verify_ms'], 1000) if last.get('outcome') == 'rejected' else pol['exit_verify_ms']
            if now - since < window:
                return
        if len(ex['attempts']) >= pol['max_exit_attempts']:
            return self._native_failed(st, s)
        cid = f'native-exit:{st["tid"]}:{len(ex["attempts"]) + 1}'
        payload = {'ticker': self.ticker, 'action': 'flatten_all',
                   'extras': {'soup_command_id': cid, 'soup_trade_id': st['tid'], 'bridge_kind': 'flatten_all'}}
        self._native_record(st, 'exit_attempt', command_id=cid)
        self._send(self._track(st['tid']), cid, 'flatten_all', payload, None, 'native')

    def _native_failed(self, st, s):
        """Exit-all unverified after every attempt: block this follower only, alert, stop sending."""
        view = {'net_position': (s or {}).get('net_position'), 'open_order_ids': (s or {}).get('open_order_ids')}
        try:
            fd = os.open(self.block_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except OSError as exc:
            log.error('follower %s: block file not written (%s)', self.key, type(exc).__name__)
        killed = self._native_kill('native exit-all unverified: ' + st['tid'])
        self._native_record(st, 'failed', reason=st['exit']['reason'], kill_switch=killed, **view)
        self.alerts.add(self.key, 'exit_all_unverified', trade_id=st['tid'], reason=st['exit']['reason'],
                        attempts=len(st['exit']['attempts']), kill_switch=killed, **view)
        self._native_finish(st, 'native_exit_failed', exit_reason=st['exit']['reason'],
                            attempts=list(st['exit']['attempts']), kill_switch=killed, **view)

    def _native_kill(self, reason):
        """POST /follower/<token>/<key>/kill on the daemon: this follower's kill switch only."""
        url = self.cfg['bridge_url'].rstrip('/') + f'/follower/{self.token}/{self.key}/kill'
        body = json.dumps({'reason': reason[:200]}).encode()
        req = urllib.request.Request(url, data=body, method='POST', headers={'Content-Type': 'application/json'})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=5) as r:
                return json.loads(r.read(4097)).get('entries_disabled') is True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _native_finish(self, st, result, **detail):
        if st['done']:
            return
        self._native_record(st, 'done', result=result)
        tr = self._track(st['tid'], st['plan']['side'])
        if st['tid'] not in self.journal.reconciled:
            self._finish(tr, result, native=True, mode=st['mode'], fallback=st['fallback'], **detail)

    def _native_supersede(self, new_tid):
        """A newer leader entry: older native trades stop sending (their exit, if any, ends here);
        the new entry's alignment closes whatever they left."""
        for tid, st in list(self.nat.items()):
            if tid != new_tid and not st['done']:
                if st['exit'] is None or st['exit']['attempts']:
                    self.alerts.add(self.key, 'native_superseded', trade_id=tid,
                                    reason=(st['exit'] or {}).get('reason') or 'open_at_next_entry')
                self._native_finish(st, 'superseded', by=new_tid)

    def native_status(self):
        out, done = {}, 0
        # Read from the service thread while the lane runs. Every open trade plus the 3 LATEST done ones
        # (1.0.9: 1.0.8 kept the first 3 done trades, so a long-running service showed stale trades).
        for tid, st in reversed(list(self.nat.items())):
            if st['done']:
                done += 1
                if done > 3:
                    continue
            out[tid] = {'mode': st['mode'], 'fallback': st['fallback'], 'stop_confirmed': st['stop_confirmed'],
                        'exit': (st['exit'] or {}).get('reason'),
                        'exit_attempts': len((st['exit'] or {}).get('attempts') or []),
                        'backstop': st['backstop'], 'reduced': {k: v for k, v in st['reduced'].items() if v},
                        'done': st['done'], 'failed': st['failed'], 'skipped': st.get('skipped')}
        return {'blocked': self.block_path.exists(), 'trades': dict(reversed(list(out.items())))}

