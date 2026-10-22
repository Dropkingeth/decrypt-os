"""Dedicated browser ownership and one-chart boundaries; no real CDP/launch."""
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/backend'))
import guardian_browser
from guardian_browser import OwnedBrowser
from guardian_auth import GuardianError
from trading_browser import MarketBrowser, MarketBrowserError, validate_chart_url

URL = 'https://www.tradingview.com/chart/AbCd1234/'
OTHER = 'https://www.tradingview.com/chart/AbCd5678/'


class FixtureBrowser(MarketBrowser):
    def __init__(self):
        super().__init__('/fixture/state')
        self.pages, self.started, self.created, self.calls = [], [], [], []

    def _systemctl(self, *args):
        self.started.append(args)

    def targets(self):
        return [dict(p) for p in self.pages]

    def _new_page(self, url):
        ident = 'fixture-' + str(len(self.pages) + 1)
        self.pages.append({'id': ident, 'type': 'page', 'url': url})
        self.created.append(url)
        return ident

    def _rpc(self, target, method, data):
        self.calls.append((target, method, data))
        return {'result': {'value': {'native': 'fixture'}}}


class BrowserBoundaries(unittest.TestCase):
    def test_invalid_urls_rejected_before_any_service_start(self):
        browser = FixtureBrowser()
        for url in (URL+'?x=1', URL+'#x', 'http://www.tradingview.com/chart/AbCd1234/',
                    'https://tradingview.com/chart/AbCd1234/', URL.replace('www.', 'evil.'),
                    URL.replace('https://', 'https://user@'), URL.replace('/AbCd1234/', '/../'),
                    'file:///fixture', 'javascript:alert(1)', 9567, {'url': URL}):
            with self.assertRaises(MarketBrowserError):
                browser.open(url)
        self.assertEqual(browser.started, [])

    def test_open_is_explicit_exact_idempotent_and_isolated(self):
        browser = FixtureBrowser()
        self.assertEqual(browser.started, [])
        target = browser.open(URL)
        self.assertEqual(browser.open(URL), target)
        self.assertEqual(browser.created, [URL])
        self.assertEqual(browser.started, [('start','decrypt-os-market-browser.service')]*2)
        self.assertEqual(browser.port, 9567)
        self.assertEqual(browser.profile, '/fixture/state/trading/browser-profile')
        self.assertEqual(OwnedBrowser('/fixture/state').port, 9566)
        self.assertEqual(OwnedBrowser('/fixture/state').profile, '/fixture/state/guardian/browser-profile')

    def test_existing_chart_reused_but_other_or_duplicate_chart_refused(self):
        browser = FixtureBrowser()
        target = browser._new_page(URL)
        self.assertEqual(browser.open(URL), target)
        self.assertEqual(browser.created, [URL])
        for conflict in (URL, OTHER, URL+'?layout=wrong', 'https://tradingview.com/chart/AbCd1234/'):
            other = FixtureBrowser(); other._new_page(URL); other._new_page(conflict)
            with self.assertRaises(MarketBrowserError):
                other.open(URL)
            self.assertEqual(len(other.created), 2)
        other = FixtureBrowser(); other._new_page(OTHER)
        with self.assertRaises(MarketBrowserError):
            other.open(URL)
        self.assertEqual(len(other.created), 1)

    def test_binding_cannot_retarget_or_evaluate_redirected_or_wrong_target(self):
        browser = FixtureBrowser(); target = browser.open(URL)
        self.assertEqual(browser.evaluate(target, 'fixture internal hook'), {'native':'fixture'})
        with self.assertRaises(MarketBrowserError): browser.open(OTHER)
        with self.assertRaises(MarketBrowserError): browser.evaluate('wrong-target', 'fixture')
        browser.pages[0]['url'] = 'https://www.tradingview.com/accounts/signin/'
        with self.assertRaises(MarketBrowserError): browser.evaluate(target, 'fixture')
        for method, args in ((browser._navigate,(target,URL)), (browser.ensure_login,('tradingview',)),
                             (browser.ensure_account_pages,({},))):
            with self.assertRaises(MarketBrowserError): method(*args)


class ProcessOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.browser = MarketBrowser(self.root)
        Path(self.browser.profile).mkdir(parents=True, mode=0o700)
        self.proc = self.root/'proc'; self.process=self.proc/'123'; self.process.mkdir(parents=True)
        (self.process/'fd').mkdir()
        (self.process/'exe').symlink_to('/usr/bin/chromium')
        (self.process/'fd/1').symlink_to('socket:[999]')
        (self.process/'stat').write_text('123 (chrome) '+' '.join(['0']*19+['42']))
        self.args=['/usr/bin/chromium','--remote-debugging-port=9567','--remote-debugging-address=127.0.0.1',
                   '--user-data-dir='+self.browser.profile]
        self.tcp=self.root/'tcp'
        self.tcp.write_text('header\n0: 0100007F:255F 00000000:0000 0A 0 0 0 0 0 999\n')
        original=Path.stat
        def file_stat(path,*args,**kwargs):
            if str(path)=='/usr/bin/chromium':
                return types.SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG|0o755)
            return original(path,*args,**kwargs)
        self.stats=patch.object(Path,'stat',file_stat); self.stats.start(); self.addCleanup(self.stats.stop)
        self.paths=patch.object(guardian_browser,'Path',lambda path: self.proc if str(path)=='/proc' else self.tcp if str(path)=='/proc/net/tcp' else Path(path))
        self.paths.start(); self.addCleanup(self.paths.stop)
        self.system=patch.object(self.browser,'_systemctl',return_value='123'); self.system.start(); self.addCleanup(self.system.stop)

    def owned(self):
        (self.process/'cmdline').write_bytes(b'\0'.join(a.encode() for a in self.args)+b'\0')
        return self.browser._owned()

    def test_positive_pid_profile_socket_and_process_restart_repin(self):
        self.assertEqual(self.owned(),123)
        self.browser.auth_targets['fixture']='old'
        self.assertEqual(self.owned(),123)
        self.assertTrue(self.browser.auth_targets)
        (self.process/'stat').write_text('123 (chrome) '+' '.join(['0']*19+['43']))
        self.owned()
        self.assertEqual(self.browser.auth_targets,{})

    def test_wrong_port_profile_override_and_unsafe_flags_refuse(self):
        original=list(self.args)
        for extra in ('--user-data-dir=/another-profile','--user-data-dir', '--no-sandbox',
                      '--remote-debugging-port=9566','--remote-debugging-address=0.0.0.0',
                      '--remote-allow-origins=*','--disable-web-security'):
            self.args=original+[extra]
            with self.assertRaises(GuardianError): self.owned()

    def test_foreign_listener_and_public_profile_refuse(self):
        self.tcp.write_text('header\n0: 00000000:255F 00000000:0000 0A 0 0 0 0 0 999\n')
        with self.assertRaises(GuardianError): self.owned()
        self.tcp.write_text('header\n0: 0100007F:255F 00000000:0000 0A 0 0 0 0 0 998\n')
        with self.assertRaises(GuardianError): self.owned()
        self.tcp.write_text('header\n0: 0100007F:255F 00000000:0000 0A 0 0 0 0 0 999\n')
        Path(self.browser.profile).chmod(0o755)
        with self.assertRaises(GuardianError): self.owned()


class Launcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('tested_market_launcher',ROOT/'installer/market_browser.py')
        cls.module=importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.module)

    def test_loopback_profile_and_no_feed_or_credentials_in_argv(self):
        args=self.module.browser_command(Path('/home/customer/.local/state/decrypt-os/trading/browser-profile'))
        self.assertIn('--remote-debugging-port=9567',args)
        self.assertIn('--remote-debugging-address=127.0.0.1',args)
        self.assertIn('--class=decrypt-os-market',args)
        self.assertEqual(args[-1],'about:blank')
        self.assertFalse(any('http' in value or '9566' in value or 'no-sandbox' in value for value in args))

    def test_linked_parent_does_not_touch_destination(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            home=Path(temp)/'home'; home.mkdir(mode=0o700)
            outside=Path(temp)/'outside'; outside.mkdir(mode=0o700)
            (home/'.local').symlink_to(outside)
            with self.assertRaises(ValueError): self.module.private_profile(home,os.getuid())
            self.assertEqual(list(outside.iterdir()),[])


if __name__ == '__main__': unittest.main()
