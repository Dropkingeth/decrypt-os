"""Unit tests for the pure parts of build/guest_build.py (no root, no VM)."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tarfile
from unittest.mock import patch
import urllib.error

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('guest_build', HERE / 'guest_build.py')
gb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gb)


def test_pacman_conf_offline_uses_only_local_repo():
    text = gb.pacman_conf('/var/cache/decrypt-repo', online=False)
    assert '[decrypt-offline]' in text and 'Server = file:///var/cache/decrypt-repo' in text
    assert 'http' not in text
    assert 'SigLevel = PackageRequired DatabaseNever' in text


def test_pacman_conf_online_uses_mirrors_and_cache():
    text = gb.pacman_conf('/var/cache/decrypt-repo', online=True)
    assert all(f'[{r}]' in text for r in ('core', 'extra', 'alarm'))
    assert 'CacheDir = /var/cache/decrypt-repo/' in text and 'decrypt-offline' not in text


def test_pacman_conf_online_mirrors_are_https_only():
    text = gb.pacman_conf('/var/cache/decrypt-repo', online=True)
    assert len(gb.ALARM_MIRRORS) >= 2
    for mirror in gb.ALARM_MIRRORS:
        assert mirror.startswith('https://')
        assert text.count(f'Server = {mirror}') == 3  # core, extra, alarm
    assert 'http://' not in text


def test_mirrorlist_is_https_only():
    text = gb.mirrorlist()
    assert 'http://' not in text
    for mirror in gb.ALARM_MIRRORS:
        assert f'Server = {mirror}' in text


def test_diff_package_lock():
    lock = [{'name': 'acl', 'version': '2.4.0-1'}, {'name': 'bash', 'version': '5.3-1'}]
    unlocked, mismatched = gb.diff_package_lock(lock, {'acl': '2.4.0-1', 'bash': '5.2-1', 'curl': '8.0-1'})
    assert unlocked == ['curl']
    assert mismatched == ['bash']
    assert gb.diff_package_lock(lock, {'acl': '2.4.0-1'}) == ([], [])


def test_arch_package_lock_is_committed_and_well_formed():
    lock = json.loads((HERE / 'arch-package-lock.json').read_text())
    assert lock['schema'] == 'decrypt.arch-package-lock.v1'
    names = [row['name'] for row in lock['packages']]
    assert len(names) == len(set(names)) and len(names) > 500
    for row in lock['packages']:
        assert gb.re.fullmatch('[0-9a-f]{64}', row['sha256'])
        assert row['bytes'] > 0 and row['version'] and row['filename'].startswith(row['name'] + '-')


def test_fstab_and_boot_entry():
    tab = gb.fstab('1111-root', 'ABCD-EF01')
    assert tab.splitlines() == ['UUID=1111-root\t/\tbtrfs\trw,relatime\t0 0',
                                'UUID=ABCD-EF01\t/boot\tvfat\trw,relatime,fmask=0077,dmask=0077\t0 2']
    assert 'mkswapfile' in gb.SWAP_UNIT and 'swap.target' not in gb.SWAP_UNIT  # no tmp.mount ordering cycle
    # Root grows into a bigger VM disk: only the linux-generic ROOT partition matches, then btrfs online.
    assert gb.REPART_ROOT == '[Partition]\nType=linux-generic\n'
    assert 'systemd-repart --dry-run=no --definitions=' + gb.REPART_DIR in gb.GROW_ROOT_UNIT
    assert 'btrfs filesystem resize max /' in gb.GROW_ROOT_UNIT
    assert 'type=L, name="ROOT"' in (gb.Path(gb.__file__).read_text())  # ROOT stays the last, linux-generic one
    assert 'root=UUID=1111-root' in gb.boot_entry('1111-root')


def test_package_list_ignores_comments(tmp_path):
    p = tmp_path / 'list.txt'
    p.write_text('# header\nfoo\n\nbar # trailing\n# baz\n')
    assert gb.read_package_list(p) == ['foo', 'bar']


def test_repo_package_lists_are_valid():
    repo = HERE.parent
    base = json.loads((repo / 'candidate/profiles/arm64-image-factory.json').read_text())['basePackages']
    desktop = gb.read_package_list(repo / 'candidate/base/desktop-packages.txt')
    assert 'linux-aarch64' in base and 'hyprland' in desktop
    assert all(gb.re.fullmatch(r'[a-z0-9@._+-]+', name) for name in base + desktop)


def test_utm_tar_layout(tmp_path):
    qcow = tmp_path / 'disk.qcow2'
    qcow.write_bytes(b'QFI\xfb' + b'\0' * 1000)
    out = tmp_path / 'x.utm.tar'
    gb.write_utm_tar(out, qcow, 'decrypt-os-test-arm64', b'# import\n', 1700000000)
    with tarfile.open(out) as archive:
        members = {m.name: m for m in archive.getmembers()}
        assert sorted(members) == ['Decrypt-OS.utm/Data/disk.qcow2', 'Decrypt-OS.utm/IMPORT.md', 'Decrypt-OS.utm/config.plist']
        assert all(m.uid == 0 and m.mtime == 1700000000 and m.mode == 0o644 for m in members.values())
        config = plistlib.loads(archive.extractfile('Decrypt-OS.utm/config.plist').read())
        assert archive.extractfile('Decrypt-OS.utm/Data/disk.qcow2').read() == qcow.read_bytes()
    assert config['System']['Architecture'] == 'aarch64' and config['QEMU']['UEFIBoot'] is True
    assert config['Drive'][0]['ImageName'] == 'disk.qcow2'
    # Deterministic: same inputs give the same bytes.
    again = tmp_path / 'y.utm.tar'
    gb.write_utm_tar(again, qcow, 'decrypt-os-test-arm64', b'# import\n', 1700000000)
    assert out.read_bytes() == again.read_bytes()


def test_sha256sums(tmp_path):
    (tmp_path / 'b').write_bytes(b'bee')
    (tmp_path / 'a').write_bytes(b'ay')
    gb.sha256sums(tmp_path, ['b', 'a'])
    assert (tmp_path / 'SHA256SUMS').read_text() == (
        f"{hashlib.sha256(b'ay').hexdigest()}  a\n{hashlib.sha256(b'bee').hexdigest()}  b\n")


def test_refuse_redirect_handler_raises_on_any_redirect():
    handler = gb.RefuseRedirect()
    try:
        handler.redirect_request(None, None, 302, 'Found', {}, 'https://evil.example/x.tgz')
        assert False, 'expected SystemExit'
    except SystemExit as exc:
        assert 'evil.example' in str(exc)


def test_fetch_npm_archive_refuses_non_sha512_integrity(tmp_path):
    import base64
    weak = 'sha256-' + base64.b64encode(hashlib.sha256(b'x').digest()).decode()
    try:
        gb.fetch_npm_archive('https://registry.npmjs.org/x/-/x-1.0.0.tgz', weak, tmp_path)
        assert False, 'expected SystemExit'
    except SystemExit as exc:
        assert 'sha512' in str(exc)


def test_fetch_npm_archive_refuses_a_redirect():
    import base64
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(b'x').digest()).decode()
    with patch.object(gb.urllib.request, 'build_opener') as build_opener:
        opener = build_opener.return_value
        opener.open.side_effect = urllib.error.HTTPError('u', 302, 'Found', {}, None)
        try:
            gb.fetch_npm_archive('https://registry.npmjs.org/x/-/x-1.0.0.tgz', integrity, '/tmp')
            assert False, 'expected an exception'
        except urllib.error.HTTPError:
            pass
    handlers = build_opener.call_args.args
    assert any(isinstance(h, gb.RefuseRedirect) for h in handlers)
    proxy_handlers = [h for h in handlers if isinstance(h, gb.urllib.request.ProxyHandler)]
    assert proxy_handlers and proxy_handlers[0].proxies == {}


def test_missing_npm_archives(tmp_path):
    url = 'https://registry.npmjs.org/x/-/x-1.0.0.tgz'
    lock = {'packages': {'': {}, 'node_modules/x': {'resolved': url, 'integrity': 'sha512-AA=='},
                         'node_modules/y': {'resolved': url.replace('x', 'y'), 'integrity': 'sha512-BB=='}}}
    (tmp_path / gb.npm_archive_name(url)).write_bytes(b'x')
    assert gb.missing_npm_archives(lock, tmp_path) == [(url.replace('x', 'y'), 'sha512-BB==')]


def test_image_id_pattern():
    assert gb.IMAGE_ID.fullmatch('decrypt-os-beta4-arm64')
    assert not gb.IMAGE_ID.fullmatch('Decrypt OS')


def test_inputs_pin_omarchy():
    pins = json.loads((HERE / 'inputs.json').read_text())['omarchy']
    assert len(pins['commit']) == 40 and len(pins['archiveSha256']) == 64


def test_inputs_pin_alarm():
    pins = json.loads((HERE / 'inputs.json').read_text())['alarm']
    assert gb.re.fullmatch('[0-9a-f]{64}', pins['rootfsSha256'])
    assert gb.re.fullmatch('[0-9a-f]{64}', pins['signatureSha256'])
    assert gb.re.fullmatch('[0-9a-f]{64}', pins['keyringSha256'])
    assert gb.re.fullmatch('[A-F0-9]{40}', pins['signingKeyFingerprint'])


def test_build_sh_refuses_unsigned_export_without_flag():
    # Exits before touching git/VM/network, so this is safe to actually run.
    result = subprocess.run(['bash', str(HERE / 'build.sh'), '--stop-after', 'export'],
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert '--unsigned-dev' in result.stderr


def test_build_sh_unsigned_dev_guard_runs_before_any_vm_work():
    text = (HERE / 'build.sh').read_text()
    guard = text.index('refusing to export an unsigned release')
    condition = text.index('$stop_after == export && -z $signing_key && $unsigned_dev == 0')
    vm_start = text.index('limactl start')
    assert condition < guard < vm_start


def test_build_sh_runs_privacy_check_before_export():
    text = (HERE / 'build.sh').read_text()
    gate = text.index('tools/privacy_check.py')
    # "before export": before the release files are copied out of the VM
    # and before MANIFEST.json is built/signed, not merely present somewhere.
    export_copy = text.index("for f in $(guest \"ls '$work/release'\"")
    manifest_build = text.index('release-manifest.v1')
    assert gate < export_copy < manifest_build


def test_utm_config_has_every_key_utm_requires():
    # UTM 4.x decodes these keys as required; a missing one makes UTM silently skip the VM.
    import plistlib
    cfg = plistlib.loads(gb.utm_config('decrypt-os-test-arm64'))
    assert {'Backend', 'ConfigurationVersion', 'Information', 'System', 'QEMU', 'Input', 'Sharing',
            'Display', 'Drive', 'Network', 'Serial', 'Sound'} <= cfg.keys()
    assert {'Name', 'UUID', 'Icon', 'IconCustom'} <= cfg['Information'].keys()
    assert {'Architecture', 'Target', 'CPU', 'CPUFlagsAdd', 'CPUFlagsRemove', 'CPUCount',
            'ForceMulticore', 'MemorySize', 'JITCacheSize'} <= cfg['System'].keys()
    assert {'DebugLog', 'UEFIBoot', 'RNGDevice', 'BalloonDevice', 'TPMDevice', 'Hypervisor',
            'RTCLocalTime', 'PS2Controller', 'AdditionalArguments'} <= cfg['QEMU'].keys()
    assert {'UsbBusSupport', 'UsbSharing', 'MaximumUsbShare'} <= cfg['Input'].keys()
    assert {'DirectoryShareMode', 'DirectoryShareReadOnly', 'ClipboardSharing'} <= cfg['Sharing'].keys()
    assert {'Hardware', 'DynamicResolution', 'NativeResolution', 'UpscalingFilter',
            'DownscalingFilter'} <= cfg['Display'][0].keys()
    assert {'ImageName', 'ImageType', 'Interface', 'Identifier'} <= cfg['Drive'][0].keys()
    assert {'Mode', 'Hardware', 'MacAddress', 'IsolateFromHost', 'PortForward'} <= cfg['Network'][0].keys()


def test_build_sh_records_source_tree_not_private_commit():
    # The commit id belongs to the private dev history; the tree id is the same
    # in the squashed public repo. Archiving the tree also drops git's pax
    # "comment=<commit id>" header.
    text = (HERE / 'build.sh').read_text()
    assert "'sourceTree': tree" in text and 'sourceCommit' not in text
    assert 'tree=$(git -C "$repo" rev-parse "$commit^{tree}")' in text
    assert 'archive --format=tar --mtime="@$epoch" "$tree"' in text


def test_build_sh_builder_state_follows_vm_name():
    text = (HERE / 'build.sh').read_text()
    lines = [ln for ln in text.splitlines() if ln.startswith(('vm=', 'state=', 'cache='))]
    assert len(lines) == 3 and 'other-builder' not in text
    script = '\n'.join(lines) + '\necho "$vm $state $cache"'

    def resolve(**env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith('DECRYPT_BUILDER_')}
        return subprocess.run(['bash', '-c', script], env={**clean, **env},
                              capture_output=True, text=True, check=True).stdout.split()

    assert resolve() == ['decrypt-builder', '/var/lib/decrypt-builder', '/var/lib/decrypt-builder/cache']
    # An existing builder with another name keeps its state under that name.
    assert resolve(DECRYPT_BUILDER_VM='other-builder') == ['other-builder', '/var/lib/decrypt-other-builder', '/var/lib/decrypt-other-builder/cache']
    assert resolve(DECRYPT_BUILDER_CACHE='/c')[2] == '/c'


def test_image_ships_license_and_notices(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'LICENSE').write_text('GNU AFFERO GENERAL PUBLIC LICENSE\n')
    (src / 'NOTICES.md').write_text('# Third-party notices\n')
    target = tmp_path / 'target'
    written = gb.stage_license_notices(src, target)
    out = target / 'usr/share/licenses/decrypt-os'
    assert written == [str(out / 'LICENSE'), str(out / 'NOTICES.md')]
    assert (out / 'LICENSE').read_text().startswith('GNU AFFERO')
    assert (out / 'NOTICES.md').read_text().startswith('# Third-party notices')
    assert oct((out / 'LICENSE').stat().st_mode & 0o777) == '0o644'
    for name in gb.LICENSE_FILES:
        assert (HERE.parent / name).read_text().strip(), name


def test_image_ships_diagnostics_tool(tmp_path):
    src = tmp_path / 'src'
    (src / 'candidate/tools').mkdir(parents=True)
    content = b'#!/usr/bin/env python3\n# diagnostics\n'
    (src / 'candidate/tools/diagnostics.py').write_bytes(content)
    target = tmp_path / 'target'
    written = gb.stage_diagnostics_tool(src, target)
    out = target / 'usr/local/lib/decrypt-os/tools/diagnostics.py'
    assert written == str(out)
    assert out.read_bytes() == content
    assert oct(out.stat().st_mode & 0o777) == '0o755'


def test_stage_diagnostics_tool_refuses_empty_source(tmp_path):
    src = tmp_path / 'src'
    (src / 'candidate/tools').mkdir(parents=True)
    (src / 'candidate/tools/diagnostics.py').write_bytes(b'')
    target = tmp_path / 'target'
    try:
        gb.stage_diagnostics_tool(src, target)
        assert False, 'expected SystemExit'
    except SystemExit as exc:
        assert 'diagnostics.py is empty' in str(exc)


def test_real_license_is_agpl_and_checked_in_image():
    assert 'GNU AFFERO GENERAL PUBLIC LICENSE' in (HERE.parent / 'LICENSE').read_text()
    source = (HERE / 'guest_build.py').read_text()
    assert "'usr/share/licenses/decrypt-os/LICENSE'" in source.split('def check_image')[1]
    assert 'stage_license_notices(self.src, self.target)' in source
