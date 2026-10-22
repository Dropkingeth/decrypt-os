"""beta4: the contract calendar gate names the current dated MES contract from the reviewed MNQ rolls."""
from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/backend'))
from trading_calendar import CalendarGate  # noqa: E402

NY = ZoneInfo('America/New_York')


def at(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=NY).timestamp() * 1000)


class MesCalendar(unittest.TestCase):
    def setUp(self):
        self.gate = CalendarGate()

    def test_current_mes_contract_is_allowed_like_mnq(self):
        mes = self.gate.status({'root': 'MES', 'symbol': 'MESZ2026'}, at('2026-09-24T09:30:00'))
        mnq = self.gate.status({'root': 'MNQ', 'symbol': 'MNQZ2026'}, at('2026-09-24T09:30:00'))
        self.assertTrue(mes['entryAllowed'])
        self.assertEqual(mes['expectedSymbol'], 'MESZ2026')
        self.assertEqual({k: v for k, v in mes.items() if k != 'expectedSymbol'},
                         {k: v for k, v in mnq.items() if k != 'expectedSymbol'})

    def test_roll_and_wrong_contracts_hold(self):
        self.assertEqual(self.gate.status({'root': 'MES', 'symbol': 'MESU2026'}, at('2026-09-24T09:30:00'))['reason'],
                         'contract_roll_required')
        self.assertEqual(self.gate.status({'root': 'MES', 'symbol': 'MESH2027'}, at('2026-09-24T09:30:00'))['reason'],
                         'contract_not_current')
        for binding in ({'root': 'MES', 'symbol': 'MNQZ2026'}, {'root': 'MNQ', 'symbol': 'MESZ2026'},
                        {'root': 'ES', 'symbol': 'ESZ2026'}, {'root': 'MES', 'symbol': 'MESF2027'}, None):
            with self.subTest(binding=binding):
                result = self.gate.status(binding, at('2026-09-24T09:30:00'))
                self.assertEqual((result['entryAllowed'], result['reason']), (False, 'exact_dated_contract_required'))

    def test_session_holds_are_the_same_for_mes(self):
        for when, reason in (('2026-09-24T17:30:00', 'daily_maintenance'), ('2026-09-26T10:00:00', 'weekend')):
            with self.subTest(when=when):
                self.assertEqual(self.gate.status({'root': 'MES', 'symbol': 'MESZ2026'}, at(when))['reason'], reason)


if __name__ == '__main__':
    unittest.main()
