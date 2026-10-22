import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import guard

PORT = 9555
URL = 'ws://127.0.0.1:9555/devtools/page/ABC'
TARGETS = [{'type': 'page', 'id': 'ABC', 'url': 'https://trader.tradovate.com/welcome',
            'webSocketDebuggerUrl': URL}]
PT = {'x': 100, 'y': 200, 'hit': True}


def obs(**kw):
    base = {'host': 'trader.tradovate.com', 'path': '/welcome', 'password_inputs': 1,
            'user_visible': True, 'pass_visible': True, 'user_filled': True, 'pass_filled': True,
            'login_buttons': 1, 'login_point': PT, 'user_point': PT, 'second_factor': False,
            'captcha': False, 'alert_text': False, 'mode_buttons': 0, 'mode_point': None}
    base.update(kw)
    return base


APP = obs(path='/', password_inputs=0, user_visible=False, pass_visible=False, user_filled=False,
          pass_filled=False, login_buttons=0, login_point=None, user_point=None)


MODE = obs(path='/trading-mode', password_inputs=0, user_visible=False, pass_visible=False,
           user_filled=False, pass_filled=False, login_buttons=0, login_point=None, user_point=None,
           mode_buttons=1, mode_point=PT)


HEALTHY = {'entries_disabled': False, 'publisher': None,
           'health': {'ok': True, 'problems': [], 'page': {'state': 'authorized', 'epoch': 7}}}
UNAUTHORIZED = {'entries_disabled': False, 'publisher': None,  # the dead-lease shape
                'health': {'ok': False, 'page': {},
                           'problems': ['page_state:None', 'tradovate_silent', 'not_synced_for_current_connection']}}
UNSYNCED = {'entries_disabled': False, 'publisher': None,
            'health': {'ok': False, 'page': {'state': 'authorized', 'epoch': 8},
                       'problems': ['not_synced_for_current_connection']}}


