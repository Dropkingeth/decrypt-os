"""Restarter tests. The real restart path runs against a FAKE systemctl (tools/fake_systemctl.py) and fake
loopback /health and /api/status endpoints; nothing here calls the real systemctl or reads live state.

    cd admin && TMPDIR=$HOME/.cache PYTHONDONTWRITEBYTECODE=1 \
        python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'app'))

import restarter as R  # noqa: E402
from test_admin import KEYS, SECOND_LOGIN_DAEMON, Base  # noqa: E402
from tree import Profile, ProfileError  # noqa: E402

COPY = os.path.dirname(os.path.dirname(os.path.abspath(R.__file__)))   # the tree app/ was imported from
FAKE = os.path.join(COPY, 'tools', 'fake_systemctl.py')
DA, DB, FIRING, STATUS = 'tvbridge-daemon.service', SECOND_LOGIN_DAEMON, R.FIRING, R.STATUS
TOKENS = {'TVBRIDGE_READ_TOKEN_A': 'ra-' + 'x' * 30, 'TVBRIDGE_READ_TOKEN_B': 'rb-' + 'y' * 30}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class FakeDaemons:
    """Loopback stand-ins for the two daemons' GET /health/<read token> and the panel's GET /api/status.
    Answers come from the fake systemctl's state file, so a restart there changes what they say."""

    def __init__(self, state_path):
        self.state_path = state_path
        self.servers = {}
        for name, unit, tok in (('a', DA, TOKENS['TVBRIDGE_READ_TOKEN_A']),
                                ('b', DB, TOKENS['TVBRIDGE_READ_TOKEN_B']), ('status', STATUS, None)):
            srv = ThreadingHTTPServer(('127.0.0.1', 0), self._handler(unit, tok))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers[name] = srv

    def url(self, name):
        return f'http://127.0.0.1:{self.servers[name].server_address[1]}'

    def close(self):
        for s in self.servers.values():
            s.shutdown()
            s.server_close()

    def _handler(self, unit, tok):
        state_path = self.state_path

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                with open(state_path) as f:
                    u = json.load(f)['units'].get(unit, {})
                if u.get('active') != 'active' or u.get('mode') in ('waiting', 'stale', 'crash'):
                    self.close_connection = True   # not listening yet (waiting for sign-in) or gone
                    return
                if tok is None:
                    body = {'ok': True} if self.path == '/api/status' else None
                elif self.path == f'/health/{tok}':
                    synced = u.get('mode') == 'synced'
                    body = {'health': {'ok': synced,
                                       'problems': [] if synced else ['not_synced_for_current_connection'],
                                       'page': {'state': 'authorized', 'synced': synced}}}
                else:
                    body = None
                data = json.dumps(body or {'error': 'not found'}).encode()
                self.send_response(200 if body else 404)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        return H


class SystemdBase(Base):
    """Base with restarter "systemd" wired to the fake systemctl and the fake endpoints."""

    def profile_extra(self):
        self.state_path = os.path.join(self.tmp, 'fake-systemctl.json')
        fcfg = os.path.join(self.tmp, 'tree/tvbridge/firing.json')
        with open(fcfg) as f:
            fdir = json.load(f)['firing_dir']
        self.firing_status = os.path.join(fdir, 'status.json')
        units = {DA: 101, DB: 102, FIRING: 103, STATUS: 104}
        self.write_state({
            'units': {u: {'active': 'active', 'sub': 'running', 'pid': pid, 'nrestarts': 0, 'result': 'success',
                          'mode': 'ok' if u == FIRING else 'synced'} for u, pid in units.items()},
            'on_restart': {}, 'on_start': {}, 'calls': [], 'next_pid': 5000,
            'firing': {'config': fcfg, 'status': self.firing_status}})
        self.fake = FakeDaemons(self.state_path)
        self.guard = {}
        for lg in ('a', 'b'):
            self.guard[lg] = os.path.join(self.tmp, f'guard-{lg}')
            os.makedirs(self.guard[lg])
            self.set_guard(lg, 'in')
        self._env = {k: os.environ.get(k) for k in TOKENS}
        os.environ.update(TOKENS)
        return {'restarter': 'systemd', 'systemd': {
            'systemctl': [sys.executable, FAKE, '--state', self.state_path],   # no exec bit needed
            'status_api_url': self.fake.url('status') + '/api/status',
            'logins_override': {lg: {'bridge_url': self.fake.url(lg), 'guard_state_dir': self.guard[lg]}
                                for lg in ('a', 'b')},
            'daemon_wait_s': 1.5, 'firing_wait_s': 1.5, 'status_wait_s': 1.0,
            'poll_s': 0.05, 'firing_fresh_s': 5, 'command_timeout_s': 20}}

    def tearDown(self):
        super().tearDown()
        self.fake.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ----- fake controls -----
    def read_state(self):
        with open(self.state_path) as f:
            return json.load(f)

    def write_state(self, st):
        with open(self.state_path, 'w') as f:
            json.dump(st, f)

    def edit_state(self, fn):
        st = self.read_state()
        fn(st)
        self.write_state(st)

    def on_restart(self, unit, *outcomes):
        self.edit_state(lambda st: st['on_restart'].__setitem__(unit, list(outcomes)))

    def on_start(self, unit, *outcomes):
        self.edit_state(lambda st: st['on_start'].__setitem__(unit, list(outcomes)))

    def set_mode(self, unit, mode):
        self.edit_state(lambda st: st['units'][unit].__setitem__('mode', mode))

    def set_guard(self, login, how, age=0):
        now = time.time() - age
        if how == 'in':
            g = {'state': 'ok', 'reason': 'logged_in', 'checked_at': iso(now), 'logged_out_since': None}
        else:
            g = {'state': 'logged_out_waiting', 'reason': 'weekend_maintenance_hold', 'checked_at': iso(now),
                 'logged_out_since': iso(now - 3600)}
        with open(os.path.join(self.guard[login], 'status.json'), 'w') as f:
            json.dump(g, f)

    def actions(self):
        """Every state-changing systemctl call, in order (show/is-active reads left out)."""
        return [(c[0], c[1]) for c in self.read_state()['calls'] if c[0] not in ('show', 'is-active')]

    def clear_calls(self):
        self.edit_state(lambda st: st.__setitem__('calls', []))

    # ----- assertions -----
    def assert_full_apply(self, ch, units):
        self.assertEqual(ch['status'], 'applied', (ch.get('error'), ch['result']))
        names = [s['name'] for s in ch['result']['steps']]
        self.assertEqual(names[-5:], ['preflight', 'backup', 'write', 'restart', 'verify'], names)
        self.assertTrue(all(s['ok'] for s in ch['result']['steps']), ch['result']['steps'])
        manifest = os.path.join(self.profile.st('backups', ch['id']), 'manifest.json')
        self.assertTrue(os.path.isfile(manifest))
        self.assertEqual(self.actions(), units)
        entry = self.e.audit.read(1)[0]
        self.assertEqual((entry['change_id'], entry['status']), (ch['id'], 'applied'))
        self.assertEqual([(r['phase'], r['unit']) for r in entry['restarts']],
                         [('apply', u) for _, u in units])
        self.assertTrue(self.e.audit.verify_chain())
        self.assertFalse(self.read_state().get('soup_touched'))


