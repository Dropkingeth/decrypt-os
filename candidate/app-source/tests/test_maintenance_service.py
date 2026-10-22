"""Real service/SQLite/core and loopback HTTP fixtures. No native/provider actions."""
import contextlib
import copy
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'app/backend'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from bridge_fixture import FakeBridge
from server import LabServer,Store
from guardian_service import GuardianService
from trading_service import TradingService,PAIR_ID
from maintenance_service import MaintenanceService,OperationGate
from maintenance import MaintenanceError

PASSPHRASE='synthetic-recovery-passphrase'
RID='11111111-2222-4333-8444-555555555555'
ACCOUNT={'nativeAccountId':12345,'nativeAccountName':'Fixture Account','accountId':'12345','symbol':'MNQZ2026'}

class ForbiddenBrowser:
    def bind_market_browser(self,browser):pass  # never a real customer browser here
    def __getattr__(self,name):raise AssertionError('No provider/browser action is permitted')

class ServiceFixtures(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.now=time.time();self.store=Store(self.root)
        self.guardian=GuardianService(self.root,browser=ForbiddenBrowser())
        self.guardian.vault.initialize('synthetic-guardian-vault-passphrase')
        # The Tradovate bridge as setup.py leaves it: fake GET /state, no socket.
        self.bridge=FakeBridge(self.root/'data',ACCOUNT,clock=lambda:int(self.now*1000))
        self.trading=TradingService(self.root,self.guardian,self.store,bridge_opener=self.bridge.opener)
        self.server=types.SimpleNamespace(store=self.store,guardian=self.guardian,trading=self.trading,
            operation_gate=OperationGate(),action_lock=threading.RLock())
        self.restarts=0
        self.service=MaintenanceService(self.server,clock=lambda:self.now,trust={},restart_factory=self.restart,
                                        bridge_opener=self.bridge.opener)
        self.server.maintenance=self.service
        self.network=patch.object(socket,'socket',side_effect=AssertionError('No network action is permitted'));self.network.start()
    def tearDown(self):
        for service in (self.server.trading,self.trading):
            if service is not None:
                with contextlib.suppress(Exception):service.close()
        for guardian in (self.server.guardian,self.guardian):
            if guardian is not None:
                with contextlib.suppress(Exception):guardian.close()
        self.network.stop();self.temp.cleanup()
    def native(self,**changes):
        """What the bridge's GET /state reports for the bound account."""
        self.bridge.state_override=changes
    def install(self):
        self.trading.action('install',{'provider':'tradovate','requestId':RID,'pairId':PAIR_ID,'streamId':'fixture-stream',
            'feedId':'fixture-feed','tp1LimitOffsetHandles':1.25,'nativeAccountId':12345,'nativeAccountName':'Fixture Account',
            'accountMode':'demo','symbol':'MNQZ2026','loginGroupId':'login-1','acknowledgeNativeSetup':True})
        return self.trading.runtime['engine_'+RID]
    def restart(self):
        self.restarts+=1
        self.server.guardian=GuardianService(self.root,browser=ForbiddenBrowser())
        self.server.trading=TradingService(self.root,self.server.guardian,self.store,bridge_opener=self.bridge.opener)
        for item in self.server.trading.manager.list():self.server.trading._open(item['binding']['instanceId'])
    def backup(self):return self.service.perform('backup',{'passphrase':PASSPHRASE})
    def refused(self,code,fn):
        with self.assertRaises(MaintenanceError) as caught:fn()
        self.assertEqual(caught.exception.code,code)
    def test_idle_backup_real_services_restart_held_and_locked(self):
        runtime=self.install();runtime.start();result=self.backup()
        self.assertTrue(runtime.closed);self.assertFalse(runtime.worker.is_alive());self.assertEqual(self.restarts,1)
        self.assertTrue(self.guardian.closed);self.assertFalse(self.server.guardian.vault.metadata()['unlocked'])
        current=self.server.trading.runtime['engine_'+RID];self.assertTrue(current.status()['entriesHeld'])
        self.assertTrue(result['result']['servicesRestartedHeld']);self.assertFalse(result['maintenance']['restartRequired'])
        self.assertIsNone(result['maintenance']['activeOperation'])
        self.assertNotIn(PASSPHRASE,json.dumps(result));self.assertNotIn('Fixture Account',json.dumps(result))
    def test_active_trade_backup_refuses_before_holding_or_stopping(self):
        runtime=self.install();runtime.engine.data['trade']={'phase':'entry_pending'}
        runtime.enabled=True;runtime.locked=False
        self.refused('maintenance_active_trade',self.backup)
        self.assertFalse(runtime.closed);self.assertFalse(runtime.stopping.is_set());self.assertTrue(runtime.enabled)
        self.assertFalse(self.trading.closed);self.assertFalse(self.guardian.closed)
    def test_unknown_started_ready_commands_refuse_without_shutdown(self):
        runtime=self.install()
        for status in ('ready','started','unknown'):
            runtime.engine.data['commands']={'fixture':{'status':status,'kind':'entry'}}
            self.refused('maintenance_unreconciled',self.backup)
            self.assertFalse(runtime.closed);self.assertFalse(self.guardian.closed)
    def test_closed_accepted_history_and_confirmed_cancel_do_not_block(self):
        runtime=self.install();runtime.engine.data['trade']={'id':'closed-fixture','phase':'closed'}
        runtime.engine.data['commands']={'cancel1':{'status':'accepted','kind':'cancel','trade_id':'closed-fixture'},'cancel2':{'status':'confirmed','kind':'cancel','trade_id':'closed-fixture'}}
        runtime.engine.save()
        self.assertTrue(self.backup()['result']['servicesRestartedHeld'])
    def test_confirmed_non_cancel_not_a_known_terminal_receipt(self):
        runtime=self.install();runtime.engine.data['trade']={'phase':'closed'}
        runtime.engine.data['commands']={'unexpected':{'status':'confirmed','kind':'entry'}}
        self.refused('maintenance_unreconciled',self.backup)
    def test_closed_historical_accepted_still_requires_native_flat(self):
        runtime=self.install();runtime.engine.data['trade']={'id':'closed-fixture','phase':'closed'};runtime.engine.data['commands']={'cancel':{'status':'accepted','kind':'cancel','trade_id':'closed-fixture'}}
        self.native(net_position=1)
        self.refused('entries_must_be_held_and_native_flat',self.backup);self.assertFalse(runtime.closed)
    def test_stale_unknown_wrong_account_and_hidden_working_order_refuse(self):
        self.install()
        cases=[({'observed_at':int(self.now*1000)-10001},'native_safety_stale'),({'synced':False},'native_safety_unknown'),
               ({'account_id':999},'native_safety_unknown'),({'open_order_ids':['1']},'entries_must_be_held_and_native_flat')]
        for data,code in cases:
            self.native(**data);self.refused(code,self.backup);self.assertFalse(self.trading.closed)
    def test_teardown_does_not_refresh_native_timestamp(self):
        self.install();original=self.trading.close
        def delayed():original();self.now+=11
        with patch.object(self.trading,'close',side_effect=delayed):self.refused('native_safety_stale',self.backup)
        self.assertEqual(len(self.service.manager.list_backups()),0)
    def test_missing_internal_fields_browser_cannot_supply_safety(self):
        self.refused('invalid_fields',lambda:self.service.perform('backup',{'passphrase':PASSPHRASE,'entriesHeld':True,'allNativeFlat':True}))
        self.assertFalse(self.trading.closed)
    def test_guardian_thread_not_stopped_prevents_snapshot(self):
        fake=types.SimpleNamespace(is_alive=lambda:True)
        with patch.object(self.guardian,'close'),patch.object(self.guardian,'worker',fake):
            self.refused('maintenance_workers_not_stopped',self.backup)
        self.assertEqual(self.service.manager.list_backups(),[])
        self.assertEqual(self.restarts,0);self.assertTrue(self.service.restart_required)
    def test_non_cancel_or_mismatched_closed_cancel_refuses(self):
        runtime=self.install();runtime.engine.data['trade']={'id':'closed-fixture','phase':'closed'}
        for command in ({'status':'accepted','kind':'entry','trade_id':'closed-fixture'},
                        {'status':'accepted','kind':'cancel','trade_id':'other-trade'},
                        {'status':'confirmed','kind':'cancel','trade_id':'other-trade'},
                        {'status':'accepted','kind':'cancel'}):
            runtime.engine.data['commands']={'unexpected':command}
            self.refused('maintenance_unreconciled',self.backup)
            self.assertFalse(runtime.closed);self.assertFalse(self.guardian.closed)
        runtime.engine.data['trade']=None;runtime.engine.data['commands']={'cancel':{'status':'accepted','kind':'cancel','trade_id':'closed-fixture'}}
        self.refused('maintenance_unreconciled',self.backup)
    def test_backup_restart_failure_remains_paused_and_reports_required_restart(self):
        self.install()
        def failed_restart():raise OSError('synthetic restart failure')
        self.service.restart_factory=failed_restart
        result=self.backup()
        self.assertTrue(result['maintenance']['restartRequired']);self.assertFalse(result['result']['servicesRestartedHeld'])
        self.assertIsNone(self.server.trading);self.assertIsNone(self.server.guardian)
    def test_no_unmanaged_ledger_adopted_as_empty_scope(self):
        orphan=self.root/'trading/orphan';orphan.mkdir(mode=0o700);(orphan/'engine.sqlite').write_bytes(b'fixture')
        self.refused('maintenance_unreconciled',self.backup)
    def test_backup_health_and_opt_in_ingress_vault_family(self):
        self.guardian.files.write('execution-ingress.json',{'fixture':'encrypted-synthetic'})
        result=self.service.perform('backup',{'passphrase':PASSPHRASE,'includeCredentials':True})
        stage=self.service.perform('restore/prepare',{'backupId':result['result']['backupId'],'passphrase':PASSPHRASE})
        receipt=json.loads((self.service.manager.root/'restores'/stage['result']['restoreId']/'RECEIPT.json').read_bytes())
        names={r['path'] for r in receipt['files']};self.assertIn('trading/health.sqlite',names);self.assertIn('guardian/execution-ingress.json',names)
        status=self.service.status();self.assertEqual(status['restores'][0]['restoreId'],stage['result']['restoreId'])
    def test_empty_scope_archive_restore_is_held_and_requires_restart(self):
        backup=self.backup();stage=self.service.perform('restore/prepare',{'backupId':backup['result']['backupId'],'passphrase':PASSPHRASE})
        result=self.service.perform('restore/apply',{'restoreId':stage['result']['restoreId']})
        self.assertTrue(result['maintenance']['restartRequired']);self.assertTrue(result['maintenance']['nativeReconciliationRequired'])
        self.assertIsNone(self.server.trading);self.assertIsNone(self.server.guardian)
        self.refused('maintenance_restart_required',self.backup)
    def test_offboarding_revokes_in_memory_vault_before_removal(self):
        self.assertTrue(self.guardian.vault.metadata()['unlocked'])
        result=self.service.perform('offboard',{'confirmation':'REMOVE SAVED SIGN-INS'})
        self.assertIsNone(self.guardian.vault.key);self.assertFalse((self.root/'guardian/vault.json').exists())
        self.assertTrue(result['result']['providerRevocationRequired']);self.assertTrue(result['maintenance']['restartRequired'])

class GateTests(unittest.TestCase):
    def test_exclusive_waits_for_delivery_and_prevents_new_writes(self):
        gate=OperationGate();first=threading.Event();release=threading.Event();exclusive=threading.Event();finish=threading.Event();last=threading.Event()
        def ordinary():
            with gate.ordinary():first.set();release.wait(2)
        def maintenance():
            with gate.exclusive():exclusive.set();finish.wait(2)
        def later():
            with gate.ordinary():last.set()
        threads=[threading.Thread(target=ordinary),threading.Thread(target=maintenance),threading.Thread(target=later)]
        threads[0].start();self.assertTrue(first.wait(1));threads[1].start()
        deadline=time.monotonic()+1
        while not gate.waiting and time.monotonic()<deadline:time.sleep(.001)
        threads[2].start();self.assertFalse(exclusive.wait(.03));self.assertFalse(last.is_set());release.set()
        self.assertTrue(exclusive.wait(1));self.assertFalse(last.is_set());finish.set()
        for thread in threads:thread.join(2);self.assertFalse(thread.is_alive())
        self.assertTrue(last.is_set())
    def test_nested_exclusive_and_owner_read_do_not_deadlock(self):
        gate=OperationGate()
        with gate.exclusive(),gate.exclusive(),gate.ordinary():self.assertEqual(gate.depth,2)
        self.assertIsNone(gate.owner)

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.server=LabServer(('127.0.0.1',0),Store(self.root))
        self.server.first_start.admitted=lambda:True  # first-start notice gate: tests/test_first_start.py
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        code,data,headers=self.request('GET','/api/session',headers={'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'});self.assertEqual(code,200)
        self.cookie=headers['Set-Cookie'].split(';')[0];self.csrf=data['csrfToken']
    def tearDown(self):self.server.shutdown();self.server.server_close();self.worker.join(2);self.temp.cleanup()
    def request(self,method,path,body=None,headers=None):
        client=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        data=json.dumps(body).encode() if body is not None else None
        base={'Content-Type':'application/json','Origin':'http://127.0.0.1:'+str(self.server.server_port)}
        if hasattr(self,'cookie'):base.update(Cookie=self.cookie,**{'X-CSRF-Token':self.csrf})
        base.update(headers or {})
        client.request(method,path,body=data,headers=base);response=client.getresponse();raw=response.read();result=json.loads(raw);response_headers=dict(response.getheaders());client.close();return response.status,result,response_headers
    def test_backup_prepare_apply_http_and_restart_boundary(self):
        code,status,_=self.request('GET','/api/maintenance');self.assertEqual(code,200);self.assertEqual(status['maintenance']['backupCount'],0)
        code,data,_=self.request('POST','/api/maintenance/backup',{'passphrase':PASSPHRASE,'includeCredentials':False});self.assertEqual(code,200,data)
        backup=data['result']['backupId'];self.assertNotIn(PASSPHRASE,json.dumps(data))
        code,data,_=self.request('POST','/api/maintenance/restore/prepare',{'backupId':backup,'passphrase':PASSPHRASE});self.assertEqual(code,200,data)
        restore=data['result']['restoreId'];self.assertEqual(data['maintenance']['restores'][0]['restoreId'],restore)
        code,data,_=self.request('POST','/api/maintenance/restore/apply',{'restoreId':restore});self.assertEqual(code,200,data);self.assertTrue(data['maintenance']['restartRequired'])
        code,data,_=self.request('POST','/api/onboarding',{});self.assertEqual(code,409);self.assertEqual(data['error']['code'],'maintenance_restart_required')
        code,data,_=self.request('GET','/api/maintenance');self.assertEqual(code,200);self.assertTrue(data['maintenance']['nativeReconciliationRequired'])
    def test_csrf_origin_body_limit_and_safety_injection_refused(self):
        for headers in ({'X-CSRF-Token':'bad'},{'Origin':'https://example.invalid'}):
            code,_,_=self.request('POST','/api/maintenance/backup',{'passphrase':PASSPHRASE},headers);self.assertEqual(code,403)
        code,_,_=self.request('POST','/api/maintenance/backup',{'passphrase':'a'*9000});self.assertEqual(code,413)
        code,data,_=self.request('POST','/api/maintenance/backup',{'passphrase':PASSPHRASE,'nativeSafety':{'allNativeFlat':True}});self.assertEqual(code,409);self.assertEqual(data['error']['code'],'invalid_fields')
        self.assertEqual(self.server.maintenance.status()['backupCount'],0)
    def test_no_arbitrary_path_or_release_bytes_route(self):
        code,_,_=self.request('POST','/api/maintenance/restore/apply',{'restoreId':'../../outside'});self.assertEqual(code,409)
        code,data,_=self.request('POST','/api/maintenance/release/stage',{'files':{}});self.assertEqual(code,409);self.assertEqual(data['error']['code'],'maintenance_action_unknown')
    def test_read_status_requires_same_local_session(self):
        code,_,_=self.request('GET','/api/maintenance',headers={'Cookie':'invalid=1'});self.assertEqual(code,403)

if __name__=='__main__':unittest.main()
