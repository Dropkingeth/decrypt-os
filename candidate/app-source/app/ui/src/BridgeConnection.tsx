import {useCallback, useEffect, useRef, useState} from 'react';
import type {FormEvent, ReactNode} from 'react';
import {api, ApiError, type BridgeLogin} from './api';
import {bridgeLabel, type LiveTrading} from './liveStatus';

// The Tradovate bridge is its own module with its own windows (Bridge window on workspace 3,
// Bridge Advanced on workspace 5). This panel sets it up for you: the same setup the module's
// setup.py does, run by the local service with your values, and a read-only look at what the
// running bridge sees after you sign in. Nothing here places an order.
type Tone = 'done' | 'current' | 'todo' | 'attention';
const MODULE = '/usr/local/lib/decrypt-os/modules/tradovate-bridge/setup.py';

function Step({n, tone, title, status, children}: {n: number; tone: Tone; title: string; status: string; children?: ReactNode}) {
  return <li className={'bridge-step ' + tone} aria-current={tone === 'current' ? 'step' : undefined}>
    <span className={'step-number ' + (tone === 'done' ? 'done' : tone === 'current' || tone === 'attention' ? 'current' : '')} aria-hidden="true">{tone === 'done' ? '✓' : n}</span>
    <div className="bridge-step-body"><div className="bridge-step-head"><strong>{title}</strong><span className={'bridge-step-status ' + tone}>{status}</span></div>{children}</div>
  </li>;
}

