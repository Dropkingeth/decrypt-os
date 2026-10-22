"""beta4 payload cross-check: Soup r4 alerts through Soup's real intake, plus the settings-contract pins.

The alerts are synthetic fixtures from `soup_alerts_fixture/` (arbitrary settings and the synthetic constants of
soup_fixture_contract.py; no Pine is in this repo). Every alert goes through the production Pine intake
(`Runtime.admit_authenticated` with `Runtime.input_identity`, exactly what `ingress.SignalIngress` calls).

There is no contract cap: the strategy alert sets the size, so an allocation smaller than the snapshot's tranche
settings is accepted as sent. The other strategies send decrypt-bridge-v2; their examples are in
tests/test_trading_bridge_v2_*.py. No network, no browser, no real accounts.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from soup_fixture_contract import FIXTURE_CONSTANTS, FIXTURE_DIGEST, constants_digest  # noqa: E402
from test_trading_runtime import binding as soup_binding  # noqa: E402
from decrypt_trading import RuntimeManager  # noqa: E402

EXAMPLES = Path(__file__).with_name('soup_alerts_fixture')
CONTRACT = (Path(__file__).resolve().parents[1]
            / 'app/trading-runtime/decrypt_trading/vendor/soup_r4/settings_contract.json')
CASES = ['entry-v3-long-basic.json', 'entry-v3-short-single.json', 'facts-bias-flip.json',
         'entry-v3-short-under-snapshot.json']
ACCEPT = None


def example(name):
    return json.loads((EXAMPLES / name).read_text())


def admit(runtime, payload):
    """The production Pine intake (ingress.SignalIngress) without the webhook secret check."""
    return runtime.admit_authenticated('pine', json.dumps(payload), producer_id=runtime.binding['signalProducerId'],
                                       event_id=runtime.input_identity(payload))


class Fixtures(unittest.TestCase):
    def test_fixture_cases_exist_and_are_json(self):
        for name in CASES:
            with self.subTest(name=name):
                self.assertIsInstance(example(name), dict)


class SettingsContract(unittest.TestCase):
    """The shipped contract pins the strategy constants by digest, names exclusion inputs by pattern only and gives
    only a name form and an upper bound for the inputs the engine never reads."""

    def test_constants_are_pinned_by_digest_only(self):
        rules = json.loads(CONTRACT.read_text())
        self.assertNotIn('constants', rules)
        self.assertRegex(rules['constants_sha256'], r'\A[0-9a-f]{64}\Z')
        self.assertNotEqual(rules['constants_sha256'], FIXTURE_DIGEST)   # the fixtures do not carry the real block

    def test_exclusion_inputs_are_a_pattern_not_a_list(self):
        rules = json.loads(CONTRACT.read_text())
        self.assertFalse([n for n in rules['inputs'] if n.startswith('excl_')])
        self.assertEqual(rules['input_patterns'], [{'pattern': '^excl_[0-9]{4}_[0-9]{4}$', 'count': 8, 'type': 'string'}])

    def test_only_engine_read_inputs_are_named(self):
        rules = json.loads(CONTRACT.read_text())
        core = (CONTRACT.parent / 'core.py').read_text()
        for name in rules['inputs']:
            self.assertRegex(core, r"['\"]%s['\"]" % re.escape(name))
        # The inputs the engine never reads are neither named, digested nor counted: a name form and a maximum only.
        self.assertNotIn('input_digests', rules)
        self.assertEqual(set(rules['unread_inputs']), {'name_pattern', 'max_count'})
        self.assertIs(type(rules['unread_inputs']['max_count']), int)
        self.assertGreater(rules['unread_inputs']['max_count'], 0)
        self.assertEqual(re.findall(r'[0-9a-f]{64}', CONTRACT.read_text()), [rules['constants_sha256']])

    def test_fixtures_name_unread_inputs_synthetically(self):
        rules = json.loads(CONTRACT.read_text())
        for path in sorted(EXAMPLES.glob('*.json')) + [EXAMPLES.with_name('test_trading_runtime_entry.json')]:
            inputs = json.loads(path.read_text())['strategy_snapshot']['inputs']
            unread = [n for n in inputs if n not in rules['inputs'] and not re.fullmatch(r'excl_[0-9]{4}_[0-9]{4}', n)]
            with self.subTest(fixture=path.name):
                self.assertTrue(unread)
                self.assertEqual([n for n in unread if not re.fullmatch(r'fixture_(bool|int|number|text)_[0-9]{2}', n)], [])

    def test_digest_is_sha256_of_canonical_json(self):
        reordered = dict(reversed(list(FIXTURE_CONSTANTS.items())))
        self.assertEqual(constants_digest(reordered), FIXTURE_DIGEST)
        canonical = '{"fixture_constant_a":1,"fixture_flag_b":true,"fixture_window_c":"0000-0001"}'
        self.assertEqual(FIXTURE_DIGEST, hashlib.sha256(canonical.encode()).hexdigest())


class Intake(unittest.TestCase):
    """Intake only: the schema registry plus the engine's contract check, at the alert's own time."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.count = 0

    def runtime(self, config, now):
        self.count += 1
        manager = RuntimeManager(Path(self.temp.name) / ('i%d' % self.count))
        manager.install(config)
        runtime = manager.open(config['instanceId'], clock=lambda: now)
        self.addCleanup(runtime.close)
        return runtime

    def check(self, config, payload, now, refusal):
        runtime = self.runtime(config, now)
        if refusal is ACCEPT:
            receipt = admit(runtime, payload)
            self.assertFalse(receipt['duplicate'])
        else:
            with self.assertRaisesRegex(Exception, refusal):
                admit(runtime, payload)

    def run_cases(self, cases, label):
        for case, (p, refusal) in cases.items():
            with self.subTest(case=case):
                config = soup_binding(label)
                config['streamId'], config['symbol'] = p['stream_id'], p['symbol']
                self.check(config, p, p['timestamp'] + 300, refusal)

    def test_soup_r4(self):
        # under-snapshot: the alert sends fewer contracts than the snapshot's tranche setting; the alert's size is used.
        self.run_cases({name: (example(name), ACCEPT) for name in CASES}, 'soup-example')

    def test_soup_r4_alert_sizes_are_the_truth(self):
        # A TP1 size above any earlier per-tranche maximum is accepted; a smaller allocation than the snapshot's
        # tranche settings is accepted. Tranches must still add up to a positive quantity.
        base = example('entry-v3-long-basic.json')
        big = copy.deepcopy(base)
        big.update(quantity=33, tp1_qty=33)
        self.run_cases({
            'large TP1': (big, ACCEPT),
            'tranches do not add up': (dict(base, quantity=5), 'allocation mismatch: quantity'),
            'zero quantity': (dict(base, quantity=0, tp1_qty=0), 'empty allocation'),
            'negative tranche': (dict(base, quantity=2, tp1_qty=3, parent_runner_qty=-1), 'allocation: parent_runner_qty'),
            'fractional quantity': (dict(base, quantity=2.0), 'empty allocation'),
            'bool tranche': (dict(base, bias_runner_qty=False), 'allocation: bias_runner_qty')}, 'soup-sizes')

    def test_constants_digest_decides(self):
        """The snapshot's constants must hash to the contract's digest; any change, or a non-object, is refused."""
        base = example('entry-v3-long-basic.json')
        changed_value = copy.deepcopy(base)
        changed_value['strategy_snapshot']['constants']['fixture_constant_a'] += 1
        extra_key = copy.deepcopy(base)
        extra_key['strategy_snapshot']['constants']['fixture_extra'] = 0
        missing_key = copy.deepcopy(base)
        missing_key['strategy_snapshot']['constants'].pop('fixture_flag_b')
        as_list = copy.deepcopy(base)
        as_list['strategy_snapshot']['constants'] = list(FIXTURE_CONSTANTS.items())
        reordered = copy.deepcopy(base)
        reordered['strategy_snapshot']['constants'] = dict(reversed(list(FIXTURE_CONSTANTS.items())))
        self.run_cases({
            'synthetic constants': (base, ACCEPT),
            'key order does not matter': (reordered, ACCEPT),
            'changed value': (changed_value, 'unsupported strategy constants'),
            'extra key': (extra_key, 'unsupported strategy constants'),
            'missing key': (missing_key, 'unsupported strategy constants'),
            'not an object': (as_list, 'unsupported strategy constants')}, 'soup-constants')

    def test_exclusion_inputs_by_pattern(self):
        """Exactly 8 inputs named excl_HHMM_HHMM, any times; one fewer, one more or a malformed name is refused."""
        base = example('entry-v3-long-basic.json')
        names = sorted(n for n in base['strategy_snapshot']['inputs'] if re.fullmatch(r'excl_[0-9]{4}_[0-9]{4}', n))
        self.assertEqual(len(names), 8)

        def changed(edit):
            p = copy.deepcopy(base)
            edit(p['strategy_snapshot']['inputs'])
            return p
        renamed = changed(lambda s: s.update({'excl_1111_2222': s.pop(names[0])}))
        self.run_cases({
            'other times': (renamed, ACCEPT),
            'seven': (changed(lambda s: s.pop(names[0])), 'complete settings required'),
            'nine': (changed(lambda s: s.update(excl_9999_9999='0000-0001')), 'complete settings required'),
            'malformed name': (changed(lambda s: s.update({'excl_12_34': s.pop(names[0])})), 'complete settings required'),
            'not a string': (changed(lambda s: s.update({names[0]: 5})), 'setting type: ' + names[0])}, 'soup-excl')

    def test_unread_inputs(self):
        """Inputs the engine never reads are checked by a plain name form, a scalar value and an upper bound only:
        their names, number and value types are not pinned. A malformed name, a non-scalar value or too many inputs
        is refused."""
        base = example('entry-v3-long-basic.json')
        inputs = base['strategy_snapshot']['inputs']
        name, number = 'fixture_bool_01', 'fixture_number_01'
        self.assertIs(type(inputs[name]), bool)
        self.assertIs(type(inputs[number]), float)

        rules = json.loads(CONTRACT.read_text())
        LIMIT = rules['unread_inputs']['max_count']

        def fill(s, total):
            unread = [n for n in s if n not in rules['inputs'] and not re.fullmatch(r'excl_[0-9]{4}_[0-9]{4}', n)]
            s.update({'fixture_extra_%03d' % i: i for i in range(total - len(unread))})

        def changed(edit):
            p = copy.deepcopy(base)
            edit(p['strategy_snapshot']['inputs'])
            return p
        self.run_cases({
            'renamed': (changed(lambda s: s.update({name + '_x': s.pop(name)})), ACCEPT),
            'whole number in a number slot': (changed(lambda s: s.update({number: 2})), ACCEPT),
            'missing': (changed(lambda s: s.pop(name)), ACCEPT),
            'extra': (changed(lambda s: s.update(fixture_unknown_input=True)), ACCEPT),
            'another type': (changed(lambda s: s.update({name: 'yes'})), ACCEPT),
            'int to bool': (changed(lambda s: s.update(fixture_int_01=True)), ACCEPT),
            'at the maximum': (changed(lambda s: fill(s, LIMIT)), ACCEPT),
            'too many': (changed(lambda s: fill(s, LIMIT + 1)), 'too many settings'),
            'malformed name': (changed(lambda s: s.update({'Fixture Input': s.pop(name)})), 'complete settings required'),
            'overlong name': (changed(lambda s: s.update({'f' * 65: s.pop(name)})), 'complete settings required'),
            'not a scalar': (changed(lambda s: s.update({name: [True]})), 'setting type: ' + name),
            'null': (changed(lambda s: s.update({name: None})), 'setting type: ' + name),
            'overlong text': (changed(lambda s: s.update(fixture_text_01='x' * 65)), 'setting type: fixture_text_01')},
            'soup-unread')

if __name__ == '__main__':
    unittest.main()
