"""Restored real ledgers stay fenced until exact internal two-acquisition proof."""
import contextlib
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

import test_maintenance_service as fixtures
from test_maintenance_service import ForbiddenBrowser,PASSPHRASE,RID
from guardian_service import GuardianService
from trading_service import TradingService
from maintenance_service import MaintenanceService
import maintenance as core

class RestoreTests(unittest.TestCase):
    setUp=fixtures.ServiceFixtures.setUp
    tearDown=fixtures.ServiceFixtures.tearDown
    install=fixtures.ServiceFixtures.install
    native=fixtures.ServiceFixtures.native
    restart=fixtures.ServiceFixtures.restart
    refused=fixtures.ServiceFixtures.refused
    backup=fixtures.ServiceFixtures.backup

    def acquire(self,**changes):
        """A new GET /state reading from the bridge for the bound account (observed now)."""
        self.bridge.state_override=dict(changes)
        return self.bridge.state()
    def new_process(self):
        # New service object, with restored hold observed before opening any engine.
        self.server.guardian=GuardianService(self.root,browser=ForbiddenBrowser())
        self.server.trading=TradingService(self.root,self.server.guardian,self.store,bridge_opener=self.bridge.opener)
        self.service=MaintenanceService(self.server,clock=lambda:self.now,trust={},restart_factory=self.restart,
                                        bridge_opener=self.bridge.opener)
        self.server.maintenance=self.service
    def restored(self,empty=False,commands=None):
        if not empty:
            runtime=self.install()
            if commands is not None:
                runtime.engine.data['trade']={'id':'closed-fixture','phase':'closed'};runtime.engine.data['commands']=commands;runtime.engine.save()
        backup=self.backup()
        if not empty:self.acquire()
        stage=self.service.perform('restore/prepare',{'backupId':backup['result']['backupId'],'passphrase':PASSPHRASE})
        self.applied=self.service.perform('restore/apply',{'restoreId':stage['result']['restoreId']})
        self.new_process()
        if not empty:self.acquire()
    def reconcile(self):return self.service.perform('restore/reconcile',{})
    def held(self):return (self.root/'maintenance/hold.json').exists()
    def test_blank_scope_restore_reconcile_then_cold_start_is_held(self):
        self.restored(empty=True);self.assertTrue(self.held())
        result=self.reconcile();self.assertEqual(result['result']['status'],'reconciled_held');self.assertEqual(result['result']['proofsCollected'],0)
        self.assertFalse(self.held());self.assertTrue(result['maintenance']['restartRequired'])
        self.assertEqual(result['maintenance']['reconciliation']['state'],'reconciled')
        receipt=json.loads(next((self.root/'maintenance/reconciliations').iterdir()).read_bytes())
        self.assertTrue(receipt['emptyExecutionScope']);self.assertEqual(receipt['observations'],[])
        self.assertTrue(receipt['entriesHeld']);self.assertFalse(receipt['automaticReplay'])
        self.new_process();self.assertEqual(self.server.trading.runtime,{});self.assertFalse(self.service.restart_required)
    def test_same_acquisition_twice_never_consumes_hold_and_new_one_finishes(self):
        self.restored()
        first=self.reconcile();self.assertEqual(first['result']['status'],'awaiting_second_observation');self.assertTrue(self.held())
        again=self.reconcile();self.assertEqual(again['result']['proofsCollected'],1);self.assertTrue(self.held())
        self.now+=.01;self.acquire();result=self.reconcile()
        self.assertEqual(result['result']['proofsCollected'],2);self.assertFalse(self.held());self.assertTrue(result['result']['entriesHeld'])
        self.assertTrue(result['maintenance']['restartRequired']);self.assertIsNone(self.server.trading)
    def test_first_acquisition_survives_service_restart(self):
        self.restored();self.reconcile();self.server.trading.close();self.server.guardian.close();self.new_process()
        self.now+=.01;self.acquire();self.assertEqual(self.reconcile()['result']['status'],'reconciled_held')
    def test_first_proof_expiry_resets_instead_of_counting_stale_evidence(self):
        self.restored();self.reconcile();self.now+=11;self.acquire()
        self.assertEqual(self.reconcile()['result']['status'],'awaiting_second_observation');self.assertTrue(self.held())
    def test_bridge_position_or_working_order_refuses(self):
        self.restored()
        for change in ({'net_position':1},{'open_order_ids':['7']},{'unresolved_commands':1}):
            self.acquire(**change)
            self.refused('entries_must_be_held_and_native_flat',self.reconcile);self.assertTrue(self.held())
    def test_bridge_account_mismatch_refuses(self):
        self.restored()
        for change in ({'account_id':999},{'soup_account_id':'other'},{'synced':False}):
            self.acquire(**change);self.refused('native_safety_unknown',self.reconcile)
    def rewrite_engine(self,edit):
        path=self.root/'trading'/('engine_'+RID)/'engine.sqlite';db=sqlite3.connect(path)
        try:
            state=json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0]);edit(state)
            db.execute('UPDATE state SET body=? WHERE id=1',(json.dumps(state),));db.commit()
        finally:db.close()
    def test_active_and_unknown_restored_trade_never_archived_or_rewritten(self):
        self.restored();self.rewrite_engine(lambda state:state.update(trade={'id':'active','phase':'entry_pending'}))
        self.refused('maintenance_active_trade',self.reconcile);self.assertTrue(self.held())
    def test_unknown_restored_command_refuses_before_any_marker_change(self):
        self.restored();self.rewrite_engine(lambda state:state.update(commands={'uncertain':{'kind':'entry','status':'unknown'}}))
        self.refused('maintenance_unreconciled',self.reconcile);self.assertTrue(self.held())
        self.assertFalse((self.root/'maintenance/reconciliations').exists())
    def test_changed_closed_ledger_does_not_inherit_old_proof(self):
        self.restored();self.reconcile();self.rewrite_engine(lambda state:state.update(test_newer_fixture=True))
        self.now+=.01;self.acquire();self.refused('reconciliation_scope_changed',self.reconcile);self.assertTrue(self.held())
    def test_accepted_same_trade_cancel_usable_without_receipt_rewriting(self):
        command={'status':'accepted','kind':'cancel','trade_id':'closed-fixture'}
        self.restored(commands={'cancel':command});self.reconcile();self.now+=.01;self.acquire();self.reconcile()
        path=self.root/'trading'/('engine_'+RID)/'engine.sqlite';db=sqlite3.connect(path)
        try:state=json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        finally:db.close()
        self.assertEqual(state['commands']['cancel'],command)
    def test_unfinished_transaction_and_offboard_fallback_markers_refuse(self):
        self.restored();identifier=self.applied['result']['transactionId'];journal=self.root/'maintenance/transactions'/identifier/'JOURNAL.json'
        value=json.loads(journal.read_bytes());value['phase']='prepared';core.atomic(journal,value)
        self.assertFalse(self.service.reconciliation_status()['available']);self.refused('reconciliation_transaction_unresolved',self.reconcile)
        for reason in ('customer_offboarded','invalid_release_fallback_requires_native_reconciliation'):
            self.service.manager._hold(reason);self.assertFalse(self.service.reconciliation_status()['available'])
            self.refused('reconciliation_not_supported',self.reconcile);self.assertTrue(self.held())
    def test_receipt_is_durable_before_atomic_consumption_and_failed_rename_keeps_hold(self):
        self.restored(empty=True);original=os.replace
        def fail_marker(source,target):
            if Path(source)==self.root/'maintenance/hold.json':
                receipts=list((self.root/'maintenance/reconciliations').glob('*.json'));self.assertEqual(len(receipts),1)
                receipt=json.loads(receipts[0].read_bytes());self.assertEqual(receipt['state'],'verified_before_consumption')
                raise OSError('synthetic marker rename failure')
            return original(source,target)
        with patch.object(os,'replace',side_effect=fail_marker),self.assertRaises(OSError):self.reconcile()
        self.assertTrue(self.held());self.assertTrue(self.service.restart_required)
        self.new_process();self.assertTrue(self.held());self.reconcile();self.assertFalse(self.held())
    def test_unsafe_request_proofs_never_accepted(self):
        self.restored();self.refused('invalid_fields',lambda:self.service.perform('restore/reconcile',{'observedAt':1,'flat':True}));self.assertTrue(self.held())
    def test_release_marker_needs_current_process_adoption(self):
        self.service.manager._hold('release_restart_requires_native_reconciliation')
        self.refused('reconciliation_adoption_unverified',self.reconcile);self.assertTrue(self.held())
    def test_release_verified_adoption_bound_and_rechecked_before_consumption(self):
        self.service.manager._hold('release_restart_requires_native_reconciliation')
        fixture={'kind':'signed','version':'fixture-only','root':'/fixture','manifestSha256':'a'*64}
        self.service.adoption=lambda manager:dict(fixture)
        result=self.reconcile();self.assertEqual(result['result']['status'],'reconciled_held');self.assertFalse(self.held())
        receipt=json.loads(next((self.root/'maintenance/reconciliations').iterdir()).read_bytes())
        self.assertEqual(receipt['scope']['adoption'],fixture)
    def test_release_adoption_change_during_shutdown_preserves_hold(self):
        self.service.manager._hold('release_rollback_requires_native_reconciliation')
        calls=[]
        def adoption(manager):
            calls.append(1)
            return {'kind':'signed','version':'fixture-'+str(len(calls)),'root':'/fixture','manifestSha256':'a'*64}
        self.service.adoption=adoption
        self.refused('reconciliation_scope_changed',self.reconcile);self.assertTrue(self.held());self.assertTrue(self.service.restart_required)

if __name__=='__main__':unittest.main()
