"""The 1-second feed (tvbridge.marketfeed, page/mdfeed.js), offline with a fake feed. Never a real broker.

Cases: fresh, stale, gap, wrong contract, restart, plus off-tick prices, reconnects, the daemon's start refusal for
Lumi without the feed, GET /feed and the page script (run under node with a fake page when node is installed).
"""
import json
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness  # noqa: F401  (puts the repository on sys.path; no browser is started here)
from tvbridge import marketfeed
from tvbridge.bridge import Bridge
from tvbridge.daemon import make_handler
from tvbridge.state import AccountState

CID = 3000123
T0 = 1633528800000          # a whole second
TS = '2021-10-06T14:00:00.000Z'


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def feed(cid=CID, at=T0):
    clock = Clock(at)
    f = marketfeed.SecondFeed('MESZ2021', contract_id=lambda: cid, tick_size=0.25, clock_ms=clock)
    f.on_state('subscribed')
    return f, clock


def iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (ms % 1000)


def quote(f, clock, price=5000.0, cid=CID, at=None, broker_lag=50):
    """One broker quote, arriving at `at` on this clock; its broker time is `broker_lag` ms earlier."""
    if at is not None:
        clock.at = at
    f.on_page_event({'type': 'md_quotes', 'data': {'quotes': [{'contractId': cid, 'timestamp': iso(clock.at - broker_lag),
                                                               'price': price}]}})


def run(f, clock, seconds, price=5000.0, start=None):
    """One quote in each of `seconds` consecutive seconds (mid-second), starting at `start`."""
    start = clock.at if start is None else start
    for i in range(seconds):
        quote(f, clock, price, at=start + i * 1000 + 300)


class Fresh(unittest.TestCase):

    def test_three_live_seconds_are_fresh(self):
        f, clock = feed()
        run(f, clock, 3)
        clock.at = T0 + 3050
        snap = f.snapshot()
        self.assertTrue(snap['fresh'], snap)
        self.assertIsNone(snap['reason'])
        self.assertEqual(snap['message'], 'The 1-second feed is live and fresh.')
        self.assertEqual([b['close_time'] for b in snap['bars']], [T0 + 1000, T0 + 2000, T0 + 3000])
        self.assertEqual(snap['symbol'], 'MESZ2021')
        self.assertEqual(snap['contract_id'], CID)

    def test_two_seconds_are_still_warming_up(self):
        f, clock = feed()
        run(f, clock, 2)
        clock.at = T0 + 2050
        snap = f.snapshot()
        self.assertFalse(snap['fresh'])
        self.assertEqual(snap['reason'], 'warming_up')
        self.assertEqual(snap['bars'], [])           # bars are served only when fresh

    def test_bars_are_ohlc_of_trade_prices_and_quiet_seconds_carry_the_last_trade(self):
        f, clock = feed()
        for at, price in ((T0 + 100, 5000.0), (T0 + 400, 5001.25), (T0 + 700, 4999.5), (T0 + 900, 5000.5)):
            quote(f, clock, price, at=at)
        quote(f, clock, None, at=T0 + 1500)          # a bid/ask-only quote: alive, no trade
        quote(f, clock, 5000.75, at=T0 + 2500)
        clock.at = T0 + 3010
        bars = list(f.bars)
        self.assertEqual((bars[0]['o'], bars[0]['h'], bars[0]['l'], bars[0]['c'], bars[0]['trades']),
                         (5000.0, 5001.25, 4999.5, 5000.5, 4))
        self.assertEqual((bars[1]['o'], bars[1]['c'], bars[1]['trades']), (5000.5, 5000.5, 0))
        self.assertTrue(f.snapshot()['fresh'])


