import {useCallback, useEffect, useRef, useState} from 'react';
import {api} from './api';

// Read-only snapshots of the two local services the Home page and Connections summarise.
// Both calls are the same status reads the detail panels make; nothing here changes state.
export type LiveHarness = {enabled: boolean; status: string; loginRunning?: boolean; consoleRunning?: boolean; ictRunning?: boolean; storage?: {configured: boolean; unlocked: boolean; busy?: boolean}};
export type LiveBridge = {configured: boolean; paused: boolean | null; firing: boolean; state: string; logins: {id: string; label: string; primary: boolean}[]};
export type LiveTrading = {enabled: boolean; bridge?: LiveBridge; instances: {instanceId: string; entriesHeld: boolean; runtimeState: string; binding: {symbol: string; accountMode?: string}}[]};

export function useLiveStatus(intervalMs = 15000) {
  const [harness, setHarness] = useState<LiveHarness | null>(null);
  const [trading, setTrading] = useState<LiveTrading | null>(null);
  const [checked, setChecked] = useState(false);
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    const [h, t] = await Promise.allSettled([api<LiveHarness>('/harness/status', {}), api<LiveTrading>('/trading/status', {})]);
    if (!mounted.current) return;
    setHarness(h.status === 'fulfilled' && typeof h.value?.status === 'string' ? h.value : null);
    setTrading(t.status === 'fulfilled' && Array.isArray(t.value?.instances) ? t.value : null);
    setChecked(true);
  }, []);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, intervalMs);
    return () => { mounted.current = false; clearInterval(timer); };
  }, [refresh, intervalMs]);
  return {harness, trading, checked, refresh};
}

export function claudeConnected(h: LiveHarness | null) {
  return !!h && h.enabled && ['ready', 'connected'].includes(h.status);
}

export function claudeLabel(h: LiveHarness | null, checked: boolean) {
  if (!checked) return 'Checking';
  if (!h || !h.enabled) return 'Unavailable';
  if (h.storage?.configured && !h.storage.unlocked && h.status !== 'connected') return 'Saved sign-in locked';
  return ({not_started: 'Not started', starting: 'Starting', needs_login: 'Sign-in needed', ready: 'Signed in', connected: 'Connected and verified', error: 'Needs attention', unavailable: 'Unavailable'} as Record<string, string>)[h.status] || 'Unknown';
}

export function bridgeLabel(t: LiveTrading | null, checked: boolean) {
  if (!checked) return 'Checking';
  const b = t?.bridge;
  if (!t?.enabled || !b) return 'Unavailable';
  if (!b.configured) return 'Not set up';
  return b.paused === false ? 'Set up · entries allowed' : b.paused === true ? 'Set up · entries paused' : 'Set up';
}
