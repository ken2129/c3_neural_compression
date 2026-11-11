"""Concrete owned-UID lifecycle core for a dedicated Linux PID-1 container.

This is not wired to live Codex and has only fake-OS unit coverage. It must not
be deployed before kernel/container adversarial tests. The adapter remains
disabled. No operation here changes host settings, cgroups, or other containers.

The launcher must itself be PID 1 (not a child of tini), single-threaded, root
real/effective/saved UID, with only SETUID/SETGID and no-new-privileges. On an
uncertain stop the core exits *itself*: Linux kills the remaining processes in
that PID namespace. There is no kill(-1), process-group signal, or CAP_KILL.

References: man7.org/linux/man-pages/man2/{kill,seteuid,pidfd_send_signal}.2.html
and man7.org/linux/man-pages/man7/pid_namespaces.7.html; kernel.org/doc/html/latest/
admin-guide/cgroup-v2.html. UID equality is not a filesystem/network sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import json
import math
from pathlib import Path
import resource
import select
import selectors
import signal
import stat
import subprocess
import sys
import time

from research_harness.schema import ContractError
from research_harness.manager_runtime import RecheckRejected, safe_recheck_failure
from research_harness.codex_adapter import (CanaryCandidate, CanaryRequest, CliInspection, CodexAdapter,
                                            parse_candidate)
from research_harness.app_server_adapter import (
    AppServerAdapter, AppServerRequest, BoundedStdioTransport, PendingAppServerResult,
    validate_app_server_request, safe_app_server_diagnostic, safe_command_observation,
)


WORKER_UID = 10002
CONTROLLER_UID = 10001
SETUID_SETGID = (1 << 7) | (1 << 6)
MAX_MEMORY = 1024 * 1024 * 1024
MAX_PIDS = 64
MAX_WORK_BYTES = 256 * 1024 * 1024
CODEX_BINARY = '/opt/codex/bin/codex'
CODEX_WORKSPACE = '/work/canary'
CODEX_SCHEMA = '/opt/c3-runtime/canary-schema.json'
APP_SERVER_FINALIZATION_RESERVE_SECONDS = 20


def report_failure(role, stage, exc, *, child_returncode=None):
    """Best-effort fixed-vocabulary diagnostics; never stringify exceptions."""
    roles = {'launcher', 'manager', 'app_server'}
    stages = {'deployment', 'epoch', 'manager_start', 'manager_initialize',
              'protocol', 'plan', 'preflight', 'model_prepare', 'binding',
              'model_runtime', 'cpu_runtime', 'result', 'admission', 'reserve',
              'watchdog', 'approval', 'spawn', 'barrier', 'identity', 'release',
              'transport', 'turn', 'cleanup', 'postcheck', 'finalize', 'entrypoint'}
    category = 'other'
    for kind, name in ((PermissionError, 'permission'), (FileNotFoundError, 'missing_file'),
                       (TimeoutError, 'timeout'), (BrokenPipeError, 'broken_pipe'),
                       (ContractError, 'contract'), (OSError, 'os_error')):
        if isinstance(exc, kind):
            category = name
            break
    try:
        record = {'phase': 'RUNTIME_BLOCKED',
            'role': role if role in roles else 'unknown',
            'stage': stage if stage in stages else 'unknown',
            'error_class': category}
        if type(child_returncode) is int and -128 <= child_returncode <= 255:
            record['observed_child_returncode'] = child_returncode
        if type(exc) is RecheckRejected:
            record['approval_failure'] = safe_recheck_failure(exc)
        detail = safe_app_server_diagnostic(exc)
        if detail is not None:
            record['app_server'] = detail
        print(json.dumps(record), file=sys.stderr, flush=True)
    except Exception:
        pass  # Diagnostics must never prevent namespace shutdown.


def report_command_observation(value):
    """Best-effort bounded event log; abrupt PID1 termination may lose output.

    These fields describe validated app-server notifications, not independently
    verified execution. No command, output or model text enters this logger.
    """
    try:
        record = safe_command_observation(value)
        if record is not None:
            print(json.dumps(dict(phase='CANARY_COMMAND_OBSERVATION', **record)),
                  file=sys.stderr, flush=True)
    except Exception:
        pass


class SupervisorBlocked(ContractError):
    pass


@dataclass(frozen=True)
class Identity:
    pid: int
    start_ticks: int
    uids: tuple[int, int, int, int]
    state: str


@dataclass(frozen=True)
class Reservation:
    boot_id: str
    pid_namespace: str
    launcher_start: int


@dataclass(frozen=True)
class FixedCanaryResult:
    checksum: int
    elapsed_seconds: float
    fixture_only: bool = True
    model_called: bool = False


@dataclass(frozen=True)
class PendingCodexResult:
    candidate: CanaryCandidate
    elapsed_seconds: float
    requires_namespace_restart: bool = field(default=True, init=False)
    cleanup_verified: bool = field(default=False, init=False)


@dataclass
class ChildHandle:
    process: object
    gate_write: int | None
    ready_read: int


class LinuxUIDBackend:
    """Real syscalls. Instantiate only in the separately authorized container."""

    @staticmethod
    def process(pid):
        try:
            path = Path('/proc') / str(pid)
            values = dict(line.split(':', 1) for line in (path / 'status').read_text().splitlines()
                          if ':' in line)
            parts = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            return Identity(pid, int(parts[19]), tuple(map(int, values['Uid'].split())), parts[0])
        except (FileNotFoundError, ProcessLookupError):
            return None
        except (OSError, ValueError, KeyError, IndexError) as exc:
            raise SupervisorBlocked('process identity is unreadable') from exc

    def processes(self):
        result = []
        for path in Path('/proc').iterdir():
            if path.name.isdigit():
                identity = self.process(int(path.name))
                if identity is not None:
                    result.append(identity)
        return result

    @staticmethod
    def context(*, init_namespace=None):
        values = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                      if ':' in line)
        marker = Path('/opt/c3-runtime/dedicated-pid1-canary')
        marker_valid = False
        try:
            info = marker.lstat()
            marker_valid = (stat.S_ISREG(info.st_mode) and info.st_uid == 0
                            and not info.st_mode & 0o022
                            and marker.read_bytes() == b'c3-dedicated-pid1-canary-v1\n')
        except OSError:
            pass
        return {
            'pid': os.getpid(), 'resuid': os.getresuid(),
            'uids': tuple(map(int, values['Uid'].split())),
            'threads': int(values['Threads']), 'nnp': int(values['NoNewPrivs']),
            'seccomp': int(values['Seccomp']),
            'cap_eff': int(values['CapEff'], 16), 'cap_prm': int(values['CapPrm'], 16),
            'cap_bnd': int(values['CapBnd'], 16), 'cap_amb': int(values['CapAmb'], 16),
            'cap_inh': int(values['CapInh'], 16),
            'sigblk': int(values['SigBlk'], 16),
            'sigign': int(values['SigIgn'], 16),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pid_namespace': os.readlink('/proc/self/ns/pid'),
            'init_namespace': os.readlink('/proc/1/ns/pid') if init_namespace is None else init_namespace,
            'nspid': tuple(map(int, values['NSpid'].split())),
            'container_marker': Path('/.dockerenv').is_file(),
            'dedicated_marker': marker_valid,
            'proc_self': os.readlink('/proc/self'),
        }

    @staticmethod
    def resources():
        # Require namespaced cgroup-v2 root. Never write quota or mount settings.
        if Path('/proc/self/cgroup').read_text().strip() != '0::/':
            raise SupervisorBlocked('dedicated namespaced cgroup v2 is required')
        root = Path('/sys/fs/cgroup')
        def finite(name):
            value = (root / name).read_text().strip()
            if value == 'max':
                raise SupervisorBlocked('unbounded outer container resource')
            return int(value)
        cpu = (root / 'cpu.max').read_text().split()
        if len(cpu) != 2 or cpu[0] == 'max':
            raise SupervisorBlocked('finite outer CPU quota is required')
        mounts = {}
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left, right = line.split(' - ', 1)
            fields, fs = left.split(), right.split()
            mounts[fields[4]] = (set(fields[5].split(',')), fs[0], set(fs[2].split(',')))
        root_mount, work_mount, cg_mount = (mounts.get(p) for p in ('/', '/work', '/sys/fs/cgroup'))
        if (not root_mount or 'ro' not in root_mount[0]
                or not work_mount or work_mount[1] != 'tmpfs'
                or not {'nosuid', 'nodev', 'noexec'} <= work_mount[0]
                or not cg_mount or cg_mount[1] != 'cgroup2' or 'ro' not in cg_mount[0]):
            raise SupervisorBlocked('read-only root/cgroup and bounded isolated /work tmpfs required')
        info = Path('/work').lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != WORKER_UID
                or info.st_mode & 0o077):
            raise SupervisorBlocked('private worker tmpfs ownership mismatch')
        if (Path('/var/run/docker.sock').exists() or Path('/run/containerd/containerd.sock').exists()
                or list(Path('/dev').glob('nvidia*')) or Path('/dev/dri').exists()):
            raise SupervisorBlocked('host control socket or GPU device is present')
        vfs = os.statvfs('/work')
        return {'memory': finite('memory.max'), 'swap': finite('memory.swap.max'),
                'pids': finite('pids.max'), 'cpu_quota': int(cpu[0]), 'cpu_period': int(cpu[1]),
                'work_bytes': vfs.f_blocks * vfs.f_frsize}

    pidfd_open = staticmethod(os.pidfd_open)
    close = staticmethod(os.close)
    seteuid = staticmethod(os.seteuid)
    getresuid = staticmethod(os.getresuid)
    signal = staticmethod(signal.pidfd_send_signal)
    now = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    @staticmethod
    def reap():
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    return
            except ChildProcessError:
                return

    @staticmethod
    def exit_own_namespace(code):
        # Never signal PID 1 or another container. Only exit this verified PID 1.
        if os.getpid() != 1 or os.getresuid()[0] != 0 or os.getresuid()[2] != 0:
            raise SupervisorBlocked('cannot establish ownership of namespace shutdown')
        os._exit(code)

    @staticmethod
    def arm_watchdog(seconds):
        if os.getpid() != 1:
            raise SupervisorBlocked('watchdog only belongs to the dedicated PID 1')
        def expire(signum, frame):
            os._exit(124)
        # Explicit default SIGCHLD disposition clears inherited auto-reaping
        # semantics (SA_NOCLDWAIT); Popen must retain the actual child status.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        previous = signal.signal(signal.SIGALRM, expire)
        signal.alarm(math.ceil(seconds))
        return previous

    @staticmethod
    def disarm_watchdog(previous):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    @staticmethod
    def launch_fixed():
        gate_read, gate_write = os.pipe()
        ready_read, ready_write = os.pipe()
        try:
            child = spawn_fixed_child(gate_read=gate_read, ready_write=ready_write)
        except BaseException:
            os.close(gate_write); os.close(ready_read)
            raise
        finally:
            os.close(gate_read); os.close(ready_write)
        return ChildHandle(child, gate_write, ready_read)

    @staticmethod
    def launch_codex(request):
        validate_fixed_codex_request(request)
        # The schema belongs to the pinned read-only image, not the worker.
        path = Path(CODEX_SCHEMA)
        for item in (path, *path.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise SupervisorBlocked('fixed Codex schema path is not protected')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if stream.read(len(request.schema_bytes) + 1) != request.schema_bytes:
                raise SupervisorBlocked('fixed Codex schema content mismatch')
        gate_read, gate_write = os.pipe()
        ready_read, ready_write = os.pipe()
        try:
            child = spawn_fixed_codex_child(request, gate_read=gate_read, ready_write=ready_write)
        except BaseException:
            os.close(gate_write); os.close(ready_read)
            raise
        finally:
            os.close(gate_read); os.close(ready_write)
        return ChildHandle(child, gate_write, ready_read)

    @staticmethod
    def send_prompt(handle, prompt):
        if prompt != validate_fixed_codex_request_from_process(handle.process).stdin:
            raise SupervisorBlocked('unexpected Codex prompt')
        # The fixed prompt is below PIPE_BUF; no arbitrary or unbounded input.
        handle.process.stdin.write(prompt)
        handle.process.stdin.close()

    @staticmethod
    def launch_app_server(request):
        validate_app_server_request(request)
        gate_read, gate_write = os.pipe()
        ready_read, ready_write = os.pipe()
        try:
            child = spawn_fixed_app_server_child(request, gate_read=gate_read, ready_write=ready_write)
        except BaseException:
            os.close(gate_write); os.close(ready_read)
            raise
        finally:
            os.close(gate_read); os.close(ready_write)
        return ChildHandle(child, gate_write, ready_read)

    @staticmethod
    def ready(handle, timeout):
        if not select.select([handle.ready_read], [], [], timeout)[0]:
            return False
        if os.read(handle.ready_read, 1) != b'R':
            raise SupervisorBlocked('worker did not reach its startup barrier')
        return True

    @staticmethod
    def release(handle):
        os.write(handle.gate_write, b'G')
        os.close(handle.gate_write)
        handle.gate_write = None

    @staticmethod
    def poll(handle):
        return handle.process.poll()

    @staticmethod
    def output(handle):
        stdout, stderr = handle.process.communicate(timeout=0.2)
        if len(stdout) > 64 or len(stderr) > 4096:
            raise SupervisorBlocked('fixed canary output exceeds its bounded contract')
        return stdout, stderr

    @staticmethod
    def close_child(handle):
        if handle.gate_write is not None:
            os.close(handle.gate_write)
            handle.gate_write = None
        os.close(handle.ready_read)
        for stream in (handle.process.stdin, handle.process.stdout, handle.process.stderr):
            if stream is not None:
                stream.close()


class OwnedUIDSupervisor:
    """One reservation per fresh container, with real per-UID pidfd cleanup.

    The backend is trusted application code, never an Issue-controlled adapter.
    The Linux backend reads kernel state, not operator-provided capability flags.
    A reservation is in-memory and tied to this instance; it is not a restart
    token. PID-1 death destroys the allocation. Approval/result durability lives
    in protected persistent storage. No normal sandbox, credential, or cost
    proof is implied by reserve(). The fixed Codex candidate path additionally
    requires a trusted deployment admission callback and stays default-disabled.
    """

    def __init__(self, backend, *, controller_pid=None):
        if controller_pid is not None and (type(controller_pid) is not int or controller_pid <= 1):
            raise SupervisorBlocked('controller PID must come from the trusted launcher')
        self.os = backend
        self._controller_pid = controller_pid
        self._controller = None
        self._reservation = None
        self._used = False

    def _context(self, *, initial=False):
        data = self.os.context()
        if (data['pid'] != 1 or data['resuid'] != (0, 0, 0)
                or data['uids'] != (0, 0, 0, 0) or data['threads'] != 1
                or data['proc_self'] != '1' or data['nnp'] != 1 or data['seccomp'] != 2
                or not data['container_marker'] or not data['dedicated_marker']
                or not data['nspid'] or data['nspid'][-1] != 1
                or data['pid_namespace'] != data['init_namespace']
                or any(data[k] != SETUID_SETGID for k in ('cap_eff', 'cap_prm', 'cap_bnd'))
                or data['cap_amb'] != 0 or data['cap_inh'] != 0):
            raise SupervisorBlocked('launcher PID/UID/capability/namespace contract is not satisfied')
        if data['sigblk'] & (1 << (signal.SIGALRM - 1)):
            raise SupervisorBlocked('launcher watchdog signal is blocked')
        if data['sigign'] & (1 << (signal.SIGCHLD - 1)):
            raise SupervisorBlocked('launcher must retain child exit status')
        leader = self.os.process(1)
        if leader is None or leader.uids != (0, 0, 0, 0):
            raise SupervisorBlocked('launcher identity is unavailable')
        current = Reservation(data['boot_id'], data['pid_namespace'], leader.start_ticks)
        if not initial and current != self._reservation:
            raise SupervisorBlocked('launcher or namespace identity changed')
        return current

    def reserve(self) -> Reservation:
        if self._used:
            raise SupervisorBlocked('allocation was already reserved; never relaunch')
        current = self._context(initial=True)
        limits = self.os.resources()
        if (not 0 < limits['memory'] <= MAX_MEMORY or limits['swap'] != 0
                or not 2 <= limits['pids'] <= MAX_PIDS
                or not 0 < limits['cpu_quota'] <= limits['cpu_period']
                or not 0 < limits['work_bytes'] <= MAX_WORK_BYTES):
            raise SupervisorBlocked('outer container resource limits exceed the fixed ceiling')
        # Only a controller created/pinned by this trusted launcher may coexist.
        # No untrusted input selects signal targets or supplies controller PIDs.
        processes = self.os.processes()
        if self._controller_pid is not None:
            self._controller = next((p for p in processes if p.pid == self._controller_pid), None)
            if self._controller is None or self._controller.uids != (CONTROLLER_UID,) * 4:
                raise SupervisorBlocked('trusted controller identity is unavailable')
        if any(item.pid not in (1, self._controller_pid) for item in processes):
            raise SupervisorBlocked('fresh sole-process namespace is required')
        self._reservation, self._used = current, True
        return current

    def _require_reservation(self, reservation):
        if reservation is not self._reservation or reservation is None:
            raise SupervisorBlocked('this instance does not own the reservation')
        self._context()

    def _fail_closed(self):
        # The initial reservation proved PID-1 ownership. Backend rechecks PID
        # and real/saved UID immediately before exiting. It never kills a host.
        self.os.exit_own_namespace(125)
        raise SupervisorBlocked('namespace shutdown did not occur')

    def _signal_owned(self, identity, signum):
        if identity.pid <= 1 or identity.uids != (WORKER_UID,) * 4:
            raise SupervisorBlocked('refusing an unowned signal target')
        try:
            fd = self.os.pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            return
        try:
            current = self.os.process(identity.pid)
            if current is None:
                return
            if (current.pid, current.start_ticks, current.uids) != (
                    identity.pid, identity.start_ticks, identity.uids):
                raise SupervisorBlocked('PID identity changed before signal')
            # A zombie group leader can still have live threads. Never infer
            # group emptiness from the leader's state alone.
            try:
                self.os.seteuid(WORKER_UID)
                if self.os.getresuid() != (0, WORKER_UID, 0):
                    raise SupervisorBlocked('temporary signal identity is incorrect')
                try:
                    self.os.signal(fd, signum)
                except ProcessLookupError:
                    pass
            finally:
                try:
                    self.os.seteuid(0)
                    if self.os.getresuid() != (0, 0, 0):
                        raise SupervisorBlocked('root identity was not restored')
                except BaseException:
                    self._fail_closed()
        finally:
            self.os.close(fd)

    def _workers(self):
        result = []
        for item in self.os.processes():
            if item.pid == 1:
                continue
            if item.pid == self._controller_pid:
                if (item.start_ticks, item.uids) != (self._controller.start_ticks, self._controller.uids):
                    raise SupervisorBlocked('controller PID identity changed')
                continue
            result.append(item)
        return result

    def stop_owned(self, reservation: Reservation, *, grace_seconds=0.2,
                   kill_seconds=2.0):
        """Stop escaped-session descendants too; uncertainty exits own PID 1.

        Caller must stop admitting new work first. Live workers may fork, so scan
        and signal repeatedly, never just the original process group. A busy
        fork race or unreadable process table cannot become cleanup success.
        """
        if reservation is not self._reservation or reservation is None:
            raise SupervisorBlocked('this instance does not own the reservation')
        if (type(grace_seconds) not in (int, float) or not 0 <= grace_seconds <= 2
                or type(kill_seconds) not in (int, float) or not 0 < kill_seconds <= 5):
            raise SupervisorBlocked('invalid cleanup deadlines')
        try:
            self._context()
            for signum, duration in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, kill_seconds)):
                deadline = self.os.now() + duration
                while True:
                    self._context()
                    self.os.reap()
                    members = self._workers()
                    if not members:
                        # A fresh second scan catches children appearing during
                        # the first /proc enumeration. External allocation owner
                        # must still verify container exit in deployment tests.
                        self.os.reap()
                        if not self._workers():
                            return {'cleanup': 'owned_uid_empty_observed',
                                    'allocation_empty_verified': False}
                        continue
                    if any(p.uids != (WORKER_UID,) * 4 for p in members):
                        raise SupervisorBlocked('unexpected peer or changed worker identity')
                    for member in members:
                        self._signal_owned(member, signum)
                    if self.os.now() >= deadline:
                        break
                    self.os.sleep(0.01)
        except BaseException:
            self._fail_closed()
        self._fail_closed()

    def finish_allocation(self, reservation):
        """Exit this dedicated namespace even after an apparently empty scan.

        /proc scans are not atomic against adversarial fork/exit races. Therefore
        arbitrary-worker cleanup becomes authoritative only when the external
        allocation owner verifies this entire container has exited. A long-lived
        manager must live outside that per-attempt disposable boundary.
        """
        self.stop_owned(reservation)
        self.os.exit_own_namespace(0)
        raise SupervisorBlocked('namespace completion did not occur')

    def run_fixed_canary(self, recheck, *, before_release=None, wall_seconds=5.0):
        """Execute/validate only the fixed CPU subprocess, with revocation checks.

        This is concrete offline lifecycle code, not a model-runtime substitute.
        The callbacks are protected controller code, never worker-supplied. A
        callback raises on cancellation/expired approval. before_release receives
        a pinned process identity to durably journal before the G barrier opens.
        The parent binds the returned checksum/time to the approved plan/run.
        """
        if (not callable(recheck) or (before_release is not None and not callable(before_release))
                or type(wall_seconds) not in (int, float) or not 0 < wall_seconds <= 120):
            raise SupervisorBlocked('invalid fixed canary controller contract')
        reservation = self.reserve()
        started = self.os.now()
        deadline = started + wall_seconds
        watchdog = self.os.arm_watchdog(wall_seconds + 4)
        handle = None
        try:
            recheck()
            handle = self.os.launch_fixed()
            while not self.os.ready(handle, min(0.05, max(0, deadline - self.os.now()))):
                recheck()
                if self.os.now() >= deadline:
                    raise SupervisorBlocked('fixed canary startup deadline exceeded')
            identity = self.os.process(handle.process.pid)
            if identity is None or identity.uids != (WORKER_UID,) * 4:
                raise SupervisorBlocked('fixed worker startup identity is invalid')
            if before_release is not None:
                before_release(identity)
            recheck()
            if self.os.now() >= deadline:
                raise SupervisorBlocked('fixed canary admission deadline exceeded')
            self.os.release(handle)
            while self.os.poll(handle) is None:
                recheck()
                if self.os.now() >= deadline:
                    raise SupervisorBlocked('fixed canary execution deadline exceeded')
                self.os.sleep(0.01)
            code = self.os.poll(handle)
            stdout, stderr = self.os.output(handle)
            self.stop_owned(reservation)
            recheck()
            # Protected code recomputes; no model claim is accepted as evidence.
            expected = sum(i * i for i in range(10000))
            elapsed = self.os.now() - started
            if code != 0 or stderr or stdout != f'{expected}\n'.encode() or elapsed > wall_seconds:
                raise SupervisorBlocked('fixed CPU canary did not validate')
            return FixedCanaryResult(expected, elapsed)
        except BaseException:
            self.stop_owned(reservation)
            raise
        finally:
            if handle is not None:
                self.os.close_child(handle)
            self.os.disarm_watchdog(watchdog)

    def run_fixed_codex_canary(self, request, recheck, *, enabled=False, admit=None,
                              before_release=None):
        """One real-CLI candidate, pending whole-namespace restart; disabled by default.

        This is NOT ValidatedSupervisor.admit_and_execute and cannot produce a
        cleanup-verified result. The separately admitted deployment must guarantee
        exclusive-container restart and persist RESULT_PENDING_REAP before PID1
        exit. It must not publish in this generation or reuse this supervisor.
        The parent PID1 alarm stays armed even when this method returns.
        Credentials are provisioned separately in the worker-only home; none are
        received from the manager or inherited here. All live deployment gates
        (normal sandbox, auth isolation, lifecycle, billing approval) remain due.
        admit(request) is protected deployment code, NOT Issue input or a bool:
        it must check configured immutable image/CLI version, actual current
        normal-sandbox positive/negative probes, exclusive restart lifecycle,
        worker-only auth, and this run's billing-cap/risk approval. It raises on
        any missing gate and returns None only after admission. No default
        production admission implementation or fabricated evidence is supplied.
        """
        if enabled is not True:
            raise SupervisorBlocked('LIVE_CODEX_DISABLED')
        validate_fixed_codex_request(request)
        if not callable(admit):
            raise SupervisorBlocked('CODEX_DEPLOYMENT_ADMISSION_NOT_CONFIGURED')
        if not callable(recheck) or (before_release is not None and not callable(before_release)):
            raise SupervisorBlocked('trusted approval callbacks are required')
        recheck()
        if admit(request) is not None:
            raise SupervisorBlocked('deployment admission must verify gates, not return a flag')
        recheck()
        reservation = self.reserve()
        started = self.os.now()
        deadline = started + request.wall_seconds
        self.os.arm_watchdog(request.wall_seconds + 4)
        handle = None
        try:
            recheck()
            handle = self.os.launch_codex(request)
            while not self.os.ready(handle, min(0.05, max(0, deadline - self.os.now()))):
                recheck()
                if self.os.now() >= deadline:
                    raise SupervisorBlocked('Codex startup deadline exceeded')
            identity = self.os.process(handle.process.pid)
            if identity is None or identity.uids != (WORKER_UID,) * 4:
                raise SupervisorBlocked('Codex startup identity is invalid')
            if before_release is not None:
                before_release(identity)
            recheck()
            if self.os.now() >= deadline:
                raise SupervisorBlocked('Codex admission deadline exceeded')
            self.os.release(handle)
            self.os.send_prompt(handle, request.stdin)
            result = drain_process(handle.process, recheck=recheck,
                                   stop_owned=lambda: self.stop_owned(reservation),
                                   deadline=deadline, max_bytes=request.max_capture_bytes,
                                   clock=self.os.now)
            recheck()
            if self.os.now() >= deadline:
                raise SupervisorBlocked('Codex collection and cleanup deadline exceeded')
            candidate = parse_candidate(result)
            recheck()
            elapsed = self.os.now() - started
            if not 0 <= elapsed < request.wall_seconds:
                raise SupervisorBlocked('Codex candidate validation deadline exceeded')
            return PendingCodexResult(candidate, elapsed)
        except BaseException:
            self.stop_owned(reservation)
            raise
        finally:
            if handle is not None:
                self.os.close_child(handle)
            # Never disarm: controller must persist pending output and exit PID1.
            # Even failure must cross a namespace generation before another run.

    def run_fixed_app_server_canary(self, request, recheck, *, enabled=False,
                                    admit_external=None, before_release=None):
        """Explicit externally-sandboxed single turn, always restart-pending.

        This is a distinct opt-in, never fallback from failed normal sandboxing.
        admit_external is protected code verifying this approved external
        filesystem/network boundary, pinned CLI/config, worker-only auth, cost
        approval, resources, hard lifetime and exclusive namespace restart.
        The explicit enabled network policy supplies no firewall. Callbacks are not
        Issue/worker inputs. This path never converts a candidate into success.
        """
        stage = 'admission'
        try:
            if enabled is not True:
                raise SupervisorBlocked('APP_SERVER_DISABLED')
            validate_app_server_request(request)
            model_seconds = request.wall_seconds - APP_SERVER_FINALIZATION_RESERVE_SECONDS
            if model_seconds <= 0:
                raise SupervisorBlocked('app-server finalization reserve exceeds request budget')
            if (not callable(admit_external) or not callable(recheck)
                    or (before_release is not None and not callable(before_release))):
                raise SupervisorBlocked('EXTERNAL_BOUNDARY_ADMISSION_NOT_CONFIGURED')
            stage = 'admission'
            adapter = AppServerAdapter(enabled=True)
            adapter.admit(request, admit_external=admit_external, recheck=recheck)
            stage = 'reserve'
            reservation = self.reserve()
            started = self.os.now()
            deadline = started + request.wall_seconds
            # Finish model work early enough for cleanup, fresh approval and
            # durable controller finalization under the unchanged hard alarm.
            model_deadline = deadline - APP_SERVER_FINALIZATION_RESERVE_SECONDS
            stage = 'watchdog'
            self.os.arm_watchdog(request.wall_seconds + 4)
        except BaseException as exc:
            report_failure('app_server', stage, exc)
            raise
        handle = transport = None
        cleanup_started = False
        try:
            stage = 'approval'
            recheck()
            stage = 'spawn'
            handle = self.os.launch_app_server(request)
            stage = 'barrier'
            while not self.os.ready(handle, min(0.05, max(0, model_deadline - self.os.now()))):
                recheck()
                if self.os.now() >= model_deadline:
                    raise SupervisorBlocked('app-server startup deadline exceeded')
            stage = 'identity'
            identity = self.os.process(handle.process.pid)
            if identity is None or identity.uids != (WORKER_UID,) * 4:
                raise SupervisorBlocked('app-server startup identity is invalid')
            if before_release is not None:
                before_release(identity)
            recheck()
            if self.os.now() >= model_deadline:
                raise SupervisorBlocked('app-server admission deadline exceeded')
            stage = 'release'
            self.os.release(handle)
            stage = 'transport'
            transport = BoundedStdioTransport(handle.process, recheck=recheck, deadline=model_deadline,
                clock=self.os.now, max_frame_bytes=request.max_frame_bytes,
                max_capture_bytes=request.max_capture_bytes)
            stage = 'turn'
            result = adapter.run(request, transport, recheck=recheck,
                                 command_observer=report_command_observation)
            if self.os.now() >= model_deadline:
                raise SupervisorBlocked('app-server model deadline exceeded')
            stage = 'cleanup'
            cleanup_started = True
            self.stop_owned(reservation)
            stage = 'postcheck'
            recheck()
            elapsed = self.os.now() - started
            if not 0 <= elapsed < request.wall_seconds:
                raise SupervisorBlocked('app-server cleanup deadline exceeded')
            return PendingAppServerResult(result.candidate, result.thread_id, result.turn_id,
                result.observed_command, result.command_exit_code, result.command_checksum, elapsed)
        except BaseException as exc:
            # Observe before cleanup; a later supervisor-induced exit is not
            # evidence that the native process originally failed that way.
            returncode = None
            if handle is not None and not cleanup_started:
                try:
                    returncode = handle.process.poll()
                except Exception:
                    pass
            report_failure('app_server', stage, exc, child_returncode=returncode)
            self.stop_owned(reservation)
            raise
        finally:
            if transport is not None:
                transport.close()
            if handle is not None:
                self.os.close_child(handle)
            # Remain armed. Persist pending state and exit this dedicated PID1.


def apply_fixed_child_limits():
    """Child-only helper, after fork and before release/exec; never call on host.

    The parent must already hold a reservation and enforce the wall deadline.
    Per-process/per-file RLIMITs supplement outer aggregate cgroup/tmpfs bounds.
    They neither enforce aggregate model spend nor prove descendant cleanup.
    """
    if os.getpid() == 1 or os.getresuid() != (WORKER_UID,) * 3:
        raise SupervisorBlocked('limits are only for the already-dropped worker child')
    limits = ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_CPU, 10),
              (resource.RLIMIT_AS, 512 * 1024 * 1024),
              (resource.RLIMIT_FSIZE, 1024 * 1024),
              (resource.RLIMIT_NPROC, 32), (resource.RLIMIT_NOFILE, 64))
    for which, ceiling in limits:
        resource.setrlimit(which, (ceiling, ceiling))


def spawn_fixed_child(*, gate_read: int, ready_write: int):
    """Fixed diagnostic child launcher, not a Codex/model or arbitrary command.

    Only the authorized PID-1 supervisor may call this after reserve(). FDs are
    private pipes. The controller journals ownership/approval before writing G.
    The child receives no ambient credentials, groups, or user Python startup.
    """
    if os.getpid() != 1 or os.getresuid() != (0, 0, 0):
        raise SupervisorBlocked('dedicated root PID-1 launcher required')
    if type(gate_read) is not int or type(ready_write) is not int or min(gate_read, ready_write) < 3:
        raise SupervisorBlocked('private startup pipe descriptors required')
    code = """
