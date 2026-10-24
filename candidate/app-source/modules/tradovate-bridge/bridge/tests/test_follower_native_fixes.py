"""Native followers: leader terminal reasons, external leader close, skipped entries.

- Leader terminal reasons `external_flat_unreconciled` and `native_broker_view_stale` close every
  follower through its own verified exit-all.
- External leader close: when the leader goes flat outside the bridge while a follower is in the same
  trade, the follower exits at once on whichever comes first, the engine's `external_flat_unreconciled`
  or the bridge's own view of the leader (flat, nothing working, unexplained by the bridge's fills, the
  engine's trade open) on 2 consecutive leader snapshots. One alert per follower, reason
  `leader_closed_externally`.
- Skipped entry: a follower entry refused or rejected with zero fill sends nothing more, sets no kill
  switch or block file, and is noted once as the informational `follower_entry_skipped`.
- Closed by hand: a follower closed by hand ends flat with an alert, does not block, and follows the next trade.
- A synthetic replay (short 4, TP1 1, break-even stop for 3, then the leader closed outside the bridge)
  with B and C following, and the leader's path compared with a leader-only run.

End-to-end classes use the harness of test_follower_native (headless Chrome, fake exchange, the
native-exits service from the optional engine package, located with TVBRIDGE_SOUP_NATIVE_PACKAGE).
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_follower_native import (AFTER_TP1, BASE, CONFIG, CONTRACT, FC, FB, LEADER, QTY, SOUP, TOKEN, LivePub,
                                  NativeFiringE2E, SYMBOL, digest, message, native_trade, tp1_limit, until)
from test_firing import LoginDouble, follower_entry
from tvbridge import follower_native, followers
from tvbridge.executor import Executor
from tvbridge.firing import AlertLog, FollowerLane
from tvbridge.journal import Journal
from tvbridge.state import AccountState

RESULTS = {}


# ----------------------------------------------------------------------------------------
# Browser-free
# ----------------------------------------------------------------------------------------

def report(seq, net=0, orders=(), synced=True, entry=5, out=2, unresolved=0, unseen=0, tid='T', open_orders=None):
    return {'main': {'synced': synced, 'net_position': net, 'orders': list(orders),
                     'leader': {'trade_id': tid, 'entry_filled': entry, 'exit_filled': out, 'unresolved': unresolved,
                                'net_position': net, 'open_orders': len(orders) if open_orders is None else open_orders,
                                'unseen': unseen, 'snapshot_sequence': seq}},
            'followers': []}


class LeaderFlatWatchUnit(unittest.TestCase):
    TRADE = {'id': 'T', 'phase': 'closing'}

    def test_two_distinct_snapshots_confirm_once(self):
        w = follower_native.LeaderFlatWatch()
        self.assertIsNone(w.observe(self.TRADE, report(10), 1000))
        self.assertIsNone(w.observe(self.TRADE, report(10), 1500))  # the same snapshot read again
        fact = w.observe(self.TRADE, report(11), 2000)
        self.assertEqual({k: fact[k] for k in ('trade_id', 'source', 'entry_filled', 'bridge_exit_filled', 'external',
                                               'snapshots', 'first_at')},
                         {'trade_id': 'T', 'source': 'bridge_view', 'entry_filled': 5, 'bridge_exit_filled': 2,
                          'external': 3, 'snapshots': [10, 11], 'first_at': 1000})
        self.assertIsNone(w.observe(self.TRADE, report(12), 2500))  # once per trade

    def test_any_non_candidate_read_resets_the_count(self):
        w = follower_native.LeaderFlatWatch()
        w.observe(self.TRADE, report(10), 1000)
        self.assertIsNone(w.observe(self.TRADE, report(11, net=-2), 1500))
        self.assertIsNone(w.observe(self.TRADE, report(12), 2000))
        self.assertIsNotNone(w.observe(self.TRADE, report(13), 2500))

    def test_not_a_candidate(self):
        cases = {
            'soup trade closed': ({'id': 'T', 'phase': 'closed'}, report(1)),
            'position': (self.TRADE, report(1, net=-1)),
            'order working': (self.TRADE, report(1, orders=[{'id': '9'}])),
            'leader open orders': (self.TRADE, report(1, open_orders=1)),
            'unseen accepted order': (self.TRADE, report(1, unseen=1)),
            'unresolved command': (self.TRADE, report(1, unresolved=1)),
            'bridge exits explain it': (self.TRADE, report(1, out=5)),
            'no entry fill': (self.TRADE, report(1, entry=0, out=0)),
            'not synced': (self.TRADE, report(1, synced=False)),
            'other trade': ({'id': 'U', 'phase': 'running'}, report(1)),
            'no leader facts': (self.TRADE, {'main': {'synced': True, 'net_position': 0, 'orders': []}}),
        }
        for what, (trade, rep) in cases.items():
            self.assertFalse(follower_native.leader_flat_candidate(trade, rep), what)
        self.assertTrue(follower_native.leader_flat_candidate(self.TRADE, report(1)))

    def test_without_a_snapshot_sequence_reads_two_seconds_apart(self):
        w = follower_native.LeaderFlatWatch()
        self.assertIsNone(w.observe(self.TRADE, report(None), 1000))
        self.assertIsNone(w.observe(self.TRADE, report(None), 2999))
        self.assertIsNotNone(w.observe(self.TRADE, report(None), 3000))

    def test_a_new_trade_starts_over(self):
        w = follower_native.LeaderFlatWatch()
        w.observe(self.TRADE, report(10), 1000)
        self.assertIsNone(w.observe({'id': 'U', 'phase': 'running'}, report(11, tid='U'), 1500))
        self.assertIsNotNone(w.observe({'id': 'U', 'phase': 'running'}, report(12, tid='U'), 2000))


class LeaderFactsRoute(unittest.TestCase):
    """GET /followers?trade_id= adds `main.leader` (read-only, in memory); without trade_id nothing new."""

    def test_summary_leader_facts(self):
        tmp = Path(tempfile.mkdtemp(prefix='fn-facts-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        state = AccountState()
        page = LoginDouble(state)
        cfg = {'ticker': 'MNQZ2021', 'account_name': 'MAIN-ACCT', 'account_id': 7, 'soup_account_id': 'Account A',
               'max_contracts': 10, 'tick_size': .25, 'journal_dir': str(tmp / 'journal'),
               'kill_switch_file': str(tmp / 'ENTRIES-DISABLED'), 'publish_mode': 'adapter-db',
               'followers': [follower_entry('f1', 'F-ONE', tmp, native_exits_enabled=True)]}
        main = Executor(page, state, Journal(cfg['journal_dir']), cfg)
        main.resolve()
        fx = followers.build(page, state, cfg, main)
        pub = LivePub()
        pub.last = {'published': 'adapter_db', 'sequence': 41}
        self.assertNotIn('leader', followers.summary(fx, main, None, pub)['main'])
        lf = followers.summary(fx, main, 'trade-x', pub)['main']['leader']
        self.assertEqual(lf, {'trade_id': 'trade-x', 'entry_filled': 0, 'exit_filled': 0, 'unresolved': 0,
                              'net_position': 0, 'open_orders': 0, 'unseen': 0, 'snapshot_sequence': 41})
        pub.last = {'published': False, 'reason': 'snapshot_unavailable'}
        self.assertIsNone(followers.summary(fx, main, 'trade-x', pub)['main']['leader']['snapshot_sequence'])
        self.assertIsNone(followers.summary(fx, main, 'trade-x')['main']['leader']['snapshot_sequence'])
        self.assertEqual(page.sent, [])


class NativeEngineCheck(unittest.TestCase):
    """The firing service's start check (native mode needs the engine's `native_message`), in a subprocess
    so the engine modules load exactly as in the service; nothing is sent (the bridge URL is unreachable)."""

    SCRIPT = r"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tvbridge.firing import Firing, FiringConfigError
tmp = Path(tempfile.mkdtemp())
(tmp / 'soup').mkdir()
cfg = {'soup_db': str(tmp / 'soup' / 'engine.sqlite'), 'soup_engine_dir': sys.argv[2], 'ticker': 'MNQZ2021',
       'bridge_url': 'http://127.0.0.1:9', 'firing_dir': str(tmp / 'firing'),
       'firing_order': ['account-a', 'account-b'],
       'accounts': {'account-a': {'role': 'main'}, 'account-b': {'role': 'follower'}},
       'follower_exits': 'native'}
try:
    f = Firing(cfg, 'x' * 32)
    print(json.dumps({'ok': True, 'lanes': sorted(f.lanes)}))
    f.close()
except FiringConfigError as exc:
    print(json.dumps({'ok': False, 'error': str(exc)}))
"""

    def run_check(self, engine):
        import subprocess
        import sys
        root = str(Path(__file__).resolve().parent.parent)
        out = subprocess.run([sys.executable, '-c', self.SCRIPT, root, str(engine)], capture_output=True, text=True,
                             timeout=60, env={'PYTHONDONTWRITEBYTECODE': '1', 'PATH': '/usr/bin:/bin'})
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_native_engine_passes_and_an_engine_without_native_message_is_refused(self):
        self.assertEqual(self.run_check(SOUP / 'engine'), {'ok': True, 'lanes': ['account-b']})
        old = Path(tempfile.mkdtemp(prefix='fn-oldengine-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(old, ignore_errors=True))
        for name in ('transport.py', 'storage.py'):
            text = (SOUP / 'engine' / name).read_text()
            (old / name).write_text(text.replace('def native_message', 'def other_message'))
        self.assertEqual(self.run_check(old)['ok'], False)


class StatusWindow(unittest.TestCase):
    def test_status_shows_open_trades_and_the_three_latest_done(self):
        class Lane(follower_native.NativeFollower):
            block_path = Path('/nonexistent/ENTRIES-BLOCKED-NATIVE-EXIT')
        lane = Lane()
        plan = follower_native.native_plan(native_trade())
        lane.nat = {}
        for i in range(6):
            st = follower_native.NativeFollower._native_apply(None, {'event': 'start', 'trade_id': 't%d' % i,
                                                                     'plan': plan, 'entry_cid': 'e', 'quantity': QTY,
                                                                     'at': i})
            st['done'] = i != 1
            lane.nat['t%d' % i] = st
        self.assertEqual(list(lane.native_status()['trades']), ['t1', 't3', 't4', 't5'])


class Clock:
    def __init__(self, t=1_500_000_000_000):
        self.t = t

    def __call__(self):
        return self.t


class StateStub:
    def __init__(self):
        self.value = None

    def get(self, trade_id=None):
        if self.value is None:
            raise OSError('no state')
        return json.loads(json.dumps(self.value))


class Transport:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return 'accepted', {'broker_order_id': str(len(self.sent))}


class LaneRules(unittest.TestCase):
    """The lane's step, with a stubbed follower account view (no browser): leader terminal mapping,
    the external leader close from the supervisor's fact, the zero-fill skip and the TP1 gate."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix='fn-rules-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        (tmp / 'soup').mkdir()
        cfg = {'soup_db': str(tmp / 'soup' / 'engine.sqlite'), 'soup_engine_dir': str(SOUP / 'engine'),
               'ticker': CONFIG['ticker'], 'mirror_dir': str(tmp / 'tb'), 'bridge_url': 'http://127.0.0.1:9'}
        self.clock, self.state, self.transport = Clock(), StateStub(), Transport()
        self.alerts = AlertLog(tmp, self.clock)
        self.lane = FollowerLane(cfg, TOKEN, 'tb', alerts=self.alerts, transport=self.transport,
                                 bridge_state=self.state, soup=(message, digest), report=None, clock_ms=self.clock,
                                 native_policy=dict(follower_native.NATIVE_DEFAULTS))
        self.addCleanup(self.lane.close)
        self.lane._native_kill = lambda reason: self.fail('kill switch requested: ' + reason)
        t = native_trade('short')
        self.plan = follower_native.native_plan(t)
        c = {'id': 'soup-entry-1', 'trade_id': t['id'], 'kind': 'entry', 'quantity': QTY, 'price': BASE,
             'started_at': self.clock()}
        info = dict(FollowerLane._trade_info(self.lane, t), symbol=CONFIG['ticker'])
        self.lane._native_entry(info, c)
        self.tid, self.st = t['id'], self.lane.nat[t['id']]

    def view(self, net=-QTY, filled=QTY, entry_status='Filled', stop_status='Working', stop_filled=0, open_ids=('s1',)):
        cmds = {'soup-entry-1': {'kind': 'bracket_entry', 'outcome': 'accepted', 'seen': True,
                                 'working': entry_status in ('Working', 'PendingNew'), 'status': entry_status,
                                 'filled': filled, 'quantity': QTY}}
        stop = {'broker_order_id': 's1', 'seen': True, 'status': stop_status, 'working': stop_status == 'Working',
                'quantity': QTY, 'filled': stop_filled, 'stop_price': self.plan['initial']}
        self.state.value = {'synced': True, 'net_position': net, 'open_order_ids': list(open_ids),
                            'unseen_accepted_orders': [], 'observed_at': self.clock(),
                            'trade': {'commands': cmds, 'stop': stop}}

    def kinds(self):
        return [p.get('action') for p in self.transport.sent]

    def step(self):
        self.lane._native_step(self.st)

    def test_soup_external_close_is_leader_closed_externally_with_one_alert(self):
        self.view()
        self.lane.leader[self.tid] = {'terminal_reason': 'external_flat_unreconciled', 'phase': 'closing',
                                      'flatten': False, 'decisions': {}}
        self.step()
        self.assertEqual((self.st['exit']['reason'], self.kinds()), ('leader_closed_externally', ['sell', 'flatten_all']))
        self.clock.t += 100
        self.step()
        self.assertEqual([(a['kind'], a['reason'], a['source']) for a in self.alerts.recent],
                         [('native_exit', 'leader_closed_externally', 'soup')])
        self.view(net=0, open_ids=())
        self.clock.t += 100
        self.state.value['observed_at'] = self.clock()
        self.step()
        self.assertEqual((self.st['done'], len(self.alerts.recent)), (True, 1))

    def test_broker_view_stale_closes_the_follower_as_a_leader_reason(self):
        self.view()
        self.lane.leader[self.tid] = {'terminal_reason': 'native_broker_view_stale', 'phase': 'closing',
                                      'flatten': False, 'decisions': {}}
        self.step()
        self.assertEqual((self.st['exit']['reason'], self.kinds()), ('leader:native_broker_view_stale',
                                                                     ['sell', 'flatten_all']))
        self.assertEqual(self.alerts.recent, [])  # Soup alerts broker_view_stale itself

    def test_bridge_view_fact_closes_the_follower_at_once(self):
        self.view()
        self.lane.leader[self.tid] = {'terminal_reason': None, 'phase': 'running', 'flatten': False, 'decisions': {}}
        self.step()  # TP1 goes out (entry filled); nothing else
        self.assertEqual(self.kinds(), ['sell', 'exit'])
        self.lane.leader_external[self.tid] = {'trade_id': self.tid, 'source': 'bridge_view', 'snapshots': [5, 6]}
        self.step()
        self.assertEqual((self.st['exit']['reason'], self.kinds()[-1]), ('leader_closed_externally', 'flatten_all'))
        self.assertEqual([(a['kind'], a['reason'], a['source'], a['snapshots']) for a in self.alerts.recent],
                         [('native_exit', 'leader_closed_externally', 'bridge_view', [5, 6])])

    def test_tp1_waits_for_the_entry_fill(self):
        self.view(net=0, filled=0, entry_status='Working', open_ids=('e1',))
        self.lane.leader[self.tid] = {'terminal_reason': None, 'phase': 'entering', 'flatten': False, 'decisions': {}}
        self.step()
        self.assertEqual(self.kinds(), ['sell'])
        self.view()
        self.step()
        self.assertEqual(self.kinds(), ['sell', 'exit'])

    def test_zero_fill_rejection_is_left_alone(self):
        self.view(net=0, filled=0, entry_status='Rejected', stop_status='Canceled', open_ids=())
        self.lane.leader[self.tid] = {'terminal_reason': None, 'phase': 'entering', 'flatten': False, 'decisions': {}}
        self.step()
        self.assertEqual((self.st['done'], self.st['skipped'], self.kinds()), (True, 'entry_rejected_no_fill', ['sell']))
        self.assertEqual([(a['kind'], a['level'], a['status']) for a in self.alerts.recent],
                         [('follower_entry_skipped', 'info', 'Rejected')])
        self.assertTrue(self.lane.entry_skipped(self.tid))
        self.assertFalse(self.lane.block_path.exists())
        # The leader ending later changes nothing for this follower.
        self.lane.leader[self.tid] = {'terminal_reason': 'eod', 'phase': 'closing', 'flatten': True, 'decisions': {}}
        self.lane._native_poll()
        self.assertEqual((self.kinds(), len(self.alerts.recent)), (['sell'], 1))

    def test_zero_fill_rejection_with_a_leg_still_working_waits_then_is_a_fault(self):
        self.view(net=0, filled=0, entry_status='Rejected', stop_status='Working', open_ids=('s1',))
        self.lane.leader[self.tid] = {'terminal_reason': None, 'phase': 'entering', 'flatten': False, 'decisions': {}}
        self.step()
        self.assertEqual((self.st['done'], self.kinds()), (False, ['sell']))
        self.clock.t += follower_native.NATIVE_DEFAULTS['stop_confirm_ms'] + 1
        self.state.value['observed_at'] = self.clock()
        self.step()
        self.assertEqual((self.st['exit']['reason'], self.kinds()), ('entry_incomplete', ['sell', 'flatten_all']))
        self.assertEqual([a['kind'] for a in self.alerts.recent], ['native_exit'])

    def test_any_fill_stays_the_fault_path(self):
        self.view(net=-1, filled=1, entry_status='Canceled')
        self.lane.leader[self.tid] = {'terminal_reason': None, 'phase': 'entering', 'flatten': False, 'decisions': {}}
        self.step()
        self.assertEqual(self.st['exit']['reason'], 'entry_incomplete')
        self.assertIsNone(self.st['skipped'])


# ----------------------------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------------------------

class ExternalCloseE2E(NativeFiringE2E):
    """A short on the leader and both followers, every TP1 filled, every stop at break-even."""

    def at_be_short(self):
        p = self.running('short')
        self.fake.set_price(tp1_limit(p))  # every account's working TP1 limit fills
        for key in ['ta'] + self.keys():
            self.at_break_even(key)
        until(lambda: all((self.native(k)['modify'] or {}).get('confirmed') for k in self.keys()), 5, 'BE confirmed')
        until(lambda: any(c['status'] == 'confirmed' for c in self.soup_commands('modify_stop')), 5, 'Soup BE confirmed')
        return p

    def close_leader_by_hand(self):
        """Exit at Mkt & Cxl on the leader at the broker, outside the engine and the bridge."""
        with self.fake.lock:
            self.fake._liquidate({'accountId': LEADER, 'contractId': CONTRACT, 'admin': False}, lambda *a: None)
        closed = time.monotonic()
        until(lambda: self.net(LEADER) == 0, 5, 'leader flat at the exchange')
        return closed

    def external_alerts(self):
        return [(a['account'], a['kind'], a.get('reason'), a.get('source')) for a in self.alerts()]

    def check_followers_closed(self, p, source):
        for key in self.keys():
            final = self.final(key)
            self.assertEqual((final['result'], final['exit_reason']), ('flat', 'leader_closed_externally'), key)
            self.assert_flat(self.acct(key))
            self.assert_never_opposite(self.acct(key), 'short')
            self.assertIn('order/liquidateposition', [e for e, _ in self.requests(self.acct(key))])
            self.assertFalse((self.h.tmp / 'firing' / key / follower_native.BLOCK_NAME).exists())
            self.assertFalse(Path(self.cfg['followers'][self.keys().index(key)]['kill_switch_file']).exists())
        alerts = self.external_alerts()
        if source is None:  # whichever came first; the same for both followers
            source = alerts[0][3] if alerts else None
        self.assertEqual(sorted(alerts), sorted((k, 'native_exit', 'leader_closed_externally', source)
                                                for k in self.keys()), alerts)
        return source


class LeaderClosedExternally(ExternalCloseE2E):
    def test_soup_external_close_closes_every_follower(self):
        """Soup's own detection (fix a) alone: the bridge-view watch is switched off here."""
        self.firing.leader_watch.observe = lambda *a, **k: None
        p = self.at_be_short()
        mark = len(self.fake.bodies)
        self.close_leader_by_hand()
        until(lambda: self.trade['terminal_reason'] == 'external_flat_unreconciled', 15, 'Soup external close')
        self.check_followers_closed(p, 'soup')
        self.assertEqual(self.requests(LEADER, mark), [])

    def test_bridge_view_closes_every_follower_before_soup_decides(self):
        """Soup receives no snapshot after the close (a stalled or slow Soup): the bridge's own view
        of the leader closes the followers without waiting. Soup then decides on its own."""
        p = self.at_be_short()
        mark = len(self.fake.bodies)
        self.feed_soup.clear()
        closed = self.close_leader_by_hand()
        exits = {k: until(lambda: (self.native(k) or {}).get('exit'), 15, k + ' exit') for k in self.keys()}
        RESULTS.setdefault('bridge_view_close_to_follower_exit_s', []).append(round(time.monotonic() - closed, 2))
        self.assertIsNone(self.trade['terminal_reason'])  # Soup has not decided anything yet
        self.assertEqual({k: e['reason'] for k, e in exits.items()}, {k: 'leader_closed_externally' for k in self.keys()})
        fact = self.firing.leader_external[p['trade_id']]
        self.assertEqual((fact['entry_filled'], fact['bridge_exit_filled'], fact['external'], len(fact['snapshots'])),
                         (QTY, 1, AFTER_TP1, 2))
        self.assertEqual(len(set(fact['snapshots'])), 2)
        self.check_followers_closed(p, 'bridge_view')
        self.feed_soup.set()
        until(lambda: self.trade['terminal_reason'] == 'external_flat_unreconciled', 15, 'Soup external close later')
        time.sleep(1.0)
        self.check_followers_closed(p, 'bridge_view')  # still one alert per follower
        self.assertEqual(self.requests(LEADER, mark), [])

    def test_partial_close_outside_the_bridge_ends_with_broker_view_stale(self):
        """One of the leader's 3 runners closed by hand: not flat, so no external close. Soup refuses the
        view, holds, and after 10 s exits all (`native_broker_view_stale`): the followers close too."""
        p = self.at_be_short()
        with self.fake.lock:
            self.fake._place({'accountId': LEADER, 'action': 'Buy', 'symbol': SYMBOL, 'orderQty': 1,
                              'orderType': 'Market', 'text': 'by-hand'}, lambda *a, **k: None)
        until(lambda: 'broker_view_stale' in [a['code'] for a in self.trade['native']['alerts']], 25,
              'Soup broker_view_stale')
        for key in self.keys():
            final = self.final(key, timeout=20)
            self.assertEqual((final['result'], final['exit_reason']), ('flat', 'leader:native_broker_view_stale'), key)
            self.assert_flat(self.acct(key))
            self.assert_never_opposite(self.acct(key), 'short')
        self.assert_flat(LEADER)
        self.assertEqual(self.alerts(), [])  # a leader reason: Soup alerts, the followers just close


class ExternalCloseReplay(unittest.TestCase):
    """Synthetic replay on leader Account A with B and C following: short 4, TP1 1 filled, stop moved
    to break-even for 3, then A closed outside the bridge (Exit at Mkt & Cxl). Prices come from the
    shared synthetic fixture in test_follower_native.
    Both followers end flat with one alert each, and the leader's path equals the leader-only run."""

    def run_replay(self, with_followers):
        case = ExternalCloseE2E('run')
        case.WITH_FOLLOWERS = case.START_FIRING = with_followers
        case._cleanups = []
        case.setUp()
        try:
            if with_followers:
                p = case.at_be_short()
            else:
                p = case.enter('short')
                until(lambda: case.net(LEADER) == -QTY and (case.trade.get('native') or {}).get('stop_confirmed'), 10,
                      'up')
                case.fake.set_price(tp1_limit(p))
                case.at_break_even('ta')
                until(lambda: any(c['status'] == 'confirmed' for c in case.soup_commands('modify_stop')), 5, 'BE ok')
            mark = len(case.fake.bodies)
            case.close_leader_by_hand()
            until(lambda: case.trade['terminal_reason'] == 'external_flat_unreconciled', 15, 'Soup external close')
            if with_followers:
                RESULTS['replay_follower_trigger'] = case.check_followers_closed(p, None)
            time.sleep(2.0)
            t = case.trade
            n = t['native']
            payloads = [json.loads(x) for x in case.leader_transport.sent]
            for x in payloads:
                x['extras'].pop('soup_command_id')
                x['extras'].pop('soup_trade_id')
            kinds = [(c['kind'], c['outcome']) for c in case.main.journal.for_trade(p['trade_id'])
                     if not c['kind'].startswith('auto_')]
            leader_after = case.requests(LEADER, mark)
            ext = {k: n['external_close'][k] for k in ('entry_qty', 'soup_exit_fills', 'external_reduction')}
            return (payloads, kinds, t['terminal_reason'], ext, (n['failed'] or {}).get('reason')), leader_after, \
                case.pump_errors
        finally:
            case.doCleanups()

    def test_replay_followers_flat_leader_path_unchanged(self):
        alone, alone_after, e1 = self.run_replay(False)
        led, led_after, e2 = self.run_replay(True)
        self.assertEqual(alone, led)
        self.assertEqual(alone[2:], ('external_flat_unreconciled',
                                     {'entry_qty': QTY, 'soup_exit_fills': 1, 'external_reduction': AFTER_TP1},
                                     'external_flat_unreconciled'))
        self.assertEqual((alone_after, led_after, e1, e2), ([], [], [], []))  # nothing sent for A after the close


class EntrySkipped(NativeFiringE2E):
    """A follower (C) whose broker refuses or risk-rejects its next entry bracket."""

    def reject_c(self, how, leg_delay_s=None, keep_legs=False):
        """C's next bracket is refused (`reject_next`) or accepted then Rejected (`risk_reject_next`).
        With leg_delay_s, the exchange cancels the rejected parent's legs that much later (the bridge
        sees the parent Rejected while its legs still look Suspended); keep_legs never cancels them."""
        orig, orig_activate = self.fake._place_oso, self.fake._activate_legs

        def activate_later(parent):
            if keep_legs:
                return

            def run():
                with self.fake.lock:
                    orig_activate(parent)
            threading.Timer(leg_delay_s, run).start()

        def oso(body, reply):
            if body.get('accountId') != FC:
                return orig(body, reply)
            setattr(self.fake, how, True)
            if leg_delay_s is None and not keep_legs:
                return orig(body, reply)
            self.fake._activate_legs = activate_later
            try:
                return orig(body, reply)
            finally:
                self.fake._activate_legs = orig_activate
        self.fake._place_oso = oso
        return lambda: setattr(self.fake, '_place_oso', orig)

    def check_left_alone(self, p, reason, mark):
        final = self.final('tc')
        self.assertEqual((final['result'], final['skip_reason']), ('entry_skipped', reason))
        self.assertEqual([e for e, _ in self.requests(FC, mark)], ['order/placeoso'],  # nothing more for C
                         (self.requests(FC, mark), self.fx['tc'].journal.for_trade(p['trade_id']),
                          [(r['type'], r.get('event'), r.get('command_id')) for r in self.lane_records('tc')]))
        self.assertEqual(self.net(FC), 0)
        self.assertFalse(Path(self.cfg['followers'][1]['kill_switch_file']).exists())
        self.assertFalse((self.h.tmp / 'firing' / 'tc' / follower_native.BLOCK_NAME).exists())
        self.assertEqual(self.fx['tc'].journal.unresolved(), [])
        self.assertTrue(self.firing.lanes['tc'].entry_skipped(p['trade_id']))
        # A and B complete the trade untouched.
        self.fake.set_price(tp1_limit(p))
        for key in ('ta', 'tb'):
            self.at_break_even(key)
        self.fake.set_price(p['break_even_price'])
        self.leader_closed()
        fb = self.final('tb')
        self.assertEqual(fb['result'], 'flat')
        self.assert_flat(FB)
        self.assertEqual([e for e, _ in self.requests(FC, mark)], ['order/placeoso'])
        notes = [(a['account'], a['kind'], a.get('level'), a.get('reason')) for a in self.alerts()]
        self.assertEqual(notes, [('tc', 'follower_entry_skipped', 'info', reason)])  # once, info only
        self.firing.write_status()
        status = json.loads((self.h.tmp / 'firing' / 'status.json').read_text())
        self.assertEqual(status['lanes']['tc']['native']['trades'][p['trade_id']]['skipped'], reason)
        until(lambda: self.firing.update_grid() is not None and
              'flat' in [e['kind'] for e in self.firing.grid.status()['recent']], 10, 'grid ALL FLAT')
        self.assertFalse([e for e in self.firing.grid.status()['recent'] if e['kind'] == 'attention'])

    def test_16_rejected_by_tradovate_after_acceptance_is_left_alone(self):
        # orderId returned, then Rejected; the exchange cancels the legs 0.8 s later. The bridge's grace
        # keeps C's auto-protect from racing that with its own cancel: nothing is sent.
        restore = self.reject_c('risk_reject_next', leg_delay_s=0.8)
        mark = len(self.fake.bodies)
        p = self.running(keys=['tb'])
        restore()
        self.check_left_alone(p, 'entry_rejected_no_fill', mark)
        self.assertEqual([c['kind'] for c in self.fx['tc'].journal.for_trade(p['trade_id'])], ['bracket_entry'])

    def test_16_rejected_with_legs_left_working_is_cleaned_up_as_a_fault(self):
        """Not seen on the fake exchange by default: the legs of a zero-fill rejected bracket stay working.
        After stop_confirm_ms (5 s) it is the normal fault path: exit-all, verified, one alert.
        Known interaction: C's leftover Sell stop is real cross-account exposure, so B's hedge guard
        may refuse B's entry for this trade if it checks while the leg is
        there; B then skips the trade (info note). Either outcome is accepted; A is never affected.
        The follower's auto-protect would cancel the leftover legs after its own zero-fill grace (also
        5 s) and the lane would then rightly skip C; that grace is lengthened here so the lane's
        stop_confirm_ms path is what this test sees."""
        grace = patch.object(followers.FollowerExecutor, 'ZERO_FILL_LEG_GRACE_MS', 60_000)
        grace.start()
        self.addCleanup(grace.stop)
        restore = self.reject_c('risk_reject_next', keep_legs=True)
        p = self.enter('long')
        until(lambda: self.net(LEADER) == QTY and (self.trade.get('native') or {}).get('stop_confirmed'), 10,
              'leader up')
        restore()
        final = self.final('tc', timeout=25)
        self.assertEqual((final['result'], final['exit_reason']), ('flat', 'entry_incomplete'))
        self.assert_flat(FC)
        self.assertFalse(Path(self.cfg['followers'][1]['kill_switch_file']).exists())
        self.eod()
        self.leader_closed()
        fb = self.final('tb')
        alerts = [(a['account'], a['kind'], a.get('reason')) for a in self.alerts()]
        if fb['result'] == 'entry_skipped':
            refusal = next(a for a in self.alerts() if a['account'] == 'tb')['refusal']
            self.assertTrue(refusal.startswith('hedge guard:'), refusal)
            self.assertEqual(sorted(alerts), [('tc', 'native_exit', 'entry_incomplete'),
                                              ('tb', 'follower_entry_skipped', 'entry_refused')])
        else:
            self.assertEqual((fb['result'], fb['exit_reason']), ('flat', 'leader:eod'))
            self.assertEqual(alerts, [('tc', 'native_exit', 'entry_incomplete')])
        RESULTS.setdefault('legs_left_working_tb', []).append(fb['result'])
        self.assert_flat(FB)
        self.assertFalse(Path(self.cfg['followers'][0]['kill_switch_file']).exists())

    def test_16_refused_outright_then_follows_the_next_trade(self):
        restore = self.reject_c('reject_next')  # a definite refusal, no order created
        mark = len(self.fake.bodies)
        p = self.running(keys=['tb'])
        restore()
        self.check_left_alone(p, 'entry_refused', mark)
        # C accepts orders again: the next trade is followed normally, with no time window anywhere.
        self.fake.set_price(BASE)
        p2 = self.running()
        self.fake.set_price(tp1_limit(p2))
        for key in ['ta'] + self.keys():
            self.at_break_even(key)
        self.fake.set_price(p2['break_even_price'])
        self.leader_closed()
        for key in self.keys():
            self.assertEqual(self.final(key, p2['trade_id'])['result'], 'flat')
            self.assert_flat(self.acct(key))
        self.assertEqual(len(self.alerts()), 1)


class FollowerClosedByHand(NativeFiringE2E):
    FIRING = {'follower_native': {'exit_verify_ms': 1000, 'backstop_ms': 1500}}

    def test_closed_by_hand_ends_flat_does_not_block_and_follows_the_next_trade(self):
        """Closed by hand: the follower ends flat with an alert, then the next leader trade is followed normally."""
        p = self.running()
        self.fake.set_price(tp1_limit(p) - 0.25)
        self.fake.fill_limit(self.tp1_id('tb'), 1)
        self.at_break_even('tb')
        with self.fake.lock:
            self.fake._liquidate({'accountId': FB, 'contractId': CONTRACT, 'admin': False}, lambda *a: None)
        final = self.final('tb')
        self.assertEqual((final['result'], final['exit_reason']), ('flat', 'position_mismatch'))
        mine = [(a['account'], a['kind'], a.get('reason')) for a in self.alerts()]
        self.assertEqual([x for x in mine if x[1] == 'native_exit'], [('tb', 'native_exit', 'position_mismatch')])
        # A state read between the hand cancel of the stop and the fill may also note a fallback first.
        self.assertTrue(all(x[0] == 'tb' and x[1] in ('native_exit', 'native_fallback') for x in mine), mine)
        first_trade_alerts = len(mine)
        self.assertFalse((self.h.tmp / 'firing' / 'tb' / follower_native.BLOCK_NAME).exists())
        self.assertFalse(Path(self.cfg['followers'][0]['kill_switch_file']).exists())
        self.assertEqual((self.net(LEADER), self.net(FC)), (QTY, QTY))  # the leader and C go on
        self.eod()
        self.leader_closed()
        self.assert_flat(FC)
        self.fake.set_price(BASE)
        p2 = self.running()  # B takes its full bracket again
        self.fake.set_price(tp1_limit(p2))
        for key in ['ta'] + self.keys():
            self.at_break_even(key)
        self.fake.set_price(p2['break_even_price'])
        self.leader_closed()
        for key in self.keys():
            self.assertEqual(self.final(key, p2['trade_id'])['result'], 'flat')
            self.assert_flat(self.acct(key))
        self.assertEqual(len(self.alerts()), first_trade_alerts)  # nothing new in the second trade


def tearDownModule():
    import sys
    print('FOLLOWER-NATIVE-FIXES-RESULTS ' + json.dumps(RESULTS, sort_keys=True), file=sys.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
