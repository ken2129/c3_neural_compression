"""CPU-only ownership/failure tests; never use GPUs or arbitrary worker commands."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from research_harness import process


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'pidfd_open'),
                     'requires Linux /proc and pidfds')
class ProcessManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'processes'
        self.manager = process.ProcessManager(self.root)
        self.started = []

    def tearDown(self):
        # Receipts intentionally corrupted by tests are restored by those tests.
        for run_id in self.started:
            try:
                self.manager.cancel(run_id, grace=0.0)
            except (process.ProcessError, FileNotFoundError):
                pass
            child = self.manager._children.get(run_id)
            if child is not None:
                try:
                    child.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.fail('test fixture was not stopped')
        self.temp.cleanup()

    def launch(self, run_id='run', **kwargs):
        self.started.append(run_id)
        return self.manager.launch(run_id, **kwargs)

    def receipt_path(self, run_id='run'):
        return self.root / run_id / 'receipt.json'

    def overwrite(self, receipt, run_id='run'):
        self.receipt_path(run_id).write_text(json.dumps(receipt))

    def test_real_cpu_fixture_completion_and_bounded_log(self):
        first = self.launch()
        self.assertEqual(first['state'], 'RUNNING')
        self.assertEqual(first['identity']['pgrp'], first['identity']['pid'])
        self.assertIn('not a security boundary', first['isolation'])
        done = self.manager.wait('run')
        self.assertEqual(done['state'], 'EXITED')
        self.assertEqual(done['returncode'], 0)
        self.assertIn('cpu-fixture-complete', self.manager.logs('run'))
        self.assertIn('cpu-fixture-checksum=333283335000', self.manager.logs('run'))
        self.assertLessEqual((self.root / 'run' / 'fixture.log').stat().st_size,
                             process.MAX_LOG_BYTES)
        with mock.patch.object(process.subprocess, 'Popen') as popen:
            self.assertEqual(self.manager.launch('run'), done)
            popen.assert_not_called()

    def test_crash_is_observed_with_nonzero_exit(self):
        self.launch(mode='crash')
        result = self.manager.wait('run')
        self.assertEqual(result['state'], 'EXITED')
        self.assertEqual(result['returncode'], 7)

    def test_term_then_kill_for_ignoring_fixture(self):
        started = self.launch(mode='ignore_term', duration=10)
        result = self.manager.cancel('run', grace=0.02)
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertEqual([item['signal'] for item in result['signals']],
                         ['SIGTERM', 'SIGKILL'])
        self.assertEqual(process._scan_group(started['identity']['pgrp']), [])
        self.assertIn('identity', result)
        self.assertTrue(result['members'])  # receipts/ownership are never discarded

    def test_descendant_group_verified_stopped(self):
        started = self.launch(mode='descendant', duration=10)
        self.assertEqual(len(started['members']), 2)
        result = self.manager.cancel('run', grace=0.02)
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertEqual(process._scan_group(started['identity']['pgrp']), [])
        killed = {entry['pid'] for entry in result['signals'] if entry['signal'] == 'SIGKILL'}
        child_pid = next(member['pid'] for member in started['members']
                         if member['pid'] != started['identity']['pid'])
        self.assertIn(child_pid, killed)

    def test_timeout_is_enforced_and_group_readback(self):
        self.launch(mode='hang', duration=10, timeout=0.08)
        result = self.manager.wait('run')
        self.assertEqual(result['state'], 'TIMED_OUT')
        self.assertEqual(process._scan_group(result['identity']['pgrp']), [])

    def test_restart_reconciles_original_without_relaunch(self):
        original = self.launch(mode='hang', duration=10)
        other = process.ProcessManager(self.root)
        with mock.patch.object(process.subprocess, 'Popen') as popen:
            restored = other.launch('run')
            popen.assert_not_called()
        self.assertEqual(restored['identity'], original['identity'])
        self.assertEqual(restored['state'], 'RUNNING')
        self.assertEqual(other.cancel('run')['state'], 'CANCELLED')

    def test_intent_only_restart_is_blocked_no_signal_no_relaunch(self):
        directory = self.manager._directory('run', create=True)
        self.manager._write('run', {'version': 1, 'run_id': 'run',
                                   'state': 'LAUNCH_INTENT', 'identity': None,
                                   'members': []})
        with mock.patch.object(process.subprocess, 'Popen') as popen, \
                mock.patch.object(process.ProcessManager, '_signal_verified') as send:
            result = process.ProcessManager(self.root).launch('run')
            self.assertEqual(result['state'], 'BLOCKED')
            popen.assert_not_called()
            send.assert_not_called()
        self.assertTrue((directory / 'receipt.json').exists())

    def test_changed_pid_or_boot_never_signals_real_job(self):
        original = self.launch(mode='hang', duration=10)
        for field, value in [('start_ticks', original['identity']['start_ticks'] + 1),
                             ('boot_id', 'different-boot')]:
            changed = json.loads(json.dumps(original))
            changed['identity'][field] = value
            changed['members'][0][field] = value
            self.overwrite(changed)
            with mock.patch.object(process.ProcessManager, '_signal_verified') as send:
                result = self.manager.cancel('run')
                self.assertEqual(result['state'], 'BLOCKED')
                send.assert_not_called()
            self.assertIsNone(self.manager._children['run'].poll())
            self.overwrite(original)

    def test_unreadable_identity_is_blocked_without_signals(self):
        original = self.launch(mode='hang', duration=10)
        with mock.patch.object(process, '_read_process', side_effect=process.ProcessError('unknown')), \
                mock.patch.object(process.ProcessManager, '_signal_verified') as send:
            result = self.manager.cancel('run')
            self.assertEqual(result['state'], 'BLOCKED')
            send.assert_not_called()
        self.overwrite(original)

    def test_foreign_group_identity_cannot_be_cancelled(self):
        original = self.launch(mode='hang', duration=10)
        own = process._read_process(os.getpid())
        foreign = json.loads(json.dumps(original))
        foreign['identity'] = own
        foreign['members'] = [own]
        self.overwrite(foreign)
        with mock.patch.object(process.ProcessManager, '_signal_verified') as send:
            self.assertEqual(self.manager.cancel('run')['state'], 'BLOCKED')
            send.assert_not_called()
        self.overwrite(original)

    def test_pidfd_signal_rechecks_identity_before_signal(self):
        identity = {'pid': 9876, 'start_ticks': 10, 'pgrp': 9876,
                    'session': 9876, 'uid': os.getuid(), 'boot_id': 'boot'}
        reused = dict(identity, start_ticks=11, state='S')
        with mock.patch.object(process.os, 'pidfd_open', return_value=99), \
                mock.patch.object(process.os, 'close'), \
                mock.patch.object(process, '_read_process', return_value=reused), \
                mock.patch.object(process.signal, 'pidfd_send_signal') as send:
            with self.assertRaisesRegex(process.ProcessError, 'identity changed'):
                self.manager._signal_verified(identity, signal.SIGTERM)
            send.assert_not_called()

    def test_launch_intent_precedes_popen_and_clean_environment(self):
        real_popen = process.subprocess.Popen
        observed = []
        def inspect(args, **kwargs):
            receipt = json.loads(self.receipt_path().read_text())
            observed.append(receipt['state'])
            self.assertIsNone(receipt['identity'])
            self.assertEqual(args[1:3], ['-I', '-S'])
            self.assertEqual(kwargs['env'], {})
            self.assertTrue(kwargs['start_new_session'])
            return real_popen(args, **kwargs)
        with mock.patch.object(process.subprocess, 'Popen', side_effect=inspect):
            self.launch()
        self.assertEqual(observed, ['LAUNCH_INTENT'])
        self.manager.wait('run')

    def test_first_receipt_failure_launches_nothing(self):
        with mock.patch.object(self.manager, '_write', side_effect=OSError('disk full')), \
                mock.patch.object(process.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.manager.launch('run')
            popen.assert_not_called()

    def test_identity_receipt_failure_cleans_started_group(self):
        real_write = self.manager._write
        count = 0
        def failing(run_id, receipt):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('identity receipt write failed')
            return real_write(run_id, receipt)
        with mock.patch.object(self.manager, '_write', side_effect=failing):
            with self.assertRaisesRegex(OSError, 'identity receipt write failed'):
                self.launch(mode='descendant', duration=10)
        receipt = self.manager._read('run')
        self.assertEqual(receipt['state'], 'LAUNCH_FAILED')
        self.assertEqual(process._scan_group(receipt['identity']['pgrp']), [])

    def test_failed_receipt_replace_preserves_old_atomic_state(self):
        self.manager._directory('run', create=True)
        before = {'version': 1, 'run_id': 'run', 'state': 'LAUNCH_INTENT'}
        self.manager._write('run', before)
        with mock.patch.object(process.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.manager._write('run', dict(before, state='RUNNING'))
        self.assertEqual(self.manager._read('run')['state'], 'LAUNCH_INTENT')
        self.assertEqual(list((self.root / 'run').glob('.receipt-*')), [])

    def test_popen_failure_is_durable_and_no_retry(self):
        with mock.patch.object(process.subprocess, 'Popen', side_effect=OSError('spawn failed')):
            with self.assertRaisesRegex(OSError, 'spawn failed'):
                self.manager.launch('run')
        with mock.patch.object(process.subprocess, 'Popen') as popen:
            self.assertEqual(self.manager.launch('run')['state'], 'LAUNCH_FAILED')
            popen.assert_not_called()

    def test_one_active_fixture_per_root(self):
        self.launch(mode='hang', duration=10)
        other = process.ProcessManager(self.root)
        with mock.patch.object(process.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(process.ProcessError, 'active or unresolved'):
                other.launch('second')
            popen.assert_not_called()
        self.assertFalse((self.root / 'second').exists())

    def test_invalid_inputs_never_execute(self):
        for kwargs in [dict(mode='shell'), dict(timeout=float('nan')),
                       dict(duration=-1), dict(timeout=True), dict(duration=31)]:
            with mock.patch.object(process.subprocess, 'Popen') as popen:
                with self.assertRaises(process.ProcessError):
                    self.manager.launch('run', **kwargs)
                popen.assert_not_called()
        for run_id in ['../escape', '/tmp/escape', 'bad/run', '.', '']:
            with self.assertRaises(process.ProcessError):
                self.manager.launch(run_id)

    def test_symlink_paths_and_corrupt_receipts_fail_closed(self):
        (self.root / 'run').symlink_to(self.temp.name, target_is_directory=True)
        with self.assertRaises(process.ProcessError):
            self.manager.launch('run')
        (self.root / 'run').unlink()
        directory = self.manager._directory('run', create=True)
        (directory / 'receipt.json').write_text('{broken')
        with mock.patch.object(process.subprocess, 'Popen') as popen:
            with self.assertRaises(process.ProcessError):
                self.manager.launch('run')
            popen.assert_not_called()

    def test_last_admission_refusal_stops_before_fixture_work(self):
        # Do not acquire the manager's flock recursively in the callback.
        def checked_refuse(receipt):
            self.assertEqual(receipt['state'], 'RUNNING')
            durable = json.loads(self.receipt_path().read_text())
            self.assertEqual(durable['identity'], receipt['identity'])
            self.assertEqual((self.root / 'run' / 'fixture.log').read_text(), '')
            raise RuntimeError('approval revoked')
        with self.assertRaisesRegex(RuntimeError, 'approval revoked'):
            self.launch(before_release=checked_refuse)
        result = self.manager._read('run')
        self.assertEqual(result['state'], 'LAUNCH_FAILED')
        self.assertEqual(process._scan_group(result['identity']['pgrp']), [])
        self.assertNotIn('cpu-fixture-checksum', self.manager.logs('run'))

    def test_last_admission_callback_receives_independent_receipt_copy(self):
        seen = []
        def approve(receipt):
            seen.append(receipt['identity']['pid'])
            receipt['identity']['pid'] = -1
        launched = self.launch(before_release=approve)
        self.assertEqual(seen, [launched['identity']['pid']])
        self.assertGreater(launched['identity']['pid'], 0)
        self.assertEqual(self.manager.wait('run')['state'], 'EXITED')

    def test_cancel_restart_continues_stop_without_relaunch(self):
        original = self.launch(mode='ignore_term', duration=10)
        original.update(state='CANCELLING', stop_target='TIMED_OUT')
        self.overwrite(original)
        with mock.patch.object(process.subprocess, 'Popen') as popen:
            result = process.ProcessManager(self.root).reconcile('run')
            self.assertEqual(result['state'], 'TIMED_OUT')
            popen.assert_not_called()
        self.assertEqual(process._scan_group(original['identity']['pgrp']), [])

    def test_stop_unconfirmed_keeps_cancelling_and_ownership(self):
        original = self.launch(mode='hang', duration=10)
        with mock.patch.object(self.manager, '_terminate',
                               side_effect=process.StopUnconfirmed('not stopped')):
            result = self.manager.cancel('run')
        self.assertEqual(result['state'], 'CANCELLING')
        self.assertEqual(result['identity'], original['identity'])
        self.assertTrue(result['members'])
        self.assertEqual(self.manager.reconcile('run')['state'], 'CANCELLED')

    def test_unrelated_live_fixture_is_unchanged_by_cancel(self):
        # Separate manager root models another owner's job without touching any
        # existing system workload. Cancellation may affect only the first root.
        other = process.ProcessManager(Path(self.temp.name) / 'other')
        foreign = other.launch('other', mode='hang', duration=10)
        try:
            original = self.launch(mode='descendant', duration=10)
            log_before = other.logs('other')
            self.assertEqual(self.manager.cancel('run')['state'], 'CANCELLED')
            foreign_now = process._read_process(foreign['identity']['pid'])
            self.assertTrue(process._same_identity(foreign['identity'], foreign_now))
            self.assertTrue(process._live(foreign_now))
            self.assertEqual(other.logs('other'), log_before)
            self.assertEqual(process._scan_group(original['identity']['pgrp']), [])
        finally:
            other.cancel('other', grace=0.0)
            other._children['other'].wait(timeout=2)

    def test_crash_after_spawn_before_identity_receipt_stays_blocked(self):
        script = r"""