def seq(*daemons, status=True):
    return [('stop', FIRING)] + [('restart', d) for d in daemons] + [('start', FIRING)] + \
        ([('restart', STATUS)] if status else [])


class TestSequenceAllTypes(SystemdBase):
    """Each structural type end-to-end: wait for flat, backup, write, restart (fake), verify, audit."""

    def test_size_cap_waits_for_flat_then_restarts(self):
        # account-d is on login B, but firing stops for the login A restart, so it must be flat too.
        self.set_status({'account-a': 0, 'account-b': 0, 'account-c': 0, 'account-d': 1, 'account-e': 0})
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'waiting_flat')
        self.assertIn('account-d', ch['eta']['waiting_on'])
        self.assertEqual([r['unit'] for r in ch['plan']['restarts']], [DA, FIRING, STATUS])
        self.e.tick()
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.jload('config.json')['followers'][0]['max_contracts'], 10)
        self.set_status({k: 0 for k in KEYS})
        self.e.tick()
        ch = self.e.get(ch['id'])
        self.assert_full_apply(ch, seq(DA))
        self.assertEqual(self.jload('config.json')['followers'][0]['max_contracts'], 6)
        self.assertIn('synced with Tradovate', ch['result']['steps'][-2]['detail'])

    def test_lane_off_and_on(self):
        ch = self.do('set_enabled', {'account': 'account-d', 'enabled': False})
        self.assert_full_apply(ch, seq())   # firing.json only: no daemon restarts
        self.assertFalse(self.jload('firing.json')['accounts']['account-d']['enabled'])
        self.clear_calls()
        ch = self.do('set_enabled', {'account': 'account-d', 'enabled': True})
        self.assert_full_apply(ch, seq())
        self.assertTrue(self.jload('firing.json')['accounts']['account-d']['enabled'])

    def test_reorder(self):
        ch = self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']})
        self.assert_full_apply(ch, seq())
        with open(self.firing_status) as f:   # the "running" firing service picked up the new order
            self.assertEqual(json.load(f)['firing_order'], ['account-a', 'account-e', 'account-b', 'account-c', 'account-h', 'account-d'])

    def test_move_to_not_assigned_and_back(self):
        ch = self.do('move_account', {'account': 'account-d', 'to_webhook': None})
        self.assert_full_apply(ch, seq())
        self.assertNotIn('account-d', self.jload('firing.json')['firing_order'])
        self.clear_calls()
        ch = self.do('move_account', {'account': 'account-d', 'to_webhook': 'main', 'index': 0})
        self.assert_full_apply(ch, seq())
        self.assertEqual(self.jload('firing.json')['firing_order'][1], 'account-d')

    def test_add_account_starts_paused(self):
        ch = self.do('add_account', {'login': 'b', 'key': 'account-g', 'label': 'Account G', 'account_name': 'DEMOB0042',
                                     'prop_profile': 'cross-account-window', 'webhook': 'main'})
        self.assert_full_apply(ch, seq(DB))
        self.assertEqual(self.jload('logins/b/config.json')['followers'][-1]['key'], 'account-g')
        self.assertTrue(os.path.isdir(self.tb('logins/b/journal-account-g')))
        self.assertEqual(self.acct('account-g')['pause']['set_by'], 'admin')
        self.assertEqual(self.jload('firing.json')['firing_order'][-1], 'account-g')

    def test_retire_keeps_journals(self):
        self.set_status({'account-a': 0, 'account-b': 0, 'account-c': 0, 'account-d': -1, 'account-e': 0})
        ch = self.do('retire_account', {'account': 'account-d'})
        self.assertEqual(ch['status'], 'waiting_flat')
        self.assertTrue(os.path.exists(self.tb('logins/b/ENTRIES-DISABLED-account-d')))
        self.assertEqual(self.actions(), [])
        self.set_status({k: 0 for k in KEYS})
        self.e.tick()
        ch = self.e.get(ch['id'])
        self.assert_full_apply(ch, seq(DB))
        self.assertNotIn('account-d', [f['key'] for f in self.jload('logins/b/config.json')['followers']])
        self.assertNotIn('account-d', self.jload('firing.json')['firing_order'])
        self.assertTrue(os.path.isdir(self.tb('logins/b/journal-account-d')))
        self.assertTrue(os.path.exists(self.tb('logins/b/ENTRIES-DISABLED-account-d')))

    def test_rollback(self):
        before = self.raw('config.json')
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assert_full_apply(ch, seq(DA))
        self.clear_calls()
        rb = self.e.rollback_preview(ch['id'])
        self.assertFalse(rb['plan']['blockers'])
        rb = self.e.confirm(rb['id'], rb['preview_hash'])
        self.assert_full_apply(rb, seq(DA))
        self.assertEqual(self.raw('config.json'), before)
        self.assertEqual(self.e.get(ch['id'])['status'], 'rolled_back')

    def test_two_logins_one_at_a_time(self):
        # add on login A, then a cap on login B's account: each restarts only its own daemon
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assert_full_apply(ch, seq(DB))
        self.assertEqual(self.e.overview()['restarter'], 'systemd')


