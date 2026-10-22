#!/usr/bin/env python3
"""Bridge window: safety checks (retired max_contracts config check, fail-closed pause switch)."""
import json
import time
import unittest

import server
import test_server


class Panel(unittest.TestCase):
    """PanelCase's fixture without re-running its own tests."""
    P = test_server.PanelCase
    setUp, fake_bridge, verdict = P.setUp, P.fake_bridge, P.verdict


class RetiredMaxContractsConfig(Panel):
    def write_config(self, **cfg):
        server.CONFIG.write_text(json.dumps(dict({'account_name': 'DEMO-0001', 'ticker': 'MNQZ2021',
                                                  'kill_switch_file': str(server.ROOT / 'ENTRIES-DISABLED')},
                                                 **cfg)))

    def test_malformed_legacy_max_contracts_is_a_config_error(self):
        # max_contracts is retired (the strategy alert sets the size); only a malformed legacy value is refused.
        for bad in (0, -1, '3', 'x', True, 2.5):
            with self.subTest(value=bad):
                self.write_config(max_contracts=bad)
                self.units['tvbridge-daemon.service'] = {'active': 'failed', 'sub': 'failed', 'result': 'exit-code',
                                                         'since': ''}
                self.health = None
                v = self.verdict()
                self.assertEqual(v['title'], 'Bridge config error')
                self.assertEqual(v['tone'], 'coral')
                self.assertIn('max_contracts is retired', v['detail'])
                self.assertNotIn('..', v['detail'])

    def test_any_positive_legacy_value_or_none_is_not_an_error(self):
        for ok in (1, 10, 11, 40, 500):
            with self.subTest(value=ok):
                self.write_config(max_contracts=ok)
                self.assertIsNone(server.config_problem(json.loads(server.CONFIG.read_text())))
                self.assertNotEqual(self.verdict()['title'], 'Bridge config error')
        self.write_config()
        self.assertIsNone(server.config_problem(json.loads(server.CONFIG.read_text())))
        self.assertNotEqual(self.verdict()['title'], 'Bridge config error')
        self.assertIsNone(server.config_problem({'account_name': 'X', 'max_contracts': None}))
        self.assertFalse(hasattr(server, 'MAX_CONTRACTS_CAP'))

    def test_followers_only_login_is_not_checked(self):
        self.assertIsNone(server.config_problem({'mode': 'followers_only'}))


class PauseFailsClosed(Panel):
    def write_config(self, **cfg):
        server.CONFIG.write_text(json.dumps(dict({'account_name': 'DEMO-0001', 'ticker': 'MNQZ2021',
                                                  'max_contracts': 5}, **cfg)))

    def test_no_switch_configured_reads_paused(self):
        self.write_config()
        s = server.snapshot()
        self.assertTrue(s['kill_switch'])
        self.assertIn('No pause switch file is configured', s['pause']['reason'])
        self.assertEqual(s['verdict']['title'], 'Healthy · entries off')
        self.assertIn('No pause switch file is configured', s['verdict']['detail'])

    def test_broken_link_reads_paused(self):
        path = server.ROOT / 'ENTRIES-DISABLED'
        path.symlink_to(server.ROOT / 'nowhere')
        self.write_config(kill_switch_file=str(path))
        s = server.snapshot()
        self.assertTrue(s['kill_switch'])
        self.assertIn('broken link', s['pause']['reason'])

    def test_missing_folder_reads_paused(self):
        self.write_config(kill_switch_file=str(server.ROOT / 'gone' / 'ENTRIES-DISABLED'))
        self.assertTrue(server.snapshot()['kill_switch'])

    def test_daemon_reason_is_shown(self):
        self.write_config(kill_switch_file=str(server.ROOT / 'ENTRIES-DISABLED'))
        self.health = dict(self.health, entries_disabled=True, entries_disabled_reason='kill_switch_unreadable:EACCES')
        s = server.snapshot()
        self.assertTrue(s['kill_switch'])
        self.assertIn('cannot be checked (EACCES)', s['pause']['reason'])

    def test_armed_switch_stays_armed(self):
        self.write_config(kill_switch_file=str(server.ROOT / 'ENTRIES-DISABLED'))
        s = server.snapshot()
        self.assertFalse(s['kill_switch'])
        self.assertEqual(s['verdict']['title'], 'Healthy · armed')

    def test_reason_texts(self):
        self.assertEqual(server.switch_reason_text('main_kill_switch_folder_missing'),
                         "The main account's pause switch folder is missing, so entries stay paused.")
        self.assertEqual(server.switch_reason_text('master_kill_switch_on'), "The leader login's pause switch is on.")


class PeerHeld(Panel):
    def test_unknown_peer_holds_entries(self):
        server.CONFIG.write_text(json.dumps({'account_name': 'DEMO-0001', 'max_contracts': 5,
                                             'kill_switch_file': str(server.ROOT / 'ENTRIES-DISABLED')}))
        self.health = dict(self.health, peer_hedge_unknown=['b'])
        v = self.verdict()
        self.assertEqual((v['tone'], v['title']), ('amber', 'Healthy · entries held'))
        self.assertIn('Login B is not answering', v['detail'])


