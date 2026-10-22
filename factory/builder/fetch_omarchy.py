#!/usr/bin/env python3
"""Acquire pinned GitHub source without executing Git or repository code.

Trust: GitHub HTTPS binds the full commit to its root tree. Git object hashes
validate all subtrees and blobs. This is not commit-signature verification.
Failed run directories are retained; retries require a new directory.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
import urllib.request

MAX_TREE = 8 * 1024**2
MAX_BLOB = 64 * 1024**2
MAX_TOTAL = 512 * 1024**2
MAX_DOWNLOAD = 512 * 1024**2
MAX_ENTRIES = 20000
RATE = 5 * 1024**2


def oid(kind, raw):
    return hashlib.sha1(kind.encode() + b' ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def full_sha(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{40}', value):
        raise ValueError('full lowercase Git object ID required')
    return value


def safe_path(name):
    if not isinstance(name, str) or not name or len(name.encode()) > 1024:
        raise ValueError('invalid source path')
    if any(ord(c) < 32 for c in name) or '\\' in name:
        raise ValueError('control character or backslash in source path')
    if any(c in ('', '.', '..', '.git') for c in name.split('/')) or name.startswith('/'):
        raise ValueError('unsafe source path')
    return name


def regular_bytes(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise ValueError('input is not a bounded regular file')
        raw = f.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('input exceeds limit')
        return raw


def validate_tree(doc, root):
    full_sha(root)
    if doc.get('truncated') is not False or doc.get('sha') != root:
        raise ValueError('truncated or mismatched root tree')
    rows = doc.get('tree')
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_ENTRIES:
        raise ValueError('tree entry count outside bound')
    entries = {}
    total = 0
    for row in rows:
        name = safe_path(row['path']); full_sha(row['sha'])
        if name in entries:
            raise ValueError('duplicate tree path')
        mode = row['mode']
        if (mode, row['type']) not in (('040000', 'tree'), ('100644', 'blob'), ('100755', 'blob'), ('120000', 'blob')):
            raise ValueError('unsupported Git mode/type; submodules forbidden')
        if row['type'] == 'blob':
            size = row.get('size')
            if type(size) is not int or not 0 <= size <= MAX_BLOB:
                raise ValueError('blob size outside bound')
            total += size
        entries[name] = row
    if total > MAX_TOTAL:
        raise ValueError('source size outside bound')
    children = {'': []}
    for name, row in entries.items():
        if row['type'] == 'tree': children[name] = []
    for name, row in entries.items():
        parent = name.rpartition('/')[0]
        if parent not in children:
            raise ValueError('missing directory or child beneath blob')
        children[parent].append(row)
    for name, rows in children.items():
        ordered = sorted(rows, key=lambda r: r['path'].rsplit('/', 1)[-1].encode() + (b'/' if r['type'] == 'tree' else b''))
        body = b''.join((r['mode'].lstrip('0') + ' ').encode() + r['path'].rsplit('/', 1)[-1].encode() + b'\0' + bytes.fromhex(r['sha']) for r in ordered)
        if oid('tree', body) != (root if not name else entries[name]['sha']):
            raise ValueError('Git tree hash mismatch')
    return entries


class Guard:
    def __init__(self, pause_marker=None, rate=RATE, deadline_seconds=900):
        if not 1 <= rate <= 20 * 1024**2 or not 1 <= deadline_seconds <= 3600:
            raise ValueError('rate or deadline outside bounds')
        self.pause_marker = pause_marker
        self.rate = rate
        self.deadline = time.monotonic() + deadline_seconds
        self.started = time.monotonic()
        self.bytes = 0

    def check(self):
        if self.pause_marker and os.path.lexists(self.pause_marker):
            raise ValueError('pause marker present')
        if time.monotonic() >= self.deadline:
            raise ValueError('operation deadline exceeded')

    def pace(self, count):
        self.check()
        self.bytes += count
        delay = self.bytes / self.rate - (time.monotonic() - self.started)
        while delay > 0:
            time.sleep(min(delay, 0.2)); self.check()
            delay = self.bytes / self.rate - (time.monotonic() - self.started)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect forbidden')


def download(url, destination, maximum, guard):
    # No inherited proxies, credentials, redirects, cookies or local transports.
    if not re.fullmatch(r'https://(?:api\.github\.com|codeload\.github\.com)/[A-Za-z0-9_./?=\-]+', url):
        raise ValueError('non-allowlisted source URL')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'decrypt-source-builder/1', 'Accept-Encoding': 'identity'})
    guard.check()
    with destination.open('xb') as out, opener.open(request, timeout=20) as response:
        if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
            raise ValueError('unexpected HTTP response')
        total = 0
        while True:
            guard.check()
            raw = response.read(min(65536, maximum - total + 1))
            if not raw: break
            total += len(raw)
            if total > maximum: raise ValueError('download exceeds bound')
            guard.pace(len(raw)); out.write(raw)
        out.flush(); os.fsync(out.fileno())
    return total


class BoundedReader:
    def __init__(self, stream, limit, guard):
        self.stream, self.remaining, self.guard = stream, limit, guard
    def read(self, size=-1):
        self.guard.check()
        size = min(size if size >= 0 else self.remaining + 1, self.remaining + 1)
        data = self.stream.read(size)
        self.remaining -= len(data)
        if self.remaining < 0: raise ValueError('expanded archive exceeds bound')
        self.guard.pace(len(data))
        return data


class PacedWriter:
    def __init__(self, stream, guard):
        self.stream, self.guard = stream, guard
    def tell(self):
        return self.stream.tell()
    def write(self, data):
        self.guard.pace(len(data))
        return self.stream.write(data)


def validate_links(links):
    # Resolve complete chains using archive paths, never the host filesystem.
    for name, target in links.items():
        pending = list(PurePosixPath(name).parent.parts) + target.split('/')
        resolved = []; expansions = 0
        while pending:
            part = pending.pop(0)
            if part in ('', '.'): continue
            if part == '..':
                if not resolved: raise ValueError('escaping symlink chain')
                resolved.pop(); continue
            resolved.append(part)
            path = '/'.join(resolved)
            if path in links:
                expansions += 1
                if expansions > 40: raise ValueError('cyclic or excessive symlink chain')
                resolved.pop()
                pending = links[path].split('/') + pending


def verify_archive(source, output, entries, prefix, guard):
    seen = set(); count = 0; blobs = 0; links = {}
    # Bound decompression including tar headers and extended headers.
    limit = sum(r['size'] for r in entries.values() if r['type'] == 'blob') + 4096 * (len(entries) + 2)
    with gzip.open(source, 'rb') as compressed, output.open('xb') as sink:
        bounded = BoundedReader(compressed, limit, guard)
        with tarfile.open(fileobj=bounded, mode='r|') as archive, tarfile.open(fileobj=PacedWriter(sink, guard), mode='w', format=tarfile.PAX_FORMAT) as target:
            for item in archive:
                guard.check(); count += 1
                if count > len(entries) + 1: raise ValueError('archive member count exceeds tree')
                if item.isdir() and item.mode not in (0o755, 0o775):
                    raise ValueError('directory mode mismatch')
                if item.issym() and item.mode != 0o777:
                    raise ValueError('symlink mode mismatch')
                name = item.name.rstrip('/') if item.isdir() else item.name
                if name == prefix and item.isdir():
                    if '' in seen: raise ValueError('duplicate archive root')
                    seen.add(''); continue
                if not name.startswith(prefix + '/'):
                    raise ValueError('archive prefix mismatch')
                name = safe_path(name[len(prefix) + 1:])
                if name in seen or name not in entries: raise ValueError('duplicate or unexpected archive path')
                seen.add(name); row = entries[name]
                result = tarfile.TarInfo(name); result.mtime = result.uid = result.gid = 0
                if row['type'] == 'tree':
                    if not item.isdir() or item.size: raise ValueError('directory type mismatch')
                    result.type = tarfile.DIRTYPE; result.mode = 0o755
                    target.addfile(result); continue
                if row['mode'] == '120000':
                    if not item.issym() or item.size: raise ValueError('symlink type mismatch')
                    raw = item.linkname.encode()
                    # Reject absolute/escaping symlinks, even when present upstream.
                    link = PurePosixPath(item.linkname)
                    parts = list(PurePosixPath(name).parent.parts)
                    if link.is_absolute() or '\\' in item.linkname or '\0' in item.linkname: raise ValueError('unsafe symlink')
                    for part in link.parts:
                        if part == '..':
                            if not parts: raise ValueError('escaping symlink')
                            parts.pop()
                        elif part != '.': parts.append(part)
                    result.type = tarfile.SYMTYPE; result.linkname = item.linkname; result.mode = 0o777
                    links[name] = item.linkname
                else:
                    if not item.isfile() or item.size != row['size'] or item.sparse is not None:
                        raise ValueError('blob type/size mismatch')
                    # Git records executable identity, not group write permission.
                    # Codeload observed0664 for Git100644; permit only this
                    # group-write variation (0775 for executable100755).
                    canonical_mode = int(row['mode'][-3:], 8)
                    if item.mode not in (canonical_mode, canonical_mode | 0o020):
                        raise ValueError('blob mode mismatch')
                    raw = archive.extractfile(item).read(MAX_BLOB + 1)
                    result.mode = int(row['mode'][-3:], 8); result.size = len(raw)
                if len(raw) != row['size'] or oid('blob', raw) != row['sha']:
                    raise ValueError('Git blob hash mismatch')
                blobs += 1; target.addfile(result, io.BytesIO(raw) if result.isfile() else None)
            # Read remaining compressed input to EOF under the same expansion cap.
            while bounded.read(65536): pass
        sink.flush(); os.fsync(sink.fileno())
    if seen - {''} != set(entries): raise ValueError('archive missing Git tree entries')
    validate_links(links)
    digest = hashlib.sha256()
    with output.open('rb') as f:
        while raw := f.read(65536): guard.pace(len(raw)); digest.update(raw)
    return {'archiveSha256': digest.hexdigest(), 'archiveBytes': output.stat().st_size, 'verifiedBlobs': blobs}


def copy_pinned(source, destination, expected, maximum, guard):
    """Stream a parent-pinned retained acquisition into a fresh attempt."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256(); total = 0
    with os.fdopen(fd, 'rb') as incoming, destination.open('xb') as outgoing:
        before = os.fstat(incoming.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError('retained input must be bounded single-link regular file')
        while raw := incoming.read(65536):
            total += len(raw)
            if total > maximum: raise ValueError('retained input exceeds bound')
            guard.pace(len(raw)); digest.update(raw); outgoing.write(raw)
        after = os.fstat(incoming.fileno())
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, k) != getattr(after, k) for k in fields) or total != before.st_size:
            raise ValueError('retained input changed while copying')
        if digest.hexdigest() != expected: raise ValueError('retained input digest mismatch')
        outgoing.flush(); os.fsync(outgoing.fileno())
    return {'sha256': expected, 'bytes': total}


