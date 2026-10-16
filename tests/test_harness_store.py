"""CPU-only transaction and state-machine evidence. No real issuer or launch."""
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from research_harness.schema import ContractError, Plan, fixture_plan
from research_harness.store import (AlreadyClaimed, InvalidTransition, Ledger,
                                    LedgerError, ResourceBusy, SCENARIOS,
                                    SingleInstance, StaleFence)


PROCESS = {'pid': 1234, 'start_time': 100, 'boot_id': 'fixture-boot', 'process_group': 1234}
SALT = 'a' * 64


def approval(plan, *, event_id='approve-1', nonce='nonce-1', **changes):
    result = {'event_id': event_id, 'issuer_id': 'fake-issuer',
              'request_id': plan.request_id, 'plan_id': plan.plan_id, 'revision': plan.revision,
              'repo': plan.repo, 'issue': plan.issue, 'digest': plan.digest(SALT),
              'nonce': nonce, 'scope': ['cpu_fixture'], 'issued_at': 0, 'expires_at': 1000}
    result.update(changes)
    return result


def revocation(plan, **changes):
    result = {'event_id': 'revoke-1', 'issuer_id': 'fake-issuer',
              'request_id': plan.request_id, 'revision': plan.revision, 'issued_at': 2}
    result.update(changes)
    return result


def race_claim(path, request_id, start, output):
    ledger = Ledger(path)
    start.wait(10)
    try:
        claim = ledger.claim(request_id, owner='competitor', now=1)
        output.put(('claimed', claim.run_id))
    except LedgerError as exc:
        output.put(('blocked', type(exc).__name__))


