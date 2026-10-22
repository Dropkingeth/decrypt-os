"""Offline stand-in for Tradovate's trading WebSocket, following the public protocol.

Frames: server 'o' open, 'h' heartbeat, 'a[...]' messages; client requests are
'endpoint\\nid\\nquery\\nbody'. Market orders fill immediately at `price`; limit
orders fill when set_price() crosses them.

Brackets (documentation-based, like the bridge; only placeorder/modifyorder(Limit)/
cancelorder/liquidateposition shapes are checked against Tradovate's web trader):
- order/placeoso: parent + bracket1/bracket2 legs. Legs have no orderQty (they inherit the
  parent's), start `Suspended`, turn `Working` once the parent fills (quantity cut to the
  parent's fill if it only partly filled) and are OCO: a leg that completely fills cancels
  its sibling. Stops fill at the current price when set_price() crosses them.
- order/modifyorder: new orderVersion (new id) with the new quantity/price/stopPrice; reply
  {commandId}. A stop modified through the market fills at once.
- order/liquidateposition: reply {orderId}, then every working/suspended order on the
  account+contract is cancelled and a Market exit tagged "Exit" closes the position.

Tradovate shapes modelled on request (1.0.9.1; defaults keep the behaviour above):
- id_blocks {accountId: first id}: ids of everything done for an account (orders, versions, fills,
  positions, command ids) come from that account's own counter, as Tradovate allocates them
  per account (so the id blocks of different accounts interleave in the same minutes).
- liquidate_reply 'no_order_id_when_working': liquidateposition with orders working on the contract
  replies without an orderId (s 200, no orderId); 'no_order_id': never one.
"""
import json
import threading
import time
from datetime import datetime, timezone

from tvbridge import ws

TOKEN = 'fake-session-token-0123456789'


def iso(ms=None):
    ms = ms if ms is not None else time.time_ns() // 1_000_000
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


