"""Page controller lease loss: the daemon must exit non-zero, not stay blind.

In-memory doubles only: no browser, no network beyond a loopback test server.
"""
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tvbridge import daemon  # noqa: E402
from tvbridge.bridge import Bridge, LeaseLost  # noqa: E402
from tvbridge.executor import Executor  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.state import AccountState  # noqa: E402
from test_executor_reliability import PageDouble, TRADE  # noqa: E402

TOKEN = 'x' * 32


def wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class FakeSession:
    """CDP session double: `touch` is what the page's touch() returns, or an exception to raise."""

    def __init__(self, touch=True):
        self.touch = touch
        self.touches = 0
        self.closed = False

    def evaluate(self, expression, timeout=None):
        if '.touch()' in expression:
            self.touches += 1
            if isinstance(self.touch, BaseException):
                raise self.touch
            return self.touch
        if '.status()' in expression:
            return {'state': 'authorized', 'epoch': 'e1', 'synced': True, 'lastRxAgoMs': 0}
        return True

    def call(self, method, params=None, timeout=None):
        return {}

    def close(self):
        self.closed = True


def attached_bridge(touch, lease_ms=600):
    bridge = Bridge(AccountState(), devtools_port=0, page_url_contains='x', trade_url_pattern='x',
                    lifecycle={'leaseMs': lease_ms})
    bridge.session = FakeSession(touch)
    bridge._heartbeat_thread = threading.Thread(target=bridge._controller_heartbeat, daemon=True)
    bridge._heartbeat_thread.start()
    return bridge


class HeartbeatGiveUp(unittest.TestCase):
    def test_stalled_heartbeat_marks_the_lease_lost_and_refuses_requests_unsent(self):
        bridge = attached_bridge(TimeoutError('cdp stalled'))
        self.assertTrue(bridge.lease_lost.wait(3))
        self.assertEqual(bridge.lease_lost_reason, 'controller_heartbeat_stalled')
        bridge._heartbeat_thread.join(2)
        self.assertFalse(bridge._heartbeat_thread.is_alive())  # the thread gave up, as before
        self.assertEqual(bridge.status()['state'], 'controller_lease_lost')
        self.assertEqual(bridge.request('order/placeorder', {}),
                         {'ok': False, 'sent': False, 'code': 'controller_lease_lost'})
        with self.assertRaises(LeaseLost):
            bridge.wait_ready(timeout=1)

    def test_page_instance_that_stopped_itself_is_a_lost_lease(self):
        bridge = attached_bridge(False)
        self.assertTrue(bridge.lease_lost.wait(3))
        self.assertEqual(bridge.lease_lost_reason, 'page_controller_stopped')

    def test_script_missing_during_reload_is_not_a_loss(self):
        bridge = attached_bridge(None)
        self.assertTrue(wait_until(lambda: bridge.session.touches >= 3))
        self.assertFalse(bridge.lease_lost.is_set())
        bridge.detach()

    def test_healthy_heartbeat_keeps_the_lease(self):
        bridge = attached_bridge(True)
        self.assertTrue(wait_until(lambda: bridge.session.touches >= 3))
        self.assertFalse(bridge.lease_lost.is_set())
        self.assertTrue(bridge.detach())
        self.assertFalse(bridge.lease_lost.is_set())  # our own detach is not a loss

    def test_lease_expired_event_after_the_heartbeat_thread_is_gone(self):
        bridge = Bridge(AccountState(), devtools_port=0, page_url_contains='x', trade_url_pattern='x')
        bridge.session = FakeSession(True)  # no heartbeat thread at all
        bridge._on_event('Runtime.bindingCalled', {'name': bridge.binding, 'payload': json.dumps(
            {'type': 'controller_lease_expired', 'data': {}, 'epoch': None})})
        self.assertTrue(bridge.lease_lost.is_set())
        self.assertEqual(bridge.lease_lost_reason, 'controller_lease_expired')
        self.assertEqual(bridge.events[-1]['type'], 'controller_lease_expired')

    def test_close_code_is_logged_and_a_close_is_not_a_lost_lease(self):
        bridge = Bridge(AccountState(), devtools_port=0, page_url_contains='x', trade_url_pattern='x')
        bridge.session = FakeSession(True)
        with self.assertLogs('tvbridge', 'INFO') as logs:
            bridge._on_event('Runtime.bindingCalled', {'name': bridge.binding, 'payload': json.dumps(
                {'type': 'closed', 'data': {'reason': 1006}, 'epoch': None})})
        self.assertIn('page event closed (reason 1006)', logs.output[0])
        self.assertFalse(bridge.lease_lost.is_set())


