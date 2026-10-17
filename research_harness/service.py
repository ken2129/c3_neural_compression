"""Bounded CPU-canary controller. Approval, durable claim and publication connect here.

The service accepts no arbitrary commands, repositories, GPU jobs or merges.
Runtime and publisher are trusted manager code, never Issue-selected plugins.
"""
import hashlib
import math
import re
import time
from urllib.error import HTTPError, URLError
from .issue_intake import classify, content_hash, positive_int, timestamp, MAX_TRACKED, SCAN_SECONDS
from .manager_runtime import RecheckRejected, safe_recheck_failure
from .owner_approval import inspect_once, approval_read_budget
from .model_result import ModelCanaryResult
from .restart_boundary import RestartEpoch
from .result_publisher import (PublicationRequest, ModelPublicationRequest,
                               PublicationState, ValidatedCanaryResult,
                               parse_publication_request)
from .schema import ContractError, canonical, fields


RUN_APPROVAL_REFRESH_SECONDS=5.0
RUN_APPROVAL_CLOCK_SKEW_SECONDS=1.0


class _RunApprovalRecheck:
    """One run's protected, short-lived approval snapshot; never a permission.

    Every callback checks local wall/monotonic expiry. Remote approval refresh
    starts at most five seconds after the preceding refresh started, and a
    refresh that returns after that freshness window is rejected. A shared 5s
    source-read budget bounds the whole refresh; failing subprocess cleanup can
    take up to 5s more. Cancellation observation may therefore take the remaining
    cache window plus refresh/cleanup and callback scheduling, not just 5s. The
    dedicated PID1 watchdog remains the hard bound (124s after arming). Ordinary
    fresh pre/post-run and publication approval checks remain uncached.
    """
    def __init__(self,refresh,*,clock,monotonic):
        self.refresh=refresh;self.clock=clock;self.monotonic=monotonic
        self.active=False;self.expires=None;self.monotonic_expiry=None
        self.last_wall=None;self.last_mono=None;self.clock_offset=None;self.next_refresh=None
        if not all(callable(value) for value in (refresh,clock,monotonic)):
            raise RecheckRejected('inactive_snapshot')
        try:
            wall,mono=self._observe()
            self._refresh(wall,mono)
        except BaseException:
            self.close();raise

    def _observe(self):
        wall,mono=self.clock(),self.monotonic()
        if (any(type(value) not in (int,float) or not math.isfinite(value) or value<0
                for value in (wall,mono))
            or self.last_wall is not None and wall<self.last_wall
            or self.last_mono is not None and mono<self.last_mono):
            raise RecheckRejected('clock_anomaly')
        offset=wall-mono
        if self.clock_offset is not None and abs(offset-self.clock_offset)>RUN_APPROVAL_CLOCK_SKEW_SECONDS:
            raise RecheckRejected('clock_anomaly')
        if self.clock_offset is None:self.clock_offset=offset
        self.last_wall=wall;self.last_mono=mono
        return wall,mono

    def _unexpired(self,wall,mono):
        if self.expires is None or self.monotonic_expiry is None:
            raise RecheckRejected('inactive_snapshot')
        if wall>=self.expires or mono>=self.monotonic_expiry:
            raise RecheckRejected('expired')

    def _refresh(self,wall,mono):
        self.active=False
        try:
            with approval_read_budget(seconds=RUN_APPROVAL_REFRESH_SECONDS):
                expires=self.refresh()
        except Exception as exc:
            detail=safe_recheck_failure(exc)
            raise RecheckRejected(detail['reason'],origin=detail['origin']) from None
        after_wall,after_mono=self._observe()
        if type(expires) not in (int,float) or not math.isfinite(expires) or expires<=0:
            raise RecheckRejected('inactive_snapshot')
        if self.expires is not None and expires!=self.expires:
            raise RecheckRejected('changed')
        if after_mono-mono>=RUN_APPROVAL_REFRESH_SECONDS:
            raise RecheckRejected('refresh_stale')
        # Never extend lifetime if wall time stalls while monotonic time moves.
        expiry=mono+(expires-wall)
        self.monotonic_expiry=expiry if self.monotonic_expiry is None else min(self.monotonic_expiry,expiry)
        self.expires=expires
        self._unexpired(after_wall,after_mono)
        self.next_refresh=mono+RUN_APPROVAL_REFRESH_SECONDS
        self.active=True

    def __call__(self):
        if not self.active:raise RecheckRejected('inactive_snapshot')
        try:
            wall,mono=self._observe()
            self._unexpired(wall,mono)
            if mono>=self.next_refresh:self._refresh(wall,mono)
        except BaseException:
            self.close();raise

    def close(self):
        self.active=False



