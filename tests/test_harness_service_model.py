"""Closed model-plan integration using synthetic receipts and offline adapters."""
import copy
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from research_harness.intake_flow_fixture import FIXTURE_NOW
from research_harness.model_result import ModelCanaryResult, EXPECTED_CHECKSUM
from research_harness.result_publisher import (ModelPublicationRequest, PublicationRequest,
    ValidatedCanaryResult, parse_publication_request)
from research_harness.schema import ContractError, canonical
from research_harness.service import Controller, canary_binding, validate_canary_plan
from research_harness.service_state import ServiceState, initialize
from test_harness_restart_boundary import epoch
from test_harness_service import (FakeRuntime, ISSUE, PublisherFactory,
                                   ServiceSource, SOURCE_SHA)


MODEL = 'offline-model-pinned'


def model_plan(**changes):
    return dict(dict(kind='codex-canary-v1', source_sha=SOURCE_SHA,
        publish_result_pr=True, model=MODEL, sandbox_mode='externalSandbox',
        external_network='enabled', max_seconds=120, model_usage_approved=True), **changes)


class ModelSource(ServiceSource):
    def __init__(self):
        super().__init__()
        self.request['plan'] = model_plan()
        self.render(refresh_receipt=True)


class ModelRuntime(FakeRuntime):
    """Models protected deployment admission; never starts a model or process."""
    deployment_contract_verified = True

    def __init__(self):
        super().__init__()
        self.epoch = epoch()
        self.prepared = []
        self.on_prepare = lambda plan: None
        self.deployed_model = MODEL

    def prepare_plan(self, plan):
        if plan['kind'] == 'codex-canary-v1' and plan['model'] != self.deployed_model:
            raise ContractError('offline deployment model mismatch')
        self.prepared.append(copy.deepcopy(plan))
        self.on_prepare(plan)

    def preflight(self):
        super().preflight()
        return self.epoch

    def current_epoch(self):
        return self.epoch

    def run(self, binding, recheck):
        self.runs += 1
        self.seen_bindings.append(copy.deepcopy(binding))
        self.on_run()
        recheck()
        if self.run_error is not None:
            raise self.run_error
        expected = {key: binding[key] for key in ('request_id', 'run_id', 'issue_number',
                                                 'plan_digest', 'source_sha')}
        result = ModelCanaryResult(**expected, model=self.deployed_model,
            reported_outcome='success', reported_checksum=EXPECTED_CHECKSUM,
            command_observed=True, elapsed_seconds=.01)
        self.on_result()
        return self.result_transform(result)


class ModelPlanSchemaTests(unittest.TestCase):
    def test_exact_model_plan_is_copied_and_all_policy_fields_bind_digest(self):
        source = ModelSource()
        plan = source.request['plan']
        self.assertEqual(validate_canary_plan(plan, SOURCE_SHA), plan)
        self.assertIsNot(validate_canary_plan(plan, SOURCE_SHA), plan)
        binding = canary_binding(source.issue, source.request, SOURCE_SHA)
        self.assertEqual(set(binding), {'issue_number', 'issue_content_sha256', 'request_id',
                                        'run_id', 'plan_digest', 'source_sha'})
        self.assertEqual(binding['plan_digest'], hashlib.sha256(canonical(plan)).hexdigest())
        source.request['plan']['model'] = 'other-pinned-model'
        changed = canary_binding(source.issue, source.request, SOURCE_SHA)
        self.assertNotEqual(changed['plan_digest'], binding['plan_digest'])

    def test_unknown_missing_and_unsafe_policy_values_are_rejected(self):
        invalid = [None, [], {**model_plan(), 'command': 'untrusted'},
                   {key: value for key, value in model_plan().items() if key != 'model_usage_approved'}]
        for key, values in {
            'kind': ('arbitrary-research', 'codex-canary-v2'),
            'model': ('', '../model', '--config', 'model name', 'model-latest', 'x' * 65, None),
            'sandbox_mode': ('danger-full-access', 'workspace-write', None),
            'external_network': ('restricted', False, None),
            'max_seconds': (True, 120.0, 0, 119, 121, '120'),
            'model_usage_approved': (False, 1, 'true', None),
            'publish_result_pr': (False, 1),
            'source_sha': ('c' * 40, 'main'),
        }.items():
            invalid.extend(model_plan(**{key: value}) for value in values)
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ContractError):
                validate_canary_plan(plan, SOURCE_SHA)

    def test_cpu_plan_cannot_smuggle_model_policy(self):
        with self.assertRaises(ContractError):
            validate_canary_plan(model_plan(kind='cpu-canary-v1'), SOURCE_SHA)


class ModelControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        initialize(self.root)
        self.state = ServiceState(self.root, require_epochs=True)
        self.addCleanup(lambda: self.state.close())
        self.source = ModelSource()
        self.runtime = ModelRuntime()
        self.publishers = PublisherFactory()
        self.make_controller()

    def make_controller(self):
        self.controller = Controller(self.state, self.source, self.source, self.runtime,
            self.publishers, source_sha=SOURCE_SHA, clock=lambda: FIXTURE_NOW)

    def reopen(self):
        self.state.close()
        self.state = ServiceState(self.root, require_epochs=True)
        self.make_controller()

    def assert_unclaimed(self):
        self.assertIsNone(self.state.get(ISSUE))
        self.assertEqual(list((self.root / 'claims').iterdir()), [])
        self.assertEqual(self.runtime.runs, 0)
        self.assertEqual(self.publishers.calls, 0)

    def test_model_result_is_pending_until_restart_then_publishes_without_rerun(self):
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'RESULT_AWAITING_RESTART')
        self.assertTrue(report['restart_required'])
        self.assertTrue(report['dispatched'])
        self.assertEqual(self.runtime.prepared, [model_plan()])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)
        saved = parse_publication_request(self.state.get(ISSUE)['result'])
        self.assertIs(type(saved), ModelPublicationRequest)
        self.assertEqual(saved.result.status, 'candidate_only')
        self.assertFalse(saved.result.research_success_verified)
        self.reopen()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.publishers.calls, 0)
        self.runtime.epoch = replace(self.runtime.epoch, pid_namespace_inode=4026533002,
                                     pid1_start_ticks=2000)
        self.reopen()
        published = self.controller.process(ISSUE)
        self.assertEqual(published['phase'], 'PUBLISHED')
        self.assertFalse(published['dispatched'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.runtime.prepared, [model_plan()])
        self.assertEqual(self.publishers.transport.events, ['branch', 'content', 'pr'])
        self.reopen()
        self.assertTrue(self.controller.process(ISSUE)['historical'])
        self.assertEqual(self.publishers.calls, 1)

    def test_missing_prepare_callback_blocks_before_preflight_and_claim(self):
        self.runtime = FakeRuntime()
        self.make_controller()
        with self.assertRaisesRegex(ContractError, 'model deployment admission'):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.preflights, 0)
        self.assert_unclaimed()

    def test_operator_model_mismatch_blocks_before_claim(self):
        self.runtime.deployed_model = 'another-pinned-model'
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.preflights, 0)
        self.assert_unclaimed()

    def test_model_runtime_must_supply_actual_typed_epoch_before_claim(self):
        for value in (None, True, epoch().to_dict()):
            with self.subTest(epoch=value):
                self.runtime.epoch = value
                with self.assertRaisesRegex(ContractError, 'verified restart epoch'):
                    self.controller.process(ISSUE)
                self.assert_unclaimed()

    def test_frozen_receipt_is_required_before_runtime_preparation(self):
        self.source.approval_present = False
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.prepared, [])
        self.assert_unclaimed()

    def test_unapproved_model_change_cannot_reuse_the_frozen_receipt(self):
        self.source.request['plan']['model'] = 'another-pinned-model'
        self.source.render(refresh_receipt=False)
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.prepared, [])
        self.assert_unclaimed()

    def test_change_during_preparation_blocks_before_claim(self):
        self.runtime.on_prepare = lambda plan: self.source.revoke()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assert_unclaimed()

    def test_prepare_cannot_mutate_the_controllers_approved_model_copy(self):
        self.runtime.on_prepare = lambda plan: plan.update(model='other-model')
        report = self.controller.process(ISSUE)
        self.assertEqual(report['phase'], 'RESULT_AWAITING_RESTART')
        self.assertEqual(parse_publication_request(self.state.get(ISSUE)['result']).result.model, MODEL)

    def test_model_candidate_for_different_model_is_blocked(self):
        self.runtime.result_transform = lambda result: replace(result, model='other-model')
        with self.assertRaisesRegex(ContractError, 'runtime model differs'):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertIsNone(self.state.get(ISSUE)['result'])
        self.assertEqual(self.publishers.calls, 0)

    def test_model_candidate_for_different_binding_is_blocked(self):
        self.runtime.result_transform = lambda result: replace(result, run_id='other-run')
        with self.assertRaisesRegex(ContractError, 'runtime result binding mismatch'):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(self.publishers.calls, 0)

    def test_cpu_result_cannot_stand_in_for_model_execution(self):
        def cpu(result):
            binding = {key: getattr(result, key) for key in
                       ('request_id', 'run_id', 'issue_number', 'plan_digest', 'source_sha')}
            return ValidatedCanaryResult(**binding, outcome='success', checksum=EXPECTED_CHECKSUM,
                                         elapsed_seconds=.01, artifact_sha256='f' * 64)
        self.runtime.result_transform = cpu
        with self.assertRaisesRegex(ContractError, 'runtime result kind differs'):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(self.publishers.calls, 0)

    def test_model_result_cannot_stand_in_for_cpu_execution(self):
        self.source.request['plan'] = dict(kind='cpu-canary-v1', source_sha=SOURCE_SHA,
                                           publish_result_pr=True)
        self.source.render(refresh_receipt=True)
        with self.assertRaisesRegex(ContractError, 'runtime result kind differs'):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.prepared, [self.source.request['plan']])
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(self.publishers.calls, 0)

    def test_cancellation_after_model_attempt_never_saves_or_publishes_candidate(self):
        self.runtime.on_result = self.source.revoke
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'], 'BLOCKED')
        self.assertIsNone(self.state.get(ISSUE)['result'])
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_restart_does_not_override_revoked_model_approval(self):
        self.controller.process(ISSUE)
        self.source.revoke()
        self.runtime.epoch = replace(self.runtime.epoch, pid_namespace_inode=4026533002,
                                     pid1_start_ticks=2000)
        self.reopen()
        with self.assertRaises(ContractError):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_restart_rechecks_saved_model_identity_against_current_frozen_plan(self):
        self.controller.process(ISSUE)
        saved = parse_publication_request(self.state.get(ISSUE)['result'])
        changed = replace(saved, result=replace(saved.result, model='other-pinned-model'))
        self.state.db.execute('UPDATE service_runs SET result=?',
                              (canonical(changed.to_dict()).decode(),))
        self.state.db.commit()
        self.runtime.epoch = replace(self.runtime.epoch, pid_namespace_inode=4026533002,
                                     pid1_start_ticks=2000)
        self.reopen()
        with self.assertRaisesRegex(ContractError, 'runtime model differs'):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)

    def test_restart_cannot_reinterpret_model_candidate_as_verified_cpu_result(self):
        self.controller.process(ISSUE)
        saved = parse_publication_request(self.state.get(ISSUE)['result'])
        binding = {key: getattr(saved.result, key) for key in
                   ('request_id', 'run_id', 'issue_number', 'plan_digest', 'source_sha')}
        cpu = ValidatedCanaryResult(**binding, outcome='success', checksum=EXPECTED_CHECKSUM,
                                   elapsed_seconds=.01, artifact_sha256='f' * 64)
        changed = PublicationRequest(cpu, saved.base_sha, saved.issue_content_sha256)
        self.state.db.execute('UPDATE service_runs SET result=?',
                              (canonical(changed.to_dict()).decode(),))
        self.state.db.commit()
        self.runtime.epoch = replace(self.runtime.epoch, pid_namespace_inode=4026533002,
                                     pid1_start_ticks=2000)
        self.reopen()
        with self.assertRaisesRegex(ContractError, 'runtime result kind differs'):
            self.controller.process(ISSUE)
        self.assertEqual(self.runtime.runs, 1)
        self.assertEqual(self.publishers.calls, 0)


if __name__ == '__main__':
    unittest.main()
