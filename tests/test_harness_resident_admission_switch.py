"""OFF races: real protected-file validation/control protocol, modeled UIDs."""
from copy import deepcopy
import io
import unittest
from unittest.mock import Mock, patch

from research_harness.goal_runtime import GoalLinuxUIDBackend, GoalRuntimeBlocked
from research_harness.schema import ContractError, canonical
from test_harness_goal_runtime import GoalAdmissionTests
from test_harness_resident import resident
from tests.test_harness_restart_boundary import epoch


class ResidentAdmissionSwitchTests(unittest.TestCase):
    setUp = GoalAdmissionTests.setUp
    admit = GoalAdmissionTests.admit

    def backend(self):
        return resident.ResidentLinuxBackend(broker_epoch=epoch(), broker_ipc_namespace='ipc:[123]')

    def test_off_before_first_admission_does_not_cache_a_job(self):
        backend = self.backend()
        self.config['enabled'] = False
        with self.assertRaisesRegex(ContractError, 'GOAL_RUNTIME_DISABLED'):
            self.admit(backend)
        self.assertIsNone(backend._admitted_goal_digest)

    def test_off_after_preflight_does_not_reject_second_admission_of_same_job(self):
        backend = self.backend()
        self.admit(backend)
        self.config['enabled'] = False
        self.admit(backend)
        self.assertEqual(backend._admitted_goal_digest, self.request.digest)

    def test_off_does_not_admit_another_plan_on_same_backend_or_a_new_backend(self):
        backend = self.backend()
        self.admit(backend)
        self.config['enabled'] = False
        with self.assertRaisesRegex(ContractError, 'GOAL_RUNTIME_DISABLED'):
            self.admit(self.backend())
        self.request.plan['goal'] += ' Different approved work.'
        self.config['installed_plan_digest'] = self.request.digest
        with self.assertRaisesRegex(ContractError, 'GOAL_RUNTIME_DISABLED'):
            self.admit(backend)

    def test_failed_preflight_does_not_cache_permission_to_ignore_off(self):
        backend = self.backend()
        original = deepcopy(self.manifest)
        self.manifest['files']['data/input.csv']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ContractError, 'GOAL_INPUT_HASH_MISMATCH'):
            self.admit(backend)
        self.assertIsNone(backend._admitted_goal_digest)
        self.manifest = original
        self.config['enabled'] = False
        with self.assertRaisesRegex(ContractError, 'GOAL_RUNTIME_DISABLED'):
            self.admit(backend)

    def test_admitted_job_still_rechecks_model_input_and_risk_consents(self):
        backend = self.backend()
        self.admit(backend)
        self.config['enabled'] = False
        original = deepcopy(self.config)
        self.config['model'] = 'changed-model'
        with self.assertRaisesRegex(ContractError, 'GOAL_OPERATOR_CONFIGURATION_MISMATCH'):
            self.admit(backend)
        self.config = deepcopy(original)
        self.config['risk_acknowledgements']['arbitrary_cpu_commands'] = False
        with self.assertRaisesRegex(ContractError, 'GOAL_OPERATOR_RISK_CONSENT_REQUIRED'):
            self.admit(backend)
        self.config = original
        self.manifest['files']['data/input.csv']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ContractError, 'GOAL_INPUT_HASH_MISMATCH'):
            self.admit(backend)

    def test_nonboolean_off_is_never_accepted_even_after_preflight(self):
        backend = self.backend()
        self.admit(backend)
        for value in (0, 1, 'false', None):
            self.config['enabled'] = value
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.admit(backend)

    def test_legacy_disposable_admission_still_rejects_off_after_success(self):
        backend = GoalLinuxUIDBackend()
        self.admit(backend)
        self.config['enabled'] = False
        with self.assertRaisesRegex(ContractError, 'GOAL_RUNTIME_DISABLED'):
            self.admit(backend)


class ResidentPauseProtocolTests(unittest.TestCase):
    def test_only_closed_off_preflight_reply_becomes_a_pause(self):
        for reply in ({'ready': False, 'reason': 'AUTOMATION_OFF'},
                      {'ready': False}, {'ready': False, 'reason': 'OTHER'},
                      {'ready': False, 'reason': 'AUTOMATION_OFF', 'extra': True}):
            runtime = resident.ResidentGoalPipeRuntime(io.BytesIO(canonical(reply) + b'\n'),
                                                      io.BytesIO(), epoch(), {})
            expected = resident.ResidentExecutionPaused if reply == {'ready': False, 'reason': 'AUTOMATION_OFF'} else ContractError
            with self.subTest(reply=reply), self.assertRaises(expected) as caught:
                runtime.preflight()
            self.assertEqual(type(caught.exception), expected)

    def test_successful_preflight_returns_its_verified_epoch(self):
        runtime = resident.ResidentGoalPipeRuntime(io.BytesIO(b'{"ready":true}\n'),
                                                  io.BytesIO(), epoch(), {})
        self.assertEqual(runtime.preflight(), epoch())

    def test_child_exports_pause_only_for_the_runtime_disabled_reason(self):
        cases = [(GoalRuntimeBlocked('GOAL_RUNTIME_DISABLED'), {'ready': False, 'reason': 'AUTOMATION_OFF'}),
                 (GoalRuntimeBlocked('GOAL_INPUT_HASH_MISMATCH'), {'ready': False}),
                 (RuntimeError('GOAL_RUNTIME_DISABLED'), {'ready': False})]
        for error, expected in cases:
            backend = Mock()
            backend.admit_goal.side_effect = error
            with self.subTest(error=type(error).__name__), patch.object(resident, 'report'):
                self.assertEqual(resident.preflight_reply(backend, object()), expected)

    def test_pause_requires_cleanup_before_propagating_to_manager(self):
        controller = Mock()
        controller.process.side_effect = resident.ResidentExecutionPaused('paused')
        cleanup = Mock()
        with self.assertRaises(resident.ResidentExecutionPaused):
            resident.process_event(controller, {'issue_number': 123}, finish_runtime=cleanup)
        cleanup.assert_called_once_with()

    def test_cleanup_failure_cannot_be_misreported_as_a_pause(self):
        controller = Mock()
        controller.process.side_effect = resident.ResidentExecutionPaused('paused')
        cleanup = Mock(side_effect=resident.IntakeBlocked('CLEANUP_UNVERIFIED'))
        with self.assertRaises(resident.IntakeBlocked):
            resident.process_event(controller, {'issue_number': 123}, finish_runtime=cleanup)


del GoalAdmissionTests

if __name__ == '__main__':
    unittest.main()
