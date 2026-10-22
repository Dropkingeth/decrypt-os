#!/usr/bin/env python3
"""Customer-only Chromium app windows. No credentials or account setup."""
import argparse
import contextlib
import fcntl
import json
from http.client import HTTPConnection, HTTPException
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import stat
import subprocess
import sys
import time

# Persistent numbered workspaces:
#   WS1 Operations (this app's own home/dashboard window)
#   WS2 Trading (the pipeline view and the user's own TradingView chart)
#   WS3 Bridge (the Bridge window on :8779; login A's Tradovate window joins it)
#   WS4 ICT assistant (its own terminal launcher, decrypt-ict-assistant)
#   WS5 Bridge Advanced, WS6-9 extra bridge logins: the bridge module's own launcher and
#       Hyprland rules (modules/tradovate-bridge/desktop), not this file
APPS = {
    'decrypt': {'class': 'decrypt-os', 'url': 'http://127.0.0.1:8811', 'workspace': 1},
    'trading-pipeline': {'class': 'decrypt-trading-pipeline', 'url': 'http://127.0.0.1:8811/pipeline', 'workspace': 2},
    'tradingview': {'class': 'decrypt-tradingview-chart', 'url': 'https://www.tradingview.com/chart/', 'workspace': 2},
    # The Bridge window (read-only status of every login) sits on workspace 3 with login A's Tradovate window.
    # Login browsers and Bridge Advanced (WS5) are placed by the bridge module's own launchers/Hyprland rules.
    'tradovate-bridge': {'class': 'decrypt-tradovate-status', 'url': 'http://127.0.0.1:8779/bridge', 'workspace': 3},
}
# Chromium on Wayland ignores --class for --app windows and derives the window
# class from the URL (host + path). The desktop's Hyprland rules use the same names.
WAYLAND_CLASSES = {
    'decrypt': 'chrome-127.0.0.1__-Default',
    'trading-pipeline': 'chrome-127.0.0.1__pipeline-Default',
    'tradingview': 'chrome-www.tradingview.com__chart_-Default',
    'tradovate-bridge': 'chrome-127.0.0.1__bridge-Default',
}
APP_SELECTORS = tuple(APPS)
ENV_KEYS = ('WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE', 'XDG_RUNTIME_DIR',
            'DBUS_SESSION_BUS_ADDRESS', 'XDG_CURRENT_DESKTOP')


def resolve_app(app):
    """A single config dict carries everything place_window/windows/open_window
    need, computed once, in one place. (bridge-login used to need a second,
    login_index-dependent workspace computed here too; the bridge module
    ships its own launcher and Hyprland rules for every login window now,
    so this app list and resolver only ever
    cover the four Chromium apps still opened from here.)"""
    require(app in APPS, 'Unknown application.')
    config = dict(APPS[app])
    config['profile_name'] = app
    return config


def require(condition, message):
    if not condition:
        raise ValueError(message)


def platform_choice(system, machine):
    require(system == 'Linux' and machine in ('aarch64', 'x86_64'),
            'This candidate supports only the Linux ARM64 and x86_64 customer desktops.')
    return {'mode': 'web-app', 'label': 'TradingView (web app)', 'nativeDesktopInstalled': False}


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=20, **kwargs)


