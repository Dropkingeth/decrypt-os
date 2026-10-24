import {strategies, engineComingSoon, engineStatusLabels, strategyListed, bridgeOf} from './strategies';
import {Icon, StrategyGlyph} from './Art';
import {useEffect, useId, useRef, useState, type KeyboardEvent} from 'react';
import type {EngineCatalogItem, State} from './api';

// The engine catalogue: which engines exist, which one you can install now, and what each needs
// before it trades. Installing is the Execution runtime above; there is nothing to save here.
type Installed = {instanceId:string; runtimeState:string; entriesHeld:boolean; binding:{pairId:string; symbol:string; nativeAccountName?:string; accountId:string}};
type Package = {pairId:string; engineId:string};

function trapDialogTab(event:KeyboardEvent<HTMLDialogElement>){
 if(event.key!=='Tab')return;
 const controls=Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary,[tabindex="0"]')).filter(element=>element.getClientRects().length>0);
 if(!controls.length){event.preventDefault();return;}
 const index=controls.indexOf(document.activeElement as HTMLElement);
 if(event.shiftKey&&(index<=0)){event.preventDefault();controls.at(-1)?.focus()}
 else if(!event.shiftKey&&(index===controls.length-1)){event.preventDefault();controls[0].focus()}
}

/** Engine status from the strategy catalog as a small label. Same look everywhere it appears. */
export function EngineStatus({status,id}:{status?:string;id?:string}){
 const label=engineStatusLabels[status||''];
 if(!label)return null;
 return <span id={id} className={'pill engine-status '+label.tone}>{label.text}</span>;
}
/** Link to the public TradingView script page. Rendered only when the catalog has a URL. */
export function TradingViewScriptLink({url,name,className}:{url?:string;name:string;className?:string}){
 if(!url)return null;
 return <a className={className||'text-button'} href={url} target="_blank" rel="noopener noreferrer"><span>View<span className="sr-only"> {name}</span> on TradingView</span><span aria-hidden="true">↗</span><span className="sr-only">, opens in a new tab</span></a>;
}

function availability(engine:EngineCatalogItem){
 if(engineComingSoon(engine))return 'coming_soon';
 return engine.packageStatus==='source_packaged'?'available':'not_included';
}
function installText(a:ReturnType<typeof availability>){
 return a==='available'?'Available in Execution runtime':a==='coming_soon'?"Can't trade in this release":'Not included in this release';
}
function accessText(state:State,strategyId:string){
 const strategy=strategies.find(item=>item.id===strategyId),access=state.strategyAccess;
 const username=access.tradingViewUsername||'your TradingView username';
 if(!strategy?.accessRequestAvailable||!strategy?.tradingviewUrl)return 'Not published in TradingView yet. There is nothing to request for this release.';
 if(access.latest?.status==='sent')return `Request sent. Decrypt Labs will add the strategy to ${username} in TradingView.`;
 return 'Not requested yet. Send your TradingView username in Strategy access; Decrypt Labs grants the invite-only script to it.';
}

