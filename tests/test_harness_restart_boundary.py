"""Fixed metadata only. Never invoke live procfs, Docker, signals or a restart."""
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from research_harness.schema import ContractError
from research_harness.restart_boundary import (BOOT_ID_PATH, Epoch, MAX_STAT_BYTES,
    MAX_U64, PID1_NAMESPACE_PATH, PID1_STAT_PATH, SELF_NAMESPACE_PATH,
    RestartEpoch, read_epoch, restart_boundary_candidate)

CONTAINER_ID = 'a' * 64
BOOT_ID = '12345678-1234-4234-8234-123456789abc'
OTHER_BOOT = 'abcdef12-abcd-4abc-8abc-abcdef123456'


def epoch(**changes):
    return RestartEpoch(**dict(dict(container_id=CONTAINER_ID, boot_id=BOOT_ID,
                                    pid_namespace_inode=4026533001, pid1_start_ticks=1000), **changes))


def proc_stat(*, ticks=1000, name=b'manager', pid=1, parent=0, state=b'S'):
    # Field 3 state; fields 4..21; field 22 starttime; fields 23..52.
    tail = [state, str(parent).encode()] + [b'0'] * 17 + [str(ticks).encode()] + [b'0'] * 30
    return str(pid).encode() + b' (' + name + b') ' + b' '.join(tail) + b'\n'


class FakeReader:
    def __init__(self):
        self.bytes = {BOOT_ID_PATH: BOOT_ID.encode() + b'\n', PID1_STAT_PATH: proc_stat()}
        self.inodes = {PID1_NAMESPACE_PATH: 4026533001, SELF_NAMESPACE_PATH: 4026533001}
        self.manager_pid = 1
        self.calls = []
        self.callback = lambda kind, path: None

    def read_bytes(self, path, maximum):
        self.calls.append(('read', path, maximum))
        self.callback('read', path)
        return self.bytes[path]

    def inode(self, path):
        self.calls.append(('inode', path))
        self.callback('inode', path)
        return self.inodes[path]

    def pid(self):
        self.calls.append(('pid',))
        self.callback('pid', None)
        return self.manager_pid


class EpochSchemaTests(unittest.TestCase):
    def test_strict_immutable_round_trip(self):
        value = epoch()
        self.assertEqual(RestartEpoch.from_dict(value.to_dict()), value)
        self.assertIs(Epoch, RestartEpoch)
        with self.assertRaises(FrozenInstanceError):
            value.pid1_start_ticks = 2
        for changes in ({'unexpected': True}, {'schema_version': 2}, {'schema_version': True}):
            with self.assertRaises(ContractError):
                RestartEpoch.from_dict({**value.to_dict(), **changes})
        missing = value.to_dict(); del missing['boot_id']
        with self.assertRaises(ContractError):
            RestartEpoch.from_dict(missing)

    def test_unknown_short_hostname_or_untrusted_container_identifier_rejected(self):
        for value in (None, '', 'a' * 12, 'a' * 63, '0' * 64, 'A' * 64,
                      'docker://' + CONTAINER_ID, 'container-name', CONTAINER_ID + '\n', True):
            with self.subTest(value=value), self.assertRaises(ContractError):
                epoch(container_id=value)

    def test_boot_id_must_be_canonical_known_and_nonzero(self):
        for value in (None, '', 'unknown', '00000000-0000-0000-0000-000000000000',
                      BOOT_ID.upper(), BOOT_ID + '\n', BOOT_ID.replace('-', ''), True):
            with self.subTest(value=value), self.assertRaises(ContractError):
                epoch(boot_id=value)

    def test_namespace_and_start_tick_fields_are_positive_bounded_integers(self):
        for field in ('pid_namespace_inode', 'pid1_start_ticks'):
            for value in (None, 0, -1, True, 1.0, '100', MAX_U64 + 1):
                with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                    epoch(**{field: value})
            self.assertEqual(getattr(epoch(**{field: MAX_U64}), field), MAX_U64)


