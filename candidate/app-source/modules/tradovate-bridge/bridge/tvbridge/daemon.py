"""Bridge service: loopback order endpoint for Soup plus broker-snapshot publisher.

Run:  python3 -m tvbridge.daemon --config config.json
Env:  TVBRIDGE_TOKEN (order/health path secret, >= 24 chars)
      SOUP_BROKER_TOKEN (only when publish_mode is "soup")
publish_mode: "evidence" (local file only), "soup" (HTTP /broker of an older engine; needs that
              engine's soup_base_url, no default),
              "adapter-db" (Soup v3: SQLite adapter database)
followers:    optional (1.0.7-1.0.9): follower accounts of the same login, see tvbridge.followers
              and tvbridge.firing. Absent or empty: the 1.0.6.2 handler, unchanged.
multi-login (1.1.0, all optional, see tvbridge.logins.daemon_settings):
              login_id/login_label; mode "followers_only" (no main account, no Soup publisher; Soup
              routes answer 409); master_kill_switch_file (read only); peer_logins (cross-login hedge
              check, tvbridge.peers; peer_hedge_scope "configured" (default) or "all"); token_env (this
              daemon's own token variable, default TVBRIDGE_TOKEN); read_token_env (a second, read-only
              token accepted only for GET /health and GET /login, never for orders, kill or follower routes).
              GET /login/<token>: every account visible on this login (read only, in memory, no page call).
              Without these keys the daemon answers exactly as 1.0.9.5.2 (plus the new /login route).
Flatten now (1.1.3, tvbridge.manual): POST /manual/<manual token>/<key>/flatten, one account per call,
              operator-confirmed, risk-reducing only. Token from manual_token_env (default
              TVBRIDGE_MANUAL_TOKEN_<LOGIN ID>); unset or empty: the route does not exist (404).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import adapters
from . import manual as manual_routes
from . import marketfeed
from . import protocol
from .bridge import Bridge
from .executor import Executor, kill_switch_state, max_contracts_problem, pause_fields
from .journal import NO_ORDER_KINDS, Journal
from .logins import daemon_settings
from .peers import LEADER_EXEMPT_ROLES, PeerHedge
from .soup_adapter import AdapterDbSink, Skip
from .state import AccountState

log = logging.getLogger('tvbridge')

# Exit status after the page controller lease was lost. Non-zero, so the unit's
# Restart=on-failure re-attaches with a fresh page session (EX_TEMPFAIL).
EXIT_LEASE_LOST = 75
DRAIN_S = 60.0
# Clients that hang up before the reply is written (they timed out). Not a daemon fault:
# the command outcome is already journaled; never print a traceback for it.
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class Shutdown:
    """Stop the daemon without cutting an order in half, and never replay one.

    Triggers: the bridge lost the page controller lease (exit EXIT_LEASE_LOST, so systemd
    restarts and re-attaches) or SIGTERM (exit 0, a requested stop). Sequence: refuse new
    order commands, stop the publisher, wait (bounded) until no executor is inside send(),
    then stop the HTTP server; main() then detaches from the page. The journal is fsynced
    per record, so nothing is buffered; an order still unsettled after the bound keeps its
    intent-before-send record and stays unresolved (entries blocked) after the restart.
    """

    def __init__(self, bridge, executors, server=None, publisher=None, drain_s=DRAIN_S, poll_s=0.5):
        self.bridge, self.executors, self.server, self.publisher = bridge, list(executors), server, publisher
        self.drain_s, self.poll_s = float(drain_s), poll_s
        self.requested = threading.Event()
        self.done = threading.Event()
        self.reason = self.code = None
        self.unsettled = []
        self._lock = threading.Lock()

    def request(self, reason, code):
        """Thread- and signal-safe; the first request wins."""
        with self._lock:
            if self.reason is None:
                self.reason, self.code = reason, code
        self.requested.set()

    def watch(self):
        """Thread target: wait for a trigger, then drain and stop the server."""
        while not self.requested.wait(self.poll_s):
            if self.bridge.lease_lost.is_set():
                self.request('controller_lease_lost:' + str(self.bridge.lease_lost_reason), EXIT_LEASE_LOST)
        if self.bridge.lease_lost.is_set() and self.code == 0:
            self.code = EXIT_LEASE_LOST  # a stop that also lost the lease still needs a re-attach
        self.bridge.stop_reason = self.reason
        log.warning('daemon stopping (%s): new orders refused, waiting up to %g s for any order in flight',
                    self.reason, self.drain_s)
        if self.publisher is not None:
            self.publisher.stopping.set()
            with self.publisher.state.changed:
                self.publisher.state.changed.notify_all()
        self.unsettled = self.drain()
        if self.unsettled:
            log.error('order still in flight after %g s (%s); stopping anyway: its journal intent record '
                      'stays unresolved and blocks entries until reconciled; nothing is replayed',
                      self.drain_s, ', '.join(self.unsettled))
        else:
            log.info('no order in flight; stopping')
        if self.server is not None:
            self.server.shutdown()
        self.done.set()

    def drain(self):
        """Wait until every executor is outside send()/reconcile. Returns the ones that were not."""
        deadline = time.monotonic() + self.drain_s
        unsettled = []
        for ex in self.executors:
            if ex.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                ex.lock.release()  # stop_reason is set: whoever takes the lock next refuses
            else:
                unsettled.append(str(getattr(ex, 'key', None) or 'main'))
        return unsettled


def _journal_refusal(journal, trade_id):
    """1.0.9.4: the journal-only refusals of Executor.snapshot for a trade, or None.

    Same rules and order as the snapshot's command loop (auto_flatten, then uncertain
    dispatch), evaluated over every command, because the snapshot stops at the first
    order Tradovate does not list and never reaches a later command.
    """
    for c in journal.for_trade(trade_id):
        kind = c['kind']
        if kind in NO_ORDER_KINDS or c['outcome'] == 'rejected':
            continue
        if kind == 'auto_flatten':
            return 'auto_flatten_after_reversal:' + c['command_id']
        if kind == 'flatten_all' and c['outcome'] == 'accepted' and not c.get('broker_order_id'):
            continue
        if c['outcome'] != 'accepted' or not c.get('broker_order_id'):
            return 'uncertain_dispatch:' + c['command_id']
    return None


def _order_refusal(executor, trade_id):
    """1.0.9.4.1: an order-level inconsistency anywhere in the trade, or None.

    Every order the trade's journal names (bracket legs included) that Tradovate lists is
    checked for the snapshot's hard refusals: a status the bridge does not map, fills above
    the order's size, and a rejected order with fills. The snapshot stops at its first
    withheld order, so a later order is never reached there.
    """
    state = executor.state
    with state.lock:
        for c in executor.journal.for_trade(trade_id):
            if c['kind'] in NO_ORDER_KINDS or c['outcome'] == 'rejected':
                continue
            ids = [c.get('broker_order_id')] + list((c.get('legs') or {}).values())
            for oid in (int(x) for x in ids if x):
                order = state.order(oid)
                if order is None:
                    continue
                filled = state.filled_qty(oid)
                status = protocol.soup_status(order.get('ordStatus'), filled)
                if status is None:
                    return 'unmapped_status:' + str(order.get('ordStatus'))
                size = state.order_qty(oid)
                if size is not None and filled > size:
                    return 'overfill:' + str(oid)
                if status == protocol.REJECTED and filled:
                    return 'rejected_with_fills:' + str(oid)
    return None


class Publisher:
    """Pushes snapshots for the latest trade while it is active, then briefly after."""

    # A snapshot withheld for a moment (an order Tradovate has not echoed yet, a
    # terminal order settling) is normal right after a dispatch. It only blocks new
    # orders once it persists this long; delivery failures still block at once.
    HOLD_GRACE_S = 10.0

    def __init__(self, executor, journal, state, mode, soup_url=None, evidence_path=None,
                 interval_s=2.0, linger_s=60.0, clock=time.monotonic, sink=None, hold_grace_s=HOLD_GRACE_S,
                 trade_kinds=('entry',)):
        self.executor, self.journal, self.state = executor, journal, state
        self.mode, self.soup_url, self.evidence_path = mode, soup_url, evidence_path
        self.interval_s, self.linger_s = interval_s, linger_s
        self.stopping = threading.Event()
        self.last = {}
        self.clock = clock
        self.alive_at = None
        self.sink = sink
        self.hold_grace_s = hold_grace_s
        self._failure = None
        self.failure_since = None
        # Which entry kinds define "the latest trade". Bracket trades are Soup-unknown, so
        # they are followed only in evidence mode (see main()).
        self.trade_kinds = tuple(trade_kinds)
        # 1.0.9.4: inactive trades no longer republished because their orders left the page
        # (see _retire_inactive). Publisher thread only.
        self.retired = set()

    @property
    def failure(self):
        return self._failure

    @failure.setter
    def failure(self, value):
        if value is None:
            self.failure_since = None
        elif value != self._failure:
            self.failure_since = self.clock()
        self._failure = value

    def run(self):
        version = -1
        inactive_since = {}
        while not self.stopping.is_set():
            self.alive_at = self.clock()
            try:
                version = self.state.wait_for_change(version, self.interval_s)
                self.alive_at = self.clock()
                if getattr(self.executor, 'bracket_protect', False) is True:
                    # Opt-in bracket housekeeping (risk-reducing only); a no-op otherwise.
                    with self.executor.lock:
                        self.executor.maintain_brackets()
                trade_id = self.journal.latest_trade_id(self.trade_kinds)
                if not trade_id or self.executor.account_id is None:
                    continue
                if self.executor.trade_active(trade_id):
                    inactive_since.pop(trade_id, None)
                    self.retired.discard(trade_id)
                else:
                    if trade_id in self.retired:
                        continue
                    first = inactive_since.setdefault(trade_id, self.clock())
                    if self.clock() - first > self.linger_s and self.failure is None:
                        continue
                with self.executor.lock:
                    self.executor.auto_reconcile()
                snap, reason = self.executor.snapshot(trade_id)
                if snap is None:
                    if self._retire_inactive(trade_id, reason, inactive_since.get(trade_id)):
                        continue
                    self.failure = 'snapshot_unavailable'
                    self.last = {'at': time.time(), 'published': False, 'reason': reason}
                    if reason.startswith('uncertain_dispatch'):
                        log.error('broker evidence blocked by an unknown order outcome: %s', reason)
                    continue
                self.publish(snap)
            except Exception as exc:  # noqa: BLE001 - health stays failed until actual publication recovers
                self.failure = 'publisher_exception'
                log.error('snapshot publisher error (%s)', type(exc).__name__)
                self.last = {'at': time.time(), 'published': False, 'reason': 'publisher_exception'}
                self.stopping.wait(1)

    def _retire_inactive(self, trade_id, reason, inactive_since):
        """1.0.9.4: stop republishing an inactive trade whose orders are no longer on the page.
        1.0.9.4.1: also an inactive trade withheld as `fills_pending` (an order Tradovate reports
        Filled whose fills do not complete the trade's evidence), under the same conditions plus
        no order-level inconsistency anywhere in the trade (`_order_refusal`). Nothing is
        fabricated: Soup gets nothing for that trade, only the bridge's health stops failing.

        After a daemon restart in a later CME session the reloaded page no longer lists the
        previous session's orders, so the latest (closed) trade's snapshot is withheld with
        `order_not_seen_yet` forever and, with the publisher failed, the linger rule never
        stops retrying: health failed and every order (exits included) was refused.
        Retired only when all of these hold, else the failure stands as before:
        - the reason is `order_not_seen_yet` (page and sync were healthy: snapshot checks them first),
          or (1.0.9.4.1) `fills_pending` with no `_order_refusal` in the trade;
        - the trade stayed inactive for the whole linger window (flat, nothing working on the
          contract, no unknown or in-flight command of the trade);
        - the trade's journal has no auto_flatten and no uncertain dispatch (refusals the
          snapshot reports only once it gets past the missing order);
        - no accepted order of the current CME session is unreported (the missing order is from
          an earlier session: an expired Day order, not a page or sync fault).
        Nothing is published for the trade and no journal is written. Returns True when retired.
        """
        fills = reason.startswith('fills_pending:')
        if not (reason.startswith('order_not_seen_yet:') or fills) or inactive_since is None:
            return False
        if self.clock() - inactive_since <= self.linger_s:
            return False
        ex = self.executor
        if ex.trade_active(trade_id) or _journal_refusal(ex.journal, trade_id) or ex._unseen_accepted_orders():
            return False
        if fills and _order_refusal(ex, trade_id):
            return False
        self.retired.add(trade_id)
        self.failure = None
        self.last = {'at': time.time(), 'published': 'skipped', 'reason': 'inactive_trade_orders_not_visible',
                     'trade_id': trade_id}
        if fills:
            self.last.update(reason='inactive_trade_fills_incomplete', withheld=reason)
            log.warning('snapshot publisher: trade %s is inactive (flat, nothing working) but its fills do not '
                        'complete its evidence (%s); not republishing it', trade_id, reason)
        else:
            log.warning('snapshot publisher: trade %s is inactive and its orders are no longer visible on the page '
                        '(%s); not republishing it', trade_id, reason)
        return True

    def watchdog(self):
        """A running loop is not proof that usable evidence is being delivered."""
        if self.alive_at is None or self.clock() - self.alive_at > self.interval_s * 3 + 5:
            return 'snapshot_publisher_stalled'
        if self.failure is not None:
            if (self.failure == 'snapshot_unavailable' and self.failure_since is not None
                    and self.clock() - self.failure_since < self.hold_grace_s):
                return None
            return 'snapshot_publisher_failed:' + self.failure
        return None

    def publish(self, snap):
        body = json.dumps(snap, sort_keys=True, separators=(',', ':')).encode()
        if self.evidence_path:
            fd = os.open(self.evidence_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, body + b'\n')
            finally:
                os.close(fd)
        if self.mode == 'adapter-db':
            try:
                stored = self.sink.publish(snap)
            except Skip as exc:
                # Engine has no matching active trade: nothing to confirm, not a fault.
                self.last = {'at': time.time(), 'published': 'skipped', 'reason': str(exc)}
                self.failure = None
                return
            except (OSError, ValueError, sqlite3.Error) as exc:
                log.error('adapter database write failed (%s)', type(exc).__name__)
                self.last = {'at': time.time(), 'published': False, 'reason': 'adapter_write_failed'}
                self.failure = 'adapter_write_failed'
                return
            self.last = {'at': time.time(), 'published': 'adapter_db', 'sequence': stored['sequence']}
            self.failure = None
            return
        if self.mode != 'soup':
            self.last = {'at': time.time(), 'published': 'evidence_only', 'sequence': snap['sequence']}
            self.failure = None
            return
        req = urllib.request.Request(self.soup_url, data=body, method='POST',
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        except (urllib.error.URLError, OSError):
            status = None
        self.last = {'at': time.time(), 'published': status == 202, 'http_status': status,
                     'sequence': snap['sequence']}
        self.failure = None if status == 202 else 'publish_not_acknowledged'


def account_state(executor, trade_id=None, extended=False):
    """Read-only view of the pinned account/contract for the Soup mirror (tvbridge.mirror).

    No names, tokens or order bodies: ids, the signed net position and working order ids.
    With `extended` (1.0.6, config `account_view_enabled`), also the configured pins a
    client checks before sending: a SHA-256 of the account name (never the name), the
    Soup pairing account, the kill-switch state and the publish mode.
    `synced` is true only when the account state belongs to the page's current connection.
    With `trade_id`, also reports the bridge journal's outcome for that trade's entry.
    """
    health = executor.health()
    page = health['page'] or {}
    with executor.state.lock:
        resolved = executor.account_id is not None and executor.contract_id is not None
        synced = (resolved and page.get('state') == 'authorized'
                  and executor.state.is_synced_for(page.get('epoch'))
                  and executor.state.has_account(executor.account_id))
        net = executor.state.net_position(executor.account_id, executor.contract_id) if resolved else None
        open_ids = [str(o) for o in executor.state.open_orders(executor.account_id, executor.contract_id)] \
            if resolved else []
    report = {
        'account_id': executor.account_id,
        'contract': {'id': executor.contract_id, 'name': executor.symbol},
        'net_position': net,
        'open_order_ids': open_ids,
        'synced': bool(synced),
        'healthy': health['ok'],  # false: the bridge refuses every order right now
        'problems': health['problems'],
        'unseen_accepted_orders': [str(o) for o in executor._unseen_accepted_orders()] if resolved else [],
        'unresolved_commands': len(executor.journal.unresolved()),
        'observed_at': executor.clock_ms(),
    }
    if extended:
        name = executor.cfg.get('account_name')
        report.update(account_name_sha256=hashlib.sha256(name.encode()).hexdigest() if isinstance(name, str) else None,
                      soup_account_id=executor.cfg.get('soup_account_id'),
                      entries_disabled=executor.entries_disabled(),
                      publish_mode=executor.cfg.get('publish_mode', 'evidence'))
        enabled = getattr(executor, 'adapters', None)
        if isinstance(enabled, dict):
            # the strategy adapter(s) this binding accepts orders from (a runtime checks its own id).
            report['adapter_ids'] = sorted(enabled)
    if trade_id is not None:
        entry = executor._entry_for(trade_id)
        report['trade_entry_outcome'] = entry['outcome'] if entry else None
    return report


def account_view(executor):
    """GET /account (1.0.6, opt-in `account_view_enabled`): balance and risk for the pinned account.

    Read-only. Values are Tradovate's own (cashBalance, accountRiskStatus,
    userAccountAutoLiq, marginSnapshot from the sync and later updates). `complete` is
    true only when the account belongs to the current connection's sync and a cash
    balance row exists for it. No order, name of other accounts, or token is included.
    """
    report = account_state(executor, extended=True)
    report['account_name'] = executor.cfg.get('account_name')
    data = executor.state.account_view(executor.account_id) if executor.account_id is not None else None
    complete = bool(report['synced'] and data and data['cash_balance'] is not None)
    report = {k: report[k] for k in ('observed_at', 'account_id', 'account_name', 'soup_account_id', 'contract',
                                     'synced', 'healthy', 'problems', 'entries_disabled', 'publish_mode',
                                     'net_position', 'open_order_ids', 'unresolved_commands')}
    report['schema'] = 'tvbridge.account.v1'
    report['account_data'] = dict(data or {'cash_balance': None, 'risk_status': None, 'auto_liq': None,
                                           'margin': None, 'updated_at': None}, complete=complete)
    return report


def _now_ms():
    return time.time_ns() // 1_000_000


# GET /login answers from the latest known page status; one older than this is not trusted.
PAGE_STATUS_STALE_S = 30.0


def page_problems(st, state, cfg, extra_age_ms=0):
    """The login-level checks of Executor.health for one page status (same problem strings)."""
    problems = []
    if st.get('state') != 'authorized':
        problems.append('page_state:' + str(st.get('state')))
    age = st.get('lastRxAgoMs')
    if age is None or age + extra_age_ms > cfg.get('max_silence_ms', 10000):
        problems.append('tradovate_silent')
    if not state.is_synced_for(st.get('epoch')):
        problems.append('not_synced_for_current_connection')
    return problems


def page_health(bridge, state, cfg):
    """1.1.0: the login-level checks of Executor.health (page, Tradovate traffic, sync), for a login
    with no main account (its /health). Same problem strings; no account checks. Asks the page."""
    st = bridge.status() or {}
    problems = page_problems(st, state, cfg)
    return {'ok': not problems, 'problems': problems, 'page': st}


class LoginView:
    """1.1.0: this daemon's login, read only, for GET /login and the login fields of /health.

    settings: tvbridge.logins.daemon_settings(cfg). `executor` is None on a followers_only login;
    `followers` is the dict tvbridge.followers.build returned ({} without followers).
    """

    SCHEMA = 'decrypt.tvbridge-login.v1'

    def __init__(self, settings, bridge, state, cfg, executor=None, followers=None, peers=None, clock_ms=_now_ms,
                 clock=time.monotonic):
        self.settings, self.bridge, self.state, self.cfg = settings, bridge, state, cfg
        self.executor, self.followers, self.peers, self.clock_ms = executor, followers or {}, peers, clock_ms
        self.clock = clock
        self._page = None  # (clock, status) of the latest page status a /health check saw

    def observe_page(self, st):
        """Remember a page status the health checks already fetched (GET /login never asks the page)."""
        if isinstance(st, dict):
            self._page = (self.clock(), st)

    def known_page(self):
        """(age in seconds, status) of the latest known page status: this view's, or the bridge's own
        (tvbridge.bridge.Bridge.last_status, refreshed by its heartbeat and every health check). None if none."""
        found = [c for c in (self._page, getattr(self.bridge, 'last_status', None))
                 if isinstance(c, tuple) and len(c) == 2 and isinstance(c[1], dict)]
        if not found:
            return None
        at, st = max(found, key=lambda c: c[0])
        return max(0.0, self.clock() - at), st

    def entries_disabled(self):
        return self.entries_disabled_reason() is not None

    def entries_disabled_reason(self):
        """followers_only: the master kill switch or this login's own top-level kill switch, each only
        when configured; a configured switch that cannot be checked counts as on (fails closed)."""
        for prefix, path in (('', self.cfg.get('kill_switch_file')),
                             ('master_', self.settings['master_kill_switch_file'])):
            if path:
                why = kill_switch_state(path, writable=not prefix)
                if why:
                    return prefix + why
        return None

    def health_fields(self):
        """Added to /health: `login_id` when the config names its login, `notes` with peer_logins."""
        out = {}
        if self.settings['explicit']:
            out['login_id'] = self.settings['login_id']
        if self.peers is not None:
            out['notes'] = self.peers.notes()
        return out

    def health_report(self):
        """GET /health on a followers_only login: the page checks, no publisher."""
        health = page_health(self.bridge, self.state, self.cfg)
        self.observe_page(health['page'])
        report = dict({'health': health, 'publisher': None}, **pause_fields(self))
        report.update(self.health_fields())
        return report

    def _executors(self):
        out = [] if self.executor is None else [('main', None, self.executor)]
        return out + [('follower', key, ex) for key, ex in self.followers.items()
                      if getattr(ex, 'account_id', None) is not None]

    def _contract(self, cid, executors):
        """Contract name from the sync, else from an executor's pinned contract or its lookup cache;
        None if unknown. Never a Tradovate lookup: /login answers from memory only."""
        name = self.state.contract_name(cid)
        if name is None:
            for _, _, ex in executors:
                if cid is not None and getattr(ex, 'contract_id', None) == cid and getattr(ex, 'symbol', None):
                    return ex.symbol
                name = (getattr(ex, 'contract_names', None) or {}).get(cid)
                if name:
                    break
        return name

    def report(self):
        """GET /login: every account visible on this login, configured ones marked. No Tradovate
        request and no page call: in-memory state plus the latest known page status (known_page).

        `page.logged_in` is computed exactly as a /followers row computes `logged_in`, with the
        login's page problems and `synced` = page authorized and state synced for its connection.
        No known page status: logged_in false, problem `page_status_unknown`; one older than
        PAGE_STATUS_STALE_S: logged_in false, problem `page_status_stale`.
        Positions: every position row of the account (net 0 included). Working: every working order,
        `qty` the remaining (unfilled) quantity or None when Tradovate has not reported it.
        """
        known = self.known_page()
        if known is None:
            page, problems, synced, logged_in = {}, ['page_status_unknown'], False, False
        else:
            age_s, page = known
            problems = page_problems(page, self.state, self.cfg, extra_age_ms=age_s * 1000)
            synced = page.get('state') == 'authorized' and self.state.is_synced_for(page.get('epoch'))
            logged_in = not any(p.startswith('page_state:') or p == 'not_synced_for_current_connection'
                                for p in problems) and synced
            if age_s > PAGE_STATUS_STALE_S:
                problems.append('page_status_stale')
                logged_in = False
        executors = self._executors()
        by_id = {ex.account_id: (role, key) for role, key, ex in executors}
        # Configured but not pinned (disabled in config, or the id did not match): marked by name.
        by_name = {}
        if self.executor is not None and isinstance(self.cfg.get('account_name'), str):
            by_name[self.cfg['account_name']] = ('main', None)
        for f in self.cfg.get('followers') or []:
            if isinstance(f, dict) and isinstance(f.get('account_name'), str):
                by_name.setdefault(f['account_name'], ('follower', f.get('key')))
        accounts = []
        with self.state.lock:
            nets = self.state.net_positions()
            working = self.state.working_orders()
            for acct in sorted(self.state.entities['account'].values(), key=lambda a: a['id']):
                aid = acct['id']
                role, key = by_id.get(aid) or by_name.get(acct.get('name')) or (None, None)
                positions = []
                for (pa, cid), net in sorted(nets.items(), key=lambda kv: kv[0][1] if type(kv[0][1]) is int else -1):
                    if pa == aid:
                        name = self._contract(cid, executors)
                        positions.append({'contract': name, 'root': protocol.product_root(name), 'net': net})
                orders = []
                for o in sorted((o for o in working if o.get('accountId') == aid), key=lambda o: o['id']):
                    name = self._contract(o.get('contractId'), executors)
                    qty = self.state.order_qty(o['id'])
                    if qty is None and type(o.get('orderQty')) is int:
                        qty = o['orderQty']
                    orders.append({'contract': name, 'root': protocol.product_root(name),
                                   'side': o.get('action') if o.get('action') in ('Buy', 'Sell') else None,
                                   'qty': None if qty is None else qty - self.state.filled_qty(o['id'])})
                accounts.append({'name': acct.get('name'), 'id': aid, 'configured': role is not None,
                                 'role': role, 'key': key, 'positions': positions, 'working': orders})
        return {'schema': self.SCHEMA, 'login_id': self.settings['login_id'], 'label': self.settings['label'],
                'mode': self.settings['mode'],
                'page': {'state': page.get('state'), 'synced': bool(synced), 'logged_in': bool(logged_in),
                         'problems': problems},
                'observed_at': self.clock_ms(), 'accounts': accounts}


def make_handler(executor, publisher, token, account_view_enabled=False, login=None, read_token=None, feed=None):
    """1.1.0 `read_token` (config read_token_env): also accepted for GET /health and GET /login only.
    `feed` (tvbridge.marketfeed.SecondFeed): GET /feed/<token> serves the 1-second feed (order token only)."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log secret paths or order bodies.

        def _authorized(self, route, readable=False):
            parts = self.path.split('/')
            if len(parts) != 3 or parts[1] != route:
                return False
            ok = protocol.same_secret(parts[2], token)
            if readable and read_token:
                ok = protocol.same_secret(parts[2], read_token) or ok
            return ok

        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            parts = split.path.split('/')
            if len(parts) == 3 and parts[1] == 'state' and protocol.same_secret(parts[2], token):
                try:
                    query = urllib.parse.parse_qs(split.query, max_num_fields=2)
                except ValueError:
                    return self.respond(400, {'error': 'invalid request'})
                trade_ids = query.get('trade_id')
                if set(query) - {'trade_id'} or (trade_ids and (len(trade_ids) != 1 or len(trade_ids[0]) > 240)):
                    return self.respond(400, {'error': 'invalid request'})
                return self.respond(200, account_state(executor, trade_ids[0] if trade_ids else None,
                                                       extended=account_view_enabled is True))
            if len(parts) == 3 and parts[1] == 'feed' and protocol.same_secret(parts[2], token) and executor is not None:
                if split.query:
                    return self.respond(400, {'error': 'invalid request'})
                return self.respond(200, feed.snapshot() if feed is not None
                                    else marketfeed.not_configured(executor.cfg.get('ticker')))
            if account_view_enabled is True and self._authorized('account'):
                return self.respond(200, account_view(executor))
            if login is not None and self._authorized('login', readable=True):
                return self.respond(200, login.report())
            if not self._authorized('health', readable=True):
                return self.respond(404, {'error': 'not found'})
            report = dict({'health': executor.health(), 'publisher': publisher.last}, **pause_fields(executor))
            peers = getattr(executor, 'peer_hedge', None)
            held = peers.unknown_blocking(executor.peer_exempt_roles) \
                if peers is not None and getattr(executor, 'hedge_guard', True) else []
            if held:
                report['peer_hedge_unknown'] = held   # these logins' unknown exposure refuses entries
            if login is not None:
                login.observe_page(report['health'].get('page'))
                report.update(login.health_fields())
            stopping = getattr(executor.bridge, 'stop_reason', None)
            if stopping:
                report['stopping'] = stopping
            self.respond(200, report)

        def do_POST(self):
            if not self._authorized('order'):
                return self.respond(404, {'error': 'not found'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 40960:
                    raise ValueError('body size')
                payload = json.loads(self.rfile.read(size))
            except (ValueError, OSError):
                return self.respond(400, {'error': 'invalid request'})
            outcome, receipt = executor.send(payload)
            self.respond(200, {'outcome': outcome, 'receipt': receipt})

        def respond(self, status, data):
            body = json.dumps(data, sort_keys=True).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except CLIENT_GONE:
                # The client timed out and closed first (e.g. while CDP is slow; without this
                # the handler logs BrokenPipe tracebacks). Whatever was done is journaled; the client decides from its own timeout.
                self.close_connection = True
                log.warning('client closed the connection before the reply (%s %s)', self.command,
                            self.path.split('/')[1] if self.path.count('/') >= 1 else '')
    return Handler


FOLLOWERS_ONLY = {'error': 'followers_only'}


def make_followers_only_handler(login, token, read_token=None):
    """1.1.0 `mode: followers_only`: no main account and no Soup publisher on this login.

    GET /health and /login answer from the page and the in-memory state; the Soup routes
    (POST /order, GET /state) answer 409 {"error": "followers_only"}; /account is 404. The
    follower routes are added by tvbridge.followers.make_follower_handler, as on a main login.
    `read_token` is accepted for GET /health and GET /login only.
    """
    base = make_handler(None, None, token, False, login, read_token)

    class Handler(base):
        def _route(self, readable=False):
            parts = urllib.parse.urlsplit(self.path).path.split('/')
            if len(parts) != 3:
                return None
            ok = protocol.same_secret(parts[2], token)
            if readable and read_token and parts[1] in ('health', 'login'):
                ok = protocol.same_secret(parts[2], read_token) or ok
            return parts[1] if ok else None

        def do_GET(self):
            route = self._route(readable=True)
            if route == 'login':
                return self.respond(200, login.report())
            if route == 'health':
                report = login.health_report()
                stopping = getattr(login.bridge, 'stop_reason', None)
                if stopping:
                    report['stopping'] = stopping
                return self.respond(200, report)
            if route == 'state':
                return self.respond(409, FOLLOWERS_ONLY)
            return self.respond(404, {'error': 'not found'})

        def do_POST(self):
            if self._route() == 'order':
                return self.respond(409, FOLLOWERS_ONLY)
            return self.respond(404, {'error': 'not found'})

    Handler.__name__ = 'FollowersOnlyHandler'
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--no-reload', action='store_true', help='attach without reloading the page')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = json.loads(Path(args.config).read_text())
    # 1.1.0 multi-login keys (all optional; their defaults are the 1.0.9.5.2 daemon).
    try:
        settings = daemon_settings(cfg)
    except ValueError as exc:
        raise SystemExit(f'config: {exc}')
    followers_only = settings['mode'] == 'followers_only'
    if not followers_only:
        # No size cap (the strategy alert sets the size); the retired max_contracts key may only hold a
        # legacy positive whole number, which is ignored. Checked at every start.
        problem = max_contracts_problem(cfg.get('max_contracts'))
        if problem is None:
            # one dated contract of a known root with its tick size, and only registered,
            # explicitly enabled strategy adapters (tvbridge.adapters). Checked before the browser.
            try:
                # A missing ticker or tick_size is refused by the executor itself, before any order.
                if 'ticker' in cfg:
                    adapters.contract_spec(cfg['ticker'])
                    if 'tick_size' in cfg:
                        adapters.check_contract(cfg['ticker'], cfg['tick_size'])
                enabled_adapters = adapters.binding_adapters(cfg)
            except adapters.AdapterError as exc:
                problem = str(exc)
            else:
                # an adapter that needs the 1-second feed (Lumi) never starts without it.
                problem = marketfeed.requirement_problem(cfg, enabled_adapters)
        if problem:
            log.error('config %s: %s; the bridge daemon does not start', args.config, problem)
            raise SystemExit(f'config: {problem}; the bridge daemon does not start')
    token = os.environ.get(settings['token_env'], '')
    if len(token) < 24:
        raise SystemExit(f'{settings["token_env"]} of at least 24 characters required')
    read_token = None
    if settings['read_token_env']:
        # 1.1.0: the read-only token peers, firing and the panel use for GET /login and /health.
        # Unset or empty: only the order token works, as before.
        read_token = os.environ.get(settings['read_token_env'], '') or None
        if read_token is not None and len(read_token) < 24:
            raise SystemExit(f'{settings["read_token_env"]} must hold at least 24 characters when set')
        if read_token is not None and hmac.compare_digest(read_token, token):
            raise SystemExit(f'{settings["read_token_env"]} must differ from {settings["token_env"]}')
    # 1.1.3 Flatten now (tvbridge.manual): a separate token for POST /manual/<token>/<key>/flatten only.
    # Unset or empty: no /manual route at all (404), exactly the 1.1.2 daemon.
    try:
        manual_cfg = manual_routes.settings(cfg, settings)
        manual_token = manual_routes.load_token(os.environ, manual_cfg['token_env'], (token, read_token))
        if manual_token is not None:
            manual_routes.check_aliases(cfg)
    except ValueError as exc:
        raise SystemExit(f'config: {exc}')
    mode = cfg.get('publish_mode', 'evidence')
    soup_url = None
    if followers_only:
        # No main account on this login: no Soup publisher, adapter database or writer lock.
        mode = None
    elif mode == 'soup':
        soup_token = os.environ.get('SOUP_BROKER_TOKEN', '')
        if len(soup_token) < 24:
            raise SystemExit('SOUP_BROKER_TOKEN required for publish_mode "soup"')
        soup_base = cfg.get('soup_base_url')
        if not isinstance(soup_base, str) or not soup_base.startswith(('http://', 'https://')):
            raise SystemExit('publish_mode "soup" needs soup_base_url, the older engine\'s HTTP base URL')
        soup_url = soup_base.rstrip('/') + '/broker/' + soup_token
    elif mode not in ('evidence', 'adapter-db'):
        raise SystemExit('publish_mode must be "evidence", "soup" or "adapter-db"')
    account_view_enabled = cfg.get('account_view_enabled', False)
    if not isinstance(account_view_enabled, bool):
        raise SystemExit('account_view_enabled must be true or false')
    # 1.0.9.3: fill-price journal (tvbridge.fills), passive, on unless set false; and the opt-in
    # `avg_fill_price` in Soup snapshots (off unless set true). Checked before touching the browser.
    fill_capture_enabled = cfg.get('fill_capture_enabled', True)
    if not isinstance(fill_capture_enabled, bool) or not isinstance(cfg.get('snapshot_fill_prices', False), bool):
        raise SystemExit('fill_capture_enabled and snapshot_fill_prices must be true or false')
    follower_accounts = None
    if cfg.get('followers') or followers_only:
        # 1.0.7 firing order (tvbridge.firing). Imported only when configured: without
        # `followers` the daemon runs exactly the 1.0.6.2 code path.
        from . import followers as follower_accounts
        try:
            follower_accounts.parse(cfg)
        except follower_accounts.FollowerConfigError as exc:
            raise SystemExit(f'followers: {exc}')
    sink = None
    if mode == 'adapter-db':
        # Take the single-writer lock before touching the browser.
        sink = AdapterDbSink(Path(cfg['soup_adapter_db']).expanduser(), account_id=cfg['soup_account_id'],
                             ticker=cfg['ticker'],
                             context_path=Path(cfg.get('soup_context_path',
                                                       '~/.local/share/decrypt-os/soup/broker-context.json')).expanduser())

    state = AccountState()
    journal = None if followers_only else Journal(cfg['journal_dir'])
    bridge = Bridge(state, devtools_port=cfg['devtools_port'], page_url_contains=cfg['page_url_contains'],
                    trade_url_pattern=cfg['trade_url_pattern'])
    feed, pinned_executor = None, []
    feed_settings = None if followers_only else marketfeed.settings(cfg)   # checked above; None when off
    if feed_settings is not None and ('ticker' not in cfg or 'tick_size' not in cfg):
        raise SystemExit('config: market_feed needs the binding\'s ticker and tick_size; the bridge daemon does not start')
    if feed_settings is not None:
        # the 1-second feed for this binding's contract only. Read only: prices never open a trade.
        feed = marketfeed.SecondFeed(cfg['ticker'], tick_size=cfg['tick_size'],
                                     contract_id=lambda: getattr(pinned_executor[0], 'contract_id', None)
                                     if pinned_executor else None)
        bridge.extensions.append(marketfeed.PageFeed(
            feed, md_url_pattern=feed_settings['md_url_pattern'], trade_url_pattern=cfg['trade_url_pattern'],
            md_symbol=protocol.tradovate_symbol(cfg['ticker']), bridge_api=bridge.api))
        log.info('1-second feed on for %s', cfg['ticker'])
    publisher = server = publisher_thread = shutdown = housekeeping = fill_capture = peers = None
    try:
        bridge.attach(reload=not args.no_reload)
        log.info('waiting for the trader page to authorize and sync')
        bridge.wait_ready(timeout=cfg.get('ready_timeout_s', 120))
        if settings['peers']:
            # 1.1.0 cross-login hedge check (tvbridge.peers): fail open per peer, never stops this login.
            peers = PeerHedge(settings['peers'], own_token=token, scope=settings['peer_hedge_scope'])
        executor = None
        if followers_only:
            log.info('login %s (%s): followers only; no main account, no Soup publisher',
                     settings['login_id'], settings['label'])
        else:
            executor = Executor(bridge, state, journal, cfg)
            executor.resolve()
            pinned_executor.append(executor)
            log.info('bridge ready for account %s / %s', cfg['account_name'], executor.symbol)
            if cfg.get('bracket_orders_enabled') or cfg.get('flatten_all_enabled'):
                log.warning('extended order kinds enabled: bracket_orders=%s flatten_all=%s auto_protect=%s',
                            cfg.get('bracket_orders_enabled', False), cfg.get('flatten_all_enabled', False),
                            executor.bracket_protect)
            if peers is not None:
                # Leader rule across logins: its followers on a peer login never refuse its entry.
                executor.peer_hedge, executor.peer_exempt_roles = peers, LEADER_EXEMPT_ROLES

            evidence = Path(cfg['journal_dir']) / 'snapshots.jsonl'
            # Soup native exits: with bracket orders enabled, bracket trades are Soup's own trades,
            # so the adapter database follows them too (1.0.6.2). Otherwise unchanged.
            kinds = ('entry', 'bracket_entry') if mode == 'evidence' or cfg.get('bracket_orders_enabled') is True \
                else ('entry',)
            publisher = Publisher(executor, journal, state, mode, soup_url, evidence, sink=sink, trade_kinds=kinds)
            publisher_thread = threading.Thread(target=publisher.run, name='publisher', daemon=True)
            publisher_thread.start()
            executor.watchdogs.append(publisher.watchdog)
            if journal.unresolved():
                log.error('journal has unresolved commands; entries stay blocked until reconciled: %s',
                          [c['command_id'] for c in journal.unresolved()])
        login = LoginView(settings, bridge, state, cfg, executor, peers=peers)
        if followers_only:
            handler = make_followers_only_handler(login, token, read_token)
            executors = []
        else:
            handler = make_handler(executor, publisher, token, account_view_enabled, login, read_token, feed=feed)
            executors = [executor]
        pinned = {}
        if follower_accounts is not None:
            # Same page, socket and account state; own executors, journals and kill switches.
            pinned = follower_accounts.build(bridge, state, cfg, executor)
            login.followers = pinned
            extra = {'login_id': settings['login_id']} if settings['explicit'] else {}
            if followers_only:
                extra.update(balances=account_view_enabled is True, clock_ms=_now_ms)
            if read_token:
                extra['read_token'] = read_token  # read-only GET /followers for the panel
            handler = follower_accounts.make_follower_handler(handler, pinned, token, executor,
                                                             publisher=publisher, **extra)
            executors += follower_accounts.executors(pinned)  # drained too on a stop or lease loss
            for ex in follower_accounts.executors(pinned):
                ex.peer_hedge = peers  # followers check every peer account (no exemption)
            housekeeping = follower_accounts.Housekeeping(pinned, state).start()  # native followers only
            if followers_only and not pinned:
                log.info('login %s: connected and ready, no account assigned', settings['login_id'])
        if manual_token is not None:
            account_map = manual_routes.accounts(executor, pinned, manual_cfg['main_key'])
            handler = manual_routes.make_manual_handler(
                handler, account_map, manual_token, {k: v for k, v in manual_cfg.items() if k in manual_routes.TIMINGS},
                main_executor=executor, blocks=manual_routes.leader_blocks(pinned, settings['peers']))
            log.warning('manual flatten route on (%s) for: %s', manual_cfg['token_env'],
                        ', '.join(sorted(account_map)) or 'no account')
        if peers is not None:
            peers.start()
        if fill_capture_enabled:
            # 1.0.9.3: read-only fill prices into each account's journal dir (fills.jsonl). Its own
            # thread; a failure here is logged and never stops or delays the daemon or an order.
            try:
                from .fills import FillCapture
                fill_capture = FillCapture.for_daemon(
                    state, executor, {k: ex for k, ex in pinned.items() if isinstance(ex, Executor)}).start()
                log.info('fill capture on: %s', ', '.join(r.account for r in fill_capture.recorders))
            except Exception as exc:  # noqa: BLE001
                fill_capture = None
                log.error('fill capture not started (%s); orders are unaffected', type(exc).__name__)
        server = ThreadingHTTPServer(('127.0.0.1', cfg.get('listen_port', 8777)), handler)
        shutdown = Shutdown(bridge, executors, server, publisher, drain_s=cfg.get('shutdown_drain_s', DRAIN_S))
        signal.signal(signal.SIGTERM, lambda signum, frame: shutdown.request('sigterm', 0))
        threading.Thread(target=shutdown.watch, name='shutdown', daemon=True).start()
        server.serve_forever()
    finally:
        if peers:
            peers.stop()
        if housekeeping:
            housekeeping.stop()
        if fill_capture:
            fill_capture.stop()
        if publisher:
            publisher.stopping.set()
            with state.changed:
                state.changed.notify_all()
        try:
            if server:
                server.server_close()
            if publisher_thread:
                publisher_thread.join(timeout=7)
        finally:
            if not bridge.detach():
                log.error('bridge cleanup unverified; no automatic broker actions sent')
            if sink:
                sink.close()
    if shutdown is not None and shutdown.code:
        log.error('exiting with status %d (%s) so systemd re-attaches the bridge', shutdown.code, shutdown.reason)
        raise SystemExit(shutdown.code)


if __name__ == '__main__':
    main()
