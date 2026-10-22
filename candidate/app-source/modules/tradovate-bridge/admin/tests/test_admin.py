"""tvbridge-admin tests. Each test runs on a fresh copy of tests/fixture/pristine-tree in a temp folder.

The pristine tree is a hand-written generic template: one login (A) with a main account and three followers.
Most tests then add a second, followers-only login (B) with two more followers (see add_second_login), so
the multi-login paths are covered too.

    cd admin && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
"""
import glob
import hashlib
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ADMIN, 'app'))

from engine import Conflict, Engine  # noqa: E402
from model import STREAM_RE, Draft, Invalid  # noqa: E402
import plans  # noqa: E402
from tree import FileOps, Profile  # noqa: E402

FIXTURE = os.path.join(HERE, 'fixture')
SB = '/tvbridge-admin-fixture'   # the root the fixture files name; each test rewrites it to its own copy
BRIDGE = os.path.join(os.path.dirname(ADMIN), 'bridge')   # the bridge package, read-only (its validators)
SECOND_LOGIN_DAEMON = 'tvbridge-daemon@b.service'   # add_second_login's Login B
KEYS = ['account-a', 'account-b', 'account-c', 'account-h', 'account-d', 'account-e']


def _write_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')


def add_second_login(tree):
    """Extend a fresh copy of the one-login fixture with a followers-only Login B (accounts D and E).
    `tree` is the copied tree folder; paths inside still name the fixture root SB (rewritten afterwards)."""
    tbr, root = os.path.join(tree, 'tvbridge'), SB + '/tree/tvbridge'
    with open(os.path.join(tbr, 'config.json')) as f:
        main = json.load(f)
    main['peer_logins'] = [{'id': 'b', 'url': 'http://127.0.0.1:8778', 'token_env': 'TVBRIDGE_READ_TOKEN_B'}]
    _write_json(os.path.join(tbr, 'config.json'), main)
    lb = root + '/logins/b'
    keep = ('ticker', 'tick_size', 'max_contracts', 'hedge_guard', 'hedge_groups', 'time_in_force', 'order_timeout_s',
            'max_silence_ms', 'page_url_contains', 'trade_url_pattern', 'account_view_enabled',
            'bracket_orders_enabled', 'flatten_all_enabled')
    cfg = {k: main[k] for k in keep}
    cfg.update({
        'kill_switch_file': lb + '/ENTRIES-DISABLED', 'journal_dir': lb + '/journal', 'devtools_port': 9557,
        'listen_port': 8778, 'login_id': 'b', 'login_label': 'Login B', 'mode': 'followers_only',
        'token_env': 'TVBRIDGE_TOKEN_B', 'read_token_env': 'TVBRIDGE_READ_TOKEN_B', 'publish_mode': 'evidence',
        'followers': [{'key': f'account-{k}', 'label': f'Account {k.upper()}', 'account_name': f'DEMO-{n:04d}',
                       'account_id': 100000 + n, 'journal_dir': f'{lb}/journal-account-{k}',
                       'kill_switch_file': f'{lb}/ENTRIES-DISABLED-account-{k}', 'max_contracts': 10,
                       'enabled': True, 'prop_profile': 'unrestricted', 'native_exits_enabled': True}
                      for k, n in (('d', 4), ('e', 5))],
        'master_kill_switch_file': root + '/ENTRIES-DISABLED',
        'peer_logins': [{'id': 'a', 'url': 'http://127.0.0.1:8777', 'token_env': 'TVBRIDGE_READ_TOKEN_A'}]})
    os.makedirs(os.path.join(tbr, 'logins/b'), mode=0o700)
    _write_json(os.path.join(tbr, 'logins/b/config.json'), cfg)
    with open(os.path.join(tbr, 'logins.json')) as f:
        logins = json.load(f)
    logins['logins'].append({
        'id': 'b', 'label': 'Login B', 'primary': False, 'config': lb + '/config.json',
        'bridge_url': 'http://127.0.0.1:8778', 'token_env': 'TVBRIDGE_TOKEN_B', 'read_token_env': 'TVBRIDGE_READ_TOKEN_B',
        'devtools_port': 9557, 'browser_unit': 'tvbridge-browser@b.service', 'daemon_unit': SECOND_LOGIN_DAEMON,
        'guard_unit': 'tvbridge-login-guard@b.service', 'trigger_unit': 'tvbridge-login-trigger@b.service',
        'guard_state_dir': '~/.local/share/decrypt-os/tvbridge-state/login-guard-b'})
    _write_json(os.path.join(tbr, 'logins.json'), logins)
    with open(os.path.join(tbr, 'firing.json')) as f:
        firing = json.load(f)
    for k in 'de':
        firing['firing_order'].append(f'account-{k}')
        firing['accounts'][f'account-{k}'] = {'role': 'follower', 'label': f'Account {k.upper()}', 'enabled': True,
                                              'prop_profile': 'unrestricted', 'size_policy': 'payload', 'login': 'b'}
    _write_json(os.path.join(tbr, 'firing.json'), firing)