class FakeServer:
    def __init__(self):
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


class ShutdownDrain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = AccountState()
        self.page = PageDouble(self.state)
        self.page.lease_lost = threading.Event()
        self.page.lease_lost_reason = None
        self.page.stop_reason = None
        self.journal = Journal(self.tmp.name)
        self.ex = Executor(self.page, self.state, self.journal, {
            'ticker': 'MNQZ2021', 'account_name': 'FAKE', 'account_id': 1,
            'soup_account_id': 'FAKE', 'max_contracts': 10, 'tick_size': .25, 'tag_wait_s': 0,
            'kill_switch_file': self.tmp.name + '/ENTRIES-DISABLED'})
        self.ex.resolve()

    def command(self, cid):
        return {'ticker': 'MNQZ2021', 'action': 'buy', 'orderType': 'market', 'quantity': 2,
                'extras': {'soup_command_id': cid, 'soup_trade_id': TRADE}}

    def start(self, drain_s=5.0):
        self.server = FakeServer()
        sd = daemon.Shutdown(self.page, [self.ex], self.server, drain_s=drain_s, poll_s=0.02)
        threading.Thread(target=sd.watch, daemon=True).start()
        return sd

    def test_lease_loss_exits_with_the_restart_status_and_refuses_new_orders(self):
        sd = self.start()
        self.page.lease_lost_reason = 'controller_heartbeat_stalled'
        self.page.lease_lost.set()
        self.assertTrue(sd.done.wait(3))
        self.assertTrue(self.server.stopped.is_set())
        self.assertEqual(sd.code, daemon.EXIT_LEASE_LOST)
        self.assertNotEqual(sd.code, 0)
        self.assertEqual(sd.unsettled, [])
        out, receipt = self.ex.send(self.command('after-stop'))
        self.assertEqual(out, 'rejected')
        self.assertTrue(receipt['reason'].startswith('daemon_stopping:controller_lease_lost'))
        self.assertIsNone(self.journal.get('after-stop'))  # refused before any intent record
        self.assertEqual(self.page.wire, [])

    def test_no_exit_while_an_order_send_is_in_flight(self):
        release, entered = threading.Event(), threading.Event()
        real_place = self.ex._place

        def slow_place(plan, tag, epoch):
            entered.set()
            release.wait(5)
            return real_place(plan, tag, epoch)

        self.ex._place = slow_place
        result = {}
        sender = threading.Thread(target=lambda: result.update(r=self.ex.send(self.command('in-flight'))))
        sender.start()
        self.assertTrue(entered.wait(3))
        sd = self.start()
        sd.request('controller_lease_lost:controller_heartbeat_stalled', daemon.EXIT_LEASE_LOST)
        time.sleep(0.4)
        self.assertFalse(self.server.stopped.is_set())  # still waiting for the send to settle
        self.assertFalse(sd.done.is_set())
        release.set()
        sender.join(3)
        self.assertTrue(sd.done.wait(3))
        self.assertTrue(self.server.stopped.is_set())
        self.assertEqual(result['r'][0], 'accepted')  # the in-flight order finished normally
        self.assertEqual(self.journal.get('in-flight')['outcome'], 'accepted')
        self.assertEqual(len(self.page.wire), 1)  # sent exactly once, never replayed
        self.assertEqual(sd.unsettled, [])

    def test_drain_is_bounded_and_leaves_the_intent_unresolved(self):
        release, entered = threading.Event(), threading.Event()

        def stuck_place(plan, tag, epoch):
            entered.set()
            release.wait(10)
            return 'unknown', {'bridge': 'test'}, None

        self.ex._place = stuck_place
        sender = threading.Thread(target=lambda: self.ex.send(self.command('stuck')), daemon=True)
        sender.start()
        self.assertTrue(entered.wait(3))
        sd = self.start(drain_s=0.3)
        sd.request('controller_lease_lost:x', daemon.EXIT_LEASE_LOST)
        self.assertTrue(sd.done.wait(3))
        self.assertEqual(sd.unsettled, ['main'])
        # The fsynced start record is on disk with no outcome: unresolved, never replayed.
        self.assertEqual([c['command_id'] for c in Journal(self.tmp.name).unresolved()], ['stuck'])
        release.set()
        sender.join(5)  # its late journal write must land before the temporary directory is removed

    def test_sigterm_is_a_clean_stop_with_status_zero(self):
        sd = self.start()
        sd.request('sigterm', 0)
        self.assertTrue(sd.done.wait(3))
        self.assertEqual(sd.code, 0)
        self.assertEqual(self.ex.send(self.command('late'))[0], 'rejected')

    def test_stop_that_also_lost_the_lease_still_asks_for_a_restart(self):
        self.page.lease_lost.set()
        sd = daemon.Shutdown(self.page, [self.ex], FakeServer(), drain_s=1, poll_s=5)
        sd.request('sigterm', 0)
        sd.watch()
        self.assertEqual(sd.code, daemon.EXIT_LEASE_LOST)

    def test_duplicate_command_still_reports_its_first_outcome_while_stopping(self):
        self.assertEqual(self.ex.send(self.command('first'))[0], 'accepted')
        self.page.stop_reason = 'sigterm'
        out, receipt = self.ex.send(self.command('first'))
        self.assertEqual(out, 'accepted')
        self.assertTrue(receipt['duplicate'])


