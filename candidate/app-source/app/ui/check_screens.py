"""Render the customer screens against fixture API data and save screenshots.

No real service, account, browser profile or network: every /api call is answered
from the fixtures below, and every other origin is blocked. Run after `npm run build`.

  DECRYPT_UI_SCREENS=/somewhere/outside/the/repo python3 check_screens.py

Viewports: 1920x1200 at 1.25 scale (the OS default) and 1280x800 at 1.0.
"""
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import re
import threading

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
OUT = Path(os.environ.get('DECRYPT_UI_SCREENS', str(BASE / 'ui-screens')))
OUT.mkdir(parents=True, exist_ok=True)
CHROME = os.environ.get('DECRYPT_UI_CHROME')  # unset: use Playwright's bundled Chromium
NOTICE = (ROOT / 'firstboot' / 'NOTICE.txt').read_text()
CATALOG = json.loads((BASE.parent / 'engine-module-catalog.json').read_text())
NOW = '2021-10-10T16:00:00Z'

def first_start(status):
    return {'schema': 'decrypt.first-start-display.v1', 'status': status, 'complete': status == 'complete', 'promptRunning': status == 'awaiting-password',
            'policySha256': '5bf99074c43e06acbd06023b425b1a218fdb00084f52fa5ae11fd56959dffb4b', 'documentSha256': '2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055',
            'instanceId': '12345678-1234-4234-9234-123456789abc', 'noticeText': NOTICE, 'source': 'root-published-display', 'nativeAcceptanceEvidence': False}

FRESH_STATE = {'schema': 'decrypt.os.lab.v1', 'mode': 'lab', 'onboarding': {'version': 2, 'step': 'welcome', 'completed': [], 'tradingPreferences': False, 'preferencesComplete': False},
    'profile': {'assistantName': '', 'tradingViewUsername': ''}, 'connections': {}, 'usage': {'ai': {'status': 'unavailable', 'used': None, 'limit': None}, 'account': {'status': 'unavailable', 'balance': None, 'currency': None}},
    'bots': [], 'activity': [], 'strategyAccess': {'tradingViewUsername': '', 'requestConfigured': True, 'publicSite': 'https://decryptlabs.io', 'latest': None},
    'engineWorkspace': {'schema': 'decrypt.engine-workspace.v1', 'setupOnly': True, 'catalog': CATALOG}}

READY_STATE = deepcopy(FRESH_STATE)
READY_STATE['onboarding'] = {'version': 2, 'step': 'review', 'completed': ['welcome', 'assistant', 'bot', 'connections', 'access', 'review'], 'tradingPreferences': True, 'preferencesComplete': True}
READY_STATE['profile'] = {'assistantName': 'Atlas', 'tradingViewUsername': 'fixture_trader'}
READY_STATE['strategyAccess'] = {'tradingViewUsername': 'fixture_trader', 'requestConfigured': True, 'publicSite': 'https://decryptlabs.io',
    'latest': {'tradingViewUsername': 'fixture_trader', 'contact': '', 'status': 'sent', 'reason': None, 'createdAt': NOW, 'sentAt': NOW}}
READY_STATE['activity'] = [{'id': 'a1', 'type': 'onboarding_updated', 'message': 'Setup preferences completed.', 'createdAt': NOW}, {'id': 'a2', 'type': 'access_requested', 'createdAt': '2021-10-10T15:00:00Z'}]

