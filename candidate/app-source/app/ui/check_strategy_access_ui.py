"""Rendered strategy-access fixtures across Home, Setup and Strategy access.

No real request leaves the browser: every /api call is answered by fixtures and
every non-local URL is blocked. Checks that no system registration remains and
that only the typed username (and optional contact) is posted.
"""
from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import threading

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[4]
BUILD=Path(os.environ.get('DECRYPT_UI_BUILD',str(Path(__file__).parent/'dist')))
OUT=Path(os.environ.get('DECRYPT_UI_EVIDENCE_DIR',str(Path(__file__).parent/'ui-evidence')))
OUT.mkdir(exist_ok=True,parents=True)
NOTICE=(ROOT/'candidate/firstboot/NOTICE.txt').read_text()
FIRST_START={'schema':'decrypt.first-start-display.v1','status':'complete','complete':True,'promptRunning':False,'policySha256':'5bf99074c43e06acbd06023b425b1a218fdb00084f52fa5ae11fd56959dffb4b','documentSha256':'2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055','instanceId':'12345678-1234-4234-9234-123456789abc','noticeText':NOTICE,'source':'root-published-display','nativeAcceptanceEvidence':False}
BASE={'schema':'decrypt.os.lab.v1','mode':'lab','onboarding':{'version':2,'step':'access','completed':['welcome','assistant','bot','connections'],'tradingPreferences':True,'preferencesComplete':False},
      'profile':{'assistantName':'Atlas','tradingViewUsername':'Access_UI_Fixture'},'connections':{},'usage':{'ai':{'status':'unavailable','used':None,'limit':None},'account':{'status':'unavailable','balance':None,'currency':None}},
      'bots':[],'activity':[],'strategyAccess':{'tradingViewUsername':'Access_UI_Fixture','requestConfigured':True,'strategyFieldSupported':False,'requestableStrategies':['soup','fvg-ifvg'],'publicSite':'https://decryptlabs.io','latest':None},
      'engineWorkspace':{'schema':'decrypt.engine-workspace.v1','setupOnly':True,'catalog':json.loads((Path(__file__).parent.parent/'engine-module-catalog.json').read_text())}}
state=deepcopy(BASE)
posts,checks,errors,external=[],[],[],[]

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass
    def send_head(self):
        if not os.path.exists(self.translate_path(self.path)) and '.' not in os.path.basename(self.path):self.path='/index.html'
        return super().send_head()

server=ThreadingHTTPServer(('127.0.0.1',0),partial(QuietHandler,directory=str(BUILD)))
threading.Thread(target=server.serve_forever,daemon=True).start()
origin=f'http://127.0.0.1:{server.server_port}'

def check(name,value):
    assert value,name
    checks.append(name)

def route(r):
    if not r.request.url.startswith(origin+'/'):
        external.append(r.request.url);r.abort();return
    if '/api/' not in r.request.url:r.continue_();return
    path=r.request.url.split('/api',1)[1]
    body=r.request.post_data_json if r.request.method=='POST' else None
    if body is not None:posts.append((path,body))
    if path=='/session':value={'csrfToken':'strategy-access-ui-fixture'}
    elif path=='/first-start/status':value=FIRST_START
    elif path=='/state':value=state
    elif path=='/strategy-access/request':
        state['strategyAccess']['latest']={'tradingViewUsername':body['tradingViewUsername'],'contact':body.get('contact',''),'status':'sent','reason':None,'strategies':body.get('strategies',[]),'createdAt':'2021-10-11T12:00:00Z','sentAt':'2021-10-11T12:00:01Z'}
        state['strategyAccess']['tradingViewUsername']=body['tradingViewUsername']
        value=state
    else:value={}
    r.fulfill(status=200,content_type='application/json',body=json.dumps(value))

