#!/usr/bin/env python3
"""Resolved-alert and layout-independence tests for the bridge status panel (1.4).
Offline: no network, no systemctl, no journalctl. Synthetic case: Account C's
`order_unknown` (flatten_all outcome uncertain) is reconciled by hand in the
follower's bridge journal (`reconciled` record, no timestamp), the daemon reports
unresolved_commands 0 and the lane trades again afterwards.
Run: python3 -m unittest test_resolved -v"""
import json
import os
import re
import tempfile
import unittest

import server
import test_stop
import test_followers as tf
from test_followers import Env, row, order, native, lane_be, EB, EC, TRADE

SCENARIOS = ('resolved_alert', 'unresolved_alert')
OLD = 'fixture-stream:MNQM2021:1615479900000:long'      # the trade with the unknown exit
A = 1615480200000                             # the alert: 2021-03-11 16:30:00 UTC
CID = 'native-exit:%s:1' % OLD
NEW = 'fixture-stream:MNQM2021:1615490100000:long'      # a later trade on the same lane
N0 = 1615490100000                            # 19:15:00 UTC
LONG_PLAN = {'side': 'long', 'initial': 7968.75, 'break_even': 8013.25}


def setUpModule():
    test_stop.setUpModule()


def tearDownModule():
    test_stop.tearDownModule()


def unknown_alert():
    return {'account': 'account-c', 'at': A, 'command_id': CID, 'kind': 'order_unknown',
            'order_kind': 'flatten_all', 'reason': 'uncertain', 'source': 'native', 'trade_id': OLD, 'type': 'alert'}


def lane_old():
    """C's lane for the old trade: entered, stop proved, exit attempt with an uncertain outcome, done."""
    return [dict(native('start', A - 240000, plan=LONG_PLAN, entry_cid='soup-old', quantity=3), trade_id=OLD),
            dict(native('stop_confirmed', A - 236000, quantity=3, price=7968.75), trade_id=OLD),
            dict(native('exit', A - 60000, reason='leader:time_exit'), trade_id=OLD),
            dict(native('exit_attempt', A - 58000, command_id=CID), trade_id=OLD),
            {'type': 'outcome', 'at': A - 50000, 'command_id': CID, 'outcome': 'unknown', 'trade_id': OLD},
            dict(native('done', A + 15000, result='flat'), trade_id=OLD),
            {'type': 'reconcile', 'at': A + 30000, 'trade_id': OLD, 'final': True, 'native': True, 'result': 'flat',
             'mode': 'native', 'attempts': [CID]}]


def lane_new():
    """A later trade that ran and finished on the same lane."""
    return [dict(native('start', N0, plan=LONG_PLAN, entry_cid='soup-new', quantity=3), trade_id=NEW),
            dict(native('stop_confirmed', N0 + 4000, quantity=3, price=7968.75), trade_id=NEW),
            dict(native('exit', N0 + 420000, reason='broker_stop_initial'), trade_id=NEW),
            dict(native('done', N0 + 424000, result='flat'), trade_id=NEW),
            {'type': 'reconcile', 'at': N0 + 430000, 'trade_id': NEW, 'final': True, 'native': True,
             'result': 'flat', 'mode': 'native'}]


def journal_old(reconciled=True, at=None):
    """C's bridge journal for the old flatten_all: start, result unknown, then (by hand) reconciled."""
    cid = 'f:account-c:' + CID
    recs = [{'at': A - 58000, 'command_id': cid, 'kind': 'flatten_all', 'mode': 'liquidate', 'quantity': 1,
             'side': 'long', 'trade_id': OLD, 'type': 'start'},
            {'command_id': cid, 'outcome': 'unknown', 'type': 'result',
             'receipt': {'code': 'None', 'mode': 'liquidate', 's': 200, 'transport': 'uncertain'}}]
    if reconciled:
        rec = {'command_id': cid, 'outcome': 'accepted', 'type': 'reconciled',
               'receipt': {'reconciled': True, 'note': 'Flatten outcome confirmed: '
                                                       'C flat with nothing working (checked by hand in Tradovate)'}}
        if at is not None:
            rec['at'] = at
        recs.append(rec)
    return recs


