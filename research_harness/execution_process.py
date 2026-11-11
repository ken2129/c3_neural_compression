"""Private job framing, device observations and owned Linux descendant cleanup."""
import ctypes
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time
from .schema import ContractError,canonical,loads
class AgentStop(Exception):pass

def gpu_observation():
    rows = subprocess.check_output(['/usr/bin/nvidia-smi', '--query-gpu=uuid,utilization.gpu,memory.used',
        '--format=csv,noheader,nounits'], text=True, timeout=2).splitlines()
    devices = {}
    for row in rows:
        uuid, util, memory = [p.strip() for p in row.split(',')]
        devices[uuid] = (float(util), float(memory))
    active = subprocess.check_output(['/usr/bin/nvidia-smi', '--query-compute-apps=gpu_uuid,pid',
        '--format=csv,noheader'], text=True, timeout=2).splitlines()
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


