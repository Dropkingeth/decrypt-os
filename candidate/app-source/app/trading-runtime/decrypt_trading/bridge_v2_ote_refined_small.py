"""decrypt-bridge-v2 strategy module: OTE Refined Small.

The strategy's TradingView alert sends the whole trade plan in the shared decrypt-bridge-v2 envelope
(PAYLOAD-V2.md); no extras. Large and Small are separate lanes/adapters so one account trades exactly one of
them (see bridge_v2_ote_refined_large.py).
"""
from . import bridge_v2

STRATEGY = bridge_v2.Strategy(
    strategy_id='ote-refined-small', adapter_id='ote-refined-small-bridge-v2', label='OTE Refined Small (v2)')

Engine, input_identity, payload_schema, signal_time, is_entry_payload, bridge_message = bridge_v2.module_api(STRATEGY)
