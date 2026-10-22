#!/usr/bin/env python3
"""Check an extracted release against its MANIFEST.json. Exit 0 only on an exact match.

Usage: python3 scripts/verify_release.py EXTRACTED_DIR
"""
import hashlib
import json
import sys
from pathlib import Path


def check(root):
    """Return (manifest, problems) for an extracted release directory. Raises if MANIFEST.json is unreadable."""
    root = Path(root).resolve()
    manifest = json.loads((root / 'MANIFEST.json').read_text())
    expected = manifest['files']
    present = {str(p.relative_to(root)) for p in root.rglob('*')
               if p.is_file() and p.name != 'MANIFEST.json' and '__pycache__' not in p.parts}
    problems = [f'missing {n}' for n in sorted(set(expected) - present)]
    problems += [f'unexpected {n}' for n in sorted(present - set(expected))]
    for name in sorted(set(expected) & present):
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected[name]:
            problems.append(f'changed {name}')
    return manifest, problems


def main():
    manifest, problems = check(sys.argv[1])
    print(json.dumps({'version': manifest['version'], 'files': len(manifest['files']), 'ok': not problems,
                      'problems': problems}))
    sys.exit(0 if not problems else 1)


if __name__ == '__main__':
    main()
