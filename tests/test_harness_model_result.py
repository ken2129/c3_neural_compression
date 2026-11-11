"""Offline candidate-schema/publication tests; no model, credentials or API calls."""
import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import unittest
from unittest.mock import patch

from research_harness.model_result import (
    EXPECTED_CHECKSUM, MAX_ARTIFACT_BYTES, MODEL_RESULT_SCHEMA, ModelCanaryResult,
)
from research_harness.result_publisher import (
    GhResultTransport, GitHubResultPublisher, ModelPublicationRequest,
    PublicationRequest, PublicationState, parse_publication_request,
)
from research_harness.schema import ContractError, REPOSITORY, canonical
from test_harness_result_publisher import FakeTransport, request_fixture


BINDING = dict(request_id='request-1', run_id='model-canary-70', issue_number=70,
               plan_digest='a' * 64, source_sha='b' * 40)
MODEL = 'test-pinned-model-2026-10-01'


def candidate(**changes):
    return ModelCanaryResult(**{
        **BINDING, 'model': MODEL, 'reported_outcome': 'success',
        'reported_checksum': EXPECTED_CHECKSUM, 'command_observed': True,
        'elapsed_seconds': 1.25, **changes,
    })


def artifact(**changes):
    return {**json.loads(candidate().to_bytes()), **changes}


def parse_artifact(raw, **expected_changes):
    return ModelCanaryResult.from_bytes(raw, **{**BINDING, 'model': MODEL, **expected_changes})


def model_request(**changes):
    return ModelPublicationRequest(candidate(**changes), 'c' * 40, 'd' * 64)


