"""Closed manager read evidence. No credentials, network, Docker or models.

The Linux resource test uses a real failed fork in a separate unprivileged
process. Its identity-environment fixture does not validate deployment security.
"""
from contextlib import redirect_stderr
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from research_harness import manager_github as github
from research_harness.manager_runtime import safe_recheck_failure
from research_harness.schema import ContractError
from test_harness_manager_github import response
from test_harness_issue_intake import raw

SECRET = 'TOKEN PRIVATE-MODEL-OUTPUT /private/path'


class ReadDiagnosticTests(unittest.TestCase):
    def capture_get(self, code, output):
        stream = io.StringIO()
        client = github.ManagerGitHub()
        with redirect_stderr(stream), patch.object(client, '_environment'), \
                patch.object(client, '_executable', return_value='/usr/bin/gh'), \
                patch.object(client, '_run', return_value=(code, output)):
            with self.assertRaises((ContractError, github.ManagerGitHubHTTPError)) as caught:
                client.get_issue(100)
        self.assertNotIn(SECRET, stream.getvalue())
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        return records, caught.exception

    def test_http_envelope_cli_status_json_and_http_error_are_distinct(self):
        for code, output, reason, status in (
                (2, SECRET.encode(), 'HTTP_ENVELOPE_INVALID', None),
                (1, response(raw()), 'HTTP_CLI_MISMATCH', 200),
                (0, b'HTTP/2.0 200 OK\r\n\r\n' + SECRET.encode(), 'HTTP_JSON_INVALID', 200),
                (1, response({'message': SECRET}, 503), 'HTTP_STATUS_REJECTED', 503)):
            with self.subTest(reason=reason):
                records, exc = self.capture_get(code, output)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['reason'], reason)
                self.assertEqual(records[0]['command_exit_code'], code)
                self.assertEqual(records[0].get('http_status'), status)
                # Existing rejection normalization remains conservative, unchanged.
                self.assertEqual(safe_recheck_failure(exc)['reason'],
                                 'source_unavailable' if status == 503 else 'contract')

    def test_successful_response_has_no_failure_record(self):
        stream = io.StringIO(); client = github.ManagerGitHub()
        with redirect_stderr(stream), patch.object(client, '_environment'), \
                patch.object(client, '_executable', return_value='/usr/bin/gh'), \
                patch.object(client, '_run', return_value=(0, response(raw()))):
            self.assertEqual(client.get_issue(100)['number'], 100)
        self.assertEqual(stream.getvalue(), '')

    def test_closed_values_and_cgroup_counters_only(self):
        values = {'pids.current': '61\n', 'pids.max': '64\n', 'pids.events': 'max 7\n'}
        def read(path, **kwargs): return io.StringIO(values[path.rsplit('/', 1)[1]])
        stream = io.StringIO()
        with redirect_stderr(stream), patch('builtins.open', side_effect=read):
            github.report_read_failure(SECRET, command_exit_code=1)
            github.report_read_failure('CLI_PROCESS_FAILED', command_exit_code=SECRET,
                                       http_status=True, os_errno=errno.EAGAIN)
        record = json.loads(stream.getvalue())
        self.assertEqual(record, dict(phase='MANAGER_GITHUB_READ_FAILED',
            reason='CLI_PROCESS_FAILED', os_errno=errno.EAGAIN,
            cgroup_pids_current=61, cgroup_pids_max=64, cgroup_pids_denied_total=7))
        self.assertNotIn(SECRET, stream.getvalue())

    def test_diagnostic_failure_does_not_replace_original_rejection(self):
        class BrokenLog:
            def write(self, _): raise OSError(SECRET)
        client = github.ManagerGitHub()
        with patch.object(github.sys, 'stderr', BrokenLog()), \
                patch('builtins.open', side_effect=OSError(SECRET)), \
                patch.object(client, '_environment'), \
                patch.object(client, '_executable', return_value='/usr/bin/gh'), \
                patch.object(client, '_run', return_value=(2, SECRET.encode())):
            with self.assertRaises(github.ManagerGitHubError) as caught:
                client.get_issue(100)
        self.assertNotIn(SECRET, str(caught.exception))

    @unittest.skipUnless(sys.platform.startswith('linux') and os.getuid() != 0,
                         'requires Linux unprivileged fork limits')
    def test_real_fork_denial_keeps_errno_without_credential_or_network_use(self):
        code = r'''
import os,resource
from research_harness.manager_github import ManagerGitHub,ManagerGitHubError
# Identity is a test fixture only; run no gh and read no authentication.
ManagerGitHub._environment=staticmethod(lambda: {'PATH':'/usr/bin:/bin'})
resource.setrlimit(resource.RLIMIT_NPROC,(0,0))
try:
    ManagerGitHub._run(['/usr/bin/true'])
except ManagerGitHubError:
    raise SystemExit(0)
raise SystemExit(3)
'''
        process = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True,
                                 timeout=10, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(process.returncode, 0)
        record = json.loads(process.stderr)
        self.assertEqual(record['phase'], 'MANAGER_GITHUB_READ_FAILED')
        self.assertEqual(record['reason'], 'CLI_PROCESS_FAILED')
        self.assertEqual(record['os_errno'], errno.EAGAIN)
        self.assertNotIn('command_exit_code', record)  # No process was launched.
        self.assertNotIn('http_status', record)         # No HTTP was received.
        self.assertEqual(process.stdout, b'')
