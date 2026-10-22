#!/usr/bin/env python3
"""Fill a builder cache's runtime-inputs/ directory for this machine's architecture.

build/target_app.py (stage "app") reads <cache>/runtime-inputs/: the reviewed
metadata files (provision_runtime.INPUT_FILES), archives/<Node>.tar.xz,
archives/openclaw-2026.9.9.tgz and wheels/<one wheel per Python package>.
This helper creates that directory on a fresh builder, typically the native
x86_64 one, where no earlier cache exists:

  - the five Python wheel metadata files come from this repo (factory/builder/),
    checked against provision_runtime.PYTHON_METADATA_SHA256;
  - openclaw.json, npm/package.json and npm/package-lock.json are the existing
    reviewed files (architecture independent), copied from --metadata-from and
    checked against provision_runtime.NPM_LOCK_HASH / OPENCLAW_SHA512;
  - node.json is written for this architecture's pinned Node archive;
  - the Node archive, the OpenClaw tarball and the wheels this Python selects
    are downloaded over HTTPS from their pinned hosts and accepted only if they
    match the pinned sha256/sha512.

Nothing is installed, extracted or executed. Run it with the same Python
version the image ships (the builder and image both use the pinned Arch
Linux snapshot), so wheel selection matches what target_app.py repeats
inside the image:

    python3 factory/builder/seed_runtime_inputs.py \\
      --metadata-from /path/to/reviewed-metadata \\
      --output /var/lib/decrypt-builder/cache/runtime-inputs
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
HOSTS = ('nodejs.org', 'registry.npmjs.org', 'files.pythonhosted.org')
NODE_BASE = 'https://nodejs.org/dist/v24.19.0/'


def load_provisioner():
    spec = importlib.util.spec_from_file_location('provision_runtime', HERE / 'provision_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def node_metadata(provisioner):
    """node.json for this architecture, in the shape fetch_runtime_inputs.py checks."""
    return {'name': 'node', 'source': NODE_BASE + 'SHASUMS256.txt',
            'files': [{'filename': provisioner.NODE_NAME + '.tar.xz', 'sha256': provisioner.NODE_HASH}]}


def python_tags():
    from pip._vendor.packaging.tags import sys_tags
    return [str(tag) for tag in sys_tags()]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit('refusing redirect: ' + newurl)


def download(url, destination, algorithm, expected, maximum=512 * 1024**2):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.query or parsed.username:
        raise SystemExit('refusing unpinned download URL: ' + url)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    digest = hashlib.new(algorithm)
    partial = destination.with_name(destination.name + '.part')
    with opener.open(url, timeout=300) as response, open(partial, 'xb') as out:
        total = 0
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > maximum:
                raise SystemExit('download too large: ' + url)
            digest.update(block)
            out.write(block)
    if digest.hexdigest() != expected:
        partial.unlink()
        raise SystemExit(f'{algorithm} mismatch for {url}')
    partial.chmod(0o644)
    partial.rename(destination)
    print('ok', destination.name, flush=True)


def seed(metadata_from, output, provisioner, tags, fetch=download):
    output = Path(output)
    if output.exists():
        raise SystemExit(f'{output} already exists; choose a new directory')
    records = {}
    for name in provisioner.VERSIONS:
        records[name + '.json'] = hashlib.sha256((HERE / (name + '.json')).read_bytes()).hexdigest()
    if provisioner.python_metadata_aggregate(records) != provisioner.PYTHON_METADATA_SHA256:
        raise SystemExit('factory/builder wheel metadata does not match PYTHON_METADATA_SHA256')
    lock = Path(metadata_from) / 'npm/package-lock.json'
    if hashlib.sha256(lock.read_bytes()).hexdigest() != provisioner.NPM_LOCK_HASH:
        raise SystemExit('npm/package-lock.json does not match provision_runtime.NPM_LOCK_HASH')
    openclaw = json.loads((Path(metadata_from) / 'openclaw.json').read_text())
    tarball = openclaw['dist']['tarball']
    output.mkdir(mode=0o755, parents=True)
    for sub in ('npm', 'archives', 'wheels'):
        (output / sub).mkdir(mode=0o755)
    for name in provisioner.VERSIONS:
        shutil.copyfile(HERE / (name + '.json'), output / (name + '.json'))
    for name in ('openclaw.json', 'npm/package.json', 'npm/package-lock.json'):
        shutil.copyfile(Path(metadata_from) / name, output / name)
    (output / 'node.json').write_text(json.dumps(node_metadata(provisioner), sort_keys=True, separators=(',', ':')) + '\n')
    fetch(NODE_BASE + provisioner.NODE_NAME + '.tar.xz', output / 'archives' / (provisioner.NODE_NAME + '.tar.xz'),
          'sha256', provisioner.NODE_HASH)
    fetch(tarball, output / 'archives/openclaw-2026.9.9.tgz', 'sha512', provisioner.OPENCLAW_SHA512)
    metadata = {name: json.loads((output / (name + '.json')).read_text()) for name in provisioner.VERSIONS}
    for row in provisioner.select_wheels(metadata, tags):
        fetch(row['url'], output / 'wheels' / row['filename'], 'sha256', row['sha256'])
    info = provisioner.verify_inputs(output)
    print(json.dumps({'runtimeInputs': str(output), 'arch': provisioner.TARGET_ARCH, 'node': provisioner.NODE_NAME,
                      'inputsSha256': info['sha256']}, indent=2))
    return info


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--metadata-from', required=True, type=Path,
                   help='directory with the reviewed openclaw.json and npm/package{,-lock}.json')
    p.add_argument('--output', required=True, type=Path, help='new runtime-inputs directory')
    args = p.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('run on the Linux builder')
    os.umask(0o022)
    seed(args.metadata_from, args.output, load_provisioner(), python_tags())


if __name__ == '__main__':
    main()
