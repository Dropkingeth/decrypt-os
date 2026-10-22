#!/usr/bin/env python3
"""Fixture-only byte staging tests. No curl/network, VM, package scripts or install."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent;sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location('fetcher',HERE/'fetch_runtime_inputs.py');f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
real=f.load_provisioner(HERE/'provision_runtime.py')

def sri(raw):return 'sha512-'+base64.b64encode(hashlib.sha512(raw).digest()).decode()

class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='.fetch-fixture-',dir=HERE);self.root=Path(self.tmp.name)
        self.inputs=self.root/'inputs';self.inputs.mkdir(mode=0o700);(self.inputs/'archives').mkdir();(self.inputs/'npm').mkdir()
        self.node=b'fixture-node';self.openclaw=b'fixture-openclaw';self.npm=b'fixture-npm';self.wheel=b'fixture-wheel'
        node_name='node-v24.19.0-linux-arm64';self.url='https://registry.npmjs.org/openclaw/-/openclaw-2026.9.9.tgz'
        self.lock={'name':'fixture','lockfileVersion':3,'packages':{'':{'dependencies':{'openclaw':'2026.9.9'}},
                   'node_modules/openclaw':{'resolved':self.url,'integrity':sri(self.openclaw)},
                   'node_modules/example':{'resolved':'https://registry.npmjs.org/example/-/example-1.0.0.tgz','integrity':sri(self.npm)}}}
        f.write(self.inputs/'npm/package-lock.json',f.canonical(self.lock));f.write(self.inputs/'npm/package.json',f.canonical({'dependencies':{'openclaw':'2026.9.9'}}))
        f.write(self.inputs/'node.json',f.canonical({'name':'node','source':'https://nodejs.org/dist/v24.19.0/SHASUMS256.txt','files':[{'filename':node_name+'.tar.xz','sha256':f.sha(self.node)}]}));f.write(self.inputs/'openclaw.json',f.canonical({'name':'openclaw','version':'2026.9.9','dist':{'tarball':self.url,'integrity':sri(self.openclaw)}}))
        for name,version in real.VERSIONS.items():
            tag={'cryptography':'cp311-abi3-manylinux_2_17_aarch64','cffi':'cp314-cp314-manylinux_2_17_aarch64','pycparser':'py3-none-any',
                 'SecretStorage':'py3-none-any','jeepney':'py3-none-any'}[name]
            filename=name+'-'+version+'-'+tag+'.whl'
            row={'filename':filename,'type':'bdist_wheel','url':'https://files.pythonhosted.org/packages/fixture/'+filename,'sha256':f.sha(self.wheel)}
            f.write(self.inputs/(name+'.json'),f.canonical({'name':name,'version':version,'files':[row]}))
        self.provisioner=types.SimpleNamespace(INPUT_FILES=real.INPUT_FILES,NPM_LOCK_HASH=f.sha((self.inputs/'npm/package-lock.json').read_bytes()),
              VERSIONS=real.VERSIONS,NODE_NAME=node_name,NODE_HASH=f.sha(self.node),OPENCLAW_SHA512=hashlib.sha512(self.openclaw).hexdigest(),select_wheels=real.select_wheels)
        self.public={'schema':'decrypt.installer-v2.public-inputs.v1','files':[]}
        for name,data in [(node_name+'.tar.xz',self.node),('openclaw-2026.9.9.tgz',self.openclaw)]:
            f.write(self.inputs/'archives'/name,data);self.public['files'].append({'destination':'dependencies/archives/'+name,'bytes':len(data),'sha256':f.sha(data),'role':'required-runtime'})
        self.public_path=self.root/'public.json';f.write(self.public_path,f.canonical(self.public))
        self.target={'schema':'decrypt.runtime-target-python.v1','execution':'native-target-observation','platform':'linux-aarch64',
                     'targetPythonIdentity':{'version':[3,14,0],'cacheTag':'cpython-314','soabi':'cpython-314-aarch64-linux-gnu'},
                     'packagingAvailable':True,'supportedTags':['cp314-cp314-manylinux_2_17_aarch64','cp311-abi3-manylinux_2_17_aarch64','py3-none-any'],'fixtureOnly':True}
        self.target_path=self.root/'target.json';f.write(self.target_path,f.canonical(self.target))
        self.metadata_sha=f.sha(json.dumps({name:f.sha((self.inputs/name).read_bytes()) for name in real.INPUT_FILES},sort_keys=True,separators=(',',':')).encode())
        self.curl=self.root/'fake-curl';f.write(self.curl,b'fixture-not-executable-code');self.curl.chmod(0o700)
        self.plan=self.make_plan();self.output=self.root/'output'
    def tearDown(self):self.tmp.cleanup()
    def make_plan(self,additional=(),refetch=False):
        return f.make_plan(self.provisioner,self.inputs,self.metadata_sha,self.public_path,f.sha(self.public_path.read_bytes()),self.target_path,f.sha(self.target_path.read_bytes()),additional,refetch_public_runtime=refetch)
    def download(self,binary,destination,row):
        f.write(destination,self.wheel if row['role']=='wheel' else self.npm)
    def execute(self,download=None):
        space=types.SimpleNamespace(f_bavail=100*1024**3,f_frsize=1)
        with mock.patch.object(f.os,'statvfs',return_value=space):
            return f.fetch(self.plan,self.inputs,self.output,self.curl,f.sha(self.curl.read_bytes()),download or self.download)
    def test_reuses_exact_reviewed_provisioner(self):
        self.assertEqual(f.sha((HERE/'provision_runtime.py').read_bytes()),f.PROVISIONER_SHA256)
        bad=self.root/'wrong.py';f.write(bad,b'raise RuntimeError("must not run")')
        with self.assertRaisesRegex(f.Refused,'input_pin'):f.load_provisioner(bad)
    def test_native_tag_selection_and_missing_tags_refuse(self):
        self.assertEqual(len(self.plan['selectedWheels']),len(real.VERSIONS))
        for change in [lambda t:t.update(packagingAvailable=False),lambda t:t.update(supportedTags=[]),
                       lambda t:t['targetPythonIdentity'].update(soabi='cpython-311-aarch64-linux-gnu'),
                       lambda t:t.update(supportedTags=['cp311-cp311-manylinux_2_17_aarch64'])]:
            t=copy.deepcopy(self.target);change(t)
            with self.subTest(change=change),self.assertRaises(f.Refused):f.target_tags(t)
    def test_metadata_pin_change_refused(self):
        (self.inputs/'node.json').write_bytes(b'changed')
        with self.assertRaisesRegex(f.Refused,'metadata_aggregate'):self.make_plan()
    def test_locked_integrity_and_hosts_rejected(self):
        for url in ['http://registry.npmjs.org/p.tgz','https://registry.npmjs.org/p.tgz?token=private','https://user:pass@registry.npmjs.org/p.tgz','https://localhost/p.tgz']:
            lock=copy.deepcopy(self.lock);lock['packages']['node_modules/example']['resolved']=url
            with self.subTest(url=url),self.assertRaises(f.Refused):f.npm_rows(lock)
        with self.assertRaises(f.Refused):f.sri('sha512-YQ==')
    def test_additional_ui_lock_union_and_conflict(self):
        ui={'lockfileVersion':3,'packages':{'':{},'node_modules/shared':{'resolved':self.url,'integrity':sri(self.openclaw)},'node_modules/ui':{'resolved':'https://registry.npmjs.org/ui/-/ui-1.tgz','integrity':sri(b'ui')}}}
        path=self.root/'ui-lock.json';f.write(path,f.canonical(ui));plan=self.make_plan([(path,f.sha(path.read_bytes()))])
        self.assertEqual(len(plan['archives']),len(self.plan['archives'])+1);self.assertEqual(len(plan['additionalNpmLockSha256']),1)
        ui['packages']['node_modules/shared']['integrity']=sri(b'conflict');path2=self.root/'bad-ui-lock.json';f.write(path2,f.canonical(ui))
        with self.assertRaisesRegex(f.Refused,'conflicting_npm'):self.make_plan([(path2,f.sha(path2.read_bytes()))])
    def test_retained_openclaw_reused_for_npm_sri(self):
        row=next(r for r in self.plan['archives'] if r['path']=='npm-archives/'+f.sha(self.url.encode())+'.tgz')
        self.assertEqual(row['retainedRelative'],'archives/openclaw-2026.9.9.tgz');self.assertEqual(row['expectedBytes'],len(self.openclaw))
    def test_curl_disable_first_and_no_retry_scripts_or_environment(self):
        argv=f.curl_argv(self.curl,'https://registry.npmjs.org/example/-/example-1.tgz',100)
        self.assertEqual(argv[1],'--disable');self.assertEqual(argv[argv.index('--retry')+1],'0')
        self.assertEqual(argv[argv.index('--limit-rate')+1],'20M');self.assertNotIn('--location',argv);self.assertNotIn('--continue-at',argv)
        self.assertNotIn('install',argv);self.assertNotIn('--config',argv)
    def test_materializes_only_verified_bytes_no_runtime_claim(self):
        receipt=self.execute();self.assertTrue(receipt['dependencyBytesVerified']);self.assertFalse(receipt['nativeRuntimeBuildVerified']);self.assertFalse(receipt['signatureVerification'])
        self.assertEqual(len(receipt['archives']),len(self.plan['archives']));self.assertNotIn(str(self.root),json.dumps(receipt))
        self.assertNotIn(str(self.root),json.dumps(self.plan))
        self.assertEqual((self.output/'archives/openclaw-2026.9.9.tgz').read_bytes(),self.openclaw)
        sizes={r['role']:r['sizeBinding'] for r in receipt['archives']}
        self.assertEqual(sizes['wheel'],'measured-bounded-hash-verified');self.assertEqual(sizes['retained-runtime'],'origin-pinned')
    def test_integrity_failure_retains_partial_never_accepts(self):
        def bad(binary,destination,row):f.write(destination,b'wrong bytes')
        with self.assertRaisesRegex(f.Refused,'archive_integrity'):self.execute(bad)
        self.assertTrue((self.output/'FETCH-FAILURE.json').exists());self.assertFalse((self.output/'FETCH-RECEIPT.json').exists())
        self.assertEqual((self.inputs/'archives/openclaw-2026.9.9.tgz').read_bytes(),self.openclaw)
    def test_changed_retained_archive_refused(self):
        (self.inputs/'archives/openclaw-2026.9.9.tgz').write_bytes(b'changed')
        with self.assertRaisesRegex(f.Refused,'retained_archive_size'):self.execute()
    def test_existing_output_and_insufficient_space_refused(self):
        self.output.mkdir()
        with self.assertRaisesRegex(f.Refused,'new_output'):self.execute()
        self.output.rmdir()
        with mock.patch.object(f.os,'statvfs',return_value=types.SimpleNamespace(f_bavail=1,f_frsize=1)):
            with self.assertRaisesRegex(f.Refused,'insufficient_space'):f.fetch(self.plan,self.inputs,self.output,self.curl,f.sha(self.curl.read_bytes()),self.download)
        self.assertFalse(self.output.exists())
    def test_observed_size_bound_and_changed_curl_refused(self):
        row=self.plan['archives'][0];path=self.root/'oversize';f.write(path,b'x'*(row['maximumBytes']+1))
        with self.assertRaisesRegex(f.Refused,'archive_size'):f.check_archive(path,row)
        with self.assertRaisesRegex(f.Refused,'input_pin'):f.fetch(self.plan,self.inputs,self.output,self.curl,'0'*64,self.download)
    def test_parent_death_setup_and_race_refusal(self):
        calls=[]
        def prctl(*args):calls.append(args);return 0
        with mock.patch.object(f.os,'getppid',return_value=100),mock.patch.object(f.os,'_exit',side_effect=RuntimeError('exit')) as terminate:
            f.child_parent_death(prctl,100);terminate.assert_not_called()
            self.assertEqual(calls,[(1,signal.SIGKILL,0,0,0)])
            with self.assertRaisesRegex(RuntimeError,'exit'):f.child_parent_death(prctl,99)
            terminate.assert_called_with(125)
        with mock.patch.object(f.os,'_exit',side_effect=RuntimeError('exit')) as terminate:
            with self.assertRaises(RuntimeError):f.child_parent_death(lambda *_:-1,100)
            terminate.assert_called_once_with(126)
    def test_curl_privileged_or_non_native_refused(self):
        with mock.patch.object(f.sys,'platform','linux'):
            with self.assertRaisesRegex(f.Refused,'native_elf'):f.curl_containment(self.curl)
            self.curl.write_bytes(b'\x7fELFfixture');self.curl.chmod(0o4700)
            with self.assertRaisesRegex(f.Refused,'native_elf'):f.curl_containment(self.curl)
            self.curl.chmod(0o700)
            with mock.patch.object(f.os,'listxattr',return_value=['security.capability'],create=True):
                with self.assertRaisesRegex(f.Refused,'file_capabilities'):f.curl_containment(self.curl)
    @unittest.skipUnless(sys.platform.startswith('linux'),'actual PDEATHSIG fixture requires Linux; no native Mac fallback')
    def test_actual_child_stops_when_controller_exits(self):
        # Linux-local only, no network/curl/VM: a disposable controller spawns a
        # sleeper under the same containment then exits. Only its child is killed.
        child_code='import time;time.sleep(10)'
        source=str(HERE/'fetch_runtime_inputs.py')
        code="import importlib.util,subprocess,sys,ctypes,os;sp=importlib.util.spec_from_file_location('f',sys.argv[1]);f=importlib.util.module_from_spec(sp);sp.loader.exec_module(f);lib=ctypes.CDLL(None);parent=os.getpid();p=subprocess.Popen([sys.executable,'-c',sys.argv[2]],preexec_fn=lambda:f.child_parent_death(lib.prctl,parent),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid,flush=True);os._exit(0)"
        controller=subprocess.run([sys.executable,'-I','-B','-c',code,source,child_code],capture_output=True,text=True,timeout=3)
        self.assertEqual(controller.returncode,0);pid=int(controller.stdout.strip())
        import time
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            try:state=Path('/proc/'+str(pid)+'/stat').read_text().split()[2]
            except FileNotFoundError:return
            if state=='Z':return
            time.sleep(0.02)
        self.fail('contained child remained alive after controller exit')
    def test_explicit_public_refetch_and_local_npm_reuse(self):
        self.assertEqual(self.plan['runtimeArchiveMode'],'retained-copy')
        self.plan=self.make_plan(refetch=True);self.assertEqual(self.plan['runtimeArchiveMode'],'explicit-public-refetch')
        calls=[]
        def download(binary,destination,row):
            calls.append(row['url'])
            if row['url']==f.NODE_URL:data=self.node
            elif row['url']==self.url:data=self.openclaw
            elif row['role']=='wheel':data=self.wheel
            else:data=self.npm
            f.write(destination,data)
        receipt=self.execute(download)
        self.assertEqual(calls.count(self.url),1);self.assertEqual(calls.count(f.NODE_URL),1)
        self.assertEqual(receipt['runtimeArchiveMode'],'explicit-public-refetch')
        with self.assertRaisesRegex(f.Refused,'node_refetch_url_not_pinned'):f.curl_argv(self.curl,'https://nodejs.org/dist/other.tar.xz',100)
    def test_total_budget_refuses_known_overflow(self):
        self.plan['maximumTotalBytes']=1
        with self.assertRaisesRegex(f.Refused,'known_archives_exceed'):self.execute()
        self.assertFalse(self.output.exists())

if __name__=='__main__':unittest.main(verbosity=2)
