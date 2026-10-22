"""Native order-message mapping; no attached brackets.

Capture is the runtime default; the bridge carries the messages. A capture records receipt, never a fill.
"""
import os
from pathlib import Path
from storage import canonical, strict_json

def quantity_ok(quantity, quantity_limit):
    """Decrypt OS: `quantity_limit` is the policy marker (1 = fixed one contract,
    10 = the alert's original allocation), never a size cap. A positive whole number; exactly 1 under 1."""
    return type(quantity) is int and quantity > 0 and (quantity_limit != 1 or quantity == 1)


def validate_quantity_message(payload, quantity_limit):
    """A second check at the HTTPS boundary. Never round or clamp malformed orders."""
    if quantity_limit is None:
        return
    if type(quantity_limit) is not int or quantity_limit not in (1,10):
        raise ValueError('supported account transport limit required')
    if isinstance(payload.get('extras'), dict) and 'bridge_kind' in payload['extras']:
        return validate_native_message(payload, quantity_limit)
    action = payload.get('action')
    if action == 'cancel':
        if set(payload) != {'ticker', 'extras', 'action'}:
            raise ValueError('fixed-one cancel fields')
        return
    if (action not in ('buy', 'sell', 'exit')
            or not quantity_ok(payload.get('quantity'), quantity_limit)
            or payload.get('quantityType') != 'fixed_quantity'
            or payload.get('orderType') not in ('market', 'limit')
            or (action != 'exit' and payload['orderType'] != 'market')
            or (action == 'exit' and payload.get('cancel') is not False)):
        raise ValueError('fixed-one transport quantity or action rejected')


NATIVE_EXTRAS = {'bracket_entry': {'reference_price', 'stop_price'},
                 'modify_stop': {'stop_price', 'stop_quantity'}, 'flatten_all': set()}


def validate_native_message(payload, quantity_limit):
    """Bridge extended kinds (tvbridge bracket_entry/modify_stop/flatten_all), original allocation only."""
    extras = payload['extras']; kind = extras.get('bridge_kind')
    if quantity_limit != 10 or kind not in NATIVE_EXTRAS:
        raise ValueError('native order kind rejected')
    if set(extras) != {'soup_command_id', 'soup_trade_id', 'bridge_kind'} | NATIVE_EXTRAS[kind]:
        raise ValueError('native order extras')
    finite = lambda v: type(v) in (int, float) and v == v and v not in (float('inf'), float('-inf')) and v > 0  # noqa: E731
    if kind == 'bracket_entry':
        if (set(payload) != {'ticker', 'extras', 'action', 'orderType', 'quantity'}
                or payload['action'] not in ('buy', 'sell') or payload['orderType'] != 'market'
                or not quantity_ok(payload['quantity'], quantity_limit)
                or not finite(extras['reference_price']) or not finite(extras['stop_price'])):
            raise ValueError('native bracket entry rejected')
    elif kind == 'modify_stop':
        if (set(payload) != {'ticker', 'extras', 'action'} or payload['action'] != 'modify_stop'
                or not quantity_ok(extras['stop_quantity'], quantity_limit)
                or not finite(extras['stop_price'])):
            raise ValueError('native stop modify rejected')
    elif set(payload) != {'ticker', 'extras', 'action'} or payload['action'] != 'flatten_all':
        raise ValueError('native flatten rejected')


def native_message(command, ticker, side, quantity_limit):
    """Exact tvbridge extended schemas (the bridge refuses any other field)."""
    kind = command['kind']
    extras = {'soup_command_id': command['id'], 'soup_trade_id': command['trade_id'],
              'bridge_kind': command['bridge_kind']}
    if kind == 'entry' and command['bridge_kind'] == 'bracket_entry':
        extras.update(reference_price=command['price'], stop_price=command['stop_price'])
        out = {'ticker': ticker, 'action': 'buy' if side == 'long' else 'sell', 'orderType': 'market',
               'quantity': command['quantity'], 'extras': extras}
    elif kind == 'modify_stop' and command['bridge_kind'] == 'modify_stop':
        extras.update(stop_price=command['price'], stop_quantity=command['quantity'])
        out = {'ticker': ticker, 'action': 'modify_stop', 'extras': extras}
    elif kind == 'flatten' and command['bridge_kind'] == 'flatten_all':
        out = {'ticker': ticker, 'action': 'flatten_all', 'extras': extras}
    else:
        raise ValueError('unsupported native command')
    if quantity_limit is None or command.get('quantity_limit') != quantity_limit or side not in ('long', 'short'):
        raise ValueError('native command rejected before transport')
    validate_quantity_message(out, quantity_limit)
    return out


def message(command, ticker, side, *, quantity_limit=None):
    kind=command['kind']
    if command.get('bridge_kind'):
        return native_message(command, ticker, side, quantity_limit if quantity_limit is not None else command.get('quantity_limit'))
    marker = command.get('quantity_limit')
    if quantity_limit is None:
        quantity_limit = marker
    if quantity_limit is not None:
        if (type(quantity_limit) is not int or quantity_limit not in (1,10)
                or type(marker) is not int or marker != quantity_limit
                or kind not in (('entry','tp1_limit','cancel','flatten') if quantity_limit==1 else ('entry','tp1_limit','cancel','reduce','flatten'))
                or type(command.get('quantity')) is not int
                or not (command['quantity']==0 if kind=='cancel' else quantity_ok(command['quantity'],quantity_limit))
                or side not in ('long','short')):
            raise ValueError('fixed-one command rejected before transport')
    out={'ticker':ticker,'extras':{'soup_command_id':command['id'],'soup_trade_id':command['trade_id']}}
    if kind=='entry':
        out.update(action='buy' if side=='long' else 'sell',orderType='market',quantity=command['quantity'],
                   quantityType='fixed_quantity',signalPrice=command['price'],sentiment='bullish' if side=='long' else 'bearish')
    elif kind=='tp1_limit':
        out.update(action='exit',orderType='limit',quantity=command['quantity'],quantityType='fixed_quantity',
                   limitPrice=command['price'],signalPrice=command['price'],cancel=False)
    elif kind=='cancel':out.update(action='cancel')
    elif kind in ('reduce','flatten'):
        out.update(action='exit',orderType='market',signalPrice=command['price'],cancel=False)
        if kind=='reduce' or quantity_limit is not None:
            out.update(quantity=command['quantity'],quantityType='fixed_quantity')
    else:raise ValueError('unsupported command')
    validate_quantity_message(out,quantity_limit)
    return out

class CaptureTransport:
    def __init__(self,path):self.path=Path(path)
    def send(self,payload):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open('a') as f:
            f.write(canonical(payload)+'\n');f.flush()
            os.fsync(f.fileno())
        return 'accepted',{'capture_only':True}
