"""Goal-only controller using existing owner intake, durable claims and epochs.

A distinct deployment opt-in is required. A canary approval or successful smoke
never enables this controller. Model candidates and workspace changes remain
private until a separate approval of the exact resulting content is present.
"""
import hashlib
import re
import time

from .schema import ContractError, canonical
from .issue_intake import classify, content_hash, positive_int, timestamp
from .owner_approval import inspect_once, approval_read_budget
from .manager_runtime import RecheckRejected
from .restart_boundary import RestartEpoch
from .service import Controller, _RunApprovalRecheck
from .goal_plan import validate_goal_plan


def goal_binding(issue, request, source_sha, *, resource_limits=None, deployment_id=None):
    plan = validate_goal_plan(request['plan'], source_sha, resource_limits=resource_limits, deployment_id=deployment_id)
    number = positive_int(issue['number'])
    if number == 56:
        raise ContractError('design Issue cannot execute')
    content = content_hash(issue)
    return dict(issue_number=number, issue_content_sha256=content,
        request_id=request['approval_ref'], run_id=f'goal-{number}-{content[:16]}',
        plan_digest=hashlib.sha256(canonical(plan)).hexdigest(), source_sha=source_sha)


class GoalController(Controller):
    """Same durable once-only claim model; independently approved publications.

    `runtime` must enforce goal-specific mounts, requested resources and owned
    process lifetime. `enabled` alone proves none of those facts. The concrete
    runtime performs actual preflight before the first durable claim.
    """
    def __init__(self, *args, enabled=False, resource_limits=None,
                 content_authorizer=None, deployment_id=None, installed_plan_digest=None, **kwargs):
        super().__init__(*args, **kwargs)
        if enabled is True and (type(deployment_id) is not str or re.fullmatch('[0-9a-f]{32}', deployment_id) is None):
            raise ContractError('explicit goal deployment identity required')
        if enabled is True and (type(installed_plan_digest) is not str or re.fullmatch('[0-9a-f]{64}', installed_plan_digest) is None):
            raise ContractError('explicit installed goal plan required')
        self.installed_plan_digest = installed_plan_digest
        self.enabled = enabled
        self.deployment_id = deployment_id
        self.resource_limits = resource_limits
        self.content_authorizer = content_authorizer

    def _approval_snapshot(self, number, expected=None):
        if self.enabled is not True or self.resource_limits is None:
            raise ContractError('goal deployment is disabled or unconfigured')
        receipt = inspect_once(self.issues, self.comments, self.state.intake, number, self.clock)
        issue = self.issues.get_issue(number)
        status, request = classify(issue, self.clock())
        if status == 'EXPIRED':
            raise RecheckRejected('expired')
        if status != 'PENDING_TRUSTED_APPROVAL' or content_hash(issue) != receipt['content_sha256']:
            raise RecheckRejected('changed')
        binding = goal_binding(issue, request, self.source_sha, resource_limits=self.resource_limits, deployment_id=self.deployment_id)
        if binding['plan_digest'] != self.installed_plan_digest:
            raise RecheckRejected('changed')
        if expected is not None and expected != binding:
            raise RecheckRejected('changed')
        return (binding, validate_goal_plan(request['plan'], self.source_sha,
                    resource_limits=self.resource_limits, deployment_id=self.deployment_id), timestamp(request['expires_at']))

    def _saved_result(self, number, old):
        from .goal_result import GoalPublicationRequest
        from .goal_publisher import GoalPublicationState
        request = GoalPublicationRequest.from_dict(old['result'])
        # Publication permission is separate and may be granted after the
        # execution deadline. The original Issue content must still match.
        issue = self.issues.get_issue(number)
        if (issue.get('state') != 'open' or content_hash(issue) != old['binding']['issue_content_sha256']):
            raise RecheckRejected('changed')
        prior = GoalPublicationState.from_dict(old['publication']) if old['publication'] else None
        if not any((change.before is not None and change.before.kind == 'file')
                   or (change.after is not None and change.after.kind == 'file')
                   for change in request.result.candidate.changes):
            return dict(issue=number, phase='NO_FILE_CHANGES', dispatched=False,
                        duplicate=True, notification='UNCONFIGURED',
                        research_success_verified=False)
        def authorize():
            current = self.issues.get_issue(number)
            if current.get('state') != 'open' or content_hash(current) != request.issue_content_sha256:
                raise RecheckRejected('changed')
            if not callable(self.content_authorizer):
                return False
            return self.content_authorizer(request) is True
        allowed = authorize()
        if not allowed and prior is None:
            return dict(issue=number, phase='AWAITING_CONTENT_REVIEW',
                        dispatched=False, duplicate=True, notification='UNCONFIGURED',
                        request_digest=request.digest,
                        research_success_verified=False)
        publisher = self.publisher_factory(lambda value:self.state.save_publication(number,value))
        final = publisher.publish(request, state=prior, authorized=allowed, authorize_callback=authorize)
        self.state.save_publication(number, final)
        return dict(issue=number, phase=self.state.get(number)['phase'], dispatched=False,
                    duplicate=True, notification='UNCONFIGURED', pr_url=final.url,
                    publication_phase=final.phase, publication_reason=final.reason)

    def process(self, number):
        if self.enabled is not True or self.resource_limits is None:
            return dict(issue=number,phase='GOAL_DISABLED',dispatched=False,notification='UNCONFIGURED')
        from .goal_result import GoalResult, GoalPublicationRequest
        from .goal_publisher import GoalPublicationState
        old = self.state.get(number)
        if old and old['phase'] == 'CLAIMED':
            self.state.interrupt_after_restart(number, self.runtime.current_epoch(),
                deployment_contract_verified=self.runtime.deployment_contract_verified)
            old = self.state.get(number)
        if old and old['phase'] == 'INTERRUPTED':
            return self._interrupted_report(number)
        if old and old['phase'] == 'RESULT_AWAITING_RESTART':
            self.state.confirm_restart(number, self.runtime.current_epoch(),
                deployment_contract_verified=self.runtime.deployment_contract_verified)
            old = self.state.get(number)
        if old and old['phase'] == 'PUBLISHED':
            published = GoalPublicationState.from_dict(old['publication'])
            return dict(issue=number, phase='PUBLISHED', dispatched=False, duplicate=True,
                        notification='UNCONFIGURED', pr_url=published.url, historical=True)
        if old and old['phase'] == 'RESULT_READY':
            return self._saved_result(number, old)
        if old:
            # A blocked/uncertain claim is never reopened, even if a later
            # comment asks to execute it again. A new approved request is needed.
            return dict(issue=number, phase=old['phase'], dispatched=False,
                        duplicate=True, notification='UNCONFIGURED')
        binding, plan = self._approved(number)
        if not callable(self.publication_base_getter):
            raise ContractError('trusted publication base reader required')
        base = self.publication_base_getter()
        if type(base) is not str or len(base) != 40 or any(c not in '0123456789abcdef' for c in base):
            raise ContractError('invalid publication base')
        self.runtime.prepare_plan(plan)
        epoch = self.runtime.preflight()
        if type(epoch) is not RestartEpoch:
            raise ContractError('goal execution requires verified restart epoch')
        self.approve(number, binding)
        if not self.state.claim(binding, epoch=epoch):
            raise ContractError('concurrent goal claim rejected')
        self.generation_attempted = True
        try:
            recheck = _RunApprovalRecheck(lambda:self._approval_snapshot(number,binding)[2],
                                        clock=self.clock, monotonic=self.monotonic)
            try:
                result = self.runtime.run(binding, recheck)
            finally:
                recheck.close()
            if type(result) is not GoalResult or result.model != plan['model']:
                raise ContractError('goal result type or model mismatch')
            for key in ('request_id','run_id','issue_number','plan_digest','source_sha'):
                if getattr(result,key) != binding[key]:
                    raise ContractError('goal result binding mismatch')
            with approval_read_budget(seconds=5):
                self.approve(number,binding)
            request = GoalPublicationRequest(result,base,binding['issue_content_sha256'])
            self.state.save_result(number,request,await_restart=True)
            return dict(issue=number,phase='RESULT_AWAITING_RESTART',dispatched=True,
                        duplicate=False,restart_required=True,notification='UNCONFIGURED')
        except BaseException:
            self.state.block(number)
            raise
