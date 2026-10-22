#!/usr/bin/env python3
"""Follower and copy tests for the bridge status panel (1.3). Offline: no network,
no systemctl, no journalctl. The scenarios use the synthetic Account A sample trade
(fixtures/, the same lines as test_stop) for the leader and the tvbridge 1.0.9 file
formats for the followers: config.json `followers`, kill-switch files, firing.json,
firing/status.json, firing/alerts.jsonl and the lane journals' `native` records.
Run: python3 -m unittest test_followers -v"""
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest import mock

import server
import test_stop
from test_stop import TRADE, STOP_ID, DEMO, lines, fixture_snapshots, fixture_context

EB, EC = 'f:account-b:', 'f:account-c:'
PLAN = {'side': 'short', 'initial': 12020.0, 'break_even': 11995.0, 'tp1_limit': 11960.0, 'tp1_price': 11960.0,
        'alloc': {'tp1': 2, 'parent': 1, 'eod': 1}, 'chart_ms': 2000}
T0 = 1615401600000          # the entry decision of the fixture trade (2021-03-10 18:40:00 UTC)
SCENARIOS = ('linked_not_trading', 'stage_b', 'all_in_trade_be', 'skipped_c', 'follower_alert',
             'leader_kill_switch', 'leader_unhealthy', 'no_followers_configured', 'daemon_without_followers_route')


def setUpModule():
    test_stop.setUpModule()


def tearDownModule():
    test_stop.tearDownModule()


def unit(active='active', sub='running'):
    return {'active': active, 'sub': sub, 'result': 'success', 'since': ''}


def order(oid, typ, qty, tp1=False, action='Buy'):
    return {'id': str(oid), 'type': typ, 'qty': qty, 'tp1': tp1, 'action': action}


def row(key, account_id, net=0, orders=(), available=True, synced=True, unresolved=0, killed=False):
    if not available:
        return {'key': key, 'available': False, 'reason': 'account_not_in_login'}
    return {'key': key, 'account_id': account_id, 'available': True, 'synced': synced, 'healthy': synced,
            'logged_in': synced, 'problems': [] if synced else ['not_synced_for_current_connection'],
            'net_position': net, 'orders': list(orders), 'unresolved_commands': unresolved,
            'entries_disabled': killed, 'native_exits': True, 'account_name_sha256': 'ab' * 32}


def native(event, at, **detail):
    return dict(detail, type='native', trade_id=TRADE, event=event, at=at)


def lane_be(prefix):
    """A follower lane that entered short 4, proved its initial stop, placed its own TP1 for 2 and moved to break-even."""
    return [native('start', T0 + 3000, plan=PLAN, entry_cid='soup-entry', quantity=4),
            {'type': 'intent', 'command_id': 'soup-entry', 'at': T0 + 3000},   # other lane records are ignored
            native('stop_confirmed', T0 + 7000, quantity=4, price=12020.0),
            native('tp1', T0 + 9000, command_id='native-tp1:' + TRADE, quantity=2, price=11960.0),
            native('modify', T0 + 77000, command_id='native-stop:%s:1' % TRADE, quantity=2, price=11995.0,
                   why='break_even'),
            native('modify_confirmed', T0 + 81000, command_id='native-stop:%s:1' % TRADE)]


def lane_initial():
    return lane_be('')[:3]


CASH_TS = '2021-03-10T18:43:00.000Z'      # Tradovate's own cash-row time (18:43:00 UTC, during the fixture trade)


def cash(amount=1234.5, realized=-50.0, ts=CASH_TS):
    """A tvbridge cash row (the daemon passes Tradovate's cashBalance values through)."""
    return {'amount': amount, 'amount_sod': amount - realized, 'realized_pnl': realized, 'week_realized_pnl': -72.25,
            'trade_date': {'year': 2021, 'month': 3, 'day': 10}, 'timestamp': ts}


def account_reply(synced=True, complete=True, row=None):
    """The daemon's GET /account reply (tvbridge.account.v1) for the pinned account."""
    return {'schema': 'tvbridge.account.v1', 'account_id': 100001, 'account_name': 'DEMO-0001',
            'soup_account_id': 'Account A', 'contract': {'id': 1000001, 'name': 'MNQH1'}, 'synced': synced,
            'healthy': True, 'problems': [], 'entries_disabled': False, 'publish_mode': 'adapter-db',
            'net_position': 0, 'open_order_ids': [], 'unresolved_commands': 0, 'observed_at': int(time.time() * 1000),
            'account_data': {'cash_balance': cash() if row is None else row, 'risk_status': None, 'auto_liq': None,
                             'margin': None, 'updated_at': int(time.time() * 1000) - 5000, 'complete': complete}}


def lane_skipped():
    return [native('start', T0 + 3000, plan=PLAN, entry_cid='soup-entry', quantity=4),
            native('skipped', T0 + 7000, reason='entry_rejected_zero_fill', command_id='soup-entry'),
            native('done', T0 + 9000, result='entry_skipped')]


