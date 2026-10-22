"""x86_64 build target: profiles, pins, bootloader/kernel names, lock arch, export
formats and kit naming. Pure helpers only: no root, no VM, no network."""
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest

repo = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gb = load('guest_build_x86', repo / 'build/guest_build.py')
gpl = load('generate_package_lock_x86', repo / 'build/generate_package_lock.py')
wm = load('write_manifest_x86', repo / 'build/write_manifest.py')
tp = load('target_profile_x86', repo / 'candidate/base/target_profile.py')
boot = load('bootstrap_alarm_root_x86', repo / 'factory/builder/bootstrap_alarm_root.py')
fri = load('fetch_runtime_inputs_x86', repo / 'factory/builder/fetch_runtime_inputs.py')
prov = load('provision_runtime_x86', repo / 'factory/builder/provision_runtime.py')

X86_PROFILES = ('x86-64-image-factory', 'loop-x86-64-dryrun', 'kvm-x86-64-qualification')
ARM_SIBLINGS = dict(zip(X86_PROFILES, ('arm64-image-factory', 'loop-arm64-dryrun', 'lume-arm64-qualification')))


def profile(name):
    return json.loads((repo / f'candidate/profiles/{name}.json').read_text())


# ---- profiles --------------------------------------------------------------

@pytest.mark.parametrize('name', X86_PROFILES)
def test_x86_profiles_validate_and_use_the_standard_arch_kernel(name):
    value = tp.validate(profile(name))
    assert value['id'] == name and value['arch'] == 'x86_64' and value['kernelPackage'] == 'linux'
    base = value['basePackages']
    assert {'linux', 'archlinux-keyring', 'intel-ucode', 'amd-ucode'} <= set(base)
    assert not [p for p in base if 'aarch64' in p or 'archlinuxarm' in p]
    assert 'apple' not in value['hypervisors']


@pytest.mark.parametrize('name', X86_PROFILES)
def test_x86_profiles_differ_from_arm_siblings_only_in_arch_fields(name):
    x86, arm = profile(name), profile(ARM_SIBLINGS[name])
    allowed = {'id', 'arch', 'kernelPackage', 'basePackages', 'hypervisors', 'graphics'}
    assert {k for k in arm if arm[k] != x86[k]} <= allowed
    swap = {'linux-aarch64': 'linux', 'archlinuxarm-keyring': 'archlinux-keyring'}
    assert [p for p in x86['basePackages'] if p not in ('intel-ucode', 'amd-ucode')] == \
        [swap.get(p, p) for p in arm['basePackages']]


def test_profile_validator_rejects_crossed_arch_and_kernel():
    bad = profile('x86-64-image-factory')
    bad['kernelPackage'] = 'linux-aarch64'
    with pytest.raises(tp.ProfileError):
        tp.validate(bad)
    bad = profile('arm64-image-factory')
    bad['arch'] = 'x86_64'
    with pytest.raises(tp.ProfileError):
        tp.validate(bad)


def test_target_table_points_at_existing_x86_files():
    cfg = gb.target_config('x86_64')
    for key in ('profile', 'lock', 'inputs', 'importDoc'):
        assert (repo / cfg[key]).is_file(), cfg[key]
    assert profile(Path(cfg['profile']).stem)['arch'] == 'x86_64'
    with pytest.raises(SystemExit):
        gb.target_config('riscv64')


# ---- pinned Arch Linux base -----------------------------------------------

def test_x86_base_pin_shape_and_committed_release_key():
    pins = boot.load_pins(repo / 'build/inputs-x86_64.json', 'archlinux')
    assert re.fullmatch(r'https://archive\.archlinux\.org/iso/(\d{4}\.\d\d\.\d\d)/archlinux-bootstrap-\1-x86_64\.tar\.zst',
                        pins['source'])
    assert pins['signature'] == pins['source'] + '.sig'
    for key in ('rootfsSha256', 'signatureSha256', 'keyringSha256'):
        assert re.fullmatch('[0-9a-f]{64}', pins[key])
    assert re.fullmatch('[A-F0-9]{40}', pins['signingKeyFingerprint'])
    key = repo / pins['keyring']
    assert hashlib.sha256(key.read_bytes()).hexdigest() == pins['keyringSha256']
    assert re.fullmatch(r'https://archive\.archlinux\.org/repos/\d{4}/\d\d/\d\d', pins['packageSnapshot'])


