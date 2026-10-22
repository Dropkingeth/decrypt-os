#!/usr/bin/env python3
"""Write MANIFEST.json for a release directory from its SHA256SUMS.

Same manifest as build/build.sh writes (schema decrypt.release-manifest.v1),
with the image architecture as a parameter. build/build-x86_64.sh uses it on
the x86_64 builder; the result stays unsigned there and is signed later on
the release host with build/sign_manifest.sh.

    write_manifest.py OUT_DIR IMAGE_ID VERSION SOURCE_TREE SOURCE_DATE_EPOCH ARCH
"""
import json
import os
import sys
import time

ARCHES = ('aarch64', 'x86_64')


def manifest(out, image_id, version, tree, epoch, arch, built_at=None):
    if arch not in ARCHES:
        raise SystemExit(f'unsupported arch {arch!r}')
    artifacts = []
    for line in open(os.path.join(out, 'SHA256SUMS')):
        digest, name = line.rstrip('\n').split('  ', 1)
        artifacts.append({'name': name, 'bytes': os.path.getsize(os.path.join(out, name)), 'sha256': digest})
    return {'schema': 'decrypt.release-manifest.v1', 'imageId': image_id, 'version': version, 'arch': arch,
            'sourceTree': tree, 'sourceDateEpoch': int(epoch),
            'builtAt': built_at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'artifacts': artifacts,
            'signature': {'algorithm': 'Ed25519', 'file': 'MANIFEST.json.sig', 'signs': 'exact bytes of MANIFEST.json'}}


def main(argv):
    if len(argv) != 6:
        raise SystemExit(__doc__)
    out = argv[0]
    with open(os.path.join(out, 'MANIFEST.json'), 'w') as f:
        json.dump(manifest(*argv), f, indent=2, sort_keys=True)
        f.write('\n')


if __name__ == '__main__':
    main(sys.argv[1:])
