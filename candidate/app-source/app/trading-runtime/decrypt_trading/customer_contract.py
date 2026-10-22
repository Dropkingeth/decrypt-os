"""Versioned native customer wire contract. No provider access or source loading.

The module-side copy must have identical bytes. Payloads are validated against
the reviewed pair; metadata supplies correlation, never new strategy rules.
"""
import hashlib
import json
import math
import re

from . import bridge_adapters

BINDING = 'decrypt.customer-tradovate-binding.v2'
ENVELOPE = 'decrypt.customer-tradovate-command.v1'
CONFIRMATION = 'decrypt.customer-tradovate-confirmation.v1'
OBSERVE = 'decrypt.customer-tradovate-observe.v1'
SOUP = 'soup-pine-authority-r4'
# Soup, plus one line per decrypt-bridge-v2 strategy (added at the end of this file).
PAIRS = {
    SOUP: ('0a7803755e3e84c02f79252ef8938e093cc6a452715b49f7a2e29c1b86281d2a', '32d09c04e6edec49ccb9c5d5f107d9ed43669c3bd60b89efebdf9b08f89b730f'),
}
BINDING_FIELDS = set('schema provider loginGroupId instanceId customerId installationId pairId engineSha256 strategySha256 accountId accountMode nativeAccountId nativeAccountName routeId routeSha256 feedId streamId symbol root tickSize tp1LimitOffsetHandles signalProducerId feedProducerId brokerProducerId'.split())
IDENTITIES = 'instanceId customerId installationId accountId routeId feedId signalProducerId feedProducerId brokerProducerId'.split()
COMMAND_FIELDS = set('commandId tradeId kind positionSide quantity price quantityLimit bridgeKind stopPrice'.split())
CONFIRMATION_COMMON = set('schema instanceId accountId pairId symbol sequence observedAt positionsComplete ordersComplete pendingComplete correlationComplete routeVerified accountMode routeId positionQuantity workingOrderIds pendingOrderIds unattributedOrderIds otherContractExposure engineEvent'.split())
CONFIRMATION_FIELDS = CONFIRMATION_COMMON | {'provider', 'nativeAccountId', 'routeSha256', 'connectionEpoch', 'acquisitionId'}


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identity(value, long=False):
    return type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:@+#-]{1,240}' if long else r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value) is not None


def native(value):
    return type(value) is dict and value.get('schema') == BINDING


def route_digest(value):
    # No secret, TP URL or subscription is represented by this digest.
    return digest({key: value[key] for key in ('provider', 'loginGroupId', 'accountMode', 'nativeAccountId', 'nativeAccountName', 'accountId', 'symbol', 'tickSize', 'routeId')})


def pair_supported(value):
    """The binding's pair and its two pins are still shipped (Soup or a decrypt-bridge-v2 strategy)."""
    pair = value.get('pairId') if type(value) is dict else None
    return type(pair) is str and pair in PAIRS and (value.get('engineSha256'), value.get('strategySha256')) == PAIRS[pair]


