"""decrypt-bridge-v2: the shared alert envelope of every non-Soup strategy, and its strict validator.

The strategy owns every trading rule. Its alert carries the whole plan: entry, stop, take-profits,
break-even, runner, end of day, daily loss lock, touch entry and the management events. This module
only checks the format: exact keys, types, tick alignment, identity, sizes that add up and freshness of
entries. It decides nothing about trading. The spec is modules/tradovate-bridge/bridge/PAYLOAD-V2.md.

Anything unknown fails closed: an unknown key (outside a strategy's declared `extras`), event, type,
time zone or strategy is refused with a plain-language reason (PayloadError).

Per-strategy fields go in `extras`. A strategy declares their schema once, in its own module, with
`Strategy(..., extras={event: {field: spec}})` (see `check_extras_schema` for the spec keys). An event
the strategy declares no extras for must carry `"extras": {}`. A strategy may also accept a bounded number
of opaque extra keys for an event (the `OPAQUE` entry): they are format-checked, recorded and never read.

Pure stdlib plus bridge_adapters (the contract table). No I/O. Command line:
    python3 -m decrypt_trading.bridge_v2_contract alert.json [more.json ...]
validates example alerts of the registered strategies (bridge_v2_registry).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
from zoneinfo import ZoneInfo

from . import bridge_adapters

PROTOCOL = 'decrypt-bridge-v2'
MAX_BYTES = 4095                  # strict JSON under 4,096 bytes (compact form)
ENTRY_MAX_AGE_MS = 120_000        # the platform's freshness limit for entries; an alert may ask for less
MAX_AHEAD_MS = 5_000              # clock skew allowed for an alert time slightly in the future
TS_MIN, TS_MAX = 1_577_836_800_000, 4_102_444_800_000   # 2020-01-01 .. 2100-01-01, Unix ms UTC
MAX_TARGETS = 6
MAX_RESTING_TARGETS = 3           # the bridge rests at most three take-profit limits per trade
TIME_ZONES = ('America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
              'Europe/London', 'UTC')
# USD per point, per contract root (exchange contract specs, not a trading rule).
POINT_VALUE = {'MNQ': 2.0, 'MES': 5.0}

EVENTS = ('entry', 'cancel_entry', 'modify_stop', 'exit', 'flatten', 'bias_flip')
MANAGEMENT_EVENTS = EVENTS[1:]
COMMON = frozenset(('protocol', 'strategy', 'build', 'event', 'alert_id', 'trade_id', 'stream_id', 'symbol',
                    'side', 'ts', 'extras'))
EVENT_FIELDS = {
    'entry': frozenset(('entry', 'touch', 'stop', 'targets', 'break_even', 'runner', 'eod', 'daily_loss',
                        'bias_flip')),
    'cancel_entry': frozenset(('reason',)),
    'modify_stop': frozenset(('stop', 'reason')),
    'exit': frozenset(('qty', 'reason')),
    'flatten': frozenset(('reason',)),
    'bias_flip': frozenset(('reason',)),
}
BLOCKS = {
    'entry': ('type', 'qty', 'price', 'max_age_ms'),
    'touch': ('enabled', 'price', 'valid_until', 'cancel_if_stop_touched'),
    'stop': ('type', 'price', 'timeframe_s', 'safety_price'),
    'target': ('price', 'qty', 'type'),
    'break_even': ('enabled', 'trigger_price', 'trigger_target', 'trigger_mode', 'confirm', 'confirm_timeframe_s',
                   'stop_price', 'offset_ticks', 'stop_type', 'stop_timeframe_s', 'if_beyond'),
    'runner': ('qty', 'mode', 'price', 'trail_activation_ticks', 'trail_distance_ticks'),
    'eod': ('enabled', 'time', 'tz'),
    'daily_loss': ('enabled', 'limit_usd', 'fee_per_contract_usd', 'tz', 'day_start', 'include_open', 'flatten'),
    'bias_flip': ('enabled',),
    'modify_stop': ('price', 'safety_price'),
}

_STRATEGY = re.compile(r'[a-z0-9][a-z0-9.-]{2,47}\Z')
_BUILD = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}\Z')
_ALERT_ID = re.compile(r'[A-Za-z0-9_.:@+-]{1,160}\Z')
_TRADE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,150}:(long|short)\Z')
_STREAM = re.compile(r'[A-Za-z0-9_-]{1,48}\Z')
_REASON = re.compile(r'[a-z0-9_]{1,64}\Z')
_CLOCK = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])\Z')
_FIELD = re.compile(r'[a-z][a-z0-9_]{0,39}\Z')
_OPAQUE_STR = re.compile(r'[A-Za-z0-9 ._:+-]{0,64}\Z')
OPAQUE = '*'                       # extras schema entry: {'*': {'type': 'opaque', 'max_keys': n}}
OPAQUE_MAX_KEYS = 12


class PayloadError(ValueError):
    """A refused alert. The message is a plain sentence naming the field."""


def require(ok, reason):
    if not ok:
        raise PayloadError(reason)


def is_int(value):
    return type(value) is int


def is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def on_tick(value, tick):
    return is_number(value) and value > 0 and abs(value / tick - round(value / tick)) < 1e-9


def bar_timeframe(seconds):
    """A bar-close timeframe the 5-second native feed can close exactly: 5 s .. 1 h, dividing the hour."""
    return is_int(seconds) and 5 <= seconds <= 3600 and seconds % 5 == 0 and 3600 % seconds == 0


def clock(value):
    """'HH:MM' -> (hour, minute). Raises PayloadError."""
    m = _CLOCK.match(value) if type(value) is str else None
    require(m is not None, 'time must be "HH:MM" (24-hour)')
    return int(m.group(1)), int(m.group(2))


def compact(payload):
    return json.dumps(payload, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def parse(raw):
    """Strict JSON text -> dict: under 4,096 bytes, no duplicate keys, no NaN/Infinity. Raises PayloadError."""
    require(isinstance(raw, (str, bytes, bytearray)), 'alert must be JSON text')
    data = raw.encode('utf-8') if isinstance(raw, str) else bytes(raw)
    require(len(data) <= MAX_BYTES, 'alert is %d bytes; the limit is under 4,096' % len(data))

    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 'duplicate JSON key %r' % key)
            out[key] = value
        return out

    def constant(value):
        raise PayloadError('NaN and Infinity are not JSON')

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except PayloadError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PayloadError('alert is not valid JSON: %s' % str(exc)[:80]) from None
    require(isinstance(value, dict), 'alert must be a JSON object')
    return value


# --- per-strategy extras ---------------------------------------------------------------------------------

_SPEC_KEYS = {
    'int': {'min', 'max'}, 'number': {'min', 'max'}, 'bool': set(), 'str': {'pattern', 'max_len'},
    'enum': {'values'}, 'price': set(), 'ms': set(), 'opaque': {'max_keys'},
}


def check_extras_schema(extras):
    """Validate a strategy's extras declaration: {event: {field: spec}}. Returns it; raises PayloadError.

    spec = {'type': 'int'|'number' (optional 'min', 'max'), 'bool', 'str' ('pattern' regex, 'max_len' <= 200),
            'enum' ('values': tuple of str/int), 'price' (on the contract tick, or 0 for none),
            'ms' (Unix ms, int >= 0); optional 'optional': True (the field may be absent)}.
    The entry named OPAQUE ('*') takes {'type': 'opaque', 'max_keys': 1-12}: up to max_keys further keys
    (field-name format) with scalar values (true/false, a finite number, or a string of up to 64 safe
    characters) are accepted as opaque metadata.
    """
    require(isinstance(extras, dict), 'extras schema must be {event: {field: spec}}')
    for event, fields in extras.items():
        require(event in EVENTS, 'extras schema: unknown event %r' % (event,))
        require(isinstance(fields, dict) and fields, 'extras schema for %s must declare at least one field' % event)
        for name, spec in fields.items():
            if name == OPAQUE:
                require(isinstance(spec, dict) and set(spec) == {'type', 'max_keys'} and spec['type'] == 'opaque'
                        and is_int(spec['max_keys']) and 1 <= spec['max_keys'] <= OPAQUE_MAX_KEYS,
                        'extras %s: opaque entry needs max_keys (1-%d)' % (event, OPAQUE_MAX_KEYS))
                continue
            require(type(name) is str and _FIELD.match(name), 'extras field name %r' % (name,))
            require(isinstance(spec, dict) and spec.get('type') in _SPEC_KEYS and spec['type'] != 'opaque',
                    'extras %s.%s: unknown type' % (event, name))
            allowed = _SPEC_KEYS[spec['type']] | {'type', 'optional'}
            require(set(spec) <= allowed, 'extras %s.%s: unknown spec key' % (event, name))
            require(spec.get('optional', False) in (True, False), 'extras %s.%s: optional must be true/false' % (event, name))
            for bound in ('min', 'max'):
                require(bound not in spec or is_number(spec[bound]), 'extras %s.%s: %s must be a number' % (event, name, bound))
            if spec['type'] == 'str':
                require(type(spec.get('pattern')) is str and is_int(spec.get('max_len')) and 1 <= spec['max_len'] <= 200,
                        'extras %s.%s: str needs pattern and max_len (1-200)' % (event, name))
                re.compile(spec['pattern'])
            if spec['type'] == 'enum':
                values = spec.get('values')
                require(isinstance(values, tuple) and values and all(type(v) in (str, int) for v in values),
                        'extras %s.%s: enum needs a tuple of values' % (event, name))
    return extras


def check_extras(value, fields, event, tick):
    """The alert's extras block against the strategy's declared fields for this event."""
    require(isinstance(value, dict), 'extras must be an object')
    fields = dict(fields or {})
    opaque = fields.pop(OPAQUE, None)
    unknown = set(value) - set(fields)
    if opaque is None:
        require(not unknown, 'extras has undeclared field(s): %s' % ', '.join(sorted(unknown)))
    else:
        require(len(unknown) <= opaque['max_keys'], 'extras has more than %d extra field(s)' % opaque['max_keys'])
        for name in sorted(unknown):
            v = value[name]
            require(type(name) is str and _FIELD.match(name) is not None, 'extras field name is invalid')
            require(type(v) is bool or is_number(v) or (type(v) is str and _OPAQUE_STR.match(v) is not None),
                    'extras.%s must be true/false, a finite number or a short plain string' % name)
    for name, spec in fields.items():
        if name not in value:
            require(spec.get('optional', False), 'extras.%s is required for %s' % (name, event))
            continue
        v, kind, where = value[name], spec['type'], 'extras.' + name
        if kind == 'int':
            require(is_int(v), where + ' must be a whole number')
        elif kind == 'number':
            require(is_number(v), where + ' must be a finite number')
        elif kind == 'bool':
            require(type(v) is bool, where + ' must be true or false')
        elif kind == 'str':
            require(type(v) is str and len(v) <= spec['max_len'] and re.fullmatch(spec['pattern'], v) is not None,
                    where + ' has an invalid value')
        elif kind == 'enum':
            require(type(v) in (str, int) and type(v) is not bool and v in spec['values'], where + ' is not an allowed value')
        elif kind == 'price':
            require(v == 0 or on_tick(v, tick), where + ' must be 0 or a positive price on the contract tick')
        elif kind == 'ms':
            require(is_int(v) and v >= 0, where + ' must be Unix milliseconds')
        if kind in ('int', 'number'):
            require('min' not in spec or v >= spec['min'], where + ' is below its minimum')
            require('max' not in spec or v <= spec['max'], where + ' is above its maximum')


@dataclass(frozen=True)
class Strategy:
    """One strategy on decrypt-bridge-v2. Declared in its own module (bridge_v2_<name>.py).

    strategy_id: the alert's `strategy` field; adapter_id: the binding's adapter (bridge_adapters, one line in
    V2_ADAPTERS); extras: {event: {field: spec}} (check_extras_schema); check: optional function(payload)
    for cross-field format checks of the extras (raise PayloadError). Neither may add trading rules."""
    strategy_id: str
    adapter_id: str
    label: str
    extras: dict = field(default_factory=dict)
    check: object = None

    def __post_init__(self):
        require(type(self.strategy_id) is str and _STRATEGY.match(self.strategy_id), 'strategy id must be 3-48 '
                'lowercase letters, digits, dots or dashes')
        require(type(self.adapter_id) is str and _STRATEGY.match(self.adapter_id), 'adapter id is invalid')
        require(type(self.label) is str and self.label.strip(), 'strategy label required')
        check_extras_schema(self.extras)
        require(self.check is None or callable(self.check), 'check must be a function')


# --- the envelope ---------------------------------------------------------------------------------------

def _block(p, name, key=None):
    value = p.get(key or name)
    require(isinstance(value, dict), '%s must be an object' % (key or name))
    want = set(BLOCKS[name])
    require(set(value) == want, '%s must have exactly the fields: %s' % (key or name, ', '.join(BLOCKS[name])))
    return value


def _price_or_zero(value, tick, where):
    require(value == 0 or on_tick(value, tick), where + ' must be 0 or a positive price on the contract tick')


def _profit(sign, price, ref):
    return sign * (price - ref) > 0


def validate(p, *, strategy, now_ms=None):
    """Check one alert against the envelope and `strategy` (a Strategy). Returns the alert unchanged.

    With `now_ms`, an entry must be fresh: a market entry at most min(entry.max_age_ms, 120 s) old, a touch
    entry before its touch.valid_until; neither more than 5 s ahead of the clock. Management events are
    never refused for age (an exit only reduces risk); they must not be ahead of the clock either."""
    require(isinstance(strategy, Strategy), 'a registered strategy is required')
    require(isinstance(p, dict), 'alert must be a JSON object')
    size = len(compact(p).encode('utf-8'))
    require(size <= MAX_BYTES, 'alert is %d bytes; the limit is under 4,096' % size)
    require(p.get('protocol') == PROTOCOL, 'protocol must be %s' % PROTOCOL)
    event = p.get('event')
    require(event in EVENTS, 'event must be one of: %s' % ', '.join(EVENTS))
    expected = COMMON | EVENT_FIELDS[event]
    keys = set(p)
    require(keys == expected, 'unknown field(s): %s; missing field(s): %s' % (
        ', '.join(sorted(keys - expected)) or 'none', ', '.join(sorted(expected - keys)) or 'none'))
    require(p['strategy'] == strategy.strategy_id, 'strategy is not this binding\'s strategy')
    require(type(p['build']) is str and _BUILD.match(p['build']), 'build must be 1-64 safe characters')
    require(type(p['alert_id']) is str and _ALERT_ID.match(p['alert_id']), 'alert_id must be 1-160 safe characters')
    require(p['side'] in ('long', 'short'), 'side must be long or short')
    require(type(p['trade_id']) is str and _TRADE_ID.match(p['trade_id']) and p['trade_id'].endswith(':' + p['side']),
            'trade_id must be safe characters ending in :<side>')
    require(type(p['stream_id']) is str and _STREAM.match(p['stream_id']), 'stream_id must be 1-48 safe characters')
    try:
        spec = bridge_adapters.contract_spec(p['symbol'])
    except bridge_adapters.AdapterError as exc:
        raise PayloadError('symbol: %s (never a continuous symbol such as MNQ1!)' % exc) from None
    require(spec['root'] in POINT_VALUE, 'contract root has no point value')
    tick = spec['tick_size']
    require(is_int(p['ts']) and TS_MIN < p['ts'] < TS_MAX, 'ts must be Unix milliseconds (UTC)')
    if now_ms is not None:
        require(p['ts'] - now_ms <= MAX_AHEAD_MS, 'ts is ahead of the clock')
    sign = 1 if p['side'] == 'long' else -1
    if event == 'entry':
        _validate_entry(p, tick, sign, now_ms)
    elif event == 'modify_stop':
        s = _block(p, 'modify_stop', 'stop')
        require(on_tick(s['price'], tick), 'stop.price must be a positive price on the contract tick')
        _price_or_zero(s['safety_price'], tick, 'stop.safety_price')
    elif event == 'exit':
        require(is_int(p['qty']) and p['qty'] >= 1, 'qty must be a positive whole number')
    if event != 'entry':
        require(type(p['reason']) is str and _REASON.match(p['reason']), 'reason must be 1-64 of a-z, 0-9, _')
    check_extras(p['extras'], strategy.extras.get(event), event, tick)
    if strategy.check is not None:
        strategy.check(p)
    return p


def _validate_entry(p, tick, sign, now_ms):
    e = _block(p, 'entry')
    require(e['type'] in ('market', 'touch'), 'entry.type must be market or touch')
    require(is_int(e['qty']) and e['qty'] >= 1, 'entry.qty must be a positive whole number')
    require(on_tick(e['price'], tick), 'entry.price must be a positive price on the contract tick')
    require(is_int(e['max_age_ms']) and 1_000 <= e['max_age_ms'] <= ENTRY_MAX_AGE_MS,
            'entry.max_age_ms must be 1000 to 120000')
    ref = e['price']

    t = _block(p, 'touch')
    require(type(t['enabled']) is bool and type(t['cancel_if_stop_touched']) is bool, 'touch switches must be true or false')
    require(t['enabled'] == (e['type'] == 'touch'), 'touch.enabled must match entry.type')
    _price_or_zero(t['price'], tick, 'touch.price')
    require(is_int(t['valid_until']) and t['valid_until'] >= 0, 'touch.valid_until must be Unix milliseconds')
    if t['enabled']:
        require(t['price'] == ref, 'touch.price must equal entry.price')
        require(t['valid_until'] > p['ts'], 'touch.valid_until must be after ts')
    if now_ms is not None:
        if t['enabled']:
            require(now_ms < t['valid_until'], 'touch entry expired (valid_until has passed)')
        else:
            age = now_ms - p['ts']
            require(age <= min(e['max_age_ms'], ENTRY_MAX_AGE_MS),
                    'entry alert is %.1f s old; older than its max_age_ms is refused' % (age / 1000))

    s = _block(p, 'stop')
    require(s['type'] in ('resting', 'bar_close'), 'stop.type must be resting or bar_close')
    require(on_tick(s['price'], tick), 'stop.price must be a positive price on the contract tick')
    require(_profit(-sign, s['price'], ref), 'stop.price is on the wrong side of entry.price')
    if s['type'] == 'resting':
        require(s['timeframe_s'] == 0 and s['safety_price'] == 0, 'a resting stop has timeframe_s 0 and safety_price 0')
    else:
        require(bar_timeframe(s['timeframe_s']), 'stop.timeframe_s must be 5-3600 s, a multiple of 5 dividing the hour')
        _price_or_zero(s['safety_price'], tick, 'stop.safety_price')
        require(s['safety_price'] == 0 or _profit(-sign, s['safety_price'], s['price']),
                'stop.safety_price must be beyond stop.price')
    resting_stop = s['type'] == 'resting' or s['safety_price'] > 0

    targets = p['targets']
    require(isinstance(targets, list) and len(targets) <= MAX_TARGETS, 'targets must be a list of at most 6')
    last, seen_market, limits = ref, False, 0
    for i, raw in enumerate(targets, 1):
        require(isinstance(raw, dict) and set(raw) == set(BLOCKS['target']),
                'targets[%d] must have exactly: price, qty, type' % i)
        require(raw['type'] in ('limit', 'market'), 'targets[%d].type must be limit or market' % i)
        require(on_tick(raw['price'], tick) and _profit(sign, raw['price'], ref),
                'targets[%d].price must be on tick and on the profit side of entry.price' % i)
        require(is_int(raw['qty']) and raw['qty'] >= 1, 'targets[%d].qty must be a positive whole number' % i)
        require(sign * (raw['price'] - last) >= 0, 'targets must be listed nearest first')
        if raw['type'] == 'limit':
            require(not seen_market, 'a resting limit target cannot come after a market target')
            limits += 1
        else:
            seen_market = True
        last = raw['price']
    require(limits <= MAX_RESTING_TARGETS, 'at most 3 targets may rest as limits')

    r = _block(p, 'runner')
    require(is_int(r['qty']) and r['qty'] >= 0, 'runner.qty must be a whole number')
    require(sum(x['qty'] for x in targets) + r['qty'] == e['qty'], 'targets qty + runner.qty must equal entry.qty')
    require(r['mode'] in ('hold', 'target', 'trail'), 'runner.mode must be hold, target or trail')
    _price_or_zero(r['price'], tick, 'runner.price')
    require(is_int(r['trail_activation_ticks']) and r['trail_activation_ticks'] >= 0
            and is_int(r['trail_distance_ticks']) and r['trail_distance_ticks'] >= 0, 'runner trail ticks must be whole numbers')
    if r['qty'] == 0:
        require(r['mode'] == 'hold', 'runner.mode must be hold when runner.qty is 0')
    elif r['mode'] == 'target':
        require(r['price'] > 0 and _profit(sign, r['price'], ref) and sign * (r['price'] - last) >= 0,
                'runner.price must be beyond every target')
    elif r['mode'] == 'trail':
        require(r['trail_distance_ticks'] >= 1, 'runner.trail_distance_ticks must be at least 1')

    b = _block(p, 'break_even')
    require(type(b['enabled']) is bool, 'break_even.enabled must be true or false')
    _price_or_zero(b['trigger_price'], tick, 'break_even.trigger_price')
    _price_or_zero(b['stop_price'], tick, 'break_even.stop_price')
    require(is_int(b['trigger_target']) and 0 <= b['trigger_target'] <= MAX_TARGETS, 'break_even.trigger_target')
    require(b['trigger_mode'] in ('any', 'all'), 'break_even.trigger_mode must be any or all')
    require(b['confirm'] in ('touch', 'bar_close'), 'break_even.confirm must be touch or bar_close')
    require(b['stop_type'] in ('resting', 'bar_close'), 'break_even.stop_type must be resting or bar_close')
    require(b['if_beyond'] in ('flatten', 'skip'), 'break_even.if_beyond must be flatten or skip')
    require(is_int(b['offset_ticks']) and -4000 <= b['offset_ticks'] <= 4000, 'break_even.offset_ticks')
    require(is_int(b['confirm_timeframe_s']) and is_int(b['stop_timeframe_s']), 'break_even timeframes')
    if b['enabled']:
        require(b['trigger_price'] > 0 or b['trigger_target'] > 0, 'break_even needs trigger_price or trigger_target')
        require(b['trigger_price'] == 0 or _profit(sign, b['trigger_price'], ref),
                'break_even.trigger_price must be on the profit side of entry.price')
        require(b['trigger_target'] <= len(targets), 'break_even.trigger_target names a missing target')
        require(b['confirm'] == 'touch' and b['confirm_timeframe_s'] == 0 or
                b['confirm'] == 'bar_close' and bar_timeframe(b['confirm_timeframe_s']),
                'break_even.confirm_timeframe_s must be 0 for touch or a bar timeframe for bar_close')
        require(b['stop_type'] == 'resting' and b['stop_timeframe_s'] == 0 or
                b['stop_type'] == 'bar_close' and bar_timeframe(b['stop_timeframe_s']),
                'break_even.stop_timeframe_s must be 0 for resting or a bar timeframe for bar_close')
        require(b['stop_type'] == 'bar_close' or resting_stop, 'break_even.stop_type resting needs a resting stop to move')

    z = _block(p, 'eod')
    require(type(z['enabled']) is bool, 'eod.enabled must be true or false')
    clock(z['time'])
    require(z['tz'] in TIME_ZONES, 'eod.tz must be one of: %s' % ', '.join(TIME_ZONES))
    ZoneInfo(z['tz'])

    d = _block(p, 'daily_loss')
    require(type(d['enabled']) is bool and type(d['include_open']) is bool and type(d['flatten']) is bool,
            'daily_loss switches must be true or false')
    require(is_number(d['limit_usd']) and d['limit_usd'] >= 0 and is_number(d['fee_per_contract_usd'])
            and d['fee_per_contract_usd'] >= 0, 'daily_loss amounts must be numbers of 0 or more')
    require(not d['enabled'] or d['limit_usd'] > 0, 'daily_loss.limit_usd must be above 0 when enabled')
    clock(d['day_start'])
    require(d['tz'] in TIME_ZONES, 'daily_loss.tz must be one of: %s' % ', '.join(TIME_ZONES))

    f = _block(p, 'bias_flip')
    require(type(f['enabled']) is bool, 'bias_flip.enabled must be true or false')


def main(argv=None):  # pragma: no cover - thin command-line wrapper, exercised by hand
    import sys
    from . import bridge_v2_registry
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__.strip().splitlines()[0])
        print('usage: python3 -m decrypt_trading.bridge_v2_contract alert.json [...]')
        return 2
    failed = 0
    for name in args:
        try:
            with open(name, 'rb') as handle:
                payload = parse(handle.read())
            validate(payload, strategy=bridge_v2_registry.strategy(payload.get('strategy')))
            print('ok    %s (%s %s, %d bytes)' % (name, payload['strategy'], payload['event'],
                                                  len(compact(payload).encode())))
        except (OSError, ValueError) as exc:
            failed += 1
            print('FAIL  %s: %s' % (name, exc))
    return 1 if failed else 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
