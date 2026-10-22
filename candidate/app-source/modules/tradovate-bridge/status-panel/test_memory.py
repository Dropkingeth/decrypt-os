#!/usr/bin/env python3
"""Snapshot sharing tests for the bridge status panel (2.2 memory). Offline, no network.
Run: python3 -m unittest test_memory -v"""
import threading
import time
import unittest
from unittest import mock

import server


class StatusBody(unittest.TestCase):
    def setUp(self):
        server.SNAPSHOT_CACHE.update(at=None, body=None)
        self.calls = 0

    def slow_snapshot(self):
        self.calls += 1
        time.sleep(0.2)
        return {'n': self.calls}

    def test_overlapping_requests_build_one_snapshot(self):
        out = []
        with mock.patch.object(server, 'snapshot', self.slow_snapshot):
            threads = [threading.Thread(target=lambda: out.append(server.status_body())) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(self.calls, 1)
        self.assertEqual(set(out), {'{"n": 1}'})

    def test_a_fresh_snapshot_after_the_reuse_window(self):
        with mock.patch.object(server, 'snapshot', self.slow_snapshot), mock.patch.object(server, 'SNAPSHOT_REUSE_S', 0.05):
            self.assertEqual(server.status_body(), '{"n": 1}')
            time.sleep(0.1)
            self.assertEqual(server.status_body(), '{"n": 2}')

    def test_a_failed_snapshot_is_not_reused(self):
        def boom():
            raise RuntimeError('bridge down')
        with mock.patch.object(server, 'snapshot', boom):
            with self.assertRaises(RuntimeError):
                server.status_body()
        self.assertIsNone(server.SNAPSHOT_CACHE['body'])
        with mock.patch.object(server, 'snapshot', self.slow_snapshot):
            self.assertEqual(server.status_body(), '{"n": 1}')


if __name__ == '__main__':
    unittest.main()
