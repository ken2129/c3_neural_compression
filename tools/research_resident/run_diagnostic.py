"""One explicitly approved model-free Docker attempt; never register/copy auth."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('diagnostic_prepare_common',ROOT/'tools/research_goal/prepare.py')
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
from research_harness.resident_diagnostic_fixture import A,INPUT_SHA,WORKFLOW,DUMMY_TOKEN,plan,worker_config
from research_harness.resident_deployment import validate_diagnostic_deployment
from research_harness.schema import canonical,ContractError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approve-model-free-diagnostic',action='store_true')
    args=parser.parse_args()
    if not args.approve_model_free_diagnostic:raise ContractError('explicit one-attempt approval required')
    started=time.monotonic();deadline=started+600;ident=None;stage='source';summary={};logs=[]
    def run(argv,*,data=None,env=None,limit=30):
        remaining=deadline-time.monotonic()
        if remaining<=0 or limit<=0:raise TimeoutError()
        result=subprocess.run(argv,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env=env,check=True,timeout=min(limit,remaining))
        return result.stdout+b'\n'+result.stderr if argv[:2]==['docker','logs'] else result.stdout
    common.run=run
    try:
        sha=common._git('rev-parse','HEAD').decode().strip()
        deployment=secrets.token_hex(16);project='uchishiba-resident-diag-'+deployment[:8]
        summary.update(source=sha,project=project,deployment_id=deployment,fixture_only=True)
        profile=common.profile_template()
        profile.update(enabled=True,deployment_id=deployment,runtime_source_sha=sha,input_source_sha=INPUT_SHA,
            model='gpt-6.1-sol',resource_limits=dict(max_seconds=45,**plan(deployment,A)['resource_budget']),
            risk_acknowledgements=dict.fromkeys(common.CONSENTS,True))
        task=common.validate_profile(profile,plan(deployment,A))
        stage='freshness';common.require_fresh_deployment_id(deployment)
        for command in (['docker','ps','-aq'],['docker','volume','ls','-q']):
            if run([*command,'--filter','label=com.docker.compose.project='+project]).strip():raise ContractError('project exists')
        control=project+'_goal-control';runner=project+'_runner-state'
        for name in (control,runner):
            if subprocess.run(['docker','volume','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10).returncode==0:
                raise ContractError('named diagnostic volume exists')
        if subprocess.run(['docker','image','inspect',project+'-controller'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10).returncode==0:
            raise ContractError('diagnostic image name exists')
        with tempfile.TemporaryDirectory(prefix='c3-model-free-context-') as tmp:
            context=Path(tmp);stage='context';common.build_context(context,profile,task)
            prefix='tools/research_resident'
            common._git('diff','--quiet','HEAD','--',prefix)
            for flags in (['--others','--exclude-standard'],['--others','--ignored','--exclude-standard']):
                if common._git('ls-files',*flags,'--',prefix).strip():raise ContractError('unclean diagnostic source')
            for record in common._git('ls-tree','-rz',sha,'--',prefix).split(b'\0'):
                if not record:continue
                metadata,path=record.split(b'\t',1);mode,kind,blob=metadata.decode().split()
                if kind!='blob' or mode not in ('100644','100755'):raise ContractError('regular source required')
                target=context/path.decode();target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(common._git('cat-file','blob',blob));target.chmod(int(mode[-3:],8))
            env=dict(os.environ,C3_GOAL_CONTEXT=str(context),C3_RUNTIME_SHA=sha,C3_INPUT_SHA=INPUT_SHA,C3_DEPLOYMENT_ID=deployment)
            compose=['docker','compose','--project-name',project,'-f',str(context/prefix/'compose.diagnostic.yaml')]
            stage='config';run([*compose,'config','--quiet'],env=env)
            stage='build';run([*compose,'build','controller'],env=env,limit=440)
            stage='create';run([*compose,'create','controller'],env=env)
            ident=run([*compose,'ps','-aq','controller'],env=env).decode().strip()
            if re.fullmatch('[0-9a-f]{64}',ident) is None:raise ContractError('full ID required')
            summary['container_id']=ident
            stage='inspect';inspect=json.loads(run(['docker','inspect',ident]))[0]
            volumes={key:json.loads(run(['docker','volume','inspect',name]))[0] for key,name in [('control',control),('runner',runner)]}
            consumers={key:run(['docker','ps','-aq','--no-trunc','--filter','volume='+name]).decode().splitlines() for key,name in [('control',control),('runner',runner)]}
            validate_diagnostic_deployment(inspect,consumers,runtime_source_sha=sha,input_source_sha=INPUT_SHA,
                volume_name=control,volume_inspect=volumes,resource_limits=profile['resource_limits'],deployment_id=deployment)
            if inspect['State']['Status']!='created' or inspect.get('AppArmorProfile')!='docker-default':raise ContractError('fresh/default profile required')
            summary.update(image_id=inspect['Image'],volumes=[control,runner],inspect_verified=True)
            manifest=dict(inspect=inspect,consumers=consumers,volume_name=control,volume_inspect=volumes,initialize_new_state=True)
            dummy_auth=canonical(dict(auth_mode='chatgpt',tokens=dict(access_token='SYNTHETIC-NONCREDENTIAL',refresh_token='SYNTHETIC-NONCREDENTIAL',id_token='SYNTHETIC-NONCREDENTIAL')))
            stage='records';common.copy_records(ident,{'goal-deployment.json':canonical(manifest),
                'goal-runtime.json':canonical(common.installed_profile(profile,task)),
                'resident-intake.json':canonical(dict(schema='c3-resident-intake-v1',enabled=True,workflow_shas=[WORKFLOW])),
                'codex-auth.json':dummy_auth,'worker-pr.json':canonical(worker_config()),'worker-github-token':DUMMY_TOKEN})
            if deadline-time.monotonic()<135:raise TimeoutError()
            stage='run';run_started=time.monotonic();run(['docker','start',ident])  # EXACTLY ONE START.
            passed=False
            while time.monotonic()-run_started<120:
                current=json.loads(run(['docker','inspect',ident],limit=min(5,120-(time.monotonic()-run_started))))[0]
                if current['Id']!=ident or current['RestartCount']!=0:raise ContractError('identity/restart changed')
                raw=run(['docker','logs','--tail','120',ident],limit=min(5,120-(time.monotonic()-run_started))).decode('utf-8','replace')
                logs=[]
                for line in raw.splitlines():
                    try:value=json.loads(line)
                    except ValueError:continue
                    if type(value) is dict:logs.append(value)
                if any(v.get('phase')=='RESIDENT_DIAGNOSTIC_PASSED' for v in logs):passed=True;break
                if not current['State']['Running']:break
                time.sleep(min(1,max(0,120-(time.monotonic()-run_started))))
            summary['run_seconds']=round(time.monotonic()-run_started,3)
            summary['live_state_before_stop']=dict(status=current['State']['Status'],restart_count=current['RestartCount'])
            if not passed:raise ContractError('diagnostic did not pass')
            stage='result';raw=run(['docker','cp','-a',ident+':/control/diagnostic-result.json','-'])
            with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
                entries=archive.getmembers()
                if len(entries)!=1:raise ContractError('single result required')
                entry=entries[0]
                if not entry.isfile() or entry.name!='diagnostic-result.json' or (entry.uid,entry.gid,entry.mode)!=(10001,10001,0o600) or entry.size>65536:raise ContractError('result contract')
                result=archive.extractfile(entry).read()
            summary.update(result=json.loads(result),result_sha256=hashlib.sha256(result).hexdigest(),phase='PASSED')
    except Exception as exc:
        summary.update(phase='BLOCKED',stage=stage,reason='TIMEOUT' if isinstance(exc,(TimeoutError,subprocess.TimeoutExpired)) else 'COMMAND_FAILED' if isinstance(exc,subprocess.CalledProcessError) else 'CONTRACT_REJECTED',
            command_exit_code=exc.returncode if isinstance(exc,subprocess.CalledProcessError) else None)
        summary['fixed_log_tail']=[{k:v[k] for k in ('phase','reason','stage','role','error_class') if k in v} for v in logs[-8:]]
    finally:
        if ident is not None:
            current=json.loads(subprocess.check_output(['docker','inspect',ident]))[0]
            if current['Id']!=ident or current['Config']['Labels'].get('c3.purpose')!='dedicated-resident-diagnostic' or current['Config']['Labels'].get('c3.deployment-id')!=deployment:
                raise ContractError('cleanup identity mismatch')
            if current['State']['Running']:
                subprocess.run(['docker','stop','--time','5',ident],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=12)
            final=json.loads(subprocess.check_output(['docker','inspect',ident]))[0]
            summary['after_stop']=dict(status=final['State']['Status'],exit_code=final['State']['ExitCode'],restart_count=final['RestartCount'])
        summary['total_seconds']=round(time.monotonic()-started,3)
        print(json.dumps(summary),flush=True)
    return 0 if summary.get('phase')=='PASSED' else 1


if __name__=='__main__':raise SystemExit(main())