def test_bootstrap_sections_bind_machine_and_layout():
    assert boot.SECTIONS == {'alarm': ('aarch64', 0), 'archlinux': ('x86_64', 1)}
    assert boot.load_alarm_pins(repo / 'build/inputs.json') == boot.load_pins(repo / 'build/inputs.json', 'alarm')


# ---- pacman / mirrors / bootloader ----------------------------------------

def test_x86_pacman_conf_uses_arch_repos_and_snapshot():
    offline = gb.pacman_conf('/var/cache/decrypt-repo', False, 'x86_64')
    assert 'Architecture = x86_64\n' in offline and '[decrypt-offline]' in offline and 'Server = https' not in offline
    mirrors = gb.build_mirrors('x86_64', repo)
    assert mirrors == ('https://archive.archlinux.org/repos/2026/10/15/$repo/os/$arch',)
    online = gb.pacman_conf('/var/cache/decrypt-repo', True, 'x86_64', mirrors)
    assert re.findall(r'^\[(\w+)\]$', online, re.M) == ['options', 'core', 'extra']
    assert 'archlinuxarm' not in online and 'alarm' not in online
    assert gb.build_mirrors('aarch64', repo) == gb.ALARM_MIRRORS


def test_x86_image_mirrorlist_is_https_arch_only():
    text = gb.mirrorlist('x86_64')
    servers = re.findall(r'^Server = (.+)$', text, re.M)
    assert servers and all(s.startswith('https://') and s.endswith('/$repo/os/$arch') for s in servers)
    assert 'archlinuxarm' not in text


def test_x86_boot_entry_kernel_and_microcode_order():
    entry = gb.boot_entry('ROOT', 'x86_64', ('intel-ucode.img', 'amd-ucode.img'))
    assert entry.splitlines()[:5] == ['title Decrypt OS', 'linux /vmlinuz-linux', 'initrd /intel-ucode.img',
                                      'initrd /amd-ucode.img', 'initrd /initramfs-linux.img']
    assert 'root=UUID=ROOT rw' in entry
    assert 'initrd /intel' not in gb.boot_entry('ROOT', 'x86_64')


def test_portable_hooks_drop_only_autodetect():
    conf = '# HOOKS=(base udev)\nMODULES=()\nHOOKS=(base systemd autodetect microcode modconf kms keyboard block filesystems fsck)\n'
    assert gb.portable_hooks(conf) == 'HOOKS=(base systemd microcode modconf kms keyboard block filesystems fsck)\n'
    for bad in ('MODULES=()\n', 'HOOKS=(base udev block)\n', 'HOOKS=(a autodetect)\nHOOKS=(b autodetect)\n'):
        with pytest.raises(SystemExit):
            gb.portable_hooks(bad)


# ---- Builder stages with privileged calls recorded ------------------------

def builder(tmp_path, arch='x86_64'):
    args = types.SimpleNamespace(source=str(repo), cache=str(tmp_path / 'cache'), work=str(tmp_path / 'work'),
                                 image_id='decrypt-os-beta4-x86_64', stop_after='export', online=False,
                                 keep_raw=True, source_date_epoch='1700000000', arch=arch)
    (tmp_path / 'work').mkdir()
    b = gb.Builder(args)
    b.calls = []

    def run(argv, **kwargs):
        b.calls.append([str(a) for a in argv])
        if argv[0] == 'qemu-img' and argv[1] == 'convert':
            Path(argv[-1]).write_bytes(b'disk-' + str(argv[argv.index('-O') + 1]).encode())
        return subprocess.CompletedProcess(argv, 0, '', '')
    b.run = run
    return b


def test_x86_builder_paths(tmp_path):
    b = builder(tmp_path)
    assert b.repo == tmp_path / 'cache/pkg/x86_64'
    assert b.lock_path == repo / 'build/arch-package-lock-x86_64.json'
    (tmp_path / 'arm').mkdir()
    arm = builder(tmp_path / 'arm', 'aarch64')
    assert arm.repo == tmp_path / 'arm/cache/pkg/aarch64' and arm.lock_path == repo / 'build/arch-package-lock.json'


