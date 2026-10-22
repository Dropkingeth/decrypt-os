"""Soup engine in the loop: real core.Engine decisions, bridge execution, bridge evidence.

Every broker snapshot the bridge produces is applied through Soup's own
core.broker() validation, so any contract mismatch fails here.
"""
import sys
import time
import unittest
from pathlib import Path

from harness import Harness
from soup_inputs import named  # synthetic Soup input names -> the package's own

import os

# The optional engine package is not bundled: point TVBRIDGE_SOUP_PACKAGE at it, otherwise this
# module is skipped.
SOUP = Path(os.environ.get('TVBRIDGE_SOUP_PACKAGE',
                           ''))
if not (SOUP / 'engine' / 'core.py').is_file():
    raise unittest.SkipTest('optional engine package not found; set TVBRIDGE_SOUP_PACKAGE')
for sub in ('engine', 'tests', 'tests/support'):
    sys.path.insert(0, str(SOUP / sub))
import test_pair as soup_fixtures  # noqa: E402
from core import Engine  # noqa: E402
from transport import message  # noqa: E402

from tvbridge.bridge import Bridge  # noqa: E402
from tvbridge.executor import Executor  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402
from tvbridge.protocol import tradovate_symbol  # noqa: E402

CONFIG = soup_fixtures.CONFIG  # the package's dated MNQ contract
SYMBOL = tradovate_symbol(CONFIG['ticker'])  # its Tradovate name
# Synthetic settings for this suite: 5 contracts = TP1 3 + parent runner 1 + EOD runner 1, stop 45,
# TP1 14, break-even +2. Prices are rebased to BASE.
BASE = 10000.0
FIXTURE = {'initial_hard_stop_handles': 45, 'tp1_handles': 14, 'be_offset_handles': 2, 'fixture_int_07': 3,
           'fixture_int_06': 1, 'fixture_int_01': 0, 'fixture_int_04': 1}
PRICE_FIELDS = ('entry_price', 'tp1_price', 'break_even_price', 'runner_cbdr_target', 'initial_hard_stop_price',
                'catastrophe_stop_price', 'effective_pre_tp1_stop_price')


def now_ms():
    return time.time_ns() // 1_000_000


def entry_payload(side, at):
    """The engine fixture's entry with this suite's settings; every price moved onto BASE."""
    p = soup_fixtures.fixture(side, named(soup_fixtures, dict(FIXTURE)), at=at)[0]
    shift = BASE - p['entry_price']
    for k in PRICE_FIELDS:
        if p[k]:
            p[k] += shift
    return p


class SoupInTheLoop(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.fake.contracts[556] = {'id': 556, 'name': SYMBOL}
        self.h.fake.set_price(BASE)
        cfg = self.h.config(ticker=CONFIG['ticker'], soup_account_id=CONFIG['account_id'])
        self.state = AccountState()
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port, page_url_contains='/trader/',
                             trade_url_pattern=cfg['trade_url_pattern'])
        self.bridge.attach(reload=True)
        self.bridge.wait_ready(timeout=20)
        self.ex = Executor(self.bridge, self.state, Journal(cfg['journal_dir']), cfg)
        self.ex.resolve()
        self.engine = Engine(self.h.tmp / 'soup.sqlite', CONFIG)
        self.results = []

    def tearDown(self):
        self.engine.close()
        self.bridge.session.close()
        self.h.close()

    def drain(self):
        """Dispatch every ready Soup command through the bridge, like runtime.Service does."""
        sent = []
        while self.engine.ready():
            c = self.engine.start(self.engine.ready()[0]['id'], now_ms())
            if c is None:
                continue
            payload = message(c, CONFIG['ticker'], self.engine.data['trade']['side'])
            outcome, receipt = self.ex.send(payload)
            self.engine.transport_result(c['id'], outcome, receipt, now_ms())
            sent.append((c['kind'], outcome, receipt.get('reason')))
        return sent

    def evidence(self):
        """Wait for a publishable snapshot and apply it through Soup's own validation."""
        deadline = time.time() + 5
        reason = None
        while time.time() < deadline:
            snap, reason = self.ex.snapshot(self.engine.data['trade']['id'])
            if snap:
                result = self.engine.apply('broker', snap, now_ms())
                self.results.append(result)
                return snap
            time.sleep(0.05)
        self.fail('no snapshot: ' + str(reason))

    def settle(self, rounds=8):
        """Alternate dispatch and evidence until Soup has nothing left to send."""
        log = []
        for _ in range(rounds):
            log += self.drain()
            time.sleep(0.2)
            self.evidence()
            if not self.engine.ready():
                break
        return log

    def test_long_trade_entry_tp1_then_protective_close(self):
        at = (now_ms() // 5000) * 5000
        entry = entry_payload('long', at)
        self.assertEqual(self.engine.apply('pine', entry, now_ms(), entries_enabled=True), 'entry_accepted')
        self.assertIsNotNone(self.engine.data['trade'], 'Soup did not accept the entry')

        log = self.settle()
        kinds = [k for k, _, _ in log]
        self.assertEqual(kinds[:2], ['entry', 'tp1_limit'], log)
        self.assertTrue(all(o == 'accepted' for _, o, _ in log), log)
        qty = entry['quantity']
        self.assertEqual(self.state.net_position(101, 556), qty)
        self.assertEqual(self.engine.data['trade']['remaining'], qty)

        # Price trades through TP1: the limit fills at the exchange, Soup sees it via evidence.
        tp1_cmd = next(c for c in self.engine.data['commands'].values() if c['kind'] == 'tp1_limit')
        self.h.fake.set_price(tp1_cmd['price'])
        time.sleep(0.3)
        self.evidence()
        tp1_cmd = self.engine.data['commands'][tp1_cmd['id']]
        self.assertEqual(tp1_cmd['status'], 'filled')
        self.assertTrue(self.engine.data['trade']['tp1_done'])
        remaining = qty - tp1_cmd['quantity']
        self.assertEqual(self.engine.data['trade']['remaining'], remaining)

        # Market collapses far below every stop: Soup must close the rest.
        crash = BASE - 400
        self.h.fake.set_price(crash)
        # Runner stops apply only after Soup's runner delay.
        deadline = self.engine.data['trade']['runner_not_before'] + 8000
        while now_ms() < deadline:
            t = now_ms()
            self.engine.apply('quote', {'event_id': f'q{t}', 'symbol': CONFIG['ticker'], 'time': t, 'price': crash}, t)
            self.settle(rounds=3)
            if self.engine.data['trade']['phase'] == 'closed':
                break
            time.sleep(0.5)
        self.assertEqual(self.engine.data['trade']['phase'], 'closed', self.engine.data['trade'])
        self.assertEqual(self.state.net_position(101, 556), 0)
        self.assertEqual(self.state.open_orders(101, 556), [])
        self.assertTrue(all(r == 'broker_reconciled' for r in self.results), self.results)
        self.assertTrue(all(o['isAutomated'] is True for o in self.h.fake.orders.values()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
