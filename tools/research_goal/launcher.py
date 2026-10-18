"""Independent goal-only PID1/manager; disabled absent explicit protected profile.

Only private frozen candidates cross the pipe. Worker-readable model auth and
unrestricted bridge egress remain approved risks, not a sandbox guarantee.
A returned result always requires a new verified PID namespace before review
and publication. No ambient credentials, old canary state or alternate runtime
are used as fallback. This entrypoint never runs copied artifact code.
"""
import hashlib
import json
import io
import os
import re
from pathlib import Path
import signal
import stat
from contextlib import contextmanager
import select
import subprocess
import sys
import time
sys.path.insert(0, '/opt/service')

from research_harness.schema import ContractError, canonical, loads, fields, identifier
from research_harness.goal_deployment import validate_goal_deployment
from research_harness.goal_plan import validate_goal_plan
from research_harness.goal_runtime import (GoalUIDSupervisor, GoalLinuxUIDBackend,
    GoalRunBinding, UntrustedGoalSnapshot, CONSENTS, safe_goal_runtime_diagnostic, goal_failure_can_acknowledge)
from research_harness.goal_runner import GoalRequest
from research_harness.goal_result import GoalResult
from research_harness.goal_artifacts import (GoalEntry, GoalChange, GoalChangeCandidate,
    GoalWorkspaceSnapshot, snapshot_goal_workspace)
from research_harness.manager_runtime import (require_recheck_reply,
    RecheckRejected, safe_recheck_failure)
from research_harness.restart_boundary import read_epoch, RestartEpoch
from research_harness.service_state import ServiceState, initialize, validate_binding
from research_harness.persistent_fixture import private, exclusive_record, sync_directory
from research_harness.uid_supervisor import report_failure
from research_harness.model_deployment import read_private, validate_chatgpt_auth

CONTROL = 10001
WORKER = 10002
SCRIPT = '/opt/service/goal_launcher.py'
MAX_RECORD = 128 * 1024 * 1024


STARTUP_STAGES = frozenset(('identity', 'control_directory', 'profile',
    'deployment_manifest', 'deployment_validation', 'installed_identity', 'epoch',
    'manager_start', 'manager_hello', 'manager_profile', 'manager_initialize', 'manager_state'))


class GoalStartupFailure(ContractError):
    """Fixed-vocabulary startup metadata, with no external exception text."""
    def __init__(self, stage, exc):
        super().__init__('goal startup rejected')
        self.stage = stage if stage in STARTUP_STAGES else 'identity'
        if isinstance(exc, PermissionError): self.reason = 'PERMISSION_DENIED'
        elif isinstance(exc, FileNotFoundError): self.reason = 'FILE_MISSING'
        elif isinstance(exc, ContractError): self.reason = 'CONTRACT_REJECTED'
        elif isinstance(exc, OSError): self.reason = 'IO_FAILED'
        elif isinstance(exc, (ValueError, KeyError, TypeError)): self.reason = 'DATA_INVALID'
        else: self.reason = 'STARTUP_FAILED'


@contextmanager
def startup_stage(stage):
    try:
        yield
    except GoalStartupFailure:
        raise
    except Exception as exc:
        raise GoalStartupFailure(stage, exc) from None


def report_startup_failure(role, exc):
    # Never trust arbitrary exception attributes, messages, paths or JSON values.
    value = exc if type(exc) is GoalStartupFailure else GoalStartupFailure('identity', exc)
    print(json.dumps({'phase': 'GOAL_BLOCKED_STARTUP',
        'role': role if role in ('launcher', 'manager') else 'launcher',
        'stage': value.stage, 'reason': value.reason}), file=sys.stderr, flush=True)


