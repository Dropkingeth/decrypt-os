"""Second webhook "W2" registered as off: no strategy, no accounts, no stream ID; nothing runs. VM only.
Each test runs on a fresh copy of sandbox/ in $HOME/.cache (test_admin.Base).

    cd admin && TMPDIR=$HOME/.cache PYTHONDONTWRITEBYTECODE=1 \
        python3 -m unittest tests.test_webhook2 -v
"""
import glob
import hashlib
import json
import os
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_admin import Base  # noqa: E402
from engine import OFF_NOTE  # noqa: E402
from model import Invalid  # noqa: E402

OFF = 'W2 is off and waiting for a strategy.'


class W2Base(Base):
    def reg(self, name='W2'):
        return self.do('register_webhook', {'name': name})

    def registry(self):
        with open(self.profile.st('webhooks.json')) as f:
            return json.load(f)

    def write_registry(self, doc):
        with open(self.profile.st('webhooks.json'), 'w') as f:
            json.dump(doc, f, indent=2)

    def wh(self, wid):
        return next((w for w in self.e.overview()['webhooks'] if w['id'] == wid), None)

    def tree_hashes(self):
        out = {}
        for p in sorted(glob.glob(os.path.join(self.tmp, 'tree/**/*'), recursive=True)):
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    out[p] = hashlib.sha256(f.read()).hexdigest()
        return out

    def refused(self, ctype, params, phrase):
        with self.assertRaises(Invalid, msg=ctype) as cm:
            self.e.preview(ctype, params)
        self.assertIn(OFF, str(cm.exception))
        self.assertIn(phrase, str(cm.exception))


class TestRegister(W2Base):
    def test_register_off(self):
        tree_before = self.tree_hashes()
        ch = self.reg()
        self.assertEqual(ch['status'], 'applied', ch.get('error'))
        p = ch['plan']
        self.assertEqual((p['immediate'], p['requires_flat'], p['restarts'], p['wait_on']), (False, False, [], []))
        self.assertEqual([f['path'] for f in p['files']], ['admin/webhooks.json'])
        path = p['generated']['path']
        self.assertRegex(path, r'^/webhook/w2-[0-9a-f]{12}$')   # no Linux user name, not guessable
        self.assertEqual(p['generated'], {'path': path, 'port': 8800,
                                          'url': f'https://{self.profile.public_host}{path}'})
        w = next(x for x in self.registry()['webhooks'] if x['id'] == 'w2')
        self.assertEqual({k: w[k] for k in ('name', 'state', 'strategy', 'leader', 'followers', 'disabled',
                                            'stream_id', 'ticker', 'paused', 'port', 'path', 'change_id')},
                         {'name': 'W2', 'state': 'off', 'strategy': None, 'leader': None, 'followers': [],
                          'disabled': [], 'stream_id': None, 'ticker': None, 'paused': None, 'port': 8800,
                          'path': path, 'change_id': ch['id']})
        self.assertIsInstance(w['created_at'], float)
        self.assertEqual([x['id'] for x in self.registry()['webhooks']], ['main', 'w2'])
        top = self.e.audit.read(1)[0]
        self.assertEqual((top['change_id'], top['type'], top['status']), (ch['id'], 'register_webhook', 'applied'))
        self.assertTrue(self.e.audit.verify_chain())
        self.assertEqual(self.tree_hashes(), tree_before)   # bridge, Soup and tunnel files untouched
        self.assertEqual(self.e.restarter.name, 'simulated')
        self.assertFalse((ch['result'] or {}).get('restart_log'))

    def test_first_free_port(self):
        self.do('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-b'})
        ch = self.reg()
        self.assertEqual(ch['plan']['generated']['port'], 8801)

    def test_rollback_register(self):
        ch = self.reg()
        rb = self.e.rollback_preview(ch['id'])
        self.assertFalse(rb['plan']['blockers'])
        self.assertEqual(self.e.confirm(rb['id'], rb['preview_hash'])['status'], 'applied')
        self.assertEqual([x['id'] for x in self.registry()['webhooks']], ['main'])

    def test_names_refused(self):
        self.reg()
        for name in ('W2', 'w2', ' W2 ', 'Main', 'MAIN', 'main!', '', '   ', 'x' * 41, None, 7):
            with self.assertRaises(Invalid, msg=repr(name)):
                self.e.preview('register_webhook', {'name': name})
        with self.assertRaises(Invalid) as cm:  # add_webhook can't take an off webhook's name either
            self.e.preview('add_webhook', {'name': 'w2', 'strategy': 'soup-pine-r4', 'leader': 'account-b'})
        self.assertIn('already exists', str(cm.exception))
        self.do('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-b'})
        with self.assertRaises(Invalid):        # and register can't take a planned webhook's name
            self.e.preview('register_webhook', {'name': 'scalper'})

    def test_slug_and_path(self):
        ch = self.reg('Webhook 3')
        self.assertRegex(ch['plan']['generated']['path'], r'^/webhook/webhook-3-[0-9a-f]{12}$')
        self.assertEqual(self.wh('webhook-3')['state'], 'off')


