"""Offline protocol replay and local pipes: no auth, UID changes or models.

The separate recorded failure fixture comes from real0.160 with localhost
scripted responses and read-only mode; it does not demonstrate a successful
command, production externalSandbox, authentication, or real inference.

Wire fixtures follow the official rust-v0.160.0 generated types, including the
notification envelope timestamp and nullable message phase (not live captures):
codex-rs/app-server-protocol/schema/typescript/ServerNotificationEnvelope.ts,
MessagePhase.ts, v2/ThreadItem.ts, v2/Turn.ts and v2/ItemStartedNotification.ts.
The complete fixture also follows app-server/src/{lib,bespoke_event_handling,
thread_state}.rs (see source references alongside the fixture below).
"""
from copy import deepcopy
import ast
import inspect
from dataclasses import replace
import json
import os
import shlex
import subprocess
from contextlib import redirect_stderr
from io import StringIO
from urllib.error import HTTPError
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import research_harness.app_server_adapter as adapter_module
from research_harness.manager_runtime import (RecheckRejected, require_recheck_reply,
    safe_recheck_failure)
from research_harness.schema import ContractError

from research_harness.app_server_adapter import (
    AppServerAdapter, AppServerBlocked, BoundedStdioTransport, CODEX_HOME,
    PROMPT, WORKSPACE, fixed_app_server_request, validate_app_server_request,
    SHELL_BODIES, safe_app_server_diagnostic,
)


def response(number, result): return {'id': number, 'result': result}
def event(method, params):
    return {'method': method, 'params': params, 'emittedAtMs': 1792224000123}


def transcript():
    thread = {'id': 'thread-1', 'ephemeral': True, 'cliVersion': '0.160.0',
              'cwd': WORKSPACE, 'turns': []}
    turn = {'id': 'turn-1', 'status': 'inProgress', 'items': [], 'itemsView': 'notLoaded',
            'error': None, 'startedAt': 1792224000, 'completedAt': None, 'durationMs': None}
    payload = json.dumps({'schema_version': 'c3-cpu-canary-worker-v1',
                          'outcome': 'success', 'reported_checksum': 333283335000})
    agent = {'type': 'agentMessage', 'id': 'final-1', 'phase': None, 'text': payload,
             'memoryCitation': None, 'delivery': None, 'questions': None}
    command = {'type': 'commandExecution', 'id': 'command-1', 'status': 'completed',
               'command': shlex.join(('/bin/bash', '-c', sorted(SHELL_BODIES)[0])),
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
            turn, status='completed', items=[agent], itemsView='summary', completedAt=1792224000,
            durationMs=20)}),
    ]


def bundled_bwrap_warning():
    # Verbatim notification from official 0.160.0 initialize/thread-start probe
    # no-model-protocol.json (2026-10-17). No model or credential content.
    return {'method': 'configWarning', 'params': {
        'summary': 'Codex could not find bubblewrap on PATH. Install bubblewrap '
        'with your OS package manager. See the sandbox prerequisites: '
        'https://developers.openai.com/codex/concepts/sandboxing#prerequisites. '
        'Codex will use the bundled bubblewrap in the meantime.',
        'details': None}, 'emittedAtMs': 1792236238533}


def rate_limits_event():
    return event('account/rateLimits/updated', {'rateLimits': {
        'limitId': 'codex', 'limitName': None, 'normalModelSlug': None,
        'primary': {'usedPercent': 13, 'windowDurationMins': 300, 'resetsAt': 1792242000},
        'secondary': None, 'credits': {'hasCredits': False, 'unlimited': False, 'balance': None},
        'individualLimit': None, 'spendControlReached': None, 'planType': 'plus',
        'rateLimitReachedType': None}})


def complete_v0160_transcript():
    """Source-derived fake sequence, not a live capture or credential evidence.

    rust-v0.160.0 app-server/src/lib.rs sends disabled remote-control state on
    initialization; bespoke_event_handling.rs handles turn start, model metadata,
    token counts and summary completion; thread_state.rs selects the last
    nonempty final/unknown-phase agent message. Payloads use generated v2 types.
    Optional model metadata is included to exercise every admitted stable kind.
    """
    base = transcript()
    wrapped = shlex.join(('/bin/bash', '-c', sorted(SHELL_BODIES)[0]))
    base[5]['params']['item']['command'] = wrapped
    base[6]['params']['item']['command'] = wrapped
    ids = {'threadId': 'thread-1', 'turnId': 'turn-1'}
    counts = {'totalTokens': 34, 'inputTokens': 20, 'cachedInputTokens': 0,
              'cacheWriteInputTokens': 0, 'outputTokens': 14, 'reasoningOutputTokens': 5}
    reasoning = {'type': 'reasoning', 'id': 'reasoning-1', 'summary': [], 'content': []}
    user = {'type': 'userMessage', 'id': 'user-1', 'clientId': None,
            'content': [{'type': 'text', 'text': PROMPT, 'text_elements': []}]}
    return [base[0],
        event('remoteControl/status/changed', {'status': 'disabled', 'serverName': 'c3-canary',
            'installationId': 'test-installation', 'environmentId': None}),
        event('account/updated', {'authMode': 'chatgpt', 'planType': 'plus'}),
        base[2], base[1],
        event('thread/status/changed', {'threadId': 'thread-1',
                                      'status': {'type': 'active', 'activeFlags': []}}),
        base[3], base[4],
        event('item/started', dict(ids, item=user, startedAtMs=1792224000000)),
        event('item/completed', dict(ids, item=deepcopy(user), completedAtMs=1792224000000)),
        event('model/verification', dict(ids, verifications=['trustedAccessForCyber'])),
        event('model/safetyBuffering/updated', dict(ids, model='configured-model-v1',
            useCases=[], reasons=[], showBufferingUi=False, fasterModel=None)),
        event('item/started', dict(ids, item=deepcopy(reasoning), startedAtMs=1792224000000)),
        event('item/reasoning/summaryPartAdded', dict(ids, itemId='reasoning-1', summaryIndex=0)),
        event('item/reasoning/summaryTextDelta', dict(ids, itemId='reasoning-1',
                                                   summaryIndex=0, delta='Run the fixed canary.')),
        event('item/reasoning/textDelta', dict(ids, itemId='reasoning-1', contentIndex=0, delta='')),
        event('item/completed', dict(ids, item=dict(reasoning, summary=['Run the fixed canary.']),
                                    completedAtMs=1792224000001)),
        base[5],
        event('item/commandExecution/outputDelta', dict(ids, itemId='command-1', delta='333283335000\n')),
        base[6],
        event('thread/tokenUsage/updated', dict(ids, tokenUsage={
            'total': counts, 'last': deepcopy(counts), 'modelContextWindow': 200000})),
        rate_limits_event(), base[7],
        event('item/agentMessage/delta', dict(ids, itemId='final-1', delta=base[8]['params']['item']['text'])),
        base[8],
        event('thread/status/changed', {'threadId': 'thread-1', 'status': {'type': 'idle'}}),
        base[9]]


