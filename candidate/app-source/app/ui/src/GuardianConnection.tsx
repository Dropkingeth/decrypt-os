import {useCallback, useEffect, useRef, useState} from 'react';
import type {FormEvent} from 'react';
import {api, ApiError} from './api';

type ProviderId = 'tradingview';
type Provider = {id: ProviderId; label: string; saved: boolean; autoLogin: boolean; state: string; reason: string | null; nextAttemptAt: number | null; requiresUser: boolean};
type GuardianStatus = {
  enabled: boolean;
  vault: {configured: boolean; unlocked: boolean; unlockMode?: 'keyring' | 'passphrase'; unlockError?: string | null};
  providers: Provider[];
};
type Action = 'use-login-keyring' | 'status' | 'initialize' | 'unlock' | 'lock' | 'save-provider' | 'delete-provider' | 'open-browser' | 'check' | 'resume-provider';
type Run = (action: Action, body?: Record<string, unknown>) => Promise<boolean>;
const providerLabels: Partial<Record<ProviderId, string>> = {tradingview: 'TradingView'};
const errorNotes: Record<string, string> = {
  keyring_locked: 'Unlock your OS login keyring, then try again. No passwords were submitted.',
  keyring_unavailable: 'The encrypted OS login keyring is unavailable. Unlock it locally or choose a separate passphrase for a new vault.',
  keyring_conflict: 'An unlock entry already exists. Guardian preserved it; contact support before changing this vault.',
  manual_lock_unpersisted: 'The vault is locked for this running service, but Guardian could not save the lock across restarts. Resolve the storage error and lock it again before restarting.',
  invalid_fields: 'Check the form and enter all required fields.',
  invalid_input: 'Check your sign-in fields and passphrase requirements.',
  locked: 'Unlock the Guardian vault before changing saved sign-ins.',
  vault_exists: 'A Guardian vault already exists. Refresh status to unlock it.',
  vault_missing: 'No Guardian vault exists yet. Refresh status to create one.',
  unlock_cooldown: 'Too many unlock attempts. Wait before trying your passphrase again.',
  unsafe_storage: 'Guardian could not verify its local credential storage. Contact support before saving credentials.',
  guardian_busy: 'Guardian is still completing the previous action. Wait, then refresh its status.',
  invalid_passphrase: 'Use a passphrase between 12 and 256 characters.',
  vault_locked: 'Unlock the Guardian vault before changing saved sign-ins.',
  unlock_failed: 'The vault could not be unlocked. Check your passphrase and try again.',
  invalid_credentials: 'Enter a username and password. Saved passwords are never shown here.',
  busy: 'Guardian is still completing the previous action. Wait, then refresh its status.',
  browser_unavailable: 'The Guardian browser could not open. Check the local browser installation and try again.',
  market_chart_not_configured: 'Open your saved TradingView chart from Trading first. Guardian uses that same feed window for sign-in.',
  market_chart_conflict: 'Guardian could not identify the configured TradingView chart. Check its existing feed window; no second chart was opened.',
  market_binding_immutable: 'TradingView is already bound to another feed window. Keep its current chart and review Trading setup.',
  session_unverified: 'Guardian could not verify a signed-in account. Finish sign-in in its browser, then try again.',
  account_mismatch: 'The signed-in account does not match the account ID you entered. Automatic recovery remains paused.',
};
const stateLabels: Record<string, string> = {
  unconfigured: 'Not configured', browser_unavailable: 'Browser unavailable', logged_out: 'Sign-in needed', recovery_paused: 'Recovery paused', login_submitted: 'Recovering sign-in', verifying: 'Checking account', cooldown: 'Waiting to retry', available: 'Saved, not verified',
  unknown: 'Unknown', unavailable: 'Unavailable', disabled: 'Not watching', not_configured: 'Not configured',
  auth_needed: 'Sign-in needed', authentication_required: 'Sign-in needed', needs_login: 'Sign-in needed',
  recovering: 'Recovering sign-in', recovery_pending: 'Waiting to recover', backoff: 'Waiting to retry',
  requires_user: 'Your action needed', challenge: 'Your action needed', mfa_required: 'Your action needed',
  locked: 'Vault locked', vault_locked: 'Vault locked', not_saved: 'Not saved', saved: 'Saved, not verified',
  authenticated: 'Signed in', connected: 'Signed in', login_only: 'Sign-in only',
};
function labelFor(state: string) { return stateLabels[state] || 'Unknown'; }
function when(value: number | null) { return value && Number.isFinite(value) ? new Date(value * 1000).toLocaleString() : 'No observation yet'; }

