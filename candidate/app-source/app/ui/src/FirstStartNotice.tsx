import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from './api';

type Display = {
 schema: 'decrypt.first-start-display.v1';
 status: 'pending' | 'awaiting-password' | 'complete' | 'unavailable';
 complete: boolean;
 promptRunning: boolean;
 policySha256?: string;
 documentSha256?: string;
 instanceId?: string;
 noticeText?: string;
 source?: 'root-published-display';
 nativeAcceptanceEvidence: false;
 message?: string;
};
const NOTICE_SHA = '2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055';

export async function checkedNotice(value: Display): Promise<Display> {
 if (!value || value.schema !== 'decrypt.first-start-display.v1' || value.nativeAcceptanceEvidence !== false) {
  throw new Error('The first-start service does not match this app. Refresh after updating them together.');
 }
 if (value.status === 'unavailable') {
  if (value.complete || value.promptRunning) throw new Error('First-start status could not be confirmed.');
  return value;
 }
 if (!['pending', 'awaiting-password', 'complete'].includes(value.status) ||
     value.complete !== (value.status === 'complete') || value.source !== 'root-published-display' ||
     !/^[0-9a-f]{64}$/.test(value.policySha256 || '') || value.documentSha256 !== NOTICE_SHA ||
     !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value.instanceId || '') ||
     typeof value.noticeText !== 'string') {
  throw new Error('The notice or this device’s setup has changed. Refresh before continuing.');
 }
 const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value.noticeText));
 const hex = [...new Uint8Array(hash)].map(n => n.toString(16).padStart(2, '0')).join('');
 if (hex !== NOTICE_SHA) throw new Error('The notice text could not be verified. Refresh before continuing.');
 return value;
}

export function FirstStartNotice({ children }: { children: ReactNode }) {
 const [display, setDisplay] = useState<Display | null>(null);
 const [accepted, setAccepted] = useState(false);
 const [busy, setBusy] = useState(false);
 const [error, setError] = useState('');
 const inFlight = useRef(false);
 const lastBinding = useRef('');
 const mounted = useRef(true);
 const refresh = useCallback(async () => {
  if (inFlight.current) return;
  inFlight.current = true;
  try {
   const next = await checkedNotice(await api<Display>('/first-start/status'));
   if (!mounted.current) return;
   const binding = [next.policySha256, next.documentSha256, next.instanceId].join(':');
   if (lastBinding.current !== binding) setAccepted(false);
   lastBinding.current = binding;
   setDisplay(next); setError('');
  } catch (e) {
   if (mounted.current) { setDisplay(null); setError((e as Error).message); }
  } finally { inFlight.current = false; }
 }, []);
 useEffect(() => {
  mounted.current = true;
  void refresh();
  const timer = setInterval(() => { void refresh(); }, 3000);
  return () => { mounted.current = false; clearInterval(timer); };
 }, [refresh]);
 async function perform(action: 'accept' | 'cancel') {
  if (!display || display.status === 'unavailable' || busy || (action === 'accept' && !accepted)) return;
  setBusy(true); setError('');
  try {
   const binding = { policySha256: display.policySha256, documentSha256: display.documentSha256, instanceId: display.instanceId };
   const next = await checkedNotice(await api<Display>('/first-start/' + action, action === 'accept' ? { ...binding, accepted: true } : binding));
   if (mounted.current) { setDisplay(next); if (action === 'cancel') setAccepted(false); }
  } catch (e) { if (mounted.current) setError((e as Error).message); }
  finally { if (mounted.current) setBusy(false); }
 }
 if (display?.complete) return <>{children}</>;
 const pending = display?.status === 'pending';
 const prompting = display?.status === 'awaiting-password';
 return <main id="first-start" className="first-start">
  <section className="panel first-start-panel" aria-labelledby="first-start-heading">
   <div className="first-start-brand"><svg width="30" height="34" viewBox="0 0 32 36" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M11 3h10m-8 0v10L5 28q-2 5 3 5h16q5 0 3-5l-8-15V3M10 23h12"/><circle cx="14" cy="27" r="1.4"/><circle cx="19" cy="21" r="1"/></svg><span className="brand-name">DECRYPT<span className="brand-labs">OS</span></span></div>
   <p className="eyebrow">First start</p>
   <h1 id="first-start-heading">Before you begin</h1>
   <p className="first-start-lead">One notice to read and accept. Then your workspace opens.</p>
   {display?.noticeText && <p className="first-start-notice">{display.noticeText}</p>}
   <div className="callout first-start-beta"><strong>Decrypt OS public beta</strong><p>Decrypt OS is beta software. Not financial advice. You trade your own broker accounts at your own risk. Test with a demo/simulation account first.</p></div>
   {error && <p className="error" role="alert">{error}</p>}
   {!display && !error && <p role="status">Checking first-start setup…</p>}
   {display?.status === 'unavailable' && <p role="status">{display.message || 'Sign in to the local desktop after account setup, then refresh.'}</p>}
   {(pending || prompting) && <>
    <label className="checkbox first-start-accept">
     <input type="checkbox" checked={accepted} disabled={busy || prompting} onChange={event => setAccepted(event.target.checked)} />
     <span>I have read this notice and agree.</span>
    </label>
    {pending && <button className="primary" disabled={!accepted || busy} onClick={() => { void perform('accept'); }}>
     {busy ? 'Opening password prompt…' : 'Accept and open password prompt'}
    </button>}
    {prompting && <div className="callout teal" role="status" aria-live="polite">
     <strong>Confirm in the terminal</strong>
     <p>Enter your local account password in the terminal window that opened. Closing or cancelling it leaves this notice pending.</p>
     <button className="secondary" disabled={busy} onClick={() => { void perform('cancel'); }}>Close password prompt</button>
    </div>}
    <p className="field-hint">Your password stays in the terminal; this app never receives it. Accepting does not connect an account or authorize a trade.</p>
   </>}
   <div className="first-start-foot"><button className="text-button" disabled={busy} onClick={() => { void refresh(); }}>Refresh status</button><span className="version">Built on Omarchy</span></div>
  </section>
 </main>;
}
