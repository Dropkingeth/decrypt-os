"""OS login-keyring custody for the existing encrypted Guardian vault.

No plaintext fallback, automatic keyring creation or prompt/unlock API. The
customer Linux session must already have unlocked its encrypted login keyring.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import json
import subprocess
import sys
import re
from pathlib import Path

from guardian_auth import GuardianError, bounded_text, require


class _SecretService:
    def _collection(self):
        try:
            if sys.platform != 'linux' or os.getuid() == 0:
                raise ValueError()
            if os.environ.get('DBUS_SESSION_BUS_ADDRESS') != 'unix:path=/run/user/%d/bus' % os.getuid():
                raise ValueError()
            import secretstorage
            from secretstorage.collection import Collection
            connection = secretstorage.dbus_init()
            collection = Collection(connection, '/org/freedesktop/secrets/collection/login')
            if collection.is_locked():
                raise ValueError()
            return collection
        except Exception:
            raise GuardianError('keyring_locked', status=423) from None

    @staticmethod
    def _attributes(vault_id):
        return {'application': 'decrypt-os-guardian', 'schema': 'unlock-v1', 'vault-id': vault_id}

    def put(self, vault_id, secret):
        try:
            collection = self._collection()
            attributes = self._attributes(vault_id)
            existing = list(collection.search_items(attributes))
            if existing:
                if len(existing) == 1 and not existing[0].is_locked() and secrets.compare_digest(existing[0].get_secret(), secret.encode()):
                    return  # Recover an interrupted adoption without replacement.
                raise GuardianError('keyring_conflict', status=409)
            collection.create_item('Decrypt OS saved sign-ins', attributes, secret.encode(),
                                   replace=False, content_type='text/plain; charset=utf8')
        except GuardianError:
            raise
        except Exception:
            raise GuardianError('keyring_unavailable', status=409) from None

    def get(self, vault_id):
        try:
            items = list(self._collection().search_items(self._attributes(vault_id)))
            if len(items) != 1 or items[0].is_locked():
                raise ValueError()
            return bounded_text(items[0].get_secret().decode(), 12, 256)
        except GuardianError:
            raise
        except Exception:
            raise GuardianError('keyring_unavailable', status=409) from None

    def remove(self, vault_id):
        try:
            for item in self._collection().search_items(self._attributes(vault_id)):
                item.delete()
        except GuardianError:
            raise
        except Exception:
            raise GuardianError('keyring_unavailable', status=409) from None


class LoginKeyring:
    """Bound Secret Service IPC duration and discard all child diagnostics."""
    def _call(self, action, vault_id, secret=None):
        try:
            result = subprocess.run([sys.executable, '-B', os.path.abspath(__file__), '--worker', action, vault_id],
                                    input=json.dumps({'secret': secret}).encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=8, check=False)
            if result.returncode != 0 or len(result.stdout) > 2048:
                raise ValueError()
            reply = json.loads(result.stdout)
            if reply.get('ok') is not True:
                code = reply.get('code')
                if code not in ('keyring_locked', 'keyring_conflict', 'keyring_unavailable'):
                    code = 'keyring_unavailable'
                raise GuardianError(code, status=423)
            return reply.get('secret')
        except GuardianError:
            raise
        except Exception:
            raise GuardianError('keyring_unavailable', status=423) from None

    def get(self, vault_id):
        return bounded_text(self._call('get', vault_id), 12, 256)

    def put(self, vault_id, secret):
        self._call('put', vault_id, secret)

    def remove(self, vault_id):
        self._call('remove', vault_id)


def login_identity():
    """Boot identity plus verified logind login, without starting a session."""
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if not re.fullmatch('[a-f0-9-]{36}', boot):
            return None, None
    except OSError:
        return None, None
    session = os.environ.get('XDG_SESSION_ID', '')
    if not re.fullmatch('[A-Za-z0-9_-]{1,64}', session):
        return boot, None
    try:
        reply = subprocess.run(['/usr/bin/loginctl', 'show-session', session,
                                '--property=User,Type,State,TimestampMonotonic'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=3, check=False)
        if reply.returncode or len(reply.stdout) > 4096:
            return boot, None
        fields = dict(line.split('=', 1) for line in reply.stdout.decode('ascii').splitlines())
        if (fields.get('User') != str(os.getuid()) or fields.get('Type') not in ('x11', 'wayland')
                or fields.get('State') != 'active' or not fields.get('TimestampMonotonic', '').isdigit()):
            return boot, None
        return boot, session + ':' + fields['TimestampMonotonic']
    except Exception:
        return boot, None


class VaultUnlock:
    def __init__(self, vault, keyring=None, identity=login_identity):
        self.vault, self.files = vault, vault.files
        self.keyring = keyring or LoginKeyring()
        self.identity = identity
        self.last_keyring_try = 0
        self.last_error = None
        # Set before a manual-lock write, including when storage is unavailable.
        # This is a process-local inhibit, not a claim of durable persistence.
        self.manual_lock_inhibit = None

    def _lock_applies(self, record):
        boot, login = self.identity()
        if boot is not None and record['bootId'] is not None and boot != record['bootId']:
            return False
        if (boot is not None and boot == record['bootId'] and login is not None
                and record['loginId'] is not None and login != record['loginId']):
            return False
        # Missing login identity never turns a service restart into consent.
        return True

    def manually_locked(self):
        if self.manual_lock_inhibit is not None:
            if self._lock_applies(self.manual_lock_inhibit):
                return True
            # Only a verified different login/boot releases this inhibit.
            self.manual_lock_inhibit = None
        missing = object()
        record = self.files.read('manual-lock.json', missing=missing)
        if record is missing:
            return False
        require(record, ('schema', 'locked', 'bootId', 'loginId'))
        if type(record['schema']) is not int or record['schema'] != 1 or type(record['locked']) is not bool:
            raise GuardianError('unsafe_storage')
        for name in ('bootId', 'loginId'):
            if record[name] is not None:
                bounded_text(record[name], 1, 128)
        return record['locked'] and self._lock_applies(record)

    def _clear_lock(self):
        self.files.write('manual-lock.json', {'schema': 1, 'locked': False, 'bootId': None, 'loginId': None})
        # A failed marker clear must not release an existing volatile inhibit.
        self.manual_lock_inhibit = None

    def mode(self):
        if self.vault.key is not None:
            return self.vault.unlock_mode
        document = self.files.read('vault.json')
        if document is None:
            return 'keyring'
        self.vault._header(document)
        return document.get('unlockMode', 'passphrase')

    def vault_id(self):
        document = self.files.read('vault.json')
        self.vault._header(document)
        return hashlib.sha256(document['salt'].encode()).hexdigest()

    def initialize(self, mode='keyring', passphrase=None):
        if mode not in ('keyring', 'passphrase'):
            raise GuardianError('invalid_input')
        if mode == 'passphrase':
            self.vault.unlock_mode = 'passphrase'
            self.vault.initialize(passphrase)
            return
        if passphrase is not None:
            raise GuardianError('invalid_fields')
        # Store the generated secret before the encrypted vault is committed.
        # An interrupted operation may leave an unused keyring item, never an
        # inaccessible vault whose only unlock secret was lost.
        if self.files.read('vault.json') is not None:
            raise GuardianError('vault_exists', status=409)
        secret = secrets.token_urlsafe(48)
        salt = secrets.token_bytes(16)
        import base64
        vault_id = hashlib.sha256(base64.b64encode(salt)).hexdigest()
        self.keyring.put(vault_id, secret)
        try:
            self.vault.unlock_mode = 'keyring'
            self.vault.salt = salt
            self.vault.key = self.vault.derive(secret, salt)
            self.vault._save()
        except Exception:
            self.vault.lock()
            # Preserve the keyring item if the vault write succeeded.
            raise

    def use_keyring(self, passphrase):
        # Existing vaults never silently migrate or gain auto-login consent.
        if self.mode() == 'keyring':
            raise GuardianError('keyring_already_configured', status=409)
        self.vault.unlock(passphrase)
        self.keyring.put(self.vault_id(), passphrase)
        self.vault.unlock_mode = 'keyring'
        try:
            self.vault._save()
        except Exception:
            self.vault.unlock_mode = 'passphrase'
            raise
        try:
            self._clear_lock()
        except Exception:
            self.vault.lock()
            raise

    def unlock(self, passphrase=None, explicit=True):
        if self.mode() == 'passphrase':
            self.vault.unlock(passphrase)
        else:
            if passphrase is not None:
                raise GuardianError('invalid_fields')
            self.vault.unlock(self.keyring.get(self.vault_id()))
        if explicit:
            try:
                self._clear_lock()
            except Exception:
                self.vault.lock()
                raise
        self.last_error = None

    def try_at_login(self):
        try:
            manually_locked = self.manually_locked()
        except Exception:
            self.vault.lock()
            self.last_error = 'unsafe_storage'
            raise
        if manually_locked:
            self.vault.lock()
            return
        if not self.vault.metadata()['configured'] or self.vault.key is not None:
            return
        if self.mode() != 'keyring':
            return
        now = self.vault.clock()
        if now < self.last_keyring_try + 30:
            return
        self.last_keyring_try = now
        try:
            self.unlock(explicit=False)
        except GuardianError as error:
            self.last_error = error.code

    def lock(self):
        self.manual_lock_inhibit = {'schema': 1, 'locked': True, 'bootId': None, 'loginId': None}
        try:
            boot, login = self.identity()
            self.manual_lock_inhibit.update(bootId=boot, loginId=login)
            self.files.write('manual-lock.json', self.manual_lock_inhibit)
        except Exception:
            self.last_error = 'manual_lock_unpersisted'
            raise GuardianError('manual_lock_unpersisted', status=503) from None
        finally:
            self.vault.lock()


if __name__ == '__main__':
    # Private pipe protocol only. This is not a user-facing credential CLI.
    try:
        import re
        if (len(sys.argv) != 4 or sys.argv[1] != '--worker' or
                sys.argv[2] not in ('get', 'put', 'remove') or not re.fullmatch('[a-f0-9]{64}', sys.argv[3])):
            raise ValueError()
        data = json.loads(sys.stdin.buffer.read(2049))
        require(data, ('secret',))
        action, vault_id = sys.argv[2:]
        store = _SecretService()
        if action == 'put':
            store.put(vault_id, bounded_text(data['secret'], 12, 256))
            result = None
        else:
            if data['secret'] is not None:
                raise ValueError()
            result = getattr(store, action)(vault_id)
        sys.stdout.write(json.dumps({'ok': True, 'secret': result}))
    except GuardianError as error:
        sys.stdout.write(json.dumps({'ok': False, 'code': error.code}))
    except Exception:
        sys.stdout.write('{"ok":false,"code":"keyring_unavailable"}')
