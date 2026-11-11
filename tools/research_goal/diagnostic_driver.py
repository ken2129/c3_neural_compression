"""Explicitly approved diagnostic only; not a historical Issue83 reproduction.
Production scanner/restart-release/publication are NOT exercised.
"""
import sys
sys.path.insert(0, '/opt/service')
import json, os, signal, time
from datetime import datetime, timezone
from pathlib import Path
import goal_launcher as launcher
import diagnostic_boundary
from diagnostic_observation import ShadowRead, observe_once
import research_harness.goal_deployment as deployment_module
from research_harness.schema import canonical, loads, ContractError
from research_harness.goal_runtime import safe_goal_runtime_diagnostic
from research_harness.goal_service import GoalController
from research_harness.goal_plan import validate_goal_plan
from research_harness.intake_flow_fixture import FixtureSource, FIXTURE_NUMBER
from research_harness.issue_intake import MARKER, content_hash
from research_harness.service_state import ServiceState
from research_harness.restart_boundary import RestartEpoch
from research_harness.persistent_fixture import exclusive_record
from research_harness.goal_result import GoalResult

import research_harness.goal_runtime as runtime_module

class ObservedTransport(runtime_module.BoundedStdioTransport):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.rpc_counts={}
    def send(self,value):
        method=value.get('method') if type(value) is dict else None
        if method in ('initialize','thread/start','turn/start'):
            self.rpc_counts[method]=self.rpc_counts.get(method,0)+1
            if self.rpc_counts[method]!=1:raise ContractError('diagnostic duplicate RPC blocked')
            report('DIAGNOSTIC_RPC_SENT',method=method,count=self.rpc_counts[method])
        return super().send(value)
    def receive(self):
        value=super().receive()
        if type(value) is dict and value.get('method') in ('turn/started','turn/completed'):
            report('DIAGNOSTIC_RPC_OBSERVED',method=value['method'])
        return value

SCRIPT='/opt/service/diagnostic_driver.py'
def configure_launcher():
    # Only this explicitly selected diagnostic entrypoint installs these hooks.
    launcher.SCRIPT=SCRIPT
    launcher.validate_goal_deployment=diagnostic_boundary.validate_goal_deployment
    deployment_module.validate_goal_deployment=diagnostic_boundary.validate_goal_deployment
    runtime_module.BoundedStdioTransport=ObservedTransport


def report(phase, **values):
    print(json.dumps(dict(phase=phase, **values)), file=sys.stderr, flush=True)


class SyntheticSource(FixtureSource):
    def __init__(self, plan):
        super().__init__()
        req=loads(self.issue['body'].split(MARKER)[1].split('\n```')[0])
        req['approval_ref']='approved-diag-'+plan['deployment_id']
        req['expires_at']=datetime.fromtimestamp(time.time()+240,timezone.utc).isoformat().replace('+00:00','Z')
        req['plan']=plan
        self.issue['body']='SYNTHETIC DIAGNOSTIC ONLY. Never posted to GitHub.'+MARKER+json.dumps(req)+'\n```'
        body=loads(self.comment['body'])
        body.update(request_id=req['approval_ref'],content_sha256=content_hash(self.issue),expires_at=req['expires_at'])
        self.comment['body']=json.dumps(body)


def make_controller(state, runtime, profile, plan, *, observe):
    checked=validate_goal_plan(plan,profile['input_source_sha'],
        resource_limits=profile['resource_limits'],deployment_id=profile['deployment_id'])
    if (checked['model']!='gpt-6.1-sol' or not 20<checked['max_seconds']<=180
            or profile['resource_limits']['max_seconds']>180):
        raise ContractError('diagnostic model or wall ceiling mismatch')
    # NOT inside GoalController or its five-second recheck budget. Construct the
    # fresh synthetic receipt after observing, so its lifetime is not consumed.
    observe_once(observe)
    fixture=SyntheticSource(checked)
    return GoalController(state,fixture,fixture,runtime,
        lambda *_: (_ for _ in ()).throw(ContractError('publication disabled in diagnostic')),
        source_sha=profile['input_source_sha'],
        publication_base_getter=lambda:profile['input_source_sha'],enabled=profile['enabled'],
        resource_limits=profile['resource_limits'],deployment_id=profile['deployment_id'],
        installed_plan_digest=profile['installed_plan_digest'],content_authorizer=lambda *_:False)


