"""Manager-only installed-recipe transport; CPU workers never receive SSH access."""
import os
from pathlib import Path
import select
import shlex
import stat
import subprocess
import time

from .schema import ContractError, canonical, loads
from .gpu_contract import installed_target, validate_gpu_result


class GPUCleanupUnverified(ContractError):
    def __init__(self):
        super().__init__('GPU_REMOTE_CLEANUP_UNVERIFIED')


def ssh_argv(target):
    """All values come from protected operator config, never Issue commands."""
    remote = shlex.join(['docker', 'exec', '-i', '--user', str(target['container_uid']),
        target['container'], target['python'], '-I', '-B', target['agent'],
        '--registry', target['registry']])
    return ['/usr/bin/ssh', '-T', '-F', '/dev/null', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes',
        '-o', 'UserKnownHostsFile='+target['known_hosts_file'],
        '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
        '-o', 'PermitLocalCommand=no', '-o', 'ProxyCommand=none', '-o', 'ProxyJump=none',
        '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
        '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2',
        '-i', target['identity_file'], '-p', str(target['ssh_port']), '--',
        target['ssh_user']+'@'+target['ssh_host'], remote]


def check_ssh_material(target):
    for key in ('identity_file', 'known_hosts_file'):
        path = Path(target[key])
        for parent in (path, *path.parents):
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in (0, 10001):
                raise ContractError('protected manager SSH material required')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 10001
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or not 0 < info.st_size <= 65536):
            raise ContractError('private manager SSH material rejected')


class SSHDockerGPUBackend:
    """One connection for waiting, running and collecting a single claimed job."""
    def __init__(self, profile, request, *, popen=subprocess.Popen,
                 credential_check=check_ssh_material, clock=time.monotonic, status=None):
        self.target, self.definition = installed_target(profile, request)
        self.request = dict(request)
        self.popen, self.credential_check, self.clock = popen, credential_check, clock
        self.status = status or (lambda phase, elapsed: None)

    def run(self, binding, recheck, *, on_result=lambda result: None):
        from .service_state import validate_binding
        validate_binding(binding)
        recheck()
        self.credential_check(self.target)
        proc = self.popen(ssh_argv(self.target), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, bufsize=0, start_new_session=True)
        deadline = self.clock() + self.request['max_wait_seconds'] + self.request['max_seconds'] + 20
        buffer = bytearray()
        aborted = None
        cancel_deadline = None
        verified = False
        def send(value):
            raw = canonical(value)+b'\n'
            while raw:
                count = proc.stdin.write(raw)
                if not count:
                    raise GPUCleanupUnverified()
                raw = raw[count:]
            proc.stdin.flush()
        try:
            send(dict(op='execute', binding=binding, request=self.request,
                      definition_sha256=self.definition['definition_sha256']))
            while True:
                if aborted is None:
                    try:
                        recheck()
                        if self.clock() >= deadline:
                            raise TimeoutError('GPU stage deadline exceeded')
                    except BaseException as exc:
                        aborted = exc
                        send({'op': 'cancel', 'binding': binding})
                        cancel_deadline = self.clock() + 20
                if cancel_deadline is not None and self.clock() >= cancel_deadline:
                    raise GPUCleanupUnverified()
                if b'\n' not in buffer:
                    if len(buffer) > 32768:
                        raise GPUCleanupUnverified()
                    if not select.select([proc.stdout], [], [], .2)[0]:
                        if proc.poll() is not None:
                            raise GPUCleanupUnverified()
                        continue
                    piece = os.read(proc.stdout.fileno(), min(4096, 32769-len(buffer)))
                    if not piece:
                        raise GPUCleanupUnverified()
                    buffer.extend(piece)
                    continue
                line, _, tail = buffer.partition(b'\n'); buffer = bytearray(tail)
                try:
                    frame = loads(bytes(line))
                except Exception:
                    raise GPUCleanupUnverified() from None
                if type(frame) is not dict:
                    raise GPUCleanupUnverified()
                if set(frame) == {'op', 'phase', 'elapsed_seconds'} and frame['op'] == 'status':
                    if (frame['phase'] not in ('waiting_gpu', 'running', 'collecting')
                            or type(frame['elapsed_seconds']) not in (int, float)
                            or not 0 <= frame['elapsed_seconds'] <= 172830):
                        raise GPUCleanupUnverified()
                    self.status(frame['phase'], frame['elapsed_seconds'])
                    continue
                if set(frame) != {'op', 'result'} or frame['op'] != 'result':
                    raise GPUCleanupUnverified()
                try:
                    result = validate_gpu_result(frame['result'], binding=binding,
                        request=self.request, definition=self.definition)
                except Exception:
                    raise GPUCleanupUnverified() from None
                verified = True
                on_result(result)
                if aborted is not None:
                    raise aborted
                if buffer.strip():
                    raise ContractError('unexpected GPU data after completion')
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise ContractError('GPU transport did not exit after completion') from None
                if code != 0:
                    raise ContractError('GPU transport failed after verified cleanup')
                if proc.stdout.read(32769).strip():
                    raise ContractError('unexpected GPU trailing output')
                return result
        except BaseException:
            if not verified:
                raise GPUCleanupUnverified() from None
            raise
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=2)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    stream.close()