class ModelCanarySchemaTests(unittest.TestCase):
    def test_canonical_bytes_and_persistence_round_trip_keep_derived_hash(self):
        result = candidate()
        raw = result.to_bytes()
        self.assertIs(type(raw), bytes)
        self.assertLess(len(raw), MAX_ARTIFACT_BYTES)
        self.assertEqual(result.artifact_sha256, hashlib.sha256(raw).hexdigest())
        self.assertNotIn('artifact_sha256', json.loads(raw))
        self.assertEqual(parse_artifact(raw), result)
        self.assertEqual(ModelCanaryResult.from_dict(result.to_dict()), result)
        self.assertEqual(result.to_bytes(), canonical(json.loads(raw)))

    def test_success_candidate_never_becomes_execution_or_research_proof(self):
        result = candidate()
        self.assertEqual(result.schema, MODEL_RESULT_SCHEMA)
        self.assertEqual(result.status, 'candidate_only')
        self.assertEqual(result.sandbox_mode, 'externalSandbox')
        self.assertIs(result.requires_namespace_restart, True)
        self.assertIs(result.research_success_verified, False)
        with self.assertRaises(FrozenInstanceError):
            result.research_success_verified = True

    def test_all_expected_bindings_and_approved_model_are_independently_checked(self):
        for key, value in (('request_id', 'other'), ('run_id', 'other'),
                           ('issue_number', 71), ('plan_digest', 'e' * 64),
                           ('source_sha', 'e' * 40), ('model', 'other-model')):
            with self.subTest(key=key), self.assertRaises(ContractError):
                parse_artifact(candidate().to_bytes(), **{key: value})
        # Invalid expected manager values cannot exploit Python bool/int equality.
        for key, value in (('issue_number', True), ('issue_number', 56),
                           ('request_id', '../escape'), ('model', 'model-latest')):
            with self.subTest(key=key), self.assertRaises(ContractError):
                parse_artifact(candidate().to_bytes(), **{key: value})

    def test_fixed_security_metadata_cannot_be_upgraded_or_weakened(self):
        for key, values in {
            'schema': ('c3-cpu-canary-result-v1', 1, None),
            'status': ('verified', 'success', False),
            'sandbox_mode': ('workspace-write', 'danger-full-access', None),
            'requires_namespace_restart': (False, 1, 'true', None),
            'research_success_verified': (True, 0, 'false', None),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ContractError):
                    candidate(**{key: value})

    def test_success_report_requires_literal_event_and_expected_integer(self):
        for change in ({'command_observed': False}, {'command_observed': 1},
                       {'command_observed': 'true'}, {'reported_checksum': True},
                       {'reported_checksum': float(EXPECTED_CHECKSUM)},
                       {'reported_checksum': 0}, {'reported_checksum': -1},
                       {'reported_checksum': 2 ** 63}, {'reported_checksum': None},
                       {'reported_outcome': 'verified'}, {'reported_outcome': True}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                candidate(**change)

    def test_failure_can_record_event_absence_without_claiming_checksum(self):
        for observed in (False, True):
            result = candidate(reported_outcome='failure', reported_checksum=None,
                               command_observed=observed)
            self.assertEqual(parse_artifact(result.to_bytes()), result)
            self.assertIs(result.research_success_verified, False)
        for checksum in (0, False, EXPECTED_CHECKSUM):
            with self.subTest(checksum=checksum), self.assertRaises(ContractError):
                candidate(reported_outcome='failure', reported_checksum=checksum)
        with self.assertRaises(ContractError):
            candidate(reported_outcome='failure', reported_checksum=None, command_observed=0)

    def test_elapsed_time_has_strict_finite_runtime_bound(self):
        for value in (0, 0.0, 120, 120.0):
            with self.subTest(value=value):
                self.assertEqual(candidate(elapsed_seconds=value).elapsed_seconds, value)
        for value in (True, False, None, '1', -1, 120.01, float('inf'),
                      float('-inf'), float('nan'), 10 ** 3000):
            with self.subTest(value=repr(value)), self.assertRaises(ContractError):
                candidate(elapsed_seconds=value)

    def test_identifier_digest_model_and_issue_limits(self):
        for key, values in {
            'request_id': ('x' * 65, '../secret', '', 'x\ntext'),
            'run_id': ('/tmp/result', 'a.json/../../secret', 'x' * 65),
            'issue_number': (0, -1, True, 56, 2 ** 31),
            'plan_digest': ('a' * 63, 'A' * 64, None),
            'source_sha': ('b' * 41, 'B' * 40, 0),
            'model': ('', 'x' * 129, '-option', 'x-latest', 'https://example.test',
                      'x\nsecret', 'x y', '日本語', None),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ContractError):
                    candidate(**{key: value})
        self.assertEqual(candidate(model='x' * 128).model, 'x' * 128)
        self.assertEqual(candidate(run_id='x' * 64).run_id, 'x' * 64)

    def test_raw_artifact_rejects_noncanonical_malformed_duplicate_or_large_input(self):
        raw = candidate().to_bytes()
        invalid = (None, '', bytearray(raw), b'', b'\xff', b' ' * (MAX_ARTIFACT_BYTES + 1),
                   raw + b'\n', raw + b' ', b' ' + raw, json.dumps(artifact()).encode(),
                   raw.replace(b'1.25', b'NaN'), raw.replace(b'1.25', b'Infinity'),
                   raw[:-1] + b',"schema":"duplicate"}', b'{}', b'[]', b'null',
                   b'[' * 1500 + b'0' + b']' * 1500,
                   raw.decode().encode('utf-16'))
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ContractError):
                parse_artifact(value)

    def test_no_model_text_logs_paths_commands_credentials_or_unknown_fields(self):
        for key in ('model_text', 'logs', 'command', 'credential', 'auth_token',
                    'output_path', 'repository', 'publish_result_pr', 'artifact_sha256'):
            value = artifact(**{key: 'SENSITIVE_UNTRUSTED_SENTINEL'})
            with self.subTest(key=key):
                with self.assertRaises(ContractError) as error:
                    parse_artifact(canonical(value))
                self.assertNotIn('SENSITIVE_UNTRUSTED_SENTINEL', str(error.exception))
        value = candidate().to_dict()
        with self.assertRaises(ContractError):
            ModelCanaryResult.from_dict({**value, 'logs': 'not allowed'})
        with self.assertRaises(ContractError):
            ModelCanaryResult.from_dict({key: item for key, item in value.items() if key != 'status'})

    def test_saved_digest_is_recomputed_and_cannot_be_selected_by_caller(self):
        result = candidate()
        record = result.to_dict()
        with self.assertRaises(TypeError):
            candidate(artifact_sha256='e' * 64)
        for value in ('e' * 64, None, True):
            with self.subTest(value=value), self.assertRaises(ContractError):
                ModelCanaryResult.from_dict({**record, 'artifact_sha256': value})
        for key, value in (('run_id', 'changed'), ('elapsed_seconds', 2), ('model', 'other-model')):
            with self.subTest(key=key), self.assertRaises(ContractError):
                ModelCanaryResult.from_dict({**record, key: value})
        self.assertNotEqual(replace(result, run_id='changed').artifact_sha256, result.artifact_sha256)


