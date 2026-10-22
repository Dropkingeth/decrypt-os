"""Strategy adapter registry and the adapter command format.

One small, explicit list of the strategy adapters the bridge accepts orders from. Soup is the
reference adapter. Another adapter plugs in with `register(Adapter(...))`: an id, the payload
schemas (format versions) its strategy alerts carry, the product roots it may trade and the order
kinds it may use. Anything else is refused (fails closed):

- an unknown adapter id, or a payload schema the adapter did not declare;
- an adapter that the binding's bridge config does not enable (every adapter is off by default);
- an order kind the adapter did not declare, or a contract root it did not declare.

These are safety checks only (format, identity, freshness). The strategy owns every trading rule;
an adapter turns its strategy's alert into exactly these orders and nothing else.

Bridge config (`config.json` of the binding), key `adapters`:
    absent                                    Soup only, exactly as before (the reference default)
    {"<adapter id>": {"enabled": true}}       this adapter only; Soup payloads are then refused
At most one adapter is enabled per binding (one strategy per account and contract).

Adapter command format (POST /order/<token>), next to Soup's unchanged payloads:
    {"ticker": "<dated contract>", "action": ..., ..., "extras": {
        "adapter_id": "<registered id>", "adapter_schema": "<declared schema>",
        "command_id": "<unique id>", "trade_id": "<id ending in :long or :short>",
        "signal_time": <alert time, Unix ms>,          entries only, at most 120 s old
        "bridge_kind": ..., <the kind's own fields>}}  bracket_entry, modify_stop, flatten_all
See ADAPTER_FIELDS below and ENGINES.md. Each kind has an exact field set: no missing or extra key.

Pure data and checks: no I/O and no imports from the bridge. The trading runtime keeps a byte-identical
copy (app/trading-runtime/decrypt_trading/bridge_adapters.py); a test keeps the two equal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SOUP = 'soup-pine-authority-r4'

# The dated contracts a binding may trade: product root -> CME tick size and contract months.
# One dated contract per binding (for example MNQZ2026 or MESZ2026). An unknown root is refused.
CONTRACTS = {
    'MNQ': {'tick_size': 0.25, 'months': 'HMUZ', 'exchange': 'CME_MINI'},
    'MES': {'tick_size': 0.25, 'months': 'HMUZ', 'exchange': 'CME_MINI'},
}

# Order kinds of the bridge (tvbridge.executor). An adapter declares the ones it uses.
ORDER_KINDS = ('entry', 'bracket_entry', 'tp1_limit', 'exit_market', 'cancel', 'modify_stop', 'flatten_all')
ENTRY_KINDS = ('entry', 'bracket_entry')
EXTENDED_KINDS = ('bracket_entry', 'modify_stop', 'flatten_all')

SIGNAL_MAX_AGE_MS = 120_000     # an entry whose alert is older is never sent (no replay after a restart)
SIGNAL_MAX_AHEAD_MS = 5_000     # clock skew allowed for an alert time slightly in the future

_ID = re.compile(r'[a-z0-9][a-z0-9.-]{2,63}\Z')
_SCHEMA = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,63}\Z')
_COMMAND_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@+#-]{0,159}\Z')
_TRADE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,199}:(long|short)\Z')
_TICKER = re.compile(r'([A-Z0-9]{1,5}?)([FGHJKMNQUVXZ])(20\d\d)\Z')

_BASE = ('adapter_id', 'adapter_schema', 'command_id', 'trade_id')
# Exact top-level fields and extras of each kind. `(name, required)` for extras.
ADAPTER_FIELDS = {
    'entry': ({'ticker', 'action', 'orderType', 'quantity', 'extras'}, {'signal_time': True}),
    'tp1_limit': ({'ticker', 'action', 'orderType', 'quantity', 'limitPrice', 'cancel', 'extras'}, {}),
    'exit_market': ({'ticker', 'action', 'orderType', 'quantity', 'cancel', 'extras'}, {}),
    'cancel': ({'ticker', 'action', 'extras'}, {}),
    'bracket_entry': ({'ticker', 'action', 'orderType', 'quantity', 'extras'},
                      {'signal_time': True, 'bridge_kind': True, 'reference_price': True, 'stop_price': True,
                       'tp1_price': False, 'tp1_quantity': False}),
    'modify_stop': ({'ticker', 'action', 'extras'},
                    {'bridge_kind': True, 'stop_price': False, 'stop_quantity': False}),
    'flatten_all': ({'ticker', 'action', 'extras'}, {'bridge_kind': True}),
}


class AdapterError(ValueError):
    """A refused adapter, binding, contract or command. The message is a plain sentence."""


@dataclass(frozen=True)
class Adapter:
    adapter_id: str
    label: str
    schemas: tuple          # payload schema ids/versions of the strategy's alerts
    roots: tuple            # product roots the adapter may trade (keys of CONTRACTS)
    kinds: tuple            # order kinds the adapter may use (ORDER_KINDS)
    take_profits: int = 1   # resting take-profit limits per trade; 1 = TP1 only (rulebook)
    profile: str = ''       # the declared take-profit profile; required when take_profits > 1
    reference: bool = False


REGISTRY = {}


def register(adapter):
    """Add an adapter to the registry. Raises AdapterError on a bad or duplicate entry."""
    if not isinstance(adapter, Adapter):
        raise AdapterError('register() takes an Adapter')
    a = adapter
    if not isinstance(a.adapter_id, str) or not _ID.match(a.adapter_id):
        raise AdapterError(f'adapter id {a.adapter_id!r} must be 3-64 lowercase letters, digits, dots or dashes')
    if a.adapter_id in REGISTRY:
        raise AdapterError(f'adapter {a.adapter_id} is already registered')
    if not isinstance(a.label, str) or not a.label.strip():
        raise AdapterError(f'adapter {a.adapter_id}: label required')
    for name, values, check in (('schemas', a.schemas, lambda s: isinstance(s, str) and _SCHEMA.match(s)),
                                ('roots', a.roots, lambda r: r in CONTRACTS),
                                ('kinds', a.kinds, lambda k: k in ORDER_KINDS)):
        if not isinstance(values, tuple) or not values or len(set(values)) != len(values) \
                or not all(check(v) for v in values):
            raise AdapterError(f'adapter {a.adapter_id}: {name} must be a non-empty tuple of known values')
    if not any(k in ENTRY_KINDS for k in a.kinds):
        raise AdapterError(f'adapter {a.adapter_id}: kinds need an entry kind')
    if type(a.take_profits) is not int or not 1 <= a.take_profits <= 3:
        raise AdapterError(f'adapter {a.adapter_id}: take_profits must be 1, 2 or 3')
    if a.take_profits > 1 and not (isinstance(a.profile, str) and _SCHEMA.match(a.profile)):
        # The rulebook allows more than TP1 resting only for a profile that explicitly declares it.
        raise AdapterError(f'adapter {a.adapter_id}: more than one resting take-profit needs a declared profile')
    if not isinstance(a.reference, bool):
        raise AdapterError(f'adapter {a.adapter_id}: reference must be true or false')
    REGISTRY[a.adapter_id] = a
    return a


def lookup(adapter_id, schema=None):
    """The registered adapter, checking `schema` against its declared schemas when given."""
    adapter = REGISTRY.get(adapter_id) if isinstance(adapter_id, str) else None
    if adapter is None:
        raise AdapterError('unknown adapter id')
    if schema is not None and schema not in adapter.schemas:
        raise AdapterError(f'payload schema is not declared by adapter {adapter.adapter_id}')
    return adapter


def contract_spec(ticker):
    """{'root', 'tick_size', ...} of a dated contract such as MNQZ2026 or MESZ2026. Raises AdapterError."""
    m = _TICKER.match(ticker) if isinstance(ticker, str) else None
    if not m:
        raise AdapterError(f'ticker {ticker!r} must be one dated contract, for example MNQZ2026')
    root, month = m.group(1), m.group(2)
    spec = CONTRACTS.get(root)
    if spec is None:
        raise AdapterError(f'contract root {root} is not supported (known: {", ".join(sorted(CONTRACTS))})')
    if month not in spec['months']:
        raise AdapterError(f'{ticker}: {root} trades the {spec["months"]} contract months only')
    return dict(spec, root=root)


def check_contract(ticker, tick_size):
    """The binding's contract and tick size are a known pair. Returns the contract spec; raises AdapterError."""
    spec = contract_spec(ticker)
    if type(tick_size) not in (int, float) or tick_size != spec['tick_size']:
        raise AdapterError(f'tick_size for {spec["root"]} must be {spec["tick_size"]} (found {tick_size!r})')
    return spec


