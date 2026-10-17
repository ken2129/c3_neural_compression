"""User-run installation of one NEW, explicitly approved goal deployment.

No ambient credentials are read. The two credential file arguments constitute
an explicit user-performed transfer into a new persistent volume. Never invoke
this installer on the user's behalf without the required deployment/security
and ongoing credential-access approvals. --template has no external effects.
"""
import argparse
import hashlib
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

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from research_harness.goal_plan import validate_goal_plan, GOAL_RESOURCE_BUDGET, RESOURCE_BOUNDS
from research_harness.goal_deployment import validate_goal_deployment
from research_harness.schema import ContractError, canonical, fields, loads

CONSENTS = ('worker_model_auth_readable', 'unrestricted_bridge_egress',
            'writable_runtime_homes_and_ipc', 'usage_without_hard_cost_cap',
            'arbitrary_cpu_commands')


def profile_template():
    return dict(schema='c3-goal-runtime-v1', enabled=False, deployment_id=secrets.token_hex(16),
                input_source_sha='REVIEWED_INPUT_COMMIT', runtime_source_sha='REVIEWED_RUNTIME_COMMIT',
                model='EXPLICIT_PINNED_MODEL',
                resource_limits={'max_seconds': 900, **GOAL_RESOURCE_BUDGET,
                                 'cpu_seconds_per_process': 600},
                risk_acknowledgements={name: False for name in CONSENTS})


def validate_profile(profile, plan):
    fields(profile, ('schema', 'enabled', 'deployment_id', 'input_source_sha', 'runtime_source_sha',
                     'model', 'resource_limits', 'risk_acknowledgements'))
    if profile['schema'] != 'c3-goal-runtime-v1' or profile['enabled'] is not True:
        raise ContractError('goal deployment remains disabled')
    for key in ('input_source_sha', 'runtime_source_sha'):
        if type(profile[key]) is not str or re.fullmatch('[0-9a-f]{40}', profile[key]) is None:
            raise ContractError('reviewed input and runtime revisions required')
    fields(profile['risk_acknowledgements'], CONSENTS)
    if any(profile['risk_acknowledgements'][key] is not True for key in CONSENTS):
        raise ContractError('explicit new goal execution and residual-risk approvals required')
    checked = validate_goal_plan(plan, profile['input_source_sha'], resource_limits=profile['resource_limits'], deployment_id=profile['deployment_id'])
    if checked['model'] != profile['model']:
        raise ContractError('approved goal and operator models differ')
    limits = profile['resource_limits']
    # Initial concrete deployment supports these aggregate ceilings. This is
    # separate from the extensible goal data contract and must be re-approved
    # alongside a different supervisor profile before increasing allocation.
    if (limits['memory_bytes'] > 1024**3 or not 2 <= limits['max_processes'] <= 64
            or limits['max_work_bytes'] > 256*1024**2 or checked['max_seconds'] <= 20):
        raise ContractError('requested deployment exceeds current goal supervisor support')
    for key in ('memory_bytes', 'max_processes', 'max_work_bytes'):
        if checked['resource_budget'][key] != limits[key]:
            raise ContractError('initial plan must approve actual aggregate deployment allocation')
    return checked


def installed_profile(profile, plan):
    """Bind the validated prepared task; proposal files cannot supply this digest."""
    checked = validate_profile(profile, plan)
    return {**profile, 'installed_plan_digest': hashlib.sha256(canonical(checked)).hexdigest()}


def run(argv, *, data=None, env=None):
    return subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True, env=env).stdout


def _git(*args):
    return run(['git', '-C', str(ROOT), *args])


