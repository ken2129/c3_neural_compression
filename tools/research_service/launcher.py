"""Dedicated-container fixed CPU service. Not for an existing research container."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
sys.path.insert(0,'/opt/service')
from research_harness.deployment_contract import validate_dedicated_deployment
from research_harness.manager_runtime import PipeRuntime,read_frame,write_frame
from research_harness.restart_boundary import read_epoch,RestartEpoch
from research_harness.schema import ContractError,loads
from research_harness.service_state import ServiceState,initialize,validate_binding
from research_harness.persistent_fixture import exclusive_record,private,sync_directory
from research_harness.uid_supervisor import LinuxUIDBackend,OwnedUIDSupervisor

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
    from research_harness.issue_intake import PublicGitHub
    from research_harness.owner_approval import GitHubOwnerComments
    from research_harness.result_publisher import GitHubResultPublisher,GhResultTransport
    from research_harness.service import Controller
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
        controller=Controller(state,PublicGitHub(),GitHubOwnerComments(),runtime,
            lambda persist:GitHubResultPublisher(transport,persist_state=persist),source_sha=hello['source_sha'])
        while True:
            try:reports=controller.once()
            except Exception:reports=[{'phase':'SOURCE_UNAVAILABLE','dispatched':False}]
            for report in reports:
                write_frame(sys.stdout,{'op':'report','reports':[report]})
            if any(report.get('restart_required') for report in reports):
                write_frame(sys.stdout,{'op':'restart'});return
            # Any claimed attempt, including uncertain/failed dispatch, ends this
            # generation before the manager may scan another Issue.
            if any(report.get('dispatched') in (True,'unknown') for report in reports):
                write_frame(sys.stdout,{'op':'restart'});return
            time.sleep(60)
    finally:state.close()


def supervise():
    started=time.monotonic()
    config=deployment()
    epoch=read_epoch(container_id=config['container_id'])
    control=subprocess.Popen([sys.executable,'-I','-B',SCRIPT,'manager'],user=CONTROL,group=CONTROL,
        extra_groups=[],env={'LANG':'C.UTF-8','PATH':'/usr/local/bin:/usr/bin:/bin',
        'HOME':'/control/home','GH_CONFIG_DIR':'/control/gh'},stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,text=True,close_fds=True)
    write_frame(control.stdin,{'epoch':epoch.to_dict(),'source_sha':config['source_sha'],'initialize_new_state':config['initialize_new_state']})
    attempted=False
    try:
        while True:
            frame=read_frame(control.stdout)
            if frame=={'op':'preflight'}:
                if attempted:raise ContractError('one attempt per PID namespace')
                # Check actual OS/outer resource boundary before durable claim.
                OwnedUIDSupervisor(LinuxUIDBackend(),controller_pid=control.pid).reserve()
                write_frame(control.stdin,{'ready':True})
            elif set(frame)=={'op','binding'} and frame['op']=='fixed_cpu':
                if attempted:raise ContractError('one attempt per PID namespace')
                attempted=True;validate_binding(frame['binding'])
                def recheck():
                    write_frame(control.stdin,{'op':'recheck'})
                    response=read_frame(control.stdout)
                    if set(response)!={'approved'} or response['approved'] is not True:raise ContractError('approval no longer valid')
                supervisor=OwnedUIDSupervisor(LinuxUIDBackend(),controller_pid=control.pid)
                result=supervisor.run_fixed_canary(recheck,wall_seconds=120)
                write_frame(control.stdin,{'result':{'checksum':result.checksum,
                    'elapsed_seconds':result.elapsed_seconds,'model_called':False}})
            elif set(frame)=={'op','reports'} and frame['op']=='report':
                print(json.dumps({'mode':'fixed-cpu-only','reports':frame['reports']},ensure_ascii=False),flush=True)
            elif frame=={'op':'restart'}:
                # Docker restart policy only applies after successful startup.
                time.sleep(max(0,11-(time.monotonic()-started)))
                # Exiting this dedicated PID1 ends the entire namespace. Results
                # remain pending until a later validated restart generation.
                return
            else:raise ContractError('unexpected manager operation')
    finally:
        control.stdin.close()
        # No host/container API or arbitrary PID signal. PID1 exit is the bound.


if __name__=='__main__':
    try:
        if sys.argv[1:]==['manager']:manager()
        elif not sys.argv[1:]:supervise()
        else:raise ContractError('unsupported launcher mode')
    except BaseException:
        print(json.dumps({'phase':'BLOCKED','detail':'dedicated service stopped; inspect protected state'}),file=sys.stderr,flush=True)
        os._exit(125)
    os._exit(0)
