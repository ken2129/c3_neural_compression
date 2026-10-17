"""Fixed manager/root pipe protocol; no worker-selected command or public output."""
import subprocess
from urllib.error import HTTPError, URLError
from weakref import WeakKeyDictionary

from .manager_github import ManagerGitHubHTTPError, ManagerGitHubRateLimited
from .owner_approval import MissingApproval, RevokedApproval
from .restart_boundary import RestartEpoch
from .result_publisher import ValidatedCanaryResult, SCHEMA
from .schema import ContractError, canonical, loads, fields

LIMIT=16384
RECHECK_REASONS=frozenset(('expired','changed','source_rate_limited',
    'source_timeout','source_unavailable','source_command_failed','contract',
    'unknown','clock_anomaly','refresh_stale','inactive_snapshot'))
RECHECK_ORIGINS=frozenset(('manager','launcher_ipc','approval_callback'))
_RECHECK_FAILURES=WeakKeyDictionary()


def _valid_recheck_record(record):
    return (type(record) is tuple and len(record)==2
        and type(record[0]) is str and record[0] in RECHECK_REASONS
        and type(record[1]) is str and record[1] in RECHECK_ORIGINS)


class RecheckRejected(ContractError):
    """Closed diagnostic only; arbitrary exception attributes are not trusted."""
    def __init__(self,reason,*,origin='manager'):
        super().__init__('approval recheck rejected')
        record=(reason,origin)
        if type(self) is RecheckRejected:
            _RECHECK_FAILURES[self]=record if _valid_recheck_record(record) else ('unknown','manager')


def safe_recheck_failure(exc,*,origin='manager'):
    """Return fresh, allowlisted diagnostics without exception text or payloads.

    Only an exact RecheckRejected with a valid private immutable record carries
    an explicit reason. Public attributes, args, subclasses and chained errors
    cannot manufacture that record or contribute diagnostic text.
    """
    fallback=origin if type(origin) is str and origin in RECHECK_ORIGINS else 'manager'
    reason='unknown'
    kind=type(exc)
    if kind is RecheckRejected:
        record=_RECHECK_FAILURES.get(exc)
        if _valid_recheck_record(record):
            return {'reason':record[0],'origin':record[1]}
    elif issubclass(kind,RecheckRejected):
        pass
    elif type(origin) is not str or origin not in RECHECK_ORIGINS:
        pass
    elif kind is ManagerGitHubRateLimited:
        reason='source_rate_limited'
    elif kind in (HTTPError,ManagerGitHubHTTPError):
        # Status is numeric metadata. Never inspect headers, URLs or bodies.
        attributes=object.__getattribute__(exc,'__dict__')
        if type(attributes) is dict:
            status=attributes.get('code')
            reason='source_rate_limited' if type(status) is int and status==429 else 'source_unavailable'
    elif issubclass(kind,HTTPError):
        # Unknown subclasses may override accessors. Never inspect them.
        pass
    elif issubclass(kind,(TimeoutError,subprocess.TimeoutExpired)):
        reason='source_timeout'
    elif issubclass(kind,(URLError,OSError)):
        reason='source_unavailable'
    elif issubclass(kind,subprocess.CalledProcessError):
        reason='source_command_failed'
    elif issubclass(kind,(MissingApproval,RevokedApproval)):
        reason='changed'
    elif issubclass(kind,ContractError):
        reason='contract'
    return {'reason':reason,'origin':fallback}


def require_recheck_reply(response):
    """Accept only the exact positive frame or a closed negative diagnostic."""
    if type(response) is dict:
        if set(response)=={'approved'} and response['approved'] is True:
            return
        if (set(response)=={'approved','reason','origin'}
                and response['approved'] is False
                and _valid_recheck_record((response['reason'],response['origin']))):
            raise RecheckRejected(response['reason'],origin=response['origin'])
    raise RecheckRejected('contract',origin='launcher_ipc')

def read_frame(stream):
    line=stream.readline(LIMIT+1)
    if not line or len(line.encode('utf-8'))>LIMIT or not line.endswith('\n'):
        raise ContractError('invalid supervisor frame')
    value=loads(line)
    if type(value) is not dict:raise ContractError('object frame required')
    return value


def write_frame(stream,value):
    if type(value) is not dict:raise ContractError('object frame required')
    encoded=canonical(value).decode()+'\n'
    if len(encoded.encode())>LIMIT:raise ContractError('oversized supervisor frame')
    stream.write(encoded);stream.flush()


class PipeRuntime:
    """Only the protected manager receives these private launcher pipes.

    This runtime runs the fixed literal CPU child, not a Codex/model process.
    The launcher must have independently verified its deployment manifest.
    """
    deployment_contract_verified=True

    def __init__(self,incoming,outgoing,epoch):
        if type(epoch) is not RestartEpoch:raise ContractError('launcher epoch required')
        self.incoming=incoming;self.outgoing=outgoing;self.epoch=epoch;self.plan=None

    def current_epoch(self):return self.epoch

    def prepare_plan(self,plan):
        write_frame(self.outgoing,{'op':'prepare_plan','plan':plan})
        response=read_frame(self.incoming)
        if set(response)!={'prepared'} or response['prepared'] is not True:raise ContractError('runtime plan preparation rejected')
        self.plan=dict(plan)

    def preflight(self):
        write_frame(self.outgoing,{'op':'preflight'})
        response=read_frame(self.incoming)
        if set(response)!={'ready'} or response['ready'] is not True:raise ContractError('supervisor admission failed')
        return self.epoch

    def run(self,binding,recheck):
        from .service_state import validate_binding
        validate_binding(binding)
        write_frame(self.outgoing,{'op':'fixed_model' if self.plan and self.plan['kind']=='codex-canary-v1' else 'fixed_cpu','binding':binding})
        while True:
            frame=read_frame(self.incoming)
            if frame=={'op':'recheck'}:
                try:recheck()
                except BaseException as exc:
                    write_frame(self.outgoing,dict(approved=False,**safe_recheck_failure(exc)));raise
                write_frame(self.outgoing,{'approved':True})
                continue
            if self.plan and self.plan['kind']=='codex-canary-v1':
                from .model_result import ModelCanaryResult
                fields(frame,('model_result',))
                expected={key:binding[key] for key in ('request_id','run_id','issue_number','plan_digest','source_sha')}
                return ModelCanaryResult.from_bytes(canonical(frame['model_result']),model=self.plan['model'],**expected)
            fields(frame,('result',))
            data=frame['result'];fields(data,('checksum','elapsed_seconds','model_called'))
            if data['model_called'] is not False:raise ContractError('unsupported model result')
            expected={key:binding[key] for key in ('request_id','run_id','issue_number','plan_digest','source_sha')}
            artifact=dict(schema=SCHEMA,**expected,outcome='success',checksum=data['checksum'],elapsed_seconds=data['elapsed_seconds'])
            return ValidatedCanaryResult.from_bytes(canonical(artifact),**expected)
