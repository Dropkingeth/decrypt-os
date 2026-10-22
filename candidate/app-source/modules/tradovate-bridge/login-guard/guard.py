#!/usr/bin/env python3
"""Tradovate bridge login guard. Version 1.3.

One pass per invocation (timer-driven). It finds exactly one Tradovate page
target on the bridge browser's loopback DevTools port (9555) and:

* When the page shows Tradovate's username/password form and Chromium's saved
  login has filled both fields, it sends one trusted mouse click to the single
  visible "Login" button, within persisted rate limits.
* When the page is logged in and tvbridge-daemon.service is failed or inactive
  (for example after hitting its start limit while the page was logged out),
  it resets and starts that one unit, within persisted rate limits.
* When the page is logged in and the daemon is active and answers its loopback
  /health, but reports the page not authorized or not synced on every pass for
  more than 60 s (for example after losing its page controller lease), it
  restarts that one unit, within the same persisted daemon-start limits.

It never types credentials, reads field values (only whether a field is
empty), reads cookies/storage, navigates, reloads, or touches ports 9556,
9566 or 9567 (Decrypt OS's other browsers). It never sends orders and never
removes the bridge kill switch. DevTools methods are limited to Runtime.evaluate with fixed
expressions, Input.dispatchMouseEvent and two fixed navigation key presses
(ArrowDown, Enter) used only to accept Chromium's own autofill suggestion.

Multi-login (1.2): --login <id> or TVBRIDGE_LOGIN_ID selects one login from the
registry (logins.json). Its DevTools port, browser/daemon units, daemon /health
port and state directory come from that entry, and every other login's DevTools
port is reserved, so one guard never touches another login's browser. Without
an instance the constants below apply unchanged (login A).

Weekend hold (1.3): Tradovate runs maintenance after the Friday close and logs
the bridge out. During the hold (default Friday 18:00 until the Sunday 18:00 open,
America/New_York wall time; set "weekend_hold" in $TVBRIDGE_ROOT/login-guard.json)
a logged-out page is left logged out: no Login click and no trading-mode click,
and the 15-minute logged-out escalation is not raised. Logged-in behaviour (daemon
start/restart) is unchanged. After the hold ends the escalation clock counts from
the hold's end, not from the logout.
"""
import argparse
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit
from collections import namedtuple
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from zoneinfo import ZoneInfo

try:
    import logins as registry  # login-guard/logins.py, installed beside this file
except ImportError:  # login A still runs on its constants; --login refuses without it
    registry = None

VERSION = '1.3'
SCHEMA = 'decrypt.tvbridge-login-guard.v1'
PAGE_HOST = 'trader.tradovate.com'
LOGIN_PATH = '/welcome'
MODE_PATH = '/trading-mode'
# Both labels Tradovate has used for the Simulation button (the newer one is "Start Simulated Trading").
# Never Market Replay; prop-firm accounts are simulation (demo endpoint).
MODE_BUTTONS = ('Access Simulation', 'Start Simulated Trading')
DEFAULT_PORT = 9555
RESERVED_PORTS = frozenset({9556, 9566, 9567})  # login guard, Guardian and market browsers
BROWSER_UNIT = 'tvbridge-browser.service'
DAEMON_UNIT = 'tvbridge-daemon.service'
DEFAULT_STATE = Path.home() / '.local/share/decrypt-os/tvbridge-state/login-guard'
DAEMON_HEALTH_PORT = 8777  # tvbridge-daemon loopback /health/<TVBRIDGE_TOKEN>
UNHEALTHY_RESTART_SECONDS = 60  # logged in + active daemon + page unhealthy for longer: restart
UNHEALTHY_MAX_GAP = 120  # passes further apart than this do not prove "continuously"
NOT_SYNCED = 'not_synced_for_current_connection'
WEEKEND_HOLD = 'weekend_maintenance_hold'

# Floors/caps: command-line options may only make these stricter.
MIN_LOGIN_INTERVAL = 300
MAX_LOGINS_PER_HOUR = 3
MIN_START_INTERVAL = 300
MAX_STARTS_PER_HOUR = 3
MIN_MODE_INTERVAL = 120
MAX_MODES_PER_HOUR = 4
HOUR = 3600
LOGIN_VERIFY_SECONDS = 25
LOGGED_OUT_ESCALATE_SECONDS = 900
HTTP_TIMEOUT = 3
WS_TIMEOUT = 5
EVAL_DEADLINE = 6.0
MAX_TARGET_BYTES = 1_000_000
EVENT_LOG_MAX_BYTES = 512 * 1024

STATES = ('ok', 'logged_in_now', 'daemon_started', 'logged_out_waiting', 'needs_human',
          'login_repeated', 'page_missing', 'error')
TARGET_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')

