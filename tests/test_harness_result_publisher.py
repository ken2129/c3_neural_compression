"""Offline tests only: no gh authentication, GitHub calls or scientific runs."""
import base64
import copy
from dataclasses import replace
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from research_harness.schema import ContractError, REPOSITORY, canonical
from research_harness.result_publisher import (EXPECTED_CHECKSUM, SCHEMA,
    GhResultTransport, GitHubResultPublisher, PublicationRequest, PublicationState,
    RemoteConflict, TransportUnknown, ValidatedCanaryResult)


def artifact(**changes):
    return dict(schema=SCHEMA, request_id='request-1', run_id='run-1',
                issue_number=70, plan_digest='a' * 64, source_sha='b' * 40,
                outcome='success', checksum=EXPECTED_CHECKSUM, elapsed_seconds=.01,
                **changes)


def result_from(data):
    return ValidatedCanaryResult.from_bytes(canonical(data), request_id='request-1',
        run_id='run-1', issue_number=70, plan_digest='a' * 64, source_sha='b' * 40)


def request_fixture():
    return PublicationRequest(result_from(artifact()), 'c' * 40, 'd' * 64)


def pr_fixture(request, head='e' * 40):
    return dict(number=123, state='open', draft=False, merged=False,
                head=dict(ref=request.branch, sha=head, repo=dict(full_name=REPOSITORY)),
                base=dict(ref='main', sha=request.base_sha, repo=dict(full_name=REPOSITORY)),
                title=request.title, body=request.body,
                html_url=f'https://github.com/{REPOSITORY}/pull/123')


class FakeTransport:
    def __init__(self, request):
        self.request = request
        self.base = request.base_sha
        self.head = None
        self.content = None
        self.pr = None
        self.events = []
        self.modes = {}
        self.commit_valid = True
        self.fail_reads = False

    def _read(self):
        if self.fail_reads:
            raise TransportUnknown('read unavailable')

    def _write(self, name, apply):
        self.events.append(name)
        mode = self.modes.get(name)
        if mode == 'unknown_before':
            raise TransportUnknown('unknown')
        apply()
        if mode == 'unknown_after':
            raise TransportUnknown('unknown')

    def base_sha(self):
        self._read()
        return self.base

    def branch_sha(self, request):
        self._read()
        return self.head

    def create_branch(self, request):
        self._write('branch', lambda: setattr(self, 'head', request.base_sha))

    def create_content(self, request):
        def apply():
            self.head = 'e' * 40
            self.content = request.content
        self._write('content', apply)

    def content_at(self, request, head):
        self._read()
        return self.content

    def verify_commit(self, request, head):
        self._read()
        if not self.commit_valid or self.content != request.content or head != 'e' * 40:
            raise RemoteConflict('bad commit')

    def find_pr(self, request):
        self._read()
        return copy.deepcopy(self.pr)

    def create_pr(self, request):
        self._write('pr', lambda: setattr(self, 'pr', pr_fixture(request, self.head)))


