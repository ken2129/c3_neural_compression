"""Closed, untrusted Codex canary metadata, never execution or research proof.

The trusted manager supplies the binding and approved model identity separately.
``command_observed`` records a fixed-command protocol event, not trusted command
execution. Even an exact checksum remains an unverified candidate. This type
does not authorize a model call, establish isolation, prove namespace cleanup,
or permit publication. The controller must require its independently verified
deployment/restart boundary and current explicit owner approval.

No model text, commands, paths, logs, URLs, credentials or arbitrary fields can
be retained. Artifact bytes are canonical JSON so their hash and serialization
are stable across manager restarts; the hash is derived, never caller-selected.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import math
import re

from .schema import ContractError, canonical, fields, identifier, loads

MODEL_RESULT_SCHEMA = 'c3-model-canary-result-v1'
EXPECTED_CHECKSUM = 333283335000
MAX_ARTIFACT_BYTES = 4096
MAX_ELAPSED_SECONDS = 120
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
_BINDING = ('request_id', 'run_id', 'issue_number', 'plan_digest', 'source_sha')


def _binding(request_id, run_id, issue_number, plan_digest, source_sha):
    identifier(request_id)
    identifier(run_id)
    if type(issue_number) is not int or not 1 <= issue_number <= 2 ** 31 - 1 or issue_number == 56:
        raise ContractError('invalid model canary issue')
    if (type(plan_digest) is not str or not _DIGEST.fullmatch(plan_digest)
            or type(source_sha) is not str or not _SHA.fullmatch(source_sha)):
        raise ContractError('invalid model canary binding digest')


def _model(value):
    # This syntax check does not establish that the identifier exists or is
    # immutable. The trusted deployment config must independently pin it.
    if type(value) is not str or not _MODEL.fullmatch(value) or value.endswith('-latest'):
        raise ContractError('approved pinned model identity required')


@dataclass(frozen=True)
class ModelCanaryResult:
    request_id: str
    run_id: str
    issue_number: int
    plan_digest: str
    source_sha: str
    model: str
    reported_outcome: str
    reported_checksum: int | None
    command_observed: bool
    elapsed_seconds: float
    schema: str = MODEL_RESULT_SCHEMA
    status: str = 'candidate_only'
    sandbox_mode: str = 'externalSandbox'
    requires_namespace_restart: bool = True
    research_success_verified: bool = False
    artifact_sha256: str = field(init=False)

    def __post_init__(self):
        _binding(*(getattr(self, key) for key in _BINDING))
        _model(self.model)
        for actual, expected in ((self.schema, MODEL_RESULT_SCHEMA),
                                 (self.status, 'candidate_only'),
                                 (self.sandbox_mode, 'externalSandbox')):
            if type(actual) is not str or actual != expected:
                raise ContractError('unsupported model candidate metadata')
        if (self.requires_namespace_restart is not True
                or self.research_success_verified is not False
                or type(self.command_observed) is not bool):
            raise ContractError('unverified candidate and namespace restart required')
        if type(self.reported_outcome) is not str or self.reported_outcome not in ('success', 'failure'):
            raise ContractError('invalid reported model outcome')
        if self.reported_outcome == 'success':
            if (self.command_observed is not True or type(self.reported_checksum) is not int
                    or self.reported_checksum != EXPECTED_CHECKSUM):
                raise ContractError('success candidate requires fixed command event and expected report')
        elif self.reported_checksum is not None:
            raise ContractError('failed candidate cannot claim a checksum')
        if (type(self.elapsed_seconds) not in (int, float)
                or not 0 <= self.elapsed_seconds <= MAX_ELAPSED_SECONDS
                or not math.isfinite(self.elapsed_seconds)):
            raise ContractError('model candidate elapsed time outside bounds')
        raw = canonical(self._artifact())
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ContractError('model candidate exceeds artifact size limit')
        object.__setattr__(self, 'artifact_sha256', hashlib.sha256(raw).hexdigest())

    def _artifact(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__
                if name != 'artifact_sha256'}

    def to_bytes(self):
        """Canonical candidate artifact without its separately derived digest."""
        return canonical(self._artifact())

    def to_dict(self):
        """Closed persistence record; includes the independently derived hash."""
        return asdict(self)

    @classmethod
    def from_bytes(cls, raw, *, request_id, run_id, issue_number, plan_digest, source_sha, model):
        """Parse canonical metadata and bind it to trusted manager inputs.

        Schema and reported-checksum checks do not establish actual execution.
        Model/Issue output cannot choose the expected arguments to this method.
        """
        _binding(request_id, run_id, issue_number, plan_digest, source_sha)
        _model(model)
        if type(raw) is not bytes or not raw or len(raw) > MAX_ARTIFACT_BYTES:
            raise ContractError('model candidate must be bounded canonical bytes')
        try:
            data = loads(raw)
            fields(data, tuple(name for name in cls.__dataclass_fields__ if name != 'artifact_sha256'))
            result = cls(**data)
            if raw != result.to_bytes():
                raise ContractError('model candidate must use canonical JSON')
        except (UnicodeError, RecursionError, ValueError, TypeError):
            raise ContractError('invalid model candidate artifact') from None
        expected = dict(request_id=request_id, run_id=run_id, issue_number=issue_number,
                        plan_digest=plan_digest, source_sha=source_sha, model=model)
        if any(getattr(result, key) != value for key, value in expected.items()):
            raise ContractError('model candidate binding mismatch')
        return result

    @classmethod
    def from_dict(cls, value):
        """Revalidate persisted closed metadata and its content hash."""
        fields(value, cls.__dataclass_fields__)
        result = cls(**{key: value[key] for key in value if key != 'artifact_sha256'})
        if type(value['artifact_sha256']) is not str or value['artifact_sha256'] != result.artifact_sha256:
            raise ContractError('saved model candidate digest mismatch')
        return result
