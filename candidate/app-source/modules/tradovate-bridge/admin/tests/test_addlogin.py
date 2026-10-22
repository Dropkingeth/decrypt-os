"""Add login tests. The bridge's install.py runs against a copied tree; systemctl is
tools/fake_systemctl.py; the new login's daemon /health is a loopback fake. Nothing here runs the real
systemctl, reads live state, opens a browser or signs in.

    TMPDIR=$HOME/.cache PYTHONDONTWRITEBYTECODE=1 python3 tools/run_worktests.py tests/test_addlogin.py
"""
import fcntl
import glob
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'app'))

import loginops  # noqa: E402
from engine import Conflict  # noqa: E402
from model import Invalid  # noqa: E402
from test_admin import KEYS  # noqa: E402
from test_restarter import (DA, DB, FIRING, STATUS, TOKENS, SystemdBase,  # noqa: E402
                            seq)
from tree import Profile, ProfileError  # noqa: E402

RELEASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'bridge')
REAL_UNITS = os.path.expanduser('~/.config/systemd/user')
MODULE_UNITS = os.path.join(os.path.dirname(RELEASE), 'systemd')
DC = 'tvbridge-daemon@c.service'
BC, TC, GC = 'tvbridge-browser@c.service', 'tvbridge-login-trigger@c.service', 'tvbridge-login-guard@c.timer'
ORDER_B = 'ob-' + 'z' * 30
PEERS_HEAD = ('# Read-only login tokens (GET /login and /health only), written by scripts/install.py add-login.\n'
              '# For every daemon, the firing service and the status panel. Mode 0600. Never commit, copy or print.\n'
              '# Holds no order token: never TVBRIDGE_TOKEN or TVBRIDGE_TOKEN_<ID>.\n')
FIRING_HEAD = ('# Order tokens of the non-primary logins, for tvbridge-firing.service ONLY (its follower orders\n'
               '# go to each login\'s daemon). Written by scripts/install.py add-login. Mode 0600. Never commit,\n'
               '# copy or print, and never load it in any other unit.\n')
_RELEASE_COPY = None


