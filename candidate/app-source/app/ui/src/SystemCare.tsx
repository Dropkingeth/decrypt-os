import {useCallback, useEffect, useRef, useState} from 'react';
import type {FormEvent} from 'react';
import {api, ApiError} from './api';
import './SystemCare.css';

type NotificationEvent = {id:number; source:string; kind:string; created:number; status:string; attempts:number; next_attempt:number; last_outcome:string|null};
type Notifications = {enabled:boolean; pending:number; events:NotificationEvent[]; userRead:false; receiptSemantics:string};
type Calendar = {state:string; reason:string; entryAllowed:boolean; expectedSymbol:string|null; coverageEnds:string|null; managementAllowed:boolean; checkedAt:number; validUntil:number|null};
type TradingStatus = {enabled:boolean; notifications?:Notifications; instances:{instanceId:string; binding:{symbol:string}; calendar?:Calendar}[]};
type Restore = {restoreId:string; fileCount?:number; credentialsIncluded?:boolean; status?:string};
type Transaction = {transactionId:string; phase:string};
type Maintenance = {schema:string; backupCount:number; backups:{backupId:string; bytes:number}[]; restores?:Restore[]; transactions?:Transaction[]; stagedVersions:string[]; currentVersion:string; nativeReconciliationRequired:boolean; restartRequired?:boolean; largeArchiveTransfer:string; installedBaselineConfigured:boolean; lastOperation?:unknown; restoredTradingReconciliationImplemented?:boolean; reconciliation?:{state:string;available:boolean;proofsCollected:number}};
type Result = {status?:string; backupId?:string; restoreId?:string; fileCount?:number; credentialsIncluded?:boolean; restartRequired?:boolean; browserSignoutRequired?:boolean; providerRevocationRequired?:boolean; ledgersPreserved?:boolean};
type MaintenanceReply = {maintenance:Maintenance; result?:Result};
type Run = (action:string, body:Record<string,unknown>) => Promise<boolean>;
const errors:Record<string,string> = {
  native_safety_unknown:'Fresh broker evidence is missing. Confirm the account is flat in Guardian before maintenance.',
  native_safety_stale:'The broker reading expired. Refresh Guardian, then try again.',
  entries_must_be_held_and_native_flat:'Hold new entries and confirm the account has no position or working orders before continuing.',
  workers_or_native_orders_not_clear:'The engine or native orders are not clear yet. Finish or reconcile the trade before maintenance.',
  maintenance_active_trade:'An active trade still needs supervision. Finish or reconcile it before changing local state.',
  maintenance_retired_engine_active:'An installed engine is retired: its strategy now uses the new bridge format. Retire it in Execution runtime first.',
  maintenance_unreconciled:'The account or order history is unresolved. Review Guardian and the engine before maintenance.',
  maintenance_restart_required:'Maintenance already requires an app restart. Restart, then complete the listed reconciliation checks.',
  backup_unlock_failed:'The archive could not be unlocked. Check its passphrase and try again.',
  invalid_passphrase:'Use a passphrase between 12 and 256 characters.',
  newer_state_preserved:'Newer local records were preserved. This archive cannot overwrite them; review the restore with support.',
  release_sequence_not_new:'That release is not newer than the installed release. Use rollback for an available previous version.',
  no_previous_release:'There is no verified previous release to roll back to.',
  offboarding_confirmation_required:'Enter REMOVE SAVED SIGN-INS to confirm local credential removal.',
  restore_reconciliation_required:'Restored records still need fresh broker reconciliation. Trading remains held.',
  reconciliation_not_supported:'This hold cannot be resolved here. Preserve the records and review the required recovery with support.',
  reconciliation_transaction_unresolved:'A restore transaction is unresolved. Complete its recorded recovery before reconciling.',
  reconciliation_scope_changed:'The records or watched account changed. Refresh and review the restored setup before trying again.',
  reconciliation_identity_mismatch:'The account or installation does not match the restored records. Do not rebind it to clear this hold.',
  reconciliation_route_unverified:'The original broker route is not verified. Check the account and route in Guardian first.',
  reconciliation_requires_stopped_engines:'Stop the held engine workers before checking restored records. Active trades must keep their supervisor.',
};
const calendarReasons:Record<string,string> = {
  reviewed_regular_session:'Calendar permits new entries; strategy, broker and feed checks still apply.',
  daily_maintenance:'New entries are held during the regular maintenance break.',
  weekend:'New entries are held for the weekend session break.',
  conservative_holiday_policy_hold:'New entries are held by the conservative holiday policy. This is not a claim that CME is closed all day.',
  contract_roll_required:'The reviewed roll date has arrived. Move to the displayed dated contract through a reviewed setup; no automatic switch is performed.',
  contract_not_current:'This is not the dated contract selected by the reviewed calendar.',
  calendar_outside_reviewed_horizon:'The date is outside the reviewed calendar. New entries stay held until a verified update is installed.',
  exact_dated_contract_required:'Choose the exact dated MNQ contract. Continuous symbols are not accepted.',
  calendar_unavailable:'The reviewed calendar is unavailable. New entries stay held.',
};
function time(value:number) {return Number.isFinite(value) && value > 0 ? new Date(value).toLocaleString() : 'Not recorded';}
function short(value:string) {return value.length > 14 ? value.slice(0,8)+'…'+value.slice(-4) : value;}
function delivery(value:string) {return ({accepted:'Accepted by desktop · read status unknown', pending:'Queued', retry:'Delivery failed · retry scheduled', sending:'Delivery in progress', exhausted:'Delivery failed · retry limit reached', unknown:'Delivery uncertain · not resubmitted', superseded:'Superseded by newer status'} as Record<string,string>)[value] || 'Delivery unconfirmed';}
function resultValid(action:string, value:Result|undefined) {
  if(!value)return false;
  if(action==='backup')return typeof value.backupId==='string'&&/^[a-f0-9]{32}$/.test(value.backupId);
  if(action==='restore/prepare')return value.status==='staged'&&typeof value.restoreId==='string'&&/^[a-f0-9]{32}$/.test(value.restoreId);
  if(action==='restore/reconcile')return value.status==='awaiting_second_observation'||value.status==='reconciled_held';
  return value.status===({'restore/apply':'restored_held','restore/recover':'rolled_back_held','release/activate':'activated_for_restart','release/rollback':'rolled_back_for_restart',offboard:'local_credentials_removed_held'} as Record<string,string>)[action];
}
function tradingValid(value:TradingStatus) {return value && typeof value.enabled==='boolean' && Array.isArray(value.instances);}
function maintenanceValid(value:Maintenance) {return value && value.schema==='decrypt.maintenance-status.v1' && Array.isArray(value.backups) && Array.isArray(value.stagedVersions);}

