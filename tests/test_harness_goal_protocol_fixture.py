"""Replay sanitized real 0.160 wire capture, without a process/model/network.

Capture used scripted loopback responses with no auth and managed readOnly.
Both harmless commands were rejected by that sandbox. The runner must preserve
an incomplete candidate, not turn a protocol replay into research success.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from research_harness.goal_plan import GOAL_RESOURCE_BUDGET
from research_harness.goal_runner import GoalAppServerRunner, GoalRequest

FIXTURE = Path(__file__).parent / 'fixtures/goal_app_server_0160.json'


def plan():
    return dict(kind='codex-goal-v1',deployment_id='1'*32, source_sha='a' * 40,
                publish_result_pr=True, model='gpt-6.1-sol',
                sandbox_mode='externalSandbox', external_network='enabled',
                max_seconds=120, model_usage_approved=True,
                goal='Read supplied public observations and produce a summary.',
                completion_criteria=['Preserve all observations.'],
                inputs=['data/public.json'], write_scope=['output'],
                resource_budget=dict(GOAL_RESOURCE_BUDGET))


class Replay:
    def __init__(self, events):
        self.events = deepcopy(events)
        self.sent = []

    def send(self, value):
        self.sent.append(deepcopy(value))

    def receive(self):
        return self.events.pop(0)


class GoalRealProtocolFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text())

    def test_real_multiple_failed_tool_events_preserve_incomplete_candidate(self):
        transport = Replay(self.fixture['events'])
        # Stub admission is ONLY for replay. No process or tool can execute here.
        result = GoalAppServerRunner(enabled=True).run(
            GoalRequest(plan(), 'a' * 40), transport,
            admit=lambda request: None, recheck=lambda: None)
        self.assertEqual(result.outcome, 'incomplete')
        self.assertFalse(result.independently_validated)
        self.assertEqual(transport.events, [])
        completed = [e['params']['item'] for e in self.fixture['events']
                     if e.get('method') == 'item/completed'
                     and e['params']['item']['type'] == 'commandExecution']
        self.assertEqual(len(completed), 2)
        self.assertEqual(len({item['command'] for item in completed}), 2)
        for item in completed:
            self.assertEqual(item['status'], 'failed')
            self.assertEqual(item['exitCode'], 1)
            self.assertIn('app-server socket directory must be a user-owned directory with mode 0700',
                          item['aggregatedOutput'])
        # Outbound replay request is not sent to the real readOnly process.
        self.assertEqual(transport.sent[-1]['params']['sandboxPolicy']['type'], 'externalSandbox')

    def test_capture_metadata_does_not_claim_model_or_boundary_success(self):
        metadata = self.fixture['metadata']
        self.assertEqual(metadata['version'], '0.160.0')
        self.assertTrue(metadata['real_app_server'])
        for field in ('actual_model_inference', 'authorization_header_present',
                      'external_egress_enforced', 'production_boundary_verified',
                      'commands_executed_successfully'):
            self.assertIs(metadata[field], False)
        self.assertEqual(metadata['capture_sandbox'], 'readOnly')
        self.assertEqual(metadata['scripted_loopback_responses'], 3)
        self.assertTrue(metadata['normalizations'])
        self.assertNotIn('/tmp/', FIXTURE.read_text())


if __name__ == '__main__':
    unittest.main()