@contextmanager
def preclaim_stage(stage):
    """Keep closed diagnostics before a claim, without trusting exception text.

    These failures still propagate to mandatory PID1 exit. They neither create
    a claim nor acknowledge a model attempt or permit reuse of this allocation.
    """
    stage = stage if stage in ('plan', 'staging', 'admission', 'resources', 'reserve') else 'preflight'
    try:
        yield
    except BaseException as exc:
        detail = safe_goal_runtime_diagnostic(exc)
        reason = (detail['reason'] if detail is not None else
                  'GOAL_TIMEOUT' if isinstance(exc, TimeoutError) else
                  'GOAL_OS_FAILURE' if isinstance(exc, OSError) else
                  'GOAL_CONTRACT_FAILURE' if isinstance(exc, ContractError) else 'GOAL_RUNTIME_FAILURE')
        try:
            print(json.dumps(dict(phase='GOAL_PRECLAIM_BLOCKED', stage=stage, reason=reason)),
                  file=sys.stderr, flush=True)
        except Exception:
            pass  # Diagnostics must not replace the original rejection/exit.
        raise


def protected_control_directory(path):
    """Validate manager-owned directories during a temporary PID1 EUID drop.

    Real/saved UID remain root in PID1. Do not change the real-UID contract of
    persistent_fixture.private, which is also used by permanently dropped roles.
    """
    info = path.lstat()
    if (os.geteuid() != CONTROL or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != CONTROL):
        raise ContractError('goal control directory identity/type/permissions mismatch')


def read_frame(stream):
    line = stream.readline(32769)
    ending = b'\n' if type(line) is bytes else '\n'
    size = len(line) if type(line) is bytes else len(line.encode('utf-8'))
    if not line or not line.endswith(ending) or size > 32768:
        raise ContractError('invalid goal supervisor frame')
    value = loads(line)
    if type(value) is not dict: raise ContractError('goal frame object required')
    return value


def write_frame(stream, value):
    if type(value) is not dict: raise ContractError('goal frame object required')
    encoded = canonical(value).decode() + '\n'
    if len(encoded.encode()) > 32768: raise ContractError('goal supervisor frame too large')
    stream.write(encoded if isinstance(stream, io.TextIOBase) else encoded.encode('utf-8')); stream.flush()


def read_failure_ack(stream, timeout=5.0):
    """Bound the complete ACK line, including a manager dying mid-write.

    No other manager frame is expected while it waits for a model result, so
    this dedicated ACK uses the pipe descriptor without TextIO prefetch.
    The existing hard watchdog is neither changed nor extended.
    """
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) <= 1024:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise ContractError('goal failure acknowledgement timed out')
        piece = os.read(stream.fileno(), 1)
        if not piece: raise ContractError('goal failure acknowledgement closed')
        data.extend(piece)
        if piece == b'\n':
            try:
                value = loads(data.decode('utf-8'))
            except (UnicodeError, ValueError) as exc:
                raise ContractError('invalid goal failure acknowledgement') from exc
            if type(value) is not dict: raise ContractError('goal ACK object required')
            return value
    raise ContractError('goal failure acknowledgement too large')


def as_uid(uid):
    os.seteuid(0)
    os.setegid(uid)
    os.seteuid(uid)


def protected_json(path, limit=65536):
    return loads(read_private(Path(path), limit=limit))


