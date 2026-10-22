"""seed_runtime_inputs.py: layout and pin checks, with downloads faked (no network)."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = load('seed_runtime_inputs')


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.meta = self.root / 'meta'
        (self.meta / 'npm').mkdir(parents=True)
        lock = {'lockfileVersion': 3, 'packages': {'': {'dependencies': {'openclaw': '2026.9.9'}}}}
        (self.meta / 'npm/package-lock.json').write_text(json.dumps(lock))
        (self.meta / 'npm/package.json').write_text(json.dumps({'dependencies': {'openclaw': '2026.9.9'}}))
        tarball = 'https://registry.npmjs.org/openclaw/-/openclaw-2026.9.9.tgz'
        (self.meta / 'openclaw.json').write_text(json.dumps({'name': 'openclaw', 'version': '2026.9.9',
                                                             'dist': {'tarball': tarball}}))
        self.prov = load('provision_runtime')
        self.prov.NPM_LOCK_HASH = hashlib.sha256((self.meta / 'npm/package-lock.json').read_bytes()).hexdigest()
        self.prov.NODE_NAME, self.prov.NODE_HASH = 'node-v24.19.0-linux-x64', hashlib.sha256(b'node').hexdigest()
        self.prov.TARGET_ARCH = 'x86_64'
        self.prov.OPENCLAW_SHA512 = hashlib.sha512(b'openclaw').hexdigest()
        self.fetched = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, url, destination, algorithm, expected):
        self.fetched.append((url, destination.relative_to(self.root / 'out').as_posix(), algorithm))
        destination.write_bytes(b'node' if 'nodejs.org' in url else b'openclaw' if 'openclaw' in url else b'wheel')

    def test_layout_node_metadata_and_x86_wheels(self):
        tags = ['cp314-cp314-manylinux_2_17_x86_64', 'cp311-abi3-manylinux_2_17_x86_64', 'py3-none-any']
        info = seed.seed(self.meta, self.root / 'out', self.prov, tags, fetch=self.fake)
        out = self.root / 'out'
        self.assertEqual(sorted(info['files']), sorted(self.prov.INPUT_FILES))
        node = json.loads((out / 'node.json').read_text())
        self.assertEqual(node['files'], [{'filename': 'node-v24.19.0-linux-x64.tar.xz', 'sha256': self.prov.NODE_HASH}])
        self.assertEqual(self.fetched[0][:2], ('https://nodejs.org/dist/v24.19.0/node-v24.19.0-linux-x64.tar.xz',
                                               'archives/node-v24.19.0-linux-x64.tar.xz'))
        wheels = [path for _, path, _ in self.fetched if path.startswith('wheels/')]
        self.assertEqual(len(wheels), len(self.prov.VERSIONS))
        self.assertFalse([w for w in wheels if 'aarch64' in w])
        with self.assertRaises(SystemExit):   # never reuses an existing directory
            seed.seed(self.meta, out, self.prov, tags, fetch=self.fake)

    def test_wrong_npm_lock_refused(self):
        self.prov.NPM_LOCK_HASH = '0' * 64
        with self.assertRaisesRegex(SystemExit, 'NPM_LOCK_HASH'):
            seed.seed(self.meta, self.root / 'out', self.prov, ['py3-none-any'], fetch=self.fake)
        self.assertFalse((self.root / 'out').exists())

    def test_download_refuses_unpinned_hosts(self):
        for url in ('http://nodejs.org/x', 'https://example.org/x', 'https://registry.npmjs.org/x?token=1'):
            with self.subTest(url=url), self.assertRaises(SystemExit):
                seed.download(url, self.root / 'x', 'sha256', '0' * 64)


if __name__ == '__main__':
    unittest.main()
