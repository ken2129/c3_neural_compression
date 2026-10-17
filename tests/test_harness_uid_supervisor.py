"""Fake-OS lifecycle tests. No UID changes, signals, models or containers run."""
from dataclasses import replace
import os
import io
import json
import signal
import stat
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_harness.uid_supervisor import (
    CONTROLLER_UID, WORKER_UID, SETUID_SETGID, MAX_MEMORY,
    Identity, OwnedUIDSupervisor, Reservation, SupervisorBlocked, drain_process,
    CODEX_BINARY, CODEX_WORKSPACE, CODEX_SCHEMA, spawn_fixed_codex_child,
    validate_fixed_codex_request, PendingCodexResult,
    spawn_fixed_app_server_child,
)
from research_harness.codex_adapter import CodexAdapter, CliInspection
from research_harness.app_server_adapter import (
    AppServerBlocked, PendingAppServerResult, fixed_app_server_request,
)


class NamespaceExit(BaseException):
    pass


def worker(pid=10, state='S'):
    return Identity(pid, 1000 + pid, (WORKER_UID,) * 4, state)


class FakeOS:
    def __init__(self):
        self.ctx = dict(pid=1, resuid=(0, 0, 0), uids=(0, 0, 0, 0), threads=1,
                        proc_self='1', nnp=1, seccomp=2, cap_eff=SETUID_SETGID,
                        cap_prm=SETUID_SETGID, cap_bnd=SETUID_SETGID, cap_amb=0, cap_inh=0,
                        sigblk=0, sigign=0,
                        boot_id='boot-a', pid_namespace='pid:[123]', init_namespace='pid:[123]',
                        nspid=(1,), container_marker=True, dedicated_marker=True)
        self.limits = dict(memory=MAX_MEMORY, swap=0, pids=64, cpu_quota=50000,
                           cpu_period=100000, work_bytes=1024 * 1024)
        self.members = {1: Identity(1, 100, (0, 0, 0, 0), 'S')}
        self.ids = (0, 0, 0)
        self.events = []
        self.t = 0.0
        self.stubborn = False
        self.reused = False
        self.restore_fails = False
        self.result_code = 0
        self.result_stdout = b'333283335000\n'
        self.released = False
        self.poll_count = 0
        self.hang = False

    def context(self): return dict(self.ctx)
    def resources(self): return dict(self.limits)
    def process(self, pid): return self.members.get(pid)
    def processes(self): return list(self.members.values())
    def now(self): return self.t
    def sleep(self, seconds): self.t += seconds
    def reap(self): self.events.append(('reap',))
    def pidfd_open(self, pid, flags):
        self.events.append(('open', pid))
        if self.reused:
            self.members[pid] = replace(self.members[pid], start_ticks=99999)
        return pid + 100
    def close(self, fd): self.events.append(('close', fd))
    def seteuid(self, uid):
        self.events.append(('seteuid', uid))
        if uid == 0 and self.restore_fails:
            raise OSError('restore failed')
        self.ids = (0, uid, 0)
    def getresuid(self): return self.ids
    def signal(self, fd, sig):
        self.events.append(('signal', fd - 100, sig, self.ids))
        if not self.stubborn:
            self.members.pop(fd - 100, None)
    def exit_own_namespace(self, code):
        self.events.append(('namespace_exit', code))
        raise NamespaceExit(code)
    def arm_watchdog(self, seconds):
        self.events.append(('arm', seconds))
        return 'previous'
    def disarm_watchdog(self, previous): self.events.append(('disarm', previous))
    def launch_fixed(self):
        self.members[10] = worker()
        self.events.append(('launch_literal',))
        return SimpleNamespace(process=SimpleNamespace(pid=10))
    def ready(self, handle, timeout): return True
    def release(self, handle):
        self.events.append(('release',))
        self.released = True
    def poll(self, handle):
        self.poll_count += 1
        if self.hang or self.poll_count < 2:
            return None
        self.members.pop(10, None)
        return self.result_code
    def output(self, handle): return self.result_stdout, b''
    def close_child(self, handle): self.events.append(('close_child',))


