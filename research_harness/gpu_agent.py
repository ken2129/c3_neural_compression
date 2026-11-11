"""One installed recipe inside an existing GPU container; no daemon or host writes.

Only trusted pinned recipe code is supported. Resource peaks are sampled; this
does not turn the shared work container into an adversarial code sandbox.
"""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time

from .schema import ContractError, canonical, fields, loads
from .gpu_contract import (MAX_RESULT_BYTES, absolute, definition_digest, integer,
                           name, public_paths, validate_gpu_request, validate_gpu_result)
from .service_state import validate_binding


class AgentStop(Exception):
    def __init__(self, reason):
        self.reason = reason


def private_file(path, limit):
    path = Path(path)
    for parent in (path, *path.parents):
        info = parent.lstat()
        sticky_root = stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if stat.S_ISLNK(info.st_mode) or (info.st_mode & 0o022 and not sticky_root):
            raise ContractError('GPU registry path is not protected')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_nlink != 1 or info.st_size > limit or info.st_mode & 0o077):
            raise ContractError('GPU registry file rejected')
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ContractError('GPU registry too large')
        return data
    finally:
        os.close(fd)


def validate_recipe(value):
    fields(value, ('argv', 'collect_argv', 'cwd', 'environment', 'source_files',
                   'gpu_uuids', 'limits', 'public_outputs'))
    absolute(value['cwd'])
    for key in ('argv', 'collect_argv'):
        argv = value[key]
        if type(argv) is not list or not (1 if key == 'argv' else 0) <= len(argv) <= 64:
            raise ContractError('fixed GPU recipe argv required')
        if argv:
            absolute(argv[0])
        if any(type(x) is not str or not x or len(x) > 2048 or '\x00' in x or '\n' in x for x in argv):
            raise ContractError('GPU recipe argument rejected')
    if type(value['environment']) is not dict or len(value['environment']) > 32:
        raise ContractError('bounded GPU recipe environment required')
    for key, text in value['environment'].items():
        if (type(key) is not str or not key.replace('_', '').isalnum()
                or type(text) is not str or len(text) > 2048 or '\x00' in text):
            raise ContractError('GPU recipe environment rejected')
    files = value['source_files']
    if type(files) is not dict or not 1 <= len(files) <= 128:
        raise ContractError('pinned recipe sources/assets required')
    for path, digest in files.items():
        absolute(path)
        if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ContractError('GPU source digest rejected')
    uuids = value['gpu_uuids']
    if (type(uuids) is not list or not 1 <= len(uuids) <= 16 or len(set(uuids)) != len(uuids)
            or any(type(u) is not str or not u.startswith('GPU-') or len(u) > 80
                   or any(c not in 'GPU-0123456789abcdefABCDEF' for c in u) for u in uuids)):
        raise ContractError('explicit GPU UUID allowlist required')
    limits = value['limits']
    fields(limits, ('max_seconds', 'max_wait_seconds', 'cpu_cores', 'memory_bytes',
                   'max_processes', 'max_artifact_bytes', 'disk_reserve_bytes', 'idle_samples'))
    for key, low, high in (('max_seconds', 30, 86400), ('max_wait_seconds', 0, 86400),
                          ('cpu_cores', 1, 64), ('memory_bytes', 64*1024**2, 256*1024**3),
                          ('max_processes', 1, 1024), ('max_artifact_bytes', 1024, 1024**4),
                          ('disk_reserve_bytes', 1, 1024**5), ('idle_samples', 1, 180)):
        integer(limits[key], low, high)
    public_paths(value['public_outputs'])
    return value


def gpu_observation():
    rows = subprocess.check_output(['/usr/bin/nvidia-smi', '--query-gpu=uuid,utilization.gpu,memory.used',
        '--format=csv,noheader,nounits'], text=True, timeout=10).splitlines()
    devices = {}
    for row in rows:
        uuid, util, memory = [p.strip() for p in row.split(',')]
        devices[uuid] = (float(util), float(memory))
    active = subprocess.check_output(['/usr/bin/nvidia-smi', '--query-compute-apps=gpu_uuid,pid',
        '--format=csv,noheader'], text=True, timeout=10).splitlines()
    counts = {}
    for row in active:
        uuid, pid = [p.strip() for p in row.split(',')]
        if not pid.isdigit():
            raise ContractError('GPU process observation rejected')
        counts[uuid] = counts.get(uuid, 0) + 1
    return devices, counts


