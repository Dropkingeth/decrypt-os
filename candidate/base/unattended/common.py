"""Small fail-closed file primitives for the root-only image factory."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import target_profile as profiles

RATE = 20 * 1024 * 1024
HEX = re.compile(r'[0-9a-f]{64}\Z')


class Refused(RuntimeError):
    pass


def require(ok, code):
    if not ok:
        raise Refused(code)


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def controlled(path, directory=False, private=False):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'unsafe-path')
    for parent in path.parents:
        st = parent.lstat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022,
                'uncontrolled-parent')
    st = path.lstat()
    require(st.st_uid == 0 and not st.st_mode & 0o022, 'uncontrolled-input')
    require(stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode) and st.st_nlink == 1,
            'unexpected-file-type')
    require(not private or stat.S_IMODE(st.st_mode) == 0o600, 'private-input-mode')
    return st


def read(path, expected=None, maximum=16 * 1024 * 1024, private=False):
    controlled(path, private=private)
    raw = profiles.read(Path(path), maximum)
    require(expected is None or HEX.fullmatch(expected) and digest(raw) == expected, 'input-hash')
    return raw


def load(path, expected=None, private=False):
    return profiles.strict_json(read(path, expected, private=private))


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(path, value, *, exclusive=True, mode=0o600):
    path = Path(path)
    raw = value if isinstance(value, bytes) else (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), mode); out.write(raw); out.flush(); os.fsync(out.fileno())
    else:
        require(not path.is_symlink(), 'linked-output')
        fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                os.fchmod(out.fileno(), mode); out.write(raw); out.flush(); os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    fsync_dir(path.parent)


def hash_file(path):
    st = controlled(path)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink, s.st_uid, s.st_mode)
    h = hashlib.sha256(); total = 0; started = time.monotonic()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        require(identity(os.fstat(source.fileno())) == identity(st), 'input-identity')
        while block := source.read(1024 * 1024):
            h.update(block); total += len(block)
            time.sleep(max(0, total / RATE - (time.monotonic() - started)))
        require(identity(os.fstat(source.fileno())) == identity(st)
                and identity(Path(path).lstat()) == identity(st), 'input-changed')
    return h.hexdigest()


def child(root, relative):
    profiles.relative_path(relative)
    return Path(root) / relative


def validate_plan(plan, profile, profile_sha, allocation, allocation_sha, binding):
    fields = set('schema buildRunId imageId targetProfileSha256 mediaSha256 manifestSha256 packageLockSha256 allocationReceiptSha256 firstBootPolicySha256 appRecipe networkPolicySha256'.split())
    require(type(plan) is dict and set(plan) == fields and plan['schema'] == 'decrypt.factory-plan.v1', 'factory-plan-schema')
    profiles.validate(profile)
    require(profile['kind'] == 'image-build' and profile['password'] == 'first-boot'
            and profile['notice'] == 'interactive' and profile['network']['phaseB'] == 'offline', 'factory-profile')
    require(profiles.uuid_text(plan['buildRunId']) and type(plan['imageId']) is str
            and re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,95}', plan['imageId']), 'factory-identity')
    for key in fields - {'schema', 'buildRunId', 'imageId', 'appRecipe'}:
        require(type(plan[key]) is str and HEX.fullmatch(plan[key]), 'factory-digest')
    recipe = plan['appRecipe']
    require(type(recipe) is dict and set(recipe) == {'path', 'sha256', 'sourceManifestSha256'}, 'factory-recipe')
    profiles.relative_path(recipe['path'])
    require(all(HEX.fullmatch(recipe[x]) for x in ('sha256', 'sourceManifestSha256')), 'recipe-digest')
    require(plan['targetProfileSha256'] == profile_sha and plan['allocationReceiptSha256'] == allocation_sha,
            'factory-input-binding')
    profiles.device_for(profile, allocation)
    require(allocation['runId'] == binding['runId'] == plan['buildRunId'], 'factory-run-binding')
    profiles.validate_run(binding, profile, profile_sha, plan['mediaSha256'], binding['logUuid'])
    return plan
