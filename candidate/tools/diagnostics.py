#!/usr/bin/env python3
"""Read-only, bounded installation facts, and a privacy-safe diagnostics
bundle a user can attach to a GitHub issue. No account, log or conversation
reads: journal lines go through redact() before they leave this process."""
from __future__ import annotations
import argparse
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tarfile

VERSION = '2026.09.24-beta1-dev'
# Single source of truth for where a diagnostics bundle gets filed.
REPO_URL = 'https://github.com/Dropkingeth/decrypt-os'
COMMANDS = ('python3', 'systemctl', 'sudo', 'foot', 'xdg-open', 'hyprctl', 'cryptsetup', 'chromium', 'notify-send')
UNITS = ('decrypt-os-api.service', 'decrypt-os-ui.service', 'decrypt-os-market-browser.service')
INSTALL_FILES = {
    'app_backend': '/usr/local/lib/decrypt-os/app/backend/server.py',
    'app_manifest': '/usr/local/lib/decrypt-os/SOURCE-MANIFEST.json',
    'runtime_node': '/usr/local/lib/decrypt-os-runtime/node-v24.19.0-linux-arm64/bin/node',
    'api_unit': '/etc/systemd/user/decrypt-os-api.service',
    'ui_unit': '/etc/systemd/user/decrypt-os-ui.service',
}
STATES = {'active', 'inactive', 'failed', 'activating', 'deactivating', 'unknown', 'maintenance', 'reloading'}
JOURNAL_LINES_PER_UNIT = 20

# Order matters: path-shaped redactions run before the generic account/number
# one, so a redacted path's own digits are not double-processed oddly, and
# email-shaped redaction runs before the bare-username path replaces an '@'
# host part that might otherwise look like a short path segment.
REDACTIONS = (
    (re.compile(r'/Users/[^/\s"\'<>]+'), '/Users/<redacted>'),
    (re.compile(r'/home/[^/\s"\'<>]+'), '/home/<redacted>'),
    (re.compile(r'\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b'), '<redacted-email>'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----'), '<redacted-private-key>'),
    # Secret/token/password-style literals: "key=value" or "key: value",
    # keeping the key name (useful context) but dropping the value.
    (re.compile(r'(?i)\b(api[_-]?key|secret|token|password|passwd|cookie|session)(["\']?\s*[:=]\s*)\S+'),
     r'\1\2<redacted>'),
    # Trading account/order identifiers and other long digit runs (account
    # numbers, contract/order ids): keep short numbers (ports, counts, exit
    # codes) but drop anything that could identify an account.
    (re.compile(r'\b\d{6,}\b'), '<redacted-number>'),
    # A bare username is usually whatever is left right after "user"/"login"/
    # "account" without a redacted path or email already covering it.
    (re.compile(r'(?i)\b(user|login|account)(\s*[:=]\s*)[A-Za-z0-9._-]+'), r'\1\2<redacted>'),
)


def redact(text):
    """Scrub one line of free text for secrets, account identifiers,
    usernames and local paths, in that fixed order."""
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text

def file_fact(path):
    """Never follow a redirected install path or read the file's contents."""
    path = Path(path)
    try:
        for item in (path, *path.parents):
            if item.is_symlink():
                return {'status': 'linked_path'}
        metadata = path.lstat()
    except FileNotFoundError:
        return {'status': 'missing'}
    except OSError:
        return {'status': 'unavailable'}
    if not stat.S_ISREG(metadata.st_mode):
        return {'status': 'unexpected_type'}
    return {'status': 'present', 'rootOwned': metadata.st_uid == 0,
            'mode': format(stat.S_IMODE(metadata.st_mode), '04o')}

def unit_fact(unit, runner=subprocess.run):
    if unit not in UNITS:
        raise ValueError('Unsupported diagnostic unit')
    # Do not capture journal text, process arguments, environment, or stderr.
    try:
        result = runner(['/usr/bin/systemctl', '--user', 'is-active', unit],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, timeout=4,
                        env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                             **{k: os.environ[k] for k in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if k in os.environ}})
    except (OSError, subprocess.TimeoutExpired):
        return 'unavailable'
    value = result.stdout.strip()
    return value if value in STATES else 'unavailable'

