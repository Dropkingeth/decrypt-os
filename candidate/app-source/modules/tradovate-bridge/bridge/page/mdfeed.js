// the 1-second feed's page side (tvbridge.marketfeed). Read only: it subscribes to the broker's
// quotes for ONE dated contract (the binding's own) and reports the contract id, quote time and last
// trade price. It never places, changes or cancels an order. Tokens remain inside this closure.
(() => {
  const CFG = __DLB_MD_CONFIG__;
  try { if (window.top !== window) return; } catch (_) { return; }
  if (window[CFG.api]) return;
  const PrevWS = window.WebSocket;     // the order bridge's wrapper (bridge.js), or the browser's own
  const prevFetch = window.fetch;
  const prevOpen = XMLHttpRequest.prototype.open;
  const mdPattern = new RegExp(CFG.mdUrlPattern);
  const tradePattern = new RegExp(CFG.tradeUrlPattern);
  const idleMs = CFG.idleMs || 5000;
  const connectMs = CFG.connectMs || 15000;
  const heartbeatMs = CFG.heartbeatMs || 2500;
  const retryMs = CFG.retryMs || 2000;
  const maxRetryMs = CFG.maxRetryMs || 30000;
  const tickMs = CFG.tickMs || 500;
  let token = null, mdUrl = null, own = null, state = 'waiting_for_app_md', stopped = false, binding = null;
  let lastRx = null, connectedAt = 0, attempts = 0, heartbeat = null, watchdog = null, reconnectTimer = null;
  let leaseTimer = null;
  const appHooks = new Set(), xhrHooks = new Set();
  const now = () => performance.now();
  const takeBinding = () => {
    if (!binding && typeof window[CFG.binding] === 'function') {
      binding = window[CFG.binding];
      try { delete window[CFG.binding]; } catch (_) {}
    }
    return binding;
  };
  const emit = (type, data) => {
    if (stopped) return;
    try { const fn = takeBinding(); if (fn) fn(JSON.stringify({type, data, at: Date.now()})); } catch (_) {}
  };
  function setState(next, reason) {
    state = next;
    emit('md_state', reason === undefined ? {state: next} : {state: next, reason});
  }
  function validMdUrl(value) {
    try {
      const u = new URL(value, location.href);
      if (u.search) {
        const params = [...u.searchParams];
        if (params.length !== 1 || params[0][0] !== 'r' || !/^[01](\.\d{1,20})?$/.test(params[0][1])) return null;
        u.search = '';
      }
      const m = u.href.match(mdPattern);
      return !u.username && !u.password && !u.search && !u.hash &&
        ['ws:', 'wss:'].includes(u.protocol) && m && m[0] === u.href ? u.href : null;
    } catch (_) { return null; }
  }
  // Token responses are read only from the trading host's own auth endpoints (bridge.js's rule).
  function approvedTokenUrl(value) {
    try {
      const u = new URL(value, location.href);
      if (u.username || u.password || u.hash || u.search || u.protocol !== 'https:') return false;
      const socket = 'wss://' + u.host + '/v1/websocket';
      const m = socket.match(tradePattern);
      return !!m && m[0] === socket &&
        ['/v1/auth/accesstokenrequest', '/v1/auth/renewaccesstoken'].includes(u.pathname.toLowerCase());
    } catch (_) { return false; }
  }
  function learn(value, url) {
    if (stopped || typeof value !== 'string' || value.length < 16) return;
    const changed = value !== token;
    token = value;
    if (url) mdUrl = url;
    // A connection stays authorized after a token rotation; a reconnect uses the newest token.
    if (!own && mdUrl && (state !== 'auth_failed' || changed)) connect();
  }
  function Wrapped(url, protocols) {
    const socket = protocols === undefined ? new PrevWS(url) : new PrevWS(url, protocols);
    const target = validMdUrl(String(url));
    if (target && !stopped) {
      const original = socket.send;
      const hook = function(data) {
        if (!stopped && typeof data === 'string' && data.startsWith('authorize\n'))
          learn(data.split('\n').slice(3).join('\n'), target);
        return original.call(this, data);
      };
      socket.send = hook;
      const entry = {socket, original, hook};
      appHooks.add(entry);
      socket.addEventListener('close', () => appHooks.delete(entry), {once: true});
    }
    return socket;
  }
  Wrapped.prototype = PrevWS.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) Object.defineProperty(Wrapped, k, {value: PrevWS[k]});
  window.WebSocket = Wrapped;
  function tokenFromJson(text, url) {
    if (!approvedTokenUrl(url)) return;
    try { const body = JSON.parse(text); learn(body && body.mdAccessToken, null); } catch (_) {}
  }
  const wrappedFetch = function(input, init) {
    const url = String(input && input.url ? input.url : input);
    const result = prevFetch.apply(this, arguments);
    if (!stopped && approvedTokenUrl(url)) result.then(r => {
      if (r.ok && approvedTokenUrl(r.url)) r.clone().text().then(text => tokenFromJson(text, r.url)).catch(() => {});
    }).catch(() => {});
    return result;
  };
  if (prevFetch) window.fetch = wrappedFetch;
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
    return prevOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.open = wrappedOpen;
  function clearTimers() {
    clearInterval(heartbeat); clearInterval(watchdog);
    heartbeat = watchdog = null;
  }
  function lose(code, authFailed = false) {
    if (stopped) return;
    const old = own; own = null;
    clearTimers();
    setState(authFailed ? 'auth_failed' : 'closed', code);
    if (old) { old.onmessage = old.onclose = old.onerror = null; try { old.close(); } catch (_) {} }
    clearTimeout(reconnectTimer);
    if (!authFailed) {
      const delay = Math.min(maxRetryMs, retryMs * Math.pow(2, Math.min(attempts++, 8)));
      reconnectTimer = setTimeout(() => { if (!stopped && token && mdUrl && !own) connect(); }, delay);
    }
  }
  function connect() {
    if (stopped || own || !token || !mdUrl) return;
    clearTimeout(reconnectTimer); clearTimers();
    lastRx = null; connectedAt = now();
    setState('connecting');
    let socket;
    try { socket = new PrevWS(mdUrl); } catch (_) { lose('socket_creation_failed'); return; }
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
        catch (_) { lose('invalid_frame'); return; }
        for (const item of items) { if (socket !== own) break; handle(item); }
      } else if (kind === 'c') lose('server_close');
    };
    socket.onclose = () => { if (socket === own) lose('socket_closed'); };
    socket.onerror = () => { if (socket === own) lose('socket_error'); };
    heartbeat = setInterval(() => {
      if (socket === own && socket.readyState === 1) try { socket.send('[]'); } catch (_) { lose('heartbeat_send_failed'); }
    }, heartbeatMs);
    watchdog = setInterval(() => {
      if (socket !== own) return;
      if (state !== 'subscribed' && now() - connectedAt > connectMs) lose('subscribe_timeout');
      else if (state === 'subscribed' && now() - (lastRx === null ? connectedAt : lastRx) > idleMs) lose('receive_timeout');
    }, tickMs);
  }
  function handle(item) {
    if (!item || typeof item !== 'object') return;
    if (item.i === 0 && state === 'connecting') {
      if (item.s === 200) {
        setState('authorized');
        // The binding's own dated contract only; nothing else is ever subscribed.
        try { own.send('md/subscribeQuote\n1\n\n' + JSON.stringify({symbol: CFG.symbol})); }
        catch (_) { lose('subscribe_send_failed'); }
      } else lose('authorization_refused', true);
      return;
    }
    if (item.i === 1 && state === 'authorized') {
      if (item.s === 200) { attempts = 0; setState('subscribed'); }
      else lose('subscribe_refused');
      return;
    }
    if (state === 'subscribed' && item.e === 'md' && item.d && Array.isArray(item.d.quotes)) {
      const quotes = [];
      for (const q of item.d.quotes.slice(0, 50)) {
        if (!q || typeof q !== 'object') continue;
        const trade = q.entries && q.entries.Trade;
        quotes.push({contractId: q.contractId, timestamp: typeof q.timestamp === 'string' ? q.timestamp.slice(0, 40) : null,
                     price: trade && typeof trade.price === 'number' ? trade.price : null});
      }
      if (quotes.length) emit('md_quotes', {quotes});
    }
    // Any other event is not forwarded.
  }
  function shutdown() {
    if (stopped) return true;
    stopped = true; state = 'stopped';
    clearTimeout(reconnectTimer); clearInterval(leaseTimer); clearTimers();
    const socket = own; own = null;
    if (socket) { socket.onmessage = socket.onclose = socket.onerror = null; try { socket.close(); } catch (_) {} }
    for (const {socket, original, hook} of appHooks) if (socket.send === hook) socket.send = original;
    appHooks.clear();
    for (const {xhr, hook} of xhrHooks) xhr.removeEventListener('load', hook);
    xhrHooks.clear();
    if (window.WebSocket === Wrapped) window.WebSocket = PrevWS;
    if (window.fetch === wrappedFetch) window.fetch = prevFetch;
    if (XMLHttpRequest.prototype.open === wrappedOpen) XMLHttpRequest.prototype.open = prevOpen;
    token = null; mdUrl = null; binding = null;
    try { delete window[CFG.api]; delete window[CFG.binding]; } catch (_) {}
    return true;
  }
  const api = Object.freeze({shutdown, status: () => ({
    state, hasToken: token !== null, lastRxAgoMs: lastRx === null ? null : now() - lastRx, attempts, stopped,
  })});
  Object.defineProperty(window, CFG.api, {value: api, enumerable: false, configurable: true, writable: false});
  takeBinding();
  // Lives exactly as long as the order bridge's page script: when that one stops, this one stops.
  leaseTimer = setInterval(() => { if (!stopped && !window[CFG.bridgeApi]) shutdown(); }, tickMs);
})();
