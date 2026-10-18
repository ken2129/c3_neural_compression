"""Mocked manager reads only: no GitHub, credentials, models or real subprocesses."""
from contextlib import ExitStack, redirect_stderr
import io
import json
import os
import signal
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from research_harness.manager_github import (
    ManagerGitHub, ManagerGitHubError, ManagerGitHubHTTPError, ManagerGitHubRateLimited, ManagerGitHubTimeout,
)
from research_harness.schema import ContractError, REPOSITORY
from research_harness.service import source_failure_report, source_retry_delay
from test_harness_issue_intake import raw


NOW = 1800000000
LIST = '/issues?state=open&sort=created&direction=asc&per_page=100&page='


def response(data, status=200, headers=()):
    return (f'HTTP/2.0 {status} Status\r\n'.encode()
            + b''.join(f'{key}: {value}\r\n'.encode() for key, value in headers)
            + b'\r\n' + json.dumps(data).encode())


class ManagerGitHubTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.monotonic = NOW, 10
        self.client = ManagerGitHub(clock=lambda: self.wall, monotonic=lambda: self.monotonic)
        self.uid = patch('research_harness.manager_github.os.getresuid', return_value=(10001,) * 3)
        self.uid.start()
        self.addCleanup(self.uid.stop)
        self.binary = patch.object(self.client, '_executable', return_value='/usr/bin/gh')
        self.binary.start()
        self.addCleanup(self.binary.stop)

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds

    def test_fixed_authenticated_get_retains_snapshot_identity_validation(self):
        with patch.object(self.client, '_run', return_value=(0, response(raw()))) as run:
            issue = self.client.get_issue(100)
        self.assertEqual(issue['number'], 100)
        self.assertEqual(run.call_args.args[0], [
            '/usr/bin/gh', 'api', '--hostname', 'github.com', '--method', 'GET', '--include',
            '-H', 'Accept: application/vnd.github+json', '-H', 'X-GitHub-Api-Version: 2022-11-28',
            f'repos/{REPOSITORY}/issues/100'])
        changed = raw()
        changed['repository_url'] = 'https://example.test/other'
        with patch.object(self.client, '_run', return_value=(0, response(changed))):
            with self.assertRaises(ContractError):
                self.client.get_issue(100)

    def test_list_pagination_pr_filter_and_incomplete_scan_semantics_unchanged(self):
        page = [raw(i) for i in range(1, 101)]
        page[0]['pull_request'] = {}
        page[1] = raw(2, prefix=False)
        with patch.object(self.client, '_run', side_effect=[(0, response(page)), (0, response([]))]) as run:
            found = self.client.list_candidates()
        self.assertEqual(len(found), 98)
        self.assertNotIn(1, found)
        self.assertNotIn(2, found)
        self.assertTrue(run.call_args_list[0].args[0][-1].endswith(LIST + '1'))
        self.assertTrue(run.call_args_list[1].args[0][-1].endswith(LIST + '2'))
        with patch.object(self.client, '_run', return_value=(0, response(page))):
            with self.assertRaisesRegex(ContractError, 'pagination limit'):
                self.client.list_candidates()

    def test_only_exact_closed_issue_routes_are_accepted(self):
        for suffix in ('https://example.test', '/issues/1/comments', '/graphql',
                       '/issues/1?foo=x', '/issues/01', '/issues/0', '/issues/2147483648',
                       '/issues/1/../../other', '/issues/1\n', LIST + '0', LIST + '21',
                       LIST + '01', LIST + '1&per_page=200', None):
            with self.subTest(suffix=suffix), patch.object(self.client, '_run') as run:
                with self.assertRaises(ManagerGitHubError):
                    self.client._get(suffix)
                run.assert_not_called()

    def test_manager_identity_required_before_any_read_process(self):
        for ids in ((0, 0, 0), (10002,) * 3, (0, 10001, 0)):
            with patch('research_harness.manager_github.os.getresuid', return_value=ids), \
                    patch.object(self.client, '_run') as run:
                with self.assertRaises(ManagerGitHubError):
                    self.client.get_issue(100)
                run.assert_not_called()

    def test_environment_is_fixed_existing_manager_config_without_ambient_tokens(self):
        with patch.dict(os.environ, {'HOME': '/attacker', 'GH_CONFIG_DIR': '/worker',
                'GH_TOKEN': 'SENSITIVE_SENTINEL', 'GITHUB_TOKEN': 'SENSITIVE_SENTINEL',
                'GH_DEBUG': 'api', 'HTTPS_PROXY': 'https://example.test',
                'GH_HOST': 'example.test', 'PATH': '/attacker', 'GOMAXPROCS': '99'}, clear=True):
            env = self.client._environment()
        self.assertEqual(env['HOME'], '/control/home')
        self.assertEqual(env['GH_CONFIG_DIR'], '/control/gh')
        self.assertEqual(env['GH_HOST'], 'github.com')
        self.assertEqual(env['GH_PROMPT_DISABLED'], '1')
        self.assertEqual(env['GOMAXPROCS'], '1')
        self.assertNotIn('SENSITIVE_SENTINEL', repr(env))
        self.assertFalse({'GH_TOKEN', 'GITHUB_TOKEN', 'GH_DEBUG', 'HTTPS_PROXY'} & env.keys())

    def test_http_error_exposes_only_safe_status_and_numeric_rate_headers(self):
        headers = [('X-RateLimit-Remaining', 0), ('X-RateLimit-Reset', NOW + 600),
                   ('Authorization', 'SENSITIVE_SENTINEL'), ('X-Unknown', 'SENSITIVE_SENTINEL')]
        with patch.object(self.client, '_run', return_value=(1, response({'secret': 'SENSITIVE_SENTINEL'}, 403, headers))):
            with self.assertRaises(ManagerGitHubRateLimited) as caught:
                self.client.list_candidates()
        error = caught.exception
        self.assertIsInstance(error, HTTPError)
        self.assertEqual(error.code, 403)
        self.assertEqual(error.status, 403)
        self.assertEqual(error.retry_at, NOW + 601)
        self.assertEqual(set(error.headers.keys()), {'X-RateLimit-Remaining', 'X-RateLimit-Reset'})
        self.assertEqual(error.read(), b'')
        self.assertNotIn('SENSITIVE_SENTINEL', repr(error) + str(error) + repr(error.headers))
        report = source_failure_report(error)
        self.assertEqual(report['http_status'], 403)
        self.assertEqual(report['rate_remaining'], 0)
        self.assertGreaterEqual(source_retry_delay([report], now=NOW), 601)

    def test_every_get_is_suppressed_until_known_reset_then_reads_resume(self):
        limited = response({}, 403, [('X-RateLimit-Remaining', 0), ('X-RateLimit-Reset', NOW + 600)])
        with patch.object(self.client, '_run', side_effect=[(1, limited), (0, response(raw()))]) as run:
            with self.assertRaises(ManagerGitHubRateLimited):
                self.client.list_candidates()
            self.advance(600)
            for call in (self.client.list_candidates, lambda: self.client.get_issue(100)):
                with self.assertRaises(ManagerGitHubRateLimited):
                    call()
            self.assertEqual(run.call_count, 1)
            self.advance(1)
            self.assertEqual(self.client.get_issue(100)['number'], 100)
            self.assertEqual(run.call_count, 2)
            self.assertIsNone(self.client.retry_at)

    def test_429_retry_after_and_missing_reset_use_bounded_cooldown(self):
        for headers, expected in (([], 300), ([('Retry-After', 900)], 900),
                                  ([('Retry-After', 86401)], 300),
                                  ([('X-RateLimit-Reset', NOW - 1)], 300)):
            client = ManagerGitHub(clock=lambda: NOW, monotonic=lambda: 10)
            with patch.object(client, '_executable', return_value='/usr/bin/gh'), \
                    patch.object(client, '_run', return_value=(1, response({}, 429, headers))) as run:
                with self.assertRaises(ManagerGitHubRateLimited) as caught:
                    client.get_issue(100)
                self.assertEqual(caught.exception.retry_at, NOW + expected)
                with self.assertRaises(ManagerGitHubRateLimited):
                    client.get_issue(100)
                self.assertEqual(run.call_count, 1)

    def test_successful_last_budget_response_closes_gate_for_future_reads(self):
        output = response(raw(), headers=[('X-RateLimit-Remaining', 0), ('X-RateLimit-Reset', NOW + 500)])
        with patch.object(self.client, '_run', return_value=(0, output)) as run:
            self.assertEqual(self.client.get_issue(100)['number'], 100)
            with self.assertRaises(ManagerGitHubRateLimited):
                self.client.list_candidates()
            self.assertEqual(run.call_count, 1)

    def test_wall_clock_forward_jump_cannot_bypass_monotonic_rate_gate(self):
        with patch.object(self.client, '_run', return_value=(1, response({}, 429))) as run:
            with self.assertRaises(ManagerGitHubRateLimited):
                self.client.get_issue(100)
            self.wall += 10000
            with self.assertRaises(ManagerGitHubRateLimited):
                self.client.get_issue(100)
            self.assertEqual(run.call_count, 1)

    def test_other_http_errors_and_cli_failures_do_not_become_empty_queue(self):
        for status in (301, 401, 404, 500):
            with patch.object(self.client, '_run', return_value=(1, response({}, status))):
                with self.assertRaises(ManagerGitHubHTTPError) as caught:
                    self.client.list_candidates()
                self.assertEqual(caught.exception.code, status)
        for code, output in ((1, b'SENSITIVE_SENTINEL'), (1, response([])),
                             (0, b'HTTP/2.0 200 OK\r\n\r\n{"x":NaN}'),
                             (0, b'HTTP/2.0 200 OK\r\nX-RateLimit-Reset: 1\r\nX-RateLimit-Reset: 2\r\n\r\n[]')):
            with patch.object(self.client, '_run', return_value=(code, output)):
                with self.assertRaises(ManagerGitHubError) as caught:
                    self.client.list_candidates()
                self.assertNotIn('SENSITIVE_SENTINEL', str(caught.exception))