def env_vals(path):
    """Test-side reader of an env file (the app itself only keeps SHA-256 digests)."""
    out = {}
    try:
        with open(path) as f:
            for line in f.read().splitlines():
                if line.strip() and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def release_copy():
    """scripts/ + tvbridge/ of release 1.1.3, copied once (read-only use of the live release)."""
    global _RELEASE_COPY
    if _RELEASE_COPY is None or not os.path.isdir(_RELEASE_COPY):   # other test modules reuse it later
        d = tempfile.mkdtemp(prefix='tba-rel-')
        for sub in ('scripts', 'tvbridge'):
            shutil.copytree(os.path.join(RELEASE, sub), os.path.join(d, sub),
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        _RELEASE_COPY = d
    return _RELEASE_COPY


def tearDownModule():
    global _RELEASE_COPY
    if _RELEASE_COPY:
        shutil.rmtree(_RELEASE_COPY, ignore_errors=True)
        _RELEASE_COPY = None


class FakeLoginC:
    """GET /health/<read token> for login C's daemon. The token is whatever install.py wrote to peers.env;
    the answer follows tvbridge-daemon@c.service in the fake systemctl state."""

    def __init__(self, state_path, peers_env):
        st, pe = state_path, peers_env

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                with open(st) as f:
                    u = json.load(f)['units'].get(DC, {})
                tok = env_vals(pe).get('TVBRIDGE_READ_TOKEN_C')
                if u.get('active') != 'active' or u.get('mode') in ('waiting', 'stale', 'crash'):
                    self.close_connection = True
                    return
                ok = bool(tok) and self.path == f'/health/{tok}'
                synced = u.get('mode') == 'synced'
                body = {'health': {'ok': synced, 'problems': [] if synced else ['not_synced_for_current_connection'],
                                   'page': {'state': 'authorized', 'synced': synced}}} if ok else {'error': 'not found'}
                data = json.dumps(body).encode()
                self.send_response(200 if ok else 404)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self):
        return f'http://127.0.0.1:{self.srv.server_address[1]}'

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class AddLoginBase(SystemdBase):
    def profile_extra(self):
        # Guard: Base rewrites the sandbox paths to this test's own copy by text. If test_admin was imported from
        # a path spelled differently from the one in sandbox/profile.json (a work copy's sandbox symlink), the
        # rewrite would silently miss and the test would run on the shared sandbox. Refuse instead.
        import test_admin
        with open(os.path.join(test_admin.FIXTURE, 'profile.json')) as f:
            if test_admin.SB + '/' not in f.read():
                raise RuntimeError('test_admin.SB does not match tests/fixture/profile.json')
        extra = super().profile_extra()
        tb = os.path.join(self.tmp, 'tree/tvbridge')
        # the env files login A/B have live (fake values, install.py's own format)
        self.write_env(os.path.join(tb, 'logins/peers.env'), PEERS_HEAD + ''.join(
            f'{k}={TOKENS[k]}\n' for k in ('TVBRIDGE_READ_TOKEN_A', 'TVBRIDGE_READ_TOKEN_B')))
        self.write_env(os.path.join(tb, 'logins/firing.env'), FIRING_HEAD + f'TVBRIDGE_TOKEN_B={ORDER_B}\n')
        self.write_env(os.path.join(tb, 'logins/b/tvbridge.env'), f'TVBRIDGE_TOKEN={ORDER_B}\nTVBRIDGE_TOKEN_B={ORDER_B}\n')
        for lg in ('a', 'b'):
            self.write_env(os.path.join(tb, f'logins/manual-{lg}.env'), f'TVBRIDGE_MANUAL_TOKEN_{lg.upper()}=m{lg}-' +
                           'q' * 40 + '\n')
        # unit folder: the live templates and drop-ins as they are (read-only copies)
        self.units = os.path.join(self.tmp, 'units')
        os.makedirs(self.units)
        for t in loginops.TEMPLATES:   # the module's shipped templates
            shutil.copy2(os.path.join(MODULE_UNITS, t), os.path.join(self.units, t))
        for d in ('tvbridge-admin.service.d', 'tvbridge-daemon@b.service.d'):   # login B's drop-ins, as Add login writes
            shutil.copytree(os.path.join(test_admin.FIXTURE, 'units', d), os.path.join(self.units, d))
        # browser profiles: login A's and B's (marker only)
        self.browser = os.path.join(self.tmp, 'browser')
        os.makedirs(os.path.join(self.browser, 'profile'))
        os.makedirs(os.path.join(self.browser, 'profile-b'))
        with open(os.path.join(self.browser, 'profile-b', '.tvbridge-login'), 'w') as f:
            f.write('b\n')
        self.guard['c'] = os.path.join(self.tmp, 'guard-c')
        os.makedirs(self.guard['c'])
        self.fake_c = FakeLoginC(self.state_path, os.path.join(tb, 'logins/peers.env'))
        extra['systemd']['logins_override']['c'] = {'bridge_url': self.fake_c.url(), 'guard_state_dir': self.guard['c']}
        extra['systemd']['logins_override']['d'] = {'bridge_url': self.fake_c.url(), 'guard_state_dir': self.guard['c']}
        extra['token_files'] = True
        extra['login_setup'] = {'unit_dir': self.units, 'browser_profile_dir': os.path.join(self.browser, 'profile'),
                                'install_script': os.path.join(release_copy(), 'scripts', 'install.py'),
                                'python': sys.executable, 'signin_wait_s': 2}
        self.snap = {}
        return extra

    def setUp(self):
        super().setUp()
        self.e.loginops.listening = lambda: set()      # the machine's own ports never decide a test
        self.e.loginops.sleep = lambda s: time.sleep(min(s, 0.05))
        self.before = {p: self.raw(p) for p in ('logins.json', 'config.json', 'logins/peers.env', 'logins/firing.env',
                                                 'firing.json', 'logins/b/config.json')}
        self.cleared = lambda: self.e.loginops._signin_cache.clear()
        self.admin_dropin = os.path.join(self.units, 'tvbridge-admin.service.d', 'manual-tokens.conf')
        with open(self.admin_dropin, 'rb') as f:
            self.admin_before = f.read()

    def tearDown(self):
        super().tearDown()
        self.fake_c.close()

    @staticmethod
    def write_env(path, text):
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, 0o600)

    def secrets_now(self):
        """Every token value in the tree (for the no-secret-in-records checks)."""
        vals = set(TOKENS.values()) | {ORDER_B}
        for path in glob.glob(self.tb('logins', '**', '*.env'), recursive=True) + \
                glob.glob(self.tb('logins', '*.removed-*', '*.env')):
            vals |= {v for k, v in env_vals(path).items() if 'TOKEN' in k and len(v) >= 24}
        return vals

    def assert_no_secrets_in_records(self):
        vals = self.secrets_now()
        self.assertGreaterEqual(len(vals), 5)
        for path in glob.glob(os.path.join(self.profile.admin_state, '**', '*'), recursive=True):
            if os.path.isfile(path):
                with open(path, errors='replace') as f:
                    text = f.read()
                for v in vals:
                    self.assertNotIn(v, text, path)

    def add_c(self):
        ch = self.e.preview('add_login', {'label': 'Login C'})
        self.assertEqual(ch['plan']['blockers'], [], ch['plan'])
        return self.e.confirm(ch['id'], ch['preview_hash'])

    def restarted(self, *units):
        """A/B daemons restarted after the add (as a flat, signed-in restart would), in the fake."""
        def fn(st):
            for u in units:
                st['units'][u]['active_enter'] = time.time() + 5
        with open(self.state_path + '.lock', 'w') as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            self.edit_state(fn)

    def signin(self, lid='c'):
        self.e.loginops._signin_cache.clear()
        return next(lg for lg in self.e.overview()['logins'] if lg['id'] == lid)['setup']['signin']

    def start_c(self):
        ch = self.e.preview('start_login', {'login': 'c'})
        self.assertEqual(ch['plan']['blockers'], [])
        return self.e.confirm(ch['id'], ch['preview_hash'])

    def originals_left(self):
        """Hard-linked originals outside a kept backup folder (*.removed-*/originals/ is the backup)."""
        found = glob.glob(self.tb('**', '*.addlogin-*'), recursive=True) + \
            glob.glob(self.tb('**', '*.rmlogin-*'), recursive=True) + \
            glob.glob(os.path.join(self.units, '**', '*.addlogin-*'), recursive=True) + \
            glob.glob(os.path.join(self.units, '**', '*.rmlogin-*'), recursive=True)
        return [x for x in found if '.removed-' not in x]

    def guard_starts_daemon_c(self, mode='synced', after=0.2):
        """What the login guard does once it sees the page signed in: start the enabled daemon."""
        def go():
            with open(self.state_path + '.lock', 'w') as lk:   # the fake's own lock: no lost update
                fcntl.flock(lk, fcntl.LOCK_EX)
                self.edit_state(lambda st: st['units'].__setitem__(DC, {
                    'active': 'active', 'sub': 'running', 'pid': 7777, 'nrestarts': 0, 'result': 'success',
                    'mode': mode}))
        if after:
            threading.Timer(after, go).start()
        else:
            go()

    def unit_files(self):
        return self.read_state().get('unit_files', {})