function VaultForm({configured, mode, busy, run}: {configured: boolean; mode: 'keyring' | 'passphrase'; busy: boolean; run: Run}) {
  const [selected, setSelected] = useState<'keyring' | 'passphrase'>(mode);
  const [passphrase, setPassphrase] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [useKeyring, setUseKeyring] = useState(false);
  const [error, setError] = useState('');
  const needsPassphrase = (configured ? mode : selected) === 'passphrase';
  async function submit(event: FormEvent) {
    event.preventDefault();
    const secret = passphrase;
    const matches = configured || !needsPassphrase || secret === confirmation;
    setPassphrase(''); setConfirmation(''); setError('');
    if (!matches) { setError('The passphrases did not match. Enter them again.'); return; }
    if (configured && mode === 'passphrase' && useKeyring) {
      await run('use-login-keyring', {passphrase: secret, acknowledgeLoginUnlock: true});
    } else if (configured) {
      await run('unlock', needsPassphrase ? {passphrase: secret} : {});
    } else {
      await run('initialize', {unlockMode: selected, ...(needsPassphrase ? {passphrase: secret} : {})});
    }
  }
  return <form className="guardian-vault-form" onSubmit={event => void submit(event)} autoComplete="off">
    <h3>{configured ? 'Unlock saved sign-ins' : 'Create an encrypted vault'}</h3>
    {!configured && <><label htmlFor="guardian-unlock-mode">Unlock saved sign-ins</label><select id="guardian-unlock-mode" value={selected} disabled={busy} onChange={event => {setSelected(event.target.value as 'keyring' | 'passphrase');setPassphrase('');setConfirmation('');}}><option value="keyring">With OS login (recommended)</option><option value="passphrase">With a separate passphrase</option></select></>}
    <p>{needsPassphrase ? 'Enter your separate vault passphrase after each restart. Without it, automatic sign-in stays locked.' : 'The encrypted OS login keyring unlocks saved sign-ins when you sign in to the OS. If that keyring stays locked, automatic sign-in waits for you.'}</p>
    {needsPassphrase && <div className="guardian-field-grid">
      <div><label htmlFor="guardian-passphrase">Vault passphrase</label><input id="guardian-passphrase" type="password" autoComplete={configured ? 'current-password' : 'new-password'} value={passphrase} onChange={event => setPassphrase(event.target.value)} minLength={12} maxLength={256} required disabled={busy}/></div>
      {!configured && <div><label htmlFor="guardian-passphrase-confirmation">Confirm passphrase</label><input id="guardian-passphrase-confirmation" type="password" autoComplete="new-password" value={confirmation} onChange={event => setConfirmation(event.target.value)} minLength={12} maxLength={256} required disabled={busy}/></div>}
    </div>}
    {configured && mode === 'passphrase' && <label><input type="checkbox" checked={useKeyring} disabled={busy} onChange={event => setUseKeyring(event.target.checked)}/>Use my encrypted OS login keyring for future unlocks</label>}
    <p className="field-hint">Automatic sign-in is off until you choose it for each provider. Verification codes and challenges still need you.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="guardian-actions"><button className="primary" disabled={busy}>{configured ? 'Unlock saved sign-ins' : 'Create encrypted vault'}</button></div>
  </form>;
}