def source_failure_report(exc):
    """Fixed fields only; never expose remote bodies, URLs or exception strings."""
    report=dict(phase='SOURCE_UNAVAILABLE',dispatched=False,error_category='OTHER')
    if isinstance(exc,HTTPError):
        report['error_category']='HTTP_ERROR'
        if type(exc.code) is int and 100<=exc.code<=599:report['http_status']=exc.code
        headers=exc.headers
        if headers is not None:
            for header,key in (('X-RateLimit-Remaining','rate_remaining'),('X-RateLimit-Reset','rate_reset_unix')):
                value=headers.get(header)
                if type(value) is str and value.isascii() and value.isdecimal() and len(value)<=12:
                    report[key]=int(value)
    elif isinstance(exc,(TimeoutError,)):
        report['error_category']='TIMEOUT'
    elif isinstance(exc,URLError):report['error_category']='TRANSPORT_ERROR'
    elif isinstance(exc,ContractError):report['error_category']='SOURCE_CONTRACT_ERROR'
    return report


def source_retry_delay(reports, *, now, minimum=60):
    """Wait out a bounded GitHub rate-reset timestamp; never spin on 403/429."""
    delay=minimum
    for report in reports:
        if report.get('phase')!='SOURCE_UNAVAILABLE':continue
        if report.get('http_status') in (403,429):
            delay=max(delay,300)
            reset=report.get('rate_reset_unix')
            if type(reset) is int and now<reset<=now+86400:
                delay=max(delay,reset-now+1)
    return delay

def validate_canary_plan(plan, allowed_source_sha):
    if type(plan) is not dict:raise ContractError('closed canary plan required')
    common=('kind','source_sha','publish_result_pr')
    if plan.get('kind')=='cpu-canary-v1':
        fields(plan,common)
    elif plan.get('kind')=='codex-canary-v1':
        fields(plan,common+('model','sandbox_mode','external_network',
                            'max_seconds','model_usage_approved'))
        if (type(plan['model']) is not str
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',plan['model']) is None
            or plan['model'].endswith('-latest')
            or plan['sandbox_mode']!='externalSandbox'
            or plan['external_network']!='enabled'
            or type(plan['max_seconds']) is not int or plan['max_seconds']!=120
            or plan['model_usage_approved'] is not True):
            raise ContractError('explicit fixed model canary policy required')
    else:
        raise ContractError('unsupported canary kind')
    if plan['publish_result_pr'] is not True:
        raise ContractError('approved result PR required')
    if (type(allowed_source_sha) is not str or len(allowed_source_sha)!=40
        or any(c not in '0123456789abcdef' for c in allowed_source_sha)
        or plan['source_sha']!=allowed_source_sha):
        raise ContractError('source is not the operator-installed approved revision')
    return dict(plan)


def canary_binding(issue, request, allowed_source_sha):
    plan=validate_canary_plan(request['plan'],allowed_source_sha)
    number=positive_int(issue['number'])
    if number==56:raise ContractError('design Issue cannot execute')
    digest=hashlib.sha256(canonical(plan)).hexdigest()
    content=content_hash(issue)
    return dict(issue_number=number,issue_content_sha256=content,
                request_id=request['approval_ref'],run_id=f'canary-{number}-{content[:16]}',
                plan_digest=digest,source_sha=allowed_source_sha)


