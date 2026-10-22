import {useCallback, useEffect, useRef, useState} from 'react';
import {api} from './api';

type HarnessStatus = {
  enabled: boolean;
  status: 'not_started' | 'starting' | 'ready' | 'needs_login' | 'connected' | 'error' | 'unavailable';
  runtime: 'OpenClaw';
  provider: 'Claude';
  credentialPersistence: 'session_only' | 'encrypted';
  storage?: {configured: boolean; unlocked: boolean; error: string | null; busy: boolean; operation: 'setup' | 'unlock' | null};
  gatewayPort: number;
  model: string | null;
  loginRunning: boolean;
  consoleRunning: boolean;
  ictRunning?: boolean;
  verifyRunning?: boolean;
  stopRunning?: boolean;
  scopeUpdatedAt: string | null;
  error: string | null;
  // Installed system: every privileged step runs in a terminal on workspace 4 where sudo asks
  // for the password; the status here is the one the runtime published after its last step.
  passwordInTerminal?: boolean;
};
type Action = 'status' | 'start' | 'login' | 'console' | 'ict' | 'stop' | 'storage_setup' | 'storage_unlock' | 'verify';
const labels: Record<HarnessStatus['status'], string> = {
  not_started: 'Not started', starting: 'Starting', ready: 'Signed in; response unverified',
  needs_login: 'Sign-in needed', connected: 'Account connected', error: 'Needs attention', unavailable: 'Unavailable',
};
const notes: Record<HarnessStatus['status'], string> = {
  not_started: 'Start your assistant, then connect your own Claude account. Nothing starts automatically.',
  starting: 'The assistant runtime is starting. Wait for its status before signing in.',
  ready: 'Sign-in completed. Select Verify connection to check a model response, then open the assistant.',
  needs_login: 'Choose Connect Claude. A terminal opens on workspace 4: pick subscription (setup-token) or API key and paste it there.',
  connected: 'A model response has been verified for this runtime. Open the assistant to continue.',
  error: 'The runtime needs attention. Refresh its status before trying again.',
  unavailable: 'The assistant runtime is unavailable. Refresh its status to try again.',
};

