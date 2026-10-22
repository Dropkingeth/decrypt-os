#!/usr/bin/env python3
"""Versioned local installation acknowledgement. Not a waiver or legal opinion."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

VERSION = '2026-09-22.v1'
TEXT = ('Decrypt provides software and research tools, not investment advice. '
        'Trading can cause substantial losses. Results are not guaranteed. '
        'You control your accounts, strategy choices and orders. '
        'TradingView, brokers and other third-party services have their own terms, fees and approvals. '
        'Back up your files before installation. Installing the OS erases the selected disk. '
        'Accepting this notice does not connect an account or authorize a trade.')
TEXT_SHA256 = hashlib.sha256(TEXT.encode()).hexdigest()
PHRASE = 'I AGREE'
FIRST_START_VERSION = '2026-09-27.firstboot.v1'
FIRST_START_TEXT = 'Decrypt provides software and research tools, not investment advice. Trading can cause substantial losses. Results are not guaranteed. You control your accounts, strategy choices and orders. TradingView, brokers and other third-party services have their own terms, fees and approvals. Accepting this notice does not connect an account or authorize a trade.'
FIRST_START_SHA256 = hashlib.sha256(FIRST_START_TEXT.encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular(path, owners):
    path = Path(path)
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and not parent.is_symlink(), 'Acknowledgement paths cannot contain links.')
        require(info.st_uid in owners | {0} and not info.st_mode & 0o022, 'Acknowledgement path is writable by another account.')
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid in owners
            and stat.S_IMODE(info.st_mode) in (0o600, 0o644) and info.st_size <= 16384,
            'Acknowledgement must be a bounded local file owned by the installing account or root.')
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, 'Duplicate acknowledgement key.')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('Nonfinite acknowledgement value.')
    return json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=invalid)


def strict_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def validate(value, stage, binding, account):
    expected = {'schema': 'decrypt.install-acknowledgement.v1', 'version': VERSION,
                'textSha256': TEXT_SHA256, 'text': TEXT, 'stage': stage,
                'binding': binding, 'account': account, 'accepted': True, 'method': 'typed-I-AGREE'}
    require(isinstance(value, dict), 'Installation acknowledgement must be an object.')
    expected['acceptedAt'] = value.get('acceptedAt')
    require(strict_equal(value, expected),
            'Installation acknowledgement is missing, outdated or belongs to another target/plan.')
    require(isinstance(value.get('acceptedAt'), str), 'Acknowledgement time is missing.')
    stamp = datetime.fromisoformat(value['acceptedAt'])
    require(stamp.tzinfo is not None, 'Acknowledgement time must include its time zone.')
    return value


def verify(path, stage, binding, account, owners=None):
    return validate(regular(path, set(owners if owners is not None else (os.getuid(), 0))), stage, binding, account)


def acknowledge(path, stage, binding, account):
    path = Path(path)
    require(sys.stdin.isatty(), 'Read and accept the installation notice in the local terminal.')
    if path.exists() or path.is_symlink():
        # A canceled/failed sudo request may reuse only this exact target and plan.
        return verify(path, stage, binding, account)
    print('\nBefore installation (' + VERSION + ')\n' + TEXT + '\n')
    if input('Type I AGREE to accept, or press Return to cancel: ').strip() != PHRASE:
        return None
    value = {'schema': 'decrypt.install-acknowledgement.v1', 'version': VERSION,
             'textSha256': TEXT_SHA256, 'text': TEXT, 'stage': stage, 'binding': binding,
             'account': account, 'accepted': True, 'method': 'typed-I-AGREE',
             'acceptedAt': datetime.now(timezone.utc).isoformat()}
    # No overwrite, symlink-following or password logging.
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and not parent.is_symlink()
                and info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022,
                'Acknowledgement path is not private to this account.')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return verify(path, stage, binding, account)


def runtime_binding(machine, recipe):
    require(all(re.fullmatch('[0-9a-f]{64}', value or '') for value in (machine, recipe)), 'Invalid installation binding.')
    return {'machineSha256': machine, 'recipeSha256': recipe}


def base_binding(context, installer):
    raw = Path(context).read_bytes()
    value = regular(context, {0})
    require(value.get('password') != 'first-boot' and value.get('customer_account_created') is not False,
            'Factory images defer customer consent until interactive first start.')
    require(isinstance(value.get('user'), str), 'Missing installation account.')
    return {'contextSha256': hashlib.sha256(raw).hexdigest(),
            'installerSha256': hashlib.sha256(Path(installer).read_bytes()).hexdigest()}, value['user']


def firstboot_accept(policy_sha256, document_sha256, instance_id, *, explicitly_accepted=False):
    """Call only after the app showed this exact text and the customer accepted it.

    The fixed root helper validates the enrolled account and active graphical session.
    Password approval is local; neither password nor arbitrary command enters this API.
    """
    import subprocess
    import uuid
    require(explicitly_accepted is True, 'Customer consent remains pending.')
    require(os.geteuid() != 0 and sys.stdin.isatty(), 'Use the local customer terminal for password-required approval.')
    require(isinstance(policy_sha256, str) and re.fullmatch('[0-9a-f]{64}', policy_sha256), 'Invalid first-start policy binding.')
    require(document_sha256 == FIRST_START_SHA256, 'Show the current first-start notice before accepting.')
    require(str(uuid.UUID(instance_id)) == instance_id and uuid.UUID(instance_id).int, 'Invalid instance identity.')
    result = subprocess.run(['/usr/bin/sudo', '-k', '--', '/usr/bin/python3', '-I', '-B',
        '/usr/local/lib/decrypt-firstboot/firstboot.py', 'accept-notice',
        '--policy-sha256', policy_sha256, '--document-sha256', document_sha256, '--instance-id', instance_id],
        check=False, env={'PATH':'/usr/bin:/bin'})
    require(result.returncode == 0, 'Notice acceptance remains pending; local approval did not complete.')
    return {'accepted': True, 'documentSha256': document_sha256, 'instanceId': instance_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('base-accept', 'base-verify'))
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(os.geteuid() == 0, 'The base installer notice uses the native root console.')
        binding, account = base_binding(args.context, args.installer)
        if args.action == 'base-accept':
            value = acknowledge(args.receipt, 'base-install', binding, account)
            require(value is not None, 'Cancelled. No disk changes made.')
        else:
            verify(args.receipt, 'base-install', binding, account, {0})
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