class FakeWs:
    def __init__(self, pages):
        self.pages = list(pages)
        self.sent = []
        self.pending = None

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg['method'] == 'Runtime.evaluate':
            value = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
            result = {'result': {'value': value}}
        else:
            result = {}
        self.pending = json.dumps({'id': msg['id'], 'result': result})

    def recv(self):
        return self.pending

    def close(self):
        pass

    def methods(self):
        return [m['method'] for m in self.sent]


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.now = 1_610_697_600.0
        self.started = []

    def make(self, pages, unit=('failed', 'enabled'), targets=TARGETS, health=HEALTHY, **kw):
        self.ws = FakeWs(pages)
        self.restarted = getattr(self, 'restarted', [])
        return guard.Guard(state_dir=self.dir, port=PORT, targets=lambda p: targets,
                           connect=lambda u, p: self.ws, units=lambda u: unit,
                           starter=self.started.append, clock=lambda: self.now,
                           sleep=lambda s: None, health=lambda: health,
                           restarter=self.restarted.append, **kw)

    def test_logged_in_daemon_active_is_ok(self):
        st = self.make([APP], unit=('active', 'enabled')).run()
        self.assertEqual(st['state'], 'ok')
        self.assertEqual(self.started, [])

    def test_logged_in_failed_daemon_is_started_then_rate_limited(self):
        st = self.make([APP]).run()
        self.assertEqual(st['state'], 'daemon_started')
        self.assertEqual(self.started, ['tvbridge-daemon.service'])
        self.now += 60
        st = self.make([APP]).run()
        self.assertEqual(st['reason'], 'daemon_start_interval_not_elapsed')
        self.assertEqual(len(self.started), 1)

    def test_daemon_start_cap(self):
        for _ in range(3):
            self.make([APP]).run()
            self.now += 301
        st = self.make([APP]).run()
        self.assertEqual(st['state'], 'needs_human')
        self.assertEqual(len(self.started), 3)

    def test_autofilled_login_clicks_and_confirms(self):
        st = self.make([obs(), obs(), APP]).run()
        self.assertEqual(st['state'], 'logged_in_now')
        methods = self.ws.methods()
        self.assertEqual(methods.count('Input.dispatchMouseEvent'), 6)  # field + button
        self.assertNotIn('Input.dispatchKeyEvent', methods)

    def test_empty_fields_use_suggestion_keys_then_give_up(self):
        empty = obs(user_filled=False, pass_filled=False)
        st = self.make([empty]).run()
        self.assertEqual(st['state'], 'needs_human')
        self.assertEqual(st['reason'], 'saved_login_not_filled')
        keys = [m['params']['key'] for m in self.ws.sent if m['method'] == 'Input.dispatchKeyEvent']
        self.assertEqual(keys, ['ArrowDown', 'ArrowDown', 'Enter', 'Enter'])
        # Only one login-button click never happened: two mouse events sets = field only.
        self.assertEqual(self.ws.methods().count('Input.dispatchMouseEvent'), 3)

    def test_login_rate_limits(self):
        self.make([obs(), obs(), obs()]).run()
        self.now += 60
        st = self.make([obs()]).run()
        self.assertEqual(st['reason'], 'login_interval_not_elapsed')
        self.assertNotIn('Input.dispatchMouseEvent', self.ws.methods())
        for _ in range(2):
            self.now += 301
            self.make([obs(), obs(), obs()]).run()
        self.now += 301
        st = self.make([obs()]).run()
        self.assertEqual(st['state'], 'login_repeated')

    def test_second_factor_and_captcha_never_click(self):
        for page in (obs(second_factor=True), obs(captcha=True)):
            st = self.make([page]).run()
            self.assertEqual(st['state'], 'needs_human')
            self.assertNotIn('Input.dispatchMouseEvent', self.ws.methods())

    def test_covered_button_not_clicked(self):
        covered = obs(login_point={'x': 1, 'y': 2, 'hit': False})
        st = self.make([obs(), covered]).run()
        self.assertEqual(st['reason'], 'login_button_changed_or_covered')
        self.assertEqual(self.ws.methods().count('Input.dispatchMouseEvent'), 3)

    def test_ambiguous_or_missing_pages(self):
        st = self.make([APP], targets=TARGETS * 2).run()
        self.assertEqual(st['reason'], 'tradovate_page_ambiguous')
        st = self.make([APP], targets=[]).run()
        self.assertEqual(st['state'], 'page_missing')
        bad = [dict(TARGETS[0], webSocketDebuggerUrl='ws://10.0.0.1:9555/devtools/page/ABC')]
        st = self.make([APP], targets=bad).run()
        self.assertEqual(st['reason'], 'page_debugger_not_loopback')

    def test_reserved_ports_refused(self):
        for port in (9556, 9566, 9567):
            with self.assertRaises(guard.Hold):
                guard.Guard(state_dir=self.dir, port=port)

    def test_dry_run_never_acts(self):
        st = self.make([obs()], dry_run=True).run()
        self.assertEqual(st['reason'], 'dry_run_would_login')
        st = self.make([APP], dry_run=True).run()
        self.assertEqual(st['reason'], 'dry_run_would_start_daemon')
        self.assertEqual(self.started, [])
        self.assertNotIn('Input.dispatchMouseEvent', self.ws.methods())

    def test_long_logout_escalates(self):
        self.make([obs(login_buttons=2)]).run()
        self.now += 901
        st = self.make([obs(login_buttons=2)]).run()
        self.assertEqual(st['state'], 'needs_human')
        self.assertTrue(st['reason'].startswith('logged_out_over_15m_'))

    def test_login_then_simulation_chooser(self):
        st = self.make([obs(), obs(), MODE, MODE, APP]).run()
        self.assertEqual(st['state'], 'logged_in_now')
        self.assertEqual(st['reason'], 'saved_login_and_simulation_accepted')
        self.assertEqual(self.ws.methods().count('Input.dispatchMouseEvent'), 9)  # field, login, simulation

    def test_standalone_chooser_and_rate_limit(self):
        st = self.make([MODE, MODE, APP]).run()
        self.assertEqual(st['reason'], 'simulation_selected')
        self.now += 30
        st = self.make([MODE]).run()
        self.assertEqual(st['reason'], 'mode_interval_not_elapsed')
        self.assertNotIn('Input.dispatchMouseEvent', self.ws.methods())

    def test_chooser_without_unique_simulation_button_never_clicks(self):
        for page in (dict(MODE, mode_buttons=0, mode_point=None), dict(MODE, mode_buttons=2, mode_point=None),
                     dict(MODE, mode_point={'x': 1, 'y': 1, 'hit': False})):
            self.setUp()
            st = self.make([page]).run()
            self.assertEqual(st['state'], 'needs_human')
            self.assertNotIn('Input.dispatchMouseEvent', self.ws.methods())

    def test_forbidden_methods(self):
        s = guard.CdpSession(FakeWs([APP]))
        with self.assertRaises(guard.Hold):
            s.call('Page.navigate', {})
        with self.assertRaises(guard.Hold):
            s.key('a')


