#!/usr/bin/env python3
"""Guided runtime/app setup for a fresh, already booted ARM64 Omarchy guest.
Default status is read-only. No disk, VM, login, vault or service operations.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import uuid

SCHEMA = 'decrypt.guided-setup.v1'
RUNTIME = Path('/usr/local/lib/decrypt-os-runtime')
APP = Path('/usr/local/lib/decrypt-os')
JOURNALS = Path('/var/lib/decrypt-os-guided-setup')
PRIVATE_HOST_BINDING = Path('/etc/decrypt-host/protected-machines.json')
NODE = 'node-v24.19.0-linux-arm64'
HEX = re.compile(r'[0-9a-f]{64}')
STEPS = ('Check verified inputs and desktop', 'Build runtime', 'Build interface',
         'Review installation plan', 'Install runtime and app', 'Verify installed files')
LIMITS = ['Base-disk and desktop installation are separate reviewed steps.',
          'Native first launch, sign-in, vault, reboot and restore are not verified by this tool.',
          'This is a pre-release ARM64 test candidate, not a complete OS installer.']

class Refused(ValueError):
    pass

def require(test, message):
    if not test:
        raise Refused(message)

def digest(data):
    return hashlib.sha256(data).hexdigest()

def hash_file(path):
    with opened(path) as handle:
        value = hashlib.sha256()
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
        return value.hexdigest()

def real_chain(path):
    path = Path(path).absolute()
    for item in [path, *path.parents]:
        require(not item.is_symlink(), 'Linked paths are not accepted: ' + str(item))
    return path

@contextlib.contextmanager
def opened(path):
    path = real_chain(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                'Expected a regular file with one link: ' + str(path))
        with os.fdopen(fd, 'rb') as handle:
            fd = None
            yield handle
    finally:
        if fd is not None:
            os.close(fd)

def read_file(path, maximum=32 * 1024 * 1024):
    with opened(path) as handle:
        raw = handle.read(maximum + 1)
    require(len(raw) <= maximum, 'File exceeds its permitted size.')
    return raw

def strict_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right

def read_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON key.')
            result[key] = value
        return result
    def invalid_constant(_):
        raise Refused('Nonfinite JSON value.')
    return json.loads(read_file(path), object_pairs_hook=unique_pairs, parse_constant=invalid_constant)

def relative(name):
    require(isinstance(name, str) and name and '\\' not in name and '\x00' not in name,
            'Invalid relative input path.')
    p = PurePosixPath(name)
    require(not p.is_absolute() and all(v not in ('', '.', '..') for v in name.split('/')),
            'Input path escapes its directory.')
    return name

def atomic_json(path, data, mode=0o600):
    path = real_chain(path)
    if path.exists():
        require(stat.S_ISREG(path.lstat().st_mode) and path.stat().st_nlink == 1,
                'Unsafe status destination.')
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), mode)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def module(path, expected):
    require(HEX.fullmatch(expected or ''), 'Missing reviewed helper hash.')
    raw = read_file(path)
    require(digest(raw) == expected, 'Reviewed installer code changed.')
    mod = types.ModuleType('_reviewed_' + expected[:12])
    mod.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), mod.__dict__)
    return mod

def verify_media(media, expected):
    media = real_chain(media)
    raw = read_file(media / 'SHA256SUMS', 1024 * 1024)
    require(HEX.fullmatch(expected or '') and digest(raw) == expected,
            'The input checksum list does not match the separate reviewed receipt.')
    records = {}
    for line in raw.decode('ascii').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        require(match is not None, 'Invalid checksum-list line.')
        name = relative(match[2])
        require(name != 'SHA256SUMS' and name not in records, 'Duplicate or self-referential input.')
        require(hash_file(media / name) == match[1], 'Input checksum mismatch: ' + name)
        records[name] = match[1]
    require(1 <= len(records) <= 2000, 'Invalid input count.')
    observed = set()
    for item in media.rglob('*'):
        require(not item.is_symlink(), 'Linked media entries are not accepted.')
        if not item.is_dir():
            require(item.is_file(), 'Special media entries are not accepted.')
            observed.add(item.relative_to(media).as_posix())
    require(observed == set(records) | {'SHA256SUMS'}, 'The media contains missing or unlisted files.')
    needed = {'app/decrypt-os-source.tar.gz', 'app/SOURCE-MANIFEST.json', 'dependencies/provision_runtime.py'}
    require(needed <= records.keys(), 'The verified media is missing setup components.')
    return records

def copy_inputs(media, attempt, records):
    dependencies = attempt / 'dependencies'
    dependencies.mkdir(mode=0o700)
    for name, expected in records.items():
        if not name.startswith('dependencies/'):
            continue
        target = attempt / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with opened(Path(media) / name) as source, target.open('xb') as dest:
            shutil.copyfileobj(source, dest)
        require(hash_file(target) == expected, 'Input changed while copying: ' + name)
    archive = Path(media) / 'app/decrypt-os-source.tar.gz'
    require(hash_file(archive) == records['app/decrypt-os-source.tar.gz'], 'App archive changed.')
    app = attempt / 'app-source'
    app.mkdir(mode=0o700)
    with tarfile.open(archive, 'r:gz') as handle:
        rows = handle.getmembers()
        require(len(rows) <= 2500 and sum(row.size for row in rows) <= 128 * 1024 * 1024,
                'Source archive exceeds its permitted size.')
        seen = set()
        for row in rows:
            relative(row.name)
            parts = PurePosixPath(row.name).parts
            require(parts[0] == 'decrypt-os-source' and len(parts) > 1,
                    'Unexpected source archive prefix.')
            name = '/'.join(parts[1:])
            require(name not in seen and (row.isfile() or row.isdir()),
                    'Source archive has duplicate, linked or special entries.')
            seen.add(name)
            destination = app / name
            if row.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with handle.extractfile(row) as source, destination.open('xb') as dest:
                    shutil.copyfileobj(source, dest)
                destination.chmod(0o755 if row.mode & 0o111 else 0o644)
    require(hash_file(archive) == records['app/decrypt-os-source.tar.gz'], 'Source changed during extraction.')
    require(hash_file(app / 'SOURCE-MANIFEST.json') == records['app/SOURCE-MANIFEST.json'],
            'Extracted source manifest differs from the reviewed media.')
    manifest = read_json(app / 'SOURCE-MANIFEST.json')
    helper_row = next((r for r in manifest.get('files', []) if r.get('path') == 'installer/clean_base.py'), None)
    require(helper_row is not None, 'Missing application installer record.')
    installer = module(app / 'installer/clean_base.py', helper_row['sha256'])
    installer.verify_bundle(app)
    return dependencies, app, installer, helper_row['sha256']

def machine_hash():
    raw = read_file('/etc/machine-id', 128).decode().strip()
    require(re.fullmatch('[0-9a-f]{32}', raw), 'A valid local machine identity is required.')
    return digest(raw.encode())

def account_info(user=None):
    account = pwd.getpwuid(os.getuid()) if user is None else pwd.getpwnam(user)
    require(account.pw_uid >= 1000, 'Use the fresh guest desktop account, not root.')
    home = real_chain(account.pw_dir)
    info = home.stat()
    require(info.st_uid == account.pw_uid and not info.st_mode & 0o022, 'Unsafe desktop home.')
    return {'user': account.pw_name, 'uid': account.pw_uid, 'gid': account.pw_gid, 'home': str(home)}

def clean_env(home, node=None):
    path = (str(node / 'bin') + ':') if node else ''
    return {'PATH': path + '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}

def progress(number, text=None):
    print(f'[{number}/6] {text or STEPS[number - 1]}', flush=True)

def logged(command, cwd, env, logfile):
    # Builds have no password prompts. Sudo is invoked separately with inherited terminal I/O.
    with Path(logfile).open('xb') as handle:
        child = subprocess.Popen(command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            code = child.wait()
        except BaseException:
            import signal
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise
    require(code == 0, 'Step failed. See ' + str(logfile) + '. Previous files were kept; a retry uses a new build directory.')

def current_state(work, lock_held=False):
    file = Path(work) / 'STATE.json'
    if not file.exists():
        return {'schema': SCHEMA, 'status': 'not_started', 'limitations': LIMITS}
    value = read_json(file)
    require(value.get('schema') == SCHEMA, 'Unknown saved setup state.')
    if value.get('status') in ('preparing', 'installing'):
        active = False
        lock = Path(work) / '.setup.lock'
        if not lock_held and lock.exists():
            fd = os.open(real_chain(lock), os.O_RDONLY | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    active = True
            finally:
                os.close(fd)
        # Status never rewrites a stale operation as successful. Lock metadata is not changed.
        value = {**value, 'status': value['status'] if active else 'interrupted',
                 'lastRecordedStatus': value['status'], 'operationActive': active}
    return value

@contextlib.contextmanager
def work_lock(work):
    work = real_chain(work)
    require(work.is_dir() and work.stat().st_uid == os.getuid() and not work.stat().st_mode & 0o077,
            'The working directory must be private and owned by the desktop account (mode 0700).')
    fd = os.open(work / '.setup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        require(os.fstat(fd).st_nlink == 1, 'Unsafe setup lock.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('Another setup operation is using this working directory.')
        yield
    finally:
        os.close(fd)

def state_write(work, **fields):
    atomic_json(Path(work) / 'STATE.json', {'schema': SCHEMA, 'updatedUtc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **fields})

def protected_machine_hashes(path=PRIVATE_HOST_BINDING):
    path = Path(path)
    for parent in path.parents:
        verify_owned(parent, directory=True)
    verify_owned(path)
    value = read_json(path)
    require(type(value) is dict and set(value) == {'schema', 'protectedMachineSha256'}
            and value['schema'] == 'decrypt.protected-hosts.v1', 'Private protected-host configuration is required.')
    hashes = value['protectedMachineSha256']
    require(type(hashes) is list and hashes and all(type(h) is str and HEX.fullmatch(h) for h in hashes)
            and len(set(hashes)) == len(hashes), 'Explicit protected-machine hashes are required.')
    return tuple(hashes)


def prepare(media, expected_manifest, work, user=None):
    require(os.geteuid() != 0, 'Build as the fresh guest account, never as root.')
    require(platform.system() == 'Linux' and platform.machine() == 'aarch64',
            'Setup builds only on a fresh Linux ARM64 desktop guest. This host is read-only.')
    account = account_info(user)
    require(account['uid'] == os.getuid(), 'Build only for the current desktop account.')
    identity = machine_hash()
    require(identity not in protected_machine_hashes(), 'The selected private protected host is refused.')
    work = real_chain(work)
    if not work.exists():
        require(work.parent.is_dir(), 'Create the working directory parent first.')
        work.mkdir(mode=0o700)
    with work_lock(work):
        old = current_state(work, lock_held=True)
        require(old['status'] not in ('installing', 'installation_unverified', 'installed') and
                old.get('lastRecordedStatus') != 'installing',
                'An installation was attempted. Run verify; do not rebuild or replay privileged steps.')
        progress(1)
        records = verify_media(media, expected_manifest)
        require(records.get('guided/guided_setup.py') == hash_file(Path(__file__)),
                'This setup program is not the version bound to the reviewed media.')
        attempt = work / ('attempt-' + uuid.uuid4().hex)
        attempt.mkdir(mode=0o700)
        state_write(work, status='preparing', attempt=str(attempt), machine=identity)
        try:
            dependencies, app, installer, app_helper_hash = copy_inputs(media, attempt, records)
            runtime = module(dependencies / 'provision_runtime.py', records['dependencies/provision_runtime.py'])
            runtime.host_guard(identity, building=True)
            host = installer.inspect_host(account['user'])
            require(host['eligibleForInstall'], 'Desktop prerequisites failed: ' + ' '.join(host['failures']))
            # Installed dependencies are intentionally absent here. Full preflight follows runtime apply.
            inputs = runtime.verify_inputs(dependencies)
            progress(2)
            build_dir = attempt / 'runtime-build'
            logged(['/usr/bin/python3', '-I', '-B', str(dependencies / 'provision_runtime.py'), 'build',
                    '--inputs', str(dependencies), '--output', str(build_dir), '--inputs-sha256', inputs['sha256']],
                   attempt, clean_env(account['home']), attempt / 'runtime-build.log')
            build_hash = hash_file(build_dir / 'DEPENDENCY-BUILD-RECEIPT.json')
            receipt = runtime.verify_build(build_dir, build_hash)
            require(receipt['inputsSha256'] == inputs['sha256'], 'Runtime build belongs to different inputs.')
            progress(3)
            node = build_dir / 'runtime' / NODE
            build_home = attempt / 'build-home'
            build_home.mkdir(mode=0o700)
            configs = [attempt / 'npm-user-empty.conf', attempt / 'npm-global-empty.conf']
            for config in configs:
                config.touch(mode=0o600, exist_ok=False)
            env = clean_env(build_home, node)
            npm = [str(node / 'bin/npm'), '--userconfig=' + str(configs[0]), '--globalconfig=' + str(configs[1])]
            logged(npm + ['ci', '--ignore-scripts', '--no-audit', '--no-fund'], app / 'app/ui', env, attempt / 'interface-dependencies.log')
            logged(npm + ['run', 'build'], app / 'app/ui', env, attempt / 'interface-build.log')
            installer.verify_bundle(app)
            progress(4)
            render = attempt / 'review'
            installer.render(app, render, account['user'])
            plan = {'schema': SCHEMA, 'attempt': str(attempt), 'machine': identity, 'account': account,
                    'manifestSha256': expected_manifest, 'inputRecords': records,
                    'runtimeHelperSha256': records['dependencies/provision_runtime.py'],
                    'appHelperSha256': app_helper_hash,
                    'noticeHelperSha256': hash_file(app / 'installer/install_notice.py'), 'inputsSha256': inputs['sha256'],
                    'runtimeReceiptSha256': build_hash,
                    'sourceManifestSha256': hash_file(app / 'SOURCE-MANIFEST.json'),
                    'recipeSha256': hash_file(render / 'RECIPE-RECEIPT.json'),
                    'guidedSha256': hash_file(Path(__file__)), 'limitations': LIMITS}
            atomic_json(attempt / 'PLAN.json', plan)
            plan_hash = hash_file(attempt / 'PLAN.json')
            state_write(work, status='ready_for_review', attempt=str(attempt), machine=identity, planSha256=plan_hash)
            print('Build complete. Review PLAN.json and review/rootfs before installing.')
            print('Plan SHA-256: ' + plan_hash)
            print('One local sudo request installs the runtime and app. Services stay stopped.')
            return plan
        except BaseException as exc:
            state_write(work, status='build_interrupted' if isinstance(exc, KeyboardInterrupt) else 'build_failed',
                        attempt=str(attempt), machine=identity,
                        message='Build did not complete. Existing attempt retained; prepare starts a new directory.')
            raise

def load_plan(path, expected):
    path = real_chain(path)
    raw = read_file(path)
    require(HEX.fullmatch(expected or '') and digest(raw) == expected, 'Reviewed plan hash does not match.')
    plan = json.loads(raw)
    require(plan.get('schema') == SCHEMA and Path(plan['attempt']) == path.parent,
            'Plan belongs to another attempt or unsupported setup.')
    require(machine_hash() == plan['machine'], 'Saved plan belongs to a different machine.')
    account = account_info(plan['account']['user'])
    require(account == plan['account'], 'Desktop account identity changed.')
    require(hash_file(Path(__file__)) == plan['guidedSha256'], 'Guided setup changed after review.')
    require(HEX.fullmatch(plan.get('manifestSha256', '')), 'Missing input manifest binding.')
    # The exact input records are part of the externally approved plan hash.
    attempt = path.parent
    for name, expected_hash in plan['inputRecords'].items():
        relative(name)
        require(HEX.fullmatch(expected_hash), 'Invalid input hash.')
        if name.startswith('dependencies/'):
            require(hash_file(attempt / name) == expected_hash, 'Copied input changed: ' + name)
    runtime = module(attempt / 'dependencies/provision_runtime.py', plan['runtimeHelperSha256'])
    installer = module(attempt / 'app-source/installer/clean_base.py', plan['appHelperSha256'])
    inputs = runtime.verify_inputs(attempt / 'dependencies')
    require(inputs['sha256'] == plan['inputsSha256'], 'Runtime input set changed.')
    receipt = runtime.verify_build(attempt / 'runtime-build', plan['runtimeReceiptSha256'])
    require(receipt['inputsSha256'] == inputs['sha256'], 'Runtime build came from different inputs.')
    _, manifest = installer.verify_bundle(attempt / 'app-source')
    require(manifest == plan['sourceManifestSha256'] == plan['inputRecords']['app/SOURCE-MANIFEST.json'],
            'Application source changed after review.')
    require(hash_file(attempt / 'review/RECIPE-RECEIPT.json') == plan['recipeSha256'], 'Rendered recipe changed.')
    # No new render directory in a read-only verification. Source/UI digest comparison binds all generated files.
    recipe = read_json(attempt / 'review/RECIPE-RECEIPT.json')
    require(recipe['sourceManifestSha256'] == manifest and recipe['desktopUser'] == account['user'],
            'Recipe source/account binding differs.')
    built_ui = installer.ui_files(attempt / 'app-source')
    ui_prefix = 'usr/local/lib/decrypt-os/app/ui/dist/'
    reviewed_ui = {row['path'][len(ui_prefix):]: row for row in recipe['files'] if row['path'].startswith(ui_prefix)}
    require(set(built_ui) == set(reviewed_ui), 'Built interface files differ from the reviewed recipe.')
    for name, data in built_ui.items():
        require(digest(data) == reviewed_ui[name]['sha256'] and len(data) == reviewed_ui[name]['bytes'],
                'Built interface changed after review.')
    for row in recipe['files']:
        file = attempt / 'review/rootfs' / relative(row['path'])
        require(hash_file(file) == row['sha256'] and file.stat().st_size == row['bytes'], 'Rendered file changed.')
    return plan, runtime, installer, receipt, recipe

def verify_owned(path, mode=None, uid=0, directory=False):
    path = real_chain(path)
    info = path.lstat()
    require(info.st_uid == uid and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
            'Installed file type or owner differs: ' + str(path))
    require(not info.st_mode & 0o022, 'Installed system path is writable by others: ' + str(path))
    if mode is not None:
        require(stat.S_IMODE(info.st_mode) == mode, 'Installed file mode differs: ' + str(path))

def verify_runtime(runtime, receipt, root=RUNTIME):
    runtime.require_python_identity(receipt)
    verify_owned(root, 0o755, directory=True)
    require(runtime.inventory(root) == receipt['files'], 'Installed runtime inventory does not match the reviewed build.')
    runtime.verify_runtime_directory_modes(root, receipt['files'])
    for row in receipt['files']:
        path = Path(root) / relative(row['path'])
        if row['type'] == 'symlink':
            info = path.lstat()
            require(info.st_uid == 0 and path.is_symlink() and os.readlink(path) == row['target'], 'Installed runtime link differs.')
        else:
            verify_owned(path, int(row['mode'], 8), directory=row['type'] == 'directory')
    return {'runtimeFilesVerified': len(receipt['files']), 'runtimeModesVerified': True}

def verify_application(plan, recipe, root=Path('/'), acknowledgement=None):
    root = Path(root)
    for row in recipe['files']:
        path = root / relative(row['path'])
        verify_owned(path, int(row['mode'], 8))
        require(path.stat().st_size == row['bytes'] and hash_file(path) == row['sha256'], 'Installed app file differs: ' + row['path'])
        if row['path'].startswith('etc/sudoers.d/'):
            logical = read_file(path, 65536).decode().replace('\\\r\n', '').replace('\\\n', '')
            active = '\n'.join(line for line in logical.splitlines() if not line.lstrip().startswith('#'))
            require(not re.search(r'NOPASSWD\s*:|!\s*authenticate', active, re.I), 'Passwordless customer privilege is not accepted.')
    marker = root / 'etc/decrypt-os/customer-test.json'
    installed = root / 'usr/local/lib/decrypt-os/INSTALL-RECEIPT.json'
    verify_owned(marker, 0o644)
    verify_owned(installed, 0o644)
    enrollment = {'schema': 'decrypt.customer-test.v1', 'role': 'clean-customer-test',
                  'machineSha256': plan['machine'], 'desktopUser': plan['account']['user'],
                  'desktopUid': plan['account']['uid'], 'sourceManifestSha256': plan['sourceManifestSha256'], 'cleanVmVerified': False}
    require(strict_equal(read_json(marker), enrollment), 'Installed machine enrollment differs.')
    require(acknowledgement is not None, 'Prepared and root acknowledgements must be verified first.')
    require(strict_equal(read_json(installed), {**recipe, 'installed': True, 'stageOnly': False,
                                   'targetMachineSha256': plan['machine'], 'servicesStarted': False,
                                   'acknowledgement': acknowledgement}),
            'Installed app receipt differs from the complete reviewed recipe.')
    state = root / Path(plan['account']['home']).relative_to('/') / '.local/state/decrypt-os'
    verify_owned(state, 0o700, uid=plan['account']['uid'], directory=True)
    require(not any(state.iterdir()), 'Customer state is no longer empty; installation-only verification cannot establish a clean baseline.')
    return {'appFilesVerified': len(recipe['files']), 'emptyCustomerState': True, 'servicesStartedBySetup': False}

def installed_verification(plan, runtime, _installer, receipt, recipe, plan_sha256):
    acknowledgement = verify_root_acknowledgement(plan, plan_sha256)
    return {**verify_runtime(runtime, receipt), **verify_application(plan, recipe, acknowledgement=acknowledgement),
            'installedFilesVerified': True, 'nativeFirstLaunchVerified': False,
            'onboardingVerified': False, 'rebootVerified': False, 'restoreVerified': False}

def journal_directory(plan_hash):
    require(HEX.fullmatch(plan_hash), 'Invalid journal identity.')
    for p in (JOURNALS.parent, *JOURNALS.parent.parents):
        verify_owned(p, directory=True)
    if not JOURNALS.exists():
        JOURNALS.mkdir(mode=0o700)
        JOURNALS.chmod(0o755)
    verify_owned(JOURNALS, 0o755, directory=True)
    destination = JOURNALS / plan_hash
    require(not destination.exists() and not destination.is_symlink(),
            'This installation was already attempted. Run verify; an interrupted privileged install is never replayed.')
    destination.mkdir(mode=0o700)
    destination.chmod(0o755)
    verify_owned(destination, 0o755, directory=True)
    return destination

def notice_module(plan):
    return module(Path(plan['attempt']) / 'app-source/installer/install_notice.py', plan['noticeHelperSha256'])


def acknowledge_install(plan):
    notice = notice_module(plan)
    return notice.acknowledge(Path(plan['attempt']) / 'ACKNOWLEDGEMENT.json', 'runtime-setup',
                              notice.runtime_binding(plan['machine'], plan['recipeSha256']), plan['account']['user'])


def verify_acknowledgement(plan):
    notice = notice_module(plan)
    return notice.verify(Path(plan['attempt']) / 'ACKNOWLEDGEMENT.json', 'runtime-setup',
                         notice.runtime_binding(plan['machine'], plan['recipeSha256']), plan['account']['user'],
                         {plan['account']['uid'], 0})


def verify_root_acknowledgement(plan, plan_sha256):
    """Bind all three receipts to the independently prepared notice and recipe."""
    attempt = Path(plan['attempt'])
    plan_path = attempt / 'PLAN.json'
    require(HEX.fullmatch(plan_sha256 or '') and hash_file(plan_path) == plan_sha256
            and strict_equal(read_json(plan_path), plan), 'Prepared plan changed during verification.')
    journal = JOURNALS / plan_sha256
    for directory in (JOURNALS, journal):
        verify_owned(directory, 0o755, directory=True)
    path = journal / 'ACKNOWLEDGEMENT.json'
    verify_owned(path, 0o644)
    prepared = verify_acknowledgement(plan)
    recorded = read_json(path)
    require(strict_equal(recorded, prepared), 'Prepared and root acknowledgements differ.')
    notice = notice_module(plan)
    notice.validate(recorded, 'runtime-setup', notice.runtime_binding(plan['machine'], plan['recipeSha256']),
                    plan['account']['user'])
    return recorded


def root_apply(path, expected):
    require(os.geteuid() == 0, 'Installation must run through the native sudo prompt.')
    plan, runtime, installer, receipt, recipe = load_plan(path, expected)
    require(os.environ.get('SUDO_UID') == str(plan['account']['uid']), 'Use sudo from the reviewed desktop account.')
    acknowledgement = verify_acknowledgement(plan)
    runtime.host_guard(plan['machine'])
    host = installer.inspect_host(plan['account']['user'])
    require(host['eligibleForInstall'], 'Target prerequisites changed: ' + ' '.join(host['failures']))
    # Re-render under root only after validating source. This performs no package builds or app execution.
    with tempfile.TemporaryDirectory(prefix='decrypt-guided-review-', dir='/var/tmp') as scratch:
        rendered = Path(scratch) / 'recipe'
        installer.render(Path(plan['attempt']) / 'app-source', rendered, plan['account']['user'])
        installer.verify_reviewed_recipe(rendered, plan['recipeSha256'])
    journal = journal_directory(expected)
    atomic_json(journal / 'ACKNOWLEDGEMENT.json', acknowledgement, 0o644)
    phase = {'schema': SCHEMA, 'planSha256': expected, 'machine': plan['machine'], 'phase': 'runtime_apply_started'}
    atomic_json(journal / 'RESULT.json', phase, 0o644)
    try:
        progress(5)
        result = runtime.apply(Path(plan['attempt']) / 'runtime-build', plan['runtimeReceiptSha256'], plan['machine'])
        phase.update(runtimeApply=result, phase='runtime_installed')
        atomic_json(journal / 'RESULT.json', phase, 0o644)
        phase['runtimeVerification'] = verify_runtime(runtime, receipt)
        # This is the supported full preflight sequence: dependencies exist only now.
        host = installer.inspect_host(plan['account']['user'])
        dependencies = installer.dependency_status() if host['eligibleForInstall'] else {'ready': False}
        require(host['eligibleForInstall'] and dependencies['ready'], 'Installed runtime or application preflight failed. Preserve this partial installation for recovery review.')
        phase['phase'] = 'app_apply_started'
        atomic_json(journal / 'RESULT.json', phase, 0o644)
        phase['appApply'] = installer.apply(Path(plan['attempt']) / 'app-source', plan['account']['user'],
                                          plan['sourceManifestSha256'], plan['machine'], plan['recipeSha256'], journal / 'ACKNOWLEDGEMENT.json')
        progress(6)
        phase['verification'] = installed_verification(plan, runtime, installer, receipt, recipe, expected)
        phase['phase'] = 'installed_files_verified'
        atomic_json(journal / 'RESULT.json', phase, 0o644)
        print('Runtime and app files verified. Services are still stopped. Native first launch is the next check.')
        return phase
    except BaseException:
        phase.update(phase='recovery_required', message='Privileged installation did not finish verification. Do not replay it; preserve files and run verify.')
        atomic_json(journal / 'RESULT.json', phase, 0o644)
        raise

# Isolated Python verifies the exact guided script bytes before executing it as root.
# The approved hash is supplied directly in the sudo argv, not read from mutable state.
BOOTSTRAP = "import os,sys,hashlib; p,h=sys.argv[1:3]; f=os.open(p,os.O_RDONLY|os.O_NOFOLLOW); b=os.fdopen(f,'rb').read(); assert hashlib.sha256(b).hexdigest()==h,'Guided setup changed'; sys.argv=[p]+sys.argv[3:]; exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"

def sudo_command(plan_path, expected, guided_hash, action='root-apply'):
    return ['/usr/bin/sudo', '--', '/usr/bin/python3', '-I', '-B', '-c', BOOTSTRAP,
            str(Path(__file__).absolute()), guided_hash, action, '--plan', str(plan_path), '--plan-sha256', expected]

def install(work, expected):
    require(os.geteuid() != 0, 'Start setup as the desktop account; sudo prompts locally when needed.')
    work = real_chain(work)
    with work_lock(work):
        state = current_state(work, lock_held=True)
        require(state['status'] == 'ready_for_review', 'No reviewed, unattempted installation is ready. Run status or verify.')
        require(state['planSha256'] == expected, 'Select the plan hash displayed by prepare.')
        plan_path = Path(state['attempt']) / 'PLAN.json'
        plan, runtime, installer, _, _ = load_plan(plan_path, expected)
        require(plan['account']['uid'] == os.getuid(), 'Plan belongs to another desktop account.')
        runtime.host_guard(plan['machine'])
        host = installer.inspect_host(plan['account']['user'])
        require(host['eligibleForInstall'], 'Desktop preflight no longer passes.')
        require(sys.stdin.isatty(), 'Install needs a local terminal for the masked sudo password prompt.')
        if acknowledge_install(plan) is None:
            print('Cancelled. Nothing was installed. The reviewed build is retained.')
            return {'cancelled': True, 'noticeAccepted': False}
        print('Install the reviewed runtime and Decrypt app. Services stay stopped; no accounts, disk layout or VM settings are changed.')
        if input('Type INSTALL to continue, or press Return to cancel: ').strip() != 'INSTALL':
            print('Cancelled. Nothing was installed. The reviewed build is retained.')
            return {'cancelled': True}
        state_write(work, status='installing', attempt=state['attempt'], machine=plan['machine'], planSha256=expected)
        try:
            # Do not capture stdin, stdout or stderr. The password belongs to sudo, never to this program.
            code = subprocess.run(sudo_command(plan_path, expected, plan['guidedSha256'])).returncode
            if code != 0:
                journal = real_chain(JOURNALS / expected)
                if not journal.exists():
                    # The root runner journals before either install. A fully exited sudo with
                    # no journal and still-fresh underlying guards proves no install began.
                    runtime.host_guard(plan['machine'])
                    fresh = installer.inspect_host(plan['account']['user'])
                    require(fresh['eligibleForInstall'], 'Target changed despite no root journal; preserve it for recovery.')
                    state_write(work, status='ready_for_review', attempt=state['attempt'], machine=plan['machine'], planSha256=expected,
                                message='Administrator request did not start installation. Fresh-target checks still pass.')
                    print('Installation did not start. The build is retained; run start when ready.')
                    return {'installed': False, 'authenticationCompleted': False, 'safeToReopenPrompt': True}
                raise Refused('Installation did not complete. Run verify; do not repeat sudo or delete partial files.')
            # Protected sudoers files require root for the complete readback. The grouped
            # operation has just performed it; consume its root-owned, bound receipt.
            recorded = read_journal(plan, expected)
            require(recorded['phase'] == 'installed_files_verified' and
                    recorded.get('verification', {}).get('installedFilesVerified') is True,
                    'Root readback proof is incomplete; run verify.')
            result = recorded['verification']
            state_write(work, status='installed', attempt=state['attempt'], machine=plan['machine'], planSha256=expected, verification=result)
            return result
        except BaseException:
            state_write(work, status='installation_unverified', attempt=state['attempt'], machine=plan['machine'], planSha256=expected,
                        message='The administrator step failed or was interrupted. Run verify. No automatic install retry is allowed.')
            raise

def read_journal(plan, expected):
    journal = JOURNALS / expected / 'RESULT.json'
    require(journal.exists(), 'No privileged attempt receipt. No success is inferred; retain the build and review before a new attempt.')
    for directory in (JOURNALS, journal.parent):
        verify_owned(directory, 0o755, directory=True)
    verify_owned(journal, 0o644)
    value = read_json(journal)
    require(value.get('planSha256') == expected and value.get('machine') == plan['machine'], 'Installation journal binding differs.')
    return value


def root_verify(plan_path, expected):
    require(os.geteuid() == 0, 'Full file verification needs read access to protected installation files.')
    plan, runtime, installer, receipt, recipe = load_plan(plan_path, expected)
    require(os.environ.get('SUDO_UID') == str(plan['account']['uid']), 'Verify from the reviewed desktop account.')
    journal = read_journal(plan, expected)
    result = installed_verification(plan, runtime, installer, receipt, recipe, expected)
    return {**result, 'recordedPhase': journal['phase'],
            'recoveredByReadOnlyVerification': journal['phase'] != 'installed_files_verified'}


def verify(work, expected):
    # Files and state stay unchanged; sudo supplies read access to the protected sudoers file.
    require(os.geteuid() != 0, 'Start verification as the desktop account.')
    state = current_state(work)
    require(state.get('planSha256') == expected, 'Saved plan hash differs.')
    plan_path = Path(state['attempt']) / 'PLAN.json'
    plan, _, _, _, _ = load_plan(plan_path, expected)
    read_journal(plan, expected)
    require(sys.stdin.isatty(), 'Full read-only verification needs a local terminal for sudo.')
    print('Read-only verification needs local administrator access to the protected installation files.', file=sys.stderr)
    result = subprocess.run(sudo_command(plan_path, expected, plan['guidedSha256'], 'root-verify'),
                            stdout=subprocess.PIPE, text=True)
    require(result.returncode == 0, 'Installed state could not be proved. Keep the current files for recovery review; do not replay install.')
    return json.loads(result.stdout)


def start(media, expected_manifest, work, user=None):
    """Customer entry point: build, show the reviewed change, then one local sudo request."""
    require(os.geteuid() != 0, 'Start setup as your desktop account, not root.')
    require(sys.stdin.isatty(), 'Start needs a local terminal for review and the masked sudo prompt.')
    state = current_state(work)
    if state['status'] in ('installed', 'installation_unverified') or state.get('lastRecordedStatus') == 'installing':
        require(state['status'] == 'installed',
                'The administrator step was interrupted. Run verify and retain this attempt for recovery review; it cannot be replayed.')
        return verify(work, state['planSha256'])
    if state['status'] != 'ready_for_review':
        require(media is not None and expected_manifest is not None,
                'Start needs the verified installation media and its separate checksum-list hash.')
        prepare(media, expected_manifest, work, user)
        state = current_state(work)
    plan, _, _, receipt, recipe = load_plan(Path(state['attempt']) / 'PLAN.json', state['planSha256'])
    require(expected_manifest is None or expected_manifest == plan['manifestSha256'],
            'This ready build uses different media. Retain it and choose a separate working directory for other inputs.')
    print('\nReady to install Decrypt')
    print('Desktop account: ' + plan['account']['user'])
    print('Target identity: ' + plan['machine'])
    print(f"Change: {len(receipt['files'])} runtime entries and {len(recipe['files'])} application files.")
    print('Locations: /usr/local/lib/decrypt-os-runtime and /usr/local/lib/decrypt-os, with reviewed desktop and service definitions.')
    print('Review: ' + str(Path(state['attempt']) / 'review/rootfs'))
    print('No provider accounts are connected. Services stay stopped. Disks and virtual machines are not changed.')
    return install(work, state['planSha256'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='status', choices=('status', 'start', 'prepare', 'install', 'verify', 'root-apply', 'root-verify'))
    parser.add_argument('--media', type=Path)
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--work', type=Path, default=Path.home() / '.local/state/decrypt-setup')
    parser.add_argument('--user')
    parser.add_argument('--plan', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--plan-sha256')
    args = parser.parse_args(argv)
    try:
        if args.action == 'status':
            result = current_state(args.work)
            result['steps'] = list(STEPS)
            result['next'] = 'Complete base/desktop checks, then prepare; review the plan before install.' if result['status'] == 'not_started' else 'See saved status. A failed privileged install needs verification, not automatic replay.'
        elif args.action == 'start':
            result = start(args.media, args.manifest_sha256, args.work, args.user)
        elif args.action == 'prepare':
            require(args.media is not None and args.manifest_sha256 is not None, 'Prepare needs verified media and its separate checksum-list hash.')
            result = prepare(args.media, args.manifest_sha256, args.work, args.user)
            result = {k: v for k, v in result.items() if k != 'inputRecords'}
        elif args.action == 'install':
            result = install(args.work, args.plan_sha256)
        elif args.action == 'verify':
            result = verify(args.work, args.plan_sha256)
        else:
            require(args.plan is not None, 'Missing reviewed installation plan.')
            result = (root_verify if args.action == 'root-verify' else root_apply)(args.plan, args.plan_sha256)
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print('Stopped. Existing files were kept. Run status before continuing.', file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print('Setup stopped: ' + str(exc), file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