def validate_binding(value, *, retired_pair=False):
    """A supported binding. With retired_pair=True: a stored binding whose pair is no longer shipped (a retired
    engine), checked in every other field so it can be shown and retired, never opened or traded."""
    need(type(value) is dict and set(value) == BINDING_FIELDS and native(value), 'native_binding_fields')
    need(value['loginGroupId'] in tuple('login-%d' % i for i in range(1,9)), 'native_login_group')
    need(value['provider'] == 'tradovate' and value['accountMode'] in ('demo', 'live'), 'native_provider_mode')
    need(all(identity(value[key]) for key in IDENTITIES), 'native_binding_identity')
    if retired_pair:
        need(not pair_supported(value) and identity(value['pairId'])
             and all(type(value[k]) is str and re.fullmatch(r'[a-f0-9]{64}', value[k]) for k in ('engineSha256', 'strategySha256')),
             'retired_pair_fields')
    else:
        need(pair_supported(value), 'unsupported_source_pair')
    need(type(value['nativeAccountId']) is int and value['nativeAccountId'] > 0, 'native_account_id')
    name = value['nativeAccountName']
    need(type(name) is str and 0 < len(name) <= 128 and all(ord(c) >= 32 for c in name), 'native_account_name')
    need(contract_problem(value) is None, 'native_contract')
    need(type(value['tickSize']) in (int, float) and value['tickSize'] == contract_tick(value['symbol']), 'native_tick')
    need(type(value['streamId']) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,48}', value['streamId']) is not None, 'native_stream')
    offset = value['tp1LimitOffsetHandles']
    need(type(offset) in (int, float) and math.isfinite(offset) and 0 <= offset <= 100 and offset / .25 == round(offset / .25), 'native_offset')
    need(value['pairId'] == SOUP or offset == 0, 'paired_exact_target')
    need(len({value[k] for k in ('signalProducerId', 'feedProducerId', 'brokerProducerId')}) == 3, 'separate_producer_authorities')
    need(value['routeSha256'] == route_digest(value), 'native_route_digest')
    return json.loads(canonical(value))


def binding_roots(pair_id):
    """Contract roots a pair may trade: what its bridge adapter declares, else MNQ."""
    adapter = bridge_adapters.REGISTRY.get(pair_id)
    return adapter.roots if adapter is not None else ('MNQ',)


def contract_problem(value):
    """beta4: one dated contract of a known root (bridge_adapters.CONTRACTS) that the pair declares."""
    try:
        spec = bridge_adapters.contract_spec(value['symbol'])
    except bridge_adapters.AdapterError as exc:
        return str(exc)
    roots = binding_roots(value['pairId']) if pair_supported(value) else tuple(bridge_adapters.CONTRACTS)
    if value['root'] != spec['root'] or spec['root'] not in roots:
        return 'contract root not declared for this pair'
    return None


def contract_tick(symbol):
    return bridge_adapters.contract_spec(symbol)['tick_size']


def command_metadata(command, side):
    return {'commandId': command['id'], 'tradeId': command['trade_id'], 'kind': command['kind'],
            'positionSide': side, 'quantity': command.get('quantity'), 'price': command.get('price'),
            'quantityLimit': command.get('quantity_limit'), 'bridgeKind': command.get('bridge_kind'),
            'stopPrice': command.get('stop_price')}


def validate_command(c):
    need(type(c) is dict and set(c) == COMMAND_FIELDS, 'command_metadata_fields')
    need(identity(c['commandId'], True) and identity(c['tradeId'], True), 'command_correlation')
    need(c['kind'] in ('entry', 'bracket_entry', 'tp1_limit', 'reduce', 'flatten', 'cancel', 'modify_stop', 'stop') and c['positionSide'] in ('long', 'short'), 'command_kind_side')
    need(type(c['quantity']) is int and (c['quantity'] >= 0 if c['kind'] in ('cancel','flatten') else c['quantity'] > 0), 'command_quantity')
    need(c['kind'] == 'cancel' or type(c['price']) in (int, float) and math.isfinite(c['price']) and c['price'] > 0, 'command_price')
    need(c['bridgeKind'] in (None, 'bracket_entry', 'modify_stop', 'flatten_all'), 'native_bridge_kind')
    need(c['stopPrice'] is None or type(c['stopPrice']) in (int, float) and math.isfinite(c['stopPrice']) and c['stopPrice'] > 0, 'native_stop_price')
    need(c['quantityLimit'] is None or type(c['quantityLimit']) is int and c['quantityLimit'] in (1, 10), 'paired_quantity_marker')


