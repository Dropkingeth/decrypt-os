"""decrypt-bridge-v2: the envelope validator, the extras hook and the strategy registry (no network)."""
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/trading-runtime'))
import bridge_v2_example_strategy as example  # noqa: E402
from bridge_v2_payloads import BASE, TRADE, entry, event  # noqa: E402
from decrypt_trading import bridge_adapters, bridge_v2, bridge_v2_registry as registry, source  # noqa: E402
from decrypt_trading import bridge_v2_contract as v2  # noqa: E402
from decrypt_trading import customer_contract as contract  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'app/trading-runtime/decrypt_trading'
SPEC = ROOT / 'modules/tradovate-bridge/bridge/PAYLOAD-V2.md'
S = example.STRATEGY


def ok(payload, now=BASE + 1000):
    return v2.validate(payload, strategy=S, now_ms=now)


class Envelope(unittest.TestCase):
    def refused(self, payload, pattern, now=BASE + 1000):
        with self.assertRaisesRegex(v2.PayloadError, pattern):
            ok(payload, now)

    def test_full_entry_with_every_feature_on_is_valid_and_small(self):
        p = entry(touch={'enabled': True, 'price': 21000.0, 'valid_until': BASE + 3_600_000, 'cancel_if_stop_touched': True},
                  stop={'type': 'bar_close', 'timeframe_s': 60, 'safety_price': 20950.0},
                  targets=[{'price': 21010.0, 'qty': 1, 'type': 'limit'}, {'price': 21020.0, 'qty': 1, 'type': 'limit'},
                           {'price': 21030.0, 'qty': 1, 'type': 'limit'}, {'price': 21040.0, 'qty': 1, 'type': 'market'}],
                  runner={'qty': 2, 'mode': 'trail', 'trail_activation_ticks': 80, 'trail_distance_ticks': 40},
                  break_even={'enabled': True, 'trigger_target': 1, 'trigger_mode': 'all', 'confirm': 'bar_close',
                              'confirm_timeframe_s': 120, 'stop_type': 'bar_close', 'stop_timeframe_s': 30},
                  eod={'enabled': True}, daily_loss={'enabled': True, 'include_open': True, 'flatten': True},
                  bias_flip={'enabled': True}, extras={'setup': 'C', 'risk_usd': 480.5})
        p['entry'] = dict(p['entry'], type='touch', qty=6)
        self.assertIs(ok(p), p)
        self.assertLess(len(v2.compact(p).encode()), 4096)
        self.assertEqual(v2.parse(v2.compact(p)), p)

    def test_no_size_cap_only_a_positive_whole_number(self):
        ok(entry(entry={'qty': 500}, runner={'qty': 498}))
        for bad in (0, -1, 2.0, True, '4'):
            self.refused(entry(entry={'qty': bad}), 'entry.qty')
        self.refused(entry(runner={'qty': 3}), 'targets qty \\+ runner.qty must equal entry.qty')
        self.refused(entry(targets=[{'price': 21010.0, 'qty': 0, 'type': 'limit'}], runner={'qty': 4}), 'qty')

    def test_unknown_or_missing_fields_fail_closed(self):
        p = entry()
        p['note'] = 'x'
        self.refused(p, 'unknown field\\(s\\): note')
        p = entry()
        del p['eod']
        self.refused(p, 'missing field\\(s\\): eod')
        for block in ('entry', 'touch', 'stop', 'runner', 'break_even', 'eod', 'daily_loss', 'bias_flip'):
            p = entry()
            p[block] = dict(p[block], surprise=1)
            self.refused(p, block + ' must have exactly')
        p = entry()
        p['targets'][0]['note'] = 1
        self.refused(p, 'targets\\[1\\] must have exactly')
        self.refused(entry(protocol='decrypt-bridge-v1'), 'protocol')
        self.refused(entry(event='entry_v2'), 'event must be one of')
        self.refused(entry(strategy='other-strategy'), 'not this binding')

    def test_identity_contract_and_time(self):
        self.refused(entry(symbol='MNQ1!'), 'never a continuous symbol')
        self.refused(entry(symbol='MNQZ26'), 'symbol')
        self.refused(entry(symbol='MNQF2027'), 'symbol')
        self.refused(entry(symbol='NQZ2026'), 'symbol')
        ok(entry(symbol='MESZ2026'))
        self.refused(entry(trade_id='ex1:abc:short', alert_id='a1'), 'trade_id')
        self.refused(entry(trade_id='bad id:long', alert_id='a1'), 'trade_id')
        self.refused(entry(alert_id='a' * 161), 'alert_id')
        self.refused(entry(stream_id='bad stream'), 'stream_id')
        self.refused(entry(side='both'), 'side')
        self.refused(entry(ts=float(BASE)), 'ts must be Unix milliseconds')
        self.refused(entry(ts=BASE // 1000), 'ts must be Unix milliseconds')
        self.refused(entry(ts=BASE + 6000), 'ahead of the clock', now=BASE)
        self.refused(entry(build='has space'), 'build')

    def test_entry_freshness_market_and_touch(self):
        ok(entry(), now=BASE + 40_000)
        self.refused(entry(), 'old', now=BASE + 40_001)
        p = entry(entry={'max_age_ms': 12000})
        ok(p, now=BASE + 12000)
        self.refused(p, 'old', now=BASE + 12001)
        self.refused(entry(entry={'max_age_ms': 120001}), 'max_age_ms')
        touch = entry(entry={'type': 'touch'},
                      touch={'enabled': True, 'price': 21000.0, 'valid_until': BASE + 7_200_000})
        ok(touch, now=BASE + 3_600_000)                      # an hour old: the touch's own valid_until counts
        self.refused(touch, 'expired', now=BASE + 7_200_000)
        self.refused(entry(touch={'enabled': True, 'price': 21000.0, 'valid_until': BASE + 1}), 'touch.enabled must match')
        bad = entry(entry={'type': 'touch'}, touch={'enabled': True, 'price': 20999.0, 'valid_until': BASE + 9})
        self.refused(bad, 'touch.price must equal entry.price')
        self.refused(entry(entry={'type': 'touch'}, touch={'enabled': True, 'price': 21000.0, 'valid_until': BASE}),
                     'valid_until must be after ts')

    def test_management_events_are_never_refused_for_age(self):
        for kind, fields in (('flatten', {}), ('bias_flip', {}), ('cancel_entry', {}), ('exit', {'qty': 2}),
                             ('modify_stop', {'stop': {'price': 20990.0, 'safety_price': 0}})):
            ok(event(kind, **fields), now=BASE + 86_400_000)
        later = BASE + 120_000
        self.refused(event('exit', qty=0), 'qty', now=later)
        self.refused(event('flatten', reason='Has Caps'), 'reason', now=later)
        self.refused(event('modify_stop', stop={'price': 20990.1, 'safety_price': 0}), 'stop.price', now=later)
        self.refused(event('modify_stop', stop={'price': 20990.0}), 'stop must have exactly', now=later)
        self.refused(event('flatten', qty=1), 'unknown field', now=later)
        self.refused(event('flatten', ts=later + 6000), 'ahead of the clock', now=later)

    def test_prices_ticks_and_sides(self):
        self.refused(entry(entry={'price': 21000.1}), 'entry.price')
        self.refused(entry(stop={'price': 21010.0}), 'wrong side')
        self.refused(entry(side='short', trade_id=TRADE.replace('long', 'short'), stop={'price': 20990.0}), 'wrong side')
        self.refused(entry(targets=[{'price': 20990.0, 'qty': 2, 'type': 'limit'}]), 'profit side')
        self.refused(entry(stop={'type': 'resting', 'timeframe_s': 60}), 'resting stop has timeframe_s 0')
        self.refused(entry(stop={'type': 'bar_close', 'timeframe_s': 7}), 'timeframe_s')
        self.refused(entry(stop={'type': 'bar_close', 'timeframe_s': 7200}), 'timeframe_s')
        self.refused(entry(stop={'type': 'bar_close', 'timeframe_s': 60, 'safety_price': 20990.0}), 'beyond stop.price')
        ok(entry(stop={'type': 'bar_close', 'timeframe_s': 30, 'safety_price': 0}))
        self.refused(entry(stop={'type': 'hard'}), 'stop.type')

    def test_targets_order_and_resting_limits(self):
        far_first = [{'price': 21020.0, 'qty': 1, 'type': 'limit'}, {'price': 21010.0, 'qty': 1, 'type': 'limit'}]
        self.refused(entry(targets=far_first), 'nearest first')
        mixed = [{'price': 21010.0, 'qty': 1, 'type': 'market'}, {'price': 21020.0, 'qty': 1, 'type': 'limit'}]
        self.refused(entry(targets=mixed), 'cannot come after a market target')
        four = [{'price': 21000.0 + 5 * i, 'qty': 1, 'type': 'limit'} for i in range(1, 5)]
        self.refused(entry(targets=four, runner={'qty': 0}), 'at most 3')
        seven = [{'price': 21000.0 + 5 * i, 'qty': 1, 'type': 'market'} for i in range(1, 8)]
        self.refused(entry(entry={'qty': 7}, targets=seven, runner={'qty': 0}), 'at most 6')
        ok(entry(targets=[], runner={'qty': 4}))
        self.refused(entry(targets=[{'price': 21010.0, 'qty': 1, 'type': 'stop'}], runner={'qty': 3}), 'type')

    def test_runner(self):
        self.refused(entry(runner={'mode': 'target', 'price': 21015.0}), 'beyond every target')
        ok(entry(runner={'mode': 'target', 'price': 21050.0}))
        self.refused(entry(runner={'mode': 'trail', 'trail_distance_ticks': 0}), 'trail_distance_ticks')
        self.refused(entry(targets=[{'price': 21010.0, 'qty': 4, 'type': 'limit'}], runner={'qty': 0, 'mode': 'trail'}),
                     'must be hold when runner.qty is 0')
        self.refused(entry(runner={'mode': 'chandelier'}), 'runner.mode')

    def test_break_even_switch_and_triggers(self):
        # Off: values are only type-checked (a strategy sends its settings even when the switch is off).
        ok(entry(break_even={'enabled': False, 'trigger_price': 0, 'trigger_target': 5, 'confirm': 'bar_close'}))
        self.refused(entry(break_even={'enabled': True, 'trigger_price': 0, 'trigger_target': 0}), 'needs trigger')
        self.refused(entry(break_even={'enabled': True, 'trigger_target': 3}), 'missing target')
        self.refused(entry(break_even={'enabled': True, 'trigger_price': 20990.0}), 'profit side')
        self.refused(entry(break_even={'enabled': True, 'confirm': 'bar_close'}), 'confirm_timeframe_s')
        self.refused(entry(break_even={'enabled': True, 'stop_type': 'bar_close'}), 'stop_timeframe_s')
        self.refused(entry(stop={'type': 'bar_close', 'timeframe_s': 30}, break_even={'enabled': True}),
                     'needs a resting stop')
        ok(entry(stop={'type': 'bar_close', 'timeframe_s': 30},
                 break_even={'enabled': True, 'stop_type': 'bar_close', 'stop_timeframe_s': 30}))
        self.refused(entry(break_even={'if_beyond': 'hold'}), 'if_beyond')
        self.refused(entry(break_even={'offset_ticks': 1.5}), 'offset_ticks')

    def test_eod_daily_loss_and_bias_flip_blocks(self):
        self.refused(entry(eod={'time': '25:00'}), 'HH:MM')
        self.refused(entry(eod={'time': '9:30'}), 'HH:MM')
        self.refused(entry(eod={'tz': 'Mars/Base'}), 'eod.tz')
        self.refused(entry(daily_loss={'enabled': True, 'limit_usd': 0}), 'limit_usd must be above 0')
        ok(entry(daily_loss={'enabled': False, 'limit_usd': 0}))
        self.refused(entry(daily_loss={'fee_per_contract_usd': -1}), 'amounts')
        self.refused(entry(daily_loss={'include_open': 'yes'}), 'switches')
        self.refused(entry(bias_flip={'enabled': 1}), 'bias_flip.enabled')

    def test_strict_json_text(self):
        text = v2.compact(entry())
        self.assertEqual(v2.parse(text)['strategy'], 'example-v2')
        with self.assertRaisesRegex(v2.PayloadError, 'duplicate JSON key'):
            v2.parse('{"a":1,"a":2}')
        with self.assertRaisesRegex(v2.PayloadError, 'NaN'):
            v2.parse('{"a":NaN}')
        with self.assertRaisesRegex(v2.PayloadError, 'limit is under 4,096'):
            v2.parse('{"a":"%s"}' % ('x' * 4100))
        with self.assertRaisesRegex(v2.PayloadError, 'JSON object'):
            v2.parse('[1]')
        big = entry(extras={'setup': 'A', 'risk_usd': 1.0})
        big['build'] = 'b' * 64
        ok(big)


class Extras(unittest.TestCase):
    def test_declared_extras_are_checked_and_undeclared_refused(self):
        ok(entry(extras={'setup': 'B'}))
        ok(entry(extras={'setup': 'B', 'risk_usd': 0}))
        for bad, pattern in (({}, 'extras.setup is required'), ({'setup': 'S 1'}, 'extras.setup'),
                             ({'setup': 'A', 'risk_usd': -1}, 'below its minimum'),
                             ({'setup': 'A', 'risk_usd': True}, 'finite number'),
                             ({'setup': 'A', 'lane': 'large'}, 'undeclared field')):
            with self.assertRaisesRegex(v2.PayloadError, pattern):
                ok(entry(extras=bad))
        # An event without declared extras carries an empty block.
        with self.assertRaisesRegex(v2.PayloadError, 'undeclared'):
            ok(event('flatten', extras={'x': 1}), now=BASE + 60_000)
        with self.assertRaisesRegex(v2.PayloadError, 'not an allowed value'):
            ok(event('bias_flip', extras={'new_trend': 'sideways'}), now=BASE + 60_000)

    def test_extras_schema_declaration_fails_closed(self):
        good = {'type': 'int', 'min': 0}
        for bad in ({'entry': {'Bad-Name': good}}, {'nope': {'x': good}}, {'entry': {'x': {'type': 'list'}}},
                    {'entry': {'x': {'type': 'int', 'pattern': 'x'}}}, {'entry': {'x': {'type': 'str'}}},
                    {'entry': {'x': {'type': 'enum', 'values': []}}}, {'entry': {}}):
            with self.assertRaises(v2.PayloadError):
                v2.Strategy('x-strategy', 'x-bridge-v2', 'X', extras=bad)
        v2.Strategy('x-strategy', 'x-bridge-v2', 'X', extras={'entry': {'x': good, 'p': {'type': 'price'},
                                                                         'm': {'type': 'ms'}, 'b': {'type': 'bool'}}})

    def test_opaque_extras_declaration_is_bounded(self):
        for spec in ({'type': 'opaque'}, {'type': 'opaque', 'max_keys': 0}, {'type': 'opaque', 'max_keys': 13},
                     {'type': 'opaque', 'max_keys': 2, 'optional': True}, {'type': 'int', 'max_keys': 2}, 'opaque'):
            with self.assertRaises(v2.PayloadError):
                v2.Strategy('x-strategy', 'x-bridge-v2', 'X', extras={'entry': {v2.OPAQUE: spec}})
        with self.assertRaises(v2.PayloadError):
            v2.Strategy('x-strategy', 'x-bridge-v2', 'X', extras={'entry': {'x': {'type': 'opaque', 'max_keys': 2}}})
        open_entry = v2.Strategy('example-v2', 'example-bridge-v2', 'Example',
                                 extras={'entry': dict(S.extras['entry'], **{v2.OPAQUE: {'type': 'opaque', 'max_keys': 2}})})
        v2.validate(entry(extras={'setup': 'B', 'aux_a': 1.5, 'aux_b': 'fx'}), strategy=open_entry, now_ms=BASE)
        for extra in ({'aux_a': 1, 'aux_b': 2, 'aux_c': 3}, {'aux_a': [1]}, {'Aux': 1}, {'setup': 'S 1'}):
            with self.assertRaises(v2.PayloadError):
                v2.validate(entry(extras=dict({'setup': 'B'}, **extra)), strategy=open_entry, now_ms=BASE)

    def test_strategy_check_hook_runs_last(self):
        seen = []

        def check(p):
            seen.append(p['event'])
            if p['event'] == 'entry' and p['extras']['setup'] == 'S9':
                raise v2.PayloadError('setup S9 needs a touch entry')
        strategy = v2.Strategy('example-v2', 'example-bridge-v2', 'Example', extras=S.extras, check=check)
        v2.validate(entry(), strategy=strategy, now_ms=BASE)
        with self.assertRaisesRegex(v2.PayloadError, 'S9'):
            v2.validate(entry(extras={'setup': 'S9'}), strategy=strategy, now_ms=BASE)
        self.assertEqual(seen, ['entry', 'entry'])


class Registry(unittest.TestCase):
    """Every registered v2 strategy is pinned the same way everywhere. Failures name the line to fix."""

    def test_shared_pins_match_the_files(self):
        for name, pin in registry.SHARED.items():
            self.assertEqual(registry.sha256(name), pin, 'bridge_v2_registry.SHARED[%r] must be %s'
                             % (name, registry.sha256(name)))

    def test_each_strategy_line_agrees_with_every_pin(self):
        v2_lines = {a[0]: a for a in bridge_adapters.V2_ADAPTERS}
        clean_base = (ROOT / 'installer/clean_base.py').read_text()
        bundle = (ROOT / 'lab/build_source_bundle.py').read_text()
        self.assertEqual(set(registry.STRATEGIES), set(v2_lines),
                         'each strategy needs one line in bridge_v2_registry.STRATEGIES and bridge_adapters.V2_ADAPTERS')
        for adapter_id, (module_file, engine_sha, strategy_sha) in registry.STRATEGIES.items():
            actual = registry.sha256(module_file)
            self.assertEqual(engine_sha, actual, '%s: module hash must be %s' % (adapter_id, actual))
            self.assertRegex(strategy_sha, r'\A[0-9a-f]{64}\Z')
            module = __import__(registry.module_name(adapter_id), fromlist=['STRATEGY'])
            self.assertEqual(module.STRATEGY.adapter_id, adapter_id)
            self.assertIs(source.load_pair_sources(adapter_id)['core'], module)
            self.assertEqual(contract.PAIRS[adapter_id], (engine_sha, strategy_sha))
            self.assertEqual(source.BRIDGE_ENGINES[adapter_id]['files'], registry.files(adapter_id))
            self.assertEqual(bridge_adapters.lookup(adapter_id, v2.PROTOCOL).take_profits, v2_lines[adapter_id][3])
            for listing in (clean_base, bundle):
                self.assertIn('app/trading-runtime/decrypt_trading/' + module_file, listing)
            self.assertIs(registry.strategy(module.STRATEGY.strategy_id), module.STRATEGY)
        with self.assertRaises(ValueError):
            registry.strategy('not-registered')

    def test_shared_files_ship_and_v2_adapters_use_every_kind(self):
        for name in ('bridge_v2.py', 'bridge_v2_contract.py', 'bridge_v2_registry.py'):
            for listing in ('installer/clean_base.py', 'lab/build_source_bundle.py'):
                self.assertIn('app/trading-runtime/decrypt_trading/' + name, (ROOT / listing).read_text())
        self.assertEqual(bridge_adapters.V2_KINDS, bridge_adapters.ORDER_KINDS)
        self.assertIn('bridge/PAYLOAD-V2.md',
                      (ROOT / 'modules/tradovate-bridge/runtime-files.txt').read_text().split())


class Spec(unittest.TestCase):
    """PAYLOAD-V2.md: its example alerts are valid, and it names every field the validator knows."""

    def test_examples_in_the_spec_validate(self):
        text = SPEC.read_text()
        blocks = re.findall(r'```json\n(\{.*?\})\n```', text, re.S)
        alerts = [json.loads(b) for b in blocks if '"protocol": "decrypt-bridge-v2"' in b]
        self.assertGreaterEqual(len(alerts), 5)
        for p in alerts:
            v2.validate(p, strategy=S, now_ms=p['ts'])

    def test_every_block_field_is_documented(self):
        text = SPEC.read_text()
        for block, fields in v2.BLOCKS.items():
            for name in fields:
                self.assertIn('`%s`' % name, text, '%s.%s is not documented' % (block, name))
        for name in v2.EVENTS:
            self.assertIn('`%s`' % name, text)
        self.assertNotRegex(text, r'//@version|\bstrategy\.(entry|exit|close)\(|\bindicator\(')    # no Pine in the repo

    def test_module_api_is_what_the_runtime_needs(self):
        for name in source.BRIDGE_ENGINE_FUNCTIONS + ('Engine', 'is_entry_payload'):
            self.assertTrue(callable(getattr(example, name)))
        self.assertIs(example.Engine.STRATEGY, S)
        self.assertEqual(example.payload_schema(entry()), 'decrypt-bridge-v2')
        self.assertEqual(example.payload_schema({'protocol': 'x'}), 'undeclared')
        self.assertEqual(example.input_identity(entry()), TRADE + ':entry')
        self.assertIsNone(example.input_identity({'alert_id': 'has space'}))
        self.assertTrue(example.is_entry_payload(entry()))
        self.assertFalse(example.is_entry_payload(event('flatten')))
        with self.assertRaises(ValueError):
            bridge_v2.module_api(object())


if __name__ == '__main__':
    unittest.main()
