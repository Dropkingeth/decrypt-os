#!/usr/bin/env python3
"""Single fixture suite. Never performs host account/key/service operations."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import firstboot as fb
import stage

HERE=Path(__file__).resolve().parent
INSTANCE='11111111-2222-4333-8444-555555555555'
OTHER='66666666-7777-4888-9999-aaaaaaaaaaaa'
PASSWORD='fixture-not-a-real-password'


class Fake:
    def __init__(self):
        self.instance=None;self.account=None;self.password=False;self.sudo=False;self.greeter=False;self.released=False
        self.actor='customer';self.calls=[];self.fail=None;self.authorized=False
    def action(self,name):
        self.calls.append(name)
        if self.fail==name:raise fb.Refusal('fixture-interruption')
    def generate_identity(self):self.action('generate-identity');self.instance=INSTANCE
    def identity_status(self):self.action('check-identity');fb.need(self.instance is not None,'identity-not-ready');return self.instance
    def account_absent(self,name):fb.need(self.account is None,'already-present')
    def create_account(self,name):self.action('create-account');self.account=name
    def verify_account_identity(self,name):fb.need(self.account==name,'account-identity-differs')
    def set_password(self,name,password):self.action('set-password');fb.need(password==PASSWORD,'fixture-password');self.password=True
    def install_sudo_policy(self):self.action('sudo-policy');self.sudo=True
    def install_greeter_account(self,name):self.action('greeter-account');self.greeter=True
    def verify_account(self,name):self.action('verify-account');fb.need(self.account==name and self.password and self.sudo and self.greeter,'account-incomplete')
    def install_runtime_authorization(self,state):self.action('runtime-authorization');fb.need(state['instanceId']==self.instance and state['accountName']==self.account,'authorization-binding');self.authorized=True
    def verify_runtime_authorization(self,state):fb.need(self.authorized,'authorization-missing')
    def release_login(self):self.action('release-login');self.released=True
    def verify_consent_actor(self,name):fb.need(name==self.actor,'consent-actor-mismatch')


class StateFlow(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.backend=Fake()
        self.store=fb.Store(self.root,'public-image','a'*64,'b'*64)
        self.ctl=fb.Controller(self.store,self.backend)
        self.state=self.store.initial()
    def identity(self):self.state=self.ctl.identity(self.state);return self.state
    def account(self):self.identity();self.state=self.ctl.account(self.state,'customer',PASSWORD);return self.state
    def accept(self):self.state=self.ctl.accept(self.state,'a'*64,'b'*64,INSTANCE,'customer');return self.state

    def test_entire_flow_is_personalization_then_separate_notice(self):
        self.assertFalse(self.ctl.report(self.state)['complete'])
        self.account();self.assertTrue(self.backend.released)
        pending=self.ctl.report(self.state);self.assertFalse(pending['complete']);self.assertFalse(pending['consentAccepted'])
        self.accept();report=self.ctl.report(self.state)
        self.assertTrue(report['complete']);self.assertTrue(report['consentAccepted']);self.assertEqual(report['instanceId'],INSTANCE)
        self.assertEqual(self.state['seq'],5)
        self.assertEqual(self.backend.calls.count('generate-identity'),1)
        raw=self.store.path.read_text()
        for secret in (PASSWORD,'$y$','PRIVATE KEY','ssh-ed25519'):
            self.assertNotIn(secret,raw);self.assertNotIn(secret,json.dumps(report))
        self.assertEqual(self.store.path.stat().st_mode&0o777,0o600)

    def test_identity_interruption_never_blindly_regenerates(self):
        self.backend.fail='generate-identity'
        with self.assertRaises(fb.Refusal):self.ctl.identity(self.state)
        failed=self.store.load();self.assertEqual(failed['status'],'failed')
        with self.assertRaises(fb.Refusal):self.ctl.identity(failed)
        self.assertEqual(self.backend.calls.count('generate-identity'),1)
        self.backend.fail=None;self.backend.instance=INSTANCE
        recovered=self.ctl.identity(failed,recovery=True)
        self.assertEqual(recovered['phase'],'account');self.assertEqual(self.backend.calls.count('generate-identity'),1)

    def test_authorization_failure_keeps_identity_and_can_replay_only_after_account_commit(self):
        self.identity();self.backend.fail='runtime-authorization'
        with self.assertRaises(fb.Refusal):self.ctl.account(self.state,'customer',PASSWORD)
        state=self.store.load();self.assertEqual(state['phase'],'notice');self.assertFalse(self.backend.released)
        self.assertEqual(state['instanceId'],INSTANCE)
        with self.assertRaises(fb.Refusal):self.ctl.report(state)
        self.backend.fail=None;self.backend.install_runtime_authorization(state)
        self.assertEqual(self.backend.calls.count('generate-identity'),1)

    def test_partial_identity_cannot_be_falsely_recovered(self):
        failed=self.store.update(self.state,status='in-progress')
        with self.assertRaises(fb.Refusal):self.ctl.identity(failed,recovery=True)
        self.assertEqual(self.store.load()['phase'],'identity')

    def test_account_interruption_requires_same_recorded_account_recovery(self):
        self.identity();self.backend.fail='set-password'
        with self.assertRaises(fb.Refusal):self.ctl.account(self.state,'customer',PASSWORD)
        failed=self.store.load();self.assertEqual(failed['failure'],{'code':'account-outcome-unknown'})
        with self.assertRaises(fb.Refusal):self.ctl.account(failed,'customer',PASSWORD)
        with self.assertRaises(fb.Refusal):self.ctl.account(failed,'different',PASSWORD,recovery=True)
        self.backend.fail=None
        recovered=self.ctl.account(failed,'customer',PASSWORD,recovery=True)
        self.assertEqual(recovered['phase'],'notice');self.assertEqual(self.backend.calls.count('create-account'),1)

    def test_unknown_useradd_outcome_is_not_recreated(self):
        self.identity();self.backend.fail='create-account'
        with self.assertRaises(fb.Refusal):self.ctl.account(self.state,'customer',PASSWORD)
        self.backend.fail=None
        with self.assertRaises(fb.Refusal):self.ctl.account(self.store.load(),'customer',PASSWORD,recovery=True)
        self.assertEqual(self.backend.calls.count('create-account'),1)

    def test_bad_account_password_refuses_before_account_creation(self):
        self.identity()
        for name,password in [('root',PASSWORD),('../escape',PASSWORD),('customer','short'),('customer','abcdefgh\n')]:
            with self.subTest(name=name),self.assertRaises(fb.Refusal):self.ctl.account(self.state,name,password)
        self.assertIsNone(self.backend.account);self.assertEqual(self.store.load()['status'],'pending')

    def test_consent_is_explicit_and_all_four_bindings_are_checked(self):
        self.account();before=self.store.path.read_bytes()
        for args in [('c'*64,'b'*64,INSTANCE,'customer'),('a'*64,'c'*64,INSTANCE,'customer'),
                     ('a'*64,'b'*64,OTHER,'customer'),('a'*64,'b'*64,INSTANCE,'different')]:
            with self.subTest(args=args),self.assertRaises(fb.Refusal):self.ctl.accept(self.state,*args)
            self.assertEqual(self.store.path.read_bytes(),before)
        self.backend.actor='other'
        with self.assertRaises(fb.Refusal):self.accept()
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_duplicate_consent_and_completed_state_cannot_replay_or_rollback(self):
        self.account();self.accept();before=self.store.path.read_bytes()
        with self.assertRaises(fb.Refusal):self.accept()
        with self.assertRaises(fb.Refusal):self.store.update(self.state,phase='identity',status='pending',completedAt=None,notice={'documentSha256':'b'*64,'accepted':False,'acceptedAt':None})
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_machine_or_ssh_identity_change_refuses_even_after_completion(self):
        self.account();self.accept();self.backend.instance=OTHER
        with self.assertRaises(fb.Refusal):self.ctl.report(self.state)

    def test_lost_password_or_sudo_policy_cannot_claim_completion(self):
        self.account();self.accept();self.backend.password=False
        with self.assertRaises(fb.Refusal):self.ctl.report(self.state)

    def test_state_rejects_secrets_extra_fields_booleans_and_missing_binding(self):
        for changes in ({'password':PASSWORD},{'seq':True},{'policySha256':'f'*64},{'notice':{}},{'instanceId':'bad'}):
            obj={**self.state,**changes}
            with self.subTest(changes=list(changes)),self.assertRaises(fb.Refusal):fb.state_check(obj,'public-image','a'*64,'b'*64)

    def test_existing_state_and_links_refuse_without_overwrite(self):
        before=self.store.path.read_bytes()
        with self.assertRaises(fb.Refusal):self.store.initial()
        self.assertEqual(self.store.path.read_bytes(),before)
        self.store.path.rename(self.root/'original')
        self.store.path.symlink_to(self.root/'original')
        with self.assertRaises(fb.Refusal):self.store.load()

    def test_reports_do_not_write_state(self):
        self.account();before=self.store.path.stat();raw=self.store.path.read_bytes()
        self.ctl.report(self.state)
        self.assertEqual(self.store.path.read_bytes(),raw);self.assertEqual(self.store.path.stat().st_mtime_ns,before.st_mtime_ns)


class BootRecheck(unittest.TestCase):
    """Regression: after a finished first start, a failed boot-time re-check (for example
    deleted SSH host keys) kept systemd-user-sessions and the greeter from starting.
    With root locked that locked the owner out of the computer on every boot."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name).resolve();self.state_dir=root/'state';self.state_dir.mkdir()
        self.gate=root/'nologin';self.report=root/'run'/'report.json';self.report.parent.mkdir()
        self.lines=[]
        for name,value in (('GATE',self.gate),('STATE_DIR',self.state_dir),('REPORT',self.report)):
            patcher=patch.object(fb,name,value);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(fb,'journal',self.lines.append);patcher.start();self.addCleanup(patcher.stop)
    def completed(self):(self.state_dir/'state.json').write_text('{}')
    def run_failing(self,argv,error):
        def failing(_argv):raise error
        stderr=__import__('io').StringIO()
        with patch.object(fb,'main',failing),patch.object(sys,'stderr',stderr):code=fb.run(argv)
        return code,stderr.getvalue()
    def missing_key(self):
        return FileNotFoundError(2,'No such file or directory','/etc/ssh/ssh_host_rsa_key')

    def test_drift_after_completed_setup_keeps_sign_in_and_logs_the_cause(self):
        self.completed();self.report.write_text('{}')
        code,stderr=self.run_failing(['setup'],self.missing_key())
        self.assertEqual(code,0);self.assertFalse(self.report.exists())
        self.assertIn('verification-or-operation-failed',stderr);self.assertIn('journalctl -u decrypt-firstboot',stderr)
        self.assertEqual(len(self.lines),1)
        for part in ('FileNotFoundError','ENOENT','/etc/ssh/ssh_host_rsa_key','failing'):self.assertIn(part,self.lines[0])
        code,_=self.run_failing(['setup'],fb.Refusal('unexpected-customer-accounts'))
        self.assertEqual(code,0);self.assertIn('unexpected-customer-accounts',self.lines[-1])

    def test_failure_before_login_release_still_keeps_logins_gated(self):
        self.completed();self.gate.write_bytes(fb.GATE_BYTES)
        code,stderr=self.run_failing(['setup'],self.missing_key())
        self.assertEqual(code,1);self.assertIn('Setup stopped: verification-or-operation-failed',stderr)
        self.assertIn('ENOENT',self.lines[0])
        self.gate.unlink();(self.state_dir/'state.json').unlink()
        self.assertEqual(self.run_failing(['setup'],self.missing_key())[0],1)
        self.gate.symlink_to(self.state_dir);self.completed()
        self.assertEqual(self.run_failing(['setup'],self.missing_key())[0],1)

    def test_only_the_boot_setup_action_is_relaxed(self):
        self.completed()
        for argv in (['recover-account'],['completion','--policy-sha256','a'*64],['accept-notice']):
            with self.subTest(argv=argv):self.assertEqual(self.run_failing(argv,fb.Refusal('identity-changed'))[0],1)
        self.assertEqual(self.run_failing(['setup'],KeyboardInterrupt())[0],130)

    def test_journal_detail_never_carries_free_form_messages(self):
        try:raise ValueError(PASSWORD)
        except ValueError as error:detail=fb.failure_detail(error)
        self.assertNotIn(PASSWORD,detail);self.assertTrue(detail.startswith('ValueError at '))
        self.completed()
        self.run_failing(['setup'],RuntimeError(PASSWORD))
        self.assertNotIn(PASSWORD,self.lines[-1])