export function BridgeConnection({trading, checked, onRefresh, onTrading}: {trading: LiveTrading | null; checked: boolean; onRefresh: () => void; onTrading: () => void}) {
  const enabled = trading?.enabled === true;
  const bridge = enabled ? trading?.bridge : undefined;
  const configured = bridge?.configured === true;
  const engines = trading?.instances.length ?? 0;
  const [login, setLogin] = useState<BridgeLogin | null>(null), [loginChecked, setLoginChecked] = useState(false);
  const [name, setName] = useState(''), [number, setNumber] = useState(''), [contract, setContract] = useState('MNQZ2026');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState(''), [turnedOn, setTurnedOn] = useState(false), [confirmOff, setConfirmOff] = useState(false);
  const mounted = useRef(true);

  const checkLogin = useCallback(async () => {
    if (!enabled) return;
    try {
      const result = await api<{bridgeLogin: BridgeLogin}>('/trading/bridge-login', {});
      if (mounted.current) setLogin(result.bridgeLogin);
    } catch { if (mounted.current) setLogin(null); }
    finally { if (mounted.current) setLoginChecked(true); }
  }, [enabled]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (configured) void checkLogin(); else setLoginChecked(checked); }, [configured, checked, checkLogin]);

  async function run(action: 'bridge-setup' | 'bridge-enable' | 'bridge-disable', body: Record<string, unknown>, done: string) {
    setBusy(true); setError(''); setNotice('');
    try {
      await api('/trading/' + action, body);
      setNotice(done);
      if (action === 'bridge-enable') setTurnedOn(true);
      if (action === 'bridge-disable') setTurnedOn(false);
      onRefresh(); await checkLogin();
      return true;
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'The local service did not confirm this change. Check the status and try again.');
      return false;
    } finally { setBusy(false); }
  }
  async function setup(event: FormEvent) {
    event.preventDefault();
    if (await run('bridge-setup', {accountName: name.trim(), accountNumber: number.trim(), contract: contract.trim().toUpperCase()}, 'Bridge set up for your account. New entries stay paused. Next: turn on the bridge.')) { setName(''); setNumber(''); }
  }

  // Live facts for each step. "done" only when the local service confirmed it.
  const running = login?.daemon === 'reachable' || turnedOn;
  const signedIn = login?.signIn === 'connected';
  const mismatch = signedIn && login?.accounts.length ? login.nameMatch === false || login.numberMatch === false : false;
  const tone = (done: boolean, current: boolean, attention = false): Tone => done ? 'done' : attention ? 'attention' : current ? 'current' : 'todo';
  const step1: Tone = tone(configured, true);
  const step2: Tone = tone(configured && running, configured);
  const step3: Tone = tone(signedIn && !mismatch, configured && running, (configured && running && login?.signIn === 'needs_human') || mismatch);
  const step4: Tone = tone(engines > 0, signedIn);
  const step5: Tone = tone(configured && bridge?.paused === false, engines > 0);
  const accountLine = login?.account ? `${login.account.name} · #${login.account.number}${login.account.contract ? ' · ' + login.account.contract : ''}` : 'Set up';

  return <section className="panel bridge-panel" aria-labelledby="bridge-heading">
    <div className="section-title"><h2 id="bridge-heading">Tradovate bridge</h2><span className={'pill ' + (configured ? 'teal' : 'muted')} role="status">{bridgeLabel(trading, checked)}</span></div>
    <p>The bridge signs in to Tradovate in its own browser window and sends the Soup engine’s orders to your account. It ships switched off. Five steps, in order; each one shows what the local service has confirmed.</p>
    {checked && !bridge && <p className="callout">Bridge status is not available from the local service right now. Nothing is assumed to be running.</p>}
    {error && <p className="error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    <ol className="bridge-steps">
      <Step n={1} tone={step1} title="Set up the bridge" status={configured ? accountLine : 'Your Tradovate account'}>
        {configured ? <p>The bridge is set up for this account. Entries start paused. To use a different account, turn the bridge off (below) and remove its folder <code>~/.local/share/decrypt-os/tvbridge</code> before setting up again.</p>
          : <form onSubmit={event => void setup(event)} className="bridge-setup-form">
            <p>Enter the Tradovate Simulation account the Soup engine should trade. The local service writes the bridge’s config for this account and keeps new entries paused. It never asks for your Tradovate password.</p>
            <div className="guardian-field-grid">
              <div><label htmlFor="bridge-account-name">Tradovate account name</label><input id="bridge-account-name" value={name} onChange={e => setName(e.target.value)} maxLength={128} required autoComplete="off" spellCheck={false} disabled={busy || !enabled} aria-describedby="bridge-account-name-help"/><p id="bridge-account-name-help" className="field-hint">Exactly as Tradovate shows it in the account selector at the top of the trading window. Simulation accounts usually start with DEMO.</p></div>
              <div><label htmlFor="bridge-account-number">Tradovate account number</label><input id="bridge-account-number" value={number} onChange={e => setNumber(e.target.value)} inputMode="numeric" pattern="[1-9][0-9]{0,14}" maxLength={15} required autoComplete="off" disabled={busy || !enabled} aria-describedby="bridge-account-number-help"/><p id="bridge-account-number-help" className="field-hint">Digits only. Tradovate lists it under Account settings. After you sign in on workspace 3, this page shows the accounts the bridge can see so you can confirm it.</p></div>
              <div><label htmlFor="bridge-contract">Dated MNQ contract</label><input id="bridge-contract" value={contract} onChange={e => setContract(e.target.value)} pattern="MNQ[HMUZ]20[0-9]{2}" maxLength={8} required autoComplete="off" disabled={busy || !enabled} aria-describedby="bridge-contract-help"/><p id="bridge-contract-help" className="field-hint">The contract your strategy trades. Use the same one when you install Soup.</p></div>
            </div>
            <div className="guardian-actions"><button className="primary" disabled={busy || !enabled || !name.trim() || !number.trim()}>{busy ? 'Saving…' : 'Save bridge setup'}</button><span className="field-hint">Writes local config only. Nothing starts or trades.</span></div>
          </form>}
      </Step>
      <Step n={2} tone={step2} title="Turn on the bridge" status={!configured ? 'After step 1' : running ? 'Running' : 'Off'}>
        <p>Starts the bridge’s browser window, its daemon, the login guard, the Bridge window and Bridge Advanced. New entries stay paused until you resume them in step 5.</p>
        {configured && <div className="guardian-actions"><button className="primary" disabled={busy || running} onClick={() => void run('bridge-enable', {}, 'The bridge is on. Sign in to Tradovate in the Bridge window on workspace 3 (Super+3).')}>{running ? 'Bridge is on' : busy ? 'Turning on…' : 'Turn on the bridge'}</button>{!running && <button className="text-button" disabled={busy} onClick={() => void checkLogin()}>Check again</button>}</div>}
      </Step>
      <Step n={3} tone={step3} title="Sign in on workspace 3" status={!configured || !running ? 'After step 2' : login ? (signedIn ? (mismatch ? 'Signed in · account differs' : 'Signed in') : login.signIn === 'needs_human' ? 'Needs you' : login.signIn === 'logged_out' ? 'Not signed in' : 'Checking') : loginChecked ? 'Unknown' : 'Checking'}>
        <p>Press Super+3. In the Bridge window, sign in to Tradovate, choose <strong>Access Simulation</strong>, and let the browser save the login. The bridge only clicks that saved login later; it never sees your password.</p>
        {configured && running && login && <>
          <p className={login.signIn === 'needs_human' || mismatch ? 'callout' : 'field-hint'}>{login.detail}</p>
          {!!login.accounts.length && <dl className="guardian-facts bridge-accounts"><div><dt>Accounts the bridge sees</dt><dd><ul>{login.accounts.map(a => <li key={a.id}>{a.name} · #{a.id}{a.configured ? <span className="pill teal">This bridge</span> : null}</li>)}</ul></dd></div></dl>}
          {mismatch && <p className="callout"><strong>The setup does not match a visible account</strong>The bridge is set up for {login.account?.name} · #{login.account?.number}. {login.nameMatch === false ? 'No visible account has that name.' : 'That name is visible, but with a different account number.'} The bridge refuses entries for an account it cannot match. Fix: turn the bridge off (below), remove <code>~/.local/share/decrypt-os/tvbridge</code>, then set it up again with the values shown here.</p>}
          <div className="guardian-actions"><button className="secondary" disabled={busy} onClick={() => void checkLogin()}>Check sign-in</button></div>
        </>}
      </Step>
      <Step n={4} tone={step4} title="Install the Soup engine" status={engines ? `${engines} installed` : signedIn ? 'Ready' : 'After step 3'}>
        <p>In Trading, install the engine for the same account. It starts with entries held. The account list there comes from what the bridge sees.</p>
        <div className="guardian-actions"><button className="text-button" onClick={onTrading}>Go to Trading →</button></div>
      </Step>
      <Step n={5} tone={step5} title="Resume entries on workspace 5" status={!configured ? 'After step 4' : bridge?.paused === false ? 'Entries allowed' : bridge?.paused ? 'Entries paused' : 'Unknown'}>
        <p>Bridge Advanced is where you pause, resume, flatten and add logins. Open it from the app launcher or the link below; it opens on workspace 5. Entries stay paused until you resume them there, and every restart pauses them again.</p>
        <div className="guardian-actions"><a className="text-button" href="http://127.0.0.1:8781/advanced" target="_blank" rel="noreferrer">Bridge Advanced in a tab ↗</a><a className="text-button" href="http://127.0.0.1:8779/bridge" target="_blank" rel="noreferrer">Bridge window in a tab ↗</a></div>
      </Step>
    </ol>
    <div className="guardian-actions"><button className="secondary" disabled={busy} onClick={() => { onRefresh(); void checkLogin(); }}>Refresh bridge status</button><span className="field-hint">No login retries from Friday 6 PM to Sunday 6 PM ET (the weekend hold). Captcha, 2FA or a missing saved login needs you in the Bridge window.</span></div>
    <details className="bridge-advanced"><summary>Advanced: terminal commands and turning the bridge off</summary>
      <p>The steps above run the bridge module’s own setup. The same commands, from a terminal in Decrypt OS:</p>
      <code>python3 {MODULE} init --account-name "YOUR ACCOUNT NAME" --soup-account-id YOUR_ACCOUNT_NUMBER --ticker MNQZ2026</code>
      <code>python3 {MODULE} enable</code>
      <code>python3 {MODULE} status</code>
      <code>python3 {MODULE} disable</code>
      <p>Turning the bridge off stops its services and keeps every file and token. It does not cancel orders or close positions: check Tradovate yourself.</p>
      {configured && (confirmOff ? <div className="guardian-actions"><button className="secondary danger" disabled={busy} onClick={async () => { await run('bridge-disable', {}, 'The bridge is off. Its files are kept. Open orders and positions are unchanged.'); setConfirmOff(false); }}>Confirm: turn off the bridge</button><button className="text-button" disabled={busy} onClick={() => setConfirmOff(false)}>Keep it on</button></div>
        : <div className="guardian-actions"><button className="text-button danger" disabled={busy} onClick={() => setConfirmOff(true)}>Turn off the bridge</button></div>)}
    </details>
  </section>;
}
