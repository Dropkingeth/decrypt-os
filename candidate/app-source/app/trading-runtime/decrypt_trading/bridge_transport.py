"""Soup-side transports. Drop-in for Soup's webhook transport (same send() contract).

TradovateBridgeTransport posts the unchanged Soup payload to the local bridge.
TeeTransport keeps a primary transport authoritative and mirrors to a shadow one.
"""
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

OUTCOMES = ('accepted', 'rejected', 'unknown')


class TradovateBridgeTransport:
    def __init__(self, base_url, token, timeout=25):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
            raise ValueError('bridge must be on loopback')
        if not isinstance(token, str) or len(token) < 24:
            raise ValueError('bridge token required')
        self.url = base_url.rstrip('/') + '/order/' + token
        self.timeout = timeout

    def send(self, payload):
        body = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        req = urllib.request.Request(self.url, data=body, method='POST', headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                status, raw = r.status, r.read(65537)
        except urllib.error.HTTPError as e:
            return ('rejected' if 400 <= e.code < 500 else 'unknown'), {'bridge_http_status': e.code}
        except urllib.error.URLError as e:
            # Connection refused means the request never reached the bridge.
            if isinstance(e.reason, ConnectionRefusedError):
                return 'rejected', {'bridge': 'unreachable'}
            return 'unknown', {'transport': 'uncertain'}
        except (TimeoutError, OSError):
            return 'unknown', {'transport': 'uncertain'}
        try:
            data = json.loads(raw)
            outcome, receipt = data['outcome'], data['receipt']
        except (ValueError, KeyError, TypeError):
            return 'unknown', {'transport': 'invalid_bridge_response', 'http_status': status}
        if outcome not in OUTCOMES or not isinstance(receipt, dict):
            return 'unknown', {'transport': 'invalid_bridge_response'}
        return outcome, receipt


class TeeTransport:
    """Primary decides the outcome Soup sees. The shadow send runs on its own ordered
    worker thread, so it never delays or breaks the primary route. Shadow results are
    appended to `shadow_log` (JSON lines) for later comparison."""

    def __init__(self, primary, shadow, shadow_log):
        self.primary, self.shadow, self.shadow_log = primary, shadow, shadow_log
        self.queue = queue.Queue(maxsize=1000)
        threading.Thread(target=self._shadow_worker, name='shadow-transport', daemon=True).start()

    def send(self, payload):
        outcome, receipt = self.primary.send(payload)
        try:
            self.queue.put_nowait((payload, outcome))
        except queue.Full:
            pass  # shadow falls behind; comparison shows the gap
        return outcome, receipt

    def _shadow_worker(self):
        while True:
            payload, primary_outcome = self.queue.get()
            started = time.time()
            try:
                outcome, receipt = self.shadow.send(payload)
            except Exception as e:  # noqa: BLE001 - shadow must never break the primary route
                outcome, receipt = 'error', {'error': type(e).__name__}
            record = {'at': started, 'latency_s': round(time.time() - started, 3), 'payload': payload,
                      'primary': primary_outcome, 'shadow': outcome, 'shadow_receipt': receipt}
            with open(self.shadow_log, 'a') as f:
                f.write(json.dumps(record, sort_keys=True) + '\n')
