"""setup.py in a temporary HOME. Never calls systemctl (init and status only); no browser."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent
SETUP = MODULE / 'setup.py'
sys.path.insert(0, str(MODULE / 'bridge'))
sys.path.insert(0, str(MODULE / 'login-guard'))
from tvbridge import firing, logins  # noqa: E402
import guard  # noqa: E402


class SetupInit(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='tvb-setup-'))
        self.addCleanup(__import__('shutil').rmtree, self.home, True)
        self.root = self.home / '.local/share/decrypt-os' / 'tvbridge'

    def run_setup(self, *argv):
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        return subprocess.run([sys.executable, str(SETUP), *argv], env=env, capture_output=True, text=True,
                              timeout=60)

    def init(self, *extra):
        return self.run_setup('init', '--account-name', 'SIM-ACCOUNT-1', '--soup-account-id', 'Sim 1', *extra)

    def test_init_creates_a_paused_login_a_without_starting_anything(self):
        proc = self.init()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = self.root
        self.assertEqual(json.loads((r / 'ENTRIES-DISABLED').read_text())['set_by'], 'tvbridge-admin',
                         'entries start paused, as a plain pause Bridge Advanced can resume')
        for name in ('config.json', 'tvbridge.env', 'logins.json', 'firing.json', 'login-guard.json',
                     'logins/peers.env', 'logins/manual-a.env'):
            self.assertEqual(stat.S_IMODE(os.stat(r / name).st_mode), 0o600, name)
        cfg = json.loads((r / 'config.json').read_text())
        self.assertEqual((cfg['account_name'], cfg['account_id'], cfg['soup_account_id']), ('SIM-ACCOUNT-1', None, 'Sim 1'))
        self.assertEqual(cfg['trade_url_pattern'], '^wss://demo\\.tradovateapi\\.com/v1/websocket')
        self.assertEqual(cfg['kill_switch_file'], str(r / 'ENTRIES-DISABLED'))
        self.assertEqual((cfg['listen_port'], cfg['devtools_port']), (8777, 9555))
        reg = logins.validate(json.loads((r / 'logins.json').read_text()))
        self.assertEqual([x.id for x in reg], ['a'])
        self.assertEqual(reg[0].read_token_env, 'TVBRIDGE_READ_TOKEN_A')
        fcfg = json.loads((r / 'firing.json').read_text())
        firing.validate(fcfg)
        self.assertEqual(fcfg['firing_order'], ['sim-1'])
        hold, err = guard.load_weekend_hold(r / 'login-guard.json')
        self.assertIsNone(err)
        self.assertEqual(hold.text(), 'Fri 6:00 PM to Sun 6:00 PM (America/New_York)')
        self.assertEqual(os.readlink(r / 'current'), str(MODULE / 'bridge'))
        profile = json.loads((self.home / '.local/share/decrypt-os/tvbridge-admin/profile.json').read_text())
        self.assertEqual((profile['mode'], profile['restarter'], profile['order_adapter']), ('live', 'systemd', 'bridge'))
        self.assertEqual(profile['tvbridge_root'], str(r))
        tokens = set()
        for name in ('tvbridge.env', 'logins/peers.env', 'logins/manual-a.env'):
            line = [ln for ln in (r / name).read_text().splitlines() if '=' in ln and not ln.startswith('#')][0]
            tokens.add(line.split('=', 1)[1])
            self.assertNotIn(line.split('=', 1)[1], proc.stdout)
        self.assertEqual(len(tokens), 3, 'order, read and flatten tokens differ')

    def test_init_never_overwrites_and_refuses_another_account(self):
        self.assertEqual(self.init().returncode, 0)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        again = self.init()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)
        other = self.run_setup('init', '--account-name', 'SIM-ACCOUNT-2', '--soup-account-id', 'Sim 2')
        self.assertEqual(other.returncode, 1)
        self.assertIn('already names another account', other.stderr)

    def test_refuses_placeholders_and_bad_values(self):
        for argv in (('--account-name', 'DEMO-ACCOUNT-NAME-FROM-RECON', '--soup-account-id', 'x'),
                     ('--account-name', 'A', '--soup-account-id', '<id>'),
                     ('--account-name', 'A', '--soup-account-id', 'x', '--ticker', 'MNQ1!'),
                     ('--account-name', 'A', '--soup-account-id', 'x', '--tick-size', '0')):
            proc = self.run_setup('init', *argv)
            self.assertEqual(proc.returncode, 1, argv)
        self.assertFalse(self.root.exists())

    def test_retired_max_contracts_flag_is_ignored_and_never_written(self):
        # No size cap: the strategy alert sets the size. Older scripts may still pass the flag.
        proc = self.init('--max-contracts', '40')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('max_contracts', json.loads((self.root / 'config.json').read_text()))

    def test_status_reports_paused_and_engine(self):
        self.init()
        proc = self.run_setup('status')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = json.loads(proc.stdout)
        self.assertEqual((st['configured'], st['paused'], st['firing_engine_found']), (True, True, False))


if __name__ == '__main__':
    unittest.main()
