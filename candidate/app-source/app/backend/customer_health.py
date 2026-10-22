"""Durable local health transitions and bounded optional desktop delivery.

Consumes backend snapshots only. No account labels, provider bodies, endpoints
or exception messages enter notifications. Desktop acceptance is never a read
receipt. No reconnect, control mutation or order authority exists here.
"""
from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time

PACKAGE = Path(__file__).resolve().parents[1] / 'trading-runtime'
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))
from decrypt_trading.runtime import _private_directory, _private_file

RETRY_SECONDS = (15, 60, 300, 900)
KINDS = {'guardian','runtime','market','ingress','service'}
COPY = {
    'guardian': ('Guardian needs attention', 'Fresh broker readings are unavailable. Check Guardian in Decrypt OS.'),
    'runtime': ('Trading engine needs attention', 'The local engine is not running normally. Review the engine and entry controls in Decrypt OS.'),
    'market': ('Market feed needs attention', 'Fresh chart callbacks are unavailable. Check the dedicated market browser in Decrypt OS.'),
    'ingress': ('Signal receiver needs attention', 'The configured local signal receiver is unavailable. Check its status in Decrypt OS.'),
    'service': ('Trading service needs attention', 'The local service reported a failed health check. Review its status in Decrypt OS.'),
}


class NativeNotifications:
    """Optional Linux desktop transport, fixed executable and fixed copy only."""
    def send(self, event):
        kind = event['source'].split(':', 1)[0]
        if kind not in KINDS or event['kind'] not in ('outage','recovery'):
            return {'outcome':'failed', 'reason':'notification_invalid', 'userRead':False}
        if event['kind'] == 'recovery':
            title, body = 'Decrypt OS connection recovered', 'Fresh '+{'guardian':'broker readings','runtime':'engine health','market':'market callbacks','ingress':'signal receiver health','service':'service health'}[kind]+' are available again. This does not enable entries.'
        else:
            title, body = COPY[kind]
        args = ['/usr/bin/notify-send','--app-name=Decrypt OS','--print-id','--expire-time=10000',
                '--urgency=' + ('normal' if event['kind']=='recovery' else 'critical')]
        ident = event.get('replaceId')
        if type(ident) is int and 0 < ident < 2**32:
            args.append('--replace-id='+str(ident))
        args += [title, body]
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=5, check=False,
                                    env=None, text=True)
        except subprocess.TimeoutExpired:
            # It may already be visible. Do not duplicate an uncertain send.
            return {'outcome':'unknown','reason':'notification_timeout','userRead':False}
        except OSError:
            return {'outcome':'failed','reason':'desktop_transport_unavailable','userRead':False}
        native_id = result.stdout.strip() if type(result.stdout) is str else ''
        if result.returncode != 0:
            return {'outcome':'failed','reason':'desktop_delivery_rejected','userRead':False}
        if not re.fullmatch(r'[1-9]\d{0,9}',native_id) or int(native_id) >= 2**32:
            return {'outcome':'unknown','reason':'desktop_receipt_unconfirmed','userRead':False}
        return {'outcome':'accepted','reason':'desktop_daemon_accepted',
                'nativeId':int(native_id),'userRead':False}


