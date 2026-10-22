"""Flatten now (tvbridge.manual): POST /manual/<manual token>/<key>/flatten.

One operator-confirmed, risk-reducing flatten of ONE account: kill switch first (admin note), cancel
that account's working orders and confirm, liquidateposition on the pinned contract only, verify flat,
journal `manual_flatten` (source operator). Never retried; ok false keeps the switch on.

Browser-free: settings/token rules, refusals (logged out, not synced, busy), duplicates, the leader
path as the firing service sees it (`leader_closed_externally`), and daemon.main() HTTP routing with
the page double (route off without a token, 403/404/400, Login A leader and follower, a Login B
followers_only follower). Headless Chrome + fake Tradovate: flat, long 4 with stop + TP1, short,
cancel timeout, liquidation rejected / lost, follower and leader accounts on one login.
"""
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import harness  # noqa: F401  (puts the repository on sys.path)
from harness import Harness

from tvbridge import daemon, follower_native, followers, manual, reconcile
from tvbridge.bridge import Bridge
from tvbridge.executor import Executor, kill_switch_state
from tvbridge.journal import Journal
from tvbridge.logins import daemon_settings
from tvbridge.state import AccountState
from test_fill_capture import (TICKER, Bench, PageBridge, free_port, p_bracket, p_entry, p_flatten, p_tp1,
                               wait_until)

FAST = {'manual_cancel_wait_s': 1.0, 'manual_settle_s': 3.0, 'manual_lock_wait_s': 2.0}
TOKEN_A = 'main-login-daemon-token-0123456789abc'
TOKEN_B = 'followers-only-daemon-token-0123456789'
READ_A = 'main-login-read-token-0123456789abcdef'
MANUAL_A = 'manual-flatten-token-a-0123456789abcdef'
MANUAL_B = 'manual-flatten-token-b-0123456789abcdef'
NOTE_KEYS = {'set_by', 'reason', 'change_id', 'at'}


def journal_records(directory):
    path = Path(directory) / 'commands.jsonl'
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def manual_records(directory):
    return [r for r in journal_records(directory) if r['command_id'].startswith(manual.COMMAND_PREFIX)]


def order_requests(requests):
    return [e for e, _ in requests if e.startswith('order/')]


# ----------------------------------------------------------------------------------------
# Browser-free: settings, token, kill-switch note
# ----------------------------------------------------------------------------------------

class Settings(unittest.TestCase):
    def test_default_token_env_per_login(self):
        a = manual.settings({}, daemon_settings({}))
        self.assertEqual(a['token_env'], 'TVBRIDGE_MANUAL_TOKEN_A')
        b = manual.settings({'login_id': 'b', 'mode': 'followers_only', 'token_env': 'TVBRIDGE_TOKEN_B'},
                            daemon_settings({'login_id': 'b', 'mode': 'followers_only',
                                             'token_env': 'TVBRIDGE_TOKEN_B'}))
        self.assertEqual(b['token_env'], 'TVBRIDGE_MANUAL_TOKEN_B')
        self.assertEqual((a['manual_cancel_wait_s'], a['manual_settle_s'], a['manual_lock_wait_s']), (3.0, 5.0, 10.0))
        self.assertIsNone(a['main_key'])

    def test_invalid_keys(self):
        for cfg, fragment in (({'manual_token_env': 'TVBRIDGE_TOKEN'}, 'manual_token_env must differ'),
                              ({'manual_token_env': 'lower'}, 'manual_token_env must be'),
                              ({'main_key': 'main'}, 'main_key'), ({'main_key': 'Account A'}, 'main_key'),
                              ({'main_key': 'fb', 'followers': [{'key': 'fb'}]}, 'main_key'),
                              ({'manual_settle_s': 0}, 'manual_settle_s'), ({'manual_cancel_wait_s': 31}, 'manual_cancel'),
                              ({'manual_lock_wait_s': True}, 'manual_lock_wait_s')):
            with self.subTest(cfg=cfg):
                with self.assertRaises(ValueError) as cm:
                    manual.settings(cfg, daemon_settings({k: v for k, v in cfg.items() if k == 'followers'}))
                self.assertIn(fragment, str(cm.exception))
        with self.assertRaises(ValueError):
            cfg = {'mode': 'followers_only', 'login_id': 'b', 'token_env': 'TVBRIDGE_TOKEN_B', 'main_key': 'x-1'}
            manual.settings(cfg, daemon_settings(cfg))
        cfg = {'read_token_env': 'TVBRIDGE_READ_TOKEN_A', 'manual_token_env': 'TVBRIDGE_READ_TOKEN_A'}
        with self.assertRaises(ValueError):
            manual.settings(cfg, daemon_settings(cfg))

    def test_token_rules(self):
        self.assertIsNone(manual.load_token({}, 'X', (TOKEN_A,)))
        self.assertIsNone(manual.load_token({'X': ''}, 'X', (TOKEN_A,)))
        self.assertEqual(manual.load_token({'X': MANUAL_A}, 'X', (TOKEN_A, None)), MANUAL_A)
        with self.assertRaises(ValueError):
            manual.load_token({'X': 'short'}, 'X', (TOKEN_A,))
        with self.assertRaises(ValueError):
            manual.load_token({'X': TOKEN_A}, 'X', (TOKEN_A,))
        with self.assertRaises(ValueError):
            manual.load_token({'X': READ_A}, 'X', (TOKEN_A, READ_A))

    def test_kill_switch_note_and_existing_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ENTRIES-DISABLED-x')
            self.assertEqual(manual.set_kill_switch(path, 'operator flatten', 'c-1', 1614147200.5004), 'set')
            note = json.loads(Path(path).read_text())
            self.assertEqual(note, {'set_by': 'tvbridge-admin', 'reason': 'operator flatten', 'change_id': 'c-1',
                                    'at': 1614147200.5})
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), '0o600')
            self.assertEqual(manual.set_kill_switch(path, 'again', 'c-2', 1.0), 'already_set')
            self.assertEqual(json.loads(Path(path).read_text())['change_id'], 'c-1', 'an existing note is kept')
            dangling = os.path.join(tmp, 'dangling')
            os.symlink(os.path.join(tmp, 'missing', 'x'), dangling)
            # the bridge pauses on a dangling link (fails closed), so the switch already counts as on.
            self.assertEqual(manual.set_kill_switch(dangling, 'r', 'c-3', 1.0), 'already_set')
            self.assertEqual(kill_switch_state(dangling), 'kill_switch_on')
            self.assertIsNone(manual.set_kill_switch(None, 'r', 'c-4', 1.0))
            self.assertIsNone(manual.set_kill_switch(os.path.join(tmp, 'no-dir', 'x'), 'r', 'c-5', 1.0))


# ----------------------------------------------------------------------------------------
# Browser-free: flatten() on the page double (test_fill_capture's Exchange)
# ----------------------------------------------------------------------------------------

TID = 'soup:MNQZ2021:1:long'


