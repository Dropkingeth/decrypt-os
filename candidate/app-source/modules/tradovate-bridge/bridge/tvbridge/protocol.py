"""Pure Tradovate data helpers: symbols, prices, statuses and timestamps."""
from __future__ import annotations

import calendar
import hmac
import re
from decimal import Decimal

MONTH_CODES = 'FGHJKMNQUVXZ'

# Soup/broker-contract order statuses.
WORKING = 'working'
PARTIAL = 'partial'
FILLED = 'filled'
CANCELLED = 'cancelled'
REJECTED = 'rejected'
TERMINAL = (FILLED, CANCELLED, REJECTED)

# Tradovate ordStatus values that still rest at the exchange or are in flight.
_OPEN_STATUSES = {'PendingNew', 'Working', 'PendingReplace', 'PendingCancel', 'Suspended'}
_CANCELLED = {'Canceled', 'Cancelled', 'Expired'}


def tradovate_symbol(soup_ticker):
    """MNQZ2021 -> MNQZ1. Tradovate uses a single-digit year."""
    m = re.fullmatch(r'([A-Z0-9]{1,5}?)([FGHJKMNQUVXZ])(20\d\d)', soup_ticker or '')
    if not m:
        raise ValueError(f'unsupported ticker {soup_ticker!r}')
    return m.group(1) + m.group(2) + m.group(3)[-1]


_OUTRIGHT = re.compile(r'([A-Z0-9]{1,5}?)([FGHJKMNQUVXZ])(\d|\d\d|20\d\d)')


def product_root(contract_name):
    """Product root of an outright futures name: MNQZ1, MNQZ21 and MNQZ2021 -> MNQ; M2KZ1 -> M2K.

    Returns None for anything else (options, spreads, unknown formats), so callers fail closed.
    """
    m = _OUTRIGHT.fullmatch(contract_name) if isinstance(contract_name, str) else None
    return m.group(1) if m else None


def on_tick(price, tick_size):
    if type(price) not in (int, float) or price != price or price in (float('inf'), float('-inf')):
        return False
    d = Decimal(str(price))
    t = Decimal(str(tick_size))
    return d > 0 and d % t == 0


_ISO = re.compile(r'(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?')


def to_ms(ts):
    """Tradovate ISO timestamp -> integer Unix milliseconds (integer math, any fraction length)."""
    if type(ts) is int:
        return ts
    if not isinstance(ts, str):
        raise ValueError('timestamp required')
    m = _ISO.fullmatch(ts.strip())
    if not m:
        raise ValueError(f'unsupported timestamp {ts!r}')
    y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
    ms = int((m.group(7) or '0')[:3].ljust(3, '0'))
    seconds = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    zone = m.group(8)
    if zone and zone != 'Z':
        sign = -1 if zone[0] == '-' else 1
        hh, mm = int(zone[1:3]), int(zone[-2:])
        seconds -= sign * (hh * 3600 + mm * 60)
    return seconds * 1000 + ms


def is_open(ord_status):
    return ord_status in _OPEN_STATUSES


def soup_status(ord_status, filled_qty):
    """Map Tradovate ordStatus + cumulative fills to the Soup broker contract status.

    Returns None for a status we do not understand, so callers fail closed.
    """
    if ord_status == 'Filled':
        return FILLED
    if ord_status in _CANCELLED:
        return CANCELLED
    if ord_status == 'Rejected':
        return REJECTED
    if ord_status in _OPEN_STATUSES:
        return PARTIAL if filled_qty > 0 else WORKING
    return None


# Request-level HTTP-style statuses that mean the request was refused before any order action.
DEFINITE_REFUSAL_STATUSES = {400, 401, 403, 404}


def exit_action(side):
    return 'Sell' if side == 'long' else 'Buy'


def entry_action(side):
    return 'Buy' if side == 'long' else 'Sell'


def side_from_trade_id(trade_id):
    if isinstance(trade_id, str):
        if trade_id.endswith(':long'):
            return 'long'
        if trade_id.endswith(':short'):
            return 'short'
    return None


def session_start_ms(now_ms):
    """Start of the CME Globex session containing now: the latest 18:00 America/New_York."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    ny = ZoneInfo('America/New_York')
    now = datetime.fromtimestamp(now_ms / 1000, ny)
    start = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if start > now:
        start = (now - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def same_secret(given, secret):
    """Constant-time comparison of a path token with a secret, as bytes: a non-ASCII path (str
    compare_digest raises TypeError on it) is simply a mismatch."""
    if not isinstance(given, str) or not isinstance(secret, str):
        return False
    return hmac.compare_digest(given.encode('utf-8', 'surrogateescape'), secret.encode('utf-8', 'surrogateescape'))
