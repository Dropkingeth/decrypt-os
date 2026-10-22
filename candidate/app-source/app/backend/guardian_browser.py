"""Bounded CDP adapter for the explicitly launched customer Guardian browser.

No discovery of other profiles/ports, no broker/order endpoints, no screenshots,
cookie extraction or arbitrary navigation API. CDP payloads are never logged.
Linux/systemd ownership checks fail closed on other hosts.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import time
from urllib.parse import urlsplit

from guardian_auth import GuardianError, provider_id

PORT = 9566
UNIT = 'decrypt-os-guardian-browser.service'
MAX_MESSAGE = 4 * 1024 * 1024
PROVIDERS = {
    'tradingview': {'origin': 'https://www.tradingview.com', 'login': 'https://www.tradingview.com/accounts/signin/'},
}


def origin(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            return None
        return 'https://' + (p.hostname or '')
    except (ValueError, TypeError):
        return None


# Fixed login-only DOM program. It never returns input values or page text.
# All selectors must resolve uniquely. MFA/CAPTCHA/errors stop, never bypass.
LOGIN_PROGRAM = r"""(function(provider, credentials) {
  'use strict';
  const origins = {tradingview:'https://www.tradingview.com'};
  if (location.origin !== origins[provider] || window.top !== window.self) return {state:'untrusted_page'};
  const visible = e => !!(e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  const text = (document.body?.innerText || '').slice(0,100000).toLowerCase();
  if (/captcha|verify you are human|checking your browser/.test(text) || [...document.querySelectorAll('iframe')].some(e => /captcha|challenge/i.test(e.src))) return {state:'captcha'};
  if (/incorrect password|invalid password|invalid credentials|invalid email or password|authentication failed|login failed/.test(text)) return {state:'bad_password'};
  if (/two.factor|verification code|authenticator|one.time (code|password)|security code/.test(text) || [...document.querySelectorAll('input[autocomplete="one-time-code"]')].some(visible)) return {state:'mfa'};
  const passwords = [...document.querySelectorAll('input[type="password"]')].filter(visible);
  if (!passwords.length) return {state:'authenticated_unverified'};
  const selector = 'input[name="username"],input[name="id_username"]';
  const users = [...new Set(document.querySelectorAll(selector))].filter(visible);
  if (passwords.length !== 1 || users.length !== 1) return {state:'ambiguous_form'};
  const p = passwords[0], u = users[0], f = p.form;
  if (!f || u.form !== f || p.disabled || u.disabled || p.readOnly || u.readOnly) return {state:'ambiguous_form'};
  const action = f.getAttribute('action');
  if ((f.method || '').toLowerCase() !== 'post') return {state:'untrusted_form'};
  if (action) {
    let dest; try { dest = new URL(f.action); } catch { return {state:'untrusted_form'}; }
    if (dest.origin !== origins[provider] || !/^\/(login|welcome|auth\/login|auth\/accesstokenrequest|accounts\/signin)\/?$/.test(dest.pathname) || (f.method || '').toLowerCase() !== 'post') return {state:'untrusted_form'};
  }
  const buttons = [...f.querySelectorAll('button,input[type="submit"]')].filter(e => visible(e) && !e.disabled && (/^(log\s*in|sign\s*in)$/i.test((e.innerText || e.value || '').trim())));
  if (buttons.length !== 1) return {state:'ambiguous_form'};
  if (buttons[0].hasAttribute('formaction') || buttons[0].hasAttribute('formmethod') || (f.target && f.target !== '_self')) return {state:'untrusted_form'};
  if (!credentials) return {state:'logged_out'};
  // Recheck before touching either input. Do not resolve credentials on another origin.
  if (location.origin !== origins[provider] || !f.isConnected || !p.isConnected || !u.isConnected) return {state:'untrusted_page'};
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
  try {
    setter.call(u,credentials.username); u.dispatchEvent(new Event('input',{bubbles:true})); u.dispatchEvent(new Event('change',{bubbles:true}));
    setter.call(p,credentials.password); p.dispatchEvent(new Event('input',{bubbles:true})); p.dispatchEvent(new Event('change',{bubbles:true}));
    buttons[0].click();
    return {state:'login_submitted'};
  } finally {
    // Framework state may retain values until navigation; clear the visible fields
    // without generating a second event or a second submission.
    if (p.isConnected) setter.call(p,'');
    if (u.isConnected) setter.call(u,'');
    credentials.username = ''; credentials.password = '';
  }
})"""

class OwnedBrowser:
    port = PORT
    unit = UNIT
    profile_component = 'guardian'

    def __init__(self, state_dir):
        self.profile = str(Path(state_dir).absolute() / self.profile_component / 'browser-profile')
        self.auth_targets = {}
        self.account_targets = {}
        self.process_identity = None

    def _systemctl(self, *args):
        try:
            result = subprocess.run(['/usr/bin/systemctl', '--user', *args], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=8, check=True)
            return result.stdout.decode('ascii').strip()
        except Exception:
            raise GuardianError('browser_unavailable', status=409) from None

    def _owned(self):
        try:
            value = self._systemctl('show', self.unit, '--property=MainPID', '--value')
            if not value.isdigit() or int(value) < 2:
                raise ValueError()
            pid = int(value)
            process = Path('/proc') / str(pid)
            if process.stat().st_uid != os.getuid():
                raise ValueError()
            executable = Path(os.readlink(process / 'exe'))
            if str(executable) not in ('/usr/lib/chromium/chromium', '/usr/lib/chromium-browser/chromium-browser', '/opt/google/chrome/chrome', '/usr/bin/chromium'):
                raise ValueError()
            if executable.stat().st_uid != 0 or executable.stat().st_mode & 0o022:
                raise ValueError()
            argv = (process / 'cmdline').read_bytes().split(b'\0')
            args = [a.decode() for a in argv if a]
            for required in ('--remote-debugging-port=' + str(self.port), '--remote-debugging-address=127.0.0.1',
                             '--user-data-dir=' + self.profile):
                if args.count(required) != 1:
                    raise ValueError()
            if any(a.split('=', 1)[0] == '--user-data-dir' and a != '--user-data-dir=' + self.profile for a in args):
                raise ValueError()
            if any(a.split('=', 1)[0] in {'--no-sandbox', '--disable-web-security', '--ignore-certificate-errors',
                                        '--remote-allow-origins', '--load-extension', '--proxy-server'} for a in args):
                raise ValueError()
            if any(a.startswith('--remote-debugging-') and a not in
                   ('--remote-debugging-port=' + str(self.port), '--remote-debugging-address=127.0.0.1') for a in args):
                raise ValueError()
            profile = Path(self.profile)
            if profile.is_symlink() or profile.stat().st_uid != os.getuid() or profile.stat().st_mode & 0o077:
                raise ValueError()
            sockets = set()
            for descriptor in (process / 'fd').iterdir():
                try:
                    sockets.add(os.readlink(descriptor))
                except FileNotFoundError:
                    continue
            matched = []
            for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
                fields = line.split()
                if fields[1].endswith(':' + format(self.port, '04X')) and fields[3] == '0A':
                    if fields[1] != '0100007F:' + format(self.port, '04X'):
                        raise ValueError()
                    matched.append('socket:[' + fields[9] + ']')
            if len(matched) != 1 or matched[0] not in sockets:
                raise ValueError()
            identity = (pid, (process / 'stat').read_text().rsplit(')', 1)[1].split()[19])
            if self.process_identity != identity:
                self.auth_targets.clear()
                self.account_targets.clear()
                self.process_identity = identity
            return pid
        except GuardianError:
            raise
        except Exception:
            raise GuardianError('browser_unavailable', status=409) from None

    def _http(self, path, method='GET'):
        self._owned()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, path, headers={'Host': '127.0.0.1:' + str(self.port)})
            response = connection.getresponse()
            content = response.read(MAX_MESSAGE + 1)
            if response.status != 200 or len(content) > MAX_MESSAGE:
                raise ValueError()
            return json.loads(content)
        except Exception:
            raise GuardianError('browser_unavailable', status=409) from None
        finally:
            connection.close()

    def targets(self):
        values = self._http('/json/list')
        if type(values) is not list or len(values) > 50:
            raise GuardianError('browser_unavailable', status=409)
        return [{k: v.get(k) for k in ('id', 'type', 'url')} for v in values if type(v) is dict]

    def _target(self, target_id):
        if not isinstance(target_id, str) or not re.fullmatch('[A-Za-z0-9-]{1,100}', target_id):
            raise GuardianError('browser_unavailable', status=409)
        matches = [v for v in self.targets() if v['id'] == target_id and v['type'] == 'page']
        if len(matches) != 1 or origin(matches[0]['url']) not in {p['origin'] for p in PROVIDERS.values()}:
            raise GuardianError('browser_unavailable', status=409)
        return matches[0]

    def evaluate(self, target_id, expression):
        self._target(target_id)
        result = self._rpc(target_id, 'Runtime.evaluate', {'expression': expression, 'returnByValue': True,
                                                          'awaitPromise': True, 'timeout': 15000})
        if result.get('exceptionDetails') or result.get('result', {}).get('subtype') == 'error':
            raise GuardianError('browser_unavailable', status=409)
        return result.get('result', {}).get('value')

    def inspect(self, provider):
        provider_id(provider)
        pages = [t for t in self.targets() if t['type'] == 'page' and origin(t['url']) == PROVIDERS[provider]['origin']]
        # Authenticated accounts may legitimately have multiple native tabs. A
        # single explicit login tab is the only page eligible for submission.
        login = [p for p in pages if urlsplit(p['url']).path.rstrip('/') in ('/login', '/welcome', '/accounts/signin')]
        pin = self.auth_targets.get(provider)
        pinned = next((p for p in pages if p['id'] == pin), None)
        if len(login) > 1 and (pinned is None or urlsplit(pinned['url']).path.rstrip('/') not in ('/login', '/welcome', '/accounts/signin')):
            # Several watched pages can redirect together when a session dies.
            # Create our own login-only tab, never choose an arbitrary form.
            self.ensure_login(provider)
            return {'state': 'page_missing'}
        target = pinned if pinned and urlsplit(pinned['url']).path.rstrip('/') in ('/login', '/welcome', '/accounts/signin') else login[0] if len(login) == 1 else pages[0] if len(pages) == 1 else None
        if target is None:
            return {'state': 'authenticated_unverified' if pages else 'page_missing'}
        result = self.evaluate(target['id'], LOGIN_PROGRAM + '(' + json.dumps(provider) + ',null)')
        if type(result) is not dict or result.get('state') not in {
            'logged_out', 'authenticated_unverified', 'mfa', 'captcha', 'bad_password',
            'ambiguous_form', 'untrusted_page', 'untrusted_form'}:
            return {'state': 'ambiguous_form'}
        return {'state': result['state'], 'targetId': target['id']}

    def submit(self, provider, target_id, username, password):
        provider_id(provider)
        if origin(self._target(target_id)['url']) != PROVIDERS[provider]['origin']:
            raise GuardianError('invalid_provider')
        # No parameters, credentials or responses ever enter argv/env/disk/logs.
        args = json.dumps(provider) + ',' + json.dumps({'username': username, 'password': password})
        result = self.evaluate(target_id, LOGIN_PROGRAM + '(' + args + ')')
        if type(result) is not dict or result.get('state') not in {
            'login_submitted', 'logged_out', 'authenticated_unverified', 'mfa', 'captcha',
            'bad_password', 'ambiguous_form', 'untrusted_page', 'untrusted_form'}:
            return {'state': 'ambiguous_form'}
        return {'state': result['state']}

    def open(self, provider):
        provider_id(provider)
        self._systemctl('start', self.unit)
        # One bounded warmup; no ownership checks are weakened on timeout.
        deadline = time.monotonic() + 8
        while True:
            try:
                pages = self.targets()
                break
            except GuardianError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
        if not any(t['type'] == 'page' and origin(t['url']) == PROVIDERS[provider]['origin'] for t in pages):
            self.ensure_login(provider)
        else:
            provider_pages = [t for t in pages if t['type'] == 'page' and origin(t['url']) == PROVIDERS[provider]['origin']]
            target = next((t for t in provider_pages if t['id'] == self.auth_targets.get(provider)), provider_pages[0])
            # Bring forward without changing a user's MFA/challenge location.
            self._rpc(target['id'], 'Page.bringToFront', {})

    def ensure_login(self, provider):
        provider_id(provider)
        pages = self.targets()
        existing = next((p for p in pages if p['id'] == self.auth_targets.get(provider)), None)
        if existing:
            if existing['url'] != PROVIDERS[provider]['login']:
                self._navigate(existing['id'], PROVIDERS[provider]['login'])
        else:
            self.auth_targets[provider] = self._new_page(PROVIDERS[provider]['login'])

    def _new_page(self, url):
        from urllib.parse import quote
        page = self._http('/json/new?' + quote(url, safe=''), method='PUT')
        if type(page) is not dict or not re.fullmatch('[A-Za-z0-9-]{1,100}', str(page.get('id', ''))):
            raise GuardianError('browser_unavailable', status=409)
        return page['id']

    def _navigate(self, target_id, url):
        # Private callers supply only compiled provider URLs or validated UUID
        # account templates; no public action accepts navigation destinations.
        self._target(target_id)
        self._rpc(target_id, 'Page.navigate', {'url': url})

    def _rpc(self, target_id, method, params):
        self._owned()
        payload = json.dumps({'id': 1, 'method': method, 'params': params}, separators=(',', ':')).encode()
        if len(payload) > MAX_MESSAGE:
            raise GuardianError('browser_unavailable', status=409)
        deadline = time.monotonic() + 18
        connection = socket.create_connection(('127.0.0.1', self.port), timeout=6)
        try:
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            request = ('GET /devtools/page/' + target_id + ' HTTP/1.1\r\nHost: 127.0.0.1:' + str(self.port)
                       + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: '
                       + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n')
            connection.sendall(request.encode())
            header = b''
            while not header.endswith(b'\r\n\r\n'):
                header += self._recv(connection, 1, deadline)
                if len(header) > 8192:
                    raise ValueError()
            lines = header.decode('ascii').split('\r\n')
            headers = {k.lower(): v.strip() for k, v in (line.split(':', 1) for line in lines[1:] if ':' in line)}
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if (not lines[0].startswith('HTTP/1.1 101 ') or headers.get('sec-websocket-accept', '') != accept
                    or headers.get('upgrade', '').lower() != 'websocket'):
                raise ValueError()
            self._send(connection, 1, payload)
            fragments = bytearray()
            for _ in range(100):
                connection.settimeout(max(.1, deadline - time.monotonic()))
                if time.monotonic() >= deadline:
                    raise ValueError()
                head = self._recv(connection, 2, deadline)
                final, opcode, masked, length = bool(head[0] & 128), head[0] & 15, head[1] & 128, head[1] & 127
                if masked or head[0] & 0x70:
                    raise ValueError()
                if length == 126:
                    length = struct.unpack('!H', self._recv(connection, 2, deadline))[0]
                elif length == 127:
                    length = struct.unpack('!Q', self._recv(connection, 8, deadline))[0]
                if length > MAX_MESSAGE:
                    raise ValueError()
                body = self._recv(connection, length, deadline)
                if opcode == 9:
                    self._send(connection, 10, body)
                    continue
                if opcode not in (0, 1) or len(fragments) + length > MAX_MESSAGE:
                    raise ValueError()
                fragments.extend(body)
                if final:
                    response = json.loads(fragments)
                    fragments.clear()
                    if response.get('id') == 1:
                        if 'error' in response:
                            raise ValueError()
                        return response['result']
            raise ValueError()
        except Exception:
            raise GuardianError('browser_unavailable', status=409) from None
        finally:
            connection.close()

    @staticmethod
    def _recv(connection, count, deadline):
        result = bytearray()
        while len(result) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            connection.settimeout(remaining)
            chunk = connection.recv(count - len(result))
            if not chunk:
                raise ValueError()
            result.extend(chunk)
        return bytes(result)

    @staticmethod
    def _send(connection, opcode, data):
        mask = secrets.token_bytes(4)
        head = bytes([128 | opcode])
        n = len(data)
        head += bytes([128 | n]) if n < 126 else bytes([254]) + struct.pack('!H', n) if n <= 65535 else bytes([255]) + struct.pack('!Q', n)
        connection.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
