"""Cross-login anti-hedge check (1.1.0 multi-login, `peer_logins` in config.json).

Before any entry (main or follower) the executor's local hedge guard sees only the accounts of its
own Tradovate login. With `peer_logins`, it also checks every peer login's latest GET /login report
(read only, the peer's read token) for positions and working orders. An opposite position, or a
working order that could open or add to an opposite position, in the entry's hedge group on a
counted peer account refuses the entry with `hedge_conflict_peer:<id>:<root>` (no account names,
as the local guard).

Rules:
- no network on the entry path: a background thread polls every peer every POLL_S (1.5 s, at most
  PEER_TIMEOUT_S per peer, never under an executor lock); conflict() reads only that cache;
- fail closed per peer: unreachable, timeout, an answer that is not a /login report, a peer
  page that is not logged in, a missing token, or a cached report older than STALE_S (10 s) means
  its exposure is unknown, so the entry is refused with `hedge_peer_unknown:<id>:<why>`. A logged-out
  or hung login still has its positions and working orders at Tradovate. Only exception: the leader
  rule below, when the peer's latest known account list (any report this daemon saw) has no account
  that could block this entry (e.g. a followers-only login B of this leader). The miss is logged at
  most once a minute per peer and reported as the /health note `peer_unreachable:<id>`;
- `peer_hedge_scope`: "configured" (default) counts only peer accounts whose role on that login is
  main or follower; "all" counts every visible account;
- a peer position or working row whose product root is unknown counts as a member of the entry's
  hedge group (fails closed, as the local guard does) when it opposes the entry, and shows as the
  /health note `peer_contract_unknown:<id>`;
- leader rule, extended across logins: the main account's entry is never refused by
  a follower (role "follower" on the peer); the followers align to the leader, as on one login.
  Follower entries check every counted peer account, the peer's main included;
- `hedge_guard: false` disables this together with the local check (the executor never calls it).

Nothing here sends an order or touches a page; the only traffic is loopback GET /login.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger('tvbridge')

LOGIN_SCHEMA = 'decrypt.tvbridge-login.v1'
PEER_TIMEOUT_S = 1.0
WARN_EVERY_S = 60.0
POLL_S = 1.5
STALE_S = 10.0
MAX_REPORT_BYTES = 2_000_000
# Leader rule: peer account roles the main account's entry never checks.
LEADER_EXEMPT_ROLES = frozenset({'follower'})
# peer_hedge_scope "configured": only accounts with one of these roles on their login count.
SCOPES = ('configured', 'all')
CONFIGURED_ROLES = frozenset({'main', 'follower'})


class PeerHedge:
    """The peers of one daemon. `conflict()` runs under an executor lock: it reads the poll cache only."""

    def __init__(self, peers, own_token=None, *, timeout_s=PEER_TIMEOUT_S, clock=time.monotonic,
                 environ=None, fetch=None, poll_s=POLL_S, scope='configured', stale_s=STALE_S):
        environ = os.environ if environ is None else environ
        if scope not in SCOPES:
            raise ValueError('peer_hedge_scope must be "configured" or "all"')
        self.timeout_s = min(float(timeout_s), PEER_TIMEOUT_S)
        self.clock, self.poll_s, self.scope, self.stale_s = clock, poll_s, scope, stale_s
        self.peers = []
        for p in peers:
            token = environ.get(p['token_env'], '')
            why = None
            if len(token) < 24:
                why = 'token_missing'
            elif own_token is not None and hmac.compare_digest(token.encode(), str(own_token).encode()):
                # The peer's token env holds this daemon's own token (e.g. TVBRIDGE_TOKEN in login B's
                # environment): every request would be refused, so the check could never run.
                why = 'token_is_own_token'
            if why:
                log.error('peer login %s: %s in %s; cross-login hedge check fails open for it',
                          p['id'], why, p['token_env'])
            self.peers.append({'id': p['id'], 'url': p['url'], 'token': None if why else token, 'config_error': why})
        self._fetch = fetch or self._http_get
        self._lock = threading.Lock()
        self.cache = {}        # peer id -> (clock at poll, report or None, reason of the miss or None)
        self.roster = {}       # peer id -> [{'role': ...}] of the latest /login report seen, logged in or not
        self.unknown = set()   # peer ids whose latest report has a counted row with an unknown root
        self._warned_at = {}   # peer id -> clock of the latest warning
        self.stopping = threading.Event()
        self.thread = None

    # --- fetching (poll thread only; never under an executor lock) ------------------------------

    def _http_get(self, url, timeout):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as r:
            return json.loads(r.read(MAX_REPORT_BYTES + 1))

    def _one(self, peer, out):
        if peer['token'] is None:
            out[peer['id']] = (None, peer['config_error'])
            return
        try:
            report = self._fetch(peer['url'] + '/login/' + peer['token'], self.timeout_s)
        except urllib.error.HTTPError as exc:
            out[peer['id']] = (None, 'http_%d' % exc.code)
            return
        except Exception as exc:  # noqa: BLE001 - any failure is a miss (fail open), never an order fault
            out[peer['id']] = (None, type(exc).__name__)
            return
        if not isinstance(report, dict) or report.get('schema') != LOGIN_SCHEMA or \
                not isinstance(report.get('accounts'), list):
            out[peer['id']] = (None, 'not_a_login_report')
        elif not isinstance(report.get('page'), dict) or report['page'].get('logged_in') is not True:
            out[peer['id']] = (None, 'logged_out', _roster(report))
        else:
            out[peer['id']] = (report, None)

    def poll_once(self):
        """Ask every peer once, in parallel, within PEER_TIMEOUT_S; refresh the cache. Network I/O:
        only the poll thread (and tests) call this, never the entry path."""
        out, threads = {}, []
        for peer in self.peers:
            t = threading.Thread(target=self._one, args=(peer, out), name='peer-' + peer['id'], daemon=True)
            t.start()
            threads.append((peer, t))
        deadline = self.clock() + self.timeout_s
        for peer, t in threads:
            t.join(max(0.0, deadline - self.clock()))
        now = self.clock()
        for peer, t in threads:
            report, why, *roster = out.get(peer['id'], (None, 'timeout'))
            roster = _roster(report) if report is not None else (roster[0] if roster else None)
            unknown = False
            if report is not None:
                try:
                    unknown = unknown_contracts(report, self.scope)
                except Exception:  # noqa: BLE001 - a malformed row never breaks the poll
                    unknown = False
            with self._lock:
                self.cache[peer['id']] = (now, report, None if report is not None else (why or 'timeout'))
                if roster is not None:
                    self.roster[peer['id']] = roster
                (self.unknown.add if unknown else self.unknown.discard)(peer['id'])
            if report is None:
                self._miss(peer['id'], why or 'timeout')
            elif unknown:
                self._warn(peer['id'], 'contract_unknown', 'peer login %s reports a position or working order on '
                           'an unknown contract: an opposite one refuses entries (fail closed)', peer['id'])

    def _fresh(self, peer_id, now):
        """(report, None) from the cache, or (None, reason of the miss)."""
        with self._lock:
            entry = self.cache.get(peer_id)
        if entry is None:
            return None, 'not_polled_yet'
        at, report, why = entry
        if now - at > self.stale_s:
            return None, 'stale'
        return report, why

    def reports(self):
        """{peer id: report} for every peer whose latest cached report is fresh and logged in. No network."""
        return {pid: report for pid, (report, _) in self.states().items() if report is not None}

    def states(self):
        """{peer id: (fresh logged-in report, None) or (None, why it is unknown)}. Logs misses. No network."""
        now, out = self.clock(), {}
        for peer in self.peers:
            report, why = self._fresh(peer['id'], now)
            if report is None:
                why = why or 'unreachable'
                self._miss(peer['id'], why)
            out[peer['id']] = (report, why)
        return out

    def unknown_blocking(self, exempt_roles=frozenset()):
        """Peer ids whose unknown exposure refuses entries for these exempt roles right now. No network."""
        now = self.clock()
        return sorted(peer['id'] for peer in self.peers
                      if self._fresh(peer['id'], now)[0] is None and not self._cannot_block(peer['id'], exempt_roles))

    def _cannot_block(self, peer_id, exempt_roles):
        """True when the peer's latest known account list has no account this entry would check (only
        exempt roles, for the leader: its own followers). Never true without a known list."""
        if not exempt_roles:
            return False
        with self._lock:
            roster = self.roster.get(peer_id)
        return roster is not None and not any(_counted(a, self.scope, exempt_roles) for a in roster)

    def _warn(self, peer_id, kind, text, *args):
        now = self.clock()
        with self._lock:
            last = self._warned_at.get((peer_id, kind))
            if last is not None and now - last < WARN_EVERY_S:
                return
            self._warned_at[(peer_id, kind)] = now
        log.warning(text, *args)

    def _miss(self, peer_id, why):
        self._warn(peer_id, 'unreachable', 'peer login %s unreachable (%s): its exposure is unknown, so entries '
                   'it could conflict with are refused (fail closed)', peer_id, why)

    def notes(self):
        """/health notes: `peer_unreachable:<id>` for each peer without a fresh logged-in report, and
        `peer_contract_unknown:<id>` while a peer's latest report has a counted row with an unknown root."""
        now, out = self.clock(), []
        for peer in self.peers:
            report, _ = self._fresh(peer['id'], now)
            if report is None:
                out.append('peer_unreachable:' + peer['id'])
            elif peer['id'] in self.unknown:
                out.append('peer_contract_unknown:' + peer['id'])
        return sorted(out)

    # --- the check (entry path: cache only) -------------------------------------------------------

    def conflict(self, side, members, exempt_roles=frozenset()):
        """`hedge_conflict_peer:<id>:<root>` if a `side` entry would oppose exposure on a peer,
        `hedge_peer_unknown:<id>:<why>` if a peer's exposure is unknown (fails closed), else None.
        A safety refusal only: it never changes or resizes an order."""
        if not self.peers:
            return None
        members = {str(m).upper() for m in members}
        unknown = None
        for peer_id, (report, why) in sorted(self.states().items()):
            if report is None:
                if unknown is None and not self._cannot_block(peer_id, exempt_roles):
                    unknown = f'hedge_peer_unknown:{peer_id}:{why}'
                continue
            try:
                root = account_conflict(report, side, members, exempt_roles, self.scope)
            except Exception as exc:  # noqa: BLE001 - a malformed report is unknown exposure: refuse
                self._miss(peer_id, 'bad_report:' + type(exc).__name__)
                unknown = unknown or f'hedge_peer_unknown:{peer_id}:bad_report'
                continue
            if root is not None:
                return f'hedge_conflict_peer:{peer_id}:{root}'
        return unknown

    # --- poll thread --------------------------------------------------------------------------------

    def start(self):
        if self.peers and self.thread is None:
            self.thread = threading.Thread(target=self._run, name='peer-logins', daemon=True)
            self.thread.start()
        return self

    def _run(self):
        while not self.stopping.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - observability only
                log.error('peer login poll error (%s)', type(exc).__name__)
            self.stopping.wait(self.poll_s)

    def stop(self, timeout=3):
        self.stopping.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout)