class FlattenUnits(unittest.TestCase):
    def setUp(self):
        self.b = Bench(self, with_followers=True)
        self.main, self.fb = self.b.main, self.b.fx['account-b']

    def flatten(self, ex, key='main', change_id='c-1', **timings):
        return manual.flatten(ex, key, 'operator flatten now', change_id, dict(FAST, **timings))

    def test_flat_account_only_sets_the_switch(self):
        r = self.flatten(self.main)
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['kill_switch'], r['net_position'], r['cancelled'], r['outcome']), ('set', 0, [], 'accepted'))
        self.assertTrue(r['entries_disabled'])
        self.assertEqual(order_requests(self.b.ex.requests), [], 'nothing sent to Tradovate')
        self.assertEqual(set(json.loads(Path(self.b.cfg['kill_switch_file']).read_text())), NOTE_KEYS)
        rec, = [x for x in manual_records(self.b.cfg['journal_dir']) if x['type'] == 'start']
        self.assertEqual((rec['kind'], rec['source'], rec['trade_id'], rec['mode']), ('manual_flatten', 'operator', None, 'noop'))

    def test_long_with_tp1_is_liquidated_with_the_tp1_left_to_it(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 4))[0], 'accepted')
        out, receipt = self.main.send(p_tp1('t1', TID, 2, 11018.0))
        self.assertEqual(out, 'accepted', receipt)
        tp1 = receipt['broker_order_id']
        before = len(self.b.ex.requests)
        r = self.flatten(self.main)
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['net_position'], r['cancelled'], r['still_working']), (0, [tp1], []))
        sent = [e for e, _ in self.b.ex.requests[before:]]
        self.assertEqual(sent, ['order/liquidateposition'], 'closing-side TP1 left to Exit at Mkt & Cxl')
        self.assertEqual(self.b.ex.requests[-1][1], {'accountId': 7, 'contractId': 2, 'admin': False})
        start, result = manual_records(self.b.cfg['journal_dir'])
        self.assertEqual((start['mode'], start['close_action'], start['quantity']), ('liquidate', 'Sell', 4))
        self.assertEqual((result['outcome'], result['receipt']['ok'], result.get('broker_order_id')),
                         ('accepted', True, None))
        self.assertTrue(result['receipt']['liquidation_order_id'])
        self.assertEqual(self.main.journal.unresolved(), [])
        # The Soup trade's own records are untouched; the follower account too.
        self.assertEqual(self.b.state.net_position(8, 2), 0)
        self.assertFalse(os.path.exists(self.b.cfg['followers'][0]['kill_switch_file']))

    def test_logged_out_and_not_synced_send_nothing_and_keep_the_switch(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 1))[0], 'accepted')
        before = len(self.b.ex.requests)
        with patch.object(self.b.ex, 'status', return_value={'state': 'unauthorized', 'epoch': 'e1',
                                                              'lastRxAgoMs': 0, 'url': 'fake'}):
            r = self.flatten(self.main, change_id='c-out')
        self.assertEqual((r['ok'], r['error'], r['kill_switch']), (False, 'logged_out', 'set'))
        self.b.state.mark_unsynced()
        r = self.flatten(self.main, change_id='c-sync')
        self.assertEqual((r['ok'], r['error'], r['kill_switch']), (False, 'not_synced', 'already_set'))
        self.assertEqual(self.b.ex.requests[before:], [], 'never signs in, never sends')
        self.assertTrue(os.path.exists(self.b.cfg['kill_switch_file']))
        self.assertEqual(manual_records(self.b.cfg['journal_dir']), [], 'refusals stay out of commands.jsonl')
        refusals = [json.loads(x) for x in (Path(self.b.cfg['journal_dir']) / 'refusals.jsonl').read_text().splitlines()]
        self.assertEqual([(x['kind'], x['reason'], x['source']) for x in refusals],
                         [('manual_flatten', 'logged_out', 'operator'), ('manual_flatten', 'not_synced', 'operator')])
        # Once the page is back (fresh sync), a new press with a new change id flattens.
        self.b.ex.sync('e2')
        r = self.flatten(self.main, change_id='c-again')
        self.assertTrue(r['ok'], r)

    def test_duplicate_change_id_sends_nothing_twice(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 2))[0], 'accepted')
        first = self.flatten(self.main, change_id='same')
        self.assertTrue(first['ok'], first)
        n = len(self.b.ex.requests)
        again = self.flatten(self.main, change_id='same')
        self.assertTrue(again['duplicate'])
        self.assertTrue(again['ok'])
        self.assertEqual(len(self.b.ex.requests), n)

    def test_busy_executor_is_not_raced(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 1))[0], 'accepted')
        self.main.lock.acquire()
        try:
            r = self.flatten(self.main, change_id='c-busy', manual_lock_wait_s=0.2)
        finally:
            self.main.lock.release()
        self.assertEqual((r['ok'], r['error'], r['kill_switch']), (False, 'busy', 'set'))
        self.assertEqual(self.b.state.net_position(7, 2), 1)
        # The lock released in time: the flatten waits for it, then runs on fresh state.
        self.main.lock.acquire()
        threading.Timer(0.3, self.main.lock.release).start()
        r = self.flatten(self.main, change_id='c-wait')
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['kill_switch'], 'already_set')

    def test_switch_on_first_refuses_a_racing_entry(self):
        self.main.lock.acquire()
        done = {}
        t = threading.Thread(target=lambda: done.update(r=self.flatten(self.main, change_id='c-race')))
        t.start()
        self.assertTrue(wait_until(lambda: os.path.exists(self.b.cfg['kill_switch_file']), 2))
        self.main.lock.release()
        t.join(10)
        out, receipt = self.main.send(p_entry('e-race', 'soup:MNQZ2021:9:long', 1))
        self.assertEqual((out, receipt['reason']), ('rejected', 'entries disabled by kill switch'))
        self.assertTrue(done['r']['ok'])

    def test_no_kill_switch_configured_means_nothing_is_done(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 1))[0], 'accepted')
        self.main.cfg = dict(self.main.cfg, kill_switch_file=None)
        # no switch configured fails closed: new entries are refused...
        out, receipt = self.main.send(p_entry('e2', 'soup:MNQZ2021:8:long', 1))
        self.assertEqual((out, receipt['reason']), ('rejected', 'entries disabled by kill switch: kill_switch_not_configured'))
        n = len(self.b.ex.requests)
        r = self.flatten(self.main, change_id='c-none')
        self.assertEqual((r['ok'], r['error']), (False, 'kill_switch_unavailable'))
        self.assertEqual(len(self.b.ex.requests), n)

    def test_follower_only_its_own_account_and_switch(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 2))[0], 'accepted')
        self.assertEqual(self.fb.send(p_entry('e1', TID, 1))[0], 'accepted')
        r = self.flatten(self.fb, key='account-b')
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.b.state.net_position(8, 2), 0)
        self.assertEqual(self.b.state.net_position(7, 2), 2, 'the leader is never touched')
        self.assertTrue(os.path.exists(self.b.cfg['followers'][0]['kill_switch_file']))
        self.assertFalse(os.path.exists(self.b.cfg['kill_switch_file']))
        self.assertEqual([b['accountId'] for e, b in self.b.ex.requests if e == 'order/liquidateposition'], [8])
        self.assertEqual(len(manual_records(self.b.cfg['followers'][0]['journal_dir'])), 2)
        self.assertEqual(manual_records(self.b.cfg['journal_dir']), [])

    def test_only_opening_side_orders_are_cancelled_before_liquidation(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 2))[0], 'accepted')
        x = self.b.ex
        ids = {name: x.order(7, action, 1, otype, price=price, stop=stop) for name, action, otype, price, stop in (
            ('tp', 'Sell', 'Limit', 11018.0, None), ('stop', 'Sell', 'Stop', None, 10987.25),
            ('stop_limit', 'Sell', 'StopLimit', 10986.25, 10987.25), ('unknown', 'Sell', None, None, None),
            ('pending', 'Sell', 'Stop', None, 10975.0), ('add_limit', 'Buy', 'Limit', 10993.5, None),
            ('add_stop', 'Buy', 'Stop', None, 11031.0))}
        x.set_status(ids['pending'], 'PendingCancel')
        working = sorted(ids.values())
        self.assertEqual(sorted(manual._opening(self.main, working, 2)), sorted([ids['add_limit'], ids['add_stop']]))
        self.assertEqual(sorted(manual._opening(self.main, working, -2)),
                         sorted(ids[k] for k in ('tp', 'stop', 'stop_limit', 'unknown', 'pending')))
        # protective_stop_working: Working closing-side stop types and unknown types, never PendingCancel.
        self.assertEqual(sorted(manual._view(self.main)['stops']),
                         sorted([ids['stop'], ids['stop_limit'], ids['unknown']]))
        n = len(x.requests)
        r = self.flatten(self.main, change_id='c-open')
        self.assertTrue(r['ok'], r)
        sent = x.requests[n:]
        self.assertEqual(sent[:3], [('order/cancelorder', {'orderId': ids['add_limit']}),
                                    ('order/cancelorder', {'orderId': ids['add_stop']}),
                                    ('order/liquidateposition', {'accountId': 7, 'contractId': 2, 'admin': False})])
        # The page double's liquidation leaves the PendingCancel order listed: the clean-up cancels it once flat.
        self.assertEqual(sent[3:], [('order/cancelorder', {'orderId': ids['pending']})])

    def test_unseen_orders_never_block_and_are_reported(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 1))[0], 'accepted')
        j = self.main.journal
        j.start('ghost', trade_id='soup:MNQZ2021:77:long', kind='tp1_limit', side='long', quantity=1, tag=None,
                at=self.main.clock_ms())
        j.finish('ghost', 'accepted', {}, '7999999')
        self.assertEqual(self.main._unseen_accepted_orders(), ['7999999'])
        n = len(self.b.ex.requests)
        r = self.flatten(self.main, change_id='c-unseen')
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['net_position'], r['unseen_orders']), (0, ['7999999']))
        self.assertEqual([e for e, _ in self.b.ex.requests[n:]], ['order/liquidateposition'],
                         'no cancel for an order Tradovate does not list')

    def test_in_progress_change_id(self):
        self.main.journal.start('manual-flatten:c-run', trade_id=None, kind='manual_flatten', side=None,
                                quantity=None, tag=None, at=self.main.clock_ms(), source='operator', mode='noop')
        r = self.flatten(self.main, change_id='c-run')
        self.assertEqual((r['ok'], r['error'], r['duplicate']), (False, 'in_progress', True))
        self.assertFalse(os.path.exists(self.b.cfg['kill_switch_file']), 'the running call already did that')
        self.assertEqual(order_requests(self.b.ex.requests), [])

    def stall_cancels(self):
        """cancelorder is acknowledged but Tradovate never shows the cancel (the order keeps working)."""
        real = self.b.ex.request

        def request(endpoint, body=None, **kw):
            if endpoint == 'order/cancelorder':
                self.b.ex.requests.append((endpoint, body))
                return {'ok': True, 'sent': True, 's': 200, 'd': {}}
            return real(endpoint, body, **kw)
        return patch.object(self.b.ex, 'request', side_effect=request)

    def test_executor_lock_is_free_during_the_waits(self):
        """The publisher's maintain/auto-reconcile path (and Soup's /order) is never held up for long."""
        self.assertEqual(self.main.send(p_entry('e1', TID, 2))[0], 'accepted')
        self.b.ex.order(7, 'Buy', 1, 'Limit', price=10993.5)  # adds exposure: cancelled before the liquidation
        done, worst, samples, other = threading.Event(), [0.0], [0], {}

        def publisher():
            while not done.is_set():
                t0 = time.monotonic()
                with self.main.lock:  # as Publisher.run does for maintain_brackets / auto_reconcile
                    self.main.maintain_brackets()
                    self.main.auto_reconcile()
                worst[0] = max(worst[0], time.monotonic() - t0)
                samples[0] += 1
                time.sleep(0.05)
        th = threading.Thread(target=publisher)
        th.start()
        try:
            with self.stall_cancels():
                t0 = time.monotonic()
                threading.Timer(0.5, lambda: other.update(r=self.flatten(self.main, change_id='c-other'))).start()
                r = self.flatten(self.main, change_id='c-slow', manual_cancel_wait_s=3.0)
                took = time.monotonic() - t0
        finally:
            done.set()
            th.join(5)
        self.assertEqual((r['ok'], r['error'], r['net_position']), (False, 'cancel_not_confirmed', 2))
        self.assertGreaterEqual(took, 2.9)
        self.assertGreater(samples[0], 20)
        self.assertLess(worst[0], 1.0, 'the executor lock was held through a wait')
        self.assertEqual((other['r']['ok'], other['r']['error']), (False, 'busy'), 'one manual flatten per account')

    def test_total_wall_time_is_bounded(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 2))[0], 'accepted')
        self.b.ex.order(7, 'Buy', 1, 'Limit', price=10993.5)
        with self.stall_cancels():
            t0 = time.monotonic()
            r = self.flatten(self.main, change_id='c-budget', manual_cancel_wait_s=10.0, manual_max_s=1.0)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(r['error'], 'cancel_not_confirmed')

    def delayed_liquidation(self, delay):
        """liquidateposition replies at once with an order id; the exit fills `delay` s later and does NOT
        cancel working orders (Tradovate's own cancel is documentation-based). Returns the call list."""
        real, calls = self.b.ex.request, []

        def close():
            net = self.b.ex.net(7)
            oid = self.b.ex.order(7, 'Sell' if net > 0 else 'Buy', abs(net), 'Market')
            self.b.ex.fill_market(oid)

        def request(endpoint, body=None, **kw):
            if endpoint == 'order/liquidateposition':
                calls.append(body)
                self.b.ex.requests.append((endpoint, body))
                threading.Timer(delay, close).start()
                return {'ok': True, 'sent': True, 's': 200, 'd': {'orderId': 7999000 + len(calls)}}
            return real(endpoint, body, **kw)
        return calls, patch.object(self.b.ex, 'request', side_effect=request)

    def test_no_second_liquidation_from_a_racing_soup_flatten_all(self):
        b = Bench(self, with_followers=True, flatten_settle_s=2.0)
        self.b, self.main = b, b.main
        self.assertEqual(self.main.send(p_entry('e1', TID, 4))[0], 'accepted')
        calls, patched = self.delayed_liquidation(0.4)
        soup = {}
        with patched:
            def race():
                wait_until(lambda: calls, 5)
                soup['r'] = self.main.send(p_flatten('f-race', TID))  # Soup's flatten_all, just after ours
            th = threading.Thread(target=race)
            th.start()
            r = self.flatten(self.main, change_id='c-race2')
            th.join(10)
        self.assertTrue(r['ok'], r)
        self.assertEqual(len(calls), 1, 'one liquidation: the lock is held until flat is seen')
        self.assertEqual(soup['r'][0], 'accepted')
        self.assertEqual(soup['r'][1].get('bridge'), 'noop')

    def test_stop_on_an_account_that_goes_flat_late_is_cancelled(self):
        self.assertEqual(self.main.send(p_entry('e1', TID, 2))[0], 'accepted')
        stop = self.b.ex.order(7, 'Sell', 2, 'Stop', stop=10987.25)
        calls, patched = self.delayed_liquidation(0.8)  # flat only after flatten_settle_s (0.3 s here)
        with patched:
            r = self.flatten(self.main, change_id='c-late')
        self.assertTrue(r['ok'], r)
        self.assertEqual([b['orderId'] for e, b in self.b.ex.requests if e == 'order/cancelorder'], [stop])
        self.assertEqual(r['cancelled'], [str(stop)])
        self.assertEqual(self.b.ex.orders[stop]['ordStatus'], 'Canceled')

    def test_decision_uses_the_position_read_under_the_lock(self):
        """Flat while waiting for the lock, then a stop triggers into a new position: the stop of that
        position is not cancelled as if flat, and the position is liquidated."""
        stop = self.b.ex.order(7, 'Sell', 1, 'Stop', stop=10987.25)  # an orphan stop on a flat account
        self.main.lock.acquire()
        result = {}
        th = threading.Thread(target=lambda: result.update(r=self.flatten(self.main, change_id='c-stale')))
        th.start()
        time.sleep(0.3)
        self.b.ex.fill(stop, [(None, 10987.25)])  # it triggers: short 1, no other order
        protect = self.b.ex.order(7, 'Buy', 1, 'Stop', stop=11031.0)  # the new position's stop
        self.main.lock.release()
        th.join(15)
        r = result['r']
        self.assertTrue(r['ok'], r)
        liq = [b for e, b in self.b.ex.requests if e == 'order/liquidateposition']
        self.assertEqual(len(liq), 1, 'the short was liquidated')
        self.assertNotIn(('order/cancelorder', {'orderId': protect}), self.b.ex.requests[:self.b.ex.requests.index(
            ('order/liquidateposition', liq[0]))], "its stop was not cancelled before the liquidation")

    def test_journal_result_failure_never_answers_in_progress(self):
        with patch.object(Journal, 'finish', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.flatten(self.main, change_id='c-disk')
        r = self.flatten(self.main, change_id='c-disk')
        self.assertEqual((r['duplicate'], r['outcome'], r['ok'], r['error']), (True, 'unknown', False, 'journal_write_failed'))
        self.assertNotEqual(r['error'], 'in_progress')

    def test_unknown_manual_flatten_never_takes_a_later_liquidation(self):
        """Reconcile attribution: an unknown manual_flatten resolves only as flat and idle, never with an
        order id, even when a liquidation order of the same shape exists after its baseline."""
        self.assertEqual(self.main.send(p_entry('e1', TID, 1))[0], 'accepted')
        # A liquidation outside the bridge journal (by hand): flatten_all's matcher would take it.
        self.b.ex.request('order/liquidateposition', {'accountId': 7, 'contractId': 2, 'admin': False})
        self.assertIsNotNone(self.main._find_liquidation({'baseline_order_id': 0, 'close_action': 'Sell'}))
        self.main.journal.start('manual-flatten:old', trade_id=None, kind='manual_flatten', side=None, quantity=1,
                                tag=None, at=self.main.clock_ms() - 60_000, source='operator', mode='liquidate',
                                close_action='Sell', baseline_order_id=0)
        self.main.journal = Journal(self.b.cfg['journal_dir'])
        self.assertEqual(self.main.auto_reconcile(), ['manual-flatten:old'])
        self.assertIsNone(self.main.journal.get('manual-flatten:old')['broker_order_id'])

    def test_unknown_manual_flatten_reconciles(self):
        """A start record left without a result (daemon died mid-flatten) resolves like flatten_all."""
        j = self.main.journal
        j.start('manual-flatten:crash', trade_id=None, kind='manual_flatten', side=None, quantity=None, tag=None,
                at=self.main.clock_ms() - 60_000, source='operator', mode='cancel')
        j.start('manual-flatten:crash2', trade_id=None, kind='manual_flatten', side=None, quantity=None, tag=None,
                at=self.main.clock_ms(), source='operator', mode='cancel')
        self.main.journal = Journal(self.b.cfg['journal_dir'])  # the restart: both starts are now unknown
        self.assertEqual(len(self.main.journal.unresolved()), 2)
        self.assertEqual(self.main.auto_reconcile(), ['manual-flatten:crash'], 'flat and idle long enough')
        out, receipt = self.main.send(p_entry('e1', TID, 1))
        self.assertIn('unknown outcome', receipt['reason'])
        with patch('builtins.print'):
            reconcile.main(['--journal-dir', self.b.cfg['journal_dir'], '--command', 'manual-flatten:crash2',
                            '--settled', '--note', 'flat'])
        self.assertEqual(Journal(self.b.cfg['journal_dir']).unresolved(), [])


class LeaderIsAnOutsideClose(unittest.TestCase):
    """The leader's Flatten now is not one of Soup's commands: Soup's snapshot shows its own orders
    cancelled, no exit fill and position 0 (Soup's external close), and the firing service's
    LeaderFlatWatch reports `leader_closed_externally` for follower lanes (unchanged code)."""

    def test_leader_flatten_reads_as_closed_outside_the_bridge(self):
        b = Bench(self, with_followers=True)
        out, receipt = b.main.send(p_bracket('c-entry', TID, qty=5, tp_qty=2))
        self.assertEqual(out, 'accepted', receipt)
        trade = {'id': TID, 'phase': 'open'}
        self.assertFalse(follower_native.leader_flat_candidate(trade, followers.summary(b.fx, b.main, TID)))
        r = manual.flatten(b.main, 'account-a', 'operator flatten now', 'c-lead', FAST)
        self.assertTrue(r['ok'], r)
        self.assertEqual(len(r['cancelled']), 2, 'stop and TP1 cancelled')
        report = followers.summary(b.fx, b.main, TID)
        facts = report['main']['leader']
        self.assertEqual((facts['entry_filled'], facts['exit_filled'], facts['unresolved'], facts['net_position']),
                         (5, 0, 0, 0))
        self.assertNotIn('own_liquidation', facts)
        self.assertTrue(follower_native.leader_flat_candidate(trade, report))
        watch = follower_native.LeaderFlatWatch()
        self.assertIsNone(watch.observe(trade, report, 1500))
        fact = watch.observe(trade, followers.summary(b.fx, b.main, TID), 4000)
        self.assertEqual((fact['trade_id'], fact['external']), (TID, 5))
        snap, why = b.main.snapshot(TID)
        self.assertIsNone(why)
        self.assertEqual(snap['position_qty'], 0)
        self.assertEqual({o['role']: (o['status'], o['filled_qty']) for o in snap['orders']},
                         {'entry': ('filled', 5), 'stop': ('cancelled', 0), 'tp1': ('cancelled', 0)})
        self.assertNotIn(r['receipt']['liquidation_order_id'], [o['broker_order_id'] for o in snap['orders']])

    def test_soup_flatten_all_stays_the_bridges_own_close(self):
        """Contrast (unchanged): Soup's own flatten_all is never an outside close."""
        b = Bench(self, with_followers=True)
        self.assertEqual(b.main.send(p_bracket('c-entry', TID, qty=5, tp_qty=2))[0], 'accepted')
        self.assertEqual(b.main.send(p_flatten('c-flat', TID))[0], 'accepted')
        self.assertFalse(follower_native.leader_flat_candidate({'id': TID, 'phase': 'open'},
                                                               followers.summary(b.fx, b.main, TID)))


# ----------------------------------------------------------------------------------------
# Browser-free: HTTP routing through daemon.main() with the page double
# ----------------------------------------------------------------------------------------

class DaemonRoutes(unittest.TestCase):
    def main_cfg(self, tmp, port):
        return {'account_name': 'MAIN-ACCT', 'account_id': 7, 'soup_account_id': 'Account A', 'ticker': TICKER,
                'tick_size': .25, 'max_contracts': 10, 'journal_dir': str(tmp / 'journal'),
                'kill_switch_file': str(tmp / 'ENTRIES-DISABLED'), 'devtools_port': 1, 'page_url_contains': 'x',
                'trade_url_pattern': 'x', 'publish_mode': 'evidence', 'listen_port': port, 'shutdown_drain_s': 2,
                'bracket_orders_enabled': True, 'flatten_all_enabled': True, 'tag_wait_s': 0.2,
                'entry_settle_s': 0.5, 'terminal_settle_ms': 0, 'read_token_env': 'TVBRIDGE_READ_TOKEN_A',
                'main_key': 'account-a', **FAST,
                'peer_logins': [{'id': 'b', 'url': 'http://127.0.0.1:%d' % free_port(), 'token_env': 'TVBRIDGE_READ_TOKEN_B'}],
                'followers': [{'key': 'account-b', 'account_name': 'F-B', 'account_id': 8,
                               'journal_dir': str(tmp / 'journal-account-b'),
                               'kill_switch_file': str(tmp / 'ENTRIES-DISABLED-account-b'), 'max_contracts': 10,
                               'prop_profile': 'cross-account-window', 'native_exits_enabled': True}]}

    def login_b_cfg(self, tmp, port):
        (tmp / 'login-a').mkdir()
        return {'login_id': 'b', 'login_label': 'Login B', 'mode': 'followers_only', 'token_env': 'TVBRIDGE_TOKEN_B',
                'ticker': TICKER, 'tick_size': .25, 'devtools_port': 1, 'page_url_contains': 'x',
                'trade_url_pattern': 'x', 'listen_port': port, 'shutdown_drain_s': 2, 'tag_wait_s': 0.2,
                'entry_settle_s': 0.5, 'kill_switch_file': str(tmp / 'ENTRIES-DISABLED-login-b'),
                'master_kill_switch_file': str(tmp / 'login-a' / 'ENTRIES-DISABLED'), **FAST,
                'followers': [{'key': 'account-f', 'account_name': 'F-B', 'account_id': 8,
                               'journal_dir': str(tmp / 'journal-account-f'),
                               'kill_switch_file': str(tmp / 'ENTRIES-DISABLED-account-f'), 'max_contracts': 10,
                               'prop_profile': 'cross-account-window', 'native_exits_enabled': True}]}

    def run_daemon(self, make_cfg, env, actions):
        PageBridge.instances = []
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        port = free_port()
        cfg = make_cfg(tmp, port)
        (tmp / 'config.json').write_text(json.dumps(cfg))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        results = {'tmp': tmp, 'cfg': cfg}

        def call(method, path, payload=None, raw=None):
            data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
            req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=data, method=method,
                                         headers={'Content-Type': 'application/json'})
            try:
                with opener.open(req, timeout=20) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())

        def drive(before):
            try:
                assert wait_until(lambda: signal.getsignal(signal.SIGTERM) is not before)
                actions(call, results, tmp)
            except BaseException:  # noqa: BLE001 - reported by the test below
                results['error'] = traceback.format_exc()
            finally:
                os.kill(os.getpid(), signal.SIGTERM)

        before = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        threading.Thread(target=drive, args=(before,), daemon=True).start()
        clean = {k: v for k, v in os.environ.items() if not k.startswith('TVBRIDGE_')}
        with patch.object(daemon, 'Bridge', PageBridge), \
                patch.object(sys, 'argv', ['daemon', '--config', str(tmp / 'config.json')]), \
                patch.dict(os.environ, dict(clean, **env), clear=True), patch('logging.basicConfig'):
            with self.assertLogs('tvbridge', level='INFO') as logs:
                self.assertIsNone(daemon.main())
        results['logs'] = logs.output
        if 'error' in results:
            self.fail(results['error'])
        return results

    BODY = {'reason': 'operator flatten now', 'change_id': 'c-demo-1'}

    def test_route_does_not_exist_without_a_manual_token(self):
        def actions(call, results, tmp):
            results['manual'] = call('POST', '/manual/%s/main/flatten' % MANUAL_A, self.BODY)
            results['order_token'] = call('POST', '/manual/%s/main/flatten' % TOKEN_A, self.BODY)
        r = self.run_daemon(self.main_cfg, {'TVBRIDGE_TOKEN': TOKEN_A}, actions)
        self.assertEqual(r['manual'], (404, {'error': 'not found'}))
        self.assertEqual(r['order_token'], (404, {'error': 'not found'}))
        self.assertFalse((r['tmp'] / 'ENTRIES-DISABLED').exists())
        self.assertFalse(any('manual flatten route on' in line for line in r['logs']))
        self.assertEqual(PageBridge.instances[0].ex.requests, [])

    def test_login_a_tokens_keys_body_and_leader_flatten(self):
        def actions(call, results, tmp):
            m = '/manual/%s/' % MANUAL_A
            results['bad'] = call('POST', '/manual/%s/main/flatten' % ('x' * len(MANUAL_A)), self.BODY)
            results['order_token'] = call('POST', '/manual/%s/main/flatten' % TOKEN_A, self.BODY)
            results['read_token'] = call('POST', '/manual/%s/main/flatten' % READ_A, self.BODY)
            results['unicode'] = call('POST', '/manual/%%C3%%A9%s/main/flatten' % MANUAL_A[2:], self.BODY)
            results['unknown'] = call('POST', m + 'sim-99/flatten', self.BODY)
            results['route'] = call('POST', m + 'main/cancel', self.BODY)
            results['short'] = call('POST', m + 'main', self.BODY)
            for name, body in (('no_reason', {'change_id': 'c'}), ('extra', dict(self.BODY, quantity=3)),
                               ('bad_id', dict(self.BODY, change_id='bad id')), ('long', dict(self.BODY, reason='r' * 201)),
                               ('blank', dict(self.BODY, reason='  '))):
                results[name] = call('POST', m + 'main/flatten', body)
            results['not_json'] = call('POST', m + 'main/flatten', raw=b'{nope')
            # The manual token opens nothing else.
            results['m_order'] = call('POST', '/order/' + MANUAL_A, p_entry('x', TID, 1))
            results['m_health'] = call('GET', '/health/' + MANUAL_A)
            results['m_login'] = call('GET', '/login/' + MANUAL_A)
            results['m_followers'] = call('GET', '/followers/' + MANUAL_A)
            results['m_kill'] = call('POST', '/follower/%s/account-b/kill' % MANUAL_A, {'reason': 'x'})
            results['m_get'] = call('GET', m + 'main/flatten')
            results['ks_after_refusals'] = (tmp / 'ENTRIES-DISABLED').exists()
            # Leader: long 4 with a resting TP1, then Flatten now through its firing-order key.
            results['entry'] = call('POST', '/order/' + TOKEN_A, p_entry('e1', TID, 4))
            results['tp1'] = call('POST', '/order/' + TOKEN_A, p_tp1('t1', TID, 2, 11018.0))
            results['fb'] = call('POST', '/follower/%s/account-b/order' % TOKEN_A, p_entry('e1', TID, 2))
            results['flatten'] = call('POST', m + 'account-a/flatten', self.BODY)
            results['again'] = call('POST', m + 'main/flatten', self.BODY)
            results['entry_after'] = call('POST', '/order/' + TOKEN_A, p_entry('e2', 'soup:MNQZ2021:2:long', 1))
            results['state'] = call('GET', '/followers/' + TOKEN_A)

        env = {'TVBRIDGE_TOKEN': TOKEN_A, 'TVBRIDGE_READ_TOKEN_A': READ_A, 'TVBRIDGE_MANUAL_TOKEN_A': MANUAL_A,
               'TVBRIDGE_READ_TOKEN_B': 'peer-read-token-b-0123456789abcdef'}
        # Login B is named as a peer but never runs here; the cross-login check (fails closed)
        # is tested in test_peer_hedge, so this test of the flatten route treats B as clear.
        from tvbridge.peers import PeerHedge
        with patch.object(PeerHedge, 'conflict', return_value=None):
            r = self.run_daemon(self.main_cfg, env, actions)
        for name in ('bad', 'order_token', 'read_token', 'unicode'):
            self.assertEqual(r[name], (403, {'error': 'forbidden'}), name)
        self.assertEqual(r['unknown'], (404, {'error': 'unknown_account'}))
        self.assertEqual(r['route'][0], 404)
        self.assertEqual(r['short'][0], 404)
        for name in ('no_reason', 'extra', 'bad_id', 'long', 'blank', 'not_json'):
            self.assertEqual(r[name], (400, {'error': 'invalid request'}), name)
        for name in ('m_order', 'm_health', 'm_login', 'm_followers', 'm_kill', 'm_get'):
            self.assertEqual(r[name][0], 404, name)
        self.assertFalse(r['ks_after_refusals'], 'no refused request touches the switch')
        self.assertEqual(r['tp1'][1]['outcome'], 'accepted')
        status, flat = r['flatten']
        self.assertEqual(status, 200)
        self.assertTrue(flat['ok'], flat)
        self.assertEqual((flat['account'], flat['account_id'], flat['net_position'], flat['kill_switch']),
                         ('account-a', 7, 0, 'set'))
        self.assertEqual(flat['cancelled'], [r['tp1'][1]['receipt']['broker_order_id']])
        self.assertEqual(flat['receipt']['source'], 'operator')
        self.assertEqual(flat['also_blocks'], ['account-b', 'login:b'], 'pausing the leader pauses the webhook')
        self.assertEqual((flat['protective_stop_working'], flat['protected'], flat['unseen_orders']), (False, True, []))
        self.assertTrue(r['again'][1]['duplicate'], 'same change id via the "main" alias: same account, no resend')
        self.assertEqual(r['entry_after'][1]['receipt']['reason'], 'entries disabled by kill switch')
        note = json.loads((r['tmp'] / 'ENTRIES-DISABLED').read_text())
        self.assertEqual(note, {'set_by': 'tvbridge-admin', 'reason': 'operator flatten now',
                                'change_id': 'c-demo-1', 'at': note['at']})
        rows = {row['key']: row for row in r['state'][1]['followers']}
        self.assertEqual(rows['account-b']['net_position'], 2, 'one account per call: the follower is untouched')
        ex = PageBridge.instances[0].ex
        self.assertEqual([b['accountId'] for e, b in ex.requests if e == 'order/liquidateposition'], [7])
        self.assertTrue(any('manual flatten route on (TVBRIDGE_MANUAL_TOKEN_A) for: account-a, account-b, main'
                            in line for line in r['logs']), r['logs'])
        start, result = manual_records(r['tmp'] / 'journal')
        self.assertEqual((start['kind'], start['account_key'], result['outcome']), ('manual_flatten', 'account-a', 'accepted'))

    def test_login_b_followers_only_follower(self):
        def actions(call, results, tmp):
            m = '/manual/%s/' % MANUAL_B
            results['entry'] = call('POST', '/follower/%s/account-f/order' % TOKEN_B, p_entry('e1', TID, 3))
            results['main'] = call('POST', m + 'main/flatten', self.BODY)
            results['login_a_token'] = call('POST', '/manual/%s/account-f/flatten' % MANUAL_A, self.BODY)
            page = PageBridge.instances[0].ex
            with patch.object(page, 'status', return_value={'state': 'unauthorized', 'epoch': page.epoch,
                                                            'lastRxAgoMs': 0, 'url': 'fake'}):
                results['logged_out'] = call('POST', m + 'account-f/flatten', dict(self.BODY, change_id='c-out'))
            n = len(page.requests)
            with patch('tvbridge.journal.Journal.start', side_effect=OSError('disk full')):
                results['disk'] = call('POST', m + 'account-f/flatten', dict(self.BODY, change_id='c-disk'))
            results['disk_sent'] = page.requests[n:]
            results['flatten'] = call('POST', m + 'account-f/flatten', self.BODY)

        env = {'TVBRIDGE_TOKEN_B': TOKEN_B, 'TVBRIDGE_MANUAL_TOKEN_B': MANUAL_B, 'TVBRIDGE_MANUAL_TOKEN_A': MANUAL_A}
        r = self.run_daemon(self.login_b_cfg, env, actions)
        self.assertEqual(r['entry'][1]['outcome'], 'accepted')
        self.assertEqual(r['main'], (404, {'error': 'unknown_account'}), 'no main account on a followers_only login')
        self.assertEqual(r['login_a_token'][0], 403, "another login's manual token is not this login's")
        out = r['logged_out'][1]
        self.assertEqual((out['ok'], out['error'], out['kill_switch'], out['net_position']), (False, 'logged_out', 'set', 3))
        self.assertEqual(r['disk'], (500, {'error': 'internal_error', 'type': 'OSError', 'account': 'account-f',
                                           'entries_disabled': True}))
        self.assertEqual(r['disk_sent'], [], 'no journal start, nothing sent')
        flat = r['flatten'][1]
        self.assertTrue(flat['ok'], flat)
        self.assertEqual((flat['account'], flat['net_position'], flat['kill_switch']), ('account-f', 0, 'already_set'))
        self.assertNotIn('also_blocks', flat, 'a follower switch blocks only that follower')
        self.assertTrue((r['tmp'] / 'ENTRIES-DISABLED-account-f').exists())
        self.assertEqual(json.loads((r['tmp'] / 'ENTRIES-DISABLED-account-f').read_text())['change_id'], 'c-out')
        self.assertFalse((r['tmp'] / 'login-a' / 'ENTRIES-DISABLED').exists(), 'the master switch is never written')
        self.assertFalse((r['tmp'] / 'ENTRIES-DISABLED-login-b').exists())
        self.assertTrue(any('manual flatten route on (TVBRIDGE_MANUAL_TOKEN_B) for: account-f' in line
                            for line in r['logs']), r['logs'])