function BackupForm({busy,run}:{busy:boolean;run:Run}) {
  const [passphrase,setPassphrase]=useState(''),[confirmation,setConfirmation]=useState(''),[credentials,setCredentials]=useState(false),[error,setError]=useState('');
  async function submit(event:FormEvent) {
    event.preventDefault(); const secret=passphrase,match=secret===confirmation;
    setPassphrase('');setConfirmation('');setError('');
    if(!match){setError('The passphrases did not match. Enter them again.');return;}
    if(await run('backup',{passphrase:secret,includeCredentials:credentials}))setCredentials(false);
  }
  return <form onSubmit={event=>void submit(event)} autoComplete="off">
    <p>Make a local encrypted archive of supported app settings and records. Keep its passphrase separately; it cannot be recovered.</p>
    <div className="care-fields"><div><label htmlFor="care-backup-passphrase">New archive passphrase</label><input id="care-backup-passphrase" type="password" autoComplete="new-password" minLength={12} maxLength={256} required disabled={busy} value={passphrase} onChange={e=>setPassphrase(e.target.value)}/></div><div><label htmlFor="care-backup-confirm">Confirm archive passphrase</label><input id="care-backup-confirm" type="password" autoComplete="new-password" minLength={12} maxLength={256} required disabled={busy} value={confirmation} onChange={e=>setConfirmation(e.target.value)}/></div></div>
    <label className="checkbox"><input type="checkbox" checked={credentials} onChange={e=>setCredentials(e.target.checked)} disabled={busy}/><span>Include saved encrypted credentials. Anyone with this archive and its passphrase may recover them.</span></label>
    <p className="field-hint">Browser sessions are not included. Passphrases are cleared when submitted.</p>
    {error&&<p role="alert" className="error">{error}</p>}
    <button className="secondary" disabled={busy}>Create encrypted backup</button>
  </form>;
}

