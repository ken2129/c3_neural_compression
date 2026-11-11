"""Diagnostic observation isolation; no credentials/network/Docker/model.

Controller approval, pipe frames, bounded stdio parsing, result readback and
SQLite persistence use production classes. Process/UID/admission and app-server
responses are explicit fixtures, not evidence of native deployment success.
"""
import ast
from contextlib import ExitStack, redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from research_harness.goal_service import GoalController
from research_harness.goal_plan import validate_goal_plan
from research_harness.goal_runner import GoalRequest
from research_harness.goal_runtime import GoalRunBinding
from research_harness.goal_result import GoalResult
from research_harness.goal_artifacts import GoalChangeCandidate, GoalChange, GoalEntry
from research_harness.manager_runtime import RecheckRejected
from research_harness.owner_approval import _READ_DEADLINE
from research_harness.schema import canonical, ContractError, loads
from research_harness.service_state import initialize, ServiceState
from tests import test_harness_goal_deployment as deployment_tests
from tests import test_harness_goal_runner as runner_tests
from tests import test_harness_goal_transport_diagnostics as transport_tests
from tests.test_harness_restart_boundary import epoch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools/research_goal'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


observation = load('c3_test_observation', TOOLS / 'diagnostic_observation.py')
boundary = load('c3_test_boundary', TOOLS / 'diagnostic_boundary.py')
with patch.dict(sys.modules, {'goal_launcher': deployment_tests.launcher,
                             'diagnostic_boundary': boundary,
                             'diagnostic_observation': observation}):
    driver = load('c3_test_driver', TOOLS / 'diagnostic_driver.py')

SECRET = 'TOKEN arbitrary-model-output /private/path'
SUCCESS = dict(owner_record_matched=True, expired=True)


class Clock:
    def __init__(self): self.wall = time.time(); self.mono = 100.
    def advance(self, seconds): self.wall += seconds; self.mono += seconds


class DiagnosticIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); initialize(self.root)
        self.state = ServiceState(self.root, require_epochs=True)
        self.addCleanup(lambda: self.state.close())
        self.plan = runner_tests.plan()
        self.profile = deployment_tests.profile_fixture()
        self.profile.update(input_source_sha=self.plan['source_sha'], model=self.plan['model'],
            deployment_id=self.plan['deployment_id'], resource_limits=dict(max_seconds=self.plan['max_seconds'],
                                                                        **self.plan['resource_budget']))
        self.profile = deployment_tests.prepare.installed_profile(
            {k:v for k,v in self.profile.items() if k != 'installed_plan_digest'}, self.plan)
        self.clock = Clock(); self.attempts = 0; self.binding = None; self.pending = None
        self.mutate_at_refresh = lambda: None
        self.outgoing = io.StringIO(); owner = self
        class Incoming(io.StringIO):
            def __init__(self): super().__init__(); self.step = 0
            def readline(self, *args):
                self.step += 1
                if self.step == 1: value = {'prepared': True}
                elif self.step == 2: value = {'ready': True}
                elif self.step == 3:
                    owner.mutate_at_refresh(); owner.clock.advance(5.1)
                    value = {'op':'recheck'}
                elif self.step == 4:
                    # Real bounded-stdio parser/supervisor over public synthetic
                    # responses, explicit process-ownership fixture.
                    frames = [loads(line) for line in owner.outgoing.getvalue().splitlines()]
                    owner.binding = next(frame['binding'] for frame in frames if frame.get('op') == 'goal')
                    owner.attempts += 1
                    req = GoalRequest(owner.plan, owner.plan['source_sha'])
                    b = owner.binding
                    helper = transport_tests.GoalTransportDiagnosticsTests()
                    try:
                        pending, detail, _ = helper.exercise('normal', req=req,
                            run_binding=GoalRunBinding(b['source_sha'],b['plan_digest'],b['request_id'],b['run_id']))
                    finally: helper.doCleanups()
                    if detail is not None: raise AssertionError('normal protocol fixture failed')
                    owner.pending = pending
                    # Public artifact bytes are a fixture; no generated code runs.
                    candidate = GoalChangeCandidate(b['source_sha'], ('output',), 'c'*64, 'd'*64,
                        (GoalChange('output/report.md',None,GoalEntry('output/report.md','file',0o644,b'fixture only\n')),))
                    result = GoalResult(**{k:b[k] for k in ('request_id','run_id','issue_number','plan_digest','source_sha')},
                        model=owner.plan['model'], candidate_outcome=pending.candidate.outcome,
                        candidate_summary=pending.candidate.summary,candidate=candidate,elapsed_seconds=pending.elapsed_seconds)
                    owner.result_path = owner.root / (b['run_id']+'.result.json')
                    fd = os.open(owner.result_path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
                    with os.fdopen(fd,'wb') as stream:
                        stream.write(canonical(result.to_dict()));stream.flush();os.fsync(stream.fileno())
                    value = {'goal_result':b['run_id']}
                else: raise AssertionError('unexpected extra IPC read')
                return canonical(value).decode()+'\n'
        self.runtime = deployment_tests.launcher.GoalPipeRuntime(Incoming(), self.outgoing, epoch(), self.profile)

    def make(self, observe):
        log = io.StringIO()
        with redirect_stderr(log):
            c = driver.make_controller(self.state,self.runtime,self.profile,self.plan,observe=observe)
        c.clock = lambda:self.clock.wall; c.monotonic = lambda:self.clock.mono
        self.controller = c
        self.assertIs(type(c), GoalController)
        self.assertIs(c._approval_snapshot.__func__, GoalController._approval_snapshot)
        self.assertIs(c.process.__func__, GoalController.process)
        self.assertIs(type(c.runtime), deployment_tests.launcher.GoalPipeRuntime)
        return c, log.getvalue()

    def read_fixture(self, path, limit):
        # Only substitute deployment UID/path mapping. Check real temporary-file
        # ownership/mode/type/bytes instead of waiving every ownership check.
        self.assertEqual(str(path), '/control/quarantine/'+self.binding['run_id']+'.result.json')
        info = self.result_path.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode)); self.assertEqual(info.st_uid,os.getuid())
        self.assertEqual(stat.S_IMODE(info.st_mode),0o600)
        self.assertLessEqual(info.st_size,limit)
        return loads(self.result_path.read_bytes())

    def process(self):
        with patch.object(deployment_tests.launcher, 'protected_json', side_effect=self.read_fixture):
            return self.controller.process(driver.FIXTURE_NUMBER)

    def test_observation_failures_do_not_veto_valid_approval_or_private_result_save(self):
        def failed(): raise ContractError(SECRET)
        observer = Mock(side_effect=failed)
        c, log = self.make(observer)
        self.assertEqual(json.loads(log)['reason'],'OBSERVATION_CONTRACT_REJECTED')
        self.assertNotIn(SECRET,log); self.assertIsNone(_READ_DEADLINE.get())
        report = self.process()
        self.assertEqual(report['phase'],'RESULT_AWAITING_RESTART')
        row = self.state.get(driver.FIXTURE_NUMBER)
        self.assertIsNotNone(row['result']);self.assertIsNone(row['publication'])
        self.assertEqual(self.attempts,1);observer.assert_called_once()
        self.assertEqual(loads(self.outgoing.getvalue().splitlines()[-1]),{'approved':True})
        # Actual SQLite reopen and production saved-result/restart review path.
        self.state.close();self.state=ServiceState(self.root,require_epochs=True);c.state=self.state
        self.runtime.epoch=epoch(pid1_start_ticks=2000)
        self.assertEqual(self.process()['phase'],'AWAITING_CONTENT_REVIEW')
        self.assertEqual(self.attempts,1);observer.assert_called_once()

    def test_slow_observation_budget_is_closed_before_fresh_approval_budget(self):
        mono = [100.]
        def slow():mono[0]+=6;return SUCCESS
        with patch('research_harness.owner_approval.time.monotonic',side_effect=lambda:mono[0]):
            c, log = self.make(slow)
        self.assertEqual(json.loads(log)['reason'],'OBSERVATION_TIMEOUT')
        self.assertIsNone(_READ_DEADLINE.get())
        self.assertEqual(self.process()['phase'],'RESULT_AWAITING_RESTART')

    def test_observation_success_does_not_authorize_invalid_or_cancelled_or_expired_receipt(self):
        # Different fresh databases prevent a prior refusal from being reset.
        for kind in ('invalid','cancelled','expired','unavailable'):
            with self.subTest(kind=kind):
                helper = type(self)();helper.setUp()
                try:
                    c, log = helper.make(Mock(return_value=SUCCESS))
                    self.assertEqual(json.loads(log)['phase'],'DIAGNOSTIC_OBSERVATION_COMPLETE')
                    body=loads(c.comments.comment['body'])
                    if kind=='invalid':body['content_sha256']='0'*64;c.comments.comment['body']=json.dumps(body)
                    elif kind=='cancelled':body['decision']='cancel-execution';c.comments.comment['body']=json.dumps(body)
                    elif kind=='expired':helper.clock.advance(300)
                    else:c.issues.get_issue=Mock(side_effect=TimeoutError(SECRET))
                    with self.assertRaises(RecheckRejected):helper.process()
                    self.assertIsNone(helper.state.get(driver.FIXTURE_NUMBER))
                    self.assertEqual(helper.attempts,0)
                finally:helper.doCleanups()

    def test_authoritative_midrun_rejection_uses_negative_ipc_block_and_cleanup(self):
        for kind in ('invalid','cancelled','expired','unavailable'):
            with self.subTest(kind=kind):
                helper=type(self)();helper.setUp()
                try:
                    observer=Mock(return_value=SUCCESS);c,_=helper.make(observer)
                    def mutate():
                        body=loads(c.comments.comment['body'])
                        if kind=='invalid':body['content_sha256']='0'*64;c.comments.comment['body']=json.dumps(body)
                        elif kind=='cancelled':body['decision']='cancel-execution';c.comments.comment['body']=json.dumps(body)
                        elif kind=='expired':helper.clock.advance(300)
                        else:c.comments.comments=Mock(side_effect=TimeoutError(SECRET))
                    helper.mutate_at_refresh=mutate
                    log=io.StringIO()
                    with redirect_stderr(log),self.assertRaises(RecheckRejected):helper.process()
                    negative=loads(helper.outgoing.getvalue().splitlines()[-1])
                    self.assertFalse(negative['approved']);self.assertEqual(negative['origin'],'manager')
                    self.assertEqual(helper.runtime.failed_issue,driver.FIXTURE_NUMBER)
                    self.assertEqual(helper.state.get(driver.FIXTURE_NUMBER)['phase'],'BLOCKED')
                    self.assertIsNone(helper.state.get(driver.FIXTURE_NUMBER)['result'])
                    # Replay that actual manager reply through shared transport
                    # and supervisor cleanup (process/UID remains a fixture).
                    native=transport_tests.GoalTransportDiagnosticsTests()
                    try:
                        _, detail, _ = native.exercise('recheck',reason=negative['reason'],origin=negative['origin'])
                    finally:native.doCleanups()
                    self.assertEqual(detail['reason'],'GOAL_APPROVAL_RECHECK_FAILED')
                    self.assertEqual(detail['app_server']['recheck_reason'],negative['reason'])
                    helper.state.close();helper.state=ServiceState(helper.root,require_epochs=True);c.state=helper.state
                    self.assertTrue(helper.process()['duplicate']);self.assertEqual(helper.attempts,0)
                    observer.assert_called_once();self.assertNotIn(SECRET,log.getvalue())
                finally:helper.doCleanups()

    def test_observation_logger_failure_still_performs_authoritative_checks(self):
        class Broken:
            def write(self,_):raise OSError(SECRET)
        with patch.object(observation.sys,'stderr',Broken()):
            c=driver.make_controller(self.state,self.runtime,self.profile,self.plan,
                                    observe=Mock(side_effect=TimeoutError(SECRET)))
        c.clock=lambda:self.clock.wall;c.monotonic=lambda:self.clock.mono;self.controller=c
        self.assertEqual(self.process()['phase'],'RESULT_AWAITING_RESTART')

    def test_process_control_signal_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            observation.observe_once(Mock(side_effect=KeyboardInterrupt()))
        self.assertIsNone(self.state.get(driver.FIXTURE_NUMBER))

    def test_diagnostic_entrypoint_keeps_production_supervision_and_persistence_functions(self):
        launcher=deployment_tests.launcher
        common={name:getattr(launcher,name) for name in
                ('supervise','stage_workspace','freeze_snapshot','GoalPipeRuntime','GoalUIDSupervisor')}
        with ExitStack() as stack:
            for module,name in ((launcher,'SCRIPT'),(launcher,'validate_goal_deployment'),
                    (driver.deployment_module,'validate_goal_deployment'),
                    (driver.runtime_module,'BoundedStdioTransport')):
                stack.enter_context(patch.object(module,name,getattr(module,name)))
            driver.configure_launcher()
            for name,value in common.items():self.assertIs(getattr(launcher,name),value)
            self.assertTrue(issubclass(driver.runtime_module.BoundedStdioTransport,
                                     driver.ObservedTransport.__bases__[0]))

    def test_diagnostic_ceiling_cannot_silently_expand_the_fixed_trial(self):
        observer=Mock(return_value=SUCCESS)
        for field,value in (('max_seconds',181),('model','other-model')):
            changed=dict(self.plan,**{field:value})
            profile=dict(self.profile,resource_limits=dict(self.profile['resource_limits'],max_seconds=181))
            with self.assertRaises(ContractError):
                driver.make_controller(self.state,self.runtime,profile,changed,observe=observer)
        observer.assert_not_called()

    def test_diagnostic_boundary_changes_only_explicit_identity_and_restart_expectations(self):
        source=(ROOT/'research_harness/goal_deployment.py').read_text()
        for old,new in [('dedicated-goal-controller','dedicated-goal-diagnostic'),
                        ('/opt/service/goal_launcher.py','/opt/service/diagnostic_driver.py'),
                        ('unless-stopped','no')]:source=source.replace(old,new)
        source=source.replace('from .schema import ContractError','from research_harness.schema import ContractError')
        self.assertEqual(ast.dump(ast.parse(source)),ast.dump(ast.parse((TOOLS/'diagnostic_boundary.py').read_text())))
        data=deployment_tests.goal_inspect();p=deployment_tests.profile_fixture()
        data['Config']['Labels']['c3.purpose']='dedicated-goal-diagnostic'
        data['Config']['Entrypoint']=['python','-I','-B','/opt/service/diagnostic_driver.py']
        data['HostConfig']['RestartPolicy']['Name']='no'
        from tests import test_harness_deployment_contract as dc
        args=dict(runtime_source_sha=dc.SOURCE_SHA,input_source_sha=p['input_source_sha'],volume_name=dc.VOLUME_NAME,
            volume_inspect={**dc.volume_fixture(),'Labels':{'c3.deployment-id':p['deployment_id']}},
            resource_limits=p['resource_limits'],deployment_id=p['deployment_id'])
        before=canonical(data);boundary.validate_goal_deployment(data,[dc.CONTAINER_ID],**args)
        self.assertEqual(canonical(data),before)
        for key,value in [('CapAdd',['SYS_ADMIN']),('ReadonlyRootfs',False),('PidMode','host'),
                          ('SecurityOpt',[]),('RestartPolicy',{'Name':'unless-stopped'})]:
            changed=loads(before);changed['HostConfig'][key]=value
            with self.assertRaises(ContractError):boundary.validate_goal_deployment(changed,[dc.CONTAINER_ID],**args)


