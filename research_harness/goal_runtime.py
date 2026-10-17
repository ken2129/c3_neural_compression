"""Disabled-by-default goal lifecycle for a separately approved image/profile.

This is not canary admission. The worker can read its ChatGPT auth and bridge
egress is unrestricted; prompts do not prevent either. Approved project writes
are limited by ownership, with explicit writable runtime-home and IPC tmpfs
exceptions. Aggregate cgroups and a PID1 alarm bound the allocation. UID scans
are best effort only: every attempt must cross a verified namespace restart.

Before that exit, bounded untrusted artifact bytes may be copied into a trusted
manager quarantine. Copying is not validation or proof that descendants stopped.
The manager may inspect the durable private copy in the same generation; no
cleanup conclusion or publication is permitted before restart and separate
content/publication approval. Never execute copied code as manager/root.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextvars import ContextVar
from functools import wraps
from weakref import WeakKeyDictionary, WeakSet
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from .schema import ContractError, canonical, fields, loads
from .goal_plan import validate_goal_plan
from .goal_runner import GoalRequest, GoalAppServerRunner, GoalCandidate, GoalBlocked, safe_goal_diagnostic
from .app_server_adapter import BoundedStdioTransport, fixed_app_server_request, safe_app_server_diagnostic
from .uid_supervisor import (OwnedUIDSupervisor, LinuxUIDBackend, ChildHandle,
                            WORKER_UID, CONTROLLER_UID)

WORKSPACE = '/work/goal'
FINALIZATION_RESERVE = 20
MAX_ENTRIES = 4096
MAX_INPUT_BYTES = 16 * 1024 * 1024
CONFIG_PATH = '/control/goal-runtime.json'
INPUT_MANIFEST = '/opt/c3-runtime/goal-inputs.json'
CONSENTS = ('arbitrary_cpu_commands', 'worker_model_auth_readable', 'unrestricted_bridge_egress',
            'writable_runtime_homes_and_ipc', 'usage_without_hard_cost_cap')


_RUNTIME_REASONS = frozenset((
    'GOAL_ACTUAL_RESOURCES_EXCEED_REQUEST',
    'GOAL_ADMISSION_NOT_A_FLAG',
    'GOAL_ADMITTED_REQUEST_CHANGED',
    'GOAL_CGROUP_NAMESPACE_REQUIRED',
    'GOAL_CHILD_IDENTITY_INVALID',
    'GOAL_CODEX_EXECUTABLE_REQUIRED',
    'GOAL_CONTAINER_IDENTITY_MISMATCH',
    'GOAL_CPU_QUOTA_REQUIRED',
    'GOAL_DEDICATED_ROOT_PID1_REQUIRED',
    'GOAL_DEPLOYMENT_INITIALIZATION_INTENT',
    'GOAL_FINALIZATION_DEADLINE',
    'GOAL_FINALIZATION_RESERVE_REQUIRED',
    'GOAL_HOST_SOCKET_OR_GPU',
    'GOAL_IMAGE_SOURCE_MISMATCH',
    'GOAL_INPUT_HASH_MISMATCH',
    'GOAL_INPUT_MANIFEST_INVALID',
    'GOAL_INPUT_NOT_READABLE',
    'GOAL_INPUT_WRITE_SCOPE_OVERLAP',
    'GOAL_IPC_NAMESPACE_CHANGED',
    'GOAL_IPC_TMPFS_LIMIT',
    'GOAL_MANAGER_DIRECTORY_PERMISSIONS',
    'GOAL_MODEL_AUTH_LAYOUT_INVALID',
    'GOAL_MODEL_DEADLINE',
    'GOAL_NESTED_WORK_MOUNT',
    'GOAL_OPERATOR_CONFIGURATION_MISMATCH',
    'GOAL_OPERATOR_CONFIGURATION_PERMISSIONS',
    'GOAL_OPERATOR_RISK_CONSENT_REQUIRED',
    'GOAL_OVERLAPPING_WRITE_SCOPE',
    'GOAL_PLAN_CHANGED',
    'GOAL_PRIVATE_TMPFS_REQUIRED',
    'GOAL_PROTECTED_INPUT_CHANGED',
    'GOAL_PROTECTED_INPUT_LIMIT',
    'GOAL_PROTECTED_PATH_INVALID',
    'GOAL_QUARANTINE_BYTE_LIMIT',
    'GOAL_QUARANTINE_CHANGED',
    'GOAL_QUARANTINE_CONTRACT',
    'GOAL_QUARANTINE_ENTRY_LIMIT',
    'GOAL_QUARANTINE_FILE_TYPE',
    'GOAL_QUARANTINE_NAME',
    'GOAL_QUARANTINE_NOT_DURABLE',
    'GOAL_QUARANTINE_OWNER_OR_MODE',
    'GOAL_QUARANTINE_READ_FAILED',
    'GOAL_READONLY_DIRECTORY_ACCESS',
    'GOAL_READONLY_ROOT_CGROUP_REQUIRED',
    'GOAL_RESOURCE_UNBOUNDED',
    'GOAL_RUNTIME_DISABLED',
    'GOAL_RUNTIME_HOME_INVALID',
    'GOAL_RUNTIME_HOME_NOT_FRESH',
    'GOAL_RUN_BINDING_INVALID',
    'GOAL_STARTUP_DEADLINE',
    'GOAL_STARTUP_PIPE_REQUIRED',
    'GOAL_TRUSTED_CALLBACKS_REQUIRED',
    'GOAL_UNAPPROVED_CONFIG',
    'GOAL_UNEXPECTED_WORKSPACE_ENTRY',
    'GOAL_WORKSPACE_ENTRY_MISSING',
    'GOAL_WORK_LAYOUT_INVALID',
    'GOAL_WORK_ROOT_OWNERSHIP',
    'GOAL_WRITE_SCOPE_NOT_FRESH',
    'GOAL_WRITE_SCOPE_OWNERSHIP',
))
_STAGES = frozenset(('admission','resources','reserve','watchdog','approval','spawn',
    'barrier','identity','release','transport','turn','cleanup','quarantine','persist','postcheck'))
_FAILURES = WeakKeyDictionary()
_ACKNOWLEDGEABLE = WeakSet()
_CONTEXT = ContextVar('goal_runtime_diagnostic',default=None)
_NORMALIZED_REASONS = frozenset(('GOAL_RUNTIME_FAILURE','GOAL_TIMEOUT','GOAL_OS_FAILURE',
    'GOAL_CONTRACT_FAILURE','GOAL_TRANSPORT_FAILURE','GOAL_TRANSPORT_OUTPUT_LIMIT',
    'GOAL_TRANSPORT_FRAME_LIMIT','GOAL_TRANSPORT_DEADLINE','GOAL_TRANSPORT_EARLY_EOF'))


class GoalRuntimeBlocked(ContractError):
    def __init__(self,reason):
        code=(reason if type(reason) is str and reason in (_RUNTIME_REASONS|_NORMALIZED_REASONS)
              else 'GOAL_RUNTIME_FAILURE')
        super().__init__(code)
        if type(self) is GoalRuntimeBlocked:
            context=_CONTEXT.get()
            _FAILURES[self]=(code,context['stage'] if context is not None else 'admission')


def safe_goal_runtime_diagnostic(exc):
    """Fresh closed fields only; exception args and mutable attributes are ignored."""
    if type(exc) not in (GoalRuntimeBlocked,GoalBlocked):return None
    value=_FAILURES.get(exc)
    if value is None:return None
    return {'reason':value[0],'stage':value[1]}


def goal_failure_can_acknowledge(exc):
    """Ordinary failure after bounded cleanup returned; NOT namespace-death proof.

    A launcher may give the manager a bounded failure acknowledgement before
    mandatory PID1 exit. The watchdog remains armed. Neither mutable exception
    attributes nor the presence of a diagnostic grants this handling status.
    """
    return type(exc) in (GoalRuntimeBlocked,GoalBlocked) and exc in _ACKNOWLEDGEABLE


def _stage(value):
    _CONTEXT.get()['stage']=value if value in _STAGES else 'admission'


def _failure_detail(exc):
    context=_CONTEXT.get()
    detail=safe_goal_runtime_diagnostic(exc)
    if type(exc) is GoalBlocked:
        detail=dict(safe_goal_diagnostic(exc),stage=context['stage'])
        _FAILURES[exc]=(detail['reason'],detail['stage'])
    if detail is None:
        transport=safe_app_server_diagnostic(exc)
        if transport is not None:
            reason={'APP_SERVER_OUTPUT_LIMIT':'GOAL_TRANSPORT_OUTPUT_LIMIT',
                    'APP_SERVER_FRAME_LIMIT':'GOAL_TRANSPORT_FRAME_LIMIT',
                    'APP_SERVER_DEADLINE_EXCEEDED':'GOAL_TRANSPORT_DEADLINE',
                    'APP_SERVER_EARLY_EOF':'GOAL_TRANSPORT_EARLY_EOF'}.get(
                        transport['reason'],'GOAL_TRANSPORT_FAILURE')
        else:
            reason=('GOAL_TIMEOUT' if isinstance(exc,TimeoutError) else
                    'GOAL_OS_FAILURE' if isinstance(exc,OSError) else
                    'GOAL_CONTRACT_FAILURE' if isinstance(exc,ContractError) else 'GOAL_RUNTIME_FAILURE')
        detail={'reason':reason,'stage':context['stage']}
    return detail


def _report_failure(exc):
    context=_CONTEXT.get()
    if context is None or context['logged']:return
    context['logged']=True
    detail=_failure_detail(exc)
    try:print(json.dumps(dict(phase='GOAL_RUNTIME_BLOCKED',**detail)),file=sys.stderr,flush=True)
    except Exception:pass


def _diagnosed(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        token=_CONTEXT.set({'stage':'admission','logged':False})
        try:return fn(*args,**kwargs)
        except BaseException as exc:
            _report_failure(exc)
            raise
        finally:_CONTEXT.reset(token)
    return wrapped


@dataclass(frozen=True)
class GoalRunBinding:
    source_sha: str
    plan_digest: str
    request_id: str
    run_id: str

    def validate(self, request):
        if (self.source_sha != request.source_sha or self.plan_digest != request.digest
                or any(type(value) is not str or re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value) is None
                       for value in (self.request_id, self.run_id))):
            raise GoalRuntimeBlocked('GOAL_RUN_BINDING_INVALID')


@dataclass(frozen=True)
class QuarantineEntry:
    path: str
    kind: str
    mode: int
    content: bytes


@dataclass(frozen=True)
class UntrustedGoalSnapshot:
    binding: GoalRunBinding
    entries: tuple[QuarantineEntry, ...]

    @property
    def digest(self):
        manifest = [dict(path=e.path, kind=e.kind, mode=e.mode, size=len(e.content),
                         sha256=hashlib.sha256(e.content).hexdigest()) for e in self.entries]
        return hashlib.sha256(canonical(dict(binding=vars(self.binding), entries=manifest))).hexdigest()


@dataclass(frozen=True)
class PendingGoalResult:
    candidate: GoalCandidate
    elapsed_seconds: float
    quarantine_digest: str
    requires_namespace_restart: bool = field(default=True, init=False)
    cleanup_verified: bool = field(default=False, init=False)
    publish_authorized: bool = field(default=False, init=False)


def _protected(path, *, owner=0, directory=False):
    info = Path(path).lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    owners=owner if type(owner) is tuple else (owner,)
    if (not expected(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise GoalRuntimeBlocked('GOAL_PROTECTED_PATH_INVALID')
    return info


def _read_protected(path, *, limit, owner=0):
    path = Path(path)
    for parent in path.parents:
        _protected(parent, owner=CONTROLLER_UID if str(parent)=='/control' else 0, directory=True)
    before = _protected(path, owner=owner)
    if before.st_size > limit:
        raise GoalRuntimeBlocked('GOAL_PROTECTED_INPUT_LIMIT')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, limit + 1)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or len(raw) != before.st_size:
            raise GoalRuntimeBlocked('GOAL_PROTECTED_INPUT_CHANGED')
        return raw
    finally:
        os.close(fd)


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _scope_paths(plan):
    scopes = tuple(plan['write_scope'])
    if any(a != b and (a.startswith(b + '/') or b.startswith(a + '/'))
           for a in scopes for b in scopes):
        raise GoalRuntimeBlocked('GOAL_OVERLAPPING_WRITE_SCOPE')
    if any(p == s or p.startswith(s + '/') or s.startswith(p + '/')
           for p in plan['inputs'] for s in scopes):
        raise GoalRuntimeBlocked('GOAL_INPUT_WRITE_SCOPE_OVERLAP')
    return scopes


def collect_untrusted_goal_bytes(request, binding, *, check):
    """FD-relative bounded copy of only approved output scopes, not execution.

    Call as the worker UID after best-effort cleanup. Active-writer races that
    are observed reject; absence of a detected race is not a quiescence proof.
    Only immutable returned bytes may be handed to the protected writer.
    """
    plan = request.validated_plan(); binding.validate(request)
    scopes = _scope_paths(plan); maximum = plan['resource_budget']['max_output_bytes']
    entries = []; total = 0

    def visit(parent, name, path):
        nonlocal total
        check()
        if len(entries) >= MAX_ENTRIES or len(path.encode()) > 1024:
            raise GoalRuntimeBlocked('GOAL_QUARANTINE_ENTRY_LIMIT')
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        mode = stat.S_IMODE(before.st_mode)
        if before.st_uid != WORKER_UID or mode & 0o6000:
            raise GoalRuntimeBlocked('GOAL_QUARANTINE_OWNER_OR_MODE')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(before.st_mode):
            fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=parent)
            try:
                if _identity(before) != _identity(os.fstat(fd)):
                    raise GoalRuntimeBlocked('GOAL_QUARANTINE_CHANGED')
                names = []
                with os.scandir(fd) as listing:
                    for item in listing:
                        check()
                        if len(names) + len(entries) >= MAX_ENTRIES:
                            raise GoalRuntimeBlocked('GOAL_QUARANTINE_ENTRY_LIMIT')
                        names.append(item.name)
                names.sort()
                entries.append(QuarantineEntry(path, 'directory', mode, b''))
                for child in names:
                    if (child.startswith('.') or any(ord(c) < 32 for c in child)
                            or '/' in child or len(child.encode()) > 256):
                        raise GoalRuntimeBlocked('GOAL_QUARANTINE_NAME')
                    visit(fd, child, path + '/' + child)
                if _identity(before) != _identity(os.fstat(fd)):
                    raise GoalRuntimeBlocked('GOAL_QUARANTINE_CHANGED')
            finally:
                os.close(fd)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            if before.st_size > maximum - total:
                raise GoalRuntimeBlocked('GOAL_QUARANTINE_BYTE_LIMIT')
            fd = os.open(name, flags, dir_fd=parent)
            try:
                if _identity(before) != _identity(os.fstat(fd)):
                    raise GoalRuntimeBlocked('GOAL_QUARANTINE_CHANGED')
                chunks = []; remaining = maximum - total + 1
                while remaining:
                    check()
                    data = os.read(fd, min(65536, remaining))
                    if not data: break
                    chunks.append(data); remaining -= len(data)
                raw = b''.join(chunks)
                if len(raw) != before.st_size or _identity(before) != _identity(os.fstat(fd)):
                    raise GoalRuntimeBlocked('GOAL_QUARANTINE_CHANGED')
                total += len(raw)
                entries.append(QuarantineEntry(path, 'file', mode, raw))
            finally:
                os.close(fd)
        else:
            raise GoalRuntimeBlocked('GOAL_QUARANTINE_FILE_TYPE')
        if _identity(before) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
            raise GoalRuntimeBlocked('GOAL_QUARANTINE_CHANGED')

    opened = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY); opened.append(fd)
        for part in ('work', 'goal'):
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            opened.append(fd)
        root = fd
        for scope in scopes:
            parent = root; nested = []
            try:
                parts = scope.split('/')
                for part in parts[:-1]:
                    parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    nested.append(parent)
                visit(parent, parts[-1], scope)
            finally:
                for item in reversed(nested): os.close(item)
    except (OSError, UnicodeError, RecursionError):
        raise GoalRuntimeBlocked('GOAL_QUARANTINE_READ_FAILED') from None
    finally:
        for fd in reversed(opened): os.close(fd)
    return UntrustedGoalSnapshot(binding, tuple(sorted(entries, key=lambda e:e.path)))


class GoalLinuxUIDBackend(LinuxUIDBackend):
    """Only for the new goal image; never silently admits the canary profile."""

    def __init__(self):
        self._ipc_namespace=None

    def context(self):
        result = LinuxUIDBackend.context()
        marker = _read_protected('/opt/c3-runtime/dedicated-pid1-goal', limit=64)
        result['dedicated_marker'] = marker == b'c3-dedicated-pid1-goal-v1\n'
        ipc=os.readlink('/proc/self/ns/ipc')
        if (ipc!=os.readlink('/proc/1/ns/ipc')
                or self._ipc_namespace is not None and ipc!=self._ipc_namespace):
            raise GoalRuntimeBlocked('GOAL_IPC_NAMESPACE_CHANGED')
        return result

    @staticmethod
    def resources():
        if Path('/proc/self/cgroup').read_text().strip() != '0::/':
            raise GoalRuntimeBlocked('GOAL_CGROUP_NAMESPACE_REQUIRED')
        cg = Path('/sys/fs/cgroup')
        def finite(name):
            value = (cg/name).read_text().strip()
            if not value.isdigit(): raise GoalRuntimeBlocked('GOAL_RESOURCE_UNBOUNDED')
            return int(value)
        cpu = (cg/'cpu.max').read_text().split()
        if len(cpu)!=2 or any(not value.isdigit() for value in cpu):
            raise GoalRuntimeBlocked('GOAL_CPU_QUOTA_REQUIRED')
        mounts = {}
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left,right=line.split(' - ',1); a,b=left.split(),right.split()
            mounts[a[4]]=(set(a[5].split(',')),b[0])
        root=mounts.get('/'); cgroup=mounts.get('/sys/fs/cgroup')
        if not root or 'ro' not in root[0] or not cgroup or cgroup[1]!='cgroup2' or 'ro' not in cgroup[0]:
            raise GoalRuntimeBlocked('GOAL_READONLY_ROOT_CGROUP_REQUIRED')
        work=mounts.get('/work'); shm=mounts.get('/dev/shm')
        if (not work or work[1]!='tmpfs' or not {'noexec','nosuid','nodev'}<=work[0]
                or not shm or shm[1]!='tmpfs' or not {'noexec','nosuid','nodev'}<=shm[0]):
            raise GoalRuntimeBlocked('GOAL_PRIVATE_TMPFS_REQUIRED')
        if any(path.startswith('/work/') for path in mounts):
            raise GoalRuntimeBlocked('GOAL_NESTED_WORK_MOUNT')
        _protected('/work',directory=True)
        if stat.S_IMODE(Path('/work').lstat().st_mode)!=0o755:
            raise GoalRuntimeBlocked('GOAL_WORK_ROOT_OWNERSHIP')
        shm_vfs=os.statvfs('/dev/shm')
        if shm_vfs.f_blocks*shm_vfs.f_frsize>64*1024*1024:
            raise GoalRuntimeBlocked('GOAL_IPC_TMPFS_LIMIT')
        if (Path('/var/run/docker.sock').exists() or Path('/run/containerd/containerd.sock').exists()
                or list(Path('/dev').glob('nvidia*')) or Path('/dev/dri').exists()):
            raise GoalRuntimeBlocked('GOAL_HOST_SOCKET_OR_GPU')
        vfs=os.statvfs('/work')
        return dict(memory=finite('memory.max'),swap=finite('memory.swap.max'),pids=finite('pids.max'),
                    cpu_quota=int(cpu[0]),cpu_period=int(cpu[1]),work_bytes=vfs.f_blocks*vfs.f_frsize)

    def admit_goal(self, request):
        try:
            os.seteuid(CONTROLLER_UID)
            if stat.S_IMODE(_protected('/control',owner=CONTROLLER_UID,directory=True).st_mode)!=0o700:
                raise GoalRuntimeBlocked('GOAL_MANAGER_DIRECTORY_PERMISSIONS')
            if stat.S_IMODE(_protected(CONFIG_PATH,owner=CONTROLLER_UID).st_mode)!=0o600:
                raise GoalRuntimeBlocked('GOAL_OPERATOR_CONFIGURATION_PERMISSIONS')
            config=loads(_read_protected(CONFIG_PATH,limit=16384,owner=CONTROLLER_UID))
            deployment=loads(_read_protected('/control/goal-deployment.json',limit=1024*1024,
                                             owner=(0,CONTROLLER_UID)))
        finally:
            os.seteuid(0)
        fields(config,('schema','enabled','input_source_sha','runtime_source_sha','model','resource_limits','risk_acknowledgements'))
        if (config['schema']!='c3-goal-runtime-v1' or config['input_source_sha']!=request.source_sha
                or config['enabled'] is not True
                or config['model']!=request.plan['model']
                or type(config['runtime_source_sha']) is not str
                or re.fullmatch('[0-9a-f]{40}',config['runtime_source_sha']) is None):
            raise GoalRuntimeBlocked('GOAL_OPERATOR_CONFIGURATION_MISMATCH')
        fields(config['risk_acknowledgements'],CONSENTS)
        if any(config['risk_acknowledgements'][key] is not True for key in CONSENTS):
            raise GoalRuntimeBlocked('GOAL_OPERATOR_RISK_CONSENT_REQUIRED')
        plan=validate_goal_plan(request.plan,request.source_sha,resource_limits=config['resource_limits'])
        # Host inspect is collected/protected by the approved operator setup.
        # Internal procfs cannot independently prove Docker image/private IPC or
        # exclusive volume ownership. No consent boolean substitutes for this.
        from .goal_deployment import validate_goal_deployment
        fields(deployment,('inspect','consumers','volume_name','volume_inspect','initialize_new_state'))
        if type(deployment['initialize_new_state']) is not bool:
            raise GoalRuntimeBlocked('GOAL_DEPLOYMENT_INITIALIZATION_INTENT')
        verified=validate_goal_deployment(deployment['inspect'],deployment['consumers'],
            runtime_source_sha=config['runtime_source_sha'],input_source_sha=request.source_sha,
            volume_name=deployment['volume_name'],volume_inspect=deployment['volume_inspect'],
            resource_limits=config['resource_limits'])
        if Path('/etc/hostname').read_text().strip()!=verified['container_id'][:12]:
            raise GoalRuntimeBlocked('GOAL_CONTAINER_IDENTITY_MISMATCH')
        ipc=os.readlink('/proc/self/ns/ipc')
        if ipc!=os.readlink('/proc/1/ns/ipc'):raise GoalRuntimeBlocked('GOAL_IPC_NAMESPACE_CHANGED')
        self._ipc_namespace=ipc
        if _read_protected('/opt/service/source-sha',limit=41).strip()!=config['runtime_source_sha'].encode():
            raise GoalRuntimeBlocked('GOAL_IMAGE_SOURCE_MISMATCH')
        info=_protected('/opt/codex/bin/codex')
        if not info.st_mode & 0o001:raise GoalRuntimeBlocked('GOAL_CODEX_EXECUTABLE_REQUIRED')
        manifest=loads(_read_protected(INPUT_MANIFEST,limit=65536))
        fields(manifest,('source_sha','files'))
        if manifest['source_sha']!=request.source_sha or type(manifest['files']) is not dict:
            raise GoalRuntimeBlocked('GOAL_INPUT_MANIFEST_INVALID')
        scopes=_scope_paths(plan)
        expected_dirs={''}
        for path in (*plan['inputs'],*scopes):
            parts=path.split('/')
            expected_dirs.update('/'.join(parts[:i]) for i in range(1,len(parts)))
        for scope in scopes: expected_dirs.add(scope)
        expected=set(expected_dirs)|set(plan['inputs']);found={''};total=0
        def walk(path,prefix):
            nonlocal total
            if stat.S_IMODE(_protected(path,directory=True).st_mode)!=0o755:
                raise GoalRuntimeBlocked('GOAL_READONLY_DIRECTORY_ACCESS')
            for name in os.listdir(path):
                relative=prefix+name;child=path/name;found.add(relative)
                if relative not in expected:raise GoalRuntimeBlocked('GOAL_UNEXPECTED_WORKSPACE_ENTRY')
                if relative in scopes:
                    info=child.lstat()
                    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=WORKER_UID
                            or stat.S_IMODE(info.st_mode)!=0o700):
                        raise GoalRuntimeBlocked('GOAL_WRITE_SCOPE_OWNERSHIP')
                    try:
                        os.seteuid(WORKER_UID)
                        if os.listdir(child):raise GoalRuntimeBlocked('GOAL_WRITE_SCOPE_NOT_FRESH')
                    finally:os.seteuid(0)
                elif relative in plan['inputs']:
                    entry=manifest['files'].get(relative)
                    fields(entry,('sha256','size_bytes'))
                    if (type(entry['size_bytes']) is not int or not 0<=entry['size_bytes']<=MAX_INPUT_BYTES
                            or type(entry['sha256']) is not str or re.fullmatch('[0-9a-f]{64}',entry['sha256']) is None):
                        raise GoalRuntimeBlocked('GOAL_INPUT_MANIFEST_INVALID')
                    raw=_read_protected(child,limit=min(MAX_INPUT_BYTES-total,entry['size_bytes']))
                    total+=len(raw)
                    if len(raw)!=entry['size_bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
                        raise GoalRuntimeBlocked('GOAL_INPUT_HASH_MISMATCH')
                    if not child.lstat().st_mode & 0o004:raise GoalRuntimeBlocked('GOAL_INPUT_NOT_READABLE')
                else:walk(child,relative+'/')
        walk(Path(WORKSPACE),'')
        if found!=expected:raise GoalRuntimeBlocked('GOAL_WORKSPACE_ENTRY_MISSING')
        if set(os.listdir('/work'))!={'goal','worker-home','codex-home'}:
            raise GoalRuntimeBlocked('GOAL_WORK_LAYOUT_INVALID')
        try:
            os.seteuid(WORKER_UID)
            for name in ('worker-home','codex-home'):
                info=Path('/work',name).lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid!=WORKER_UID or stat.S_IMODE(info.st_mode)!=0o700:
                    raise GoalRuntimeBlocked('GOAL_RUNTIME_HOME_INVALID')
            if os.listdir('/work/worker-home') or set(os.listdir('/work/codex-home'))!={'auth.json'}:
                raise GoalRuntimeBlocked('GOAL_RUNTIME_HOME_NOT_FRESH')
            info=Path('/work/codex-home/auth.json').lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=WORKER_UID or info.st_nlink!=1
                    or stat.S_IMODE(info.st_mode)!=0o600 or not 0<info.st_size<=65536):
                raise GoalRuntimeBlocked('GOAL_MODEL_AUTH_LAYOUT_INVALID')
        finally:os.seteuid(0)
        if any(os.path.lexists(p) for p in ('/.codex','/work/.codex','/work/goal/.codex')):
            raise GoalRuntimeBlocked('GOAL_UNAPPROVED_CONFIG')

    @staticmethod
    def launch_goal(request):
        gate_read,gate_write=os.pipe();ready_read,ready_write=os.pipe()
        try:
            child=spawn_goal_child(request,gate_read=gate_read,ready_write=ready_write)
        except BaseException:
            os.close(gate_write);os.close(ready_read);raise
        finally:
            os.close(gate_read);os.close(ready_write)
        return ChildHandle(child,gate_write,ready_read)

    @staticmethod
    def collect_goal(request,binding,check):
        def protected_check():
            os.seteuid(0)
            try:check()
            finally:os.seteuid(WORKER_UID)
        try:
            os.seteuid(WORKER_UID)
            return collect_untrusted_goal_bytes(request,binding,check=protected_check)
        finally:os.seteuid(0)


def spawn_goal_child(request, *, gate_read, ready_write):
    plan=request.validated_plan();budget=plan['resource_budget']
    if os.getpid()!=1 or os.getresuid()!=(0,0,0):
        raise GoalRuntimeBlocked('GOAL_DEDICATED_ROOT_PID1_REQUIRED')
    for fd in (gate_read,ready_write):
        if type(fd) is not int or fd<3 or not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise GoalRuntimeBlocked('GOAL_STARTUP_PIPE_REQUIRED')
    # Reuse pinned CLI switches only, never the canary prompt/admission/limits.
    argv=(*fixed_app_server_request(plan['model']).argv,'-c',
          'shell_environment_policy.set={PATH="/usr/local/bin:/usr/bin:/bin",LANG="C.UTF-8"}')
    code=r'''
import os,resource,sys
if os.getresuid()!=(10002,)*3 or os.getresgid()!=(10002,)*3 or os.getgroups():raise SystemExit(125)
status=dict(line.split(':',1) for line in open('/proc/self/status') if ':' in line)
if any(int(status[k],16) for k in ('CapEff','CapPrm','CapAmb','CapInh')) or status['NoNewPrivs'].strip()!='1':raise SystemExit(125)
cpu,pids,work=map(int,sys.argv[3:6])
for key,n in ((resource.RLIMIT_CORE,0),(resource.RLIMIT_CPU,cpu),(resource.RLIMIT_NPROC,pids),(resource.RLIMIT_FSIZE,work),(resource.RLIMIT_NOFILE,64)):
    resource.setrlimit(key,(n,n))
os.chdir('/work/goal')
gate,ready=int(sys.argv[1]),int(sys.argv[2])
os.write(ready,b'R');os.close(ready)
if os.read(gate,1)!=b'G':raise SystemExit(125)
os.close(gate)
env={'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8','HOME':'/work/worker-home','CODEX_HOME':'/work/codex-home','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
os.execve('/opt/codex/bin/codex',sys.argv[6:],env)
'''
    return subprocess.Popen([sys.executable,'-I','-S','-c',code,str(gate_read),str(ready_write),
        str(budget['cpu_seconds_per_process']),str(budget['max_processes']),str(budget['max_work_bytes']),*argv],
        user=WORKER_UID,group=WORKER_UID,extra_groups=[],env={},cwd='/',stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,close_fds=True,
        pass_fds=(gate_read,ready_write),start_new_session=True)


class GoalUIDSupervisor(OwnedUIDSupervisor):
    """Initial aggregate profile <=1GiB/64pids/256MiB; broader syntax is not support.

    Requested wall/CPU limits are enforced independently. Larger aggregate
    profiles require a separately reviewed supervisor/deployment revision.
    """
    @_diagnosed
    def run_goal(self,request,recheck,*,binding,quarantine,enabled=False,before_release=None):
        if enabled is not True:raise GoalRuntimeBlocked('GOAL_RUNTIME_DISABLED')
        if (type(request) is not GoalRequest or type(binding) is not GoalRunBinding
                or not callable(recheck) or not callable(quarantine)
                or (before_release is not None and not callable(before_release))):
            raise GoalRuntimeBlocked('GOAL_TRUSTED_CALLBACKS_REQUIRED')
        plan=request.validated_plan();binding.validate(request);digest=request.digest
        if plan['max_seconds']<=FINALIZATION_RESERVE:
            raise GoalRuntimeBlocked('GOAL_FINALIZATION_RESERVE_REQUIRED')
        self._context(initial=True)
        recheck()
        if self.os.admit_goal(request) is not None:raise GoalRuntimeBlocked('GOAL_ADMISSION_NOT_A_FLAG')
        _stage('resources')
        limits=self.os.resources();budget=plan['resource_budget']
        if (limits['memory']>budget['memory_bytes'] or limits['pids']>budget['max_processes']
                or limits['work_bytes']>budget['max_work_bytes']):
            raise GoalRuntimeBlocked('GOAL_ACTUAL_RESOURCES_EXCEED_REQUEST')
        _stage('reserve');reservation=self.reserve();started=self.os.now()
        deadline=started+plan['max_seconds'];model_deadline=deadline-FINALIZATION_RESERVE
        _stage('watchdog');self.os.arm_watchdog(plan['max_seconds']+4)
        handle=transport=None
        def check():
            if self.os.now()>=deadline:raise GoalRuntimeBlocked('GOAL_FINALIZATION_DEADLINE')
            recheck()
            if request.digest!=digest:raise GoalRuntimeBlocked('GOAL_PLAN_CHANGED')
        def admitted(current):
            if current is not request or current.digest!=digest:
                raise GoalRuntimeBlocked('GOAL_ADMITTED_REQUEST_CHANGED')
        try:
            _stage('approval');check()
            _stage('spawn');handle=self.os.launch_goal(request)
            _stage('barrier')
            while not self.os.ready(handle,min(0.05,max(0,model_deadline-self.os.now()))):
                check()
                if self.os.now()>=model_deadline:raise GoalRuntimeBlocked('GOAL_STARTUP_DEADLINE')
            _stage('identity');identity=self.os.process(handle.process.pid)
            if identity is None or identity.uids!=(WORKER_UID,)*4:
                raise GoalRuntimeBlocked('GOAL_CHILD_IDENTITY_INVALID')
            if before_release is not None:before_release(identity)
            check()
            if self.os.now()>=model_deadline:raise GoalRuntimeBlocked('GOAL_STARTUP_DEADLINE')
            _stage('release');self.os.release(handle)
            _stage('transport')
            transport=BoundedStdioTransport(handle.process,recheck=check,deadline=model_deadline,
                clock=self.os.now,max_frame_bytes=min(65536,budget['max_output_bytes']),
                max_capture_bytes=min(1024*1024,budget['max_output_bytes']))
            _stage('turn');candidate=GoalAppServerRunner(enabled=True).run(request,transport,admit=admitted,recheck=check)
            if self.os.now()>=model_deadline:raise GoalRuntimeBlocked('GOAL_MODEL_DEADLINE')
            _stage('cleanup');self.stop_owned(reservation)
            _stage('quarantine');check();snapshot=self.os.collect_goal(request,binding,check)
            if type(snapshot) is not UntrustedGoalSnapshot or snapshot.binding!=binding:
                raise GoalRuntimeBlocked('GOAL_QUARANTINE_CONTRACT')
            check()
            _stage('persist')
            if quarantine(snapshot) is not None:raise GoalRuntimeBlocked('GOAL_QUARANTINE_NOT_DURABLE')
            _stage('postcheck');check()
            return PendingGoalResult(candidate,self.os.now()-started,snapshot.digest)
        except BaseException as exc:
            detail=_failure_detail(exc)
            _report_failure(exc)
            self.stop_owned(reservation)
            # Only a returned bounded stop permits the manager failure ACK.
            # Process-control exceptions and uncertain cleanup still exit the
            # namespace immediately. No allocation-empty claim is made here.
            if isinstance(exc,Exception):
                handled=exc if safe_goal_runtime_diagnostic(exc) is not None else GoalRuntimeBlocked(detail['reason'])
                _FAILURES[handled]=(detail['reason'],detail['stage'])
                _ACKNOWLEDGEABLE.add(handled)
                if handled is not exc:raise handled from None
            raise
        finally:
            if transport is not None:transport.close()
            if handle is not None:self.os.close_child(handle)
            # Remain armed; even a returned candidate is not reusable/publishable.
