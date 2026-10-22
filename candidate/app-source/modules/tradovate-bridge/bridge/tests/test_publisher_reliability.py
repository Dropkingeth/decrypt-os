"""Report-delivery failure must not look healthy merely because the loop runs."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tvbridge.daemon import Publisher, main


class PublisherReliability(unittest.TestCase):
    def make(self, mode='soup'):
        p = Publisher(Mock(), Mock(), Mock(), mode, 'http://127.0.0.1:1/unused', clock=lambda: 100)
        p.alive_at = 100
        return p

    def test_failed_delivery_stays_unhealthy_despite_active_loop_and_recovers_on_receipt(self):
        p = self.make()
        with patch('tvbridge.daemon.urllib.request.urlopen', side_effect=OSError('offline')):
            p.publish({'sequence': 1})
            p.alive_at = 100
            p.publish({'sequence': 2})
        self.assertIn('publish_not_acknowledged', p.watchdog())
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 202
        with patch('tvbridge.daemon.urllib.request.urlopen', return_value=response):
            p.publish({'sequence': 3})
        self.assertIsNone(p.watchdog())

    def test_non202_is_not_success(self):
        p = self.make()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        with patch('tvbridge.daemon.urllib.request.urlopen', return_value=response):
            p.publish({'sequence': 1})
        self.assertIn('failed', p.watchdog())

    def test_snapshot_exception_marks_failure_and_preserves_loop_liveness(self):
        p = self.make()
        p.journal.latest_trade_id.return_value = 't'
        p.executor.account_id = 1
        p.executor.trade_active.return_value = True
        p.executor.lock = threading.Lock()
        p.executor.snapshot.side_effect = ValueError('bad snapshot')
        def one_iteration(*args):
            p.stopping.set()
            return 1
        p.state.wait_for_change.side_effect = one_iteration
        with self.assertLogs('tvbridge', level='ERROR'):
            p.run()
        self.assertIn('publisher_exception', p.watchdog())

    def test_evidence_mode_success_clears_failure_without_network(self):
        p = self.make('evidence')
        p.failure = 'publisher_exception'
        with patch('tvbridge.daemon.urllib.request.urlopen') as network:
            p.publish({'sequence': 1})
        network.assert_not_called()
        self.assertIsNone(p.watchdog())

    def test_wall_clock_change_cannot_hide_stalled_publisher(self):
        p = self.make()
        p.clock = lambda: 200
        with patch('tvbridge.daemon.time.time', return_value=-100000):
            self.assertEqual(p.watchdog(), 'snapshot_publisher_stalled')

    def test_startup_failure_detaches_before_any_http_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'journal_dir': directory, 'devtools_port': 12345, 'max_contracts': 10,
                                         'page_url_contains': '/fake/', 'trade_url_pattern': '^fake$'}))
            with patch('sys.argv', ['daemon', '--config', str(config)]), \
                 patch.dict('os.environ', {'TVBRIDGE_TOKEN': 'x' * 32}), \
                 patch('tvbridge.daemon.Bridge') as bridge, \
                 patch('tvbridge.daemon.ThreadingHTTPServer') as server:
                bridge.return_value.wait_ready.side_effect = TimeoutError('fake timeout')
                with self.assertRaises(TimeoutError):
                    main()
                bridge.return_value.detach.assert_called_once_with()
                server.assert_not_called()


if __name__ == '__main__':
    unittest.main()
