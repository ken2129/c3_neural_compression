"""Real file-mode regressions; no Docker, credentials, network or model calls."""
from contextlib import redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from research_harness.goal_runtime import _protected, GoalRuntimeBlocked
from research_harness.schema import canonical, ContractError
from tests.test_harness_goal_deployment import launcher, prepare, goal_fixture, profile_fixture


class GoalInputManifestModeTests(unittest.TestCase):
    def test_committed_context_manifest_is_protected_under_host_umasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source'; source.mkdir()
            def git(*args):
                return subprocess.check_output(['git', '-C', str(source), *args], stderr=subprocess.DEVNULL)
            git('init', '-q')
            (source/'research_harness').mkdir()
            (source/'research_harness/example.py').write_text('# committed public code\n')
            (source/'public.json').write_bytes(b'{"public":true}\n')
            git('add', '.')
            git('-c', 'user.name=Offline test', '-c', 'user.email=test@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
            sha = git('rev-parse', 'HEAD').decode().strip()
            profile = profile_fixture()
            profile.update(runtime_source_sha=sha, input_source_sha=sha)
            plan = goal_fixture(); plan.update(source_sha=sha, inputs=['public.json'])
            expected = canonical(dict(source_sha=sha, files={'public.json':dict(
                size_bytes=16, sha256=hashlib.sha256(b'{"public":true}\n').hexdigest())}))
            # Include the lab host's 0002, plus more permissive/restrictive masks.
            for mask in (0o002, 0o000, 0o022, 0o077):
                with self.subTest(umask=oct(mask)):
                    destination = root/('context-'+str(mask)); destination.mkdir()
                    previous = os.umask(mask)
                    try:
                        with patch.object(prepare, 'ROOT', source):
                            prepare.build_context(destination, profile, plan)
                    finally:
                        os.umask(previous)
                    manifest = destination/'goal-inputs.json'
                    self.assertEqual(manifest.read_bytes(), expected)
                    self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o444)
                    _protected(manifest, owner=os.getuid())
                    self.assertEqual(stat.S_IMODE((destination/'goal-inputs/public.json').stat().st_mode), 0o444)

    def test_observed_0664_is_still_rejected_without_relaxing_the_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory)/'goal-inputs.json'
            manifest.write_bytes(b'{"public":true}\n'); manifest.chmod(0o664)
            with self.assertRaises(GoalRuntimeBlocked) as caught:
                _protected(manifest, owner=os.getuid())
            from research_harness.goal_runtime import safe_goal_runtime_diagnostic
            self.assertEqual(safe_goal_runtime_diagnostic(caught.exception)['reason'], 'GOAL_PROTECTED_PATH_INVALID')


class GoalPreclaimDiagnosticTests(unittest.TestCase):
    def test_supervise_rejects_real_manifest_before_ready_or_model_dispatch(self):
        from types import SimpleNamespace
        from research_harness.restart_boundary import RestartEpoch
        plan = goal_fixture(); profile = prepare.installed_profile(profile_fixture(), plan)
        frames = [{'op':'prepare_goal', 'plan':plan}, {'op':'preflight'}]
        incoming = io.BytesIO(b''.join(canonical(f)+b'\n' for f in frames))
        class Pipe(io.BytesIO):
            def close(self): self.saved = self.getvalue(); super().close()
        outgoing = Pipe()
        control = SimpleNamespace(pid=22, stdin=outgoing, stdout=incoming)
        epoch = RestartEpoch('a'*64, '11111111-1111-1111-1111-111111111111', 22, 33)
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory)/'goal-inputs.json'
            manifest.write_bytes(b'public'); manifest.chmod(0o664)
            backend = SimpleNamespace(admit_goal=lambda request: _protected(manifest, owner=os.getuid()))
            output = io.StringIO()
            with patch.object(launcher, 'deployment', return_value=({'container_id':'a'*64}, profile, False)), \
                 patch.object(launcher, 'read_epoch', return_value=epoch), \
                 patch.object(launcher.subprocess, 'Popen', return_value=control), \
                 patch.object(launcher, 'stage_workspace', return_value=object()), \
                 patch.object(launcher, 'GoalLinuxUIDBackend', return_value=backend), \
                 patch.object(launcher, 'GoalUIDSupervisor') as supervisor, \
                 patch.object(launcher.time, 'sleep'), redirect_stderr(output), \
                 self.assertRaises(GoalRuntimeBlocked):
                launcher.supervise()
            supervisor.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), dict(
            phase='GOAL_PRECLAIM_BLOCKED', stage='admission', reason='GOAL_PROTECTED_PATH_INVALID'))
        replies = [json.loads(line) for line in outgoing.saved.splitlines()]
        self.assertEqual(replies[1:], [{'prepared':True}])  # hello + prepared, never ready/goal

    def test_real_mode_rejection_keeps_reason_and_admission_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory)/'goal-inputs.json'
            manifest.write_bytes(b'PRIVATE_CONTENT'); manifest.chmod(0o664)
            output = io.StringIO()
            with redirect_stderr(output), self.assertRaises(GoalRuntimeBlocked):
                with launcher.preclaim_stage('admission'):
                    _protected(manifest, owner=os.getuid())
            self.assertEqual(json.loads(output.getvalue()), dict(
                phase='GOAL_PRECLAIM_BLOCKED', stage='admission', reason='GOAL_PROTECTED_PATH_INVALID'))
            self.assertNotIn('PRIVATE', output.getvalue())
            self.assertNotIn(directory, output.getvalue())

    def test_unknown_errors_and_forged_attributes_never_escape(self):
        class Forged(GoalRuntimeBlocked): pass
        for exc, reason in ((ContractError('PRIVATE'), 'GOAL_CONTRACT_FAILURE'),
                (PermissionError('PRIVATE'), 'GOAL_OS_FAILURE'),
                (TimeoutError('PRIVATE'), 'GOAL_TIMEOUT'),
                (RuntimeError('PRIVATE'), 'GOAL_RUNTIME_FAILURE'),
                (Forged('PRIVATE'), 'GOAL_CONTRACT_FAILURE')):
            exc.reason = exc.stage = 'PRIVATE'
            output = io.StringIO()
            with redirect_stderr(output), self.assertRaises(type(exc)):
                with launcher.preclaim_stage('staging'):
                    raise exc
            self.assertEqual(json.loads(output.getvalue()), dict(
                phase='GOAL_PRECLAIM_BLOCKED', stage='staging', reason=reason))
            self.assertNotIn('PRIVATE', output.getvalue())

    def test_success_does_not_emit_a_failure_or_suppress_checks(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory)/'goal-inputs.json'
            manifest.write_bytes(b'public'); manifest.chmod(0o444)
            with redirect_stderr(output), launcher.preclaim_stage('admission'):
                _protected(manifest, owner=os.getuid())
        self.assertEqual(output.getvalue(), '')

    def test_logging_failure_does_not_replace_the_original_rejection(self):
        original = GoalRuntimeBlocked('GOAL_PROTECTED_PATH_INVALID')
        with patch.object(launcher.sys, 'stderr') as output:
            output.write.side_effect = OSError('PRIVATE')
            with self.assertRaises(GoalRuntimeBlocked) as caught:
                with launcher.preclaim_stage('admission'):
                    raise original
        self.assertIs(caught.exception, original)
