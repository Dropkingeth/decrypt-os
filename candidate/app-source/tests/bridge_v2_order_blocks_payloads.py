"""Synthetic decrypt-bridge-v2 alerts for the `order-blocks` adapter tests (synthetic fixture).

entry(...) builds a complete market entry on MNQZ2026 with every contract block present: 2 contracts at
21000 (an arbitrary, on-tick literal), a resting stop 17 points away, end of day on and every other
optional exit (target, break-even, trail, daily loss lock) present and switched off with its own
`enabled`/mode field. Tests switch on the block they exercise with a plain block override, the same way as
tests/bridge_v2_payloads.py. Nothing here is derived from any strategy's own rules.
"""
import copy
from datetime import datetime, timezone

TICK = 0.25
BASE = int(datetime(2021, 6, 9, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)   # alert ts
STREAM = 'fx-ob-01'
CONTRACT = 'MNQZ2026'
TRADE = 'obx-1:long'
BUILD = 'fixture-build-1'


def entry(side='long', ts=BASE, trade_id=None, qty=2, price=21000.0, stop=None, **blocks):
    """One entry alert. `blocks` are contract-block overrides merged into the defaults, e.g.
    targets=[{'price': p, 'qty': n, 'type': 'limit'}], break_even={'enabled': True, 'trigger_price': p},
    runner={'mode': 'trail', ...}, daily_loss={'enabled': True, 'limit_usd': x}, eod={'enabled': False}.
    The runner holds whatever the targets leave, unless the override sets runner.qty itself."""
    if stop is None:
        stop = price - 17 if side == 'long' else price + 17
    tid = trade_id or TRADE.replace(':long', ':' + side)
    value = {
        'protocol': 'decrypt-bridge-v2', 'strategy': 'order-blocks', 'build': BUILD,
        'event': 'entry', 'alert_id': tid + ':entry', 'trade_id': tid, 'stream_id': STREAM,
        'symbol': CONTRACT, 'side': side, 'ts': ts,
        'entry': {'type': 'market', 'qty': qty, 'price': price, 'max_age_ms': 20000},
        'touch': {'enabled': False, 'price': 0, 'valid_until': 0, 'cancel_if_stop_touched': False},
        'stop': {'type': 'resting', 'price': stop, 'timeframe_s': 0, 'safety_price': 0},
        'targets': [],
        'runner': {'qty': qty, 'mode': 'hold', 'price': 0, 'trail_activation_ticks': 0, 'trail_distance_ticks': 0},
        'break_even': {'enabled': False, 'trigger_price': 0, 'trigger_target': 0, 'trigger_mode': 'any',
                       'confirm': 'bar_close', 'confirm_timeframe_s': 240, 'stop_price': 0, 'offset_ticks': 0,
                       'stop_type': 'resting', 'stop_timeframe_s': 0, 'if_beyond': 'flatten'},
        'eod': {'enabled': True, 'time': '15:45', 'tz': 'America/New_York'},
        'daily_loss': {'enabled': False, 'limit_usd': 60.0, 'fee_per_contract_usd': 1.5, 'tz': 'America/New_York',
                       'day_start': '00:00', 'include_open': False, 'flatten': False},
        'bias_flip': {'enabled': False},
        'extras': {'zone_top': price + 4, 'zone_bottom': price - 8},
    }
    for name, change in blocks.items():
        if name != 'extras' and isinstance(change, dict) and isinstance(value.get(name), dict):
            value[name] = dict(value[name], **change)
        else:
            value[name] = copy.deepcopy(change)
    if 'qty' not in blocks.get('runner', {}):
        value['runner']['qty'] = qty - sum(t['qty'] for t in value['targets'])
    return value


def flatten(reason, trade_id=TRADE, side='long', ts=BASE + 2_220_000):
    """A strategy-sent `flatten` event; it may duplicate the receiver's own eod/daily_loss clock.
    `reason` is free-form ('eod' or 'daily_loss' here)."""
    return {
        'protocol': 'decrypt-bridge-v2', 'strategy': 'order-blocks', 'build': BUILD,
        'event': 'flatten', 'alert_id': '%s:flatten-%s' % (trade_id, reason), 'trade_id': trade_id,
        'stream_id': STREAM, 'symbol': CONTRACT, 'side': side, 'ts': ts, 'reason': reason, 'extras': {},
    }