def acquire(repository, commit, destination, tree_json=None, pause_marker=None, rate=RATE, deadline_seconds=900, *, retained_source=None, retained_pins=None):
    full_sha(commit)
    if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', repository) or '..' in repository:
        raise ValueError('public GitHub owner/repository required')
    if retained_source is not None:
        retained_source = Path(retained_source).absolute()
        if retained_source.resolve(strict=True) != retained_source or not retained_source.is_dir() or tree_json:
            raise ValueError('canonical retained acquisition directory required; no tree override')
        if type(retained_pins) is not dict or set(retained_pins) != {'commit.json', 'tree.json', 'upstream.tar.gz'} or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in retained_pins.values()):
            raise ValueError('explicit parent SHA256 pins required for all retained acquisition files')
    elif retained_pins is not None:
        raise ValueError('retained pins require retained source')
    guard = Guard(pause_marker, rate, deadline_seconds); guard.check()
    destination = Path(destination).absolute()
    if retained_source is not None and (destination == retained_source or destination in retained_source.parents or retained_source in destination.parents):
        raise ValueError('retained acquisition and new output must not overlap')
    if destination.parent.resolve(strict=True) != destination.parent:
        raise ValueError('output parent must be canonical and already exist')
    destination.mkdir(mode=0o700)  # Refuse existing run and symlink aliases.
    try:
        base = 'https://api.github.com/repos/' + repository + '/git/'
        retained = {}
        if retained_source is not None:
            for name in ('commit.json', 'tree.json', 'upstream.tar.gz'):
                retained[name] = copy_pinned(retained_source / name, destination / name,
                    retained_pins[name], MAX_DOWNLOAD if name == 'upstream.tar.gz' else MAX_TREE, guard)
        else:
            download(base + 'commits/' + commit, destination / 'commit.json', MAX_TREE, guard)
        binding = json.loads(regular_bytes(destination / 'commit.json', MAX_TREE))
        if binding['sha'] != commit: raise ValueError('commit binding mismatch')
        root = full_sha(binding['tree']['sha'])
        if tree_json:
            raw = regular_bytes(tree_json, MAX_TREE)
            with (destination / 'tree.json').open('xb') as f:
                f.write(raw); f.flush(); os.fsync(f.fileno())
        elif retained_source is None:
            download(base + 'trees/' + root + '?recursive=1', destination / 'tree.json', MAX_TREE, guard)
        raw = regular_bytes(destination / 'tree.json', MAX_TREE)
        entries = validate_tree(json.loads(raw), root)
        if retained_source is None:
            download('https://codeload.github.com/' + repository + '/tar.gz/' + commit,
                     destination / 'upstream.tar.gz', MAX_DOWNLOAD, guard)
        result = verify_archive(destination / 'upstream.tar.gz', destination / 'omarchy.tar', entries,
                                repository.split('/')[1] + '-' + commit, guard)
        result.update(schema='decrypt.omarchy-source.v2', commit=commit, rootTree=root,
                      sourceUrl='https://github.com/' + repository, networkAccessed=retained_source is None,
                      authentication='GitHub HTTPS commit-to-tree binding; recursive Git SHA-1 tree and blob verification; no signature verification',
                      treeJsonSha256=hashlib.sha256(raw).hexdigest(), rateBytesPerSecond=rate,
                      deadlineSeconds=deadline_seconds, status='verified', localGitExecuted=False)
        if retained_source is not None:
            result['retainedAcquisition'] = retained
            result['authentication'] = 'Parent-pinned retained GitHub HTTPS acquisition; recursive Git SHA-1 tree and blob verification; no new network access or signature verification'
        guard.check()
        with (destination / 'receipt.json').open('x') as f:
            json.dump(result, f, indent=2, sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
        directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
        return result
    except BaseException as exc:
        # Avoid recording remote exception text, request URLs or local private paths.
        with (destination / 'failure.json').open('x') as f:
            json.dump({'schema': 'decrypt.omarchy-source-failure.v1', 'status': 'failed', 'errorType': type(exc).__name__, 'partialOutputsRetained': True}, f, indent=2)
        raise


def make_archive(*args, **kwargs):
    raise ValueError('local Git repository mode retired; use authenticated HTTPS acquisition')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--repository', default='omacom/omarchy', help='public GitHub owner/repository, not a local path')
    p.add_argument('--commit', required=True); p.add_argument('--destination', required=True)
    p.add_argument('--retained-source', help='reuse all three retained acquisition files without network')
    p.add_argument('--retained-commit-sha256'); p.add_argument('--retained-tree-sha256'); p.add_argument('--retained-archive-sha256')
    p.add_argument('--tree-json'); p.add_argument('--pause-marker')
    p.add_argument('--rate-bytes-per-second', type=int, default=RATE)
    p.add_argument('--deadline-seconds', type=int, default=900)
    a = p.parse_args()
    values = (a.retained_commit_sha256, a.retained_tree_sha256, a.retained_archive_sha256)
    pins = dict(zip(('commit.json', 'tree.json', 'upstream.tar.gz'), values)) if any(v is not None for v in values) else None
    acquire(a.repository, a.commit, a.destination, a.tree_json, a.pause_marker, a.rate_bytes_per_second, a.deadline_seconds, retained_source=a.retained_source, retained_pins=pins)
