"""Native followers: each follower runs Account A's native exit flow on its own account.
The refused-entry expectations follow the rules in test_follower_native_fixes.py.

- Browser-free: follower config (native flag, default off), firing config (`follower_exits`,
  `follower_native` bounds), Soup trade -> native plan and leader decisions, the kill route,
  the hedge-race exposure wait, the restart fold of `native` journal records, and the synthetic
  break-even watcher.
- End to end in headless Chrome: the native-exits service from the optional engine package
  trades the leader through POST /order on the daemon handler; the firing service
  (follower_exits "native") drives two followers of the same login; the daemon's own follower
  auto-protect threads run; one fake exchange. The optional engine package is located with
  TVBRIDGE_SOUP_NATIVE_PACKAGE (as tests/test_native_exits.py); without it this module is skipped.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import logging

from harness import Harness, seed_market_price
from soup_inputs import engine_db, named  # synthetic Soup input names -> the package's own
from test_firing import LoginDouble, Pub, follower_entry, records  # noqa: F401 (also sets sys.path)

SOUP = Path(os.environ.get('TVBRIDGE_SOUP_NATIVE_PACKAGE',
                           ''))
if not (SOUP / 'engine' / 'core.py').is_file() or 'validate_native_exits' not in (SOUP / 'engine/core.py').read_text():
    raise unittest.SkipTest('optional engine package (native exits) not found; set TVBRIDGE_SOUP_NATIVE_PACKAGE')
# Other suites load a different build of the optional engine package under the same module names:
# import in isolation.
_SOUP_MODULES = ('core', 'transport', 'storage', 'runtime', 'test_pair', 'mechanical_payload_test',
                 'verify_payload_contract')
_saved = {n: sys.modules.pop(n) for n in _SOUP_MODULES if n in sys.modules}
_paths = [str(SOUP / sub) for sub in ('engine', 'tests', 'tests/support')]
sys.path[:0] = _paths
try:
    import test_pair as soup  # noqa: E402
    import core  # noqa: E402
    from core import NATIVE_ID  # noqa: E402
    from runtime import Service  # noqa: E402
    from transport import message  # noqa: E402
    from storage import canonical, digest  # noqa: E402
finally:
    for _n in _SOUP_MODULES:
        sys.modules.pop(_n, None)
    for _p in _paths:
        sys.path.remove(_p)
    sys.modules.update(_saved)

from tvbridge import follower_native, followers  # noqa: E402
from tvbridge.bridge import Bridge  # noqa: E402
from tvbridge.daemon import make_handler  # noqa: E402
from tvbridge.executor import Executor, Refused  # noqa: E402
from tvbridge.firing import ALERTS_NAME, Firing, FiringConfigError, FollowerLane, validate  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.mirror import JOURNAL_NAME  # noqa: E402
from tvbridge.soup_transport import TradovateBridgeTransport  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402
from tvbridge.protocol import tradovate_symbol  # noqa: E402

for _name in ('tvbridge', 'tvbridge.firing', 'tvbridge.mirror'):
    logging.getLogger(_name).addHandler(logging.NullHandler())
    logging.getLogger(_name).propagate = False

CONFIG = soup.CONFIG  # the package's dated MNQ contract
SYMBOL = tradovate_symbol(CONFIG['ticker'])  # its Tradovate name
LEADER, FB, FC, CONTRACT = 101, 118, 116, 556
TOKEN = 'native-follower-token-0123456789abcdef'
POLICY = {'id': 'account-original-allocation-micro-cap-v1', 'account_id': CONFIG['account_id'], 'max_contracts': 10,
          'exit_allocation': 'original',
          'native_binding': {'broker_id': 'b', 'account_value': 'v', 'account_label': 'l', 'account_url_sha256': '0' * 64}}
NATIVE = {'id': NATIVE_ID, 'account_id': CONFIG['account_id'], 'modify_confirm_ms': 2500, 'cancel_confirm_ms': 5000,
          'stop_confirm_ms': 5000, 'backstop_ms': 1500, 'exit_verify_ms': 4000, 'reduce_confirm_ms': 5000,
          'max_exit_attempts': 3}
# Synthetic settings shared by the native-follower suites: long 4 = TP1 1 + parent runner 2 +
# EOD runner 1, stop 18, TP1 26 with its working limit 1.5 inside, break-even +5, prices on BASE.
BASE = 12000.0
FIXTURE = {'initial_hard_stop_handles': 18, 'tp1_handles': 26, 'be_offset_handles': 5, 'fixture_int_07': 1,
           'fixture_int_06': 2, 'fixture_int_01': 0, 'fixture_int_04': 1}
ALLOC = {'tp1': 1, 'parent': 2, 'bias': 0, 'eod': 1}
QTY, AFTER_TP1 = 4, 3
TP1_OFFSET = 1.5
PRICE_FIELDS = ('entry_price', 'tp1_price', 'break_even_price', 'runner_cbdr_target', 'initial_hard_stop_price',
                'catastrophe_stop_price', 'effective_pre_tp1_stop_price')
RESULTS = {}


def entry_payload(side='long', changes=None, at=None):
    """The engine fixture's entry with these settings; every price moved onto BASE."""
    p = soup.fixture(side, named(soup, dict(FIXTURE, **(changes or {}))), **({} if at is None else {'at': at}))[0]
    shift = BASE - p['entry_price']
    for k in PRICE_FIELDS:
        if p[k]:
            p[k] += shift
    return p


def tp1_limit(p):
    """The TP1 working limit: TP1_OFFSET inside the strategy target."""
    return p['tp1_price'] - (TP1_OFFSET if p['side'] == 'long' else -TP1_OFFSET)


