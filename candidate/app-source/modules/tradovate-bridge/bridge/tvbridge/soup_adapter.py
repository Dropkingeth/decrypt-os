"""Deliver bridge snapshots to Soup v3 the way Soup's own broker adapter does.

Soup v3 does not accept broker snapshots over HTTP. It reads append-only
SQLite adapter databases (`--adapter-db`, channel `broker`) and requires each event's
`sequence` to exceed the engine's own `broker_seq`. The older page-reading adapter reads that
value from `broker-context.json`; this sink does the same, so the bridge can take over
from an adapter that has already advanced the sequence.

Schema and write pattern match Soup v3's own broker adapter (adapter.py).
Only one broker adapter may feed Soup at a time; cutover swaps Soup's `--adapter-db`.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import sqlite3
import stat
from pathlib import Path

SCHEMA = ('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; '
          'CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,channel TEXT,event_id TEXT UNIQUE,'
          'payload TEXT,source TEXT,observed_at INTEGER); '
          'CREATE TABLE IF NOT EXISTS broker_state(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL);')
DEFAULT_CONTEXT = Path.home() / '.local/share/decrypt-os/soup/broker-context.json'


class Skip(Exception):
    """Nothing to publish right now; not a fault (e.g. engine has no active trade)."""


def _read_regular_json(path):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError('context is not a regular file')
    with open(path, 'rb') as fh:
        return json.loads(fh.read(1_000_000))


class AdapterDbSink:
    def __init__(self, db_path, *, account_id, ticker, context_path=DEFAULT_CONTEXT, source='tvbridge'):
        self.path = Path(db_path)
        self.context_path = Path(context_path)
        self.account_id, self.ticker, self.source = account_id, ticker, source
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # One writer per database: a second bridge must not interleave sequences.
        self._lock = open(self.path.parent / 'tvbridge-writer.lock', 'a')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise RuntimeError('another bridge writer holds this adapter database')
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()
        self._lock.close()

    def previous_sequence(self):
        row = self.db.execute('SELECT body FROM broker_state WHERE id=1').fetchone()
        return json.loads(row[0])['sequence'] if row else 0

    def publish(self, snap):
        """Append one broker event. Returns the stored payload.

        Raises Skip when the engine has no matching active trade, ValueError on a
        pairing mismatch or unusable context. Nothing is written in either case.
        """
        context = _read_regular_json(self.context_path)
        pairing = context.get('pairing') or {}
        if pairing.get('account_id') != self.account_id or pairing.get('ticker') != self.ticker:
            raise ValueError('engine pairing differs from bridge configuration')
        if snap.get('account_id') != self.account_id or snap.get('symbol') != self.ticker:
            raise ValueError('snapshot pairing differs from bridge configuration')
        engine = context.get('engine') or {}
        trade = engine.get('trade')
        if not trade or trade.get('phase') == 'closed' or trade.get('id') != snap.get('trade_id'):
            raise Skip('no_matching_active_trade')
        broker_seq = engine.get('broker_seq')
        if type(broker_seq) is not int or broker_seq < 0:
            raise ValueError('engine broker_seq missing')
        seq = max(self.previous_sequence(), broker_seq) + 1
        payload = copy.deepcopy(snap)
        payload.update(sequence=seq, event_id=f'{self.source}-{seq}')
        body = json.dumps(payload, separators=(',', ':'))
        with self.db:
            self.db.execute('INSERT INTO events(channel,event_id,payload,source,observed_at) VALUES(?,?,?,?,?)',
                            ('broker', payload['event_id'], body, self.source, payload['observed_at']))
            self.db.execute('INSERT OR REPLACE INTO broker_state VALUES(1,?)',
                            (json.dumps({'sequence': seq,
                                         'orders': {o['command_id']: o for o in payload['orders']}}),))
        return payload
