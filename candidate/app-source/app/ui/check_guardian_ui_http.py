"""Built UI against the real loopback API and encrypted vault in temporary state.

The actual HTTP handler and CSRF/session boundary are used. No account monitoring
is enabled, no browser service is started and no real provider is contacted.

Guardian only manages a vault plus saved Tradovate/TradingView sign-ins now;
there is no more watched-account config. TradingView is used here because the
real backend's provider list (guardian_auth.PROVIDERS) only ever contains
'tradovate' and 'tradingview', and the UI filters out the bare 'tradovate' row.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent / 'backend'))
from server import LabServer, Store

BUILD = Path(os.environ.get('DECRYPT_UI_BUILD', str(ROOT / 'evidence/ui-build')))
OUT = Path(os.environ.get('DECRYPT_UI_EVIDENCE_DIR', str(ROOT / 'evidence/guardian-ui-http')))
OUT.mkdir(parents=True, exist_ok=True)
checks, errors, external, browser_calls = [], [], [], []
passphrase = 'FAKE-HTTP-UI-VAULT-PASSPHRASE'
password = 'FAKE-HTTP-UI-PROVIDER-PASSWORD'
username = 'http-ui-fixture@example.invalid'
# The real first-start check reads this machine's first-start state; pin a completed display so
# the app opens past the first-start notice on any test machine (as check_ui_api.py does).
NOTICE = (HERE.parents[2] / 'firstboot/NOTICE.txt').read_text()
FIRST_START = {'schema': 'decrypt.first-start-display.v1', 'status': 'complete', 'complete': True, 'promptRunning': False,
               'policySha256': '5bf99074c43e06acbd06023b425b1a218fdb00084f52fa5ae11fd56959dffb4b',
               'documentSha256': '2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055',
               'instanceId': '12345678-1234-4234-9234-123456789abc', 'noticeText': NOTICE,
               'source': 'root-published-display', 'nativeAcceptanceEvidence': False}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class ForbiddenBrowser:
    def __getattr__(self, name):
        browser_calls.append(name)
        raise AssertionError('No browser method is permitted in this HTTP smoke')


def check(name, value):
    assert value, name
    checks.append(name)


with tempfile.TemporaryDirectory(prefix='guardian-ui-http-') as temporary:
    static = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(BUILD)))
    static_thread = threading.Thread(target=static.serve_forever, daemon=True)
    static_thread.start()
    origin = f'http://127.0.0.1:{static.server_port}'
    backend = LabServer(('127.0.0.1', 0), Store(Path(temporary) / 'state'), ui_port=static.server_port, guardian_enabled=True)
    backend.first_start.status = lambda: FIRST_START
    backend.guardian.browser = ForbiddenBrowser()
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    api_origin = f'http://127.0.0.1:{backend.server_port}'

    def route(request):
        if not request.request.url.startswith(origin + '/'):
            external.append(request.request.url)
            request.abort()
        elif '/api/' in request.request.url:
            # This forwards bytes to the real API. No response fixtures or edits.
            response = request.fetch(url=api_origin + request.request.url[len(origin):])
            request.fulfill(response=response)
        else:
            request.continue_()

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get('DECRYPT_UI_CHROME') or None)
            context = browser.new_context(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
            context.route('**/*', route)
            context.set_default_timeout(10000)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))

            def connections():
                page.goto(origin)
                page.get_by_role('button', name='Connections', exact=True).click()
                page.get_by_role('heading', name='Guardian sign-ins', exact=True).wait_for()

            connections()
            panel = page.locator('.guardian-panel')
            provider = page.locator('.guardian-provider').filter(has=page.get_by_role('heading', name='TradingView', exact=True))
            expect(page.get_by_role('button', name='Create encrypted vault', exact=True)).to_be_visible()
            check('Real API reports unconfigured vault', not backend.guardian.status()['vault']['configured'])
            page.get_by_label('Unlock saved sign-ins', exact=True).select_option('passphrase')
            page.get_by_label('Vault passphrase', exact=True).fill(passphrase)
            page.get_by_label('Confirm passphrase', exact=True).fill(passphrase)
            page.get_by_role('button', name='Create encrypted vault', exact=True).click()
            expect(page.get_by_role('button', name='Lock Guardian vault', exact=True)).to_be_visible()
            check('Browser vault create reached real encrypted backend', backend.guardian.status()['vault']['configured'] is True and backend.guardian.status()['vault']['unlocked'] is True)
            provider.get_by_role('button', name='Save a sign-in', exact=True).click()
            page.get_by_label('TradingView username (not email)', exact=True).fill(username)
            page.get_by_label('TradingView password', exact=True).fill(password)
            check('Actual HTTP credential save defaults recovery off', not provider.get_by_role('checkbox').is_checked())
            page.get_by_role('button', name='Save TradingView credentials', exact=True).click()
            expect(provider.get_by_role('button', name='Replace saved sign-in', exact=True)).to_be_visible()
            saved = next(item for item in backend.guardian.status()['providers'] if item['id'] == 'tradingview')
            check('Real API confirms saved metadata only', saved['saved'] is True and saved['autoLogin'] is False and 'username' not in saved and 'password' not in saved)
            check('Password field cleared after real save', page.get_by_label('TradingView password', exact=True).count() == 0)
            page.get_by_role('button', name='Lock Guardian vault', exact=True).click()
            expect(page.get_by_role('button', name='Unlock saved sign-ins', exact=True)).to_be_visible()
            check('Real lock reflected in UI', backend.guardian.status()['vault']['unlocked'] is False and provider.get_by_role('button', name='Replace saved sign-in', exact=True).is_disabled())
            page.get_by_label('Vault passphrase', exact=True).fill('FAKE-INCORRECT-HTTP-PASSPHRASE')
            page.get_by_role('button', name='Unlock saved sign-ins', exact=True).click()
            expect(page.get_by_role('alert').filter(has_text='Check your passphrase')).to_be_visible()
            check('Real incorrect passphrase fails closed', backend.guardian.status()['vault']['unlocked'] is False)
            panel.evaluate("element => { const note = document.createElement('p'); note.textContent = 'REAL LOCAL API · FAKE CREDENTIALS · NO PROVIDER'; note.setAttribute('style', 'font:11px monospace;color:#ffba80;margin-bottom:20px'); element.prepend(note); }")
            panel.screenshot(path=str(OUT / 'real-http-failed-unlock-desktop.png'))
            page.get_by_role('button', name='Refresh Guardian status', exact=True).click()
            expect(page.get_by_role('button', name='Unlock saved sign-ins', exact=True)).to_be_visible()
            check('Failed passphrase never repopulates', page.get_by_label('Vault passphrase', exact=True).input_value() == '')
            # Cooldown is backend-owned; do not bypass it merely to complete UI tests.
            connections()
            check('Reload preserves locked metadata, not entered secrets', page.get_by_label('Vault passphrase', exact=True).input_value() == '' and backend.guardian.status()['vault']['unlocked'] is False)
            panel.evaluate("element => { const note = document.createElement('p'); note.textContent = 'REAL LOCAL API · FAKE CREDENTIALS · NO PROVIDER'; note.setAttribute('style', 'font:11px monospace;color:#ffba80;margin-bottom:20px'); element.prepend(note); }")
            page.set_viewport_size({'width': 390, 'height': 844})
            page.get_by_role('button', name='Refresh Guardian status', exact=True).focus()
            panel.screenshot(path=str(OUT / 'real-http-locked-narrow.png'))
            check('Real locked screen fits narrow viewport', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            serialized = json.dumps(backend.guardian.status())
            check('Actual status never returns credential material', all(secret not in serialized for secret in [username, password, passphrase]))
            storage = page.evaluate('JSON.stringify({local:{...localStorage},session:{...sessionStorage}})')
            check('Real request secrets absent from browser storage', all(secret not in storage for secret in [username, password, passphrase]))
            files = [item.read_bytes() for item in (Path(temporary) / 'state' / 'guardian').rglob('*') if item.is_file()]
            check('Credential material absent from plaintext state files', all(secret.encode() not in content for content in files for secret in [username, password, passphrase]))
            check('No real or fake provider browser method called', not browser_calls)
            check('No browser exceptions', not errors)
            check('No external request attempted', not external)
            context.close()
            browser.close()
    finally:
        backend.shutdown()
        backend.server_close()
        backend_thread.join(timeout=5)
        static.shutdown()
        static.server_close()
        static_thread.join(timeout=5)

(OUT / 'results.json').write_text(json.dumps({'kind': 'built-ui-real-loopback-http-encrypted-vault', 'passed': len(checks), 'checks': checks, 'errors': errors, 'providerContacted': False, 'browserServiceStarted': False, 'credentials': 'obvious test values only', 'realOrders': False, 'state': 'temporary and removed'}, indent=2) + '\n')
print(json.dumps({'passed': len(checks), 'evidence': str(OUT)}, indent=2))
