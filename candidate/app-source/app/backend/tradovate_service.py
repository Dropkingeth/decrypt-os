"""The Tradovate bridge as the backend sees it: file status, guided setup and a read-only sign-in check.

The bridge is its own module (modules/tradovate-bridge): tvbridge daemons per login, the firing
order, the Bridge window and Bridge Advanced. The Decrypt app sets it up for the user by running the
module's own setup.py ('init', 'enable', 'disable') as a fixed-argument subprocess: no shell, every
value validated here first, and exactly what the terminal commands do (new entries stay paused).
The Soup engine sends its payload to the bridge's loopback /order route and reads the bridge's broker
events (decrypt_trading.bridge_route), exactly as in a standard bridge install.

The sign-in check asks the running daemon's read-only GET /login (loopback, the read token from the
bridge folder) which Tradovate accounts it can see. No token is ever returned to the browser.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

PACKAGE = Path(__file__).resolve().parents[1] / 'trading-runtime'
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))
from decrypt_trading import paths  # noqa: E402

INSTALLED_MODULE = Path('/usr/local/lib/decrypt-os/modules/tradovate-bridge')
SOURCE_MODULE = Path(__file__).resolve().parents[2] / 'modules' / 'tradovate-bridge'
# The same refusals as setup.py's text_value(), plus no leading dash (an argument, not a name).
PLACEHOLDER = re.compile(r'FROM-RECON|PAIRING|PLACEHOLDER|CHANGE[-_ ]?ME|EXAMPLE|TODO|XXX|<[^>]*>', re.I)
ACCOUNT_NUMBER = re.compile(r'[1-9][0-9]{0,14}\Z')
CONTRACT = re.compile(r'MNQ[HMUZ]20[0-9]{2}\Z')
SETUP_TIMEOUT_S = 180
LOGIN_TIMEOUT_S = 3


class BridgeError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


def module_dir():
    override = os.environ.get('DECRYPT_BRIDGE_MODULE')
    if override:
        return Path(os.path.expanduser(override))
    return INSTALLED_MODULE if (INSTALLED_MODULE / 'setup.py').is_file() else SOURCE_MODULE


def bridge_root(root=None):
    return Path(root) if root else paths.tvbridge_root()


def bridge_status(root=None):
    """What is set up, from files only."""
    root = bridge_root(root)
    result = {'configured': False, 'paused': None, 'logins': [], 'firing': False,
              'state': 'not_configured', 'root': str(root)}
    try:
        registry = json.loads((root / 'logins.json').read_text())
        logins = [{'id': lg['id'], 'label': lg.get('label', lg['id']), 'primary': lg.get('primary') is True}
                  for lg in registry['logins'] if isinstance(lg, dict) and isinstance(lg.get('id'), str)]
        configured = (root / 'config.json').is_file()
    except (OSError, ValueError, KeyError, TypeError):
        return result
    result.update(configured=configured, logins=logins, firing=(root / 'firing.json').is_file(),
                  paused=(root / 'ENTRIES-DISABLED').exists(),
                  state='configured' if configured else 'not_configured')
    return result


def _text(value, label):
    if (type(value) is not str or not 1 <= len(value.strip()) <= 128 or value.startswith('-')
            or any(ord(c) < 32 or ord(c) == 127 for c in value) or PLACEHOLDER.search(value)):
        raise BridgeError('invalid_text', f'{label} must be your own value: 1 to 128 printable characters, no placeholder.', 400)
    return value.strip()


def setup_arguments(data):
    """The exact setup.py init arguments for the form's three values, or a refusal."""
    if type(data) is not dict or set(data) != {'accountName', 'accountNumber', 'contract'}:
        raise BridgeError('invalid_fields', 'Send the account name, the account number and the contract only.', 400)
    name = _text(data['accountName'], 'The Tradovate account name')
    number = data['accountNumber']
    if type(number) is not str or not ACCOUNT_NUMBER.match(number):
        raise BridgeError('invalid_account_number', 'The account number is digits only (up to 15).', 400)
    contract = data['contract']
    if type(contract) is not str or not CONTRACT.match(contract):
        raise BridgeError('invalid_contract', 'Use a dated MNQ contract, for example MNQZ2026.', 400)
    return ['init', '--account-name', name, '--soup-account-id', number, '--ticker', contract]