class TestPreview(AddLoginBase):
    def test_preview_lists_everything_and_changes_nothing(self):
        ch = self.e.preview('add_login', {'label': 'Login C'})
        plan = ch['plan']
        self.assertEqual(plan['blockers'], [])
        lg = plan['login']
        self.assertEqual((lg['id'], lg['label'], lg['devtools_port'], lg['bridge_port']), ('c', 'Login C', 9558, 8780))
        self.assertNotIn('dry_run', lg)
        paths = {f['path']: f for f in plan['files']}
        for p in ('tvbridge/logins/c/config.json', 'tvbridge/logins/c/tvbridge.env', 'tvbridge/logins/c/login.env',
                  'tvbridge/logins/c/journal', 'tvbridge/logins/peers.env', 'tvbridge/logins/firing.env',
                  'tvbridge/logins.json', 'tvbridge/config.json', 'tvbridge/logins/manual-c.env'):
            self.assertIn(p, paths, sorted(paths))
        self.assertTrue(any(f['action'] == 'create' and f['path'] == 'tvbridge/logins/c/ENTRIES-DISABLED'
                            and f['by'] == 'install.py' for f in plan['files']))
        self.assertTrue(any(f['action'] == 'rename' and f['path'].endswith('logins/c/ENTRIES-DISABLED')
                            for f in plan['files']))
        self.assertTrue(any(f['path'].endswith('tvbridge-daemon@c.service.d/manual-token.conf') for f in plan['files']))
        self.assertTrue(any(f['path'].endswith('tvbridge-admin.service.d/manual-tokens.conf') for f in plan['files']))
        self.assertEqual([t['name'] for t in lg['tokens']],
                         ['TVBRIDGE_TOKEN_C', 'TVBRIDGE_READ_TOKEN_C', 'TVBRIDGE_MANUAL_TOKEN_C'])
        units = {u['unit']: u for u in lg['units']}
        self.assertEqual(units[BC]['when'], 'now')
        self.assertEqual(units[DC]['when'], 'Start step')
        self.assertEqual([r['unit'] for r in plan['restarts']], [STATUS])
        self.assertEqual(plan['wait_on'], [])
        self.assertEqual(ch['eta']['kind'], 'now')
        # nothing written, no unit touched, no secret in the preview
        self.assertFalse(os.path.exists(self.tb('logins/c')))
        self.assertFalse(os.path.exists(os.path.join(self.browser, 'profile-c')))
        for p, b in self.before.items():
            self.assertEqual(self.raw(p), b, p)
        self.assertEqual(self.actions(), [])
        self.assert_no_secrets_in_records()

    def test_next_id_and_name_rules(self):
        os.makedirs(self.tb('logins/c'))             # leftover of an earlier login c: never reused
        ch = self.e.preview('add_login', {'label': 'Login D'})
        self.assertEqual(ch['plan']['login']['id'], 'd')
        for bad in ({'label': 'Login B'}, {'label': ''}, {'label': 'x' * 41}, {'label': 'a;rm -rf'}, {}):
            with self.assertRaises(Invalid, msg=str(bad)):
                self.e.preview('add_login', bad)
        for t in ('start_login', 'remove_login'):
            with self.assertRaises(Invalid):
                self.e.preview(t, {'login': 'a'})   # the primary is never started or removed here
            with self.assertRaises(Invalid):
                self.e.preview(t, {'login': 'zz'})

    def test_blockers(self):
        self.e.loginops.listening = lambda: {8780}
        ch = self.e.preview('add_login', {'label': 'Login C'})
        self.assertTrue(any('8780' in b and 'in use' in b for b in ch['plan']['blockers']), ch['plan']['blockers'])
        self.e.loginops.listening = lambda: set()
        os.makedirs(os.path.join(self.units, 'tvbridge-daemon@c.service.d'))
        ch = self.e.preview('add_login', {'label': 'Login C'})
        self.assertTrue(any('tvbridge-daemon@c.service.d' in b for b in ch['plan']['blockers']))
        with self.assertRaises(Conflict):
            self.e.confirm(ch['id'], ch['preview_hash'])
        os.rmdir(os.path.join(self.units, 'tvbridge-daemon@c.service.d'))
        self.write_env(self.tb('logins/peers.env'), PEERS_HEAD + f'TVBRIDGE_READ_TOKEN_A={TOKENS["TVBRIDGE_READ_TOKEN_A"]}\n')
        ch = self.e.preview('add_login', {'label': 'Login C'})
        self.assertTrue(any('no read token for Login B' in b for b in ch['plan']['blockers']), ch['plan']['blockers'])
        self.assertTrue(any('install.py warns' in b for b in ch['plan']['blockers']))
        os.remove(os.path.join(self.units, 'tvbridge-browser@.service'))
        ch = self.e.preview('add_login', {'label': 'Login C'})
        self.assertTrue(any('templates are missing' in b for b in ch['plan']['blockers']))
        # A system template folder (Decrypt OS: /etc/systemd/user) counts as well.
        self.e.loginops.cfg['template_dirs'] = [MODULE_UNITS]
        ch = self.e.preview('add_login', {'label': 'Login C'})
        self.assertFalse(any('templates are missing' in b for b in ch['plan']['blockers']))
        self.assertEqual(self.actions(), [])

    def test_port_of_this_page_is_never_picked(self):
        # login D used to get loopback 8781 (this page's port) from install.py's port picker; it is skipped now
        self.assertEqual(self.add_c()['status'], 'applied')
        ch = self.e.preview('add_login', {'label': 'Login D'})
        self.assertEqual(ch['plan']['login']['bridge_port'], 8782)
        self.assertFalse(any('8781' in b for b in ch['plan']['blockers']), ch['plan']['blockers'])

    def test_stale_preview(self):
        ch = self.e.preview('add_login', {'label': 'Login C'})
        with open(self.tb('logins/firing.env'), 'a') as f:
            f.write('# touched\n')
        with self.assertRaises(Conflict) as cm:
            self.e.confirm(ch['id'], ch['preview_hash'])
        self.assertEqual(cm.exception.code, 'stale_preview')
        self.assertFalse(os.path.exists(self.tb('logins/c')))


