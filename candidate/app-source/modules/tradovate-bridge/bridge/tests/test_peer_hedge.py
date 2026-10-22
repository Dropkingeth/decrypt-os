"""1.1.0 multi-login: the cross-login anti-hedge check (`peer_logins`, tvbridge.peers).

In-memory doubles and loopback HTTP only: no browser. A background poll keeps each peer login's
GET /login report; before an entry, the executor's hedge guard reads that cache only (no network
under the executor lock). Opposite exposure in the hedge group on a counted peer account (role
main/follower with the default `peer_hedge_scope` "configured"; every account with "all") refuses
with `hedge_conflict_peer:<id>:<root>` (no account names). A peer that is unreachable, slow (over
1.0 s), logged out, answers something else or whose report is older than 10 s fails open for that
peer, warns at most once a minute and shows `peer_unreachable:<id>` in /health notes. Rows with an
unknown root are skipped and noted as `peer_contract_unknown:<id>`.
"""
import json
import logging
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import harness  # noqa: F401  (puts the repository on sys.path)

from tvbridge import peers as peers_mod
from tvbridge.daemon import LoginView, make_followers_only_handler
from tvbridge.logins import daemon_settings
from tvbridge.peers import LEADER_EXEMPT_ROLES, PeerHedge, account_conflict
from test_fill_capture import Exchange
from test_hedge_guard import HedgeGuardBase, pos
from tvbridge.state import AccountState

MEMBERS = {'ES', 'MES', 'NQ', 'MNQ', 'YM', 'MYM', 'RTY', 'M2K'}
TOKEN_A = 'peer-hedge-token-login-a-0123456789'
TOKEN_B = 'peer-hedge-token-login-b-0123456789'
PEER_B = {'id': 'b', 'url': 'http://127.0.0.1:1', 'token_env': 'TVBRIDGE_TOKEN_B'}


def report(*accounts, logged_in=True):
    return {'schema': 'decrypt.tvbridge-login.v1', 'login_id': 'b', 'label': 'Login B', 'mode': 'followers_only',
            'page': {'state': 'authorized', 'synced': logged_in, 'logged_in': logged_in, 'problems': []},
            'observed_at': 1, 'accounts': list(accounts)}


def account(name, positions=(), working=(), role='follower', key=None, aid=1):
    return {'name': name, 'id': aid, 'configured': role is not None, 'role': role, 'key': key,
            'positions': list(positions), 'working': list(working)}


def p(net, contract='MNQZ1', root='MNQ'):
    return {'contract': contract, 'root': root, 'net': net}


