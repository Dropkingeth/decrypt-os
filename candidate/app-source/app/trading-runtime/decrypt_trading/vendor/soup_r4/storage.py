"""Strict local JSON and single-writer persistence. No network operations."""
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def strict_json(raw, max_bytes=65536):
    if not isinstance(raw,(str,bytes,bytearray)):
        raise ValueError('JSON text required')
    if len(raw.encode('utf-8') if isinstance(raw,str) else raw) > max_bytes:
        raise ValueError('JSON exceeds byte limit')
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate JSON key')
            out[key] = value
        return out
    def invalid(value):
        raise ValueError('nonfinite JSON constant')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except RecursionError as exc:
        raise ValueError('JSON complexity limit') from exc
    if not isinstance(value, dict):
        raise ValueError('JSON root must be an object')
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 20 or count > max(10000, max_bytes):
            raise ValueError('JSON complexity limit')
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError('nonfinite JSON number')
    return value


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('input must be a single-link regular file')
        if info.st_size > 65536:
            raise ValueError('input exceeds 64 KiB')
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            return strict_json(handle.read(65537))
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    encoded = (canonical(value) + '\n').encode()
    fd, name = tempfile.mkstemp(prefix='.soup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class WriterLock:
    """Never unlink the lock. Abort if the bound workspace or inode changes."""
    def __init__(self, workspace):
        self.path = Path(workspace)
        self.path.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('workspace symlink')
        self.identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        self.lock_path = self.path / '.writer.lock'
        self.fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ValueError('invalid lock topology')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.validate()
        except BaseException:
            os.close(self.fd)
            raise

    def validate(self):
        if self.path.is_symlink():
            raise ValueError('workspace replaced')
        try:
            info = self.path.stat()
            disk = self.lock_path.lstat()
        except FileNotFoundError as exc:
            raise ValueError('workspace/lock removed or replaced') from exc
        held = os.fstat(self.fd)
        if (info.st_dev, info.st_ino) != self.identity or (disk.st_dev, disk.st_ino) != (held.st_dev, held.st_ino):
            raise ValueError('workspace/lock identity changed')

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
