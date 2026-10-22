#!/usr/bin/env python3
"""Balance tests for the bridge status panel (1.4). Offline: no network, no systemctl,
no journalctl. Sources under test: the daemon's GET /account reply (Account A, cached)
and a follower's /followers row `cash_balance` (not reported by the 1.0.9.1 daemon: reads unavailable).
Run: python3 -m unittest test_balances -v"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import server
import test_stop
import test_followers as tf
from test_followers import Env, row, account_reply, cash, CASH_TS


def setUpModule():
    test_stop.setUpModule()


def tearDownModule():
    test_stop.tearDownModule()


class BalanceCase(unittest.TestCase):
    def run_env(self, env):
        ps = env.patches()
        for p in ps:
            p.start()
        try:
            s = json.loads(json.dumps(server.snapshot(), default=str))
        finally:
            for p in ps:
                p.stop()
        s['_bridge_calls'] = env.bridge_calls
        return s

    def cards(self, s):
        return {c['key']: c for c in s['followers']['cards']}

    def env(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP'))
        self.addCleanup(self._tmp.cleanup)
        env = Env(self._tmp.name)
        env.followers_report(row('account-b', 100002), row('account-c', 100003))
        return env

    # --- Account A: the daemon's own /account -------------------------------------------------

    def test_leader_balance_from_daemon_account(self):
        env = self.env()
        s = self.run_env(env)
        b = self.cards(s)['account-a']['balance']
        self.assertEqual((b['state'], b['text'], b['amount']), ('ok', '$1,234.50', 1234.5))
        self.assertEqual(b['as_of_text'], '18:43:00 UTC')         # Tradovate's cash-row time, VM zone
        self.assertEqual(b['as_of'], 1615401780000)
        self.assertEqual((b['day_pnl'], b['day_pnl_text']), (-50.0, '\u2212$50.00'))
        self.assertEqual(b['source'], 'bridge /account')
        self.assertEqual(s['_bridge_calls'].count('account'), 1)
        self.assertIsNone(b['reason'])

    def test_account_is_asked_at_most_every_30_seconds(self):
        env = self.env()
        ps = env.patches()
        for p in ps:
            p.start()
        try:
            server.snapshot(), server.snapshot(), server.snapshot()
            self.assertEqual(env.bridge_calls.count('account'), 1)
            server.BALANCE_CACHE['at'] -= server.BALANCE_CACHE_SECONDS + 1
            server.snapshot()
            self.assertEqual(env.bridge_calls.count('account'), 2)
        finally:
            for p in ps:
                p.stop()

    def test_stale_daemon_reply_is_unavailable_never_a_number(self):
        env = self.env()
        old = account_reply()
        env.account = None                       # the route stops answering; the cached reply ages
        ps = env.patches()
        for p in ps:
            p.start()
        try:
            server.BALANCE_CACHE.update(at=time.time() - server.BALANCE_MAX_AGE_SECONDS - 90, reply=old, error=None)
            s = json.loads(json.dumps(server.snapshot(), default=str))
        finally:
            for p in ps:
                p.stop()
        b = self.cards(s)['account-a']['balance']
        self.assertEqual((b['state'], b['text'], b['amount'], b['as_of'], b['day_pnl']),
                         ('stale', 'unavailable', None, None, None))
        self.assertIn('bridge /account: last read 3 min ago', b['reason'])

    def test_daemon_not_synced_or_incomplete_is_unavailable(self):
        no_cash = account_reply()
        no_cash['account_data']['cash_balance'] = None                 # complete left true: still no number
        for reply in (account_reply(synced=False), account_reply(complete=False), no_cash):
            env = self.env()
            env.account = reply
            b = self.cards(self.run_env(env))['account-a']['balance']
            self.assertEqual((b['state'], b['text'], b['amount']), ('unavailable', 'unavailable', None))
            self.assertIn('no complete balance', b['reason'])

    def test_account_view_disabled_asks_nothing_and_reads_unavailable(self):
        env = self.env()
        env.config(account_view=False)
        s = self.run_env(env)
        self.assertNotIn('account', s['_bridge_calls'])
        b = self.cards(s)['account-a']['balance']
        self.assertEqual((b['state'], b['text']), ('unavailable', 'unavailable'))
        self.assertEqual(b['reason'], 'bridge /account: not asked (account_view_disabled)')

    # --- followers: /followers rows -------------------------------------------------------------------

    def test_follower_balance_unavailable_with_the_current_daemon(self):
        env = self.env()
        c = self.cards(self.run_env(env))
        for key in ('account-b', 'account-c'):
            b = c[key]['balance']
            self.assertEqual((b['state'], b['text'], b['amount'], b['source']),
                             ('unavailable', 'unavailable', None, 'bridge /followers'))
            self.assertEqual(b['reason'], 'the bridge does not report follower balances yet')

    def test_follower_balance_when_the_daemon_reports_cash_balance(self):
        env = self.env()
        rb = dict(row('account-b', 100002), cash_balance=cash(2345.0, 200.0))
        rc = dict(row('account-c', 100003, synced=False), cash_balance=cash(3456.25, 0))
        env.followers_report(rb, rc)
        c = self.cards(self.run_env(env))
        b = c['account-b']['balance']
        self.assertEqual((b['state'], b['text'], b['as_of_text'], b['day_pnl_text'], b['source']),
                         ('ok', '$2,345.00', '18:43:00 UTC', '+$200.00', 'bridge /followers'))
        self.assertEqual(c['account-c']['balance']['state'], 'unavailable')   # not synced: never shown
        self.assertEqual(c['account-c']['balance']['text'], 'unavailable')

    def test_follower_balance_null_row_or_not_linked(self):
        env = self.env()
        env.followers_report(dict(row('account-b', 100002), cash_balance=None),
                             row('account-c', 100003, available=False))
        c = self.cards(self.run_env(env))
        self.assertEqual(c['account-b']['balance']['state'], 'unavailable')
        self.assertEqual(c['account-c']['balance']['reason'], 'account not linked')
        env = self.env()
        env.report = None
        c = self.cards(self.run_env(env))
        self.assertEqual(c['account-b']['balance']['reason'], 'the bridge did not report this account')

    # --- words, safety, JSON --------------------------------------------------------------------------

    def test_money_text(self):
        self.assertEqual(server.money_text(1234.5), '$1,234.50')
        self.assertEqual(server.money_text(-50.0), '\u2212$50.00')
        self.assertEqual(server.money_text(-50.0, sign=True), '\u2212$50.00')
        self.assertEqual(server.money_text(12.5, sign=True), '+$12.50')
        self.assertEqual(server.money_text(0), '$0.00')
        self.assertEqual(server.money_text(0, sign=True), '$0.00')
        self.assertIsNone(server.money_text(None))
        self.assertIsNone(server.money_text('1234.5'))
        self.assertIsNone(server.money_text(True))

    def test_every_scenario_never_shows_zero_for_a_missing_balance(self):
        for name in tf.SCENARIOS:
            s = tf.scenario_snapshot(name, os.environ.get('PANEL_TEST_TMP'))
            for c in s['followers']['cards']:
                b = c['balance']
                self.assertIn(b['state'], ('ok', 'stale', 'unavailable'), (name, c['key']))
                if b['state'] != 'ok':
                    self.assertEqual((b['text'], b['amount']), ('unavailable', None), (name, c['key']))
                else:
                    self.assertRegex(b['text'], r'^\u2212?\$[\d,]+\.\d\d$')

    def test_balances_stay_out_of_the_headline(self):
        s = tf.scenario_snapshot('all_in_trade_be', os.environ.get('PANEL_TEST_TMP'))
        head = [s['verdict']['title'], s['verdict']['detail'], s['glance']['trade']['text'],
                s['glance']['followers']['text'], s['followers']['summary']['text']]
        for text in head:
            self.assertNotIn('$', text)
        page = server.PAGE
        body = page[page.index('<body>'):page.index('<script>')]
        self.assertNotIn('$', body)                         # no balance in static copy or the title

    def test_balance_read_error_keeps_the_cards(self):
        env = self.env()
        with mock.patch.object(server, 'leader_balance', side_effect=RuntimeError('boom')):
            s = self.run_env(env)
        c = self.cards(s)
        self.assertEqual(list(c), ['account-a', 'account-b', 'account-c'])
        self.assertEqual(c['account-a']['balance']['text'], 'unavailable')
        self.assertEqual(c['account-a']['balance']['reason'], 'error:RuntimeError')
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')

    def test_read_only_and_same_local_reads(self):
        src = open(server.__file__).read()
        self.assertEqual(src.count('urlopen') + src.count('opener.open('), 1)   # one HTTP path for every route
        self.assertNotIn('tradovateapi', src.split("PAGE = r'''")[0].replace(
            "'live.tradovateapi.com' in endpoint", ''))             # the panel never contacts Tradovate
        self.assertIn("bridge('account', port)", src)
        self.assertIn("cfg.get('account_view_enabled') is not True", src)
        self.assertEqual(server.CASH_TS if hasattr(server, 'CASH_TS') else CASH_TS, CASH_TS)


if __name__ == '__main__':
    unittest.main()
