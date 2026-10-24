"""Firing order (1.0.7): follower accounts in the daemon (tvbridge.followers) and the fan-out
service (tvbridge.firing).

- Config validation for both (browser-free).
- Daemon routes with an in-memory login of three accounts: the main routes are unchanged,
  follower routes, per-account journals and tags, kill switches, the hedge guard across
  accounts, and an unavailable follower that never blocks the main account.
- The entry gate and parallel lanes against Soup's real runtime and storage.
- End to end in headless Chrome: Soup trades the main account through POST /order and the
  follower accounts of the same login follow through one page, one socket.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import Harness  # also puts the repository on sys.path
from test_mirror import (CONFIG, CONTRACT, LIMIT, NY, POLICY, PRIMARY, SOUP, TOKEN, Recorder,  # noqa: F401
                         SYMBOL, SoupSide, dead_port, until)
from storage import canonical  # noqa: E402  (Soup's, via test_mirror's path setup)

from tvbridge import firing as firing_mod
from tvbridge import followers
from tvbridge import grid
from tvbridge.bridge import Bridge
from tvbridge.daemon import account_state, make_handler
from tvbridge.executor import Executor, client_tag
from tvbridge.firing import ALERTS_NAME, Firing, FiringConfigError, FollowerLane, validate
from tvbridge.followers import ALIGN_ACTION
from tvbridge.journal import Journal
from tvbridge.mirror import JOURNAL_NAME
from tvbridge.soup_transport import TradovateBridgeTransport
from tvbridge.state import AccountState

import logging
logging.getLogger('tvbridge.firing').addHandler(logging.NullHandler())
logging.getLogger('tvbridge.firing').propagate = False
logging.getLogger('tvbridge.mirror').propagate = False
logging.getLogger('tvbridge').addHandler(logging.NullHandler())  # expected follower errors stay quiet

TDATE = {'year': 2021, 'month': 1, 'day': 1}


def follower_entry(key, name, tmp, **kw):
    f = {'key': key, 'account_name': name, 'journal_dir': str(tmp / ('journal-' + key)),
         'kill_switch_file': str(tmp / ('ENTRIES-DISABLED-' + key)), 'max_contracts': 10,
         'prop_profile': 'cross-account-window'}
    f.update(kw)
    return f


def records(path):
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text().split('\n'):
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ----------------------------------------------------------------------------------------
# Config validation
# ----------------------------------------------------------------------------------------

class FollowerConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path('/tmp/fo-cfg')
        self.cfg = {'account_name': 'MAIN', 'account_id': 7, 'ticker': 'MNQZ2021', 'tick_size': .25,
                    'journal_dir': '/tmp/fo-cfg/journal', 'kill_switch_file': '/tmp/fo-cfg/ENTRIES-DISABLED',
                    'hedge_guard': True, 'max_contracts': 10}

    def parse(self, *entries):
        return followers.parse(dict(self.cfg, followers=list(entries)))

    def test_absent_or_empty_means_no_followers(self):
        self.assertEqual(followers.parse(self.cfg), [])
        self.assertEqual(followers.parse(dict(self.cfg, followers=[])), [])

    def test_valid_follower_inherits_contract_settings_and_forces_guards(self):
        [(key, ecfg, enabled)] = self.parse(follower_entry('ac', 'FC', self.tmp, account_id=None, enabled=False))
        self.assertEqual((key, enabled), ('ac', False))
        self.assertEqual((ecfg['ticker'], ecfg['tick_size'], ecfg['hedge_guard'], ecfg['bracket_orders_enabled'],
                          ecfg['flatten_all_enabled']),
                         ('MNQZ2021', .25, True, False, False))
        self.assertNotIn('max_contracts', ecfg)  # no size cap: the strategy alert sets the size
        self.assertIsNone(ecfg['soup_account_id'])

    def test_unrestricted_profile_follower(self):
        [(key, ecfg, _)] = self.parse(follower_entry('ac', 'DEMO00000000015', self.tmp, account_id=30004444,
                                                     prop_profile='unrestricted'))
        self.assertEqual((ecfg['prop_profile'], ecfg['hedge_guard']), ('unrestricted', True))

    def test_retired_max_contracts_any_legacy_positive_value_is_ignored(self):
        for legacy in (1, 11, 40):
            [(_, ecfg, _)] = self.parse(follower_entry('ac', 'FC', self.tmp, max_contracts=legacy))
            self.assertNotIn('max_contracts', ecfg)

    def test_refusals(self):
        bad = [
            (follower_entry('ac', 'FC', self.tmp, prop_profile='apex'), 'Apex'),
            (follower_entry('ac', 'FC', self.tmp, prop_profile='Apex'), 'Apex'),
            (follower_entry('ac', 'FC', self.tmp, prop_profile='some-other-profile'), 'prop_profile'),
            (follower_entry('ac', 'FC', self.tmp, max_contracts='3'), 'max_contracts is retired'),
            (follower_entry('ac', 'FC', self.tmp, max_contracts=0), 'max_contracts is retired'),
            (follower_entry('ac', 'MAIN', self.tmp), 'account_name'),
            (follower_entry('ac', 'FC', self.tmp, account_id=7), 'account_id'),
            (follower_entry('ac', 'FC', self.tmp, journal_dir='/tmp/fo-cfg/journal'), 'journal_dir'),
            (follower_entry('ac', 'FC', self.tmp, kill_switch_file='/tmp/fo-cfg/ENTRIES-DISABLED'),
             'kill_switch_file'),
            (follower_entry('ac', 'FC', self.tmp, journal_dir='relative'), 'absolute'),
            (follower_entry('T 9', 'FC', self.tmp), 'slug'),
            (follower_entry('ac', 'FC', self.tmp, hedge_guard=False), 'unknown keys'),
            (follower_entry('ac', 'FC', self.tmp, size_multiplier=2), 'unknown keys'),
        ]
        for entry, text in bad:
            with self.assertRaises(followers.FollowerConfigError, msg=entry) as ctx:
                self.parse(entry)
            self.assertIn(text, str(ctx.exception))
        with self.assertRaises(followers.FollowerConfigError):
            self.parse(follower_entry('ac', 'FC', self.tmp), follower_entry('ac', 'FB', self.tmp))
        with self.assertRaises(followers.FollowerConfigError):
            followers.parse(dict(self.cfg, followers={'ac': {}}))


class FiringConfig(unittest.TestCase):
    def cfg(self, **over):
        c = {'soup_db': '/x/engine.sqlite', 'soup_engine_dir': '/x/engine', 'ticker': 'MNQZ2021',
             'firing_dir': '/y/firing', 'firing_order': ['aa', 'ac', 'ab'],
             'accounts': {'aa': {'role': 'main'},
                          'ac': {'role': 'follower', 'enabled': False, 'prop_profile': 'cross-account-window'},
                          'ab': {'role': 'follower', 'prop_profile': 'cross-account-window', 'size_policy': 'payload'}}}
        c.update(over)
        return c

    def test_valid_main_first_followers_in_order(self):
        self.assertEqual(validate(self.cfg()), ('aa', ['ab']))
        both = self.cfg()
        both['accounts']['ac']['enabled'] = True
        self.assertEqual(validate(both), ('aa', ['ac', 'ab']))

    def test_refusals(self):
        def acct(key, **fields):
            c = self.cfg()
            c['accounts'][key].update(fields)
            return c
        bad = [
            (self.cfg(firing_order=['ac', 'aa', 'ab']), 'main'),
            (acct('ab', prop_profile='apex'), 'Apex'),
            (acct('aa', prop_profile='APEX'), 'Apex'),
            (acct('ab', size_policy='multiplier'), 'size_policy'),
            (acct('ab', role='main'), 'main'),
            (acct('ab', enabled='yes'), 'enabled'),
            (self.cfg(firing_mode='sequential'), 'parallel'),
            (self.cfg(entry_gate='maybe'), 'entry_gate'),
            (self.cfg(firing_order=['aa', 'ab']), 'same account keys'),
            (self.cfg(reconcile_grace_ms=60000), 'cross-account-window'),
            (self.cfg(main_gate_timeout_ms=9500), 'main_gate_timeout_ms'),
            (self.cfg(align_wait_ms=6000), 'align_wait_ms'),
            (self.cfg(grid_attention_ms=0), 'grid_attention_ms'),
            (self.cfg(poll_interval_ms=0), 'poll_interval_ms'),
            (self.cfg(reconcile_mode='flatten'), 'reconcile_mode'),
            (self.cfg(mirror_dir='/z'), 'unknown config keys'),
        ]
        for c, text in bad:
            with self.assertRaises(FiringConfigError, msg=text) as ctx:
                validate(c)
            self.assertIn(text, str(ctx.exception))

    def test_unrestricted_follower_has_no_cross_account_window_limit(self):
        c = self.cfg(reconcile_grace_ms=60000)
        c['accounts']['ab']['prop_profile'] = 'unrestricted'
        self.assertEqual(validate(c), ('aa', ['ab']))

    def test_check_cli_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'firing.json'
            p.write_text(json.dumps(self.cfg()))
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch('logging.basicConfig'):
                firing_mod.main(['--config', str(p), '--check'])
            out = json.loads(buf.getvalue())
            self.assertEqual((out['ok'], out['main'], out['followers_enabled']), (True, 'aa', ['ab']))
            self.assertEqual(sorted(os.listdir(d)), ['firing.json'])

    def test_example_config_is_valid_and_disabled_by_default(self):
        example = json.loads((Path(__file__).resolve().parent.parent / 'firing.example.json').read_text())
        main_key, enabled = validate(example)
        self.assertEqual((main_key, enabled), ('account-a', []))


# ----------------------------------------------------------------------------------------
# Daemon follower routes with an in-memory login (no browser)
# ----------------------------------------------------------------------------------------

class LoginDouble:
    """One page on one login with three accounts: main 7, followers 8 and 9."""

    def __init__(self, state, nets=None, working=()):
        self.state, self.epoch = state, 'e1'
        accounts = [{'id': 7, 'name': 'MAIN-ACCT'}, {'id': 8, 'name': 'F-ONE'}, {'id': 9, 'name': 'F-TWO'},
                    {'id': 10, 'name': 'NOT-A-FOLLOWER'}]
        positions = [{'id': 300 + a, 'accountId': a, 'contractId': 2, 'netPos': n, 'tradeDate': TDATE}
                     for a, n in (nets or {}).items()]
        orders = [{'id': 400 + i, 'accountId': a, 'contractId': 2, 'ordStatus': 'Working', 'action': act}
                  for i, (a, act) in enumerate(working)]
        versions = [{'id': 500 + i, 'orderId': 400 + i, 'orderQty': 1, 'orderType': 'Limit', 'price': 10100.0}
                    for i in range(len(working))]
        data = {'accounts': accounts, 'orders': orders, 'fills': [], 'orderVersions': versions,
                'contracts': [{'id': 2, 'name': 'MNQZ1'}], 'positions': positions}
        state.load_sync(accounts, data, self.epoch)
        self.sent = []

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5.0, expected_epoch=None):
        self.sent.append((endpoint, body))
        return {'ok': True, 'sent': True, 's': 200, 'd': {'orderId': 9000 + len(self.sent)}}


class Pub:
    last = {}


def entry_payload(cid='soup-abc', tid='fixture-stream:MNQZ2021:1632147200000:long', qty=3, action='buy'):
    return {'ticker': 'MNQZ2021', 'action': action, 'orderType': 'market', 'quantity': qty,
            'extras': {'soup_command_id': cid, 'soup_trade_id': tid}}


class FollowerRoutes(unittest.TestCase):
    def setUp(self, nets=None, working=(), f1_extra=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = AccountState()
        self.page = LoginDouble(self.state, nets, working)
        self.cfg = {'ticker': 'MNQZ2021', 'account_name': 'MAIN-ACCT', 'account_id': 7, 'soup_account_id': 'Account A',
                    'max_contracts': 10, 'tick_size': .25, 'journal_dir': str(self.tmp / 'journal'),
                    'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED'), 'publish_mode': 'adapter-db',
                    'followers': [follower_entry('f1', 'F-ONE', self.tmp, max_contracts=3, **(f1_extra or {})),
                                  follower_entry('f2', 'F-TWO', self.tmp),
                                  follower_entry('gone', 'NOT-IN-THIS-LOGIN', self.tmp),
                                  follower_entry('off', 'F-OFF', self.tmp, enabled=False)]}
        self.main = Executor(self.page, self.state, Journal(self.cfg['journal_dir']), self.cfg)
        self.main.resolve()
        self.fx = followers.build(self.page, self.state, self.cfg, self.main)
        self.plain = self.serve(make_handler(self.main, Pub(), TOKEN, True))
        self.base = self.serve(followers.make_follower_handler(make_handler(self.main, Pub(), TOKEN, True),
                                                               self.fx, TOKEN, self.main))

    def serve(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d' % server.server_address[1]

    def call(self, base, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(base + path, data=data, method='POST' if data else 'GET',
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_build_pins_each_follower_and_isolates_faults(self):
        self.assertEqual((self.fx['f1'].account_id, self.fx['f2'].account_id), (8, 9))
        self.assertIsInstance(self.fx['gone'], followers.Unavailable)
        self.assertIn('not visible', self.fx['gone'].reason)
        self.assertEqual(self.fx['off'].reason, 'disabled_in_config')
        self.assertIs(self.fx['f1'].bridge, self.main.bridge)
        self.assertIs(self.fx['f1'].state, self.main.state)
        self.assertEqual(self.main.account_id, 7)

    def test_main_routes_are_unchanged_by_the_follower_wrapper(self):
        for path in ('/state/' + TOKEN, '/state/' + TOKEN + '?trade_id=x%3Along', '/account/' + TOKEN,
                     '/health/' + TOKEN, '/state/' + 'x' * len(TOKEN), '/nope'):
            a, b = self.call(self.plain, path), self.call(self.base, path)
            for body in (a[1], b[1]):
                body.pop('observed_at', None)
            self.assertEqual(a, b, path)
        # Follower paths do not exist on the 1.0.6 handler.
        self.assertEqual(self.call(self.plain, '/followers/' + TOKEN)[0], 404)
        self.assertEqual(self.call(self.plain, f'/follower/{TOKEN}/f1/state')[0], 404)
        # The main order route answers as before (guard refusal, nothing sent).
        bad = entry_payload(action='sell')
        self.assertEqual(self.call(self.plain, '/order/' + TOKEN, bad), self.call(self.base, '/order/' + TOKEN, bad))
        self.assertEqual(self.page.sent, [])

    def test_follower_state_summary_and_path_checks(self):
        status, s = self.call(self.base, f'/follower/{TOKEN}/f1/state?trade_id=t%3Along')
        self.assertEqual(status, 200)
        self.assertEqual((s['account_id'], s['follower'], s['net_position'], s['synced'], s['healthy']),
                         (8, 'f1', 0, True, True))
        self.assertEqual((s['publish_mode'], s['soup_account_id'], s['trade_entry_outcome']), ('none', None, None))
        self.assertNotIn('F-ONE', json.dumps(s))
        # 1.0.8: with trade_id the follower route adds `trade` (that trade's orders on this follower).
        self.assertEqual(set(account_state(self.main, extended=True)) | {'follower'},
                         set(s) - {'trade_entry_outcome', 'trade'})
        self.assertEqual(s['trade'], {'commands': {}, 'stop': None})
        self.assertNotIn('trade', self.call(self.base, f'/follower/{TOKEN}/f1/state')[1])
        status, summary = self.call(self.base, '/followers/' + TOKEN)
        rows = {r['key']: r for r in summary['followers']}
        self.assertEqual((rows['f1']['available'], rows['gone']['available'], rows['off']['available']),
                         (True, False, False))
        import hashlib
        self.assertEqual(rows['f1']['account_name_sha256'], hashlib.sha256(b'F-ONE').hexdigest())
        self.assertEqual((rows['f1']['logged_in'], rows['f1']['orders'], summary['main']['account_id']), (True, [], 7))
        self.assertNotIn(TOKEN, json.dumps(summary))
        self.assertNotIn('F-ONE', json.dumps(summary))
        self.assertEqual(self.call(self.base, '/followers/' + TOKEN + '?x=1')[0], 400)
        _, with_trade = self.call(self.base, '/followers/' + TOKEN + '?trade_id=t%3Along')
        self.assertEqual(with_trade['main']['trade'], [])
        for path in (f'/follower/{"x" * len(TOKEN)}/f1/state', f'/follower/{TOKEN}/nobody/state',
                     f'/follower/{TOKEN}/f1/order', f'/follower/{TOKEN}/f1', '/followers/' + 'y' * len(TOKEN),
                     '/followers/' + TOKEN + '/x'):
            self.assertEqual(self.call(self.base, path)[0], 404, path)
        self.assertEqual(self.call(self.base, f'/follower/{TOKEN}/f1/state?a=1')[0], 400)
        self.assertEqual(self.call(self.base, f'/follower/{TOKEN}/gone/state')[0], 503)

    def test_follower_orders_use_their_own_journal_ids_and_tags(self):
        p = entry_payload()
        status, r = self.call(self.base, f'/follower/{TOKEN}/f2/order', p)
        self.assertEqual((status, r['outcome'], r['receipt']['follower']), (200, 'accepted', 'f2'))
        status, r = self.call(self.base, '/order/' + TOKEN, p)
        self.assertEqual(r['outcome'], 'accepted')
        (ep1, body1), (ep2, body2) = self.page.sent
        self.assertEqual((ep1, body1['accountId'], ep2, body2['accountId']), ('order/placeorder', 9, 'order/placeorder', 7))
        self.assertEqual(body2['text'], client_tag('soup-abc'))  # the main account: exactly as in 1.0.6
        self.assertEqual(body1['text'], client_tag('f:f2:soup-abc'))
        self.assertNotEqual(body1['text'], body2['text'])
        self.assertIsNotNone(self.fx['f2'].journal.get('f:f2:soup-abc'))
        self.assertIsNone(self.fx['f2'].journal.get('soup-abc'))
        self.assertIsNone(self.main.journal.get('f:f2:soup-abc'))
        # A repeat is answered from the follower journal, never sent again.
        status, r = self.call(self.base, f'/follower/{TOKEN}/f2/order', p)
        self.assertTrue(r['receipt']['duplicate'])
        self.assertEqual(len(self.page.sent), 2)
        # The follower's unreported order blocks only that follower's next entry.
        _, r = self.call(self.base, f'/follower/{TOKEN}/f2/order', entry_payload('soup-next', '1002:M:1:long'))
        self.assertEqual(r['outcome'], 'rejected')
        # The main /state reports only the main account's own order (9002), never the follower's (9001).
        _, s = self.call(self.base, '/state/' + TOKEN)
        self.assertEqual((s['unseen_accepted_orders'], s['unresolved_commands']), (['9002'], 0))

    def test_unavailable_follower_is_refused_and_main_unaffected(self):
        _, r = self.call(self.base, f'/follower/{TOKEN}/gone/order', entry_payload())
        self.assertEqual(r['outcome'], 'rejected')
        self.assertIn('follower_unavailable', r['receipt']['reason'])
        _, r = self.call(self.base, f'/follower/{TOKEN}/off/order', entry_payload())
        self.assertIn('disabled_in_config', r['receipt']['reason'])
        self.assertEqual(self.page.sent, [])
        _, r = self.call(self.base, '/order/' + TOKEN, entry_payload())
        self.assertEqual(r['outcome'], 'accepted')

    def test_follower_takes_the_alert_size_as_is(self):
        # No size cap: f1's legacy max_contracts 3 is ignored; nothing is clipped.
        _, r = self.call(self.base, f'/follower/{TOKEN}/f1/order', entry_payload(qty=4))
        self.assertEqual(r['outcome'], 'accepted', r['receipt'])

    def test_kill_switches(self):
        Path(self.cfg['followers'][0]['kill_switch_file']).touch()  # f1 only
        _, r = self.call(self.base, f'/follower/{TOKEN}/f1/order', entry_payload())
        self.assertEqual(r['receipt']['reason'], 'entries disabled by kill switch')
        _, r = self.call(self.base, f'/follower/{TOKEN}/f2/order', entry_payload())
        self.assertEqual(r['outcome'], 'accepted')
        Path(self.cfg['kill_switch_file']).touch()  # the main kill switch stops follower entries too
        _, r = self.call(self.base, f'/follower/{TOKEN}/f2/order', entry_payload('soup-2', '1002:M:1:long'))
        self.assertEqual(r['receipt']['reason'], 'entries disabled by kill switch')
        self.assertTrue(self.call(self.base, '/followers/' + TOKEN)[1]['followers'][1]['entries_disabled'])


class HedgeAcrossAccounts(unittest.TestCase):
    """The anti-hedge guard spans the main account and every follower of the login."""

    def make(self, nets, working=(), with_followers=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = Path(tmp.name)
        state = AccountState()
        page = LoginDouble(state, nets, working)
        cfg = {'ticker': 'MNQZ2021', 'account_name': 'MAIN-ACCT', 'soup_account_id': 'S', 'max_contracts': 10,
               'tick_size': .25, 'journal_dir': str(t / 'j'), 'kill_switch_file': str(t / 'K'),
               'followers': [follower_entry('f1', 'F-ONE', t)]}
        main = Executor(page, state, Journal(cfg['journal_dir']), cfg)
        main.resolve()
        if not with_followers:
            return page, main, None
        return page, main, followers.build(page, state, cfg, main)['f1']

    def test_leader_is_never_blocked_by_a_follower_position_or_order(self):
        page, main, f1 = self.make({8: -2}, working=((8, 'Sell'),))
        self.assertEqual(main.hedge_exempt_accounts, frozenset({8}))
        outcome, receipt = main.send(entry_payload())
        self.assertEqual(outcome, 'accepted', receipt)
        self.assertEqual(page.sent[0][1]['accountId'], 7)

    def test_leader_is_still_blocked_by_an_account_that_is_not_a_follower(self):
        page, main, f1 = self.make({10: -2})
        outcome, receipt = main.send(entry_payload())
        self.assertEqual(outcome, 'rejected')
        self.assertIn('hedge guard', receipt['reason'])
        self.assertIn('1 other account(s)', receipt['reason'])
        self.assertEqual(page.sent, [])

    def test_without_followers_the_leader_guard_is_the_1_0_6_guard(self):
        page, main, _ = self.make({8: -2}, with_followers=False)
        self.assertEqual(main.hedge_exempt_accounts, frozenset())
        outcome, receipt = main.send(entry_payload())
        self.assertEqual(outcome, 'rejected')
        self.assertIn('hedge guard', receipt['reason'])

    def test_follower_guard_still_sees_the_leader(self):
        page, main, f1 = self.make({7: -1})
        outcome, receipt = f1.send(entry_payload())
        self.assertEqual(outcome, 'rejected')
        self.assertIn('hedge guard', receipt['reason'])
        self.assertEqual(page.sent, [])

    def test_same_side_positions_do_not_block(self):
        page, main, f1 = self.make({7: 3})
        outcome, _ = f1.send(entry_payload())
        self.assertEqual(outcome, 'accepted')


def align_payload(qty, cid='align-flatten:t', tid='fixture-stream:MNQZ2021:1632147200000:short'):
    return {'ticker': 'MNQZ2021', 'action': ALIGN_ACTION, 'quantity': qty,
            'extras': {'soup_command_id': cid, 'soup_trade_id': tid}}


class AlignFlattenCommand(HedgeAcrossAccounts):
    """The follower-only market close used by leader alignment: exact quantity, never a flip."""

    def test_closes_exact_long_with_a_sell(self):
        page, main, f1 = self.make({8: 2})
        outcome, receipt = f1.send(align_payload(2))
        self.assertEqual(outcome, 'accepted', receipt)
        (endpoint, body), = page.sent
        self.assertEqual((endpoint, body['accountId'], body['action'], body['orderQty'], body['orderType']),
                         ('order/placeorder', 8, 'Sell', 2, 'Market'))
        self.assertEqual(body['text'], client_tag('f:f1:align-flatten:t'))
        self.assertEqual(f1.journal.get('f:f1:align-flatten:t')['kind'], ALIGN_ACTION)

    def test_closes_exact_short_with_a_buy_even_when_killed(self):
        page, main, f1 = self.make({8: -3})
        Path(f1.cfg['kill_switch_file']).touch()
        outcome, _ = f1.send(align_payload(3))
        self.assertEqual((outcome, page.sent[0][1]['action'], page.sent[0][1]['orderQty']), ('accepted', 'Buy', 3))

    def test_refusals_never_send(self):
        for nets, working, payload, reason in (
                ({8: 2}, (), align_payload(3), 'differs from the live position'),
                ({8: 2}, ((8, 'Sell'),), align_payload(2), 'cancel them first'),
                ({}, (), align_payload(1), 'already flat'),
                ({8: 2}, (), dict(align_payload(2), orderType='market'), 'align_flatten payload'),
                ({8: 2}, (), align_payload(0), 'invalid quantity')):
            page, main, f1 = self.make(nets, working)
            outcome, receipt = f1.send(payload)
            self.assertEqual(outcome, 'rejected')
            self.assertIn(reason, receipt['reason'])
            self.assertEqual(page.sent, [])

    def test_the_leader_does_not_accept_align_commands(self):
        page, main, _ = self.make({7: 2})
        outcome, receipt = main.send(dict(align_payload(2)))
        self.assertEqual(outcome, 'rejected')
        self.assertEqual(page.sent, [])


class VerifyPins(unittest.TestCase):
    """Stage A read-only proof that each follower's name/id pairing comes from the page's own sync."""

    def setUp(self):
        import hashlib
        self.h = lambda n: hashlib.sha256(n.encode()).hexdigest()
        t = Path('/tmp/fo-verify')
        self.cfg = {'account_name': 'DEMO00000000013', 'account_id': 30002222,
                    'journal_dir': '/tmp/fo-verify/j', 'kill_switch_file': '/tmp/fo-verify/K',
                    'followers': [follower_entry('account-b', 'DEMO00000000014', t, account_id=30003333),
                                  follower_entry('account-c', 'DEMO00000000015', t, account_id=30004444,
                                                 prop_profile='unrestricted')]}

    def row(self, key, acct, name, **kw):
        return dict({'key': key, 'available': True, 'account_id': acct, 'account_name_sha256': self.h(name),
                     'synced': True}, **kw)

    def test_matching_pairs_pass(self):
        report = {'followers': [self.row('account-b', 30003333, 'DEMO00000000014'),
                                self.row('account-c', 30004444, 'DEMO00000000015')]}
        ok, rows = followers.verify(self.cfg, report)
        self.assertTrue(ok, rows)

    def test_wrong_id_name_or_unpinned_fails(self):
        for bad in (self.row('account-c', 30004445, 'DEMO00000000015'),
                    self.row('account-c', 30004444, 'DEMO00000000099'),
                    {'key': 'account-c', 'available': False, 'reason': 'unavailable:not visible'},
                    self.row('account-c', 30004444, 'DEMO00000000015', synced=False)):
            report = {'followers': [self.row('account-b', 30003333, 'DEMO00000000014'), bad]}
            ok, rows = followers.verify(self.cfg, report)
            self.assertFalse(ok, bad)
            self.assertEqual({r['key']: r['ok'] for r in rows}, {'account-b': True, 'account-c': False})

    def test_a_disabled_follower_does_not_fail_the_check(self):
        self.cfg['followers'][1]['enabled'] = False
        report = {'followers': [self.row('account-b', 30003333, 'DEMO00000000014'),
                                {'key': 'account-c', 'available': False, 'reason': 'disabled_in_config'}]}
        self.assertTrue(followers.verify(self.cfg, report)[0])


class VerifyCli(VerifyPins):
    def test_cli_reads_followers_and_exits_nonzero_on_mismatch(self):
        report = {'followers': [self.row('account-b', 30003333, 'DEMO00000000014'),
                                self.row('account-c', 30004499, 'DEMO00000000015')]}
        from http.server import BaseHTTPRequestHandler
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                body = json.dumps(report).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.json'
            p.write_text(json.dumps(self.cfg))
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {'TVBRIDGE_TOKEN': TOKEN}), redirect_stdout(buf), \
                    self.assertRaises(SystemExit) as ctx:
                followers.main(['--config', str(p), '--verify', '--base', 'http://127.0.0.1:%d' % srv.server_address[1]])
        self.assertEqual(ctx.exception.code, 1)
        out = json.loads(buf.getvalue())
        self.assertEqual({r['key']: r['checks']['account_id'] for r in out['followers']},
                         {'account-b': True, 'account-c': False})
        self.assertEqual(seen, ['/followers/' + TOKEN])


class GridFormat(unittest.TestCase):
    AT = 1623766800000  # 2021-06-15 14:20:00 UTC (10:20 New York)

    def rows(self, pos='-4', orders='TP1 2', states=('OK', 'OK', 'OK')):
        return [(k, pos, orders, st) for k, st in zip(('AA', 'AB', 'AC'), states)]

    def test_entry_block_matches_reference_example_exactly(self):
        text = grid.render('MNQ', self.AT, grid.event_title('entry', 'short', 4, 5000.0), self.rows())
        self.assertEqual(text, 'MNQ  10:20  SHORT 4 @ 5000.00\n'
                               'ACCT  POS  ORDERS   STATE\n'
                               'AA    -4   TP1 2    OK\n'
                               'AB    -4   TP1 2    OK\n'
                               'AC    -4   TP1 2    OK')

    def test_flat_line_matches_reference_example_exactly(self):
        at = self.AT + 7 * 60000
        self.assertEqual(grid.render_flat('MNQ', at, self.rows('0', '-')),
                         'MNQ  10:27  ALL FLAT\nAA 0 | AB 0 | AC 0   OK')
        self.assertTrue(grid.render_flat('MNQ', at, self.rows('0', '-', ('OK', 'KILLED', 'OK'))).endswith('   KILLED'))

    def test_titles_orders_and_widths(self):
        self.assertEqual(grid.event_title('tp1', 'short', 2, 4991.75), 'TP1 2 @ 4991.75')
        self.assertEqual(grid.event_title('exit', 'short', 2, None), 'EXIT 2')
        self.assertEqual(grid.event_title('entry', 'long', 5, 5000.25), 'LONG 5 @ 5000.25')
        self.assertEqual(grid.orders_text([]), '-')
        self.assertEqual(grid.orders_text([{'id': '9', 'type': 'STP', 'qty': 3}, {'id': '8', 'type': 'LMT', 'qty': 1,
                                                                                 'tp1': True}]), 'TP1 1,STP 3')
        self.assertEqual([grid.label(k) for k in ('account-a', 'ab', 'main')], ['AA', 'AB', 'MAIN'])
        wide = grid.table([('AA', '-10', 'TP1 1,STP 3', 'MISMATCH')])
        self.assertEqual(wide, 'ACCT  POS  ORDERS       STATE\nAA    -10  TP1 1,STP 3  MISMATCH')
        self.assertEqual(grid.hhmm(self.AT), '10:20')

    def report(self, main=(-4, True), rows=None):
        def acct(key, net, tp1, **kw):
            orders = [{'id': key + '-o', 'type': 'LMT', 'qty': 2, 'tp1': True}] if tp1 else []
            return dict({'key': key, 'available': True, 'logged_in': True, 'entries_disabled': False,
                         'net_position': net, 'orders': orders}, **kw)
        return {'main': acct('main', *main), 'followers': rows if rows is not None else
                [acct('ab', -4, True), acct('ac', -4, True)]}

    def test_states(self):
        r = self.report(rows=[dict(self.report()['followers'][0], net_position=0, orders=[]),
                              dict(self.report()['followers'][1], entries_disabled=True)])
        rows = grid.account_rows(dict(r, main_key='account-a'), ['ab', 'ac'])
        self.assertEqual([x[3] for x in rows], ['OK', 'MISMATCH', 'KILLED'])
        rows = grid.account_rows(dict(r, main_key='account-a'), ['ab', 'ac'], skipped={'ab'})
        self.assertEqual(rows[1][3], 'SKIPPED')
        r['main']['logged_in'] = False
        self.assertEqual(grid.account_rows(r, ['ab'])[0][3], 'LOGGED OUT')
        self.assertEqual(grid.account_rows(self.report(), ['ab', 't99'])[2][3], 'MISMATCH')
        self.assertEqual(grid.account_rows(self.report(), ['ab'], unavailable={'ab': 'x'})[1][3], 'MISMATCH')

    def test_tracker_one_block_per_event_after_settle_and_attention_once(self):
        trade = {'id': 'T:short', 'side': 'short', 'phase': 'open'}
        cmds = {'e': {'id': 'e', 'kind': 'entry', 'quantity': 4, 'started_at': 1},
                'p': {'id': 'p', 'kind': 'tp1_limit', 'quantity': 2, 'started_at': 2}}
        tr = grid.GridTracker('MNQ', 'account-a', ['ab', 'ac'], settle_ms=2000, attention_ms=5000)
        idle = self.report(main=(0, False), rows=[dict(self.report()['followers'][i], net_position=0, orders=[])
                                                   for i in range(2)])
        self.assertEqual(tr.update(None, {}, idle, self.AT), [])  # baseline: nothing yet
        filled = self.report()
        filled['main']['trade'] = [{'command_id': 'e', 'quantity': 4, 'filled': 4, 'price': 5000.0,
                                    'fill_ms': self.AT}]
        lagging = dict(filled, followers=[dict(filled['followers'][0], net_position=0, orders=[]),
                                          filled['followers'][1]])
        self.assertEqual(tr.update(trade, cmds, lagging, self.AT + 250), [])        # waits for AB
        [ev] = tr.update(trade, cmds, filled, self.AT + 500)                      # everyone matches: sent
        self.assertEqual((ev['kind'], ev['seq']), ('entry', 1))
        self.assertTrue(ev['text'].startswith('MNQ  10:20  SHORT 4 @ 5000.00\nACCT  POS  ORDERS   STATE\nAA'))
        self.assertEqual(tr.update(trade, cmds, filled, self.AT + 750), [])       # never twice
        # TP1 fills on the leader only: sent after the settle time, showing the mismatch.
        tp = dict(filled, main=dict(filled['main'], net_position=-2, orders=[],
                                    trade=filled['main']['trade'] + [{'command_id': 'p', 'quantity': 2, 'filled': 2,
                                                                      'price': 4991.75, 'fill_ms': self.AT + 1000}]))
        self.assertEqual(tr.update(trade, cmds, tp, self.AT + 1000), [])
        [ev] = tr.update(trade, cmds, tp, self.AT + 3250)
        self.assertEqual(ev['kind'], 'tp1')
        self.assertIn('AB    -4   TP1 2    MISMATCH', ev['text'])
        # The mismatch persists: one ATTENTION block, not one per poll.
        self.assertEqual(tr.update(trade, cmds, tp, self.AT + 4000), [])
        [att] = tr.update(trade, cmds, tp, self.AT + 8500)
        self.assertEqual(att['kind'], 'attention')
        self.assertIn('ATTENTION', att['text'].split('\n')[0])
        self.assertEqual(tr.update(trade, cmds, tp, self.AT + 15000), [])
        # Closed and flat everywhere: ALL FLAT.
        flat = dict(idle, main=dict(idle['main'], trade=tp['main']['trade']))
        [ev] = tr.update(dict(trade, phase='closed'), cmds, flat, self.AT + 420000)
        self.assertEqual(ev['text'], 'MNQ  10:27  ALL FLAT\nAA 0 | AB 0 | AC 0   OK')
        self.assertEqual(tr.status()['seq'], 4)

    def test_restart_does_not_resend_past_events(self):
        trade = {'id': 'T:long', 'side': 'long', 'phase': 'open'}
        cmds = {'e': {'id': 'e', 'kind': 'entry', 'quantity': 5, 'started_at': 1}}
        r = self.report(main=(5, False), rows=[dict(self.report()['followers'][i], net_position=5, orders=[])
                                               for i in range(2)])
        r['main']['trade'] = [{'command_id': 'e', 'quantity': 5, 'filled': 5, 'price': 1.0, 'fill_ms': self.AT}]
        tr = grid.GridTracker('MNQ', 'account-a', ['ab', 'ac'])
        self.assertEqual(tr.update(trade, cmds, r, self.AT + 10), [])


class DaemonConfigCheck(unittest.TestCase):
    def test_bad_followers_stop_the_daemon_before_the_browser(self):
        from tvbridge import daemon
        with tempfile.TemporaryDirectory() as d:
            cfg = {'account_name': 'MAIN', 'soup_account_id': 'S', 'ticker': 'MNQZ2021', 'journal_dir': d + '/j',
                   'max_contracts': 10,
                   'kill_switch_file': d + '/K', 'devtools_port': dead_port(), 'page_url_contains': 'x',
                   'trade_url_pattern': 'x', 'publish_mode': 'evidence',
                   'followers': [follower_entry('a', 'DEMO-1', Path(d), prop_profile='apex')]}
            p = Path(d) / 'config.json'
            p.write_text(json.dumps(cfg))
            argv, env = sys.argv, os.environ.get('TVBRIDGE_TOKEN')
            sys.argv, os.environ['TVBRIDGE_TOKEN'] = ['daemon', '--config', str(p)], TOKEN
            try:
                with self.assertRaises(SystemExit) as ctx, mock.patch('logging.basicConfig'):
                    daemon.main()
            finally:
                sys.argv = argv
                if env is None:
                    os.environ.pop('TVBRIDGE_TOKEN', None)
                else:
                    os.environ['TVBRIDGE_TOKEN'] = env
            self.assertIn('Apex', str(ctx.exception))
            self.assertFalse(os.path.exists(d + '/j'))


# ----------------------------------------------------------------------------------------
# Entry gate and parallel lanes against Soup's real runtime (browser-free)
# ----------------------------------------------------------------------------------------

class SlowRecorder(Recorder):
    """A follower route stand-in that takes `delay` seconds and records when each send began."""

    def __init__(self, delay=0.0, outcome='accepted'):
        super().__init__(outcome=outcome)
        self.delay, self.started = delay, []

    def send(self, payload):
        self.started.append(time.monotonic())
        time.sleep(self.delay)
        return super().send(payload)


class Unreachable:
    def get(self, trade_id=None):
        raise OSError('no bridge in this test')


class ScriptedState:
    """A follower /state stand-in: flat and ready unless told otherwise."""

    def __init__(self, net=0, orders=(), killed=False):
        self.net, self.orders, self.killed = net, list(orders), killed
        self.calls = 0

    def get(self, trade_id=None):
        self.calls += 1
        return {'net_position': self.net, 'open_order_ids': list(self.orders), 'synced': True, 'healthy': True,
                'unseen_accepted_orders': [], 'entries_disabled': self.killed, 'trade_entry_outcome': None}


class GateAndLanes(SoupSide, unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='tvbridge-firing-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.main_route = SlowRecorder()
        self.soup_setup(self.tmp, self.main_route, lambda: time.time_ns() // 1_000_000)

    def firing_cfg(self, **over):
        c = {'soup_db': str(self.soup_db), 'soup_engine_dir': str(SOUP / 'engine'),
             'soup_quantity_policy': str(self.tmp / 'policy.json'), 'soup_pairing': str(self.tmp / 'pairing.json'),
             'ticker': CONFIG['ticker'], 'bridge_url': 'http://127.0.0.1:%d' % dead_port(),
             'firing_dir': str(self.tmp / 'firing'), 'firing_order': ['aa', 'ac', 'ab'],
             'accounts': {'aa': {'role': 'main'}, 'ac': {'role': 'follower'}, 'ab': {'role': 'follower'}},
             'reconcile_grace_ms': 300, 'reconcile_settle_ms': 300, 'reconcile_deadline_ms': 2000,
             'main_gate_timeout_ms': 3000}
        c.update(over)
        return c

    def firing(self, routes, states=None, **over):
        f = Firing(self.firing_cfg(**over), TOKEN, transports=routes,
                   states=states or {k: ScriptedState() for k in routes}, report=Unreachable())
        self.addCleanup(f.close)
        return f

    def lane_records(self, key):
        return records(self.tmp / 'firing' / key / JOURNAL_NAME)

    def test_followers_get_soup_bytes_only_after_main_accepted(self):
        routes = {'ac': SlowRecorder(), 'ab': SlowRecorder()}
        f = self.firing(routes)
        for lane in f.lanes.values():
            lane.poll_once()  # baseline on an idle Soup
        self.enter_soup('long')
        for lane in f.lanes.values():
            lane.poll_once()
        for key in ('ac', 'ab'):
            self.assertEqual(routes[key].sent, self.main_route.sent)  # entry and TP1, byte for byte
            gate = f.lanes[key].status['last_main_gate']
            self.assertEqual((gate['main_outcome'], gate['main_status']), ('accepted', 'accepted'))
        self.assertEqual(len(self.main_route.sent), 2)

    def test_main_outcome_reads_soups_transport_result(self):
        mo = FollowerLane.main_outcome
        self.assertEqual([mo(None), mo({'status': 'ready'}), mo({'status': 'started'})], [None, None, None])
        self.assertEqual(mo({'status': 'accepted', 'receipt': {'transport_outcome': 'accepted'}}), 'accepted')
        # Broker evidence moves the status on after the send; the transport outcome still decides.
        self.assertEqual(mo({'status': 'filled', 'receipt': {'transport_outcome': 'accepted'}}), 'accepted')
        self.assertEqual(mo({'status': 'cancelled', 'receipt': {'transport_outcome': 'accepted'}}), 'broker_cancelled')
        self.assertEqual(mo({'status': 'rejected', 'receipt': {'transport_outcome': 'accepted'}}), 'broker_rejected')
        self.assertEqual(mo({'status': 'rejected', 'receipt': {'transport_outcome': 'rejected'}}), 'rejected')
        self.assertEqual(mo({'status': 'unknown', 'receipt': {}}), 'unknown')
        self.assertEqual(mo({'status': 'superseded'}), 'status_superseded')

    def test_main_rejected_entry_is_not_fired_and_alerted(self):
        self.main_route.outcome = 'rejected'
        routes = {'ac': SlowRecorder(), 'ab': SlowRecorder()}
        f = self.firing(routes)
        for lane in f.lanes.values():
            lane.poll_once()
        self.enter_soup('long')
        self.assertEqual(self.trade['phase'], 'closed')
        for lane in f.lanes.values():
            lane.poll_once()
        for key in ('ac', 'ab'):
            self.assertEqual(routes[key].sent, [])
            skip = next(r for r in self.lane_records(key) if r.get('kind') == 'entry')
            self.assertEqual((skip['type'], skip['reason'], skip['main_status']),
                             ('skipped', 'main_rejected', 'rejected'))
        alerts = records(self.tmp / 'firing' / ALERTS_NAME)
        self.assertEqual(sorted((a['account'], a['kind'], a['reason']) for a in alerts),
                         [('ab', 'entry_not_fired', 'main_rejected'), ('ac', 'entry_not_fired', 'main_rejected')])
        self.assertEqual(os.stat(self.tmp / 'firing' / ALERTS_NAME).st_mode & 0o777, 0o600)

    def test_main_still_in_flight_times_out_and_is_skipped(self):
        routes = {'ac': SlowRecorder()}
        f = self.firing(routes, main_gate_timeout_ms=200,
                        accounts={'aa': {'role': 'main'}, 'ac': {'role': 'follower'},
                                  'ab': {'role': 'follower', 'enabled': False}})
        self.assertEqual(list(f.lanes), ['ac'])
        lane = f.lanes['ac']
        lane.poll_once()
        seen = {}

        class InFlight(SlowRecorder):
            def send(inner, payload):
                if 'entry' not in seen:  # the follower looks while Soup's main send is still open
                    seen['entry'] = True
                    lane.poll_once()
                return SlowRecorder.send(inner, payload)
        self.service.transport = InFlight()
        self.enter_soup('long')
        skip = next(r for r in self.lane_records('ac') if r.get('kind') == 'entry')
        self.assertEqual((skip['reason'], skip['main_status']), ('main_gate_timeout', 'started'))
        lane.poll_once()
        self.assertEqual(routes['ac'].sent, [])  # its TP1 is skipped too: entry not fired
        tp1 = next(r for r in self.lane_records('ac') if r.get('kind') == 'tp1_limit')
        self.assertEqual(tp1['reason'], 'entry_not_mirrored')

    def test_gate_none_fires_without_waiting_for_main(self):
        routes = {'ac': SlowRecorder()}
        f = self.firing(routes, entry_gate='none',
                        accounts={'aa': {'role': 'main'}, 'ac': {'role': 'follower'},
                                  'ab': {'role': 'follower', 'enabled': False}})
        lane = f.lanes['ac']
        lane.poll_once()

        class InFlight(SlowRecorder):
            def send(inner, payload):
                lane.poll_once()
                return SlowRecorder.send(inner, payload)
        self.service.transport = InFlight()
        self.enter_soup('long')
        self.assertEqual(json.loads(routes['ac'].sent[0])['action'], 'buy')

    def test_followers_fire_in_parallel_after_main(self):
        self.main_route.delay = 0.3
        routes = {'ac': SlowRecorder(0.4), 'ab': SlowRecorder(0.4)}
        f = self.firing(routes, poll_interval_ms=10)
        f.start()
        self.addCleanup(f.stop)
        until(lambda: all(l.baselined for l in f.lanes.values()), 5, 'baseline')
        done = {}
        real = self.main_route.send

        def main_send(payload):
            out = real(payload)
            done.setdefault('entry_at', time.monotonic())
            return out
        self.main_route.send = main_send
        self.enter_soup('long')
        until(lambda: all(len(r.sent) == 2 for r in routes.values()), 10, 'both followers got entry and TP1')
        ec, eb = routes['ac'].started[0], routes['ab'].started[0]
        self.assertGreaterEqual(min(ec, eb), done['entry_at'] - 0.01)  # main first
        self.assertLess(abs(ec - eb), 0.25)  # parallel: not one 0.4 s round trip after the other
        f.write_status()
        status = json.loads((self.tmp / 'firing' / 'status.json').read_text())
        self.assertEqual((status['main'], status['firing_mode'], status['entry_gate']),
                         ('aa', 'parallel', 'main_accepted'))
        self.assertEqual(sorted(status['lanes']), ['ab', 'ac'])
        self.assertTrue(all(l['running'] and l['counts']['accepted'] == 2 for l in status['lanes'].values()))
        self.assertEqual(os.stat(self.tmp / 'firing').st_mode & 0o777, 0o700)

    def one_lane(self, **over):
        return dict(accounts={'aa': {'role': 'main'}, 'ac': {'role': 'follower'},
                              'ab': {'role': 'follower', 'enabled': False}}, **over)

    def test_misaligned_follower_is_flattened_exactly_before_the_new_entry(self):
        route, state = SlowRecorder(), ScriptedState(net=2, orders=['77'])
        real = route.send

        def send(payload):  # the stand-in account reacts like Tradovate would
            out = real(payload)
            if payload['action'] == 'cancel':
                state.orders = []
            elif payload['action'] == ALIGN_ACTION:
                state.net = 0
            return out
        route.send = send
        f = self.firing({'ac': route}, {'ac': state}, **self.one_lane())
        lane = f.lanes['ac']
        lane.poll_once()
        self.enter_soup('short')
        lane.poll_once()
        actions = [json.loads(p)['action'] for p in route.sent]
        self.assertEqual(actions, ['cancel', ALIGN_ACTION, 'sell', 'exit'])
        self.assertEqual(json.loads(route.sent[1])['quantity'], 2)  # exact, never a flip
        self.assertEqual(route.sent[2:], self.main_route.sent)       # then Soup's own bytes
        self.assertEqual(lane.status['last_alignment']['result'], 'aligned')
        self.assertEqual(lane.status['last_alignment']['before'], {'net_position': 2, 'open_order_ids': ['77']})

    def test_flatten_timeout_skips_the_entry_and_alerts_leader_unaffected(self):
        route, state = SlowRecorder(), ScriptedState(net=2)  # the close never shows as filled
        f = self.firing({'ac': route}, {'ac': state}, align_wait_ms=400, **self.one_lane())
        lane = f.lanes['ac']
        lane.poll_once()
        self.enter_soup('long')
        lane.poll_once()
        self.assertEqual([json.loads(p)['action'] for p in route.sent], [ALIGN_ACTION])  # sent once, not retried
        skip = next(r for r in self.lane_records('ac') if r.get('kind') == 'entry')
        self.assertEqual((skip['type'], skip['reason']), ('skipped', 'align_timeout:position_not_closed'))
        self.assertEqual([json.loads(p)['action'] for p in self.main_route.sent], ['buy', 'exit'])
        alerts = records(self.tmp / 'firing' / ALERTS_NAME)
        self.assertIn(('ac', 'entry_not_fired', 'align_timeout:position_not_closed'),
                      [(a['account'], a['kind'], a.get('reason')) for a in alerts])
        tp1 = next(r for r in self.lane_records('ac') if r.get('kind') == 'tp1_limit')
        self.assertEqual(tp1['reason'], 'entry_not_mirrored')

    def test_killed_follower_is_not_aligned_or_entered(self):
        route, state = SlowRecorder(), ScriptedState(net=1, killed=True)
        f = self.firing({'ac': route}, {'ac': state}, **self.one_lane())
        lane = f.lanes['ac']
        lane.poll_once()
        self.enter_soup('long')
        lane.poll_once()
        self.assertEqual(route.sent, [])
        skip = next(r for r in self.lane_records('ac') if r.get('kind') == 'entry')
        self.assertEqual(skip['reason'], 'follower_killed')
        self.assertTrue(lane.entry_skipped(self.trade['id']))

    def test_flat_follower_sends_no_alignment(self):
        route = SlowRecorder()
        f = self.firing({'ac': route}, **self.one_lane())
        f.lanes['ac'].poll_once()
        self.enter_soup('long')
        f.lanes['ac'].poll_once()
        self.assertEqual(route.sent, self.main_route.sent)
        self.assertFalse([r for r in self.lane_records('ac') if r['command_id'].startswith('align-')])

    def test_unpinned_follower_is_refused_at_start(self):
        class Report:
            def get(self, trade_id=None):
                return {'followers': [{'key': 'ac', 'available': False, 'reason': 'unavailable:id mismatch'},
                                      {'key': 'ab', 'available': True}]}
        f = Firing(self.firing_cfg(), TOKEN, transports={'ab': SlowRecorder()}, states={'ab': ScriptedState()},
                   report=Report())
        self.addCleanup(f.close)
        self.assertEqual((list(f.lanes), f.unavailable), (['ab'], {'ac': 'unavailable:id mismatch'}))
        self.assertEqual([(a['account'], a['kind']) for a in records(self.tmp / 'firing' / ALERTS_NAME)],
                         [('ac', 'follower_unavailable')])

    def test_second_service_on_same_dir_refused_and_apex_refused(self):
        f = self.firing({'ac': SlowRecorder(), 'ab': SlowRecorder()})
        with self.assertRaises(RuntimeError):
            Firing(self.firing_cfg(), TOKEN)
        f.close()
        with self.assertRaises(FiringConfigError):
            Firing(self.firing_cfg(accounts={'aa': {'role': 'main'}, 'ac': {'role': 'follower'},
                                             'ab': {'role': 'follower', 'prop_profile': 'apex'}}), TOKEN)

    def test_restart_never_resends(self):
        routes = {'ac': SlowRecorder(), 'ab': SlowRecorder()}
        f = self.firing(routes)
        for lane in f.lanes.values():
            lane.poll_once()
        self.enter_soup('long')
        for lane in f.lanes.values():
            lane.poll_once()
        f.close()
        again = {'ac': SlowRecorder(), 'ab': SlowRecorder()}
        f2 = self.firing(again)
        for _ in range(3):
            for lane in f2.lanes.values():
                lane.poll_once()
        self.assertEqual([r.sent for r in again.values()], [[], []])


# ----------------------------------------------------------------------------------------
# End to end: headless Chrome, one page, one socket, main + two followers of one login.
# ----------------------------------------------------------------------------------------

FC, FB = 116, 118


class FiringE2E(SoupSide, unittest.TestCase):
    FOLLOWERS = (('ac', 'ACCOUNT-C', FC, {}), ('ab', 'ACCOUNT-B', FB, {}))
    FIRING_OVERRIDES = {}
    # These lifecycles use a 3-contract entry (TP1 1 + parent runner 1 + EOD runner 1);
    # test_mirror's FIXTURE enters 5.
    ENTRY = {'fixture_int_07': 1, 'fixture_int_06': 1, 'fixture_int_01': 0, 'fixture_int_04': 1}

    def enter_soup(self, side, changes=None):
        return super().enter_soup(side, dict(self.ENTRY, **(changes or {})))

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.fake = self.h.fake
        self.fake.contracts[CONTRACT] = {'id': CONTRACT, 'name': SYMBOL}
        for _, name, acct, _ in self.FOLLOWERS:
            if acct is not None:
                self.fake.accounts.append({'id': acct, 'name': name, 'userId': 7, 'active': True})
        self.ny_clock(11, 0, 0)
        self.state = AccountState()
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port, page_url_contains='/trader/',
                             trade_url_pattern=r'^ws://127\.0\.0\.1:\d+/v1/websocket$')
        self.addCleanup(self.bridge.detach)
        self.bridge.attach(reload=True)
        self.bridge.wait_ready(timeout=20)
        tmp = self.h.tmp
        self.cfg = self.h.config(ticker=CONFIG['ticker'], soup_account_id=CONFIG['account_id'], max_contracts=LIMIT,
                                 journal_dir=str(tmp / 'journal-main'),
                                 followers=[follower_entry(k, name, tmp, **extra)
                                            for k, name, _, extra in self.FOLLOWERS])
        self.main = Executor(self.bridge, self.state, Journal(self.cfg['journal_dir']), self.cfg, clock_ms=self.now)
        self.main.resolve()
        self.fx = followers.build(self.bridge, self.state, self.cfg, self.main, clock_ms=self.now)
        handler = followers.make_follower_handler(make_handler(self.main, Pub(), TOKEN), self.fx, TOKEN, self.main)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.bridge_url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        # Soup's main route: POST /order on the daemon, exactly as the live bridge transport.
        self.soup_setup(tmp, Recorder(TradovateBridgeTransport(self.bridge_url, TOKEN)), self.now)
        cfg = {'soup_db': str(self.soup_db), 'soup_engine_dir': str(SOUP / 'engine'),
               'soup_quantity_policy': str(tmp / 'policy.json'), 'soup_pairing': str(tmp / 'pairing.json'),
               'ticker': CONFIG['ticker'], 'bridge_url': self.bridge_url, 'firing_dir': str(tmp / 'firing'),
               'firing_order': ['aa'] + [k for k, *_ in self.FOLLOWERS],
               'accounts': dict({'aa': {'role': 'main', 'label': 'Account A'}},
                                **{k: {'role': 'follower', 'prop_profile': 'cross-account-window'} for k, *_ in self.FOLLOWERS}),
               'reconcile_grace_ms': 300, 'reconcile_settle_ms': 300, 'reconcile_deadline_ms': 20000}
        cfg.update(self.FIRING_OVERRIDES)
        self.firing = Firing(cfg, TOKEN, clock_ms=self.now)
        self.firing_thread = threading.Thread(target=self.firing.run, daemon=True)
        self.firing_thread.start()
        self.addCleanup(self.stop_firing)
        until(lambda: all(l.baselined for l in self.firing.lanes.values()) and self.firing.grid.baselined, 5,
              'lanes and grid baselined')

    def stop_firing(self):
        self.firing.stopping.set()
        self.firing_thread.join(30)
        self.firing.close()

    # --- clock and Soup evidence from the main account (as test_mirror) ----------------------

    def now(self):
        return self.fake.now_ms()

    def skip_ahead(self, ms):
        self.fake.skew_ms += max(0, int(ms))

    def ny_clock(self, hour, minute, second):
        from datetime import datetime
        real = time.time_ns() // 1_000_000
        target = datetime.fromtimestamp(real / 1000, NY).replace(hour=hour, minute=minute, second=second,
                                                                 microsecond=0)
        self.fake.skew_ms = int(target.timestamp() * 1000) - real

    def conserved(self, snap):
        kinds = {c['command_id']: c['kind'] for c in self.main.journal.for_trade(snap['trade_id'])}
        net = sum(o['filled_qty'] * (1 if kinds[o['command_id']] == 'entry' else -1) for o in snap['orders'])
        return net == snap['position_qty']

    def evidence(self, ready=None, timeout=6.0):
        deadline = time.monotonic() + timeout
        reason = None
        while time.monotonic() < deadline:
            snap, reason = self.main.snapshot(self.trade['id'])
            if snap and self.conserved(snap) and (ready is None or ready(snap)):
                self.assertEqual(self.soup('broker', snap), ('processed', 'broker_reconciled'), snap)
                return snap
            time.sleep(0.05)
        self.fail('no settled snapshot: ' + str(reason))

    def run_until_closed(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while self.trade['phase'] != 'closed':
            self.assertLess(time.monotonic(), deadline, self.trade)
            self.service.dispatch(self.now())
            if self.trade['phase'] == 'closed':
                break
            self.evidence()
            time.sleep(0.05)

    def order_in(self, snap, command_id):
        return next(o for o in snap['orders'] if o['command_id'] == command_id)

    def lane_records(self, key):
        return records(self.h.tmp / 'firing' / key / JOURNAL_NAME)

    def caught_up(self, keys=None, timeout=10):
        started = [c['id'] for c in self.engine.data['commands'].values() if c['started_at'] is not None]

        def done():
            for key in keys or [k for k, *_ in self.FOLLOWERS]:
                settled = {r['command_id'] for r in self.lane_records(key) if r['type'] != 'intent'}
                if not set(started) <= settled:
                    return False
            return True
        until(done, timeout, 'lanes caught up')

    def final(self, key, trade_id, timeout=20):
        return until(lambda: next((r for r in self.lane_records(key) if r['type'] == 'reconcile'
                                   and r['trade_id'] == trade_id and r['final']), None), timeout,
                     f'final reconciliation {key}')

    def net(self, acct):
        return self.state.net_position(acct, CONTRACT)

    def enter(self, side):
        entry = self.enter_soup(side)
        self.evidence(lambda s: s['position_qty'] == entry['quantity'] and s['open_orders'])
        self.caught_up()
        return entry

    def break_even_exit(self):
        t = self.trade
        self.caught_up()
        self.skip_ahead(t['runner_not_before'] - self.now() + 100)
        be = t['payload']['break_even_price']
        self.fake.set_price(be)
        self.assertEqual(self.quote(be)[0], 'processed')
        self.run_until_closed()

    def hard_stop(self):
        stop = self.trade['payload']['initial_hard_stop_price']
        self.fake.set_price(stop)
        self.assertEqual(self.quote(stop)[0], 'processed')
        self.run_until_closed()

    def grid_event(self, kind, timeout=10):
        """The fake exchange runs a whole trade in well under the service's 0.5 s grid poll; step it here."""
        return until(lambda: self.firing.update_grid() is not None and
                     kind in [e['kind'] for e in self.firing.grid.status()['recent']], timeout, 'grid ' + kind)

    def account_orders(self, acct):
        with self.fake.lock:
            return {oid: o for oid, o in self.fake.orders.items() if o['accountId'] == acct}

    def assert_flat(self, acct):
        until(lambda: self.net(acct) == 0, 5, f'account {acct} flat')
        self.assertEqual([o for o in self.fake.working_orders() if o in self.account_orders(acct)], [])

    def assert_never_opposite(self, acct, side):
        sign = 1 if side == 'long' else -1
        path = [net for a, _, net in self.fake.position_history if a == acct]
        self.assertTrue(all(sign * net >= 0 for net in path), path)


