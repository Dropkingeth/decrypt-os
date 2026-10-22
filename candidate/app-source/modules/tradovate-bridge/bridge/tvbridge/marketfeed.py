"""The 1-second market feed for the binding's own dated contract. Lumi RTH Gap requires it.

Source: the broker's own market data. The page script `page/mdfeed.js` opens the trader page's market-data
connection a second time, with the page's own market-data token (it never leaves the page), subscribes to
quotes for exactly one contract (the binding's, e.g. MESZ2021 = Tradovate `MESZ1`) and reports each quote's
contract id, time and last trade price. This module turns those quotes into 1-second bars and says whether
the feed is live and fresh. GET /feed/<token> (daemon.py) serves the result; the trading runtime reads it
(decrypt_trading/bridge_feed.py) for Lumi's touch entries, break-even and price-hit exits.

Safety rules, the same as the bridge's other per-contract price reading (`last-market-price.json`):
- own contract only: a quote for any other contract id stops the feed (fail closed) until the daemon
  restarts; quotes before the pinned contract id is known are ignored;
- prices never open a trade: this module has no order path. It only answers GET /feed;
- stale means closed: no quote for STALE_MS, a second without any quote (a gap), a page connection that
  is not subscribed, or an off-tick price all make the feed not fresh. After a gap or a (re)start the feed
  is fresh again only after WARMUP_S consecutive live seconds;
- nothing is kept across a restart: a new daemon starts with an empty feed.

Times: seconds and freshness use this machine's clock (the runtime checks freshness on the same clock). The
broker's own quote time is checked too, with CLOCK_SKEW_MS of tolerance for the two clocks: a quote whose broker
time is more than STALE_MS + CLOCK_SKEW_MS old when it arrives (a late backlog) or more than CLOCK_SKEW_MS ahead
is not counted, and the feed is not fresh once the newest broker time is that old. A second counts only with a
trade price seen in it or within STALE_MS before its close; an older price is never carried forward.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from collections import Counter, deque
from pathlib import Path

from . import protocol

log = logging.getLogger('tvbridge')

SCHEMA = 'decrypt.tvbridge-1s-feed.v1'
SCRIPT = Path(__file__).resolve().parent.parent / 'page' / 'mdfeed.js'
SECOND_MS = 1000
STALE_MS = 2000          # no quote for the contract for longer than this: not fresh
WARMUP_S = 3             # consecutive live seconds needed after a start, reconnect or gap
CLOCK_SKEW_MS = 1000     # tolerance between the broker's quote times and this clock
BROKER_MAX_AGE_MS = STALE_MS + CLOCK_SKEW_MS   # a quote with an older broker time is late: not counted
KEEP_BARS = 120          # closed 1-second bars kept in memory (two minutes)
SERVE_BARS = 10          # bars GET /feed returns
MAX_QUOTES_PER_EVENT = 50
DEFAULT_MD_URL_PATTERN = r'^wss://md(-demo)?\.tradovateapi\.com/v1/websocket$'

# Adapters that refuse to run without this feed (the daemon refuses to start, the runtime refuses entries).
REQUIRED_BY = {'lumi-gap-bridge-v2': 'Lumi RTH Gap'}

MESSAGES = {
    None: 'The 1-second feed is live and fresh.',
    'halted_wrong_contract': 'The 1-second feed stopped: the broker sent prices for a different contract. '
                             'Restart the bridge daemon; until then no feed price is used.',
    'contract_unknown': 'The 1-second feed is waiting for the bridge to pin its contract.',
    'md_not_connected': 'The 1-second feed is not connected to the broker\'s market data yet.',
    'md_auth_failed': 'The broker refused the 1-second feed\'s market-data login. It retries after the trader '
                      'page signs in again.',
    'no_data': 'The 1-second feed has no prices from the broker yet.',
    'no_recent_trade': 'The 1-second feed has no trade price from the last 2 seconds.',
    'stale': 'The 1-second feed is stale: no broker price for more than 2 seconds.',
    'warming_up': 'The 1-second feed is warming up after a start, reconnect or gap (3 live seconds needed).',
}


def message(reason):
    """Plain-language text for a reason code (codes may carry ':detail')."""
    return MESSAGES.get(reason.split(':')[0] if isinstance(reason, str) else reason, MESSAGES['stale'])


def _now_ms():
    return time.time_ns() // 1_000_000


def settings(cfg):
    """None when the feed is off; else {'md_url_pattern': str}. Raises ValueError on a bad `market_feed`."""
    raw = cfg.get('market_feed') if isinstance(cfg, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict) or not set(raw) <= {'enabled', 'md_url_pattern'} \
            or not isinstance(raw.get('enabled'), bool):
        raise ValueError('market_feed must be {"enabled": true|false} with an optional "md_url_pattern"')
    if not raw['enabled']:
        return None
    pattern = raw.get('md_url_pattern', DEFAULT_MD_URL_PATTERN)
    if not isinstance(pattern, str) or not pattern.startswith('^wss://') or not pattern.endswith('$') \
            or r'\.tradovateapi\.com/' not in pattern or len(pattern) > 200:
        raise ValueError('market_feed.md_url_pattern must be an anchored ^wss://...tradovateapi.com/...$ pattern')
    try:
        re.compile(pattern)
    except re.error:
        raise ValueError('market_feed.md_url_pattern is not a valid pattern') from None
    return {'md_url_pattern': pattern}


def requirement_problem(cfg, enabled_adapter_ids):
    """Plain refusal when an enabled adapter needs the feed and the config does not turn it on, else None."""
    try:
        on = settings(cfg) is not None
    except ValueError as exc:
        return str(exc)
    for adapter_id in enabled_adapter_ids:
        if adapter_id in REQUIRED_BY and not on:
            return (f'{REQUIRED_BY[adapter_id]} ({adapter_id}) requires the 1-second MES feed: add '
                    f'"market_feed": {{"enabled": true}} to this bridge config '
                    f'(setup.py init --adapter {adapter_id} writes it)')
    return None


class SecondFeed:
    """1-second bars from the broker's quotes for one pinned contract. Thread safe; in memory only."""

    def __init__(self, symbol, *, contract_id, tick_size, clock_ms=_now_ms):
        self.symbol = symbol                        # the binding's dated contract, e.g. MESZ2021
        self._contract_id = contract_id             # int, or a callable returning the pinned id (or None)
        self.tick_size = tick_size
        self.clock_ms = clock_ms
        self.lock = threading.Lock()
        self.started_at = clock_ms()
        self.md_state = 'not_started'
        self.md_reason = None
        self.halted = None
        self.bars = deque(maxlen=KEEP_BARS)
        self.current = None                         # the open second: {'t', 'o', 'h', 'l', 'c', 'trades', 'quotes'}
        self.last_trade_price = None
        self.last_trade_at = None                   # this machine's clock, ms
        self.last_quote_at = None                   # this machine's clock, ms
        self.last_broker_time = None                # the broker's quote time, ms
        self.run_start = None                       # first second (ms) of the current unbroken run of bars
        self.gaps = 0
        self.rejected = Counter()

    def contract_id(self):
        value = self._contract_id() if callable(self._contract_id) else self._contract_id
        return value if type(value) is int else None

    # --- input from the page ----------------------------------------------------------------------

    def on_page_event(self, msg):
        """One message from page/mdfeed.js. Never raises: anything malformed is counted and dropped."""
        try:
            kind, data = msg.get('type'), msg.get('data') or {}
            if kind == 'md_state':
                self.on_state(data.get('state'), data.get('reason'))
            elif kind == 'md_quotes':
                quotes = data.get('quotes')
                if not isinstance(quotes, list) or len(quotes) > MAX_QUOTES_PER_EVENT:
                    raise ValueError('quotes')
                now = self.clock_ms()
                for q in quotes:
                    self.on_quote(q.get('contractId'), q.get('timestamp'), q.get('price'), now)
            else:
                raise ValueError('kind')
        except (AttributeError, TypeError, ValueError):
            with self.lock:
                self.rejected['malformed_event'] += 1

    def on_state(self, state, reason=None):
        state = state if isinstance(state, str) and len(state) <= 40 else 'unknown'
        reason = reason if isinstance(reason, str) and len(reason) <= 60 else None
        with self.lock:
            if state != self.md_state:
                log.info('1-second feed: market data %s%s', state, f' ({reason})' if reason else '')
            self.md_state = state
            self.md_reason = reason
            if state != 'subscribed':
                self._break_run()                    # a new subscription starts a new run (warm-up again)

    def on_quote(self, contract_id, timestamp, price, received_ms=None):
        now = self.clock_ms() if received_ms is None else received_ms
        with self.lock:
            if self.halted:
                self.rejected['halted'] += 1
                return
            if self.md_state != 'subscribed':
                self.rejected['not_subscribed'] += 1
                return
            if type(contract_id) is not int:
                self.rejected['malformed_quote'] += 1
                return
            expected = self.contract_id()
            if expected is None:
                self.rejected['contract_unknown'] += 1
                return
            if contract_id != expected:
                self.halted = 'wrong_contract'
                self._break_run()
                log.error('1-second feed stopped: a quote for contract id %s, not the pinned %s (%s)',
                          contract_id, expected, self.symbol)
                return
            try:
                broker_ms = protocol.to_ms(timestamp)
            except (ValueError, TypeError):
                self.rejected['malformed_quote'] += 1
                return
            if broker_ms - now > CLOCK_SKEW_MS:
                self.rejected['clock_skew'] += 1
                return
            if now - broker_ms > BROKER_MAX_AGE_MS:
                self.rejected['late_quote'] += 1      # delivered late (a backlog): never counted as live
                return
            if price is not None and (isinstance(price, bool) or not protocol.on_tick(price, self.tick_size)):
                self.rejected['invalid_price'] += 1
                return
            self._roll(now)
            if self.current is None or self.current['t'] != now // SECOND_MS * SECOND_MS:
                self.current = {'t': now // SECOND_MS * SECOND_MS, 'o': None, 'h': None, 'l': None, 'c': None,
                                'trades': 0, 'quotes': 0}
            cur = self.current
            cur['quotes'] += 1
            if price is not None:
                price = float(price)
                cur['trades'] += 1
                cur['o'] = price if cur['o'] is None else cur['o']
                cur['h'] = price if cur['h'] is None else max(cur['h'], price)
                cur['l'] = price if cur['l'] is None else min(cur['l'], price)
                cur['c'] = price
                self.last_trade_price = price
                self.last_trade_at = now
            self.last_quote_at = now
            self.last_broker_time = broker_ms

    # --- seconds ------------------------------------------------------------------------------------

    def _break_run(self):
        self.run_start = None
        self.current = None

    def _roll(self, now):
        """Close the open second once it has passed. A whole second with no quote is a gap."""
        second = now // SECOND_MS * SECOND_MS
        cur = self.current
        if cur is None or cur['t'] >= second:
            return
        self.current = None
        price = cur['c']
        if price is None and self.last_trade_at is not None \
                and cur['t'] + SECOND_MS - self.last_trade_at <= STALE_MS:
            price = self.last_trade_price             # a quiet second inside the 2 s window
        if price is None:
            self.run_start = None                     # no recent trade price: this second does not count
        else:
            bar = {'t': cur['t'], 'close_time': cur['t'] + SECOND_MS,
                   'o': cur['o'] if cur['o'] is not None else price, 'h': cur['h'] if cur['h'] is not None else price,
                   'l': cur['l'] if cur['l'] is not None else price, 'c': price, 'trades': cur['trades']}
            if not self.bars or self.bars[-1]['t'] != bar['t'] - SECOND_MS or self.run_start is None:
                self.run_start = bar['t']
            self.bars.append(bar)
        if cur['t'] + SECOND_MS < second:
            self.gaps += 1                            # at least one whole second without any quote
            self.run_start = None

    # --- output -------------------------------------------------------------------------------------

    def reason(self, now):
        """None when live and fresh, else a reason code (MESSAGES)."""
        if self.halted:
            return 'halted_' + self.halted
        if self.contract_id() is None:
            return 'contract_unknown'
        if self.md_state == 'auth_failed':
            return 'md_auth_failed'
        if self.md_state != 'subscribed':
            return 'md_not_connected:' + self.md_state
        if self.last_quote_at is None:
            return 'no_data'
        if not 0 <= now - self.last_quote_at <= STALE_MS or now - self.last_broker_time > BROKER_MAX_AGE_MS:
            return 'stale'
        if self.last_trade_at is None or not 0 <= now - self.last_trade_at <= STALE_MS:
            return 'no_recent_trade'
        second = now // SECOND_MS * SECOND_MS
        last = self.bars[-1] if self.bars else None
        if last is None or self.run_start is None or last['close_time'] != second:
            return 'warming_up'
        if (last['t'] - self.run_start) // SECOND_MS + 1 < WARMUP_S:
            return 'warming_up'
        return None

    def snapshot(self, now=None):
        """The GET /feed answer: status plus the latest closed 1-second bars (prices only, no secrets)."""
        with self.lock:
            now = self.clock_ms() if now is None else now
            self._roll(now)
            if self.current is None and self.last_quote_at is not None and self.bars \
                    and self.bars[-1]['close_time'] < now // SECOND_MS * SECOND_MS:
                # No quote since the last closed bar and at least one whole second has passed: a gap.
                if self.run_start is not None:
                    self.gaps += 1
                self.run_start = None
            reason = self.reason(now)
            fresh = reason is None
            live = (self.md_state == 'subscribed' and not self.halted and self.last_quote_at is not None
                    and 0 <= now - self.last_quote_at <= STALE_MS)
            return {
                'schema': SCHEMA, 'configured': True, 'symbol': self.symbol, 'contract_id': self.contract_id(),
                'observed_at': now, 'started_at': self.started_at, 'second_ms': SECOND_MS,
                'stale_ms': STALE_MS, 'warmup_s': WARMUP_S, 'md_state': self.md_state, 'md_reason': self.md_reason,
                'live': live, 'fresh': fresh, 'reason': reason, 'message': message(reason),
                'halted': self.halted, 'last_quote_at': self.last_quote_at, 'last_broker_time': self.last_broker_time,
                'last_trade_price': self.last_trade_price, 'run_start': self.run_start, 'gaps': self.gaps,
                'rejected': dict(self.rejected),
                'bars': [dict(b) for b in list(self.bars)[-SERVE_BARS:]] if fresh else [],
            }


