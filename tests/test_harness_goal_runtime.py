"""Offline goal lifecycle/FD-copy tests; no model, UID change or live container."""
from copy import deepcopy
from contextlib import redirect_stderr
import ast
import inspect
import io
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import research_harness.goal_runtime as runtime
from research_harness.goal_runner import GoalRequest, GoalCandidate
from research_harness.schema import ContractError
from tests.test_harness_goal_runner import plan
from tests.test_harness_uid_supervisor import FakeOS, worker


def request(): return GoalRequest(plan(),'a'*40)
def binding(req): return runtime.GoalRunBinding(req.source_sha,req.digest,'request-1','run-1')


class GoalOS(FakeOS):
    def admit_goal(self,request): self.events.append(('goal_admission',))
    def launch_goal(self,request):
        self.members[10]=worker()
        self.events.append(('goal_spawn',))
        return SimpleNamespace(process=SimpleNamespace(pid=10,poll=lambda:None))
    def collect_goal(self,request,binding,check):
        check();self.events.append(('quarantine_copy',))
        return runtime.UntrustedGoalSnapshot(binding,())


class GoalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.os=GoalOS();self.supervisor=runtime.GoalUIDSupervisor(self.os)
        self.request=request();self.binding=binding(self.request)
        self.runner=Mock();self.runner.run.return_value=GoalCandidate('completed','untrusted','t','u')
        self.transport=Mock();self.writer=Mock(return_value=None)

    def run_goal(self,*,recheck=None,**kwargs):
        options=dict(binding=self.binding,quarantine=self.writer,enabled=True)
        options.update(kwargs)
        with patch.object(runtime,'GoalAppServerRunner',return_value=self.runner), \
             patch.object(runtime,'BoundedStdioTransport',return_value=self.transport) as factory:
            self.factory=factory
            return self.supervisor.run_goal(self.request,recheck or (lambda:None),**options)

    def test_disabled_and_missing_goal_admission_never_spawn(self):
        with self.assertRaisesRegex(ContractError,'DISABLED'):self.run_goal(enabled=False)
        self.assertFalse(self.os.events)
        self.os.admit_goal=Mock(return_value=True)
        with self.assertRaisesRegex(ContractError,'NOT_A_FLAG'):self.run_goal()
        self.assertNotIn(('goal_spawn',),self.os.events)

    def test_actual_limits_must_fit_request_not_only_operator_maximum(self):
        for field,request_field in (('memory','memory_bytes'),('pids','max_processes'),('work_bytes','max_work_bytes')):
            with self.subTest(field=field):
                self.setUp();self.os.limits[field]=self.request.plan['resource_budget'][request_field]+1
                with self.assertRaisesRegex(ContractError,'ACTUAL_RESOURCES'):self.run_goal()
                self.assertNotIn(('goal_spawn',),self.os.events)

    def test_same_cleanup_and_watchdog_with_untrusted_quarantine(self):
        result=self.run_goal()
        self.assertIn(('arm',124),self.os.events)
        self.assertEqual(self.factory.call_args.kwargs['deadline'],100)
        self.assertEqual(self.factory.call_args.kwargs['max_capture_bytes'],1024*1024)
        self.assertTrue(any(event[0]=='signal' for event in self.os.events))
        self.assertFalse(any(event[0]=='disarm' for event in self.os.events))
        self.assertNotIn(10,self.os.members)
        self.writer.assert_called_once()
        self.assertEqual(result.quarantine_digest,self.writer.call_args.args[0].digest)
        self.assertTrue(result.requires_namespace_restart)
        self.assertFalse(result.cleanup_verified)
        self.assertFalse(result.publish_authorized)
        self.assertFalse(result.candidate.independently_validated)
        self.transport.close.assert_called_once()
        with self.assertRaises(ContractError):self.run_goal()

    def test_approved_wall_is_not_fixed_canary_duration(self):
        self.request.plan['max_seconds']=240;self.binding=binding(self.request)
        self.run_goal()
        self.assertIn(('arm',244),self.os.events)
        self.assertEqual(self.factory.call_args.kwargs['deadline'],220)

    def test_late_model_and_quarantine_failure_cleanup_without_disarming(self):
        for kind in ('late','writer','runner'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind=='late':
                    def late(*a,**kw):
                        self.os.t=100
                        return GoalCandidate('completed','candidate','t','u')
                    self.runner.run.side_effect=late
                elif kind=='writer':self.writer.side_effect=RuntimeError('private failure')
                else:self.runner.run.side_effect=ContractError('failed turn')
                with self.assertRaises(Exception):self.run_goal()
                self.assertNotIn(10,self.os.members)
                self.assertFalse(any(e[0]=='disarm' for e in self.os.events))
                if kind!='writer':self.writer.assert_not_called()

    def test_request_mutation_and_insufficient_reserve_reject(self):
        self.request.plan['max_seconds']=20;self.binding=binding(self.request)
        with self.assertRaisesRegex(ContractError,'RESERVE'):self.run_goal()
        self.assertFalse(self.os.events)
        self.setUp()
        def mutate(identity):self.request.plan['goal']='Changed after admission'
        with self.assertRaisesRegex(ContractError,'PLAN_CHANGED'):self.run_goal(before_release=mutate)
        self.assertNotIn(('release',),self.os.events)

    def test_recheck_failures_during_turn_copy_and_persist_never_return(self):
        for phase in ('turn','quarantine','persist'):
            with self.subTest(phase=phase):
                self.setUp()
                def recheck():
                    if runtime._CONTEXT.get()['stage']==phase:raise ContractError('cancelled')
                def run(*args,**kwargs):
                    kwargs['recheck']()
                    return GoalCandidate('completed','candidate','t','u')
                self.runner.run.side_effect=run
                if phase=='persist':self.writer.side_effect=lambda snapshot:recheck()
                with self.assertRaisesRegex(ContractError,'GOAL_CONTRACT_FAILURE'):self.run_goal(recheck=recheck)
                self.assertNotIn(10,self.os.members)
                self.assertFalse(any(e[0]=='disarm' for e in self.os.events))
                if phase!='persist':self.writer.assert_not_called()

    def test_transport_limit_and_slow_persistence_never_return_candidate(self):
        self.runner.run.side_effect=runtime.GoalBlocked('GOAL_EVENT_LIMIT')
        with self.assertRaises(ContractError):self.run_goal()
        self.writer.assert_not_called();self.assertNotIn(10,self.os.members)
        self.setUp()
        from research_harness.app_server_adapter import AppServerBlocked
        self.runner.run.side_effect=AppServerBlocked('combined app-server output exceeds its limit')
        with self.assertRaises(ContractError):self.run_goal()
        self.writer.assert_not_called();self.assertNotIn(10,self.os.members)
        self.setUp()
        self.writer.side_effect=lambda snapshot:setattr(self.os,'t',120)
        with self.assertRaisesRegex(ContractError,'FINALIZATION_DEADLINE'):self.run_goal()
        self.assertNotIn(10,self.os.members)
        self.assertFalse(any(e[0]=='disarm' for e in self.os.events))

    def test_ordinary_failures_export_handled_evidence_only_after_cleanup(self):
        from research_harness.app_server_adapter import AppServerBlocked
        cases=((AppServerBlocked('combined app-server output exceeds its limit'),'GOAL_TRANSPORT_OUTPUT_LIMIT'),
               (ContractError('private contract body'),'GOAL_CONTRACT_FAILURE'),
               (OSError('private path'),'GOAL_OS_FAILURE'),
               (TimeoutError('private timeout'),'GOAL_TIMEOUT'),
               (RuntimeError('private implementation details'),'GOAL_RUNTIME_FAILURE'))
        for exc,reason in cases:
            with self.subTest(reason=reason):
                self.setUp();self.runner.run.side_effect=exc
                with self.assertRaises(runtime.GoalRuntimeBlocked) as caught:self.run_goal()
                handled=caught.exception
                self.assertTrue(runtime.goal_failure_can_acknowledge(handled))
                self.assertEqual(runtime.safe_goal_runtime_diagnostic(handled),dict(reason=reason,stage='turn'))
                self.assertNotIn('private',str(handled))
                self.assertNotIn(10,self.os.members)
                self.assertFalse(runtime.goal_failure_can_acknowledge(exc))
                handled.args=('private mutated body',)
                handled.cleanup_verified=True
                self.assertEqual(runtime.safe_goal_runtime_diagnostic(handled),dict(reason=reason,stage='turn'))

    def test_uncertain_cleanup_process_control_and_close_failure_cannot_acknowledge(self):
        for case in ('cleanup','interrupt','close'):
            with self.subTest(case=case):
                self.setUp()
                self.runner.run.side_effect=(KeyboardInterrupt('private') if case=='interrupt'
                                             else OSError('private'))
                if case=='cleanup':self.supervisor.stop_owned=Mock(side_effect=ContractError('uncertain cleanup'))
                if case=='close':self.transport.close.side_effect=ContractError('close failed')
                with self.assertRaises(BaseException) as caught:self.run_goal()
                self.assertFalse(runtime.goal_failure_can_acknowledge(caught.exception))
        forged=runtime.GoalRuntimeBlocked('GOAL_MODEL_DEADLINE');forged.cleanup_verified=True
        self.assertFalse(runtime.goal_failure_can_acknowledge(forged))


class GoalSpawnTests(unittest.TestCase):
    def test_bootstrap_is_clean_dropped_bounded_and_does_not_choose_commands(self):
        req=request()
        with patch.object(runtime.os,'getpid',return_value=1), \
             patch.object(runtime.os,'getresuid',return_value=(0,0,0)), \
             patch.object(runtime.os,'fstat',return_value=SimpleNamespace(st_mode=stat.S_IFIFO)), \
             patch.object(runtime.subprocess,'Popen') as popen:
            runtime.spawn_goal_child(req,gate_read=8,ready_write=9)
        args=popen.call_args.args[0];kw=popen.call_args.kwargs
        self.assertEqual(kw['env'],{})
        self.assertEqual((kw['user'],kw['group'],kw['extra_groups']),(10002,10002,[]))
        self.assertEqual(kw['cwd'],'/')
        self.assertEqual(kw['pass_fds'],(8,9))
        self.assertEqual(args[5:10],['8','9','120','64',str(256*1024*1024)])
        self.assertEqual(args[10:15],['/opt/codex/bin/codex','app-server','--listen','stdio://','--strict-config'])
        self.assertIn('RLIMIT_CPU,cpu',args[4])
        self.assertIn('RLIMIT_FSIZE,work',args[4])
        self.assertNotIn('RLIMIT_AS',args[4])
        self.assertNotIn('sum(i*i',args[4])
        self.assertNotIn('/control',args[4])
        self.assertIn("'PATH':'/usr/local/bin:/usr/bin:/bin'",args[4])
        self.assertEqual(args[-2:],['-c',
            'shell_environment_policy.set={PATH="/usr/local/bin:/usr/bin:/bin",LANG="C.UTF-8"}'])


class GoalResourceTests(unittest.TestCase):
    def readings(self):
        return {'/proc/self/cgroup':'0::/\n','/sys/fs/cgroup/cpu.max':'100000 100000',
            '/sys/fs/cgroup/memory.max':str(1024**3),'/sys/fs/cgroup/memory.swap.max':'0',
            '/sys/fs/cgroup/pids.max':'64','/proc/self/mountinfo':
            '1 0 0:1 / / ro,relatime - overlay overlay rw\n'
            '2 1 0:2 / /work rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw\n'
            '3 1 0:3 / /dev/shm rw,nosuid,nodev,noexec,relatime - tmpfs shm rw\n'
            '4 1 0:4 / /sys/fs/cgroup ro,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw\n'}

    def read(self,values,*,work_mode=0o755,shm_size=64*1024**2):
        with patch.object(Path,'read_text',lambda path:values[str(path)]), \
             patch.object(Path,'lstat',return_value=SimpleNamespace(st_mode=stat.S_IFDIR|work_mode,st_uid=0)), \
             patch.object(Path,'exists',return_value=False), \
             patch.object(Path,'glob',return_value=[]), \
             patch.object(runtime.os,'statvfs',side_effect=lambda path:SimpleNamespace(
                 f_blocks=(256*1024**2 if path=='/work' else shm_size)//4096,f_frsize=4096)):
            return runtime.GoalLinuxUIDBackend.resources()

    def test_reads_actual_cgroup_and_goal_mounts(self):
        self.assertEqual(self.read(self.readings()),dict(memory=1024**3,swap=0,pids=64,
            cpu_quota=100000,cpu_period=100000,work_bytes=256*1024**2))

    def test_unbounded_resources_wrong_mounts_and_legacy_owner_mode_block(self):
        values=self.readings()
        for path,bad in (('/proc/self/cgroup','0::/other'),
                         ('/sys/fs/cgroup/cpu.max','max 100000'),
                         ('/sys/fs/cgroup/memory.max','max'),
                         ('/proc/self/mountinfo',values['/proc/self/mountinfo'].replace('/ / ro,','/ / rw,')),
                         ('/proc/self/mountinfo',values['/proc/self/mountinfo'].replace('/ /work rw,nosuid,nodev,noexec,','/ /work rw,')),
                         ('/proc/self/mountinfo',values['/proc/self/mountinfo']+'5 2 0:5 / /work/goal rw - tmpfs tmpfs rw\n')):
            with self.subTest(path=path),self.assertRaises(ContractError):self.read(dict(values,**{path:bad}))
        with self.assertRaises(ContractError):self.read(values,work_mode=0o700)
        with self.assertRaises(ContractError):self.read(values,shm_size=128*1024**2)


class GoalDiagnosticTests(unittest.TestCase):
    def test_literal_reason_coverage_and_forged_values_cannot_escape(self):
        tree=ast.parse(inspect.getsource(runtime))
        reasons={n.exc.args[0].value for n in ast.walk(tree)
            if isinstance(n,ast.Raise) and isinstance(n.exc,ast.Call)
            and getattr(n.exc.func,'id',None)=='GoalRuntimeBlocked'}
        self.assertEqual(reasons,runtime._RUNTIME_REASONS)
        exc=runtime.GoalRuntimeBlocked('GOAL_CHILD_IDENTITY_INVALID')
        exc.args=('sk-secret',);exc.reason='sk-secret';exc.stage='sk-secret'
        detail=runtime.safe_goal_runtime_diagnostic(exc)
        self.assertEqual(detail,dict(reason='GOAL_CHILD_IDENTITY_INVALID',stage='admission'))
        detail['reason']='sk-secret'
        self.assertNotIn('sk-secret',json.dumps(runtime.safe_goal_runtime_diagnostic(exc)))
        class Forged(runtime.GoalRuntimeBlocked):pass
        self.assertIsNone(runtime.safe_goal_runtime_diagnostic(Forged('GOAL_CHILD_IDENTITY_INVALID')))
        self.assertIsNone(runtime.safe_goal_runtime_diagnostic(RuntimeError('sk-secret')))

    def test_runner_failure_keeps_stage_and_safe_code_instead_of_body(self):
        os_backend=GoalOS();supervisor=runtime.GoalUIDSupervisor(os_backend)
        req=request();transport=Mock();runner=Mock()
        runner.run.side_effect=runtime.GoalBlocked('GOAL_SERVER_ERROR')
        runner.run.side_effect.args=('sk-secret-remote-body',)
        log=io.StringIO()
        with patch.object(runtime,'GoalAppServerRunner',return_value=runner), \
             patch.object(runtime,'BoundedStdioTransport',return_value=transport), \
             redirect_stderr(log),self.assertRaises(runtime.GoalBlocked) as caught:
            supervisor.run_goal(req,lambda:None,binding=binding(req),quarantine=lambda s:None,enabled=True)
        record=json.loads(log.getvalue())
        self.assertEqual(record,dict(phase='GOAL_RUNTIME_BLOCKED',reason='GOAL_SERVER_ERROR',stage='turn'))
        self.assertEqual(runtime.safe_goal_runtime_diagnostic(caught.exception),dict(reason='GOAL_SERVER_ERROR',stage='turn'))
        self.assertNotIn('sk-secret',log.getvalue())


class GoalAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for path,mode in (('work',0o755),('work/goal',0o755),('work/goal/data',0o755),
                          ('work/goal/output',0o700),('work/worker-home',0o700),('work/codex-home',0o700)):
            (self.root/path).mkdir(mode=mode,parents=True,exist_ok=True)
        (self.root/'work/goal/data/input.csv').write_bytes(b'public input\n')
        (self.root/'work/codex-home/auth.json').write_bytes(b'dummy-not-auth')
        (self.root/'work/codex-home/auth.json').chmod(0o600)
        (self.root/'etc').mkdir();(self.root/'etc/hostname').write_text('c'*12)
        self.request=request();p=self.request.plan
        self.config=dict(schema='c3-goal-runtime-v1',enabled=True,input_source_sha='a'*40,
            runtime_source_sha='b'*40,model=p['model'],
            resource_limits=dict(max_seconds=p['max_seconds'],**p['resource_budget']),
            risk_acknowledgements=dict.fromkeys(runtime.CONSENTS,True))
        raw=(self.root/'work/goal/data/input.csv').read_bytes()
        self.manifest=dict(source_sha='a'*40,files={'data/input.csv':
            dict(sha256=hashlib.sha256(raw).hexdigest(),size_bytes=len(raw))})

    def admit(self):
        real_path=Path;real_listdir=os.listdir;real_lstat=Path.lstat
        def mapped(*values):
            path=real_path(*values)
            if str(path).startswith(('/work','/etc')):return self.root/str(path)[1:]
            return path
        def protected(path,**kw):
            mode=0o755 if kw.get('directory') else 0o644
            if str(path)=='/control':mode=0o700
            if str(path)==runtime.CONFIG_PATH:mode=0o600
            if str(path)=='/opt/codex/bin/codex':mode=0o755
            return SimpleNamespace(st_mode=mode|(stat.S_IFDIR if kw.get('directory') else stat.S_IFREG))
        def read(path,**kw):
            if str(path)==runtime.CONFIG_PATH:return json.dumps(self.config).encode()
            if str(path)=='/control/goal-deployment.json':return json.dumps(dict(inspect={},consumers=[],
                volume_name='new-goal',volume_inspect={},initialize_new_state=False)).encode()
            if str(path)==runtime.INPUT_MANIFEST:return json.dumps(self.manifest).encode()
            if str(path)=='/opt/service/source-sha':return b'b'*40+b'\n'
            return real_path(path).read_bytes()
        def metadata(path,*args,**kwargs):
            info=real_lstat(path,*args,**kwargs)
            return SimpleNamespace(st_mode=info.st_mode,st_nlink=info.st_nlink,st_size=info.st_size,
                st_uid=10002 if any(part in str(path) for part in ('/output','/worker-home','/codex-home')) else 0)
        with patch.object(runtime,'Path',side_effect=mapped), \
             patch.object(runtime,'_protected',side_effect=protected), \
             patch.object(runtime,'_read_protected',side_effect=read), \
             patch.object(Path,'lstat',metadata), \
             patch.object(runtime.os,'seteuid') as uid, \
             patch.object(runtime.os,'readlink',return_value='ipc:[123]'), \
             patch.object(runtime.os,'listdir',side_effect=lambda path:real_listdir(mapped(path))), \
             patch.object(runtime.os.path,'lexists',return_value=False), \
             patch('research_harness.goal_deployment.validate_goal_deployment',return_value={'container_id':'c'*64}) as deployment:
            backend=runtime.GoalLinuxUIDBackend();backend.admit_goal(self.request)
            self.assertEqual(backend._ipc_namespace,'ipc:[123]')
            self.assertEqual(uid.call_args.args,(0,))
            self.assertEqual(deployment.call_args.kwargs['runtime_source_sha'],'b'*40)
            self.assertEqual(deployment.call_args.kwargs['input_source_sha'],'a'*40)

    def test_distinct_input_runtime_revisions_and_fresh_layout_admit(self):self.admit()

    def test_scope_permissions_hash_extra_state_and_risk_consent_reject(self):
        for case in ('scope','input','extra','risk','disabled','runtimehome'):
            with self.subTest(case=case):
                self.setUp()
                if case=='scope':(self.root/'work/goal/output').chmod(0o777)
                elif case=='input':self.manifest['files']['data/input.csv']['sha256']='0'*64
                elif case=='extra':(self.root/'work/goal/unapproved').write_bytes(b'x')
                elif case=='risk':self.config['risk_acknowledgements']['arbitrary_cpu_commands']=False
                elif case=='disabled':self.config['enabled']=False
                else:(self.root/'work/codex-home/config.toml').write_bytes(b'x')
                with self.assertRaises(ContractError):self.admit()

    def test_protected_file_helper_accepts_only_explicit_allowed_owners(self):
        path=self.root/'protected';path.write_bytes(b'public');path.chmod(0o644)
        info=path.lstat()
        self.assertEqual(runtime._protected(path,owner=(info.st_uid,123456)).st_uid,info.st_uid)
        with self.assertRaises(ContractError):runtime._protected(path,owner=(123456,123457))
        path.chmod(0o666)
        with self.assertRaises(ContractError):runtime._protected(path,owner=info.st_uid)
        path.chmod(0o644)
        link=self.root/'link';link.symlink_to(path)
        with self.assertRaises(ContractError):runtime._protected(link,owner=info.st_uid)
        parent_link=self.root/'parent-link';parent_link.symlink_to(self.root,target_is_directory=True)
        with patch.object(runtime.os,'open') as opened,self.assertRaises(ContractError):
            runtime._read_protected(parent_link/'protected',limit=100,owner=info.st_uid)
        opened.assert_not_called()


class GoalQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.goal=self.root/'work/goal'
        (self.goal/'output').mkdir(parents=True)
        (self.root/'work/codex-home').mkdir()
        (self.root/'work/codex-home/auth.json').write_text('not an artifact')
        self.request=request();self.binding=binding(self.request)

    def collect(self,check=lambda:None):
        real_open=os.open
        def mapped(path,*args,**kw):
            return real_open(self.root if path=='/' else path,*args,**kw)
        with patch.object(runtime.os,'open',side_effect=mapped), \
             patch.object(runtime,'WORKER_UID',os.getuid()):
            return runtime.collect_untrusted_goal_bytes(self.request,self.binding,check=check)

    def test_only_scope_bytes_are_copied_and_digest_binds_run_and_contents(self):
        (self.goal/'output/result.txt').write_text('untrusted output')
        snapshot=self.collect()
        self.assertEqual([e.path for e in snapshot.entries],['output','output/result.txt'])
        self.assertEqual(snapshot.entries[1].content,b'untrusted output')
        before=snapshot.digest
        (self.goal/'output/result.txt').write_text('other output')
        self.assertNotEqual(before,self.collect().digest)
        self.assertEqual(snapshot.entries[1].content,b'untrusted output')
        self.assertNotIn(b'not an artifact',b''.join(e.content for e in snapshot.entries))

    def test_symlink_hardlink_fifo_hidden_name_and_size_are_rejected(self):
        output=self.goal/'output/result'
        creators=(lambda:output.symlink_to(self.root/'work/codex-home/auth.json'),
                  lambda:os.link(self.root/'work/codex-home/auth.json',output),
                  lambda:os.mkfifo(output),
                  lambda:(self.goal/'output/.hidden').write_bytes(b'x'),
                  lambda:output.write_bytes(b'x'*(1024*1024+1)))
        for create in creators:
            with self.subTest(create=create):
                create()
                with self.assertRaises(ContractError):self.collect()
                for path in (output,self.goal/'output/.hidden'):
                    if os.path.lexists(path):path.unlink()

    def test_cancel_and_read_race_refuse_snapshot(self):
        output=self.goal/'output/result';output.write_bytes(b'before')
        with self.assertRaisesRegex(RuntimeError,'cancel'):
            self.collect(check=Mock(side_effect=RuntimeError('cancel')))
        real_read=os.read
        def racing(fd,n):
            data=real_read(fd,n)
            if data:output.write_bytes(b'changed')
            return data
        with patch.object(runtime.os,'read',side_effect=racing),self.assertRaisesRegex(ContractError,'CHANGED'):
            self.collect()

    def test_approval_callback_runs_as_root_then_reader_identity_restored(self):
        observed=[]
        with patch.object(runtime.os,'seteuid',side_effect=lambda uid:observed.append(uid)), \
             patch.object(runtime,'collect_untrusted_goal_bytes',side_effect=lambda r,b,check:check()):
            runtime.GoalLinuxUIDBackend.collect_goal(self.request,self.binding,lambda:observed.append('check'))
        self.assertEqual(observed,[10002,0,'check',10002,0])
