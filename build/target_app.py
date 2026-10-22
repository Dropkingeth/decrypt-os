#!/usr/bin/env python3
"""Build and install the Decrypt app + runtime. Runs INSIDE the new image (arch-chroot).

Called by build/guest_build.py (stage "app"). Running inside the image means the
Python venv matches the image's own Python ABI. Everything is offline: npm uses a
local cache filled from verified archives, pip uses retained wheels with hashes.

Installs:
  /usr/local/lib/decrypt-os-runtime/{node-v24.19.0-linux-arm64 (x86_64: -linux-x64),openclaw,python}
  the generic (account-free) app rootfs rendered by installer/clean_base.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(argv, env, cwd=None):
    print('$', ' '.join(str(a) for a in argv), flush=True)
    subprocess.run([str(a) for a in argv], env=env, cwd=cwd, check=True)


def npm_inputs(lock_path, archives, destination, helper):
    """Lay out <destination>/npm/package-lock.json + npm-archives/ for provision_runtime's cache filler."""
    lock = json.loads(Path(lock_path).read_text())
    (destination / 'npm').mkdir(parents=True)
    shutil.copyfile(lock_path, destination / 'npm/package-lock.json')
    (destination / 'npm-archives').mkdir()
    for name, row in lock['packages'].items():
        if name and 'resolved' in row:
            archive = helper.sha(row['resolved'].encode()) + '.tgz'
            if not (destination / 'npm-archives' / archive).exists():
                shutil.copyfile(Path(archives) / archive, destination / 'npm-archives' / archive)
    return destination


def build_runtime(helper, inputs, archives, work):
    target = helper.TARGET
    if target.exists():
        raise SystemExit(f'{target} already exists')
    helper.verify_inputs(inputs)  # npm lock, Node and OpenClaw archive hashes, wheel metadata
    work.mkdir(mode=0o700)
    home = work / 'home'
    home.mkdir(mode=0o700)
    target.mkdir(mode=0o755, parents=True)
    node_src = helper.extract_checked(inputs / 'archives' / (helper.NODE_NAME + '.tar.xz'), work / 'node-extract', helper.NODE_NAME)
    node_src.rename(target / helper.NODE_NAME)
    node = target / helper.NODE_NAME
    env = helper.clean_environment(home, node)
    version = subprocess.check_output([node / 'bin/node', '--version'], env=env, text=True).strip()
    if version != 'v24.19.0':
        raise SystemExit('unexpected Node version ' + version)
    # OpenClaw from the locked npm tree, offline, no install scripts.
    staged = npm_inputs(inputs / 'npm/package-lock.json', archives, work / 'npm-in', helper)
    wrapper = work / 'npm-wrapper'
    wrapper.mkdir()
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(inputs / 'npm' / name, wrapper / name)
    for name in ('empty-user.npmrc', 'empty-global.npmrc'):
        (work / name).write_text('')
    helper.prepare_offline_npm_cache(staged, node, work, env)
    run(helper.npm_command(node, work), env, wrapper)
    if helper.hash_file(wrapper / 'package-lock.json') != helper.NPM_LOCK_HASH:
        raise SystemExit('npm changed the runtime lockfile')
    helper.contain_npm_tree(wrapper, target / 'openclaw')
    # Python venv with the retained, hash-pinned wheels.
    python = target / 'python'
    run(['/usr/bin/python3', '-I', '-m', 'venv', '--copies', python], env)
    executable = python / 'bin/python'
    tags = json.loads(subprocess.check_output([executable, '-I', '-c',
        'import json;from pip._vendor.packaging.tags import sys_tags;print(json.dumps([str(t) for t in sys_tags()]))'], env=env, text=True))
    metadata = {name: json.loads((inputs / (name + '.json')).read_text()) for name in helper.VERSIONS}
    wheels = work / 'wheels'
    wheels.mkdir()
    lines = []
    for row in helper.select_wheels(metadata, tags):
        helper.retained_wheel(row, wheels / row['filename'], inputs)
        lines.append(f"{wheels / row['filename']} --hash=sha256:{row['sha256']}")
    (work / 'wheels.txt').write_text('\n'.join(lines) + '\n')
    run([executable, '-I', '-m', 'pip', '--isolated', 'install', '--no-index', '--no-deps', '--no-compile',
         '--only-binary=:all:', '--require-hashes', '-r', work / 'wheels.txt'], env)
    versions = json.loads(subprocess.check_output([executable, '-I', '-c',
        'import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ("cryptography","cffi","pycparser","SecretStorage","jeepney")}))'],
        env=env, text=True))
    if versions != helper.VERSIONS:
        raise SystemExit(f'python runtime versions differ: {versions}')
    print('runtime ok:', version, versions, flush=True)
    return node, env


