#!/usr/bin/env python3
"""Verdict tests for the read-only bridge status panel. Offline: no network,
no systemctl, no journal. Run: python3 -m unittest test_server.py -v"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import server

TOKEN = 'x' * 32
DEMO = 'wss://demo.tradovateapi.com/v1/websocket'

# A firing.json shaped like the real one: one leader ('main'), three followers, in order.
FIRING_FULL = {
    'firing_order': ['account-a', 'account-b', 'account-e', 'account-f'],
    'accounts': {
        'account-a': {'role': 'main', 'label': 'Account A'},
        'account-b': {'role': 'follower', 'label': 'Account B'},
        'account-e': {'role': 'follower', 'label': 'Account E'},
        'account-f': {'role': 'follower', 'label': 'Account F'},
    },
}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def unit(active='active', sub='running', result='success'):
    return {'active': active, 'sub': sub, 'result': result, 'since': ''}


def all_units(daemon=None):
    return {'tvbridge-browser.service': unit(), 'tvbridge-daemon.service': daemon or unit(),
            'tvbridge-login-guard.timer': unit(sub='waiting')}


def health(state='authorized', synced=True, problems=()):
    page = None if state is None else {'state': state, 'synced': synced, 'epoch': 7 if state else None,
                                       'url': DEMO, 'lastRxAgoMs': 400}
    return {'health': {'ok': not problems, 'problems': list(problems), 'page': page}}


def guard(state='ok', reason='logged_in_daemon_active', age=10, **extra):
    now = time.time()
    return dict({'schema': 1, 'state': state, 'reason': reason, 'checked_at': iso(now - age),
                 'logged_out_since': None, 'logins_last_hour': 0, 'last_login_attempt_at': None}, **extra)


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP'))
        root = Path(self.tmp.name)
        (root / 'guard').mkdir()
        patches = {'ROOT': root, 'CONFIG': root / 'config.json', 'MIRROR': root / 'mirror.json',
                   'SOUP': root / 'soup.json', 'GUARD_DIR': root / 'guard',
                   'WEBHOOK_REGISTRY': root / 'webhooks.json'}
        for name, value in patches.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, fake in (('daemon_log', lambda: ([], 0)), ('bridge', self.fake_bridge),
                           ('units', lambda: self.units)):
            p = mock.patch.object(server, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.units = all_units()
        self.health = health()
        self.state = {'net_position': 0, 'unresolved_commands': 0}

    def fake_bridge(self, endpoint, port):
        value = self.health if endpoint == 'health' else self.state
        return (value, None) if value is not None else (None, 'URLError')

    def write_guard(self, status, events=()):
        d = server.GUARD_DIR
        (d / 'status.json').write_text(json.dumps(status))
        (d / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))

    def verdict(self):
        s = server.snapshot()
        json.dumps(s, default=str)
        return s['verdict']

    def test_fresh_install_reads_not_set_up(self):
        # No config.json and the browser unit off: the bridge ships off, which is not an alarm.
        self.units = dict(all_units(), **{'tvbridge-browser.service': unit('inactive', 'dead', 'success')})
        self.health = self.state = None
        v = self.verdict()
        self.assertEqual(v['title'], 'Bridge not set up yet')
        self.assertEqual(v['tone'], 'off')

    def test_turned_off_reads_off_not_an_error(self):
        # Set up, then turned off (setup.py disable): units stopped and disabled, a stale guard file left behind.
        server.CONFIG.write_text(json.dumps({'account_name': 'DEMO-1'}))
        now = time.time()
        self.write_guard(guard('logged_out_waiting', 'login_interval_not_elapsed',
                               logged_out_since=iso(now - 120), last_login_attempt_at=iso(now - 60)))
        off = dict(unit('inactive', 'dead', 'success'), file='disabled')
        self.units = dict(all_units(), **{'tvbridge-browser.service': off, 'tvbridge-daemon.service': off})
        self.health = self.state = None
        v = self.verdict()
        self.assertEqual((v['tone'], v['title']), ('off', 'Bridge is off'))
        self.assertNotIn('in progress', v['detail'])
        # A browser that stopped on its own stays enabled: still the red fault.
        self.units['tvbridge-browser.service'] = dict(unit('inactive', 'dead', 'success'), file='enabled')
        self.assertEqual(self.verdict()['title'], 'Bridge browser is not running')

    def test_login_row_reads_off_when_its_daemon_is_turned_off(self):
        now = time.time()
        g = guard('logged_out_waiting', 'login_interval_not_elapsed', logged_out_since=iso(now - 120))
        entry = {'label': 'Login A', 'primary': True, 'token_env': 'TVBRIDGE_TOKEN'}
        off = dict(unit('inactive', 'dead', 'success'), file='disabled')
        self.assertEqual(server.login_connection(entry, g, None, 'URLError', None, off, now),
                         ('off', 'The bridge is turned off.'))
        self.assertEqual(server.login_connection(entry, g, None, 'URLError', None, unit('inactive', 'dead'), now)[0],
                         'logged_out')

    # --- the five required cases ---------------------------------------------

    def test_logged_out(self):
        now = time.time()
        self.write_guard(guard('logged_out_waiting', 'login_interval_not_elapsed',
                               logged_out_since=iso(now - 120), last_login_attempt_at=iso(now - 60)))
        self.health = health(None, problems=['page_state:None'])
        v = self.verdict()
        self.assertEqual(v['title'], 'Tradovate logged out')
        self.assertEqual(v['detail'], 'Automatic login is in progress (checked about every 30 seconds). Last try 60 s ago.')

    def test_logged_out_weekend_hold(self):
        now = time.time()
        self.write_guard(guard('logged_out_waiting', 'weekend_maintenance_hold',
                               logged_out_since=iso(now - 120), last_login_attempt_at=iso(now - 60)))
        self.health = health(None, problems=['page_state:None'])
        v = self.verdict()
        self.assertEqual(v['title'], 'Tradovate logged out')
        self.assertTrue(v['detail'].startswith('Weekend pause: automatic login is off until the Sunday'))

    def test_logged_in_page_unauthorized(self):
        # Dead page lease: guard ok, daemon active, page session gone.
        self.write_guard(guard())
        self.health = health(None, synced=None, problems=['page_state:None', 'tradovate_silent'])
        v = self.verdict()
        self.assertEqual(v['title'], 'Bridge disconnected from page')
        self.assertNotIn('logged out', v['title'].lower())
        self.assertTrue(v['detail'].startswith('Tradovate is logged in, but the bridge lost its session'))

    def test_logged_in_health_unreachable_daemon_auto_restarting(self):
        self.write_guard(guard())
        self.health = self.state = None
        self.units = all_units(daemon=unit('activating', 'auto-restart', 'exit-code'))
        v = self.verdict()
        self.assertEqual(v['title'], 'Bridge disconnected from page')
        self.assertEqual(v['detail'], 'Tradovate is logged in. Recovery in progress: the bridge daemon is restarting automatically.')

    def test_recovery_after_guard_restart_state(self):
        self.write_guard(guard('daemon_started', 'daemon_restarted_unhealthy', age=40))
        self.health = health('connecting', synced=False, problems=['page_state:connecting'])
        v = self.verdict()
        self.assertEqual(v['title'], 'Bridge disconnected from page')
        self.assertEqual(v['detail'], 'Tradovate is logged in. Recovery in progress: the login guard restarted the bridge daemon 40 s ago.')

    def test_recovery_after_guard_restart_event_spelling(self):
        now = time.time()
        self.write_guard(guard(age=5), events=[
            {'at': iso(now - 50), 'kind': 'daemon_restarted_unhealthy', 'state': 'ok', 'reason': 'health_unauthorized_60s'},
            {'at': iso(now - 5), 'kind': 'observation', 'state': 'ok', 'reason': 'logged_in_daemon_active'}])
        self.health = health('authorized', synced=False, problems=['not_synced_for_current_connection'])
        v = self.verdict()
        self.assertEqual(v['title'], 'Bridge disconnected from page')
        self.assertIn('restarted the bridge daemon 50 s ago', v['detail'])

    def test_healthy_after_guard_restart(self):
        self.write_guard(guard('daemon_started', 'daemon_restarted_unhealthy', age=20))
        v = self.verdict()
        self.assertEqual((v['tone'], v['title']), ('mint', 'Healthy · armed'))  # 1.3 copy

    def test_healthy(self):
        self.write_guard(guard())
        v = self.verdict()
        self.assertEqual((v['tone'], v['title'], v['detail']),
                         ('mint', 'Healthy · armed',  # 1.3 copy: Soup sends to A directly
                          "The main account receives the strategy's orders directly, with the exact payload: "
                          'quantity, TP1, stop, break-even, runners and exits.'))

    # --- edges ---------------------------------------------------------------

    def test_stale_guard_never_says_logged_out(self):
        self.write_guard(guard('logged_out_waiting', 'login_interval_not_elapsed', age=900,
                               logged_out_since=iso(time.time() - 1000)))
        self.health = health(None)
        v = self.verdict()
        self.assertEqual(v['title'], 'Not connected to Tradovate')

    def test_logged_out_daemon_offline_says_logged_out(self):
        self.write_guard(guard('logged_out_waiting', 'login_form_not_recognised', logged_out_since=iso(time.time() - 30)))
        self.health = self.state = None
        self.units = all_units(daemon=unit('inactive', 'dead'))
        self.assertEqual(self.verdict()['title'], 'Tradovate logged out')

    def test_daemon_start_cap_is_not_a_login_problem(self):
        self.write_guard(guard('needs_human', 'daemon_start_cap_reached'))
        self.health = health(None)
        self.assertEqual(self.verdict()['title'], 'Needs you: bridge keeps disconnecting')

    def test_captcha_still_asks_for_login(self):
        self.write_guard(guard('needs_human', 'captcha', logged_out_since=iso(time.time() - 60)))
        self.assertEqual(self.verdict()['title'], 'Needs you: log in to Tradovate')

    def test_daemon_offline_unchanged_when_guard_unknown(self):
        self.health = self.state = None
        self.units = all_units(daemon=unit('failed', 'failed', 'exit-code'))
        self.assertEqual(self.verdict()['title'], 'Bridge daemon offline')

    def test_only_recent_guard_restart_counts(self):
        self.write_guard(guard('daemon_started', 'daemon_restarted_unhealthy', age=80),
                         events=[{'at': iso(time.time() - 900), 'kind': 'daemon_restarted_unhealthy'}])
        self.health = health(None)
        with mock.patch.object(server, 'GUARD_STALE_SECONDS', 10_000):
            detail = self.verdict()['detail']
        self.assertIn('restarted the bridge daemon 80 s ago', detail)

    def test_panel_has_no_controls(self):
        src = Path(server.__file__).read_text()
        for word in ('restart', 'start', 'stop', 'kill'):
            self.assertNotIn("'systemctl', '--user', '%s'" % word, src)
        self.assertNotIn('do_POST', src)

    # --- webhook line (2.2, read-only display), end to end through snapshot() ------------

    def test_snapshot_webhook_line_end_to_end_without_registry(self):
        (server.ROOT / 'firing.json').write_text(json.dumps(FIRING_FULL))
        self.write_guard(guard())
        s = server.snapshot()
        json.dumps(s, default=str)  # the whole snapshot must still be JSON-safe
        self.assertEqual(s['webhook_line'], {
            'name': 'Webhook Main', 'strategy': None, 'leader': 'Account A',
            'followers': ['Account B', 'Account E', 'Account F']})

    def test_snapshot_webhook_line_end_to_end_with_registry(self):
        (server.ROOT / 'firing.json').write_text(json.dumps(FIRING_FULL))
        server.WEBHOOK_REGISTRY.write_text(json.dumps(
            {'webhooks': [{'id': 'main', 'name': 'Main', 'strategy': 'soup-pine-r4', 'state': 'live'}]}))
        self.write_guard(guard())
        s = server.snapshot()
        self.assertEqual(s['webhook_line']['strategy'], 'Soup Pine r4')
        self.assertEqual(s['webhook_line']['leader'], 'Account A')

    def test_snapshot_webhook_line_none_without_firing_json(self):
        self.write_guard(guard())
        s = server.snapshot()
        self.assertIsNone(s['webhook_line'])


class WebhookLineCase(unittest.TestCase):
    """webhook_line() in isolation: the registry file only ever adds a strategy name,
    and reading it never breaks the line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP'))
        self.registry = Path(self.tmp.name) / 'webhooks.json'
        p = mock.patch.object(server, 'WEBHOOK_REGISTRY', self.registry)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_registry_present_known_strategy_id(self):
        self.registry.write_text(json.dumps(
            {'webhooks': [{'id': 'main', 'name': 'Main', 'strategy': 'soup-pine-r4', 'state': 'live'}]}))
        line = server.webhook_line(FIRING_FULL)
        self.assertEqual(line, {'name': 'Webhook Main', 'strategy': 'Soup Pine r4',
                                 'leader': 'Account A',
                                 'followers': ['Account B', 'Account E', 'Account F']})

    def test_registry_missing_drops_strategy_only(self):
        # self.registry is never created: the file-not-found case.
        line = server.webhook_line(FIRING_FULL)
        self.assertIsNotNone(line)
        self.assertIsNone(line['strategy'])
        self.assertEqual(line['leader'], 'Account A')
        self.assertEqual(line['followers'], ['Account B', 'Account E', 'Account F'])

    def test_registry_malformed_json_never_breaks_the_line(self):
        self.registry.write_text('{not valid json')
        line = server.webhook_line(FIRING_FULL)
        self.assertIsNotNone(line)
        self.assertIsNone(line['strategy'])
        self.assertEqual(line['leader'], 'Account A')

    def test_registry_wrong_shape_never_breaks_the_line(self):
        self.registry.write_text(json.dumps({'webhooks': 'not-a-list'}))
        line = server.webhook_line(FIRING_FULL)
        self.assertIsNotNone(line)
        self.assertIsNone(line['strategy'])

    def test_registry_without_main_id_drops_strategy(self):
        self.registry.write_text(json.dumps({'webhooks': [{'id': 'other', 'strategy': 'soup-pine-r4'}]}))
        line = server.webhook_line(FIRING_FULL)
        self.assertIsNone(line['strategy'])

    def test_unknown_strategy_id_is_shown_as_is(self):
        self.registry.write_text(json.dumps({'webhooks': [{'id': 'main', 'strategy': 'mystery-v2'}]}))
        line = server.webhook_line(FIRING_FULL)
        self.assertEqual(line['strategy'], 'mystery-v2')

    def test_missing_firing_config_gives_no_line(self):
        self.assertIsNone(server.webhook_line(None))

    def test_firing_config_missing_accounts_gives_no_line(self):
        self.assertIsNone(server.webhook_line({'firing_order': ['a']}))

    def test_firing_config_without_a_leader_gives_no_line(self):
        cfg = {'firing_order': ['a', 'b'],
                'accounts': {'a': {'role': 'follower', 'label': 'A'}, 'b': {'role': 'follower', 'label': 'B'}}}
        self.assertIsNone(server.webhook_line(cfg))

    def test_firing_config_without_followers_gives_no_line(self):
        cfg = {'firing_order': ['a'], 'accounts': {'a': {'role': 'main', 'label': 'A'}}}
        self.assertIsNone(server.webhook_line(cfg))