def idle(uuid, observation):
    devices, counts = observation
    return uuid in devices and devices[uuid][0] <= 5 and devices[uuid][1] <= 256 and not counts.get(uuid)


class Control:
    """Raw bounded framing throughout; no BufferedReader/os.read mixing."""
    def __init__(self, incoming, outgoing):
        self.incoming, self.outgoing = incoming, outgoing
        self.buffer = bytearray()

    def emit(self, value):
        data = canonical(value)
        if len(data) > 32768:
            raise ContractError('GPU wire frame too large')
        self.outgoing.write(data + b'\n'); self.outgoing.flush()

    def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if b'\n' in self.buffer:
                line, _, tail = self.buffer.partition(b'\n'); self.buffer = bytearray(tail)
                return loads(bytes(line))
            if len(self.buffer) > 32768:
                raise ContractError('GPU input frame too large')
            remaining = max(0, deadline - time.monotonic())
            if not select.select([self.incoming], [], [], remaining)[0]:
                return None
            piece = os.read(self.incoming.fileno(), min(4096, 32769-len(self.buffer)))
            if not piece:
                raise AgentStop('CANCELLED')
            self.buffer.extend(piece)

    def check(self, binding):
        value = self.receive(0)
        if value is not None:
            if value != {'op': 'cancel', 'binding': binding}:
                raise ContractError('GPU control frame rejected')
            raise AgentStop('CANCELLED')


def verify_assets(recipe, check):
    for path, expected in recipe['source_files'].items():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise AgentStop('INPUT_CHANGED')
            digest = hashlib.sha256()
            while True:
                check()
                block = os.read(fd, 1024*1024)
                if not block:
                    break
                digest.update(block)
            if digest.hexdigest() != expected:
                raise AgentStop('INPUT_CHANGED')
        finally:
            os.close(fd)


def tree_bytes(root):
    size = 0
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(base, name); info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise AgentStop('RESOURCE_LIMIT')
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
    return size


def subreaper():
    if sys.platform != 'linux' or not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ContractError('GPU agent requires Linux subreaping and pidfds')
    libc = ctypes.CDLL(None, use_errno=True)
    observed = ctypes.c_int()
    if libc.prctl(36, 1, 0, 0, 0) or libc.prctl(37, ctypes.byref(observed), 0, 0, 0) or observed.value != 1:
        raise ContractError('GPU subreaper could not be observed')


def family():
    """Only this dedicated agent's children, including adopted escaped sessions."""
    records = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry/'stat').read_text().rsplit(')', 1)[1].split()
            status = dict(line.split(':', 1) for line in (entry/'status').read_text().splitlines() if ':' in line)
            records[int(entry.name)] = (int(parts[1]), int(parts[19]),
                int(status.get('VmRSS', '0 kB').split()[0])*1024, int(status['Threads']))
        except (FileNotFoundError, ProcessLookupError):
            continue
    owned = {os.getpid()}
    while True:
        new = {pid for pid, record in records.items() if record[0] in owned} - owned
        if not new:
            break
        owned.update(new)
    return {pid: records[pid] for pid in owned if pid != os.getpid()}


def cleanup_family():
    deadline = time.monotonic() + 8
    gentle = time.monotonic() + 3
    sent = set()
    while time.monotonic() < deadline:
        sig = signal.SIGTERM if time.monotonic() < gentle else signal.SIGKILL
        for pid, record in family().items():
            identity = (pid, record[1], sig)
            if identity in sent:
                continue
            try:
                fd = os.pidfd_open(pid)
                try:
                    now = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
                    if int(now[19]) != record[1]:
                        raise ContractError('GPU child identity changed')
                    signal.pidfd_send_signal(fd, sig)
                    sent.add(identity)
                finally:
                    os.close(fd)
            except (ProcessLookupError, FileNotFoundError):
                continue
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                return True  # ECHILD, not merely the original process-group exit.
        time.sleep(.02)
    return False


