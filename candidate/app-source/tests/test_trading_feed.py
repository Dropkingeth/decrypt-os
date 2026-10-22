"""Worker lifecycle and freshness tests; no live browser or order transport."""
from pathlib import Path
import copy
from datetime import datetime, timezone
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app/backend'))
from guardian_auth import GuardianError
from trading_feed import FeedWorker

URL='https://www.tradingview.com/chart/AbCd1234/'


class Runtime:
    def __init__(self):
        self.binding={'feedProducerId':'fixture-feed'}
        self.now=1800000000000
        self.invalidations=[]
    def clock(self): return self.now
    def invalidate_market(self,reason,*,producer_id):
        assert producer_id=='fixture-feed'
        self.invalidations.append(reason)


class Browser:
    def __init__(self): self.opens=[]
    def open(self,url): self.opens.append((url,threading.get_ident())); return 'fixture-target'


class FakeFeed:
    instances=[]
    tick_entered=threading.Event()
    release_tick=threading.Event()
    result=None
    fail_install=False
    def __init__(self,runtime,browser,**kwargs):
        self.runtime=runtime
        self.calls=[('create',threading.get_ident())]
        self.closed=threading.Event()
        FakeFeed.instances.append(self)
    def install(self):
        self.calls.append(('install',threading.get_ident()))
        if self.fail_install: raise ValueError('secret provider diagnostics must not escape')
    def tick(self):
        self.calls.append(('tick',threading.get_ident()))
        self.tick_entered.set()
        self.release_tick.wait(2)
        return dict(self.result)
    def close(self):
        self.calls.append(('close',threading.get_ident())); self.closed.set()


class FeedLifecycle(unittest.TestCase):
    def setUp(self):
        FakeFeed.instances=[]
        FakeFeed.tick_entered=threading.Event()
        FakeFeed.release_tick=threading.Event()
        FakeFeed.fail_install=False
        FakeFeed.result={'state':'hold','reason':'native_acquisition_invalid','nativeVerified':False}
        self.runtime=Runtime(); self.browser=Browser()
        self.worker=FeedWorker(self.runtime,self.browser,URL)
        self.mock=patch('trading_feed.NativeMarketFeed',FakeFeed); self.mock.start()
        self.addCleanup(self.mock.stop)
        self.addCleanup(self.worker.close)
        self.addCleanup(FakeFeed.release_tick.set)

    def test_explicit_start_only_one_owner_and_loss_requires_manual_retry(self):
        self.assertEqual(self.browser.opens,[])
        self.worker.start()
        self.assertTrue(FakeFeed.tick_entered.wait(2))
        self.worker.start()
        self.assertEqual(len(self.browser.opens),1)
        FakeFeed.release_tick.set()
        self.worker.worker.join(2)
        self.assertFalse(self.worker.worker.is_alive())
        self.assertEqual(self.worker.status()['state'],'requires_retry')
        self.assertFalse(self.worker.status()['nativeVerified'])
        self.assertIn('native_acquisition_invalid',self.runtime.invalidations)
        self.assertEqual(len(FakeFeed.instances),1)
        calls=FakeFeed.instances[0].calls
        self.assertEqual(len({thread for name,thread in calls}),1)
        self.assertNotEqual(calls[0][1],threading.get_ident())
        self.assertEqual([name for name,thread in calls],['create','install','tick','close'])
        self.worker.start(); self.worker.worker.join(2)
        self.assertEqual(len(FakeFeed.instances),2)

    def test_install_failure_is_fixed_code_closes_and_does_not_loop(self):
        FakeFeed.fail_install=True
        self.worker.start(); self.worker.worker.join(2)
        self.assertEqual(self.worker.status(),{'state':'requires_retry','reason':'native_feed_unavailable',
                                              'nativeVerified':False,'observedAt':None})
        self.assertTrue(FakeFeed.instances[0].closed.is_set())
        self.assertEqual(len(self.browser.opens),1)
        self.assertEqual(self.runtime.invalidations,['native_feed_starting','native_feed_unavailable'])

    def test_status_uses_callback_time_not_last_poll_and_handles_clock_rollback(self):
        self.worker._record('fresh',None,True,self.runtime.now-1999)
        self.assertTrue(self.worker.status()['nativeVerified'])
        self.runtime.now+=2
        self.assertFalse(self.worker.status()['nativeVerified'])
        self.assertEqual(self.worker.status()['reason'],'no_fresh_native_callback')
        self.runtime.now-=3000
        self.assertFalse(self.worker.status()['nativeVerified'])

    def test_restart_cannot_promote_previous_callback_without_new_packet(self):
        FakeFeed.result={'state':'fresh','reason':None,'nativeVerified':True,'observedAt':self.runtime.now}
        self.worker.start(); self.assertTrue(FakeFeed.tick_entered.wait(2))
        FakeFeed.release_tick.set()
        # Closing wakes the worker; the result completed from the old timestamp
        # is still rejected before the next loop or shutdown.
        self.worker.stop_event.set(); self.worker.worker.join(2)
        self.assertIn('no_fresh_native_callback',self.runtime.invalidations)
        self.assertFalse(self.worker.status()['nativeVerified'])

    def test_close_is_idempotent_and_never_stops_browser_or_restarts(self):
        self.worker.close(); self.worker.close()
        self.assertEqual(self.runtime.invalidations,['native_feed_stopped'])
        self.assertEqual(self.browser.opens,[])
        self.assertEqual(self.worker.status()['state'],'closed')
        with self.assertRaises(GuardianError): self.worker.start()