class FakeStream:
    def __init__(self, fd):
        self.fd, self.closed = fd, False
    def fileno(self):
        return self.fd
    def close(self):
        self.closed = True


class FakeSelector:
    def __init__(self):
        self.entries = {}
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def register(self, stream, events):
        self.entries[stream.fd] = SimpleNamespace(fd=stream.fd, fileobj=stream)
    def unregister(self, stream):
        self.entries.pop(stream.fd)
    def get_map(self):
        return self.entries
    def select(self, timeout):
        return [(key, 1) for key in list(self.entries.values())]


class ManagerSubprocessTests(unittest.TestCase):
    def run_fake(self, stdout, stderr=b'', *, cap=None, times=None, wait_error=None,
                 code=0, stderr_chunks=None):
        process = SimpleNamespace(pid=4321, stdout=FakeStream(17), stderr=FakeStream(18),
                                  wait=Mock(return_value=code))
        if wait_error is not None:
            process.wait.side_effect = [wait_error, 0]
        chunks = {17: [stdout, b''],
                  18: ([stderr] if stderr_chunks is None else list(stderr_chunks)) + [b'']}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('research_harness.manager_github.os.getresuid', return_value=(10001,) * 3))
        popen = stack.enter_context(patch('research_harness.manager_github.subprocess.Popen', return_value=process))
        stack.enter_context(patch('research_harness.manager_github.selectors.DefaultSelector', FakeSelector))
        stack.enter_context(patch('research_harness.manager_github.os.set_blocking'))
        stack.enter_context(patch('research_harness.manager_github.os.read', side_effect=lambda fd, size: chunks[fd].pop(0)))
        kill = stack.enter_context(patch('research_harness.manager_github.os.killpg'))
        if cap is not None:
            stack.enter_context(patch('research_harness.manager_github.MAX_RESPONSE', cap))
        if times is not None:
            stack.enter_context(patch('research_harness.manager_github.time.monotonic', side_effect=times))
        return process, popen, kill

    def test_fixed_process_flags_and_stderr_is_counted_but_never_returned(self):
        process, popen, kill = self.run_fake(b'RESPONSE', b'SENSITIVE_SENTINEL')
        code, output = ManagerGitHub._run(['/usr/bin/gh', 'api'])
        self.assertEqual((code, output), (0, b'RESPONSE'))
        self.assertIs(popen.call_args.kwargs['shell'], False)
        self.assertIs(popen.call_args.kwargs['close_fds'], True)
        self.assertIs(popen.call_args.kwargs['start_new_session'], True)
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs['env']['GH_CONFIG_DIR'], '/control/gh')
        self.assertEqual(popen.call_args.kwargs['env']['GOMAXPROCS'], '1')
        self.assertTrue(process.stdout.closed and process.stderr.closed)
        kill.assert_not_called()

    def test_nonzero_stderr_categories_survive_split_reads_without_returning_stderr(self):
        secret = b'SENSITIVE_SENTINEL /private/path\n'
        signatures = (
            ('GO_THREAD_CREATE_FAILED', [b'runtime: failed to create new OS ',
                b'thread (have 62 already; errno=11)\n', b'fatal error: new', b'osproc\n', secret]),
            ('GO_THREAD_CREATE_FAILED', [b'runtime/cgo: pthread_',
                b'create failed: Resource temporarily unavailable\n', secret]),
            ('GO_RUNTIME_FATAL', [b'fatal error: unexpected signal during runtime execution\n',
                b'runtime sta', b'ck:\n', secret]),
            ('GO_THREAD_CREATE_FAILED', [b'runtime: failed to create new OS thread\r\n',
                b'fatal error: newosproc']),  # EOF completes the final line.
            ('OTHER', [secret]),
            ('NONE', []),
        )
        for category, chunks in signatures:
            with self.subTest(category=category):
                process, _, kill = self.run_fake(b'', code=2, stderr_chunks=chunks)
                output = io.StringIO()
                with redirect_stderr(output), patch('builtins.open', side_effect=OSError('offline')):
                    self.assertEqual(ManagerGitHub._run(['/usr/bin/gh', 'api']), (2, b''))
                self.assertEqual(json.loads(output.getvalue()), dict(
                    phase='MANAGER_GITHUB_READ_FAILED', reason='CLI_EXIT_FAILED',
                    command_exit_code=2, stderr_category=category))
                self.assertNotIn('SENSITIVE_SENTINEL', output.getvalue())
                self.assertTrue(process.stdout.closed and process.stderr.closed)
                kill.assert_not_called()

    def test_incomplete_or_unanchored_go_signatures_remain_other(self):
        inputs = (
            b'runtime: failed to create new OS thread (errno=11)\n',
            b'fatal error: newosproc\n',
            b'runtime stack:\n',
            b'prefix runtime: failed to create new OS thread\nprefix fatal error: newosproc\n',
            b'prefix fatal error: bad\nruntime stack:\n',
            b'prefix runtime/cgo: pthread_create failed: Resource temporarily unavailable\n',
            b'runtime/cgo: pthread_create failure: malformed marker\n',
            b'fatal error: bad\nprefix runtime stack:\n',
        )
        for value in inputs:
            with self.subTest(value=value):
                self.run_fake(b'', value, code=2)
                output = io.StringIO()
                with redirect_stderr(output), patch('builtins.open', side_effect=OSError('offline')):
                    self.assertEqual(ManagerGitHub._run(['/usr/bin/gh', 'api']), (2, b''))
                self.assertEqual(json.loads(output.getvalue())['stderr_category'], 'OTHER')

    def test_go_signatures_split_into_single_bytes_and_long_stderr_are_bounded(self):
        signature = (b'runtime: failed to create new OS thread (errno=11)\n'
                     b'fatal error: newosproc\n')
        chunks = [bytes([byte]) for byte in signature]
        chunks += [b'PRIVATE_BODY' * 1000, b'\n']
        self.run_fake(b'', code=2, stderr_chunks=chunks)
        output = io.StringIO()
        with redirect_stderr(output), patch('builtins.open', side_effect=OSError('offline')):
            self.assertEqual(ManagerGitHub._run(['/usr/bin/gh', 'api']), (2, b''))
        self.assertEqual(json.loads(output.getvalue())['stderr_category'], 'GO_THREAD_CREATE_FAILED')
        self.assertNotIn('PRIVATE_BODY', output.getvalue())
        self.assertLess(len(output.getvalue()), 256)

    def test_classifier_retains_bounded_prefix_without_false_chunk_line_anchor(self):
        from research_harness.manager_github import _StderrClassifier
        classifier = _StderrClassifier()
        classifier.feed(b'x' * 10000)
        self.assertLessEqual(len(classifier.prefix), 256)
        classifier.feed(b'runtime: failed to create new OS thread\n')
        classifier.feed(b'fatal error: newosproc')
        self.assertLessEqual(len(classifier.prefix), 256)
        self.assertEqual(classifier.category(), 'OTHER')

    def test_output_cap_terminates_only_owned_new_session(self):
        process, _, kill = self.run_fake(b'ok', b'SENSITIVE_SENTINEL', cap=10)
        with self.assertRaises(ManagerGitHubError) as caught:
            ManagerGitHub._run(['/usr/bin/gh', 'api'])
        self.assertNotIn('SENSITIVE_SENTINEL', str(caught.exception))
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_deadline_terminates_owned_session_without_more_reads(self):
        process, _, kill = self.run_fake(b'ignored', times=[0, 21])
        with self.assertRaisesRegex(ManagerGitHubTimeout, 'deadline') as caught:
            ManagerGitHub._run(['/usr/bin/gh', 'api'])
        self.assertEqual(source_failure_report(caught.exception)['error_category'],'TIMEOUT')
        kill.assert_called_once_with(process.pid, signal.SIGKILL)

    def test_subprocess_errors_are_sanitized(self):
        process, _, kill = self.run_fake(b'', wait_error=subprocess.TimeoutExpired('SENSITIVE_SENTINEL', 20))
        with self.assertRaises(ManagerGitHubError) as caught:
            ManagerGitHub._run(['/usr/bin/gh', 'api'])
        self.assertNotIn('SENSITIVE_SENTINEL', str(caught.exception))
        kill.assert_called_once_with(process.pid, signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
