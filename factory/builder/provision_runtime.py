#!/usr/bin/env python3
"""Pinned customer runtime candidate. Default: inspect inputs, never install."""
from __future__ import annotations
import argparse
import contextlib
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse

TARGET = Path('/usr/local/lib/decrypt-os-runtime')
HOST_BINDING_PATH = None
HOST_BINDING_SHA256 = None
# Node.js distribution per image architecture; sha256 values from
# https://nodejs.org/dist/v24.19.0/SHASUMS256.txt. The runtime is built inside
# the image itself (build/target_app.py), so this machine's architecture is the
# image's. Anything other than x86_64 keeps the original linux-arm64 pin.
NODE_ARCHIVES = {
    'aarch64': ('node-v24.19.0-linux-arm64', '01443c1e1a29e531ccad5a46fefa6df490d2189c49f7955904aecdbb0fe86fdc'),
    'x86_64': ('node-v24.19.0-linux-x64', '14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647'),
}
TARGET_ARCH = 'x86_64' if platform.machine() == 'x86_64' else 'aarch64'
NODE_NAME, NODE_HASH = NODE_ARCHIVES[TARGET_ARCH]
NPM_LOCK_HASH = '6098915b07cd9296d1e3b831a5e4dce85607aaa80b24e0d18d60797fca2a1d22'
OPENCLAW_SHA512 = 'dec07a7a3ab9b26068cdf0615440df2b8f301696e4c7ed1fe8f275f6a6630b769bdd1a05ad83b9f029c4da602c6d4cbcb17771741b63437be402a716f4165703'
VERSIONS = {'cryptography': '46.0.5', 'cffi': '2.1.1', 'pycparser': '3.0',
            'SecretStorage': '3.3.3', 'jeepney': '0.9.0'}
# sha256 of the canonical (sorted-key, compact) JSON of
# {"<name>.json": sha256(file bytes), ...} for every name in VERSIONS. Field-
# level checks below (name/version/host/hex-digest shape) constrain what a
# *replaced* metadata file could say, but only this constant pins the exact
# reviewed file bytes -- the same role NPM_LOCK_HASH plays for the npm lock.
PYTHON_METADATA_SHA256 = 'f720e3be18f63a81cdb5aca3d09b38b0682f076192582485a1eb84596bec96ca'
INPUT_FILES = ('node.json', 'openclaw.json', 'cryptography.json', 'cffi.json', 'pycparser.json',
               'SecretStorage.json', 'jeepney.json', 'npm/package.json', 'npm/package-lock.json')

class Refused(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def open_directory_chain(path):
    path = Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in ('.', '..'):
                raise Refused('Unnormalized source directory refused.')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def open_regular(path, expected_size=None):
    path = Path(path).absolute()
    directory = open_directory_chain(path.parent)
    file = None
    try:
        file = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(file)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 512 * 1024 * 1024 or
                expected_size is not None and info.st_size != expected_size):
            raise Refused('Dependency input changed or is not a bounded regular file.')
        yield file, info.st_size
    finally:
        if file is not None:
            os.close(file)
        os.close(directory)


def read_regular(path, maximum=4 * 1024 * 1024):
    with open_regular(path) as (fd, size):
        if size > maximum:
            raise Refused('Dependency metadata exceeds its size limit.')
        pieces = []
        remaining = size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise Refused('Dependency input changed while reading.')
            pieces.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise Refused('Dependency input grew while reading.')
        return b''.join(pieces)


def hash_file(path, algorithm='sha256'):
    value = hashlib.new(algorithm)
    transfer_start=time.monotonic();transferred=0
    with open_regular(path) as (fd, size):
        remaining = size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise Refused('Dependency input changed while reading.')
            value.update(chunk)
            remaining -= len(chunk)
            transferred += len(chunk)
            time.sleep(max(0,transferred/(20*1024**2)-(time.monotonic()-transfer_start)))
        if os.read(fd, 1):
            raise Refused('Dependency input grew while reading.')
    return value.hexdigest()


