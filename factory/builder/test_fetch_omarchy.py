"""Small synthetic source-authentication fixtures; no network or VM actions."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('fetch_omarchy', Path(__file__).with_name('fetch_omarchy.py'))
m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)


def fixture():
    blobs = {'LICENSE': (b'public fixture\n', '100644'), 'bin/run': (b'#!/bin/sh\ntrue\n', '100755'), 'link': (b'bin/run', '120000')}
    rows = [{'path': n, 'mode': mode, 'type': 'blob', 'size': len(raw), 'sha': m.oid('blob', raw)} for n, (raw, mode) in blobs.items()]
    row = next(r for r in rows if r['path'] == 'bin/run')
    tree = m.oid('tree', b'100755 run\0' + bytes.fromhex(row['sha']))
    rows.append({'path': 'bin', 'mode': '040000', 'type': 'tree', 'sha': tree})
    top = sorted([r for r in rows if '/' not in r['path']], key=lambda r: r['path'] + ('/' if r['type'] == 'tree' else ''))
    root = m.oid('tree', b''.join((r['mode'].lstrip('0') + ' ' + r['path']).encode() + b'\0' + bytes.fromhex(r['sha']) for r in top))
    return blobs, {'sha': root, 'truncated': False, 'tree': rows}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.blobs, self.tree = fixture()
        self.entries = m.validate_tree(self.tree, self.tree['sha'])

    def archive(self, changes=None, omit=None, extra=None, modes=None):
        path = self.root / 'source.tar.gz'
        with tarfile.open(path, 'w:gz') as t:
            for name, row in self.entries.items():
                if name == omit: continue
                info = tarfile.TarInfo('source/' + name)
                raw = self.blobs.get(name, (b'', ''))[0]
                if changes and name in changes: raw = changes[name]
                if row['type'] == 'tree': info.type = tarfile.DIRTYPE; info.mode = 0o755
                elif row['mode'] == '120000': info.type = tarfile.SYMTYPE; info.linkname = raw.decode(); info.mode = 0o777
                else: info.size = len(raw); info.mode = int(row['mode'][-3:], 8)
                if modes and name in modes: info.mode = modes[name]
                t.addfile(info, io.BytesIO(raw) if info.isfile() else None)
            if extra: t.addfile(tarfile.TarInfo(extra))
        return path

    def verify(self, path):
        return m.verify_archive(path, self.root / 'out.tar', self.entries, 'source', m.Guard())

    def test_full_tree_blob_and_tar_roundtrip(self):
        result = self.verify(self.archive())
        self.assertEqual(result['verifiedBlobs'], 3)
        self.assertEqual(result['archiveSha256'], hashlib.sha256((self.root / 'out.tar').read_bytes()).hexdigest())
        with tarfile.open(self.root / 'out.tar') as t:
            self.assertEqual(set(t.getnames()), set(self.entries))
            self.assertEqual(t.extractfile('bin/run').read(), self.blobs['bin/run'][0])
        with self.assertRaises(FileExistsError): self.verify(self.root / 'source.tar.gz')

    def test_codeload_group_write_only_and_canonical_modes(self):
        self.verify(self.archive(modes={'LICENSE': 0o664, 'bin/run': 0o775, 'bin': 0o775}))
        with tarfile.open(self.root / 'out.tar') as t:
            self.assertEqual(t.getmember('LICENSE').mode, 0o644)
            self.assertEqual(t.getmember('bin/run').mode, 0o755)
        for index, (name, mode) in enumerate([('LICENSE', 0o666), ('LICENSE', 0o4644),
                ('LICENSE', 0o2644), ('LICENSE', 0o1644), ('LICENSE', 0o755),
                ('bin/run', 0o644), ('bin/run', 0o777), ('bin/run', 0o4755),
                ('bin/run', 0o754), ('bin', 0o777), ('bin', 0o2755), ('link', 0o4777)]):
            with self.subTest(name=name, mode=oct(mode)), self.assertRaisesRegex(ValueError, 'mode mismatch'):
                m.verify_archive(self.archive(modes={name: mode}), self.root / ('bad-mode-'+str(index)+'.tar'),
                                 self.entries, 'source', m.Guard())

    def test_corrupt_blob_retains_failure_output(self):
        with self.assertRaisesRegex(ValueError, 'blob hash'): self.verify(self.archive(changes={'LICENSE': b'public fixturf\n'}))
        self.assertTrue((self.root / 'out.tar').exists())

    def test_missing_blob(self):
        with self.assertRaisesRegex(ValueError, 'missing'): self.verify(self.archive(omit='LICENSE'))

    def test_extra_traversal(self):
        with self.assertRaises(ValueError): self.verify(self.archive(omit='LICENSE', extra='source/../bad'))

    def test_symlink_escape(self):
        with self.assertRaisesRegex(ValueError, 'escaping'): self.verify(self.archive(changes={'link': b'../../outside'}))

    def test_tree_substitution_truncation_duplicate_bounds_and_gitlink(self):
        variants = []
        a = copy.deepcopy(self.tree); a['tree'][0]['sha'] = '0' * 40; variants.append(a)
        a = copy.deepcopy(self.tree); a['truncated'] = True; variants.append(a)
        a = copy.deepcopy(self.tree); a['tree'].append(a['tree'][0]); variants.append(a)
        a = copy.deepcopy(self.tree); a['tree'][0]['size'] = m.MAX_BLOB + 1; variants.append(a)
        a = copy.deepcopy(self.tree); a['tree'][0]['mode'] = '160000'; a['tree'][0]['type'] = 'commit'; variants.append(a)
        a = copy.deepcopy(self.tree); a['tree'][0]['path'] = '../bad'; variants.append(a)
        for doc in variants:
            with self.subTest(doc=doc), self.assertRaises(ValueError): m.validate_tree(doc, self.tree['sha'])

    def test_chained_symlink_escape_and_cycle(self):
        with self.assertRaisesRegex(ValueError, 'escaping'):
            m.validate_links({'a': '.', 'dir/link': '../a/../outside'})
        with self.assertRaisesRegex(ValueError, 'cyclic'):
            m.validate_links({'a': 'b', 'b': 'a'})

    def test_pause_symlink_and_deadline(self):
        marker = self.root / 'pause'; marker.symlink_to('missing')
        with self.assertRaisesRegex(ValueError, 'pause'): m.Guard(marker).check()
        guard = m.Guard(); guard.deadline = 0
        with self.assertRaisesRegex(ValueError, 'deadline'): guard.check()

    def test_network_failure_retained_and_no_resume(self):
        out = self.root / 'attempt'
        with patch.object(m, 'download', side_effect=ValueError('synthetic failure')):
            with self.assertRaises(ValueError): m.acquire('basecamp/omarchy', 'a' * 40, out)
        self.assertEqual(json.loads((out / 'failure.json').read_text())['status'], 'failed')
        with self.assertRaises(FileExistsError): m.acquire('basecamp/omarchy', 'a' * 40, out)

    def test_full_acquisition_binding(self):
        archive = self.archive(); commit = 'a' * 40; out = self.root / 'attempt'
        # Use the fixture prefix expected by the public repository input.
        with tarfile.open(archive, 'r:gz') as t:
            values = [(i, t.extractfile(i).read() if i.isfile() else None) for i in t]
        with tarfile.open(archive, 'w:gz') as t:
            for i, data in values:
                i.name = i.name.replace('source/', 'omarchy-' + commit + '/', 1)
                t.addfile(i, io.BytesIO(data) if data is not None else None)
        def download(url, dest, maximum, guard):
            if '/commits/' in url: raw = json.dumps({'sha': commit, 'tree': {'sha': self.tree['sha']}}).encode()
            elif '/trees/' in url: raw = json.dumps(self.tree).encode()
            else: raw = archive.read_bytes()
            dest.write_bytes(raw)
        with patch.object(m, 'download', side_effect=download): result = m.acquire('basecamp/omarchy', commit, out)
        self.assertEqual(result['status'], 'verified'); self.assertFalse(result['localGitExecuted'])
        self.assertTrue((out / 'receipt.json').exists())
        pins = {name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                for name in ('commit.json', 'tree.json', 'upstream.tar.gz')}
        with patch.object(m, 'download', side_effect=AssertionError('offline reuse must never access network')):
            reused = m.acquire('basecamp/omarchy', commit, self.root / 'reuse', retained_source=out, retained_pins=pins)
            self.assertFalse(reused['networkAccessed'])
            self.assertEqual(reused['archiveSha256'], result['archiveSha256'])
            self.assertEqual(set(reused['retainedAcquisition']), set(pins))
            bad = dict(pins); bad['upstream.tar.gz'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                m.acquire('basecamp/omarchy', commit, self.root / 'bad-reuse', retained_source=out, retained_pins=bad)
        self.assertTrue((self.root / 'bad-reuse/failure.json').exists())
        self.assertFalse((self.root / 'bad-reuse/receipt.json').exists())
        self.assertEqual(pins, {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in pins})

    def test_local_git_retired_and_url_redirect_refused(self):
        with self.assertRaisesRegex(ValueError, 'retired'): m.make_archive('anything')
        with self.assertRaisesRegex(ValueError, 'allowlisted'):
            m.download('file:///etc/passwd', self.root / 'out', 1024, m.Guard())
        with self.assertRaisesRegex(ValueError, 'redirect'):
            m.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.org')


if __name__ == '__main__': unittest.main()
