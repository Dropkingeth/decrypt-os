"""Engines platform: the strategy adapter registry, non-Soup bracket_entry / modify_stop /
flatten_all through Soup's own order guards, no contract cap for any adapter (followers too), MES
bindings and the restart paths. Browser-free: test_fill_capture's exchange double; synthetic ids only."""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import harness  # noqa: F401  (puts the repository on sys.path; no browser is started here)
from harness import seed_market_price
from test_fill_capture import ACCOUNTS, CONTRACT, Bench, Exchange, p_bracket, p_entry
from tvbridge import adapters, daemon, followers
from tvbridge.adapters import Adapter, AdapterError
from tvbridge.executor import MARKET_PRICE_NAME, Executor
from tvbridge.journal import Journal

ADAPTER = 'test-gap-adapter-v2'
SCHEMA = 'test-gap-entry-v1'
MGMT = 'test-gap-management-v1'
TID = '3000:MESZ2021:1632147200000:long'
SHORT = '3000:MESZ2021:1632147200000:short'


def make_adapter(**kw):
    spec = dict(adapter_id=ADAPTER, label='Synthetic gap adapter', schemas=(SCHEMA, MGMT), roots=('MES', 'MNQ'),
                kinds=adapters.ORDER_KINDS)
    spec.update(kw)
    return Adapter(**spec)