class FakeTransport:
    def __init__(self, frames=None):
        self.frames = deepcopy(transcript() if frames is None else frames)
        self.sent = []
    def send(self, message): self.sent.append(deepcopy(message))
    def receive(self):
        if not self.frames: raise AppServerBlocked('fake transport closed')
        return self.frames.pop(0)


class RecordedProtocolTest(unittest.TestCase):
    def test_actual_code_mode_wrapper_uses_existing_command_events_and_stays_rejected(self):
        from pathlib import Path
        record = json.loads((Path(__file__).parent / 'fixtures' /
            'codex_0160_code_mode_command_rejected.json').read_text())
        self.assertFalse(record['real_model_call'])
        self.assertFalse(record['command_execution_succeeded'])
        self.assertEqual(record['sandbox'], 'readOnly')
        self.assertEqual([frame['method'] for frame in record['frames']],
                         ['item/started', 'item/completed'])
        for frame in record['frames']:
            self.assertEqual(frame['params']['item']['source'], 'unifiedExecStartup')
            self.assertEqual(shlex.split(frame['params']['item']['command'])[:2],
                             ['/bin/bash', '-c'])
        observed = []
        with self.assertRaisesRegex(AppServerBlocked, 'canary command did not complete') as caught:
            AppServerAdapter(enabled=True).run(
                fixed_app_server_request('configured-model-v1'),
                FakeTransport(transcript()[:5] + record['frames']),
                admit_external=lambda request: None, recheck=lambda: None,
                command_observer=observed.append)
        self.assertEqual(observed, [
            dict(command_started=True, command_completed=False, command_exit_code=None),
            dict(command_started=True, command_completed=True, command_exit_code=1)])
        self.assertEqual(safe_app_server_diagnostic(caught.exception)['reason'],
                         'APP_SERVER_COMMAND_NOT_COMPLETED')

    def test_actual_0160_loopback_failure_is_a_failure_candidate(self):
        from pathlib import Path
        record = json.loads((Path(__file__).parent / 'fixtures' /
            'codex_0160_recorded_failure.json').read_text())
        # Real 0.160 emitted these frames from scripted localhost responses.
        # No authentication/inference/tool call; readonly, not externalSandbox.
        # Only paths and generated identities were normalized. Never count this
        # accepted failure candidate as an executed or successful canary.
        transport = FakeTransport(record['frames'])
        result = AppServerAdapter(enabled=True).run(
            fixed_app_server_request('gpt-6.1-sol'), transport,
            admit_external=lambda request: None, recheck=lambda: None)
        self.assertEqual(result.candidate.outcome, 'failure')
        self.assertIsNone(result.candidate.reported_checksum)
        self.assertIsNone(result.observed_command)
        self.assertIsNone(result.command_exit_code)


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
        self.assertIn('suppress_unstable_features_warning=true', self.request.argv)
        for alias in ('codex_hooks', 'connectors', 'memory_tool'):
            self.assertNotIn(f'features.{alias}=false', self.request.argv)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', self.request.argv)
        for request in (replace(self.request, argv=('sh',)), replace(self.request, cwd='/control'),
                        replace(self.request, codex_home='/control/auth'),
                        replace(self.request, wall_seconds=121),
                        replace(self.request, external_network='restricted')):
            with self.subTest(request=request), self.assertRaises(AppServerBlocked):
                validate_app_server_request(request)
        for model in ('', 'model-latest', 'x;sh', 'model\n'):
            with self.assertRaises(AppServerBlocked): fixed_app_server_request(model)

    def test_prompt_permits_one_code_mode_tool_call_and_only_final_json(self):
        code = adapter_module.CODE_MODE_CANARY
        prefix, suffix = 'const r = await tools.exec_command(', '); text(r);'
        self.assertTrue(code.startswith(prefix)); self.assertTrue(code.endswith(suffix))
        arguments = json.loads(code[len(prefix):-len(suffix)])
        self.assertEqual(arguments, {
            'cmd': 'python3 -I -S -c "print(sum(i*i for i in range(10000)))"',
            'workdir': WORKSPACE, 'login': False})
        self.assertEqual(tuple(shlex.split(arguments['cmd'])), adapter_module.COMMAND)
        self.assertIn(arguments['cmd'], SHELL_BODIES)
        self.assertEqual(PROMPT.count(code), 1)
        self.assertIn('exactly one functions.exec call', PROMPT)
        self.assertIn('Tool calls are allowed and required.', PROMPT)
        self.assertIn('Only your final response must be JSON:', PROMPT)
        self.assertNotIn('Return only JSON:', PROMPT)
        for restriction in ('delegate to other agents', 'extra tool calls', 'or retry',
                            'security settings', 'sandboxing', 'Do not inspect files'):
            self.assertIn(restriction, PROMPT)
        self.run_fake()
        self.assertEqual(self.transport.sent[2]['params']['developerInstructions'], PROMPT)
        self.assertEqual(self.transport.sent[3]['params']['input'][0]['text'], PROMPT)

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

    def test_empty_or_partial_full_completed_turn_snapshot_is_rejected(self):
        for retained_indices in ((), (0,), (1,)):
            frames = transcript()
            items = [frames[6]['params']['item'], frames[8]['params']['item']]
            frames[-1]['params']['turn']['itemsView'] = 'full'
            frames[-1]['params']['turn']['items'] = [items[index] for index in retained_indices]
            with self.subTest(retained_indices=retained_indices), self.assertRaisesRegex(
                    AppServerBlocked, 'incomplete final item snapshot'):
                self.run_fake(frames)

    def test_source_derived_complete_v0160_sequence_and_full_view(self):
        for view in ('summary', 'full'):
            frames = complete_v0160_transcript()
            if view == 'full':
                frames[-1]['params']['turn'].update(itemsView='full', items=[
                    frame['params']['item'] for frame in frames if frame.get('method') == 'item/completed'])
            with self.subTest(view=view):
                result = self.run_fake(frames)
                self.assertEqual(result.command_checksum, 333283335000)
                self.assertFalse(result.candidate.independently_validated)
                self.assertTrue(result.requires_namespace_restart)

    def test_summary_must_match_the_streamed_final_candidate(self):
        for replacement in ([], [transcript()[6]['params']['item']],
                            [transcript()[8]['params']['item']] * 2,
                            [dict(transcript()[8]['params']['item'], id='unobserved')],
                            [dict(transcript()[8]['params']['item'], text='{}')]):
            frames = transcript(); frames[-1]['params']['turn']['items'] = replacement
            with self.subTest(items=replacement), self.assertRaises(AppServerBlocked):
                self.run_fake(frames)
        for view in (None, 'missing', 'notLoaded', 'partial', True, {}):
            frames = transcript()
            if view == 'missing': frames[-1]['params']['turn'].pop('itemsView')
            else: frames[-1]['params']['turn']['itemsView'] = view
            if view == 'notLoaded': frames[-1]['params']['turn']['items'] = []
            with self.subTest(view=view), self.assertRaises(AppServerBlocked): self.run_fake(frames)

    def test_summary_selection_follows_last_nonempty_noncommentary_message(self):
        for phase, text, accepted in (('commentary', 'Finished.', True),
                                      (None, '  ', True), (None, 'Finished.', False)):
            frames = transcript()
            extra = dict(frames[8]['params']['item'], id='after-final', phase=phase, text=text)
            frames[-1:-1] = [
                event('item/started', {'threadId': 'thread-1', 'turnId': 'turn-1',
                                      'item': dict(extra, text=''), 'startedAtMs': 1792224000021}),
                event('item/completed', {'threadId': 'thread-1', 'turnId': 'turn-1',
                                        'item': extra, 'completedAtMs': 1792224000022})]
            with self.subTest(phase=phase, text=text):
                if accepted: self.assertTrue(self.run_fake(frames).requires_namespace_restart)
                else:
                    with self.assertRaisesRegex(AppServerBlocked, 'summary'):
                        self.run_fake(frames)

    def test_connection_metadata_is_valid_at_every_protocol_stage(self):
        for position in (0, 1, 3, 5, 8, 9):
            frames = transcript()
            frames[position:position] = [rate_limits_event(),
                event('account/updated', {'authMode': None, 'planType': None}),
                event('remoteControl/status/changed', {'status': 'disabled', 'serverName': 'test',
                    'installationId': 'test-installation', 'environmentId': None})]
            with self.subTest(position=position):
                self.assertEqual(self.run_fake(frames).candidate.reported_checksum, 333283335000)
        update = rate_limits_event()
        update['params']['rateLimits'] = {key: None for key in update['params']['rateLimits']}
        frames = transcript(); frames.insert(5, update)
        self.assertTrue(self.run_fake(frames).requires_namespace_restart)
        update = rate_limits_event()
        update['params']['rateLimits']['individualLimit'] = {
            'limit': '100', 'used': '20', 'remainingPercent': 80, 'resetsAt': 1792242000}
        frames = transcript(); frames.insert(5, update)
        self.assertTrue(self.run_fake(frames).requires_namespace_restart)

    def test_malformed_connection_metadata_and_remote_enablement_block(self):
        mutations = [
            lambda p: p.update(rateLimits=None),
            lambda p: p.update(threadId='thread-1'),
            lambda p: p['rateLimits'].update(primary=[]),
            lambda p: p['rateLimits']['primary'].update(usedPercent=True),
            lambda p: p['rateLimits']['primary'].update(usedPercent=12.5),
            lambda p: p['rateLimits']['primary'].update(usedPercent=2**31),
            lambda p: p['rateLimits']['primary'].update(usedPercent=float('inf')),
            lambda p: p['rateLimits']['primary'].update(resetsAt='later'),
            lambda p: p['rateLimits']['credits'].update(hasCredits=1),
            lambda p: p['rateLimits'].update(planType='unknown-plan'),
            lambda p: p['rateLimits'].update(spendControlReached=1),
            lambda p: p['rateLimits'].update(individualLimit={'limit': '1'}),
        ]
        for mutate in mutations:
            update = rate_limits_event(); mutate(update['params'])
            frames = transcript(); frames.insert(0, update)
            with self.subTest(mutation=mutate), self.assertRaises(AppServerBlocked): self.run_fake(frames)
        for params in ({}, {'authMode': 'chatgpt', 'planType': []}):
            with self.assertRaises(AppServerBlocked):
                self.run_fake([event('account/updated', params)] + transcript())
        for status in ('connected', 'connecting', 'errored', None, True):
            update = event('remoteControl/status/changed', {'status': status,
                'serverName': 'test', 'installationId': 'test-installation', 'environmentId': None})
            with self.subTest(status=status), self.assertRaises(AppServerBlocked):
                self.run_fake([update] + transcript())

    def test_telemetry_does_not_replace_command_evidence_or_change_turn(self):
        frames = complete_v0160_transcript()
        frames = [frame for frame in frames if not (
            frame.get('method', '').startswith('item/')
            and (frame['params'].get('itemId') == 'command-1'
                 or frame['params'].get('item', {}).get('id') == 'command-1'))]
        with self.assertRaisesRegex(AppServerBlocked, 'command completion'): self.run_fake(frames)
        for method in ('thread/tokenUsage/updated', 'model/verification', 'model/safetyBuffering/updated'):
            for key in ('threadId', 'turnId'):
                frames = complete_v0160_transcript()
                next(frame for frame in frames if frame.get('method') == method)['params'][key] = 'other'
                with self.subTest(method=method, key=key), self.assertRaises(AppServerBlocked):
                    self.run_fake(frames)

    def test_malformed_thread_telemetry_and_deltas_block(self):
        mutations = [
            ('thread/status/changed', lambda p: p.update(status={'type': 'active', 'activeFlags': [None]})),
            ('thread/tokenUsage/updated', lambda p: p['tokenUsage']['last'].update(totalTokens=True)),
            ('model/verification', lambda p: p.update(verifications=['unknown'])),
            ('model/safetyBuffering/updated', lambda p: p.update(showBufferingUi=1)),
            ('item/reasoning/summaryPartAdded', lambda p: p.update(summaryIndex=-1)),
            ('item/reasoning/summaryTextDelta', lambda p: p.update(delta=[])),
            ('item/reasoning/textDelta', lambda p: p.update(contentIndex=True)),
            ('item/commandExecution/outputDelta', lambda p: p.update(delta=None)),
            ('item/agentMessage/delta', lambda p: p.update(itemId='unknown')),
        ]
        for method, mutate in mutations:
            frames = complete_v0160_transcript()
            mutate(next(frame for frame in frames if frame.get('method') == method)['params'])
            with self.subTest(method=method), self.assertRaises(AppServerBlocked): self.run_fake(frames)

    def test_exact_bundled_bwrap_notice_is_admitted_at_every_receive_phase(self):
        for position in range(len(transcript())):
            for locations in ({}, {'path': None}, {'range': None}, {'path': None, 'range': None}):
                frames = transcript()
                warning = bundled_bwrap_warning(); warning['params'].update(locations)
                frames.insert(position, warning)
                with self.subTest(position=position, locations=locations):
                    result = self.run_fake(frames)
                    self.assertEqual(result.candidate.reported_checksum, 333283335000)
        # Captured order: initialize reply -> notice -> remote state -> thread reply.
        frames = complete_v0160_transcript(); frames.insert(1, bundled_bwrap_warning())
        self.assertEqual(self.run_fake(frames).command_checksum, 333283335000)

    def test_config_warning_unknown_content_and_security_changes_are_blocked(self):
        approved = bundled_bwrap_warning()['params']['summary']
        summaries = [approved + ' ', approved.replace('bundled', 'disabled'),
            'User namespaces are disabled. Sandbox cannot start.',
            'WSL1 does not support the Linux sandbox.',
            'Falling back to default configuration after a load error.',
            'Managed policy overrides sandbox settings.', 'sk-secret-do-not-export']
        variants = [dict(summary=summary, details=None) for summary in summaries]
        variants += [dict(summary=approved, details='sk-secret-do-not-export'),
                     dict(summary=approved, details=None, path='/secret/path'),
                     dict(summary=approved, details=None,
                          range={'start': {'line': 1, 'column': 1}, 'end': {'line': 1, 'column': 2}})]
        for params in variants:
            for position in (0, 1, 3, 5, 9):
                frames = transcript(); frames.insert(position, event('configWarning', params))
                with self.subTest(position=position), self.assertRaises(AppServerBlocked) as caught:
                    self.run_fake(frames)
                detail = safe_app_server_diagnostic(caught.exception)
                self.assertEqual(detail['reason'], 'APP_SERVER_CONFIG_WARNING_REJECTED')
                self.assertEqual(detail['config_warning'], 'unrecognized')
                self.assertEqual(detail['method'], 'configWarning')
                self.assertNotIn('sk-secret', json.dumps(detail))
                self.assertNotIn('/secret/path', json.dumps(detail))

    def test_malformed_config_warning_fields_are_not_general_telemetry(self):
        approved = bundled_bwrap_warning()['params']['summary']
        variants = [{}, {'summary': approved}, {'details': None},
            {'summary': None, 'details': None}, {'summary': [], 'details': None},
            {'summary': approved, 'details': False}, {'summary': approved, 'details': {}},
            {'summary': approved, 'details': None, 'path': 1},
            {'summary': approved, 'details': None, 'range': 'wrong-type'},
            {'summary': approved, 'details': None, 'unknown': 'sk-secret-do-not-export'}]
        for params in variants:
            frames = transcript(); frames.insert(0, event('configWarning', params))
            with self.subTest(params=params), self.assertRaises(AppServerBlocked) as caught:
                self.run_fake(frames)
            detail = safe_app_server_diagnostic(caught.exception)
            self.assertEqual(detail['reason'], 'APP_SERVER_CONFIG_WARNING_REJECTED')
            self.assertEqual(detail['config_warning'], 'malformed')
            self.assertNotIn('sk-secret', json.dumps(detail))

    def test_warning_envelope_requests_and_model_warnings_remain_blocked(self):
        for mutate in [lambda w: w.update(extra='secret'), lambda w: w.update(emittedAtMs=True),
                       lambda w: w.update(jsonrpc='1.0'), lambda w: w.update(id=10),
                       lambda w: w.update(params=None), lambda w: w.update(method='warning'),
                       lambda w: w.update(method='guardianWarning')]:
            warning = bundled_bwrap_warning(); mutate(warning)
            frames = transcript(); frames.insert(0, warning)
            with self.subTest(mutate=mutate), self.assertRaises(AppServerBlocked): self.run_fake(frames)
        frames = transcript(); frames.insert(1, bundled_bwrap_warning())
        frames = [f for f in frames if f.get('params', {}).get('item', {}).get('type') != 'commandExecution']
        with self.assertRaisesRegex(AppServerBlocked, 'command completion'):
            self.run_fake(frames)

    def test_config_warning_diagnostic_does_not_leak_to_later_frames(self):
        for after_warning in [response(1, {}), event('unknown-secret-method', {})]:
            frames = [bundled_bwrap_warning(), after_warning]
            with self.assertRaises(AppServerBlocked) as caught: self.run_fake(frames)
            detail = safe_app_server_diagnostic(caught.exception)
            self.assertEqual(detail['config_warning'], 'none')
            self.assertNotIn('unknown-secret-method', json.dumps(detail))
        with self.assertRaises(AppServerBlocked) as caught: self.run_fake([bundled_bwrap_warning()])
        self.assertEqual(safe_app_server_diagnostic(caught.exception)['config_warning'], 'none')

    def test_semantic_changes_warnings_and_experimental_notifications_remain_blocked(self):
        for method in ('model/rerouted', 'warning', 'configWarning', 'deprecationNotice',
                       'modelProvider/authRecoveryStarted', 'thread/settings/updated',
                       'turn/moderationMetadata', 'hook/started', 'app/list/updated'):
            frames = transcript()
            frames.insert(5, event(method, {'threadId': 'thread-1', 'turnId': 'turn-1'}))
            with self.subTest(method=method), self.assertRaises(AppServerBlocked): self.run_fake(frames)

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

    def test_only_nonlogin_bash_wrapper_of_exact_literal_body_is_accepted(self):
        for body in SHELL_BODIES:
            command = shlex.join(('/bin/bash', '-c', body))
            frames = transcript()
            frames[5]['params']['item']['command'] = command
            frames[6]['params']['item']['command'] = command
            with self.subTest(body=body):
                result = self.run_fake(frames)
                self.assertEqual(result.observed_command, command)

    def test_login_other_shell_and_unwrapped_commands_rejected_at_both_events(self):
        for body in SHELL_BODIES:
            commands = [body, shlex.join(adapter_module.COMMAND)]
            commands += [shlex.join((shell, mode, body)) for shell, mode in (
                ('/bin/bash', '-lc'), ('/bin/sh', '-c'), ('/bin/sh', '-lc'))]
            for command in commands:
                for position in (5, 6):
                    frames = transcript()
                    frames[position]['params']['item']['command'] = command
                    with self.subTest(command=command, position=position):
                        with self.assertRaises(AppServerBlocked) as caught:
                            self.run_fake(frames)
                        self.assertEqual(safe_app_server_diagnostic(caught.exception)['reason'],
                                         'APP_SERVER_COMMAND_MISMATCH')

    def test_shell_metacharacters_substitution_newlines_and_extra_args_rejected(self):
        body = sorted(SHELL_BODIES)[0]
        bodies = (body + '; id', body + ' && true', body + ' > /tmp/out',
                  body + '\ntrue', body.replace(' -I', '\n-I'),
                  body + ' $(id)', body + ' `id`', 'exec ' + body)
        commands = [shlex.join(('/bin/bash', '-c', value)) for value in bodies]
        commands += [shlex.join(('/usr/bin/bash', '-c', body)),
                     shlex.join(('/bin/bash', '-c', body, 'extra'))]
        for command in commands:
            frames = transcript(); frames[5]['params']['item']['command'] = command
            with self.subTest(command=command), self.assertRaises(AppServerBlocked):
                self.run_fake(frames)

    def test_cancel_errors_and_unexpected_tools_block(self):
        with self.assertRaisesRegex(AppServerBlocked, 'recheck failed'):
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


