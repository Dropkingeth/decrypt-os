"""The live restarter: restart bridge services after a structural change, then prove they came back.

Sequence, one unit at a time, no retry loops:
1. stop tvbridge-firing.service when a daemon or the firing service is in the plan, so no follower
   fires mid-restart;
2. restart each affected daemon (tvbridge-daemon.service, tvbridge-daemon@<id>.service) and wait up to
   `daemon_wait_s` (60 s) for it to be synced (below);
3. start tvbridge-firing.service and wait for firing/status.json to be rewritten by the new process
   (under `firing_fresh_s`, 5 s old, its pid = the unit's MainPID, its firing order = firing.json);
4. restart tvbridge-status.service and wait for GET /api/status to answer 200.
Soup (the profile's optional `soup_service` unit) and every other unit are refused outright.

Daemon health is read exactly as the Bridge status panel reads it:
- synced: the daemon's GET /health/<read token> says page state "authorized" and page.synced true, with
  no "not_synced_for_current_connection" problem (the panel's login_connection /health view). The read
  token comes from the login's `read_token_env` (logins.json); the order token is never used.
- A daemon is never restarted while its login is logged out or not synced: the daemon
  serves HTTP only after the page is signed in (bridge.wait_ready, 120 s), so a logged-out restart exits,
  systemd retries and the unit ends `failed`. The engine keeps such a change waiting for the next sign-in
  (`signin_wait`); preflight, the post-restart check and verify accept only "synced".
- Logged out is still *recognised* (never accepted as healthy): the login guard's status.json (the login's
  guard_state_dir) is fresh (180 s, the panel's GUARD_STALE_SECONDS) and says logged out (state
  logged_out_waiting or logged_out_since set; or needs_human/login_repeated for a login reason), as the
  panel's guard_login/login_connection decide. It is used for (a) the wait reason, (b) refusing to restart
  a daemon whose login logged out after the wait check passed (a race), and (c) failing fast instead of
  waiting 60 s when the login logs out during the restart. Nothing here signs in, clicks or reloads.

Safety rules:
- systemd start limits: StartLimitBurst/StartLimitIntervalUSec are read per unit and this page's own recent
  starts come from restarts.jsonl (one line per start, written when issued). A change starts only when every
  unit it touches has at least 2 starts left (apply + restore); otherwise the engine keeps it waiting.
- apply: a daemon is restarted only when a reading taken right before its restart says synced.
- restore: only daemons this change actually restarted are restarted again, and only when the login guard
  has a fresh reading that does not say logged out. Daemons can also be left alone on purpose (not flat).
- firing is started with --job-mode=ignore-requirements, so its Wants=tvbridge-daemon.service never starts
  daemon A as a side effect; preflight also requires daemon A to be active whenever firing starts.
- lanes the firing service refuses at start are compared before/after (refused_lanes).

Every outside call goes through `run` (systemctl argv) and `http_get`, so tests inject fakes; the sandbox
profile may only point `systemd.systemctl` at tools/fake_systemctl.py (tree.Profile enforces it).
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from tree import token_from_files

LIVE_SYSTEMCTL = '/usr/bin/systemctl'
FIRING = 'tvbridge-firing.service'
STATUS = 'tvbridge-status.service'
DAEMON_RE = re.compile(r'^tvbridge-daemon(@[a-z][a-z0-9-]{0,15})?\.service\Z')
GUARD_STALE_S = 180            # tvbridge-status GUARD_STALE_SECONDS
FIRING_PANEL_STALE_S = 20      # tvbridge-status FIRING_STALE_SECONDS (pre-check only)
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
PROPS = ('ActiveState', 'SubState', 'MainPID', 'NRestarts', 'Result')
PRIMARY_DAEMON = 'tvbridge-daemon.service'     # tvbridge-firing.service has Wants= on it
STARTS_NEEDED = 2                              # one for the apply run, one for the restore run
COMMAND_TIMEOUT_S = 180                        # above the daemons' TimeoutStartSec=150
_UNITS = {'us': 1e-6, 'ms': 1e-3, 's': 1, 'sec': 1, 'm': 60, 'min': 60, 'h': 3600, 'hr': 3600, 'd': 86400}


def parse_ts(v):
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def guard_state(g, now):
    """('out'|'in'|'stale'|'none', reason) from a login-guard status.json, with the status panel's rules:
    guard_login() ('out') plus login_connection()'s needs_human/login_repeated case."""
    if not isinstance(g, dict):
        return 'none', 'no login-guard reading'
    checked = parse_ts(g.get('checked_at'))
    if checked is None or now - checked > GUARD_STALE_S:
        return 'stale', 'login-guard reading is stale'
    reason = str(g.get('reason') or '')
    if g.get('state') in ('needs_human', 'login_repeated') and ('daemon' not in reason or g.get('logged_out_since')):
        return 'out', (reason or g.get('state')).replace('_', ' ')
    if g.get('state') == 'logged_out_waiting' or g.get('logged_out_since'):
        return 'out', (reason or 'logged out').replace('_', ' ')
    return 'in', 'login guard says logged in'


