"""1.1.0 multi-login: a `mode: followers_only` login daemon, end to end through daemon.main().

No browser: the page double from test_fill_capture stands in for tvbridge.bridge.Bridge. On a
followers_only login there is no main account, no Soup publisher (no adapter database, no writer
lock, no soup_* keys needed); Soup routes answer 409 {"error": "followers_only"} and /account is
404. Followers work as on a main login. With an empty follower list the daemon still attaches,
syncs and serves /health, /login and /followers ("connected and ready, no account assigned").
"""
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import harness  # noqa: F401  (puts the repository on sys.path)

from tvbridge import daemon
from test_fill_capture import TICKER, PageBridge, free_port, p_entry, wait_until

TOKEN_B = 'followers-only-daemon-token-0123456789'
TOKEN_A = 'main-login-daemon-token-0123456789abc'
READ_B = 'followers-only-read-token-0123456789ab'
TID = 'soup:MNQZ2021:1:long'


class FollowersOnlyDaemon(unittest.TestCase):
    def config(self, tmp, port, followers=()):
        return {'login_id': 'b', 'login_label': 'Login B', 'mode': 'followers_only', 'token_env': 'TVBRIDGE_TOKEN_B',
                'ticker': TICKER, 'tick_size': .25, 'devtools_port': 1, 'page_url_contains': 'x',
                'trade_url_pattern': 'x', 'listen_port': port, 'shutdown_drain_s': 2, 'tag_wait_s': 0.2,
                'entry_settle_s': 0.5,
                # Present but ignored on a followers_only login: nothing Soup-side is opened.
                'publish_mode': 'adapter-db', 'soup_adapter_db': str(tmp / 'adapter' / 'events.sqlite'),
                'master_kill_switch_file': str(tmp / 'login-a' / 'ENTRIES-DISABLED'),
                'followers': list(followers)}

    def run_daemon(self, followers=(), actions=None, override=None, token_env='TVBRIDGE_TOKEN_B', token=TOKEN_B,
                   paths_token=TOKEN_B, extra_env=None):
        PageBridge.instances = []
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / 'login-a').mkdir()
        port = free_port()
        follower_cfg = [dict(f, journal_dir=str(tmp / f['journal_dir']), kill_switch_file=str(tmp / f['kill_switch_file']))
                        for f in followers]
        cfg = self.config(tmp, port, follower_cfg)
        for key, value in (override(tmp) if override else {}).items():
            if value is None:
                cfg.pop(key, None)
            else:
                cfg[key] = value
        (tmp / 'config.json').write_text(json.dumps(cfg))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        results = {}

        def call(method, path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=data, method=method,
                                         headers={'Content-Type': 'application/json'})
            try:
                with opener.open(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        def drive(before):
            try:
                assert wait_until(lambda: signal.getsignal(signal.SIGTERM) is not before)
                results['health'] = call('GET', '/health/' + paths_token)
                results['login'] = call('GET', '/login/' + paths_token)
                results['followers'] = call('GET', '/followers/' + paths_token)
                results['order'] = call('POST', '/order/' + paths_token, p_entry('c1', TID, 1))
                results['state'] = call('GET', '/state/' + paths_token + '?trade_id=' + TID)
                results['account'] = call('GET', '/account/' + paths_token)
                results['bad_token'] = call('GET', '/login/' + 'x' * len(paths_token))
                results['threads'] = sorted(t.name for t in threading.enumerate())
                if actions:
                    actions(call, results, tmp)
            finally:
                os.kill(os.getpid(), signal.SIGTERM)

        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        threading.Thread(target=drive, args=(before,), daemon=True).start()
        env = {k: v for k, v in os.environ.items() if not k.startswith('TVBRIDGE_TOKEN')}
        env[token_env] = token
        env.update(extra_env or {})
        with patch.object(daemon, 'Bridge', PageBridge), \
                patch.object(sys, 'argv', ['daemon', '--config', str(tmp / 'config.json')]), \
                patch.dict(os.environ, env, clear=True), patch('logging.basicConfig'):
            with self.assertLogs('tvbridge', level='INFO') as logs:
                self.assertIsNone(daemon.main())
        results['logs'] = logs.output
        results['tmp'] = tmp
        return results

    def assert_followers_only_routes(self, r):
        self.assertEqual(r['order'], (409, {'error': 'followers_only'}))
        self.assertEqual(r['state'], (409, {'error': 'followers_only'}))
        self.assertEqual(r['account'][0], 404)
        self.assertEqual(r['bad_token'][0], 404)
        self.assertNotIn('publisher', r['threads'])
        self.assertFalse((r['tmp'] / 'adapter').exists(), 'no adapter database or writer lock')
        status, health = r['health']
        self.assertEqual(status, 200)
        self.assertEqual(health['login_id'], 'b')
        self.assertIsNone(health['publisher'])
        self.assertEqual(health['health']['page']['state'], 'authorized')  # what the login trigger reads
        self.assertTrue(health['health']['ok'], health)
        self.assertFalse(health['entries_disabled'])

    def test_empty_follower_list_is_connected_and_ready(self):
        r = self.run_daemon()
        self.assert_followers_only_routes(r)
        status, login = r['login']
        self.assertEqual(status, 200)
        self.assertEqual((login['login_id'], login['label'], login['mode']), ('b', 'Login B', 'followers_only'))
        self.assertTrue(login['page']['logged_in'])
        self.assertEqual([(a['name'], a['configured'], a['role']) for a in login['accounts']],
                         [('MAIN-ACCT', False, None), ('F-B', False, None), ('F-C', False, None)])
        status, report = r['followers']
        self.assertEqual((status, report['followers'], report['login_id']), (200, [], 'b'))
        self.assertNotIn('main', report)
        self.assertIsInstance(report['observed_at'], int)
        self.assertTrue(any('connected and ready, no account assigned' in line for line in r['logs']))
        self.assertEqual(PageBridge.instances[0].ex.requests, [], 'nothing sent to Tradovate')

    def test_followers_trade_and_master_switch_stops_them(self):
        def actions(call, results, tmp):
            results['f_order'] = call('POST', '/follower/%s/fb/order' % TOKEN_B, p_entry('c1', TID, 1))
            (tmp / 'login-a' / 'ENTRIES-DISABLED').touch()
            results['f_refused'] = call('POST', '/follower/%s/fb/order' % TOKEN_B,
                                        p_entry('c2', 'soup:MNQZ2021:2:long', 1))
            results['health_killed'] = call('GET', '/health/' + TOKEN_B)
            results['followers_killed'] = call('GET', '/followers/' + TOKEN_B)

        r = self.run_daemon([{'key': 'fb', 'account_name': 'F-B', 'account_id': 8, 'journal_dir': 'journal-fb',
                              'kill_switch_file': 'ENTRIES-DISABLED-fb', 'max_contracts': 10,
                              'prop_profile': 'cross-account-window'}], actions)
        self.assert_followers_only_routes(r)
        roles = {a['name']: (a['role'], a['key']) for a in r['login'][1]['accounts']}
        self.assertEqual(roles['F-B'], ('follower', 'fb'))
        self.assertEqual(roles['MAIN-ACCT'], (None, None))
        row, = r['followers'][1]['followers']
        self.assertEqual((row['key'], row['available'], row['login_id'], row['logged_in']), ('fb', True, 'b', True))
        self.assertEqual(r['f_order'][1]['outcome'], 'accepted')
        self.assertEqual(r['f_refused'][1]['receipt']['reason'], 'entries disabled by kill switch')
        self.assertTrue(r['health_killed'][1]['entries_disabled'])
        self.assertTrue(r['followers_killed'][1]['followers'][0]['entries_disabled'])
        self.assertTrue((r['tmp'] / 'login-a' / 'ENTRIES-DISABLED').exists())
        self.assertTrue(any('fill capture on: fb' in line for line in r['logs']))

    def test_read_token_reads_login_health_and_followers_only(self):
        def actions(call, results, tmp):
            results['r_login'] = call('GET', '/login/' + READ_B)
            results['r_health'] = call('GET', '/health/' + READ_B)
            results['r_followers'] = call('GET', '/followers/' + READ_B)
            results['r_fstate'] = call('GET', '/follower/%s/fb/state' % READ_B)
            results['r_order'] = call('POST', '/follower/%s/fb/order' % READ_B, p_entry('c1', TID, 1))
            results['r_kill'] = call('POST', '/follower/%s/fb/kill' % READ_B, {})
            results['r_soup_order'] = call('POST', '/order/' + READ_B, p_entry('c2', TID, 1))
            results['r_state'] = call('GET', '/state/' + READ_B)

        r = self.run_daemon([{'key': 'fb', 'account_name': 'F-B', 'account_id': 8, 'journal_dir': 'journal-fb',
                              'kill_switch_file': 'ENTRIES-DISABLED-fb', 'max_contracts': 10,
                              'prop_profile': 'cross-account-window'}], actions,
                            override=lambda tmp: {'read_token_env': 'TVBRIDGE_READ_TOKEN_B'},
                            extra_env={'TVBRIDGE_READ_TOKEN_B': READ_B})
        self.assert_followers_only_routes(r)
        self.assertEqual(r['r_login'][0], 200)
        self.assertEqual(r['r_login'][1]['login_id'], 'b')
        self.assertEqual(r['r_health'][0], 200)
        self.assertEqual(r['r_followers'][0], 200)  # read-only report for the panel
        self.assertEqual([x['key'] for x in r['r_followers'][1]['followers']], ['fb'])
        for key in ('r_fstate', 'r_order', 'r_kill', 'r_soup_order', 'r_state'):
            self.assertEqual(r[key][0], 404, key)
        self.assertFalse((r['tmp'] / 'ENTRIES-DISABLED-fb').exists())
        self.assertEqual(PageBridge.instances[0].ex.requests, [], 'nothing sent to Tradovate')


class MainLoginWithPeer(FollowersOnlyDaemon):
    """A main login (today's daemon) with login_id and an unreachable peer: entries fail open, the
    peer shows in /health notes (not problems), /login lists the main account as `main`."""

    test_empty_follower_list_is_connected_and_ready = None
    test_followers_trade_and_master_switch_stops_them = None
    test_read_token_reads_login_health_and_followers_only = None

    def test_unreachable_peer_fails_closed_and_is_noted(self):
        def override(tmp):
            return {'mode': None, 'login_id': 'a', 'login_label': None, 'token_env': None, 'publish_mode': 'evidence',
                    'soup_adapter_db': None, 'master_kill_switch_file': None, 'account_name': 'MAIN-ACCT',
                    'account_id': 7, 'soup_account_id': 'Account A', 'max_contracts': 10,
                    'journal_dir': str(tmp / 'journal'), 'kill_switch_file': str(tmp / 'ENTRIES-DISABLED'),
                    'peer_logins': [{'id': 'b', 'url': 'http://127.0.0.1:%d' % free_port(),
                                     'token_env': 'TVBRIDGE_TOKEN_B'}]}

        def actions(call, results, tmp):
            wait_until(lambda: call('GET', '/health/' + TOKEN_A)[1].get('notes'), 5)
            results['health_noted'] = call('GET', '/health/' + TOKEN_A)

        r = self.run_daemon(actions=actions, override=override, token_env='TVBRIDGE_TOKEN', token=TOKEN_A,
                            paths_token=TOKEN_A, extra_env={'TVBRIDGE_TOKEN_B': TOKEN_B})
        # login B never answered, so its exposure is unknown: the leader's entry is refused (fail closed)
        self.assertEqual(r['order'][1]['outcome'], 'rejected')
        self.assertTrue(r['order'][1]['receipt']['reason'].startswith('hedge_peer_unknown:b:'))
        health = r['health_noted'][1]
        self.assertEqual((health['login_id'], health['notes']), ('a', ['peer_unreachable:b']))
        self.assertNotIn('peer_unreachable:b', health['health']['problems'])
        login = r['login'][1]
        self.assertEqual((login['login_id'], login['mode']), ('a', 'main'))
        self.assertEqual({a['name']: a['role'] for a in login['accounts']}['MAIN-ACCT'], 'main')
        self.assertIn('publisher', r['threads'])
        self.assertIn('peer-logins', r['threads'])
        self.assertTrue(any('peer login b unreachable' in line for line in r['logs']))


class FollowersOnlyStartup(unittest.TestCase):
    def run_main(self, cfg, env):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'config.json'
            p.write_text(json.dumps(cfg))
            with patch.object(sys, 'argv', ['daemon', '--config', str(p)]), patch.dict(os.environ, env, clear=True), \
                    patch.object(daemon, 'Bridge', side_effect=AssertionError('browser touched')):
                with self.assertRaises(SystemExit) as cm:
                    daemon.main()
        return str(cm.exception)

    def test_own_token_env_is_required_before_the_browser(self):
        cfg = {'mode': 'followers_only', 'login_id': 'b', 'token_env': 'TVBRIDGE_TOKEN_B'}
        self.assertIn('TVBRIDGE_TOKEN_B of at least 24 characters required',
                      self.run_main(cfg, {'TVBRIDGE_TOKEN': 'x' * 32}))

    def test_bad_read_token_stops_before_the_browser(self):
        cfg = {'mode': 'followers_only', 'login_id': 'b', 'token_env': 'TVBRIDGE_TOKEN_B',
               'read_token_env': 'TVBRIDGE_READ_TOKEN_B'}
        self.assertIn('TVBRIDGE_READ_TOKEN_B must hold at least 24 characters',
                      self.run_main(cfg, {'TVBRIDGE_TOKEN_B': TOKEN_B, 'TVBRIDGE_READ_TOKEN_B': 'short'}))
        self.assertIn('must differ from TVBRIDGE_TOKEN_B',
                      self.run_main(cfg, {'TVBRIDGE_TOKEN_B': TOKEN_B, 'TVBRIDGE_READ_TOKEN_B': TOKEN_B}))

    def test_invalid_login_keys_stop_before_the_browser(self):
        for cfg, fragment in (({'mode': 'leader'}, 'mode must be'), ({'login_id': 'B'}, 'login_id'),
                              ({'peer_hedge_scope': 'some'}, 'peer_hedge_scope'),
                              ({'read_token_env': 'TVBRIDGE_TOKEN'}, 'read_token_env'),
                              ({'peer_logins': [{'id': 'b'}]}, 'peer_logins[0]')):
            with self.subTest(cfg=cfg):
                self.assertIn(fragment, self.run_main(cfg, {'TVBRIDGE_TOKEN': 'x' * 32}))


if __name__ == '__main__':
    unittest.main()
