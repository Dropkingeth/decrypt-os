"""Account state rebuilt from the Tradovate sync snapshot and props events."""
from __future__ import annotations

import logging
import threading
import time
import copy
from collections import OrderedDict

from . import protocol

log = logging.getLogger('tvbridge')

# Entity collections we track, keyed by the sync snapshot's collection names.
_COLLECTIONS = {
    'accounts': 'account',
    'orders': 'order',
    'fills': 'fill',
    'positions': 'position',
    'orderVersions': 'orderVersion',
    'contracts': 'contract',
}

# Read-only account data (1.0.6), for the balance/risk view only. Kept apart from the
# trading entities above: never validated as trading inventory, never required for a
# sync, never used by an order guard. A missing or malformed row makes the view
# incomplete, never the account unsynced. Tradovate keys the last three by account id.
_ACCOUNT_DATA = {
    'cashBalances': 'cashBalance',
    'accountRiskStatuses': 'accountRiskStatus',
    'userAccountAutoLiqs': 'userAccountAutoLiq',
    'marginSnapshots': 'marginSnapshot',
}
ACCOUNT_DATA_KINDS = frozenset(_ACCOUNT_DATA.values())

# 1.0.9.5.2: Tradovate's own rejection text for an order, observability only. Tradovate sends
# it on entities the bridge's socket already receives (sync collections and props events) and
# 1.0.9.5.1 dropped: a `commandReport` (commandStatus RiskRejected/ExecutionRejected, rejectReason,
# text) for the order's New `command` (a command's id equals its order's id), or an
# `executionReport` with execType/ordStatus Rejected. Kept apart from the trading entities: never
# validated, never required, never bumps `version`, never fails a sync or an update.
_REPORT_COLLECTIONS = {'commands': 'command', 'commandReports': 'commandReport',
                       'executionReports': 'executionReport'}
REPORT_KINDS = frozenset(_REPORT_COLLECTIONS.values())
REJECT_REASON_MAX = 300
_REPORTS_MAX = 2000  # per map; oldest dropped first
_REJECTED_COMMAND = frozenset(('RiskRejected', 'ExecutionRejected'))
_NOT_A_REASON = frozenset(('', 'Success'))


def _clean_text(value):
    if not isinstance(value, str):
        return ''
    value = ''.join(ch if ch.isprintable() else ' ' for ch in value[:2000])
    return ' '.join(value.split())


def reject_text(reject_reason, text):
    """Bounded, single-line rejection text from Tradovate's rejectReason code and free text, or None.

    "RiskLimitExceeded: Order rejected by ..." / the text alone when the code is
    UnknownReason / the code alone without text. At most REJECT_REASON_MAX characters.
    """
    code, words = _clean_text(reject_reason), _clean_text(text)
    if code in _NOT_A_REASON:
        code = ''
    if code == 'UnknownReason' and words:
        code = ''
    if code and words and code != words:
        out = code + ': ' + words
    else:
        out = words or code
    if not out:
        return None
    return out if len(out) <= REJECT_REASON_MAX else out[:REJECT_REASON_MAX - 3].rstrip() + '...'