def copy_reviewed_file(source_root_fd, relative, destination, row):
    parts = safe_relative(relative).split('/')
    parent = os.dup(source_root_fd)
    source = None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(source)
        size = row.get('bytes')
        if (type(size) is not int or size < 0 or size > 512 * 1024 * 1024 or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or info.st_size != size):
            raise Refused('Reviewed source file changed before copying.')
        remaining, value = size, hashlib.sha256()
        transfer_start=time.monotonic();transferred=0
        with Path(destination).open('xb') as output:
            while remaining:
                chunk = os.read(source, min(remaining, 1024 * 1024))
                if not chunk:
                    raise Refused('Reviewed source changed while copying.')
                value.update(chunk)
                output.write(chunk)
                remaining -= len(chunk)
                transferred += len(chunk)
                time.sleep(max(0,transferred/(20*1024**2)-(time.monotonic()-transfer_start)))
            if os.read(source, 1) or value.hexdigest() != row['sha256']:
                raise Refused('Copied file differs from the reviewed dependency build.')
        Path(destination).chmod(int(row['mode'], 8))
    finally:
        if source is not None:
            os.close(source)
        os.close(parent)


PYTHON_IDENTITY_CODE = 'import json,sys,sysconfig;print(json.dumps({"version":list(sys.version_info[:3]),"cacheTag":sys.implementation.cache_tag,"soabi":sysconfig.get_config_var("SOABI")}))'


def python_identity(executable, env):
    return json.loads(command([str(executable), '-I', '-B', '-c', PYTHON_IDENTITY_CODE], env, timeout=10))