def test_x86_desktop_stage_installs_bootx64_and_portable_initramfs(tmp_path):
    b = builder(tmp_path)
    t = b.target
    for rel in ('etc/sudoers.d', 'etc/systemd/system', 'usr/lib/systemd/boot/efi', 'boot'):
        (t / rel).mkdir(parents=True)
    (t / 'etc/locale.gen').write_text('#en_US.UTF-8 UTF-8\n')
    (t / 'etc/mkinitcpio.conf').write_text('HOOKS=(base systemd autodetect microcode modconf kms block filesystems fsck)\n')
    (t / 'usr/lib/systemd/boot/efi/systemd-bootx64.efi').write_bytes(b'x64-stub')
    for name in ('vmlinuz-linux', 'initramfs-linux.img', 'amd-ucode.img'):
        (t / 'boot' / name).write_bytes(name.encode())
    b.root_uuid, b.efi_uuid = 'ROOT', 'EFI'
    b.install_omarchy_desktop = lambda: None
    b.stage_desktop()
    assert (t / 'boot/EFI/BOOT/BOOTX64.EFI').read_bytes() == b'x64-stub'
    assert (t / 'boot/EFI/systemd/systemd-bootx64.efi').read_bytes() == b'x64-stub'
    assert not (t / 'boot/EFI/BOOT/BOOTAA64.EFI').exists()
    entry = (t / 'boot/loader/entries/arch.conf').read_text()
    assert 'linux /vmlinuz-linux\ninitrd /amd-ucode.img\ninitrd /initramfs-linux.img\n' in entry  # only present microcode
    assert (t / 'etc/mkinitcpio.conf.d/decrypt-portable.conf').read_text().splitlines()[-1] == \
        'HOOKS=(base systemd microcode modconf kms block filesystems fsck)'
    chroot_cmds = [c[c.index('/mnt/target') + 1:] for c in b.calls if 'arch-chroot' in c]
    assert ['mkinitcpio', '-P'] in chroot_cmds


def test_x86_check_image_requires_x86_boot_files_and_node(tmp_path):
    b = builder(tmp_path)
    b.target.mkdir(parents=True)
    b.check_no_customer = lambda: None
    b.check_package_lock = lambda: None
    with pytest.raises(SystemExit) as error:
        b.check_image()
    message = str(error.value)
    assert 'boot/vmlinuz-linux' in message and 'boot/EFI/BOOT/BOOTX64.EFI' in message
    assert 'boot/Image' not in message and 'BOOTAA64' not in message
    assert gb.TARGETS['x86_64']['node'] == prov.NODE_ARCHIVES['x86_64'][0]


def test_x86_export_is_qcow2_plus_vmdk_with_x86_import_doc(tmp_path):
    b = builder(tmp_path)
    b.raw.write_bytes(b'raw')
    b.stage_export()
    names = sorted(p.name for p in b.release.iterdir())
    assert names == ['IMPORT.md', 'SHA256SUMS', 'decrypt-os-beta4-x86_64.qcow2', 'decrypt-os-beta4-x86_64.vmdk']
    sums = (b.release / 'SHA256SUMS').read_text()
    assert [line.split('  ')[1] for line in sums.splitlines()] == ['IMPORT.md', 'decrypt-os-beta4-x86_64.qcow2',
                                                                    'decrypt-os-beta4-x86_64.vmdk']
    assert (b.release / 'IMPORT.md').read_text().startswith('# Import Decrypt OS (x86_64)')
    assert ['-O', 'vmdk'] == [c for c in b.calls if 'vmdk' in c][0][5:7]


