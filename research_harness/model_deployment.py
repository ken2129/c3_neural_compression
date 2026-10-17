"""Explicit operator configuration for the optional fixed model canary.

Protected configuration records consent, not proof of isolation. The supervisor
still checks the live namespace, UIDs, capabilities and resource boundary. No
credential is returned to the manager protocol or public result.
"""
import os
from pathlib import Path
import stat
import subprocess
import sys
from .schema import ContractError, fields, loads

CONFIG_KEYS=('source_sha','model','codex_version','sandbox_mode','external_network',
 'external_boundary_approved','worker_auth_exposure_acknowledged','usage_without_hard_cost_cap_approved')


def validate_model_config(data, *, source_sha, model):
    fields(data,CONFIG_KEYS)
    if data['source_sha']!=source_sha or data['model']!=model or data['codex_version']!='0.160.0':
        raise ContractError('MODEL_CONFIGURATION_REVISION_OR_MODEL_MISMATCH')
    if data['sandbox_mode']!='externalSandbox' or data['external_network']!='enabled':
        raise ContractError('MODEL_EXTERNAL_BOUNDARY_NOT_CONFIGURED')
    for name in CONFIG_KEYS[5:]:
        if data[name] is not True:raise ContractError('MODEL_OPERATOR_CONSENT_MISSING')
    from .app_server_adapter import fixed_app_server_request
    return fixed_app_server_request(model)


def read_private(path, *, limit):
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=10001
            or info.st_mode & 0o777!=0o600 or info.st_size>limit):
        raise ContractError('MODEL_PROTECTED_INPUT_INVALID')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        opened=os.fstat(fd)
        if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):raise ContractError('MODEL_INPUT_CHANGED')
        raw=os.read(fd,limit+1)
        if len(raw)>limit:raise ContractError('MODEL_INPUT_TOO_LARGE')
        return raw
    finally:os.close(fd)


def validate_chatgpt_auth(raw):
    value=loads(raw)
    if type(value) is not dict or value.get('auth_mode')!='chatgpt' or type(value.get('tokens')) is not dict:
        raise ContractError('MODEL_DEDICATED_CHATGPT_LOGIN_REQUIRED')
    if value.get('OPENAI_API_KEY') is not None:raise ContractError('MODEL_API_KEY_NOT_ADMITTED')
    for name in ('access_token','refresh_token','id_token','account_id'):
        token=value['tokens'].get(name)
        if type(token) is not str or not token.strip() or len(token)>32768:
            raise ContractError('MODEL_DEDICATED_CHATGPT_LOGIN_REQUIRED')
    # Presence is not proof of validity; authentication errors are not retried.


def prepare_model(*,source_sha,model):
    """PID1 only, before any claim; fresh worker directory and auth-only copy.

    A human must separately provision these files after approving persistent
    credential use. Missing setup never starts login or copies ambient HOME.
    """
    if os.getpid()!=1 or os.getresuid()!=(0,0,0):raise ContractError('MODEL_DEDICATED_PID1_REQUIRED')
    try:
        os.seteuid(10001)
        try:
            config=loads(read_private(Path('/control/model.json'),limit=8192))
            auth=read_private(Path('/control/codex-auth.json'),limit=65536)
        except FileNotFoundError as exc:raise ContractError('MODEL_OPERATOR_SETUP_MISSING') from exc
    finally:os.seteuid(0)
    request=validate_model_config(config,source_sha=source_sha,model=model)
    validate_chatgpt_auth(auth)
    try:
        os.seteuid(10002)
        # Fresh namespace/tmpfs only. Never reuse worker configuration or state.
        for name in ('canary','worker-home','codex-home'):
            path=Path('/work')/name
            path.mkdir(mode=0o700)
        git=Path('/work/canary/.git');git.mkdir(mode=0o700)
        for name in ('objects','refs','refs/heads','refs/tags'):(git/name).mkdir(mode=0o700)
        for path,raw in ((git/'HEAD',b'ref: refs/heads/main\n'),
                         (git/'config',b'[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n'),
                         (Path('/work/codex-home/auth.json'),auth)):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            try:
                with os.fdopen(fd,'wb',closefd=False) as stream:stream.write(raw);stream.flush();os.fsync(fd)
            finally:os.close(fd)
    finally:os.seteuid(0)
    binary=Path('/opt/codex/bin/codex')
    if not binary.exists():raise ContractError('MODEL_CODEX_BINARY_NOT_INSTALLED')
    probe="""
import os,resource
if os.getresuid()!=(10002,)*3 or os.getresgid()!=(10002,)*3 or os.getgroups():raise SystemExit(125)
for kind,n in ((resource.RLIMIT_CORE,0),(resource.RLIMIT_CPU,10),
              (resource.RLIMIT_AS,536870912),(resource.RLIMIT_FSIZE,1048576),
              (resource.RLIMIT_NPROC,32),(resource.RLIMIT_NOFILE,64)):
    resource.setrlimit(kind,(n,n))
os.execve('/opt/codex/bin/codex',['/opt/codex/bin/codex','--version'],dict(os.environ))
"""
    result=subprocess.run([sys.executable,'-I','-S','-c',probe],user=10002,group=10002,extra_groups=[],
        env={'PATH':'/usr/bin:/bin','HOME':'/work/worker-home','CODEX_HOME':'/work/codex-home'},
        cwd='/',stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5,check=False)
    if result.returncode or result.stdout.strip()!=b'codex-cli 0.160.0' or result.stderr:
        raise ContractError('MODEL_CODEX_VERSION_MISMATCH')
    return request
