"""Fake protocol and local pipes only: no Codex, auth, UID changes or models.

Wire fixtures follow the official rust-v0.160.0 generated types, including the
notification envelope timestamp and nullable message phase (not live captures):
codex-rs/app-server-protocol/schema/typescript/ServerNotificationEnvelope.ts,
MessagePhase.ts, v2/ThreadItem.ts, v2/Turn.ts and v2/ItemStartedNotification.ts.
"""
from copy import deepcopy
from dataclasses import replace
import json
import os
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from research_harness.app_server_adapter import (
    AppServerAdapter, AppServerBlocked, BoundedStdioTransport, CODEX_HOME,
    PROMPT, WORKSPACE, fixed_app_server_request, validate_app_server_request,
    SHELL_BODIES,
)


def response(number, result): return {'id': number, 'result': result}
def event(method, params):
    return {'method': method, 'params': params, 'emittedAtMs': 1792224000123}


def transcript():
    thread = {'id': 'thread-1', 'ephemeral': True, 'cliVersion': '0.160.0',
              'cwd': WORKSPACE, 'turns': []}
    turn = {'id': 'turn-1', 'status': 'inProgress', 'items': [], 'itemsView': 'full',
            'error': None, 'startedAt': 1792224000, 'completedAt': None, 'durationMs': None}
    payload = json.dumps({'schema_version': 'c3-cpu-canary-worker-v1',
                          'outcome': 'success', 'reported_checksum': 333283335000})
    agent = {'type': 'agentMessage', 'id': 'final-1', 'phase': None, 'text': payload,
             'memoryCitation': None, 'delivery': None, 'questions': None}
    command = {'type': 'commandExecution', 'id': 'command-1', 'status': 'completed',
               'command': 'python3 -I -S -c "print(sum(i*i for i in range(10000)))"',
               'cwd': WORKSPACE, 'exitCode': 0, 'aggregatedOutput': '333283335000\n',
               'processId': None, 'source': 'agent', 'commandActions': [],
               'pluginId': None, 'scriptPath': None, 'durationMs': 1}
    return [
        response(1, {'codexHome': CODEX_HOME, 'platformOs': 'linux',
                     'platformFamily': 'unix', 'userAgent': 'codex/0.160.0'}),
        event('thread/started', {'thread': thread}),
        response(2, {'thread': thread, 'model': 'configured-model-v1', 'cwd': WORKSPACE,
                     'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                     'instructionSources': [], 'sandbox': {'type': 'readOnly'}}),
        event('turn/started', {'threadId': 'thread-1', 'turn': turn}),
        response(3, {'turn': turn}),
        event('item/started', {'threadId': 'thread-1', 'turnId': 'turn-1',
                              'startedAtMs': 1792224000001,
                              'item': dict(command, status='inProgress', exitCode=None,
                                           aggregatedOutput=None, durationMs=None)}),
        event('item/completed', {'threadId': 'thread-1', 'turnId': 'turn-1',
                                'completedAtMs': 1792224000002, 'item': command}),
        event('item/started', {'threadId': 'thread-1', 'turnId': 'turn-1',
                              'startedAtMs': 1792224000010, 'item': dict(agent, text='')}),
        event('item/completed', {'threadId': 'thread-1', 'turnId': 'turn-1',
                                'completedAtMs': 1792224000020, 'item': agent}),
        event('turn/completed', {'threadId': 'thread-1', 'turn': dict(
            turn, status='completed', items=[command, agent], completedAt=1792224000,
            durationMs=20)}),
    ]


class FakeTransport:
    def __init__(self, frames=None):
        self.frames = deepcopy(transcript() if frames is None else frames)
        self.sent = []
    def send(self, message): self.sent.append(deepcopy(message))
    def receive(self):
        if not self.frames: raise AppServerBlocked('fake transport closed')
        return self.frames.pop(0)


class AppServerAdapterTest(unittest.TestCase):
    def setUp(self):
        self.request = fixed_app_server_request('configured-model-v1')
        self.transport = FakeTransport()

    def run_fake(self, frames=None, **kwargs):
        return AppServerAdapter(enabled=True).run(self.request,
            self.transport if frames is None else FakeTransport(frames),
            admit_external=kwargs.get('admit', lambda request: None),
            recheck=kwargs.get('recheck', lambda: None))

    def test_disabled_missing_or_boolean_admission_never_initializes(self):
        for adapter, admit in [(AppServerAdapter(), lambda request: None),
                               (AppServerAdapter(enabled=True), None),
                               (AppServerAdapter(enabled=True), True),
                               (AppServerAdapter(enabled=True), lambda request: True)]:
            with self.subTest(admit=admit), self.assertRaises(AppServerBlocked):
                adapter.run(self.request, self.transport, admit_external=admit, recheck=lambda: None)
        self.assertEqual(self.transport.sent, [])

    def test_exact_fixed_launch_request_and_no_api_key_or_arbitrary_input(self):
        self.assertEqual(self.request.argv[:5], ('/opt/codex/bin/codex', 'app-server',
                                               '--listen', 'stdio://', '--strict-config'))
        self.assertNotIn('--ignore-user-config', self.request.argv)
        self.assertIn('forced_login_method="chatgpt"', self.request.argv)
        self.assertIn('cli_auth_credentials_store="file"', self.request.argv)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', self.request.argv)
        for request in (replace(self.request, argv=('sh',)), replace(self.request, cwd='/control'),
                        replace(self.request, codex_home='/control/auth'),
                        replace(self.request, wall_seconds=121),
                        replace(self.request, external_network='restricted')):
            with self.subTest(request=request), self.assertRaises(AppServerBlocked):
                validate_app_server_request(request)
        for model in ('', 'model-latest', 'x;sh', 'model\n'):
            with self.assertRaises(AppServerBlocked): fixed_app_server_request(model)

    def test_success_is_matching_command_candidate_only_pending_restart(self):
        result = self.run_fake()
        self.assertEqual((result.thread_id, result.turn_id), ('thread-1', 'turn-1'))
        self.assertEqual(result.candidate.reported_checksum, 333283335000)
        self.assertFalse(result.candidate.independently_validated)
        self.assertEqual(result.command_exit_code, 0)
        self.assertEqual(result.command_checksum, 333283335000)
        self.assertTrue(result.requires_namespace_restart)
        self.assertFalse(result.cleanup_verified)
        sent = self.transport.sent
        self.assertEqual([item['method'] for item in sent],
                         ['initialize', 'initialized', 'thread/start', 'turn/start'])
        self.assertEqual(sent[1], {'method': 'initialized'})
        self.assertTrue(sent[2]['params']['ephemeral'])
        self.assertEqual(sent[3]['params']['sandboxPolicy'],
                         {'type': 'externalSandbox', 'networkAccess': 'enabled'})
        self.assertEqual(sent[3]['params']['input'],
                         [{'type': 'text', 'text': PROMPT, 'text_elements': []}])

    def test_v0160_timestamped_envelope_and_nullable_or_legacy_phase(self):
        for phase in (None, 'missing', 'final_answer'):
            frames = transcript()
            for index in (7, 8):
                item = frames[index]['params']['item']
                if phase == 'missing': item.pop('phase')
                else: item['phase'] = phase
            with self.subTest(phase=phase):
                result = self.run_fake(frames)
                self.assertEqual(result.candidate.reported_checksum, 333283335000)
                self.assertFalse(result.candidate.independently_validated)
        for timestamp in (None, 0, 2**63 - 1):
            frames = transcript()
            for frame in frames:
                if 'method' in frame:
                    if timestamp is None: frame.pop('emittedAtMs')
                    else: frame['emittedAtMs'] = timestamp
            with self.subTest(timestamp=timestamp):
                self.assertTrue(self.run_fake(frames).requires_namespace_restart)

    def test_invalid_notification_timestamp_blocks_before_model_turn(self):
        for value in (None, True, False, -1, 2**63, 1.0, '1792224000123', {}, []):
            frames = transcript(); frames[1]['emittedAtMs'] = value
            transport = FakeTransport(frames)
            with self.subTest(timestamp=value), self.assertRaisesRegex(AppServerBlocked, 'timestamp'):
                AppServerAdapter(enabled=True).run(self.request, transport,
                    admit_external=lambda request: None, recheck=lambda: None)
            self.assertNotIn('turn/start', [frame.get('method') for frame in transport.sent])
        frames = transcript(); frames[1]['unexpectedEnvelopeKey'] = 1
        with self.assertRaises(AppServerBlocked): self.run_fake(frames)

    def test_unknown_phase_commentary_is_not_evidence_or_an_ambiguous_candidate(self):
        frames = transcript()
        commentary = {'type': 'agentMessage', 'id': 'commentary-1', 'text': 'Running the fixed canary.',
                      'phase': None, 'memoryCitation': None, 'delivery': None, 'questions': None}
        frames[7:7] = [
            event('item/started', {'threadId': 'thread-1', 'turnId': 'turn-1',
                                  'startedAtMs': 1792224000003, 'item': dict(commentary, text='')}),
            event('item/completed', {'threadId': 'thread-1', 'turnId': 'turn-1',
                                    'completedAtMs': 1792224000004, 'item': commentary})]
        frames[-1]['params']['turn']['items'].insert(1, commentary)
        self.assertEqual(self.run_fake(frames).candidate.reported_checksum, 333283335000)
        frames = transcript()
        frames[8]['params']['item']['phase'] = 'commentary'
        with self.assertRaises(AppServerBlocked): self.run_fake(frames)

    def test_multiple_compatible_phase_candidates_and_conflicting_snapshots_block(self):
        for first_phase, second_phase in ((None, None), (None, 'final_answer'),
                                           ('final_answer', None)):
            frames = transcript()
            frames[8]['params']['item']['phase'] = first_phase
            extra = dict(frames[8]['params']['item'], id='final-2', phase=second_phase)
            frames[-1:-1] = [
                event('item/started', {'threadId': 'thread-1', 'turnId': 'turn-1',
                                      'startedAtMs': 1792224000021, 'item': dict(extra, text='')}),
                event('item/completed', {'threadId': 'thread-1', 'turnId': 'turn-1',
                                        'completedAtMs': 1792224000022, 'item': extra})]
            frames[-1]['params']['turn']['items'].append(extra)
            with self.subTest(phases=(first_phase, second_phase)), self.assertRaisesRegex(
                    AppServerBlocked, 'ambiguous'):
                self.run_fake(frames)
        frames = transcript()
        frames[-1]['params']['turn']['items'] = deepcopy(frames[-1]['params']['turn']['items'])
        frames[-1]['params']['turn']['items'][-1]['text'] = '{}'
        with self.assertRaisesRegex(AppServerBlocked, 'snapshot'):
            self.run_fake(frames)

    def test_configuration_and_instruction_mismatches_block_before_turn(self):
        for location, key, value in [(0, 'codexHome', '/home/elsewhere'),
                                    (2, 'instructionSources', ['/work/canary/AGENTS.md']),
                                    (2, 'approvalPolicy', 'on-request'), (2, 'model', 'other')]:
            frames = transcript(); frames[location]['result'][key] = value
            transport = FakeTransport(frames)
            with self.subTest(key=key), self.assertRaises(AppServerBlocked):
                AppServerAdapter(enabled=True).run(self.request, transport,
                    admit_external=lambda request: None, recheck=lambda: None)
            self.assertNotIn('turn/start', [item.get('method') for item in transport.sent])

    def test_response_and_event_ids_status_phase_and_final_are_strict(self):
        mutations = [
            lambda f: f[0].update(id=True),
            lambda f: f[4]['result']['turn'].update(id='other-turn'),
            lambda f: f[5]['params'].update(threadId='other-thread'),
            lambda f: f[5]['params'].update(turnId='other-turn'),
            lambda f: f[8]['params']['item'].update(phase='unknown-invalid-phase'),
            lambda f: f[9]['params']['turn'].update(status='failed'),
            lambda f: f[9]['params']['turn'].update(error={'message': 'failure'}),
            lambda f: f.insert(7, deepcopy(f[6])),
            lambda f: f[8]['params']['item'].update(text='{}'),
        ]
        for mutate in mutations:
            frames = transcript(); mutate(frames)
            with self.subTest(mutation=mutate), self.assertRaises(AppServerBlocked):
                self.run_fake(frames)

    def test_server_requests_are_rejected_never_approved(self):
        for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                       'item/tool/requestUserInput', 'mcpServer/elicitation/request',
                       'account/chatgptAuthTokens/refresh', 'item/tool/call'):
            transport = FakeTransport([{'id': 'server-1', 'method': method, 'params': {}}])
            with self.subTest(method=method), self.assertRaises(AppServerBlocked):
                AppServerAdapter(enabled=True).run(self.request, transport,
                    admit_external=lambda request: None, recheck=lambda: None)
            self.assertEqual(transport.sent[-1]['error']['code'], -32601)
            self.assertNotIn('result', transport.sent[-1])

    def test_missing_failed_or_nonliteral_command_is_not_success(self):
        mutations = [lambda f: f.pop(6), lambda f: f.pop(5),
                     lambda f: f[6]['params']['item'].update(exitCode=1),
                     lambda f: f[6]['params']['item'].update(exitCode=True),
                     lambda f: f[6]['params']['item'].update(command='python3 -c "print(7)"'),
                     lambda f: f[6]['params']['item'].update(cwd='/control'),
                     lambda f: f[6]['params']['item'].update(aggregatedOutput='7\n'),
                     lambda f: f[6]['params']['item'].update(status='inProgress')]
        for mutate in mutations:
            frames = transcript(); mutate(frames)
            with self.subTest(mutation=mutate), self.assertRaises(AppServerBlocked):
                self.run_fake(frames)

    def test_only_known_shell_wrappers_of_exact_literal_body_are_accepted(self):
        for shell in ('/bin/bash', '/bin/sh'):
            for mode in ('-c', '-lc'):
                for body in SHELL_BODIES:
                    command = shlex.join((shell, mode, body))
                    frames = transcript()
                    frames[5]['params']['item']['command'] = command
                    frames[6]['params']['item']['command'] = command
                    with self.subTest(shell=shell, mode=mode, body=body):
                        result = self.run_fake(frames)
                        self.assertEqual(result.observed_command, command)

    def test_shell_metacharacters_substitution_newlines_and_extra_args_rejected(self):
        body = sorted(SHELL_BODIES)[0]
        bodies = (body + '; id', body + ' && true', body + ' > /tmp/out',
                  body + '\ntrue', body.replace(' -I', '\n-I'),
                  body + ' $(id)', body + ' `id`', 'exec ' + body)
        commands = [shlex.join(('/bin/bash', '-lc', value)) for value in bodies]
        commands += [shlex.join(('/usr/bin/bash', '-lc', body)),
                     shlex.join(('/bin/bash', '-lc', body, 'extra'))]
        for command in commands:
            frames = transcript(); frames[5]['params']['item']['command'] = command
            with self.subTest(command=command), self.assertRaises(AppServerBlocked):
                self.run_fake(frames)

    def test_cancel_errors_and_unexpected_tools_block(self):
        with self.assertRaisesRegex(AppServerBlocked, 'cancelled'):
            self.run_fake(recheck=Mock(side_effect=AppServerBlocked('cancelled')))
        for frame in (event('error', {'willRetry': True}),
                      {'id': 1, 'error': {'code': -1, 'message': 'do not log me'}}):
            with self.assertRaises(AppServerBlocked): self.run_fake([frame])
        frames = transcript(); frames[5]['params']['item']['type'] = 'webSearch'
        with self.assertRaises(AppServerBlocked): self.run_fake(frames)

    def test_extra_command_start_unfinished_item_or_snapshot_is_rejected(self):
        frames = transcript()
        extra = deepcopy(frames[5]); extra['params']['item']['id'] = 'command-2'
        frames.insert(7, extra)
        with self.assertRaises(AppServerBlocked): self.run_fake(frames)
        for extra_item in ({'type': 'webSearch', 'id': 'web-1'},
                           dict(transcript()[6]['params']['item'], id='command-2')):
            frames = transcript(); frames[-1]['params']['turn']['items'].append(extra_item)
            with self.assertRaises(AppServerBlocked): self.run_fake(frames)

    def test_adapter_cannot_reuse_after_one_attempt(self):
        adapter = AppServerAdapter(enabled=True)
        adapter.run(self.request, self.transport, admit_external=lambda request: None, recheck=lambda: None)
        with self.assertRaisesRegex(AppServerBlocked, 'reused'):
            adapter.run(self.request, FakeTransport(), admit_external=lambda request: None, recheck=lambda: None)

    def test_pre_spawn_admission_is_one_shot_and_request_identity_bound(self):
        adapter = AppServerAdapter(enabled=True)
        admit = Mock(return_value=None)
        adapter.admit(self.request, admit_external=admit, recheck=lambda: None)
        with self.assertRaisesRegex(AppServerBlocked, 'binding mismatch'):
            adapter.run(replace(self.request), FakeTransport(), recheck=lambda: None)
        adapter.run(self.request, FakeTransport(), recheck=lambda: None)
        admit.assert_called_once_with(self.request)


class BoundedStdioTransportTest(unittest.TestCase):
    def pipes(self, stdout=b'', stderr=b''):
        stdin_read, stdin_write = os.pipe()
        streams = [os.fdopen(stdin_write, 'wb')]
        fds = [stdin_read]
        for data in (stdout, stderr):
            read, write = os.pipe()
            if data: os.write(write, data)
            os.close(write)
            streams.append(os.fdopen(read, 'rb'))
        self.addCleanup(lambda: [stream.close() for stream in streams])
        self.addCleanup(lambda: [os.close(fd) for fd in fds])
        return SimpleNamespace(stdin=streams[0], stdout=streams[1], stderr=streams[2],
                               poll=lambda: None), stdin_read

    def transport(self, process, **kwargs):
        result = BoundedStdioTransport(process, recheck=kwargs.pop('recheck', lambda: None),
            deadline=kwargs.pop('deadline', 10), clock=kwargs.pop('clock', lambda: 0), **kwargs)
        self.addCleanup(result.close)
        return result

    def test_binary_roundtrip_with_separate_bounded_stderr(self):
        process, read = self.pipes(b'{"id":1,"result":{}}\n', b'diagnostic')
        transport = self.transport(process)
        transport.send({'method': 'initialized'})
        self.assertEqual(json.loads(os.read(read, 100)), {'method': 'initialized'})
        self.assertEqual(transport.receive(), {'id': 1, 'result': {}})
        self.assertGreaterEqual(transport.total, 21)

    def test_frame_combined_output_and_malformed_json_are_rejected(self):
        for stdout, stderr, kwargs in [(b'x' * 33, b'', {'max_frame_bytes': 32}),
                                      (b'{}\n', b'x' * 8, {'max_capture_bytes': 8}),
                                      (b'{"x":1,"x":2}\n', b'', {}),
                                      (b'{"x":1e9999}\n', b'', {}),
                                      (b'[]\n', b'', {}), (b'\xff\n', b'', {}),
                                      (b'partial', b'', {})]:
            process, _ = self.pipes(stdout, stderr)
            with self.subTest(stdout=stdout), self.assertRaises(AppServerBlocked):
                self.transport(process, **kwargs).receive()

    def test_read_and_write_honor_deadline_and_cancellation(self):
        process, _ = self.pipes(b'{}\n')
        with self.assertRaisesRegex(AppServerBlocked, 'deadline'):
            self.transport(process, deadline=0).receive()
        process, _ = self.pipes()
        with self.assertRaisesRegex(AppServerBlocked, 'cancel'):
            self.transport(process, recheck=Mock(side_effect=AppServerBlocked('cancel'))).send({})


if __name__ == '__main__':
    unittest.main()