class ModelPublicationSchemaTests(unittest.TestCase):
    def test_closed_union_round_trips_each_type_without_changing_cpu_record(self):
        for request, expected_type in ((request_fixture(), PublicationRequest),
                                       (model_request(), ModelPublicationRequest)):
            with self.subTest(expected_type=expected_type.__name__):
                record = request.to_dict()
                restored = parse_publication_request(record)
                self.assertIs(type(restored), expected_type)
                self.assertEqual(restored, request)
                self.assertEqual(restored.to_dict(), record)
        self.assertNotIn('schema', request_fixture().to_dict()['result'])

    def test_model_and_cpu_schema_types_cannot_be_confused(self):
        cpu = request_fixture()
        model = model_request()
        for parse, value in ((PublicationRequest.from_dict, model.to_dict()),
                             (ModelPublicationRequest.from_dict, cpu.to_dict())):
            with self.assertRaises(ContractError):
                parse(value)
        for record in (None, {}, {'result': model.result.to_dict()},
                       {**model.to_dict(), 'type': 'cpu'},
                       {**model.to_dict(), 'result': []},
                       {**model.to_dict(), 'result': {**model.result.to_dict(), 'schema': 'future-schema'}},
                       {**cpu.to_dict(), 'result': {**cpu.result.to_dict(), 'schema': MODEL_RESULT_SCHEMA}}):
            with self.subTest(record_type=type(record).__name__), self.assertRaises(ContractError):
                parse_publication_request(record)
        with self.assertRaises(ContractError):
            ModelPublicationRequest(cpu.result, cpu.base_sha, cpu.issue_content_sha256)
        with self.assertRaises(ContractError):
            PublicationRequest(model.result, model.base_sha, model.issue_content_sha256)

    def test_static_public_candidate_metadata_and_japanese_summary(self):
        request = model_request()
        value = json.loads(request.content)
        self.assertEqual(value['schema'], 'c3-public-model-canary-result-v1')
        self.assertEqual(value['result_schema'], MODEL_RESULT_SCHEMA)
        self.assertEqual(value['repository'], REPOSITORY)
        self.assertEqual(value['status'], 'candidate_only')
        self.assertIs(value['research_success_verified'], False)
        self.assertIs(value['requires_namespace_restart'], True)
        self.assertEqual(value['artifact_sha256'], request.result.artifact_sha256)
        self.assertEqual(value['validation_scope'],
                         'model_report_candidate_only_no_execution_or_research_success_proof')
        self.assertEqual(request.path, 'docs/research/generated/model-canary-70.json')
        self.assertTrue(request.title.startswith('[研究結果]'))
        self.assertIn('未検証', request.title)
        self.assertIn('candidate_only', request.body)
        self.assertIn('研究の成功、コマンドの実行成功、圧縮性能を検証した結果ではありません', request.body)
        self.assertNotIn('Closes', request.body)
        self.assertLess(len(request.content), MAX_ARTIFACT_BYTES)
        self.assertEqual(set(request.to_dict()), {'result', 'base_sha', 'issue_content_sha256'})

    def test_failure_publication_has_no_success_claim(self):
        request = model_request(reported_outcome='failure', reported_checksum=None,
                                command_observed=False)
        self.assertIn('失敗の申告', request.body)
        self.assertNotIn('成功の申告', request.body)
        self.assertIs(json.loads(request.content)['reported_checksum'], None)

    def test_all_binding_changes_change_publication_digest_and_branch(self):
        request = model_request()
        for changed in (replace(request, base_sha='e' * 40),
                        replace(request, issue_content_sha256='e' * 64),
                        replace(request, result=replace(request.result, run_id='other-run')),
                        replace(request, result=replace(request.result, model='other-model')),
                        replace(request, result=replace(request.result, plan_digest='e' * 64))):
            with self.subTest(digest=changed.digest):
                self.assertNotEqual(changed.digest, request.digest)
                self.assertNotEqual(changed.branch, request.branch)


