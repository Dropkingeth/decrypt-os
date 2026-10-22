#!/usr/bin/env python3
"""Own-child-only local lab launcher. Never uses production state or credentials."""
from __future__ import annotations
import argparse
import http.cookiejar
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / 'lab'
BACKEND = ROOT / 'app' / 'backend' / 'server.py'


def reserve_port(requested):
    # Bind test only; a later race is a startup error, never a process takeover.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', requested))
        return sock.getsockname()[1]


class Client:
    def __init__(self, port):
        self.base = f'http://127.0.0.1:{port}'
        self.token = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, data=None, headers=None):
        h = {'Accept': 'application/json'}
        if data is not None:
            h.update({'Content-Type': 'application/json', 'Origin': self.base})
            if self.token:
                h['X-CSRF-Token'] = self.token
        h.update(headers or {})
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(), headers=h)
        try:
            with self.opener.open(request, timeout=4) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            except (ValueError, UnicodeDecodeError):
                return error.code, {}

    def session(self):
        # The backend issues sessions only to its own page's same-origin fetch().
        status, body = self.request('/api/session', headers={'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'})
        assert status == 200, 'Session endpoint unavailable'
        self.token = body['csrfToken']


@contextlib.contextmanager
def backend(state, port):
    logfile = state.parent / 'backend.log'
    # Minimal environment excludes inherited API keys, proxies and agent context.
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONUNBUFFERED': '1',
           'LANG': 'C.UTF-8', 'HOME': str(state.parent), 'TMPDIR': str(state.parent)}
    with logfile.open('ab') as log:
        # Lab runs never send a strategy access request anywhere.
        offline = Path(state).parent / 'strategy-access.json'
        offline.write_text(json.dumps({'requestUrl': '', 'publicSite': 'https://decryptlabs.io'}))
        process = subprocess.Popen([sys.executable, str(BACKEND), '--state-dir', str(state), '--port', str(port),
                                    '--strategy-access-config', str(offline)],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        client = Client(port)
        try:
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError(f'Lab backend exited with code {process.returncode}; inspect {logfile}')
                try:
                    if client.request('/api/state')[0] == 200:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError('Lab backend did not become ready in six seconds')
            client.process = process
            yield client
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def check(client):
    results = []
    def passed(label):
        results.append(label)
    status, state = client.request('/api/state')
    assert status == 200 and state['schema'] == 'decrypt.os.lab.v1' and state['mode'] == 'lab'
    assert state['usage']['ai']['used'] is None and state['usage']['account']['balance'] is None
    assert state['connections'] == {'ai': 'not_connected', 'tradingview': 'not_connected', 'tradovate': 'not_connected'}
    passed('fresh identity has truthful unavailable usage and disconnected providers')
    client.session()
    for step in ('welcome', 'assistant', 'connections', 'review'):
        data = {'step': step}
        if step == 'assistant': data['assistantName'] = 'Fixture Orbit'
        status, _ = client.request('/api/onboarding', data)
        assert status in (200, 201), f'Cannot persist onboarding step {step}'
    status, state = client.request('/api/state')
    assert state['profile']['assistantName'] == 'Fixture Orbit'
    assert all(bot['status'] == 'not_connected' for bot in state['bots'])
    passed('onboarding checkpoints do not activate trading')
    status, state = client.request('/api/onboarding', {'step': 'review', 'complete': True})
    assert status == 200 and state['onboarding']['preferencesComplete']
    assert not state['onboarding']['tradingPreferences'] and state['strategyAccess']['latest'] is None
    passed('assistant-only preferences finish without trading or access requests')
    status, state = client.request('/api/onboarding', {'step': 'assistant', 'tradingPreferences': True})
    assert status == 200 and not state['onboarding']['preferencesComplete']
    for step in ('bot', 'connections', 'access', 'review'):
        status, _ = client.request('/api/onboarding', {'step': step})
        assert status == 200
    passed('optional trading screens require an explicit saved choice')
    status, state = client.request('/api/strategy-access/request', {'tradingViewUsername': 'fixture_user'})
    assert status == 200, 'Access request failed'
    latest = state['strategyAccess']['latest']
    assert latest['status'] == 'not_sent' and latest['reason'] == 'not_configured'
    assert state['strategyAccess']['tradingViewUsername'] == 'fixture_user'
    assert all(bot['status'] == 'not_connected' for bot in state['bots'])
    passed('offline access request keeps the username to send by hand and enables nothing')
    status, _ = client.request('/api/onboarding', {'step': 'assistant', 'assistantName': 'Attacker'}, {'Origin': 'https://example.invalid'})
    assert status in (400, 403)
    passed('foreign Origin cannot mutate local identity')
    status, _ = client.request('/api/onboarding', {'step': 'assistant', 'apiKey': 'fixture-not-a-secret'})
    assert status in (400, 422)
    passed('credential-shaped fields rejected')
    status, _ = client.request('/api/trading/enable', {})
    assert status in (400, 403, 404, 405)
    passed('trading endpoint absent')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args()
    if not BACKEND.is_file():
        parser.error(f'Backend not built yet: {BACKEND}')
    port = reserve_port(args.port if args.port is not None else (8810 if args.serve else 0))
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='fixture-', dir=LAB) as temporary:
        state = Path(temporary) / 'state'
        with backend(state, port) as client:
            if args.serve:
                print(f'LAB ONLY: {client.base}; disposable identity; no provider credentials accepted.', flush=True)
                try:
                    while client.process.poll() is None: time.sleep(1)
                    raise RuntimeError("Lab backend exited during interactive preview")
                except KeyboardInterrupt:
                    return 0
            checks = check(client)
        with backend(state, port) as client:
            _, restored = client.request('/api/state')
            assert restored['profile']['assistantName'] == 'Fixture Orbit'
            assert restored['strategyAccess']['tradingViewUsername'] == 'fixture_user'
            checks.append('restart preserves fixture identity and username')
        result = {'schema': 'decrypt.os.lab.check.v1', 'passed': len(checks), 'checks': checks,
                  'state_removed_after_run': True, 'real_provider_calls': False,
                  'clean_vm_provisioned': False, 'backend_sha256': __import__('hashlib').sha256(BACKEND.read_bytes()).hexdigest()}
    evidence = ROOT / 'evidence'
    if evidence.is_symlink():
        raise RuntimeError('Refusing symlink evidence directory')
    evidence.mkdir(mode=0o700, exist_ok=True)
    receipt = evidence / 'lab-smoke.json'
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        output.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
