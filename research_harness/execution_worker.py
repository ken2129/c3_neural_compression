"""One native Codex session inside the selected reusable experiment container.

This trusted supervisor runs as root; Codex and its ordinary shell/file tools
run as the installed nonroot research user, without manager/Docker authority.
Existing containers explicitly accept sampled limits rather than a cgroup hard
limit. Only descendants of this per-job subreaper are terminated.
"""
import base64
import ctypes
import fcntl
import hashlib
import os
import pwd
from pathlib import Path
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

from .app_server_adapter import BoundedStdioTransport,VERSION,fixed_app_server_request
from .direct_pr_service import DirectPRResult
from .execution_contract import CONTROL_ROOT,alias,validate_containers,auth_identity
from .execution_process import Control,cleanup_family,family,gpu_observation,idle,subreaper,tree_bytes
from .goal_plan import validate_goal_plan,direct_pr_scope
from .goal_runner import GoalAppServerRunner,GoalRequest
from .model_deployment import validate_chatgpt_auth
from .restart_boundary import RestartEpoch
from .schema import ContractError,canonical,fields,loads
from .service_state import validate_binding
from .worker_pr import WorkerGitHub,validate_worker_pr_config


def protected_root(path,mode=0o700):
    path=Path(path)
    path.mkdir(parents=True,exist_ok=True,mode=mode)
    for parent in (path,*path.parents):
        info=parent.lstat()
        sticky_parent=parent!=path and info.st_uid==0 and info.st_mode&stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid!=0 or (info.st_mode&0o022 and not sticky_parent)):
            raise ContractError('protected execution supervisor directory required')
    path.chmod(mode)
    return path


def worker_epoch(container_id):
    """Namespace observation tagged by the trusted Docker inspect, not an oracle."""
    namespace=os.stat('/proc/self/ns/pid').st_ino
    if namespace!=os.stat('/proc/1/ns/pid').st_ino:raise ContractError('execution namespace mismatch')
    parts=Path('/proc/1/stat').read_text().rsplit(')',1)[1].split()
    return RestartEpoch(container_id,Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                        namespace,int(parts[19]))


def validate_preparation(value):
    fields(value,('op','plan','policy'))
    if value['op']!='prepare':raise ContractError('execution preparation required')
    policy=value['policy']
    fields(policy,('alias','container_id','runtime_source_sha','source_sha','deployment_id','scope','target'))
    alias(policy['alias']);target=validate_containers({policy['alias']:policy['target']})[policy['alias']]
    from .goal_plan import DIRECT_PR_KIND
    plan=validate_goal_plan(value['plan'],policy['source_sha'],resource_limits=target['resource_limits'],
                            deployment_id=policy['deployment_id'])
    if (plan['kind']!=DIRECT_PR_KIND or plan.get('execution_container')!=policy['alias']
            or direct_pr_scope(plan)!=policy['scope']):raise ContractError('execution scope mismatch')
    worker_epoch(policy['container_id'])
    pwd.getpwuid(target['worker_uid'])
    for path in ('/run/c3-docker.sock','/var/run/docker.sock'):
        if os.path.lexists(path):raise ContractError('Docker authority is forbidden in the experiment container')
    workspace=Path(target['workspace'])
    if not workspace.exists():
        workspace.mkdir(parents=True,mode=0o700);os.chown(workspace,target['worker_uid'],target['worker_uid'])
    for parent in (workspace,*workspace.parents):
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):raise ContractError('fixed research workspace required')
    if workspace.stat().st_uid!=target['worker_uid']:raise ContractError('research workspace owner mismatch')
    # Only the public, pinned inputs are seeded. Existing research files are
    # never overwritten or reset when this container is reused.
    runtime=Path(__file__).resolve().parents[1]
    manifest=loads((runtime/'goal-inputs.json').read_bytes())
    if manifest['source_sha']!=plan['source_sha'] or set(manifest['files'])!=set(plan['inputs']):
        raise ContractError('execution input manifest mismatch')
    for relative in plan['inputs']:
        raw=(runtime/'goal-inputs'/relative).read_bytes();entry=manifest['files'][relative]
        if len(raw)!=entry['size_bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
            raise ContractError('execution input digest mismatch')
        destination=workspace/relative
        for directory in reversed([destination.parent,*destination.parent.parents]):
            if directory==workspace or workspace in directory.parents:
                if not directory.exists():directory.mkdir(mode=0o755)
                info=directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise ContractError('execution input parent is not a regular directory')
        if os.path.lexists(destination):
            fd=os.open(destination,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=len(raw) or stream.read(len(raw)+1)!=raw:
                    raise ContractError('existing research input differs from approved source; no overwrite')
        else:
            fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444)
            with os.fdopen(fd,'wb') as stream:stream.write(raw)
    codex=Path(target['codex']).resolve(strict=True);info=codex.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
        raise ContractError('protected Codex installation required')
    # Codex v0.160 creates CODEX_HOME/tmp/arg0 even for --version. Use a separate
    # fresh home, never the auth-only home later admitted for the model turn.
    protected_root('/work',0o755)
    probe=Path(tempfile.mkdtemp(prefix='codex-version-',dir='/work'))
    try:
        os.chown(probe,target['worker_uid'],target['worker_uid'])
        for name in ('home','codex-home'):
            path=probe/name;path.mkdir(mode=0o700);os.chown(path,target['worker_uid'],target['worker_uid'])
        def probe_setup():
            if ctypes.CDLL(None).prctl(38,1,0,0,0):raise RuntimeError('probe no-new-privileges unavailable')
            resource.setrlimit(resource.RLIMIT_CORE,(0,0))
            resource.setrlimit(resource.RLIMIT_CPU,(10,10))
        version=subprocess.run([target['codex'],'--version'],user=target['worker_uid'],group=target['worker_uid'],
            extra_groups=[],cwd='/',env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(probe/'home'),
                'CODEX_HOME':str(probe/'codex-home')},stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                timeout=10,preexec_fn=probe_setup)
    finally:shutil.rmtree(probe)
    if version.returncode or version.stdout.strip()!=('codex-cli '+VERSION).encode():
        raise ContractError('installed Codex version mismatch')
    for name in ('worker-home','codex-home'):
        if os.path.lexists('/work/'+name):raise ContractError('prior execution credentials/work remain')
    return policy,plan


