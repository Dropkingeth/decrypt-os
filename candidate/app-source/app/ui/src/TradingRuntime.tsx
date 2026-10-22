import {useCallback, useEffect, useRef, useState} from 'react';
import type {FormEvent} from 'react';
import {api, ApiError, type BridgeLogin} from './api';
import RuntimeSignalAccess from './RuntimeSignalAccess';
import type {PrivateIngress, SignalKey} from './RuntimeSignalAccess';
import NativeBridge from './NativeBridge';
import type {BridgeStatus} from './NativeBridge';

type Instance = {instanceId: string; route?: string | null; runtimeState: string; entriesHeld: boolean; entryReady: boolean; nativeFresh: boolean; tradePhase: string; tp1State?:string; sizeLockEnabled?: boolean; pendingCommands: number | null; blockers: string[]; feed?: {state: string; nativeVerified: boolean; reason?: string | null}; binding: {loginGroupId?:string;accountId: string; symbol: string; pairId: string; provider?: string; accountMode?: string; nativeAccountId?: number; nativeAccountName?: string; streamId: string; feedId: string}};
type Package = {pairId: string; label: string; version: string; engineId: string; tp1OffsetMode: 'explicit' | 'zero'};
type RetiredInstance = {binding: Instance['binding'] & {instanceId: string}; retiredAt: number; retirementBasis: string};
export type Status = {bridge?:BridgeStatus; enabled: boolean; mode: string; package: Package | null; packages: Package[]; account: {accountLabel: string; nativeAccountId: string; symbol: string; routeConfigured: boolean} | null; instances: Instance[]; retiredInstances?: RetiredInstance[]; ingress?: {state: string; publicOrigin?: string; publicTlsVerified: boolean}; ingressKeys?: {locked: boolean; producers: SignalKey[] | null}; oneTimeIngress?: PrivateIngress; bridgeLogin?: BridgeLogin; health: {source: string; state: string; changed: number}[]; events?: {source: string; state: string; observed: number}[]};
const blockers: Record<string, string> = {
  bridge_not_configured: 'Set up the Tradovate bridge for this account first (Connections).',
  bridge_route_soup_only: 'The app runs only the Soup engine on the Tradovate bridge. This engine cannot trade from here.',
  bridge_soup_account_mismatch: 'The bridge is set up for a different account number.',
  bridge_account_name_mismatch: 'The bridge is set up for a different Tradovate account name.',
  bridge_contract_mismatch: 'The bridge is set up for a different contract.',
  bridge_publish_mode_mismatch: 'The bridge must publish broker events to its adapter database.',
  bridge_env_not_private: 'The bridge token file must be private to your user (mode 600).',
  bridge_token_invalid: 'The bridge token file is missing its order token.',
  bridge_route_unavailable: 'The Tradovate bridge route is not connected.',
  bridge_state_unavailable: 'No fresh account reading from the Tradovate bridge. Check that it is on and signed in (workspace 3).',
  bridge_tick_failed: 'Reading the bridge failed. Check the Bridge window.',
  contract_roll_required: 'This dated contract has reached its reviewed roll date. Reconcile it before setting up the next contract.',
  contract_not_current: 'The configured contract does not match the reviewed current MNQ contract.',
  calendar_outside_reviewed_horizon: 'The contract calendar needs a reviewed update before new entries.',
  calendar_unavailable: 'The contract calendar could not be verified.',
  retired_engine: 'Retired engine: this strategy now uses the new bridge format. Retire this instance.',
  daily_maintenance: 'New entries are held during the daily exchange maintenance window.',
  weekend: 'New entries are held during the weekend closure.',
  conservative_holiday_policy_hold: 'The reviewed holiday policy holds new entries for this trading date.',
  restore_reconciliation_required: 'Restored trading records need broker reconciliation before the engine can reopen.',
};
const notices: Record<string, string> = {
  install: 'Engine installed with entries held. Nothing trades until its checks pass and you resume entries in Bridge Advanced.',
  retire: 'Engine retired. Its original ledger is retained and incoming alerts cannot reopen it.',
  reactivate: 'Engine returned to active setups without starting a worker. Start held, reconnect and review its current checks.',
  'enable-entries': 'Entry controls updated. Fresh account and price checks still apply.',
  'configure-ingress': 'The local receiver is configured. Your public HTTPS proxy still needs verification.',
  'configure-feed': 'The customer market feed is starting. New entries remain held until fresh data and account checks pass.',
  'configure-size-lock': 'Size Lock updated. The engine stays held; the new sizing policy applies from the next trade.',
  'rotate-signal-key': 'Alert key replaced. The old address is rejected. Update the intended TradingView alert and keep entries held while reviewing delivery.',
  'create-signal-key': 'Private alert address ready. Copy it to your intended TradingView alert, then dismiss it.',
  'show-signal-address': 'Private alert address ready. Copy it to your intended TradingView alert, then dismiss it.',
};
function valid(value: Status) {return value && typeof value.enabled === 'boolean' && Array.isArray(value.instances) && Array.isArray(value.health);}