def guard_says_logged_out(g, now):
    state, reason = guard_state(g, now)
    return state == 'out', reason


def parse_span(text):
    """Seconds from a systemd time span ("10min", "1min 30s", "500ms"); None for infinity/0/unknown."""
    text = (text or '').strip()
    if not text or text in ('infinity', '0'):
        return None
    total = 0.0
    for num, unit in re.findall(r'([0-9.]+)\s*([a-z]*)', text):
        total += float(num) * _UNITS.get(unit or 's', 1)
    return total or None


def health_synced(reply):
    """(synced, why) from a daemon /health reply, as the panel's login_connection reads it."""
    hh = reply.get('health') if isinstance(reply, dict) else None
    if not isinstance(hh, dict):
        return False, 'no health block'
    page = hh.get('page') if isinstance(hh.get('page'), dict) else {}
    problems = hh.get('problems') if isinstance(hh.get('problems'), list) else []
    if page.get('state') == 'authorized' and page.get('synced') is True \
            and 'not_synced_for_current_connection' not in problems:
        return True, 'synced'
    return False, 'page ' + str(page.get('state')) + (', not synced' if page.get('synced') is not True else '')


def loopback_get(url, timeout=3.0):
    """(status, parsed JSON or None, error). Loopback only, no proxy."""
    host = urllib.parse.urlsplit(url).hostname
    if host not in LOOPBACK:
        return None, None, 'not_loopback'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, method='GET', headers={'Accept': 'application/json'}),
                         timeout=timeout) as r:
            body = r.read(400_000)
            try:
                return r.status, json.loads(body), None
            except ValueError:
                return r.status, None, None
    except urllib.error.HTTPError as exc:
        return exc.code, None, f'HTTP {exc.code}'
    except Exception as exc:  # noqa: BLE001 - no answer is a reading, not a crash
        return None, None, type(exc).__name__


def run_cmd(argv, timeout):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, '', f'timed out after {timeout:.0f} s'
    except OSError as exc:
        return 127, '', f'{type(exc).__name__}: {exc}'


class Failed(Exception):
    pass


