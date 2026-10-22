"""1.0.9.2: each /followers row reports its own account's Tradovate cash balance (read-only).

`cash_balance` is the same object GET /account returns as `account_data.cash_balance`, read
from the sync data the daemon already holds: no Tradovate request. Missing data is null, never
0; a row that is not synced for the current connection reports null (never a previous
connection's balance). Unavailable follower rows are unchanged. Only with config.json's existing
`account_view_enabled` (the GET /account switch); without it every row is exactly the 1.0.9.1 row.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness  # noqa: F401  (puts the repository on sys.path)

from tvbridge import followers
from tvbridge.daemon import account_view, make_handler
from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.state import AccountState

TOKEN = 'follower-balance-token-0123456789abcdef'
TDATE = {'year': 2021, 'month': 3, 'day': 10}
ACCOUNTS = [{'id': 7, 'name': 'MAIN-ACCT'}, {'id': 8, 'name': 'F-ONE'}, {'id': 9, 'name': 'F-TWO'},
            {'id': 10, 'name': 'NOT-A-FOLLOWER'}]
CASH_FIELDS = {'amount', 'amount_sod', 'realized_pnl', 'week_realized_pnl', 'trade_date', 'timestamp'}
# The 1.0.9.1 row keys; 1.0.9.2 adds exactly `cash_balance`.
ROW_KEYS_1091 = {'account_id', 'available', 'synced', 'healthy', 'logged_in', 'problems', 'net_position',
                 'orders', 'unresolved_commands', 'entries_disabled', 'native_exits', 'account_name_sha256', 'key'}


def cash(account, amount, day=10, cid=None, realized=12.5, **extra):
    row = {'id': cid or account * 100 + day, 'accountId': account, 'amount': amount,
           'amountSOD': None if amount is None or not isinstance(amount, (int, float)) else amount - realized,
           'realizedPnL': realized, 'weekRealizedPnL': 40.25, 'currencyId': 1, 'archived': False,
           'tradeDate': dict(TDATE, day=day), 'timestamp': '2021-03-%02dT17:30:00.000Z' % day}
    row.update(extra)
    return row


class Page:
    """One page on one login: main 7, followers 8 and 9, and one other account 10."""

    def __init__(self, state, cash_rows):
        self.state, self.epoch, self.page_state = state, 'e1', 'authorized'
        self.requests = []
        self.sync(cash_rows)

    def sync(self, cash_rows, epoch=None):
        self.epoch = epoch or self.epoch
        data = {'accounts': ACCOUNTS, 'orders': [], 'fills': [], 'orderVersions': [], 'positions': [],
                'contracts': [{'id': 2, 'name': 'MNQZ1'}]}
        if cash_rows is not None:
            data['cashBalances'] = cash_rows
        self.state.load_sync(ACCOUNTS, data, self.epoch)

    def status(self):
        return {'state': self.page_state, 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5.0, expected_epoch=None):
        self.requests.append(endpoint)
        return {'ok': True, 'sent': True, 's': 200, 'd': {'orderId': 9000 + len(self.requests)}}


def follower_entry(key, name, tmp, **kw):
    f = {'key': key, 'account_name': name, 'journal_dir': str(tmp / ('journal-' + key)),
         'kill_switch_file': str(tmp / ('ENTRIES-DISABLED-' + key)), 'max_contracts': 10,
         'prop_profile': 'unrestricted'}
    f.update(kw)
    return f


class Pub:
    last = {}


class FollowerBalances(unittest.TestCase):
    CASH = [cash(7, 1234.5, realized=-3.5), cash(8, 2345.0, realized=200.0), cash(9, 3456.25, realized=0),
            cash(10, 1.0)]

    def setUp(self):
        self.start(self.CASH)

    def start(self, cash_rows, account_view_enabled=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = AccountState()
        self.page = Page(self.state, cash_rows)
        self.cfg = {'ticker': 'MNQZ2021', 'account_name': 'MAIN-ACCT', 'account_id': 7,
                    'soup_account_id': 'Account A', 'max_contracts': 10, 'tick_size': .25,
                    'journal_dir': str(self.tmp / 'journal'), 'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED'),
                    'publish_mode': 'adapter-db', 'account_view_enabled': account_view_enabled,
                    'followers': [follower_entry('f1', 'F-ONE', self.tmp), follower_entry('f2', 'F-TWO', self.tmp),
                                  follower_entry('gone', 'NOT-IN-THIS-LOGIN', self.tmp),
                                  follower_entry('off', 'F-OFF', self.tmp, enabled=False)]}
        self.main = Executor(self.page, self.state, Journal(self.cfg['journal_dir']), self.cfg)
        self.main.resolve()
        self.fx = followers.build(self.page, self.state, self.cfg, self.main)
        server = ThreadingHTTPServer(('127.0.0.1', 0), followers.make_follower_handler(
            make_handler(self.main, Pub(), TOKEN, account_view_enabled is True), self.fx, TOKEN, self.main))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = 'http://127.0.0.1:%d' % server.server_address[1]
        self.requests_before = list(self.page.requests)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def report(self, query=''):
        status, body = self.get('/followers/' + TOKEN + query)
        self.assertEqual(status, 200)
        rows = {r['key']: r for r in body['followers']}
        rows['main'] = body['main']
        return body, rows

    def assertNoTradovateRequest(self):
        self.assertEqual(self.page.requests, self.requests_before, 'a balance read never sends a Tradovate request')

    # --- present ------------------------------------------------------------------------------

    def test_present_each_row_reports_its_own_account(self):
        body, rows = self.report()
        expected = {'main': (1234.5, -3.5), 'f1': (2345.0, 200.0), 'f2': (3456.25, 0)}
        for key, (amount, realized) in expected.items():
            with self.subTest(key=key):
                c = rows[key]['cash_balance']
                self.assertEqual(set(c), CASH_FIELDS)
                self.assertEqual((c['amount'], c['realized_pnl'], c['timestamp'], c['trade_date']),
                                 (amount, realized, '2021-03-10T17:30:00.000Z', TDATE))
                self.assertEqual(c['amount_sod'], amount - realized)
                self.assertEqual(c['week_realized_pnl'], 40.25)
                self.assertEqual(c, self.state.account_view(rows[key]['account_id'])['cash_balance'])
        # The leader's row carries exactly the object GET /account serves.
        _, acct = self.get('/account/' + TOKEN)
        self.assertEqual(rows['main']['cash_balance'], acct['account_data']['cash_balance'])
        self.assertEqual(rows['main']['cash_balance'], account_view(self.main)['account_data']['cash_balance'])
        # Another account of the login never leaks into a row.
        self.assertNotIn(1.0, [r['cash_balance']['amount'] for r in rows.values() if r.get('cash_balance')])
        self.assertNotIn('NOT-A-FOLLOWER', json.dumps(body))
        self.assertNotIn(TOKEN, json.dumps(body))
        self.assertNoTradovateRequest()

    def test_present_row_is_the_1091_row_plus_cash_balance_only(self):
        _, rows = self.report()
        for key in ('main', 'f1', 'f2'):
            self.assertEqual(set(rows[key]), ROW_KEYS_1091 | {'cash_balance'}, key)
        # Unavailable follower rows are unchanged.
        self.assertEqual(set(rows['gone']), {'key', 'available', 'reason'})
        self.assertEqual(set(rows['off']), {'key', 'available', 'reason'})
        # With trade_id: the same balance, plus the unchanged trade fields.
        _, with_trade = self.report('?trade_id=t%3Along')
        for key in ('main', 'f1', 'f2'):
            self.assertEqual(with_trade[key]['cash_balance'], rows[key]['cash_balance'])
            self.assertEqual(with_trade[key]['trade'], [])
        self.assertIn('leader', with_trade['main'])
        self.assertNoTradovateRequest()

    def test_present_a_real_zero_balance_stays_zero(self):
        self.page.sync([cash(8, 0.0, realized=0.0)])
        _, rows = self.report()
        self.assertEqual(rows['f1']['cash_balance']['amount'], 0.0)
        self.assertIsNone(rows['f2']['cash_balance'])

    def test_present_live_update_shows_without_resync_and_without_waking_trading_state(self):
        version = self.state.version
        self.state.apply_props({'entityType': 'cashBalance', 'eventType': 'Updated',
                                'entity': cash(9, 3401.75, realized=-54.5,
                                               timestamp='2021-03-10T17:36:00.000Z')})
        self.assertEqual(self.state.version, version)
        _, rows = self.report()
        self.assertEqual((rows['f2']['cash_balance']['amount'], rows['f2']['cash_balance']['realized_pnl'],
                          rows['f2']['cash_balance']['timestamp']), (3401.75, -54.5, '2021-03-10T17:36:00.000Z'))
        self.assertEqual(rows['f1']['cash_balance']['amount'], 2345.0)
        self.assertNoTradovateRequest()

    def test_flag_off_rows_are_exactly_the_1091_rows(self):
        for flag in (False, None):
            with self.subTest(account_view_enabled=flag):
                self.start(self.CASH, account_view_enabled=flag)
                for query in ('', '?trade_id=t%3Along'):
                    _, rows = self.report(query)
                    for key in ('main', 'f1', 'f2'):
                        self.assertEqual(set(rows[key]) - {'trade', 'leader'}, ROW_KEYS_1091, key)
                    self.assertEqual(set(rows['gone']), {'key', 'available', 'reason'})
                self.assertNoTradovateRequest()

    # --- missing ------------------------------------------------------------------------------

    def test_missing_no_cash_rows_reads_null_never_zero(self):
        for rows_in in (None, [], [cash(10, 1.0)]):
            with self.subTest(cash_rows=rows_in):
                self.page.sync(rows_in)
                _, rows = self.report()
                for key in ('main', 'f1', 'f2'):
                    self.assertIn('cash_balance', rows[key])
                    self.assertIsNone(rows[key]['cash_balance'], key)
                    self.assertTrue(rows[key]['synced'])
        self.assertNoTradovateRequest()

    def test_missing_malformed_or_non_numeric_amount_reads_null(self):
        self.page.sync([cash(8, 'NaN-text'), cash(9, None), {'id': 'x', 'accountId': 7, 'amount': 5.0},
                        cash(7, True)])
        _, rows = self.report()
        for key in ('main', 'f1', 'f2'):
            self.assertIsNone(rows[key]['cash_balance'], key)
        self.assertTrue(self.state.is_synced_for('e1'), 'bad account data never unsyncs trading state')

    def test_missing_partial_fields_are_null_not_zero(self):
        row = cash(8, 2345.0)
        for k in ('realizedPnL', 'weekRealizedPnL', 'amountSOD', 'tradeDate', 'timestamp'):
            row.pop(k)
        self.page.sync([row])
        _, rows = self.report()
        self.assertEqual(rows['f1']['cash_balance'],
                         {'amount': 2345.0, 'amount_sod': None, 'realized_pnl': None, 'week_realized_pnl': None,
                          'trade_date': None, 'timestamp': None})

    def test_missing_after_tradovate_deletes_the_row(self):
        self.state.apply_props({'entityType': 'cashBalance', 'eventType': 'Deleted', 'entity': {'id': cash(8, 0)['id']}})
        _, rows = self.report()
        self.assertIsNone(rows['f1']['cash_balance'])
        self.assertEqual(rows['f2']['cash_balance']['amount'], 3456.25)

    # --- stale --------------------------------------------------------------------------------

    def test_stale_unsynced_state_reports_null_not_the_cached_balance(self):
        self.state.mark_unsynced()
        _, rows = self.report()
        for key in ('main', 'f1', 'f2'):
            self.assertFalse(rows[key]['synced'])
            self.assertIsNone(rows[key]['cash_balance'], key)
        # The data is still cached (as /account reports it, with complete false): only the row hides it.
        self.assertEqual(self.state.account_view(8)['cash_balance']['amount'], 2345.0)
        self.assertFalse(account_view(self.main)['account_data']['complete'])

    def test_stale_page_logged_out_or_new_connection_reports_null(self):
        self.page.page_state = 'unauthorized'
        _, rows = self.report()
        self.assertTrue(all(rows[k]['cash_balance'] is None for k in ('main', 'f1', 'f2')))
        self.page.page_state = 'authorized'
        self.page.epoch = 'e2'   # a new connection whose sync has not arrived yet
        _, rows = self.report()
        self.assertTrue(all(rows[k]['cash_balance'] is None for k in ('main', 'f1', 'f2')))
        self.page.sync([cash(8, 2390.5, realized=200.0)], epoch='e2')
        _, rows = self.report()
        self.assertEqual(rows['f1']['cash_balance']['amount'], 2390.5)
        self.assertIsNone(rows['f2']['cash_balance'], 'the previous sync\'s rows are replaced, not kept')

    def test_stale_older_trade_date_never_wins(self):
        self.page.sync([cash(8, 2201.0, day=9), cash(8, 2345.0, day=10), cash(8, 2112.25, day=8)])
        _, rows = self.report()
        self.assertEqual((rows['f1']['cash_balance']['amount'], rows['f1']['cash_balance']['trade_date']['day']),
                         (2345.0, 10))


if __name__ == '__main__':
    unittest.main(verbosity=2)