class FollowerDrain(unittest.TestCase):
    def test_every_account_executor_is_drained_and_a_stuck_one_is_named(self):
        class Ex:
            def __init__(self, key=None):
                self.lock, self.key = threading.Lock(), key

        main, tb = Ex(), Ex('tb')
        tb.lock.acquire()  # a follower send in flight
        page = mock.Mock(lease_lost=threading.Event(), stop_reason=None)
        sd = daemon.Shutdown(page, [main, tb], FakeServer(), drain_s=0.2, poll_s=5)
        sd.request('sigterm', 0)
        sd.watch()
        self.assertEqual(sd.unsettled, ['tb'])
        self.assertEqual(page.stop_reason, 'sigterm')
        self.assertFalse(main.lock.locked())


class ClientHangsUp(unittest.TestCase):
    def test_broken_pipe_on_reply_is_quiet(self):
        class Pub:
            last = {}

        handler_cls = daemon.make_handler(mock.Mock(), Pub(), TOKEN)
        h = handler_cls.__new__(handler_cls)
        h.command, h.path, h.request_version = 'GET', '/health/' + TOKEN, 'HTTP/1.1'
        h.requestline = 'GET /health HTTP/1.1'
        h.close_connection = False

        class Gone:
            def write(self, data):
                raise BrokenPipeError(32, 'Broken pipe')

            def flush(self):
                raise BrokenPipeError(32, 'Broken pipe')

        h.wfile = Gone()
        for exc in (BrokenPipeError(), ConnectionResetError()):
            h.close_connection = False
            with mock.patch.object(Gone, 'write', side_effect=exc):
                h.respond(200, {'ok': True})  # must not raise
            self.assertTrue(h.close_connection)

    def test_real_server_survives_clients_that_leave_early(self):
        class Slow:
            bridge = mock.Mock(stop_reason=None)

            def health(self):
                time.sleep(0.3)
                return {'ok': True, 'problems': [], 'page': {}}

            def entries_disabled(self):
                return False

        class Pub:
            last = {}

        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(('127.0.0.1', 0), daemon.make_handler(Slow(), Pub(), TOKEN))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with mock.patch('sys.stderr') as err:
            for _ in range(3):
                s = socket.create_connection(('127.0.0.1', port))
                s.sendall(f'GET /health/{TOKEN} HTTP/1.1\r\nHost: x\r\n\r\n'.encode())
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00\x00\x00\x00\x00')
                s.close()  # RST: the reply write fails
            time.sleep(0.8)
            printed = ''.join(str(c) for c in err.write.call_args_list)
        self.assertNotIn('Traceback', printed)
        import urllib.request
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health/{TOKEN}', timeout=3) as r:
            self.assertEqual(r.status, 200)