def w(side, qty=1, contract='MNQZ1', root='MNQ'):
    return {'contract': contract, 'root': root, 'side': side, 'qty': qty}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class AccountConflict(unittest.TestCase):
    def conflict(self, acct, side='long', exempt=frozenset(), scope='configured'):
        return account_conflict(report(acct), side, MEMBERS, exempt, scope)

    def test_opposite_position_in_group_conflicts(self):
        self.assertEqual(self.conflict(account('B1', [p(-1)])), 'MNQ')
        self.assertEqual(self.conflict(account('B1', [p(-2, 'ESZ1', 'ES')])), 'ES')
        self.assertEqual(self.conflict(account('B1', [p(3)]), side='short'), 'MNQ')

    def test_scope_configured_counts_only_main_and_follower_roles(self):
        unassigned = account('DEMO-X', [p(-1)], role=None)
        self.assertIsNone(self.conflict(unassigned))
        self.assertEqual(self.conflict(unassigned, scope='all'), 'MNQ')
        self.assertEqual(self.conflict(account('M', [p(-1)], role='main')), 'MNQ')
        self.assertEqual(self.conflict(account('F', [p(-1)], role='follower')), 'MNQ')

    def test_same_side_flat_or_other_group_is_clear(self):
        for acct in (account('B1', [p(2)]), account('B1', [p(0)]), account('B1', [p(-1, 'CLZ1', 'CL')]),
                     account('B1')):
            with self.subTest(acct=acct):
                self.assertIsNone(self.conflict(acct))

    def test_working_orders(self):
        self.assertEqual(self.conflict(account('B1', working=[w('Sell')])), 'MNQ')     # could open a short
        self.assertIsNone(self.conflict(account('B1', working=[w('Buy')])))            # same side
        self.assertIsNone(self.conflict(account('B1', [p(2)], [w('Sell', 2)])))        # only reduces a long
        self.assertEqual(self.conflict(account('B1', [p(2)], [w('Sell', 3)])), 'MNQ')  # would reverse through flat
        self.assertEqual(self.conflict(account('B1', [p(2)], [w('Sell', None)])), 'MNQ')  # unknown size
        self.assertEqual(self.conflict(account('B1', working=[w(None)])), 'MNQ')       # unknown side
        self.assertIsNone(self.conflict(account('B1', working=[w('Sell', 1, 'CLZ1', 'CL')])))

    def test_unknown_root_counts_as_a_group_member_and_is_reported(self):
        # Fail closed, as the local guard: opposite exposure on an unknown contract refuses
        for acct in (account('B1', [p(-1, None, None)]), account('B1', working=[w('Sell', 1, None, None)])):
            with self.subTest(acct=acct):
                self.assertEqual(self.conflict(acct), 'unknown')
                self.assertIsNone(self.conflict(acct, side='short'))
                self.assertTrue(peers_mod.unknown_contracts(report(acct)))
        self.assertFalse(peers_mod.unknown_contracts(report(account('B1', [p(0, None, None)]))))  # flat
        self.assertFalse(peers_mod.unknown_contracts(report(account('X', [p(-1, None, None)], role=None))))
        self.assertTrue(peers_mod.unknown_contracts(report(account('X', [p(-1, None, None)], role=None)), 'all'))

    def test_leader_exempts_peer_followers_only(self):
        f = account('F1', [p(-1)], role='follower', key='f1')
        m = account('M1', [p(-1)], role='main')
        self.assertIsNone(self.conflict(f, exempt=LEADER_EXEMPT_ROLES))
        self.assertEqual(self.conflict(m, exempt=LEADER_EXEMPT_ROLES), 'MNQ')
        self.assertEqual(self.conflict(f), 'MNQ')  # a follower's entry checks every counted peer account


