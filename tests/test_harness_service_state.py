"""Offline durable-state tests: no containers, credentials, or network calls."""
import copy
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from research_harness.result_publisher import (
    EXPECTED_CHECKSUM, PublicationRequest, PublicationState,
    ValidatedCanaryResult,
)
from research_harness.schema import ContractError, canonical
from research_harness.restart_boundary import RestartEpoch
from research_harness.service_state import ServiceState, initialize
from research_harness.store import ResourceBusy


def binding_fixture():
    return dict(issue_number=70, issue_content_sha256='d' * 64,
                request_id='request-1', run_id='run-1',
                plan_digest='a' * 64, source_sha='b' * 40)


def request_fixture(binding=None):
    binding = binding or binding_fixture()
    result = ValidatedCanaryResult(
        request_id=binding['request_id'], run_id=binding['run_id'],
        issue_number=binding['issue_number'], plan_digest=binding['plan_digest'],
        source_sha=binding['source_sha'], outcome='success',
        checksum=EXPECTED_CHECKSUM, elapsed_seconds=.01,
        artifact_sha256='f' * 64)
    return PublicationRequest(result, binding['source_sha'], binding['issue_content_sha256'])


class ServiceStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        initialize(self.root)
        self.state = ServiceState(self.root)
        self.addCleanup(self.close_state)
        self.binding = binding_fixture()
        self.request = request_fixture()

    def close_state(self):
        if self.state is not None:
            self.state.close()
            self.state = None

    def reopen(self):
        self.close_state()
        self.state = ServiceState(self.root)
        return self.state

    def seed_receipt(self, content_hash=None):
        self.state.db.execute('INSERT INTO intake VALUES (?,?,?,?,?,?)',
                              (70, self.binding['issue_content_sha256'],
                               'PENDING_TRUSTED_APPROVAL', 0, 1,
                               'PENDING_TRUSTED_APPROVAL'))
        self.state.db.execute('INSERT INTO owner_receipts VALUES (?,?,?,?)',
                              (70, 'IC_OFFLINE_SERVICE_TEST', 'e' * 64,
                               content_hash or self.binding['issue_content_sha256']))
        self.state.db.commit()

    def claimed(self):
        self.seed_receipt()
        self.assertTrue(self.state.claim(self.binding))

    def result_ready(self):
        self.claimed()
        self.state.save_result(70, self.request)

    def published(self):
        self.result_ready()
        publication = PublicationState(self.request.digest, self.request.branch,
                                       'PUBLISHED', 'e' * 40, 123)
        self.state.save_publication(70, publication)
        return publication

    def raw_update(self, sql, parameters=()):
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            db.execute(sql, parameters)

    def assert_reopen_rejected(self):
        self.close_state()
        with self.assertRaises((ContractError, sqlite3.DatabaseError,
                                FileNotFoundError, ValueError, KeyError)):
            opened = ServiceState(self.root)
            opened.close()

    def test_explicit_initialize_and_open_empty_state(self):
        self.assertIsNone(self.state.get(70))
        self.assertEqual(self.state.pending_claims(), [])
        self.assertEqual(list((self.root / 'claims').iterdir()), [])
        self.assertEqual((self.root / 'state.sqlite').stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.reopen().get(70))

    def test_normal_open_never_initializes_empty_directory(self):
        with tempfile.TemporaryDirectory() as empty:
            root = Path(empty)
            root.chmod(0o700)
            with self.assertRaises(FileNotFoundError):
                ServiceState(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_initialize_does_not_reset_existing_volume(self):
        before = (self.root / 'initialized.json').read_bytes()
        with self.assertRaises(ContractError):
            initialize(self.root)
        self.assertEqual((self.root / 'initialized.json').read_bytes(), before)

    def test_initialize_allows_fixed_private_manager_lock(self):
        with tempfile.TemporaryDirectory() as empty:
            root = Path(empty)
            root.chmod(0o700)
            lock = root / 'manager.lock'
            lock.touch(mode=0o600)
            inode = lock.stat().st_ino
            initialize(root)
            opened = ServiceState(root)
            opened.close()
            self.assertEqual(lock.stat().st_ino, inode)

    def test_validated_connection_is_not_reopened_through_intake(self):
        self.close_state()
        with patch('research_harness.service_state.Intake.__init__',
                   side_effect=AssertionError('must retain validated connection')):
            self.state = ServiceState(self.root)
        self.assertIs(self.state.intake.db, self.state.db)

    def test_second_open_is_rejected_until_first_state_is_closed(self):
        self.claimed()
        lock = self.root / 'manager.lock'
        inode = lock.stat().st_ino
        for _ in range(2):
            with self.assertRaises(ResourceBusy):
                opened = ServiceState(self.root)
                opened.close()
        self.assertEqual(self.state.get(70)['phase'], 'CLAIMED')
        self.assertEqual(self.reopen().get(70)['phase'], 'CLAIMED')
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertFalse(self.state.claim(self.binding))

    def test_failed_database_validation_releases_lifetime_lock(self):
        self.close_state()
        self.raw_update('PRAGMA user_version=999')
        with self.assertRaisesRegex(ContractError, 'service database invalid'):
            ServiceState(self.root)
        self.raw_update('PRAGMA user_version=1')
        self.state = ServiceState(self.root)
        self.assertIsNone(self.state.get(70))

    def test_failed_run_validation_releases_lifetime_lock(self):
        self.claimed()
        self.close_state()
        self.raw_update('UPDATE intake SET fingerprint=?', ('0' * 64,))
        with self.assertRaisesRegex(ContractError, 'saved observation missing or changed'):
            ServiceState(self.root)
        self.raw_update('UPDATE intake SET fingerprint=?',
                        (self.binding['issue_content_sha256'],))
        self.state = ServiceState(self.root)
        self.assertEqual(self.state.get(70)['phase'], 'CLAIMED')

    def test_live_policy_rejects_no_epoch_claim_before_creating_witness(self):
        self.close_state()
        self.state = ServiceState(self.root, require_epochs=True)
        self.seed_receipt()
        with self.assertRaisesRegex(ContractError, 'live claim requires runtime epoch'):
            self.state.claim(self.binding)
        self.assertIsNone(self.state.get(70))
        self.assertEqual(list((self.root / 'claims').iterdir()), [])

    def test_live_policy_rejects_legacy_claim_without_epoch_on_open(self):
        self.claimed()
        self.close_state()
        with self.assertRaisesRegex(ContractError, 'live run is missing runtime epoch'):
            ServiceState(self.root, require_epochs=True)
        self.state = ServiceState(self.root)
        self.assertEqual(self.state.get(70)['phase'], 'CLAIMED')

    def test_live_policy_rejects_missing_epoch_for_ready_and_published_results(self):
        self.close_state()
        self.state = ServiceState(self.root, require_epochs=True)
        self.seed_receipt()
        before = RestartEpoch('a' * 64, '11111111-1111-1111-1111-111111111111', 22, 33)
        after = replace(before, pid_namespace_inode=23, pid1_start_ticks=34)
        self.state.claim(self.binding, epoch=before)
        self.state.save_result(70, self.request, await_restart=True)
        self.state.confirm_restart(70, after, deployment_contract_verified=True)
        self.close_state()
        self.state = ServiceState(self.root, require_epochs=True)
        self.assertEqual(self.state.get(70)['phase'], 'RESULT_READY')
        self.close_state()
        self.raw_update('DELETE FROM service_epochs')
        for phase in ('RESULT_READY', 'PUBLISHED'):
            with self.subTest(phase=phase):
                if phase == 'PUBLISHED':
                    publication = PublicationState(self.request.digest, self.request.branch,
                                                   'PUBLISHED', 'e' * 40, 123)
                    self.raw_update('UPDATE service_runs SET phase=?,publication=?',
                                    (phase, canonical(publication.to_dict()).decode()))
                with self.assertRaisesRegex(ContractError, 'live run is missing runtime epoch'):
                    ServiceState(self.root, require_epochs=True)
                with sqlite3.connect(self.root / 'state.sqlite') as db:
                    self.assertEqual(db.execute('SELECT * FROM service_epochs').fetchall(), [])

    def test_epoch_policy_requires_exact_boolean(self):
        self.close_state()
        for policy in (None, 0, 1, 'true'):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ContractError, 'invalid epoch policy'):
                    ServiceState(self.root, require_epochs=policy)

    def test_read_only_policy_requires_exact_boolean(self):
        self.close_state()
        for policy in (None, 0, 1, 'true'):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ContractError, 'invalid read-only policy'):
                    ServiceState(self.root, read_only=policy)

    def test_read_only_state_validates_and_reads_without_allowing_database_mutation(self):
        _, after = self.epoch_claim()
        self.state.save_result(70, self.request, await_restart=True)
        original = self.state.get(70)
        self.close_state()
        def files():
            return {str(path.relative_to(self.root)): path.read_bytes()
                    for path in self.root.rglob('*') if path.is_file()}
        before_files = files()
        validate = ServiceState._validate_runs
        def validated_read_only(state):
            self.assertEqual(state.db.execute('PRAGMA query_only').fetchone(), (1,))
            validate(state)
        with patch.object(ServiceState, '_validate_runs', validated_read_only):
            self.state = ServiceState(self.root, require_epochs=True, read_only=True)
        self.assertEqual(self.state.get(70), original)
        with self.assertRaises(sqlite3.OperationalError):
            self.state.confirm_restart(70, after, deployment_contract_verified=True)
        with self.assertRaises(sqlite3.OperationalError):
            self.state.db.execute("UPDATE service_runs SET phase='BLOCKED' WHERE issue=70")
        self.state.db.rollback()
        # mode=ro also denies writes if trusted code disables query_only.
        self.state.db.execute('PRAGMA query_only=OFF')
        with self.assertRaises(sqlite3.OperationalError):
            self.state.db.execute("UPDATE service_runs SET phase='BLOCKED' WHERE issue=70")
        self.state.db.rollback()
        self.assertEqual(self.state.get(70), original)
        self.close_state()
        self.assertEqual(files(), before_files)
        # Ordinary construction retains its existing writable behavior.
        self.state = ServiceState(self.root, require_epochs=True)
        self.state.confirm_restart(70, after, deployment_contract_verified=True)
        self.assertEqual(self.state.get(70)['phase'], 'RESULT_READY')

    def test_claim_is_durable_and_duplicate_does_not_replace_witness(self):
        self.claimed()
        self.assertEqual(self.state.pending_claims(), [70])
        witness = self.root / 'claims' / '70.json'
        self.assertEqual(witness.read_bytes(), canonical(self.binding))
        inode = witness.stat().st_ino
        self.assertFalse(self.state.claim(dict(self.binding)))
        self.assertEqual(witness.stat().st_ino, inode)
        self.assertEqual(self.reopen().get(70),
                         dict(binding=self.binding, phase='CLAIMED',
                              result=None, publication=None))
        self.assertFalse(self.state.claim(dict(self.binding)))

    def test_claim_requires_matching_frozen_owner_receipt(self):
        with self.assertRaises(ContractError):
            self.state.claim(self.binding)
        self.assertIsNone(self.state.get(70))
        self.assertEqual(list((self.root / 'claims').iterdir()), [])
        self.seed_receipt('0' * 64)
        with self.assertRaises(ContractError):
            self.state.claim(self.binding)
        self.assertIsNone(self.state.get(70))

    def test_claim_requires_existing_intake_observation_before_witness(self):
        self.seed_receipt()
        self.state.db.execute('DELETE FROM intake')
        self.state.db.commit()
        with self.assertRaisesRegex(ContractError, 'claim requires matching observation'):
            self.state.claim(self.binding)
        self.assertIsNone(self.state.get(70))
        self.assertEqual(list((self.root / 'claims').iterdir()), [])
        self.assertEqual(self.state.db.execute('SELECT * FROM intake').fetchall(), [])

    def test_claim_requires_matching_intake_fingerprint_before_witness(self):
        self.seed_receipt()
        self.state.db.execute('UPDATE intake SET fingerprint=?', ('0' * 64,))
        self.state.db.commit()
        with self.assertRaisesRegex(ContractError, 'claim requires matching observation'):
            self.state.claim(self.binding)
        self.assertIsNone(self.state.get(70))
        self.assertEqual(list((self.root / 'claims').iterdir()), [])
        self.assertEqual(self.state.db.execute('SELECT fingerprint FROM intake').fetchall(),
                         [('0' * 64,)])

    def test_claim_rejects_unknown_or_malformed_binding_before_witness(self):
        self.seed_receipt()
        invalid = [
            {**self.binding, 'command': 'untrusted'},
            {key: value for key, value in self.binding.items() if key != 'source_sha'},
            {**self.binding, 'issue_number': True},
            {**self.binding, 'issue_number': '../outside'},
            {**self.binding, 'request_id': '../request'},
            {**self.binding, 'run_id': '../run'},
            {**self.binding, 'plan_digest': 'invalid'},
            {**self.binding, 'source_sha': 'invalid'},
            {**self.binding, 'issue_content_sha256': 'invalid'},
        ]
        for binding in invalid:
            with self.subTest(binding=binding):
                with self.assertRaises(ContractError):
                    self.state.claim(binding)
                self.assertEqual(list((self.root / 'claims').iterdir()), [])

    def test_existing_claim_rejects_different_binding(self):
        self.claimed()
        with self.assertRaises(ContractError):
            self.state.claim({**self.binding, 'run_id': 'another-run'})
        self.assertEqual(self.state.get(70)['binding'], self.binding)

    def test_blocked_claim_stays_blocked_after_restart(self):
        self.claimed()
        self.state.block(70)
        self.assertEqual(self.reopen().get(70)['phase'], 'BLOCKED')
        self.assertFalse(self.state.claim(self.binding))
        self.assertEqual(self.state.pending_claims(), [])

    def epoch_claim(self):
        self.seed_receipt()
        before = RestartEpoch('a' * 64, '11111111-1111-1111-1111-111111111111', 22, 33)
        after = replace(before, pid_namespace_inode=23, pid1_start_ticks=34)
        self.assertTrue(self.state.claim(self.binding, epoch=before))
        return before, after

    def interrupted(self):
        before, after = self.epoch_claim()
        self.assertTrue(self.state.interrupt_after_restart(70, after,
                                                          deployment_contract_verified=True))
        return before, after

    def test_reused_namespace_inode_confirms_result_and_reopens_under_live_policy(self):
        before, _ = self.epoch_claim()
        after = replace(before, pid1_start_ticks=34)
        witness = (self.root / 'claims' / '70.json').read_bytes()
        self.state.save_result(70, self.request, await_restart=True)
        original = self.state.get(70)
        self.state.confirm_restart(70, after, deployment_contract_verified=True)
        self.close_state()
        self.state = ServiceState(self.root, require_epochs=True)
        self.assertEqual(self.state.get(70), {**original, 'phase': 'RESULT_READY'})
        self.assertEqual(self.state.db.execute('SELECT * FROM service_epochs').fetchall(),
                         [(70, canonical(before.to_dict()).decode(), canonical(after.to_dict()).decode())])
        self.assertEqual((self.root / 'claims' / '70.json').read_bytes(), witness)
        self.assertFalse(self.state.claim(self.binding, epoch=after))

    def test_reused_namespace_inode_interrupts_claim_and_reopens_under_live_policy(self):
        before, _ = self.epoch_claim()
        after = replace(before, pid1_start_ticks=34)
        witness = (self.root / 'claims' / '70.json').read_bytes()
        self.assertTrue(self.state.interrupt_after_restart(70, after,
                                                          deployment_contract_verified=True))
        self.close_state()
        self.state = ServiceState(self.root, require_epochs=True)
        self.assertEqual(self.state.get(70), dict(binding=self.binding, phase='INTERRUPTED',
                                                result=None, publication=None))
        self.assertEqual(self.state.db.execute('SELECT * FROM service_epochs').fetchall(),
                         [(70, canonical(before.to_dict()).decode(), canonical(after.to_dict()).decode())])
        self.assertEqual((self.root / 'claims' / '70.json').read_bytes(), witness)
        self.assertFalse(self.state.claim(self.binding, epoch=after))

    def test_restart_confirmation_second_write_failure_cannot_leak_into_later_commit(self):
        before, after = self.epoch_claim()
        self.state.save_result(70, self.request, await_restart=True)
        original = self.state.get(70)
        witness = (self.root / 'claims' / '70.json').read_bytes()
        self.state.db.execute("CREATE TEMP TRIGGER reject_confirmation BEFORE UPDATE ON service_runs "
                              "BEGIN SELECT RAISE(ABORT, 'synthetic phase write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.confirm_restart(70, after, deployment_contract_verified=True)
        self.assertFalse(self.state.db.in_transaction)
        # An unrelated state operation commits on this same connection. It must
        # not persist the preceding failed confirmation's first UPDATE.
        self.state.block(71)
        self.close_state()
        self.state = ServiceState(self.root, require_epochs=True)
        self.assertEqual(self.state.get(70), original)
        self.assertEqual(self.state.db.execute('SELECT * FROM service_epochs').fetchall(),
                         [(70, canonical(before.to_dict()).decode(), None)])
        self.assertEqual((self.root / 'claims' / '70.json').read_bytes(), witness)

    def test_restart_confirmation_requires_both_guarded_updates_to_change_one_row(self):
        before, after = self.epoch_claim()
        self.state.save_result(70, self.request, await_restart=True)
        original = self.state.get(70)
        for table in ('service_epochs', 'service_runs'):
            with self.subTest(table=table):
                self.state.db.execute('CREATE TEMP TRIGGER ignore_confirmation BEFORE UPDATE ON ' + table +
                                      ' BEGIN SELECT RAISE(IGNORE); END')
                with self.assertRaises(ContractError):
                    self.state.confirm_restart(70, after, deployment_contract_verified=True)
                self.assertFalse(self.state.db.in_transaction)
                self.state.db.execute('DROP TRIGGER ignore_confirmation')
                self.state.block(71)
                self.close_state()
                self.state = ServiceState(self.root, require_epochs=True)
                self.assertEqual(self.state.get(70), original)
                self.assertEqual(self.state.db.execute('SELECT * FROM service_epochs').fetchall(),
                                 [(70, canonical(before.to_dict()).decode(), None)])

    def test_interruption_preserves_claim_receipts_schema_and_epoch_history(self):
        before, after = self.epoch_claim()
        witness = self.root / 'claims' / '70.json'
        original_witness, original_inode = witness.read_bytes(), witness.stat().st_ino
        schema = self.state.db.execute('SELECT name,sql FROM sqlite_master').fetchall()
        receipts = self.state.db.execute('SELECT * FROM owner_receipts').fetchall()
        observations = self.state.db.execute('SELECT * FROM intake').fetchall()
        original_epoch = self.state.db.execute('SELECT before_epoch FROM service_epochs').fetchone()
        self.assertTrue(self.state.interrupt_after_restart(70, after,
                                                          deployment_contract_verified=True))
        self.assertEqual(self.reopen().get(70), dict(binding=self.binding, phase='INTERRUPTED',
                                                   result=None, publication=None))
        self.assertEqual(self.state.db.execute('SELECT name,sql FROM sqlite_master').fetchall(), schema)
        self.assertEqual(self.state.db.execute('PRAGMA user_version').fetchone(), (1,))
        self.assertEqual(self.state.db.execute('SELECT * FROM owner_receipts').fetchall(), receipts)
        self.assertEqual(self.state.db.execute('SELECT * FROM intake').fetchall(), observations)
        self.assertEqual(self.state.db.execute('SELECT before_epoch FROM service_epochs').fetchone(),
                         original_epoch)
        self.assertEqual(self.state.db.execute('SELECT * FROM service_epochs').fetchall(),
                         [(70, canonical(before.to_dict()).decode(), canonical(after.to_dict()).decode())])
        self.assertEqual((witness.read_bytes(), witness.stat().st_ino),
                         (original_witness, original_inode))
        self.close_state()
        self.state = ServiceState(self.root, require_epochs=True)
        self.assertEqual(self.state.get(70)['phase'], 'INTERRUPTED')

    def test_interrupted_run_cannot_reclaim_block_save_result_or_publish(self):
        _, after = self.interrupted()
        self.assertEqual(self.state.pending_claims(), [])
        self.assertFalse(self.state.claim(self.binding, epoch=after))
        self.state.block(70)
        self.assertEqual(self.state.get(70)['phase'], 'INTERRUPTED')
        with self.assertRaises(ContractError):
            self.state.save_result(70, self.request, await_restart=True)
        with self.assertRaises(ContractError):
            self.state.confirm_restart(70, after, deployment_contract_verified=True)
        with self.assertRaises(ContractError):
            self.state.interrupt_after_restart(70, after, deployment_contract_verified=True)
        publication = PublicationState(self.request.digest, self.request.branch,
                                       'PUBLISHED', 'e' * 40, 123)
        with self.assertRaises(ContractError):
            self.state.save_publication(70, publication)
        self.assertEqual(self.reopen().get(70), dict(binding=self.binding, phase='INTERRUPTED',
                                                   result=None, publication=None))

    def test_interruption_requires_existing_claim_with_saved_epoch(self):
        after = RestartEpoch('a' * 64, '11111111-1111-1111-1111-111111111111', 23, 34)
        with self.assertRaises(ContractError):
            self.state.interrupt_after_restart(70, after, deployment_contract_verified=True)
        self.claimed()
        self.assertFalse(self.state.interrupt_after_restart(70, after,
                                                           deployment_contract_verified=True))
        self.assertEqual(self.reopen().get(70)['phase'], 'CLAIMED')
        self.state.block(70)
        with self.assertRaises(ContractError):
            self.state.interrupt_after_restart(70, after, deployment_contract_verified=True)
        self.assertEqual(self.state.db.execute('SELECT * FROM service_epochs').fetchall(), [])

    def test_interruption_cannot_consume_saved_result(self):
        _, after = self.epoch_claim()
        self.state.save_result(70, self.request, await_restart=True)
        with self.assertRaises(ContractError):
            self.state.interrupt_after_restart(70, after, deployment_contract_verified=True)
        self.assertEqual(self.state.get(70)['phase'], 'RESULT_AWAITING_RESTART')
        self.assertEqual(self.state.db.execute('SELECT after_epoch FROM service_epochs').fetchone(), (None,))

    def test_interruption_phase_and_epoch_commit_atomically(self):
        before, after = self.epoch_claim()
        self.state.db.execute("CREATE TEMP TRIGGER reject_interruption BEFORE UPDATE ON service_runs "
                              "BEGIN SELECT RAISE(ABORT, 'synthetic phase write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.interrupt_after_restart(70, after, deployment_contract_verified=True)
        self.assertFalse(self.state.db.in_transaction)
        self.assertEqual(self.state.get(70)['phase'], 'CLAIMED')
        self.assertEqual(self.state.db.execute('SELECT * FROM service_epochs').fetchall(),
                         [(70, canonical(before.to_dict()).decode(), None)])
        self.assertEqual(self.reopen().get(70)['phase'], 'CLAIMED')

    def test_interrupted_phase_cannot_be_forged_without_restart_epochs(self):
        self.claimed()
        self.close_state()
        self.raw_update("UPDATE service_runs SET phase='INTERRUPTED'")
        self.assert_reopen_rejected()

    def test_interrupted_reopen_revalidates_restart_evidence(self):
        before, after = self.interrupted()
        self.close_state()
        invalid = [None, '{}', canonical(before.to_dict()).decode(),
                   canonical(replace(after, container_id='b' * 64).to_dict()).decode(),
                   canonical(replace(after, boot_id='22222222-2222-2222-2222-222222222222').to_dict()).decode(),
                   canonical(replace(after, pid1_start_ticks=before.pid1_start_ticks - 1).to_dict()).decode(),
                   canonical(replace(after, pid1_start_ticks=before.pid1_start_ticks).to_dict()).decode()]
        for saved in invalid:
            with self.subTest(epoch=saved):
                self.raw_update('UPDATE service_epochs SET after_epoch=?', (saved,))
                self.assert_reopen_rejected()
        self.raw_update('DELETE FROM service_epochs')
        self.assert_reopen_rejected()

    def test_interrupted_run_rejects_result_and_publication_on_reopen(self):
        self.interrupted()
        self.close_state()
        self.raw_update('UPDATE service_runs SET result=?',
                        (canonical(self.request.to_dict()).decode(),))
        self.assert_reopen_rejected()
        self.raw_update("UPDATE service_runs SET result=NULL,publication='{}'")
        self.assert_reopen_rejected()

    def test_missing_database_is_not_recreated(self):
        self.close_state()
        database = self.root / 'state.sqlite'
        database.unlink()
        self.assert_reopen_rejected()
        self.assertFalse(database.exists())

    def test_corrupt_database_is_not_reset(self):
        self.close_state()
        database = self.root / 'state.sqlite'
        database.write_bytes(b'not a SQLite database')
        self.assert_reopen_rejected()
        self.assertEqual(database.read_bytes(), b'not a SQLite database')

    def test_missing_initialization_marker_is_not_recreated(self):
        self.close_state()
        marker = self.root / 'initialized.json'
        marker.unlink()
        self.assert_reopen_rejected()
        self.assertFalse(marker.exists())

    def test_unknown_database_version_rejected(self):
        self.close_state()
        self.raw_update('PRAGMA user_version=999')
        self.assert_reopen_rejected()

    def test_volume_identity_mismatch_rejected(self):
        self.close_state()
        self.raw_update("UPDATE service_meta SET nonce='other-volume'")
        self.assert_reopen_rejected()

    def test_unknown_schema_object_rejected(self):
        self.close_state()
        self.raw_update('CREATE TABLE unexpected (value TEXT)')
        self.assert_reopen_rejected()

    def test_same_column_names_with_removed_constraints_rejected(self):
        self.close_state()
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            db.execute('DROP TABLE service_runs')
            db.execute('CREATE TABLE service_runs (issue INTEGER,binding TEXT,phase TEXT,'
                       'result TEXT,publication TEXT,notification TEXT)')
        self.assert_reopen_rejected()

    def test_database_permissions_symlink_and_hardlink_rejected(self):
        self.close_state()
        database = self.root / 'state.sqlite'
        database.chmod(0o644)
        self.assert_reopen_rejected()
        database.chmod(0o600)
        extra = self.root / 'other.sqlite'
        os.link(database, extra)
        self.assert_reopen_rejected()
        extra.unlink()
        database.rename(extra)
        database.symlink_to(extra.name)
        self.assert_reopen_rejected()

    def test_missing_claim_witness_blocks_without_repair(self):
        self.claimed()
        self.close_state()
        witness = self.root / 'claims' / '70.json'
        witness.unlink()
        self.assert_reopen_rejected()
        self.assertFalse(witness.exists())

    def test_orphan_witness_blocks_instead_of_dispatching_again(self):
        self.claimed()
        self.close_state()
        self.raw_update('DELETE FROM service_runs')
        self.assert_reopen_rejected()
        self.assertTrue((self.root / 'claims' / '70.json').exists())

    def test_modified_claim_witness_rejected(self):
        self.claimed()
        self.close_state()
        (self.root / 'claims' / '70.json').write_bytes(
            canonical({**self.binding, 'run_id': 'another-run'}))
        self.assert_reopen_rejected()

    def test_claim_witness_symlink_rejected(self):
        self.claimed()
        self.close_state()
        witness = self.root / 'claims' / '70.json'
        witness.rename(self.root / 'saved-witness.json')
        witness.symlink_to('../saved-witness.json')
        self.assert_reopen_rejected()

    def test_missing_frozen_receipt_blocks_without_reconstruction(self):
        self.claimed()
        self.close_state()
        self.raw_update('DELETE FROM owner_receipts')
        self.assert_reopen_rejected()
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            self.assertEqual(db.execute('SELECT * FROM owner_receipts').fetchall(), [])

    def test_missing_intake_observation_blocks_without_reconstruction(self):
        self.claimed()
        self.close_state()
        self.raw_update('DELETE FROM intake')
        self.assert_reopen_rejected()
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            self.assertEqual(db.execute('SELECT * FROM intake').fetchall(), [])
            self.assertEqual(db.execute('SELECT phase FROM service_runs').fetchall(),
                             [('CLAIMED',)])
        self.assertEqual((self.root / 'claims' / '70.json').read_bytes(),
                         canonical(self.binding))

    def test_changed_intake_fingerprint_blocks_without_repair(self):
        self.claimed()
        self.close_state()
        self.raw_update('UPDATE intake SET fingerprint=?', ('0' * 64,))
        self.assert_reopen_rejected()
        with sqlite3.connect(self.root / 'state.sqlite') as db:
            self.assertEqual(db.execute('SELECT fingerprint FROM intake').fetchall(),
                             [('0' * 64,)])

    def test_changed_frozen_content_hash_rejected(self):
        self.claimed()
        self.close_state()
        self.raw_update('UPDATE owner_receipts SET content_hash=?', ('0' * 64,))
        self.assert_reopen_rejected()

    def test_unknown_phase_and_notification_rejected(self):
        self.claimed()
        self.close_state()
        self.raw_update("UPDATE service_runs SET phase='RETRY_AUTOMATICALLY'")
        self.assert_reopen_rejected()
        self.raw_update("UPDATE service_runs SET phase='CLAIMED',notification='SENT'")
        self.assert_reopen_rejected()

    def test_unfinished_claim_cannot_contain_result_or_publication(self):
        self.claimed()
        self.close_state()
        self.raw_update('UPDATE service_runs SET result=?',
                        (canonical(self.request.to_dict()).decode(),))
        self.assert_reopen_rejected()
        self.raw_update("UPDATE service_runs SET result=NULL,publication='{}'")
        self.assert_reopen_rejected()

    def test_result_ready_survives_restart_and_does_not_reclaim(self):
        self.result_ready()
        saved = self.reopen().get(70)
        self.assertEqual(saved['phase'], 'RESULT_READY')
        self.assertEqual(PublicationRequest.from_dict(saved['result']), self.request)
        self.assertFalse(self.state.claim(self.binding))

    def test_result_requires_existing_claim_and_cannot_overwrite(self):
        with self.assertRaises(ContractError):
            self.state.save_result(70, self.request)
        self.result_ready()
        with self.assertRaises(ContractError):
            self.state.save_result(70, self.request)

    def test_result_must_match_every_claim_binding_field(self):
        self.claimed()
        changes = dict(issue_number=71, request_id='other-request', run_id='other-run',
                       plan_digest='0' * 64, source_sha='0' * 40,
                       issue_content_sha256='0' * 64)
        for key, value in changes.items():
            with self.subTest(field=key):
                self.state.db.execute("UPDATE service_runs SET phase='CLAIMED',result=NULL")
                self.state.db.commit()
                changed = request_fixture({**self.binding, key: value})
                with self.assertRaises(ContractError):
                    self.state.save_result(70, changed)
                self.assertEqual(self.state.get(70)['phase'], 'CLAIMED')

    def test_result_publication_base_can_differ_from_immutable_execution_source(self):
        self.claimed()
        request = replace(self.request, base_sha='c' * 40)
        self.state.save_result(70, request)
        saved = self.reopen().get(70)
        self.assertEqual(saved['phase'], 'RESULT_READY')
        self.assertEqual(saved['binding'], self.binding)
        self.assertEqual(saved['result'], request.to_dict())
        self.assertEqual(saved['result']['result']['source_sha'], self.binding['source_sha'])

    def test_reopen_revalidates_every_saved_result_binding_field(self):
        self.result_ready()
        self.close_state()
        changes = dict(issue_number=71, request_id='other-request', run_id='other-run',
                       plan_digest='0' * 64, source_sha='0' * 40,
                       issue_content_sha256='0' * 64)
        for key, value in changes.items():
            with self.subTest(field=key):
                changed = request_fixture({**self.binding, key: value})
                self.raw_update('UPDATE service_runs SET result=?',
                                (canonical(changed.to_dict()).decode(),))
                self.assert_reopen_rejected()

    def test_reopen_revalidates_result_schema_and_checksum(self):
        self.result_ready()
        self.close_state()
        for raw in ('{}', None, '{"result":1,"result":2}'):
            with self.subTest(raw=raw):
                self.raw_update('UPDATE service_runs SET result=?', (raw,))
                self.assert_reopen_rejected()
        changed = copy.deepcopy(self.request.to_dict())
        changed['result']['checksum'] = 1
        self.raw_update('UPDATE service_runs SET result=?', (canonical(changed).decode(),))
        self.assert_reopen_rejected()

    def test_reopen_revalidates_saved_base_commit(self):
        self.result_ready()
        self.close_state()
        for base in (None, True, '', 'main', 'c' * 12, 'C' * 40, 'c' * 40 + '\n'):
            with self.subTest(base=base):
                changed = self.request.to_dict()
                changed['base_sha'] = base
                self.raw_update('UPDATE service_runs SET result=?', (canonical(changed).decode(),))
                self.assert_reopen_rejected()

    def test_publication_journal_digest_binds_the_saved_base_independently_of_source(self):
        self.claimed()
        request = replace(self.request, base_sha='c' * 40)
        self.state.save_result(70, request)
        self.state.save_publication(70, PublicationState(request.digest, request.branch))
        self.close_state()
        changed = replace(request, base_sha='d' * 40)
        self.raw_update('UPDATE service_runs SET result=?', (canonical(changed.to_dict()).decode(),))
        self.assert_reopen_rejected()

    def test_unknown_publication_resumes_without_claiming_again(self):
        self.result_ready()
        publication = PublicationState(self.request.digest, self.request.branch,
                                       'PR_UNKNOWN', 'e' * 40)
        self.state.save_publication(70, publication)
        saved = self.reopen().get(70)
        self.assertEqual(saved['phase'], 'RESULT_READY')
        self.assertEqual(PublicationState.from_dict(saved['publication']), publication)
        self.assertFalse(self.state.claim(self.binding))

    def test_published_result_and_pr_identity_survive_restart(self):
        publication = self.published()
        saved = self.reopen().get(70)
        self.assertEqual(saved['phase'], 'PUBLISHED')
        self.assertEqual(PublicationState.from_dict(saved['publication']), publication)
        self.assertFalse(self.state.claim(self.binding))

    def test_published_requires_saved_published_state(self):
        self.published()
        self.close_state()
        self.raw_update('UPDATE service_runs SET publication=NULL')
        self.assert_reopen_rejected()
        pending = PublicationState(self.request.digest, self.request.branch, 'PR_UNKNOWN', 'e' * 40)
        self.raw_update('UPDATE service_runs SET publication=?',
                        (canonical(pending.to_dict()).decode(),))
        self.assert_reopen_rejected()

    def test_reopen_revalidates_publisher_digest_and_branch(self):
        publication = self.published()
        self.close_state()
        for changed in (replace(publication, request_digest='0' * 64),
                        replace(publication, branch='research-results-i71-' + self.request.digest[:32])):
            with self.subTest(changed=changed):
                self.raw_update('UPDATE service_runs SET publication=?',
                                (canonical(changed.to_dict()).decode(),))
                self.assert_reopen_rejected()

    def test_publication_requires_existing_result(self):
        publication = PublicationState(self.request.digest, self.request.branch)
        with self.assertRaises(ContractError):
            self.state.save_publication(70, publication)
        self.claimed()
        with self.assertRaises(ContractError):
            self.state.save_publication(70, publication)

    def test_publication_must_match_result_digest_and_branch_before_saving(self):
        self.result_ready()
        for publication in (
            PublicationState('0' * 64, self.request.branch),
            PublicationState(self.request.digest, 'research-results-i71-' + self.request.digest[:32]),
        ):
            with self.subTest(publication=publication):
                with self.assertRaises(ContractError):
                    self.state.save_publication(70, publication)
                self.assertIsNone(self.state.get(70)['publication'])


if __name__ == '__main__':
    unittest.main()
