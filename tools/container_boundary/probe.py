"""Finite, dummy-only UID boundary diagnostic. Never enables execution."""
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

CONTROL_UID = 10001
WORKER_UID = 10002
SCRIPT = '/opt/boundary/probe.py'


def status():
    values = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    return {key: values[key].strip() for key in ('Uid', 'Gid', 'Groups', 'CapEff', 'CapPrm', 'CapAmb', 'NoNewPrivs')}


def identity_ok(values, uid):
    return (values['Uid'].split() == [str(uid)] * 4
            and values['Gid'].split() == [str(uid)] * 4
            and not values['Groups'].split()
            and all(int(values[k], 16) == 0 for k in ('CapEff', 'CapPrm', 'CapAmb'))
            and values['NoNewPrivs'] == '1')


def denied(operation):
    """Only a real permission denial counts; missing target is inconclusive."""
    try:
        operation()
    except OSError as exc:
        return exc.errno in (errno.EACCES, errno.EPERM)
    return False


def attempt_open(path, flags):
    fd = os.open(path, flags)
    os.close(fd)


def manager():
    signal.alarm(15)
    if not identity_ok(status(), CONTROL_UID):
        raise RuntimeError('manager identity does not satisfy contract')
    os.umask(0o077)
    for name in ('dummy-credential', 'dummy-ledger'):
        Path('/control', name).write_text('DUMMY ONLY\n')
    # Keep a protected descriptor open for the /proc/fd denial check.
    with open('/control/dummy-credential') as protected:
        print(json.dumps({'pid': os.getpid(), 'fd': protected.fileno()}), flush=True)
        # stdin is a private pipe; supervisor closes it after worker completes.
        sys.stdin.buffer.read(1)


def worker(pid, fd):
    signal.alarm(10)
    checks = {'worker_identity': identity_ok(status(), WORKER_UID)}
    for name in ('dummy-credential', 'dummy-ledger'):
        for label, flags in [('read', os.O_RDONLY), ('write', os.O_WRONLY)]:
            checks[name + '_' + label] = denied(lambda n=name, f=flags: attempt_open('/control/' + n, f))
    for suffix in ('environ', 'mem', 'fd/' + str(fd)):
        checks['manager_proc_' + suffix] = denied(lambda s=suffix: attempt_open(f'/proc/{pid}/{s}', os.O_RDONLY))
    # Signal 0 checks permission without delivering a signal to any process.
    checks['manager_signal'] = denied(lambda: os.kill(pid, 0))
    checks['become_root'] = denied(lambda: os.setuid(0))
    checks['docker_socket_absent'] = not Path('/var/run/docker.sock').exists()
    checks['no_auth_environment'] = set(os.environ) <= {'LANG', 'LC_CTYPE'}
    Path('/work/owned-output').write_text('dummy worker output\n')
    checks['own_output_writable'] = Path('/work/owned-output').read_text() == 'dummy worker output\n'
    print(json.dumps({'checks': checks, 'execution_allowed': False}), flush=True)
    return 0 if all(checks.values()) else 1


def supervise():
    # Requires the dedicated Compose container. Never invoke this on a host.
    if not Path('/.dockerenv').exists() or os.getuid() != 0:
        raise RuntimeError('dedicated diagnostic container required')
    signal.alarm(25)
    child_env = {'LANG': 'C.UTF-8'}
    control = subprocess.Popen([sys.executable, '-I', '-B', SCRIPT, 'manager'],
                               user=CONTROL_UID, group=CONTROL_UID, extra_groups=[],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               env=child_env, close_fds=True, text=True)
    try:
        ready = json.loads(control.stdout.readline())
        if ready['pid'] != control.pid or type(ready['fd']) is not int:
            raise RuntimeError('invalid manager readiness')
        result = subprocess.run([sys.executable, '-I', '-B', SCRIPT, 'worker', str(control.pid), str(ready['fd'])],
                                user=WORKER_UID, group=WORKER_UID, extra_groups=[],
                                env=child_env, close_fds=True, timeout=12, check=False)
        control.stdin.close()
        control.wait(timeout=3)
        if control.returncode:
            raise RuntimeError('manager failed')
        return result.returncode
    finally:
        # Own finite dummy child only. No process search or unrelated signals.
        if not control.stdin.closed:
            control.stdin.close()


if __name__ == '__main__':
    try:
        if len(sys.argv) == 1:
            code = supervise()
        elif sys.argv[1:] == ['manager']:
            manager()
            code = 0
        elif len(sys.argv) == 4 and sys.argv[1] == 'worker':
            code = worker(int(sys.argv[2]), int(sys.argv[3]))
        else:
            raise RuntimeError('unsupported diagnostic mode')
        sys.exit(code)
    except Exception as exc:
        print(json.dumps({'diagnostic_failed': type(exc).__name__, 'execution_allowed': False}), file=sys.stderr)
        sys.exit(1)
