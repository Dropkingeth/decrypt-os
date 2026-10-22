"""Real service/registry retirement; no provider/browser/order transport."""
import json
import unittest
from unittest.mock import patch
import test_trading_service as fixture
from trading_service import TradingService, TradingError


class RetirementServiceTests(unittest.TestCase):
    setUp = fixture.ServiceTests.setUp
    install = fixture.ServiceTests.install

    def retire(self):
        return self.service.action('retire', {'instanceId':self.instance_id, 'acknowledgeRetirement':True})

    def test_unused_retirement_reactivation_retains_ledger_without_opening(self):
        self.install()
        old=self.service.runtime[self.instance_id]
        result=self.retire()
        self.assertTrue(old.closed)
        self.assertEqual(result['instances'], [])
        self.assertEqual(result['retiredInstances'][0]['retirementBasis'],'never_dispatched_idle')
        path=old.directory/'engine.sqlite';before=path.read_bytes()
        result=self.service.action('reactivate', {'instanceId':self.instance_id,'acknowledgeReactivation':True})
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(result['retiredInstances'])
        self.assertEqual(result['instances'][0]['runtimeState'],'stopped')
        self.assertFalse(self.service.runtime)
        self.assertTrue(result['instances'][0]['entriesHeld'])

    def test_retired_alert_target_and_restart_remain_absent(self):
        self.install();self.retire();self.service.close()
        other=TradingService(self.root,self.guardian,self.store,bridge_opener=self.bridge.opener);self.addCleanup(other.close)
        other.start()
        self.assertEqual(other.runtime,{})
        self.assertEqual(len(other.status()['retiredInstances']),1)
        with self.assertRaises(TradingError):other.action('start-held',{'instanceId':self.instance_id})

    def test_new_instance_can_use_retired_account_without_reusing_identity(self):
        self.install();self.retire()
        request='21111111-2222-4333-8444-555555555555'
        result=self.service.action('install',{**self.data,'requestId':request})
        self.assertEqual(len(result['instances']),1)
        self.assertEqual(len(result['retiredInstances']),1)
        with self.assertRaises(TradingError):
            self.service.action('reactivate',{'instanceId':self.instance_id,'acknowledgeReactivation':True})
        self.assertEqual(len(self.service.status()['retiredInstances']),1)

    def test_retired_identity_refuses_before_any_secret_write(self):
        self.install();self.retire()
        with self.assertRaisesRegex(TradingError,'binding_immutable'):self.install()

    def test_refused_precheck_does_not_close_feed_or_worker(self):
        self.install();runtime=self.service.runtime[self.instance_id]
        from decrypt_trading import Conflict
        class Feed:
            closed=False
            def close(self):self.closed=True
            def status(self):return {'state':'fixture','nativeVerified':False}
        feed=Feed();self.service.feeds[self.instance_id]=feed
        with patch.object(self.service.manager,'retirement_precheck',side_effect=Conflict('active trade')):
            with self.assertRaises(TradingError):self.retire()
        self.assertFalse(feed.closed)
        self.assertFalse(runtime.closed)
        self.assertTrue(runtime.status()['entriesHeld'])

    def test_retirement_requires_explicit_ack_and_restore_blocks_reactivation(self):
        self.install()
        with self.assertRaises(TradingError):self.service.action('retire',{'instanceId':self.instance_id,'acknowledgeRetirement':False})
        self.retire();marker=self.root/'maintenance/hold.json';marker.parent.mkdir(mode=0o700);marker.write_text('{}')
        with self.assertRaises(TradingError):self.service.action('reactivate',{'instanceId':self.instance_id,'acknowledgeReactivation':True})
        self.assertEqual(len(self.service.manager.list(include_retired=True)),1)


if __name__=='__main__':unittest.main()