class FiringLifecycle(FiringE2E):
    def test_long_tp1_break_even_fires_to_both_followers_and_all_end_flat(self):
        entry = self.enter('long')
        self.assertEqual(entry['quantity'], 3)
        for acct in (PRIMARY, FC, FB):
            until(lambda: self.net(acct) == 3, 5, f'{acct} entry fill')
        self.grid_event('entry')
        tp1 = next(c for c in self.engine.data['commands'].values() if c['kind'] == 'tp1_limit')
        self.fake.set_price(tp1['price'])  # TP1 fills on every account
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        for acct in (FC, FB):
            until(lambda: self.net(acct) == 2, 5, f'{acct} TP1 fill')
        self.grid_event('tp1')
        self.break_even_exit()
        self.assertEqual((self.trade['phase'], self.trade['remaining']), ('closed', 0))
        self.caught_up()
        self.grid_event('exit')
        self.grid_event('flat')
        soup_payloads = self.primary_transport.sent
        for key, _, acct, _ in self.FOLLOWERS:
            intents = [r for r in self.lane_records(key) if r['type'] == 'intent' and r['source'] == 'soup']
            self.assertEqual([canonical(r['payload']) for r in intents], soup_payloads)  # Soup's bytes
            outs = {r['command_id']: r for r in self.lane_records(key) if r['type'] == 'outcome'}
            self.assertEqual({outs[r['command_id']]['outcome'] for r in intents}, {'accepted'})
            self.assertEqual(self.final(key, self.trade['id'])['result'], 'flat')
            self.assert_flat(acct)
            self.assert_never_opposite(acct, 'long')
            self.assertEqual(self.fx[key].journal.unresolved(), [])
            ids = [c['command_id'] for c in self.fx[key].journal.for_trade(self.trade['id'])]
            self.assertTrue(ids and all(i.startswith(f'f:{key}:') for i in ids), ids)
        self.assert_flat(PRIMARY)
        # The main account's journal holds only Soup's own ids, as in 1.0.6.
        main_ids = [c['command_id'] for c in self.main.journal.for_trade(self.trade['id'])]
        self.assertEqual(main_ids, [json.loads(p)['extras']['soup_command_id'] for p in soup_payloads])
        # Order text tags never repeat across accounts.
        with self.fake.lock:
            tags = [self.fake.versions[o]['text'] for o in self.fake.orders
                    if self.fake.orders[o]['accountId'] in (PRIMARY, FC, FB) and self.fake.versions[o].get('text')]
        self.assertEqual(len(tags), len(set(tags)), tags)
        # Followers fired only after the main account's entry was accepted.
        entry_id = self.engine.data['commands'][[c for c in self.engine.data['commands']
                                                 if self.engine.data['commands'][c]['kind'] == 'entry'][0]]['id']
        main_entry_at = self.main.journal.get(entry_id)['at']
        for key in ('ac', 'ab'):
            intent = next(r for r in self.lane_records(key) if r['type'] == 'intent' and r['command_id'] == entry_id)
            out = next(r for r in self.lane_records(key) if r['type'] == 'outcome' and r['command_id'] == entry_id)
            self.assertGreaterEqual(intent['sent_at'], main_entry_at)  # main first
            self.assertLessEqual(out['latency_ms'], 3000, out)
        for key in ('ac', 'ab'):  # both followers were flat: no alignment was needed
            self.assertFalse([r for r in self.lane_records(key) if r.get('command_id', '').startswith('align-')])
        # Grid: one block per event, in order, every account OK.
        texts = [e['text'] for e in self.firing.grid.status()['recent']]
        self.assertEqual([e['kind'] for e in self.firing.grid.status()['recent']], ['entry', 'tp1', 'exit', 'flat'])
        self.assertRegex(texts[0], r'^MNQ  \d\d:\d\d  LONG 3 @ \d+\.\d\d\nACCT  POS  ORDERS   STATE\n'
                                   r'AA    3    TP1 1    OK\nAC    3    TP1 1    OK\nAB    3    TP1 1    OK$')
        self.assertIn('TP1 1 @', texts[1])
        self.assertIn('EXIT 2 @', texts[2])
        self.assertRegex(texts[3], r'^MNQ  \d\d:\d\d  ALL FLAT\nAA 0 \| AC 0 \| AB 0   OK$')
        self.firing.write_status()
        status = json.loads((self.h.tmp / 'firing' / 'status.json').read_text())
        self.assertEqual((status['firing_order'], status['grid']['last']['kind']), (['aa', 'ac', 'ab'], 'flat'))

    def test_tp1_on_main_only_followers_close_exact_remainder(self):
        self.enter('long')
        main_tp1 = int(self.main.journal.get(next(c['id'] for c in self.engine.data['commands'].values()
                                                  if c['kind'] == 'tp1_limit'))['broker_order_id'])
        self.fake.fill_limit(main_tp1, 1)
        tp1 = next(c for c in self.engine.data['commands'].values() if c['kind'] == 'tp1_limit')
        self.evidence(lambda s: self.order_in(s, tp1['id'])['status'] == 'filled')
        self.break_even_exit()  # Soup: cancel (followers' TP1 cancelled too), reduce 2
        self.caught_up()
        for key, _, acct, _ in self.FOLLOWERS:
            final = self.final(key, self.trade['id'])
            self.assertEqual(final['result'], 'flat', final)
            self.assertEqual([(a['command_id'].split(':')[0], a['outcome'], a.get('quantity'))
                              for a in final['actions']], [('mirror-flatten', 'accepted', 1)])
            self.assert_flat(acct)
            self.assert_never_opposite(acct, 'long')
        self.assert_flat(PRIMARY)


