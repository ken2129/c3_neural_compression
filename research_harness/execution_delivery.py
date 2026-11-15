"""Per-job trusted Docker broker in the management container; no local Codex."""
import base64
import hashlib
import os
from pathlib import Path
import select
import subprocess
import time

from .direct_pr_service import DirectPRResult
from .execution_contract import installed_container,execution_limits,auth_identity
from .execution_docker import LocalDocker,ExecutionCleanupUnverified
from .goal_plan import validate_goal_plan,direct_pr_scope,require_installed_goal
from .goal_runner import GoalRequest
from .restart_boundary import RestartEpoch
from .schema import ContractError,canonical,loads
from .service_state import validate_binding


class ContainerSession:
    def __init__(self,profile,plan,*,docker=None,clock=time.monotonic):
        self.profile,self.plan,self.clock=profile,plan,clock
        self.target=installed_container(profile,plan)
        self.docker=docker or LocalDocker()
        self.process=None;self.buffer=bytearray();self.executed=False;self.verified=False;self.policy=None

    def send(self,value):
        raw=canonical(value)+b'\n'
        if len(raw)>32768:raise ContractError('execution frame exceeds limit')
        while raw:
            count=self.process.stdin.write(raw)
            if not count:raise ExecutionCleanupUnverified()
            raw=raw[count:]
        self.process.stdin.flush()

    def receive(self,timeout=.2):
        if b'\n' not in self.buffer:
            if not select.select([self.process.stdout],[],[],timeout)[0]:
                if self.process.poll() is not None:raise ExecutionCleanupUnverified()
                return None
            if len(self.buffer)>32768:raise ExecutionCleanupUnverified()
            block=os.read(self.process.stdout.fileno(),min(4096,32769-len(self.buffer)))
            if not block:raise ExecutionCleanupUnverified()
            self.buffer.extend(block)
        if b'\n' not in self.buffer:return None
        raw,_,tail=self.buffer.partition(b'\n');self.buffer=bytearray(tail)
        if not raw or len(raw)>32768:raise ExecutionCleanupUnverified()
        value=loads(bytes(raw))
        if type(value) is not dict:raise ExecutionCleanupUnverified()
        return value

    def prepare(self):
        ident=self.docker.acquire(self.target,self.plan['execution_container'],self.profile['deployment_id'])
        runtime=self.docker.install(ident,self.target['python'],self.profile['runtime_source_sha'])
        self.policy=dict(alias=self.plan['execution_container'],container_id=ident,
            runtime_source_sha=self.profile['runtime_source_sha'],source_sha=self.profile['input_source_sha'],
            deployment_id=self.profile['deployment_id'],scope=direct_pr_scope(self.plan),target=self.target)
        self.process=self.docker.worker(ident,self.target['python'],runtime)
        try:
            self.send(dict(op='prepare',plan=self.plan,policy=self.policy))
            deadline=self.clock()+30
            while self.clock()<deadline:
                frame=self.receive()
                if frame is None:continue
                if set(frame)!={'op','epoch','policy_sha256'} or frame['op']!='ready':
                    raise ContractError('execution preparation rejected')
                epoch=RestartEpoch.from_dict(frame['epoch'])
                if (epoch.container_id!=ident or frame['policy_sha256']!=hashlib.sha256(canonical(self.policy)).hexdigest()):
                    raise ContractError('execution identity/policy mismatch')
                return epoch
            raise ContractError('execution preparation timeout')
        except BaseException:self.close();raise

    def run(self,binding,recheck,credentials,*,persist=lambda value:None,status=lambda phase:None,auth_update=None):
        validate_binding(binding)
        if (self.policy is None or binding['plan_digest']!=GoalRequest(self.plan,self.plan['source_sha']).digest
                or binding['source_sha']!=self.plan['source_sha']):raise ContractError('execution binding mismatch')
        recheck()
        # A partial send might have started the worker; mark it uncertain first.
        self.executed=True
        deadline=self.clock()+self.plan['max_seconds']+15
        aborted=None;cancel_deadline=None;next_lease=0
        try:
            self.send(dict(op='execute',binding=binding,credentials=credentials))
            while True:
                now=self.clock()
                if aborted is None:
                    try:
                        recheck()
                        if now>=deadline:raise TimeoutError('execution deadline exceeded')
                    except BaseException as exc:
                        aborted=exc;cancel_deadline=now+30
                        self.send(dict(op='cancel',binding=binding))
                    else:
                        if now>=next_lease:
                            self.send(dict(op='lease',binding=binding));next_lease=now+2
                if cancel_deadline is not None and now>=cancel_deadline:raise ExecutionCleanupUnverified()
                frame=self.receive()
                if frame is None:continue
                if set(frame)=={'op','binding','auth_state'} and frame['op']=='auth_state':
                    if frame['binding']!=binding or auth_update is None:raise ExecutionCleanupUnverified()
                    raw=base64.b64decode(frame['auth_state'],validate=True)
                    from .model_deployment import validate_chatgpt_auth
                    validate_chatgpt_auth(raw)
                    original=base64.b64decode(credentials['codex_auth'],validate=True)
                    identity=auth_identity(original)
                    if len(raw)>16384 or identity is None or auth_identity(raw)!=identity:
                        raise ExecutionCleanupUnverified()
                    auth_update(raw)
                    self.send(dict(op='auth_state_saved',binding=binding))
                    continue
                if set(frame)=={'op','phase'} and frame['op']=='status':
                    if frame['phase'] not in ('waiting_gpu','codex_running'):raise ExecutionCleanupUnverified()
                    status(frame['phase']);continue
                if (frame.get('binding')!=binding or frame.get('cleanup_verified') is not True
                        or frame.get('op') not in ('worker_result','worker_failure')):
                    raise ExecutionCleanupUnverified()
                if frame['op']=='worker_result':
                    if set(frame)!={'op','binding','cleanup_verified','result'}:raise ExecutionCleanupUnverified()
                    result=DirectPRResult.from_dict(frame['result'])
                    if result.binding!=binding or result.model!=self.plan['model']:raise ExecutionCleanupUnverified()
                elif set(frame)!={'op','binding','cleanup_verified','reason'} or frame['reason']!='EXECUTION_FAILED':
                    raise ExecutionCleanupUnverified()
                self.verified=True;persist(frame)
                if aborted is not None:raise aborted
                if frame['op']=='worker_failure':raise ContractError('execution failed after verified cleanup')
                if self.buffer.strip():raise ContractError('unexpected execution trailing data')
                if self.process.wait(timeout=10)!=0:raise ContractError('execution transport finalization failed')
                if self.process.stdout.read(32769).strip():raise ContractError('unexpected execution trailing data')
                return result
        except BaseException:
            if not self.verified:raise ExecutionCleanupUnverified() from None
            raise
        finally:self.close()

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)
            for stream in (self.process.stdin,self.process.stdout,getattr(self.process,'stderr',None)):
                if stream is not None:stream.close()


