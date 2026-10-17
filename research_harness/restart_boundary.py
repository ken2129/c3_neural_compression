"""Conservative restart-epoch candidates, not standalone container-death proof.

A changed PID namespace and newer PID 1 start time do NOT independently prove
that Docker stopped the old container, killed all its workers, or enforced a
security boundary. Using a candidate to release results requires an externally
verified deployment contract: exactly one dedicated container may use this
manager volume; the trusted manager is PID 1; exiting PID 1 causes Docker to stop
that container and restart it under the configured policy; procfs and manager
state cannot be forged by a worker. Host PID namespace sharing is excluded.

The full container ID is an externally verified deployment binding supplied by
the caller. It is NOT discovered or authenticated by this module. In particular,
/etc/hostname, a short ID, an environment variable, or worker JSON alone cannot
establish container identity. The deployment-contract flag is an assertion by
the trusted caller, not a mechanism which verifies that contract.

Collection is explicit, bounded, read-only and uses fixed procfs paths. Tests
inject only fixed fake metadata and never observe or mutate a live container.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import re
import stat

from .schema import ContractError, fields

BOOT_ID_PATH = '/proc/sys/kernel/random/boot_id'
PID1_STAT_PATH = '/proc/1/stat'
PID1_NAMESPACE_PATH = '/proc/1/ns/pid'
SELF_NAMESPACE_PATH = '/proc/self/ns/pid'
MAX_STAT_BYTES = 8192
MAX_U64 = (1 << 64) - 1
_CONTAINER_ID = re.compile(r'[0-9a-f]{64}\Z')
_BOOT_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')


def _container_id(value):
    if type(value) is not str or not _CONTAINER_ID.fullmatch(value) or value == '0' * 64:
        raise ContractError('full externally verified Docker container ID required')
    return value


def _boot_id(value):
    if (type(value) is not str or not _BOOT_ID.fullmatch(value)
            or value.replace('-', '') == '0' * 32):
        raise ContractError('canonical nonzero boot ID required')
    return value


def _u64(value):
    if type(value) is not int or not 1 <= value <= MAX_U64:
        raise ContractError('positive bounded epoch integer required')
    return value


@dataclass(frozen=True)
class RestartEpoch:
    """Strict observed process epoch tagged with an external container binding."""
    container_id: str
    boot_id: str
    pid_namespace_inode: int
    pid1_start_ticks: int
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError('unsupported restart epoch version')
        _container_id(self.container_id)
        _boot_id(self.boot_id)
        _u64(self.pid_namespace_inode)
        _u64(self.pid1_start_ticks)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        fields(value, cls.__dataclass_fields__)
        return cls(**value)


# Short name for callers that keep this type within a restart-boundary module.
Epoch = RestartEpoch


def restart_boundary_candidate(before, after, *, expected_container_id,
                               deployment_contract_verified=False):
    """Pure, fail-closed comparison; True is a candidate under the above contract.

    No same-epoch, PID-1-only replacement, namespace-only change, boot change,
    container recreation, backwards time or incomplete observation is accepted.
    Namespace inode reuse or equal-tick restarts are conservative false negatives.
    Approval of publication, worker isolation and termination are separate gates.
    """
    if type(before) is not RestartEpoch or type(after) is not RestartEpoch:
        raise ContractError('complete typed before and after epochs required')
    _container_id(expected_container_id)
    if deployment_contract_verified is not True:
        return False
    return (before.container_id == after.container_id == expected_container_id
            and before.boot_id == after.boot_id
            and before.pid_namespace_inode != after.pid_namespace_inode
            and after.pid1_start_ticks > before.pid1_start_ticks)


def _parse_boot_id(raw):
    if type(raw) is not bytes or not 1 <= len(raw) <= 64:
        raise ContractError('boot ID observation must be bounded bytes')
    if raw.endswith(b'\n'):
        raw = raw[:-1]
    try:
        return _boot_id(raw.decode('ascii'))
    except UnicodeError:
        raise ContractError('invalid boot ID observation') from None


def _parse_pid1_start_ticks(raw):
    """Read field 22 after the final comm delimiter, not by whitespace index.

    Linux comm can contain spaces and ')' characters. The remaining stat fields
    are numeric, so the final ') ' separates comm from field 3 (state).
    """
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_STAT_BYTES or not raw.startswith(b'1 ('):
        raise ContractError('bounded PID 1 stat observation required')
    end = raw.rfind(b') ')
    if end < 4 or end - 3 > 15:
        raise ContractError('invalid PID 1 command field')
    parts = raw[end + 2:].split()
    if not 20 <= len(parts) <= 64 or parts[0] not in (b'R', b'S', b'D', b'T', b't', b'P', b'I'):
        raise ContractError('PID 1 stat is incomplete or not a live process')
    for value in parts[1:]:
        if len(value) > 21 or re.fullmatch(rb'-?(?:0|[1-9][0-9]*)', value) is None:
            raise ContractError('invalid numeric PID 1 stat field')
    # procfs must expose PID 1 as namespace init, without a visible parent.
    if parts[1] != b'0' or not re.fullmatch(rb'[1-9][0-9]*', parts[19]):
        raise ContractError('PID 1 identity or start ticks unavailable')
    return _u64(int(parts[19]))


class _ProcReader:
    """Fixed-path live reader; construction does no I/O. Never invoked by tests."""
    @staticmethod
    def read_bytes(path, maximum):
        if (path, maximum) not in ((BOOT_ID_PATH, 64), (PID1_STAT_PATH, MAX_STAT_BYTES)):
            raise ContractError('procfs read outside restart metadata scope')
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ContractError('restart metadata is not a regular procfs file')
            data = bytearray()
            while len(data) <= maximum:
                chunk = os.read(descriptor, min(4096, maximum + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > maximum:
                raise ContractError('restart metadata exceeds bound')
            return bytes(data)
        except OSError:
            raise ContractError('restart metadata unavailable') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def inode(path):
        if path not in (PID1_NAMESPACE_PATH, SELF_NAMESPACE_PATH):
            raise ContractError('namespace read outside restart metadata scope')
        try:
            # Namespace files are intentional procfs links to nsfs objects.
            return os.stat(path).st_ino
        except OSError:
            raise ContractError('PID namespace observation unavailable') from None

    @staticmethod
    def pid():
        return os.getpid()


def read_epoch(*, container_id, reader=None):
    """Take two consistent observations, only from a manager running as PID 1.

    container_id is supplied by the externally verified deployment, never from
    Issue/worker input or inferred from hostname. reader is a trusted injectable
    reader with read_bytes(path, maximum), inode(path), and pid() methods. The
    default reader observes only the fixed procfs paths above. It neither calls
    Docker nor proves the external container/volume/restart-policy contract.
    """
    _container_id(container_id)
    if reader is None:
        reader = _ProcReader()

    def observe():
        pid = reader.pid()
        if type(pid) is not int or pid != 1:
            raise ContractError('restart observer must be the trusted PID 1 manager')
        boot_id = _parse_boot_id(reader.read_bytes(BOOT_ID_PATH, 64))
        start_ticks = _parse_pid1_start_ticks(reader.read_bytes(PID1_STAT_PATH, MAX_STAT_BYTES))
        namespace = _u64(reader.inode(PID1_NAMESPACE_PATH))
        if _u64(reader.inode(SELF_NAMESPACE_PATH)) != namespace:
            raise ContractError('manager and PID 1 namespace observations disagree')
        return RestartEpoch(container_id, boot_id, namespace, start_ticks)

    try:
        first, second = observe(), observe()
    except (OSError, AttributeError, KeyError, TypeError, ValueError):
        raise ContractError('complete restart metadata could not be verified') from None
    if first != second:
        raise ContractError('restart metadata changed during observation')
    return first