def _roster(report):
    """The account roles of a /login report (logged in or not), or None if it is not one."""
    if not isinstance(report, dict) or report.get('schema') != LOGIN_SCHEMA or not isinstance(report.get('accounts'), list):
        return None
    return [{'role': a.get('role')} for a in report['accounts'] if isinstance(a, dict)]


def _counted(acct, scope, exempt_roles=frozenset()):
    if not isinstance(acct, dict) or acct.get('role') in exempt_roles:
        return False
    return scope == 'all' or acct.get('role') in CONFIGURED_ROLES


def unknown_contracts(report, scope='configured'):
    """True when a counted account has a non-flat position or a working order whose root is unknown."""
    for acct in report.get('accounts') or []:
        if not _counted(acct, scope):
            continue
        for p in acct.get('positions') or []:
            if isinstance(p, dict) and p.get('root') is None and p.get('net') not in (0, None):
                return True
        for w in acct.get('working') or []:
            if isinstance(w, dict) and w.get('root') is None:
                return True
    return False


def account_conflict(report, side, members, exempt_roles=frozenset(), scope='configured'):
    """The product root of the first counted peer row with exposure opposite to `side` in `members`
    ('unknown' if it has none), else None.

    The same classification as Executor._hedge_conflict: an opposite position; a working order on
    the opposite side unless it only reduces a position held on the entry's side (remaining size
    within that position); a working order with unknown side or size. A row whose root is unknown
    counts as a hedge-group member (fails closed, as the local guard does) and returns
    'unknown'. `scope` "configured" counts only accounts with role main or follower; "all" counts
    every account.
    """
    opp = -1 if side == 'long' else 1

    def member(root):
        return root is None or root in members

    for acct in report.get('accounts') or []:
        if not _counted(acct, scope, exempt_roles):
            continue
        nets = {}
        for p in acct.get('positions') or []:
            if not isinstance(p, dict):
                continue
            net = p.get('net') if type(p.get('net')) is int else 0
            nets[p.get('contract')] = net
            if net * opp > 0 and member(p.get('root')):
                return p.get('root') or 'unknown'
        for w in acct.get('working') or []:
            if not isinstance(w, dict) or not member(w.get('root')):
                continue
            direction = {'Buy': 1, 'Sell': -1}.get(w.get('side'))
            if direction is None:
                return w.get('root') or 'unknown'
            if direction != opp:
                continue
            held = -opp * nets.get(w.get('contract'), 0) if w.get('contract') is not None else 0
            qty = w.get('qty')
            if held <= 0 or type(qty) is not int or qty > held:
                return w.get('root') or 'unknown'
    return None