class DaemonStartup(unittest.TestCase):
    def run_main(self, cfg, env):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'config.json'
            p.write_text(json.dumps(dict({'max_contracts': 10}, **cfg)))
            with patch.object(sys, 'argv', ['daemon', '--config', str(p)]), patch.dict(os.environ, env, clear=True), \
                    patch.object(daemon, 'Bridge', side_effect=AssertionError('browser touched')):
                with self.assertRaises(SystemExit) as cm:
                    daemon.main()
        return str(cm.exception)

    def test_bad_manual_token_or_key_stops_before_the_browser(self):
        self.assertIn('TVBRIDGE_MANUAL_TOKEN_A must hold at least 24',
                      self.run_main({}, {'TVBRIDGE_TOKEN': TOKEN_A, 'TVBRIDGE_MANUAL_TOKEN_A': 'short'}))
        self.assertIn('must differ from the order and read tokens',
                      self.run_main({}, {'TVBRIDGE_TOKEN': TOKEN_A, 'TVBRIDGE_MANUAL_TOKEN_A': TOKEN_A}))
        self.assertIn('main_key', self.run_main({'main_key': 'main'}, {'TVBRIDGE_TOKEN': TOKEN_A}))
        follower_main = {'followers': [{'key': 'main'}]}
        self.assertIn('follower keyed "main" collides', self.run_main(
            follower_main, {'TVBRIDGE_TOKEN': TOKEN_A, 'TVBRIDGE_MANUAL_TOKEN_A': MANUAL_A}))
        self.assertIn('main_key must be', self.run_main(
            {'main_key': 'fb', 'followers': [{'key': 'fb'}]}, {'TVBRIDGE_TOKEN': TOKEN_A, 'TVBRIDGE_MANUAL_TOKEN_A': MANUAL_A}))
        manual.check_aliases({'followers': [{'key': 'fb'}]})
        cfg = {'mode': 'followers_only', 'login_id': 'b', 'token_env': 'TVBRIDGE_TOKEN_B'}
        self.assertIn('TVBRIDGE_MANUAL_TOKEN_B must hold', self.run_main(
            cfg, {'TVBRIDGE_TOKEN_B': TOKEN_B, 'TVBRIDGE_MANUAL_TOKEN_B': 'x'}))


