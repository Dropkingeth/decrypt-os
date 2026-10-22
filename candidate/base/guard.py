#!/usr/bin/env python3
"""Profile-bound read-only target guards. No guessed disks or legacy target fallback."""
import argparse
import gzip
import json
import os
from pathlib import Path
import platform
import re
import socket
import stat
import subprocess
import sys
import target_profile as profiles

LIVE_CONTEXT = Path('/run/decrypt-clean-install-context.json')
INSTALLED_CONTEXT = Path('/etc/decrypt-clean-base-context.json')
INSTALLED_PROFILE = Path('/etc/decrypt-target-profile.json')
TARGET = Path('/mnt/decrypt-base')
ZRAM_ROOT = '/dev/zram0'
ZRAM_SYSFS = '/sys/devices/virtual/block/zram0'


class Refusal(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, 'Required probe failed: ' + args[0])
    return result.stdout.strip()


def read_private_context(path):
    path = Path(path)
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'Context parent is not root-controlled.')
    info = path.lstat()
    require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600,
            'Context must be owned by root with mode 600.')
    return profiles.strict_json(profiles.read(path))


valid_user = profiles.valid_user


def verify_zero_stream(stream, expected_bytes):
    require(type(expected_bytes) is int and expected_bytes > 0, 'Invalid scan size.')
    remaining = expected_bytes
    while remaining:
        chunk = stream.read(min(4 * 1024**2, remaining))
        require(bool(chunk) and len(chunk) <= remaining, 'Blank scan ended early or exceeded the bound.')
        require(not chunk.strip(b'\0'), 'Refusing a disk containing existing nonzero data.')
        remaining -= len(chunk)


def verify_blank_disk(snapshot, profile, allocation=None):
    device = profiles.device_for(profile, allocation)
    with os.fdopen(os.open(device, os.O_RDONLY | os.O_NOFOLLOW), 'rb', buffering=0) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISBLK(info.st_mode) and info.st_rdev == snapshot['device_id'],
                'Selected disk changed before its blank scan.')
        verify_zero_stream(stream, profile['disk']['bytes'])