class OwnedUIDSupervisorTest(unittest.TestCase):
    def setUp(self):
        self.os = FakeOS()
        self.supervisor = OwnedUIDSupervisor(self.os)

    def test_reserve_checks_actual_context_and_cannot_relaunch(self):
        receipt = self.supervisor.reserve()
        self.assertEqual(receipt, Reservation('boot-a', 'pid:[123]', 100))
        with self.assertRaises(SupervisorBlocked): self.supervisor.reserve()
        self.assertFalse(self.os.events)

    def test_host_shared_namespace_nonroot_threads_or_caps_are_rejected(self):
        cases = {'pid': 12, 'resuid': (1000, 1000, 1000), 'threads': 2, 'nnp': 0,
                 'seccomp': 0, 'cap_eff': SETUID_SETGID | (1 << 5),
                 'sigblk': 1 << (signal.SIGALRM - 1),
                 'sigign': 1 << (signal.SIGCHLD - 1),
                 'proc_self': '1234', 'container_marker': False, 'dedicated_marker': False,
                 'init_namespace': 'pid:[host]', 'nspid': (22,)}
        for key, value in cases.items():
            with self.subTest(key=key):
                backend = FakeOS(); backend.ctx[key] = value
                with self.assertRaises(SupervisorBlocked): OwnedUIDSupervisor(backend).reserve()
                self.assertEqual(backend.events, [])

    def test_resource_ceiling_and_preexisting_uid_are_rejected(self):
        for key, value in [('memory', MAX_MEMORY + 1), ('swap', 1), ('pids', 65),
                           ('cpu_quota', 200000), ('work_bytes', 10**10)]:
            backend = FakeOS(); backend.limits[key] = value
            with self.subTest(key=key), self.assertRaises(SupervisorBlocked):
                OwnedUIDSupervisor(backend).reserve()
        self.os.members[10] = worker()
        with self.assertRaises(SupervisorBlocked): self.supervisor.reserve()

    def test_all_owned_sessions_signaled_via_pidfds_with_root_restored(self):
        reservation = self.supervisor.reserve()
        self.os.members.update({10: worker(10), 20: worker(20)})
        result = self.supervisor.stop_owned(reservation)
        sent = [event for event in self.os.events if event[0] == 'signal']
        self.assertEqual({event[1] for event in sent}, {10, 20})
        self.assertTrue(all(event[3] == (0, WORKER_UID, 0) for event in sent))
        self.assertEqual(self.os.ids, (0, 0, 0))
        self.assertEqual(result['cleanup'], 'owned_uid_empty_observed')
        self.assertFalse(result['allocation_empty_verified'])

    def test_pinned_controller_is_never_a_signal_target(self):
        self.os.members[3] = Identity(3, 103, (CONTROLLER_UID,) * 4, 'S')
        supervisor = OwnedUIDSupervisor(self.os, controller_pid=3)
        reservation = supervisor.reserve()
        self.os.members[10] = worker()
        supervisor.stop_owned(reservation)
        self.assertIn(3, self.os.members)
        self.assertNotIn(3, [e[1] for e in self.os.events if e[0] == 'signal'])

    def test_reused_pid_never_signaled_and_own_namespace_exits(self):
        reservation = self.supervisor.reserve()
        self.os.members[10] = worker(); self.os.reused = True
        with self.assertRaises(NamespaceExit): self.supervisor.stop_owned(reservation)
        self.assertFalse(any(e[0] == 'signal' for e in self.os.events))
        self.assertIn(('close', 110), self.os.events)

    def test_unknown_uid_or_changed_context_fails_closed_after_reservation(self):
        for changed in ('peer', 'context'):
            backend = FakeOS(); supervisor = OwnedUIDSupervisor(backend)
            reservation = supervisor.reserve()
            if changed == 'peer':
                backend.members[10] = Identity(10, 1010, (9999,) * 4, 'S')
            else:
                backend.ctx['boot_id'] = 'changed'
            with self.subTest(changed=changed), self.assertRaises(NamespaceExit):
                supervisor.stop_owned(reservation)
            self.assertFalse(any(e[0] == 'signal' for e in backend.events))

    def test_bad_reservation_cannot_authorize_namespace_exit(self):
        reservation = self.supervisor.reserve()
        with self.assertRaises(SupervisorBlocked):
            self.supervisor.stop_owned(replace(reservation))
        self.assertFalse(any(e[0] == 'namespace_exit' for e in self.os.events))

    def test_restore_failure_and_stubborn_workers_exit_own_namespace(self):
        for attr in ('restore_fails', 'stubborn'):
            backend = FakeOS(); supervisor = OwnedUIDSupervisor(backend)
            reservation = supervisor.reserve(); backend.members[10] = worker()
            setattr(backend, attr, True)
            with self.subTest(attr=attr), self.assertRaises(NamespaceExit):
                supervisor.stop_owned(reservation, grace_seconds=0, kill_seconds=0.02)
            self.assertTrue(any(e[0] == 'namespace_exit' for e in backend.events))

    def test_zombie_leader_does_not_establish_group_emptiness(self):
        reservation = self.supervisor.reserve()
        self.os.members[10] = worker(state='Z')
        self.supervisor.stop_owned(reservation)
        self.assertTrue(any(e[0] == 'signal' for e in self.os.events))

    def test_finish_allocation_requires_external_exit_readback(self):
        reservation = self.supervisor.reserve()
        with self.assertRaises(NamespaceExit): self.supervisor.finish_allocation(reservation)
        self.assertIn(('namespace_exit', 0), self.os.events)

    def test_fixed_literal_canary_checks_approval_before_release_and_during_run(self):
        checks = Mock()
        before = Mock(side_effect=lambda identity: self.assertFalse(self.os.released))
        result = self.supervisor.run_fixed_canary(checks, before_release=before)
        self.assertEqual(result.checksum, 333283335000)
        self.assertTrue(result.fixture_only)
        self.assertFalse(result.model_called)
        self.assertGreaterEqual(checks.call_count, 4)
        self.assertEqual(before.call_args.args[0].uids, (WORKER_UID,) * 4)
        self.assertIn(('close_child',), self.os.events)
        self.assertIn(('disarm', 'previous'), self.os.events)

    def test_revocation_before_release_cleans_worker_without_release(self):
        checks = Mock(side_effect=[None, SupervisorBlocked('revoked')])
        with self.assertRaisesRegex(SupervisorBlocked, 'revoked'):
            self.supervisor.run_fixed_canary(checks)
        self.assertFalse(self.os.released)
        self.assertNotIn(10, self.os.members)

    def test_fixed_deadline_and_wrong_checksum_are_failures(self):
        for condition in ('hang', 'bad_checksum'):
            backend = FakeOS(); supervisor = OwnedUIDSupervisor(backend)
            if condition == 'hang': backend.hang = True
            else: backend.result_stdout = b'7\n'
            with self.subTest(condition=condition), self.assertRaises(SupervisorBlocked):
                supervisor.run_fixed_canary(lambda: None, wall_seconds=0.02)
            self.assertNotIn(10, backend.members)


