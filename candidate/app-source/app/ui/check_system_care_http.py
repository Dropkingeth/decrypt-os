"""Built System care UI -> real loopback API and encrypted local maintenance.

Disposable blank customer scope. No provider, credential, desktop notification,
engine, native account assertion, real release or external request is used.
Only the notification transport and post-backup service factory are injected.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import base64
import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'backend'))
from server import LabServer, Store
from guardian_service import GuardianService
from trading_service import TradingService
from customer_health import NativeNotifications

OUT = Path(os.environ['DECRYPT_UI_EVIDENCE_DIR']); OUT.mkdir(parents=True, exist_ok=True)
BUILD = Path(os.environ.get('DECRYPT_UI_BUILD', str(HERE / 'dist')))
# Pin a completed first-start display on the real API (as check_ui_api.py does).
NOTICE = (HERE.parents[2] / 'firstboot/NOTICE.txt').read_text()
FIRST_START = {'schema': 'decrypt.first-start-display.v1', 'status': 'complete', 'complete': True, 'promptRunning': False,
               'policySha256': '5bf99074c43e06acbd06023b425b1a218fdb00084f52fa5ae11fd56959dffb4b',
               'documentSha256': '2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055',
               'instanceId': '12345678-1234-4234-9234-123456789abc', 'noticeText': NOTICE,
               'source': 'root-published-display', 'nativeAcceptanceEvidence': False}
PASSPHRASE = 'FAKE-SYSTEM-CARE-ARCHIVE-PASSPHRASE'
WRONG = 'FAKE-INCORRECT-ARCHIVE-PASSPHRASE'
checks, errors, external, browser_calls, responses = [], [], [], [], []
SOURCE = HERE.parents[1]
checked_paths = {Path(__file__).resolve(), HERE/'src/SystemCare.tsx', HERE/'src/SystemCare.css'}
for folder in (SOURCE/'app/backend', SOURCE/'app/trading-runtime', BUILD):
    checked_paths.update(p for p in folder.rglob('*') if p.is_file() and p.suffix in ('.py','.js','.json','.css','.html','.woff2','.jpg'))
source_hashes = {str(p.relative_to(SOURCE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(checked_paths)}

def check(name, condition):
    assert condition, name
    checks.append(name)

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args): pass

class ForbiddenBrowser:
    """Every provider-browser call fails the check, except TradingService's constructor hook
    bind_market_browser, which only records the binding (no browser is started)."""
    def __init__(self): self.market_bound = None
    def bind_market_browser(self, browser): self.market_bound = browser
    def __getattr__(self, name):
        browser_calls.append(name)
        raise AssertionError('Provider browser forbidden')

class FixtureNotifications:
    def __init__(self): self.calls = []
    def send(self, event):
        self.calls.append(copy.deepcopy(event))
        return {'outcome': 'accepted', 'reason': 'desktop_daemon_accepted', 'nativeId': 17, 'userRead': False}

with tempfile.TemporaryDirectory(prefix='system-care-ui-http-') as temporary, patch.object(
        NativeNotifications, 'send', side_effect=AssertionError('Native notification forbidden')):
    static = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(BUILD)))
    threading.Thread(target=static.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{static.server_port}'
    store = Store(Path(temporary) / 'state')
    backend = LabServer(('127.0.0.1', 0), store, ui_port=static.server_port)
    backend.first_start.status = lambda: FIRST_START
    notifications = FixtureNotifications()
    def restart_fixture_services():
        backend.guardian = GuardianService(store.directory, browser=ForbiddenBrowser())
        backend.trading = TradingService(store.directory, backend.guardian, store, notification_transport=notifications)
    restart_fixture_services()
    backend.maintenance.restart_factory = restart_fixture_services
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    api_origin = f'http://127.0.0.1:{backend.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=os.environ.get('DECRYPT_UI_CHROME') or os.environ.get('CHROMIUM_EXECUTABLE') or None)
            context = browser.new_context(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
            context.set_default_timeout(10000)
            def route(request):
                if not request.request.url.startswith(origin + '/'):
                    external.append(request.request.url); request.abort()
                elif '/api/' in request.request.url:
                    response = request.fetch(url=api_origin + request.request.url[len(origin):])
                    responses.append(response.text())
                    request.fulfill(response=response)
                else: request.continue_()
            context.route('**/*', route)
            page = context.new_page(); page.on('pageerror', lambda value: errors.append(str(value)))
            page.goto(origin)
            page.get_by_role('button', name='System care', exact=True).click()
            panel = page.locator('.system-care')
            expect(panel.get_by_role('heading', name='System care', exact=True)).to_be_visible()
            check('Built full app mounts dedicated System care navigation', page.get_by_role('button', name='System care', exact=True).get_attribute('aria-current') == 'page')
            expect(panel.get_by_text('No installed engine has a calendar check yet.', exact=True)).to_be_visible()
            check('Blank state has no notification or trading setup', backend.trading.status()['notifications']['enabled'] is False and not backend.trading.manager.list())
            check('Blank development state does not claim an installed release', panel.get_by_text('Development build', exact=True).is_visible() and not backend.maintenance.status()['installedBaselineConfigured'])
            panel.get_by_role('button', name='Enable desktop alerts', exact=True).click()
            expect(panel.get_by_role('button', name='Disable desktop alerts', exact=True)).to_be_visible()
            check('Explicit local notification opt-in reaches real API', backend.trading.status()['notifications']['enabled'] is True)
            check('Unconfigured customer sends no warning', not notifications.calls and not backend.trading.customer_health.status()['events'])
            # Synthetic internal monitor transition only. No account is configured.
            healthy = {'config': {'enabled': True}, 'monitor': {'state': 'monitoring_ready', 'lastObservedAt': int(time.time()), 'validUntil': time.time()+12}}
            backend.trading.customer_health.observe(guardian=healthy, trading={'instances': []})
            backend.trading.customer_health.observe(guardian={**healthy, 'monitor': {'state': 'unknown'}}, trading={'instances': []})
            backend.trading.customer_health.dispatch_one()
            panel.get_by_role('button', name='Refresh system care', exact=True).click()
            panel.locator('summary').filter(has_text='Delivery receipts').click()
            expect(panel.get_by_text('Accepted by desktop · read status unknown', exact=True)).to_be_visible()
            check('Real persisted delivery receipt remains user-read unknown', len(notifications.calls) == 1 and backend.trading.customer_health.status()['userRead'] is False)

            panel.locator('summary').filter(has_text='Create encrypted backup').click()
            pass_input = panel.get_by_label('New archive passphrase', exact=True)
            check('Archive passphrase is masked', pass_input.get_attribute('type') == 'password')
            check('Saved credentials excluded by default', not panel.get_by_label('Include saved encrypted credentials.', exact=False).is_checked())
            pass_input.fill(PASSPHRASE)
            panel.get_by_label('Confirm archive passphrase', exact=True).fill(PASSPHRASE)
            panel.get_by_role('button', name='Create encrypted backup', exact=True).click()
            expect(panel.get_by_text('Encrypted archive created locally.', exact=False)).to_be_visible()
            check('Passphrases clear on real submission', pass_input.input_value() == '' and panel.get_by_label('Confirm archive passphrase', exact=True).input_value() == '')
            state = backend.maintenance.status(); backup_id = state['backups'][0]['backupId']
            raw = backend.maintenance.manager.export_backup(backup_id); archive = json.loads(raw)
            check('Actual archive is AES-GCM encrypted envelope with ciphertext', archive['schema'] == 'decrypt.encrypted-backup.v1' and len(base64.b64decode(archive['ciphertext'])) > 16 and 'files' not in archive)
            check('Archive contains no passphrase or plaintext SQLite header', PASSPHRASE.encode() not in raw and b'SQLite format 3' not in raw)
            check('Backup restarts real services held without initialized credential vault', not state['restartRequired'] and backend.trading.status()['notifications']['enabled'] and not backend.guardian.vault.metadata()['configured'])
            panel.locator('summary').filter(has_text='Restore saved records').click()
            panel.get_by_label('Saved archive', exact=True).select_option(backup_id)
            panel.get_by_label('Restore archive passphrase', exact=True).fill(WRONG)
            panel.get_by_role('button', name='Prepare restore', exact=True).click()
            expect(panel.get_by_role('alert').filter(has_text='could not be unlocked')).to_be_visible()
            check('Wrong archive passphrase stages no restore', not backend.maintenance.status()['restores'])
            check('Failed secret is absent from visible error', WRONG not in panel.inner_text())
            panel.get_by_role('button', name='Refresh system care', exact=True).click()
            expect(panel.locator('summary').filter(has_text='Restore saved records')).to_be_visible()
            panel.locator('summary').filter(has_text='Restore saved records').click()
            check('Failed restore passphrase never repopulates', panel.get_by_label('Restore archive passphrase', exact=True).input_value() == '')
            panel.get_by_label('Saved archive', exact=True).select_option(backup_id)
            panel.get_by_label('Restore archive passphrase', exact=True).fill(PASSPHRASE)
            panel.get_by_role('button', name='Prepare restore', exact=True).click()
            expect(panel.get_by_text('Archive checked and staged.', exact=False)).to_be_visible()
            restore_id = backend.maintenance.status()['restores'][0]['restoreId']
            check('Successful decrypt stages verified known restore ID without applying', len(restore_id) == 32 and not backend.maintenance.status()['nativeReconciliationRequired'])
            check('Restore passphrase cleared after successful real staging', panel.get_by_label('Restore archive passphrase', exact=True).input_value() == '')
            panel.get_by_label('Verified staged restore', exact=True).select_option(restore_id)
            expect(panel.get_by_role('button', name='Apply staged restore', exact=True)).to_be_disabled()
            check('Unknown paths cannot be entered through ID-only selectors', panel.get_by_label('Saved archive', exact=True).evaluate('el=>el.tagName') == 'SELECT')

            def post(path, body, csrf=True):
                return page.evaluate('''async ({path,body,csrf})=>{const session=await(await fetch('/api/session')).json();const headers={'Content-Type':'application/json'};if(csrf)headers['X-CSRF-Token']=session.csrfToken;const r=await fetch(path,{method:'POST',headers,body:JSON.stringify(body)});return {status:r.status,data:await r.json()};}''', {'path': path, 'body': body, 'csrf': csrf})
            check('Maintenance API still rejects missing CSRF', post('/api/maintenance/restore/apply', {'restoreId': restore_id}, False)['status'] == 403)
            check('Real API rejects arbitrary restore path', post('/api/maintenance/restore/apply', {'restoreId': '../../outside'})['status'] == 409)
            check('Browser cannot supply native safety assertions', post('/api/maintenance/backup', {'passphrase': PASSPHRASE, 'allNativeFlat': True})['status'] == 409)
            panel.get_by_label('I want to apply these staged records and keep trading held for reconciliation.', exact=True).check()
            panel.get_by_role('button', name='Apply staged restore', exact=True).click()
            expect(panel.get_by_text('Restore applied.', exact=False)).to_be_visible()
            state = backend.maintenance.status()
            check('Real blank-scope restore requires restart and retains reconciliation hold', state['restartRequired'] and state['nativeReconciliationRequired'] and (store.directory/'maintenance/hold.json').exists())
            check('Restore closes actual Guardian and trading services', backend.guardian is None and backend.trading is None)
            check('Post-restore mutation remains refused', post('/api/trading/configure-notifications', {'enabled': False})['data']['error']['code'] == 'maintenance_restart_required')
            expect(panel.get_by_text('A restart alone does not clear this hold.', exact=False)).to_be_visible()
            expect(panel.get_by_role('button', name='Check restored records', exact=True)).to_be_disabled()
            check('Reconciliation cannot run before app restart', post('/api/maintenance/restore/reconcile', {})['data']['error']['code'] == 'maintenance_restart_required')
            # Real HTTP/service object restart on the same temporary files. This
            # is not an OS restart or native provider acceptance claim.
            backend.shutdown(); backend.server_close()
            backend = LabServer(('127.0.0.1', 0), store, ui_port=static.server_port)
            backend.first_start.status = lambda: FIRST_START
            restart_fixture_services(); backend.maintenance.restart_factory = restart_fixture_services
            threading.Thread(target=backend.serve_forever, daemon=True).start()
            api_origin = f'http://127.0.0.1:{backend.server_port}'
            page.reload()
            page.get_by_role('button', name='System care', exact=True).click()
            expect(panel.get_by_label('Check these restored records without enabling entries or replaying orders.', exact=True)).to_be_visible()
            check('Actual API restart exposes only supported recorded restore reconciliation', backend.maintenance.status()['reconciliation']['available'] is True and not backend.maintenance.status()['restartRequired'])
            panel.get_by_label('Check these restored records without enabling entries or replaying orders.', exact=True).check()
            panel.get_by_role('button', name='Check restored records', exact=True).click()
            expect(panel.get_by_text('Restored records verified.', exact=False)).to_be_visible()
            state = backend.maintenance.status()
            check('Explicit blank-scope reconciliation makes no broker claim and remains held for restart', state['reconciliation']['state'] == 'reconciled' and state['restartRequired'] and not state['nativeReconciliationRequired'] and backend.trading is None)
            check('No engine was installed or queued by reconciliation', not list((store.directory/'trading').glob('engine_*/engine.sqlite')))
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(OUT/'real-http-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 1000})
            check('System care full app fits narrow screen', page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(OUT/'real-http-narrow.png'), full_page=True)
            check('Secrets absent from API response bodies', all(secret not in body for body in responses for secret in (PASSPHRASE, WRONG)))
            check('Secrets absent from browser storage', page.evaluate('JSON.stringify({...localStorage,...sessionStorage})') == '{}')
            files = [p.read_bytes() for p in store.directory.rglob('*') if p.is_file()]
            check('Passphrases absent from all persisted files including stages/receipts', all(secret.encode() not in raw for raw in files for secret in (PASSPHRASE, WRONG)))
            check('No native provider browser method called', not browser_calls)
            check('No external request attempted', not external)
            check('No browser exception', not errors)
            browser.close()
    finally:
        backend.shutdown(); backend.server_close(); static.shutdown(); static.server_close()

check('Checked source and built assets did not change during smoke', all(hashlib.sha256((SOURCE/name).read_bytes()).hexdigest()==digest for name,digest in source_hashes.items()))
(OUT/'SOURCE-HASHES.json').write_text(json.dumps(source_hashes,sort_keys=True,indent=2)+'\n')
(OUT/'results.json').write_text(json.dumps({'kind':'built-full-app-real-loopback-api-encrypted-maintenance', 'passed':len(checks), 'checks':checks, 'providerContacted':False, 'notifications':'injected fake transport only', 'scope':'blank disposable customer; no native account assertion', 'state':'temporary and removed'},indent=2)+'\n')
print(json.dumps({'passed':len(checks),'evidence':str(OUT)}))
