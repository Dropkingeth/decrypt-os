"""Grid alerts for the firing order: one compact monospace block per trade event.

Deterministic: the same inputs always give the same text. Example (illustrative values):

    MNQ  HH:MM  LONG 6 @ 10000.00
    ACCT  POS  ORDERS   STATE
    AA    6    TP1 4    OK
    AB    6    TP1 4    OK
    AC    6    TP1 4    OK

and, when everything is flat:

    MNQ  HH:MM  ALL FLAT
    AA 0 | AB 0 | AC 0   OK

HH:MM is the event time in the display time zone (TVBRIDGE_DISPLAY_TZ).

STATE: OK, MISMATCH (position or working orders differ from the leader, or the follower is
not pinned), LOGGED OUT (page not authorized or state not synced), KILLED (kill switch
present), SKIPPED (the follower did not take this trade's entry). One ATTENTION block is
sent when MISMATCH, LOGGED OUT or KILLED persists.

1.1.0 multi-login: when the firing order spans more than one Tradovate login, rows are grouped
by login under a header line `<id>: <label>  <CONNECTED | LOGGED OUT | UNREACHABLE>`, primary
first, and every row of an unreachable login reads UNREACHABLE. With one login the text is
exactly the single table above.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .mirror import soup_sequence

STATES = ('OK', 'MISMATCH', 'LOGGED OUT', 'KILLED', 'SKIPPED', 'UNREACHABLE')
ATTENTION_STATES = ('MISMATCH', 'LOGGED OUT', 'KILLED', 'UNREACHABLE')
LOGIN_STATES = ('CONNECTED', 'LOGGED OUT', 'UNREACHABLE')
# Display time zone of the grid lines (TVBRIDGE_DISPLAY_TZ, default New York market time).
TZ = ZoneInfo(os.environ.get('TVBRIDGE_DISPLAY_TZ') or 'America/New_York')
EXIT_KINDS = ('reduce', 'flatten')


def label(key):
    """'account-7' -> 'A7'; 'account-b' -> 'AB'; 'main' has no digits -> 'MAIN'."""
    digits = ''.join(ch for ch in str(key) if ch.isdigit())
    if digits:
        return str(key)[:1].upper() + digits
    letter = re.fullmatch(r'[A-Za-z]+-([A-Za-z])', str(key))
    return str(key)[:1].upper() + letter[1].upper() if letter else str(key).upper()[:4]


def hhmm(at_ms):
    return datetime.fromtimestamp(at_ms / 1000, TZ).strftime('%H:%M')


def price_text(price):
    return None if price is None else f'{float(price):.2f}'


def pos_text(net):
    return '?' if net is None else str(int(net))


def orders_text(orders):
    if not orders:
        return '-'
    parts = []
    for o in sorted(orders, key=lambda o: (not o.get('tp1'), str(o.get('type')), str(o.get('id')))):
        name = 'TP1' if o.get('tp1') else (o.get('type') or 'ORD')
        parts.append(f'{name} {"?" if o.get("qty") is None else o["qty"]}')
    return ','.join(parts)


def orders_signature(orders):
    return sorted((bool(o.get('tp1')), str(o.get('type')), o.get('qty')) for o in orders or [])


def table(rows):
    """rows: [(label, pos text, orders text, state)] -> header plus one line per account."""
    return grouped_table([(None, rows)])


def login_header(login_id, label_text, state):
    return f'{login_id}: {label_text}  {state}'


def grouped_table(groups):
    """groups: [(login header line or None, rows)] -> one column header, then each header and its rows.
    Column widths span every group, so the accounts of all logins line up."""
    rows = [r for _, rs in groups for r in rs]
    w0 = max([6] + [len(r[0]) + 2 for r in rows])
    w1 = max([5] + [len(r[1]) + 2 for r in rows])
    w2 = max([9] + [len(r[2]) + 2 for r in rows])
    lines = ['ACCT'.ljust(w0) + 'POS'.ljust(w1) + 'ORDERS'.ljust(w2) + 'STATE']
    for header, rs in groups:
        if header is not None:
            lines.append(header)
        lines += [r[0].ljust(w0) + r[1].ljust(w1) + r[2].ljust(w2) + r[3] for r in rs]
    return '\n'.join(lines)


def render(root, at_ms, title, rows, groups=None):
    return f'{root}  {hhmm(at_ms)}  {title}\n' + (grouped_table(groups) if groups else table(rows))


def render_flat(root, at_ms, rows, groups=None):
    worst = next((r[3] for r in rows if r[3] != 'OK'), 'OK')
    if not groups:
        return f'{root}  {hhmm(at_ms)}  ALL FLAT\n' + ' | '.join(f'{r[0]} {r[1]}' for r in rows) + '   ' + worst
    lines = []
    for header, rs in groups:
        lines += [header, ' | '.join(f'{r[0]} {r[1]}' for r in rs)]
    return f'{root}  {hhmm(at_ms)}  ALL FLAT\n' + '\n'.join(lines) + '   ' + worst


def event_title(kind, side=None, quantity=None, price=None):
    at = price_text(price)
    tail = '' if at is None else ' @ ' + at
    if kind == 'entry':
        return f'{str(side).upper()} {quantity}{tail}'
    if kind == 'tp1':
        return f'TP1 {quantity}{tail}'
    if kind == 'exit':
        return f'EXIT {quantity}{tail}'
    return kind.upper()


def account_rows(report, keys, skipped=(), unavailable=None):
    """Grid rows for the main account plus `keys` (followers, in firing order).

    report: the daemon's GET /followers response (with its `main` row).
    skipped: follower keys that did not take the current trade's entry.
    unavailable: {key: reason} for followers the service refused to run.
    Returns [(label, pos, orders, state)].
    """
    main = report.get('main') or {}
    live = {r.get('key'): r for r in report.get('followers') or []}
    rows = []

    def base_state(r):
        if not r.get('available', True):
            return 'MISMATCH'
        if not r.get('logged_in'):
            return 'LOGGED OUT'
        if r.get('entries_disabled'):
            return 'KILLED'
        return None

    rows.append((label(report.get('main_key') or 'main'), pos_text(main.get('net_position')),
                 orders_text(main.get('orders')), base_state(main) or 'OK'))
    lead = (main.get('net_position'), orders_signature(main.get('orders')))
    for key in keys:
        r = live.get(key) or {'available': False}
        if unavailable and key in unavailable:
            r = {'available': False}
        state = base_state(r)
        if state is None:
            if key in skipped:
                state = 'SKIPPED'
            elif (r.get('net_position'), orders_signature(r.get('orders'))) != lead:
                state = 'MISMATCH'
            else:
                state = 'OK'
        rows.append((label(key), pos_text(r.get('net_position')), orders_text(r.get('orders')), state))
    return rows


def login_groups(rows, keys, logins, states):
    """1.1.0: split account_rows() output (main first, then `keys`) by login.

    logins: [(login id, label, [follower keys on that login])], primary first; the main row belongs
    to the primary. states: {login id: CONNECTED | LOGGED OUT | UNREACHABLE} from the merged report.
    Every row of an UNREACHABLE login reads UNREACHABLE. Returns [(header line, rows)].
    """
    by_key = dict(zip(keys, rows[1:]))
    groups = []
    for i, (login_id, login_label, login_keys) in enumerate(logins):
        state = states.get(login_id) or 'UNREACHABLE'
        rs = ([rows[0]] if i == 0 else []) + [by_key[k] for k in login_keys if k in by_key]
        if state == 'UNREACHABLE':
            rs = [(r[0], '?', '-', 'UNREACHABLE') for r in rs]
        groups.append((login_header(login_id, login_label, state), rs))
    return groups


class GridTracker:
    """Turns successive (Soup trade, daemon report) observations into grid events.

    Events per Soup trade, each sent once: entry (the leader's entry completely filled), tp1
    (the leader's TP1 filled), exit (each leader reduce/flatten filled), flat (Soup closed the
    trade and every account is flat with nothing working). An event waits up to `settle_ms`
    for the followers to match, then is sent with whatever the grid shows. ATTENTION is sent
    once per distinct problem set that persists `attention_ms` (default 30 s, longer than the
    15 s end-of-trade reconciliation grace) with no event waiting.
    """

    def __init__(self, root, main_key, keys, settle_ms=2000, attention_ms=30000, logins=None):
        self.root, self.main_key, self.keys = root, main_key, list(keys)
        self.settle_ms, self.attention_ms = settle_ms, attention_ms
        # 1.1.0: [(login id, label, [keys])], primary first; grouped only when more than one login has rows.
        self.logins = list(logins) if logins and len(logins) > 1 else None
        self.first_seen = {}
        self.emitted = set()
        self.seq = 0
        self.last = None
        self.recent = []
        self.attention_since = None
        self.attention_sent = None
        self.now_text = None
        self.baselined = False

    def _emit(self, key, kind, trade_id, at_ms, text, now_ms):
        self.seq += 1
        self.emitted.add(key)
        self.last = {'seq': self.seq, 'key': key, 'kind': kind, 'trade_id': trade_id, 'at': now_ms,
                     'event_at': at_ms, 'text': text}
        self.recent = (self.recent + [self.last])[-8:]
        return self.last

    def candidates(self, trade, commands, report):
        """[(event key, kind, event time ms or None, title or None)] whose leader condition holds now."""
        if not trade:
            return []
        tid, side = trade['id'], trade.get('side')
        fills = {f['command_id']: f for f in (report.get('main') or {}).get('trade') or []}
        out = []
        ordered = sorted((commands or {}).values(), key=lambda c: (c.get('started_at') or 0, soup_sequence(trade, c)))
        for c in ordered:
            f = fills.get(c['id'])
            if not f or not f.get('filled') or f['filled'] < (f.get('quantity') or c.get('quantity') or 0):
                continue
            kind = {'entry': 'entry', 'tp1_limit': 'tp1'}.get(c['kind'], 'exit' if c['kind'] in EXIT_KINDS else None)
            if kind is None:
                continue
            title = event_title(kind, side, f['filled'], f.get('price'))
            out.append((f'{tid}:{kind}:{c["id"]}', kind, f.get('fill_ms'), title))
        if trade.get('phase') == 'closed':
            out.append((f'{tid}:flat', 'flat', None, None))
        return out

    def update(self, trade, commands, report, now_ms, skipped=(), unavailable=None):
        """Returns the list of events emitted by this observation (usually empty)."""
        rows = account_rows(dict(report, main_key=self.main_key), self.keys, skipped, unavailable)
        groups = None
        if self.logins is not None:
            states = {k: (v or {}).get('state') for k, v in (report.get('logins') or {}).items()}
            groups = login_groups(rows, self.keys, self.logins, states)
            rows = [r for _, rs in groups for r in rs]
        self.now_text = grouped_table(groups) if groups else table(rows)
        all_ok = all(r[3] in ('OK', 'SKIPPED') for r in rows)
        flat = all(r[1] == '0' and r[2] == '-' for r in rows)
        events, waiting = [], False
        found = self.candidates(trade, commands, report)
        if not self.baselined:
            # Events that already happened before this process started are not re-sent.
            self.emitted.update(key for key, kind, _, _ in found if kind != 'flat' or flat)
            self.baselined = True
        for key, kind, at_ms, title in found:
            if key in self.emitted:
                continue
            if kind == 'flat' and not flat:
                continue  # "ALL FLAT" only when it is true; a leftover becomes ATTENTION after attention_ms
            first = self.first_seen.setdefault(key, now_ms)
            if not all_ok and now_ms - first < self.settle_ms:
                waiting = True
                break  # keep events in order
            at = at_ms or now_ms
            text = render_flat(self.root, at, rows, groups) if kind == 'flat' else \
                render(self.root, at, title, rows, groups)
            events.append(self._emit(key, kind, trade['id'], at, text, now_ms))
        problems = tuple((r[0], r[3]) for r in rows if r[3] in ATTENTION_STATES)
        if not problems:
            self.attention_since, self.attention_sent = None, None
        elif not waiting:
            if self.attention_since is None or self.attention_since[0] != problems:
                self.attention_since = (problems, now_ms)
            if now_ms - self.attention_since[1] >= self.attention_ms and self.attention_sent != problems:
                self.attention_sent = problems
                text = render(self.root, now_ms, 'ATTENTION', rows, groups)
                events.append(self._emit('attention:' + ','.join(f'{a}={s}' for a, s in problems) + f':{now_ms}',
                                         'attention', trade['id'] if trade else None, now_ms, text, now_ms))
        return events

    def status(self):
        return {'seq': self.seq, 'last': self.last, 'recent': list(self.recent), 'now': self.now_text}
