"""Issue-selected local execution containers. This data never authorizes a job."""
from copy import deepcopy
import base64
import re
from pathlib import PurePosixPath
from .schema import ContractError,fields,loads

DOCKER_SOCKET='/run/c3-docker.sock'
CONTROL_ROOT='/var/lib/c3-execution'
RUNTIME_ROOT='/opt/c3-execution'


def alias(value):
    if type(value) is not str or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',value) is None:
        raise ContractError('registered execution container name required')
    return value


def absolute(value):
    if (type(value) is not str or not value.startswith('/') or value.startswith('//')
            or len(value)>512 or any(ord(c)<32 for c in value)
            or str(PurePosixPath(value))!=value or '..' in PurePosixPath(value).parts):
        raise ContractError('fixed installed execution path required')
    return value


def integer(value,low,high):
    if type(value) is not int or not low<=value<=high:raise ContractError('execution limit outside bounds')


def validate_containers(value):
    from .goal_plan import RESOURCE_BOUNDS
    if type(value) is not dict or not 1<=len(value)<=8:
        raise ContractError('bounded registered execution containers required')
    identities=set();names=set()
    for key,target in value.items():
        alias(key)
        fields(target,('name','container_id','create_image','python','codex','worker_uid','workspace',
            'resource_limits','cpu_cores','gpu_uuids','max_wait_seconds','sampled_limits_accepted'),('environment','disk_reserve_bytes'))
        if (type(target['name']) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',target['name']) is None
                or target['name'] in names):raise ContractError('unique installed Docker container name required')
        names.add(target['name'])
        ident,image=target['container_id'],target['create_image']
        if ident is not None:
            if type(ident) is not str or re.fullmatch('[0-9a-f]{64}',ident) is None or ident in identities or image is not None:
                raise ContractError('pin an existing container or an image for creation')
            identities.add(ident)
        elif type(image) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}',image) is None:
            raise ContractError('creation requires an installed immutable image ID')
        for path in ('python','codex','workspace'):absolute(target[path])
        if target['python'] not in ('/usr/local/bin/python','/usr/bin/python3'):
            raise ContractError('trusted supervisor must use the system Python, not a research venv')
        if target['workspace'] in ('/','/work','/opt','/var','/run','/etc') or any(
                target['workspace']==p or target['workspace'].startswith(p+'/')
                for p in (CONTROL_ROOT,RUNTIME_ROOT,'/work/worker-home','/work/codex-home')):
            raise ContractError('research workspace must be separate from harness state')
        integer(target['worker_uid'],1,2**31-1);integer(target['cpu_cores'],1,64)
        integer(target['max_wait_seconds'],0,86400)
        if type(target['sampled_limits_accepted']) is not bool:raise ContractError('explicit resource policy required')
        if ident is not None and target['sampled_limits_accepted'] is not True:
            raise ContractError('existing container requires explicit sampled-limit acceptance')
        fields(target['resource_limits'],RESOURCE_BOUNDS)
        for item,(low,high) in RESOURCE_BOUNDS.items():integer(target['resource_limits'][item],low,high)
        uuids=target['gpu_uuids']
        if (type(uuids) is not list or len(uuids)>16 or len(set(uuids))!=len(uuids)
                or any(type(u) is not str or re.fullmatch(r'GPU-[0-9a-fA-F-]{8,75}',u) is None for u in uuids)):
            raise ContractError('installed GPU UUID allowlist required')
        if image is not None and len(uuids)>1:
            raise ContractError('initial creation backend supports one assigned GPU')
        integer(target.get('disk_reserve_bytes',0),0,1024**5)
        environment=target.get('environment',{})
        if type(environment) is not dict or len(environment)>32:raise ContractError('bounded research environment required')
        reserved={'HOME','CODEX_HOME','GH_CONFIG_DIR','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CERT_PATH',
            'OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','GITHUB_TOKEN','GH_TOKEN','BASH_ENV','ENV',
            'LD_PRELOAD','PYTHONSTARTUP','CUDA_VISIBLE_DEVICES'}
        for name,text in environment.items():
            if (type(name) is not str or re.fullmatch('[A-Z_][A-Z0-9_]*',name) is None or name in reserved
                    or type(text) is not str or len(text)>2048 or '\x00' in text):
                raise ContractError('execution environment overrides protected runtime settings')
    return deepcopy(value)


def installed_container(profile,plan):
    name=alias(plan.get('execution_container'))
    if profile.get('docker_control_approved') is not True:
        raise ContractError('explicit local Docker control approval required')
    targets=validate_containers(profile.get('execution_containers'))
    if name not in targets:raise ContractError('execution container is not registered')
    return targets[name]


def execution_limits(profile,plan):
    return installed_container(profile,plan)['resource_limits'] if 'execution_container' in plan else profile['resource_limits']


def auth_identity(raw):
    """Compare recorded identity during private refresh, not JWT verification."""
    value=loads(raw);tokens=value['tokens']
    if tokens.get('account_id'):return ('account',tokens['account_id'])
    try:
        encoded=tokens['id_token'].split('.')[1]
        payload=loads(base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4)))
        subject=payload.get('sub')
        if type(subject) is str and subject:return ('subject',subject)
    except (ValueError,KeyError,IndexError,TypeError,AttributeError):pass
    return None
