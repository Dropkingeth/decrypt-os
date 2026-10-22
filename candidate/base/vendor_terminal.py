#!/usr/bin/env python3
"""Install the pinned upstream terminal resolver in the new guest only."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from guard import Refusal, check_installed, require

VENDOR = Path(__file__).resolve().parent / 'vendor/xdg-terminal-exec'
EXPECTED = {
    'xdg-terminal-exec': '89f2be9a65deb8df93633a9a09b4953a921c83d591ab987f7569280ecd3127ac',
    'LICENSE': '589ed823e9a84c56feb95ac58e7cf384626b9cbf4fda2a907bc36e103de1bad2',
    'README.md': 'c7a1a8bb711e44e4a19ca0a3c943f13a8e53d5fbb6c752e08cb186d5995a9d40',
}


def validate_sources(directory=VENDOR):
    for name, expected in EXPECTED.items():
        source = Path(directory) / name
        require(source.is_file() and not source.is_symlink(), 'Missing or linked resolver source: ' + name)
        require(hashlib.sha256(source.read_bytes()).hexdigest() == expected, 'Resolver source hash mismatch: ' + name)
    provenance = json.loads((Path(directory) / 'source.json').read_text())
    require(provenance.get('commit') == '065925df9f419008159258ae169018bfd23df71b', 'Resolver source commit mismatch.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-new-test-resolver', required=True, action='store_true')
    parser.parse_args()
    check_installed()
    validate_sources()
    binary = Path('/usr/local/bin/xdg-terminal-exec')
    source_dir = Path('/usr/local/share/xdg-terminal-exec/source')
    license_dir = Path('/usr/local/share/licenses/xdg-terminal-exec')
    for destination in (binary, source_dir, license_dir):
        require(not destination.exists() and not destination.is_symlink(),
                'Existing terminal resolver installation requires review: ' + str(destination))
    binary.parent.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True)
    license_dir.mkdir(parents=True)
    for name in (*EXPECTED, 'source.json'):
        shutil.copyfile(VENDOR / name, source_dir / name)
        (source_dir / name).chmod(0o644)
    shutil.copyfile(VENDOR / 'xdg-terminal-exec', binary)
    binary.chmod(0o755)
    shutil.copyfile(VENDOR / 'LICENSE', license_dir / 'LICENSE')
    (license_dir / 'LICENSE').chmod(0o644)
    print('Pinned upstream xdg-terminal-exec 0.14.3 installed with source and GPL-3.0-or-later license.')


if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, ValueError) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
