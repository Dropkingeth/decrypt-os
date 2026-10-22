#!/usr/bin/env python3
"""C1 profile and C5 run validation. Validation grants no disk-write authority."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

SCHEMA = 'decrypt.target-profile.v1'
FIELDS = set('schema id kind arch hypervisors vmName hostname user disk partitionNaming liveRoot kernelPackage basePackages desktopPackageList network logChannel graphics password notice'.split())
ID = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
PACKAGE = re.compile(r'[a-z0-9][a-z0-9+_.@-]*\Z')
HEX = re.compile(r'[a-f0-9]{64}\Z')
DEVICE = re.compile(r'/dev/(?:[a-z]+[a-z0-9]*|nvme[0-9]+n[0-9]+)\Z')
LOOP = re.compile(r'/dev/loop[0-9]+\Z')
SUPPORTED_KERNELS = {'aarch64': 'linux-aarch64', 'x86_64': 'linux'}
RESERVED_USERS = frozenset('root nobody bin daemon mail ftp http dbus polkitd systemd-network systemd-resolve systemd-timesync avahi sddm arch alarm omarchy'.split())


class ProfileError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ProfileError(message)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON field.')
            result[key] = value
        return result
    def invalid(_):
        raise ProfileError('Nonfinite JSON is not accepted.')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def valid_user(value):
    return (type(value) is str and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value)
            is not None and value not in RESERVED_USERS)


def object_keys(value, keys, label):
    require(type(value) is dict and set(value) == set(keys.split()), label + ' fields differ.')


def strings(value, label, pattern=None):
    require(type(value) is list and value and all(type(x) is str and x for x in value)
            and len(set(value)) == len(value), label + ' must be a nonempty unique string array.')
    require(pattern is None or all(pattern.fullmatch(x) for x in value), label + ' has an invalid value.')


def relative_path(value):
    require(type(value) is str and value and not value.startswith('/') and '\\' not in value
            and all(part not in ('', '.', '..') for part in value.split('/'))
            and not any(c.isspace() or ord(c) < 32 for c in value), 'Unsafe signed-media path.')
    return value


def validate(value):
    require(type(value) is dict and set(value) == FIELDS and value['schema'] == SCHEMA,
            'Unsupported target profile or unknown fields.')
    for key in ('id', 'vmName'):
        require(type(value[key]) is str and ID.fullmatch(value[key]), 'Invalid profile ' + key + '.')
    require(value['kind'] in ('qualification', 'dryrun-loop', 'image-build'), 'Unsupported profile kind.')
    require(type(value['arch']) is str and value['arch'] in SUPPORTED_KERNELS, 'Architecture is not implemented or qualified by this factory.')
    require(type(value['hostname']) is str and len(value['hostname']) <= 63
            and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', value['hostname']), 'Invalid hostname label.')
    require(valid_user(value['user']), 'Invalid or reserved local username.')
    strings(value['hypervisors'], 'Hypervisors', re.compile(r'[a-z][a-z0-9-]*\Z'))
    require('none' not in value['hypervisors'], 'An isolated virtual builder is required.')
    disk = value['disk']; object_keys(disk, 'device bytes', 'Disk')
    require(type(disk['device']) is str and DEVICE.fullmatch(disk['device']), 'Explicit canonical block-device path required.')
    require(type(disk['bytes']) is int and disk['bytes'] > 0 and disk['bytes'] % 512 == 0, 'Positive aligned exact disk size required.')
    require(value['partitionNaming'] in ('suffix', 'p-suffix'), 'Unknown partition naming rule.')
    require(value['partitionNaming'] == ('p-suffix' if disk['device'][-1].isdigit() else 'suffix'), 'Partition naming does not match the device family.')
    if value['kind'] in ('dryrun-loop', 'image-build'):
        require(LOOP.fullmatch(disk['device']), 'Factory and dry-run profiles require an explicitly allocated loop image.')
    live = value['liveRoot']; object_keys(live, 'allowedFsTypes requireRamBacked', 'Live root')
    strings(live['allowedFsTypes'], 'Live filesystems', re.compile(r'[a-z][a-z0-9_-]*\Z'))
    require(type(live['requireRamBacked']) is bool, 'RAM-backed policy must be a boolean.')
    require(value['kind'] == 'image-build' or live['requireRamBacked'], 'Live qualification/dry-run requires a RAM-backed root.')
    require(value['kernelPackage'] == SUPPORTED_KERNELS[value['arch']], 'Unsupported architecture/kernel pairing.')
    strings(value['basePackages'], 'Base packages', PACKAGE)
    require(value['kernelPackage'] in value['basePackages'], 'Kernel is absent from base roots.')
    relative_path(value['desktopPackageList'])
    network = value['network']; object_keys(network, 'phaseA phaseB policyId', 'Network')
    require(network['phaseA'] == 'offline' and network['phaseB'] in ('offline', 'pinned-runtime-only')
            and type(network['policyId']) is str and ID.fullmatch(network['policyId']), 'Unsupported network policy.')
    log = value['logChannel']; object_keys(log, 'kind label uuid', 'Log channel')
    require(log == {'kind': 'log-disk', 'label': 'DECRYPT_LOG', 'uuid': None}, 'Only reusable C5-bound log-disk profiles are supported.')
    graphics = value['graphics']; object_keys(graphics, 'policy softwareRendering', 'Graphics')
    require(type(graphics['policy']) is str and ID.fullmatch(graphics['policy'])
            and type(graphics['softwareRendering']) is bool, 'Invalid graphics policy.')
    require(value['password'] == {'qualification': 'native-prompt', 'dryrun-loop': 'locked', 'image-build': 'first-boot'}[value['kind']], 'Password mode does not match profile kind.')
    require(value['notice'] in ('interactive', 'profile-accepted')
            and (value['notice'] != 'profile-accepted' or value['kind'] == 'qualification'), 'Customer consent cannot be preaccepted by an image profile.')
    return value


def read(path, maximum=65536):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'Absolute canonical input path required.')
    for parent in path.parents:
        require(stat.S_ISDIR(parent.lstat().st_mode), 'Linked or missing input parent.')
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= maximum,
            'Bounded single-link regular input required.')
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        require(identity(os.fstat(handle.fileno())) == identity(before), 'Input changed before read.')
        raw = handle.read(maximum + 1)
        require(identity(os.fstat(handle.fileno())) == identity(before), 'Input changed while reading.')
    require(len(raw) == before.st_size and identity(path.lstat()) == identity(before), 'Input changed after read.')
    return raw


def load(path, expected_sha256):
    require(type(expected_sha256) is str and HEX.fullmatch(expected_sha256), 'Externally authenticated profile hash required.')
    raw = read(Path(path))
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, 'Signed profile bytes differ.')
    return validate(strict_json(raw))


def uuid_text(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except (ValueError, AttributeError):
        return False


def validate_run(value, profile, profile_sha256, media_sha256, log_uuid):
    validate(profile)
    object_keys(value, 'schema runId vmName mediaSha256 profileId profileSha256 logUuid targetBytes createdAt', 'Run binding')
    require(value['schema'] == 'decrypt.install-run.v1' and uuid_text(value['runId']) and uuid_text(value['logUuid']), 'Invalid C5 run or log UUID.')
    require(type(profile_sha256) is str and HEX.fullmatch(profile_sha256)
            and type(media_sha256) is str and HEX.fullmatch(media_sha256), 'Independent profile/media hashes required.')
    require(value['vmName'] == profile['vmName'] and value['profileId'] == profile['id']
            and value['profileSha256'] == profile_sha256 and value['mediaSha256'] == media_sha256
            and value['logUuid'] == log_uuid and type(value['targetBytes']) is int
            and value['targetBytes'] == profile['disk']['bytes'], 'C5 run/profile/media/log/target mismatch.')
    require(type(value['createdAt']) is str and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z', value['createdAt']), 'UTC RFC3339 run time required.')
    try:
        datetime.datetime.fromisoformat(value['createdAt'].replace('Z', '+00:00'))
    except ValueError as error:
        raise ProfileError('Invalid run timestamp.') from error
    return value


def device_for(profile, allocation=None):
    validate(profile)
    if profile['kind'] == 'qualification':
        require(allocation is None, 'Qualification profile cannot substitute its disk.')
        return profile['disk']['device']
    require(type(allocation) is dict and type(allocation.get('device')) is str
            and LOOP.fullmatch(allocation['device']), 'An explicit allocated-loop binding is required; never guess the profile example device.')
    require(type(allocation.get('bytes')) is int and allocation['bytes'] == profile['disk']['bytes']
            and allocation.get('createdExclusive') is True and uuid_text(allocation.get('runId')),
            'Loop allocation size, exclusive creation or run identity is missing.')
    for key in ('backingDevice', 'backingInode'):
        require(type(allocation.get(key)) is int and allocation[key] > 0, 'Loop backing-file identity is missing.')
    path = allocation.get('backingFile')
    require(type(path) is str and path.startswith('/') and str(PurePosixPath(path)) == path
            and '..' not in PurePosixPath(path).parts, 'Canonical loop backing file required.')
    return allocation['device']


def partition_device(profile, index, allocation=None):
    require(type(index) is int and 1 <= index <= 128, 'Invalid partition index.')
    return device_for(profile, allocation) + ('p' if profile['partitionNaming'] == 'p-suffix' else '') + str(index)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args()
    value = load(args.profile, args.sha256)
    print(json.dumps({'schema': value['schema'], 'id': value['id'], 'kind': value['kind'], 'validated': True, 'nativeQualified': False}))
