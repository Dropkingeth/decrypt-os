"""Local opt-in Guardian vault. No plaintext storage, provider traffic or logging.

The vault is independent of settings/history. Restart locks it. Clearing a lock
drops all credential references and overwrites the mutable key/buffers; Python
cannot promise forensic erasure of every temporary allocation.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# Tradovate is not a Guardian provider: the bridge's own Chromium window keeps that login
# (saved by Chromium, encrypted by the desktop keyring).
PROVIDERS = {'tradingview': 'TradingView'}
MAX_FILE = 65536


class GuardianError(Exception):
    def __init__(self, code, message='Guardian could not complete this action.', status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)  # Never include supplied fields or provider responses.


def require(data, required=(), optional=()):
    if type(data) is not dict or not set(required) <= data.keys() <= set(required) | set(optional):
        raise GuardianError('invalid_fields')


def bounded_text(value, minimum=1, maximum=256):
    if (type(value) is not str or not minimum <= len(value) <= maximum
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise GuardianError('invalid_input')
    return value


def provider_id(value):
    if type(value) is not str or value not in PROVIDERS:
        raise GuardianError('invalid_provider')
    return value


class PrivateFiles:
    """All file operations relative to an owned, private, no-follow directory."""
    def __init__(self, state_dir):
        path = Path(state_dir).absolute() / 'guardian'
        # Do not resolve away symlinks before validating the ancestry.
        current = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                if part in ('.', '..'):
                    raise GuardianError('unsafe_storage')
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                except FileNotFoundError:
                    if part != path.name:
                        raise GuardianError('unsafe_storage') from None
                    os.mkdir(part, 0o700, dir_fd=current)
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = nxt
                ancestry = os.fstat(current)
                # Shared sticky temporary roots are permitted for isolated tests;
                # deployment state ancestors must otherwise be root/user-owned.
                if ancestry.st_uid not in (0, os.getuid()) or (ancestry.st_mode & 0o022 and not ancestry.st_mode & stat.S_ISVTX):
                    raise GuardianError('unsafe_storage')
            info = os.fstat(current)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise GuardianError('unsafe_storage')
            self.fd, self.path = current, path
            current = None
        except OSError:
            raise GuardianError('unsafe_storage') from None
        finally:
            if current is not None:
                os.close(current)
        self.lock_fd = None
        try:
            fd = os.open('runtime.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
            self.validate(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd = fd
        except (OSError, GuardianError):
            if 'fd' in locals():
                os.close(fd)
            os.close(self.fd)
            raise GuardianError('guardian_busy', status=409) from None

    @staticmethod
    def validate(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_FILE):
            raise GuardianError('unsafe_storage')

    def read(self, name, *, missing=None):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return missing
        except OSError:
            raise GuardianError('unsafe_storage') from None
        try:
            self.validate(fd)
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                raise GuardianError('unsafe_storage')
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise GuardianError('unsafe_storage') from None
        finally:
            os.close(fd)

    def write(self, name, value):
        content = json.dumps(value, separators=(',', ':'), sort_keys=True).encode()
        if len(content) > MAX_FILE:
            raise GuardianError('invalid_input')
        self.read(name)  # Existing symlink, hardlink or unsafe mode is never overwritten.
        temp = '.pending-' + secrets.token_hex(16)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            self.validate(fd)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(fd)
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
            os.close(self.fd)


class GuardianVault:
    def __init__(self, files, clock=time.time):
        self.files, self.clock = files, clock
        self.mutex = threading.RLock()
        self.key = None
        self.entries = {}
        self.salt = None
        self.failed_until = 0
        self.unlock_mode = 'passphrase'

    @staticmethod
    def derive(passphrase, salt):
        # Parameters are compiled in, never supplied by a file/header/client.
        return bytearray(Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode()))

    @staticmethod
    def wipe(value):
        if isinstance(value, bytearray):
            value[:] = b'\0' * len(value)

    def lock(self):
        with self.mutex:
            self.wipe(self.key)
            for entry in self.entries.values():
                self.wipe(entry['username'])
                self.wipe(entry['password'])
            self.key, self.salt, self.entries = None, None, {}

    def metadata(self):
        with self.mutex:
            document = self.files.read('vault.json')
            if document is None:
                return {'configured': False, 'unlocked': False, 'providers': {}}
            self._header(document)
            # Locked headers are display hints, not automatic-login consent.
            # Once unlocked, use only entries authenticated by AES-GCM, never
            # a newly reread header that could have changed since decryption.
            return {'configured': True, 'unlocked': self.key is not None,
                    'providers': ({p: {'saved': True, 'autoLogin': e['autoLogin']}
                                   for p, e in self.entries.items()}
                                  if self.key is not None else document['providers'])}

    def auto_login_enabled(self, provider):
        """Consent from decrypted entries only; never expose a credential."""
        provider_id(provider)
        with self.mutex:
            return self.key is not None and self.entries.get(provider, {}).get('autoLogin') is True

    @staticmethod
    def _header(document):
        require(document, ('schema', 'kdf', 'salt', 'nonce', 'ciphertext', 'providers'), ('unlockMode',))
        if document.get('unlockMode', 'passphrase') not in ('keyring', 'passphrase'):
            raise GuardianError('unsafe_storage')
        if type(document['schema']) is not int or document['schema'] != 1 or document['kdf'] != 'scrypt-32768-8-1':
            raise GuardianError('unsafe_storage')
        if type(document['providers']) is not dict or set(document['providers']) - PROVIDERS.keys():
            raise GuardianError('unsafe_storage')
        for value in document['providers'].values():
            require(value, ('saved', 'autoLogin'))
            if value['saved'] is not True or type(value['autoLogin']) is not bool:
                raise GuardianError('unsafe_storage')

    def initialize(self, passphrase):
        bounded_text(passphrase, 12, 256)
        with self.mutex:
            if self.files.read('vault.json') is not None:
                raise GuardianError('vault_exists', status=409)
            self.salt = secrets.token_bytes(16)
            self.key = self.derive(passphrase, self.salt)
            try:
                self._save()
            except Exception:
                self.lock()
                raise

    def unlock(self, passphrase):
        bounded_text(passphrase, 12, 256)
        with self.mutex:
            if self.clock() < self.failed_until:
                raise GuardianError('unlock_cooldown', status=429)
            document = self.files.read('vault.json')
            if document is None:
                raise GuardianError('vault_missing', status=409)
            self.lock()
            key = None
            try:
                self._header(document)
                salt = base64.b64decode(document['salt'], validate=True)
                nonce = base64.b64decode(document['nonce'], validate=True)
                ciphertext = base64.b64decode(document['ciphertext'], validate=True)
                if len(salt) != 16 or len(nonce) != 12 or not 16 <= len(ciphertext) <= 32768:
                    raise ValueError()
                key = self.derive(passphrase, salt)
                header = {k: v for k, v in document.items() if k not in ('nonce', 'ciphertext')}
                data = json.loads(AESGCM(bytes(key)).decrypt(nonce, ciphertext, self._aad(header)))
                if type(data) is not dict or data.keys() != document['providers'].keys():
                    raise ValueError()
                entries = {}
                for provider, entry in data.items():
                    require(entry, ('username', 'password', 'autoLogin'))
                    if entry['autoLogin'] != document['providers'][provider]['autoLogin']:
                        raise ValueError()
                    entries[provider] = {'username': bytearray(bounded_text(entry['username'], 1, 256).encode()),
                                         'password': bytearray(bounded_text(entry['password'], 1, 1024).encode()),
                                         'autoLogin': entry['autoLogin']}
                self.key, self.salt, self.entries = key, salt, entries
                self.unlock_mode = document.get('unlockMode', 'passphrase')
                self.failed_until = 0
            except Exception:
                self.wipe(key)
                self.lock()
                self.failed_until = self.clock() + 5
                raise GuardianError('unlock_failed', status=403) from None

    @staticmethod
    def _aad(header):
        return b'decrypt-os-guardian-v1\0' + json.dumps(header, sort_keys=True, separators=(',', ':')).encode()

    def _save(self):
        if self.key is None:
            raise GuardianError('locked', status=423)
        header = {'schema': 1, 'kdf': 'scrypt-32768-8-1', 'unlockMode': self.unlock_mode, 'salt': base64.b64encode(self.salt).decode(),
                  'providers': {p: {'saved': True, 'autoLogin': e['autoLogin']} for p, e in self.entries.items()}}
        data = {p: {'username': e['username'].decode(), 'password': e['password'].decode(),
                    'autoLogin': e['autoLogin']} for p, e in self.entries.items()}
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(bytes(self.key)).encrypt(nonce, json.dumps(data).encode(), self._aad(header))
        self.files.write('vault.json', {**header, 'nonce': base64.b64encode(nonce).decode(),
                                      'ciphertext': base64.b64encode(encrypted).decode()})

    def save_provider(self, provider, username, password, auto_login):
        provider_id(provider)
        bounded_text(username, 1, 256)
        bounded_text(password, 1, 1024)
        if type(auto_login) is not bool:
            raise GuardianError('invalid_input')
        with self.mutex:
            if self.key is None:
                raise GuardianError('locked', status=423)
            old = self.entries.get(provider)
            new = {'username': bytearray(username.encode()), 'password': bytearray(password.encode()),
                   'autoLogin': auto_login}
            self.entries[provider] = new
            try:
                self._save()
            except Exception:
                self.entries.pop(provider)
                if old is not None:
                    self.entries[provider] = old
                self.wipe(new['username']); self.wipe(new['password'])
                raise
            if old:
                self.wipe(old['username']); self.wipe(old['password'])

    def delete_provider(self, provider):
        provider_id(provider)
        with self.mutex:
            if self.key is None:
                raise GuardianError('locked', status=423)
            old = self.entries.pop(provider, None)
            try:
                self._save()
            except Exception:
                if old is not None:
                    self.entries[provider] = old
                raise
            if old:
                self.wipe(old['username']); self.wipe(old['password'])

    @contextmanager
    def credential(self, provider):
        """Serialize lock against the bounded submission; copies never leave this lease."""
        with self.mutex:
            if self.key is None or provider not in self.entries:
                raise GuardianError('locked', status=423)
            entry = self.entries[provider]
            yield entry['username'].decode(), entry['password'].decode(), entry['autoLogin']
