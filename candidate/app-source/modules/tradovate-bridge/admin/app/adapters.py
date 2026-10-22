"""Outside-world adapters: the read-only status feed, service restarts and the order adapter.

This build ships only safe adapters:
- StatusSource: GET-only reads of the Bridge panel's /api/status (or a fixture file in tests).
- SimulatedRestarter: records which units would restart; never calls systemctl.
- restarter.SystemdRestarter (profile `restarter: "systemd"`): the live restart sequence; see restarter.py.
- DryRunOrders: records the bridge call Flatten now would make; never sends anything.
- BridgeOrders (profile `order_adapter: "bridge"`): Flatten now through the owning daemon's /manual route.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from tree import dump_json, token_from_files


class StatusSource:
    """Positions and login state from the read-only Bridge panel. Cached for `ttl` seconds."""

    def __init__(self, url, ttl=2.0, timeout=4.0):
        self.url, self.ttl, self.timeout = url, ttl, timeout
        self._lock = threading.Lock()
        self._at, self._data, self._error = 0.0, None, 'not read yet'

    def _fetch(self):
        if self.url.startswith('file://'):
            with open(self.url[7:]) as f:
                return json.load(f)
        if not self.url.startswith('http://127.0.0.1:'):
            raise ValueError('status feed must be a loopback http URL')
        req = urllib.request.Request(self.url, method='GET', headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def read(self, fresh=False):
        with self._lock:
            if fresh or time.time() - self._at > self.ttl:
                try:
                    self._data, self._error = self._fetch(), None
                except Exception as exc:  # noqa: BLE001 - any failure means "unknown", never "flat"
                    self._data, self._error = None, f'{type(exc).__name__}: {str(exc)[:120]}'
                self._at = time.time()
            return self._data, self._error, self._at

    def cards(self, fresh=False):
        """{key: card} from every card the panel shows (followers view plus each login's cards)."""
        data, error, at = self.read(fresh)
        out = {}
        if data:
            for c in (data.get('followers') or {}).get('cards') or []:
                out.setdefault(c.get('key'), c)
            for lg in data.get('logins') or []:
                for c in lg.get('cards') or []:
                    out.setdefault(c.get('key'), c)
        return out, error, at


def card_flat(card):
    """True only when the panel positively shows no position and no working orders."""
    if not card:
        return None
    pos = (card.get('position') or {}).get('net')
    orders = ((card.get('orders') or {}).get('text') or '').strip().lower()
    if pos is None:
        return None
    return pos == 0 and orders in ('none', '')


class SimulatedRestarter:
    """Records restarts instead of running them. `fail_units` lets tests force a failed restart."""

    name = 'simulated'

    def __init__(self, log_path, fail_units=()):
        self.log_path = log_path
        self.fail_units = set(fail_units)

    def preflight(self, units):
        """Nothing to check for a simulation: ([problems], detail or None)."""
        return [], None

    def restart(self, units, change_id, progress=None, phase='apply', **_):
        results = []
        for u in units:
            ok = u['unit'] not in self.fail_units
            results.append({'unit': u['unit'], 'ok': ok,
                            'detail': 'simulated restart' if ok else 'simulated failure (test)'})
        self._log({'at': time.time(), 'change_id': change_id, 'restarts': results})
        return results

    def health(self, units):
        return {u['unit']: u['unit'] not in self.fail_units for u in units}

    def _log(self, entry):
        os.makedirs(os.path.dirname(self.log_path), mode=0o700, exist_ok=True)
        with open(self.log_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')


class DryRunOrders:
    """Flatten now in this build: write down the exact call, send nothing."""

    name = 'dry-run'

    def __init__(self, log_path):
        self.log_path = log_path

    def readiness(self, account):
        return [], []

    def flatten(self, account, change_id, reason=None):
        call = {
            'at': time.time(), 'change_id': change_id, 'account': account['key'],
            'login': account['login'], 'sent': False,
            'would_call': f"POST {account['bridge_url']}/manual/<order-token>/{account['key']}/flatten",
            'body': {'reason': 'operator_flatten_now', 'change_id': change_id,
                     'then': 'kill_switch_on', 'cancel_working_orders': True},
        }
        os.makedirs(os.path.dirname(self.log_path), mode=0o700, exist_ok=True)
        with open(self.log_path, 'a') as f:
            f.write(json.dumps(call) + '\n')
        return {'ok': True, 'dry_run': True, 'detail': 'Dry run: recorded the bridge call, sent nothing.',
                'call': call['would_call'], 'outcome_class': 'dry_run', 'sent': False, 'reply': None}


# Flatten now through the bridge (tvbridge 1.1.3, tvbridge/manual.py). Error classes for the page:
REFUSED = {'logged_out', 'not_synced', 'unhealthy', 'busy', 'daemon_stopping', 'kill_switch_unavailable'}
INCOMPLETE = {'cancel_not_confirmed', 'liquidation_rejected', 'not_flat', 'orders_still_working'}
UNKNOWN = {'liquidation_unknown', 'in_progress', 'exception_after_start'}
CHANGE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}\Z')     # the bridge's manual.CHANGE_ID_RE
HTTP_REQUEST_ERRORS = {400: 'the bridge rejected the request body', 403: 'the bridge rejected the manual token',
                       404: 'the bridge has no Flatten now route for this account (unknown key, or the '
                            'manual token is not set on that daemon)',
                       503: 'the bridge reports this account unavailable (not pinned)'}
WORDS = {
    'logged_out': 'Tradovate is logged out on this login', 'not_synced': 'the account is not synced',
    'unhealthy': 'the bridge is not healthy', 'busy': 'the account is busy with another order',
    'daemon_stopping': 'the bridge daemon is stopping', 'kill_switch_unavailable': 'its pause switch could not be set',
    'cancel_not_confirmed': 'Tradovate did not confirm the cancels in time, so nothing was liquidated',
    'liquidation_rejected': 'Tradovate rejected the liquidation', 'not_flat': 'the position is not flat yet',
    'orders_still_working': 'orders are still working', 'liquidation_unknown': 'the liquidation outcome is unknown',
    'in_progress': 'a flatten for this change is still in progress',
    'exception_after_start': 'the bridge failed after it started sending'}


def flatten_key(account):
    """The bridge's <key>: `main` for the login's main (leader) account, else the follower key."""
    return 'main' if account.get('daemon_role') == 'main' else account['key']


def classify_flatten(http_status, reply, transport_error=None):
    """(outcome_class, detail). outcome_class: flat | refused | incomplete | unknown | rejected_request.
    Only `flat` is success; a transport error, timeout or 5xx other than 503 is `unknown`."""
    if transport_error:
        return 'unknown', f'Unknown outcome: no answer from the bridge ({transport_error}). Check the Bridge window.'
    if http_status in HTTP_REQUEST_ERRORS:
        return 'rejected_request', f'Nothing sent: {HTTP_REQUEST_ERRORS[http_status]} (HTTP {http_status}).'
    if http_status != 200 or not isinstance(reply, dict):
        return 'unknown', (f'Unknown outcome: the bridge answered HTTP {http_status}'
                           f'{" (" + str((reply or {}).get("error")) + ")" if isinstance(reply, dict) else ""}. '
                           f'Usually nothing was sent; check the Bridge window.')
    dup = ' (answer to an earlier call with this change id; nothing sent again)' if reply.get('duplicate') else ''
    if reply.get('ok') is True:
        return 'flat', f'Flat with nothing working; pause switch {reply.get("kill_switch") or "on"}{dup}.'
    err = reply.get('error') or 'unknown'
    words = WORDS.get(err, err.replace('_', ' '))
    tail = (f' Net {reply.get("net_position")}, still working: {len(reply.get("still_working") or [])}.'
            f' Pause switch: {reply.get("kill_switch") or "not set"}.{dup}')
    if err in REFUSED:
        return 'refused', f'Nothing sent: {words}.{tail}'
    if err in INCOMPLETE:
        return 'incomplete', f'Not completed: {words}. Check the account in Tradovate.{tail}'
    return 'unknown', f'Unknown outcome: {words}. Check the Bridge window.{tail}'


class BridgeOrders:
    """Flatten now through the owning daemon's POST /manual/<manual token>/<key>/flatten (loopback only).
    One call per confirmed change, 60 s timeout, never retried here; a resend reuses the change id (the
    bridge answers a repeated change id from its journal and sends nothing twice)."""

    name = 'bridge'

    def __init__(self, profile, http_post=None, http_get=None, environ=None):
        cfg = profile.data.get('order_bridge') or {}
        self.p = profile
        self.timeout_s = float(cfg.get('timeout_s', 60))
        self.overrides = cfg.get('logins_override') or {}
        self.env = os.environ if environ is None else environ
        self.http_post = http_post or self._post
        self.http_get = http_get
        self.log_path = profile.st('flatten-calls.jsonl')

    def login(self, login_id):
        with open(self.p.tb('logins.json')) as f:
            items = {e['id']: e for e in json.load(f)['logins']}
        if self.p.mode == 'sandbox' and set(items) - set(self.overrides):
            raise RuntimeError('sandbox bridge adapter needs order_bridge.logins_override for every login')
        e = dict(items[login_id], **(self.overrides.get(login_id) or {}))
        return e

    def token_env(self, account, entry):
        try:
            with open(account['cfg_path']) as f:
                env = json.load(f).get('manual_token_env')
        except (OSError, ValueError, KeyError):
            env = None
        return env or entry.get('manual_token_env') or 'TVBRIDGE_MANUAL_TOKEN_' + account['login'].upper().replace('-', '_')

    def token(self, account, entry):
        name = self.token_env(account, entry)
        tok = self.env.get(name, '')
        if len(tok) < 24:   # a login added after this service started (profile token_files)
            tok = token_from_files(self.p, name, account['login']) or ''
        return name, (tok if len(tok) >= 24 else None)

    def readiness(self, account):
        """([blockers], [warnings]) before sending: the manual token is here and the login is synced."""
        from restarter import health_synced, loopback_get
        blockers = []
        try:
            entry = self.login(account['login'])
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            return [f'Cannot read the login registry ({type(exc).__name__}).'], []
        label = entry.get('label') or account['login']
        if urllib.parse.urlsplit(entry.get('bridge_url', '')).hostname not in ('127.0.0.1', 'localhost', '::1'):
            return [f'{label}: bridge_url is not loopback, so Flatten now is off.'], []
        name, tok = self.token(account, entry)
        if not tok:
            blockers.append(f'Flatten now needs the manual token {name} for {label}, and this service does not have '
                            f'it (manual-tokens drop-in). Nothing can be sent.')
        rname = entry.get('read_token_env')
        rtok = self.env.get(rname, '') if rname else ''
        if len(rtok) < 24:
            rtok = token_from_files(self.p, rname) or ''
        if len(rtok) < 24:
            blockers.append(f'Cannot check that {label} is signed in: {rname or "its read token"} is not available.')
        else:
            st, reply, err = (self.http_get or loopback_get)(entry['bridge_url'].rstrip('/') + '/health/' + rtok)
            if st != 200 or reply is None:
                blockers.append(f'{label}\'s bridge daemon does not answer ({err or st}). Flatten now needs it '
                                f'running and synced.')
            else:
                ok, why = health_synced(reply)
                if not ok:
                    blockers.append(f'{label} is not signed in and synced with Tradovate ({why}). Sign in to '
                                    f'Tradovate first; Flatten now never signs in.')
        return blockers, []

    def _post(self, url, body, timeout):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json'})
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, json.loads(r.read(400_000) or b'null'), None
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(100_000) or b'null')
            except ValueError:
                data = None
            return exc.code, data, None
        except Exception as exc:  # noqa: BLE001 - timeout, refused, reset: the outcome is unknown
            return None, None, f'{type(exc).__name__}: {str(exc)[:120]}'

    def flatten(self, account, change_id, reason='Flatten now (Bridge Advanced)'):
        if not CHANGE_ID_RE.match(change_id):
            return {'ok': False, 'dry_run': False, 'outcome_class': 'rejected_request', 'sent': False,
                    'detail': 'Nothing sent: the change id is not a valid bridge change id.', 'reply': None}
        entry = self.login(account['login'])
        name, tok = self.token(account, entry)
        if not tok:
            return {'ok': False, 'dry_run': False, 'outcome_class': 'rejected_request', 'sent': False,
                    'detail': f'Nothing sent: {name} is not available to this service.', 'reply': None}
        key = flatten_key(account)
        base = entry['bridge_url'].rstrip('/')
        if urllib.parse.urlsplit(base).hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise PermissionError('bridge_url is not loopback')
        started = time.time()
        status, reply, terr = self.http_post(f'{base}/manual/{tok}/{key}/flatten',
                                             {'reason': reason, 'change_id': change_id}, self.timeout_s)
        oc, detail = classify_flatten(status, reply, terr)
        out = {'ok': oc == 'flat', 'dry_run': False, 'outcome_class': oc, 'detail': detail, 'http_status': status,
               'transport_error': terr, 'reply': reply, 'key': key, 'login': account['login'],
               'call': f'POST {base}/manual/<manual token>/{key}/flatten', 'change_id': change_id,
               'seconds': round(time.time() - started, 2), 'unknown': oc == 'unknown',
               'sent': oc in ('flat', 'incomplete', 'unknown')}   # orders may have gone out
        with open(self.log_path, 'a') as f:
            f.write(json.dumps(dict(out, at=started)) + '\n')
        return out


def build(profile):
    if profile.order_adapter not in ('dry-run', 'bridge'):
        raise ValueError('order_adapter must be dry-run or bridge')
    if profile.restarter == 'simulated':
        restarter = SimulatedRestarter(profile.st('simulated-restarts.jsonl'),
                                       profile.data.get('simulate_fail_units', ()))
    elif profile.restarter == 'systemd':
        from restarter import SystemdRestarter  # live restarter
        restarter = SystemdRestarter(profile)
    else:
        raise ValueError('restarter must be simulated or systemd')
    orders = BridgeOrders(profile) if profile.order_adapter == 'bridge' else \
        DryRunOrders(profile.st('dry-run-orders.jsonl'))
    status = StatusSource(profile.status_url, ttl=profile.data.get('status_ttl_s', 2.0))
    return status, restarter, orders


__all__ = ['StatusSource', 'card_flat', 'SimulatedRestarter', 'DryRunOrders', 'BridgeOrders', 'build',
           'classify_flatten', 'flatten_key', 'dump_json']