def until(fn, timeout=10.0, what='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(what + ' not reached in time')


class LivePub:
    """The daemon publisher's `last` (numbers the leader snapshots in /followers); the pump sets it."""

    def __init__(self):
        self.last = {}


class Recorder:
    def __init__(self, inner):
        self.inner, self.sent = inner, []

    def send(self, payload):
        self.sent.append(canonical(payload))
        return self.inner.send(payload)


# ----------------------------------------------------------------------------------------
# Browser-free
# ----------------------------------------------------------------------------------------

class NativeConfig(unittest.TestCase):
    CFG = {'account_name': 'MAIN', 'account_id': 7, 'ticker': 'MNQZ2021', 'tick_size': .25,
           'journal_dir': '/tmp/fn-cfg/journal', 'kill_switch_file': '/tmp/fn-cfg/ENTRIES-DISABLED',
           'hedge_guard': True, 'max_contracts': 10, 'flatten_settle_s': 3.0}

    def parse(self, **kw):
        return followers.parse(dict(self.CFG, followers=[follower_entry('tb', 'FB', Path('/tmp/fn-cfg'), **kw)]))

    def test_native_flag_is_off_by_default_and_then_exactly_the_mirror_follower(self):
        [(_, off, _)] = self.parse()
        self.assertEqual((off['bracket_orders_enabled'], off['flatten_all_enabled']), (False, False))
        self.assertNotIn('bracket_auto_protect', off)
        self.assertNotIn('native_exits_enabled', off)
        [(_, on, _)] = self.parse(native_exits_enabled=True)
        self.assertEqual((on['bracket_orders_enabled'], on['flatten_all_enabled'], on['bracket_auto_protect'],
                          on['native_exits_enabled'], on['hedge_guard'], on['flatten_settle_s']),
                         (True, True, True, True, True, 3.0))
        self.assertEqual({k: v for k, v in on.items() if k not in ('bracket_orders_enabled', 'flatten_all_enabled',
                                                                 'bracket_auto_protect', 'native_exits_enabled')},
                         {k: v for k, v in off.items() if k not in ('bracket_orders_enabled', 'flatten_all_enabled')})

    def test_native_refusals(self):
        for kw, text in (({'native_exits_enabled': 'yes'}, 'native_exits_enabled'),
                         ({'native_exits_enabled': True, 'flatten_all_enabled': False}, 'flatten_all_enabled')):
            with self.assertRaises(followers.FollowerConfigError) as ctx:
                self.parse(**kw)
            self.assertIn(text, str(ctx.exception))

    def firing_cfg(self, **over):
        c = {'soup_db': '/x/engine.sqlite', 'soup_engine_dir': '/x/engine', 'ticker': 'MNQZ2021',
             'firing_dir': '/y/firing', 'firing_order': ['ta', 'tb', 'tc'],
             'accounts': {'ta': {'role': 'main'}, 'tb': {'role': 'follower'},
                          'tc': {'role': 'follower', 'enabled': False, 'prop_profile': 'unrestricted'}}}
        c.update(over)
        return c

    def test_firing_follower_exits_mode_and_bounds(self):
        self.assertEqual(follower_native.validate_native(self.firing_cfg()), ('mirror', None))
        mode, policy = follower_native.validate_native(self.firing_cfg(follower_exits='native'))
        self.assertEqual((mode, policy), ('native', follower_native.NATIVE_DEFAULTS))
        # The defaults are the leader's native-exits.json policy.
        self.assertEqual(policy, {'modify_confirm_ms': 2500, 'cancel_confirm_ms': 5000, 'stop_confirm_ms': 5000,
                                  'backstop_ms': 2000, 'exit_verify_ms': 5000, 'reduce_confirm_ms': 5000,
                                  'max_exit_attempts': 3})
        self.assertEqual(validate(self.firing_cfg(follower_exits='native', follower_native={'exit_verify_ms': 3000})),
                         ('ta', ['tb']))
        for bad in ({'follower_exits': 'copy'}, {'follower_native': {}},
                    {'follower_exits': 'native', 'follower_native': {'max_exit_attempts': 9}},
                    {'follower_exits': 'native', 'follower_native': {'backstop_ms': 1.5}},
                    {'follower_exits': 'native', 'follower_native': {'modify_ms': 1000}}):
            with self.assertRaises(FiringConfigError, msg=bad):
                validate(self.firing_cfg(**bad))

    def test_example_configs(self):
        root = Path(__file__).resolve().parent.parent
        cfg = json.loads((root / 'firing.example.json').read_text())
        self.assertEqual(validate(cfg), ('account-a', []))
        self.assertEqual(follower_native.validate_native(cfg)[0], 'native')


def native_trade(side='long', native=True, **over):
    p = entry_payload(side)
    t = {'id': p['trade_id'], 'side': side, 'phase': 'running', 'payload': p,
         'alloc': dict(ALLOC), 'quantity_policy': dict(POLICY),
         'execution_policy': {'tp1_working_limit': tp1_limit(p)},
         'reductions': {}, 'terminal_reason': None}
    if native:
        t['native'] = {'mode': 'native'}
    t.update(over)
    return t


class NativePlanAndDecisions(unittest.TestCase):
    def test_plan_only_for_trades_soup_pinned_native(self):
        self.assertIsNone(follower_native.native_plan(native_trade(native=False)))
        t = native_trade()
        plan = follower_native.native_plan(t)
        self.assertEqual((plan['initial'], plan['break_even'], plan['tp1_limit'], plan['alloc'], plan['chart_ms']),
                         (11982.0, 12005.0, 12024.5, ALLOC, 5000))
        t['payload'] = dict(t['payload'], initial_hard_stop_enabled=False)
        self.assertIsNone(follower_native.native_plan(t))
        self.assertIsNone(follower_native.native_plan(native_trade(quantity_policy={'exit_allocation': 'tp1_only'})))

    def test_leader_view_reads_decisions_never_stop_commands(self):
        t = native_trade(reductions={'eod': 'cbdr'})
        commands = {'a': {'kind': 'modify_stop', 'started_at': 10, 'alloc': {}},
                    'b': {'kind': 'reduce', 'started_at': 20, 'created_at': 15, 'alloc': {'parent': 1},
                          'reason': 'parent_sweep'},
                    'c': {'kind': 'cancel', 'started_at': 30, 'alloc': {}}}
        v = follower_native.leader_view(t, commands, 99)
        self.assertEqual(v['decisions'], {'parent': ('parent_sweep', 15), 'eod': ('cbdr', 99)})
        self.assertFalse(v['flatten'])
        v = follower_native.leader_view(dict(t, terminal_reason='eod'),
                                        dict(commands, d={'kind': 'flatten', 'started_at': 40, 'alloc': {}}), 99)
        self.assertEqual((v['terminal_reason'], v['flatten']), ('eod', True))

    def test_restart_fold_rebuilds_the_trade_state(self):
        plan = follower_native.native_plan(native_trade())
        recs = [{'event': 'start', 'trade_id': 't', 'plan': plan, 'entry_cid': 'soup-e', 'quantity': QTY, 'at': 1},
                {'event': 'tp1', 'trade_id': 't', 'command_id': 'native-tp1:t', 'at': 2},
                {'event': 'stop_confirmed', 'trade_id': 't', 'at': 3},
                {'event': 'modify', 'trade_id': 't', 'command_id': 'native-stop:t:2', 'quantity': AFTER_TP1,
                 'price': 12005.0, 'at': 4},
                {'event': 'modify_confirmed', 'trade_id': 't', 'command_id': 'native-stop:t:2', 'at': 5},
                {'event': 'reduce', 'trade_id': 't', 'command_id': 'native-reduce:t:4', 'quantity': 2,
                 'tranches': {'parent': 2}, 'at': 6},
                {'event': 'reduce_filled', 'trade_id': 't', 'tranches': {'parent': 2}, 'at': 7},
                {'event': 'exit', 'trade_id': 't', 'reason': 'leader:eod', 'at': 8},
                {'event': 'exit_attempt', 'trade_id': 't', 'command_id': 'native-exit:t:1', 'at': 9}]
        st = None
        for r in recs:
            st = follower_native.NativeFollower._native_apply(st, r)
        self.assertEqual((st['tp1_cid'], st['stop_confirmed'], st['modify']['confirmed'], st['reduced']['parent'],
                          st['reduce'], st['exit']['reason'], st['exit']['attempts'], st['n'], st['done']),
                         ('native-tp1:t', True, True, 2, None, 'leader:eod', ['native-exit:t:1'], 4, False))


class Bare(follower_native.NativeFollower):
    """The mixin alone, for the watcher logic."""

    def __init__(self):
        self.recs = []
        self.market = {}
        self.key = 'tb'

    def clock_ms(self):
        return 0

    def _native_record(self, st, event, **detail):
        self.recs.append(dict(detail, event=event))
        return follower_native.NativeFollower._native_apply(st, dict(detail, event=event, trade_id='t', at=0))


class Watchers(unittest.TestCase):
    def state(self, mode='native'):
        plan = follower_native.native_plan(native_trade())
        st = follower_native.NativeFollower._native_apply(None, {'event': 'start', 'trade_id': 't', 'plan': plan,
                                                                 'entry_cid': 'e', 'quantity': QTY, 'at': 1000})
        st['mode'] = mode
        return st

    def test_native_backstop_before_and_after_the_own_tp1(self):
        b, st = Bare(), self.state()
        b.market = {'quote': {'time': 2000, 'price': 11991.0}}
        b._native_crossings(st, QTY, False, None, 2000)
        self.assertIsNone(st['backstop'])
        b.market = {'quote': {'time': 2100, 'price': 11982.0}}
        b._native_crossings(st, QTY, False, None, 2100)
        self.assertEqual(st['backstop'], {'reason': 'initial_hard_stop', 'at': 2100})
        st2 = self.state()
        b.market = {'quote': {'time': 3000, 'price': 12005.0}}  # break-even, before the own TP1 fill time
        b._native_crossings(st2, AFTER_TP1, True, 3500, 3000)
        self.assertIsNone(st2['backstop'])
        b.market = {'bar': {'open_time': 4000, 'close_time': 5000, 'low': 12004.0, 'high': 12030.0, 'close': 12026.0}}
        b._native_crossings(st2, AFTER_TP1, True, 3500, 5000)
        self.assertEqual(st2['backstop'], {'reason': 'break_even', 'at': 5000})

    def test_synthetic_break_even_waits_for_the_runner_delay(self):
        b, st = Bare(), self.state('synthetic')
        tp1_at = 10200  # 5 s bars: runner_not_before = (10200 // 5000 + 2) * 5000 = 20000
        b.market = {'quote': {'time': 19000, 'price': 12004.0}}
        b._native_crossings(st, AFTER_TP1, True, tp1_at, 19000)
        self.assertFalse(st.get('synthetic_decisions'))
        b.market = {'quote': {'time': 20001, 'price': 12004.0}}
        b._native_crossings(st, AFTER_TP1, True, tp1_at, 20001)
        self.assertEqual(st['synthetic_decisions'], {'parent': ('break_even', 20001), 'eod': ('break_even', 20001)})
        st3 = self.state('synthetic')
        b.market = {'quote': {'time': 2000, 'price': 11981.0}}
        b._native_crossings(st3, QTY, False, None, 2000)
        self.assertEqual(st3['synthetic_exit'], 'initial_hard_stop')


class ExposureWait(unittest.TestCase):
    """A bracket stop reported a moment before its entry's position must not trip a follower's hedge guard."""

    class Report:
        def __init__(self, data):
            self.data = data

        def get(self, trade_id=None):
            return self.data

    def lane(self, data, native=True, peers=('tc',)):
        lane = FollowerLane.__new__(FollowerLane)
        lane.report, lane.peers = self.Report(data), tuple(peers)
        lane.native_policy = dict(follower_native.NATIVE_DEFAULTS) if native else None
        return lane

    def test_opposite_single_order_above_the_reported_position_waits(self):
        stop = {'action': 'Sell', 'qty': QTY, 'type': 'STP'}
        tp1 = {'action': 'Sell', 'qty': 1, 'type': 'LMT', 'tp1': True}
        early = {'main': {'net_position': 0, 'orders': [stop]}, 'followers': [{'key': 'tc', 'net_position': 0}]}
        later = {'main': {'net_position': QTY, 'orders': [stop, tp1]}, 'followers': [{'key': 'tc', 'net_position': QTY,
                                                                                    'orders': [stop, tp1]}]}
        self.assertFalse(self.lane(early)._peers_clear('long'))
        self.assertTrue(self.lane(later)._peers_clear('long'))
        self.assertTrue(self.lane(early, native=False)._peers_clear('long'))  # mirror-mode rule unchanged
        self.assertTrue(self.lane(early)._peers_clear('short'))  # a Sell never blocks a short entry
        self.assertFalse(self.lane({'main': {}, 'followers': [{'key': 'tc', 'net_position': -1}]})._peers_clear('long'))


class HedgeRaceRetry(unittest.TestCase):
    """Two followers fire in parallel: one follower's guard can see the other's fresh bracket stop
    before its position. A guard refusal sends nothing, so the lane retries once after the exposure
    clears; any other refusal, or a second refusal, skips the trade."""

    class Transport:
        def __init__(self, replies):
            self.replies, self.sent = list(replies), []

        def send(self, payload):
            self.sent.append(payload)
            return self.replies.pop(0)

    class Report:
        def __init__(self):
            self.calls = 0

        def get(self, trade_id=None):
            self.calls += 1
            held = QTY if self.calls > 2 else 0  # the peer's position arrives on the third read
            return {'main': {'net_position': QTY, 'orders': []},
                    'followers': [{'key': 'tc', 'net_position': held,
                                   'orders': [{'action': 'Sell', 'qty': QTY, 'type': 'STP'}]}]}

    def lane(self, replies):
        tmp = Path(tempfile.mkdtemp(prefix='fn-retry-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        (tmp / 'soup').mkdir()
        cfg = {'soup_db': str(tmp / 'soup' / 'engine.sqlite'), 'soup_engine_dir': str(SOUP / 'engine'),
               'ticker': CONFIG['ticker'], 'mirror_dir': str(tmp / 'tb'), 'bridge_url': 'http://127.0.0.1:9'}
        alerts = __import__('tvbridge.firing', fromlist=['AlertLog']).AlertLog(tmp)
        lane = FollowerLane(cfg, TOKEN, 'tb', alerts=alerts, transport=self.Transport(replies),
                            bridge_state=object(), soup=(message, digest), peers=['tc'], report=self.Report(),
                            native_policy=dict(follower_native.NATIVE_DEFAULTS))
        self.addCleanup(lane.close)
        return lane, alerts

    def entry(self, lane):
        t = native_trade()
        c = {'id': 'soup-entry-1', 'trade_id': t['id'], 'kind': 'entry', 'quantity': QTY, 'price': BASE,
             'started_at': lane.clock_ms()}
        info = dict(FollowerLane._trade_info(lane, t), symbol=CONFIG['ticker'])
        lane._native_entry(info, c)
        return t['id'], lane.nat[t['id']]

    GUARD = ('rejected', {'bridge': 'refused', 'reason': 'hedge guard: long MNQ entry refused; ...'})

    def test_guard_refusal_is_retried_once_after_the_exposure_clears(self):
        lane, alerts = self.lane([self.GUARD, ('accepted', {'broker_order_id': '1'})])
        tid, st = self.entry(lane)
        ids = [p['extras']['soup_command_id'] for p in lane.transport.sent]
        self.assertEqual(ids, ['soup-entry-1', 'soup-entry-1:retry'])
        self.assertEqual((st['entry_cid'], st['done']), ('soup-entry-1:retry', False))
        self.assertGreaterEqual(lane.report.calls, 3)
        self.assertEqual(alerts.recent, [])  # the race resolved: nothing to tell the operator
        rebuilt = None
        for rec in follower_native.NativeLog(lane.journal.path).by_trade[tid]:
            rebuilt = follower_native.NativeFollower._native_apply(rebuilt, rec)
        self.assertEqual(rebuilt['entry_cid'], 'soup-entry-1:retry')  # a restart knows the retry

    def test_second_refusal_or_another_reason_skips_the_trade(self):
        # A refused follower entry is skipped and noted once (info), never a fault.
        lane, alerts = self.lane([self.GUARD, self.GUARD])
        _, st = self.entry(lane)
        self.assertEqual((st['done'], st['skipped']), (True, 'entry_refused'))
        self.assertEqual([(a['kind'], a['level']) for a in alerts.recent], [('follower_entry_skipped', 'info')])
        lane, alerts = self.lane([('rejected', {'bridge': 'refused', 'reason': 'entries disabled by kill switch'})])
        _, st = self.entry(lane)
        self.assertEqual((len(lane.transport.sent), st['done']), (1, True))
        self.assertEqual([(a['kind'], a['level'], a['refusal']) for a in alerts.recent],
                         [('follower_entry_skipped', 'info', 'entries disabled by kill switch')])


class KillRoute(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = AccountState()
        self.page = LoginDouble(self.state)
        self.cfg = {'ticker': 'MNQZ2021', 'account_name': 'MAIN-ACCT', 'account_id': 7, 'soup_account_id': 'Account A',
                    'max_contracts': 10, 'tick_size': .25, 'journal_dir': str(self.tmp / 'journal'),
                    'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED'), 'publish_mode': 'adapter-db',
                    'followers': [follower_entry('f1', 'F-ONE', self.tmp, native_exits_enabled=True),
                                  follower_entry('f2', 'F-TWO', self.tmp),
                                  follower_entry('gone', 'NOT-IN-THIS-LOGIN', self.tmp)]}
        self.main = Executor(self.page, self.state, Journal(self.cfg['journal_dir']), self.cfg)
        self.main.resolve()
        self.fx = followers.build(self.page, self.state, self.cfg, self.main)
        server = ThreadingHTTPServer(('127.0.0.1', 0), followers.make_follower_handler(
            make_handler(self.main, Pub(), TOKEN, True), self.fx, TOKEN, self.main))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = 'http://127.0.0.1:%d' % server.server_address[1]

    def post(self, path, body):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_kill_sets_only_that_followers_switch(self):
        self.assertEqual(self.post(f'/follower/{TOKEN}/f1/kill', {'reason': 'native exit-all unverified'}),
                         (200, {'kill_switch': True, 'entries_disabled': True, 'follower': 'f1'}))
        self.assertTrue(Path(self.cfg['followers'][0]['kill_switch_file']).exists())
        self.assertFalse(Path(self.cfg['followers'][1]['kill_switch_file']).exists())
        self.assertFalse(Path(self.cfg['kill_switch_file']).exists())  # never the leader's
        self.assertEqual(self.post(f'/follower/{TOKEN}/f1/kill', {'reason': ''})[0], 400)
        self.assertEqual(self.post(f'/follower/{TOKEN}/f1/kill', {'why': 'x'})[0], 400)
        self.assertEqual(self.post(f'/follower/{TOKEN}/gone/kill', {'reason': 'x'})[0], 503)
        self.assertEqual(self.post(f'/follower/{"x" * len(TOKEN)}/f1/kill', {'reason': 'x'})[0], 404)
        self.assertEqual(self.post(f'/kill/{TOKEN}', {'reason': 'x'})[0], 404)
        self.assertEqual(self.page.sent, [])  # nothing reaches Tradovate

    def test_rows_report_native_and_order_side_and_only_native_followers_get_housekeeping(self):
        summary = followers.summary(self.fx, self.main)
        rows = {r['key']: r for r in summary['followers']}
        self.assertEqual((rows['f1']['native_exits'], rows['f2']['native_exits'], summary['main']['native_exits']),
                         (True, False, False))
        keeper = followers.Housekeeping(self.fx, self.state)
        self.assertEqual([t.name for t in keeper.threads], ['follower-protect-f1'])
        self.assertEqual((self.fx['f1'].bracket_protect, self.fx['f2'].bracket_protect, self.main.bracket_protect),
                         (True, False, False))
        keeper.start().stop()
        self.assertEqual([executor.key for executor in followers.executors(self.fx)], ['f1', 'f2'])


# ----------------------------------------------------------------------------------------
# End to end: leader Account A native (Soup service) + native followers B and C.
# ----------------------------------------------------------------------------------------

class NativeFiringE2E(unittest.TestCase):
    FOLLOWERS = (('tb', 'ACCOUNT-B', FB, {}), ('tc', 'ACCOUNT-C', FC, {}))
    FIRING = {}
    START_FIRING = True
    WITH_FOLLOWERS = True

    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.fake = self.h.fake
        self.fake.contracts[CONTRACT] = {'id': CONTRACT, 'name': SYMBOL}
        self.fake.set_price(BASE)
        for _, name, acct, _ in self.FOLLOWERS:
            self.fake.accounts.append({'id': acct, 'name': name, 'userId': 7, 'active': True})
        self.ny_clock(11, 0, 0)
        self.state = AccountState()
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port, page_url_contains='/trader/',
                             trade_url_pattern=r'^ws://127\.0\.0\.1:\d+/v1/websocket$')
        self.addCleanup(self.bridge.detach)
        self.bridge.attach(reload=True)
        self.bridge.wait_ready(timeout=20)
        tmp = self.h.tmp
        extra = {}
        if self.WITH_FOLLOWERS:
            extra['followers'] = [follower_entry(k, name, tmp, native_exits_enabled=True, **kw)
                                  for k, name, _, kw in self.FOLLOWERS]
        self.cfg = self.h.config(ticker=CONFIG['ticker'], soup_account_id=CONFIG['account_id'], max_contracts=10,
                                 journal_dir=str(tmp / 'journal-main'), bracket_orders_enabled=True,
                                 flatten_all_enabled=True, **extra)
        self.main = Executor(self.bridge, self.state, Journal(self.cfg['journal_dir']), self.cfg, clock_ms=self.now)
        self.main.resolve()
        self.pub = LivePub()
        handler = make_handler(self.main, self.pub, TOKEN)
        self.fx = {}
        if self.WITH_FOLLOWERS:
            self.fx = followers.build(self.bridge, self.state, self.cfg, self.main, clock_ms=self.now)
            handler = followers.make_follower_handler(handler, self.fx, TOKEN, self.main, publisher=self.pub)
            keeper = followers.Housekeeping(self.fx, self.state, interval_s=0.2).start()  # the daemon's threads
            self.addCleanup(keeper.stop)
        for executor in [self.main, *self.fx.values()]:  # bracket entries need a known market price
            seed_market_price(executor, BASE)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.bridge_url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        # Leader: the engine's native-exits service, POST /order to the daemon handler.
        self.workspace = tmp / 'soup'
        self.workspace.mkdir()
        (self.workspace / 'control.json').write_text('{"enabled":true,"locked":false}')
        (tmp / 'policy.json').write_text(json.dumps(POLICY))
        (tmp / 'pairing.json').write_text(json.dumps(CONFIG))
        self.leader_transport = Recorder(TradovateBridgeTransport(self.bridge_url, TOKEN))
        self.service = Service(self.workspace, CONFIG, self.leader_transport, tp1_limit_offset_handles=TP1_OFFSET,
                               quantity_policy=POLICY, native_exits=NATIVE)
        self.soup_db = engine_db(self.workspace)
        self.soup_lock = threading.RLock()
        self.feed_soup = threading.Event()  # cleared: snapshots are built and numbered but Soup gets none
        self.feed_soup.set()
        self.pump_stop = threading.Event()
        self.pump_errors = []
        self.pump_thread = threading.Thread(target=self.pump, daemon=True)
        self.pump_thread.start()
        self.addCleanup(self.close_soup)
        self.firing = None
        if self.START_FIRING and self.WITH_FOLLOWERS:
            self.start_firing()

    # --- plumbing ----------------------------------------------------------------------------

    def firing_cfg(self):
        tmp = self.h.tmp
        cfg = {'soup_db': str(self.soup_db), 'soup_engine_dir': str(SOUP / 'engine'),
               'soup_quantity_policy': str(tmp / 'policy.json'), 'soup_pairing': str(tmp / 'pairing.json'),
               'ticker': CONFIG['ticker'], 'bridge_url': self.bridge_url, 'firing_dir': str(tmp / 'firing'),
               'firing_order': ['ta'] + [k for k, *_ in self.FOLLOWERS],
               'accounts': dict({'ta': {'role': 'main'}},
                                **{k: {'role': 'follower', 'prop_profile': 'unrestricted'} for k, *_ in self.FOLLOWERS}),
               'reconcile_grace_ms': 300, 'reconcile_settle_ms': 300, 'reconcile_deadline_ms': 20000,
               'follower_exits': 'native', 'follower_native': {'exit_verify_ms': 1500, 'backstop_ms': 1500}}
        cfg.update(self.FIRING)
        return cfg

    def start_firing(self):
        self.firing = Firing(self.firing_cfg(), TOKEN, clock_ms=self.now, soup=(message, digest))
        self.firing_thread = threading.Thread(target=self.firing.run, daemon=True)
        self.firing_thread.start()
        until(lambda: all(lane.baselined for lane in self.firing.lanes.values()) and self.firing.grid.baselined,
              5, 'lanes baselined')

    def stop_firing(self):
        if self.firing is not None:
            self.firing.stopping.set()
            self.firing_thread.join(30)
            self.firing.close()
            self.firing = None

    def close_soup(self):
        self.stop_firing()
        self.pump_stop.set()
        self.pump_thread.join(10)
        self.service.close()

    def now(self):
        return self.fake.now_ms()

    def ny_clock(self, hour, minute, second):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        real = time.time_ns() // 1_000_000
        target = datetime.fromtimestamp(real / 1000, ZoneInfo('America/New_York')).replace(
            hour=hour, minute=minute, second=second, microsecond=0)
        self.fake.skew_ms = int(target.timestamp() * 1000) - real

    def soup(self, channel, p):
        """One message through Soup's inbox, processed at once (serialized with the pump)."""
        with self.soup_lock:
            now = self.now()
            seq = self.service.enqueue(channel, canonical(p).encode(), now)
            self.service.process_one(now)
            with self.service.mutex:
                return self.service.engine.db.execute('SELECT status, detail FROM inbox WHERE seq=?', (seq,)).fetchone()

    def pump(self):
        """The live loop around Soup: leader auto-protect + evidence (the daemon publisher), Soup ticks."""
        while not self.pump_stop.is_set():
            try:
                with self.main.lock:
                    self.main.maintain_brackets()
                t = self.trade
                if t and t['phase'] != 'closed':
                    snap, _ = self.main.snapshot(t['id'])
                    if snap:
                        self.pub.last = {'published': 'adapter_db', 'sequence': snap['sequence']}
                        if self.feed_soup.is_set():
                            self.soup('broker', snap)
                with self.soup_lock:
                    with self.service.execution:
                        with self.service.mutex:
                            self.service.engine.tick(self.now())
                        self.service.dispatch(self.now())
            except Exception as exc:  # noqa: BLE001 - reported by the tests
                self.pump_errors.append(repr(exc))
            self.pump_stop.wait(0.05)

    @property
    def trade(self):
        with self.service.mutex:
            t = self.service.engine.data['trade']
            return json.loads(json.dumps(t)) if t else None

    def soup_commands(self, kind=None):
        with self.service.mutex:
            cs = json.loads(json.dumps(self.service.engine.data['commands']))
        return [c for c in cs.values() if kind is None or c['kind'] == kind]

    # --- scenario helpers ---------------------------------------------------------------------

    def enter(self, side='long'):
        at = self.now() // 1000 * 1000
        p = entry_payload(side, {'chart_seconds': 1, 'time': at - 1000}, at=at)
        self.assertEqual(self.soup('pine', p), ('processed', 'entry_accepted'))
        self.p = p
        return p

    def net(self, acct):
        return self.state.net_position(acct, CONTRACT)

    def keys(self):
        return [k for k, *_ in self.FOLLOWERS]

    def acct(self, key):
        return {k: a for k, _, a, _ in self.FOLLOWERS}.get(key, LEADER)

    def journal(self, key):
        return self.main.journal if key == 'ta' else self.fx[key].journal

    def entry_rec(self, key, tid=None):
        tid = tid or self.p['trade_id']
        return next((c for c in self.journal(key).for_trade(tid) if c['kind'] == 'bracket_entry'
                     and c['outcome'] == 'accepted'), None)

    def stop_id(self, key, tid=None):
        e = self.entry_rec(key, tid)
        return int(e['legs']['stop']) if e else None

    def tp1_id(self, key, tid=None):
        tid = tid or self.p['trade_id']
        kinds = ('tp1_limit',)
        c = next((c for c in self.journal(key).for_trade(tid) if c['kind'] in kinds and c.get('broker_order_id')), None)
        return int(c['broker_order_id']) if c else None

    def stop_values(self, key):
        v = self.fake.versions.get(self.stop_id(key)) or {}
        return v.get('orderQty'), v.get('stopPrice'), (self.fake.orders.get(self.stop_id(key)) or {}).get('ordStatus')

    def native(self, key, tid=None):
        return self.firing.lanes[key].nat.get(tid or self.p['trade_id'])

    def running(self, side='long', keys=None):
        p = self.enter(side)
        sign = 1 if side == 'long' else -1
        until(lambda: self.net(LEADER) == QTY * sign and (self.trade.get('native') or {}).get('stop_confirmed'), 10,
              'leader bracket running')
        for key in self.keys() if keys is None else keys:
            try:
                until(lambda: self.net(self.acct(key)) == QTY * sign and self.native(key) is not None
                      and self.native(key)['stop_confirmed'] and self.tp1_id(key) is not None
                      and self.fake.orders[self.tp1_id(key)]['ordStatus'] == 'Working', 10, key + ' bracket running')
            except AssertionError:
                raise AssertionError('%s bracket running not reached: net %s native %s lane %s alerts %s status %s' % (
                    key, self.net(self.acct(key)), self.native(key),
                    [(r['type'], r.get('command_id'), r.get('reason'), r.get('outcome')) for r in self.lane_records(key)],
                    self.alerts(), self.firing.lanes[key].status.get('last_error')))
        return p

    def at_break_even(self, key, timeout=6):
        until(lambda: self.stop_values(key)[:2] == (AFTER_TP1, self.p['break_even_price']), timeout,
              key + ' at break-even')

    def lane_records(self, key):
        return records(self.h.tmp / 'firing' / key / JOURNAL_NAME)

    def final(self, key, tid=None, timeout=20):
        tid = tid or self.p['trade_id']
        return until(lambda: next((r for r in self.lane_records(key) if r['type'] == 'reconcile'
                                   and r['trade_id'] == tid and r['final']), None), timeout, 'final ' + key)

    def alerts(self):
        return records(self.h.tmp / 'firing' / ALERTS_NAME)

    def assert_flat(self, acct):
        until(lambda: self.net(acct) == 0, 8, f'account {acct} flat')
        until(lambda: not [o for o, v in self.fake.orders.items() if v['accountId'] == acct
                           and v['ordStatus'] in ('PendingNew', 'Working', 'Suspended')], 8, f'{acct} nothing working')

    def assert_never_opposite(self, acct, side='long'):
        sign = 1 if side == 'long' else -1
        path = [n for a, _, n in self.fake.position_history if a == acct]
        self.assertTrue(all(sign * n >= 0 for n in path), (acct, path))

    def requests(self, acct, since=0):
        """(endpoint, body) of every order request for one account."""
        out = []
        for endpoint, body in self.fake.bodies[since:]:
            owner = body.get('accountId')
            if owner is None and body.get('orderId') in self.fake.orders:
                owner = self.fake.orders[body['orderId']]['accountId']
            if owner == acct:
                out.append((endpoint, body))
        return out

    def fill_now(self, oid, price=None):
        with self.fake.lock:
            v = self.fake.versions[oid]
            self.fake._fill(oid, price if price is not None else (v.get('stopPrice') or v.get('price')))

    def eod(self):
        decision = self.trade['payload']['execution_context']['decision_time']
        until(lambda: self.now() // 1000 * 1000 - 1000 >= decision, 5, 'a bar after the entry')
        with mock.patch.object(core, 'in_session', return_value=True):
            until(lambda: self.trade['terminal_reason'] == 'eod', 5, 'Soup EOD')

    def leader_closed(self, timeout=15):
        until(lambda: self.trade['phase'] == 'closed', timeout, 'leader closed')
        self.assert_flat(LEADER)

    def tearDown(self):
        self.assertEqual(self.pump_errors, [])


class Together(NativeFiringE2E):
    """B and C together: bracket, own TP1 -> break-even, break-even stop -> exit-all, all flat."""

    def test_16_and_18_follow_the_full_native_cycle(self):
        mark = len(self.fake.bodies)
        p = self.running()
        tid = p['trade_id']
        for key in self.keys():
            reqs = self.requests(self.acct(key), mark)
            oso = next(b for e, b in reqs if e == 'order/placeoso')
            self.assertEqual((oso['orderQty'], oso['bracket1']['orderType'], oso['bracket1']['stopPrice'], oso['action']),
                             (QTY, 'Stop', p['initial_hard_stop_price'], 'Buy'))
            tp1 = next(b for e, b in reqs if e == 'order/placeorder')
            self.assertEqual((tp1['orderType'], tp1['orderQty'], tp1['price']), ('Limit', 1, tp1_limit(p)))
            self.assertEqual(self.stop_values(key), (QTY, p['initial_hard_stop_price'], 'Working'))
        self.fake.set_price(tp1_limit(p))  # every account's TP1 fills
        t_fill = time.monotonic()
        for key in ['ta'] + self.keys():
            self.at_break_even(key)
        for key in self.keys():
            be = next(i for i, (e, b) in enumerate(self.fake.bodies) if e == 'order/modifyorder' and
                      b['orderId'] == self.stop_id(key) and b.get('stopPrice') == p['break_even_price'])
            RESULTS.setdefault('follower_tp1_fill_to_be_modify_ms', []).append(
                round((self.fake.body_times[be] - t_fill) * 1000))
        self.fake.set_price(p['break_even_price'])  # every break-even stop fills
        self.leader_closed()
        for key in self.keys():
            final = self.final(key)
            self.assertEqual(final['result'], 'flat', final)
            self.assertIn(final['exit_reason'], ('broker_stop_break_even', 'leader:broker_stop_break_even'))
            self.assert_flat(self.acct(key))
            self.assert_never_opposite(self.acct(key))
            self.assertEqual(self.fx[key].journal.unresolved(), [])
            ids = [c['command_id'] for c in self.fx[key].journal.for_trade(tid)]
            self.assertTrue(ids and all(i.startswith(f'f:{key}:') for i in ids), ids)
            # Soup's own stop commands were never sent to a follower.
            soup_modifies = {c['id'] for c in self.soup_commands('modify_stop')}
            self.assertTrue(soup_modifies)
            sent = {r['command_id'] for r in self.lane_records(key) if r['type'] == 'intent'}
            self.assertFalse(sent & soup_modifies)
            self.assertEqual({r['reason'] for r in self.lane_records(key) if r.get('command_id') in soup_modifies},
                             {'native_leader_only'})
        self.assert_never_opposite(LEADER)
        # The leader journal holds only Soup's own ids (plus its own bracket sub-commands).
        soup_ids = {json.loads(x)['extras']['soup_command_id'] for x in self.leader_transport.sent}
        self.assertTrue({c['command_id'].split('#')[0] for c in self.main.journal.for_trade(tid)} <= soup_ids)
        with self.fake.lock:
            tags = [self.fake.versions[o]['text'] for o in self.fake.orders if self.fake.versions[o].get('text')
                    and self.fake.versions[o]['text'] != 'Exit']
        self.assertEqual(len(tags), len(set(tags)))
        until(lambda: self.firing.update_grid() is not None and
              'flat' in [e['kind'] for e in self.firing.grid.status()['recent']], 10, 'grid ALL FLAT')
        self.assertFalse([a for a in self.alerts() if a['kind'] not in ()])  # a clean cycle raises no alert
        self.firing.write_status()
        status = json.loads((self.h.tmp / 'firing' / 'status.json').read_text())
        self.assertEqual(status['follower_exits'], 'native')
        self.assertTrue(status['lanes']['tb']['native']['trades'][tid]['done'])


class TP1Timing(NativeFiringE2E):
    FOLLOWERS = (('tb', 'ACCOUNT-B', FB, {}), ('tc', 'ACCOUNT-C', FC, {}))

    def test_follower_tp1_before_the_leader_moves_only_that_follower(self):
        p = self.running()
        self.fake.set_price(tp1_limit(p) - 0.25)  # above break-even, one tick short of every TP1 limit
        self.fake.fill_limit(self.tp1_id('tb'), 1)
        self.at_break_even('tb')
        time.sleep(1.0)
        self.assertEqual(self.stop_values('ta')[:2], (QTY, p['initial_hard_stop_price']))
        self.assertEqual(self.stop_values('tc')[:2], (QTY, p['initial_hard_stop_price']))
        self.assertFalse(self.trade['tp1_done'])
        self.fake.set_price(tp1_limit(p))  # the leader and C fill TP1 now
        for key in ('ta', 'tc'):
            self.at_break_even(key)
        self.eod()
        self.leader_closed()
        for key in self.keys():
            self.assertEqual((self.final(key)['result'], self.final(key)['exit_reason']), ('flat', 'leader:eod'))
            self.assert_flat(self.acct(key))

    def test_follower_tp1_after_the_leader_waits_for_its_own_fill(self):
        p = self.running()
        self.fake.set_price(tp1_limit(p) - 0.25)  # above break-even, one tick short of every TP1 limit
        self.fake.fill_limit(self.tp1_id('ta'), 1)
        self.at_break_even('ta')
        until(lambda: self.trade['tp1_done'], 5, 'Soup TP1 done')
        time.sleep(1.0)
        for key in self.keys():  # the leader's TP1 moves nothing on a follower
            self.assertEqual(self.stop_values(key)[:2], (QTY, p['initial_hard_stop_price']))
            self.assertEqual(self.fake.orders[self.tp1_id(key)]['ordStatus'], 'Working')
        self.fake.fill_limit(self.tp1_id('tb'), 1)
        self.at_break_even('tb')
        self.assertEqual(self.stop_values('tc')[:2], (QTY, p['initial_hard_stop_price']))
        self.fake.set_price(p['break_even_price'])  # leader and B stop out at break-even; C is liquidated
        self.leader_closed()
        fc = self.final('tc')
        self.assertEqual((fc['result'], fc['exit_reason']), ('flat', 'leader:broker_stop_break_even'))
        self.assertIn(self.final('tb')['exit_reason'], ('broker_stop_break_even', 'leader:broker_stop_break_even'))
        for key in self.keys():
            self.assert_flat(self.acct(key))
            self.assert_never_opposite(self.acct(key))
        self.assertIn('order/liquidateposition', [e for e, _ in self.requests(FC)])


class FollowerFaults(NativeFiringE2E):
    FIRING = {'follower_native': {'exit_verify_ms': 1000, 'backstop_ms': 1500}}

    def test_follower_stop_fill_while_the_leader_runs(self):
        p = self.running()
        leader_ids = [c['command_id'] for c in self.main.journal.for_trade(p['trade_id'])]
        mark = len(self.fake.bodies)
        self.fill_now(self.stop_id('tb'))  # only B's resting stop fills
        final = self.final('tb')
        self.assertEqual((final['result'], final['exit_reason']), ('flat', 'broker_stop_initial'))
        self.assert_flat(FB)
        time.sleep(1.0)
        self.assertEqual((self.net(LEADER), self.stop_values('ta')),
                         (QTY, (QTY, p['initial_hard_stop_price'], 'Working')))
        self.assertEqual(self.fake.orders[self.tp1_id('ta')]['ordStatus'], 'Working')
        self.assertEqual([c['command_id'] for c in self.main.journal.for_trade(p['trade_id'])], leader_ids)
        self.assertEqual(self.requests(LEADER, mark), [])  # nothing at all was sent for the leader
        self.assertEqual((self.net(FC), self.stop_values('tc')[:2]), (QTY, (QTY, p['initial_hard_stop_price'])))
        self.assertEqual(self.trade['phase'], 'entering' if not self.trade['tp1_done'] else 'running')
        self.eod()
        self.leader_closed()
        self.assertEqual(self.final('tc')['exit_reason'], 'leader:eod')
        self.assert_flat(FC)

    def test_follower_closed_by_hand_mid_trade_ends_flat_with_an_alert(self):
        """A leader-side scenario run on a follower: after TP1 and break-even, the runners are
        closed outside the bridge (Exit at Mkt & Cxl). The follower's own fills no longer explain its
        position; after reduce_confirm_ms it runs exit-all, verifies flat and ends with an alert. The
        leader and the other follower are untouched."""
        p = self.running()
        self.fake.set_price(tp1_limit(p) - 0.25)
        self.fake.fill_limit(self.tp1_id('tb'), 1)
        self.at_break_even('tb')
        leader_ids = [c['command_id'] for c in self.main.journal.for_trade(p['trade_id'])]
        mark = len(self.fake.bodies)
        with self.fake.lock:  # by hand, not through the bridge: cancel the stop, close the 3 runners
            self.fake._cancel(self.stop_id('tb'))
            self.fake._place({'accountId': FB, 'action': 'Sell', 'symbol': SYMBOL, 'orderQty': AFTER_TP1,
                              'orderType': 'Market', 'text': 'by-hand'}, lambda *a, **k: None)
        final = self.final('tb')
        self.assertEqual((final['result'], final['exit_reason']), ('flat', 'position_mismatch'))
        self.assertIn(('tb', 'native_exit'), {(a['account'], a['kind']) for a in self.alerts()})
        self.assertFalse((self.h.tmp / 'firing' / 'tb' / follower_native.BLOCK_NAME).exists())
        self.assertEqual(self.requests(LEADER, mark), [])
        self.assertEqual([c['command_id'] for c in self.main.journal.for_trade(p['trade_id'])], leader_ids)
        self.assertEqual((self.net(LEADER), self.net(FC)), (QTY, QTY))
        self.eod()
        self.leader_closed()
        self.assert_flat(FC)

    def test_follower_exit_all_failure_blocks_only_that_follower(self):
        p = self.running()
        exb = self.fx['tb']

        def refuse():
            raise Refused('test: liquidation refused for this follower')
        exb._guard_flatten = refuse
        self.eod()
        self.leader_closed()
        self.assertEqual(self.final('tc')['result'], 'flat')
        self.assert_flat(FC)
        final = self.final('tb', timeout=20)
        self.assertEqual((final['result'], final['exit_reason'], len(final['attempts']), final['kill_switch']),
                         ('native_exit_failed', 'leader:eod', 3, True))
        self.assertEqual(self.net(FB), QTY)  # left with its resting stop; a person decides
        self.assertEqual(self.stop_values('tb')[2], 'Working')
        self.assertTrue(Path(self.cfg['followers'][0]['kill_switch_file']).exists())
        self.assertTrue((self.h.tmp / 'firing' / 'tb' / follower_native.BLOCK_NAME).exists())
        self.assertFalse(Path(self.cfg['kill_switch_file']).exists())  # the leader is never killed
        self.assertFalse(Path(self.cfg['followers'][1]['kill_switch_file']).exists())
        self.assertIn(('tb', 'exit_all_unverified'), {(a['account'], a['kind']) for a in self.alerts()})
        self.assertEqual(self.main.journal.unresolved(), [])
        # The next trade: the leader and C trade; B is skipped, its position untouched. Even with its
        # kill switch gone (say the kill request had failed), the lane's own block holds: no alignment.
        del exb._guard_flatten
        Path(self.cfg['followers'][0]['kill_switch_file']).unlink()
        self.fake.set_price(BASE)
        mark = len(self.fake.bodies)
        p2 = self.enter('long')
        until(lambda: self.net(LEADER) == QTY and self.net(FC) == QTY, 10, 'leader and C in the next trade')
        rec = until(lambda: next((r for r in self.lane_records('tb') if r.get('trade_id') == p2['trade_id']
                                  and r['type'] == 'skipped' and r.get('kind') == 'entry'), None), 10, 'B skip')
        self.assertEqual(rec['reason'], 'follower_blocked_native_exit')
        self.assertEqual((self.net(FB), self.requests(FB, mark)), (QTY, []))
        self.eod()
        self.leader_closed()
        self.assert_flat(FC)

    def test_follower_break_even_modify_rejected_cancels_its_stop_then_synthetic(self):
        p = self.running()
        stopb = self.stop_id('tb')
        self.fake.reject_modify_when = lambda b: b.get('orderId') == stopb and \
            b.get('stopPrice') == p['break_even_price']
        mark = len(self.fake.bodies)
        self.fake.set_price(tp1_limit(p))
        for key in ('ta', 'tc'):
            self.at_break_even(key)
        until(lambda: (self.native('tb') or {}).get('fallback') == 'stop_move_failed:stop_modify_rejected', 10,
              'B synthetic after the stop was cancelled')
        self.assertEqual(self.fake.orders[stopb]['ordStatus'], 'Canceled')
        self.assertEqual(self.net(FB), AFTER_TP1)
        eps = [e for e, _ in self.requests(FB, mark)]
        self.assertIn('order/cancelorder', eps)
        self.assertLess(max(i for i, e in enumerate(eps) if e == 'order/modifyorder'), eps.index('order/cancelorder'))
        self.assertIn(('tb', 'native_fallback'), {(a['account'], a['kind']) for a in self.alerts()})
        self.fake.reject_modify_when = None
        self.fake.set_price(p['break_even_price'])  # leader and C stop out; B exits on the leader's terminal
        self.leader_closed()
        self.assertEqual(self.final('tb')['exit_reason'], 'leader:broker_stop_break_even')
        for key in self.keys():
            self.assert_flat(self.acct(key))
            self.assert_never_opposite(self.acct(key))


class RejectedEntry(NativeFiringE2E):
    FOLLOWERS = (('tb', 'ACCOUNT-B', FB, {'max_contracts': 2}), ('tc', 'ACCOUNT-C', FC, {}))

    def setUp(self):
        super().setUp()
        # There is no size cap any more (tb's legacy max_contracts 2 is ignored), so
        # tb's bracket is refused by a bridge-side refusal injected here; every refusal takes this path.
        def refuse(cmd):
            raise Refused('entry refused for this test')
        self.fx['tb']._entry_preconditions = refuse

    def test_follower_bracket_refused_skips_that_follower_only(self):
        p = self.running(keys=['tc'])
        final = self.final('tb')
        self.assertEqual((final['result'], final['skip_reason']), ('entry_skipped', 'entry_refused'))
        self.assertEqual([o for o in self.fake.orders.values() if o['accountId'] == FB], [])
        # One informational note, no fault alert, no kill switch or block.
        self.assertEqual([(a['account'], a['kind'], a.get('level')) for a in self.alerts()],
                         [('tb', 'follower_entry_skipped', 'info')])
        self.assertFalse(Path(self.cfg['followers'][0]['kill_switch_file']).exists())
        self.assertFalse((self.h.tmp / 'firing' / 'tb' / follower_native.BLOCK_NAME).exists())
        self.fake.set_price(tp1_limit(p))
        self.at_break_even('ta')
        self.at_break_even('tc')
        self.fake.set_price(p['break_even_price'])
        self.leader_closed()
        self.assertEqual(self.final('tc')['result'], 'flat')
        self.assert_flat(FC)
        self.assertFalse([a for a in self.alerts() if a['account'] != 'tb'])
        self.assertEqual(len([a for a in self.alerts() if a['account'] == 'tb']), 1)  # once per trade


class Misaligned(NativeFiringE2E):
    def manual(self, acct, action, qty, order_type='Market', price=None):
        body = {'accountId': acct, 'action': action, 'symbol': SYMBOL, 'orderQty': qty, 'orderType': order_type,
                'text': 'by-hand'}
        if price is not None:
            body['price'] = price
        with self.fake.lock:
            self.fake._place(body, lambda *a, **k: None)

    def test_pre_existing_position_and_working_order_are_aligned_before_the_bracket(self):
        self.manual(FB, 'Buy', 2)                      # B: long 2 by hand
        self.manual(FC, 'Sell', 1, 'Limit', BASE + 250)  # C: a stray working order
        stray = max(o for o, v in self.fake.orders.items() if v['accountId'] == FC)
        until(lambda: self.net(FB) == 2 and self.state.open_orders(FC, CONTRACT) == [stray], 5, 'misaligned')
        self.running('short')                           # the leader is never blocked by B's long
        self.assertEqual([n for a, _, n in self.fake.position_history if a == FB], [2, 0, -QTY])
        self.assertEqual(self.fake.orders[stray]['ordStatus'], 'Canceled')
        acts = {k: [(r['command_id'].split(':')[0], r['payload'].get('action')) for r in self.lane_records(k)
                    if r['type'] == 'intent' and r['source'] == 'align'] for k in self.keys()}
        self.assertEqual(acts, {'tb': [('align-flatten', 'align_flatten')], 'tc': [('align-cancel', 'cancel')]})
        self.eod()
        self.leader_closed()
        for key in self.keys():
            self.assert_flat(self.acct(key))


class LeaderExits(NativeFiringE2E):
    def test_leader_stop_out_exits_every_follower(self):
        p = self.running()
        mark = len(self.fake.bodies)
        self.fill_now(self.stop_id('ta'))  # only the leader's resting stop fills
        self.leader_closed()
        self.assertEqual(self.trade['terminal_reason'], 'broker_stop_initial')
        for key in self.keys():
            final = self.final(key)
            self.assertEqual((final['result'], final['exit_reason']), ('flat', 'leader:broker_stop_initial'))
            self.assert_flat(self.acct(key))
            self.assert_never_opposite(self.acct(key))
            self.assertIn('order/liquidateposition', [e for e, _ in self.requests(self.acct(key), mark)])
        self.assertFalse(self.alerts())

    def test_eod_after_break_even_liquidates_every_account_once(self):
        p = self.running()
        self.fake.set_price(tp1_limit(p))
        for key in ['ta'] + self.keys():
            self.at_break_even(key)
        mark = len(self.fake.bodies)
        self.eod()
        self.leader_closed()
        for key in self.keys():
            self.assertEqual(self.final(key)['exit_reason'], 'leader:eod')
            self.assertEqual([e for e, _ in self.requests(self.acct(key), mark)], ['order/liquidateposition'])
            self.assert_flat(self.acct(key))

    def test_parent_sweep_then_cbdr_shrink_first_on_each_follower(self):
        p = self.running()
        self.fake.set_price(tp1_limit(p))
        for key in ['ta'] + self.keys():
            self.at_break_even(key)
        until(lambda: self.now() >= self.trade['runner_not_before'] + 1000, 6, 'runner window')
        mark = len(self.fake.bodies)
        at = self.now() // 1000 * 1000
        self.assertEqual(self.soup('pine', soup.facts(p, [{'kind': 'parent_sweep', 'close_side': 'long'}], at,
                                                      price=BASE + 1)),
                         ('processed', 'facts_processed'))
        for key in ['ta'] + self.keys():
            until(lambda: self.net(self.acct(key)) == 1, 10, key + ' parent runner out')
        for key in self.keys():
            reqs = [(e, b) for e, b in self.requests(self.acct(key), mark)]
            self.assertEqual([e for e, _ in reqs], ['order/modifyorder', 'order/placeorder'], key)
            self.assertEqual((reqs[0][1]['orderQty'], reqs[0][1]['stopPrice']), (1, p['break_even_price']))
            self.assertEqual((reqs[1][1]['orderType'], reqs[1][1]['orderQty']), ('Market', ALLOC['parent']))
        b = self.now() // 1000 * 1000
        t = p['runner_cbdr_target']
        self.fake.set_price(t)
        self.assertEqual(self.soup('bar', {'event_id': 'bar%d' % b, 'symbol': CONFIG['ticker'], 'open_time': b - 1000,
                                           'close_time': b, 'open': t - 2, 'high': t + 1, 'low': t - 3, 'close': t}),
                         ('processed', 'bar_processed'))
        self.leader_closed()
        for key in self.keys():
            self.assertEqual(self.final(key)['result'], 'flat')
            self.assert_flat(self.acct(key))
            self.assert_never_opposite(self.acct(key))


class Restart(NativeFiringE2E):
    def test_restart_mid_trade_resumes_without_replay(self):
        p = self.running()
        self.fake.set_price(tp1_limit(p))
        for key in ['ta'] + self.keys():
            self.at_break_even(key)
        until(lambda: all((self.native(k)['modify'] or {}).get('confirmed') for k in self.keys()), 5, 'BE confirmed')
        before = {k: [c['command_id'] for c in self.fx[k].journal.for_trade(p['trade_id'])] for k in self.keys()}
        self.stop_firing()
        self.start_firing()
        time.sleep(2.0)
        for key in self.keys():
            st = self.native(key)
            self.assertTrue(st['stop_confirmed'] and st['modify']['confirmed'] and not st['done'], st)
            self.assertEqual([c['command_id'] for c in self.fx[key].journal.for_trade(p['trade_id'])], before[key])
        self.eod()
        self.leader_closed()
        for key in self.keys():
            self.assertEqual(self.final(key)['result'], 'flat')
            self.assert_flat(self.acct(key))
            ids = [c['command_id'] for c in self.fx[key].journal.for_trade(p['trade_id'])]
            self.assertEqual(ids[:len(before[key])], before[key])
            self.assertEqual(ids[len(before[key]):], [f'f:{key}:native-exit:{p["trade_id"]}:1'])
            intents = [r['command_id'] for r in self.lane_records(key) if r['type'] == 'intent']
            self.assertEqual(len(intents), len(set(intents)))


class StageAIdentity(unittest.TestCase):
    """Followers configured off (Stage A: pinned, kill switches set, no firing service) or not configured
    at all: the leader's native trade is the same."""

    def run_leader(self, with_followers):
        case = NativeFiringE2E('run')
        case.WITH_FOLLOWERS, case.START_FIRING = with_followers, False
        case._cleanups = []
        case.setUp()
        try:
            if with_followers:
                for f in case.cfg['followers']:
                    Path(f['kill_switch_file']).touch()
            p = case.enter('long')
            until(lambda: case.net(LEADER) == QTY and (case.trade.get('native') or {}).get('stop_confirmed'), 10, 'up')
            case.fake.set_price(tp1_limit(p))
            until(lambda: case.stop_values('ta')[:2] == (AFTER_TP1, p['break_even_price']), 6, 'BE')
            case.fake.set_price(p['break_even_price'])
            until(lambda: case.trade['phase'] == 'closed', 15, 'closed')
            soup_payloads = [json.loads(x) for x in case.leader_transport.sent]
            for x in soup_payloads:
                x['extras'].pop('soup_command_id')
                x['extras'].pop('soup_trade_id')
            kinds = [(c['kind'], c['outcome']) for c in case.main.journal.for_trade(p['trade_id'])
                     if not c['kind'].startswith('auto_')]
            others = [b for e, b in case.fake.bodies if (b.get('accountId') or LEADER) != LEADER]
            fjournals = [c for ex in case.fx.values() if hasattr(ex, 'journal') for c in ex.journal.commands.values()]
            return soup_payloads, kinds, case.trade['terminal_reason'], others, fjournals, case.pump_errors
        finally:
            case.doCleanups()

    def test_leader_native_trade_identical_with_followers_pinned_but_not_firing(self):
        plain = self.run_leader(False)
        pinned = self.run_leader(True)
        self.assertEqual(plain[:3], pinned[:3])
        self.assertEqual((pinned[3], pinned[4], plain[5], pinned[5]), ([], [], [], []))
        self.assertEqual(plain[2], 'broker_stop_break_even')


def tearDownModule():
    print('FOLLOWER-NATIVE-RESULTS ' + json.dumps(RESULTS, sort_keys=True), file=sys.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
