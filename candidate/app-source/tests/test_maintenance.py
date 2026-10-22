"""Isolated encrypted-recovery, signed-release and failure-boundary fixtures.

Never opens a VM, provider, real credential, production profile or order route.
"""
import base64
import contextlib
import io
from contextlib import contextmanager, nullcontext
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import types
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'app/backend'))
import maintenance as m
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
spec=importlib.util.spec_from_file_location('test_version_bootstrap',SOURCE/'installer/version_bootstrap.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
PASS='synthetic-fixture-passphrase'

class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.state=Path(self.temp.name).resolve();self.now=1900000000.0
        self.safety={'checkedAt':int(self.now*1000),'entriesHeld':True,'allNativeFlat':True,'workingOrders':0,'pendingOrders':0,'unknownCommands':0,'activeInstances':0}
        self.key=Ed25519PrivateKey.generate();self.public=self.key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        self.manager=m.Maintenance(self.state,lambda:dict(self.safety),trusted_release_keys={'fixture':self.public},quiesce=nullcontext,clock=lambda:self.now,installed_release=lambda:{'kind':'installed','version':'installed','root':'/fixture','manifestSha256':'0'*64})
        self.db=self.state/'onboarding.sqlite';self.put_db(self.db,'original')
    def tearDown(self):
        for p in self.state.rglob('*'):
            if p.is_dir() and not p.is_symlink():p.chmod(0o700)
        self.temp.cleanup()
    def put_db(self,path,value):
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS example(value TEXT)');db.execute('DELETE FROM example');db.execute('INSERT INTO example VALUES (?)',(value,))
    def read_db(self,path=None):
        with sqlite3.connect(path or self.db) as db:return db.execute('SELECT value FROM example').fetchone()[0]
    def put(self,name,raw):m.write_new(self.state/name,raw)
    def backup(self,credentials=False):return self.manager.create_backup(PASS,credentials)
    def stage(self,credentials=False):
        backup=self.backup(credentials);return self.manager.prepare_restore(backup['backupId'],PASS)
    def signed(self,version='release-1',sequence=1):
        files={'app/backend/server.py':b'print("fixture only")\n','app/ui/dist/index.html':b'<p>Fixture only</p>', 'app/trading-runtime/decrypt_trading/__init__.py':b''}
        manifest={'schema':'decrypt.signed-release.v1','version':version,'sequence':sequence,'stateSchema':1,'createdAt':int(self.now*1000)-1000,'expiresAt':int(self.now*1000)+86400000,'files':[{'path':n,'bytes':len(d),'sha256':m.digest(d)} for n,d in sorted(files.items())]}
        sig=base64.b64encode(self.key.sign(m.canonical(manifest))).decode()
        return manifest,sig,'fixture',files
    def stage_release(self,version='release-1',sequence=1):return self.manager.stage_release(*self.signed(version,sequence))
    def refused(self,code,call):
        with self.assertRaises(m.MaintenanceError) as caught:call()
        self.assertEqual(caught.exception.code,code)

class BackupTests(Fixture):
    def test_archive_encrypted_private_and_portable(self):
        result=self.backup();raw=self.manager.export_backup(result['backupId'])
        for text in (PASS,'original','CREATE TABLE example'):self.assertNotIn(text.encode(),raw)
        self.assertEqual((self.manager.root/'backups'/(result['backupId']+'.json')).stat().st_mode&0o777,0o600)
        imported=self.manager.import_backup(raw);self.assertNotEqual(result['backupId'],imported['backupId'])
        stage=self.manager.prepare_restore(imported['backupId'],PASS)
        self.assertEqual(self.read_db(self.manager.root/'restores'/stage['restoreId']/'files/onboarding.sqlite'),'original')
    def test_wrong_passphrase_and_tamper_refused(self):
        result=self.backup()
        self.refused('backup_unlock_failed',lambda:self.manager.prepare_restore(result['backupId'],'different-fixture-passphrase'))
        path=self.manager.root/'backups'/(result['backupId']+'.json');value=json.loads(path.read_bytes());raw=bytearray(base64.b64decode(value['ciphertext']));raw[5]^=1;value['ciphertext']=base64.b64encode(raw).decode();path.write_bytes(m.canonical(value))
        self.refused('backup_unlock_failed',lambda:self.manager.prepare_restore(result['backupId'],PASS))
    def test_credentials_browser_profiles_and_unknown_files_excluded(self):
        for name in m.CREDENTIAL_FILES:self.put(name,b'encrypted-secret-fixture')
        self.put('guardian/browser-profile/Default/Cookies',b'cookie-fixture');self.put('unexpected.txt',b'private-unrelated')
        stage=self.stage();receipt=json.loads((self.manager.root/'restores'/stage['restoreId']/'RECEIPT.json').read_bytes())
        self.assertEqual({x['path'] for x in receipt['files']},{'onboarding.sqlite'})
    def test_opt_in_pairs_encrypted_and_included(self):
        for name in m.CREDENTIAL_FILES:self.put(name,b'encrypted-fixture-only')
        stage=self.stage(True);receipt=json.loads((self.manager.root/'restores'/stage['restoreId']/'RECEIPT.json').read_bytes())
        self.assertTrue(m.CREDENTIAL_FILES<={x['path'] for x in receipt['files']})
    def test_mismatched_route_vault_refused(self):
        self.put('guardian/execution-routes.json',b'fixture')
        self.refused('route_vault_pair_missing',lambda:self.backup(True))
    def test_sqlite_wal_snapshot_reads_committed_values(self):
        connection=sqlite3.connect(self.db);connection.execute('PRAGMA journal_mode=WAL');connection.execute('UPDATE example SET value=?',('from-wal',));connection.commit()
        try:
            stage=self.stage();self.assertEqual(self.read_db(self.manager.root/'restores'/stage['restoreId']/'files/onboarding.sqlite'),'from-wal')
        finally:connection.close()
    def test_symlink_source_refused(self):
        self.put('other.sqlite',b'fixture');self.db.unlink();self.db.symlink_to(self.state/'other.sqlite')
        with self.assertRaises((OSError,m.MaintenanceError)):self.backup()
    def test_hardlink_source_refused(self):
        os.link(self.db,self.state/'linked.sqlite');self.refused('unsafe_file',self.backup)
    def test_browser_symlink_not_traversed(self):
        (self.state/'guardian').mkdir(mode=0o700);(self.state/'guardian/browser-profile').symlink_to('/tmp')
        self.assertEqual(self.backup()['fileCount'],1)
        (self.state/'trading').mkdir(mode=0o700);(self.state/'trading/browser-profile').symlink_to('/tmp')
        self.assertEqual(self.backup()['fileCount'],1)
        self.assertFalse(m.state_path_allowed('trading/browser-profile/engine.sqlite'))
    def test_no_quiescence_refuses_backup(self):
        manager=m.Maintenance(self.state,lambda:self.safety)
        self.refused('maintenance_quiescence_unavailable',lambda:manager.create_backup(PASS))
    def test_all_runtime_database_families_included(self):
        names=('trading/instances.sqlite','trading/service.sqlite','trading/ingress.sqlite','trading/safe-id/engine.sqlite','trading/safe-id/market.sqlite')
        for name in names:self.put_db(self.state/name,'runtime-fixture')
        self.assertEqual(self.backup()['fileCount'],6)
    def test_backup_quiescence_active_before_snapshot(self):
        events=[]
        @contextmanager
        def quiet():
            events.append('enter');yield;events.append('exit')
        self.manager.quiesce=quiet
        original=self.manager._snapshot
        def observed(path):self.assertEqual(events,['enter']);return original(path)
        with patch.object(self.manager,'_snapshot',side_effect=observed):self.backup()
        self.assertEqual(events,['enter','exit'])
    def test_corrupt_database_refused(self):
        self.db.write_bytes(b'not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):self.backup()
    def test_no_arbitrary_backup_id_or_import_path(self):
        for invalid in ('../escape','/tmp/id','bad-id',True):
            self.refused('invalid_backup_id',lambda:self.manager.export_backup(invalid))
        self.refused('invalid_backup',lambda:self.manager.import_backup(b'{}'))
    def test_duplicate_json_and_unsafe_restore_names(self):
        self.refused('duplicate_json_key',lambda:m.strict_json(b'{"x":1,"x":2}'))
        for name in ('../other','/tmp/file','app/../../file','guardian//vault.json','guardian\\vault.json'):
            with self.assertRaises(m.MaintenanceError):m.state_path_allowed(name,True)

class RestoreTests(Fixture):
    def test_restore_held_original_retained_and_no_replay(self):
        stage=self.stage();self.put_db(self.db,'newer')
        result=self.manager.apply_restore(stage['restoreId'])
        self.assertEqual(self.read_db(),'original');self.assertEqual(result['status'],'restored_held');self.assertFalse(result['automaticReplay'])
        self.assertEqual(self.read_db(self.manager.root/'transactions'/result['transactionId']/'before/onboarding.sqlite'),'newer')
        self.assertTrue((self.manager.root/'hold.json').exists())
    def test_all_incomplete_safety_states_refuse_before_swap(self):
        stage=self.stage();self.put_db(self.db,'newer');original=dict(self.safety)
        cases=[('checkedAt',int(self.now*1000)-10001),('checkedAt',int(self.now*1000)+1),('checkedAt',True),('entriesHeld',False),('allNativeFlat',False),('workingOrders',None),('pendingOrders',1),('unknownCommands',True),('activeInstances',1)]
        for key,value in cases:
            self.safety={**original,key:value}
            with self.subTest(key=key,value=value),self.assertRaises(m.MaintenanceError):self.manager.apply_restore(stage['restoreId'])
            self.assertEqual(self.read_db(),'newer')
    def test_extra_browser_supplied_safety_not_accepted(self):
        stage=self.stage();self.safety['browserReady']=True
        self.refused('native_safety_unknown',lambda:self.manager.apply_restore(stage['restoreId']))
    def test_stage_tamper_and_duplicate_row_refused(self):
        stage=self.stage();path=self.manager.root/'restores'/stage['restoreId']/'RECEIPT.json';value=json.loads(path.read_bytes());value['files']*=2;path.write_bytes(m.canonical(value))
        self.refused('invalid_restore_stage',lambda:self.manager.apply_restore(stage['restoreId']))
        self.assertFalse((self.manager.root/'hold.json').exists())
    def test_existing_target_symlink_is_not_followed(self):
        stage=self.stage();outside=self.state/'outside';outside.write_bytes(b'kept');self.db.unlink();self.db.symlink_to(outside)
        with self.assertRaises((OSError,m.MaintenanceError)):self.manager.apply_restore(stage['restoreId'])
        self.assertEqual(outside.read_bytes(),b'kept')
    def interrupted(self):
        self.put('guardian/monitor.json',b'{"enabled":false,"fixture":"old"}')
        stage=self.stage();self.put_db(self.db,'newer');(self.state/'guardian/monitor.json').write_bytes(b'{"enabled":false,"fixture":"new"}')
        original=self.manager._replace_state;count=[0]
        def interrupted(path,raw):
            count[0]+=1
            if count[0]==2:raise OSError('injected bounded fixture interruption')
            original(path,raw)
        with patch.object(self.manager,'_replace_state',side_effect=interrupted),self.assertRaises(OSError):self.manager.apply_restore(stage['restoreId'])
        transaction=next((self.manager.root/'transactions').iterdir())
        self.assertEqual(json.loads((transaction/'JOURNAL.json').read_bytes())['phase'],'recovery_required')
        return transaction
    def test_interrupted_restore_rollback_preserves_originals(self):
        tx=self.interrupted();result=self.manager.recover_restore(tx.name)
        self.assertEqual(self.read_db(),'newer');self.assertIn(b'new',(self.state/'guardian/monitor.json').read_bytes());self.assertEqual(result['status'],'rolled_back_held')
    def test_recovery_prevalidates_all_before_any_swap(self):
        tx=self.interrupted();monitor=(self.state/'guardian/monitor.json').read_bytes();self.put_db(self.db,'post-failure-newer')
        self.refused('newer_state_preserved',lambda:self.manager.recover_restore(tx.name))
        self.assertEqual((self.state/'guardian/monitor.json').read_bytes(),monitor);self.assertEqual(self.read_db(),'post-failure-newer')
    def test_completed_restore_not_silently_rewound(self):
        stage=self.stage();result=self.manager.apply_restore(stage['restoreId'])
        self.refused('restore_not_interrupted',lambda:self.manager.recover_restore(result['transactionId']))

class ReleaseTests(Fixture):
    def test_signed_stage_then_activation_requires_restart(self):
        stage=self.stage_release();self.assertFalse((self.manager.root/'current-release.json').exists())
        result=self.manager.activate_release(stage['version']);self.assertEqual(result['status'],'activated_for_restart')
        self.assertFalse(result['automaticReplay']);self.assertTrue((self.manager.root/'hold.json').exists())
    def test_first_update_can_rollback_to_installed_and_keep_sequence(self):
        self.stage_release();self.manager.activate_release('release-1');result=self.manager.rollback_release()
        self.assertEqual(result['version'],'installed');self.assertEqual(self.manager._pointer()['highestSequence'],1)
        self.refused('release_sequence_not_new',lambda:self.manager.activate_release('release-1'))
    def test_no_verified_baseline_no_first_activation(self):
        self.stage_release();self.manager.installed_release=None
        self.refused('installed_baseline_unavailable',lambda:self.manager.activate_release('release-1'))
    def test_default_trust_has_no_keys(self):
        blank=m.Maintenance(self.state,lambda:self.safety,quiesce=nullcontext,clock=lambda:self.now)
        self.refused('release_key_not_trusted',lambda:blank.stage_release(*self.signed()))
    def test_hash_tamper_refused(self):
        manifest,sig,key,files=self.signed();files['app/backend/server.py']=b'altered'
        self.refused('release_hash_mismatch',lambda:self.manager.stage_release(manifest,sig,key,files))
    def test_signature_tamper_refused(self):
        manifest,sig,key,files=self.signed();manifest['version']='different'
        self.refused('invalid_release_signature',lambda:self.manager.stage_release(manifest,sig,key,files))
    def test_expired_cannot_stage(self):
        args=list(self.signed());args[0]['expiresAt']=int(self.now*1000)-1
        args[1]=base64.b64encode(self.key.sign(m.canonical(args[0]))).decode()
        self.refused('expired_or_incompatible_release',lambda:self.manager.stage_release(*args))
    def test_previously_accepted_release_remains_bootable_after_expiry(self):
        self.stage_release();self.manager.activate_release('release-1');self.now+=172800;self.safety['checkedAt']=int(self.now*1000)
        self.assertEqual(self.manager.verified_release('release-1')['version'],'release-1')
        self.refused('expired_or_incompatible_release',lambda:self.manager.activate_release('release-1'))
    def test_release_inventory_cannot_escape_or_hide_extra(self):
        args=list(self.signed());args[3]['app/backend/unlisted.py']=b'fixture'
        self.refused('release_inventory_mismatch',lambda:self.manager.stage_release(*args))
        for path in ('../server.py','app/../server.py','installer/hook.sh'):
            with self.subTest(path=path):
                try:self.assertFalse(m.release_path_allowed(path))
                except m.MaintenanceError:pass
    def test_installed_release_immutable_files_and_directories_checked(self):
        self.stage_release();root=Path(self.manager.verified_release('release-1')['root']);(root/'app').chmod(0o700)
        self.refused('release_not_immutable',lambda:self.manager.verified_release('release-1'))
    def test_release_after_staging_tamper_detected(self):
        self.stage_release();path=self.manager.root/'releases/release-1/payload/app/backend/server.py';path.chmod(0o600);path.write_bytes(b'changed');path.chmod(0o400)
        self.refused('release_hash_mismatch',lambda:self.manager.verified_release('release-1'))
    def test_release_symlink_rejected(self):
        self.stage_release();path=self.manager.root/'releases/release-1/payload/app/backend';path.chmod(0o700);(path/'surprise.py').symlink_to(self.db);path.chmod(0o500)
        self.refused('linked_release_path',lambda:self.manager.verified_release('release-1'))
    def test_overwrite_and_sequence_rollback_rejected(self):
        self.stage_release();self.refused('release_already_exists',lambda:self.stage_release())
        self.manager.activate_release('release-1');self.stage_release('release-2',1)
        self.refused('release_sequence_not_new',lambda:self.manager.activate_release('release-2'))
    def test_code_rollback_does_not_rewind_ledgers_or_sequence(self):
        self.stage_release();self.manager.activate_release('release-1');self.stage_release('release-2',2);self.manager.activate_release('release-2');self.put_db(self.db,'after-release')
        result=self.manager.rollback_release();self.assertEqual(result['version'],'release-1');self.assertFalse(result['ledgersRewound']);self.assertEqual(self.read_db(),'after-release');self.assertEqual(self.manager._pointer()['highestSequence'],2)
        self.refused('release_sequence_not_new',lambda:self.manager.activate_release('release-2'))
    def test_activation_with_open_worker_refused(self):
        self.stage_release();self.safety['activeInstances']=1
        self.refused('workers_or_native_orders_not_clear',lambda:self.manager.activate_release('release-1'))

class OffboardTests(Fixture):
    def test_credentials_removed_ledger_and_encrypted_backups_retained(self):
        for name in m.CREDENTIAL_FILES:self.put(name,b'opaque-fixture')
        self.put('guardian/monitor.json',b'{"enabled":true}')
        backup=self.backup(True);result=self.manager.offboard('REMOVE SAVED SIGN-INS')
        self.assertEqual(result['status'],'local_credentials_removed_held');self.assertTrue(result['providerRevocationRequired']);self.assertTrue(result['browserSignoutRequired'])
        self.assertEqual(self.read_db(),'original');self.assertTrue(self.manager.export_backup(backup['backupId']));self.assertTrue(all(not (self.state/n).exists() for n in m.CREDENTIAL_FILES));self.assertFalse(json.loads((self.state/'guardian/monitor.json').read_bytes())['enabled'])
    def test_confirmation_required(self):self.refused('offboarding_confirmation_required',lambda:self.manager.offboard('yes'))
    def test_bad_later_path_does_not_partially_delete(self):
        self.put('guardian/execution-ingress.json',b'kept');(self.state/'guardian/vault.json').symlink_to(self.db)
        with self.assertRaises((OSError,m.MaintenanceError)):self.manager.offboard('REMOVE SAVED SIGN-INS')
        self.assertEqual((self.state/'guardian/execution-ingress.json').read_bytes(),b'kept')

class BootstrapTests(Fixture):
    def installed(self):
        root=self.state/'installed';files={'app/backend/server.py':b'print("installed")','app/ui/dist/index.html':b'installed-ui'}
        for name,raw in files.items():m.write_new(root/name,raw)
        receipt={'installed':True,'stageOnly':False,'files':[{'path':'usr/local/lib/decrypt-os/'+n,'bytes':len(d),'sha256':m.digest(d)} for n,d in files.items()]}
        m.write_new(root/'INSTALL-RECEIPT.json',m.canonical(receipt));self.manager.installed_release=lambda:b.installed_release(root,m,self.reader);return root
    def reader(self,path,module):return m.regular(path)
    def identity(self,pid,module):return {'pid':pid,'startTicks':12345,'bootId':'fixture-boot'}
    def test_missing_pointer_falls_back_verified_installed(self):
        result=b.selected_release(self.manager,self.installed(),m,self.reader);self.assertEqual(result['kind'],'installed')
    def test_invalid_pointer_falls_back_held_not_unverified_code(self):
        root=self.installed();m.atomic(self.manager.root/'current-release.json',{'version':'../../escape'})
        self.assertEqual(b.selected_release(self.manager,root,m,self.reader)['kind'],'installed');self.assertTrue((self.manager.root/'hold.json').exists())
    def test_image_first_start_receipt_uses_build_file_list(self):
        # Image installs: first start writes a receipt without a file list; the
        # build wrote INSTALLED-FILES.json (template unit names included).
        root=self.state/'image';files={'app/backend/server.py':b'print("installed")','app/ui/dist/index.html':b'installed-ui',
                                       'modules/x/systemd/unit@.service':b'[Unit]\n'}
        for name,raw in files.items():m.write_new(root/name,raw)
        listing={'schema':'decrypt.installed-files.v1','files':[{'path':'usr/local/lib/decrypt-os/'+n,'bytes':len(d),'sha256':m.digest(d)} for n,d in files.items()]}
        m.write_new(root/'INSTALLED-FILES.json',m.canonical(listing))
        m.write_new(root/'INSTALL-RECEIPT.json',m.canonical({'schema':'decrypt.firstboot-install-receipt.v1','installed':True,'stageOnly':False}))
        self.assertEqual(b.installed_release(root,m,self.reader)['kind'],'installed')
        (root/'modules/x/systemd/unit@.service').write_bytes(b'changed')
        self.refused('installed_payload_changed',lambda:b.installed_release(root,m,self.reader))
    def test_installed_fallback_tamper_refused(self):
        root=self.installed();(root/'app/backend/server.py').write_bytes(b'changed')
        self.refused('installed_payload_changed',lambda:b.selected_release(self.manager,root,m,self.reader))
    def test_exact_signed_pointer_manifest_required(self):
        root=self.installed();self.stage_release();self.manager.activate_release('release-1');pointer=self.manager._pointer();pointer['manifestSha256']='a'*64;m.atomic(self.manager.root/'current-release.json',pointer)
        self.assertEqual(b.selected_release(self.manager,root,m,self.reader)['kind'],'installed')
    def test_ui_stays_bound_to_running_api_not_new_pointer(self):
        root=self.installed();self.stage_release();self.manager.activate_release('release-1');release=b.selected_release(self.manager,root,m,self.reader);b.adopt_api(self.manager,release,m,self.identity)
        self.stage_release('release-2',2);self.manager.activate_release('release-2')
        self.assertEqual(b.ui_release(self.manager,root,m,self.identity,self.reader)['version'],'release-1')
    def test_first_update_rollback_adopts_verified_installed_payload(self):
        root=self.installed();self.stage_release();self.manager.activate_release('release-1');self.manager.rollback_release()
        result=b.selected_release(self.manager,root,m,self.reader);self.assertEqual(result['kind'],'installed');self.assertFalse((self.manager.root/'api-adoption.json').exists())
    def test_ui_pid_boot_or_starttime_reuse_refused(self):
        root=self.installed();release=b.selected_release(self.manager,root,m,self.reader);b.adopt_api(self.manager,release,m,self.identity)
        wrong=lambda pid,module:{'pid':pid,'startTicks':999,'bootId':'other-boot'}
        self.refused('api_adoption_not_live',lambda:b.ui_release(self.manager,root,m,wrong,self.reader))
    def test_ui_exited_api_process_is_not_live_not_a_crash(self):
        self.refused('api_adoption_not_live',lambda:b.process_identity(10**9,m))
    def test_ui_waits_for_fresh_api_adoption_then_succeeds(self):
        calls=[];now=[0.0]
        def release(*args):
            calls.append(1)
            if len(calls)<3:raise m.MaintenanceError('api_adoption_not_live')
            return {'version':'installed'}
        result=b.wait_for_ui_release(None,None,m,timeout=5,clock=lambda:now[0],sleep=lambda s:now.__setitem__(0,now[0]+s),release=release)
        self.assertEqual(result,{'version':'installed'});self.assertEqual(len(calls),3)
    def test_ui_wait_raises_last_refusal_after_timeout(self):
        now=[0.0]
        def release(*args):raise m.MaintenanceError('api_adoption_changed')
        with self.assertRaises(m.MaintenanceError):
            b.wait_for_ui_release(None,None,m,timeout=1,clock=lambda:now[0],sleep=lambda s:now.__setitem__(0,now[0]+s),release=release)
    def test_ui_cannot_replace_signed_payload_root(self):
        root=self.installed();self.stage_release();self.manager.activate_release('release-1');release=b.selected_release(self.manager,root,m,self.reader);value=b.adopt_api(self.manager,release,m,self.identity);value['root']='/tmp/arbitrary';m.atomic(self.manager.root/'api-adoption.json',value)
        self.refused('api_adoption_changed',lambda:b.ui_release(self.manager,root,m,self.identity,self.reader))
    def test_trust_is_explicit_and_public_only(self):
        path=self.state/'trust.json';m.write_new(path,m.canonical({'schema':'decrypt.release-trust.v1','keys':{'release':base64.b64encode(self.public).decode()}}))
        self.assertEqual(b.load_trust(path,m,self.reader),{'release':self.public})
        with self.assertRaises(m.MaintenanceError):b.load_trust(path,m)
    def test_services_use_same_trusted_bootstrap_and_pinned_runtime(self):
        spec=importlib.util.spec_from_file_location('fixture_clean_base',SOURCE/'installer/clean_base.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        files=module.generated_files('customer');api=files['etc/systemd/user/decrypt-os-api.service'][0].decode();ui=files['etc/systemd/user/decrypt-os-ui.service'][0].decode()
        self.assertIn(module.PYTHON+' -E -s -B '+module.INSTALL+'/installer/version_bootstrap.py',api);self.assertIn('--component api',api);self.assertIn('--enable-trading',api);self.assertIn('--component ui',ui)
    def test_user_units_avoid_namespace_settings_that_hide_root_ownership(self):
        # In a user manager these imply a private user namespace; root-owned installs then appear as nobody.
        spec=importlib.util.spec_from_file_location('fixture_clean_base_ns',SOURCE/'installer/clean_base.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        files=module.generated_files('customer');units={k:v[0].decode() for k,v in files.items() if k.startswith('etc/systemd/user/') and k.endswith('.service')}
        self.assertEqual(sorted(n.rsplit('/',1)[1] for n in units),['decrypt-desktop-layout.service','decrypt-os-api.service',
            'decrypt-os-market-browser.service','decrypt-os-ui.service','decrypt-tmp-janitor.service'])
        for name,text in units.items():
            keys={line.split('=',1)[0] for line in text.splitlines() if '=' in line}
            self.assertFalse(keys&{'ProtectSystem','ProtectHome','PrivateTmp','PrivateUsers','ReadWritePaths','ReadOnlyPaths','InaccessiblePaths','BindPaths','BindReadOnlyPaths','TemporaryFileSystem','PrivateDevices','ProtectKernelTunables','ProtectKernelModules','ProtectControlGroups','ProtectProc','ProtectClock','ProtectKernelLogs','ProtectHostname','PrivateNetwork','PrivateIPC'},name)
            for required in ('NoNewPrivileges=yes','RestrictSUIDSGID=yes','UMask=0077','LimitCORE=0'):self.assertIn(required,text,name)

    def running(self,root,release,**kwargs):
        return b.verify_running_api(self.manager,root,m,Path(release['root'])/'app/backend/server.py',
            Path(release['root'])/'app/backend/maintenance_service.py',identity=self.identity,reader=self.reader,modules={},**kwargs)
    def test_running_api_requires_current_process_not_another_live_descriptor(self):
        root=self.installed();release=b.selected_release(self.manager,root,m,self.reader)
        value=b.adopt_api(self.manager,release,m,self.identity);value['pid']+=1;m.atomic(self.manager.root/'api-adoption.json',value)
        self.refused('reconciliation_adoption_unverified',lambda:self.running(root,release))
    def test_running_installed_and_signed_api_adoption_pass_exact_current_selection(self):
        root=self.installed();release=b.selected_release(self.manager,root,m,self.reader);b.adopt_api(self.manager,release,m,self.identity)
        self.assertEqual(self.running(root,release),release)
        self.stage_release();self.manager.activate_release('release-1');release=b.selected_release(self.manager,root,m,self.reader)
        b.adopt_api(self.manager,release,m,self.identity);self.assertEqual(self.running(root,release),release)
    def test_running_api_cannot_clear_new_release_hold_with_old_loaded_version(self):
        root=self.installed();self.stage_release();self.manager.activate_release('release-1');release=b.selected_release(self.manager,root,m,self.reader)
        b.adopt_api(self.manager,release,m,self.identity);self.stage_release('release-2',2);self.manager.activate_release('release-2')
        self.refused('reconciliation_adoption_unverified',lambda:self.running(root,release))
    def test_running_api_different_loaded_server_path_rejected(self):
        root=self.installed();release=b.selected_release(self.manager,root,m,self.reader);b.adopt_api(self.manager,release,m,self.identity)
        self.refused('reconciliation_adoption_unverified',lambda:b.verify_running_api(self.manager,root,m,'/wrong/server.py',
            root/'app/backend/maintenance_service.py',identity=self.identity,reader=self.reader,modules={}))
    def test_running_api_mixed_imports_rejected(self):
        import types
        root=self.installed();release=b.selected_release(self.manager,root,m,self.reader);b.adopt_api(self.manager,release,m,self.identity)
        self.refused('reconciliation_adoption_unverified',lambda:b.verify_running_api(self.manager,root,m,root/'app/backend/server.py',
            root/'app/backend/maintenance_service.py',identity=self.identity,reader=self.reader,
            modules={'guardian_service':types.SimpleNamespace(__file__='/wrong/app/backend/guardian_service.py')}))
    def test_running_api_reboot_identity_is_rechecked(self):
        root=self.installed();release=b.selected_release(self.manager,root,m,self.reader);b.adopt_api(self.manager,release,m,self.identity)
        self.refused('api_adoption_not_live',lambda:b.verify_running_api(self.manager,root,m,root/'app/backend/server.py',
            root/'app/backend/maintenance_service.py',reader=self.reader,modules={},
            identity=lambda pid,module:{'pid':pid,'startTicks':12345,'bootId':'another-boot'}))

class CliTests(Fixture):
    def cli(self,*args,passphrase=PASS):
        spec=importlib.util.spec_from_file_location('fixture_maintenance_cli',SOURCE/'installer/maintenance_cli.py')
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        output=io.StringIO();real_manager=m.Maintenance
        def fixture_manager(*args,**kwargs):return real_manager(*args,**kwargs,clock=lambda:self.now)
        with patch.object(m,'Maintenance',side_effect=fixture_manager),patch.object(sys,'argv',['maintenance_cli.py','--state-dir',str(self.state),*map(str,args)]), \
             patch.dict(sys.modules,{'version_bootstrap':b}),patch.object(b,'load_trust',return_value={'fixture':self.public}), \
             patch.object(cli.getpass,'getpass',return_value=passphrase),contextlib.redirect_stdout(output), \
             patch.object(sys,'stdin',types.SimpleNamespace(isatty=lambda:True)), \
             patch.object(sys,'stderr',types.SimpleNamespace(isatty=lambda:True,write=lambda *_:None,flush=lambda:None)):
            cli.main()
        return output.getvalue()
    def test_export_exact_encrypted_bytes_without_overwrite(self):
        backup=self.backup();destination=self.state/'export.json'
        self.cli('export','--backup-id',backup['backupId'],'--destination',destination)
        self.assertEqual(destination.read_bytes(),self.manager.export_backup(backup['backupId']))
        with self.assertRaises(SystemExit):self.cli('export','--backup-id',backup['backupId'],'--destination',destination)
    def test_import_masks_passphrase_and_only_stages(self):
        backup=self.backup();source=self.manager.root/'backups'/(backup['backupId']+'.json')
        output=self.cli('import','--source',source)
        self.assertIn('Restore staged:',output);self.assertNotIn(PASS,output)
        self.assertEqual(self.read_db(),'original');self.assertFalse((self.manager.root/'hold.json').exists())
    def test_import_wrong_passphrase_does_not_swap(self):
        backup=self.backup();source=self.manager.root/'backups'/(backup['backupId']+'.json')
        with self.assertRaises(SystemExit):self.cli('import','--source',source,passphrase='wrong-fixture-passphrase')
        self.assertEqual(self.read_db(),'original')
    def test_signed_directory_stage_only_never_activates(self):
        manifest,signature,key,files=self.signed();source=self.state/'signed-input'
        m.write_new(source/'RELEASE.json',m.canonical({'manifest':manifest,'signature':signature,'keyId':key}))
        for name,raw in files.items():m.write_new(source/'payload'/name,raw)
        output=self.cli('stage-release','--source',source)
        self.assertIn('Verified signed release staged:',output);self.assertFalse((self.manager.root/'current-release.json').exists())

if __name__=='__main__':unittest.main()