def lock_competitor(path, output):
    try:
        with SingleInstance(path):
            output.put('acquired')
    except ResourceBusy:
        output.put('blocked')


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'ledger.sqlite')
        self.ledger = Ledger(self.path)
        self.plan = fixture_plan()
        self.ledger.register_plan(self.plan, digest=self.plan.digest(SALT))
        self.ledger.ingest_approval(approval(self.plan))

    def claim(self, **kwargs):
        return self.ledger.claim(self.plan.request_id, owner='controller', now=1, **kwargs)

    def running(self, **kwargs):
        claim = self.claim(**kwargs)
        self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=2)
        return claim

    def validated(self):
        claim = self.running()
        self.ledger.finish_attempt(claim.run_id, claim.fence, now=4, elapsed_seconds=2, outcome='success', process_gone=True)
        self.step(claim, 'S10', 'valid_result')
        return claim

    def step(self, claim, scenario, event):
        return self.ledger.transition(claim.run_id, claim.fence, scenario, event, now=5)

    def test_atomic_claim_is_single_consumption_reservation_and_budget(self):
        claim = self.claim()
        run = self.ledger.get_run(claim.run_id)
        self.assertEqual(run['reserved_seconds'], 5)
        self.assertEqual(run['attempt_count'], 1)
        self.assertEqual(self.ledger.machine_owner(), claim.run_id)
        with self.assertRaises(AlreadyClaimed):
            self.claim()
        self.assertEqual(self.ledger.counts()['runs'], 1)
        self.assertEqual(self.ledger.counts()['attempts'], 1)

    def test_failed_machine_reservation_rolls_back_nonce_and_run(self):
        claim = self.running()
        other = self.plan.to_dict()
        other['request_id'] = 'request-2'
        second = Plan.parse(other)
        self.ledger.register_plan(second, digest=second.digest(SALT))
        self.ledger.ingest_approval(approval(second, event_id='approve-2', nonce='nonce-2'))
        with self.assertRaises(ResourceBusy):
            self.ledger.claim(second.request_id, owner='second', now=3)
        self.assertEqual(self.ledger.counts()['runs'], 1)
        self.ledger.finish_attempt(claim.run_id, claim.fence, now=4, elapsed_seconds=2, outcome='success', process_gone=True)
        second_claim = self.ledger.claim(second.request_id, owner='second', now=5)
        self.assertNotEqual(claim.run_id, second_claim.run_id)

    def test_mid_transaction_exception_rolls_back_every_write(self):
        with mock.patch.object(self.ledger, '_fence', side_effect=OSError('injected disk fault')):
            with self.assertRaises(OSError):
                self.claim()
        self.assertIsNone(self.ledger.machine_owner())
        self.assertEqual(self.ledger.counts()['runs'], 0)
        self.assertEqual(self.ledger.counts()['attempts'], 0)
        self.assertEqual(self.claim().fence, 1)

    def test_duplicate_events_are_idempotent_but_edits_and_deletion_are_rejected(self):
        self.assertFalse(self.ledger.ingest_approval(approval(self.plan)))
        with self.assertRaisesRegex(LedgerError, 'immutable event'):
            self.ledger.ingest_approval(approval(self.plan, expires_at=2000))
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM events")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE events SET body='changed'")
        self.assertEqual(self.ledger.counts()['events'], 1)

    def test_nonce_replay_rolls_back_event_ingestion(self):
        with self.assertRaisesRegex(LedgerError, 'nonce replay'):
            self.ledger.ingest_approval(approval(self.plan, event_id='another-delivery'))
        self.assertEqual(self.ledger.counts()['events'], 1)

    def test_plan_revision_is_immutable(self):
        self.assertFalse(self.ledger.register_plan(self.plan, digest=self.plan.digest(SALT)))
        with self.assertRaisesRegex(LedgerError, 'immutable plan'):
            self.ledger.register_plan(self.plan, digest='b' * 64)

    def test_higher_revision_blocks_old_approval(self):
        body = self.plan.to_dict()
        body['revision'] = 2
        newer = Plan.parse(body)
        self.ledger.register_plan(newer, digest=newer.digest(SALT))
        with self.assertRaisesRegex(LedgerError, 'no current'):
            self.claim()
        self.ledger.ingest_approval(approval(newer, event_id='approve-2', nonce='nonce-2'))
        self.assertEqual(self.ledger.get_run(self.claim().run_id)['revision'], 2)

    def test_wrong_issue_digest_plan_and_repo_cannot_authorize(self):
        for key, value in [('issue', 42), ('issue', True), ('digest', 'b' * 64),
                           ('plan_id', 'other-plan'), ('repo', 'unrelated/repo')]:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as root:
                ledger = Ledger(str(Path(root) / 'ledger'))
                ledger.register_plan(self.plan, digest=self.plan.digest(SALT))
                ledger.ingest_approval(approval(self.plan, **{key: value}))
                with self.assertRaisesRegex(LedgerError, 'no current'):
                    ledger.claim(self.plan.request_id, owner='fixture', now=1)
                self.assertIsNone(ledger.machine_owner())

    def test_future_and_expired_approval_block_claim(self):
        for at in (-1, 1000, 1001):
            with self.subTest(at=at), self.assertRaises(LedgerError):
                self.ledger.claim(self.plan.request_id, owner='controller', now=at)

    def test_fake_events_never_enable_real_modes_or_author_based_trust(self):
        for changes in [{'issuer_id': 'github-admin'}, {'scope': ['gpu']},
                        {'author': 'verified-owner'}, {'version': True}]:
            with self.subTest(changes=changes), self.assertRaises(LedgerError):
                self.ledger.ingest_approval(approval(self.plan, **changes))
        with self.assertRaises(LedgerError):
            Ledger(self.path, fake_issuer_id='live-issuer')
        body = self.plan.to_dict()
        body['operation'] = 'live_gpu'
        with self.assertRaises(ContractError):
            self.ledger.register_plan(body, digest='a' * 64)

    def test_revocation_arriving_before_approval_is_retained(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Ledger(str(Path(root) / 'ledger'))
            self.assertTrue(ledger.ingest_revocation(revocation(self.plan)))
            self.assertFalse(ledger.ingest_revocation(revocation(self.plan)))
            ledger.ingest_approval(approval(self.plan))
            ledger.register_plan(self.plan, digest=self.plan.digest(SALT))
            with self.assertRaises(LedgerError):
                ledger.claim(self.plan.request_id, owner='fixture', now=3)
            self.assertEqual(ledger.counts()['revocations'], 1)
            self.assertEqual(ledger.counts()['runs'], 0)

    def test_revoke_claimed_and_running_never_releases_unconfirmed_process(self):
        for start in (False, True):
            with self.subTest(start=start):
                if start:
                    self.setUp()
                claim = self.running() if start else self.claim()
                self.ledger.ingest_revocation(revocation(self.plan))
                self.assertEqual(self.ledger.get_run(claim.run_id)['state'], 'CANCELLING')
                self.assertEqual(self.ledger.machine_owner(), claim.run_id)
                with self.assertRaises(InvalidTransition):
                    self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=3)
                self.ledger.finish_attempt(claim.run_id, claim.fence, now=4,
                                           elapsed_seconds=2, outcome='cancelled', process_gone=True)
                self.assertEqual(self.ledger.get_run(claim.run_id)['state'], 'CANCELLED')
                self.assertIsNone(self.ledger.machine_owner())

    def test_expiry_and_revision_rechecked_immediately_before_running(self):
        claim = self.claim(lease_seconds=2000)
        with self.assertRaisesRegex(LedgerError, 'expiry'):
            self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=1000)
        body = self.plan.to_dict()
        body['revision'] = 2
        new_plan = Plan.parse(body)
        self.ledger.register_plan(new_plan, digest=new_plan.digest(SALT))
        with self.assertRaisesRegex(LedgerError, 'revision'):
            self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=3)

    def test_process_identity_is_complete_and_persisted(self):
        claim = self.claim()
        for invalid in [{}, dict(PROCESS, pid=0), dict(PROCESS, start_time=None)]:
            with self.subTest(invalid=invalid), self.assertRaises(LedgerError):
                self.ledger.mark_running(claim.run_id, claim.fence, process=invalid, now=2)
        self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=2)
        self.assertEqual(self.ledger.get_run(claim.run_id)['attempts'][0]['process'], PROCESS)

    def test_lease_expiry_fences_but_retains_machine_and_budget(self):
        claim = self.running(lease_seconds=3)
        self.assertEqual(self.ledger.expire_leases(now=4), [claim.run_id])
        self.assertEqual(self.ledger.expire_leases(now=5), [])
        run = self.ledger.get_run(claim.run_id)
        self.assertEqual(run['state'], 'BLOCKED_RECONCILING')
        self.assertEqual(run['reserved_seconds'], 5)
        self.assertEqual(self.ledger.machine_owner(), claim.run_id)
        self.assertGreater(run['fence'], claim.fence)
        with self.assertRaises(StaleFence):
            self.ledger.heartbeat(claim.run_id, claim.fence, now=5)
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(claim.run_id, owner='replacement', now=5)
        with self.assertRaises(LedgerError):
            self.ledger.reconcile(claim.run_id, owner='replacement', now=5,
                                  process_gone=False, elapsed_seconds=3, outcome='failed')
        recovered = self.ledger.reconcile(claim.run_id, owner='replacement', now=5,
                                         process_gone=True, elapsed_seconds=3, outcome='failed')
        self.assertGreater(recovered.fence, run['fence'])
        self.assertIsNone(self.ledger.machine_owner())
        self.assertEqual(self.ledger.get_run(claim.run_id)['spent_seconds'], 3)

    def test_cancel_survives_reconciliation(self):
        claim = self.running(lease_seconds=3)
        self.ledger.ingest_revocation(revocation(self.plan))
        self.ledger.expire_leases(now=4)
        self.ledger.reconcile(claim.run_id, owner='replacement', now=5,
                              process_gone=True, elapsed_seconds=3, outcome='transient_io')
        self.assertEqual(self.ledger.get_run(claim.run_id)['state'], 'CANCELLED')

    def test_heartbeat_rejects_expired_and_backward_clock(self):
        claim = self.running(lease_seconds=3)
        with self.assertRaises(LedgerError):
            self.ledger.heartbeat(claim.run_id, claim.fence, now=1)
        with self.assertRaises(StaleFence):
            self.ledger.heartbeat(claim.run_id, claim.fence, now=4)
        self.ledger.heartbeat(claim.run_id, claim.fence, now=3, lease_seconds=5)
        self.assertEqual(self.ledger.expire_leases(now=4), [])

    def test_finish_requires_confirmed_termination_and_is_not_repeatable(self):
        claim = self.running()
        with self.assertRaises(LedgerError):
            self.ledger.finish_attempt(claim.run_id, claim.fence, now=3,
                                       elapsed_seconds=1, outcome='success', process_gone=False)
        self.assertEqual(self.ledger.machine_owner(), claim.run_id)
        self.ledger.finish_attempt(claim.run_id, claim.fence, now=4, elapsed_seconds=2, outcome='success', process_gone=True)
        with self.assertRaises(InvalidTransition):
            self.ledger.finish_attempt(claim.run_id, claim.fence, now=5, elapsed_seconds=2, outcome='success', process_gone=True)
        self.assertEqual(self.ledger.get_run(claim.run_id)['spent_seconds'], 2)

    def test_retry_is_fresh_attempt_in_same_run_and_budget(self):
        first = self.running()
        self.ledger.finish_attempt(first.run_id, first.fence, now=4, elapsed_seconds=2, outcome='transient_io', process_gone=True)
        second = self.ledger.retry(first.run_id, owner='controller', now=5)
        self.assertEqual(first.run_id, second.run_id)
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        self.assertGreater(second.fence, first.fence)
        run = self.ledger.get_run(first.run_id)
        self.assertEqual(run['spent_seconds'], 2)
        self.assertEqual(run['reserved_seconds'], 5)
        self.assertEqual(run['attempt_count'], 2)
        self.assertIsNone(run['attempts'][1]['process'])
        with self.assertRaises(StaleFence):
            self.ledger.heartbeat(first.run_id, first.fence, now=5)
        self.ledger.finish_attempt(second.run_id, second.fence, now=7, elapsed_seconds=2, outcome='transient_io', process_gone=True)
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(first.run_id, owner='controller', now=8)

    def test_oom_unapproved_failures_and_scientific_success_never_retry(self):
        for outcome in ('oom', 'failed', 'timeout', 'transient_api', 'success'):
            with self.subTest(outcome=outcome, process_gone=True):
                if outcome != 'oom':
                    self.setUp()
                claim = self.running()
                self.ledger.finish_attempt(claim.run_id, claim.fence, now=4,
                                           elapsed_seconds=2, outcome=outcome, process_gone=True)
                with self.assertRaises(InvalidTransition):
                    self.ledger.retry(claim.run_id, owner='controller', now=5)
        with self.assertRaisesRegex(LedgerError, 'performance'):
            self.ledger.finish_attempt(claim.run_id, claim.fence, now=5,
                                       elapsed_seconds=0, outcome='improve_ap', process_gone=True)

    def test_budget_overrun_is_recorded_not_erased_or_retried(self):
        claim = self.running()
        self.ledger.finish_attempt(claim.run_id, claim.fence, now=20, elapsed_seconds=15, outcome='transient_io', process_gone=True)
        run = self.ledger.get_run(claim.run_id)
        self.assertEqual(run['state'], 'BLOCKED_BUDGET')
        self.assertEqual(run['spent_seconds'], 15)
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(claim.run_id, owner='controller', now=21)

    def test_budget_remaining_must_cover_entire_attempt(self):
        body = self.plan.to_dict()
        body['request_id'] = 'limited-request'
        body['budget']['total_seconds'] = 6
        plan = Plan.parse(body)
        self.ledger.register_plan(plan, digest=plan.digest(SALT))
        self.ledger.ingest_approval(approval(plan, event_id='limited-approval', nonce='limited-nonce'))
        claim = self.ledger.claim(plan.request_id, owner='controller', now=1)
        self.ledger.finish_attempt(claim.run_id, claim.fence, now=4, elapsed_seconds=2, outcome='transient_io', process_gone=True)
        self.assertEqual(self.ledger.get_run(claim.run_id)['state'], 'FAILED')
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(claim.run_id, owner='controller', now=5)

    def test_invalid_numeric_values_do_not_mutate(self):
        for value in (float('nan'), float('inf'), -1, True):
            with self.subTest(value=value), self.assertRaises(LedgerError):
                self.ledger.claim(self.plan.request_id, owner='controller', now=value)
        self.assertEqual(self.ledger.counts()['runs'], 0)

    def test_reopen_preserves_existing_run_without_launch_permission(self):
        claim = self.running()
        reopened = Ledger(self.path)
        self.assertEqual(reopened.get_run(claim.run_id)['attempts'][0]['process'], PROCESS)
        with self.assertRaises(AlreadyClaimed):
            reopened.claim(self.plan.request_id, owner='restarted', now=3)

    def test_multiprocess_duplicate_claim_exactly_one_winner(self):
        ctx = multiprocessing.get_context('spawn')
        start, output = ctx.Event(), ctx.Queue()
        children = [ctx.Process(target=race_claim, args=(self.path, self.plan.request_id, start, output)) for _ in range(4)]
        for child in children:
            child.start()
        start.set()
        results = [output.get(timeout=20) for _ in children]
        for child in children:
            child.join(20)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(sum(result[0] == 'claimed' for result in results), 1)
        self.assertEqual(self.ledger.counts()['runs'], 1)
        self.assertEqual(self.ledger.counts()['attempts'], 1)

    def test_single_instance_flock_excludes_other_process_and_retains_inode(self):
        path = str(Path(self.temp.name) / 'dispatcher.lock')
        ctx, output = multiprocessing.get_context('spawn'), multiprocessing.get_context('spawn').Queue()
        with SingleInstance(path):
            inode = Path(path).stat().st_ino
            child = ctx.Process(target=lock_competitor, args=(path, output))
            child.start()
            self.assertEqual(output.get(timeout=10), 'blocked')
            child.join(10)
            self.assertEqual(child.exitcode, 0)
        with SingleInstance(path):
            self.assertEqual(Path(path).stat().st_ino, inode)

    def test_s01_s02_duplicate_and_existing_run_preserve_attempts(self):
        claim = self.running()
        self.step(claim, 'S01', 'duplicate')
        self.step(claim, 'S02', 'existing_run')
        self.assertEqual(self.ledger.get_run(claim.run_id)['state'], 'RUNNING')
        self.assertEqual(self.ledger.counts()['attempts'], 1)

    def test_s03_to_s05_preflight_and_scope_fail_closed(self):
        for scenario, event, state in [('S03', 'dependency_missing', 'BLOCKED_DEPENDENCY'),
                                       ('S03', 'environment_missing', 'BLOCKED_ENV'),
                                       ('S03', 'resource_missing', 'BLOCKED_RESOURCE'),
                                       ('S04', 'change', 'REPLAN'), ('S04', 'defer', 'DEFERRED'),
                                       ('S04', 'expire', 'EXPIRED'), ('S05', 'scope_changed', 'REPLAN')]:
            with self.subTest(event=event):
                if event != 'dependency_missing':
                    self.setUp()
                claim = self.claim()
                self.assertEqual(self.step(claim, scenario, event)['state'], state)
                with self.assertRaises(InvalidTransition):
                    self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=6)

    def test_s06_to_s08_pause_drain_cancel_and_unknown_ownership(self):
        claim = self.running()
        self.assertEqual(self.step(claim, 'S07', 'drain')['state'], 'DRAINING')
        self.assertEqual(self.step(claim, 'S07', 'cancel')['state'], 'CANCELLING')
        run = self.step(claim, 'S08', 'ownership_unknown')
        self.assertEqual(run['state'], 'BLOCKED_RECONCILING')
        self.assertEqual(self.ledger.machine_owner(), claim.run_id)
        with self.assertRaises(StaleFence):
            self.step(claim, 'S01', 'duplicate')

    def test_s09_correction_and_s10_negative_valid_result_no_retry(self):
        claim = self.running()
        self.ledger.finish_attempt(claim.run_id, claim.fence, now=4, elapsed_seconds=2, outcome='success', process_gone=True)
        self.assertEqual(self.step(claim, 'S09', 'artifact_invalid')['state'], 'CORRECTION')
        self.step(claim, 'S09', 'revalidate')
        result = self.step(claim, 'S10', 'valid_result')
        self.assertEqual(result['science_status'], 'COMPLETE')
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(claim.run_id, owner='improve-ap', now=6)

    def test_s11_s12_head_change_invalidates_gate_and_failed_gate_blocks(self):
        claim = self.validated()
        self.assertEqual(self.step(claim, 'S11', 'head_changed')['state'], 'MERGE_GATE_PENDING')
        self.assertEqual(self.step(claim, 'S12', 'gate_failed')['merge_status'], 'BLOCKED')
        with self.assertRaises(InvalidTransition):
            self.step(claim, 'S14', 'merge_verified')

    def test_s13_external_close_is_not_cancel_or_relaunch(self):
        claim = self.running()
        result = self.step(claim, 'S13', 'external_close')
        self.assertEqual(result['state'], 'EXTERNAL_RECONCILING')
        self.assertEqual(result['science_status'], 'RUNNING')
        self.assertEqual(self.ledger.machine_owner(), claim.run_id)
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(claim.run_id, owner='controller', now=6)

    def test_s14_s15_s16_aftermath_never_repeats_science_or_merge(self):
        claim = self.validated()
        self.step(claim, 'S15', 'publish_failed')
        self.step(claim, 'S15', 'publish_verified')
        self.step(claim, 'S14', 'merge_unknown')
        with self.assertRaises(InvalidTransition):
            self.step(claim, 'S14', 'merge_unknown')
        self.step(claim, 'S14', 'merge_absent')
        self.step(claim, 'S14', 'merge_verified')
        self.step(claim, 'S15', 'close_failed')
        self.step(claim, 'S15', 'close_verified')
        self.step(claim, 'S15', 'report_failed')
        result = self.step(claim, 'S15', 'report_verified')
        expected = {'science_status': 'COMPLETE', 'publication_status': 'PUBLISHED',
                    'merge_status': 'MERGED', 'close_status': 'CLOSED', 'report_status': 'DELIVERED',
                    'storage_status': 'NOT_REQUIRED'}
        self.assertEqual({key: result[key] for key in expected}, expected)
        self.assertEqual(result['attempt_count'], 1)
        self.assertEqual(self.step(claim, 'S16', 'followup_requested')['state'], 'FOLLOWUP_REQUEST')
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(claim.run_id, owner='controller', now=6)

    def test_s17_input_verification_is_required_before_launch(self):
        claim = self.claim()
        self.step(claim, 'S17', 'input_missing')
        with self.assertRaises(InvalidTransition):
            self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=6)
        self.step(claim, 'S20', 'storage_interrupted')
        with self.assertRaises(InvalidTransition):
            self.step(claim, 'S20', 'resume_copy')
        self.step(claim, 'S20', 'resume_fetch')
        self.step(claim, 'S17', 'input_verified')
        self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=6)

    def test_s18_s19_s20_archive_cleanup_are_approval_gated_bookkeeping(self):
        claim = self.validated()
        self.step(claim, 'S18', 'archive_needed')
        with self.assertRaises(InvalidTransition):
            self.step(claim, 'S18', 'archive_verified')
        self.step(claim, 'S18', 'archive_approval_needed')
        self.step(claim, 'S18', 'archive_approved')
        self.step(claim, 'S20', 'storage_interrupted')
        with self.assertRaises(InvalidTransition):
            self.step(claim, 'S20', 'resume_cleanup')
        self.step(claim, 'S20', 'resume_copy')
        self.step(claim, 'S18', 'archive_verified')
        self.step(claim, 'S19', 'cleanup_needed')
        self.step(claim, 'S19', 'cleanup_approved')
        self.step(claim, 'S20', 'storage_interrupted')
        self.step(claim, 'S20', 'resume_cleanup')
        result = self.step(claim, 'S19', 'space_verified')
        self.assertEqual(result['science_status'], 'COMPLETE')
        self.assertEqual(result['attempt_count'], 1)
        self.assertEqual(result['storage_status'], 'SPACE_VERIFIED')

    def test_required_storage_cannot_skip_input_verification(self):
        body = self.plan.to_dict()
        body['request_id'] = 'storage-request'
        body['storage_required'] = True
        plan = Plan.parse(body)
        self.ledger.register_plan(plan, digest=plan.digest(SALT))
        self.ledger.ingest_approval(approval(plan, event_id='storage-approval', nonce='storage-nonce'))
        claim = self.ledger.claim(plan.request_id, owner='controller', now=1)
        with self.assertRaisesRegex(LedgerError, 'input has not been verified'):
            self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=2)
        self.step(claim, 'S17', 'input_missing')
        self.step(claim, 'S17', 'input_verified')
        self.ledger.mark_running(claim.run_id, claim.fence, process=PROCESS, now=6)
        self.assertEqual(self.ledger.get_run(claim.run_id)['state'], 'RUNNING')

    def test_external_close_blocks_retry_after_process_is_gone(self):
        claim = self.running()
        self.step(claim, 'S13', 'external_close')
        self.ledger.finish_attempt(claim.run_id, claim.fence, now=6, elapsed_seconds=2,
                                   outcome='transient_io', process_gone=True)
        self.assertEqual(self.ledger.get_run(claim.run_id)['state'], 'EXTERNAL_RECONCILING')
        self.assertIsNone(self.ledger.machine_owner())
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(claim.run_id, owner='controller', now=7)

    def test_expired_publisher_resumes_only_aftermath_with_fresh_fence(self):
        claim = self.validated()
        self.step(claim, 'S15', 'publish_failed')
        with self.assertRaises(ResourceBusy):
            self.ledger.resume_aftermath(claim.run_id, owner='publisher', now=6)
        recovered = self.ledger.resume_aftermath(claim.run_id, owner='publisher', now=200)
        self.assertGreater(recovered.fence, claim.fence)
        self.assertEqual(recovered.attempt_id, claim.attempt_id)
        with self.assertRaises(StaleFence):
            self.ledger.transition(claim.run_id, claim.fence, 'S15', 'publish_verified', now=201)
        self.ledger.transition(recovered.run_id, recovered.fence, 'S15', 'publish_verified', now=201)
        result = self.ledger.get_run(claim.run_id)
        self.assertEqual(result['science_status'], 'COMPLETE')
        self.assertEqual(result['attempt_count'], 1)
        with self.assertRaises(InvalidTransition):
            self.ledger.retry(claim.run_id, owner='publisher', now=202)

    def test_aftermath_recovery_cannot_take_over_running_process(self):
        claim = self.running()
        with self.assertRaises(InvalidTransition):
            self.ledger.resume_aftermath(claim.run_id, owner='publisher', now=200)
        self.assertEqual(self.ledger.machine_owner(), claim.run_id)
        self.assertEqual(self.ledger.get_run(claim.run_id)['fence'], claim.fence)

    def test_different_requests_compete_for_one_machine_atomically(self):
        body = self.plan.to_dict()
        body['request_id'] = 'other-request'
        other = Plan.parse(body)
        self.ledger.register_plan(other, digest=other.digest(SALT))
        self.ledger.ingest_approval(approval(other, event_id='other-approval', nonce='other-nonce'))
        ctx = multiprocessing.get_context('spawn')
        start, output = ctx.Event(), ctx.Queue()
        children = [ctx.Process(target=race_claim, args=(self.path, request, start, output))
                    for request in (self.plan.request_id, other.request_id)]
        for child in children:
            child.start()
        start.set()
        results = [output.get(timeout=20) for _ in children]
        for child in children:
            child.join(20)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(sum(result[0] == 'claimed' for result in results), 1)
        self.assertEqual(sum(result[1] == 'ResourceBusy' for result in results), 1)
        self.assertEqual(self.ledger.counts()['attempts'], 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM approvals WHERE consumed_by IS NOT NULL').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT SUM(reserved_seconds) FROM runs').fetchone()[0], 5)

    def test_revocation_during_work_wins_over_successful_exit(self):
        claim = self.running()
        self.ledger.ingest_revocation(revocation(self.plan))
        self.ledger.finish_attempt(claim.run_id, claim.fence, now=4, elapsed_seconds=2,
                                   outcome='success', process_gone=True)
        run = self.ledger.get_run(claim.run_id)
        self.assertEqual(run['state'], 'CANCELLED')
        self.assertEqual(run['attempts'][0]['outcome'], 'success')
        with self.assertRaises(InvalidTransition):
            self.step(claim, 'S10', 'valid_result')

    def test_completed_science_is_preserved_but_revocation_blocks_publication(self):
        claim = self.validated()
        self.ledger.ingest_revocation(revocation(self.plan))
        for scenario, event in [('S14', 'merge_unknown'), ('S14', 'merge_verified'),
                                ('S15', 'publish_verified'), ('S11', 'head_changed')]:
            with self.subTest(event=event), self.assertRaises(InvalidTransition):
                self.step(claim, scenario, event)
        self.assertEqual(self.ledger.get_run(claim.run_id)['science_status'], 'COMPLETE')
        self.assertEqual(self.ledger.get_run(claim.run_id)['attempt_count'], 1)

    def test_direct_cancel_persists_through_ownership_reconciliation(self):
        claim = self.running()
        self.step(claim, 'S07', 'cancel')
        self.step(claim, 'S08', 'ownership_unknown')
        self.ledger.reconcile(claim.run_id, owner='replacement', now=6,
                              process_gone=True, elapsed_seconds=2, outcome='success')
        self.assertEqual(self.ledger.get_run(claim.run_id)['state'], 'CANCELLED')

    def test_unknown_and_premature_transitions_fail_without_mutation(self):
        claim = self.claim()
        before = self.ledger.get_run(claim.run_id)
        for scenario, event in [('S99', 'anything'), ('S01', 'launch'),
                                ('S10', 'valid_result'), ('S18', 'archive_needed')]:
            with self.subTest(scenario=scenario), self.assertRaises(InvalidTransition):
                self.step(claim, scenario, event)
        self.assertEqual(self.ledger.get_run(claim.run_id), before)
        self.assertEqual({key[0] for key in SCENARIOS}, {f'S{i:02}' for i in range(1, 21)})


if __name__ == '__main__':
    unittest.main()
