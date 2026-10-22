"""1.0.6 read-only account view: Tradovate balance/risk data and the opt-in GET /account.

The account data never affects sync validity, trading state or order guards, and the
endpoint answers only when `account_view_enabled` is true.
"""
import hashlib
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from harness import Harness

from tvbridge.bridge import Bridge
from tvbridge.daemon import account_state, account_view, make_handler
from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.state import AccountState

TOKEN = 'account-view-token-0123456789abcdef'
ACCOUNTS = [{'id': 101, 'name': 'DEMO-1'}, {'id': 102, 'name': 'OTHER'}]
EMPTY = {'accounts': ACCOUNTS, 'orders': [], 'fills': [], 'positions': [], 'orderVersions': []}


def cash(account, amount, day=25, cid=None, **extra):
    row = {'id': cid or account * 10 + day, 'accountId': account, 'amount': amount, 'amountSOD': amount - 5.5,
           'realizedPnL': 5.5, 'weekRealizedPnL': 12.25, 'currencyId': 1, 'archived': False,
           'tradeDate': {'year': 2021, 'month': 9, 'day': day}, 'timestamp': '2021-09-%02dT13:00:00Z' % day}
    row.update(extra)
    return row


ACCOUNT_DATA = {
    'cashBalances': [cash(101, 10100.5), cash(102, 1.0)],
    'accountRiskStatuses': [{'id': 101, 'maxNetLiq': 10200.0, 'minNetLiq': 9900.0,
                             'maxNetLiqTimestamp': '2021-09-25T14:00:00Z', 'minNetLiqTimestamp': None}],
    'userAccountAutoLiqs': [{'id': 101, 'trailingMaxDrawdown': 1000.0, 'trailingMaxDrawdownLimit': 9200.0,
                             'trailingMaxDrawdownMode': 'RealTime', 'doNotUnlock': True}],
    'marginSnapshots': [{'id': 101, 'initialMargin': 0.0, 'maintenanceMargin': 0.0, 'autoLiqLevel': 9200.0,
                         'liqOnlyLevel': 0.0, 'totalUsedMargin': 0.0, 'timestamp': '2021-09-25T14:00:00Z',
                         'riskTimePeriodId': 1}],
}


class StateAccountData(unittest.TestCase):
    def synced(self, extra=None):
        s = AccountState()
        s.load_sync(ACCOUNTS, dict(EMPTY, **(extra if extra is not None else ACCOUNT_DATA)), epoch='e1')
        return s

    def test_view_fields(self):
        v = self.synced().account_view(101)
        self.assertEqual(v['cash_balance'], {'amount': 10100.5, 'amount_sod': 10095.0, 'realized_pnl': 5.5,
                                             'week_realized_pnl': 12.25,
                                             'trade_date': {'year': 2021, 'month': 9, 'day': 25},
                                             'timestamp': '2021-09-25T13:00:00Z'})
        self.assertEqual(v['risk_status']['max_net_liq'], 10200.0)
        self.assertIsNone(v['risk_status']['min_net_liq_timestamp'])
        self.assertEqual(v['auto_liq'], {'trailing_max_drawdown': 1000.0, 'trailing_max_drawdown_limit': 9200.0,
                                         'trailing_max_drawdown_mode': 'RealTime'})
        self.assertEqual(v['margin']['auto_liq_level'], 9200.0)
        self.assertIsInstance(v['updated_at'], int)
        # Another account's data never leaks into this view.
        self.assertEqual(self.synced().account_view(102)['cash_balance']['amount'], 1.0)
        self.assertIsNone(self.synced().account_view(102)['risk_status'])

    def test_missing_or_malformed_account_data_never_breaks_sync(self):
        for extra in ({}, {'cashBalances': 'bad'}, {'cashBalances': [None, {'id': 'x'}, {'id': -1}]},
                      {'accountRiskStatuses': [{'no': 'id'}]}):
            with self.subTest(extra=extra):
                s = self.synced(extra)
                self.assertTrue(s.is_synced_for('e1'))
                self.assertIsNone(s.account_view(101)['cash_balance'])
        s = self.synced({'cashBalances': [dict(cash(101, 1.0), amount='NaN-text')]})
        self.assertIsNone(s.account_view(101)['cash_balance'], 'a non-numeric amount is not a balance')

    def test_latest_trade_date_wins(self):
        s = self.synced({'cashBalances': [cash(101, 1.0, day=24), cash(101, 2.0, day=25), cash(101, 0.5, day=23)]})
        self.assertEqual(s.account_view(101)['cash_balance']['amount'], 2.0)

    def test_props_update_delete_and_ignore_malformed(self):
        s = self.synced()
        version = s.version
        s.apply_props({'entityType': 'cashBalance', 'eventType': 'Updated', 'entity': cash(101, 1300.0)})
        self.assertEqual(s.account_view(101)['cash_balance']['amount'], 1300.0)
        self.assertEqual(s.version, version, 'account data must not wake the trading publisher')
        s.apply_props({'entityType': 'cashBalance', 'eventType': 'Updated', 'entity': {'id': 'bad'}})
        s.apply_props({'entityType': 'accountRiskStatus', 'eventType': 'Updated', 'entity': None})
        self.assertTrue(s.is_synced_for('e1'), 'a malformed account-data row never unsyncs trading state')
        s.apply_props({'entityType': 'cashBalance', 'eventType': 'Deleted', 'entity': {'id': cash(101, 0)['id']}})
        self.assertIsNone(s.account_view(101)['cash_balance'])

    def test_failed_resync_revokes_epoch(self):
        s = self.synced()
        with self.assertRaises(ValueError):
            s.load_sync(ACCOUNTS, {'orders': None}, epoch='e2')
        self.assertFalse(s.is_synced_for('e1'))