class Stale(unittest.TestCase):

    def test_no_quote_for_more_than_two_seconds_is_stale(self):
        f, clock = feed()
        run(f, clock, 4)
        clock.at = T0 + 3300 + 2001
        snap = f.snapshot()
        self.assertFalse(snap['fresh'])
        self.assertEqual(snap['reason'], 'stale')
        self.assertIn('stale', snap['message'])
        self.assertFalse(snap['live'])

    def test_not_subscribed_is_not_fresh_and_its_quotes_are_ignored(self):
        f, clock = feed()
        run(f, clock, 4)
        f.on_page_event({'type': 'md_state', 'data': {'state': 'closed', 'reason': 'socket_closed'}})
        quote(f, clock, at=T0 + 4300)
        clock.at = T0 + 4400
        snap = f.snapshot()
        self.assertEqual(snap['reason'], 'md_not_connected:closed')
        self.assertEqual(snap['rejected'].get('not_subscribed'), 1)

    def test_a_refused_market_data_login_says_so(self):
        f, clock = feed()
        f.on_state('auth_failed', 'authorization_refused')
        self.assertEqual(f.snapshot()['reason'], 'md_auth_failed')


class Gap(unittest.TestCase):

    def test_a_second_without_any_quote_restarts_the_warm_up(self):
        f, clock = feed()
        run(f, clock, 4)                                       # seconds 0..3
        quote(f, clock, at=T0 + 5300)                          # second 4 had nothing
        clock.at = T0 + 6050
        snap = f.snapshot()
        self.assertEqual(snap['reason'], 'warming_up')
        self.assertEqual(snap['gaps'], 1)
        run(f, clock, 2, start=T0 + 6000)
        clock.at = T0 + 8050
        self.assertTrue(f.snapshot()['fresh'])

    def test_a_gap_seen_only_by_the_reader_is_a_gap(self):
        f, clock = feed()
        run(f, clock, 4)
        clock.at = T0 + 5100                                   # second 4 passed with no quote, not yet stale
        snap = f.snapshot()
        self.assertFalse(snap['fresh'])
        self.assertEqual(snap['reason'], 'warming_up')

    def test_a_reconnect_restarts_the_warm_up(self):
        f, clock = feed()
        run(f, clock, 4)
        f.on_state('closed', 'receive_timeout')
        f.on_state('connecting')
        f.on_state('authorized')
        f.on_state('subscribed')
        run(f, clock, 1, start=T0 + 4000)
        clock.at = T0 + 5050
        self.assertEqual(f.snapshot()['reason'], 'warming_up')


class WrongContract(unittest.TestCase):

    def test_a_quote_for_another_contract_stops_the_feed_until_restart(self):
        f, clock = feed()
        run(f, clock, 4)
        quote(f, clock, cid=CID + 1, at=T0 + 4300)
        run(f, clock, 5, start=T0 + 4000)                      # good quotes afterwards change nothing
        f.on_state('closed')
        f.on_state('subscribed')                               # nor does a resubscribe
        clock.at = T0 + 9050
        snap = f.snapshot()
        self.assertFalse(snap['fresh'])
        self.assertEqual(snap['reason'], 'halted_wrong_contract')
        self.assertIn('different contract', snap['message'])
        self.assertEqual(snap['bars'], [])

    def test_quotes_before_the_contract_is_pinned_are_ignored(self):
        clock = Clock(T0)
        f = marketfeed.SecondFeed('MESZ2021', contract_id=lambda: None, tick_size=0.25, clock_ms=clock)
        f.on_state('subscribed')
        run(f, clock, 4)
        snap = f.snapshot()
        self.assertEqual(snap['reason'], 'contract_unknown')
        self.assertEqual(snap['rejected']['contract_unknown'], 4)

    def test_off_tick_or_bad_prices_and_malformed_events_are_dropped(self):
        f, clock = feed()
        for bad in (5000.1, -1.0, float('nan'), True, '5000'):
            quote(f, clock, bad, at=T0 + 100)
        f.on_page_event({'type': 'md_quotes', 'data': {'quotes': [{'contractId': '3000123', 'timestamp': TS,
                                                                   'price': 5000.0}]}})
        f.on_page_event({'type': 'something_else'})
        f.on_page_event(None)
        self.assertIsNone(f.last_quote_at)
        self.assertEqual(f.rejected['invalid_price'], 5)
        self.assertEqual(f.rejected['malformed_quote'], 1)
        self.assertEqual(f.rejected['malformed_event'], 2)

    def test_a_quote_time_far_ahead_of_this_clock_is_refused(self):
        f, clock = feed()
        quote(f, clock, broker_lag=-60_000)                                        # TS is a minute ahead of this clock
        self.assertEqual(f.rejected['clock_skew'], 1)

    def test_clock_skew_tolerance_is_one_second(self):
        f, clock = feed()
        quote(f, clock, at=T0 + 100, broker_lag=-900)          # broker clock 0.9 s ahead: accepted
        quote(f, clock, at=T0 + 200, broker_lag=-1500)         # 1.5 s ahead: refused
        self.assertEqual(f.rejected['clock_skew'], 1)
        self.assertEqual(f.last_quote_at, T0 + 100)