class Env:
    """A temporary bridge home for one scenario. Patches the panel's paths and local readers."""

    def __init__(self, tmp):
        self.root = Path(tmp)
        for d in ('guard', 'journal', 'soup', 'journal-account-b', 'journal-account-c'):
            (self.root / d).mkdir()
        self.bridge_calls = []
        page = {'state': 'authorized', 'synced': True, 'epoch': 7, 'url': DEMO, 'lastRxAgoMs': 420,
                'reconnectAttempts': 0}
        self.health = {'health': {'ok': True, 'problems': [], 'page': page}}
        self.state = {'net_position': 0, 'open_order_ids': [], 'unresolved_commands': 0,
                      'contract': {'id': 1000001, 'name': 'MNQH1'}}
        self.report = None           # /followers reply; None = the route answers an error
        self.account = account_reply()   # 1.4: /account reply (A's balance); None = the route answers an error
        self.firing_unit = {'load': 'not-found', 'active': 'inactive', 'sub': 'dead', 'result': 'success'}
        self.units = {'tvbridge-browser.service': unit(), 'tvbridge-daemon.service': unit(),
                      'tvbridge-login-guard.timer': unit(sub='waiting')}
        self.now = time.time()
        self.write_json('guard/status.json', {'state': 'ok', 'reason': 'logged_in_daemon_active',
                                              'checked_at': self.iso(self.now - 8), 'logged_out_since': None,
                                              'logins_last_hour': 0, 'last_login_attempt_at': None})
        (self.root / 'guard' / 'events.jsonl').write_text('')
        self.write_json('soup/status.json', {
            'phase': 'flat', 'mode': 'bridge', 'manual_enabled': True, 'locked': False, 'enabled': True,
            'worker_ok': True, 'quote_fresh': True, 'updated_at': int(self.now * 1000) - 900,
            'outbound_attempts': 405, 'accepted_receipts': 404, 'rejected_receipts': 1, 'unknown_receipts': 0,
            'remaining_qty': 0, 'market_session': {'state': 'open', 'allow_entries': True},
            'native_exits': {'alerts': [], 'entries_blocked': False, 'external_close': None, 'failed': None,
                             'fallback': None, 'trade_mode': 'native', 'configured': True}})
        self.write_json('mirror/status.json', {'counts': {'mirrored': 7, 'accepted': 7, 'rejected': 0, 'unknown': 0},
                                               'last_error': None, 'updated_at': 1615398000000})
        self.config(followers=True)

    @staticmethod
    def iso(ts):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    def write_json(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))

    def write_lines(self, rel, recs):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(''.join((r if isinstance(r, str) else json.dumps(r)) + '\n' for r in recs))

    def config(self, followers=True, account_view=True):
        cfg = {'account_name': 'DEMO-0001', 'account_id': 100001, 'soup_account_id': 'Account A',
               'ticker': 'MNQH2021', 'kill_switch_file': str(self.root / 'ENTRIES-DISABLED'),
               'journal_dir': str(self.root / 'journal'), 'listen_port': 8777, 'publish_mode': 'adapter-db',
               'account_view_enabled': account_view}   # 1.4: the deployed config opts in to GET /account
        if followers:
            cfg['followers'] = [
                {'key': 'account-b', 'label': 'Account B', 'account_id': 100002,
                 'journal_dir': str(self.root / 'journal-account-b'),
                 'kill_switch_file': str(self.root / 'ENTRIES-DISABLED-account-b'),
                 'enabled': True, 'prop_profile': 'unrestricted', 'native_exits_enabled': True},
                {'key': 'account-c', 'label': 'Account C (demo)', 'account_id': 100003,
                 'journal_dir': str(self.root / 'journal-account-c'),
                 'kill_switch_file': str(self.root / 'ENTRIES-DISABLED-account-c'),
                 'enabled': True, 'prop_profile': 'unrestricted', 'native_exits_enabled': True}]
        self.write_json('config.json', cfg)

    def kill(self, name, on=True):
        p = self.root / name
        if on:
            p.write_text('')
        elif p.exists():
            p.unlink()

    def firing(self, enabled=('account-b', 'account-c'), lanes=None, alerts=(), age_s=1.0):
        """Firing service installed and active (Stage B/C), with its settings, status and alerts."""
        self.firing_unit = {'load': 'loaded', 'active': 'active', 'sub': 'running', 'result': 'success'}
        self.write_json('firing.json', {'firing_dir': str(self.root / 'firing'),
                                        'firing_order': ['account-a', 'account-b', 'account-c'],
                                        'accounts': {'account-a': {'role': 'main'},
                                                     'account-b': {'role': 'follower', 'enabled': 'account-b' in enabled},
                                                     'account-c': {'role': 'follower', 'enabled': 'account-c' in enabled}}})
        lanes = lanes or {}
        status_lanes = {}
        for key in ('account-b', 'account-c'):
            if key in enabled:
                status_lanes[key] = dict({'enabled': True, 'running': True, 'baseline_done': True,
                                          'native': {'blocked': False, 'trades': {}}}, **lanes.get(key, {}))
            else:
                status_lanes[key] = {'enabled': False, 'running': False, 'unavailable': None}
        self.write_json('firing/status.json', {'schema': 'tvbridge.firing.v2', 'updated_at': int((self.now - age_s) * 1000),
                                               'firing_order': ['account-a', 'account-b', 'account-c'],
                                               'main': 'account-a', 'follower_exits': 'native',
                                               'lanes': status_lanes, 'leader_external': {}, 'grid': {},
                                               'alerts': [a for a in alerts][-20:]})
        self.write_lines('firing/alerts.jsonl', alerts)

    def followers_report(self, rb, rc, main=None):
        self.report = {'schema': 'tvbridge.followers.v2', 'observed_at': int(self.now * 1000),
                       'followers': [rb, rc], 'main': main or row('main', 100001)}

    def leader_trade(self, upto, qty):
        """Account A in the fixture short: `upto` = evidence sequence (9 initial 4, 32 break-even 2)."""
        j = self.root / 'journal'
        (j / 'commands.jsonl').write_text(''.join(l + '\n' for l in lines('trade-commands.jsonl')))
        (j / 'snapshots.jsonl').write_text(''.join(l + '\n' for l in fixture_snapshots(upto)))
        (self.root / 'soup' / 'broker-context.json').write_text(json.dumps(fixture_context()))
        self.state.update(net_position=-qty, open_order_ids=[STOP_ID, '1000003'])

    # --- patched readers ------------------------------------------------------
    def fake_bridge(self, endpoint, port):
        self.bridge_calls.append(endpoint)
        value = {'health': self.health, 'state': self.state, 'followers': self.report,
                 'account': self.account}.get(endpoint)
        return (value, None) if value is not None else (None, 'HTTPError')

    def patches(self):
        r = self.root
        out = [mock.patch.object(server, n, v) for n, v in (
            ('ROOT', r), ('CONFIG', r / 'config.json'), ('MIRROR', r / 'mirror' / 'status.json'),
            ('SOUP', r / 'soup' / 'status.json'), ('GUARD_DIR', r / 'guard'),
            # 1.4: the /account cache lives inside the scenario
            ('BALANCE_CACHE', {}))]
        out += [mock.patch.object(server, 'daemon_log', lambda: (['bridge ready', 'publisher: skipped'], 0)),
                mock.patch.object(server, 'bridge', self.fake_bridge),
                mock.patch.object(server, 'units', lambda: self.units),
                mock.patch.object(server, 'unit_state', lambda name: dict(self.firing_unit))]
        return out


