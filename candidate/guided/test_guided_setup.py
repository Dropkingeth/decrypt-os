"""Synthetic tests only. Never invoke native installers, sudo, VM tools or disk commands."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('guided', HERE / 'guided_setup.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

class TemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.test-', dir=HERE)
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
    def file(self, name, data=b'example'):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

class InputTests(TemporaryCase):
    def media(self):
        records = {'app/decrypt-os-source.tar.gz': b'archive', 'app/SOURCE-MANIFEST.json': b'{}',
                   'dependencies/provision_runtime.py': b'# reviewed'}
        for name, raw in records.items():
            self.file('media/' + name, raw)
        rows = ''.join(g.digest(raw) + '  ' + name + '\n' for name, raw in records.items()).encode()
        self.file('media/SHA256SUMS', rows)
        return self.root / 'media', g.digest(rows)
    def test_verifies_every_manifest_file(self):
        media, h = self.media()
        self.assertEqual(len(g.verify_media(media, h)), 3)
    def test_wrong_trust_anchor_rejected(self):
        media, _ = self.media()
        with self.assertRaisesRegex(g.Refused, 'separate reviewed receipt'):
            g.verify_media(media, '0' * 64)
    def test_changed_payload_rejected(self):
        media, h = self.media()
        (media / 'app/decrypt-os-source.tar.gz').write_bytes(b'changed')
        with self.assertRaisesRegex(g.Refused, 'checksum mismatch'):
            g.verify_media(media, h)
    def test_unlisted_file_rejected(self):
        media, h = self.media()
        (media / 'unexpected').write_text('not reviewed')
        with self.assertRaisesRegex(g.Refused, 'unlisted'):
            g.verify_media(media, h)
    def test_duplicate_manifest_rejected(self):
        media, _ = self.media()
        raw = (media / 'SHA256SUMS').read_bytes()
        raw += raw.splitlines(keepends=True)[0]
        (media / 'SHA256SUMS').write_bytes(raw)
        with self.assertRaisesRegex(g.Refused, 'Duplicate'):
            g.verify_media(media, g.digest(raw))
    def test_traversal_manifest_rejected(self):
        media, _ = self.media()
        raw = ('0' * 64 + '  ../outside\n').encode()
        (media / 'SHA256SUMS').write_bytes(raw)
        with self.assertRaisesRegex(g.Refused, 'escapes'):
            g.verify_media(media, g.digest(raw))
    def test_linked_input_rejected(self):
        media, h = self.media()
        p = media / 'app/SOURCE-MANIFEST.json'
        p.unlink()
        p.symlink_to(self.file('outside', b'{}'))
        with self.assertRaisesRegex(g.Refused, 'Linked'):
            g.verify_media(media, h)
    def test_hardlinked_input_rejected(self):
        p = self.file('input')
        os.link(p, self.root / 'link')
        with self.assertRaisesRegex(g.Refused, 'one link'):
            g.hash_file(p)
    def test_wrong_module_hash_never_executes(self):
        p = self.file('module.py', b'raise RuntimeError("must not execute")')
        with self.assertRaisesRegex(g.Refused, 'installer code changed'):
            g.module(p, '0' * 64)
    def test_safe_archive_refuses_symlink(self):
        media = self.root / 'media'
        self.file('media/dependencies/provision_runtime.py', b'pass')
        archive = media / 'app/decrypt-os-source.tar.gz'
        archive.parent.mkdir()
        with tarfile.open(archive, 'w:gz') as handle:
            row = tarfile.TarInfo('decrypt-os-source/escape')
            row.type = tarfile.SYMTYPE
            row.linkname = '/etc'
            handle.addfile(row)
        attempt = self.root / 'attempt'
        attempt.mkdir()
        with self.assertRaisesRegex(g.Refused, 'linked or special'):
            g.copy_inputs(media, attempt, {'dependencies/provision_runtime.py':g.hash_file(media / 'dependencies/provision_runtime.py'),
                                         'app/decrypt-os-source.tar.gz':g.hash_file(archive)})

class StateTests(TemporaryCase):
    def test_default_status_does_not_create_work(self):
        work = self.root / 'missing'
        self.assertEqual(g.current_state(work)['status'], 'not_started')
        self.assertFalse(work.exists())
    def test_stale_build_is_interrupted_not_complete(self):
        g.state_write(self.root, status='preparing')
        before = (self.root / 'STATE.json').read_bytes()
        self.assertEqual(g.current_state(self.root)['status'], 'interrupted')
        self.assertEqual((self.root / 'STATE.json').read_bytes(), before)
    def test_active_build_not_mislabelled_interrupted(self):
        g.state_write(self.root, status='preparing')
        with g.work_lock(self.root):
            self.assertEqual(g.current_state(self.root)['status'], 'preparing')
            self.assertTrue(g.current_state(self.root)['operationActive'])
    def test_second_operation_cannot_take_lock(self):
        with g.work_lock(self.root), self.assertRaisesRegex(g.Refused, 'Another setup'):
            with g.work_lock(self.root):
                pass
    def test_world_readable_work_refused(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(g.Refused, 'private'):
            with g.work_lock(self.root):
                pass
    def test_nonlinux_build_refused_before_any_work_write(self):
        work = self.root / 'not-created'
        with patch.object(g.os, 'geteuid', return_value=1000), patch.object(g.platform, 'system', return_value='Darwin'):
            with self.assertRaisesRegex(g.Refused, 'Linux ARM64'):
                g.prepare(self.root / 'media', '0' * 64, work)
        self.assertFalse(work.exists())
    def test_root_build_refused_before_work_write(self):
        work = self.root / 'not-created'
        with patch.object(g.os, 'geteuid', return_value=0), self.assertRaisesRegex(g.Refused, 'never as root'):
            g.prepare(self.root / 'media', '0' * 64, work)
        self.assertFalse(work.exists())
    def test_atomic_state_has_private_mode(self):
        g.state_write(self.root, status='ready_for_review')
        self.assertEqual(stat.S_IMODE((self.root / 'STATE.json').stat().st_mode), 0o600)
    def test_no_blind_rebuild_after_privilege_interruption(self):
        g.state_write(self.root, status='installing')
        fake = {'uid':os.getuid(), 'home':str(self.root), 'user':'customer'}
        with patch.object(g.os,'geteuid',return_value=1000), patch.object(g.platform,'system',return_value='Linux'), \
             patch.object(g.platform,'machine',return_value='aarch64'), patch.object(g,'account_info',return_value=fake), \
             patch.object(g,'machine_hash',return_value='a'*64), self.assertRaisesRegex(g.Refused,'attempted'):
            g.prepare(self.root/'media', '0'*64, self.root)

class InstallTests(TemporaryCase):
    def fixture(self):
        plan = {'attempt':str(self.root/'attempt'), 'account':{'uid':os.getuid(),'user':'customer'},
                'machine':'a'*64, 'guidedSha256':'b'*64}
        expected = 'c'*64
        g.state_write(self.root, status='ready_for_review', attempt=plan['attempt'], planSha256=expected)
        runtime = Mock()
        installer = Mock()
        installer.inspect_host.return_value = {'eligibleForInstall':True, 'failures':[]}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(g, 'load_plan', return_value=(plan,runtime,installer,{},{})))
        stack.enter_context(patch.object(g, 'JOURNALS', self.root/'journals'))
        stack.enter_context(patch.object(g.os,'geteuid',return_value=1000))
        stack.enter_context(patch.object(g.sys,'stdin',Mock(isatty=lambda:True)))
        stack.enter_context(patch('builtins.input',return_value='INSTALL'))
        stack.enter_context(patch.object(g, 'acknowledge_install', return_value={'accepted': True}))
        return plan, expected, runtime, installer, stack
    def test_one_grouped_sudo_on_normal_path(self):
        plan,h,runtime,installer,stack = self.fixture()
        run = stack.enter_context(patch.object(g.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)))
        stack.enter_context(patch.object(g,'read_journal',return_value={'phase':'installed_files_verified','verification':{'installedFilesVerified':True}}))
        self.assertTrue(g.install(self.root,h)['installedFilesVerified'])
        self.assertEqual(run.call_count,1)
        args=run.call_args.args[0]
        self.assertEqual(args[:3],['/usr/bin/sudo','--','/usr/bin/python3'])
        self.assertIn('root-apply',args)
        self.assertNotIn('-S',args)
        self.assertNotIn('-n',args)
        self.assertEqual(g.current_state(self.root)['status'],'installed')
    def test_cancel_before_sudo_keeps_ready_build(self):
        _,h,_,_,stack=self.fixture()
        stack.enter_context(patch('builtins.input',return_value=''))
        run=stack.enter_context(patch.object(g.subprocess,'run'))
        self.assertTrue(g.install(self.root,h)['cancelled'])
        run.assert_not_called()
        self.assertEqual(g.current_state(self.root)['status'],'ready_for_review')
    def test_failed_auth_proves_fresh_target_before_reopen(self):
        _,h,runtime,installer,stack=self.fixture()
        stack.enter_context(patch.object(g.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)))
        self.assertTrue(g.install(self.root,h)['safeToReopenPrompt'])
        self.assertEqual(runtime.host_guard.call_count,2)
        self.assertEqual(installer.inspect_host.call_count,2)
        self.assertEqual(g.current_state(self.root)['status'],'ready_for_review')
    def test_privileged_partial_failure_never_replayed(self):
        _,h,_,_,stack=self.fixture()
        (self.root/'journals'/h).mkdir(parents=True)
        stack.enter_context(patch.object(g.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)))
        with self.assertRaisesRegex(g.Refused,'did not complete'):
            g.install(self.root,h)
        self.assertEqual(g.current_state(self.root)['status'],'installation_unverified')
        with self.assertRaisesRegex(g.Refused,'unattempted'):
            g.install(self.root,h)
    def test_cancel_during_sudo_is_unknown_not_ready(self):
        _,h,_,_,stack=self.fixture()
        stack.enter_context(patch.object(g.subprocess,'run',side_effect=KeyboardInterrupt))
        with self.assertRaises(KeyboardInterrupt):
            g.install(self.root,h)
        self.assertEqual(g.current_state(self.root)['status'],'installation_unverified')
    def test_changed_target_after_auth_failure_stays_blocked(self):
        _,h,runtime,_,stack=self.fixture()
        runtime.host_guard.side_effect=[None,g.Refused('Target changed')]
        stack.enter_context(patch.object(g.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)))
        with self.assertRaisesRegex(g.Refused,'Target changed'):
            g.install(self.root,h)
        self.assertEqual(g.current_state(self.root)['status'],'installation_unverified')
    def test_root_success_requires_verified_journal_not_exit_code_only(self):
        _,h,_,_,stack=self.fixture()
        stack.enter_context(patch.object(g.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)))
        stack.enter_context(patch.object(g,'read_journal',return_value={'phase':'app_apply_started'}))
        with self.assertRaisesRegex(g.Refused,'proof is incomplete'):
            g.install(self.root,h)
        self.assertEqual(g.current_state(self.root)['status'],'installation_unverified')
    def test_stale_review_hash_cannot_open_sudo(self):
        _,_,_,_,stack=self.fixture()
        run=stack.enter_context(patch.object(g.subprocess,'run'))
        with self.assertRaisesRegex(g.Refused,'plan hash'):
            g.install(self.root,'0'*64)
        run.assert_not_called()
    def test_interactive_start_reuses_reverified_ready_build(self):
        plan,h,_,_,stack=self.fixture()
        plan['manifestSha256']='d'*64
        plan['account']['user']='customer'
        prepare=stack.enter_context(patch.object(g,'prepare'))
        stack.enter_context(patch.object(g,'load_plan',return_value=(plan,None,None,{'files':[]},{'files':[]})))
        install=stack.enter_context(patch.object(g,'install',return_value={'installed':True}))
        self.assertTrue(g.start(None,None,self.root)['installed'])
        prepare.assert_not_called()
        install.assert_called_once_with(self.root,h)

class VerificationTests(TemporaryCase):
    def test_exact_modes_not_normalized_inventory_only(self):
        file=self.file('mode')
        file.chmod(0o600)
        with self.assertRaisesRegex(g.Refused,'mode differs'):
            g.verify_owned(file,0o644,uid=os.getuid())
    def test_writable_installed_file_refused(self):
        file=self.file('mode')
        file.chmod(0o666)
        with self.assertRaisesRegex(g.Refused,'writable by others'):
            g.verify_owned(file,uid=os.getuid())
    def test_journal_directories_exact_modes_under_private_umask(self):
        root=self.root/'journal'
        original=g.verify_owned
        def owned(path,mode=None,uid=0,directory=False):
            # Sandbox files stand in for root-owned system files; mode/type checks remain real.
            return original(path,mode,os.getuid(),directory)
        old=os.umask(0o077)
        try:
            with patch.object(g,'JOURNALS',root), patch.object(g,'verify_owned',side_effect=owned):
                # Above sandbox ancestors are outside scope; patch only ownership of those ancestors.
                with patch.object(g,'verify_owned',side_effect=lambda p,*a,**kw: owned(p,*a,**kw) if Path(p).is_relative_to(self.root) else None):
                    dest=g.journal_directory('a'*64)
                    self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o755)
                    self.assertEqual(stat.S_IMODE(dest.stat().st_mode),0o755)
                    with self.assertRaisesRegex(g.Refused,'already attempted'):
                        g.journal_directory('a'*64)
        finally:
            os.umask(old)
    def test_full_verify_cannot_succeed_from_receipt_alone(self):
        fake_runtime=Mock()
        fake_runtime.inventory.return_value=[]
        with patch.object(g,'verify_owned'), patch.object(g,'verify_runtime',return_value={}):
            with self.assertRaises(FileNotFoundError):
                g.verify_application({'account':{'home':'/home/customer','uid':1000}},
                                     {'files':[{'path':'usr/local/lib/decrypt-os/missing','mode':'0o644','bytes':3,'sha256':'a'*64}]},self.root)
    def test_unprivileged_verify_uses_only_readonly_root_action(self):
        plan={'machine':'a'*64,'guidedSha256':'b'*64,'account':{'uid':os.getuid()}}
        h='c'*64
        g.state_write(self.root,status='installation_unverified',planSha256=h,attempt=str(self.root/'attempt'))
        with patch.object(g.os,'geteuid',return_value=1000), patch.object(g.sys,'stdin',Mock(isatty=lambda:True)), \
             patch.object(g,'load_plan',return_value=(plan,None,None,None,None)), patch.object(g,'read_journal'), \
             patch.object(g.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout='{"installedFilesVerified":true}')) as run:
            before=(self.root/'STATE.json').read_bytes()
            self.assertTrue(g.verify(self.root,h)['installedFilesVerified'])
            self.assertIn('root-verify',run.call_args.args[0])
            self.assertNotIn('root-apply',run.call_args.args[0])
            self.assertEqual((self.root/'STATE.json').read_bytes(),before)

class RootSequenceTests(TemporaryCase):
    def setup_root(self):
        plan={'attempt':str(self.root/'attempt'),'machine':'a'*64,'account':{'uid':1000,'user':'customer'},
              'runtimeReceiptSha256':'b'*64,'sourceManifestSha256':'c'*64,'recipeSha256':'d'*64}
        runtime=Mock()
        runtime.apply.return_value={'installed':True}
        installer=Mock()
        installer.inspect_host.return_value={'eligibleForInstall':True,'failures':[]}
        installer.dependency_status.return_value={'ready':True}
        installer.apply.return_value={'installed':True}
        journal=self.root/'journal'
        journal.mkdir()
        stack=contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(g.os,'geteuid',return_value=0))
        stack.enter_context(patch.dict(g.os.environ,{'SUDO_UID':'1000'}))
        stack.enter_context(patch.object(g,'load_plan',return_value=(plan,runtime,installer,{},{})))
        stack.enter_context(patch.object(g,'journal_directory',return_value=journal))
        stack.enter_context(patch.object(g,'verify_acknowledgement',return_value={'accepted':True}))
        # Force root's scratch work into this local fixture, not /var/tmp.
        original=tempfile.TemporaryDirectory
        stack.enter_context(patch.object(g.tempfile,'TemporaryDirectory',side_effect=lambda **k:original(dir=self.root)))
        stack.enter_context(patch.object(g,'verify_runtime',return_value={'runtimeModesVerified':True}))
        stack.enter_context(patch.object(g,'installed_verification',return_value={'installedFilesVerified':True}))
        return plan,runtime,installer,journal,stack
    def test_runtime_then_full_preflight_then_app(self):
        plan,runtime,installer,journal,stack=self.setup_root()
        calls=[]
        runtime.apply.side_effect=lambda *a:(calls.append('runtime'),{'installed':True})[1]
        installer.dependency_status.side_effect=lambda:(calls.append('full-preflight'),{'ready':True})[1]
        installer.apply.side_effect=lambda *a:(calls.append('app'),{'installed':True})[1]
        result=g.root_apply(self.root/'PLAN.json','e'*64)
        self.assertEqual(calls,['runtime','full-preflight','app'])
        self.assertEqual(result['phase'],'installed_files_verified')
        self.assertTrue(g.read_json(journal/'RESULT.json')['runtimeApply']['installed'])
    def test_runtime_failure_stops_before_app(self):
        _,runtime,installer,journal,_=self.setup_root()
        runtime.apply.side_effect=g.Refused('runtime failed')
        with self.assertRaisesRegex(g.Refused,'runtime failed'):
            g.root_apply(self.root/'PLAN.json','e'*64)
        installer.apply.assert_not_called()
        self.assertEqual(g.read_json(journal/'RESULT.json')['phase'],'recovery_required')
    def test_partial_app_failure_retains_runtime_receipt(self):
        _,_,installer,journal,_=self.setup_root()
        installer.apply.side_effect=KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            g.root_apply(self.root/'PLAN.json','e'*64)
        receipt=g.read_json(journal/'RESULT.json')
        self.assertTrue(receipt['runtimeApply']['installed'])
        self.assertEqual(receipt['phase'],'recovery_required')
    def test_failed_full_preflight_never_runs_app(self):
        _,_,installer,journal,_=self.setup_root()
        installer.dependency_status.return_value={'ready':False}
        with self.assertRaisesRegex(g.Refused,'preflight failed'):
            g.root_apply(self.root/'PLAN.json','e'*64)
        installer.apply.assert_not_called()
        self.assertEqual(g.read_json(journal/'RESULT.json')['phase'],'recovery_required')
    def test_wrong_sudo_account_before_install(self):
        _,runtime,_,_,stack=self.setup_root()
        stack.enter_context(patch.dict(g.os.environ,{'SUDO_UID':'1001'}))
        with self.assertRaisesRegex(g.Refused,'reviewed desktop account'):
            g.root_apply(self.root/'PLAN.json','e'*64)
        runtime.apply.assert_not_called()

if __name__=='__main__':
    unittest.main(verbosity=2)
