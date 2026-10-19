"""Opt-in, direct-PR-only reusable allocation inside one dedicated container.

The old disposable path is unchanged. This path retains the PID1/UID/capability
contract and pidfd-based worker cleanup, while admitting a separately isolated
runner UID. A fresh supervisor is created per job. Failed cleanup durably locks
the service before the final namespace shutdown; it never starts another job.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import signal
import stat
import time

from .schema import ContractError
from .goal_runtime import GoalLinuxUIDBackend, GoalUIDSupervisor
from .restart_boundary import RestartEpoch
from .uid_supervisor import (WORKER_UID, CONTROLLER_UID, MAX_MEMORY, MAX_PIDS,
                            MAX_WORK_BYTES, SupervisorBlocked, Identity, Reservation, SETUID_SETGID,
                            ChildHandle)

RUNNER_UID = 10003
BLOCK_MARKER = '/control/resident-blocked'


class ResidentBlocked(ContractError):
    CODES = frozenset(('TRUSTED_ROLE_EXITED','JOB_DEADLINE','JOB_EXITED_UNCLEAN',
                       'WORKER_REMAINED','WORKSPACE_REMAINED','IPC_CONTRACT_REJECTED',
                       'CLEANUP_UNVERIFIED','STARTUP_REJECTED'))

    def __init__(self, code, *, child_exit=None):
        self.code = code if type(code) is str and code in self.CODES else 'CLEANUP_UNVERIFIED'
        self.child_exit = child_exit if type(child_exit) is int and -128 <= child_exit <= 255 else None
        super().__init__(self.code)


def subreaper_enabled():
    value = ctypes.c_int()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(37, ctypes.byref(value), 0, 0, 0) != 0:  # PR_GET_CHILD_SUBREAPER
        raise SupervisorBlocked('subreaper status unavailable')
    return value.value == 1


def install_subreaper():
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise SupervisorBlocked('subreaper setup failed')
    if not subreaper_enabled():
        raise SupervisorBlocked('subreaper setup not observed')


def require_job_supervisor():
    if os.getpid() <= 1 or os.getppid() != 1 or os.getresuid() != (0, 0, 0) or not subreaper_enabled():
        raise SupervisorBlocked('dedicated per-job root subreaper required')


def reap_children():
    """ECHILD proves no descendants of this isolated subreaper remain.

    Unlike the container PID1, this job process has NO runner/manager children.
    Escaped sessions/double-fork descendants are reparented here. A /proc scan
    alone or an original process-group exit is never this proof.
    """
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0: return False
        except ChildProcessError:
            return True


def persist_block():
    """A fixed latch, no secret/context/exception serialization."""
    try:
        os.seteuid(0)
        os.seteuid(CONTROLLER_UID)
        try:
            fd = os.open(BLOCK_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return
        with os.fdopen(fd, 'wb') as stream:
            stream.write(b'CLEANUP_UNVERIFIED\n'); stream.flush(); os.fsync(stream.fileno())
        fd = os.open('/control', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        os.seteuid(0)


class ResidentLinuxBackend(GoalLinuxUIDBackend):
    block_service = staticmethod(persist_block)

    def __init__(self,*,broker_epoch,broker_ipc_namespace):
        super().__init__()
        if (type(broker_epoch) is not RestartEpoch or type(broker_ipc_namespace) is not str
                or re.fullmatch(r'ipc:\[[1-9][0-9]{0,19}\]',broker_ipc_namespace) is None
                or int(broker_ipc_namespace[5:-1])>2**64-1):
            raise SupervisorBlocked('trusted PID1 namespace anchor required')
        # Only PID1's private bootstrap pipe supplies these observations. Never
        # read them from a plan, event, worker or mutable operator JSON.
        self.broker_epoch=broker_epoch
        self.broker_ipc_namespace=broker_ipc_namespace

    def init_namespace(self,kind):
        if kind=='pid':return 'pid:[%d]'%self.broker_epoch.pid_namespace_inode
        if kind=='ipc':return self.broker_ipc_namespace
        raise SupervisorBlocked('unsupported broker namespace')

    @staticmethod
    def verify_deployment(*args, **kwargs):
        from .resident_deployment import validate_resident_deployment
        return validate_resident_deployment(*args, **kwargs)

    def reap(self):
        self.children_empty = reap_children()

    def context(self):
        value = super().context()
        parent=self.process(1)
        if (value['pid_namespace']!=self.init_namespace('pid')
                or value['boot_id']!=self.broker_epoch.boot_id or parent is None
                or parent.uids!=(0,)*4 or parent.start_ticks!=self.broker_epoch.pid1_start_ticks):
            raise SupervisorBlocked('trusted PID1 epoch changed')
        value['subreaper'] = subreaper_enabled()
        value['parent_pid'] = os.getppid()
        return value

    @staticmethod
    def exit_own_namespace(code):
        # Only exit the owned job process. PID1 observes this failed job and
        # performs the last-resort container shutdown; normal jobs never do so.
        require_job_supervisor()
        os._exit(code)

    @staticmethod
    def launch_goal(request):
        from .goal_runtime import spawn_goal_child
        gate_read, gate_write = os.pipe()
        ready_read, ready_write = os.pipe()
        try:
            child = spawn_goal_child(request, gate_read=gate_read, ready_write=ready_write,
                                     resident_supervisor=True)
        except BaseException:
            os.close(gate_write); os.close(ready_read); raise
        finally:
            os.close(gate_read); os.close(ready_write)
        return ChildHandle(child, gate_write, ready_read)

    @staticmethod
    def arm_watchdog(seconds):
        require_job_supervisor()
        def expire(*_):
            try: persist_block()
            finally: os._exit(124)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        previous = signal.signal(signal.SIGALRM, expire)
        signal.alarm(int(seconds))
        return previous


class ResidentGoalSupervisor(GoalUIDSupervisor):
    """Fixed trusted roles coexist; only UID10002 is ever signalled."""
    def __init__(self, backend, *, controller_pid, runner_pid):
        super().__init__(backend, controller_pid=controller_pid)
        self.runner_pid = runner_pid
        self.runner = None

    def _context(self, *, initial=False):
        data = self.os.context()
        if (data['pid'] <= 1 or data['parent_pid'] != 1 or not data['subreaper']
                or data['resuid'] != (0, 0, 0) or data['uids'] != (0,) * 4
                or data['threads'] != 1 or data['proc_self'] != str(data['pid'])
                or data['nnp'] != 1 or data['seccomp'] != 2
                or not data['container_marker'] or not data['dedicated_marker']
                or not data['nspid'] or data['nspid'][-1] != data['pid']
                or data['pid_namespace'] != data['init_namespace']
                or any(data[k] != SETUID_SETGID for k in ('cap_eff', 'cap_prm', 'cap_bnd'))
                or data['cap_amb'] != 0 or data['cap_inh'] != 0
                or data['sigblk'] & (1 << (signal.SIGALRM - 1))
                or data['sigign'] & (1 << (signal.SIGCHLD - 1))):
            raise SupervisorBlocked('resident job context rejected')
        leader = self.os.process(data['pid'])
        parent = self.os.process(1)
        if leader is None or leader.uids != (0,) * 4 or parent is None or parent.uids != (0,) * 4:
            raise SupervisorBlocked('resident job/parent identity unavailable')
        current = Reservation(data['boot_id'], data['pid_namespace'], leader.start_ticks)
        if not initial and current != self._reservation:
            raise SupervisorBlocked('resident job identity changed')
        return current

    def _trusted(self, processes):
        manager = next((p for p in processes if p.pid == self._controller_pid), None)
        runner = next((p for p in processes if p.pid == self.runner_pid), None)
        if manager is None or manager.uids != (CONTROLLER_UID,) * 4:
            raise SupervisorBlocked('resident manager identity unavailable')
        if runner is None or runner.uids != (RUNNER_UID,) * 4:
            raise SupervisorBlocked('resident runner identity unavailable')
        if self._controller is not None and manager != self._controller:
            # Process state may change between R/S; compare stable identity only.
            if (manager.pid, manager.start_ticks, manager.uids) != (self._controller.pid, self._controller.start_ticks, self._controller.uids):
                raise SupervisorBlocked('resident manager identity changed')
        if self.runner is not None and (runner.pid, runner.start_ticks, runner.uids) != (self.runner.pid, self.runner.start_ticks, self.runner.uids):
            raise SupervisorBlocked('resident runner identity changed')
        return manager, runner

    def reserve(self):
        if self._used:
            raise SupervisorBlocked('resident job was already reserved')
        current = self._context(initial=True)
        limits = self.os.resources()
        if (not 0 < limits['memory'] <= MAX_MEMORY or limits['swap'] != 0
                or not 2 <= limits['pids'] <= MAX_PIDS
                or not 0 < limits['cpu_quota'] <= limits['cpu_period']
                or not 0 < limits['work_bytes'] <= MAX_WORK_BYTES):
            raise SupervisorBlocked('resident outer limits rejected')
        processes = self.os.processes()
        self._controller, self.runner = self._trusted(processes)
        if any(p.pid not in (1, self.os.context()['pid'], self._controller_pid)
               and p.uids != (RUNNER_UID,) * 4 for p in processes):
            raise SupervisorBlocked('resident job requires an empty worker UID')
        self._reservation, self._used = current, True
        return current

    def _workers(self):
        processes = self.os.processes()
        self._trusted(processes)
        peers = [p for p in processes if p.pid not in (1, self.os.context()['pid'], self._controller_pid)
                 and p.uids != (RUNNER_UID,) * 4]
        if any(p.uids != (WORKER_UID,) * 4 for p in peers):
            raise SupervisorBlocked('resident unexpected process identity')
        return peers

    def _fail_closed(self):
        try: self.os.block_service()
        finally: super()._fail_closed()

    def stop_owned(self, *args, **kwargs):
        result = super().stop_owned(*args, **kwargs)
        self.os.reap()
        if not self.os.children_empty:
            self._fail_closed()
        return result


def erase_tree_contents(root, *, switch_uid, root_uid=0, worker_uid=WORKER_UID,
                        maximum_entries=65536, seconds=5, clock=time.monotonic):
    """FD-relative deletion, never follow a worker's link or execute a file.

    Call only after the worker UID is proved empty. Root has no DAC override or
    CHOWN: enter each directory as its existing fixed owner, restore root on
    every path, and refuse foreign owners/mounts. Test arguments are trusted
    local constructions, never plan/receipt fields.
    """
    deadline = clock() + seconds
    remaining = maximum_entries
    def budget():
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or clock() >= deadline:
            raise SupervisorBlocked('resident erase deadline or entry limit')
    def visit(fd, owner, device):
        switch_uid(owner)
        for name in os.listdir(fd):
            budget(); switch_uid(owner)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid not in (root_uid, worker_uid) or info.st_dev != device:
                raise SupervisorBlocked('resident foreign file or mount')
            if stat.S_ISDIR(info.st_mode):
                switch_uid(info.st_uid)
                # No worker is alive to exchange this lstat-checked directory.
                # Keep root-owned input ancestors searchable when entering the
                # existing worker UID; no recursive chown/DAC capability.
                os.chmod(name, 0o755 if info.st_uid == root_uid else 0o700, dir_fd=fd)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise SupervisorBlocked('resident erase identity changed')
                    visit(child, info.st_uid, device)
                finally: os.close(child)
                switch_uid(owner); os.rmdir(name, dir_fd=fd)
            else:
                # Includes symlinks, sockets and FIFOs: unlink the name only.
                os.unlink(name, dir_fd=fd)
        if os.listdir(fd):
            raise SupervisorBlocked('resident directory not empty')
    fd = None
    try:
        switch_uid(root_uid)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != root_uid:
            raise SupervisorBlocked('resident work root owner mismatch')
        visit(fd, root_uid, info.st_dev)
    finally:
        switch_uid(root_uid)
        if fd is not None: os.close(fd)


def clear_worker_ipc(*, worker_uid=WORKER_UID):
    """Private container IPC only, invoked by the verified job subreaper."""
    require_job_supervisor()
    def owned_files(directory):
        path = Path(directory)
        if not path.is_dir(): return []
        return [p for p in path.iterdir() if p.lstat().st_uid == worker_uid]
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        os.seteuid(worker_uid)
        for directory in ('/dev/shm', '/dev/mqueue'):
            for path in owned_files(directory):
                if path.is_dir() and not path.is_symlink():
                    # Unexpected nested IPC objects are not declared clean.
                    raise SupervisorBlocked('resident IPC directory remains')
                path.unlink()
        for name, id_key, call in (('shm', 'shmid', libc.shmctl), ('sem', 'semid', libc.semctl), ('msg', 'msqid', libc.msgctl)):
            lines = Path('/proc/sysvipc', name).read_text().splitlines()
            if not lines: raise SupervisorBlocked('resident IPC table unreadable')
            header = lines[0].split()
            for line in lines[1:]:
                row = dict(zip(header, line.split()))
                if int(row['uid']) == worker_uid:
                    args = (int(row[id_key]), 0, 0) if name == 'sem' else (int(row[id_key]), 0, ctypes.c_void_p())
                    if call(*args) != 0:  # IPC_RMID; semctl's command is its third argument.
                        raise SupervisorBlocked('resident IPC removal failed')
            again = Path('/proc/sysvipc', name).read_text().splitlines()
            if any(int(dict(zip(header, line.split()))['uid']) == worker_uid for line in again[1:]):
                raise SupervisorBlocked('resident IPC remains')
        if any(owned_files(p) for p in ('/dev/shm', '/dev/mqueue')):
            raise SupervisorBlocked('resident named IPC remains')
    finally: os.seteuid(0)


def cleanup_job(supervisor):
    """Called before success/failure ACK permits another job."""
    if supervisor._reservation is None:
        # Cancellation/admission failure can precede run_goal's reservation.
        # Prove the worker UID empty AND subreaper ECHILD on that path too.
        supervisor.reserve()
    supervisor.stop_owned(supervisor._reservation)
    if any(p.uids == (WORKER_UID,) * 4 for p in supervisor.os.processes()):
        raise SupervisorBlocked('resident workers remain before erase')
    # Recheck the read-only mount/IPC/resource contract before traversing.
    supervisor.os.resources()
    erase_tree_contents('/work', switch_uid=lambda uid: os.seteuid(uid))
    clear_worker_ipc()
    if any(p.uids == (WORKER_UID,) * 4 for p in supervisor.os.processes()):
        raise SupervisorBlocked('resident worker reappeared')
    if os.listdir('/work'):
        raise SupervisorBlocked('resident temporary data remains')