def deployment():
    with startup_stage('identity'):
        if os.getresuid() != (0, 0, 0) or os.getpid() != 1:
            raise ContractError('goal requires dedicated root PID1')
    try:
        with startup_stage('control_directory'):
            as_uid(CONTROL)
            protected_control_directory(Path('/control'))
        with startup_stage('profile'):
            profile = protected_json('/control/goal-runtime.json')
            fields(profile, ('schema', 'enabled', 'deployment_id', 'input_source_sha', 'runtime_source_sha',
                             'model', 'resource_limits', 'risk_acknowledgements', 'installed_plan_digest'))
            if type(profile['installed_plan_digest']) is not str or re.fullmatch('[0-9a-f]{64}', profile['installed_plan_digest']) is None:
                raise ContractError('installed goal plan digest required')
            fields(profile['risk_acknowledgements'], CONSENTS)
            if (profile['schema'] != 'c3-goal-runtime-v1' or profile['enabled'] is not True
                    or any(profile['risk_acknowledgements'][key] is not True for key in CONSENTS)):
                raise ContractError('new goal execution remains disabled')
        with startup_stage('deployment_manifest'):
            data = protected_json('/control/goal-deployment.json', 1024*1024)
    finally:
        as_uid(0)
    with startup_stage('deployment_validation'):
        fields(data, ('inspect', 'consumers', 'volume_name', 'volume_inspect', 'initialize_new_state'))
        verified = validate_goal_deployment(data['inspect'], data['consumers'],
            runtime_source_sha=profile['runtime_source_sha'], input_source_sha=profile['input_source_sha'],
            volume_name=data['volume_name'], volume_inspect=data['volume_inspect'],
            resource_limits=profile['resource_limits'], deployment_id=profile['deployment_id'])
    with startup_stage('installed_identity'):
        if (Path('/opt/service/source-sha').read_text().strip() != verified['runtime_source_sha']
                or Path('/etc/hostname').read_text().strip() != verified['container_id'][:12]
                or type(data['initialize_new_state']) is not bool):
            raise ContractError('goal installed identity mismatch')
    return verified, profile, data['initialize_new_state']


