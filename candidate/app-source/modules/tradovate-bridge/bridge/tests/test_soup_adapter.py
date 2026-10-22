"""Soup v3 adapter-database delivery: schema, sequence floor, pairing, single writer."""
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tvbridge.soup_adapter import AdapterDbSink, Skip


def snap(**over):
    s = {'event_id': 'tvbridge-1', 'sequence': 1, 'observed_at': 1_632_147_200_000, 'account_id': 'Account C',
         'symbol': 'MNQZ2021', 'trade_id': 'T1-long', 'side': 'long', 'position_qty': 3,
         'orders': [{'command_id': 'c1', 'broker_order_id': '9', 'status': 'filled', 'filled_qty': 3,
                     'fill_time': 1_632_147_199_000}], 'open_orders': []}
    s.update(over)
    return s


class AdapterDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.context = root / 'broker-context.json'
        self.write_context(broker_seq=7000)
        self.db_path = root / 'state' / 'events.sqlite'
        self.sink = AdapterDbSink(self.db_path, account_id='Account C', ticker='MNQZ2021',
                                  context_path=self.context)

    def tearDown(self):
        self.sink.close()
        self.tmp.cleanup()

    def write_context(self, broker_seq=0, trade_id='T1-long', phase='entry', account='Account C', ticker='MNQZ2021'):
        trade = {'id': trade_id, 'phase': phase} if trade_id else None
        self.context.write_text(json.dumps({'schema': 'x', 'pairing': {'account_id': account, 'ticker': ticker},
                                            'engine': {'broker_seq': broker_seq, 'trade': trade}}))

    def rows(self):
        with sqlite3.connect(self.db_path) as db:
            return db.execute('SELECT seq,channel,event_id,payload,source,observed_at FROM events ORDER BY seq').fetchall()

    def test_sequence_starts_above_engine_broker_seq(self):
        # The engine is already at broker sequence 7000; a bridge counting from 1 would be rejected.
        p = self.sink.publish(snap())
        self.assertEqual(p['sequence'], 7001)
        self.assertEqual(p['event_id'], 'tvbridge-7001')
        (seq, channel, event_id, payload, source, observed), = self.rows()
        self.assertEqual((channel, event_id, source, observed), ('broker', 'tvbridge-7001', 'tvbridge', 1_632_147_200_000))
        self.assertEqual(json.loads(payload)['sequence'], 7001)

    def test_sequence_keeps_rising_when_engine_lags(self):
        self.assertEqual(self.sink.publish(snap())['sequence'], 7001)
        self.assertEqual(self.sink.publish(snap())['sequence'], 7002)
        self.write_context(broker_seq=8000)
        self.assertEqual(self.sink.publish(snap())['sequence'], 8001)

    def test_no_active_or_different_trade_writes_nothing(self):
        for kwargs in ({'trade_id': None}, {'phase': 'closed'}, {'trade_id': 'T2-short'}):
            self.write_context(broker_seq=5, **kwargs)
            with self.assertRaises(Skip):
                self.sink.publish(snap())
        self.assertEqual(self.rows(), [])

    def test_pairing_mismatch_refused(self):
        self.write_context(broker_seq=5, account='Other')
        with self.assertRaises(ValueError):
            self.sink.publish(snap())
        self.write_context(broker_seq=5)
        with self.assertRaises(ValueError):
            self.sink.publish(snap(symbol='MNQH2022'))
        self.assertEqual(self.rows(), [])

    def test_missing_or_symlinked_context_refused(self):
        self.context.unlink()
        with self.assertRaises(OSError):
            self.sink.publish(snap())
        target = Path(self.tmp.name) / 'elsewhere.json'
        target.write_text('{}')
        self.context.symlink_to(target)
        with self.assertRaises(ValueError):
            self.sink.publish(snap())

    def test_private_permissions_and_single_writer(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path.parent).st_mode), 0o700)
        with self.assertRaises(RuntimeError):
            AdapterDbSink(self.db_path, account_id='Account C', ticker='MNQZ2021', context_path=self.context)

    def test_schema_readable_by_soup_adapter_reader_query(self):
        self.sink.publish(snap())
        uri = self.db_path.as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True) as db:
            low, high = db.execute('SELECT min(seq),max(seq) FROM events').fetchone()
            rows = db.execute('SELECT seq,channel,payload,observed_at FROM events WHERE seq>? ORDER BY seq LIMIT 128', (0,)).fetchall()
        self.assertEqual((low, high), (1, 1))
        self.assertEqual(rows[0][1], 'broker')


if __name__ == '__main__':
    unittest.main()
