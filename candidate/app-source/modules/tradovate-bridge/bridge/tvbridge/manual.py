"""Flatten now (1.1.3): one operator-confirmed, risk-reducing flatten of ONE account, outside Soup.

    POST /manual/<manual token>/<key>/flatten     body {"reason": "...", "change_id": "..."}

The single exception to "the bridge only executes Soup's payload": a person (the admin page) asks
the daemon that owns the account to stop and close it. It only reduces risk. One account per call;
there is no flatten-all-accounts route.

Token: a separate narrow secret, accepted ONLY on /manual/* (never for /order, /follower, /health,
/login, ...), and the order and read tokens are never accepted here. Its variable is the config's
`manual_token_env`, default TVBRIDGE_MANUAL_TOKEN_<LOGIN ID> (TVBRIDGE_MANUAL_TOKEN_A on login A,
TVBRIDGE_MANUAL_TOKEN_B on login B). Unset or empty: the route does not exist (404), so installing
this release changes nothing until a token is set. Set: at least 24 characters, different from the
order and read tokens, and no follower keyed "main", else the daemon refuses to start.

<key>: a follower key of this daemon's config, `main` for the login's main (leader) account, or
the config's optional `main_key` (e.g. "account-a", the firing order's name for the leader).
The leader's kill switch is also every follower's main switch on its login and other logins' master
switch: the reply's `also_blocks` lists those accounts/logins (pausing the leader pauses the webhook).

Order of operations, modelled on Tradovate's own "Exit at Mkt & Cxl". The account executor's lock
(the one Soup's orders, the publisher and auto-protect use) is held while a request is sent and, from
the liquidation until flat is seen (at most `flatten_settle_s`), as flatten_all holds it, so no Soup
flatten_all can send a second liquidation. Every other wait runs with the lock released; each step
re-reads the position and orders under the lock and re-checks the page and connection first.
1. Set the account's kill switch first (entries refused from now on), with the admin page's note
   {"set_by": "tvbridge-admin", "reason", "change_id", "at"}. An existing switch is left as it is.
   No switch possible (none configured, or the file cannot be created): nothing else is done.
2. Refuse when the daemon is stopping, the page is logged out (`logged_out`) or the account is not
   synced for the current connection (`not_synced`). Never signs in. The switch stays on.
3. Under the lock, from the position read there: with a position, cancel only the orders that could
   open or add exposure (the opening side). Every closing-side order (take-profit, stop, unknown)
   keeps working: cancelling one leg of an OCO bracket can cancel its stop, and the liquidation
   handles them. Flat: cancel every working order. Then wait up to `manual_cancel_wait_s` (3 s) until
   Tradovate lists none of them working; not confirmed: stop here, `cancel_not_confirmed`.
4. Always: order/liquidateposition for that account and the pinned contract only, when the position
   read under the lock at send time is not 0 (skipped when flat). Rejected or unknown: every
   closing-side order, the stop included, is still working.
5. Clean-up loop (up to `manual_settle_s`, 5 s): whenever the account is flat with orders still
   working, cancel them once each under the lock (listed fresh). The run never ends flat with a
   working order whose cancel was not sent. With a position left, nothing more is cancelled.
Orders the bridge journaled as accepted but Tradovate does not list ("unseen") never block; they
are reported (`unseen_orders`) and left to auto-reconcile.
6. Journal: one `manual_flatten` command (source `operator`, trade_id null, no broker_order_id, so
   no Soup trade and no unseen-order check ever counts it) in the account's commands.jsonl, start
   fsynced before the first request. Refusals before any request go to refusals.jsonl only. The
   liquidation order id is kept in the receipt only; an unknown record auto-reconciles only as
   "flat and idle", never by matching a liquidation order. A result record that cannot be written
   leaves the command `unknown` in memory (never `in_progress` forever).
Time: `manual_max_s` (20 s) bounds the lock waits and passive waits. A request already sent finishes
(each up to `order_timeout_s`), at most MAX_CANCELS cancel requests are sent in one run, and the
clean-up cancel of a flat account is sent even when the budget is spent.
Each request is sent once: nothing is retried. ok is true only when the account is verified flat
with nothing working; otherwise ok false with what is still open, and the kill switch stays on.
`protective_stop_working`: a closing-side order with status Working whose type is a stop type or not
reported; a position left without one is logged as an error. A flat account with nothing working:
only the switch is set; ok true.
A repeated change_id answers the first call's result (`duplicate`), or `in_progress` while it runs.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from . import protocol
from .executor import REFUSALS_NAME, _with_reject_reason
from .journal import _append

log = logging.getLogger('tvbridge')

KIND = 'manual_flatten'
SOURCE = 'operator'
SET_BY = 'tvbridge-admin'
COMMAND_PREFIX = 'manual-flatten:'
MAIN = 'main'
CHANGE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z')
KEY_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}\Z')
ENV_NAME_RE = re.compile(r'^[A-Z_][A-Z0-9_]{0,127}\Z')
MAX_REASON = 200
MAX_BODY = 4096
MIN_TOKEN = 24
TIMINGS = {'manual_cancel_wait_s': 3.0, 'manual_settle_s': 5.0, 'manual_lock_wait_s': 10.0, 'manual_max_s': 20.0}
TIMING_MAX_S = 30.0
NOT_SYNCED = ('not_synced_for_current_connection', 'pinned_account_missing_from_sync', 'not_resolved')
# Order types that protect a position when they sit on its closing side (Tradovate orderType values).
PROTECTIVE_TYPES = ('Stop', 'StopLimit', 'TrailingStop', 'TrailingStopLimit')
# At most this many cancel requests in one run (the run's sends are not otherwise time-capped).
MAX_CANCELS = 20

_LOCKS_GUARD = threading.Lock()


def default_token_env(login_id):
    return 'TVBRIDGE_MANUAL_TOKEN_' + login_id.upper().replace('-', '_')


def settings(cfg, login_settings):
    """The daemon config's manual keys, validated (ValueError). Reads no environment, sends nothing.

    manual_token_env (default TVBRIDGE_MANUAL_TOKEN_<LOGIN ID>), main_key (optional), and the bounded
    times manual_cancel_wait_s / manual_settle_s / manual_lock_wait_s / manual_max_s (0 < s <= 30).
    """
    env = cfg.get('manual_token_env', default_token_env(login_settings['login_id']))
    if not isinstance(env, str) or not ENV_NAME_RE.match(env):
        raise ValueError('manual_token_env must be an environment variable name')
    if env in (login_settings['token_env'], login_settings['read_token_env']):
        raise ValueError('manual_token_env must differ from token_env and read_token_env')
    main_key = cfg.get('main_key')
    if main_key is not None:
        if not isinstance(main_key, str) or not KEY_RE.match(main_key) or main_key == MAIN \
                or main_key in _follower_keys(cfg):
            raise ValueError('main_key must be a lowercase slug other than "main" and every follower key')
        if login_settings['mode'] == 'followers_only':
            raise ValueError('main_key needs a main account (not valid with mode "followers_only")')
    out = {'token_env': env, 'main_key': main_key}
    for name, default in TIMINGS.items():
        value = cfg.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= TIMING_MAX_S:
            raise ValueError(f'{name} must be a number of seconds above 0 and at most {TIMING_MAX_S:g}')
        out[name] = float(value)
    return out


def _follower_keys(cfg):
    return [f.get('key') for f in cfg.get('followers') or [] if isinstance(f, dict)]


def check_aliases(cfg):
    """With the route on: no follower may be keyed "main" (the leader's name on /manual). ValueError."""
    if MAIN in _follower_keys(cfg):
        raise ValueError('a follower keyed "main" collides with the manual route\'s name for the main account')


def load_token(env, name, others):
    """The manual token from `env` (a mapping), or None when unset/empty (route off). Raises ValueError
    when set but shorter than 24 characters or equal to one of `others` (the order and read tokens)."""
    token = env.get(name, '') or None
    if token is None:
        return None
    if len(token) < MIN_TOKEN:
        raise ValueError(f'{name} must hold at least {MIN_TOKEN} characters when set')
    if any(o and hmac.compare_digest(token.encode(), o.encode()) for o in others):
        raise ValueError(f'{name} must differ from the order and read tokens')
    return token


def accounts(main_executor, followers, main_key=None):
    """key -> executor (or tvbridge.followers.Unavailable). `main` and `main_key` name the main account."""
    out = dict(followers or {})
    if main_executor is not None:
        out.setdefault(MAIN, main_executor)
        if main_key:
            out[main_key] = main_executor
    return out


def leader_blocks(followers, peers):
    """What the leader's kill switch also stops: every follower of this login (their main switch) and
    each peer login, whose followers_only config names it as master_kill_switch_file (login B today)."""
    return sorted(followers or {}) + ['login:' + p['id'] for p in peers or []]


def set_kill_switch(path, reason, change_id, now):
    """Create the kill-switch file with the admin page's note. 'set', 'already_set' (left untouched),
    or None (not configured or not creatable: the switch is NOT on)."""
    if not isinstance(path, str) or not path:
        return None
    note = {'set_by': SET_BY, 'reason': reason, 'change_id': change_id, 'at': round(now, 3)}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        result = 'already_set'
    except OSError as exc:
        log.error('manual flatten: kill switch could not be set (%s): %s', type(exc).__name__, path)
        return None
    else:
        result = 'set'
        try:
            os.write(fd, (json.dumps(note, indent=2) + '\n').encode())
            os.fsync(fd)
        except OSError as exc:  # the file exists, so the switch is on; only the note is incomplete
            log.error('manual flatten: kill switch note not written (%s): %s', type(exc).__name__, path)
        finally:
            os.close(fd)
    if not os.path.lexists(path):  # the bridge pauses on anything at the path, a dangling symlink included
        log.error('manual flatten: kill switch file is not there after setting it: %s', path)
        return None
    if result == 'set':
        log.error('kill switch set (manual flatten %s): %s', change_id, path)
    return result


def _problem(problems):
    if any(p.startswith('page_state:') for p in problems):
        return 'logged_out'
    if any(p in NOT_SYNCED for p in problems):
        return 'not_synced'
    return 'unhealthy'


def _opening(ex, working, net):
    """Working order ids on the opening side of position `net` (not 0): they could open or add exposure.
    Orders on the closing side, and orders whose side Tradovate has not reported, are not included."""
    close = 'Sell' if net > 0 else 'Buy'
    with ex.state.lock:
        return [oid for oid in working if (ex.state.order(oid) or {}).get('action') in ('Buy', 'Sell')
                and ex.state.order(oid).get('action') != close]


def _view(ex):
    """The pinned account/contract: net (None if unresolved), working ids Tradovate lists, unseen
    accepted ids (journal only), and the protective stops for the current position: closing side,
    status Working (not PendingCancel/Suspended), type a stop type or not reported."""
    if ex.account_id is None or ex.contract_id is None:
        return {'net': None, 'working': [], 'unseen': [], 'stops': []}
    with ex.state.lock:
        net = ex.state.net_position(ex.account_id, ex.contract_id)
        working = ex.state.open_orders(ex.account_id, ex.contract_id)
        stops = []
        if net:
            close = 'Sell' if net > 0 else 'Buy'
            for o in working:
                order = ex.state.order(o) or {}
                otype = (ex.state.order_version(o) or {}).get('orderType')
                if order.get('action') == close and order.get('ordStatus') == 'Working' and \
                        (otype in PROTECTIVE_TYPES or otype is None):
                    stops.append(o)
    return {'net': net, 'working': working, 'unseen': [int(o) for o in ex._unseen_accepted_orders()], 'stops': stops}


def _wait(pred, seconds):
    deadline = time.time() + max(0.0, seconds)
    while True:
        if pred():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


def _manual_lock(ex):
    """One manual flatten at a time per account (the executor lock is released while it waits)."""
    with _LOCKS_GUARD:
        lock = ex.__dict__.get('_manual_flatten_lock')
        if lock is None:
            lock = ex.__dict__['_manual_flatten_lock'] = threading.Lock()
        return lock


def _record_refusal(ex, cid, error, change_id):
    try:
        _append(Path(ex.journal.dir) / REFUSALS_NAME,
                {'at': ex.clock_ms(), 'command_id': cid, 'trade_id': None, 'kind': KIND, 'reason': error,
                 'source': SOURCE, 'change_id': change_id})
    except Exception as exc:  # noqa: BLE001 - a refusal record never changes the answer
        log.error('manual flatten refusal record not written (%s)', type(exc).__name__)


def _reply(ex, key, cid, v, also_blocks, **fields):
    out = {'account': key, 'account_id': ex.account_id, 'command_id': cid, 'entries_disabled': ex.entries_disabled(),
           'net_position': v['net'], 'still_working': [str(o) for o in v['working']],
           'unseen_orders': [str(o) for o in v['unseen']], 'protective_stop_working': bool(v['stops']),
           'protected': v['net'] == 0 or bool(v['stops'])}
    if also_blocks is not None:
        out['also_blocks'] = list(also_blocks)
    out.update(fields)
    return out


def _refused(ex, key, cid, error, switch, change_id, also_blocks=None, problems=None):
    log.error('manual flatten %s on %s refused: %s%s', change_id, key, error,
              (' (' + ','.join(problems) + ')') if problems else '')
    _record_refusal(ex, cid, error, change_id)
    receipt = {'bridge': 'refused', 'reason': error}
    if problems:
        receipt['problems'] = list(problems)
    return _reply(ex, key, cid, _view(ex), also_blocks, ok=False, error=error, kill_switch=switch, cancelled=[],
                  outcome=None, receipt=receipt)


def _prior(ex, key, prior, also_blocks=None):
    """The answer to a repeated change_id: the first call's result, or in_progress while it runs."""
    receipt = prior.get('receipt') or {}
    if prior.get('outcome') is None:
        return _reply(ex, key, prior['command_id'], _view(ex), also_blocks, ok=False, error='in_progress',
                      duplicate=True, kill_switch=None, cancelled=[], outcome=None, receipt={})
    return _reply(ex, key, prior['command_id'], _view(ex), also_blocks, ok=receipt.get('ok') is True,
                  error=receipt.get('error'), duplicate=True, kill_switch=receipt.get('kill_switch'),
                  cancelled=list(receipt.get('cancelled') or []), outcome=prior.get('outcome'), receipt=receipt)


def flatten(ex, key, reason, change_id, timings=None, clock=time.time, also_blocks=None):
    """Flatten one account (see the module docstring). Returns the reply dict; never raises for a
    broker fault (an exception after the journal start is an `unknown` outcome)."""
    t = dict(TIMINGS, **(timings or {}))
    cid = COMMAND_PREFIX + change_id
    prior = ex.journal.get(cid)
    if prior is not None:
        return _prior(ex, key, prior, also_blocks)
    switch = set_kill_switch(ex.cfg.get('kill_switch_file'), reason, change_id, clock())
    if switch is None:
        return _refused(ex, key, cid, 'kill_switch_unavailable', None, change_id, also_blocks)
    one = _manual_lock(ex)
    if not one.acquire(blocking=False):
        return _refused(ex, key, cid, 'busy', switch, change_id, also_blocks)
    try:
        run = _Run(ex, key, cid, reason, change_id, switch, t, also_blocks)
        error, answer = run.locked(run.start, first=True)
        if error:
            return _refused(ex, key, cid, error, switch, change_id, also_blocks, run.problems)
        return answer if answer is not None else run.finish()
    finally:
        one.release()


class _Run:
    def __init__(self, ex, key, cid, reason, change_id, switch, t, also_blocks):
        self.ex, self.key, self.cid, self.reason, self.change_id = ex, key, cid, reason, change_id
        self.switch, self.t, self.also_blocks = switch, t, also_blocks
        self.deadline = time.monotonic() + t['manual_max_s']
        self.epoch = self.problems = None
        self.uncertain = self.rejected = self.liquidation_unknown = False
        self.touched = set()
        self.cancel_sent = set()
        self.receipt = {}

    def remaining(self):
        return self.deadline - time.monotonic()

    def wait(self, pred, seconds):
        """A passive wait, never under the executor lock, inside the run's time budget."""
        return _wait(pred, min(seconds, self.remaining()))

    def locked(self, fn, first=False, past_budget=False):
        """fn() under the executor lock after re-checking the daemon, page and connection.
        Returns (error, None) or (None, fn's result). `past_budget`: the clean-up cancel of a flat
        account, still sent when the run's time is spent (lock wait up to manual_lock_wait_s)."""
        if past_budget:
            wait = max(self.remaining(), self.t['manual_lock_wait_s'])
        else:
            wait = min(self.t['manual_lock_wait_s'], self.remaining()) if first else self.remaining()
        if wait <= 0:
            return 'time_budget_exceeded', None
        if not self.ex.lock.acquire(timeout=wait):
            return 'busy', None
        try:
            stopping = getattr(self.ex.bridge, 'stop_reason', None)
            if stopping:
                self.problems = [str(stopping)]
                return 'daemon_stopping', None
            health = self.ex.health(include_watchdogs=False)  # a publisher fault never blocks reducing risk
            if not health['ok']:
                self.problems = health['problems']
                return _problem(health['problems']), None
            epoch = health['page'].get('epoch')
            if self.epoch is None:
                self.epoch = epoch
            elif epoch != self.epoch:
                return 'connection_changed', None
            return None, fn()
        finally:
            self.ex.lock.release()

    def start(self):
        """Under the lock: a duplicate answers from the journal; else the journal start record."""
        ex = self.ex
        prior = ex.journal.get(self.cid)
        if prior is not None:
            return _prior(ex, self.key, prior, self.also_blocks)
        v = _view(ex)
        with ex.state.lock:
            baseline = ex.state.max_order_id(ex.account_id)
        mode = 'liquidate' if v['net'] else ('cancel' if v['working'] else 'noop')
        plan = {'mode': mode}
        if v['net']:
            plan.update(close_action='Sell' if v['net'] > 0 else 'Buy', baseline_order_id=baseline)
        self.receipt = {'source': SOURCE, 'account': self.key, 'account_id': ex.account_id, 'contract': ex.symbol,
                        'reason': self.reason, 'change_id': self.change_id, 'kill_switch': self.switch, 'mode': mode,
                        'net_before': v['net'], 'working_before': [str(o) for o in v['working']],
                        'unseen_before': [str(o) for o in v['unseen']],
                        'protective_stops_before': [str(o) for o in v['stops']]}
        if self.also_blocks is not None:
            self.receipt['also_blocks'] = list(self.also_blocks)
        self.touched.update(v['working'])  # `cancelled` also reports what Tradovate's liquidation cancelled
        ex.journal.start(self.cid, trade_id=None, kind=KIND, side=None, quantity=abs(v['net']) or None, tag=None,
                         at=ex.clock_ms(), source=SOURCE, reason=self.reason, change_id=self.change_id,
                         account_key=self.key, **plan)
        log.warning('manual flatten %s on %s: net %s, working %s (protective stops %s), unseen %s; kill switch %s',
                    self.change_id, self.key, v['net'], v['working'], v['stops'], v['unseen'], self.switch)
        if v['net'] and not v['stops']:
            log.error('manual flatten %s on %s: position %s has NO working protective stop', self.change_id,
                      self.key, v['net'])
        return None

    def finish(self):
        ex = self.ex
        try:
            error = self.execute()
        except Exception as exc:  # noqa: BLE001 - after the journal start, any failure is an unknown outcome
            error, self.uncertain = 'exception_after_start', True
            self.receipt['exception'] = type(exc).__name__
        v = _view(ex)
        with ex.state.lock:
            cancelled = sorted(str(o) for o in self.touched if (ex.state.order(o) or {}).get('ordStatus') == 'Canceled')
        ok = error is None and v['net'] == 0 and not v['working']
        if not ok and error is None:
            error = 'not_flat' if v['net'] != 0 else 'orders_still_working'
        outcome = 'unknown' if self.uncertain else ('rejected' if self.rejected else 'accepted')
        self.receipt.update(ok=ok, error=error, net_after=v['net'], cancelled=cancelled,
                            still_working=[str(o) for o in v['working']], unseen_orders=[str(o) for o in v['unseen']],
                            protective_stop_working=bool(v['stops']), finished_at=ex.clock_ms())
        try:
            ex.journal.finish(self.cid, outcome, self.receipt)
        except Exception:
            # The result is not on disk: after a restart the start record reads `unknown`. Say the same
            # now, so the change_id never answers in_progress forever.
            with ex.journal.lock:
                rec = ex.journal.commands.get(self.cid)
                if rec is not None and rec.get('outcome') is None:
                    rec.update(outcome='unknown', receipt=dict(self.receipt, ok=False, error='journal_write_failed',
                                                               journal_result_written=False))
            log.error('manual flatten %s on %s: journal result not written; the command stays unknown',
                      self.change_id, self.key)
            raise
        if v['net'] and not v['stops']:
            log.error('MANUAL FLATTEN %s ON %s LEFT POSITION %s WITHOUT A WORKING PROTECTIVE STOP (%s); check '
                      'Tradovate now', self.change_id, self.key, v['net'], error)
        if v['unseen']:
            log.warning('manual flatten %s on %s: accepted orders not listed by Tradovate (left to auto-reconcile): %s',
                        self.change_id, self.key, v['unseen'])
        (log.warning if ok else log.error)('manual flatten %s on %s: %s (outcome %s, net %s, still working %s)',
                                           self.change_id, self.key, 'flat, nothing working' if ok else error,
                                           outcome, v['net'], v['working'] or 'none')
        return _reply(ex, self.key, self.cid, v, self.also_blocks, ok=ok, error=error, kill_switch=self.switch,
                      cancelled=cancelled, outcome=outcome, receipt=self.receipt)

    # --- steps: each decision read under the lock at send time ---------------------------------

    def execute(self):
        """Error string or None. Each request is sent at most once."""
        error = self.cancel_opening()
        if error:
            return error
        error = self.liquidate()  # skipped when the position read under the lock is 0
        if error:
            return error
        return self.clean_up()

    def _cancel(self, targets):
        """Caller holds the lock. Cancel requests for targets not sent before, within MAX_CANCELS."""
        todo = [o for o in targets if o not in self.cancel_sent][:max(0, MAX_CANCELS - len(self.cancel_sent))]
        if not todo:
            return [], None, {}
        self.cancel_sent.update(todo)
        self.touched.update(todo)
        out, rec, _ = self.ex._cancel_all(self.epoch, targets=todo)
        self.uncertain |= out != 'accepted'
        return todo, out, rec.get('cancel_targets', {})

    def cancel_opening(self):
        """Position open: orders that could open or add exposure; flat: every working order. Then confirm."""
        ex = self.ex

        def send():
            v = _view(ex)  # read under the lock: the decision is made from this position
            targets = _opening(ex, v['working'], v['net']) if v['net'] else list(v['working'])
            sent, out, results = self._cancel(targets)
            left = sorted(set(v['working']) - set(targets))
            return v['net'], sent, out, results, left
        error, result = self.locked(send)
        if error:
            return error
        net, sent, out, results, left = result
        self.receipt['cancel'] = {'net_at_send': net, 'outcome': out, 'targets': results,
                                  'left_for_liquidation': [str(o) for o in left]}
        if not sent:
            self.receipt['cancel']['confirmed'] = True
            return None
        confirmed = self.wait(lambda: not set(sent) & set(_view(ex)['working']), self.t['manual_cancel_wait_s'])
        self.receipt['cancel']['confirmed'] = confirmed
        return None if confirmed else 'cancel_not_confirmed'

    def liquidate(self):
        """order/liquidateposition (verified body and reply) when the position is not 0, then hold
        the lock until flat is seen (at most flatten_settle_s), as Executor._flatten_all does."""
        ex = self.ex

        def send():
            with ex.state.lock:
                net = ex.state.net_position(ex.account_id, ex.contract_id)
                baseline = ex.state.max_order_id(ex.account_id)
            if net == 0:
                return None
            plan = {'mode': 'liquidate', 'quantity': abs(net), 'close_action': 'Sell' if net > 0 else 'Buy',
                    'baseline_order_id': baseline}
            body = {'accountId': ex.account_id, 'contractId': ex.contract_id, 'admin': False}
            r = ex.bridge.request('order/liquidateposition', body, timeout=ex.cfg.get('order_timeout_s', 5),
                                  expected_epoch=self.epoch)
            result = _liquidation_result(ex, plan, r)
            if result[0] != 'rejected':
                _wait(lambda: _view(ex)['net'] == 0, float(ex.cfg.get('flatten_settle_s', 3.0)))
            return plan, result
        error, result = self.locked(send)
        if error:
            return error
        if result is None:
            self.receipt['liquidation'] = {'skipped': 'flat when the liquidation would have been sent'}
            return None
        plan, (out, rec, oid) = result
        self.receipt['liquidation'] = dict(rec, outcome=out, quantity=plan['quantity'],
                                           close_action=plan['close_action'])
        if oid:
            self.receipt['liquidation_order_id'] = str(oid)
        if out == 'rejected':
            self.rejected = True
            return 'liquidation_rejected'
        if out == 'unknown':
            self.uncertain = self.liquidation_unknown = True
        return None

    def clean_up(self):
        """Bounded loop: flat with orders listed working -> cancel them once (fresh list, under the lock);
        until flat with nothing working or manual_settle_s. With a position, nothing is cancelled."""
        ex = self.ex
        grace = float(ex.cfg.get('flatten_cancel_grace_s', 0.5))  # Tradovate's own cancels first
        until = time.monotonic() + max(0.0, min(self.t['manual_settle_s'], self.remaining()))
        flat_since = None
        while True:
            v = _view(ex)
            if v['net'] == 0 and not v['working']:
                return None
            now = time.monotonic()
            flat_since = (flat_since or now) if v['net'] == 0 else None
            unsent = [o for o in v['working'] if o not in self.cancel_sent]
            if len(self.cancel_sent) >= MAX_CANCELS:
                unsent = []
            last = now >= until
            if v['net'] == 0 and unsent and (last or now - flat_since >= grace):
                error = self._clean_up_send(past_budget=last)
                if error:
                    return error
            elif last:
                break
            time.sleep(0.05)
        return 'liquidation_unknown' if self.liquidation_unknown and _view(ex)['net'] != 0 else None

    def _clean_up_send(self, past_budget=False):
        ex = self.ex

        def send():
            v = _view(ex)
            if v['net'] != 0:
                return None  # a position again: its stop keeps working
            sent, out, results = self._cancel(v['working'])
            return sent, out, results
        error, result = self.locked(send, past_budget=past_budget)
        if error:
            return error
        if result and result[0]:
            self.receipt.setdefault('leftover_cancel', []).append(
                {'outcome': result[1], 'targets': result[2]})
        return None


def _liquidation_result(ex, plan, r):
    """(outcome, receipt, order id or None) of a liquidateposition reply, as Executor._flatten_all
    decides it, without its leftover cancel (done by _Run.clean_up)."""
    d = r.get('d') if isinstance(r.get('d'), dict) else {}
    if r.get('ok') and type(d.get('orderId')) is int and not d.get('failureReason'):
        oid = str(d['orderId'])
        return 'accepted', {'broker_order_id': oid, 'mode': 'liquidate'}, oid
    if not r.get('sent'):
        return 'rejected', {'bridge': 'not_sent', 'code': str(r.get('code'))[:120]}, None
    failure = d.get('failureReason') if r.get('ok') else None
    detail = {'mode': 'liquidate', 'code': str(r.get('code'))[:120], 's': r.get('s'),
              'failureReason': str(failure)[:120] if failure else None,
              'failureText': str(d.get('failureText', ''))[:240] if failure else None}
    found = []
    _wait(lambda: found.append(ex._find_liquidation(plan)) or found[-1] is not None, ex.cfg.get('tag_wait_s', 1.5))
    if found and found[-1] is not None:
        oid = str(found[-1])
        return 'accepted', dict(detail, broker_order_id=oid, matched_by_liquidation_order=True), oid
    if failure or r.get('s') in protocol.DEFINITE_REFUSAL_STATUSES:
        return 'rejected', _with_reject_reason(detail, failure, d.get('failureText')), None
    return 'unknown', dict(detail, transport='uncertain'), None


def _body(handler):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= MAX_BODY:
            raise ValueError('body size')
        payload = json.loads(handler.rfile.read(size))
    except (ValueError, OSError):
        return None
    if not isinstance(payload, dict) or set(payload) != {'reason', 'change_id'}:
        return None
    reason, change_id = payload['reason'], payload['change_id']
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON or \
            not isinstance(change_id, str) or not CHANGE_ID_RE.match(change_id):
        return None
    return ' '.join(reason.split()), change_id


def make_manual_handler(base, account_map, token, timings=None, main_executor=None, blocks=None):
    """Wrap the daemon's handler class: POST /manual/<token>/<key>/flatten here, everything else unchanged.
    Only built when a manual token is configured; without it /manual/* is the base handler's 404.
    `blocks` (leader_blocks) is reported as `also_blocks` when the key names `main_executor`."""
    from .followers import Unavailable  # imported here: tvbridge.followers imports tvbridge.daemon
    secret = token.encode()

    class Handler(base):
        def do_POST(self):
            parts = urllib.parse.urlsplit(self.path).path.split('/')
            if len(parts) < 2 or parts[1] != 'manual':
                return super().do_POST()
            if len(parts) < 3 or not hmac.compare_digest(parts[2].encode('latin-1', 'replace'), secret):
                return self.respond(403, {'error': 'forbidden'})
            if len(parts) != 5 or parts[4] != 'flatten':
                return self.respond(404, {'error': 'not found'})
            ex = account_map.get(parts[3])
            if ex is None:
                return self.respond(404, {'error': 'unknown_account'})
            body = _body(self)
            if body is None:
                return self.respond(400, {'error': 'invalid request'})
            if isinstance(ex, Unavailable):
                return self.respond(503, {'error': 'account_unavailable', 'reason': ex.reason, 'account': parts[3]})
            reason, change_id = body
            also = list(blocks or []) if main_executor is not None and ex is main_executor else None
            try:
                result = flatten(ex, parts[3], reason, change_id, timings, also_blocks=also)
            except Exception as exc:  # noqa: BLE001 - e.g. the journal cannot be written; never a traceback
                log.error('manual flatten %s on %s failed (%s); check the account in Tradovate', change_id, parts[3],
                          type(exc).__name__)
                return self.respond(500, {'error': 'internal_error', 'type': type(exc).__name__,
                                          'account': parts[3], 'entries_disabled': ex.entries_disabled()})
            return self.respond(200, result)

    Handler.__name__ = 'ManualHandler'
    return Handler