def stage_workspace(plan):
    """Fresh known files before any worker exists; no CHOWN capability needed."""
    scopes = plan['write_scope']
    if (any(a != b and (a.startswith(b + '/') or b.startswith(a + '/')) for a in scopes for b in scopes)
            or any(p == s or p.startswith(s + '/') or s.startswith(p + '/') for p in plan['inputs'] for s in scopes)):
        raise ContractError('goal input and write scopes must not overlap')
    if os.listdir('/work'):
        raise ContractError('goal workspace must be fresh')
    os.umask(0o022)
    root = Path('/work/goal'); root.mkdir(mode=0o755)
    manifest = loads(Path('/opt/c3-runtime/goal-inputs.json').read_bytes())
    if manifest['source_sha'] != plan['source_sha']:
        raise ContractError('goal input revision mismatch')
    for path in plan['inputs']:
        raw = (Path('/opt/goal-inputs') / path).read_bytes()
        record = manifest['files'][path]
        if len(raw) != record['size_bytes'] or hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ContractError('goal pinned input mismatch')
        target = root / path; target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_bytes(raw); target.chmod(0o444)
    # Temporarily allow only the trusted launcher (no worker exists yet) to
    # create worker-owned directories through a UID switch. Then restore root
    # ownership mode before model admission. Never do this after dispatch.
    for relative in (*['goal/' + p for p in plan['write_scope']], 'worker-home', 'codex-home'):
        target = Path('/work') / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        parent_mode = target.parent.stat().st_mode & 0o777
        target.parent.chmod(0o777)
        try:
            as_uid(WORKER); target.mkdir(mode=0o700)
        finally:
            as_uid(0); target.parent.chmod(parent_mode)
    try:
        as_uid(CONTROL)
        auth = read_private(Path('/control/codex-auth.json'), limit=65536)
        validate_chatgpt_auth(auth)
        as_uid(WORKER)
        fd = os.open('/work/codex-home/auth.json', os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(auth); stream.flush(); os.fsync(stream.fileno())
        # There is no worker process yet. This is a real initial-state snapshot.
        baseline = snapshot_goal_workspace(root, source_sha=plan['source_sha'],
            write_scope=plan['write_scope'], inputs=plan['inputs'], worker_stopped=True,
            max_file_bytes=16*1024**2, max_total_bytes=16*1024**2)
    finally:
        as_uid(0)
    return baseline


def freeze_snapshot(snapshot, binding, baseline, plan):
    """Durably freeze immutable copies; never claim source-side quiescence."""
    if type(snapshot) is not UntrustedGoalSnapshot:
        raise ContractError('closed quarantine snapshot required')
    expected = GoalRunBinding(plan['source_sha'], binding['plan_digest'], binding['request_id'], binding['run_id'])
    if snapshot.binding != expected:
        raise ContractError('quarantine binding mismatch')
    entries = tuple(GoalEntry(item.path, item.kind, item.mode, item.content) for item in snapshot.entries)
    if (len({entry.path for entry in entries}) != len(entries)
            or sum(len(entry.content) for entry in entries) > plan['resource_budget']['max_output_bytes']):
        raise ContractError('quarantine duplicate path or output budget exceeded')
    value = dict(binding=binding, quarantine_digest=snapshot.digest,
                 entries=[entry.to_dict() for entry in entries])
    encoded = canonical(value)
    if len(encoded) > MAX_RECORD: raise ContractError('quarantine serialization too large')
    name = identifier(binding['run_id'])
    try:
        as_uid(CONTROL)
        directory = Path('/control/quarantine'); directory.mkdir(mode=0o700, exist_ok=True)
        protected_control_directory(directory)
        temporary = directory / (name + '.copying.json')
        final = directory / (name + '.snapshot.json')
        if final.exists(): raise ContractError('quarantine run already exists')
        exclusive_record(temporary, value)
        os.replace(temporary, final); sync_directory(directory)
        frozen = protected_json(final, MAX_RECORD)
        if canonical(frozen) != encoded: raise ContractError('quarantine durable copy mismatch')
    finally:
        as_uid(0)
    # Only frozen protected bytes are interpreted from here. Hash/path validation
    # says nothing about model truth or global worker cleanup; restart is pending.
    checked = tuple(GoalEntry.from_dict(entry) for entry in frozen['entries'])
    before = {entry.path: entry for entry in baseline.entries}
    after = {path: entry for path, entry in before.items()
             if not any(path == scope or path.startswith(scope + '/') for scope in plan['write_scope'])}
    for entry in checked:
        if entry.path in after: raise ContractError('duplicate quarantine path')
        after[entry.path] = entry
    final_snapshot = GoalWorkspaceSnapshot(plan['source_sha'], tuple(sorted(plan['write_scope'])),
        tuple(after[path] for path in sorted(after)), baseline.max_file_bytes,
        baseline.max_total_bytes, tuple(plan['inputs']))
    changes = tuple(GoalChange(path, before.get(path), after.get(path))
        for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path))
    candidate = GoalChangeCandidate(plan['source_sha'], tuple(sorted(plan['write_scope'])),
        baseline.snapshot_digest, final_snapshot.snapshot_digest, changes)
    return candidate


def require_installed_plan(profile, plan):
    expected = profile.get('installed_plan_digest')
    if (type(expected) is not str or re.fullmatch('[0-9a-f]{64}', expected) is None
            or hashlib.sha256(canonical(plan)).hexdigest() != expected):
        raise ContractError('goal differs from prepared operator task')


