"""Fake subprocesses only: these tests never call Codex, auth, or a sandbox."""
import json
import subprocess
import unittest
from unittest.mock import Mock

from research_harness.codex_adapter import (
    CANARY_PROMPT, CONFIG, MAX_CAPTURE_BYTES, REQUIRED_EXEC_OPTIONS,
    CanaryCandidate, CliInspection, CodexAdapter, CodexBlocked,
    SandboxInspection, ValidatedSupervisor, WORKER_SCHEMA, parse_candidate,
)
from research_harness.schema import ContractError


VERSION = '0.160.0'
BINARY = '/opt/codex/bin/codex'
VALID = CliInspection(VERSION, VERSION, True, ())


def completed(stdout=b'', *, code=0, args=(), stderr=b''):
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def candidate_stream(checksum=333283335000, outcome='success'):
    payload = {'schema_version': 'c3-cpu-canary-worker-v1',
               'outcome': outcome, 'reported_checksum': checksum}
    events = [
        {'type': 'thread.started', 'thread_id': 'fake-thread'},
        {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(payload)}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}},
    ]
    return b'\n'.join(json.dumps(event).encode() for event in events) + b'\n'


class TestOnlySupervisor(ValidatedSupervisor):
    """Simulation, never production evidence or subprocess execution."""
    def __init__(self):
        self.requests = []

    def admit_and_execute(self, request):
        self.requests.append(request)
        return completed(candidate_stream(), args=request.argv)


