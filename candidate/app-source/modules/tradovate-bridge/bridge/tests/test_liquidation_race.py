"""flatten_all outcome matching per account.

Broker behaviour reproduced here:
- With orders working on the contract, the broker's order/liquidateposition reply carries no orderId
  (s 200, on every account of the login). The bridge then identifies the exit order it created: a new
  Market order, closing side, on the pinned account and contract, id above a baseline taken at dispatch.
- The broker allocates order ids per account, so the id blocks of different accounts interleave in the
  same minutes. The baseline is the pinned account's own maximum order id, never the login-wide maximum:
  with a login-wide baseline, an account whose ids sit below another account's (A and C below B) would
  never match its own liquidation.
- The leader's own liquidation, accepted (or reconciled) without an identified exit order, must not
  count as a close outside the bridge (firing `leader_closed_externally`).

Browser-free units, then headless Chrome + fake exchange (interleaved id blocks and the broker's reply
shape): three accounts liquidated concurrently, every ordering of the id blocks, mixed concurrent order
kinds, and a synthetic exit-all replay through the engine's native-exits service, the firing service and
both followers (optional engine package via TVBRIDGE_SOUP_NATIVE_PACKAGE, as tests/test_follower_native.py).
"""
import itertools
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness import Harness, seed_market_price
from test_firing import follower_entry, records
from test_follower_native import (BASE, CONFIG, FC, FB, LEADER, NativeFiringE2E, soup, tp1_limit, until)
from tvbridge import follower_native, followers
from tvbridge.bridge import Bridge
from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.state import AccountState

RESULTS = {}
# Synthetic interleaved id blocks (leader A in the middle, B highest, C lowest).
ID_BLOCKS = {'ta': 1200000, 'tb': 1300000, 'tc': 1100000}
TID = 'race:MNQZ2021:%d:long'
REF_PRICE, STOP_PRICE, ENTRY_QTY = 10000.0, 9987.5, 4  # synthetic bracket for the concurrency tests
TRADE_DATE = {'year': 2019, 'month': 8, 'day': 21}


# ----------------------------------------------------------------------------------------
# Browser-free
# ----------------------------------------------------------------------------------------

class InterleavedLogin:
    """One page, three accounts with per-account order id blocks; liquidateposition replies without an orderId
    and the exit order's events arrive right after the reply (as the broker does)."""

    ACCOUNTS = {'AA': 2001, 'AB': 2002, 'AC': 2003}
    BASE = {2001: 1200040, 2002: 1300070, 2003: 1100025}

    def __init__(self, state):
        self.state, self.epoch, self.sent = state, 'e1', []
        self.BASE = dict(InterleavedLogin.BASE)  # per instance: ids grow as exit orders are created
        accounts = [{'id': i, 'name': n} for n, i in self.ACCOUNTS.items()]
        positions = [{'id': 300 + k, 'accountId': a, 'contractId': 2, 'netPos': 1, 'tradeDate': TRADE_DATE}
                     for k, a in enumerate(self.BASE)]
        # Each account: its latest order (a filled market reduce) and a working stop for 1.
        orders, versions = [], []
        for a, top in self.BASE.items():
            orders += [{'id': top, 'accountId': a, 'contractId': 2, 'ordStatus': 'Filled', 'action': 'Sell'},
                       {'id': top - 10, 'accountId': a, 'contractId': 2, 'ordStatus': 'Working', 'action': 'Sell'}]
            versions += [{'id': top, 'orderId': top, 'orderQty': 1, 'orderType': 'Market'},
                         {'id': top - 10, 'orderId': top - 10, 'orderQty': 1, 'orderType': 'Stop', 'stopPrice': 1.0}]
        state.load_sync(accounts, {'accounts': accounts, 'orders': orders, 'fills': [], 'orderVersions': versions,
                                   'contracts': [{'id': 2, 'name': 'MNQZ1'}], 'positions': positions}, self.epoch)
        self.lock = threading.Lock()

    def status(self):
        return {'state': 'authorized', 'epoch': self.epoch, 'lastRxAgoMs': 0, 'url': 'fake'}

    def request(self, endpoint, body=None, query='', timeout=5.0, expected_epoch=None):
        self.sent.append((endpoint, body))
        if endpoint == 'order/liquidateposition':
            acct = body['accountId']
            with self.lock:
                self.BASE[acct] += 5
                oid = self.BASE[acct]
            threading.Timer(0.05, self._exit_events, args=(acct, oid)).start()
            return {'ok': True, 'sent': True, 's': 200, 'd': {}}
        return {'ok': True, 'sent': True, 's': 200, 'd': {'commandId': 1}}

    def _exit_events(self, acct, oid):
        st = self.state
        for kind, entity in (
                ('order', {'id': oid, 'accountId': acct, 'contractId': 2, 'ordStatus': 'Filled', 'action': 'Sell'}),
                ('orderVersion', {'id': oid, 'orderId': oid, 'orderQty': 1, 'orderType': 'Market', 'text': 'Exit'}),
                ('fill', {'id': oid + 1, 'orderId': oid, 'contractId': 2, 'qty': 1, 'price': 1.0, 'action': 'Sell',
                          'timestamp': '2019-08-21T17:05:30.000Z', 'active': True}),
                ('position', {'id': 300 + list(self.BASE).index(acct), 'accountId': acct, 'contractId': 2,
                              'netPos': 0, 'tradeDate': TRADE_DATE})):
            st.apply_props({'entityType': kind, 'eventType': 'Updated', 'entity': entity})
        for o in list(st.entities['order'].values()):
            if o['accountId'] == acct and o['ordStatus'] == 'Working':
                st.apply_props({'entityType': 'order', 'eventType': 'Updated', 'entity': dict(o, ordStatus='Canceled')})


class PerAccountBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='liq-unit-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.state = AccountState()
        self.page = InterleavedLogin(self.state)
        self.ex = {}
        for name, acct in InterleavedLogin.ACCOUNTS.items():
            cfg = {'ticker': 'MNQZ2021', 'account_name': name, 'account_id': acct, 'soup_account_id': name,
                   'max_contracts': 10, 'tick_size': .25, 'journal_dir': str(self.tmp / name),
                   'flatten_all_enabled': True, 'bracket_orders_enabled': True, 'flatten_settle_s': 1.0}
            ex = Executor(self.page, self.state, Journal(cfg['journal_dir']), cfg)
            ex.resolve()
            self.ex[name] = ex

    def flatten(self, name):
        return self.ex[name].send({'ticker': 'MNQZ2021', 'action': 'flatten_all', 'extras': {
            'soup_command_id': 'flat-' + name, 'soup_trade_id': 'unit:MNQZ2021:1:long', 'bridge_kind': 'flatten_all'}})

    def test_state_max_order_id_per_account(self):
        self.assertEqual(self.state.max_order_id(), 1300070)  # the login-wide value (not a baseline)
        self.assertEqual({a: self.state.max_order_id(a) for a in InterleavedLogin.ACCOUNTS.values()},
                         {2001: 1200040, 2002: 1300070, 2003: 1100025})
        self.assertEqual(self.state.max_order_id(12345), 0)

    def test_baseline_is_the_pinned_accounts_own(self):
        for name, ex in self.ex.items():
            plan = ex._guard_flatten()
            self.assertEqual((plan['mode'], plan['baseline_order_id']),
                             ('liquidate', self.state.max_order_id(ex.account_id)), name)

    def test_concurrent_liquidations_each_match_their_own_exit_order(self):
        barrier = threading.Barrier(3)
        out = {}

        def run(name):
            barrier.wait()
            out[name] = self.flatten(name)
        threads = [threading.Thread(target=run, args=(n,)) for n in self.ex]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        for name, (outcome, receipt) in out.items():
            acct = InterleavedLogin.ACCOUNTS[name]
            self.assertEqual(outcome, 'accepted', (name, receipt))
            self.assertTrue(receipt.get('matched_by_liquidation_order'), receipt)
            oid = int(receipt['broker_order_id'])
            self.assertEqual((self.state.order(oid)['accountId'], oid), (acct, self.page.BASE[acct]), name)
            self.assertEqual(self.ex[name].journal.unresolved(), [])

    def test_another_accounts_new_order_never_matches(self):
        """A record whose baseline is below another account's new closing Market order: account filter."""
        ex = self.ex['AC']
        rec = {'baseline_order_id': 1, 'close_action': 'Sell'}
        self.page._exit_events(2002, 1300100)  # B's liquidation arrives, C has none
        self.assertIsNone(ex._find_liquidation(dict(rec, baseline_order_id=self.state.max_order_id(ex.account_id))))


