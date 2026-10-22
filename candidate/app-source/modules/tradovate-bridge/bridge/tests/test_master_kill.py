"""1.1.0 multi-login: `master_kill_switch_file`, another login's main kill switch, read only.

While the file exists every new entry on the daemon (main and followers) is refused exactly as by
its own kill switch. The daemon never creates or removes it; auto-protect and the firing service's
/follower/.../kill keep writing only the account's own kill-switch file. In-memory doubles only.
"""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness  # noqa: F401  (puts the repository on sys.path)

from tvbridge import followers
from tvbridge.daemon import make_handler
from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.state import AccountState
from test_follower_balances import Page, Pub, follower_entry

TOKEN = 'master-kill-switch-token-0123456789abc'


class MasterKillSwitch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.master = self.tmp / 'login-a' / 'ENTRIES-DISABLED'
        self.master.parent.mkdir()
        self.state = AccountState()
        self.page = Page(self.state, None)
        self.cfg = {'ticker': 'MNQZ2021', 'account_name': 'MAIN-ACCT', 'account_id': 7, 'soup_account_id': 'S',
                    'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0, 'entry_settle_s': 0,
                    'journal_dir': str(self.tmp / 'journal'), 'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED'),
                    'master_kill_switch_file': str(self.master),
                    'followers': [follower_entry('f1', 'F-ONE', self.tmp)]}
        self.main = Executor(self.page, self.state, Journal(self.cfg['journal_dir']), self.cfg)
        self.main.resolve()
        self.fx = followers.build(self.page, self.state, self.cfg, self.main)
        self.seq = 0

    def payload(self, side='long'):
        self.seq += 1
        return {'ticker': 'MNQZ2021', 'action': 'buy' if side == 'long' else 'sell', 'orderType': 'market',
                'quantity': 1, 'extras': {'soup_command_id': f'c{self.seq}',
                                          'soup_trade_id': f'soup:MNQZ2021:{self.seq}:{side}'}}

    def placed(self):
        return [r for r in self.page.requests if r == 'order/placeorder']

    def test_master_switch_refuses_main_and_follower_entries(self):
        self.master.touch()
        for ex in (self.main, self.fx['f1']):
            with self.subTest(account=getattr(ex, 'key', 'main')):
                self.assertTrue(ex.entries_disabled())
                self.assertTrue(ex.master_kill_active())
                outcome, receipt = ex.send(self.payload())
                self.assertEqual((outcome, receipt['reason']), ('rejected', 'entries disabled by kill switch'))
        self.assertEqual(self.placed(), [])
        self.master.unlink()
        self.assertFalse(self.main.entries_disabled())
        self.assertEqual(self.main.send(self.payload())[0], 'accepted')
        self.assertEqual(self.fx['f1'].send(self.payload())[0], 'accepted')

    def test_auto_protect_and_follower_kill_write_only_their_own_file(self):
        with self.assertLogs('tvbridge', level='ERROR'):
            self.assertTrue(self.main._set_kill_switch('auto-protect: test'))
            self.assertTrue(self.fx['f1']._set_kill_switch('firing: test'))
        self.assertTrue(Path(self.cfg['kill_switch_file']).exists())
        self.assertTrue(Path(self.cfg['followers'][0]['kill_switch_file']).exists())
        self.assertFalse(self.master.exists(), 'the master switch is never created by this daemon')
        self.master.touch()
        Path(self.cfg['kill_switch_file']).unlink()
        self.assertTrue(self.main.entries_disabled())
        self.assertTrue(self.master.exists(), 'and never removed')

    def test_followers_report_shows_entries_disabled(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), followers.make_follower_handler(
            make_handler(self.main, Pub(), TOKEN), self.fx, TOKEN, self.main))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:%d' % server.server_address[1]

        def get(path):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return json.loads(r.read())

        self.assertFalse(get('/followers/' + TOKEN)['followers'][0]['entries_disabled'])
        self.master.touch()
        report = get('/followers/' + TOKEN)
        self.assertTrue(report['followers'][0]['entries_disabled'])
        self.assertTrue(report['main']['entries_disabled'])
        self.assertTrue(get('/health/' + TOKEN)['entries_disabled'])

    def test_without_the_key_nothing_changes(self):
        cfg = {k: v for k, v in self.cfg.items() if k != 'master_kill_switch_file'}
        parsed = followers.parse(cfg)
        self.assertNotIn('master_kill_switch_file', parsed[0][1])
        self.assertEqual(followers.parse(self.cfg)[0][1]['master_kill_switch_file'], str(self.master))
        ex = Executor(self.page, self.state, Journal(self.tmp / 'j2'), dict(cfg, journal_dir=str(self.tmp / 'j2')))
        self.master.touch()
        self.assertFalse(ex.entries_disabled())


if __name__ == '__main__':
    unittest.main()
