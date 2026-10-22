#!/usr/bin/env python3
"""Hourly /tmp housekeeping for this desktop account only.

Only ever touches top-level /tmp entries this account owns; never recurses,
never follows symlinks, and never touches another account's files (/tmp is
shared and sticky-bit protected, so other entries cannot be renamed anyway).
Known disposable caches are deleted outright; anything else old enough is
archived under ~/.cache/decrypt-tmp-archive instead of being silently lost.
"""
import argparse
import os
from pathlib import Path
import shutil
import stat
import sys
import time

THRESHOLD_SECONDS = 24 * 60 * 60
# Name prefixes this OS and its dependencies are known to leave behind in
# /tmp and that are always safe to delete outright once old enough.
DISPOSABLE_PREFIXES = ('chromium-', '.org.chromium.', 'npm-', 'pip-', 'pytest-of-', 'tmp')
TMP = Path('/tmp')
ARCHIVE_RELATIVE = '.cache/decrypt-tmp-archive'


def is_disposable(name):
    return any(name.startswith(prefix) for prefix in DISPOSABLE_PREFIXES)


def plan(entries, now, threshold_seconds=THRESHOLD_SECONDS):
    """Pure decision logic. `entries` is a list of dicts with the fields below,
    so this is testable without touching a real filesystem."""
    actions = []
    for entry in entries:
        if entry['is_symlink'] or entry['uid'] != entry['own_uid']:
            continue
        age = now - entry['mtime']
        if age < threshold_seconds:
            continue
        actions.append((entry['name'], 'delete' if is_disposable(entry['name']) else 'archive'))
    return actions


def collect(tmp_dir, own_uid):
    entries = []
    try:
        names = os.listdir(tmp_dir)
    except OSError:
        return entries
    for name in names:
        path = Path(tmp_dir) / name
        try:
            info = path.lstat()
        except OSError:
            continue
        entries.append({'name': name, 'is_symlink': stat.S_ISLNK(info.st_mode),
                         'uid': info.st_uid, 'own_uid': own_uid, 'mtime': info.st_mtime})
    return entries


def apply(tmp_dir=TMP, home=None, now=None, threshold_seconds=THRESHOLD_SECONDS):
    home = Path(home) if home is not None else Path.home()
    now = time.time() if now is None else now
    own_uid = os.getuid()
    entries = collect(tmp_dir, own_uid)
    actions = plan(entries, now, threshold_seconds)
    archive_dir = home / ARCHIVE_RELATIVE
    deleted, archived, failed = [], [], []
    for name, action in actions:
        path = Path(tmp_dir) / name
        try:
            if action == 'delete':
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                deleted.append(name)
            else:
                archive_dir.mkdir(parents=True, exist_ok=True)
                destination = archive_dir / name
                if destination.exists() or destination.is_symlink():
                    destination = archive_dir / (name + '.' + str(int(now)))
                shutil.move(str(path), str(destination))
                archived.append(name)
        except OSError as error:
            failed.append({'name': name, 'error': str(error)})
    return {'deleted': deleted, 'archived': archived, 'failed': failed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', required=True)
    parser.parse_args(argv)
    result = apply()
    if result['failed']:
        print('decrypt-tmp-janitor: ' + str(len(result['failed'])) + ' item(s) could not be cleaned', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