def p_touch_follower(state, params, change_id):
    """TEST ONLY: the smallest structural change, an edit of one follower's entry in its login config, so the
    daemon of that login (and firing, status) must restart. It rewrites the retired `max_contracts` value: the
    bridge accepts a legacy positive whole number and ignores it, so the edit changes nothing about trading.
    The page itself never writes `max_contracts` (there is no size cap; the strategy alert sets the size)."""
    key = params['account']
    a = state.account(key)
    plans._follower_in(state, key)
    d = Draft(state)
    d.follower_entry(key)['max_contracts'] = params['n']
    return plans._finish(state, d, plans._plan(affected=[key], summary=f'{a["label"]}: test edit {params["n"]}'))


plans.BUILDERS.setdefault('touch_follower', p_touch_follower)


def card(key, net=0, orders='none'):
    return {'key': key, 'position': {'net': net, 'text': 'Flat' if not net else f'Long {net}'},
            'orders': {'text': orders}, 'status': {'label': 'OK', 'tone': 'green'}}


class Base(unittest.TestCase):
    TWO_LOGINS = True   # False: run on the one-login pristine tree as it is

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='tba-'))
        shutil.copytree(os.path.join(FIXTURE, 'pristine-tree'), os.path.join(self.tmp, 'tree'), symlinks=True)
        if self.TWO_LOGINS:
            add_second_login(os.path.join(self.tmp, 'tree'))
        with open(os.path.join(self.tmp, 'tree/tvbridge/firing.json')) as f:
            lanes = json.load(f)['firing_order'][1:]
        for key in lanes:   # the firing service's lane folders, as a running install has them
            os.makedirs(os.path.join(self.tmp, 'tree/tvbridge/firing', key), mode=0o700, exist_ok=True)
        for cfg in glob.glob(os.path.join(self.tmp, 'tree/tvbridge/**/config.json'), recursive=True):
            with open(cfg) as f:   # and every journal folder its configs name
                text = f.read().replace(SB + '/', self.tmp + '/')
            data = json.loads(text)
            for entry in [data] + (data.get('followers') or []):
                if entry.get('journal_dir'):
                    os.makedirs(entry['journal_dir'], mode=0o700, exist_ok=True)
        for path in glob.glob(os.path.join(self.tmp, 'tree/**/*.json'), recursive=True) + \
                glob.glob(os.path.join(self.tmp, 'tree/tunnel/*')):
            with open(path) as f:
                text = f.read()
            with open(path, 'w') as f:
                f.write(text.replace(SB + '/', self.tmp + '/'))
        with open(os.path.join(FIXTURE, 'profile.json')) as f:
            text = f.read().replace(SB, self.tmp)
        prof = json.loads(text)
        prof['bridge_release'] = BRIDGE  # read-only use
        self.status_file = os.path.join(self.tmp, 'status.json')
        prof['status_url'] = 'file://' + self.status_file
        prof['status_ttl_s'] = 0
        prof.update(self.profile_extra())
        self.set_status({k: 0 for k in self.keys()})
        self.profile = Profile(prof)
        self.e = Engine(self.profile)

    def profile_extra(self):
        return {}

    def keys(self):
        return KEYS if self.TWO_LOGINS else KEYS[:3]

    def tearDown(self):
        self.e.stop()
        self.e._flock.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_status(self, nets, down=False):
        if down:
            if os.path.exists(self.status_file):
                os.remove(self.status_file)
            return
        with open(self.status_file, 'w') as f:
            json.dump({'followers': {'cards': [card(k, n) for k, n in nets.items()]}, 'logins': [
                {'id': 'a', 'state_label': 'Logged out', 'tone': 'amber',
                 'visible': [{'name': 'DEMO-0006', 'configured': False}]}],
                'soup': {'market_session': {'state': 'closed', 'reason': 'weekend'}}}, f)

    def tb(self, *p):
        return os.path.join(self.tmp, 'tree/tvbridge', *p)

    def jload(self, *p):
        with open(self.tb(*p)) as f:
            return json.load(f)

    def do(self, ctype, params, text=None):
        ch = self.e.preview(ctype, params)
        return self.e.confirm(ch['id'], ch['preview_hash'], text)

    def raw(self, *p):
        with open(self.tb(*p), 'rb') as f:
            return f.read()

    def acct(self, key):
        return next(a for a in self.e.overview()['accounts'] if a['key'] == key)