function ProviderSignIn({provider, unlocked, busy, run, nativeAccountId}: {provider: Provider; unlocked: boolean; busy: boolean; run: Run; nativeAccountId?: string}) {
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [autoLogin, setAutoLogin] = useState(provider.autoLogin);
  const [resumeAccount, setResumeAccount] = useState(nativeAccountId || '');
  const [manualSignInComplete, setManualSignInComplete] = useState(false);
  const label = providerLabels[provider.id] || provider.label;
  const needsNativeSignIn = provider.requiresUser && !['locked', 'not_saved'].includes(provider.state);
  async function submit(event: FormEvent) {
    event.preventDefault();
    const payload = {provider: provider.id, username: username.trim(), password, autoLogin};
    setUsername(''); setPassword('');
    if (await run('save-provider', payload)) setEditing(false);
  }
  return <section className="guardian-provider" aria-labelledby={'guardian-' + provider.id + '-heading'}>
    <div className="guardian-row"><h3 id={'guardian-' + provider.id + '-heading'}>{label}</h3><span className="pill muted">{needsNativeSignIn ? 'Your action needed' : provider.state === 'disabled' ? 'Recovery off' : labelFor(provider.state)}</span></div>
    <p>Sign-in uses the saved chart already opened from Trading. Guardian does not open a second chart or restart its feed, and does not change alerts or strategy rules.</p>
    <dl className="guardian-facts"><div><dt>Credentials</dt><dd>{provider.saved ? 'Saved in Guardian vault' : 'Not saved'}</dd></div><div><dt>Automatic sign-in</dt><dd>{provider.saved && provider.autoLogin ? 'Allowed while unlocked' : 'Off'}</dd></div></dl>
    {needsNativeSignIn && <div className="callout guardian-challenge" role="status"><p><strong>Complete sign-in in this provider's browser window</strong>Enter any verification code or solve the CAPTCHA there. Guardian will not bypass it. Then verify the signed-in account below.</p>
      {provider.saved && <form onSubmit={event => {event.preventDefault(); void run('resume-provider', {provider: provider.id, nativeAccountId: resumeAccount.trim(), acknowledgeManualSignIn: true});}}>
        <label htmlFor={'guardian-resume-' + provider.id}>Signed-in {label} username</label><input id={'guardian-resume-' + provider.id} value={resumeAccount} onChange={event => setResumeAccount(event.target.value)} required maxLength={36} autoComplete="off" disabled={busy}/>
        <label className="checkbox"><input type="checkbox" checked={manualSignInComplete} onChange={event => setManualSignInComplete(event.target.checked)} disabled={busy}/><span>I completed sign-in in this provider's browser window.</span></label>
        <div className="guardian-actions"><button className="secondary" disabled={busy || !manualSignInComplete}>Verify {label} sign-in and resume</button></div>
        <p className="field-hint">Guardian checks the actual account before resuming. This does not reset its retry limit.</p>
      </form>}
    </div>}
    {provider.nextAttemptAt && <p className="field-hint">Next permitted recovery attempt: {when(provider.nextAttemptAt)}.</p>}
    <div className="guardian-actions">
      <button className="secondary" disabled={busy} onClick={() => void run('open-browser', {provider: provider.id})}>Show TradingView feed window</button>
      <button className="text-button" disabled={busy || !unlocked} onClick={() => { setEditing(!editing); setUsername(''); setPassword(''); setAutoLogin(provider.autoLogin); setRemoving(false); }}>{editing ? 'Cancel credential change' : provider.saved ? 'Replace saved sign-in' : 'Save a sign-in'}</button>
      {provider.saved && <button className="text-button danger" disabled={busy || !unlocked} onClick={() => { setRemoving(true); setEditing(false); setUsername(''); setPassword(''); }}>Remove saved sign-in</button>}
    </div>
    {editing && unlocked && <form className="guardian-provider-form" onSubmit={event => void submit(event)} autoComplete="off">
      <div className="guardian-field-grid"><div><label htmlFor={'guardian-' + provider.id + '-username'}>{label} username (not email)</label><input id={'guardian-' + provider.id + '-username'} value={username} onChange={event => setUsername(event.target.value)} autoComplete="off" maxLength={254} required disabled={busy} spellCheck={false} autoCapitalize="none"/></div><div><label htmlFor={'guardian-' + provider.id + '-password'}>{label} password</label><input id={'guardian-' + provider.id + '-password'} type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="new-password" maxLength={1024} required disabled={busy}/></div></div>
      <label className="checkbox"><input type="checkbox" checked={autoLogin} onChange={event => setAutoLogin(event.target.checked)} disabled={busy}/><span>Allow Guardian to sign back in to {label} automatically</span></label>
      <p className="field-hint">Off unless you choose it. Recovery requires an unlocked vault. Verification prompts still need you. Saving credentials does not prove sign-in succeeded.</p>
      <div className="guardian-actions"><button className="primary" disabled={busy}>Save {label} credentials</button></div>
    </form>}
    {removing && <div className="guardian-remove" role="group" aria-label={'Remove ' + label + ' sign-in'}><p>Remove the saved {label} username and password and turn off automatic sign-in? This does not sign out an open browser.</p><div className="guardian-actions"><button className="secondary danger" disabled={busy} onClick={async () => { if (await run('delete-provider', {provider: provider.id})) setRemoving(false); }}>Confirm removal</button><button className="text-button" disabled={busy} onClick={() => setRemoving(false)}>Keep saved sign-in</button></div></div>}
  </section>;
}

