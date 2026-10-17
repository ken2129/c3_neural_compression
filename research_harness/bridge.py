"""Non-network chat/issuer transport fixture. No real user authentication claim."""
from dataclasses import dataclass
import hashlib

from research_harness.schema import ContractError, canonical, fields, identifier


@dataclass(frozen=True)
class Envelope:
    event_id: str
    request_id: str
    sequence: int
    kind: str
    payload_digest: str

    @classmethod
    def parse(cls, value):
        fields(value, cls.__dataclass_fields__)
        identifier(value['event_id']); identifier(value['request_id'])
        if type(value['sequence']) is not int or value['sequence'] < 0:
            raise ContractError('invalid event sequence')
        if value['kind'] not in ('plan', 'approval', 'revocation', 'status', 'report'):
            raise ContractError('unknown event kind')
        digest = value['payload_digest']
        if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ContractError('invalid payload digest')
        return cls(**value)


class FakeChatBridge:
    """Append-only in-memory transport; event delivery can be reordered/repeated.

    The Ledger, not arrival sequence, owns approval/revocation authority. This
    transport is explicitly volatile and unsuitable for production recovery.
    """
    mode = 'fixture-only'

    def __init__(self):
        self._events = {}
        self._payloads = {}

    def send(self, event_id, request_id, kind, payload):
        encoded = canonical(payload)
        envelope = Envelope.parse({'event_id': event_id, 'request_id': request_id,
                                   'sequence': len(self._events), 'kind': kind,
                                   'payload_digest': hashlib.sha256(encoded).hexdigest()})
        if event_id in self._events:
            old = self._events[event_id]
            if (old.request_id, old.kind, old.payload_digest) != (request_id, kind, envelope.payload_digest):
                raise ContractError('immutable event was changed')
            return old
        self._events[event_id] = envelope
        self._payloads[event_id] = encoded
        return envelope

    def readback(self, event_id):
        from research_harness.schema import loads
        if event_id not in self._events:
            raise ContractError('event unavailable')
        return self._events[event_id], loads(self._payloads[event_id])

    def poll(self, after_sequence=-1):
        return tuple(e for e in self._events.values() if e.sequence > after_sequence)


class UnsupportedLiveBridge:
    def __init__(self, *args, **kwargs):
        raise ContractError('BLOCKED_ENV: real chat delivery and trusted issuer are not connected')