class Registered(unittest.TestCase):
    """Each test gets a private registry: Soup plus the synthetic adapter."""

    def setUp(self):
        patcher = mock.patch.dict(adapters.REGISTRY, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        adapters.register(make_adapter())


class MesExchange(Exchange):
    def sync(self, epoch=None):
        self.epoch = epoch or self.epoch
        data = {'accounts': ACCOUNTS, 'orders': list(self.orders.values()), 'fills': list(self.fills.values()),
                'orderVersions': list(self.versions.values()), 'positions': list(self.positions.values()),
                'contracts': [{'id': CONTRACT, 'name': 'MESZ1'}]}
        self.state.load_sync(ACCOUNTS, copy.deepcopy(data), self.epoch)


class MesBench(Bench):
    """Bench with the adapter enabled on an MES binding (ticker MESZ2021, tick 0.25)."""

    def __init__(self, test, ticker='MESZ2021', price=5000.0, **cfg):
        cfg.setdefault('adapters', {ADAPTER: {'enabled': True}})
        with mock.patch('test_fill_capture.Exchange', MesExchange if ticker.startswith('MES') else Exchange):
            super().__init__(test, ticker=ticker, **cfg)
        self.ex.price = price
        for ex in [self.main] + [f for f in self.fx.values() if isinstance(f, Executor)]:
            seed_market_price(ex, price)


def cmd(kind, cid, tid=TID, ticker='MESZ2021', schema=SCHEMA, adapter_id=ADAPTER, signal_time=None, **fields):
    extras = {'adapter_id': adapter_id, 'adapter_schema': schema, 'command_id': cid, 'trade_id': tid}
    side = 'long' if tid.endswith(':long') else 'short'
    buy = 'buy' if side == 'long' else 'sell'
    if kind in ('entry', 'bracket_entry'):
        extras['signal_time'] = int(time.time() * 1000) if signal_time is None else signal_time
    if kind == 'entry':
        p = {'ticker': ticker, 'action': buy, 'orderType': 'market', 'quantity': fields.pop('quantity', 1)}
    elif kind == 'bracket_entry':
        p = {'ticker': ticker, 'action': buy, 'orderType': 'market', 'quantity': fields.pop('quantity', 3)}
        extras.update(bridge_kind='bracket_entry', reference_price=5000.0,
                      stop_price=4992.5 if side == 'long' else 5007.5)
    elif kind == 'tp1_limit':
        p = {'ticker': ticker, 'action': 'exit', 'orderType': 'limit', 'quantity': fields.pop('quantity', 1),
             'limitPrice': fields.pop('price', 5006.25), 'cancel': False}
    elif kind == 'exit_market':
        p = {'ticker': ticker, 'action': 'exit', 'orderType': 'market', 'quantity': fields.pop('quantity', 1),
             'cancel': False}
    elif kind == 'cancel':
        p = {'ticker': ticker, 'action': 'cancel'}
    elif kind == 'modify_stop':
        p = {'ticker': ticker, 'action': 'modify_stop'}
        extras.update(bridge_kind='modify_stop')
    else:
        p = {'ticker': ticker, 'action': 'flatten_all'}
        extras.update(bridge_kind='flatten_all')
    extras.update(fields)
    p['extras'] = extras
    return p


# --- 1. the registry ---------------------------------------------------------------------------

class Registry(Registered):
    def test_soup_is_the_registered_reference(self):
        soup = adapters.lookup(adapters.SOUP)
        self.assertTrue(soup.reference)
        self.assertEqual(soup.roots, ('MNQ',))
        self.assertEqual(soup.take_profits, 1)

    def test_unknown_id_or_schema_is_refused(self):
        with self.assertRaisesRegex(AdapterError, 'unknown adapter id'):
            adapters.lookup('no-such-adapter-v1')
        with self.assertRaisesRegex(AdapterError, 'not declared'):
            adapters.lookup(ADAPTER, 'test-gap-entry-v2')
        self.assertIs(adapters.lookup(ADAPTER, MGMT).adapter_id, ADAPTER)

    def test_bad_registrations_are_refused(self):
        bad = {'duplicate': make_adapter(),
               'id': make_adapter(adapter_id='Bad Id'),
               'unknown root': make_adapter(adapter_id='x-1', roots=('ES',)),
               'unknown kind': make_adapter(adapter_id='x-2', kinds=('entry', 'limit_entry')),
               'no entry kind': make_adapter(adapter_id='x-3', kinds=('cancel',)),
               'no schema': make_adapter(adapter_id='x-4', schemas=()),
               'multi tp without profile': make_adapter(adapter_id='x-5', take_profits=3),
               'too many tps': make_adapter(adapter_id='x-6', take_profits=4, profile='p-1')}
        for name, spec in bad.items():
            with self.subTest(name), self.assertRaises(AdapterError):
                adapters.register(spec)
        adapters.register(make_adapter(adapter_id='x-7', take_profits=3, profile='declared-3tp-1'))

    def test_binding_config_defaults_and_refusals(self):
        self.assertEqual(list(adapters.binding_adapters({'ticker': 'MNQZ2021'})), [adapters.SOUP])
        self.assertEqual(list(adapters.binding_adapters({'ticker': 'MESZ2021', 'adapters': {ADAPTER: {'enabled': True}}})),
                         [ADAPTER])
        self.assertEqual(adapters.binding_adapters({'ticker': 'MNQZ2021', 'adapters': {ADAPTER: {'enabled': False}}}), {})
        for cfg, fragment in (({'adapters': {'nope-v1': {'enabled': True}}}, 'not a registered adapter'),
                              ({'adapters': {ADAPTER: True}}, 'must be'),
                              ({'adapters': {ADAPTER: {'enabled': 'yes'}}}, 'must be'),
                              ({'adapters': {ADAPTER: {'enabled': True, 'max': 40}}}, 'must be'),
                              ({'adapters': []}, 'must be an object'),
                              ({'adapters': None}, 'must be an object'),
                              ({'adapters': {ADAPTER: {'enabled': True}, adapters.SOUP: {'enabled': True}}},
                               'at most one adapter'),
                              ({'ticker': 'MESZ2021'}, 'does not trade MES')):
            with self.subTest(cfg=cfg), self.assertRaisesRegex(AdapterError, fragment):
                adapters.binding_adapters(dict({'ticker': 'MNQZ2021'}, **cfg))


class Contracts(unittest.TestCase):
    def test_known_roots_and_ticks(self):
        self.assertEqual(adapters.check_contract('MNQZ2021', .25)['root'], 'MNQ')
        self.assertEqual(adapters.check_contract('MESH2022', 0.25)['root'], 'MES')
        for ticker, tick, fragment in (('ESZ2021', .25, 'not supported'), ('MYMZ2021', .25, 'not supported'),
                                       ('MESZ2021', .5, 'tick_size for MES must be 0.25'),
                                       ('MESZ2021', None, 'tick_size'), ('MESF2022', .25, 'HMUZ'),
                                       ('MES1!', .25, 'one dated contract'), ('MESZ21', .25, 'one dated contract'),
                                       (None, .25, 'one dated contract'), ('MESZ2021\n', .25, 'one dated contract')):
            with self.subTest(ticker=ticker), self.assertRaisesRegex(AdapterError, fragment):
                adapters.check_contract(ticker, tick)

    def test_executor_refuses_unknown_root_or_tick_at_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {'account_name': 'FAKE', 'max_contracts': 2, 'kill_switch_file': tmp + '/K'}
            for ticker, tick in (('ESZ2021', .25), ('MNQZ2021', .5), ('MNQZ2021', None)):
                with self.subTest(ticker=ticker, tick=tick), self.assertRaises(ValueError):
                    Executor(object(), object(), Journal(tmp), dict(base, ticker=ticker, tick_size=tick))
            # Soup (the default adapter) trades MNQ only: an MES binding needs an MES adapter.
            with self.assertRaisesRegex(ValueError, 'does not trade MES'):
                Executor(object(), object(), Journal(tmp), dict(base, ticker='MESZ2021', tick_size=.25))


# --- 2. non-Soup commands through Soup's own guards ---------------------------------------------

class AdapterOrders(Registered):
    def setUp(self):
        super().setUp()
        self.b = MesBench(self)
        self.main = self.b.main

    def refused(self, payload, fragment, executor=None):
        before = len(self.b.ex.requests)
        outcome, receipt = (executor or self.main).send(payload)
        self.assertEqual(outcome, 'rejected', receipt)
        self.assertEqual(receipt.get('bridge'), 'refused')
        self.assertIn(fragment, receipt['reason'])
        self.assertEqual(self.b.ex.requests[before:], [], 'a refused command sends nothing')
        return receipt

    def test_mes_bracket_modify_flatten_lifecycle(self):
        out, receipt = self.main.send(cmd('bracket_entry', 'g-1', tp1_price=5011.25, tp1_quantity=1))
        self.assertEqual(out, 'accepted', receipt)
        self.assertEqual(self.b.ex.requests[0][0], 'order/placeoso')
        body = self.b.ex.requests[0][1]
        self.assertEqual((body['symbol'], body['orderQty'], body['bracket1']['stopPrice']), ('MESZ1', 3, 4992.5))
        self.assertEqual(receipt['tp1']['outcome'], 'accepted')     # staged TP1 for 1 of 3
        rec = self.main.journal.get('g-1')
        self.assertEqual((rec['kind'], rec['adapter'], rec['trade_id']), ('bracket_entry', ADAPTER, TID))
        stop = int(rec['legs']['stop'])
        out, receipt = self.main.send(cmd('modify_stop', 'g-2', stop_price=5000.0, stop_quantity=3))
        self.assertEqual(out, 'accepted', receipt)
        self.assertEqual(self.b.ex.latest(stop)['stopPrice'], 5000.0)
        out, receipt = self.main.send(cmd('flatten_all', 'g-3'))
        self.assertEqual(out, 'accepted', receipt)
        self.assertEqual(receipt['mode'], 'liquidate')
        self.assertEqual(self.b.ex.net(7), 0)

    def test_plain_entry_tp1_and_exit(self):
        self.assertEqual(self.main.send(cmd('entry', 'p-1', quantity=2))[0], 'accepted')
        out, receipt = self.main.send(cmd('tp1_limit', 'p-2', quantity=1, price=5006.25))
        self.assertEqual(out, 'accepted', receipt)
        # TP1 is the only resting take-profit: a second limit is refused (rulebook), never stacked.
        self.refused(cmd('tp1_limit', 'p-3', quantity=1, price=5012.5), 'other orders are working')
        self.assertEqual(self.main.send(cmd('cancel', 'p-4'))[0], 'accepted')
        self.assertEqual(self.main.send(cmd('exit_market', 'p-5', quantity=2))[0], 'accepted')
        self.assertEqual(self.b.ex.net(7), 0)

    def test_soup_payloads_are_refused_on_an_adapter_binding(self):
        soup = p_bracket('s-1', TID)
        soup['ticker'] = 'MESZ2021'
        self.refused(soup, 'Soup orders are not enabled on this binding')

    def test_identity_and_format_refusals(self):
        cases = [
            (cmd('entry', 'r-1', adapter_id='other-adapter-v1'), 'unknown adapter id'),
            (cmd('entry', 'r-2', schema='test-gap-entry-v9'), 'not declared'),
            (cmd('entry', 'r-3', ticker='MNQZ2021'), 'pinned ticker'),
            (cmd('entry', 'r-4', tid='3000:MESZ2021:1'), 'trade_id'),
            (cmd('entry', 'r-5', note='x'), 'unexpected extras'),
            (cmd('entry', 'r#6'), 'reserved'),
            (cmd('entry', 'r-7', quantity=1.0), 'invalid quantity'),
            (cmd('entry', 'r-8', signal_time='now'), 'signal_time'),
            (dict(cmd('entry', 'r-9'), orderType='limit'), 'market orders only'),
            (dict(cmd('entry', 'r-10'), signalPrice=5000.0), 'missing or unexpected fields'),
            (dict(cmd('exit_market', 'r-11'), cancel=True), 'cancel:false'),
        ]
        no_sig = cmd('entry', 'r-12')
        del no_sig['extras']['signal_time']
        cases.append((no_sig, 'missing or unexpected extras'))
        for payload, fragment in cases:
            with self.subTest(fragment):
                self.refused(payload, fragment)

    def test_undeclared_kind_and_disabled_adapter(self):
        with mock.patch.dict(adapters.REGISTRY, {}):
            adapters.register(make_adapter(adapter_id='entry-only-v1', kinds=('entry', 'exit_market', 'cancel')))
            b = MesBench(self, adapters={'entry-only-v1': {'enabled': True}})
            self.refused(cmd('bracket_entry', 'k-1', adapter_id='entry-only-v1'),
                         'order kind bracket_entry is not declared', b.main)
        b = MesBench(self, adapters={ADAPTER: {'enabled': False}})
        self.refused(cmd('entry', 'k-2'), 'not enabled on this binding', b.main)

    def test_extended_kinds_still_need_their_bridge_flags(self):
        b = MesBench(self, bracket_orders_enabled=False)
        self.refused(cmd('bracket_entry', 'f-1'), 'bracket_entry is disabled', b.main)

    def test_one_trade_at_a_time_and_no_reversal(self):
        self.assertEqual(self.main.send(cmd('entry', 'o-1', quantity=2))[0], 'accepted')
        self.refused(cmd('entry', 'o-2', tid='3000:MESZ2021:1632147260000:long'), 'flat contract')
        self.refused(cmd('entry', 'o-3', tid=SHORT), 'flat contract')
        self.refused(cmd('exit_market', 'o-4', quantity=3), 'exceeds live position')

    def test_kill_switch_and_its_fail_closed_states_refuse_adapter_entries(self):
        Path(self.b.cfg['kill_switch_file']).write_text('{}')
        self.refused(cmd('bracket_entry', 'ks-1'), 'entries disabled by kill switch')
        os.unlink(self.b.cfg['kill_switch_file'])
        self.main.cfg['kill_switch_file'] = None
        self.refused(cmd('bracket_entry', 'ks-2'), 'kill_switch_not_configured')

    def test_anti_hedge_spans_mes_and_mnq(self):
        # Another account of the login holds a short MNQ: a long MES entry is a hedge (same group).
        self.b.ex.positions[99] = {'id': 99, 'accountId': 8, 'contractId': 5, 'netPos': -1,
                                   'tradeDate': {'year': 2021, 'month': 9, 'day': 28}}
        data = {'accounts': ACCOUNTS, 'orders': [], 'fills': [], 'orderVersions': [],
                'positions': list(self.b.ex.positions.values()),
                'contracts': [{'id': CONTRACT, 'name': 'MESZ1'}, {'id': 5, 'name': 'MNQZ1'}]}
        self.b.state.load_sync(ACCOUNTS, data, self.b.ex.epoch)
        self.refused(cmd('bracket_entry', 'h-1'), 'hedge guard: long MES entry refused')

    def test_cross_login_hedge_m4_applies_to_adapter_entries(self):
        from tvbridge.peers import PeerHedge
        peer = {'id': 'b', 'url': 'http://127.0.0.1:1', 'token_env': 'TVBRIDGE_TOKEN_B'}
        report = {'schema': 'decrypt.tvbridge-login.v1', 'page': {'logged_in': True},
                  'accounts': [{'name': 'X', 'id': 1, 'role': 'main', 'working': [],
                                'positions': [{'contract': 'MNQZ1', 'root': 'MNQ', 'net': -1}]}]}
        state = {'fetch': None}

        def fetch(url, timeout):
            return state['fetch']()
        self.main.peer_hedge = PeerHedge([peer], own_token='a' * 32, environ={'TVBRIDGE_TOKEN_B': 'b' * 32},
                                         fetch=fetch)
        # Not polled yet: unknown exposure on login B refuses the entry (fails closed).
        self.refused(cmd('bracket_entry', 'm4-1'), 'hedge_peer_unknown:b:not_polled_yet')
        # Login B holds a short MNQ: a long MES entry here is a cross-login hedge.
        state['fetch'] = lambda: report
        self.main.peer_hedge.poll_once()
        self.refused(cmd('bracket_entry', 'm4-2'), 'hedge_conflict')
        self.assertEqual(self.main.send(cmd('bracket_entry', 'm4-3', tid=SHORT))[0], 'accepted')


# --- 3. no size cap for any adapter: the strategy alert sets the size ----------------------------

class AdapterNoCap(Registered):
    def test_large_entry_is_sent_as_is(self):
        b = MesBench(self, max_contracts=4)  # a legacy value is ignored, never a limit
        out, receipt = b.main.send(cmd('bracket_entry', 'cap-1', quantity=40))
        self.assertEqual(out, 'accepted', receipt)
        self.assertEqual(len(b.ex.requests), 1)

    def test_legacy_key_any_positive_value_starts_and_malformed_refuses(self):
        for good in (11, 40):
            with self.subTest(good):
                MesBench(self, max_contracts=good)
        for bad in (0, -1, '4'):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, 'max_contracts is retired'):
                MesBench(self, max_contracts=bad)
        b = MesBench(self)
        for bad in (0, -2, 1.5, True):
            with self.subTest(quantity=bad):
                out, receipt = b.main.send(cmd('entry', f'cap-q-{bad!r}', quantity=bad))
                self.assertEqual(out, 'rejected')
        self.assertEqual(b.ex.requests, [])

    def test_followers_take_the_same_adapter_and_the_alert_size(self):
        b = MesBench(self, with_followers=True)
        f = b.fx['account-b']
        f.cfg['max_contracts'] = 2  # legacy value, ignored
        self.assertEqual(f.send(cmd('entry', 'fc-1', quantity=12))[0], 'accepted')
        self.assertEqual(f.adapters, b.main.adapters)
        self.assertEqual(f.journal.get('f:account-b:fc-1')['adapter'], ADAPTER)
        soup = p_entry('fs-1', TID, 1)
        soup['ticker'] = 'MESZ2021'
        self.assertIn('Soup orders are not enabled', f.send(soup)[1]['reason'])