def binding_adapters(cfg):
    """{adapter id: Adapter} enabled for this binding by `cfg['adapters']`. Raises AdapterError.

    Absent: Soup only (the reference default, unchanged). Present: an object of
    {"<registered id>": {"enabled": true|false}}; at most one enabled; none enabled is allowed
    (nothing trades on this binding). The binding's contract root must be one the adapter declares.
    """
    if not isinstance(cfg, dict):
        raise AdapterError('bridge config must be an object')
    raw = cfg.get('adapters')
    if raw is None and 'adapters' not in cfg:
        enabled = {SOUP: REGISTRY[SOUP]}
    else:
        if not isinstance(raw, dict):
            raise AdapterError('adapters must be an object of {"<adapter id>": {"enabled": true}}')
        enabled = {}
        for adapter_id, entry in raw.items():
            adapter = lookup(adapter_id) if adapter_id in REGISTRY else None
            if adapter is None:
                raise AdapterError(f'adapters: {adapter_id!r} is not a registered adapter')
            if not isinstance(entry, dict) or set(entry) != {'enabled'} or not isinstance(entry['enabled'], bool):
                raise AdapterError(f'adapters.{adapter_id} must be {{"enabled": true}} or {{"enabled": false}}')
            if entry['enabled']:
                enabled[adapter_id] = adapter
        if len(enabled) > 1:
            raise AdapterError('adapters: at most one adapter may be enabled per binding '
                               '(one strategy per account and contract)')
    ticker = cfg.get('ticker')
    if ticker is not None:
        root = contract_spec(ticker)['root']
        for adapter in enabled.values():
            if root not in adapter.roots:
                raise AdapterError(f'adapter {adapter.adapter_id} does not trade {root} '
                                   f'(declared: {", ".join(adapter.roots)})')
    return enabled