HARNESS = {'fresh': {'enabled': True, 'status': 'not_started', 'runtime': 'OpenClaw', 'provider': 'Claude', 'credentialPersistence': 'session_only', 'gatewayPort': 0, 'model': None, 'loginRunning': False, 'consoleRunning': False, 'ictRunning': False, 'scopeUpdatedAt': None, 'error': None},
    'login': {'enabled': True, 'status': 'needs_login', 'runtime': 'OpenClaw', 'provider': 'Claude', 'credentialPersistence': 'session_only', 'gatewayPort': 19789, 'model': None, 'loginRunning': False, 'consoleRunning': False, 'ictRunning': False, 'scopeUpdatedAt': NOW, 'error': None},
    'ready': {'enabled': True, 'status': 'connected', 'runtime': 'OpenClaw', 'provider': 'Claude', 'credentialPersistence': 'encrypted', 'storage': {'configured': True, 'unlocked': True, 'error': None, 'busy': False, 'operation': None}, 'gatewayPort': 19789, 'model': 'claude-opus-5-5', 'loginRunning': False, 'consoleRunning': False, 'ictRunning': False, 'scopeUpdatedAt': NOW, 'error': None}}

PACKAGES = [{'pairId': 'soup-pine-authority-r4', 'label': 'ICT - Turtle Soup Shift engine', 'version': 'soup-r4-os-1', 'engineId': 'soup', 'publicationVerified': False, 'providerCommissioned': False, 'tp1OffsetMode': 'explicit'}]   # the app installs Soup only; decrypt-bridge-v2 engines are set up with the bridge's setup
BRIDGE = {'fresh': {'configured': False, 'paused': None, 'logins': [], 'firing': False, 'state': 'not_configured'}, 'ready': {'configured': True, 'paused': True, 'logins': [{'id': 'a', 'label': 'Login A', 'primary': True}], 'firing': True, 'state': 'configured'}}
BRIDGE['setup'] = BRIDGE['ready']
# What the bridge reports after sign-in (POST /trading/bridge-login): fixture accounts only.
LOGIN = {'fresh': {'configured': False, 'account': None, 'daemon': 'unreachable', 'signIn': 'unknown', 'detail': 'The bridge is not set up yet.', 'accounts': [], 'nameMatch': None, 'numberMatch': None, 'guard': None},
    'setup': {'configured': True, 'account': {'name': 'DEMO000000', 'number': '123456', 'contract': 'MNQZ2026'}, 'daemon': 'unreachable', 'signIn': 'unknown', 'detail': 'The bridge is not running, or not answering yet. Turn it on, then check again.', 'accounts': [], 'nameMatch': None, 'numberMatch': None, 'guard': None},
    'ready': {'configured': True, 'account': {'name': 'DEMO000000', 'number': '123456', 'contract': 'MNQZ2026'}, 'daemon': 'reachable', 'signIn': 'connected', 'detail': 'Signed in. The bridge sees 2 accounts.', 'accounts': [{'id': 123456, 'name': 'DEMO000000', 'configured': True}, {'id': 123457, 'name': 'DEMO000001', 'configured': False}], 'nameMatch': True, 'numberMatch': True, 'guard': {'state': 'ok', 'reason': 'logged_in'}}}
INSTANCE = {'instanceId': 'inst_fixture_soup', 'route': 'tradovate_bridge', 'runtimeState': 'running', 'entriesHeld': True, 'entryReady': False, 'nativeFresh': True, 'tradePhase': 'idle', 'pendingCommands': 0,
    'blockers': ['weekend'], 'feed': {'state': 'fresh', 'nativeVerified': True}, 'calendar': {'state': 'weekend', 'reason': 'weekend', 'entryAllowed': False, 'expectedSymbol': 'MNQZ2026', 'coverageEnds': '2027-03-01', 'managementAllowed': True, 'checkedAt': 1633824000000, 'validUntil': 4102444800000},
    'binding': {'loginGroupId': 'login-1', 'accountId': 'DEMO000000', 'symbol': 'MNQZ2026', 'pairId': 'soup-pine-authority-r4', 'provider': 'tradovate', 'accountMode': 'demo', 'nativeAccountId': 123456, 'nativeAccountName': 'DEMO000000', 'streamId': 'soup-main', 'feedId': 'mnq-1s'}}
