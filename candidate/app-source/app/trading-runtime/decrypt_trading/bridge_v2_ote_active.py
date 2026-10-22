"""OTE Active (Silver Bullet) on decrypt-bridge-v2 (beta4).

The strategy's TradingView alert carries the whole plan (PAYLOAD-V2.md): entry, stop, targets, runner,
break-even, end of day, daily loss lock and the bias-flip exit, each with the values the alert sends. This
module only declares the strategy and its extras: metadata the receiver records with the trade and never
acts on. An entry may carry a few further opaque metadata keys (bridge_v2_contract.OPAQUE); they are
format-checked only.
"""
from . import bridge_v2

_TREND = {'type': 'enum', 'values': ('bullish', 'bearish', 'neutral')}
_EVENT = {'new_trend': _TREND, 'price': {'type': 'number', 'min': 0}}

STRATEGY = bridge_v2.Strategy(
    strategy_id='ote-active', adapter_id='ote-active-bridge-v2', label='OTE Active (Silver Bullet)',
    extras={
        'entry': {
            'profile': {'type': 'str', 'pattern': r'[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}', 'max_len': 40},
            bridge_v2.contract.OPAQUE: {'type': 'opaque', 'max_keys': 8},
        },
        'bias_flip': _EVENT,
        'exit': _EVENT,
    })

Engine, input_identity, payload_schema, signal_time, is_entry_payload, bridge_message = bridge_v2.module_api(STRATEGY)