class FakeBridge:
    instances = []

    def __init__(self, state, **kw):
        self.state = state
        self.lease_lost = threading.Event()
        self.lease_lost_reason = None
        self.stop_reason = None
        self.detached = False
        FakeBridge.instances.append(self)

    def attach(self, reload=True):
        pass

    def wait_ready(self, timeout=60):
        return {'state': 'authorized'}

    def status(self):
        return {'state': 'authorized', 'epoch': 'e'}

    def detach(self):
        self.detached = True
        return True


class FakeExecutor:
    def __init__(self, bridge, state, journal, cfg):
        self.bridge, self.state, self.journal, self.cfg = bridge, state, journal, cfg
        self.lock = threading.Lock()
        self.symbol, self.bracket_protect, self.watchdogs, self.account_id = 'MNQZ1', False, [], None

    def resolve(self):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class DaemonMainExit(unittest.TestCase):
    def run_main(self, trigger):
        FakeBridge.instances = []
        with tempfile.TemporaryDirectory() as d:
            cfg = {'account_name': 'FAKE', 'ticker': 'MNQZ2021', 'journal_dir': d + '/j', 'devtools_port': 1,
                   'max_contracts': 10,
                   'page_url_contains': 'x', 'trade_url_pattern': 'x', 'publish_mode': 'evidence',
                   'listen_port': free_port(), 'shutdown_drain_s': 2}
            p = Path(d) / 'config.json'
            p.write_text(json.dumps(cfg))
            before = signal.getsignal(signal.SIGTERM)
            self.addCleanup(signal.signal, signal.SIGTERM, before)
            threading.Thread(target=trigger, args=(before,), daemon=True).start()
            with mock.patch.object(daemon, 'Bridge', FakeBridge), mock.patch.object(daemon, 'Executor', FakeExecutor), \
                    mock.patch.object(sys, 'argv', ['daemon', '--config', str(p)]), \
                    mock.patch.dict(os.environ, {'TVBRIDGE_TOKEN': TOKEN}), mock.patch('logging.basicConfig'):
                return daemon.main()

    def test_heartbeat_give_up_makes_the_daemon_exit_non_zero(self):
        def lose(_):
            wait_until(lambda: FakeBridge.instances)
            time.sleep(0.3)
            FakeBridge.instances[0].lease_lost_reason = 'controller_heartbeat_stalled'
            FakeBridge.instances[0].lease_lost.set()

        with self.assertRaises(SystemExit) as ctx:
            self.run_main(lose)
        self.assertEqual(ctx.exception.code, daemon.EXIT_LEASE_LOST)
        self.assertTrue(FakeBridge.instances[0].detached)

    def test_sigterm_stops_cleanly_with_status_zero(self):
        def term(before):
            wait_until(lambda: signal.getsignal(signal.SIGTERM) is not before)
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGTERM)

        self.assertIsNone(self.run_main(term))
        self.assertTrue(FakeBridge.instances[0].detached)


if __name__ == '__main__':
    unittest.main()