def manager():
    if os.getresuid()!=(10001,)*3:raise ContractError('diagnostic manager UID')
    os.umask(0o077)
    hello=launcher.read_frame(sys.stdin);epoch=RestartEpoch.from_dict(hello['epoch'])
    profile=launcher.protected_json('/control/goal-runtime.json')
    plan=launcher.protected_json('/control/diagnostic-plan.json')
    root=launcher.initialize_manager_state(hello,epoch)
    runtime=launcher.GoalPipeRuntime(sys.stdin,sys.stdout,epoch,profile)
    state=ServiceState(root,require_epochs=True)
    controller=make_controller(state,runtime,profile,plan,observe=ShadowRead().observe)
    report('DIAGNOSTIC_MANAGER_READY',synthetic_intake=True,real_github_shadow=True,shadow_in_approval_callback=False,real_codex=True,
           manager_uid=os.getuid(),manager_euid=os.geteuid(),publication_enabled=False)
    try:
        try:
            answer=controller.process(FIXTURE_NUMBER)
            row=state.get(FIXTURE_NUMBER)
            result=GoalResult.from_dict(row['result']['result']) if row and row.get('result') else None
            files=[];exact=False
            if result:
                for change in result.candidate.changes:
                    entry=change.after
                    if entry and entry.kind=='file':
                        files.append(entry.descriptor())
                        if entry.path=='diagnostic-output/connection.json':
                            try:exact=loads(entry.content)=={'diagnostic':True,'input_real_model_call':False}
                            except Exception:exact=False
            safe=dict(ledger_phase=row['phase'],result_present=bool(row.get('result')),publication_present=bool(row.get('publication')),
                      candidate_outcome=result.candidate_outcome if result else None,
                      files=files,expected_json_match=exact and len(files)==1,additional_observation_phase='before_controller',
                      after_epoch_present=state.db.execute('SELECT after_epoch IS NOT NULL FROM service_epochs WHERE issue=?',(FIXTURE_NUMBER,)).fetchone()[0]==1,
                      synthetic_intake=True,real_model=True,research_success_verified=False)
            exclusive_record(Path('/control/diagnostic-summary.json'),safe)
            report('DIAGNOSTIC_PRIVATE_PERSISTED',**safe)
            launcher.write_frame(sys.stdout,{'op':'report','reports':[answer]})
            launcher.write_frame(sys.stdout,{'op':'restart'}) # supervisor returns; Docker restart=no, NO restart occurs.
        except BaseException as exc:
            saved=state.get(FIXTURE_NUMBER)
            if runtime.failed_issue is not None:
                launcher.write_frame(sys.stdout,{'op':'failure_ack','issue':FIXTURE_NUMBER,'phase':saved['phase'] if saved else 'MISSING'})
            report('DIAGNOSTIC_MANAGER_TERMINAL',ledger_phase=saved['phase'] if saved else 'UNCLAIMED',
                   result_present=bool(saved and saved.get('result')),
                   error_class='contract' if isinstance(exc,ContractError) else 'timeout' if isinstance(exc,TimeoutError) else 'os' if isinstance(exc,OSError) else 'other')
            if runtime.failed_issue is None:raise
    finally:state.close()


def install_pid1_stop_handlers():
    if os.getpid()!=1 or os.getresuid()!=(0,0,0):
        raise ContractError('diagnostic stop handlers require root PID1')
    def stop(signum, frame):
        # Exit this dedicated namespace immediately, including during a
        # transient EUID drop. Do not write/repair a claim, ACK uncertain
        # cleanup, or signal a caller-selected PID/process group.
        os._exit(128+signum)
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)


def main():
    if sys.argv[1:]==['manager']:
        manager();return
    if sys.argv[1:]:raise ContractError('diagnostic argv rejected')
    if os.getpid()!=1 or os.getresuid()!=(0,0,0):raise ContractError('diagnostic PID1')
    install_pid1_stop_handlers()
    # Startup/admission is bounded too; runtime later sets its own wall+4 watchdog.
    signal.signal(signal.SIGALRM,lambda *_:os._exit(124));signal.alarm(230)
    launcher.blocked_startup=lambda:(_ for _ in ()).throw(ContractError('diagnostic startup blocked'))
    report('DIAGNOSTIC_ONCE',source=Path('/opt/service/source-sha').read_text().strip(),model='gpt-6.1-sol',codex_version='0.160.0',
           restart='no',max_model_seconds=160,max_runtime_seconds=180,hard_runtime_seconds=184)
    launcher.supervise()


if __name__=='__main__':
    code=0
    configure_launcher()
    try:main()
    except BaseException as exc:
        detail=safe_goal_runtime_diagnostic(exc)
        if detail:report('DIAGNOSTIC_CLOSED_FAILURE',**detail)
        else:report('DIAGNOSTIC_CLOSED_FAILURE',reason='DIAGNOSTIC_DRIVER_FAILURE',
                    stage='manager' if sys.argv[1:]==['manager'] else 'launcher',
                    error_class='contract' if isinstance(exc,ContractError) else 'os' if isinstance(exc,OSError) else 'other')
        code=125
    os._exit(code)
