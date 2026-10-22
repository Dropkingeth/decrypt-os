#!/usr/bin/env python3
"""Multi-login tests for the bridge status panel (tvbridge 1.1.0). Offline: no network, no
systemctl, no journalctl. Login A is test_followers' Stage C scenario (all three accounts short 2
with break-even stops, the fixture trade); login B's daemon replies, config, guard status and guard
events come from fixtures/logins/ (placeholders: "{root}" = the scenario's temp home, "@iso-N" /
"@ms-N" = N seconds before now).

Run: python3 -m unittest test_logins -v
Static previews (no server, no network): python3 test_logins.py --preview [/tmp/tvbridge-panel-preview]
"""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

import server
import test_followers as tf
from test_followers import Env, row

HERE = Path(__file__).resolve().parent
FIX = HERE / 'fixtures' / 'logins'
TOKEN_B = 'b' * 40
READ_B = 'r' * 40         # login B's read token (GET /health and GET /login only)
READ_A = 'q' * 40
LOGIN_SCENARIOS = ('two_logins', 'b_logged_out', 'b_unreachable', 'b_zero_assigned')


def setUpModule():
    tf.setUpModule()


def tearDownModule():
    tf.tearDownModule()


def materialize(value, root, now):
    if isinstance(value, dict):
        return {k: materialize(v, root, now) for k, v in value.items()}
    if isinstance(value, list):
        return [materialize(v, root, now) for v in value]
    if isinstance(value, str):
        m = re.fullmatch(r'@(iso|ms)-(\d+)', value)
        if m:
            t = now - int(m.group(2))
            return Env.iso(t) if m.group(1) == 'iso' else int(t * 1000)
        return value.replace('{root}', str(root))
    return value


class LoginEnv(Env):
    """test_followers' Env plus a login registry and login B's daemon, config and guard."""

    def __init__(self, tmp):
        super().__init__(tmp)
        self.b = {'health': None, 'login': None, 'followers': None}
        self.a_login = self.fixture('a-login.json')
        self.unit_states = {}
        self.calls = []
        self.sent = []          # (port, endpoint, token) of every daemon read

    def fixture(self, name):
        return materialize(json.loads((FIX / name).read_text()), self.root, self.now)

    def fixture_lines(self, name):
        return [materialize(json.loads(l), self.root, self.now) for l in (FIX / name).read_text().splitlines() if l]

    def registry(self, name='registry-two.json', where=None, read_envs=None):
        """read_envs: {'b': 'TVBRIDGE_READ_TOKEN_B'} adds `read_token_env` to those entries."""
        data = self.fixture(name)
        for e in data['logins']:
            if e['id'] in (read_envs or {}):
                e['read_token_env'] = read_envs[e['id']]
        self.write_json(where or 'logins.json', data)

    def login_b(self, health='b-health-ok.json', login='b-login-connected.json', followers='b-followers.json',
                guard='b-guard-ok.json', config='b-config.json'):
        self.b = {'health': self.fixture(health) if health else None, 'login': self.fixture(login) if login else None,
                  'followers': self.fixture(followers) if followers else None}
        cfg = self.fixture(config)
        self.write_json('logins/b/config.json', cfg)
        self.write_json('guard-b/status.json', self.fixture(guard))
        self.write_lines('guard-b/events.jsonl', self.fixture_lines('b-guard-events.jsonl'))

    def fake_daemon_get(self, base_url, endpoint, tok, timeout=3):
        port = int(base_url.rsplit(':', 1)[1])
        self.calls.append((port, endpoint))
        self.sent.append((port, endpoint, tok))
        if port == 8777 and endpoint == 'login':
            value = self.a_login
        elif port == 8778:
            value = self.b.get(endpoint)
        else:
            value = None
        return (value, None) if value is not None else (None, 'URLError')

    def fake_unit_state(self, name):
        if name == server.FIRING_UNIT:
            return dict(self.firing_unit)
        return dict(self.unit_states.get(name, {'load': 'loaded', 'active': 'active', 'sub': 'running',
                                                'result': 'success'}))

    def patches(self):
        out = [p for p in super().patches() if getattr(p, 'attribute', None) != 'unit_state']
        return out + [mock.patch.object(server, 'daemon_get', self.fake_daemon_get),
                      mock.patch.object(server, 'unit_state', self.fake_unit_state),
                      mock.patch.object(server, 'unit_log',
                                        lambda unit: (['%s: page event ready' % unit, 'token ' + TOKEN_B], 3)),
                      mock.patch.dict(os.environ, {'TVBRIDGE_TOKEN_B': TOKEN_B})]


