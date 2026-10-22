#!/usr/bin/env python3
"""Resting-stop tests for the bridge status panel (1.2; 1.3 changes only the verdict copy,
the version and the /followers call count). Offline: no network,
no systemctl, no journalctl. Fixtures in fixtures/ are bridge journal and
evidence-log lines of one synthetic Account A sample trade (short 4 @ 12000.00, initial stop 12020.00,
TP1 2 @ 11960.00 filled 60 s after the entry, stop moved to break-even 11995.00 for the last 2; flat from
evidence sequence 33).
Run: python3 -m unittest test_stop -v"""
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest import mock

import server

HERE = Path(__file__).resolve().parent
TRADE = 'fixture-stream:MNQH2021:1615401600000:short'
ENTRY = 'soup-00000000000000000000000000000001'
BE_MODIFY = 'soup-00000000000000000000000000000003'
STOP_ID = '1000002'
TOKEN = 'x' * 32
DEMO = 'wss://demo.tradovateapi.com/v1/websocket'


def setUpModule():
    global _TZ
    _TZ = os.environ.get('TZ')
    os.environ['TZ'] = 'UTC'  # fixed zone for the expected clock texts
    time.tzset()


def tearDownModule():
    if _TZ is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = _TZ
    time.tzset()


def lines(name):
    return (HERE / 'fixtures' / name).read_text().splitlines()


def fixture_snapshots(upto=None):
    out = []
    for line in lines('trade-snapshots.jsonl'):
        if upto is not None and json.loads(line)['sequence'] > upto:
            break
        out.append(line)
    return out


def fixture_context(plan=True, commands=True):
    trade = {'id': TRADE, 'side': 'short', 'phase': 'open', 'tp1_done': True}
    if plan:
        trade['payload'] = {'trade_id': TRADE, 'side': 'short', 'quantity': 4, 'entry_price': 12000.0,
                            'break_even_price': 11995.0, 'initial_hard_stop_price': 12020.0}
    cmds = {}
    if commands:
        cmds = {ENTRY: {'id': ENTRY, 'kind': 'bracket_entry', 'reason': None},
                ENTRY + '#stop': {'id': ENTRY + '#stop', 'kind': 'stop', 'price': 12020.0, 'reason': 'initial_hard_stop'},
                BE_MODIFY: {'id': BE_MODIFY, 'kind': 'modify_stop', 'price': 11995.0, 'quantity': 2,
                            'reason': 'break_even'}}
    return {'schema': 'decrypt.soup-v3.broker-context.v1', 'engine': {'trade': trade, 'commands': cmds}}


class StopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('PANEL_TEST_TMP'))
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'guard').mkdir()
        (root / 'journal').mkdir()
        self.root = root
        patches = {'ROOT': root, 'CONFIG': root / 'config.json', 'MIRROR': root / 'mirror.json',
                   'SOUP': root / 'soup' / 'status.json', 'GUARD_DIR': root / 'guard'}
        (root / 'soup').mkdir()
        for name, value in patches.items():
            p = mock.patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.bridge_calls = []
        for name, fake in (('daemon_log', lambda: ([], 0)), ('bridge', self.fake_bridge),
                           ('units', self.fake_units)):
            p = mock.patch.object(server, name, fake)
            p.start()
            self.addCleanup(p.stop)
        page = {'state': 'authorized', 'synced': True, 'epoch': 7, 'url': DEMO, 'lastRxAgoMs': 400}
        self.health = {'health': {'ok': True, 'problems': [], 'page': page}}
        self.state = {'net_position': 0, 'open_order_ids': [], 'unresolved_commands': 0}

    def fake_units(self):
        u = {'active': 'active', 'sub': 'running', 'result': 'success', 'since': ''}
        return {k: dict(u) for k in server.UNITS}

    def fake_bridge(self, endpoint, port):
        self.bridge_calls.append(endpoint)
        return (self.health if endpoint == 'health' else self.state), None

    def write(self, commands=None, snapshots=None, context=None):
        j = self.root / 'journal'
        if commands is not None:
            (j / 'commands.jsonl').write_text(''.join(l + '\n' for l in commands))
        if snapshots is not None:
            (j / 'snapshots.jsonl').write_text(''.join(l + '\n' for l in snapshots))
        if context is not None:
            (self.root / 'soup' / 'broker-context.json').write_text(json.dumps(context))

    def open_short(self, qty):
        self.state = {'net_position': -qty, 'open_order_ids': [STOP_ID], 'unresolved_commands': 0}

    def snap(self):
        s = server.snapshot()
        json.dumps(s, default=str)
        return s

    # --- the fixture trade ---------------------------------------------------

    def test_fixture_initial_stop_full_quantity(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(9), fixture_context())
        self.open_short(4)
        s = self.snap()
        st, v = s['stop'], s['stop_view']
        self.assertEqual((st['quantity'], st['price'], st['kind'], st['kind_source']),
                         (4, 12020.0, 'initial', 'soup_trade_plan'))
        self.assertEqual((st['confirmed_at'], st['confirmed_text']), (1615401610000, '18:40:10.000 UTC'))
        self.assertEqual(v['summary'], '4 @ 12020.00 · initial stop · confirmed by Tradovate 18:40:10.000 UTC')
        self.assertEqual((v['pill'], v['tone']), ('Initial stop', 'teal'))
        self.assertIsNone(st['break_even'])

    def test_fixture_shrink_after_tp1_is_still_initial(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(11), fixture_context())
        self.open_short(2)
        v = self.snap()['stop_view']
        self.assertEqual(v['summary'], '2 @ 12020.00 · initial stop · confirmed by Tradovate 18:41:04.000 UTC')

    def test_fixture_break_even(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), fixture_context())
        self.open_short(2)
        s = self.snap()
        st, v = s['stop'], s['stop_view']
        self.assertEqual((st['quantity'], st['price'], st['kind'], st['order_id'], st['resting']),
                         (2, 11995.0, 'break_even', STOP_ID, True))
        self.assertEqual(st['confirmed_at'], 1615401676000)
        self.assertEqual(v['summary'], '2 @ 11995.00 · break-even · confirmed by Tradovate 18:41:16.000 UTC')
        self.assertEqual((v['pill'], v['tone']), ('Break-even', 'mint'))
        self.assertEqual(v['rows'][:3], [['Stop', '2 @ 11995.00'], ['Type', 'break-even'],
                                         ['Confirmed by Tradovate', '18:41:16.000 UTC']])
        self.assertEqual(st['break_even'], {'quantity': 2, 'price': 11995.0, 'price_text': '11995.00',
                                            'confirmed_at': 1615401676000, 'confirmed_text': '18:41:16.000 UTC'})
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')

    def test_fixture_break_even_from_soup_command_reason(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), fixture_context(plan=False))
        self.open_short(2)
        st = self.snap()['stop']
        self.assertEqual((st['kind'], st['kind_source']), ('break_even', 'soup_command_reason'))

    def test_fixture_break_even_without_soup_context_is_unknown(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32))
        self.open_short(2)
        s = self.snap()
        self.assertEqual(s['stop']['kind'], 'unknown')
        self.assertEqual(s['stop_view']['summary'], '2 @ 11995.00 · type unknown · confirmed by Tradovate 18:41:16.000 UTC')
        self.assertEqual((s['stop_view']['pill'], s['stop_view']['tone']), ('Type unknown', 'amber'))
        self.assertIsNone(s['stop']['break_even'])

    def test_fixture_other_trade_in_context_is_not_used(self):
        ctx = fixture_context()
        ctx['engine']['trade']['id'] = 'another-trade'
        ctx['engine']['commands'] = {}
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), ctx)
        self.open_short(2)
        self.assertEqual(self.snap()['stop']['kind'], 'unknown')

    def test_fixture_initial_without_soup_context_from_bridge_journal(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(9))
        self.open_short(4)
        st = self.snap()['stop']
        self.assertEqual((st['kind'], st['kind_source']), ('initial', 'bridge_journal_entry'))

    def test_fixture_after_close_shows_no_open_trade_and_keeps_break_even(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(), fixture_context())
        s = self.snap()
        st, v = s['stop'], s['stop_view']
        self.assertIs(st['open'], False)
        self.assertIs(st['resting'], False)
        self.assertEqual(st['status'], 'cancelled')
        self.assertEqual((v['pill'], v['summary']), ('No open trade', 'No open trade'))
        self.assertEqual(st['break_even']['confirmed_text'], '18:41:16.000 UTC')

    # --- no trade, missing or partial data ------------------------------------

    def test_no_journal(self):
        s = self.snap()
        self.assertIsNone(s['stop'])
        self.assertEqual((s['stop_view']['pill'], s['stop_view']['summary']), ('No open trade', 'No open native trade'))

    def test_latest_trade_without_bridge_stop(self):
        plain = json.dumps({'at': 1, 'command_id': 'soup-plain', 'kind': 'entry', 'quantity': 1, 'side': 'long',
                            'trade_id': 'x:long', 'type': 'start'})
        self.write(lines('trade-commands.jsonl') + [plain], fixture_snapshots(32), fixture_context())
        self.assertIsNone(self.snap()['stop'])

    def test_missing_evidence_log(self):
        self.write(lines('trade-commands.jsonl'), None, fixture_context())
        self.open_short(2)
        s = self.snap()
        self.assertEqual(s['stop']['problem'], 'no_stop_evidence')
        self.assertEqual((s['stop_view']['pill'], s['stop_view']['summary']), ('Unknown', 'Resting stop unknown'))

    def test_bridge_state_unavailable_uses_evidence(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), fixture_context())
        self.state = None
        s = self.snap()
        self.assertTrue(s['stop']['resting'])
        self.assertEqual(s['stop']['kind'], 'break_even')

    def test_bridge_state_disagrees_with_evidence(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), fixture_context())
        self.state = {'net_position': -2, 'open_order_ids': [], 'unresolved_commands': 0}
        s = self.snap()
        self.assertIsNone(s['stop']['resting'])
        self.assertEqual(s['stop']['problem'], 'bridge_state_shows_stop_not_working')
        self.assertEqual(s['stop_view']['pill'], 'Unknown')

    def test_flat_with_stop_still_working(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), fixture_context())
        self.state = {'net_position': 0, 'open_order_ids': [STOP_ID], 'unresolved_commands': 0}
        v = self.snap()['stop_view']
        self.assertEqual((v['pill'], v['tone']), ('Stop still working', 'coral'))

    def test_cut_log_never_invents_a_confirmation_time(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), fixture_context())
        self.open_short(2)
        with mock.patch.object(server, 'SNAPSHOT_TAIL_BYTES', 3000):
            s = self.snap()
        self.assertEqual((s['stop']['quantity'], s['stop']['price'], s['stop']['kind']), (2, 11995.0, 'break_even'))
        self.assertIsNone(s['stop']['confirmed_at'])
        self.assertEqual(s['stop_view']['summary'], '2 @ 11995.00 · break-even · confirmed by Tradovate (time unknown)')
        with mock.patch.object(server, 'SNAPSHOT_TAIL_BYTES', 40000):  # window reaches back past the move
            self.assertEqual(self.snap()['stop']['confirmed_text'], '18:41:16.000 UTC')

    def test_torn_and_garbage_lines_are_skipped(self):
        snaps = fixture_snapshots(32)
        self.write(['not json', '[1,2]'] + lines('trade-commands.jsonl') + ['{"torn":'],
                   snaps[:15] + ['{garbage', '"str"'] + snaps[15:] + ['{"trade_id":"' + TRADE + '","orders":'],
                   fixture_context())
        self.open_short(2)
        self.assertEqual(self.snap()['stop_view']['summary'],
                         '2 @ 11995.00 · break-even · confirmed by Tradovate 18:41:16.000 UTC')

    def test_malformed_context_fails_soft(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), None)
        (self.root / 'soup' / 'broker-context.json').write_text('{"engine": [1,2]}')
        self.open_short(2)
        self.assertEqual(self.snap()['stop']['kind'], 'unknown')

    def test_error_never_breaks_the_panel(self):
        with mock.patch.object(server, 'resting_stop', side_effect=RuntimeError('boom')):
            s = self.snap()
        self.assertEqual(s['stop'], {'problem': 'error:RuntimeError'})
        self.assertEqual(s['stop_view']['pill'], 'Unknown')
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')

    # --- synthetic trade ------------------------------------------------------

    def test_synthetic_long_partial_stop(self):
        t = 1615559100000   # a synthetic long's entry decision (2021-03-12 14:25:00 UTC)
        tid, cid = 'soup-main:MNQM2021:%d:long' % t, 'soup-' + 'a' * 32
        commands = [
            json.dumps({'at': t + 2000, 'command_id': cid, 'kind': 'bracket_entry', 'quantity': 5, 'side': 'long',
                        'stop_price': 9978.75, 'trade_id': tid, 'type': 'start'}),
            json.dumps({'command_id': cid, 'legs': {'stop': '1000002'}, 'outcome': 'accepted', 'type': 'result',
                        'broker_order_id': '1000001'}),
            json.dumps({'at': t + 180000, 'command_id': 'soup-' + 'b' * 32, 'kind': 'modify_stop', 'quantity': 4,
                        'side': 'long', 'stop_price': 10006.5, 'target_order_id': 1000002, 'trade_id': tid,
                        'type': 'start'})]

        def snap(seq, at, qty, price, status='working', filled=0, pos=5):
            return json.dumps({'trade_id': tid, 'sequence': seq, 'observed_at': at, 'position_qty': pos,
                               'orders': [{'command_id': cid, 'broker_order_id': '1000001', 'status': 'filled'},
                                          {'command_id': cid + '#stop', 'broker_order_id': '1000002', 'role': 'stop',
                                           'quantity': qty, 'stop_price': price, 'status': status,
                                           'filled_qty': filled}]})
        ctx = {'engine': {'trade': {'id': tid, 'payload': {'break_even_price': 10006.5,
                                                           'initial_hard_stop_price': 9978.75}}}}
        self.write(commands, [snap(1, t + 4000, 5, 9978.75), snap(2, t + 182250, 4, 10006.5, pos=4),
                              snap(3, t + 184000, 4, 10006.5, 'partial', 1, pos=3)], ctx)
        self.state = {'net_position': 3, 'open_order_ids': ['1000002'], 'unresolved_commands': 0}
        s = self.snap()
        self.assertEqual(s['stop']['confirmed_at'], t + 182250)
        self.assertEqual(s['stop_view']['summary'],
                         '3 @ 10006.50 · break-even · confirmed by Tradovate %s' % server.clock_text(t + 182250))
        self.assertRegex(s['stop']['confirmed_text'], r'^\d\d:\d\d:\d\d\.250 UTC$')

    # --- behaviour kept -------------------------------------------------------

    def test_no_new_bridge_or_broker_calls(self):
        self.write(lines('trade-commands.jsonl'), fixture_snapshots(32), fixture_context())
        self.open_short(2)
        self.snap()
        self.assertEqual(self.bridge_calls, ['health', 'state'])
        src = Path(server.__file__).read_text()
        # the def, the two 1.1 calls and (1.3) GET /followers, asked only when config.json has followers
        self.assertEqual(src.count('bridge('), 5)   # 1.4: + the daemon's own /account (balance)
        self.assertEqual(src.count('urlopen') + src.count('opener.open('), 1)

    def test_page_escapes_stop_values(self):
        page = server.PAGE
        # 2.0: the stop rows go through dl() into the details drawer, and the page never assembles HTML
        # from data: no innerHTML, no insertAdjacentHTML, no outerHTML; every value is a text node.
        self.assertIn("for(const x of (Array.isArray(sv.rows)?sv.rows:[]))rows.push(['Stop · '+x[0],x[1]])", page)
        self.assertEqual(page.count('innerHTML'), 0)
        self.assertEqual(page.count('insertAdjacentHTML') + page.count('outerHTML') + page.count('document.write'), 0)
        self.assertIn("h('dt',null,k),h('dd',null,v??'—')", page)
        evil = '<img src=x onerror=alert(1)>'
        view = server.stop_view({'trade_id': evil, 'open': False})
        self.assertIn(['Last trade', evil], view['rows'])  # raw in JSON; escaped only by the page

    def test_clock_text(self):
        self.assertEqual(server.clock_text(1615401676000), '18:41:16.000 UTC')
        for bad in (None, 'x', True, 0, -5):
            self.assertIsNone(server.clock_text(bad))

    def test_panel_still_has_no_controls(self):
        src = Path(server.__file__).read_text()
        self.assertNotIn('do_POST', src)
        self.assertIsNone(re.search(r"'systemctl', '--user', '(re)?start'", src))
        self.assertEqual(server.VERSION, '2.1')


if __name__ == '__main__':
    unittest.main()