def build(env, name):
    """Set up one named scenario in `env`."""
    kb, kc = 'ENTRIES-DISABLED-account-b', 'ENTRIES-DISABLED-account-c'
    flatb, flatc = row('account-b', 100002), row('account-c', 100003)
    if name == 'linked_not_trading':          # today: pinned, kill switches on, firing not installed
        env.kill(kb), env.kill(kc)
        env.followers_report(flatb, flatc)
    elif name == 'stage_b':                   # B on, C still off (kill switch on, not enabled)
        env.kill(kc)
        env.firing(enabled=('account-b',))
        env.followers_report(flatb, flatc)
    elif name == 'all_in_trade_be':           # Stage C: all three short 2 with break-even stops
        env.leader_trade(32, 2)
        env.firing(lanes={'account-b': {'native': {'blocked': False, 'trades': {TRADE: {'mode': 'native'}}}},
                          'account-c': {'native': {'blocked': False, 'trades': {TRADE: {'mode': 'native'}}}}})
        env.write_lines('firing/account-b/mirror-journal.jsonl', lane_be(EB))
        env.write_lines('firing/account-c/mirror-journal.jsonl', lane_be(EC))
        stp = [order(801, 'STP', 2)]
        env.followers_report(row('account-b', 100002, -2, stp), row('account-c', 100003, -2, [order(901, 'STP', 2)]),
                             main=row('main', 100001, -2, [order(STOP_ID, 'STP', 2)]))
    elif name == 'skipped_c':                # Stage C: C refused before its start; A and B in the trade
        env.leader_trade(9, 4)
        skip = {'type': 'alert', 'account': 'account-c', 'kind': 'follower_entry_skipped', 'level': 'info',
                'trade_id': TRADE, 'reason': 'entry_rejected_zero_fill', 'at': T0 + 7000}
        env.firing(alerts=[skip])
        env.write_lines('firing/account-b/mirror-journal.jsonl', lane_initial())
        env.write_lines('firing/account-c/mirror-journal.jsonl', lane_skipped())
        env.followers_report(row('account-b', 100002, -4, [order(801, 'STP', 4), order(802, 'LMT', 2, tp1=True)]),
                             flatc, main=row('main', 100001, -4, [order(STOP_ID, 'STP', 4),
                                                                     order('1000003', 'LMT', 2, tp1=True)]))
    elif name == 'follower_alert':            # B: exit not verified (blocked); C closed with A
        alerts = [{'type': 'alert', 'account': 'account-c', 'kind': 'native_exit', 'trade_id': TRADE,
                   'reason': 'leader_closed_externally', 'source': 'bridge_view', 'at': T0 + 520000},
                  {'type': 'alert', 'account': 'account-b', 'kind': 'native_exit', 'trade_id': TRADE,
                   'reason': 'leader_closed_externally', 'source': 'bridge_view', 'at': T0 + 524000},
                  {'type': 'alert', 'account': 'account-b', 'kind': 'exit_all_unverified', 'trade_id': TRADE,
                   'reason': 'leader_closed_externally', 'at': T0 + 540000}]
        env.kill(kb)
        env.firing(alerts=alerts, lanes={'account-b': {'native': {'blocked': True, 'trades': {
            TRADE: {'mode': 'native', 'exit': 'leader_closed_externally', 'failed': 'leader_closed_externally'}}}}})
        env.write_lines('firing/account-b/' + server.LANE_BLOCK_NAME, [])
        env.write_lines('firing/account-b/mirror-journal.jsonl', lane_be(EB) + [
            native('exit', T0 + 524000, reason='leader_closed_externally', source='bridge_view'),
            native('failed', T0 + 540000, reason='leader_closed_externally', kill_switch=True)])
        env.write_lines('firing/account-c/mirror-journal.jsonl', lane_be(EC) + [
            native('exit', T0 + 520000, reason='leader_closed_externally'), native('done', T0 + 532000, result='flat')])
        env.followers_report(row('account-b', 100002, -1, [order(801, 'STP', 1)], killed=True), flatc)
    elif name == 'leader_kill_switch':        # A's kill switch on; followers linked, not trading
        env.kill('ENTRIES-DISABLED'), env.kill(kb), env.kill(kc)
        env.followers_report(flatb, flatc)
    elif name == 'leader_unhealthy':          # A's page session lost while Tradovate is logged in
        env.health = {'health': {'ok': False, 'problems': ['page_state:None', 'tradovate_silent'],
                                 'page': {'state': None, 'synced': None, 'url': DEMO, 'lastRxAgoMs': 48000,
                                          'reconnectAttempts': 3}}}
        env.state = None
        env.kill(kb), env.kill(kc)
        env.report = None
        env.account = None   # 1.4: /account fails with the rest of the daemon
    elif name == 'no_followers_configured':   # the 1.0.6.2 daemon config: no followers block
        env.config(followers=False)
    elif name == 'daemon_without_followers_route':
        env.kill(kb), env.kill(kc)
        env.report = None
    else:
        raise KeyError(name)