class BoundaryComparisonTests(unittest.TestCase):
    def setUp(self):
        self.before = epoch()
        self.after = epoch(pid_namespace_inode=4026533002, pid1_start_ticks=2000)

    def candidate(self, before=None, after=None, **changes):
        options = dict(expected_container_id=CONTAINER_ID, deployment_contract_verified=True)
        options.update(changes)
        return restart_boundary_candidate(self.before if before is None else before,
                                          self.after if after is None else after, **options)

    def test_later_pid1_under_same_container_and_boot_is_candidate(self):
        self.assertTrue(self.candidate())
        # New namespace numbers need not be numerically greater.
        self.assertTrue(self.candidate(after=replace(self.after, pid_namespace_inode=10)))

    def test_reused_inode_with_later_pid1_requires_same_verified_container_contract(self):
        after = replace(self.before, pid1_start_ticks=2000)
        self.assertTrue(self.candidate(after=after))
        for flag in (False, None, 1, 'verified'):
            self.assertFalse(self.candidate(after=after, deployment_contract_verified=flag))
        self.assertFalse(self.candidate(after=replace(after, container_id='b' * 64)))
        self.assertFalse(self.candidate(after=replace(after, boot_id=OTHER_BOOT)))
        self.assertFalse(self.candidate(after=after, expected_container_id='b' * 64))

    def test_external_deployment_contract_is_required_and_never_inferred(self):
        self.assertFalse(restart_boundary_candidate(self.before, self.after, expected_container_id=CONTAINER_ID))
        for flag in (False, None, 1, 'verified', {'trusted': True}):
            self.assertFalse(self.candidate(deployment_contract_verified=flag))

    def test_same_epoch_exec_namespace_only_and_nonnewer_birth_never_suffice(self):
        # Exec preserves the PID 1 birth time, even when its executable changes.
        for after in (self.before, replace(self.before, pid1_start_ticks=999),
                      replace(self.before, pid_namespace_inode=4026533002),
                      replace(self.after, pid1_start_ticks=999)):
            self.assertFalse(self.candidate(after=after))

    def test_container_recreation_foreign_volume_or_reboot_is_rejected(self):
        self.assertFalse(self.candidate(after=replace(self.after, container_id='b' * 64)))
        self.assertFalse(self.candidate(expected_container_id='b' * 64))
        self.assertFalse(self.candidate(after=replace(self.after, boot_id=OTHER_BOOT)))
        self.assertFalse(self.candidate(before=replace(self.before, boot_id=OTHER_BOOT)))

    def test_missing_or_untyped_evidence_never_becomes_a_candidate(self):
        for before, after in ((None, self.after), (self.before, None),
                              (self.before.to_dict(), self.after), (self.before, {})):
            with self.assertRaises(ContractError):
                restart_boundary_candidate(before, after, expected_container_id=CONTAINER_ID,
                                           deployment_contract_verified=True)
        with self.assertRaises(ContractError):
            self.candidate(expected_container_id='a' * 12)


class EpochCollectionTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        # Any accidental live observation fails the test immediately.
        self.open_guard = patch('research_harness.restart_boundary.os.open', side_effect=AssertionError('no real syscalls'))
        self.stat_guard = patch('research_harness.restart_boundary.os.stat', side_effect=AssertionError('no real syscalls'))
        self.pid_guard = patch('research_harness.restart_boundary.os.getpid', side_effect=AssertionError('no real syscalls'))
        for guard in (self.open_guard, self.stat_guard, self.pid_guard):
            guard.start(); self.addCleanup(guard.stop)

    def collect(self):
        return read_epoch(container_id=CONTAINER_ID, reader=self.reader)

    def test_fixed_metadata_yields_two_matching_complete_observations(self):
        self.assertEqual(self.collect(), epoch())
        expected = [('pid',), ('read', BOOT_ID_PATH, 64), ('read', PID1_STAT_PATH, MAX_STAT_BYTES),
                    ('inode', PID1_NAMESPACE_PATH), ('inode', SELF_NAMESPACE_PATH)]
        self.assertEqual(self.reader.calls, expected * 2)
        self.assertNotIn('/etc/hostname', str(self.reader.calls))

    def test_default_reader_is_injectable_without_invoking_live_procfs(self):
        with patch('research_harness.restart_boundary._ProcReader', return_value=self.reader):
            self.assertEqual(read_epoch(container_id=CONTAINER_ID), epoch())

    def test_manager_must_actually_be_pid1_and_share_its_namespace(self):
        for value in (0, 2, True, '1', None):
            self.reader.manager_pid = value
            with self.assertRaises(ContractError):
                self.collect()
        self.reader.manager_pid = 1
        self.reader.inodes[SELF_NAMESPACE_PATH] = 4026533999
        with self.assertRaises(ContractError):
            self.collect()

    def test_stat_parser_handles_spaces_and_parentheses_in_comm(self):
        for name in (b'manager ) name', b'(nested)', b'a b', b'name\nline'):
            self.reader.bytes[PID1_STAT_PATH] = proc_stat(name=name)
            self.assertEqual(self.collect().pid1_start_ticks, 1000)

    def test_incomplete_wrong_process_dead_or_unbounded_stat_is_rejected(self):
        invalid = [b'', b'1 (manager) S 0', b'x' * (MAX_STAT_BYTES + 1),
                   proc_stat(pid=2), proc_stat(parent=1), proc_stat(ticks=0),
                   proc_stat(ticks=-1), proc_stat(ticks=MAX_U64 + 1),
                   proc_stat(state=b'Z'), proc_stat(state=b'X'), proc_stat(state=b'?'),
                   proc_stat(name=b'x' * 16), 'not bytes',
                   proc_stat().replace(b' 1000 ', b' NaN ')]
        for raw in invalid:
            self.reader.bytes[PID1_STAT_PATH] = raw
            with self.subTest(raw=raw), self.assertRaises(ContractError):
                self.collect()

    def test_unknown_boot_or_namespace_metadata_is_rejected(self):
        for raw in (b'', b'unknown\n', BOOT_ID.encode() + b'\n\n', b'x' * 65, b'\xff', BOOT_ID):
            self.reader.bytes[BOOT_ID_PATH] = raw
            with self.assertRaises(ContractError):
                self.collect()
        self.reader.bytes[BOOT_ID_PATH] = BOOT_ID.encode()
        for value in (None, True, 0, -1, '4026533001'):
            self.reader.inodes[PID1_NAMESPACE_PATH] = value
            with self.assertRaises(ContractError):
                self.collect()

    def test_torn_boot_namespace_and_start_observations_are_rejected(self):
        for change in ('boot', 'namespace', 'ticks'):
            with self.subTest(change=change):
                self.reader = FakeReader()
                observations = [0]
                def mutate(kind, path):
                    if kind != 'pid':
                        return
                    observations[0] += 1
                    if observations[0] != 2:
                        return
                    if change == 'boot':
                        self.reader.bytes[BOOT_ID_PATH] = OTHER_BOOT.encode()
                    elif change == 'namespace':
                        self.reader.inodes = {PID1_NAMESPACE_PATH: 4026533002, SELF_NAMESPACE_PATH: 4026533002}
                    else:
                        self.reader.bytes[PID1_STAT_PATH] = proc_stat(ticks=2000)
                self.reader.callback = mutate
                with self.assertRaises(ContractError):
                    self.collect()

    def test_missing_required_observation_has_no_partial_epoch_fallback(self):
        del self.reader.bytes[BOOT_ID_PATH]
        with self.assertRaises(ContractError):
            self.collect()
        self.reader = FakeReader()
        del self.reader.inodes[PID1_NAMESPACE_PATH]
        with self.assertRaises(ContractError):
            self.collect()

    def test_missing_or_unreadable_metadata_is_sanitized_without_fallback(self):
        def unavailable(kind, path):
            raise OSError('private path or credential-like error must not be repeated')
        self.reader.callback = unavailable
        with self.assertRaises(ContractError) as caught:
            self.collect()
        self.assertNotIn('private path', str(caught.exception))
        with self.assertRaises(ContractError):
            read_epoch(container_id=CONTAINER_ID, reader=object())
        with self.assertRaises(ContractError):
            read_epoch(container_id='a' * 12, reader=self.reader)


if __name__ == '__main__':
    unittest.main()
