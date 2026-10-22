#!/usr/bin/env python3
"""Tests for the 2.0 front end and its derived fields (`checks`, `trade`, card `lifecycle`).

Scenarios here extend test_followers' (same Env, same offline fakes): a trade before TP1 with
initial stops, A's kill switch with Soup held, Tradovate logged out, balances unavailable while
healthy, and follower balances reported by a daemon that carries `cash_balance` on /followers rows
(tvbridge 1.0.9.2). Run: python3 -m unittest test_page20 -v
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server
import test_followers as tf
from test_followers import Env, order, row, cash, native, lane_initial, lane_be, EB, EC, T0, PLAN, TRADE

SCENARIOS = ('mid_trade_initial', 'kill_switch_soup_held', 'logged_out', 'balances_unavailable',
             'follower_balances')
ALL = tf.SCENARIOS + SCENARIOS + ('resolved_alert', 'unresolved_alert')


def setUpModule():
    tf.setUpModule()


def tearDownModule():
    tf.tearDownModule()


def build(env, name):
    kb, kc = 'ENTRIES-DISABLED-account-b', 'ENTRIES-DISABLED-account-c'
    flatb, flatc = row('account-b', 100002), row('account-c', 100003)
    if name == 'mid_trade_initial':          # all three short 4, initial stops confirmed, TP1 limits working
        env.leader_trade(9, 4)
        env.firing(lanes={'account-b': {'native': {'blocked': False, 'trades': {TRADE: {'mode': 'native'}}}},
                          'account-c': {'native': {'blocked': False, 'trades': {TRADE: {'mode': 'native'}}}}})
        env.write_lines('firing/account-b/mirror-journal.jsonl', lane_initial())
        env.write_lines('firing/account-c/mirror-journal.jsonl', lane_initial())
        env.followers_report(row('account-b', 100002, -4, [order(801, 'STP', 4), order(802, 'LMT', 2, tp1=True)]),
                             row('account-c', 100003, -4, [order(901, 'STP', 4), order(902, 'LMT', 2, tp1=True)]),
                             main=row('main', 100001, -4, [order(tf.STOP_ID, 'STP', 4),
                                                             order('1000003', 'LMT', 2, tp1=True)]))
    elif name == 'kill_switch_soup_held':    # A's kill switch on and Soup holding entries
        env.kill('ENTRIES-DISABLED'), env.kill(kb), env.kill(kc)
        soup = json.loads((env.root / 'soup' / 'status.json').read_text())
        soup.update(manual_enabled=False, locked=True)
        env.write_json('soup/status.json', soup)
        env.followers_report(flatb, flatc)
    elif name == 'logged_out':               # the login guard saw Tradovate logged out; page not authorized
        env.write_json('guard/status.json', {'state': 'logged_out_waiting', 'reason': 'login_form_visible',
                                             'checked_at': env.iso(env.now - 12), 'logged_out_since': env.iso(env.now - 95),
                                             'logins_last_hour': 1, 'last_login_attempt_at': env.iso(env.now - 40)})
        env.health = {'health': {'ok': False, 'problems': ['page_state:login'],
                                 'page': {'state': 'login', 'synced': False, 'url': tf.DEMO, 'lastRxAgoMs': 95000,
                                          'reconnectAttempts': 1}}}
        env.state = None
        env.report = None
        env.account = None
        env.firing()
    elif name == 'balances_unavailable':     # healthy, both followers trading, but no balance source at all
        env.account = None
        env.firing()
        env.followers_report(flatb, flatc)
    elif name == 'follower_balances':        # tvbridge 1.0.9.2: /followers rows carry cash_balance
        env.firing()
        rb = dict(flatb, cash_balance=cash(amount=4567.75, realized=12.5, ts=env.iso(env.now - 30)))
        rc = dict(flatc, cash_balance=cash(amount=6789.5, realized=-31.25, ts=env.iso(env.now - 30)))
        env.followers_report(rb, rc)
    else:
        tf.build(env, name)


def scenario_snapshot(name, tmp_dir=None):
    with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
        env = Env(tmp)
        build(env, name)
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


def all_snapshot(name, tmp_dir=None):
    """Any scenario: test_followers', test_resolved's or this module's."""
    import test_resolved as tr
    if name in tr.SCENARIOS:
        return tr.scenario_snapshot(name, tmp_dir)
    return scenario_snapshot(name, tmp_dir)