class TestAddLogin(AddLoginBase):
    def test_add_login_end_to_end(self):
        ch = self.add_c()
        self.assertEqual(ch['status'], 'applied', (ch.get('error'), ch['result']['steps']))
        names = [s['name'] for s in ch['result']['steps']]
        self.assertEqual(names, ['preflight', 'dry run', 'record', 'backup', 'install.py add-login', 'check', 'peers',
                                 'login switch', 'flatten token', 'browser', 'restart', 'verify'])
        self.assertTrue(all(s['ok'] for s in ch['result']['steps']))
        self.assertEqual(ch['result']['login']['stage'], 'awaiting_signin')
        # only this login's browser + daemon-reload + the read-only panel; nothing of A, B, firing or Soup
        self.assertEqual(self.actions(), [('daemon-reload', ''), ('enable', BC), ('start', BC), ('restart', STATUS)])
        self.assertEqual(self.read_state()['units'].get(DC, {}).get('active', 'inactive'), 'inactive')
        self.assertFalse(self.read_state().get('soup_touched'))
        # registry, login A's peer entry, tokens
        reg = self.jload('logins.json')
        self.assertEqual([e['id'] for e in reg['logins']], ['a', 'b', 'c'])
        self.assertEqual(reg['logins'][2]['bridge_url'], 'http://127.0.0.1:8780')
        self.assertEqual(self.jload('config.json')['peer_logins'][-1]['id'], 'c')
        self.assertEqual(self.raw('firing.json'), self.before['firing.json'])
        bcfg = self.jload('logins/b/config.json')                          # B lists C as a peer
        self.assertEqual(bcfg['peer_logins'][-1], {'id': 'c', 'url': 'http://127.0.0.1:8780',
                                                   'token_env': 'TVBRIDGE_READ_TOKEN_C'})
        self.assertEqual(self.originals_left(), [])                        # hard-linked originals dropped
        rec = loginops.read_record(self.profile, 'c')
        self.assertEqual((rec['state'], rec['change_id']), ('created', ch['id']))
        peers = env_vals(self.tb('logins/peers.env'))
        self.assertEqual({k: peers[k] for k in TOKENS}, TOKENS)
        self.assertGreaterEqual(len(peers['TVBRIDGE_READ_TOKEN_C']), 24)
        fe = env_vals(self.tb('logins/firing.env'))
        self.assertEqual(fe['TVBRIDGE_TOKEN_B'], ORDER_B)
        manual = self.tb('logins/manual-c.env')
        self.assertEqual(stat.S_IMODE(os.stat(manual).st_mode), 0o600)
        self.assertEqual(list(env_vals(manual)), ['TVBRIDGE_MANUAL_TOKEN_C'])
        with open(os.path.join(self.units, 'tvbridge-daemon@c.service.d', 'manual-token.conf')) as f:
            self.assertIn('EnvironmentFile=-', f.read())
        with open(self.admin_dropin) as f:
            self.assertTrue(f.read().rstrip().endswith('manual-c.env'))
        # login-wide switch retired as on Login B; the login has no accounts
        self.assertFalse(os.path.exists(self.tb('logins/c/ENTRIES-DISABLED')))
        self.assertTrue(glob.glob(self.tb('logins/c/ENTRIES-DISABLED.removed-*')))
        cfg = self.jload('logins/c/config.json')
        self.assertEqual((cfg['mode'], cfg['followers']), ('followers_only', []))
        self.assertTrue(os.path.isfile(os.path.join(self.browser, 'profile-c', '.tvbridge-login')))
        self.assertEqual(self.unit_files(), {BC: 'enabled'})
        # page view
        ov = self.e.overview()
        c = next(lg for lg in ov['logins'] if lg['id'] == 'c')
        self.assertEqual((c['setup']['stage'], c['setup']['can_start'], c['setup']['can_remove']),
                         ('awaiting_signin', True, True))
        self.assertEqual(ov['login_setup'], {'available': True, 'reason': None, 'next_id': 'd', 'incomplete': []})
        self.assertEqual(c['setup']['origin'], 'this_page')
        json.dumps(ov)                                   # the page gets it as JSON
        b = next(lg for lg in ov['logins'] if lg['id'] == 'b')
        self.assertEqual((b['setup']['can_remove'], b['setup']['can_start'], b['setup']['origin']),
                         (False, False, 'outside'))
        self.assertIn('not added by this page', b['setup']['remove_blocker'])
        self.assertIsNone(next(lg for lg in ov['logins'] if lg['id'] == 'a')['setup'])
        self.assertEqual(self.e.audit.read(1)[0]['status'], 'applied')
        self.assertTrue(self.e.audit.verify_chain())
        self.assert_no_secrets_in_records()

    def test_start_login_lets_the_guard_start_the_daemon(self):
        self.add_c()
        self.clear_calls()
        ch = self.e.preview('start_login', {'login': 'c'})
        self.assertEqual(ch['plan']['blockers'], [])
        t0 = time.time()
        ch = self.e.confirm(ch['id'], ch['preview_hash'])
        self.assertLess(time.time() - t0, 3)                 # no in-lock wait for the sign-in
        self.assertEqual(ch['status'], 'applied', ch['result'])
        self.assertEqual((ch['result']['login']['state'], ch['result']['login']['connected']), ('starting', False))
        self.assertEqual(self.signin()['state'], 'starting')
        self.guard_starts_daemon_c(after=0)                  # the guard sees the sign-in and starts the daemon
        self.assertEqual(self.signin()['state'], 'connected')
        # enabled, but the daemon is never started from this page
        self.assertEqual(self.actions(), [('enable', DC), ('enable', TC), ('enable', GC), ('start', GC), ('start', TC)])
        self.assertEqual(self.unit_files(), {BC: 'enabled', DC: 'enabled', TC: 'enabled', GC: 'enabled'})
        c = next(lg for lg in self.e.overview()['logins'] if lg['id'] == 'c')
        self.assertEqual(c['setup']['stage'], 'running')
        # a second Start is refused
        self.assertTrue(self.e.preview('start_login', {'login': 'c'})['plan']['blockers'])

    def test_start_login_signed_out_waits_for_the_guard(self):
        self.add_c()
        with open(os.path.join(self.guard['c'], 'status.json'), 'w') as f:
            json.dump({'state': 'needs_human', 'reason': 'saved_login_not_filled',
                       'checked_at': time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()), 'logged_out_since': None}, f)
        ch = self.start_c()
        self.assertEqual(ch['status'], 'applied')
        sig = self.signin()
        self.assertEqual(sig['state'], 'signed_out')
        self.assertIn('Sign in by hand', sig['text'])
        self.assertNotIn(('start', DC), self.actions())

    def test_flatten_stays_responsive_while_start_waits(self):
        """Start returns at once; while Login C waits for its sign-in, Flatten now on another
        account is previewed and confirmed without waiting on the engine lock."""
        self.add_c()
        done = {}

        def start():
            done['start'] = self.start_c()
        th = threading.Thread(target=start)
        th.start()
        t0 = time.time()
        fl = self.e.preview('flatten', {'account': 'account-d'})
        fl = self.e.confirm(fl['id'], fl['preview_hash'], fl['plan']['confirm']['expect'])
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(fl['status'], 'applied', fl.get('error'))
        th.join(10)
        self.assertEqual(done['start']['result']['login']['state'], 'starting')
        self.assertEqual(self.signin()['state'], 'starting')            # still waiting for the sign-in
        self.assertTrue(self.e.lock.acquire(blocking=False))            # nothing holds the engine lock
        self.e.lock.release()
        fl = self.e.preview('flatten', {'account': 'account-b'})
        self.assertEqual(self.e.confirm(fl['id'], fl['preview_hash'], fl['plan']['confirm']['expect'])['status'],
                         'applied')

    def test_start_and_remove_refuse_logins_not_added_here(self):
        """Login B (and any login this page did not add) is never started or removed here."""
        self.edit_state(lambda st: st['units'].__setitem__('tvbridge-browser@b.service', {
            'active': 'active', 'sub': 'running', 'pid': 1, 'nrestarts': 0, 'result': 'success', 'mode': 'ok'}))
        for t in ('start_login', 'remove_login'):
            ch = self.e.preview(t, {'login': 'b'})
            self.assertTrue(any('not added by this page' in b for b in ch['plan']['blockers']), ch['plan'])
            with self.assertRaises(Conflict):
                self.e.confirm(ch['id'], ch['preview_hash'], 'Login B')
        self.assertEqual(self.actions(), [])

    def test_start_fails_after_units_started_says_what_runs(self):
        """A failed Start restores the units, or names what still runs (needs a person)."""
        self.add_c()
        self.edit_state(lambda st: st.__setitem__('fail_verbs', {f'start {TC}': True}))
        ch = self.start_c()
        self.assertEqual(ch['status'], 'failed')
        self.assertEqual(ch['result']['steps'][-1]['name'], 'rollback')
        self.assertTrue(ch['result']['steps'][-1]['ok'])
        self.assertEqual(self.read_state()['units'][GC]['active'], 'inactive')
        self.assertTrue(ch['eta']['text'].startswith('Failed and undone'))
        # now the stop of the guard timer fails too: the page says it still runs
        self.edit_state(lambda st: st.__setitem__('fail_verbs', {f'start {TC}': True, f'stop {GC}': True}))
        ch = self.start_c()
        self.assertEqual(ch['status'], 'failed')
        self.assertIn(GC, ch['result']['needs_person']['units'])
        self.assertIn('Still not as before', ch['error'])
        self.assertFalse(ch['eta']['text'].startswith('Failed and undone'))
        self.assertIn(GC, ch['eta']['text'])

    def test_new_login_account_restarts_only_its_daemon(self):
        """After sign-in, an account added on login C restarts daemon C, firing and the panel; never A or B.
        Login C's read token comes from peers.env (token_files): this service was never restarted."""
        self.assertNotIn('TVBRIDGE_READ_TOKEN_C', os.environ)
        self.add_c()
        self.assertEqual(self.start_c()['status'], 'applied')
        self.guard_starts_daemon_c(after=0)
        acct = {'login': 'c', 'key': 'account-i', 'label': 'Account I', 'account_name': 'DEMOC0001',
                'prop_profile': 'cross-account-window', 'webhook': 'main'}
        pv = self.e.preview('add_account', acct)                 # A and B have not restarted yet
        self.assertIn('Adding accounts to Login C needs a restart of Login A/B while flat and signed in to see '
                      'Login C.', pv['plan']['blockers'])
        with self.assertRaises(Conflict):
            self.e.confirm(pv['id'], pv['preview_hash'])
        self.restarted(DA)
        pv = self.e.preview('add_account', acct)
        self.assertIn('needs a restart of Login B while flat', ' '.join(pv['plan']['blockers']))
        self.restarted(DB)
        self.clear_calls()
        ch = self.do('add_account', {'login': 'c', 'key': 'account-i', 'label': 'Account I', 'account_name': 'DEMOC0001',
                                     'prop_profile': 'cross-account-window', 'webhook': 'main'})
        self.assert_full_apply(ch, seq(DC))
        self.assertEqual(self.jload('firing.json')['accounts']['account-i']['login'], 'c')
        self.assertEqual(self.acct('account-i')['pause']['on'], True)          # starts paused
        self.assertTrue(os.path.exists(self.tb('logins/c/ENTRIES-DISABLED-account-i')))
        rm = self.e.preview('remove_login', {'login': 'c'})
        self.assertTrue(any('still has accounts' in b for b in rm['plan']['blockers']))