def is_adapter_command(payload):
    """True for the adapter command format (extras.adapter_id), False for Soup's own payloads."""
    return isinstance(payload, dict) and isinstance(payload.get('extras'), dict) and 'adapter_id' in payload['extras']


def command_kind(payload):
    """The order kind of an adapter command, read from its shape. Raises AdapterError."""
    extras = payload['extras']
    action, order_type = payload.get('action'), payload.get('orderType')
    if 'bridge_kind' in extras:
        kind = extras['bridge_kind']
        if kind not in EXTENDED_KINDS:
            raise AdapterError('unsupported bridge_kind')
        return kind
    if action in ('buy', 'sell'):
        if order_type != 'market':
            raise AdapterError('entries are market orders only')
        return 'entry'
    if action == 'exit' and order_type == 'limit':
        return 'tp1_limit'
    if action == 'exit' and order_type == 'market':
        return 'exit_market'
    if action == 'cancel':
        return 'cancel'
    raise AdapterError('unsupported action/orderType')


def validate_command(payload, *, ticker, enabled, now_ms=None):
    """Check an adapter command's format, identity and freshness. Returns (Adapter, kind).

    `ticker` is the binding's dated contract; `enabled` is binding_adapters(cfg). With `now_ms`, an entry's
    `signal_time` must be at most SIGNAL_MAX_AGE_MS old and at most SIGNAL_MAX_AHEAD_MS ahead. Prices,
    quantities and the position are checked by the bridge's order guards, the same ones Soup's orders pass.
    """
    if not is_adapter_command(payload):
        raise AdapterError('not an adapter command')
    extras = payload['extras']
    adapter = lookup(extras.get('adapter_id'), extras.get('adapter_schema'))
    if adapter.reference:
        raise AdapterError(f'{adapter.adapter_id} sends its own payload format, not adapter commands')
    if adapter.adapter_id not in enabled:
        raise AdapterError(f'adapter {adapter.adapter_id} is not enabled on this binding')
    if payload.get('ticker') != ticker:
        raise AdapterError('ticker is not the pinned ticker')
    if contract_spec(ticker)['root'] not in adapter.roots:
        raise AdapterError(f'adapter {adapter.adapter_id} does not trade this contract')
    kind = command_kind(payload)
    if kind not in adapter.kinds:
        raise AdapterError(f'order kind {kind} is not declared by adapter {adapter.adapter_id}')
    fields, extra_keys = ADAPTER_FIELDS[kind]
    if set(payload) != fields:
        raise AdapterError(f'{kind} command has missing or unexpected fields')
    allowed = set(_BASE) | set(extra_keys)
    required = set(_BASE) | {k for k, need in extra_keys.items() if need}
    if set(extras) - allowed or required - set(extras):
        raise AdapterError(f'{kind} command has missing or unexpected extras')
    if not isinstance(extras['command_id'], str) or not _COMMAND_ID.match(extras['command_id']):
        raise AdapterError('command_id must be 1-160 safe characters')
    if '#' in extras['command_id']:
        raise AdapterError('command_id must not contain "#" (reserved for bridge sub-commands)')
    if not isinstance(extras['trade_id'], str) or not _TRADE_ID.match(extras['trade_id']):
        raise AdapterError('trade_id must be safe characters ending in :long or :short')
    if kind in ('tp1_limit', 'exit_market') and payload.get('cancel') is not False:
        raise AdapterError('exit must carry cancel:false')
    if kind in ('tp1_limit', 'exit_market', 'entry', 'bracket_entry'):
        qty = payload.get('quantity')
        if type(qty) is not int or qty <= 0:
            raise AdapterError('invalid quantity')
    if kind in ENTRY_KINDS:
        at = extras.get('signal_time')
        if type(at) is not int or at <= 0:
            raise AdapterError('signal_time must be the alert time in Unix milliseconds')
        if now_ms is not None and not -SIGNAL_MAX_AHEAD_MS <= now_ms - at <= SIGNAL_MAX_AGE_MS:
            raise AdapterError(f'entry signal is {(now_ms - at) / 1000:.1f} s old; entries older than '
                               f'{SIGNAL_MAX_AGE_MS // 1000} s (or ahead of the clock) are never sent')
    return adapter, kind