def build(env, name):
    """Login A: Stage C (all three short 2, break-even). Login B per scenario."""
    tf.build(env, 'all_in_trade_be')
    if name == 'no_registry':
        return
    env.registry()
    if name == 'two_logins':
        env.login_b()
    elif name == 'b_logged_out':
        env.login_b(health='b-health-logged-out.json', login='b-login-logged-out.json',
                    guard='b-guard-logged-out.json')
        env.b['followers']['followers'] = [dict(r, logged_in=False, synced=False, healthy=False,
                                                problems=['page_state:login']) for r in env.b['followers']['followers']]
    elif name == 'b_unreachable':
        env.login_b(health=None, login=None, followers=None)
    elif name == 'b_zero_assigned':
        cfg = env.fixture('b-config.json')
        cfg['followers'] = []
        env.login_b(login='b-login-zero-assigned.json', followers=None)
        env.write_json('logins/b/config.json', cfg)
    else:
        raise KeyError(name)


def run(env):
    ps = env.patches()
    for p in ps:
        p.start()
    try:
        raw = server.snapshot()
        body = server.scrub(json.dumps(raw, default=str))
    finally:
        for p in ps:
            p.stop()
    return json.loads(body), body


def scenario(name, tmp_dir=None):
    with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
        env = LoginEnv(tmp)
        build(env, name)
        s, body = run(env)
        return s, body, env.calls


