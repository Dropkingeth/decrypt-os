#!/usr/bin/env python3
"""tvbridge-admin: the loopback service behind the Bridge Advanced window (port 8781).

Run:  python3 server.py --profile ~/.local/share/decrypt-os/tvbridge-admin/profile.json

- Binds 127.0.0.1 only. Checks Host on every request; POSTs also need JSON and a same-origin Origin.
- /api/* needs X-Admin-Token. The token is made at start and written (0600) to the runtime dir. It is
  never on a command line: the desktop launcher writes a one-time launch code (0600, `launch-code-<port>`)
  and opens http://127.0.0.1:8781/advanced#c=<code> (own path = own window class); the page trades the
  code for the token once (POST /api/claim, same-origin, within CODE_TTL_S). A used or old code is gone.
- The page and its static files need no token.
"""
import argparse
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import Conflict, Engine  # noqa: E402
from model import Invalid  # noqa: E402
from tree import Profile  # noqa: E402

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')
MAX_BODY = 64 * 1024
CHANGE_RE = re.compile(r'^/api/changes/([A-Za-z0-9-]{1,64})(?:/(confirm|cancel|rollback|resend))?\Z')
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.json': 'application/json', '.css': 'text/css',
                '.js': 'text/javascript', '.svg': 'image/svg+xml', '.png': 'image/png'}


CODE_TTL_S = 60


