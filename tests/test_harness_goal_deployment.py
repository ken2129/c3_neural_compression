"""Goal deployment fixtures only; no Docker, models or credential reads."""
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from research_harness.goal_deployment import validate_goal_deployment
from research_harness.schema import ContractError
from tests.test_harness_deployment_contract import (inspect_fixture, volume_fixture,
    CONTAINER_ID, SOURCE_SHA, VOLUME_NAME)
from tests.test_harness_goal_plan import goal_fixture, SHA

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('goal_' + name, ROOT / 'tools/research_goal' / (name+'.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


prepare = load('prepare')
launcher = load('launcher')


def profile_fixture():
    value = prepare.profile_template()
    value.update(enabled=True, deployment_id='1'*32, input_source_sha=SHA, runtime_source_sha=SOURCE_SHA,
                 model='configured-model-v1')
    value['risk_acknowledgements'] = {key: True for key in prepare.CONSENTS}
    return value


def goal_inspect():
    value = inspect_fixture(); profile = profile_fixture(); limits = profile['resource_limits']
    value['Image'] = 'sha256:' + '1'*64
    value['Config']['Labels'].update({'c3.purpose':'dedicated-goal-controller', 'c3.input-source-sha':SHA,'c3.deployment-id':'1'*32})
    value['Config']['Entrypoint'] = ['python','-I','-B','/opt/service/goal_launcher.py']
    value['HostConfig'].update(Memory=limits['memory_bytes'], MemorySwap=limits['memory_bytes'],
        PidsLimit=limits['max_processes'], NanoCpus=10**9, ShmSize=64*1024**2,
        Tmpfs={'/work':f"rw,noexec,nosuid,nodev,size={limits['max_work_bytes']},mode=0755,uid=0,gid=0"})
    return value


class GoalDeploymentTests(unittest.TestCase):
    def validate(self, data, **kwargs):
        args = dict(runtime_source_sha=SOURCE_SHA, input_source_sha=SHA, volume_name=VOLUME_NAME,
                    volume_inspect={**volume_fixture(),'Labels':{'c3.deployment-id':'1'*32}},
                    resource_limits=profile_fixture()['resource_limits'], deployment_id='1'*32)
        args.update(kwargs)
        return validate_goal_deployment(data, [CONTAINER_ID], **args)

    def test_distinct_image_input_source_and_root_owned_work(self):
        value = goal_inspect(); before = deepcopy(value)
        result = self.validate(value)
        self.assertEqual(value, before)
        self.assertEqual(result['runtime_source_sha'], SOURCE_SHA)
        self.assertEqual(result['input_source_sha'], SHA)
        self.assertEqual(result['image_id'], value['Image'])
        self.assertNotIn('execution_allowed', result)
        with self.assertRaises(ContractError): self.validate(inspect_fixture())

    def test_isolation_resource_and_profile_mismatches_fail_closed(self):
        changes = {'Privileged':True, 'ReadonlyRootfs':False, 'PidMode':'host',
                   'IpcMode':'none', 'CgroupnsMode':'host', 'CapAdd':['SYS_ADMIN'],
                   'NetworkMode':'host', 'Init':True, 'NanoCpus':2*10**9,
                   'Memory':512*1024**2, 'PidsLimit':32, 'ShmSize':128*1024**2,
                   'Devices':[{}], 'Binds':['/tmp:/work'],
                   'Tmpfs':{'/work':'rw,uid=10002'}}
        for key, replacement in changes.items():
            value = goal_inspect(); value['HostConfig'][key] = replacement
            with self.subTest(key=key), self.assertRaises(ContractError): self.validate(value)
        value = goal_inspect(); value['Mounts'].append(dict(value['Mounts'][0]))
        with self.assertRaises(ContractError): self.validate(value)

    def test_optional_absent_docker_empty_fields(self):
        value = goal_inspect()
        for name in ('Binds','Devices','DeviceRequests','DeviceCgroupRules'):
            del value['HostConfig'][name]
        self.validate(value)

    def test_template_disabled_and_all_risks_explicit(self):
        self.assertIs(prepare.profile_template()['enabled'], False)
        profile = profile_fixture(); plan = goal_fixture()
        prepare.validate_profile(profile, plan)
        for key in profile['risk_acknowledgements']:
            changed = deepcopy(profile); changed['risk_acknowledgements'][key] = False
            with self.assertRaises(ContractError): prepare.validate_profile(changed, plan)
        profile['enabled'] = False
        with self.assertRaises(ContractError): prepare.validate_profile(profile, plan)

    def test_aggregate_requested_budget_matches_real_allocation(self):
        profile = profile_fixture(); plan = goal_fixture()
        for key in ('memory_bytes','max_processes','max_work_bytes'):
            changed = deepcopy(plan); changed['resource_budget'][key] -= 1
            with self.assertRaises(ContractError): prepare.validate_profile(profile, changed)
        plan['max_seconds'] = 900
        plan['resource_budget']['cpu_seconds_per_process'] = 600
        prepare.validate_profile(profile, plan)

    def test_template_does_not_invoke_subprocess(self):
        with patch.object(prepare.subprocess, 'run') as run, patch('sys.stdout', new_callable=io.StringIO):
            prepare.main(['--template'])
        run.assert_not_called()

    def test_goal_frames_bound_large_plan_without_artifact_pipe(self):
        value = {'op':'prepare_goal', 'plan':{'goal':'x'*16000}}
        stream = io.StringIO(); launcher.write_frame(stream, value); stream.seek(0)
        self.assertEqual(launcher.read_frame(stream), value)
        for raw in ('', '{}', '[]\n', '{"x":1,"x":2}\n', '{"x":"'+'x'*33000+'"}\n'):
            with self.assertRaises(ContractError): launcher.read_frame(io.StringIO(raw))


if __name__ == '__main__': unittest.main()


class GoalQuarantineTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from research_harness.goal_runtime import GoalRunBinding, QuarantineEntry, UntrustedGoalSnapshot
        from research_harness.goal_artifacts import GoalEntry, GoalWorkspaceSnapshot
        from research_harness.schema import canonical
        import hashlib
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'quarantine'
        self.plan = goal_fixture(); self.plan['write_scope'] = ['out']; self.plan['inputs'] = []
        self.binding = dict(source_sha=SHA, plan_digest=hashlib.sha256(canonical(self.plan)).hexdigest(),
            request_id='request-1', run_id='goal-72-test', issue_number=72, issue_content_sha256='d'*64)
        bound = GoalRunBinding(SHA, self.binding['plan_digest'], 'request-1', 'goal-72-test')
        self.snapshot = UntrustedGoalSnapshot(bound, (QuarantineEntry('out','directory',0o700,b''),
            QuarantineEntry('out/result.md','file',0o644,b'candidate only\n')))
        self.baseline = GoalWorkspaceSnapshot(SHA, ('out',), (GoalEntry('out','directory',0o700,b''),),
                                               1024**2,1024**2,())

    def invoke(self, *, readback=None):
        from research_harness.schema import loads
        actual_path = Path
        def path(value):
            return self.root if str(value) == '/control/quarantine' else actual_path(value)
        reader = readback or (lambda p, limit: loads(actual_path(p).read_bytes()))
        with patch.object(launcher, 'as_uid'), patch.object(launcher, 'private'), \
             patch.object(launcher, 'Path', side_effect=path), \
             patch.object(launcher, 'protected_json', side_effect=reader):
            return launcher.freeze_snapshot(self.snapshot, self.binding, self.baseline, self.plan)

    def test_durable_protected_bytes_form_only_an_unapproved_candidate(self):
        result = self.invoke()
        self.assertTrue((self.root / 'goal-72-test.snapshot.json').is_file())
        self.assertFalse((self.root / 'goal-72-test.copying.json').exists())
        self.assertEqual(result.changes[0].after.content, b'candidate only\n')
        self.assertFalse(result.publish_authorized)
        self.assertEqual(result.source_sha, SHA)
        with self.assertRaises(ContractError): self.invoke()

    def test_changed_durable_readback_is_not_resultized(self):
        with self.assertRaises(ContractError): self.invoke(readback=lambda *_: {})

    def test_duplicate_paths_and_requested_output_budget_rejected_before_write(self):
        from dataclasses import replace
        self.snapshot = replace(self.snapshot, entries=self.snapshot.entries + (self.snapshot.entries[-1],))
        with self.assertRaises(ContractError): self.invoke()
        self.assertFalse(self.root.exists())

    def test_no_new_authority_from_bound_snapshot_or_changed_source(self):
        from dataclasses import replace
        self.snapshot = replace(self.snapshot, binding=replace(self.snapshot.binding, source_sha='e'*40))
        with self.assertRaises(ContractError): self.invoke()
        self.assertFalse(self.root.exists())


class GoalPipeFailureTests(unittest.TestCase):
    def test_known_root_failure_returns_to_manager_for_durable_block(self):
        from research_harness.schema import canonical
        from research_harness.restart_boundary import RestartEpoch
        epoch = RestartEpoch('a'*64, '11111111-1111-1111-1111-111111111111', 22, 33)
        incoming = io.StringIO(canonical({'goal_failure':{'reason':'GOAL_MODEL_DEADLINE','stage':'turn'}}).decode()+'\n')
        outgoing = io.StringIO()
        runtime = launcher.GoalPipeRuntime(incoming, outgoing, epoch, profile_fixture())
        binding = dict(source_sha=SHA, plan_digest='b'*64, request_id='req-1', run_id='run-1',
                       issue_number=72, issue_content_sha256='d'*64)
        with self.assertRaisesRegex(ContractError, '^goal runtime rejected the claimed attempt$'):
            runtime.run(binding, lambda:None)
        self.assertEqual(runtime.failed_issue, 72)
        self.assertNotIn('GOAL_MODEL_DEADLINE', outgoing.getvalue())

    def test_failure_frame_does_not_echo_untrusted_text(self):
        from research_harness.schema import canonical
        incoming = io.StringIO(canonical({'goal_failure':{'reason':'PRIVATE SECRET','stage':'PRIVATE SECRET'}}).decode()+'\n')
        runtime = launcher.GoalPipeRuntime(incoming, io.StringIO(), None, profile_fixture())
        binding = dict(source_sha=SHA, plan_digest='b'*64, request_id='req-1', run_id='run-1',
                       issue_number=72, issue_content_sha256='d'*64)
        with self.assertRaises(ContractError) as caught: runtime.run(binding, lambda:None)
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_tar_copy_uses_numeric_manager_ownership_without_archive_override(self):
        import tarfile
        calls = []
        payload = None
        def fake_run(argv, data=None, env=None):
            nonlocal payload
            calls.append(argv)
            if data is not None:
                payload = data; return b''
            return payload
        with patch.object(prepare, 'run', side_effect=fake_run):
            prepare.copy_records('c'*64, {'goal-runtime.json':b'{}'})
        self.assertEqual(calls[0][:3], ['docker','cp','-'])
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:') as archive:
            info = archive.getmembers()[0]
            self.assertEqual((info.uid,info.gid,info.mode,info.uname,info.gname), (10001,10001,0o600,'',''))


class GoalFailureAckTests(unittest.TestCase):
    def test_partial_ack_is_bounded_without_touching_watchdog(self):
        from types import SimpleNamespace
        pipe = SimpleNamespace(fileno=lambda:99)
        with patch.object(launcher.select, 'select', side_effect=[([pipe],[],[]),([],[],[])]), \
             patch.object(launcher.os, 'read', return_value=b'{'), \
             patch.object(launcher.signal, 'alarm') as alarm:
            with self.assertRaisesRegex(ContractError, 'timed out'):
                launcher.read_failure_ack(pipe)
            alarm.assert_not_called()

    def test_complete_ack_is_closed_json(self):
        from types import SimpleNamespace
        pipe = SimpleNamespace(fileno=lambda:99)
        raw = b'{"op":"failure_ack","issue":72,"phase":"BLOCKED"}\n'
        with patch.object(launcher.select, 'select', return_value=([pipe],[],[])), \
             patch.object(launcher.os, 'read', side_effect=[bytes([b]) for b in raw]):
            self.assertEqual(launcher.read_failure_ack(pipe), {'op':'failure_ack','issue':72,'phase':'BLOCKED'})


class GoalLauncherFailureIntegrationTests(unittest.TestCase):
    def invoke(self, mode):
        import hashlib
        from contextlib import ExitStack
        from types import SimpleNamespace
        from research_harness.goal_artifacts import GoalChangeCandidate, GoalChange, GoalEntry
        from research_harness.schema import canonical
        from research_harness.restart_boundary import RestartEpoch
        class Pipe(io.StringIO):
            def close(self): self.saved = self.getvalue(); super().close()
        plan = goal_fixture(); profile = prepare.installed_profile(profile_fixture(), plan)
        binding = dict(source_sha=SHA, plan_digest=hashlib.sha256(canonical(plan)).hexdigest(),
                       request_id='req-1', run_id='run-1', issue_number=72, issue_content_sha256='d'*64)
        frames = [{'op':'prepare_goal','plan':plan},{'op':'preflight'},{'op':'goal','binding':binding}]
        control = SimpleNamespace(pid=22, stdin=Pipe(), stdout=io.StringIO(''.join(canonical(v).decode()+'\n' for v in frames)))
        candidate = GoalChangeCandidate(SHA, ('artifacts',), 'e'*64, 'f'*64,
                    (GoalChange('artifacts/result.md', None, GoalEntry('artifacts/result.md','file',0o644,b'output')),))
        backend = SimpleNamespace(admit_goal=lambda _:None, resources=lambda:dict(memory=1024**3,pids=64,work_bytes=256*1024**2))
        def run_goal(request, recheck, **kwargs):
            if mode in ('runtime','cleanup'): raise ContractError('PRIVATE transport body')
            kwargs['quarantine'](object())
            return SimpleNamespace(candidate=SimpleNamespace(outcome='completed',summary='private'),elapsed_seconds=1)
        supervisor = SimpleNamespace(reserve=lambda:object(),run_goal=run_goal)
        epoch = RestartEpoch('a'*64,'11111111-1111-1111-1111-111111111111',22,33)
        with ExitStack() as stack:
            def patcher(*args, **kwargs): return stack.enter_context(patch.object(*args, **kwargs))
            patcher(launcher,'deployment',return_value=({'container_id':'a'*64},profile,False))
            patcher(launcher,'read_epoch',return_value=epoch)
            patcher(launcher.subprocess,'Popen',return_value=control)
            patcher(launcher,'stage_workspace',return_value=object())
            patcher(launcher,'GoalLinuxUIDBackend',return_value=backend)
            patcher(launcher,'GoalUIDSupervisor',return_value=supervisor)
            patcher(launcher,'freeze_snapshot',return_value=candidate)
            patcher(launcher,'as_uid')
            patcher(launcher.time,'sleep')
            patcher(launcher.sys,'stderr',io.StringIO())
            diagnostic = {'reason':'GOAL_RUNTIME_FAILURE','stage':'cleanup' if mode=='cleanup' else 'transport'}
            patcher(launcher,'safe_goal_runtime_diagnostic',return_value=diagnostic)
            patcher(launcher,'goal_failure_can_acknowledge',return_value=mode!='cleanup')
            ack = patcher(launcher,'read_failure_ack',return_value={'op':'failure_ack','issue':72,'phase':'BLOCKED'})
            if mode == 'persist': patcher(launcher,'exclusive_record',side_effect=OSError('PRIVATE disk path'))
            if mode == 'cleanup':
                with self.assertRaises(ContractError): launcher.supervise()
                ack.assert_not_called()
            else:
                launcher.supervise(); ack.assert_called_once()
        return [__import__('json').loads(line) for line in control.stdin.saved.splitlines()]

    def test_normal_transport_failure_is_acknowledged_after_block(self):
        frames = self.invoke('runtime')
        self.assertEqual(frames[-1], {'goal_failure':{'reason':'GOAL_RUNTIME_FAILURE','stage':'transport'}})

    def test_postrun_persistence_failure_is_acknowledged_after_block(self):
        frames = self.invoke('persist')
        self.assertEqual(frames[-1], {'goal_failure':{'reason':'GOAL_FINALIZATION_FAILED','stage':'persist'}})

    def test_uncertain_cleanup_never_waits_for_normal_failure_ack(self):
        frames = self.invoke('cleanup')
        self.assertFalse(any('goal_failure' in frame for frame in frames))


class GoalFreshDeploymentIdentityTests(unittest.TestCase):
    def test_template_creates_new_public_id_without_changing_consent_defaults(self):
        import re
        first, second = prepare.profile_template(), prepare.profile_template()
        self.assertIsNotNone(re.fullmatch('[0-9a-f]{32}', first['deployment_id']))
        self.assertNotEqual(first['deployment_id'], second['deployment_id'])
        self.assertIs(first['enabled'], False)
        self.assertTrue(all(value is False for value in first['risk_acknowledgements'].values()))

    def test_profile_plan_and_both_deployment_labels_must_match(self):
        profile = profile_fixture(); plan = goal_fixture()
        profile['deployment_id'] = '2'*32
        with self.assertRaises(ContractError): prepare.validate_profile(profile, plan)
        for where in ('container', 'volume'):
            data = goal_inspect()
            volume = {**volume_fixture(), 'Labels':{'c3.deployment-id':'1'*32}}
            if where == 'container': data['Config']['Labels']['c3.deployment-id'] = '2'*32
            else: volume['Labels']['c3.deployment-id'] = '2'*32
            with self.subTest(where=where), self.assertRaises(ContractError):
                validate_goal_deployment(data, [CONTAINER_ID], runtime_source_sha=SOURCE_SHA,
                    input_source_sha=SHA, volume_name=VOLUME_NAME, volume_inspect=volume,
                    resource_limits=profile['resource_limits'], deployment_id='1'*32)

    def test_existing_stopped_container_volume_or_image_rejects_reused_identity(self):
        for index in range(3):
            answers = [b'']*index + [b'existing\n']
            with patch.object(prepare, 'run', side_effect=answers) as run:
                with self.assertRaises(ContractError): prepare.require_fresh_deployment_id('1'*32)
                self.assertEqual(run.call_count, index+1)
                for args in run.call_args_list:
                    self.assertEqual(args.args[0][-1], 'label=c3.deployment-id='+'1'*32)
        with patch.object(prepare, 'run', return_value=b'') as run:
            prepare.require_fresh_deployment_id('2'*32)
            self.assertEqual(run.call_count, 3)


class InstalledGoalPlanTests(unittest.TestCase):
    def test_preparation_computes_digest_and_rejects_caller_digest(self):
        import hashlib
        from research_harness.schema import canonical
        profile = profile_fixture(); plan = goal_fixture()
        installed = prepare.installed_profile(profile, plan)
        self.assertEqual(installed['installed_plan_digest'], hashlib.sha256(canonical(plan)).hexdigest())
        self.assertNotIn('installed_plan_digest', profile)
        self.assertNotIn('installed_plan_digest', prepare.profile_template())
        with self.assertRaises(ContractError): prepare.validate_profile(installed, plan)
        launcher.require_installed_plan(installed, plan)

    def test_other_task_or_scope_rejected_before_pipe_or_staging(self):
        plan = goal_fixture(); profile = prepare.installed_profile(profile_fixture(), plan)
        for key, value in [('goal', 'Different research purpose'), ('write_scope', ['other-output'])]:
            changed = deepcopy(plan); changed[key] = value
            outgoing = io.StringIO()
            runtime = launcher.GoalPipeRuntime(io.StringIO(), outgoing, None, profile)
            with self.assertRaises(ContractError): runtime.prepare_plan(changed)
            self.assertEqual(outgoing.getvalue(), '')
        for digest in (None, 'f'*64, 'A'*64):
            changed = dict(profile, installed_plan_digest=digest)
            with self.assertRaises(ContractError): launcher.require_installed_plan(changed, plan)


class InstallerDiagnosticTests(unittest.TestCase):
    def test_failure_metadata_never_contains_exception_or_arguments(self):
        import json
        import subprocess
        cases = [
            (subprocess.CalledProcessError(23, ['PRIVATE_ARG'], output=b'PRIVATE_OUT', stderr=b'PRIVATE_ERR'), 'COMMAND_FAILED', 23),
            (ContractError('PRIVATE_CONTRACT'), 'CONTRACT_REJECTED', None),
            (FileNotFoundError('PRIVATE_PATH'), 'FILE_OR_COMMAND_MISSING', None),
            (PermissionError('PRIVATE_PATH'), 'PERMISSION_DENIED', None),
            (OSError('PRIVATE_PATH'), 'IO_FAILED', None),
            (ValueError('PRIVATE_VALUE'), 'DATA_INVALID', None),
            (RuntimeError('PRIVATE_UNKNOWN'), 'INSTALLER_FAILED', None),
        ]
        for stage in prepare.INSTALL_STAGES:
            for exc, reason, code in cases:
                def failing(_):
                    return prepare.at_stage(stage, lambda: (_ for _ in ()).throw(exc))
                with patch.object(prepare, 'main', failing), patch('sys.stderr', new_callable=io.StringIO) as output:
                    self.assertEqual(prepare.cli([]), 1)
                value = json.loads(output.getvalue())
                self.assertEqual(value, dict(phase='GOAL_INSTALL_BLOCKED', stage=stage,
                    reason=reason, command_exit_code=code, installer_exit_code=1))
                self.assertNotIn('PRIVATE', output.getvalue())

    def test_invalid_cli_does_not_echo_unknown_arguments(self):
        with patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertEqual(prepare.cli(['--PRIVATE_SECRET']), 1)
        self.assertNotIn('PRIVATE_SECRET', output.getvalue())
        self.assertIn('CONTRACT_REJECTED', output.getvalue())

    def test_nested_git_and_readback_stages_preserved(self):
        import subprocess
        with patch.object(prepare, 'run', side_effect=subprocess.CalledProcessError(7, ['secret'])):
            with self.assertRaises(prepare.InstallFailure) as caught:
                prepare.at_stage('context', prepare._git, 'status')
        self.assertEqual(caught.exception.stage, 'git')
        with patch.object(prepare, 'run', side_effect=[b'', b'not a tar PRIVATE']):
            with self.assertRaises(prepare.InstallFailure) as caught:
                prepare.copy_records('a'*64, {'record': b'private'})
        self.assertEqual(caught.exception.stage, 'readback')
        self.assertEqual(caught.exception.reason, 'DATA_INVALID')
