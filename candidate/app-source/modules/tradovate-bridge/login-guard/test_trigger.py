import unittest

import trigger


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class LoggedInTest(unittest.TestCase):
    def test_authorized_daemon_response(self):
        h = {'entries_disabled': False, 'health': {'ok': True, 'page': {'state': 'authorized'}}}
        self.assertTrue(trigger.logged_in(h))

    def test_not_authorized_states(self):
        for state in ('waiting_for_app_login', 'connecting', None):
            h = {'health': {'ok': False, 'page': {'state': state}}}
            self.assertFalse(trigger.logged_in(h))

    def test_no_answer_or_garbage(self):
        for h in (None, [], 'x', {}, {'health': {}}, {'health': {'page': 'authorized'}}):
            self.assertFalse(trigger.logged_in(h))


class ReadyTest(unittest.TestCase):
    def test_authorized_and_synced_is_ready(self):
        h = {'health': {'ok': True, 'problems': [], 'page': {'state': 'authorized'}}}
        self.assertTrue(trigger.ready(h))

    def test_authorized_but_unsynced_is_a_miss(self):
        h = {'health': {'ok': False, 'problems': ['not_synced_for_current_connection'],
                        'page': {'state': 'authorized'}}}
        self.assertTrue(trigger.logged_in(h))
        self.assertFalse(trigger.ready(h))

    def test_other_problems_are_not_a_miss(self):
        h = {'health': {'ok': False, 'problems': ['tradovate_silent'], 'page': {'state': 'authorized'}}}
        self.assertTrue(trigger.ready(h))

    def test_not_authorized_or_no_answer_is_a_miss(self):
        for h in (None, {}, {'health': {'page': {'state': 'waiting_for_app_login'}, 'problems': []}},
                  {'health': {'page': {}, 'problems': ['page_state:None']}}):
            self.assertFalse(trigger.ready(h))


class TriggerTest(unittest.TestCase):
    def setUp(self):
        self.starts = []
        self.clock = Clock()
        self.trig = trigger.Trigger(lambda: self.starts.append(self.clock.t), now=self.clock)

    def test_one_miss_does_not_start(self):
        self.assertFalse(self.trig.observe(False))
        self.assertFalse(self.trig.observe(True))
        self.assertFalse(self.trig.observe(False))
        self.assertEqual(self.starts, [])

    def test_two_misses_start_once_then_cooldown(self):
        self.trig.observe(False)
        self.assertTrue(self.trig.observe(False))
        self.clock.t = 10
        self.assertFalse(self.trig.observe(False))
        self.clock.t = 21
        self.assertTrue(self.trig.observe(False))
        self.assertEqual(self.starts, [0.0, 21])

    def test_unsynced_live_daemon_starts_guard_with_throttle(self):
        unsynced = {'health': {'ok': False, 'problems': ['not_synced_for_current_connection'],
                               'page': {'state': 'authorized'}}}
        self.assertFalse(self.trig.observe(trigger.ready(unsynced)))
        self.assertTrue(self.trig.observe(trigger.ready(unsynced)))
        self.clock.t = 19
        self.assertFalse(self.trig.observe(trigger.ready(unsynced)))
        self.assertEqual(self.starts, [0.0])

    def test_start_guard_only_starts_the_guard_unit(self):
        calls = []
        real = trigger.subprocess.run
        trigger.subprocess.run = lambda argv, **kw: calls.append(argv)
        try:
            trigger.start_guard()
        finally:
            trigger.subprocess.run = real
        self.assertEqual(calls, [['systemctl', '--user', 'start', '--no-block', 'tvbridge-login-guard.service']])

    def test_logged_in_never_starts(self):
        for _ in range(50):
            self.assertFalse(self.trig.observe(True))
        self.assertEqual(self.starts, [])


class ReadHealthTest(unittest.TestCase):
    """read_health against a real loopback HTTP server."""

    def serve(self, status, body):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.paths.append(self.path)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        server.paths = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_returns_parsed_json_and_uses_token_path(self):
        server = self.serve(200, b'{"health": {"ok": true}}')
        self.assertEqual(trigger.read_health('tok123', port=server.server_port), {'health': {'ok': True}})
        self.assertEqual(server.paths, ['/health/tok123'])

    def test_non_200_is_none(self):
        server = self.serve(404, b'{"error": "not found"}')
        self.assertIsNone(trigger.read_health('tok', port=server.server_port))

    def test_bad_json_is_none(self):
        server = self.serve(200, b'not json')
        self.assertIsNone(trigger.read_health('tok', port=server.server_port))

    def test_oversize_body_is_none(self):
        server = self.serve(200, b'"' + b'x' * trigger.MAX_HEALTH_BYTES + b'"')
        self.assertIsNone(trigger.read_health('tok', port=server.server_port))

    def test_nothing_listening_is_none(self):
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        self.assertIsNone(trigger.read_health('tok', port=port))


if __name__ == '__main__':
    unittest.main()