export function EngineDetails({engine,state,installed,onClose,onInstall,onStrategyAccess}:{engine:EngineCatalogItem;state:State;installed:Installed[];onClose:()=>void;onInstall:()=>void;onStrategyAccess:()=>void}){
 const dialog=useRef<HTMLDialogElement>(null),heading=useRef<HTMLHeadingElement>(null),returnFocus=useRef<HTMLElement|null>(null),uid=useId();
 const strategy=strategies.find(item=>item.id===engine.strategyId),a=availability(engine),parked=strategy?.engineStatus==='parked',bridge=bridgeOf(engine.strategyId);
 const requestable=!!(strategy?.accessRequestAvailable&&strategy?.tradingviewUrl);
 useEffect(()=>{returnFocus.current=document.activeElement as HTMLElement;dialog.current?.showModal();heading.current?.focus();return()=>{dialog.current?.close();if(returnFocus.current?.isConnected)returnFocus.current.focus();}},[]);
 return <dialog onKeyDown={trapDialogTab} ref={dialog} className="ew-dialog engine-dialog" aria-labelledby={uid+'-title'} onCancel={event=>{event.preventDefault();onClose()}}>
  <div className="drawer-head"><span className="glyph-wrap"><StrategyGlyph id={engine.strategyId}/></span><div><h2 id={uid+'-title'} ref={heading} tabIndex={-1}>{engine.label}</h2><p className="drawer-sub">Runs {strategy?.label||'a strategy not recorded'}</p></div><button className="icon-button" aria-label="Close engine details" onClick={onClose}>×</button></div>
  <div className="drawer-body">
   <section className="drawer-section"><h3>Status</h3><p><EngineStatus status={strategy?.engineStatus}/></p><p>{engine.description}</p>
    <dl className="details"><div><dt>Engine</dt><dd>{installText(a)}</dd></div><div><dt>On this system</dt><dd>{installed.length?installed.map(i=>`${i.binding.nativeAccountName||i.binding.accountId} · ${i.binding.symbol} · ${i.entriesHeld?'entries held':'entries allowed'}`).join('; '):'Not installed'}</dd></div><div><dt>Strategy access</dt><dd>{accessText(state,engine.strategyId)}</dd></div></dl>
   </section>
   <details className="drawer-section" open><summary><h3>Before this engine can trade</h3></summary><ul className="ew-requirements">
    <li><strong>Engine installed</strong>{a==='coming_soon'?parked?"Parked. It can't be installed or trade in this release.":'Coming soon. In this release only Soup can be installed and trade.':a==='available'?'Install it in Execution runtime; it starts with entries held.':'Not included in this release.'}</li>
    <li><strong>Strategy access</strong>Decrypt Labs grants the invite-only TradingView script to your username. Request it in Strategy access.</li>
    <li><strong>Tradovate bridge</strong>Set up, turned on and signed in on workspace 3, for the same account.</li>
    <li><strong>Fresh account reading</strong>The bridge must report your account flat and synced before the first entry.</li>
    <li><strong>Market open</strong>The contract calendar holds entries on weekends, maintenance breaks and reviewed holidays.</li>
    <li><strong>Entries resumed</strong>You resume entries in Bridge Advanced on workspace 5.</li>
   </ul></details>
   {/* The chart, the alert format and the numbered notes all come from the strategy catalog (bridge block). */}
   {bridge&&<details className="drawer-section alert-setup"><summary><h3>TradingView alert setup</h3><span className="summary-hint">{bridge.alertSetup.length} {bridge.alertSetup.length===1?'step':'steps'}</span></summary>
    <dl className="alert-facts"><div><dt>Chart</dt><dd>{bridge.chart}</dd></div><div><dt>Alert format</dt><dd><code>{bridge.alertFormat}</code></dd></div></dl>
    <ol className="alert-steps" aria-label="Alert setup steps">{bridge.alertSetup.map(note=><li key={note}>{note}</li>)}</ol>
   </details>}
  </div>
  <div className="drawer-actions">{a==='available'?<button className="primary" onClick={onInstall}>Install in Execution runtime</button>:requestable?<button className="secondary" onClick={onStrategyAccess}>Request strategy access</button>:null}<TradingViewScriptLink url={strategy?.tradingviewUrl} name={strategy?.name||engine.label}/><p className="action-reason">{a==='coming_soon'?(parked?'Parked. ':'Coming soon. ')+"This engine can't be installed or trade in this release.":a==='available'?'Installing saves a held engine for one account.':'Install unavailable. This engine is not included in this release.'}</p></div>
 </dialog>;
}