def build(env, name, **opt):
    if name == 'resolved_alert':          # today: reconciled by hand, both followers trading again
        env.firing(alerts=[unknown_alert()], lanes={
            'account-b': {'native': {'blocked': False, 'trades': {NEW: {'mode': 'native', 'done': True}}}},
            'account-c': {'native': {'blocked': False, 'trades': {NEW: {'mode': 'native', 'done': True}}}}})
        env.write_lines('firing/account-c/mirror-journal.jsonl', lane_old() + lane_new())
        env.write_lines('firing/account-b/mirror-journal.jsonl', lane_new())
        env.write_lines('journal-account-c/commands.jsonl', journal_old(reconciled=opt.get('reconciled', True),
                                                                          at=opt.get('at')))
        env.followers_report(row('account-b', 100002), row('account-c', 100003,
                                                                 unresolved=opt.get('unresolved', 0)))
    elif name == 'unresolved_alert':      # the same alert while the command is still unknown: a live fault
        env.firing(alerts=[unknown_alert()], lanes={
            'account-b': {'native': {'blocked': False, 'trades': {}}},
            'account-c': {'native': {'blocked': False, 'trades': {OLD: {'mode': 'native', 'done': True}}}}})
        env.write_lines('firing/account-c/mirror-journal.jsonl', lane_old())
        env.write_lines('journal-account-c/commands.jsonl', journal_old(reconciled=False))
        env.followers_report(row('account-b', 100002), row('account-c', 100003, unresolved=1))
    else:
        tf.build(env, name)


def scenario_snapshot(name, tmp_dir=None, **opt):
    with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
        env = Env(tmp)
        build(env, name, **opt)
        ps = env.patches()
        for p in ps:
            p.start()
        try:
            s = server.snapshot()
        finally:
            for p in ps:
                p.stop()
        s = json.loads(json.dumps(s, default=str))
        s['_scenario'] = name
        s['_bridge_calls'] = env.bridge_calls
        return s