# Fixed page scan. Returns only host/path, booleans, counts and one button
# centre. Field values never leave the page: only "is it empty".
_SCAN = r'''
const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
const shown = (el) => {
  if (!el || !el.isConnected || el.disabled) return false;
  const rects = el.getClientRects ? Array.from(el.getClientRects()) : [];
  if (!rects.some((r) => r.width > 0 && r.height > 0)) return false;
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse'
        || Number(s.opacity) === 0 || n.hidden || n.inert
        || n.getAttribute('aria-hidden') === 'true') return false;
  }
  return true;
};
const centre = (el) => {
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const top = document.elementFromPoint(x, y);
  return {x: Math.round(x), y: Math.round(y), hit: !!top && (top === el || el.contains(top))};
};
const user = document.getElementById('name-input');
const pass = document.getElementById('password-input');
const passwords = Array.from(document.querySelectorAll('input[type="password"]')).filter(shown).length;
let login = Array.from(document.querySelectorAll('button,[role="button"]'))
  .filter((el) => norm(el.innerText || el.textContent) === 'Login' && shown(el));
login = login.filter((el) => !login.some((o) => o !== el && el.contains(o)));
let sim = Array.from(document.querySelectorAll('button,[role="button"]'))
  .filter((el) => %(modes)s.includes(norm(el.innerText || el.textContent)) && shown(el));
sim = sim.filter((el) => !sim.some((o) => o !== el && el.contains(o)));
const text = ((document.body && document.body.innerText) || '').toLowerCase();
const obs = {
  host: location.hostname, path: location.pathname,
  password_inputs: passwords,
  user_visible: shown(user), pass_visible: shown(pass),
  user_filled: !!(user && user.value), pass_filled: !!(pass && pass.value),
  login_buttons: login.length,
  login_point: login.length === 1 ? centre(login[0]) : null,
  mode_buttons: sim.length,
  mode_point: sim.length === 1 ? centre(sim[0]) : null,
  user_point: shown(user) ? centre(user) : null,
  second_factor: /verification code|two-factor|2fa|security code|one-time|authenticator/.test(text),
  captcha: !!document.querySelector('iframe[src*="captcha"],iframe[title*="captcha" i],.g-recaptcha,[data-hcaptcha-widget-id]'),
  alert_text: !!document.querySelector('[role="alert"],.MuiAlert-root,.error-message'),
};
''' % {'modes': json.dumps(list(MODE_BUTTONS))}
DETECT_JS = '(() => {' + _SCAN + 'return obs; })()'
# Fixed scroll-into-view expressions (the chooser's Simulation button can sit
# below the window). They only scroll the one element the scan identified.
SCROLL_JS = {
    'user': '(() => {' + _SCAN + 'if (obs.user_visible) user.scrollIntoView({block: "center"}); return obs.user_visible; })()',
    'login': '(() => {' + _SCAN + 'if (login.length === 1) login[0].scrollIntoView({block: "center"}); return login.length === 1; })()',
    'mode': '(() => {' + _SCAN + 'if (sim.length === 1) sim[0].scrollIntoView({block: "center"}); return sim.length === 1; })()',
}
EXPRESSIONS = frozenset({DETECT_JS, *SCROLL_JS.values()})
ALLOWED_METHODS = frozenset({'Runtime.evaluate', 'Input.dispatchMouseEvent', 'Input.dispatchKeyEvent'})
ALLOWED_KEYS = {'ArrowDown': 40, 'Enter': 13}


class Hold(Exception):
    """A refusal with a fixed reason code. Never carries page or exception text."""

    def __init__(self, reason, state='error'):
        super().__init__(reason)
        self.reason = reason
        self.state = state


def iso(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ---------------------------------------------------------------- transport

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Hold('devtools_redirect_blocked')


def fetch_targets(port):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open('http://127.0.0.1:%d/json/list' % port, timeout=HTTP_TIMEOUT) as r:
        raw = r.read(MAX_TARGET_BYTES + 1)
    if len(raw) > MAX_TARGET_BYTES:
        raise Hold('devtools_target_list_too_large')
    return json.loads(raw)


def health_reader(token, port=DAEMON_HEALTH_PORT):
    """Callable returning the daemon's /health JSON, or None when it does not answer.

    None (no reader) when the token is missing. The token never leaves this closure
    and is never logged; errors are swallowed without their text.
    """
    if not isinstance(token, str) or len(token) < 24:
        return None

    def read():
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open('http://127.0.0.1:%d/health/%s' % (port, token), timeout=HTTP_TIMEOUT) as r:
                raw = r.read(200_001)
            return None if len(raw) > 200_000 else json.loads(raw)
        except Exception:
            return None
    return read


def health_verdict(response):
    """'healthy', 'not_authorized', 'not_synced' or 'unreachable' (no or malformed answer)."""
    inner = response.get('health') if isinstance(response, dict) else None
    page = inner.get('page') if isinstance(inner, dict) else None
    if not isinstance(page, dict):
        return 'unreachable'
    if page.get('state') != 'authorized':
        return 'not_authorized'
    problems = inner.get('problems')
    if isinstance(problems, list) and NOT_SYNCED in problems:
        return 'not_synced'
    return 'healthy'


def ws_connect(url, port):
    import websocket  # the websocket-client package (a runtime dependency)
    sock = socket.create_connection(('127.0.0.1', port), timeout=WS_TIMEOUT)
    ws = websocket.create_connection(url, socket=sock, suppress_origin=True,
                                     timeout=WS_TIMEOUT, redirect_limit=0)
    ws.settimeout(WS_TIMEOUT)
    return ws


class CdpSession:
    """Minimal CDP client with a fixed method and expression allow-list."""

    def __init__(self, ws, clock=time.monotonic):
        self.ws, self.clock, self.sequence = ws, clock, 0

    def call(self, method, params):
        if method not in ALLOWED_METHODS:
            raise Hold('cdp_method_forbidden')
        self.sequence += 1
        self.ws.send(json.dumps({'id': self.sequence, 'method': method, 'params': params}))
        deadline = self.clock() + EVAL_DEADLINE
        for _ in range(500):
            if self.clock() >= deadline:
                break
            raw = self.ws.recv()
            if not isinstance(raw, (str, bytes)) or len(raw) > 200_000:
                raise Hold('cdp_response_invalid')
            message = json.loads(raw)
            if isinstance(message, dict) and message.get('id') == self.sequence:
                if 'error' in message:
                    raise Hold('cdp_call_failed')
                return message.get('result') or {}
        raise Hold('cdp_timeout')

    def detect(self):
        result = self.call('Runtime.evaluate', {'expression': DETECT_JS, 'returnByValue': True,
                                                'awaitPromise': False})
        if 'exceptionDetails' in result:
            raise Hold('page_script_failed')
        value = (result.get('result') or {}).get('value')
        if not isinstance(value, dict):
            raise Hold('page_result_invalid')
        return value

    def scroll(self, which):
        expression = SCROLL_JS[which]
        if expression not in EXPRESSIONS:
            raise Hold('cdp_expression_forbidden')
        result = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                'awaitPromise': False})
        if 'exceptionDetails' in result:
            raise Hold('page_script_failed')

    def click(self, point):
        x, y = point['x'], point['y']
        if type(x) is not int or type(y) is not int or not (0 <= x < 10000 and 0 <= y < 10000):
            raise Hold('click_point_invalid')
        self.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
        for kind in ('mousePressed', 'mouseReleased'):
            self.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y,
                                                   'button': 'left', 'clickCount': 1})

    def key(self, name):
        code = ALLOWED_KEYS.get(name)
        if code is None:
            raise Hold('key_forbidden')
        for kind in ('rawKeyDown', 'keyUp'):
            self.call('Input.dispatchKeyEvent', {'type': kind, 'key': name, 'code': name,
                                                 'windowsVirtualKeyCode': code,
                                                 'nativeVirtualKeyCode': code})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------- ownership