export function EngineCatalog({state,instances,packages,onConnections,onStrategyAccess,onInstall}:{state:State;instances:Installed[];packages:Package[];onConnections:()=>void;onStrategyAccess:()=>void;onInstall:()=>void}){
 const catalog=state.engineWorkspace?.catalog,sent=state.strategyAccess.latest?.status==='sent';
 const [detail,setDetail]=useState<string|null>(null);
 const originTrigger=useRef<HTMLElement|null>(null),wasModal=useRef(false);
 useEffect(()=>{if(wasModal.current&&!detail){const target=originTrigger.current?.isConnected?originTrigger.current:document.getElementById('ew-engines-heading');target?.focus()}wasModal.current=!!detail},[detail]);
 if(!catalog)return <section className="panel"><h2>Engine catalog unavailable</h2><p>The local service did not report an engine catalog. This does not mean no engine is installed.</p><div className="guardian-actions"><button className="text-button" onClick={onStrategyAccess}>Strategy access →</button></div></section>;
 // An engine whose strategy is unlisted in the catalog is left out, whatever its status.
 const engines=catalog.engines.filter(item=>strategyListed(item.strategyId));
 const engine=detail?engines.find(item=>item.id===detail):undefined;
 const pairsByEngine=new Map(packages.map(p=>[p.engineId==='turtle-soup'?'soup':p.engineId,p.pairId]));
 const installedFor=(id:string)=>instances.filter(i=>i.binding.pairId===pairsByEngine.get(id));
 const sizeLock=catalog.modules.find(m=>m.id==='size-lock');
 return <div className="engine-workspace">
 <section className="panel" aria-labelledby="ew-engines-heading"><div className="section-title"><h2 id="ew-engines-heading" tabIndex={-1}>Engines</h2><span className="version">{engines.length} in catalog</span></div><p className="panel-intro">An engine runs a strategy’s trades through the Tradovate bridge. Only Soup can trade in this release; install it in Execution runtime above. The label on each row is the engine’s status; where the strategy is published on TradingView, you can request access to it now.</p>
  <ul className="engine-list">{engines.map(item=>{const strategy=strategies.find(entry=>entry.id===item.strategyId),a=availability(item),mine=installedFor(item.id),requestable=!!(strategy?.accessRequestAvailable&&strategy?.tradingviewUrl);return <li key={item.id} data-engine={item.id} data-status={strategy?.engineStatus} className={a==='coming_soon'?'coming-soon':undefined}><span className="glyph-wrap"><StrategyGlyph id={item.strategyId}/></span><div className="row-main"><h4>{item.label}</h4><p>Runs {strategy?.label||'a strategy not recorded'}</p><dl className="facts"><div><dt>Engine</dt><dd>{mine.length?`Installed · ${mine.length}`:installText(a)}</dd></div><div><dt>Strategy access</dt><dd>{!requestable?'Not published in TradingView':sent?'Username sent':'Not requested'}</dd></div></dl>{requestable&&<div className="row-links"><button className="text-button" aria-label={(sent?'View access status for ':'Request access to ')+strategy!.label} onClick={onStrategyAccess}>{sent?'Access status':'Request access'} →</button><TradingViewScriptLink url={strategy!.tradingviewUrl} name={strategy!.name}/></div>}</div><div className="row-actions"><EngineStatus status={strategy?.engineStatus}/><button className="secondary" aria-label={'View '+item.label} onClick={()=>{originTrigger.current=document.activeElement as HTMLElement;setDetail(item.id)}}>View engine</button></div></li>})}</ul>
  {!engines.length&&<p className="inline-empty">No engines are listed in this catalog.</p>}
 </section>
 {sizeLock&&<section className="panel" aria-labelledby="ew-modules-heading"><div className="section-title"><h2 id="ew-modules-heading">Modules</h2><span className="pill teal">Available for Soup</span></div><div className="module-row"><span className="m-icon" aria-hidden="true"><Icon name="module"/></span><div><h3>{sizeLock.label}</h3><p>{sizeLock.description}</p><p className="field-hint">Switch it on or off for an installed Soup engine in Execution runtime, while the engine is held and idle.</p></div></div>
  <div className="panel-foot"><p className="field-hint">The Tradovate bridge and the contract calendar are in Connections.</p><button className="text-button" onClick={onConnections}>Open Connections →</button></div></section>}
 {engine&&<EngineDetails engine={engine} state={state} installed={installedFor(engine.id)} onClose={()=>setDetail(null)} onInstall={()=>{setDetail(null);onInstall()}} onStrategyAccess={()=>{setDetail(null);onStrategyAccess()}}/>}
 </div>;
}
