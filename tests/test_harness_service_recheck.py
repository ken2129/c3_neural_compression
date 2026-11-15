"""Per-run approval refresh uses fake clocks/sources, never network or models."""
import math
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from research_harness.intake_flow_fixture import FIXTURE_NOW
from research_harness.manager_runtime import RecheckRejected, safe_recheck_failure
from research_harness.owner_approval import approval_read_timeout
from research_harness.result_publisher import parse_publication_request
from research_harness.schema import ContractError
from research_harness.service import Controller, _RunApprovalRecheck
from research_harness.service_state import ServiceState, initialize
from test_harness_service import FakeRuntime, ISSUE, PublisherFactory, ServiceSource, SOURCE_SHA
from test_harness_service_model import ModelRuntime, ModelSource


class Clock:
    def __init__(self):
        self.wall = FIXTURE_NOW
        self.mono = 100.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class RunApprovalRecheckTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.expires = self.clock.wall + 60
        self.calls = 0
        self.effect = lambda: None

    def refresh(self):
        self.calls += 1
        self.effect()
        return self.expires

    def make(self, refresh=None):
        return _RunApprovalRecheck(refresh or self.refresh,
            clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)

    def assert_reason(self, reason, action):
        with self.assertRaises(RecheckRejected) as caught:
            action()
        self.assertEqual(safe_recheck_failure(caught.exception),
                         dict(reason=reason, origin='manager'))

    def test_initial_snapshot_is_fresh_and_io_checks_do_not_fetch_again(self):
        check = self.make()
        self.assertEqual(self.calls, 1)
        for _ in range(1000):
            check()
        self.assertEqual(self.calls, 1)
        self.clock.advance(4.99)
        check()
        self.assertEqual(self.calls, 1)
        self.clock.advance(.02)
        check()
        self.assertEqual(self.calls, 2)
        for _ in range(1000):
            check()
        self.assertEqual(self.calls, 2)

    def test_refresh_age_is_measured_from_start_not_completion(self):
        self.effect = lambda: self.clock.advance(2)
        check = self.make()
        self.clock.advance(2.99)
        check()
        self.assertEqual(self.calls, 1)
        self.clock.advance(.02)
        check()
        self.assertEqual(self.calls, 2)

    def test_every_refresh_runs_inside_shared_source_read_budget(self):
        budgets = []
        self.effect = lambda: budgets.append(approval_read_timeout(20))
        check = self.make()
        self.clock.advance(5)
        check()
        self.assertEqual(len(budgets), 2)
        self.assertTrue(all(0 < budget <= 5 for budget in budgets))
        self.assertEqual(approval_read_timeout(20), 20)

    def test_local_wall_expiry_blocks_immediately_without_refresh(self):
        self.expires = self.clock.wall + 1
        check = self.make()
        self.clock.advance(1)
        self.assert_reason('expired', check)
        self.assertEqual(self.calls, 1)
        self.assert_reason('inactive_snapshot', check)

    def test_monotonic_expiry_cannot_be_extended_by_a_stalled_wall_clock(self):
        self.expires = self.clock.wall + 1
        check = self.make()
        self.clock.mono += 1
        self.assert_reason('expired', check)
        self.assertEqual(self.calls, 1)

    def test_expiry_during_refresh_rejects_without_renewing_snapshot(self):
        self.expires = self.clock.wall + 6
        check = self.make()
        self.clock.advance(5)
        self.effect = lambda: self.clock.advance(2)
        self.assert_reason('expired', check)
        self.assert_reason('inactive_snapshot', check)

    def test_failed_refresh_cannot_reuse_old_snapshot_or_retry_implicitly(self):
        check = self.make()
        self.clock.advance(5)
        self.effect = lambda: (_ for _ in ()).throw(TimeoutError('private upstream text'))
        self.assert_reason('source_timeout', check)
        self.assertEqual(self.calls, 2)
        self.effect = lambda: None
        self.assert_reason('inactive_snapshot', check)
        self.assertEqual(self.calls, 2)

    def test_refresh_cannot_change_expiry_bound_to_original_issue(self):
        check = self.make()
        self.clock.advance(5)
        self.expires += 1
        self.assert_reason('changed', check)
        self.assert_reason('inactive_snapshot', check)

    def test_snapshot_exceeding_five_second_window_is_never_accepted(self):
        for seconds in (5, 6):
            with self.subTest(seconds=seconds):
                self.clock = Clock()
                self.effect = lambda: self.clock.advance(seconds)
                self.assert_reason('refresh_stale', self.make)
        self.effect = lambda: None
        check = self.make()
        self.clock.advance(5)
        self.effect = lambda: self.clock.advance(5)
        self.assert_reason('refresh_stale', check)
        self.assert_reason('inactive_snapshot', check)

    def test_missing_invalid_or_already_expired_initial_snapshot_rejected(self):
        for value in (None, True, {}, float('nan'), float('inf'), -1, 0):
            with self.subTest(value=value):
                self.assert_reason('inactive_snapshot', lambda: self.make(lambda: value))
        self.assert_reason('expired', lambda: self.make(lambda: self.clock.wall))
        self.assert_reason('source_timeout', lambda: self.make(
            lambda: (_ for _ in ()).throw(TimeoutError('private'))))

    def test_clock_reversals_and_nonfinite_values_invalidate_permanently(self):
        for field in ('wall', 'mono'):
            for value in (None, True, float('nan'), float('inf'), -1, 0):
                with self.subTest(field=field, value=value):
                    self.clock = Clock()
                    check = self.make()
                    setattr(self.clock, field, value)
                    self.assert_reason('clock_anomaly', check)
                    self.clock = Clock()
                    self.assert_reason('inactive_snapshot', check)

    def test_initial_invalid_clocks_never_call_source(self):
        for field in ('wall', 'mono'):
            self.clock = Clock()
            setattr(self.clock, field, math.nan)
            self.assert_reason('clock_anomaly', self.make)
        self.assertEqual(self.calls, 0)

    def test_forward_clock_jump_or_stalled_clock_cannot_extend_cached_authority(self):
        for field in ('wall', 'mono'):
            with self.subTest(field=field):
                self.clock = Clock()
                check = self.make()
                setattr(self.clock, field, getattr(self.clock, field) + 2)
                self.assert_reason('clock_anomaly', check)
                self.assert_reason('inactive_snapshot', check)

    def test_small_clock_drift_is_measured_against_original_run_offset(self):
        check = self.make()
        for _ in range(4):
            self.clock.wall += .2
            check()
        self.clock.wall += .4
        self.assert_reason('clock_anomaly', check)

    def test_clock_anomaly_during_refresh_is_rejected(self):
        check = self.make()
        self.clock.advance(5)
        self.effect = lambda: setattr(self.clock, 'wall', self.clock.wall - 1)
        self.assert_reason('clock_anomaly', check)
        self.assert_reason('inactive_snapshot', check)

    def test_completed_run_snapshot_cannot_be_reused(self):
        check = self.make()
        check.close()
        self.assert_reason('inactive_snapshot', check)
        self.assertEqual(self.calls, 1)