def _positive(value):
    return type(value) is int and value > 0


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class AccountState:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.entities = {kind: {} for kind in _COLLECTIONS.values()}
        self.sync_epoch = None
        self.last_event_ms = None
        self.terminal_seen_ms = {}  # order id -> local time its terminal status arrived
        self.anomalies = []
        self.version = 0
        self.account_data = {kind: {} for kind in ACCOUNT_DATA_KINDS}
        self.account_data_at = None  # local ms of the latest account-data row (sync or props)
        self._new_reports()
        self._reject_logged = OrderedDict()  # order ids whose rejection text was logged (props only)

    @property
    def synced(self):
        return self.sync_epoch is not None

    def _now_ms(self):
        return int(self.clock() * 1000)

    def _touch(self):
        self.version += 1
        self.last_event_ms = self._now_ms()
        self.changed.notify_all()

    def _note_terminal(self, order):
        if not protocol.is_open(order.get('ordStatus')):
            self.terminal_seen_ms.setdefault(order['id'], self._now_ms())
        else:
            self.terminal_seen_ms.pop(order['id'], None)

    def load_sync(self, accounts, snapshot, epoch):
        """Validate a complete snapshot, then replace state atomically.

        Missing inventory is unknown, not an empty account. A failed sync
        preserves the previous entities for diagnosis but revokes their epoch.
        """
        with self.lock:
            self.sync_epoch = None
            try:
                if not isinstance(epoch, str) or not epoch or not isinstance(snapshot, dict):
                    raise ValueError('invalid sync envelope')
                if not isinstance(accounts, list):
                    raise ValueError('invalid account list')
                fresh = {kind: {} for kind in _COLLECTIONS.values()}
                account_ids = set()
                for account in accounts:
                    self._validate_entity('account', account)
                    if account['id'] in account_ids:
                        raise ValueError('duplicate account')
                    account_ids.add(account['id'])
                for name, kind in _COLLECTIONS.items():
                    # Contracts may be resolved separately with contract/find.
                    rows = snapshot.get(name, [] if name == 'contracts' else None)
                    if not isinstance(rows, list):
                        raise ValueError('missing or invalid sync collection')
                    for item in rows:
                        self._validate_entity(kind, item)
                        if item['id'] in fresh[kind]:
                            raise ValueError('duplicate sync entity')
                        fresh[kind][item['id']] = copy.deepcopy(item)
                if set(fresh['account']) != account_ids:
                    raise ValueError('account lists disagree')
            except Exception:
                self._touch()
                raise
            self.entities = fresh
            self.terminal_seen_ms = {}
            for order in self.entities['order'].values():
                self._note_terminal(order)
            self._load_account_data(snapshot)
            self._load_reports(snapshot)
            self.sync_epoch = epoch
            self._touch()

    def _load_account_data(self, snapshot):
        """Replace the read-only account data from a validated sync. Never raises."""
        data = {kind: {} for kind in ACCOUNT_DATA_KINDS}
        for name, kind in _ACCOUNT_DATA.items():
            rows = snapshot.get(name)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and type(row.get('id')) is int and row['id'] > 0:
                    data[kind][row['id']] = copy.deepcopy(row)
        self.account_data = data
        self.account_data_at = self._now_ms()

    # --- 1.0.9.5.2: rejection text (observability only) ----------------------------------

    def _new_reports(self):
        # command id -> (order id, commandType); command id -> (report id, text); order id -> (report id, text)
        self._commands = OrderedDict()
        self._command_rejects = OrderedDict()
        self._execution_rejects = OrderedDict()

    @staticmethod
    def _bounded_put(table, key, value):
        table[key] = value
        table.move_to_end(key)
        while len(table) > _REPORTS_MAX:
            table.popitem(last=False)

    def _note_report(self, kind, entity):
        """Remember one command/report row if it names a rejection. Returns the order id it may
        explain (or None). Never raises."""
        try:
            if not isinstance(entity, dict) or not _positive(entity.get('id')):
                return None
            if kind == 'command':
                if _positive(entity.get('orderId')):
                    self._bounded_put(self._commands, entity['id'], (entity['orderId'], entity.get('commandType')))
                    if entity['id'] in self._command_rejects:
                        return entity['orderId']
                return None
            if kind == 'commandReport':
                if not _positive(entity.get('commandId')):
                    return None
                if entity.get('commandStatus') not in _REJECTED_COMMAND and \
                        _clean_text(entity.get('rejectReason')) in _NOT_A_REASON:
                    return None
                text = reject_text(entity.get('rejectReason'), entity.get('text'))
                if text is None:
                    return None
                self._bounded_put(self._command_rejects, entity['commandId'], (entity['id'], text))
                command = self._commands.get(entity['commandId'])
                return command[0] if command else entity['commandId']
            if kind == 'executionReport':
                if not _positive(entity.get('orderId')) or \
                        'Rejected' not in (entity.get('execType'), entity.get('ordStatus')):
                    return None
                text = reject_text(entity.get('rejectReason'), entity.get('text'))
                if text is None:
                    return None
                self._bounded_put(self._execution_rejects, entity['orderId'], (entity['id'], text))
                return entity['orderId']
        except Exception:  # noqa: BLE001 - observability must never disturb trading state
            return None
        return None

    def _load_reports(self, snapshot):
        """Rebuild the rejection text from a validated sync. Never raises; a bad row is skipped."""
        self._new_reports()
        for name, kind in _REPORT_COLLECTIONS.items():
            rows = snapshot.get(name)
            if not isinstance(rows, list):
                continue
            for row in rows[-_REPORTS_MAX * 4:]:
                self._note_report(kind, row)

    def _log_reject(self, order_id):
        """One INFO line per order seen Rejected live (props) once its text is known."""
        try:
            if not _positive(order_id) or order_id in self._reject_logged or \
                    (self.entities['order'].get(order_id) or {}).get('ordStatus') != 'Rejected':
                return
            text = self.reject_reason(order_id)
            if text is None:
                return
            self._bounded_put(self._reject_logged, order_id, True)
            order = self.entities['order'].get(order_id) or {}
            log.info('Tradovate rejected order %s (account %s): %s', order_id, order.get('accountId', '?'), text)
        except Exception:  # noqa: BLE001
            pass

    def reject_reason(self, order_id):
        """Tradovate's rejection text for an order (bounded, single line), or None when unknown.

        Callers use it only for an order whose ordStatus is Rejected. The latest matching report
        wins: an executionReport of the order, or a commandReport of its New command.
        """
        with self.lock:
            found = [self._execution_rejects.get(order_id)]
            commands = [cid for cid, (oid, ctype) in self._commands.items()
                        if oid == order_id and ctype in (None, 'New')]
            if order_id not in self._commands:
                commands.append(order_id)  # a New command's id is its order's id
            found += [self._command_rejects.get(cid) for cid in commands]
            found = [f for f in found if f]
            return max(found)[1] if found else None

    @staticmethod
    def _validate_entity(kind, item):
        def positive(value):
            return type(value) is int and value > 0
        if not isinstance(item, dict) or not positive(item.get('id')):
            raise ValueError('invalid entity identity')
        keys = {'position': ('accountId', 'contractId'),
                'order': ('accountId', 'contractId'),
                'fill': ('orderId',), 'orderVersion': ('orderId',)}
        if any(not positive(item.get(key)) for key in keys.get(kind, ())):
            raise ValueError('invalid entity relationship')
        if kind == 'position':
            if type(item.get('netPos')) is not int:
                raise ValueError('invalid position quantity')
            date = item.get('tradeDate')
            if not isinstance(date, dict) or any(not positive(date.get(k)) for k in ('year', 'month', 'day')):
                raise ValueError('invalid position trade date')
        if kind == 'order' and (not isinstance(item.get('ordStatus'), str) or
                                protocol.soup_status(item['ordStatus'], 0) is None):
            raise ValueError('unsupported order status')
        if kind in ('fill', 'orderVersion'):
            field = 'qty' if kind == 'fill' else 'orderQty'
            if not positive(item.get(field)):
                raise ValueError('invalid order or fill quantity')

    def apply_props(self, d):
        with self.lock:
            if not isinstance(d, dict):
                self.mark_unsynced()
                raise ValueError('invalid entity update')
            kind = d.get('entityType')
            entity = d.get('entity')
            if isinstance(kind, str) and kind in ACCOUNT_DATA_KINDS:
                # View-only data: ignore a malformed row; never touch trading state or version.
                if isinstance(entity, dict) and type(entity.get('id')) is int and entity['id'] > 0:
                    if d.get('eventType') == 'Deleted':
                        self.account_data[kind].pop(entity['id'], None)
                    else:
                        self.account_data[kind][entity['id']] = copy.deepcopy(entity)
                    self.account_data_at = self._now_ms()
                return
            if isinstance(kind, str) and kind in REPORT_KINDS:
                # 1.0.9.5.2, observability only: as before, no validation, no version change.
                if d.get('eventType') != 'Deleted':
                    self._log_reject(self._note_report(kind, entity))
                return
            if not isinstance(kind, str) or kind not in self.entities:
                return
            try:
                if d.get('eventType') == 'Deleted':
                    if not isinstance(entity, dict) or type(entity.get('id')) is not int or entity['id'] <= 0:
                        raise ValueError('invalid deleted entity')
                else:
                    self._validate_entity(kind, entity)
            except (TypeError, ValueError, KeyError):
                self.mark_unsynced()
                raise
            if d.get('eventType') == 'Deleted':
                if kind == 'fill':
                    # A busted/corrected fill. Keep a record so fills never silently shrink.
                    self.anomalies.append({'at': self._now_ms(), 'kind': 'fill_deleted', 'fill_id': entity['id']})
                self.entities[kind].pop(entity['id'], None)
            else:
                self.entities[kind][entity['id']] = copy.deepcopy(entity)
                if kind == 'order':
                    self._note_terminal(entity)
                    if entity.get('ordStatus') == 'Rejected':
                        self._log_reject(entity['id'])  # 1.0.9.5.2: logs only; never raises
            self._touch()

    def mark_unsynced(self):
        with self.lock:
            self.sync_epoch = None
            self._touch()

    def is_synced_for(self, epoch):
        with self.lock:
            return epoch is not None and self.sync_epoch == epoch

    # --- queries (callers may hold self.lock for a consistent multi-query read) ---

    def account_by_name(self, name):
        with self.lock:
            for a in self.entities['account'].values():
                if a.get('name') == name:
                    return a
        return None

    def has_account(self, account_id):
        with self.lock:
            return account_id in self.entities['account']

    def contract_id(self, symbol):
        with self.lock:
            for c in self.entities['contract'].values():
                if c.get('name') == symbol:
                    return c['id']
        return None

    def add_contract(self, contract):
        with self.lock:
            self.entities['contract'][contract['id']] = contract

    @staticmethod
    def _position_order(p):
        d = p.get('tradeDate') or {}
        return (d.get('year', 0), d.get('month', 0), d.get('day', 0), p.get('timestamp') or '', p['id'])

    def net_position(self, account_id, contract_id):
        """Net position from the latest trade-date position entity (one entity per trade date)."""
        with self.lock:
            rows = [p for p in self.entities['position'].values()
                    if p.get('accountId') == account_id and p.get('contractId') == contract_id]
            if not rows:
                return 0
            return int(max(rows, key=self._position_order).get('netPos') or 0)

    def net_positions(self):
        """{(accountId, contractId): netPos} from the latest trade-date entity, for every account in the login."""
        with self.lock:
            latest = {}
            for p in self.entities['position'].values():
                key = (p.get('accountId'), p.get('contractId'))
                if key not in latest or self._position_order(p) > self._position_order(latest[key]):
                    latest[key] = p
            return {key: int(p.get('netPos') or 0) for key, p in latest.items()}

    def working_orders(self):
        """Copies of every non-terminal order, across all accounts in the login."""
        with self.lock:
            return [dict(o) for o in self.entities['order'].values() if protocol.is_open(o.get('ordStatus'))]

    def contract_name(self, contract_id):
        with self.lock:
            c = self.entities['contract'].get(contract_id)
            return c.get('name') if c else None

    def fills_for(self, order_id):
        with self.lock:
            return [f for f in self.entities['fill'].values()
                    if f.get('orderId') == order_id and f.get('active', True) is not False]

    def filled_qty(self, order_id):
        return sum(int(f.get('qty') or 0) for f in self.fills_for(order_id))

    def avg_fill_price(self, order_id):
        """Quantity-weighted fill price, or None when no priced fill is known."""
        fills = [f for f in self.fills_for(order_id) if isinstance(f.get('price'), (int, float)) and f.get('qty')]
        qty = sum(int(f['qty']) for f in fills)
        return sum(float(f['price']) * int(f['qty']) for f in fills) / qty if qty else None

    def last_fill_ms(self, order_id):
        fills = self.fills_for(order_id)
        return max(protocol.to_ms(f['timestamp']) for f in fills) if fills else None

    def order(self, order_id):
        with self.lock:
            return self.entities['order'].get(order_id)

    def order_qty(self, order_id):
        """Latest known order quantity from orderVersion, if any."""
        with self.lock:
            versions = [v for v in self.entities['orderVersion'].values() if v.get('orderId') == order_id]
            if not versions:
                return None
            return int(max(versions, key=lambda v: v['id']).get('orderQty') or 0)

    def order_version(self, order_id):
        """Latest orderVersion of an order (a modify adds a version with a new id), or None."""
        with self.lock:
            versions = [v for v in self.entities['orderVersion'].values() if v.get('orderId') == order_id]
            return dict(max(versions, key=lambda v: v['id'])) if versions else None

    def has_version_text(self, order_id, text):
        """True when any version of the order carries `text` (a modify's client tag)."""
        with self.lock:
            return any(v.get('orderId') == order_id and v.get('text') == text
                       for v in self.entities['orderVersion'].values())

    def last_fill(self, contract_id):
        """The most recent priced fill on a contract, in any account of the login (a copy), or None."""
        with self.lock:
            fills = [f for f in self.entities['fill'].values()
                     if f.get('contractId') == contract_id and f.get('active', True) is not False
                     and isinstance(f.get('price'), (int, float)) and not isinstance(f.get('price'), bool)]
            if not fills:
                return None
            return dict(max(fills, key=lambda f: (str(f.get('timestamp') or ''), f['id'])))

    def last_fill_price(self, contract_id):
        """Price of the most recent priced fill on a contract, in any account of the login, or None."""
        fill = self.last_fill(contract_id)
        return None if fill is None else float(fill['price'])

    def max_order_id(self, account_id=None):
        """Highest known order id, of one account when `account_id` is given.

        1.0.9.1: Tradovate allocates order ids per account (accounts of one login can sit in
        quite different id ranges within the same minutes), so ids are only ordered within one
        account. A login-wide maximum is not a valid baseline: a liquidation baseline must be
        the account's own.
        """
        with self.lock:
            if account_id is None:
                return max(self.entities['order'], default=0)
            return max((oid for oid, o in self.entities['order'].items() if o.get('accountId') == account_id),
                       default=0)

    def order_id_for_tag(self, tag):
        """Find the order carrying our client tag in its `text` (order or orderVersion)."""
        with self.lock:
            for o in self.entities['order'].values():
                if o.get('text') == tag:
                    return o['id']
            for v in self.entities['orderVersion'].values():
                if v.get('text') == tag and v.get('orderId') is not None:
                    return v['orderId']
        return None

    def terminal_age_ms(self, order_id):
        with self.lock:
            seen = self.terminal_seen_ms.get(order_id)
            return None if seen is None else self._now_ms() - seen

    def open_orders(self, account_id, contract_id):
        with self.lock:
            return sorted(o['id'] for o in self.entities['order'].values()
                          if o.get('accountId') == account_id and o.get('contractId') == contract_id
                          and protocol.is_open(o.get('ordStatus')))

    def account_view(self, account_id):
        """Read-only balance/risk data for one account, as Tradovate reported it.

        Numbers are passed through unchanged (no open P&L is computed). Each part is
        None when Tradovate has not reported it; `cash_balance` None makes the view
        incomplete. Several cash rows (trade dates) resolve to the latest one.
        """
        with self.lock:
            cash_rows = [r for r in self.account_data['cashBalance'].values() if r.get('accountId') == account_id]
            cash = None
            if cash_rows:
                row = max(cash_rows, key=lambda r: (*(((r.get('tradeDate') or {}).get(k) or 0)
                                                      for k in ('year', 'month', 'day')),
                                                    str(r.get('timestamp') or ''), r['id']))
                date = row.get('tradeDate')
                cash = {'amount': _number(row.get('amount')), 'amount_sod': _number(row.get('amountSOD')),
                        'realized_pnl': _number(row.get('realizedPnL')),
                        'week_realized_pnl': _number(row.get('weekRealizedPnL')),
                        'trade_date': ({k: date.get(k) for k in ('year', 'month', 'day')}
                                       if isinstance(date, dict) else None),
                        'timestamp': row.get('timestamp') if isinstance(row.get('timestamp'), str) else None}
                if cash['amount'] is None:
                    cash = None
            risk = self.account_data['accountRiskStatus'].get(account_id)
            auto = self.account_data['userAccountAutoLiq'].get(account_id)
            margin = self.account_data['marginSnapshot'].get(account_id)

            def text(value):
                return value if isinstance(value, str) else None
            return {
                'cash_balance': cash,
                'risk_status': None if risk is None else {
                    'max_net_liq': _number(risk.get('maxNetLiq')), 'min_net_liq': _number(risk.get('minNetLiq')),
                    'max_net_liq_timestamp': text(risk.get('maxNetLiqTimestamp')),
                    'min_net_liq_timestamp': text(risk.get('minNetLiqTimestamp'))},
                'auto_liq': None if auto is None else {
                    'trailing_max_drawdown': _number(auto.get('trailingMaxDrawdown')),
                    'trailing_max_drawdown_limit': _number(auto.get('trailingMaxDrawdownLimit')),
                    'trailing_max_drawdown_mode': text(auto.get('trailingMaxDrawdownMode'))},
                'margin': None if margin is None else {
                    'initial_margin': _number(margin.get('initialMargin')),
                    'maintenance_margin': _number(margin.get('maintenanceMargin')),
                    'auto_liq_level': _number(margin.get('autoLiqLevel')),
                    'liq_only_level': _number(margin.get('liqOnlyLevel')),
                    'total_used_margin': _number(margin.get('totalUsedMargin')),
                    'timestamp': text(margin.get('timestamp'))},
                'updated_at': self.account_data_at,
            }

    def wait_for_change(self, since_version, timeout):
        with self.lock:
            if self.version == since_version:
                self.changed.wait(timeout)
            return self.version
