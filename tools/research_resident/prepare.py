"""Create one fresh resident deployment, stopped; no runner registration/start.

An explicit deployment and credential-transfer approval is required separately
from a reviewed Issue execution approval. Never infer it from file presence.
The GitHub workflow/client remain dot's PR93; only its pinned blob is installed.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('goal_prepare_common',ROOT/'tools/research_goal/prepare.py')
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
from research_harness.actions_inbox import validate_policy
from research_harness.goal_plan import DIRECT_PR_KIND
from research_harness.resident_deployment import validate_resident_deployment
from research_harness.schema import ContractError, canonical, loads

CLIENT_COMMIT='9f99f59a5b08b4c7139582b774d17e8c3e2fc753'
CLIENT_PATH='tools/research_actions/issue_event.py'
CLIENT_SHA256='d01cc43548dc935c60208319bf0c9beeab8d45df6c03252082e3560359858447'


def build_context(destination, profile, plan):
    common.build_context(destination,profile,plan)
    prefix='tools/research_resident'
    common._git('diff','--quiet','HEAD','--',prefix)
    for flags in (['--others','--exclude-standard'],['--others','--ignored','--exclude-standard']):
        if common._git('ls-files',*flags,'--',prefix).strip():
            raise ContractError('resident sources must be committed and clean')
    listing=common._git('ls-tree','-rz',profile['runtime_source_sha'],'--',prefix)
    for record in listing.split(b'\0'):
        if not record:continue
        metadata,path=record.split(b'\t',1);mode,kind,sha=metadata.decode().split()
        if mode not in ('100644','100755') or kind!='blob':
            raise ContractError('resident sources must be regular committed blobs')
        dest=destination/path.decode();dest.parent.mkdir(parents=True,exist_ok=True)
        dest.write_bytes(common._git('cat-file','blob',sha));dest.chmod(int(mode[-3:],8))
    client=common._git('show',CLIENT_COMMIT+':'+CLIENT_PATH)
    if hashlib.sha256(client).hexdigest()!=CLIENT_SHA256:
        raise ContractError('pinned dot client differs')
    (destination/'issue_event.py').write_bytes(client)
    dockerfile=(destination/'tools/research_goal/Dockerfile').read_bytes()
    extension=(destination/prefix/'Dockerfile').read_bytes()
    (destination/'Dockerfile.resident').write_bytes(dockerfile+b'\n'+extension)


def validate_resident_profile(profile,plan,policy):
    checked=common.validate_profile(profile,plan)
    if checked['kind']!=DIRECT_PR_KIND:raise ContractError('resident is direct-PR-only')
    validate_policy(policy)
    if policy['enabled'] is not False:
        raise ContractError('initial installation must remain OFF')
    return checked


def main(argv=None):
    parser=common.SafeArgumentParser(description=__doc__)
    parser.add_argument('--approve-new-resident-deployment',action='store_true')
    parser.add_argument('--approve-worker-github-access',action='store_true')
    for name in ('project','profile','plan','intake-policy','codex-auth-file','github-hosts-file',
                 'worker-pr-config','worker-github-token-file','runner-version','runner-sha256'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    if not args.approve_new_resident_deployment or not args.approve_worker_github_access:
        raise ContractError('explicit dedicated deployment and worker access approval required')
    if (re.fullmatch('[a-z][a-z0-9-]{5,48}',args.project) is None
            or re.fullmatch('[0-9]+\.[0-9]+\.[0-9]+',args.runner_version) is None
            or re.fullmatch('[0-9a-f]{64}',args.runner_sha256) is None):
        raise ContractError('reviewed project and runner package pin required')
    with common.install_stage('profile'):
        profile=loads(Path(args.profile).read_bytes());policy=loads(Path(args.intake_policy).read_bytes())
        plan=validate_resident_profile(profile,loads(Path(args.plan).read_bytes()),policy)
    common.at_stage('freshness',common.require_fresh_deployment_id,profile['deployment_id'])
    with common.install_stage('credentials'):
        auth=common.read_credential(args.codex_auth_file,65536)
        from research_harness.model_deployment import validate_chatgpt_auth
        from research_harness.worker_pr import validate_worker_pr_config,WorkerGitHub
        validate_chatgpt_auth(auth)
        github=common.read_credential(args.github_hosts_file,65536)
        worker=validate_worker_pr_config(loads(Path(args.worker_pr_config).read_bytes()))
        token=common.read_credential(args.worker_github_token_file,8192)
        if worker['enabled'] is not True:raise ContractError('worker credential access disabled')
        actor=WorkerGitHub(token.decode('ascii').strip()).request('GET','/user')
        if (type(actor) is not dict or type(actor.get('id')) is not int
                or actor['id']!=worker['actor_id'] or type(actor.get('login')) is not str
                or actor['login'].lower()!=worker['actor_login'].lower()):
            raise ContractError('worker actor differs')
    control=args.project+'_goal-control';runner=args.project+'_runner-state'
    with common.install_stage('freshness'):
        for command in (['docker','ps','-aq'],['docker','volume','ls','-q']):
            if common.run([*command,'--filter','label=com.docker.compose.project='+args.project]).strip():
                raise ContractError('project already exists')
        for name in (control,runner):
            if subprocess.run(['docker','volume','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
                raise ContractError('private volume name already exists')
    with tempfile.TemporaryDirectory(prefix='c3-resident-install-') as tmp:
        context=Path(tmp);common.at_stage('context',build_context,context,profile,plan)
        env=dict(os.environ,C3_GOAL_CONTEXT=str(context),C3_RUNTIME_SHA=profile['runtime_source_sha'],
            C3_INPUT_SHA=profile['input_source_sha'],C3_DEPLOYMENT_ID=profile['deployment_id'],
            C3_GOAL_PIDS=str(profile['resource_limits']['max_processes']),
            C3_GOAL_MEMORY=str(profile['resource_limits']['memory_bytes']),
            C3_GOAL_WORK=str(profile['resource_limits']['max_work_bytes']),
            C3_RUNNER_VERSION=args.runner_version,C3_RUNNER_SHA256=args.runner_sha256)
        compose=['docker','compose','--project-name',args.project,'-f',str(context/'tools/research_resident/compose.yaml')]
        common.at_stage('compose',common.run,[*compose,'config','--quiet'],env=env)
        common.at_stage('create',common.run,[*compose,'create','--build','controller'],env=env)
        with common.install_stage('inspect'):
            ident=common.run([*compose,'ps','-aq','controller'],env=env).decode().strip()
            if re.fullmatch('[0-9a-f]{64}',ident) is None:raise ContractError('full container identity required')
            inspect=json.loads(common.run(['docker','inspect',ident]))[0]
            volumes={key:json.loads(common.run(['docker','volume','inspect',name]))[0]
                     for key,name in (('control',control),('runner',runner))}
            consumers={key:common.run(['docker','ps','-aq','--no-trunc','--filter','volume='+name]).decode().splitlines()
                       for key,name in (('control',control),('runner',runner))}
            validate_resident_deployment(inspect,consumers,runtime_source_sha=profile['runtime_source_sha'],
                input_source_sha=profile['input_source_sha'],volume_name=control,volume_inspect=volumes,
                resource_limits=profile['resource_limits'],deployment_id=profile['deployment_id'])
        manifest=dict(inspect=inspect,consumers=consumers,volume_name=control,
                      volume_inspect=volumes,initialize_new_state=True)
        common.copy_records(ident,{'goal-deployment.json':canonical(manifest),
            'goal-runtime.json':canonical(common.installed_profile(profile,plan)),
            'resident-intake.json':canonical(policy),'codex-auth.json':auth,'gh/hosts.yml':github,
            'worker-pr.json':canonical(worker),'worker-github-token':token})
        print(json.dumps(dict(phase='RESIDENT_CREATED_STOPPED',container_id=ident,
            image_id=inspect['Image'],deployment_id=profile['deployment_id'],intake_enabled=False)))
        # Deliberately no docker start, runner registration, repository-variable
        # change, legacy-service stop, Issue creation or model call.


def cli(argv=None):
    try:common.at_stage('arguments',main,argv)
    except common.InstallFailure as exc:
        print(json.dumps(dict(phase='RESIDENT_INSTALL_BLOCKED',stage=exc.stage,
            reason=exc.reason,command_exit_code=exc.exit_code,installer_exit_code=1)),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':raise SystemExit(cli())