def require_python_identity(receipt):
    current = python_identity('/usr/bin/python3', {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    if current != receipt.get('pythonIdentity'):
        raise Refused('Target Python version or ABI differs from the reviewed build guest.')


def safe_relative(name):
    if not name or '\\' in name or '\x00' in name or PurePosixPath(name).is_absolute():
        raise Refused('Invalid relative path.')
    if any(part in ('', '.', '..') for part in name.split('/')):
        raise Refused('Invalid relative path.')
    return name


def public_url(url, host):
    value = urllib.parse.urlsplit(url)
    if value.scheme != 'https' or value.hostname != host or value.username or value.password or value.port or value.fragment or value.query:
        raise Refused('A dependency URL is not an approved HTTPS source.')
    return url


def python_metadata_aggregate(records):
    return sha(json.dumps({name + '.json': records[name + '.json'] for name in VERSIONS},
                           sort_keys=True, separators=(',', ':')).encode())


def verify_inputs(inputs):
    root = Path(inputs).absolute()
    records = {}
    for name in INPUT_FILES:
        path = root / name
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
            raise Refused('Linked dependency inputs are not accepted.')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 3 * 1024 * 1024:
            raise Refused('Dependency metadata is not a bounded regular file.')
        records[name] = sha(path.read_bytes())
    if records['npm/package-lock.json'] != NPM_LOCK_HASH:
        raise Refused('The reviewed npm lockfile changed.')
    if python_metadata_aggregate(records) != PYTHON_METADATA_SHA256:
        raise Refused('The reviewed Python wheel metadata changed.')
    lock = json.loads((root / 'npm/package-lock.json').read_text())
    if lock.get('lockfileVersion') != 3 or lock.get('packages', {}).get('', {}).get('dependencies') != {'openclaw': '2026.9.9'}:
        raise Refused('Unexpected dependency lock root.')
    for name, record in lock['packages'].items():
        if not name:
            continue
        safe_relative(name)
        if record.get('inBundle') is True:
            # Bundled inside its parent's own tarball (e.g. npm's dependency
            # tree, now pulled in transitively by OpenClaw 2026.9.9); never
            # fetched or cached separately, so it carries no resolved/integrity.
            continue
        public_url(record.get('resolved', ''), 'registry.npmjs.org')
        if not re.fullmatch(r'sha(?:256|512)-[A-Za-z0-9+/]+={0,2}', record.get('integrity', '')):
            raise Refused('Every npm package needs locked integrity.')
    for name, version in VERSIONS.items():
        metadata = json.loads((root / (name + '.json')).read_text())
        if metadata.get('name') != name or metadata.get('version') != version:
            raise Refused('Python package metadata changed version.')
        for row in metadata.get('files', []):
            public_url(row.get('url', ''), 'files.pythonhosted.org')
            if not re.fullmatch('[0-9a-f]{64}', row.get('sha256', '')):
                raise Refused('Python artifacts need a SHA-256 digest.')
    for name, algorithm, expected in ((NODE_NAME + '.tar.xz', 'sha256', NODE_HASH), ('openclaw-2026.9.9.tgz', 'sha512', OPENCLAW_SHA512)):
        path = root / 'archives' / name
        if path.is_symlink() or not path.is_file() or hash_file(path, algorithm) != expected:
            raise Refused('A retained public archive failed integrity verification.')
    return {'schema': 'decrypt.runtime-inputs.v1', 'files': records,
            'sha256': sha(json.dumps(records, sort_keys=True, separators=(',', ':')).encode()),
            'lockedNpmPackages': len(lock['packages']) - 1, 'signatureVerification': False}


def private_host_binding():
    if HOST_BINDING_PATH is None or not re.fullmatch('[0-9a-f]{64}', HOST_BINDING_SHA256 or ''):
        raise Refused('Explicit independently pinned private host binding required.')
    path = Path(HOST_BINDING_PATH).absolute()
    raw = read_regular(path, 65536)
    info = path.lstat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid() or sha(raw) != HOST_BINDING_SHA256:
        raise Refused('Private host binding identity/permissions differ.')
    value = json.loads(raw)
    if (set(value) != {'schema','targetMachineSha256','protectedMachineSha256','targetPythonIdentity'}
            or value['schema'] != 'decrypt.runtime-build-host.v1'
            or not re.fullmatch('[0-9a-f]{64}', value['targetMachineSha256'])
            or not isinstance(value['protectedMachineSha256'],list) or not value['protectedMachineSha256']
            or not all(re.fullmatch('[0-9a-f]{64}', x) for x in value['protectedMachineSha256'])
            or value['targetMachineSha256'] in value['protectedMachineSha256']):
        raise Refused('Invalid explicit target/protected host binding.')
    return value


def host_guard(expected_machine=None, building=False):
    if platform.system() != 'Linux' or platform.machine() not in NODE_ARCHIVES:
        raise Refused('Build requires the selected disposable Linux ARM64 or x86_64 guest.')
    binding = private_host_binding()
    machine = Path('/etc/machine-id').read_text().strip()
    if not re.fullmatch('[0-9a-f]{32}',machine):
        raise Refused('The build guest needs its own machine identity.')
    identity = sha(machine.encode())
    if identity in binding['protectedMachineSha256'] or identity != binding['targetMachineSha256']:
        raise Refused('Guest is protected or differs from the selected build target.')
    if expected_machine is not None and identity != expected_machine:
        raise Refused('The selected target machine does not match.')
    current_python = python_identity('/usr/bin/python3', {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    if current_python != binding['targetPythonIdentity']:
        raise Refused('Build Python ABI differs from the intended image runtime.')
    for item in (TARGET, Path('/usr/local/lib/decrypt-os'), Path('/etc/decrypt-os'), Path('/run/decrypt-os-customer-pilot'), Path('/var/lib/decrypt-os/customer-storage')):
        if item.exists() or item.is_symlink():
            raise Refused('Existing customer runtime or installation found; nothing is overwritten.')
    for home in Path('/home').iterdir():
        for name in ('.openclaw', '.local/state/decrypt-os'):
            if (home / name).exists() or (home / name).is_symlink():
                raise Refused('Existing assistant/development state found. Use a fresh guest.')
    for port in (8810, 8811, 19789, 18789):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                raise Refused('A customer/development service is already listening.')
    if building and os.geteuid() == 0:
        raise Refused('Build dependencies as the fresh guest user, never root.')
    return identity


def extract_checked(archive, destination, prefix):
    destination = Path(destination)
    if destination.exists():
        raise Refused('Archive destination already exists.')
    with tarfile.open(archive, 'r:*') as handle:
        entries = handle.getmembers()
        paths, total = set(), 0
        for member in entries:
            name = member.name.rstrip('/')
            safe_relative(name)
            if name != prefix and not name.startswith(prefix + '/'):
                raise Refused('Archive has an unexpected top-level directory.')
            if name in paths or not (member.isfile() or member.isdir() or member.issym()):
                raise Refused('Duplicate, linked or special archive member refused.')
            paths.add(name)
            total += member.size
            if total > 1024 * 1024 * 1024:
                raise Refused('Archive exceeds the extraction limit.')
        for member in entries:
            if member.issym():
                target = Path(member.name).parent / member.linkname
                normalized = Path(os.path.normpath(str(target)))
                if os.path.isabs(member.linkname) or not normalized.is_relative_to(prefix):
                    raise Refused('Archive symlink leaves its component.')
                if any(other.startswith(member.name.rstrip('/') + '/') for other in paths):
                    raise Refused('Archive writes through a symlink.')
        destination.mkdir(mode=0o700)
        for member in sorted(entries, key=lambda item: item.issym()):
            path = destination / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True, mode=0o755)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with path.open('xb') as output:
                    shutil.copyfileobj(handle.extractfile(member), output)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                path.symlink_to(member.linkname)
    return destination / prefix


def clean_environment(home, node=None):
    return {'HOME': str(home), 'PATH': (str(node / 'bin') + ':' if node else '') + '/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'npm_config_ignore_scripts': 'true', 'npm_config_audit': 'false', 'npm_config_fund': 'false',
            'npm_config_update_notifier': 'false', 'PIP_CONFIG_FILE': '/dev/null', 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}


def command(args, env, cwd=None, timeout=1200):
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise Refused('A pinned dependency operation failed. No scripts or fallback were enabled.')
    return result.stdout


def npm_command(node, work):
    return [str(node / 'bin/node'), str(node / 'lib/node_modules/npm/bin/npm-cli.js'), 'ci', '--ignore-scripts',
            '--no-audit', '--no-fund', '--no-progress', '--include=optional', '--offline', '--maxsockets=1',
            '--userconfig=' + str(work / 'empty-user.npmrc'), '--globalconfig=' + str(work / 'empty-global.npmrc'),
            '--cache=' + str(work / 'npm-cache')]


def contain_npm_tree(wrapper, target):
    """Preserve the exact nested+hoisted topology inside one mount."""
    wrapper, target = Path(wrapper), Path(target)
    package = wrapper / 'node_modules/openclaw'
    manifest = json.loads((package / 'package.json').read_text())
    if manifest.get('name') != 'openclaw' or manifest.get('version') != '2026.9.9':
        raise Refused('Installed OpenClaw does not match the lock.')
    target.mkdir(mode=0o755)
    (wrapper / 'node_modules').rename(target / 'node_modules')
    package = target / 'node_modules/openclaw'
    for name in ('package.json', 'LICENSE', 'README.md'):
        if (package / name).is_file():
            shutil.copyfile(package / name, target / name)
    (target / 'openclaw.mjs').symlink_to('node_modules/openclaw/openclaw.mjs')
    inventory(target)  # All relative links must remain inside this mount.


def inventory(root):
    root = Path(root).absolute()
    rows = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        safe_relative(relative)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if os.path.isabs(target) or not path.resolve().is_relative_to(root) or not path.exists():
                raise Refused('Runtime symlink leaves the component or is dangling.')
            rows.append({'path': relative, 'type': 'symlink', 'target': target})
        elif stat.S_ISDIR(info.st_mode):
            rows.append({'path': relative, 'type': 'directory', 'mode': '0755'})
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            rows.append({'path': relative, 'type': 'file', 'bytes': info.st_size,
                         'mode': '0755' if info.st_mode & 0o111 else '0644', 'sha256': hash_file(path)})
        else:
            raise Refused('Runtime contains linked or special files.')
    return rows


def retained_wheel(row, destination, inputs):
    name = safe_relative(row['filename'])
    if Path(name).name != name:
        raise Refused('Wheel filename is not a basename.')
    source = Path(inputs) / 'wheels' / name
    if hash_file(source) != row['sha256']:
        raise Refused('Retained wheel differs from pinned metadata.')
    size = source.stat().st_size
    source_fd = open_directory_chain(source.parent)
    try:
        copy_reviewed_file(source_fd,name,destination,{'bytes':size,'sha256':row['sha256'],'mode':'0644'})
    finally:
        os.close(source_fd)


def prepare_offline_npm_cache(inputs, node, work, env):
    lock = json.loads(read_regular(Path(inputs) / 'npm/package-lock.json', 3*1024**2))
    archives = []
    for name, row in lock['packages'].items():
        if not name or row.get('inBundle') is True: continue
        public_url(row['resolved'],'registry.npmjs.org')
        algorithm, encoded = row['integrity'].split('-',1)
        expected = base64.b64decode(encoded,validate=True).hex()
        archive = Path(inputs) / 'npm-archives' / (sha(row['resolved'].encode())+'.tgz')
        if hash_file(archive,algorithm) != expected:
            raise Refused('Retained npm archive differs from locked integrity.')
        archives.append(archive)
    for archive in dict.fromkeys(archives):
        command([str(node/'bin/node'),str(node/'lib/node_modules/npm/bin/npm-cli.js'),
                 'cache','add',str(archive),'--offline','--ignore-scripts','--no-audit','--no-fund',
                 '--userconfig='+str(work/'empty-user.npmrc'),'--globalconfig='+str(work/'empty-global.npmrc'),
                 '--cache='+str(work/'npm-cache')],env,work)


def select_wheels(metadata, supported_tags):
    """Filename tags are compared without importing a downloaded package."""
    result = []
    priorities = {value: index for index, value in enumerate(supported_tags)}
    for name, version in VERSIONS.items():
        candidates = []
        for row in metadata[name]['files']:
            if row.get('type') != 'bdist_wheel' or not row['filename'].endswith('.whl'):
                continue
            parts = row['filename'][:-4].rsplit('-', 3)
            if len(parts) != 4:
                continue
            tags = [a + '-' + b + '-' + c for a in parts[1].split('.') for b in parts[2].split('.') for c in parts[3].split('.')]
            ranks = [priorities[tag] for tag in tags if tag in priorities]
            if ranks:
                candidates.append((min(ranks), row['filename'], row))
        if not candidates:
            raise Refused('No retained wheel matches this Python ABI: ' + name)
        result.append(min(candidates, key=lambda value: (value[0], value[1]))[2])
    return result


def build(inputs, output, expected_inputs):
    host_guard(building=True)
    info = verify_inputs(inputs)
    if info['sha256'] != expected_inputs:
        raise Refused('Reviewed dependency metadata hash does not match.')
    inputs, output = Path(inputs).absolute(), Path(output).absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise Refused('Choose a new build directory under an existing parent.')
    output.mkdir(mode=0o700)
    artifact = output / 'runtime'
    artifact.mkdir(mode=0o755)
    with tempfile.TemporaryDirectory(prefix='.build-', dir=output) as temporary:
        work = Path(temporary)
        home = work / 'home'
        home.mkdir(mode=0o700)
        extracted = extract_checked(inputs / 'archives' / (NODE_NAME + '.tar.xz'), work / 'node-extract', NODE_NAME)
        extracted.rename(artifact / NODE_NAME)
        node = artifact / NODE_NAME
        env = clean_environment(home, node)
        if command([str(node / 'bin/node'), '--version'], env).strip() != 'v24.19.0':
            raise Refused('Node version does not match.')
        wrapper = work / 'npm-wrapper'
        wrapper.mkdir()
        for name in ('package.json', 'package-lock.json'):
            shutil.copyfile(inputs / 'npm' / name, wrapper / name)
        for name in ('empty-user.npmrc', 'empty-global.npmrc'):
            (work / name).write_text('')
        prepare_offline_npm_cache(inputs,node,work,env)
        command(npm_command(node, work), env, wrapper)
        if hash_file(wrapper / 'package-lock.json') != NPM_LOCK_HASH:
            raise Refused('npm changed the reviewed lockfile.')
        contain_npm_tree(wrapper, artifact / 'openclaw')
        python = artifact / 'python'
        command(['/usr/bin/python3', '-I', '-m', 'venv', '--copies', str(python)], env)
        executable = str(python / 'bin/python')
        tag_code = 'import json;from pip._vendor.packaging.tags import sys_tags;print(json.dumps([str(t) for t in sys_tags()]))'
        tags = json.loads(command([executable, '-I', '-c', tag_code], env))
        python_id = python_identity(executable, env)
        metadata = {name: json.loads((inputs / (name + '.json')).read_text()) for name in VERSIONS}
        selected = select_wheels(metadata, tags)
        wheels = work / 'wheels'
        wheels.mkdir()
        lines = []
        for row in selected:
            safe_relative(row['filename'])
            destination = wheels / row['filename']
            retained_wheel(row, destination, inputs)
            lines.append(str(destination) + ' --hash=sha256:' + row['sha256'])
        requirements = work / 'wheels.txt'
        requirements.write_text('\n'.join(lines) + '\n')
        command([executable, '-I', '-m', 'pip', '--isolated', 'install', '--no-index', '--no-deps', '--no-compile',
                 '--only-binary=:all:', '--require-hashes', '-r', str(requirements)], env)
        version_code = 'import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ("cryptography","cffi","pycparser","SecretStorage","jeepney")}))'
        if json.loads(command([executable, '-I', '-c', version_code], env)) != VERSIONS:
            raise Refused('Python dependency versions do not match.')
        # Make the copied venv's helper scripts refer to their final location.
        for file in [python / 'pyvenv.cfg', *list((python / 'bin').iterdir())]:
            if file.is_symlink() or not file.is_file():
                continue
            raw = file.read_bytes()
            if b'\x00' not in raw:
                file.write_bytes(raw.replace(str(python).encode(), str(TARGET / 'python').encode()))
        smoke_env = {**env, 'OPENCLAW_HOME': str(home / 'openclaw'), 'OPENCLAW_STATE_DIR': str(home / 'state'),
                     'OPENCLAW_CONFIG_PATH': str(home / 'config.json')}
        version = command([str(node / 'bin/node'), str(artifact / 'openclaw/openclaw.mjs'), '--version'], smoke_env, timeout=90)
        if '2026.9.9' not in version:
            raise Refused('OpenClaw version smoke check failed. No script fallback was enabled.')
    rows = inventory(artifact)
    receipt = {'schema': 'decrypt.runtime-build.v1', 'inputsSha256': info['sha256'], 'platform': 'linux-' + TARGET_ARCH,
               'pythonIdentity': python_id, 'lifecycleScriptsExecuted': False,
               'openclawVersionSmoke': True, 'rootInstallationVerified': False, 'signatureVerification': False,
               'networkFallbackAllowed': False, 'privateHostBindingShipped': False,
               'wheels': [{'filename': row['filename'], 'sha256': row['sha256']} for row in selected], 'files': rows}
    (output / 'DEPENDENCY-BUILD-RECEIPT.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    return {'built': True, 'installed': False, 'files': len(rows), 'receiptSha256': hash_file(output / 'DEPENDENCY-BUILD-RECEIPT.json')}


def verify_build(bundle, expected_receipt):
    bundle = Path(bundle).absolute()
    receipt_path = bundle / 'DEPENDENCY-BUILD-RECEIPT.json'
    receipt_data = read_regular(receipt_path, 32 * 1024 * 1024)
    if sha(receipt_data) != expected_receipt:
        raise Refused('Reviewed dependency build receipt does not match.')
    receipt = json.loads(receipt_data)
    if receipt.get('schema') != 'decrypt.runtime-build.v1' or receipt.get('platform') != 'linux-' + TARGET_ARCH or receipt.get('lifecycleScriptsExecuted') is not False or receipt.get('openclawVersionSmoke') is not True:
        raise Refused('The dependency build did not pass its required checks.')
    root = bundle / 'runtime'
    if root.is_symlink() or set(p.name for p in root.iterdir()) != {'openclaw', NODE_NAME, 'python'}:
        raise Refused('Unexpected dependency build layout.')
    if inventory(root) != receipt.get('files'):
        raise Refused('Dependency build files changed after review.')
    for component in ('openclaw', NODE_NAME, 'python'):
        inventory(root / component)  # No links between independently mounted components.
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    global HOST_BINDING_PATH, HOST_BINDING_SHA256
    parser.add_argument('action', nargs='?', choices=('inspect', 'build', 'verify'), default='inspect')
    parser.add_argument('--inputs', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--inputs-sha256')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--receipt-sha256')
    parser.add_argument('--host-binding',type=Path)
    parser.add_argument('--host-binding-sha256')
    args = parser.parse_args(argv)
    HOST_BINDING_PATH,HOST_BINDING_SHA256=args.host_binding,args.host_binding_sha256
    try:
        if args.action == 'inspect':
            result = {**verify_inputs(args.inputs), 'action': 'inspect', 'installed': False,
                      'next': 'Build as an unprivileged user on the fresh Linux ARM64 guest; the host and other existing VMs are not build targets.'}
        elif args.action == 'build':
            if not args.output or not args.inputs_sha256:
                raise Refused('Build requires --output and the reviewed --inputs-sha256.')
            result = build(args.inputs, args.output, args.inputs_sha256)
        else:
            if not args.bundle or not args.receipt_sha256:
                raise Refused('Verify requires bundle and reviewed receipt hash.')
            result = verify_build(args.bundle,args.receipt_sha256)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(json.dumps({'error': str(exc), 'installed': False}), file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
