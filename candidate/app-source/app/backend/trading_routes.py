"""Generic encrypted secret storage, encrypted under an unlocked Guardian vault.

This base class owns only the encrypted-blob plumbing (key derivation, load,
save). Concrete stores such as ``IngressSecrets`` (trading_ingress_secrets.py)
define their own entry shape, validation and lease semantics.
"""
from __future__ import annotations

import base64
import json
import re
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from guardian_auth import GuardianError

FILE = 'execution-routes.json'
AAD = b'decrypt-os-execution-routes-v1'
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z')
DIGEST = re.compile(r'[a-f0-9]{64}\Z')


def require(condition, code='invalid_route'):
    if not condition:
        raise GuardianError(code)


class RouteSecrets:
    filename = FILE
    aad = AAD

    def __init__(self, guardian_vault):
        self.vault = guardian_vault

    def _key(self):
        if self.vault.key is None:
            raise GuardianError('locked', status=423)
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                    info=self.aad).derive(bytes(self.vault.key))

    def _load(self):
        key = self._key()
        raw = self.vault.files.read(self.filename)
        if raw is None:
            return {}
        try:
            require(type(raw) is dict and set(raw) == {'schema', 'nonce', 'ciphertext'})
            require(type(raw['schema']) is int and raw['schema'] == 1)
            nonce = base64.b64decode(raw['nonce'], validate=True)
            encrypted = base64.b64decode(raw['ciphertext'], validate=True)
            require(len(nonce) == 12 and 16 <= len(encrypted) <= 32768)
            routes = json.loads(AESGCM(key).decrypt(nonce, encrypted, self.aad))
            require(type(routes) is dict and len(routes) <= 32)
            for route_id, entry in routes.items():
                self._validate(route_id, entry)
            return routes
        except Exception:
            raise GuardianError('route_storage_invalid') from None

    def _save(self, routes):
        require(len(routes) <= 32, 'route_limit')
        raw = json.dumps(routes, sort_keys=True, separators=(',', ':')).encode()
        require(len(raw) <= 32000, 'route_limit')
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self._key()).encrypt(nonce, raw, self.aad)
        self.vault.files.write(self.filename, {'schema': 1, 'nonce': base64.b64encode(nonce).decode(),
                                    'ciphertext': base64.b64encode(encrypted).decode()})
