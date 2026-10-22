"""scripts/install.py add-login (1.1.0 multi-login) in temp directories only.

No release, systemctl, network or real profile is involved; tokens must never appear in output.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge import logins as login_registry  # noqa: E402

INSTALL = ROOT / 'scripts' / 'install.py'
PRIMARY_TOKEN = 'p' * 43


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def snapshot(base):
    out = {}
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            rel = os.path.relpath(p, base)
            if rel == 'home/Library' or rel.startswith('home/Library/'):
                continue
            if stat.S_ISDIR(st.st_mode):
                out[rel] = ('dir', stat.S_IMODE(st.st_mode))
            else:
                with open(p, 'rb') as fh:
                    out[rel] = ('file', stat.S_IMODE(st.st_mode), st.st_mtime_ns,
                                hashlib.sha256(fh.read()).hexdigest())
    return out


class AddLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='tvb-addlogin-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / 'root'
        self.root.mkdir(mode=0o700)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.profile = self.tmp / 'browser' / 'profile'
        self.config = self.root / 'config.json'
        self.kill = self.root / 'ENTRIES-DISABLED'
        cfg = json.loads((ROOT / 'config.example.json').read_text())
        cfg.update(account_name='DEMO-A', soup_account_id='account-a', kill_switch_file=str(self.kill),
                   journal_dir=str(self.root / 'journal'),
                   followers=[{'key': 'x'}])  # copied nowhere; never parsed here
        self.config.write_text(json.dumps(cfg, indent=2) + '\n')
        os.chmod(self.config, 0o600)
        (self.root / 'tvbridge.env').write_text(f'TVBRIDGE_TOKEN={PRIMARY_TOKEN}\n')
        os.chmod(self.root / 'tvbridge.env', 0o600)
        self.outputs = []

    def tearDown(self):
        tokens = [PRIMARY_TOKEN] + [self.token(i) for i in ('b', 'c') if self.token(i)]
        peers = self.root / 'logins' / 'peers.env'
        if peers.is_file():
            tokens += list(self.env_values(peers).values())
        for out in self.outputs:
            for t in tokens:
                self.assertNotIn(t, out, 'a token leaked into output')

    def run_tool(self, *extra):
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        argv = [sys.executable, str(INSTALL), 'add-login', '--root', str(self.root), '--unit-dir',
                str(self.tmp / 'units'), '--profile-dir', str(self.profile), *extra]
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        self.outputs.append(proc.stdout + proc.stderr)
        return proc

    def add(self, *extra):
        proc = self.run_tool(*extra)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def token(self, login_id):
        path = self.root / 'logins' / login_id / 'tvbridge.env'
        if not path.is_file():
            return None
        values = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line
                      and not line.startswith('#'))
        return values.get('TVBRIDGE_TOKEN')

    def env_values(self, path):
        return dict(line.split('=', 1) for line in Path(path).read_text().splitlines()
                    if '=' in line and not line.startswith('#'))

    def backups(self, name):
        return sorted(p for p in self.root.iterdir() if p.name.startswith(name + '.bak-'))

    # ------------------------------------------------------------ tests

    def test_dry_run_changes_nothing(self):
        before = snapshot(self.tmp)
        proc = self.add('b', '--dry-run')
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn('DevTools 9557, loopback 8778', proc.stdout)
        for text in ('logins/b/config.json', 'login.env', 'tvbridge.env', 'peers.env', 'firing.env', 'logins.json',
                     'config.json.bak-', '"mode": "followers_only"', 'tvbridge-daemon@b.service', 'To apply:'):
            self.assertIn(text, proc.stdout)

    def test_first_login_creates_layout_registry_and_peers(self):
        primary_before = json.loads(self.config.read_text())
        proc = self.add('b')
        home = self.root / 'logins' / 'b'
        for d in (self.root / 'logins', home, home / 'journal'):
            self.assertEqual(mode(d), 0o700, d)
        for f in ('config.json', 'login.env', 'tvbridge.env', 'ENTRIES-DISABLED'):
            self.assertEqual(mode(home / f), 0o600, f)
        self.assertEqual(mode(self.root / 'logins' / 'peers.env'), 0o600)
        self.assertEqual(mode(self.root / 'logins' / 'firing.env'), 0o600)

        cfg = json.loads((home / 'config.json').read_text())
        self.assertEqual(cfg['mode'], 'followers_only')
        self.assertEqual(cfg['followers'], [])
        self.assertEqual((cfg['login_id'], cfg['login_label']), ('b', 'Login B'))
        self.assertEqual((cfg['devtools_port'], cfg['listen_port']), (9557, 8778))
        self.assertEqual(cfg['master_kill_switch_file'], str(self.kill))
        self.assertEqual(cfg['kill_switch_file'], str(home / 'ENTRIES-DISABLED'))
        self.assertEqual(cfg['journal_dir'], str(home / 'journal'))
        self.assertEqual(cfg['token_env'], 'TVBRIDGE_TOKEN_B')
        self.assertEqual(cfg['read_token_env'], 'TVBRIDGE_READ_TOKEN_B')
        self.assertEqual(cfg['publish_mode'], 'evidence')
        self.assertEqual(cfg['peer_logins'], [{'id': 'a', 'url': 'http://127.0.0.1:8777',
                                               'token_env': 'TVBRIDGE_READ_TOKEN_A'}])
        for key in ('account_name', 'account_id', 'soup_account_id', 'soup_base_url', 'soup_adapter_db'):
            self.assertNotIn(key, cfg)
        self.assertEqual(cfg['ticker'], primary_before['ticker'])
        settings = login_registry.daemon_settings(cfg)
        self.assertEqual((settings['mode'], settings['login_id']), ('followers_only', 'b'))

        login_env = self.env_values(home / 'login.env')
        profile_b = self.profile.parent / 'profile-b'
        self.assertEqual(login_env, {'TVBRIDGE_DEVTOOLS_PORT': '9557', 'TVBRIDGE_PROFILE': str(profile_b)})
        self.assertEqual(mode(profile_b), 0o700)
        self.assertEqual((profile_b / '.tvbridge-login').read_text(), 'b\n')

        secret = self.env_values(home / 'tvbridge.env')
        self.assertGreaterEqual(len(secret['TVBRIDGE_TOKEN']), 43)
        self.assertEqual(secret['TVBRIDGE_TOKEN_B'], secret['TVBRIDGE_TOKEN'])
        self.assertNotEqual(secret['TVBRIDGE_TOKEN'], PRIMARY_TOKEN)
        # peers.env: read-only tokens only, one fresh random token per login; never an order token.
        peers = self.env_values(self.root / 'logins' / 'peers.env')
        self.assertEqual(sorted(peers), ['TVBRIDGE_READ_TOKEN_A', 'TVBRIDGE_READ_TOKEN_B'])
        self.assertEqual(len({peers['TVBRIDGE_READ_TOKEN_A'], peers['TVBRIDGE_READ_TOKEN_B'],
                              PRIMARY_TOKEN, secret['TVBRIDGE_TOKEN']}), 4)
        self.assertTrue(all(len(v) >= 43 for v in peers.values()))
        # firing.env: login b's order token, for the firing service only.
        self.assertEqual(self.env_values(self.root / 'logins' / 'firing.env'),
                         {'TVBRIDGE_TOKEN_B': secret['TVBRIDGE_TOKEN']})

        a, b = login_registry.load_logins(self.root / 'logins.json')
        self.assertEqual((a.id, a.primary, a.devtools_port, a.bridge_url, a.token_env, a.config),
                         ('a', True, 9555, 'http://127.0.0.1:8777', 'TVBRIDGE_TOKEN', str(self.config)))
        self.assertEqual((b.id, b.primary, b.devtools_port, b.bridge_url, b.token_env, b.config),
                         ('b', False, 9557, 'http://127.0.0.1:8778', 'TVBRIDGE_TOKEN_B', str(home / 'config.json')))
        self.assertEqual((a.read_token_env, b.read_token_env), ('TVBRIDGE_READ_TOKEN_A', 'TVBRIDGE_READ_TOKEN_B'))
        self.assertEqual((b.browser_unit, b.daemon_unit, b.guard_unit, b.trigger_unit),
                         ('tvbridge-browser@b.service', 'tvbridge-daemon@b.service',
                          'tvbridge-login-guard@b.service', 'tvbridge-login-trigger@b.service'))
        self.assertEqual(b.guard_state_dir, os.path.expanduser('~/.local/share/decrypt-os/tvbridge-state/login-guard-b'))
        self.assertEqual(json.loads((self.root / 'logins.json').read_text())['logins'][1]['guard_state_dir'],
                         '~/.local/share/decrypt-os/tvbridge-state/login-guard-b')

        primary = json.loads(self.config.read_text())
        self.assertEqual(primary.pop('peer_logins'), [{'id': 'b', 'url': 'http://127.0.0.1:8778',
                                                       'token_env': 'TVBRIDGE_READ_TOKEN_B'}])
        self.assertEqual(primary.pop('read_token_env'), 'TVBRIDGE_READ_TOKEN_A')
        self.assertEqual(primary, primary_before)
        self.assertEqual(login_registry.daemon_settings(json.loads(self.config.read_text()))['read_token_env'],
                         'TVBRIDGE_READ_TOKEN_A')
        self.assertEqual(mode(self.config), 0o600)
        [backup] = self.backups('config.json')
        self.assertEqual(json.loads(backup.read_text()), primary_before)
        self.assertEqual(mode(backup), 0o600)
        self.assertEqual(self.env_values(self.root / 'tvbridge.env'), {'TVBRIDGE_TOKEN': PRIMARY_TOKEN})
        self.assertFalse(self.kill.exists())  # the primary's kill switch is never created or removed
        self.assertIn('systemctl --user start tvbridge-browser@b.service', proc.stdout)
        self.assertIn('Load it in tvbridge-firing.service ONLY', proc.stdout)
        self.assertIn('Nothing was enabled or started', proc.stdout)

    def test_second_login_skips_used_ports_and_status_panel(self):
        self.add('b')
        b_config = (self.root / 'logins' / 'b' / 'config.json').read_bytes()
        peers_b = self.env_values(self.root / 'logins' / 'peers.env')
        self.add('c', '--label', 'Spare')
        logins = login_registry.load_logins(self.root / 'logins.json')
        self.assertEqual([l.id for l in logins], ['a', 'b', 'c'])
        c = logins[2]
        self.assertEqual((c.devtools_port, c.bridge_url, c.label), (9558, 'http://127.0.0.1:8780', 'Spare'))
        cfg = json.loads((self.root / 'logins' / 'c' / 'config.json').read_text())
        self.assertEqual([p['id'] for p in cfg['peer_logins']], ['a', 'b'])
        self.assertEqual(cfg['peer_logins'][1], {'id': 'b', 'url': 'http://127.0.0.1:8778',
                                                 'token_env': 'TVBRIDGE_READ_TOKEN_B'})
        self.assertEqual(cfg['peer_logins'][0]['token_env'], 'TVBRIDGE_READ_TOKEN_A')
        primary = json.loads(self.config.read_text())
        self.assertEqual([p['id'] for p in primary['peer_logins']], ['b', 'c'])
        self.assertEqual((self.root / 'logins' / 'b' / 'config.json').read_bytes(), b_config)
        peers = self.env_values(self.root / 'logins' / 'peers.env')
        self.assertEqual(sorted(peers), ['TVBRIDGE_READ_TOKEN_A', 'TVBRIDGE_READ_TOKEN_B', 'TVBRIDGE_READ_TOKEN_C'])
        self.assertEqual({k: peers[k] for k in ('TVBRIDGE_READ_TOKEN_A', 'TVBRIDGE_READ_TOKEN_B')}, peers_b,
                         'existing read tokens are kept')
        self.assertNotIn(self.token('b'), peers.values())
        self.assertEqual(self.env_values(self.root / 'logins' / 'firing.env'),
                         {'TVBRIDGE_TOKEN_B': self.token('b'), 'TVBRIDGE_TOKEN_C': self.token('c')})
        self.assertEqual(json.loads(self.config.read_text())['read_token_env'], 'TVBRIDGE_READ_TOKEN_A')
        self.assertEqual(len(self.backups('logins.json')), 1)
        self.assertEqual(len(self.backups('config.json')), 2)  # one per add-login, even within one second
        self.assertEqual(len(set(self.token(i) for i in 'bc') | {PRIMARY_TOKEN}), 3)

    def test_new_logins_never_take_service_ports(self):
        # 8779 Bridge window, 8781 Bridge Advanced, 8783 web-reader proxy: never a login's bridge port.
        for login_id in 'bcde':
            self.add(login_id)
        ports = {l.id: (l.devtools_port, l.bridge_url) for l in login_registry.load_logins(self.root / 'logins.json')}
        self.assertEqual(ports['d'], (9559, 'http://127.0.0.1:8782'))
        self.assertEqual(ports['e'], (9560, 'http://127.0.0.1:8784'))

    def test_next_free_stays_inside_the_guarded_ranges(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('tvbridge_install_under_test', INSTALL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.next_free(8780, set(), mod.LAST_LISTEN_PORT), 8780)
        self.assertEqual(mod.next_free(8781, set(), mod.LAST_LISTEN_PORT), 8782)
        self.assertEqual(mod.next_free(8783, set(), mod.LAST_LISTEN_PORT), 8784)
        with self.assertRaises(mod.Refused):
            mod.next_free(8778, set(range(8778, 8800)), mod.LAST_LISTEN_PORT)
        with self.assertRaises(mod.Refused):
            mod.next_free(9557, set(range(9557, 9600)), mod.LAST_DEVTOOLS_PORT)

    def test_refusals_change_nothing(self):
        self.add('b')
        before = snapshot(self.tmp)
        cases = [('B', 'must match'), ('1x', 'must match'), ('b', 'already in'), ('a', 'already in'),
                 ('x' * 17, 'must match')]
        for login_id, text in cases:
            proc = self.run_tool(login_id)
            self.assertEqual(proc.returncode, 1, login_id)
            self.assertIn(text, proc.stderr)
        stray = self.profile.parent / 'profile-d'
        (stray / 'Default').mkdir(parents=True)
        before = snapshot(self.tmp)
        proc = self.run_tool('d')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('already holds another browser profile', proc.stderr)
        proc = self.run_tool('e', '--login-profile-dir', str(self.profile))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("primary login's profile", proc.stderr)
        self.assertEqual(snapshot(self.tmp), before)

    def test_refuses_without_primary_config_or_with_invalid_registry(self):
        self.config.unlink()
        proc = self.run_tool('b')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('primary config', proc.stderr)
        self.assertFalse((self.root / 'logins').exists())
        (self.root / 'logins.json').write_text('{"schema": "decrypt.tvbridge-logins.v1", "logins": []}')
        proc = self.run_tool('b')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('logins registry', proc.stderr)
        self.assertFalse((self.root / 'logins').exists())


if __name__ == '__main__':
    unittest.main()
