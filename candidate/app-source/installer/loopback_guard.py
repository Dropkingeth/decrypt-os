#!/usr/bin/python3
"""Per-UID loopback firewall for trading, bridge and browser-debugging ports.

Every Decrypt listener binds 127.0.0.1, so loopback is the only barrier between
local users. This nftables table rejects TCP connections to those ports from:

- systemd DynamicUser services (UID/GID 61184-65519): the temporary assistant
  sandbox and the HTTPS ingress (Caddy);
- the assistant's static account (decrypt-os-customer) once it exists.

Protected ports:
  8777-8782, 8784-8799  bridge daemons, status window (8779), Bridge Advanced (8781), Soup
  8810-8811             Decrypt OS app API and UI
  9555-9599             Chromium DevTools: Tradovate logins, login guard, Guardian, market
Left open on purpose:
  8783   the assistant's own web reader proxy
  19789  the assistant's own OpenClaw Gateway
  8812   the signal ingress that Caddy forwards to. Caddy and the temporary
         sandbox share the DynamicUser range, so the sandbox can reach it too;
         it still needs the secret signal URL. The static account cannot.

The desktop user's own processes are not filtered. Run as root:
  loopback_guard.py apply | check | render
"""
from __future__ import annotations
import os
import pwd
import subprocess
import sys

TABLE = 'decrypt_os_loopback'
UNIT_NAME = 'decrypt-os-loopback-guard.service'
STATIC_USER = 'decrypt-os-customer'
DYNAMIC = (61184, 65519)
PROTECTED = ((8777, 8782), (8784, 8799), (8810, 8811), (9555, 9599))
INGRESS_PORT = 8812
NFT = '/usr/bin/nft'


def port_set(ranges):
    return '{ ' + ', '.join(f'{a}-{b}' if a != b else str(a) for a, b in ranges) + ' }'


def static_uid():
    """UID of the assistant's static account, if created (vault setup creates it)."""
    try:
        account = pwd.getpwnam(STATIC_USER)
    except KeyError:
        return None
    uid = account.pw_uid
    if not 0 < uid < 1000 or account.pw_gid == 0:
        raise ValueError('The assistant account is not a system account.')
    return uid


def render(static=None):
    if static is not None and (type(static) is not int or not 0 < static < 1000):
        raise ValueError('Invalid static assistant UID.')
    dynamic = f'{DYNAMIC[0]}-{DYNAMIC[1]}'
    ports = port_set(PROTECTED)
    rules = [f'meta skuid {dynamic} tcp dport {ports} reject with tcp reset',
             f'meta skgid {dynamic} tcp dport {ports} reject with tcp reset']
    if static is not None:
        rules.append(f'meta skuid {static} tcp dport {port_set(PROTECTED + ((INGRESS_PORT, INGRESS_PORT),))} reject with tcp reset')
    body = '\n'.join('    ' + rule for rule in rules)
    # Declare, delete and recreate in one transaction: reloads are atomic and idempotent.
    return (f'table inet {TABLE}\n'
            f'delete table inet {TABLE}\n'
            f'table inet {TABLE} {{\n'
            f'  chain output {{\n'
            f'    type filter hook output priority -5; policy accept;\n'
            f'{body}\n'
            f'  }}\n'
            f'}}\n')


def nft(args, text):
    return subprocess.run([NFT, *args, '-f', '-'], input=text, capture_output=True, text=True,
                          timeout=20, env={'PATH': '/usr/bin:/bin', 'LANG': 'C'}, check=False)


def apply(check_only=False):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('Run the loopback guard as root on Decrypt OS.')
    result = nft(['-c'] if check_only else [], render(static_uid()))
    if result.returncode:
        raise SystemExit('The loopback firewall could not be loaded.')


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args == ['render']:
        print(render(static_uid()), end='')
    elif args in (['apply'], ['check']):
        apply(check_only=args == ['check'])
    else:
        raise SystemExit('usage: loopback_guard.py apply|check|render')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
