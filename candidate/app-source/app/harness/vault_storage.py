#!/usr/bin/env python3
"""Fixed, root-owned encrypted block storage for the customer assistant.

This module does not start services, copy credentials, or collect passphrases.
Callers must stop every customer process before lock(). No CLI accepts paths.
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from dataclasses import dataclass

IMAGE = pathlib.Path("/var/lib/decrypt-os/customer-storage/customer.luks")
MAPPER = "decrypt-os-customer-vault"
MOUNT = pathlib.Path("/run/decrypt-os-customer-vault")
DATA = MOUNT / "data"
SIZE = 1024 ** 3
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}

class StorageError(RuntimeError):
    """Messages never include subprocess output, passphrases or credential data."""

def _storage_errors(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except StorageError:
            raise
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise StorageError("Encrypted storage could not complete safely. Existing data was preserved.") from None
    return guarded

@dataclass(frozen=True)
class Paths:
    image: pathlib.Path = IMAGE
    mapper: str = MAPPER
    mount: pathlib.Path = MOUNT

    @property
    def device(self):
        return pathlib.Path("/dev/mapper") / self.mapper

    @property
    def metadata(self):
        return self.image.parent / "customer-storage.json"

    @property
    def stage(self):
        return self.image.parent / "customer.luks.creating"

class Storage:
    def __init__(self, paths=None):
        self.paths = paths or Paths()

    def _root(self):
        if os.geteuid() != 0 or sys.platform != "linux":
            raise StorageError("Encrypted storage requires the fixed root helper on Linux.")
        p = self.paths
        if not p.image.is_absolute() or not p.mount.is_absolute():
            raise StorageError("Storage paths are invalid.")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", p.mapper):
            raise StorageError("Storage identity is invalid.")

    def _run(self, args, *, secret=None, timeout=45):
        try:
            result = subprocess.run(args, input=secret, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=SAFE_ENV, check=False,
                                    timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise StorageError("An encrypted storage operation could not complete.") from None
        return result

    def _ok(self, args, **kwargs):
        result = self._run(args, **kwargs)
        if result.returncode:
            raise StorageError("An encrypted storage operation failed. Existing data was preserved.")
        return result.stdout

    @staticmethod
    def _exists(path):
        return os.path.lexists(path)

    def _secure_dirs(self, path, *, create=False, leaf_mode=0o700):
        """Check every ancestor with lstat; never traverse an untrusted parent."""
        parts = list(reversed(path.parents)) + [path]
        for current in parts:
            if not self._exists(current):
                if not create:
                    raise StorageError("The trusted storage directory is missing.")
                try:
                    current.mkdir(mode=leaf_mode)
                    self._sync_dir(current.parent)
                except OSError:
                    raise StorageError("The trusted storage directory could not be created.") from None
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                    stat.S_IMODE(info.st_mode) & 0o022):
                raise StorageError("Storage requires root-owned directories without writable or symbolic-link parents.")
        if stat.S_IMODE(path.stat().st_mode) != leaf_mode:
            # Existing system parents may be0755, but our dedicated leaf must be0700.
            raise StorageError("The private storage directory must have mode0700.")

    def _file_info(self, path, *, expected_size=None):
        try:
            info = path.lstat()
        except OSError:
            raise StorageError("The encrypted storage file is unavailable.") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise StorageError("Encrypted storage requires a root-owned single-link mode0600 regular file.")
        if expected_size is not None and info.st_size != expected_size:
            raise StorageError("The encrypted storage file has an unexpected size.")
        return info

    @staticmethod
    def _sync_dir(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _sync_image(self):
        fd = os.open(self.paths.image, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._sync_dir(self.paths.image.parent)

    @contextlib.contextmanager
    def _operation(self):
        path = self.paths.image.parent / "operation.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                    info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise StorageError("The storage operation lock is not trusted.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StorageError("Another encrypted storage operation is in progress.") from None
            yield
        finally:
            os.close(fd)

    def _uuid(self, image=None):
        raw = self._ok(["/usr/bin/cryptsetup", "luksUUID", str(image or self.paths.image)])
        value = raw.decode("ascii", errors="replace").strip()
        if not UUID_RE.fullmatch(value):
            raise StorageError("The encrypted storage header is not recognized.")
        return value.lower()

    def _load_metadata(self):
        self._file_info(self.paths.metadata)
        fd = os.open(self.paths.metadata, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        try:
            value = json.loads(raw)
            if (len(raw) > 4096 or not isinstance(value, dict) or value.get("version") != 1 or
                    not UUID_RE.fullmatch(value.get("luksUuid", "")) or
                    not UUID_RE.fullmatch(value.get("filesystemUuid", ""))):
                raise ValueError
            info = self._file_info(self.paths.image, expected_size=SIZE)
            if value.get("imageDevice") != info.st_dev or value.get("imageInode") != info.st_ino:
                raise ValueError
            if self._uuid() != value["luksUuid"].lower():
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise StorageError("Encrypted storage metadata does not match the existing image.") from None
        return value

    def _mapping(self):
        result = self._run(["/usr/bin/cryptsetup", "status", self.paths.mapper])
        if result.returncode:
            if result.returncode == 4 and not self._exists(self.paths.device):
                return None
            raise StorageError("The encrypted device mapping could not be verified.")
        fields = {}
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            if ":" in line:
                key, value = line.strip().split(":", 1)
                fields[key.strip()] = value.strip()
        loop = fields.get("device", "")
        if fields.get("type") != "LUKS2" or not re.fullmatch(r"/dev/loop[0-9]+", loop):
            raise StorageError("The occupied mapping is not the expected encrypted image.")
        raw = self._ok(["/usr/bin/losetup", "--json", "--list",
                        "--output", "NAME,BACK-FILE,BACK-INO,BACK-MAJ:MIN", loop])
        try:
            devices = json.loads(raw)["loopdevices"]
            if len(devices) != 1:
                raise ValueError
            item = devices[0]
            info = self._file_info(self.paths.image, expected_size=SIZE)
            if (item.get("name") != loop or item.get("back-file") != str(self.paths.image) or
                    int(item.get("back-ino", -1)) != info.st_ino or
                    item.get("back-maj:min") != f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"):
                raise ValueError
            device_info = self.paths.device.stat()
            if not stat.S_ISBLK(device_info.st_mode):
                raise ValueError
        except (ValueError, TypeError, KeyError, OSError):
            raise StorageError("The occupied mapping does not match the fixed encrypted image.") from None
        return {"loop": loop, "majorMinor": f"{os.major(device_info.st_rdev)}:{os.minor(device_info.st_rdev)}"}

    def _mounted(self, mapping, metadata=None):
        result = self._run(["/usr/bin/findmnt", "--json", "--mountpoint", str(self.paths.mount),
                            "--output", "SOURCE,TARGET,FSTYPE,OPTIONS,MAJ:MIN,UUID"])
        if result.returncode == 1:
            return False
        if result.returncode:
            raise StorageError("The storage mount could not be inspected.")
        try:
            entries = json.loads(result.stdout)["filesystems"]
            if len(entries) != 1 or not mapping:
                raise ValueError
            entry = entries[0]
            options = set(entry["options"].split(","))
            if (entry["target"] != str(self.paths.mount) or entry["fstype"] != "ext4" or
                    entry["maj:min"] != mapping["majorMinor"] or
                    not {"rw", "nodev", "nosuid", "noexec"}.issubset(options)):
                raise ValueError
            if metadata is not None and entry.get("uuid", "").lower() != metadata["filesystemUuid"].lower():
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise StorageError("The occupied mount is not the expected encrypted filesystem.") from None
        return True

    def _filesystem_uuid(self):
        raw = self._ok(["/usr/bin/blkid", "-p", "-o", "export", str(self.paths.device)])
        fields = {}
        for line in raw.decode("ascii", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        value = fields.get("UUID", "")
        if fields.get("TYPE") != "ext4" or not UUID_RE.fullmatch(value):
            raise StorageError("The encrypted filesystem is not the expected ext4 volume.")
        return value.lower()

    def _write_metadata(self, value):
        temp = self.paths.metadata.with_suffix(".json.creating")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps(value, sort_keys=True).encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            if self._exists(self.paths.metadata):
                raise StorageError("Storage metadata already exists.")
            os.rename(temp, self.paths.metadata)
            self._sync_dir(self.paths.image.parent)
        except BaseException:
            # Keep incomplete creation evidence; never erase an existing encrypted image.
            raise

    def _passphrase(self, value):
        if not isinstance(value, bytes):
            raise StorageError("The local passphrase must be supplied privately.")
        try:
            valid = len(value.decode("utf-8")) >= 12
        except UnicodeError:
            valid = False
        if not valid or len(value) > 4096 or b"\0" in value:
            raise StorageError("Use a passphrase of at least12 characters and at most4096 UTF-8 bytes.")

    def _mount(self, mapping, metadata):
        self._secure_dirs(self.paths.mount, create=True)
        if self._mounted(mapping, metadata):
            return
        if any(self.paths.mount.iterdir()):
            raise StorageError("The storage mountpoint is not empty.")
        if self._filesystem_uuid() != metadata["filesystemUuid"].lower():
            raise StorageError("The existing encrypted filesystem identity does not match.")
        self._ok(["/usr/bin/mount", "-t", "ext4", "-o", "nodev,nosuid,noexec",
                  str(self.paths.device), str(self.paths.mount)])
        if not self._mounted(mapping, metadata):
            raise StorageError("Encrypted storage mounted but verification did not complete.")
        # A newly formatted ext4 root defaults to0755. Tighten only after exact source verification.
        os.chmod(self.paths.mount, 0o700)
        self._sync_dir(self.paths.mount)

    def status(self):
        configured = False
        try:
            self._root()
            configured = self._exists(self.paths.image)
            if not self._exists(self.paths.image.parent):
                # A foreign mapping or mount must not be called unconfigured/locked.
                mapping = self._mapping()
                if mapping or self._mounted(None):
                    raise StorageError("Unexpected storage mapping exists.")
                return {"configured": False, "unlocked": False, "error": None}
            self._secure_dirs(self.paths.image.parent)
            if self._exists(self.paths.stage):
                raise StorageError("An interrupted storage creation needs local recovery; no data was overwritten.")
            if not configured:
                if self._exists(self.paths.metadata):
                    raise StorageError("Storage metadata exists without its encrypted image.")
                mapping = self._mapping()
                if mapping or self._mounted(None):
                    raise StorageError("Unexpected storage mapping exists.")
                return {"configured": False, "unlocked": False, "error": None}
            metadata = self._load_metadata()
            mapping = self._mapping()
            if mapping is None:
                if self._mounted(None):
                    raise StorageError("Unexpected storage mount exists.")
                return {"configured": True, "unlocked": False, "error": None}
            self._secure_dirs(self.paths.mount)
            mounted = self._mounted(mapping, metadata)
            return {"configured": True, "unlocked": True,
                    "error": None if mounted else "The encrypted volume is open but not mounted."}
        except (StorageError, OSError, ValueError) as exc:
            # Unknown identity is not safe to call locked. Report an explicit error.
            return {"configured": configured, "unlocked": self._exists(self.paths.device),
                    "error": str(exc) if isinstance(exc, StorageError) else "Encrypted storage inspection failed."}

    @_storage_errors
    def create(self, passphrase):
        self._root()
        self._passphrase(passphrase)
        self._secure_dirs(self.paths.image.parent, create=True)
        with self._operation():
            if any(self._exists(p) for p in (self.paths.image, self.paths.stage, self.paths.metadata,
                                             self.paths.metadata.with_suffix(".json.creating"))):
                raise StorageError("Encrypted storage already exists or creation was interrupted; it was not overwritten.")
            if self._mapping() is not None or self._mounted(None):
                raise StorageError("The dedicated storage mapping or mount is already occupied.")
            self._secure_dirs(self.paths.mount, create=True)
            if any(self.paths.mount.iterdir()):
                raise StorageError("The storage mountpoint is not empty.")
            free = os.statvfs(self.paths.image.parent)
            if free.f_bavail * free.f_frsize < SIZE + 256 * 1024 ** 2:
                raise StorageError("At least1.25 GiB of free space is required for encrypted storage.")
            fd = os.open(self.paths.stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.ftruncate(fd, SIZE)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._sync_dir(self.paths.image.parent)
            self._ok(["/usr/bin/cryptsetup", "luksFormat", "--type", "luks2", "--pbkdf", "argon2id",
                      "--pbkdf-memory", "262144", "--pbkdf-parallel", "2", "--iter-time", "2000",
                      "--batch-mode", "--key-file", "-", str(self.paths.stage)],
                     secret=passphrase, timeout=180)
            luks_uuid = self._uuid(self.paths.stage)
            stage_fd = os.open(self.paths.stage, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            os.rename(self.paths.stage, self.paths.image)
            self._sync_dir(self.paths.image.parent)
            self._ok(["/usr/bin/cryptsetup", "open", "--type", "luks2", "--key-file", "-",
                      str(self.paths.image), self.paths.mapper], secret=passphrase, timeout=120)
            mapping = self._mapping()
            if mapping is None:
                raise StorageError("The new encrypted mapping did not become available.")
            # Formatting is only reachable for this newly exclusively created image.
            self._ok(["/usr/bin/mkfs.ext4", "-q", str(self.paths.device)], timeout=120)
            fs_uuid = self._filesystem_uuid()
            info = self._file_info(self.paths.image, expected_size=SIZE)
            metadata = {"version": 1, "luksUuid": luks_uuid, "filesystemUuid": fs_uuid,
                        "imageDevice": info.st_dev, "imageInode": info.st_ino}
            self._write_metadata(metadata)
            self._sync_image()
            self._mount(mapping, metadata)
        return self.status()

    @_storage_errors
    def unlock(self, passphrase):
        self._root()
        self._passphrase(passphrase)
        self._secure_dirs(self.paths.image.parent)
        with self._operation():
            if self._exists(self.paths.stage):
                raise StorageError("Interrupted creation requires local recovery.")
            metadata = self._load_metadata()
            mapping = self._mapping()
            if mapping is None:
                if self._mounted(None):
                    raise StorageError("The dedicated mountpoint is occupied.")
                self._ok(["/usr/bin/cryptsetup", "open", "--type", "luks2", "--key-file", "-",
                          str(self.paths.image), self.paths.mapper], secret=passphrase, timeout=120)
                mapping = self._mapping()
            if mapping is None:
                raise StorageError("The encrypted mapping did not become available.")
            self._mount(mapping, metadata)
        return self.status()

    def _loop_detached(self, mapping):
        raw = self._ok(["/usr/bin/losetup", "--json", "--list", "--associated", str(self.paths.image),
                        "--output", "NAME,BACK-FILE,BACK-INO"])
        try:
            devices = json.loads(raw)["loopdevices"]
            if not isinstance(devices, list):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise StorageError("Loop-device cleanup could not be verified.") from None
        if devices:
            # cryptsetup creates an autoclear loop. Do not detach unknown or still-referenced devices.
            raise StorageError("The encrypted mapping closed but loop-device cleanup is not confirmed.")

    @_storage_errors
    def lock(self):
        self._root()
        self._secure_dirs(self.paths.image.parent)
        with self._operation():
            metadata = self._load_metadata()
            mapping = self._mapping()
            if mapping is None:
                if self._mounted(None):
                    raise StorageError("Unexpected mount prevents verified locking.")
                return {"configured": True, "unlocked": False, "error": None}
            mounted = self._mounted(mapping, metadata)
            if mounted:
                self._ok(["/usr/bin/sync", "-f", str(self.paths.mount)])
                self._ok(["/usr/bin/umount", str(self.paths.mount)])
                if self._mounted(mapping, metadata):
                    raise StorageError("The encrypted filesystem is still mounted; storage is unlocked.")
            # Recheck backing identity immediately before closing, never use deferred close.
            current = self._mapping()
            if current != mapping:
                raise StorageError("The encrypted mapping changed during lock.")
            self._ok(["/usr/bin/cryptsetup", "close", self.paths.mapper])
            if self._mapping() is not None:
                raise StorageError("The encrypted mapping is still open.")
            self._loop_detached(mapping)
            self._sync_image()
        return self.status()

def status():
    return Storage().status()

def create(passphrase):
    return Storage().create(passphrase)

def unlock(passphrase):
    return Storage().unlock(passphrase)

def lock():
    return Storage().lock()
