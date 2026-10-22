"""Chrome DevTools Protocol client for one loopback page target."""
from __future__ import annotations

import json
import threading
import urllib.request

from . import ws


class CDPError(Exception):
    pass


class NotSent(CDPError):
    """Raised when a DevTools command definitely never left this process."""


def find_page(port, url_pattern):
    """Return the webSocketDebuggerUrl of the first page whose URL contains url_pattern."""
    with urllib.request.urlopen(f'http://127.0.0.1:{int(port)}/json', timeout=5) as r:
        targets = json.loads(r.read())
    pages = [t for t in targets if t.get('type') == 'page' and url_pattern in t.get('url', '')]
    if not pages:
        raise CDPError(f'no page target matching {url_pattern!r}')
    return pages[0]['webSocketDebuggerUrl']


class Session:
    def __init__(self, debugger_url, on_event=None):
        self.conn = ws.connect(debugger_url)
        self.on_event = on_event or (lambda method, params: None)
        self.lock = threading.Lock()
        self.next_id = 0
        self.waiting = {}
        self.closed = threading.Event()
        threading.Thread(target=self._reader, name='cdp-reader', daemon=True).start()

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.conn.recv_text())
                if 'id' in msg:
                    slot = self.waiting.pop(msg['id'], None)
                    if slot:
                        slot[1].append(msg)
                        slot[0].set()
                elif 'method' in msg:
                    try:
                        self.on_event(msg['method'], msg.get('params', {}))
                    except Exception:
                        pass  # An event handler failure must not stop the reader.
        except (ws.Closed, OSError, ValueError):
            pass
        finally:
            self.closed.set()
            for event, box in list(self.waiting.values()):
                box.append({'error': {'message': 'devtools connection closed'}})
                event.set()

    def call(self, method, params=None, timeout=10):
        if self.closed.is_set():
            raise NotSent('devtools connection closed')
        with self.lock:
            self.next_id += 1
            mid = self.next_id
        event, box = threading.Event(), []
        self.waiting[mid] = (event, box)
        try:
            self.conn.send_text(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        except (ws.Closed, OSError) as e:
            self.waiting.pop(mid, None)
            # sendall may transmit some or all of Runtime.evaluate before it
            # raises. The page may already have executed the broker request.
            # Only the pre-dispatch closed check above proves nothing was sent.
            raise CDPError('devtools send outcome uncertain') from e
        if not event.wait(timeout):
            self.waiting.pop(mid, None)
            raise TimeoutError(f'devtools {method} timed out')
        msg = box[0]
        if 'error' in msg:
            raise CDPError(msg['error'].get('message', 'devtools error'))
        return msg.get('result', {})

    def evaluate(self, expression, timeout=10):
        """Evaluate in the page, awaiting promises. Returns the JSON value or raises."""
        res = self.call('Runtime.evaluate', {
            'expression': expression, 'awaitPromise': True, 'returnByValue': True,
            'timeout': int(timeout * 1000)}, timeout=timeout + 2)
        if 'exceptionDetails' in res:
            details = res['exceptionDetails']
            text = (details.get('exception') or {}).get('value') or details.get('text')
            raise CDPError(str(text))
        return res.get('result', {}).get('value')

    def close(self):
        self.conn.close()