# ----------------------------------------------------------------------------------------
# Headless Chrome + fake Tradovate (the VM harness)
# ----------------------------------------------------------------------------------------

ACCOUNT, OTHER, CONTRACT = 101, 102, 555


def bracket(cid, tid, qty, stop, tp=None, tp_qty=None, ref=10000.0):
    extras = {'soup_command_id': cid, 'soup_trade_id': tid, 'bridge_kind': 'bracket_entry',
              'reference_price': ref, 'stop_price': stop}
    if tp is not None:
        extras['tp1_price'] = tp
    if tp_qty is not None:
        extras['tp1_quantity'] = tp_qty
    action = 'buy' if tid.endswith(':long') else 'sell'
    return {'ticker': TICKER, 'action': action, 'orderType': 'market', 'quantity': qty, 'extras': extras}


def entry(cid, tid, qty):
    return {'ticker': TICKER, 'action': 'buy' if tid.endswith(':long') else 'sell', 'orderType': 'market',
            'quantity': qty, 'extras': {'soup_command_id': cid, 'soup_trade_id': tid}}


def tp1(cid, tid, qty, price):
    return {'ticker': TICKER, 'action': 'exit', 'orderType': 'limit', 'limitPrice': price, 'quantity': qty,
            'cancel': False, 'extras': {'soup_command_id': cid, 'soup_trade_id': tid}}


