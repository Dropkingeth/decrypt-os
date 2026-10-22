"""Explicit customer feed worker. No automatic browser/hook recovery loops."""
from __future__ import annotations

from pathlib import Path
import sys
import threading

from guardian_auth import GuardianError
from trading_browser import validate_chart_url

PACKAGE = Path(__file__).resolve().parents[1] / 'trading-runtime'
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))
from decrypt_trading.native_market import NativeMarketFeed


class FeedWorker:
    def __init__(self, runtime, browser, chart_url):
        self.runtime, self.browser = runtime, browser
        self.chart_url = validate_chart_url(chart_url)
        self.mutex = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.closed = False
        self.started_at = None
        self.shutdown_done = False
        self._status = {'state': 'not_started', 'reason': 'explicit_open_required',
                        'nativeVerified': False, 'observedAt': None}

    def _invalidate(self, reason):
        self.runtime.invalidate_market(reason, producer_id=self.runtime.binding['feedProducerId'])

    def _record(self, state, reason, native_verified=False, observed_at=None):
        with self.mutex:
            self._status = {'state': state, 'reason': reason, 'nativeVerified': native_verified,
                            'observedAt': observed_at}

    def status(self):
        with self.mutex:
            value = dict(self._status)
        if value['nativeVerified']:
            stamp = value['observedAt']
            if type(stamp) is not int or not 0 <= self.runtime.clock() - stamp <= 2000:
                value.update(state='hold', reason='no_fresh_native_callback', nativeVerified=False)
        return value

    def start(self):
        """Explicit retry is required after source loss or a chart reload."""
        with self.mutex:
            if self.closed:
                raise GuardianError('market_worker_closed', status=409)
            if self.worker is not None and self.worker.is_alive():
                return self.status()
            self.stop_event.clear()
            self.started_at = self.runtime.clock()
            self._invalidate('native_feed_starting')
            self._record('starting', 'opening_customer_chart')
            self.worker = threading.Thread(target=self._work, name='customer-native-market', daemon=True)
            self.worker.start()
            return self.status()

    def _work(self):
        feed = None
        try:
            target_id = self.browser.open(self.chart_url)
            if self.stop_event.is_set():
                return
            feed = NativeMarketFeed(self.runtime, self.browser, chart_url=self.chart_url, target_id=target_id)
            feed.install()
            self._record('waiting', 'waiting_for_native_callback')
            while not self.stop_event.is_set():
                result = feed.tick()
                if result.get('reason') == 'native_acquisition_invalid':
                    self._invalidate('native_acquisition_invalid')
                    self._record('requires_retry', 'native_acquisition_invalid')
                    break
                stamp = result.get('observedAt')
                fresh = (result.get('nativeVerified') is True and type(stamp) is int
                         and stamp > self.started_at and 0 <= self.runtime.clock() - stamp <= 2000)
                if fresh:
                    self._record('fresh', None, True, stamp)
                else:
                    self._invalidate('no_fresh_native_callback')
                    self._record('waiting', 'no_fresh_native_callback')
                self.stop_event.wait(.25)
        except Exception:
            self._invalidate('native_feed_unavailable')
            self._record('requires_retry', 'native_feed_unavailable')
        finally:
            if feed is not None:
                try:
                    feed.close()
                except Exception:
                    pass
            if self.stop_event.is_set():
                self._record('closed', 'stopped')

    def close(self):
        # Do not hold this lock while waiting for the owning worker to close
        # its SQLite store and hook. The browser itself is not stopped here.
        with self.mutex:
            if self.shutdown_done:
                return
            self.closed = True
            self.stop_event.set()
            worker = self.worker
        self._invalidate('native_feed_stopped')
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=45)
            if worker.is_alive():
                raise GuardianError('market_worker_busy', status=409)
        self._record('closed', 'stopped')
        with self.mutex:
            self.shutdown_done = True