class TestRemoveAndRollback(AddLoginBase):
    def assert_back_to_before(self):
        for p, b in self.before.items():
            self.assertEqual(self.raw(p), b, p)
        with open(self.admin_dropin, 'rb') as f:
            self.assertEqual(f.read(), self.admin_before)
        self.assertFalse(os.path.exists(self.tb('logins/c')))
        self.assertFalse(os.path.exists(self.tb('logins/manual-c.env')))
        self.assertFalse(os.path.exists(os.path.join(self.units, 'tvbridge-daemon@c.service.d')))
        self.assertFalse(os.path.exists(os.path.join(self.browser, 'profile-c')))

    def test_remove_login_keeps_a_backup(self):
        self.add_c()
        ch = self.e.preview('start_login', {'login': 'c'})
        self.guard_starts_daemon_c(after=0.1)
        self.e.confirm(ch['id'], ch['preview_hash'])
        self.clear_calls()
        rm = self.e.preview('remove_login', {'login': 'c'})
        self.assertEqual(rm['plan']['blockers'], [])
        self.assertTrue(rm['plan']['confirm']['required'])
        with self.assertRaises(Conflict):
            self.e.confirm(rm['id'], rm['preview_hash'], 'login c')
        rm = self.e.confirm(rm['id'], rm['preview_hash'], 'Login C')
        self.assertEqual(rm['status'], 'applied', (rm.get('error'), rm['result']['steps']))
        self.assertEqual(self.actions(), [('stop', TC), ('stop', GC), ('stop', 'tvbridge-login-guard@c.service'),
                                          ('stop', DC), ('stop', BC), ('disable', TC), ('disable', GC),
                                          ('disable', DC), ('disable', BC), ('daemon-reload', '')])
        self.assert_back_to_before()
        kept = glob.glob(self.tb('logins/c.removed-*'))
        self.assertEqual(len(kept), 1)
        for name in ('config.json', 'tvbridge.env', 'login.env', 'journal', 'manual-c.env',
                     'units/tvbridge-daemon@c.service.d/manual-token.conf'):
            self.assertTrue(os.path.exists(os.path.join(kept[0], name)), name)
        self.assertEqual(len(glob.glob(os.path.join(self.browser, 'profile-c.removed-*'))), 1)
        self.assertEqual(self.e.overview()['login_setup']['next_id'], 'd')    # c is never reused
        self.assert_no_secrets_in_records()

    def test_rollback_of_add_login_is_remove(self):
        ch = self.add_c()
        rb = self.e.rollback_preview(ch['id'])
        self.assertEqual(rb['plan']['blockers'], [])
        self.assertEqual(rb['plan']['confirm']['expect'], 'Login C')
        rb = self.e.confirm(rb['id'], rb['preview_hash'], 'Login C')
        self.assertEqual(rb['status'], 'applied', rb['result']['steps'])
        self.assertEqual(self.e.get(ch['id'])['status'], 'rolled_back')
        self.assert_back_to_before()


