"""decrypt-bridge-v2 receiver and executor (beta4): one engine for every non-Soup strategy.

The strategy owns every trading rule. Its alert (bridge_v2_contract.py, PAYLOAD-V2.md) carries the whole
plan; this engine does exactly what it says through the Tradovate bridge's adapter order kinds, and adds
safety checks only: strict format and identity, entry freshness (a touch entry uses its valid_until),
size/position conservation, fresh broker evidence before each management order, one trade at a time,
and fail closed on anything unknown. The bridge adds anti-hedge, the kill switch and broker confirmation.
There are no size caps, filters, time windows or trade rules of its own: a feature the alert turns off
does nothing here.

What one entry alert drives (each part only when the alert carries it):
- entry: `bracket_entry` (market, with the resting stop attached) when the plan has a resting stop
  (stop.type resting, or a bar-close stop with a safety_price); a plain market `entry` otherwise. A touch
  entry is armed and sent the same way when the last price reaches touch.price before touch.valid_until.
- stop: a resting broker stop, kept sized to the open position (`modify_stop`); and/or a bar-close stop
  watched on the binding's 5-second native bars, closed at the stop timeframe: a close at or beyond the
  level exits everything (`flatten_all`).
- targets: `limit` targets rest at the broker (the first one staged with the bracket, the others as
  `tp1_limit`); `market` targets exit their quantity at market when the last price reaches them. The
  stop is shrunk first so it can never over-fill. A market target waits until every nearer resting
  limit has filled (the bridge never sends a market exit while a take-profit limit rests).
- break_even: when its triggers are met (price touch and/or a target fill, any or all), at once or at
  the close of the confirm-timeframe bar, the stop moves to stop_price (or the average fill +/- offset
  ticks). Resting: `modify_stop`; bar-close: the watched level. A stop is never loosened by break-even.
  If the last price is already at or beyond the level: flatten or skip, as the alert says.
- runner: hold; target (market exit of everything at runner.price); or trail (after the best price is
  activation ticks beyond the fill, exit when the price comes back distance ticks from the best).
- eod: flatten at the next eod.time in eod.tz (an armed touch is disarmed, an unsent entry dropped).
- daily_loss: the realized net P&L (fills, point value, fee per contract round turn) of the trading day
  (tz, day_start); an entry is refused while the day is at or below -limit_usd; with `flatten`, an open
  trade is flattened when the day reaches the limit (with include_open, counting the open position).
- management events: modify_stop, exit (a quantity), flatten, bias_flip (only when the entry's
  bias_flip.enabled), cancel_entry (disarms an armed touch). They match the open trade by trade_id.

Unknown broker outcomes, a missing protective stop, fills that do not add up, or a management order the
bridge refuses three times put the trade in `recovery_required`: nothing more is sent and entries are
held until a person checks Tradovate and calls release_recovery().

A strategy module (bridge_v2_<name>.py) declares its Strategy and exports module_api(STRATEGY).
"""
import copy
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
import sqlite3
from zoneinfo import ZoneInfo

from . import bridge_adapters
from . import bridge_v2_contract as contract
from .bridge_v2_contract import PROTOCOL, Strategy, PayloadError  # noqa: F401  (re-exported for strategy modules)

REVISION = 'decrypt-bridge-v2-engine-1'
BROKER_MAX_AGE_MS = 12_000        # management orders need broker evidence at most this old
QUOTE_MAX_AGE_MS = 2_000          # a price decision needs a last price at most this old
EVIDENCE_TIMEOUT_MS = 30_000      # an accepted order the broker evidence never settles: a person checks
RETRY_MS = (2_000, 4_000, 8_000)  # a management order the bridge definitely refused: at most 3 tries
TOUCH_DISPATCH_GRACE_MS = 5_000   # a touched entry not sent by then is dropped, never queued
BAR_MS = 5_000                    # bar-close levels are read from the binding's 5-second native bars
OPEN = ('entering', 'entered', 'running', 'closing')
EVIDENCE_FIELDS = {'event_id', 'sequence', 'observed_at', 'account_id', 'symbol', 'trade_id', 'side',
                   'position_qty', 'orders', 'open_orders'}
