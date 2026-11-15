"""Deployment-owned model selection; no model, network or live configuration."""
from copy import deepcopy
import hashlib
import unittest

from research_harness.goal_plan import resolve_direct_pr_plan, validate_goal_plan
from research_harness.issue_intake import content_hash
from research_harness.schema import ContractError, canonical
import test_harness_direct_pr as direct


class CentralModelPlanTests(unittest.TestCase):
    def resolve(self, plan, model='operator-selected-model'):
        return resolve_direct_pr_plan(plan, direct.SOURCE_SHA, configured_model=model,
            resource_limits=direct.LIMITS, deployment_id='1' * 32)

    def test_omitted_model_uses_configuration_without_mutating_approved_issue_plan(self):
        plan = direct.direct_plan()
        del plan['model']
        before = deepcopy(plan)
        resolved = self.resolve(plan)
        self.assertEqual(resolved['model'], 'operator-selected-model')
        resolved['inputs'].append('different-input')
        self.assertEqual(plan, before)

    def test_matching_legacy_explicit_model_keeps_the_same_plan_digest(self):
        plan = direct.direct_plan()
        self.assertEqual(canonical(self.resolve(plan, plan['model'])), canonical(plan))

    def test_legacy_model_cannot_override_configuration(self):
        for value in ('other-model', None, '', True):
            with self.subTest(model=value):
                plan = direct.direct_plan()
                plan['model'] = value
                with self.assertRaises(ContractError):
                    self.resolve(plan)

    def test_invalid_configuration_never_uses_an_implicit_model(self):
        plan = direct.direct_plan()
        del plan['model']
        for value in (None, '', 'model-latest', 'model with spaces', 3, True):
            with self.subTest(model=value), self.assertRaises(ContractError):
                self.resolve(plan, value)

    def test_resolution_preserves_closed_schema_source_deployment_and_budget_checks(self):
        for change in ({'configured_model': 'untrusted'}, {'goal': None},
                       {'model_usage_approved': False}, {'source_sha': 'f' * 40},
                       {'deployment_id': '2' * 32}, {'max_seconds': 121}):
            with self.subTest(change=change):
                plan = direct.direct_plan()
                del plan['model']
                plan.update(change)
                with self.assertRaises(ContractError):
                    self.resolve(plan)

    def test_internal_worker_plan_still_requires_a_resolved_model(self):
        plan = direct.direct_plan()
        del plan['model']
        with self.assertRaises(ContractError):
            validate_goal_plan(plan, direct.SOURCE_SHA)


class CentralModelInstallationTests(unittest.TestCase):
    def test_initial_plan_uses_model_from_profile_and_binds_resolved_plan_digest(self):
        plan = direct.direct_plan()
        profile = direct.direct_profile(plan)
        model = profile['model']
        del plan['model']
        installed = direct.prepare.installed_profile(profile, plan)
        resolved = dict(plan, model=model)
        self.assertEqual(installed['model'], model)
        self.assertEqual(installed['installed_plan_digest'],
                         hashlib.sha256(canonical(resolved)).hexdigest())
        self.assertNotIn('model', plan)

    def test_installation_rejects_legacy_model_conflicting_with_profile(self):
        plan = direct.direct_plan()
        profile = direct.direct_profile(plan)
        plan['model'] = 'unconfigured-model'
        with self.assertRaises(ContractError):
            direct.prepare.validate_profile(profile, plan)

    def test_legacy_goal_only_contract_still_requires_its_explicit_model(self):
        plan = direct.direct_plan()
        profile = direct.direct_profile(plan)
        plan['kind'] = 'codex-goal-v1'
        del plan['direct_pr']
        del plan['model']
        with self.assertRaises(ContractError):
            direct.prepare.validate_profile(profile, plan)


class CentralModelServiceTests(unittest.TestCase):
    setUp = direct.DirectServiceTests.setUp
    make = direct.DirectServiceTests.make
    assert_not_claimed = direct.DirectServiceTests.assert_not_claimed

    def omit_model(self, *, refresh_receipt=True):
        del self.source.request['plan']['model']
        self.source.render(refresh_receipt=refresh_receipt)

    def test_approved_issue_without_model_dispatches_with_single_configured_value(self):
        self.omit_model()
        issue_hash = content_hash(self.source.get_issue(direct.ISSUE))
        self.profile['model'] = 'operator-selected-model-v2'
        report = self.controller.process(direct.ISSUE)
        self.assertEqual(report['phase'], 'WORKER_FINISHED')
        self.assertEqual(self.runtime.plan['model'], self.profile['model'])
        saved = self.state.get(direct.ISSUE)
        self.assertEqual(saved['result']['model'], self.profile['model'])
        self.assertEqual(saved['binding']['issue_content_sha256'], issue_hash)
        self.assertEqual(saved['binding']['plan_digest'],
                         hashlib.sha256(canonical(self.runtime.plan)).hexdigest())
        self.assertNotIn('model', self.source.request['plan'])

    def test_removing_legacy_model_requires_approval_of_the_changed_issue_bytes(self):
        self.omit_model(refresh_receipt=False)
        with self.assertRaises(ContractError):
            self.controller.process(direct.ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)

    def test_legacy_explicit_override_is_rejected_before_preflight_and_claim(self):
        self.profile['model'] = 'different-installed-model'
        with self.assertRaises(ContractError):
            self.controller.process(direct.ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)

    def test_invalid_configured_model_rejects_model_free_issue_before_claim(self):
        self.omit_model()
        self.profile['model'] = None
        with self.assertRaises(ContractError):
            self.controller.process(direct.ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)

    def test_model_change_during_execution_invalidates_binding_and_blocks_claim(self):
        self.omit_model()
        def changed():
            self.profile['model'] = 'different-installed-model'
            self.now += 6
            self.mono += 6
        self.runtime.on_run = changed
        with self.assertRaises(ContractError):
            self.controller.process(direct.ISSUE)
        self.assertEqual(self.state.get(direct.ISSUE)['phase'], 'BLOCKED')
        self.assertEqual(self.runtime.runs, 1)

    def test_model_omission_does_not_expand_installed_write_scope(self):
        self.omit_model()
        self.source.request['plan']['write_scope'].append('uninstalled')
        self.source.render(refresh_receipt=True)
        with self.assertRaises(ContractError):
            self.controller.process(direct.ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)

    def test_model_omission_does_not_bypass_resource_ceiling(self):
        self.omit_model()
        self.source.request['plan']['max_seconds'] = direct.LIMITS['max_seconds'] + 1
        self.source.render(refresh_receipt=True)
        with self.assertRaises(ContractError):
            self.controller.process(direct.ISSUE)
        self.assert_not_claimed()
        self.assertEqual(self.runtime.preflights, 0)


if __name__ == '__main__':
    unittest.main()
