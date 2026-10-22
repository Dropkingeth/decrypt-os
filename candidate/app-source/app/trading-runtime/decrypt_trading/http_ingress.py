"""Dedicated loopback receiver behind a customer-owned HTTPS reverse proxy.

No trading controls, browser calls, CORS or application-session endpoint exists
here. Access/error logs deliberately never contain the secret request target.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
from urllib.parse import urlsplit

from .ingress import SignalIngress
from .runtime import RuntimeError


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True
    allow_reuse_address = False
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()
    def handle_error(self, request, client_address):
        pass


class IngressListener:
    def __init__(self, runtimes, lease, public_origin, *, host='127.0.0.1', port=8812):
        origin = urlsplit(public_origin) if type(public_origin) is str else None
        if (origin is None or origin.scheme != 'https' or not origin.hostname or origin.path not in ('', '/')
                or origin.query or origin.fragment or origin.username or origin.password
                or re.fullmatch(r'[A-Za-z0-9.-]+', origin.hostname) is None
                or origin.hostname.lower() in ('localhost', '127.0.0.1') or origin.port not in (None, 443)
                or host != '127.0.0.1' or type(port) is not int or not 1024 <= port <= 65535):
            raise RuntimeError('customer HTTPS origin and loopback listener required')
        self.public_origin = 'https://' + origin.netloc.lower()
        self.authority = origin.netloc.lower()
        self.runtimes, self.lease, self.host, self.port = runtimes, lease, host, port
        self.server, self.thread = None, None

    def start(self):
        if self.server is not None:
            raise RuntimeError('ingress listener already started')
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            server_version = 'DecryptInput'
            sys_version = ''
            def setup(self):
                super().setup()
                self.connection.settimeout(3)
            def log_message(self, *args):
                pass
            def send_error(self, code, message=None, explain=None):
                self.reply(code, {'error': 'request_rejected'})
            def reply(self, code, value):
                body = json.dumps(value, separators=(',', ':')).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
            def do_POST(self):
                headers = self.headers
                single = ('Host', 'X-Forwarded-Host', 'X-Forwarded-Proto', 'Content-Type', 'Content-Length')
                if (any(len(headers.get_all(k, [])) != 1 for k in single)
                        or headers['Host'].lower() != owner.authority
                        or headers['X-Forwarded-Host'].lower() != owner.authority
                        or headers['X-Forwarded-Proto'] != 'https'
                        or headers['Content-Type'].lower() != 'application/json'
                        or any(headers.get_all(k) for k in ('Transfer-Encoding', 'Expect', 'Origin', 'Referer'))):
                    return self.reply(400, {'error': 'request_rejected'})
                path = re.fullmatch(r'/signal/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})/([A-Za-z0-9_-]{43,128})', self.path)
                length = headers['Content-Length']
                if path is None or re.fullmatch(r'[0-9]{1,5}', length) is None or not 0 < int(length) <= 40960:
                    return self.reply(400, {'error': 'request_rejected'})
                try:
                    raw = self.rfile.read(int(length))
                    if len(raw) != int(length):
                        raise RuntimeError('incomplete request')
                    runtime = owner.runtimes(path[1])
                    if runtime is None or runtime.binding['instanceId'] != path[1]:
                        raise RuntimeError('runtime unavailable')
                    result = SignalIngress(runtime, owner.lease).accept(raw, presented_secret=path[2])
                    return self.reply(202, result)
                except Exception:
                    # No exception message, path, headers or body is exposed.
                    return self.reply(403, {'error': 'input_not_accepted'})
            def do_GET(self):
                return self.reply(405, {'error': 'method_not_allowed'})
            do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET
        self.server = _Server((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name='customer-signal-ingress', daemon=True)
        self.thread.start()
        return {'state': 'listening_loopback', 'port': self.port, 'publicTlsVerified': False}

    def close(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = self.thread = None