class TestFailures(SystemdBase):
    def test_restart_failure_restores_and_reruns(self):
        before = self.raw('config.json')
        self.on_restart(DA, 'fail', 'synced')
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertTrue(ch['result']['rolled_back'])
        self.assertNotIn('needs_person', ch['result'])
        self.assertEqual(self.raw('config.json'), before)
        # first run stops at the daemon (firing left stopped), the re-run on the old files completes
        self.assertEqual(self.actions(), [('stop', FIRING), ('restart', DA)] + seq(DA))
        names = [s['name'] for s in ch['result']['steps']]
        self.assertEqual(names, ['preflight', 'backup', 'write', 'restart', 'rollback', 'restart old files'])
        self.assertTrue(ch['result']['steps'][-1]['ok'])
        entry = self.e.audit.read(1)[0]
        self.assertEqual({r['phase'] for r in entry['restarts']}, {'apply', 'rollback'})
        self.assertEqual(ch['eta']['text'], 'Failed and rolled back.')

    def test_rerun_fails_needs_a_person(self):
        before = self.raw('logins/b/config.json')
        self.on_restart(DB, 'fail')   # sticks: the re-run on the old files fails too
        ch = self.do('add_account', {'login': 'b', 'key': 'account-g', 'label': 'Account G', 'account_name': 'DEMOB0042',
                                     'prop_profile': 'cross-account-window', 'webhook': None})
        self.assertEqual(ch['status'], 'failed')
        self.assertEqual(self.raw('logins/b/config.json'), before)
        self.assertFalse(os.path.exists(self.tb('logins/b/journal-account-g')))
        np = ch['result']['needs_person']
        self.assertEqual(np['units'], [DB, FIRING])
        self.assertTrue(ch['eta']['text'].startswith('Needs a person'))
        self.assertTrue(np['firing_stopped'])
        self.assertIn('followers will not fire until this is fixed', np['text'])
        self.assertEqual(self.actions(), [('stop', FIRING), ('restart', DB)] * 2)   # one re-run, no loop
        ov = self.e.overview()
        self.assertEqual([n['change_id'] for n in ov['needs_person']], [ch['id']])
        self.assertIn('followers will not fire until this is fixed', ov['needs_person'][0]['text'])
        self.assertTrue(ov['needs_person'][0]['firing_stopped'])
        self.assertEqual(self.e.audit.read(1)[0]['needs_person'], [DB, FIRING])
        self.e.tick()
        self.assertEqual(len(self.actions()), 4)   # nothing retries on its own

    def test_not_synced_in_time(self):
        self.on_restart(DA, 'unsynced', 'synced')
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('not synced within', ch['result']['steps'][3]['detail'])
        self.assertTrue(ch['result']['steps'][-1]['ok'])

    def test_daemon_exits_after_restart(self):
        self.on_restart(DA, 'crash', 'synced')
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('exited after the restart', ch['result']['steps'][3]['detail'])

    def test_firing_rejects_config(self):
        before = self.raw('firing.json')
        self.on_start(FIRING, 'bad_config', 'ok')
        ch = self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']})
        self.assertEqual(ch['status'], 'failed')
        self.assertEqual(self.raw('firing.json'), before)
        self.assertEqual(self.actions(), seq(status=False) + seq())
        self.assertTrue(ch['result']['rolled_back'])
        self.assertNotIn('needs_person', ch['result'])

    def test_firing_status_never_refreshes(self):
        self.on_start(FIRING, 'stale', 'ok')
        ch = self.do('set_enabled', {'account': 'account-d', 'enabled': False})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('firing', ch['result']['steps'][3]['detail'])

    def test_status_panel_restart_fails(self):
        self.on_restart(STATUS, 'fail', 'synced')
        ch = self.do('set_enabled', {'account': 'account-d', 'enabled': False})
        self.assertEqual(ch['status'], 'failed')
        self.assertTrue(self.jload('firing.json')['accounts']['account-d']['enabled'])
        self.assertEqual(self.actions(), seq() + seq())


