"""Synthetic decrypt-bridge-v2 alerts for the OTE Refined Large/Small executor tests.

Every price, quantity, offset and clock time here is an arbitrary fixture value. entry(lane, ...) builds a
complete market entry with every contract block present (two market targets, a holding runner, break-even
on the first target fill, end of day on, the rest off); tests switch individual contract blocks with plain
block overrides, the same way as tests/bridge_v2_payloads.py.
"""
import copy
from datetime import datetime, timezone

BASE = int(datetime(2021, 6, 9, 17, 0, tzinfo=timezone.utc).timestamp() * 1000)   # arbitrary fixture time
STREAM = 'fx-otr-01'
CONTRACT = 'MNQZ2026'
TICK = 0.25
MAX_AGE_MS = 35000

# Arbitrary fixture geometry, in points from the entry price.
ENTRY_PRICE = 21000.0
STOP_POINTS = 20.0
FIRST_POINTS = 10.0
SECOND_POINTS = 30.0
LEVEL_POINTS = 15.0

# Arbitrary fixture sizes: (first target qty, second target qty, runner qty).
LARGE = {'strategy_id': 'ote-refined-large', 'build': 'fixture-build', 'tag': 'large', 'qty': (6, 3, 2)}
SMALL = {'strategy_id': 'ote-refined-small', 'build': 'fixture-build', 'tag': 'small', 'qty': (2, 2, 3)}


def trade_id_for(lane, side, seq):
    return 'fx-%s-%s:%s' % (lane['tag'], seq, side)


def entry(lane=LARGE, side='long', ts=BASE, trade_id=None, seq='a', sizes=None, **blocks):
    """`blocks` are contract-block overrides merged into the defaults (a list, such as `targets`, replaces
    the default). `sizes` = (first target, second target, runner) quantities; a 0 drops that target, and
    break-even then has no first target to trigger on, so it is sent disabled unless an override arms it."""
    sign = 1.0 if side == 'long' else -1.0
    qty1, qty2, run = sizes or lane['qty']
    price = ENTRY_PRICE
    tid = trade_id or trade_id_for(lane, side, seq)
    targets = [{'price': price + sign * points, 'qty': qty, 'type': 'market'}
               for points, qty in ((FIRST_POINTS, qty1), (SECOND_POINTS, qty2)) if qty]
    value = {
        'protocol': 'decrypt-bridge-v2', 'strategy': lane['strategy_id'], 'build': lane['build'],
        'event': 'entry', 'alert_id': tid + ':entry', 'trade_id': tid, 'stream_id': STREAM,
        'symbol': CONTRACT, 'side': side, 'ts': ts,
        'entry': {'type': 'market', 'qty': qty1 + qty2 + run, 'price': price, 'max_age_ms': MAX_AGE_MS},
        'touch': {'enabled': False, 'price': 0, 'valid_until': 0, 'cancel_if_stop_touched': False},
        'stop': {'type': 'resting', 'price': price - sign * STOP_POINTS, 'timeframe_s': 0, 'safety_price': 0},
        'targets': targets,
        'runner': {'qty': run, 'mode': 'hold', 'price': 0, 'trail_activation_ticks': 0, 'trail_distance_ticks': 0},
        'break_even': {'enabled': bool(qty1), 'trigger_price': 0, 'trigger_target': 1 if qty1 else 0,
                       'trigger_mode': 'any', 'confirm': 'touch', 'confirm_timeframe_s': 0,
                       'stop_price': price + sign * 3 * TICK, 'offset_ticks': 3, 'stop_type': 'resting',
                       'stop_timeframe_s': 0, 'if_beyond': 'flatten'},
        'eod': {'enabled': True, 'time': '15:30', 'tz': 'America/New_York'},
        'daily_loss': {'enabled': False, 'limit_usd': 150.0, 'fee_per_contract_usd': 2.0, 'tz': 'America/New_York',
                       'day_start': '00:00', 'include_open': False, 'flatten': False},
        'bias_flip': {'enabled': False},
        'extras': {},
    }
    for name, change in blocks.items():
        if name != 'extras' and isinstance(change, dict) and isinstance(value.get(name), dict):
            value[name] = dict(value[name], **change)
        else:
            value[name] = copy.deepcopy(change)
    return value


def bias_flip(lane=LARGE, trade_id=None, side='long', ts=BASE + 75_000, seq='a'):
    tid = trade_id or trade_id_for(lane, side, seq)
    return {
        'protocol': 'decrypt-bridge-v2', 'strategy': lane['strategy_id'], 'build': lane['build'],
        'event': 'bias_flip', 'alert_id': tid + ':flip', 'trade_id': tid,
        'stream_id': STREAM, 'symbol': CONTRACT, 'side': side, 'ts': ts, 'reason': 'bias_flip', 'extras': {},
    }
