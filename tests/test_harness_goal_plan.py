"""Goal contract tests are offline: no models, deployments, or publication."""
from copy import deepcopy
import unittest

from research_harness.goal_plan import GOAL_RESOURCE_BUDGET, RESOURCE_BOUNDS, validate_goal_plan
from research_harness.schema import ContractError, canonical

SHA = 'a' * 40


def goal_fixture():
    return dict(kind='codex-goal-v1', deployment_id='1'*32, source_sha=SHA, publish_result_pr=True,
                model='configured-model-v1', sandbox_mode='externalSandbox',
                external_network='enabled', max_seconds=120,
                model_usage_approved=True,
                goal='Inspect the supplied rate audit and summarize CPU-only checks.',
                completion_criteria=['Produce the requested summary from supplied inputs.'],
                inputs=['docs/issue41/existing_rate_audit.json'],
                write_scope=['artifacts'], resource_budget=dict(GOAL_RESOURCE_BUDGET))


class GoalPlanTests(unittest.TestCase):
    def reject(self, plan):
        with self.assertRaises(ContractError):
            validate_goal_plan(plan, SHA)

    def test_valid_plan_is_detached_without_losing_approval_bytes(self):
        original = goal_fixture()
        result = validate_goal_plan(original, SHA)
        self.assertEqual(canonical(original), canonical(result))
        result['inputs'].append('README.md')
        result['completion_criteria'].append('Changed criterion')
        result['resource_budget']['max_processes'] = 1
        self.assertEqual(original, goal_fixture())

    def test_closed_plan_and_budget_fields(self):
        for key in goal_fixture():
            value = goal_fixture()
            del value[key]
            self.reject(value)
        for value in (None, [], 'goal', True):
            self.reject(value)
        value = goal_fixture()
        value['command'] = 'arbitrary command is not a schema field'
        self.reject(value)
        value = goal_fixture()
        value['resource_budget']['cost_usd'] = 10
        self.reject(value)

    def test_new_kind_does_not_reuse_canary_contract(self):
        for kind in ('cpu-canary-v1', 'codex-canary-v1', '', None):
            value = goal_fixture(); value['kind'] = kind
            self.reject(value)

    def test_source_model_and_explicit_policy(self):
        changes = {
            'source_sha': ['b' * 40, 'A' * 40, None],
            'model': ['', 'model-latest', 'model with spaces', 1],
            'publish_result_pr': [False, 1, 'true'],
            'model_usage_approved': [False, 1, 'true'],
            'sandbox_mode': ['workspace-write', 'read-only'],
            'external_network': ['disabled', True],
            'max_seconds': [True, 120.0, 3601, 0],
        }
        for key, alternatives in changes.items():
            for alternative in alternatives:
                with self.subTest(key=key, value=alternative):
                    value = goal_fixture(); value[key] = alternative
                    self.reject(value)
        for sha in ('A' * 40, 'a' * 39, None):
            with self.assertRaises(ContractError):
                validate_goal_plan(goal_fixture(), sha)

    def test_resource_request_bounds_and_strict_types(self):
        for key, (minimum, maximum) in RESOURCE_BOUNDS.items():
            for replacement in (True, float(minimum), minimum - 1, maximum + 1):
                value = goal_fixture()
                target = value if key == 'max_seconds' else value['resource_budget']
                target[key] = replacement
                self.reject(value)
            for replacement in (minimum, maximum):
                value = goal_fixture()
                target = value if key == 'max_seconds' else value['resource_budget']
                target[key] = replacement
                validate_goal_plan(value, SHA)

    def test_operator_ceilings_are_optional_validation_not_execution_authority(self):
        plan = goal_fixture()
        limits = {'max_seconds': plan['max_seconds'], **plan['resource_budget']}
        self.assertEqual(validate_goal_plan(plan, SHA, resource_limits=limits), plan)
        for key in limits:
            smaller = dict(limits); smaller[key] -= 1
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate_goal_plan(plan, SHA, resource_limits=smaller)
            missing = dict(limits); del missing[key]
            with self.assertRaises(ContractError):
                validate_goal_plan(plan, SHA, resource_limits=missing)
        for limits in ({}, {'extra': 1}, True, [],
                       {key: True for key in RESOURCE_BOUNDS}):
            with self.assertRaises(ContractError):
                validate_goal_plan(plan, SHA, resource_limits=limits)
        plan['max_seconds'] = 900
        plan['resource_budget']['cpu_seconds_per_process'] = 600
        validate_goal_plan(plan, SHA)

    def test_goal_and_criteria_bounds(self):
        for text in ('', ' \n', '\x00injection', '\ud800', 'x' * 4097, 12):
            value = goal_fixture(); value['goal'] = text
            self.reject(value)
        for criteria in ([], ['same', 'same'], ['x'] * 17, ['x' * 1025],
                         [''], [True], 'criterion'):
            value = goal_fixture(); value['completion_criteria'] = criteria
            self.reject(value)
        value = goal_fixture(); value['goal'] = '入力を読み、結果をまとめる。\nCPUのみ。'
        validate_goal_plan(value, SHA)

    def test_paths_are_relative_literal_bounded_and_nonsecret(self):
        invalid = ('/etc/passwd', '../escape', 'a/../b', './a', 'a//b', 'a/',
                   'a\\b', '.git/config', 'a/.codex/config', 'a/*', 'a/?',
                   'a/[x]', 'a\x00b', '\ud800', 'a' * 257, None)
        for name in ('inputs', 'write_scope'):
            for path in invalid:
                value = goal_fixture(); value[name] = [path]
                with self.subTest(name=name, path=repr(path)):
                    self.reject(value)
            value = goal_fixture(); value[name] = ['same', 'same']
            self.reject(value)
            value = goal_fixture(); value[name] = 'path'
            self.reject(value)
        value = goal_fixture(); value['inputs'] = []
        validate_goal_plan(value, SHA)
        value['write_scope'] = []
        self.reject(value)
        for key, count in (('inputs', 33), ('write_scope', 9)):
            value = goal_fixture(); value[key] = [f'path-{i}' for i in range(count)]
            self.reject(value)

    def test_aggregate_size_limit(self):
        value = goal_fixture()
        value['goal'] = 'x' * 4096
        value['completion_criteria'] = [str(i) + 'x' * 1020 for i in range(16)]
        self.reject(value)


if __name__ == '__main__':
    unittest.main()


class GoalDeploymentIdentityTests(unittest.TestCase):
    def test_public_nonce_is_required_and_strict(self):
        for value in (None, True, '', '1'*31, '1'*33, 'A'*32, 'z'*32):
            plan = goal_fixture(); plan['deployment_id'] = value
            with self.assertRaises(ContractError): validate_goal_plan(plan, SHA)
        plan = goal_fixture(); del plan['deployment_id']
        with self.assertRaises(ContractError): validate_goal_plan(plan, SHA)

    def test_nonce_mismatch_rejects_even_when_input_source_is_identical(self):
        plan = goal_fixture()
        validate_goal_plan(plan, SHA, deployment_id='1'*32)
        with self.assertRaises(ContractError):
            validate_goal_plan(plan, SHA, deployment_id='2'*32)
        changed = deepcopy(plan); changed['deployment_id'] = '2'*32
        self.assertNotEqual(canonical(plan), canonical(changed))
        validate_goal_plan(changed, SHA, deployment_id='2'*32)