class TestPause(Base):
    def test_pause_and_resume_admin(self):
        ch = self.do('pause', {'account': 'account-b', 'reason': 'testing'})
        self.assertEqual(ch['status'], 'applied')
        kill = self.tb('ENTRIES-DISABLED-account-b')
        self.assertTrue(os.path.exists(kill))
        self.assertEqual(self.jload('ENTRIES-DISABLED-account-b')['set_by'], 'tvbridge-admin')
        self.assertEqual(self.acct('account-b')['pause']['set_by'], 'admin')
        ch = self.do('resume', {'account': 'account-b'})
        self.assertEqual(ch['status'], 'applied')
        self.assertFalse(os.path.exists(kill))
        self.assertTrue(glob.glob(kill + '.removed-*'))
        self.assertTrue(self.e.audit.verify_chain())

    def test_resume_safety_pause_needs_label(self):
        kill = self.tb('logins/b/ENTRIES-DISABLED-account-d')
        open(kill, 'w').close()  # how the bridge's auto-protect sets it: an empty file
        self.assertEqual(self.acct('account-d')['pause']['set_by'], 'system')
        ch = self.e.preview('resume', {'account': 'account-d'})
        self.assertTrue(ch['plan']['confirm']['required'])
        with self.assertRaises(Conflict) as cm:
            self.e.confirm(ch['id'], ch['preview_hash'], 'apex')
        self.assertEqual(cm.exception.code, 'confirm_text')
        self.assertEqual(self.e.confirm(ch['id'], ch['preview_hash'], 'Account D')['status'], 'applied')
        self.assertFalse(os.path.exists(kill))

    def test_pause_leader_is_webhook_pause(self):
        ch = self.e.preview('pause_webhook', {'webhook': 'main', 'reason': 'news'})
        self.assertTrue(any('whole webhook' in w for w in ch['plan']['warnings']))
        self.e.confirm(ch['id'], ch['preview_hash'])
        self.assertTrue(os.path.exists(self.tb('ENTRIES-DISABLED')))
        self.assertTrue(self.e.overview()['webhooks'][0]['paused'])

    def test_pause_label_matches_bridge_window(self):
        """Same rule as the Bridge window: a pause outranks the card's label, a fault outranks a pause."""
        self.do('pause', {'account': 'account-b', 'reason': 'sizing'})
        st = {a['key']: a for a in self.e.overview()['accounts']}['account-b']['status']
        self.assertEqual(st['label'], 'Paused')
        with open(self.status_file, 'w') as f:
            blocked = dict(card('account-b'), status={'state': 'blocked', 'label': 'Blocked', 'tone': 'coral'})
            json.dump({'followers': {'cards': [card('account-a'), blocked]}}, f)
        self.e.status._cache = None if hasattr(self.e.status, '_cache') else None
        import time as _t; _t.sleep(max(0.0, float(getattr(self.e.p, 'status_ttl_s', 0) or 0)) + 0.05)
        st = {a['key']: a for a in self.e.overview()['accounts']}['account-b']['status']
        self.assertEqual(st['label'], 'Blocked')

    def test_double_pause_blocked(self):
        self.do('pause', {'account': 'account-b', 'reason': 'x'})
        ch = self.e.preview('pause', {'account': 'account-b', 'reason': 'y'})
        self.assertTrue(ch['plan']['blockers'])
        with self.assertRaises(Conflict):
            self.e.confirm(ch['id'], ch['preview_hash'])


