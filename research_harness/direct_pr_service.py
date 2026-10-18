"""Thin approved-Issue dispatcher. Codex owns its draft PR; manager never writes GitHub.

The worker's final URL is an untrusted report, not proof of publication or research
success. GitHub's PR event/readback is the independent notification channel.
"""
from dataclasses import dataclass, asdict
import math
import re

from .goal_plan import DIRECT_PR_KIND, validate_goal_plan, require_installed_goal
from .goal_service import GoalController, goal_binding
from .issue_intake import classify, content_hash, timestamp
from .owner_approval import inspect_once
from .manager_runtime import RecheckRejected
from .restart_boundary import RestartEpoch
from .schema import ContractError, REPOSITORY, fields
from .service import _RunApprovalRecheck


def validate_pr_url(value):
    if value is not None and (type(value) is not str or re.fullmatch(
            re.escape('https://github.com/' + REPOSITORY + '/pull/') + '[1-9][0-9]*', value) is None):
        raise ContractError('invalid reported PR URL')
    return value


@dataclass(frozen=True)
class DirectPRResult:
    binding: dict
    model: str
    outcome: str
    summary: str
    pr_url: str | None
    elapsed_seconds: float

    def to_dict(self):
        from .service_state import validate_binding
        validate_binding(self.binding)
        if (type(self.model) is not str or not self.model or len(self.model) > 64
                or self.outcome not in ('completed', 'incomplete')
                or type(self.summary) is not str or len(self.summary.encode()) > 4096
                or type(self.elapsed_seconds) not in (int, float)
                or not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0):
            raise ContractError('invalid worker completion report')
        validate_pr_url(self.pr_url)
        return asdict(self)

    @classmethod
    def from_dict(cls, raw):
        fields(raw, ('binding', 'model', 'outcome', 'summary', 'pr_url', 'elapsed_seconds'))
        value = cls(**raw)
        value.to_dict()
        return value


class DirectPRController(GoalController):
    """Reuse owner verification and claims; no candidate or publisher gates."""
    def __init__(self, *args, profile, worker_pr_config, **kwargs):
        from .worker_pr import validate_worker_pr_config
        self.worker_pr_config = validate_worker_pr_config(worker_pr_config)
        self.profile = profile
        super().__init__(*args, **kwargs)

    def _approval_snapshot(self, number, expected=None):
        if self.enabled is not True or not self.worker_pr_config['enabled']:
            raise ContractError('direct PR deployment disabled')
        receipt = inspect_once(self.issues, self.comments, self.state.intake, number, self.clock)
        issue = self.issues.get_issue(number)
        status, request = classify(issue, self.clock())
        if status == 'EXPIRED':
            raise RecheckRejected('expired')
        if status != 'PENDING_TRUSTED_APPROVAL' or content_hash(issue) != receipt['content_sha256']:
            raise RecheckRejected('changed')
        plan = validate_goal_plan(request['plan'], self.source_sha,
            resource_limits=self.resource_limits, deployment_id=self.deployment_id)
        if (plan['kind'] != DIRECT_PR_KIND or plan['direct_pr']['issue_number'] != number
                or plan['model'] != self.profile['model']):
            raise ContractError('direct PR request does not match execution Issue/profile')
        require_installed_goal(self.profile, plan)
        binding = goal_binding(issue, request, self.source_sha,
            resource_limits=self.resource_limits, deployment_id=self.deployment_id)
        if expected is not None and expected != binding:
            raise RecheckRejected('changed')
        return binding, plan, timestamp(request['expires_at'])

    def process(self, number):
        if not self.enabled or not self.worker_pr_config['enabled']:
            return dict(issue=number, phase='DIRECT_PR_DISABLED', dispatched=False)
        old = self.state.get(number)
        if old and old['phase'] == 'CLAIMED':
            self.state.interrupt_after_restart(number, self.runtime.current_epoch(),
                deployment_contract_verified=self.runtime.deployment_contract_verified)
            old = self.state.get(number)
        if old:
            # Never rerun on a missing PR, lost final message or notification.
            report = dict(issue=number, phase=old['phase'], dispatched=False, duplicate=True,
                          notification='EXTERNAL_PR_EVENT', research_success_verified=False)
            if old['phase'] == 'WORKER_FINISHED':
                result = DirectPRResult.from_dict(old['result'])
                report.update(pr_url=result.pr_url, pr_verified=False, outcome=result.outcome)
            return report
        binding, plan = self._approved(number)
        self.runtime.prepare_plan(plan)
        epoch = self.runtime.preflight()
        if type(epoch) is not RestartEpoch:
            raise ContractError('verified dedicated namespace required')
        self.approve(number, binding)
        if not self.state.claim(binding, epoch=epoch):
            raise ContractError('concurrent direct PR claim rejected')
        self.generation_attempted = True
        try:
            recheck = _RunApprovalRecheck(lambda: self._approval_snapshot(number, binding)[2],
                                         clock=self.clock, monotonic=self.monotonic)
            try:
                result = self.runtime.run(binding, recheck)
            finally:
                recheck.close()
            if type(result) is not DirectPRResult or result.binding != binding or result.model != plan['model']:
                raise ContractError('worker completion binding mismatch')
            self.state.save_direct_result(number, result)
            return dict(issue=number, phase='WORKER_FINISHED', dispatched=True, duplicate=False,
                        restart_required=True, pr_url=result.pr_url, pr_verified=False,
                        outcome=result.outcome, research_success_verified=False,
                        notification='EXTERNAL_PR_EVENT')
        except BaseException:
            self.state.block(number)
            raise