class BoundedDrainTest(unittest.TestCase):
    def fake_process(self, stdout=b'out', stderr=b'err', *, open_writer=False):
        streams, writes = [], []
        for data in (stdout, stderr):
            read, write = os.pipe()
            os.write(write, data)
            streams.append(os.fdopen(read, 'rb'))
            if open_writer: writes.append(write)
            else: os.close(write)
        process = SimpleNamespace(args=('fixed',), stdout=streams[0], stderr=streams[1], poll=lambda: 0)
        self.addCleanup(lambda: [stream.close() for stream in streams])
        self.addCleanup(lambda: [os.close(fd) for fd in writes])
        return process

    def test_combined_streams_are_collected_and_cleanup_always_runs(self):
        stop = Mock(); recheck = Mock()
        result = drain_process(self.fake_process(), recheck=recheck, stop_owned=stop,
                               deadline=10, clock=lambda: 0)
        self.assertEqual((result.stdout, result.stderr), (b'out', b'err'))
        stop.assert_called_once(); self.assertGreater(recheck.call_count, 0)

    def test_combined_output_overflow_stops_all_owned_processes(self):
        stop = Mock()
        with self.assertRaisesRegex(SupervisorBlocked, 'combined'):
            drain_process(self.fake_process(b'abcd', b'efgh'), recheck=lambda: None,
                          stop_owned=stop, deadline=10, clock=lambda: 0, max_bytes=7)
        stop.assert_called_once()

    def test_cancel_timeout_and_cleanup_failure_never_return_success(self):
        stop = Mock()
        with self.assertRaisesRegex(SupervisorBlocked, 'cancel'):
            drain_process(self.fake_process(), recheck=Mock(side_effect=SupervisorBlocked('cancel')),
                          stop_owned=stop, deadline=10, clock=lambda: 0)
        stop.assert_called_once()
        with self.assertRaisesRegex(SupervisorBlocked, 'deadline'):
            drain_process(self.fake_process(open_writer=True), recheck=lambda: None,
                          stop_owned=lambda: None, deadline=0, clock=lambda: 1)
        with self.assertRaisesRegex(SupervisorBlocked, 'unconfirmed'):
            drain_process(self.fake_process(), recheck=lambda: None,
                          stop_owned=Mock(side_effect=SupervisorBlocked('unconfirmed')),
                          deadline=10, clock=lambda: 0)


