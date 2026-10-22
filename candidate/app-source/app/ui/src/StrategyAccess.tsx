import { useEffect, useId, useState, type FormEvent } from 'react';
import { api, type State, type StrategyAccessState } from './api';
import { requestableStrategies, listNames as listText } from './strategies';
import { EngineStatus, TradingViewScriptLink } from './EngineCatalog';

type Props={state:State;refresh:()=>Promise<State|null>};
const date=(value:string|null|undefined)=>value&&!Number.isNaN(Date.parse(value))?new Date(value).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}):'';
const reasons:Record<string,string>={not_configured:'Sending is turned off on this system.',rate_limited:'Decrypt Labs is getting too many requests right now.',refused:'Decrypt Labs did not accept the request.',unavailable:'Decrypt Labs could not be reached. Check your internet connection.',interrupted:'The app closed before the send finished.'};
const strategyNames=(ids:string[])=>ids.map(id=>requestableStrategies().find(s=>s.id===id)?.name||id);

export function TradingViewLink(){return <div className="tv-launch"><a className="secondary" href="https://www.tradingview.com/chart/" target="_blank" rel="noopener noreferrer">Open TradingView to sign in <span aria-hidden="true">↗</span></a><p className="field-hint">Opens TradingView in a new tab. Your chart window also lives on workspace 2 (Super+2). Sign in with your own account; Decrypt never asks for its password.</p></div>}

/** One line for Home, Setup and Trading: what happened to the username. */
export function accessSummary(access?:StrategyAccessState){
 const latest=access?.latest;
 if(!latest)return {tone:'muted' as const,text:access?.tradingViewUsername?'Not sent yet':'No username yet'};
 if(latest.status==='sent')return {tone:'teal' as const,text:'Sent to Decrypt Labs'};
 return {tone:'amber' as const,text:'Not sent · send it by hand'};
}

