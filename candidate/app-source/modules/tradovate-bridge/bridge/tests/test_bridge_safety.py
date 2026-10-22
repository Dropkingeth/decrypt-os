"""Safety checks: no size cap, retired max_contracts key, fail-closed kill switch, fail-closed cross-login
hedge check and the bracket reference-price check after a restart. In-memory doubles only."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import types
import urllib.error

from tvbridge import daemon, firing, mirror
from tvbridge.peers import LEADER_EXEMPT_ROLES, PeerHedge
from tvbridge.executor import (MARKET_PRICE_NAME, Executor, Refused, kill_switch_state,
                               pause_fields)
from tvbridge.followers import FollowerExecutor
from test_hedge_guard import HedgeGuardBase



# --- no size cap. The strategy alert sets the size; the retired max_contracts key
# may hold a legacy positive whole number (ignored); a malformed value never starts. -----------

RETIRED = 'max_contracts is retired'


class NoSizeCap(HedgeGuardBase):
    def test_executor_refuses_to_start_with_a_malformed_legacy_value(self):
        for bad in (0, -1, '5', 5.0, True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as ctx:
                    self.make_executor({'max_contracts': bad})
                self.assertIn(RETIRED, str(ctx.exception))

    def test_executor_starts_with_any_legacy_positive_value_or_none(self):
        for good in (1, 10, 11, 40, 1000, None):
            self.assertIsInstance(self.make_executor({'max_contracts': good}), Executor)

    def test_missing_key_starts(self):
        cfg = dict(self.executor.cfg)
        cfg.pop('max_contracts', None)
        self.assertIsInstance(Executor(self.page, self.state, self.journal, cfg), Executor)

    def test_large_entry_is_accepted_and_sent_as_is(self):
        self.executor.cfg['max_contracts'] = 2  # a legacy value is never a limit
        outcome, receipt = self.executor.send(self.payload(quantity=40))
        self.assertEqual(outcome, 'accepted', receipt)
        self.assertEqual(len(self.page.placed()), 1)

    def test_entry_quantity_must_be_a_positive_whole_number(self):
        for bad in (0, -1, 2.0, True, '3'):
            with self.subTest(quantity=bad):
                outcome, receipt = self.executor.send(self.payload(quantity=bad))
                self.assertEqual(outcome, 'rejected')
        self.assertEqual(self.page.placed(), [])


class DaemonStartLegacyKey(unittest.TestCase):
    def run_main(self, cfg):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'config.json'
        path.write_text(json.dumps(cfg))
        argv = ['tvbridge.daemon', '--config', str(path)]
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(daemon, 'Bridge', side_effect=AssertionError('browser touched')), \
                mock.patch.dict(os.environ, {'TVBRIDGE_TOKEN': 'x' * 32}):
            with self.assertRaises(SystemExit) as ctx:
                daemon.main()
        return str(ctx.exception)

    def test_daemon_refuses_a_malformed_legacy_value_before_the_browser(self):
        for bad in (0, -3, '3', 2.5):
            with self.subTest(value=bad):
                msg = self.run_main({'ticker': 'MNQZ2021', 'max_contracts': bad})
                self.assertIn(RETIRED, msg)
                self.assertIn('does not start', msg)


class FiringNoSizeCap(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.sent = []

    def make(self, policy_max):
        policy = self.tmp / 'policy.json'
        policy.write_text(json.dumps({'max_contracts': policy_max}))
        cfg = {'soup_db': str(self.tmp / 'soup' / 'engine.sqlite'), 'soup_engine_dir': str(self.tmp / 'engine'),
               'ticker': 'MNQZ2021', 'mirror_dir': str(self.tmp / 'mirror'), 'soup_quantity_policy': str(policy)}
        sent = self.sent

        class Transport:
            def send(self, payload):
                sent.append(payload)
                return 'accepted', {}

        m = mirror.Mirror(cfg, 'x' * 32, transport=Transport(), bridge_state=object(),
                          soup=(lambda *a, **k: {}, lambda value: 'digest'))
        self.addCleanup(m.close)
        return m

    def test_firing_refuses_a_policy_without_its_marker(self):
        # Soup's policy keeps max_contracts as its policy marker (1 or 10), never a size limit.
        for bad in (0, 11, 50, '10', None):
            with self.subTest(value=bad), self.assertRaises(ValueError) as ctx:
                self.make(bad)
            self.assertIn('policy marker', str(ctx.exception))

    def test_firing_sends_a_large_entry_as_is_and_refuses_a_malformed_one(self):
        m = self.make(10)
        tr = m._track('t1', 'long')
        for i, bad in enumerate((0, -2, 1.5, True, None)):
            outcome, receipt = m._send(tr, f'c-bad-{i}', 'entry', {'quantity': bad}, None, 'soup')
            self.assertEqual((outcome, receipt['mirror']), ('rejected', 'refused'))
        self.assertEqual(self.sent, [])
        self.assertEqual(m.journal.commands['c-bad-0']['outcome'], 'rejected')
        outcome, _ = m._send(tr, 'c2', 'entry', {'quantity': 26}, None, 'soup')
        self.assertEqual(outcome, 'accepted')
        self.assertEqual(self.sent, [{'quantity': 26}])


# --- the kill switch fails closed ---------------------------------------------------------

class KillSwitchFailsClosed(HedgeGuardBase):
    def kill_path(self):
        return self.executor.cfg['kill_switch_file']

    def assert_paused(self, reason):
        self.assertEqual(self.executor.entries_disabled_reason(), reason)
        self.assertTrue(self.executor.entries_disabled())
        outcome, receipt = self.entry('long')
        self.assertEqual(outcome, 'rejected')
        self.assertIn('entries disabled by kill switch', receipt['reason'])
        self.assertEqual(self.page.placed(), [])
        return receipt

    def test_absent_switch_in_a_usable_folder_is_armed(self):
        self.assertIsNone(self.executor.entries_disabled_reason())
        self.assertEqual(self.entry('long')[0], 'accepted')

    def test_switch_file_pauses(self):
        Path(self.kill_path()).write_text('')
        receipt = self.assert_paused('kill_switch_on')
        self.assertEqual(receipt['reason'], 'entries disabled by kill switch')

    def test_no_switch_configured_pauses(self):
        for value in (None, '', 'relative/ENTRIES-DISABLED', 7):
            with self.subTest(value=value):
                self.executor.cfg['kill_switch_file'] = value
                receipt = self.assert_paused('kill_switch_not_configured')
                self.assertTrue(receipt['reason'].endswith(': kill_switch_not_configured'))
        del self.executor.cfg['kill_switch_file']
        self.assert_paused('kill_switch_not_configured')

    def test_broken_symlink_pauses(self):
        os.symlink(self.tmp.name + '/nowhere/target', self.kill_path())
        self.assert_paused('kill_switch_on')

    def test_missing_folder_pauses(self):
        self.executor.cfg['kill_switch_file'] = self.tmp.name + '/gone/ENTRIES-DISABLED'
        self.assert_paused('kill_switch_folder_missing')

    def test_folder_that_is_a_file_pauses(self):
        Path(self.tmp.name, 'plain').write_text('')
        self.executor.cfg['kill_switch_file'] = self.tmp.name + '/plain/ENTRIES-DISABLED'
        self.assertIn(self.executor.entries_disabled_reason(),
                      ('kill_switch_unreadable:ENOTDIR', 'kill_switch_folder_unusable'))

    @unittest.skipIf(os.geteuid() == 0, 'root reads every folder')
    def test_unreadable_folder_pauses(self):
        folder = Path(self.tmp.name, 'locked')
        folder.mkdir()
        self.executor.cfg['kill_switch_file'] = str(folder / 'ENTRIES-DISABLED')
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o700)
        self.assertIn(self.executor.entries_disabled_reason(),
                      ('kill_switch_unreadable:EACCES', 'kill_switch_folder_unusable'))
        self.assertTrue(self.executor.entries_disabled())

    def test_master_switch_fails_closed_only_when_configured(self):
        self.executor.cfg['master_kill_switch_file'] = self.tmp.name + '/gone/MASTER'
        self.assertEqual(self.executor.entries_disabled_reason(), 'master_kill_switch_folder_missing')
        self.executor.cfg['master_kill_switch_file'] = self.tmp.name + '/MASTER'
        self.assertIsNone(self.executor.entries_disabled_reason())
        os.symlink(self.tmp.name + '/nowhere', self.tmp.name + '/MASTER')
        self.assertEqual(self.executor.entries_disabled_reason(), 'master_kill_switch_on')

    def test_exits_still_work_while_failed_closed(self):
        self.executor.cfg['kill_switch_file'] = None
        self.assertTrue(self.executor.entries_disabled())
        outcome, receipt = self.executor.send(self.payload('exit', 'long', quantity=None))
        self.assertNotIn('kill switch', receipt.get('reason', ''))

    def test_health_names_a_fail_closed_reason_only(self):
        self.assertEqual(pause_fields(self.executor), {'entries_disabled': False})
        Path(self.kill_path()).write_text('')
        self.assertEqual(pause_fields(self.executor), {'entries_disabled': True})
        os.remove(self.kill_path())
        self.executor.cfg['kill_switch_file'] = None
        self.assertEqual(pause_fields(self.executor), {'entries_disabled': True,
                                                       'entries_disabled_reason': 'kill_switch_not_configured'})

    def test_follower_checks_the_main_switch_fail_closed(self):
        cfg = dict(self.executor.cfg, follower_key='f1', account_id=None,
                   kill_switch_file=self.tmp.name + '/ENTRIES-DISABLED-f1')
        f = FollowerExecutor(self.page, self.state, self.journal, cfg,
                             main_kill_switch=self.tmp.name + '/gone/ENTRIES-DISABLED')
        self.assertEqual(f.entries_disabled_reason(), 'main_kill_switch_folder_missing')
        f.main_kill_switch = self.kill_path()
        self.assertIsNone(f.entries_disabled_reason())
        Path(self.kill_path()).write_text('')
        self.assertEqual(f.entries_disabled_reason(), 'main_kill_switch_on')


class KillSwitchState(unittest.TestCase):
    def test_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'K')
            self.assertIsNone(kill_switch_state(path))
            os.mkdir(path)
            self.assertEqual(kill_switch_state(path), 'kill_switch_on')   # anything at the path
            self.assertEqual(kill_switch_state(None), 'kill_switch_not_configured')


# --- the cross-login anti-hedge check fails closed --------------------------------------------

PEER_B = {'id': 'b', 'url': 'http://127.0.0.1:1', 'token_env': 'TVBRIDGE_TOKEN_B'}


def login_report(accounts, logged_in=True):
    return {'schema': 'decrypt.tvbridge-login.v1', 'page': {'logged_in': logged_in}, 'accounts': accounts}


def short_row(role='main'):
    return {'name': 'X', 'id': 1, 'role': role, 'positions': [{'contract': 'MNQZ1', 'root': 'MNQ', 'net': -1}],
            'working': []}


class PeerFailsClosed(unittest.TestCase):
    def hedge(self, fetch):
        return PeerHedge([PEER_B], own_token='a' * 32, environ={'TVBRIDGE_TOKEN_B': 'b' * 32}, fetch=fetch)

    def test_down_logged_out_or_unpolled_peer_refuses_and_is_reported(self):
        def down(url, timeout):
            raise urllib.error.URLError('down')

        for fetch, why in ((down, 'URLError'), (lambda u, t: login_report([short_row()], False), 'logged_out')):
            with self.subTest(why=why), self.assertLogs('tvbridge', level='WARNING'):
                h = self.hedge(fetch)
                self.assertEqual(h.conflict('long', ['MNQ']), 'hedge_peer_unknown:b:not_polled_yet')
                h.poll_once()
                self.assertEqual(h.conflict('short', ['MNQ']), 'hedge_peer_unknown:b:' + why)
                self.assertEqual(h.unknown_blocking(), ['b'])

    def test_a_known_followers_only_login_never_blocks_the_leader(self):
        state = {'report': login_report([dict(short_row('follower'), key='f1')])}

        def fetch(url, timeout):
            if state['report'] is None:
                raise urllib.error.URLError('down')
            return state['report']

        h = self.hedge(fetch)
        h.poll_once()
        state['report'] = None
        with self.assertLogs('tvbridge', level='WARNING'):
            h.poll_once()
            self.assertIsNone(h.conflict('long', ['MNQ'], LEADER_EXEMPT_ROLES))
            self.assertEqual(h.unknown_blocking(LEADER_EXEMPT_ROLES), [])
            self.assertEqual(h.conflict('long', ['MNQ']), 'hedge_peer_unknown:b:URLError')   # a follower entry
        # the login later shows a main account: the leader is held while it is down
        state['report'] = login_report([short_row('main')], logged_in=False)
        h.poll_once()   # (the miss was already warned this minute)
        self.assertEqual(h.conflict('long', ['MNQ'], LEADER_EXEMPT_ROLES), 'hedge_peer_unknown:b:logged_out')


class FiringLoginsFailClosed(unittest.TestCase):
    def reports(self, view):
        links = [types.SimpleNamespace(id='a', primary=True, view=None, report=None),
                 types.SimpleNamespace(id='b', primary=False, view=view, report=None)]
        return firing.LoginReports(links, {'f1': 'a'}, 'a', hedge_members=('MNQ', 'ES'))

    def test_unknown_login_skips_follower_entries(self):
        class Down:
            def get(self):
                raise OSError('down')

        r = self.reports(Down())
        with self.assertLogs('tvbridge.firing', 'WARNING'):
            self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_unknown_login:b:not_polled_yet')
            r.poll_once()
            self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_unknown_login:b:OSError')
        r = self.reports(None)
        with self.assertLogs('tvbridge.firing', 'WARNING'):
            self.assertEqual(r.hedge_conflict('a', 'short'), 'hedge_unknown_login:b:token_missing')

    def test_known_logins_still_classify(self):
        class Up:
            def __init__(self, rep):
                self.rep = rep

            def get(self):
                return self.rep

        r = self.reports(Up(login_report([short_row()])))
        r.poll_once()
        self.assertEqual(r.hedge_conflict('a', 'long'), 'hedge_conflict_login:b:MNQ')
        self.assertIsNone(r.hedge_conflict('a', 'short'))
        r = self.reports(Up(login_report([short_row()], logged_in=False)))
        r.poll_once()
        with self.assertLogs('tvbridge.firing', 'WARNING'):
            self.assertEqual(r.hedge_conflict('a', 'short'), 'hedge_unknown_login:b:logged_out')

    def test_lane_check_errors_fail_closed(self):
        class Broken:
            primary_id = 'a'

            def hedge_conflict(self, login_id, side):
                raise RuntimeError('boom')

        self.assertEqual(firing.LaneLogin(Broken(), 'a').hedge_conflict('long'), 'hedge_unknown_login:check_failed')


# --- the bracket reference-price check after a restart ------------------------------------------

class ReferencePriceAfterRestart(HedgeGuardBase):
    config = {'bracket_orders_enabled': True}

    def bracket(self, ref=12000.0, stop=11986.75):
        self.seq += 1
        payload = {'ticker': 'MNQZ2021', 'action': 'buy', 'orderType': 'market', 'quantity': 1,
                   'extras': {'soup_command_id': f'b-{self.seq}', 'soup_trade_id': f'test:MNQZ2021:{self.seq}:long',
                              'bridge_kind': 'bracket_entry', 'reference_price': ref, 'stop_price': stop}}
        return self.executor._parse(payload)

    def saved(self, price, age_s=0, symbol=None):
        path = Path(self.journal.dir) / MARKET_PRICE_NAME
        path.write_text(json.dumps({'symbol': symbol or self.executor.symbol, 'price': price,
                                    'at_ms': self.executor.clock_ms() - int(age_s * 1000)}))

    def refused(self, cmd):
        with self.assertRaises(Refused) as ctx:
            self.executor._guard_bracket(cmd)
        return str(ctx.exception)

    def test_no_known_price_refuses_the_bracket(self):
        self.assertIn('reference_price cannot be checked', self.refused(self.bracket()))
        outcome, receipt = self.executor.send({'ticker': 'MNQZ2021', 'action': 'buy', 'orderType': 'market',
                                               'quantity': 1, 'extras': {
                                                   'soup_command_id': 'x1', 'soup_trade_id': 'test:MNQZ2021:x:long',
                                                   'bridge_kind': 'bracket_entry', 'reference_price': 12000.0,
                                                   'stop_price': 11986.75}})
        self.assertEqual(outcome, 'rejected')
        self.assertIn('no recent fill price', receipt['reason'])
        self.assertEqual(self.page.requests, [])

    def test_saved_price_checks_the_reference_after_a_restart(self):
        self.saved(12004.5, age_s=3600)
        plan = self.executor._guard_bracket(self.bracket(ref=12000.0))
        self.assertEqual(plan['reference_price'], 12000.0)
        # the class of error under test: a garbage reference with a stop near it
        reason = self.refused(self.bracket(ref=8000.0, stop=7991.25))
        self.assertIn('from the last known fill 12004.5', reason)

    def test_old_or_foreign_saved_price_is_not_used(self):
        self.saved(12004.5, age_s=self.executor.reference_price_max_age_s + 60)
        self.assertIn('cannot be checked', self.refused(self.bracket()))
        self.saved(12004.5, symbol='ESZ1')
        self.assertIn('cannot be checked', self.refused(self.bracket()))
        (Path(self.journal.dir) / MARKET_PRICE_NAME).write_text('not json')
        self.assertIn('cannot be checked', self.refused(self.bracket()))

    def test_a_fill_in_the_sync_is_used_and_saved(self):
        self.sync(fills=[{'id': 1, 'orderId': 5, 'contractId': 2, 'price': 12045.0, 'qty': 1,
                          'timestamp': '2021-04-20T19:45:00.000Z', 'active': True}])
        self.assertIn('from the latest fill 12045.0', self.refused(self.bracket(ref=8000.0, stop=7991.25)))
        saved = json.loads((Path(self.journal.dir) / MARKET_PRICE_NAME).read_text())
        self.assertEqual((saved['symbol'], saved['price']), (self.executor.symbol, 12045.0))
        self.assertEqual(saved['at_ms'], 1618947900000)
        self.sync()                                   # restart: the new sync has no fill
        self.executor.reference_price_max_age_s = 10 ** 9
        self.assertEqual(self.executor.market_price(), (12045.0, 'last known fill'))

    def test_max_age_must_be_positive(self):
        for bad in (0, -1, 'x', None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                self.make_executor({'reference_price_max_age_s': bad})


# --- path tokens are compared as bytes ------------------------------------------------------------

class NonAsciiTokens(unittest.TestCase):
    def test_same_secret(self):
        from tvbridge.protocol import same_secret
        self.assertTrue(same_secret('a' * 32, 'a' * 32))
        self.assertFalse(same_secret('\u00e9' * 32, 'a' * 32))   # str compare_digest raised TypeError here
        self.assertFalse(same_secret(None, 'a' * 32))

    def test_non_ascii_path_is_a_plain_404(self):
        import socket
        import threading
        from http.server import ThreadingHTTPServer
        handler = daemon.make_handler(object(), object(), 't' * 32, login=None)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        for path in (b'/health/\xc3\xa9', b'/state/\xc3\xa9', b'/order/\xc3\xa9'):
            with self.subTest(path=path), socket.create_connection(httpd.server_address, timeout=5) as c:
                method = b'POST' if path.startswith(b'/order') else b'GET'
                c.sendall(method + b' ' + path + b' HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n')
                self.assertTrue(c.recv(64).startswith(b'HTTP/1.0 404'))


# --- id regexes refuse a trailing newline ------------------------------------------------------------

class IdsRefuseTrailingNewline(unittest.TestCase):
    def test_every_id_pattern(self):
        import importlib.util
        import re
        from tvbridge import firing as f, followers, logins, manual
        module_root = ROOT.parent
        spec = importlib.util.spec_from_file_location('tvb_setup_under_test', module_root / 'setup.py')
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        cases = [(logins.ID_RE, 'b'), (logins.ENV_NAME_RE, 'TVBRIDGE_TOKEN_B'), (f.LOGIN_ID_RE, 'b'),
                 (followers.KEY_RE, 'account-b'), (manual.KEY_RE, 'account-b'), (manual.CHANGE_ID_RE, 'c-1'),
                 (setup.KEY_RE, 'account-b'), (setup.TICKER_RE, 'MNQZ2021')]
        for pattern, good in cases:
            with self.subTest(pattern=pattern.pattern):
                self.assertTrue(pattern.match(good))
                self.assertIsNone(pattern.match(good + '\n'))
        # every anchored validation pattern in the module ends with \Z, never $
        for path in module_root.rglob('*.py'):
            if 'tests' in path.parts or path.name.startswith('test_') or 'fixture' in str(path):
                continue
            for line in path.read_text().splitlines():
                self.assertIsNone(re.search(r"re\.compile\(r?['\"]\^.*\$['\"]", line), f'{path}: {line}')


if __name__ == '__main__':
    unittest.main()