class Sources(unittest.TestCase):
    def test_installed_notice_is_required_and_exact(self):
        policy=(HERE/'policy.json').read_bytes()
        config=json.dumps({'schema':'decrypt.firstboot-image.v1','imageId':'arm64-beta3','policySha256':fb.digest(policy)}).encode()
        inputs={fb.POLICY:policy,fb.CONFIG:config,Path(fb.SERVICE):(HERE/'firstboot.py').read_bytes(),fb.NOTICE:(HERE/'NOTICE.txt').read_bytes()}
        with patch.object(fb,'secure_read',side_effect=lambda path,**_:inputs[Path(path)]):fb.load_inputs()
        inputs[fb.NOTICE]=b'altered notice'
        with patch.object(fb,'secure_read',side_effect=lambda path,**_:inputs[Path(path)]),self.assertRaises(fb.Refusal):fb.load_inputs()
        def missing(path,**kwargs):
            if Path(path)==fb.NOTICE:raise FileNotFoundError('fixture')
            return inputs[Path(path)]
        with patch.object(fb,'secure_read',side_effect=missing),self.assertRaises(FileNotFoundError):fb.load_inputs()

    def test_sudoers_logical_line_refusals(self):
        fb.validate_sudo_text('%wheel ALL=(ALL:ALL) PASSWD: ALL\n@includedir /etc/sudoers.d\n')
        continuation=chr(92)+'\n'
        for text in ('%wheel ALL=(ALL:ALL) NOPASS'+continuation+'WD: ALL',
                     'Defaults !authen'+continuation+'ticate',
                     'Defaults exempt_'+continuation+'group=wheel',
                     '@inclu'+continuation+'de /private/file'):
            with self.subTest(text=text),self.assertRaises(fb.Refusal):fb.validate_sudo_text(text)

    def test_first_login_greeter_binds_only_chosen_instance_account(self):
        fb.check_greeter_source(b'[General]\n','customer')
        self.assertEqual(fb.greeter_bytes('customer'),b'[General]\nUsername=customer\n')
        fb.check_greeter_source(fb.greeter_bytes('customer'),'customer')
        with self.assertRaises(fb.Refusal):fb.check_greeter_source(fb.greeter_bytes('someone-else'),'customer')
        with self.assertRaises(fb.Refusal):fb.greeter_bytes('customer\nUsername=root')

    def test_bundle_exact_policy_and_service_binding_with_no_factory_identity(self):
        policy=HERE/'policy.json';raw=policy.read_bytes();bundle=stage.build_bundle(policy,fb.digest(raw),'arm64-beta3')
        self.assertEqual(len(bundle),9)
        self.assertNotIn('etc/systemd/system/decrypt-firstboot.service.d/20-report-directory.conf',bundle)
        unit=bundle['etc/systemd/system/decrypt-firstboot.service']['bytes']
        self.assertIn(b'RuntimeDirectory=decrypt-firstboot',unit)
        self.assertIn(b'RuntimeDirectoryMode=0755',unit)
        self.assertEqual(unit.count(b'ExecStart='),1)
        self.assertEqual(bundle['etc/nologin']['bytes'],fb.GATE_BYTES)
        self.assertEqual(bundle['usr/local/lib/decrypt-firstboot/firstboot.py']['bytes'],(HERE/'firstboot.py').read_bytes())
        for name,record in bundle.items():
            self.assertFalse(name.startswith('/'));self.assertNotIn('..',Path(name).parts)
            self.assertEqual(record['mode'],0o644)
            self.assertNotIn(name,('etc/passwd','etc/shadow','etc/machine-id','var/lib/decrypt-firstboot/state.json'))
        self.assertNotIn('accountName',json.loads(bundle['etc/decrypt-firstboot/image.json']['bytes']))
        with self.assertRaises(ValueError):stage.build_bundle(policy,'a'*64,'arm64-beta3')
        with self.assertRaises(ValueError):stage.build_bundle(policy,fb.digest(raw),'../private')

    def test_report_directory_mode_survives_restrictive_umask(self):
        # Regression: UMask=0077 on the unit narrowed a plain
        # mkdir(mode=0o755) down to 0o700, so publish() refused its own freshly
        # created directory with 'unsafe-report-directory' right after the
        # customer account had already been committed. ensure_report_directory()
        # must chmod() explicitly, since chmod() (unlike mkdir()'s mode argument)
        # is never narrowed by the process umask.
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'decrypt-firstboot'
            previous=os.umask(0o077)
            try:
                fb.ensure_report_directory(directory)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(directory.lstat().st_mode),0o755)
            # Idempotent: an already-correct directory is left exactly alone.
            fb.ensure_report_directory(directory)
            self.assertEqual(stat.S_IMODE(directory.lstat().st_mode),0o755)

    def test_policy_rejects_unknown_and_weakened_modes(self):
        policy=json.loads((HERE/'policy.json').read_bytes())
        for changes in ({'extra':True},{'version':True},{'completionRequires':[]},
                        {'account':{'creation':'factory','rootLocked':True,'sudo':'password-required'}},
                        {'identity':{'machineId':'reuse','sshHostKeys':'generate-once'}},
                        {'service':{'path':'/bin/sh','sha256':'a'*64}}):
            with self.subTest(changes=changes),self.assertRaises(fb.Refusal):fb.policy_check({**policy,**changes})

    def test_password_echo_control_failure_never_reads_fallback_input(self):
        # Exercise Python's real fallback: its warning must abort before _raw_input.
        with patch.object(fb.getpass, 'getpass', side_effect=fb.getpass.fallback_getpass), \
             patch.object(fb.getpass, '_raw_input') as raw_input:
            with self.assertRaisesRegex(fb.Refusal, '^password-echo-control-failed$'):
                fb.masked_password('Fixture password: ')
        raw_input.assert_not_called()

    def test_password_masked_path_unchanged(self):
        with patch.object(fb.getpass, 'getpass', return_value=PASSWORD) as reader:
            self.assertEqual(fb.masked_password('Fixture password: '), PASSWORD)
        reader.assert_called_once_with('Fixture password: ')

    def test_password_uses_stdin_not_argv_or_record(self):
        with patch.object(fb,'command',return_value='') as command:
            fb.Native().set_password('customer',PASSWORD)
        self.assertEqual(command.call_args.args[0],['/usr/sbin/chpasswd'])
        self.assertEqual(command.call_args.args[1],('customer:'+PASSWORD+'\n').encode())
        self.assertNotIn(PASSWORD,str(command.call_args.args[0]))

    def test_readiness_and_completion_are_read_only_source_branches(self):
        import ast
        source=ast.parse((HERE/'firstboot.py').read_bytes())
        main=next(node for node in source.body if isinstance(node,ast.FunctionDef) and node.name=='main')
        marker=next(i for i,node in enumerate(main.body) if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
                    and isinstance(node.value.func,ast.Name) and node.value.func.id=='prepare_directory')
        branch=main.body[marker-1]
        self.assertIsInstance(branch,ast.If)
        names={node.func.id for node in ast.walk(branch) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name)}
        self.assertTrue({'print','root_locked','sudo_check'}<=names)
        self.assertFalse({'atomic','prepare_directory','publish'}&names)
        self.assertIsInstance(branch.body[-1],ast.Return)

    def test_local_help_and_runtime_refusal_do_not_invoke_native_operations(self):
        help_result=subprocess.run([sys.executable,'-I','-B',str(HERE/'firstboot.py'),'--help'],capture_output=True,text=True)
        self.assertEqual(help_result.returncode,0,help_result.stderr)
        if sys.platform=='darwin':
            refused=subprocess.run([sys.executable,'-I','-B',str(HERE/'firstboot.py'),'setup'],capture_output=True,text=True)
            self.assertNotEqual(refused.returncode,0);self.assertIn('linux-root-required',refused.stderr)


if __name__=='__main__':
    for path in (HERE/'firstboot.py',HERE/'stage.py'):compile(path.read_bytes(),str(path),'exec')
    unittest.main(verbosity=2)