class AppServerDiagnosticTest(unittest.TestCase):
    def failure(self, frames, *, transport=None):
        with self.assertRaises(AppServerBlocked) as caught:
            AppServerAdapter(enabled=True).run(
                fixed_app_server_request('configured-model-v1'),
                transport if transport is not None else FakeTransport(frames),
                admit_external=lambda request: None, recheck=lambda: None)
        detail = safe_app_server_diagnostic(caught.exception)
        self.assertIsNotNone(detail)
        self.assertLessEqual(len(json.dumps(detail)), 512)
        return detail

    def test_every_literal_raise_has_a_unique_stable_reason(self):
        tree = ast.parse(inspect.getsource(adapter_module))
        sites = [node.exc.args[0].value for node in ast.walk(tree)
                 if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                 and isinstance(node.exc.func, ast.Name) and node.exc.func.id == 'AppServerBlocked'
                 and node.exc.args and isinstance(node.exc.args[0], ast.Constant)]
        self.assertEqual(len(sites), 88)
        self.assertEqual(set(sites), set(adapter_module._BLOCK_REASON_CODES))
        codes = list(adapter_module._BLOCK_REASON_CODES.values())
        self.assertEqual(len(codes), len(set(codes)))
        for message in sites:
            detail = safe_app_server_diagnostic(AppServerBlocked(message))
            self.assertEqual(detail['reason'], adapter_module._BLOCK_REASON_CODES[message])
            self.assertNotEqual(detail['reason'], 'APP_SERVER_CONTRACT_OTHER')

    def test_unknown_exception_values_and_forged_attributes_do_not_escape(self):
        secret = 'sk-secret-do-not-export'
        exc = AppServerBlocked(secret)
        baseline = safe_app_server_diagnostic(exc)
        self.assertEqual(baseline['reason'], 'APP_SERVER_CONTRACT_OTHER')
        exc.args = (secret, {'event': secret})
        exc.diagnostic = {'reason': secret}
        exc._diagnostic = secret
        exc.phase = secret
        first = safe_app_server_diagnostic(exc)
        first['reason'] = secret
        first['payload'] = secret
        self.assertEqual(safe_app_server_diagnostic(exc), baseline)
        self.assertNotIn(secret, json.dumps(safe_app_server_diagnostic(exc)))
        self.assertIsNone(safe_app_server_diagnostic(AppServerBlocked.__new__(AppServerBlocked)))
        self.assertIsNone(safe_app_server_diagnostic(ValueError(secret)))
        class Hostile(AppServerBlocked):
            def __getattribute__(self, name): raise AssertionError('must not inspect subclass')
        self.assertIsNone(safe_app_server_diagnostic(Hostile(secret)))
        class HostileValue:
            def __str__(self): raise AssertionError('must not stringify')
            def __repr__(self): raise AssertionError('must not stringify')
            def __hash__(self): raise AssertionError('must not hash')
        self.assertEqual(safe_app_server_diagnostic(AppServerBlocked(HostileValue()))['reason'],
                         'APP_SERVER_CONTRACT_OTHER')

    def test_defensive_export_rejects_corrupted_internal_records(self):
        exc = AppServerBlocked('invalid app-server identifier')
        record = adapter_module._DIAGNOSTIC_RECORDS[exc]
        for corrupt in ({'reason': 'secret'}, ('secret',) + record[1:],
                        record[:4] + (1, False, False, False) + record[8:],
                        record[:4] + (False, True, False, False) + record[8:],
                        record[:3] + ('secret',) + record[4:],
                        record[:8] + ('secret',) + record[9:], record[:8] + ([],) + record[9:],
                        record[:9] + ([], 'manager'),
                        record[:9] + ('expired', 'secret'),
                        record[:9] + ('none', 'manager')):
            adapter_module._DIAGNOSTIC_RECORDS[exc] = corrupt
            self.assertIsNone(safe_app_server_diagnostic(exc))

    def test_initialization_thread_and_turn_failures_have_distinct_milestones(self):
        frames = transcript(); frames[0]['result']['codexHome'] = 'secret-home'
        detail = self.failure(frames)
        self.assertEqual((detail['reason'], detail['phase'], detail['frame_kind'], detail['method']),
            ('APP_SERVER_INITIALIZE_CONFIGURATION', 'initialize_validate', 'response', 'initialize'))
        self.assertEqual([detail[k] for k in adapter_module._DIAGNOSTIC_FIELDS[4:8]], [False]*4)
        frames = transcript(); frames[2]['result']['instructionSources'] = ['secret-path']
        detail = self.failure(frames)
        self.assertEqual((detail['reason'], detail['phase'], detail['method']),
            ('APP_SERVER_THREAD_CONFIGURATION', 'thread_start_validate', 'thread/start'))
        self.assertEqual([detail[k] for k in adapter_module._DIAGNOSTIC_FIELDS[4:8]],
                         [True, False, False, False])
        frames = transcript(); frames[4]['result']['turn']['status'] = 'failed'
        detail = self.failure(frames)
        self.assertEqual((detail['reason'], detail['phase'], detail['method']),
            ('APP_SERVER_TURN_START_STATUS', 'turn_start_validate', 'turn/start'))
        self.assertEqual([detail[k] for k in adapter_module._DIAGNOSTIC_FIELDS[4:8]],
                         [True, True, False, False])

    def test_buffered_frame_is_classified_when_validated(self):
        frames = transcript()
        frames.insert(0, event('deprecationNotice', {'message': 'sk-secret-do-not-export'}))
        detail = self.failure(frames)
        self.assertEqual((detail['reason'], detail['phase'], detail['frame_kind'], detail['method']),
            ('APP_SERVER_PRE_INITIALIZE_ACTIVITY', 'initialize_validate', 'notification', 'deprecationNotice'))
        self.assertNotIn('sk-secret', json.dumps(detail))
        frames = transcript()
        frames.insert(1, event('deprecationNotice', {'message': 'sk-secret-do-not-export'}))
        detail = self.failure(frames)
        self.assertEqual((detail['reason'], detail['phase'], detail['frame_kind'], detail['method']),
            ('APP_SERVER_PRE_TURN_ACTIVITY', 'thread_start_validate', 'notification', 'deprecationNotice'))
        self.assertNotIn('sk-secret', json.dumps(detail))

    def test_stream_and_final_rejection_context(self):
        frames = transcript(); frames[5]['params']['item']['command'] = 'secret-command'
        detail = self.failure(frames)
        self.assertEqual((detail['reason'], detail['phase'], detail['method']),
                         ('APP_SERVER_COMMAND_MISMATCH', 'stream', 'item/started'))
        self.assertEqual([detail[k] for k in adapter_module._DIAGNOSTIC_FIELDS[4:8]],
                         [True, True, True, False])
        frames = transcript(); frames[-1]['params']['turn']['items'] = []
        detail = self.failure(frames)
        self.assertEqual((detail['reason'], detail['phase'], detail['method']),
            ('APP_SERVER_SUMMARY_MISMATCH', 'final_validate', 'turn/completed'))
        self.assertFalse(detail['final_validated'])

    def test_known_server_requests_errors_and_unknown_methods_are_content_free(self):
        secret = 'sk-secret-do-not-export'
        for incoming, kind, method, reason in [
            ({'id': secret, 'method': 'item/commandExecution/requestApproval', 'params': {'secret': secret}},
             'request', 'item/commandExecution/requestApproval', 'APP_SERVER_SERVER_REQUEST'),
            ({'id': 3, 'error': {'code': -32000, 'message': secret, 'data': secret}},
             'response', 'turn/start', 'APP_SERVER_SERVER_ERROR'),
            (event('error', {'message': secret}), 'notification', 'error', 'APP_SERVER_SERVER_ERROR'),
            (event(secret, {'threadId': 'thread-1', 'turnId': 'turn-1', 'secret': secret}),
             'notification', 'unknown', 'APP_SERVER_NOTIFICATION_UNSUPPORTED')]:
            frames = transcript(); frames.insert(5, incoming)
            detail = self.failure(frames)
            self.assertEqual((detail['frame_kind'], detail['method'], detail['reason']),
                             (kind, method, reason))
            self.assertNotIn(secret, json.dumps(detail))

    def test_send_wait_and_preconstructed_transport_failure_are_distinct(self):
        for failed_method, phase in [('initialize', 'initialize_send'),
                                     ('thread/start', 'thread_start_send'),
                                     ('turn/start', 'turn_start_send')]:
            transport = FakeTransport()
            original = transport.send
            def send(message):
                if message.get('method') == failed_method:
                    raise AppServerBlocked('app-server input pipe failed')
                original(message)
            transport.send = send
            detail = self.failure(None, transport=transport)
            self.assertEqual(detail['phase'], phase)
            self.assertEqual((detail['frame_kind'], detail['method']), ('none', 'none'))
        for prefix, phase in [(0, 'initialize_wait'), (1, 'thread_start_wait'), (3, 'turn_start_wait')]:
            transport = FakeTransport(transcript()[:prefix])
            error = AppServerBlocked('app-server closed before protocol completion')
            original = transport.receive
            def receive():
                if not transport.frames: raise error
                return original()
            transport.receive = receive
            detail = self.failure(None, transport=transport)
            self.assertEqual(detail['phase'], phase)
            self.assertEqual(detail['reason'], 'APP_SERVER_EARLY_EOF')
            self.assertEqual((detail['frame_kind'], detail['method']), ('none', 'none'))

    def test_success_milestones_and_context_reset(self):
        with patch.object(adapter_module, 'AppServerCandidate',
                          side_effect=AppServerBlocked('post-validation test failure')):
            detail = self.failure(transcript())
        self.assertTrue(all(detail[k] for k in adapter_module._DIAGNOSTIC_FIELDS[4:8]))
        self.assertEqual(detail['phase'], 'final_validate')
        outside = safe_app_server_diagnostic(AppServerBlocked('invalid app-server identifier'))
        self.assertEqual(outside['phase'], 'unscoped')
        self.assertFalse(any(outside[k] for k in adapter_module._DIAGNOSTIC_FIELDS[4:8]))