class MarkupCase(unittest.TestCase):
    """The gear link and the webhook-line placeholder: present, read-only, styled with the
    panel's own tokens."""

    def test_gear_link_in_header_after_the_clock(self):
        page = server.PAGE
        self.assertIn('<a class="gear" href="decrypt-advanced://open"', page)
        self.assertIn('aria-label="Open Bridge Advanced"', page)
        self.assertIn('stroke="currentColor"', page)
        header = page[page.index('<header class="top">'):page.index('</header>')]
        self.assertIn('class="gear"', header)
        self.assertGreater(header.index('class="gear"'), header.index('class="clock"'))

    def test_stop_time_strips_any_zone_abbreviation(self):
        """The stop line shortens 'confirmed by Tradovate <time> <zone>' to 'confirmed <time>' for any
        zone the panel host reports (clock_text uses the host's own %Z), not just one region's."""
        import re
        page = server.PAGE
        self.assertIn(".replace('confirmed by Tradovate ','confirmed ').replace(/ [A-Z]{2,5}$/,'')", page)
        self.assertNotIn('P[DS]T', page)
        strip = re.compile(r' [A-Z]{2,5}$')
        for when in ('confirmed by Tradovate 18:41:16.000 UTC', 'confirmed by Tradovate 18:41:16.000 CET',
                     'confirmed by Tradovate 18:41:16.000 AEST', 'confirmed by Tradovate (time unknown)'):
            text = strip.sub('', when.replace('confirmed by Tradovate ', 'confirmed '))
            self.assertNotRegex(text, r' [A-Z]{2,5}$', when)
            self.assertTrue(text.startswith('confirmed '), text)

    def test_gear_focus_ring_matches_panel_amber_token(self):
        self.assertIn('.gear:focus-visible{outline:2px solid var(--amber)', server.PAGE)
        self.assertIn('width:28px;height:28px', server.PAGE.split('.gear{')[1].split('}')[0])

    def test_gear_is_a_plain_link_not_a_control(self):
        page = server.PAGE
        gear_html = page[page.index('class="gear"'):page.index('</a>', page.index('class="gear"'))]
        self.assertNotIn('onclick', gear_html)
        self.assertNotIn('fetch(', gear_html)

    def test_webhook_line_placeholder_inside_trade_section_under_the_header(self):
        page = server.PAGE
        self.assertIn('<p class="webhook" id="tr-webhook" hidden></p>', page)
        trade_start = page.index('<section class="trade"')
        sh_end = page.index('</div>', trade_start)  # end of the .sh header row
        tr_head = page.index('<div class="tr-head">', trade_start)
        webhook_pos = page.index('id="tr-webhook"', trade_start)
        self.assertGreater(webhook_pos, sh_end)
        self.assertLess(webhook_pos, tr_head)

    def test_webhook_line_rendered_with_textcontent_only(self):
        self.assertIn("$('tr-webhook').textContent=", server.PAGE)


if __name__ == '__main__':
    unittest.main()
