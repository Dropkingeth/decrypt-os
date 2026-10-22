"""Synthetic decrypt-bridge-v2 Lumi RTH Gap alerts for tests. Every value is an arbitrary test value that
exercises the public payload contract; none is taken from the strategy or its settings. entry(side='short')
matches fixtures/bridge_v2_lumi/plan-short-market.json: a market entry on MESM2021 with every block present
and every optional feature (touch, break-even, end of day, daily loss) off. touch_entry(...) builds a touch
entry (long by default) and event(...) a management alert. Tests override the blocks they exercise, the same
way tests/bridge_v2_payloads.py does.

Synthetic geometry (round points from entry): stop 8, first target +4 (qty 1), second target +12 (qty 2),
remainder target +30 (qty 3); 6 contracts in all.
"""
import copy

BASE = 1617718500000                      # the fixtures' bar open (a synthetic 2021 New York morning)
BAR_SECONDS = 30
BAR_CLOSE = BASE + BAR_SECONDS * 1000
ARRIVAL_MS = 1700                         # how long after the decision bar closes a synthetic alert is sent
STREAM = 'synth-gap-7'
CONTRACT = 'MESM2021'
BUILD = 'synthetic-r3'
TOUCH_WINDOW_MS = 1_500_000               # how long a synthetic touch entry stays valid
MAX_AGE_MS = 30_000


def trade_ref(n, side='long'):
    """The n-th synthetic trade id (the contract only requires it to end in :<side>)."""
    return 'q%03d:%s' % (n, side)


def alert_ref(kind, ts, trade_id):
    return '%s+%d@%s' % (kind, ts, trade_id)


TRADE = trade_ref(1)
TRADE_SHORT = trade_ref(1, 'short')

STOP, TP1, TP2, RUNNER = 8.0, 4.0, 12.0, 30.0
QTY, TP1_QTY, TP2_QTY, RUNNER_QTY = 6, 1, 2, 3
LONG_PRICE, SHORT_PRICE = 5000.0, 5004.0              # market entries
TOUCH_LONG_PRICE, TOUCH_SHORT_PRICE = 4997.0, 5003.0  # touch entries
LOSS_LIMIT_USD, FEE_USD = 110.0, 0.4


def _extras(setup='K', **over):
    value = {'setup': setup, 'chart_symbol': CONTRACT, 'bar_seconds': BAR_SECONDS, 'decision_time': BAR_CLOSE,
             'account_profile': 'synthetic profile', 'risk_usd': STOP * QTY * 5}
    value.update(over)
    return value


def entry(side='long', ts=BAR_CLOSE + ARRIVAL_MS, trade_id=None, extras=None, price=None, **blocks):
    sign = 1 if side == 'long' else -1
    if price is None:
        price = LONG_PRICE if side == 'long' else SHORT_PRICE
    value = {
        'protocol': 'decrypt-bridge-v2', 'strategy': 'lumi-gap', 'build': BUILD, 'event': 'entry',
        'alert_id': None, 'trade_id': trade_id or (TRADE if side == 'long' else TRADE_SHORT),
        'stream_id': STREAM, 'symbol': CONTRACT, 'side': side, 'ts': ts,
        'entry': {'type': 'market', 'qty': QTY, 'price': price, 'max_age_ms': MAX_AGE_MS},
        'touch': {'enabled': False, 'price': 0, 'valid_until': 0, 'cancel_if_stop_touched': False},
        'stop': {'type': 'resting', 'price': price - sign * STOP, 'timeframe_s': 0, 'safety_price': 0},
        'targets': [{'price': price + sign * TP1, 'qty': TP1_QTY, 'type': 'limit'},
                    {'price': price + sign * TP2, 'qty': TP2_QTY, 'type': 'limit'}],
        'runner': {'qty': RUNNER_QTY, 'mode': 'target', 'price': price + sign * RUNNER, 'trail_activation_ticks': 0,
                   'trail_distance_ticks': 0},
        'break_even': {'enabled': False, 'trigger_price': 0, 'trigger_target': 0, 'trigger_mode': 'any',
                       'confirm': 'bar_close', 'confirm_timeframe_s': 30, 'stop_price': 0, 'offset_ticks': 8,
                       'stop_type': 'resting', 'stop_timeframe_s': 0, 'if_beyond': 'flatten'},
        'eod': {'enabled': False, 'time': '15:45', 'tz': 'America/New_York'},
        'daily_loss': {'enabled': False, 'limit_usd': LOSS_LIMIT_USD, 'fee_per_contract_usd': FEE_USD,
                       'tz': 'America/New_York', 'day_start': '00:00', 'include_open': False, 'flatten': False},
        'bias_flip': {'enabled': False},
        'extras': extras or _extras(setup='K' if side == 'long' else 'M'),
    }
    for name, change in blocks.items():
        if name != 'extras' and isinstance(change, dict) and isinstance(value.get(name), dict):
            value[name] = dict(value[name], **change)
        else:
            value[name] = copy.deepcopy(change)
    if value['alert_id'] is None:
        value['alert_id'] = alert_ref('entry', value['ts'], value['trade_id'])
    return value


def touch_entry(side='long', ts=BAR_CLOSE + ARRIVAL_MS, trade_id=None, valid_until=None, **blocks):
    """A touch entry: entry.type touch, touch.enabled true, price == entry.price."""
    price = TOUCH_LONG_PRICE if side == 'long' else TOUCH_SHORT_PRICE
    base = dict(blocks)
    base.setdefault('entry', {}).update({'type': 'touch', 'price': price})
    base.setdefault('touch', {}).update({'enabled': True, 'price': price,
                                          'valid_until': valid_until if valid_until is not None else ts + TOUCH_WINDOW_MS,
                                          'cancel_if_stop_touched': True})
    base.setdefault('extras', _extras(setup='P'))
    return entry(side=side, ts=ts, trade_id=trade_id, price=price, **base)


def event(kind, trade_id=TRADE, side='long', ts=BAR_CLOSE + 45_000, **fields):
    value = {'protocol': 'decrypt-bridge-v2', 'strategy': 'lumi-gap', 'build': BUILD, 'event': kind,
             'alert_id': alert_ref(kind, ts, trade_id), 'trade_id': trade_id, 'stream_id': STREAM,
             'symbol': CONTRACT, 'side': side, 'ts': ts, 'reason': 'test', 'extras': {}}
    value.update(fields)
    return value
