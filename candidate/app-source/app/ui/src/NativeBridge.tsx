import {useEffect, useRef, useState} from 'react';
import type {FormEvent} from 'react';
import type {RuntimeAction} from './runtimeAction';
import type {BridgeLogin} from './api';
import {comingSoonEngines} from './strategies';

export type BridgeStatus = {configured:boolean; paused:boolean|null; firing:boolean; state:string; logins:{id:string;label:string;primary:boolean}[]};
type Package = {pairId:string; label:string; engineId?:string; tp1OffsetMode:'explicit'|'zero'};
type Instance = {instanceId:string; entriesHeld:boolean; tradePhase:string; nativeFresh:boolean; route?:string|null; binding:{loginGroupId?:string;provider?:string; accountMode?:string; nativeAccountId?:number; nativeAccountName?:string; symbol:string; pairId:string}};
const OTHER = 'other';

// Install the Soup engine for one Tradovate account. The account list comes from what the
// bridge sees after you sign in (read only); the rest are the engine's reviewed settings.
export default function NativeBridge({bridge, login, packages: allPackages, instances, busy, run, onConnections, onCheckLogin}: {
  bridge?:BridgeStatus; login:BridgeLogin|null; packages:Package[]; instances:Instance[];
  busy:boolean; run:RuntimeAction; onConnections:()=>void; onCheckLogin:()=>void;
}) {
  const [pair,setPair]=useState(''), [choice,setChoice]=useState(''), [account,setAccount]=useState(''), [name,setName]=useState('');
  const [symbol,setSymbol]=useState(''), [offset,setOffset]=useState(''), [loginGroup,setLoginGroup]=useState('login-1'), [stream,setStream]=useState('soup-main'), [feed,setFeed]=useState('mnq-1s');
  const [setupConsent,setSetupConsent]=useState(false);
  const request=useRef(crypto.randomUUID());
  // Only Soup installs and trades in this release; the app service has no route for the other engines yet.
  const packages=allPackages.filter(p=>!comingSoonEngines.has(p.engineId||''));
  const selected=packages.find(p=>p.pairId===pair)||packages[0];
  const routed=instances.filter(row=>row.route==='tradovate_bridge');
  const visible=login?.accounts||[];
  const installed=new Set(instances.map(i=>String(i.binding.nativeAccountId)));
  // The selected account is derived from what the bridge sees: the bridge's own account first
  // (skipping one that already has an engine), unless the user picked another entry.
  const auto=visible.find(a=>a.configured&&!installed.has(String(a.id)))||visible.find(a=>a.configured)||visible[0];
  const effective=!visible.length?OTHER:(choice===OTHER||visible.some(a=>String(a.id)===choice))?choice:auto?String(auto.id):OTHER;
  useEffect(()=>{ if(login?.account?.contract&&!symbol) setSymbol(login.account.contract); },[login,symbol]);
  useEffect(()=>{
    const a=visible.find(x=>String(x.id)===effective);
    if(a&&(account!==String(a.id)||name!==a.name)){setAccount(String(a.id));setName(a.name);}
  },[effective,visible,account,name]);
  function choose(value:string){ setChoice(value); const a=visible.find(x=>String(x.id)===value); if(a){setAccount(String(a.id));setName(a.name);} else {setAccount('');setName('');} }
  async function install(event:FormEvent) {
    event.preventDefault();
    if (!selected||!setupConsent) return;
    const done=await run('install',{provider:'tradovate',requestId:request.current,pairId:selected.pairId,
      loginGroupId:loginGroup,nativeAccountId:Number(account),nativeAccountName:name.trim(),accountMode:'demo',symbol:symbol.trim().toUpperCase(),
      streamId:stream.trim(),feedId:feed.trim(),tp1LimitOffsetHandles:selected.tp1OffsetMode==='zero'?0:Number(offset),acknowledgeNativeSetup:true});
    if(done){request.current=crypto.randomUUID();setSetupConsent(false);}
  }
  const manual=effective===OTHER;
  return <section className="runtime-install native-install" aria-labelledby="native-bridge-heading">
    <h3 id="native-bridge-heading">Install the Soup engine</h3>
    <p>The Soup engine sends its orders to the Tradovate bridge on this machine. Set the bridge up and sign in first (Connections), then install the engine here for the same account. Pause, resume and Flatten live in Bridge Advanced on workspace 5.</p>
    {!bridge?<p className="callout">Bridge status is unavailable. No running state is assumed.</p>:<>
      <dl className="guardian-facts"><div><dt>Bridge</dt><dd>{bridge.configured?'Set up':'Not set up'}</dd></div><div><dt>Sign-in</dt><dd>{!bridge.configured?'—':login?login.signIn==='connected'?'Signed in':login.signIn==='needs_human'?'Needs you':login.signIn==='logged_out'?'Not signed in':login.daemon==='unreachable'?'Bridge not answering':'Unknown':'Checking'}</dd></div><div><dt>New entries at the bridge</dt><dd>{bridge.paused===null?'Unknown':bridge.paused?'Paused':'Allowed'}</dd></div><div><dt>Engines on the bridge</dt><dd>{routed.length}</dd></div></dl>
      {!bridge.configured?<div className="guardian-actions"><button className="secondary" onClick={onConnections}>Set up the bridge in Connections →</button></div>
      :<details open={!instances.length}><summary>Install the Soup engine for an account</summary>
        <p>Installing saves a held engine for one account. It does not start the bridge or place an order. The bridge connects to Tradovate Simulation in this release.</p>
        {!packages.length?<p className="callout">No installable engine package was reported by the local service.</p>:<form onSubmit={event=>void install(event)}>
          {packages.length>1?<><label htmlFor="native-pair">Engine package</label><select id="native-pair" value={selected?.pairId||''} onChange={event=>{setPair(event.target.value);setOffset('');}} disabled={busy}>{packages.map(item=><option key={item.pairId} value={item.pairId}>{item.label}</option>)}</select></>:<p className="field-hint">Engine: {selected?.label}</p>}
          <label htmlFor="native-choice">Tradovate account</label>
          <select id="native-choice" value={effective} onChange={event=>choose(event.target.value)} disabled={busy} aria-describedby="native-choice-help">
            {visible.map(a=><option key={a.id} value={String(a.id)}>{a.name} · #{a.id}{a.configured?' · bridge account':''}{installed.has(String(a.id))?' · engine installed':''}</option>)}
            <option value={OTHER}>{visible.length?'Another account (enter below)':'Enter the account below'}</option>
          </select>
          <p id="native-choice-help" className="field-hint">{visible.length?'These are the accounts the bridge can see right now. Pick the one you gave the bridge setup.':login?.signIn==='connected'?'The bridge is signed in but lists no account yet. Check again, or enter the account by hand.':'Sign in on workspace 3 and the bridge’s accounts appear here. Until then, enter the same account name and number you gave the bridge setup.'} <button type="button" className="text-button inline" disabled={busy} onClick={onCheckLogin}>Check again</button></p>
          <div className="guardian-field-grid">
            {manual&&<><div><label htmlFor="native-account">Tradovate account number</label><input id="native-account" value={account} onChange={event=>setAccount(event.target.value)} inputMode="numeric" pattern="[1-9][0-9]{0,14}" maxLength={15} required disabled={busy}/></div>
            <div><label htmlFor="native-name">Tradovate account name</label><input id="native-name" value={name} onChange={event=>setName(event.target.value)} maxLength={128} required disabled={busy} autoComplete="off" spellCheck={false}/></div></>}
            <div><label htmlFor="native-symbol">Dated MNQ contract</label><input id="native-symbol" value={symbol} onChange={event=>setSymbol(event.target.value)} pattern="MNQ[HMUZ]20[0-9]{2}" maxLength={8} required disabled={busy} aria-describedby="native-symbol-help"/><p id="native-symbol-help" className="field-hint">The same contract as the bridge setup{login?.account?.contract?` (${login.account.contract})`:''}.</p></div>
            {selected?.tp1OffsetMode==='explicit'&&<div><label htmlFor="native-offset">TP1 limit offset, points</label><input id="native-offset" type="number" min="0" max="100" step="0.25" value={offset} onChange={event=>setOffset(event.target.value)} required disabled={busy} aria-describedby="native-offset-help"/><p id="native-offset-help" className="field-hint">The TP1 offset set in your Turtle Soup Shift strategy on TradingView. Enter the same value so the engine’s TP1 matches the strategy.</p></div>}
          </div>
          <details className="native-advanced"><summary>Advanced names</summary>
            <div className="guardian-field-grid">
              <div><label htmlFor="native-login-group">Tradovate login group</label><select id="native-login-group" value={loginGroup} onChange={event=>setLoginGroup(event.target.value)} disabled={busy}>{Array.from({length:8},(_,i)=>`login-${i+1}`).map(g=><option key={g} value={g}>{g}</option>)}</select><p className="field-hint">Accounts under one Tradovate username share a group. Keep login-1 unless you add logins in Bridge Advanced.</p></div>
              <div><label htmlFor="native-stream">Strategy stream name</label><input id="native-stream" value={stream} onChange={event=>setStream(event.target.value)} pattern="[A-Za-z0-9_-]{1,48}" maxLength={48} required disabled={busy}/><p className="field-hint">Labels this engine’s alerts. The default is fine.</p></div>
              <div><label htmlFor="native-feed">Market-feed name</label><input id="native-feed" value={feed} onChange={event=>setFeed(event.target.value)} pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,95}" maxLength={96} required disabled={busy}/><p className="field-hint">Labels this engine’s chart feed. The default is fine.</p></div>
            </div>
          </details>
          <label className="checkbox"><input type="checkbox" checked={setupConsent} onChange={event=>setSetupConsent(event.target.checked)} required disabled={busy}/><span>I checked this is my Simulation account{name?` (${name})`:''}. The engine starts with entries held; nothing trades until I resume entries in Bridge Advanced.</span></label>
          <button className="primary" disabled={busy||!setupConsent||!selected||!account||!name.trim()}>Install Soup with entries held</button>
        </form>}
      </details>}
    </>}
  </section>;
}