class TestFailures(AddLoginBase):
    def assert_undone(self, ch):
        self.assertEqual(ch['status'], 'failed')
        self.assertTrue(ch['result']['rolled_back'], ch['result']['steps'])
        self.assertNotIn('needs_person', ch['result'])
        for p, b in self.before.items():
            self.assertEqual(self.raw(p), b, p)
        with open(self.admin_dropin, 'rb') as f:
            self.assertEqual(f.read(), self.admin_before)
        self.assertFalse(os.path.exists(self.tb('logins/c')))
        self.assertFalse(os.path.exists(self.tb('logins/manual-c.env')))
        self.assertFalse(os.path.exists(os.path.join(self.units, 'tvbridge-daemon@c.service.d')))
        self.assertEqual(self.originals_left(), [])
        self.assertEqual(loginops.read_record(self.profile, 'c')['state'], 'undone')
        self.assertEqual(self.unit_files().get(BC, 'disabled'), 'disabled')
        self.assertEqual(self.read_state()['units'].get(BC, {}).get('active', 'inactive'), 'inactive')

    def test_browser_start_fails_everything_undone(self):
        self.edit_state(lambda st: st.__setitem__('fail_verbs', {f'start {BC}': True}))
        ch = self.add_c()
        self.assert_undone(ch)
        self.assertFalse(os.path.exists(os.path.join(self.browser, 'profile-c')))
        self.assertNotIn(('restart', STATUS), self.actions())
        self.assertEqual([a for a in self.actions() if a[0] == 'daemon-reload'], [('daemon-reload', '')] * 2)

    def test_status_restart_fails_undone_and_panel_restarted(self):
        self.on_restart(STATUS, 'fail', 'synced')
        ch = self.add_c()
        self.assert_undone(ch)
        acts = self.actions()
        self.assertEqual(acts[-1], ('restart', STATUS))
        self.assertEqual(acts.count(('restart', STATUS)), 2)
        self.assertLess(acts.index(('restart', STATUS)), acts.index(('disable', BC)))
        self.assertTrue(ch['result']['steps'][-1]['ok'])           # restart old files: panel back

    def test_install_differs_from_its_dry_run(self):
        real_run = self.e.loginops.run

        def run(argv, timeout, env):
            code, out, err = real_run(argv, timeout, env)
            if '--dry-run' not in argv:
                out = out.replace('  replace  ', '  create   ', 1)
            return code, out, err
        self.e.loginops.run = run
        ch = self.add_c()
        self.assert_undone(ch)
        self.assertIn('other than its dry run', ch['error'])

    def wrap_real_run(self, after):
        real_run = self.e.loginops.run

        def run(argv, timeout, env):
            code, out, err = real_run(argv, timeout, env)
            if '--dry-run' not in argv:
                return after(code, out, err)
            return code, out, err
        self.e.loginops.run = run

    def test_partial_install_failure_restores_byte_exact(self):
        """`install.py` exits non-zero after rewriting the token files; quoted values and comments
        in firing.env come back byte for byte."""
        self.write_env(self.tb('logins/firing.env'), FIRING_HEAD + '# hand note\n' + f'TVBRIDGE_TOKEN_B="{ORDER_B}"\n')
        self.before['logins/firing.env'] = self.raw('logins/firing.env')
        self.wrap_real_run(lambda code, out, err: (1, out, 'add-login crashed (test)'))
        ch = self.add_c()
        self.assert_undone(ch)
        self.assertIn('install.py add-login failed', ch['error'])

    def test_changed_existing_token_is_refused_and_restored(self):
        def change_b(code, out, err):
            path = self.tb('logins/firing.env')
            text = open(path).read().replace(ORDER_B, 'ob-' + 'w' * 30)
            loginops.write_private(path, text)
            return code, out, err
        self.wrap_real_run(change_b)
        ch = self.add_c()
        self.assert_undone(ch)
        self.assertIn('TVBRIDGE_TOKEN_B', ch['error'])                  # the name only, never a value
        self.assert_no_secrets_in_records()

    def test_undo_that_cannot_verify_needs_a_person_with_paths(self):
        def lose_original(code, out, err):
            for x in glob.glob(self.tb('logins', 'firing.env.addlogin-*')):
                os.remove(x)
            return 1, out, 'crashed (test)'
        self.wrap_real_run(lose_original)
        ch = self.add_c()
        self.assertEqual(ch['status'], 'failed')
        np = ch['result']['needs_person']
        self.assertIn(f'Not restored: {self.tb("logins/firing.env")}', np['text'])
        self.assertIn('Remove login Login C', np['text'])
        self.assertEqual(loginops.read_record(self.profile, 'c')['state'], 'add_failed')
        self.assertEqual(self.raw('logins/peers.env'), self.before['logins/peers.env'])   # the rest is back
        ov = self.e.overview()
        self.assertEqual([x['id'] for x in ov['login_setup']['incomplete']], ['c'])
        self.assertEqual(ov['login_setup']['next_id'], 'd')               # c is not reused

    def test_incomplete_undo_still_restarts_the_panel(self):
        """A failed undo still restarts the panel and names the unit that needs a person."""
        self.on_restart(STATUS, 'fail', 'synced')
        self.edit_state(lambda st: st.__setitem__('fail_verbs', {f'disable {BC}': True}))
        ch = self.add_c()
        self.assertEqual(ch['status'], 'failed')
        self.assertIn(BC, ch['result']['needs_person']['units'])
        self.assertEqual(self.actions().count(('restart', STATUS)), 2)
        self.assertEqual(ch['result']['steps'][-1]['name'], 'restart old files')
        self.assertTrue(ch['result']['steps'][-1]['ok'])

    def test_refused_install_never_removes_what_appeared_meanwhile(self):
        """A folder that appears between the preview and the run makes install.py refuse; the
        undo leaves it alone."""
        real_run = self.e.loginops.run

        def run(argv, timeout, env):
            if '--dry-run' not in argv:
                os.makedirs(self.tb('logins/c'))
                with open(self.tb('logins/c/someone-elses.txt'), 'w') as f:
                    f.write('x')
            return real_run(argv, timeout, env)
        self.e.loginops.run = run
        ch = self.add_c()
        self.assertEqual(ch['status'], 'failed')
        self.assertTrue(os.path.isfile(self.tb('logins/c/someone-elses.txt')))
        for p, b in self.before.items():
            self.assertEqual(self.raw(p), b, p)

    def test_letters_are_not_reused_after_a_failed_add(self):
        os.makedirs(self.tb('logins/c.failed-20211010T000000'))
        self.assertEqual(self.e.preview('add_login', {'label': 'Login C'})['plan']['login']['id'], 'd')

    def test_preflight_refuses_when_panel_down(self):
        self.edit_state(lambda st: st['units'][STATUS].update(active='failed', pid=0))
        ch = self.add_c()
        self.assertEqual(ch['status'], 'failed')
        self.assertTrue(ch['error'].startswith('Not applied'))
        self.assertFalse(os.path.exists(self.tb('logins/c')))
        self.assertEqual(self.actions(), [])


