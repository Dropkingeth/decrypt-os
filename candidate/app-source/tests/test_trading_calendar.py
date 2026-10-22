"""Entry-only retained calendar policy. No orders or provider calls."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app/backend'))
from trading_calendar import CalendarGate, SESSION_POLICY

NY=ZoneInfo('America/New_York')
def at(value): return int(datetime.fromisoformat(value).replace(tzinfo=NY).timestamp()*1000)
def binding(symbol='MNQZ2026'): return {'root':'MNQ','symbol':symbol}


class CalendarTests(unittest.TestCase):
    def setUp(self): self.gate=CalendarGate()
    def status(self,value,symbol='MNQZ2026'): return self.gate.status(binding(symbol),at(value))
    def assert_hold(self,value,reason,symbol='MNQZ2026'):
        result=self.status(value,symbol)
        self.assertFalse(result['entryAllowed']); self.assertTrue(result['managementAllowed'])
        self.assertEqual(result['reason'],reason)
        return result

    def test_regular_session_retains_strategy_authority(self):
        result=self.status('2026-09-24T09:30:00')
        self.assertTrue(result['entryAllowed'])
        self.assertTrue(result['strategySessionUnchanged'])
        self.assertFalse(result['automaticSwitching'])
        self.assertTrue(result['managementAllowed'])
        self.assertEqual(result['expectedSymbol'],'MNQZ2026')
        self.assertEqual(result['coverageEnds'],'2026-12-31')

    def test_daily_maintenance_and_exact_reopening(self):
        self.assertTrue(self.status('2026-09-24T16:59:59')['entryAllowed'])
        self.assert_hold('2026-09-24T17:00:00','daily_maintenance')
        self.assert_hold('2026-09-24T17:59:59','daily_maintenance')
        result=self.status('2026-09-24T18:00:00')
        self.assertTrue(result['entryAllowed']); self.assertEqual(result['tradeDate'],'2026-09-25')

    def test_weekend_and_sunday_reopening(self):
        self.assert_hold('2026-09-25T17:00:00','weekend')
        self.assert_hold('2026-09-26T18:00:00','weekend')
        self.assert_hold('2026-09-27T17:59:59','weekend')
        self.assertTrue(self.status('2026-09-27T18:00:00')['entryAllowed'])

    def test_daylight_saving_uses_reviewed_new_york_wall_time(self):
        for day in ('2026-10-25','2026-11-01','2026-11-08'):
            self.assert_hold(day+'T17:59:59','weekend')
            self.assertTrue(self.status(day+'T18:00:00')['entryAllowed'])

    def test_conservative_holiday_and_early_close_are_not_exchange_closure_claims(self):
        for instant in ('2026-11-25T18:00:00','2026-11-26T09:00:00','2026-11-26T18:00:00','2026-11-27T15:59:59'):
            result=self.assert_hold(instant,'conservative_holiday_policy_hold')
            self.assertNotEqual(result['state'],'closed')
            self.assertIn('not_exchange_closure',result['holidayBasis'])
        self.assertTrue(self.status('2026-11-29T18:00:00')['entryAllowed'])

    def test_roll_requires_explicit_dated_next_contract_no_position_switch(self):
        self.assertTrue(self.status('2026-12-11T16:59:59')['entryAllowed'])
        result=self.assert_hold('2026-12-13T18:00:00','contract_roll_required')
        self.assertEqual(result['expectedSymbol'],'MNQH2027'); self.assertTrue(result['rollWindow'])
        self.assertTrue(self.status('2026-12-14T09:30:00','MNQH2027')['entryAllowed'])
        self.assert_hold('2026-09-24T09:30:00','contract_roll_required','MNQU2026')
        self.assert_hold('2026-09-24T09:30:00','contract_not_current','MNQH2027')

    def test_contract_horizon_bounds_even_when_session_policy_extends_2027(self):
        self.assertTrue(self.status('2026-12-31T16:00:00','MNQH2027')['entryAllowed'])
        self.assert_hold('2026-12-31T18:00:00','calendar_outside_reviewed_horizon','MNQH2027')
        self.assert_hold('2027-01-04T09:30:00','calendar_outside_reviewed_horizon','MNQH2027')
        self.assert_hold('2026-09-13T16:00:00','calendar_outside_reviewed_horizon')

    def test_invalid_or_continuous_symbol_and_clock_never_open(self):
        for symbol in ('MNQ1!','MNQZ6','NQZ2026','MESZ2026','MNQX2026',None):
            result=self.gate.status(binding(symbol),at('2026-09-24T09:30:00'))
            self.assertFalse(result['entryAllowed']); self.assertTrue(result['managementAllowed'])
        for clock in (True,None,float('nan'),1.0,-1):
            self.assertFalse(self.gate.status(binding(),clock)['entryAllowed'])

    def test_missing_corrupt_symlink_and_duplicate_data_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'calendar.json'
            self.assertFalse(CalendarGate(p).status(binding(),at('2026-09-24T09:30:00'))['entryAllowed'])
            for raw in ('{}','{"schema":1,"schema":2}','not json'):
                p.write_text(raw)
                self.assertFalse(CalendarGate(p).status(binding(),at('2026-09-24T09:30:00'))['entryAllowed'])
            p.unlink(); p.symlink_to(ROOT/'app/contract-calendar.json')
            self.assertFalse(CalendarGate(p).status(binding(),at('2026-09-24T09:30:00'))['entryAllowed'])

    def test_horizon_only_extension_does_not_invent_later_roll(self):
        data=json.loads((ROOT/'app/contract-calendar.json').read_text())
        data['coverageEnds']='2027-12-31'
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'calendar.json';p.write_text(json.dumps(data))
            result=CalendarGate(p).status(binding('MNQH2027'),at('2027-03-01T09:30:00'))
            self.assertFalse(result['entryAllowed'])
            self.assertEqual(result['coverageEnds'],'2027-02-28')


if __name__=='__main__': unittest.main()
