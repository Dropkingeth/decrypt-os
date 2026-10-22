#!/usr/bin/env python3
"""Regenerate the package lock from the current offline repo cache.

One lock per image architecture: build/arch-package-lock.json (aarch64, the
default) and build/arch-package-lock-x86_64.json (--arch x86_64).

check_image (build/guest_build.py) verifies every package pacstrap actually
installs against this committed lock (name, version, sha256), so an offline
cache that silently drifted from a reviewed state fails the build instead of
shipping quietly.

Refresh it after changing a package list or running `build/build.sh --online`
(which re-syncs the cache from the mirrors and rebuilds decrypt-offline's
own database from exactly what got installed). Run inside the builder VM,
where the cache lives:

    limactl shell --workdir / decrypt-builder -- sudo python3 \\
      /path/to/repo/build/generate_package_lock.py \\
      --repo /var/lib/decrypt-builder/cache/pkg/aarch64 \\
      --output /path/to/repo/build/arch-package-lock.json

x86_64, on the native Arch Linux x86_64 builder (see build/build-x86_64.sh):

    sudo python3 build/generate_package_lock.py --arch x86_64 \\
      --repo /var/lib/decrypt-builder/cache/pkg/x86_64 \\
      --output build/arch-package-lock-x86_64.json

Then copy the output back to the repo checkout and commit it.
"""
from __future__ import annotations

import argparse
import json
import tarfile
from pathlib import Path


def parse_desc(raw):
    """One repo-add 'desc' entry: '%FIELD%\\nvalue\\n...' blocks."""
    fields = {}
    for block in raw.split('\n\n'):
        lines = block.strip('\n').split('\n')
        if lines and lines[0].startswith('%') and lines[0].endswith('%'):
            fields[lines[0].strip('%')] = lines[1:]
    return fields


ARCHES = ('aarch64', 'x86_64')


def load_packages(repo, arch=None):
    """Read decrypt-offline.db.tar.gz: one entry per package name (repo-add
    already dedupes to the newest version), so this is exactly the set
    pacstrap resolves from this repo today."""
    db = Path(repo) / 'decrypt-offline.db.tar.gz'
    packages = []
    with tarfile.open(db, 'r:gz') as archive:
        for member in archive.getmembers():
            if not member.name.endswith('/desc'):
                continue
            fields = parse_desc(archive.extractfile(member).read().decode())
            # Every package must be built for the lock's architecture or be
            # architecture-independent ("any"); a mixed-up cache fails here.
            if arch is not None and fields.get('ARCH', [arch])[0] not in (arch, 'any'):
                raise SystemExit(f"{fields['NAME'][0]} is built for {fields['ARCH'][0]}, not {arch}")
            packages.append({
                'name': fields['NAME'][0],
                'version': fields['VERSION'][0],
                'filename': fields['FILENAME'][0],
                'sha256': fields['SHA256SUM'][0],
                'bytes': int(fields['CSIZE'][0]),
            })
    packages.sort(key=lambda row: row['name'])
    return packages


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--arch', choices=ARCHES, default='aarch64', help='image architecture (default aarch64)')
    p.add_argument('--repo', required=True, type=Path, help='offline repo dir, e.g. .../cache/pkg/aarch64')
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    packages = load_packages(args.repo, args.arch)
    args.output.write_text(json.dumps({
        'schema': 'decrypt.arch-package-lock.v1',
        'repo': 'decrypt-offline',
        'arch': args.arch,
        'packages': packages,
    }, indent=2, sort_keys=True) + '\n')
    print(f'wrote {len(packages)} packages to {args.output}')


if __name__ == '__main__':
    main()
