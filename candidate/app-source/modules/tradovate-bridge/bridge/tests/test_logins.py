"""1.1.0 multi-login: the login registry (tvbridge.logins) and the daemon's login settings.

Pure parsing: no browser, no network. Without logins.json there is exactly one implicit login `a`
built from today's defaults; every daemon key is optional and defaults to today's daemon.
"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import harness  # noqa: F401  (puts the repository on sys.path)

from tvbridge import logins

HOME = os.path.expanduser('~')
A = {'id': 'a', 'label': 'Login A', 'primary': True,
     'config': '~/.local/share/decrypt-os/tvbridge/config.json',
     'bridge_url': 'http://127.0.0.1:8777', 'token_env': 'TVBRIDGE_TOKEN',
     'devtools_port': 9555,
     'browser_unit': 'tvbridge-browser.service', 'daemon_unit': 'tvbridge-daemon.service',
     'guard_unit': 'tvbridge-login-guard.service', 'trigger_unit': 'tvbridge-login-trigger.service',
     'guard_state_dir': '~/.local/share/decrypt-os/tvbridge-state/login-guard'}
B = {'id': 'b', 'label': 'Login B', 'primary': False,
     'config': '~/.local/share/decrypt-os/tvbridge/logins/b/config.json',
     'bridge_url': 'http://127.0.0.1:8778', 'token_env': 'TVBRIDGE_TOKEN_B',
     'devtools_port': 9557,
     'browser_unit': 'tvbridge-browser@b.service', 'daemon_unit': 'tvbridge-daemon@b.service',
     'guard_unit': 'tvbridge-login-guard@b.service', 'trigger_unit': 'tvbridge-login-trigger@b.service',
     'guard_state_dir': '~/.local/share/decrypt-os/tvbridge-state/login-guard-b'}


def registry(*entries):
    return {'schema': logins.SCHEMA, 'logins': [copy.deepcopy(e) for e in entries]}


class Registry(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(logins.ENV_PATH, None)

    def write(self, data, name='logins.json'):
        p = self.dir / name
        p.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return p

    def test_absent_file_is_one_implicit_login_a_with_todays_defaults(self):
        found = logins.load_logins(self.dir / 'missing.json')
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual((a.id, a.label, a.primary), ('a', 'Login A', True))
        self.assertEqual((a.bridge_url, a.bridge_port, a.token_env, a.devtools_port),
                         ('http://127.0.0.1:8777', 8777, 'TVBRIDGE_TOKEN', 9555))
        self.assertEqual(a.config, HOME + '/.local/share/decrypt-os/tvbridge/config.json')
        self.assertEqual(a.guard_state_dir, HOME + '/.local/share/decrypt-os/tvbridge-state/login-guard')
        self.assertEqual(a.units, ('tvbridge-browser.service', 'tvbridge-daemon.service',
                                   'tvbridge-login-guard.service', 'tvbridge-login-trigger.service'))
        self.assertIs(logins.primary(found), a)
        self.assertIs(logins.by_id(found, 'a'), a)

    def test_design_example_loads_and_expands_home(self):
        found = logins.load_logins(self.write(registry(A, B)))
        self.assertEqual([x.id for x in found], ['a', 'b'])
        b = logins.by_id(found, 'b')
        self.assertEqual(b.config, HOME + '/.local/share/decrypt-os/tvbridge/logins/b/config.json')
        self.assertEqual((b.bridge_port, b.devtools_port, b.primary), (8778, 9557, False))
        self.assertEqual(logins.primary(found).id, 'a')
        with self.assertRaisesRegex(ValueError, "no login with id 'c'"):
            logins.by_id(found, 'c')

    def test_path_precedence_explicit_then_env_then_default(self):
        env_file = self.write(registry(A, B), 'env.json')
        os.environ[logins.ENV_PATH] = str(env_file)
        self.assertEqual(len(logins.load_logins()), 2)
        self.assertEqual(len(logins.load_logins(self.dir / 'absent.json')), 1)  # explicit path wins
        del os.environ[logins.ENV_PATH]
        self.assertEqual(logins.registry_path(), Path(HOME) / '.local/share/decrypt-os/tvbridge/logins.json')

    def test_label_defaults_and_primary_defaults_false(self):
        b = {k: v for k, v in B.items() if k not in ('label', 'primary')}
        found = logins.validate(registry(A, b))
        self.assertEqual((found[1].label, found[1].primary), ('Login B', False))

    def assertInvalid(self, data, fragment):
        with self.assertRaises(ValueError) as cm:
            logins.validate(data)
        self.assertIn(fragment, str(cm.exception))

    def test_validation_errors(self):
        cases = [
            (dict(registry(A), schema='other'), 'schema'),
            ({'schema': logins.SCHEMA, 'logins': []}, 'non-empty list'),
            (registry(dict(A, id='A')), 'id must match'),
            (registry(dict(A, id='a' * 17)), 'id must match'),
            (registry(dict(A, id='1a')), 'id must match'),
            (registry(A, dict(B, primary=True)), 'exactly one login must be primary'),
            (registry(dict(A, primary=False), B), 'exactly one login must be primary'),
            (registry(A, dict(B, id='a')), 'listed more than once'),
            (registry(A, dict(B, devtools_port=9556)), 'reserved by the login guard'),
            (registry(A, dict(B, devtools_port=9555)), 'share the same port'),
            (registry(A, dict(B, bridge_url='http://127.0.0.1:8777')), 'share the same port'),
            (registry(A, dict(B, devtools_port=8778)), 'same port for DevTools and its bridge'),
            (registry(A, dict(B, config=A['config'])), 'share the same config'),
            (registry(A, dict(B, token_env='TVBRIDGE_TOKEN')), 'share the same token_env'),
            (registry(A, dict(B, guard_unit='tvbridge-login-guard.service')), 'share the same unit'),
            (registry(A, dict(B, guard_state_dir=A['guard_state_dir'])), 'share the same guard_state_dir'),
            (registry(dict(A, devtools_port='9555')), 'devtools_port must be a port number'),
            (registry(dict(A, bridge_url='127.0.0.1:8777')), 'bridge_url must be an http URL'),
            (registry(dict(A, bridge_url='http://127.0.0.1')), 'bridge_url must be an http URL'),
            (registry(dict(A, token_env='lower')), 'token_env must be'),
            (registry(dict(A, daemon_unit='tvbridge-daemon')), '.service'),
            (registry(dict(A, primary='yes')), 'primary must be true or false'),
            (registry(dict(A, read_token_env='lower')), 'read_token_env must be'),
            (registry(dict(A, read_token_env='TVBRIDGE_TOKEN')), 'must differ from its token_env'),
            (registry(A, dict(B, read_token_env='TVBRIDGE_TOKEN')), 'share the same token_env'),
            (registry(dict(A, read_token_env='R'), dict(B, read_token_env='R')), 'share the same token_env'),
            (registry({k: v for k, v in A.items() if k != 'guard_state_dir'}), 'missing guard_state_dir'),
        ]
        for data, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertInvalid(data, fragment)

    def test_read_token_env_is_optional(self):
        found = logins.validate(registry(dict(A, read_token_env='TVBRIDGE_READ_TOKEN_A'), B))
        self.assertEqual([x.read_token_env for x in found], ['TVBRIDGE_READ_TOKEN_A', None])

    def test_unreadable_json_is_a_clear_value_error(self):
        with self.assertRaisesRegex(ValueError, 'not readable JSON'):
            logins.load_logins(self.write('{nope'))


class DaemonSettings(unittest.TestCase):
    def test_defaults_are_todays_daemon(self):
        s = logins.daemon_settings({'account_name': 'X'})
        self.assertEqual(s, {'login_id': 'a', 'label': 'Login A', 'mode': 'main', 'token_env': 'TVBRIDGE_TOKEN',
                             'read_token_env': None, 'peer_hedge_scope': 'configured',
                             'master_kill_switch_file': None, 'peers': [], 'explicit': False})

    def test_login_b_followers_only(self):
        s = logins.daemon_settings({
            'login_id': 'b', 'login_label': 'Login B', 'mode': 'followers_only',
            'master_kill_switch_file': '/x/ENTRIES-DISABLED',
            'peer_logins': [{'id': 'a', 'url': 'http://127.0.0.1:8777/', 'token_env': 'TVBRIDGE_TOKEN_A'}]})
        self.assertEqual((s['login_id'], s['label'], s['mode'], s['explicit']), ('b', 'Login B', 'followers_only', True))
        self.assertEqual(s['peers'], [{'id': 'a', 'url': 'http://127.0.0.1:8777', 'token_env': 'TVBRIDGE_TOKEN_A'}])

    def test_login_b_example_config_is_valid(self):
        from tvbridge import followers
        cfg = json.loads((Path(harness.ROOT) / 'config.login-b.example.json').read_text())
        s = logins.daemon_settings(cfg)
        self.assertEqual((s['login_id'], s['mode'], s['token_env'], [p['id'] for p in s['peers']]),
                         ('b', 'followers_only', 'TVBRIDGE_TOKEN_B', ['a']))
        # Peers are read with read tokens; this login accepts its own read token for /login and /health.
        self.assertEqual((s['read_token_env'], s['peers'][0]['token_env'], s['peer_hedge_scope']),
                         ('TVBRIDGE_READ_TOKEN_B', 'TVBRIDGE_READ_TOKEN_A', 'configured'))
        self.assertEqual(followers.parse(cfg), [])
        self.assertEqual(len(followers.parse(dict(cfg, followers=cfg['_followers_example']))), 1)

    def test_explicit_only_when_named(self):
        self.assertTrue(logins.daemon_settings({'login_id': 'a'})['explicit'])
        self.assertTrue(logins.daemon_settings({'mode': 'followers_only'})['explicit'])
        self.assertFalse(logins.daemon_settings({'mode': 'main'})['explicit'])

    def test_invalid(self):
        peer = {'id': 'b', 'url': 'http://127.0.0.1:8778', 'token_env': 'TVBRIDGE_TOKEN_B'}
        cases = [
            ({'login_id': 'B'}, 'login_id'),
            ({'login_label': ''}, 'login_label'),
            ({'mode': 'leader'}, 'mode'),
            ({'token_env': 'x'}, 'token_env'),
            ({'master_kill_switch_file': 'relative'}, 'absolute path'),
            ({'master_kill_switch_file': '/k/OWN', 'kill_switch_file': '/k/OWN'}, 'own kill-switch'),
            ({'master_kill_switch_file': '/k/F', 'followers': [{'kill_switch_file': '/k/F'}]}, 'own kill-switch'),
            ({'peer_logins': {}}, 'must be a list'),
            ({'peer_logins': [dict(peer, extra=1)]}, 'must be {'),
            ({'peer_logins': [dict(peer, id='a')]}, 'other than this login'),
            ({'peer_logins': [peer, peer]}, 'other than this login'),
            ({'peer_logins': [dict(peer, url='ftp://x:1')]}, 'http URL'),
            ({'peer_logins': [dict(peer, token_env='bad name')]}, 'token_env'),
            ({'read_token_env': 'lower'}, 'read_token_env'),
            ({'read_token_env': 'TVBRIDGE_TOKEN'}, 'read_token_env'),
            ({'token_env': 'T_B', 'read_token_env': 'T_B'}, 'read_token_env'),
            ({'peer_hedge_scope': 'peers'}, 'peer_hedge_scope'),
        ]
        for cfg, fragment in cases:
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError) as cm:
                    logins.daemon_settings(cfg)
                self.assertIn(fragment, str(cm.exception))


if __name__ == '__main__':
    unittest.main()
