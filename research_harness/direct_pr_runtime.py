"""Direct-PR staging/admission, before the worker exists. No network or publication."""
import hashlib
import os
from pathlib import Path
import stat

from .schema import ContractError, canonical, loads
from .worker_pr import (validate_worker_pr_config, CONTROL_CONFIG_PATH, CONTROL_TOKEN_PATH,
                        WORKER_CONFIG_PATH, WORKER_TOKEN_PATH, WorkerGitHub)

WORKER = 10002
CONTROL = 10001


def in_scope(path, scopes):
    return any(path == scope or path.startswith(scope + '/') for scope in scopes)


def _worker_directory(path, as_uid):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    mode = stat.S_IMODE(path.parent.stat().st_mode)
    path.parent.chmod(0o777)
    try:
        as_uid(WORKER)
        path.mkdir(mode=0o700)
    finally:
        as_uid(0)
        path.parent.chmod(mode)


def _write_new(path, raw, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def stage_direct_workspace(plan, *, as_uid):
    from .model_deployment import read_private, validate_chatgpt_auth
    if os.listdir('/work'):
        raise ContractError('direct PR workspace must be fresh')
    scopes = plan['write_scope']
    if any(a != b and (a.startswith(b + '/') or b.startswith(a + '/')) for a in scopes for b in scopes):
        raise ContractError('overlapping write scopes')
    if any(path == scope or scope.startswith(path + '/') for path in plan['inputs'] for scope in scopes):
        raise ContractError('input file conflicts with writable directory')
    os.umask(0o022)
    root = Path('/work/goal')
    root.mkdir(mode=0o755)
    for scope in scopes:
        _worker_directory(root / scope, as_uid)
    for name in ('worker-home', 'codex-home'):
        _worker_directory(Path('/work') / name, as_uid)
    manifest = loads(Path('/opt/c3-runtime/goal-inputs.json').read_bytes())
    if manifest['source_sha'] != plan['source_sha'] or set(manifest['files']) != set(plan['inputs']):
        raise ContractError('direct input manifest mismatch')
    for path in plan['inputs']:
        raw = (Path('/opt/goal-inputs') / path).read_bytes()
        entry = manifest['files'][path]
        if len(raw) != entry['size_bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ContractError('direct input digest mismatch')
        target = root / path
        writable = in_scope(path, scopes)
        try:
            if writable:
                as_uid(WORKER)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            _write_new(target, raw, 0o600 if writable else 0o444)
        finally:
            as_uid(0)
    try:
        as_uid(CONTROL)
        auth = read_private(Path('/control/codex-auth.json'), limit=65536)
        validate_chatgpt_auth(auth)
        config = validate_worker_pr_config(loads(read_private(Path(CONTROL_CONFIG_PATH), limit=16384)))
        if config['enabled'] is not True:
            raise ContractError('worker GitHub access remains disabled')
        token = read_private(Path(CONTROL_TOKEN_PATH), limit=8192)
        if not token.strip() or b'\n' in token.strip() or b'\r' in token.strip():
            raise ContractError('dedicated worker token must be one line')
        # This must precede worker access, not just happen in the optional helper.
        # Same-owner access needs its explicit config acknowledgment; a token
        # must still match that selected actor before it reaches the worker.
        actor = WorkerGitHub(token.decode('ascii').strip()).request('GET', '/user')
        if (type(actor) is not dict or type(actor.get('id')) is not int or actor.get('id') != config['actor_id']
                or type(actor.get('login')) is not str
                or actor['login'].lower() != config['actor_login'].lower()):
            raise ContractError('actual worker credential identity mismatch')
        as_uid(WORKER)
        _write_new('/work/codex-home/auth.json', auth, 0o600)
        _write_new(WORKER_CONFIG_PATH, canonical(config), 0o600)
        _write_new(WORKER_TOKEN_PATH, token, 0o600)
        _write_new('/work/worker-home/approved-plan.json', canonical(plan), 0o600)
    finally:
        as_uid(0)
    return True  # Staged only; not a candidate or a publication approval.


def validate_direct_workspace(plan, manifest):
    """Initial-byte/ownership check before dispatch, never inspect model changes."""
    root = Path('/work/goal')
    expected_dirs = {''}
    for path in (*plan['inputs'], *plan['write_scope']):
        parts = path.split('/')
        expected_dirs.update('/'.join(parts[:n]) for n in range(1, len(parts)))
    expected_dirs.update(plan['write_scope'])
    expected = expected_dirs | set(plan['inputs'])
    found = {''}
    total = 0
    def walk(directory, prefix):
        nonlocal total
        os.seteuid(WORKER if in_scope(prefix.rstrip('/'), plan['write_scope']) else 0)
        for name in os.listdir(directory):
            relative = prefix + name
            child = directory / name
            found.add(relative)
            if relative not in expected:
                raise ContractError('unexpected initial direct workspace entry')
            info = child.lstat()
            owned = in_scope(relative, plan['write_scope'])
            owner = WORKER if owned else 0
            if info.st_uid != owner or info.st_mode & 0o022:
                raise ContractError('direct workspace ownership/mode mismatch')
            if relative in plan['inputs']:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ContractError('direct input must be regular')
                if stat.S_IMODE(info.st_mode) != (0o600 if owned else 0o444):
                    raise ContractError('direct input permissions mismatch')
                entry = manifest['files'][relative]
                if type(entry['size_bytes']) is not int or not 0 <= entry['size_bytes'] <= 16*1024**2:
                    raise ContractError('direct input size bound')
                if info.st_size != entry['size_bytes']:
                    raise ContractError('direct input size changed')
                raw = child.read_bytes()
                total += len(raw)
                if total > 16*1024**2 or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                    raise ContractError('direct input bytes changed')
            elif stat.S_ISDIR(info.st_mode):
                expected_mode = 0o700 if relative in plan['write_scope'] else 0o755
                if stat.S_IMODE(info.st_mode) != expected_mode:
                    raise ContractError('direct directory permissions mismatch')
                walk(child, relative + '/')
                os.seteuid(WORKER if in_scope(prefix.rstrip('/'), plan['write_scope']) else 0)
            else:
                raise ContractError('direct workspace special file rejected')
    # root can traverse both owners before any worker is launched.
    if stat.S_IMODE(root.lstat().st_mode) != 0o755 or root.lstat().st_uid != 0:
        raise ContractError('direct workspace root not protected')
    try:
        walk(root, '')
    finally:
        os.seteuid(0)
    if found != expected:
        raise ContractError('direct workspace entry missing')


def validate_worker_home(plan):
    def read_worker(path, limit):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != WORKER or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit):
            raise ContractError('worker credential/plan layout invalid')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                raise ContractError('worker credential/plan changed')
            raw = os.read(fd, limit + 1)
            if len(raw) > limit:
                raise ContractError('worker credential/plan too large')
            return raw
        finally:
            os.close(fd)
    expected = {'worker-pr.json', 'github-token', 'approved-plan.json'}
    home = Path('/work/worker-home')
    if set(os.listdir(home)) != expected:
        raise ContractError('direct worker home must contain only explicit credentials/plan')
    config = validate_worker_pr_config(loads(read_worker(home/'worker-pr.json', 16384)))
    if not config['enabled'] or loads(read_worker(home/'approved-plan.json', 32768)) != plan:
        raise ContractError('direct worker config/plan mismatch')
    if not read_worker(home/'github-token', 8192).strip():
        raise ContractError('direct worker credential missing')