def build_context(destination, profile, plan):
    """Copy committed regular blobs only; never execute or trust archive paths."""
    runtime = profile['runtime_source_sha']
    if _git('rev-parse', 'HEAD').decode().strip() != runtime:
        raise ContractError('runtime checkout is not the reviewed commit')
    _git('diff', '--quiet', 'HEAD', '--', 'research_harness', 'tools/research_goal')
    for flags in (['--others', '--exclude-standard'], ['--others', '--ignored', '--exclude-standard']):
        if _git('ls-files', *flags, '--', 'research_harness', 'tools/research_goal').strip():
            raise ContractError('untracked or ignored deployment sources require a fresh checkout')
    listing = _git('ls-tree', '-rz', runtime, '--', 'research_harness', 'tools/research_goal')
    for record in listing.split(b'\0'):
        if not record: continue
        metadata, raw_path = record.split(b'\t', 1)
        mode, kind, sha = metadata.decode().split()
        path = raw_path.decode('utf-8')
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ContractError('deployment source must contain regular committed files')
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_git('cat-file', 'blob', sha))
        target.chmod(int(mode[-3:], 8))
    manifest = {'source_sha': profile['input_source_sha'], 'files': {}}
    (destination / 'goal-inputs').mkdir()
    total = 0
    for path in plan['inputs']:
        listing = _git('ls-tree', '-z', profile['input_source_sha'], '--', path)
        records = [record for record in listing.split(b'\0') if record]
        if len(records) != 1: raise ContractError('approved input is missing')
        metadata, found = records[0].split(b'\t', 1)
        mode, kind, sha = metadata.decode().split()
        if found.decode() != path or kind != 'blob' or mode not in ('100644', '100755'):
            raise ContractError('approved input must be a regular committed source file')
        raw = _git('cat-file', 'blob', sha); total += len(raw)
        if total > 16*1024**2: raise ContractError('input bundle too large')
        target = destination / 'goal-inputs' / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw); target.chmod(0o444)
        manifest['files'][path] = dict(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    (destination / 'goal-inputs.json').write_bytes(canonical(manifest))


def copy_records(container, records):
    """Copy exact manager-owned records, then verify bytes and ownership."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, raw in records.items():
            info = tarfile.TarInfo(name); info.uid = info.gid = 10001
            info.uname = info.gname = ''
            info.mode = 0o600; info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    run(['docker', 'cp', '-', container + ':/control/'], data=buffer.getvalue())
    for name, raw in records.items():
        readback = run(['docker', 'cp', '-a', container + ':/control/' + name, '-'])
        with tarfile.open(fileobj=io.BytesIO(readback), mode='r:') as archive:
            members = archive.getmembers()
            if len(members) != 1: raise ContractError('deployment copy verification failed')
            entry = members[0]
            if (not entry.isfile() or entry.uid != 10001 or entry.gid != 10001
                    or entry.mode != 0o600 or archive.extractfile(entry).read() != raw):
                raise ContractError('deployment record ownership or contents differ')


def require_fresh_deployment_id(deployment_id):
    """Reject accidental profile reuse on this engine, including stopped state.

    The operator must never intentionally reuse IDs on another host or after
    deleting all installation evidence. The public nonce is not a secret.
    """
    if type(deployment_id) is not str or re.fullmatch('[0-9a-f]{32}', deployment_id) is None:
        raise ContractError('fresh public deployment identity required')
    label = 'label=c3.deployment-id=' + deployment_id
    for command in (['docker', 'ps', '-aq'], ['docker', 'volume', 'ls', '-q'],
                    ['docker', 'image', 'ls', '-q']):
        if run([*command, '--filter', label]).strip():
            raise ContractError('deployment identity has already been installed; create a fresh profile')


def read_credential(path, maximum):
    path = Path(path); info = path.lstat()
    import stat
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ContractError('explicit credential source must be a private regular file')
    raw = path.read_bytes()
    if not 0 < len(raw) <= maximum: raise ContractError('credential source size invalid')
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', action='store_true')
    parser.add_argument('--approve-new-goal-deployment', action='store_true')
    parser.add_argument('--project'); parser.add_argument('--profile'); parser.add_argument('--plan')
    parser.add_argument('--codex-auth-file'); parser.add_argument('--github-hosts-file')
    args = parser.parse_args(argv)
    if args.template:
        print(json.dumps(profile_template(), indent=2)); return
    if not args.approve_new_goal_deployment or not all((args.project, args.profile, args.plan,
                                                      args.codex_auth_file, args.github_hosts_file)):
        parser.error('explicit new-deployment approval, project, profile, plan and both credential files required')
    if re.fullmatch('[a-z][a-z0-9-]{5,48}', args.project) is None:
        raise ContractError('invalid new project name')
    profile = loads(Path(args.profile).read_bytes())
    plan = validate_profile(profile, loads(Path(args.plan).read_bytes()))
    require_fresh_deployment_id(profile['deployment_id'])
    # This installer is run by the user; never infer approval from file presence.
    auth = read_credential(args.codex_auth_file, 65536)
    from research_harness.model_deployment import validate_chatgpt_auth
    validate_chatgpt_auth(auth)
    github = read_credential(args.github_hosts_file, 65536)
    for kind, command in (('containers', ['docker', 'ps', '-aq']),
                          ('volumes', ['docker', 'volume', 'ls', '-q'])):
        if run([*command, '--filter', 'label=com.docker.compose.project=' + args.project]).strip():
            raise ContractError('project already has ' + kind + '; never reuse a canary deployment')
    volume = args.project + '_goal-control'
    probe = subprocess.run(['docker', 'volume', 'inspect', volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode == 0: raise ContractError('goal volume name already exists')
    with tempfile.TemporaryDirectory(prefix='c3-goal-install-') as tmp:
        context = Path(tmp); build_context(context, profile, plan)
        env = dict(os.environ, C3_GOAL_CONTEXT=str(context), C3_RUNTIME_SHA=profile['runtime_source_sha'],
                   C3_INPUT_SHA=profile['input_source_sha'], C3_DEPLOYMENT_ID=profile['deployment_id'], C3_GOAL_PIDS=str(profile['resource_limits']['max_processes']),
                   C3_GOAL_MEMORY=str(profile['resource_limits']['memory_bytes']),
                   C3_GOAL_WORK=str(profile['resource_limits']['max_work_bytes']))
        compose = ['docker', 'compose', '--project-name', args.project, '-f',
                   str(context / 'tools/research_goal/compose.yaml')]
        run([*compose, 'config', '--quiet'], env=env)
        run([*compose, 'create', '--build', 'controller'], env=env)
        ident = run([*compose, 'ps', '-aq', 'controller'], env=env).decode().strip()
        if re.fullmatch('[0-9a-f]{64}', ident) is None: raise ContractError('container identity missing')
        inspect = json.loads(run(['docker', 'inspect', ident]))[0]
        volume_info = json.loads(run(['docker', 'volume', 'inspect', volume]))[0]
        consumers = run(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'volume=' + volume]).decode().splitlines()
        validate_goal_deployment(inspect, consumers, runtime_source_sha=profile['runtime_source_sha'],
            input_source_sha=profile['input_source_sha'], volume_name=volume,
            volume_inspect=volume_info, resource_limits=profile['resource_limits'], deployment_id=profile['deployment_id'])
        deployment = dict(inspect=inspect, consumers=consumers, volume_name=volume,
                          volume_inspect=volume_info, initialize_new_state=True)
        copy_records(ident, {'goal-deployment.json': canonical(deployment),
                            'goal-runtime.json': canonical(installed_profile(profile, plan)), 'codex-auth.json': auth,
                            'gh/hosts.yml': github})
        run(['docker', 'start', ident])
        print('New goal-only controller started: ' + ident)
        print('Execution still requires a fresh owner-approved goal Issue. Publication requires separate exact-content approval.')
        print('Inspect status: docker logs --tail 30 ' + ident)


if __name__ == '__main__':
    try: main()
    except (ContractError, OSError, ValueError, subprocess.CalledProcessError):
        print('Goal installation blocked. No fallback or existing deployment reuse was attempted.', file=sys.stderr)
        raise SystemExit(1)
