"""Export whole setup (GET /api/export) and the "coming soon" flags. VM only; sandbox copies only.

    TMPDIR=$HOME/.cache PYTHONDONTWRITEBYTECODE=1 python3 tools/run_worktests.py tests/test_export.py
"""
import glob
import hashlib
import io
import json
import os
import sys
import tarfile
import threading
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'app'))

import export  # noqa: E402
from test_addlogin import AddLoginBase, env_vals  # noqa: E402

STRONG_SID = 'abcd-efgh-jkmn-pqrs'
SOUP_SECRET = 'sb-' + 'k' * 40


class ExportBase(AddLoginBase):
    def setUp(self):
        super().setUp()
        self.profile.data['soup_service'] = {'unit': 'example-soup.service'}   # Soup as its own unit
        # a Soup drop-in with a secret, the way older drop-ins carry Environment= lines
        d = os.path.join(self.units, 'example-soup.service.d')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'zz-test.conf'), 'w') as f:
            f.write(f'[Service]\nEnvironment=SOUP_BROKER_TOKEN={SOUP_SECRET}\n'
                    f'Environment="SOUP_TICKER=MNQZ2021" TVBRIDGE_PROFILE=/home/x/profile\n')
        with open(os.path.join(self.units, 'example-soup.service'), 'w') as f:
            f.write('[Unit]\nDescription=Soup\n[Service]\nExecStart=/usr/bin/true\n')
        with open(os.path.join(self.units, 'unrelated.service'), 'w') as f:
            f.write('[Service]\nEnvironment=OTHER_TOKEN=' + 'z' * 30 + '\n')

    def unpack(self, data):
        out = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            for m in tar.getmembers():
                self.assertTrue(m.isfile(), m.name)
                self.assertNotIn('..', m.name)
                out[m.name.split('/', 1)[1]] = tar.extractfile(m).read()
        return out

    def known_secrets(self):
        vals = set(self.secrets_now()) | {SOUP_SECRET, STRONG_SID, 'z' * 30}
        for path in glob.glob(self.tb('**', '*.env'), recursive=True):
            vals |= {v for v in env_vals(path).values() if len(v) >= 8 and not v.startswith('/')}
        return vals


