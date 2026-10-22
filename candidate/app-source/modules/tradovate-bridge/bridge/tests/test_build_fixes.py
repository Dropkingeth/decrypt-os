"""Session-scoped unseen orders, settling an unknown cancel, brief snapshot holds,
adapter-db snapshot delivery and the limit deviation guard."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tvbridge import protocol, reconcile
from tvbridge.daemon import Publisher
from tvbridge.executor import Executor
from tvbridge.journal import Journal
from tvbridge.soup_adapter import Skip

NY = ZoneInfo('America/New_York')


def ms(text):
    return int(datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=NY).timestamp() * 1000)


class SessionScopedUnseenOrders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(self.tmp.name)
        self.ex = Executor.__new__(Executor)
        self.ex.journal = self.journal
        self.ex.state = Mock()
        self.ex.state.order.return_value = None  # Tradovate's sync shows none of them.
        self.ex.clock_ms = lambda: ms('2021-04-14 10:00')

    def tearDown(self):
        self.tmp.cleanup()

    def accepted(self, cid, oid, at):
        self.journal.start(cid, trade_id='t', kind='entry', side='long', quantity=1, tag='x', at=at)
        self.journal.finish(cid, 'accepted', {}, broker_order_id=oid)

    def test_session_boundaries_follow_new_york_time(self):
        self.assertEqual(protocol.session_start_ms(ms('2021-04-14 10:00')), ms('2021-04-13 18:00'))
        self.assertEqual(protocol.session_start_ms(ms('2021-04-14 18:00')), ms('2021-04-14 18:00'))
        self.assertEqual(protocol.session_start_ms(ms('2021-11-07 20:00')), ms('2021-11-07 18:00'))

    def test_earlier_session_orders_missing_from_sync_do_not_block(self):
        self.accepted('old', '11', ms('2021-04-12 09:00'))
        self.assertEqual(self.ex._unseen_accepted_orders(), [])

    def test_current_session_or_undated_orders_still_block(self):
        self.accepted('now', '12', ms('2021-04-13 19:00'))
        self.journal.start('undated', trade_id='t', kind='entry', side='long', quantity=1, tag='y')
        self.journal.finish('undated', 'accepted', {}, broker_order_id='13')
        self.assertEqual(sorted(self.ex._unseen_accepted_orders()), ['12', '13'])


class CancelReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(self.tmp.name)
        for cid, kind in (('c', 'cancel'), ('e', 'entry')):
            self.journal.start(cid, trade_id='t', kind=kind, side='long', quantity=1, tag=cid)
            self.journal.finish(cid, 'unknown', {})

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        with patch.object(sys, 'argv', ['reconcile', '--journal-dir', self.tmp.name, *args]), \
                redirect_stdout(io.StringIO()):
            reconcile.main()

    def test_cancel_settled_without_order_id(self):
        self.cli('--command', 'c', '--cancel-settled', '--note', 'nothing working')
        self.assertEqual(Journal(self.tmp.name).get('c')['outcome'], 'accepted')

    def test_cancel_settled_refused_for_orders_and_order_id_refused_for_cancels(self):
        with self.assertRaises(SystemExit):
            self.cli('--command', 'e', '--cancel-settled')
        with self.assertRaises(SystemExit):
            self.cli('--command', 'c', '--placed', '99')
        with self.assertRaises(ValueError):
            self.journal.reconcile('e', 'accepted')
        self.assertEqual(len(Journal(self.tmp.name).unresolved()), 2)

    def test_cancel_not_placed_still_allowed(self):
        self.cli('--command', 'c', '--not-placed')
        self.assertEqual(Journal(self.tmp.name).get('c')['outcome'], 'rejected')


class PublisherHoldsAndAdapterDb(unittest.TestCase):
    def make(self, mode='soup', sink=None):
        self.now = 100.0
        p = Publisher(Mock(), Mock(), Mock(), mode, 'http://127.0.0.1:1/unused', clock=lambda: self.now, sink=sink)
        p.alive_at = 100.0
        return p

    def test_brief_hold_does_not_block_dispatch_but_persistent_one_does(self):
        p = self.make()
        p.failure = 'snapshot_unavailable'
        self.now = 109.0
        p.alive_at = self.now
        self.assertIsNone(p.watchdog())
        self.now = 111.0
        p.alive_at = self.now
        self.assertEqual(p.watchdog(), 'snapshot_publisher_failed:snapshot_unavailable')
        p.failure = None
        self.assertIsNone(p.watchdog())

    def test_delivery_failure_blocks_immediately(self):
        p = self.make()
        p.failure = 'publish_not_acknowledged'
        self.assertEqual(p.watchdog(), 'snapshot_publisher_failed:publish_not_acknowledged')

    def test_adapter_db_publish_skip_and_failure(self):
        sink = Mock()
        sink.publish.return_value = {'sequence': 41}
        p = self.make('adapter-db', sink)
        with patch('tvbridge.daemon.urllib.request.urlopen') as network:
            p.publish({'sequence': 1})
        network.assert_not_called()
        self.assertEqual(p.last['published'], 'adapter_db')
        self.assertIsNone(p.watchdog())
        sink.publish.side_effect = Skip('no_matching_active_trade')
        p.publish({'sequence': 2})
        self.assertEqual(p.last['published'], 'skipped')
        self.assertIsNone(p.watchdog())
        sink.publish.side_effect = ValueError('engine pairing differs')
        with self.assertLogs('tvbridge', level='ERROR'):
            p.publish({'sequence': 3})
        self.assertEqual(p.watchdog(), 'snapshot_publisher_failed:adapter_write_failed')


if __name__ == '__main__':
    unittest.main()


class LimitDeviationGuard(unittest.TestCase):
    def guard(self, price, fill=12000.0, band=None):
        ex = Executor.__new__(Executor)
        ex.cfg = {'max_contracts': 5} if band is None else {'max_contracts': 5, 'max_limit_deviation_pct': band}
        ex.account_id, ex.contract_id = 1, 2
        ex.journal = Mock()
        ex._entry_for = lambda trade_id: {'outcome': 'accepted', 'broker_order_id': '9', 'quantity': 1}
        ex._wait_entry_terminal = lambda oid: ('Filled', 1)
        ex.state = Mock()
        ex.state.lock = __import__('threading').RLock()
        ex.state.net_position.return_value = 1
        ex.state.open_orders.return_value = []
        ex.state.avg_fill_price.return_value = fill
        return ex._guard({'kind': 'tp1_limit', 'side': 'long', 'quantity': 1, 'price': price, 'trade_id': 't'})

    def test_price_of_40_on_a_12000_contract_is_refused(self):
        from tvbridge.executor import Refused
        with self.assertRaises(Refused):
            self.guard(40)

    def test_reasonable_limit_passes_and_band_is_configurable(self):
        from tvbridge.executor import Refused
        self.assertEqual(self.guard(12030.5)['price'], 12030.5)
        with self.assertRaises(Refused):
            self.guard(12030.5, band=0.1)

    def test_unknown_fill_price_refuses(self):
        from tvbridge.executor import Refused
        with self.assertRaises(Refused):
            self.guard(12030.5, fill=None)