REGISTRATION=('registration not connected','must register this system','registered system','not registered','register your','registration')

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path=os.environ.get('DECRYPT_UI_CHROME') or None)
    context=browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='reduce')
    context.route('**/*',route);context.set_default_timeout(10000)
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def no_registration(where):
        text=page.locator('main').inner_text().lower()
        check(where+' has no system-registration wording',not any(phrase in text for phrase in REGISTRATION))

    page.goto(origin+'/')
    page.get_by_role('heading',name='Right now',exact=True).wait_for()
    status=page.locator('.workspace-status')
    check('Right now shows the saved username without a registration state',status.get_by_text('Access_UI_Fixture',exact=True).is_visible())
    check('Right now shows the access request as not sent yet',status.get_by_text('Not sent yet',exact=True).is_visible())
    no_registration('Home')
    page.screenshot(path=str(OUT/'home-access-not-sent.png'),full_page=True)

    page.get_by_role('navigation',name='Main navigation').get_by_role('button').filter(has_text='Setup').click()
    page.locator('.setup-panel').wait_for()
    check('Setup access step uses the plain grant explanation',page.get_by_text('Enter your TradingView username. Decrypt Labs grants the invite-only strategy to that username in TradingView.',exact=False).first.is_visible())
    no_registration('Setup access step')
    for step in ('Start here','Trading setup','Review and finish'):
        page.locator('.step-rail').get_by_role('button').filter(has_text=step).first.click()
        no_registration('Setup '+step)
        if step=='Trading setup':
            strip=page.locator('.setup-panel .status-list');text=strip.inner_text().lower()
            check('Setup strategy strip follows the catalog engine status, no coming-soon wording','engine live' in text and 'engines on hold' not in text and 'on hold' in text and 'in development' in text and 'parked' in text and 'coming soon' not in text)
            strip.screenshot(path=str(OUT/'setup-strategies-strip.png'))
    page.get_by_role('button',name='Open Atlas help',exact=True).click();page.locator('dialog[open]').wait_for()
    page.get_by_role('dialog').get_by_role('button',name='Which engines can I run?',exact=True).click()
    answer=page.locator('dialog[open] .answer').inner_text().lower()
    check('Help answer derives engine status from the catalog, no coming-soon wording','live' in answer and 'parked' in answer and 'in development' in answer and 'coming soon' not in answer)
    page.locator('dialog[open] .answer').screenshot(path=str(OUT/'help-which-engines.png'))
    page.keyboard.press('Escape');page.locator('dialog[open]').wait_for(state='detached')

    page.get_by_role('button',name='Strategy access',exact=True).click()
    page.get_by_role('heading',name='Request strategy access',exact=True).wait_for()
    no_registration('Strategy access')
    check('Saved username is prefilled',page.get_by_label('TradingView username',exact=True).input_value()=='Access_UI_Fixture')
    # The strategy picker: a fieldset of labelled checkboxes, each with the engine status label and a TradingView link.
    picker=page.locator('fieldset.strategy-pick');rows=picker.locator('.strategy-pick-row')
    check('Strategy picker is a fieldset with a legend',picker.count()==1 and picker.locator('legend').inner_text().startswith('Which strategies?'))
    check('Picker lists only strategies that are listed, requestable and have a TradingView page',rows.count()==5)
    check('Each picker row carries the engine status label',rows.filter(has_text='Live').count()==1 and rows.filter(has_text='On hold').count()==1 and rows.filter(has_text='Parked').count()==1 and rows.filter(has_text='In development').count()==2)
    links=picker.locator('a[href*="tradingview.com/script/"]')
    check('Each picker row links to its TradingView page in a new tab, isolated',links.count()==5 and all(a.get_attribute('target')=='_blank' and a.get_attribute('rel')=='noopener noreferrer' for a in links.all()))
    check('Picker checkboxes are named by the strategy',page.get_by_role('checkbox',name='ICT - Turtle Soup Shift',exact=True).count()==1)
    page.get_by_label('TradingView username',exact=True).focus();page.keyboard.press('Tab')
    check('Tab from the username reaches the first strategy checkbox',page.evaluate("document.activeElement.type==='checkbox'"))
    picker.screenshot(path=str(OUT/'access-picker-focus.png'))
    check('Honest note while the strategy field is off',page.get_by_text('they are not sent yet',exact=False).count()==1)
    page.screenshot(path=str(OUT/'access-picker-desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    check('Picker at 390px has no horizontal overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
    picker.screenshot(path=str(OUT/'access-picker-narrow.png'))
    page.set_viewport_size({'width':1440,'height':1000})
    page.get_by_label('ICT - Turtle Soup Shift',exact=True).check()
    page.get_by_role('button',name='Send to Decrypt Labs',exact=True).click()
    page.get_by_role('heading',name='Request sent',exact=True).wait_for()
    check('Request body is the username plus the picked strategy',[item for item in posts if item[0]=='/strategy-access/request']
          ==[('/strategy-access/request',{'tradingViewUsername':'Access_UI_Fixture','strategies':['soup']})])
    check('No other call carries the username',not any('Access_UI_Fixture' in json.dumps(body) for path,body in posts if path!='/strategy-access/request'))
    check('The sent page says which strategy the request covers',page.get_by_text('This request covers ICT - Turtle Soup Shift.',exact=True).is_visible())
    check('Unsupported flag shows a manual copy block for the picked strategy',page.locator('.manual-send').get_by_text('ICT - Turtle Soup Shift',exact=False).is_visible())
    check('That block says the username went but the strategies did not',page.locator('.manual-send').get_by_text('Your username was sent, but the strategies you picked were not.',exact=False).is_visible())
    page.locator('.request-state').screenshot(path=str(OUT/'access-sent-strategies-not-sent.png'))

    page.get_by_role('button',name='Home',exact=True).click()
    page.get_by_role('heading',name='Right now',exact=True).wait_for()
    check('Right now reports the username as sent',page.locator('.workspace-status').get_by_text('Sent to Decrypt Labs',exact=True).is_visible())
    page.screenshot(path=str(OUT/'home-access-sent.png'),full_page=True)

    page.get_by_role('button',name='Trading',exact=True).click()
    page.get_by_role('heading',name='Engines',exact=True).wait_for()
    no_registration('Trading')
    check('Soup install is not blocked by registration',page.get_by_text('Register this system first',exact=False).count()==0)
    page.locator('.engine-list').screenshot(path=str(OUT/'trading-engines-desktop.png'))
    page.locator('[data-engine="ote-silver-bullet"]').get_by_role('button',name='View ICT - Silver Bullet OTE engine',exact=True).click()
    dialog=page.get_by_role('dialog');dialog.get_by_text("Parked. This engine can't be installed or trade in this release.",exact=True).wait_for()
    check('Parked engine dialog shows the Parked label and a TradingView link',dialog.locator('.pill',has_text='Parked').count()==1 and dialog.locator('a[href*="tradingview.com/script/"]').count()==1)
    check('Engine dialog shows the catalog alert setup for decrypt-bridge-v2',dialog.get_by_text('TradingView alert setup',exact=True).count()==1 and dialog.get_by_text('decrypt-bridge-v2',exact=True).count()==1)
    dialog.screenshot(path=str(OUT/'trading-engine-dialog-parked.png'))
    page.keyboard.press('Escape');page.locator('dialog[open]').wait_for(state='detached')
    page.set_viewport_size({'width':390,'height':844})
    check('Engines at 390px have no horizontal overflow',page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
    page.locator('.engine-list').screenshot(path=str(OUT/'trading-engines-narrow.png'))
    page.set_viewport_size({'width':1440,'height':1000})

    check('No external requests',not external)
    check('No browser errors',not errors)
    context.close();browser.close()
server.shutdown()
(OUT/'strategy-access-results.json').write_text(json.dumps({'kind':'strategy-access-ui-fixtures','checks':checks,'count':len(checks),'errors':errors,'external':external},indent=2)+'\n')
print(json.dumps({'passed':len(checks),'evidence':str(OUT)},indent=2))
