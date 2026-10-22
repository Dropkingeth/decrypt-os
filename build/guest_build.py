#!/usr/bin/env python3
"""Build the Decrypt OS disk image (ARM64 or x86_64) inside a Linux builder.

Run as root on a Linux builder of the target architecture (--arch, default
aarch64), inside a private mount namespace (build.sh and build-x86_64.sh do
this with `unshare --mount --propagation private`). The architecture selects
one entry of TARGETS below, which names the image-factory profile, package
lock, mirrors, bootloader and kernel file names and export formats.

Stages, in order:
  disk      sparse raw disk, GPT (EFI + btrfs root), filesystems mounted
  base      every OS package (base + desktop) installed with pacstrap
  desktop   system config, systemd-boot, Omarchy desktop, theme and greeter
  app       Decrypt app + runtime (Node, OpenClaw, Python venv) built and installed
  finalize  first-start service, identity cleanup, checks, unmount -> bootable raw disk
  export    qcow2 + UTM .tar (aarch64) or qcow2 + VMDK (x86_64) + SHA256SUMS

The image is account-free: no user, no password, no machine or SSH identity,
no pacman keyring and no customer configuration. First start creates them.
Signing is not done here; build.sh signs MANIFEST.json on the host.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import time
import types
import urllib.request
import uuid

STAGES = ('disk', 'base', 'desktop', 'app', 'finalize', 'export')
DISK_BYTES = 40 * 1024**3
EFI_SECTORS = 2097152  # 1 GiB
HOSTNAME = 'decrypt'
# HTTPS only: an http mirror lets a network-path attacker roll the package set
# back to older, still-signed packages (CVE-bearing but signature-valid). Three
# independent mirrors with working HTTPS certificates, so one outage does not
# force a fallback to plain HTTP. Checked manually against each mirror's own
# TLS certificate; archlinuxarm.org does not publish an aggregate HTTPS list.
# pacman tries them top to bottom; the order says nothing about where the image is built.
ALARM_MIRRORS = (
    'https://de3.mirror.archlinuxarm.org/$arch/$repo',
    'https://fl.us.mirror.archlinuxarm.org/$arch/$repo',
    'https://ca.us.mirror.archlinuxarm.org/$arch/$repo',
)
# Arch Linux (x86_64) mirrors, HTTPS only for the same reason as above. These
# go into the shipped image's /etc/pacman.d/mirrorlist. Build-time installs
# for x86_64 use the dated archive snapshot pinned in build/inputs-x86_64.json
# instead, so an --online build resolves the same package set every time.
ARCH_MIRRORS = (
    'https://geo.mirror.pkgbuild.com/$repo/os/$arch',
    'https://mirrors.kernel.org/archlinux/$repo/os/$arch',
    'https://mirror.rackspace.com/archlinux/$repo/os/$arch',
)
IMAGE_ID = re.compile(r'[a-z0-9][a-z0-9._-]{0,95}\Z')

# One entry per supported image architecture. aarch64 is the original target and
# the default everywhere; its values are exactly what this file hardcoded before
# x86_64 support existed.
TARGETS = {
    'aarch64': {
        'profile': 'candidate/profiles/arm64-image-factory.json',
        'lock': 'build/arch-package-lock.json',
        'inputs': None,
        'mirrors': ALARM_MIRRORS,
        'repos': ('core', 'extra', 'alarm'),
        'mirrorlistHeader': '# Decrypt OS: HTTPS Arch Linux ARM mirrors only. See build/guest_build.py.\n',
        'kernelImage': 'Image',
        'microcode': (),
        'efiStub': 'systemd-bootaa64.efi',
        'efiFallback': 'BOOTAA64.EFI',
        'portableInitramfs': False,
        'node': 'node-v24.19.0-linux-arm64',
        'importDoc': 'build/IMPORT.md',
        'formats': ('qcow2', 'utm.tar'),
    },
    'x86_64': {
        'profile': 'candidate/profiles/x86-64-image-factory.json',
        'lock': 'build/arch-package-lock-x86_64.json',
        'inputs': 'build/inputs-x86_64.json',
        'mirrors': ARCH_MIRRORS,
        'repos': ('core', 'extra'),
        'mirrorlistHeader': '# Decrypt OS: HTTPS Arch Linux mirrors only. See build/guest_build.py.\n',
        'kernelImage': 'vmlinuz-linux',
        # Early microcode, loaded before the main initramfs when present.
        'microcode': ('intel-ucode.img', 'amd-ucode.img'),
        'efiStub': 'systemd-bootx64.efi',
        'efiFallback': 'BOOTX64.EFI',
        # Real PCs and VirtualBox/VMware use other storage/GPU drivers than the
        # builder VM: build the initramfs without mkinitcpio's autodetect hook.
        'portableInitramfs': True,
        'node': 'node-v24.19.0-linux-x64',
        'importDoc': 'build/IMPORT-x86_64.md',
        'formats': ('qcow2', 'vmdk'),
    },
}


def target_config(arch='aarch64'):
    if arch not in TARGETS:
        raise SystemExit(f'unsupported --arch {arch!r}; choose one of: ' + ', '.join(TARGETS))
    return TARGETS[arch]


def build_mirrors(arch, src=None):
    """Servers pacman uses at build time with --online. aarch64: the ALARM
    mirrors. x86_64: the dated archive snapshot from build/inputs-x86_64.json
    (when the source tree is known), so the package set is reproducible."""
    target = target_config(arch)
    if target['inputs'] and src is not None:
        pins = json.loads((Path(src) / target['inputs']).read_text())['archlinux']
        return (pins['packageSnapshot'].rstrip('/') + '/$repo/os/$arch',)
    return target['mirrors']


def log(message):
    print(time.strftime('%H:%M:%S'), message, flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_package_list(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            rows.append(line)
    return rows


def pacman_conf(repo_dir, online, arch='aarch64', mirrors=None):
    """pacman.conf used by pacstrap inside the tool root."""
    target = target_config(arch)
    head = (f'[options]\nArchitecture = {arch}\nSigLevel = Required DatabaseOptional\n'
            'LocalFileSigLevel = Optional\nParallelDownloads = 5\n'
            f'CacheDir = {repo_dir}/\n')
    if online:
        servers = ''.join(f'Server = {mirror}\n' for mirror in (mirrors or target['mirrors']))
        return head + ''.join(f'\n[{repo}]\n{servers}' for repo in target['repos'])
    return head + f'\n[decrypt-offline]\nSigLevel = PackageRequired DatabaseNever\nServer = file://{repo_dir}\n'


def mirrorlist(arch='aarch64'):
    """/etc/pacman.d/mirrorlist shipped in the image: HTTPS only, same mirrors
    build-time pacman uses. `pacstrap -M` would otherwise keep the stock
    pacman-mirrorlist package's default (plain http://mirror.archlinuxarm.org),
    so any later `pacman -Syu` the customer runs would fall back to HTTP."""
    target = target_config(arch)
    lines = [target['mirrorlistHeader']]
    lines += [f'Server = {mirror}\n' for mirror in target['mirrors']]
    return ''.join(lines)


USER_TIMERS = ('decrypt-desktop-layout.timer', 'decrypt-tmp-janitor.timer')

SWAP_PATH = '/var/lib/decrypt-swap/swapfile'
# Created on first boot (not baked into the image) so the download stays small.
# btrfs needs NODATACOW preallocation, which `btrfs filesystem mkswapfile` does.
SWAP_UNIT = ('[Unit]\nDescription=Decrypt OS swapfile\n'
             'After=local-fs.target\nRequiresMountsFor=/var/lib\n\n'
             '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
             'ExecStart=/bin/sh -c "[ -e ' + SWAP_PATH + ' ] || { install -d -m 0700 /var/lib/decrypt-swap && '
             'btrfs filesystem mkswapfile --size 4G ' + SWAP_PATH + '; }; swapon ' + SWAP_PATH + '"\n'
             'ExecStop=/sbin/swapoff ' + SWAP_PATH + '\n\n'
             '[Install]\nWantedBy=multi-user.target\n')

# The image is DISK_BYTES; a VM given a bigger disk gets the rest at boot. systemd-repart grows
# the last partition (ROOT, the only linux-generic one) into free space at the end of the boot
# disk; it never moves, shrinks or deletes a partition. Then btrfs grows online. Both steps are
# no-ops once root fills the disk, so the unit runs on every boot and also after a later resize.
REPART_DIR = '/usr/lib/decrypt-repart.d'
REPART_ROOT = '[Partition]\nType=linux-generic\n'
GROW_ROOT_UNIT = ('[Unit]\nDescription=Decrypt OS: grow the root partition to fill the disk\n'
                  'After=local-fs.target\n\n'
                  '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
                  'ExecStart=/usr/bin/systemd-repart --dry-run=no --definitions=' + REPART_DIR + '\n'
                  'ExecStart=/usr/bin/btrfs filesystem resize max /\n\n'
                  '[Install]\nWantedBy=multi-user.target\n')


def fstab(root_uuid, efi_uuid):
    return (f'UUID={root_uuid}\t/\tbtrfs\trw,relatime\t0 0\n'
            f'UUID={efi_uuid}\t/boot\tvfat\trw,relatime,fmask=0077,dmask=0077\t0 2\n')


def boot_entry(root_uuid, arch='aarch64', microcode=()):
    """systemd-boot entry. microcode: early-microcode initrds present in /boot
    (x86_64), listed before the main initramfs as systemd-boot requires."""
    target = target_config(arch)
    initrds = ''.join(f'initrd /{name}\n' for name in microcode)
    return (f'title Decrypt OS\nlinux /{target["kernelImage"]}\n{initrds}initrd /initramfs-linux.img\n'
            f'options root=UUID={root_uuid} rw rootwait quiet loglevel=3 systemd.show_status=false rd.udev.log_level=3\n')


def portable_hooks(mkinitcpio_conf):
    """HOOKS=(...) from the image's /etc/mkinitcpio.conf without `autodetect`,
    as a drop-in line. autodetect keeps only the modules the *builder* VM uses;
    an x86_64 image must also boot on real PCs and other hypervisors."""
    rows = [line for line in mkinitcpio_conf.splitlines() if re.match(r'\s*HOOKS=\(', line)]
    if len(rows) != 1:
        raise SystemExit('expected exactly one active HOOKS=(...) line in /etc/mkinitcpio.conf')
    hooks = re.match(r'\s*HOOKS=\(([^)]*)\)', rows[0]).group(1).split()
    if 'autodetect' not in hooks:
        raise SystemExit('mkinitcpio HOOKS has no autodetect hook; review portable_hooks()')
    return 'HOOKS=(' + ' '.join(h for h in hooks if h != 'autodetect') + ')\n'


def vmdk_convert_argv(raw, vmdk):
    """Sparse single-file VMDK (monolithicSparse): VirtualBox and VMware
    Workstation attach it directly as an existing, writable disk."""
    return ['qemu-img', 'convert', '-p', '-f', 'raw', '-O', 'vmdk', '-o', 'subformat=monolithicSparse', raw, vmdk]


def utm_config(image_id):
    """UTM 4.x (QEMU backend) config: ARM64 virt, UEFI, VirtIO disk/GPU/network.

    UTM decodes every key below as required (Configuration/*.swift in UTM 4.7).
    A missing key makes UTM silently skip the VM, so keep the full set.
    """
    template = str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://decryptlabs.io/vm-template/' + image_id)).upper()
    mac = '52:54:00:' + ':'.join(template.replace('-', '')[i:i + 2] for i in (0, 2, 4)).lower()
    return plistlib.dumps({
        'Backend': 'QEMU', 'ConfigurationVersion': 4,
        'Information': {'Name': 'Decrypt OS', 'UUID': template, 'Icon': 'linux', 'IconCustom': False,
                        'Notes': 'Decrypt OS ' + image_id},
        'System': {'Architecture': 'aarch64', 'Target': 'virt', 'CPU': 'default', 'CPUFlagsAdd': [],
                   'CPUFlagsRemove': [], 'CPUCount': 4, 'ForceMulticore': False, 'MemorySize': 8192,
                   'JITCacheSize': 0},
        'QEMU': {'DebugLog': False, 'UEFIBoot': True, 'RNGDevice': True, 'BalloonDevice': False,
                 'TPMDevice': False, 'Hypervisor': True, 'TSO': False, 'RTCLocalTime': False,
                 'PS2Controller': False, 'AdditionalArguments': []},
        'Input': {'UsbBusSupport': '3.0', 'UsbSharing': False, 'MaximumUsbShare': 3},
        'Sharing': {'DirectoryShareMode': 'None', 'DirectoryShareReadOnly': False, 'ClipboardSharing': True},
        'Display': [{'Hardware': 'virtio-gpu-gl-pci', 'DynamicResolution': True, 'NativeResolution': False,
                     'UpscalingFilter': 'Linear', 'DownscalingFilter': 'Linear'}],
        'Drive': [{'Identifier': 'system', 'ImageName': 'disk.qcow2', 'ImageType': 'Disk',
                   'Interface': 'VirtIO', 'InterfaceVersion': 1, 'ReadOnly': False}],
        'Network': [{'Mode': 'Shared', 'Hardware': 'virtio-net-pci', 'MacAddress': mac,
                     'IsolateFromHost': False, 'PortForward': []}],
        'Serial': [],
        'Sound': [],
    }, fmt=plistlib.FMT_XML, sort_keys=True)


def write_utm_tar(path, qcow, image_id, import_text, epoch):
    """Decrypt-OS.utm/{config.plist,Data/disk.qcow2,IMPORT.md} with fixed owner/mtime."""
    def member(name, size):
        info = tarfile.TarInfo('Decrypt-OS.utm/' + name)
        info.size, info.mode, info.mtime = size, 0o644, epoch
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info
    config = utm_config(image_id)
    with tarfile.open(path, 'w', format=tarfile.GNU_FORMAT) as archive:
        archive.addfile(member('Data/disk.qcow2', Path(qcow).stat().st_size), open(qcow, 'rb'))
        archive.addfile(member('IMPORT.md', len(import_text)), io.BytesIO(import_text))
        archive.addfile(member('config.plist', len(config)), io.BytesIO(config))


def diff_package_lock(lock_packages, installed):
    """lock_packages: build/arch-package-lock.json's "packages" list.
    installed: {name: version} from `pacman -Q` in the built image.
    Returns (unlocked, mismatched), both sorted name lists."""
    by_name = {row['name']: row for row in lock_packages}
    unlocked = sorted(name for name in installed if name not in by_name)
    mismatched = sorted(name for name, version in installed.items()
                         if name in by_name and by_name[name]['version'] != version)
    return unlocked, mismatched


def sha256sums(directory, names):
    lines = [f'{sha256_file(Path(directory) / name)}  {name}\n' for name in sorted(names)]
    (Path(directory) / 'SHA256SUMS').write_text(''.join(lines))
    return lines


def npm_archive_name(url):
    """Cache file name used by factory/builder/provision_runtime.py."""
    return hashlib.sha256(url.encode()).hexdigest() + '.tgz'


def missing_npm_archives(lock, archives):
    missing = []
    for name, row in lock.get('packages', {}).items():
        if not name or 'resolved' not in row:
            continue
        if not (Path(archives) / npm_archive_name(row['resolved'])).is_file():
            missing.append((row['resolved'], row['integrity']))
    return missing


# The image carries the project license and the third-party notice summary
# where Arch keeps package licenses. Component licenses stay beside their
# components under /usr/local/lib/decrypt-os (UI notices, font and skill licenses).
LICENSE_FILES = ('LICENSE', 'NOTICES.md')


def stage_license_notices(src, target):
    """Copy the repository LICENSE (AGPL-3.0) and NOTICES.md into the image."""
    out = Path(target) / 'usr/share/licenses/decrypt-os'
    out.mkdir(parents=True, exist_ok=True)
    for name in LICENSE_FILES:
        data = (Path(src) / name).read_bytes()
        if not data.strip():
            raise SystemExit(f'{name} is empty; the image must ship the license and notices')
        (out / name).write_bytes(data)
        os.chmod(out / name, 0o644)
    return [str(out / name) for name in LICENSE_FILES]


def stage_diagnostics_tool(src, target):
    """Copy the read-only support-diagnostics script to the installed path its
    own docs and the bug-report issue template reference (candidate/tools/ is
    a dev-tree-only prefix, stripped like every other installed-path doc)."""
    data = (Path(src) / 'candidate/tools/diagnostics.py').read_bytes()
    if not data.strip():
        raise SystemExit('candidate/tools/diagnostics.py is empty; the image must ship the diagnostics tool')
    out = Path(target) / 'usr/local/lib/decrypt-os/tools'
    out.mkdir(parents=True, exist_ok=True)
    destination = out / 'diagnostics.py'
    destination.write_bytes(data)
    os.chmod(destination, 0o755)
    return str(destination)


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could point the --online npm fetch at a non-pinned host
    (registry.npmjs.org's own check above only looks at the *original* URL)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit('Refusing npm archive redirect: ' + newurl)


def fetch_npm_archive(url, integrity, archives):
    if not url.startswith('https://registry.npmjs.org/'):
        raise SystemExit('Refusing npm archive outside registry.npmjs.org: ' + url)
    algorithm, encoded = integrity.split('-', 1)
    if algorithm != 'sha512':
        # npm lockfile integrity can be sha1/sha256/sha512; only accept the
        # strongest so a weaker-hash lock entry can't be used to smuggle in a
        # second, unreviewed archive with a colliding weak digest.
        raise SystemExit('Refusing npm archive integrity algorithm other than sha512: ' + url)
    # ProxyHandler({}): ignore HTTP(S)_PROXY/env proxy config -- this fetch is
    # pinned to one exact host and must not be redirected through anything else.
    opener = urllib.request.build_opener(RefuseRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=120) as response:
        raw = response.read()
    if hashlib.new(algorithm, raw).digest() != base64.b64decode(encoded):
        raise SystemExit('npm archive integrity mismatch: ' + url)
    target = Path(archives) / npm_archive_name(url)
    target.write_bytes(raw)
    target.chmod(0o644)


class Builder:
    def __init__(self, args):
        self.args = args
        self.src = Path(args.source).resolve()
        self.cache = Path(args.cache).resolve()
        self.work = Path(args.work).resolve()
        self.image_id = args.image_id
        self.arch = getattr(args, 'arch', 'aarch64')
        self.config = target_config(self.arch)
        self.lock_path = self.src / self.config['lock']
        self.toolroot = self.work / 'toolroot'
        self.target = self.toolroot / 'mnt/target'   # host path; /mnt/target inside the tool root
        self.raw = self.work / (self.image_id + '.raw')
        self.release = self.work / 'release'
        self.repo = self.cache / 'pkg' / self.arch
        self.loop = None
        self.mounts = []
        self.timings = {}

    # ---- helpers -------------------------------------------------------
    def run(self, argv, **kwargs):
        log('$ ' + ' '.join(str(a) for a in argv))
        kwargs.setdefault('check', True)
        return subprocess.run([str(a) for a in argv], **kwargs)

    def out(self, argv):
        return self.run(argv, capture_output=True, text=True).stdout.strip()

    def mount(self, args, target):
        Path(target).mkdir(parents=True, exist_ok=True)
        self.run(['mount', *args, target])
        self.mounts.append(Path(target))

    def umount(self, target):
        self.run(['umount', '-R', target])
        self.mounts.remove(Path(target))

    def tool(self, argv, **kwargs):
        """Run inside the Arch Linux (ARM) tool root (pacstrap, arch-chroot, repo-add)."""
        env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin', 'LANG': 'C.UTF-8', 'HOME': '/root'}
        return self.run(['chroot', self.toolroot, *argv], env=env, **kwargs)

    def chroot(self, argv, **kwargs):
        """Run inside the new image (arch-chroot mounts /proc, /sys, /dev for us)."""
        return self.tool(['arch-chroot', '/mnt/target', *argv], **kwargs)

    def cleanup(self):
        for target in reversed(list(self.mounts)):
            subprocess.run(['umount', '-R', '-l', str(target)], check=False)
        self.mounts.clear()
        if self.loop:
            subprocess.run(['losetup', '-d', self.loop], check=False)
            self.loop = None

    # ---- stages --------------------------------------------------------
    def stage_disk(self):
        if self.raw.exists():
            raise SystemExit(f'{self.raw} already exists; use a new --work directory')
        with open(self.raw, 'wb') as f:
            f.truncate(DISK_BYTES)
        self.loop = self.out(['losetup', '--find', '--show', '--partscan', self.raw])
        layout = f'label: gpt\nstart=2048, size={EFI_SECTORS}, type=U, name="EFI"\ntype=L, name="ROOT"\n'
        self.run(['sfdisk', '--quiet', self.loop], input=layout.encode())
        self.run(['partx', '-u', self.loop], check=False)
        self.run(['udevadm', 'settle'])
        efi, root = self.loop + 'p1', self.loop + 'p2'
        self.run(['mkfs.fat', '-F', '32', '-n', 'DECRYPT_EFI', efi])
        self.run(['mkfs.btrfs', '-q', '-L', 'DECRYPT_ROOT', root])
        self.efi_uuid = self.out(['blkid', '-s', 'UUID', '-o', 'value', efi])
        self.root_uuid = self.out(['blkid', '-s', 'UUID', '-o', 'value', root])
        # Tool root: throwaway overlay over the cached Arch Linux (ARM) root.
        for name in ('toolroot-upper', 'toolroot-work'):
            (self.work / name).mkdir()
        self.mount(['-t', 'overlay', 'overlay', '-o',
                    f'lowerdir={self.cache / "toolroot"},upperdir={self.work / "toolroot-upper"},workdir={self.work / "toolroot-work"}'],
                   self.toolroot)
        self.mount(['--rbind', '/dev'], self.toolroot / 'dev')
        self.mount(['-t', 'proc', 'proc'], self.toolroot / 'proc')
        self.mount(['--rbind', '/sys'], self.toolroot / 'sys')
        self.mount(['-t', 'tmpfs', '-o', 'mode=755', 'tmpfs'], self.toolroot / 'run')
        self.mount(['-t', 'tmpfs', '-o', 'mode=1777', 'tmpfs'], self.toolroot / 'tmp')
        resolv = self.toolroot / 'etc/resolv.conf'
        if resolv.is_symlink() or resolv.exists():
            resolv.unlink()
        resolv.write_text('nameserver 1.1.1.1\nnameserver 8.8.8.8\n')
        self.mount([root], self.target)
        (self.target / 'boot').mkdir(mode=0o700)
        self.mount(['-o', 'fmask=0077,dmask=0077', efi], self.target / 'boot')

    def stage_base(self):
        profile = json.loads((self.src / self.config['profile']).read_text())
        if profile['arch'] != self.arch:
            raise SystemExit(f"{self.config['profile']} is for {profile['arch']}, not {self.arch}")
        packages = profile['basePackages']
        desktop = read_package_list(self.src / 'candidate/base/desktop-packages.txt')
        repo_inside = '/var/cache/decrypt-repo'
        self.mount(['--bind', self.repo], self.toolroot / repo_inside.lstrip('/'))
        if not self.args.online:
            self.run(['mount', '-o', 'remount,bind,ro', self.toolroot / repo_inside.lstrip('/')])
        mirrors = build_mirrors(self.arch, self.src)
        (self.toolroot / 'etc/pacman-build.conf').write_text(pacman_conf(repo_inside, self.args.online, self.arch, mirrors))
        # -c: use the cache/repo dir directly (nothing copied into the image)
        # -G: do not copy the tool root keyring; the image gets its own at first boot
        # -M: keep the stock mirrorlist from the pacman-mirrorlist package (overwritten below)
        self.tool(['pacstrap', '-C', '/etc/pacman-build.conf', '-c', '-G', '-M', '/mnt/target',
                   *dict.fromkeys(packages + desktop)])
        if self.args.online:
            self.refresh_offline_repo(repo_inside)
        # pacstrap -M ships the pacman-mirrorlist package's own default (plain
        # http://mirror.archlinuxarm.org). Replace it so any later `pacman -Syu`
        # the customer runs also stays on HTTPS.
        (self.target / 'etc/pacman.d/mirrorlist').write_text(mirrorlist(self.arch))
        self.umount(self.toolroot / repo_inside.lstrip('/'))

    def refresh_offline_repo(self, repo_inside):
        """After an online install, rebuild decrypt-offline.db from exactly what was installed."""
        files = []
        for line in self.out(['chroot', self.target, 'pacman', '-Q']).splitlines():
            name, version = line.split(' ', 1)
            found = sorted(p.name for p in self.repo.glob(f'{name}-{version}-*.pkg.tar.*') if not p.name.endswith('.sig'))
            if not found:
                raise SystemExit(f'Installed package not in cache: {name} {version}')
            files.append(found[0])
        for name in ('decrypt-offline.db', 'decrypt-offline.db.tar.gz', 'decrypt-offline.files', 'decrypt-offline.files.tar.gz'):
            (self.repo / name).unlink(missing_ok=True)
        self.tool(['repo-add', '-q', f'{repo_inside}/decrypt-offline.db.tar.gz',
                   *[f'{repo_inside}/{name}' for name in files]])
        for name in ('db', 'files'):
            link = self.repo / f'decrypt-offline.{name}'
            if link.is_symlink():
                link.unlink()
                shutil.copyfile(self.repo / f'decrypt-offline.{name}.tar.gz', link)

    def stage_desktop(self):
        t = self.target
        (t / 'etc/fstab').write_text(fstab(self.root_uuid, self.efi_uuid))
        (t / 'etc/hostname').write_text(HOSTNAME + '\n')
        (t / 'etc/systemd/system/decrypt-swapfile.service').write_text(SWAP_UNIT)
        self.chroot(['systemctl', 'enable', 'decrypt-swapfile.service'])
        (t / REPART_DIR.lstrip('/')).mkdir(parents=True, exist_ok=True)
        (t / REPART_DIR.lstrip('/') / '50-root.conf').write_text(REPART_ROOT)
        (t / 'etc/systemd/system/decrypt-grow-root.service').write_text(GROW_ROOT_UNIT)
        self.chroot(['systemctl', 'enable', 'decrypt-grow-root.service'])
        (t / 'etc/hosts').write_text(f'127.0.0.1 localhost\n::1 localhost\n127.0.1.1 {HOSTNAME}\n')
        (t / 'etc/locale.conf').write_text('LANG=en_US.UTF-8\n')
        gen = t / 'etc/locale.gen'
        gen.write_text(gen.read_text().replace('#en_US.UTF-8 UTF-8', 'en_US.UTF-8 UTF-8'))
        self.chroot(['locale-gen'])
        self.chroot(['passwd', '-l', 'root'])
        sudoers = t / 'etc/sudoers.d/10-decrypt-local-admin'
        sudoers.write_text('%wheel ALL=(ALL:ALL) ALL\n')
        sudoers.chmod(0o440)
        self.chroot(['visudo', '-c'])
        cfg = self.config
        if cfg['portableInitramfs']:
            dropin = t / 'etc/mkinitcpio.conf.d/decrypt-portable.conf'
            dropin.parent.mkdir(parents=True, exist_ok=True)
            dropin.write_text('# Decrypt OS: no autodetect, so the image boots on any x86_64 PC or hypervisor.\n'
                              + portable_hooks((t / 'etc/mkinitcpio.conf').read_text()))
            self.chroot(['mkinitcpio', '-P'])
        # systemd-boot as the removable-media fallback loader (no NVRAM writes needed).
        efi = t / 'usr/lib/systemd/boot/efi' / cfg['efiStub']
        for dest in ('boot/EFI/BOOT/' + cfg['efiFallback'], 'boot/EFI/systemd/' + cfg['efiStub']):
            (t / dest).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(efi, t / dest)
        microcode = tuple(name for name in cfg['microcode'] if (t / 'boot' / name).is_file())
        (t / 'boot/loader/entries').mkdir(parents=True, exist_ok=True)
        (t / 'boot/loader/loader.conf').write_text('default arch.conf\ntimeout 2\neditor no\n')
        (t / 'boot/loader/entries/arch.conf').write_text(boot_entry(self.root_uuid, self.arch, microcode))
        for name in (cfg['kernelImage'], 'initramfs-linux.img'):
            if not (t / 'boot' / name).is_file():
                raise SystemExit('missing boot file: /boot/' + name)
        self.install_omarchy_desktop()

    def install_omarchy_desktop(self):
        """Reuse candidate/base/unattended/phase_b.py (Omarchy, skeleton, theme, greeter)."""
        pins = json.loads((self.src / 'build/inputs.json').read_text())['omarchy']
        archive = self.cache / 'omarchy' / pins['commit'] / 'omarchy.tar'
        if sha256_file(archive) != pins['archiveSha256']:
            raise SystemExit('Omarchy archive does not match build/inputs.json')
        # phase_b reads its inputs from a root-owned "media" tree.
        media = self.work / 'media'
        (media / 'installer').mkdir(parents=True)
        shutil.copytree(self.src / 'candidate/base', media / 'installer/base')
        (media / 'omarchy').mkdir()
        shutil.copyfile(archive, media / 'omarchy/omarchy.tar')
        sys.path.insert(0, str(self.src / 'candidate/base/unattended'))
        sys.modules['driver'] = types.SimpleNamespace(TARGET=self.target)
        phase_b = load_module('phase_b', self.src / 'candidate/base/unattended/phase_b.py')
        builder = self

        class Factory:
            manifest = {'omarchy': {'path': 'omarchy/omarchy.tar', 'sha256': pins['archiveSha256'], 'commit': pins['commit']}}
            inventory = {'omarchy/omarchy.tar': pins['archiveSha256']}
            profile = {'graphics': {'softwareRendering': False}}

            def __init__(self):
                self.media = media

            def chroot(self, argv):
                builder.chroot(argv)

            def command(self, argv):
                builder.run(argv)

            def no_customer(self):
                builder.check_no_customer()

        phase_b.install_desktop(Factory(), install_packages=False)
        self.run(['systemctl', '--root', self.target, 'enable', 'systemd-boot-update.service'])

    def stage_app(self):
        """Build the Decrypt app and runtime inside the new image (same Python ABI), then install."""
        runtime_inputs = self.cache / 'runtime-inputs'
        archives = self.cache / 'npm-archives'
        locks = [self.src / 'candidate/app-source/app/ui/package-lock.json', runtime_inputs / 'npm/package-lock.json']
        missing = [row for lock in locks for row in missing_npm_archives(json.loads(lock.read_text()), archives)]
        if missing and not self.args.online:
            raise SystemExit(f'{len(missing)} npm archives are not cached; rerun with --online')
        for url, integrity in dict.fromkeys(missing):
            log('fetch ' + url)
            fetch_npm_archive(url, integrity, archives)
        inside = self.target / 'var/tmp/decrypt-build'
        inside.mkdir(mode=0o700)
        for source, name in ((self.src, 'src'), (runtime_inputs, 'runtime-inputs'), (archives, 'npm-archives')):
            self.mount(['--bind', source], inside / name)
            self.run(['mount', '-o', 'remount,bind,ro', inside / name])
        (inside / 'work').mkdir()
        try:
            self.chroot(['/usr/bin/python3', '-I', '-B', '/var/tmp/decrypt-build/src/build/target_app.py',
                         '--src', '/var/tmp/decrypt-build/src', '--runtime-inputs', '/var/tmp/decrypt-build/runtime-inputs',
                         '--npm-archives', '/var/tmp/decrypt-build/npm-archives', '--work', '/var/tmp/decrypt-build/work'])
        finally:
            for name in ('src', 'runtime-inputs', 'npm-archives'):
                if inside / name in self.mounts:
                    self.umount(inside / name)
        shutil.rmtree(inside)
        # The desktop database lists the decrypt-os:// and decrypt-advanced:// handlers
        # (the Bridge window's links) so the desktop portal's app chooser finds them.
        self.chroot(['update-desktop-database', '/usr/share/applications'])
        # Every account's desktop: the layout timer opens (and reopens) the WS1-3
        # app windows and the WS4 assistant; the janitor keeps /tmp small. The
        # app's own API/UI services stay default-off and start on demand.
        self.run(['systemctl', '--root', self.target, '--global', 'enable', *USER_TIMERS])
        stage_license_notices(self.src, self.target)
        stage_diagnostics_tool(self.src, self.target)

    def check_lua(self):
        """Hyprland on Omarchy 4 reads Lua config. One syntax error leaves the
        user with an empty desktop, so every shipped .lua must parse."""
        luac = shutil.which('luac5.4') or shutil.which('luac')
        if not luac:
            raise SystemExit('luac not found in the builder (Ubuntu: apt install lua5.4; Arch: pacman -S lua54); '
                             'cannot check desktop config')
        roots = [self.target / 'etc/skel', self.target / 'usr/local/lib/decrypt-os']
        files = sorted(f for r in roots if r.exists() for f in r.rglob('*.lua') if f.is_file())
        for f in files:
            self.run([luac, '-p', f])
        print(f'lua syntax ok: {len(files)} files', flush=True)

    def stage_finalize(self):
        t = self.target
        self.stage_firstboot()
        # Package keyring: created per machine at first boot, never shipped.
        shutil.rmtree(t / 'etc/pacman.d/gnupg', ignore_errors=True)
        unit = t / 'etc/systemd/system/decrypt-pacman-keyring.service'
        unit.write_bytes((self.src / 'build/decrypt-pacman-keyring.service').read_bytes())
        self.run(['systemctl', '--root', t, 'enable', 'decrypt-pacman-keyring.service'])
        # Generalize: no machine id, SSH host keys, sync databases, caches or logs.
        (t / 'etc/machine-id').write_text('')
        dbus = t / 'var/lib/dbus/machine-id'
        if dbus.exists() and not dbus.is_symlink():
            dbus.unlink()
        for key in (t / 'etc/ssh').glob('ssh_host_*'):
            key.unlink()
        root_ssh = t / 'root/.ssh'
        if root_ssh.is_dir() and not any(root_ssh.iterdir()):
            root_ssh.rmdir()
        # Both are machine-specific secrets systemd writes while the tool root
        # or pacstrap chroot ran (entropy state, credential-encryption key).
        # Shipping either would let every image decrypt/predict the same
        # customer-facing secrets; first start regenerates them.
        (t / 'var/lib/systemd/random-seed').unlink(missing_ok=True)
        (t / 'var/lib/systemd/credential.secret').unlink(missing_ok=True)
        for directory in ('var/lib/pacman/sync', 'var/cache/pacman/pkg', 'var/log/journal', 'tmp', 'var/tmp'):
            path = t / directory
            if path.is_dir():
                for child in path.iterdir():
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        (t / 'var/log/pacman.log').unlink(missing_ok=True)
        self.check_lua()
        self.check_image()
        self.run(['fstrim', t / 'boot'], check=False)
        self.run(['fstrim', t])
        for target in reversed(list(self.mounts)):
            self.umount(target)
        self.run(['losetup', '-d', self.loop])
        self.loop = None
        log(f'bootable raw disk: {self.raw}')

    def stage_firstboot(self):
        """Reuse candidate/firstboot/stage.py: account setup happens on the customer's first start."""
        policy = self.src / 'candidate/firstboot/policy.json'
        stage = load_module('factory_firstboot_stage', self.src / 'candidate/firstboot/stage.py')
        bundle = stage.build_bundle(policy, sha256_file(policy), self.image_id)
        for relative, record in sorted(bundle.items()):
            path = self.target / relative
            if path.exists():
                raise SystemExit('first-start file already exists: ' + relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(record['bytes'])
            path.chmod(record['mode'])
        self.run(['systemctl', '--root', self.target, 'enable', stage.UNIT])

    def check_no_customer(self):
        for row in (self.target / 'etc/passwd').read_text().splitlines():
            fields = row.split(':')
            if 1000 <= int(fields[2]) < 65534:
                raise SystemExit('customer account present in image: ' + fields[0])
        root = [r for r in (self.target / 'etc/shadow').read_text().splitlines() if r.startswith('root:')]
        if len(root) != 1 or not root[0].split(':')[1].startswith(('!', '*')):
            raise SystemExit('root account is not locked')
        # !authenticate disables the password prompt the same way NOPASSWD does
        # (it is just the other sudoers spelling of the same tag), so it needs
        # the same refusal.
        passwordless = re.compile(r'NOPASSWD|!authenticate')
        for path in [self.target / 'etc/sudoers', *(self.target / 'etc/sudoers.d').glob('*')]:
            text = re.sub(r'\\\n', '', path.read_text())
            if any(passwordless.search(line) for line in text.splitlines() if not line.lstrip().startswith('#')):
                raise SystemExit('passwordless sudo rule found: ' + path.name)
        if any((self.target / 'home').iterdir()):
            raise SystemExit('/home is not empty')

    def check_package_lock(self):
        """Every package pacstrap actually installed must be the exact
        name/version/sha256 recorded in build/arch-package-lock.json (see
        build/README.md for how to regenerate it). This catches a mismatched
        offline cache or a mirror that served something other than what the
        lock says it reviewed, even though pacman's own signature check
        already passed."""
        name = self.config['lock']
        lock = json.loads(self.lock_path.read_text())
        if lock.get('arch') != self.arch:
            raise SystemExit(f'{name} is for {lock.get("arch")}, not {self.arch}')
        if not lock['packages']:
            raise SystemExit(f'{name} has no packages yet: generate it from the offline repo cache '
                             '(build/generate_package_lock.py, see build/README.md) and commit it')
        by_name = {row['name']: row for row in lock['packages']}
        installed = dict(line.split(' ', 1) for line in self.out(['chroot', self.target, 'pacman', '-Q']).splitlines())
        unlocked, mismatched = diff_package_lock(lock['packages'], installed)
        if unlocked:
            raise SystemExit(f'installed package(s) missing from {name}: ' + ', '.join(unlocked))
        if mismatched:
            raise SystemExit(f'installed package version(s) differ from {name}: ' + ', '.join(mismatched))
        for package, version in installed.items():
            row = by_name[package]
            found = sorted(p.name for p in self.repo.glob(f'{package}-{version}-*.pkg.tar.*') if not p.name.endswith('.sig'))
            if found and sha256_file(self.repo / found[0]) != row['sha256']:
                raise SystemExit(f'cached package differs from {name}: ' + package)

    def check_image(self):
        t = self.target
        self.check_no_customer()
        self.check_package_lock()
        cfg = self.config
        need = ['boot/' + cfg['kernelImage'], 'boot/initramfs-linux.img', 'boot/EFI/BOOT/' + cfg['efiFallback'], 'boot/loader/entries/arch.conf',
                'usr/share/omarchy/bin', 'usr/share/sddm/themes/decrypt/theme.conf', 'etc/nologin',
                'usr/local/lib/decrypt-os/app/backend/server.py', 'usr/local/lib/decrypt-os/app/ui/dist/index.html',
                'usr/local/lib/decrypt-os-runtime/openclaw/openclaw.mjs', 'usr/local/lib/decrypt-os-runtime/python/bin/python',
                'etc/systemd/system/multi-user.target.wants/decrypt-firstboot.service',
                'etc/systemd/system/multi-user.target.wants/decrypt-grow-root.service',
                'usr/local/lib/decrypt-os/INSTALLED-FILES.json', 'usr/local/lib/decrypt-os/tools/diagnostics.py',
                'usr/share/licenses/decrypt-os/LICENSE', 'usr/share/licenses/decrypt-os/NOTICES.md',
                'etc/systemd/user/graphical-session.target.wants/decrypt-desktop-layout.timer',
                'etc/systemd/user/timers.target.wants/decrypt-tmp-janitor.timer',
                'etc/skel/.local/state/decrypt-os', 'etc/skel/.config/hypr/autostart.lua']
        missing = [p for p in need if not os.path.lexists(t / p)]  # unit links are absolute
        if missing:
            raise SystemExit('image is missing: ' + ', '.join(missing))
        if (t / 'etc/machine-id').read_text().strip() or list((t / 'etc/ssh').glob('ssh_host_*')):
            raise SystemExit('machine identity was not removed')
        if (t / 'etc/pacman.d/gnupg').exists():
            raise SystemExit('pacman keyring must not ship')
        for secret in ('var/lib/systemd/random-seed', 'var/lib/systemd/credential.secret'):
            if os.path.lexists(t / secret):
                raise SystemExit('machine-specific systemd secret must not ship: ' + secret)
        if (t / 'etc/skel/.config/default').exists():
            raise SystemExit('etc/skel/.config/default would shadow Omarchy defaults')
        wants = t / 'etc/systemd/user/default.target.wants'
        if wants.exists() and any(p.name.startswith('decrypt-') for p in wants.iterdir()):
            raise SystemExit('Decrypt user services must be default-off')
        node = f"/usr/local/lib/decrypt-os-runtime/{cfg['node']}/bin/node"
        self.chroot(['/usr/bin/env', '-i', 'HOME=/tmp', 'OPENCLAW_HOME=/tmp/oc', 'OPENCLAW_STATE_DIR=/tmp/ocs',
                     'OPENCLAW_CONFIG_PATH=/tmp/oc.json', node, '/usr/local/lib/decrypt-os-runtime/openclaw/openclaw.mjs', '--version'])
        self.chroot(['/usr/local/lib/decrypt-os-runtime/python/bin/python', '-I', '-B', '-c',
                     'import cryptography, secretstorage; print("python runtime ok")'])
        for leftover in ('tmp/oc', 'tmp/ocs', 'tmp/oc.json'):
            p = t / leftover
            shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)

    def stage_export(self):
        self.release.mkdir()
        formats = self.config['formats']
        qcow = self.release / (self.image_id + '.qcow2')
        imp = self.release / 'IMPORT.md'
        self.run(['qemu-img', 'convert', '-p', '-f', 'raw', '-O', 'qcow2', '-c', '-o', 'compression_type=zstd',
                  self.raw, qcow], stdout=subprocess.DEVNULL)
        self.run(['qemu-img', 'check', qcow], stdout=subprocess.DEVNULL)
        import_text = (self.src / self.config['importDoc']).read_bytes()
        imp.write_bytes(import_text)
        names = [qcow.name]
        if 'utm.tar' in formats:
            utm = self.release / (self.image_id + '.utm.tar')
            epoch = int(self.args.source_date_epoch)
            log('$ write ' + utm.name)
            write_utm_tar(utm, qcow, self.image_id, import_text, epoch)
            names.append(utm.name)
        if 'vmdk' in formats:
            vmdk = self.release / (self.image_id + '.vmdk')
            self.run(vmdk_convert_argv(self.raw, vmdk), stdout=subprocess.DEVNULL)
            names.append(vmdk.name)
        log('$ sha256sum')
        for line in sha256sums(self.release, names + [imp.name]):
            log(line.strip())
        for path in self.release.iterdir():
            path.chmod(0o644)
        if not self.args.keep_raw:
            self.raw.unlink()

    def build(self):
        stop = STAGES.index(self.args.stop_after)
        try:
            for name in STAGES[:stop + 1]:
                started = time.monotonic()
                log(f'== stage {name}')
                getattr(self, 'stage_' + name)()
                self.timings[name] = round(time.monotonic() - started)
                log(f'== stage {name} done in {self.timings[name]}s')
        finally:
            self.cleanup()
            (self.work / 'TIMINGS.json').write_text(json.dumps(self.timings, indent=2) + '\n')
        log('timings: ' + json.dumps(self.timings))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--source', required=True, help='repo checkout (read-only)')
    p.add_argument('--cache', default='/var/lib/decrypt-builder/cache')
    p.add_argument('--work', required=True, help='new empty work directory')
    p.add_argument('--image-id', required=True)
    p.add_argument('--stop-after', choices=STAGES, default='export')
    p.add_argument('--arch', choices=tuple(TARGETS), default='aarch64', help='image architecture; must match the builder (default aarch64)')
    p.add_argument('--online', action='store_true', help='install packages from the mirrors (aarch64: Arch Linux ARM; '
                   'x86_64: the pinned Arch archive snapshot) and fetch missing npm archives')
    p.add_argument('--keep-raw', action='store_true')
    p.add_argument('--source-date-epoch', default=os.environ.get('SOURCE_DATE_EPOCH', '0'))
    args = p.parse_args()
    if os.geteuid() != 0 or os.uname().machine != args.arch or sys.platform != 'linux':
        raise SystemExit(f'Run as root on an {args.arch} Linux builder.')
    if os.readlink('/proc/self/ns/mnt') == os.readlink('/proc/1/ns/mnt'):
        raise SystemExit('Run inside a private mount namespace: unshare --mount --propagation private ...')
    if not IMAGE_ID.fullmatch(args.image_id):
        raise SystemExit('image id must match ' + IMAGE_ID.pattern)
    os.umask(0o022)
    Path(args.work).mkdir(mode=0o700, parents=True, exist_ok=True)
    Builder(args).build()


if __name__ == '__main__':
    main()