class CodexAdapterTest(unittest.TestCase):
    def setUp(self):
        self.runner = Mock(side_effect=AssertionError('unexpected subprocess'))
        self.adapter = CodexAdapter(BINARY, runner=self.runner)

    def request(self, adapter=None):
        return (adapter or self.adapter).build_canary_request(
            inspection=VALID, workspace='/work/canary',
            schema_path='/opt/c3/canary.schema.json', model='configured-model-v1')

    def test_check_only_is_default_and_does_not_execute(self):
        report = self.adapter.preflight().to_dict()
        self.assertFalse(report['execution_allowed'])
        self.assertEqual(report['mode'], 'check-only')
        self.assertIn('ADAPTER_DISABLED', report['blockers'])
        self.assertIn('NORMAL_SANDBOX_NOT_PROBED', report['blockers'])
        self.assertIn('SPENDING_CAP_OR_RISK_APPROVAL_MISSING', report['blockers'])
        self.runner.assert_not_called()

    def test_exact_version_and_real_cli_options_are_inspected(self):
        self.runner.side_effect = [
            completed(b'codex-cli 0.160.0\n'),
            completed((' '.join(REQUIRED_EXEC_OPTIONS)).encode()),
            completed(b'Usage: codex sandbox [OPTIONS] [COMMAND]...\n--config <key=value>'),
        ]
        self.assertEqual(self.adapter.inspect_cli(), VALID)
        self.assertEqual([call.args[0] for call in self.runner.call_args_list], [
            [BINARY, '--version'], [BINARY, 'exec', '--help'], [BINARY, 'sandbox', '--help']])
        for call in self.runner.call_args_list:
            self.assertFalse(call.kwargs['shell'])
            self.assertEqual(call.kwargs['timeout'], 5)
            self.assertNotIn('OPENAI_API_KEY', call.kwargs['env'])
            self.assertNotIn('CODEX_API_KEY', call.kwargs['env'])

    def test_version_mismatch_fails_before_help_or_model(self):
        self.runner.side_effect = [completed(b'codex-cli 0.161.0\n')]
        self.assertEqual(self.adapter.inspect_cli().blockers, ('CLI_VERSION_MISMATCH',))
        self.assertEqual(self.runner.call_count, 1)

    def test_missing_options_fail_closed(self):
        self.runner.side_effect = [completed(b'codex-cli 0.160.0\n'), completed(b'--json')]
        self.assertEqual(self.adapter.inspect_cli().blockers, ('CLI_OPTIONS_UNSUPPORTED',))

    def test_legacy_nested_sandbox_is_not_guessed(self):
        self.runner.side_effect = [
            completed(b'codex-cli 0.160.0\n'),
            completed((' '.join(REQUIRED_EXEC_OPTIONS)).encode()),
            completed(b'Usage: codex sandbox [COMMAND]\nCommands:\n linux\n--config'),
        ]
        self.assertEqual(self.adapter.inspect_cli().blockers, ('SANDBOX_CLI_UNSUPPORTED',))

    def test_inspection_errors_timeout_and_oversize_are_blockers(self):
        for result in [OSError('missing'), subprocess.TimeoutExpired('codex', 5),
                       completed(b'x' * (MAX_CAPTURE_BYTES + 1)), completed(code=1),
                       completed(b'\xff')]:
            with self.subTest(result=type(result).__name__):
                self.runner.side_effect = [result]
                self.assertFalse(self.adapter.inspect_cli().supported)

    def test_command_is_real_fixed_argv_and_prompt_without_shell(self):
        request = self.request()
        self.assertEqual(request.argv[:3], (BINARY, 'exec', '--json'))
        self.assertEqual(request.argv[-1], '-')
        self.assertEqual(request.stdin, CANARY_PROMPT.encode())
        self.assertEqual(json.loads(request.schema_bytes), WORKER_SCHEMA)
        self.assertEqual(request.wall_seconds, 120)
        for config in CONFIG:
            self.assertIn(config, request.argv)
        for forbidden in ['--full-auto', '--dangerously-bypass-approvals-and-sandbox',
                          'danger-full-access', '--ignore-rules', '--skip-git-repo-check',
                          '--approve-for-me', '--search']:
            self.assertNotIn(forbidden, request.argv)
        self.runner.assert_not_called()

    def test_invalid_paths_models_or_cli_cannot_build(self):
        for workspace in ['relative', '/', '/work/../control', '/work//canary', '/work/\ncanary']:
            with self.subTest(workspace=workspace), self.assertRaises(ContractError):
                self.adapter.build_canary_request(inspection=VALID, workspace=workspace,
                    schema_path='/opt/c3/schema.json', model='configured-model-v1')
        for model in ['', 'model-latest', '-m', 'model;curl', 'model\n']:
            with self.subTest(model=model), self.assertRaises(ContractError):
                self.adapter.build_canary_request(inspection=VALID, workspace='/work/canary',
                    schema_path='/opt/c3/schema.json', model=model)
        with self.assertRaises(CodexBlocked):
            self.adapter.build_canary_request(inspection=CliInspection('0.159.2', VERSION, True, ()),
                workspace='/work/canary', schema_path='/opt/c3/schema.json', model='model-v1')

    def test_explicit_normal_sandbox_probe_never_launches_a_model(self):
        self.runner.side_effect = [completed(b'c3-normal-sandbox-started-v1\n')]
        probe = self.adapter.probe_sandbox(inspection=VALID, workspace='/work/canary',
                                           codex_home='/work/codex-home')
        self.assertEqual(probe.status, 'STARTED_ONLY')
        self.assertFalse(probe.boundary_validated)
        call = self.runner.call_args
        self.assertEqual(call.args[0][:2], [BINARY, 'sandbox'])
        self.assertNotIn('linux', call.args[0])
        self.assertNotIn('exec', call.args[0])
        self.assertNotIn('danger-full-access', call.args[0])
        self.assertEqual(call.kwargs['timeout'], 10)
        report = self.adapter.preflight(inspection=VALID, sandbox=probe)
        self.assertFalse(report.execution_allowed)
        self.assertIn('WORKER_BOUNDARY_NOT_VALIDATED', report.blockers)
        self.assertIn('DESCENDANT_CLEANUP_NOT_ENFORCED', report.blockers)

    def test_failed_probe_has_no_retry_or_fallback(self):
        self.runner.side_effect = [completed(code=1, stderr=b'bwrap: namespace failed')]
        probe = self.adapter.probe_sandbox(inspection=VALID, workspace='/work/canary',
                                           codex_home='/work/codex-home')
        self.assertEqual(probe.status, 'FAILED')
        self.assertEqual(self.runner.call_count, 1)
        self.assertIn('NORMAL_SANDBOX_PROBE_FAILED',
                      self.adapter.preflight(inspection=VALID, sandbox=probe).blockers)

    def test_boundary_probe_is_fixed_and_does_not_execute(self):
        probe = self.adapter.build_boundary_probe(inspection=VALID)
        self.assertEqual(probe.argv[:2], (BINARY, 'sandbox'))
        self.assertEqual(probe.cwd, '/work/canary')
        self.assertIn('/work/sandbox-outside/c3-dummy', probe.argv[-1])
        self.assertIn('/control/c3-dummy', probe.argv[-1])
        self.assertIn('network_socket_denied', probe.argv[-1])
        self.assertNotIn('ENOENT', probe.argv[-1])
        self.assertNotIn('danger-full-access', probe.argv)
        self.runner.assert_not_called()

    def test_operator_enabled_or_boolean_does_not_establish_authority(self):
        adapter = CodexAdapter(BINARY, enabled=True, runner=self.runner)
        for supervisor in [None, True, {'verified': True}, Mock()]:
            with self.subTest(supervisor=type(supervisor).__name__), self.assertRaises(CodexBlocked):
                adapter.run_canary(inspection=VALID, workspace='/work/canary',
                    schema_path='/opt/c3/schema.json', model='model-v1', supervisor=supervisor)
        report = adapter.preflight(inspection=VALID, sandbox=SandboxInspection('STARTED_ONLY'))
        self.assertFalse(report.execution_allowed)
        self.assertIn('HARD_RESOURCES_NOT_ENFORCED', report.blockers)
        self.runner.assert_not_called()

    def test_default_disabled_rejects_even_test_supervisor(self):
        supervisor = TestOnlySupervisor()
        with self.assertRaisesRegex(CodexBlocked, 'ADAPTER_DISABLED'):
            self.adapter.run_canary(inspection=VALID, workspace='/work/canary',
                schema_path='/opt/c3/schema.json', model='model-v1', supervisor=supervisor)
        self.assertEqual(supervisor.requests, [])

    def test_test_supervisor_exercises_handoff_but_is_not_production_proof(self):
        supervisor = TestOnlySupervisor()
        adapter = CodexAdapter(BINARY, enabled=True, runner=self.runner)
        result = adapter.run_canary(inspection=VALID, workspace='/work/canary',
            schema_path='/opt/c3/schema.json', model='model-v1', supervisor=supervisor)
        self.assertEqual(result, CanaryCandidate('success', 333283335000))
        self.assertFalse(result.independently_validated)
        self.assertEqual(len(supervisor.requests), 1)
        self.runner.assert_not_called()

    def test_checksum_is_only_a_candidate_even_when_wrong(self):
        self.assertEqual(parse_candidate(completed(candidate_stream(7))).reported_checksum, 7)
        self.assertFalse(parse_candidate(completed(candidate_stream())).independently_validated)

    def test_invalid_or_failed_event_stream_is_not_success(self):
        for result in [completed(code=1), completed(b''), completed(b'not-json'),
                       completed(candidate_stream() + b'{"type":"turn.failed"}\n'),
                       completed(candidate_stream() + b'{"type":"turn.completed"}\n'),
                       completed(candidate_stream(True)), completed(candidate_stream(-1)),
                       completed(candidate_stream(7, 'failure')),
                       completed(candidate_stream() + b'{"type":"error"}\n')]:
            with self.subTest(stdout=result.stdout[:60]), self.assertRaises(CodexBlocked):
                parse_candidate(result)
        failure = parse_candidate(completed(candidate_stream(None, 'failure')))
        self.assertEqual(failure, CanaryCandidate('failure', None))


if __name__ == '__main__':
    unittest.main()
