// Tokens remain inside this closure. No orders are replayed after connection loss.
(() => {
  const CFG = __DLB_CONFIG__;
  // Top frame only. Injected-on-new-document scripts also run in the app's iframes
  // (Stripe, sandboxed helpers); an iframe copy has no controller, so its lease expired
  // after 30 s and its event made the bridge distrust a healthy connection.
  try { if (window.top !== window) return; } catch (_) { return; }
  if (window[CFG.api]) return;
  const NativeWS = window.WebSocket;
  const nativeFetch = window.fetch;
  const nativeOpen = XMLHttpRequest.prototype.open;
  const tradePattern = new RegExp(CFG.tradeUrlPattern);
  const opts = CFG.lifecycle || {};
  const idleMs = opts.idleMs || 15000;
  const connectMs = opts.connectMs || 15000;
  const retryMs = opts.retryMs || 2000;
  const maxRetryMs = opts.maxRetryMs || 30000;
  const tickMs = opts.tickMs || 500;
  const heartbeatMs = opts.heartbeatMs || 2500;
  const leaseMs = opts.leaseMs || 30000;
  const instance = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const readEndpoints = new Set(['account/list', 'user/syncrequest', 'contract/find', 'contract/item']);
  let token = null, tradeUrl = null, own = null, epoch = null;
  let state = 'waiting_for_app_login', lastRx = null, connectedAt = 0;
  let seq = 0, connections = 0, tokenGeneration = 0, reconnectAttempts = 0;
  let heartbeat = null, reconnectTimer = null, syncTimer = null, watchdog = null;
  let stopped = false, synced = false, binding = null, propsAfterSnapshot = null;
  const pending = new Map(), appHooks = new Set(), xhrHooks = new Set();
  const now = () => performance.now();
  let controllerSeen = now();
  let leaseTimer = null;
  const takeBinding = () => {
    if (!binding && typeof window[CFG.binding] === 'function') {
      binding = window[CFG.binding];
      try { delete window[CFG.binding]; } catch (_) {}
    }
    return binding;
  };
  const emit = (type, data) => {
    if (stopped) return;
    try { const fn = takeBinding(); if (fn) fn(JSON.stringify({type, data, epoch, at: Date.now()})); } catch (_) {}
  };
  function validTradeUrl(value) {
    try {
      const u = new URL(value, location.href);
      // The app adds a Math.random() cache-buster (?r=0.123...). Accept only that
      // exact parameter and drop it. No prefix matching, userinfo, other query
      // parameters (e.g. tokens), or fragments.
      if (u.search) {
        const params = [...u.searchParams];
        if (params.length !== 1 || params[0][0] !== 'r' || !/^[01](\.\d{1,20})?$/.test(params[0][1])) return null;
        u.search = '';
      }
      const m = u.href.match(tradePattern);
      return !u.username && !u.password && !u.search && !u.hash &&
        ['ws:', 'wss:'].includes(u.protocol) && m && m[0] === u.href ? u.href : null;
    } catch (_) { return null; }
  }
  function approvedTokenUrl(value) {
    try {
      const u = new URL(value, location.href);
      if (u.username || u.password || u.hash || u.search) return false;
      if (tradeUrl) {
        const current = new URL(tradeUrl);
        if (u.host !== current.host || (u.protocol === 'https:') !== (current.protocol === 'wss:')) return false;
      }
      const socketUrl = u.protocol === 'https:' ? 'wss://' : u.protocol === 'http:' ? 'ws://' : null;
      // REST origin must independently pass the exact trading endpoint policy.
      return socketUrl && validTradeUrl(socketUrl + u.host + '/v1/websocket') &&
        ['/v1/auth/accesstokenrequest', '/v1/auth/renewaccesstoken'].includes(u.pathname.toLowerCase());
    } catch (_) { return false; }
  }
  function learnToken(value, url) {
    if (stopped || typeof value !== 'string' || value.length < 16) return;
    const changed = value !== token;
    const changedUrl = url && url !== tradeUrl;
    token = value;
    if (url) tradeUrl = url;
    if (changed) { tokenGeneration++; emit('token_seen', {generation: tokenGeneration}); }
    // A rotation never interrupts an authorized request. Reconnect uses the newest token.
    if (changedUrl && own) lose('endpoint_changed');
    if (!own && tradeUrl && (state !== 'auth_failed' || changed)) connect();
  }
  function Wrapped(url, protocols) {
    const socket = protocols === undefined ? new NativeWS(url) : new NativeWS(url, protocols);
    const target = validTradeUrl(String(url));
    if (target && !stopped) {
      const original = socket.send;
      const hook = function(data) {
        if (!stopped && typeof data === 'string' && data.startsWith('authorize\n'))
          learnToken(data.split('\n').slice(3).join('\n'), target);
        return original.call(this, data);
      };
      socket.send = hook;
      const entry = {socket, original, hook};
      appHooks.add(entry);
      socket.addEventListener('close', () => appHooks.delete(entry), {once: true});
    }
    return socket;
  }
  Wrapped.prototype = NativeWS.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) Object.defineProperty(Wrapped, k, {value: NativeWS[k]});
  window.WebSocket = Wrapped;
  function tokenFromJson(text, url) {
    if (!approvedTokenUrl(url)) return;
    try {
      const body = JSON.parse(text);
      const u = new URL(url, location.href);
      learnToken(body && body.accessToken, (u.protocol === 'https:' ? 'wss://' : 'ws://') + u.host + '/v1/websocket');
    } catch (_) {}
  }
  const wrappedFetch = function(input, init) {
    const url = String(input && input.url ? input.url : input);
    const result = nativeFetch.apply(this, arguments);
    if (!stopped && approvedTokenUrl(url)) result.then(r => {
      if (r.ok && approvedTokenUrl(r.url)) r.clone().text().then(text => tokenFromJson(text, r.url)).catch(() => {});
    }).catch(() => {});
    return result;
  };
  if (nativeFetch) window.fetch = wrappedFetch;
  const wrappedOpen = function(method, url) {
    if (!stopped && approvedTokenUrl(String(url))) {
      const xhr = this;
      const hook = () => {
        xhrHooks.delete(entry);
        try {
          if (xhr.status >= 200 && xhr.status < 300 && approvedTokenUrl(xhr.responseURL))
            tokenFromJson(xhr.responseType === 'json' ? JSON.stringify(xhr.response) : xhr.responseText, xhr.responseURL);
        } catch (_) {}
      };
      const entry = {xhr, hook};
      xhrHooks.add(entry);
      xhr.addEventListener('load', hook, {once: true});
    }
    return nativeOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.open = wrappedOpen;
  function settleAll(code) {
    for (const p of pending.values()) { clearTimeout(p.timer); p.resolve({ok: false, sent: true, code}); }
    pending.clear();
  }
  function clearConnectionTimers() {
    clearInterval(heartbeat); clearInterval(watchdog); clearTimeout(syncTimer);
    heartbeat = watchdog = syncTimer = null;
  }
  function lose(code, authFailed = false) {
    if (stopped) return;
    const old = own; own = null; epoch = null; synced = false; propsAfterSnapshot = null;
    clearConnectionTimers();
    settleAll(code);
    state = authFailed ? 'auth_failed' : 'closed';
    emit(authFailed ? 'auth_failed' : 'closed', {reason: code});
    if (old) { old.onmessage = old.onclose = old.onerror = null; try { old.close(); } catch (_) {} }
    clearTimeout(reconnectTimer);
    if (!authFailed) {
      const delay = Math.min(maxRetryMs, retryMs * Math.pow(2, Math.min(reconnectAttempts++, 8)));
      reconnectTimer = setTimeout(() => { if (!stopped && token && tradeUrl && !own) connect(); }, delay);
    }
  }
  function connect() {
    if (stopped || own || !token || !tradeUrl) return;
    clearTimeout(reconnectTimer); clearConnectionTimers();
    epoch = null; synced = false; state = 'connecting'; lastRx = null; connectedAt = now();
    emit('connecting', {});
    let socket;
    try { socket = new NativeWS(tradeUrl); } catch (_) { lose('socket_creation_failed'); return; }
    own = socket;
    socket.onmessage = event => {
      if (stopped || socket !== own) return;
      lastRx = now();
      const frame = String(event.data), kind = frame.charAt(0);
      if (kind === 'o') {
        try { socket.send('authorize\n0\n\n' + token); } catch (_) { lose('authorize_send_failed'); }
      } else if (kind === 'a') {
        let items;
        try { items = JSON.parse(frame.slice(1)); if (!Array.isArray(items)) throw Error(); }
        catch (_) { emit('bad_frame', {}); lose('invalid_frame'); return; }
        for (const item of items) { if (socket !== own) break; handle(item); }
      } else if (kind === 'c') lose('server_close');
    };
    socket.onclose = () => { if (socket === own) lose('socket_closed_after_send'); };
    socket.onerror = () => { if (socket === own) lose('socket_error_after_send'); };
    heartbeat = setInterval(() => {
      if (socket === own && socket.readyState === 1) try { socket.send('[]'); } catch (_) { lose('heartbeat_send_failed'); }
    }, heartbeatMs);
    watchdog = setInterval(() => {
      if (socket !== own) return;
      if (state === 'connecting' && now() - connectedAt > connectMs) lose('authorization_timeout');
      else if (state === 'authorized' && now() - (lastRx === null ? connectedAt : lastRx) > idleMs) lose('receive_timeout');
    }, tickMs);
  }
  function handle(item) {
    if (item && item.i === 0 && state === 'connecting') {
      if (item.s === 200) {
        state = 'authorized'; epoch = instance + ':' + (++connections);
        emit('authorized', {}); sync(1);
      } else lose('authorization_refused', true);
      return;
    }
    if (item && typeof item.i === 'number' && pending.has(item.i)) {
      const p = pending.get(item.i); pending.delete(item.i); clearTimeout(p.timer);
      if (p.endpoint === 'user/syncrequest' && item.s === 200 && !synced) propsAfterSnapshot = [];
      p.resolve({ok: item.s === 200, sent: true, s: item.s, d: item.d});
      if (item.s === 401) lose('session_authorization_refused', true);
      return;
    }
    if (state === 'authorized' && item && item.e === 'props') {
      if (synced) emit('props', item.d);
      else if (propsAfterSnapshot) propsAfterSnapshot.push(item.d);
    }
    // Other unsolicited event payloads may contain credentials: do not forward them.
  }
  function request(endpoint, query, body, timeoutMs, expectedEpoch) {
    return new Promise(resolve => {
      if (CFG.readOnly && !readEndpoints.has(endpoint)) {
        resolve({ok: false, sent: false, code: 'read_only_endpoint'}); return;
      }
      if (!readEndpoints.has(endpoint) && (!synced || (expectedEpoch != null && expectedEpoch !== epoch))) {
        resolve({ok: false, sent: false, code: 'stale_epoch_or_unsynced'}); return;
      }
      if (stopped || state !== 'authorized' || !own || own.readyState !== 1) {
        resolve({ok: false, sent: false, code: 'not_ready:' + state}); return;
      }
      const id = ++seq;
      let frame = endpoint + '\n' + id + '\n' + (query || '');
      if (body !== undefined && body !== null) frame += '\n' + JSON.stringify(body);
      const timer = setTimeout(() => { pending.delete(id); resolve({ok: false, sent: true, code: 'timeout'}); }, timeoutMs || 5000);
      pending.set(id, {resolve, timer, endpoint});
      try { own.send(frame); } catch (_) {
        pending.delete(id); clearTimeout(timer); resolve({ok: false, sent: false, code: 'send_failed'});
      }
    });
  }
  async function sync(attempt) {
    const forEpoch = epoch;
    const current = () => !stopped && forEpoch !== null && epoch === forEpoch;
    const retry = (step, r) => {
      if (!current()) return;
      emit('sync_failed', {step, code: r.code, s: r.s, attempt});
      if (attempt < 5) syncTimer = setTimeout(() => { if (current()) sync(attempt + 1); }, opts.syncRetryMs || 3000);
      else lose('sync_exhausted');
    };
    const accounts = await request('account/list', '', null, opts.syncTimeoutMs || 8000);
    if (!current()) return;
    if (!accounts.ok) { retry('account/list', accounts); return; }
    const snapshot = await request('user/syncrequest', '', null, opts.syncTimeoutMs || 15000);
    if (!current()) return;
    if (!snapshot.ok) { retry('user/syncrequest', snapshot); return; }
    const rows = value => Array.isArray(value) && value.every(row => row && typeof row === 'object' && row.id != null);
    if (!rows(accounts.d) || !snapshot.d || typeof snapshot.d !== 'object' ||
        !['accounts', 'orders', 'fills', 'positions', 'orderVersions'].every(k => rows(snapshot.d[k])) ||
        (snapshot.d.contracts !== undefined && !rows(snapshot.d.contracts))) {
      propsAfterSnapshot = null; retry('snapshot_shape', {code: 'invalid_snapshot'}); return;
    }
    synced = true; reconnectAttempts = 0;
    emit('sync', {accounts: accounts.d, snapshot: snapshot.d, syncEpoch: forEpoch});
    for (const data of propsAfterSnapshot || []) emit('props', data);
    propsAfterSnapshot = null;
  }
  // Controller lost trust in its copy of the account (CDP stall, rejected update). Re-sync on the
  // same connection; writes are refused until the fresh snapshot is delivered.
  function resync(expected) {
    if (stopped || state !== 'authorized' || !own || own.readyState !== 1 || !synced ||
        epoch === null || expected !== epoch) return false;
    synced = false; propsAfterSnapshot = null; clearTimeout(syncTimer);
    sync(0);
    return true;
  }
  function shutdown() {
    if (stopped) return true;
    stopped = true; state = 'stopped'; epoch = null; synced = false;
    clearTimeout(reconnectTimer); clearInterval(leaseTimer); clearConnectionTimers(); settleAll('bridge_detached_after_send');
    const socket = own; own = null;
    if (socket) { socket.onmessage = socket.onclose = socket.onerror = null; try { socket.close(); } catch (_) {} }
    for (const {socket, original, hook} of appHooks) if (socket.send === hook) socket.send = original;
    appHooks.clear();
    for (const {xhr, hook} of xhrHooks) xhr.removeEventListener('load', hook);
    xhrHooks.clear();
    if (window.WebSocket === Wrapped) window.WebSocket = NativeWS;
    if (window.fetch === wrappedFetch) window.fetch = nativeFetch;
    if (XMLHttpRequest.prototype.open === wrappedOpen) XMLHttpRequest.prototype.open = nativeOpen;
    token = null; tradeUrl = null; binding = null;
    try { delete window[CFG.api]; delete window[CFG.binding]; } catch (_) {}
    return true;
  }
  const api = Object.freeze({request, shutdown, resync, touch: () => { if (stopped) return false; controllerSeen = now(); return true; }, status: () => ({
    state, epoch, url: tradeUrl, hasToken: token !== null, lastRxAgoMs: lastRx === null ? null : now() - lastRx,
    pending: pending.size, readOnly: !!CFG.readOnly, connections, reconnectAttempts, tokenGeneration,
    stopped, synced,
  })});
  Object.defineProperty(window, CFG.api, {value: api, enumerable: false, configurable: true, writable: false});
  takeBinding();
  leaseTimer = setInterval(() => {
    if (!stopped && now() - controllerSeen > leaseMs) { emit('controller_lease_expired', {}); shutdown(); }
  }, Math.min(tickMs, leaseMs / 4));
})();