def broker(boot,profile,incoming,outgoing,common,*,docker=None):
    """Same manager claim/recheck/failure ACK protocol as the CPU resident path."""
    plan=validate_goal_plan(boot['plan'],profile['input_source_sha'],
        resource_limits=execution_limits(profile,boot['plan']),deployment_id=profile['deployment_id'])
    require_installed_goal(profile,plan)
    session=ContainerSession(profile,plan,docker=docker)
    common.write_frame(outgoing,{'prepared':True})
    ready=attempted=False
    def current_profile():
        try:
            common.as_uid(10001)
            current=common.protected_json('/control/goal-runtime.json')
            if (type(current.get('enabled')) is not bool or
                    canonical({k:v for k,v in current.items() if k!='enabled'})!=
                    canonical({k:v for k,v in profile.items() if k!='enabled'})):
                raise ContractError('execution deployment changed during preparation')
            return current
        finally:common.as_uid(0)
    try:
        while True:
            frame=common.read_frame(incoming)
            if frame=={'op':'preflight'} and not attempted:
                if not current_profile()['enabled']:
                    common.write_frame(outgoing,{'ready':False,'reason':'AUTOMATION_OFF'});continue
                try:session.prepare()
                except Exception:
                    session.close();common.write_frame(outgoing,{'ready':False});continue
                current=current_profile()
                if not current['enabled']:
                    session.close();common.write_frame(outgoing,{'ready':False,'reason':'AUTOMATION_OFF'});continue
                ready=True;common.write_frame(outgoing,{'ready':True})
            elif set(frame)=={'op','binding'} and frame['op']=='goal' and ready and not attempted:
                attempted=True;binding=frame['binding']
                def recheck():
                    common.write_frame(outgoing,{'op':'recheck'})
                    from .manager_runtime import require_recheck_reply
                    require_recheck_reply(common.read_frame(incoming))
                def persist(value):
                    from .persistent_fixture import exclusive_record
                    try:
                        common.as_uid(10001)
                        directory=Path('/control/execution-results');directory.mkdir(mode=0o700,exist_ok=True)
                        common.protected_control_directory(directory)
                        exclusive_record(directory/(binding['run_id']+'.json'),dict(worker_report=value,
                            execution_container=plan['execution_container'],container_id=session.policy['container_id'],
                            runtime_source_sha=profile['runtime_source_sha']))
                    finally:common.as_uid(0)
                try:
                    try:
                        common.as_uid(10001)
                        auth=common.read_private(Path('/control/codex-auth.json'),limit=65536)
                        token=common.read_private(Path('/control/worker-github-token'),limit=8192)
                        config=common.protected_json('/control/worker-pr.json')
                    finally:common.as_uid(0)
                    credentials=dict(codex_auth=base64.b64encode(auth).decode(),
                        worker_token=base64.b64encode(token).decode(),worker_config=config)
                    def save_auth(raw):
                        try:
                            common.as_uid(10001)
                            destination=Path('/control/codex-auth.json')
                            if common.read_private(destination,limit=65536)!=auth:
                                raise ContractError('manager auth changed during this execution')
                            temporary=destination.with_suffix('.pending')
                            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                            with os.fdopen(fd,'wb') as stream:
                                stream.write(raw);stream.flush();os.fsync(stream.fileno())
                            os.replace(temporary,destination)
                            directory_fd=os.open('/control',os.O_RDONLY|os.O_DIRECTORY)
                            try:os.fsync(directory_fd)
                            finally:os.close(directory_fd)
                        finally:common.as_uid(0)
                    result=session.run(binding,recheck,credentials,persist=persist,auth_update=save_auth)
                except ExecutionCleanupUnverified:
                    common.write_frame(outgoing,{'execution_cleanup_unverified':True})
                    raise
                except BaseException:
                    if session.executed and not session.verified:
                        common.write_frame(outgoing,{'execution_cleanup_unverified':True});raise
                    detail=dict(reason='GOAL_RUNTIME_FAILURE',stage='turn')
                    common.write_frame(outgoing,{'goal_failure':detail})
                    if common.read_failure_ack(incoming)!={'op':'failure_ack','issue':binding['issue_number'],'phase':'BLOCKED'}:
                        raise ContractError('execution failure not durably acknowledged')
                else:common.write_frame(outgoing,{'direct_pr_result':result.to_dict()})
            elif frame=={'op':'job_finished'}:
                session.close();common.write_frame(outgoing,{'cleaned':True});return
            else:raise ContractError('execution broker order rejected')
    finally:session.close()
