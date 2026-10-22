"""Entry-only MNQ/MES contract/session gate. No orders, controls or auto rollover.

Quarterly contracts come from app/contract-calendar.json. Session policy is a
sanitized port of the retained reviewed decrypt.market-session.v1 policy: 18ET
reopen, 17ET daily maintenance, weekends, and conservative whole-trade-date
holiday/early-close holds. Those holds are NOT assertions of CME full closure.
Strategy-owned sessions remain authoritative additional restrictions.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

DEFAULT = Path(__file__).resolve().parents[1] / 'contract-calendar.json'
NY = ZoneInfo('America/New_York')
SESSION_POLICY = {
    'schema': 'decrypt.market-session.v1', 'timezone': 'America/New_York',
    'coverage_start': '2026-01-01', 'coverage_end': '2027-12-31',
    'holiday_policy': 'hold_entire_trade_date_until_product_hours_reviewed',
    'holiday_trade_dates': (
        '2026-01-01','2026-01-19','2026-02-16','2026-04-03','2026-05-25','2026-06-19',
        '2026-07-03','2026-09-07','2026-11-26','2026-12-25',
        '2027-01-01','2027-01-18','2027-02-15','2027-03-26','2027-05-31','2027-06-18',
        '2027-07-05','2027-09-06','2027-11-25','2027-12-24'),
    'early_close_trade_dates': ('2026-11-27','2026-12-24','2027-11-26'),
    'source': 'https://www.cmegroup.com/education/courses/micro-e-mini-futures/micro-e-mini-futures-products-overview',
    'holiday_source': 'https://www.cmegroup.com/trading-hours.html',
    'basis': 'retained_reviewed_conservative_operator_policy_not_exchange_closure',
}


def _day(value):
    if type(value) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is None:
        raise ValueError('invalid_calendar_date')
    return date.fromisoformat(value)


def _symbol(value):
    return type(value) is str and re.fullmatch(r'MNQ[HMUZ]20\d\d', value) is not None


# Micro E-mini S&P 500 (MES) futures expire on the same quarterly third Fridays as Micro
# Nasdaq-100 (MNQ) and share CME's equity-index roll dates, so the reviewed MNQ rolls also name the
# current dated MES contract (same month and year, root swapped). Any other root holds entries.
SAME_ROLL_ROOTS = ('MNQ', 'MES')


def _binding_symbol(binding):
    root = binding.get('root') if type(binding) is dict else None
    symbol = binding.get('symbol') if type(binding) is dict else None
    return (root in SAME_ROLL_ROOTS and type(symbol) is str
            and re.fullmatch(root + r'[HMUZ]20\d\d', symbol) is not None)


def _next_contract(value):
    letters = 'HMUZ'
    index, year = letters.index(value[3]), int(value[4:])
    return 'MNQ' + letters[(index+1) % 4] + str(year + (index == 3))


def _read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError('calendar_invalid')
    raw = path.read_bytes()
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate_calendar_field')
            out[key] = value
        return out
    data = json.loads(raw, object_pairs_hook=unique)
    if (type(data) is not dict or data.get('schema') != 'decrypt.contract-calendar.v1'
            or data.get('product') != 'MNQ' or data.get('exchange') != 'CME'
            or data.get('automaticSwitchingImplemented') is not False
            or data.get('source') != 'https://www.cmegroup.com/trading/equity-index/rolldates.html'):
        raise ValueError('calendar_invalid')
    first, last = _day(data['verifiedOn']), _day(data['coverageEnds'])
    if first > last or type(data.get('rolls')) is not list or not 1 <= len(data['rolls']) <= 32:
        raise ValueError('calendar_invalid')
    previous = None
    for roll in data['rolls']:
        if type(roll) is not dict or set(roll) != {'from','to','rollDate','expirationDate'}:
            raise ValueError('calendar_invalid')
        if not _symbol(roll['from']) or not _symbol(roll['to']) or _next_contract(roll['from']) != roll['to']:
            raise ValueError('calendar_invalid')
        start, expiry = _day(roll['rollDate']), _day(roll['expirationDate'])
        # Validate supplied reviewed dates; do not manufacture a future roll.
        if start > expiry or (expiry-start).days > 7:
            raise ValueError('calendar_invalid')
        if expiry.year != int(roll['from'][4:]) or expiry.month != {'H':3,'M':6,'U':9,'Z':12}[roll['from'][3]]:
            raise ValueError('calendar_invalid')
        if previous and (roll['from'] != previous['to'] or start <= _day(previous['expirationDate'])):
            raise ValueError('calendar_invalid')
        previous = roll
    return data, hashlib.sha256(raw).hexdigest()


class CalendarGate:
    def __init__(self, calendar_path=DEFAULT):
        # Path is a trusted package/backend argument, never an HTTP parameter.
        self.path = Path(calendar_path)

    def status(self, binding, now):
        """now is epoch milliseconds; no entry eligibility implies arming."""
        result = {'schema':'decrypt.customer-calendar-gate.v1', 'state':'hold',
                  'reason':'calendar_unavailable', 'entryAllowed':False,
                  'managementAllowed':True, 'automaticSwitching':False,
                  'strategySessionUnchanged':True, 'timezone':'America/New_York',
                  'expectedSymbol':None, 'checkedAt':now if type(now) is int else None,
                  'validUntil':None, 'tradeDate':None, 'coverageEnds':None,
                  'calendarSha256':None, 'rollWindow':False,
                  'holidayBasis':SESSION_POLICY['basis']}
        try:
            if type(now) is not int or not 0 < now < 2**53:
                raise ValueError('clock_invalid')
            if not _binding_symbol(binding):
                result['reason'] = 'exact_dated_contract_required'
                return result
            profile, digest = _read(self.path)
            dt = datetime.fromtimestamp(now/1000, NY)
            minute = dt.hour*60 + dt.minute
            trade = dt.date() + timedelta(days=int(minute >= 1080))
            coverage_start = max(_day(profile['verifiedOn']), _day(SESSION_POLICY['coverage_start']))
            coverage_end = min(_day(profile['coverageEnds']), _day(SESSION_POLICY['coverage_end']))
            # A horizon extension alone cannot carry the last listed successor
            # indefinitely. Without its reviewed roll, hold before its expiry
            # month rather than inventing the next year's exchange roll date.
            successor = profile['rolls'][-1]['to']
            successor_month = {'H':3,'M':6,'U':9,'Z':12}[successor[3]]
            last_known = date(int(successor[4:]), successor_month, 1)-timedelta(days=1)
            coverage_end = min(coverage_end, last_known)
            result.update(tradeDate=trade.isoformat(), coverageEnds=coverage_end.isoformat(),
                          calendarSha256=digest, validUntil=(now//60000 + 1)*60000)
            if not coverage_start <= trade <= coverage_end:
                result['reason'] = 'calendar_outside_reviewed_horizon'
                return result
            root = binding['root']
            expected = root + profile['rolls'][0]['from'][3:]
            for roll in profile['rolls']:
                if trade >= _day(roll['rollDate']):
                    expected = root + roll['to'][3:]
                if _day(roll['rollDate']) <= trade <= _day(roll['expirationDate']):
                    result['rollWindow'] = True
            result['expectedSymbol'] = expected
            if binding['symbol'] != expected:
                result['reason'] = 'contract_roll_required' if any(binding['symbol'] == root + r['from'][3:] and trade >= _day(r['rollDate']) for r in profile['rolls']) else 'contract_not_current'
                return result
            if dt.weekday() == 5 or (dt.weekday() == 4 and minute >= 1020) or (dt.weekday() == 6 and minute < 1080):
                result.update(state='closed', reason='weekend')
            elif 1020 <= minute < 1080:
                result.update(state='closed', reason='daily_maintenance')
            elif trade.isoformat() in SESSION_POLICY['holiday_trade_dates'] + SESSION_POLICY['early_close_trade_dates']:
                result.update(reason='conservative_holiday_policy_hold')
            else:
                result.update(state='open', reason='reviewed_regular_session', entryAllowed=True)
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            # Corrupt/absent package data or an invalid clock never opens entries.
            result.update(state='hold', reason='calendar_unavailable', entryAllowed=False, validUntil=None)
        return result