class FakeTradovate:
    def __init__(self, include_contract_in_sync=True):
        self.lock = threading.RLock()
        self.price = 10000.0
        self.accounts = [{'id': 101, 'name': 'DEMO-1', 'userId': 7, 'active': True},
                         {'id': 102, 'name': 'OTHER', 'userId': 7, 'active': True}]
        self.contracts = {555: {'id': 555, 'name': 'MNQZ1'}}
        self.include_contract_in_sync = include_contract_in_sync
        self.orders, self.fills, self.versions, self.positions = {}, {}, {}, {}
        self.next_id = 1000
        self.clients = []
        self.drop_next = set()
        self.next_place_status = None   # e.g. 500: order is placed but the reply is an error
        self.next_market_partial = None  # fill only this many, then cancel the rest
        self.swallow_next = False        # next placeorder: no reply and no order (request lost)
        self.reject_next = False         # next placeorder: refused like Tradovate's data-feed rejection
        self.risk_reject_next = False    # next placeorder: orderId returned, then RiskRejected
        self.requests = []
        self.authorize_count = 0
        self.token = TOKEN
        self.silent = False
        self.delay_next = {}
        self.sync_props_once = None
        self.malformed_once = None
        self.skew_ms = 0                 # exchange clock offset for entity timestamps (replay tests)
        self.cancel_race = None          # next cancel of a working limit: 'fill_first' or 'fill_after_reply'
        self.position_history = []       # (accountId, contractId, netPos) after every fill
        self.swallow_endpoints = set()   # next request to each endpoint: no reply and no effect (lost)
        self.oso_legs = {}               # parent order id -> [leg order ids]
        self.oco = {}                    # leg order id -> sibling leg ids (OCO)
        self.bodies = []                 # (endpoint, body) of every order request, for wire-shape tests
        self.body_times = []             # time.monotonic() of each entry in bodies (latency tests)
        self.fill_times = {}             # order id -> time.monotonic() of its latest fill
        self.account_data = {}           # extra sync collections, e.g. {'cashBalances': [...]} (1.0.6 view)
        self.liquidate_cancels_orders = True  # False: liquidateposition closes the position only (unverified against Tradovate)
        self.reject_modify_next = False  # next modifyorder: refused like a definite rejection
        self.reject_modify_when = None   # callable(body) -> True: that modifyorder is refused
        self.reject_cancel_next = False  # next cancelorder: refused, the order keeps working
        self.reject_liquidate_next = False  # next liquidateposition: refused (failureReason), nothing changes
        self.oco_cancel_links = False    # True: cancelling one OCO bracket leg also cancels its sibling
        self.id_blocks = None            # {accountId: first id}: per-account id counters (Tradovate's shape)
        self._acct = None                # account the current request/fill acts for (id_blocks only)
        self.liquidate_reply = 'order_id'  # or 'no_order_id_when_working' / 'no_order_id' (Tradovate's shapes)
        self.server = ws.Server(self._handle)
        self.port = self.server.port
        self.running = True
        threading.Thread(target=self._heartbeat, daemon=True).start()

    # --- plumbing -------------------------------------------------------------

    def _heartbeat(self):
        while self.running:
            time.sleep(1)
            for c in list(self.clients):
                if self.silent:
                    continue
                try:
                    c.send_text('h')
                except Exception:
                    self._drop(c)

    def _drop(self, conn):
        with self.lock:
            if conn in self.clients:
                self.clients.remove(conn)

    def _id(self):
        if self.id_blocks and self._acct in self.id_blocks:
            self.id_blocks[self._acct] += 1
            return self.id_blocks[self._acct]
        self.next_id += 1
        return self.next_id

    def now_ms(self):
        return time.time_ns() // 1_000_000 + self.skew_ms

    def _iso(self):
        return iso(self.now_ms())

    def _reply(self, conn, rid, d, s=200):
        conn.send_text('a' + json.dumps([{'s': s, 'i': rid, 'd': d}]))

    def _props(self, entity_type, event_type, entity):
        frame = 'a' + json.dumps([{'e': 'props', 'd': {'entityType': entity_type, 'eventType': event_type,
                                                         'entity': entity}}])
        for c in list(self.clients):
            try:
                c.send_text(frame)
            except Exception:
                self._drop(c)

    def disconnect_all(self):
        for c in list(self.clients):
            self._drop(c)
            c.close()

    def stop(self):
        self.running = False
        self.disconnect_all()
        self.server.stop()

    def _handle(self, conn, path):
        conn.send_text('o')
        authorized = False
        try:
            while True:
                frame = conn.recv_text()
                if frame == '[]':
                    continue
                parts = frame.split('\n', 3)
                if len(parts) < 3:
                    continue
                endpoint, rid, query = parts[0], int(parts[1]), parts[2]
                if len(parts) == 4:
                    body = parts[3]
                    if body == '' and endpoint != 'authorize':
                        # Real Tradovate: an empty body line is parsed as JSON and refused.
                        self._reply(conn, rid, 'Invalid JSON: unexpected end of input, offset: 0x00000000', 400)
                        continue
                else:
                    body = ''
                if endpoint == 'authorize':
                    self.authorize_count += 1
                    authorized = body == self.token
                    if not self.silent:
                        self._reply(conn, rid, {} if authorized else 'Access is denied', 200 if authorized else 401)
                    if authorized:
                        with self.lock:
                            self.clients.append(conn)
                    continue
                if not authorized:
                    self._reply(conn, rid, 'Access is denied', 401)
                    continue
                self.requests.append(endpoint)
                if endpoint.startswith('order/') and body:
                    self.bodies.append((endpoint, json.loads(body)))
                    self.body_times.append(time.monotonic())
                if self.silent:
                    continue
                if endpoint in self.swallow_endpoints:
                    self.swallow_endpoints.discard(endpoint)
                    continue
                if endpoint in self.delay_next:
                    delay = self.delay_next.pop(endpoint)
                    time.sleep(delay)
                if endpoint in self.drop_next:
                    self.drop_next.discard(endpoint)
                    self._route(conn, None, endpoint, query, json.loads(body) if body else None)
                    continue
                self._route(conn, rid, endpoint, query, json.loads(body) if body else None)
        except ws.Closed:
            pass
        finally:
            self._drop(conn)

    # --- endpoints -------------------------------------------------------------------

    def _route(self, conn, rid, endpoint, query, body):
        reply = (lambda d, s=200: self._reply(conn, rid, d, s)) if rid is not None else (lambda d, s=200: None)
        with self.lock:
            if self.id_blocks and isinstance(body, dict):
                owner = body.get('accountId')
                if owner is None and body.get('orderId') in self.orders:
                    owner = self.orders[body['orderId']]['accountId']
                self._acct = owner
            if endpoint == 'account/list':
                reply(self.accounts)
            elif endpoint == 'user/syncrequest':
                snapshot = {'users': [{'id': 7}], 'accounts': self.accounts,
                       'orders': list(self.orders.values()), 'fills': list(self.fills.values()),
                       'positions': list(self.positions.values()), 'orderVersions': list(self.versions.values()),
                       'contracts': list(self.contracts.values()) if self.include_contract_in_sync else []}
                snapshot.update(self.account_data)
                if self.sync_props_once:
                    props, self.sync_props_once = self.sync_props_once, None
                    conn.send_text('a' + json.dumps([{'s': 200, 'i': rid, 'd': snapshot}, {'e': 'props', 'd': props}]))
                elif self.malformed_once is not None:
                    bad, self.malformed_once = self.malformed_once, None
                    reply(bad)
                else:
                    reply(snapshot)
            elif endpoint == 'contract/find':
                name = query.split('=', 1)[1] if '=' in query else ''
                match = [c for c in self.contracts.values() if c['name'] == name]
                reply(match[0] if match else 'Not found', 200 if match else 404)
            elif endpoint == 'order/placeorder':
                self._place(body, reply)
            elif endpoint == 'order/placeoso':
                self._place_oso(body, reply)
            elif endpoint == 'order/modifyorder':
                self._modify(body, reply)
            elif endpoint == 'order/liquidateposition':
                self._liquidate(body, reply)
            elif endpoint == 'order/cancelorder':
                order = self.orders.get(body.get('orderId'))
                if order and order['ordStatus'] == 'Working' and self.reject_cancel_next:
                    self.reject_cancel_next = False
                    reply({'failureReason': 'UnknownReason', 'failureText': 'Cancel rejected'})
                    return
                if order and order['ordStatus'] == 'Working' and self.cancel_race:
                    # The order (a limit, or a stop at the market) fills before the cancel is processed.
                    race, self.cancel_race = self.cancel_race, None
                    price = self.versions[order['id']]['price']
                    price = self.price if price is None else price
                    if race == 'fill_after_reply':
                        # Refusal reaches the client before the fill events do.
                        queued = []
                        self._fill(order['id'], price, props=lambda *frame: queued.append(frame))
                        reply({'failureReason': 'UnknownReason', 'failureText': 'Order is not working'})
                        threading.Timer(0.4, self._emit, args=(queued,)).start()
                        return
                    self._fill(order['id'], price)
                    order = self.orders[order['id']]
                if not order or order['ordStatus'] not in ('Working', 'Suspended'):
                    reply({'failureReason': 'UnknownReason', 'failureText': 'Order is not working'})
                    return
                reply({'commandId': self._id()})  # Tradovate: cancel/modify reply with a commandId
                self._cancel(order['id'])
                if self.oco_cancel_links:  # OCO: the linked bracket leg goes with it
                    for sibling in self.oco.get(order['id'], []):
                        if self.orders[sibling]['ordStatus'] in ('Working', 'Suspended'):
                            self._cancel(sibling)
            else:
                reply('Unknown endpoint', 404)

    def _place(self, body, reply):
        if self.swallow_next:
            self.swallow_next = False
            return
        account = next((a for a in self.accounts if a['id'] == body.get('accountId')), None)
        contract = next((c for c in self.contracts.values() if c['name'] == body.get('symbol')), None)
        if self.reject_next:
            # Tradovate: a definite refusal, cause only in failureText, no order created.
            self.reject_next = False
            reply({'failureReason': 'UnknownReason', 'failureText': 'Your account is not subscribed to the data feed'})
            return
        if not account or not contract or body.get('orderQty', 0) <= 0:
            reply({'failureReason': 'UnknownReason', 'failureText': 'bad account/symbol/qty'})
            return
        oid = self._id()
        order = {'id': oid, 'accountId': account['id'], 'contractId': contract['id'], 'action': body['action'],
                 'ordStatus': 'PendingNew', 'timestamp': self._iso(), 'isAutomated': body.get('isAutomated')}
        self.orders[oid] = order
        self.versions[oid] = {'id': oid, 'orderId': oid, 'orderQty': body['orderQty'], 'orderType': body['orderType'],
                              'price': body.get('price'), 'text': body.get('text')}
        if self.next_place_status:
            status, self.next_place_status = self.next_place_status, None
            reply('Internal error', status)
        else:
            reply({'orderId': oid})
        self._props('order', 'Created', order)
        self._props('orderVersion', 'Created', self.versions[oid])
        if self.risk_reject_next:
            self.risk_reject_next = False
            order = dict(order, ordStatus='Rejected')
            self.orders[oid] = order
            self._props('order', 'Updated', order)
            return
        order = dict(order, ordStatus='Working')
        self.orders[oid] = order
        self._props('order', 'Updated', order)
        if body['orderType'] == 'Market':
            partial, self.next_market_partial = self.next_market_partial, None
            self._fill(oid, self.price, qty=partial)

    def _cancel(self, oid, props=None):
        order = dict(self.orders[oid], ordStatus='Canceled', timestamp=self._iso())
        self.orders[oid] = order
        (props or self._props)('order', 'Updated', order)

    def _place_oso(self, body, reply):
        """Parent order plus linked legs (Tradovate PlaceOSO: legs carry no orderQty)."""
        account = next((a for a in self.accounts if a['id'] == body.get('accountId')), None)
        contract = next((c for c in self.contracts.values() if c['name'] == body.get('symbol')), None)
        if self.reject_next:
            self.reject_next = False
            reply({'failureReason': 'UnknownReason', 'failureText': 'Your account is not subscribed to the data feed'})
            return
        legs_in = [body[k] for k in ('bracket1', 'bracket2') if isinstance(body.get(k), dict)]
        if not account or not contract or body.get('orderQty', 0) <= 0 or not legs_in or \
                any('orderQty' in leg or leg.get('orderType') not in ('Stop', 'Limit') for leg in legs_in):
            reply({'failureReason': 'UnknownReason', 'failureText': 'bad oso request'})
            return
        oid = self._id()
        leg_ids = [self._id() for _ in legs_in]
        order = {'id': oid, 'accountId': account['id'], 'contractId': contract['id'], 'action': body['action'],
                 'ordStatus': 'PendingNew', 'timestamp': self._iso(), 'isAutomated': body.get('isAutomated')}
        self.orders[oid] = order
        self.versions[oid] = {'id': oid, 'orderId': oid, 'orderQty': body['orderQty'], 'orderType': body['orderType'],
                              'price': body.get('price'), 'text': body.get('text')}
        legs = []
        for lid, leg in zip(leg_ids, legs_in):
            legs.append({'id': lid, 'accountId': account['id'], 'contractId': contract['id'], 'action': leg['action'],
                         'ordStatus': 'Suspended', 'timestamp': self._iso(), 'isAutomated': body.get('isAutomated')})
            self.orders[lid] = legs[-1]
            self.versions[lid] = {'id': lid, 'orderId': lid, 'orderQty': body['orderQty'],
                                  'orderType': leg['orderType'], 'price': leg.get('price'),
                                  'stopPrice': leg.get('stopPrice'), 'text': leg.get('text')}
        self.oso_legs[oid] = leg_ids
        for lid in leg_ids:
            self.oco[lid] = [x for x in leg_ids if x != lid]
        if self.next_place_status:
            status, self.next_place_status = self.next_place_status, None
            reply('Internal error', status)
        else:
            result = {'orderId': oid, 'oso1Id': leg_ids[0]}
            if len(leg_ids) > 1:
                result['oso2Id'] = leg_ids[1]
            reply(result)
        self._props('order', 'Created', order)
        self._props('orderVersion', 'Created', self.versions[oid])
        for leg in legs:
            self._props('order', 'Created', leg)
            self._props('orderVersion', 'Created', self.versions[leg['id']])
        if self.risk_reject_next:
            self.risk_reject_next = False
            self.orders[oid] = dict(order, ordStatus='Rejected')
            self._props('order', 'Updated', self.orders[oid])
            self._activate_legs(oid)
            return
        self.orders[oid] = dict(order, ordStatus='Working')
        self._props('order', 'Updated', self.orders[oid])
        if body['orderType'] == 'Market':
            partial, self.next_market_partial = self.next_market_partial, None
            self._fill(oid, self.price, qty=partial)

    def _activate_legs(self, parent):
        """Parent terminal: legs work for what filled; with nothing filled they are cancelled."""
        filled = self.filled_qty(parent)
        for lid in self.oso_legs.get(parent, []):
            if self.orders[lid]['ordStatus'] != 'Suspended':
                continue
            if not filled:
                self._cancel(lid)
                continue
            if filled < self.versions[lid]['orderQty']:
                self.versions[lid] = dict(self.versions[lid], id=self._id(), orderQty=filled)
                self._props('orderVersion', 'Created', self.versions[lid])
            self.orders[lid] = dict(self.orders[lid], ordStatus='Working', timestamp=self._iso())
            self._props('order', 'Updated', self.orders[lid])
        self._trigger_stops()

    def _modify(self, body, reply):
        oid = body.get('orderId')
        order = self.orders.get(oid)
        if self.reject_modify_next or (self.reject_modify_when and self.reject_modify_when(body)):
            self.reject_modify_next = False
            reply({'failureReason': 'UnknownReason', 'failureText': 'Modification rejected'})
            return
        if not order or order['ordStatus'] not in ('Working', 'Suspended'):
            reply({'failureReason': 'UnknownReason', 'failureText': 'Order is not working'})
            return
        qty = body.get('orderQty')
        current = self.versions[oid]
        if type(qty) is not int or qty <= self.filled_qty(oid) or body.get('orderType') != current['orderType']:
            reply({'failureReason': 'UnknownReason', 'failureText': 'invalid modification'})
            return
        version = dict(current, id=self._id(), orderQty=qty)
        for key in ('price', 'stopPrice', 'text'):
            if key in body:
                version[key] = body[key]
        self.versions[oid] = version
        reply({'commandId': self._id()})  # Tradovate: modifyorder -> {commandId}
        self._props('orderVersion', 'Created', version)
        self._trigger_stops()

    def _liquidate(self, body, reply):
        """Exit at Mkt & Cxl: cancel every working order on the contract, then close the position."""
        if self.reject_liquidate_next:
            self.reject_liquidate_next = False
            reply({'failureReason': 'UnknownReason', 'failureText': 'Liquidation rejected'})
            return
        key = (body.get('accountId'), body.get('contractId'))
        if body.get('admin') is not False or key[0] not in {a['id'] for a in self.accounts}:
            reply({'failureReason': 'UnknownReason', 'failureText': 'bad liquidation request'})
            return
        net = (self.positions.get(key) or {}).get('netPos', 0)
        working = sorted(oid for oid, o in self.orders.items() if (o['accountId'], o['contractId']) == key
                         and o['ordStatus'] in ('PendingNew', 'Working', 'Suspended'))
        exit_id = self._id() if net else None
        if net and (self.liquidate_reply == 'no_order_id' or
                    (self.liquidate_reply == 'no_order_id_when_working' and working)):
            reply({})
        elif net:
            reply({'orderId': exit_id})
        else:
            reply({'failureReason': 'UnknownReason', 'failureText': 'No position to liquidate'})
        for oid in working if self.liquidate_cancels_orders else ():
            self._cancel(oid)
        if not net:
            return
        order = {'id': exit_id, 'accountId': key[0], 'contractId': key[1], 'action': 'Sell' if net > 0 else 'Buy',
                 'ordStatus': 'PendingNew', 'timestamp': self._iso(), 'isAutomated': False}
        self.orders[exit_id] = order
        self.versions[exit_id] = {'id': exit_id, 'orderId': exit_id, 'orderQty': abs(net), 'orderType': 'Market',
                                  'price': None, 'text': 'Exit'}
        self._props('order', 'Created', order)
        self._props('orderVersion', 'Created', self.versions[exit_id])
        self.orders[exit_id] = dict(order, ordStatus='Working')
        self._props('order', 'Updated', self.orders[exit_id])
        self._fill(exit_id, self.price)

    def _trigger_stops(self):
        for oid in sorted(self.orders):
            order, v = self.orders[oid], self.versions.get(oid) or {}
            if order['ordStatus'] != 'Working' or v.get('orderType') != 'Stop':
                continue
            if (order['action'] == 'Sell' and self.price <= v['stopPrice']) or \
                    (order['action'] == 'Buy' and self.price >= v['stopPrice']):
                self._fill(oid, self.price)

    def _emit(self, frames):
        with self.lock:
            for frame in frames:
                self._props(*frame)

    def filled_qty(self, oid):
        with self.lock:
            return sum(f['qty'] for f in self.fills.values() if f['orderId'] == oid)

    def _fill(self, oid, price, qty=None, leave_working=False, props=None):
        """Fill `qty` (default: everything still open). A short market fill cancels the rest;
        a short fill with leave_working=True keeps the order working (partial limit fill)."""
        props = props or self._props
        order, version = self.orders[oid], self.versions[oid]
        if self.id_blocks:
            self._acct = order['accountId']
        done = self.filled_qty(oid)
        qty = qty or version['orderQty'] - done
        if qty <= 0 or done + qty > version['orderQty']:
            raise AssertionError('fake exchange asked to overfill an order')
        fid = self._id()
        fill = {'id': fid, 'orderId': oid, 'contractId': order['contractId'], 'timestamp': self._iso(),
                'action': order['action'], 'qty': qty, 'price': price, 'active': True}
        self.fills[fid] = fill
        self.fill_times[oid] = time.monotonic()
        props('fill', 'Created', fill)
        if done + qty == version['orderQty']:
            final = 'Filled'
        else:
            final = order['ordStatus'] if leave_working else 'Canceled'
        order = dict(order, ordStatus=final, timestamp=self._iso())
        self.orders[oid] = order
        props('order', 'Updated', order)
        key = (order['accountId'], order['contractId'])
        pos = self.positions.get(key) or {'id': self._id(), 'accountId': key[0], 'contractId': key[1], 'netPos': 0,
                                          'tradeDate': {'year': 2021, 'month': 1, 'day': 1}}
        delta = fill['qty'] if order['action'] == 'Buy' else -fill['qty']
        pos = dict(pos, netPos=pos['netPos'] + delta, timestamp=self._iso())
        self.positions[key] = pos
        self.position_history.append((key[0], key[1], pos['netPos']))
        props('position', 'Updated', pos)
        if final == 'Filled':
            for sibling in self.oco.get(oid, []):  # OCO: the other bracket leg is cancelled
                if self.orders[sibling]['ordStatus'] in ('Working', 'Suspended'):
                    self._cancel(sibling, props)
        if oid in self.oso_legs and final != 'Working':
            self._activate_legs(oid)

    def fill_limit(self, oid, qty):
        """Partial fill of a working limit at its price; the rest keeps working."""
        with self.lock:
            if self.orders[oid]['ordStatus'] != 'Working' or self.versions[oid]['orderType'] != 'Limit':
                raise AssertionError('fake exchange: not a working limit')
            self._fill(oid, self.versions[oid]['price'], qty=qty, leave_working=True)

    def set_price(self, price):
        with self.lock:
            self.price = price
            for oid in sorted(self.orders):
                order, v = self.orders[oid], self.versions[oid]  # re-read: an OCO fill cancels siblings
                if order['ordStatus'] != 'Working' or v['orderType'] != 'Limit':
                    continue
                if (order['action'] == 'Sell' and price >= v['price']) or (order['action'] == 'Buy' and price <= v['price']):
                    self._fill(oid, v['price'])
            self._trigger_stops()

    def working_orders(self, contract_id=None):
        with self.lock:
            return sorted(oid for oid, o in self.orders.items()
                          if o['ordStatus'] in ('PendingNew', 'Working', 'Suspended')
                          and (contract_id is None or o['contractId'] == contract_id))

    def order_count(self):
        with self.lock:
            return len(self.orders)


PAGE = """<!doctype html><html><head><title>Fake Tradovate Trader</title></head><body>
<h1>fake trader</h1><script>
(function start() {
  const ws = new WebSocket('ws://127.0.0.1:%d/v1/websocket');
  ws.onmessage = (e) => { if (e.data === 'o') ws.send('authorize\\n0\\n\\n%s'); };
  setInterval(() => { if (ws.readyState === 1) ws.send('[]'); }, 2500);
  window.appSocket = ws;
})();
</script></body></html>"""


def page_html(port):
    return PAGE % (port, TOKEN)
