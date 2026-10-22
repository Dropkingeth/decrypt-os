#!/usr/bin/python3
"""Launch only the customer's dedicated market browser. No credentials."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import socket
import stat
import sys

PORT = 9567
BROWSER = '/usr/bin/chromium'


def require(value):
    if not value:
        raise ValueError('market browser prerequisites are not ready.')


def private_profile(home, uid):
    home = Path(home)
    require(home.is_absolute() and home != Path('/'))
    target = home / '.local/state/decrypt-os/trading/browser-profile'
    for part in reversed((target, *target.parents)):
        if not part.exists() and not part.is_symlink():
            part.mkdir(mode=0o700)
        metadata = part.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and not part.is_symlink())
        require(metadata.st_uid in (0, uid) and not metadata.st_mode & 0o022)
    metadata = target.lstat()
    require(metadata.st_uid == uid and stat.S_IMODE(metadata.st_mode) == 0o700)
    return target


def browser_command(profile):
    return [BROWSER, '--ozone-platform=wayland', '--no-first-run',
            '--no-default-browser-check', '--disable-session-crashed-bubble',
            '--disable-sync', '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=' + str(PORT),
            '--user-data-dir=' + str(profile), '--class=decrypt-os-market',
            'about:blank']


def main():
    try:
        uid = os.getuid()
        require(sys.platform == 'linux' and uid >= 1000 and os.geteuid() == uid)
        for key in ('WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
            value = os.environ.get(key, '')
            require(0 < len(value) <= 4096 and not any(ord(c) < 32 for c in value))
        require(os.access(BROWSER, os.X_OK))
        os.umask(0o077)
        profile = private_profile(pwd.getpwuid(uid).pw_dir, uid)
        # Never ask an unrelated browser to accept our profile or debugging port.
        with socket.socket() as probe:
            probe.settimeout(1)
            require(probe.connect_ex(('127.0.0.1', PORT)) != 0)
        command = browser_command(profile)
        # Preserve systemd MainPID. No login values or provider URLs enter argv.
        os.execv(command[0], command)
    except (OSError, ValueError):
        print('market browser could not start. Check the local desktop and dedicated browser port.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