def expected_payload(binding, c):
    """Contract comparison, not the engine's message builder or order sender."""
    validate_command(c)
    ticker, kind, side, quantity, price = binding['symbol'], c['kind'], c['positionSide'], c['quantity'], c['price']
    action = 'buy' if side == 'long' else 'sell'
    sentiment = 'bullish' if side == 'long' else 'bearish'
    pair = binding['pairId']
    if pair == SOUP and c['bridgeKind']:
        extended = c['bridgeKind']
        # quantityLimit is Soup's policy marker (10 = original allocation), never a size cap.
        need(c['quantityLimit'] == 10 and (0 <= quantity if extended == 'flatten_all' else 0 < quantity), 'native_original_allocation')
        extras = {'soup_command_id': c['commandId'], 'soup_trade_id': c['tradeId'], 'bridge_kind': extended}
        if extended == 'bracket_entry':
            need(kind in ('entry', 'bracket_entry') and c['stopPrice'] is not None, 'native_bracket_metadata')
            extras.update(reference_price=price, stop_price=c['stopPrice'])
            return {'ticker': ticker, 'action': action, 'orderType': 'market', 'quantity': quantity, 'extras': extras}
        if extended == 'modify_stop':
            need(kind == 'modify_stop', 'native_modify_metadata')
            extras.update(stop_price=price, stop_quantity=quantity)
            return {'ticker': ticker, 'action': 'modify_stop', 'extras': extras}
        need(kind == 'flatten', 'native_flatten_metadata')
        return {'ticker': ticker, 'action': 'flatten_all', 'extras': extras}
    need(c['bridgeKind'] is None and c['stopPrice'] is None, 'unexpected_native_metadata')
    if pair == SOUP:
        marker = c['quantityLimit']
        if marker is not None:
            # This marker is a rule already enforced by the unchanged paired
            # message builder, not a new bridge quantity policy.
            # The marker is Soup's policy (1 = fixed one contract, 10 = original allocation), never a size cap.
            need((quantity == 0 if kind == 'cancel' else 0 < quantity and (marker != 1 or quantity == 1))
                 and (marker != 1 or kind != 'reduce'), 'paired_quantity_marker_mismatch')
        out = {'ticker': ticker, 'extras': {'soup_command_id': c['commandId'], 'soup_trade_id': c['tradeId']}}
        if kind == 'entry':
            out.update(action=action, orderType='market', quantity=quantity, quantityType='fixed_quantity', signalPrice=price, sentiment=sentiment)
        elif kind == 'tp1_limit':
            out.update(action='exit', orderType='limit', quantity=quantity, quantityType='fixed_quantity', limitPrice=price, signalPrice=price, cancel=False)
        elif kind == 'cancel':
            out.update(action='cancel')
        else:
            out.update(action='exit', orderType='market', signalPrice=price, cancel=False)
            if kind == 'reduce' or c['quantityLimit'] is not None:
                out.update(quantity=quantity, quantityType='fixed_quantity')
        return out
    # Every other pair is a bridge adapter: it sends adapter commands (bridge_adapters), never this envelope.
    raise ValueError('unsupported_source_pair')


def envelope(binding, command, side, payload):
    b = validate_binding(binding)
    result = {'schema': ENVELOPE, 'bindingSha256': digest(b), 'command': command_metadata(command, side), 'payload': payload}
    validate_envelope(result, b)
    return result


def validate_envelope(value, binding):
    validate_binding(binding)
    need(type(value) is dict and set(value) == {'schema', 'bindingSha256', 'command', 'payload'} and value['schema'] == ENVELOPE, 'customer_envelope_fields')
    need(value['bindingSha256'] == digest(binding), 'customer_envelope_binding')
    need(canonical(value['payload']) == canonical(expected_payload(binding, value['command'])), 'paired_payload_changed')
    need(len(canonical(value).encode()) <= 40960, 'customer_envelope_size')
    c, p = value['command'], value['payload']
    kind = c['kind']
    if c['bridgeKind']:
        extras = p['extras']
        return dict(command_id=c['commandId'], trade_id=c['tradeId'], side=c['positionSide'],
                    kind=c['bridgeKind'], quantity=p.get('quantity'), price=None,
                    reference_price=extras.get('reference_price'), stop_price=extras.get('stop_price'),
                    stop_quantity=extras.get('stop_quantity'), tp1_price=None, tp1_quantity=None,
                    time_in_force='Day', customer_kind=kind, envelope_sha256=digest(value))
    return {'command_id': c['commandId'], 'trade_id': c['tradeId'], 'side': c['positionSide'],
            'kind': 'exit_market' if kind in ('reduce', 'flatten') else kind,
            'quantity': p.get('quantity'), 'price': p.get('limitPrice'),
            'time_in_force': 'GTC' if p.get('timeInForce') == 'gtc' else 'Day',
            'customer_kind': kind, 'envelope_sha256': digest(value)}