class UnhealthyDaemonTest(unittest.TestCase):
    """Logged-in page, active daemon, /health not authorized/synced: restart after > 60 s."""

    setUp, make = GuardTest.setUp, GuardTest.make

    def advance(self, seconds, **kw):
        """Unhealthy passes every 60 s (continuous observation) over `seconds`."""
        while seconds > 0:
            step = min(60, seconds)
            self.now += step
            seconds -= step
            self.pass_(**kw)

    def pass_(self, health=UNAUTHORIZED, page=APP, **kw):
        return self.make([page], unit=('active', 'enabled'), health=health, **kw).run()

    def events(self):
        return [json.loads(line) for line in (self.dir / 'events.jsonl').read_text().splitlines()]

    def memory(self):
        return json.loads((self.dir / 'guard-state.json').read_text())

    def test_restarts_after_unhealthy_over_60s(self):
        st = self.pass_()
        self.assertEqual((st['state'], st['reason']), ('ok', 'logged_in_daemon_page_not_authorized_0s'))
        self.assertEqual(st['daemon_health'], 'not_authorized')
        self.assertEqual(self.memory()['daemon_unhealthy_since'], self.now)
        self.now += 30
        self.pass_()
        self.now += 31
        st = self.pass_()
        self.assertEqual((st['state'], st['reason']), ('daemon_started', 'daemon_restarted_unhealthy'))
        self.assertEqual(self.restarted, ['tvbridge-daemon.service'])
        self.assertEqual(self.started, [])
        self.assertEqual(st['daemon_starts_last_hour'], 1)
        kinds = [(e['kind'], e['state'], e['reason']) for e in self.events()]
        self.assertIn(('daemon_restarted_unhealthy', 'daemon_started', 'page_not_authorized_for_61s'), kinds)
        self.assertEqual(kinds[-1], ('observation', 'daemon_started', 'daemon_restarted_unhealthy'))
        self.assertIsNone(self.memory()['daemon_unhealthy_since'])  # fresh window after a restart

    def test_restarts_for_unsynced_authorized_page(self):
        self.pass_(health=UNSYNCED)
        self.now += 61
        st = self.pass_(health=UNSYNCED)
        self.assertEqual(st['state'], 'daemon_started')
        self.assertEqual(len(self.restarted), 1)

    def test_no_restart_at_60s_or_less(self):
        self.pass_()
        self.now += 60
        st = self.pass_()
        self.assertEqual((st['state'], st['reason']), ('ok', 'logged_in_daemon_page_not_authorized_60s'))
        self.assertEqual(self.restarted, [])

    def test_never_restarts_when_logged_out(self):
        self.pass_()
        for _ in range(4):
            self.now += 30
            st = self.pass_(page=obs(login_buttons=2))  # login form the guard will not click
            self.assertEqual(st['state'], 'logged_out_waiting')
        self.assertEqual(self.restarted, [])
        self.assertIsNone(self.memory()['daemon_unhealthy_since'])
        self.now += 30  # logged in again: the 60 s window starts over
        st = self.pass_()
        self.assertEqual(st['reason'], 'logged_in_daemon_page_not_authorized_0s')

    def test_healthy_reading_resets_timer(self):
        self.pass_()
        self.now += 50
        st = self.pass_(health=HEALTHY)
        self.assertEqual((st['state'], st['reason'], st['daemon_health']), ('ok', 'logged_in_daemon_active', 'healthy'))
        self.assertIsNone(st['daemon_unhealthy_since'])
        self.now += 20
        st = self.pass_()
        self.now += 50
        st = self.pass_()
        self.assertEqual(st['reason'], 'logged_in_daemon_page_not_authorized_50s')
        self.assertEqual(self.restarted, [])

    def test_unreachable_or_no_token_resets_and_never_restarts(self):
        self.pass_()
        self.now += 50
        st = self.pass_(health=None)
        self.assertEqual((st['state'], st['daemon_health']), ('ok', 'unreachable'))
        self.assertIsNone(self.memory()['daemon_unhealthy_since'])
        self.now += 50
        self.pass_()
        self.now += 50
        g = guard.Guard(state_dir=self.dir, port=PORT, targets=lambda p: TARGETS,
                        connect=lambda u, p: FakeWs([APP]), units=lambda u: ('active', 'enabled'),
                        starter=self.started.append, restarter=self.restarted.append,
                        clock=lambda: self.now, sleep=lambda s: None)
        g.health = None  # what a missing TVBRIDGE_TOKEN gives
        st = g.run()
        self.assertEqual((st['state'], st['daemon_health']), ('ok', 'no_token'))
        self.assertIsNone(self.memory()['daemon_unhealthy_since'])
        self.assertEqual(self.restarted, [])

    def test_page_error_resets_timer(self):
        self.pass_()
        self.now += 40
        st = self.make([APP], unit=('active', 'enabled'), health=UNAUTHORIZED, targets=[]).run()
        self.assertEqual(st['state'], 'page_missing')
        self.now += 40
        st = self.pass_()
        self.assertEqual(st['reason'], 'logged_in_daemon_page_not_authorized_0s')
        self.assertEqual(self.restarted, [])

    def test_gap_between_passes_restarts_window(self):
        self.pass_()
        self.now += 600  # no pass in between (e.g. VM suspended): not "continuous"
        st = self.pass_()
        self.assertEqual(st['reason'], 'logged_in_daemon_page_not_authorized_0s')
        self.assertEqual(self.restarted, [])

    def test_min_interval_shared_with_daemon_starts(self):
        self.make([APP]).run()  # failed daemon started through the existing path
        self.assertEqual(self.started, ['tvbridge-daemon.service'])
        self.now += 10
        self.pass_()
        self.now += 61
        st = self.pass_()
        self.assertEqual((st['state'], st['reason']), ('ok', 'daemon_start_interval_not_elapsed'))
        self.assertEqual(self.restarted, [])
        skipped = [e for e in self.events() if e['kind'] == 'daemon_restart_skipped']
        self.assertEqual(skipped[-1]['reason'], 'page_not_authorized_61s_daemon_start_interval_not_elapsed')
        self.advance(300 - 71 + 1)  # 301 s after the start: allowed again, still unhealthy throughout
        self.assertEqual(self.restarted, ['tvbridge-daemon.service'])

    def test_restart_interval_after_own_restart(self):
        self.pass_()
        self.now += 61
        self.pass_()
        self.assertEqual(len(self.restarted), 1)
        self.pass_()
        self.now += 61
        st = self.pass_()
        self.assertEqual(st['reason'], 'daemon_start_interval_not_elapsed')
        self.assertEqual(len(self.restarted), 1)

    def test_hourly_cap(self):
        self.pass_()
        self.advance(61)
        self.advance(301)
        self.advance(301)
        self.assertEqual(len(self.restarted), 3)
        self.advance(301)
        st = self.pass_()
        self.assertEqual((st['state'], st['reason']), ('needs_human', 'daemon_start_cap_reached'))
        self.assertEqual(len(self.restarted), 3)
        self.assertEqual(self.events()[-2]['kind'], 'daemon_restart_skipped')

    def test_dry_run_and_no_daemon_never_restart(self):
        self.pass_(dry_run=True)
        self.now += 61
        st = self.pass_(dry_run=True)
        self.assertEqual(st['reason'], 'dry_run_would_restart_daemon')
        st = self.pass_(manage_daemon=False)
        self.assertEqual(st['reason'], 'logged_in')
        self.assertEqual(self.restarted, [])

    def test_v10_state_file_is_accepted(self):
        (self.dir).mkdir(exist_ok=True)
        guard.write_json(self.dir / 'guard-state.json',
                         {'logged_out_since': None, 'logins': [], 'modes': [],
                          'schema': 'decrypt.tvbridge-login-guard.v1', 'starts': [self.now - 100]})
        st = self.pass_()
        self.assertEqual(st['state'], 'ok')
        mem = self.memory()
        self.assertEqual(mem['daemon_unhealthy_since'], self.now)
        self.assertEqual(mem['starts'], [self.now - 100])

    def test_health_verdict(self):
        v = guard.health_verdict
        self.assertEqual(v(HEALTHY), 'healthy')
        self.assertEqual(v(UNAUTHORIZED), 'not_authorized')
        self.assertEqual(v(UNSYNCED), 'not_synced')
        for bad in (None, [], 'x', {}, {'health': {}}, {'health': {'page': 'authorized'}}):
            self.assertEqual(v(bad), 'unreachable')
        self.assertIsNone(guard.health_reader('short'))