class FailClosed(unittest.TestCase):
    """A peer whose exposure is unknown refuses entries (it used to fail open)."""
    def hedge(self, fetch, clock=None, peers=(PEER_B,), env=None, **kw):
        return PeerHedge(list(peers), own_token=TOKEN_A, environ={'TVBRIDGE_TOKEN_B': TOKEN_B} if env is None else env,
                         fetch=fetch, clock=clock or time.monotonic, **kw)

    def test_conflict_reason_names_peer_and_root_never_the_account(self):
        seen = []

        def fetch(url, timeout):
            seen.append((url, timeout))
            return report(account('DEMO-1', [p(-1)]))

        h = self.hedge(fetch)
        h.poll_once()
        self.assertEqual(h.conflict('long', MEMBERS), 'hedge_conflict_peer:b:MNQ')
        self.assertIsNone(h.conflict('short', MEMBERS))
        self.assertEqual(seen, [('http://127.0.0.1:1/login/' + TOKEN_B, 1.0)])
        self.assertEqual(h.notes(), [])

    def test_conflict_reads_the_cache_only(self):
        calls = []
        h = self.hedge(lambda u, t: calls.append(u) or report(account('X', [p(-1)])))
        with self.assertLogs('tvbridge', level='WARNING'):
            # never polled: fail closed, no network
            self.assertEqual(h.conflict('long', MEMBERS), 'hedge_peer_unknown:b:not_polled_yet')
        self.assertEqual((calls, h.notes()), ([], ['peer_unreachable:b']))
        h.poll_once()
        for _ in range(3):
            self.assertEqual(h.conflict('long', MEMBERS), 'hedge_conflict_peer:b:MNQ')
        self.assertEqual(len(calls), 1)

    def test_stale_cache_fails_closed_with_a_note(self):
        clock = Clock()
        h = self.hedge(lambda u, t: report(account('X', [p(-1)])), clock)
        h.poll_once()
        clock.t += 9.9
        self.assertEqual(h.conflict('long', MEMBERS), 'hedge_conflict_peer:b:MNQ')
        clock.t += 0.2
        with self.assertLogs('tvbridge', level='WARNING') as logs:
            self.assertEqual(h.conflict('long', MEMBERS), 'hedge_peer_unknown:b:stale')
        self.assertIn('(stale)', logs.output[0])
        self.assertIn('fail closed', logs.output[0])
        self.assertEqual(h.notes(), ['peer_unreachable:b'])

    def test_scope_all_counts_unassigned_accounts(self):
        unassigned = report(account('DEMO-X', [p(-1)], role=None))
        h = self.hedge(lambda u, t: unassigned)
        h.poll_once()
        self.assertIsNone(h.conflict('long', MEMBERS))
        h = self.hedge(lambda u, t: unassigned, scope='all')
        h.poll_once()
        self.assertEqual(h.conflict('long', MEMBERS), 'hedge_conflict_peer:b:MNQ')
        with self.assertRaises(ValueError):
            self.hedge(lambda u, t: unassigned, scope='some')

    def test_opposite_row_on_an_unknown_contract_refuses_with_a_note(self):
        h = self.hedge(lambda u, t: report(account('B1', [p(-1, None, None)])))
        with self.assertLogs('tvbridge', level='WARNING') as logs:
            h.poll_once()
        self.assertTrue(any('unknown contract' in line for line in logs.output))
        self.assertEqual(h.conflict('long', MEMBERS), 'hedge_conflict_peer:b:unknown')
        self.assertIsNone(h.conflict('short', MEMBERS))   # same side: never a conflict
        self.assertEqual(h.notes(), ['peer_contract_unknown:b'])

    def test_malformed_report_fails_closed(self):
        h = self.hedge(lambda u, t: report({'name': 'X', 'id': 1, 'role': 'main', 'positions': 5}))
        h.poll_once()
        with self.assertLogs('tvbridge', level='WARNING'):
            self.assertEqual(h.conflict('long', MEMBERS), 'hedge_peer_unknown:b:bad_report')

    def test_every_miss_fails_closed_with_a_health_note(self):
        def raises(exc):
            def fetch(url, timeout):
                raise exc
            return fetch

        cases = {
            'URLError': raises(urllib.error.URLError('refused')),
            'http_404': raises(urllib.error.HTTPError('u', 404, 'nf', {}, None)),
            'TimeoutError': raises(TimeoutError()),
            'logged_out': lambda u, t: report(account('X', [p(-1)]), logged_in=False),
            'not_a_login_report': lambda u, t: {'health': {}},
        }
        for why, fetch in cases.items():
            with self.subTest(why=why):
                h = self.hedge(fetch)
                with self.assertLogs('tvbridge', level='WARNING') as logs:
                    h.poll_once()
                    self.assertEqual(h.conflict('long', MEMBERS), 'hedge_peer_unknown:b:' + why)
                    self.assertEqual(h.conflict('short', MEMBERS), 'hedge_peer_unknown:b:' + why)
                self.assertEqual(h.notes(), ['peer_unreachable:b'])
                self.assertTrue(any('peer login b unreachable' in line for line in logs.output))
                self.assertFalse(any(TOKEN_B in line for line in logs.output))

    def test_missing_or_own_token_fails_closed(self):
        for env in ({}, {'TVBRIDGE_TOKEN_B': 'short'}, {'TVBRIDGE_TOKEN_B': TOKEN_A}):
            with self.subTest(env=env):
                calls = []
                with self.assertLogs('tvbridge', level='ERROR'):
                    h = self.hedge(lambda u, t: calls.append(u), env=env)
                with self.assertLogs('tvbridge', level='WARNING'):
                    h.poll_once()
                    self.assertTrue(h.conflict('long', MEMBERS).startswith('hedge_peer_unknown:b:token_'))
                self.assertEqual((calls, h.notes()), ([], ['peer_unreachable:b']))

    def test_warning_at_most_once_a_minute_per_peer_and_note_clears_on_recovery(self):
        clock = Clock()
        state = {'up': False}

        def fetch(url, timeout):
            if not state['up']:
                raise urllib.error.URLError('down')
            return report()

        h = self.hedge(fetch, clock)
        log = logging.getLogger('tvbridge')
        with self.assertLogs('tvbridge', level='WARNING') as logs:
            for _ in range(5):
                h.poll_once()
                h.conflict('long', MEMBERS)
                clock.t += 10
            log.warning('marker')
        self.assertEqual(sum('unreachable' in line for line in logs.output), 1)
        clock.t += 60
        with self.assertLogs('tvbridge', level='WARNING') as logs:
            h.poll_once()
            h.conflict('long', MEMBERS)
        self.assertEqual(sum('unreachable' in line for line in logs.output), 1)
        state['up'] = True
        h.poll_once()
        self.assertEqual(h.notes(), [])

    def test_one_peer_down_does_not_hide_another_peers_conflict(self):
        def fetch(url, timeout):
            if ':1/' in url:
                raise urllib.error.URLError('down')
            return report(account('C1', [p(-1)]))

        peer_c = {'id': 'c', 'url': 'http://127.0.0.1:2', 'token_env': 'TVBRIDGE_TOKEN_C'}
        h = PeerHedge([PEER_B, peer_c], own_token=TOKEN_A, fetch=fetch,
                      environ={'TVBRIDGE_TOKEN_B': TOKEN_B, 'TVBRIDGE_TOKEN_C': 'c' * 30})
        with self.assertLogs('tvbridge', level='WARNING'):
            h.poll_once()
            self.assertEqual(h.conflict('long', MEMBERS), 'hedge_conflict_peer:c:MNQ')
        self.assertEqual(h.notes(), ['peer_unreachable:b'])