def validate_confirmation(value, binding):
    need(type(value) is dict and set(value) == CONFIRMATION_FIELDS and value['schema'] == CONFIRMATION, 'native_confirmation_fields')
    for key in ('instanceId', 'accountId', 'pairId', 'symbol', 'accountMode', 'routeId', 'provider', 'nativeAccountId', 'routeSha256'):
        need(type(value[key]) is type(binding[key]) and value[key] == binding[key], 'native_confirmation_binding')
    need(type(value['connectionEpoch']) is str and 0 < len(value['connectionEpoch']) <= 240, 'native_connection_epoch')
    need(type(value['acquisitionId']) is str and re.fullmatch(r'[a-f0-9]{32}', value['acquisitionId']) is not None, 'native_acquisition_id')
    return value


def validate_complete_confirmation(value, binding, *, at, not_before=0, require_flat=False):
    """Pure evidence validator; never acquires, refreshes or creates a receipt.

    Archive callers use the preserved acquisition time as `at` and separately
    verify their stored digest/basis. This does not claim current broker flatness.
    """
    validate_binding(binding)
    validate_confirmation(value, binding)
    need(type(at) is int and type(not_before) is int and type(value['observedAt']) is int
         and not_before <= value['observedAt'] <= at and at - value['observedAt'] <= 12000, 'native_confirmation_freshness')
    need(type(value['sequence']) is int and 0 < value['sequence'] < 2**63, 'native_confirmation_sequence')
    need(all(value[key] is True for key in ('positionsComplete', 'ordersComplete', 'pendingComplete', 'correlationComplete', 'routeVerified')), 'native_confirmation_incomplete')
    need(type(value['positionQuantity']) is int and value['positionQuantity'] >= 0
         and value['otherContractExposure'] is False, 'native_confirmation_position')
    for key in ('workingOrderIds', 'pendingOrderIds', 'unattributedOrderIds'):
        rows = value[key]
        need(type(rows) is list and len(rows) <= 10000 and all(identity(item, True) for item in rows)
             and len(rows) == len(set(rows)), 'native_confirmation_order_ids')
    need(not value['unattributedOrderIds'], 'native_confirmation_unattributed')
    event = value['engineEvent']
    if event is not None:
        fields = {'event_id', 'sequence', 'observed_at', 'account_id', 'symbol', 'trade_id', 'side', 'position_qty', 'orders', 'open_orders'}
        need(type(event) is dict and set(event) == fields and identity(event['event_id'], True)
             and identity(event['trade_id'], True) and event['side'] in ('long', 'short'), 'native_confirmation_event')
        for key, field in (('sequence', 'sequence'), ('observed_at', 'observedAt'), ('account_id', 'accountId'), ('symbol', 'symbol'), ('position_qty', 'positionQuantity')):
            need(type(event[key]) is type(value[field]) and event[key] == value[field], 'native_confirmation_event_binding')
        need(type(event['open_orders']) is list and all(identity(item, True) for item in event['open_orders'])
             and len(event['open_orders']) == len(set(event['open_orders']))
             and sorted(event['open_orders']) == sorted(value['workingOrderIds']) and not value['pendingOrderIds'], 'native_confirmation_event_inventory')
        need(type(event['orders']) is list and len(event['orders']) <= 10000, 'native_confirmation_event_orders')
        commands, orders = set(), set()
        for row in event['orders']:
            need(type(row) is dict and {'command_id', 'broker_order_id', 'status', 'filled_qty', 'fill_time'} <= set(row)
                 <= {'command_id', 'broker_order_id', 'status', 'filled_qty', 'fill_time', 'role', 'parent_broker_order_id', 'quantity', 'stop_price', 'avg_fill_price'}, 'native_confirmation_order_fields')
            if 'role' in row:
                need(binding['pairId'] == SOUP and row['role'] in ('entry', 'stop', 'tp1', 'flatten'), 'native_order_role')
            for field in ('stop_price', 'avg_fill_price'):
                if field in row:
                    need(type(row[field]) in (int, float) and math.isfinite(row[field]) and row[field] > 0, 'native_order_price')
            if 'quantity' in row:
                need(type(row['quantity']) is int and row['quantity'] > 0, 'native_order_quantity')
            if 'parent_broker_order_id' in row:
                need(identity(row['parent_broker_order_id'], True), 'native_order_parent')
            need(identity(row['command_id'], True) and identity(row['broker_order_id'], True)
                 and row['command_id'] not in commands and row['broker_order_id'] not in orders, 'native_confirmation_order_correlation')
            need(row['status'] in ('working', 'partial', 'filled', 'cancelled', 'rejected')
                 and type(row['filled_qty']) is int and row['filled_qty'] >= 0, 'native_confirmation_order_state')
            need((type(row['fill_time']) is int and 0 < row['fill_time'] <= value['observedAt'])
                 if row['filled_qty'] else row['fill_time'] is None, 'native_confirmation_fill_time')
            commands.add(row['command_id']); orders.add(row['broker_order_id'])
        need(sorted(row['broker_order_id'] for row in event['orders'] if row['status'] in ('working', 'partial')) == sorted(event['open_orders']), 'native_confirmation_open_order_correlation')
    if require_flat:
        need(value['positionQuantity'] == 0 and not value['workingOrderIds'] and not value['pendingOrderIds'], 'native_confirmation_not_flat')
    return value


