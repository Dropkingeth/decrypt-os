"""Reversible registry-only retirement. No native orders or ledger rewrites.

Parent lifecycle must stop feed/bridge and close the held runtime first. Ordinary
SQLite close may settle its WAL BEFORE retirement; this module preserves every
remaining instance-file byte and all historical command/receipt/input records.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

MAX_INSTANCES = 32
FINAL_COMMANDS = frozenset({'filled','cancelled','rejected','superseded'})


def _need(value,reason):
    if not value:
        from .runtime import Conflict
        raise Conflict(reason)


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS retired_instances (id TEXT PRIMARY KEY, '
               'account_id TEXT NOT NULL, route_id TEXT NOT NULL, endpoint_sha256 TEXT NOT NULL, '
               'binding TEXT NOT NULL, created_ms INTEGER NOT NULL, retired_ms INTEGER NOT NULL, evidence TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS instance_lifecycle_events (seq INTEGER PRIMARY KEY, '
               'instance_id TEXT NOT NULL, at_ms INTEGER NOT NULL, action TEXT NOT NULL, evidence TEXT NOT NULL)')


def total(db):
    return db.execute('SELECT (SELECT count(*) FROM instances)+(SELECT count(*) FROM retired_instances)').fetchone()[0]


def file_manifest(directory):
    """Only private stopped SQLite files; no WAL/SHM or special/unknown entries."""
    allowed={'.writer.lock','engine.sqlite','market.sqlite'}
    directory=Path(directory);before=directory.stat();out={}
    _need(stat.S_ISDIR(directory.lstat().st_mode),'archive directory must not be linked')
    for path in sorted(directory.iterdir()):
        _need(path.name in allowed,'stop all workers and settle SQLite sidecars before retirement')
        info=path.lstat()
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.getuid()
              and not info.st_mode&0o077,'archive files must be private single-link regular files')
        hasher=hashlib.sha256()
        with path.open('rb')as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):hasher.update(block)
        digest=hasher.hexdigest()
        after=path.lstat()
        _need((info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns),'archive file changed during preservation')
        out[path.name]={'bytes':info.st_size,'sha256':digest}
    _need('engine.sqlite'in out,'instance engine ledger is missing')
    _need((directory.stat().st_dev,directory.stat().st_ino)==(before.st_dev,before.st_ino),'archive directory changed')
    return out


def settle_owned_sqlite(path):
    """Apple SQLite persist-WAL compatibility, using SQLite's own file-control.

    Run only after all owning Python connections have closed. Never unlink a
    journal. Linux/pinned runtimes normally have no sidecars and do nothing here.
    """
    import subprocess
    import sys
    from .runtime import _private_file, RuntimeError
    path=Path(path).absolute();_private_file(path)
    sidecars=[Path(str(path)+suffix)for suffix in ('-wal','-shm')]
    if not any(p.exists()for p in sidecars):return
    if sys.platform!='darwin':
        raise RuntimeError('SQLite journals remain open; archive cannot proceed')
    before=path.lstat();identity=(before.st_dev,before.st_ino)
    # Fixed system utility/static SQL. No user SQL, shell, logs or raw diagnostics.
    # PERSIST_WAL is an SQLite file-control API, unavailable through Python's
    # stdlib wrapper. SQLite itself safely releases its transient journals.
    try:
        result=subprocess.run(['/usr/bin/sqlite3','-batch','-bail',str(path)],
            input='.timeout 1000\n.filectrl persist_wal 0\nPRAGMA journal_mode=WAL;\nPRAGMA wal_checkpoint(TRUNCATE);\nPRAGMA journal_mode=DELETE;\n',
            text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
    except Exception:
        raise RuntimeError('SQLite journal settlement unavailable; preserve state')from None
    if result.returncode!=0 or result.stdout.splitlines()!=['0','wal','0|0|0','delete']:
        raise RuntimeError('SQLite journal settlement incomplete; preserve state')
    after=path.lstat()
    if (after.st_dev,after.st_ino)!=identity or any(p.exists()for p in sidecars):
        raise RuntimeError('SQLite journal identity changed or sidecars remain')


def read_ledger(directory,binding):
    from .runtime import canonical
    path=Path(directory)/'engine.sqlite'
    # Sidecars were refused before immutable open: no hidden unapplied WAL state.
    db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        _need(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','engine ledger integrity must be verified')
        metadata=db.execute("SELECT value FROM customer_runtime_metadata WHERE key='binding'").fetchone()
        _need(metadata and metadata[0]==hashlib.sha256(canonical(binding).encode()).hexdigest(),'archive binding differs from preserved engine ledger')
        row=db.execute('SELECT body FROM state WHERE id=1').fetchone();_need(row is not None,'engine state missing')
        state=json.loads(row[0])
        _need(db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0]==0,'pending inputs must be drained while held before retirement')
        history=[json.loads(row[0])for row in db.execute('SELECT body FROM history ORDER BY trade_id')]
        generation=db.execute("SELECT max(seq) FROM customer_runtime_audit WHERE kind='opened_held'").fetchone()[0]
        broker_sequence=int(db.execute("SELECT value FROM customer_runtime_metadata WHERE key='broker_sequence'").fetchone()[0])
        return state,history,generation,broker_sequence
    finally:db.close()


def _closed_commands(trade,commands):
    _need(trade is None or trade.get('phase')=='closed','active trades cannot be retired')
    _need(type(commands)is dict,'command ledger is invalid')
    for command in commands.values():
        accepted_cancel=(command.get('status')in ('accepted','confirmed') and command.get('kind') in ('cancel','modify_stop')
            and trade and trade.get('phase')=='closed' and command.get('trade_id')==trade.get('id'))
        _need(command.get('status')in FINAL_COMMANDS or accepted_cancel,'ready, started, unknown or unresolved accepted command cannot be retired')


def eligibility(runtime,state,history,generation,broker_sequence,*,require_closed=True,require_native_flat=True):
    from .runtime import canonical
    if require_closed:
        _need(runtime.closed and not(runtime.worker and runtime.worker.is_alive()),'close the held runtime before retirement')
    _need(runtime.enabled is False and runtime.locked is True,'retirement requires held entry controls')
    _need(getattr(runtime,'open_generation',None)==generation,'runtime generation changed; use the most recently closed runtime')
    _need(canonical(runtime.engine.data)==canonical(state),'runtime snapshot differs from stopped ledger')
    trade,commands=state.get('trade'),state.get('commands')
    _closed_commands(trade,commands)
    for old in history:
        _need(type(old)is dict and set(old)=={'trade','commands'},'historical lifecycle format is invalid')
        _closed_commands(old['trade'],old['commands'])
    never_traded=trade is None and not commands and not history
    if never_traded:
        return {'basis':'never_dispatched_idle','nativeFlatClaimed':False,'nativeEvidence':None}
    if not require_native_flat:
        # Only the observation-reattachment seam uses this. It does not grant
        # retirement, configuration, rotation, entry or broker-flat authority.
        return {'basis':'closed_local_state_only','nativeFlatClaimed':False,'nativeEvidence':None}
    from .runtime import canonical as _canonical
    if getattr(runtime,'bridge',None) is not None:
        # Tradovate bridge route: the bridge's own fresh account report proves flat.
        snap=runtime.bridge.route.flat_state()
        _need(snap is not None,'retirement needs a fresh flat account report from the Tradovate bridge')
        return {'basis':'closed_bridge_flat','nativeFlatClaimed':True,
                'nativeEvidence':{'observedAt':snap.get('observed_at'),
                                  'snapshotSha256':hashlib.sha256(_canonical(snap).encode()).hexdigest(),
                                  'snapshot':json.loads(_canonical(snap))}}
    p=runtime.proof;now=runtime.clock()
    from .customer_contract import native, validate_complete_confirmation
    if native(runtime.binding):
        try:
            validate_complete_confirmation(p,runtime.binding,at=now,
                                           not_before=runtime.opened_at,require_flat=True)
        except (ValueError,KeyError,TypeError):
            _need(False,'retirement needs a fresh complete native Tradovate flat confirmation')
        _need(p['sequence']==broker_sequence,'retirement native acquisition sequence invalid')
        return {'basis':'closed_native_flat','nativeFlatClaimed':True,
                'nativeEvidence':{'sequence':p['sequence'],'observedAt':p['observedAt'],
                                  'snapshotSha256':hashlib.sha256(canonical(p).encode()).hexdigest(),
                                  'snapshot':json.loads(canonical(p))}}
    fields={'schema','instanceId','accountId','pairId','symbol','sequence','observedAt',
            'positionsComplete','ordersComplete','pendingComplete','correlationComplete','routeVerified',
            'accountMode','routeId','subscriptionId','endpointSha256','entryRecipients','positionQuantity',
            'workingOrderIds','pendingOrderIds','unattributedOrderIds','otherContractExposure','engineEvent'}
    _need(type(p)is dict and set(p)==fields,'only the accepted allowlisted native snapshot can be archived')
    event=p['engineEvent']
    if event is not None:
        _need(type(event)is dict and set(event)=={'event_id','sequence','observed_at','account_id','symbol','trade_id','side','position_qty','orders','open_orders'},'native event archive fields differ')
        _need(type(event['orders'])is list and all(type(o)is dict and set(o)=={'command_id','broker_order_id','status','filled_qty','fill_time'}for o in event['orders']),'native order archive fields differ')
    _need(type(p)is dict and p.get('schema')=='decrypt.customer-broker-confirmation.v1'
          and type(p.get('observedAt'))is int and runtime.opened_at<=p['observedAt']<=now
          and now-p['observedAt']<=12000,'traded retirement needs fresh independently accepted native confirmation')
    for key in ('instanceId','accountId','pairId','symbol','accountMode','routeId','subscriptionId','endpointSha256'):
        _need(p.get(key)==runtime.binding[key],'retirement native proof binding differs')
    _need(all(p.get(k)is True for k in ('positionsComplete','ordersComplete','pendingComplete','correlationComplete','routeVerified'))
          and p.get('entryRecipients')==[runtime.binding['accountId']]
          and p.get('positionQuantity')==0 and p.get('otherContractExposure')is False
          and all(p.get(k)==[]for k in ('workingOrderIds','pendingOrderIds','unattributedOrderIds')),
          'retirement needs complete native flat with no working, pending or unattributed orders')
    _need(type(p.get('sequence'))is int and p['sequence']>0 and p['sequence']==broker_sequence,'retirement native acquisition sequence invalid')
    return {'basis':'closed_native_flat','nativeFlatClaimed':True,
            'nativeEvidence':{'sequence':p['sequence'],'observedAt':p['observedAt'],
                              'snapshotSha256':hashlib.sha256(canonical(p).encode()).hexdigest(),'snapshot':json.loads(canonical(p))}}


def precheck(manager,runtime,*,require_native_flat=True):
    """Refuse unsafe retirement BEFORE the parent stops active supervision.

    Caller has already held entries. This reads committed local state only; it
    never steps/drains an inbox or sends an order. Final retirement repeats it
    after workers close and cold file hashes are preserved.
    """
    from .runtime import canonical
    _need(runtime.directory==manager.root/runtime.binding['instanceId'],'runtime belongs to another registry')
    with runtime.mutex:
        runtime._check()
        _need(manager.get(runtime.binding['instanceId'])==runtime.binding,'instance binding is not active')
        db=runtime.engine.db
        _need(db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0]==0,'pending inputs must finish under normal held supervision before retirement')
        state=json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        history=[json.loads(row[0])for row in db.execute('SELECT body FROM history ORDER BY trade_id')]
        generation=db.execute("SELECT max(seq) FROM customer_runtime_audit WHERE kind='opened_held'").fetchone()[0]
        sequence=int(db.execute("SELECT value FROM customer_runtime_metadata WHERE key='broker_sequence'").fetchone()[0])
        evidence=eligibility(runtime,state,history,generation,sequence,require_closed=False,require_native_flat=require_native_flat)
        return {'instanceId':runtime.binding['instanceId'],'retirementAllowed':True,'entriesHeld':True,
                'basis':evidence['basis'],'nativeFlatClaimed':evidence['nativeFlatClaimed'],
                'supervisionStopped':False,'registryChanged':False}


def retire(manager,runtime):
    from .runtime import canonical,Conflict
    _need(runtime.directory==manager.root/runtime.binding['instanceId'],'runtime belongs to another registry')
    with runtime.mutex:
        _need(runtime.closed,'close the held runtime before retirement')
        with manager._db()as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM instances WHERE id=?',(runtime.binding['instanceId'],)).fetchone()
            _need(row is not None,'instance is not active; retirement is not a replay action')
            _need(row['binding']==canonical(runtime.binding),'runtime immutable binding differs from registry')
            # Registry transaction precedes writer lock, matching RuntimeManager.open.
            writer=runtime.modules['storage'].WriterLock(runtime.directory)
            try:
                files=file_manifest(runtime.directory);ledger=read_ledger(runtime.directory,runtime.binding)
                evidence={**eligibility(runtime,*ledger),'files':files,'entriesHeld':True,
                          'ledgerRewritten':False,'restoresRemainRetired':True}
                _need(file_manifest(runtime.directory)==files,'ledger changed before archival registry commit')
                # Repeat native freshness after hashing, immediately before moving row.
                eligibility(runtime,*ledger)
                now=runtime.clock();values=tuple(row[k]for k in ('id','account_id','route_id','endpoint_sha256','binding','created_ms'))
                db.execute('INSERT INTO retired_instances VALUES (?,?,?,?,?,?,?,?)',(*values,now,canonical(evidence)))
                db.execute('DELETE FROM instances WHERE id=?',(row['id'],))
                db.execute('INSERT INTO instance_lifecycle_events(instance_id,at_ms,action,evidence) VALUES (?,?,?,?)',(row['id'],now,'retired',canonical(evidence)))
            except sqlite3.IntegrityError as exc:raise Conflict('instance archival registry conflict')from exc
            finally:writer.close()
    return {'instanceId':runtime.binding['instanceId'],'registryState':'retired','entriesHeld':True,
            'runtimeOpened':False,'nativeFlatClaimed':evidence['nativeFlatClaimed'],'basis':evidence['basis'],'ledgerPreserved':True}


def retire_retired_engine(manager,instance_id,*,clock):
    """Retire an active instance whose engine is retired (its pair is no longer shipped), without loading it.

    Its engine cannot open, so there is no runtime to hold or close: the ledger is read only (immutable SQLite),
    must hold no pending input, active trade or unresolved command, and is preserved byte for byte. No broker
    flat is claimed. Anything else refuses, exactly like ordinary retirement.
    """
    from .runtime import _identifier,validate_stored_binding,canonical,Conflict
    from .source import load_sources
    _need(_identifier(instance_id),'invalid instance identity')
    directory=manager.root/instance_id
    with manager._db()as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM instances WHERE id=?',(instance_id,)).fetchone()
        _need(row is not None,'instance is not active; retirement is not a replay action')
        binding,retired=validate_stored_binding(json.loads(row['binding']))
        _need(retired,'this engine is supported; retire it through its held runtime')
        _need(binding['instanceId']==instance_id and row['binding']==canonical(binding),'retired engine binding differs from registry')
        _need((directory/'engine.sqlite').exists(),'retired engine ledger is missing')
        settle_owned_sqlite(directory/'engine.sqlite')
        # Soup's reviewed writer lock only (no engine code): no other process may hold this ledger.
        writer=load_sources()['storage'].WriterLock(directory)
        try:
            files=file_manifest(directory)
            state,history,_generation,_sequence=read_ledger(directory,binding)
            _closed_commands(state.get('trade'),state.get('commands'))
            for old in history:
                _need(type(old)is dict and set(old)=={'trade','commands'},'historical lifecycle format is invalid')
                _closed_commands(old['trade'],old['commands'])
            evidence={'basis':'retired_engine_closed_ledger','nativeFlatClaimed':False,'nativeEvidence':None,
                      'files':files,'entriesHeld':True,'ledgerRewritten':False,'restoresRemainRetired':True}
            _need(file_manifest(directory)==files,'ledger changed before archival registry commit')
            now=clock();values=tuple(row[k]for k in ('id','account_id','route_id','endpoint_sha256','binding','created_ms'))
            try:
                db.execute('INSERT INTO retired_instances VALUES (?,?,?,?,?,?,?,?)',(*values,now,canonical(evidence)))
                db.execute('DELETE FROM instances WHERE id=?',(row['id'],))
                db.execute('INSERT INTO instance_lifecycle_events(instance_id,at_ms,action,evidence) VALUES (?,?,?,?)',(row['id'],now,'retired',canonical(evidence)))
            except sqlite3.IntegrityError as exc:raise Conflict('instance archival registry conflict')from exc
        finally:writer.close()
    return {'instanceId':instance_id,'registryState':'retired','entriesHeld':True,'runtimeOpened':False,
            'nativeFlatClaimed':False,'basis':'retired_engine_closed_ledger','ledgerPreserved':True}


def reactivate(manager,instance_id,*,clock):
    from .runtime import _identifier,canonical,Conflict
    from .source import load_pair_sources
    _need(_identifier(instance_id),'invalid instance identity')
    with manager._db()as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM retired_instances WHERE id=?',(instance_id,)).fetchone()
        _need(row is not None,'instance is not retired; reactivation is explicit')
        from .runtime import validate_stored_binding
        binding,retired=validate_stored_binding(json.loads(row['binding']))
        _need(not retired,'Retired engine: this strategy now uses the new bridge format and cannot be reactivated.')
        modules=load_pair_sources(binding['pairId'])
        writer=modules['storage'].WriterLock(manager.root/instance_id)
        try:
            evidence=json.loads(row['evidence']);_need(file_manifest(manager.root/instance_id)==evidence['files'],'archived ledger changed; do not reactivate')
            read_ledger(manager.root/instance_id,binding)
            values=tuple(row[k]for k in ('id','account_id','route_id','endpoint_sha256','binding','created_ms'))
            try:db.execute('INSERT INTO instances (id, account_id, route_id, endpoint_sha256, binding, created_ms) VALUES (?,?,?,?,?,?)',values)
            except sqlite3.IntegrityError as exc:raise Conflict('account or route is active on another instance; archived ledger retained')from exc
            db.execute('DELETE FROM retired_instances WHERE id=?',(instance_id,))
            detail={'entriesHeld':True,'runtimeOpened':False,'retirementEvidenceSha256':hashlib.sha256(row['evidence'].encode()).hexdigest()}
            db.execute('INSERT INTO instance_lifecycle_events(instance_id,at_ms,action,evidence) VALUES (?,?,?,?)',(instance_id,clock(),'reactivated',canonical(detail)))
        finally:writer.close()
    return {'instanceId':instance_id,'registryState':'active','entriesHeld':True,'runtimeOpened':False,'ledgerPreserved':True}
