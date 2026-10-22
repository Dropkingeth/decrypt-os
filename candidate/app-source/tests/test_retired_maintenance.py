"""Archived registry bytes and signed public commissioning receipt backup scope."""
import hashlib
import json
from pathlib import Path
import unittest

import test_maintenance_service as service_fixtures
import test_maintenance as kernel_fixtures
from test_maintenance_service import ForbiddenBrowser,PASSPHRASE,RID
from guardian_service import GuardianService
from trading_service import TradingService
from maintenance_service import MaintenanceService
import maintenance as core

class ArchivedMaintenance(unittest.TestCase):
    setUp=service_fixtures.ServiceFixtures.setUp
    tearDown=service_fixtures.ServiceFixtures.tearDown
    install=service_fixtures.ServiceFixtures.install
    native=service_fixtures.ServiceFixtures.native
    restart=service_fixtures.ServiceFixtures.restart
    refused=service_fixtures.ServiceFixtures.refused
    backup=service_fixtures.ServiceFixtures.backup
    def archive(self):
        runtime=self.install();runtime.close();self.trading.manager.retire(runtime)
        self.trading.runtime.pop(runtime.binding['instanceId'])
        self.guardian.config=None;self.guardian.observation=None
        self.engine=self.root/'trading'/('engine_'+RID)/'engine.sqlite'
        self.original=self.engine.read_bytes();self.assertEqual(self.original[18:20],b'\x02\x02')
    def test_retired_bytes_and_registry_remain_retired_through_backup_restore_reconcile(self):
        self.archive();backup=self.backup()
        self.assertEqual(self.engine.read_bytes(),self.original)
        stage=self.service.perform('restore/prepare',{'backupId':backup['result']['backupId'],'passphrase':PASSPHRASE})
        staged=self.root/'maintenance/restores'/stage['result']['restoreId']/'files/trading'/('engine_'+RID)
        self.assertEqual((staged/'engine.sqlite').read_bytes(),self.original);self.assertEqual((staged/'.writer.lock').read_bytes(),b'')
        self.service.perform('restore/apply',{'restoreId':stage['result']['restoreId']})
        self.server.guardian=GuardianService(self.root,browser=ForbiddenBrowser());self.server.trading=TradingService(self.root,self.server.guardian,self.store)
        self.service=MaintenanceService(self.server,clock=lambda:self.now,trust={})
        result=self.service.perform('restore/reconcile',{})
        self.assertEqual(result['result']['status'],'reconciled_held');self.assertEqual(result['result']['proofsCollected'],0)
        self.assertEqual(self.engine.read_bytes(),self.original)
        guardian=GuardianService(self.root,browser=ForbiddenBrowser());trading=TradingService(self.root,guardian,self.store)
        try:
            self.assertEqual(trading.manager.list(),[]);self.assertEqual(trading.manager.list(include_retired=True)[0]['registryState'],'retired')
        finally:trading.close();guardian.close()
    def test_actual_closed_native_lifecycle_archive_receipt_is_checked(self):
        import shutil
        import test_trading_runtime as runtime_fixtures
        helper=runtime_fixtures.RuntimeTests(methodName='runTest');helper.setUp()
        try:
            helper.test_complete_native_fixture_lifecycle_and_cancel_not_a_fill();helper.runtime.close()
            helper.manager.retire(helper.runtime);self.trading.close()
            shutil.copytree(helper.manager.root,self.root/'trading',dirs_exist_ok=True)
            self.guardian.config=None;self.guardian.observation=None
            self.server.trading=TradingService(self.root,self.guardian,self.store)
            self.service._validate_retired();self.backup()
            path=self.root/'trading/instances.sqlite'
            import sqlite3
            db=sqlite3.connect(path)
            try:
                raw=db.execute('SELECT evidence FROM retired_instances').fetchone()[0];e=json.loads(raw)
                e['nativeEvidence']['snapshot']['positionQuantity']=1
                e['nativeEvidence']['snapshotSha256']=core.digest(core.canonical(e['nativeEvidence']['snapshot']))
                db.execute('UPDATE retired_instances SET evidence=?',(json.dumps(e),));db.commit()
            finally:db.close()
            self.refused('maintenance_archived_changed',self.service._validate_retired)
        finally:helper.doCleanups()
    def test_archive_hash_tamper_refuses_before_shutdown(self):
        self.archive();raw=bytearray(self.original);raw[-1]^=1;self.engine.write_bytes(raw)
        self.refused('maintenance_archived_changed',self.backup);self.assertFalse(self.trading.closed)
    def test_archive_evidence_is_not_an_unmanaged_directory_exemption(self):
        self.archive();database=self.root/'trading/instances.sqlite'
        import sqlite3
        db=sqlite3.connect(database)
        try:db.execute("UPDATE retired_instances SET evidence='{}'");db.commit()
        finally:db.close()
        self.refused('maintenance_archived_changed',self.backup)

    def retired_engine(self):
        from test_trading_service import make_retired
        runtime=self.install();runtime.close();instance_id=runtime.binding['instanceId']
        self.trading.runtime.pop(instance_id)
        make_retired(self.root,instance_id)
        self.guardian.config=None;self.guardian.observation=None
        return instance_id
    def test_an_active_retired_engine_refuses_maintenance_plainly(self):
        self.retired_engine()
        self.refused('maintenance_retired_engine_active',self.backup)
    def test_a_retired_engine_archived_without_its_engine_backs_up(self):
        instance_id=self.retired_engine()
        self.assertEqual(self.trading.manager.retire_retired_engine(instance_id)['basis'],'retired_engine_closed_ledger')
        self.service._validate_retired();self.backup()

class CommissioningReceipts(unittest.TestCase):
    setUp=kernel_fixtures.Fixture.setUp
    tearDown=kernel_fixtures.Fixture.tearDown
    put_db=kernel_fixtures.Fixture.put_db
    put=kernel_fixtures.Fixture.put
    backup=kernel_fixtures.Fixture.backup
    refused=kernel_fixtures.Fixture.refused
    def test_public_signed_receipts_in_default_backup_not_private_keys(self):
        active='trading/commissioning/engine_fixture.json';history='trading/commissioning/history/'+'a'*64+'.json'
        self.put(active,b'{"fixture":"signed-public-receipt"}');self.put(history,b'{"fixture":"prior-public-receipt"}')
        backup=self.backup();stage=self.manager.prepare_restore(backup['backupId'],kernel_fixtures.PASS)
        files=self.manager.root/'restores'/stage['restoreId']/'files'
        self.assertEqual((files/active).read_bytes(),(self.state/active).read_bytes());self.assertTrue((files/history).exists())
        self.assertFalse(core.state_path_allowed('trading/commissioning/key.pem'))
        self.assertFalse(core.state_path_allowed('trading/commissioning/history/not-a-hash.json'))
    def test_receipt_inventory_maximum_128_and_symlink_refuse(self):
        for i in range(129):self.put('trading/commissioning/history/'+format(i,'064x')+'.json',b'{}')
        self.refused('too_many_commissioning_receipts',self.backup)
    def test_unlisted_signing_key_or_symlink_never_copied(self):
        self.put('trading/commissioning/private.pem',b'not-a-real-key')
        self.refused('commissioning_receipt_path_invalid',self.backup)
        (self.state/'trading/commissioning/private.pem').unlink()
        (self.state/'trading/commissioning/engine_fixture.json').symlink_to(self.db)
        with self.assertRaises((OSError,core.MaintenanceError)):self.backup()

if __name__=='__main__':unittest.main()