class GoalPipeRuntime:
    deployment_contract_verified = True

    def __init__(self, incoming, outgoing, epoch, profile):
        self.incoming = incoming; self.outgoing = outgoing; self.epoch = epoch
        self.profile = profile; self.plan = None; self.failed_issue = None

    def current_epoch(self): return self.epoch

    def prepare_plan(self, plan):
        self.plan = validate_goal_plan(plan, self.profile['input_source_sha'],
                                      resource_limits=self.profile['resource_limits'], deployment_id=self.profile['deployment_id'])
        require_installed_plan(self.profile, self.plan)
        write_frame(self.outgoing, {'op': 'prepare_goal', 'plan': self.plan})
        if read_frame(self.incoming) != {'prepared': True}: raise ContractError('goal preparation rejected')

    def preflight(self):
        write_frame(self.outgoing, {'op': 'preflight'})
        if read_frame(self.incoming) != {'ready': True}: raise ContractError('goal preflight rejected')
        return self.epoch

    def run(self, binding, recheck):
        validate_binding(binding)
        write_frame(self.outgoing, {'op': 'goal', 'binding': binding})
        while True:
            frame = read_frame(self.incoming)
            if frame == {'op': 'recheck'}:
                try: recheck()
                except BaseException as exc:
                    self.failed_issue = binding['issue_number']
                    write_frame(self.outgoing, dict(approved=False, **safe_recheck_failure(exc))); raise
                write_frame(self.outgoing, {'approved': True}); continue
            if set(frame) == {'goal_failure'}:
                detail = frame['goal_failure']
                fields(detail, ('reason', 'stage'))
                if any(type(detail[key]) is not str for key in detail):
                    raise ContractError('invalid goal failure diagnostic')
                # Root already logged the allowlisted reason. Never incorporate
                # arbitrary pipe strings in an exception or user-visible report.
                self.failed_issue = binding['issue_number']
                raise ContractError('goal runtime rejected the claimed attempt')
            if frame != {'goal_result': binding['run_id']}:
                raise ContractError('goal private result identity mismatch')
            raw = protected_json('/control/quarantine/' + identifier(binding['run_id']) + '.result.json', MAX_RECORD)
            result = GoalResult.from_dict(raw)
            for key in ('source_sha', 'plan_digest', 'request_id', 'run_id', 'issue_number'):
                if getattr(result, key) != binding[key]: raise ContractError('goal result binding mismatch')
            if result.model != self.plan['model']: raise ContractError('goal result model mismatch')
            return result


def initialize_manager_state(hello, epoch):
    root = Path('/control/state'); marker = Path('/control/state-initialized.json')
    if hello['initialize_new_state'] is True:
        if marker.exists(): raise ContractError('interrupted goal initialization requires reconciliation')
        exclusive_record(marker, {'container_id': epoch.container_id})
        root.mkdir(mode=0o700); initialize(root)
        path = Path('/control/goal-deployment.json'); data = protected_json(path, 1024*1024)
        if data['initialize_new_state'] is not True: raise ContractError('goal initialization intent changed')
        data['initialize_new_state'] = False
        temporary = Path('/control/goal-deployment-consumed.json')
        exclusive_record(temporary, data); os.replace(temporary, path); sync_directory(Path('/control'))
    else:
        private(marker)
        if loads(marker.read_bytes()) != {'container_id': epoch.container_id}:
            raise ContractError('goal state belongs to another container')
    return root


def manager():
    from research_harness.manager_github import ManagerGitHub
    from research_harness.owner_approval import GitHubOwnerComments, MissingApproval, RevokedApproval
    from research_harness.goal_publisher import GitHubGoalPublisher, GhGoalTransport, matching_goal_publication_receipt
    from research_harness.goal_service import GoalController
    from research_harness.service import source_failure_report, source_retry_delay
    with startup_stage('identity'):
        if os.getresuid() != (CONTROL,)*3: raise ContractError('goal manager identity mismatch')
    os.umask(0o077)
    with startup_stage('manager_hello'):
        hello = read_frame(sys.stdin); epoch = RestartEpoch.from_dict(hello['epoch'])
    with startup_stage('manager_profile'):
        profile = protected_json('/control/goal-runtime.json')
    with startup_stage('manager_initialize'):
        root = initialize_manager_state(hello, epoch)
    runtime = GoalPipeRuntime(sys.stdin, sys.stdout, epoch, profile)
    with startup_stage('manager_state'):
        state = ServiceState(root, require_epochs=True)
    try:
        transport = GhGoalTransport(enabled=True, manager_credentials_isolated=True, runtime_verified=True)
        comments = GitHubOwnerComments()
        def content_authorizer(request):
            try:
                matching_goal_publication_receipt(request, comments.comments(request.result.issue_number), time.time())
            except (MissingApproval, RevokedApproval):
                return False
            return True
        controller = GoalController(state, ManagerGitHub(), comments, runtime,
            lambda persist: GitHubGoalPublisher(transport, persist_state=persist),
            source_sha=profile['input_source_sha'], publication_base_getter=transport.base_sha,
            enabled=profile['enabled'], resource_limits=profile['resource_limits'], deployment_id=profile['deployment_id'],
            installed_plan_digest=profile['installed_plan_digest'],
            content_authorizer=content_authorizer)
        while True:
            try: reports = controller.once()
            except Exception as exc: reports = [source_failure_report(exc)]
            if runtime.failed_issue is not None:
                saved = state.get(runtime.failed_issue)
                write_frame(sys.stdout, {'op':'failure_ack', 'issue':runtime.failed_issue,
                                         'phase':saved['phase'] if saved else 'MISSING'})
                return
            for report in reports: write_frame(sys.stdout, {'op': 'report', 'reports': [report]})
            if controller.generation_attempted or any(r.get('restart_required') for r in reports):
                write_frame(sys.stdout, {'op': 'restart'}); return
            time.sleep(source_retry_delay(reports, now=time.time()))
    finally:
        state.close()