function RestoreForm({status,busy,run}:{status:Maintenance;busy:boolean;run:Run}) {
  const [backup,setBackup]=useState(''),[passphrase,setPassphrase]=useState(''),[restore,setRestore]=useState(''),[ack,setAck]=useState(false);
  const knownBackup=status.backups.some(item=>item.backupId===backup),knownRestore=status.restores?.some(item=>item.restoreId===restore);
  async function prepare(event:FormEvent) {event.preventDefault();const secret=passphrase;setPassphrase('');await run('restore/prepare',{backupId:backup,passphrase:secret});}
  return <div>
    <form onSubmit={event=>void prepare(event)} autoComplete="off">
      <p>Preparing checks and stages an archive. It does not replace current records.</p>
      <label htmlFor="care-backup-select">Saved archive</label><select id="care-backup-select" value={backup} onChange={e=>setBackup(e.target.value)} disabled={busy||!status.backups.length} required><option value="">Choose an archive</option>{status.backups.map(item=><option key={item.backupId} value={item.backupId}>{short(item.backupId)} · {(item.bytes/1024).toFixed(1)} KB</option>)}</select>
      {!status.backups.length&&<p className="field-hint">No local archives are available. Create one or import an existing encrypted archive locally.</p>}
      <label htmlFor="care-restore-passphrase">Restore archive passphrase</label><input id="care-restore-passphrase" type="password" autoComplete="off" maxLength={256} value={passphrase} onChange={e=>setPassphrase(e.target.value)} required disabled={busy||!knownBackup}/>
      <button className="secondary" disabled={busy||!knownBackup}>Prepare restore</button>
    </form>
    {!!status.restores?.length&&<form onSubmit={async event=>{event.preventDefault();if(await run('restore/apply',{restoreId:restore}))setAck(false);}}>
      <label htmlFor="care-restore-select">Verified staged restore</label><select id="care-restore-select" value={restore} onChange={e=>{setRestore(e.target.value);setAck(false);}} required disabled={busy}><option value="">Choose staged records</option>{status.restores.map(item=><option key={item.restoreId} value={item.restoreId}>{short(item.restoreId)}{item.fileCount===undefined?'':' · '+item.fileCount+' files'}</option>)}</select>
      <p>Applying a restore requires held entries, a fresh flat account and stopped workers. Newer records are protected. The app will require a restart and fresh reconciliation.</p>
      <label className="checkbox"><input type="checkbox" checked={ack} onChange={e=>setAck(e.target.checked)} disabled={busy} required/><span>I want to apply these staged records and keep trading held for reconciliation.</span></label><button className="secondary" disabled={busy||!knownRestore||!ack}>Apply staged restore</button>
    </form>}
  </div>;
}

function ReleaseActions({status,busy,run}:{status:Maintenance;busy:boolean;run:Run}) {
  const [version,setVersion]=useState(''),[confirm,setConfirm]=useState<'activate'|'rollback'|null>(null);
  return <div><p>Only locally staged, signature-verified releases appear here. Activation and rollback require held, flat and stopped checks. A restart does not enable trading.</p>
    <label htmlFor="care-release-select">Verified staged release</label><select id="care-release-select" value={version} onChange={e=>{setVersion(e.target.value);setConfirm(null);}} disabled={busy||!status.stagedVersions.length}><option value="">Choose a release</option>{status.stagedVersions.map(item=><option value={item} key={item}>{item}</option>)}</select>
    {!status.stagedVersions.length&&<p className="field-hint">No verified updates are staged. Transfer a signed release locally, then refresh.</p>}
    <div className="care-actions"><button className="secondary" disabled={busy||!status.stagedVersions.includes(version)} onClick={()=>setConfirm('activate')}>Review activation</button><button className="text-button" disabled={busy||status.currentVersion==='installed'} onClick={()=>setConfirm('rollback')}>Review rollback</button></div>
    {confirm&&<div className="callout"><p>{confirm==='activate'?'Activate '+version+' for the next app restart?':'Return to the verified previous release? Trading records are not rewound.'}</p><div className="care-actions"><button className="secondary" disabled={busy} onClick={async()=>{if(await run(confirm==='activate'?'release/activate':'release/rollback',confirm==='activate'?{version}:{}))setConfirm(null);}}>{confirm==='activate'?'Activate verified release':'Roll back verified release'}</button><button className="text-button" disabled={busy} onClick={()=>setConfirm(null)}>Cancel</button></div></div>}
  </div>;
}

