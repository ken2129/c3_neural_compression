"""Private pipe integration, fake subprocess/OS only. Never run a real launcher."""
import importlib.util
import io
import json
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from research_harness.restart_boundary import RestartEpoch
from research_harness.schema import ContractError,canonical
from research_harness.manager_runtime import (LIMIT,RecheckRejected,
    safe_recheck_failure,RECHECK_REASONS,RECHECK_ORIGINS)
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
    def invoke(self,frames,*,raw_suffix=''):
        if any(frame.get('op')=='fixed_cpu' for frame in frames):
            plan=dict(kind='cpu-canary-v1',source_sha='b'*40,publish_result_pr=True)
            frames=[{'op':'prepare_plan','plan':plan},*frames]
            for frame in frames:
                if frame.get('op')=='fixed_cpu':frame['binding']=dict(frame['binding'],plan_digest=hashlib.sha256(canonical(plan)).hexdigest())
        control=SimpleNamespace(pid=22,stdin=FakePipe(),stdout=io.StringIO(''.join(json.dumps(v)+'\n' for v in frames)+raw_suffix))
        self.last_control=control
        with patch.object(launcher,'deployment',return_value={'container_id':'a'*64,'source_sha':'b'*40,'initialize_new_state':False}), \
             patch.object(launcher,'read_epoch',return_value=EPOCH), \
             patch.object(launcher.subprocess,'Popen',return_value=control) as popen, \
             patch.object(launcher,'OwnedUIDSupervisor',Supervisor), \
             patch.object(launcher,'LinuxUIDBackend',return_value=object()), \
             patch.object(launcher.time,'sleep') as restart_sleep,patch.object(launcher.sys,'stdout',io.StringIO()):
            launcher.supervise()
            control.restart_sleeps=restart_sleep.call_args_list
        return control,popen

    def test_fixed_run_recheck_then_namespace_restart(self):
        control,popen=self.invoke([{'op':'preflight'},{'op':'fixed_cpu','binding':binding_fixture()},
            {'approved':True},{'op':'report','reports':[{'restart_required':True}]},{'op':'restart'}])
        sent=[json.loads(line) for line in control.stdin.saved.splitlines()]
        self.assertEqual(sent[1],{'prepared':True})
        self.assertEqual(sent[2],{'ready':True})
        self.assertEqual(sent[3],{'op':'recheck'})
        self.assertFalse(sent[4]['result']['model_called'])
        self.assertEqual(popen.call_args.kwargs['user'],10001)
        self.assertNotIn('GH_TOKEN',popen.call_args.kwargs['env'])
        self.assertTrue(popen.call_args.kwargs['close_fds'])

    def test_model_preflight_missing_setup_never_calls_worker(self):
        plan=dict(kind='codex-canary-v1',source_sha='b'*40,publish_result_pr=True,
                  model='gpt-5.3-codex',sandbox_mode='externalSandbox',external_network='enabled',
                  max_seconds=120,model_usage_approved=True)
        with patch('research_harness.model_deployment.prepare_model',side_effect=ContractError('MODEL_OPERATOR_SETUP_MISSING')) as prepare:
            with self.assertRaisesRegex(ContractError,'MODEL_OPERATOR_SETUP_MISSING'):
                self.invoke([{'op':'prepare_plan','plan':plan},{'op':'preflight'}])
            prepare.assert_called_once()

    def test_model_branch_emits_candidate_only_and_requires_restart(self):
        plan=dict(kind='codex-canary-v1',source_sha='b'*40,publish_result_pr=True,
                  model='gpt-5.3-codex',sandbox_mode='externalSandbox',external_network='enabled',
                  max_seconds=120,model_usage_approved=True)
        binding=dict(binding_fixture(),plan_digest=hashlib.sha256(canonical(plan)).hexdigest())
        def fake_model(self,request,recheck,*,enabled,admit_external):
            if enabled is not True or not callable(admit_external):raise AssertionError('missing admission')
            recheck()
            return SimpleNamespace(candidate=SimpleNamespace(outcome='success',reported_checksum=333283335000),
              observed_command='fixed',command_exit_code=0,elapsed_seconds=.1)
        with patch('research_harness.model_deployment.prepare_model',return_value=SimpleNamespace(model=plan['model'])), \
             patch.object(Supervisor,'run_fixed_app_server_canary',fake_model,create=True):
            control,_=self.invoke([{'op':'prepare_plan','plan':plan},{'op':'preflight'},
              {'op':'fixed_model','binding':binding},{'approved':True},{'op':'restart'}])
        sent=[json.loads(line) for line in control.stdin.saved.splitlines()]
        result=next(value['model_result'] for value in sent if 'model_result' in value)
        self.assertEqual(result['status'],'candidate_only')
        self.assertFalse(result['research_success_verified'])
        self.assertTrue(result['requires_namespace_restart'])
        self.assertNotIn('observed_command',result)

    def test_prepared_model_is_consumed_by_any_report_or_next_admission(self):
        plan=dict(kind='codex-canary-v1',source_sha='b'*40,publish_result_pr=True,
                  model='gpt-5.3-codex',sandbox_mode='externalSandbox',external_network='enabled',
                  max_seconds=120,model_usage_approved=True)
        binding=dict(binding_fixture(),plan_digest=hashlib.sha256(canonical(plan)).hexdigest())
        outcomes=[
            {'op':'prepare_plan','plan':plan},
            {'op':'prepare_plan','plan':dict(kind='cpu-canary-v1',source_sha='b'*40,publish_result_pr=True)},
            {'op':'preflight'},
            {'op':'report','reports':[{'phase':'APPROVAL_REVOKED','dispatched':False}]},
            {'op':'report','reports':[{'phase':'SOURCE_UNAVAILABLE','dispatched':False}]},
            {'op':'report','reports':[]},
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), \
                 patch('research_harness.model_deployment.prepare_model',return_value=SimpleNamespace(model=plan['model'])) as prepare, \
                 patch.object(Supervisor,'run_fixed_app_server_canary',create=True) as run_model, \
                 patch.object(launcher.time,'monotonic',return_value=100):
                control,_=self.invoke([{'op':'prepare_plan','plan':plan},{'op':'preflight'},outcome,
                    {'op':'fixed_model','binding':binding},{'approved':True},{'op':'restart'}])
                prepare.assert_called_once()
                run_model.assert_not_called()
                sent=[json.loads(line) for line in control.stdin.saved.splitlines()]
                self.assertEqual(sent[1:],[{'prepared':True},{'ready':True}])
                self.assertEqual(len(control.restart_sleeps),1)
                self.assertEqual(control.restart_sleeps[0].args,(11,))
                # A later candidate's dispatch is left unread, never admitted.
                self.assertEqual(json.loads(control.stdout.readline())['op'],'fixed_model')

    def test_partial_model_preparation_failure_exits_instead_of_quarantining(self):
        plan=dict(kind='codex-canary-v1',source_sha='b'*40,publish_result_pr=True,
                  model='gpt-5.3-codex',sandbox_mode='externalSandbox',external_network='enabled',
                  max_seconds=120,model_usage_approved=True)
        with patch('research_harness.model_deployment.prepare_model',side_effect=PermissionError('partial auth preparation')), \
             patch.object(launcher,'blocked_startup') as blocked, \
             patch.object(Supervisor,'run_fixed_app_server_canary',create=True) as run_model:
            with self.assertRaises(PermissionError):
                self.invoke([{'op':'prepare_plan','plan':plan},{'op':'preflight'}])
            blocked.assert_not_called()
            run_model.assert_not_called()

    def test_runtime_source_must_match_operator_revision(self):
        binding=dict(binding_fixture(),source_sha='c'*40)
        with self.assertRaisesRegex(ContractError,'runtime plan binding mismatch'):
            self.invoke([{'op':'fixed_cpu','binding':binding}])

    def test_arbitrary_manager_command_is_rejected(self):
        with self.assertRaises(ContractError):self.invoke([{'op':'shell','command':'id'}])

    def test_integer_approval_does_not_release_worker(self):
        with self.assertRaises(ContractError):self.invoke([{'op':'fixed_cpu','binding':binding_fixture()},{'approved':1}])

    def assert_no_result(self):
        sent=[json.loads(line) for line in self.last_control.stdin.saved.splitlines()]
        self.assertFalse(any('result' in frame or 'model_result' in frame for frame in sent))
        self.assertEqual(sent[-1],{'op':'recheck'})

    def test_negative_reply_preserves_closed_reason_origin_and_never_completes_worker(self):
        for reason in RECHECK_REASONS:
            for origin in RECHECK_ORIGINS:
                with self.subTest(reason=reason,origin=origin), \
                     patch.object(launcher,'report_failure') as report:
                    with self.assertRaises(RecheckRejected) as caught:
                        self.invoke([{'op':'fixed_cpu','binding':binding_fixture()},
                            {'approved':False,'reason':reason,'origin':origin}])
                    self.assertEqual(safe_recheck_failure(caught.exception),{'reason':reason,'origin':origin})
                    self.assertEqual(caught.exception.args,('approval recheck rejected',))
                    self.assert_no_result()
                    report.assert_called_once_with('launcher','cpu_runtime',caught.exception)

    def test_malformed_negative_reply_eof_or_payload_never_leaks_diagnostics(self):
        secret='private-token https://secret.invalid/body stderr=private'
        frames=[{'approved':False},{'approved':True,'secret':secret},
            {'approved':False,'reason':secret,'origin':'manager'},
            {'approved':False,'reason':'changed','origin':secret},
            {'approved':False,'reason':'expired','origin':'manager','stderr':secret},
            {'approved':False,'reason':'expired'},
            {'approved':0,'reason':'expired','origin':'manager'},
            {'approved':True,'reason':'expired','origin':'manager'},
            {'approved':False,'reason':[],'origin':'manager'}]
        for frame in frames:
            with self.subTest(frame=frame),patch.object(launcher,'report_failure'):
                with self.assertRaises(RecheckRejected) as caught:
                    self.invoke([{'op':'fixed_cpu','binding':binding_fixture()},frame])
                self.assertEqual(safe_recheck_failure(caught.exception),
                    {'reason':'contract','origin':'launcher_ipc'})
                self.assertNotIn(secret,str(caught.exception))
                self.assert_no_result()
        for raw in ('','{"approved":','[]\n',
                '{"approved":false,"approved":true}\n',
                '{"approved":NaN}\n','x'*(LIMIT+1)+'\n','['*2000+']'*2000+'\n'):
            with self.subTest(raw=raw[:40]),patch.object(launcher,'report_failure'):
                with self.assertRaises(RecheckRejected) as caught:
                    self.invoke([{'op':'fixed_cpu','binding':binding_fixture()}],raw_suffix=raw)
                self.assertEqual(safe_recheck_failure(caught.exception),
                    {'reason':'contract','origin':'launcher_ipc'})
                self.assert_no_result()

    def test_recheck_io_failures_have_fixed_launcher_origin(self):
        secret='private-token https://secret.invalid/body stderr=private'
        original_read=launcher.read_frame
        original_write=launcher.write_frame
        def fail_read(stream):
            value=original_read(stream)
            if value=={'approved':True}:raise OSError(secret)
            return value
        def fail_write(stream,value):
            if value=={'op':'recheck'}:raise OSError(secret)
            return original_write(stream,value)
        for name,replacement in (('read_frame',fail_read),('write_frame',fail_write)):
            with self.subTest(operation=name),patch.object(launcher,name,replacement), \
                 patch.object(launcher,'report_failure'):
                with self.assertRaises(RecheckRejected) as caught:
                    self.invoke([{'op':'fixed_cpu','binding':binding_fixture()},{'approved':True}])
                self.assertEqual(safe_recheck_failure(caught.exception),
                    {'reason':'source_unavailable','origin':'launcher_ipc'})
                self.assertNotIn(secret,str(caught.exception))
                sent=[json.loads(line) for line in self.last_control.stdin.saved.splitlines()]
                self.assertFalse(any('result' in frame or 'model_result' in frame for frame in sent))
                self.assertEqual(sum(frame=={'op':'recheck'} for frame in sent),
                    1 if name=='read_frame' else 0)

    def test_second_attempt_same_generation_is_rejected(self):
        with self.assertRaises(ContractError):self.invoke([
            {'op':'fixed_cpu','binding':binding_fixture()},{'approved':True},
            {'op':'fixed_cpu','binding':binding_fixture()}])


