"""decrypt-bridge-v2 strategy module: Order Blocks Decoded.

Research build for paper accounts only; ships off by default. This module only lets its bridge alert be
received and executed like any other decrypt-bridge-v2 strategy. No trade rule lives here -- see bridge_v2.py
and PAYLOAD-V2.md.

Extras (entry only): `zone_top` and `zone_bottom`, a price zone recorded for the audit trail.
No bias_flip extras: this strategy sends no bias_flip event.
"""
from . import bridge_v2

STRATEGY = bridge_v2.Strategy(
    strategy_id='order-blocks', adapter_id='order-blocks-bridge-v2', label='Order Blocks Decoded',
    extras={
        'entry': {'zone_top': {'type': 'price'}, 'zone_bottom': {'type': 'price'}},
    })

Engine, input_identity, payload_schema, signal_time, is_entry_payload, bridge_message = bridge_v2.module_api(STRATEGY)