def build_app(src, archives, work, helper, node):
    app_source = src / 'candidate/app-source'
    bundler = load('build_source_bundle', app_source / 'lab/build_source_bundle.py')
    work.mkdir(mode=0o700)
    receipt = bundler.build(app_source, work / 'bundle', customer_base=True)
    print('source bundle:', receipt['files'], 'files', flush=True)
    extracted = work / 'source'
    with tarfile.open(work / 'bundle/decrypt-os-source.tar.gz', 'r:gz') as archive:
        archive.extractall(work / 'unpacked', filter='data')
    (work / 'unpacked/decrypt-os-source').rename(extracted)
    # UI: offline npm ci + vite build with the runtime's Node.
    home = work / 'home'
    home.mkdir(mode=0o700)
    env = helper.clean_environment(home, node)
    env.update(TMPDIR=str(work / 'tmp'), TZ='UTC', LC_ALL='C.UTF-8')
    (work / 'tmp').mkdir(mode=0o700)
    for name in ('empty-user.npmrc', 'empty-global.npmrc'):
        (work / name).write_text('')
    ui = extracted / 'app/ui'
    staged = npm_inputs(ui / 'package-lock.json', archives, work / 'npm-in', helper)
    helper.prepare_offline_npm_cache(staged, node, work, env)
    run(helper.npm_command(node, work), env, ui)
    cli = [node / 'bin/node', node / 'lib/node_modules/npm/bin/npm-cli.js']
    run(cli + ['--userconfig=' + str(work / 'empty-user.npmrc'), '--globalconfig=' + str(work / 'empty-global.npmrc'),
               '--cache=' + str(work / 'npm-cache'), '--ignore-scripts', '--offline', 'run', 'build'], env, ui)
    # Render the generic account-free app rootfs with the app's own renderer.
    renderer = load('clean_base', extracted / 'installer/clean_base.py')
    renderer.render(extracted, work / 'recipe', user=None)
    rootfs = work / 'recipe/rootfs'
    installed = 0
    for path in sorted(rootfs.rglob('*')):
        relative = path.relative_to(rootfs)
        destination = Path('/') / relative
        if path.is_dir():
            destination.mkdir(mode=0o755, exist_ok=True)
            continue
        if destination.exists() or destination.is_symlink():
            raise SystemExit(f'app file would overwrite /{relative}')
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        destination.chmod(path.stat().st_mode & 0o777)
        installed += 1
    print('app files installed:', installed, flush=True)
    # The app verifies its installed files against this list at every start
    # (installer/version_bootstrap.py); first start adds the account receipt.
    recipe = json.loads((work / 'recipe/RECIPE-RECEIPT.json').read_text())
    listing = Path('/usr/local/lib/decrypt-os/INSTALLED-FILES.json')
    if listing.exists():
        raise SystemExit('app file would overwrite ' + str(listing))
    listing.write_text(json.dumps({'schema': 'decrypt.installed-files.v1',
                                   'files': [{k: row[k] for k in ('path', 'bytes', 'sha256')} for row in recipe['files']]},
                                  indent=2, sort_keys=True) + '\n')
    listing.chmod(0o644)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('src', 'runtime-inputs', 'npm-archives', 'work'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    os.umask(0o022)
    helper = load('provision_runtime', a.src / 'factory/builder/provision_runtime.py')
    node, _ = build_runtime(helper, a.runtime_inputs, a.npm_archives, a.work / 'runtime')
    build_app(a.src, a.npm_archives, a.work / 'app', helper, node)


if __name__ == '__main__':
    main()