class CommandObservationTest(unittest.TestCase):
    @staticmethod
    def failure_frames(*, command=True, code=1, status='completed'):
        frames = transcript()
        text = json.dumps({'schema_version': 'c3-cpu-canary-worker-v1',
                           'outcome': 'failure', 'reported_checksum': None})
        frames[8]['params']['item']['text'] = text
        frames[6]['params']['item'].update(exitCode=code, status=status,
                                         aggregatedOutput='PRIVATE output must not be logged')
        if not command: frames = frames[:5] + frames[7:]
        return frames

    def run_observed(self, frames, observe):
        return AppServerAdapter(enabled=True).run(
            fixed_app_server_request('configured-model-v1'), FakeTransport(frames),
            admit_external=lambda request: None, recheck=lambda: None,
            command_observer=observe)

    def test_no_attempt_nonzero_and_success_are_distinct_without_result_schema_change(self):
        for frames, expected, outcome in [
            (self.failure_frames(command=False), (False, False, None), 'failure'),
            (self.failure_frames(code=7), (True, True, 7), 'failure'),
            (transcript(), (True, True, 0), 'success')]:
            observed=[]
            with self.subTest(expected=expected):
                result=self.run_observed(frames, observed.append)
                self.assertEqual(tuple(observed[-1][key] for key in adapter_module._COMMAND_FIELDS),expected)
                self.assertEqual(result.candidate.outcome,outcome)
                self.assertFalse(result.candidate.independently_validated)
                self.assertNotIn('PRIVATE',json.dumps(observed))
                self.assertTrue(all(set(record)==set(adapter_module._COMMAND_FIELDS) for record in observed))
                self.assertEqual(len(observed), 1 if expected[0] is False else 3)

    def test_interrupted_after_started_preserves_observation_before_exception(self):
        frames=transcript()[:6]
        frames.append(event('turn/completed', {'threadId':'thread-1',
            'turn':{'id':'turn-1','error':None,'status':'interrupted','items':[], 'itemsView':'summary'}}))
        observed=[]
        with self.assertRaises(AppServerBlocked) as caught:self.run_observed(frames,observed.append)
        expected=dict(command_started=True,command_completed=False,command_exit_code=None)
        self.assertEqual(observed,[expected])
        detail=safe_app_server_diagnostic(caught.exception)
        self.assertEqual({key:detail[key] for key in expected},expected)

    def test_failed_terminal_event_is_recorded_without_relaxing_canary_policy(self):
        for status,code in (('failed',7),('declined',None)):
            observed=[]
            with self.subTest(status=status), self.assertRaisesRegex(AppServerBlocked,'did not complete') as caught:
                self.run_observed(self.failure_frames(status=status,code=code),observed.append)
            expected=dict(command_started=True,command_completed=True,command_exit_code=code)
            self.assertEqual(observed[-1],expected)
            detail=safe_app_server_diagnostic(caught.exception)
            self.assertEqual({key:detail[key] for key in expected},expected)

    def test_malformed_completion_does_not_set_completed_diagnostic(self):
        for status,code in (('inProgress',0),('completed',True),('failed','PRIVATE')):
            observed=[]
            with self.subTest(status=status,code=code),self.assertRaises(AppServerBlocked) as caught:
                self.run_observed(self.failure_frames(status=status,code=code),observed.append)
            self.assertEqual(observed,[dict(command_started=True,command_completed=False,command_exit_code=None)])
            self.assertFalse(safe_app_server_diagnostic(caught.exception)['command_completed'])

    def test_unvalidated_command_or_completion_never_invents_evidence(self):
        for stage in ('start','completion'):
            frames=transcript()
            frames[5 if stage=='start' else 6]['params']['item']['command']='PRIVATE arbitrary command'
            observed=[]
            with self.subTest(stage=stage),self.assertRaises(AppServerBlocked) as caught:
                self.run_observed(frames,observed.append)
            detail=safe_app_server_diagnostic(caught.exception)
            self.assertEqual(detail['command_started'],stage=='completion')
            self.assertFalse(detail['command_completed']);self.assertIsNone(detail['command_exit_code'])
            self.assertNotIn('PRIVATE',json.dumps(detail));self.assertNotIn('PRIVATE',json.dumps(observed))

    def test_observer_mutation_and_failure_do_not_change_validation(self):
        def observer(record):
            record.update(command_started=False,command_exit_code='PRIVATE')
            raise RuntimeError('PRIVATE')
        self.assertEqual(self.run_observed(transcript(),observer).candidate.outcome,'success')
        with self.assertRaisesRegex(AppServerBlocked,'trusted command observer'):
            self.run_observed(transcript(), True)

    def test_closed_command_export_rejects_forgery_and_impossible_states(self):
        valid=dict(command_started=True,command_completed=True,command_exit_code=7)
        first=adapter_module.safe_command_observation(valid);first['command_exit_code']='PRIVATE'
        self.assertEqual(adapter_module.safe_command_observation(valid),valid)
        for change in ({'command_started':1},{'command_completed':1},{'command_exit_code':True},
                       {'command_exit_code':'PRIVATE'},{'command_exit_code':2**31},
                       {'command_started':False},{'command_completed':False},{'extra':'PRIVATE'}):
            with self.subTest(change=change):
                self.assertIsNone(adapter_module.safe_command_observation(dict(valid,**change)))
        exc=AppServerBlocked('invalid protocol object');record=adapter_module._DIAGNOSTIC_RECORDS[exc]
        for suffix in ((True,True,0),(False,True,None),(True,False,1),(False,False,'PRIVATE')):
            adapter_module._DIAGNOSTIC_RECORDS[exc]=record[:11]+suffix
            self.assertIsNone(safe_app_server_diagnostic(exc))


