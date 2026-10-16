"""Fail-closed result gates and an in-memory publisher. No live transport.

All approvals, checks, branch protections and source observations here are
synthetic fixtures. Passing this gate does not demonstrate independent security
boundaries and can never enable a GitHub merge in MVP0.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
import re

from .artifacts import (ContractError, EvidenceBinding, StrictRecord,
                        ValidationEvidence, VALIDATOR_VERSION, canonical_bytes,
                        digest, integer, relative_path, require_fields, sha, token)

REPOSITORY = "ken2129/c3_neural_compression"


def _tuple_strings(values, *, nonempty=True):
    if type(values) is not tuple or (nonempty and not values) or any(type(v) is not str or not v for v in values):
        raise ContractError("Expected immutable nonempty string list")
    if len(set(values)) != len(values):
        raise ContractError("Duplicate scope or check entry")


def _protected(path):
    lower = path.lower()
    parts = lower.split("/")
    if lower.startswith(("research_harness/", ".github/", ".git/", ".agents/", ".codex/", "evaluation/")):
        return True
    if any(re.search(r"(^|[_\-.])(auth(?:entication|orization)?|credentials?|secrets?|tokens?|polic(?:y|ies)|validators?|permissions?|harness|ci|dependencies|environment|deploy)([_\-.]|$)", part) for part in parts):
        return True
    if parts[-1].startswith(("requirements", "dockerfile")) or parts[-1] in (
            "pyproject.toml", "setup.py", "setup.cfg", "uv.lock", "poetry.lock", "environment.yml", "environment.yaml", "agents.md"):
        return True
    return not lower.startswith(("experiments/", "configs/", "docs/research/", "docs/results/", "results/", "figures/"))


@dataclass(frozen=True)
class CheckResult(StrictRecord):
    name: str
    head_sha: str
    conclusion: str
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        token(self.name)
        sha(self.head_sha, 40)
        if self.conclusion not in ("success", "failure", "pending", "unknown", "cancelled", "skipped"):
            raise ContractError("Unknown check conclusion")


@dataclass(frozen=True)
class PRSnapshot(StrictRecord):
    number: int
    binding: EvidenceBinding
    branch: str
    base_branch: str
    changed_paths: tuple[str, ...]
    checks: tuple[CheckResult, ...]
    state: str = "open"
    draft: bool = False
    mergeable: bool | None = None
    protections_satisfied: bool | None = None
    merge_commit: str | None = None
    merged_head_sha: str | None = None
    merged_base_sha: str | None = None
    issue_closed: bool | None = None
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        integer(self.number, 1)
        if type(self.binding) is not EvidenceBinding:
            raise ContractError("Missing PR identity binding")
        token(self.branch)
        token(self.base_branch)
        _tuple_strings(self.changed_paths)
        for path in self.changed_paths:
            relative_path(path)
        if type(self.checks) is not tuple or any(type(c) is not CheckResult for c in self.checks):
            raise ContractError("Invalid checks schema")
        if len({c.name for c in self.checks}) != len(self.checks):
            raise ContractError("Ambiguous duplicate checks")
        if self.state not in ("open", "closed", "merged") or type(self.draft) is not bool:
            raise ContractError("Invalid PR state")
        for value in (self.mergeable, self.protections_satisfied, self.issue_closed):
            if value is not None and type(value) is not bool:
                raise ContractError("Invalid tri-state observation")
        for value in (self.merge_commit, self.merged_head_sha, self.merged_base_sha):
            if value is not None:
                sha(value, 40)

    @classmethod
    def from_dict(cls, value):
        require_fields(value, {field.name for field in fields(cls)})
        if type(value["checks"]) is not list or type(value["changed_paths"]) is not list:
            raise ContractError("Invalid PR arrays")
        return cls(**{**value, "binding": EvidenceBinding.from_dict(value["binding"]),
                      "changed_paths": tuple(value["changed_paths"]),
                      "checks": tuple(CheckResult.from_dict(c) for c in value["checks"])})


@dataclass(frozen=True)
class MergePolicy(StrictRecord):
    policy_version: str
    required_checks: tuple[str, ...]
    validator_version: str = VALIDATOR_VERSION
    repository: str = REPOSITORY
    base_branch: str = "main"
    fixture_only: bool = True
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        token(self.policy_version)
        _tuple_strings(self.required_checks)
        for name in self.required_checks:
            token(name)
        if self.repository != REPOSITORY or self.base_branch != "main" or self.fixture_only is not True or self.validator_version != VALIDATOR_VERSION:
            raise ContractError("Unsupported publisher policy")

    @classmethod
    def from_dict(cls, value):
        require_fields(value, {field.name for field in fields(cls)})
        if type(value["required_checks"]) is not list:
            raise ContractError("Invalid required checks")
        return cls(**{**value, "required_checks": tuple(value["required_checks"])})


@dataclass(frozen=True)
class MergeAuthorization(StrictRecord):
    binding: EvidenceBinding
    pr_number: int
    branch: str
    allowed_paths: tuple[str, ...]
    artifact_digest: str
    execution_approved: bool
    result_publication_approved: bool
    result_merge_approved: bool
    approval_current: bool
    complete_result: bool
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        if type(self.binding) is not EvidenceBinding:
            raise ContractError("Missing authorization binding")
        integer(self.pr_number, 1)
        token(self.branch)
        _tuple_strings(self.allowed_paths)
        for path in self.allowed_paths:
            relative_path(path)
        sha(self.artifact_digest)
        for value in (self.execution_approved, self.result_publication_approved,
                      self.result_merge_approved, self.approval_current, self.complete_result):
            if type(value) is not bool:
                raise ContractError("Invalid approval flag")

    @classmethod
    def from_dict(cls, value):
        require_fields(value, {field.name for field in fields(cls)})
        if type(value["allowed_paths"]) is not list:
            raise ContractError("Invalid scope array")
        return cls(**{**value, "binding": EvidenceBinding.from_dict(value["binding"]),
                      "allowed_paths": tuple(value["allowed_paths"])})


@dataclass(frozen=True)
class GateDecision(StrictRecord):
    allowed: bool
    reason: str
    evidence_digest: str | None = None
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        if type(self.allowed) is not bool or self.reason not in (
                "APPROVAL_OR_COMPLETION_MISSING", "IDENTITY_OR_SOURCE_CHANGED", "HARNESS_ISSUE_EXCLUDED",
                "POLICY_OR_VALIDATOR_CHANGED", "ARTIFACT_CHANGED", "PR_NOT_ELIGIBLE", "SCOPE_EXCLUDED",
                "PROTECTION_OR_MERGEABILITY_UNCONFIRMED", "REQUIRED_CHECKS_UNCONFIRMED", "MOCK_GATE_PASSED"):
            raise ContractError("Invalid gate decision")
        if self.evidence_digest is not None:
            sha(self.evidence_digest)


class MergeGate:
    @staticmethod
    def evaluate(pr: PRSnapshot, authorization: MergeAuthorization,
                 evidence: ValidationEvidence, policy: MergePolicy) -> GateDecision:
        if any(type(obj) is not kind for obj, kind in ((pr, PRSnapshot), (authorization, MergeAuthorization),
                                                     (evidence, ValidationEvidence), (policy, MergePolicy))):
            raise ContractError("Invalid gate inputs")
        if not all((authorization.execution_approved, authorization.result_publication_approved,
                    authorization.result_merge_approved, authorization.approval_current, authorization.complete_result)):
            return GateDecision(False, "APPROVAL_OR_COMPLETION_MISSING")
        if pr.binding != authorization.binding or pr.binding != evidence.binding or pr.number != authorization.pr_number or pr.branch != authorization.branch:
            return GateDecision(False, "IDENTITY_OR_SOURCE_CHANGED")
        if pr.binding.issue_number == 56:
            return GateDecision(False, "HARNESS_ISSUE_EXCLUDED")
        if pr.binding.policy_version != policy.policy_version or pr.binding.validator_version != policy.validator_version or pr.binding.repository != policy.repository:
            return GateDecision(False, "POLICY_OR_VALIDATOR_CHANGED")
        if evidence.artifact_digest != authorization.artifact_digest:
            return GateDecision(False, "ARTIFACT_CHANGED")
        if pr.state != "open" or pr.draft or pr.base_branch != policy.base_branch:
            return GateDecision(False, "PR_NOT_ELIGIBLE")
        if any(_protected(path) for path in pr.changed_paths) or not set(pr.changed_paths) <= set(authorization.allowed_paths):
            return GateDecision(False, "SCOPE_EXCLUDED")
        if pr.mergeable is not True or pr.protections_satisfied is not True:
            return GateDecision(False, "PROTECTION_OR_MERGEABILITY_UNCONFIRMED")
        checks = {check.name: check for check in pr.checks}
        if any(name not in checks or checks[name].head_sha != pr.binding.head_sha or checks[name].conclusion != "success"
               for name in policy.required_checks):
            return GateDecision(False, "REQUIRED_CHECKS_UNCONFIRMED")
        return GateDecision(True, "MOCK_GATE_PASSED", digest(canonical_bytes(evidence.to_dict())))


@dataclass(frozen=True)
class PublicationState(StrictRecord):
    phase: str = "PUBLISH_PENDING"
    pr_number: int | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    merge_commit: str | None = None
    schema_version: int = 1

    def __post_init__(self):
        self._version()
        if self.phase not in ("PUBLISH_PENDING", "BLOCKED", "MERGE_UNKNOWN", "RETRY_READY", "CLOSE_PENDING", "MERGED", "EXTERNAL_REVIEW"):
            raise ContractError("Unknown publication phase")
        if self.pr_number is not None:
            integer(self.pr_number, 1)
        for value in (self.head_sha, self.base_sha, self.merge_commit):
            if value is not None:
                sha(value, 40)
        if self.phase in ("MERGE_UNKNOWN", "RETRY_READY", "CLOSE_PENDING", "MERGED") and any(
                value is None for value in (self.pr_number, self.head_sha, self.base_sha)):
            raise ContractError("Incomplete persisted publication identity")
        if self.phase in ("CLOSE_PENDING", "MERGED") and self.merge_commit is None:
            raise ContractError("Missing confirmed merge commit")


class FakeGitHubTransport:
    """In-memory CAS only. An exact type check excludes live transport injection."""
    def __init__(self, pr: PRSnapshot):
        if type(pr) is not PRSnapshot:
            raise ContractError("Invalid fake PR")
        self.pr = pr
        self.available = True
        self.next_merge_response = "success"  # success, unknown_before, unknown_after
        self.definitively_not_applied = False
        self.merge_calls = 0
        self.close_on_merge = True

    def read_pr(self):
        return self.pr if self.available else None

    def merge(self, *, expected_head, expected_base):
        self.merge_calls += 1
        if self.pr.binding.head_sha != expected_head or self.pr.binding.base_sha != expected_base or self.pr.state != "open":
            return "rejected"
        mode = self.next_merge_response
        self.next_merge_response = "success"
        self.definitively_not_applied = mode == "unknown_before"
        if mode == "unknown_before":
            return "unknown"
        if mode not in ("success", "unknown_after"):
            raise ContractError("Unknown fake response mode")
        commit = digest(canonical_bytes({"head": expected_head, "base": expected_base}))[:40]
        self.pr = replace(self.pr, state="merged", merge_commit=commit,
                          merged_head_sha=expected_head, merged_base_sha=expected_base,
                          issue_closed=self.close_on_merge)
        return "unknown" if mode == "unknown_after" else "success"


class FakePublisher:
    """Recoverable merge/readback model; it cannot send network requests.

    Supply ``persist_state`` to durably journal intent before the fake call. A restored MERGE_UNKNOWN
    state performs readback only; a definite non-application permits a new gate.
    """
    def __init__(self, transport: FakeGitHubTransport, state: PublicationState | None = None, *, persist_state=None):
        if type(transport) is not FakeGitHubTransport:
            raise ContractError("Live publisher unsupported in MVP0")
        self.transport = transport
        self.state = state or PublicationState()
        if type(self.state) is not PublicationState:
            raise ContractError("Invalid publication state")
        self.last_gate = None
        if persist_state is not None and not callable(persist_state):
            raise ContractError("Invalid publication state writer")
        self._persist_state = persist_state

    def _save(self):
        if self._persist_state is not None:
            self._persist_state(self.state)
        return self.state

    def publish(self, authorization, evidence, policy) -> PublicationState:
        if self.state.phase in ("MERGE_UNKNOWN", "CLOSE_PENDING", "MERGED"):
            return self.reconcile()
        if self.state.phase == "EXTERNAL_REVIEW":
            return self._save()
        pr = self.transport.read_pr()
        if pr is None:
            self.state = replace(self.state, phase="BLOCKED")
            return self._save()
        self.last_gate = MergeGate.evaluate(pr, authorization, evidence, policy)
        if not self.last_gate.allowed:
            self.state = replace(self.state, phase="BLOCKED")
            return self._save()
        # Record intent BEFORE the side effect, so a crash cannot cause a resend.
        self.state = PublicationState("MERGE_UNKNOWN", pr.number, pr.binding.head_sha, pr.binding.base_sha)
        self._save()
        # Recheck after journaling: the head, base or required checks may have
        # changed since the initial read. The fake transport also uses SHA CAS.
        current = self.transport.read_pr()
        if current is None:
            # No write was attempted, but keep unknown intent until a definitive
            # readback rather than making an unsafe automatic resend.
            return self._save()
        self.last_gate = MergeGate.evaluate(current, authorization, evidence, policy)
        if not self.last_gate.allowed:
            self.state = replace(self.state, phase="BLOCKED")
            return self._save()
        response = self.transport.merge(expected_head=self.state.head_sha, expected_base=self.state.base_sha)
        if response == "unknown":
            return self._save()
        if response == "rejected":
            self.state = replace(self.state, phase="BLOCKED")
            return self._save()
        return self.reconcile()

    def reconcile(self) -> PublicationState:
        if self.state.phase not in ("MERGE_UNKNOWN", "CLOSE_PENDING", "MERGED"):
            return self._save()
        pr = self.transport.read_pr()
        if pr is None:
            return self._save()
        if pr.number != self.state.pr_number:
            self.state = replace(self.state, phase="EXTERNAL_REVIEW")
        elif pr.state == "merged":
            if pr.merged_head_sha != self.state.head_sha or pr.merged_base_sha != self.state.base_sha or pr.merge_commit is None:
                self.state = replace(self.state, phase="EXTERNAL_REVIEW")
            else:
                self.state = replace(self.state, phase="MERGED" if pr.issue_closed is True else "CLOSE_PENDING", merge_commit=pr.merge_commit)
        elif self.state.phase in ("CLOSE_PENDING", "MERGED") or pr.state == "closed":
            self.state = replace(self.state, phase="EXTERNAL_REVIEW")
        elif self.transport.definitively_not_applied:
            self.state = replace(self.state, phase="RETRY_READY")
        # Open alone is not proof that a timed-out operation was not applied.
        return self._save()