class SlowPeer(unittest.TestCase):
    def test_slow_peer_is_bounded_to_one_second_and_fails_closed(self):
        release = threading.Event()

        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                release.wait(5)
                body = json.dumps(report(account('X', [p(-1)]))).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        peer = dict(PEER_B, url='http://127.0.0.1:%d' % server.server_address[1])
        h = PeerHedge([peer], own_token=TOKEN_A, environ={'TVBRIDGE_TOKEN_B': TOKEN_B})
        start = time.monotonic()
        with self.assertLogs('tvbridge', level='WARNING'):
            h.poll_once()
            self.assertTrue(h.conflict('long', MEMBERS).startswith('hedge_peer_unknown:b:'))
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(h.notes(), ['peer_unreachable:b'])

    def test_timeout_is_never_above_one_second(self):
        self.assertEqual(PeerHedge([], timeout_s=5).timeout_s, 1.0)


class ExecutorPeerStep(HedgeGuardBase):
    """The real Executor: the peer step runs after the local guard, inside the entry preconditions."""

    def wire(self, rep, roles=frozenset()):
        self.fetches = []

        def fetch(url, timeout):
            self.fetches.append(url)
            if isinstance(rep, Exception):
                raise rep
            return rep

        self.executor.peer_hedge = PeerHedge([PEER_B], own_token=TOKEN_A, fetch=fetch,
                                             environ={'TVBRIDGE_TOKEN_B': TOKEN_B})
        self.executor.peer_exempt_roles = roles
        # The background poll (PeerHedge.start) fills the cache; the entry path only reads it.
        if isinstance(rep, Exception):
            with self.assertLogs('tvbridge', level='WARNING'):
                self.executor.peer_hedge.poll_once()
        else:
            self.executor.peer_hedge.poll_once()
        self.fetches.clear()

    def test_peer_conflict_refuses_and_sends_nothing(self):
        self.wire(report(account('B-ACCT', [p(-1)])))
        outcome, receipt = self.entry('long')
        self.assertEqual((outcome, receipt['reason']), ('rejected', 'hedge_conflict_peer:b:MNQ'))
        self.assertEqual(self.fetches, [], 'the entry path reads the poll cache, never the network')
        self.assertEqual(self.page.placed(), [])
        self.assert_accepted(self.entry('short'))

    def test_peer_down_fails_closed(self):
        self.wire(urllib.error.URLError('down'))  # warned once by the poll
        outcome, receipt = self.entry('long')
        self.assertEqual((outcome, receipt['reason']), ('rejected', 'hedge_peer_unknown:b:URLError'))
        self.assertEqual(self.page.placed(), [])
        self.assertEqual(self.executor.peer_hedge.notes(), ['peer_unreachable:b'])

    def test_leader_is_not_blocked_by_a_down_login_that_holds_only_its_followers(self):
        state = {'up': True}

        def fetch(url, timeout):
            if not state['up']:
                raise urllib.error.URLError('down')
            return report(account('B-F', [], role='follower', key='f1'))

        self.executor.peer_hedge = PeerHedge([PEER_B], own_token=TOKEN_A, fetch=fetch,
                                             environ={'TVBRIDGE_TOKEN_B': TOKEN_B})
        self.executor.peer_exempt_roles = LEADER_EXEMPT_ROLES
        with self.assertLogs('tvbridge', level='WARNING'):
            self.executor.peer_hedge.poll_once()        # B seen once: followers only
            state['up'] = False
            self.executor.peer_hedge.poll_once()        # then B goes down
            self.assert_accepted(self.entry('long'))   # the leader rule: its followers never block it
            # a follower entry checks every account (no exemption): refused
            self.assertEqual(self.executor.peer_hedge.conflict('long', self.executor.hedge_members),
                             'hedge_peer_unknown:b:URLError')

    def test_leader_is_blocked_by_a_down_login_never_seen(self):
        self.wire(urllib.error.URLError('down'), LEADER_EXEMPT_ROLES)
        self.assertEqual(self.entry('long')[1]['reason'], 'hedge_peer_unknown:b:URLError')

    def test_local_conflict_is_reported_first(self):
        self.wire(report(account('B-ACCT', [p(-1)])))
        self.sync(positions=[pos(10, 7001, 2, -2)])
        outcome, receipt = self.entry('long')
        self.assertEqual(outcome, 'rejected')
        self.assertFalse(receipt['reason'].startswith('hedge_conflict_peer:'))

    def test_unassigned_peer_account_never_blocks_by_default(self):
        self.wire(report(account('DEMO-OTHER', [p(-1)], role=None)))
        self.assert_accepted(self.entry('long'))

    def test_leader_skips_peer_followers(self):
        self.wire(report(account('B-F', [p(-1)], role='follower', key='f1')), LEADER_EXEMPT_ROLES)
        self.assert_accepted(self.entry('long'))

    def test_exits_never_ask_peers(self):
        self.wire(report(account('B-ACCT', [p(-1)])))
        self.assert_accepted(self.entry('short'))
        self.sync(positions=[pos(10, 1, 2, -1)])
        self.fetches.clear()
        outcome, _ = self.executor.send(self.payload('exit', 'short', 'market', cancel=False))
        self.assertEqual(self.fetches, [])