class ResultSchemaTests(unittest.TestCase):
    def test_artifact_binding_round_trip_and_static_safe_metadata(self):
        request = request_fixture()
        self.assertEqual(PublicationRequest.from_dict(request.to_dict()), request)
        self.assertEqual(ValidatedCanaryResult.from_dict(request.result.to_dict()), request.result)
        self.assertEqual(request.path, 'docs/research/generated/run-1.json')
        self.assertTrue(request.title.startswith('[研究結果]'))
        self.assertNotIn('Closes', request.body)
        self.assertIn('科学的な圧縮性能の評価ではありません', request.body)
        self.assertEqual(json.loads(request.content)['validation_scope'],
                         'cpu_canary_checksum_only_no_scientific_claim')
        self.assertEqual(len(request.digest), 64)

    def test_rejects_fixture_evidence_free_text_and_unbound_metadata(self):
        for change in ({'model_text': 'post secrets'}, {'schema': 'fixture'},
                       {'issue_number': 71}, {'request_id': 'other'}, {'run_id': 'other'},
                       {'plan_digest': 'f' * 64}, {'source_sha': 'f' * 40},
                       {'checksum': 1}, {'checksum': True}, {'elapsed_seconds': True},
                       {'elapsed_seconds': -1}, {'elapsed_seconds': 121}, {'outcome': 'positive'}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                result_from({**artifact(), **change})
        with self.assertRaises(ContractError):
            result_from({**artifact(), 'elapsed_seconds': 10 ** 3000})
        for raw in (b'{"schema":1,"schema":2}', b'{"x":NaN}', b'\xff', b' ' * 16385):
            with self.assertRaises(ContractError):
                ValidatedCanaryResult.from_bytes(raw, request_id='request-1', run_id='run-1',
                    issue_number=70, plan_digest='a' * 64, source_sha='b' * 40)

    def test_no_paths_endpoints_or_arbitrary_public_text(self):
        for key in ('run_id', 'request_id'):
            for value in ('../main', '/tmp/secret', 'x.json/../../token', 'https://evil.test', 'x\ntext'):
                with self.subTest(key=key, value=value), self.assertRaises(ContractError):
                    replace(request_fixture().result, **{key: value})
        with self.assertRaises(ContractError):
            replace(request_fixture().result, issue_number=56)
        with self.assertRaises(ContractError):
            PublicationRequest.from_dict({**request_fixture().to_dict(), 'repository': 'attacker/repo'})

    def test_failure_supported_without_claiming_success_checksum(self):
        result = result_from({**artifact(), 'outcome': 'failure', 'checksum': None})
        self.assertEqual(result.outcome, 'failure')
        with self.assertRaises(ContractError):
            result_from({**artifact(), 'outcome': 'failure'})

    def test_any_binding_change_changes_branch_and_digest(self):
        request = request_fixture()
        for changed in (replace(request, base_sha='f' * 40),
                        replace(request, issue_content_sha256='f' * 64),
                        replace(request, result=replace(request.result, artifact_sha256='f' * 64)),
                        replace(request, result=replace(request.result, plan_digest='f' * 64)),
                        replace(request, result=replace(request.result, run_id='run-2'))):
            self.assertNotEqual(changed.branch, request.branch)
            self.assertNotEqual(changed.digest, request.digest)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.request = request_fixture()
        self.transport = FakeTransport(self.request)
        self.saved = []
        self.publisher = GitHubResultPublisher(self.transport, persist_state=self.saved.append)

    def publish(self, state=None):
        return self.publisher.publish(self.request, state, authorized=True, authorize_callback=lambda: True)

    def test_success_journals_before_each_mutation_and_is_idempotent(self):
        def journal(state):
            count = len(self.transport.events)
            if state.phase == 'BRANCH_UNKNOWN':
                self.assertEqual(count, 0)
            if state.phase == 'CONTENT_UNKNOWN':
                self.assertEqual(count, 1)
            if state.phase == 'PR_UNKNOWN':
                self.assertEqual(count, 2)
            self.saved.append(state)
        self.publisher.persist_state = journal
        state = self.publish()
        self.assertEqual(state.phase, 'PUBLISHED')
        self.assertEqual(state.pr_number, 123)
        self.assertEqual(state.url, f'https://github.com/{REPOSITORY}/pull/123')
        self.assertEqual(self.transport.events, ['branch', 'content', 'pr'])
        restored = PublicationState.from_dict(state.to_dict())
        self.assertEqual(self.publish(restored), state)
        self.assertEqual(self.transport.events, ['branch', 'content', 'pr'])
        self.assertNotIn('merge', self.transport.events)
        self.assertNotIn('notification', state.to_dict())

    def test_default_authority_denied_before_any_transport_operation(self):
        state = self.publisher.publish(self.request)
        self.assertEqual(state.phase, 'BLOCKED')
        self.assertEqual(state.reason, 'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.events, [])

    def test_static_authority_cannot_replace_current_approval_callback(self):
        state = self.publisher.publish(self.request, authorized=True)
        self.assertEqual(state.phase, 'BLOCKED')
        self.assertEqual(state.reason, 'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.events, [])

    def test_each_external_write_rechecks_approval_after_intent_is_durable(self):
        phases = ['BRANCH_UNKNOWN', 'CONTENT_UNKNOWN', 'PR_UNKNOWN']
        calls = []
        def approval():
            calls.append(self.saved[-1].phase)
            self.assertEqual(len(self.transport.events), len(calls) - 1)
            return True
        state = self.publisher.publish(self.request, authorized=True, authorize_callback=approval)
        self.assertEqual(state.phase, 'PUBLISHED')
        self.assertEqual(calls, phases)

    def test_revocation_or_source_error_before_each_write_stops_new_writes(self):
        for rejected_index in range(3):
            for denied in (False, None, 1, {'approved': True}, 'raise'):
                with self.subTest(index=rejected_index, denied=denied):
                    self.setUp()
                    calls = [0]
                    def approval():
                        index = calls[0]; calls[0] += 1
                        if index != rejected_index:
                            return True
                        if denied == 'raise':
                            raise ContractError('source unavailable or approval expired')
                        return denied
                    state = self.publisher.publish(self.request, authorized=True, authorize_callback=approval)
                    self.assertEqual(state.phase, 'BLOCKED')
                    self.assertEqual(state.reason, 'AUTHORIZATION_MISSING')
                    self.assertEqual(self.transport.events, ['branch', 'content', 'pr'][:rejected_index])

    def test_revoked_unknown_pr_can_only_read_back_a_preexisting_creation(self):
        self.transport.modes['pr'] = 'unknown_before'
        state = self.publish()
        self.assertEqual(state.phase, 'PR_UNKNOWN')
        def no_approval_calls():
            self.fail('read-only reconciliation must not ask for new write authority')
        unresolved = self.publisher.publish(self.request, state, authorized=False,
                                             authorize_callback=no_approval_calls)
        self.assertEqual(unresolved.phase, 'PR_UNKNOWN')
        self.transport.pr = pr_fixture(self.request)
        recovered = self.publisher.publish(self.request, unresolved, authorized=False,
                                           authorize_callback=no_approval_calls)
        self.assertEqual(recovered.phase, 'PUBLISHED')
        self.assertEqual(self.transport.events, ['branch', 'content', 'pr'])

    def test_revoked_unknown_branch_recovery_never_creates_result_content(self):
        self.transport.modes['branch'] = 'unknown_before'
        state = self.publish()
        absent = self.publisher.publish(self.request, state, authorized=False)
        self.assertEqual(absent.phase, 'BRANCH_UNKNOWN')
        self.transport.head = self.request.base_sha
        recovered = self.publisher.publish(self.request, absent, authorized=False)
        self.assertEqual(recovered.phase, 'BLOCKED')
        self.assertEqual(recovered.reason, 'AUTHORIZATION_MISSING')
        self.assertEqual(self.transport.events, ['branch'])

    def test_unknown_after_each_write_recovers_without_duplicate(self):
        for operation in ('branch', 'content', 'pr'):
            with self.subTest(operation=operation):
                self.setUp()
                self.transport.modes[operation] = 'unknown_after'
                state = self.publish()
                self.assertEqual(state.phase, 'PUBLISHED')
                self.assertEqual(self.transport.events.count(operation), 1)

    def test_unknown_before_each_write_never_retries_on_absence(self):
        for operation, phase in (('branch', 'BRANCH_UNKNOWN'), ('content', 'CONTENT_UNKNOWN'), ('pr', 'PR_UNKNOWN')):
            with self.subTest(operation=operation):
                self.setUp()
                self.transport.modes[operation] = 'unknown_before'
                state = self.publish()
                self.assertEqual(state.phase, phase)
                self.transport.modes.clear()
                for _ in range(3):
                    state = self.publish(PublicationState.from_dict(state.to_dict()))
                self.assertEqual(state.phase, phase)
                self.assertEqual(self.transport.events.count(operation), 1)

    def test_restart_recovers_unknown_after_write_and_read_failure(self):
        original = self.transport.create_pr
        def create(request):
            original(request)
            self.transport.fail_reads = True
            raise TransportUnknown('connection lost after create')
        self.transport.create_pr = create
        state = self.publish()
        self.assertEqual(state.phase, 'PR_UNKNOWN')
        self.transport.fail_reads = False
        restored = GitHubResultPublisher(self.transport, persist_state=self.saved.append)
        state = restored.publish(self.request, PublicationState.from_dict(self.saved[-1].to_dict()), authorized=True, authorize_callback=lambda: True)
        self.assertEqual(state.phase, 'PUBLISHED')
        self.assertEqual(self.transport.events.count('pr'), 1)

    def test_delayed_unknown_write_readback_progresses_without_retry(self):
        for operation in ('branch', 'content', 'pr'):
            with self.subTest(operation=operation):
                self.setUp()
                self.transport.modes[operation] = 'unknown_before'
                state = self.publish()
                self.transport.modes.clear()
                if operation == 'branch':
                    self.transport.head = self.request.base_sha
                elif operation == 'content':
                    self.transport.head = 'e' * 40
                    self.transport.content = self.request.content
                else:
                    self.transport.pr = pr_fixture(self.request)
                state = self.publish(state)
                self.assertEqual(state.phase, 'PUBLISHED')
                self.assertEqual(self.transport.events.count(operation), 1)

    def test_pr_unknown_rechecks_actual_commit_even_with_a_restored_receipt(self):
        self.transport.pr = pr_fixture(self.request)
        self.transport.commit_valid = False
        state = PublicationState(self.request.digest, self.request.branch,
                                 phase='PR_UNKNOWN', head_sha='e' * 40)
        self.assertEqual(self.publish(state).phase, 'BLOCKED')
        self.assertEqual(self.transport.events, [])

    def test_journal_failure_prevents_corresponding_write(self):
        for phase, expected in (('BRANCH_UNKNOWN', []), ('CONTENT_UNKNOWN', ['branch']),
                                ('PR_UNKNOWN', ['branch', 'content'])):
            with self.subTest(phase=phase):
                self.setUp()
                def fail(state):
                    if state.phase == phase:
                        raise OSError('journal unavailable')
                self.publisher.persist_state = fail
                with self.assertRaises(OSError):
                    self.publish()
                self.assertEqual(self.transport.events, expected)

    def test_base_change_blocks_without_publishing(self):
        self.transport.base = 'f' * 40
        state = self.publish()
        self.assertEqual(state.phase, 'BLOCKED')
        self.assertEqual(state.reason, 'BASE_CHANGED')
        self.assertEqual(self.transport.events, [])

    def test_content_and_branch_conflicts_do_not_create_pr(self):
        for alter in ('wrong_content', 'wrong_parent', 'other_branch_head', 'existing_base_path'):
            with self.subTest(alter=alter):
                self.setUp()
                self.transport.head = 'e' * 40
                self.transport.content = self.request.content
                if alter == 'wrong_content':
                    self.transport.content = b'private text'
                elif alter == 'wrong_parent':
                    self.transport.commit_valid = False
                elif alter == 'other_branch_head':
                    self.transport.head = 'f' * 40
                else:
                    self.transport.head = self.request.base_sha
                self.assertEqual(self.publish().phase, 'BLOCKED')
                self.assertEqual(self.transport.events, [])

    def test_existing_exact_pr_is_adopted_without_any_write(self):
        self.transport.head = 'e' * 40
        self.transport.content = self.request.content
        self.transport.pr = pr_fixture(self.request)
        self.assertEqual(self.publish().phase, 'PUBLISHED')
        self.assertEqual(self.transport.events, [])

    def test_existing_pr_must_match_every_binding_and_be_open_nondraft(self):
        changes = [lambda p: p.update(state='closed'), lambda p: p.update(merged=True),
                   lambda p: p.update(draft=True), lambda p: p.update(body='untrusted model prose'),
                   lambda p: p.update(title='wrong'), lambda p: p.update(html_url='https://evil.test'),
                   lambda p: p['head'].update(sha='f' * 40), lambda p: p['head'].update(ref='main'),
                   lambda p: p['base'].update(sha='f' * 40), lambda p: p['base'].update(ref='other'),
                   lambda p: p['head']['repo'].update(full_name='evil/repo'),
                   lambda p: p['base']['repo'].update(full_name='evil/repo')]
        for mutate in changes:
            self.setUp()
            self.transport.head = 'e' * 40
            self.transport.content = self.request.content
            self.transport.pr = pr_fixture(self.request)
            mutate(self.transport.pr)
            self.assertEqual(self.publish().phase, 'BLOCKED')
            self.assertEqual(self.transport.events, [])

    def test_state_mismatch_unknown_fields_and_missing_journal_fail_closed(self):
        state = PublicationState(self.request.digest, self.request.branch)
        with self.assertRaises(ContractError):
            self.publish(replace(state, request_digest='f' * 64))
        with self.assertRaises(ContractError):
            PublicationState.from_dict({**state.to_dict(), 'allow_retry': True})
        with self.assertRaises(ContractError):
            GitHubResultPublisher(self.transport, persist_state=None)
        with self.assertRaises(ContractError):
            replace(state, phase='PUBLISHED')


class GhTransportTests(unittest.TestCase):
    def setUp(self):
        self.request = request_fixture()
        self.transport = GhResultTransport(enabled=True, manager_credentials_isolated=True, runtime_verified=True)

    def test_no_runtime_flag_is_implied_and_no_process_launched_by_default(self):
        with patch.object(GhResultTransport, '_run') as run:
            for configuration in ({}, {'enabled': True}, {'enabled': True, 'manager_credentials_isolated': True},
                                  {'enabled': 1, 'manager_credentials_isolated': True, 'runtime_verified': True}):
                with self.assertRaises(ContractError):
                    GhResultTransport(**configuration).base_sha()
            run.assert_not_called()

    def test_fixed_cli_host_stdin_json_no_login_no_shell_model_text(self):
        with patch.object(self.transport, '_executable', return_value='/usr/bin/gh'), \
             patch.object(self.transport, '_run', return_value=(0, b'HTTP/2.0 201 Created\r\n\r\n{}')) as run:
            self.transport.create_pr(self.request)
        command, data = run.call_args.args
        self.assertEqual(command[:4], ['/usr/bin/gh', 'api', '--hostname', 'github.com'])
        self.assertEqual(command[-2:], ['--input', '-'])
        self.assertIn(f'repos/{REPOSITORY}/pulls', command)
        payload = json.loads(data)
        self.assertIs(payload['draft'], False)
        self.assertTrue(payload['title'].startswith('[研究結果]'))
        self.assertEqual(set(payload), {'title', 'body', 'head', 'base', 'draft'})
        self.assertNotIn(self.request.body, command)

    def test_only_generated_file_allowed_and_no_overwrite_sha_parameter(self):
        with patch.object(self.transport, '_request', return_value={}) as api:
            self.transport.create_content(self.request)
        method, path, body = api.call_args.args
        self.assertEqual(method, 'PUT')
        self.assertEqual(path, '/contents/docs/research/generated/run-1.json')
        self.assertEqual(base64.b64decode(body['content']), self.request.content)
        self.assertEqual(set(body), {'message', 'content', 'branch'})
        class Untrusted:
            path = 'research_harness/controller.py'
        with self.assertRaises(ContractError):
            self.transport.create_content(Untrusted())

    def test_unknown_routes_hosts_methods_and_protected_paths_denied(self):
        for method, route in (('PATCH', '/git/refs/heads/main'), ('PUT', '/pulls/1/merge'),
                              ('POST', '/issues/1/comments'), ('POST', '/graphql'),
                              ('PUT', '/contents/research_harness/controller.py'),
                              ('GET', 'https://evil.test'), ('GET', '/../other'),
                              ('GET', '/contents/docs/research/generated/x.json?ref=main')):
            with self.subTest(route=route), patch.object(self.transport, '_run') as run:
                with self.assertRaises(ContractError):
                    self.transport._request(method, route)
                run.assert_not_called()

    def test_404_and_error_response_handling_is_sanitized(self):
        cases = [(1, b'HTTP/2.0 404 Not Found\r\n\r\n{"message":"secret"}', True),
                 (1, b'HTTP/2.0 403 Forbidden\r\n\r\n{"message":"secret"}', False),
                 (0, b'private credentials in bogus output', False),
                 (0, b'HTTP/2.0 200 OK\r\n\r\n{"x":NaN}', False)]
        with patch.object(self.transport, '_executable', return_value='/usr/bin/gh'):
            for code, output, absent in cases:
                with patch.object(self.transport, '_run', return_value=(code, output)):
                    if absent:
                        self.assertIsNone(self.transport.branch_sha(self.request))
                    else:
                        with self.assertRaises(TransportUnknown) as context:
                            self.transport.branch_sha(self.request)
                        self.assertNotIn('secret', str(context.exception))
                        self.assertNotIn('credentials', str(context.exception))

    def test_environment_does_not_inherit_proxy_debug_path_or_host(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'https://evil.test', 'GH_DEBUG': 'api',
                                     'GH_HOST': 'evil.test', 'PATH': '/untrusted', 'MODEL_TEXT': 'x'}, clear=True):
            env = self.transport._environment()
        self.assertEqual(env['GH_HOST'], 'github.com')
        self.assertNotIn('HTTPS_PROXY', env)
        self.assertNotIn('GH_DEBUG', env)
        self.assertNotIn('MODEL_TEXT', env)
        self.assertNotEqual(env['PATH'], '/untrusted')

    def test_result_commit_requires_one_added_file_exact_parent_and_content(self):
        valid = dict(sha='e' * 40, parents=[dict(sha=self.request.base_sha)],
                     files=[dict(filename=self.request.path, status='added')])
        with patch.object(self.transport, '_request', return_value=valid), \
             patch.object(self.transport, 'content_at', return_value=self.request.content):
            self.transport.verify_commit(self.request, 'e' * 40)
        for data in ({**valid, 'parents': [dict(sha='f' * 40)]},
                     {**valid, 'files': valid['files'] + [dict(filename='README.md', status='modified')]},
                     {**valid, 'files': [dict(filename=self.request.path, status='modified')]},
                     {**valid, 'files': [dict(filename='research_harness/controller.py', status='added')]}):
            with patch.object(self.transport, '_request', return_value=data), self.assertRaises(RemoteConflict):
                self.transport.verify_commit(self.request, 'e' * 40)
        with patch.object(self.transport, '_request', return_value=valid), \
             patch.object(self.transport, 'content_at', return_value=b'private logs'), self.assertRaises(RemoteConflict):
            self.transport.verify_commit(self.request, 'e' * 40)

    def test_content_readback_rejects_malformed_path_size_encoding_or_base64(self):
        valid = dict(type='file', path=self.request.path, encoding='base64',
                     content=base64.b64encode(self.request.content).decode(),
                     size=len(self.request.content))
        with patch.object(self.transport, '_request', return_value=valid):
            self.assertEqual(self.transport.content_at(self.request, 'e' * 40), self.request.content)
        for changes in ({'path': 'private.txt'}, {'size': -1}, {'size': True},
                        {'size': 16385}, {'content': '*notbase64'}, {'encoding': 'none'},
                        {'type': 'symlink'}, {'size': 1}):
            with patch.object(self.transport, '_request', return_value={**valid, **changes}), self.assertRaises(RemoteConflict):
                self.transport.content_at(self.request, 'e' * 40)

    def test_pr_query_includes_closed_records_and_never_uses_returned_urls(self):
        response = pr_fixture(self.request)
        response['url'] = 'https://evil.test/steal'
        with patch.object(self.transport, '_request', side_effect=[[response], response]) as api:
            self.assertEqual(self.transport.find_pr(self.request), response)
        self.assertIn('state=all', api.call_args_list[0].args[1])
        self.assertEqual(api.call_args_list[1].args, ('GET', '/pulls/123'))
        with patch.object(self.transport, '_request', return_value=[response, response]), self.assertRaises(RemoteConflict):
            self.transport.find_pr(self.request)

    def test_process_output_limit_and_timeout_are_enforced_without_network(self):
        with patch('research_harness.result_publisher.MAX_RESPONSE', 1024):
            with self.assertRaises(TransportUnknown):
                GhResultTransport._run([sys.executable, '-I', '-S', '-c',
                                        'import os; os.write(1, b"x" * 2048)'], b'')
        with patch('research_harness.result_publisher.MAX_SECONDS', .05):
            with self.assertRaises(TransportUnknown):
                GhResultTransport._run([sys.executable, '-I', '-S', '-c',
                                        'import time; time.sleep(10)'], b'')
        code, output = GhResultTransport._run([sys.executable, '-I', '-S', '-c',
                                              'import os; os.write(1, b"ok")'], b'')
        self.assertEqual((code, output), (0, b'ok'))


if __name__ == '__main__':
    unittest.main()