class FiringFollowerFault(FiringE2E):
    """ac is not in this login (refused at start); ab's own kill switch is set. The leader trades on."""
    FOLLOWERS = (('ac', 'ACCOUNT-C-MISSING', None, {}), ('ab', 'ACCOUNT-B', FB, {}))
    FIRING_OVERRIDES = {'grid_attention_ms': 1000}

    def test_follower_faults_never_block_the_leader(self):
        Path(self.cfg['followers'][1]['kill_switch_file']).touch()
        self.assertIsInstance(self.fx['ac'], followers.Unavailable)
        self.assertEqual((list(self.firing.lanes), list(self.firing.unavailable)), (['ab'], ['ac']))
        self.enter_soup('short')
        until(lambda: self.net(PRIMARY) == -3, 5, 'leader entry fill')
        self.hard_stop()
        self.assertEqual((self.trade['phase'], self.trade['remaining']), ('closed', 0))
        main_out = {c['command_id']: c['outcome'] for c in self.main.journal.for_trade(self.trade['id'])}
        self.assertEqual(set(main_out.values()), {'accepted'})
        self.assert_flat(PRIMARY)
        self.caught_up(['ab'])
        recs = self.lane_records('ab')
        entry = next(r for r in recs if r.get('kind') == 'entry')
        self.assertEqual((entry['type'], entry['reason']), ('skipped', 'follower_killed'))
        self.assertTrue(all(r['reason'] in ('follower_killed', 'entry_not_mirrored')
                            for r in recs if r['type'] == 'skipped'))
        self.assertFalse([r for r in recs if r['type'] == 'intent' and r['source'] != 'reconcile'])
        self.assertEqual(self.final('ab', self.trade['id'])['result'], 'nothing_to_do')
        self.assertEqual(self.account_orders(FB), {})
        alerts = records(self.h.tmp / 'firing' / ALERTS_NAME)
        kinds = {(a['account'], a['kind'], a.get('reason')) for a in alerts}
        self.assertIn(('ac', 'follower_unavailable', 'unavailable:configured Tradovate account is not visible '
                                                      'in this login'), kinds)
        self.assertIn(('ab', 'entry_not_fired', 'follower_killed'), kinds)
        # Grid: ATTENTION names the unpinned and the killed follower.
        att = until(lambda: next((e for e in self.firing.grid.status()['recent'] if e['kind'] == 'attention'), None),
                    10, 'attention block')
        self.assertIn('AC    ?    -        MISMATCH', att['text'])
        self.assertIn('KILLED', att['text'])