def _require_result(result,binding,plan):
    model_plan=plan['kind']=='codex-canary-v1'
    expected_type=ModelCanaryResult if model_plan else ValidatedCanaryResult
    if type(result) is not expected_type:
        raise ContractError('runtime result kind differs from approved canary')
    if model_plan and result.model!=plan['model']:
        raise ContractError('runtime model differs from approved canary')
    for key in ('request_id','run_id','issue_number','plan_digest','source_sha'):
        if getattr(result,key)!=binding[key]:raise ContractError('runtime result binding mismatch')


class Controller:
    """ServiceState holds one private-state lock for the entire controller lifetime.

    runtime.preflight must perform actual checks, then runtime.run owns bounded
    launch/cleanup and invokes recheck during a run. A boolean from an Issue is
    never passed as a runtime permission. Tests use injected fake implementations.
    """
    def __init__(self,state,issues,comments,runtime,publisher_factory,*,source_sha,
                 publication_base_getter=None,clock=time.time,monotonic=time.monotonic):
        self.state=state;self.issues=issues;self.comments=comments
        self.runtime=runtime;self.publisher_factory=publisher_factory
        self.source_sha=source_sha;self.clock=clock;self.monotonic=monotonic
        self.publication_base_getter=publication_base_getter
        self.generation_attempted=False

    def _approval_snapshot(self,number,expected=None):
        receipt=inspect_once(self.issues,self.comments,self.state.intake,number,self.clock)
        issue=self.issues.get_issue(number)
        status,request=classify(issue,self.clock())
        if status=='EXPIRED':raise RecheckRejected('expired')
        if status!='PENDING_TRUSTED_APPROVAL' or content_hash(issue)!=receipt['content_sha256']:
            raise RecheckRejected('changed')
        binding=canary_binding(issue,request,self.source_sha)
        if expected is not None and binding!=expected:raise RecheckRejected('changed')
        return binding,validate_canary_plan(request['plan'],self.source_sha),timestamp(request['expires_at'])

    def _approved(self,number,expected=None):
        try:
            binding,plan,_=self._approval_snapshot(number,expected)
        except Exception as exc:
            detail=safe_recheck_failure(exc)
            raise RecheckRejected(detail['reason'],origin=detail['origin']) from None
        return binding,plan

    def approve(self,number,expected=None):
        return self._approved(number,expected)[0]

    def approved_plan(self,number,binding):
        # Fresh owner receipt and full Issue binding, never title-selected policy.
        return self._approved(number,binding)[1]

    @staticmethod
    def _interrupted_report(number):
        return dict(issue=number,phase='INTERRUPTED',dispatched=False,duplicate=True,
                    notification='UNCONFIGURED',historical=True,outcome='unknown')

    def recover_interrupted_claims(self):
        """Reconcile only local claims, before remote discovery or admission.

        This explicit pass uses the trusted runtime epoch and unchanged restart
        contract. It never calls process(), approval, preflight or publication.
        Merely constructing/opening ServiceState still performs no recovery.
        """
        numbers=self.state.pending_claims()
        current_epoch=getattr(self.runtime,'current_epoch',None)
        if not numbers or not callable(current_epoch):return []
        epoch=current_epoch()
        verified=getattr(self.runtime,'deployment_contract_verified',False)
        reports=[]
        for number in numbers:
            if self.state.interrupt_after_restart(number,epoch,deployment_contract_verified=verified):
                reports.append(self._interrupted_report(number))
        return reports

    def process(self,number):
        old=self.state.get(number)
        if old and old['phase']=='CLAIMED':
            current_epoch=getattr(self.runtime,'current_epoch',None)
            if callable(current_epoch):
                self.state.interrupt_after_restart(number,current_epoch(),
                    deployment_contract_verified=getattr(self.runtime,'deployment_contract_verified',False))
                old=self.state.get(number)
        if old and old['phase']=='INTERRUPTED':
            # Historical status needs no new execution/publication approval. A
            # changed epoch proves interruption under the deployment contract,
            # not whether the former worker started or completed its work.
            return self._interrupted_report(number)
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
            request=parse_publication_request(old['result'])
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
            plan=self.approved_plan(number,binding)
        else:
            # Missing runtime setup does not consume a durable execution claim.
            plan=self.approved_plan(number,binding)
            # Pin main through a trusted fixed-repository reader before the
            # runtime/watchdog or durable claim. The execution source remains
            # owner-approved source_sha; saved runs retain their original base.
            if not callable(self.publication_base_getter):
                raise ContractError('trusted publication base reader required')
            publication_base_sha=self.publication_base_getter()
            if (type(publication_base_sha) is not str
                    or re.fullmatch(r'[0-9a-f]{40}',publication_base_sha) is None):
                raise ContractError('full publication base commit required')
            model_plan=plan['kind']=='codex-canary-v1'
            prepare=getattr(self.runtime,'prepare_plan',None)
            if model_plan and not callable(prepare):
                raise ContractError('model deployment admission is not configured')
            if callable(prepare):prepare(dict(plan))
            epoch=self.runtime.preflight()
            if model_plan and type(epoch) is not RestartEpoch:
                raise ContractError('model canary requires a verified restart epoch')
            self.approve(number,binding)
            if not self.state.claim(binding,epoch=epoch):raise ContractError('concurrent claim rejected')
            try:
                recheck=_RunApprovalRecheck(lambda:self._approval_snapshot(number,binding)[2],
                    clock=self.clock,monotonic=self.monotonic)
                dispatched=True
                self.generation_attempted=True
                try:
                    result=self.runtime.run(binding,recheck)
                finally:
                    recheck.close()
                _require_result(result,binding,plan)
                if model_plan:
                    # Fresh, uncached approval within the model finalization
                    # reserve; one shared budget covers every source read.
                    with approval_read_budget(seconds=5.0):
                        self.approve(number,binding)
                else:
                    self.approve(number,binding)
                request_type=ModelPublicationRequest if model_plan else PublicationRequest
                request=request_type(result,publication_base_sha,binding['issue_content_sha256'])
                self.state.save_result(number,request,await_restart=epoch is not None)
                if epoch is not None:
                    return dict(issue=number,phase='RESULT_AWAITING_RESTART',dispatched=True,
                                duplicate=False,notification='UNCONFIGURED',restart_required=True)
            except BaseException:
                self.state.block(number)
                raise
            old=self.state.get(number)
        request=parse_publication_request(old['result'])
        _require_result(request.result,binding,plan)
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
        reports=self.recover_interrupted_claims()
        recovered={report['issue'] for report in reports}
        try:
            candidates=self.issues.list_candidates()
        except Exception as exc:
            # A remote outage must not hide a completed local transition. It
            # still blocks every new admission and publication in this scan.
            if not reports:raise
            return reports+[source_failure_report(exc)]
        numbers=set(candidates)|set(self.state.intake.tracked())
        if len(numbers)>MAX_TRACKED:raise ContractError('tracked Issue limit reached')
        # A failed complete listing aborts before any dispatch; each known Issue
        # is still re-read, including cancellation/removal of its title marker.
        for number in sorted(numbers-recovered):
            if time.monotonic()-started>=SCAN_SECONDS:
                reports.append(dict(phase='SCAN_DEADLINE',dispatched=False));break
            try:
                report=self.process(number);reports.append(report)
                if report.get('restart_required'):break
            except Exception as exc:
                saved=self.state.get(number)
                report=dict(issue=number,phase=saved['phase'] if saved else 'BLOCKED_OR_UNCONFIGURED',
                                    dispatched='unknown' if self.generation_attempted else False,
                                    notification='UNCONFIGURED')
                if type(exc) is RecheckRejected:report['approval_failure']=safe_recheck_failure(exc)
                reports.append(report)
                if self.generation_attempted:break
        return reports

    def poll(self,*,stop,interval=60,sleep=time.sleep,emit=lambda report:None):
        if type(interval) is not int or not 30<=interval<=3600:
            raise ContractError('poll interval must be 30..3600 seconds')
        while not stop():
            try:reports=self.once()
            except Exception as exc:reports=[source_failure_report(exc)]
            emit(reports)
            if not stop():sleep(source_retry_delay(reports,now=self.clock(),minimum=interval))
