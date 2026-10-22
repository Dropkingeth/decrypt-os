#!/usr/bin/env python3
"""Start the login guard as soon as the bridge page is logged out or unhealthy.

Every 2 s this asks the bridge daemon's loopback /health whether the Tradovate
page is authorized and synced. After two misses in a row it starts
tvbridge-login-guard.service, which does the actual (rate-limited) login, or
restarts a live daemon whose page stayed unauthorized/unsynced while logged in.
It never clicks, logs in, trades, restarts the daemon or touches the kill switch.
The token is never logged.

Multi-login: --login <id> or TVBRIDGE_LOGIN_ID polls that login's daemon (port
from its registry bridge_url, token from its token_env) and starts its guard_unit.
Without an instance it is login A exactly as before.
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import time

try:
    import logins as registry  # login-guard/logins.py, installed beside this file
except ImportError:  # login A still runs on its constants; --login refuses without it
    registry = None

PORT = 8777
POLL_SECONDS = 2.0
MISSES_BEFORE_TRIGGER = 2
COOLDOWN_SECONDS = 20.0
GUARD_UNIT = 'tvbridge-login-guard.service'


def logged_in(health):
    """True only when the daemon answered and its page is authorized."""
    if not isinstance(health, dict):
        return False
    inner = health.get('health') if isinstance(health.get('health'), dict) else health
    page = inner.get('page')
    return isinstance(page, dict) and page.get('state') == 'authorized'


def ready(health):
    """True only when the page is authorized and the daemon's state is synced for its connection.

    An authorized but unsynced page (for example a dead controller lease) is a miss too,
    so the guard gets to see it and can restart the daemon if it lasts.
    """
    if not logged_in(health):
        return False
    inner = health.get('health') if isinstance(health.get('health'), dict) else health
    problems = inner.get('problems')
    return not (isinstance(problems, list) and 'not_synced_for_current_connection' in problems)


MAX_HEALTH_BYTES = 200_000


def read_health(token, port=PORT):
    # Plain http.client on loopback (never proxied). A fresh urllib opener per poll
    # can retain ~400 KB of native memory per call (seen on Python 3.14), which would
    # push the 2 s loop past MemoryMax and get it OOM-killed every few hours.
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        conn.request('GET', '/health/%s' % token)
        r = conn.getresponse()
        if r.status != 200:
            return None
        body = r.read(MAX_HEALTH_BYTES + 1)
        if len(body) > MAX_HEALTH_BYTES:
            return None
        return json.loads(body)
    except Exception:
        return None
    finally:
        conn.close()


class Trigger:
    def __init__(self, start, now=time.monotonic):
        self.start, self.now = start, now
        self.misses = 0
        self.last_start = None

    def observe(self, is_logged_in):
        """Returns True when it started the guard on this observation."""
        if is_logged_in:
            self.misses = 0
            return False
        self.misses += 1
        if self.misses < MISSES_BEFORE_TRIGGER:
            return False
        t = self.now()
        if self.last_start is not None and t - self.last_start < COOLDOWN_SECONDS:
            return False
        self.last_start = t
        self.start()
        return True


def start_guard(unit=GUARD_UNIT):
    subprocess.run(['systemctl', '--user', 'start', '--no-block', unit],
                   capture_output=True, timeout=10)


def resolve(login_id=None, path=None):
    """(daemon port, token env var, guard unit). No login_id: login A's constants.

    Raises ValueError for an invalid registry or an unknown login (never falls back to login A).
    """
    if login_id is None:
        return PORT, 'TVBRIDGE_TOKEN', GUARD_UNIT
    if registry is None:
        raise ValueError('logins.py is missing beside trigger.py')
    if not registry.ID_RE.match(login_id):
        raise ValueError('login id %r is not valid' % login_id)
    own = registry.by_id(registry.load_logins(path), login_id)
    return own.bridge_port, own.token_env, own.guard_unit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--login', help='login id from logins.json (default: TVBRIDGE_LOGIN_ID, else login A)')
    args = parser.parse_args(argv)
    try:
        port, token_env, unit = resolve(args.login or os.environ.get('TVBRIDGE_LOGIN_ID') or None)
    except ValueError as exc:
        print('login registry: %s' % exc, file=sys.stderr)
        return 2
    # The instance's tvbridge.env sets both its token_env and TVBRIDGE_TOKEN.
    token = os.environ.get(token_env) or os.environ.get('TVBRIDGE_TOKEN', '')
    if len(token) < 24:
        print('%s missing' % token_env, file=sys.stderr)
        return 2
    trig = Trigger(lambda: start_guard(unit))
    while True:
        ok = ready(read_health(token, port))
        if trig.observe(ok):
            print('bridge page not authorized/synced on %d checks; started %s' % (MISSES_BEFORE_TRIGGER, unit),
                  file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    sys.exit(main())
