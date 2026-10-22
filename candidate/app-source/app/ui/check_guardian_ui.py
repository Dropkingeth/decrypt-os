"""Guardian rendered-browser acceptance with isolated, clearly labelled fixtures.

No account credentials, real provider pages, installed profiles or live orders.
Responses exercise the real built UI against the documented Guardian API shape.

Guardian only manages an encrypted sign-in vault plus saved Tradovate and
TradingView sign-ins with opt-in automatic login recovery. There is no more
broker-account monitor, watched-account config or commissioning/discovery
flow; those were retired. The real backend only ever reports
the 'tradovate' and 'tradingview' provider ids (see guardian_auth.PROVIDERS),
and the UI filters out the bare 'tradovate' row, so TradingView is the only
provider card ever rendered here.
"""
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import threading

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[4]
BUILD = Path(os.environ.get('DECRYPT_UI_BUILD', str(ROOT / 'evidence/ui-build')))
OUT = Path(os.environ.get('DECRYPT_UI_EVIDENCE_DIR', str(ROOT / 'evidence/guardian-ui')))
OUT.mkdir(parents=True, exist_ok=True)
BASE = {'schema': 'decrypt.os.lab.v1', 'mode': 'lab', 'onboarding': {'version': 2, 'step': 'review', 'completed': ['welcome', 'assistant', 'connections', 'review'], 'tradingPreferences': False, 'preferencesComplete': True}, 'profile': {'assistantName': 'Fixture assistant'}, 'connections': {}, 'usage': {'ai': {'status': 'unavailable', 'used': None, 'limit': None}, 'account': {'status': 'unavailable', 'balance': None, 'currency': None}}, 'bots': [], 'activity': [], 'strategyAccess': {'tradingViewUsername': '', 'requestConfigured': True, 'publicSite': 'https://decryptlabs.io', 'latest': None}}
guard = {'enabled': True, 'vault': {'configured': False, 'unlocked': False}, 'providers': [{'id': item, 'label': label, 'saved': False, 'autoLogin': False, 'state': 'not_saved', 'reason': None, 'nextAttemptAt': None, 'requiresUser': True} for item, label in [('tradovate', 'Tradovate'), ('tradingview', 'TradingView')]], 'engineReady': False, 'executionAuthority': False}
checks, posts, errors, external, headers = [], [], [], [], []
mode = {'error': None, 'malformed': False, 'delay': None}
delayed_routes = []
fake_passphrase = 'FAKE-UI-VAULT-PASSPHRASE'
fake_password = 'FAKE-UI-PROVIDER-PASSWORD'
fake_username = 'ui-fixture@example.invalid'
# The app shows only the first-start notice until first start is complete (as in check_ui.py).
NOTICE = (Path(__file__).resolve().parents[2] / 'firstboot/NOTICE.txt').read_text()
FIRST_START = {'schema': 'decrypt.first-start-display.v1', 'status': 'complete', 'complete': True, 'promptRunning': False,
               'policySha256': '5bf99074c43e06acbd06023b425b1a218fdb00084f52fa5ae11fd56959dffb4b',
               'documentSha256': '2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055',
               'instanceId': '12345678-1234-4234-9234-123456789abc', 'noticeText': NOTICE,
               'source': 'root-published-display', 'nativeAcceptanceEvidence': False}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(BUILD)))
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
origin = f'http://127.0.0.1:{server.server_port}'


def check(name, value):
    assert value, name
    checks.append(name)


def provider_row(provider_id):
    return next(item for item in guard['providers'] if item['id'] == provider_id)


