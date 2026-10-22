"""Owned-page management regressions; no sockets, browser launch or live login."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend'))
from guardian_auth import GuardianError
from guardian_browser import OwnedBrowser, origin

class FixtureBrowser(OwnedBrowser):
    def __init__(self):
        super().__init__('/fixture/customer-state')
        self.pages, self.created, self.navigated, self.started, self.rpc = [], [], [], [], []
    def targets(self): return [dict(p) for p in self.pages]
    def _new_page(self, url):
        ident = 'fixture-' + str(len(self.pages) + 1)
        self.pages.append({'id': ident, 'type': 'page', 'url': url})
        self.created.append(url)
        return ident
    def _navigate(self, target_id, url):
        self.navigated.append((target_id, url))
        next(p for p in self.pages if p['id'] == target_id)['url'] = url
    def _systemctl(self, *args): self.started.append(args)
    def _rpc(self, target_id, method, params): self.rpc.append((target_id, method, params)); return {}
    def evaluate(self, target_id, expression): return {'state': 'logged_out'}


class BrowserTests(unittest.TestCase):
    def test_only_fixed_https_provider_origins(self):
        self.assertEqual(origin('https://www.tradingview.com/accounts/signin/'), 'https://www.tradingview.com')
        for value in ('http://www.tradingview.com', 'https://user:pass@www.tradingview.com', 'https://www.tradingview.com:9999', 'https://www.tradingview.com:bad'):
            self.assertIsNone(origin(value))

    def test_multiple_logout_pages_get_one_dedicated_auth_target(self):
        browser = FixtureBrowser()
        for _ in range(3): browser._new_page('https://www.tradingview.com/accounts/signin/')
        self.assertEqual(browser.inspect('tradingview')['state'], 'page_missing')
        self.assertEqual(len(browser.created), 4)
        view = browser.inspect('tradingview')
        self.assertEqual(view['state'], 'logged_out')
        self.assertEqual(view['targetId'], browser.auth_targets['tradingview'])
        self.assertEqual(len(browser.created), 4)

    def test_open_brings_existing_challenge_forward_without_navigating(self):
        browser = FixtureBrowser()
        target = browser._new_page('https://www.tradingview.com/accounts/signin/?mfa=true')
        browser.auth_targets['tradingview'] = target
        browser.open('tradingview')
        self.assertEqual(browser.navigated, [])
        self.assertEqual(browser.rpc, [(target, 'Page.bringToFront', {})])

    def test_cross_provider_submission_blocked_before_evaluate(self):
        browser = FixtureBrowser()
        target = browser._new_page('https://www.tradingview.com/accounts/signin/')
        with self.assertRaises(GuardianError):
            browser.submit('tradovate', target, 'fixture-user', 'fixture-password')


if __name__ == '__main__': unittest.main()