def not_configured(symbol):
    """The GET /feed answer of a daemon without the feed."""
    return {'schema': SCHEMA, 'configured': False, 'symbol': symbol, 'live': False, 'fresh': False,
            'reason': 'not_configured', 'bars': [],
            'message': 'This bridge has no 1-second feed. Add "market_feed": {"enabled": true} to its config.'}


class PageFeed:
    """Installs page/mdfeed.js beside the order bridge's page script (tvbridge.bridge extensions)."""

    def __init__(self, feed, *, md_url_pattern, trade_url_pattern, md_symbol, bridge_api):
        self.feed = feed
        self.api = '__dlbm_' + secrets.token_hex(8)
        self.binding = '__dlbme_' + secrets.token_hex(8)
        self.script_id = None
        self.config = {'mdUrlPattern': md_url_pattern, 'tradeUrlPattern': trade_url_pattern, 'symbol': md_symbol,
                       'api': self.api, 'binding': self.binding, 'bridgeApi': bridge_api}

    def source(self):
        return SCRIPT.read_text().replace('__DLB_MD_CONFIG__', json.dumps(self.config))

    def install(self, session, evaluate_now=False):
        session.call('Runtime.addBinding', {'name': self.binding})
        source = self.source()
        self.script_id = session.call('Page.addScriptToEvaluateOnNewDocument', {'source': source})['identifier']
        if evaluate_now:
            session.evaluate(source)

    def on_binding(self, params):
        """True when the binding call was ours (handled), False otherwise."""
        if params.get('name') != self.binding:
            return False
        try:
            msg = json.loads(params['payload'])
        except (TypeError, ValueError, KeyError):
            msg = None
        self.feed.on_page_event(msg if isinstance(msg, dict) else {})
        return True

    def lost(self):
        """The page connection is gone (reload, lease loss): the feed is not live until it subscribes again."""
        self.feed.on_state('page_reset')

    def uninstall(self, session):
        ok = True
        try:
            if self.script_id:
                session.call('Page.removeScriptToEvaluateOnNewDocument', {'identifier': self.script_id})
                self.script_id = None
        except Exception:  # noqa: BLE001
            ok = False
        try:
            api = json.dumps(self.api)
            ok = session.evaluate('window[%s] ? window[%s].shutdown() : true' % (api, api), timeout=3) is True and ok
        except Exception:  # noqa: BLE001
            ok = False
        try:
            session.call('Runtime.removeBinding', {'name': self.binding}, timeout=3)
        except Exception:  # noqa: BLE001
            ok = False
        self.feed.on_state('stopped')
        return ok
