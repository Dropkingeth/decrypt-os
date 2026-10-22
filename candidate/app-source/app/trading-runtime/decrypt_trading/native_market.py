"""Customer-owned native TradingView chart acquisition; never navigates a page.

Uses live main-series callbacks and native completed candles. Browser callback
time is not exchange trade time. Browser transport must be the app-owned isolated
customer profile, not an arbitrary request-supplied debugger connection.
"""
import hashlib
import json
from pathlib import Path
import re
import threading

from .native_market_store import MarketStore, SOURCE
from .runtime import RuntimeError, _private_file


class NativeMarketFeed:
    def __init__(self, runtime, browser_transport, *, chart_url, target_id):
        if type(chart_url) is not str or re.fullmatch(r'https://www\.tradingview\.com/chart/[A-Za-z0-9]{6,24}/', chart_url) is None:
            raise RuntimeError('explicit customer chart URL required')
        if type(target_id) is not str or re.fullmatch(r'[A-Za-z0-9_-]{8,128}', target_id) is None:
            raise RuntimeError('explicit customer chart target required')
        self.runtime, self.browser, self.chart_url, self.target_id = runtime, browser_transport, chart_url, target_id
        self.key = '__decryptCustomerMarket_' + hashlib.sha256(runtime.binding['instanceId'].encode()).hexdigest()[:20]
        self.binding_key = self.key + '_deliver'
        self.store = None
        self.mutex = threading.RLock()
        self.cursor = None
        self.installed = False
        self.closed = False
        self.last_status = {'state': 'not_started', 'nativeVerified': False, 'reason': 'waiting_for_native_callback'}

    def _target(self):
        targets = self.browser.targets()
        if type(targets) is not list or len(targets) > 128:
            raise RuntimeError('customer browser target inventory invalid')
        matches = [t for t in targets if type(t) is dict and t.get('type') == 'page' and t.get('url') == self.chart_url]
        if len(matches) != 1 or matches[0].get('id') != self.target_id:
            raise RuntimeError('customer chart target missing or ambiguous')

    def _health(self, value):
        symbol = 'CME_MINI:' + self.runtime.binding['symbol']
        return (type(value) is dict and value.get('page_url') == self.chart_url and value.get('same_series') is True
                and type(value.get('chart_style')) is int and value['chart_style'] == 1
                and value.get('series_symbol') == symbol and value.get('timeframe') == '1S'
                and value.get('chart_ready') is True and value.get('chart_failed') is False
                and type(value.get('series_status')) is int and value['series_status'] == 3
                and value.get('is_delayed') is False and type(value.get('replay_status')) is int and value['replay_status'] == 0
                and (type(value.get('native5_status')) is not dict or value['native5_status'].get('failed') is not True)
                and value.get('stopped') is False and value.get('symbol_info') == {'full_name': symbol, 'minmov': 25, 'pricescale': 100})

    def install(self):
        """Explicit hook installation only on already-open, exact customer page."""
        with self.mutex:
            return self._install()

    def _install(self):
        if self.closed or self.installed:
            raise RuntimeError('native feed lifecycle invalid')
        self._target()
        path = self.runtime.directory / 'market.sqlite'
        _private_file(path)
        self.store = MarketStore(path, symbol=self.runtime.binding['symbol'], chart_url=self.chart_url)
        # No old quote or historical bar is replayed as current after restart.
        self.cursor = self.store.sequence
        script = Path(__file__).with_name('native_chart_hook.js').read_text()
        for marker, value in [('__CUSTOMER_CHART_URL__', self.chart_url), ('__CUSTOMER_BINDING__', self.binding_key), ('__CUSTOMER_READER_KEY__', self.key)]:
            script = script.replace(marker, json.dumps(value))
        try:
            health = self.browser.evaluate(self.target_id, script)
            if not self._health(health):
                raise RuntimeError('native chart identity or state not qualified')
            self.installed = True
            self.last_status = {'state': 'waiting', 'nativeVerified': False, 'reason': 'waiting_for_native_callback', 'source': SOURCE}
        except BaseException:
            self.store.close()
            self.store = None
            raise
        return dict(self.last_status)

    def tick(self):
        """Bounded callback drain. Call from one worker at <=500ms cadence."""
        with self.mutex:
            return self._tick()

    def _tick(self):
        if self.closed or not self.installed:
            raise RuntimeError('native feed is not installed')
        try:
            self._target()
            result = self.browser.evaluate(self.target_id, '(() => {const s=window['+json.dumps(self.key)+'];return s?s.drain():null;})()')
            if (type(result) is not dict or set(result) != {'health', 'overflow', 'packets'}
                    or not self._health(result['health']) or result['overflow'] is not False
                    or type(result['packets']) is not list or len(result['packets']) > 128):
                raise RuntimeError('native source lost or acquisition overflow')
            now, accepted = self.runtime.clock(), 0
            for packet in result['packets']:
                if not self._health(packet):
                    raise RuntimeError('native callback identity invalid')
                self.store.ingest(packet, now)
            rows = self.store.db.execute('SELECT seq,channel,event_id,payload FROM events WHERE seq>? ORDER BY seq LIMIT 1024', (self.cursor,)).fetchall()
            for seq, channel, event_id, body in rows:
                payload = json.loads(body)
                intervals = self.runtime.market_bar_intervals()
                if channel == 'bar':
                    if intervals == (30000,):
                        if payload['close_time']-payload['open_time'] != 5000:
                            self.cursor = seq
                            continue
                        aggregate = self._thirty_bar(payload['close_time'])
                        if aggregate is None:
                            self.cursor = seq
                            continue
                        payload = aggregate
                        body = json.dumps(aggregate, sort_keys=True, separators=(',', ':'))
                        event_id = aggregate['event_id']
                    elif payload['close_time']-payload['open_time'] not in intervals:
                        self.cursor = seq
                        continue
                # The authenticated authority here is the locally bound browser
                # acquisition adapter. User JSON cannot assert these callbacks.
                receipt = self.runtime.admit_authenticated(channel, body.encode(),
                    producer_id=self.runtime.binding['feedProducerId'], event_id=event_id, sequence=seq)
                self.cursor = seq
                accepted += int(not receipt['duplicate'])
            previous = self.store.previous
            fresh = bool(previous and 0 <= now - self._stamp(previous['observed_at']) <= 2000)
            self.last_status = {'state': 'fresh' if fresh else 'hold', 'nativeVerified': fresh,
                'reason': None if fresh else 'no_fresh_native_callback', 'source': SOURCE,
                'observedAt': self._stamp(previous['observed_at']) if previous else None,
                'timestampSemantics': 'browser_callback_not_exchange_trade', 'acceptedInputs': accepted}
        except Exception:
            self.runtime.invalidate_market('native_acquisition_invalid', producer_id=self.runtime.binding['feedProducerId'])
            self.last_status = {'state': 'hold', 'nativeVerified': False, 'reason': 'native_acquisition_invalid', 'source': SOURCE}
            # Data cannot be made fresh by polling health or retrying an old
            # callback. The runtime quote TTL independently gates new entries.
        return dict(self.last_status)

    def _thirty_bar(self, close_time):
        """Six contiguous native five-second bars, never price-poll OHLC."""
        if close_time % 30000:
            return None
        rows = self.store.db.execute('SELECT payload FROM finalized WHERE close_time>? AND close_time<=? AND event_id LIKE ? ORDER BY close_time',
                                    (close_time-30000, close_time, 'tvbar:'+self.runtime.binding['symbol']+':5:%')).fetchall()
        bars = [json.loads(row[0]) for row in rows]
        if len(bars) != 6 or any(bar['open_time'] != close_time-30000+i*5000 or bar['close_time'] != close_time-25000+i*5000 for i, bar in enumerate(bars)):
            return None
        return {'event_id':'native30:'+self.runtime.binding['symbol']+':'+str(close_time-30000),
                'symbol':self.runtime.binding['symbol'],'open_time':close_time-30000,'close_time':close_time,
                'open':bars[0]['open'],'high':max(bar['high'] for bar in bars),'low':min(bar['low'] for bar in bars),'close':bars[-1]['close']}

    @staticmethod
    def _stamp(value):
        from datetime import datetime
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()*1000)

    def close(self):
        with self.mutex:
            return self._close()

    def _close(self):
        if self.closed:
            return
        if self.installed:
            try:
                self._target()
                self.browser.evaluate(self.target_id, 'window['+json.dumps(self.key)+']?.stop()')
            except Exception:
                pass  # the hook stops itself when its 5-second lease expires
        if self.store:
            self.store.close()
        self.closed = True
        self.last_status = {'state': 'closed', 'nativeVerified': False, 'reason': 'stopped'}