class CrossSiteRefused(unittest.TestCase):
    """The Bridge window answers its own page and local tools, never another site's request."""
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from unittest import mock
        p = mock.patch.object(server, 'status_body', lambda: '{"ok": true}')
        p.start()
        self.addCleanup(p.stop)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def get(self, path, **headers):
        import http.client
        c = http.client.HTTPConnection('127.0.0.1', self.httpd.server_address[1], timeout=5)
        h = {'Host': '127.0.0.1:%d' % server.PORT}
        h.update({k.replace('_', '-'): v for k, v in headers.items()})
        c.request('GET', path, headers=h)
        r = c.getresponse()
        r.read()
        return r.status, r.getheader('X-Frame-Options')

    def test_cross_site_requests_are_refused(self):
        self.assertEqual(self.get('/api/status'), (200, 'DENY'))                                    # local watcher
        self.assertEqual(self.get('/api/status', Sec_Fetch_Site='same-origin')[0], 200)            # the page
        self.assertEqual(self.get('/api/status', Origin='http://127.0.0.1:%d' % server.PORT)[0], 200)
        self.assertEqual(self.get('/api/status', Sec_Fetch_Site='cross-site')[0], 403)
        self.assertEqual(self.get('/api/status', Origin='https://evil.example')[0], 403)
        self.assertEqual(self.get('/bridge', Origin='null')[0], 403)


if __name__ == '__main__':
    unittest.main()


class PriceFeedLine(Panel):
    """The 1-second MES feed line (runtime status.json `price_feed`). Hidden unless a feed is in use."""
    NOW = 1_633_596_800.0

    def soup(self, feed, age_s=1):
        return {'phase': 'idle', 'updated_at': int((self.NOW - age_s) * 1000), 'price_feed': feed}

    def feed(self, **kw):
        return dict({'feed': '1s', 'symbol': 'MESZ2021', 'fresh': True, 'reason': None,
                     'message': 'The 1-second MES feed is live and fresh.', 'bridge_message': None,
                     'last_bar_at': 1, 'queued': 3, 'refused_by_runtime': 0}, **kw)

    def view(self, feed, age_s=1):
        return server.price_feed_view(self.soup(feed, age_s), self.NOW)

    def test_no_feed_means_no_line(self):
        for soup in (None, {}, {'phase': 'idle'}, {'price_feed': None}, {'price_feed': 'live'}, self.soup([])):
            self.assertIsNone(server.price_feed_view(soup, self.NOW))

    def test_live_is_quiet(self):
        v = self.view(self.feed())
        self.assertEqual((v['state'], v['tone'], v['text'], v['symbol']), ('live', '', 'live', 'MESZ2021'))
        self.assertEqual(v['label'], '1-second MES feed')

    def test_stale_names_the_consequence(self):
        for reason in ('feed_not_fresh:stale', 'feed_not_fresh:no_recent_trade', 'feed_stale', 'feed_unavailable',
                       'feed_not_fresh', None, 42):
            v = self.view(self.feed(fresh=False, reason=reason, message="The bridge's 1-second MES feed is not live and fresh.",
                                    bridge_message='No quote for 4 s.'))
            self.assertEqual((v['state'], v['tone'], v['text']), ('stale', 'amber', 'stale — Lumi won’t enter'), reason)
            self.assertEqual(v['detail'], "The bridge's 1-second MES feed is not live and fresh. No quote for 4 s.")

    def test_wrong_contract_is_stopped(self):
        for reason in ('feed_wrong_contract', 'feed_not_fresh:halted_wrong_contract'):
            v = self.view(self.feed(fresh=False, reason=reason))
            self.assertEqual((v['state'], v['tone'], v['text']), ('stopped', 'coral', 'stopped — wrong contract, restart the bridge'), reason)

    def test_not_configured_is_stopped(self):
        for reason in ('feed_not_configured', 'feed_not_fresh:not_configured'):
            v = self.view(self.feed(fresh=False, reason=reason))
            self.assertEqual((v['state'], v['tone']), ('stopped', 'coral'), reason)
            self.assertIn('not set up in the bridge', v['text'])

    def test_old_engine_status_never_reads_live(self):
        v = self.view(self.feed(), age_s=120)
        self.assertEqual((v['state'], v['tone']), ('unknown', ''))
        self.assertEqual(v['text'], 'no recent reading (engine status 2 min ago)')
        v = server.price_feed_view({'price_feed': self.feed()}, self.NOW)
        self.assertEqual(v['text'], 'no recent reading (engine status no time recorded)')

    def test_fresh_wins_over_a_leftover_reason(self):
        v = self.view(self.feed(fresh=True, reason='feed_stale'))
        self.assertEqual(v['state'], 'live')

    def test_snapshot_carries_the_line_and_the_page_hides_it_without_one(self):
        soup = dict(self.soup(self.feed(fresh=False, reason='feed_wrong_contract')), updated_at=int(time.time() * 1000))
        server.SOUP.write_text(json.dumps(soup))
        s = server.snapshot()
        json.dumps(s)
        self.assertEqual(s['soup']['price_feed']['reason'], 'feed_wrong_contract')
        self.assertEqual(s['price_feed']['state'], 'stopped')
        server.SOUP.write_text(json.dumps({'phase': 'idle', 'updated_at': 1}))
        self.assertIsNone(server.snapshot()['price_feed'])
        self.assertIn('id="tr-feed" hidden', server.PAGE)
        self.assertIn("fl.hidden=!pf", server.PAGE)