import json, os, sys
from pathlib import Path
from research_harness.process import ProcessManager
manager = ProcessManager(sys.argv[1])
original = manager._write
def crash(run_id, receipt):
    if receipt['state'] == 'RUNNING':
        Path(sys.argv[2]).write_text(json.dumps(receipt['members']))
        os._exit(72)
    original(run_id, receipt)
manager._write = crash
manager.launch('run', mode='hang', duration=10)
"""
        observed_path = Path(self.temp.name) / 'test-only-identities.json'
        dispatcher = subprocess.run([sys.executable, '-c', script, str(self.root),
                                     str(observed_path)], timeout=5, check=False)
        self.assertEqual(dispatcher.returncode, 72)
        with mock.patch.object(process.subprocess, 'Popen') as popen, \
                mock.patch.object(process.ProcessManager, '_signal_verified') as send:
            result = self.manager.launch('run')
            self.assertEqual(result['state'], 'BLOCKED')
            popen.assert_not_called()
            send.assert_not_called()
        # The fixed bootstrap sees the dead controller's pipe EOF and exits,
        # but reconciliation still refuses to infer permission for a new launch.
        identities = json.loads(observed_path.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            active = [identity for identity in identities
                      if (current := process._read_process(identity['pid']))
                      and process._same_identity(identity, current) and process._live(current)]
            if not active:
                break
            time.sleep(0.01)
        self.assertEqual(active, [])

    def test_saved_orphan_identity_remains_a_valid_group_anchor(self):
        leader = {'pid': 90001, 'start_ticks': 10, 'pgrp': 90001,
                  'session': 90001, 'uid': os.getuid(), 'boot_id': process._boot_id(), 'state': 'S'}
        orphan = dict(leader, pid=90002, start_ticks=11)
        receipt = {'identity': leader, 'members': [leader, orphan]}
        with mock.patch.object(process, '_read_process',
                               side_effect=lambda pid: orphan if pid == 90002 else None), \
                mock.patch.object(process, '_scan_group', return_value=[orphan]):
            self.assertEqual(self.manager._owned_group(receipt), [orphan])

    def test_missing_receipt_in_existing_run_never_relaunches(self):
        self.manager._directory('run', create=True)
        with mock.patch.object(process.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(process.ProcessError, 'missing or invalid'):
                self.manager.launch('run')
            popen.assert_not_called()

    def test_unknown_group_without_anchor_is_blocked(self):
        identity = {'pid': 90001, 'start_ticks': 10, 'pgrp': 90001,
                    'session': 90001, 'uid': os.getuid(), 'boot_id': process._boot_id(), 'state': 'S'}
        self.manager._directory('run', create=True)
        self.manager._write('run', {'version': 1, 'run_id': 'run', 'state': 'RUNNING',
                                   'identity': identity, 'members': [identity]})
        with mock.patch.object(process, '_read_process', return_value=None), \
                mock.patch.object(process, '_scan_group', return_value=[dict(identity, pid=90002)]), \
                mock.patch.object(process.ProcessManager, '_signal_verified') as send:
            self.assertEqual(self.manager.cancel('run')['state'], 'BLOCKED')
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