class StartupQuarantineTests(unittest.TestCase):
    class EndWait(BaseException):
        pass

    def test_startup_validation_failures_idle_without_spawn_exit_or_retry(self):
        for stage in ('deployment', 'epoch'):
            with self.subTest(stage=stage):
                handlers = {}
                output = io.StringIO()
                with patch.object(launcher, 'deployment',
                        side_effect=PermissionError('private path') if stage == 'deployment' else None,
                        return_value={'container_id': 'a' * 64}) as deploy, \
                     patch.object(launcher, 'read_epoch', side_effect=ContractError('bad epoch')) as epoch, \
                     patch.object(launcher.os, 'getpid', return_value=1), \
                     patch.object(launcher.os, 'getresuid', return_value=(0, 0, 0)), \
                     patch.object(launcher.os, '_exit') as exit_process, \
                     patch.object(launcher.subprocess, 'Popen') as popen, \
                     patch.object(launcher.signal, 'signal', side_effect=lambda signum, handler: handlers.update({signum: handler})), \
                     patch.object(launcher.signal, 'pause', side_effect=[None, self.EndWait()]) as wait, \
                     patch.object(launcher.sys, 'stderr', output):
                    with self.assertRaises(self.EndWait):
                        launcher.supervise()
                    deploy.assert_called_once()
                    self.assertEqual(epoch.call_count, 0 if stage == 'deployment' else 1)
                    self.assertEqual(wait.call_count, 2)
                    popen.assert_not_called()
                    exit_process.assert_not_called()
                    records=[json.loads(line) for line in output.getvalue().splitlines()]
                    self.assertEqual(len(records),2)
                    self.assertEqual(records[0]['role'],'launcher')
                    self.assertEqual(records[0]['stage'],stage)
                    self.assertEqual(records[1]['phase'],'BLOCKED_STARTUP')
                    handlers[launcher.signal.SIGTERM](launcher.signal.SIGTERM, None)
                    exit_process.assert_called_once_with(125)

    def test_quarantine_requires_root_pid1(self):
        for pid, identity in ((2, (0, 0, 0)), (1, (10001,) * 3), (1, (0, 10001, 0))):
            with self.subTest(pid=pid, identity=identity), \
                 patch.object(launcher.os, 'getpid', return_value=pid), \
                 patch.object(launcher.os, 'getresuid', return_value=identity):
                with self.assertRaises(ContractError):
                    launcher.blocked_startup(wait=lambda: self.fail('must not wait'))

    def test_manager_spawn_failure_does_not_quarantine(self):
        with patch.object(launcher, 'deployment', return_value={'container_id': 'a' * 64}), \
             patch.object(launcher, 'read_epoch', return_value=EPOCH), \
             patch.object(launcher.subprocess, 'Popen', side_effect=PermissionError('spawn failed')), \
             patch.object(launcher, 'blocked_startup') as blocked:
            with self.assertRaises(PermissionError):
                launcher.supervise()
            blocked.assert_not_called()


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
                 patch('research_harness.manager_github.ManagerGitHub', return_value=object()), \
                 patch('research_harness.owner_approval.GitHubOwnerComments', return_value=object()), \
                 patch('research_harness.result_publisher.GhResultTransport',
                       return_value=SimpleNamespace(base_sha=lambda: 'c' * 40)), \
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