def stage_credentials(target,plan,credentials):
    fields(credentials,('codex_auth','worker_config','worker_token'))
    try:
        auth=base64.b64decode(credentials['codex_auth'],validate=True)
        token=base64.b64decode(credentials['worker_token'],validate=True)
    except (ValueError,TypeError):raise ContractError('execution credentials rejected') from None
    if len(auth)>16384 or len(token)>4097:raise ContractError('execution credential size rejected')
    validate_chatgpt_auth(auth)
    config=validate_worker_pr_config(credentials['worker_config'])
    if not config['enabled']:raise ContractError('worker publication disabled')
    actor=WorkerGitHub(token.decode('ascii').strip()).request('GET','/user')
    if actor.get('id')!=config['actor_id'] or actor.get('login','').lower()!=config['actor_login'].lower():
        raise ContractError('worker credential actor mismatch')
    uid=target['worker_uid']
    for name in ('worker-home','codex-home'):
        path=Path('/work')/name;path.mkdir(mode=0o700);os.chown(path,uid,uid)
    for name,raw in {'codex-home/auth.json':auth,'worker-home/github-token':token,
        'worker-home/worker-pr.json':canonical(config),'worker-home/approved-plan.json':canonical(plan)}.items():
        path=Path('/work')/name
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw)
        os.chown(path,uid,uid)


def return_auth_state(target,credentials,control,binding):
    """Private lifecycle traffic, consumed by the broker and never in reports."""
    path=Path('/work/codex-home/auth.json')
    if not path.exists():return
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=target['worker_uid'] or info.st_nlink!=1
                or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>16384):
            raise ContractError('private model auth state rejected')
        raw=stream.read(16385)
    original=base64.b64decode(credentials['codex_auth'],validate=True)
    if raw==original:return
    validate_chatgpt_auth(raw)
    identity=auth_identity(original)
    if identity is None or auth_identity(raw)!=identity:
        raise ContractError('model auth identity changed')
    control.emit(dict(op='auth_state',binding=binding,auth_state=base64.b64encode(raw).decode()))
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        frame=control.receive(.2)
        if frame is None:continue
        if frame==dict(op='auth_state_saved',binding=binding):return
        if frame not in (dict(op='lease',binding=binding),dict(op='cancel',binding=binding)):
            raise ContractError('model auth state acknowledgement rejected')
    raise ContractError('model auth state not saved')


