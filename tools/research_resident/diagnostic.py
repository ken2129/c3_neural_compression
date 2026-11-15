"""Approved fixed model-free Docker diagnostic. Never a production entrypoint."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time

sys.path.insert(0,'/opt/service')
import resident_launcher as resident
import goal_launcher as common
from research_harness.resident_deployment import validate_diagnostic_deployment
from research_harness.resident_diagnostic_fixture import A,B,LocalSources,LocalWorkerAPI,failure_location
from research_harness.resident_runtime import ResidentLinuxBackend
from research_harness.goal_runtime import GoalLinuxUIDBackend
from research_harness.restart_boundary import read_epoch
from research_harness.schema import ContractError,canonical


def protected(path):
    if os.getresuid()[0]!=0:return common.protected_json(path)
    try:
        common.as_uid(10001)
        return common.protected_json(path)
    finally:common.as_uid(0)


def configure():
    if Path('/proc/self/attr/current').read_text().strip()!='docker-default (enforce)':
        raise ContractError('diagnostic AppArmor enforcement missing')
    resident.SCRIPT='/opt/service/resident_diagnostic.py'
    resident.validate_resident_deployment=validate_diagnostic_deployment
    ResidentLinuxBackend.verify_deployment=staticmethod(validate_diagnostic_deployment)
    from research_harness import worker_pr,direct_pr_runtime
    worker_pr.WorkerGitHub=direct_pr_runtime.WorkerGitHub=LocalWorkerAPI
    profile=protected('/control/goal-runtime.json')
    source=LocalSources(profile['deployment_id'])
    resident.ManagerGitHub=lambda:source
    resident.GitHubOwnerComments=lambda:source
    original_cleanup=resident.cleanup_job
    def checked_cleanup(supervisor):
        original_cleanup(supervisor)
        if not supervisor.os.children_empty or os.listdir('/work'):
            raise ContractError('diagnostic cleanup proof missing')
        resident.report(dict(phase='RESIDENT_DIAGNOSTIC_JOB_CLEANUP',worker=0,
            descendants_echild=True,workspace_empty=True,temporary_dummy_auth_removed=True))
    resident.cleanup_job=checked_cleanup


def observe():
    """PID1-only passive observation, no extra process disturbing reserve()."""
    deployment=protected('/control/goal-deployment.json')
    ident=deployment['inspect']['Id'];before=read_epoch(container_id=ident)
    backend=GoalLinuxUIDBackend();baseline={};reports=[];trace=[];cleaned=0;done=False
    original_write=common.write_frame
    original_start=resident._start_runner
    def start():
        child=original_start();baseline['runner']=backend.process(child.pid)
        return child
    resident._start_runner=start

    def marker():
        try:
            common.as_uid(10001)
            path=Path('/control-ipc/diagnostic-two-cleaned')
            with path.open('xb') as out:out.write(b'FIXTURE\n')
            path.chmod(0o444)
        finally:common.as_uid(0)

    def finish():
        after=read_epoch(container_id=ident)
        manager=backend.process(baseline['manager'].pid)
        runner=backend.process(baseline['runner'].pid)
        same=lambda a,b:a is not None and b is not None and (a.pid,a.start_ticks,a.uids)==(b.pid,b.start_ticks,b.uids)
        checks=dict(pid1_unchanged=before==after,manager_unchanged=same(manager,baseline['manager']),
            runner_unchanged=same(runner,baseline['runner']),worker_zero=not any(p.uids==(10002,)*4 for p in backend.processes()),
            workspace_empty=not os.listdir('/work'),cleanup_count_two=cleaned==2,
            failure_handshake_order=trace[:5]==['approved_false','goal_failure','failure_ack','job_finished','cleaned'])
        until=time.monotonic()+2
        try:
            common.as_uid(10003)
            path=Path('/runner/delivery-result.json')
            while not path.exists() and time.monotonic()<until:time.sleep(.01)
            delivery=json.loads(path.read_bytes())
        finally:common.as_uid(0)
        checks['receipt_ack_statuses']=delivery==['enqueued','enqueued','duplicate','enqueued','enqueued']
        try:
            common.as_uid(10001)
            with sqlite3.connect('file:/control/state/state.sqlite?mode=ro',uri=True) as db:
                db.execute('PRAGMA query_only=ON')
                rows=db.execute('SELECT issue,phase,result FROM service_runs ORDER BY issue').fetchall()
            with sqlite3.connect('file:/control/inbox.sqlite?mode=ro',uri=True) as db:
                db.execute('PRAGMA query_only=ON')
                events=db.execute('SELECT phase FROM events').fetchall()
            checks.update(ledger_phases=[(r[0],r[1]) for r in rows]==[(A,'BLOCKED'),(B,'WORKER_FINISHED')],
                cancelled_result_absent=len(rows)==2 and rows[0][2] is None,
                completed_result_saved=len(rows)==2 and rows[1][2] is not None and json.loads(rows[1][2])['outcome']=='completed',
                no_reported_pr=len(rows)==2 and rows[1][2] is not None and json.loads(rows[1][2])['pr_url'] is None,
                claim_count_two=len(list(Path('/control/state/claims').iterdir()))==2,
                inbox_four_finished=events==[('FINISHED',)]*4)
            for name in ('goal-runtime.json','worker-pr.json','worker-github-token','codex-auth.json'):
                info=Path('/control',name).lstat()
                checks['private_'+name.replace('.','_').replace('-','_')]=(info.st_uid,info.st_gid,info.st_mode&0o777)==(10001,10001,0o600)
            if not all(checks.values()):raise ContractError('diagnostic invariant failed')
            result=dict(phase='RESIDENT_DIAGNOSTIC_PASSED',fixture_only=True,model_started=False,
                github_api_used=False,checks=checks,ipc_trace=trace,job_reports=reports,
                pid1_start_ticks=after.pid1_start_ticks,manager_pid=manager.pid,runner_pid=runner.pid)
            fd=os.open('/control/diagnostic-result.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as out:
                out.write(canonical(result));out.flush();os.fsync(out.fileno())
        finally:common.as_uid(0)
        original_write(sys.stdout.buffer,result)

    def write(stream,value):
        nonlocal cleaned,done
        original_write(stream,value)
        if done:return
        if set(value)=={'epoch','initialize_new_state'}:
            managers=[p for p in backend.processes() if p.uids==(10001,)*4]
            if len(managers)!=1:raise ContractError('diagnostic manager identity ambiguous')
            baseline['manager']=managers[0]
        if cleaned==0:
            if value.get('approved') is False:trace.append('approved_false')
            elif set(value)=={'goal_failure'}:trace.append('goal_failure')
            elif value.get('op') in ('failure_ack','job_finished'):trace.append(value['op'])
            elif value=={'cleaned':True}:trace.append('cleaned')
        if value.get('phase')=='RESIDENT_JOB_CLEANED':
            cleaned+=1
            if cleaned==2:marker()
        if value.get('mode')=='resident-direct-pr':
            reports.extend(value['reports'])
            if len(reports)==4:
                if [r['phase'] for r in reports]!=['BLOCKED','WORKER_FINISHED','BLOCKED','WORKER_FINISHED']:
                    raise ContractError('diagnostic report phases differ')
                if not all(r.get('duplicate') is True and r['dispatched'] is False for r in reports[2:]):
                    raise ContractError('diagnostic duplicate dispatched')
                finish();done=True
    common.write_frame=write


if __name__=='__main__':
    try:
        configure()
        if os.getpid()==1:observe()
        resident.main()
    except BaseException as exc:
        if type(exc) is common.GoalStartupFailure:common.report_startup_failure('launcher',exc)
        reason=exc.code if type(exc) is resident.ResidentBlocked else 'DIAGNOSTIC_CONTRACT_REJECTED'
        common.write_frame(sys.stderr.buffer,dict(phase='RESIDENT_DIAGNOSTIC_BLOCKED',reason=reason,
            error_class='permission' if isinstance(exc,PermissionError) else 'contract' if isinstance(exc,ContractError) else 'other',
            **failure_location(exc,sys.argv[1:])))
        os._exit(125)
