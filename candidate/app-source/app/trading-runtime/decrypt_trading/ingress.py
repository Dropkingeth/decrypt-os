"""Authenticated raw-Pine and HMAC relay ingress. No network listener or secrets.

The serving app owns TLS, body limits before buffering, route secret leasing and
no-access-log routing. These adapters never accept producer identity from JSON.
"""
import hashlib
import hmac
import re

from .runtime import RuntimeError, canonical


def _raw(value):
    if type(value) is not bytes or not 0 < len(value) <= 40960:
        raise RuntimeError('bounded raw request bytes required')
    return value


class SignalIngress:
    """Direct TradingView webhook body remains byte-for-byte unchanged.

    lease(instance_id, 'pine') yields the customer's random URL-safe token from
    the unlocked encrypted vault. The HTTP adapter passes only the token segment,
    never a complete URL. No raw headers/path/body are logged by this package.
    Do not expose this through an unauthenticated local JSON control endpoint.
    """
    def __init__(self, runtime, lease):
        self.runtime, self.lease = runtime, lease

    def accept(self, raw, *, presented_secret):
        raw = _raw(raw)
        if type(presented_secret) is not str or re.fullmatch(r'[A-Za-z0-9_-]{43,128}', presented_secret) is None:
            raise RuntimeError('input authentication failed')
        with self.runtime.mutex:
            return self._accept_locked(raw, presented_secret)

    def _accept_locked(self, raw, presented_secret):
        with self.lease(self.runtime.binding['instanceId'], 'pine') as expected:
            if (type(expected) is not str or re.fullmatch(r'[A-Za-z0-9_-]{43,128}', expected) is None
                    or not hmac.compare_digest(presented_secret, expected)):
                raise RuntimeError('input authentication failed')
            payload = self.runtime.modules['storage'].strict_json(raw, max_bytes=40960)
            if type(payload) is not dict:
                raise RuntimeError('Pine object required')
            event_id = self.runtime.input_identity(payload)
            return self.runtime.admit_authenticated('pine', raw,
                producer_id=self.runtime.binding['signalProducerId'], event_id=event_id)


class SignedIngress:
    """Controlled customer relay / market-source HMAC boundary.

    Header metadata is NOT broker truth or a provider qualification. The key
    lease is selected from the installed instance and channel, not client data.
    Each channel has a distinct minimum-256-bit key. TradingView itself cannot
    compute this HMAC; use SignalIngress for its unchanged alert payload.
    """
    def __init__(self, runtime, lease):
        self.runtime, self.lease = runtime, lease

    @staticmethod
    def signing_bytes(metadata, raw):
        return ('decrypt.ingress.v1\n' + canonical(metadata) + '\n' + hashlib.sha256(raw).hexdigest()).encode()

    def accept(self, channel, raw, *, metadata, signature):
        raw = _raw(raw)
        if channel not in ('pine', 'quote', 'bar'):
            raise RuntimeError('unsupported signed channel')
        fields = {'schema', 'instanceId', 'channel', 'feedId', 'eventId', 'sequence', 'sentAt'}
        if type(metadata) is not dict or set(metadata) != fields:
            raise RuntimeError('signed metadata fields required')
        if (metadata['schema'] != 'decrypt.ingress.v1' or metadata['instanceId'] != self.runtime.binding['instanceId']
                or metadata['channel'] != channel
                or metadata['feedId'] != (None if channel == 'pine' else self.runtime.binding['feedId'])
                or type(metadata['sentAt']) is not int or not 0 <= self.runtime.clock()-metadata['sentAt'] <= 15000
                or type(metadata['sequence']) is not int or not 0 < metadata['sequence'] < 2**63
                or type(metadata['eventId']) is not str or re.fullmatch(r'[A-Za-z0-9_.:@+-]{1,240}', metadata['eventId']) is None
                or type(signature) is not str or re.fullmatch('[a-f0-9]{64}', signature) is None):
            raise RuntimeError('signed metadata binding or clock invalid')
        with self.runtime.mutex:
            return self._accept_locked(channel, raw, metadata, signature)

    def _accept_locked(self, channel, raw, metadata, signature):
        with self.lease(self.runtime.binding['instanceId'], channel) as key:
            if type(key) is not bytes or not 32 <= len(key) <= 128:
                raise RuntimeError('private signing key unavailable')
            expected = hmac.new(key, self.signing_bytes(metadata, raw), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise RuntimeError('input authentication failed')
            producer = self.runtime.binding['signalProducerId' if channel == 'pine' else 'feedProducerId']
            return self.runtime.admit_authenticated(channel, raw, producer_id=producer,
                event_id=metadata['eventId'], sequence=metadata['sequence'])