class FixedCodexCandidateTest(unittest.TestCase):
    """Simulated deployment admission and process only; not live gate evidence."""

    def setUp(self):
        self.request = CodexAdapter(CODEX_BINARY).build_canary_request(
            inspection=CliInspection('0.160.0', '0.160.0', True, ()),
            workspace=CODEX_WORKSPACE, schema_path=CODEX_SCHEMA,
            model='configured-model-v1')
        self.backend = FakeOS()
        def launch(request):
            self.backend.members[10] = worker()
            self.backend.events.append(('launch_codex',))
            return SimpleNamespace(process=SimpleNamespace(pid=10, args=request.argv))
        self.backend.launch_codex = Mock(side_effect=launch)
        self.backend.send_prompt = Mock()
        self.supervisor = OwnedUIDSupervisor(self.backend)
        payload = dict(schema_version='c3-cpu-canary-worker-v1',
                       outcome='success', reported_checksum=7)
        output = [dict(type='item.completed', item=dict(type='agent_message', text=json.dumps(payload))),
                  dict(type='turn.completed')]
        self.output = b'\n'.join(json.dumps(item).encode() for item in output)

    def fake_drain(self, process, **kwargs):
        kwargs['recheck']()
        kwargs['stop_owned']()
        return subprocess.CompletedProcess(process.args, 0, self.output, b'')

    def test_disabled_or_unconfigured_admission_never_spawns(self):
        for kwargs, message in [({}, 'DISABLED'), ({'enabled': True}, 'NOT_CONFIGURED'),
                                ({'enabled': True, 'admit': True}, 'NOT_CONFIGURED'),
                                ({'enabled': True, 'admit': lambda request: True}, 'flag')]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(SupervisorBlocked, message):
                self.supervisor.run_fixed_codex_canary(self.request, lambda: None, **kwargs)
        self.backend.launch_codex.assert_not_called()
        self.assertFalse(self.backend.events)

    def test_missing_sandbox_or_billing_admission_stops_before_reservation(self):
        for blocker in ('NORMAL_SANDBOX_NEGATIVE_TEST_MISSING', 'BILLING_RISK_NOT_APPROVED'):
            with self.subTest(blocker=blocker), self.assertRaisesRegex(SupervisorBlocked, blocker):
                self.supervisor.run_fixed_codex_canary(self.request, lambda: None, enabled=True,
                    admit=Mock(side_effect=SupervisorBlocked(blocker)))
        self.backend.launch_codex.assert_not_called()
        self.assertFalse(self.backend.events)

    def test_tampered_request_rejected_before_any_launch(self):
        for request in (replace(self.request, stdin=b'arbitrary'),
                        replace(self.request, argv=self.request.argv + ('--full-auto',)),
                        replace(self.request, cwd='/control'),
                        replace(self.request, schema_path='/work/schema'),
                        replace(self.request, wall_seconds=121),
                        replace(self.request, max_capture_bytes=10**9)):
            with self.subTest(request=request), self.assertRaises(SupervisorBlocked):
                validate_fixed_codex_request(request)
        self.assertEqual(validate_fixed_codex_request(self.request), self.request)

    def test_success_is_only_candidate_pending_restart_and_watchdog_remains(self):
        admission = Mock(return_value=None)
        before = Mock(side_effect=lambda identity: self.assertFalse(self.backend.released))
        with patch('research_harness.uid_supervisor.drain_process', side_effect=self.fake_drain):
            result = self.supervisor.run_fixed_codex_canary(
                self.request, lambda: None, enabled=True, admit=admission, before_release=before)
        self.assertIsInstance(result, PendingCodexResult)
        self.assertEqual(result.candidate.reported_checksum, 7)
        self.assertFalse(result.candidate.independently_validated)
        self.assertTrue(result.requires_namespace_restart)
        self.assertFalse(result.cleanup_verified)
        admission.assert_called_once_with(self.request)
        self.backend.send_prompt.assert_called_once()
        self.assertEqual(self.backend.send_prompt.call_args.args[1], self.request.stdin)
        self.assertEqual(before.call_args.args[0].uids, (WORKER_UID,) * 4)
        self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))
        with self.assertRaisesRegex(SupervisorBlocked, 'already reserved'):
            self.supervisor.run_fixed_codex_canary(self.request, lambda: None,
                                                 enabled=True, admit=lambda request: None)

    def test_cancellation_after_spawn_cleans_and_preserves_watchdog(self):
        checks = Mock(side_effect=[None, None, None, SupervisorBlocked('revoked')])
        with self.assertRaisesRegex(SupervisorBlocked, 'revoked'):
            self.supervisor.run_fixed_codex_canary(self.request, checks, enabled=True,
                                                 admit=lambda request: None)
        self.assertNotIn(10, self.backend.members)
        self.assertFalse(self.backend.released)
        self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))

    def test_post_cleanup_cancellation_or_deadline_prevents_pending_result(self):
        checks = Mock(side_effect=[None] * 5 + [SupervisorBlocked('late revocation')])
        with patch('research_harness.uid_supervisor.drain_process', side_effect=self.fake_drain):
            with self.assertRaisesRegex(SupervisorBlocked, 'late revocation'):
                self.supervisor.run_fixed_codex_canary(self.request, checks, enabled=True,
                                                     admit=lambda request: None)
        self.assertNotIn(10, self.backend.members)
        self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))
        self.setUp()
        def late_drain(process, **kwargs):
            result = self.fake_drain(process, **kwargs)
            self.backend.t = 120
            return result
        with patch('research_harness.uid_supervisor.drain_process', side_effect=late_drain):
            with self.assertRaisesRegex(SupervisorBlocked, 'cleanup deadline'):
                self.supervisor.run_fixed_codex_canary(self.request, lambda: None, enabled=True,
                                                     admit=lambda request: None)

    def test_spawn_builds_fixed_bootstrap_uid_env_and_argv_without_running(self):
        with patch('research_harness.uid_supervisor.os.getpid', return_value=1), \
             patch('research_harness.uid_supervisor.os.getresuid', return_value=(0, 0, 0)), \
             patch('research_harness.uid_supervisor.os.fstat',
                   return_value=SimpleNamespace(st_mode=stat.S_IFIFO)), \
             patch('research_harness.uid_supervisor.subprocess.Popen') as popen:
            child = spawn_fixed_codex_child(self.request, gate_read=8, ready_write=9)
        args, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(kwargs['user'], WORKER_UID)
        self.assertEqual(kwargs['group'], WORKER_UID)
        self.assertEqual(kwargs['extra_groups'], [])
        self.assertEqual(kwargs['env'], {})
        self.assertEqual(kwargs['cwd'], '/')
        self.assertEqual(kwargs['pass_fds'], (8, 9))
        self.assertEqual(tuple(args[7:]), self.request.argv)
        self.assertIn("os.chdir('/work/canary')", args[4])
        self.assertIn("'CODEX_HOME':'/work/codex-home'", args[4])
        self.assertIn("'GIT_CONFIG_GLOBAL':'/dev/null'", args[4])
        self.assertIn("'.git/config':", args[4])
        self.assertIn('info.st_nlink != 1', args[4])
        self.assertNotIn('GITHUB', args[4])
        self.assertIs(child._c3_fixed_request, self.request)