function MarketFeedSetup({instance, busy, run}: {instance: Instance; busy: boolean; run: (action: string, body: Record<string, unknown>) => Promise<boolean>}) {
  const [chartUrl, setChartUrl] = useState(''), [resumeOnLogin, setResumeOnLogin] = useState(false);
  const id = 'runtime-chart-' + instance.instanceId;
  return <details className="runtime-install"><summary>Market feed · {instance.feed?.nativeVerified ? 'Fresh native updates' : instance.feed?.state?.replaceAll('_', ' ') || 'Not started'}</summary>
    <p>Open your own saved chart for {instance.binding.symbol}. Use standard candles and a 1-second timeframe with real-time CME data. TradingView must support 1-second charts on your plan.</p>
    <p className="field-hint">This uses a separate customer market browser. Sign in there when it opens. A loaded chart alone does not count as a fresh quote. Reconnecting keeps new entries held.</p>
    {instance.feed?.state === 'requires_retry' && <p className="callout">The feed could not verify fresh native updates. Check sign-in, dated symbol, real-time data and timeframe in the market browser, then reconnect.</p>}
    <form onSubmit={event => {event.preventDefault(); void run('configure-feed', {instanceId: instance.instanceId, chartUrl, resumeOnLogin});}}>
      <label htmlFor={id}>Your saved market chart</label><input id={id} type="url" value={chartUrl} onChange={event => setChartUrl(event.target.value)} placeholder="https://www.tradingview.com/chart/…/" maxLength={120} required disabled={busy}/>
      <label className="checkbox"><input type="checkbox" checked={resumeOnLogin} onChange={event => setResumeOnLogin(event.target.checked)} disabled={busy}/><span>Reopen this market feed after I log in to the OS. New entries still start held.</span></label>
      <button className="secondary" disabled={busy}>Connect market feed</button>
    </form>
  </details>;
}

