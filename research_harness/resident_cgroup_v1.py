"""Resident-only admission for Docker's private cgroup-v1 layout.

Only kernel observations choose this adapter. It never mounts, writes a quota,
changes a namespace, or accepts host-visible cgroup paths. The v2 and disposable
backends retain their existing checks. A real bounded-container check remains
necessary; fixtures alone do not prove quota enforcement.
"""
from pathlib import Path
import os
import re
import stat

from .goal_runtime import GoalRuntimeBlocked, _protected

CGROOT = '/sys/fs/cgroup'
MAX_MEMORY = 1024 ** 3
MAX_PIDS = 64


def blocked(reason):
    raise GoalRuntimeBlocked(reason)


def text(path, maximum=131072):
    value = Path(path).read_text()
    if len(value) > maximum:
        blocked('GOAL_CGROUP_NAMESPACE_REQUIRED')
    return value


def memberships(raw):
    groups = set()
    controllers = set()
    ids = set()
    unified = False
    for line in raw.strip().splitlines():
        parts = line.split(':')
        if len(parts) != 3 or not parts[0].isascii() or not parts[0].isdigit() or parts[2] != '/':
            blocked('GOAL_CGROUP_NAMESPACE_REQUIRED')
        number = int(parts[0])
        if number in ids or number > 2 ** 31 - 1:
            blocked('GOAL_CGROUP_NAMESPACE_REQUIRED')
        ids.add(number)
        if number == 0:
            if parts[1] or unified:
                blocked('GOAL_CGROUP_NAMESPACE_REQUIRED')
            unified = True
            continue
        names = parts[1].split(',')
        if (not names or len(set(names)) != len(names)
                or any(re.fullmatch(r'(?:name=)?[a-z][a-z0-9_]*', n) is None for n in names)
                or controllers.intersection(names)):
            blocked('GOAL_CGROUP_NAMESPACE_REQUIRED')
        group = frozenset(names)
        groups.add(group)
        controllers.update(names)
    if not {'memory', 'cpu', 'pids'} <= controllers:
        blocked('GOAL_CGROUP_NAMESPACE_REQUIRED')
    return groups, controllers


def mounts(raw):
    result = {}
    for line in raw.splitlines():
        left, right = line.split(' - ', 1)
        a, b = left.split(), right.split()
        if len(a) < 6 or len(b) < 3 or a[4] in result:
            blocked('GOAL_READONLY_ROOT_CGROUP_REQUIRED')
        result[a[4]] = dict(root=a[3], options=set(a[5].split(',')),
                           fs=b[0], super_options=set(b[2].split(',')))
    return result


def private_directory(path, *, exact=False):
    info = Path(path).lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or exact and stat.S_IMODE(info.st_mode) != 0o755):
        blocked('GOAL_READONLY_ROOT_CGROUP_REQUIRED')


def controller_mounts(table, groups, names):
    parent = table.get(CGROOT)
    if (not parent or parent['fs'] != 'tmpfs' or parent['root'] != '/'
            or not {'nosuid', 'nodev', 'noexec'} <= parent['options']):
        blocked('GOAL_READONLY_ROOT_CGROUP_REQUIRED')
    # Docker v1 uses an rw tmpfs only as the root-owned directory of controller
    # mountpoints. It is not a quota interface. Every controller below must be
    # a separate ro cgroup mount; no untrusted role can write this 0755 parent.
    private_directory(CGROOT, exact=True)
    seen = set()
    selected = {}
    for point, mount in table.items():
        if not point.startswith(CGROOT + '/'):
            continue
        basename = point[len(CGROOT) + 1:]
        members = frozenset(mount['super_options'].intersection(names))
        if ('/' in basename or mount['fs'] != 'cgroup' or mount['root'] != '/'
                or not {'ro', 'nosuid', 'nodev', 'noexec'} <= mount['options']
                or members not in groups or members in seen
                or mount['super_options'] - members - {'ro', 'rw', 'xattr', 'noprefix', 'clone_children'}
                or set(basename.split(',')) != {n.removeprefix('name=') for n in members}):
            blocked('GOAL_READONLY_ROOT_CGROUP_REQUIRED')
        private_directory(point)
        seen.add(members)
        for name in members:
            if name in ('memory', 'cpu', 'pids'):
                selected[name] = point
    if seen != groups or set(selected) != {'memory', 'cpu', 'pids'}:
        blocked('GOAL_READONLY_ROOT_CGROUP_REQUIRED')
    return selected


