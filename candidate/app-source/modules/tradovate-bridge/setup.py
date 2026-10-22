#!/usr/bin/env python3
"""Decrypt OS Tradovate bridge setup. Run as the desktop user. Stdlib only.

The bridge ships off. Nothing here runs at image build time, and no account name or id is ever
shipped: you give your own.

  setup.py init --account-name NAME --soup-account-id ID [options]
        Creates the bridge folder for login A: config.json, firing.json (no followers yet),
        tokens (0600), logins.json, login-guard.json (weekend hold), the paused switch
        ENTRIES-DISABLED and the Bridge Advanced profile. Never overwrites a file. Starts nothing.
  setup.py enable    Enables and starts login A's browser, daemon, login trigger and guard, the Bridge
                     window and Bridge Advanced (plus firing and every extra login, when configured).
                     New entries stay paused until you press Resume in Bridge Advanced.
  setup.py disable   Stops and disables every bridge unit. It does not cancel orders or close
                     positions, and it keeps every file.
  setup.py status    Prints what is set up and running (JSON). Never prints a token.

All data lives under one root, ~/.local/share/decrypt-os (DECRYPT_OS_DATA), with one override per
part: tvbridge/ (TVBRIDGE_ROOT), tvbridge-admin/, tvbridge-state/ (broker events, login-guard state),
tvbridge-browser/ (the bridge's Chromium profiles) and soup/ (DECRYPT_SOUP_ROOT: the files the Decrypt
app's Soup engine writes for the bridge; soup/current links to its engine folder).

Sign in to Tradovate yourself in the bridge's own window and let Chromium save the login. The login
guard only clicks that saved login (and the Simulation button); it never types or reads it. Chromium
keeps it encrypted with your desktop keyring.
"""
import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

MODULE = Path(__file__).resolve().parent
HOME = Path.home()
DATA = Path(os.path.expanduser(os.environ.get('DECRYPT_OS_DATA') or '~/.local/share/decrypt-os'))
ROOT = Path(os.path.expanduser(os.environ.get('TVBRIDGE_ROOT') or str(DATA / 'tvbridge')))
ADMIN = DATA / 'tvbridge-admin'
PROFILE_DIR = DATA / 'tvbridge-browser' / 'profile'
SOUP_STATE = Path(os.path.expanduser(os.environ.get('DECRYPT_SOUP_ROOT') or str(DATA / 'soup')))
# The Decrypt app's Soup engine code (the firing order imports Soup's own message() from it).
SOUP_ENGINE = Path('/usr/local/lib/decrypt-os/app/trading-runtime/decrypt_trading/vendor/soup_r4')
ADAPTER_DB = DATA / 'tvbridge-state' / 'events.sqlite'
GUARD_STATE = DATA / 'tvbridge-state' / 'login-guard'
SYSTEMCTL = '/usr/bin/systemctl'
TICKER_RE = re.compile(r'^[A-Z0-9]{1,5}[FGHJKMNQUVXZ]20\d\d\Z')
PLACEHOLDER_RE = re.compile(r'FROM-RECON|PAIRING|PLACEHOLDER|CHANGE[-_ ]?ME|EXAMPLE|TODO|XXX|<[^>]*>', re.I)
KEY_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}\Z')
HEDGE_GROUPS = [['ES', 'MES', 'NQ', 'MNQ', 'YM', 'MYM', 'RTY', 'M2K']]

LOGIN_A_UNITS = ('tvbridge-browser.service', 'tvbridge-daemon.service', 'tvbridge-login-trigger.service',
                 'tvbridge-login-guard.timer')
PANEL_UNITS = ('tvbridge-status.service', 'tvbridge-admin.service')
FIRING_UNIT = 'tvbridge-firing.service'


class Refused(Exception):
    pass


def write_new(path, text, mode=0o600):
    """Create a file that must not exist yet. Returns False (and changes nothing) when it does."""
    path = Path(path)
    if os.path.lexists(path):
        return False
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def private_dir(path):
    Path(path).mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def token():
    return secrets.token_urlsafe(32)


