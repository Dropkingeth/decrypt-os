"""Synthetic decrypt-bridge-v2 alerts for tests (no real accounts, ids or strategy code).

entry(...) builds a complete market entry on MNQZ2026 with every feature present and switched off, the
way a strategy sends its settings: tests switch on the feature they exercise with keyword overrides.
"""
import copy
from datetime import datetime, timezone

BASE = int(datetime(2021, 6, 9, 14, 30, tzinfo=timezone.utc).timestamp() * 1000)   # 10:30 New York
STREAM = 'fixture-stream'
CONTRACT = 'MNQZ2026'
TRADE = 'ex1:MNQZ2026:%d:long' % (BASE - 60_000)


def entry(side='long', ts=BASE, trade_id=None, **blocks):
    sign = 1 if side == 'long' else -1
    price = 21000.0
    value = {
        'protocol': 'decrypt-bridge-v2', 'strategy': 'example-v2', 'build': 'example-1.0', 'event': 'entry',
        'alert_id': None, 'trade_id': trade_id or TRADE.replace(':long', ':' + side), 'stream_id': STREAM,
        'symbol': CONTRACT, 'side': side, 'ts': ts,
        'entry': {'type': 'market', 'qty': 4, 'price': price, 'max_age_ms': 40000},
        'touch': {'enabled': False, 'price': 0, 'valid_until': 0, 'cancel_if_stop_touched': False},
        'stop': {'type': 'resting', 'price': price - sign * 20, 'timeframe_s': 0, 'safety_price': 0},
        'targets': [{'price': price + sign * 10, 'qty': 1, 'type': 'limit'},
                    {'price': price + sign * 20, 'qty': 1, 'type': 'limit'}],
        'runner': {'qty': 2, 'mode': 'hold', 'price': 0, 'trail_activation_ticks': 0, 'trail_distance_ticks': 0},
        'break_even': {'enabled': False, 'trigger_price': price + sign * 10, 'trigger_target': 0, 'trigger_mode': 'any',
                       'confirm': 'touch', 'confirm_timeframe_s': 0, 'stop_price': 0, 'offset_ticks': 2,
                       'stop_type': 'resting', 'stop_timeframe_s': 0, 'if_beyond': 'flatten'},
        'eod': {'enabled': False, 'time': '15:30', 'tz': 'America/New_York'},
        'daily_loss': {'enabled': False, 'limit_usd': 100, 'fee_per_contract_usd': 1.0, 'tz': 'America/New_York',
                       'day_start': '00:00', 'include_open': False, 'flatten': False},
        'bias_flip': {'enabled': False},
        'extras': {'setup': 'A'},
    }
    for name, change in blocks.items():
        if name != 'extras' and isinstance(change, dict) and isinstance(value.get(name), dict):
            value[name] = dict(value[name], **change)
        else:
            value[name] = copy.deepcopy(change)
    if value['alert_id'] is None:
        value['alert_id'] = value['trade_id'] + ':entry'
    return value


def event(kind, trade_id=TRADE, side='long', ts=BASE + 60_000, **fields):
    value = {'protocol': 'decrypt-bridge-v2', 'strategy': 'example-v2', 'build': 'example-1.0', 'event': kind,
             'alert_id': '%s:%s:%d' % (trade_id, kind, ts), 'trade_id': trade_id, 'stream_id': STREAM,
             'symbol': CONTRACT, 'side': side, 'ts': ts, 'reason': 'test', 'extras': {}}
    if kind == 'bias_flip':
        value['extras'] = {'new_trend': 'bear' if side == 'long' else 'bull'}
    value.update(fields)
    return value