class AccountEndpoint(unittest.TestCase):
    def start(self, enabled):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.fake.account_data = json.loads(json.dumps(ACCOUNT_DATA))
        cfg = self.h.config(publish_mode='adapter-db', account_view_enabled=enabled, soup_account_id='Account B')
        self.state = AccountState()
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port, page_url_contains='/trader/',
                             trade_url_pattern=cfg['trade_url_pattern'])
        self.addCleanup(self.bridge.detach)
        self.bridge.attach(reload=True)
        self.bridge.wait_ready(timeout=20)
        self.ex = Executor(self.bridge, self.state, Journal(cfg['journal_dir']), cfg)
        self.ex.resolve()
        self.cfg = cfg

        class Pub:
            last = {}
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.ex, Pub(), TOKEN, enabled))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = 'http://127.0.0.1:%d' % server.server_address[1]

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_disabled_by_default(self):
        self.start(False)
        self.assertEqual(self.get('/account/' + TOKEN)[0], 404)
        code, state = self.get('/state/' + TOKEN)
        self.assertEqual(code, 200)
        # Without the flag, /state is exactly the 1.0.4 shape.
        self.assertEqual(set(state), {'account_id', 'contract', 'net_position', 'open_order_ids', 'synced',
                                      'healthy', 'problems', 'unseen_accepted_orders', 'unresolved_commands',
                                      'observed_at'})

    def test_enabled_view_follows_tradovate(self):
        self.start(True)
        self.assertEqual(self.get('/account/' + 'z' * 36)[0], 404)
        code, view = self.get('/account/' + TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual((view['schema'], view['account_id'], view['account_name'], view['soup_account_id']),
                         ('tvbridge.account.v1', 101, 'DEMO-1', 'Account B'))
        self.assertEqual((view['synced'], view['net_position'], view['open_order_ids']), (True, 0, []))
        self.assertTrue(view['account_data']['complete'])
        self.assertEqual(view['account_data']['cash_balance']['amount'], 10100.5)
        self.assertNotIn(TOKEN, json.dumps(view))
        self.assertNotIn('OTHER', json.dumps(view))
        # With the flag, /state carries the binding pins, and the name only as a hash.
        _, state = self.get('/state/' + TOKEN)
        self.assertEqual((state['account_name_sha256'], state['soup_account_id'], state['entries_disabled'],
                          state['publish_mode']),
                         (hashlib.sha256(b'DEMO-1').hexdigest(), 'Account B', False, 'adapter-db'))
        self.assertNotIn('DEMO-1', json.dumps(state))
        # A live cashBalance update from Tradovate shows up without a resync.
        self.h.fake._props('cashBalance', 'Updated', cash(101, 1111.25))
        deadline = time.time() + 5
        while time.time() < deadline and self.get('/account/' + TOKEN)[1]['account_data']['cash_balance'][
                'amount'] != 1111.25:
            time.sleep(0.05)
        self.assertEqual(self.get('/account/' + TOKEN)[1]['account_data']['cash_balance']['amount'], 1111.25)
        # Kill switch visible on both views.
        open(self.cfg['kill_switch_file'], 'w').close()
        self.assertTrue(self.get('/state/' + TOKEN)[1]['entries_disabled'])
        self.assertTrue(self.get('/account/' + TOKEN)[1]['entries_disabled'])
        # Unsynced: the view is never complete, whatever data is cached.
        self.state.mark_unsynced()
        view = account_view(self.ex)
        self.assertFalse(view['synced'])
        self.assertFalse(view['account_data']['complete'])
        self.assertEqual(set(account_state(self.ex, extended=True)) - set(view),
                         {'unseen_accepted_orders', 'account_name_sha256', 'adapter_ids'},
                         'the account view is /state (less the adapter ids) plus the plain name and account data')


if __name__ == '__main__':
    unittest.main(verbosity=2)
