"""Synthetic decrypt-bridge-v2 alerts for the `fvg-r5b` adapter tests (synthetic fixture).

All values are arbitrary, round and on-tick (0.25); they only exercise the payload contract. entry(...)
builds an 8-contract bracket with an even split (2/2/2 + runner 2), a bar-close stop plus a resting safety
stop, break-even on a price trigger, EOD, a daily loss lock and the bias-flip exit. Prices: entry 21000,
bar-close stop 20990, safety stop 20985, targets 21010 / 21020 / 21030, break-even stop 21002.
"""
import copy
from datetime import datetime, timezone

TICK = 0.25
BASE = int(datetime(2021, 6, 9, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)   # signal bar open
CLOSE = BASE + 300_000                                                              # bar close, ts of the alert
STREAM = 'fx-fvg-01'
SYMBOL = 'MNQZ2026'
TRADE = 'fvg1:%s:%d:long' % (SYMBOL, BASE)


def entry(side='long', ts=CLOSE, bar_open=BASE, trade_id=None, **blocks):
    long = side == 'long'
    price = 21000.0
    stop = 20990.0 if long else 21010.0
    safety = 20985.0 if long else 21015.0
    tp1, tp2, tp3 = (21010.0, 21020.0, 21030.0) if long else (20990.0, 20980.0, 20970.0)
    be_price = 21002.0 if long else 20998.0
    tid = trade_id or ('fvg1:%s:%d:%s' % (SYMBOL, bar_open, side))
    value = {
        'protocol': 'decrypt-bridge-v2', 'strategy': 'fvg-r5b', 'build': 'fixture-build-1', 'event': 'entry',
        'alert_id': None, 'trade_id': tid, 'stream_id': STREAM, 'symbol': SYMBOL, 'side': side, 'ts': ts,
        'entry': {'type': 'market', 'qty': 8, 'price': price, 'max_age_ms': 60000},
        'touch': {'enabled': False, 'price': 0, 'valid_until': 0, 'cancel_if_stop_touched': False},
        'stop': {'type': 'bar_close', 'price': stop, 'timeframe_s': 300, 'safety_price': safety},
        'targets': [{'price': tp1, 'qty': 2, 'type': 'limit'}, {'price': tp2, 'qty': 2, 'type': 'limit'},
                    {'price': tp3, 'qty': 2, 'type': 'limit'}],
        'runner': {'qty': 2, 'mode': 'hold', 'price': 0, 'trail_activation_ticks': 0, 'trail_distance_ticks': 0},
        'break_even': {'enabled': True, 'trigger_price': tp1, 'trigger_target': 1, 'trigger_mode': 'any',
                       'confirm': 'touch', 'confirm_timeframe_s': 0, 'stop_price': be_price, 'offset_ticks': 0,
                       'stop_type': 'resting', 'stop_timeframe_s': 0, 'if_beyond': 'flatten'},
        'eod': {'enabled': True, 'time': '15:30', 'tz': 'America/New_York'},
        'daily_loss': {'enabled': True, 'limit_usd': 100.0, 'fee_per_contract_usd': 1.0, 'tz': 'America/New_York',
                       'day_start': '00:00', 'include_open': False, 'flatten': False},
        'bias_flip': {'enabled': True},
        'extras': {'profile': 'fx-p1', 'risk_ticks': 40.0},
    }
    for name, change in blocks.items():
        if name != 'extras' and isinstance(change, dict) and isinstance(value.get(name), dict):
            value[name] = dict(value[name], **change)
        else:
            value[name] = copy.deepcopy(change)
    if value['alert_id'] is None:
        value['alert_id'] = value['trade_id'] + ':entry'
    return value


def price_trigger_only(side='long', **blocks):
    """Break-even on a price trigger that matches no target (trigger_target 0): two targets + runner,
    6 contracts, trigger at 21006 (long) / 20994 (short)."""
    value = entry(side=side, entry={'qty': 6}, runner={'qty': 2}, **blocks)
    value['targets'] = value['targets'][1:]
    value['break_even'] = dict(value['break_even'], trigger_target=0,
                               trigger_price=21006.0 if side == 'long' else 20994.0)
    return value


def event(kind, trade_id=TRADE, side='long', ts=CLOSE + 120_000, reason='test', ref_price=21000.0):
    """A management alert: kind `bias_flip`, or kind `flatten` with a free-form `reason`."""
    return {'protocol': 'decrypt-bridge-v2', 'strategy': 'fvg-r5b', 'build': 'fixture-build-1', 'event': kind,
            'alert_id': '%s:%s:%d' % (trade_id, kind, ts), 'trade_id': trade_id, 'stream_id': STREAM,
            'symbol': SYMBOL, 'side': side, 'ts': ts, 'reason': reason, 'extras': {'ref_price': ref_price}}
