"""Dedicated-container fixed CPU service. Not for an existing research container."""
import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
sys.path.insert(0,'/opt/service')
from research_harness.deployment_contract import validate_dedicated_deployment
from research_harness.manager_runtime import (PipeRuntime,RecheckRejected,
    read_frame,write_frame,require_recheck_reply)
from research_harness.restart_boundary import read_epoch,RestartEpoch
from research_harness.schema import ContractError,loads,canonical
from research_harness.service_state import ServiceState,initialize,validate_binding
from research_harness.persistent_fixture import exclusive_record,private,sync_directory
from research_harness.uid_supervisor import LinuxUIDBackend,OwnedUIDSupervisor,report_failure

CONTROL=10001
SCRIPT='/opt/service/launcher.py'


def deployment():
    # No DAC_OVERRIDE: read only as the fixed manager, while saved UID stays root.
    if os.getresuid()!=(0,0,0) or os.getpid()!=1:raise ContractError('dedicated PID1 required')
    try:
        os.seteuid(CONTROL)
        control_stat=Path('/control').lstat()
        if Path('/control').is_symlink() or not Path('/control').is_dir() or control_stat.st_uid!=CONTROL or control_stat.st_mode & 0o777!=0o700:
            raise ContractError('invalid protected manager directory')
        path=Path('/control/deployment.json');st=path.lstat()
        if not path.is_file() or path.is_symlink() or st.st_nlink!=1 or st.st_uid not in (0,CONTROL):
            raise ContractError('invalid deployment record')
        if st.st_mode & 0o022:raise ContractError('deployment record is writable by others')
        raw=path.read_bytes()
        if len(raw)>1024*1024:raise ContractError('deployment record too large')
        data=loads(raw)
    finally:os.seteuid(0)
    verified=validate_dedicated_deployment(data['inspect'],data['consumers'],
        source_sha=data['source_sha'],volume_name=data['volume_name'],volume_inspect=data['volume_inspect'])
    if Path('/opt/service/source-sha').read_text().strip()!=verified['source_sha']:
        raise ContractError('installed source revision differs')
    if Path('/etc/hostname').read_text().strip()!=verified['container_id'][:12]:
        raise ContractError('deployed container identity changed')
    if type(data.get('initialize_new_state')) is not bool:raise ContractError('initialization intent missing')
    return dict(verified,initialize_new_state=data['initialize_new_state'])


def manager():
    from research_harness.manager_github import ManagerGitHub
    from research_harness.owner_approval import GitHubOwnerComments
    from research_harness.result_publisher import GitHubResultPublisher,GhResultTransport
    from research_harness.service import Controller,source_failure_report,source_retry_delay
    if os.getresuid()!=(CONTROL,)*3:raise ContractError('manager identity mismatch')
    os.umask(0o077)
    hello=read_frame(sys.stdin)
    epoch=RestartEpoch.from_dict(hello['epoch'])
    root=Path('/control/state');marker=Path('/control/state-initialized.json')
    if hello['initialize_new_state'] is True:
        if marker.exists():raise ContractError('initialization was interrupted; manual reconciliation required')
        # Consumed before creating state. Interrupted initialization never resets.
        exclusive_record(marker,{'container_id':epoch.container_id})
        root.mkdir(mode=0o700)
        initialize(root)
        # Permanently consume installation intent before any Issue is processed.
        deployment_path=Path('/control/deployment.json')
        raw=loads(deployment_path.read_bytes())
        if raw.get('initialize_new_state') is not True:raise ContractError('initialization intent changed')
        raw['initialize_new_state']=False
        temporary=Path('/control/deployment-consumed.json')
        exclusive_record(temporary,raw)
        os.replace(temporary,deployment_path);sync_directory(Path('/control'))
    else:
        private(marker)
        if loads(marker.read_bytes())!={'container_id':epoch.container_id}:
            raise ContractError('state belongs to a different dedicated container')
    runtime=PipeRuntime(sys.stdin,sys.stdout,epoch)
    state=ServiceState(root,require_epochs=True)
    try:
        transport=GhResultTransport(enabled=True,manager_credentials_isolated=True,runtime_verified=True)
        controller=Controller(state,ManagerGitHub(),GitHubOwnerComments(),runtime,
            lambda persist:GitHubResultPublisher(transport,persist_state=persist),source_sha=hello['source_sha'])
        while True:
            try:reports=controller.once()
            except Exception as exc:reports=[source_failure_report(exc)]
            for report in reports:
                write_frame(sys.stdout,{'op':'report','reports':[report]})
            if any(report.get('restart_required') for report in reports):
                write_frame(sys.stdout,{'op':'restart'});return
            # Any claimed attempt, including uncertain/failed dispatch, ends this
            # generation before the manager may scan another Issue.
            if any(report.get('dispatched') in (True,'unknown') for report in reports):
                write_frame(sys.stdout,{'op':'restart'});return
            time.sleep(source_retry_delay(reports,now=time.time()))
    finally:state.close()


