"""Restart gating integration with fixed synthetic epochs and offline adapters."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_harness.schema import ContractError, loads
from research_harness.service import Controller
from research_harness.service_state import ServiceState, initialize
from research_harness.intake_flow_fixture import FIXTURE_NOW
from research_harness.issue_intake import timestamp
from research_harness.issue_intake import MAX_TRACKED, SCAN_SECONDS
from test_harness_restart_boundary import epoch, OTHER_BOOT
from test_harness_service import FakeRuntime, ISSUE, PublisherFactory, ServiceSource, SOURCE_SHA


class EpochRuntime(FakeRuntime):
    # This explicit assertion applies only to synthetic test metadata.
    deployment_contract_verified = True

    def __init__(self):
        super().__init__()
        self.epoch = epoch()
        self.current_calls = 0

    def preflight(self):
        super().preflight()
        return self.epoch

    def current_epoch(self):
        self.current_calls += 1
        return self.epoch


class ServiceRestartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        initialize(self.root)
        self.state = ServiceState(self.root)
        self.addCleanup(lambda: self.state.close())
        self.source = ServiceSource()
        self.runtime = EpochRuntime()
        self.publishers = PublisherFactory()
        self.now = FIXTURE_NOW
        self.make_controller()

    def make_controller(self):
        self.controller = Controller(self.state, self.source, self.source, self.runtime, self.publishers,
                                     source_sha=SOURCE_SHA, clock=lambda: self.now)

    def reopen(self):
        self.state.close()
        self.state = ServiceState(self.root)
        self.make_controller()

    def newer_epoch(self):
        return epoch(pid_namespace_inode=4026533002, pid1_start_ticks=2000)

    def saved_epoch(self):
        row = self.state.db.execute('SELECT before_epoch,after_epoch FROM service_epochs WHERE issue=?', (ISSUE,)).fetchone()
        return tuple(loads(value) if value else None for value in row)

    def await_restart(self):
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'RESULT_AWAITING_RESTART')
        self.assertTrue(report['restart_required'])
        self.assertTrue(report['dispatched'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)
        return report

    def test_valid_result_is_saved_but_never_published_in_execution_epoch(self):
        self.await_restart()
        saved = self.state.get(ISSUE)
        self.assertEqual(saved['phase'], 'RESULT_AWAITING_RESTART')
        self.assertIsNotNone(saved['result'])
        self.assertIsNone(saved['publication'])
        self.assertEqual(self.saved_epoch(), (epoch().to_dict(), None))
        self.assertEqual(self.runtime.current_calls, 0)

    def test_same_epoch_reopen_never_publishes_or_recomputes(self):
        self.await_restart()
        self.reopen()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'RESULT_AWAITING_RESTART')
        self.assertEqual(self.saved_epoch()[1], None)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.runtime.preflights, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_same_epoch_poll_report_stays_pending_without_new_dispatch(self):
        self.await_restart()
        report = self.controller.once()[0]
        self.assertEqual(report['phase'], 'RESULT_AWAITING_RESTART')
        self.assertEqual(report['dispatched'], 'unknown')
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_only_new_epoch_can_publish_saved_result_without_reexecuting(self):
        self.await_restart()
        self.runtime.epoch = self.newer_epoch()
        self.reopen()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertFalse(report['dispatched'])
        self.assertEqual(report['publication_phase'], 'PUBLISHED')
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.runtime.preflights, 1)
        self.assertEqual(self.publishers.transport.events, ['branch', 'content', 'pr'])
        self.assertEqual(self.saved_epoch(), (epoch().to_dict(), self.newer_epoch().to_dict()))
        self.reopen()
        historical = self.controller.process(ISSUE)
        self.assertTrue(historical['historical'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 1)

    def test_foreign_container_reboot_partial_epoch_change_and_backward_time_rejected(self):
        self.await_restart()
        changed = [replace(self.newer_epoch(), container_id='f' * 64),
                   replace(self.newer_epoch(), boot_id=OTHER_BOOT),
                   replace(epoch(), pid1_start_ticks=2000),
                   replace(epoch(), pid_namespace_inode=4026533002),
                   replace(self.newer_epoch(), pid1_start_ticks=999)]
        for value in changed:
            with self.subTest(epoch=value):
                self.runtime.epoch = value
                self.reopen()
                with self.assertRaises(ContractError):
                    self.controller.process(ISSUE)
                self.assertEqual(self.state.get(ISSUE)['phase'], 'RESULT_AWAITING_RESTART')
                self.assertIsNone(self.saved_epoch()[1])
                self.assertEqual(self.runtime.runs, 1)
                self.assertEqual(self.publishers.calls, 0)

    def test_external_contract_never_inferred_from_a_new_epoch(self):
        self.await_restart()
        self.runtime.epoch = self.newer_epoch()
        for verified in (False, None, 1, 'verified'):
            self.runtime.deployment_contract_verified = verified
            with self.assertRaises(ContractError):
                self.controller.process(ISSUE)
            self.assertEqual(self.state.get(ISSUE)['phase'], 'RESULT_AWAITING_RESTART')
            self.assertEqual(self.publishers.calls, 0)
        self.assertEqual(self.runtime.runs, 1)

    def test_missing_current_epoch_is_not_restart_evidence(self):
        self.await_restart()
        self.runtime.epoch = None
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'RESULT_AWAITING_RESTART')
        self.assertEqual(self.publishers.calls, 0)

    def test_interrupted_claim_never_reexecutes_even_after_valid_new_epoch(self):
        binding = self.controller.approve(ISSUE)
        self.assertTrue(self.state.claim(binding, epoch=epoch()))
        witness = self.root / 'claims' / (str(ISSUE) + '.json')
        original_witness, original_inode = witness.read_bytes(), witness.stat().st_ino
        self.runtime.epoch = self.newer_epoch()
        self.reopen()
        self.assertEqual(self.state.get(ISSUE)['phase'], 'CLAIMED')
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'INTERRUPTED')
        self.assertEqual(report['outcome'], 'unknown')
        self.assertTrue(report['historical'])
        self.assertFalse(report['dispatched'])
        self.assertTrue(report['duplicate'])
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.runtime.preflights, 0)
        self.assertEqual(self.runtime.current_calls, 1)
        self.assertEqual(self.publishers.calls, 0)
        self.assertEqual(self.saved_epoch(), (epoch().to_dict(), self.newer_epoch().to_dict()))
        self.assertEqual(self.state.get(ISSUE), dict(binding=binding, phase='INTERRUPTED',
                                                   result=None, publication=None))
        self.assertFalse(self.state.claim(binding, epoch=self.newer_epoch()))
        self.assertEqual((witness.read_bytes(), witness.stat().st_ino),
                         (original_witness, original_inode))
        self.reopen()
        self.runtime.epoch = epoch(pid_namespace_inode=4026533003, pid1_start_ticks=3000)
        self.assertEqual(self.controller.process(ISSUE), report)
        self.assertEqual(self.runtime.current_calls, 1)
        self.assertEqual(self.saved_epoch(), (epoch().to_dict(), self.newer_epoch().to_dict()))

    def test_same_epoch_reopen_leaves_unfinished_claim_unchanged(self):
        binding = self.controller.approve(ISSUE)
        self.state.claim(binding, epoch=epoch())
        for _ in range(2):
            self.reopen()
            report = self.controller.process(ISSUE)
            self.assertEqual(report['phase'], 'CLAIMED')
            self.assertFalse(report['dispatched'])
            self.assertEqual(self.saved_epoch(), (epoch().to_dict(), None))
            self.assertEqual(self.runtime.runs, 0)
            self.assertEqual(self.publishers.calls, 0)

    def test_interruption_requires_complete_verified_restart_evidence(self):
        binding = self.controller.approve(ISSUE)
        self.state.claim(binding, epoch=epoch())
        changed = [epoch(), replace(self.newer_epoch(), container_id='f' * 64),
                   replace(self.newer_epoch(), boot_id=OTHER_BOOT),
                   replace(epoch(), pid1_start_ticks=2000),
                   replace(epoch(), pid_namespace_inode=4026533002),
                   replace(self.newer_epoch(), pid1_start_ticks=999)]
        for value in changed:
            with self.subTest(epoch=value):
                self.runtime.epoch = value
                self.reopen()
                self.assertEqual(self.controller.process(ISSUE)['phase'], 'CLAIMED')
                self.assertEqual(self.saved_epoch(), (epoch().to_dict(), None))
        self.runtime.epoch = self.newer_epoch()
        for verified in (False, None, 1, 'verified'):
            with self.subTest(contract=verified):
                self.runtime.deployment_contract_verified = verified
                self.assertEqual(self.controller.process(ISSUE)['phase'], 'CLAIMED')
                self.assertEqual(self.saved_epoch(), (epoch().to_dict(), None))
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.runtime.preflights, 0)
        self.assertEqual(self.publishers.calls, 0)

    def test_missing_epoch_cannot_mark_claim_interrupted(self):
        binding = self.controller.approve(ISSUE)
        self.state.claim(binding, epoch=epoch())
        self.runtime.epoch = None
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'CLAIMED')
        self.assertEqual(self.saved_epoch(), (epoch().to_dict(), None))
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.publishers.calls, 0)

    def test_interruption_is_historical_even_when_approval_or_source_changed(self):
        binding = self.controller.approve(ISSUE)
        self.state.claim(binding, epoch=epoch())
        self.runtime.epoch = self.newer_epoch()
        self.reopen()
        self.now = timestamp(self.source.request['expires_at']) + 1
        self.source.revoke()
        self.controller.source_sha = 'c' * 40
        with patch.object(self.source, 'get_issue', side_effect=AssertionError('no new admission')):
            report = self.controller.process(ISSUE)
            self.assertEqual(self.controller.process(ISSUE), report)
        self.assertEqual(report['phase'], 'INTERRUPTED')
        self.assertEqual(report['outcome'], 'unknown')
        self.assertEqual(self.state.get(ISSUE)['binding'], binding)
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.runtime.preflights, 0)
        self.assertEqual(self.publishers.calls, 0)

    def test_interrupted_history_does_not_restart_generation_or_starve_scan(self):
        binding = self.controller.approve(ISSUE)
        self.state.claim(binding, epoch=epoch())
        self.runtime.epoch = self.newer_epoch()
        self.source.candidates = [ISSUE, ISSUE + 1]
        self.reopen()
        with patch.object(self.controller, 'process', wraps=self.controller.process) as process:
            reports = self.controller.once()
        self.assertEqual(process.call_count, 2)
        self.assertEqual(reports[0]['phase'], 'INTERRUPTED')
        self.assertEqual(reports[0]['outcome'], 'unknown')
        self.assertTrue(all(report['dispatched'] is False for report in reports))
        self.assertTrue(all(not report.get('restart_required') for report in reports))
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.publishers.calls, 0)

    def test_failed_claim_stays_blocked_after_new_epoch(self):
        self.runtime.run_error = RuntimeError('synthetic interrupted execution')
        with self.assertRaises(RuntimeError):
            self.controller.process(ISSUE)
        self.runtime.run_error = None
        self.runtime.epoch = self.newer_epoch()
        self.reopen()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'BLOCKED')
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_restart_boundary_does_not_replace_current_owner_authorization(self):
        self.await_restart()
        self.source.revoke()
        self.runtime.epoch = self.newer_epoch()
        self.reopen()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)
        self.assertIsNone(self.state.get(ISSUE)['publication'])

    def test_approval_expiry_after_restart_still_prevents_publication(self):
        self.await_restart()
        self.now = timestamp(self.source.request['expires_at'])
        self.runtime.epoch = self.newer_epoch()
        self.reopen()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.publishers.calls, 0)
        self.assertEqual(self.runtime.runs, 1)

    def test_batch_stops_immediately_after_result_requires_restart(self):
        self.source.candidates = [ISSUE, ISSUE + 1]
        with patch.object(self.controller, 'process', wraps=self.controller.process) as process:
            reports = self.controller.once()
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]['restart_required'])
        self.assertEqual(process.call_count, 1)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_batch_stops_after_attempt_even_when_post_result_approval_fails(self):
        self.source.candidates = [ISSUE, ISSUE + 1]
        self.runtime.on_result = self.source.revoke
        with patch.object(self.controller, 'process', wraps=self.controller.process) as process:
            reports = self.controller.once()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['phase'], 'BLOCKED')
        self.assertEqual(reports[0]['dispatched'], 'unknown')
        self.assertEqual(process.call_count, 1)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_old_failed_claim_does_not_force_restart_or_starve_later_issues(self):
        self.runtime.run_error = RuntimeError('synthetic failed attempt')
        with self.assertRaises(RuntimeError):
            self.controller.process(ISSUE)
        self.runtime.run_error = None
        self.source.revoke()
        self.source.candidates = [ISSUE, ISSUE + 1]
        self.reopen()
        with patch.object(self.controller, 'process', wraps=self.controller.process) as process:
            reports = self.controller.once()
        self.assertEqual(process.call_count, 2)
        self.assertEqual([report['issue'] for report in reports], [ISSUE, ISSUE + 1])
        self.assertTrue(all(report['dispatched'] is False for report in reports))
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_oversized_queue_is_rejected_before_processing_any_issue(self):
        self.source.candidates = list(range(1, MAX_TRACKED + 2))
        with patch.object(self.controller, 'process') as process:
            with self.assertRaisesRegex(ContractError, 'tracked Issue limit reached'):
                self.controller.once()
        process.assert_not_called()
        self.assertEqual(self.runtime.runs, 0)

    def test_scan_deadline_stops_before_next_issue(self):
        with patch('research_harness.service.time.monotonic', side_effect=[0, SCAN_SECONDS]), \
             patch.object(self.controller, 'process') as process:
            reports = self.controller.once()
        self.assertEqual(reports, [dict(phase='SCAN_DEADLINE', dispatched=False)])
        process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