class SystemdRestarter:
    name = 'systemd'

    def __init__(self, profile, run=None, http_get=None, clock=time.time, sleep=time.sleep, environ=None):
        cfg = profile.data.get('systemd') or {}
        self.p = profile
        sc = cfg.get('systemctl') or [LIVE_SYSTEMCTL]
        self.systemctl = [sc] if isinstance(sc, str) else list(sc)
        self.daemon_wait_s = float(cfg.get('daemon_wait_s', 60))
        self.firing_wait_s = float(cfg.get('firing_wait_s', 30))
        self.firing_fresh_s = float(cfg.get('firing_fresh_s', 5))
        self.status_wait_s = float(cfg.get('status_wait_s', 30))
        self.poll_s = float(cfg.get('poll_s', 1.0))
        self.cmd_timeout_s = float(cfg.get('command_timeout_s', COMMAND_TIMEOUT_S))
        self.status_api_url = cfg.get('status_api_url', 'http://127.0.0.1:8779/api/status')
        self.overrides = cfg.get('logins_override') or {}
        self.run = run or run_cmd
        self.http_get = http_get or loopback_get
        self.clock, self.sleep = clock, sleep
        self.env = os.environ if environ is None else environ
        self.log_path = profile.st('restarts.jsonl')

    # ----- systemctl -----
    def sc(self, *args, timeout=None):
        return self.run(self.systemctl + ['--user', *args], timeout or self.cmd_timeout_s)

    def show(self, unit, props=PROPS):
        code, out, err = self.sc('show', unit, *sum((['-p', p] for p in props), []), timeout=15)
        raw = dict(line.split('=', 1) for line in out.splitlines() if '=' in line) if code == 0 else {}
        num = lambda k: int(raw[k]) if (raw.get(k) or '').isdigit() else 0  # noqa: E731
        return {'active': raw.get('ActiveState', 'unknown'), 'sub': raw.get('SubState', ''), 'pid': num('MainPID'),
                'nrestarts': num('NRestarts'), 'result': raw.get('Result', ''), 'raw': raw,
                'error': None if code == 0 else (err.strip() or f'exit {code}')}

    def unit_state(self, unit):
        sh = self.show(unit)
        return sh['active'] if not sh['error'] else 'unknown'

    # ----- config the checks read (current files, so a rollback run checks the restored ones) -----
    def logins(self):
        with open(self.p.tb('logins.json')) as f:
            items = json.load(f)['logins']
        out = {}
        for e in items:
            e = dict(e, **(self.overrides.get(e['id']) or {}))
            e['guard_state_dir'] = os.path.expanduser(e.get('guard_state_dir') or '')
            out[e['id']] = e
        if self.p.mode == 'sandbox' and set(out) - set(self.overrides):
            raise RuntimeError('sandbox restarter needs systemd.logins_override for every login')
        return out

    def login_for(self, unit):
        for e in self.logins().values():
            if e.get('daemon_unit') == unit:
                return e
        return None

    def firing_files(self):
        with open(self.p.tb('firing.json')) as f:
            cfg = json.load(f)
        d = cfg.get('firing_dir') or self.p.tb('firing')
        return cfg, os.path.join(d, 'status.json')

    @staticmethod
    def classify(units):
        """(daemons, firing?, status?, refused) for a plan's unit names, daemons in plan order."""
        daemons, refused = [], []
        for u in units:
            if DAEMON_RE.match(u):
                if u not in daemons:
                    daemons.append(u)
            elif u not in (FIRING, STATUS):
                refused.append(u)
        return daemons, FIRING in units or bool(daemons), STATUS in units, refused

    def touched(self, units):
        daemons, firing, status, _ = self.classify([u['unit'] for u in units])
        return daemons + ([FIRING] if firing else []) + ([STATUS] if status else [])

    # ----- readings -----
    def read_token(self, entry):
        name = entry.get('read_token_env')
        tok = self.env.get(name, '') if name else ''
        if len(tok) < 24:   # a login added after this service started: same file its drop-in loads
            tok = token_from_files(self.p, name) or ''
        return tok if len(tok) >= 24 else None

    def guard_reading(self, entry):
        g = None
        if entry and entry.get('guard_state_dir'):
            try:
                with open(os.path.join(entry['guard_state_dir'], 'status.json')) as f:
                    g = json.load(f)
            except (OSError, ValueError):
                g = None
        return guard_state(g, self.clock())

    def daemon_reading(self, unit):
        """(verdict, detail, show) with verdict synced | logged_out | not_ready | down | bad_token.
        The login guard is read first, so every verdict (down included) says what it reports."""
        entry = self.login_for(unit)
        gstate, greason = self.guard_reading(entry)
        sh = self.show(unit)
        if sh['active'] != 'active':
            return 'down', (f'unit is {sh["active"]}/{sh["sub"]}' + (f' ({sh["error"]})' if sh['error'] else '')
                            + f'; {greason}'), sh
        why = 'no health reading'
        tok = self.read_token(entry) if entry else None
        if entry and tok:
            st, reply, err = self.http_get(entry['bridge_url'].rstrip('/') + '/health/' + tok)
            if st == 200 and reply is not None:
                ok, why = health_synced(reply)
                if ok:
                    return 'synced', 'synced with Tradovate', sh
            elif st in (401, 403, 404):
                return 'bad_token', (f'the daemon rejected the read token {entry.get("read_token_env")} '
                                     f'(HTTP {st}); check logins/peers.env and the read-tokens drop-in'), sh
            else:
                why = 'no /health answer (' + (err or f'HTTP {st}') + ')'
        elif entry:
            why = f'{entry.get("read_token_env") or "read_token_env"} is not set for this service'
        if gstate == 'out':
            return 'logged_out', f'Tradovate logged out ({greason})', sh
        return 'not_ready', f'{why}; {greason}', sh

    def firing_status(self):
        cfg, path = self.firing_files()
        with open(path) as f:
            return cfg, json.load(f)

    def refused_lanes(self):
        """Enabled lanes the running firing service refused at its start ({key: reason})."""
        try:
            _, st = self.firing_status()
        except (OSError, ValueError, KeyError):
            return {}
        return {k: lane.get('unavailable') for k, lane in (st.get('lanes') or {}).items()
                if isinstance(lane, dict) and lane.get('enabled') and lane.get('unavailable')}

    def firing_reading(self, since_ms=None, fresh_s=None):
        """(ok, detail). ok: active, status.json rewritten by the running process recently, with the
        firing order from firing.json (a config the service rejects never gets this far)."""
        sh = self.show(FIRING)
        if sh['active'] != 'active':
            return False, f'unit is {sh["active"]}/{sh["sub"]}'
        try:
            cfg, st = self.firing_status()
        except (OSError, ValueError, KeyError) as exc:
            return False, f'no firing status ({type(exc).__name__})'
        upd = st.get('updated_at') if isinstance(st.get('updated_at'), (int, float)) else None
        if upd is None:
            return False, 'firing status has no updated_at'
        age = self.clock() - upd / 1000.0
        if since_ms is not None and upd < since_ms:
            return False, 'firing status not rewritten since the start'
        if age > (fresh_s if fresh_s is not None else self.firing_fresh_s):
            return False, f'firing status is {age:.0f} s old'
        if sh['pid'] and st.get('pid') != sh['pid']:
            return False, 'firing status was written by another process'
        if st.get('firing_order') != cfg.get('firing_order'):
            return False, 'firing status shows a different firing order than firing.json'
        off = sorted(self.refused_lanes())
        return True, 'running, status fresh' + (f'; refused at start: {", ".join(off)}' if off else '')

    def status_reading(self):
        sh = self.show(STATUS)
        if sh['active'] != 'active':
            return False, f'unit is {sh["active"]}/{sh["sub"]}'
        st, _, err = self.http_get(self.status_api_url)
        return (st == 200), ('GET /api/status 200' if st == 200 else f'GET /api/status: {err or st}')

    # ----- systemd start limits -----
    def recent_starts(self, unit, since):
        try:
            with open(self.log_path) as f:
                lines = [json.loads(x) for x in f if x.strip()]
        except (OSError, ValueError):
            return []
        out = []
        for ln in lines:
            for r in ln.get('results') or [ln]:   # older lines held a list; new ones one start each
                if r.get('unit') == unit and r.get('started') and (r.get('at') or 0) >= since:
                    out.append(r['at'])
        return sorted(out)

    def start_budget(self, units):
        """Units that do not have STARTS_NEEDED starts left inside systemd's start limit:
        [{unit, burst, interval_s, used, free_in_s}]. Counts this page's own starts (restarts.jsonl)."""
        short = []
        now = self.clock()
        for u in self.touched(units):
            sh = self.show(u, props=('StartLimitBurst', 'StartLimitIntervalUSec'))
            burst = int(sh['raw']['StartLimitBurst']) if (sh['raw'].get('StartLimitBurst') or '').isdigit() else None
            interval = parse_span(sh['raw'].get('StartLimitIntervalUSec'))
            if not burst or not interval:
                continue
            starts = self.recent_starts(u, now - interval)
            if burst - len(starts) < STARTS_NEEDED:
                k = len(starts) - (burst - STARTS_NEEDED)        # starts that must age out first
                free_in = max(0.0, starts[k - 1] + interval - now) if 0 < k <= len(starts) else interval
                short.append({'unit': u, 'burst': burst, 'interval_s': interval, 'used': len(starts),
                              'free_in_s': round(free_in + 1, 1)})
        return short

    # ----- engine interface -----
    def signin_wait(self, units):
        """Daemons in the plan that are not synced now: [{unit, login, label, verdict, detail}].
        verdict down gets its own reason in the engine; a rejected or missing read token is not listed
        here (preflight refuses that change with a clear message instead of waiting)."""
        daemons = self.classify([u['unit'] for u in units])[0]
        if not daemons:
            return []
        out = []
        logins = self.logins()
        for d in daemons:
            entry = next((e for e in logins.values() if e.get('daemon_unit') == d), None)
            if not entry or not self.read_token(entry):
                continue
            verdict, detail, _ = self.daemon_reading(d)
            if verdict not in ('synced', 'bad_token'):
                out.append({'unit': d, 'login': entry['id'], 'label': entry.get('label') or entry['id'],
                            'verdict': verdict, 'detail': detail})
        return out

    def preflight(self, units):
        """([problems], detail). Runs before anything is written: every unit is allowed, readable and
        healthy now, so a failure afterwards is the change's, not a pre-existing fault."""
        names = [u['unit'] for u in units]
        daemons, firing, status, refused = self.classify(names)
        problems = []
        for u in refused:
            problems.append('Soup is never restarted by this page.'
                            if self.p.is_soup_unit(u)
                            else f'{u} is not a unit this page restarts.')
        if problems:
            return problems, None
        try:
            logins = self.logins()
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            return [f'Cannot read logins.json ({exc}).'], None
        for d in daemons:
            entry = next((e for e in logins.values() if e.get('daemon_unit') == d), None)
            if not entry:
                problems.append(f'{d} is not the daemon of any login in logins.json.')
                continue
            if urllib.parse.urlsplit(entry.get('bridge_url', '')).hostname not in LOOPBACK:
                problems.append(f'{d}: bridge_url is not loopback.')
                continue
            if not self.read_token(entry):
                problems.append(f'{d}: the read-only token {entry.get("read_token_env")} is not available to '
                                f'this service, so its health cannot be checked.')
                continue
            verdict, detail, _ = self.daemon_reading(d)
            if verdict != 'synced':
                problems.append(f'{d} is not synced with Tradovate now ({detail}).')
        if firing:
            if self.unit_state(PRIMARY_DAEMON) != 'active':
                problems.append(f'{PRIMARY_DAEMON} is not active; the firing service wants it, so firing is not '
                                f'restarted without it.')
            ok, detail = self.firing_reading(fresh_s=FIRING_PANEL_STALE_S)
            if not ok:
                problems.append(f'{FIRING} is not healthy now ({detail}).')
        if status:
            ok, detail = self.status_reading()
            if not ok:
                problems.append(f'{STATUS} is not healthy now ({detail}).')
        for b in ([] if problems else self.start_budget(units)):
            problems.append(f'{b["unit"]} is near its systemd start limit ({b["used"]} of {b["burst"]} starts '
                            f'in {b["interval_s"]:.0f} s).')
        order = ([f'stop {FIRING}'] if firing else []) + [f'restart {d}' for d in daemons] + \
            ([f'start {FIRING}'] if firing else []) + ([f'restart {STATUS}'] if status else [])
        return problems, 'services healthy now; sequence: ' + ', '.join(order)

    def restart(self, units, change_id, progress=None, phase='apply', attempted=None, keep_daemons=None):
        """Run the sequence. phase "rollback": only daemons in `attempted` are restarted, and only when the
        login guard has a fresh reading that does not say logged out. `keep_daemons` (a reason) leaves every
        daemon alone on purpose (the restore found someone not flat)."""
        names = [u['unit'] for u in units]
        daemons, firing, status, refused = self.classify(names)
        attempted = set(attempted or ())
        results = []
        state = {'firing_stopped': False}

        def rec(unit, action, ok, detail, started=False):
            r = {'unit': unit, 'action': action, 'ok': ok, 'detail': detail, 'at': self.clock()}
            if started:
                r['started'] = True
            results.append(r)
            if started:
                self._log(dict(r, change_id=change_id, phase=phase))
            if progress:
                progress(r)
            if ok is False:
                raise Failed(detail)

        try:
            if refused:
                for u in refused:
                    rec(u, 'refused', False, 'never restarted by this page')
            if firing:
                code, _, err = self.sc('stop', FIRING)
                sh = self.show(FIRING)
                rec(FIRING, 'stop', code == 0 and sh['active'] in ('inactive', 'failed'),
                    'stopped' if code == 0 else f'stop failed: {err.strip() or code}')
                state['firing_stopped'] = True
            for d in daemons:
                if phase == 'apply':
                    verdict, detail, _ = self.daemon_reading(d)      # fresh, right before the restart
                    if verdict != 'synced':
                        rec(d, 'skipped', False, f'not restarted: {detail}')
                        continue
                elif keep_daemons:
                    rec(d, 'skipped', None, f'not restarted: {keep_daemons}')
                    continue
                elif d not in attempted:
                    rec(d, 'skipped', None, 'not restarted: this change never restarted it, so it still runs the '
                                            'old files')
                    continue
                else:
                    gstate, greason = self.guard_reading(self.login_for(d))
                    if gstate != 'in':
                        rec(d, 'skipped', False, f'not restarted: {greason}; it loads the restored files at its '
                                                 f'next start, after sign-in')
                        continue
                self._restart_daemon(d, rec)
            if firing:
                started_ms = int(self.clock() * 1000) - 1   # the service writes whole ms
                # ignore-requirements: firing's Wants=tvbridge-daemon.service must not start daemon A.
                code, _, err = self.sc('start', '--job-mode=ignore-requirements', FIRING)
                if code != 0:
                    rec(FIRING, 'start', False, f'start failed: {err.strip() or code}', started=True)
                rec(FIRING, 'start', *self._wait(lambda: self.firing_reading(since_ms=started_ms),
                                                 self.firing_wait_s), started=True)
                state['firing_stopped'] = False
            if status:
                code, _, err = self.sc('restart', STATUS)
                if code != 0:
                    rec(STATUS, 'restart', False, f'restart failed: {err.strip() or code}', started=True)
                rec(STATUS, 'restart', *self._wait(self.status_reading, self.status_wait_s), started=True)
        except Failed:
            done = {(r['unit'], r['action']) for r in results}
            for d in daemons:
                if (d, 'restart') not in done and (d, 'skipped') not in done:
                    results.append({'unit': d, 'action': 'restart', 'ok': None, 'detail': 'not reached',
                                    'at': self.clock()})
            if status and (STATUS, 'restart') not in done:
                results.append({'unit': STATUS, 'action': 'restart', 'ok': None, 'detail': 'not reached',
                                'at': self.clock()})
            if state['firing_stopped'] and (FIRING, 'start') not in done:
                # Left stopped on purpose: no follower fires into a daemon that did not come back.
                results.append({'unit': FIRING, 'action': 'left stopped', 'ok': False,
                                'detail': 'stopped; followers will not fire until this is fixed', 'at': self.clock()})
        return results

    def _restart_daemon(self, unit, rec):
        before = self.show(unit)
        code, _, err = self.sc('restart', unit)
        if code != 0:
            rec(unit, 'restart', False, f'restart failed: {err.strip() or code}', started=True)
        after = self.show(unit)
        if after['pid'] and after['pid'] == before['pid']:
            rec(unit, 'restart', False, 'the process did not change, so it did not restart', started=True)
        pid, nr = after['pid'], after['nrestarts']
        deadline = self.clock() + self.daemon_wait_s
        while True:
            verdict, detail, sh = self.daemon_reading(unit)
            if sh['active'] == 'failed':
                rec(unit, 'restart', False, f'unit failed after the restart ({sh["result"] or detail})', started=True)
            if sh['nrestarts'] > nr or (sh['pid'] and pid and sh['pid'] != pid):
                rec(unit, 'restart', False, 'the daemon exited after the restart and systemd started it again '
                                            '(config rejected?)', started=True)
            if verdict == 'synced':
                return rec(unit, 'restart', True, detail, started=True)
            if verdict == 'logged_out':
                rec(unit, 'restart', False, f'{detail} during the restart; it finishes starting only after sign-in',
                    started=True)
            if self.clock() >= deadline:
                rec(unit, 'restart', False, f'not synced within {self.daemon_wait_s:.0f} s ({detail})', started=True)
            self.sleep(self.poll_s)

    def _wait(self, check, seconds):
        deadline = self.clock() + seconds
        while True:
            ok, detail = check()
            if ok:
                return True, detail
            if self.clock() >= deadline:
                return False, f'not ready within {seconds:.0f} s ({detail})'
            self.sleep(self.poll_s)

    def health(self, units):
        """Quick re-check after the sequence (the engine's verify step). No waiting."""
        daemons, firing, status, refused = self.classify([u['unit'] for u in units])
        out = {u: False for u in refused}
        for d in daemons:
            out[d] = self.daemon_reading(d)[0] == 'synced'
        if firing:
            out[FIRING] = self.firing_reading(fresh_s=max(self.firing_fresh_s, 5))[0]
        if status:
            out[STATUS] = self.status_reading()[0]
        return out

    def _log(self, entry):
        os.makedirs(os.path.dirname(self.log_path), mode=0o700, exist_ok=True)
        with open(self.log_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')