export function TradingRuntime({onConnections, onStatus}: {onConnections: () => void; onStatus?: (status: Status | null) => void}) {
  const [status, setStatus] = useState<Status | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [login, setLogin] = useState<BridgeLogin | null>(null);
  const [confirmEnable, setConfirmEnable] = useState<string | null>(null);
  const [confirmArchive, setConfirmArchive] = useState<{id: string; action: 'retire' | 'reactivate'} | null>(null);
  const [publicOrigin, setPublicOrigin] = useState(''), [ownsOrigin, setOwnsOrigin] = useState(false), [privateAddress, setPrivateAddress] = useState<PrivateIngress | null>(null);
  const generation = useRef(0), submitting = useRef(false);
  const publish = useCallback((value: Status | null) => { setStatus(value); onStatus?.(value); }, [onStatus]);
  const refresh = useCallback(async () => {
    const current = generation.current;
    try {
      const result = await api<Status>('/trading/status', {});
      if (!valid(result)) throw Error();
      if (generation.current === current) {publish(result); setError(''); if (result.ingressKeys?.locked) {setPrivateAddress(null);}}
    } catch {
      if (generation.current === current) {publish(null); setPrivateAddress(null); setError('Runtime status is unavailable. Previous readings are not current.');}
    }
  }, [publish]);
  // What the bridge sees after you sign in: the account list for the install form. Read only.
  const checkLogin = useCallback(async () => {
    try { const result = await api<Status>('/trading/bridge-login', {}); setLogin(result.bridgeLogin || null); } catch { setLogin(null); }
  }, []);
  const configured = status?.bridge?.configured === true;
  useEffect(() => { if (configured) void checkLogin(); else setLogin(null); }, [configured, checkLogin]);
  useEffect(() => {void refresh(); const interval = setInterval(() => {if (!submitting.current) void refresh();}, 5000); return () => {generation.current++; clearInterval(interval);};}, [refresh]);
  async function run(action: string, body: Record<string, unknown>) {
    submitting.current = true; setBusy(true); setError(''); setNotice('');
    const current = ++generation.current;
    try {
      const result = await api<Status>('/trading/' + action, body);
      if (!valid(result)) throw Error();
      if (generation.current !== current) return false;
      if (result.oneTimeIngress) setPrivateAddress(result.oneTimeIngress);
      const {oneTimeIngress: _private, ...ordinary} = result;
      publish(ordinary); setNotice(notices[action] || 'New entries are held. Existing-trade management is not disabled.');
      return true;
    } catch (cause) {
      if (generation.current === current) {setError(cause instanceof ApiError ? cause.message : 'The action was not confirmed. Refresh status before trying it again.'); publish(null); setPrivateAddress(null);}
      return false;
    } finally {
      submitting.current = false;
      if (generation.current === current) setBusy(false);
    }
  }
  const packages = status?.packages || (status?.package ? [status.package] : []);
  return <section className="panel trading-runtime" aria-labelledby="trading-runtime-heading">
    <div className="section-title"><h2 id="trading-runtime-heading">Execution runtime</h2><span className="pill muted">{status?.instances.length ? `${status.instances.length} installed` : 'Nothing installed'}</span></div>
    <p>Installed engines keep their own trading records and start with new entries held after every restart. Nothing trades until the checks below pass and you resume entries in Bridge Advanced.</p>
    {error && <p className="error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    {!status ? <button className="secondary" disabled={busy} onClick={() => void refresh()}>Refresh runtime status</button> : !status.enabled ? <p className="callout">The runtime service is not enabled on this installation. No running-engine status is available here.</p> : <>
      <NativeBridge bridge={status.bridge} login={login} packages={packages} instances={status.instances} busy={busy} run={run} onConnections={onConnections} onCheckLogin={() => void checkLogin()}/>
      {status.instances.map(instance => {
        // A retired engine (its strategy moved to the new bridge format) never runs: the service lists it
        // blocked with runtimeState and blocker 'retired_engine'. Its only action here is to retire the instance.
        const retired = instance.runtimeState === 'retired_engine' || instance.blockers.includes('retired_engine');
        const retireReady = retired || (instance.entriesHeld && ['idle', 'closed'].includes(instance.tradePhase));
        return <article className={'runtime-instance' + (retired ? ' retired-engine' : '')} key={instance.instanceId} data-retired-engine={retired || undefined}>
        <div className="guardian-row"><h3>{packages.find(item => item.pairId === instance.binding.pairId)?.label || instance.binding.pairId} · {instance.binding.symbol}</h3><span className={retired ? 'pill' : 'pill muted'}>{retired ? 'Retired engine' : instance.entriesHeld ? 'Entries held' : instance.entryReady ? 'Entry checks passed' : 'Waiting for entry checks'}</span></div>
        <dl className="guardian-facts"><div><dt>Account</dt><dd>{instance.binding.nativeAccountName || instance.binding.accountId}{instance.binding.nativeAccountName && instance.binding.nativeAccountId ? ` · #${instance.binding.nativeAccountId}` : ''}</dd></div><div><dt>Worker</dt><dd>{retired ? 'Not started (retired engine)' : instance.runtimeState}</dd></div><div><dt>Bridge account reading</dt><dd>{instance.nativeFresh ? 'Fresh' : 'Unknown'}</dd></div><div><dt>Engine trade state</dt><dd>{instance.tradePhase.replaceAll('_', ' ')}</dd></div>{!retired && <div><dt>Size Lock</dt><dd>{instance.sizeLockEnabled ? 'On' : 'Off'}</dd></div>}</dl>
        {!!instance.blockers.length && <details className="runtime-blockers" open><summary>{retired ? 'Why this engine is blocked' : 'Before new entries can be enabled'}</summary><ul>{instance.blockers.map(reason => <li key={reason}>{blockers[reason] || 'A required bridge or account check has not passed. Check the Bridge window.'}</li>)}</ul></details>}
        {!retired && <div className="guardian-actions"><button className="secondary" disabled={busy} onClick={() => void run(instance.runtimeState === 'running' ? 'hold' : 'start-held', {instanceId: instance.instanceId})}>{instance.runtimeState === 'running' ? 'Hold new entries' : 'Start with entries held'}</button><button className="primary" disabled={busy || !!instance.blockers.length || !instance.entriesHeld} onClick={() => setConfirmEnable(instance.instanceId)}>Enable {instance.binding.accountMode==='live'?'live':'demo'} entries</button></div>}
        {instance.tp1State==='waiting_for_entry_fill'&&<p className="callout">TP1 was refused before the entry fill reached the bridge. The engine is waiting for fresh fill evidence. It can resend the same planned TP1 at most twice within 30 seconds. An unknown send is never retried.</p>}
        {instance.tp1State==='resent_after_entry_fill'&&<p className="field-hint">The planned TP1 was resent after the entry fill was verified. Price and quantity were unchanged.</p>}
        {!retired && <MarketFeedSetup instance={instance} busy={busy} run={run}/>}
        {!retired && <details className="runtime-install"><summary>Size Lock module · {instance.sizeLockEnabled ? 'On' : 'Off'}</summary><p>Off: the engine’s original allocation. On: one contract, assigned to the original TP1 trigger, with the engine’s protective exits unchanged. These are the engine’s two reviewed sizing policies; there is no free-form size. The engine must be held and idle to change it.</p><button className="secondary" disabled={busy || !instance.entriesHeld || !['idle', 'closed'].includes(instance.tradePhase)} onClick={() => void run('configure-size-lock', {instanceId: instance.instanceId, enabled: !instance.sizeLockEnabled})}>{instance.sizeLockEnabled ? 'Turn Size Lock off' : 'Turn Size Lock on'}</button></details>}
        <details className="runtime-install" open={retired || undefined}><summary>{retired ? 'Retire this instance' : 'Retire this engine'}</summary><p>{retired ? 'This instance cannot run or receive alerts. Retiring it keeps its trading ledger unchanged and frees the account for a new engine setup. It does not close positions or cancel orders, and it claims no broker reading.' : 'Retirement frees the account for a new engine setup and keeps the original trading ledger unchanged. It does not close positions or cancel orders. A used engine needs a fresh flat reading from the bridge; an unused idle setup does not claim a broker reading.'}</p><button className={retired ? 'primary' : 'secondary'} disabled={busy || !retireReady} onClick={() => setConfirmArchive({id: instance.instanceId, action: 'retire'})}>Review retirement</button></details>
        {!retired && confirmEnable === instance.instanceId && <div className="callout"><p>Allow this engine to place new {instance.binding.accountMode==='live'?'live':'demo'} trades on account {instance.binding.nativeAccountName||instance.binding.accountId}? {instance.binding.accountMode==='live'?'This permits live strategy orders on this exact account.':'This does not authorize live accounts.'}</p><div className="guardian-actions"><button className="primary" disabled={busy} onClick={async () => {await run('enable-entries', {instanceId: instance.instanceId, [instance.binding.accountMode==='live'?'acknowledgeLiveTrading':'acknowledgeDemoTrading']: true}); setConfirmEnable(null);}}>Confirm {instance.binding.accountMode==='live'?'live':'demo'} entries</button><button className="secondary" disabled={busy} onClick={() => setConfirmEnable(null)}>Keep entries held</button></div></div>}
        {!retired && <RuntimeSignalAccess instance={instance} keys={status.ingressKeys?.producers} unlocked={status.ingressKeys?.locked===false} listening={status.ingress?.state==='listening_loopback'} address={privateAddress} busy={busy} run={run} dismiss={()=>setPrivateAddress(null)} notice={setNotice}/>}
      </article>})}
      {!!status.retiredInstances?.length && <details className="runtime-install"><summary>Retired engines · {status.retiredInstances.length}</summary><p>These ledgers are retained and cannot receive alerts. Restoring a backup does not reactivate them. A maximum of 32 active and retired instances can be retained.</p>{status.retiredInstances.map(item => <article className="runtime-instance" key={item.binding.instanceId}><h3>{packages.find(pkg => pkg.pairId === item.binding.pairId)?.label || item.binding.pairId} · {item.binding.symbol}</h3><p>Account {item.binding.nativeAccountName || item.binding.accountId}. {item.retirementBasis === 'never_dispatched_idle' ? 'Retired unused; no broker-flat assertion.' : 'Retired after a flat check.'}</p><button className="secondary" disabled={busy} onClick={() => setConfirmArchive({id: item.binding.instanceId, action: 'reactivate'})}>Review reactivation</button></article>)}</details>}
      {confirmArchive && <div className="callout" role="region" aria-label="Confirm engine lifecycle change"><p>{confirmArchive.action === 'retire' ? (status.instances.find(item => item.instanceId === confirmArchive.id)?.runtimeState === 'retired_engine' ? 'Retire this instance of a retired engine? Its records are kept unchanged. It cannot run or receive alerts.' : 'Retire this held engine while retaining its exact records? It cannot receive alerts after retirement.') :'Return this archived engine to active setups? Its source and ledger must still match, and no active engine may use its account or route. This will not start a worker or enable entries.'}</p><div className="guardian-actions"><button className="secondary" disabled={busy} onClick={async () => {const action=confirmArchive.action; await run(action, {instanceId: confirmArchive.id, [action === 'retire' ? 'acknowledgeRetirement' : 'acknowledgeReactivation']: true}); setConfirmArchive(null);}}>{confirmArchive.action === 'retire' ? 'Confirm retirement' : 'Confirm reactivation'}</button><button className="text-button" disabled={busy} onClick={() => setConfirmArchive(null)}>Cancel change</button></div></div>}
      {!!status.instances.length && <details className="runtime-install"><summary>Connect incoming strategy alerts</summary><p>Use a dedicated HTTPS address that you control. The local receiver listens only inside this machine; a customer-owned HTTPS reverse proxy must forward it. Do not point this at another person’s service.</p><p className="field-hint">Receiver: {status.ingress?.state?.replaceAll('_', ' ') || 'Not configured'}. Public HTTPS delivery: {status.ingress?.publicTlsVerified ? 'Verified' : 'Not verified'}.</p><form onSubmit={event => {event.preventDefault(); void run('configure-ingress', {publicOrigin, acknowledgeOwnsOrigin: ownsOrigin});}}><label htmlFor="runtime-public-origin">Your HTTPS alert origin</label><input id="runtime-public-origin" type="url" placeholder="https://alerts.your-domain.example" value={publicOrigin} onChange={e => setPublicOrigin(e.target.value)} required maxLength={240} disabled={busy}/><label className="checkbox"><input type="checkbox" checked={ownsOrigin} onChange={e => setOwnsOrigin(e.target.checked)} required disabled={busy}/><span>I control this HTTPS address and will configure its proxy to this installation.</span></label><button className="secondary" disabled={busy || !ownsOrigin}>Configure local receiver</button></form></details>}
      <details className="runtime-health"><summary>Runtime health history</summary>{status.events?.length ? <ol>{status.events.map((item, index) => <li key={item.observed + '-' + index}><span>{item.state.replaceAll('_', ' ')}</span><time dateTime={new Date(item.observed).toISOString()}>{new Date(item.observed).toLocaleString()}</time></li>)}</ol> : <p>No runtime transitions have been recorded yet.</p>}<p className="field-hint">This history stays on this installation. External alert delivery is not configured.</p></details>
    </>}
  </section>;
}
