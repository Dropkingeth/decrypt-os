"""Distinct encrypted signal/market producer secrets in the Guardian vault."""
import base64
from contextlib import contextmanager
import re
import secrets
from guardian_auth import GuardianError
from trading_routes import RouteSecrets, IDENTIFIER, require


class IngressSecrets(RouteSecrets):
    filename = 'execution-ingress.json'
    aad = b'decrypt-os-execution-ingress-v1'

    @staticmethod
    def _id(instance_id, channel):
        require(type(instance_id) is str and IDENTIFIER.fullmatch(instance_id))
        require(channel in ('pine', 'quote', 'bar'))
        return instance_id + ':' + channel

    @staticmethod
    def _validate(identity, entry):
        require(type(entry) is dict and set(entry) in ({'instanceId', 'channel', 'secret'},
                                                       {'instanceId', 'channel', 'secret', 'generation'}))
        require(identity == IngressSecrets._id(entry['instanceId'], entry['channel']))
        require(type(entry['secret']) is str and re.fullmatch(r'[A-Za-z0-9_-]{43}', entry['secret']))
        require(len(base64.urlsafe_b64decode(entry['secret'] + '=')) == 32)
        require(type(entry.get('generation', 1)) is int and 1 <= entry.get('generation', 1) < 2**53)

    def metadata(self):
        with self.vault.mutex:
            if self.vault.key is None:
                return {'locked': True, 'producers': None}
            return {'locked': False, 'producers': [{'instanceId': value['instanceId'], 'channel': value['channel'],
                                                   'generation': value.get('generation', 1)}
                                                   for _, value in sorted(self._load().items())]}

    def create(self, instance_id, channel):
        identity = self._id(instance_id, channel)
        with self.vault.mutex:
            values = self._load()
            require(identity not in values, 'ingress_secret_exists')
            token = secrets.token_urlsafe(32)
            values[identity] = {'instanceId': instance_id, 'channel': channel, 'secret': token, 'generation': 1}
            self._save(values)
            # This one-time result is for an explicitly requested local form,
            # never generic status/history/logging or browser storage.
            return token

    def rotate(self, instance_id, expected_generation):
        """Internal Pine rotation; service must hold/settle runtime before lease."""
        identity = self._id(instance_id, 'pine')
        require(type(expected_generation) is int and 1 <= expected_generation < 2**53 - 1,
                'ingress_generation_changed')
        with self.vault.mutex:
            values = self._load()
            old = values.get(identity)
            require(old is not None, 'ingress_secret_missing')
            if old.get('generation', 1) != expected_generation:
                raise GuardianError('ingress_generation_changed',
                                    'The saved key changed. Refresh status and read the saved private address before replacing it.', 409)
            token = secrets.token_urlsafe(32)
            values[identity] = {**old, 'secret': token, 'generation': expected_generation + 1}
            try:
                self._save(values)
            except OSError:
                # A replacement may have committed before fsync failed. The
                # saved generation/address is the recovery authority, not retry.
                raise GuardianError('ingress_rotation_storage_uncertain',
                                    'Check the saved private address and generation before retrying rotation.', 409) from None
            return token, expected_generation + 1

    def save(self, *args, **kwargs):
        raise GuardianError('unsupported_ingress_action')

    def delete(self, *args, **kwargs):
        raise GuardianError('unsupported_ingress_action')

    @contextmanager
    def lease(self, instance_id, channel):
        identity = self._id(instance_id, channel)
        with self.vault.mutex:
            value = self._load().get(identity)
            require(value is not None, 'ingress_secret_missing')
            # Direct TradingView Pine receiver compares the URL-safe token.
            # Controlled market/relay producers use its 32-byte HMAC key.
            yield value['secret'] if channel == 'pine' else base64.urlsafe_b64decode(value['secret'] + '=')

    @contextmanager
    def lease_signed(self, instance_id, channel):
        with self.lease(instance_id, channel) as value:
            yield base64.urlsafe_b64decode(value + '=') if type(value) is str else value