class LeaderOwnLiquidation(unittest.TestCase):
    """The bridge's own accepted liquidation of the trade, without an identified exit order, explains a
    flat leader: never `leader_closed_externally`."""

    TRADE = {'id': 'T', 'phase': 'closing'}

    @staticmethod
    def report(seq, own=None):
        lf = {'trade_id': 'T', 'entry_filled': 5, 'exit_filled': 3, 'unresolved': 0, 'net_position': 0,
              'open_orders': 0, 'unseen': 0, 'snapshot_sequence': seq}
        if own is not None:
            lf['own_liquidation'] = own
        return {'main': {'synced': True, 'net_position': 0, 'orders': [], 'leader': lf}, 'followers': []}

    def test_candidate_rule(self):
        self.assertTrue(follower_native.leader_flat_candidate(self.TRADE, self.report(1)))
        self.assertFalse(follower_native.leader_flat_candidate(self.TRADE, self.report(1, own=1)))
        w = follower_native.LeaderFlatWatch()
        for seq in range(5):
            self.assertIsNone(w.observe(self.TRADE, self.report(seq, own=1), 1000 * seq))

    def test_leader_facts_count_own_liquidation_only_when_present(self):
        tmp = Path(tempfile.mkdtemp(prefix='liq-facts-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        state = AccountState()
        page = InterleavedLogin(state)
        cfg = {'ticker': 'MNQZ2021', 'account_name': 'AA', 'account_id': 2001, 'soup_account_id': 'AA',
               'max_contracts': 10, 'tick_size': .25, 'journal_dir': str(tmp / 'j'), 'flatten_all_enabled': True}
        ex = Executor(page, state, Journal(cfg['journal_dir']), cfg)
        ex.resolve()
        tid = 'fixture-stream:MNQZ2021:1566407125000:long'
        self.assertNotIn('own_liquidation', followers.leader_facts(ex, tid))
        # The record under test: liquidate, unknown, then the daemon's "flat with nothing working; no order".
        ex.journal.start('soup-flat-1', trade_id=tid, kind='flatten_all', side='long', quantity=1, tag=None, at=1,
                         mode='liquidate', close_action='Sell', baseline_order_id=1300070)
        ex.journal.finish('soup-flat-1', 'unknown', {'transport': 'uncertain'})
        self.assertEqual(followers.leader_facts(ex, tid)['unresolved'], 1)
        ex.journal.reconcile('soup-flat-1', 'accepted', note='flat with nothing working; no order')
        facts = followers.leader_facts(ex, tid)
        self.assertEqual((facts['unresolved'], facts.get('own_liquidation')), (0, 1))
        # A no-op or cancel-only flatten_all is not a liquidation.
        ex.journal.start('soup-noop', trade_id=tid, kind='flatten_all', side='long', quantity=None, tag=None, at=2,
                         mode='noop')
        ex.journal.finish('soup-noop', 'accepted', {'bridge': 'noop'})
        self.assertEqual(followers.leader_facts(ex, tid).get('own_liquidation'), 1)


# ----------------------------------------------------------------------------------------
# Headless Chrome + fake exchange (interleaved id blocks and the broker's reply shape)
# ----------------------------------------------------------------------------------------

class ConcurrentLiquidationE2E(unittest.TestCase):
    """Leader + two followers of one login, one page; brackets on all three; then concurrent kinds."""

    def open(self, blocks, reply='no_order_id_when_working'):
        h = Harness()
        self.addCleanup(h.close)
        fake = h.fake
        fake.accounts = [{'id': LEADER, 'name': 'ACCOUNT-A', 'userId': 7, 'active': True},
                         {'id': FB, 'name': 'ACCOUNT-B', 'userId': 7, 'active': True},
                         {'id': FC, 'name': 'ACCOUNT-C', 'userId': 7, 'active': True}]
        fake.id_blocks = {LEADER: blocks['ta'], FB: blocks['tb'], FC: blocks['tc']}
        fake.liquidate_reply = reply
        fake.set_price(REF_PRICE)
        state = AccountState()
        bridge = Bridge(state, devtools_port=h.devtools_port, page_url_contains='/trader/',
                        trade_url_pattern=r'^ws://127\.0\.0\.1:\d+/v1/websocket$')
        self.addCleanup(bridge.detach)
        bridge.attach(reload=True)
        bridge.wait_ready(timeout=20)
        cfg = h.config(account_name='ACCOUNT-A', journal_dir=str(h.tmp / 'journal-main'),
                       bracket_orders_enabled=True, flatten_all_enabled=True,
                       followers=[follower_entry('tb', 'ACCOUNT-B', h.tmp, native_exits_enabled=True),
                                  follower_entry('tc', 'ACCOUNT-C', h.tmp, native_exits_enabled=True)])
        main = Executor(bridge, state, Journal(cfg['journal_dir']), cfg)
        main.resolve()
        fx = followers.build(bridge, state, cfg, main)
        ex = {'ta': main, 'tb': fx['tb'], 'tc': fx['tc']}
        for e in ex.values():  # bracket entries need a known market price
            seed_market_price(e, REF_PRICE)
        acct = {'ta': LEADER, 'tb': FB, 'tc': FC}
        return h, fake, state, ex, acct

    @staticmethod
    def payload(cid, tid, kind, **extra):
        p = {'ticker': 'MNQZ2021', 'extras': dict({'soup_command_id': cid, 'soup_trade_id': tid}, **extra)}
        if kind == 'bracket_entry':
            p['extras'].update(bridge_kind='bracket_entry', reference_price=REF_PRICE, stop_price=STOP_PRICE)
            p.update(action='buy', orderType='market', quantity=ENTRY_QTY)
        elif kind == 'flatten_all':
            p['extras']['bridge_kind'] = 'flatten_all'
            p['action'] = 'flatten_all'
        elif kind == 'modify_stop':
            p['extras']['bridge_kind'] = 'modify_stop'
            p['action'] = 'modify_stop'
        elif kind == 'exit_market':
            p.update(action='exit', orderType='market', quantity=extra.pop('qty', 1), cancel=False)
            p['extras'].pop('qty', None)
        return p

    def bracket_all(self, fake, state, ex, acct, tid):
        for key, e in ex.items():
            outcome, receipt = e.send(self.payload('entry', tid, 'bracket_entry'))
            self.assertEqual(outcome, 'accepted', (key, receipt))
            # One account at a time, fully settled: the next follower's hedge guard must see this
            # account's position before its stop (a stop without its position reads as opposite).
            until(lambda: state.net_position(acct[key], 555) == ENTRY_QTY and len(state.open_orders(acct[key], 555)) == 1
                  and all(state.order(o).get('ordStatus') == 'Working' for o in state.open_orders(acct[key], 555)),
                  5, key + ' long, stop working')

    def concurrently(self, calls):
        barrier = threading.Barrier(len(calls))
        out, started = {}, {}

        def run(key, fn):
            barrier.wait()
            started[key] = time.monotonic()
            out[key] = fn()
            out[key] = out[key] + (time.monotonic() - started[key],)
        threads = [threading.Thread(target=run, args=kv) for kv in calls.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        return out

    def check_liquidated(self, fake, state, ex, acct, out, keys):
        for key in keys:
            outcome, receipt, _ = out[key]
            self.assertEqual(outcome, 'accepted', (key, receipt))
            oid = int(receipt['broker_order_id'])
            order, version = fake.orders[oid], fake.versions[oid]
            self.assertEqual((order['accountId'], version['orderType'], version['text'], order['ordStatus']),
                             (acct[key], 'Market', 'Exit', 'Filled'), key)
            rec = next(c for c in ex[key].journal.commands.values() if c['kind'] == 'flatten_all')
            self.assertLess(rec['baseline_order_id'], oid)
            self.assertEqual(rec['baseline_order_id'] // 100_000, oid // 100_000, 'baseline from the same account')
            self.assertEqual(ex[key].journal.unresolved(), [], key)
            until(lambda: state.net_position(acct[key], 555) == 0 and not state.open_orders(acct[key], 555), 5,
                  key + ' flat, nothing working')

    def test_three_accounts_liquidated_within_milliseconds_interleaved_id_blocks(self):
        """A, B and C liquidated within milliseconds of each other; interleaved blocks and reply shape."""
        h, fake, state, ex, acct = self.open(ID_BLOCKS)
        tid = TID % 1
        self.bracket_all(fake, state, ex, acct, tid)
        mark = len(fake.bodies)
        out = self.concurrently({k: (lambda k=k: ex[k].send(self.payload('flat-' + k, tid, 'flatten_all')))
                                 for k in ex})
        self.assertEqual(sorted(e for e, _ in fake.bodies[mark:] if e == 'order/liquidateposition'),
                         ['order/liquidateposition'] * 3)
        RESULTS['concurrent_interleaved'] = {k: {'outcome': v[0], 'matched': v[1].get('matched_by_liquidation_order'),
                                          'seconds': round(v[2], 3)} for k, v in out.items()}
        self.check_liquidated(fake, state, ex, acct, out, list(ex))
        for key in ex:
            self.assertTrue(out[key][1].get('matched_by_liquidation_order'), key)

    def test_every_ordering_of_the_account_id_blocks(self):
        """Whichever account has the highest or lowest ids, each account matches its own exit order."""
        bases = sorted(ID_BLOCKS.values())
        for perm in itertools.permutations(bases):
            blocks = dict(zip(('ta', 'tb', 'tc'), perm))
            with self.subTest(blocks=blocks):
                h, fake, state, ex, acct = self.open(blocks)
                tid = TID % 2
                self.bracket_all(fake, state, ex, acct, tid)
                out = self.concurrently({k: (lambda k=k: ex[k].send(self.payload('flat-' + k, tid, 'flatten_all')))
                                         for k in ex})
                RESULTS.setdefault('orderings', []).append(
                    {k: out[k][0] for k in ('ta', 'tb', 'tc')})
                self.check_liquidated(fake, state, ex, acct, out, list(ex))
                self.doCleanups()  # one browser at a time

    def test_concurrent_mixed_order_kinds_on_the_shared_page(self):
        """A liquidates while B sends a market reduce and C shrinks its stop, all at once: every
        outcome is matched to its own account's order, never another account's."""
        h, fake, state, ex, acct = self.open(ID_BLOCKS)
        tid = TID % 3
        self.bracket_all(fake, state, ex, acct, tid)
        # B: the stop must fit beside a reduce of 1 (the engine shrinks first); C: shrink by one too.
        stopb = int(ex['tb'].journal.get('f:tb:entry')['legs']['stop'])
        self.assertEqual(ex['tb'].send(self.payload('shrinkb', tid, 'modify_stop', stop_quantity=ENTRY_QTY - 1))[0],
                         'accepted')
        until(lambda: state.order_qty(stopb) == ENTRY_QTY - 1, 5, 'B stop shrunk')
        out = self.concurrently({
            'ta': lambda: ex['ta'].send(self.payload('flat-a', tid, 'flatten_all')),
            'tb': lambda: ex['tb'].send(self.payload('reduceb', tid, 'exit_market', qty=1)),
            'tc': lambda: ex['tc'].send(self.payload('shrinkc', tid, 'modify_stop', stop_quantity=ENTRY_QTY - 1)),
        })
        self.check_liquidated(fake, state, ex, acct, out, ['ta'])
        outcome, receipt, _ = out['tb']
        self.assertEqual(outcome, 'accepted', receipt)
        self.assertEqual(fake.orders[int(receipt['broker_order_id'])]['accountId'], FB)
        until(lambda: state.net_position(FB, 555) == ENTRY_QTY - 1, 5, 'B reduced by one')
        self.assertEqual(out['tc'][0], 'accepted', out['tc'])
        self.assertEqual(state.net_position(FC, 555), ENTRY_QTY)
        # Now B and C liquidate together, after A's exit order (the highest id of A's block) exists.
        out2 = self.concurrently({k: (lambda k=k: ex[k].send(self.payload('flat-' + k, tid, 'flatten_all')))
                                  for k in ('tb', 'tc')})
        self.check_liquidated(fake, state, ex, acct, out2, ['tb', 'tc'])

    def test_reply_with_order_id_is_unchanged(self):
        """The broker's reply shape with nothing working ({orderId}): used directly."""
        h, fake, state, ex, acct = self.open(ID_BLOCKS, reply='order_id')
        tid = TID % 4
        self.bracket_all(fake, state, ex, acct, tid)
        out = self.concurrently({k: (lambda k=k: ex[k].send(self.payload('flat-' + k, tid, 'flatten_all')))
                                 for k in ex})
        self.check_liquidated(fake, state, ex, acct, out, list(ex))
        for key in ex:
            self.assertNotIn('matched_by_liquidation_order', out[key][1], key)


class ExitAllReplay(NativeFiringE2E):
    """Synthetic replay: long 4 on A (engine native exits) with B and C following; TP1 1 fills
    everywhere, stops shrink to 3 and move to break-even, the parent runners are reduced (stop to 1,
    market 2), then the engine's CBDR bar close exits all: flatten_all on A and, from the firing lanes,
    on B and C within milliseconds. Interleaved id blocks (A middle, B highest, C lowest) and the
    broker's reply shape (no orderId while the stop works). Prices come from the shared synthetic
    fixture in test_follower_native. The pump adds the daemon publisher's auto_reconcile, as
    tvbridge.daemon does."""

    def setUp(self):
        super().setUp()
        with self.fake.lock:
            self.fake.id_blocks = {LEADER: ID_BLOCKS['ta'], FB: ID_BLOCKS['tb'], FC: ID_BLOCKS['tc']}
            self.fake.liquidate_reply = 'no_order_id_when_working'

    def pump(self):
        while not self.pump_stop.is_set():
            try:
                with self.main.lock:
                    self.main.maintain_brackets()
                t = self.trade
                if t and t['phase'] != 'closed':
                    with self.main.lock:
                        self.main.auto_reconcile()  # tvbridge.daemon publisher loop
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

    def to_cbdr(self):
        p = self.running()
        self.fake.set_price(tp1_limit(p))
        for key in ['ta'] + self.keys():
            self.at_break_even(key)
        until(lambda: self.now() >= self.trade['runner_not_before'] + 1000, 6, 'runner window')
        at = self.now() // 1000 * 1000
        self.assertEqual(self.soup('pine', soup.facts(p, [{'kind': 'parent_sweep', 'close_side': 'long'}], at,
                                                      price=BASE + 1)),
                         ('processed', 'facts_processed'))
        for key in ['ta'] + self.keys():
            until(lambda: self.net(self.acct(key)) == 1, 10, key + ' parent runner out')
        mark = len(self.fake.bodies)
        b = self.now() // 1000 * 1000
        t = p['runner_cbdr_target']
        self.fake.set_price(t)
        self.assertEqual(self.soup('bar', {'event_id': 'bar%d' % b, 'symbol': CONFIG['ticker'], 'open_time': b - 1000,
                                           'close_time': b, 'open': t - 2, 'high': t + 1, 'low': t - 3, 'close': t}),
                         ('processed', 'bar_processed'))
        return p, mark

    def flattens(self, key):
        return [c for c in self.journal(key).for_trade(self.p['trade_id']) if c['kind'] == 'flatten_all'
                and c.get('mode') == 'liquidate']

    def liquidation_times(self, mark):
        """Dispatch order of the three liquidateposition requests (fake bodies with monotonic times)."""
        return [(body['accountId'], t) for (e, body), t in zip(self.fake.bodies[mark:], self.fake.body_times[mark:])
                if e == 'order/liquidateposition']

    def test_replay_all_three_liquidations_matched_soup_closes_cleanly(self):
        p, mark = self.to_cbdr()
        try:
            until(lambda: self.trade['phase'] == 'closed', 30, 'Soup trade closed')
        finally:  # the observed end state, also when this fails
            t = self.trade
            RESULTS['replay_soup'] = {
                'phase': t['phase'], 'terminal_reason': t['terminal_reason'], 'remaining': t['remaining'],
                'failed': t['native']['failed'], 'alerts': [a['code'] for a in t['native']['alerts']],
                'flatten_commands': [(c['status'], c['quantity']) for c in self.soup_commands('flatten')],
                'bridge_flatten': {k: [(c['outcome'], c.get('broker_order_id') is not None,
                                        (c.get('receipt') or {}).get('note')) for c in self.flattens(k)]
                                   for k in ['ta'] + self.keys()},
                'leader_external': bool(self.firing.leader_external)}
        self.assert_flat(LEADER)
        for key in self.keys():
            final = self.final(key)
            self.assertEqual((final['result'], final['exit_reason']), ('flat', 'leader:cbdr'), key)
            self.assert_flat(self.acct(key))
            self.assert_never_opposite(self.acct(key))
        times = self.liquidation_times(mark)
        self.assertEqual(sorted(a for a, _ in times), sorted([LEADER, FB, FC]))
        RESULTS['replay_liquidation_order'] = [{LEADER: 'ta', FB: 'tb', FC: 'tc'}[a] for a, _ in times]
        out = {}
        for key in ['ta'] + self.keys():
            recs = self.flattens(key)
            self.assertEqual(len(recs), 1, key)
            rec = recs[0]
            out[key] = (rec['outcome'], (rec.get('receipt') or {}).get('matched_by_liquidation_order'))
            self.assertEqual(rec['outcome'], 'accepted', (key, rec))
            self.assertTrue(rec.get('broker_order_id'), rec)
            oid = int(rec['broker_order_id'])
            self.assertEqual((self.fake.orders[oid]['accountId'], self.fake.versions[oid]['text']),
                             (self.acct(key), 'Exit'), key)
            self.assertEqual(self.journal(key).unresolved(), [], key)
        RESULTS['replay_flatten'] = out
        RESULTS['replay_liquidation_spread_ms'] = round((times[-1][1] - times[0][1]) * 1000, 1)
        t = self.trade
        self.assertEqual(t['terminal_reason'], 'cbdr')
        self.assertIsNone(t['native']['failed'])
        self.assertNotIn('exit_all_unverified', [a['code'] for a in t['native']['alerts']])
        self.assertEqual(self.firing.leader_external, {})
        self.assertEqual(self.alerts(), [])
        self.assertFalse((self.workspace / 'ENTRIES-BLOCKED-NATIVE-EXIT').exists())
        self.assertFalse(Path(self.cfg['kill_switch_file']).exists())

    def test_own_liquidation_reconciled_without_an_order_is_not_an_external_close(self):
        """If the leader's exit order still cannot be identified (reply without orderId, order never
        matched), the daemon reconciles it "flat with nothing working; no order". The firing service
        must not report the leader closed outside the bridge."""
        self.main._find_liquidation = lambda rec: None  # an exit order that is never matched, leader only
        p, mark = self.to_cbdr()
        until(lambda: any(c['outcome'] == 'accepted' for c in self.flattens('ta')), 20, 'leader flatten reconciled')
        rec = self.flattens('ta')[0]
        self.assertEqual((rec['outcome'], rec.get('broker_order_id'), rec['receipt'].get('note')),
                         ('accepted', None, 'flat with nothing working; no order'))
        for key in self.keys():
            final = self.final(key)
            self.assertEqual((final['result'], final['exit_reason']), ('flat', 'leader:cbdr'), key)
        time.sleep(6.0)  # well past 2 leader snapshots after the reconcile
        self.assertEqual(self.firing.leader_external, {})
        self.assertEqual([a for a in self.alerts() if a.get('reason') == 'leader_closed_externally'], [])
        report = self.firing.report.get(p['trade_id'])
        self.assertEqual(report['main']['leader'].get('own_liquidation'), 1)
        t = self.trade
        RESULTS['own_liquidation_unmatched_soup'] = {
            'phase': t['phase'], 'terminal_reason': t['terminal_reason'],
            'failed': (t['native']['failed'] or {}).get('reason'),
            'alerts': [a['code'] for a in t['native']['alerts']]}


def tearDownModule():
    import sys
    print('LIQUIDATION-RACE-RESULTS ' + json.dumps(RESULTS, sort_keys=True), file=sys.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