class LateAndCarried(unittest.TestCase):
    """Review fixes: freshness also checks the broker's quote time; an old price is never carried forward."""

    def test_a_late_backlog_does_not_count_as_live(self):
        f, clock = feed()
        for i in range(4):                                     # a steady stream, every quote 4 s late
            quote(f, clock, at=T0 + 300 + i * 1000, broker_lag=4000)
        clock.at = T0 + 3400
        snap = f.snapshot()
        self.assertFalse(snap['fresh'])                        # by arrival time alone it would look live
        self.assertEqual(snap['reason'], 'no_data')
        self.assertEqual(snap['rejected']['late_quote'], 4)

    def test_an_old_broker_time_makes_the_feed_stale(self):
        f, clock = feed()
        run(f, clock, 3)
        quote(f, clock, at=T0 + 3300, broker_lag=2900)         # accepted (within 2 s + 1 s tolerance)
        clock.at = T0 + 3600                                   # receive time fresh, broker time 3.2 s old
        snap = f.snapshot()
        self.assertFalse(snap['fresh'])
        self.assertEqual(snap['reason'], 'stale')

    def test_quotes_without_trades_never_carry_an_old_price_as_fresh(self):
        f, clock = feed()
        quote(f, clock, 5000.0, at=T0 + 300)                   # the last trade
        for i in range(1, 6):
            quote(f, clock, None, at=T0 + i * 1000 + 300)       # bid/ask only, every second
        clock.at = T0 + 6050
        snap = f.snapshot()
        self.assertFalse(snap['fresh'])
        self.assertEqual(snap['reason'], 'no_recent_trade')
        self.assertEqual([b['close_time'] for b in f.bars], [T0 + 1000, T0 + 2000])   # only inside the 2 s window
        quote(f, clock, 5001.0, at=T0 + 6300)                  # trades again: warm-up from here
        run(f, clock, 2, price=5001.0, start=T0 + 7000)
        clock.at = T0 + 9050
        self.assertTrue(f.snapshot()['fresh'])

    def test_a_long_reason_is_not_logged_or_kept(self):
        f, clock = feed()
        with self.assertLogs('tvbridge', level='INFO') as logs:
            f.on_state('closed', 'x' * 500)
        self.assertNotIn('x' * 61, ''.join(logs.output))
        self.assertIsNone(f.md_reason)


class Restart(unittest.TestCase):

    def test_a_new_daemon_starts_empty(self):
        f, clock = feed()
        run(f, clock, 5)
        g, clock2 = feed(at=clock.at + 200)                    # the same moment, a restarted daemon
        snap = g.snapshot()
        self.assertFalse(snap['fresh'])
        self.assertEqual(snap['reason'], 'no_data')
        self.assertEqual(snap['bars'], [])
        run(g, clock2, 3, start=T0 + 6000)
        clock2.at = T0 + 9050
        self.assertTrue(g.snapshot()['fresh'])

    def test_a_page_reload_is_not_live_until_resubscribed(self):
        f, clock = feed()
        run(f, clock, 4)
        page = marketfeed.PageFeed(f, md_url_pattern=marketfeed.DEFAULT_MD_URL_PATTERN, trade_url_pattern='^x$',
                                   md_symbol='MESZ1', bridge_api='__dlb_x')
        page.lost()
        clock.at = T0 + 4050
        self.assertEqual(f.snapshot()['reason'], 'md_not_connected:page_reset')