ORDER_STATUSES = ('working', 'partial', 'filled', 'cancelled', 'rejected')
_ALERT_ID = re.compile(r'[A-Za-z0-9_.:@+-]{1,160}\Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _price(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def round_tick(value, tick):
    """Nearest tick, halves away from zero (prices are positive)."""
    return round(math.floor(value / tick + 0.5) * tick, 10)


# --- time helpers ------------------------------------------------------------------------------------

def next_clock(at_ms, hhmm, tz):
    """The first moment at or after `at_ms` whose local time in `tz` is `hhmm`, Unix ms."""
    hour, minute = contract.clock(hhmm)
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(at_ms / 1000, zone)
    day = local.date()
    for _ in range(3):
        at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        if int(at.timestamp() * 1000) >= at_ms:
            return int(at.timestamp() * 1000)
        day += timedelta(days=1)
    raise ValueError('no local time found')  # pragma: no cover


def day_key(at_ms, tz, day_start):
    """The trading day of `at_ms`: the local date of the latest `day_start` boundary at or before it."""
    hour, minute = contract.clock(day_start)
    local = datetime.fromtimestamp(at_ms / 1000, ZoneInfo(tz)).replace(tzinfo=None)
    return (local - timedelta(hours=hour, minutes=minute)).date().isoformat()


def bar_end(at_ms, seconds):
    """Close (ms) of the `seconds` bar that contains `at_ms` (epoch aligned; these timeframes divide the hour)."""
    size = seconds * 1000
    return (at_ms // size + 1) * size


# --- the module functions the runtime needs next to Engine (ENGINES.md) --------------------------------

def input_identity(payload):
    """The dedupe identity of an alert: its alert_id."""
    value = payload.get('alert_id') if isinstance(payload, dict) else None
    return value if type(value) is str and _ALERT_ID.match(value) else None


def payload_schema(payload):
    """The alert's protocol, checked against the adapter's declared schemas before intake."""
    return PROTOCOL if isinstance(payload, dict) and payload.get('protocol') == PROTOCOL else 'undeclared'


def is_entry_payload(payload):
    return isinstance(payload, dict) and payload.get('event') == 'entry'


def signal_time(trade):
    """When the entry decision was made, Unix ms: the alert ts, or the touch time of a touch entry."""
    return trade['touched_at'] if trade.get('touched_at') is not None else trade['payload']['ts']


def bridge_message(adapter_id, command, ticker, side, trade):
    """One engine command in the bridge's adapter command format (bridge_adapters.ADAPTER_FIELDS)."""
    base = {'adapter_id': adapter_id, 'adapter_schema': PROTOCOL, 'command_id': command['id'],
            'trade_id': command['trade_id']}
    kind = command['kind']
    if kind == 'entry':
        action = 'buy' if side == 'long' else 'sell'
        extras = dict(base, signal_time=signal_time(trade))
        if trade['resting_stop'] is None:
            return {'ticker': ticker, 'action': action, 'orderType': 'market', 'quantity': command['quantity'],
                    'extras': extras}
        extras.update(bridge_kind='bracket_entry', reference_price=command['price'],
                      stop_price=trade['resting_stop'])
        staged = [x for x in trade['targets'] if x['via'] == 'bracket']
        if staged:
            extras.update(tp1_price=staged[0]['price'], tp1_quantity=staged[0]['qty'])
        return {'ticker': ticker, 'action': action, 'orderType': 'market', 'quantity': command['quantity'],
                'extras': extras}
    if kind == 'tp_limit':
        return {'ticker': ticker, 'action': 'exit', 'orderType': 'limit', 'quantity': command['quantity'],
                'limitPrice': command['price'], 'cancel': False, 'extras': base}
    if kind == 'modify_stop':
        return {'ticker': ticker, 'action': 'modify_stop',
                'extras': dict(base, bridge_kind='modify_stop', stop_price=command['price'],
                               stop_quantity=command['quantity'])}
    if kind == 'exit':
        return {'ticker': ticker, 'action': 'exit', 'orderType': 'market', 'quantity': command['quantity'],
                'cancel': False, 'extras': base}
    if kind == 'flatten':
        return {'ticker': ticker, 'action': 'flatten_all', 'extras': dict(base, bridge_kind='flatten_all')}
    raise ValueError('unsupported decrypt-bridge-v2 command kind')


def module_api(strategy):
    """What a strategy module exports: (Engine, input_identity, payload_schema, signal_time,
    is_entry_payload, bridge_message), all bound to `strategy` (a Strategy)."""
    require(isinstance(strategy, Strategy), 'module_api needs a Strategy')

    class StrategyEngine(Engine):
        STRATEGY = strategy

    def message(command, ticker, side, trade):
        return bridge_message(strategy.adapter_id, command, ticker, side, trade)

    StrategyEngine.__name__ = 'Engine'
    return StrategyEngine, input_identity, payload_schema, signal_time, is_entry_payload, message


class Engine:
    """One decrypt-bridge-v2 strategy on one account and dated contract. The lifecycle interface the runtime
    drives: apply, ready, start, transport_result, discard_entry, tick, validate_pine, close."""

    STRATEGY = None

    def __init__(self, database, config, *, tp1_limit_offset_handles=0):
        require(isinstance(self.STRATEGY, Strategy), 'decrypt-bridge-v2 engine needs its Strategy')
        require(tp1_limit_offset_handles == 0, 'decrypt-bridge-v2 targets come from the alert; no offset')
        spec = bridge_adapters.contract_spec(config.get('ticker'))
        require(config.get('root') == spec['root'] and config.get('mintick') == spec['tick_size'],
                'binding contract and tick size disagree')
        self.config = copy.deepcopy(config)
        self.tick_size = spec['tick_size']
        self.point_value = contract.POINT_VALUE[spec['root']]
        self.db = sqlite3.connect(str(database), check_same_thread=False)
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,hash TEXT NOT NULL,outcome TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS history(trade_id TEXT PRIMARY KEY,body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS inbox(seq INTEGER PRIMARY KEY AUTOINCREMENT,channel TEXT NOT NULL,received_ms INTEGER NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',detail TEXT);
            ''')
            pairing = digest({'config': config, 'strategy': self.STRATEGY.strategy_id,
                              'adapter': self.STRATEGY.adapter_id})
            row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
            self.data = json.loads(row[0]) if row else {
                'revision': REVISION, 'pairing': pairing, 'trade': None, 'commands': {}, 'broker_seq': 0,
                'last_quote': None, 'last_bar': None, 'realized': [], 'used_trade_ids': []}
            require(self.data['revision'] == REVISION and self.data['pairing'] == pairing,
                    'decrypt-bridge-v2 state belongs to another revision, strategy or binding')
            t = self.data['trade']
            if t and t['phase'] == 'armed':
                self._close('restart_touch_disarmed', None)     # an armed touch never survives a restart
            elif t and t['phase'] in OPEN:
                # A send in flight has an unknown outcome (never resent); only evidence observed after this
                # start drives management.
                t['broker_time'] = None
                for c in self.data['commands'].values():
                    if c['status'] == 'started':
                        c['status'] = 'unknown'
                        self._recovery('restart_with_' + c['kind'] + '_in_flight', None)
            self.save()
        except BaseException:
            self.db.close()
            raise

    # --- persistence -----------------------------------------------------------------------------------

    def save(self):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (canonical(self.data),))
        self.db.commit()

    def close(self):
        self.db.close()

    def market_bar_intervals(self):
        """5-second native bars while a trade has a bar-close stop (or may get one from break-even)."""
        t = self.data['trade']
        if t and t['phase'] in OPEN and (t['close_stop'] or t['be']['stop_type'] == 'bar_close'):
            return (BAR_MS,)
        return ()

    # --- intake ----------------------------------------------------------------------------------------

    def validate_pine(self, p, now):
        contract.validate(p, strategy=self.STRATEGY, now_ms=now)
        require(p['symbol'] == self.config['ticker'], 'symbol is not the binding\'s dated contract')
        require(p['stream_id'] == self.config['stream_id'], 'stream_id is not this binding\'s stream')

    def apply(self, channel, p, now, *, entries_enabled=False, locked=False):
        before = copy.deepcopy(self.data)
        try:
            eid = input_identity(p) if channel == 'pine' else p.get('event_id')
            require(type(eid) is str and 0 < len(eid) <= 240, 'event identity required')
            key, value = channel + ':' + eid, digest(p)
            old = self.db.execute('SELECT hash,outcome FROM events WHERE id=?', (key,)).fetchone()
            if old:
                require(old[0] == value, 'decrypt-bridge-v2 event identity changed content')
                return 'duplicate:' + old[1]
            allowed = entries_enabled and not locked
            if channel == 'pine':
                self.validate_pine(p, now)
                outcome = self.entry(p, now, allowed) if p['event'] == 'entry' else self.manage(p, now)
            elif channel == 'quote':
                require(p.get('symbol') == self.config['ticker'] and type(p.get('time')) is int
                        and 0 <= now - p['time'] <= QUOTE_MAX_AGE_MS and _price(p.get('price')), 'fresh quote')
                last = self.data['last_quote']
                require(not last or p['time'] > last['time'], 'quote must advance')
                self.data['last_quote'] = {'time': p['time'], 'price': p['price']}
                outcome = self.observe(p['price'], p['time'], now, allowed)
            elif channel == 'bar':
                outcome = self.bar(p, now)
            elif channel == 'broker':
                outcome = self.broker(p, now)
            else:
                raise ValueError('unknown input channel')
            self.advance(now)
            self.db.execute('INSERT INTO events VALUES(?,?,?)', (key, value, outcome))
            self.save()
            return outcome
        except BaseException:
            self.db.rollback()
            self.data = before
            raise

    def entry(self, p, now, allowed):
        if not allowed:
            return 'entry_disabled'
        previous = self.data['trade']
        if previous and previous['phase'] != 'closed':
            return 'refused:trade_open_or_pending'          # one trade at a time; never stacked or reversed
        if p['trade_id'] in self.data['used_trade_ids'] or \
                self.db.execute('SELECT 1 FROM history WHERE trade_id=?', (p['trade_id'],)).fetchone():
            return 'refused:trade_id_already_used'
        targets = p['targets']
        limits = sum(1 for x in targets if x['type'] == 'limit')
        slots = bridge_adapters.lookup(self.STRATEGY.adapter_id).take_profits
        if limits > slots:
            return 'refused:resting_targets_exceed_adapter'
        d = p['daily_loss']
        if d['enabled']:
            net = self.day_net(now, d, include_open=False)
            if net is None:
                return 'refused:daily_loss_pnl_unknown'
            if net <= -d['limit_usd']:
                return 'refused:daily_loss_locked'
        if previous:
            self.db.execute('INSERT OR IGNORE INTO history VALUES(?,?)',
                            (previous['id'], canonical({'trade': previous, 'commands': self.data['commands']})))
        s, b, e = p['stop'], p['break_even'], p['entry']
        resting = s['price'] if s['type'] == 'resting' else (s['safety_price'] or None)
        rows = []
        for i, x in enumerate(targets, 1):
            via = 'market' if x['type'] == 'market' else (
                'bracket' if resting is not None and not any(r['via'] == 'bracket' for r in rows) else 'limit')
            rows.append({'n': i, 'price': x['price'], 'qty': x['qty'], 'type': x['type'], 'via': via,
                         'state': 'pending', 'cid': None, 'filled': 0, 'crossed': False, 'fill_time': None})
        self.data['commands'] = {}
        self.data['trade'] = t = {
            'id': p['trade_id'], 'side': p['side'], 'payload': copy.deepcopy(p),
            'phase': 'armed' if e['type'] == 'touch' else 'entering', 'touched_at': None, 'opened_at': now,
            'eod_at': next_clock(p['ts'], p['eod']['time'], p['eod']['tz']) if p['eod']['enabled'] else None,
            'entry_qty': e['qty'], 'entry_filled': 0, 'remaining': 0, 'avg_fill_price': None, 'running_at': None,
            'broker_time': None, 'open_orders': [], 'orders': {}, 'stop_row': None,
            'resting_stop': resting, 'close_stop': ({'price': s['price'], 'tf': s['timeframe_s']}
                                                    if s['type'] == 'bar_close' else None),
            'targets': rows, 'runner': {'qty': p['runner']['qty'], 'closed': p['runner']['qty'] == 0, 'best': None,
                                        'exit_pending': False},
            'be': {'enabled': b['enabled'], 'stop_type': b['stop_type'] if b['enabled'] else None,
                   'price_hit': False, 'met_at': None, 'due_at': None, 'state': None},
            'exit_request': None, 'terminal_reason': None, 'recovery_reason': None, 'closed_reason': None,
            'step': None, 'counter': 0, 'last_price': None, 'tp1_done': False,
            'native': {'mode': 'decrypt-bridge-v2', 'stop_confirmed': False, 'failed': None, 'alerts': []}}
        self._remember(t['id'])
        if t['phase'] == 'armed':
            return 'touch_entry_armed'
        self.command('entry', e['qty'], e['price'])
        return 'entry_accepted_not_sent'

    def manage(self, p, now):
        """A management event. It acts only on the open trade with the same trade_id and side."""
        t, event = self.data['trade'], p['event']
        if not t or t['phase'] == 'closed' or t['id'] != p['trade_id'] or t['side'] != p['side']:
            return event + '_no_matching_trade'
        if event == 'cancel_entry':
            if t['phase'] != 'armed':
                return 'cancel_entry_ignored_not_armed'
            self._close('cancel_entry:' + p['reason'], now)
            return 'touch_entry_disarmed'
        if event == 'bias_flip' and not t['payload']['bias_flip']['enabled']:
            return 'bias_flip_ignored_disabled_by_entry'
        if t['phase'] == 'recovery_required':
            self._alert(t, event + '_during_recovery_needs_human', now)
            return event + '_held_recovery_required'
        if event in ('flatten', 'bias_flip'):
            self.terminal(event + ':' + p['reason'])
            return event + '_requested'
        if event == 'exit':
            if t['phase'] == 'closing':
                return 'exit_ignored_already_closing'
            if p['qty'] >= (t['remaining'] if t['phase'] == 'running' else t['entry_qty']):
                self.terminal('exit:' + p['reason'])
                return 'exit_requested'
            if t['phase'] in ('armed', 'entering'):
                return 'exit_ignored_no_position'
            if any(x['via'] in ('bracket', 'limit') and x['state'] == 'placed' for x in t['targets']):
                self._alert(t, 'partial_exit_refused_resting_targets', now)
                return 'exit_refused_resting_targets_working'
            t['exit_request'] = {'qty': p['qty'], 'reason': p['reason']}      # sent once the trade is running
            return 'exit_accepted'
        # modify_stop
        stop = p['stop']
        if t['phase'] in ('armed', 'entering', 'entered'):
            return 'modify_stop_ignored_not_running'
        if stop['safety_price'] and t['close_stop'] is None:
            return 'modify_stop_refused_safety_price_without_bar_close_stop'
        if t['close_stop'] is not None:
            t['close_stop']['price'] = stop['price']
            if stop['safety_price']:
                if t['resting_stop'] is None:
                    return 'modify_stop_refused_no_safety_stop'
                t['resting_stop'] = stop['safety_price']
        elif t['resting_stop'] is not None:
            t['resting_stop'] = stop['price']
        return 'modify_stop_accepted'

    # --- prices --------------------------------------------------------------------------------------

    def observe(self, price, at, now, allowed):
        """The plan's price rules on the binding's own last price. Nothing is sent from here directly."""
        t = self.data['trade']
        if not t or t['phase'] in ('closed', 'recovery_required') or t['terminal_reason']:
            return 'quote_recorded'
        p, sign = t['payload'], (1 if t['side'] == 'long' else -1)
        t['last_price'] = price
        if t['phase'] == 'armed':
            touch = p['touch']
            if touch['cancel_if_stop_touched'] and sign * (price - p['stop']['price']) <= 0:
                self._close('stop_touched_before_entry', now)
                return 'touch_entry_disarmed'
            if sign * (price - touch['price']) <= 0:
                if not allowed:
                    self._close('entry_held_at_touch', now)
                    return 'touch_entry_dropped'
                t['phase'], t['touched_at'] = 'entering', at
                self.command('entry', t['entry_qty'], touch['price'])
                return 'touch_entry_triggered'
            return 'quote_recorded'
        if t['phase'] != 'running':
            return 'quote_recorded'
        for x in t['targets']:
            if x['via'] == 'market' and sign * (price - x['price']) >= 0:
                x['crossed'] = True
        b = p['break_even']
        if t['be']['enabled'] and b['trigger_price'] and sign * (price - b['trigger_price']) >= 0:
            t['be']['price_hit'] = True
        self._be_check(now)
        r, runner = p['runner'], t['runner']
        if not runner['closed'] and r['mode'] == 'target' and sign * (price - r['price']) >= 0:
            self.terminal('runner_target')
            return 'runner_target_reached'
        if not runner['closed'] and r['mode'] == 'trail' and t['avg_fill_price'] is not None:
            runner['best'] = price if runner['best'] is None else (max if sign > 0 else min)(runner['best'], price)
            if sign * (runner['best'] - t['avg_fill_price']) >= r['trail_activation_ticks'] * self.tick_size:
                level = runner['best'] - sign * r['trail_distance_ticks'] * self.tick_size
                if sign * (price - level) <= 0:
                    runner['exit_pending'] = True
        self._daily_loss_check(now)
        return 'quote_observed'

    def bar(self, p, now):
        """A completed 5-second native bar: closes of the bar-close stop's timeframe."""
        require(set(p) == {'event_id', 'symbol', 'open_time', 'close_time', 'open', 'high', 'low', 'close'}
                and p['symbol'] == self.config['ticker'] and type(p['close_time']) is int
                and p['close_time'] - p['open_time'] == BAR_MS and _price(p['close']), 'native bar')
        last = self.data['last_bar']
        require(not last or p['close_time'] > last['close_time'], 'bars must advance')
        self.data['last_bar'] = {'close_time': p['close_time'], 'close': p['close']}
        t = self.data['trade']
        if not t or t['phase'] != 'running' or t['terminal_reason'] or not t['close_stop']:
            return 'bar_recorded'
        size = t['close_stop']['tf'] * 1000
        boundary = p['close_time'] // size * size
        if boundary <= t['running_at'] or not last or boundary <= last['close_time']:
            return 'bar_recorded'
        # The close of the timeframe bar: this bar when it ends on the boundary, else the last close before it.
        close = p['close'] if p['close_time'] == boundary else last['close']
        if p['close_time'] != boundary and last['close_time'] <= boundary - size:
            self._alert(t, 'bar_close_stop_no_bar_in_timeframe', now)
            return 'bar_recorded'
        sign = 1 if t['side'] == 'long' else -1
        if sign * (close - t['close_stop']['price']) <= 0:
            self.terminal('bar_close_stop')
            return 'bar_close_stop_hit'
        return 'bar_close_stop_held'

    # --- commands ------------------------------------------------------------------------------------

    def command(self, kind, quantity, price, *, stage=None):
        t = self.data['trade']
        t['counter'] += 1
        cid = 'v2-' + hashlib.sha256((t['id'] + ':' + str(t['counter']) + ':' + kind).encode()).hexdigest()[:32]
        self.data['commands'][cid] = {
            'id': cid, 'trade_id': t['id'], 'kind': kind, 'quantity': quantity, 'price': price, 'stage': stage,
            'status': 'ready', 'filled_qty': 0, 'started_at': None, 'result_at': None, 'receipt': None}
        return cid

    def ready(self):
        return [copy.deepcopy(c) for c in self.data['commands'].values() if c['status'] == 'ready']

    def discard_entry(self, reason):
        t = self.data['trade']
        for c in self.data['commands'].values():
            if c['status'] == 'ready':
                c['status'] = 'superseded'
        if t and t['phase'] != 'closed':
            self._close(reason, None)
        self.save()

    def start(self, cid, now):
        c, t = self.data['commands'][cid], self.data['trade']
        require(c['status'] == 'ready', 'decrypt-bridge-v2 commands are never retried as the same command')
        if c['kind'] == 'entry':
            e = t['payload']['entry']
            limit = TOUCH_DISPATCH_GRACE_MS if t['touched_at'] is not None else e['max_age_ms']
            if not -contract.MAX_AHEAD_MS <= now - signal_time(t) <= min(limit, contract.ENTRY_MAX_AGE_MS):
                self.discard_entry('stale_entry_not_replayed')
                return None
        c['status'], c['started_at'] = 'started', now
        self.save()
        return copy.deepcopy(c)

    def transport_result(self, cid, outcome, receipt, now):
        require(outcome in ('accepted', 'rejected', 'unknown'), 'decrypt-bridge-v2 transport result')
        c, t = self.data['commands'][cid], self.data['trade']
        require(c['status'] == 'started', 'decrypt-bridge-v2 attempt is not active')
        c['status'], c['receipt'], c['result_at'] = outcome, copy.deepcopy(receipt), now
        if outcome == 'unknown':
            self._recovery('unknown_outcome_' + c['kind'], now)
        elif c['kind'] == 'entry':
            if outcome == 'rejected':
                self._close('entry_refused', now)            # definitely not placed: nothing to manage
            else:
                t['phase'] = 'entered'
                staged = (receipt or {}).get('tp1')
                if isinstance(staged, dict) and (staged.get('sent') is False
                                                 or staged.get('outcome') not in (None, 'accepted')):
                    # The bracket's staged first limit target was not placed: place it as its own limit.
                    for x in t['targets']:
                        if x['via'] == 'bracket':
                            x['via'] = 'limit'
                    self._alert(t, 'first_limit_target_not_staged', now)
        elif outcome == 'rejected' and t['step'] and t['step']['cid'] == cid:
            t['step']['retry_at'] = now + RETRY_MS[min(t['step']['attempts'], len(RETRY_MS)) - 1]
        self.advance(now)
        self.save()

    # --- clock ---------------------------------------------------------------------------------------

    def tick(self, now):
        before = canonical(self.data)
        t = self.data['trade']
        if t and t['phase'] not in ('closed', 'recovery_required'):
            p = t['payload']
            entry = self._entry()
            unsent = entry is not None and entry['status'] == 'ready'
            if t['phase'] == 'armed' and now >= p['touch']['valid_until']:
                self._close('touch_expired', now)
            elif t['eod_at'] is not None and now >= t['eod_at'] and not t['terminal_reason']:
                if t['phase'] == 'armed' or unsent:
                    self.discard_entry('eod_before_entry_sent')
                else:
                    self.terminal('eod')
            elif t['phase'] == 'entering' and t['touched_at'] is not None and unsent \
                    and now - t['touched_at'] > TOUCH_DISPATCH_GRACE_MS:
                self._close('touch_entry_not_ready_dropped', now)
            if t['phase'] in ('entered', 'running', 'closing'):
                # An accepted order the broker evidence does not settle in time: a person checks.
                for c in self.data['commands'].values():
                    waiting = c['status'] in ('accepted', 'working', 'partial') and not (
                        c['kind'] == 'tp_limit' and c['status'] in ('working', 'partial'))
                    waiting = waiting or (c['kind'] == 'entry' and t['phase'] == 'entered')
                    if waiting and c['result_at'] is not None and now - c['result_at'] > EVIDENCE_TIMEOUT_MS:
                        self._recovery(c['kind'] + '_not_confirmed_by_broker_evidence', now)
                        break
            if t['phase'] == 'running':
                self._be_check(now)
        self.advance(now)
        if canonical(self.data) != before:
            self.save()

    # --- management (one order at a time, each on fresh broker evidence) ------------------------------

    def advance(self, now):
        t = self.data['trade']
        if not t or t['phase'] not in ('running', 'closing'):
            return
        if any(c['status'] in ('ready', 'started') for c in self.data['commands'].values()):
            return
        if t['broker_time'] is None or not 0 <= now - t['broker_time'] <= BROKER_MAX_AGE_MS:
            return
        if t['step'] and not self._step_done(t, now):
            return
        if t['terminal_reason']:
            t['phase'] = 'closing'
            if not t['step']:
                self._step(None, t['remaining'], 'flatten', 'flatten', t['remaining'], None)
            return
        remaining = t['remaining']
        if remaining <= 0:
            if t['open_orders']:
                # Flat with orders still working (closed by hand, or a target filled the rest): cancel them so
                # they can never open a position. flatten_all only cancels when the account is flat.
                self.terminal('flat_with_orders_working')
                t['phase'] = 'closing'
                self._step(None, 0, 'flatten', 'flatten', 0, None)
            return
        row = t['stop_row']
        if t['resting_stop'] is not None:
            if not row or row['status'] not in ('working', 'partial'):
                self._recovery('protective_stop_missing', now)
                return
            if row['status'] == 'partial':
                return                                   # the broker stop is filling; evidence settles it
            if (row.get('quantity'), row.get('stop_price')) != (remaining, t['resting_stop']):
                self._step(None, remaining, 'sync', 'modify_stop', remaining, t['resting_stop'])
                return
        for x in t['targets']:
            if x['via'] == 'limit' and x['state'] == 'pending':
                self._step(x['n'], x['qty'], 'place', 'tp_limit', x['qty'], x['price'])
                return
        if self._daily_loss_check(now):
            return self.advance(now)
        quote = self.data['last_quote']
        if not quote or not 0 <= now - quote['time'] <= QUOTE_MAX_AGE_MS:
            return
        price = quote['price']
        if t['exit_request']:
            req = t['exit_request']
            t['exit_request'] = None
            self._partial('manual', req['qty'], price)
            return
        resting_limits = any(x['via'] in ('bracket', 'limit') and x['state'] != 'filled' for x in t['targets'])
        for x in t['targets']:
            if x['via'] == 'market' and x['state'] == 'pending' and x['crossed']:
                if resting_limits:
                    return                               # waits until every nearer resting limit has filled
                self._partial(x['n'], x['qty'], price)
                return
        be = t['be']
        if be['state'] == 'due':
            self._apply_break_even(now, price)
            return
        runner = t['runner']
        if runner['exit_pending'] and not runner['closed']:
            if all(x['state'] == 'filled' for x in t['targets']):
                runner['closed'] = True
                self.terminal('runner_trail')
            elif not resting_limits:
                self._partial('runner', runner['qty'], price)
            return

    def _step_done(self, t, now):
        """True when the current step has finished (and clears it); False while waiting or retrying."""
        step = t['step']
        c = self.data['commands'][step['cid']]
        stage, status = step['stage'], c['status']
        if status == 'rejected':
            if step['attempts'] >= len(RETRY_MS):
                self._recovery(stage + '_refused_by_bridge', now)
                return False
            if now >= step['retry_at']:
                step['attempts'] += 1
                step['cid'] = self.command(c['kind'], c['quantity'], c['price'], stage=stage)
            return False
        if stage == 'flatten':
            return False                                  # closed by broker evidence (flat, nothing working)
        if stage == 'place':
            if status in ('working', 'partial', 'filled'):
                t['step'] = None
                return True
            if status == 'accepted':
                return False
        elif stage in ('sync', 'shrink'):
            if status == 'confirmed':
                if stage == 'shrink':
                    if t['terminal_reason']:
                        t['step'] = None
                        return True
                    if step['qty'] >= t['remaining']:
                        self._recovery('position_changed_during_partial', now)
                        return False
                    self._step(step['leg'], step['qty'], 'exit', 'exit', step['qty'], None)
                    return False
                t['step'] = None
                return True
            if status == 'accepted':
                return False
        elif stage == 'exit':
            if status == 'filled':
                self._leg_done(step['leg'], now)
                t['step'] = None
                return True
            if status in ('accepted', 'working', 'partial'):
                return False
        self._recovery(stage + '_' + status, now)
        return False

    def _step(self, leg, qty, stage, kind, quantity, price):
        t = self.data['trade']
        cid = self.command(kind, quantity, price, stage=stage)
        t['step'] = {'leg': leg, 'qty': qty, 'stage': stage, 'cid': cid, 'attempts': 1, 'retry_at': None}
        if stage == 'place':
            target = t['targets'][leg - 1]
            target['cid'], target['state'] = cid, 'placing'

    def _partial(self, leg, quantity, price):
        """Exit `quantity` at market. The resting stop is shrunk first so it can never over-fill."""
        t = self.data['trade']
        quantity = min(quantity, t['remaining'])
        if quantity <= 0:
            self._leg_done(leg, None)
            return
        if quantity >= t['remaining']:
            self._leg_done(leg, None)
            self.terminal('%s_closes_position' % (leg if isinstance(leg, str) else 'target_%d' % leg))
            t['phase'] = 'closing'
            self._step(None, t['remaining'], 'flatten', 'flatten', t['remaining'], None)
            return
        left = t['remaining'] - quantity
        row = t['stop_row']
        if t['resting_stop'] is not None and row and row.get('quantity', 0) > left:
            self._step(leg, quantity, 'shrink', 'modify_stop', left, row.get('stop_price'))
        else:
            self._step(leg, quantity, 'exit', 'exit', quantity, None)

    def _leg_done(self, leg, now):
        t = self.data['trade']
        if leg == 'runner':
            t['runner']['closed'], t['runner']['exit_pending'] = True, False
        elif isinstance(leg, int):
            x = t['targets'][leg - 1]
            x['state'], x['fill_time'] = 'filled', now
            self._target_filled(x, now)

    def _target_filled(self, x, at):
        t = self.data['trade']
        if x['n'] == 1:
            t['tp1_done'] = True                          # the runtime status reads this

    # --- break-even ----------------------------------------------------------------------------------

    def _be_check(self, now):
        """Arm break-even when the alert's triggers are met; due at once (touch) or at the bar close."""
        t = self.data['trade']
        be, b = t['be'], t['payload']['break_even']
        if not be['enabled'] or be['state'] is not None or t['phase'] != 'running' or t['terminal_reason']:
            return
        if be['met_at'] is None:
            conditions = []
            if b['trigger_price']:
                conditions.append(be['price_hit'])
            if b['trigger_target']:
                conditions.append(t['targets'][b['trigger_target'] - 1]['state'] == 'filled')
            if not conditions or not (all(conditions) if b['trigger_mode'] == 'all' else any(conditions)):
                return
            met = now
            if b['trigger_target'] and t['targets'][b['trigger_target'] - 1]['state'] == 'filled':
                met = t['targets'][b['trigger_target'] - 1]['fill_time'] or now
            be['met_at'] = met
            be['due_at'] = met if b['confirm'] == 'touch' else bar_end(met, b['confirm_timeframe_s'])
        if now >= be['due_at']:
            be['state'] = 'due'

    def _apply_break_even(self, now, price):
        t = self.data['trade']
        be, b = t['be'], t['payload']['break_even']
        sign = 1 if t['side'] == 'long' else -1
        if b['stop_price']:
            level = b['stop_price']
        else:
            if t['avg_fill_price'] is None:
                self._recovery('entry_average_fill_missing', now)
                return
            level = round_tick(t['avg_fill_price'] + sign * b['offset_ticks'] * self.tick_size, self.tick_size)
        if sign * (price - level) <= 0:
            if b['if_beyond'] == 'flatten':
                be['state'] = 'through'
                self.terminal('break_even_price_through')
            else:
                be['state'] = 'skipped_price_through'
            return
        be['state'] = 'applied'
        if b['stop_type'] == 'bar_close':
            current = t['close_stop']
            if current is None or sign * (level - current['price']) > 0:
                t['close_stop'] = {'price': level, 'tf': b['stop_timeframe_s']}
            return
        if sign * (level - t['resting_stop']) > 0:          # never loosen the stop
            t['resting_stop'] = level

    # --- daily loss ----------------------------------------------------------------------------------

    def _row_pnl(self, t, row):
        """Net USD of one exit row of trade `t`, or None when a price is unknown."""
        if not row['filled_qty']:
            return 0.0
        avg, entry = row.get('avg_fill_price'), t['avg_fill_price']
        if not _price(avg) or entry is None:
            return None
        sign = 1 if t['side'] == 'long' else -1
        fee = t['payload']['daily_loss']['fee_per_contract_usd']
        return (avg - entry) * sign * row['filled_qty'] * self.point_value - fee * row['filled_qty']

    def _exit_rows(self, t):
        entry = self._entry()
        return [o for cid, o in t['orders'].items() if not entry or cid != entry['id']]

    def day_net(self, now, d, *, include_open):
        """Realized net USD of the trading day of `now` (tz, day_start), or None when unknown."""
        key = day_key(now, d['tz'], d['day_start'])
        total = 0.0
        for at, usd in self.data['realized']:
            if day_key(at, d['tz'], d['day_start']) == key:
                if usd is None:
                    return None
                total += usd
        t = self.data['trade']
        if t and t['phase'] not in ('closed', 'armed') and t['entry_filled']:
            for row in self._exit_rows(t):
                usd = self._row_pnl(t, row)
                if usd is None:
                    return None
                total += usd
            if include_open and t['remaining'] > 0:
                quote = self.data['last_quote']
                if t['avg_fill_price'] is None or not quote:
                    return None
                sign = 1 if t['side'] == 'long' else -1
                fee = t['payload']['daily_loss']['fee_per_contract_usd']
                total += (quote['price'] - t['avg_fill_price']) * sign * t['remaining'] * self.point_value \
                    - fee * t['remaining']
        return total

    def _daily_loss_check(self, now):
        """With daily_loss.flatten: flatten the open trade once the day reaches the limit. True when it did."""
        t = self.data['trade']
        d = t['payload']['daily_loss'] if t else None
        if not d or not d['enabled'] or not d['flatten'] or t['phase'] != 'running' or t['terminal_reason']:
            return False
        net = self.day_net(now, d, include_open=d['include_open'])
        if net is None:
            self._recovery('daily_loss_pnl_unknown', now)
            return False
        if net <= -d['limit_usd']:
            self.terminal('daily_loss')
            return True
        return False

    # --- broker evidence (the bridge's adapter-db snapshots) -----------------------------------------

    def _entry(self):
        return next((c for c in self.data['commands'].values() if c['kind'] == 'entry'), None)

    def broker(self, p, now):
        t = self.data['trade']
        require(isinstance(p, dict), 'broker evidence shape')
        if t and t['phase'] == 'closed' and p.get('trade_id') == t['id'] and p.get('position_qty') == 0 \
                and p.get('open_orders') == []:
            return 'broker_evidence_after_close'
        require(t is not None and t['phase'] not in ('closed', 'armed'), 'broker evidence needs an open trade')
        require(set(p) == EVIDENCE_FIELDS, 'broker evidence fields')
        require(p['account_id'] == self.config['account_id'] and p['symbol'] == self.config['ticker']
                and p['trade_id'] == t['id'] and p['side'] == t['side'], 'broker evidence is for another binding or trade')
        seq, at = p['sequence'], p['observed_at']
        require(type(seq) is int and seq > self.data['broker_seq'] and type(at) is int
                and 0 <= now - at <= BROKER_MAX_AGE_MS, 'broker evidence sequence or freshness')
        require(t['broker_time'] is None or at >= t['broker_time'], 'broker evidence went back in time')
        require(type(p['position_qty']) is int and p['position_qty'] >= 0 and type(p['orders']) is list
                and type(p['open_orders']) is list and all(type(o) is str for o in p['open_orders']),
                'broker evidence shape')
        entry = self._entry()
        require(entry is not None and entry['status'] not in ('ready', 'superseded'), 'evidence before the entry was sent')
        rows, seen = {}, set()
        for o in p['orders']:
            require(isinstance(o, dict) and type(o.get('command_id')) is str and type(o.get('broker_order_id')) is str
                    and o['broker_order_id'] and o.get('status') in ORDER_STATUSES and type(o.get('filled_qty')) is int
                    and o['filled_qty'] >= 0 and o['command_id'] not in seen, 'broker order row')
            old = t['orders'].get(o['command_id'])
            require(not old or (old['broker_order_id'] == o['broker_order_id'] and o['filled_qty'] >= old['filled_qty']),
                    'broker order id changed or fills went back')
            seen.add(o['command_id'])
            rows[o['command_id']] = copy.deepcopy(o)
        self.data['broker_seq'] = seq
        t['broker_time'], t['open_orders'] = at, list(p['open_orders'])
        commands = self.data['commands']
        legs = {entry['id'] + '#stop': 'stop', entry['id'] + '#tp1': 'tp1'}
        unknown = [cid for cid in rows if cid not in legs and cid not in commands]
        if unknown:
            return self._recovery('broker_order_not_from_this_engine', now) or 'broker_evidence_unknown_order'
        exited = 0
        for cid, o in rows.items():
            if cid in legs:
                if legs[cid] == 'stop':
                    require(type(o.get('quantity')) is int and _price(o.get('stop_price')), 'stop row quantity/price')
                exited += o['filled_qty']
                continue
            c = commands[cid]
            if c['status'] in ('ready', 'superseded'):
                return self._recovery('unsent_command_has_broker_orders', now) or 'broker_evidence_unknown_order'
            c['filled_qty'] = o['filled_qty']
            if o['status'] == 'filled' and o['filled_qty'] == c['quantity']:
                c['status'] = 'filled'
            elif o['status'] in ('cancelled', 'rejected'):
                c['status'] = o['status']
            elif c['status'] in ('accepted', 'working', 'partial'):
                c['status'] = o['status']
            if c['kind'] != 'entry':
                exited += o['filled_qty']
        t['orders'] = rows
        t['stop_row'] = rows.get(entry['id'] + '#stop')
        entry_row = rows.get(entry['id'])
        t['entry_filled'] = entry_row['filled_qty'] if entry_row else 0
        t['remaining'] = p['position_qty']
        if t['entry_filled'] - exited != p['position_qty']:
            return self._recovery('broker_fills_do_not_explain_position', now) or 'broker_evidence_mismatch'
        if t['entry_filled']:
            if _price(entry_row.get('avg_fill_price')):
                t['avg_fill_price'] = entry_row['avg_fill_price']
            elif self._needs_fill_price(t):
                return self._recovery('entry_average_fill_missing', now) or 'broker_evidence_no_fill_price'
        row = t['stop_row']
        for c in commands.values():
            if c['kind'] == 'modify_stop' and c['status'] == 'accepted' and row \
                    and row.get('quantity') == c['quantity'] and row.get('stop_price') == c['price']:
                c['status'] = 'confirmed'
        t['native']['stop_confirmed'] = bool(row and row['status'] == 'working' and row['quantity'] == p['position_qty'])
        self._targets_from_rows(t, rows, entry, at, now)
        if t['phase'] == 'recovery_required':
            return 'broker_evidence_recorded_recovery_required'
        if t['phase'] in ('entering', 'entered') and entry_row:
            if entry_row['status'] == 'filled' and t['entry_filled'] == t['entry_qty']:
                if t['resting_stop'] is None or (row and row['status'] == 'working' and row['quantity'] == t['entry_qty']
                                                 and row['stop_price'] == t['resting_stop']):
                    t['phase'], t['running_at'] = 'running', at
            elif entry_row['status'] in ('cancelled', 'rejected'):
                if t['entry_filled'] == 0 and p['position_qty'] == 0:
                    self._close('entry_not_filled', now)
                    return 'broker_evidence_entry_not_filled'
                return self._recovery('entry_partly_filled', now) or 'broker_evidence_entry_partly_filled'
        if t['entry_filled'] and p['position_qty'] == 0 and not p['open_orders']:
            reason = t['terminal_reason'] or ('stop_filled' if row and row['filled_qty'] else 'flat_at_broker')
            self._close(reason, now)
            return 'broker_evidence_trade_closed'
        return 'broker_evidence_reconciled'

    def _needs_fill_price(self, t):
        p = t['payload']
        return (p['daily_loss']['enabled'] or (p['runner']['mode'] == 'trail' and p['runner']['qty'])
                or (p['break_even']['enabled'] and not p['break_even']['stop_price']))

    def _targets_from_rows(self, t, rows, entry, at, now):
        """Resting limit targets: placed, filled, or gone (a person checks)."""
        for x in t['targets']:
            if x['via'] == 'market' or x['state'] == 'filled':
                continue
            cid = entry['id'] + '#tp1' if x['via'] == 'bracket' else x['cid']
            o = rows.get(cid) if cid else None
            if not o:
                continue
            x['filled'] = o['filled_qty']
            if o['status'] == 'filled' and o['filled_qty'] >= x['qty']:
                x['state'] = 'filled'
                x['fill_time'] = o.get('fill_time') if type(o.get('fill_time')) is int else at
                self._target_filled(x, x['fill_time'])
            elif o['status'] in ('working', 'partial'):
                x['state'] = 'placed'
            elif t['remaining'] > 0 and t['phase'] not in ('closing',) and not t['terminal_reason']:
                self._recovery('limit_target_%d_%s' % (x['n'], o['status']), now)

    # --- closing, recovery and alerts ----------------------------------------------------------------

    def terminal(self, reason):
        """Exit everything (flatten_all) for `reason`; an armed touch or an unsent entry just closes."""
        t = self.data['trade']
        if not t or t['phase'] in ('closed', 'recovery_required') or t['terminal_reason']:
            return
        t['terminal_reason'] = reason
        for c in self.data['commands'].values():
            if c['status'] == 'ready' and c['kind'] != 'entry':
                c['status'] = 'superseded'
        entry = self._entry()
        if t['phase'] == 'armed' or (entry and entry['status'] == 'ready'):
            self._close(reason, None)                   # nothing is at the broker: send nothing
            return
        step = t['step']
        if step and self.data['commands'][step['cid']]['status'] in ('ready', 'superseded', 'rejected'):
            t['step'] = None

    def _remember(self, trade_id):
        used = self.data['used_trade_ids']
        if trade_id not in used:
            used.append(trade_id)
        del used[:-2000]

    def _close(self, reason, now):
        t = self.data['trade']
        t['phase'], t['step'] = 'closed', None
        t['closed_reason'] = t['terminal_reason'] or reason
        for c in self.data['commands'].values():
            if c['status'] == 'ready':
                c['status'] = 'superseded'
        if t['entry_filled']:
            for row in self._exit_rows(t):
                if row['filled_qty']:
                    at = row.get('fill_time') if type(row.get('fill_time')) is int else (t['broker_time'] or now or 0)
                    usd = self._row_pnl(t, row)
                    self.data['realized'].append([at, None if usd is None else round(usd, 6)])
            del self.data['realized'][:-1000]

    def _alert(self, t, code, now):
        if t is not None:
            t['native']['alerts'] = (t['native']['alerts'] + [{'code': code, 'at': now}])[-50:]

    def _recovery(self, reason, now):
        """Hold for a person: nothing more is sent for this trade and entries stay held."""
        t = self.data['trade']
        if t['phase'] != 'recovery_required':
            t['phase'], t['recovery_reason'] = 'recovery_required', reason
            t['native']['failed'] = {'reason': reason, 'at': now}
            self._alert(t, 'recovery_required:' + reason, now)
            for c in self.data['commands'].values():
                if c['status'] == 'ready':
                    c['status'] = 'superseded'
        return None

    def release_recovery(self, now):
        """A person checked Tradovate and closed everything: fresh broker evidence must show the account flat
        with nothing working. Only then is the trade closed and the next entry allowed. Never sends anything."""
        t = self.data['trade']
        require(t and t['phase'] == 'recovery_required', 'no trade waits for recovery')
        require(t['broker_time'] is not None and 0 <= now - t['broker_time'] <= BROKER_MAX_AGE_MS
                and t['remaining'] == 0 and not t['open_orders'],
                'fresh broker evidence must show the account flat with nothing working')
        t['terminal_reason'] = t['terminal_reason'] or 'recovered_by_person'
        self._close('recovered_by_person', now)
        self.save()
