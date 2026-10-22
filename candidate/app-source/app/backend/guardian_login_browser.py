"""Customer browser routing and fresh, read-only provider-request evidence.

No browser startup in a check, order request, screenshot or token export.
Provider HTML contracts remain subject to isolated native acceptance.

TradingView only. Tradovate is not handled here: the bridge owns its own sign-in. You sign in once
in the bridge's window and Chromium saves the login (encrypted by the desktop keyring); the bridge's
login guard clicks that saved login when needed. Guardian never types or stores a Tradovate password.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from guardian_auth import GuardianError, provider_id
from guardian_browser import LOGIN_PROGRAM, PROVIDERS, origin


class MarketLoginBrowser:
    """Login-only view of the exact MarketBrowser owned by TradingService."""
    def __init__(self, market):
        self.market = market

    @staticmethod
    def _provider(provider):
        if provider != 'tradingview':
            raise GuardianError('invalid_provider')

    def targets(self):
        return [self.market.login_target()]

    def evaluate(self, target_id, expression):
        return self.market.evaluate_login(target_id, expression)

    def inspect(self, provider):
        self._provider(provider)
        target = self.market.login_target()
        result = self.evaluate(target['id'], LOGIN_PROGRAM + '("tradingview",null)')
        states = {'logged_out', 'authenticated_unverified', 'mfa', 'captcha',
                  'bad_password', 'ambiguous_form', 'untrusted_page', 'untrusted_form'}
        if type(result) is not dict or result.get('state') not in states:
            return {'state': 'ambiguous_form'}
        return {'state': result['state'], 'targetId': target['id']}

    def submit(self, provider, target_id, username, password):
        self._provider(provider)
        args = json.dumps({'username': username, 'password': password})
        result = self.evaluate(target_id, LOGIN_PROGRAM + '("tradingview",' + args + ')')
        states = {'login_submitted', 'logged_out', 'authenticated_unverified', 'mfa',
                  'captcha', 'bad_password', 'ambiguous_form', 'untrusted_page', 'untrusted_form'}
        if type(result) is not dict or result.get('state') not in states:
            return {'state': 'ambiguous_form'}
        return {'state': result['state']}

    def open(self, provider):
        self._provider(provider)
        self.market.focus_login()


class LoginBrowsers:
    def __init__(self, state_dir, clock=time.time):
        self.market = None
        self.market_profile = str(Path(state_dir).absolute() / 'trading/browser-profile')
        self.clock = clock

    def bind_market_browser(self, browser):
        """Constructor hook only, called under Guardian's service mutex."""
        from trading_browser import MarketBrowser
        if (not isinstance(browser, MarketBrowser) or browser.profile != self.market_profile
                or browser.port != 9567 or browser.unit != 'decrypt-os-market-browser.service'):
            raise GuardianError('market_chart_conflict', status=409)
        if self.market is not None and self.market.market is not browser:
            raise GuardianError('market_binding_immutable', status=409)
        self.market = self.market or MarketLoginBrowser(browser)

    def selected(self, provider):
        provider_id(provider)
        if self.market is None:
            raise GuardianError('market_chart_not_configured', status=409)
        return self.market

    def inspect(self, provider):
        return self.selected(provider).inspect(provider)

    def submit(self, provider, target_id, username, password):
        return self.selected(provider).submit(provider, target_id, username, password)

    def open(self, provider):
        return self.selected(provider).open(provider)

    def verify_session(self, provider, native_account_id, config=None):
        browser = self.selected(provider)
        pages = [p for p in browser.targets() if p['type'] == 'page'
                 and origin(p['url']) == PROVIDERS[provider]['origin']]
        identity = {'username': native_account_id}
        if len(pages) != 1:
            return {'valid': False, 'reason': 'page_ambiguous'}
        result = browser.evaluate(pages[0]['id'], REQUEST_PROOF + '(' +
                                  json.dumps(provider) + ',' + json.dumps(identity) + ')')
        if type(result) is not dict or result.get('valid') is not True:
            return {'valid': False, 'reason': 'session_unverified'}
        return {'valid': True, 'identityVerified': True, 'sessionVerified': True,
                'provider': provider, 'nativeAccountId': native_account_id,
                'observedAt': result.get('observedAt')}

    def verify_saved(self, provider, username, config=None):
        if not username:
            return {'valid': False, 'reason': 'account_not_configured'}
        return self.verify_session(provider, username, config)


# This parses ONLY the response from a fresh provider GET, never visible DOM.
# Unknown markup, redirects, account changes, response errors or missing markers
# remain unverified. No request URL or response HTML leaves the page process.
REQUEST_PROOF = r"""(async function(provider, identity) {
  const expected = 'https://www.tradingview.com';
  const fail = () => ({valid:false,reason:'session_unverified'});
  if (window.top !== window.self || location.origin !== expected) return fail();
  const start = location.href;
  const path = '/';
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 6000);
  let reader;
  try {
    const response = await fetch(expected + path, {method:'GET', credentials:'same-origin',
      cache:'no-store', redirect:'error', signal:controller.signal});
    if (response.status !== 200 || response.redirected || response.url !== expected + path ||
        (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase() !== 'text/html') return fail();
    reader = response.body.getReader();
    const decoder = new TextDecoder(); let text = '', bytes = 0;
    for (;;) {
      const chunk = await reader.read(); if (chunk.done) break;
      bytes += chunk.value.length; if (bytes > 1048576) return fail();
      text += decoder.decode(chunk.value, {stream:true});
    }
    text += decoder.decode();
    if (location.href !== start) return fail();
    // Contract is intentionally strict and must be checked against a real
    // customer page in the isolated VM before enabling this adapter.
    const matches = [...text.matchAll(/window\.user\s*=\s*(\{[^;]+\})\s*;/g)];
    if (matches.length !== 1) return fail();
    const user = JSON.parse(matches[0][1]);
    if (user.is_authenticated !== true || typeof user.username !== 'string' ||
        user.username.toLowerCase() !== identity.username.toLowerCase() ||
        !Number.isSafeInteger(user.id) || user.id <= 0) return fail();
    return {valid:true,observedAt:Date.now()};
  } catch { return fail(); }
  finally { clearTimeout(timer); if (reader) await reader.cancel().catch(() => {}); controller.abort(); }
})"""