def verified_login_session():
    """Match the inherited ID to this caller's actual graphical logind session."""
    session = os.environ.get('XDG_SESSION_ID', '')
    if re.fullmatch('[A-Za-z0-9_-]{1,64}', session) is None:
        return None
    try:
        result = subprocess.run(['/usr/bin/loginctl', 'show-session', 'self',
                                 '--property=Id,User,Type,State,TimestampMonotonic'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=3, check=False)
        if result.returncode or len(result.stdout) > 4096:
            return None
        pairs = [line.split('=', 1) for line in result.stdout.decode('ascii').splitlines()]
        fields = dict(pairs)
        if (len(pairs) != len(fields) or set(fields) != {'Id', 'User', 'Type', 'State', 'TimestampMonotonic'}
                or fields.get('Id') != session or fields.get('User') != str(os.getuid())
                or fields.get('Type') not in ('wayland', 'x11') or fields.get('State') != 'active'
                or not fields.get('TimestampMonotonic', '').isdigit()
                or int(fields['TimestampMonotonic']) <= 0):
            return None
        return session
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return None


def import_desktop_environment():
    uid = os.getuid()
    require(uid >= 1000 and os.geteuid() == uid, 'Open apps as your local desktop account.')
    session = verified_login_session()
    # Clear a previous login before validating other desktop fields. A missing
    # Wayland/DBus field must not leave the manager's old login ID in place.
    os.environ.pop('XDG_SESSION_ID', None)
    run(['/usr/bin/systemctl', '--user', 'unset-environment', 'XDG_SESSION_ID'])
    run(['/usr/bin/dbus-update-activation-environment', '--systemd', 'XDG_SESSION_ID='])
    run(['/usr/bin/systemctl', '--user', 'unset-environment', 'XDG_SESSION_ID'])
    for key in ENV_KEYS:
        value = os.environ.get(key)
        require(value and len(value) <= 4096 and not any(ord(c) < 32 or ord(c) == 127 for c in value),
                'Open this app from the Omarchy desktop.')
    if session is None:
        keys = ENV_KEYS
    else:
        os.environ['XDG_SESSION_ID'] = session
        keys = (*ENV_KEYS, 'XDG_SESSION_ID')
    run(['/usr/bin/systemctl', '--user', 'import-environment', *keys])
    run(['/usr/bin/dbus-update-activation-environment', '--systemd', *keys])
    return session is not None


def start_desktop_session():
    # Adopts this login's session ID for login-keyring acceptance. Omarchy's own
    # autostart starts the shell and imports the rest of the environment.
    uid = os.getuid()
    require(uid >= 1000 and os.geteuid() == uid, 'Start the desktop as its local user.')
    adopted = False
    try:
        adopted = import_desktop_environment()
    except (OSError, ValueError, subprocess.SubprocessError):
        os.environ.pop('XDG_SESSION_ID', None)
        print('Desktop environment adoption failed; login-keyring acceptance is unavailable.', file=sys.stderr)
    if not adopted:
        print('Graphical login identity is unavailable; it is not evidence of a new login.', file=sys.stderr)
    return 0 if adopted else 1


def query(name):
    result = run(['/usr/bin/hyprctl', '-j', name], capture_output=True, text=True)
    require(len(result.stdout) <= 4 * 1024 * 1024, 'Desktop response exceeded its size limit.')
    return json.loads(result.stdout)


def private_directory(path, uid):
    path = Path(path)
    for parent in reversed((path, *path.parents)):
        if not parent.exists() and not parent.is_symlink():
            parent.mkdir(mode=0o700)
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and not parent.is_symlink(), 'App profile paths must be real directories.')
        require(info.st_uid in (0, uid) and not info.st_mode & 0o022, 'App profile path is writable by another account.')
    info = path.lstat()
    require(info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o700, 'The dedicated app profile must be private to this account.')
    return path


def profile_path(config, home, uid):
    # Never consult an inherited browser profile or an owner/developer home.
    return private_directory(Path(home) / '.local/share/decrypt-browser' / config['profile_name'], uid)