def scenario_snapshot(name, tmp_dir=None):
    """The /api/status JSON the 1.3 panel serves in one scenario (also used by the screenshot harness)."""
    with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
        env = Env(tmp)
        build(env, name)
        ps = env.patches()
        for p in ps:
            p.start()
        try:
            s = server.snapshot()
        finally:
            for p in ps:
                p.stop()
        s = json.loads(json.dumps(s, default=str))
        s['_scenario'] = name
        s['_bridge_calls'] = env.bridge_calls
        return s


def visible_text(s):
    """Every string the page shows from the snapshot outside the collapsed details (cards, hero, alerts)."""
    out = [s['verdict']['title'], s['verdict']['detail']]
    g = s.get('glance') or {}
    out += [(g.get('trade') or {}).get('text'), (g.get('followers') or {}).get('text')]
    for c in s['followers']['cards']:
        st, b, res = c['status'], c.get('balance') or {}, (c['alert'] or {}).get('resolved') or {}
        out += [c['name'], c.get('sub'), st['label'], st['reason'], c['kill_switch']['text'], c['position']['text'],
                c['orders']['text'], c['stop'].get('line'), c['stop'].get('when'), c['alert']['text'],
                b.get('text'), b.get('as_of_text'), b.get('reason'), res.get('how'), res.get('note')]   # 1.4
        if c.get('note'):
            out += [c['note']['title'], c['note']['text']]
    for a in s['followers']['alerts']:
        res = a.get('resolved') or {}
        out += [a['account_label'], a['text'], a.get('note'), res.get('how'), res.get('note')]   # 1.4
    return [x for x in out if isinstance(x, str)]