def text_value(value, name):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128 or any(ord(c) < 32 for c in value) \
            or PLACEHOLDER_RE.search(value):
        raise Refused(f'{name} must be your own value (1-128 characters, no placeholders)')
    return value.strip()


def leader_key(soup_account_id):
    slug = re.sub(r'[^a-z0-9]+', '-', soup_account_id.lower()).strip('-')[:32].strip('-')
    return slug if KEY_RE.match(slug) and slug != 'main' else 'leader'


def contract_problem(ticker, tick_size, adapter=None):
    """The daemon's own checks (tvbridge.adapters): a known root (MNQ, MES) with its tick, and an
    adapter (Soup when none is named) that is registered and declares that root."""
    sys.path.insert(0, str(MODULE / 'bridge'))
    try:
        from tvbridge import adapters
    finally:
        sys.path.pop(0)
    try:
        adapters.check_contract(ticker, tick_size)
        adapters.binding_adapters(adapter_config(ticker, adapter))
    except adapters.AdapterError as exc:
        return '--ticker/--tick-size/--adapter: ' + str(exc)
    return None


def adapter_config(ticker, adapter):
    """The config keys naming the binding's adapter: none for Soup (the default), else exactly one, enabled.
    An adapter that requires the 1-second feed (tvbridge.marketfeed.REQUIRED_BY, Lumi) also gets it turned on."""
    value = {'ticker': ticker, **({'adapters': {adapter: {'enabled': True}}} if adapter else {})}
    if adapter:
        sys.path.insert(0, str(MODULE / 'bridge'))
        try:
            from tvbridge import marketfeed
        finally:
            sys.path.pop(0)
        if adapter in marketfeed.REQUIRED_BY:
            value['market_feed'] = {'enabled': True}
    return value


def daemon_config(args):
    return {
        'account_name': args.account_name, 'account_id': None, 'soup_account_id': args.soup_account_id,
        'ticker': args.ticker, 'tick_size': args.tick_size,
        'hedge_guard': True, 'hedge_groups': HEDGE_GROUPS,
        'time_in_force': 'Day', 'order_timeout_s': 5, 'max_silence_ms': 10000,
        'kill_switch_file': str(ROOT / 'ENTRIES-DISABLED'), 'journal_dir': str(ROOT / 'journal'),
        'devtools_port': 9555, 'page_url_contains': 'trader.tradovate.com',
        # Simulation only: the login guard picks Simulation and the daemon refuses any other endpoint.
        'trade_url_pattern': '^wss://demo\\.tradovateapi\\.com/v1/websocket',
        'listen_port': 8777, 'publish_mode': args.publish_mode, 'account_view_enabled': True,
        # publish_mode is adapter-db or evidence (below), so no soup_base_url: that is only for the
        # older engine's HTTP "soup" mode, which the in-app runtime does not serve.
        'soup_adapter_db': str(ADAPTER_DB),
        'soup_context_path': str(Path(args.soup_state) / 'broker-context.json'),
        'bracket_orders_enabled': True, 'flatten_all_enabled': True, 'followers': [],
        'read_token_env': 'TVBRIDGE_READ_TOKEN_A', 'peer_logins': [],
        **{k: v for k, v in adapter_config(args.ticker, getattr(args, 'adapter', None)).items() if k != 'ticker'},
    }


def registry():
    return {'schema': 'decrypt.tvbridge-logins.v1', 'logins': [{
        'id': 'a', 'label': 'Login A', 'primary': True, 'config': str(ROOT / 'config.json'),
        'bridge_url': 'http://127.0.0.1:8777', 'token_env': 'TVBRIDGE_TOKEN',
        'read_token_env': 'TVBRIDGE_READ_TOKEN_A', 'devtools_port': 9555,
        'browser_unit': 'tvbridge-browser.service', 'daemon_unit': 'tvbridge-daemon.service',
        'guard_unit': 'tvbridge-login-guard.service', 'trigger_unit': 'tvbridge-login-trigger.service',
        'guard_state_dir': str(GUARD_STATE)}]}


