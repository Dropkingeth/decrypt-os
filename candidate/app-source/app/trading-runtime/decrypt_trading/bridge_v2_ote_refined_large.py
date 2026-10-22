"""decrypt-bridge-v2 strategy module: OTE Refined Large.

The strategy's TradingView alert sends the whole trade plan in the shared decrypt-bridge-v2 envelope
(PAYLOAD-V2.md); every field the receiver needs is in the generic contract, so this strategy declares no
extras of its own. One lane per account: `ote-refined-small-bridge-v2` is a separate adapter/strategy id
(bridge_v2_ote_refined_small.py), and the binding only enables one adapter at a time.
"""
from . import bridge_v2

STRATEGY = bridge_v2.Strategy(
    strategy_id='ote-refined-large', adapter_id='ote-refined-large-bridge-v2', label='OTE Refined Large (v2)')

Engine, input_identity, payload_schema, signal_time, is_entry_payload, bridge_message = bridge_v2.module_api(STRATEGY)
