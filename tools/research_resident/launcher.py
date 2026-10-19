"""One explicitly installed resident PID1, manager and Actions runner.

Only the worker is per-job. No Issue listing or recovery scan exists here.
The old goal/canary entrypoints and their namespace-release rule are unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, '/opt/service')
import goal_launcher as common
from research_harness.actions_inbox import (Inbox, ReceiptServer, SOCKET_PATH,
    validate_policy, validate_receipt, IntakeBlocked)
from research_harness.direct_pr_service import DirectPRController, DirectPRResult
from research_harness.goal_plan import DIRECT_PR_KIND, validate_goal_plan
from research_harness.goal_runner import GoalRequest
from research_harness.goal_runtime import (GoalRunBinding, GoalLinuxUIDBackend, GoalRuntimeBlocked,
    safe_goal_runtime_diagnostic)
from research_harness.manager_github import ManagerGitHub
from research_harness.manager_runtime import safe_recheck_failure, require_recheck_reply
from research_harness.owner_approval import GitHubOwnerComments
from research_harness.resident_deployment import validate_resident_deployment
from research_harness.resident_runtime import (ResidentGoalSupervisor, ResidentLinuxBackend,
    cleanup_job, persist_block, BLOCK_MARKER, RUNNER_UID, ResidentBlocked)
from research_harness.restart_boundary import RestartEpoch, read_epoch
from research_harness.schema import ContractError, canonical
from research_harness.service_state import ServiceState

SCRIPT = '/opt/service/resident_launcher.py'


def report(value):
    common.write_frame(sys.stderr.buffer, value)


class ResidentHandshakeBlocked(IntakeBlocked):
    def __init__(self):
        super().__init__('CLEANUP_UNVERIFIED')


def read_failure_frame(stream, *, timeout=10.0):
    """Bound the full line, including EOF/partial writes, without prefetch.

    Resident IPC readers use raw pipes throughout. This cannot skip bytes held
    by an earlier BufferedReader or consume the subsequent cleaned response.
    The independent job watchdog is neither disarmed nor extended here.
    """
    deadline=time.monotonic()+timeout
    data=bytearray()
    while len(data)<32768:
        remaining=deadline-time.monotonic()
        if remaining<=0 or not select.select([stream],[],[],remaining)[0]:
            raise ResidentHandshakeBlocked()
        piece=os.read(stream.fileno(),1)
        if not piece:raise ResidentHandshakeBlocked()
        data.extend(piece)
        if piece==b'\n':
            value=common.loads(bytes(data))
            if type(value) is not dict:raise ResidentHandshakeBlocked()
            return value
    raise ResidentHandshakeBlocked()


class ResidentGoalPipeRuntime(common.GoalPipeRuntime):
    """Settle a denied recheck before the controller commits its BLOCKED ACK.

    The disposable runtime can return immediately because it exits its whole
    namespace. Reuse requires consuming the job's terminal failure frame first.
    Approval checks and the negative reply itself still use the common runtime.
    """
    def run(self,binding,recheck):
        rejected=False
        def checked():
            nonlocal rejected
            try:recheck()
            except Exception:
                rejected=True
                raise
        try:
            return super().run(binding,checked)
        except Exception:
            if rejected:
                self.failed_issue=None  # No failure_ack until goal_failure arrived.
                try:
                    frame=read_failure_frame(self.incoming)
                    if set(frame)!={'goal_failure'}:raise ResidentHandshakeBlocked()
                    detail=common.validated_goal_failure(frame['goal_failure'])
                    if detail is None:raise ResidentHandshakeBlocked()
                except Exception:
                    raise ResidentHandshakeBlocked() from None
                self.failed_issue=binding['issue_number']
                report(dict(phase='GOAL_MANAGER_BLOCKED',issue=self.failed_issue,**detail))
            raise


def acknowledge_goal_failure(incoming,outgoing,issue,detail,*,cleanup):
    """Job side: prove cleanup, send failure, require durable BLOCKED ACK."""
    if common.validated_goal_failure(detail) is None:
        raise ContractError('resident failure diagnostic rejected')
    cleanup()
    common.write_frame(outgoing,{'goal_failure':detail})
    if common.read_failure_ack(incoming)!={'op':'failure_ack','issue':issue,'phase':'BLOCKED'}:
        raise ContractError('resident failure not durably acknowledged')


def send_cleaned(outgoing,*,cleanup):
    cleanup()
    signal.alarm(0)
    common.write_frame(outgoing,{'cleaned':True})


def process_event(controller, receipt, *, finish_runtime):
    """Formal controller first; cleanup ACK before marking delivery finished.

    Exceptions are classified only after the existing controller had its chance
    to commit BLOCKED. No claim or owner receipt is reset by notification code.
    """
    runtime = controller.runtime
    try:
        answer = controller.process(receipt['issue_number'])
    except ResidentHandshakeBlocked:
        # No failure_ack/job_finished after a missing or invalid goal_failure.
        # The manager blocks the inbox and PID1 performs fail-closed shutdown.
        raise
    except Exception as exc:
        if runtime.failed_issue is not None:
            row = controller.state.get(runtime.failed_issue)
            if row is None or row['phase']!='BLOCKED':
                raise ResidentHandshakeBlocked() from None
            common.write_frame(runtime.outgoing, {'op': 'failure_ack',
                'issue': runtime.failed_issue, 'phase': 'BLOCKED'})
        row = controller.state.get(receipt['issue_number'])
        answer = dict(issue=receipt['issue_number'], phase=row['phase'] if row else 'NOT_APPROVED',
                      dispatched=False, reason=safe_recheck_failure(exc)['reason'])
    finish_runtime()
    return {key: value for key, value in answer.items() if key in (
        'issue', 'phase', 'dispatched', 'duplicate', 'outcome', 'pr_url', 'reason')}


def manager():
    if os.getresuid() != (10001,) * 3:
        raise ContractError('resident manager UID required')
    os.umask(0o077)
    incoming=sys.stdin.buffer.raw
    hello = common.read_frame(incoming)
    epoch = RestartEpoch.from_dict(hello['epoch'])
    profile = common.protected_json('/control/goal-runtime.json')
    policy = validate_policy(common.protected_json('/control/resident-intake.json'))
    root = common.initialize_manager_state(hello, epoch)
    state = ServiceState(root, require_epochs=True)
    inbox = Inbox('/control/inbox.sqlite')
    server = ReceiptServer(SOCKET_PATH, inbox, policy)
    server.start()
    try:
        # A prior uncertain attempt is evidence, never permission for a retry.
        if state.db.execute("SELECT 1 FROM service_runs WHERE phase='CLAIMED'").fetchone():
            inbox.block()
            report(dict(phase='RESIDENT_BLOCKED', reason='UNCERTAIN_PRIOR_CLAIM'))
        else:
            inbox.recover()  # Queued deliveries only; no GitHub discovery.
        report(dict(phase='RESIDENT_MANAGER_READY', intake_enabled=policy['enabled'], issue_polling=False))
        while True:
            if not server.thread.is_alive():
                raise IntakeBlocked('IPC_FAILED')
            try:
                receipt = inbox.take() if policy['enabled'] else None
            except IntakeBlocked:
                # Keep roles alive but refuse all new receipts after a latch.
                time.sleep(1)
                continue
            if receipt is None:
                inbox.wake.clear()
                receipt = inbox.take() if policy['enabled'] else None
                if receipt is None:
                    inbox.wake.wait()
                    continue
            validate_receipt(receipt, policy)
            runtime = ResidentGoalPipeRuntime(incoming, sys.stdout.buffer, epoch, profile)
            controller = DirectPRController(state, ManagerGitHub(), GitHubOwnerComments(), runtime, None,
                profile=profile, worker_pr_config=common.protected_json('/control/worker-pr.json'),
                source_sha=profile['input_source_sha'], enabled=profile['enabled'],
                resource_limits=profile['resource_limits'], deployment_id=profile['deployment_id'],
                installed_plan_digest=profile['installed_plan_digest'])
            def finish():
                common.write_frame(sys.stdout.buffer, {'op': 'job_finished'})
                if common.read_frame(incoming) != {'cleaned': True}:
                    inbox.block()
                    raise IntakeBlocked('CLEANUP_UNVERIFIED')
            try:
                answer = process_event(controller, receipt, finish_runtime=finish)
            except ResidentHandshakeBlocked:
                inbox.block()
                report(dict(phase='RESIDENT_BLOCKED',reason='FAILURE_HANDSHAKE_UNVERIFIED'))
                raise
            inbox.finish(receipt['receipt_id'], answer)
            common.write_frame(sys.stdout.buffer, {'op': 'report', 'reports': [answer]})
    finally:
        server.close()
        state.close()


def _runner_ready():
    try:
        common.as_uid(RUNNER_UID)
        return Path('/runner/.registration-ready').exists()
    finally: common.as_uid(0)


def _start_runner():
    """Registration is a separate authorized operation, never an implicit login."""
    if not _runner_ready():
        report(dict(phase='RESIDENT_WAITING_FOR_RUNNER', intake_enabled=False))
        while not _runner_ready():
            time.sleep(1)  # Local setup state only; no GitHub Issue polling.
    binary = Path('/runner/bin/Runner.Listener')
    try:
        common.as_uid(RUNNER_UID)
        marker = Path('/runner/.registration-ready').lstat()
        if (not stat.S_ISREG(marker.st_mode) or marker.st_uid != RUNNER_UID
                or stat.S_IMODE(marker.st_mode) != 0o600 or marker.st_nlink != 1):
            raise ContractError('runner registration marker rejected')
        for name in ('.runner', '.credentials', '.credentials_rsaparams'):
            info = Path('/runner', name).lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != RUNNER_UID
                    or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 65536):
                raise ContractError('private pre-registered runner files required')
        info = binary.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != RUNNER_UID or info.st_mode & 0o022:
            raise ContractError('dedicated runner executable required')
        for name in ('home', '_temp', '_diag'):
            path = Path('/runner', name)
            path.mkdir(mode=0o700, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != RUNNER_UID or info.st_mode & 0o077:
                raise ContractError('runner runtime directory rejected')
        # Private runner logs are never relayed to manager output.
        fd = os.open('/runner/_diag/resident-output.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    finally: common.as_uid(0)
    # Open the log as its owner, then restore root before Popen performs its
    # permanent setgroups/setgid/setuid. UID10003 has no SETGID capability.
    with os.fdopen(fd, 'ab', buffering=0) as output:
        # CPython chdir(cwd) precedes setgid/setuid. Root has no DAC override
        # here, so cwd=/runner (UID10003,0700) would fail before the UID drop.
        # Start a fixed trusted bootstrap in /, then chdir/exec as UID10003.
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c',
            "import sys;sys.path.insert(0,'/opt/service');from research_harness.resident_runner import main;main()"],
            user=RUNNER_UID, group=RUNNER_UID, extra_groups=[], cwd='/',
            env=dict(PATH='/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin', LANG='C.UTF-8', HOME='/runner/home',
                     TMPDIR='/runner/_temp', DOTNET_PROCESSOR_COUNT='1'),
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, close_fds=True)
    return child


def job_supervise():
    """Per-job subreaper: only Codex descendants are children of this process."""
    from research_harness.resident_runtime import install_subreaper, require_job_supervisor
    install_subreaper(); require_job_supervisor()
    incoming=sys.stdin.buffer.raw
    boot = common.read_frame(incoming)
    if set(boot) != {'plan', 'controller_pid', 'runner_pid'}:
        raise ContractError('resident job bootstrap rejected')
    try:
        common.as_uid(10001)
        profile = common.protected_json('/control/goal-runtime.json')
    finally: common.as_uid(0)
    plan = validate_goal_plan(boot['plan'], profile['input_source_sha'],
        resource_limits=profile['resource_limits'], deployment_id=profile['deployment_id'])
    if plan['kind'] != DIRECT_PR_KIND: raise ContractError('resident direct-PR-only')
    common.require_installed_plan(profile, plan)
    backend = ResidentLinuxBackend()
    supervisor = ResidentGoalSupervisor(backend, controller_pid=boot['controller_pid'], runner_pid=boot['runner_pid'])
    supervisor.reserve()
    backend.arm_watchdog(plan['max_seconds'] + 30)
    try:
        common.stage_workspace(plan)
    except Exception:
        common.write_frame(sys.stdout.buffer, {'prepared': False})
        report(dict(phase='RESIDENT_PRECLAIM_BLOCKED', reason='STAGING_REJECTED'))
    else:
        common.write_frame(sys.stdout.buffer, {'prepared': True})
    attempted = preflight = False
    while True:
        frame = common.read_frame(incoming)
        if frame == {'op': 'preflight'}:
            try:
                backend.admit_goal(GoalRequest(plan, profile['input_source_sha']))
            except Exception:
                report(dict(phase='RESIDENT_PRECLAIM_BLOCKED', reason='ADMISSION_REJECTED'))
                common.write_frame(sys.stdout.buffer, {'ready': False})
            else:
                preflight = True
                common.write_frame(sys.stdout.buffer, {'ready': True})
        elif set(frame) == {'op', 'binding'} and frame['op'] == 'goal':
            if attempted or not preflight: raise ContractError('resident preflight order')
            attempted = True
            supervisor = ResidentGoalSupervisor(backend, controller_pid=boot['controller_pid'], runner_pid=boot['runner_pid'])
            binding = frame['binding']; common.validate_binding(binding)
            request = GoalRequest(plan, profile['input_source_sha'])
            if binding['plan_digest'] != request.digest or binding['source_sha'] != request.source_sha:
                raise ContractError('resident binding mismatch')
            def recheck():
                common.write_frame(sys.stdout.buffer, {'op': 'recheck'})
                require_recheck_reply(common.read_frame(incoming))
            try:
                pending = supervisor.run_goal(request, recheck,
                    binding=GoalRunBinding(request.source_sha, request.digest, binding['request_id'], binding['run_id']),
                    quarantine=lambda _: (_ for _ in ()).throw(ContractError('resident private publisher disabled')),
                    enabled=True)
            except Exception as exc:
                detail = safe_goal_runtime_diagnostic(exc)
                # Includes approval failure before run_goal reserved/spawned.
                # The disposable path's ACK flag alone is insufficient here;
                # this resident path must prove ECHILD and erase its workspace.
                if detail is None:
                    detail = safe_goal_runtime_diagnostic(GoalRuntimeBlocked('GOAL_RUNTIME_FAILURE'))
                acknowledge_goal_failure(incoming,sys.stdout.buffer,binding['issue_number'],detail,
                    cleanup=lambda:cleanup_job(supervisor))
            else:
                value = DirectPRResult(binding, plan['model'], pending.candidate.outcome,
                    pending.candidate.summary, pending.candidate.pr_url, pending.elapsed_seconds)
                common.write_frame(sys.stdout.buffer, {'direct_pr_result': value.to_dict()})
        elif frame == {'op': 'job_finished'}:
            send_cleaned(sys.stdout.buffer,cleanup=lambda:cleanup_job(supervisor))
            return
        else:
            raise ContractError('resident job frame rejected')


def supervise():
    try:
        common.as_uid(10001)
        blocked = Path(BLOCK_MARKER).exists()
    finally: common.as_uid(0)
    if blocked:
        report(dict(phase='RESIDENT_BLOCKED', reason='CLEANUP_UNVERIFIED'))
        while True: signal.pause()
    config, profile, initialize_new = common.deployment(validator=validate_resident_deployment)
    if 'direct_pr_scope' not in profile:
        raise ContractError('resident mode is explicitly direct-PR-only')
    epoch = read_epoch(container_id=config['container_id'])
    runner = _start_runner()
    control = subprocess.Popen([sys.executable, '-I', '-B', SCRIPT, 'manager'],
        user=10001, group=10001, extra_groups=[],
        env=dict(LANG='C.UTF-8', PATH='/usr/local/bin:/usr/bin:/bin', HOME='/control/home', GH_CONFIG_DIR='/control/gh'),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True, bufsize=0)
    common.write_frame(control.stdin, {'epoch': epoch.to_dict(), 'initialize_new_state': initialize_new})
    job = None
    job_deadline = None
    while True:
        if runner.poll() is not None or control.poll() is not None:
            raise ResidentBlocked('TRUSTED_ROLE_EXITED')
        if job is not None:
            if job.poll() not in (None, 0):
                raise ResidentBlocked('JOB_EXITED_UNCLEAN', child_exit=job.returncode)
            if time.monotonic() >= job_deadline:
                raise ResidentBlocked('JOB_DEADLINE')
        watched = [control.stdout] + ([job.stdout] if job is not None else [])
        readable = select.select(watched, [], [], .25)[0]
        for stream in readable:
            frame = common.read_frame(stream)
            if stream is control.stdout:
                if set(frame) == {'op', 'plan'} and frame['op'] == 'prepare_goal':
                    if job is not None: raise ContractError('resident overlap')
                    plan = validate_goal_plan(frame['plan'], profile['input_source_sha'],
                        resource_limits=profile['resource_limits'], deployment_id=profile['deployment_id'])
                    common.require_installed_plan(profile, plan)
                    if plan['kind'] != DIRECT_PR_KIND: raise ContractError('resident direct-PR-only')
                    job = subprocess.Popen([sys.executable, '-I', '-B', SCRIPT, 'job'],
                        env=dict(LANG='C.UTF-8', PATH='/usr/local/bin:/usr/bin:/bin'),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, close_fds=True, bufsize=0)
                    job_deadline = time.monotonic() + plan['max_seconds'] + 35
                    common.write_frame(job.stdin, {'plan': plan, 'controller_pid': control.pid, 'runner_pid': runner.pid})
                elif set(frame) == {'op', 'reports'} and frame['op'] == 'report':
                    common.write_frame(sys.stdout.buffer, {'mode': 'resident-direct-pr', 'reports': frame['reports']})
                elif job is not None and (frame in ({'op':'preflight'}, {'op':'job_finished'})
                        or set(frame)=={'op','binding'} and frame['op']=='goal'
                        or set(frame)=={'approved'} or set(frame)=={'approved','reason','origin'}
                        or set(frame)=={'op','issue','phase'} and frame['op']=='failure_ack'):
                    if frame.get('op') == 'goal': job_deadline = time.monotonic() + plan['max_seconds'] + 5
                    common.write_frame(job.stdin, frame)
                elif frame == {'op': 'job_finished'} and job is None:
                    if os.listdir('/work') or any(p.uids == (10002,) * 4 for p in GoalLinuxUIDBackend().processes()):
                        raise ResidentBlocked('WORKSPACE_REMAINED')
                    common.write_frame(control.stdin, {'cleaned': True})
                else:
                    raise ContractError('resident manager frame rejected')
            else:
                if frame == {'cleaned': True}:
                    if job.wait(timeout=3) != 0:
                        raise ResidentBlocked('JOB_EXITED_UNCLEAN', child_exit=job.returncode)
                    if any(p.uids == (10002,) * 4 for p in GoalLinuxUIDBackend().processes()):
                        raise ResidentBlocked('WORKER_REMAINED')
                    job.stdin.close(); job.stdout.close(); job = None
                    common.write_frame(control.stdin, {'cleaned': True})
                    report(dict(phase='RESIDENT_JOB_CLEANED', worker=0, descendants_echild=True,
                                temporary_credentials_removed=True))
                elif (set(frame) in ({'prepared'}, {'ready'}, {'goal_failure'}, {'direct_pr_result'})
                        or frame == {'op':'recheck'}):
                    common.write_frame(control.stdin, frame)
                else:
                    raise ContractError('resident job frame rejected')


def main():
    if sys.argv[1:] == ['manager']:
        manager()
    elif sys.argv[1:] == ['job']:
        job_supervise()
    elif not sys.argv[1:]:
        common.install_pid1_shutdown_handlers()
        supervise()
    else:
        raise ContractError('resident argv rejected')


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        # Cleanup uncertainty or loss of a trusted role must not permit another
        # job. The last resort exits only this verified dedicated PID namespace.
        if os.getpid() == 1 and os.getresuid()[0] == 0:
            try: persist_block()
            finally:
                if type(exc) is common.GoalStartupFailure:
                    common.report_startup_failure('launcher', exc)
                reason = exc.code if type(exc) is ResidentBlocked else 'CLEANUP_UNVERIFIED'
                code = exc.child_exit if type(exc) is ResidentBlocked else None
                report(dict(phase='RESIDENT_BLOCKED', reason=reason, child_exit_code=code))
                os._exit(125)
        role = 'job' if sys.argv[1:] == ['job'] else 'manager'
        report(dict(phase='RESIDENT_ROLE_FAILED', reason='ROLE_FAILED', role=role))
        os._exit(125)