# --- 4. price watching per contract and the restart paths ----------------------------------

class RestartAndPrice(Registered):
    def test_saved_price_is_per_contract(self):
        b = MesBench(self)
        path = Path(b.main.journal.dir) / MARKET_PRICE_NAME
        path.write_text(json.dumps({'symbol': 'MNQZ1', 'price': 12000.0, 'at_ms': b.main.clock_ms()}))
        out, receipt = b.main.send(cmd('bracket_entry', 'mp-1'))
        self.assertIn('reference_price cannot be checked', receipt['reason'])
        seed_market_price(b.main, 5000.0)
        self.assertEqual(json.loads(path.read_text())['symbol'], 'MESZ1')
        self.assertEqual(b.main.market_price(), (5000.0, 'last known fill'))
        self.assertEqual(b.main.send(cmd('bracket_entry', 'mp-2'))[0], 'accepted')
        # A fill on the binding's own contract is watched and saved for the next restart.
        self.assertEqual(json.loads(path.read_text())['symbol'], 'MESZ1')

    def test_garbage_reference_is_refused_against_the_mes_price(self):
        b = MesBench(self)
        p = cmd('bracket_entry', 'gr-1')
        p['extras'].update(reference_price=12000.0, stop_price=11992.5)
        out, receipt = b.main.send(p)
        self.assertIn('from the last known fill 5000.0', receipt['reason'])

    def test_expired_or_future_signal_is_never_sent(self):
        b = MesBench(self)
        now = int(time.time() * 1000)
        for at, fragment in ((now - 121_000, 's old'), (now - 3_600_000, 'never sent'), (now + 30_000, 'ahead')):
            with self.subTest(at=at):
                out, receipt = b.main.send(cmd('bracket_entry', f'old-{at}', signal_time=at))
                self.assertEqual(out, 'rejected')
                self.assertIn(fragment, receipt['reason'])
        self.assertEqual(b.ex.requests, [])
        refusals = [json.loads(x) for x in (Path(b.main.journal.dir) / 'refusals.jsonl').read_text().splitlines()]
        self.assertEqual([r['command_id'] for r in refusals], [f'old-{now - 121_000}', f'old-{now - 3_600_000}',
                                                               f'old-{now + 30_000}'])
        self.assertEqual({r['trade_id'] for r in refusals}, {TID})
        self.assertEqual(b.main.send(cmd('bracket_entry', 'fresh', signal_time=now - 100_000))[0], 'accepted')

    def test_dedupe_survives_a_restart_and_nothing_is_resent(self):
        b = MesBench(self)
        first = cmd('bracket_entry', 'dd-1')
        self.assertEqual(b.main.send(first)[0], 'accepted')
        sent = len(b.ex.requests)
        again = Executor(b.ex, b.state, Journal(b.cfg['journal_dir']), b.cfg)   # restart: same journal
        again.resolve()
        out, receipt = again.send(first)
        self.assertEqual((out, receipt.get('duplicate')), ('accepted', True))
        # The same trade cannot enter twice under a new command id either, even once flat again.
        out, receipt = again.send(cmd('flatten_all', 'dd-2'))
        self.assertEqual(out, 'accepted')
        out, receipt = again.send(cmd('bracket_entry', 'dd-3'))
        self.assertEqual(out, 'rejected')
        self.assertIn('trade already has an entry', receipt['reason'])
        self.assertEqual([e for e, _ in b.ex.requests[sent:]], ['order/liquidateposition'])

    def test_unknown_outcome_after_a_restart_blocks_adapter_entries(self):
        b = MesBench(self)
        b.main.journal.start('u-1', trade_id=TID, kind='bracket_entry', side='long', quantity=1, tag='DLx',
                             at=b.main.clock_ms(), adapter=ADAPTER)
        again = Executor(b.ex, b.state, Journal(b.cfg['journal_dir']), b.cfg)
        again.resolve()
        out, receipt = again.send(cmd('bracket_entry', 'u-2', tid='3000:MESZ2021:1632147260000:long'))
        self.assertIn('unknown outcome', receipt['reason'])


