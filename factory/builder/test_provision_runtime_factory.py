import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

s=importlib.util.spec_from_file_location('runtime',Path(__file__).with_name('provision_runtime.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

class RuntimeFactoryTests(unittest.TestCase):
    def test_private_pinned_host_binding_permissions_and_protected_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp).resolve()/'private.json';value={'schema':'decrypt.runtime-build-host.v1','targetMachineSha256':'a'*64,'protectedMachineSha256':['b'*64],
              'targetPythonIdentity':{'version':[3,14,0],'cacheTag':'cpython-314','soabi':'cpython-314-aarch64-linux-gnu'}}
            p.write_text(json.dumps(value));p.chmod(0o600);m.HOST_BINDING_PATH=p;m.HOST_BINDING_SHA256=hashlib.sha256(p.read_bytes()).hexdigest()
            self.assertEqual(m.private_host_binding(),value)
            p.chmod(0o644)
            with self.assertRaises(m.Refused):m.private_host_binding()
            p.chmod(0o600);value['protectedMachineSha256']=['a'*64];p.write_text(json.dumps(value));m.HOST_BINDING_SHA256=hashlib.sha256(p.read_bytes()).hexdigest()
            with self.assertRaises(m.Refused):m.private_host_binding()

    def test_npm_command_is_offline_and_scriptless(self):
        args=m.npm_command(Path('/fixture/node'),Path('/fixture/work'))
        self.assertIn('--offline',args);self.assertIn('--ignore-scripts',args);self.assertIn('--maxsockets=1',args)
        self.assertFalse(any(x.startswith('--registry=') for x in args))
        self.assertFalse(hasattr(m,'apply'))

    def test_all_archive_hashes_checked_before_cache_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'npm').mkdir();(root/'npm-archives').mkdir()
            import base64
            url='https://registry.npmjs.org/example/-/example-1.tgz';raw=b'fixture archive'
            record={'resolved':url,'integrity':'sha512-'+base64.b64encode(hashlib.sha512(raw).digest()).decode()}
            (root/'npm/package-lock.json').write_text(json.dumps({'packages':{'':{},'node_modules/example':record}}))
            archive=root/'npm-archives'/(m.sha(url.encode())+'.tgz');archive.write_bytes(raw)
            with patch.object(m,'command') as run:
                m.prepare_offline_npm_cache(root,root/'node',root/'work',{});self.assertEqual(run.call_count,1)
                self.assertIn('--offline',run.call_args.args[0])
            archive.write_bytes(b'changed')
            with patch.object(m,'command') as run:
                with self.assertRaises(m.Refused):m.prepare_offline_npm_cache(root,root/'node',root/'work',{})
                run.assert_not_called()

    def test_bundled_npm_dependencies_need_no_separate_archive(self):
        # OpenClaw 2026.9.9 added a direct dependency on npm itself, which
        # ships its whole tree (inBundle=true) inside its own tarball -- no
        # resolved/integrity of its own, so prepare_offline_npm_cache must
        # skip it rather than KeyError on a missing 'resolved'.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'npm').mkdir();(root/'npm-archives').mkdir()
            bundled={'version':'1.0.0','inBundle':True,'license':'MIT'}
            (root/'npm/package-lock.json').write_text(json.dumps(
                {'packages':{'':{},'node_modules/npm/node_modules/example':bundled}}))
            with patch.object(m,'command') as run:
                m.prepare_offline_npm_cache(root,root/'node',root/'work',{})
            run.assert_not_called()

    def test_python_metadata_aggregate_matches_committed_files(self):
        # Regression guard: if cryptography.json/cffi.json/pycparser.json/
        # SecretStorage.json/jeepney.json are edited without updating
        # PYTHON_METADATA_SHA256, verify_inputs must start refusing.
        here = Path(__file__).parent
        records = {name + '.json': hashlib.sha256((here / (name + '.json')).read_bytes()).hexdigest()
                   for name in m.VERSIONS}
        self.assertEqual(m.python_metadata_aggregate(records), m.PYTHON_METADATA_SHA256)

    def test_python_metadata_aggregate_detects_tampering(self):
        records = {name + '.json': 'a' * 64 for name in m.VERSIONS}
        self.assertNotEqual(m.python_metadata_aggregate(records), m.PYTHON_METADATA_SHA256)
        tampered = {name + '.json': hashlib.sha256((Path(__file__).parent / (name + '.json')).read_bytes()).hexdigest()
                   for name in m.VERSIONS}
        tampered['cffi.json'] = hashlib.sha256(b'replaced').hexdigest()
        self.assertNotEqual(m.python_metadata_aggregate(tampered), m.PYTHON_METADATA_SHA256)

    def test_retained_wheel_exact_bytes_and_no_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'wheels').mkdir();raw=b'wheel';(root/'wheels/fixture.whl').write_bytes(raw)
            row={'filename':'fixture.whl','sha256':m.sha(raw)};m.retained_wheel(row,root/'copied.whl',root)
            self.assertEqual((root/'copied.whl').read_bytes(),raw)
            for row in [{'filename':'../escape','sha256':m.sha(raw)},{'filename':'fixture.whl','sha256':'0'*64}]:
                with self.assertRaises(m.Refused):m.retained_wheel(row,root/'bad.whl',root)

if __name__=='__main__':unittest.main()