/** The copy-by-hand block. `strategiesOnly` is the case where the username was sent but the picked strategies were not. */
function ManualSend({username,site,strategyNames:names,strategiesOnly}:{username:string;site:string;strategyNames?:string[];strategiesOnly?:boolean}){
 const [copied,setCopied]=useState('');
 const text=names&&names.length?`${username} — ${listText(names)}`:username;
 const siteText=<a href={site} target="_blank" rel="noopener noreferrer">{site.replace(/^https:\/\//,'')} ↗</a>;
 async function copy(){try{await navigator.clipboard.writeText(text);setCopied('Copied.')}catch{setCopied('Copy did not work. Select the text and copy it.')}}
 return <div className="callout manual-send"><strong>{strategiesOnly?'Tell Decrypt Labs which strategies':'Send this to Decrypt Labs'}</strong><p>{strategiesOnly?<>Your username was sent, but the strategies you picked were not. Contact Decrypt Labs through {siteText} and give them:</>:<>Contact Decrypt Labs through {siteText} and give them your TradingView username{names&&names.length?' and the strategies you picked':''}:</>}</p><p className="manual-username"><code>{text}</code> <button type="button" className="secondary" onClick={copy}>Copy</button></p>{copied&&<p className="field-hint" role="status">{copied}</p>}</div>;
}

export function AccessStatus({access}:{access:StrategyAccessState}){
 const latest=access.latest;
 if(!latest)return null;
 const names=strategyNames(latest.strategies||[]);
 const picked=names.length?<p>This request covers {listText(names)}.</p>:null;
 if(latest.status==='sent')return <div className="request-state"><span className="pill teal">Sent to Decrypt Labs</span><h3>Request sent</h3><p>Decrypt Labs will add the strategy to your TradingView username, <strong>{latest.tradingViewUsername}</strong>. This is done by hand, so allow some time. Then check <strong>Indicators → Invite-only scripts</strong>.</p>{picked}{names.length>0&&!access.strategyFieldSupported&&<ManualSend username={latest.tradingViewUsername} site={access.publicSite} strategyNames={names} strategiesOnly/>}<p className="field-hint">Sent {date(latest.sentAt)}.</p></div>;
 return <div className="request-state"><span className="pill amber">Not sent</span><h3>Your request did not reach Decrypt Labs</h3><p>{reasons[latest.reason||'']||'The request could not be sent.'} You can try again, or send the username yourself.</p>{picked}<ManualSend username={latest.tradingViewUsername} site={access.publicSite} strategyNames={names}/></div>;
}

export function StrategyAccess({state,refresh}:Props){
 const uid=useId(),access=state.strategyAccess;
 const options=requestableStrategies();
 const [username,setUsername]=useState(access.tradingViewUsername||''),[contact,setContact]=useState(''),[selected,setSelected]=useState<string[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState('');
 useEffect(()=>{setUsername(access.tradingViewUsername||'')},[access.tradingViewUsername]);
 function toggle(id:string){setSelected(current=>current.includes(id)?current.filter(item=>item!==id):[...current,id])}
 async function submit(e:FormEvent){
  e.preventDefault();setBusy(true);setError('');
  const body:{tradingViewUsername:string;contact?:string;strategies?:string[]}={tradingViewUsername:username.trim()};
  if(contact.trim())body.contact=contact.trim();
  if(selected.length)body.strategies=selected;
  try{await api('/strategy-access/request',body);await refresh();setContact('');setSelected([])}catch(e){setError((e as Error).message)}finally{setBusy(false)}
 }
 const name=username.trim();
 return <section className="strategy-access" aria-labelledby={uid+'-heading'}>
  <h2 id={uid+'-heading'}>Request strategy access</h2>
  <p>Enter your TradingView username, pick which strategies you want, and send. Decrypt Labs grants the invite-only strategy to that username in TradingView. Then add the strategy to your chart and create the alert as shown.</p>
  <AccessStatus access={access}/>
  <form onSubmit={submit}>
   <label htmlFor={uid+'-username'}>TradingView username</label>
   <input id={uid+'-username'} value={username} onChange={e=>setUsername(e.target.value)} maxLength={64} required autoComplete="off" autoCapitalize="none" spellCheck={false} aria-describedby={uid+'-username-help'} placeholder="Your TradingView username"/>
   <p id={uid+'-username-help'} className="field-hint">As shown on your TradingView profile. Username only: not an email, password or profile link.</p>
   {options.length>0&&<fieldset className="strategy-pick" aria-describedby={uid+'-pick-help'}><legend>Which strategies? <span className="tag">Optional</span></legend>
    <ul className="strategy-pick-list">{options.map(s=><li key={s.id} className="strategy-pick-row"><label className="checkbox"><input type="checkbox" checked={selected.includes(s.id)} onChange={()=>toggle(s.id)} aria-describedby={uid+'-st-'+s.id}/><span>{s.name}</span></label><EngineStatus id={uid+'-st-'+s.id} status={s.engineStatus}/><TradingViewScriptLink url={s.tradingviewUrl} name={s.name} className="strategy-pick-link"/></li>)}</ul>
    <p id={uid+'-pick-help'} className="field-hint">The label is the engine’s status. Leave all unchecked to send only your username; Decrypt Labs then decides which strategies apply.</p></fieldset>}
   <label htmlFor={uid+'-contact'}>Contact <span className="tag">Optional</span></label>
   <input id={uid+'-contact'} value={contact} onChange={e=>setContact(e.target.value)} maxLength={200} autoComplete="off" aria-describedby={uid+'-contact-help'}/>
   <p id={uid+'-contact-help'} className="field-hint">An email or other way for Decrypt Labs to reach you. Leave it empty to send only your username.</p>
   <button className="primary" disabled={busy||!name}>{busy?'Sending…':'Send to Decrypt Labs'}</button>
   <p className="field-hint">{access.strategyFieldSupported?'Your username, the contact if you add one, and the strategies you pick are sent.':'Only your username, and the contact if you add one, are sent. The strategies you pick are shown back to you here; they are not sent yet.'} Nothing about this computer or your accounts is sent.</p>
  </form>
  {!access.requestConfigured&&!access.latest&&name&&<ManualSend username={name} site={access.publicSite} strategyNames={strategyNames(selected)}/>}
  {error&&<p className="error" role="alert">{error}</p>}
 </section>;
}

export function StrategyAccessPage({state,refresh}:Props){
 return <div className="strategy-access-page">
  <ol className="how-steps how-steps-row" aria-label="How strategy access works"><li><strong>1 · Send your username</strong><span>Enter your TradingView username below and send it to Decrypt Labs.</span></li><li><strong>2 · Decrypt Labs grants access</strong><span>Your username is added to the invite-only strategy in TradingView.</span></li><li><strong>3 · Add it and create the alert</strong><span>Add the strategy from Indicators → Invite-only scripts. Then create the alert as shown in Trading → your engine → Private alert delivery.</span></li></ol>
  <div className="catalog-access-grid"><div className="panel" id="catalog-access"><StrategyAccess state={state} refresh={refresh}/></div><div className="panel"><h2>After access is granted</h2><TradingViewLink/><ol className="approval-instructions"><li>Sign in to TradingView as <strong>{state.strategyAccess.tradingViewUsername||'your username'}</strong>.</li><li>Open <strong>Indicators → Invite-only scripts</strong> and add the strategy to your chart. If it is missing, refresh TradingView and check the username you sent.</li><li>Create the alert as shown in <strong>Trading → your engine → Private alert delivery</strong>. The engine receives no signals until that alert exists.</li></ol><p className="field-hint">Questions? Contact Decrypt Labs through <a href={state.strategyAccess.publicSite} target="_blank" rel="noopener noreferrer">{state.strategyAccess.publicSite.replace(/^https:\/\//,'')} ↗</a>.</p></div></div>
 </div>;
}
