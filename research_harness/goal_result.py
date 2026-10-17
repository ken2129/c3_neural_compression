"""Private closed goal results and durable publication identity.

Execution approval does not authorize publishing generated files or prose. This
module exposes no public file ``content``; PR metadata is fixed and sanitized. A publisher
must obtain a fresh exact-content review receipt for the candidate file bytes,
paths and modes before every external write. Receipt flags supplied in model or
Issue JSON cannot grant that authority. The private summary is never implicitly
included in a public PR. Candidate completion is not verified research success.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re

from .goal_artifacts import GoalChangeCandidate
from .model_result import _binding, _model
from .schema import ContractError, canonical, fields

GOAL_RESULT_SCHEMA = 'c3-goal-result-v1'
MAX_SUMMARY_BYTES = 4096


def _sha(value, length):
    if type(value) is not str or re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is None:
        raise ContractError('invalid goal result digest')


@dataclass(frozen=True)
class GoalResult:
    request_id: str
    run_id: str
    issue_number: int
    plan_digest: str
    source_sha: str
    model: str
    candidate_outcome: str
    candidate_summary: str
    candidate: GoalChangeCandidate
    elapsed_seconds: float
    schema: str = GOAL_RESULT_SCHEMA
    requires_namespace_restart: bool = True
    research_success_verified: bool = False

    def __post_init__(self):
        _binding(self.request_id, self.run_id, self.issue_number,
                 self.plan_digest, self.source_sha)
        _model(self.model)
        if self.schema != GOAL_RESULT_SCHEMA:
            raise ContractError('unsupported goal result schema')
        if (self.requires_namespace_restart is not True
                or self.research_success_verified is not False):
            raise ContractError('goal candidate requires restart and remains unverified')
        if type(self.candidate_outcome) is not str or self.candidate_outcome not in ('completed', 'incomplete'):
            raise ContractError('invalid goal candidate outcome')
        if type(self.candidate_summary) is not str:
            raise ContractError('private goal summary must be text')
        try:
            summary_size = len(self.candidate_summary.encode('utf-8'))
        except UnicodeError as exc:
            raise ContractError('invalid goal summary encoding') from exc
        if (summary_size > MAX_SUMMARY_BYTES
                or any(ord(char) < 32 and char not in '\n\t' for char in self.candidate_summary)):
            raise ContractError('goal summary outside bounds')
        if (type(self.elapsed_seconds) not in (int, float)
                or not math.isfinite(self.elapsed_seconds)
                or not 0 <= self.elapsed_seconds <= 3600):
            raise ContractError('goal elapsed time outside parser bounds')
        if type(self.candidate) is not GoalChangeCandidate:
            raise ContractError('closed goal change candidate required')
        # Revalidate all byte hashes, paths and serialization even for directly
        # constructed dataclasses. Persistence is never a source of approval.
        checked = GoalChangeCandidate.from_dict(self.candidate.to_dict())
        if checked.source_sha != self.source_sha:
            raise ContractError('goal artifact source binding mismatch')
        if checked != self.candidate:
            raise ContractError('goal candidate is not canonical')

    @property
    def artifact_manifest_sha256(self):
        return self.candidate.candidate_digest

    @property
    def artifact_sha256(self):
        return self.artifact_manifest_sha256

    def to_dict(self):
        return {name: (self.candidate.to_dict() if name == 'candidate' else getattr(self, name))
                for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value):
        fields(value, cls.__dataclass_fields__)
        data = dict(value)
        data['candidate'] = GoalChangeCandidate.from_dict(data['candidate'])
        return cls(**data)


@dataclass(frozen=True)
class GoalPublicationRequest:
    """Private persisted envelope; review receipt is supplied separately.

    ``digest`` binds the full private envelope for crash recovery. Public content
    approval must additionally bind exact candidate files and their destination,
    source/base revisions and PR metadata. Neither digest is approval by itself.
    """
    result: GoalResult
    base_sha: str
    issue_content_sha256: str

    def __post_init__(self):
        if type(self.result) is not GoalResult:
            raise ContractError('closed goal result required')
        _sha(self.base_sha, 40)
        _sha(self.issue_content_sha256, 64)

    @property
    def digest(self):
        return hashlib.sha256(canonical(self.to_dict())).hexdigest()

    @property
    def branch(self):
        return f'research-goal-i{self.result.issue_number}-{self.digest[:32]}'

    @property
    def title(self):
        return f'[研究結果] Codex CPU goal 成果物候補: Issue #{self.result.issue_number}'

    @property
    def body(self):
        return (f'<!-- c3-goal-publication:{self.digest} -->\n'
                f'関連 Issue: #{self.result.issue_number}\n\n'
                '承認された CPU goal の成果物候補です。研究上の成功は未検証です。\n'
                '公開対象は別途内容確認されたファイル変更のみです。\n'
                f'実行 ID: {self.result.run_id}\n'
                f'実行元コミット: {self.result.source_sha}\n'
                f'公開基準コミット: {self.base_sha}\n'
                f'成果物候補 SHA-256: {self.candidate_digest}\n\n'
                '下書き PR として作成します。マージは人による確認が必要です。')

    @property
    def candidate_digest(self):
        return self.result.candidate.candidate_digest

    def to_dict(self):
        return dict(result=self.result.to_dict(), base_sha=self.base_sha,
                    issue_content_sha256=self.issue_content_sha256)

    @classmethod
    def from_dict(cls, value):
        fields(value, cls.__dataclass_fields__)
        return cls(GoalResult.from_dict(value['result']), value['base_sha'], value['issue_content_sha256'])
