"""Bounded CPU-canary controller. Approval, durable claim and publication connect here.

The service accepts no arbitrary commands, repositories, GPU jobs or merges.
Runtime and publisher are trusted manager code, never Issue-selected plugins.
"""
import hashlib
import time
from .issue_intake import classify, content_hash, positive_int, MAX_TRACKED, SCAN_SECONDS
from .owner_approval import inspect_once
from .result_publisher import PublicationRequest, PublicationState, ValidatedCanaryResult
from .schema import ContractError, canonical, fields


def canary_binding(issue, request, allowed_source_sha):
    plan=request['plan']
    fields(plan, ('kind','source_sha','publish_result_pr'))
    if plan['kind']!='cpu-canary-v1' or plan['publish_result_pr'] is not True:
        raise ContractError('only approved fixed CPU canary and result PR supported')
    if (type(allowed_source_sha) is not str or len(allowed_source_sha)!=40
        or any(c not in '0123456789abcdef' for c in allowed_source_sha)
        or plan['source_sha']!=allowed_source_sha):
        raise ContractError('source is not the operator-installed approved revision')
    number=positive_int(issue['number'])
    if number==56:raise ContractError('design Issue cannot execute')
    digest=hashlib.sha256(canonical(plan)).hexdigest()
    content=content_hash(issue)
    return dict(issue_number=number,issue_content_sha256=content,
                request_id=request['approval_ref'],run_id=f'canary-{number}-{content[:16]}',
                plan_digest=digest,source_sha=allowed_source_sha)


class Controller:
    """ServiceState holds one private-state lock for the entire controller lifetime.

    runtime.preflight must perform actual checks, then runtime.run owns bounded
    launch/cleanup and invokes recheck during a run. A boolean from an Issue is
    never passed as a runtime permission. Tests use injected fake implementations.
    """
    def __init__(self,state,issues,comments,runtime,publisher_factory,*,source_sha,clock=time.time):
        self.state=state;self.issues=issues;self.comments=comments
        self.runtime=runtime;self.publisher_factory=publisher_factory
        self.source_sha=source_sha;self.clock=clock
        self.generation_attempted=False

    def approve(self,number,expected=None):
        receipt=inspect_once(self.issues,self.comments,self.state.intake,number,self.clock)
        issue=self.issues.get_issue(number)
        status,request=classify(issue,self.clock())
        if status!='PENDING_TRUSTED_APPROVAL' or content_hash(issue)!=receipt['content_sha256']:
            raise ContractError('approval changed before controller admission')
        binding=canary_binding(issue,request,self.source_sha)
        if expected is not None and binding!=expected:raise ContractError('approved binding changed')
        return binding

    def process(self,number):
        old=self.state.get(number)
        if old and old['phase']=='RESULT_AWAITING_RESTART':
            self.state.confirm_restart(number,self.runtime.current_epoch(),
                deployment_contract_verified=self.runtime.deployment_contract_verified)
            old=self.state.get(number)
        if old and old['phase']=='PUBLISHED':
            published=PublicationState.from_dict(old['publication'])
            return dict(issue=number,phase='PUBLISHED',dispatched=False,duplicate=True,
                        notification='UNCONFIGURED',pr_url=published.url,historical=True)
        try:
            binding=self.approve(number)
        except Exception:
            if not old or old['phase']!='RESULT_READY' or not old['publication']:
                raise
            prior=PublicationState.from_dict(old['publication'])
            if prior.phase not in ('BRANCH_UNKNOWN','CONTENT_UNKNOWN','PR_UNKNOWN'):
                raise
            # Resolve an already-attempted write without authorizing another.
            request=PublicationRequest.from_dict(old['result'])
            publisher=self.publisher_factory(lambda value:self.state.save_publication(number,value))
            final=publisher.publish(request,state=prior,authorized=False,authorize_callback=lambda:False)
            self.state.save_publication(number,final)
            return dict(issue=number,phase=self.state.get(number)['phase'],dispatched=False,
                        duplicate=True,notification='UNCONFIGURED',pr_url=final.url,
                        publication_phase=final.phase,publication_reason=final.reason)
        dispatched=False
        if old:
            if old['binding']!=binding:raise ContractError('claimed plan changed')
            if old['phase'] in ('CLAIMED','BLOCKED','PUBLISHED'):
                return dict(issue=number,phase=old['phase'],dispatched=False,duplicate=True,
                            notification='UNCONFIGURED')
        else:
            # Missing runtime setup does not consume a durable execution claim.
            epoch=self.runtime.preflight()
            self.approve(number,binding)
            if not self.state.claim(binding,epoch=epoch):raise ContractError('concurrent claim rejected')
            try:
                self.approve(number,binding)
                dispatched=True
                self.generation_attempted=True
                result=self.runtime.run(binding,lambda:self.approve(number,binding))
                if type(result) is not ValidatedCanaryResult:
                    raise ContractError('runtime did not return validated canary result')
                for key in ('request_id','run_id','issue_number','plan_digest','source_sha'):
                    if getattr(result,key)!=binding[key]:raise ContractError('runtime result binding mismatch')
                self.approve(number,binding)
                request=PublicationRequest(result,self.source_sha,binding['issue_content_sha256'])
                self.state.save_result(number,request,await_restart=epoch is not None)
                if epoch is not None:
                    return dict(issue=number,phase='RESULT_AWAITING_RESTART',dispatched=True,
                                duplicate=False,notification='UNCONFIGURED',restart_required=True)
            except BaseException:
                self.state.block(number)
                raise
            old=self.state.get(number)
        request=PublicationRequest.from_dict(old['result'])
        publication=PublicationState.from_dict(old['publication']) if old['publication'] else None
        # Publication may be reconciled on restart; computation is never repeated.
        self.approve(number,binding)
        publisher=self.publisher_factory(lambda value:self.state.save_publication(number,value))
        final=publisher.publish(request,state=publication,authorized=True,
                                authorize_callback=lambda:bool(self.approve(number,binding)))
        self.state.save_publication(number,final)
        return dict(issue=number,phase=self.state.get(number)['phase'],dispatched=dispatched,
                    duplicate=not dispatched,notification='UNCONFIGURED',pr_url=final.url,publication_phase=final.phase,publication_reason=final.reason)

    def once(self):
        started=time.monotonic()
        candidates=self.issues.list_candidates()
        numbers=set(candidates)|set(self.state.intake.tracked())
        if len(numbers)>MAX_TRACKED:raise ContractError('tracked Issue limit reached')
        # A failed complete listing aborts before any dispatch; each known Issue
        # is still re-read, including cancellation/removal of its title marker.
        reports=[]
        for number in sorted(numbers):
            if time.monotonic()-started>=SCAN_SECONDS:
                reports.append(dict(phase='SCAN_DEADLINE',dispatched=False));break
            try:
                report=self.process(number);reports.append(report)
                if report.get('restart_required'):break
            except Exception:
                saved=self.state.get(number)
                reports.append(dict(issue=number,phase=saved['phase'] if saved else 'BLOCKED_OR_UNCONFIGURED',
                                    dispatched='unknown' if self.generation_attempted else False,
                                    notification='UNCONFIGURED'))
                if self.generation_attempted:break
        return reports

    def poll(self,*,stop,interval=60,sleep=time.sleep,emit=lambda report:None):
        if type(interval) is not int or not 30<=interval<=3600:
            raise ContractError('poll interval must be 30..3600 seconds')
        while not stop():
            try:emit(self.once())
            except Exception:emit([dict(phase='SOURCE_UNAVAILABLE',dispatched=False)])
            if not stop():sleep(interval)
