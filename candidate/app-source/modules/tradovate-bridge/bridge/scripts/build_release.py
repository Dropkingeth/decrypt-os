#!/usr/bin/env python3
"""Build a deterministic release archive with a per-file SHA-256 manifest.

Usage: python3 scripts/build_release.py OUT_DIR
Recordings, journals, configs and caches are never included.
"""
import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

VERSION = '1.1.3'
ROOT = Path(__file__).resolve().parent.parent
NAME = 'tvbridge-' + VERSION
INCLUDE = ['README.md', 'config.example.json', 'config.login-b.example.json', 'firing.example.json',
           'firing.multilogin.example.json', '.gitignore', 'page', 'scripts', 'tests', 'tvbridge']
EXCLUDE_PARTS = {'__pycache__', 'out', 'journal'}
EXCLUDE_NAMES = {'config.json', '.DS_Store'}


def files():
    for entry in INCLUDE:
        p = ROOT / entry
        paths = [p] if p.is_file() else sorted(x for x in p.rglob('*') if x.is_file())
        for f in paths:
            rel = f.relative_to(ROOT)
            if EXCLUDE_PARTS & set(rel.parts) or f.name in EXCLUDE_NAMES or f.suffix == '.pyc':
                continue
            yield rel


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rels = sorted(set(files()), key=str)
    manifest = {'name': 'tvbridge', 'version': VERSION, 'files': {}}
    for rel in rels:
        manifest['files'][str(rel)] = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as tar:
        def add(name, data, mode):
            info = tarfile.TarInfo(f'{NAME}/{name}')
            info.size, info.mode, info.mtime = len(data), mode, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            tar.addfile(info, io.BytesIO(data))
        add('MANIFEST.json', manifest_bytes, 0o644)
        for rel in rels:
            src = ROOT / rel
            add(str(rel), src.read_bytes(), 0o755 if src.stat().st_mode & 0o111 else 0o644)
    archive = out / f'{NAME}.tar.gz'
    with open(archive, 'wb') as fh, gzip.GzipFile(fileobj=fh, mode='wb', mtime=0, filename='') as gz:
        gz.write(buf.getvalue())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / f'{NAME}.tar.gz.sha256').write_text(f'{digest}  {NAME}.tar.gz\n')
    print(json.dumps({'archive': str(archive), 'sha256': digest, 'files': len(rels)}))


if __name__ == '__main__':
    main()