class CustomerHealth:
    def __init__(self, state_dir, transport=None, clock=time.time):
        self.root = _private_directory(Path(state_dir)/'trading')
        self.path = self.root/'health.sqlite'
        _private_file(self.path)
        self.transport, self.clock = transport, clock
        self.mutex = threading.RLock()
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    source TEXT PRIMARY KEY, state TEXT NOT NULL, generation INTEGER NOT NULL,
                    armed INTEGER NOT NULL, changed INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
                    generation INTEGER NOT NULL, kind TEXT NOT NULL, created INTEGER NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0,
                    native_id INTEGER, last_outcome TEXT, last_reason TEXT);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, transition_id INTEGER NOT NULL,
                    attempted INTEGER NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL,
                    native_id INTEGER);
            ''')

    @contextmanager
    def _db(self):
        _private_file(self.path)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self):
        value = self.clock()
        if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
            raise ValueError('health_clock_invalid')
        return int(value*1000)

    @staticmethod
    def _exposed(item):
        return item.get('tradePhase') not in (None,'idle','closed','unobserved')

    def _observations(self, guardian, trading, now):
        if type(guardian) is not dict or type(trading) is not dict:
            raise ValueError('internal_health_snapshots_required')
        instances = trading.get('instances', [])
        if type(instances) is not list or len(instances) > 32:
            raise ValueError('internal_health_snapshots_invalid')
        values = {}
        active = any(type(item) is dict and self._exposed(item) for item in instances)
        config = guardian.get('config') or {}
        if type(config) is dict and config.get('enabled') is True:
            monitor = guardian.get('monitor') or {}
            stamp, until = monitor.get('lastObservedAt'), monitor.get('validUntil')
            fresh = (monitor.get('state') == 'monitoring_ready'
                     and type(stamp) in (int,float) and type(until) in (int,float)
                     and 0 <= now/1000-stamp <= 12 and now/1000 <= until)
            values['guardian'] = ('healthy' if fresh else 'fault', active)
        else:
            values['guardian'] = ('unconfigured', False)
        for item in instances:
            if type(item) is not dict:
                raise ValueError('internal_health_snapshots_invalid')
            ident = item.get('instanceId')
            if type(ident) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',ident) is None:
                raise ValueError('internal_health_snapshots_invalid')
            exposed = self._exposed(item)
            runtime_state = item.get('runtimeState')
            state = 'healthy' if runtime_state == 'running' and not item.get('fault') else 'fault' if runtime_state in ('failed','closed') or item.get('fault') or exposed else 'unconfigured'
            values['runtime:'+ident] = (state, exposed)
            feed = item.get('feed') or {}
            stamp = feed.get('observedAt')
            fresh = (feed.get('nativeVerified') is True and type(stamp) is int and 0 <= now-stamp <= 2000)
            configured = feed.get('state') not in (None,'not_started','not_configured')
            values['market:'+ident] = ('healthy' if fresh else 'fault' if configured or exposed else 'unconfigured', exposed)
        ingress = trading.get('ingress') or {}
        state = ingress.get('state')
        values['ingress'] = ('healthy' if state in ('listening','listening_loopback','running') else 'unconfigured' if state in (None,'not_configured') else 'fault', active)
        checks = trading.get('health') or []
        failed = any(type(item) is dict and item.get('source') == 'service' and item.get('state') == 'check_failed' for item in checks)
        values['service'] = ('fault' if failed else 'healthy' if instances else 'unconfigured', active)
        return values

    def observe(self, *, guardian, trading):
        """Persist transitions only; no native transport runs under service locks."""
        now = self._now()
        values = self._observations(guardian, trading, now)
        with self.mutex, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Removed bindings no longer emit stale warnings. Do not claim a
            # healthy recovery just because a setup disappeared from inventory.
            for row in db.execute('SELECT source FROM sources').fetchall():
                values.setdefault(row['source'], ('unconfigured', False))
            for source, (state, force) in values.items():
                old = db.execute('SELECT * FROM sources WHERE source=?',(source,)).fetchone()
                if old and old['state'] == state and not (force and not old['armed']):
                    continue
                generation = old['generation']+1 if old else 1
                armed = bool(force or state == 'healthy' or old and old['armed'])
                previous = db.execute('SELECT * FROM transitions WHERE source=? ORDER BY id DESC LIMIT 1',(source,)).fetchone()
                recovering = bool(old and old['state']=='fault' and state=='healthy' and previous and
                                  previous['kind']=='outage' and (previous['status'] in ('accepted','sending','unknown') or previous['last_outcome'] in ('accepted','unknown')))
                db.execute("UPDATE transitions SET status='superseded' WHERE source=? AND status IN ('pending','retry','sending')",(source,))
                if state == 'fault' and armed or recovering:
                    db.execute('INSERT INTO transitions(source,generation,kind,created,status,next_attempt) VALUES(?,?,?,?,?,?)',
                               (source,generation,'recovery' if recovering else 'outage',now,'pending',now))
                db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)',(source,state,generation,int(armed),now))
            db.execute("DELETE FROM transitions WHERE status IN ('accepted','superseded','exhausted','unknown') AND id NOT IN (SELECT id FROM transitions ORDER BY id DESC LIMIT 1000)")
            db.execute('DELETE FROM attempts WHERE id NOT IN (SELECT id FROM attempts ORDER BY id DESC LIMIT 2000)')
        return self.status()

    def dispatch_one(self):
        """One bounded native send. A caller may run this after releasing locks."""
        if self.transport is None:
            return {'state':'disabled','userRead':False}
        now = self._now()
        with self.mutex, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            expired = db.execute("SELECT * FROM transitions WHERE status='sending' AND lease_until<=?",(now,)).fetchall()
            for stale in expired:
                db.execute("UPDATE transitions SET status='unknown',last_outcome='unknown',last_reason='delivery_interrupted' WHERE id=?",(stale['id'],))
                db.execute('INSERT INTO attempts(transition_id,attempted,outcome,reason) VALUES(?,?,?,?)',
                           (stale['id'],now,'unknown','delivery_interrupted'))
            row = db.execute("SELECT * FROM transitions WHERE status IN ('pending','retry') AND next_attempt<=? ORDER BY id LIMIT 1",(now,)).fetchone()
            if row is None:
                return {'state':'idle','userRead':False}
            row = dict(row)
            prior = db.execute('SELECT native_id FROM transitions WHERE source=? AND native_id IS NOT NULL ORDER BY id DESC LIMIT 1',(row['source'],)).fetchone()
            db.execute("UPDATE transitions SET status='sending',attempts=attempts+1,lease_until=? WHERE id=?",(now+15000,row['id']))
            event = {'source':row['source'],'kind':row['kind'],'replaceId':prior['native_id'] if prior else None}
        try:
            receipt = self.transport.send(event)
        except Exception:
            # Unknown custom transport exception may follow a successful send.
            receipt = {'outcome':'unknown','reason':'transport_result_unknown'}
        allowed = {'desktop_daemon_accepted','desktop_transport_unavailable','desktop_delivery_rejected',
                   'notification_timeout','desktop_receipt_unconfirmed','notification_invalid','transport_result_unknown'}
        if (type(receipt) is not dict or receipt.get('outcome') not in ('accepted','failed','unknown')
                or receipt.get('reason') not in allowed):
            receipt = {'outcome':'unknown','reason':'transport_result_unknown'}
        outcome, reason = receipt['outcome'], receipt['reason']
        native_id = receipt.get('nativeId')
        if type(native_id) is not int or not 0 < native_id < 2**32:
            native_id = None
            if outcome == 'accepted':
                outcome, reason = 'unknown','desktop_receipt_unconfirmed'
        finished = self._now()
        with self.mutex, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT * FROM transitions WHERE id=?',(row['id'],)).fetchone()
            status = 'superseded' if latest['status']=='superseded' else 'accepted' if outcome=='accepted' else 'unknown' if outcome=='unknown' else 'exhausted' if latest['attempts']>=5 else 'retry'
            retry = finished + RETRY_SECONDS[min(latest['attempts']-1,3)]*1000 if status=='retry' else 0
            db.execute('UPDATE transitions SET status=?,next_attempt=?,lease_until=0,native_id=?,last_outcome=?,last_reason=? WHERE id=?',
                       (status,retry,native_id,outcome,reason,row['id']))
            db.execute('INSERT INTO attempts(transition_id,attempted,outcome,reason,native_id) VALUES(?,?,?,?,?)',
                       (row['id'],finished,outcome,reason,native_id))
        return {'state':status,'outcome':outcome,'reason':reason,'transitionId':row['id'],
                'nativeId':native_id,'userRead':False}

    def status(self):
        with self.mutex, self._db() as db:
            current = [dict(row) for row in db.execute('SELECT source,state,changed FROM sources ORDER BY source')]
            events = [dict(row) for row in db.execute('SELECT id,source,kind,created,status,attempts,next_attempt,last_outcome,last_reason FROM transitions ORDER BY id DESC LIMIT 20')]
            pending = db.execute("SELECT count(*) FROM transitions WHERE status IN ('pending','retry','sending')").fetchone()[0]
        return {'enabled':self.transport is not None,'current':current,'events':events,'pending':pending,
                'receiptSemantics':'desktop_daemon_acceptance_not_user_read','userRead':False}