class HedgeGuardOff(HedgeGuardBase):
    config = {'hedge_guard': False, 'hedge_guard_off_acknowledged': True}

    def test_hedge_guard_false_disables_the_peer_step(self):
        calls = []
        self.executor.peer_hedge = PeerHedge([PEER_B], own_token=TOKEN_A, fetch=lambda u, t: calls.append(u),
                                             environ={'TVBRIDGE_TOKEN_B': TOKEN_B})
        self.assert_accepted(self.entry('long'))
        self.assertEqual(calls, [])


class OverHttp(HedgeGuardBase):
    """Login A's executor asks login B's real /login route (followers_only handler) over loopback."""

    def setUp(self):
        super().setUp()
        self.state_b = AccountState()
        self.ex_b = Exchange(self.state_b)  # login B: accounts 7, 8, 9 (MAIN-ACCT, F-B, F-C)
        settings = daemon_settings({'login_id': 'b', 'mode': 'followers_only'})
        # F-B is a configured follower on login B; MAIN-ACCT and F-C are only visible there.
        view = LoginView(settings, self.ex_b, self.state_b,
                         {'ticker': 'MNQZ2021', 'followers': [{'key': 'fb', 'account_name': 'F-B'}]})
        view.observe_page(self.ex_b.status())  # what login B's heartbeat and /health keep current
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_followers_only_handler(view, TOKEN_B))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        peer = dict(PEER_B, url='http://127.0.0.1:%d' % server.server_address[1])
        self.executor.peer_hedge = PeerHedge([peer], own_token=TOKEN_A, environ={'TVBRIDGE_TOKEN_B': TOKEN_B})

    def test_short_on_peer_refuses_long_here(self):
        oid = self.ex_b.order(8, 'Sell', 1, 'Market')
        self.ex_b.fill_market(oid)
        self.executor.peer_hedge.poll_once()
        outcome, receipt = self.entry('long')
        self.assertEqual((outcome, receipt['reason']), ('rejected', 'hedge_conflict_peer:b:MNQ'))
        self.assertEqual(self.page.placed(), [])
        self.assertEqual(self.executor.peer_hedge.notes(), [])
        self.assert_accepted(self.entry('short'))

    def test_unassigned_account_on_peer_counts_only_with_scope_all(self):
        oid = self.ex_b.order(9, 'Sell', 1, 'Market')  # F-C: visible on login B, not assigned there
        self.ex_b.fill_market(oid)
        hedge = self.executor.peer_hedge
        hedge.poll_once()
        self.assertIsNone(hedge.conflict('long', MEMBERS))
        hedge.scope = 'all'
        hedge.poll_once()
        self.assertEqual(hedge.conflict('long', MEMBERS), 'hedge_conflict_peer:b:MNQ')
        hedge.scope = 'configured'
        self.assert_accepted(self.entry('long'))

    def test_wrong_token_is_a_miss_not_a_pass_silently(self):
        self.executor.peer_hedge.peers[0]['token'] = 'x' * 32
        with self.assertLogs('tvbridge', level='WARNING') as logs:
            self.executor.peer_hedge.poll_once()
            self.assertEqual(self.entry('long')[1]['reason'], 'hedge_peer_unknown:b:http_404')
        self.assertTrue(any('http_404' in line for line in logs.output))
        self.assertEqual(self.executor.peer_hedge.notes(), ['peer_unreachable:b'])


class PollThread(unittest.TestCase):
    def test_poll_keeps_notes_current_and_stops(self):
        h = PeerHedge([PEER_B], own_token=TOKEN_A, environ={'TVBRIDGE_TOKEN_B': TOKEN_B},
                      fetch=lambda u, t: (_ for _ in ()).throw(urllib.error.URLError('x')), poll_s=0.05)
        with self.assertLogs('tvbridge', level='WARNING'):
            h.start()
            deadline = time.monotonic() + 3
            while 'b' not in h.cache and time.monotonic() < deadline:
                time.sleep(0.02)
        h.stop()
        self.assertEqual(h.notes(), ['peer_unreachable:b'])
        self.assertFalse(h.thread.is_alive())

    def test_no_peers_no_thread(self):
        h = PeerHedge([]).start()
        self.assertIsNone(h.thread)
        self.assertIsNone(h.conflict('long', MEMBERS))
        self.assertEqual(peers_mod.PEER_TIMEOUT_S, 1.0)


if __name__ == '__main__':
    unittest.main()