KILL_POINTS = ('record', 'originals', 'mid-install', 'install.py', 'check', 'peers', 'login switch',
               'flatten token', 'drop-in', 'admin drop-in', 'daemon-reload', 'enable browser', 'start browser',
               'restart status', 'verify')


class TestKilledMidAdd(AddLoginBase):
    """The service is killed at each step of an add. On the next start the change is "needs a
    person" naming the login record; Remove login then cleans up everything, even without a logins.json entry."""

    def kill_at(self, point):
        ops = self.e.loginops
        if point == 'mid-install':
            # install.py killed after writing peers.env/firing.env but before logins.json and A's config
            real_run = ops.run
            keep = {x: self.raw(x) for x in ('logins.json', 'config.json')}

            def run(argv, timeout, env):
                code, out, err = real_run(argv, timeout, env)
                if '--dry-run' not in argv:
                    for x, b in keep.items():
                        with open(self.tb(x), 'wb') as f:
                            f.write(b)
                    raise loginops.Killed('killed inside install.py')
                return code, out, err
            ops.run = run
        else:
            def hook(name):
                if name == point:
                    raise loginops.Killed(point)
            ops.kill_hook = hook

    def restart_service(self):
        self.e.stop()
        self.e._flock.close()
        from engine import Engine
        self.e = Engine(self.profile)
        self.e.loginops.listening = lambda: set()

    def test_kill_at_each_step_then_remove(self):
        for point in KILL_POINTS:
            with self.subTest(point=point):
                ch = self.e.preview('add_login', {'label': f'Login {point[:10].strip()}'.replace('-', ' ')})
                lid = ch['plan']['login']['id']
                self.kill_at(point)
                with self.assertRaises(loginops.Killed):
                    self.e.confirm(ch['id'], ch['preview_hash'])
                self.restart_service()
                ch = self.e.get(ch['id'])
                self.assertEqual(ch['status'], 'failed')
                text = ch['result']['needs_person']['text']
                self.assertIn(f'admin/logins/{lid}.json', text)
                self.assertIn('To undo: Remove login', text)
                rec = loginops.read_record(self.profile, lid)
                self.assertEqual(rec['state'], 'interrupted')
                self.assertIn(f'tvbridge-browser@{lid}.service', ch['result']['needs_person']['units'])
                self.assertIn(lid, [x['id'] for x in self.e.overview()['login_setup']['incomplete']])
                rm = self.e.preview('remove_login', {'login': lid})
                self.assertEqual(rm['plan']['blockers'], [], rm['plan']['blockers'])
                rm = self.e.confirm(rm['id'], rm['preview_hash'], rec['label'])
                self.assertEqual(rm['status'], 'applied', (point, rm.get('error'), rm['result']['steps']))
                for p, b in self.before.items():
                    self.assertEqual(self.raw(p), b, (point, p))
                with open(self.admin_dropin, 'rb') as f:
                    self.assertEqual(f.read(), self.admin_before, point)
                for x in (self.tb('logins', lid), self.tb('logins', f'manual-{lid}.env'),
                          os.path.join(self.units, f'tvbridge-daemon@{lid}.service.d'),
                          os.path.join(self.browser, f'profile-{lid}')):
                    self.assertFalse(os.path.exists(x), (point, x))
                self.assertEqual(self.originals_left(), [], point)
                st = self.read_state()
                for u in (f'tvbridge-browser@{lid}.service',):
                    self.assertEqual(st['units'].get(u, {}).get('active', 'inactive'), 'inactive', point)
                    self.assertEqual(st.get('unit_files', {}).get(u, 'disabled'), 'disabled', point)
                self.assertEqual(loginops.read_record(self.profile, lid)['state'], 'removed')
                self.assertNotIn(lid, [x['id'] for x in self.e.overview()['login_setup']['incomplete']])
        self.assert_no_secrets_in_records()


