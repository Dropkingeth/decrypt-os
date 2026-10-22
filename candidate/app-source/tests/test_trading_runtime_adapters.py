"""Isolated customer input/native-source adapters; no provider session is used."""
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, timezone
import copy
import hashlib
import hmac
import http.client
import io
import json
import re
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_trading_runtime import binding, ENTRY, BASE, RuntimeManager, RuntimeError, Conflict
from decrypt_trading.ingress import SignalIngress, SignedIngress
from decrypt_trading.http_ingress import IngressListener
from decrypt_trading.native_market import NativeMarketFeed

TOKEN = 'test-only-unguessable-token-' + 'x'*32
KEY = b'test-only-signing-key-not-provider-' + b'x'*32


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = BASE
        self.config = binding()
        self.manager = RuntimeManager(Path(self.temp.name)/'runtime')
        self.manager.install(self.config)
        self.runtime = self.manager.open(self.config['instanceId'], clock=lambda:self.now)
        self.addCleanup(lambda:self.runtime.close())
        self.signal = SignalIngress(self.runtime, self.token_lease)
        self.signed = SignedIngress(self.runtime, self.key_lease)

    @contextmanager
    def token_lease(self, instance, channel):
        self.assertEqual((instance,channel),(self.config['instanceId'],'pine'))
        yield TOKEN

    @contextmanager
    def key_lease(self, instance, channel):
        self.assertEqual(instance,self.config['instanceId'])
        yield KEY

    def raw(self, value):
        return json.dumps(value, separators=(',',':')).encode()

    def signed_args(self, payload, seq=1, channel='quote'):
        raw=self.raw(payload)
        meta={'schema':'decrypt.ingress.v1','instanceId':self.config['instanceId'],'channel':channel,
              'feedId':None if channel=='pine' else self.config['feedId'],'eventId':payload.get('event_id',payload.get('trade_id')),
              'sequence':seq,'sentAt':self.now}
        signature=hmac.new(KEY,SignedIngress.signing_bytes(meta,raw),hashlib.sha256).hexdigest()
        return raw,{'metadata':meta,'signature':signature}

    def quote(self, event='quote-one'):
        return {'event_id':event,'symbol':self.config['symbol'],'time':self.now,'price':24000.}

    def test_raw_pine_held_is_durable_once_not_rearmed(self):
        first=self.signal.accept(self.raw(ENTRY),presented_secret=TOKEN)
        self.assertTrue(first['held'])
        self.runtime.set_controls(enabled=True,locked=False)
        second=self.signal.accept(self.raw(ENTRY),presented_secret=TOKEN)
        self.assertEqual(first['receipt'],second['receipt'])
        self.assertTrue(second['duplicate'])
        self.runtime.step()
        self.assertIsNone(self.runtime.engine.data['trade'])

    def test_wrong_secret_or_secret_body_cannot_create_inbox(self):
        for token in ('x'*64,'short'):
            with self.assertRaises(RuntimeError):self.signal.accept(self.raw(ENTRY),presented_secret=token)
        bad=copy.deepcopy(ENTRY);bad['secret']=TOKEN
        with self.assertRaises(ValueError):self.signal.accept(self.raw(bad),presented_secret=TOKEN)
        self.assertEqual(self.runtime.engine.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)

    def test_signal_identity_digest_conflict(self):
        self.signal.accept(self.raw(ENTRY),presented_secret=TOKEN)
        changed=copy.deepcopy(ENTRY);changed['entry_price']+=.25
        with self.assertRaises(Conflict):self.signal.accept(self.raw(changed),presented_secret=TOKEN)

    def test_signed_quote_exactly_once_and_persists_cursor(self):
        raw,args=self.signed_args(self.quote())
        first=self.signed.accept('quote',raw,**args)
        self.assertTrue(self.signed.accept('quote',raw,**args)['duplicate'])
        self.runtime.step();self.runtime.close()
        self.runtime=self.manager.open(self.config['instanceId'],clock=lambda:self.now)
        self.signed=SignedIngress(self.runtime,self.key_lease)
        self.assertEqual(self.signed.accept('quote',raw,**args)['receipt'],first['receipt'])
        self.now+=1
        raw,args=self.signed_args(self.quote('second'),seq=1)
        with self.assertRaises(Conflict):self.signed.accept('quote',raw,**args)
        raw,args=self.signed_args(self.quote('second'),seq=2)
        self.assertFalse(self.signed.accept('quote',raw,**args)['duplicate'])

    def test_signed_body_clock_channel_and_route_tampering_rejected(self):
        raw,args=self.signed_args(self.quote())
        for altered in (raw.replace(b'24000.0',b'24001.0'),raw+b' '):
            with self.assertRaises(RuntimeError):self.signed.accept('quote',altered,**args)
        for key,value in [('feedId','wrong'),('instanceId','wrong'),('sentAt',self.now+1),('channel','bar'),('sequence',True)]:
            bad=copy.deepcopy(args);bad['metadata'][key]=value
            with self.assertRaises(RuntimeError):self.signed.accept('quote',raw,**bad)

    def test_duplicate_json_keys_and_stale_quotes_never_persist(self):
        raw,args=self.signed_args(self.quote())
        raw=raw.replace(b'"price":24000.0',b'"price":24000.0,"price":25000')
        args['signature']=hmac.new(KEY,SignedIngress.signing_bytes(args['metadata'],raw),hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError):self.signed.accept('quote',raw,**args)
        old=self.quote();old['time']-=2001
        raw,args=self.signed_args(old)
        with self.assertRaises(RuntimeError):self.signed.accept('quote',raw,**args)

    def test_ingress_secrets_not_persisted(self):
        self.signal.accept(self.raw(ENTRY),presented_secret=TOKEN)
        self.runtime.engine.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        for path in self.runtime.directory.iterdir():
            if path.is_file():
                self.assertNotIn(TOKEN.encode(),path.read_bytes())
                self.assertNotIn(KEY,path.read_bytes())

    def http_server(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        listener=IngressListener(lambda name:self.runtime if name==self.config['instanceId'] else None,
            self.token_lease,'https://customer.example.invalid',port=port)
        listener.start();self.addCleanup(listener.close)
        return listener,port

    def http_request(self,port,path=None,headers=None,body=None):
        default={'Host':'customer.example.invalid','X-Forwarded-Host':'customer.example.invalid',
                 'X-Forwarded-Proto':'https','Content-Type':'application/json'}
        default.update(headers or {})
        client=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
        try:
            client.request('POST',path or '/signal/'+self.config['instanceId']+'/'+TOKEN,
                body=body if body is not None else self.raw(ENTRY),headers=default)
            response=client.getresponse()
            return response.status,json.loads(response.read())
        finally:client.close()

    def test_real_loopback_listener_accepts_bounded_exact_signal_route(self):
        _,port=self.http_server()
        code,result=self.http_request(port)
        self.assertEqual(code,202);self.assertTrue(result['held'])
        self.assertEqual(self.http_request(port)[1]['duplicate'],True)

    def test_http_proxy_origin_and_browser_requests_rejected(self):
        _,port=self.http_server()
        for headers in ({'Host':'evil.invalid'},{'X-Forwarded-Host':'evil.invalid'},
                        {'X-Forwarded-Proto':'http'},{'Origin':'https://customer.example.invalid'},
                        {'Content-Type':'text/plain'}):
            self.assertEqual(self.http_request(port,headers=headers)[0],400)
        for path in ('/api/trading/control','/signal/'+self.config['instanceId']+'/'+TOKEN+'?x=1'):
            self.assertEqual(self.http_request(port,path=path)[0],400)

    def test_http_failures_never_log_token_or_body(self):
        output=io.StringIO()
        with redirect_stderr(output):
            _,port=self.http_server()
            code,result=self.http_request(port,body=b'{"bad":')
            self.assertEqual(code,403)
            self.http_request(port,path='/signal/'+self.config['instanceId']+'/'+('z'*64))
        self.assertNotIn(TOKEN,output.getvalue());self.assertNotIn('bad',output.getvalue())
        self.assertNotIn(TOKEN,json.dumps(result))

    def test_listener_refuses_public_bind_and_non_https_origin(self):
        for options in ({'host':'0.0.0.0'}, {'public_origin':'http://customer.example.invalid'}, {'public_origin':'https://customer.example.invalid/path'}):
            kwargs={'public_origin':'https://customer.example.invalid'};kwargs.update(options)
            with self.assertRaises(RuntimeError):IngressListener(lambda _:None,self.token_lease,**kwargs)

    def test_two_thread_runtime_vault_lock_order_for_both_ingress_modes(self):
        for mode in ('signal', 'signed'):
            with self.subTest(mode=mode):
                real = threading.RLock(); vault = threading.RLock()
                held, waiting, dispatched = threading.Event(), threading.Event(), threading.Event()
                errors = []
                class TracedMutex:
                    def __enter__(inner):
                        if threading.current_thread().name == 'fixture-ingress':
                            waiting.set()
                        real.acquire()
                        return inner
                    def __exit__(inner, *args): real.release()
                self.runtime.mutex = TracedMutex()
                @contextmanager
                def lease(instance, channel):
                    with vault:
                        yield TOKEN if mode == 'signal' else KEY
                def dispatch():
                    try:
                        with self.runtime.mutex:
                            held.set()
                            if not waiting.wait(2): raise AssertionError('ingress never reached runtime lock')
                            with vault: dispatched.set()
                    except BaseException as exc: errors.append(exc)
                def receive():
                    try:
                        if not held.wait(2): raise AssertionError('dispatch never held runtime lock')
                        if mode == 'signal':
                            SignalIngress(self.runtime, lease).accept(self.raw(ENTRY), presented_secret=TOKEN)
                        else:
                            raw, args = self.signed_args(self.quote())
                            SignedIngress(self.runtime, lease).accept('quote', raw, **args)
                    except BaseException as exc: errors.append(exc)
                a = threading.Thread(target=dispatch, daemon=True, name='fixture-dispatch')
                b = threading.Thread(target=receive, daemon=True, name='fixture-ingress')
                a.start(); b.start(); a.join(3); b.join(3)
                self.assertFalse(a.is_alive() or b.is_alive(), 'runtime/vault lock inversion')
                self.assertTrue(dispatched.is_set()); self.assertEqual(errors, [])


class FakeBrowser:
    url='https://www.tradingview.com/chart/Test1234/'
    target='fixture-target-1234'
    def __init__(self,clock):
        self.clock=clock;self.packets=[];self.bad=False;self.installs=[]
    def targets(self):return [{'type':'page','id':self.target,'url':self.url}]
    def health(self):
        return {'page_url':self.url,'same_series':True,'chart_style':1,'series_symbol':'CME_MINI:MNQZ2026',
                'timeframe':'1S','chart_ready':True,'chart_failed':False,'series_status':3,'is_delayed':self.bad,
                'replay_status':0,'stopped':False,'symbol_info':{'full_name':'CME_MINI:MNQZ2026','minmov':25,'pricescale':100}}
    def evaluate(self,target,script):
        if '.drain()' in script:
            rows,self.packets=self.packets,[]
            return {'health':self.health(),'overflow':False,'packets':rows}
        if 'const chartUrl' in script:self.installs.append(script)
        return self.health()
    def packet(self,price=24000.):
        now=self.clock();stamp=now//1000
        return {**self.health(),'event_kind':'data_updated','history':False,
                'callback_at':datetime.fromtimestamp(now/1000,timezone.utc).isoformat(),
                'bar':[stamp,price,price,price,price,1],
                'rows':[[stamp-1,price,price,price,price,1],[stamp,price,price,price,price,1]]}


class NativeFeedTests(unittest.TestCase):
    setUp = AdapterTests.setUp
    token_lease = AdapterTests.token_lease
    key_lease = AdapterTests.key_lease
    def feed(self):
        browser=FakeBrowser(lambda:self.now)
        feed=NativeMarketFeed(self.runtime,browser,chart_url=browser.url,target_id=browser.target)
        feed.install();self.addCleanup(feed.close)
        return feed,browser
    def test_native_callback_is_quote_not_polling_snapshot(self):
        feed,browser=self.feed()
        self.assertEqual(feed.tick()['state'],'hold')
        self.assertIsNone(self.runtime.engine.data['last_quote'])
        browser.packets=[browser.packet()]
        result=feed.tick();self.assertEqual(result['state'],'fresh');self.assertEqual(result['acceptedInputs'],1)
        self.runtime.step();self.assertEqual(self.runtime.engine.data['last_quote']['time'],self.now)
        self.now+=2001
        self.assertEqual(feed.tick()['state'],'hold')
        self.assertEqual(self.runtime.engine.data['last_quote']['time'],BASE)
    def test_chart_identity_must_be_exact_customer_target(self):
        browser=FakeBrowser(lambda:self.now)
        feed=NativeMarketFeed(self.runtime,browser,chart_url=browser.url,target_id='wrong-target-123')
        with self.assertRaises(RuntimeError):feed.install()
        self.assertEqual(browser.installs,[])
    def test_delayed_replay_or_history_never_produces_quote(self):
        feed,browser=self.feed()
        for key,value in [('is_delayed',True),('replay_status',1),('history',True),('same_series',False)]:
            packet=browser.packet();packet[key]=value;browser.packets=[packet]
            self.assertEqual(feed.tick()['state'],'hold')
        self.assertEqual(self.runtime.engine.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
    def test_hook_contains_only_explicit_customer_chart(self):
        _,browser=self.feed();script=browser.installs[0]
        self.assertIn(browser.url,script)
        # The only chart layout the hook may name is the customer's own URL.
        self.assertEqual(set(re.findall(r'tradingview\.com/chart/([A-Za-z0-9_-]+)',script)),{browser.url.rstrip('/').rsplit('/',1)[1]})
        self.assertNotIn('__CUSTOMER_',script)
        self.assertIn('queue.length >= 128',script)

    def test_native_thirty_aggregation_requires_all_six_contiguous_five_bars(self):
        feed,browser=self.feed();end=(self.now//30000+1)*30000
        for i in range(6):
            a=end-30000+i*5000
            bar={'event_id':'tvbar:MNQZ2026:5:'+str(a),'symbol':'MNQZ2026','open_time':a,'close_time':a+5000,
                 'open':24000.,'high':24002.+i*.25,'low':23999.,'close':24001.}
            feed.store.db.execute('INSERT INTO finalized VALUES(?,?,?)',(bar['event_id'],json.dumps(bar),bar['close_time']))
        feed.store.db.commit()
        result=feed._thirty_bar(end);self.assertEqual(result['high'],24003.25);self.assertEqual(result['close_time']-result['open_time'],30000)
        feed.store.db.execute('DELETE FROM finalized WHERE close_time=?',(end-10000,));feed.store.db.commit()
        self.assertIsNone(feed._thirty_bar(end),'missing native candle must not be synthesized')


if __name__=='__main__':unittest.main()
