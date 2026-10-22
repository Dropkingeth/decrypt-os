"""A decrypt-bridge-v2 strategy module for tests only: never shipped, not a strategy.

It is exactly what a real strategy adds as `app/trading-runtime/decrypt_trading/bridge_v2_<name>.py`:
a Strategy declaration (its strategy id, its adapter id and its extras schema) and the module API.
"""
from decrypt_trading import bridge_v2

STRATEGY = bridge_v2.Strategy(
    strategy_id='example-v2', adapter_id='example-bridge-v2', label='Example strategy (tests only)',
    extras={
        'entry': {'setup': {'type': 'str', 'pattern': r'[A-Za-z0-9_]{1,16}', 'max_len': 16},
                  'risk_usd': {'type': 'number', 'min': 0, 'optional': True}},
        'bias_flip': {'new_trend': {'type': 'enum', 'values': ('bull', 'bear')}},
    })

Engine, input_identity, payload_schema, signal_time, is_entry_payload, bridge_message = bridge_v2.module_api(STRATEGY)