import os, resource, sys
uid = 10002
if os.getresuid() != (uid,uid,uid) or os.getresgid() != (uid,uid,uid) or os.getgroups():
    raise SystemExit(125)
os.chdir('/work')
for kind, n in ((resource.RLIMIT_CORE,0),(resource.RLIMIT_CPU,10),
                (resource.RLIMIT_AS,536870912),(resource.RLIMIT_FSIZE,1048576),
                (resource.RLIMIT_NPROC,32),(resource.RLIMIT_NOFILE,64)):
    resource.setrlimit(kind,(n,n))
gate, ready = int(sys.argv[1]), int(sys.argv[2])
os.write(ready,b'R'); os.close(ready)
if os.read(gate,1) != b'G':
    raise SystemExit(125)
os.close(gate)
print(sum(i*i for i in range(10000)), flush=True)
"""
    return subprocess.Popen(
        [sys.executable, '-I', '-S', '-c', code, str(gate_read), str(ready_write)],
        # CPython chdir precedes its user/group drop. Root with only SETUID/GID
        # cannot traverse private UID10002 /work; the child chdirs after drop.
        user=WORKER_UID, group=WORKER_UID, extra_groups=[], env={}, cwd='/',
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, pass_fds=(gate_read, ready_write), start_new_session=True,
    )


def validate_fixed_codex_request(request):
    if type(request) is not CanaryRequest:
        raise SupervisorBlocked('only a canonical CPU-canary request is accepted')
    try:
        model = request.argv[request.argv.index('--model') + 1]
        adapter = CodexAdapter(CODEX_BINARY)
        expected = adapter.build_canary_request(
            inspection=CliInspection('0.160.0', '0.160.0', True, ()),
            workspace=CODEX_WORKSPACE, schema_path=CODEX_SCHEMA, model=model)
    except (ValueError, IndexError, TypeError, ContractError) as exc:
        raise SupervisorBlocked('invalid fixed Codex request') from exc
    if request != expected or len(request.stdin) > 4096:
        raise SupervisorBlocked('Codex request differs from the fixed canary contract')
    return request


def validate_fixed_codex_request_from_process(process):
    # Set only by the trusted fixed launcher; never parsed from stdout/Issue JSON.
    request = getattr(process, '_c3_fixed_request', None)
    return validate_fixed_codex_request(request)


def spawn_fixed_codex_child(request, *, gate_read, ready_write):
    """Fixed executable/argv/environment; no general arbitrary-command API."""
    validate_fixed_codex_request(request)
    return _spawn_fixed_model_child(request, gate_read=gate_read, ready_write=ready_write)


def spawn_fixed_app_server_child(request, *, gate_read, ready_write):
    """Only the canonical explicit-external-sandbox app-server request."""
    validate_app_server_request(request)
    return _spawn_fixed_model_child(request, gate_read=gate_read, ready_write=ready_write)


def _spawn_fixed_model_child(request, *, gate_read, ready_write):
    # Internal shared bootstrap; no path/argv supplied by an Issue is admitted.
    if type(request) is CanaryRequest:
        validate_fixed_codex_request(request)
    elif type(request) is AppServerRequest:
        validate_app_server_request(request)
    else:
        raise SupervisorBlocked('unsupported fixed model child')
    if os.getpid() != 1 or os.getresuid() != (0, 0, 0):
        raise SupervisorBlocked('dedicated root PID-1 launcher required')
    for fd in (gate_read, ready_write):
        if type(fd) is not int or fd < 3 or not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise SupervisorBlocked('private startup pipe descriptors required')
    code = r'''
import os, resource, stat, sys
uid = 10002
if os.getresuid() != (uid,uid,uid) or os.getresgid() != (uid,uid,uid) or os.getgroups():
    raise SystemExit(125)
values = dict(line.split(':',1) for line in open('/proc/self/status') if ':' in line)
if any(int(values[k].strip(),16) for k in ('CapEff','CapPrm','CapAmb','CapInh')) or values['NoNewPrivs'].strip() != '1':
    raise SystemExit(125)
for path in ('/work', '/work/canary', '/work/worker-home', '/work/codex-home'):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise SystemExit(125)
workspace = os.lstat('/work/canary')
os.chdir('/work/canary')
opened = os.stat('.')
if (workspace.st_dev,workspace.st_ino) != (opened.st_dev,opened.st_ino):
    raise SystemExit(125)
if set(os.listdir('.')) != {'.git'} or not os.path.isdir('.git') or os.path.islink('.git'):
    raise SystemExit(125)
# Empty, known Git metadata only. Git config/hooks must never become a
# pre-sandbox command channel (for example core.fsmonitor or config includes).
directories = {'.git': {'HEAD','config','objects','refs'},
               '.git/objects': set(), '.git/refs': {'heads','tags'},
               '.git/refs/heads': set(), '.git/refs/tags': set()}
for path, names in directories.items():
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077 or set(os.listdir(path)) != names:
        raise SystemExit(125)
files = {'.git/HEAD': b'ref: refs/heads/main\n',
         '.git/config': b'[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n'}
for path, expected in files.items():
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077 or info.st_nlink != 1:
        raise SystemExit(125)
    with open(path,'rb') as stream:
        if stream.read(len(expected)+1) != expected: raise SystemExit(125)
if os.listdir('/work/worker-home'):
    raise SystemExit(125)
if sys.argv[4] == 'app-server':
    # Do not read secret bytes. Only user-provisioned file auth is accepted at
    # startup; no inherited config, skills, hooks, plugins, MCP or old state.
    if set(os.listdir('/work/codex-home')) != {'auth.json'}:
        raise SystemExit(125)
    auth = os.lstat('/work/codex-home/auth.json')
    if (not stat.S_ISREG(auth.st_mode) or auth.st_uid != uid or stat.S_IMODE(auth.st_mode) != 0o600
            or auth.st_nlink != 1 or not 0 < auth.st_size <= 65536):
        raise SystemExit(125)
if any(os.path.lexists(path) for path in ('/.codex','/work/.codex','/work/canary/.codex')):
    raise SystemExit(125)
# The verified container cgroup enforces aggregate resident memory (1 GiB).
# Codex reserves more virtual address space than its resident memory; imposing
# the CPU-fixture AS512MiB limit prevents SQLite initialization. Its own SQLite
# WAL also exceeds1MiB. Keep captured output bounded separately in transport.
for kind, n in ((resource.RLIMIT_CORE,0),(resource.RLIMIT_CPU,10),
                (resource.RLIMIT_FSIZE,16777216),
                (resource.RLIMIT_NPROC,64),(resource.RLIMIT_NOFILE,64)):
    resource.setrlimit(kind,(n,n))
gate, ready = int(sys.argv[1]), int(sys.argv[2])
os.write(ready,b'R'); os.close(ready)
if os.read(gate,1) != b'G': raise SystemExit(125)
os.close(gate)
env = {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8',
       'HOME':'/work/worker-home','CODEX_HOME':'/work/codex-home',
       'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'}
os.execve('/opt/codex/bin/codex',sys.argv[3:],env)
'''
    child = subprocess.Popen(
        [sys.executable, '-I', '-S', '-c', code, str(gate_read), str(ready_write), *request.argv],
        user=WORKER_UID, group=WORKER_UID, extra_groups=[], env={}, cwd='/',
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, pass_fds=(gate_read, ready_write), start_new_session=True,
    )
    child._c3_fixed_request = request
    return child


def drain_process(process, *, recheck, stop_owned, deadline,
                  max_bytes=1024 * 1024, clock=time.monotonic):
    """Bound combined stdout/stderr while checking deadline and cancellation.

    Generic trusted-supervisor plumbing, not an arbitrary process launcher.
    stop_owned is invoked on both success and failure before this returns. Its
    result does NOT establish whole-allocation cleanup. For arbitrary workers,
    the caller must additionally cross a verified namespace/container restart
    boundary before publishing or admitting another run.
    The bounded returned bytes are still untrusted. No log text is interpolated
    into exceptions, and no credential environment is inspected or copied.
    """
    if (not callable(recheck) or not callable(stop_owned)
            or type(max_bytes) is not int or not 0 < max_bytes <= 1024 * 1024
            or type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise SupervisorBlocked('invalid output collector contract')
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    total = 0
    try:
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                stream = getattr(process, name, None)
                if stream is None:
                    raise SupervisorBlocked('separate binary stdout/stderr pipes are required')
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                recheck()
                remaining = deadline - clock()
                if remaining <= 0:
                    raise SupervisorBlocked('worker output deadline exceeded')
                for key, _ in selector.select(min(0.05, remaining)):
                    try:
                        data = os.read(key.fileobj.fileno(), min(65536, max_bytes - total + 1))
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > max_bytes:
                        raise SupervisorBlocked('combined worker output exceeds its limit')
                    buffers[key.data].extend(data)
            recheck()
            return subprocess.CompletedProcess(process.args, process.poll(),
                                               bytes(buffers['stdout']), bytes(buffers['stderr']))
    finally:
        # Exceptions from cleanup must prevent an apparent successful result.
        stop_owned()