def trading(kind):
    return {'enabled': True, 'mode': 'customer_provider_bound', 'bridge': BRIDGE[kind], 'package': PACKAGES[0], 'packages': PACKAGES, 'account': None,
            'notifications': {'enabled': kind == 'ready', 'pending': 0, 'events': [], 'userRead': False, 'receiptSemantics': 'desktop-accepted'},
            'instances': [INSTANCE] if kind == 'ready' else [], 'retiredInstances': [], 'bridgeLogin': LOGIN[kind], 'ingress': {'state': 'not_configured', 'publicTlsVerified': False}, 'ingressKeys': {'locked': False, 'producers': []},
            'health': [{'source': 'bridge', 'state': 'fresh', 'changed': 1633824000000}] if kind == 'ready' else [], 'events': [], 'entryAuthority': False}
GUARDIAN = {'enabled': True, 'vault': {'configured': True, 'unlocked': True, 'unlockMode': 'keyring', 'unlockError': None}, 'providers': [{'id': 'tradingview', 'label': 'TradingView', 'saved': False, 'autoLogin': False, 'state': 'not_saved', 'reason': None, 'nextAttemptAt': None, 'requiresUser': False}]}
MAINTENANCE = {'maintenance': {'schema': 'decrypt.maintenance-status.v1', 'backupCount': 0, 'backups': [], 'restores': [], 'transactions': [], 'stagedVersions': [], 'currentVersion': 'installed', 'nativeReconciliationRequired': False, 'restartRequired': False, 'largeArchiveTransfer': 'local_tool', 'installedBaselineConfigured': True}}

scenario = {'first_start': 'complete', 'state': FRESH_STATE, 'harness': 'fresh', 'trading': 'fresh'}
errors, external = [], []

class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args): pass
    def send_head(self):
        # Same single-page fallback as installer/frontend.py: unknown paths serve index.html.
        path = self.translate_path(self.path)
        if not os.path.exists(path) and '.' not in os.path.basename(self.path):
            self.path = '/index.html'
        return super().send_head()

server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(BASE / 'dist')))
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f'http://127.0.0.1:{server.server_port}'

def route(r):
    url = r.request.url
    if not url.startswith(origin + '/'):
        external.append(url); r.abort(); return
    if '/api/' not in url: r.continue_(); return
    path = url.split('/api', 1)[1].split('?')[0]
    value = {}
    if path == '/session': value = {'csrfToken': 'fixture'}
    elif path == '/first-start/status': value = first_start(scenario['first_start'])
    elif path == '/state': value = scenario['state']
    elif path == '/harness/status': value = HARNESS[scenario['harness']]
    elif path in ('/trading/status', '/trading/bridge-login'): value = trading(scenario['trading'])
    elif path == '/guardian/status': value = GUARDIAN
    elif path == '/maintenance': value = MAINTENANCE
    r.fulfill(status=200, content_type='application/json', body=json.dumps(value))

def shoot(page, name, full=True):
    page.wait_for_timeout(250)
    page.screenshot(path=str(OUT / (name + '.png')), full_page=full)