export function SystemCare() {
  const [trading,setTrading]=useState<TradingStatus|null>(null),[maintenance,setMaintenance]=useState<Maintenance|null>(null);
  const [tradeError,setTradeError]=useState(''),[maintenanceError,setMaintenanceError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
  const [offboard,setOffboard]=useState(''),[recovery,setRecovery]=useState(''),[recoveryAck,setRecoveryAck]=useState(false);
  const [reconcileAck,setReconcileAck]=useState(false);
  const generation=useRef(0),submitting=useRef(false);
  const [clock,setClock]=useState(Date.now());
  useEffect(()=>{const timer=setInterval(()=>setClock(Date.now()),1000);return()=>clearInterval(timer);},[]);
  const refresh=useCallback(async()=>{
    const current=generation.current;
    const results=await Promise.allSettled([api<TradingStatus>('/trading/status',{}),api<MaintenanceReply>('/maintenance')]);
    if(current!==generation.current)return;
    const [trade,care]=results;
    if(trade.status==='fulfilled'&&tradingValid(trade.value)){setTrading(trade.value);setTradeError('');}
    else{setTrading(null);setTradeError('Health and calendar status are unavailable. Previous readings are not current.');}
    if(care.status==='fulfilled'&&maintenanceValid(care.value.maintenance)){setMaintenance(care.value.maintenance);setMaintenanceError('');}
    else{setMaintenance(null);setMaintenanceError('Backup and maintenance status are unavailable. No action is confirmed.');}
  },[]);
  useEffect(()=>{void refresh();const timer=setInterval(()=>{if(!submitting.current)void refresh();},10000);return()=>{generation.current++;clearInterval(timer);};},[refresh]);
  async function run(action:string,body:Record<string,unknown>) {
    if(submitting.current)return false;
    submitting.current=true;setBusy(true);setNotice('');setMaintenanceError('');const current=++generation.current;
    try{
      const reply=await api<MaintenanceReply>('/maintenance/'+action,body);
      if(!maintenanceValid(reply.maintenance)||!resultValid(action,reply.result))throw Error();
      if(current!==generation.current)return false;
      setMaintenance(reply.maintenance);
      const messages:Record<string,string>={backup:'Encrypted archive created locally. Copy it to a separate location using the local transfer tool.', 'restore/prepare':'Archive checked and staged. Current records have not been replaced.', 'restore/apply':'Restore applied. Restart the app, then complete fresh native reconciliation. Trading remains held.', 'restore/recover':'Interrupted restore recovery completed. Restart and reconcile before considering new entries.', 'release/activate':'Verified release selected for restart. Trading remains held.', 'release/rollback':'Previous verified release selected for restart. Trading records were not rewound.',offboard:'Saved local credentials removed. Trading records and encrypted backups remain. Browser sign-out and provider revocation still require separate completion.'};
      setNotice(messages[action]||'Maintenance action confirmed. Review the updated status.');
      if(action==='restore/reconcile')setNotice(reply.result?.status==='awaiting_second_observation'?'First broker reading retained. Wait for Guardian to acquire a different fresh reading, then check again. Trading remains held.':'Restored records verified. Restart the app; engines remain held. This did not enable entries or replay orders.');
      setOffboard('');return true;
    }catch(cause){if(current===generation.current){setMaintenance(null);setMaintenanceError(cause instanceof ApiError?(errors[cause.code]||cause.message):'The maintenance action was not confirmed. Refresh before trying again.');}return false;}
    finally{submitting.current=false;if(current===generation.current)setBusy(false);}
  }
  async function notifications(enabled:boolean) {
    if(submitting.current)return;
    submitting.current=true;setBusy(true);setTradeError('');const current=++generation.current;
    try{const reply=await api<TradingStatus>('/trading/configure-notifications',{enabled});if(!tradingValid(reply))throw Error();if(current===generation.current){setTrading(reply);setNotice(enabled?'Desktop alerts enabled. Delivery and read status are separate; review receipts below.':'Desktop alerts disabled. Local health history is retained.');}}
    catch{if(current===generation.current){setTrading(null);setTradeError('The alert preference was not confirmed. Refresh its status before trying again.');}}
    finally{submitting.current=false;if(current===generation.current)setBusy(false);}
  }
  const alerts=trading?.notifications;
  return <section className="panel system-care" aria-labelledby="system-care-heading">
    <div className="section-title"><div><h2 id="system-care-heading">System care</h2><p>Health alerts, reviewed trading dates and local recovery.</p></div><button className="secondary" disabled={busy} onClick={()=>void refresh()}>Refresh system care</button></div>
    {notice&&<p className="notice" role="status">{notice}</p>}
    <div className="care-status-grid">
      <section aria-labelledby="care-alert-heading"><h3 id="care-alert-heading">Desktop health alerts</h3>
        {tradeError&&<p className="error" role="alert">{tradeError}</p>}
        {!tradeError&&alerts?<><p>{alerts.enabled?'Enabled on this desktop':'Off until you enable them'}. Alerts report observed outages and recovery, not trading permission.</p><button className="secondary" disabled={busy} onClick={()=>void notifications(!alerts.enabled)}>{alerts.enabled?'Disable desktop alerts':'Enable desktop alerts'}</button><p className="field-hint">An accepted notification is not proof you saw it. A stopped machine or backend cannot report through its own health monitor.</p>
          <details><summary>Delivery receipts{alerts.events.length?' · '+alerts.events.length:''}</summary>{alerts.events.length?<ol className="care-receipts">{alerts.events.map(item=><li key={item.id}><strong>{item.source.split(':')[0]} · {item.kind==='recovery'?'recovery':'outage'}</strong><span>{delivery(item.status)}</span><time dateTime={new Date(item.created).toISOString()}>{time(item.created)}</time>{item.status==='retry'&&<span>Attempt {item.attempts} of 5 · next retry {time(item.next_attempt)}</span>}</li>)}</ol>:<p>No delivery transitions have been recorded. Initial unconfigured setup does not send warnings.</p>}</details>
        </>:!tradeError&&<p className="field-hint">Desktop alert status is not available on this installation.</p>}
      </section>
      <section aria-labelledby="care-calendar-heading"><h3 id="care-calendar-heading">Trading calendar</h3>
        {!trading?<p className="field-hint">Calendar checks are unavailable.</p>:!trading.instances.length?<p>No installed engine has a calendar check yet.</p>:trading.instances.map(item=><article className="care-calendar" key={item.instanceId}><strong>{item.binding.symbol}</strong><p>{item.calendar?(item.calendar.validUntil!==null&&item.calendar.checkedAt<=clock&&clock<item.calendar.validUntil?(calendarReasons[item.calendar.reason]||'Calendar status is unconfirmed. New entries should remain held.'):'This calendar reading has expired. Refresh before relying on its entry check.'):'No calendar result is available for this engine.'}</p>{item.calendar&&<dl><div><dt>Reviewed contract</dt><dd>{item.calendar.expectedSymbol||'Unknown'}</dd></div><div><dt>Coverage through</dt><dd>{item.calendar.coverageEnds||'Unknown'}</dd></div></dl>}</article>)}
        <p className="field-hint">This gate holds new entries only. Existing-trade management and each strategy’s own session rules are unchanged. Contracts never switch automatically.</p>
      </section>
    </div>
    <section className="care-maintenance" aria-labelledby="care-maintenance-heading"><div className="section-title"><h3 id="care-maintenance-heading">Backup and maintenance</h3>{maintenance&&<span className="pill muted">{maintenance.currentVersion==='installed'?(maintenance.installedBaselineConfigured?'Installed baseline':'Development build'):`Selected release ${maintenance.currentVersion}`}</span>}</div>
      {maintenanceError&&<p className="error" role="alert">{maintenanceError}</p>}
      {maintenance&&<>
        {(maintenance.nativeReconciliationRequired||maintenance.restartRequired)&&<p className="callout" role="status">{maintenance.restartRequired?'Restart required. ':''}{maintenance.nativeReconciliationRequired?'Trading remains held until the restored records pass reconciliation. A restart alone does not clear this hold.':''}</p>}
        {maintenance.nativeReconciliationRequired&&(maintenance.restoredTradingReconciliationImplemented===true&&maintenance.reconciliation?.available===true?<form className="care-reconcile" onSubmit={async event=>{event.preventDefault();if(await run('restore/reconcile',{}))setReconcileAck(false);}}><h4>Check restored records</h4><p>This checks supported idle or closed records against the original account and route. An account with trading records needs two different fresh broker readings. Active or unresolved trades are refused. An empty execution setup makes no broker-flat claim.</p>{maintenance.reconciliation.state==='awaiting_second_observation'&&<p role="status">First reading retained. Guardian must acquire a different fresh reading before the second check.</p>}<label className="checkbox"><input type="checkbox" checked={reconcileAck} onChange={e=>setReconcileAck(e.target.checked)} disabled={busy||maintenance.restartRequired} required/><span>Check these restored records without enabling entries or replaying orders.</span></label><button className="secondary" disabled={busy||maintenance.restartRequired||!reconcileAck}>{maintenance.reconciliation.state==='awaiting_second_observation'?'Check second broker reading':'Check restored records'}</button></form>:<p className="field-hint">This hold is not available for automatic reconciliation. Preserve the records and complete the required recovery with support.</p>)}
        <details><summary>Create encrypted backup</summary><BackupForm busy={busy} run={run}/></details>
        <details><summary>Restore saved records · {maintenance.backups.length} local archives</summary><RestoreForm status={maintenance} busy={busy} run={run}/></details>
        {!!maintenance.transactions?.length&&<details><summary>Recover an interrupted restore</summary><form onSubmit={async event=>{event.preventDefault();if(await run('restore/recover',{transactionId:recovery}))setRecoveryAck(false);}}><p>Only recorded interrupted transactions are shown. Recovery preserves newer records and keeps trading held.</p><label htmlFor="care-transaction-select">Interrupted transaction</label><select id="care-transaction-select" value={recovery} onChange={e=>{setRecovery(e.target.value);setRecoveryAck(false);}} required disabled={busy}><option value="">Choose a transaction</option>{maintenance.transactions.map(item=><option key={item.transactionId} value={item.transactionId}>{short(item.transactionId)} · {item.phase.replaceAll('_',' ')}</option>)}</select><label className="checkbox"><input type="checkbox" checked={recoveryAck} onChange={e=>setRecoveryAck(e.target.checked)} required disabled={busy}/><span>Recover this recorded transaction and keep trading held.</span></label><button className="secondary" disabled={busy||!recoveryAck||!maintenance.transactions.some(item=>item.transactionId===recovery)}>Recover interrupted restore</button></form></details>}
        <details><summary>Update or roll back the app</summary><ReleaseActions status={maintenance} busy={busy} run={run}/></details>
        <details><summary>Transfer archives or signed updates</summary><p>Use the installed local recovery tool for large files. This screen accepts known archive and release IDs only; it does not upload files or accept local paths.</p><ul><li>Export a saved encrypted backup to a separate storage location.</li><li>Import an encrypted archive, then select its verified staged restore above.</li><li>Stage an official signed release, refresh this screen, then review activation.</li></ul><p className="field-hint">In the local terminal, run <code>/usr/local/lib/decrypt-os-runtime/python/bin/python -E -s -B /usr/local/lib/decrypt-os/installer/maintenance_cli.py --help</code> for transfer commands. Never put a passphrase in a command line or message.</p></details>
        <details className="care-offboard"><summary>Remove saved local sign-ins</summary><form onSubmit={event=>{event.preventDefault();void run('offboard',{confirmation:offboard});setOffboard('');}}><p>This removes supported local credentials after held, flat and stopped checks. It preserves trading records and encrypted backups. Browser sign-out and provider revocation are separate tasks.</p><label htmlFor="care-offboard-confirm">Type REMOVE SAVED SIGN-INS to confirm</label><input id="care-offboard-confirm" value={offboard} onChange={e=>setOffboard(e.target.value)} autoComplete="off" maxLength={32} disabled={busy}/><button className="secondary" disabled={busy||offboard!=='REMOVE SAVED SIGN-INS'}>Remove local sign-ins</button></form></details>
      </>}
    </section>
  </section>;
}