def test_package_lock_check_refuses_placeholder_and_wrong_arch(tmp_path):
    committed = json.loads((repo / 'build/arch-package-lock-x86_64.json').read_text())
    assert committed['arch'] == 'x86_64' and committed['schema'] == 'decrypt.arch-package-lock.v1'
    assert committed['packages'] and all(r['filename'].endswith(('-x86_64.pkg.tar.zst', '-any.pkg.tar.zst'))
                                         for r in committed['packages'])
    placeholder = tmp_path / 'placeholder-lock.json'
    placeholder.write_text(json.dumps({'arch': 'x86_64', 'packages': [], 'repo': 'decrypt-offline',
                                       'schema': 'decrypt.arch-package-lock.v1'}))
    b = builder(tmp_path)
    b.lock_path = placeholder
    with pytest.raises(SystemExit, match='has no packages yet'):
        b.check_package_lock()
    b.lock_path = repo / 'build/arch-package-lock.json'   # the aarch64 lock
    with pytest.raises(SystemExit, match='is for aarch64, not x86_64'):
        b.check_package_lock()


def test_guest_build_refuses_wrong_host_arch(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['guest_build.py', '--arch', 'x86_64', '--source', '/s', '--work', '/w',
                                      '--image-id', 'decrypt-os-x-x86_64'])
    with pytest.raises(SystemExit, match='Run as root on an x86_64 Linux builder.'):
        gb.main()


# ---- package lock generator -----------------------------------------------

def repo_db(path, arch):
    desc = (f'%FILENAME%\nacl-2.4.0-1-{arch}.pkg.tar.zst\n\n%NAME%\nacl\n\n%VERSION%\n2.4.0-1\n\n'
            f'%CSIZE%\n10\n\n%SHA256SUM%\n{"b" * 64}\n\n%ARCH%\n{arch}\n')
    path.mkdir()
    with tarfile.open(path / 'decrypt-offline.db.tar.gz', 'w:gz') as archive:
        info = tarfile.TarInfo('acl-2.4.0-1/desc')
        info.size = len(desc)
        archive.addfile(info, io.BytesIO(desc.encode()))
    return path


def test_lock_generator_writes_x86_lock(tmp_path, monkeypatch):
    pkg = repo_db(tmp_path / 'pkg', 'x86_64')
    out = tmp_path / 'lock.json'
    monkeypatch.setattr(sys, 'argv', ['g', '--arch', 'x86_64', '--repo', str(pkg), '--output', str(out)])
    gpl.main()
    lock = json.loads(out.read_text())
    assert lock['arch'] == 'x86_64' and lock['packages'][0]['filename'] == 'acl-2.4.0-1-x86_64.pkg.tar.zst'


def test_lock_generator_refuses_foreign_arch_packages(tmp_path):
    with pytest.raises(SystemExit, match='built for aarch64, not x86_64'):
        gpl.load_packages(repo_db(tmp_path / 'pkg', 'aarch64'), 'x86_64')
    assert gpl.load_packages(tmp_path / 'pkg')[0]['name'] == 'acl'   # no arch given: unchanged behaviour


# ---- runtime inputs ---------------------------------------------------------

def x86_report(**changes):
    report = {'schema': 'decrypt.runtime-target-python.v1', 'execution': 'native-target-observation',
              'platform': 'linux-x86_64',
              'targetPythonIdentity': {'version': [3, 14, 0], 'cacheTag': 'cpython-314', 'soabi': 'cpython-314-x86_64-linux-gnu'},
              'packagingAvailable': True,
              'supportedTags': ['cp314-cp314-manylinux_2_17_x86_64', 'cp311-abi3-manylinux_2_17_x86_64', 'py3-none-any']}
    report.update(changes)
    return report


def test_x86_target_python_report_accepted_and_cross_arch_refused():
    identity, tags = fri.target_tags(x86_report())
    assert tags[0] == 'cp314-cp314-manylinux_2_17_x86_64'
    for bad in (x86_report(supportedTags=['cp314-cp314-manylinux_2_17_aarch64']),
                x86_report(targetPythonIdentity={'version': [3, 14, 0], 'cacheTag': 'cpython-314',
                                                 'soabi': 'cpython-314-aarch64-linux-gnu'}),
                x86_report(platform='linux-riscv64')):
        with pytest.raises(fri.Refused):
            fri.target_tags(bad)


