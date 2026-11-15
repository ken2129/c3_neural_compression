"""Resident v1 parser/admission fixtures; no container start, model or auth.

Filesystem/kernel facts are fixtures except the explicitly named current-layout
rejection. Passing these is not evidence of live cgroup quota enforcement.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research_harness import resident_cgroup_v1 as v1
from research_harness.goal_runtime import GoalLinuxUIDBackend, GoalRuntimeBlocked
from research_harness.resident_runtime import ResidentLinuxBackend
from research_harness.uid_supervisor import LinuxUIDBackend, SupervisorBlocked


CGROUP = '3:cpu,cpuacct:/\n2:memory:/\n1:pids:/\n0::/\n'
MOUNTS = (
    '1 0 0:1 / / ro,relatime - overlay overlay rw\n'
    '2 1 0:2 / /work rw,nosuid,nodev,noexec - tmpfs tmpfs rw\n'
    '3 1 0:3 / /dev/shm rw,nosuid,nodev,noexec - tmpfs shm rw\n'
    '4 1 0:4 / /sys/fs/cgroup rw,nosuid,nodev,noexec - tmpfs tmpfs rw\n'
    '5 4 0:5 / /sys/fs/cgroup/memory ro,nosuid,nodev,noexec - cgroup cgroup rw,memory\n'
    '6 4 0:6 / /sys/fs/cgroup/cpu,cpuacct ro,nosuid,nodev,noexec - cgroup cgroup rw,cpu,cpuacct\n'
    '7 4 0:7 / /sys/fs/cgroup/pids ro,nosuid,nodev,noexec - cgroup cgroup rw,pids\n'
)
LIMITS = {
    '/sys/fs/cgroup/memory/memory.limit_in_bytes': str(1024 ** 3),
    '/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes': str(1024 ** 3),
    '/sys/fs/cgroup/memory/memory.use_hierarchy': '1',
    '/sys/fs/cgroup/pids/pids.max': '64',
    '/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us': '100000',
    '/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us': '100000',
}


class ResidentV1ResourceTests(unittest.TestCase):
    def setUp(self):
        self.files = {'/proc/self/cgroup': CGROUP, '/proc/self/mountinfo': MOUNTS, **LIMITS}
        self.stats = {}
        self.present = set()
        self.gpu = []
        self.sizes = {'/work': 256 * 1024 ** 2, '/dev/shm': 64 * 1024 ** 2}
        self.reads = []

    def read(self, path):
        key = str(path)
        self.reads.append(key)
        value = self.files[key]
        if isinstance(value, Exception):
            raise value
        return value

    def info(self, path):
        key = str(path)
        if key in self.stats:
            return self.stats[key]
        if key in LIMITS:
            mode = stat.S_IFREG | 0o644
        else:
            mode = stat.S_IFDIR | (0o1777 if key == '/dev/shm' else 0o755)
        return SimpleNamespace(st_mode=mode, st_uid=0, st_nlink=1)

    def observe(self, function=v1.resources):
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, 'read_text', new=lambda path, *a, **kw: self.read(path)))
            stack.enter_context(patch.object(Path, 'lstat', new=lambda path: self.info(path)))
            stack.enter_context(patch.object(Path, 'exists', new=lambda path: str(path) in self.present))
            stack.enter_context(patch.object(Path, 'glob', new=lambda path, pattern: list(self.gpu)))
            stack.enter_context(patch.object(os, 'statvfs',
                side_effect=lambda path: SimpleNamespace(f_blocks=self.sizes[str(path)], f_frsize=1)))
            return function()

    def rejected(self, function=v1.resources):
        with self.assertRaises(GoalRuntimeBlocked):
            self.observe(function)

    def test_private_v1_reads_all_actual_limit_interfaces_and_returns_common_units(self):
        self.assertEqual(self.observe(), dict(memory=1024 ** 3, swap=0, pids=64,
                         cpu_quota=100000, cpu_period=100000, work_bytes=256 * 1024 ** 2))
        self.assertTrue(set(LIMITS) <= set(self.reads))
        self.assertFalse(any(p.endswith(('memory.max', 'cpu.max')) for p in self.reads))

    def test_optional_hybrid_entry_and_named_controller_have_exact_readonly_mount(self):
        self.files['/proc/self/cgroup'] = CGROUP + '4:name=systemd:/\n'
        self.files['/proc/self/mountinfo'] = MOUNTS + (
            '8 4 0:8 / /sys/fs/cgroup/systemd ro,nosuid,nodev,noexec - cgroup cgroup rw,xattr,name=systemd\n')
        self.assertEqual(self.observe()['pids'], 64)
        self.files['/proc/self/cgroup'] = self.files['/proc/self/cgroup'].replace('0::/\n', '')
        self.assertEqual(self.observe()['memory'], 1024 ** 3)

    def test_host_visible_paths_missing_controllers_duplicate_ids_and_malformed_memberships_reject(self):
        samples = [
            CGROUP.replace('2:memory:/', '2:memory:/docker/other'),
            CGROUP.replace('0::/', '0::/docker/other'),
            CGROUP.replace('2:memory:/\n', ''),
            CGROUP.replace('2:memory:/', '3:memory:/'),
            CGROUP + '5:cpu:/\n', CGROUP + '0::/\n',
            CGROUP.replace('cpu,cpuacct', 'cpu,cpu'),
            CGROUP.replace('2:memory:/', '2:memory:/../'),
            CGROUP.replace('2:memory:/', '2:memory'),
            '0::/\n', '', 'x:cpu:/\n',
        ]
        for value in samples:
            with self.subTest(value=value):
                self.files['/proc/self/cgroup'] = value
                self.rejected()
                self.assertFalse(any(p in LIMITS for p in self.reads))
                self.reads.clear()

    def test_every_reported_hierarchy_requires_a_matching_mount(self):
        self.files['/proc/self/cgroup'] = CGROUP + '4:devices:/\n'
        self.rejected()
        self.files['/proc/self/cgroup'] = CGROUP
        self.files['/proc/self/mountinfo'] = '\n'.join(
            line for line in MOUNTS.splitlines() if '/sys/fs/cgroup/pids ' not in line)
        self.rejected()

    def test_controller_mounts_must_be_readonly_private_and_match_kernel_membership(self):
        for old, new in [
            ('/sys/fs/cgroup/memory ro,', '/sys/fs/cgroup/memory rw,'),
            ('/sys/fs/cgroup/memory ro,nosuid,nodev,noexec', '/sys/fs/cgroup/memory ro,nosuid,nodev'),
            ('0:5 / /sys/fs/cgroup/memory', '0:5 /docker/id /sys/fs/cgroup/memory'),
            ('- cgroup cgroup rw,memory', '- tmpfs cgroup rw,memory'),
            ('/sys/fs/cgroup/cpu,cpuacct ', '/sys/fs/cgroup/cpu '),
            ('rw,cpu,cpuacct', 'rw,cpu'),
            ('rw,memory', 'rw,memory,release_agent=/unexpected'),
        ]:
            with self.subTest(new=new):
                self.files['/proc/self/mountinfo'] = MOUNTS.replace(old, new)
                self.rejected()

    def test_controller_aliases_nested_mounts_and_duplicate_mountpoints_reject(self):
        additions = [
            '8 4 0:8 / /sys/fs/cgroup/alias ro - cgroup cgroup rw,memory\n',
            '8 5 0:8 / /sys/fs/cgroup/memory/child ro - cgroup cgroup rw,memory\n',
            '8 4 0:8 / /sys/fs/cgroup/memory ro - cgroup cgroup rw,memory\n',
            '8 4 0:8 / /sys/fs/cgroup/unified ro - cgroup2 cgroup rw\n',
        ]
        for extra in additions:
            with self.subTest(extra=extra):
                self.files['/proc/self/mountinfo'] = MOUNTS + extra
                self.rejected()

    def test_parent_tmpfs_has_no_quota_write_access_and_is_root_owned(self):
        for old, new in [
            ('/sys/fs/cgroup rw,nosuid,nodev,noexec - tmpfs', '/sys/fs/cgroup rw,nosuid,nodev,noexec - cgroup2'),
            ('/sys/fs/cgroup rw,nosuid,nodev,noexec', '/sys/fs/cgroup rw,nosuid,nodev'),
        ]:
            with self.subTest(new=new):
                self.files['/proc/self/mountinfo'] = MOUNTS.replace(old, new)
                self.rejected()
        self.files['/proc/self/mountinfo'] = MOUNTS
        for mode, uid in [(stat.S_IFDIR | 0o777, 0), (stat.S_IFDIR | 0o755, 10002),
                          (stat.S_IFLNK | 0o755, 0)]:
            with self.subTest(mode=mode, uid=uid):
                self.stats['/sys/fs/cgroup'] = SimpleNamespace(st_mode=mode, st_uid=uid)
                self.rejected()

    def test_memory_memsw_and_hierarchy_must_prove_finite_one_gib_no_swap(self):
        for path, values in [
            ('memory.limit_in_bytes', ['max', '-1', '0', str(2 ** 63 - 4096), str(1024 ** 3 + 1), '64']),
            ('memory.memsw.limit_in_bytes', ['max', '-1', '0', str(2 * 1024 ** 3)]),
            ('memory.use_hierarchy', ['0', '2', 'max']),
        ]:
            key = '/sys/fs/cgroup/memory/' + path
            for value in values:
                with self.subTest(path=path, value=value):
                    self.files = {'/proc/self/cgroup': CGROUP, '/proc/self/mountinfo': MOUNTS, **LIMITS}
                    self.files[key] = value
                    self.rejected()

    def test_cpu_and_pid_limits_must_be_finite_and_match_one_cpu_ceiling(self):
        for key, values in [
            ('/sys/fs/cgroup/pids/pids.max', ['max', '-1', '0', '1', '65']),
            ('/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us', ['-1', '0', '99999', '100001']),
            ('/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us', ['0', '-1', '999', '1000001']),
        ]:
            for value in values:
                with self.subTest(key=key, value=value):
                    self.files = {'/proc/self/cgroup': CGROUP, '/proc/self/mountinfo': MOUNTS, **LIMITS}
                    self.files[key] = value
                    self.rejected()

    def test_limit_interfaces_must_be_regular_root_owned_and_not_writable_by_untrusted_roles(self):
        key = '/sys/fs/cgroup/memory/memory.limit_in_bytes'
        for mode, uid in [(stat.S_IFLNK | 0o644, 0), (stat.S_IFREG | 0o666, 0),
                          (stat.S_IFREG | 0o644, 10002), (stat.S_IFDIR | 0o755, 0)]:
            with self.subTest(mode=mode, uid=uid):
                self.stats[key] = SimpleNamespace(st_mode=mode, st_uid=uid)
                self.rejected()

    def test_missing_unreadable_and_oversized_records_fail_without_guessing(self):
        for key, value in [
            ('/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes', FileNotFoundError()),
            ('/sys/fs/cgroup/pids/pids.max', PermissionError()),
            ('/sys/fs/cgroup/pids/pids.max', '9' * 129),
            ('/proc/self/cgroup', 'x' * 8193),
            ('/proc/self/mountinfo', 'malformed'),
            ('/proc/self/mountinfo', 'x' * 131073),
        ]:
            with self.subTest(key=key):
                self.files = {'/proc/self/cgroup': CGROUP, '/proc/self/mountinfo': MOUNTS, **LIMITS}
                self.files[key] = value
                self.rejected()

    def test_readonly_root_private_work_and_ipc_mounts_are_still_required(self):
        for old, new in [
            ('/ ro,relatime', '/ rw,relatime'),
            ('/work rw,nosuid,nodev,noexec', '/work rw,nosuid,nodev'),
            ('/dev/shm rw,nosuid,nodev,noexec', '/dev/shm rw,nosuid,nodev'),
            ('/work rw,nosuid,nodev,noexec - tmpfs', '/work rw,nosuid,nodev,noexec - ext4'),
        ]:
            with self.subTest(new=new):
                self.files['/proc/self/mountinfo'] = MOUNTS.replace(old, new)
                self.rejected()
        for point in ('/work/nested', '/dev/shm/nested'):
            self.files['/proc/self/mountinfo'] = MOUNTS + f'8 2 0:8 / {point} rw - tmpfs tmpfs rw\n'
            self.rejected()

    def test_work_and_shared_memory_ownership_modes_and_sizes_are_bounded(self):
        for point, mode, uid in [
            ('/work', 0o700, 0), ('/work', 0o755, 10002),
            ('/dev/shm', 0o777, 0), ('/dev/shm', 0o1777, 10002),
        ]:
            with self.subTest(point=point, mode=mode, uid=uid):
                self.stats = {point: SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid)}
                self.rejected()
        self.stats = {}
        for point, size in [('/work', 256 * 1024 ** 2 + 1), ('/work', 0),
                            ('/dev/shm', 64 * 1024 ** 2 + 1)]:
            with self.subTest(point=point, size=size):
                self.sizes = {'/work': 256 * 1024 ** 2, '/dev/shm': 64 * 1024 ** 2}
                self.sizes[point] = size
                self.rejected()

    def test_host_control_sockets_and_gpu_are_rejected(self):
        for point in ('/var/run/docker.sock', '/run/containerd/containerd.sock', '/dev/dri'):
            with self.subTest(point=point):
                self.present = {point}
                self.rejected()
        self.present = set()
        self.gpu = [Path('/dev/nvidia0')]
        self.rejected()

    def test_resident_selects_v1_only_from_kernel_cgroup_view(self):
        self.assertEqual(self.observe(ResidentLinuxBackend.resources)['swap'], 0)
        self.rejected(GoalLinuxUIDBackend.resources)
        with self.assertRaises(SupervisorBlocked):
            self.observe(LinuxUIDBackend.resources)

    def test_resident_v2_delegation_is_unchanged_and_never_falls_back_on_failure(self):
        self.files['/proc/self/cgroup'] = '0::/\n'
        expected = {'kernel_v2_result': True}
        with patch.object(GoalLinuxUIDBackend, 'resources', return_value=expected) as original, \
             patch.object(v1, 'resources') as fallback:
            self.assertIs(self.observe(ResidentLinuxBackend.resources), expected)
            original.assert_called_once_with()
            fallback.assert_not_called()
        with patch.object(GoalLinuxUIDBackend, 'resources',
                          side_effect=GoalRuntimeBlocked('GOAL_RESOURCE_UNBOUNDED')), \
             patch.object(v1, 'resources') as fallback:
            self.rejected(ResidentLinuxBackend.resources)
            fallback.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', 'actual Linux self-cgroup read')
    def test_actual_nonprivate_current_layout_is_rejected_before_reading_quotas(self):
        raw = Path('/proc/self/cgroup').read_text()
        if not any(line.split(':', 2)[-1] != '/' for line in raw.strip().splitlines()):
            self.skipTest('current process is already at a namespaced cgroup root')
        with self.assertRaises(GoalRuntimeBlocked) as caught:
            v1.resources()
        self.assertEqual(str(caught.exception), 'GOAL_CGROUP_NAMESPACE_REQUIRED')


if __name__ == '__main__':
    unittest.main()
