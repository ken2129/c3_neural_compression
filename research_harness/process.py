"""Linux-only ownership checks for a fixed, benign CPU test fixture.

This is an MVP0 process-lifecycle fixture, NOT a sandbox or security boundary.
In particular, a same-UID peer can modify receipts or join a process group. Real
workers, GPU jobs, arbitrary commands, and production isolation are unsupported.
A crash in the launch window is never interpreted as permission to relaunch.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable


class ProcessError(RuntimeError):
    """The fixture cannot safely complete the requested operation."""


class StopUnconfirmed(ProcessError):
    """Owned processes are still present; retain CANCELLING for reconciliation."""


TERMINAL = frozenset({"EXITED", "CANCELLED", "TIMED_OUT", "LAUNCH_FAILED"})
MODES = frozenset({"success", "crash", "hang", "descendant", "ignore_term"})
IDENTITY_KEYS = ("boot_id", "pid", "start_ticks", "pgrp", "session", "uid")
MAX_LOG_BYTES = 4096
MAX_SECONDS = 30.0

# This literal is the only executable payload. -I -S and an empty environment
# avoid loading sitecustomize, PYTHONPATH, credentials, or caller-controlled code.
# All descendants inherit the new session/group and the same startup barrier.
_FIXTURE = r'''
import os, resource, signal, sys, time
mode, duration, gate, ready = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
# Independent safety backstop, also active if the controller vanishes.
signal.signal(signal.SIGALRM, lambda signum, frame: os._exit(124))
signal.alarm(32)
stop = False
def request_stop(signum, frame):
    global stop
    stop = True
if mode in ('ignore_term', 'descendant'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = None
if mode == 'descendant':
    child = os.fork()
    if child == 0:
        signal.alarm(32)  # fork does not inherit the leader's alarm timer
        os.close(ready)
    else:
        # Keep the leader alive to reap its child during normal cancellation.
        signal.signal(signal.SIGTERM, request_stop)
if child != 0:
    os.write(ready, b'R')
    os.close(ready)
if os.read(gate, 1) != b'G':
    os._exit(125)
os.close(gate)
if child == 0:
    time.sleep(duration)
    os._exit(0)
if child is not None:
    os.waitpid(child, 0)
    os._exit(0)
os.write(1, b'cpu-fixture-start\n')
if mode == 'crash':
    os._exit(7)
if mode in ('hang', 'ignore_term'):
    time.sleep(duration)
else:
    # Deliberately tiny, bounded CPU work. No external commands or network.
    checksum = sum(i * i for i in range(10000))
    time.sleep(duration)
    os.write(1, ('cpu-fixture-checksum=%d\n' % checksum).encode('ascii'))
os.write(1, b'cpu-fixture-complete\n')
'''


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _read_process(pid: int) -> dict[str, Any] | None:
    """Return identity, None only for a definitively absent PID; otherwise raise."""
    try:
        path = Path("/proc") / str(pid)
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        uid = path.stat().st_uid
        return {"boot_id": _boot_id(), "pid": pid,
                "start_ticks": int(fields[19]), "pgrp": int(fields[2]),
                "session": int(fields[3]), "uid": uid, "state": fields[0]}
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError, IndexError) as exc:
        raise ProcessError("process identity is unreadable") from exc


def _same_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in IDENTITY_KEYS)


def _live(process: dict[str, Any]) -> bool:
    # Zombies cannot execute or retain resources; reap direct children separately.
    return process.get("state") not in {"Z", "X"}


def _scan_group(pgrp: int) -> list[dict[str, Any]]:
    members = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise ProcessError("process table is unreadable") from exc
    for entry in entries:
        if entry.name.isdigit():
            identity = _read_process(int(entry.name))
            if identity and identity["pgrp"] == pgrp and _live(identity):
                members.append(identity)
    return members


def _number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProcessError(f"{name} must be a finite number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ProcessError(f"{name} is outside the fixture limit")
    return float(value)


class ProcessManager:
    """Durable single-fixture supervisor with conservative restart reconciliation.

    The dedicated root must be a private, controller-owned local directory.
    Operations are serialized by flock, including between separate managers.
    Methods return JSON-compatible receipts. BLOCKED means no signal/relaunch is
    permitted until an operator resolves ownership; it is never a success state.
    Call wait() for active timeout enforcement. The fixture also self-expires;
    this module does not install a daemon, production watchdog, or hard quota.
    """

    def __init__(self, root: str | Path):
        if not sys.platform.startswith("linux"):
            raise ProcessError("the CPU process fixture requires Linux /proc")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise ProcessError("Linux pidfds are required for safe fixture signals")
        candidate = Path(root).absolute()
        if any(part.is_symlink() for part in (candidate, *candidate.parents)):
            raise ProcessError("process root must not traverse a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = candidate
        self._children: dict[str, subprocess.Popen] = {}
        self._check_directory(self.root)
        self._sync_dir(self.root.parent)

    @staticmethod
    def _check_directory(path: Path) -> None:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise ProcessError("process directory is not an owned directory")

    def _directory(self, run_id: str, *, create: bool = False) -> Path:
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
            raise ProcessError("invalid fixture run ID")
        path = self.root / run_id
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        self._check_directory(path)
        return path

    @contextmanager
    def _lock(self):
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ProcessError("invalid process lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _write(self, run_id: str, receipt: dict[str, Any]) -> None:
        """Atomic file replacement plus file/directory fsync; never truncate old state."""
        directory = self._directory(run_id)
        receipt["observed_at"] = time.time()
        payload = json.dumps(receipt, sort_keys=True, allow_nan=False).encode() + b"\n"
        fd, name = tempfile.mkstemp(prefix=".receipt-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, directory / "receipt.json")
            self._sync_dir(directory)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _sync_dir(path: Path) -> None:
        fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read(self, run_id: str) -> dict[str, Any]:
        try:
            path = self._directory(run_id) / "receipt.json"
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as stream:
                if os.fstat(stream.fileno()).st_size > 65536:
                    raise ProcessError("receipt exceeds size limit")
                receipt = json.load(stream)
            if not isinstance(receipt, dict) or receipt.get("run_id") != run_id or receipt.get("version") != 1:
                raise ProcessError("invalid fixture receipt")
            if receipt.get("state") not in TERMINAL | {"RUNNING", "LAUNCH_INTENT", "CANCELLING", "BLOCKED"}:
                raise ProcessError("invalid fixture receipt state")
            return receipt
        except (OSError, ValueError) as exc:
            raise ProcessError("fixture receipt is missing or invalid; do not relaunch") from exc

    def _blocked(self, run_id: str, receipt: dict[str, Any], reason: str) -> dict[str, Any]:
        receipt.update(state="BLOCKED", reason=reason)
        self._write(run_id, receipt)
        return receipt

    def _owned_group(self, receipt: dict[str, Any]) -> list[dict[str, Any]]:
        """Require boot identity and a surviving, recorded group anchor."""
        leader = receipt.get("identity")
        known = receipt.get("members")
        if not isinstance(leader, dict) or not isinstance(known, list) or not known:
            raise ProcessError("launch identity was not durably recorded")
        if any(type(leader.get(key)) is not int or leader[key] <= 0
               for key in ("pid", "start_ticks", "pgrp", "session")):
            raise ProcessError("invalid recorded process identity")
        if leader.get("boot_id") != _boot_id():
            raise ProcessError("boot identity changed")
        if leader["pgrp"] != leader["pid"] or leader["session"] != leader["pid"] or leader.get("uid") != os.getuid():
            raise ProcessError("recorded group is not an owned fixture session")
        anchor = False
        for previous in known:
            if not isinstance(previous, dict) or type(previous.get("pid")) is not int:
                raise ProcessError("invalid recorded member identity")
            current = _read_process(previous["pid"])
            if current is not None:
                if not _same_identity(previous, current):
                    raise ProcessError("PID identity changed; no signal is permitted")
                anchor = anchor or _live(current)
        members = _scan_group(leader["pgrp"])
        if members and not anchor:
            raise ProcessError("live group has no surviving recorded identity")
        for member in members:
            if member["session"] != leader["session"] or member["uid"] != leader["uid"]:
                raise ProcessError("group ownership is unknown")
        return members

    def launch(self, run_id: str, *, mode: str = "success", duration: float = 0.1,
               timeout: float = 5.0,
               before_release: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Start only the fixed fixture after durable identity and a trusted gate.

        before_release is a controller-side integration callback (never CLI or
        transport input). It receives a copy of the RUNNING receipt, allowing an
        admission ledger to recheck approval immediately before fixture work.
        Raising aborts startup and cleans the owned group without releasing it.
        The callback must not reenter manager methods (the root lock is held).
        """
        if before_release is not None and not callable(before_release):
            raise ProcessError("before_release must be a trusted controller callback")
        if mode not in MODES:
            raise ProcessError("only fixed CPU fixture modes are supported")
        duration = _number(duration, "duration", minimum=0.0, maximum=MAX_SECONDS)
        timeout = _number(timeout, "timeout", minimum=0.01, maximum=MAX_SECONDS)
        with self._lock():
            if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
                raise ProcessError("invalid fixture run ID")
            directory = self.root / run_id
            if directory.exists() or directory.is_symlink():
                # Even a directory with a missing receipt is unresolved. A lost
                # receipt must never turn a restart into permission to relaunch.
                return self._reconcile_locked(run_id)
            # At most one nonterminal receipt in this dedicated process root.
            # Check before mkdir so a denied second launch leaves no empty run.
            for candidate in self.root.iterdir():
                if candidate.is_dir():
                    prior = self._read(candidate.name)
                    if prior.get("state") not in TERMINAL:
                        raise ProcessError("another fixture is active or unresolved")
            directory = self._directory(run_id, create=True)
            receipt = {"version": 1, "run_id": run_id, "state": "LAUNCH_INTENT",
                       "mode": mode, "duration": duration, "timeout": timeout,
                       "created_at": time.time(), "created_monotonic": time.monotonic(),
                       "identity": None, "members": [], "signals": [],
                       "isolation": "same-UID CPU fixture; not a security boundary"}
            self._write(run_id, receipt)
            self._sync_dir(self.root)
            gate_read = gate_write = ready_read = ready_write = log_fd = None
            child = None
            try:
                gate_read, gate_write = os.pipe()
                ready_read, ready_write = os.pipe()
                log_fd = os.open(directory / "fixture.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                child = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-c", _FIXTURE, mode,
                     str(duration), str(gate_read), str(ready_write)],
                    stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                    cwd=directory, env={}, close_fds=True,
                    pass_fds=(gate_read, ready_write), start_new_session=True)
                self._children[run_id] = child
                os.close(gate_read)
                gate_read = None
                os.close(ready_write)
                ready_write = None
                if not select.select([ready_read], [], [], 2.0)[0] or os.read(ready_read, 1) != b"R":
                    raise ProcessError("fixture failed to reach startup barrier")
                identity = _read_process(child.pid)
                if identity is None or identity["pgrp"] != child.pid or identity["session"] != child.pid:
                    raise ProcessError("fixture launch identity is unknown")
                receipt["identity"] = identity
                receipt["members"] = [identity]
                receipt["members"] = self._owned_group(receipt)
                receipt["state"] = "RUNNING"
                self._write(run_id, receipt)
                # No fixture work occurs before its identity is durable and
                # the controller's last approval/revocation check has succeeded.
                if before_release is not None:
                    before_release(copy.deepcopy(receipt))
                os.write(gate_write, b"G" * len(receipt["members"]))
                return receipt
            except BaseException:
                # Closing the barrier stops not-yet-started fixed children. Signal
                # only when we have a verified identity, even on receipt failure.
                if gate_write is not None:
                    os.close(gate_write)
                    gate_write = None
                if child is not None:
                    if receipt.get("identity") is None:
                        observed = _read_process(child.pid)
                        if observed and observed["pgrp"] == child.pid and observed["session"] == child.pid:
                            receipt["identity"] = observed
                            receipt["members"] = [observed]
                    try:
                        self._terminate(receipt, grace=0.1)
                        child.wait(timeout=2.0)
                        receipt.update(state="LAUNCH_FAILED", reason="launch failed; owned children stopped")
                    except (ProcessError, OSError, subprocess.TimeoutExpired):
                        receipt.update(state="BLOCKED", reason="launch failure cleanup is unconfirmed")
                else:
                    receipt.update(state="LAUNCH_FAILED", reason="process creation failed")
                try:
                    self._write(run_id, receipt)
                except OSError:
                    # The durable LAUNCH_INTENT (or RUNNING receipt) remains.
                    pass
                raise
            finally:
                for fd in (gate_read, gate_write, ready_read, ready_write, log_fd):
                    if fd is not None:
                        os.close(fd)

    def _reap(self, run_id: str) -> int | None:
        child = self._children.get(run_id)
        return child.poll() if child is not None else None

    def _reconcile_locked(self, run_id: str) -> dict[str, Any]:
        receipt = self._read(run_id)
        if receipt.get("state") in TERMINAL:
            return receipt
        if receipt.get("state") == "BLOCKED":
            return receipt
        try:
            members = self._owned_group(receipt)
        except ProcessError as exc:
            return self._blocked(run_id, receipt, str(exc))
        receipt["members"] = members or receipt["members"]
        if receipt.get("state") == "CANCELLING":
            return self._cancel_locked(run_id, receipt, grace=0.2,
                                       final_state=receipt.get("stop_target", "CANCELLED"))
        code = self._reap(run_id)
        if not members:
            receipt.update(state="EXITED", returncode=code,
                           reason="owned group is no longer running; exit code may be unavailable after restart")
        else:
            try:
                started = _number(receipt.get("created_monotonic"), "start time", minimum=0.0, maximum=1e12)
                timeout = _number(receipt.get("timeout"), "timeout", minimum=0.01, maximum=MAX_SECONDS)
                if time.monotonic() < started:
                    raise ProcessError("monotonic clock moved backward")
            except ProcessError as exc:
                return self._blocked(run_id, receipt, str(exc))
            if time.monotonic() - started >= timeout:
                return self._cancel_locked(run_id, receipt, grace=0.2, final_state="TIMED_OUT")
        self._write(run_id, receipt)
        return receipt

    def reconcile(self, run_id: str) -> dict[str, Any]:
        """Observe existing ownership only; never launch or replace a worker."""
        with self._lock():
            return self._reconcile_locked(run_id)

    status = reconcile

    @staticmethod
    def _signal_verified(identity: dict[str, Any], signum: int) -> bool:
        """Signal through a pidfd, rechecking identity after opening it (no PID race)."""
        try:
            fd = os.pidfd_open(identity["pid"], 0)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise ProcessError("cannot pin owned PID for signaling") from exc
        try:
            current = _read_process(identity["pid"])
            if current is None:
                return False
            if not _same_identity(identity, current):
                raise ProcessError("PID identity changed before signal")
            if not _live(current):
                return False
            signal.pidfd_send_signal(fd, signum)
            return True
        except ProcessLookupError:
            return False
        finally:
            os.close(fd)

    def _terminate(self, receipt: dict[str, Any], *, grace: float) -> None:
        members = self._owned_group(receipt)
        if not members:
            return
        receipt["members"] = members
        for signum, delay in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
            # Children first permits the fixed leader to reap them before exit.
            current = self._owned_group(receipt)
            current.sort(key=lambda item: item["pid"] == receipt["identity"]["pid"])
            for member in current:
                if self._signal_verified(member, signum):
                    receipt["signals"].append({"pid": member["pid"], "signal": signum.name})
                if signum == signal.SIGKILL and member["pid"] != receipt["identity"]["pid"]:
                    time.sleep(0.02)
            deadline = time.monotonic() + delay
            while True:
                members = self._owned_group(receipt)
                if not members:
                    return
                # Preserve all earlier identity anchors until full group exit.
                known = {item["pid"]: item for item in receipt["members"]}
                known.update({item["pid"]: item for item in members})
                receipt["members"] = list(known.values())
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
        raise StopUnconfirmed("owned group stop has not been confirmed")

    def _cancel_locked(self, run_id: str, receipt: dict[str, Any], *, grace: float,
                       final_state: str = "CANCELLED") -> dict[str, Any]:
        if receipt.get("state") in TERMINAL or receipt.get("state") == "BLOCKED":
            return receipt
        try:
            self._owned_group(receipt)
        except ProcessError as exc:
            return self._blocked(run_id, receipt, str(exc))
        receipt.update(state="CANCELLING", stop_target=final_state,
                       reason="verifying owned process group termination")
        self._write(run_id, receipt)
        try:
            self._terminate(receipt, grace=grace)
        except StopUnconfirmed as exc:
            receipt.update(state="CANCELLING", reason=str(exc))
            self._write(run_id, receipt)
            return receipt
        except (ProcessError, OSError) as exc:
            return self._blocked(run_id, receipt, str(exc))
        receipt.update(state=final_state, returncode=self._reap(run_id),
                       reason="owned process group termination verified")
        self._write(run_id, receipt)
        return receipt

    def cancel(self, run_id: str, *, grace: float = 0.2) -> dict[str, Any]:
        grace = _number(grace, "grace", minimum=0.0, maximum=5.0)
        with self._lock():
            return self._cancel_locked(run_id, self._read(run_id), grace=grace)

    def wait(self, run_id: str, *, poll_interval: float = 0.02) -> dict[str, Any]:
        interval = _number(poll_interval, "poll_interval", minimum=0.001, maximum=1.0)
        while True:
            receipt = self.reconcile(run_id)
            if receipt["state"] in TERMINAL or receipt["state"] == "BLOCKED":
                return receipt
            time.sleep(interval)

    def logs(self, run_id: str) -> str:
        """Local fixture text only, bounded on disk and when read; not a publisher."""
        with self._lock():
            fd = os.open(self._directory(run_id) / "fixture.log", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                return stream.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