def listener_pids(port):
    """PIDs holding the loopback LISTEN socket on port (read-only /proc)."""
    inodes = set()
    want = '0100007F:%04X' % port
    try:
        with open('/proc/net/tcp') as f:
            next(f)
            for line in f:
                parts = line.split()
                if parts[1] == want and parts[3] == '0A':
                    inodes.add('socket:[%s]' % parts[9])
    except OSError:
        raise Hold('owner_check_unavailable')
    if not inodes:
        raise Hold('devtools_listener_missing', 'page_missing')
    pids = set()
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            fds = os.listdir('/proc/%s/fd' % pid)
            if any(os.readlink('/proc/%s/fd/%s' % (pid, fd)) in inodes for fd in fds):
                pids.add(int(pid))
        except OSError:
            continue
    return pids


def browser_main_pid(unit=BROWSER_UNIT):
    out = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'MainPID', '--value'],
                         capture_output=True, text=True, timeout=10)
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 0


def check_owner(port, unit=BROWSER_UNIT):
    """The DevTools listener must be exactly the main process of the login's browser unit."""
    main_pid = browser_main_pid(unit)
    if main_pid <= 0 or listener_pids(port) != {main_pid}:
        raise Hold('browser_owner_unverified')


# ---------------------------------------------------------------- page choice

def is_tradovate_url(url):
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return False
    return (p.scheme == 'https' and p.hostname == PAGE_HOST and p.netloc == PAGE_HOST
            and port is None and not p.username and not p.password)


def select_page(targets, port):
    if not isinstance(targets, list) or len(targets) > 1000:
        raise Hold('devtools_target_list_invalid')
    matches = [t for t in targets if isinstance(t, dict) and t.get('type') == 'page'
               and is_tradovate_url(t.get('url'))]
    if not matches:
        raise Hold('tradovate_page_missing', 'page_missing')
    if len(matches) > 1:
        raise Hold('tradovate_page_ambiguous')
    ident = matches[0].get('id')
    if not isinstance(ident, str) or not TARGET_ID.fullmatch(ident):
        raise Hold('page_target_id_invalid')
    if matches[0].get('webSocketDebuggerUrl') != 'ws://127.0.0.1:%d/devtools/page/%s' % (port, ident):
        raise Hold('page_debugger_not_loopback')
    return matches[0]['webSocketDebuggerUrl']


def check_obs(obs):
    if obs.get('host') != PAGE_HOST or not isinstance(obs.get('path'), str):
        raise Hold('page_identity_mismatch')
    bools = ('user_visible', 'pass_visible', 'user_filled', 'pass_filled', 'second_factor',
             'captcha', 'alert_text')
    if not all(type(obs.get(k)) is bool for k in bools) or type(obs.get('login_buttons')) is not int \
            or type(obs.get('password_inputs')) is not int or type(obs.get('mode_buttons')) is not int:
        raise Hold('page_result_invalid')
    for key in ('login_point', 'user_point', 'mode_point'):
        pt = obs.get(key)
        if pt is not None and not (isinstance(pt, dict) and type(pt.get('x')) is int
                                   and type(pt.get('y')) is int and type(pt.get('hit')) is bool):
            raise Hold('page_result_invalid')
    return obs


def logged_in(obs):
    return obs['path'] not in (LOGIN_PATH, MODE_PATH) and obs['password_inputs'] == 0


def login_form(obs):
    return (obs['path'] == LOGIN_PATH and obs['user_visible'] and obs['pass_visible']
            and obs['password_inputs'] == 1)


# ---------------------------------------------------------------- systemd

def unit_state(unit):
    out = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '-p', 'UnitFileState',
                          '--value'], capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise Hold('systemctl_show_failed')
    lines = out.stdout.split('\n')
    return (lines[0].strip() if lines else ''), (lines[1].strip() if len(lines) > 1 else '')


def start_unit(unit):
    subprocess.run(['systemctl', '--user', 'reset-failed', unit], capture_output=True, timeout=10)
    out = subprocess.run(['systemctl', '--user', 'start', '--no-block', unit], capture_output=True,
                         timeout=10)
    if out.returncode != 0:
        raise Hold('daemon_start_failed')


