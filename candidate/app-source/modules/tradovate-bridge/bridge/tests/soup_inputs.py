"""Synthetic Soup input names in the bridge tests -> the optional engine package's own names.

The suites set Soup's contract allocation with synthetic keys (`fixture_int_NN`), because the
strategy's input names stay out of this repository. The package's `test_pair.fixture()` reads
only its own input names and silently ignores unknown keys, so the keys are translated here.
Each synthetic key names an allocation field of the entry payload; the matching input is found
by probing the package once: the integer input that changes only that field (and the total).
"""

import json
from pathlib import Path

# Synthetic key -> the entry-payload field it sets.
ROLES = {'fixture_int_07': 'tp1_qty', 'fixture_int_06': 'parent_runner_qty',
         'fixture_int_01': 'bias_runner_qty', 'fixture_int_04': 'eod_runner_qty'}

_names = {}


def _probe(soup):
    package = Path(soup.__file__).resolve().parents[1]
    if package in _names:
        return _names[package]
    contract = json.loads((package / 'engine' / 'settings_contract.json').read_text())['inputs']
    base = soup.fixture('long')[0]
    current = base['strategy_snapshot']['inputs']
    fields = set(ROLES.values())
    found = {}
    for name, spec in contract.items():
        if not (isinstance(spec, dict) and spec.get('type') == 'int') or name not in current:
            continue
        value = current[name] + 1
        if spec.get('max') is not None and value > spec['max']:
            value = current[name] - 1
        p = soup.fixture('long', {name: value})[0]
        changed = {f for f in fields if p[f] != base[f]}
        if len(changed) == 1:
            found.setdefault(changed.pop(), []).append(name)
    names = {}
    for key, field in ROLES.items():
        hits = found.get(field, [])
        if len(hits) != 1:
            raise AssertionError('engine package: %d inputs set %s (need exactly 1)' % (len(hits), field))
        names[key] = hits[0]
    _names[package] = names
    return names


def named(soup, settings):
    """`settings` with every synthetic key replaced by the package's own input name."""
    if not any(k in ROLES for k in settings):
        return settings
    names = _probe(soup)
    return {names.get(k, k): v for k, v in settings.items()}