def to_bridge_payload(payload, kind):
    """The same command in the bridge's own order format (Soup's), for the shared order guards."""
    extras = payload['extras']
    out = {k: v for k, v in payload.items() if k != 'extras'}
    out['extras'] = {'soup_command_id': extras['command_id'], 'soup_trade_id': extras['trade_id']}
    out['extras'].update({k: v for k, v in extras.items()
                          if k not in _BASE and k != 'signal_time'})
    return out


register(Adapter(SOUP, 'Turtle Soup R4 (reference)',
                 schemas=('decrypt-soup-entry-v3', 'decrypt-soup-entry-v4', 'decrypt-soup-management-v3'),
                 roots=('MNQ',), kinds=ORDER_KINDS, reference=True))


# --- decrypt-bridge-v2: the shared alert envelope of every non-Soup strategy --------------------
# The only non-Soup format the bridge accepts.
# One engine reads it (app/trading-runtime/decrypt_trading/bridge_v2.py); the format is
# bridge/PAYLOAD-V2.md. A v2 strategy uses every order kind: a market entry (with its resting stop as a
# bracket, or plain when its stop is watched on bar closes), up to `take_profits` resting limits (the
# targets the alert declares as limits), stop moves, market exits and flatten. Off on every binding until
# its bridge config enables it. ONE line per strategy:
#     (adapter id, label, contract roots, resting take-profit limits 1-3)
V2_SCHEMA = 'decrypt-bridge-v2'
V2_PROFILE = 'decrypt-bridge-v2-targets'     # the alert's own targets[] declare the resting limits
V2_KINDS = ORDER_KINDS
V2_ADAPTERS = (
    ('fvg-r5b-bridge-v2', 'FVG/IFVG', ('MNQ',), 3),
    ('lumi-gap-bridge-v2', 'Lumi RTH Gap', ('MES',), 3),
    ('ote-refined-large-bridge-v2', 'OTE Refined Large (v2)', ('MNQ',), 2),
    ('ote-refined-small-bridge-v2', 'OTE Refined Small (v2)', ('MNQ',), 2),
    ('ote-active-bridge-v2', 'OTE Active (Silver Bullet)', ('MNQ',), 2),
    ('order-blocks-bridge-v2', 'Order Blocks Decoded', ('MNQ',), 1),
)
for _id, _label, _roots, _resting in V2_ADAPTERS:
    register(Adapter(_id, _label, schemas=(V2_SCHEMA,), roots=_roots, kinds=V2_KINDS, take_profits=_resting,
                     profile=V2_PROFILE))
