"""Registry-only reversible archival; isolated SQLite/native fixtures, no network."""
import copy,json,unittest
from pathlib import Path
from unittest.mock import patch
import test_trading_runtime as fixtures
from decrypt_trading import RuntimeManager,RuntimeError,Conflict,SignalIngress
from decrypt_trading.retirement import file_manifest

class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.h=fixtures.RuntimeTests(methodName='runTest');self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.m,self.r=self.h.manager,self.h.runtime
    def close_idle(self):self.r.close();return file_manifest(self.r.directory)
    def retire_idle(self):self.close_idle();return self.m.retire(self.r)
    def complete(self):self.h.test_complete_native_fixture_lifecycle_and_cancel_not_a_fill()
    def test_preclose_checks_refuse_active_or_pending_without_stopping_supervision(self):
        result=self.m.retirement_precheck(self.r)
        self.assertTrue(result['retirementAllowed']);self.assertFalse(result['nativeFlatClaimed']);self.assertFalse(self.r.closed)
        self.h.ingest('pine',self.h.entry)
        with self.assertRaises(Conflict):self.m.retirement_precheck(self.r)
        self.assertFalse(self.r.closed);self.assertEqual(self.h.transport.sent,[])
    def test_preclose_checks_current_active_trade_and_never_dispatches_or_changes_inbox(self):
        self.h.enter();self.r.hold();before=len(self.h.transport.sent)
        with self.assertRaises(Conflict):self.m.retirement_precheck(self.r)
        self.assertFalse(self.r.closed);self.assertEqual(len(self.h.transport.sent),before)

    def test_actual_started_worker_closes_sqlite_owned_journals_before_archival(self):
        self.r.start();self.r.hold();self.m.retirement_precheck(self.r);self.r.close()
        self.assertFalse(self.r.worker.is_alive())
        self.assertEqual({p.name for p in self.r.directory.iterdir()},{'engine.sqlite','.writer.lock'})
        before=file_manifest(self.r.directory);result=self.m.retire(self.r)
        self.assertTrue(result['ledgerPreserved']);self.assertEqual(file_manifest(self.r.directory),before)

    def test_idle_retirement_claims_no_native_flat_and_preserves_every_instance_byte(self):
        before=self.close_idle();r=self.m.retire(self.r)
        self.assertEqual(r['basis'],'never_dispatched_idle');self.assertFalse(r['nativeFlatClaimed'])
        self.assertEqual(file_manifest(self.r.directory),before);self.assertEqual(self.m.list(),[])
        row=self.m.list(include_retired=True)[0];self.assertEqual(row['registryState'],'retired');self.assertFalse(row['entryReady'])
        with self.assertRaises(RuntimeError):self.m.open(self.h.config['instanceId'])
        with self.assertRaises(RuntimeError):self.m.get(self.h.config['instanceId'])
        self.assertEqual(self.m.get(self.h.config['instanceId'],include_retired=True),self.h.config)
    def test_open_runtime_and_pending_inputs_refused_without_registry_or_ledger_changes(self):
        with self.assertRaises(Conflict):self.m.retire(self.r)
        self.h.ingest('pine',self.h.entry);before=self.close_idle()
        with self.assertRaises(Conflict):self.m.retire(self.r)
        self.assertEqual(file_manifest(self.r.directory),before);self.assertEqual(len(self.m.list()),1)
    def test_traded_closed_retirement_requires_fresh_native_flat_and_retains_receipt_chain(self):
        self.complete();before=self.close_idle();r=self.m.retire(self.r)
        self.assertTrue(r['nativeFlatClaimed']);self.assertEqual(r['basis'],'closed_native_flat')
        self.assertEqual(file_manifest(self.r.directory),before)
        with self.m._db()as db:
            archived=db.execute('SELECT binding,evidence FROM retired_instances').fetchone();e=json.loads(archived['evidence'])
        self.assertEqual(json.loads(archived['binding']),self.h.config)
        self.assertEqual(e['nativeEvidence']['snapshot']['positionQuantity'],0)
        self.assertEqual(e['nativeEvidence']['snapshot']['engineEvent']['orders'],list(self.h.orders.values()))
    def test_stale_missing_and_conflicting_native_proof_refuse(self):
        self.complete();self.close_idle();good=copy.deepcopy(self.r.proof)
        for proof in (None,{**good,'positionQuantity':1},{**good,'workingOrderIds':['native-working']},{**good,'observedAt':self.h.now-12001}):
            self.r.proof=proof
            with self.assertRaises(Conflict):self.m.retire(self.r)
        self.r.proof=good;self.assertEqual(len(self.m.list()),1)
    def test_arbitrary_extra_native_fields_cannot_enter_archive(self):
        self.complete();self.close_idle();self.r.proof['privateDiagnostic']='should never be stored'
        with self.assertRaises(Conflict):self.m.retire(self.r)
        self.assertEqual(len(self.m.list()),1)
    def test_actual_market_database_bytes_are_preserved_and_restored_archives_stay_retired(self):
        import shutil,sqlite3
        p=self.r.directory/'market.sqlite';db=sqlite3.connect(p);db.execute('CREATE TABLE fixture(value TEXT)');db.execute('INSERT INTO fixture VALUES (?)',('native-shaped retained fixture',));db.commit();db.close();p.chmod(0o600)
        self.retire_idle();before=file_manifest(self.r.directory)
        restored=self.m.root.parent/'restored';shutil.copytree(self.m.root,restored);restored.chmod(0o700)
        manager=RuntimeManager(restored);self.assertEqual(manager.list(),[])
        self.assertEqual(file_manifest(restored/self.h.config['instanceId']),before)
        with self.assertRaises(RuntimeError):manager.open(self.h.config['instanceId'])
    def test_separate_open_writer_prevents_archival_registry_move(self):
        self.close_idle();other=self.m.open(self.h.config['instanceId'],clock=lambda:self.h.now);self.addCleanup(other.close)
        with self.assertRaises((Conflict,BlockingIOError)):self.m.retire(self.r)
        self.assertEqual(len(self.m.list()),1)
    def test_active_and_unknown_lifecycles_cannot_be_retired(self):
        self.h.enter();self.close_idle()
        with self.assertRaises(Conflict):self.m.retire(self.r)
        self.assertEqual(len(self.m.list()),1)
    def test_reactivate_requires_explicit_action_and_starts_held_without_native_proof(self):
        self.retire_idle();before=file_manifest(self.r.directory);m=RuntimeManager(self.m.root)
        self.assertEqual(m.list(),[]);r=m.reactivate(self.h.config['instanceId'],clock=lambda:self.h.now)
        self.assertFalse(r['runtimeOpened']);self.assertTrue(r['entriesHeld']);self.assertEqual(file_manifest(self.r.directory),before)
        opened=m.open(self.h.config['instanceId'],clock=lambda:self.h.now);self.addCleanup(opened.close)
        self.assertTrue(opened.status()['entriesHeld']);self.assertFalse(opened.status()['nativeFresh']);self.assertFalse(opened.status()['entryReady'])
    def test_retirement_frees_account_route_but_reactivation_conflict_is_atomic(self):
        self.retire_idle();new={**self.h.config,'instanceId':'second-pair'};self.m.install(new)
        with self.assertRaises(Conflict):self.m.reactivate(self.h.config['instanceId'])
        self.assertEqual(len(self.m.list()),1);self.assertEqual(len(self.m.list(include_retired=True)),2)
        with self.assertRaises(Conflict):self.m.install(self.h.config)
    def test_retired_id_never_reused_or_implicitly_reinstalled(self):
        self.retire_idle()
        for payload in (self.h.config,{**self.h.config,'accountId':'other-account'}):
            with self.assertRaises(Conflict):self.m.install(payload)
    def test_archive_ledger_tamper_blocks_reactivation_and_preserves_archive(self):
        self.retire_idle();p=self.r.directory/'engine.sqlite';raw=p.read_bytes();p.write_bytes(raw+b'changed')
        with self.assertRaises(Conflict):self.m.reactivate(self.h.config['instanceId'])
        self.assertEqual(self.m.list(),[]);self.assertEqual(len(self.m.list(include_retired=True)),1)
    def test_unsupported_or_changed_exact_source_cannot_reactivate(self):
        self.retire_idle()
        with patch('decrypt_trading.runtime.pair_spec',side_effect=ValueError('not supported')):
            with self.assertRaises(RuntimeError):self.m.reactivate(self.h.config['instanceId'])
        self.assertEqual(self.m.list(),[])
    def test_archive_registry_transaction_rolls_back_whole_move_on_failure(self):
        before=self.close_idle()
        with self.m._db()as db:db.execute("CREATE TRIGGER reject_archive BEFORE INSERT ON retired_instances BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(Conflict):self.m.retire(self.r)
        self.assertEqual(len(self.m.list()),1);self.assertEqual(len(self.m.list(include_retired=True)),1)
        self.assertEqual(file_manifest(self.r.directory),before)
    def test_total_active_plus_archived_is_bounded_32(self):
        self.retire_idle()
        for n in range(31):self.m.install(fixtures.binding('new-'+str(n)))
        with self.assertRaises(Conflict):self.m.install(fixtures.binding('over-limit'))
        self.assertEqual(len(self.m.list(include_retired=True)),32)
    def test_closed_old_runtime_cannot_retire_later_generation(self):
        self.close_idle();fresh=self.m.open(self.h.config['instanceId'],clock=lambda:self.h.now);fresh.close()
        with self.assertRaises(Conflict):self.m.retire(self.r)
        self.assertEqual(len(self.m.list()),1);self.assertTrue(self.m.retire(fresh)['ledgerPreserved'])
    def test_engine_or_feed_wal_sidecars_prevent_unsettled_archive(self):
        self.close_idle();side=self.r.directory/'market.sqlite-wal';side.write_bytes(b'unsettled');side.chmod(0o600)
        with self.assertRaises(Conflict):self.m.retire(self.r)
        self.assertEqual(len(self.m.list()),1)
    def test_reactivated_old_authenticated_entry_receipt_remains_consumed(self):
        from contextlib import contextmanager
        @contextmanager
        def lease(instance,channel):yield 'a'*43
        raw=json.dumps(self.h.entry).encode();first=SignalIngress(self.r,lease).accept(raw,presented_secret='a'*43)
        self.r.step();self.retire_idle();self.m.reactivate(self.h.config['instanceId'])
        fresh=self.m.open(self.h.config['instanceId'],clock=lambda:self.h.now);self.addCleanup(fresh.close)
        replay=SignalIngress(fresh,lease).accept(raw,presented_secret='a'*43)
        self.assertTrue(replay['duplicate']);self.assertEqual(replay['receipt'],first['receipt']);self.assertFalse(fresh.engine.ready())
    def test_retired_runtime_cannot_accept_new_input_or_controls(self):
        self.retire_idle()
        with self.assertRaises(RuntimeError):self.r.set_controls(enabled=True,locked=False)
        with self.assertRaises(RuntimeError):self.h.ingest('pine',self.h.entry)

if __name__=='__main__':unittest.main()