def route(request):
    if not request.request.url.startswith(origin + '/'):
        external.append(request.request.url)
        request.abort()
        return
    if '/api/' not in request.request.url:
        request.continue_()
        return
    path = request.request.url.split('/api', 1)[1]
    body = request.request.post_data_json if request.request.method == 'POST' else None
    if body is not None:
        posts.append((path, deepcopy(body)))
    if path == '/session':
        value = {'csrfToken': 'guardian-browser-fixture-csrf'}
    elif path == '/first-start/status':
        value = FIRST_START
    elif path == '/state':
        value = BASE
    elif path == '/harness/status':
        value = {'enabled': False, 'status': 'unavailable', 'runtime': 'OpenClaw', 'provider': 'Claude', 'credentialPersistence': 'session_only', 'gatewayPort': 0, 'model': None, 'loginRunning': False, 'consoleRunning': False, 'scopeUpdatedAt': None, 'error': None}
    elif path.startswith('/guardian/'):
        headers.append(request.request.headers.get('x-csrf-token'))
        action = path.rsplit('/', 1)[1]
        if mode['delay'] == action:
            delayed_routes.append(request)
            return
        if mode['error']:
            request.fulfill(status=400, content_type='application/json', body=json.dumps({'error': {'code': mode['error'], 'message': fake_password + ' raw unsafe provider payload'}}))
            return
        if mode['malformed']:
            request.fulfill(status=200, content_type='application/json', body='{}')
            return
        if action == 'initialize':
            check('Vault passphrase sent only in action body', body == {'passphrase': fake_passphrase, 'unlockMode': 'passphrase'} or body == {'unlockMode': 'keyring'})
            guard['vault'].update(configured=True, unlocked=True, unlockMode=body.get('unlockMode', 'keyring'))
        elif action == 'unlock':
            guard['vault']['unlocked'] = True
        elif action == 'lock':
            guard['vault']['unlocked'] = False
            for provider in guard['providers']:
                if provider['saved']:
                    provider['state'] = 'locked'
        elif action == 'save-provider':
            check('Provider save contract is explicit and independent', set(body) == {'provider', 'username', 'password', 'autoLogin'})
            check('Only typed fixture password is submitted', body['password'] == fake_password)
            provider_row(body['provider']).update(saved=True, autoLogin=body['autoLogin'], state='available', requiresUser=False)
        elif action == 'delete-provider':
            provider_row(body['provider']).update(saved=False, autoLogin=False, state='not_saved', requiresUser=True)
        elif action == 'resume-provider':
            check('Resume requests exact account verification', body == {'provider': 'tradingview', 'nativeAccountId': fake_username, 'acknowledgeManualSignIn': True})
            provider_row('tradingview').update(state='available', requiresUser=False)
        elif action not in ['status', 'check', 'open-browser']:
            raise AssertionError('Unexpected Guardian action: ' + action)
        value = guard
    else:
        value = {}
    request.fulfill(status=200, content_type='application/json', body=json.dumps(value))