class Page20Case(unittest.TestCase):
    def snap(self, name):
        return all_snapshot(name, os.environ.get('PANEL_TEST_TMP'))

    def cards(self, s):
        return {c['key']: c for c in s['followers']['cards']}

    def step_states(self, card):
        return {x['key']: x['state'] for x in card['lifecycle']['steps']}

    def step_times(self, card):
        return {x['key']: x['time_text'] for x in card['lifecycle']['steps']}

    @staticmethod
    def page_parts():
        page = server.PAGE
        body = page[page.index('<body>'):page.index('<script>')]
        script = page[page.index('<script>'):]
        return page, body, script

    # --- lifecycle -----------------------------------------------------------------------------------

    def test_all_in_trade_break_even_lifecycle(self):
        c = self.cards(self.snap('all_in_trade_be'))
        for key in ('account-a', 'account-b', 'account-c'):
            self.assertEqual(self.step_states(c[key]), {'entry': 'done', 'stop': 'done', 'tp1': 'done', 'break_even': 'done',
                                                        'runners': 'now', 'flat': 'pending'}, key)
            self.assertEqual(c[key]['lifecycle']['stage'], 'runners')
        ta = self.step_times(c['account-a'])
        self.assertEqual(ta['entry'], '18:40:02.000 UTC')        # the bracket entry command
        self.assertEqual(ta['stop'], '18:40:10.000 UTC')         # first stop version Tradovate showed
        self.assertEqual(ta['tp1'], '18:41:04.000 UTC')          # first snapshot with the stop leg at 2 (TP1 filled)
        self.assertEqual(ta['break_even'], '18:41:16.000 UTC')   # break-even stop confirmed
        tb = self.step_times(c['account-b'])
        self.assertEqual(tb['break_even'], '18:41:21.000 UTC')
        self.assertIsNone(tb['tp1'])   # the lane does not record the fill time; never a guess

    def test_mid_trade_before_tp1(self):
        s = self.snap('mid_trade_initial')
        c = self.cards(s)
        self.assertEqual(self.step_states(c['account-a']), {'entry': 'done', 'stop': 'done', 'tp1': 'now', 'break_even': 'pending',
                                                              'runners': 'pending', 'flat': 'pending'})
        self.assertEqual(c['account-a']['lifecycle']['label'], 'Stop confirmed, waiting for TP1')
        for key in ('account-b', 'account-c'):
            self.assertEqual(self.step_states(c[key])['tp1'], 'now')      # TP1 limit working, not filled
            self.assertEqual(c[key]['stop']['kind_label'], 'initial stop')
        self.assertEqual(s['trade']['open'], True)
        self.assertEqual((s['trade']['side'], s['trade']['size']), ('short', 4))
        self.assertEqual([f['relation'] for f in s['trade']['followers']], ['same', 'same'])

    def test_follower_tp1_is_placed_not_filled(self):
        # The lane's `tp1` record is the limit being placed. Without a shrunken position or a
        # break-even move it stays 'now' (working); it is never 'done' on that record alone.
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            build(env, 'mid_trade_initial')
            env.write_lines('firing/account-b/mirror-journal.jsonl', lane_initial() + [
                native('tp1', T0 + 9000, command_id='native-tp1:' + TRADE, quantity=2, price=11960.0)])
            ps = env.patches()
            for p in ps:
                p.start()
            try:
                s = json.loads(json.dumps(server.snapshot(), default=str))
            finally:
                for p in ps:
                    p.stop()
        c = self.cards(s)
        self.assertEqual(self.step_states(c['account-b'])['tp1'], 'now')
        self.assertEqual(self.step_states(c['account-b'])['break_even'], 'pending')

    def test_skipped_and_behind(self):
        s = self.snap('skipped_c')
        c = self.cards(s)
        self.assertEqual(self.step_states(c['account-c']), {'entry': 'skipped', 'stop': 'none', 'tp1': 'none', 'break_even': 'none',
                                                              'runners': 'none', 'flat': 'none'})
        self.assertEqual(c['account-c']['lifecycle']['stage'], 'skipped')
        rel = {f['key']: f for f in s['trade']['followers']}
        self.assertEqual(rel['account-c']['relation'], 'skipped')
        self.assertEqual(rel['account-b']['relation'], 'same')

    def test_follower_fault_and_finished_trade(self):
        s = self.snap('follower_alert')
        c = self.cards(s)
        self.assertEqual(self.step_states(c['account-b'])['flat'], 'failed')
        self.assertEqual(c['account-b']['lifecycle']['stage'], 'failed')
        self.assertEqual(self.step_states(c['account-c'])['flat'], 'done')
        self.assertEqual(self.step_times(c['account-c'])['flat'], '18:48:52.000 UTC')
        rel = {f['key']: f['relation'] for f in s['trade']['followers']}
        self.assertEqual(rel, {'account-b': 'fault', 'account-c': 'same'})
        self.assertEqual(s['trade']['open'], False)           # A is flat; B's short 1 shows in the glance text
        self.assertEqual(s['trade']['text'], 'B short 1')

    def test_flat_and_unknown_lifecycles(self):
        c = self.cards(self.snap('linked_not_trading'))
        self.assertTrue(all(v == 'none' for v in self.step_states(c['account-a']).values()))
        self.assertEqual(c['account-a']['lifecycle']['stage'], 'idle')
        c = self.cards(self.snap('leader_unhealthy'))
        self.assertEqual(c['account-a']['lifecycle']['stage'], 'idle')
        s = self.snap('leader_unhealthy')
        self.assertEqual(s['trade']['unknown'], True)
        self.assertEqual([f['relation'] for f in s['trade']['followers']], ['fault', 'fault'])

    def test_leader_lifecycle_never_guesses(self):
        life = server.leader_lifecycle({'trade_id': 't', 'open': True, 'resting': None, 'problem': 'no_stop_evidence',
                                        'entry_text': '18:00:00.000 UTC'}, -2, None)
        st = {x['key']: x['state'] for x in life['steps']}
        self.assertEqual((st['entry'], st['stop'], st['tp1'], st['break_even']), ('done', 'warn', 'pending', 'pending'))
        life = server.leader_lifecycle({'trade_id': 't', 'open': True, 'resting': False, 'entry_text': None}, -2, None)
        self.assertEqual({x['key']: x['state'] for x in life['steps']}['stop'], 'failed')
        self.assertEqual(server.leader_lifecycle(None, 0, None)['stage'], 'idle')
        self.assertEqual(server.leader_lifecycle({'problem': 'error:X'}, 0, None)['stage'], 'unknown')
        self.assertEqual(server.lane_lifecycle([])['stage'], 'idle')

    # --- checks and trade ------------------------------------------------------------------------------

    def test_checks_six_facts_with_levels(self):
        for name, want in (('all_in_trade_be', 'YYYYYY'), ('leader_kill_switch', 'YYYNYY'), ('kill_switch_soup_held', 'YYYNNY'),
                           ('logged_out', 'NNNYYY'), ('leader_unhealthy', 'NY?YYY')):
            ch = self.snap(name)['checks']
            self.assertEqual([c['key'] for c in ch], ['bridge', 'login', 'sync', 'armed', 'soup', 'feed'], name)
            self.assertEqual(''.join('Y' if c['ok'] else 'N' if c['ok'] is False else '?' for c in ch), want, name)
            for c in ch:
                self.assertIn(c['level'], ('ok', 'warn', 'bad', 'unknown'))
                self.assertTrue(c['text'])
        ch = {c['key']: c for c in self.snap('kill_switch_soup_held')['checks']}
        self.assertEqual((ch['armed']['level'], ch['armed']['text']), ('warn', 'Paused'))   # deliberate: amber, not red
        self.assertEqual((ch['soup']['level'], ch['soup']['text']), ('warn', 'Soup held'))
        ch = {c['key']: c for c in self.snap('logged_out')['checks']}
        self.assertEqual((ch['login']['level'], ch['login']['text']), ('bad', 'Logged out'))
        self.assertEqual(ch['bridge']['text'], 'Bridge not ready')
        ch = {c['key']: c for c in self.snap('unresolved_alert')['checks']}
        self.assertEqual(ch['armed']['text'], 'Armed')     # C's unresolved command is C's, not A's

    def test_feed_stale_is_a_warning(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            build(env, 'balances_unavailable')
            soup = json.loads((env.root / 'soup' / 'status.json').read_text())
            soup['quote_fresh'] = False
            env.write_json('soup/status.json', soup)
            ps = env.patches()
            for p in ps:
                p.start()
            try:
                s = json.loads(json.dumps(server.snapshot(), default=str))
            finally:
                for p in ps:
                    p.stop()
        ch = {c['key']: c for c in s['checks']}
        self.assertEqual((ch['feed']['ok'], ch['feed']['level'], ch['feed']['text']), (False, 'warn', 'Feed stale'))
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')   # the verdict is unchanged by the checks

    # --- balances ---------------------------------------------------------------------------------------

    def test_follower_balances_from_a_1_0_9_2_daemon(self):
        c = self.cards(self.snap('follower_balances'))
        self.assertEqual(c['account-b']['balance']['text'], '$4,567.75')
        self.assertEqual(c['account-b']['balance']['day_pnl_text'], '+$12.50')
        self.assertEqual(c['account-c']['balance']['text'], '$6,789.50')
        self.assertEqual(c['account-c']['balance']['day_pnl_text'], '−$31.25')
        self.assertEqual(c['account-c']['balance']['source'], 'bridge /followers')

    def test_unavailable_balances_are_never_zero_in_any_scenario(self):
        for name in ALL:
            for c in self.snap(name)['followers']['cards']:
                b = c['balance']
                if b['state'] != 'ok':
                    self.assertEqual(b['text'], 'unavailable', (name, c['key']))
                    self.assertIsNone(b['amount'], (name, c['key']))
                    self.assertIsNone(b['day_pnl_text'], (name, c['key']))
                    self.assertTrue(b['reason'], (name, c['key']))
        c = self.cards(self.snap('balances_unavailable'))
        self.assertEqual(c['account-a']['balance']['state'], 'unavailable')
        self.assertIn('bridge /account: no answer', c['account-a']['balance']['reason'])

    def test_page_shows_balance_text_only_never_formats_amounts(self):
        page, body, script = self.page_parts()
        self.assertNotIn('b.amount', script)
        self.assertNotIn('day_pnl)', script)
        self.assertNotIn('toFixed', script)
        self.assertNotIn('toLocaleString', script)
        self.assertIn("ok?b.text:'unavailable'", script)
        self.assertIn("ok?(b.day_pnl_text||'—'):'—'", script)

    # --- the page: wired, read-only, plain words ----------------------------------------------------

    def test_static_page_has_no_hardcoded_values(self):
        page, body, script = self.page_parts()
        for bad in ('$', 'unrestricted', 'MNQ', 'Healthy', 'Armed', 'Trading', 'Short', 'Long', 'UTC', '11995', '1,234'):
            self.assertNotIn(bad, body, bad)
        self.assertIsNone(re.search(r'\d\d:\d\d', body))          # no clock text in the static page
        self.assertIsNone(re.search(r'\d{3,}', body))              # no numbers at all besides small labels
        # every live value on screen comes from /api/status: the script reads these fields
        for field in ('s.verdict', 's.checks', 's.trade', 'c.lifecycle', 'c.balance', 'c.stop', 'c.status', 'fo.alerts',
                      'c.position', 's.glance', 'a.resolved', 'c.note'):
            self.assertIn(field, script, field)
        self.assertEqual(re.findall(r"fetch\('([^']+)'", page), ['/api/status'])

    def test_page_has_no_controls_and_no_html_from_data(self):
        page, body, script = self.page_parts()
        for bad in ('<button', '<form', '<input', '<select', '<textarea', 'onclick', 'onload', 'XMLHttpRequest', 'method=',
                    'innerHTML', 'insertAdjacentHTML', 'outerHTML', 'document.write', 'eval(', 'new Function'):
            self.assertNotIn(bad, page, bad)
        # Two display-only links are allowed: the header's gear to Bridge Advanced (decrypt-advanced://)
        # and, before setup, the Decrypt OS Connections link (decrypt-os://). Both are desktop launchers,
        # never controls: no onclick, no javascript:, no http, no POST.
        links = re.findall(r'<a\s[^>]*>', body)
        self.assertEqual(len(links), 2, links)
        self.assertTrue(links[0].startswith('<a class="gear" href="decrypt-advanced://open"'), links[0])
        self.assertTrue(links[1].startswith('<a class="btn" href="decrypt-os://connections"'), links[1])
        for link in links:
            self.assertNotIn('onclick', link)
            self.assertNotIn('javascript:', link)
        self.assertNotIn('http', body.lower().replace('http-equiv', '').replace('decrypt-advanced://open', ''))
        self.assertIn('<title>Tradovate bridge | Decrypt OS</title>', page)   # the window rules match this title

    def test_no_mirror_wording_and_plain_words(self):
        page, body, script = self.page_parts()
        self.assertNotIn('id="legacy"', body)      # the retired copy service has no row any more
        self.assertNotIn('mirror', body.lower())
        for lit in re.findall(r"'([^'\n]*)'", script):
            self.assertNotIn('mirror', lit.lower(), lit)
        for name in ALL:
            s = self.snap(name)
            new = [c['text'] for c in s['checks']] + [c['lifecycle']['label'] for c in s['followers']['cards']] \
                + [f['text'] for f in s['trade']['followers']] + [x['label'] for c in s['followers']['cards'] for x in c['lifecycle']['steps']]
            for text in tf.visible_text(s) + new:
                self.assertNotIn('mirror', text.lower(), (name, text))
                self.assertNotRegex(text, r'\bNone\b|undefined|NaN', (name, text))
            for text in new:
                self.assertNotRegex(text, r'_|\d{6,}|unrestricted', (name, text))   # plain words, no ids or profiles
        # a 1.3 reason such as leader_closed_externally is shown by the page with spaces
        self.assertIn("const words=t=>String(t??'').replace(/_/g,' ')", script)

    def test_reduced_motion_and_self_contained(self):
        page, body, script = self.page_parts()
        self.assertIn('@media (prefers-reduced-motion:no-preference)', page)
        self.assertNotIn('@import', page)
        self.assertNotIn('<link', page)
        self.assertNotIn('url(', page)                            # no external fonts, images or CDNs
        for font in ("'Space Grotesk'", "'JetBrains Mono'", "'Noto Sans'", "'Noto Sans Mono'"):
            self.assertIn(font, page)                             # the VM's own fonts, with fallbacks

    # --- JSON: additive, watcher fields unchanged ------------------------------------------------------

    def test_json_is_additive_over_1_4(self):
        s = self.snap('all_in_trade_be')
        for k in ('health', 'health_error', 'state', 'mirror', 'kill_switch', 'guard', 'guard_events', 'units',
                  'verdict', 'stop', 'stop_view', 'soup', 'followers', 'glance', 'account', 'log', 'version'):
            self.assertIn(k, s)
        self.assertEqual(sorted(s['units']), sorted(server.UNITS))
        self.assertEqual(sorted(s['verdict']), ['detail', 'title', 'tone'])
        for k in ('trade_id', 'open', 'resting', 'kind', 'break_even', 'confirmed_at', 'confirmed_text',
                  'entry_at', 'entry_text', 'entry_quantity', 'tp1_filled_at', 'tp1_filled_text', 'first_confirmed_text'):
            self.assertIn(k, s['stop'])
        self.assertEqual(s['stop']['break_even']['price_text'], '11995.00')
        for c in s['followers']['cards']:
            for k in ('key', 'role', 'name', 'short', 'status', 'kill_switch', 'position', 'orders', 'stop', 'balance',
                      'alert', 'note', 'detail', 'lifecycle'):
                self.assertIn(k, c)
            self.assertEqual([x['key'] for x in c['lifecycle']['steps']], [k for k, _ in server.STEPS])
        for k in ('open', 'unknown', 'side', 'size', 'text', 'contract', 'entered_text', 'stage', 'followers'):
            self.assertIn(k, s['trade'])
        self.assertEqual(s['version'], '2.1')

    def test_derived_fields_never_take_the_panel_down(self):
        with mock.patch.object(server, 'checks', side_effect=RuntimeError('boom')), \
                mock.patch.object(server, 'trade_view', side_effect=RuntimeError('boom')):
            s = scenario_snapshot('mid_trade_initial', os.environ.get('PANEL_TEST_TMP'))
        self.assertEqual(s['checks'], [])
        self.assertEqual(s['trade']['error'], 'error:RuntimeError')
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')

    def test_still_read_only_same_reads(self):
        src = Path(server.__file__).read_text()
        self.assertEqual(src.count('urlopen') + src.count('opener.open('), 1)
        self.assertEqual(sorted(re.findall(r"bridge\('(\w+)'", src)), ['account', 'followers', 'health', 'state'])
        self.assertNotIn('do_POST', src)
        for bad in ('.write_text(', '.unlink(', 'os.remove', 'os.rename', "open(path, 'w", '.touch(', 'mkdir('):
            self.assertNotIn(bad, src)
        s = self.snap('all_in_trade_be')
        self.assertEqual(s['_bridge_calls'], ['health', 'state', 'followers', 'account'])


if __name__ == '__main__':
    unittest.main()


class NotConfiguredCase(unittest.TestCase):
    """A fresh install (no config.json, browser unit off) shows one calm empty state and nothing alarming."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP'))
        root = Path(self.tmp.name)
        (root / 'guard').mkdir()
        for name, value in {'ROOT': root, 'CONFIG': root / 'config.json', 'MIRROR': root / 'mirror.json',
                            'SOUP': root / 'soup.json', 'GUARD_DIR': root / 'guard',
                            'WEBHOOK_REGISTRY': root / 'webhooks.json'}.items():
            p = mock.patch.object(server, name, value); p.start(); self.addCleanup(p.stop)
        off = {'active': 'inactive', 'sub': 'dead', 'result': 'success', 'since': ''}
        units = {u: dict(off) for u in ('tvbridge-browser.service', 'tvbridge-daemon.service',
                                        'tvbridge-login-guard.timer')}
        for name, fake in (('daemon_log', lambda: ([], 0)), ('bridge', lambda endpoint, port: (None, 'URLError')),
                           ('units', lambda: units), ('unit_state', lambda unit: dict(off, load='not-found'))):
            p = mock.patch.object(server, name, fake); p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_fresh_install_has_no_checks_no_armed_and_a_setup_pointer(self):
        s = server.snapshot()
        json.dumps(s, default=str)
        self.assertEqual((s['verdict']['tone'], s['verdict']['title']), ('off', 'Bridge not set up yet'))
        self.assertIn('Connections', s['verdict']['detail'])
        self.assertEqual(s['checks'], [])
        self.assertFalse(s['configured'])
        for card in s['followers']['cards']:
            self.assertNotEqual(card['status']['state'], 'armed')
            self.assertNotIn('armed', card['status']['label'].lower())
        for text in (c['text'] for c in s['checks']):
            self.assertNotIn('unreachable', text.lower())

    def test_configured_bridge_keeps_its_six_checks(self):
        server.CONFIG.write_text(json.dumps({'account_name': 'fixture', 'ticker': 'MNQZ2021', 'listen_port': 8777}))
        s = server.snapshot()
        self.assertNotEqual(s['verdict']['tone'], 'off')
        self.assertEqual([c['key'] for c in s['checks']], ['bridge', 'login', 'sync', 'armed', 'soup', 'feed'])

    def test_page_hides_the_tables_and_shows_the_setup_block_only_before_setup(self):
        page = server.PAGE
        body = page[page.index('<body>'):page.index('<script>')]
        script = page[page.index('<script>'):]
        self.assertIn('<section class="setup" id="setup" hidden', body)
        self.assertIn('Open Decrypt OS → Connections', body)
        self.assertIn("off=v.tone==='off'", script)
        self.assertIn("classList.toggle('unconfigured',off)", script)
        self.assertIn('.unconfigured .trade,.unconfigured .money,.unconfigured .side,.unconfigured .more{display:none}', page)
