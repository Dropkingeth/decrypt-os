#!/usr/bin/env python3
"""Prove phase-A offline isolation. Never change the host or builder uplink."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
from guard import Refusal, read_private_context, require, run

RECEIPT = Path('/run/decrypt-test-network-ready.json')

BLOCKED_V4 = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
              '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
              '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
              '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')


def render(gateway):
    address = ipaddress.IPv4Address(gateway)
    require(any(address in ipaddress.IPv4Network(network)
                for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')),
            'The NAT gateway must be an RFC1918 address.')
    return f'''# New customer-test guest only. No host policy changes.
table inet decrypt_customer_test {{
  set blocked_v4 {{
    type ipv4_addr
    flags interval
    elements = {{ {', '.join(BLOCKED_V4)} }}
  }}
  chain input {{
    type filter hook input priority -10; policy drop;
    iifname "lo" accept
    ct state invalid drop
    ip saddr {address} udp sport 67 udp dport 68 accept
    ct state established,related accept
  }}
  chain forward {{
    type filter hook forward priority -10; policy drop;
  }}
  chain output {{
    type filter hook output priority -10; policy drop;
    oifname "lo" accept
    ct state invalid drop
    ip daddr {{ 255.255.255.255, {address} }} udp sport 68 udp dport 67 accept
    ip daddr {address} udp dport 53 accept
    ip daddr {address} tcp dport 53 accept
    meta nfproto ipv6 reject
    ip daddr @blocked_v4 reject
    ct state established,related accept
    meta nfproto ipv4 accept
  }}
}}
'''


def collect_offline():
    require(platform.system() == 'Linux' and os.geteuid() == 0, 'Offline proof requires guest Linux root.')
    current = os.stat('/proc/self/ns/net')
    initial = os.stat('/proc/1/ns/net')
    return {'namespace': [current.st_dev, current.st_ino],
            'initialNamespace': [initial.st_dev, initial.st_ino],
            'bootId': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'links': json.loads(run('ip', '-json', 'link', 'show')),
            'routes4': json.loads(run('ip', '-json', '-4', 'route', 'show', 'table', 'all')),
            'routes6': json.loads(run('ip', '-json', '-6', 'route', 'show', 'table', 'all'))}


def validate_offline(snapshot):
    import target_profile
    for key in ('namespace', 'initialNamespace'):
        values = snapshot.get(key)
        require(type(values) is list and len(values) == 2
                and all(type(x) is int and x > 0 for x in values), 'Missing kernel network namespace identity.')
    require(snapshot['namespace'] != snapshot['initialNamespace'], 'Refusing the builder initial network namespace.')
    require(target_profile.uuid_text(snapshot.get('bootId')), 'Missing boot identity.')
    links = snapshot.get('links')
    require(type(links) is list and len(links) == 1 and links[0].get('ifname') == 'lo'
            and links[0].get('link_type') == 'loopback', 'Offline namespace contains a non-loopback interface.')
    for key in ('routes4', 'routes6'):
        rows = snapshot.get(key)
        require(type(rows) is list, 'Missing route inventory.')
        require(all(row.get('dev') == 'lo' and not row.get('gateway')
                    and row.get('type') in ('local', 'broadcast', 'unicast', None)
                    and row.get('dst') not in ('default', '0.0.0.0/0', '::/0') for row in rows),
                'Offline namespace contains an egress/default route.')
    return True


def write_exclusive(path, value):
    info = path.parent.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            'Receipt parent must be a real root-controlled directory.')
    raw = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as handle:
        os.fchmod(handle.fileno(), 0o600); handle.write(raw); handle.flush(); os.fsync(handle.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify():
    receipt = read_private_context(RECEIPT)
    require(receipt.get('schema') == 'decrypt.offline-network.v1', 'Legacy public-network receipt is retired.')
    snapshot = collect_offline(); validate_offline(snapshot)
    require(receipt.get('bootId') == snapshot['bootId'] and receipt.get('namespace') == snapshot['namespace'],
            'Network receipt belongs to another boot or namespace.')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--apply-live-offline', action='store_true')
    actions.add_argument('--verify', action='store_true')
    actions.add_argument('--render', action='store_true', help='Inspect legacy NAT text only; never applies it.')
    actions.add_argument('--apply-guest-policy', action='store_true', help=argparse.SUPPRESS)
    actions.add_argument('--resume-guest-policy', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--gateway')
    args = parser.parse_args()
    require(not args.apply_guest_policy and not args.resume_guest_policy,
            'Online/live recovery is retired. Use a newly isolated offline driver namespace.')
    if args.render:
        require(args.gateway is not None, 'An explicit gateway is required for text rendering.')
        print(render(args.gateway), end=''); return
    require(args.gateway is None, 'Offline mode accepts no gateway.')
    if args.verify:
        print(json.dumps(verify(), sort_keys=True)); return
    snapshot = collect_offline(); validate_offline(snapshot)
    receipt = {'schema': 'decrypt.offline-network.v1', 'bootId': snapshot['bootId'],
               'namespace': snapshot['namespace'], 'phaseA': 'offline', 'hostNetworkChanged': False}
    # A ready receipt records observation, not an escape-capable network policy.
    write_exclusive(RECEIPT, receipt)
    verify()
    print(json.dumps(receipt, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        print('Stopped: ' + str(error), file=sys.stderr); sys.exit(1)