class AppServerRecheckDiagnosticTest(unittest.TestCase):
    def fail_recheck(self, failure, *, received=0):
        request = fixed_app_server_request('configured-model-v1')
        adapter = AppServerAdapter(enabled=True)
        adapter.admit(request, admit_external=lambda request: None, recheck=lambda: None)
        transport = FakeTransport()
        total = len(transport.frames)
        def recheck():
            if total - len(transport.frames) >= received:
                raise failure
        with self.assertRaises(AppServerBlocked) as caught:
            adapter.run(request, transport, recheck=recheck)
        return caught.exception

    def test_raw_trusted_check_failures_are_now_classified_without_exception_text(self):
        secret = 'SECRET-never-export'
        cases = [
            (ContractError(secret), 'contract', 'approval_callback'),
            (RecheckRejected('expired'), 'expired', 'manager'),
            (RecheckRejected('changed'), 'changed', 'manager'),
            (HTTPError('https://secret.invalid', 429, secret, {}, None),
             'source_rate_limited', 'approval_callback'),
            (TimeoutError(secret), 'source_timeout', 'approval_callback'),
            (subprocess.TimeoutExpired(secret, 1, output=secret, stderr=secret),
             'source_timeout', 'approval_callback'),
            (subprocess.CalledProcessError(1, secret, output=secret, stderr=secret),
             'source_command_failed', 'approval_callback'),
            (OSError(secret), 'source_unavailable', 'approval_callback'),
            (RuntimeError(secret), 'unknown', 'approval_callback')]
        for failure, reason, origin in cases:
            with self.subTest(reason=reason):
                error = self.fail_recheck(failure)
                detail = safe_app_server_diagnostic(error)
                self.assertEqual(detail['reason'], 'APP_SERVER_RECHECK_FAILED')
                self.assertEqual((detail['recheck_reason'], detail['recheck_origin']), (reason, origin))
                self.assertNotIn(secret, json.dumps(detail))
                self.assertEqual(str(error), 'trusted approval recheck failed')

    def test_initial_streaming_and_final_rechecks_keep_precise_progress(self):
        for received, phase, flags in [
            (0, 'admission', [False, False, False, False]),
            (1, 'thread_start_wait', [True, False, False, False]),
            (5, 'stream', [True, True, True, False]),
            (10, 'final_validate', [True, True, True, False])]:
            detail = safe_app_server_diagnostic(self.fail_recheck(
                RecheckRejected('source_rate_limited'), received=received))
            self.assertEqual(detail['phase'], phase)
            self.assertEqual([detail[k] for k in adapter_module._DIAGNOSTIC_FIELDS[4:8]], flags)
            self.assertEqual(detail['recheck_reason'], 'source_rate_limited')

    def test_closed_manager_reply_reproduces_previous_missing_detail_with_context(self):
        from research_harness.uid_supervisor import report_failure
        for reply, reason, origin in [
            ({'approved': False, 'reason': 'changed', 'origin': 'manager'}, 'changed', 'manager'),
            ({'approved': False, 'reason': 'source_timeout', 'origin': 'manager'},
             'source_timeout', 'manager'),
            ({'approved': False}, 'contract', 'launcher_ipc'),
            ({'approved': False, 'reason': 'SECRET', 'origin': 'manager'}, 'contract', 'launcher_ipc')]:
            try: require_recheck_reply(reply)
            except ContractError as denied:
                error = self.fail_recheck(denied, received=5)
            output = StringIO()
            with redirect_stderr(output): report_failure('app_server', 'turn', error)
            record = json.loads(output.getvalue())
            self.assertEqual(record['error_class'], 'contract')
            self.assertEqual(record['app_server']['reason'], 'APP_SERVER_RECHECK_FAILED')
            self.assertEqual(record['app_server']['recheck_reason'], reason)
            self.assertEqual(record['app_server']['recheck_origin'], origin)
            self.assertNotIn('observed_child_returncode', record)
            self.assertNotIn('SECRET', output.getvalue())

    def test_non_recheck_contract_keeps_context_without_false_approval_label(self):
        for operation, phase in [('send', 'initialize_send'), ('receive', 'initialize_wait')]:
            transport = FakeTransport()
            setattr(transport, operation, Mock(side_effect=ContractError('SECRET')))
            with self.assertRaises(AppServerBlocked) as caught:
                AppServerAdapter(enabled=True).run(fixed_app_server_request('configured-model-v1'),
                    transport, admit_external=lambda request: None, recheck=lambda: None)
            detail = safe_app_server_diagnostic(caught.exception)
            self.assertEqual(detail['reason'], 'APP_SERVER_BOUNDARY_CONTRACT')
            self.assertEqual(detail['phase'], phase)
            self.assertEqual((detail['recheck_reason'], detail['recheck_origin']), ('none', 'none'))
            self.assertNotIn('SECRET', json.dumps(detail))

    def test_unregistered_or_subclassed_boundary_errors_get_only_fixed_context(self):
        class UntrustedError(AppServerBlocked):
            pass
        for error in (UntrustedError('SECRET'), AppServerBlocked.__new__(AppServerBlocked)):
            transport = FakeTransport(); transport.receive = Mock(side_effect=error)
            with self.assertRaises(AppServerBlocked) as caught:
                AppServerAdapter(enabled=True).run(fixed_app_server_request('configured-model-v1'),
                    transport, admit_external=lambda request: None, recheck=lambda: None)
            detail = safe_app_server_diagnostic(caught.exception)
            self.assertEqual(detail['reason'], 'APP_SERVER_BOUNDARY_CONTRACT')
            self.assertEqual(detail['phase'], 'initialize_wait')
            self.assertNotIn('SECRET', json.dumps(detail))

    def test_process_control_exceptions_are_not_swallowed_or_reclassified(self):
        request = fixed_app_server_request('configured-model-v1')
        adapter = AppServerAdapter(enabled=True)
        adapter.admit(request, admit_external=lambda request: None, recheck=lambda: None)
        with self.assertRaises(KeyboardInterrupt):
            adapter.run(request, FakeTransport(), recheck=Mock(side_effect=KeyboardInterrupt()))
        self.assertEqual(safe_app_server_diagnostic(AppServerBlocked('outside'))['phase'], 'unscoped')


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

    def test_transport_recheck_source_failure_is_classified_before_reading(self):
        process, _ = self.pipes(b'{}\n')
        with self.assertRaises(AppServerBlocked) as caught:
            self.transport(process, recheck=Mock(side_effect=RecheckRejected(
                'source_timeout', origin='manager'))).receive()
        detail = safe_app_server_diagnostic(caught.exception)
        self.assertEqual(detail['reason'], 'APP_SERVER_RECHECK_FAILED')
        self.assertEqual((detail['recheck_reason'], detail['recheck_origin']), ('source_timeout', 'manager'))

    def test_read_and_write_honor_deadline_and_cancellation(self):
        process, _ = self.pipes(b'{}\n')
        with self.assertRaisesRegex(AppServerBlocked, 'deadline'):
            self.transport(process, deadline=0).receive()
        process, _ = self.pipes()
        with self.assertRaisesRegex(AppServerBlocked, 'recheck failed'):
            self.transport(process, recheck=Mock(side_effect=AppServerBlocked('cancel'))).send({})


if __name__ == '__main__':
    unittest.main()