class FixedAppServerSupervisorTest(unittest.TestCase):
    def setUp(self):
        from tests.test_harness_app_server_adapter import FakeTransport
        self.request = fixed_app_server_request('configured-model-v1')
        self.backend = FakeOS()
        self.supervisor = OwnedUIDSupervisor(self.backend)
        self.transport = FakeTransport()
        self.transport.close = Mock()
        def launch(request):
            self.backend.members[10] = worker()
            self.backend.events.append(('launch_app_server',))
            return SimpleNamespace(process=SimpleNamespace(pid=10, args=request.argv))
        self.backend.launch_app_server = Mock(side_effect=launch)

    def test_disabled_missing_or_boolean_external_admission_never_spawns(self):
        for kwargs in ({}, {'enabled': True}, {'enabled': True, 'admit_external': True},
                       {'enabled': True, 'admit_external': lambda request: True}):
            with self.subTest(kwargs=kwargs), self.assertRaises((SupervisorBlocked, AppServerBlocked)):
                self.supervisor.run_fixed_app_server_canary(self.request, lambda: None, **kwargs)
        self.backend.launch_app_server.assert_not_called()
        self.assertFalse(self.backend.events)

    def test_full_fake_protocol_is_connected_to_gated_uid_lifecycle(self):
        admit = Mock(return_value=None)
        before = Mock(side_effect=lambda identity: self.assertFalse(self.backend.released))
        with patch('research_harness.uid_supervisor.BoundedStdioTransport', return_value=self.transport):
            result = self.supervisor.run_fixed_app_server_canary(self.request, lambda: None,
                enabled=True, admit_external=admit, before_release=before)
        self.assertIsInstance(result, PendingAppServerResult)
        self.assertEqual(result.command_checksum, 333283335000)
        self.assertEqual(result.command_exit_code, 0)
        self.assertTrue(result.requires_namespace_restart)
        self.assertFalse(result.cleanup_verified)
        self.assertFalse(result.candidate.independently_validated)
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)
        self.assertLess(result.elapsed_seconds, 120)
        self.assertEqual(admit.call_count, 1)
        before.assert_called_once()
        self.assertNotIn(10, self.backend.members)
        self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))
        self.transport.close.assert_called_once()
        self.assertIn(('close_child',), self.backend.events)

    def test_protocol_failure_cleans_owned_workers_and_preserves_watchdog(self):
        self.transport.receive = Mock(side_effect=AppServerBlocked('bad protocol'))
        with patch('research_harness.uid_supervisor.BoundedStdioTransport', return_value=self.transport):
            with self.assertRaisesRegex(AppServerBlocked, 'bad protocol'):
                self.supervisor.run_fixed_app_server_canary(self.request, lambda: None,
                    enabled=True, admit_external=lambda request: None)
        self.assertNotIn(10, self.backend.members)
        self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))
        self.transport.close.assert_called_once()

    def test_app_server_reserves_twenty_seconds_without_extending_hard_alarm(self):
        self.backend.t = 27.0
        with patch('research_harness.uid_supervisor.BoundedStdioTransport',
                   return_value=self.transport) as transport:
            result = self.supervisor.run_fixed_app_server_canary(self.request, lambda: None,
                enabled=True, admit_external=lambda request: None)
        self.assertEqual(transport.call_args.kwargs['deadline'], 127.0)
        self.assertEqual([event for event in self.backend.events if event[0] == 'arm'],
                         [('arm', 124)])
        self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))
        self.assertTrue(result.requires_namespace_restart)

    def test_insufficient_finalization_budget_is_rejected_before_admission(self):
        # The canonical request remains 120 seconds. An excessive protected
        # reserve must fail closed rather than create a nonpositive model budget.
        for reserve in (120, 121):
            admit = Mock(return_value=None)
            with self.subTest(reserve=reserve), patch(
                    'research_harness.uid_supervisor.APP_SERVER_FINALIZATION_RESERVE_SECONDS',
                    reserve), self.assertRaisesRegex(SupervisorBlocked, 'reserve exceeds'):
                self.supervisor.run_fixed_app_server_canary(self.request, lambda: None,
                    enabled=True, admit_external=admit)
            admit.assert_not_called()
        self.backend.launch_app_server.assert_not_called()
        self.assertFalse(self.backend.events)

    def test_soft_startup_deadline_prevents_release_after_slow_barrier_or_callback(self):
        for slow in ('waiting', 'ready', 'before_release'):
            with self.subTest(slow=slow):
                self.setUp()
                before = None
                if slow == 'before_release':
                    before = lambda identity: setattr(self.backend, 't', 100.0)
                else:
                    def ready(handle, timeout):
                        self.assertLessEqual(timeout, 0.05)
                        self.backend.t = 100.0
                        return slow == 'ready'
                    self.backend.ready = ready
                with patch('research_harness.uid_supervisor.BoundedStdioTransport') as transport:
                    with self.assertRaisesRegex(SupervisorBlocked, 'deadline exceeded'):
                        self.supervisor.run_fixed_app_server_canary(self.request, lambda: None,
                            enabled=True, admit_external=lambda request: None, before_release=before)
                transport.assert_not_called()
                self.assertFalse(self.backend.released)
                self.assertNotIn(10, self.backend.members)
                self.assertIn(('arm', 124), self.backend.events)
                self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))
                self.assertIn(('close_child',), self.backend.events)

    def test_late_fake_transport_candidate_fails_and_cleans_at_soft_deadline(self):
        for completion_time in (100.0, 100.1):
            with self.subTest(completion_time=completion_time):
                self.setUp()
                original_receive = self.transport.receive
                def receive():
                    frame = original_receive()
                    if frame.get('method') == 'turn/completed':
                        self.backend.t = completion_time
                    return frame
                self.transport.receive = receive
                with patch('research_harness.uid_supervisor.BoundedStdioTransport',
                           return_value=self.transport):
                    with self.assertRaisesRegex(SupervisorBlocked, 'model deadline exceeded'):
                        self.supervisor.run_fixed_app_server_canary(self.request, lambda: None,
                            enabled=True, admit_external=lambda request: None)
                self.assertNotIn(10, self.backend.members)
                self.assertIn(('arm', 124), self.backend.events)
                self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))
                self.transport.close.assert_called_once()
                self.assertIn(('close_child',), self.backend.events)

    def test_near_soft_deadline_candidate_keeps_full_cleanup_and_postcheck_budget(self):
        original_receive = self.transport.receive
        def receive():
            frame = original_receive()
            if frame.get('method') == 'turn/completed':
                self.backend.t = 99.9
            return frame
        self.transport.receive = receive
        original_stop = self.supervisor.stop_owned
        def stop(reservation):
            self.assertGreaterEqual(120 - self.backend.t, 20)
            return original_stop(reservation)
        def recheck():
            if self.backend.released and 10 not in self.backend.members:
                self.backend.t = 119.9
        with patch('research_harness.uid_supervisor.BoundedStdioTransport',
                   return_value=self.transport), \
             patch.object(self.supervisor, 'stop_owned', side_effect=stop):
            result = self.supervisor.run_fixed_app_server_canary(self.request, recheck,
                enabled=True, admit_external=lambda request: None)
        self.assertEqual(result.elapsed_seconds, 119.9)
        self.assertTrue(result.requires_namespace_restart)
        self.assertFalse(result.cleanup_verified)
        self.assertIn(('arm', 124), self.backend.events)
        self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))

    def test_failure_diagnostic_is_stage_only_and_preserves_cleanup(self):
        secret = 'MODEL_PRIVATE_TOKEN_https://secret.example/path'
        for stage in ('admission', 'spawn', 'barrier', 'turn'):
            with self.subTest(stage=stage):
                self.setUp()
                error = PermissionError(secret)
                admission = Mock(return_value=None)
                if stage == 'admission': admission.side_effect = error
                if stage == 'spawn': self.backend.launch_app_server.side_effect = error
                if stage == 'barrier': self.backend.ready = Mock(side_effect=error)
                if stage == 'turn': self.transport.receive = Mock(side_effect=error)
                output = io.StringIO()
                with patch('research_harness.uid_supervisor.BoundedStdioTransport', return_value=self.transport), \
                     patch('research_harness.uid_supervisor.sys.stderr', output):
                    with self.assertRaises(PermissionError):
                        self.supervisor.run_fixed_app_server_canary(self.request, lambda: None,
                            enabled=True, admit_external=admission)
                self.assertEqual(json.loads(output.getvalue()), {
                    'phase':'RUNTIME_BLOCKED','role':'app_server','stage':stage,'error_class':'permission'})
                self.assertNotIn(secret, output.getvalue())
                self.assertNotIn(10, self.backend.members)

    def test_diagnostic_never_stringifies_unknown_exception(self):
        from research_harness.uid_supervisor import report_failure
        class PrivateError(Exception):
            def __str__(self): raise AssertionError('must not stringify')
        output = io.StringIO()
        with patch('research_harness.uid_supervisor.sys.stderr', output):
            report_failure('private role', 'private stage', PrivateError())
        self.assertEqual(json.loads(output.getvalue()), {
            'phase':'RUNTIME_BLOCKED','role':'unknown','stage':'unknown','error_class':'other'})

    def test_pre_post_turn_typed_recheck_failure_is_preserved(self):
        from research_harness.uid_supervisor import report_failure
        from research_harness.manager_runtime import RecheckRejected
        for stage in ('approval','barrier','postcheck'):
            error=RecheckRejected('expired',origin='manager')
            error.args=('SECRET',);error.reason='SECRET'
            output=io.StringIO()
            with patch('research_harness.uid_supervisor.sys.stderr',output):
                report_failure('app_server',stage,error)
            record=json.loads(output.getvalue())
            self.assertEqual(record['approval_failure'],{'reason':'expired','origin':'manager'})
            self.assertNotIn('SECRET',output.getvalue())
            self.assertNotIn('app_server',record)

    def test_diagnostic_observed_exit_is_bounded_numeric_only(self):
        from research_harness.uid_supervisor import report_failure
        for value in (-25, None, True, 'SECRET', -129, 256):
            output = io.StringIO()
            with patch('research_harness.uid_supervisor.sys.stderr', output):
                report_failure('app_server', 'turn', RuntimeError('SECRET'),
                               child_returncode=value)
            record = json.loads(output.getvalue())
            if value == -25:
                self.assertEqual(record['observed_child_returncode'], -25)
            else:
                self.assertNotIn('observed_child_returncode', record)
            self.assertNotIn('SECRET', output.getvalue())

    def test_post_cleanup_failure_never_reports_supervisor_induced_child_exit(self):
        for failure in ('revoked', 'deadline'):
            with self.subTest(failure=failure):
                self.setUp()
                poll = Mock(return_value=-signal.SIGTERM)
                original_launch = self.backend.launch_app_server.side_effect
                def launch(request):
                    handle = original_launch(request)
                    handle.process.poll = poll
                    return handle
                self.backend.launch_app_server.side_effect = launch
                original_stop = self.supervisor.stop_owned
                def stop(reservation):
                    result = original_stop(reservation)
                    if failure == 'deadline':
                        self.backend.t = 120
                    return result
                def recheck():
                    if (failure == 'revoked' and self.backend.released
                            and 10 not in self.backend.members):
                        raise SupervisorBlocked('post-cleanup revocation')
                output = io.StringIO()
                with patch('research_harness.uid_supervisor.BoundedStdioTransport', return_value=self.transport), \
                     patch.object(self.supervisor, 'stop_owned', side_effect=stop), \
                     patch('research_harness.uid_supervisor.sys.stderr', output):
                    with self.assertRaises(SupervisorBlocked):
                        self.supervisor.run_fixed_app_server_canary(self.request, recheck,
                            enabled=True, admit_external=lambda request: None)
                record = json.loads(output.getvalue())
                self.assertEqual(record['stage'], 'postcheck')
                self.assertNotIn('observed_child_returncode', record)
                poll.assert_not_called()
                self.assertTrue(any(event[0] == 'signal' for event in self.backend.events))
                self.assertNotIn(10, self.backend.members)
                self.assertFalse(any(event[0] == 'disarm' for event in self.backend.events))
                self.transport.close.assert_called_once()
                self.assertIn(('close_child',), self.backend.events)

    def test_protocol_failure_observes_child_exit_before_cleanup_only(self):
        def observe_exit():
            self.assertIn(10, self.backend.members)
            self.assertFalse(any(event[0] == 'signal' for event in self.backend.events))
            return -25
        poll = Mock(side_effect=observe_exit)
        original_launch = self.backend.launch_app_server.side_effect
        def launch(request):
            handle = original_launch(request)
            handle.process.poll = poll
            return handle
        self.backend.launch_app_server.side_effect = launch
        self.transport.receive = Mock(side_effect=AppServerBlocked('bad protocol'))
        output = io.StringIO()
        with patch('research_harness.uid_supervisor.BoundedStdioTransport', return_value=self.transport), \
             patch('research_harness.uid_supervisor.sys.stderr', output):
            with self.assertRaises(AppServerBlocked):
                self.supervisor.run_fixed_app_server_canary(self.request, lambda: None,
                    enabled=True, admit_external=lambda request: None)
        self.assertEqual(json.loads(output.getvalue())['observed_child_returncode'], -25)
        poll.assert_called_once()
        self.assertNotIn(10, self.backend.members)

    def test_diagnostic_exports_only_fixed_adapter_context(self):
        from research_harness.uid_supervisor import report_failure
        from research_harness.app_server_adapter import safe_app_server_diagnostic
        error = AppServerBlocked('invalid protocol object')
        expected = safe_app_server_diagnostic(error)
        error.args = ('SECRET-token-and-private-output',)
        error.protocol = {'secret': 'SECRET-token-and-private-output'}
        output = io.StringIO()
        with patch('research_harness.uid_supervisor.sys.stderr', output):
            report_failure('app_server', 'turn', error)
        record = json.loads(output.getvalue())
        self.assertEqual(record['app_server'], expected)
        self.assertNotIn('SECRET', output.getvalue())
        self.assertEqual(record['error_class'], 'contract')

    def test_fixed_app_server_spawn_has_auth_only_bootstrap_without_secret_reads(self):
        with patch('research_harness.uid_supervisor.os.getpid', return_value=1), \
             patch('research_harness.uid_supervisor.os.getresuid', return_value=(0, 0, 0)), \
             patch('research_harness.uid_supervisor.os.fstat',
                   return_value=SimpleNamespace(st_mode=stat.S_IFIFO)), \
             patch('research_harness.uid_supervisor.subprocess.Popen') as popen:
            spawn_fixed_app_server_child(self.request, gate_read=8, ready_write=9)
        args, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(tuple(args[7:]), self.request.argv)
        self.assertEqual(kwargs['user'], WORKER_UID)
        self.assertEqual(kwargs['env'], {})
        self.assertEqual(kwargs['cwd'], '/')
        self.assertIn("set(os.listdir('/work/codex-home')) != {'auth.json'}", args[4])
        self.assertNotIn('resource.RLIMIT_AS', args[4])
        self.assertIn('(resource.RLIMIT_FSIZE,16777216)', args[4])
        self.assertIn('(resource.RLIMIT_NPROC,64)', args[4])
        self.assertIn('(resource.RLIMIT_NOFILE,64)', args[4])
        self.assertEqual(self.request.max_capture_bytes, 1048576)
        self.assertIn('stat.S_IMODE(auth.st_mode) != 0o600', args[4])
        self.assertIn('auth.st_size <= 65536', args[4])
        self.assertNotIn("open('/work/codex-home/auth.json'", args[4])


if __name__ == '__main__':
    unittest.main()