def journal_fact(unit, lines=JOURNAL_LINES_PER_UNIT, runner=subprocess.run):
    """Recent journal lines for one unit, each redacted before it is ever
    stored or returned. Message text only (-o cat): no journal metadata
    fields (no _EXE, _CMDLINE, _SYSTEMD_INVOCATION_ID, ...) are requested."""
    if unit not in UNITS:
        raise ValueError('Unsupported diagnostic unit')
    try:
        result = runner(['/usr/bin/journalctl', '--user', '-u', unit, '-n', str(lines),
                         '--no-pager', '-o', 'cat'],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, timeout=6,
                        env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                             **{k: os.environ[k] for k in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS') if k in os.environ}})
    except (OSError, subprocess.TimeoutExpired):
        return ['<unavailable>']
    return [redact(line) for line in result.stdout.splitlines()[-lines:]]


def collect(*, system=None, machine=None, uid=None, file_reader=file_fact,
            unit_reader=unit_fact, journal_reader=journal_fact, which=shutil.which,
            disk_reader=shutil.disk_usage):
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    uid = os.geteuid() if uid is None else uid
    facts = {'schema': 'decrypt.diagnostics.v1', 'candidate': VERSION,
             'platform': {'system': system, 'architecture': machine},
             'mode': 'read_only', 'installationVerified': False,
             'providerChecked': False, 'nativeAcceptance': 'not_established'}
    if system != 'Linux' or machine not in ('aarch64', 'arm64'):
        return {**facts, 'status': 'unsupported_host', 'nextAction': 'Run diagnostics in the ARM64 test desktop.'}
    if uid == 0:
        return {**facts, 'status': 'wrong_account', 'nextAction': 'Run diagnostics as the ordinary desktop account, without sudo.'}
    facts['commands'] = {name: bool(which(name)) for name in COMMANDS}
    facts['files'] = {name: file_reader(path) for name, path in INSTALL_FILES.items()}
    facts['services'] = {unit: unit_reader(unit) for unit in UNITS}
    facts['recentErrors'] = {unit: journal_reader(unit) for unit in UNITS}
    try:
        disk = disk_reader('/')
        facts['rootDisk'] = {'totalBytes': disk.total, 'freeBytes': disk.free}
    except OSError:
        facts['rootDisk'] = {'status': 'unavailable'}
    facts['status'] = 'collected'
    facts['nextAction'] = 'If installation stopped, use guided status and its documented recovery step. Do not rerun disk installation.'
    facts['limitations'] = [
        'File presence is not file-integrity verification.',
        'Inactive services can be normal before first launch.',
        'No credentials, trading history, account settings, serial numbers or machine identity were collected.',
        'Recent service journal lines are included, with secrets, account ids, usernames and local paths redacted '
        'by pattern before collection -- review the bundle yourself before attaching it; redaction is not a '
        'cryptographic guarantee.',
    ]
    return facts

def write_bundle(facts, output):
    """One deterministic tar.gz containing diagnostics.json, the only file
    the user is asked to attach to a GitHub issue. 0600: the file sits in
    the user's home directory until they manually attach and delete it."""
    output = Path(output)
    if os.path.lexists(output):
        raise SystemExit(f'{output} already exists; choose a new path')
    payload = json.dumps(facts, indent=2, sort_keys=True).encode() + b'\n'
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            info = tarfile.TarInfo('diagnostics.json')
            info.size, info.mode, info.mtime = len(payload), 0o600, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            archive.addfile(info, io.BytesIO(payload))
    return output

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, metavar='PATH',
                        help='write a diagnostics.json tar.gz to PATH (default: print JSON to stdout only)')
    args = parser.parse_args()
    result = collect()
    if args.bundle:
        path = write_bundle(result, args.bundle)
        print(f'wrote {path}')
        print(f'Attach that file to a new issue at {REPO_URL}/issues/new -- it is already redacted, '
              'but take a look before you attach it.')
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'collected' else 2

if __name__ == '__main__':
    raise SystemExit(main())
