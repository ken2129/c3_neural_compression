"""Fixed runner bootstrap, executed only after Popen's permanent UID drop."""
import json
import os
from pathlib import Path
import stat
import sys

from .schema import ContractError


def require_runner_identity():
    if (os.getresuid()!=(10003,)*3 or os.getresgid()!=(10003,)*3 or os.getgroups()):
        raise ContractError('runner UID/GID contract rejected')
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if (any(int(status[key],16) for key in ('CapEff','CapPrm','CapInh','CapAmb'))
            or status['NoNewPrivs'].strip()!='1'):
        raise ContractError('runner capability contract rejected')


def exec_private_runner(directory):
    # Caller establishes the fixed permanent UID first. This helper never
    # changes ownership/mode or impersonates another user to traverse the path.
    info=directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid()
            or stat.S_IMODE(info.st_mode)!=0o700):
        raise ContractError('runner private directory rejected')
    binary=directory/'bin/Runner.Listener';info=binary.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
            or info.st_mode&0o022 or not info.st_mode&0o111):
        raise ContractError('runner executable rejected')
    os.chdir(directory)
    os.execve(str(binary),[str(binary),'run','--startuptype','service'],dict(os.environ))


def main():
    stage='identity'
    try:
        require_runner_identity()
        stage='private_directory'
        exec_private_runner(Path('/runner'))
    except BaseException as exc:
        # No exception text, credentials, arguments or runner output here.
        print(json.dumps(dict(phase='RESIDENT_RUNNER_BOOTSTRAP_BLOCKED',stage=stage,
            reason='PERMISSION_DENIED' if isinstance(exc,PermissionError) else 'CONTRACT_REJECTED')),
            file=sys.stderr,flush=True)
        os._exit(125)
