"""The packaged engine catalogue in /api/state, and the retired draft route."""
from copy import deepcopy
from http.client import HTTPConnection
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/backend'))
import engine_workspace
spec = importlib.util.spec_from_file_location('engine_workspace_server', ROOT / 'app/backend/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='decrypt-engine-catalog-')
        self.addCleanup(self.temp.cleanup)
        self.store = server.Store(self.temp.name)

    def test_state_carries_the_catalog_and_no_drafts(self):
        state = self.store.state()
        workspace = state['engineWorkspace']
        self.assertEqual(set(workspace), {'schema', 'setupOnly', 'catalog'})
        self.assertTrue(workspace['setupOnly'])
        self.assertFalse(workspace['catalog']['executionEnabled'])
        engines = {e['id']: e for e in workspace['catalog']['engines']}
        self.assertEqual(engines['soup']['customerAvailability'], 'available')
        self.assertEqual({engines[i]['customerAvailability'] for i in ('fvg-ifvg', 'ote-silver-bullet')}, {'coming_soon'})
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), ['onboarding.sqlite'])
        self.assertEqual(state, server.Store(self.temp.name).state())

    def test_no_draft_tables_are_created(self):
        with self.store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse({t for t in tables if t.startswith('engine_')}, tables)

    def test_every_packaged_engine_pins_the_included_code(self):
        catalog = engine_workspace.load_catalog()
        pairs = {p['id']: p for p in catalog['sourcePairs']}
        for engine in catalog['engines']:
            if engine['packageStatus'] == 'source_packaged':
                roles = {a['role'] for a in pairs[engine['sourcePairId']]['artifacts']}
                self.assertTrue({'engine', 'strategy'} <= roles, engine['id'])

    def test_catalog_rejects_unpinned_unknown_pair_and_path_refs(self):
        original = engine_workspace.load_catalog()
        for mutation in ('hash', 'path', 'unknown_pair', 'duplicate'):
            catalog = deepcopy(original)
            if mutation == 'hash': catalog['sourcePairs'][0]['artifacts'][0]['sha256'] = 'not-hash'
            if mutation == 'path': catalog['sourcePairs'][0]['artifacts'][0]['evidenceRef'] = '/private/runtime.py'
            if mutation == 'unknown_pair': catalog['engines'][0]['sourcePairId'] = 'unknown'
            if mutation == 'duplicate': catalog['engines'].append(catalog['engines'][0])
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                engine_workspace.validate_catalog(catalog)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='decrypt-engine-http-')
        self.store = server.Store(self.temp.name)
        self.http = server.LabServer(('127.0.0.1', 0), self.store)
        self.http.first_start.admitted = lambda: True  # first-start notice gate: tests/test_first_start.py
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)
        status, headers, body = self.request('GET', '/api/session', headers={'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'})
        self.assertEqual(status, 200)
        self.auth = {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': body['csrfToken']}

    def close(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def request(self, method, path, data=None, headers=None):
        conn = HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        conn.request(method, path, body=json.dumps(data) if data is not None else None,
                     headers={'Content-Type': 'application/json', **(headers or {})})
        response = conn.getresponse(); status = response.status; response_headers = dict(response.getheaders())
        payload = json.loads(response.read()); conn.close()
        return status, response_headers, payload

    def test_state_serves_the_catalog(self):
        status, _, state = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(state['engineWorkspace'], engine_workspace.engine_catalog())

    def test_draft_route_is_gone(self):
        status, _, body = self.request('POST', '/api/engine-workspace/actions',
                                       {'action': 'add_engine', 'engineId': 'soup', 'label': 'x'}, self.auth)
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))

    def test_bridge_setup_route_keeps_session_csrf_host_origin_guards(self):
        data = {'accountName': 'SIM-ACCOUNT-1', 'accountNumber': '12345', 'contract': 'MNQZ2026'}
        cases = [({}, 'session_required'), ({'Cookie': self.auth['Cookie']}, 'csrf_required'),
                 ({**self.auth, 'X-CSRF-Token': 'wrong'}, 'csrf_required'),
                 ({**self.auth, 'Origin': 'https://external.invalid'}, 'invalid_origin'),
                 ({**self.auth, 'Host': 'external.invalid'}, 'invalid_host'),
                 ({**self.auth, 'Sec-Fetch-Site': 'cross-site'}, 'invalid_origin')]
        for headers, code in cases:
            with self.subTest(code=code):
                status, _, body = self.request('POST', '/api/trading/bridge-setup', data, headers)
                self.assertEqual((status, body['error']['code']), (403, code))
        # With the guards satisfied, this fixture server has no trading service: refused, nothing run.
        status, _, body = self.request('POST', '/api/trading/bridge-setup', data, self.auth)
        self.assertEqual((status, body['error']['code']), (403, 'trading_disabled'))


if __name__ == '__main__':
    unittest.main()