def runtime_dir():
    base = os.environ.get('XDG_RUNTIME_DIR') or os.path.expanduser('~/.cache')
    d = os.path.join(base, 'tvbridge-admin')
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def write_token(token, port):
    """Write the token (0600) through a random, exclusive temp name: never follows a planted link."""
    d = runtime_dir()
    path = os.path.join(d, f'token-{port}')
    tmp = os.path.join(d, f'.token-{port}.{secrets.token_hex(8)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def claim_code(port, code, now=None):
    """True once for the launcher's one-time code: our own 0600 file, at most CODE_TTL_S old. A matching or
    expired code file is removed; a wrong guess leaves it (the code is 32 random bytes)."""
    if not isinstance(code, str) or not 32 <= len(code) <= 128:
        return False
    path = os.path.join(runtime_dir(), f'launch-code-{port}')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        want = os.read(fd, 256).decode('ascii', 'replace').strip()
    finally:
        os.close(fd)
    now = time.time() if now is None else now
    fresh = st.st_uid == os.getuid() and not st.st_mode & 0o077 and 0 <= now - st.st_mtime <= CODE_TTL_S
    ok = fresh and len(want) >= 32 and hmac.compare_digest(want.encode(), code.encode())
    if ok or not fresh:
        try:
            os.unlink(path)
        except OSError:
            pass
    return ok


def make_handler(engine, token, port):
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    origins = {f'http://{h}' for h in hosts}

    class H(BaseHTTPRequestHandler):
        server_version = 'tvbridge-admin/1'
        sys_version = ''

        def log_message(self, fmt, *args):  # quiet; the audit log is the record
            pass

        def _send(self, code, obj=None, body=None, ctype='application/json', headers=None):
            data = body if body is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            if ctype.startswith('text/html'):
                self.send_header('Content-Security-Policy',
                                 "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' "
                                 "'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, error, detail):
            self._send(code, {'error': error, 'detail': detail})

        def _host_ok(self):
            if self.headers.get('Host') not in hosts:
                self._err(421, 'bad_host', 'This service answers only on 127.0.0.1.')
                return False
            return True

        def _auth_ok(self):
            got = self.headers.get('X-Admin-Token') or ''
            if not hmac.compare_digest(got.encode(), token.encode()):
                self._err(401, 'no_token', 'Open this page from the Bridge Advanced launcher.')
                return False
            return True

        def _body(self):
            if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
                raise Invalid('Send JSON.')
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                raise Invalid('Request too large.')
            raw = self.rfile.read(n) if n else b'{}'
            data = json.loads(raw or b'{}')
            if not isinstance(data, dict):
                raise Invalid('Send a JSON object.')
            return data

        def do_GET(self):
            if not self._host_ok():
                return
            url = urllib.parse.urlsplit(self.path)
            path, q = url.path, urllib.parse.parse_qs(url.query)
            if path in ('/', '/index.html', '/advanced') or path.startswith('/static/') or path.startswith('/mock/'):
                return self._static(path)
            if not path.startswith('/api/'):
                return self._err(404, 'not_found', 'Not found.')
            if not self._auth_ok():
                return
            limit = int((q.get('limit') or ['50'])[0] or 50)
            try:
                if path == '/api/overview':
                    return self._send(200, engine.overview())
                if path == '/api/changes':
                    return self._send(200, {'changes': engine.store.all()[:max(1, min(limit, 500))]})
                if path == '/api/export':   # read-only backup download (no secrets; audit entry only)
                    data, name, _ = engine.export()
                    return self._send(200, body=data, ctype='application/gzip',
                                      headers={'Content-Disposition': f'attachment; filename="{name}"'})
                if path == '/api/audit':
                    return self._send(200, {'entries': engine.audit.read(max(1, min(limit, 1000))),
                                            'chain_ok': engine.audit.verify_chain()})
                m = re.match(r'^/api/logins/([a-z][a-z0-9-]{0,15})/visible$', path)
                if m:
                    return self._send(200, engine.visible(m.group(1)))
                m = CHANGE_RE.match(path)
                if m and not m.group(2):
                    return self._send(200, engine.get(m.group(1)))
            except KeyError:
                return self._err(404, 'not_found', 'Not found.')
            return self._err(404, 'not_found', 'Not found.')

        def do_POST(self):
            if not self._host_ok():
                return
            if self.headers.get('Origin') not in origins:
                return self._err(403, 'bad_origin', 'Requests must come from the Bridge Advanced page.')
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/claim':   # the launcher's one-time code for the token (no token yet)
                try:
                    code = self._body().get('code')
                except (Invalid, ValueError):
                    code = None
                if not claim_code(port, code):
                    return self._err(401, 'no_token', 'Open this page from the Bridge Advanced launcher.')
                return self._send(200, {'token': token})
            if not self._auth_ok():
                return
            try:
                body = self._body()
                if path == '/api/changes':
                    return self._send(200, engine.preview(body.get('type'), body.get('params') or {}))
                m = CHANGE_RE.match(path)
                if m and m.group(2) == 'confirm':
                    return self._send(200, engine.confirm(m.group(1), body.get('preview_hash'),
                                                          body.get('confirm_text')))
                if m and m.group(2) == 'cancel':
                    return self._send(200, engine.cancel(m.group(1)))
                if m and m.group(2) == 'rollback':
                    return self._send(200, engine.rollback_preview(m.group(1)))
                if m and m.group(2) == 'resend':
                    return self._send(200, engine.resend_flatten(m.group(1), body.get('confirm_text')))
                return self._err(404, 'not_found', 'Not found.')
            except Invalid as exc:
                return self._err(400, 'invalid', str(exc))
            except (ValueError, json.JSONDecodeError):
                return self._err(400, 'invalid', 'Bad request.')
            except Conflict as exc:
                return self._err(409, exc.code, exc.detail)
            except KeyError:
                return self._err(404, 'not_found', 'Not found.')
            except PermissionError as exc:
                return self._err(403, 'outside_roots', str(exc))

        def _static(self, path):
            rel = 'index.html' if path in ('/', '/index.html', '/advanced') else path.lstrip('/').split('/', 1)[1] \
                if path.startswith('/static/') else path.lstrip('/')
            full = os.path.realpath(os.path.join(UI_DIR, rel))
            if not full.startswith(os.path.realpath(UI_DIR) + os.sep) or not os.path.isfile(full):
                return self._err(404, 'not_found', 'Not found.')
            if path.startswith('/mock/') and engine.p.mode != 'sandbox':
                return self._err(404, 'not_found', 'Not found.')
            with open(full, 'rb') as f:
                data = f.read()
            self._send(200, body=data, ctype=STATIC_TYPES.get(os.path.splitext(full)[1], 'application/octet-stream'))

    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--profile', required=True)
    ap.add_argument('--token', help='fixed token (tests only); default: random per start')
    args = ap.parse_args(argv)
    profile = Profile.load(args.profile)
    engine = Engine(profile)
    port = int(profile.port)
    token = args.token or secrets.token_urlsafe(32)
    token_path = write_token(token, port)
    engine.run_waiter(float(profile.data.get('wait_poll_s', 5)))
    httpd = ThreadingHTTPServer(('127.0.0.1', port), make_handler(engine, token, port))
    print(f'tvbridge-admin {profile.mode} on http://127.0.0.1:{port}/ (token in {token_path})', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()


if __name__ == '__main__':
    main()
