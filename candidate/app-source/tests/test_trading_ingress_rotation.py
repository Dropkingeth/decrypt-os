"""Encrypted generation rotation with real held runtime and replay ledger."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

import test_trading_service as fixture
import test_trading_runtime as engine_fixture
from trading_service import TradingError
from guardian_auth import GuardianError
from trading_ingress_secrets import IngressSecrets
from decrypt_trading import SignalIngress, RuntimeError as EngineError


class FakeListener:
    public_origin = 'https://fixture.invalid'
    def close(self):pass


class RotationTests(unittest.TestCase):
    def setUp(self):
        fixture.ServiceTests.setUp(self)
        self.service.action('install',self.data)
        self.runtime=self.service.runtime[self.instance_id]
        self.payload=copy.deepcopy(engine_fixture.ENTRY)
        decision=(self.runtime.clock()//5000)*5000
        self.payload['timestamp']=decision-5000
        self.payload['execution_context'].update(decision_time=decision,entry_bar_open=decision-5000)
        self.service.ingress=FakeListener()
        self.old=self.service.action('create-signal-key',{'instanceId':self.instance_id,'acknowledgeOneTimeSecret':True})['oneTimeIngress']['url'].rsplit('/',1)[1]

    def rotate(self,generation=1):
        return self.service.action('rotate-signal-key',{'instanceId':self.instance_id,'expectedGeneration':generation,'acknowledgeOneTimeSecret':True})

    def saved(self):
        return self.service.action('show-signal-address',{'instanceId':self.instance_id,'acknowledgeOneTimeSecret':True})['oneTimeIngress']

    def raw(self):return json.dumps(self.payload).encode()

    def test_rotation_revokes_old_token_without_resetting_replay_or_engine(self):
        ingress=SignalIngress(self.runtime,self.service.ingress_secrets.lease)
        first=ingress.accept(self.raw(),presented_secret=self.old);self.runtime.step()
        before=json.dumps(self.runtime.engine.data,sort_keys=True)
        result=self.rotate();new=result['oneTimeIngress']['url'].rsplit('/',1)[1]
        self.assertNotEqual(new,self.old);self.assertEqual(result['oneTimeIngress']['generation'],2)
        with self.assertRaises(EngineError):ingress.accept(self.raw(),presented_secret=self.old)
        again=ingress.accept(self.raw(),presented_secret=new)
        self.assertEqual(again['receipt'],first['receipt'])
        self.assertEqual(json.dumps(self.runtime.engine.data,sort_keys=True),before)
        self.assertEqual(self.runtime.engine.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
        normal=json.dumps(self.service.status())
        self.assertNotIn(self.old,normal);self.assertNotIn(new,normal)
        dump='\n'.join(self.runtime.engine.db.iterdump())
        self.assertNotIn(self.old,dump);self.assertNotIn(new,dump)
        self.assertNotIn(new,json.dumps(self.guardian.vault.files.read('execution-ingress.json')))

    def test_lost_response_is_recovered_by_show_not_a_second_rotation(self):
        self.rotate()
        with self.assertRaisesRegex(GuardianError,'ingress_generation_changed') as conflict:self.rotate()
        self.assertEqual(conflict.exception.status,409)
        self.assertIn('Refresh status',conflict.exception.message)
        current=self.saved();self.assertEqual(current['generation'],2)
        self.assertNotEqual(current['url'].rsplit('/',1)[1],self.old)
        self.assertEqual(self.saved(),current)

    def test_locked_invalid_generation_or_unheld_rotation_refuses(self):
        for generation in (0,True,'1',2):
            with self.assertRaises(GuardianError):self.rotate(generation)
        self.runtime.set_controls(enabled=True,locked=False)
        with self.assertRaises(TradingError):self.rotate()
        self.runtime.hold();self.guardian.vault.lock()
        with self.assertRaises(GuardianError):self.rotate()
        self.guardian.vault.unlock('FAKE-VAULT-FOR-ISOLATED-SERVICE-TEST')
        self.assertEqual(self.saved()['generation'],1)

    def test_pending_input_refuses_without_drain_or_key_change(self):
        self.runtime.ingest('pine',self.raw(),producer_id=self.runtime.binding['signalProducerId'])
        with self.assertRaisesRegex(TradingError,'module_settled_required'):self.rotate()
        self.assertEqual(self.runtime.engine.db.execute("SELECT count(*) FROM inbox WHERE status='pending'").fetchone()[0],1)
        self.assertEqual(self.saved()['url'].rsplit('/',1)[1],self.old)

    def test_generation_and_current_secret_survive_vault_reopen(self):
        current=self.rotate()['oneTimeIngress'];self.guardian.vault.lock()
        self.guardian.vault.unlock('FAKE-VAULT-FOR-ISOLATED-SERVICE-TEST')
        other=IngressSecrets(self.guardian.vault)
        self.assertEqual(other.metadata()['producers'][0]['generation'],2)
        with other.lease(self.instance_id,'pine')as value:
            self.assertEqual(value,current['url'].rsplit('/',1)[1])

    def test_unknown_trade_refuses_rotation_without_resend(self):
        h=engine_fixture.RuntimeTests(methodName='runTest');h.setUp();self.addCleanup(h.doCleanups)
        h.transport.outcome='timeout';h.enter();h.runtime.hold()
        self.service.manager=h.manager;self.service.runtime={h.config['instanceId']:h.runtime};self.instance_id=h.config['instanceId']
        self.service.ingress_secrets.create(self.instance_id,'pine')
        before=len(h.transport.sent)
        with self.assertRaisesRegex(TradingError,'module_settled_required'):self.rotate()
        self.assertEqual(len(h.transport.sent),before)
        self.assertEqual(self.saved()['generation'],1)

    def test_legacy_ciphertext_has_generation_one_without_read_rewrite(self):
        values=self.service.ingress_secrets._load()
        values[self.instance_id+':pine'].pop('generation')
        self.service.ingress_secrets._save(values)
        before=self.guardian.vault.files.read('execution-ingress.json')
        self.assertEqual(self.saved()['generation'],1)
        self.assertEqual(self.guardian.vault.files.read('execution-ingress.json'),before)
        self.assertEqual(self.rotate()['oneTimeIngress']['generation'],2)

    def test_failed_write_retains_old_key_and_does_not_expose_exception(self):
        with patch.object(self.service.ingress_secrets,'_save',side_effect=OSError('private diagnostic must not escape')):
            with self.assertRaisesRegex(GuardianError,'ingress_rotation_storage_uncertain')as error:self.rotate()
        self.assertNotIn('private',str(error.exception))
        self.assertEqual(self.saved()['url'].rsplit('/',1)[1],self.old)

    def test_commit_then_error_is_not_retried_or_rolled_back(self):
        original=self.service.ingress_secrets._save
        def save_then_fail(values):original(values);raise OSError('fixture fsync error')
        with patch.object(self.service.ingress_secrets,'_save',side_effect=save_then_fail):
            with self.assertRaisesRegex(GuardianError,'ingress_rotation_storage_uncertain'):self.rotate()
        self.assertEqual(self.saved()['generation'],2)
        with self.assertRaisesRegex(GuardianError,'ingress_generation_changed'):self.rotate()

    def test_admission_and_rotation_serialize_runtime_before_vault_without_deadlock(self):
        started=threading.Event();done=threading.Event();results=[]
        def rotate():
            started.set()
            try:results.append(self.rotate()['oneTimeIngress']['generation'])
            except Exception as exc:results.append(type(exc).__name__)
            finally:done.set()
        with self.runtime.mutex:
            worker=threading.Thread(target=rotate);worker.start()
            self.assertTrue(started.wait(1))
            SignalIngress(self.runtime,self.service.ingress_secrets.lease).accept(self.raw(),presented_secret=self.old)
            self.runtime.step()
        self.assertTrue(done.wait(3));worker.join(1)
        self.assertEqual(results,[2])


if __name__=='__main__':unittest.main()
