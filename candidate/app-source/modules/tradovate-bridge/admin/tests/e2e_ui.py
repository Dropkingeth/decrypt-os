#!/usr/bin/env python3
"""Click-through of the Bridge Advanced page against its OWN throwaway sandbox admin server. VM only.

Starts app/server.py (this tree's app/) on a separate port (default 8792, never 8781 where the live
service runs) with a fresh copy of sandbox/pristine-tree, its own admin state, a file:// status fixture
(all flat) and its own XDG_RUNTIME_DIR, so the token it reads is its own server's. Drives a throwaway
headless Chromium over CDP (loopback port, profile in ~/.cache), clicks the real buttons, and checks the
copied files changed as the page said. Leaves no browser or server running.

    TMPDIR=$HOME/.cache python3 tests/e2e_ui.py
    (env: TVBRIDGE_ADMIN_SANDBOX=<sandbox dir> for a work copy; E2E_ADMIN_PORT=<port>)
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket  # websocket-client

ADMIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SB = os.environ.get('TVBRIDGE_ADMIN_SANDBOX') or os.path.join(ADMIN, 'sandbox')
SHOTS = os.path.expanduser('~/.cache/tba-e2e')
PORT = 9781                                             # Chromium DevTools (throwaway browser)
ADMIN_PORT = int(os.environ.get('E2E_ADMIN_PORT', '8792'))  # a free port beside the default admin port
LIVE_ADMIN_PORT = 8781
KEYS = TREE = LOG = None   # set by start_admin() from the sandbox's own firing.json

JS_HELPERS = r"""
window.__q = (sel, root) => [...(root||document).querySelectorAll(sel)];
window.__card = (label) => __q('li, article, .card').find(el => {
  const t = el.querySelector('h3, .name, strong, b'); return t && t.textContent.trim() === label; });