class FiringAlignment(FiringE2E):
    """Leader alignment on the fake exchange: the leader is never blocked; followers align first."""

    def manual(self, acct, action, qty, order_type='Market', price=None):
        body = {'accountId': acct, 'action': action, 'symbol': SYMBOL, 'orderQty': qty, 'orderType': order_type,
                'text': 'by-hand'}
        if price is not None:
            body['price'] = price
        with self.fake.lock:
            self.fake._place(body, lambda *a, **k: None)

    def align_actions(self, key):
        recs = self.lane_records(key)
        return [(r['command_id'].split(':')[0], r['payload'].get('action'), r['payload'].get('quantity'))
                for r in recs if r['type'] == 'intent' and r['source'] == 'align']

    def test_follower_long_vs_new_short_on_the_leader(self):
        self.manual(FB, 'Buy', 2)  # left over or by hand: long 2 on AB
        until(lambda: self.net(FB) == 2, 5, 'follower long')
        self.enter('short')          # the leader is not blocked by AB's long
        entry = next(c for c in self.engine.data['commands'].values() if c['kind'] == 'entry')
        self.assertEqual(self.main.journal.get(entry['id'])['outcome'], 'accepted')
        until(lambda: self.net(PRIMARY) == -3 and self.net(FB) == -3 and self.net(FC) == -3, 8, 'all short 3')
        self.assertEqual(self.align_actions('ab'), [('align-flatten', 'align_flatten', 2)])
        self.assertEqual(self.align_actions('ac'), [])
        path = [net for a, _, net in self.fake.position_history if a == FB]
        self.assertEqual(path, [2, 0, -3])  # closed exactly, then the new entry: never a flip
        self.assertEqual(self.firing.lanes['ab'].status['last_alignment']['result'], 'aligned')
        self.hard_stop()
        self.caught_up()
        for acct in (PRIMARY, FC, FB):
            self.assert_flat(acct)

    def test_manual_position_on_the_same_side_is_closed_not_added_to(self):
        self.manual(FC, 'Buy', 1)
        until(lambda: self.net(FC) == 1, 5, 'manual long 1')
        self.enter('long')
        until(lambda: self.net(FC) == 3 and self.net(FB) == 3, 8, 'followers long 3')
        self.assertEqual(self.align_actions('ac'), [('align-flatten', 'align_flatten', 1)])
        self.assertEqual([net for a, _, net in self.fake.position_history if a == FC], [1, 0, 3])

    def test_working_orders_only_are_cancelled_first(self):
        self.manual(FB, 'Buy', 1, 'Limit', 9800.0)
        stray = max(self.account_orders(FB))
        until(lambda: self.state.open_orders(FB, CONTRACT) == [stray], 5, 'stray order visible')
        self.enter('long')
        until(lambda: self.net(FB) == 3, 8, 'follower long 3')
        self.assertEqual(self.align_actions('ab'), [('align-cancel', 'cancel', None)])
        self.assertEqual(self.fake.orders[stray]['ordStatus'], 'Canceled')


if __name__ == '__main__':
    unittest.main(verbosity=2)