def collect_files(run_dir, paths):
    result = {}
    for relative in paths:
        fds = [os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
        try:
            components = relative.split('/')
            for part in components[:-1]:
                fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
            fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
            fds.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_RESULT_BYTES:
                raise AgentStop('OUTPUT_REJECTED')
            raw = os.read(fd, MAX_RESULT_BYTES + 1)
            if len(raw) > MAX_RESULT_BYTES:
                raise AgentStop('OUTPUT_REJECTED')
            result[relative] = raw.decode('utf-8')
        finally:
            for fd in reversed(fds):
                os.close(fd)
    return result


class RecipeAgent:
    def __init__(self, registry, control, *, observe=gpu_observation):
        fields(registry, ('schema', 'root', 'recipes'))
        if registry['schema'] != 'c3-gpu-registry-v1' or type(registry['recipes']) is not dict:
            raise ContractError('GPU registry rejected')
        absolute(registry['root'])
        self.root = Path(registry['root'])
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ContractError('private GPU run root required')
        for key, recipe in registry['recipes'].items():
            name(key); validate_recipe(recipe)
        self.registry, self.control, self.observe = registry, control, observe

    def execute(self, value):
        fields(value, ('op', 'binding', 'request', 'definition_sha256'))
        if value['op'] != 'execute':
            raise ContractError('GPU execution request rejected')
        validate_binding(value['binding'])
        request = validate_gpu_request(value['request'])
        recipe = self.registry['recipes'].get(request['recipe'])
        if recipe is None or definition_digest(recipe) != value['definition_sha256']:
            raise ContractError('GPU recipe definition changed')
        limits = recipe['limits']
        if any(request[k] > limits[k] for k in ('max_seconds', 'max_wait_seconds')):
            raise ContractError('GPU request exceeds agent ceiling')
        run_id = value['binding']['run_id']
        if len(run_id) > 128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in run_id):
            raise ContractError('GPU run identity rejected')
        run_dir = self.root / run_id
        run_dir.mkdir(mode=0o700)  # Existing/uncertain runs are never reset or retried.
        started = time.monotonic()
        overall = started + request['max_wait_seconds'] + request['max_seconds']
        lock = None
        cleanup = True
        receipt = dict(schema='c3-gpu-result-v1', binding=value['binding'], machine=request['machine'],
            recipe=request['recipe'], definition_sha256=value['definition_sha256'], outcome='failed',
            reason='EXECUTOR_REJECTED', elapsed_seconds=0, cleanup_verified=True, files={})

        def check():
            self.control.check(value['binding'])
            if time.monotonic() >= overall - 10:
                raise AgentStop('RUN_TIMEOUT')

        def status(phase):
            self.control.emit(dict(op='status', phase=phase, elapsed_seconds=time.monotonic()-started))

        try:
            subreaper()
            available = sorted(os.sched_getaffinity(0))
            if len(available) < limits['cpu_cores']:
                raise ContractError('GPU CPU affinity unavailable')
            os.sched_setaffinity(0, available[-limits['cpu_cores']:])
            verify_assets(recipe, check)
            wait_deadline = min(overall - 10, time.monotonic() + request['max_wait_seconds'])
            stable = {uuid: 0 for uuid in recipe['gpu_uuids']}
            while True:
                check()
                observation = self.observe()
                selected = None
                for uuid in recipe['gpu_uuids']:
                    stable[uuid] = stable[uuid] + 1 if idle(uuid, observation) else 0
                    if stable[uuid] >= limits['idle_samples']:
                        fd = os.open(self.root / (uuid + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            os.close(fd); continue
                        if idle(uuid, self.observe()):
                            lock, selected = fd, uuid
                            break
                        os.close(fd)
                if selected is not None:
                    break
                if time.monotonic() >= wait_deadline:
                    raise AgentStop('WAIT_TIMEOUT')
                status('waiting_gpu'); time.sleep(min(1, max(0, wait_deadline-time.monotonic())))
            verify_assets(recipe, check)
            gpu_deadline = min(overall - 10, time.monotonic() + request['max_seconds'] - 10)
            for phase, argv in (('running', recipe['argv']), ('collecting', recipe['collect_argv'])):
                if not argv:
                    continue
                check(); status(phase)
                cleanup = False
                self._command(argv, phase, recipe, run_dir, selected, gpu_deadline, check)
                cleanup = cleanup_family()
                if not cleanup:
                    raise ContractError('GPU descendants not reaped')
            verify_assets(recipe, check)
            receipt.update(outcome='completed', reason=None,
                           files=collect_files(run_dir, recipe['public_outputs']))
            try:
                validate_gpu_result(dict(receipt, elapsed_seconds=time.monotonic()-started),
                                    binding=value['binding'], request=request)
            except ContractError:
                raise AgentStop('OUTPUT_REJECTED') from None
        except AgentStop as exc:
            receipt.update(outcome='cancelled' if exc.reason == 'CANCELLED' else 'failed', reason=exc.reason, files={})
        except (OSError, ValueError, ContractError):
            receipt.update(outcome='failed', reason='EXECUTOR_REJECTED', files={})
        finally:
            cleanup = cleanup_family() if not cleanup else cleanup
            receipt.update(elapsed_seconds=time.monotonic()-started, cleanup_verified=cleanup)
            if not cleanup:
                # No terminal completion frame can certify unobserved cleanup.
                raise ContractError('GPU cleanup unverified')
            if lock is not None:
                os.close(lock)
            (run_dir/'executor-receipt.json').write_bytes(canonical(receipt))
        return validate_gpu_result(receipt, binding=value['binding'], request=request)

    def _command(self, argv, phase, recipe, run_dir, uuid, deadline, check):
        limits = recipe['limits']
        def expand(text):
            return text.replace('{run_dir}', str(run_dir)).replace('{gpu_uuid}', uuid)
        env = dict(os.environ, **{key: expand(text) for key, text in recipe['environment'].items()})
        env.update(CUDA_VISIBLE_DEVICES=uuid if phase == 'running' else '',
                   JAX_PLATFORMS='cuda,cpu' if phase == 'running' else 'cpu',
                   XLA_PYTHON_CLIENT_PREALLOCATE='false', PYTHONDONTWRITEBYTECODE='1',
                   OMP_NUM_THREADS=str(limits['cpu_cores']), OPENBLAS_NUM_THREADS=str(limits['cpu_cores']),
                   MKL_NUM_THREADS=str(limits['cpu_cores']), TF_NUM_INTRAOP_THREADS=str(limits['cpu_cores']),
                   TF_NUM_INTEROP_THREADS='1', TMPDIR=str(run_dir), XDG_CACHE_HOME=str(run_dir/'cache'),
                   CUDA_CACHE_PATH=str(run_dir/'cache/cuda'), JAX_COMPILATION_CACHE_DIR=str(run_dir/'cache/jax'))
        parent = os.getpid()
        def child_setup():
            allowed = sorted(os.sched_getaffinity(0))
            if len(allowed) < limits['cpu_cores']:
                raise RuntimeError('CPU allocation unavailable')
            os.sched_setaffinity(0, allowed[-limits['cpu_cores']:])
            libc = ctypes.CDLL(None)
            if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(1, signal.SIGKILL) or os.getppid() != parent:
                raise RuntimeError('GPU parent-death guard unavailable')
        with (run_dir/(phase+'.log')).open('xb') as log:
            proc = subprocess.Popen([expand(x) for x in argv], cwd=recipe['cwd'], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, preexec_fn=child_setup)
            while proc.poll() is None:
                check()
                if time.monotonic() >= deadline:
                    raise AgentStop('RUN_TIMEOUT')
                descendants = family()
                free = os.statvfs(run_dir)
                if (sum(r[2] for r in descendants.values()) > limits['memory_bytes']
                        or sum(r[3] for r in descendants.values()) > limits['max_processes']
                        or tree_bytes(run_dir) > limits['max_artifact_bytes']
                        or free.f_bavail*free.f_frsize < limits['disk_reserve_bytes']):
                    raise AgentStop('RESOURCE_LIMIT')
                if phase == 'running' and self.observe()[1].get(uuid, 0) > 1:
                    raise AgentStop('GPU_COMPETITION')
                time.sleep(.2)
            if proc.returncode != 0:
                raise AgentStop('COMMAND_FAILED')
        # Also catch fast successful commands that exceed limits before a tick.
        if tree_bytes(run_dir) > limits['max_artifact_bytes']:
            raise AgentStop('RESOURCE_LIMIT')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    if os.geteuid() == 0:
        raise ContractError('GPU recipe agent must run as the installed non-root user')
    control = Control(sys.stdin.buffer.raw, sys.stdout.buffer)
    def stop(*_):
        raise AgentStop('CANCELLED')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    registry = loads(private_file(args.registry, 65536))
    agent = RecipeAgent(registry, control)
    result = agent.execute(control.receive(10))
    control.emit({'op': 'result', 'result': result})