def blocked_startup():
    signal.signal(signal.SIGTERM, lambda *_: os._exit(125))
    signal.signal(signal.SIGINT, lambda *_: os._exit(125))
    while True: signal.pause()


def supervise():
    started = time.monotonic()
    try:
        config, profile, initialize_new = deployment()
        with startup_stage('epoch'):
            epoch = read_epoch(container_id=config['container_id'])
    except Exception as exc:
        report_startup_failure('launcher', exc); blocked_startup(); return
    with startup_stage('manager_start'):
        control = subprocess.Popen([sys.executable, '-I', '-B', SCRIPT, 'manager'],
            user=CONTROL, group=CONTROL, extra_groups=[],
            env={'LANG':'C.UTF-8', 'PATH':'/usr/local/bin:/usr/bin:/bin',
                 'HOME':'/control/home', 'GH_CONFIG_DIR':'/control/gh'},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=False, bufsize=0, close_fds=True)
        write_frame(control.stdin, {'epoch':epoch.to_dict(), 'initialize_new_state':initialize_new})
    plan = baseline = None; attempted = False; preflight_complete = False
    try:
        while True:
            frame = read_frame(control.stdout)
            if set(frame) == {'op', 'plan'} and frame['op'] == 'prepare_goal':
                if plan is not None or attempted: return  # fresh generation only
                with preclaim_stage('plan'):
                    plan = validate_goal_plan(frame['plan'], profile['input_source_sha'], resource_limits=profile['resource_limits'], deployment_id=profile['deployment_id'])
                    require_installed_plan(profile, plan)
                with preclaim_stage('staging'):
                    baseline = stage_workspace(plan)
                write_frame(control.stdin, {'prepared': True})
            elif frame == {'op':'preflight'}:
                if plan is None or attempted: raise ContractError('goal plan missing or reused')
                with preclaim_stage('admission'):
                    backend = GoalLinuxUIDBackend(); backend.admit_goal(GoalRequest(plan, profile['input_source_sha']))
                with preclaim_stage('resources'):
                    limits = backend.resources(); budget = plan['resource_budget']
                    if (limits['memory'] > budget['memory_bytes'] or limits['pids'] > budget['max_processes']
                            or limits['work_bytes'] > budget['max_work_bytes'] or plan['max_seconds'] <= 20):
                        raise ContractError('goal actual allocation exceeds approved request')
                with preclaim_stage('reserve'):
                    GoalUIDSupervisor(backend, controller_pid=control.pid).reserve()
                # Make Docker's restart policy active before any durable claim
                # or untrusted process can start; no post-failure grace sleep.
                time.sleep(max(0, 11-(time.monotonic()-started)))
                preflight_complete = True
                write_frame(control.stdin, {'ready': True})
            elif set(frame) == {'op', 'binding'} and frame['op'] == 'goal':
                if plan is None or baseline is None or attempted or not preflight_complete:
                    raise ContractError('goal allocation missing preflight or already consumed')
                attempted = True; binding = frame['binding']; validate_binding(binding)
                request = GoalRequest(plan, profile['input_source_sha'])
                if binding['source_sha'] != request.source_sha or binding['plan_digest'] != request.digest:
                    raise ContractError('goal runtime plan binding mismatch')
                def recheck():
                    write_frame(control.stdin, {'op':'recheck'})
                    require_recheck_reply(read_frame(control.stdout))
                frozen = []
                def quarantine(snapshot):
                    frozen.append(freeze_snapshot(snapshot, binding, baseline, plan))
                completed_run = False
                try:
                    pending = GoalUIDSupervisor(GoalLinuxUIDBackend(), controller_pid=control.pid).run_goal(
                        request, recheck, binding=GoalRunBinding(request.source_sha, request.digest,
                            binding['request_id'], binding['run_id']), quarantine=quarantine, enabled=profile['enabled'])
                    completed_run = True
                    if len(frozen) != 1: raise ContractError('goal frozen candidate missing')
                    result = GoalResult(**{key:binding[key] for key in ('request_id','run_id','issue_number','plan_digest','source_sha')},
                        model=plan['model'], candidate_outcome=pending.candidate.outcome,
                        candidate_summary=pending.candidate.summary, candidate=frozen[0], elapsed_seconds=pending.elapsed_seconds)
                    try:
                        as_uid(CONTROL)
                        exclusive_record(Path('/control/quarantine') / (binding['run_id']+'.result.json'), result.to_dict())
                    finally: as_uid(0)
                except Exception as exc:
                    detail = ({'reason':'GOAL_FINALIZATION_FAILED', 'stage':'persist'} if completed_run
                              else safe_goal_runtime_diagnostic(exc))
                    # Only runtime-known ordinary failures or protected result
                    # finalization errors enter this bounded BLOCKED handshake.
                    # Uncertain cleanup and hard watchdog exits retain an
                    # uncertain claim and terminate PID1 immediately.
                    if detail is None or (not completed_run and not goal_failure_can_acknowledge(exc)): raise
                    print(json.dumps({'mode':'approved-goal-candidate', 'phase':'GOAL_RUNTIME_FAILED',
                                      **detail}), file=sys.stderr, flush=True)
                    try:
                        write_frame(control.stdin, {'goal_failure':detail})
                    except BrokenPipeError:
                        # A manager-local approval failure can already have
                        # committed BLOCKED and sent its ACK before exiting.
                        pass
                    ack = read_failure_ack(control.stdout)
                    if ack != {'op':'failure_ack', 'issue':binding['issue_number'], 'phase':'BLOCKED'}:
                        raise ContractError('goal blocked state was not acknowledged')
                    return
                write_frame(control.stdin, {'goal_result':binding['run_id']})
            elif set(frame) == {'op','reports'} and frame['op'] == 'report':
                print(json.dumps({'mode':'approved-goal-candidate','reports':frame['reports']}, ensure_ascii=False), flush=True)
                if plan is not None and not attempted: return
            elif frame == {'op':'restart'}:
                return
            else:
                raise ContractError('unexpected goal manager operation')
    finally:
        control.stdin.close()
        # Every prepared/attempted allocation ends by PID1 exit, never reused.
        if not attempted:
            time.sleep(max(0, 11-(time.monotonic()-started)))


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['manager']: manager()
        elif not sys.argv[1:]: supervise()
        else: raise ContractError('unsupported goal launcher mode')
    except BaseException as exc:
        if type(exc) is GoalStartupFailure:
            report_startup_failure('manager' if sys.argv[1:] == ['manager'] else 'launcher', exc)
        else:
            report_failure('manager' if sys.argv[1:] == ['manager'] else 'launcher', 'entrypoint', exc)
        os._exit(125)
    os._exit(0)
