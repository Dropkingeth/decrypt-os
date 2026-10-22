"""Isolated rendered UI checks. Explicit API fixtures; never contacts providers.

Serves the built `dist` folder itself on 127.0.0.1:18821 with the same single-page
fallback as installer/frontend.py (unknown paths return index.html).
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import threading
from playwright.sync_api import sync_playwright, expect

class Quiet(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass
    def send_head(self):
        if not os.path.exists(self.translate_path(self.path)) and '.' not in os.path.basename(self.path):self.path='/index.html'
        return super().send_head()
server=ThreadingHTTPServer(('127.0.0.1',18821),partial(Quiet,directory=str(Path(__file__).parent/'dist')))
threading.Thread(target=server.serve_forever,daemon=True).start()

ROOT=Path(__file__).resolve().parents[4]
OUT=Path(os.environ.get('DECRYPT_UI_EVIDENCE_DIR',str(Path(__file__).parent/'ui-evidence')))
OUT.mkdir(exist_ok=True,parents=True)
NOTICE=(ROOT/'candidate/firstboot/NOTICE.txt').read_text()
FIRST_START={'schema':'decrypt.first-start-display.v1','status':'complete','complete':True,'promptRunning':False,'policySha256':'5bf99074c43e06acbd06023b425b1a218fdb00084f52fa5ae11fd56959dffb4b','documentSha256':'2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055','instanceId':'12345678-1234-4234-9234-123456789abc','noticeText':NOTICE,'source':'root-published-display','nativeAcceptanceEvidence':False}
CATALOG=json.loads((Path(__file__).parent.parent/'engine-module-catalog.json').read_text())
state={'schema':'decrypt.os.lab.v1','mode':'lab','onboarding':{'version':2,'step':'access','completed':['welcome','assistant','bot','connections'],'tradingPreferences':True,'preferencesComplete':False},'profile':{'assistantName':'Atlas'},'connections':{},'usage':{'ai':{'status':'unavailable','used':None,'limit':None},'account':{'status':'unavailable','balance':None,'currency':None}},'bots':[],'activity':[],'strategyAccess':{'tradingViewUsername':'','requestConfigured':True,'publicSite':'https://decryptlabs.io','latest':None},
 'engineWorkspace':{'schema':'decrypt.engine-workspace.v1','setupOnly':True,'catalog':CATALOG}}
send_result={'status':'sent','reason':None}
posts=[]
results=[]
errors=[]
def check(name,condition):
    assert condition,name
    results.append(name)
def route(r):
    path=r.request.url.split('/api')[-1]
    if '/api/' not in r.request.url:
        if not r.request.url.startswith('http://127.0.0.1:18821/'):
            r.abort();return
        r.continue_();return
    body=r.request.post_data_json if r.request.method=='POST' else None
    if body is not None:posts.append((path,body))
    if path=='/session':value={'csrfToken':'explicit-ui-fixture'}
    elif path=='/first-start/status':value=FIRST_START
    elif path=='/state':value=state
    elif path=='/harness/status':value={'enabled':False,'status':'not_started','runtime':'OpenClaw','provider':'Claude','credentialPersistence':'session_only','gatewayPort':0,'model':None,'loginRunning':False,'consoleRunning':False,'scopeUpdatedAt':None,'error':None}
    elif path=='/strategy-access/request':
        sent=send_result['status']=='sent'
        state['strategyAccess']['tradingViewUsername']=body['tradingViewUsername']
        state['strategyAccess']['latest']={'tradingViewUsername':body['tradingViewUsername'],'contact':body.get('contact',''),'status':send_result['status'],'reason':send_result['reason'],'createdAt':'2021-10-11T12:00:00Z','sentAt':'2021-10-11T12:00:01Z' if sent else None}
        value=state
    else:value={}
    r.fulfill(status=200,content_type='application/json',body=json.dumps(value))

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('DECRYPT_UI_CHROME') or None)
    context=browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='reduce')
    context.route('**/*',route)
    context.set_default_timeout(10000)
    page=context.new_page()
    page.on('pageerror',lambda e:errors.append(str(e)))
    def open_access():
        page.get_by_role('button',name='Strategy access',exact=True).click()
        page.get_by_role('heading',name='Request strategy access',exact=True).wait_for()
    page.goto('http://127.0.0.1:18821/')
    page.get_by_role('button',name='Trading',exact=True).click()
    page.get_by_role('heading',name='Engines',exact=True).wait_for()
    check('All seven catalog engines visible',page.locator('.engine-list li').count()==7)
    pills=page.locator('.engine-list .row-actions .pill')
    # The row label is the engine status from strategy-catalog.json (Live / On hold / In development / Parked);
    # install availability is the "Engine" fact on each row.
    check('Row labels carry the engine status: Soup live, FVG on hold, Silver parked, Lumi, OTE Refined L and S and Order Blocks in development',pills.filter(has_text='Live').count()==1 and pills.filter(has_text='On hold').count()==1 and pills.filter(has_text='Parked').count()==1 and pills.filter(has_text='In development').count()==4)
    facts=page.locator('.engine-list .facts')
    check('Only Soup is installable; the others say so on the row',facts.filter(has_text='Available in Execution runtime').count()==1 and facts.filter(has_text='Not installable in this release').count()==6 and facts.filter(has_text='Not included in this release').count()==0)
    check('TradingView links exist only where the catalog has a script page, open a new tab, and are isolated',page.locator('.engine-list a[href*="tradingview.com/script/"]').count()==5 and all(a.get_attribute('target')=='_blank' and a.get_attribute('rel')=='noopener noreferrer' for a in page.locator('.engine-list a[href*="tradingview.com"]').all()))
    check('Parked engine is listed with its label, not as coming soon',page.locator('[data-engine="ote-silver-bullet"] .row-actions .pill').inner_text().strip().lower()=='parked')
    page.locator('[data-engine="fvg-ifvg"]').get_by_role('button',name='View FVG/IFVG customer execution engine',exact=True).click()
    page.get_by_role('dialog').get_by_text('Coming soon. This engine cannot be installed in this release.',exact=True).wait_for()
    check('Coming-soon engine offers no install action',page.get_by_role('dialog').get_by_role('button',name='Install in Execution runtime',exact=True).count()==0)
    check('Strategy access names who grants it',page.get_by_role('dialog').get_by_text('Decrypt Labs grants the invite-only TradingView script to your username',exact=False).count()==1)
    page.keyboard.press('Escape')
    expect(page.locator('dialog[open]')).to_have_count(0)
    open_access()
    check('Access page states the TradingView username grant plainly',page.get_by_text('Enter your TradingView username, pick which strategies you want, and send. Decrypt Labs grants the invite-only strategy to that username in TradingView. Then add the strategy to your chart and create the alert as shown.',exact=True).is_visible())
    check('No registration or consent wording remains',page.get_by_text('regist',exact=False).count()==0 and page.get_by_text('consent',exact=False).count()==0)
    check('The strategy picker is optional and starts with nothing checked',page.get_by_role('checkbox').count()>=1 and page.locator('input[type="checkbox"]:checked').count()==0)
    check('Username is required to send',page.get_by_role('button',name='Send to Decrypt Labs',exact=True).is_disabled())
    page.screenshot(path=str(OUT/'access-empty-desktop.png'),full_page=True)
    page.get_by_label('TradingView username',exact=True).fill('fixture_customer_22')
    page.get_by_role('button',name='Send to Decrypt Labs',exact=True).click()
    page.get_by_role('heading',name='Request sent',exact=True).wait_for()
    check('Only the username reaches the API',posts[-1]==('/strategy-access/request',{'tradingViewUsername':'fixture_customer_22'}))
    check('Sent state names the invite-only scripts menu',page.locator('.request-state').get_by_text('Indicators → Invite-only scripts',exact=True).is_visible())
    page.screenshot(path=str(OUT/'access-sent-desktop.png'),full_page=True)
    page.get_by_label('Contact',exact=False).fill('fixture contact')
    page.get_by_role('button',name='Send to Decrypt Labs',exact=True).click()
    page.get_by_role('heading',name='Request sent',exact=True).wait_for()
    check('Optional contact is sent only when typed',posts[-1]==('/strategy-access/request',{'tradingViewUsername':'fixture_customer_22','contact':'fixture contact'}))
    send_result.update(status='not_sent',reason='refused')
    page.get_by_role('button',name='Send to Decrypt Labs',exact=True).click()
    page.get_by_text('Your request did not reach Decrypt Labs',exact=True).wait_for()
    check('Failed send shows the username to send by hand',page.locator('.manual-send code').inner_text()=='fixture_customer_22')
    check('Manual route is the public site only',page.locator('.manual-send a').get_attribute('href')=='https://decryptlabs.io' and page.locator('.manual-send a').get_attribute('rel')=='noopener noreferrer')
    check('Copy button offered',page.locator('.manual-send').get_by_role('button',name='Copy',exact=True).count()==1)
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(OUT/'access-not-sent-narrow.png'),full_page=True)
    check('Narrow content has no horizontal overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
    page.set_viewport_size({'width':320,'height':900})
    check('320px content has no horizontal overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
    page.set_viewport_size({'width':1440,'height':1000})
    check('TradingView link is official and isolated',page.locator('.tv-launch a').first.get_attribute('href')=='https://www.tradingview.com/chart/' and page.locator('.tv-launch a').first.get_attribute('rel')=='noopener noreferrer')
    check('No live trading action',page.get_by_role('button',name='Start trading').count()==0)
    state['strategyAccess'].update(requestConfigured=False,latest=None)
    page.reload();open_access()
    page.get_by_label('TradingView username',exact=True).fill('fixture_customer_22')
    check('Sending turned off still shows the manual route before any send',page.locator('.manual-send code').inner_text()=='fixture_customer_22')
    page.screenshot(path=str(OUT/'access-unconfigured-desktop.png'),full_page=True)
    page.get_by_role('button',name='Connections',exact=True).click()
    page.get_by_role('heading',name='TradingView',exact=True).wait_for()
    check('Connections offers TradingView login',page.get_by_role('link',name='Open TradingView to sign in').count()==1)
    check('Connections explains the Tradovate bridge',page.get_by_role('heading',name='Tradovate bridge',exact=True).count()==1)
    page.get_by_role('button',name='Open Atlas help',exact=True).click()
    page.locator('dialog[open]').wait_for()
    page.keyboard.press('Escape')
    check('Help dialog closes with Escape',page.locator('dialog[open]').count()==0)
    check('No browser errors',not errors)
    context.close();browser.close()
server.shutdown()

(OUT/'results.json').write_text(json.dumps({'kind':'isolated-rendered-ui-fixtures','checks':results,'count':len(results),'errors':errors,'externalRequests':'blocked','providerGrant':'fixture only; no real provider accessed'},indent=2)+'\n')
print(json.dumps({'passed':len(results),'evidence':str(OUT)},indent=2))