def eastern(*args):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(*args, tzinfo=ZoneInfo('America/New_York')).timestamp()


class WeekendHoldTest(unittest.TestCase):
    FRI_1800 = eastern(2021, 10, 15, 18, 0)  # Friday
    setUp = GuardTest.setUp
    make = GuardTest.make

    def test_window_edges(self):
        hold = guard.WeekendHold()
        self.assertFalse(hold.active(eastern(2021, 10, 15, 17, 59)))
        self.assertTrue(hold.active(self.FRI_1800))
        self.assertTrue(hold.active(eastern(2021, 10, 16, 12, 0)))  # Saturday
        self.assertTrue(hold.active(eastern(2021, 10, 17, 17, 59)))
        self.assertFalse(hold.active(eastern(2021, 10, 17, 18, 0)))  # Sunday open
        self.assertFalse(hold.active(eastern(2021, 10, 18, 9, 0)))  # Monday
        # DST ends Sunday 2021-11-07: the window still follows Eastern wall time.
        self.assertTrue(hold.active(eastern(2021, 11, 7, 17, 59)))
        self.assertFalse(hold.active(eastern(2021, 11, 7, 18, 0)))
        self.assertEqual(hold.last_end(eastern(2021, 11, 8, 9, 0)), eastern(2021, 11, 7, 18, 0))
        self.assertEqual(hold.last_end(eastern(2021, 10, 17, 18, 0)), eastern(2021, 10, 17, 18, 0))
        self.assertEqual(hold.last_end(eastern(2021, 10, 17, 14, 0)), eastern(2021, 10, 10, 18, 0))

    def test_logged_out_in_hold_never_clicks_or_escalates(self):
        self.now = self.FRI_1800 + 60
        for page in (obs(), MODE):
            st = self.make([page]).run()
            self.assertEqual((st['state'], st['reason']), ('logged_out_waiting', 'weekend_maintenance_hold'))
            self.assertNotIn('Input.dispatchMouseEvent', self.ws.methods())
            self.assertNotIn('Input.dispatchKeyEvent', self.ws.methods())
        self.now += 36 * 3600  # Saturday night, logged out all along
        st = self.make([obs()]).run()
        self.assertEqual((st['state'], st['reason']), ('logged_out_waiting', 'weekend_maintenance_hold'))
        self.assertEqual(st['logins_last_hour'], 0)

    def test_before_hold_still_logs_in(self):
        self.now = self.FRI_1800 - 60
        st = self.make([obs(), obs(), APP]).run()
        self.assertEqual(st['state'], 'logged_in_now')

    def test_logged_in_in_hold_unchanged(self):
        self.now = self.FRI_1800 + 60
        st = self.make([APP], unit=('active', 'enabled')).run()
        self.assertEqual(st['state'], 'ok')
        st = self.make([APP]).run()
        self.assertEqual(st['state'], 'daemon_started')

    def test_sunday_open_logs_in_and_escalation_counts_from_open(self):
        self.now = self.FRI_1800 + 60
        self.make([obs(login_buttons=2)]).run()
        self.now = eastern(2021, 10, 17, 18, 0) + 30
        st = self.make([obs(login_buttons=2)]).run()
        self.assertEqual(st['state'], 'logged_out_waiting')  # not escalated by the Friday logout
        self.now += 901
        st = self.make([obs(login_buttons=2)]).run()
        self.assertEqual(st['state'], 'needs_human')
        self.assertTrue(st['reason'].startswith('logged_out_over_15m_'))
        self.now += 400
        st = self.make([obs(), obs(), APP]).run()
        self.assertEqual(st['state'], 'logged_in_now')