class TestSignInWait(SystemdBase):
    """A daemon is never restarted while its login is logged out or not synced: the change waits."""

    def logged_out(self, *logins):
        for lg in logins:
            self.set_guard(lg, 'out')
            self.set_mode({'a': DA, 'b': DB}[lg], 'waiting')   # running, /health not answering

    def sign_in(self, lg):
        self.set_guard(lg, 'in')
        self.set_mode({'a': DA, 'b': DB}[lg], 'synced')

    def test_waits_for_signin_then_applies(self):
        self.logged_out('a', 'b')
        before = self.raw('logins/b/config.json')
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assertEqual(ch['status'], 'waiting_flat')
        self.assertEqual(ch['eta']['kind'], 'after_signin')
        self.assertTrue(ch['eta']['text'].startswith('Waits for Tradovate sign-in (Login B).'), ch['eta'])
        self.assertIn('after the next sign-in', ch['eta']['text'])
        self.assertEqual(ch['eta']['waiting_for_signin'], ['Login B'])
        self.e.tick()
        self.assertEqual(self.e.get(ch['id'])['status'], 'waiting_flat')
        self.assertEqual(self.raw('logins/b/config.json'), before)
        self.assertEqual(self.actions(), [])          # nothing restarted, nothing stopped
        self.sign_in('b')                             # login A stays logged out: not in this plan
        self.e.tick()
        ch = self.e.get(ch['id'])
        self.assert_full_apply(ch, seq(DB))
        self.assertEqual(self.jload('logins/b/config.json')['followers'][0]['max_contracts'], 3)

    def test_waits_for_both_signin_and_flat(self):
        self.logged_out('b')
        self.set_status({'account-a': 0, 'account-b': 1, 'account-c': 0, 'account-d': 0, 'account-e': 0})
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assertEqual(ch['eta']['kind'], 'after_signin')
        self.assertIn('account-b', ch['eta']['waiting_on'])
        self.sign_in('b')
        self.e.tick()
        ch = self.e.get(ch['id'])
        self.assertEqual((ch['status'], ch['eta']['kind']), ('waiting_flat', 'after_flat'))
        self.set_status({k: 0 for k in KEYS})
        self.e.tick()
        self.assertEqual(self.e.get(ch['id'])['status'], 'applied')

    def test_signed_in_but_not_synced_waits(self):
        self.set_mode(DA, 'unsynced')
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual((ch['status'], ch['eta']['kind']), ('waiting_flat', 'after_signin'))
        self.assertIn('Login A', ch['eta']['text'])
        self.assertEqual(self.actions(), [])

    def test_stale_guard_and_no_health_waits(self):
        self.set_guard('b', 'out', age=600)
        self.set_mode(DB, 'waiting')
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assertEqual((ch['status'], ch['eta']['kind']), ('waiting_flat', 'after_signin'))
        self.assertEqual(self.actions(), [])

    def test_firing_only_changes_apply_while_logged_out(self):
        self.logged_out('a', 'b')
        ch = self.do('set_enabled', {'account': 'account-d', 'enabled': False})
        self.assert_full_apply(ch, seq())             # no daemon restart, so no sign-in needed
        self.clear_calls()
        ch = self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']})
        self.assert_full_apply(ch, seq())

    def test_signin_wait_can_be_cancelled_over_http(self):
        from http.server import ThreadingHTTPServer as S
        import http.client
        import server
        self.logged_out('b')
        before = self.raw('logins/b/config.json')
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assertEqual(ch['status'], 'waiting_flat')
        httpd = S(('127.0.0.1', 0), server.make_handler(self.e, 'tok', 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = server.make_handler(self.e, 'tok', port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            c.request('POST', f'/api/changes/{ch["id"]}/cancel', '{}',
                      {'Host': f'127.0.0.1:{port}', 'Origin': f'http://127.0.0.1:{port}', 'X-Admin-Token': 'tok',
                       'Content-Type': 'application/json'})
            r = c.getresponse()
            body = json.loads(r.read())
            self.assertEqual((r.status, body['status']), (200, 'cancelled'))
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.sign_in('b')
        self.e.tick()
        self.assertEqual(self.e.get(ch['id'])['status'], 'cancelled')
        self.assertEqual(self.raw('logins/b/config.json'), before)
        self.assertEqual(self.actions(), [])

    def test_logout_between_check_and_restart_never_restarts_daemon(self):
        before = self.raw('logins/b/config.json')
        real = self.e.restarter.preflight

        def flip(units):  # the login logs out right after the checks passed (e.g. Friday 6 PM ET)
            out = real(units)
            self.logged_out('b')
            return out
        self.e.restarter.preflight = flip
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assertEqual(ch['status'], 'failed')
        self.assertTrue(ch['result']['rolled_back'])
        self.assertNotIn('needs_person', ch['result'])     # the daemon never restarted: still on the old files
        self.assertEqual(self.raw('logins/b/config.json'), before)
        self.assertNotIn(('restart', DB), self.actions())
        self.assertEqual(self.actions(), [('stop', FIRING)] + seq())   # restore run brings firing back

    def test_logout_during_daemon_restart_needs_a_person_after_signin(self):
        real = self.e.restarter.sc

        def sc(*args, **kw):
            out = real(*args, **kw)
            if args[:2] == ('restart', DB):
                self.logged_out('b')
            return out
        self.e.restarter.sc = sc
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('Tradovate logged out', ch['result']['steps'][3]['detail'])
        np = ch['result']['needs_person']
        self.assertEqual(np['units'], [DB, FIRING])
        self.assertIn('after sign-in', np['text'])
        self.assertTrue(np['firing_stopped'])
        self.assertIn('followers will not fire until this is fixed', np['text'])
        self.assertEqual(self.actions(), [('stop', FIRING), ('restart', DB), ('stop', FIRING)])  # never again


class TestPreflightAndGuards(SystemdBase):
    def test_missing_read_token_refuses_before_writing(self):
        os.environ.pop('TVBRIDGE_READ_TOKEN_A')
        before = self.raw('config.json')
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('TVBRIDGE_READ_TOKEN_A', ch['error'])
        self.assertEqual(self.raw('config.json'), before)
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.e.audit.read(1)[0]['status'], 'failed')

    def test_soup_is_never_restarted(self):
        self.e.restarter.p.data['soup_service'] = {'unit': 'example-soup.service'}   # Soup as its own unit
        units = [{'unit': 'example-soup.service', 'why': 'x', 'seconds': 1, 'login': None}]
        problems, _ = self.e.restarter.preflight(units)
        self.assertEqual(problems, ['Soup is never restarted by this page.'])
        res = self.e.restarter.restart(units, 'test')
        self.assertEqual([(r['unit'], r['ok']) for r in res], [('example-soup.service', False)])
        self.assertEqual(self.actions(), [])
        self.assertFalse(self.read_state().get('soup_touched'))

    def test_firing_down_now_is_refused(self):
        self.edit_state(lambda st: st['units'][FIRING].update(active='failed', sub='failed', pid=0))
        ch = self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn(FIRING, ch['error'])
        self.assertEqual(self.actions(), [])

    def test_preflight_crash_fails_closed(self):
        def boom(units):
            raise OSError('no bus')
        self.e.restarter.preflight = boom
        before = self.raw('config.json')
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('no bus', ch['error'])
        self.assertEqual(self.raw('config.json'), before)
        self.assertEqual(self.actions(), [])

    def test_pause_never_restarts(self):
        self.assertEqual(self.do('pause', {'account': 'account-d', 'reason': 'x'})['status'], 'applied')
        self.assertEqual(self.read_state()['calls'], [])

    def test_progress_saved_while_restarting(self):
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        log = self.e.get(ch['id'])['result']['restart_log']
        self.assertEqual([(r['action'], r['unit'], r['ok']) for r in log],
                         [('stop', FIRING, True), ('restart', DA, True), ('start', FIRING, True),
                          ('restart', STATUS, True)])


class TestReviewFixes(SystemdBase):
    """Fixes from an independent review."""

    def shift_time(self, seconds):
        """Pretend `seconds` passed for start-limit purposes (this page's log and the fake's own count)."""
        path = self.profile.st('restarts.jsonl')
        with open(path) as f:
            rows = [json.loads(x) for x in f if x.strip()]
        with open(path, 'w') as f:
            for r in rows:
                r['at'] -= seconds
                f.write(json.dumps(r) + '\n')
        self.edit_state(lambda st: st.__setitem__('starts', {u: [t - seconds for t in ts]
                                                             for u, ts in st.get('starts', {}).items()}))

    # 1. systemd start limits
    def test_queued_changes_respect_start_limit(self):
        self.set_status({'account-a': 0, 'account-b': 1, 'account-c': 0, 'account-d': 0, 'account-e': 0})
        ids = [self.do('touch_follower', {'account': 'account-b', 'n': n})['id'] for n in (6, 5, 4, 3)]
        self.set_status({k: 0 for k in KEYS})
        self.e.tick()   # one restarting change per unit per pass
        self.assertEqual([self.e.get(i)['status'] for i in ids], ['applied'] + ['waiting_flat'] * 3)
        self.e.tick()
        self.assertEqual([self.e.get(i)['status'] for i in ids], ['applied'] * 2 + ['waiting_flat'] * 2)
        self.e.tick()   # daemon A: 2 of 3 starts used in 10 min, 1 left < 2 needed (apply + restore)
        ch = self.e.get(ids[2])
        self.assertEqual((ch['status'], ch['eta']['kind']), ('waiting_flat', 'cooldown'))
        self.assertTrue(ch['eta']['text'].startswith('Cooling down: restart limit'), ch['eta']['text'])
        self.assertEqual(self.actions().count(('restart', DA)), 2)
        self.shift_time(700)
        self.e.tick()
        self.e.tick()
        self.assertEqual([self.e.get(i)['status'] for i in ids], ['applied'] * 4)
        self.assertEqual(self.jload('config.json')['followers'][0]['max_contracts'], 3)
        self.assertNotIn('start-limit-hit', json.dumps(self.read_state()['units']))

    def test_unit_with_too_small_a_limit_never_starts(self):
        self.edit_state(lambda st: st.__setitem__('limits', {DA: [1, 600]}))
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual((ch['status'], ch['eta']['kind']), ('waiting_flat', 'cooldown'))
        self.assertEqual(self.actions(), [])

    def test_fake_enforces_start_limit_like_systemd(self):
        self.edit_state(lambda st: st.__setitem__('limits', {DA: [1, 600]}))
        first = self.e.restarter.restart([{'unit': DA}], 'direct-1')
        self.assertTrue(all(r['ok'] for r in first), first)
        second = self.e.restarter.restart([{'unit': DA}], 'direct-2')   # the 2nd start in 10 min is refused
        self.assertEqual(self.read_state()['units'][DA]['result'], 'start-limit-hit')
        self.assertFalse(all(r['ok'] for r in second))

    # 2. never restart a daemon that is not synced right before its restart; restore rules
    def test_apply_needs_synced_right_before_restart(self):
        real = self.e.restarter.preflight

        def flip(units):
            out = real(units)
            self.set_mode(DA, 'unsynced')   # guard still says logged in: not synced is enough to refuse
            return out
        self.e.restarter.preflight = flip
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertNotIn(('restart', DA), self.actions())
        self.assertNotIn('needs_person', ch['result'])
        self.assertEqual(self.actions(), [('stop', FIRING)] + seq())

    def test_restore_refuses_stale_or_missing_guard(self):
        for how in ('stale', 'missing'):
            with self.subTest(how=how):
                self.clear_calls()
                self.edit_state(lambda st: st.__setitem__('starts', {}))
                self.on_restart(DA, 'unsynced')
                self.set_mode(DA, 'synced')
                self.edit_state(lambda st: st['units'][FIRING].update(active='active', sub='running', pid=103,
                                                                       mode='ok'))
                self.set_guard('a', 'in')
                real = self.e.restarter._restart_daemon

                def hook(unit, rec, real=real, how=how):
                    if how == 'stale':
                        self.set_guard('a', 'in', age=600)
                    else:
                        os.remove(os.path.join(self.guard['a'], 'status.json'))
                    return real(unit, rec)
                self.e.restarter._restart_daemon = hook
                ch = self.do('touch_follower', {'account': 'account-b', 'n': 6 if how == 'stale' else 5})
                self.e.restarter._restart_daemon = real
                self.assertEqual(ch['status'], 'failed')
                self.assertEqual(self.actions().count(('restart', DA)), 1)   # never restarted again
                np = ch['result']['needs_person']
                self.assertEqual(np['units'], [DA, FIRING])
                self.assertIn('stale' if how == 'stale' else 'no login-guard reading', np['text'] + json.dumps(
                    ch['result']['restart_log']))

    def test_down_reading_includes_guard(self):
        self.set_guard('b', 'out')
        self.edit_state(lambda st: st['units'][DB].update(active='failed', sub='failed', pid=0))
        verdict, detail, _ = self.e.restarter.daemon_reading(DB)
        self.assertEqual(verdict, 'down')
        self.assertIn('weekend maintenance hold', detail)

    # 4. interrupted mid-sequence
    def test_interrupted_change_needs_a_person_on_start(self):
        ch = self.e.preview('touch_follower', {'account': 'account-b', 'n': 6})
        ch = self.e.store.get(ch['id'])
        ch['status'] = 'applying'
        ch['result'] = {'ok': None, 'steps': [], 'rolled_back': False}
        self.e.store.put(ch)
        os.makedirs(self.profile.st('backups', ch['id']))
        self.edit_state(lambda st: st['units'][FIRING].update(active='inactive', sub='dead', pid=0))
        self.e.stop()
        self.e._flock.close()
        from engine import Engine
        self.e = Engine(self.profile)
        ch = self.e.get(ch['id'])
        self.assertEqual(ch['status'], 'failed')
        np = ch['result']['needs_person']
        self.assertIn('stopped while this change was applying', np['text'])
        self.assertIn('followers will not fire until this is fixed', np['text'])
        self.assertEqual(np['backup'], self.profile.st('backups', ch['id']))
        self.assertIn(FIRING, np['units'])
        self.assertEqual(self.actions(), [])   # nothing restored or restarted automatically
        self.assertEqual(self.e.overview()['needs_person'][0]['change_id'], ch['id'])
        self.assertEqual(self.e.audit.read(1)[0]['status'], 'failed')

    # 5. lanes refused at start
    def test_newly_refused_lane_fails_and_restores(self):
        before = self.raw('firing.json')
        self.on_start(FIRING, 'refuse:account-d', 'ok')
        ch = self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('lane account-d refused at start', ch['result']['steps'][4]['detail'])
        self.assertEqual(self.raw('firing.json'), before)

    def test_lane_refused_before_and_after_is_not_new(self):
        self.edit_state(lambda st: st['units'][FIRING].__setitem__('refused', ['account-e']))
        self.on_start(FIRING, 'refuse:account-e')
        ch = self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']})
        self.assertEqual(ch['status'], 'applied', ch['result']['steps'])

    def test_just_added_paused_account_may_be_refused(self):
        self.on_start(FIRING, 'refuse:account-g')
        ch = self.do('add_account', {'login': 'b', 'key': 'account-g', 'label': 'Account G', 'account_name': 'DEMOB0042',
                                     'prop_profile': 'cross-account-window', 'webhook': 'main'})
        self.assertEqual(ch['status'], 'applied', ch['result']['steps'])
        self.assertIn('account-g', ch['result']['refused_lanes_after'])

    # 6. restore re-checks flat
    def test_restore_skips_daemons_when_not_flat(self):
        self.on_restart(DA, 'fail')
        real = self.e.restarter.restart

        def hook(units, cid, **kw):
            out = real(units, cid, **kw)
            if kw.get('phase') == 'apply':
                self.set_status({'account-a': 0, 'account-b': 0, 'account-c': 0, 'account-h': 0, 'account-d': 2, 'account-e': 0})
            return out
        self.e.restarter.restart = hook
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertEqual(self.actions().count(('restart', DA)), 1)
        np = ch['result']['needs_person']
        self.assertEqual(np['units'], [DA])
        self.assertIn('not flat at restore time (account-d)', np['text'])
        self.assertIn('The firing service is running.', np['text'])
        self.assertFalse(np['firing_stopped'])

    # 7. firing start never pulls in daemon A
    def test_firing_start_ignores_requirements(self):
        self.do('set_enabled', {'account': 'account-d', 'enabled': False})
        starts = [c for c in self.read_state()['calls'] if c[0] == 'start' and c[1] == FIRING]
        self.assertTrue(starts and all(c[3] == ['--job-mode=ignore-requirements'] for c in starts), starts)
        self.assertNotIn('firing_started_with_requirements', self.read_state())

    def test_firing_needs_daemon_a_active(self):
        self.edit_state(lambda st: st['units'][DA].update(active='inactive', sub='dead', pid=0))
        ch = self.do('set_enabled', {'account': 'account-d', 'enabled': False})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('tvbridge-daemon.service is not active', ch['error'])
        self.assertEqual(self.actions(), [])

    # 8. misleading waits
    def test_rejected_read_token_refuses_instead_of_waiting(self):
        os.environ['TVBRIDGE_READ_TOKEN_A'] = 'wrong-' + 'z' * 30
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertIn('rejected the read token TVBRIDGE_READ_TOKEN_A (HTTP 404)', ch['error'])
        self.assertEqual(self.actions(), [])

    def test_down_daemon_has_its_own_reason(self):
        self.edit_state(lambda st: st['units'][DB].update(active='failed', sub='failed', pid=0))
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assertEqual((ch['status'], ch['eta']['kind'], ch['eta']['reason']), ('waiting_flat', 'blocked', 'daemon_down'))
        self.assertIn('is down: needs a person', ch['eta']['text'])
        self.assertNotIn('sign-in', ch['eta']['text'])


class TestProfiles(unittest.TestCase):
    BASE = {'mode': 'sandbox', 'tvbridge_root': '/x/tb', 'soup_state': '/x/soup', 'admin_state': '/x/admin',
            'status_url': 'http://127.0.0.1:8779/api/status', 'port': 8781, 'user': 'u', 'public_host': 'h',
            'restarter': 'systemd', 'order_adapter': 'dry-run', 'strategies': []}
    FAKE_SD = {'systemctl': [FAKE, '--state', '/x/s.json'], 'status_api_url': 'http://127.0.0.1:1/api/status',
               'logins_override': {}}

    def test_sandbox_needs_a_fake_systemctl(self):
        Profile(dict(self.BASE, systemd=self.FAKE_SD))
        for argv in (['/usr/bin/systemctl'], ['/bin/systemctl'], 'systemctl', [], None):
            with self.assertRaises(ProfileError, msg=str(argv)):
                Profile(dict(self.BASE, systemd=dict(self.FAKE_SD, systemctl=argv)))
        with self.assertRaises(ProfileError):
            Profile(dict(self.BASE, systemd={'systemctl': [FAKE]}))   # no fake endpoints

    LIVE_SD = {'systemctl': ['/usr/bin/systemctl'], 'status_api_url': 'http://127.0.0.1:8779/api/status'}

    def test_live_needs_exact_systemctl_status_url_and_timeout(self):
        live = dict(self.BASE, mode='live')
        Profile(dict(live, systemd=self.LIVE_SD))
        for bad in ({}, {'systemctl': ['/bin/systemctl']}, {'systemctl': '/usr/bin/systemctl'},
                    {'systemctl': ['/usr/bin/systemctl', '--no-block']}, {'status_api_url': 'http://127.0.0.1:1/x'},
                    {'command_timeout_s': 150}):
            with self.assertRaises(ProfileError, msg=str(bad)):
                Profile(dict(live, systemd={**self.LIVE_SD, **bad} if bad else {}))

    def test_sandbox_argv_must_be_the_fake(self):
        with self.assertRaises(ProfileError):
            Profile(dict(self.BASE, systemd=dict(self.FAKE_SD, systemctl=[sys.executable, '/tmp/other.py'])))

    def test_live_refuses_overrides_and_unknown_restarters(self):
        live = dict(self.BASE, mode='live', systemd=self.LIVE_SD)
        Profile(live)
        with self.assertRaises(ProfileError):
            Profile(dict(live, systemd=dict(self.LIVE_SD, logins_override={'a': {'bridge_url': 'http://127.0.0.1:1'}})))
        with self.assertRaises(ProfileError):
            Profile(dict(live, restarter='ansible'))
        with self.assertRaises(ProfileError):
            Profile(dict(live, restarter='simulated'))   # unchanged: live writes need restarts

class FakeRun:
    """In-process systemctl for unit tests of the sequence logic (no subprocess at all)."""

    def __init__(self, status_path, order, fail=()):
        self.calls, self.pids, self.fail = [], {DA: 1, DB: 2, FIRING: 3, STATUS: 4}, set(fail)
        self.status_path, self.order = status_path, order

    def __call__(self, argv, timeout):
        args = [a for a in argv[2:] if not a.startswith('--')]
        verb, unit = args[0], args[1]
        if verb == 'show':
            active = 'inactive' if self.pids.get(unit) == 0 else 'active'
            if unit == FIRING and active == 'active':
                with open(self.status_path, 'w') as f:
                    json.dump({'pid': self.pids[unit], 'updated_at': int(time.time() * 1000),
                               'firing_order': self.order}, f)
            return 0, f'ActiveState={active}\nSubState=running\nMainPID={self.pids.get(unit, 0)}\nNRestarts=0\n' \
                      f'Result=success\n', ''
        self.calls.append((verb, unit))
        if unit in self.fail:
            self.pids[unit] = 0
            return 1, '', 'failed'
        self.pids[unit] = 0 if verb == 'stop' else self.pids.get(unit, 0) + 100
        return 0, '', ''


class FakeProfile:
    def __init__(self, root):
        self.root, self.mode = root, 'live'
        self.data = {'systemd': {'poll_s': 0.01, 'daemon_wait_s': 0.3,
                                 'firing_wait_s': 0.3, 'status_wait_s': 0.3}}

    def tb(self, *p):
        return os.path.join(self.root, 'tb', *p)

    def st(self, *p):
        return os.path.join(self.root, 'st', *p)


class TestRestarterUnit(unittest.TestCase):
    def test_guard_rules_match_panel(self):
        now = time.time()
        f = R.guard_says_logged_out
        self.assertTrue(f({'state': 'logged_out_waiting', 'reason': 'weekend_maintenance_hold',
                           'checked_at': iso(now)}, now)[0])
        self.assertTrue(f({'state': 'ok', 'logged_out_since': iso(now - 9), 'checked_at': iso(now)}, now)[0])
        self.assertFalse(f({'state': 'logged_out_waiting', 'checked_at': iso(now - 181)}, now)[0])
        self.assertFalse(f({'state': 'ok', 'reason': 'logged_in', 'checked_at': iso(now)}, now)[0])
        self.assertTrue(f({'state': 'needs_human', 'reason': 'login_form', 'checked_at': iso(now)}, now)[0])
        self.assertFalse(f({'state': 'needs_human', 'reason': 'daemon_start_cap_reached',
                            'checked_at': iso(now)}, now)[0])
        self.assertFalse(f(None, now)[0])

    def test_health_synced_rules(self):
        ok = {'health': {'problems': [], 'page': {'state': 'authorized', 'synced': True}}}
        self.assertTrue(R.health_synced(ok)[0])
        for bad in ({'health': {'problems': ['not_synced_for_current_connection'],
                                'page': {'state': 'authorized', 'synced': True}}},
                    {'health': {'problems': [], 'page': {'state': 'authorized', 'synced': False}}},
                    {'health': {'problems': [], 'page': {'state': 'waiting_for_app_login', 'synced': False}}},
                    {}, None):
            self.assertFalse(R.health_synced(bad)[0], bad)

    def test_classify_and_order(self):
        d, firing, status, refused = R.SystemdRestarter.classify([DB, DA, STATUS, 'example-soup.service'])
        self.assertEqual((d, firing, status, refused), ([DB, DA], True, True, ['example-soup.service']))
        d, firing, status, refused = R.SystemdRestarter.classify([STATUS])
        self.assertEqual((d, firing, status), ([], False, True))

    def make(self, fail=()):
        import shutil
        import tempfile
        root = tempfile.mkdtemp(prefix='tbr-')
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, 'tb', 'firing'))
        logins = [{'id': lg, 'daemon_unit': u, 'bridge_url': f'http://127.0.0.1:{port}',
                   'read_token_env': f'TOK_{lg.upper()}', 'token_env': f'ORDER_{lg.upper()}',
                   'guard_state_dir': os.path.join(root, 'g')} for lg, u, port in (('a', DA, 1), ('b', DB, 2))]
        with open(os.path.join(root, 'tb', 'logins.json'), 'w') as f:
            json.dump({'logins': logins}, f)
        order = ['lead', 'f1']
        with open(os.path.join(root, 'tb', 'firing.json'), 'w') as f:
            json.dump({'firing_order': order, 'firing_dir': os.path.join(root, 'tb', 'firing')}, f)
        run = FakeRun(os.path.join(root, 'tb', 'firing', 'status.json'), order, fail)
        urls = []

        def http(url):
            urls.append(url)
            if '/health/' in url:
                return 200, {'health': {'problems': [], 'page': {'state': 'authorized', 'synced': True}}}, None
            return 200, {}, None
        r = R.SystemdRestarter(FakeProfile(root), run=run, http_get=http,
                               environ={'TOK_A': 'a' * 30, 'TOK_B': 'b' * 30, 'ORDER_A': 'o' * 30})
        return r, run, urls

    def test_sequence_with_injected_fakes(self):
        r, run, urls = self.make()
        units = [{'unit': u} for u in (DB, DA, STATUS)]
        self.assertEqual(r.preflight(units)[0], [])
        res = r.restart(units, 'c1')
        self.assertTrue(all(x['ok'] for x in res), res)
        self.assertEqual(run.calls, [('stop', FIRING), ('restart', DB), ('restart', DA), ('start', FIRING),
                                     ('restart', STATUS)])
        self.assertTrue(all(u.endswith('/health/' + 'a' * 30) or u.endswith('/health/' + 'b' * 30)
                            or u.endswith('/api/status') for u in urls))   # read tokens only, never ORDER_A
        self.assertEqual(r.health(units), {DB: True, DA: True, FIRING: True, STATUS: True})

    def test_failed_daemon_stops_sequence_and_leaves_firing_stopped(self):
        r, run, _ = self.make(fail={DB})
        res = r.restart([{'unit': u} for u in (DB, DA, STATUS)], 'c1')
        self.assertEqual(run.calls, [('stop', FIRING), ('restart', DB)])
        self.assertEqual([(x['unit'], x['ok']) for x in res],
                         [(FIRING, True), (DB, False), (DA, None), (STATUS, None), (FIRING, False)])

    def test_loopback_only(self):
        self.assertEqual(R.loopback_get('http://example.com/health/x')[2], 'not_loopback')


if __name__ == '__main__':
    unittest.main()