class TestStructural(Base):
    def test_structural_edit_applies_when_flat(self):
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'applied', ch)
        self.assertEqual(self.jload('config.json')['followers'][0]['max_contracts'], 6)
        self.assertIn('tvbridge-daemon.service', [r['unit'] for r in ch['plan']['restarts']])
        self.assertEqual([s['name'] for s in ch['result']['steps']], ['backup', 'write', 'restart', 'verify'])

    def test_size_cap_is_retired(self):
        # The strategy alert sets the size: there is no size-cap change type any more, of any value.
        for n in (1, 6, 10, 40):
            with self.assertRaises(Invalid) as cm:
                self.e.preview('set_max_contracts', {'account': 'account-b', 'max_contracts': n})
            self.assertEqual(str(cm.exception), 'The size cap is retired: the strategy alert sets the size.')
        self.assertNotIn('set_max_contracts', plans.BUILDERS)
        self.assertFalse(hasattr(plans, 'MAX_CAP'))
        self.assertNotIn('max_contracts', self.acct('account-b'))   # nothing in the overview either

    def test_legacy_max_contracts_any_positive_value_is_fine(self):
        # A legacy positive whole number of any size loads, arms and lets structural changes through.
        for path, where in (('config.json', None), ('config.json', 0), ('logins/b/config.json', 0)):
            cfg = self.jload(path)
            (cfg if where is None else cfg['followers'][where])['max_contracts'] = 40
            with open(self.tb(path), 'w') as f:
                json.dump(cfg, f, indent=2)
        cfg = self.jload('logins/b/config.json')
        cfg['followers'][1].pop('max_contracts', None)     # and an account with no max_contracts at all
        with open(self.tb('logins/b/config.json'), 'w') as f:
            json.dump(cfg, f, indent=2)
        self.do('pause', {'account': 'account-b', 'reason': 'x'})
        ch = self.e.preview('resume', {'account': 'account-b'})
        self.assertFalse(ch['plan']['blockers'], ch['plan']['blockers'])
        self.assertEqual(self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']}
                                 )['status'], 'applied')
        self.assertEqual(self.do('set_enabled', {'account': 'account-e', 'enabled': False})['status'], 'applied')
        self.assertEqual(self.jload('logins/b/config.json')['followers'][0]['max_contracts'], 40)  # left as it was

    def test_leaders_are_fixed(self):
        with self.assertRaises(Invalid):
            self.e.preview('touch_follower', {'account': 'account-a', 'n': 5})

    def test_waits_for_flat_then_applies(self):
        self.set_status({'account-a': 0, 'account-b': 2, 'account-c': 0, 'account-d': 0, 'account-e': 0})
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 4})
        self.assertEqual(ch['status'], 'waiting_flat')
        self.assertEqual(ch['eta']['kind'], 'after_flat')
        self.assertIn('account-b', ch['eta']['waiting_on'])
        self.assertEqual(self.jload('config.json')['followers'][0]['max_contracts'], 10)
        self.e.tick()
        self.assertEqual(self.e.get(ch['id'])['status'], 'waiting_flat')
        self.set_status({k: 0 for k in KEYS})
        self.e.tick()
        self.assertEqual(self.e.get(ch['id'])['status'], 'applied')
        self.assertEqual(self.jload('config.json')['followers'][0]['max_contracts'], 4)

    def test_restart_scope_waits_on_whole_login(self):
        # Login B daemon restart touches both Login B accounts, even if only one is edited.
        self.set_status({'account-a': 0, 'account-b': 0, 'account-c': 0, 'account-d': 0, 'account-e': -1})
        ch = self.do('touch_follower', {'account': 'account-d', 'n': 3})
        self.assertEqual(ch['status'], 'waiting_flat')
        self.assertIn('account-e', ch['eta']['waiting_on'])

    def test_status_down_never_assumes_flat(self):
        self.set_status(None, down=True)
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 4})
        self.assertEqual(ch['status'], 'waiting_flat')
        self.assertEqual(ch['eta']['kind'], 'blocked')
        self.e.tick()
        self.assertEqual(self.e.get(ch['id'])['status'], 'waiting_flat')

    def test_unknown_position_is_not_flat(self):
        with open(self.status_file, 'w') as f:
            json.dump({'followers': {'cards': [card(k) for k in KEYS if k != 'account-b']}}, f)
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 4})
        self.assertEqual(ch['status'], 'waiting_flat')

    def test_stale_preview(self):
        ch = self.e.preview('touch_follower', {'account': 'account-b', 'n': 6})
        cfg = self.jload('config.json')
        cfg['time_in_force'] = 'Day'
        with open(self.tb('config.json'), 'a') as f:
            f.write(' ')
        with self.assertRaises(Conflict) as cm:
            self.e.confirm(ch['id'], ch['preview_hash'])
        self.assertEqual(cm.exception.code, 'stale_preview')

    def test_preview_expires(self):
        ch = self.e.preview('pause', {'account': 'account-d', 'reason': 'x'})
        real = self.e.clock
        self.e.clock = lambda: real() + 1000
        self.e.tick()
        self.assertEqual(self.e.get(ch['id'])['status'], 'expired')
        self.assertNotIn(ch['id'], [c['id'] for c in self.e.overview()['pending']])
        with self.assertRaises(Conflict):
            self.e.confirm(ch['id'], ch['preview_hash'])

    def test_cancel_waiting(self):
        self.set_status({'account-a': 0, 'account-b': 1, 'account-c': 0, 'account-d': 0, 'account-e': 0})
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 4})
        self.assertEqual(self.e.cancel(ch['id'])['status'], 'cancelled')
        self.set_status({k: 0 for k in KEYS})
        self.e.tick()
        self.assertEqual(self.jload('config.json')['followers'][0]['max_contracts'], 10)

    def test_reorder(self):
        ch = self.do('reorder', {'webhook': 'main', 'followers': ['account-e', 'account-b', 'account-c', 'account-h', 'account-d']})
        self.assertEqual(ch['status'], 'applied')
        self.assertEqual(self.jload('firing.json')['firing_order'],
                         ['account-a', 'account-e', 'account-b', 'account-c', 'account-h', 'account-d'])
        with self.assertRaises(Invalid):
            self.e.preview('reorder', {'webhook': 'main', 'followers': ['account-d']})

    def test_lane_off(self):
        self.do('set_enabled', {'account': 'account-d', 'enabled': False})
        self.assertFalse(self.jload('firing.json')['accounts']['account-d']['enabled'])
        self.assertFalse(self.acct('account-d')['enabled'])

    def test_move_out_and_back(self):
        self.do('move_account', {'account': 'account-d', 'to_webhook': None})
        f = self.jload('firing.json')
        self.assertNotIn('account-d', f['firing_order'])
        self.assertEqual(self.acct('account-d')['role'], 'unassigned')
        ch = self.do('move_account', {'account': 'account-d', 'to_webhook': 'main', 'index': 0})
        self.assertEqual(ch['status'], 'applied')
        f = self.jload('firing.json')
        self.assertEqual(f['firing_order'][1], 'account-d')
        self.assertEqual(f['accounts']['account-d']['login'], 'b')
        self.assertEqual(f['accounts']['account-d']['size_policy'], 'payload')
        with self.assertRaises(Invalid):
            self.e.preview('move_account', {'account': 'account-a', 'to_webhook': None})

    def test_add_account_starts_paused(self):
        ch = self.do('add_account', {'login': 'a', 'key': 'account-f', 'label': 'Account F',
                                     'account_name': 'DEMO-0006', 'account_id': None,
                                     'prop_profile': 'cross-account-window', 'webhook': 'main'})
        self.assertEqual(ch['status'], 'applied', ch.get('error'))
        cfg = self.jload('config.json')
        self.assertEqual(cfg['followers'][-1]['key'], 'account-f')
        self.assertNotIn('max_contracts', cfg['followers'][-1])   # retired: the strategy alert sets the size
        self.assertTrue(os.path.isdir(self.tb('journal-account-f')))
        self.assertTrue(os.path.isdir(self.tb('firing', 'account-f')))
        self.assertEqual(self.acct('account-f')['pause']['set_by'], 'admin')
        self.assertEqual(self.jload('firing.json')['firing_order'][-1], 'account-f')
        units = [r['unit'] for r in ch['plan']['restarts']]
        self.assertEqual(units[:2], ['tvbridge-daemon.service', 'tvbridge-firing.service'])
        for bad in ({'key': 'account-b'}, {'key': 'Bad Key'},
                    {'prop_profile': 'some-other-profile'}, {'label': 'Account B'}):
            p = {'login': 'a', 'key': 'account-x', 'label': 'Account X', 'account_name': 'DEMO-0099',
                 'prop_profile': 'cross-account-window', 'webhook': None}
            p.update(bad)
            with self.assertRaises(Invalid, msg=str(bad)):
                self.e.preview('add_account', p)

    def test_add_account_login_b_paths(self):
        # An older caller that still sends max_contracts (any size): ignored and never written.
        self.do('add_account', {'login': 'b', 'key': 'account-g', 'label': 'Account G', 'account_name': 'DEMOB0042',
                                'prop_profile': 'cross-account-window', 'max_contracts': 40, 'webhook': None})
        entry = self.jload('logins/b/config.json')['followers'][-1]
        self.assertNotIn('max_contracts', entry)
        self.assertEqual(entry['kill_switch_file'], self.tb('logins/b/ENTRIES-DISABLED-account-g'))
        self.assertEqual(self.acct('account-g')['role'], 'unassigned')

    def test_retire(self):
        self.set_status({'account-a': 0, 'account-b': 0, 'account-c': 0, 'account-d': 1, 'account-e': 0})
        ch = self.do('retire_account', {'account': 'account-d'})
        self.assertEqual(ch['status'], 'waiting_flat')
        self.assertTrue(os.path.exists(self.tb('logins/b/ENTRIES-DISABLED-account-d')))  # paused right away
        self.set_status({k: 0 for k in KEYS})
        self.e.tick()
        self.assertEqual(self.e.get(ch['id'])['status'], 'applied')
        self.assertNotIn('account-d', self.jload('firing.json')['firing_order'])
        self.assertNotIn('account-d', [f['key'] for f in self.jload('logins/b/config.json')['followers']])
        self.assertTrue(os.path.isdir(self.tb('logins/b/journal-account-d')))  # journals kept

    def test_failed_restart_rolls_back(self):
        before = self.raw('config.json')
        self.e.restarter.fail_units = {'tvbridge-daemon.service'}
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'failed')
        self.assertTrue(ch['result']['rolled_back'])
        self.assertEqual(self.raw('config.json'), before)

    def test_rollback(self):
        before = self.raw('config.json')
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        rb = self.e.rollback_preview(ch['id'])
        self.assertFalse(rb['plan']['blockers'])
        self.assertEqual(self.e.confirm(rb['id'], rb['preview_hash'])['status'], 'applied')
        self.assertEqual(self.raw('config.json'), before)
        self.assertEqual(self.e.get(ch['id'])['status'], 'rolled_back')

    def test_rollback_blocked_after_newer_change(self):
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.do('touch_follower', {'account': 'account-b', 'n': 5})
        self.assertTrue(self.e.rollback_preview(ch['id'])['plan']['blockers'])


