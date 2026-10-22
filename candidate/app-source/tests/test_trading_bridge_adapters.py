"""beta4 engines platform, runtime side: a registered non-Soup adapter on an MES binding trades through the
same bridge route as Soup (fake bridge, synthetic decrypt-bridge-v2 test strategy and ids, no network)."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_fixture import FakeBridge, native_binding  # noqa: E402
import bridge_v2_example_strategy as engine_module  # noqa: E402
import bridge_v2_payloads as payloads  # noqa: E402
from decrypt_trading import RuntimeManager, RuntimeError as EngineError  # noqa: E402
from decrypt_trading import bridge_adapters, bridge_route, source  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402
from decrypt_trading.bridge_route import BindingError  # noqa: E402

BASE = payloads.BASE
ADAPTER = engine_module.STRATEGY.adapter_id
TID = 'ex1:MESZ2026:%d:long' % (BASE - 60_000)
MODULE_REGISTRY = (Path(__file__).resolve().parents[1] / 'modules/tradovate-bridge/bridge/tvbridge/adapters.py')
COPIED_REGISTRY = (Path(__file__).resolve().parents[1] / 'app/trading-runtime/decrypt_trading/bridge_adapters.py')
ENGINE_SHA = hashlib.sha256(Path(engine_module.__file__).read_bytes()).hexdigest()
STRATEGY_SHA = '5' * 64


def mes_binding(pair=ADAPTER, symbol='MESZ2026', root='MES', tick=.25):
    value = native_binding(name='adapter-fixture', pair=pair)
    value.update(symbol=symbol, root=root, tickSize=tick)
    value['routeSha256'] = contract.route_digest(value)
    return value


def entry(timestamp, side='long', qty=3, runner=1):
    """A decrypt-bridge-v2 market entry on MESZ2026: stop 10 points away, two resting targets and a runner."""
    sign = 1 if side == 'long' else -1
    return payloads.entry(side, ts=timestamp, trade_id=TID if side == 'long' else TID[:-4] + 'short',
                          symbol='MESZ2026', entry={'qty': qty, 'price': 6800.}, stop={'price': 6800 - sign * 10},
                          targets=[{'price': 6800 + sign * 10, 'qty': 1, 'type': 'limit'},
                                   {'price': 6800 + sign * 20, 'qty': 1, 'type': 'limit'}],
                          runner={'qty': runner}, break_even={'trigger_price': 6800 + sign * 10})


class Registered(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for patcher in (patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')),
                        patch.dict(bridge_adapters.REGISTRY, {}), patch.dict(source.BRIDGE_ENGINES, {}),
                        patch.dict(contract.PAIRS, {ADAPTER: (ENGINE_SHA, STRATEGY_SHA)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge_adapters.register(bridge_adapters.Adapter(
            ADAPTER, 'Synthetic test adapter', schemas=(bridge_adapters.V2_SCHEMA,), roots=('MES',),
            kinds=bridge_adapters.V2_KINDS, take_profits=3, profile=bridge_adapters.V2_PROFILE))
        source.register_bridge_engine(ADAPTER, module='bridge_v2_example_strategy',
                                      files={'bridge_v2_example_strategy.py': ENGINE_SHA}, strategy_sha256=STRATEGY_SHA)
        self.now = BASE
        self.root = Path(self.temp.name)
        self.binding = mes_binding()
        self.bridge = FakeBridge(self.root / 'data', self.binding, clock=lambda: self.now)
        self.bridge.write_config(adapters={ADAPTER: {'enabled': True}})
        self.bridge.state_override = {'adapter_ids': [ADAPTER]}


class Contracts(Registered):
    def test_copied_registry_matches_the_bridge_module(self):
        self.assertEqual(COPIED_REGISTRY.read_bytes(), MODULE_REGISTRY.read_bytes())

    def test_mes_binding_needs_an_adapter_that_declares_mes(self):
        self.assertEqual(contract.validate_binding(self.binding)['symbol'], 'MESZ2026')
        for change, reason in (({'pair': contract.SOUP}, 'native_contract'),
                               ({'symbol': 'ESZ2026', 'root': 'ES'}, 'native_contract'),
                               ({'symbol': 'MESZ2026', 'root': 'MNQ'}, 'native_contract'),
                               ({'symbol': 'MESF2027'}, 'native_contract'),
                               ({'tick': .5}, 'native_tick')):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, reason):
                contract.validate_binding(mes_binding(**change))
        # Soup keeps its dated MNQ contract exactly as before.
        self.assertEqual(contract.validate_binding(native_binding())['root'], 'MNQ')

    def test_pins_are_checked_before_the_engine_loads(self):
        with self.assertRaisesRegex(ValueError, 'already registered'):
            source.register_bridge_engine(ADAPTER, module='bridge_v2_example_strategy',
                                          files={'bridge_v2_example_strategy.py': ENGINE_SHA}, strategy_sha256=STRATEGY_SHA)
        with self.assertRaisesRegex(ValueError, 'unknown adapter id'):
            source.register_bridge_engine('other-adapter-v2', module='bridge_v2_example_strategy',
                                          files={'bridge_v2_example_strategy.py': ENGINE_SHA}, strategy_sha256=STRATEGY_SHA)
        source.BRIDGE_ENGINES[ADAPTER]['files'] = {'bridge_v2_example_strategy.py': '0' * 64}
        with self.assertRaisesRegex(ValueError, 'differs from its pinned files'):
            source.load_pair_sources(ADAPTER)


class Route(Registered):
    def test_open_route_needs_the_adapter_registered_and_enabled(self):
        transport = self.bridge.opener(self.binding)
        self.assertEqual(transport.route.entry_kinds, bridge_adapters.ENTRY_KINDS)
        self.bridge.write_config(adapters={ADAPTER: {'enabled': False}})
        with self.assertRaisesRegex(BindingError, 'bridge_adapter_not_enabled'):
            self.bridge.opener(self.binding)
        self.bridge.write_config()          # no adapters key: Soup only, and Soup does not trade MES
        with self.assertRaisesRegex(BindingError, 'bridge_adapters_invalid'):
            self.bridge.opener(self.binding)
        self.bridge.write_config(adapters={ADAPTER: {'enabled': True}, 'nope-v2': {'enabled': True}})
        with self.assertRaisesRegex(BindingError, 'bridge_adapters_invalid'):
            self.bridge.opener(self.binding)
        del bridge_adapters.REGISTRY[ADAPTER]   # unregistered: MES is no longer a declared contract either
        with self.assertRaisesRegex(ValueError, 'native_contract'):
            self.bridge.opener(self.binding)

    def test_state_must_name_the_adapter(self):
        transport = self.bridge.opener(self.binding)
        self.bridge.state_override = {}
        transport.route.latest = None
        self.assertEqual(transport.problem(), 'bridge_adapter_mismatch')
        self.bridge.state_override = {'adapter_ids': [contract.SOUP]}
        transport.route.latest = None
        self.assertEqual(transport.problem(), 'bridge_adapter_mismatch')


class RuntimeBase(Registered):
    def setUp(self):
        super().setUp()
        self.manager = RuntimeManager(self.root / 'trading')
        self.manager.install(self.binding)
        self.open()

    def open(self):
        self.transport = self.bridge.opener(self.binding)
        self.runtime = self.manager.open(self.binding['instanceId'], transport=self.transport, clock=lambda: self.now)
        self.addCleanup(self.runtime.close)

    def ingest(self, channel, value, step=True):
        producer = self.binding['signalProducerId' if channel == 'pine' else 'feedProducerId']
        self.runtime.ingest(channel, json.dumps(value), producer_id=producer)
        if step:
            self.runtime.step()

    def enter(self, timestamp=None):
        self.ingest('quote', {'event_id': 'quote-%d' % self.now, 'symbol': 'MESZ2026', 'time': self.now,
                              'price': 6800.})
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', entry(self.now if timestamp is None else timestamp))


class RuntimeOnBridge(RuntimeBase):
    def test_only_the_bridge_files_an_adapter_needs(self):
        soup = self.bridge.soup
        self.assertFalse(os.path.lexists(soup / 'current'), 'the Soup firing order never reads this engine')
        self.assertFalse((soup / 'quantity-policy.json').exists())
        self.assertEqual(json.loads((soup / 'pairing.json').read_text())['ticker'], 'MESZ2026')

    def test_entry_waits_for_fresh_flat_state_then_sends_the_adapter_bracket(self):
        self.enter()
        self.assertEqual(self.bridge.sent, [], 'the first gate call only asks for fresh state')
        self.transport.route.refresh()
        self.runtime.step()
        sent = self.bridge.sent
        self.assertEqual([p['extras'].get('bridge_kind') for p in sent], ['bracket_entry'])
        bracket = sent[0]
        self.assertEqual(bracket['ticker'], 'MESZ2026')
        self.assertEqual((bracket['quantity'], bracket['extras']['stop_price'], bracket['extras']['signal_time']),
                         (3, 6790., BASE))
        self.assertEqual((bracket['extras']['adapter_id'], bracket['extras']['trade_id']), (ADAPTER, TID))
        # The first resting target rides with the bracket; the rest follow once the entry fills.
        self.assertEqual((bracket['extras']['tp1_price'], bracket['extras']['tp1_quantity']), (6810., 1))
        for payload in sent:   # what left the runtime passes the bridge's own format check
            bridge_adapters.validate_command(payload, ticker='MESZ2026',
                                             enabled={ADAPTER: bridge_adapters.REGISTRY[ADAPTER]})

    def test_entry_not_flat_is_held_then_dropped_after_120_s(self):
        self.bridge.state_override = {'adapter_ids': [ADAPTER], 'net_position': 1}
        self.enter()
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual(self.bridge.sent, [])
        self.now += 121_000
        self.runtime.step()
        self.assertEqual(self.bridge.sent, [])
        trade = self.runtime.engine.data['trade']
        self.assertEqual((trade['phase'], trade['closed_reason']), ('closed', 'stale_entry_not_replayed'))

    def test_stale_price_holds_the_entry(self):
        self.ingest('quote', {'event_id': 'q-old', 'symbol': 'MESZ2026', 'time': self.now, 'price': 6800.})
        self.now += 2500                                   # the last quote is now older than 2 s
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', entry(self.now))
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual(self.bridge.sent, [])
        self.ingest('quote', {'event_id': 'q-new', 'symbol': 'MESZ2026', 'time': self.now, 'price': 6800.})
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual([p['extras'].get('bridge_kind') for p in self.bridge.sent], ['bracket_entry'])

    def test_an_undeclared_alert_schema_is_refused_at_intake(self):
        other = entry(self.now)
        other['protocol'] = 'decrypt-bridge-v3'
        with self.assertRaisesRegex(EngineError, 'not declared by this adapter'):
            self.ingest('pine', other)
        count = self.runtime.engine.db.execute("SELECT count(*) FROM inbox WHERE channel='pine'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_large_entry_is_sent_as_is(self):
        # No contract cap: the strategy alert sets the size.
        big = entry(self.now, qty=11, runner=9)
        self.ingest('quote', {'event_id': 'q-1', 'symbol': 'MESZ2026', 'time': self.now, 'price': 6800.})
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', big)
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual([x['quantity'] for x in self.bridge.sent][:1], [11])
        self.assertNotEqual(self.runtime.engine.data['trade']['phase'], 'closed')

    def test_a_malformed_adapter_command_is_never_sent(self):
        with patch.object(engine_module, 'bridge_message',
                          lambda c, t, s, tr: {'ticker': t, 'action': 'buy', 'orderType': 'limit', 'extras': {}}):
            self.enter()
            self.transport.route.refresh()
            self.runtime.step()
        self.assertEqual(self.bridge.sent, [])
        command = next(iter(self.runtime.engine.data['commands'].values()))
        self.assertEqual(command['status'], 'rejected')
        self.assertIn('adapter_command_refused', command['receipt']['reason'])

    def test_restart_never_replays_a_pending_entry(self):
        self.ingest('quote', {'event_id': 'q-2', 'symbol': 'MESZ2026', 'time': self.now, 'price': 6800.})
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', entry(self.now), step=False)        # admitted, not yet processed
        self.runtime.close()
        self.open()
        rows = self.runtime.engine.db.execute("SELECT status,detail FROM inbox WHERE channel='pine'").fetchall()
        self.assertEqual(rows, [('rejected', 'restart_entry_not_replayed')])
        self.runtime.set_controls(enabled=True, locked=False)
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual(self.bridge.sent, [])
        # The same alert sent again is a duplicate of the refused one, never a new trade.
        self.ingest('pine', entry(self.now))
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual(self.bridge.sent, [])

    def test_soup_route_is_unchanged_for_a_soup_binding(self):
        soup = native_binding(name='soup-fixture', account=54321)
        bridge = FakeBridge(self.root / 'soup-data', soup, clock=lambda: self.now)
        transport = bridge.opener(soup)
        self.assertEqual(transport.route.entry_kinds, ('entry',))
        self.assertEqual(bridge_route.expected_account(soup)['adapter_id'], contract.SOUP)
        self.assertIsNone(transport.problem(), 'a Soup bridge without adapter_ids in /state still matches')


class MesPriceWatch(RuntimeBase):
    """The native chart feed watches the binding's own contract; prices never create a trade or an order."""

    def browser(self, symbol):
        from test_trading_runtime_adapters import FakeBrowser

        class Browser(FakeBrowser):
            def health(inner):
                value = FakeBrowser.health(inner)
                full = 'CME_MINI:' + symbol
                value.update(series_symbol=full, symbol_info={'full_name': full, 'minmov': 25, 'pricescale': 100})
                return value
        return Browser(lambda: self.now)

    def feed(self, symbol):
        from decrypt_trading.native_market import NativeMarketFeed
        browser = self.browser(symbol)
        feed = NativeMarketFeed(self.runtime, browser, chart_url=browser.url, target_id=browser.target)
        return feed, browser

    def test_mes_chart_feeds_fresh_quotes_and_nothing_else(self):
        feed, browser = self.feed('MESZ2026')
        feed.install()
        self.addCleanup(feed.close)
        browser.packets = [browser.packet(6800.25)]
        self.assertEqual(feed.tick()['state'], 'fresh')
        self.runtime.set_controls(enabled=True, locked=False)
        self.transport.route.refresh()
        self.runtime.step()
        self.assertEqual(self.runtime.engine.data['last_quote'], {'time': self.now, 'price': 6800.25})
        self.assertIsNone(self.runtime.engine.data['trade'], 'a price never opens a trade')
        self.assertEqual((self.runtime.engine.data['commands'], self.bridge.sent), ({}, []))
        self.now += 2001
        self.assertEqual(feed.tick()['state'], 'hold', 'a quote older than 2 s is stale')

    def test_a_chart_of_another_contract_is_refused(self):
        feed, _ = self.feed('MNQZ2026')
        with self.assertRaises(EngineError):
            feed.install()


if __name__ == '__main__':
    unittest.main()