class FollowerCase(unittest.TestCase):
    def snap(self, name):
        return scenario_snapshot(name, os.environ.get('PANEL_TEST_TMP'))

    def cards(self, s):
        return {c['key']: c for c in s['followers']['cards']}

    # --- today: pinned, not trading ---------------------------------------------------------

    def test_today_linked_not_trading(self):
        s = self.snap('linked_not_trading')
        c = self.cards(s)
        self.assertEqual(list(c), ['account-a', 'account-b', 'account-c'])
        self.assertEqual(c['account-a']['role'], 'leader')
        self.assertEqual(c['account-a']['status']['label'], 'Armed')
        for key in ('account-b', 'account-c'):
            st = c[key]['status']
            # An empty switch file (a safety event, or made by hand) reads as Bridge Advanced shows it.
            self.assertEqual((st['state'], st['label'], st['tone']), ('paused', 'Paused by safety', 'coral'))
            self.assertEqual(st['reason'], 'Paused by a safety event or by hand. Exits still work; new entries are '
                                           'refused. Also: follower service not installed.')
            self.assertEqual((c[key]['pause']['on'], c[key]['pause']['by']), (True, 'safety'))
            self.assertEqual(c[key]['kill_switch'], {'on': True, 'text': 'On · entries off'})
            self.assertEqual((c[key]['position']['text'], c[key]['orders']['text'], c[key]['stop']['line']),
                             ('Flat', 'none', 'none'))
            self.assertEqual(c[key]['alert']['text'], 'none')
            self.assertIsNone(c[key]['note'])
        self.assertEqual((c['account-c']['name'], c['account-c']['sub']), ('Account C', 'demo'))
        self.assertEqual(s['followers']['summary'], {'text': 'Both paused', 'tone': 'coral'})
        self.assertEqual(s['glance']['trade'], {'text': 'No open trade', 'tone': ''})
        self.assertEqual(s['followers']['service']['load'], 'not-found')
        self.assertEqual(s['followers']['alerts'], [])
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')

    def test_stage_b_b_trading_c_off(self):
        c = self.cards(self.snap('stage_b'))
        self.assertEqual((c['account-b']['status']['state'], c['account-b']['status']['label']), ('trading', 'Trading'))
        self.assertEqual(c['account-b']['status']['reason'], 'Takes each Soup trade after the leader.')
        self.assertEqual(c['account-c']['status']['state'], 'paused')
        self.assertEqual(c['account-c']['status']['reason'], 'Paused by a safety event or by hand. Exits still work; '
                         'new entries are refused. Also: turned off in the follower settings.')
        self.assertFalse(c['account-b']['pause']['on'])

    def test_follower_off_in_settings_without_kill_switch(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            env.firing(enabled=('account-b',))
            env.followers_report(row('account-b', 100002), row('account-c', 100003))
            s = self._run(env)
        c = self.cards(s)
        self.assertEqual((c['account-c']['status']['state'], c['account-c']['status']['label']), ('off', 'Off'))
        self.assertEqual(s['followers']['summary']['text'], 'B trading · C off')

    def _run(self, env):
        ps = env.patches()
        for p in ps:
            p.start()
        try:
            return json.loads(json.dumps(server.snapshot(), default=str))
        finally:
            for p in ps:
                p.stop()

    # --- Stage C: in a trade ------------------------------------------------------------------

    def test_all_three_in_trade_with_break_even(self):
        s = self.snap('all_in_trade_be')
        c = self.cards(s)
        self.assertEqual(c['account-a']['stop']['line'], '2 @ 11995.00 · break-even')
        self.assertEqual((c['account-a']['stop']['value'], c['account-a']['stop']['kind_label']),
                         ('2 @ 11995.00', 'break-even'))
        self.assertEqual(c['account-a']['stop']['when'], 'confirmed by Tradovate 18:41:16.000 UTC')
        self.assertEqual(c['account-a']['position']['text'], 'Short 2')
        self.assertEqual(c['account-a']['orders']['text'], 'stop 2')
        for key in ('account-b', 'account-c'):
            self.assertEqual(c[key]['status']['label'], 'Trading')
            self.assertEqual(c[key]['position']['text'], 'Short 2')
            self.assertEqual(c[key]['stop']['line'], '2 @ 11995.00 · break-even')
            self.assertEqual(c[key]['stop']['when'], 'confirmed by Tradovate 18:41:21.000 UTC')
            self.assertEqual(c[key]['stop']['tone'], 'mint')
        self.assertEqual(s['glance']['trade']['text'], 'All 3 short 2')
        self.assertEqual(s['followers']['summary'], {'text': 'Both trading', 'tone': 'mint'})
        # the 1.2 stop evidence is unchanged
        self.assertEqual(s['stop_view']['summary'], '2 @ 11995.00 · break-even · confirmed by Tradovate 18:41:16.000 UTC')

    def test_initial_stop_and_tp1_order_words(self):
        c = self.cards(self.snap('skipped_c'))
        self.assertEqual(c['account-b']['stop']['line'], '4 @ 12020.00 · initial stop')
        self.assertEqual(c['account-b']['stop']['tone'], 'teal')
        self.assertEqual(c['account-b']['orders']['text'], 'stop 4 · TP1 limit 2')
        self.assertEqual(self.snap('skipped_c')['glance']['trade']['text'], 'A short 4 · B short 4')
        self.assertEqual(c['account-a']['stop']['line'], '4 @ 12020.00 · initial stop')

    def test_follower_stop_missing_from_working_orders_is_flagged(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            build(env, 'all_in_trade_be')
            env.report['followers'][0]['orders'] = []
            c = self.cards(self._run(env))
        self.assertEqual((c['account-b']['stop']['when'], c['account-b']['stop']['tone']),
                         ('not in its working orders right now', 'amber'))

    def test_break_even_move_waiting_for_tradovate(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            build(env, 'all_in_trade_be')
            env.write_lines('firing/account-b/mirror-journal.jsonl', lane_be(EB)[:-1])
            c = self.cards(self._run(env))
        self.assertEqual(c['account-b']['stop']['line'], '4 @ 12020.00 · initial stop')
        self.assertEqual(c['account-b']['stop']['when'], 'moving to break-even @ 11995.00, waiting for Tradovate')

    # --- C skipped: calm note, never a fault -----------------------------------------------------

    def test_c_skipped_is_a_calm_note(self):
        s = self.snap('skipped_c')
        c = self.cards(s)['account-c']
        self.assertEqual(c['status']['state'], 'trading')      # still follows the next trade
        self.assertEqual(c['stop'], {'trade_id': TRADE, 'state': 'skipped', 'line': 'none: skipped this trade',
                                     'when': None, 'tone': 'info'})
        self.assertEqual(c['note'], {'title': 'C skipped a trade at 18:40', 'tone': 'info',
                                     'text': 'No action needed. This is expected while the account is not open for trading yet.'})
        self.assertEqual(c['alert']['tone'], 'info')
        a = s['followers']['alerts'][0]
        self.assertEqual((a['level'], a['tone'], a['account_label']), ('info', 'info', 'Account C'))
        self.assertEqual(a['text'], 'Skipped a trade: the entry was refused and nothing filled')
        self.assertEqual(s['verdict']['tone'], 'mint')           # A is unaffected
        self.assertEqual(s['followers']['summary'], {'text': 'Both trading · C skipped a trade', 'tone': 'mint'})
        for text in visible_text(s):
            self.assertNotRegex(text.lower(), r'fault|error|failed|problem')

    # --- follower alerts ------------------------------------------------------------------------

    def test_follower_alert_blocks_b_only(self):
        s = self.snap('follower_alert')
        c = self.cards(s)
        self.assertEqual((c['account-b']['status']['state'], c['account-b']['status']['tone']), ('blocked', 'coral'))
        self.assertIn('exit was not verified', c['account-b']['status']['reason'])
        self.assertEqual(c['account-b']['alert']['text'], 'Exit not verified: its entries are blocked')
        self.assertEqual(c['account-b']['stop']['line'], 'exit not verified')
        self.assertEqual(c['account-c']['alert']['text'], 'Closed with the leader: the leader was closed outside the bridge')
        self.assertEqual(c['account-c']['stop']['line'], 'none')
        self.assertEqual([a['kind'] for a in s['followers']['alerts']],
                         ['exit_all_unverified', 'native_exit', 'native_exit'])   # newest first
        self.assertEqual(s['followers']['summary']['tone'], 'coral')
        self.assertEqual(s['verdict']['tone'], 'mint')   # a follower never changes A's verdict (watcher faults use it)

    def test_alert_words(self):
        labels = {'account-b': 'Account B'}
        for kind, reason, want in (
                ('order_unknown', None, 'Order outcome unknown: its entries stay blocked until it is reconciled'),
                ('native_exit', 'position_mismatch', 'Closed: its position changed outside the bridge'),
                ('entry_not_fired', 'align_timeout:position', 'Entry not sent (align_timeout:position)'),
                ('lane_stopped', None, 'Follower lane stopped'),
                ('something_new', 'x', 'something new (x)')):
            a = server.alert_view({'account': 'account-b', 'kind': kind, 'reason': reason, 'at': T0}, labels)
            self.assertEqual(a['text'], want)
        self.assertEqual(server.alert_view({'kind': 'order_rejected', 'level': 'info'}, {})['tone'], 'info')

    def test_firing_status_stale_needs_a_look(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            env.firing(age_s=45)
            env.followers_report(row('account-b', 100002), row('account-c', 100003))
            c = self.cards(self._run(env))
        self.assertEqual(c['account-b']['status']['label'], 'Needs a look')
        self.assertIn('follower service status is 45 s old', c['account-b']['status']['reason'].lower())

    def test_lane_stopped_and_not_linked(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            env.firing(lanes={'account-b': {'running': False}})
            env.followers_report(row('account-b', 100002), row('account-c', 100003, available=False))
            c = self.cards(self._run(env))
        self.assertEqual(c['account-b']['status']['state'], 'attention')
        self.assertIn('its follower lane is stopped', c['account-b']['status']['reason'].lower())
        self.assertEqual((c['account-c']['status']['state'], c['account-c']['status']['tone']), ('not_linked', 'coral'))
        self.assertIn('account_not_in_login', c['account-c']['status']['reason'])

    # --- pauses set from Bridge Advanced (same reading as tvbridge-admin read_switch) ---------------

    def admin_pause(self, env, name, reason='Paused for review', at=1615403400.5):
        (env.root / name).write_text(json.dumps({'set_by': 'tvbridge-admin', 'reason': reason,
                                                 'change_id': 'c-test', 'at': at}, indent=2) + '\n')

    def snap_with(self, name, setup):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            build(env, name)
            setup(env)
            ps = env.patches()
            for p in ps:
                p.start()
            try:
                return json.loads(json.dumps(server.snapshot(), default=str))
            finally:
                for p in ps:
                    p.stop()

    def test_admin_pause_shows_paused_with_reason(self):
        s = self.snap_with('stage_b', lambda env: self.admin_pause(env, 'ENTRIES-DISABLED-account-b'))
        c = self.cards(s)['account-b']
        self.assertEqual((c['status']['state'], c['status']['label'], c['status']['tone']), ('paused', 'Paused', 'amber'))
        self.assertEqual(c['status']['reason'], 'Paused from Bridge Advanced: Paused for review. '
                                                'Exits still work; new entries are refused.')
        p = c['pause']
        self.assertEqual((p['on'], p['by'], p['label'], p['reason'], p['at']),
                         (True, 'admin', 'Paused', 'Paused for review', 1615403400.5))
        self.assertRegex(p['time_text'], r'^\d\d:\d\d:\d\d( [A-Z]{2,5})?$')
        self.assertEqual(c['kill_switch'], {'on': True, 'text': 'On · entries off'})
        self.assertEqual(s['followers']['summary']['text'], 'Both paused')   # C's safety switch is on in stage_b

    def test_admin_pause_on_unsynced_account_still_reads_paused(self):
        def setup(env):
            self.admin_pause(env, 'ENTRIES-DISABLED-account-b')
            env.followers_report(row('account-b', 100002, synced=False), row('account-c', 100003))
        c = self.cards(self.snap_with('stage_b', setup))['account-b']
        self.assertEqual(c['status']['label'], 'Paused')
        self.assertTrue(c['status']['reason'].endswith('Also: not synced with Tradovate right now.'))

    def test_resume_clears_pause(self):
        def setup(env):
            self.admin_pause(env, 'ENTRIES-DISABLED-account-b')
            env.kill('ENTRIES-DISABLED-account-b', on=False)
        c = self.cards(self.snap_with('stage_b', setup))['account-b']
        self.assertEqual(c['status']['label'], 'Trading')
        self.assertEqual(c['pause'], {'on': False, 'by': None, 'label': None, 'reason': None, 'at': None,
                                      'time_text': None})

    def test_unreadable_or_foreign_note_is_a_safety_pause(self):
        for text in ('not json', json.dumps({'set_by': 'someone-else', 'reason': 'x'}), json.dumps([1])):
            c = self.cards(self.snap_with('stage_b', lambda env: (env.root / 'ENTRIES-DISABLED-account-b')
                                          .write_text(text)))['account-b']
            self.assertEqual((c['status']['label'], c['pause']['by']), ('Paused by safety', 'safety'), text)

    def test_fault_outranks_pause(self):
        s = self.snap_with('follower_alert', lambda env: self.admin_pause(env, 'ENTRIES-DISABLED-account-b'))
        c = self.cards(s)['account-b']
        self.assertEqual(c['status']['label'], 'Blocked')
        self.assertTrue(c['pause']['on'])

    def test_admin_pause_on_leader(self):
        s = self.snap_with('stage_b', lambda env: self.admin_pause(env, 'ENTRIES-DISABLED', reason='weekend changes'))
        c = self.cards(s)['account-a']
        self.assertEqual((c['status']['state'], c['status']['label'], c['status']['tone']), ('paused', 'Paused', 'amber'))
        self.assertEqual(c['status']['reason'], 'Paused from Bridge Advanced: weekend changes. Exits still work; new '
                                                'entries are refused. Followers on this webhook take no new entries either.')
        self.assertEqual((c['pause']['by'], c['pause']['reason']), ('admin', 'weekend changes'))

    # --- A ----------------------------------------------------------------------------------------

    def test_leader_kill_switch(self):
        s = self.snap('leader_kill_switch')
        c = self.cards(s)
        self.assertEqual((s['verdict']['tone'], s['verdict']['title']), ('teal', 'Healthy · entries off'))
        self.assertEqual((c['account-a']['status']['label'], c['account-a']['status']['tone'],
                          c['account-a']['kill_switch']['text']), ('Paused by safety', 'coral', 'On · entries off'))
        self.assertIn('Followers on this webhook take no new entries either.', c['account-a']['status']['reason'])
        self.assertIn("the leader's kill switch also stops follower entries", c['account-b']['status']['reason'])

    def test_leader_unhealthy(self):
        s = self.snap('leader_unhealthy')
        c = self.cards(s)
        self.assertEqual(s['verdict']['title'], 'Bridge disconnected from page')
        self.assertEqual(c['account-a']['status']['label'], 'Not ready')
        self.assertEqual(c['account-a']['position']['text'], 'unknown')
        self.assertEqual(s['glance']['trade']['text'], 'Position unknown')
        for key in ('account-b', 'account-c'):
            self.assertEqual((c[key]['status']['state'], c[key]['position']['text']), ('unknown', 'unknown'))
        self.assertEqual(s['followers']['summary']['text'], 'Follower status unknown')

    def test_soup_native_state_is_the_leaders_last_alert(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            soup = json.loads((env.root / 'soup' / 'status.json').read_text())
            soup['native_exits'].update(entries_blocked=True, failed={'reason': 'external_flat_unreconciled'},
                                        external_close={'trade_id': TRADE, 'entry_qty': 4})
            env.write_json('soup/status.json', soup)
            env.followers_report(row('account-b', 100002), row('account-c', 100003))
            c = self.cards(self._run(env))
        self.assertEqual(c['account-a']['alert'], {'text': 'Closed outside the bridge: entries blocked until the '
                                                             'reviewed recovery', 'tone': 'coral', 'time_text': None,
                                                     'at': None, 'resolved': None, 'live': True})   # 1.4 fields

    # --- old daemons and read failures ---------------------------------------------------------------

    def test_no_followers_configured_asks_nothing_new(self):
        s = self.snap('no_followers_configured')
        self.assertEqual(s['_bridge_calls'], ['health', 'state', 'account'])   # 1.4: /account (balance); no /followers
        self.assertEqual([c['key'] for c in s['followers']['cards']], ['account-a'])
        self.assertEqual(s['followers']['summary']['text'], 'No followers set up')

    def test_followers_route_missing(self):
        s = self.snap('daemon_without_followers_route')
        self.assertEqual(s['_bridge_calls'], ['health', 'state', 'followers', 'account'])   # 1.4: + /account
        self.assertEqual(self.cards(s)['account-b']['status']['label'], 'Link unknown')
        self.assertEqual(s['followers']['error'], 'HTTPError')

    def test_follower_read_error_keeps_the_panel(self):
        with mock.patch.object(server, 'followers_snapshot', side_effect=RuntimeError('boom')):
            s = self.snap('linked_not_trading')
        self.assertEqual(s['followers']['error'], 'error:RuntimeError')
        self.assertEqual([c['role'] for c in s['followers']['cards']], ['leader'])
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')

    def test_lane_journal_garbage_is_skipped(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP')) as tmp:
            env = Env(tmp)
            build(env, 'all_in_trade_be')
            p = env.root / 'firing/account-b/mirror-journal.jsonl'
            p.write_text('{"native" torn\n[1,2]\n' + p.read_text() + '{"type": "native", "event"')
            c = self.cards(self._run(env))
        self.assertEqual(c['account-b']['stop']['line'], '2 @ 11995.00 · break-even')

    # --- copy and safety --------------------------------------------------------------------------

    def test_no_mirror_wording_in_any_scenario(self):
        for name in SCENARIOS:
            s = self.snap(name)
            for text in visible_text(s):
                self.assertNotIn('mirror', text.lower(), (name, text))

    def test_page_main_ui_has_no_mirror_wording(self):
        page = server.PAGE
        body = page[page.index('<body>'):page.index('<script>')]
        # The old copy service has no section on the page any more (its data stays in the JSON).
        self.assertNotIn('id="legacy"', body)
        self.assertNotIn('copy service', body.lower())
        self.assertNotIn('mirror', body.lower())
        # In the script, 'mirror' appears only as data keys, never inside a visible string literal.
        script = page[page.index('<script>'):]
        for lit in re.findall(r"'([^'\n]*)'", script):
            self.assertNotIn('mirror', lit.lower(), lit)
        self.assertIn('<title>Tradovate bridge | Decrypt OS</title>', page)   # window rules match this title

    def test_verdict_says_direct_with_exact_payload(self):
        v = self.snap('linked_not_trading')['verdict']
        self.assertIn("receives the strategy's orders directly, with the exact payload", v['detail'])

    def test_headline_copy_has_no_technical_ids(self):
        for name in SCENARIOS:
            s = self.snap(name)
            head = [s['verdict']['title'], s['glance']['trade']['text'], s['glance']['followers']['text']] + \
                [c['name'] for c in s['followers']['cards']] + [c['status']['label'] for c in s['followers']['cards']]
            for text in head:
                self.assertIsNone(re.search(r'\d{6,}|DEMO|sim-\d|_', text), (name, text))

    def test_units_unchanged_for_the_watcher(self):
        s = self.snap('all_in_trade_be')
        self.assertEqual(sorted(s['units']), sorted(server.UNITS))
        self.assertNotIn(server.FIRING_UNIT, s['units'])
        self.assertIn('mirror', s)          # the old copy service's data stays in the JSON

    def test_still_read_only(self):
        src = Path(server.__file__).read_text()
        self.assertNotIn('do_POST', src)
        self.assertNotIn('/kill', src)
        self.assertIsNone(re.search(r"'systemctl', '--user', '(re)?start'|'systemctl', '--user', 'stop'", src))
        for bad in ('.write_text(', '.unlink(', 'os.remove', 'os.rename', "open(path, 'w", '.touch(', 'mkdir('):
            self.assertNotIn(bad, src)
        self.assertEqual(src.count('urlopen') + src.count('opener.open('), 1)
        self.assertEqual(sorted(re.findall(r"bridge\('(\w+)'", src)), ['account', 'followers', 'health', 'state'])  # 1.4: + /account
        page = server.PAGE
        self.assertNotIn('<button', page)
        self.assertNotIn('<form', page)
        self.assertEqual(re.findall(r"fetch\('([^']+)'", page), ['/api/status'])
        self.assertEqual(page.count('innerHTML'), 0)   # 2.0: text nodes only, no HTML from data


if __name__ == '__main__':
    unittest.main()