def firing_config(args):
    key = leader_key(args.soup_account_id)
    soup = Path(args.soup_state)
    return {
        # The app's Soup engine database, through the link it keeps at soup/current.
        'soup_db': str(soup / 'current' / 'engine.sqlite'), 'soup_engine_dir': str(Path(args.soup_engine_dir)),
        'soup_quantity_policy': str(soup / 'quantity-policy.json'), 'soup_pairing': str(soup / 'pairing.json'),
        'ticker': args.ticker, 'logins_file': str(ROOT / 'logins.json'), 'bridge_url': 'http://127.0.0.1:8777',
        'firing_dir': str(ROOT / 'firing'), 'firing_mode': 'parallel', 'firing_order': [key],
        'entry_gate': 'main_accepted', 'main_gate_timeout_ms': 5000, 'align_wait_ms': 4000,
        'grid_settle_ms': 2000, 'grid_attention_ms': 30000, 'follower_exits': 'native',
        'follower_native': {'stop_confirm_ms': 5000, 'modify_confirm_ms': 2500, 'cancel_confirm_ms': 5000,
                            'backstop_ms': 2000, 'exit_verify_ms': 5000, 'reduce_confirm_ms': 5000,
                            'max_exit_attempts': 3},
        'accounts': {key: dict({'role': 'main', 'label': args.soup_account_id},
                               **({'prop_profile': args.prop_profile}
                                  if args.prop_profile else {}))},
        'poll_interval_ms': 100, 'max_age_ms': 5000, 'max_send_delay_ms': 10000, 'reconcile_grace_ms': 15000,
        'reconcile_mode': 'two_step', 'reconcile_settle_ms': 2000, 'reconcile_deadline_ms': 60000,
        'reconcile_step_wait_ms': 5000, 'exit_barrier_wait_ms': 2000, 'send_timeout_s': 25,
    }


def admin_profile(args):
    status = 'http://127.0.0.1:8779/api/status'
    return {
        'mode': 'live', 'port': 8781, 'user': getpass.getuser(), 'public_host': args.public_host,
        'tvbridge_root': str(ROOT), 'soup_state': str(Path(args.soup_state)), 'admin_state': str(ADMIN / 'state'),
        'bridge_release': str(MODULE / 'bridge'), 'status_url': status, 'status_ttl_s': 2.0, 'wait_poll_s': 5,
        'restarter': 'systemd',
        'systemd': {'systemctl': [SYSTEMCTL], 'status_api_url': status, 'daemon_wait_s': 60, 'firing_wait_s': 30,
                    'firing_fresh_s': 5, 'status_wait_s': 30, 'poll_s': 1.0, 'command_timeout_s': 180},
        'order_adapter': 'bridge', 'order_bridge': {'timeout_s': 60}, 'strategies': [], 'token_files': True,
        'login_setup': {'unit_dir': str(HOME / '.config/systemd/user'), 'browser_profile_dir': str(PROFILE_DIR),
                        'install_script': str(MODULE / 'bridge/scripts/install.py'), 'python': '/usr/bin/python3',
                        'guard_dir': str(MODULE / 'login-guard'), 'template_dirs': ['/etc/systemd/user']},
    }


def guard_config():
    return {'weekend_hold': {'enabled': True, 'timezone': 'America/New_York',
                             'start': 'Fri 18:00', 'end': 'Sun 18:00'}}


def dump(value):
    return json.dumps(value, indent=2) + '\n'