class ModelPublisherTests(unittest.TestCase):
    def setUp(self):
        self.request = model_request()
        self.transport = FakeTransport(self.request)
        self.saved = []
        self.publisher = GitHubResultPublisher(self.transport, persist_state=self.saved.append)

    def test_publication_reuses_existing_journal_and_is_idempotent(self):
        state = self.publisher.publish(self.request, authorized=True, authorize_callback=lambda: True)
        self.assertEqual(state.phase, 'PUBLISHED')
        self.assertEqual(self.transport.events, ['branch', 'content', 'pr'])
        self.assertEqual([item.phase for item in self.saved],
                         ['BRANCH_UNKNOWN', 'BRANCH_READY', 'CONTENT_UNKNOWN',
                          'CONTENT_READY', 'PR_UNKNOWN', 'PUBLISHED'])
        self.assertEqual(self.transport.content, self.request.content)
        self.assertIs(self.transport.pr['draft'], False)
        restored = parse_publication_request(copy.deepcopy(self.request.to_dict()))
        self.assertEqual(self.publisher.publish(restored, PublicationState.from_dict(state.to_dict())), state)
        self.assertEqual(self.transport.events, ['branch', 'content', 'pr'])

    def test_default_disabled_and_revocation_before_each_write_are_preserved(self):
        state = self.publisher.publish(self.request)
        self.assertEqual(state.reason, 'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.events, [])
        checks = []

        def authorize():
            checks.append(self.saved[-1].phase)
            return len(checks) == 1

        state = self.publisher.publish(self.request, authorized=True, authorize_callback=authorize)
        self.assertEqual(checks, ['BRANCH_UNKNOWN', 'CONTENT_UNKNOWN'])
        self.assertEqual(state.phase, 'BLOCKED')
        self.assertEqual(state.reason, 'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.events, ['branch'])

    def test_unknown_create_readback_after_revocation_never_repeats_model_or_write(self):
        original = self.transport.create_pr

        def uncertain_create(request):
            original(request)
            self.transport.fail_reads = True

        self.transport.create_pr = uncertain_create
        state = self.publisher.publish(self.request, authorized=True, authorize_callback=lambda: True)
        self.assertEqual(state.phase, 'PR_UNKNOWN')
        self.transport.fail_reads = False
        final = self.publisher.publish(self.request, state, authorized=False, authorize_callback=lambda: False)
        self.assertEqual(final.phase, 'PUBLISHED')
        self.assertEqual(self.transport.events, ['branch', 'content', 'pr'])

    def test_exact_closed_union_rejects_subclasses_and_duck_typed_requests(self):
        class ForeignRequest(ModelPublicationRequest):
            pass

        foreign = ForeignRequest(self.request.result, self.request.base_sha,
                                 self.request.issue_content_sha256)
        for value in (foreign, self.request.to_dict(), object()):
            with self.assertRaises(ContractError):
                self.publisher.publish(value, authorized=True, authorize_callback=lambda: True)
            with self.assertRaises(ContractError):
                GhResultTransport._validate_request(value)
        self.assertEqual(self.transport.events, [])

    def test_gh_transport_retains_allowlisted_paths_and_non_draft_candidate_body(self):
        transport = GhResultTransport()
        with patch.object(transport, '_request') as remote:
            transport.create_branch(self.request)
            transport.create_content(self.request)
            transport.create_pr(self.request)
        calls = remote.call_args_list
        self.assertEqual(calls[0].args[:2], ('POST', '/git/refs'))
        self.assertEqual(calls[1].args[:2], ('PUT', '/contents/' + self.request.path))
        self.assertEqual(calls[1].args[2]['message'], 'Model canary candidate for Issue #70')
        self.assertEqual(calls[2].args[:2], ('POST', '/pulls'))
        self.assertEqual(calls[2].args[2], dict(title=self.request.title, body=self.request.body,
                                              head=self.request.branch, base='main', draft=False))


if __name__ == '__main__':
    unittest.main()