def nav(page, label):
    # The Setup entry also carries its step counter in its accessible name.
    page.get_by_role('navigation', name='Main navigation').get_by_role('button', name=re.compile('^' + re.escape(label))).click()
    page.wait_for_timeout(300)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=CHROME)
    for tag, viewport, scale in (('1920', {'width': 1920, 'height': 1200}, 1.25), ('1280', {'width': 1280, 'height': 800}, 1.0)):
        context = browser.new_context(viewport=viewport, device_scale_factor=scale, reduced_motion='reduce')
        context.route('**/*', route); context.set_default_timeout(10000)
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
        scenario.update(first_start='pending', state=FRESH_STATE, harness='fresh', trading='fresh')
        page.goto(origin + '/'); page.get_by_role('heading', name='Before you begin').wait_for(); shoot(page, f'{tag}-00-first-start')
        scenario['first_start'] = 'complete'
        page.reload(); page.get_by_role('heading', name='Welcome to Decrypt OS').wait_for(); page.wait_for_timeout(400); shoot(page, f'{tag}-01-home-fresh')
        nav(page, 'Setup'); shoot(page, f'{tag}-02-setup-welcome')
        page.get_by_role('button', name='Name your assistant').click(); page.wait_for_timeout(300); shoot(page, f'{tag}-03-setup-assistant', full=False)
        nav(page, 'Connections'); shoot(page, f'{tag}-04-connections-fresh')
        page.get_by_label('Tradovate account name').fill('DEMO000000'); page.get_by_label('Tradovate account number').fill('123456')
        page.get_by_label('Tradovate account name').scroll_into_view_if_needed(); shoot(page, f'{tag}-16-bridge-form-filled', full=False)
        scenario['trading'] = 'setup'
        page.goto(origin + '/connections'); page.get_by_role('heading', name='Tradovate bridge').wait_for(); page.wait_for_timeout(600)
        page.get_by_role('heading', name='Tradovate bridge').scroll_into_view_if_needed(); shoot(page, f'{tag}-17-bridge-after-setup', full=False)
        scenario['trading'] = 'fresh'
        nav(page, 'Trading'); shoot(page, f'{tag}-05-trading-fresh')
        scenario.update(state=READY_STATE, harness='ready', trading='ready')
        page.goto(origin + '/'); page.get_by_role('heading', name='Welcome to Decrypt OS').wait_for(); page.wait_for_timeout(500); shoot(page, f'{tag}-06-home-ready')
        page.goto(origin + '/pipeline'); page.get_by_role('heading', name='Execution runtime').wait_for(); page.wait_for_timeout(500); shoot(page, f'{tag}-07-trading-ready')
        page.locator('[data-engine="fvg-ifvg"]').get_by_role('button', name='View FVG/IFVG customer execution engine', exact=True).click(); page.wait_for_timeout(300); shoot(page, f'{tag}-08-engine-coming-soon', full=False); page.keyboard.press('Escape')
        page.locator('[data-engine="soup"]').get_by_role('button', name='View ICT - Turtle Soup Shift engine', exact=True).click(); page.wait_for_timeout(300); shoot(page, f'{tag}-18-engine-soup', full=False)
        page.get_by_role('dialog').get_by_role('button', name='Install in Execution runtime').click(); page.wait_for_timeout(400); shoot(page, f'{tag}-19-trading-install-form', full=False)
        nav(page, 'Strategy access'); shoot(page, f'{tag}-09-strategy-access')
        nav(page, 'Connections'); page.wait_for_timeout(400); shoot(page, f'{tag}-10-connections-ready')
        nav(page, 'System care'); page.wait_for_timeout(400); shoot(page, f'{tag}-11-system-care')
        nav(page, 'Activity'); shoot(page, f'{tag}-12-activity')
        nav(page, 'Setup'); page.get_by_role('button', name='Trading setup').click(); page.wait_for_timeout(200); shoot(page, f'{tag}-13-setup-trading', full=False)
        page.get_by_role('button', name='Open Atlas help').click(); page.wait_for_timeout(300); shoot(page, f'{tag}-14-help', full=False); page.keyboard.press('Escape')
        # Keyboard: Tab from the skip link must land on visible nav controls.
        page.goto(origin + '/'); page.get_by_role('heading', name='Welcome to Decrypt OS').wait_for()
        for _ in range(3): page.keyboard.press('Tab')
        shoot(page, f'{tag}-15-keyboard-focus', full=False)
        overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
        if overflow: errors.append(f'{tag}: horizontal overflow on Home')
        context.close()
    browser.close()
server.shutdown()
print(json.dumps({'screens': sorted(p.name for p in OUT.glob('*.png')), 'errors': errors, 'externalRequests': external}, indent=2))
raise SystemExit(1 if errors or external else 0)