class Lease:
    def __init__(self,control,binding,deadline):
        self.control,self.binding,self.deadline=control,binding,deadline
        self.until=time.monotonic()+10

    def check(self):
        while True:
            frame=self.control.receive(0)
            if frame is None:break
            if frame!=dict(op='lease',binding=self.binding):raise ContractError('execution cancelled')
            self.until=time.monotonic()+10
        if time.monotonic()>=min(self.until,self.deadline):raise ContractError('execution lease/deadline expired')


def run_job(policy,plan,binding,credentials,control,*,runner_factory=GoalAppServerRunner,
            stage=stage_credentials,observe=gpu_observation):
    validate_binding(binding)
    target=policy['target'];workspace=Path(target['workspace'])
    helper=str(Path(__file__).resolve().parents[1]/'worker_pr.py')
    request=GoalRequest(plan,policy['source_sha'],workspace=str(workspace),publication_helper=helper)
    if (binding['plan_digest']!=request.digest or binding['source_sha']!=policy['source_sha']
            or binding['issue_number']!=plan['direct_pr']['issue_number']):
        raise ContractError('execution binding mismatch')
    state=Path(CONTROL_ROOT)/'runs';state.mkdir(mode=0o700,exist_ok=True)
    receipt_path=state/(binding['run_id']+'.json')
    fd=os.open(receipt_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(canonical(dict(phase='CLAIMED',binding=binding)));stream.flush();os.fsync(stream.fileno())
    directory_fd=os.open(state,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory_fd)
    finally:os.close(directory_fd)
    subreaper()
    started=time.monotonic();deadline=started+plan['max_seconds']
    lease=Lease(control,binding,deadline-20)
    def interrupted(*_):raise ContractError('execution interrupted')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    process=transport=candidate=None;failure=None;cleanup=False;selected=None;gpu_lock=None
    initial=tree_bytes(workspace);last_sample=0
    def check():
        nonlocal last_sample
        lease.check()
        if time.monotonic()-last_sample<.5:return
        last_sample=time.monotonic()
        records=family();budget=plan['resource_budget']
        growth=max(0,tree_bytes(workspace)-initial)
        for name in ('worker-home','codex-home'):
            if Path('/work',name).exists():growth+=tree_bytes(Path('/work',name))
        if (sum(row[2] for row in records.values())>budget['memory_bytes']
                or sum(row[3] for row in records.values())>budget['max_processes']
                or growth>budget['max_work_bytes']):raise ContractError('execution sampled resource limit')
        free=os.statvfs(workspace)
        if free.f_bavail*free.f_frsize<target.get('disk_reserve_bytes',0):
            raise ContractError('execution disk reserve reached')
        if selected is not None and observe()[1].get(selected,0)>1:
            raise ContractError('selected GPU competition detected')
    try:
        allowed=sorted(os.sched_getaffinity(0))
        if len(allowed)<target['cpu_cores']:raise ContractError('execution CPU allocation unavailable')
        os.sched_setaffinity(0,allowed[-target['cpu_cores']:])
        if target['gpu_uuids']:
            wait_until=min(deadline-20,time.monotonic()+target['max_wait_seconds'])
            stable={gpu:0 for gpu in target['gpu_uuids']}
            while selected is None:
                check();observed=observe()
                for gpu in stable:
                    stable[gpu]=stable[gpu]+1 if idle(gpu,observed) else 0
                    if stable[gpu]<3:continue
                    fd=os.open(Path(CONTROL_ROOT)/(gpu+'.lock'),os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:os.close(fd);continue
                    if idle(gpu,observe()):selected,gpu_lock=gpu,fd;break
                    os.close(fd)
                if selected is not None:break
                if time.monotonic()>=wait_until:raise ContractError('GPU wait timed out')
                control.emit(dict(op='status',phase='waiting_gpu'));time.sleep(min(1,max(0,wait_until-time.monotonic())))
        stage(target,plan,credentials);check()
        control.emit(dict(op='status',phase='codex_running'))
        uid=target['worker_uid'];parent=os.getpid()
        def child_setup():
            libc=ctypes.CDLL(None)
            if libc.prctl(38,1,0,0,0) or libc.prctl(1,signal.SIGKILL) or os.getppid()!=parent:
                raise RuntimeError('execution child guard failed')
            resource.setrlimit(resource.RLIMIT_CORE,(0,0))
            seconds=plan['resource_budget']['cpu_seconds_per_process']
            resource.setrlimit(resource.RLIMIT_CPU,(seconds,seconds))
            maximum=plan['resource_budget']['max_work_bytes']
            resource.setrlimit(resource.RLIMIT_FSIZE,(maximum,maximum))
        env=dict(PATH='/usr/local/bin:/usr/bin:/bin',LANG='C.UTF-8')
        env.update(target.get('environment',{}))
        env.update(HOME='/work/worker-home',CODEX_HOME='/work/codex-home',CUDA_VISIBLE_DEVICES=selected or '',
            PYTHONDONTWRITEBYTECODE='1',XLA_PYTHON_CLIENT_PREALLOCATE='false',
            GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null')
        args=[target['codex'],*fixed_app_server_request(plan['model']).argv[1:]]
        process=subprocess.Popen(args,user=uid,group=uid,extra_groups=[],cwd=str(workspace),env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,close_fds=True,
            start_new_session=True,preexec_fn=child_setup)
        transport=BoundedStdioTransport(process,recheck=check,deadline=deadline-20,
            max_frame_bytes=min(65536,plan['resource_budget']['max_output_bytes']),
            max_capture_bytes=min(1024*1024,plan['resource_budget']['max_output_bytes']))
        candidate=runner_factory(enabled=True).run(request,transport,admit=lambda request:None,recheck=check)
        check()
    except BaseException as exc:failure=type(exc).__name__
    finally:
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        signal.signal(signal.SIGINT,signal.SIG_IGN)
        if transport is not None:transport.close()
        cleanup=cleanup_family()
        if process is not None:
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()
        if cleanup:
            try:
                return_auth_state(target,credentials,control,binding)
                for name in ('worker-home','codex-home'):
                    path=Path('/work')/name
                    if path.exists():shutil.rmtree(path)
            except BaseException:
                # Retain private files for recovery, and block this target.
                cleanup=False
        if gpu_lock is not None:os.close(gpu_lock)
    if not cleanup:
        (Path(CONTROL_ROOT)/'blocked').write_text('CLEANUP_UNVERIFIED\n')
        raise ContractError('execution cleanup unverified')
    value=dict(binding=binding,cleanup_verified=True)
    if failure is None:
        result=DirectPRResult(binding,plan['model'],candidate.outcome,candidate.summary,candidate.pr_url,
                              time.monotonic()-started)
        value.update(op='worker_result',result=result.to_dict())
        private=dict(value,codex_session=dict(thread_id=candidate.thread_id,turn_id=candidate.turn_id))
    else:
        value.update(op='worker_failure',reason='EXECUTION_FAILED')
        private=dict(value,private_failure_class=failure)
    private['execution']=dict(container_id=policy['container_id'],execution_container=policy['alias'],
        runtime_source_sha=policy['runtime_source_sha'],workspace=str(workspace),gpu_uuid=selected,
        source_sha=policy['source_sha'],model=plan['model'])
    temporary=receipt_path.with_suffix('.complete')
    with temporary.open('xb') as stream:
        stream.write(canonical(private));stream.flush();os.fsync(stream.fileno())
    temporary.chmod(0o600);os.replace(temporary,receipt_path)
    directory_fd=os.open(state,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory_fd)
    finally:os.close(directory_fd)
    control.emit(value)


def main():
    if os.geteuid()!=0:raise ContractError('trusted execution supervisor requires container root')
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    caps=int(status['CapEff'].strip(),16)
    if not all(caps&(1<<bit) for bit in (0,1,5,6,7)):
        raise ContractError('execution supervisor requires UID, file ownership and owned-process cleanup capabilities')
    os.umask(0o077)
    protected_root(CONTROL_ROOT)
    if (Path(CONTROL_ROOT)/'blocked').exists():raise ContractError('execution container is blocked')
    fd=os.open(Path(CONTROL_ROOT)/'active.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for record in (Path(CONTROL_ROOT)/'runs').glob('*.json'):
            saved=loads(record.read_bytes())
            if saved.get('cleanup_verified') is not True:
                raise ContractError('prior execution cleanup is uncertain')
        control=Control(sys.stdin.buffer.raw,sys.stdout.buffer)
        policy,plan=validate_preparation(control.receive(30))
        control.emit(dict(op='ready',epoch=worker_epoch(policy['container_id']).to_dict(),
                          policy_sha256=hashlib.sha256(canonical(policy)).hexdigest()))
        frame=control.receive(30);fields(frame,('op','binding','credentials'))
        if frame['op']!='execute':raise ContractError('execution claim required')
        run_job(policy,plan,frame['binding'],frame['credentials'],control)
    finally:os.close(fd)
