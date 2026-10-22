"""Internal maintenance boundary for real customer services, not browser assertions."""
from __future__ import annotations
from contextlib import contextmanager, ExitStack
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

import maintenance as core

COLD={'restore/apply','restore/recover','release/activate','release/rollback','offboard','restore/reconcile'}
FIELDS={'backup':({'passphrase'},{'includeCredentials'}),'restore/prepare':({'backupId','passphrase'},set()),
        'restore/apply':({'restoreId'},set()),'restore/recover':({'transactionId'},set()),
        'restore/reconcile':(set(),set()),'release/activate':({'version'},set()),'release/rollback':(set(),set()),
        'offboard':({'confirmation'},set())}

class OperationGate:
    """Concurrent ordinary operations, exclusive maintenance, no lock/join cycle.

    This keeps manual holds responsive during a slow provider request, while
    maintenance waits for every existing writer/delivery.
    Once maintenance is pending, no further ordinary operation starts.
    """
    def __init__(self):
        self.condition=threading.Condition(threading.RLock())
        self.active=0;self.waiting=0;self.owner=None;self.depth=0
    @contextmanager
    def ordinary(self,stop_event=None):
        ident=threading.get_ident()
        with self.condition:
            while (self.owner is not None or self.waiting) and self.owner!=ident:
                core.need(stop_event is None or not stop_event.is_set(),'operation_stopped')
                self.condition.wait(.1)
            nested=self.owner==ident
            if not nested:self.active+=1
        try:yield
        finally:
            if not nested:
                with self.condition:self.active-=1;self.condition.notify_all()
    @contextmanager
    def exclusive(self):
        ident=threading.get_ident()
        with self.condition:
            if self.owner==ident:self.depth+=1
            else:
                self.waiting+=1
                try:
                    while self.owner is not None or self.active:self.condition.wait()
                    self.owner=ident;self.depth=1
                finally:self.waiting-=1
        try:yield
        finally:
            with self.condition:
                self.depth-=1
                if not self.depth:self.owner=None;self.condition.notify_all()


def installed_configuration():
    """Only fixed immutable installation paths supply signing keys and baseline."""
    root=Path('/usr/local/lib/decrypt-os');helper=root/'installer/version_bootstrap.py'
    if not helper.exists():return {},None
    for path in (helper,*helper.parents):
        info=path.lstat()
        core.need(not path.is_symlink() and info.st_uid==0 and not info.st_mode&0o022,'installed_root_not_trusted')
    spec=importlib.util.spec_from_file_location('trusted_maintenance_bootstrap',helper)
    bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)
    trust=bootstrap.load_trust(root/'release-trust.json',core)
    return trust,lambda:bootstrap.installed_release(root,core)


def installed_adoption(server,manager):
    root=Path('/usr/local/lib/decrypt-os');helper=root/'installer/version_bootstrap.py'
    core.need(helper.exists(),'reconciliation_adoption_unverified')
    for path in (helper,*helper.parents):
        info=path.lstat()
        core.need(not path.is_symlink() and info.st_uid==0 and not info.st_mode&0o022,'installed_root_not_trusted')
    spec=importlib.util.spec_from_file_location('trusted_release_reconciliation',helper)
    bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)
    return bootstrap.verify_running_api(manager,root,core,inspect.getfile(type(server)),__file__)


