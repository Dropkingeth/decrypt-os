"""FVG/IFVG on decrypt-bridge-v2 (beta4): adapter `fvg-r5b-bridge-v2`, alerts `strategy: "fvg-r5b"`.

The strategy's TradingView alert carries the whole plan in the shared format (PAYLOAD-V2.md); the generic
receiver (bridge_v2.py) does exactly what it says. This module only declares the strategy, its extras and an
identity format check; it adds no trading rule. The alert may use any block of the contract (entry, stop with
an optional resting safety stop, targets, runner, break_even, eod, daily_loss, bias_flip) and the management
events `flatten` and `bias_flip`, each with the values the alert sends.

Extras are recorded with the trade and do nothing here: the entry's account profile and stop size in
ticks, and a reference price on management events. The format check below adds identity checks only.
"""
import re

from . import bridge_v2
from .bridge_v2_contract import PayloadError

_TRADE_ID = re.compile(r'fvg1:(MNQ[HMUZ]20[2-9][0-9]):([0-9]{13}):(long|short)\Z')


def _identity(p):
    """Format only: the trade id names this alert's contract and side, and every alert id starts with it."""
    m = _TRADE_ID.match(p['trade_id'])
    if not m or m.group(1) != p['symbol']:
        raise PayloadError('trade_id must be fvg1:<contract>:<bar time ms>:<side> for this alert\'s contract')
    if not p['alert_id'].startswith(p['trade_id'] + ':'):
        raise PayloadError('alert_id must start with the trade_id')
    if p['event'] == 'entry' and p['alert_id'] != p['trade_id'] + ':entry':
        raise PayloadError('an entry alert_id is <trade_id>:entry')
    if int(m.group(2)) >= p['ts']:
        raise PayloadError('ts must be after the signal bar time in the trade_id')


_REF = {'ref_price': {'type': 'price'}}

STRATEGY = bridge_v2.Strategy(
    strategy_id='fvg-r5b', adapter_id='fvg-r5b-bridge-v2', label='FVG/IFVG',
    extras={
        'entry': {'profile': {'type': 'str', 'pattern': r'[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}', 'max_len': 40},
                  'risk_ticks': {'type': 'number', 'min': 0}},
        'flatten': _REF,
        'bias_flip': _REF,
    },
    check=_identity)

Engine, input_identity, payload_schema, signal_time, is_entry_payload, bridge_message = bridge_v2.module_api(STRATEGY)
