"""Optional installed GPU recipes. These contracts never authorize a run."""
from copy import deepcopy
import hashlib
import math
from pathlib import PurePosixPath
import re

from .schema import ContractError, canonical, fields, relative_path

MAX_RESULT_BYTES = 16384
GPU_RESULT_PATH = '/work/worker-home/gpu-result.json'
PENDING = {'schema': 'c3-gpu-pending-v1'}
REASONS = frozenset(('WAIT_TIMEOUT', 'RUN_TIMEOUT', 'CANCELLED', 'RESOURCE_LIMIT',
    'INPUT_CHANGED', 'COMMAND_FAILED', 'OUTPUT_REJECTED', 'PRIOR_RUN',
    'GPU_COMPETITION', 'EXECUTOR_REJECTED'))


def name(value):
    if type(value) is not str or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value) is None:
        raise ContractError('invalid GPU recipe or machine name')
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ContractError('GPU budget outside bounds')
    return value


def absolute(value, *, control=False):
    if (type(value) is not str or not value.startswith('/') or len(value) > 512
            or any(ord(c) < 32 for c in value) or '..' in PurePosixPath(value).parts
            or (control and not value.startswith('/control/'))):
        raise ContractError('invalid installed GPU path')
    return value


def public_paths(value):
    if type(value) is not list or not 1 <= len(value) <= 16:
        raise ContractError('bounded GPU export list required')
    for path in value:
        relative_path(path)
        if (len(path) > 128 or PurePosixPath(path).suffix not in ('.json', '.csv', '.md', '.txt')
                or any(part.startswith('.') for part in path.split('/'))):
            raise ContractError('unsupported GPU export path')
    if len(set(value)) != len(value):
        raise ContractError('duplicate GPU export path')
    return value


def validate_gpu_request(value):
    fields(value, ('machine', 'recipe', 'max_seconds', 'max_wait_seconds'))
    name(value['machine']); name(value['recipe'])
    integer(value['max_seconds'], 30, 86400)
    integer(value['max_wait_seconds'], 0, 86400)
    return deepcopy(value)


def validate_targets(value):
    if type(value) is not dict or not 1 <= len(value) <= 4:
        raise ContractError('bounded installed GPU targets required')
    for machine, target in value.items():
        name(machine)
        fields(target, ('transport', 'ssh_host', 'ssh_user', 'ssh_port', 'container',
                       'container_uid', 'python', 'agent', 'registry', 'identity_file',
                       'known_hosts_file', 'recipes'))
        if target['transport'] != 'ssh-docker-stdio-v1':
            raise ContractError('unsupported GPU backend')
        for key, pattern in (('ssh_host', r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}'),
                             ('ssh_user', r'[a-z_][a-z0-9_-]{0,63}'),
                             ('container', r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')):
            if type(target[key]) is not str or re.fullmatch(pattern, target[key]) is None:
                raise ContractError('invalid installed GPU destination')
        integer(target['ssh_port'], 1, 65535); integer(target['container_uid'], 1, 2**31-1)
        for key in ('python', 'agent', 'registry'):
            absolute(target[key])
        for key in ('identity_file', 'known_hosts_file'):
            absolute(target[key], control=True)
        recipes = target['recipes']
        if type(recipes) is not dict or not 1 <= len(recipes) <= 16:
            raise ContractError('installed GPU recipes required')
        for recipe, definition in recipes.items():
            name(recipe)
            fields(definition, ('definition_sha256', 'max_seconds', 'max_wait_seconds', 'public_outputs'))
            if (type(definition['definition_sha256']) is not str
                    or re.fullmatch('[0-9a-f]{64}', definition['definition_sha256']) is None):
                raise ContractError('pinned GPU recipe definition required')
            integer(definition['max_seconds'], 30, 86400)
            integer(definition['max_wait_seconds'], 0, 86400)
            public_paths(definition['public_outputs'])
    return deepcopy(value)


def installed_target(profile, request):
    if type(profile) is not dict:
        raise ContractError('protected GPU profile required')
    request = validate_gpu_request(request)
    targets = validate_targets(profile.get('gpu_targets'))
    try:
        target = targets[request['machine']]
        definition = target['recipes'][request['recipe']]
    except KeyError:
        raise ContractError('GPU recipe is not installed for this machine') from None
    if any(request[k] > definition[k] for k in ('max_seconds', 'max_wait_seconds')):
        raise ContractError('GPU request exceeds installed recipe ceiling')
    return target, definition


def resident_job_seconds(plan):
    gpu = plan.get('gpu_experiment')
    if gpu is None:
        return plan['max_seconds']
    validate_gpu_request(gpu)
    # Only the trusted pre-worker GPU stage gets this additional allocation.
    # Codex still has its original max_seconds and CPU-only resource profile.
    return plan['max_seconds'] + gpu['max_seconds'] + gpu['max_wait_seconds'] + 30


def validate_gpu_result(value, *, binding=None, request=None, definition=None):
    from .service_state import validate_binding
    fields(value, ('schema', 'binding', 'machine', 'recipe', 'definition_sha256',
                   'outcome', 'reason', 'elapsed_seconds', 'cleanup_verified', 'files'))
    validate_binding(value['binding'])
    name(value['machine']); name(value['recipe'])
    if (value['schema'] != 'c3-gpu-result-v1' or value['cleanup_verified'] is not True
            or value['outcome'] not in ('completed', 'failed', 'cancelled')
            or (value['reason'] is not None and value['reason'] not in REASONS)
            or type(value['elapsed_seconds']) not in (int, float)
            or not math.isfinite(value['elapsed_seconds']) or value['elapsed_seconds'] < 0
            or type(value['definition_sha256']) is not str
            or re.fullmatch('[0-9a-f]{64}', value['definition_sha256']) is None):
        raise ContractError('GPU completion contract rejected')
    if binding is not None and value['binding'] != binding:
        raise ContractError('GPU result belongs to another run')
    if ((value['outcome'] == 'completed' and value['reason'] is not None)
            or (value['outcome'] != 'completed' and value['reason'] not in REASONS)):
        raise ContractError('inconsistent GPU terminal outcome')
    if request is not None and any(value[k] != request[k] for k in ('machine', 'recipe')):
        raise ContractError('GPU result recipe mismatch')
    files = value['files']
    if type(files) is not dict or len(files) > 16:
        raise ContractError('GPU public result files rejected')
    if files:
        public_paths(list(files))
    if any(type(text) is not str for text in files.values()):
        raise ContractError('GPU results must be bounded text')
    if definition is not None:
        if (value['definition_sha256'] != definition['definition_sha256']
                or not set(files) <= set(definition['public_outputs'])
                or (value['outcome'] == 'completed' and set(files) != set(definition['public_outputs']))):
            raise ContractError('GPU result differs from installed export scope')
    if len(canonical(value)) > MAX_RESULT_BYTES:
        raise ContractError('GPU result exceeds IPC/worker input limit')
    return deepcopy(value)


def definition_digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()