try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get('DECRYPT_UI_CHROME') or None)
        context = browser.new_context(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
        context.route('**/*', route)
        context.set_default_timeout(8000)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))


        def open_connections():
            page.goto(origin)
            page.get_by_role('button', name='Connections', exact=True).click()
            page.get_by_role('heading', name='Guardian sign-ins', exact=True).wait_for()

        def refresh():
            page.get_by_role('button', name='Refresh Guardian status', exact=True).click()
            expect(page.get_by_role('button', name='Refresh Guardian status', exact=True)).to_be_enabled()

        def screenshot(name):
            page.locator('.guardian-panel').evaluate("element => { if (!element.querySelector('[data-fixture-label]')) { const note = document.createElement('p'); note.dataset.fixtureLabel = 'true'; note.textContent = 'BROWSER FIXTURE · NO REAL ACCOUNT'; note.setAttribute('style', 'font:11px monospace;color:#ffba80;margin-bottom:20px'); element.prepend(note); } }")
            page.locator('.guardian-panel').screenshot(path=str(OUT / name))

        open_connections()
        panel = page.locator('.guardian-panel')
        tradingview = page.locator('.guardian-provider').filter(has=page.get_by_role('heading', name='TradingView', exact=True))
        check('Only TradingView is rendered; the bare Tradovate row is filtered', panel.locator('.guardian-provider').count() == 1)
        check('Manual browser sign-in available without saved credentials', tradingview.get_by_role('button', name='Show TradingView feed window', exact=True).is_enabled())
        check('Missing credentials do not claim a sign-in challenge', panel.get_by_text("Complete sign-in in this provider's browser window", exact=False).count() == 0)
        check('Cannot save credentials before vault unlock', tradingview.get_by_role('button', name='Save a sign-in', exact=True).is_disabled())
        check('Check sign-ins is available without any saved provider', panel.get_by_role('button', name='Check sign-ins', exact=True).is_enabled())
        screenshot('unconfigured-desktop.png')
        page.get_by_label('Unlock saved sign-ins', exact=True).select_option('passphrase')
        page.get_by_label('Vault passphrase', exact=True).fill(fake_passphrase)
        page.get_by_label('Confirm passphrase', exact=True).fill('FAKE-MISMATCH-PASSPHRASE')
        page.get_by_role('button', name='Create encrypted vault', exact=True).click()
        expect(page.get_by_role('alert').filter(has_text='did not match')).to_be_visible()
        check('Mismatched passphrases never sent', not any(path == '/guardian/initialize' for path, body in posts))
        check('Mismatched passphrase fields clear', page.get_by_label('Vault passphrase', exact=True).input_value() == '' and page.get_by_label('Confirm passphrase', exact=True).input_value() == '')
        page.get_by_label('Vault passphrase', exact=True).fill(fake_passphrase)
        page.get_by_label('Confirm passphrase', exact=True).fill(fake_passphrase)
        page.get_by_role('button', name='Create encrypted vault', exact=True).click()
        page.get_by_role('button', name='Lock Guardian vault', exact=True).wait_for()
        check('Passphrase fields disappear after create', page.get_by_label('Vault passphrase', exact=True).count() == 0)
        tradingview.get_by_role('button', name='Save a sign-in', exact=True).click()
        consent = tradingview.get_by_role('checkbox', name='Allow Guardian to sign back in to TradingView automatically', exact=True)
        check('Automatic sign-in is off by default', not consent.is_checked())
        page.get_by_label('TradingView username (not email)', exact=True).fill(fake_username)
        page.get_by_label('TradingView password', exact=True).fill(fake_password)
        check('Provider password is masked', page.get_by_label('TradingView password', exact=True).get_attribute('type') == 'password')
        consent.focus()
        page.keyboard.press('Space')
        check('Recovery opt-in works by keyboard', consent.is_checked())
        page.get_by_role('button', name='Save TradingView credentials', exact=True).click()
        expect(tradingview.get_by_role('button', name='Replace saved sign-in', exact=True)).to_be_visible()
        check('Credential fields clear and close after save', page.get_by_label('TradingView password', exact=True).count() == 0)
        check('Saving TradingView does not opt Tradovate in', not provider_row('tradovate')['saved'] and not provider_row('tradovate')['autoLogin'])
        tradingview.get_by_role('button', name='Replace saved sign-in', exact=True).click()
        check('Replacement never prefills saved username or password', page.get_by_label('TradingView username (not email)', exact=True).input_value() == '' and page.get_by_label('TradingView password', exact=True).input_value() == '')
        tradingview.get_by_role('button', name='Cancel credential change', exact=True).click()
        screenshot('saved-desktop.png')
        before_browser = sum(path == '/guardian/open-browser' for path, body in posts)
        check('No browser launch is automatic UI side effect', before_browser == 0)
        provider_row('tradingview').update(state='requires_user', requiresUser=True)
        refresh()
        expect(panel.get_by_text("Complete sign-in in this provider's browser window", exact=False)).to_be_visible()
        tradingview.get_by_role('button', name='Show TradingView feed window', exact=True).focus()
        page.keyboard.press('Enter')
        expect(panel.get_by_text('Continue in the Guardian browser on this device.', exact=False)).to_be_visible()
        check('Manual browser action targets exact provider', ('/guardian/open-browser', {'provider': 'tradingview'}) in posts)
        check('Challenge never requests a code in app', panel.get_by_label('Signed-in TradingView username', exact=True).is_visible() and not panel.locator('input[autocomplete="one-time-code"]').count())
        panel.get_by_role('button', name='Check sign-ins', exact=True).click()
        expect(panel.get_by_text('Sign-in check completed. Review provider status below.', exact=False)).to_be_visible()
        check('Sign-in check is an explicit empty-body action', ('/guardian/check', {}) in posts)
        screenshot('requires-user-desktop.png')
        page.set_viewport_size({'width': 390, 'height': 844})
        screenshot('requires-user-narrow.png')
        check('390px viewport has no horizontal overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.set_viewport_size({'width': 320, 'height': 900})
        check('320px viewport has no horizontal overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.set_viewport_size({'width': 1440, 'height': 1100})
        page.get_by_label('Signed-in TradingView username', exact=True).fill(fake_username)
        check('Recovery resume requires manual sign-in acknowledgment', page.get_by_role('button', name='Verify TradingView sign-in and resume', exact=True).is_disabled())
        page.get_by_role('checkbox', name="I completed sign-in in this provider's browser window.", exact=True).check()
        page.get_by_role('button', name='Verify TradingView sign-in and resume', exact=True).click()
        expect(page.get_by_text('The signed-in account was verified.', exact=False)).to_be_visible()
        check('Successful native verification removes the challenge prompt', panel.get_by_text("Complete sign-in in this provider's browser window", exact=False).count() == 0)
        page.get_by_role('button', name='Lock Guardian vault', exact=True).click()
        expect(page.get_by_role('button', name='Unlock saved sign-ins', exact=True)).to_be_visible()
        check('Locked vault disables credential replacement', tradingview.get_by_role('button', name='Replace saved sign-in', exact=True).is_disabled())
        screenshot('locked-desktop.png')
        page.get_by_label('Vault passphrase', exact=True).fill('FAKE-WRONG-PASSPHRASE')
        mode['error'] = 'unlock_failed'
        page.get_by_role('button', name='Unlock saved sign-ins', exact=True).click()
        expect(page.get_by_role('alert').filter(has_text='Check your passphrase')).to_be_visible()
        expect(page.get_by_role('alert').filter(has_text='Check your passphrase')).to_be_focused()
        check('Raw provider error payload not displayed', fake_password not in page.locator('body').inner_text())
        check('Failed action hides vault and provider sections', panel.locator('.guardian-vault').count() == 0 and panel.locator('.guardian-provider').count() == 0)
        screenshot('safe-error-desktop.png')
        mode['error'] = None
        refresh()
        page.get_by_label('Vault passphrase', exact=True).fill(fake_passphrase)
        page.get_by_role('button', name='Unlock saved sign-ins', exact=True).click()
        expect(page.get_by_role('button', name='Lock Guardian vault', exact=True)).to_be_visible()
        tradingview.get_by_role('button', name='Remove saved sign-in', exact=True).click()
        check('Removal needs explicit inline confirmation', not any(path == '/guardian/delete-provider' for path, body in posts))
        tradingview.get_by_role('button', name='Confirm removal', exact=True).click()
        expect(tradingview.get_by_role('button', name='Save a sign-in', exact=True)).to_be_visible()
        check('Removal disables automatic recovery', not provider_row('tradingview')['autoLogin'])
        tradingview.get_by_role('button', name='Save a sign-in', exact=True).click()
        page.get_by_label('TradingView password', exact=True).fill(fake_password)
        page.get_by_label('TradingView username (not email)', exact=True).fill(fake_username)
        guard['vault']['unlocked'] = False
        refresh()
        check('External vault lock clears unsaved provider secrets', page.get_by_label('TradingView password', exact=True).count() == 0 and page.get_by_label('Vault passphrase', exact=True).input_value() == '')
        guard['vault']['unlocked'] = True
        refresh()
        tradingview.get_by_role('button', name='Save a sign-in', exact=True).click()
        page.get_by_label('TradingView password', exact=True).fill(fake_password)
        page.get_by_label('TradingView username (not email)', exact=True).fill(fake_username)
        mode['delay'] = 'save-provider'
        page.get_by_role('button', name='Save TradingView credentials', exact=True).click()
        expect(page.get_by_label('TradingView password', exact=True)).to_have_value('')
        check('Submitted secrets clear before a delayed response', page.get_by_label('TradingView username (not email)', exact=True).input_value() == '')
        page.get_by_role('button', name='Home', exact=True).click()
        page.get_by_role('button', name='Connections', exact=True).click()
        expect(tradingview.get_by_role('button', name='Save a sign-in', exact=True)).to_be_visible()
        provider_row('tradingview').update(saved=True, autoLogin=True, state='available')
        late_response = deepcopy(guard)
        check('Deferred action held exactly one request', len(delayed_routes) == 1)
        mode['delay'] = None
        delayed_routes.pop().fulfill(status=200, content_type='application/json', body=json.dumps(late_response))
        expect(tradingview.get_by_role('button', name='Save a sign-in', exact=True)).to_be_visible()
        check('Late response cannot overwrite a remounted screen', tradingview.get_by_role('button', name='Replace saved sign-in', exact=True).count() == 0)
        mode['malformed'] = True
        refresh()
        expect(page.get_by_role('alert').filter(has_text='could not confirm')).to_be_visible()
        check('Malformed service response hides vault and provider sections', panel.locator('.guardian-provider').count() == 0)
        check('All Guardian posts carry local CSRF token', headers and all(item == 'guardian-browser-fixture-csrf' for item in headers))
        check('Credentials absent from browser storage', all(value not in page.evaluate('JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})') for value in [fake_password, fake_passphrase, fake_username]))
        check('No browser exceptions', not errors)
        check('No external request attempted', not external)
        check('Only allowlisted Guardian actions issued', all(path.rsplit('/', 1)[1] in ['status', 'initialize', 'unlock', 'lock', 'save-provider', 'delete-provider', 'open-browser', 'check', 'resume-provider'] for path, body in posts if path.startswith('/guardian/')))
        context.close()
        browser.close()
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

(OUT / 'results.json').write_text(json.dumps({'kind': 'isolated-guardian-rendered-browser-fixtures', 'passed': len(checks), 'checks': checks, 'errors': errors, 'externalRequests': 'blocked', 'realProviderLogin': False, 'realProviderObservation': False, 'realOrders': False, 'screenshots': sorted(item.name for item in OUT.glob('*.png'))}, indent=2) + '\n')
print(json.dumps({'passed': len(checks), 'evidence': str(OUT)}, indent=2))