class ResolvedCase(unittest.TestCase):
    def snap(self, name, **opt):
        return scenario_snapshot(name, os.environ.get('PANEL_TEST_TMP'), **opt)

    def cards(self, s):
        return {c['key']: c for c in s['followers']['cards']}

    def run_env(self, env):
        ps = env.patches()
        for p in ps:
            p.start()
        try:
            return json.loads(json.dumps(server.snapshot(), default=str))
        finally:
            for p in ps:
                p.stop()

    # --- the reconciled-by-hand case ----------------------------------------------------------------

    def test_reconciled_order_unknown_is_greyed_not_red(self):
        s = self.snap('resolved_alert')
        c = self.cards(s)['account-c']
        a = s['followers']['alerts'][0]
        self.assertEqual(a['kind'], 'order_unknown')
        self.assertEqual((a['live'], a['tone'], a['problem_tone']), (False, '', 'coral'))
        self.assertEqual(a['resolved']['how'], 'reconciled by hand')
        self.assertIsNone(a['resolved']['at'])                # the journal record has no timestamp
        self.assertIsNone(a['resolved']['time_text'])
        self.assertIn('checked by hand in Tradovate', a['resolved']['note'])
        self.assertEqual(a['time_text'], '16:30:00 UTC')
        self.assertEqual((c['alert']['live'], c['alert']['tone']), (False, ''))
        self.assertEqual(c['alert']['text'], 'Order outcome unknown: its entries stay blocked until it is reconciled')
        self.assertEqual(c['alert']['resolved']['how'], 'reconciled by hand')
        self.assertEqual((c['status']['state'], c['status']['label']), ('trading', 'Trading'))
        self.assertEqual(s['followers']['live_alerts'], 0)
        self.assertEqual(s['followers']['summary'], {'text': 'Both trading', 'tone': 'mint'})
        self.assertEqual(s['verdict']['tone'], 'mint')

    def test_reconcile_time_shown_when_the_journal_records_it(self):
        s = self.snap('resolved_alert', at=A + 3600000)       # 17:30:00 UTC
        a = s['followers']['alerts'][0]
        self.assertEqual((a['resolved']['time_text'], a['resolved']['at']), ('17:30:00 UTC', A + 3600000))

    def test_live_count_resolves_when_the_journal_has_no_record(self):
        s = self.snap('resolved_alert', reconciled=False, unresolved=0)
        a = s['followers']['alerts'][0]
        self.assertFalse(a['live'])
        self.assertEqual(a['resolved']['how'], 'the bridge reports no unresolved command')
        self.assertIsNone(a['resolved']['note'])

    def test_unresolved_command_stays_a_live_red_problem(self):
        s = self.snap('unresolved_alert')
        c = self.cards(s)['account-c']
        a = s['followers']['alerts'][0]
        self.assertEqual((a['live'], a['tone'], a['resolved']), (True, 'coral', None))
        self.assertEqual((c['alert']['live'], c['alert']['tone']), (True, 'coral'))
        self.assertEqual((c['status']['state'], c['status']['label'], c['status']['tone']),
                         ('attention', 'Needs a look', 'amber'))
        self.assertIn('1 unresolved command(s): entries blocked until reconciled', c['status']['reason'])
        self.assertEqual(s['followers']['live_alerts'], 1)
        self.assertEqual(s['followers']['summary']['tone'], 'amber')
        self.assertEqual(s['verdict']['tone'], 'mint')   # A is unaffected

    def test_journal_record_wins_over_a_zero_live_count_only_when_definite(self):
        # journal still says unknown, row says 0: the live count decides (resolved, no note)
        s = self.snap('resolved_alert', reconciled=False, unresolved=0)
        self.assertFalse(s['followers']['alerts'][0]['live'])
        # journal reconciled but the row says 1 unresolved (another command): the alert is still resolved
        s = self.snap('resolved_alert', unresolved=1)
        self.assertEqual(s['followers']['alerts'][0]['resolved']['how'], 'reconciled by hand')

    # --- other kinds ------------------------------------------------------------------------------------

    def test_blocked_exit_stays_live_until_reviewed_reset_and_a_newer_trade(self):
        s = self.snap('follower_alert')
        kinds = {a['kind']: a for a in s['followers']['alerts'] if a['account'] == 'account-b'}
        self.assertTrue(kinds['exit_all_unverified']['live'])
        self.assertEqual(kinds['exit_all_unverified']['tone'], 'coral')
        self.assertTrue(kinds['native_exit']['live'])            # B's trade never finished: still live
        self.assertEqual(s['followers']['live_alerts'], 2)
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            tf.build(env, 'follower_alert')
            (env.root / 'firing/account-b' / server.LANE_BLOCK_NAME).unlink()
            st = json.loads((env.root / 'firing/status.json').read_text())
            st['lanes']['account-b']['native'] = {'blocked': False, 'trades': {}}
            env.write_json('firing/status.json', st)
            env.write_lines('firing/account-b/mirror-journal.jsonl', lane_be(EB) + [
                native('exit', tf.T0 + 524000, reason='leader_closed_externally'),
                native('failed', tf.T0 + 540000, reason='leader_closed_externally')] + lane_new())
            env.kill('ENTRIES-DISABLED-account-b', on=False)
            for r in env.report['followers']:   # the daemon reports the resumed switch too
                if r.get('key') == 'account-b':
                    r['entries_disabled'] = False
            s = self.run_env(env)
        kinds = {a['kind']: a for a in s['followers']['alerts'] if a['account'] == 'account-b'}
        self.assertEqual(kinds['exit_all_unverified']['resolved']['how'], 'a newer trade ran after the reviewed reset')
        self.assertEqual(kinds['exit_all_unverified']['resolved']['time_text'], '19:15:00 UTC')
        self.assertEqual(kinds['native_exit']['resolved']['how'], 'a newer trade started')
        self.assertEqual(s['followers']['live_alerts'], 0)
        self.assertEqual(self.cards(s)['account-b']['status']['label'], 'Trading')

    def test_closed_with_the_leader_is_resolved_once_that_trade_finished(self):
        s = self.snap('follower_alert')
        a = next(x for x in s['followers']['alerts'] if x['account'] == 'account-c')
        self.assertEqual(a['text'], 'Closed with the leader: the leader was closed outside the bridge')
        self.assertEqual((a['live'], a['tone']), (False, ''))
        self.assertEqual(a['resolved']['how'], 'the trade finished (flat)')
        self.assertEqual(a['resolved']['time_text'], '18:48:52 UTC')
        c = self.cards(s)['account-c']
        self.assertEqual(c['alert']['resolved']['how'], 'the trade finished (flat)')

    def test_skip_note_stays_until_the_next_trade(self):
        s = self.snap('skipped_c')
        self.assertIsNotNone(self.cards(s)['account-c']['note'])
        self.assertIsNone(s['followers']['alerts'][0]['resolved'])
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            tf.build(env, 'skipped_c')
            env.write_lines('firing/account-c/mirror-journal.jsonl', tf.lane_skipped() + lane_new())
            s = self.run_env(env)
        c = self.cards(s)['account-c']
        self.assertIsNone(c['note'])
        self.assertEqual(c['alert']['resolved']['how'], 'a newer trade started')
        self.assertEqual((c['alert']['live'], c['alert']['tone']), (False, ''))
        self.assertEqual(s['followers']['summary']['text'], 'Both trading')

    def test_resolution_words_are_plain(self):
        for name in SCENARIOS + ('follower_alert', 'skipped_c'):
            s = self.snap(name)
            for a in s['followers']['alerts']:
                if a['resolved']:
                    self.assertRegex(a['resolved']['how'], r'^[a-z][^_]*$')
                    self.assertEqual(a['tone'], '')
                    self.assertFalse(a['live'])
                else:
                    self.assertEqual(a['live'], a['level'] == 'alert')

    def test_alert_json_is_additive(self):
        a = server.alert_view({'account': 'account-b', 'kind': 'lane_stopped', 'at': A}, {})
        for k in ('account', 'account_label', 'kind', 'level', 'text', 'note', 'tone', 'at', 'time_text', 'trade_id'):
            self.assertIn(k, a)                                        # every 1.3 key
        self.assertEqual((a['command_id'], a['resolved'], a['live']), (None, None, True))

    # --- layout-independent content ------------------------------------------------------------------------

    def test_cards_carry_everything_the_page_shows_at_any_width(self):
        keys = ('key', 'role', 'name', 'short', 'status', 'kill_switch', 'position', 'orders', 'stop', 'balance',
                'alert', 'note', 'detail')
        for name in tf.SCENARIOS + SCENARIOS:
            s = self.snap(name)
            for c in s['followers']['cards']:
                for k in keys:
                    self.assertIn(k, c, (name, c.get('key'), k))
                for k in ('state', 'text', 'as_of_text', 'reason', 'source'):
                    self.assertIn(k, c['balance'])
                for k in ('text', 'tone', 'resolved', 'live'):
                    self.assertIn(k, c['alert'])
            for text in tf.visible_text(s):
                self.assertNotRegex(text, r'\bNone\b|undefined|NaN', (name, text))

    def test_page_keeps_three_cards_in_one_row(self):
        # 2.0: the accounts are rows of one table (the trade matrix and the money table), never cards
        # that can wrap; the table is fixed-layout so a narrow window shrinks columns instead.
        page = server.PAGE
        self.assertNotIn('.cards{', page)
        self.assertNotIn('auto-fit,minmax(272px', page)             # the 1.3 rule that let C wrap
        self.assertIn('table{border-collapse:collapse;width:100%;table-layout:fixed}', page)
        self.assertIn('<table class="matrix">', page)
        self.assertIn('<table class="money">', page)
        self.assertEqual(re.findall(r"fetch\('([^']+)'", page), ['/api/status'])
        self.assertEqual(page.count('innerHTML'), 0)
        for bad in ('<button', '<form', '<input', 'onclick', 'XMLHttpRequest', 'method='):
            self.assertNotIn(bad, page)
        self.assertIn('<title>Tradovate bridge | Decrypt OS</title>', page)   # window rules match this title

    def test_watcher_fields_unchanged(self):
        s = self.snap('all_in_trade_be')
        self.assertEqual(sorted(s['units']), sorted(server.UNITS))
        for k in ('health', 'health_error', 'state', 'mirror', 'kill_switch', 'guard', 'guard_events', 'units',
                  'verdict', 'stop', 'stop_view', 'soup', 'followers', 'glance', 'account', 'log', 'version'):
            self.assertIn(k, s)
        self.assertEqual(sorted(s['verdict']), ['detail', 'title', 'tone'])
        for k in ('trade_id', 'open', 'resting', 'kind', 'break_even'):
            self.assertIn(k, s['stop'])
        self.assertEqual(s['stop']['break_even']['price_text'], '11995.00')
        self.assertEqual(s['version'], '2.1')


if __name__ == '__main__':
    unittest.main()