class Config(unittest.TestCase):

    def test_settings(self):
        self.assertIsNone(marketfeed.settings({}))
        self.assertIsNone(marketfeed.settings({'market_feed': {'enabled': False}}))
        self.assertEqual(marketfeed.settings({'market_feed': {'enabled': True}}),
                         {'md_url_pattern': marketfeed.DEFAULT_MD_URL_PATTERN})
        for bad in ({'enabled': 'yes'}, {'enabled': True, 'extra': 1}, [], {'enabled': True, 'md_url_pattern': '.*'},
                    {'enabled': True, 'md_url_pattern': '^wss://evil.example/v1/websocket$'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                marketfeed.settings({'market_feed': bad})

    def test_lumi_without_the_feed_does_not_start(self):
        problem = marketfeed.requirement_problem({'adapters': {'lumi-gap-bridge-v2': {'enabled': True}}},
                                                 ['lumi-gap-bridge-v2'])
        self.assertIn('Lumi RTH Gap (lumi-gap-bridge-v2) requires the 1-second MES feed', problem)
        self.assertIn('"market_feed": {"enabled": true}', problem)
        self.assertIsNone(marketfeed.requirement_problem({'market_feed': {'enabled': True}}, ['lumi-gap-bridge-v2']))
        self.assertIsNone(marketfeed.requirement_problem({}, ['soup-v3']))

    def test_daemon_refuses_to_start_lumi_without_the_feed(self):
        import tempfile
        from unittest import mock
        from tvbridge import daemon
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'config.json'
            cfg.write_text(json.dumps({'ticker': 'MESZ2021', 'tick_size': 0.25,
                                       'adapters': {'lumi-gap-bridge-v2': {'enabled': True}}}))
            with mock.patch('sys.argv', ['daemon', '--config', str(cfg)]), \
                    mock.patch.object(daemon, 'Bridge', side_effect=AssertionError('browser touched')):
                with self.assertRaises(SystemExit) as caught:
                    daemon.main()
        self.assertIn('requires the 1-second MES feed', str(caught.exception))
        self.assertIn('the bridge daemon does not start', str(caught.exception))

    def test_setup_writes_the_feed_for_lumi_only(self):
        import importlib.util
        path = Path(__file__).resolve().parents[2] / 'setup.py'
        spec = importlib.util.spec_from_file_location('tvbridge_setup_for_test', path)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        self.assertEqual(setup.adapter_config('MESZ2021', 'lumi-gap-bridge-v2')['market_feed'], {'enabled': True})
        self.assertNotIn('market_feed', setup.adapter_config('MNQZ2021', None))


class FeedRoute(unittest.TestCase):
    """GET /feed/<token> on the daemon's loopback server (order token only)."""

    TOKEN = 'f' * 32

    class Ex:
        cfg = {'ticker': 'MESZ2021'}

    def serve(self, f):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.Ex(), None, self.TOKEN, feed=f))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d' % server.server_address[1]

    def get(self, base, path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_feed_answer(self):
        f, clock = feed()
        run(f, clock, 3)
        clock.at = T0 + 3050
        base = self.serve(f)
        status, body = self.get(base, '/feed/' + self.TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body['schema'], marketfeed.SCHEMA)
        self.assertTrue(body['fresh'])
        self.assertEqual(self.get(base, '/feed/' + 'x' * 32)[0], 404)
        self.assertEqual(self.get(base, '/feed/' + self.TOKEN + '?a=1')[0], 400)

    def test_no_feed_configured(self):
        status, body = self.get(self.serve(None), '/feed/' + self.TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual((body['configured'], body['fresh'], body['reason']), (False, False, 'not_configured'))


class FakeSession:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None, timeout=10):
        self.calls.append((method, params))
        return {'identifier': str(len(self.calls))} if method == 'Page.addScriptToEvaluateOnNewDocument' else {}

    def evaluate(self, expression, timeout=10):
        self.calls.append(('evaluate', expression[:40]))
        return True

    def close(self):
        pass


class PageExtension(unittest.TestCase):

    def setUp(self):
        self.f, self.clock = feed()
        self.f.on_state('not_started')
        self.bridge = Bridge(AccountState(), devtools_port=0, page_url_contains='x', trade_url_pattern='^wss://x$')
        self.page = marketfeed.PageFeed(self.f, md_url_pattern=marketfeed.DEFAULT_MD_URL_PATTERN,
                                        trade_url_pattern='^wss://demo\\.tradovateapi\\.com/v1/websocket',
                                        md_symbol='MESZ1', bridge_api=self.bridge.api)
        self.bridge.extensions.append(self.page)

    def test_events_on_its_own_binding_reach_the_feed_only(self):
        call = {'name': self.page.binding, 'payload': json.dumps({'type': 'md_state', 'data': {'state': 'subscribed'}})}
        self.bridge._on_event('Runtime.bindingCalled', call)
        self.assertEqual(self.f.md_state, 'subscribed')
        self.assertEqual(self.bridge.events, [])               # nothing reaches the order bridge's event list
        self.bridge._on_event('Runtime.bindingCalled', {'name': self.page.binding, 'payload': 'not json'})
        self.assertEqual(self.f.rejected['malformed_event'], 1)
        self.bridge._on_event('Runtime.executionContextsCleared', {})
        self.assertEqual(self.f.md_state, 'page_reset')

    def test_installed_after_the_order_script_and_removed_before_it(self):
        session = FakeSession()
        self.page.install(session)
        source = [p['source'] for m, p in session.calls if m == 'Page.addScriptToEvaluateOnNewDocument'][0]
        self.assertIn('"symbol": "MESZ1"', source)
        self.assertIn(json.dumps(self.bridge.api), source)
        self.assertNotIn('__DLB_MD_CONFIG__', source)
        self.assertTrue(self.page.uninstall(session))
        self.assertEqual(self.f.md_state, 'stopped')

    def test_bridge_attach_order(self):
        from unittest import mock
        session = FakeSession()
        with mock.patch('tvbridge.bridge.cdp.Session', return_value=session), \
                mock.patch('tvbridge.bridge.threading.Thread'):
            self.bridge.debugger_url = 'ws://127.0.0.1:1/devtools/page/x'
            self.bridge.attach(reload=True)
        scripts = [p['source'] for m, p in session.calls if m == 'Page.addScriptToEvaluateOnNewDocument']
        self.assertEqual(len(scripts), 2)
        self.assertIn('__dlb_', scripts[0][:2000] + json.dumps(self.bridge.api))
        self.assertIn('mdfeed', marketfeed.SCRIPT.name)
        self.assertIn('md/subscribeQuote', scripts[1])          # the feed script is the second one
        self.assertEqual(session.calls[-1][0], 'Page.reload')   # reload only after both are installed


NODE_HARNESS = r'''
const vm = require('vm');
const src = process.argv[process.argv.length - 1];
const out = {emitted: [], sockets: []};
const timers = [];
const ctx = {console, JSON, Math, Date, Promise, String, Array, Set, Map, Object, RegExp, URL, Error};
ctx.performance = {now: () => Date.now()};
ctx.setInterval = (fn, ms) => { const t = {fn, ms, on: true}; timers.push(t); return t; };
ctx.clearInterval = t => { if (t) t.on = false; };
ctx.setTimeout = (fn, ms) => { const t = {fn, ms, on: true, once: true}; timers.push(t); return t; };
ctx.clearTimeout = t => { if (t) t.on = false; };
ctx.location = {href: 'https://trader.tradovate.com/'};
class FakeWS {
  constructor(url) { this.url = url; this.sent = []; this.readyState = 1; this.listeners = {}; out.sockets.push(this); }
  send(data) { this.sent.push(data); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  close() { this.closed = true; }
}
FakeWS.OPEN = 1;
function XHR() {}
XHR.prototype.open = function() {};
ctx.XMLHttpRequest = XHR;
ctx.fetch = () => Promise.resolve({ok: true, url: '', clone() { return this; }, text: () => Promise.resolve('{}')});
ctx.WebSocket = FakeWS;
ctx.__dlb_bridge = {};
ctx.__dlbme_test = data => out.emitted.push(JSON.parse(data));
vm.createContext(ctx);
vm.runInContext('globalThis.window = globalThis; globalThis.top = globalThis;', ctx);
vm.runInContext(src, ctx);
const W = ctx.WebSocket;
out.wrapped = W !== FakeWS;
// The app's own sockets: the trading one is not touched; its market-data one teaches the token.
const trade = new W('wss://demo.tradovateapi.com/v1/websocket');
trade.send('authorize\n0\n\nTRADE-TOKEN-0123456789');
const app = new W('wss://md.tradovateapi.com/v1/websocket');
app.send('authorize\n0\n\nMD-TOKEN-0123456789abcdef');
const own = out.sockets[2];
out.ownUrl = own && own.url;
own.onmessage({data: 'o'});
own.onmessage({data: 'a[{"i":0,"s":200}]'});
own.onmessage({data: 'a[{"i":1,"s":200,"d":{}}]'});
own.onmessage({data: 'a[{"e":"md","d":{"quotes":[{"timestamp":"2021-10-06T14:00:00.250Z","contractId":3000123,' +
                '"entries":{"Trade":{"price":5000.25,"size":2},"Bid":{"price":5000,"size":9}}},' +
                '{"timestamp":"2021-10-06T14:00:00.300Z","contractId":3000123,"entries":{"Bid":{"price":5000,"size":8}}}]}}]'});
own.onmessage({data: 'a[{"e":"props","d":{"entityType":"order","secret":"x"}}]'});
out.ownSent = own.sent.slice();
delete ctx.__dlb_bridge;                                   // the order bridge's page script stopped
for (const t of timers.slice()) if (t.on && t.ms <= 50) t.fn();
out.restored = ctx.WebSocket === FakeWS;
out.ownClosed = !!own.closed;
out.apiGone = ctx.__dlbm_test === undefined;
console.log(JSON.stringify(out, (k, v) => k === 'listeners' || k === 'onmessage' ? undefined : v));
'''


@unittest.skipIf(shutil.which('node') is None, 'node is not installed; the page script check needs it')
class PageScript(unittest.TestCase):

    def test_reads_one_contract_and_forwards_only_quote_numbers(self):
        config = {'mdUrlPattern': marketfeed.DEFAULT_MD_URL_PATTERN,
                  'tradeUrlPattern': '^wss://demo\\.tradovateapi\\.com/v1/websocket', 'symbol': 'MESZ1',
                  'api': '__dlbm_test', 'binding': '__dlbme_test', 'bridgeApi': '__dlb_bridge', 'tickMs': 10}
        source = marketfeed.SCRIPT.read_text().replace('__DLB_MD_CONFIG__', json.dumps(config))
        result = subprocess.run(['node', '-e', NODE_HARNESS, source], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertTrue(out['wrapped'])
        self.assertEqual(out['ownUrl'], 'wss://md.tradovateapi.com/v1/websocket')
        self.assertEqual(out['ownSent'], ['authorize\n0\n\nMD-TOKEN-0123456789abcdef',
                                          'md/subscribeQuote\n1\n\n{"symbol":"MESZ1"}'])
        states = [e['data']['state'] for e in out['emitted'] if e['type'] == 'md_state']
        self.assertEqual(states, ['connecting', 'authorized', 'subscribed'])
        quotes = [e['data']['quotes'] for e in out['emitted'] if e['type'] == 'md_quotes']
        self.assertEqual(quotes, [[{'contractId': 3000123, 'timestamp': '2021-10-06T14:00:00.250Z', 'price': 5000.25},
                                   {'contractId': 3000123, 'timestamp': '2021-10-06T14:00:00.300Z', 'price': None}]])
        self.assertEqual({e['type'] for e in out['emitted']}, {'md_state', 'md_quotes'})
        self.assertNotIn('TOKEN', json.dumps(out['emitted']))  # no token ever leaves the page
        self.assertEqual(len(out['sockets']), 3)               # the app's two and the feed's own; no other socket
        self.assertTrue(out['restored'] and out['ownClosed'] and out['apiGone'])


if __name__ == '__main__':
    unittest.main()
