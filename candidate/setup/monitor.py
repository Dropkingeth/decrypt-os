#!/usr/bin/env python3
"""Read-only local view of guided Decrypt setup. This program never installs."""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import types
from urllib.parse import parse_qs, urlsplit

PIN = '327a0d8c8bf01f1ad33dfbb7655892644f2f6c794e23aeaf1288cf872d04e38d'
GUIDED = Path(__file__).resolve().parents[1] / 'guided/guided_setup.py'
STATUSES = frozenset(('not_started', 'preparing', 'ready_for_review', 'installing', 'installed',
    'build_failed', 'build_interrupted', 'interrupted', 'installation_unverified', 'unavailable'))
FILES = {'/': ('index.html', 'text/html; charset=utf-8'),
    '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/assets/logo.svg': ('assets/logo.svg', 'image/svg+xml'),
    '/assets/space-grotesk.woff2': ('assets/space-grotesk.woff2', 'font/woff2'),
    '/assets/jetbrains-mono.woff2': ('assets/jetbrains-mono.woff2', 'font/woff2')}


def load_guided(path=GUIDED):
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)) or not path.is_file():
        raise ValueError('The reviewed setup reader is missing.')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != PIN:
        raise ValueError('The setup reader does not match this candidate.')
    module = types.ModuleType('reviewed_guided_setup')
    module.__file__ = str(path)
    exec(compile(data, str(path), 'exec'), module.__dict__)
    return module


def projection(state, preview=False):
    status = state.get('status')
    if status not in STATUSES:
        status = 'unavailable'
    result = {'mode': 'preview' if preview else 'live', 'status': status,
              'source': 'guided-runtime-app', 'checkedAt': datetime.now(timezone.utc).isoformat()}
    if state.get('lastRecordedStatus') in STATUSES:
        result['lastRecordedStatus'] = state['lastRecordedStatus']
    if isinstance(state.get('operationActive'), bool):
        result['operationActive'] = state['operationActive']
    return result


class Monitor(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, work, port=0, preview=False, guided=None, static=None):
        self.work = Path(work)
        self.preview = preview
        self.guided = None if preview else guided or load_guided()
        self.static = Path(static or Path(__file__).parent).resolve()
        super().__init__(('127.0.0.1', port), Handler)

    def snapshot(self, query):
        values = parse_qs(query, strict_parsing=False)
        if self.preview:
            selected = values.get('state', ['not_started'])
            if len(selected) != 1 or selected[0] not in STATUSES:
                raise ValueError('Unknown preview state.')
            return projection({'status': selected[0]}, True)
        if values:
            raise ValueError('Live state cannot be selected by the browser.')
        try:
            state = self.guided.current_state(self.work)
            return projection(state)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return projection({'status': 'unavailable'})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, data, content_type='text/plain; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def do_GET(self):
        expected = '127.0.0.1:' + str(self.server.server_port)
        if self.headers.get('Host') != expected or self.headers.get('Origin', 'http://' + expected) != 'http://' + expected:
            return self.send(403, b'Local access only.')
        target = urlsplit(self.path)
        if target.scheme or target.netloc:
            return self.send(400, b'Invalid request.')
        if target.path == '/api/status':
            try:
                result = self.server.snapshot(target.query)
            except ValueError:
                return self.send(400, b'Invalid state request.')
            return self.send(200, json.dumps(result).encode(), 'application/json')
        item = FILES.get(target.path)
        if not item:
            return self.send(404, b'Not found.')
        file = self.server.static / item[0]
        if file.is_symlink() or not file.is_file() or file.resolve() != file:
            return self.send(404, b'Not found.')
        return self.send(200, file.read_bytes(), item[1])

    do_HEAD = do_GET

    def do_POST(self):
        self.send(405, b'This view is read-only. Use the installation terminal.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=Path.home() / '.local/state/decrypt-setup')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--preview', action='store_true', help='Show clearly labeled sample states; never read installation state.')
    args = parser.parse_args()
    with Monitor(args.work, args.port, args.preview) as server:
        print(f"{'Preview' if args.preview else 'Read-only setup view'}: http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