def collect_zram_root(root):
    """Read kernel identities for the observed Archboot Btrfs-on-zram root."""
    roots = root.get('filesystems', [])
    if len(roots) != 1 or roots[0].get('source') != ZRAM_ROOT or roots[0].get('fstype') != 'btrfs':
        return None
    uuid = roots[0].get('uuid', '')
    require(bool(re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', uuid)),
            'The zram root filesystem identity is missing.')
    metadata = os.lstat(ZRAM_ROOT)
    sysfs = Path('/sys/class/block/zram0')
    members = Path('/sys/fs/btrfs') / uuid / 'devices'
    backing = sysfs / 'backing_dev'
    writeback_disabled = False
    if backing.exists():
        backing_device = backing.read_text().strip()
    else:
        # This Archboot kernel omits the attribute because writeback is disabled.
        # Missing metadata alone is never evidence of memory-only storage.
        with gzip.open('/proc/config.gz', 'rt', encoding='ascii') as stream:
            config = stream.read(4 * 1024 * 1024 + 1)
        require(len(config) <= 4 * 1024 * 1024, 'Unexpected running-kernel configuration size.')
        lines = config.splitlines()
        writeback_disabled = ('# CONFIG_ZRAM_WRITEBACK is not set' in lines
                              and not any(line.startswith('CONFIG_ZRAM_WRITEBACK=') for line in lines))
        backing_device = None
    return {
        'device_is_block': stat.S_ISBLK(metadata.st_mode),
        'device_realpath': os.path.realpath(ZRAM_ROOT),
        'device_numbers': f'{os.major(metadata.st_rdev)}:{os.minor(metadata.st_rdev)}',
        'sysfs_realpath': os.path.realpath(sysfs),
        'sysfs_device_numbers': (sysfs / 'dev').read_text().strip(),
        'disk_bytes': int((sysfs / 'disksize').read_text().strip()),
        'backing_device': backing_device,
        'writeback_disabled_in_running_kernel': writeback_disabled,
        'filesystem_uuid': uuid,
        'filesystem_devices': {member.name: os.path.realpath(member) for member in members.iterdir()},
    }


def validate_live_root(snapshot, profile):
    roots = snapshot.get('root', {}).get('filesystems', [])
    require(len(roots) == 1 and roots[0].get('target') == '/',
            'Expected exactly one live root mount.')
    root = roots[0]
    require(root.get('fstype') in profile['liveRoot']['allowedFsTypes'], 'Root filesystem is outside the signed profile.')
    if not profile['liveRoot']['requireRamBacked']:
        require(profile['kind'] == 'image-build', 'Only a bound factory loop image may use an installed builder root.')
        require(root.get('fstype') in {'tmpfs', 'ramfs', 'rootfs'}
                or (root.get('fstype') in {'ext4', 'btrfs'} and str(root.get('source', '')).startswith('/dev/')),
                'Factory root backing is ambiguous.')
        return
    if root.get('fstype') in {'tmpfs', 'ramfs', 'rootfs'}:
        return
    # Overlay/squashfs alone do not prove RAM backing. Ambiguous roots refuse.
    zram = snapshot.get('zram_root') or {}
    require(root.get('fstype') == 'btrfs' and root.get('source') == ZRAM_ROOT,
            'Expected a live memory-backed installer, not an installed root disk.')
    require(zram.get('device_is_block') is True and zram.get('device_realpath') == ZRAM_ROOT,
            'The live zram root must be its exact block device, not a file or link.')
    require(zram.get('sysfs_realpath') == ZRAM_SYSFS,
            'The live root is not a kernel virtual zram device.')
    numbers = zram.get('device_numbers', '')
    require(isinstance(numbers, str) and bool(re.fullmatch(r'[0-9]+:[0-9]+', numbers))
            and numbers == zram.get('sysfs_device_numbers'),
            'The zram device and kernel block identities do not match.')
    require(type(zram.get('disk_bytes')) is int and zram['disk_bytes'] > 0,
            'The zram root capacity is missing or zero.')
    require(zram.get('backing_device') == 'none'
            or (zram.get('backing_device') is None
                and zram.get('writeback_disabled_in_running_kernel') is True),
            'A zram root with disk-backed writeback is not a memory-only installer.')
    uuid = root.get('uuid', '')
    require(isinstance(uuid, str)
            and bool(re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', uuid))
            and zram.get('filesystem_uuid') == uuid,
            'The live Btrfs root identity does not match its kernel device inventory.')
    require(zram.get('filesystem_devices') == {'zram0': ZRAM_SYSFS},
            'The live Btrfs root must use only the proven zram device.')


def flatten_blocks(rows, parent=None, result=None):
    result = {} if result is None else result
    require(type(rows) is list, 'Missing block inventory.')
    for row in rows:
        name = row.get('name')
        require(type(name) is str and name.startswith('/dev/') and name not in result,
                'Duplicate or invalid block identity.')
        result[name] = {**row, '_parent': parent or row.get('pkname')}
        flatten_blocks(row.get('children', []), name, result)
    return result


def lineage(name, inventory):
    found = set()
    while name:
        require(name in inventory and name not in found, 'Incomplete or cyclic device ancestry.')
        found.add(name)
        name = inventory[name].get('_parent')
    return found


def validate_exclusions(snapshot, device, run_binding):
    inventory = flatten_blocks(snapshot.get('inventory', {}).get('blockdevices'))
    selected = lineage(device, inventory)
    require(inventory[device].get('maj:min') == f"{os.major(snapshot['device_id'])}:{os.minor(snapshot['device_id'])}", 'Target kernel identity differs from inventory.')
    exclusions = []
    for label in ('DECRYPT_MEDIA', 'DECRYPT_LOG'):
        found = [name for name, row in inventory.items() if row.get('label') == label]
        require(len(found) == 1, 'Expected exactly one ' + label + ' filesystem.')
        name = found[0]
        require(inventory[name].get('fstype') == 'ext4', 'C2/C5 media and evidence require ext4.')
        require(profiles.uuid_text(inventory[name].get('uuid')), 'Missing media/evidence filesystem UUID.')
        require(sum(row.get('uuid') == inventory[name]['uuid'] for row in inventory.values()) == 1, 'Ambiguous media/evidence filesystem UUID.')
        ancestry = lineage(name, inventory)
        require(not any(ancestry.intersection(prior) for prior in exclusions), 'Media and evidence devices overlap.')
        exclusions.append(ancestry)
        require(not selected.intersection(ancestry), 'Target overlaps media or evidence ancestry.')
        if label == 'DECRYPT_LOG':
            require(inventory[name].get('uuid') == run_binding['logUuid'], 'Attached evidence disk UUID differs.')
        else:
            require(inventory[name].get('ro') in (True, 1), 'Installer media must remain read-only.')
    roots = snapshot['root']['filesystems']
    root_source = roots[0].get('source', '').split('[')[0]
    if root_source.startswith('/dev/'):
        require(not selected.intersection(lineage(root_source, inventory)), 'Target overlaps the running root disk.')
    return inventory


def loop_evidence(allocation):
    path = Path(allocation['backingFile'])
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'Loop backing directory is linked or not root-controlled.')
    info = path.lstat()
    block = Path('/sys/class/block') / Path(allocation['device']).name / 'loop'
    return {'regular': stat.S_ISREG(info.st_mode), 'uid': info.st_uid,
            'mode': stat.S_IMODE(info.st_mode), 'links': info.st_nlink,
            'device': info.st_dev, 'inode': info.st_ino, 'bytes': info.st_size,
            'backingFile': (block / 'backing_file').read_text().strip(),
            'offset': int((block / 'offset').read_text()),
            'sizeLimit': int((block / 'sizelimit').read_text())}


def validate_loop(snapshot, allocation, run_binding):
    proof = snapshot.get('loop') or {}
    require(allocation['runId'] == run_binding['runId'], 'Loop allocation belongs to a different run.')
    require(proof.get('regular') is True and proof.get('uid') == 0 and proof.get('links') == 1
            and proof.get('mode') == 0o600, 'Loop backing must be an exclusive root-private regular image.')
    expected = {'device': allocation['backingDevice'], 'inode': allocation['backingInode'],
                'bytes': allocation['bytes'], 'backingFile': allocation['backingFile'],
                'offset': 0, 'sizeLimit': 0}
    require(all(proof.get(key) == value for key, value in expected.items()), 'Kernel loop/backing identity differs.')


def collect_live(profile, allocation=None):
    profiles.validate(profile)
    require(platform.system() == 'Linux' and os.geteuid() == 0, 'Run inside the isolated Linux builder as root.')
    require(not os.environ.get('SSH_CONNECTION') and not os.environ.get('SSH_TTY'),
            'Direct SSH installation is refused; use the bound factory driver.')
    device = profiles.device_for(profile, allocation)
    info = os.lstat(device)
    root = json.loads(run('findmnt', '--json', '--target', '/', '--output', 'SOURCE,FSTYPE,TARGET,UUID'))
    columns = 'NAME,TYPE,SIZE,MOUNTPOINTS,FSTYPE,RO,LABEL,UUID,PKNAME,MAJ:MIN'
    blocks = json.loads(run('lsblk', '--json', '--bytes', '--paths', '--output', columns, device))
    inventory = json.loads(run('lsblk', '--json', '--bytes', '--paths', '--output', columns))
    signatures = json.loads(run('wipefs', '--no-act', '--json', device))
    mounts = json.loads(run('findmnt', '--json', '--list', '--output', 'SOURCE,TARGET,FSTYPE'))
    return {'system': platform.system(), 'arch': platform.machine(), 'uid': os.geteuid(),
            'hostname': socket.gethostname(), 'native_console': sys.stdin.isatty() and sys.stdout.isatty(), 'virtualization': run('systemd-detect-virt', '--vm'),
            'root': root, 'zram_root': collect_zram_root(root), 'blocks': blocks,
            'inventory': inventory, 'signatures': signatures, 'mounts': mounts,
            'disk_is_block': stat.S_ISBLK(info.st_mode), 'disk_realpath': os.path.realpath(device),
            'target_empty': not TARGET.exists() or (TARGET.is_dir() and not any(TARGET.iterdir())),
            'target_is_symlink': TARGET.is_symlink(), 'loop': loop_evidence(allocation) if allocation else None,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'device_id': info.st_rdev}


def validate_live(snapshot, context=None, *, profile, run_binding, profile_sha256, media_sha256, allocation=None):
    profiles.validate_run(run_binding, profile, profile_sha256, media_sha256, run_binding.get('logUuid'))
    device = profiles.device_for(profile, allocation)
    require(snapshot.get('system') == 'Linux' and snapshot.get('arch') == profile['arch'], 'Wrong platform or architecture.')
    require(snapshot.get('uid') == 0, 'Root is required in the isolated builder.')
    require(snapshot.get('hostname') == profile['vmName'] + '-live', 'Live builder hostname differs from the run profile.')
    require(snapshot.get('virtualization') in profile['hypervisors'], 'Virtualization is outside the profile.')
    require(profiles.uuid_text(snapshot.get('boot_id')) and type(snapshot.get('device_id')) is int
            and snapshot['device_id'] > 0, 'Missing kernel boot or block identity.')
    if profile['kind'] == 'qualification':
        require(snapshot.get('native_console') is True, 'Qualification requires a native console.')
    validate_live_root(snapshot, profile)
    require(snapshot.get('disk_is_block') is True and snapshot.get('disk_realpath') == device, 'Target is not its exact block device.')
    disks = snapshot.get('blocks', {}).get('blockdevices', [])
    require(len(disks) == 1, 'Expected exactly one selected target.')
    disk = disks[0]
    require(disk.get('name') == device and disk.get('type') == ('disk' if allocation is None else 'loop'), 'Unexpected target type.')
    require(type(disk.get('size')) is int and disk['size'] == profile['disk']['bytes'], 'Exact disk size differs.')
    require(disk.get('ro') in (False, 0), 'Target is read-only or unknown.')
    require(not disk.get('children') and not disk.get('fstype') and not any(disk.get('mountpoints') or []),
            'Refusing a partitioned, formatted or mounted target.')
    require(snapshot.get('signatures', {}).get('signatures') == [], 'Existing or unknown signatures.')
    require(snapshot.get('target_empty') is True and snapshot.get('target_is_symlink') is False,
            'Install directory must be empty and not linked.')
    mounts = snapshot.get('mounts', {}).get('filesystems')
    require(type(mounts) is list and mounts, 'Mount inventory is missing.')
    for row in mounts:
        source, target = str(row.get('source', '')), str(row.get('target', ''))
        require(not source.startswith(device), 'Target or a child is mounted.')
        require(target != str(TARGET) and not target.startswith(str(TARGET) + '/'), 'Install directory contains a mount.')
    validate_exclusions(snapshot, device, run_binding)
    if allocation is not None:
        validate_loop(snapshot, allocation, run_binding)
    if context is not None:
        expected = {'schema': 'decrypt.live-context.v2', 'vm_name': profile['vmName'],
                    'profile_sha256': profile_sha256, 'run_id': run_binding['runId'],
                    'disk': device, 'disk_bytes': profile['disk']['bytes'],
                    'boot_id': snapshot['boot_id'], 'device_id': snapshot['device_id'],
                    'user': profile['user'], 'password': profile['password']}
        require(all(context.get(key) == value for key, value in expected.items()), 'Context does not match this run, profile and boot.')
    return True


def check_installed():
    require(platform.system() == 'Linux' and os.geteuid() == 0, 'Use root inside the guarded installed guest.')
    context = read_private_context(INSTALLED_CONTEXT)
    require(context.get('schema') == 'decrypt.installed-context.v2', 'Legacy or missing installed context is refused.')
    info = INSTALLED_PROFILE.lstat()
    require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o644, 'Installed profile must be root-owned mode 0644.')
    profile = profiles.load(INSTALLED_PROFILE, context.get('profile_sha256'))
    require(platform.machine() == profile['arch'] and socket.gethostname() == profile['hostname'], 'Installed platform/hostname differs.')
    require(context.get('vm_name') == profile['vmName'] and context.get('user') == profile['user']
            and context.get('disk_bytes') == profile['disk']['bytes']
            and context.get('password') == profile['password'], 'Installed profile binding differs.')
    require(profiles.uuid_text(context.get('run_id')), 'Installed run identity missing.')
    root_uuid = run('findmnt', '--noheadings', '--output', 'UUID', '--target', '/')
    require(bool(root_uuid) and root_uuid == context.get('root_uuid'), 'Installed root identity differs.')
    if profile['kind'] == 'image-build':
        require(context.get('customer_account_created') is False, 'Factory images must defer customer creation until first boot.')
    return {**context, 'profile': profile}


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--installed-check', action='store_true')
    parser.add_argument('--verify-blank-bytes', action='store_true')
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--profile-sha256')
    parser.add_argument('--media-sha256')
    parser.add_argument('--run-binding', type=Path)
    parser.add_argument('--allocation', type=Path)
    args = parser.parse_args()
    if args.installed_check:
        context = check_installed()
        print(json.dumps({'guard': 'passed', 'profileId': context['profile']['id']}))
        return
    require(args.profile and args.profile_sha256 and args.media_sha256 and args.run_binding,
            'An authenticated profile, media hash and C5 run binding are required; legacy invocation is retired.')
    profile = profiles.load(args.profile, args.profile_sha256)
    binding = profiles.strict_json(profiles.read(args.run_binding))
    allocation = read_private_context(args.allocation) if args.allocation else None
    context = read_private_context(LIVE_CONTEXT)
    kwargs = dict(profile=profile, run_binding=binding, profile_sha256=args.profile_sha256,
                  media_sha256=args.media_sha256, allocation=allocation)
    snapshot = collect_live(profile, allocation)
    validate_live(snapshot, context, **kwargs)
    if args.verify_blank_bytes:
        verify_blank_disk(snapshot, profile, allocation)
        validate_live(collect_live(profile, allocation), context, **kwargs)
    print(json.dumps({'guard': 'passed', 'profileId': profile['id'], 'disk': profiles.device_for(profile, allocation)}))


if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