class TestTokenFiles(AddLoginBase):
    def test_only_read_and_manual_tokens_from_files(self):
        from tree import token_from_files
        self.add_c()
        self.assertTrue(token_from_files(self.profile, 'TVBRIDGE_READ_TOKEN_C'))
        self.assertTrue(token_from_files(self.profile, 'TVBRIDGE_MANUAL_TOKEN_C', 'c'))
        self.assertIsNone(token_from_files(self.profile, 'TVBRIDGE_TOKEN_C'))          # order tokens never
        self.assertIsNone(token_from_files(self.profile, 'TVBRIDGE_TOKEN_B', 'b'))
        self.assertIsNone(token_from_files(self.profile, 'TVBRIDGE_MANUAL_TOKEN_C', '../c'))
        off = Profile(dict(self.profile.data, token_files=False))
        self.assertIsNone(token_from_files(off, 'TVBRIDGE_READ_TOKEN_C'))


class TestLoginProfiles(unittest.TestCase):
    BASE = {'mode': 'sandbox', 'tvbridge_root': '/x/tb', 'soup_state': '/x/soup', 'admin_state': '/x/admin',
            'status_url': 'http://127.0.0.1:8779/api/status', 'port': 8781, 'user': 'trader', 'public_host': 'h',
            'restarter': 'systemd', 'order_adapter': 'dry-run', 'strategies': [],
            'systemd': {'systemctl': [os.path.join(os.path.dirname(HERE), 'tools', 'fake_systemctl.py')],
                        'status_api_url': 'http://127.0.0.1:1/api/status', 'logins_override': {}}}
    LS = {'unit_dir': '/x/units', 'browser_profile_dir': '/x/browser/profile'}

    def test_sandbox_never_points_at_real_units_or_profiles(self):
        Profile(dict(self.BASE, login_setup=self.LS))
        for bad in ({'unit_dir': REAL_UNITS}, {'unit_dir': os.path.join(REAL_UNITS, 'x')},
                    {'browser_profile_dir': os.path.expanduser('~/.local/share/decrypt-os/tvbridge-browser/profile')},
                    {'unit_dir': 'relative'}, {'install_script': '/x/other.py'}, {'signin_wait_s': 0}):
            with self.assertRaises(ProfileError, msg=str(bad)):
                Profile(dict(self.BASE, login_setup=dict(self.LS, **bad)))
        with self.assertRaises(ProfileError):
            Profile(dict(self.BASE, restarter='simulated', systemd={}, login_setup=self.LS))
        with self.assertRaises(ProfileError):
            Profile(dict(self.BASE, token_files='yes'))

    def test_live_unit_dir_is_the_users(self):
        live = dict(self.BASE, mode='live', systemd={'systemctl': ['/usr/bin/systemctl'],
                                                      'status_api_url': 'http://127.0.0.1:8779/api/status'})
        home = os.path.expanduser('~')
        live['user'] = __import__('getpass').getuser()
        Profile(dict(live, login_setup={'unit_dir': os.path.join(home, '.config/systemd/user'),
                                        'browser_profile_dir': os.path.join(home, '.local/share/decrypt-os/tvbridge-browser/profile')}))
        with self.assertRaises(ProfileError):
            Profile(dict(live, login_setup=self.LS))

    def test_parse_install_output(self):
        out = ('tvbridge add-login plan (nothing is changed): login c, DevTools 9558, loopback 8780\n'
               '  backup   /t/config.json.bak-20210101T000000Z  copy of the primary config config.json, mode 0600\n'
               '  create   /t/logins/c                          directory, mode 0700\n'
               '\n--- /t/logins/c/config.json ---\n  create   /x  not an action\n')
        head, actions = loginops.parse_install(out)
        self.assertEqual(head, {'id': 'c', 'devtools': 9558, 'listen': 8780})
        self.assertEqual(loginops.norm_actions(actions), [['backup', '/t/config.json.bak-<UTC>'],
                                                          ['create', '/t/logins/c']])

    def test_systemctl_only_for_the_logins_units(self):
        class R:
            calls = []

            def sc(self, *a, timeout=None):
                self.calls.append(a)
                return 0, '', ''
        ops = loginops.LoginSetup.__new__(loginops.LoginSetup)
        ops.restarter = R()
        ops.sc('c', 'start', BC)
        for verb, unit in (('start', DA), ('restart', BC), ('start', 'tvbridge-browser@b.service'),
                           ('stop', 'example-soup.service'), ('enable', FIRING), ('start', 'tvbridge-browser@C.service')):
            with self.assertRaises(PermissionError, msg=f'{verb} {unit}'):
                ops.sc('c', verb, unit)
        with self.assertRaises(PermissionError):
            ops.sc('../x', 'daemon-reload')
        self.assertEqual(R.calls, [('start', BC)])


if __name__ == '__main__':
    unittest.main()
