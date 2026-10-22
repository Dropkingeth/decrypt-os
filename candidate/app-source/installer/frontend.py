#!/usr/bin/python3
"""Loopback static UI and fixed API proxy for the customer-test build."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection
from pathlib import Path
import mimetypes
from urllib.parse import unquote, urlsplit

DIST = Path('/usr/local/lib/decrypt-os/app/ui/dist')
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
HOP = frozenset(('connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length', 'server', 'date'))

class Handler(BaseHTTPRequestHandler):
    server_version = 'DecryptOS'
    def log_message(self, *_):
        pass
    def headers_end(self, length):
        self.send_header('Content-Length', str(length))
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
    def error(self, status, message):
        data = message.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.headers_end(len(data))
        if self.command != 'HEAD': self.wfile.write(data)
    def handle_request(self):
        if self.headers.get_all('Host') not in (['127.0.0.1:8811'], ['localhost:8811']):
            return self.error(403, 'Open Decrypt OS on this device.')
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length') or []) > 1:
            return self.error(400, 'Unsupported request.')
        # BaseHTTPRequestHandler normalizes a leading // before this method.
        # Reject the original request target as well, before serving a SPA fallback.
        request_parts = self.requestline.split()
        if len(request_parts) < 2 or request_parts[1].startswith('//'):
            return self.error(400, 'Unsupported request.')
        target = urlsplit(self.path)
        if target.scheme or target.netloc or not self.path.startswith('/') or self.path.startswith('//'):
            return self.error(400, 'Unsupported request.')
        if target.path == '/api' or target.path.startswith('/api/'):
            return self.proxy()
        if self.command not in ('GET', 'HEAD'):
            return self.error(405, 'This action is not available.')
        decoded = unquote(target.path)
        if any(p in ('.', '..') for p in decoded.split('/')) or '\\' in decoded or '\x00' in decoded:
            return self.error(404, 'Page not found.')
        root = self.server.dist.resolve()
        item = root / decoded.lstrip('/')
        if not item.resolve().is_relative_to(root): return self.error(404, 'Page not found.')
        if not item.is_file() and '.' not in Path(decoded).name:
            item = root / 'index.html'
        if not item.is_file(): return self.error(404, 'Page not found.')
        data = item.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(item.name)[0] or 'application/octet-stream')
        self.headers_end(len(data))
        if self.command != 'HEAD': self.wfile.write(data)
    def proxy(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.error(400, 'Unsupported request.')
        if length < 0 or length > 8192: return self.error(413, 'Request too large.')
        if self.command not in ('GET', 'POST', 'HEAD', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            return self.error(405, 'This action is not available.')
        body = self.rfile.read(length) if length else None
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP}
        headers['Connection'] = 'close'
        connection = HTTPConnection('127.0.0.1', 8810, timeout=150)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            reply = connection.getresponse()
            # Conversation responses are bounded by the API. Never stream an unlimited upstream body.
            data = reply.read(1048577)
            if len(data) > 1048576: return self.error(502, 'The local response was too large.')
            self.send_response(reply.status)
            for key, value in reply.getheaders():
                if key.lower() not in HOP and key.lower() not in ('content-security-policy', 'cache-control', 'referrer-policy', 'x-content-type-options'):
                    self.send_header(key, value)
            self.headers_end(len(data))
            if self.command != 'HEAD': self.wfile.write(data)
        except (OSError, TimeoutError):
            self.error(503, 'The local service is not ready. Try again shortly.')
        finally:
            connection.close()
    do_GET = do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_request

if __name__ == '__main__':
    if not (DIST / 'index.html').is_file():
        raise SystemExit('Build the Decrypt OS interface before starting it.')
    server = ThreadingHTTPServer(('127.0.0.1', 8811), Handler)
    server.dist = DIST
    server.serve_forever()