def test_node_pins_per_arch():
    assert prov.NODE_ARCHIVES['x86_64'] == ('node-v24.19.0-linux-x64',
                                            '14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647')
    assert prov.NODE_ARCHIVES['aarch64'][1] == '01443c1e1a29e531ccad5a46fefa6df490d2189c49f7955904aecdbb0fe86fdc'
    assert set(fri.NODE_URLS) == {name for name, _ in prov.NODE_ARCHIVES.values()}
    assert all(url.endswith('/' + name + '.tar.xz') for name, url in fri.NODE_URLS.items())
    fri.curl_argv('/usr/bin/curl', fri.NODE_URLS['node-v24.19.0-linux-x64'], 10)


def test_x86_wheels_selected_from_pinned_metadata():
    metadata = {name: json.loads((repo / 'factory/builder' / (name + '.json')).read_text()) for name in prov.VERSIONS}
    tags = ['cp314-cp314-manylinux_2_17_x86_64', 'cp311-abi3-manylinux_2_17_x86_64', 'cp311-abi3-manylinux2014_x86_64',
            'py3-none-any']
    picked = {row['filename'] for row in prov.select_wheels(metadata, tags)}
    assert any(n.startswith('cffi-') and 'x86_64' in n for n in picked)
    assert any(n.startswith('cryptography-') and 'x86_64' in n for n in picked)
    assert not [n for n in picked if 'aarch64' in n]


# ---- release manifest and kit naming --------------------------------------

def test_write_manifest_matches_build_sh_for_aarch64_and_sets_x86_arch(tmp_path):
    (tmp_path / 'a.qcow2').write_bytes(b'abc')
    (tmp_path / 'SHA256SUMS').write_text(hashlib.sha256(b'abc').hexdigest() + '  a.qcow2\n')
    script = (repo / 'build/build.sh').read_text()
    heredoc = script.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    subprocess.run([sys.executable, '-c', heredoc, str(tmp_path), 'img', 'v1', 'tree', '7'], check=True)
    from_build_sh = json.loads((tmp_path / 'MANIFEST.json').read_text())
    ours = wm.manifest(str(tmp_path), 'img', 'v1', 'tree', '7', 'aarch64', built_at=from_build_sh['builtAt'])
    assert ours == from_build_sh
    x86 = wm.manifest(str(tmp_path), 'img', 'v1', 'tree', '7', 'x86_64')
    assert x86['arch'] == 'x86_64' and x86['artifacts'] == ours['artifacts']
    with pytest.raises(SystemExit):
        wm.manifest(str(tmp_path), 'img', 'v1', 'tree', '7', 'arm64')


def test_x86_kit_naming_and_unsigned_dev_guard():
    script = (repo / 'build/build-x86_64.sh').read_text()
    assert "printf 'decrypt-os-%s-x86_64'" in script
    assert '--arch x86_64' in script and 'write_manifest.py' in script and 'x86_64' in script.split('write_manifest.py', 1)[1].splitlines()[0]
    assert 'pkeyutl -sign' not in script and 'ssh-keygen -Y sign' not in script   # never signs on the builder
    result = subprocess.run(['bash', str(repo / 'build/build-x86_64.sh')], capture_output=True, text=True)
    assert result.returncode == 2 and '--unsigned-dev' in result.stderr
    image_id = subprocess.run(['bash', '-c', "printf 'decrypt-os-%s-x86_64' 'Beta4-Test' | tr 'A-Z' 'a-z' | tr -c 'a-z0-9._\\n-' '-'"],
                              capture_output=True, text=True).stdout
    assert image_id == 'decrypt-os-beta4-test-x86_64' and gb.IMAGE_ID.fullmatch(image_id)


def test_sign_manifest_refuses_mismatched_sums(tmp_path):
    (tmp_path / 'MANIFEST.json').write_text(json.dumps({'schema': 'decrypt.release-manifest.v1', 'imageId': 'i', 'arch': 'x86_64',
                                                        'artifacts': [{'name': 'a', 'sha256': '0' * 64, 'bytes': 1}]}))
    (tmp_path / 'SHA256SUMS').write_text('1' * 64 + '  a\n')
    key = tmp_path / 'k.pem'
    key.write_text('not a key')
    result = subprocess.run(['bash', str(repo / 'build/sign_manifest.sh'), '--dir', str(tmp_path), '--signing-key', str(key)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / 'MANIFEST.json.sig').exists()
