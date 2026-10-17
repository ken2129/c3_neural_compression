"""Private pipe integration, fake subprocess/OS only. Never run a real launcher."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from research_harness.restart_boundary import RestartEpoch
from research_harness.schema import ContractError
from research_harness.manager_runtime import LIMIT
from test_harness_service_state import binding_fixture

spec=importlib.util.spec_from_file_location('service_launcher',Path(__file__).resolve().parents[1]/'tools/research_service/launcher.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
EPOCH=RestartEpoch('a'*64,'11111111-1111-1111-1111-111111111111',22,33)

class FakePipe(io.StringIO):
    def close(self):self.saved=self.getvalue();super().close()

class Supervisor:
    def __init__(self,backend,controller_pid):
        if controller_pid!=22:raise AssertionError('wrong controller')
    def reserve(self):return object()
    def run_fixed_canary(self,recheck,wall_seconds):
        recheck();return SimpleNamespace(checksum=333283335000,elapsed_seconds=.01)

class LauncherTests(unittest.TestCase):
    def invoke(self,frames):
        control=SimpleNamespace(pid=22,stdin=FakePipe(),stdout=io.StringIO(''.join(json.dumps(v)+'\n' for v in frames)))
        with patch.object(launcher,'deployment',return_value={'container_id':'a'*64,'source_sha':'b'*40,'initialize_new_state':False}), \
             patch.object(launcher,'read_epoch',return_value=EPOCH), \
             patch.object(launcher.subprocess,'Popen',return_value=control) as popen, \
             patch.object(launcher,'OwnedUIDSupervisor',Supervisor), \
             patch.object(launcher,'LinuxUIDBackend',return_value=object()), \
             patch.object(launcher.time,'sleep'),patch.object(launcher.sys,'stdout',io.StringIO()):
            launcher.supervise()
        return control,popen

    def test_fixed_run_recheck_then_namespace_restart(self):
        control,popen=self.invoke([{'op':'preflight'},{'op':'fixed_cpu','binding':binding_fixture()},
            {'approved':True},{'op':'report','reports':[{'restart_required':True}]},{'op':'restart'}])
        sent=[json.loads(line) for line in control.stdin.saved.splitlines()]
        self.assertEqual(sent[1],{'ready':True})
        self.assertEqual(sent[2],{'op':'recheck'})
        self.assertFalse(sent[3]['result']['model_called'])
        self.assertEqual(popen.call_args.kwargs['user'],10001)
        self.assertNotIn('GH_TOKEN',popen.call_args.kwargs['env'])
        self.assertTrue(popen.call_args.kwargs['close_fds'])

    def test_arbitrary_manager_command_is_rejected(self):
        with self.assertRaises(ContractError):self.invoke([{'op':'shell','command':'id'}])

    def test_integer_approval_does_not_release_worker(self):
        with self.assertRaises(ContractError):self.invoke([{'op':'fixed_cpu','binding':binding_fixture()},{'approved':1}])

    def test_second_attempt_same_generation_is_rejected(self):
        with self.assertRaises(ContractError):self.invoke([
            {'op':'fixed_cpu','binding':binding_fixture()},{'approved':True},
            {'op':'fixed_cpu','binding':binding_fixture()}])


class ManagerStartupTests(unittest.TestCase):
    """Real temporary state; fake identity and remote adapters only."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = Path(self.temp.name)
        self.manifest = self.control / 'deployment.json'
        self.manifest.write_text(json.dumps({'initialize_new_state': True,
                                             'source_sha': 'b' * 40}))
        self.manifest.chmod(0o600)

    def run_manager(self, initialize_new, reports=None):
        if reports is None:
            reports = [{'phase': 'OFFLINE_TEST', 'restart_required': True,
                        'dispatched': False}]
        incoming = io.StringIO(json.dumps({'epoch': EPOCH.to_dict(),
            'source_sha': 'b' * 40, 'initialize_new_state': initialize_new}) + '\n')
        outgoing = io.StringIO()
        def local_path(value):
            path = Path(value)
            return self.control / path.relative_to('/control')
        # manager() sets a process-local umask; restore the test process afterward.
        old_umask = os.umask(0o077)
        try:
            with patch.object(launcher, 'Path', side_effect=local_path), \
                 patch.object(launcher.os, 'getresuid', return_value=(10001,) * 3), \
                 patch.object(launcher.sys, 'stdin', incoming), \
                 patch.object(launcher.sys, 'stdout', outgoing), \
                 patch('research_harness.issue_intake.PublicGitHub', return_value=object()), \
                 patch('research_harness.owner_approval.GitHubOwnerComments', return_value=object()), \
                 patch('research_harness.result_publisher.GhResultTransport', return_value=object()), \
                 patch('research_harness.service.Controller',
                       return_value=SimpleNamespace(once=lambda: reports)):
                launcher.manager()
        finally:
            os.umask(old_umask)
        return outgoing.getvalue()

    def test_first_initialize_consumes_intent_and_resume_preserves_state(self):
        self.run_manager(True)
        manifest = json.loads(self.manifest.read_text())
        self.assertIs(manifest['initialize_new_state'], False)
        self.assertEqual(manifest['source_sha'], 'b' * 40)
        marker = self.control / 'state-initialized.json'
        self.assertEqual(json.loads(marker.read_text()), {'container_id': EPOCH.container_id})
        identity = (self.control / 'state' / 'initialized.json').read_bytes()
        self.run_manager(False)
        self.assertEqual((self.control / 'state' / 'initialized.json').read_bytes(), identity)

    def test_consumed_intent_cannot_reinitialize_missing_marker_and_state(self):
        self.run_manager(True)
        shutil.rmtree(self.control / 'state')
        (self.control / 'state-initialized.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_manager(False)
        self.assertFalse((self.control / 'state').exists())
        self.assertFalse((self.control / 'state-initialized.json').exists())
        self.assertIs(json.loads(self.manifest.read_text())['initialize_new_state'], False)

    def test_interrupted_initialization_is_not_retried(self):
        marker = self.control / 'state-initialized.json'
        marker.write_text(json.dumps({'container_id': EPOCH.container_id}))
        marker.chmod(0o600)
        with self.assertRaisesRegex(ContractError, 'initialization was interrupted'):
            self.run_manager(True)
        self.assertFalse((self.control / 'state').exists())

    def test_many_reports_are_each_sent_in_a_separate_bounded_frame(self):
        reports = [dict(issue=number, phase='PUBLISHED', dispatched=False,
                        duplicate=True, historical=True, notification='UNCONFIGURED',
                        pr_url='https://github.com/ken2129/c3_neural_compression/pull/123')
                   for number in range(200)]
        reports[-1]['restart_required'] = True
        self.assertGreater(len(json.dumps(reports).encode()), LIMIT)
        lines = self.run_manager(True, reports).splitlines(keepends=True)
        frames = [json.loads(line) for line in lines]
        self.assertEqual(len(frames), 201)
        self.assertEqual(frames[-1], {'op': 'restart'})
        self.assertTrue(all(len(frame['reports']) == 1 for frame in frames[:-1]))
        self.assertTrue(all(len(line.encode()) <= LIMIT for line in lines))
