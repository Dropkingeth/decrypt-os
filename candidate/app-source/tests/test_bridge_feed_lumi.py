"""Lumi requires the 1-second MES feed (bridge_feed.py + bridge_v2_lumi.Engine), offline with a fake feed.

The fake bridge answers GET /feed the way tvbridge.marketfeed.SecondFeed.snapshot() does; the real route reader,
runtime intake and Lumi engine run unchanged. No network: sockets are patched to fail. Cases: fresh, stale,
gap, wrong contract, restart, and the start refusal with its plain-language message.
"""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_trading_bridge_v2_lumi import V2LumiBase, mes_binding, ADAPTER  # noqa: E402
from bridge_fixture import FakeBridge  # noqa: E402
from bridge_v2_lumi_payloads import CONTRACT, entry, touch_entry  # noqa: E402
from decrypt_trading import bridge_feed, bridge_v2_lumi as lumi  # noqa: E402
from decrypt_trading.bridge_route import BindingError, open_route  # noqa: E402


class FeedBase(V2LumiBase):
    """Samples go through the real path and are NOT pre-warmed (unlike V2LumiBase.quote)."""

    def sample(self, price, close=None):
        bars = self.bridge.feed_bars
        if close is None:
            close = bars[-1]['close_time'] + 1000 if bars else (self.now // 1000 + 1) * 1000
        self.now = max(self.now, close + 1)
        self.price = price
        bars.append({'t': close - 1000, 'close_time': close, 'o': price, 'h': price, 'l': price, 'c': price,
                     'trades': 1})
        self.transport.feed.refresh()
        self.runtime._bridge_tick()
        while self.runtime.step():
            pass
        return close

    def live(self, price, seconds=3):
        for _ in range(seconds):
            self.sample(price)

    def pine(self, value):
        self.runtime.set_controls(enabled=True, locked=False)
        self.ingest('pine', value)
        return self.detail()

    def quote_details(self):
        return [r[0] for r in self.engine.db.execute("SELECT detail FROM inbox WHERE channel='quote' ORDER BY seq")]


class Fresh(FeedBase):

    def test_three_live_seconds_make_the_feed_fresh_and_the_entry_is_sent(self):
        value = entry(ts=self.now + 4000)
        self.live(5000.0)
        self.assertIsNone(self.engine.feed_problem(self.now))
        self.assertEqual(self.engine.data['last_quote']['price'], 5000.0)
        self.now = max(self.now, value['ts'])
        self.assertEqual(self.pine(value), 'entry_accepted_not_sent')
        self.gate()
        self.assertEqual(self.kinds(), ['bracket_entry'])
        # The quotes are the feed's own samples: event id bridge1s:<contract>:<second close>.
        ids = [json.loads(r[0])['event_id'] for r in
               self.engine.db.execute("SELECT body FROM inbox WHERE channel='quote' ORDER BY seq")]
        self.assertTrue(ids and all(i.startswith('bridge1s:%s:' % CONTRACT) for i in ids), ids)
        status = self.transport.status()['priceFeed']
        self.assertTrue(status['fresh'])
        self.assertEqual(status['message'], 'The 1-second MES feed is live and fresh.')

    def test_one_or_two_seconds_are_not_enough(self):
        self.live(5000.0, seconds=2)
        self.assertEqual(self.engine.feed_problem(self.now), 'mes_1s_feed_warming_up')
        value = entry(ts=self.now)
        self.assertEqual(self.pine(value), 'refused:mes_1s_feed_warming_up')
        self.assertEqual(self.bridge.sent, [])

    def test_no_feed_at_all_refuses_with_a_plain_message(self):
        value = entry(ts=self.now)
        self.assertEqual(self.pine(value), 'refused:mes_1s_feed_no_data')
        self.assertEqual(lumi.feed_message('mes_1s_feed_no_data'),
                         'Lumi refused: the 1-second MES feed has sent no price since Lumi started.')
        self.assertEqual(self.bridge.sent, [])


class Stale(FeedBase):

    def test_a_feed_silent_for_more_than_two_seconds_refuses_entries(self):
        self.live(5000.0)
        self.now += 2500                               # no new sample
        self.runtime._bridge_tick()
        self.runtime.step()
        self.assertEqual(self.engine.feed_problem(self.now), 'mes_1s_feed_stale')
        self.assertEqual(self.pine(entry(ts=self.now)), 'refused:mes_1s_feed_stale')
        self.assertEqual(self.bridge.sent, [])

    def test_a_not_fresh_bridge_answer_queues_nothing_and_withdraws_the_market(self):
        self.live(5000.0)
        self.bridge.feed_override = {'fresh': False, 'reason': 'stale', 'bars': []}
        before = self.engine.data['last_quote']
        self.now += 1000
        self.transport.feed.refresh()
        self.runtime._bridge_tick()
        self.assertEqual(self.runtime.market_invalid_reason, 'bridge_1s_feed_not_fresh_stale')
        self.assertEqual(self.engine.data['last_quote'], before)
        status = self.transport.status()['priceFeed']
        self.assertFalse(status['fresh'])
        self.assertIn('not live and fresh', status['message'])

    def test_an_old_bar_is_never_queued(self):
        self.bridge.feed_bars.append({'t': self.now - 6000, 'close_time': self.now - 5000, 'o': 5000.0,
                                      'h': 5000.0, 'l': 5000.0, 'c': 5000.0, 'trades': 1})
        self.transport.feed.refresh()
        self.runtime._bridge_tick()
        self.runtime.step()
        self.assertIsNone(self.engine.data['last_quote'])

    def test_an_open_trade_with_a_stale_feed_alerts_once_and_sends_nothing(self):
        value = entry(ts=self.now + 4000)
        self.live(value['entry']['price'])
        self.now = max(self.now, value['ts'])
        self.assertEqual(self.pine(value), 'entry_accepted_not_sent')
        self.gate()
        self.now += 200
        self.book.fill(self.book.entry, self.price)
        self.evidence()
        self.assertEqual(self.trade['phase'], 'running')
        sent = len(self.bridge.sent)
        for _ in range(3):
            self.now += 1500
            self.runtime.step()
        alerts = [a['code'] for a in self.trade['native']['alerts'] if a['code'].startswith('mes_1s_feed')]
        self.assertEqual(alerts, ['mes_1s_feed_not_live:mes_1s_feed_stale'])
        self.assertEqual(len(self.bridge.sent), sent)  # the resting stop protects; nothing is sent for a stale feed


class Gap(FeedBase):

    def test_a_missing_second_restarts_the_warm_up(self):
        self.live(5000.0)
        close = self.bridge.feed_bars[-1]['close_time']
        self.sample(5000.25, close=close + 2000)         # the second in between never arrived
        self.assertEqual(self.engine.feed_problem(self.now), 'mes_1s_feed_warming_up')
        self.assertEqual(self.pine(entry(ts=self.now)), 'refused:mes_1s_feed_warming_up')
        self.live(5000.25, seconds=2)
        self.assertIsNone(self.engine.feed_problem(self.now))

    def test_an_armed_touch_waits_while_warming_up(self):
        value = touch_entry(side='long', ts=self.now + 4000)
        self.live(value['touch']['price'] + 3)   # a long touch waits above its price
        self.now = max(self.now, value['ts'])
        self.assertEqual(self.pine(value), 'touch_entry_armed')
        close = self.bridge.feed_bars[-1]['close_time']
        self.sample(value['touch']['price'], close=close + 2000)          # touched, but right after a gap
        self.assertEqual(self.trade['phase'], 'armed')
        self.assertEqual(self.quote_details()[-1], 'quote_recorded_touch_waits_for_fresh_feed')
        self.assertEqual(self.bridge.sent, [])
        self.live(value['touch']['price'], seconds=2)                       # fresh again: the touch triggers
        self.assertEqual(self.trade['phase'], 'entering')

    def test_stop_touch_during_warm_up_disarms(self):
        value = touch_entry(side='long', ts=self.now + 4000)
        value['touch']['cancel_if_stop_touched'] = True
        self.live(value['touch']['price'] + 3)
        self.now = max(self.now, value['ts'])
        self.assertEqual(self.pine(value), 'touch_entry_armed')
        close = self.bridge.feed_bars[-1]['close_time']
        self.sample(value['stop']['price'], close=close + 3000)
        self.assertEqual(self.trade['phase'], 'closed')
        self.assertEqual(self.trade['closed_reason'], 'stop_touched_before_entry')


class WrongContract(FeedBase):

    def test_an_answer_for_another_contract_queues_nothing(self):
        self.bridge.feed_override = {'symbol': 'MESU2021'}
        self.live(5000.0)
        self.assertIsNone(self.engine.data['last_quote'])
        self.assertEqual(self.transport.feed.problem(), 'feed_wrong_contract')
        self.assertEqual(self.runtime.market_invalid_reason, 'bridge_1s_feed_wrong_contract')

    def test_a_quote_for_another_contract_is_refused_at_intake(self):
        with self.assertRaises(Exception):
            self.runtime.ingest('quote', json.dumps({'event_id': 'bridge1s:MESU2021:%d' % (self.now // 1000 * 1000),
                                                     'symbol': 'MESU2021', 'time': self.now // 1000 * 1000,
                                                     'price': 5000.0}),
                                producer_id=self.binding['feedProducerId'])

    def test_a_quote_that_is_not_a_feed_sample_is_refused(self):
        at = self.now // 1000 * 1000
        for event_id, time in (('q-1', at), ('bridge1s:MESU2021:%d' % at, at), ('bridge1s:%s:%d' % (CONTRACT, at + 1), at + 1)):
            with self.subTest(event_id=event_id):
                self.now = time + 1
                self.runtime.ingest('quote', json.dumps({'event_id': event_id, 'symbol': CONTRACT, 'time': time,
                                                         'price': 5000.0}), producer_id=self.binding['feedProducerId'])
                self.runtime.step()
                self.assertEqual(self.quote_details()[-1], 'input_rejected')
        self.assertIsNone(self.engine.data['last_quote'])


class Restart(FeedBase):

    def test_a_restart_starts_empty_and_needs_three_new_seconds(self):
        self.live(5000.0)
        self.assertIsNone(self.engine.feed_problem(self.now))
        self.runtime.close()
        self.now += 300                                 # a quick restart: the last sample is still < 2 s old
        self.open()
        self.assertIsNone(self.engine.data['last_quote'])
        self.assertEqual(self.engine.feed_problem(self.now), 'mes_1s_feed_no_data')
        self.runtime._bridge_tick()                     # the old bars are not queued again
        self.runtime.step()
        self.assertIsNone(self.engine.data['last_quote'])
        self.assertEqual(self.pine(entry(ts=self.now)), 'refused:mes_1s_feed_no_data')
        self.live(5000.0)
        self.assertIsNone(self.engine.feed_problem(self.now))


class TickSafety(FeedBase):
    """Review fix: the reader checks and uses ONE copy of the bridge's answer, and a feed error can never skip the
    route's exit guard or status write."""

    def test_a_poll_finishing_mid_tick_cannot_break_the_tick(self):
        from unittest.mock import patch
        self.live(5000.0, seconds=2)
        feed = self.transport.feed
        self.bridge.feed_bars.append({'t': self.now // 1000 * 1000, 'close_time': self.now // 1000 * 1000 + 1000,
                                      'o': 5000.0, 'h': 5000.0, 'l': 5000.0, 'c': 5000.0, 'trades': 1})
        self.now = self.now // 1000 * 1000 + 1001
        feed.refresh()
        real = bridge_feed.snapshot_problem

        def racing(symbol, snap, now):
            feed.latest = (now, None)                  # a failed poll lands between the check and the read
            return real(symbol, snap, now)
        with patch.object(bridge_feed, 'snapshot_problem', side_effect=racing):
            self.assertEqual(feed.tick(self.runtime), 1)  # it used the copy it checked
        self.assertEqual(feed.problem(), 'feed_unavailable')

    def test_a_feed_error_never_skips_the_exit_guard_or_the_status(self):
        from unittest.mock import patch
        status = self.bridge.soup / 'status.json'
        with patch.object(self.transport.feed, 'tick', side_effect=TypeError('boom')), \
                patch.object(self.transport, '_native_guard') as guard:
            self.transport.tick(self.runtime)
        guard.assert_called_once()
        self.assertTrue(status.exists())
        self.assertIn('price_feed', json.loads(status.read_text()))
        self.assertEqual(self.transport.feed.counts['tick_errors'], 1)


class StartRefusal(unittest.TestCase):
    """open_route: Lumi's route is not built unless the feed is live and fresh for its contract right now."""

    def setUp(self):
        import tempfile, socket
        from unittest.mock import patch
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1617717600000   # a synthetic 2021 New York morning
        self.binding = mes_binding()
        self.bridge = FakeBridge(Path(self.temp.name) / 'data', self.binding, clock=lambda: self.now)
        self.bridge.write_config(adapters={ADAPTER: {'enabled': True}}, market_feed={'enabled': True})

    def open(self):
        return open_route(self.binding, root=self.bridge.tvbridge, soup_root=self.bridge.soup, fetch=self.bridge.state,
                          clock=lambda: self.now, start=False, feed_fetch=self.bridge.feed, check_feed=True)

    def refused(self):
        with self.assertRaises(BindingError) as caught:
            self.open()
        return str(caught.exception)

    def bar(self):
        close = self.now // 1000 * 1000
        self.bridge.feed_bars.append({'t': close - 1000, 'close_time': close, 'o': 5000.0, 'h': 5000.0, 'l': 5000.0,
                                      'c': 5000.0, 'trades': 1})

    def test_fresh_feed_starts(self):
        self.bar()
        transport = self.open()
        self.assertIsNotNone(transport.feed)
        transport.close()

    def test_no_feed_yet_is_refused_in_plain_language(self):
        text = self.refused()
        self.assertTrue(text.startswith('Lumi RTH Gap needs the live 1-second MES feed from the bridge and did not '
                                        'start.'), text)
        self.assertIn('not live and fresh', text)

    def test_feed_not_configured_is_refused(self):
        self.bridge.feed_override = {'configured': False, 'fresh': False}
        self.assertIn('Add "market_feed": {"enabled": true}', self.refused())

    def test_unreachable_bridge_is_refused(self):
        self.bridge.available = False
        self.assertIn('did not answer', self.refused())

    def test_wrong_contract_is_refused(self):
        self.bar()
        self.bridge.feed_override = {'symbol': 'MESU2021'}
        self.assertIn('different contract', self.refused())

    def test_stale_answer_is_refused(self):
        self.bar()
        self.now += 2500
        self.assertIn('stale', self.refused())

    def test_other_strategies_get_no_feed_reader(self):
        from decrypt_trading import bridge_v2_registry
        others = [a for a in bridge_v2_registry.STRATEGIES if a not in bridge_feed.REQUIRED_BY]
        self.assertTrue(others)
        self.assertEqual(set(bridge_feed.REQUIRED_BY), {'lumi-gap-bridge-v2'})


if __name__ == '__main__':
    unittest.main()