export function HarnessConnection() {
  const [status, setStatus] = useState<HarnessStatus | null>(null);
  const [pending, setPending] = useState<Action | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [storageNotice, setStorageNotice] = useState('');
  const active = useRef(false);
  const mounted = useRef(false);
  const generation = useRef(0);

  const run = useCallback(async (action: Action) => {
    if (active.current) return;
    active.current = true;
    const expected = generation.current;
    setPending(action);
    if (action !== 'status') { setError(''); setNotice(''); }
    try {
      const next = await api<HarnessStatus>('/harness/' + action, {});
      if (!mounted.current || expected !== generation.current) return;
      setStatus(next);
      setError('');
      if (action === 'status') setNotice('');
      if (next.storage?.error || (next.storage?.unlocked && !next.storage.busy && ['ready', 'needs_login', 'connected'].includes(next.status))) setStorageNotice('');
      if (action === 'storage_setup' || action === 'storage_unlock') setStorageNotice('Continue in the storage terminal on workspace 4. Enter your passphrase there, not in this page or chat. Status updates after the terminal completes.');
      if ((action === 'login' || action === 'start') && next.loginRunning) setNotice(next.passwordInTerminal ? 'Continue in the terminal on workspace 4 (Super+4). It asks for your password, then walks you through start and sign-in. This page updates after each step.' : 'Continue in the sign-in terminal on workspace 4.');
      if (action === 'verify' && next.verifyRunning) setNotice('Continue in the terminal on workspace 4. The result shows here after it finishes.');
      if (action === 'stop' && next.stopRunning) setNotice('Confirm the stop in the terminal on workspace 4.');
      if (action === 'verify' && !next.error) setNotice(next.status === 'connected'
        ? 'Connection verified. Open the assistant to continue.'
        : next.status === 'ready' ? 'The response check completed. Waiting for the runtime status to update.' : '');
      if (action === 'console' && next.consoleRunning) setNotice('The assistant terminal is open on workspace 4.');
      if (action === 'ict' && next.ictRunning) setNotice('The ICT assistant is open on workspace 4.');
      if (action === 'stop' && next.status === 'not_started') {
        setStorageNotice('');
        setNotice(next.storage?.configured
          ? next.storage.unlocked ? 'The assistant stopped, but saved sign-in is still unlocked. Check the storage error before leaving this device.' : 'Assistant stopped and saved sign-in locked. Your sign-in and history are saved.'
          : 'Temporary runtime stopped. Sign in again next time.');
      }
    } catch (cause) {
      if (!mounted.current || expected !== generation.current) return;
      // Never retain a successful-looking state after an unknown action outcome.
      setStatus(previous => previous ? {...previous, status: 'unavailable', error: null} : null);
      setError(cause instanceof Error ? cause.message : 'The local service could not complete this action. Refresh status to try again.');
      setNotice('');
      setStorageNotice('');
      if (action !== 'status') {
        try {
          const next = await api<HarnessStatus>('/harness/status', {});
          if (mounted.current && expected === generation.current) setStatus(next);
        } catch { /* Keep unavailable and the original action error. */ }
      }
    } finally {
      if (expected === generation.current) {
        active.current = false;
        if (mounted.current) setPending(null);
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void run('status');
    const timer = setInterval(() => { if (!document.hidden) void run('status'); }, 15_000);
    return () => { mounted.current = false; generation.current += 1; active.current = false; clearInterval(timer); };
  }, [run]);

  const busy = pending !== null || status?.storage?.busy === true;
  const enabled = status?.enabled === true;
  const storageConfigured = status?.storage?.configured === true;
  const encrypted = storageConfigured || status?.credentialPersistence === 'encrypted';
  // If encrypted storage metadata is missing, never offer a fresh temporary start.
  const storageLocked = encrypted && status?.storage?.unlocked !== true;
  const canLogin = enabled && !storageLocked && status.status === 'needs_login' && !status.loginRunning;
  const canVerify = enabled && !storageLocked && !status?.storage?.error && !status.loginRunning && ['ready', 'connected'].includes(status.status);
  const canStop = enabled && (status.status !== 'not_started' || status.storage?.unlocked === true);
  const canSetupStorage = enabled && !encrypted && !['starting', 'unavailable'].includes(status.status) && !status.loginRunning;
  const terminal = status?.passwordInTerminal === true;
  const label = status ? status.storage?.busy ? status.storage.operation === 'setup' ? 'Setting up encrypted storage' : 'Unlocking saved sign-in' : storageLocked && enabled && !['unavailable', 'error'].includes(status.status) ? 'Saved sign-in is locked' : labels[status.status] || 'Unavailable' : error ? 'Unavailable' : 'Checking status';
  return <section className="panel harness-panel" aria-labelledby="harness-heading">
    <div className="ai-connection">
      <div className="section-title"><h2 id="harness-heading">Claude account</h2><span className="pill" role="status">{label}</span></div>
      <p>Connect your own Claude subscription (setup-token) or Anthropic API key. Sign-in and both assistants open in a terminal on workspace 4 (Super+4).{terminal && ' Each step asks for your password there; this page shows the status the runtime reported after its last step.'}</p>
      <p>{status ? !enabled ? 'This connection is not enabled on the local service.' : status.storage?.busy ? 'Finish the storage step in the terminal. Assistant controls are unavailable until it completes.' : storageLocked ? 'Choose Unlock saved sign-in. The assistant stays stopped until you unlock.' : terminal && status.status === 'not_started' ? 'Choose Start and connect. A terminal opens on workspace 4: it starts your assistant, then asks for your Claude setup-token or API key.' : notes[status.status] || notes.unavailable : error ? notes.unavailable : 'Checking the local runtime. Nothing starts automatically.'}</p>
      {error && <p className="error" role="alert">{error}</p>}
      {status?.error && <p className="error" role="alert">{status.error}</p>}
      {status?.storage?.error && <p className="error" role="alert">{status.storage.error}</p>}
      {(storageNotice || status?.storage?.busy) && <p className="notice" role="status">{storageNotice || 'Continue in the storage terminal on workspace 4. Enter your passphrase there, not in this page or chat. Status updates after the terminal completes.'}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      <p><strong>{encrypted ? storageLocked ? 'Saved sign-in is locked' : 'Encrypted storage is unlocked' : 'Session-only sign-in'}</strong>{encrypted
        ? storageLocked ? '. Your sign-in and history remain saved and encrypted.' : '. Sign-in and history are saved. Use Stop and lock before leaving this device.'
        : '. Stopping the runtime discards your sign-in and console history. Encrypted storage is optional.'}</p>
      <div className="ai-actions">
        {storageLocked ? <button className="primary" disabled={busy || !enabled || !storageConfigured || status?.status === 'starting'} onClick={() => void run('storage_unlock')}>Unlock saved sign-in</button> : status?.status === 'not_started' ? <button className="primary" disabled={busy || !enabled} onClick={() => void run('start')}>{terminal ? 'Start and connect' : 'Start assistant'}</button> : status?.status === 'needs_login' ? <button className="primary" disabled={busy || !canLogin} onClick={() => void run('login')}>{status?.loginRunning ? 'Sign-in terminal open' : 'Connect Claude'}</button> : status?.status === 'starting' ? <button className="primary" disabled>Starting assistant…</button> : ['ready', 'connected'].includes(status?.status || '') && <>
          <button className={status?.status === 'ready' ? 'primary' : 'secondary'} disabled={busy || !canVerify} onClick={() => void run('verify')}>{pending === 'verify' ? 'Checking connection…' : 'Verify connection'}</button>
          <button className={status?.status === 'connected' ? 'primary' : 'secondary'} disabled={busy || !enabled || storageLocked || !['ready', 'connected'].includes(status?.status || '') || status?.consoleRunning} onClick={() => void run('console')}>{status?.consoleRunning ? 'Assistant terminal open' : 'Open assistant'}</button>
          <button className="secondary" disabled={busy || !enabled || storageLocked || !['ready', 'connected'].includes(status?.status || '') || status?.ictRunning} onClick={() => void run('ict')}>{status?.ictRunning ? 'ICT assistant open' : 'Open ICT assistant'}</button>
        </>}
        <button className={!status || ['error', 'unavailable'].includes(status?.status || '') ? 'primary' : 'text-button'} disabled={pending !== null} onClick={() => void run('status')}>Refresh status</button>
      </div>
      <details className="ai-disclosure runtime-controls"><summary>Storage and stop controls</summary>
        <p>Keep sign-in across restarts with encrypted storage, or stop this assistant.</p>
        <div className="ai-actions">
          {!encrypted && <button className="secondary" disabled={busy || !canSetupStorage} onClick={() => void run('storage_setup')}>Keep sign-in after restart</button>}
          <button className="secondary ai-delete" disabled={busy || !canStop} onClick={() => {
            const confirmation = encrypted
              ? 'Stop the assistant and lock your saved sign-in and conversation history? They will not be deleted. Other services will not be changed.'
              : 'Stop this temporary assistant runtime and discard its sign-in and console history? You will need to sign in again. Other services will not be changed.';
            if (window.confirm(confirmation)) void run('stop');
          }}>{encrypted ? 'Stop and lock' : 'Stop assistant'}</button>
        </div>
      </details>
      <p className="field-hint">Verify connection sends a short test through your Claude account and counts toward your plan or API usage.</p>
      {pending !== null && <p className="field-hint" role="status">{pending === 'status' ? 'Refreshing runtime status.' : pending === 'verify' ? 'Waiting for a model response. This can take up to two minutes.' : 'Waiting for the local runtime. Its status will update when the request finishes.'}</p>}
      <p className="field-hint">Neither assistant has repair or trading tools. The ICT assistant can only check the time and read public web pages. Saving sign-in does not add permissions.</p>
      <details className="ai-disclosure"><summary>Subscription sign-in and the optional helper</summary>
        <p>A Claude subscription (Pro or Max) signs in with a setup-token. You get one by running <code>claude setup-token</code> in Claude Code on any computer, then pasting it into the terminal here.</p>
        <p>No Claude Code anywhere? The sign-in terminal offers to install a sign-in helper (one pinned, hash-checked Claude Code release from the public npm registry) into your own account and run that command for you. It is optional, asks first, and is never bundled with Decrypt OS. An API key needs no helper.</p>
      </details>
      <details className="ai-disclosure"><summary>Storage and usage details</summary>
        <p>{encrypted
          ? 'Unlock once after reboot, or after Stop and lock. Your passphrase is not stored and cannot be recovered. The assistant can access saved sign-in and history while storage is unlocked.'
          : 'To keep sign-in after restart, create a passphrase in the storage terminal on workspace 4. Setup can move your current customer session into encrypted storage. The passphrase is not stored and cannot be recovered.'}</p>
        <p>The vault protects saved sign-in and history while locked. It does not encrypt the whole OS.</p>
        <p>Model access and usage limits depend on your Claude plan or API account. Usage is not shown here.</p>
        <p>Model: Claude Opus 5.5, with Claude Sonnet 5 as fallback. Availability is confirmed only by a successful response.</p>
      </details>
    </div>
  </section>;
}
