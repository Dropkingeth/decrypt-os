"""Deterministic recovery tests: fake clock/browser; no provider traffic."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend'))
from guardian_auth import GuardianError
from guardian_service import GuardianService

PASSPHRASE = 'fixture-vault-passphrase-ONLY'


class Clock:
    now = 1_800_000_000
    def __call__(self): return self.now


class Browser:
    def __init__(self):
        self.state = 'logged_out'
        self.submissions = []
        self.reservation = None
        self.raises = False
        self.login_pages = []
        self.verified = {}

    def inspect(self, provider): return {'state': self.state, 'targetId': provider + '-fixture'}

    def submit(self, provider, target_id, username, password):
        self.submissions.append((provider, target_id, username, password))
        if self.reservation: self.reservation()
        if self.raises: raise ValueError('fixture-login-password-ONLY')
        return {'state': 'login_submitted'}

    def open(self, provider): self.login_pages.append(provider)
    def ensure_login(self, provider): self.login_pages.append(provider)

    def verify_saved(self, provider, username, config=None):
        return dict(self.verified.get(provider, {'valid': False}))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.clock, self.browser = Clock(), Browser()
        self.service = GuardianService(self.directory, browser=self.browser, clock=self.clock)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def enable(self, provider='tradingview'):
        self.service.action('initialize', {'passphrase': PASSPHRASE})
        self.service.action('save-provider', {'provider': provider, 'username': 'fixture-user', 'password': 'fixture-login-password-ONLY', 'autoLogin': True})

    def test_opt_in_and_manual_browser_without_vault(self):
        self.service.check()
        self.assertEqual(self.browser.submissions, [])
        self.service.action('open-browser', {'provider': 'tradingview'})
        self.assertEqual(self.browser.login_pages, ['tradingview'])
        self.service.check()
        self.assertEqual(self.browser.submissions, [])

    def test_reservation_before_submission_repeated_checks_and_daily_limit(self):
        self.enable()
        self.browser.reservation = lambda: self.assertEqual(len(self.service.files.read('recovery.json')['tradingview']['attempts']), len(self.browser.submissions))
        for _ in range(10): self.service.check()
        self.assertEqual(len(self.browser.submissions), 1)
        self.clock.now += 61
        self.service.check()
        self.assertEqual(len(self.browser.submissions), 2)
        self.clock.now += 301
        self.service.check()
        self.assertEqual(len(self.browser.submissions), 3)
        self.clock.now += 901
        self.service.check()
        self.assertEqual(len(self.browser.submissions), 3)
        self.assertEqual(self.service.status()['providers'][0]['reason'], 'attempt_limit')

    def test_restart_locks_preserves_cooldown_and_no_reset_on_edit(self):
        self.enable(); self.service.check(); self.service.close()
        self.service = GuardianService(self.directory, browser=self.browser, clock=self.clock)
        self.service.check()
        self.assertEqual(len(self.browser.submissions), 1)
        self.assertFalse(self.service.status()['vault']['unlocked'])
        self.service.action('unlock', {'passphrase': PASSPHRASE})
        self.service.action('save-provider', {'provider': 'tradingview', 'username': 'fixture-updated', 'password': 'fixture-new-password', 'autoLogin': True})
        self.clock.now += 6
        self.service.check()
        self.assertEqual(len(self.browser.submissions), 1)
        self.assertEqual(self.service.status()['providers'][0]['state'], 'cooldown')

    def test_unknown_submission_pauses_and_does_not_expose_exception(self):
        self.enable(); self.browser.raises = True; self.service.check()
        self.clock.now += 1000; self.service.check()
        self.assertEqual(len(self.browser.submissions), 1)
        status = self.service.status()
        self.assertEqual(status['providers'][0]['reason'], 'submission_unknown')
        self.assertNotIn('fixture-login-password-ONLY', json.dumps(status))

    def test_mfa_captcha_bad_password_and_ambiguous_pause_without_submit(self):
        self.enable()
        for state in ('mfa', 'captcha', 'bad_password', 'ambiguous_form', 'untrusted_page', 'untrusted_form'):
            self.browser.state = state
            self.clock.now += 6  # one page inspection per provider every 5 s
            self.service.check()
            self.assertEqual(self.service.status()['providers'][0]['reason'], state)
            self.assertEqual(self.browser.submissions, [])

    def test_concurrent_poll_single_submission(self):
        self.enable()
        threads = [threading.Thread(target=self.service.check) for _ in range(10)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(len(self.browser.submissions), 1)
        self.assertEqual(self.browser.submissions[0][3], 'fixture-login-password-ONLY')

    def test_tradovate_is_not_a_guardian_provider(self):
        self.service.action('initialize', {'passphrase': PASSPHRASE})
        for provider in ('tradovate', 'tradovate:login-1'):
            with self.assertRaises(GuardianError):
                self.service.action('save-provider', {'provider': provider, 'username': 'u', 'password': 'p', 'autoLogin': True})
        self.assertEqual([row['id'] for row in self.service.status()['providers']], ['tradingview'])

    def test_independent_tradingview_opt_in(self):
        self.service.action('initialize', {'passphrase': PASSPHRASE})
        self.service.action('save-provider', {'provider': 'tradingview', 'username': 'fixture-chart-user', 'password': 'fixture-chart-password', 'autoLogin': True})
        self.service.action('open-browser', {'provider': 'tradingview'})
        self.service.check(); self.service.check()
        self.assertEqual(len(self.browser.submissions), 1)
        self.assertEqual(self.browser.submissions[0][0], 'tradingview')

    def test_unverified_loading_page_cannot_clear_challenge_pause(self):
        self.enable(); self.browser.state = 'bad_password'; self.service.check()
        self.browser.state = 'authenticated_unverified'
        self.clock.now += 6
        self.service.check()
        self.assertEqual(self.service.retry['tradingview']['pause'], 'bad_password')
        self.browser.state = 'logged_out'; self.clock.now += 6; self.service.check()
        self.assertEqual(self.browser.submissions, [])
        self.browser.state = 'authenticated_unverified'
        self.clock.now += 6
        self.browser.verified['tradingview'] = {'valid': True, 'observedAt': self.clock() * 1000}
        self.service.check()
        # A verified session shows as signed in, but only the explicit resume action clears a challenge pause.
        self.assertEqual(self.service.status()['providers'][0]['state'], 'authenticated')
        self.assertEqual(self.service.retry['tradingview']['pause'], 'bad_password')
        self.assertEqual(self.browser.submissions, [])


if __name__ == '__main__': unittest.main()