class PackagedRuntimeIntegration(unittest.TestCase):
    def test_real_native_feed_store_and_runtime_admit_then_loss_without_order(self):
        import test_trading_runtime as fixture
        from decrypt_trading import RuntimeManager
        with tempfile.TemporaryDirectory() as temp, patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
            clock = [fixture.BASE]
            binding = fixture.binding('worker-fixture')
            manager = RuntimeManager(Path(temp)/'runtimes'); manager.install(binding)
            runtime = manager.open(binding['instanceId'], clock=lambda: clock[0])
            self.addCleanup(runtime.close)
            entered, release = threading.Event(), threading.Event()
            threads = []
            health = {'page_url':URL,'same_series':True,'chart_style':1,'series_symbol':'CME_MINI:MNQZ2026',
                      'timeframe':'1S','chart_ready':True,'chart_failed':False,'series_status':3,'is_delayed':False,
                      'replay_status':0,'stopped':False,'symbol_info':{'full_name':'CME_MINI:MNQZ2026','minmov':25,'pricescale':100}}
            class NativeBrowser:
                drains = 0
                def open(self,url):
                    self.url=url
                    return 'fixture-target'
                def targets(self): return [{'type':'page','url':self.url,'id':'fixture-target'}]
                def evaluate(self,target,expression):
                    threads.append(threading.get_ident())
                    if '.drain()' in expression:
                        self.drains += 1
                        if self.drains == 1:
                            clock[0] += 1
                            row = [clock[0]//1000,24000,24000.5,23999.75,24000.25,2]
                            packet = {**health,'event_kind':'data_updated','history':False,
                                      'callback_at':datetime.fromtimestamp(clock[0]/1000,timezone.utc).isoformat(),
                                      'bar':row,'rows':[row]}
                            return {'health':copy.deepcopy(health),'overflow':False,'packets':[packet]}
                        entered.set(); release.wait(2)
                        return None  # Reload removed the installed native hook.
                    return copy.deepcopy(health)
            browser = NativeBrowser(); worker = FeedWorker(runtime,browser,URL)
            self.addCleanup(worker.close); self.addCleanup(release.set)
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertTrue(worker.status()['nativeVerified'])
            runtime.step()
            self.assertEqual(runtime.engine.data['last_quote']['price'],24000.25)
            self.assertTrue(runtime.status()['entriesHeld'])
            self.assertEqual(runtime.engine.data['commands'],{})
            clock[0] += 2001
            self.assertFalse(worker.status()['nativeVerified'])
            release.set(); worker.worker.join(2)
            self.assertFalse(worker.worker.is_alive())
            self.assertEqual(worker.status()['state'],'requires_retry')
            self.assertFalse(runtime.status()['entryReady'])
            self.assertEqual(len(set(threads)),1)
            worker.close(); runtime.close()


if __name__=='__main__': unittest.main()