class TestWebhooks(Base):
    def test_add_webhook_planned(self):
        ch = self.do('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-b'})
        self.assertEqual(ch['status'], 'planned', ch.get('error'))
        g = ch['plan']['generated']
        self.assertTrue(STREAM_RE.match(g['stream_id']) and len(g['stream_id']) == 19)
        self.assertRegex(g['path'], r'^/webhook/scalper-[0-9a-f]{12}$')   # no user name in the path
        self.assertTrue(g['url'].startswith('https://'))
        self.assertNotIn('account-b', self.jload('firing.json')['firing_order'])
        w = [w for w in self.e.overview()['webhooks'] if w['id'] == 'scalper'][0]
        self.assertEqual((w['state'], w['leader'], w['stream_id_strength']), ('planned', 'account-b', 'strong'))
        with self.assertRaises(Invalid):
            self.e.preview('add_webhook', {'name': 'scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-d'})
        with self.assertRaises(Invalid):
            self.e.preview('add_webhook', {'name': 'Other', 'strategy': 'nope', 'leader': 'account-d'})
        with self.assertRaises(Invalid):  # already leads
            self.e.preview('add_webhook', {'name': 'Other', 'strategy': 'soup-pine-r4', 'leader': 'account-a'})

    def test_planned_membership_and_remove(self):
        self.do('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-b'})
        ch = self.do('move_account', {'account': 'account-d', 'to_webhook': 'scalper'})
        self.assertEqual(ch['status'], 'applied')
        self.assertTrue(any('not running yet' in w for w in ch['plan']['warnings']))
        rm = self.e.preview('remove_webhook', {'webhook': 'scalper'})
        self.assertTrue(rm['plan']['blockers'])
        self.do('move_account', {'account': 'account-d', 'to_webhook': None})
        self.assertEqual(self.do('remove_webhook', {'webhook': 'scalper'})['status'], 'applied')
        self.assertEqual(self.acct('account-b')['role'], 'unassigned')
        self.assertTrue(self.e.preview('remove_webhook', {'webhook': 'main'})['plan']['blockers'])

    def test_rotate_stream(self):
        ch = self.do('rotate_stream_id', {'webhook': 'main'})
        self.assertEqual(ch['status'], 'manual')
        with open(os.path.join(self.tmp, 'tree/soup-state/pairing.json')) as f:
            self.assertEqual(json.load(f)['stream_id'], 'fx-demo-stream')  # never written by the page
        self.do('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-b'})
        old = [w for w in self.e.overview()['webhooks'] if w['id'] == 'scalper'][0]['stream_id']
        self.assertEqual(self.do('rotate_stream_id', {'webhook': 'scalper'})['status'], 'applied')
        new = [w for w in self.e.overview()['webhooks'] if w['id'] == 'scalper'][0]['stream_id']
        self.assertNotEqual(old, new)

    def test_add_login_manual(self):
        ch = self.do('add_login', {'label': 'Login C'})
        self.assertEqual(ch['status'], 'manual')
        self.assertTrue(any('install.py add-login' in s for s in ch['plan']['manual_steps']))