class LoginsCase(unittest.TestCase):
    def snap(self, name):
        s, body, calls = scenario(name, os.environ.get('PANEL_TEST_TMP'))
        self.assertNotIn(TOKEN_B, body)         # login B's token never leaves the server
        return s, calls

    def logins(self, s):
        return {l['id']: l for l in s['logins']}

    # --- no registry: unchanged -------------------------------------------------------------------

    def test_no_registry_unchanged(self):
        s, calls = self.snap('no_registry')
        ref = tf.scenario_snapshot('all_in_trade_be', os.environ.get('PANEL_TEST_TMP'))
        for k in ('logins', 'logins_error', 'verdict_logins', 'verdict_primary'):
            self.assertNotIn(k, s)
        self.assertEqual(calls, [])                               # no /login, no second daemon
        self.assertEqual(sorted(k for k in s if not k.startswith('_')), sorted(k for k in ref if not k.startswith('_')))
        self.assertEqual(s['verdict'], ref['verdict'])
        self.assertEqual(s['followers']['alerts'], ref['followers']['alerts'])
        self.assertEqual(s['followers']['live_alerts'], ref['followers']['live_alerts'])
        self.assertEqual([c['key'] for c in s['followers']['cards']], ['account-a', 'account-b', 'account-c'])
        self.assertEqual(s['followers']['cards'][0]['name'], 'Account A')
        self.assertNotIn('_all_alerts', s['followers'])

    def test_leader_name_comes_from_config(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'no_registry')
            cfg = json.loads((env.root / 'config.json').read_text())
            cfg['soup_account_id'] = 'Main 9'
            env.write_json('config.json', cfg)
            s, _ = run(env)
            self.assertEqual((s['followers']['cards'][0]['name'], s['followers']['cards'][0]['short']), ('Main 9', '9'))
            del cfg['soup_account_id']
            env.write_json('config.json', cfg)
            s, _ = run(env)
            self.assertEqual(s['followers']['cards'][0]['name'], 'DEMO-0001')   # account_name, not 'Account A'

    # --- two logins, both connected ----------------------------------------------------------------

    def test_two_logins_connected(self):
        s, calls = self.snap('two_logins')
        self.assertEqual([l['id'] for l in s['logins']], ['a', 'b'])          # primary first
        a, b = self.logins(s)['a'], self.logins(s)['b']
        self.assertEqual((a['state'], a['state_label'], b['state'], b['state_label']),
                         ('connected', 'Connected', 'connected', 'Connected'))
        self.assertEqual((s['verdict']['tone'], s['verdict']['title']), ('mint', 'Healthy · armed'))   # primary's own
        self.assertEqual(s['verdict_logins'], s['verdict'])                 # nothing to add: the same headline
        self.assertEqual([c['key'] for c in a['cards']], ['account-a', 'account-b', 'account-c'])
        self.assertEqual(a['cards'][0]['status']['label'], 'Armed')
        self.assertEqual(a['counts_text'], '5 accounts visible · 3 assigned')
        self.assertEqual([c['key'] for c in b['cards']], ['account-f', 'account-g'])
        self.assertEqual(b['cards'][0]['name'], 'Account F')
        self.assertEqual(b['summary'], 'Connected · 2 accounts visible · 2 assigned')
        self.assertEqual(b['mode'], 'followers_only')
        self.assertEqual(b['trade_followers'], [{'key': k, 'short': k[-1].upper(), 'relation': 'not_in',
                                                 'text': 'not in this trade'} for k in ('account-f', 'account-g')])
        self.assertIsNotNone(b['last_relogin_at'])                           # the guard's logged_in_now event
        self.assertRegex(b['last_relogin_text'], r'^\d\d:\d\d:\d\d')
        self.assertEqual(sorted(b['units']), sorted(['tvbridge-browser@b.service', 'tvbridge-daemon@b.service',
                                                     'tvbridge-login-guard@b.timer', 'tvbridge-login-trigger@b.service']))
        self.assertEqual(b['units_down'], 0)
        self.assertTrue(b['master_kill_switch'] is False)
        self.assertIn('tvbridge-daemon@b.service: page event ready', b['log'])
        self.assertIn((8778, 'health'), calls)
        self.assertIn((8778, 'followers'), calls)
        self.assertIn((8777, 'login'), calls)
        self.assertNotIn((8777, 'health'), calls)                            # the primary's /health is today's call
        # The watcher's fields keep their shape.
        self.assertEqual(sorted(s['units']), sorted(server.UNITS))
        self.assertEqual(sorted(s['verdict']), ['detail', 'title', 'tone'])

    def test_master_kill_switch_is_a_calm_reason(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            env.kill('ENTRIES-DISABLED')
            s, _ = run(env)
        b = self.logins(s)['b']
        self.assertTrue(b['master_kill_switch'])
        self.assertIn('kill switch also stops follower entries', b['cards'][0]['status']['reason'])

    def test_login_header_counts_paused_accounts(self):
        """A pause set from Bridge Advanced shows on the account and in its login's header."""
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            s, _ = run(env)
            self.assertNotIn('paused', self.logins(s)['b']['counts_text'])
            sw = env.root / 'logins/b/ENTRIES-DISABLED-account-f'
            sw.parent.mkdir(parents=True, exist_ok=True)
            sw.write_text(json.dumps({'set_by': 'tvbridge-admin', 'reason': 'sizing', 'change_id': 'c', 'at': 1.0}))
            s, _ = run(env)
        b = self.logins(s)['b']
        self.assertEqual(b['counts_text'], '%d assigned · 1 paused' % b['assigned']
                         if 'visible' not in b['counts_text'] else b['counts_text'])
        self.assertIn('1 paused', b['counts_text'])
        card = b['cards'][0]
        self.assertEqual((card['status']['label'], card['pause']['by'], card['pause']['reason']), ('Paused', 'admin', 'sizing'))

    # --- login B logged out: amber, names B, A unaffected -----------------------------------------

    def test_b_logged_out(self):
        s, _ = self.snap('b_logged_out')
        a, b = self.logins(s)['a'], self.logins(s)['b']
        self.assertEqual((b['state'], b['state_label'], b['tone']), ('logged_out', 'Logged out', 'amber'))
        hl = s['verdict_logins']
        self.assertEqual((hl['tone'], hl['title']), ('amber', 'Login B logged out · Login A trading normally'))
        self.assertTrue(hl['detail'].startswith('Login B: Automatic login is in progress'))
        self.assertIn('Its 2 accounts skip new entries until it is back.', hl['detail'])
        self.assertTrue(hl['detail'].endswith('Login A is not affected.'))
        # `verdict` is exactly the primary's own verdict, as without a registry (other /api/status readers use it).
        self.assertEqual(s['verdict'], primary_verdict())
        self.assertEqual((s['verdict']['tone'], s['verdict']['title']), ('mint', 'Healthy · armed'))
        self.assertNotIn('verdict_primary', s)
        self.assertEqual(a['state'], 'connected')
        self.assertEqual(a['cards'][0]['status']['label'], 'Armed')          # A's leader card is untouched
        self.assertEqual([c['status']['state'] for c in a['cards'][1:]], ['trading', 'trading'])
        self.assertEqual(b['summary'], 'Logged out · 2 assigned')
        self.assertIn(b['cards'][0]['status']['state'], ('attention',))       # not synced right now
        self.assertEqual([c['ok'] for c in s['checks']][:3], [True, True, True])   # A's checks strip unchanged

    def test_primary_problem_keeps_todays_verdict(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'b_logged_out')
            env.kill('ENTRIES-DISABLED')
            s, _ = run(env)
            self.assertEqual((s['verdict_logins']['tone'], s['verdict_logins']['title']),
                             ('amber', 'Login B logged out · Login A entries off'))
            self.assertEqual((s['verdict']['tone'], s['verdict']['title']), ('teal', 'Healthy · entries off'))
            env.health = {'health': {'ok': False, 'problems': ['page_state:None'],
                                     'page': {'state': None, 'synced': None, 'url': tf.DEMO}}}
            s, _ = run(env)
        self.assertEqual(s['verdict']['title'], 'Bridge disconnected from page')   # A's problem wins, as today
        self.assertEqual(s['verdict_logins'], s['verdict'])
        self.assertEqual(self.logins(s)['a']['state'], 'reconnecting')

    # --- login B unreachable --------------------------------------------------------------------------

    def test_b_unreachable(self):
        s, _ = self.snap('b_unreachable')
        a, b = self.logins(s)['a'], self.logins(s)['b']
        self.assertEqual((b['state'], b['state_label'], b['tone']), ('unreachable', 'Unreachable', 'coral'))
        self.assertEqual((b['detail'], b['health_error']), ('Its bridge daemon does not answer.', 'URLError'))
        self.assertEqual((s['verdict_logins']['tone'], s['verdict_logins']['title']),
                         ('amber', 'Login B unreachable · Login A trading normally'))
        self.assertEqual(s['verdict'], primary_verdict())
        self.assertEqual(b['cards'][0]['status']['label'], 'Link unknown')
        self.assertIsNone(b['visible_count'])
        self.assertEqual(b['counts_text'], '2 assigned')
        self.assertEqual(a['state'], 'connected')

    def test_b_unreachable_while_daemon_restarts_or_stopped(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'b_unreachable')
            env.unit_states['tvbridge-daemon@b.service'] = {'load': 'loaded', 'active': 'activating',
                                                             'sub': 'auto-restart', 'result': 'exit-code'}
            s, _ = run(env)
            self.assertEqual(self.logins(s)['b']['state'], 'reconnecting')
            self.assertEqual(s['verdict_logins']['title'], 'Login B reconnecting · Login A trading normally')
            env.unit_states['tvbridge-daemon@b.service'] = {'load': 'loaded', 'active': 'failed', 'sub': 'failed',
                                                             'result': 'exit-code'}
            s, _ = run(env)
            b = self.logins(s)['b']
            self.assertEqual((b['state'], b['detail'], b['units_down']),
                             ('unreachable', 'Its bridge daemon is not running (failed).', 1))

    def test_missing_token_says_so_without_network(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            ps = [p for p in env.patches() if getattr(p, 'attribute', None) != 'daemon_get'
                  and not isinstance(p, mock._patch_dict)]
            for p in ps:
                p.start()
            try:
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop('TVBRIDGE_TOKEN_B', None)
                    os.environ.pop('TVBRIDGE_TOKEN', None)
                    s = json.loads(json.dumps(server.snapshot(), default=str))
            finally:
                for p in ps:
                    p.stop()
        b = self.logins(s)['b']
        self.assertEqual(b['state'], 'unreachable')
        self.assertEqual(b['detail'], 'The panel has no token for this login (TVBRIDGE_TOKEN_B is not set).')

    def test_token_only_sent_to_loopback(self):
        with mock.patch.object(server.urllib.request, 'build_opener', side_effect=AssertionError('no network')):
            self.assertEqual(server.daemon_get('http://10.0.0.5:8778', 'login', 'z' * 32), (None, 'not_loopback'))
            self.assertEqual(server.daemon_get('http://127.0.0.1:8778', 'login', 'short'), (None, 'token_missing'))

    # --- login B with no accounts assigned ---------------------------------------------------------

    def test_b_zero_assigned(self):
        s, calls = self.snap('b_zero_assigned')
        b = self.logins(s)['b']
        self.assertEqual(b['state'], 'connected')
        self.assertEqual(b['cards'], [])
        self.assertEqual(b['summary'], 'Connected · 3 accounts visible · none assigned')
        self.assertEqual([a['name'] for a in b['visible']], ['DEMOB0006', 'DEMOB0007', 'DEMOB0008'])
        self.assertEqual((s['verdict']['tone'], s['verdict']['title']), ('mint', 'Healthy · armed'))
        self.assertNotIn((8778, 'followers'), calls)                          # asked only with followers configured

    def test_b_zero_assigned_logged_out_names_no_accounts(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'b_zero_assigned')
            env.write_json('guard-b/status.json', env.fixture('b-guard-logged-out.json'))
            env.b['login'] = env.fixture('b-login-logged-out.json')
            s, _ = run(env)
        self.assertEqual(s['verdict_logins']['detail'].count('skip'), 0)     # nothing assigned, nothing skipped
        self.assertEqual(s['verdict_logins']['title'], 'Login B logged out · Login A trading normally')
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')

    # --- alerts, registry rules, page --------------------------------------------------------------

    def test_b_alerts_are_labelled_and_merged(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            fs = json.loads((env.root / 'firing.json').read_text())
            fs['accounts']['account-f'] = {'role': 'follower', 'enabled': True, 'login': 'b'}
            env.write_json('firing.json', fs)
            env.write_lines('firing/alerts.jsonl', [
                {'type': 'alert', 'account': 'account-f', 'kind': 'entry_not_fired', 'reason': 'login_down:b',
                 'at': int(env.now * 1000) - 5000}])
            s, _ = run(env)
        a = s['followers']['alerts'][0]
        self.assertEqual((a['account_label'], a['text']), ('Account F', 'Entry not sent (login_down:b)'))
        self.assertEqual(self.logins(s)['b']['cards'][0]['alert']['text'], 'Entry not sent (login_down:b)')

    # --- follower alerts: login A in today's order, other logins appended -------------------------

    def alerts_env(self, env, accounts):
        """alerts.jsonl with one entry_not_fired alert per account, oldest first, 10 s apart."""
        fs = json.loads((env.root / 'firing.json').read_text())
        fs['accounts']['account-f'] = {'role': 'follower', 'enabled': True, 'login': 'b'}
        env.write_json('firing.json', fs)
        n = len(accounts)
        env.write_lines('firing/alerts.jsonl', [
            {'type': 'alert', 'account': acc, 'kind': 'entry_not_fired', 'reason': 'r%d' % i,
             'at': int(env.now * 1000) - 10_000 * (n - i)} for i, acc in enumerate(accounts)])

    def test_alert_order_without_and_with_registry(self):
        accounts = ['account-b', 'account-f', 'account-c', 'account-f', 'account-b']
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            self.alerts_env(env, accounts)
            with_reg, _ = run(env)
            (env.root / 'logins.json').unlink()
            without, _ = run(env)
        # Without a registry: exactly today's list, newest first, every account (1.0 order).
        self.assertEqual([(a['account'], a['text']) for a in without['followers']['alerts']],
                         [('account-b', 'Entry not sent (r4)'), ('account-f', 'Entry not sent (r3)'),
                          ('account-c', 'Entry not sent (r2)'), ('account-f', 'Entry not sent (r1)'),
                          ('account-b', 'Entry not sent (r0)')])
        self.assertEqual(without['followers']['live_alerts'], 5)
        # With one: login A's alerts in that same order, then login B's appended (newest first), not merged.
        fo = with_reg['followers']
        self.assertEqual([a['text'] for a in fo['alerts']],
                         ['Entry not sent (r%d)' % i for i in (4, 2, 0, 3, 1)])
        self.assertEqual([a['account_label'] for a in fo['alerts']][3:], ['Account F', 'Account F'])
        self.assertEqual(fo['live_alerts'], sum(1 for a in fo['alerts'] if a['live']))
        self.assertEqual(fo['live_alerts'], 5)

    def test_alert_order_with_registry_unchanged_without_other_login_alerts(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            self.alerts_env(env, ['account-b', 'account-c'] * 6)
            with_reg, _ = run(env)
            (env.root / 'logins.json').unlink()
            without, _ = run(env)
        self.assertEqual(len(without['followers']['alerts']), server.ALERT_LIST)
        self.assertEqual(with_reg['followers']['alerts'], without['followers']['alerts'])
        self.assertEqual(with_reg['followers']['live_alerts'], without['followers']['live_alerts'])

    def test_alert_lists_are_capped_per_login(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            self.alerts_env(env, ['account-f'] + ['account-b'] * 10)
            s, _ = run(env)
        fo = s['followers']
        self.assertEqual([a['account'] for a in fo['alerts']], ['account-b'] * server.ALERT_LIST + ['account-f'])
        self.assertEqual(fo['alerts'][0]['text'], 'Entry not sent (r10)')
        self.assertEqual(fo['live_alerts'], 11)                              # counts every alert, as today

    # --- read tokens (GET /health and GET /login only) ---------------------------------------------

    def read_run(self, env_vars, read_envs={'b': 'TVBRIDGE_READ_TOKEN_B'}, drop=('TVBRIDGE_TOKEN_B',)):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            env.registry(read_envs=read_envs)
            with mock.patch.dict(os.environ, env_vars):
                ps = env.patches()
                for p in ps:
                    p.start()
                try:
                    for name in drop:
                        os.environ.pop(name, None)
                    raw = server.snapshot()
                    body = server.scrub(json.dumps(raw, default=str))
                finally:
                    for p in ps:
                        p.stop()
            return json.loads(body), body, env.sent

    def test_read_token_used_for_health_and_login(self):
        s, body, sent = self.read_run({'TVBRIDGE_READ_TOKEN_B': READ_B}, drop=())
        b = self.logins(s)['b']
        self.assertEqual(sorted((e, tok) for port, e, tok in sent if port == 8778),
                         [('followers', READ_B), ('health', READ_B), ('login', READ_B)])    # 1.1.0: read token on /followers too
        self.assertEqual((b['state'], b['followers_error']), ('connected', None))
        self.assertEqual(b['cards'][0]['status']['label'], 'Linked')
        self.assertNotIn(READ_B, body)

    def test_primary_login_read_uses_its_read_token(self):
        s, _, sent = self.read_run({'TVBRIDGE_READ_TOKEN_A': READ_A, 'TVBRIDGE_TOKEN': 'a' * 40},
                                   read_envs={'a': 'TVBRIDGE_READ_TOKEN_A'}, drop=())
        self.assertEqual([tok for port, e, tok in sent if port == 8777], [READ_A])     # its /login
        self.assertEqual(self.logins(s)['a']['state'], 'connected')

    def test_without_read_token_falls_back_to_order_token(self):
        for read_envs, env_vars in (({}, {}),                                            # entry names none
                                    ({'b': 'TVBRIDGE_READ_TOKEN_B'}, {}),                 # named, not set
                                    ({'b': 'TVBRIDGE_READ_TOKEN_B'}, {'TVBRIDGE_READ_TOKEN_B': 'short'})):
            with self.subTest(read_envs=read_envs, env_vars=env_vars):
                s, _, sent = self.read_run(env_vars, read_envs=read_envs, drop=('TVBRIDGE_READ_TOKEN_B',)
                                           if not env_vars else ())
                self.assertEqual(sorted((e, tok) for port, e, tok in sent if port == 8778),
                                 [('followers', TOKEN_B), ('health', TOKEN_B), ('login', TOKEN_B)])
                self.assertEqual(self.logins(s)['b']['state'], 'connected')

    def test_read_token_only_reads_followers_too(self):
        s, body, sent = self.read_run({'TVBRIDGE_READ_TOKEN_B': READ_B})
        b = self.logins(s)['b']
        self.assertEqual(sorted((e, tok) for port, e, tok in sent if port == 8778),
                         [('followers', READ_B), ('health', READ_B), ('login', READ_B)])  # no order token needed
        self.assertEqual((b['state'], b['followers_error']), ('connected', None))
        self.assertNotIn(READ_B, body)

    def test_no_token_names_the_read_token_env(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'two_logins')
            env.registry(read_envs={'b': 'TVBRIDGE_READ_TOKEN_B'})
            ps = [p for p in env.patches() if getattr(p, 'attribute', None) != 'daemon_get'
                  and not isinstance(p, mock._patch_dict)]
            for p in ps:
                p.start()
            try:
                with mock.patch.dict(os.environ, {}, clear=False):
                    for name in ('TVBRIDGE_TOKEN_B', 'TVBRIDGE_READ_TOKEN_B', 'TVBRIDGE_TOKEN'):
                        os.environ.pop(name, None)
                    s = json.loads(json.dumps(server.snapshot(), default=str))
            finally:
                for p in ps:
                    p.stop()
        b = self.logins(s)['b']
        self.assertEqual(b['detail'], 'The panel has no token for this login (TVBRIDGE_READ_TOKEN_B is not set).')
        self.assertEqual(b['followers_error'], 'token_missing')

    def test_read_tokens_are_scrubbed(self):
        with mock.patch.object(server, 'LOGIN_TOKEN_ENVS', set()), \
                mock.patch.dict(os.environ, {'TVBRIDGE_READ_TOKEN_B': READ_B}):
            self.assertEqual(server.scrub('x ' + READ_B), 'x ' + READ_B)               # not known yet
            with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
                env = LoginEnv(tmp)
                build(env, 'two_logins')
                env.registry(read_envs={'b': 'TVBRIDGE_READ_TOKEN_B'})
                ps = env.patches() + [mock.patch.object(server, 'unit_log',
                                                        lambda unit: (['read ' + READ_B, 'order ' + TOKEN_B], 0))]
                for p in ps:
                    p.start()
                try:
                    s = json.loads(json.dumps(server.snapshot(), default=str))
                    body = server.scrub(json.dumps(s))
                finally:
                    for p in reversed(ps):
                        p.stop()
            self.assertIn('TVBRIDGE_READ_TOKEN_B', server.LOGIN_TOKEN_ENVS)
            self.assertEqual(server.scrub('x ' + READ_B), 'x <token>')
        self.assertNotIn(READ_B, body)
        self.assertNotIn(TOKEN_B, body)
        self.assertIn('read <token>', self.logins(json.loads(body))['b']['log'])   # what /api/status serves

    def test_env_override_and_invalid_registry(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = LoginEnv(tmp)
            build(env, 'no_registry')
            env.login_b()
            env.registry(where='elsewhere/my-logins.json')
            with mock.patch.dict(os.environ, {'TVBRIDGE_LOGINS': str(env.root / 'elsewhere/my-logins.json')}):
                s, _ = run(env)
                self.assertEqual([l['id'] for l in s['logins']], ['a', 'b'])
                bad = env.fixture('registry-two.json')
                bad['logins'][0]['primary'] = True
                env.write_json('elsewhere/my-logins.json', bad)
                s, _ = run(env)
        self.assertEqual(s['logins'], [])
        self.assertIn('exactly one login must have "primary": true', s['logins_error'])
        self.assertEqual((s['verdict_logins']['tone'], s['verdict_logins']['title']), ('amber', 'Login list unreadable'))
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')
        self.assertEqual(s['followers']['cards'][0]['status']['label'], 'Armed')   # the main login still shows

    def test_registry_rules(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            root = Path(tmp)
            base = materialize(json.loads((FIX / 'registry-two.json').read_text()), root, 0)
            path = root / 'logins.json'

            def load(mutate):
                d = json.loads(json.dumps(base))
                mutate(d)
                path.write_text(json.dumps(d))
                return server.load_logins(path)

            self.assertEqual([l['id'] for l in load(lambda d: None)], ['a', 'b'])
            unlabelled = load(lambda d: [d['logins'][0].pop('label'), d['logins'][0].pop('primary')])
            self.assertEqual((unlabelled[1]['label'], unlabelled[1]['primary']), ('Login B', False))   # shared defaults
            home = load(lambda d: d['logins'][0].update(guard_state_dir='~/x-b'))
            self.assertEqual(server.by_id(home, 'b')['guard_state_dir'], os.path.expanduser('~/x-b'))
            self.assertEqual(server.primary(home)['id'], 'a')
            read = load(lambda d: [e.update(read_token_env='TVBRIDGE_READ_TOKEN_' + e['id'].upper()) for e in d['logins']])
            self.assertEqual([l['read_token_env'] for l in read], ['TVBRIDGE_READ_TOKEN_A', 'TVBRIDGE_READ_TOKEN_B'])
            self.assertNotIn('read_token_env', server.by_id(home, 'b'))      # optional
            cases = {
                'schema must be': lambda d: d.update(schema='v0'),
                'id must match': lambda d: d['logins'][0].update(id='B'),
                'reserved for the login guard': lambda d: d['logins'][0].update(devtools_port=9556),
                'port 8777 is used by both': lambda d: d['logins'][0].update(bridge_url='http://127.0.0.1:8777'),
                'token_env TVBRIDGE_TOKEN is used': lambda d: d['logins'][0].update(token_env='TVBRIDGE_TOKEN'),
                'unit tvbridge-daemon.service is used': lambda d: d['logins'][0].update(daemon_unit='tvbridge-daemon.service'),
                'guard_state_dir': lambda d: d['logins'][0].update(guard_state_dir=d['logins'][1]['guard_state_dir']),
                'config ': lambda d: d['logins'][0].update(config=d['logins'][1]['config']),
                'exactly one login': lambda d: d['logins'][1].update(primary=False),
                'primary must be': lambda d: d['logins'][1].update(primary='yes'),
                'label must be': lambda d: d['logins'][1].update(label=' '),
                'must name a .service unit': lambda d: d['logins'][0].update(guard_unit='tvbridge-login-guard@b.timer'),
                'http URL with a port': lambda d: d['logins'][0].update(bridge_url='http://127.0.0.1'),
                'environment variable name': lambda d: d['logins'][0].update(token_env='token-b'),
                'non-empty list': lambda d: d.update(logins=[]),
                'read_token_env must be an environment variable name':
                    lambda d: d['logins'][0].update(read_token_env='read-b'),
                'read_token_env must be an': lambda d: d['logins'][0].update(read_token_env=None),
                # never any login's order token env, its own or another's; unique among read tokens
                'read_token_env TVBRIDGE_TOKEN_B is used by both':
                    lambda d: d['logins'][0].update(read_token_env='TVBRIDGE_TOKEN_B'),
                'token_env TVBRIDGE_TOKEN is used by both login b (read_token_env) and login a':
                    lambda d: d['logins'][0].update(read_token_env='TVBRIDGE_TOKEN'),
                'read_token_env TVBRIDGE_TOKEN_B is used by both login b and login a (read_token_env)':
                    lambda d: d['logins'][1].update(read_token_env='TVBRIDGE_TOKEN_B'),
                'read_token_env TVBRIDGE_READ_TOKEN is used by both login b (read_token_env) and login a':
                    lambda d: [e.update(read_token_env='TVBRIDGE_READ_TOKEN') for e in d['logins']],
            }
            for needle, mutate in cases.items():
                with self.assertRaises(ValueError, msg=needle) as cm:
                    load(mutate)
                self.assertIn(needle, str(cm.exception))
            self.assertEqual([l['id'] for l in server.load_logins(root / 'absent.json')], ['a'])   # implicit login a

    def test_page_login_blocks_are_text_only_and_accessible(self):
        page = server.PAGE
        script = page[page.index('<script>'):]
        self.assertIn("h('h3',null,l.label||l.id", script)                  # a heading per login
        self.assertIn('hd.tabIndex=0', script)                               # keyboard-focusable
        self.assertIn("box.setAttribute('role','region')", script)
        self.assertIn("h('span','ls '+tone2(l.tone),h('i'),l.state_label", script)   # state in words, not colour only
        self.assertIn('.lh h3:focus-visible{outline', page)
        self.assertEqual(page.count('innerHTML'), 0)
        self.assertEqual(re.findall(r"fetch\('([^']+)'", page), ['/api/status'])
        for bad in ('<button', '<form', '<input', 'onclick'):
            self.assertNotIn(bad, page)

    def test_preview_embeds_data_as_text(self):
        s, _ = self.snap('two_logins')
        s['logins'][1]['visible'][0]['name'] = '</script><img src=x onerror=alert(1)>'
        html = server.preview_page(s)
        self.assertEqual(html.count('</script>'), page_scripts() + 2)
        self.assertNotIn('<img src=x', html)
        data = html[html.index('id="preview-data">') + len('id="preview-data">'):]
        self.assertEqual(json.loads(data[:data.index('</script>')])['logins'][1]['visible'][0]['name'],
                         '</script><img src=x onerror=alert(1)>')


def primary_verdict():
    """Login A's verdict without a registry (the same Stage C scenario): what `verdict` must stay."""
    return tf.scenario_snapshot('all_in_trade_be', os.environ.get('PANEL_TEST_TMP'))['verdict']


def page_scripts():
    return server.PAGE.count('</script>')


def write_previews(out_dir='/tmp/tvbridge-panel-preview'):
    """Static HTML of the two-login and B-logged-out fixtures, through the server's own snapshot
    and page. Offline: every daemon reply and unit state is a fixture."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tf.setUpModule()
    try:
        written = []
        for name, file in (('two_logins', 'two-logins.html'), ('b_logged_out', 'b-logged-out.html'),
                           ('b_unreachable', 'b-unreachable.html'), ('b_zero_assigned', 'b-zero-assigned.html')):
            s, _, _ = scenario(name)
            (out / file).write_text(server.preview_page(s))
            written.append(str(out / file))
        return written
    finally:
        tf.tearDownModule()


if __name__ == '__main__':
    if '--preview' in sys.argv:
        args = [a for a in sys.argv[1:] if a != '--preview']
        print('\n'.join(write_previews(*args[:1])))
    else:
        unittest.main()