def safe_native_receipt(receipt, outcome):
    need(type(receipt) is dict and outcome in ('accepted', 'rejected', 'unknown'), 'native_receipt')
    clean = {'messageCode': 'native_' + outcome}
    for key in ('id', 'broker_order_id'):
        value = receipt.get(key)
        if type(value) is str and re.fullmatch(r'[0-9]{1,24}', value):
            clean[key] = value
    if receipt.get('bridge') in ('refused', 'noop', 'disabled'):
        clean['bridge'] = receipt['bridge']
    if receipt.get('mode') in ('cancel_only', 'liquidate'):
        clean['mode'] = receipt['mode']
    reason = receipt.get('reason')
    if receipt.get('bridge') == 'refused' and type(reason) is str and re.fullmatch(r'entry not completely filled \([A-Za-z]{1,24}, [0-9]{1,3}/[0-9]{1,3}\)', reason):
        clean['reason'] = reason
    legs = receipt.get('legs')
    if type(legs) is dict:
        clean['legs'] = {k:v for k,v in legs.items() if k in ('stop','tp1') and type(v) is str and re.fullmatch(r'[0-9]{1,24}',v)}
    if receipt.get('duplicate') is True:
        clean['duplicate'] = True
    return clean

# decrypt-bridge-v2 strategies (beta4): the pair (strategy module hash, strategy hash) of each line in
# bridge_v2_registry.STRATEGIES, the same hashes source.py pins.
from . import bridge_v2_registry as _bridge_v2  # noqa: E402
for _adapter_id, (_module_file, _engine_pin, _strategy_pin) in _bridge_v2.STRATEGIES.items():
    PAIRS[_adapter_id] = (_engine_pin, _strategy_pin)