def integer(path):
    info = Path(path).lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
        blocked('GOAL_RESOURCE_UNBOUNDED')
    value = text(path, maximum=128).strip()
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        blocked('GOAL_RESOURCE_UNBOUNDED')
    return int(value)


def workspace(table):
    root = table.get('/')
    if not root or 'ro' not in root['options']:
        blocked('GOAL_READONLY_ROOT_CGROUP_REQUIRED')
    for point in ('/work', '/dev/shm'):
        mount = table.get(point)
        if (not mount or mount['fs'] != 'tmpfs'
                or not {'noexec', 'nosuid', 'nodev'} <= mount['options']):
            blocked('GOAL_PRIVATE_TMPFS_REQUIRED')
    if any(p.startswith('/work/') or p.startswith('/dev/shm/') for p in table):
        blocked('GOAL_NESTED_WORK_MOUNT')
    _protected('/work', directory=True)
    if stat.S_IMODE(Path('/work').lstat().st_mode) != 0o755:
        blocked('GOAL_WORK_ROOT_OWNERSHIP')
    shm_info = Path('/dev/shm').lstat()
    if (not stat.S_ISDIR(shm_info.st_mode) or shm_info.st_uid != 0
            or stat.S_IMODE(shm_info.st_mode) != 0o1777):
        blocked('GOAL_PRIVATE_TMPFS_REQUIRED')
    shm = os.statvfs('/dev/shm')
    if shm.f_blocks * shm.f_frsize > 64 * 1024 ** 2:
        blocked('GOAL_IPC_TMPFS_LIMIT')
    if (Path('/var/run/docker.sock').exists() or Path('/run/containerd/containerd.sock').exists()
            or list(Path('/dev').glob('nvidia*')) or Path('/dev/dri').exists()):
        blocked('GOAL_HOST_SOCKET_OR_GPU')
    work = os.statvfs('/work')
    size = work.f_blocks * work.f_frsize
    if not 1024 ** 2 <= size <= 256 * 1024 ** 2:
        blocked('GOAL_PRIVATE_TMPFS_REQUIRED')
    return size


def resources():
    try:
        groups, names = memberships(text('/proc/self/cgroup', maximum=8192))
        table = mounts(text('/proc/self/mountinfo'))
        points = controller_mounts(table, groups, names)
        values = dict(memory=integer(points['memory'] + '/memory.limit_in_bytes'),
                      memsw=integer(points['memory'] + '/memory.memsw.limit_in_bytes'),
                      hierarchy=integer(points['memory'] + '/memory.use_hierarchy'),
                      pids=integer(points['pids'] + '/pids.max'),
                      cpu_quota=integer(points['cpu'] + '/cpu.cfs_quota_us'),
                      cpu_period=integer(points['cpu'] + '/cpu.cfs_period_us'))
        if (not 64 * 1024 ** 2 <= values['memory'] <= MAX_MEMORY
                or values['memsw'] != values['memory'] or values['hierarchy'] != 1
                or not 2 <= values['pids'] <= MAX_PIDS):
            blocked('GOAL_RESOURCE_UNBOUNDED')
        if (not 1000 <= values['cpu_period'] <= 1000000
                or values['cpu_quota'] != values['cpu_period']):
            blocked('GOAL_CPU_QUOTA_REQUIRED')
        return dict(memory=values['memory'], swap=0, pids=values['pids'],
                    cpu_quota=values['cpu_quota'], cpu_period=values['cpu_period'],
                    work_bytes=workspace(table))
    except GoalRuntimeBlocked:
        raise
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        # Kernel/DAC/format failures never become an unbounded or guessed value.
        raise GoalRuntimeBlocked('GOAL_RESOURCE_UNBOUNDED') from None