class TestExport(ExportBase):
    def test_export_has_everything_and_no_secret(self):
        self.add_c()
        self.do('register_webhook', {'name': 'W2'})
        # a planned webhook with a strong stream ID, also named in a change summary
        reg = json.load(open(self.profile.st('webhooks.json')))
        reg['webhooks'].append({'id': 'w3', 'name': 'W3', 'state': 'planned', 'path': '/webhook/trader-w3',
                                'stream_id': STRONG_SID, 'ticker': 'MNQZ2021', 'leader': None, 'followers': []})
        json.dump(reg, open(self.profile.st('webhooks.json'), 'w'))
        self.e.store.put({'id': 'c-test-sid', 'type': 'rotate_stream_id', 'status': 'cancelled', 'created_at': 1,
                          'summary': f'W3: change stream ID x → {STRONG_SID}', 'plan': {'files': []}})
        tree_before = {p: open(p, 'rb').read() for p in glob.glob(os.path.join(self.tmp, 'tree', '**', '*'),
                                                                     recursive=True) if os.path.isfile(p)}
        audit_before = len(self.e.audit.read(10 ** 6))
        data, name, manifest = self.e.export()
        self.assertTrue(name.startswith('bridge-setup-') and name.endswith('.tar.gz'))
        files = self.unpack(data)
        for want in ('MANIFEST.json', 'admin/webhooks.json', 'admin/profile.json', 'admin/audit.jsonl',
                     'admin/logins/c.json', 'tvbridge/logins.json', 'tvbridge/config.json',
                     'tvbridge/logins/b/config.json', 'tvbridge/logins/c/config.json', 'tvbridge/firing.json',
                     'tvbridge/logins/peers.env', 'tvbridge/logins/firing.env', 'tvbridge/logins/manual-c.env',
                     'tvbridge/logins/c/tvbridge.env', 'tvbridge/logins/b/tvbridge.env', 'soup/quantity-policy.json',
                     'units/tvbridge-daemon@.service', 'units/tvbridge-admin.service.d/manual-tokens.conf',
                     'units/tvbridge-daemon@c.service.d/manual-token.conf', 'units/example-soup.service',
                     'units/example-soup.service.d/zz-test.conf', 'tunnel/config.yml'):
            self.assertIn(want, files, sorted(files))
        self.assertNotIn('units/unrelated.service', files)
        # no secret value anywhere in the archive (raw bytes of every member, the manifest included)
        blob = b'\n'.join(files.values()) + data
        for v in self.known_secrets():
            self.assertNotIn(v.encode(), blob, f'secret of length {len(v)} leaked')
        # env files keep their names, values become <redacted>
        peers = files['tvbridge/logins/peers.env'].decode()
        for n in ('TVBRIDGE_READ_TOKEN_A', 'TVBRIDGE_READ_TOKEN_B', 'TVBRIDGE_READ_TOKEN_C'):
            self.assertIn(f'{n}=<redacted>', peers)
        self.assertIn('# Read-only login tokens', peers)
        unit = files['units/example-soup.service.d/zz-test.conf'].decode()
        self.assertIn('SOUP_BROKER_TOKEN=<redacted>', unit)
        self.assertIn('"SOUP_TICKER=MNQZ2021" TVBRIDGE_PROFILE=/home/x/profile', unit)
        self.assertEqual(json.loads(files['admin/webhooks.json'])['webhooks'][-1]['stream_id'], '<redacted>')
        # manifest: sha256 per file as exported, release, versions, time
        man = json.loads(files['MANIFEST.json'])
        self.assertEqual(man, json.loads(json.dumps(manifest)))
        for f in man['files']:
            self.assertEqual(hashlib.sha256(files[f['path']]).hexdigest(), f['sha256'], f['path'])
        self.assertEqual(len(man['files']), len(files) - 1)
        for k in ('created_at_utc', 'versions', 'bridge_release', 'left_out', 'mode'):
            self.assertIn(k, man)
        # read only: the tree is unchanged; one audit entry
        for p, b in tree_before.items():
            self.assertEqual(open(p, 'rb').read(), b, p)
        entries = self.e.audit.read(10 ** 6)
        self.assertEqual(len(entries), audit_before + 1)
        self.assertEqual((entries[0]['type'], entries[0]['status']), ('export', 'done'))
        self.assertTrue(self.e.audit.verify_chain())

    def test_export_over_http(self):
        import http.client
        from http.server import ThreadingHTTPServer
        import server
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(self.e, 'tok', 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = server.make_handler(self.e, 'tok', port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for tok, want in ((None, 401), ('tok', 200)):
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                h = {'Host': f'127.0.0.1:{port}'}
                if tok:
                    h['X-Admin-Token'] = tok
                c.request('GET', '/api/export', headers=h)
                r = c.getresponse()
                body = r.read()
                self.assertEqual(r.status, want)
            self.assertEqual(r.getheader('Content-Type'), 'application/gzip')
            self.assertIn('attachment; filename="bridge-setup-', r.getheader('Content-Disposition'))
            self.assertIn('MANIFEST.json', self.unpack(body))
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestComingSoon(ExportBase):
    def test_overview_and_previews_flag_what_is_not_live(self):
        self.do('register_webhook', {'name': 'W2'})
        ov = self.e.overview()
        for k in ('rotate_stream_id', 'activate_webhook', 'add_webhook'):
            self.assertTrue(ov['coming_soon'][k]['coming_soon'])
            self.assertTrue(ov['coming_soon'][k]['text'].endswith('.'))
        self.assertNotIn('add_login', ov['coming_soon'])                 # login_setup is on here
        w2 = next(w for w in ov['webhooks'] if w['id'] == 'w2')
        main = next(w for w in ov['webhooks'] if w['id'] == 'main')
        self.assertEqual(sorted(w2['coming_soon']), ['activate_webhook', 'rotate_stream_id'])
        self.assertEqual(sorted(main['coming_soon']), ['rotate_stream_id'])
        self.assertEqual(ov['export'], {'available': True, 'url': '/api/export'})
        ch = self.e.preview('rotate_stream_id', {'webhook': 'main'})
        self.assertTrue(ch['plan']['coming_soon']['coming_soon'])
        json.dumps(ov)


class TestComingSoonWithoutLoginSetup(ExportBase):
    def profile_extra(self):
        extra = super().profile_extra()
        extra.pop('login_setup')
        return extra

    def test_add_login_coming_soon(self):
        ov = self.e.overview()
        self.assertTrue(ov['coming_soon']['add_login']['coming_soon'])
        data, _, man = self.e.export()           # no login_setup in a sandbox: the real unit folder is not read
        self.assertFalse([f for f in man['files'] if f['path'].startswith('units/')])


def tearDownModule():
    import test_addlogin
    test_addlogin.tearDownModule()


if __name__ == '__main__':
    unittest.main()
