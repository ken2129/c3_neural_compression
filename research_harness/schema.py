"""Strict, immutable MVP0 plans. Untrusted JSON never supplies executable code."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import secrets

REPOSITORY = 'ken2129/c3_neural_compression'
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')


class ContractError(ValueError):
    """An untrusted input failed admission."""


def fields(value, required, optional=()):
    if type(value) is not dict:
        raise ContractError('expected object')
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ContractError('missing or unknown fields')


def identifier(value):
    if type(value) is not str or ID.fullmatch(value) is None:
        raise ContractError('invalid opaque identifier')
    return value


def positive(value, *, integer=False, zero=False):
    if type(value) not in ((int,) if integer else (int, float)):
        raise ContractError('invalid number type')
    if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise ContractError('number must be finite and within bounds')
    return value


def relative_path(value):
    if type(value) is not str or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise ContractError('invalid relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        raise ContractError('invalid relative path')
    return value


def contained_path(root, relative):
    """Read-side check only; production needs an enforced worker boundary."""
    relative_path(relative)
    root = Path(root).resolve(strict=True)
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ContractError('symlink prohibited')
    if not candidate.resolve().is_relative_to(root):
        raise ContractError('path escapes root')
    return candidate


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ContractError('noncanonical JSON') from exc


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(_):
        raise ContractError('nonfinite JSON number')
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except json.JSONDecodeError as exc:
        raise ContractError('invalid JSON') from exc


@dataclass(frozen=True)
class Budget:
    wall_seconds: float
    total_seconds: float
    max_attempts: int
    max_output_bytes: int
    min_free_bytes: int

    @classmethod
    def parse(cls, data):
        fields(data, cls.__dataclass_fields__)
        for key, value in data.items():
            positive(value, integer=key in ('max_attempts', 'max_output_bytes', 'min_free_bytes'),
                     zero=key == 'min_free_bytes')
        if data['wall_seconds'] > data['total_seconds']:
            raise ContractError('attempt budget exceeds total')
        # Fixture has bounded, tiny resource use; cannot turn it into a daemon.
        if data['wall_seconds'] > 30 or data['total_seconds'] > 120 or data['max_attempts'] > 4:
            raise ContractError('fixture budget ceiling exceeded')
        return cls(**data)


@dataclass(frozen=True)
class Plan:
    version: int
    request_id: str
    plan_id: str
    revision: int
    repo: str
    issue: int
    environment: str
    operation: str
    source_sha: str
    allowed_paths: tuple[str, ...]
    budget: Budget
    retry_codes: tuple[str, ...]
    storage_required: bool

    @classmethod
    def parse(cls, data):
        fields(data, cls.__dataclass_fields__)
        if type(data['version']) is not int or data['version'] != 1:
            raise ContractError('unsupported schema version')
        for key in ('request_id', 'plan_id', 'environment'):
            identifier(data[key])
        positive(data['revision'], integer=True)
        positive(data['issue'], integer=True)
        if data['repo'] != REPOSITORY:
            raise ContractError('repository not allowed')
        if data['operation'] != 'cpu_fixture' or data['environment'] != 'local-fixture':
            raise ContractError('live operation/environment unsupported')
        if type(data['source_sha']) is not str or not SHA.fullmatch(data['source_sha']):
            raise ContractError('source must be fixed commit SHA')
        for key in ('allowed_paths', 'retry_codes'):
            if type(data[key]) is not list or len(data[key]) != len(set(map(str, data[key]))):
                raise ContractError('expected unique list')
        if not data['allowed_paths']:
            raise ContractError('source scope required')
        for path in data['allowed_paths']:
            relative_path(path)
        if any(code not in ('transient_api', 'transient_io') for code in data['retry_codes']):
            raise ContractError('retry class not allowed')
        if type(data['storage_required']) is not bool:
            raise ContractError('storage_required must be boolean')
        values = dict(data, budget=Budget.parse(data['budget']),
                      allowed_paths=tuple(data['allowed_paths']), retry_codes=tuple(data['retry_codes']))
        return cls(**values)

    def to_dict(self):
        result = asdict(self)
        result['allowed_paths'] = list(self.allowed_paths)
        result['retry_codes'] = list(self.retry_codes)
        return result

    def digest(self, salt):
        if type(salt) is not str or not re.fullmatch(r'[0-9a-f]{64}', salt):
            raise ContractError('private 256-bit salt required')
        return hashlib.sha256(bytes.fromhex(salt) + canonical(self.to_dict())).hexdigest()


def new_salt():
    return secrets.token_hex(32)


def freeze_source(plan, candidate_sha, changed_paths, *, independently_validated=False):
    """Two-stage scope contract; tests are not semantic approval evidence."""
    if type(candidate_sha) is not str or not SHA.fullmatch(candidate_sha):
        raise ContractError('candidate must be immutable commit')
    if independently_validated is not True:
        raise ContractError('independent source validation required')
    if type(changed_paths) not in (tuple, list) or not changed_paths or any(relative_path(p) not in plan.allowed_paths for p in changed_paths):
        raise ContractError('source scope changed; reapproval required')
    return {'version': 1, 'plan_id': plan.plan_id, 'revision': plan.revision,
            'base_sha': plan.source_sha, 'source_sha': candidate_sha,
            'changed_paths': sorted(changed_paths), 'mode': 'fixture-only'}


def require_live_environment():
    raise ContractError('BLOCKED_ENV: real issuer, chat bridge, protected validator, isolation, '
                        'device allocation and hard limits are not implemented or verified')


def fixture_plan():
    return Plan.parse({'version': 1, 'request_id': 'fixture-request', 'plan_id': 'fixture-plan',
                      'revision': 1, 'repo': REPOSITORY, 'issue': 56,
                      'environment': 'local-fixture', 'operation': 'cpu_fixture',
                      'source_sha': '0' * 40, 'allowed_paths': ['docs/research/fixture.md'],
                      'budget': {'wall_seconds': 5, 'total_seconds': 10, 'max_attempts': 2,
                                 'max_output_bytes': 1048576, 'min_free_bytes': 1048576},
                      'retry_codes': ['transient_io'], 'storage_required': False})
