"""Outage, supersession and bounded local receipt tests. No desktop delivery."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app/backend'))
from customer_health import CustomerHealth, NativeNotifications


class Transport:
    def __init__(self):
        self.calls=[]
        self.result={'outcome':'accepted','reason':'desktop_daemon_accepted','nativeId':123,'userRead':False}
    def send(self,event):
        self.calls.append(copy.deepcopy(event))
        return dict(self.result)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now=1800000000
        self.transport=Transport()
        self.health=CustomerHealth(self.temp.name,self.transport,lambda:self.now)
        self.guardian={'config':{'enabled':True},'monitor':{'state':'monitoring_ready','lastObservedAt':self.now,'validUntil':self.now+12}}
        self.trading={'instances':[],'ingress':{'state':'not_configured'}}
        self.observe()

    def observe(self): return self.health.observe(guardian=self.guardian,trading=self.trading)
    def outage(self):
        self.guardian['monitor']['state']='unknown'
        return self.observe()
    def recover(self):
        self.guardian['monitor'].update(state='monitoring_ready',lastObservedAt=self.now,validUntil=self.now+12)
        return self.observe()

    def test_unconfigured_and_initial_unknown_do_not_notify(self):
        self.assertEqual(self.health.status()['events'],[])
        other=CustomerHealth(Path(self.temp.name)/'new',self.transport,lambda:self.now)
        for guardian in ({'config':None},{'config':{'enabled':True},'monitor':{'state':'unknown'}}):
            other.observe(guardian=guardian,trading={'instances':[]})
        self.assertEqual(other.status()['events'],[])
        self.assertEqual(other.dispatch_one()['state'],'idle')
        self.assertEqual(self.transport.calls,[])

    def test_one_outage_then_recovery_with_receipts_not_user_read(self):
        self.outage(); self.outage()
        self.assertEqual(len(self.health.status()['events']),1)
        result=self.health.dispatch_one()
        self.assertEqual(result['outcome'],'accepted'); self.assertFalse(result['userRead'])
        self.assertEqual(self.health.dispatch_one()['state'],'idle')
        self.recover(); self.recover()
        self.assertEqual(self.health.dispatch_one()['outcome'],'accepted')
        self.assertEqual([e['kind'] for e in self.transport.calls],['outage','recovery'])
        self.assertEqual(self.transport.calls[1]['replaceId'],123)
        self.assertEqual(self.health.status()['receiptSemantics'],'desktop_daemon_acceptance_not_user_read')

    def test_unsent_old_warning_superseded_without_spurious_recovery(self):
        self.outage(); self.recover()
        status=self.health.status()
        self.assertEqual(status['events'][0]['status'],'superseded')
        self.assertEqual(status['pending'],0)
        self.health.dispatch_one()
        self.assertEqual(self.transport.calls,[])

    def test_failed_delivery_retries_then_stops_after_five(self):
        self.outage()
        self.transport.result={'outcome':'failed','reason':'desktop_transport_unavailable'}
        self.assertEqual(self.health.dispatch_one()['state'],'retry')
        self.assertEqual(self.health.dispatch_one()['state'],'idle')
        for delay in (15,60,300,900):
            self.now+=delay
            result=self.health.dispatch_one()
        self.assertEqual(result['state'],'exhausted')
        self.now+=100000
        self.outage(); self.health.dispatch_one()
        self.assertEqual(len(self.transport.calls),5)
        with self.health._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],5)

    def test_retry_cancelled_by_recovery_and_removed_source(self):
        self.outage()
        self.transport.result={'outcome':'failed','reason':'desktop_delivery_rejected'}
        self.health.dispatch_one()
        self.recover(); self.now+=20
        self.health.dispatch_one()
        self.assertEqual(len(self.transport.calls),1)
        self.outage(); self.guardian['config']=None; self.observe()
        self.assertEqual(self.health.status()['pending'],0)

    def test_timeout_unknown_is_not_retried(self):
        self.outage()
        self.transport.result={'outcome':'unknown','reason':'notification_timeout'}
        self.assertEqual(self.health.dispatch_one()['state'],'unknown')
        self.now+=1000; self.outage()
        self.assertEqual(self.health.dispatch_one()['state'],'idle')
        self.assertEqual(len(self.transport.calls),1)

    def test_restart_retains_retry_budget_and_does_not_realert_same_state(self):
        self.outage()
        self.transport.result={'outcome':'failed','reason':'desktop_transport_unavailable'}
        self.health.dispatch_one()
        other=CustomerHealth(self.temp.name,self.transport,lambda:self.now)
        other.observe(guardian=self.guardian,trading=self.trading)
        self.assertEqual(other.dispatch_one()['state'],'idle')
        self.now+=15
        other.dispatch_one()
        self.assertEqual(other.status()['events'][0]['attempts'],2)

    def test_interrupted_inflight_is_unknown_not_replayed(self):
        self.outage()
        with self.health._db() as db:
            db.execute("UPDATE transitions SET status='sending',attempts=1,lease_until=?",(int(self.now*1000)-1,))
        self.health.dispatch_one()
        self.assertEqual(self.health.status()['events'][0]['status'],'unknown')
        self.assertEqual(self.transport.calls,[])

    def test_recovery_during_send_supersedes_warning_and_replaces_native_receipt(self):
        self.outage()
        original=self.transport.send
        def send(event):
            self.recover()
            return original(event)
        self.transport.send=send
        self.assertEqual(self.health.dispatch_one()['state'],'superseded')
        self.transport.send=original
        self.health.dispatch_one()
        self.assertEqual(self.transport.calls[-1]['kind'],'recovery')
        self.assertEqual(self.transport.calls[-1]['replaceId'],123)

    def test_stale_timestamps_override_green_snapshot_and_active_exposure_arms_fault(self):
        self.now+=13
        self.assertEqual(self.observe()['events'][0]['kind'],'outage')
        other=CustomerHealth(Path(self.temp.name)/'exposed',self.transport,lambda:self.now)
        guardian={'config':{'enabled':True},'monitor':{'state':'unknown'}}
        other.observe(guardian=guardian,trading={'instances':[]})
        trading={'instances':[{'instanceId':'fixture','tradePhase':'active','runtimeState':'running','feed':{'state':'not_started'}}]}
        result=other.observe(guardian=guardian,trading=trading)
        self.assertTrue(any(e['source']=='guardian' and e['kind']=='outage' for e in result['events']))
        self.assertTrue(any(e['source']=='market:fixture' for e in result['events']))

    def test_market_timestamp_loss_and_receiver_fault_create_separate_transitions(self):
        item={'instanceId':'fixture','runtimeState':'running','tradePhase':'idle','feed':{'state':'fresh','nativeVerified':True,'observedAt':int(self.now*1000)}}
        self.trading={'instances':[item],'ingress':{'state':'listening_loopback'}}
        self.observe(); self.now+=2.001; self.trading['ingress']['state']='listener_unavailable'
        sources={e['source'] for e in self.observe()['events']}
        self.assertEqual(sources,{'market:fixture','ingress'})

    def test_default_transport_disabled_and_raw_exception_fields_excluded(self):
        other=CustomerHealth(Path(self.temp.name)/'disabled',clock=lambda:self.now)
        other.observe(guardian=self.guardian,trading=self.trading)
        self.assertEqual(other.dispatch_one()['state'],'disabled')
        self.outage()
        self.transport.result={'outcome':'accepted','reason':'secret arbitrary provider text','nativeId':123}
        result=self.health.dispatch_one()
        self.assertEqual(result['reason'],'transport_result_unknown')
        self.assertNotIn('secret',str(self.health.status()))


class NativeTransport(unittest.TestCase):
    def test_fixed_copy_bounded_process_and_acceptance_not_read(self):
        sender=NativeNotifications()
        with patch('customer_health.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],0,'432\n','')
            result=sender.send({'source':'guardian','kind':'outage','replaceId':321})
            self.assertEqual(result['outcome'],'accepted'); self.assertFalse(result['userRead'])
            args=run.call_args.args[0]
            self.assertEqual(args[0],'/usr/bin/notify-send')
            self.assertIn('--replace-id=321',args)
            self.assertEqual(run.call_args.kwargs['timeout'],5)
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_missing_daemon_failed_timeout_unknown_and_bad_receipt_unknown(self):
        sender=NativeNotifications(); event={'source':'market:fixture','kind':'outage'}
        with patch('customer_health.subprocess.run',side_effect=FileNotFoundError()):
            self.assertEqual(sender.send(event)['outcome'],'failed')
        with patch('customer_health.subprocess.run',side_effect=subprocess.TimeoutExpired('redacted',5)):
            self.assertEqual(sender.send(event)['outcome'],'unknown')
        with patch('customer_health.subprocess.run',return_value=subprocess.CompletedProcess([],0,'','')):
            self.assertEqual(sender.send(event)['outcome'],'unknown')


if __name__=='__main__': unittest.main()