def blocked_startup(*, wait=None):
    """Quarantine only a child-free root PID1, without retrying installation.

    This is called exclusively before Popen. Runtime failures must still exit
    PID1 to end the worker namespace. An operator stop explicitly exits this
    idle process; an operator restart may revalidate repaired installation data.
    The wait hook permits bounded, fake-OS tests without starting a container.
    """
    if os.getpid()!=1 or os.getresuid()!=(0,0,0):
        raise ContractError('dedicated root PID1 required for startup quarantine')
    print(json.dumps({'phase':'BLOCKED_STARTUP',
        'detail':'startup validation failed; operator repair and restart required'}),
        file=sys.stderr,flush=True)
    def stop(signum, frame):
        os._exit(125)
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    if wait is None:wait=signal.pause
    while True:wait()


def supervise():
    started=time.monotonic()
    stage='deployment'
    try:
        config=deployment()
        stage='epoch'
        epoch=read_epoch(container_id=config['container_id'])
    except Exception as exc:
        report_failure('launcher',stage,exc)
        # No manager or worker exists yet. Never turn a bad installation into
        # an unless-stopped restart loop or retry it inside this generation.
        blocked_startup()
        raise ContractError('startup quarantine unexpectedly returned')
    try:
        control=subprocess.Popen([sys.executable,'-I','-B',SCRIPT,'manager'],user=CONTROL,group=CONTROL,
            extra_groups=[],env={'LANG':'C.UTF-8','PATH':'/usr/local/bin:/usr/bin:/bin',
            'HOME':'/control/home','GH_CONFIG_DIR':'/control/gh'},stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,text=True,close_fds=True)
        write_frame(control.stdin,{'epoch':epoch.to_dict(),'source_sha':config['source_sha'],'initialize_new_state':config['initialize_new_state']})
    except BaseException as exc:
        report_failure('launcher','manager_start',exc)
        raise
    attempted=False;selected_plan=None;model_request=None
    stage='protocol'
    try:
        while True:
            stage='protocol'
            frame=read_frame(control.stdout)
            if model_request is not None and frame.get('op') in ('prepare_plan','preflight'):
                # Preparation already copied auth into this generation's tmpfs.
                # A revoked/unclaimed candidate can make once() advance before
                # reporting. Never acknowledge another plan or reuse that auth.
                time.sleep(max(0,11-(time.monotonic()-started)))
                return
            if set(frame)=={'op','plan'} and frame['op']=='prepare_plan':
                from research_harness.service import validate_canary_plan
                if attempted:raise ContractError('one attempt per PID namespace')
                stage='plan'
                selected_plan=validate_canary_plan(frame['plan'],config['source_sha'])
                write_frame(control.stdin,{'prepared':True})
            elif frame=={'op':'preflight'}:
                if attempted:raise ContractError('one attempt per PID namespace')
                stage='preflight'
                # Check actual OS/outer resource boundary before durable claim.
                OwnedUIDSupervisor(LinuxUIDBackend(),controller_pid=control.pid).reserve()
                if selected_plan and selected_plan['kind']=='codex-canary-v1':
                    from research_harness.model_deployment import prepare_model
                    stage='model_prepare'
                    model_request=prepare_model(source_sha=config['source_sha'],model=selected_plan['model'])
                write_frame(control.stdin,{'ready':True})
            elif set(frame)=={'op','binding'} and frame['op'] in ('fixed_cpu','fixed_model'):
                if attempted:raise ContractError('one attempt per PID namespace')
                stage='binding'
                attempted=True;validate_binding(frame['binding'])
                if selected_plan is None or frame['binding']['source_sha']!=config['source_sha'] or hashlib.sha256(canonical(selected_plan)).hexdigest()!=frame['binding']['plan_digest']:
                    raise ContractError('runtime plan binding mismatch')
                model_mode=selected_plan['kind']=='codex-canary-v1'
                if (frame['op']=='fixed_model')!=model_mode:raise ContractError('runtime kind mismatch')
                def recheck():
                    try:
                        write_frame(control.stdin,{'op':'recheck'})
                        response=read_frame(control.stdout)
                    except OSError:
                        raise RecheckRejected('source_unavailable',origin='launcher_ipc') from None
                    except (ContractError,EOFError,ValueError,TypeError,RecursionError):
                        raise RecheckRejected('contract',origin='launcher_ipc') from None
                    require_recheck_reply(response)
                supervisor=OwnedUIDSupervisor(LinuxUIDBackend(),controller_pid=control.pid)
                if model_mode:
                    if model_request is None:raise ContractError('MODEL_PREFLIGHT_MISSING')
                    def admit_external(request):
                        # Protected operator consent is separate from this live
                        # supervisor's actual namespace/resource/UID checks.
                        from research_harness.model_deployment import validate_model_config,read_private
                        try:
                            os.seteuid(CONTROL)
                            current=loads(read_private(Path('/control/model.json'),limit=8192))
                        finally:os.seteuid(0)
                        expected=validate_model_config(current,source_sha=config['source_sha'],model=selected_plan['model'])
                        if request!=expected:raise ContractError('MODEL_CONFIGURATION_CHANGED')
                    stage='model_runtime'
                    result=supervisor.run_fixed_app_server_canary(model_request,recheck,
                        enabled=True,admit_external=admit_external)
                    stage='result'
                    from research_harness.model_result import MODEL_RESULT_SCHEMA as MODEL_SCHEMA
                    candidate=result.candidate
                    artifact=dict(schema=MODEL_SCHEMA,**{key:frame['binding'][key] for key in
                        ('request_id','run_id','issue_number','plan_digest','source_sha')},
                        status='candidate_only',model=selected_plan['model'],sandbox_mode='externalSandbox',
                        reported_outcome=candidate.outcome,reported_checksum=candidate.reported_checksum,
                        command_observed=result.observed_command is not None and result.command_exit_code==0,
                        elapsed_seconds=result.elapsed_seconds,requires_namespace_restart=True,
                        research_success_verified=False)
                    write_frame(control.stdin,{'model_result':artifact})
                else:
                    stage='cpu_runtime'
                    result=supervisor.run_fixed_canary(recheck,wall_seconds=120)
                    write_frame(control.stdin,{'result':{'checksum':result.checksum,
                        'elapsed_seconds':result.elapsed_seconds,'model_called':False}})
            elif set(frame)=={'op','reports'} and frame['op']=='report':
                print(json.dumps({'mode':'approved-fixed-canary','reports':frame['reports']},ensure_ascii=False),flush=True)
                if model_request is not None:
                    # Even no-dispatch outcomes consume prepared model state.
                    # Only PID1 exit/restart clears it; never delete/reuse it live.
                    time.sleep(max(0,11-(time.monotonic()-started)))
                    return
            elif frame=={'op':'restart'}:
                # Docker restart policy only applies after successful startup.
                time.sleep(max(0,11-(time.monotonic()-started)))
                # Exiting this dedicated PID1 ends the entire namespace. Results
                # remain pending until a later validated restart generation.
                return
            else:raise ContractError('unexpected manager operation')
    except BaseException as exc:
        report_failure('launcher',stage,exc)
        raise
    finally:
        control.stdin.close()
        # No host/container API or arbitrary PID signal. PID1 exit is the bound.


if __name__=='__main__':
    try:
        if sys.argv[1:]==['manager']:manager()
        elif not sys.argv[1:]:supervise()
        else:raise ContractError('unsupported launcher mode')
    except BaseException as exc:
        report_failure('manager' if sys.argv[1:]==['manager'] else 'launcher','entrypoint',exc)
        os._exit(125)
    os._exit(0)
