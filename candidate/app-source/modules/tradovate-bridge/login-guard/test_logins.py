import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

import logins

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
SPEC = {'schema': 'decrypt.tvbridge-logins.v1', 'logins': [A, B]}


def registry(**changes):
    """SPEC with per-login changes: registry(b={'devtools_port': 9556})."""
    data = copy.deepcopy(SPEC)
    for entry in data['logins']:
        entry.update(changes.get(entry['id'], {}))
    return data


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / 'logins.json'

    def write(self, data):
        self.path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return self.path

    def test_spec_example_loads_and_expands_home(self):
        a, b = logins.load_logins(self.write(SPEC))
        self.assertEqual((a.id, a.primary, a.devtools_port, a.bridge_port), ('a', True, 9555, 8777))
        self.assertEqual((b.id, b.primary, b.devtools_port, b.bridge_port), ('b', False, 9557, 8778))
        self.assertEqual(b.guard_state_dir, os.path.expanduser('~/.local/share/decrypt-os/tvbridge-state/login-guard-b'))
        self.assertEqual(b.config, os.path.expanduser('~/.local/share/decrypt-os/tvbridge/logins/b/config.json'))
        self.assertEqual(logins.primary([b, a]), a)
        self.assertEqual(logins.by_id([a, b], 'b'), b)
        with self.assertRaises(ValueError):
            logins.by_id([a, b], 'c')

    def test_missing_file_is_implicit_login_a_with_todays_defaults(self):
        only = logins.load_logins(self.dir / 'absent.json')
        self.assertEqual(only, [logins.DEFAULT_LOGIN])
        a = only[0]
        self.assertEqual((a.id, a.devtools_port, a.bridge_port, a.token_env, a.browser_unit, a.daemon_unit,
                          a.guard_unit, a.trigger_unit),
                         ('a', 9555, 8777, 'TVBRIDGE_TOKEN', 'tvbridge-browser.service', 'tvbridge-daemon.service',
                          'tvbridge-login-guard.service', 'tvbridge-login-trigger.service'))
        self.assertEqual(a.guard_state_dir, str(Path.home() / '.local/share/decrypt-os/tvbridge-state/login-guard'))

    def test_env_override_and_default_path(self):
        old = os.environ.get('TVBRIDGE_LOGINS')
        try:
            os.environ['TVBRIDGE_LOGINS'] = str(self.write(SPEC))
            self.assertEqual([l.id for l in logins.load_logins()], ['a', 'b'])
            del os.environ['TVBRIDGE_LOGINS']
            self.assertEqual(logins.registry_path(),
                             Path(os.path.expanduser('~/.local/share/decrypt-os/tvbridge/logins.json')))
        finally:
            if old is None:
                os.environ.pop('TVBRIDGE_LOGINS', None)
            else:
                os.environ['TVBRIDGE_LOGINS'] = old

    def test_label_defaults(self):
        data = registry()
        del data['logins'][1]['label']
        self.assertEqual(logins.load_logins(self.write(data))[1].label, 'Login B')

    def test_read_token_env_is_optional_and_carried(self):
        a, b = logins.load_logins(self.write(SPEC))
        self.assertEqual((a.read_token_env, b.read_token_env), (None, None))
        self.assertIsNone(logins.DEFAULT_LOGIN.read_token_env)
        a, b = logins.load_logins(self.write(registry(a={'read_token_env': 'TVBRIDGE_READ_TOKEN_A'},
                                                      b={'read_token_env': 'TVBRIDGE_READ_TOKEN_B'})))
        self.assertEqual((a.read_token_env, b.read_token_env), ('TVBRIDGE_READ_TOKEN_A', 'TVBRIDGE_READ_TOKEN_B'))
        self.assertEqual((a.token_env, b.token_env), ('TVBRIDGE_TOKEN', 'TVBRIDGE_TOKEN_B'))   # own tokens unchanged

    def test_read_token_env_rules(self):
        cases = [
            (registry(b={'read_token_env': 'read-b'}), 'read_token_env must be an environment variable name'),
            (registry(b={'read_token_env': ''}), 'read_token_env must be an environment variable name'),
            (registry(b={'read_token_env': None}), 'read_token_env must be an environment variable name'),
            (registry(b={'read_token_env': 7}), 'read_token_env must be an environment variable name'),
            (registry(b={'read_token_env': 'TVBRIDGE_TOKEN_B'}),
             'login b: read_token_env TVBRIDGE_TOKEN_B is the token_env of login b'),
            (registry(b={'read_token_env': 'TVBRIDGE_TOKEN'}),
             'login b: read_token_env TVBRIDGE_TOKEN is the token_env of login a'),
            (registry(a={'read_token_env': 'TVBRIDGE_TOKEN_B'}),
             'login a: read_token_env TVBRIDGE_TOKEN_B is the token_env of login b'),
            (registry(a={'read_token_env': 'TVBRIDGE_READ'}, b={'read_token_env': 'TVBRIDGE_READ'}),
             'login b: read_token_env TVBRIDGE_READ is already used by login a'),
        ]
        for data, text in cases:
            with self.subTest(text=text):
                self.assertRefused(data, text)

    def assertRefused(self, data, text):
        with self.assertRaises(ValueError) as ctx:
            logins.load_logins(self.write(data))
        self.assertIn(text, str(ctx.exception))

    def test_invalid_registries_raise_clear_errors(self):
        cases = [
            ('not json', 'not valid JSON'),
            ([], 'JSON object'),
            (dict(SPEC, schema='other'), 'schema'),
            (dict(SPEC, logins=[]), 'non-empty list'),
            (registry(b={'id': 'B'}), '.id must match'),
            (registry(b={'id': 'b' * 17}), '.id must match'),
            (registry(b={'id': '1b'}), '.id must match'),
            (registry(b={'devtools_port': 9556}), 'reserved by the login guard'),
            (registry(b={'devtools_port': '9557'}), 'integer port'),
            (registry(b={'devtools_port': True}), 'integer port'),
            (registry(b={'primary': True}), 'exactly one primary'),
            (registry(a={'primary': False}), 'exactly one primary'),
            (registry(b={'primary': 'no'}), 'primary must be true or false'),
            (registry(b={'id': 'a'}), 'id a is already used'),
            (registry(b={'devtools_port': 9555}), 'port 9555 is already used'),
            (registry(b={'bridge_url': 'http://127.0.0.1:8777'}), 'port 8777 is already used'),
            (registry(b={'devtools_port': 8778}), 'port 8778 is already used'),
            (registry(b={'config': '~/.local/share/decrypt-os/tvbridge/config.json'}), 'config'),
            (registry(b={'token_env': 'TVBRIDGE_TOKEN'}), 'token_env TVBRIDGE_TOKEN is already used'),
            (registry(b={'token_env': 'bad name'}), 'environment variable name'),
            (registry(b={'daemon_unit': 'tvbridge-daemon.service'}), 'unit tvbridge-daemon.service is already used'),
            (registry(b={'guard_unit': 'tvbridge-login-guard@b.timer'}), 'must name a .service unit'),
            (registry(b={'guard_state_dir': '~/.local/share/decrypt-os/tvbridge-state/login-guard'}), 'guard_state_dir'),
            (registry(b={'bridge_url': 'ftp://127.0.0.1:8778'}), 'http URL with a port'),
            (registry(b={'bridge_url': 'http://127.0.0.1'}), 'http URL with a port'),
            (registry(b={'bridge_url': 'http://127.0.0.1:8778/health'}), 'http URL with a port'),
            (registry(b={'bridge_url': 'http://127.0.0.1:99999'}), 'http URL with a port'),
            (registry(b={'config': ''}), 'config must be a non-empty string'),
        ]
        for data, text in cases:
            with self.subTest(text=text, data=data if not isinstance(data, dict) else None):
                self.assertRefused(data, text)
        missing = registry()
        del missing['logins'][1]['guard_unit']
        self.assertRefused(missing, 'guard_unit must be a non-empty string')


if __name__ == '__main__':
    unittest.main()
