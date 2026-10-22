"""Explicit-only customer market browser, isolated from Guardian and owner tabs.

The inherited transport verifies this service's process, profile and socket for
every CDP operation. Only internal native-feed code can evaluate a program; the
public service accepts one exact chart URL, never a port, target or JavaScript.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlsplit

from guardian_auth import GuardianError
from guardian_browser import OwnedBrowser


class MarketBrowserError(GuardianError):
    def __init__(self, code):
        super().__init__(code, 'The dedicated market browser needs attention. Check the saved chart and its open tabs.', 409)


def validate_chart_url(url):
    if type(url) is not str or re.fullmatch(r'https://www\.tradingview\.com/chart/[A-Za-z0-9]{6,24}/', url) is None:
        raise MarketBrowserError('market_chart_invalid')
    return url


class MarketBrowser(OwnedBrowser):
    port = 9567
    unit = 'decrypt-os-market-browser.service'
    profile_component = 'trading'

    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.chart_url = None
        self.login_target_id = None
        self.login_process_identity = None
        self.mutex = threading.RLock()

    @staticmethod
    def _charts(pages):
        # Count even malformed/query/alternate TradingView chart tabs as a
        # conflict. Never open a second chart to hide a wrong existing target.
        charts = []
        for page in pages:
            if page.get('type') != 'page':
                continue
            try:
                url = urlsplit(page.get('url') or '')
            except ValueError:
                raise MarketBrowserError('market_chart_conflict') from None
            if (url.hostname or '').lower() in ('www.tradingview.com', 'tradingview.com') and url.path.startswith('/chart'):
                charts.append(page)
        return charts

    def _target(self, target_id):
        if type(target_id) is not str or re.fullmatch(r'[A-Za-z0-9-]{1,100}', target_id) is None:
            raise MarketBrowserError('market_target_invalid')
        charts = self._charts(self.targets())
        if (self.chart_url is None or len(charts) != 1 or charts[0].get('id') != target_id
                or charts[0].get('url') != self.chart_url):
            raise MarketBrowserError('market_chart_conflict')
        return charts[0]

    def login_target(self):
        """The bound feed tab only; an exact same-tab sign-in redirect is allowed.

        This does not relax _target(), which remains chart-only for market data.
        No second chart, sign-in tab or profile is opened by recovery.
        """
        with self.mutex:
            if self.chart_url is None or self.login_target_id is None:
                raise MarketBrowserError('market_chart_not_configured')
            if (type(self.login_target_id) is not str
                    or re.fullmatch(r'[A-Za-z0-9-]{1,100}', self.login_target_id) is None):
                raise MarketBrowserError('market_target_invalid')
            pages = self.targets()
            if self.login_process_identity != self.process_identity:
                raise MarketBrowserError('market_chart_conflict')
            charts = self._charts(pages)
            if (len(charts) > 1 or any(p.get('id') != self.login_target_id
                                     or p.get('url') != self.chart_url for p in charts)):
                raise MarketBrowserError('market_chart_conflict')
            matches = [p for p in pages if p.get('type') == 'page' and p.get('id') == self.login_target_id]
            if (len(matches) != 1 or matches[0].get('url') not in
                    (self.chart_url, 'https://www.tradingview.com/accounts/signin/')):
                raise MarketBrowserError('market_chart_conflict')
            return matches[0]

    def evaluate_login(self, target_id, expression):
        """Internal login/proof programs on the pinned feed tab, not a public API."""
        with self.mutex:
            if self.login_target().get('id') != target_id:
                raise MarketBrowserError('market_chart_conflict')
            result = self._rpc(target_id, 'Runtime.evaluate', {
                'expression': expression, 'returnByValue': True,
                'awaitPromise': True, 'timeout': 15000})
            if result.get('exceptionDetails') or result.get('result', {}).get('subtype') == 'error':
                raise MarketBrowserError('market_browser_unavailable')
            return result.get('result', {}).get('value')

    def focus_login(self):
        """Explicit focus only. Starting/configuring a feed remains a trading action."""
        with self.mutex:
            target = self.login_target()
            self._rpc(target['id'], 'Page.bringToFront', {})

    def open(self, chart_url):
        """Idempotently open one chart only after an explicit local action."""
        validate_chart_url(chart_url)
        with self.mutex:
            if self.chart_url is not None and self.chart_url != chart_url:
                raise MarketBrowserError('market_binding_immutable')
            self._systemctl('start', self.unit)
            deadline = time.monotonic() + 8
            while True:
                try:
                    pages = self.targets()
                    charts = self._charts(pages)
                    break
                except GuardianError:
                    if time.monotonic() >= deadline:
                        raise MarketBrowserError('market_browser_unavailable') from None
                    time.sleep(.2)
            if len(charts) > 1 or (charts and charts[0].get('url') != chart_url):
                raise MarketBrowserError('market_chart_conflict')
            self.chart_url = chart_url
            if charts:
                target_id = charts[0]['id']
            else:
                prior = [p for p in pages if p.get('type') == 'page' and p.get('id') == self.login_target_id]
                if prior:
                    # A logout redirect must not cause a second feed tab.
                    target_id = self.login_target()['id']
                else:
                    # After a backend restart we cannot know which existing
                    # sign-in/other page was the old feed. Its later navigation
                    # could revive that feed, so only a blank browser permits
                    # creation. Do not guess ownership from a familiar origin.
                    if any(p.get('type') == 'page' and p.get('url') != 'about:blank' for p in pages):
                        raise MarketBrowserError('market_chart_conflict')
                    target_id = self._new_page(chart_url)
            self.login_target_id = target_id
            self.login_process_identity = self.process_identity
            # Target creation is not proof of native market data or login.
            self.login_target()
            self._rpc(target_id, 'Page.bringToFront', {})
            return target_id

    def _navigate(self, target_id, url):
        raise MarketBrowserError('market_navigation_disabled')

    def ensure_login(self, provider):
        raise MarketBrowserError('market_navigation_disabled')

    def ensure_account_pages(self, config):
        raise MarketBrowserError('market_navigation_disabled')
