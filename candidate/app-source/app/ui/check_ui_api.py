"""Browser + real customer API/SQLite + a loopback stand-in for Decrypt Labs.

Proves end to end that the Strategy access form sends only the typed username
(and contact) and that a failed send falls back to the manual route. Nothing
leaves 127.0.0.1.
"""
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from playwright.sync_api import sync_playwright

BASE=Path(__file__).resolve().parent
OUT=Path(os.environ.get('DECRYPT_UI_EVIDENCE_DIR',str(BASE/'ui-evidence')))
OUT.mkdir(exist_ok=True,parents=True)
sys.path.insert(0,str(BASE.parent/'backend'))
os.environ.update(no_proxy='*',NO_PROXY='*')
import server
import strategy_access
NOTICE=(BASE.parents[2]/'firstboot/NOTICE.txt').read_text()
FIRST_START={'schema':'decrypt.first-start-display.v1','status':'complete','complete':True,'promptRunning':False,'policySha256':'5bf99074c43e06acbd06023b425b1a218fdb00084f52fa5ae11fd56959dffb4b','documentSha256':'2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055','instanceId':'12345678-1234-4234-9234-123456789abc','noticeText':NOTICE,'source':'root-published-display','nativeAcceptanceEvidence':False}
results=[]
def check(label,value):
    assert value,label
    results.append(label)

class DecryptLabsStandIn(BaseHTTPRequestHandler):
    status=202
    seen=[]
    def log_message(self,*args):pass
    def do_POST(self):
        DecryptLabsStandIn.seen.append({'path':self.path,'body':json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
        self.send_response(DecryptLabsStandIn.status);self.send_header('Content-Length','0');self.end_headers()

labs=ThreadingHTTPServer(('127.0.0.1',0),DecryptLabsStandIn)
threading.Thread(target=labs.serve_forever,daemon=True).start()
class QuietUI(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass
ui=ThreadingHTTPServer(('127.0.0.1',18821),partial(QuietUI,directory=str(BASE/'dist')))
threading.Thread(target=ui.serve_forever,daemon=True).start()

with tempfile.TemporaryDirectory(prefix='ui-api-') as temp:
    config={'requestUrl':'','publicSite':'https://decryptlabs.io'}
    sender=strategy_access.HttpSender(f'http://127.0.0.1:{labs.server_port}/v1/strategy-access',timeout=5)
    store=server.Store(Path(temp)/'state',config,sender)
    api=server.LabServer(('127.0.0.1',0),store,18821)
    api.first_start.admitted=lambda:True
    threading.Thread(target=api.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=os.environ.get('DECRYPT_UI_CHROME') or None)
            context=browser.new_context(viewport={'width':1440,'height':1000});context.set_default_timeout(10000)
            def route(r):
                if '/api/first-start/status' in r.request.url:
                    r.fulfill(status=200,content_type='application/json',body=json.dumps(FIRST_START))
                elif '/api/' in r.request.url:
                    r.fulfill(response=r.fetch(url='http://127.0.0.1:'+str(api.server_port)+'/api/'+r.request.url.split('/api/',1)[1],headers={**r.request.headers,'host':'127.0.0.1:'+str(api.server_port)}))
                elif r.request.url.startswith('http://127.0.0.1:18821/'):r.continue_()
                else:r.abort()
            context.route('**/*',route)
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto('http://127.0.0.1:18821/')
            page.get_by_role('button',name='Strategy access',exact=True).click()
            page.get_by_role('heading',name='Request strategy access',exact=True).wait_for()
            page.get_by_label('TradingView username',exact=True).fill('UI_Fixture_Customer')
            page.get_by_role('button',name='Send to Decrypt Labs',exact=True).click()
            page.get_by_role('heading',name='Request sent',exact=True).wait_for()
            check('Decrypt Labs stand-in received the username only',DecryptLabsStandIn.seen==[{'path':'/v1/strategy-access','body':{'tradingViewUsername':'UI_Fixture_Customer'}}])
            check('Real API saved the sent state',store.state()['strategyAccess']['latest']['status']=='sent')
            DecryptLabsStandIn.status=404
            page.get_by_label('TradingView username',exact=True).fill('UI_Fixture_Other')
            page.get_by_role('button',name='Send to Decrypt Labs',exact=True).click()
            page.get_by_text('Your request did not reach Decrypt Labs',exact=True).wait_for()
            check('Refused send falls back to the manual route',page.locator('.manual-send code').inner_text()=='UI_Fixture_Other')
            page.screenshot(path=str(OUT/'real-api-not-sent.png'),full_page=True)
            check('No browser runtime error',not errors)
            context.close();browser.close()
    finally:
        api.shutdown();api.server_close()
ui.shutdown();labs.shutdown()
(OUT/'api-results.json').write_text(json.dumps({'kind':'real-browser-customer-api-sqlite','checks':results,'count':len(results),'externalRequests':'blocked'},indent=2)+'\n')
print(json.dumps({'passed':len(results)},indent=2))
