"""Private goal result persistence tests, without model or external writes."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from research_harness.goal_artifacts import snapshot_goal_workspace, collect_goal_changes
from research_harness.goal_result import GoalResult, GoalPublicationRequest
from research_harness.schema import ContractError, canonical
from research_harness.service_state import validate_request

SHA = 'a' * 40


class GoalResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'out').mkdir()
        args = dict(source_sha=SHA, write_scope=['out'], worker_stopped=True)
        baseline = snapshot_goal_workspace(root, **args)
        (root / 'out/result.md').write_text('review these exact bytes\n')
        self.candidate = collect_goal_changes(root, baseline, **args)
        self.result = GoalResult('request-1', 'run-1', 72, 'b' * 64, SHA,
                                 'configured-model-v1', 'completed',
                                 'PRIVATE summary: not public PR text', self.candidate, 12.5)
        self.request = GoalPublicationRequest(self.result, 'c' * 40, 'd' * 64)

    def test_closed_roundtrip_keeps_exact_content_and_private_digest(self):
        parsed = GoalPublicationRequest.from_dict(self.request.to_dict())
        self.assertEqual(parsed, self.request)
        self.assertEqual(parsed.digest, self.request.digest)
        self.assertEqual(parsed.candidate_digest, self.candidate.candidate_digest)
        self.assertEqual(parsed.result.artifact_sha256, self.candidate.candidate_digest)
        self.assertEqual(parsed.result.candidate.changes[0].after.content,
                         b'review these exact bytes\n')
        self.assertFalse(parsed.result.candidate.publish_authorized)
        self.assertTrue(parsed.result.requires_namespace_restart)
        self.assertFalse(parsed.result.research_success_verified)

    def test_public_metadata_does_not_copy_private_summary_or_file_contents(self):
        public = self.request.title + self.request.body
        self.assertNotIn('PRIVATE', public)
        self.assertNotIn('review these exact bytes', public)
        self.assertFalse(hasattr(self.request, 'content'))
        self.assertFalse(hasattr(self.request, 'publish_authorized'))
        self.assertIn(self.candidate.candidate_digest, public)
        changed = replace(self.request, result=replace(self.result, candidate_summary='different private text'))
        self.assertNotEqual(changed.digest, self.request.digest)
        self.assertNotEqual(changed.branch, self.request.branch)

    def test_existing_state_binding_interface_and_source_base_separation(self):
        binding = dict(issue_number=72, request_id='request-1', run_id='run-1',
                       plan_digest='b' * 64, source_sha=SHA,
                       issue_content_sha256='d' * 64)
        validate_request(self.request, binding)
        for key in binding:
            altered = dict(binding)
            altered[key] = 73 if key == 'issue_number' else 'wrong'
            with self.assertRaises(ContractError):
                validate_request(self.request, altered)
        self.assertNotEqual(self.request.base_sha, self.result.source_sha)

    def test_closed_fields_cannot_embed_approval_or_success_claims(self):
        for key in ('publish_authorized', 'content_approved', 'receipt', 'public_summary'):
            value = self.request.to_dict(); value[key] = True
            with self.assertRaises(ContractError):
                GoalPublicationRequest.from_dict(value)
            value = self.result.to_dict(); value[key] = True
            with self.assertRaises(ContractError):
                GoalResult.from_dict(value)
        for key in self.result.to_dict():
            value = self.result.to_dict(); del value[key]
            with self.assertRaises(ContractError):
                GoalResult.from_dict(value)

    def test_result_rejects_invalid_types_bounds_and_bindings(self):
        changes = {
            'schema': ['unknown', None], 'candidate_outcome': ['success', True, ''],
            'requires_namespace_restart': [False, 1],
            'research_success_verified': [True, 0],
            'candidate_summary': [True, 'x' * 4097, '\ud800', '\x00'],
            'elapsed_seconds': [True, -1, 3601, float('nan'), float('inf')],
            'source_sha': ['e' * 40], 'model': ['model-latest'],
            'candidate': [None, {}],
        }
        for key, values in changes.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)), self.assertRaises(ContractError):
                    replace(self.result, **{key: value})
        replace(self.result, elapsed_seconds=900, candidate_outcome='incomplete')

    def test_publication_identity_rejects_invalid_digests(self):
        for key in ('base_sha', 'issue_content_sha256'):
            for value in ('', True, 'A' * (40 if key == 'base_sha' else 64)):
                with self.assertRaises(ContractError):
                    replace(self.request, **{key: value})


if __name__ == '__main__':
    unittest.main()