@contextlib.contextmanager
def launch_lock(profile):
    fd = os.open(profile / '.decrypt-launch.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1,
                'Unsafe application launch lock.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('This app is already opening. Wait for its window.')
        yield
    finally:
        os.close(fd)


def browser_command(config, profile):
    return ['/usr/bin/chromium', '--ozone-platform=wayland', '--no-first-run', '--no-default-browser-check',
            '--disable-session-crashed-bubble', '--disable-sync', '--user-data-dir=' + str(profile),
            '--class=' + config['class'], '--app=' + config['url']]


def owns_process(pid, profile, config):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        location = Path('/proc') / str(pid)
        if location.stat().st_uid != os.getuid():
            return False
        if (location / 'exe').resolve() != Path('/usr/lib/chromium/chromium'):
            return False
        raw = (location / 'cmdline').read_bytes()
        if not raw or len(raw) > 65536 or not raw.endswith(b'\0'):
            return False
        args = raw[:-1].split(b'\0')
        # Chromium can rewrite argv into one display string on Linux.
        if len(args) == 1:
            args = [s.encode() for s in shlex.split(args[0].decode())]
        return bool([a for a in args if a.startswith(b'--user-data-dir=')]
                    == [('--user-data-dir=' + str(profile)).encode()]
                    and [a for a in args if a.startswith(b'--app=')]
                    == [('--app=' + config['url']).encode()])
    except (OSError, ValueError, UnicodeError):
        return False


def windows(app, profile, config):
    clients = query('clients')
    require(isinstance(clients, list), 'Desktop window list is invalid.')
    result = []
    for client in clients:
        if (isinstance(client, dict) and client.get('class') in (config['class'], WAYLAND_CLASSES[app])
                and client.get('mapped') is True and owns_process(client.get('pid'), profile, config)):
            require(re.fullmatch(r'0x[0-9a-fA-F]{1,32}', client.get('address', '')), 'Invalid app window address.')
            result.append(client)
    require(len(result) <= 1, 'More than one dedicated app window is open. Close extras, then reopen the app.')
    return result


def place_window(app, profile, window, config):
    address = window['address']
    workspace = config['workspace']
    # Exact selectors avoid moving whatever window happens to have keyboard focus.
    selector = 'address:' + address
    run(['/usr/bin/hyprctl', 'dispatch',
         f'hl.dsp.window.move({{workspace="{workspace}",window="{selector}",follow=true}})'])
    run(['/usr/bin/hyprctl', 'dispatch', f'hl.dsp.focus({{window="{selector}"}})'])
    placed = windows(app, profile, config)
    active = query('activewindow')
    current = query('activeworkspace')
    require(len(placed) == 1 and placed[0]['address'] == address and
            placed[0].get('workspace', {}).get('id') == workspace and
            active.get('address') == address and current.get('id') == workspace,
            'App window placement was not confirmed. No additional window was opened.')


def open_window(app, profile, config):
    existing = windows(app, profile, config)
    if existing:
        place_window(app, profile, existing[0], config)
        return {'reusedWindow': True, 'workspaceVerified': True}
    # A separate user-data-dir prevents Chromium's ordinary browser from taking this URL.
    process = subprocess.Popen(browser_command(config, profile), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        existing = windows(app, profile, config)
        if existing:
            place_window(app, profile, existing[0], config)
            return {'reusedWindow': False, 'workspaceVerified': True}
        require(process.poll() in (None, 0), 'The app browser failed to start.')
        time.sleep(0.2)
    raise ValueError('No dedicated app window was confirmed. Check the desktop before trying again.')


def service_ready():
    # Fixed loopback endpoint; ignore proxy environment and never follow redirects.
    connection = HTTPConnection('127.0.0.1', 8811, timeout=1)
    try:
        # Answers before the first-start notice is accepted (/api/state does not),
        # so the window can open and show that notice.
        connection.request('GET', '/api/first-start/status')
        response = connection.getresponse()
        raw = response.read(1048577)
        return response.status == 200 and len(raw) <= 1048576 and isinstance(json.loads(raw), dict)
    except (OSError, ValueError, HTTPException):
        return False
    finally:
        connection.close()


def wait_for_service():
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if service_ready():
            return
        time.sleep(0.2)
    raise ValueError('Decrypt is not ready yet. Reopen Decrypt OS to retry; if this continues, check Setup.')


def wait_for_bridge_status():
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        connection = HTTPConnection('127.0.0.1', 8779, timeout=1)
        try:
            connection.request('GET', '/bridge')
            response = connection.getresponse()
            raw = response.read(1048577)
            if (response.status == 200 and len(raw) <= 1048576
                    and response.getheader('Content-Type', '').split(';')[0].strip().lower() == 'text/html'):
                return
        except (OSError, ValueError, HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.2)
    raise ValueError('Tradovate bridge status is unavailable. No trading service was started.')


def show_error(app, message):
    print(message, file=sys.stderr)
    # Foot is already an explicit desktop prerequisite. Fixed argv, no shell or sudo.
    # Keep the explanation visible because desktop entries run with Terminal=false.
    try:
        subprocess.Popen(['/usr/bin/foot', '--title=Decrypt app could not open', '/usr/bin/python3', '-I', '-c',
                          "import sys; print(sys.argv[1]); input('Press Return to close. ')",
                          ('TradingView: ' if app == 'tradingview' else 'Decrypt OS: ') + message[:1000]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass  # The original error remains on stderr if no graphical terminal can open.


def ensure_backend_ready(app):
    """Start (if needed) and wait for the service a window's app depends on.
    Shared by the explicit launcher (main()) and the silent restore timer, so
    both ways of opening a window wait on the same readiness check."""
    if app == 'decrypt':
        run(['/usr/bin/systemctl', '--user', 'start', 'decrypt-os-ui.service'])
        wait_for_service()
    elif app == 'tradovate-bridge':
        run(['/usr/bin/systemctl', '--user', 'start', 'tvbridge-status.service'])
        wait_for_bridge_status()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', choices=APP_SELECTORS, nargs='?')
    parser.add_argument('--session-start', action='store_true')
    args = parser.parse_args(argv)
    if args.session_start == (args.app is not None):
        parser.error('Choose one app or --session-start.')
    try:
        platform_choice(platform.system(), platform.machine())
        if args.session_start:
            return start_desktop_session()
        uid = os.getuid()
        import_desktop_environment()
        app = args.app
        config = resolve_app(app)
        profile = profile_path(config, pwd.getpwuid(uid).pw_dir, uid)
        with launch_lock(profile):
            ensure_backend_ready(app)
            open_window(app, profile, config)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        show_error(args.app, str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