class HarnessFlatten(unittest.TestCase):
    """Real page script, real Bridge/Executor/FollowerExecutor, fake Tradovate on loopback."""

    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.fake = cls.h.fake
        cls.cfg = cls.h.config(bracket_orders_enabled=True, flatten_all_enabled=True, terminal_settle_ms=0,
                               followers=[{'key': 'account-b', 'account_name': 'OTHER', 'account_id': OTHER,
                                           'journal_dir': str(cls.h.tmp / 'journal-account-b'),
                                           'kill_switch_file': str(cls.h.tmp / 'ENTRIES-DISABLED-account-b'),
                                           'max_contracts': 10, 'prop_profile': 'cross-account-window',
                                           'native_exits_enabled': True}])
        cls.state = AccountState()
        cls.bridge = Bridge(cls.state, devtools_port=cls.h.devtools_port, page_url_contains='/trader/',
                            trade_url_pattern=cls.cfg['trade_url_pattern'])
        cls.bridge.attach(reload=True)
        cls.bridge.wait_ready(timeout=20)
        cls.ex = Executor(cls.bridge, cls.state, Journal(cls.cfg['journal_dir']), cls.cfg)
        cls.ex.resolve()
        cls.fx = followers.build(cls.bridge, cls.state, cls.cfg, cls.ex)
        cls.fb = cls.fx['account-b']
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.bridge.detach()
        cls.h.close()

    def setUp(self):
        type(self).n += 1
        self.addCleanup(self.cleanup)

    def tid(self, side='long'):
        return 'soup:MNQZ2021:%d:%s' % (1000 + self.n, side)

    def cleanup(self):
        """Test-only: settle unknowns, flatten both accounts on the fake exchange, remove kill switches."""
        for ex in (self.ex, self.fb):
            for c in ex.journal.unresolved():
                ex.journal.reconcile(c['command_id'], 'rejected', note='test cleanup')
        self.fake.swallow_endpoints.clear()
        self.fake.reject_cancel_next = self.fake.reject_liquidate_next = False
        self.fake.liquidate_cancels_orders = True
        self.fake.oco_cancel_links = False
        with self.fake.lock:
            for oid in self.fake.working_orders():
                self.fake._cancel(oid)
            for (acct, cid), pos in list(self.fake.positions.items()):
                if pos['netPos']:
                    self.fake._place({'accountId': acct, 'symbol': 'MNQZ1', 'orderQty': abs(pos['netPos']),
                                      'orderType': 'Market', 'action': 'Sell' if pos['netPos'] > 0 else 'Buy'},
                                     lambda d, s=200: None)
            self.fake.price = 10000.0
        self.assertTrue(wait_until(lambda: not self.state.open_orders(ACCOUNT, CONTRACT)
                                   and not self.state.open_orders(OTHER, CONTRACT)
                                   and all(v == 0 for v in self.state.net_positions().values()), 10))
        for path in (self.cfg['kill_switch_file'], self.cfg['followers'][0]['kill_switch_file']):
            if os.path.exists(path):
                os.remove(path)

    def flatten(self, ex, key, change_id, **timings):
        return manual.flatten(ex, key, 'operator flatten now', change_id, dict(FAST, **timings))

    def bodies(self, since):
        return [(e, b) for e, b in self.fake.bodies[since:]]

    def bracket_long_4(self):
        out, receipt = self.ex.send(bracket('pb-%d' % self.n, self.tid(), 4, stop=9988.5, tp=10017.25, tp_qty=2))
        self.assertEqual(out, 'accepted', receipt)
        stop, tp = receipt['legs']['stop'], receipt['tp1']['broker_order_id']
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 4 and
                                   set(map(str, self.state.open_orders(ACCOUNT, CONTRACT))) == {stop, tp}))
        return stop, tp

    def assert_stop_still_protects(self, r, stop, tp):
        self.assertEqual((r['protective_stop_working'], r['protected'], r['net_position']), (True, True, 4))
        self.assertEqual(sorted(r['still_working']), sorted([stop, tp]))
        self.assertEqual(self.fake.orders[int(stop)]['ordStatus'], 'Working')
        for oid in (stop, tp):
            self.assertNotIn(('order/cancelorder', {'orderId': int(oid)}), self.fake.bodies, 'closing side untouched')
        self.assertTrue(os.path.exists(self.cfg['kill_switch_file']))

    def add_opening_limit(self):
        """A working Buy limit on a long: it could add exposure."""
        with self.fake.lock:
            self.fake._place({'accountId': ACCOUNT, 'symbol': 'MNQZ1', 'orderQty': 1, 'orderType': 'Limit',
                              'action': 'Buy', 'price': 9600.0}, lambda d, s=200: None)
            oid = max(self.fake.orders)
        self.assertTrue(wait_until(lambda: oid in self.state.open_orders(ACCOUNT, CONTRACT)))
        return oid

    def test_opening_order_cancel_fails_stop_and_tp_keep_working(self):
        stop, tp = self.bracket_long_4()
        add = self.add_opening_limit()
        self.fake.reject_cancel_next = True
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-addfail-%d' % self.n)
        self.assertEqual((r['ok'], r['error']), (False, 'cancel_not_confirmed'))
        self.assertEqual(self.bodies(n), [('order/cancelorder', {'orderId': add})], 'no liquidation')
        self.assertEqual((r['protective_stop_working'], r['protected'], r['net_position']), (True, True, 4))
        self.assertEqual(self.fake.orders[int(stop)]['ordStatus'], 'Working')

    def test_liquidation_rejected_stop_keeps_working(self):
        stop, tp = self.bracket_long_4()
        self.fake.reject_liquidate_next = True
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-lrejstop-%d' % self.n)
        self.assertEqual((r['ok'], r['error'], r['outcome']), (False, 'liquidation_rejected', 'rejected'))
        self.assertEqual([e for e, _ in self.bodies(n)], ['order/liquidateposition'])
        self.assert_stop_still_protects(r, stop, tp)

    def test_liquidation_unknown_stop_keeps_working(self):
        stop, tp = self.bracket_long_4()
        self.fake.swallow_endpoints.add('order/liquidateposition')
        r = self.flatten(self.ex, 'main', 'h-lunkstop-%d' % self.n)
        self.assertEqual((r['ok'], r['error'], r['outcome']), (False, 'liquidation_unknown', 'unknown'))
        self.assert_stop_still_protects(r, stop, tp)

    def test_stop_left_after_liquidation_is_cancelled_once_flat(self):
        """If Exit at Mkt & Cxl does not cancel the orders (documentation-based), the clean-up does."""
        stop, tp = self.bracket_long_4()
        self.fake.liquidate_cancels_orders = False
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-left-%d' % self.n)
        self.assertTrue(r['ok'], r)
        sent = [(e, b.get('orderId')) for e, b in self.bodies(n)]
        self.assertEqual(sent[0], ('order/liquidateposition', None))
        self.assertEqual(sorted(sent[1:]), sorted([('order/cancelorder', int(stop)), ('order/cancelorder', int(tp))]))
        self.assertEqual(sorted(r['cancelled']), sorted([stop, tp]))
        self.assertEqual(self.fake.working_orders(CONTRACT), [])

    def oco_bracket_long_4(self):
        """M == N bracket: stop and TP1 are OCO legs of one placeoso; cancelling one cancels the other."""
        self.fake.oco_cancel_links = True
        out, receipt = self.ex.send(bracket('oco-%d' % self.n, self.tid(), 4, stop=9988.5, tp=10017.25, tp_qty=4))
        self.assertEqual(out, 'accepted', receipt)
        stop, tp = receipt['legs']['stop'], receipt['legs']['tp1']
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 4 and
                                   set(map(str, self.state.open_orders(ACCOUNT, CONTRACT))) == {stop, tp}))
        return stop, tp

    def test_oco_bracket_liquidation_rejected_keeps_the_stop(self):
        stop, tp = self.oco_bracket_long_4()
        self.fake.reject_liquidate_next = True
        r = self.flatten(self.ex, 'main', 'h-ocorej-%d' % self.n)
        self.assertEqual((r['ok'], r['error']), (False, 'liquidation_rejected'))
        self.assert_stop_still_protects(r, stop, tp)

    def test_oco_bracket_flattens_and_cleans_up(self):
        stop, tp = self.oco_bracket_long_4()
        self.fake.liquidate_cancels_orders = False  # the clean-up cancels one leg; OCO takes the other
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-oco-%d' % self.n)
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.bodies(n)[0][0], 'order/liquidateposition')
        self.assertEqual(self.fake.working_orders(CONTRACT), [])
        self.assertEqual(sorted(r['cancelled']), sorted([stop, tp]))

    def test_oco_cancel_model(self):
        """The fake's OCO model: cancelling one leg cancels its sibling; a filled leg cancels the other."""
        stop, tp = self.oco_bracket_long_4()
        out, rec, _ = self.ex._cancel_all(self.state.sync_epoch, targets=[int(tp)])
        self.assertTrue(wait_until(lambda: self.fake.orders[int(stop)]['ordStatus'] == 'Canceled'))

    def test_tp1_just_sent_is_closed_either_way(self):
        """Listed or still unlisted when the flatten starts: the account ends flat with nothing working."""
        tid = self.tid()
        self.assertEqual(self.ex.send(entry('e-%d' % self.n, tid, 3))[0], 'accepted')
        self.assertEqual(self.ex.send(tp1('t-%d' % self.n, tid, 2, 10017.25))[0], 'accepted')
        r = self.flatten(self.ex, 'main', 'h-fresh-%d' % self.n)
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.fake.working_orders(CONTRACT), [])
        self.assertEqual(self.fake.positions[(ACCOUNT, CONTRACT)]['netPos'], 0)

    def test_flat_with_working_orders_cancels_all(self):
        tid = self.tid()
        self.assertEqual(self.ex.send(entry('e-%d' % self.n, tid, 1))[0], 'accepted')
        out, receipt = self.ex.send(tp1('t-%d' % self.n, tid, 1, 10017.25))
        self.assertEqual(out, 'accepted', receipt)
        with self.fake.lock:  # the position closes by hand; the TP1 stays working on a flat contract
            self.fake._place({'accountId': ACCOUNT, 'symbol': 'MNQZ1', 'orderQty': 1, 'orderType': 'Market',
                              'action': 'Sell'}, lambda d, s=200: None)
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 0))
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-flatw-%d' % self.n)
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.bodies(n), [('order/cancelorder', {'orderId': int(receipt['broker_order_id'])})])

    def test_flat_account_only_sets_the_switch(self):
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-flat-%d' % self.n)
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['net_position'], r['cancelled'], r['kill_switch']), (0, [], 'set'))
        self.assertEqual(self.bodies(n), [], 'nothing sent')
        self.assertTrue(self.ex.entries_disabled())
        self.assertEqual(set(json.loads(Path(self.cfg['kill_switch_file']).read_text())), NOTE_KEYS)

    def test_long_4_with_stop_and_tp1_liquidated_then_flat_then_journaled(self):
        tid = self.tid()
        out, receipt = self.ex.send(bracket('b-%d' % self.n, tid, 4, stop=9988.5, tp=10017.25, tp_qty=2))
        self.assertEqual(out, 'accepted', receipt)
        self.assertEqual(receipt['tp1']['outcome'], 'accepted', receipt)
        stop, tp = receipt['legs']['stop'], receipt['tp1']['broker_order_id']
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 4 and
                                   set(map(str, self.state.open_orders(ACCOUNT, CONTRACT))) == {stop, tp}))
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-long-%d' % self.n)
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['net_position'], sorted(r['cancelled']), r['still_working']), (0, sorted([stop, tp]), []))
        sent = self.bodies(n)
        self.assertEqual(sent, [('order/liquidateposition', {'accountId': ACCOUNT, 'contractId': CONTRACT,
                                                             'admin': False})],
                         'stop and TP1 work until Exit at Mkt & Cxl')
        self.assertEqual(sorted(r['receipt']['cancel']['left_for_liquidation']), sorted([stop, tp]))
        self.assertEqual(self.fake.positions[(ACCOUNT, CONTRACT)]['netPos'], 0)
        self.assertEqual(self.fake.working_orders(CONTRACT), [])
        start, result = [x for x in manual_records(self.cfg['journal_dir'])
                         if x['command_id'] == 'manual-flatten:h-long-%d' % self.n]
        self.assertEqual((start['kind'], start['source'], start['mode'], start['quantity']),
                         ('manual_flatten', 'operator', 'liquidate', 4))
        self.assertEqual((result['outcome'], result['receipt']['ok'], result['receipt']['cancel']['confirmed']),
                         ('accepted', True, True))
        self.assertEqual(result['receipt']['liquidation']['outcome'], 'accepted')
        self.assertTrue(os.path.exists(self.cfg['kill_switch_file']))

    def test_short_position(self):
        tid = self.tid('short')
        self.assertEqual(self.ex.send(entry('s-%d' % self.n, tid, 2))[0], 'accepted')
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == -2))
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-short-%d' % self.n)
        self.assertTrue(r['ok'], r)
        self.assertEqual([e for e, _ in self.bodies(n)], ['order/liquidateposition'])
        self.assertEqual(self.fake.positions[(ACCOUNT, CONTRACT)]['netPos'], 0)
        rec = manual_records(self.cfg['journal_dir'])[-1]
        self.assertEqual(rec['receipt']['liquidation']['close_action'], 'Buy')

    def test_cancel_times_out_switch_stays_on_no_liquidation(self):
        tid = self.tid()
        self.assertEqual(self.ex.send(entry('e-%d' % self.n, tid, 2))[0], 'accepted')
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 2))
        add = self.add_opening_limit()
        self.fake.swallow_endpoints.add('order/cancelorder')  # the cancel request is lost: no reply, no effect
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-cto-%d' % self.n)
        self.assertEqual((r['ok'], r['error'], r['net_position']), (False, 'cancel_not_confirmed', 2))
        self.assertEqual(r['still_working'], [str(add)])
        self.assertEqual([e for e, _ in self.bodies(n)], ['order/cancelorder'], 'sent once, never retried, no liquidation')
        self.assertTrue(os.path.exists(self.cfg['kill_switch_file']))
        result = manual_records(self.cfg['journal_dir'])[-1]
        self.assertEqual((result['outcome'], result['receipt']['cancel']['confirmed']), ('unknown', False))

    def test_cancel_refused_on_a_flat_account_switch_stays_on(self):
        tid = self.tid()
        self.assertEqual(self.ex.send(entry('e-%d' % self.n, tid, 1))[0], 'accepted')
        self.assertEqual(self.ex.send(tp1('t-%d' % self.n, tid, 1, 10017.25))[0], 'accepted')
        self.assertTrue(wait_until(lambda: len(self.state.open_orders(ACCOUNT, CONTRACT)) == 1))
        with self.fake.lock:  # closed by hand: the TP1 is left working on a flat contract
            self.fake._place({'accountId': ACCOUNT, 'symbol': 'MNQZ1', 'orderQty': 1, 'orderType': 'Market',
                              'action': 'Sell'}, lambda d, s=200: None)
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 0))
        self.fake.reject_cancel_next = True
        r = self.flatten(self.ex, 'main', 'h-crej-%d' % self.n)
        self.assertEqual((r['ok'], r['error'], r['net_position']), (False, 'cancel_not_confirmed', 0))
        self.assertTrue(self.ex.entries_disabled())
        self.assertNotIn('order/liquidateposition', [e for e, _ in self.fake.bodies[-2:]])

    def test_liquidation_rejected_switch_stays_on(self):
        tid = self.tid()
        self.assertEqual(self.ex.send(entry('e-%d' % self.n, tid, 2))[0], 'accepted')
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 2))
        self.fake.reject_liquidate_next = True
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-lrej-%d' % self.n)
        self.assertEqual((r['ok'], r['error'], r['net_position'], r['outcome']), (False, 'liquidation_rejected', 2, 'rejected'))
        self.assertEqual([e for e, _ in self.bodies(n)], ['order/liquidateposition'])
        self.assertTrue(os.path.exists(self.cfg['kill_switch_file']))
        self.assertEqual(r['receipt']['liquidation'].get('reject_reason'), 'Liquidation rejected')

    def test_liquidation_lost_is_unknown_and_never_retried(self):
        tid = self.tid()
        self.assertEqual(self.ex.send(entry('e-%d' % self.n, tid, 1))[0], 'accepted')
        self.assertTrue(wait_until(lambda: self.state.net_position(ACCOUNT, CONTRACT) == 1))
        self.fake.swallow_endpoints.add('order/liquidateposition')
        n = len(self.fake.bodies)
        r = self.flatten(self.ex, 'main', 'h-llost-%d' % self.n)
        self.assertEqual((r['ok'], r['error'], r['outcome'], r['net_position']), (False, 'liquidation_unknown', 'unknown', 1))
        self.assertEqual([e for e, _ in self.bodies(n)], ['order/liquidateposition'])
        self.assertEqual(len(self.ex.journal.unresolved()), 1, 'entries stay blocked until reconciled')

    def test_follower_account_on_the_same_login(self):
        tid = self.tid()
        self.assertEqual(self.ex.send(entry('e-%d' % self.n, tid, 1))[0], 'accepted')
        out, receipt = self.fb.send(bracket('fb-%d' % self.n, tid, 3, stop=9988.5, tp=10017.25, tp_qty=3))
        self.assertEqual(out, 'accepted', receipt)
        self.assertTrue(wait_until(lambda: self.state.net_position(OTHER, CONTRACT) == 3 and
                                   len(self.state.open_orders(OTHER, CONTRACT)) == 2))
        n = len(self.fake.bodies)
        r = self.flatten(self.fb, 'account-b', 'h-fb-%d' % self.n)
        self.assertTrue(r['ok'], r)
        self.assertEqual(len(r['cancelled']), 2)
        self.assertEqual({b.get('accountId') for e, b in self.bodies(n) if e == 'order/liquidateposition'}, {OTHER})
        self.assertEqual(self.fake.positions[(ACCOUNT, CONTRACT)]['netPos'], 1, 'leader untouched')
        self.assertTrue(os.path.exists(self.cfg['followers'][0]['kill_switch_file']))
        self.assertFalse(os.path.exists(self.cfg['kill_switch_file']))
        self.assertTrue(self.fb.entries_disabled())
        self.assertFalse(self.ex.entries_disabled())

    def test_leader_flatten_is_an_outside_close_for_firing(self):
        tid = self.tid()
        out, receipt = self.ex.send(bracket('lb-%d' % self.n, tid, 4, stop=9988.5, tp=10017.25, tp_qty=2))
        self.assertEqual(out, 'accepted', receipt)
        self.assertTrue(wait_until(lambda: len(self.state.open_orders(ACCOUNT, CONTRACT)) == 2))
        r = self.flatten(self.ex, 'account-a', 'h-lead-%d' % self.n)
        self.assertTrue(r['ok'], r)
        trade = {'id': tid, 'phase': 'open'}
        report = followers.summary(self.fx, self.ex, tid)
        self.assertNotIn('own_liquidation', report['main']['leader'])
        self.assertTrue(follower_native.leader_flat_candidate(trade, report), report['main'])
        watch = follower_native.LeaderFlatWatch()
        watch.observe(trade, report, 0)
        fact = watch.observe(trade, followers.summary(self.fx, self.ex, tid), 3000)
        self.assertEqual((fact['trade_id'], fact['external']), (tid, 4))
        snap, why = self.ex.snapshot(tid)
        self.assertIsNone(why)
        self.assertEqual(snap['position_qty'], 0)
        self.assertEqual(sorted(o['status'] for o in snap['orders']), ['cancelled', 'cancelled', 'filled'])


if __name__ == '__main__':
    unittest.main()