def cmd_init(args):
    args.account_name = text_value(args.account_name, '--account-name')
    args.soup_account_id = text_value(args.soup_account_id, '--soup-account-id')
    if not TICKER_RE.match(args.ticker):
        raise Refused('--ticker must be a dated futures contract, for example MNQZ2026')
    if not 0 < args.tick_size < 100000:
        raise Refused('--tick-size must be positive')
    problem = contract_problem(args.ticker, args.tick_size, getattr(args, 'adapter', None))
    if problem:
        raise Refused(problem)
    config = ROOT / 'config.json'
    if config.exists():
        old = json.loads(config.read_text())
        if (old.get('account_name'), old.get('soup_account_id')) != (args.account_name, args.soup_account_id):
            raise Refused(f'{config} already names another account. Change accounts in Bridge Advanced.')
    private_dir(ROOT)
    for d in (ROOT / 'journal', ROOT / 'logins', ROOT / 'firing', ADMIN, ADMIN / 'state'):
        private_dir(d)
    done = []

    def add(path, text, mode=0o600):
        if write_new(path, text, mode):
            done.append(str(path))
    # The pause switch first: entries stay off until you resume them in Bridge Advanced. The note marks it as
    # a plain pause (not a safety event), the way Bridge Advanced writes its own.
    add(ROOT / 'ENTRIES-DISABLED', json.dumps({'set_by': 'tvbridge-admin', 'at': time.time(),
                                               'reason': 'New setup: entries start paused'}) + '\n')
    add(config, dump(daemon_config(args)))
    add(ROOT / 'tvbridge.env', '# Login A order token. Mode 0600. Never copy or print.\nTVBRIDGE_TOKEN=%s\n' % token())
    add(ROOT / 'logins/peers.env', '# Read-only login tokens (GET /login and /health only). Mode 0600.\n'
                                   'TVBRIDGE_READ_TOKEN_A=%s\n' % token())
    add(ROOT / 'logins/manual-a.env', '# Flatten now token of login A (POST /manual only). Mode 0600.\n'
                                      'TVBRIDGE_MANUAL_TOKEN_A=%s\n' % token())
    add(ROOT / 'logins.json', dump(registry()))
    add(ROOT / 'login-guard.json', dump(guard_config()))
    add(ADMIN / 'profile.json', dump(admin_profile(args)))
    add(ROOT / 'firing.json', dump(firing_config(args)))
    current = ROOT / 'current'
    if not os.path.lexists(current):
        os.symlink(MODULE / 'bridge', current)
        done.append(str(current))
    private_dir(PROFILE_DIR.parent)
    print(json.dumps({'created': done, 'paused': True,
                      'firing_engine_found': soup_engine_found(),
                      'next': 'setup.py enable, then sign in to Tradovate in the bridge window and choose '
                              'Simulation. Resume entries in Bridge Advanced when you are ready.'}, indent=2))
    return 0


def systemctl(*argv, check=True):
    proc = subprocess.run([SYSTEMCTL, '--user', *argv], capture_output=True, text=True, timeout=120)
    if check and proc.returncode != 0:
        raise Refused(f'systemctl {" ".join(argv)} failed: {proc.stderr.strip()[:300]}')
    return proc


def extra_login_units():
    try:
        logins = json.loads((ROOT / 'logins.json').read_text())['logins']
    except (OSError, ValueError, KeyError, TypeError):
        return []
    units = []
    for lg in logins:
        if isinstance(lg, dict) and not lg.get('primary') and isinstance(lg.get('id'), str):
            lid = lg['id']
            units += [f'tvbridge-browser@{lid}.service', f'tvbridge-daemon@{lid}.service',
                      f'tvbridge-login-trigger@{lid}.service', f'tvbridge-login-guard@{lid}.timer']
    return units


def soup_engine_found():
    """The firing order reads the Soup engine's database through Soup's own engine code.

    True once the engine code is installed and the Decrypt app has linked its Soup engine
    (soup/current and pairing.json, written when the app opens Soup on the bridge route).
    """
    try:
        cfg = json.loads((ROOT / 'firing.json').read_text())
        return ((Path(os.path.expanduser(cfg['soup_engine_dir'])) / 'transport.py').is_file()
                and Path(os.path.expanduser(cfg['soup_db'])).is_file()
                and Path(os.path.expanduser(cfg['soup_pairing'])).is_file())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def all_units():
    units = list(LOGIN_A_UNITS) + extra_login_units() + list(PANEL_UNITS)
    if soup_engine_found():
        units.append(FIRING_UNIT)
    return units