export function GuardianConnection() {
  const [status, setStatus] = useState<GuardianStatus | null>(null);
  const [pending, setPending] = useState<Action | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const mounted = useRef(false);
  const active = useRef(false);
  const generation = useRef(0);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const run = useCallback<Run>(async (action, body = {}) => {
    if (active.current) return false;
    active.current = true;
    const expected = generation.current;
    setPending(action);
    if (action !== 'status') { setError(''); setNotice(''); }
    try {
      const next = await api<GuardianStatus>('/guardian/' + action, body);
      if (!mounted.current || generation.current !== expected) return false;
      if (!next || typeof next.enabled !== 'boolean' || typeof next.vault?.configured !== 'boolean' || typeof next.vault?.unlocked !== 'boolean' || !Array.isArray(next.providers)) throw new Error('Invalid status');
      setStatus(next); setError('');
      if (action === 'status') setNotice('');
      if (action !== 'status') setNotice({'use-login-keyring': 'Saved sign-ins will unlock with your OS login keyring. Provider automatic sign-in choices are unchanged.', initialize: 'Guardian vault created and unlocked.', unlock: 'Guardian vault unlocked.', lock: 'Guardian vault locked. Automatic sign-in cannot use saved credentials while locked.', 'save-provider': 'Sign-in saved in the encrypted vault. Connection verification is separate.', 'delete-provider': 'Saved sign-in removed. Automatic sign-in is off for this provider.', 'open-browser': 'Continue in the Guardian browser on this device. Keep verification codes out of this page and chat.', check: 'Sign-in check completed. Review provider status below.', 'resume-provider': 'The signed-in account was verified. Automatic recovery can resume within its existing retry limits.'}[action]);
      return true;
    } catch (cause) {
      if (!mounted.current || generation.current !== expected) return false;
      setStatus(null);
      // Do not display transport errors or provider payloads. They may contain secrets.
      setError(cause instanceof ApiError && errorNotes[cause.code] ? errorNotes[cause.code] : 'Guardian could not confirm this action. Refresh status before trying again.');
      setNotice('');
      return false;
    } finally {
      if (generation.current === expected) { active.current = false; if (mounted.current) setPending(null); }
    }
  }, []);
  useEffect(() => {
    mounted.current = true; void run('status');
    const timer = setInterval(() => { if (!document.hidden) void run('status'); }, 10_000);
    return () => { mounted.current = false; generation.current += 1; active.current = false; clearInterval(timer); };
  }, [run]);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);
  const enabled = status?.enabled === true;
  const busy = pending !== null;
  const unlocked = status?.vault.unlocked === true;
  return <section className="panel guardian-panel" aria-labelledby="guardian-heading">
    <div className="section-title"><h2 id="guardian-heading">Guardian sign-ins</h2><span className="pill muted" role="status">{!status ? 'Unknown' : enabled ? (unlocked ? 'Vault unlocked' : 'Vault locked') : 'Unavailable'}</span></div>
    <p>Optional. Guardian keeps an encrypted copy of your TradingView sign-in so it can sign the chart back in after a session expires, with your permission. It does not trade or read your accounts.</p>
    <p className="field-hint">Tradovate is not saved here. The bridge window on workspace 3 keeps that login in the browser, encrypted by your desktop keyring.</p>
    {error && <p className="error" ref={errorRef} tabIndex={-1} role="alert">{error}</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {status && !enabled && <p className="callout">Guardian is unavailable on this local service. No saved sign-ins or observations have been verified.</p>}
    {enabled && status && <>
      <div className="guardian-vault">
        <div className="guardian-row"><h3>Saved sign-ins</h3><span className="pill muted">{unlocked ? 'Vault unlocked' : status.vault.configured ? 'Vault locked' : 'Vault not created'}</span></div>
        {unlocked ? <><p>Guardian can use saved credentials while this vault is unlocked. {status.vault.unlockMode === 'keyring' ? 'Your OS login keyring can unlock it after a restart.' : 'You must enter your vault passphrase after a restart.'} Automatic sign-in still requires your opt-in for each provider.</p><button className="secondary" disabled={busy} onClick={() => void run('lock')}>Lock Guardian vault</button></> : <VaultForm key={String(status.vault.configured)} configured={status.vault.configured} mode={status.vault.unlockMode || (status.vault.configured ? 'passphrase' : 'keyring')} busy={busy} run={run}/>}
        <p className="field-hint">The vault protects saved credentials, not the whole device or browser cookies. Locking does not sign out an open browser.</p>
      </div>
      <div className="guardian-providers">{status.providers.map(row => {
        const provider = row; const id = row.id;
        return <ProviderSignIn key={id + String(unlocked)} provider={provider} unlocked={unlocked} busy={busy} run={run}/>;
      })}</div>
      <div className="guardian-actions"><button className="text-button" disabled={busy} onClick={() => void run('check')}>{pending === 'check' ? 'Checking sign-ins…' : 'Check sign-ins'}</button><button className="text-button" disabled={busy} onClick={() => void run('status')}>Refresh Guardian status</button></div>
    </>}
    <p className="field-hint guardian-scope">This panel manages the saved TradingView sign-in only. Orders go through the Tradovate bridge below.</p>
  </section>;
}