def restart_unit(unit):
    out = subprocess.run(['systemctl', '--user', 'restart', '--no-block', unit], capture_output=True,
                         timeout=10)
    if out.returncode != 0:
        raise Hold('daemon_restart_failed')


# ---------------------------------------------------------------- state files

def secure_dir(path):
    path = Path(path)
    # The guard timer can fire before the daemon has created tvbridge-state/ on a fresh setup;
    # create the parents too instead of failing until the daemon gets there first.
    try:
        os.makedirs(str(path), 0o700)
    except FileExistsError:
        pass
    info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise Hold('state_dir_unsafe')
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(str(path), 0o700)
    return path


def _open_private(path, flags):
    fd = os.open(str(path), flags | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise Hold('state_file_unsafe')
    if stat.S_IMODE(info.st_mode) != 0o600:
        os.fchmod(fd, 0o600)
    return fd


def write_json(path, value):
    temp = path.with_name('.' + path.name + '.tmp')
    try:
        os.unlink(str(temp))
    except FileNotFoundError:
        pass
    fd = _open_private(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, sort_keys=True, separators=(',', ':'))
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(temp), str(path))


def read_json(path, default):
    try:
        fd = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return default
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Hold('state_file_unsafe')
        raise
    with os.fdopen(fd) as f:
        raw = f.read(1_000_001)
    try:
        value = json.loads(raw)
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def append_event(state_dir, record):
    path = state_dir / 'events.jsonl'
    try:
        if os.lstat(str(path)).st_size > EVENT_LOG_MAX_BYTES:
            os.replace(str(path), str(state_dir / 'events.jsonl.1'))
    except FileNotFoundError:
        pass
    fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, 'a') as f:
        f.write(json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n')


# ---------------------------------------------------------------- weekend hold

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
DEFAULT_HOLD = {'enabled': True, 'timezone': 'America/New_York', 'start': 'Fri 18:00', 'end': 'Sun 18:00'}


class WeekendHold:
    """No login retries from `start` to `end` (weekday 0 = Monday, hour, minute) in the wall time of `tz`.

    Default: Friday 18:00 to Sunday 18:00 America/New_York, Tradovate's weekend maintenance. Wall time
    makes it DST-safe. A window whose end comes before its start in the week wraps past Sunday midnight.
    """

    def __init__(self, tz='America/New_York', start=(4, 18), end=(6, 18), enabled=True):
        start, end = tuple(start) + (0,) * (3 - len(start)), tuple(end) + (0,) * (3 - len(end))
        for day, hour, minute in (start, end):
            if not (0 <= day <= 6 and 0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError('hold time out of range')
        self.tz_name, self.tz, self.enabled = tz, ZoneInfo(tz), bool(enabled)
        self.start_at, self.end_at = start, end
        self.start = start[0] * 1440 + start[1] * 60 + start[2]
        self.end = end[0] * 1440 + end[1] * 60 + end[2]
        if self.start == self.end:
            raise ValueError('hold start equals its end')
        self.end_day, self.end_hour, self.end_minute = end

    def active(self, ts):
        if not self.enabled:
            return False
        t = datetime.fromtimestamp(ts, self.tz)
        minute = t.weekday() * 1440 + t.hour * 60 + t.minute
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end

    def last_end(self, ts):
        """Timestamp of the most recent hold end at or before ts (0 when the hold is off)."""
        if not self.enabled:
            return 0.0
        t = datetime.fromtimestamp(ts, self.tz)
        day = (t - timedelta(days=(t.weekday() - self.end_day) % 7)).date()
        end = datetime(day.year, day.month, day.day, self.end_hour, self.end_minute, tzinfo=self.tz)
        if end.timestamp() > ts:
            day -= timedelta(days=7)
            end = datetime(day.year, day.month, day.day, self.end_hour, self.end_minute, tzinfo=self.tz)
        return end.timestamp()

    def text(self):
        """'Fri 6:00 PM to Sun 6:00 PM (America/New_York)', or 'off'."""
        if not self.enabled:
            return 'off'

        def when(at):
            hour = at[1] % 12 or 12
            return '%s %d:%02d %s' % (DAYS[at[0]].title(), hour, at[2], 'AM' if at[1] < 12 else 'PM')
        return '%s to %s (%s)' % (when(self.start_at), when(self.end_at), self.tz_name)


def parse_when(text):
    """'Fri 18:00' -> (4, 18, 0). Day names may be full or three letters, any case."""
    m = re.fullmatch(r'\s*([A-Za-z]{3,9})\s+(\d{1,2}):(\d{2})\s*', str(text))
    if not m or m.group(1)[:3].lower() not in DAYS:
        raise ValueError('use a weekday and 24-hour time, for example "Fri 18:00"')
    return DAYS.index(m.group(1)[:3].lower()), int(m.group(2)), int(m.group(3))


def guard_config_path():
    root = os.environ.get('TVBRIDGE_ROOT') or '~/.local/share/decrypt-os/tvbridge'
    return Path(os.path.expanduser(os.environ.get('TVBRIDGE_GUARD_CONFIG') or root + '/login-guard.json'))


def load_weekend_hold(path=None):
    """(WeekendHold, error code or None) from login-guard.json's "weekend_hold".

    Missing file or key: the default hold. An unreadable or invalid file also gives the default hold,
    plus an error code, so a typo never weakens the hold. {"enabled": false} turns it off.
    """
    path = Path(path) if path is not None else guard_config_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError):
        return WeekendHold(), 'guard_config_unreadable'
    try:
        if not isinstance(raw, dict):
            raise ValueError('not an object')
        value = dict(DEFAULT_HOLD, **(raw.get('weekend_hold') or {}))
        if set(value) != set(DEFAULT_HOLD) or type(value['enabled']) is not bool:
            raise ValueError('unknown keys')
        return WeekendHold(value['timezone'], parse_when(value['start']), parse_when(value['end']),
                           value['enabled']), None
    except Exception:  # bad zone name, times or keys
        return WeekendHold(), 'guard_config_invalid'


# ---------------------------------------------------------------- one pass

class Guard:
    def __init__(self, state_dir=DEFAULT_STATE, port=DEFAULT_PORT, targets=None, connect=None,
                 owner_check=None, units=None, starter=None, clock=time.time, sleep=time.sleep,
                 verify_seconds=LOGIN_VERIFY_SECONDS, dry_run=False, manage_daemon=True,
                 health=None, restarter=None, reserved=None, daemon_unit=DAEMON_UNIT,
                 token_env='TVBRIDGE_TOKEN', health_port=DAEMON_HEALTH_PORT, login_id=None,
                 weekend=None, weekend_error=None):
        # 9556/9566/9567 always stay reserved; an instance adds the other logins' ports.
        self.reserved = RESERVED_PORTS | frozenset(reserved or ())
        if port in self.reserved:
            raise Hold('reserved_port')
        self.state_dir = Path(state_dir)
        self.port = port
        self.daemon_unit = daemon_unit
        self.login_id = login_id
        self.targets = targets or fetch_targets
        self.connect = connect or ws_connect
        self.owner_check = owner_check
        self.units = units or unit_state
        self.starter = starter or start_unit
        self.restarter = restarter or restart_unit
        # Same env var and file as trigger.py/the daemon (EnvironmentFile tvbridge.env).
        # An instance names its own variable; its tvbridge.env also sets TVBRIDGE_TOKEN.
        token = os.environ.get(token_env) or os.environ.get('TVBRIDGE_TOKEN', '')
        self.health = health if health is not None else health_reader(token, health_port)
        self.clock, self.sleep = clock, sleep
        self.verify_seconds = verify_seconds
        self.dry_run = dry_run
        self.manage_daemon = manage_daemon
        self.weekend = weekend or WeekendHold()
        self.weekend_error = weekend_error

    @staticmethod
    def recent(stamps, now):
        return [t for t in stamps if now - t < HOUR]

    def allowed(self, stamps, now, interval, per_hour):
        recent = self.recent(stamps, now)
        if len(recent) >= per_hour:
            return 'cap'
        if recent and now - max(recent) < interval:
            return 'interval'
        return None

    def run(self):
        secure_dir(self.state_dir)
        lock_fd = _open_private(self.state_dir / 'guard.lock', os.O_WRONLY | os.O_CREAT)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'state': 'busy'}
            return self._locked()
        finally:
            os.close(lock_fd)

    def _locked(self):
        memory = read_json(self.state_dir / 'guard-state.json', {})
        self.logins = [float(t) for t in memory.get('logins', []) if type(t) in (int, float)]
        self.starts = [float(t) for t in memory.get('starts', []) if type(t) in (int, float)]
        self.modes = [float(t) for t in memory.get('modes', []) if type(t) in (int, float)]
        self.logged_out_since = memory.get('logged_out_since')
        stamp = lambda v: float(v) if type(v) in (int, float) else None  # noqa: E731
        self.unhealthy_since = stamp(memory.get('daemon_unhealthy_since'))  # absent in v1.0 state: None
        self.unhealthy_seen = stamp(memory.get('daemon_unhealthy_seen'))
        self.unhealthy_observed = False  # set only by a logged-in + active + unhealthy reading
        self.daemon_health = 'not_checked'
        now = self.clock()
        session = None
        try:
            url = select_page(self.targets(self.port), self.port)
            if self.owner_check is not None:
                self.owner_check(self.port)
            session = CdpSession(self.connect(url, self.port))
            outcome = self._decide(session, now)
        except Hold as hold:
            outcome = {'state': hold.state, 'reason': hold.reason}
        except Exception as exc:  # page/exception text is never recorded
            outcome = {'state': 'error', 'reason': 'unexpected_' + type(exc).__name__}
        finally:
            if session is not None:
                session.close()
        return self._persist(outcome, now)

    def _save_memory(self):
        write_json(self.state_dir / 'guard-state.json',
                   {'schema': SCHEMA, 'logins': sorted(self.logins)[-50:],
                    'starts': sorted(self.starts)[-50:], 'modes': sorted(self.modes)[-50:],
                    'logged_out_since': self.logged_out_since,
                    'daemon_unhealthy_since': self.unhealthy_since,
                    'daemon_unhealthy_seen': self.unhealthy_seen})

    def _event(self, kind, now, state, reason):
        append_event(self.state_dir, {'at': iso(now), 'ts': round(now, 3), 'kind': kind,
                                      'state': state, 'reason': reason})

    def _decide(self, session, now):
        obs = check_obs(session.detect())
        if logged_in(obs):
            self.logged_out_since = None
            return self._daemon(now)
        if self.logged_out_since is None:
            self.logged_out_since = now
        if self.weekend.active(now):
            return {'state': 'logged_out_waiting', 'reason': WEEKEND_HOLD}
        if obs['path'] == MODE_PATH and obs['password_inputs'] == 0:
            return self._choose_mode(session, obs, now)
        if not login_form(obs):
            return {'state': 'logged_out_waiting', 'reason': 'login_form_not_recognised'}
        if obs['captcha'] or obs['second_factor']:
            return {'state': 'needs_human', 'reason': 'captcha' if obs['captcha'] else 'second_factor'}
        limit = self.allowed(self.logins, now, MIN_LOGIN_INTERVAL, MAX_LOGINS_PER_HOUR)
        if limit == 'cap':
            return {'state': 'login_repeated', 'reason': 'hourly_login_cap_reached'}
        if limit == 'interval':
            return {'state': 'logged_out_waiting', 'reason': 'login_interval_not_elapsed'}
        if obs['login_buttons'] != 1 or not obs['login_point'] or not obs['user_point']:
            return {'state': 'logged_out_waiting', 'reason': 'login_button_not_unique'}
        if self.dry_run:
            return {'state': 'logged_out_waiting', 'reason': 'dry_run_would_login'}
        # Count the attempt before any input: a crash mid-login still rate-limits.
        self.logins.append(now)
        self._save_memory()
        self._event('login_attempt', now, 'logged_out_waiting', 'login_form_confirmed')
        # A trusted click in the username field makes Chromium release its
        # autofilled values to the page. If nothing was filled, accept the
        # first saved-login suggestion with ArrowDown + Enter.
        if not obs['user_point']['hit']:
            session.scroll('user')
            self.sleep(0.5)
            obs = check_obs(session.detect())
            if not obs['user_point'] or not obs['user_point']['hit']:
                return {'state': 'logged_out_waiting', 'reason': 'username_field_covered'}
        session.click(obs['user_point'])
        self.sleep(1.0)
        obs = check_obs(session.detect())
        if not (obs['user_filled'] and obs['pass_filled']):
            session.key('ArrowDown')
            self.sleep(0.5)
            session.key('Enter')
            self.sleep(1.0)
            obs = check_obs(session.detect())
        if not (obs['user_filled'] and obs['pass_filled']):
            return {'state': 'needs_human', 'reason': 'saved_login_not_filled'}
        if obs['login_point'] and not obs['login_point']['hit']:
            session.scroll('login')
            self.sleep(0.5)
            obs = check_obs(session.detect())
        if not login_form(obs) or obs['login_buttons'] != 1 or not obs['login_point'] \
                or not obs['login_point']['hit']:
            return {'state': 'logged_out_waiting', 'reason': 'login_button_changed_or_covered'}
        session.click(obs['login_point'])
        waited = 0.0
        while waited < self.verify_seconds:
            self.sleep(2.0)
            waited += 2.0
            try:
                obs = check_obs(session.detect())
            except Hold:
                continue  # navigation in progress
            if obs['path'] == MODE_PATH and obs['password_inputs'] == 0:
                return self._choose_mode(session, obs, now, after_login=True)
            if logged_in(obs):
                self.logged_out_since = None
                return {'state': 'logged_in_now', 'reason': 'saved_login_accepted'}
            if obs['captcha'] or obs['second_factor']:
                return {'state': 'needs_human', 'reason': 'captcha' if obs['captcha'] else 'second_factor'}
        reason = 'login_rejected_alert_shown' if obs.get('alert_text') else 'login_not_confirmed'
        return {'state': 'logged_out_waiting', 'reason': reason}

    def _choose_mode(self, session, obs, now, after_login=False):
        """On Tradovate's trading-mode chooser click only the Simulation button (MODE_BUTTONS)."""
        limit = self.allowed(self.modes, now, MIN_MODE_INTERVAL, MAX_MODES_PER_HOUR)
        if limit == 'cap':
            return {'state': 'login_repeated', 'reason': 'hourly_mode_cap_reached'}
        if limit == 'interval':
            return {'state': 'logged_out_waiting', 'reason': 'mode_interval_not_elapsed'}
        for _ in range(5):  # the chooser can render a moment after navigation
            if obs['mode_buttons'] == 1 and obs['mode_point'] and obs['mode_point']['hit']:
                break
            if obs['mode_buttons'] == 1:
                session.scroll('mode')
            self.sleep(1.0)
            obs = check_obs(session.detect())
            if obs['path'] != MODE_PATH:
                break
        if obs['path'] != MODE_PATH:
            return {'state': 'logged_out_waiting', 'reason': 'mode_page_left'}
        if obs['mode_buttons'] != 1 or not obs['mode_point'] or not obs['mode_point']['hit']:
            return {'state': 'needs_human', 'reason': 'simulation_button_not_unique'}
        if self.dry_run:
            return {'state': 'logged_out_waiting', 'reason': 'dry_run_would_choose_simulation'}
        self.modes.append(now)
        self._save_memory()
        self._event('mode_attempt', now, 'logged_out_waiting', 'simulation_button_confirmed')
        session.click(obs['mode_point'])
        waited = 0.0
        while waited < self.verify_seconds:
            self.sleep(2.0)
            waited += 2.0
            try:
                obs = check_obs(session.detect())
            except Hold:
                continue
            if logged_in(obs):
                self.logged_out_since = None
                return {'state': 'logged_in_now',
                        'reason': 'saved_login_and_simulation_accepted' if after_login else 'simulation_selected'}
        return {'state': 'logged_out_waiting', 'reason': 'simulation_not_confirmed'}

    def _daemon(self, now):
        if not self.manage_daemon:
            return {'state': 'ok', 'reason': 'logged_in'}
        active, enabled = self.units(self.daemon_unit)
        if active == 'active':
            return self._daemon_health(now)
        if active in ('activating', 'reloading', 'deactivating'):
            return {'state': 'ok', 'reason': 'logged_in_daemon_' + active}
        if enabled != 'enabled':
            return {'state': 'ok', 'reason': 'logged_in_daemon_not_enabled'}
        limit = self.allowed(self.starts, now, MIN_START_INTERVAL, MAX_STARTS_PER_HOUR)
        if limit == 'cap':
            return {'state': 'needs_human', 'reason': 'daemon_start_cap_reached'}
        if limit == 'interval':
            return {'state': 'ok', 'reason': 'daemon_start_interval_not_elapsed'}
        if self.dry_run:
            return {'state': 'ok', 'reason': 'dry_run_would_start_daemon'}
        self.starts.append(now)
        self._save_memory()
        self._event('daemon_start_attempt', now, 'ok', 'daemon_' + (active or 'unknown'))
        self.starter(self.daemon_unit)
        return {'state': 'daemon_started', 'reason': 'daemon_was_' + (active or 'unknown')}

    def _daemon_health(self, now):
        """Logged in, daemon active: restart it when /health says the page has been
        not authorized / not synced on every pass for more than 60 s.

        No token or no answer is not this path (a dead daemon is the failed-unit path).
        Shares the daemon-start limiter (self.starts) with _daemon.
        """
        ok = {'state': 'ok', 'reason': 'logged_in_daemon_active'}
        if self.health is None:
            self.daemon_health = 'no_token'
            return ok
        verdict = self.daemon_health = health_verdict(self.health())
        if verdict in ('healthy', 'unreachable'):
            return ok
        self.unhealthy_observed = True
        if self.unhealthy_since is None or self.unhealthy_seen is None \
                or now - self.unhealthy_seen > UNHEALTHY_MAX_GAP or now < self.unhealthy_since:
            self.unhealthy_since = now
        self.unhealthy_seen = now
        held = int(now - self.unhealthy_since)
        if now - self.unhealthy_since <= UNHEALTHY_RESTART_SECONDS:
            return {'state': 'ok', 'reason': 'logged_in_daemon_page_%s_%ds' % (verdict, held)}
        limit = self.allowed(self.starts, now, MIN_START_INTERVAL, MAX_STARTS_PER_HOUR)
        if limit is not None:
            state, reason = (('needs_human', 'daemon_start_cap_reached') if limit == 'cap'
                             else ('ok', 'daemon_start_interval_not_elapsed'))
            self._event('daemon_restart_skipped', now, state, 'page_%s_%ds_%s' % (verdict, held, reason))
            return {'state': state, 'reason': reason}
        if self.dry_run:
            return {'state': 'ok', 'reason': 'dry_run_would_restart_daemon'}
        detail = 'page_%s_for_%ds' % (verdict, held)
        self.starts.append(now)
        self._save_memory()
        self._event('daemon_restart_attempt', now, 'ok', detail)
        self.restarter(self.daemon_unit)
        # state 'daemon_started' lets an external watcher report the recovery.
        self._event('daemon_restarted_unhealthy', now, 'daemon_started', detail)
        self.unhealthy_observed = False  # a fresh 60 s window after the restart
        return {'state': 'daemon_started', 'reason': 'daemon_restarted_unhealthy'}

    def _persist(self, outcome, now):
        if not self.unhealthy_observed:  # healthy, logged out, unreachable, error or restarted
            self.unhealthy_since = self.unhealthy_seen = None
        self.logins = [t for t in self.logins if now - t < 2 * HOUR and t - now < 86400]
        self.starts = [t for t in self.starts if now - t < 2 * HOUR and t - now < 86400]
        self.modes = [t for t in self.modes if now - t < 2 * HOUR and t - now < 86400]
        self._save_memory()
        state = outcome['state'] if outcome['state'] in STATES else 'error'
        reason = outcome['reason']
        # The escalation clock starts at the later of the logout and the last weekend-hold end.
        since = None if self.logged_out_since is None else max(self.logged_out_since,
                                                                self.weekend.last_end(now))
        if state == 'logged_out_waiting' and reason != WEEKEND_HOLD and since is not None \
                and now - since > LOGGED_OUT_ESCALATE_SECONDS:
            state, reason = 'needs_human', 'logged_out_over_15m_' + reason
        outcome = dict(outcome, state=state, reason=reason)
        status = {'schema': SCHEMA, 'version': VERSION, 'state': state, 'reason': outcome['reason'],
                  'checked_at': iso(now), 'logged_out_since': iso(self.logged_out_since),
                  'logins_last_hour': len(self.recent(self.logins, now)),
                  'last_login_attempt_at': iso(max(self.logins) if self.logins else None),
                  'daemon_starts_last_hour': len(self.recent(self.starts, now)),
                  'daemon_health': self.daemon_health,
                  'daemon_unhealthy_since': iso(self.unhealthy_since),
                  'limits': {'min_login_interval_seconds': MIN_LOGIN_INTERVAL,
                             'max_logins_per_hour': MAX_LOGINS_PER_HOUR,
                             'min_daemon_start_interval_seconds': MIN_START_INTERVAL,
                             'max_daemon_starts_per_hour': MAX_STARTS_PER_HOUR,
                             'daemon_unhealthy_restart_seconds': UNHEALTHY_RESTART_SECONDS},
                  'dry_run': self.dry_run, 'port': self.port,
                  'weekend_hold': {'enabled': self.weekend.enabled, 'active': self.weekend.active(now),
                                   'text': self.weekend.text(), 'config_error': self.weekend_error}}
        if self.login_id is not None:  # only for a registry instance; login A's status is unchanged
            status['login_id'] = self.login_id
        write_json(self.state_dir / 'status.json', status)
        self._event('observation', now, state, outcome['reason'])
        return status


