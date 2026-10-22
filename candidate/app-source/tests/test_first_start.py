#!/usr/bin/env python3
"""First-start fixtures only. No native desktop, sudo, credentials or network."""
import copy
from email.message import Message
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

SOURCE=Path(__file__).resolve().parents[1];CANDIDATE=SOURCE.parent
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location('first_start_fixture',SOURCE/'app/backend/first_start.py');fs=importlib.util.module_from_spec(spec);spec.loader.exec_module(fs)
spec=importlib.util.spec_from_file_location('server_fixture',SOURCE/'app/backend/server.py');server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
INSTANCE='12345678-1234-4234-9234-123456789abc'

class FirstStartTests(unittest.TestCase):
    def setUp(self):
        self.report={'schema':'decrypt.firstboot-report.v1','imageId':'fixture','policySha256':fs.POLICY_SHA256,'status':'pending','phase':'notice',
                     'instanceId':INSTANCE,'accountName':'fixture','complete':False,'consentAccepted':False,'documentSha256':fs.NOTICE_SHA256,'completedAt':None,'sequence':5}
        self.files={fs.POLICY:(CANDIDATE/'firstboot/policy.json').read_bytes(),fs.NOTICE:(CANDIDATE/'firstboot/NOTICE.txt').read_bytes(),
                    fs.HELPER:(CANDIDATE/'firstboot/firstboot.py').read_bytes(),fs.CALLBACK:(SOURCE/'installer/install_notice.py').read_bytes(),
                    fs.IMAGE:json.dumps({'schema':'decrypt.firstboot-image.v1','imageId':'fixture','policySha256':fs.POLICY_SHA256}).encode()}
        self.active=False;self.conflict=False;self.commands=[];self.processes=[];self.fresh=True
        self.controller=fs.FirstStart(read=self.read,run=self.fake_run,actor_check=lambda name:1000 if name=='fixture' else fs.need(False),
               instance=lambda:INSTANCE,gui=lambda uid:{'PATH':'/usr/bin:/bin','XDG_RUNTIME_DIR':'/run/user/'+str(uid)},
               fresh=self.freshness,process_check=lambda *args:self.processes.append(args))
    def read(self,path,*args):
        if path==fs.REPORT:return json.dumps(self.report).encode()
        return self.files[path]
    def freshness(self):fs.need(self.fresh)
    def binding(self):return {k:self.report[k] for k in ('policySha256','documentSha256','instanceId')}
    def fake_run(self,argv,env=None,timeout=3):
        self.commands.append(argv)
        if argv[0]=='/usr/bin/systemd-run':self.active=True;return 0,''
        if 'stop' in argv:self.active=False;return 0,''
        if not self.active:return 1,'Id='+fs.UNIT+'\nLoadState=not-found\n'
        binding=self.binding()
        values={'Id':fs.UNIT,'LoadState':'loaded','ActiveState':'active','SubState':'running','Transient':'yes',
                'Description':'foreign' if self.conflict else fs.description(binding),
                'ExecStart':'{ path=/usr/bin/foot ; argv[]='+' '.join(fs.terminal_argv(binding))+' ; ignore_errors=no ; start_time=fixture ; }',
                'User':'','RuntimeMaxUSec':'3min','KillMode':'control-group','MainPID':'2222','NoNewPrivileges':'no'}
        return 0,'\n'.join(k+'='+v for k,v in values.items())
    def test_pending_exact_notice_and_display_only(self):
        value=self.controller.status();self.assertEqual(value['status'],'pending');self.assertFalse(value['nativeAcceptanceEvidence'])
        self.assertEqual(hashlib.sha256(value['noticeText'].encode()).hexdigest(),fs.NOTICE_SHA256)
        self.assertNotIn('accountName',value);self.assertFalse(self.controller.admitted())
    def test_stale_missing_changed_identity_and_source_refuse(self):
        self.fresh=False;self.assertEqual(self.controller.status()['status'],'unavailable');self.fresh=True
        self.report['instanceId']='22345678-1234-4234-9234-123456789abc';self.assertEqual(self.controller.status()['status'],'unavailable');self.report['instanceId']=INSTANCE
        self.files[fs.NOTICE]+=b'changed';self.assertEqual(self.controller.status()['status'],'unavailable')
    def test_accept_requires_exact_explicit_binding_no_arbitrary_fields(self):
        good={**self.binding(),'accepted':True}
        for patch in [{'accepted':False},{'instanceId':'22345678-1234-4234-9234-123456789abc'},{'argv':['arbitrary']},{'password':'fixture-prohibited'}]:
            with self.subTest(patch=patch),self.assertRaises(fs.FirstStartError):self.controller.perform('accept',{**good,**patch})
        self.assertFalse(any(c[0]=='/usr/bin/systemd-run' for c in self.commands))
    def test_single_fixed_password_terminal_and_no_capture(self):
        value=self.controller.perform('accept',{**self.binding(),'accepted':True});self.assertEqual(value['status'],'awaiting-password')
        self.controller.perform('accept',{**self.binding(),'accepted':True})
        launches=[c for c in self.commands if c[0]=='/usr/bin/systemd-run'];self.assertEqual(len(launches),1)
        command=launches[0];self.assertIn('--property=StandardOutput=null',command);self.assertIn('--property=StandardError=null',command)
        self.assertIn('--property=RuntimeMaxSec=180',command);self.assertIn('--property=NoNewPrivileges=no',command)
        self.assertEqual(command[-len(fs.terminal_argv(self.binding())):],fs.terminal_argv(self.binding()))
        self.assertNotIn('/usr/bin/sudo',command);self.assertNotIn('sh',command);self.assertNotIn('-c',command)
        self.assertTrue(self.processes)
    def test_cancel_only_matching_unit_leaves_pending(self):
        original=copy.deepcopy(self.report);self.active=True
        value=self.controller.perform('cancel',self.binding())
        self.assertEqual(value['status'],'pending');self.assertEqual(self.report,original)
        self.assertIn(['/usr/bin/systemctl','--user','stop',fs.UNIT],self.commands)
    def test_foreign_unit_is_not_stopped_or_replaced(self):
        self.active=True;self.conflict=True
        with self.assertRaises(fs.FirstStartError):self.controller.perform('cancel',self.binding())
        self.assertFalse(any('stop' in c or c[0]=='/usr/bin/systemd-run' for c in self.commands))
    def test_password_cancel_or_timeout_does_not_complete(self):
        self.controller.perform('accept',{**self.binding(),'accepted':True});self.active=False
        value=self.controller.status();self.assertEqual(value['status'],'pending');self.assertFalse(value['complete'])
    def test_completed_projection_and_rollback_refusal(self):
        self.report.update(phase='complete',status='complete',complete=True,consentAccepted=True,completedAt='2021-01-01T00:00:00Z',sequence=6)
        self.assertTrue(self.controller.admitted());self.assertFalse(self.controller.status()['nativeAcceptanceEvidence'])
        self.report.update(phase='notice',status='pending',complete=False,consentAccepted=False,completedAt=None,sequence=5)
        self.assertFalse(self.controller.admitted())
    def test_public_instance_uses_only_public_files(self):
        seen=[]
        def read(path,*args):
            seen.append(str(path))
            if str(path)=='/etc/machine-id':return b'1'*32+b'\n'
            return b'ssh-ed25519 ZmFrZQ== fixture\n'
        value=fs.public_instance(read);self.assertEqual(len(value),36)
        self.assertTrue(all(p=='/etc/machine-id' or p.endswith('.pub') for p in seen))
    def test_root_state_and_secret_inputs_absent_from_backend(self):
        text=(SOURCE/'app/backend/first_start.py').read_text()
        self.assertNotIn('/var/lib/decrypt-firstboot/state.json',text)
        self.assertNotIn('sudo -n',text);self.assertNotIn('NOPASSWD',text)
        self.assertNotIn('input(',text);self.assertNotIn('getpass',text)
    def test_api_gate_blocks_workspace_mutations_and_state(self):
        class Handler:
            _handle_api=server.Handler._handle_api
            def __init__(self,path,verb):
                self.path=path;self.command=verb;self.connection=types.SimpleNamespace(settimeout=lambda *_:None)
                self.headers=Message();self.headers['X-CSRF-Token']='fixture';self.replies=[]
                self.server=types.SimpleNamespace(first_start=self_controller)
                self.boundary=lambda:None;self.session=lambda *args,**kwargs:('id','fixture');self.read_json=lambda:{}
            def reply(self,status,payload,*args):self.replies.append((status,payload))
        self_controller=self.controller
        for path,verb in [('/api/state','GET'),('/api/onboarding','POST'),('/api/guardian/connect','POST'),('/api/trading/start','POST')]:
            h=Handler(path,verb);h._handle_api();self.assertEqual(h.replies[0][0],403);self.assertEqual(h.replies[0][1]['error']['code'],'first_start_pending')
        h=Handler('/api/first-start/status','GET');h._handle_api();self.assertEqual(h.replies[0][0],200)
    def test_ui_workspace_hooks_are_inside_consent_gate(self):
        text=(SOURCE/'app/ui/src/App.tsx').read_text();self.assertIn('return <FirstStartNotice><Workspace/></FirstStartNotice>',text)
        self.assertLess(text.index('function Workspace()'),text.index("api<State>('/state')"))
        ui=(SOURCE/'app/ui/src/FirstStartNotice.tsx').read_text();self.assertIn('crypto.subtle.digest',ui);self.assertIn('disabled={!accepted || busy}',ui)
        self.assertNotIn('type="password"',ui);self.assertIn('if (display?.complete) return <>{children}</>',ui)

if __name__=='__main__':unittest.main(verbosity=2)