class WeekendHoldConfigTest(unittest.TestCase):
    def write(self, value):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__('shutil').rmtree, d, True)
        path = os.path.join(d, 'login-guard.json')
        with open(path, 'w') as f:
            f.write(value if isinstance(value, str) else json.dumps(value))
        return path

    def test_missing_file_is_the_default_hold(self):
        hold, err = guard.load_weekend_hold('/nonexistent/login-guard.json')
        self.assertIsNone(err)
        self.assertEqual(hold.text(), 'Fri 6:00 PM to Sun 6:00 PM (America/New_York)')
        self.assertTrue(hold.active(eastern(2021, 10, 16, 12, 0)))

    def test_custom_window_and_zone(self):
        hold, err = guard.load_weekend_hold(self.write({'weekend_hold': {
            'timezone': 'America/Chicago', 'start': 'friday 16:30', 'end': 'Sun 17:00'}}))
        self.assertIsNone(err)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ct = lambda *a: datetime(*a, tzinfo=ZoneInfo('America/Chicago')).timestamp()  # noqa: E731
        self.assertFalse(hold.active(ct(2021, 10, 15, 16, 29)))
        self.assertTrue(hold.active(ct(2021, 10, 15, 16, 30)))
        self.assertTrue(hold.active(ct(2021, 10, 17, 16, 59)))
        self.assertFalse(hold.active(ct(2021, 10, 17, 17, 0)))
        self.assertEqual(hold.last_end(ct(2021, 10, 18, 9, 0)), ct(2021, 10, 17, 17, 0))
        self.assertEqual(hold.text(), 'Fri 4:30 PM to Sun 5:00 PM (America/Chicago)')

    def test_window_may_wrap_past_sunday_midnight(self):
        hold, err = guard.load_weekend_hold(self.write({'weekend_hold': {'start': 'Sat 20:00', 'end': 'Mon 06:00'}}))
        self.assertIsNone(err)
        self.assertTrue(hold.active(eastern(2021, 10, 16, 21, 0)))   # Saturday night
        self.assertTrue(hold.active(eastern(2021, 10, 18, 5, 59)))   # Monday early
        self.assertFalse(hold.active(eastern(2021, 10, 18, 6, 0)))
        self.assertFalse(hold.active(eastern(2021, 10, 16, 19, 59)))
        self.assertEqual(hold.last_end(eastern(2021, 10, 18, 7, 0)), eastern(2021, 10, 18, 6, 0))

    def test_disabled_hold_never_holds(self):
        hold, err = guard.load_weekend_hold(self.write({'weekend_hold': {'enabled': False}}))
        self.assertIsNone(err)
        self.assertFalse(hold.active(eastern(2021, 10, 16, 12, 0)))
        self.assertEqual(hold.text(), 'off')
        self.assertEqual(hold.last_end(eastern(2021, 10, 16, 12, 0)), 0.0)

    def test_invalid_config_keeps_the_default_hold(self):
        for bad in ('{not json', {'weekend_hold': {'timezone': 'Mars/Base'}},
                    {'weekend_hold': {'start': 'Funday 18:00'}}, {'weekend_hold': {'start': 'Fri 25:00'}},
                    {'weekend_hold': {'start': 'Fri 18:00', 'end': 'Fri 18:00'}},
                    {'weekend_hold': {'enabled': 'no'}}, {'weekend_hold': {'extra': 1}}, []):
            with self.subTest(bad=bad):
                hold, err = guard.load_weekend_hold(self.write(bad))
                self.assertIn(err, ('guard_config_invalid', 'guard_config_unreadable'))
                self.assertTrue(hold.enabled)
                self.assertEqual(hold.text(), 'Fri 6:00 PM to Sun 6:00 PM (America/New_York)')

    def test_status_reports_the_hold(self):
        t = WeekendHoldTest('test_window_edges')
        t.setUp()
        t.now = WeekendHoldTest.FRI_1800 + 60
        st = t.make([obs()]).run()
        self.assertEqual(st['weekend_hold'], {'enabled': True, 'active': True, 'config_error': None,
                                              'text': 'Fri 6:00 PM to Sun 6:00 PM (America/New_York)'})


if __name__ == '__main__':
    unittest.main()


class SecureDirParentsCase(unittest.TestCase):
    def test_state_dir_parents_are_created_on_a_fresh_setup(self):
        import tempfile
        root = Path(tempfile.mkdtemp())
        target = root / 'tvbridge-state' / 'login-guard'
        self.assertEqual(guard.secure_dir(target), target)
        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        guard.secure_dir(target)      # idempotent
