"""GET /api/session issues a CSRF token only to the app's own same-origin fetch."""
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
spec = importlib.util.spec_from_file_location('session_origin_server', ROOT / 'app/backend/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

APP_FETCH = {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}


class SessionOrigin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='decrypt-session-origin-')
        self.http = server.LabServer(('127.0.0.1', 0), server.Store(self.temp.name))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.host = '127.0.0.1:' + str(self.http.server_port)

    def close(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def get(self, headers):
        conn = HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        conn.request('GET', '/api/session', headers=headers)
        response = conn.getresponse()
        status, cookie, body = response.status, response.getheader('Set-Cookie'), json.loads(response.read())
        conn.close()
        return status, cookie, body

    def test_app_fetch_gets_a_session(self):
        for extra in ({}, {'Origin': 'http://' + self.host}):
            with self.subTest(extra=extra):
                status, cookie, body = self.get({**APP_FETCH, **extra})
                self.assertEqual(status, 200)
                self.assertIn('csrfToken', body)
                self.assertIn('HttpOnly', cookie)

    def test_plain_local_client_gets_no_token(self):
        status, cookie, body = self.get({})
        self.assertEqual(status, 403)
        self.assertIsNone(cookie)
        self.assertEqual(body['error']['code'], 'session_origin')
        self.assertNotIn('csrfToken', body)

    def test_refused_variants(self):
        variants = {
            'same-site page': {**APP_FETCH, 'Sec-Fetch-Site': 'same-site'},
            'typed navigation': {**APP_FETCH, 'Sec-Fetch-Site': 'none', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'},
            'cross-site': {**APP_FETCH, 'Sec-Fetch-Site': 'cross-site'},
            'no-cors embed': {**APP_FETCH, 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'script'},
            'iframe': {**APP_FETCH, 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'iframe'},
            'missing dest': {'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors'},
            'other local port origin': {**APP_FETCH, 'Origin': 'http://127.0.0.1:1'},
            'foreign origin': {**APP_FETCH, 'Origin': 'https://attacker.invalid'},
        }
        for name, headers in variants.items():
            with self.subTest(name):
                status, cookie, body = self.get(headers)
                self.assertEqual(status, 403)
                self.assertIsNone(cookie)
                self.assertNotIn('csrfToken', body)

    def test_ui_proxy_host_is_accepted(self):
        # installer/frontend.py forwards the page's Host (the UI port) unchanged.
        conn = HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        conn.putrequest('GET', '/api/session', skip_host=True)
        conn.putheader('Host', '127.0.0.1:8811')
        conn.putheader('Origin', 'http://127.0.0.1:8811')
        for key, value in APP_FETCH.items():
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        response.read(); conn.close()


if __name__ == '__main__':
    unittest.main()