# ---------------------------------------------------------------- instances

DEFAULT_LOGIN_ID = 'a'
Instance = namedtuple('Instance', ('login_id', 'port', 'state_dir', 'browser_unit', 'daemon_unit',
                                   'health_port', 'token_env', 'reserved', 'registry_error'))


def _registry_file(path=None):
    """Same lookup as logins.registry_path; usable even when logins.py is missing."""
    root = os.environ.get('TVBRIDGE_ROOT') or '~/.local/share/decrypt-os/tvbridge'
    return os.path.expanduser(str(path or os.environ.get('TVBRIDGE_LOGINS') or root + '/logins.json'))


def _salvage_ports(path):
    """Every integer devtools_port listed for another login in a registry that failed validation."""
    try:
        with open(_registry_file(path), 'rb') as f:
            data = json.loads(f.read(1_000_001))
        entries = data.get('logins') if isinstance(data, dict) else None
    except (OSError, ValueError):
        return set()
    return {e['devtools_port'] for e in (entries if isinstance(entries, list) else [])
            if isinstance(e, dict) and e.get('id') != DEFAULT_LOGIN_ID and type(e.get('devtools_port')) is int
            and e['devtools_port'] != DEFAULT_PORT}


def resolve_instance(login_id=None, path=None):
    """Settings for one guard instance.

    No login_id: today's constants (login A). The registry only adds the other
    logins' DevTools ports to the reserved set; when it exists but is invalid,
    login A keeps running and reserves 9557 plus every port it can still read.
    With a login_id the registry entry decides everything; a missing or invalid
    registry entry is a Hold, never a fallback to login A's browser.
    """
    if login_id is None:
        reserved, error, known = set(), None, []
        if registry is None:  # logins.py not deployed: only a registry file on disk changes anything
            if os.path.exists(_registry_file(path)):
                error = 'login_registry_unavailable'
                reserved |= _salvage_ports(path) | {9557}
        else:
            try:
                known = registry.load_logins(path)
            except ValueError:
                error = 'login_registry_invalid'
                reserved |= _salvage_ports(path) | {9557}
        if known:
            own = next((l for l in known if l.id == DEFAULT_LOGIN_ID), None) or registry.primary(known)
            reserved |= {l.devtools_port for l in known if l.id != own.id}
        return Instance(None, DEFAULT_PORT, DEFAULT_STATE, BROWSER_UNIT, DAEMON_UNIT, DAEMON_HEALTH_PORT,
                        'TVBRIDGE_TOKEN', frozenset(reserved), error)
    if registry is None:
        raise Hold('login_registry_unavailable')
    if not isinstance(login_id, str) or not registry.ID_RE.match(login_id):
        raise Hold('login_id_invalid')
    try:
        known = registry.load_logins(path)
    except ValueError:
        raise Hold('login_registry_invalid')
    try:
        own = registry.by_id(known, login_id)
    except ValueError:
        raise Hold('login_unknown')
    return Instance(own.id, own.devtools_port, Path(own.guard_state_dir), own.browser_unit, own.daemon_unit,
                    own.bridge_port, own.token_env,
                    frozenset(l.devtools_port for l in known if l.id != own.id), None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true', help='one guarded pass (default)')
    mode.add_argument('--status', action='store_true', help='print the saved status only')
    parser.add_argument('--dry-run', action='store_true', help='detect and report, never click, start or restart')
    parser.add_argument('--no-daemon', action='store_true', help='never start or restart the login\'s daemon')
    parser.add_argument('--login', help='login id from logins.json (default: TVBRIDGE_LOGIN_ID, else login A)')
    parser.add_argument('--state-dir', type=Path, default=None, help='default: the login\'s guard_state_dir')
    parser.add_argument('--port', type=int, default=None, help='default: the login\'s devtools_port')
    parser.add_argument('--skip-owner-check', action='store_true',
                        help='do not prove the DevTools listener belongs to the login\'s browser unit')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        inst = resolve_instance(args.login or os.environ.get('TVBRIDGE_LOGIN_ID') or None)
    except Hold as hold:
        print(json.dumps({'state': 'error', 'reason': hold.reason}), file=sys.stderr)
        return 2
    state_dir = args.state_dir or inst.state_dir
    if args.status:
        try:
            print((state_dir / 'status.json').read_text().strip())
        except OSError:
            print(json.dumps({'schema': SCHEMA, 'state': 'error', 'reason': 'status_unreadable'}))
        return 0
    if inst.registry_error:
        print(json.dumps({'warning': inst.registry_error}), file=sys.stderr)
    owner = None if args.skip_owner_check else (lambda port: check_owner(port, inst.browser_unit))
    try:
        hold, hold_error = load_weekend_hold()
        guard = Guard(state_dir=state_dir, port=args.port or inst.port, owner_check=owner,
                      dry_run=args.dry_run, manage_daemon=not args.no_daemon, reserved=inst.reserved,
                      daemon_unit=inst.daemon_unit, token_env=inst.token_env, health_port=inst.health_port,
                      login_id=inst.login_id, weekend=hold, weekend_error=hold_error)
        status = guard.run()
    except Hold as hold:
        print(json.dumps({'state': 'error', 'reason': hold.reason}), file=sys.stderr)
        return 2
    print(json.dumps({k: status.get(k) for k in ('state', 'reason', 'logins_last_hour',
                                                  'daemon_starts_last_hour')}))
    return 1 if status.get('state') == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
