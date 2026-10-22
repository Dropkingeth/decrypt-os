"""Attach to the trader page; keep credentials in-page and detach without trading."""
from __future__ import annotations

import json
import logging
import secrets
import time
import threading
from pathlib import Path

from . import cdp

log = logging.getLogger('tvbridge')
SCRIPT = Path(__file__).resolve().parent.parent / 'page' / 'bridge.js'
READ_ENDPOINTS = frozenset(('account/list', 'user/syncrequest', 'contract/find', 'contract/item'))
RESYNC_MIN_S, RESYNC_MAX_S = 2.0, 60.0
SLOW_REQUEST_S = 1.0  # 1.1.2: a broker request round trip above this is logged (observability only)


def _note_slow_request(endpoint, started):
    """1.1.2: warn when a request round trip was slow: endpoint and ms only, never a body or token.
    Never raises, so it cannot change a request's result."""
    try:
        elapsed = time.monotonic() - started
        if elapsed > SLOW_REQUEST_S:
            log.warning('slow broker request %s: %d ms', endpoint, int(elapsed * 1000))
    except Exception:  # noqa: BLE001
        pass


class LeaseLost(RuntimeError):
    """The page controller lease is gone; only a fresh attach (daemon restart) recovers."""


class Bridge:
    def __init__(self, state, *, devtools_port, page_url_contains, trade_url_pattern,
                 read_only=False, lifecycle=None, debugger_url=None, expected_origin=None):
        self.state = state
        self.port = devtools_port
        self.page_url_contains = page_url_contains
        self.trade_url_pattern = trade_url_pattern
        self.read_only = bool(read_only)
        self.lifecycle = lifecycle or {}
        self.debugger_url = debugger_url
        self.expected_origin = expected_origin
        self.session = None
        self.events = []
        self.api = '__dlb_' + secrets.token_hex(8)
        self.binding = '__dlbe_' + secrets.token_hex(8)
        self.script_id = None
        self.epoch = None
        self._detached = None
        # 1.1.0: (time.monotonic(), status) of the latest status() answer, for GET /login (no page call).
        self.last_status = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._resync_after = 0.0
        self._resync_backoff = RESYNC_MIN_S
        self.resyncs = 0
        # Set once, never cleared: this attach can no longer renew the page lease, so every
        # later page session would die ~30 s after it starts. The daemon exits
        # non-zero on it and systemd re-attaches (tvbridge.daemon.Shutdown).
        self.lease_lost = threading.Event()
        self.lease_lost_reason = None
        # Set by the daemon while it stops: new order commands are refused (Executor.send).
        self.stop_reason = None
        # read-only page extensions installed beside the order script (tvbridge.marketfeed.PageFeed).
        # Each has install(session, evaluate_now), on_binding(params) -> bool, lost() and uninstall(session).
        self.extensions = []

    def _lose_lease(self, reason):
        if self._detached is not None:
            return  # our own teardown, not a loss
        self.epoch = None
        self.state.mark_unsynced()
        if self.lease_lost.is_set():
            return
        self.lease_lost_reason = reason
        self.lease_lost.set()
        log.error('page controller lease lost (%s): orders refused; the daemon must re-attach', reason)

    def _on_event(self, method, params):
        if method in ('Runtime.executionContextsCleared', 'Page.frameNavigated'):
            if method == 'Runtime.executionContextsCleared' or not params.get('frame', {}).get('parentId'):
                self.epoch = None
                self.state.mark_unsynced()
                for ext in self.extensions:
                    ext.lost()
            return
        if method == 'Runtime.bindingCalled' and params.get('name') != self.binding and self._detached is None:
            for ext in self.extensions:
                if ext.on_binding(params):
                    return
            return
        if method != 'Runtime.bindingCalled' or params.get('name') != self.binding or self._detached is not None:
            return
        try:
            msg = json.loads(params['payload'])
            kind, data = msg.get('type'), msg.get('data') or {}
            if kind == 'authorized':
                self.epoch = msg.get('epoch')
                self.state.mark_unsynced()
            elif kind == 'props':
                if self.epoch and msg.get('epoch') == self.epoch and self.state.is_synced_for(self.epoch):
                    self.state.apply_props(data)
                return
            elif kind == 'sync':
                if not self.epoch or msg.get('epoch') != self.epoch or data.get('syncEpoch') != self.epoch:
                    return
                accounts, snapshot = data.get('accounts'), data.get('snapshot')
                if not isinstance(accounts, list) or not isinstance(snapshot, dict):
                    raise ValueError('invalid sync shape')
                for name in ('accounts', 'positions', 'orders', 'fills', 'orderVersions', 'contracts'):
                    if name in snapshot and (not isinstance(snapshot[name], list) or
                            any(not isinstance(row, dict) or 'id' not in row for row in snapshot[name])):
                        raise ValueError('invalid sync collection')
                if any(not isinstance(row, dict) or 'id' not in row for row in accounts):
                    raise ValueError('invalid accounts')
                self.state.load_sync(accounts, snapshot, epoch=self.epoch)
                log.info('account sync loaded')
                return
            elif kind in ('closed', 'auth_failed', 'connecting', 'controller_lease_expired'):
                self.epoch = None
                self.state.mark_unsynced()
            elif kind == 'sync_failed':
                if msg.get('epoch') != self.epoch:
                    return
                self.state.mark_unsynced()
            # Page emits only structured lifecycle data here, never raw frames/tokens.
            self.events.append(msg)
            del self.events[:-50]
            reason = data.get('reason') if isinstance(data, dict) else None
            if isinstance(reason, (int, str)) and not isinstance(reason, bool) and len(str(reason)) <= 40:
                log.info('page event %s (reason %s)', kind, reason)  # websocket close code
            else:
                log.info('page event %s', kind)
            if kind == 'controller_lease_expired':
                # The page stopped its hooks. Only the top frame runs the script, so this is our
                # session ending, whether or not the heartbeat thread is still running.
                self._lose_lease(kind)
        except (TypeError, ValueError, KeyError, AttributeError):
            self.epoch = None
            self.state.mark_unsynced()
            self.events.append({'type': 'event_parse_failed', 'data': {}, 'epoch': None})
            del self.events[:-50]

    def attach(self, reload=True):
        """Install once. Without reload only future app auth/renewal can supply tokens."""
        if self.session is not None or self._detached is not None:
            raise RuntimeError('Bridge instances attach only once')
        url = self.debugger_url or cdp.find_page(self.port, self.page_url_contains)
        self.session = cdp.Session(url, on_event=self._on_event)
        config = json.dumps({'tradeUrlPattern': self.trade_url_pattern, 'api': self.api,
                             'binding': self.binding, 'readOnly': self.read_only, 'lifecycle': self.lifecycle})
        source = SCRIPT.read_text().replace('__DLB_CONFIG__', config)
        try:
            if self.expected_origin is not None and self.session.evaluate('location.origin') != self.expected_origin:
                raise RuntimeError('page origin changed before attach')
            self.session.call('Runtime.enable')
            self.session.call('Page.enable')
            self.session.call('Runtime.addBinding', {'name': self.binding})
            result = self.session.call('Page.addScriptToEvaluateOnNewDocument', {'source': source})
            self.script_id = result['identifier']
            self.state.mark_unsynced()
            if not reload:
                self.session.evaluate(source)
            for ext in self.extensions:
                ext.install(self.session, evaluate_now=not reload)   # after the order script, never before
            if reload:
                self.session.call('Page.reload', {'ignoreCache': False})
            self._heartbeat_thread = threading.Thread(target=self._controller_heartbeat,
                                                      name='bridge-controller-lease', daemon=True)
            self._heartbeat_thread.start()
        except Exception:
            self.detach()
            raise

    def _controller_heartbeat(self):
        # No broker request: renew only the local page controller lease, including
        # while a human is paused at a Terminal confirmation prompt.
        lease_s = float(self.lifecycle.get('leaseMs', 30000)) / 1000.0
        interval = min(5.0, lease_s / 3.0)
        api = json.dumps(self.api)
        expression = 'window[%s] ? window[%s].touch() : null' % (api, api)
        last_ok = time.monotonic()
        while not self._heartbeat_stop.wait(interval):
            try:
                alive = self.session.evaluate(expression, timeout=2)
                last_ok = time.monotonic()
            except Exception:
                # A busy host can stall one CDP call. State is untrusted meanwhile, but only a
                # stall of half the lease ends the session.
                self.state.mark_unsynced()
                if time.monotonic() - last_ok > lease_s / 2:
                    # Without the controller the page lease expires itself. Staying alive here
                    # would leave the daemon running but blind: report the loss.
                    self._lose_lease('controller_heartbeat_stalled')
                    break
                continue
            if alive is False:
                # touch() is false only after the page instance stopped itself (lease expired);
                # null means the script is not there yet (reload in progress).
                self._lose_lease('page_controller_stopped')
                break
            self._resync_if_needed()

    def _resync_if_needed(self):
        """Ask the page for a fresh snapshot when its connection is up but local state is untrusted.

        Covers a transient CDP error or a rejected update. Without this the bridge would stay
        unsynced, refusing every order, until Tradovate happened to drop the socket.
        """
        if self._heartbeat_stop.is_set() or self._detached is not None:
            return
        st = self.status() or {}
        epoch = st.get('epoch')
        if st.get('state') != 'authorized' or not epoch or st.get('synced') is not True:
            return  # Page is connecting or already syncing on its own.
        if self.state.is_synced_for(epoch):
            self._resync_backoff = RESYNC_MIN_S
            return
        now = time.monotonic()
        if now < self._resync_after:
            return
        self._resync_after = now + self._resync_backoff
        self._resync_backoff = min(self._resync_backoff * 2, RESYNC_MAX_S)
        self.epoch = epoch
        api = json.dumps(self.api)
        try:
            ok = self.session.evaluate('window[%s] ? window[%s].resync(%s) : false' % (api, api, json.dumps(epoch)),
                                       timeout=2)
        except Exception:
            return
        if ok:
            self.resyncs += 1
            log.warning('account state untrusted on a live connection; requested a fresh snapshot')

    def detach(self):
        """Remove only our injection, hooks/socket and CDP connection; no broker action.

        False means cleanup could not be verified (for example CDP was lost).
        Repeated calls return the original cleanup result.
        """
        if self._detached is not None:
            return self._detached
        self._detached = False  # Reject late binding callbacks during teardown.
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None and threading.current_thread() is not self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)
        self.epoch = None
        self.state.mark_unsynced()
        if self.session is None:
            self._detached = True
            return True
        ok = True
        for ext in self.extensions:
            try:
                ok = ext.uninstall(self.session) and ok
            except Exception:  # noqa: BLE001
                ok = False
        try:
            if self.script_id:
                self.session.call('Page.removeScriptToEvaluateOnNewDocument', {'identifier': self.script_id})
                self.script_id = None
        except Exception:
            ok = False
        try:
            api = json.dumps(self.api)
            result = self.session.evaluate('window[%s] ? window[%s].shutdown() : true' % (api, api), timeout=3)
            ok = result is True and ok
        except Exception:
            ok = False
        try:
            self.session.call('Runtime.removeBinding', {'name': self.binding}, timeout=3)
        except Exception:
            ok = False
        try:
            self.session.close()
        except Exception:
            ok = False
        self._detached = ok
        return ok

    def status(self):
        st = self._status()
        self.last_status = (time.monotonic(), st)
        return st

    def _status(self):
        if self._detached is not None:
            return {'state': 'detached', 'epoch': None, 'cleanupVerified': self._detached,
                    'readOnly': self.read_only}
        if self.lease_lost.is_set():
            # Whatever the page says now, this attach cannot keep it: never report it usable.
            return {'state': 'controller_lease_lost', 'epoch': None, 'reason': self.lease_lost_reason,
                    'readOnly': self.read_only}
        try:
            return self.session.evaluate('window[%s] ? window[%s].status() : null' % (
                json.dumps(self.api), json.dumps(self.api)), timeout=3)
        except Exception:
            self.epoch = None
            self.state.mark_unsynced()
            return {'state': 'devtools_error', 'epoch': None}

    def wait_ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.lease_lost.is_set():
                raise LeaseLost(self.lease_lost_reason)
            st = self.status() or {}
            if st.get('state') == 'authorized' and self.state.is_synced_for(st.get('epoch')):
                return st
            time.sleep(0.25)
        raise TimeoutError(f'bridge not ready: {self.status()}')

    def request(self, endpoint, body=None, query='', timeout=5.0, expected_epoch=None):
        """Return transport evidence, never replay an uncertain request."""
        started = time.monotonic()
        try:
            return self._request(endpoint, body, query, timeout, expected_epoch)
        finally:
            _note_slow_request(endpoint, started)

    def _request(self, endpoint, body, query, timeout, expected_epoch):
        if self.read_only and endpoint not in READ_ENDPOINTS:
            return {'ok': False, 'sent': False, 'code': 'read_only_endpoint'}
        if self._detached is not None or self.session is None:
            return {'ok': False, 'sent': False, 'code': 'bridge_not_attached'}
        if self.lease_lost.is_set():
            return {'ok': False, 'sent': False, 'code': 'controller_lease_lost'}
        api = json.dumps(self.api)
        expr = ("window[%s] ? window[%s].request(%s, %s, %s, %d, %s) "
                ": {ok: false, sent: false, code: 'page_script_missing'}") % (
            api, api, json.dumps(endpoint), json.dumps(query), json.dumps(body), int(timeout * 1000), json.dumps(expected_epoch))
        try:
            return self.session.evaluate(expr, timeout=timeout + 2)
        except TimeoutError:
            return {'ok': False, 'sent': True, 'code': 'devtools_timeout'}
        except cdp.NotSent:
            return {'ok': False, 'sent': False, 'code': 'devtools_not_sent'}
        except cdp.CDPError:
            return {'ok': False, 'sent': True, 'code': 'devtools_error'}
