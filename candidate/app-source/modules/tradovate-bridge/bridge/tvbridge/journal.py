"""Durable command journal and snapshot sequence counter.

Every dispatch writes a `start` record (fsynced) before any request leaves the
bridge, then a `result` record. A start without a result after a restart means
the outcome is unknown and the command is never re-sent. Unknown outcomes block
new entries until a human records a `reconciled` result (see tvbridge.reconcile).

Bracket entries (opt-in, see executor) also record the ids of their protective
legs (`legs`: {"stop": id, "tp1": id}) in the same fsynced result record.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append(path, record):
    new = not path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n').encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    if new:
        _fsync_dir(path.parent)


# Kinds that open a trade.
ENTRY_KINDS = ('entry', 'bracket_entry')
# Kinds that act on existing orders and never own a broker order id.
NO_ORDER_KINDS = frozenset({'cancel', 'modify_stop', 'auto_modify_stop', 'auto_cancel'})


class Journal:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.dir / 'commands.jsonl'
        self.seq_path = self.dir / 'snapshot-sequence'
        self.lock = threading.Lock()
        self.commands = {}
        self.torn_tail = False
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        lines = self.path.read_text().split('\n')
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                if i >= len(lines) - 2:
                    self.torn_tail = True  # crash mid-append; the fsynced start record precedes it
                    continue
                raise
            cid = rec['command_id']
            if rec['type'] == 'start':
                self.commands[cid] = dict(rec, outcome=None)
            elif rec['type'] in ('result', 'reconciled') and cid in self.commands:
                self.commands[cid].update(outcome=rec['outcome'], broker_order_id=rec.get('broker_order_id'),
                                          receipt=rec.get('receipt'))
                if 'legs' in rec:
                    self.commands[cid]['legs'] = rec['legs']
        for c in self.commands.values():
            if c['outcome'] is None:
                c['outcome'] = 'unknown'
                c['receipt'] = {'transport': 'bridge_restarted_after_dispatch_start'}

    def get(self, command_id):
        with self.lock:
            c = self.commands.get(command_id)
            return dict(c) if c else None

    def start(self, command_id, **fields):
        with self.lock:
            if command_id in self.commands:
                raise ValueError('duplicate command id')
            rec = dict(fields, type='start', command_id=command_id)
            _append(self.path, rec)
            self.commands[command_id] = dict(rec, outcome=None)

    def finish(self, command_id, outcome, receipt, broker_order_id=None, legs=None):
        with self.lock:
            rec = {'type': 'result', 'command_id': command_id, 'outcome': outcome, 'receipt': receipt}
            if broker_order_id is not None:
                rec['broker_order_id'] = broker_order_id
            if legs is not None:
                rec['legs'] = {k: str(v) for k, v in legs.items()}
            _append(self.path, rec)
            self.commands[command_id].update(outcome=outcome, receipt=receipt, broker_order_id=broker_order_id)
            if legs is not None:
                self.commands[command_id]['legs'] = rec['legs']

    def reconcile(self, command_id, outcome, broker_order_id=None, note='', legs=None):
        """Resolution of an unknown command: 'accepted' (with order id) or 'rejected'.

        A bracket entry accepted by hand also needs its leg ids (`legs`).
        """
        with self.lock:
            c = self.commands.get(command_id)
            if not c or c['outcome'] != 'unknown':
                raise ValueError('only unknown commands can be reconciled')
            if outcome not in ('accepted', 'rejected'):
                raise ValueError('outcome must be accepted or rejected')
            kind = c.get('kind')
            if kind in NO_ORDER_KINDS:
                # A cancel or modify has no order of its own: 'accepted' means Tradovate
                # shows its effect (no working order left, or the stop changed).
                if broker_order_id:
                    raise ValueError('a %s has no broker order id' % kind)
            elif kind in ('flatten_all', 'manual_flatten'):
                # Accepted with the liquidation order id, or without one when the account
                # is verified flat with nothing working. (1.1.3: also the operator's manual_flatten.)
                if outcome == 'rejected' and broker_order_id:
                    raise ValueError('rejected must not have a broker order id')
            elif (outcome == 'accepted') != bool(broker_order_id):
                raise ValueError('accepted needs a broker order id; rejected must not have one')
            if legs and (kind != 'bracket_entry' or outcome != 'accepted'):
                raise ValueError('leg ids belong only to an accepted bracket entry')
            if kind == 'bracket_entry' and outcome == 'accepted':
                legs = {k: str(v) for k, v in (legs or {}).items()}
                if set(legs) - {'stop', 'tp1'} or 'stop' not in legs \
                        or (c.get('tp1_leg') == 'oso') != ('tp1' in legs):
                    raise ValueError('an accepted bracket entry needs its stop leg id'
                                     + (' and its tp1 leg id' if c.get('tp1_leg') == 'oso' else ''))
            receipt = {'reconciled': True, 'note': note[:240]}
            rec = {'type': 'reconciled', 'command_id': command_id, 'outcome': outcome, 'receipt': receipt}
            if broker_order_id:
                rec['broker_order_id'] = str(broker_order_id)
            if legs:
                rec['legs'] = legs
            _append(self.path, rec)
            c.update(outcome=outcome, receipt=receipt, broker_order_id=rec.get('broker_order_id'))
            if legs:
                c['legs'] = legs

    def unresolved(self):
        """Commands whose outcome is unknown or still in flight."""
        with self.lock:
            return [dict(c) for c in self.commands.values() if c['outcome'] in (None, 'unknown')]

    def accepted_order_ids(self):
        return [oid for oid, _ in self.accepted_orders()]

    def accepted_orders(self):
        """(broker_order_id, dispatch time ms or None) for every accepted order, bracket legs included."""
        with self.lock:
            out = []
            for c in self.commands.values():
                if c['outcome'] != 'accepted':
                    continue
                if c.get('broker_order_id'):
                    out.append((c['broker_order_id'], c.get('at')))
                out += [(oid, c.get('at')) for _, oid in sorted((c.get('legs') or {}).items())]
            return out

    def for_trade(self, trade_id):
        with self.lock:
            return [dict(c) for c in self.commands.values() if c.get('trade_id') == trade_id]

    def latest_trade_id(self, kinds=('entry',)):
        with self.lock:
            entries = [c for c in self.commands.values() if c.get('kind') in kinds]
            return entries[-1]['trade_id'] if entries else None

    def next_sequence(self):
        """Strictly increasing across restarts."""
        with self.lock:
            current = int(self.seq_path.read_text()) if self.seq_path.exists() else 0
            value = current + 1
            tmp = self.seq_path.with_suffix('.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, str(value).encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.seq_path)
            _fsync_dir(self.dir)
            return value