class TestFlatten(Base):
    def test_flatten_dry_run(self):
        ch = self.e.preview('flatten', {'account': 'account-b'})
        self.assertTrue(ch['plan']['confirm']['required'])
        with self.assertRaises(Conflict):
            self.e.confirm(ch['id'], ch['preview_hash'], '')
        ch = self.e.confirm(ch['id'], ch['preview_hash'], 'Account B')
        self.assertEqual(ch['status'], 'applied')
        with open(self.profile.st('dry-run-orders.jsonl')) as f:
            calls = [json.loads(x) for x in f]
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]['sent'])
        self.assertTrue(os.path.exists(self.tb('ENTRIES-DISABLED-account-b')))


class TestSafety(Base):
    def test_outside_roots_refused(self):
        with self.assertRaises(PermissionError):
            FileOps(self.profile).apply([{'path': '/tvbridge-admin-outside/config.json', 'action': 'write',
                                          'content': 'x'}])

    def test_sandbox_profile_cannot_send(self):
        bad = dict(self.profile.data, order_adapter='tradovate')
        with self.assertRaises(ValueError):
            Profile(bad)


class TestSwitchesOnly(Base):
    def profile_extra(self):
        return {'live_switches_only': True}

    def test_only_switches(self):
        self.assertEqual(self.do('pause', {'account': 'account-d', 'reason': 'x'})['status'], 'applied')
        for t, p in (('set_enabled', {'account': 'account-d', 'enabled': False}),
                     ('flatten', {'account': 'account-d'})):
            with self.assertRaises(Invalid):
                self.e.preview(t, p)

    def test_live_profile_guard(self):
        with self.assertRaises(ValueError):
            Profile(dict(self.profile.data, mode='live', live_switches_only=False))
        Profile(dict(self.profile.data, mode='live', live_switches_only=True))

    def test_soup_service_is_optional(self):
        # default: Soup runs inside the app, so the overview names no Soup unit or port
        self.assertIsNone(self.profile.soup_unit())
        main = next(w for w in self.e.overview()['webhooks'] if w['id'] == 'main')
        self.assertEqual((main['soup']['unit'], main['soup']['port']), (None, None))
        p = Profile(dict(self.profile.data, soup_service={'unit': 'example-soup.service', 'port': 8850}))
        self.assertEqual((p.soup_unit(), p.soup_unit('w2'), p.soup_port()),
                         ('example-soup.service', 'example-soup@w2.service', 8850))
        self.assertTrue(p.is_soup_unit('example-soup@w2.service'))
        self.assertFalse(p.is_soup_unit('tvbridge-firing.service'))
        for bad in ('x', {'unit': 'x'}, {'unit': 'a@b.service'}, {'unit': 'ok.service', 'port': 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Profile(dict(self.profile.data, soup_service=bad))


class TestServer(Base):
    def setUp(self):
        super().setUp()
        from http.server import ThreadingHTTPServer
        import server
        os.makedirs(server.UI_DIR, exist_ok=True)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(self.e, 'tok', 0))
        self.port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass = server.make_handler(self.e, 'tok', self.port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, host=None, token='tok', origin='same'):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        h = {'Host': host or f'127.0.0.1:{self.port}'}
        if token:
            h['X-Admin-Token'] = token
        if body is not None:
            h['Content-Type'] = 'application/json'
        if origin == 'same':
            h['Origin'] = f'http://127.0.0.1:{self.port}'
        elif origin:
            h['Origin'] = origin
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if r.getheader('Content-Type', '').startswith('application/json') else data)

    def test_guards(self):
        self.assertEqual(self.req('GET', '/api/overview', host='evil.example')[0], 421)
        self.assertEqual(self.req('GET', '/api/overview', token=None)[0], 401)
        self.assertEqual(self.req('GET', '/api/overview', token='nope')[0], 401)
        self.assertEqual(self.req('POST', '/api/changes', {'type': 'pause'}, origin='http://evil.example')[0], 403)
        self.assertEqual(self.req('POST', '/api/changes', {'type': 'pause'}, origin=None)[0], 403)

    def test_flow_over_http(self):
        st, ov = self.req('GET', '/api/overview')
        self.assertEqual(st, 200)
        self.assertEqual(ov['mode'], 'sandbox')
        st, ch = self.req('POST', '/api/changes', {'type': 'pause', 'params': {'account': 'account-d', 'reason': 'x'}})
        self.assertEqual(st, 200)
        st, ch = self.req('POST', f'/api/changes/{ch["id"]}/confirm', {'preview_hash': ch['preview_hash']})
        self.assertEqual((st, ch['status']), (200, 'applied'))
        st, err = self.req('POST', '/api/changes', {'type': 'set_max_contracts',
                                                    'params': {'account': 'account-d', 'max_contracts': 3}})
        self.assertEqual(st, 400)
        self.assertEqual(err['detail'], 'The size cap is retired: the strategy alert sets the size.')
        st, err = self.req('POST', '/api/changes', {'type': 'no_such_type', 'params': {}})
        self.assertEqual(st, 400)
        self.assertIn('Unknown change type', err['detail'])
        st, au = self.req('GET', '/api/audit')
        self.assertTrue(au['chain_ok'])
        self.assertEqual(self.req('GET', '/api/logins/a/visible')[1]['accounts'][0]['name'], 'DEMO-0006')