class DiagnosticStopTests(unittest.TestCase):
    def test_non_pid1_or_manager_cannot_install_namespace_stop_handlers(self):
        for pid,uids in ((2,(0,0,0)),(1,(10001,)*3),(1,(0,10001,0))):
            with self.subTest(pid=pid,uids=uids), patch.object(driver.os,'getpid',return_value=pid), \
                    patch.object(driver.os,'getresuid',return_value=uids), \
                    patch.object(driver.signal,'signal') as install:
                with self.assertRaises(ContractError):driver.install_pid1_stop_handlers()
                install.assert_not_called()

    def test_handlers_precede_supervision_and_survive_runtime_watchdog_rearm(self):
        handlers={};old_stop=[]
        def install(signum,callback):
            previous=handlers.get(signum,signal.SIG_DFL);handlers[signum]=callback
            return previous
        def supervise():
            old_stop.extend((handlers[signal.SIGTERM],handlers[signal.SIGINT]))
            # The actual backend re-arms ALRM/CHLD, without replacing TERM/INT.
            driver.runtime_module.GoalLinuxUIDBackend.arm_watchdog(184)
            self.assertIs(handlers[signal.SIGTERM],old_stop[0])
            self.assertIs(handlers[signal.SIGINT],old_stop[1])
        with patch.object(driver.os,'getpid',return_value=1), \
                patch.object(driver.os,'getresuid',return_value=(0,0,0)), \
                patch.object(driver.signal,'signal',side_effect=install), \
                patch.object(driver.signal,'alarm') as alarm, \
                patch.object(driver,'Path') as path, patch.object(driver,'report'), \
                patch.object(driver.launcher,'blocked_startup'), \
                patch.object(driver.launcher,'supervise',side_effect=supervise), \
                patch.object(sys,'argv',['diagnostic_driver.py']):
            path.return_value.read_text.return_value='a'*40
            driver.main()
            self.assertEqual([call.args[0] for call in alarm.call_args_list],[230,184])
            # Termination must also work while PID1 temporarily has manager EUID.
            with patch.object(driver.os,'getresuid',return_value=(0,10001,0)), \
                    patch.object(driver.os,'_exit') as exit_namespace:
                for signum,code in ((signal.SIGTERM,143),(signal.SIGINT,130),(signal.SIGALRM,124)):
                    handlers[signum](signum,None)
                    exit_namespace.assert_called_with(code)

    def test_manager_branch_does_not_install_pid1_handlers(self):
        with patch.object(sys,'argv',['diagnostic_driver.py','manager']), \
                patch.object(driver,'manager') as manager, \
                patch.object(driver,'install_pid1_stop_handlers') as install:
            driver.main()
        manager.assert_called_once();install.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith('linux'),'Linux signal subprocess test')
    def test_real_sigterm_and_sigint_exit_without_watchdog_or_sigkill(self):
        # Only the installer identity check is a fixture: this is an ordinary
        # unprivileged subprocess, not a PID namespace/container. Signal delivery,
        # handlers, os._exit and observed exit codes are real. No model/credentials.
        code = '''
import sys,signal
sys.path[:0]=[ROOT,ROOT+'/tests']
from unittest.mock import patch
from tests.test_harness_goal_diagnostic_isolation import driver
with patch.object(driver.os,'getpid',return_value=1), patch.object(driver.os,'getresuid',return_value=(0,0,0)):
    driver.install_pid1_stop_handlers()
print('READY',flush=True)
while True:signal.pause()
'''.replace('ROOT',repr(str(ROOT)))
        for signum,expected in ((signal.SIGTERM,143),(signal.SIGINT,130)):
            with self.subTest(signal=signum):
                process=subprocess.Popen([sys.executable,'-I','-B','-c',code],
                    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    cwd=ROOT,close_fds=True)
                try:
                    self.assertTrue(select.select([process.stdout],[],[],10)[0],'signal child startup timed out')
                    self.assertEqual(process.stdout.readline(),b'READY\n')
                    process.send_signal(signum)
                    stdout,stderr=process.communicate(timeout=2)
                    self.assertEqual(process.returncode,expected)
                    self.assertEqual(stdout,b'');self.assertEqual(stderr,b'')
                finally:
                    if process.poll() is None:process.kill()
                    process.communicate(timeout=2)