class TestOverview(W2Base):
    def test_off_renders(self):
        main_before = self.wh('main')
        self.reg()
        ov = self.e.overview()
        w = next(x for x in ov['webhooks'] if x['id'] == 'w2')
        # merged with the cleanup pass: the page shows "Coming soon" for what an off webhook can't do yet
        self.assertEqual(sorted(w.pop('coming_soon')), ['activate_webhook', 'rotate_stream_id'])
        path = w['path']
        self.assertRegex(path, r'^/webhook/w2-[0-9a-f]{12}$')
        self.assertEqual(w, {
            'id': 'w2', 'name': 'W2', 'path': path,
            'url': f'https://{self.profile.public_host}{path}', 'url_active': False,
            'state': 'off', 'status_text': 'Off — waiting for a strategy',
            'strategy': {'id': None, 'name': None}, 'stream_id': None, 'stream_id_strength': None,
            'ticker': None, 'leader': None, 'followers': [], 'paused': False, 'pause': None,
            'soup': {'unit': None, 'port': 8800, 'enabled': False, 'locked': True,
                     'installed': False},
            'notes': [OFF_NOTE]})
        self.assertFalse(any('easy to guess' in n for n in w['notes']))
        self.assertEqual(self.wh('main'), main_before)
        self.assertEqual((main_before['url_active'], main_before['status_text']), (True, 'Live'))
        self.assertTrue(all(a['webhook'] != 'w2' for a in ov['accounts']))
        self.assertEqual(ov['drift'], self.e.state().drift)
        self.assertFalse(any('W2' in d for d in ov['drift']))

    def test_planned_url_not_active(self):
        self.do('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-b'})
        w = self.wh('scalper')
        self.assertEqual((w['url_active'], w['status_text']), (False, 'Planned — not running yet'))
        self.assertNotIn('installed', w['soup'])

    def test_bad_registry_entry_never_breaks_overview(self):
        reg = self.registry()
        reg['webhooks'].append({'id': 'broken', 'state': 'off'})           # no name, no path
        reg['webhooks'].append({'id': 'odd', 'name': 'Odd', 'state': 'planned', 'strategy': 'soup-pine-r4',
                                'stream_id': 'abcd-efgh-jkmn-pqrs', 'leader': None, 'followers': []})  # no path
        self.write_registry(reg)
        ov = self.e.overview()
        ws = {w['id']: w for w in ov['webhooks']}
        self.assertEqual((ws['broken']['name'], ws['broken']['path'], ws['broken']['url'], ws['broken']['url_active']),
                         ('broken', None, None, False))
        self.assertEqual((ws['odd']['path'], ws['odd']['url']), (None, None))
        self.assertTrue(ws['main']['url'].startswith('https://'))
        self.reg()   # registering still works next to a bad entry
        self.assertIn('w2', [w['id'] for w in self.e.overview()['webhooks']])


class TestOffRefusals(W2Base):
    def setUp(self):
        super().setUp()
        self.reg()

    def test_accounts_refused(self):
        self.refused('move_account', {'account': 'account-d', 'to_webhook': 'w2'}, 'can\'t take accounts')
        self.refused('add_account', {'login': 'a', 'key': 'account-f', 'label': 'Account F',
                                     'account_name': 'DEMO-0006', 'prop_profile': 'cross-account-window',
                                     'webhook': 'w2'}, 'can\'t take accounts')
        self.do('move_account', {'account': 'account-d', 'to_webhook': None})
        self.refused('move_account', {'account': 'account-d', 'to_webhook': 'w2'}, 'can\'t take accounts')
        self.assertEqual(self.registry()['webhooks'][1]['followers'], [])

    def test_webhook_controls_refused(self):
        self.refused('reorder', {'webhook': 'w2', 'followers': []}, 'no accounts to reorder')
        self.refused('pause_webhook', {'webhook': 'w2', 'reason': 'x'}, 'can\'t be paused; nothing runs on it')
        self.refused('resume_webhook', {'webhook': 'w2'}, 'can\'t be resumed; nothing runs on it')
        self.refused('rotate_stream_id', {'webhook': 'w2'}, 'stream ID when a strategy is approved')

    def test_set_enabled_refused(self):
        # Only reachable through a hand-edited registry: an account listed in an off webhook.
        self.do('move_account', {'account': 'account-d', 'to_webhook': None})
        reg = self.registry()
        reg['webhooks'][1]['followers'] = ['account-d']
        self.write_registry(reg)
        self.refused('set_enabled', {'account': 'account-d', 'enabled': False}, 'lanes can\'t be turned on or off')
        with self.assertRaises(Invalid) as cm:     # any registry edit now fails validation until it is cleaned up
            self.e.preview('register_webhook', {'name': 'W3'})
        self.assertIn(OFF, str(cm.exception))

    def test_remove_off(self):
        ch = self.e.preview('remove_webhook', {'webhook': 'w2'})
        self.assertFalse(ch['plan']['blockers'])
        self.assertEqual(ch['plan']['affected'], [])
        self.assertTrue(any('It is off' in x for x in ch['plan']['warnings']))
        self.assertEqual(self.e.confirm(ch['id'], ch['preview_hash'])['status'], 'applied')
        self.assertEqual([x['id'] for x in self.registry()['webhooks']], ['main'])
        self.assertIsNone(self.wh('w2'))
        self.assertEqual(self.reg()['plan']['generated']['port'], 8800)   # port freed

    def test_main_still_works(self):
        self.assertEqual(self.do('pause_webhook', {'webhook': 'main', 'reason': 'x'})['status'], 'applied')
        self.assertEqual(self.do('resume_webhook', {'webhook': 'main'})['status'], 'applied')
        ch = self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']})
        self.assertEqual(ch['status'], 'applied')


class TestSwitchesOnlyRegister(W2Base):
    def profile_extra(self):
        return {'live_switches_only': True}

    def test_register_refused(self):
        with self.assertRaises(Invalid) as cm:
            self.e.preview('register_webhook', {'name': 'W2'})
        self.assertIn('only pause and resume', str(cm.exception))
        self.assertEqual([x['id'] for x in self.registry()['webhooks']], ['main'])


if __name__ == '__main__':
    unittest.main()