class MaintenanceService:
    def __init__(self,server,*,clock=time.time,trust=None,installed_release=None,restart_factory=None,adoption=None,bridge_opener=None):
        self.server,self.clock=server,clock
        self.bridge_opener=bridge_opener
        self.action_name=None;self.snapshot=None;self.restart_required=False
        self.last_operation=None;self.restart_factory=restart_factory
        self.adoption=adoption or (lambda manager:installed_adoption(self.server,manager))
        if trust is None:trust,installed_release=installed_configuration()
        self.manager=core.Maintenance(server.store.directory,self.runtime_safety,
            trusted_release_keys=trust,quiesce=self.quiesce,clock=clock,installed_release=installed_release)
        self.receipt=self.manager.root/'last-operation.json'
        if self.receipt.exists() or self.receipt.is_symlink():
            value=core.strict_json(core.regular(self.receipt))
            core.need(type(value) is dict and set(value)=={'schema','action','at','result'}
                      and value['schema']=='decrypt.maintenance-operation.v1'
                      and value['action'] in FIELDS and type(value['at']) is int and type(value['result']) is dict,'maintenance_receipt_invalid')
            self.last_operation={k:value[k] for k in ('action','at','result')}

    def _now(self):return int(self.clock()*1000)

    @staticmethod
    def _read_database(path,query,immutable=False):
        core.regular(path)
        db=sqlite3.connect(Path(path).absolute().as_uri()+'?mode=ro'+('&immutable=1' if immutable else ''),uri=True,timeout=5)
        try:return db.execute(query).fetchall()
        finally:db.close()

    def _bindings(self,include_retired=True):
        path=self.manager.state/'trading/instances.sqlite'
        if not path.exists() and not path.is_symlink():return []
        rows=self._read_database(path,'SELECT binding FROM instances ORDER BY id')
        active=[core.strict_json(row[0]) for row in rows]
        archived=[core.strict_json(row[0]) for row in self._retired_rows()] if include_retired else []
        core.need(len({item.get('instanceId') for item in active+archived})==len(active+archived),'maintenance_unreconciled')
        from decrypt_trading.runtime import validate_stored_binding
        out=[]
        for item in active:
            binding,retired=validate_stored_binding(item)
            # An active instance of a retired engine cannot be held or proven flat by its engine: retire it first.
            core.need(not retired,'maintenance_retired_engine_active')
            out.append(binding)
        # An archived instance may belong to a retired engine; its ledger is still verified below.
        return out+[validate_stored_binding(item)[0] for item in archived]

    def _retired_rows(self):
        path=self.manager.state/'trading/instances.sqlite'
        if not path.exists():return []
        tables=self._read_database(path,"SELECT name FROM sqlite_master WHERE type='table' AND name='retired_instances'")
        return self._read_database(path,'SELECT binding,evidence,retired_ms FROM retired_instances ORDER BY id') if tables else []

    def _retired_ids(self):
        return {core.strict_json(row[0])['instanceId'] for row in self._retired_rows()}

    def _validate_retired(self):
        from decrypt_trading import retirement
        from decrypt_trading.runtime import validate_stored_binding
        for raw,evidence_raw,retired_at in self._retired_rows():
            binding=validate_stored_binding(core.strict_json(raw))[0];evidence=core.strict_json(evidence_raw)
            core.need(type(evidence) is dict and set(evidence)=={'basis','nativeFlatClaimed','nativeEvidence','files',
                'entriesHeld','ledgerRewritten','restoresRemainRetired'} and evidence['entriesHeld'] is True
                and evidence['ledgerRewritten'] is False and evidence['restoresRemainRetired'] is True
                and type(retired_at) is int and retired_at>0,'maintenance_archived_changed')
            directory=self.manager.state/'trading'/binding['instanceId']
            try:
                core.need(retirement.file_manifest(directory)==evidence['files'],'maintenance_archived_changed')
                state,history,*_=retirement.read_ledger(directory,binding)
                retirement._closed_commands(state.get('trade'),state.get('commands'))
                for old in history:retirement._closed_commands(old['trade'],old['commands'])
            except Exception:raise core.MaintenanceError('maintenance_archived_changed') from None
            if evidence['basis']=='never_dispatched_idle':
                core.need(evidence['nativeFlatClaimed'] is False and evidence['nativeEvidence'] is None
                    and state.get('trade') is None and not state.get('commands') and not history,'maintenance_archived_changed')
            elif evidence['basis']=='retired_engine_closed_ledger':
                # Retired engine retired without its runtime: closed local ledger, no broker claim.
                core.need(evidence['nativeFlatClaimed'] is False and evidence['nativeEvidence'] is None,'maintenance_archived_changed')
            elif evidence['basis']=='closed_bridge_flat':
                proof=evidence['nativeEvidence']
                core.need(evidence['nativeFlatClaimed'] is True and type(proof) is dict
                    and set(proof)=={'observedAt','snapshotSha256','snapshot'} and type(proof['snapshot']) is dict
                    and core.digest(core.canonical(proof['snapshot']))==proof['snapshotSha256']
                    and proof['snapshot'].get('net_position')==0 and proof['snapshot'].get('open_order_ids')==[],
                    'maintenance_archived_changed')
            else:
                proof=evidence['nativeEvidence']
                core.need(evidence['basis']=='closed_native_flat' and evidence['nativeFlatClaimed'] is True and type(proof) is dict
                    and set(proof)=={'sequence','observedAt','snapshotSha256','snapshot'} and type(proof['sequence']) is int and proof['sequence']>0
                    and type(proof['observedAt']) is int and 0<=retired_at-proof['observedAt']<=12000
                    and type(proof['snapshotSha256']) is str and core.HEX.fullmatch(proof['snapshotSha256']),'maintenance_archived_changed')
                snapshot=proof['snapshot']
                core.need(type(snapshot) is dict,'maintenance_archived_changed')
                from decrypt_trading.customer_contract import native, validate_complete_confirmation
                if native(binding):
                    core.need(core.digest(core.canonical(snapshot))==proof['snapshotSha256']
                        and snapshot.get('sequence')==proof['sequence'] and snapshot.get('observedAt')==proof['observedAt'],
                        'maintenance_archived_changed')
                    try:
                        validate_complete_confirmation(snapshot,binding,at=retired_at,require_flat=True)
                    except (ValueError,KeyError,TypeError):
                        raise core.MaintenanceError('maintenance_archived_changed') from None
                    continue
                core.need(type(snapshot) is dict and snapshot.get('schema')=='decrypt.customer-broker-confirmation.v1'
                    and core.digest(core.canonical(snapshot))==proof['snapshotSha256']
                    and snapshot.get('sequence')==proof['sequence'] and snapshot.get('observedAt')==proof['observedAt']
                    and all(snapshot.get(k)==binding[k] for k in ('instanceId','accountId','pairId','symbol','accountMode','routeId','subscriptionId','endpointSha256'))
                    and all(snapshot.get(k) is True for k in ('positionsComplete','ordersComplete','pendingComplete','correlationComplete','routeVerified'))
                    and snapshot.get('entryRecipients')==[binding['accountId']] and type(snapshot.get('positionQuantity')) is int
                    and snapshot['positionQuantity']==0 and snapshot.get('otherContractExposure') is False
                    and all(snapshot.get(k)==[] for k in ('workingOrderIds','pendingOrderIds','unattributedOrderIds')),'maintenance_archived_changed')

    def _engine(self,binding,runtime):
        if runtime is not None and not runtime.closed:return runtime.adapter_snapshot()['engine']
        path=self.manager.state/'trading'/binding['instanceId']/'engine.sqlite'
        core.need(path.exists(),'maintenance_unreconciled')
        archived=binding['instanceId'] in self._retired_ids()
        rows=self._read_database(path,"SELECT value FROM customer_runtime_metadata WHERE key='binding'",immutable=archived)
        from decrypt_trading.runtime import canonical
        core.need(len(rows)==1 and rows[0][0]==core.digest(canonical(binding).encode()),'maintenance_unreconciled')
        rows=self._read_database(path,'SELECT body FROM state WHERE id=1',immutable=archived)
        core.need(len(rows)==1,'maintenance_unreconciled')
        return core.strict_json(rows[0][0])

    def _bridge_proofs(self,bindings):
        """A fresh flat report from the Tradovate bridge for every active native engine.

        The bridge module keeps running during maintenance: it is not part of this backup.
        Unreachable or unbound: native_safety_unknown. Reachable and bound but not flat:
        entries_must_be_held_and_native_flat.
        """
        from decrypt_trading.customer_contract import native
        from decrypt_trading.bridge_route import BindingError, binding_problem, flat, open_route
        opener=self.bridge_opener or (lambda binding:open_route(binding,start=False))
        runtimes=getattr(self.server.trading,'runtime',{}) if self.server.trading is not None else {}
        proofs=[]
        for binding in sorted((b for b in bindings if native(b)),key=lambda b:b['instanceId']):
            bridge=getattr(runtimes.get(binding['instanceId']),'bridge',None)
            try:
                route=bridge.route if bridge is not None else opener(binding).route
            except BindingError:
                raise core.MaintenanceError('native_safety_unknown') from None
            state=route.refresh()
            core.need(state is not None and binding_problem(route.expect,state) is None
                      and state.get('synced') is True and state.get('healthy') is True
                      and type(state.get('observed_at')) is int,'native_safety_unknown')
            core.need(0<=self._now()-state['observed_at']<=10000,'native_safety_stale')
            core.need(flat(route.expect,state),'entries_must_be_held_and_native_flat')
            proofs.append({'instanceId':binding['instanceId'],'observedAt':state['observed_at'],
                           'stateSha256':core.digest(core.canonical(state))})
        return proofs

    def _native_flat(self,bindings,guardian):
        proofs=self._bridge_proofs(bindings)
        # An empty scope is simply "now", not a broker assertion.
        return min([self._now()]+[proof['observedAt'] for proof in proofs])

    def _check_no_active_trade(self,bindings,trading):
        self._validate_retired()
        runtimes=getattr(trading,'runtime',{}) if trading is not None else {}
        core.need(not any(not runtime.closed for name,runtime in runtimes.items() if name in self._retired_ids()),'maintenance_unreconciled')
        known={item['instanceId'] for item in bindings}
        core.need(set(runtimes)<=known,'maintenance_unreconciled')
        root=self.manager.state/'trading'
        if root.exists():
            for child in root.iterdir():
                if child.name=='browser-profile':continue
                if child.is_dir() and (child/'engine.sqlite').exists():core.need(child.name in known,'maintenance_unreconciled')
        for binding in bindings:
            runtime=runtimes.get(binding['instanceId']);engine=self._engine(binding,runtime)
            core.need(type(engine) is dict and type(engine.get('commands')) is dict,'maintenance_unreconciled')
            trade=engine.get('trade')
            core.need(trade is None or type(trade) is dict and trade.get('phase')=='closed','maintenance_active_trade')
            core.need(all(type(command) is dict and (command.get('status') in ('filled','cancelled','rejected','superseded')
                          or command.get('status') in ('confirmed','accepted') and command.get('kind') in ('cancel','modify_stop')
                          and trade is not None and trade.get('phase')=='closed' and type(trade.get('id')) is str
                          and command.get('trade_id')==trade['id'])
                          for command in engine['commands'].values()),'maintenance_unreconciled')
        return runtimes

    def runtime_safety(self):
        core.need(self.snapshot is not None,'native_safety_unknown')
        return dict(self.snapshot)

    def _checkpoint(self):
        for name,path in self.manager._files(False).items():
            if not name.endswith('.sqlite'):continue
            if len(Path(name).parts)==3 and Path(name).parts[1] in self._retired_ids():continue
            core.regular(path)
            db=sqlite3.connect(path,timeout=5)
            try:
                result=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                core.need(result is not None and result[0]==0,'database_writer_not_stopped')
            finally:db.close()

    @contextmanager
    def quiesce(self):
        # perform() holds the exclusive API/delivery gate. Never join any worker
        # while holding a lock that worker can need during its final phase.
        trading=self.server.trading;guardian=self.server.guardian
        closed_any=False;fully_stopped=False
        self.snapshot=None
        try:
            with ExitStack() as locks:
                if trading is not None:locks.enter_context(trading.mutex)
                runtimes=getattr(trading,'runtime',{}) if trading is not None else {}
                for name in sorted(runtimes):locks.enter_context(runtimes[name].mutex)
                bindings=self._bindings()
                runtimes=self._check_no_active_trade(bindings,trading)
                # An active/uncertain trade is refused before holding or closing its supervisor.
                for runtime in runtimes.values():
                    if not runtime.closed:
                        runtime.hold();core.need(runtime.status()['entriesHeld'] is True,'entries_must_be_held_and_native_flat')
                observed=self._native_flat(self._bindings(include_retired=False),guardian)
                for runtime in runtimes.values():
                    if not runtime.closed:runtime.stopping.set();runtime.wake.set()
                if trading is not None:
                    trading.stopping.set();trading.wake.set()
                    for feed in trading.feeds.values():feed.stop_event.set()
                self.snapshot={'checkedAt':observed,'entriesHeld':True,'allNativeFlat':True,
                    'workingOrders':0,'pendingOrders':0,'unknownCommands':0,'activeInstances':len([r for r in runtimes.values() if not r.closed])}
            closed_any=True;self.restart_required=True
            if trading is not None:
                trading.close()
                core.need(trading.closed and all(runtime.closed for runtime in trading.runtime.values()),'maintenance_workers_not_stopped')
            if guardian is not None:
                guardian.close()
                core.need(guardian.worker is None or not guardian.worker.is_alive(),'maintenance_workers_not_stopped')
            self.server.trading=None;self.server.guardian=None
            fully_stopped=True
            self.snapshot['activeInstances']=0
            # Retain acquisition time. Waiting for shutdown never makes old broker data fresh.
            core.need(0<=self._now()-self.snapshot['checkedAt']<=10000,'native_safety_stale')
            self._checkpoint()
            yield
        finally:
            if fully_stopped and self.action_name=='backup':
                try:
                    core.need(self.restart_factory is not None,'maintenance_restart_required')
                    self.restart_factory()
                    self.restart_required=False
                except Exception:
                    self.restart_required=True
            if closed_any and self.action_name in COLD:
                self.restart_required=True
            self.snapshot=None

    def _reconcile_hold(self):
        path=self.manager.root/'hold.json'
        raw=core.regular(path);value=core.strict_json(raw)
        fields={'schema','reason','at','explicitNativeReconciliationRequired','automaticReplay'}
        core.need(type(value) is dict and fields<=value.keys() and value.keys()<=fields|{'transactionId'}
                  and value['schema']=='decrypt.maintenance-hold.v1' and type(value['at']) is int
                  and value['explicitNativeReconciliationRequired'] is True and value['automaticReplay'] is False,
                  'reconciliation_not_supported')
        reason=value['reason'];transaction=None;adoption=None
        if reason in ('restore_requires_native_reconciliation','interrupted_restore_recovered'):
            identifier=value.get('transactionId')
            core.need(type(identifier) is str and re.fullmatch('[a-f0-9]{32}',identifier),'reconciliation_not_supported')
            journal_raw=core.regular(self.manager.root/'transactions'/identifier/'JOURNAL.json')
            journal=core.strict_json(journal_raw)
            expected='applied' if reason=='restore_requires_native_reconciliation' else 'rolled_back'
            core.need(type(journal) is dict and journal.get('schema')=='decrypt.restore-transaction.v1'
                      and journal.get('phase')==expected,'reconciliation_transaction_unresolved')
            transaction={'transactionId':identifier,'journalSha256':core.digest(journal_raw),'phase':expected}
        elif reason in ('release_restart_requires_native_reconciliation','release_rollback_requires_native_reconciliation'):
            core.need(callable(self.adoption),'reconciliation_adoption_unverified')
            adoption=self.adoption(self.manager)
            core.need(type(adoption) is dict and set(adoption)=={'kind','version','root','manifestSha256'}
                      and adoption['kind'] in ('installed','signed') and type(adoption['manifestSha256']) is str
                      and core.HEX.fullmatch(adoption['manifestSha256']),'reconciliation_adoption_unverified')
        else:raise core.MaintenanceError('reconciliation_not_supported')
        return {'holdSha256':core.digest(raw),'reason':reason,'transaction':transaction,'adoption':adoption}

    def _reconcile_scope(self,guardian,trading):
        hold=self._reconcile_hold();bindings=self._bindings()
        runtimes=self._check_no_active_trade(bindings,trading)
        core.need(not any(not r.closed for r in runtimes.values()),'reconciliation_requires_stopped_engines')
        engines={item['instanceId']:core.digest(core.canonical(self._engine(item,None))) for item in bindings}
        if hold['transaction'] is not None:
            transaction=hold['transaction'];journal=core.strict_json(core.regular(
                self.manager.root/'transactions'/transaction['transactionId']/'JOURNAL.json'))
            prefix='after' if transaction['phase']=='applied' else 'before'
            for row in journal.get('changes',[]):
                name=row.get('path') if type(row) is dict else None
                if name=='trading/instances.sqlite' or type(name) is str and re.fullmatch(r'trading/[A-Za-z0-9_-]+/engine\.sqlite',name):
                    if prefix=='before' and row.get('hadOriginal') is False:continue
                    path=self.manager.state/name
                    current=self.manager._snapshot(path) if name.endswith('.sqlite') else core.regular(path)
                    core.need(self.manager._content_digest(name,current)==row.get(prefix+'ContentSha256'),'reconciliation_scope_changed')
        scope={**hold,'bindings':bindings,'retiredIds':sorted(self._retired_ids()),'engineSha256':engines}
        return bindings,scope,core.digest(core.canonical(scope))

    def _reconcile_evidence(self,bindings,guardian):
        native_proofs=self._bridge_proofs(bindings)
        if not native_proofs:return None
        observed=min(p['observedAt'] for p in native_proofs)
        expires=min(p['observedAt']+10000 for p in native_proofs)
        core.need(0<=self._now()-observed<=10000 and self._now()<=expires,'native_safety_stale')
        return {'observedAt':observed,'validUntil':expires,
                'observationSha256':core.digest(core.canonical({'tradovate':native_proofs}))}

    def reconciliation_status(self):
        hold=self.manager.root/'hold.json'
        if not hold.exists() and not hold.is_symlink():
            path=self.manager.root/'reconciliation.json'
            if path.exists() or path.is_symlink():
                value=core.strict_json(core.regular(path));identifier=value.get('receiptId') if type(value) is dict else None
                if value.get('state')=='reconciled' and type(identifier) is str and re.fullmatch('[a-f0-9]{32}',identifier):
                    receipt=core.strict_json(core.regular(self.manager.root/'reconciliations'/(identifier+'.json')))
                    consumed=core.regular(self.manager.root/'reconciled-holds'/(identifier+'.json'))
                    core.need(receipt.get('scopeSha256')==value.get('scopeSha256') and core.digest(consumed)==value.get('holdSha256')
                              and receipt.get('entriesHeld') is True,'reconciliation_receipt_invalid')
                    return {'state':'reconciled','available':False,'proofsCollected':len(receipt['observations'])}
            return {'state':'not_required','available':False,'proofsCollected':0}
        try:scope=self._reconcile_hold()
        except (core.MaintenanceError,OSError,ValueError,KeyError,TypeError):return {'state':'held','available':False,'proofsCollected':0}
        path=self.manager.root/'reconciliation.json'
        if path.exists() or path.is_symlink():
            value=core.strict_json(core.regular(path))
            if type(value) is dict and value.get('holdSha256')==scope['holdSha256'] and value.get('state')=='awaiting_second_observation':
                return {'state':'awaiting_second_observation','available':True,'proofsCollected':1}
        return {'state':'held','available':True,'proofsCollected':0}

    def reconcile(self):
        # No worker is opened to inspect a restored ledger. A valid marker keeps
        # dispatch fenced while two distinct reader acquisitions are collected.
        guardian,trading=self.server.guardian,self.server.trading
        with ExitStack() as locks:
            if trading is not None:locks.enter_context(trading.mutex)
            for name in sorted(getattr(trading,'runtime',{})):locks.enter_context(trading.runtime[name].mutex)
            bindings,scope,scope_hash=self._reconcile_scope(guardian,trading)
            current=self._reconcile_evidence(self._bindings(include_retired=False),guardian)
        path=self.manager.root/'reconciliation.json';previous=None
        if path.exists() or path.is_symlink():
            prior=core.strict_json(core.regular(path))
            if (type(prior) is dict and prior.get('state')=='awaiting_second_observation'
                    and prior.get('scopeSha256')==scope_hash and prior.get('holdSha256')==scope['holdSha256']):
                previous=prior.get('first')
        if current is not None:
            fresh=(type(previous) is dict and set(previous)=={'observedAt','validUntil','observationSha256'}
                   and type(previous['observedAt']) is int and type(previous['validUntil']) is int
                   and 0<=self._now()-previous['observedAt']<=10000 and self._now()<=previous['validUntil']
                   and type(previous['observationSha256']) is str and core.HEX.fullmatch(previous['observationSha256']))
            if fresh and current['observedAt']==previous['observedAt']:
                core.need(current==previous,'reconciliation_observation_changed')
                return {'status':'awaiting_second_observation','proofsCollected':1,'automaticReplay':False}
            if not fresh or current['observedAt']<previous['observedAt']:
                core.atomic(path,{'schema':'decrypt.reconciliation-progress.v1','state':'awaiting_second_observation',
                    'holdSha256':scope['holdSha256'],'scopeSha256':scope_hash,'first':current})
                return {'status':'awaiting_second_observation','proofsCollected':1,'automaticReplay':False}
            proofs=[previous,current]
        else:proofs=[]
        with self.manager._operation(flat=True):
            _,checked_scope,checked_hash=self._reconcile_scope(guardian,None)
            core.need(checked_hash==scope_hash,'reconciliation_scope_changed')
            for proof in proofs:
                core.need(0<=self._now()-proof['observedAt']<=10000 and self._now()<=proof['validUntil'],'native_safety_stale')
            receipt_id=secrets.token_hex(16)
            receipt={'schema':'decrypt.reconciliation-receipt.v1','state':'verified_before_consumption',
                'at':self._now(),'scopeSha256':scope_hash,'scope':checked_scope,'observations':proofs,
                'emptyExecutionScope':not proofs,'automaticReplay':False,'entriesHeld':True}
            receipts=core.directory(self.manager.root/'reconciliations',create=True)
            consumed=core.directory(self.manager.root/'reconciled-holds',create=True)
            core.write_new(receipts/(receipt_id+'.json'),core.canonical(receipt))
            core.sync_directory(receipts)
            hold=self.manager.root/'hold.json'
            core.need(core.digest(core.regular(hold))==scope['holdSha256'],'reconciliation_scope_changed')
            # Receipt is durable before this single atomic marker consumption.
            # An interrupted action never silently clears or recreates a marker.
            destination=consumed/(receipt_id+'.json')
            core.need(not destination.exists() and not destination.is_symlink(),'reconciliation_scope_changed')
            os.replace(hold,destination);core.sync_directory(consumed);core.sync_directory(self.manager.root)
            core.atomic(path,{'schema':'decrypt.reconciliation-progress.v1','state':'reconciled',
                'holdSha256':scope['holdSha256'],'scopeSha256':scope_hash,'receiptId':receipt_id})
        return {'status':'reconciled_held','proofsCollected':len(proofs),'reconciliationId':receipt_id,
                'automaticReplay':False,'entriesHeld':True,'restartRequired':True}

    def _staged(self):
        result=[]
        for path in sorted((self.manager.root/'restores').iterdir()):
            if not re.fullmatch('[a-f0-9]{32}',path.name) or path.is_symlink():continue
            value=core.strict_json(core.regular(path/'RECEIPT.json'))
            core.need(type(value) is dict and value.get('schema')=='decrypt.restore-stage.v1'
                      and type(value.get('files')) is list and len(value['files'])<=core.MAX_FILES
                      and type(value.get('credentialsIncluded')) is bool,'invalid_restore_stage')
            result.append({'restoreId':path.name,'fileCount':len(value['files']),
                           'credentialsIncluded':value['credentialsIncluded'],'status':'staged'})
        return result

    def _transactions(self):
        result=[]
        for path in sorted((self.manager.root/'transactions').iterdir()):
            if not re.fullmatch('[a-f0-9]{32}',path.name) or path.is_symlink():continue
            value=core.strict_json(core.regular(path/'JOURNAL.json'))
            core.need(type(value) is dict and value.get('schema')=='decrypt.restore-transaction.v1'
                      and value.get('phase') in ('prepared','recovery_required','applied','rolled_back'),'invalid_restore_transaction')
            if value['phase'] in ('prepared','recovery_required'):
                result.append({'transactionId':path.name,'phase':value['phase']})
        return result

    def status(self):
        return {**self.manager.status(),'backups':self.manager.list_backups(),'restores':self._staged(),
                'transactions':self._transactions(),'lastOperation':self.last_operation,
                'restartRequired':self.restart_required,'activeOperation':self.action_name,
                'restoredTradingReconciliationImplemented':True,'reconciliation':self.reconciliation_status()}

    def perform(self,action,data):
        core.need(action in FIELDS,'maintenance_action_unknown')
        required,optional=FIELDS[action]
        core.need(type(data) is dict and required<=data.keys() and data.keys()<=required|optional,'invalid_fields')
        if 'passphrase' in data:
            core.need(type(data['passphrase']) is str and 12<=len(data['passphrase'])<=1024,'backup_passphrase_required')
        if 'includeCredentials' in data:core.need(type(data['includeCredentials']) is bool,'invalid_fields')
        with self.server.operation_gate.exclusive(),self.server.action_lock:
            core.need(not self.restart_required,'maintenance_restart_required')
            self.action_name=action
            try:
                if action=='backup':result=self.manager.create_backup(data['passphrase'],data.get('includeCredentials',False))
                elif action=='restore/prepare':result=self.manager.prepare_restore(data['backupId'],data['passphrase'])
                elif action=='restore/apply':result=self.manager.apply_restore(data['restoreId'])
                elif action=='restore/recover':result=self.manager.recover_restore(data['transactionId'])
                elif action=='restore/reconcile':result=self.reconcile()
                elif action=='release/activate':result=self.manager.activate_release(data['version'])
                elif action=='release/rollback':result=self.manager.rollback_release()
                else:result=self.manager.offboard(data['confirmation'])
                if action=='backup':result={**result,'servicesRestartedHeld':not self.restart_required,'credentialsRemainLocked':True,'feedReconnectMayBeRequired':True}
                result={**result,'restartRequired':self.restart_required or result.get('restartRequired',False)}
                self.last_operation={'action':action,'at':self._now(),'result':result}
                core.atomic(self.receipt,{'schema':'decrypt.maintenance-operation.v1',**self.last_operation})
                self.action_name=None
                return {'result':result,'maintenance':self.status()}
            finally:self.action_name=None