class TestOneLoginTemplate(Base):
    """The pristine tree as shipped: one login, a main account and two followers, nothing else."""
    TWO_LOGINS = False

    def test_overview_lists_the_template(self):
        ov = self.e.overview()
        self.assertEqual([a['key'] for a in ov['accounts']], KEYS[:4])
        self.assertEqual([lg['id'] for lg in ov['logins']], ['a'])
        self.assertEqual(self.jload('firing.json')['firing_order'], KEYS[:4])
        self.assertFalse(os.path.exists(self.tb('logins')))

    def test_pause_and_resume_a_follower(self):
        self.assertEqual(self.do('pause', {'account': 'account-c', 'reason': 'x'})['status'], 'applied')
        self.assertTrue(os.path.exists(self.tb('ENTRIES-DISABLED-account-c')))
        self.assertEqual(self.do('resume', {'account': 'account-c'})['status'], 'applied')
        self.assertFalse(os.path.exists(self.tb('ENTRIES-DISABLED-account-c')))


class TestFixtureDigests(unittest.TestCase):
    """The Soup state files of the fixture agree with each other: every digest is derived from fixture content."""
    SOUP = os.path.join(FIXTURE, 'pristine-tree/soup-state')
    ACCOUNT_URL = 'https://fixture.invalid/tradovate/accounts/100001'   # synthetic; the fixture's account URL
    CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(ADMIN))),
                        'app/trading-runtime/decrypt_trading/vendor/soup_r4')

    def load(self, name):
        with open(os.path.join(self.SOUP, name), 'rb') as f:
            raw = f.read()
        return raw, json.loads(raw)

    def test_binding_pins_the_fixture_quantity_policy(self):
        raw, _ = self.load('quantity-policy.json')
        _, binding = self.load('bridge-binding.json')
        self.assertEqual(binding['quantity_policy_sha256'], hashlib.sha256(raw).hexdigest())

    def test_policy_names_the_fixture_account(self):
        _, policy = self.load('quantity-policy.json')
        _, binding = self.load('bridge-binding.json')
        nb = policy['native_binding']
        self.assertEqual(nb['account_url_sha256'], hashlib.sha256(self.ACCOUNT_URL.encode()).hexdigest())
        self.assertEqual(nb['account_value'], str(binding['tradovate_account_id']))
        self.assertIn(binding['tradovate_account_name'], nb['account_label'])
        self.assertEqual(policy['account_id'], binding['account_id'])

    def test_policy_passes_the_engine_validator(self):
        if not os.path.isfile(os.path.join(self.CORE, 'core.py')):
            self.skipTest('Soup engine source not in this checkout')
        code = ('import json, sys; sys.path.insert(0, sys.argv[1]); import core; '
                'p = json.load(open(sys.argv[2])); core.validate_quantity_policy(p, p["account_id"]); print("ok")')
        import subprocess
        out = subprocess.run([sys.executable, '-c', code, self.CORE, os.path.join(self.SOUP, 'quantity-policy.json')],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), 'ok', out.stderr[-500:])


if __name__ == '__main__':
    unittest.main()
