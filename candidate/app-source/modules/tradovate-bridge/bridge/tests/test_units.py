import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge import protocol  # noqa: E402
from tvbridge.daemon import make_handler  # noqa: E402
from tvbridge.journal import Journal  # noqa: E402
from tvbridge.soup_transport import TeeTransport, TradovateBridgeTransport  # noqa: E402

TOKEN = 'x' * 32


class Protocol(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(protocol.tradovate_symbol('MNQZ2021'), 'MNQZ1')
        self.assertEqual(protocol.tradovate_symbol('ESH2022'), 'ESH2')
        with self.assertRaises(ValueError):
            protocol.tradovate_symbol('MNQ1!')

    def test_timestamps(self):
        base = 1622657700000
        self.assertEqual(protocol.to_ms('2021-06-02T18:15:00.123Z'), base + 123)
        self.assertEqual(protocol.to_ms('2021-06-02T18:15:00.1234567Z'), base + 123)
        self.assertEqual(protocol.to_ms('2021-06-02T18:15:00.12+00:00'), base + 120)
        self.assertEqual(protocol.to_ms('2021-06-02T18:15:00.1Z'), base + 100)
        self.assertEqual(protocol.to_ms('2021-06-02T18:15:00Z'), base)
        self.assertEqual(protocol.to_ms('2021-06-03T03:45:00.5+09:30'), base + 500)
        with self.assertRaises(ValueError):
            protocol.to_ms('yesterday')

    def test_statuses(self):
        self.assertEqual(protocol.soup_status('Working', 0), 'working')
        self.assertEqual(protocol.soup_status('Working', 1), 'partial')
        self.assertEqual(protocol.soup_status('Filled', 3), 'filled')
        self.assertEqual(protocol.soup_status('Expired', 0), 'cancelled')
        self.assertIsNone(protocol.soup_status('SomethingNew', 0))

    def test_ticks(self):
        self.assertTrue(protocol.on_tick(10007.75, 0.25))
        self.assertFalse(protocol.on_tick(10007.6, 0.25))
        for bad in (float('nan'), float('inf'), True, '10007', None):
            self.assertFalse(protocol.on_tick(bad, 0.25))


class Positions(unittest.TestCase):
    def test_latest_trade_date_wins(self):
        from tvbridge.state import AccountState
        st = AccountState()
        st.load_sync([{'id': 1, 'name': 'A'}], {'accounts': [{'id': 1, 'name': 'A'}],
            'orders': [], 'fills': [], 'orderVersions': [], 'positions': [
            {'id': 10, 'accountId': 1, 'contractId': 5, 'netPos': 2, 'tradeDate': {'year': 2021, 'month': 9, 'day': 23}},
            {'id': 11, 'accountId': 1, 'contractId': 5, 'netPos': 0, 'tradeDate': {'year': 2021, 'month': 9, 'day': 24}},
        ]}, epoch='e1')
        self.assertEqual(st.net_position(1, 5), 0)


class JournalRestart(unittest.TestCase):
    def test_start_without_result_is_unknown_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.start('a', trade_id='t:long', kind='entry', side='long', quantity=1, at=1)
            j.finish('a', 'accepted', {'broker_order_id': '9'}, '9')
            j.start('b', trade_id='t:long', kind='tp1_limit', side='long', quantity=1, at=2)
            self.assertEqual([j.next_sequence(), j.next_sequence()], [1, 2])
            j2 = Journal(d)
            self.assertEqual(j2.get('a')['outcome'], 'accepted')
            self.assertEqual(j2.get('b')['outcome'], 'unknown')
            self.assertEqual(j2.next_sequence(), 3)
            with self.assertRaises(ValueError):
                j2.start('a', trade_id='t:long', kind='entry', side='long', quantity=1, at=3)
            self.assertEqual([c['command_id'] for c in j2.unresolved()], ['b'])
            with self.assertRaises(ValueError):
                j2.reconcile('b', 'accepted')  # needs an order id
            j2.reconcile('b', 'accepted', broker_order_id='77', note='seen')
            j3 = Journal(d)
            self.assertEqual((j3.get('b')['outcome'], j3.get('b')['broker_order_id']), ('accepted', '77'))
            self.assertEqual(j3.unresolved(), [])

    def test_torn_final_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d)
            j.start('a', trade_id='t:long', kind='entry', side='long', quantity=1, at=1)
            with open(Path(d) / 'commands.jsonl', 'a') as f:
                f.write('{"type":"start","comm')
            j2 = Journal(d)
            self.assertTrue(j2.torn_tail)
            self.assertEqual(j2.get('a')['outcome'], 'unknown')


class StubExecutor:
    def __init__(self):
        self.seen = []

    def send(self, payload):
        self.seen.append(payload)
        return 'accepted', {'broker_order_id': '42'}

    def health(self):
        return {'ok': True}

    def entries_disabled(self):
        return False


class StubPublisher:
    last = {}


class DaemonRoundTrip(unittest.TestCase):
    def setUp(self):
        self.ex = StubExecutor()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.ex, StubPublisher(), TOKEN))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_transport_round_trip(self):
        t = TradovateBridgeTransport(self.base, TOKEN)
        out, rec = t.send({'ticker': 'MNQZ2021', 'action': 'buy'})
        self.assertEqual((out, rec), ('accepted', {'broker_order_id': '42'}))
        self.assertEqual(self.ex.seen, [{'ticker': 'MNQZ2021', 'action': 'buy'}])

    def test_wrong_token_is_rejected_without_executing(self):
        out, _ = TradovateBridgeTransport(self.base, 'y' * 32).send({'a': 1})
        self.assertEqual(out, 'rejected')
        self.assertEqual(self.ex.seen, [])

    def test_unreachable_bridge_is_a_definite_rejection(self):
        port = self.server.server_address[1]
        self.tearDown()
        out, rec = TradovateBridgeTransport(f'http://127.0.0.1:{port}', TOKEN).send({'a': 1})
        self.assertEqual((out, rec), ('rejected', {'bridge': 'unreachable'}))
        self.setUp()

    def test_non_loopback_refused(self):
        with self.assertRaises(ValueError):
            TradovateBridgeTransport('http://10.0.0.5:8777', TOKEN)

    def test_tee_keeps_primary_outcome_and_never_waits_for_shadow(self):
        release = threading.Event()

        class SlowBoom:
            def send(self, payload):
                release.wait(5)
                raise RuntimeError('shadow down')

        class Primary:
            def send(self, payload):
                return 'accepted', {'id': 'tp-1'}

        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'shadow.jsonl'
            tee = TeeTransport(Primary(), SlowBoom(), log)
            out, rec = tee.send({'n': 1})
            self.assertEqual((out, rec), ('accepted', {'id': 'tp-1'}))
            release.set()
            for _ in range(50):
                if log.exists() and log.read_text():
                    break
                threading.Event().wait(0.05)
            record = json.loads(log.read_text().splitlines()[0])
            self.assertEqual((record['primary'], record['shadow']), ('accepted', 'error'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