window.__btn = (text, root) => __q('button', root).find(b => b.textContent.trim() === text && !b.disabled);
window.__dlg = () => document.getElementById('dlg');
"""


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({'id': self.n, 'method': method, 'params': params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == self.n:
                if 'error' in msg:
                    raise RuntimeError(msg['error'])
                return msg.get('result', {})

    def js(self, expr):
        r = self.call('Runtime.evaluate', expression=expr, awaitPromise=True, returnByValue=True)
        if r.get('exceptionDetails'):
            raise RuntimeError(r['exceptionDetails'].get('text') + ' ' + json.dumps(r['exceptionDetails'])[:400])
        return r.get('result', {}).get('value')

    def until(self, expr, what, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            v = self.js(expr)
            if v:
                return v
            time.sleep(0.2)
        raise AssertionError('timed out waiting for ' + what)

    def shot(self, name):
        data = self.call('Page.captureScreenshot', format='png')['data']
        import base64
        with open(os.path.join(SHOTS, name + '.png'), 'wb') as f:
            f.write(base64.b64decode(data))


def ok(cond, msg):
    print(('PASS ' if cond else 'FAIL ') + msg, flush=True)
    if not cond:
        raise SystemExit(1)


def port_busy(port):
    import socket
    with socket.socket() as so:
        so.settimeout(0.5)
        return so.connect_ex(('127.0.0.1', port)) == 0


def follower_entry(key):
    """The follower entry for `key` in whichever login config of the copied tree lists it."""
    for cfg in glob.glob(os.path.join(TREE, '**/config.json'), recursive=True):
        for e in json.load(open(cfg)).get('followers') or []:
            if e.get('key') == key:
                return e
    raise SystemExit('no follower entry for ' + key)


def start_admin(tmp):
    """Own sandbox copy + own admin server on ADMIN_PORT. Returns (process, token)."""
    global KEYS, TREE, LOG
    if ADMIN_PORT == LIVE_ADMIN_PORT:
        raise SystemExit('refusing: 8781 is the live admin service')
    if port_busy(ADMIN_PORT):
        raise SystemExit(f'refusing: something already listens on {ADMIN_PORT}')
    shutil.copytree(os.path.join(SB, 'pristine-tree'), os.path.join(tmp, 'tree'), symlinks=True)
    for path in glob.glob(os.path.join(tmp, 'tree/**/*.json'), recursive=True) + glob.glob(os.path.join(tmp, 'tree/tunnel/*')):
        with open(path) as f:
            text = f.read()
        with open(path, 'w') as f:
            f.write(text.replace(SB + '/', tmp + '/'))
    with open(os.path.join(SB, 'profile.json')) as f:
        prof = json.loads(f.read().replace(SB, tmp))
    with open(os.path.join(tmp, 'tree/tvbridge/firing.json')) as f:
        KEYS = json.load(f)['firing_order']
    status = os.path.join(tmp, 'status.json')
    with open(status, 'w') as f:   # every account flat, as the read-only panel would show it
        json.dump({'followers': {'cards': [{'key': k, 'position': {'net': 0, 'text': 'Flat'}, 'orders': {'text': 'none'},
                                            'status': {'label': 'OK', 'tone': 'green'}} for k in KEYS]},
                   'logins': [], 'soup': {'market_session': {'state': 'closed', 'reason': 'e2e'}}}, f)
    prof.update(port=ADMIN_PORT, mode='sandbox', restarter='simulated', order_adapter='dry-run',
                bridge_release=os.path.join(SB, 'bridge-release'), status_url='file://' + status,
                admin_state=os.path.join(tmp, 'admin-state'))
    prof.pop('live_switches_only', None)
    with open(os.path.join(tmp, 'profile.json'), 'w') as f:
        json.dump(prof, f)
    run = os.path.join(tmp, 'run')
    os.makedirs(run, mode=0o700)
    env = dict(os.environ, XDG_RUNTIME_DIR=run, PYTHONDONTWRITEBYTECODE='1')
    proc = subprocess.Popen([sys.executable, os.path.join(ADMIN, 'app', 'server.py'), '--profile',
                             os.path.join(tmp, 'profile.json')], env=env, stdout=open(os.path.join(tmp, 'server.log'), 'w'),
                            stderr=subprocess.STDOUT)
    tok_path = os.path.join(run, 'tvbridge-admin', f'token-{ADMIN_PORT}')
    for _ in range(50):
        if os.path.exists(tok_path) and port_busy(ADMIN_PORT):
            break
        if proc.poll() is not None:
            raise SystemExit('admin server exited: ' + open(os.path.join(tmp, 'server.log')).read()[-500:])
        time.sleep(0.2)
    TREE = os.path.join(tmp, 'tree/tvbridge')
    LOG = os.path.join(tmp, 'admin-state/dry-run-orders.jsonl')
    print(f'own sandbox admin on 127.0.0.1:{ADMIN_PORT} (app {os.path.join(ADMIN, "app")}, tree {TREE})', flush=True)
    return proc, open(tok_path).read().strip()


def main():
    import tempfile
    tmp = tempfile.mkdtemp(prefix='tba-e2e-', dir=os.path.expanduser('~/.cache'))
    shutil.rmtree(SHOTS, ignore_errors=True)
    os.makedirs(SHOTS + '/prof')
    try:
        server, token = start_admin(tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    proc = subprocess.Popen(['chromium', '--headless=new', '--disable-gpu', '--no-first-run',
                             f'--user-data-dir={SHOTS}/prof', f'--remote-debugging-port={PORT}',
                             '--remote-debugging-address=127.0.0.1', '--window-size=1600,1000', 'about:blank'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f'http://127.0.0.1:{PORT}/json'))
                page = next(t for t in tabs if t['type'] == 'page')
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        c = CDP(page['webSocketDebuggerUrl'])
        c.call('Page.enable')
        c.call('Runtime.enable')
        c.call('Page.navigate', url=f'http://127.0.0.1:{ADMIN_PORT}/advanced#t={token}')
        c.until("document.readyState==='complete' && !!document.querySelector('#webhooks *')", 'page load')
        c.js(JS_HELPERS)
        ok(c.js("location.hash===''"), 'token removed from the address bar')
        firing = json.load(open(os.path.join(TREE, 'firing.json')))
        first, last = KEYS[1], KEYS[-1]   # the first and the last follower in the firing order
        lab1, lab2 = firing['accounts'][first]['label'], firing['accounts'][last]['label']
        c.until(f"!!__card('{lab2}')", lab2 + ' card')

        # 1. Pause the last follower from its card
        c.js(f"__btn('Pause', __card('{lab2}')).click()")
        c.until("__dlg().open && !!__dlg().querySelector('input[name=reason]')", 'pause dialog')
        c.js("{const i=__dlg().querySelector('input[name=reason]'); i.value='e2e test'; "
             "i.dispatchEvent(new Event('input',{bubbles:true})); __btn('Preview pause', __dlg()).click()}")
        c.until("!!__btn('Confirm', __dlg())", 'pause preview')
        c.shot('preview-pause')
        c.js("__btn('Confirm', __dlg()).click()")
        kill = follower_entry(last)['kill_switch_file']
        end = time.time() + 8
        while time.time() < end and not os.path.exists(kill):
            time.sleep(0.2)
        ok(os.path.exists(kill) and json.load(open(kill))['reason'] == 'e2e test', 'Pause wrote the sandbox switch')
        c.js("__dlg().open && __dlg().close()")
        c.until(f"!!__card('{lab2}') && __card('{lab2}').textContent.includes('Admin pause')",
                'card shows Admin pause', timeout=12)
        ok(True, 'card shows Admin pause after the poll')

        # 2. Resume it
        c.js(f"__btn('Resume', __card('{lab2}')).click()")
        c.until("!!__btn('Confirm', __dlg())", 'resume preview')
        c.js("__btn('Confirm', __dlg()).click()")
        end = time.time() + 8
        while time.time() < end and os.path.exists(kill):
            time.sleep(0.2)
        ok(not os.path.exists(kill) and glob.glob(kill + '.removed-*'), 'Resume renamed the switch (.removed-<UTC>)')
        c.js("__dlg().open && __dlg().close()")

        # 3. Flatten now on the first follower: the typed label gates Confirm; dry run only
        c.js(f"__btn('Flatten now', __card('{lab1}')).click()")
        c.until("__dlg().open && !!__q('button', __dlg()).find(b=>/Flatten|Confirm/.test(b.textContent) && b.disabled)",
                'flatten preview with a disabled confirm')
        c.shot('preview-flatten')
        c.js("{const i=__q('input', __dlg()).pop(); i.value='" + lab1 + "'; i.dispatchEvent(new Event('input',{bubbles:true}))}")
        c.until("!!__q('button', __dlg()).find(b=>/Flatten|Confirm/.test(b.textContent) && b.classList.contains('primary') && !b.disabled)",
                'confirm enabled after typing the label')
        c.js("__q('button', __dlg()).find(b=>/Flatten|Confirm/.test(b.textContent) && b.classList.contains('primary')).click()")
        log = LOG
        end = time.time() + 8
        while time.time() < end and not os.path.exists(log):
            time.sleep(0.2)
        calls = [json.loads(x) for x in open(log)] if os.path.exists(log) else []
        ok(calls and calls[-1]['account'] == first and calls[-1]['sent'] is False,
           'Flatten recorded a dry-run call and sent nothing')
        c.js("__dlg().open && __dlg().close()")
        time.sleep(3.5)
        c.shot('after-flows')
        print('screens in', SHOTS)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Chromium forks helpers; make sure none outlive the test.
        subprocess.run(['pkill', '-f', f'user-data-dir={SHOTS}/prof'], check=False)
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