def cmd_enable(args):
    config = ROOT / 'config.json'
    if not config.exists() or not (ROOT / 'tvbridge.env').exists():
        raise Refused('run setup.py init first')
    cfg = json.loads(config.read_text())
    for key in ('account_name', 'soup_account_id'):
        text_value(cfg.get(key), key)
    if not shutil.which('chromium') and not os.path.exists('/usr/bin/chromium'):
        raise Refused('Chromium is not installed')
    systemctl('daemon-reload')
    units = all_units()
    systemctl('enable', '--now', *units)
    print(json.dumps({'enabled': units, 'paused': (ROOT / 'ENTRIES-DISABLED').exists(),
                      'note': 'Sign in to Tradovate in the bridge window and let Chromium save the login. '
                              'New entries stay paused until you press Resume in Bridge Advanced.'}, indent=2))
    return 0


def cmd_disable(args):
    # The read-only Bridge window (tvbridge-status.service) is part of the desktop on workspace 3
    # and stays up so it can show "off" instead of going blank; everything else stops.
    units = [u for u in reversed(all_units()) if u != 'tvbridge-status.service'] + ['tvbridge-login-guard.service', FIRING_UNIT]
    systemctl('disable', '--now', *dict.fromkeys(units), check=False)
    systemctl('reset-failed', *dict.fromkeys(units), check=False)   # a daemon killed mid-reconnect must not linger as "failed"
    print(json.dumps({'disabled': list(dict.fromkeys(units)),
                      'note': 'Stopping the bridge does not cancel orders or close positions. Check Tradovate.'},
                     indent=2))
    return 0


def cmd_status(args):
    units = all_units()
    states = {}
    if os.path.exists(SYSTEMCTL):
        out = systemctl('is-active', *units, check=False).stdout.split()
        states = dict(zip(units, out))
    print(json.dumps({'root': str(ROOT), 'configured': (ROOT / 'config.json').exists(),
                      'paused': (ROOT / 'ENTRIES-DISABLED').exists(), 'firing_engine_found': soup_engine_found(),
                      'units': states}, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('init', help='create the bridge folder for login A (paused, nothing started)')
    p.add_argument('--account-name', required=True, help='your Tradovate account name (Simulation)')
    p.add_argument('--soup-account-id', required=True,
                   help='the account id your Soup engine pairs with: your Tradovate account number, as entered '
                        'when you installed Soup in the Decrypt app')
    p.add_argument('--ticker', default='MNQZ2026')
    p.add_argument('--tick-size', type=float, default=0.25)
    # Retired: there is no size cap (the strategy alert sets the size). Still parsed so older install
    # scripts keep working; the value is ignored and never written to the config.
    p.add_argument('--max-contracts', type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument('--publish-mode', choices=('adapter-db', 'evidence'), default='adapter-db')
    p.add_argument('--prop-profile', choices=('unrestricted', 'cross-account-window'), default=None,
                   help='prop-firm rule profile of the main account (default: none written; the bridge default '
                        'applies)')
    p.add_argument('--soup-state', default=str(SOUP_STATE))
    p.add_argument('--soup-engine-dir', default=str(SOUP_ENGINE),
                   help='Soup engine package the firing order reads with (default %(default)s)')
    p.add_argument('--public-host', default='localhost', help='public webhook host, if you have one')
    p.add_argument('--adapter', default=None,
                   help='a registered strategy adapter id for this binding (default: Soup); see bridge/ENGINES.md')
    for name in ('enable', 'disable', 'status'):
        sub.add_parser(name)
    args = ap.parse_args(argv)
    if sys.platform != 'linux' and args.cmd in ('enable', 'disable'):
        print('the bridge runs only on Linux', file=sys.stderr)
        return 1
    os.umask(0o077)
    try:
        return {'init': cmd_init, 'enable': cmd_enable, 'disable': cmd_disable, 'status': cmd_status}[args.cmd](args)
    except Refused as exc:
        print(f'{args.cmd} refused: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