class CountSource(ServiceSource):
    def __init__(self):
        super().__init__()
        self.comment_reads = 0

    def comments(self, number):
        self.comment_reads += 1
        return super().comments(number)


class BurstRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.inspect_recheck = lambda check: None
        self.saved_recheck = None

    def run(self, binding, recheck):
        self.saved_recheck = recheck
        self.inspect_recheck(recheck)
        return super().run(binding, recheck)


class ControllerRecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        initialize(self.root)
        self.state = ServiceState(self.root)
        self.addCleanup(self.state.close)
        self.source = CountSource()
        self.clock = Clock()
        self.runtime = BurstRuntime()
        self.publishers = PublisherFactory()
        self.controller = Controller(self.state, self.source, self.source, self.runtime,
            self.publishers, source_sha=SOURCE_SHA, publication_base_getter=lambda: SOURCE_SHA,
            clock=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono)

    def test_each_fresh_snapshot_is_three_rest_reads_and_one_comment_scan(self):
        self.controller.approve(ISSUE)
        self.assertEqual((self.source.reads, self.source.comment_reads), (3, 1))

    def test_run_pumps_reuse_only_run_snapshot_and_periodic_refresh_is_fresh(self):
        def inspect(check):
            start = (self.source.reads, self.source.comment_reads)
            self.assertEqual(start, (12, 4))
            for _ in range(1000): check()
            self.assertEqual((self.source.reads, self.source.comment_reads), start)
            self.clock.advance(5)
            check()
            self.assertEqual((self.source.reads, self.source.comment_reads),
                             (start[0] + 3, start[1] + 1))
        self.runtime.inspect_recheck = inspect
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'PUBLISHED')
        self.assertEqual(self.runtime.runs, 1)
        with self.assertRaises(RecheckRejected): self.runtime.saved_recheck()

    def test_runtime_failure_also_invalidates_captured_callback(self):
        self.runtime.run_error = RuntimeError('synthetic runtime failure')
        with self.assertRaises(RuntimeError): self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        with self.assertRaises(RecheckRejected): self.runtime.saved_recheck()
        self.assertEqual(self.publishers.calls, 0)

    def test_periodic_source_failure_blocks_with_safe_reason_and_never_retries(self):
        def inspect(check):
            self.clock.advance(5)
            with patch.object(self.source, 'get_issue', side_effect=HTTPError(
                    'https://private.invalid', 429, 'secret body', {}, None)):
                check()
        self.runtime.inspect_recheck = inspect
        report = self.controller.once()[0]
        self.assertEqual(report['phase'], 'BLOCKED')
        self.assertEqual(report['approval_failure'],
                         dict(reason='source_rate_limited', origin='manager'))
        self.assertNotIn('private', str(report))
        self.assertNotIn('secret', str(report))
        self.assertIsNone(self.state.get(ISSUE)['result'])
        self.assertEqual(self.publishers.calls, 0)
        self.runtime.inspect_recheck = lambda check: None
        self.assertEqual(self.controller.process(ISSUE)['phase'], 'BLOCKED')

    def test_periodic_revocation_is_detected_within_refresh_window(self):
        def inspect(check):
            self.source.revoke()
            check()  # Receipt remains valid only inside the approved cache window.
            self.clock.advance(5)
            check()
        self.runtime.inspect_recheck = inspect
        report = self.controller.once()[0]
        self.assertEqual(report['phase'], 'BLOCKED')
        self.assertEqual(report['approval_failure'], dict(reason='changed', origin='manager'))
        self.assertIsNone(self.state.get(ISSUE)['result'])
        self.assertEqual(self.publishers.calls, 0)

    def test_fresh_postrun_check_blocks_revoke_inside_cache_window(self):
        self.runtime.on_result = self.source.revoke
        report = self.controller.once()[0]
        self.assertEqual(report['phase'], 'BLOCKED')
        self.assertEqual(report['approval_failure'], dict(reason='changed', origin='manager'))
        self.assertIsNone(self.state.get(ISSUE)['result'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_publication_callback_still_reads_fresh_authorization(self):
        self.publishers.before_publish = lambda request: self.source.revoke()
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'RESULT_READY')
        self.assertEqual(report['publication_phase'], 'BLOCKED')
        self.assertEqual(self.publishers.transport.events, [])
        self.assertEqual(self.runtime.runs, 1)

    def test_unrelated_runtime_contract_error_is_not_mislabeled_approval(self):
        self.runtime.run_error = ContractError('unrelated runtime contract')
        report = self.controller.once()[0]
        self.assertEqual(report['phase'], 'BLOCKED')
        self.assertNotIn('approval_failure', report)

    def make_model_controller(self):
        self.source = ModelSource()
        self.runtime = ModelRuntime()
        self.controller = Controller(self.state, self.source, self.source, self.runtime,
            self.publishers, source_sha=SOURCE_SHA, publication_base_getter=lambda: SOURCE_SHA,
            clock=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono)

    def test_model_final_approval_has_shared_budget_without_changing_other_fresh_checks(self):
        self.make_model_controller()
        budgets = []
        original = self.controller._approval_snapshot
        def snapshot(*args):
            budgets.append(approval_read_timeout(20))
            return original(*args)
        with patch('research_harness.owner_approval.time.monotonic', side_effect=lambda: self.clock.mono), \
             patch.object(self.controller, '_approval_snapshot', side_effect=snapshot):
            report = self.controller.process(ISSUE)
            self.assertEqual(report['phase'], 'RESULT_AWAITING_RESTART')
            # Preflight/admission stay ordinary fresh checks; the initial run
            # snapshot and distinct post-result approval each have a 5s budget.
            self.assertEqual(budgets, [20, 20, 20, 5, 5])
            self.assertEqual(self.source.reads, 15)
            self.assertEqual(approval_read_timeout(20), 20)
            budgets.clear()
            self.runtime.epoch = replace(self.runtime.epoch, pid1_start_ticks=2000)
            published = self.controller.process(ISSUE)
            self.assertEqual(published['phase'], 'PUBLISHED')
            self.assertGreaterEqual(len(budgets), 4)
            self.assertTrue(all(budget == 20 for budget in budgets))
        self.assertEqual(self.runtime.runs, 1)

    def test_model_final_approval_timeout_blocks_claim_without_saving_result(self):
        self.make_model_controller()
        original = self.source.get_issue
        def source_read(number):
            if self.runtime.runs:
                self.clock.advance(5)
                approval_read_timeout(20)
            return original(number)
        with patch('research_harness.owner_approval.time.monotonic', side_effect=lambda: self.clock.mono), \
             patch.object(self.source, 'get_issue', side_effect=source_read):
            report = self.controller.once()[0]
        self.assertEqual(report['phase'], 'BLOCKED')
        self.assertEqual(report['approval_failure'], dict(reason='source_timeout', origin='manager'))
        saved = self.state.get(ISSUE)
        self.assertIsNone(saved['result'])
        self.assertIsNone(saved['publication'])
        self.assertTrue((self.root / 'claims' / (str(ISSUE) + '.json')).is_file())
        self.assertEqual(self.state.db.execute('SELECT after_epoch FROM service_epochs').fetchone(), (None,))
        self.assertEqual(self.controller.process(ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_model_candidate_near_soft_deadline_saves_after_bounded_fresh_approval(self):
        self.make_model_controller()
        started = self.clock.mono
        self.runtime.on_result = lambda: self.clock.advance(99.9)
        self.runtime.result_transform = lambda result: replace(result, elapsed_seconds=99.9)
        original = self.controller._approval_snapshot
        def snapshot(*args):
            if self.runtime.runs:
                self.assertEqual(approval_read_timeout(20), 5)
                self.clock.advance(4.9)
            return original(*args)
        with patch('research_harness.owner_approval.time.monotonic', side_effect=lambda: self.clock.mono), \
             patch.object(self.controller, '_approval_snapshot', side_effect=snapshot):
            report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'RESULT_AWAITING_RESTART')
        self.assertTrue(report['restart_required'])
        self.assertAlmostEqual(self.clock.mono - started, 104.8)
        request = parse_publication_request(self.state.get(ISSUE)['result'])
        self.assertEqual(request.result.elapsed_seconds, 99.9)
        self.assertEqual(request.result.status, 'candidate_only')
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_model_final_approval_returning_after_budget_still_cannot_save(self):
        self.make_model_controller()
        original = self.controller._approval_snapshot
        def snapshot(*args):
            value = original(*args)
            if self.runtime.runs:
                self.clock.advance(5)
            return value
        with patch('research_harness.owner_approval.time.monotonic', side_effect=lambda: self.clock.mono), \
             patch.object(self.controller, '_approval_snapshot', side_effect=snapshot):
            with self.assertRaises(TimeoutError):
                self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertIsNone(self.state.get(ISSUE)['result'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)
        self.assertEqual(approval_read_timeout(20), 20)


if __name__ == '__main__':
    unittest.main()
