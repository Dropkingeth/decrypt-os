"""Strategy access: send a TradingView username to Decrypt Labs.

Decrypt Labs grants its invite-only TradingView strategies to a TradingView
username. This is the only gated part of Decrypt OS; engines install without it.

The request carries only what the user typed: the username and, if given, a
contact. No machine ID, account data or other system detail is sent. The
endpoint comes from a config file. When it is empty or the send fails, the app
shows the username so the user can send it to Decrypt Labs themselves.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = APP_ROOT / 'strategy-access.json'
USER_CONFIG = Path.home() / '.config/decrypt-os/strategy-access.json'
PUBLIC_SITE = 'https://decryptlabs.io'
USERNAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
STRATEGY_ID = re.compile(r'[a-z0-9-]{1,40}')
CONTACT_LIMIT = 200
RESEND_SECONDS = 600
SENDS_PER_HOUR = 5
STRATEGIES_LIMIT = 10
USER_AGENT = 'DecryptOS (+https://decryptlabs.io)'


class ConfigError(ValueError):
    pass


class SendError(Exception):
    """Reason code only. Never contains the username, contact or response body."""


def _https(value, field):
    if value == '':
        return ''
    try:
        parts = urlsplit(value)
        parts.port
    except (ValueError, TypeError, AttributeError):
        raise ConfigError(field + ' must be an https URL') from None
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(ord(c) < 33 for c in value)):
        raise ConfigError(field + ' must be an https URL')
    return value


def load_config(path=None):
    """Read the endpoint config. A user file overrides the shipped default.

    strategyFieldSupported (default False) is the live-rollout switch: the
    public sign-up service at requestUrl currently rejects any JSON key other
    than tradingViewUsername/contact, so a picked strategy list is never put
    on the wire while this is False. Once Decrypt Labs deploys the service
    change that accepts a `strategies` array, flipping this to True is the
    only step needed here -- no other client change.
    """
    if path is None:
        path = USER_CONFIG if USER_CONFIG.is_file() else DEFAULT_CONFIG
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ConfigError('strategy access config is unreadable') from None
    if (not isinstance(raw, dict) or not set(raw) <= {'requestUrl', 'publicSite', 'strategyFieldSupported'}
            or not isinstance(raw.get('requestUrl', ''), str)):
        raise ConfigError('strategy access config needs only requestUrl, publicSite and strategyFieldSupported')
    site = raw.get('publicSite', PUBLIC_SITE)
    if not isinstance(site, str) or not _https(site, 'publicSite'):
        raise ConfigError('publicSite must be an https URL')
    supported = raw.get('strategyFieldSupported', False)
    if type(supported) is not bool:
        raise ConfigError('strategyFieldSupported must be true or false')
    return {'requestUrl': _https(raw.get('requestUrl', ''), 'requestUrl'), 'publicSite': site,
            'strategyFieldSupported': supported}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SendError('redirect_refused')


class HttpSender:
    """POST {tradingViewUsername[, contact][, strategies]} as JSON to one fixed https URL.

    The live service currently rejects any JSON key other than
    tradingViewUsername/contact, so `strategies` is only ever included when
    strategies_supported is True (see load_config's strategyFieldSupported).
    While False, a picked strategy list stays local to this app (shown back
    to the user) and is never put on the wire.
    """

    def __init__(self, url, timeout=10, strategies_supported=False):
        self.url = url
        self.timeout = timeout
        self.strategies_supported = strategies_supported
        self._opener = build_opener(_NoRedirect())

    @property
    def configured(self):
        return bool(self.url)

    def send(self, username, contact=None, strategies=None):
        if not self.url:
            raise SendError('not_configured')
        body = {'tradingViewUsername': username}
        if contact:
            body['contact'] = contact
        if strategies and self.strategies_supported:
            body['strategies'] = list(strategies)
        request = Request(self.url, data=json.dumps(body).encode(), method='POST',
                          headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                                   'User-Agent': USER_AGENT})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status not in (200, 201, 202):
                    raise SendError('unexpected_response')
        except SendError:
            raise
        except HTTPError as error:
            code = error.code
            error.close()
            raise SendError('rate_limited' if code == 429 else 'refused' if 400 <= code < 500 else 'unavailable') from None
        except (URLError, OSError, ValueError):
            raise SendError('unavailable') from None


def clean_username(value):
    if not isinstance(value, str) or not USERNAME.fullmatch(value.strip()):
        return None
    return value.strip()


def clean_contact(value):
    if value is None or value == '':
        return ''
    if (not isinstance(value, str) or len(value.strip()) > CONTACT_LIMIT
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return None
    return value.strip()


def clean_strategies(value, allowed):
    """Validate the user's picked strategy ids against the requestable set
    from the catalog (`allowed`). None/absent means none picked (the plain
    username-only request). Dedupes while keeping the first-seen order; caps
    at STRATEGIES_LIMIT; any id outside `allowed` fails the whole list."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > STRATEGIES_LIMIT:
        return None
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not STRATEGY_ID.fullmatch(item) or item not in allowed:
            return None
        if item not in cleaned:
            cleaned.append(item)
    return cleaned