def run_setup(*argv):
    """Run the module's setup.py with fixed arguments. Returns its JSON report."""
    script = module_dir() / 'setup.py'
    if not script.is_file():
        raise BridgeError('bridge_module_missing', 'The Tradovate bridge module is not installed on this system.', 503)
    try:
        proc = subprocess.run([sys.executable, '-I', '-B', str(script), *argv], capture_output=True, text=True,
                              timeout=SETUP_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise BridgeError('bridge_setup_failed', 'The bridge setup did not run. Check the bridge window and try again.', 503) from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        reason = detail[-1] if detail else ''
        if 'only on Linux' in reason:
            raise BridgeError('bridge_linux_only', 'The bridge runs only inside Decrypt OS (Linux).', 409)
        reason = re.sub(r'^\w+ refused: ', '', reason)
        raise BridgeError('bridge_setup_refused', reason[:300] or 'The bridge setup refused this change.', 409)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise BridgeError('bridge_setup_failed', 'The bridge setup gave no readable result. Check its status.', 503) from None


def setup_bridge(data):
    """setup.py init: creates login A for the user's own account, paused. Never overwrites a file."""
    report = run_setup(*setup_arguments(data))
    return {'created': len(report.get('created') or []), 'paused': report.get('paused') is True,
            'firingEngineFound': report.get('firing_engine_found') is True}


def enable_bridge():
    report = run_setup('enable')
    return {'enabled': [u for u in report.get('enabled') or [] if type(u) is str], 'paused': report.get('paused') is True}


def disable_bridge():
    report = run_setup('disable')
    return {'disabled': [u for u in report.get('disabled') or [] if type(u) is str]}


def _env_value(path, key):
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith(key + '='):
                return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return None


def _daemon_login(port, token):
    """GET /login/<read token> on the loopback daemon. None when it does not answer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{int(port)}/login/{token}', timeout=LOGIN_TIMEOUT_S) as response:
            if response.status != 200:
                return None
            reply = json.loads(response.read(262144).decode('utf-8'))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return reply if isinstance(reply, dict) else None


GUARD_DETAIL = {
    'captcha': 'Tradovate shows a captcha. Solve it in the Bridge window (Super+3).',
    'second_factor': 'Tradovate asks for a second factor. Enter it in the Bridge window (Super+3).',
    'saved_login_not_filled': 'No saved login. Sign in once in the Bridge window and let the browser save it.',
    'simulation_button_not_unique': 'Choose Access Simulation in the Bridge window (Super+3).',
    'daemon_start_cap_reached': 'The bridge restarted too often this hour. It will try again later.',
}


def login_status(root=None, state_root=None):
    """Read-only: what the bridge sees after you sign in. No token leaves this function."""
    root = bridge_root(root)
    state_root = Path(state_root) if state_root else paths.bridge_state_root()
    out = {'configured': False, 'account': None, 'daemon': 'unreachable', 'signIn': 'unknown', 'detail': '',
           'accounts': [], 'nameMatch': None, 'numberMatch': None, 'guard': None}
    try:
        cfg = json.loads((root / 'config.json').read_text())
        name, number = cfg['account_name'], cfg['soup_account_id']
        if type(name) is not str or type(number) is not str:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        out['detail'] = 'The bridge is not set up yet.'
        return out
    out.update(configured=True, account={'name': name, 'number': number, 'contract': cfg.get('ticker')})
    try:
        guard = json.loads((state_root / 'login-guard' / 'status.json').read_text())
        if isinstance(guard, dict):
            out['guard'] = {'state': guard.get('state'), 'reason': guard.get('reason')}
    except (OSError, ValueError):
        pass
    token = _env_value(root / 'logins' / 'peers.env', 'TVBRIDGE_READ_TOKEN_A')
    reply = _daemon_login(cfg.get('listen_port', 8777), token) if token and len(token) >= 24 else None
    guard_state = (out['guard'] or {}).get('state')
    if reply is None:
        out['detail'] = ('Needs you: ' + GUARD_DETAIL.get((out['guard'] or {}).get('reason'), 'finish the sign-in in the Bridge window (Super+3).')
                         if guard_state == 'needs_human' else 'The bridge is not running, or not answering yet. Turn it on, then check again.')
        out['signIn'] = 'needs_human' if guard_state == 'needs_human' else 'unknown'
        return out
    out['daemon'] = 'reachable'
    page = reply.get('page') if isinstance(reply.get('page'), dict) else {}
    accounts = [{'id': a['id'], 'name': str(a.get('name') or '')[:80], 'configured': a.get('configured') is True}
                for a in (reply.get('accounts') or []) if isinstance(a, dict) and type(a.get('id')) is int]
    out['accounts'] = accounts
    if accounts:
        out['nameMatch'] = any(a['name'] == name for a in accounts)
        out['numberMatch'] = any(a['name'] == name and str(a['id']) == number for a in accounts)
    if page.get('logged_in') is True and page.get('state') == 'authorized':
        out['signIn'] = 'connected'
        out['detail'] = ('Signed in. The bridge sees %d account%s.' % (len(accounts), '' if len(accounts) == 1 else 's')
                         if accounts else 'Signed in. No account is visible yet.')
    elif guard_state == 'needs_human':
        out['signIn'] = 'needs_human'
        out['detail'] = 'Needs you: ' + GUARD_DETAIL.get(out['guard'].get('reason'), 'finish the sign-in in the Bridge window (Super+3).')
    else:
        out['signIn'] = 'logged_out'
        out['detail'] = 'Not signed in yet. Sign in to Tradovate in the Bridge window (Super+3) and choose Access Simulation.'
    return out
