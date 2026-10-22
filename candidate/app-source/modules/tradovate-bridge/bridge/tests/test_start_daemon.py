"""scripts/start_daemon.py: wait for the browser's DevTools port, then exec the daemon. No browser."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import start_daemon  # noqa: E402


class Version(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"Browser": "stub"}'
        self.send_response(200 if self.path == '/json/version' else 404)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StartDaemon(unittest.TestCase):
    def config(self, **values):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d, True)
        path = os.path.join(d, 'config.json')
        with open(path, 'w') as f:
            json.dump(values, f)
        return path

    def test_wait_stops_at_the_first_answer_or_the_deadline(self):
        answers = iter([False, False, True])
        t = [0.0]
        ok = start_daemon.wait_for(1, 90, probe=lambda port: next(answers), clock=lambda: t[0],
                                   sleep=lambda s: t.__setitem__(0, t[0] + s))
        self.assertTrue(ok)
        self.assertEqual(t[0], 2.0)
        t[0] = 0.0
        self.assertFalse(start_daemon.wait_for(1, 5, probe=lambda port: False, clock=lambda: t[0],
                                               sleep=lambda s: t.__setitem__(0, t[0] + s)))

    def test_execs_the_daemon_once_devtools_answers(self):
        srv = ThreadingHTTPServer(('127.0.0.1', 0), Version)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        cfg = self.config(devtools_port=srv.server_address[1])
        with mock.patch.object(start_daemon.os, 'execv') as execv, mock.patch.object(start_daemon.os, 'chdir'):
            start_daemon.main(['--config', cfg, '--wait', '3'])
        execv.assert_called_once_with(sys.executable, [sys.executable, '-m', 'tvbridge.daemon', '--config', cfg])

    def test_refuses_without_a_port_or_an_answer(self):
        with mock.patch.object(start_daemon.os, 'execv') as execv:
            self.assertEqual(start_daemon.main(['--config', self.config(devtools_port='9555')]), 1)
            with mock.patch.object(start_daemon, 'devtools_answers', return_value=False):
                self.assertEqual(start_daemon.main(['--config', self.config(devtools_port=9), '--wait', '0']), 1)
        execv.assert_not_called()


if __name__ == '__main__':
    unittest.main()