# --- 5. the multi-TP hook (declared profiles only; no profile is built here) ---------------------

class TakeProfitHook(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(adapters.REGISTRY, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        adapters.register(make_adapter(take_profits=2, profile='synthetic-two-tp-1'))

    def test_declared_profile_rests_up_to_its_take_profits_within_the_position(self):
        b = MesBench(self)
        self.assertEqual(b.main.send(cmd('entry', 't-1', quantity=3))[0], 'accepted')
        self.assertEqual(b.main.send(cmd('tp1_limit', 't-2', quantity=1, price=5006.25))[0], 'accepted')
        out, receipt = b.main.send(cmd('tp1_limit', 't-3', quantity=3, price=5012.5))
        self.assertIn('exceeds live position', receipt['reason'])        # 1 resting + 3 > 3 held
        self.assertEqual(b.main.send(cmd('tp1_limit', 't-4', quantity=2, price=5012.5))[0], 'accepted')
        out, receipt = b.main.send(cmd('tp1_limit', 't-5', quantity=1, price=5018.75))
        self.assertIn('its profile allows 2', receipt['reason'])


# --- 6. the daemon refuses a bad contract or adapter config before the browser ------------------

class DaemonStart(Registered):
    def run_main(self, cfg):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'config.json'
        path.write_text(json.dumps(cfg))
        with mock.patch.object(sys, 'argv', ['tvbridge.daemon', '--config', str(path)]), \
                mock.patch.object(daemon, 'Bridge', side_effect=AssertionError('browser touched')), \
                mock.patch.dict(os.environ, {'TVBRIDGE_TOKEN': 'x' * 32}):
            with self.assertRaises(SystemExit) as ctx:
                daemon.main()
        return str(ctx.exception)

    def test_refusals_before_the_browser(self):
        base = {'ticker': 'MNQZ2021', 'tick_size': .25, 'max_contracts': 2}
        for extra, fragment in (({'ticker': 'ESZ2021'}, 'not supported'), ({'tick_size': 1}, 'tick_size'),
                                ({'adapters': {'nope-v1': {'enabled': True}}}, 'not a registered adapter'),
                                ({'ticker': 'MESZ2021'}, 'does not trade MES')):
            with self.subTest(extra=extra):
                msg = self.run_main(dict(base, **extra))
                self.assertIn(fragment, msg)
                self.assertIn('does not start', msg)

    def test_state_reports_the_binding_adapter(self):
        b = MesBench(self)
        self.assertEqual(daemon.account_state(b.main, extended=True)['adapter_ids'], [ADAPTER])
        self.assertNotIn('adapter_ids', daemon.account_state(b.main))


class CopyInRuntime(unittest.TestCase):
    def test_runtime_copy_is_byte_identical(self):
        here = Path(adapters.__file__)
        app = here.parents[4] / 'app' / 'trading-runtime' / 'decrypt_trading' / 'bridge_adapters.py'
        self.assertEqual(app.read_bytes(), here.read_bytes())


if __name__ == '__main__':
    unittest.main()
