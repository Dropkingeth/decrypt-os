"""Uncertain local DevTools writes must never become definite broker refusals.

All transports here are in-memory doubles. No browser or broker is contacted.
"""
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge import cdp, ws
from tvbridge.bridge import Bridge


class FailingWrite:
    def __init__(self, transmitted, error):
        self.transmitted = transmitted
        self.error = error
        self.calls = []
        self.received = ''

    def send_text(self, text):
        self.calls.append(text)
        self.received = text if self.transmitted == 'all' else text[:8]
        raise self.error


def session_with(connection, closed=False):
    session = cdp.Session.__new__(cdp.Session)
    session.conn = connection
    session.lock = threading.Lock()
    session.next_id = 0
    session.waiting = {}
    session.closed = threading.Event()
    if closed:
        session.closed.set()
    return session


def bridge_with(session):
    bridge = Bridge.__new__(Bridge)
    bridge.session = session
    bridge.api = 'offline_test_api'
    bridge.read_only = False
    bridge._detached = None
    bridge.lease_lost = threading.Event()  # 1.0.6.1: set only after a lost page lease
    return bridge


class DevToolsWriteUncertainty(unittest.TestCase):
    def test_closed_before_dispatch_is_definitely_not_sent(self):
        conn = FailingWrite('all', OSError('unreachable'))
        session = session_with(conn, closed=True)
        with self.assertRaises(cdp.NotSent):
            session.call('Runtime.evaluate', {'expression': 'test()'})
        self.assertEqual(conn.calls, [])
        result = bridge_with(session).request('order/placeorder', {'orderQty': 1})
        self.assertFalse(result['sent'])
        self.assertFalse(result['ok'])
        self.assertEqual(conn.calls, [])

    def test_partial_or_complete_write_error_is_uncertain_and_never_retried(self):
        for transmitted in ('partial', 'all'):
            for error in (OSError('socket failed'), ws.Closed('connection closed')):
                with self.subTest(transmitted=transmitted, error=type(error).__name__):
                    conn = FailingWrite(transmitted, error)
                    session = session_with(conn)
                    with self.assertRaises(cdp.CDPError) as caught:
                        session.call('Runtime.evaluate', {'expression': 'test()'})
                    self.assertNotIsInstance(caught.exception, cdp.NotSent)
                    self.assertEqual(len(conn.calls), 1)
                    self.assertTrue(conn.received)
                    self.assertEqual(session.waiting, {})

    def test_broker_request_preserves_uncertainty_after_fully_transmitted_write(self):
        conn = FailingWrite('all', OSError('socket failed after transmission'))
        session = session_with(conn)
        result = bridge_with(session).request('order/placeorder', {'orderQty': 1})
        self.assertFalse(result['ok'])
        self.assertTrue(result['sent'])
        self.assertEqual(len(conn.calls), 1)
        sent = json.loads(conn.received)
        self.assertEqual(sent['method'], 'Runtime.evaluate')
        self.assertIn('order/placeorder', sent['params']['expression'])
        self.assertEqual(session.waiting, {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
