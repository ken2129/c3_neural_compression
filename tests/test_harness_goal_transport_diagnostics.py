"""Real bounded stdio pipes and controller persistence; no Codex/model/network.

UID/process ownership is the existing FakeOS fixture. Protocol parsing,
transport rechecks, exception normalization, launcher ACK and SQLite are real.
"""
from contextlib import redirect_stderr, ExitStack
import io
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from research_harness import app_server_adapter as app, goal_runtime as runtime
from research_harness.schema import canonical, ContractError
from research_harness.manager_runtime import RecheckRejected, require_recheck_reply
from research_harness.goal_result import GoalResult
from research_harness.goal_artifacts import GoalEntry, GoalChange, GoalChangeCandidate
from research_harness.restart_boundary import RestartEpoch
from tests.test_harness_goal_runtime import GoalOS, request, binding
from tests.test_harness_goal_runner import frames
from tests.test_harness_goal_deployment import launcher, prepare, profile_fixture
from tests.test_harness_goal_service import GoalServiceTests
from test_harness_service import ISSUE

SECRET = 'private-token remote-body model-output /private/path'


class PipeProcess:
    pid = 10
    def __init__(self, raw, code=None, broken_input=False):
        self.code = code
        reader, writer = os.pipe(); self.input_reader = reader
        self.stdin = os.fdopen(writer, 'wb', buffering=0)
        reader, writer = os.pipe()
        # All test frames fit in the smallest Linux pipe capacity (4096 bytes).
        if len(raw) > 4096: raise AssertionError('fixture pipe budget')
        os.write(writer, raw); os.close(writer)
        self.stdout = os.fdopen(reader, 'rb', buffering=0)
        reader, writer = os.pipe(); os.close(writer)
        self.stderr = os.fdopen(reader, 'rb', buffering=0)
        if broken_input:
            os.close(self.input_reader); self.input_reader = None

    def poll(self): return self.code
    def close(self):
        for stream in (self.stdin, self.stdout, self.stderr): stream.close()
        if self.input_reader is not None: os.close(self.input_reader)


class PipeOS(GoalOS):
    def __init__(self, process): super().__init__(); self.process_fixture = process
    def launch_goal(self, request):
        handle = super().launch_goal(request)
        handle.process = self.process_fixture
        return handle


class GoalTransportDiagnosticsTests(unittest.TestCase):
    def exercise(self, kind='recheck', reason='source_timeout', origin='manager', child_code=None,
                 req=None, run_binding=None):
        req = request() if req is None else req
        values = frames()
        if kind == 'server': values = [{'id':1, 'error':{'code':-32603, 'message':SECRET}}]
        raw = b''.join(canonical(x)+b'\n' for x in values)
        if kind in ('recheck','direct_recheck','timeout','exit','broken'): raw = b''
        if kind == 'json': raw = (SECRET+'\n').encode()
        if kind == 'eof': raw = b''
        process = PipeProcess(raw, code=child_code, broken_input=kind=='broken')
        self.addCleanup(process.close)
        backend = PipeOS(process); supervisor = runtime.GoalUIDSupervisor(backend)
        def recheck():
            state = app._DIAGNOSTIC_CONTEXT.get()
            if kind == 'recheck' and state is not None and state.phase in ('unscoped','initialize_send'):
                require_recheck_reply({'approved':False,'reason':reason,'origin':origin})
            if kind == 'direct_recheck' and state is not None and state.phase == 'initialize_wait':
                raise RecheckRejected(reason, origin=origin)
        if kind == 'timeout':
            def clock():
                state = app._DIAGNOSTIC_CONTEXT.get()
                return 100 if state is not None and state.phase == 'initialize_send' else 0
            backend.now = clock
        log = io.StringIO(); saved = []
        with redirect_stderr(log):
            try:
                result = supervisor.run_goal(req, recheck, binding=binding(req) if run_binding is None else run_binding,
                    quarantine=lambda snapshot:saved.append(snapshot), enabled=True)
            except Exception as exc:
                self.assertNotIn(10, backend.members)
                self.assertTrue(runtime.goal_failure_can_acknowledge(exc))
                return exc, runtime.safe_goal_runtime_diagnostic(exc), log.getvalue()
        self.assertEqual(len(saved), 1)
        self.assertNotIn(10, backend.members)
        return result, None, log.getvalue()

    def test_manager_recheck_failure_is_not_a_transport_outage(self):
        for reason in ('source_timeout','source_rate_limited','expired','changed'):
            for origin in ('manager','launcher_ipc'):
                with self.subTest(reason=reason, origin=origin):
                    exc, detail, log = self.exercise(reason=reason, origin=origin)
                    self.assertEqual(detail['reason'], 'GOAL_APPROVAL_RECHECK_FAILED')
                    inner = detail['app_server']
                    self.assertEqual(inner['reason'], 'APP_SERVER_RECHECK_FAILED')
                    self.assertEqual((inner['recheck_reason'],inner['recheck_origin']), (reason,origin))
                    self.assertEqual((inner['phase'],inner['method']), ('initialize_send','initialize'))
                    self.assertFalse(inner['initialized']); self.assertFalse(inner['turn_accepted'])
                    self.assertNotIn('observed_child_returncode', detail)
                    self.assertEqual(json.loads(log)['app_server'], inner)
                    exc.args=(SECRET,); exc.app_server=SECRET
                    detail['app_server']['recheck_reason']=SECRET
                    self.assertNotIn(SECRET, json.dumps(runtime.safe_goal_runtime_diagnostic(exc)))

    def test_disconnect_timeout_format_and_server_response_remain_distinct(self):
        cases = [('eof','GOAL_TRANSPORT_EARLY_EOF','APP_SERVER_EARLY_EOF'),
                 ('broken','GOAL_TRANSPORT_FAILURE','APP_SERVER_INPUT_PIPE_FAILED'),
                 ('json','GOAL_TRANSPORT_FAILURE','APP_SERVER_JSON_FRAME_INVALID'),
                 ('timeout','GOAL_TRANSPORT_DEADLINE','APP_SERVER_DEADLINE_EXCEEDED'),
                 ('exit','GOAL_TRANSPORT_FAILURE','APP_SERVER_EXITED_WRITING')]
        for kind, top, inner in cases:
            with self.subTest(kind=kind):
                _, detail, log = self.exercise(kind, child_code=23 if kind=='exit' else None)
                self.assertEqual(detail['reason'], top)
                self.assertEqual(detail['app_server']['reason'], inner)
                self.assertEqual(detail.get('observed_child_returncode'), 23 if kind=='exit' else None)
                self.assertNotIn(SECRET, log)
        _, detail, log = self.exercise('server')
        self.assertEqual(detail['reason'], 'GOAL_SERVER_ERROR')
        self.assertEqual((detail['protocol']['phase'],detail['protocol']['method']), ('initialize_wait','initialize'))
        self.assertFalse(detail['protocol']['turn_accepted'])
        self.assertNotIn(SECRET, log)

    def test_direct_manager_failure_preserves_approval_and_protocol_context(self):
        _, detail, _ = self.exercise('direct_recheck')
        self.assertEqual(detail['reason'],'GOAL_APPROVAL_RECHECK_FAILED')
        self.assertEqual(detail['approval_failure'],{'reason':'source_timeout','origin':'manager'})
        self.assertEqual(detail['protocol']['phase'],'initialize_wait')

    def test_normal_wire_responses_reach_quarantine_callback(self):
        result, detail, log = self.exercise('normal')
        self.assertIsNone(detail); self.assertEqual(log,'')
        self.assertEqual(result.candidate.outcome,'completed')
        self.assertFalse(result.candidate.independently_validated)
        self.assertTrue(result.requires_namespace_restart)

    def test_real_failure_survives_launcher_ack_and_manager_sqlite_block(self):
        exc, detail, _ = self.exercise(child_code=23)
        plan = request().plan
        profile = profile_fixture()
        profile.update(input_source_sha=plan['source_sha'],deployment_id=plan['deployment_id'],model=plan['model'],
                       resource_limits=dict(max_seconds=plan['max_seconds'],**plan['resource_budget']))
        profile = prepare.installed_profile(profile, plan)
        b = dict(source_sha=plan['source_sha'],plan_digest=request().digest,
                 request_id='req-1',run_id='run-1',issue_number=ISSUE,issue_content_sha256='d'*64)
        incoming_frames=[{'op':'prepare_goal','plan':plan},{'op':'preflight'},
                         {'op':'goal','binding':b},{'op':'failure_ack','issue':ISSUE,'phase':'BLOCKED'}]
        read_fd, write_fd = os.pipe()
        os.write(write_fd,b''.join(canonical(f)+b'\n' for f in incoming_frames));os.close(write_fd)
        incoming = os.fdopen(read_fd,'rb',buffering=0);self.addCleanup(incoming.close)
        class Pipe(io.BytesIO):
            def close(self): self.saved=self.getvalue();super().close()
        outgoing=Pipe();control=SimpleNamespace(pid=22,stdin=outgoing,stdout=incoming)
        backend=SimpleNamespace(admit_goal=lambda _:None,resources=lambda:dict(memory=1024**3,pids=64,work_bytes=256*1024**2))
        def failed(*args,**kwargs): raise exc
        supervisor=SimpleNamespace(reserve=lambda:None,run_goal=failed)
        root_log=io.StringIO()
        epoch=RestartEpoch('a'*64,'11111111-1111-1111-1111-111111111111',22,33)
        with ExitStack() as stack:
            for target,name,value in [(launcher,'deployment',lambda:({'container_id':'a'*64},profile,False)),
                    (launcher,'read_epoch',lambda **kw:epoch),
                    (launcher.subprocess,'Popen',lambda *a,**kw:control),
                    (launcher,'stage_workspace',lambda _:object()),
                    (launcher,'GoalLinuxUIDBackend',lambda:backend),
                    (launcher,'GoalUIDSupervisor',lambda *a,**kw:supervisor),
                    (launcher.time,'sleep',lambda _:None)]:
                stack.enter_context(patch.object(target,name,value))
            with redirect_stderr(root_log):launcher.supervise()
        root_record=json.loads(root_log.getvalue())
        self.assertEqual(root_record['app_server'],detail['app_server'])
        failure=json.loads(outgoing.saved.splitlines()[-1])
        self.assertEqual(failure,{'goal_failure':detail})
        helper=GoalServiceTests();helper.setUp();self.addCleanup(helper.doCleanups)
        attempts=[];manager_log=io.StringIO()
        def run(binding,recheck):
            attempts.append(binding['run_id'])
            pipe=launcher.GoalPipeRuntime(io.StringIO(canonical(failure).decode()+'\n'),
                io.StringIO(),helper.runtime.current_epoch(),profile)
            return pipe.run(binding,recheck)
        helper.runtime.run=run
        with redirect_stderr(manager_log),self.assertRaises(ContractError):helper.controller.process(ISSUE)
        self.assertEqual(json.loads(manager_log.getvalue())['app_server'],detail['app_server'])
        self.assertEqual(helper.state.get(ISSUE)['phase'],'BLOCKED')
        self.assertIsNone(helper.state.get(ISSUE)['result'])
        helper.restart()
        report=helper.controller.process(ISSUE)
        self.assertEqual(report['phase'],'BLOCKED');self.assertTrue(report['duplicate'])
        self.assertEqual(len(attempts),1);self.assertEqual(helper.writes,0)
        self.assertNotIn(SECRET,root_log.getvalue()+manager_log.getvalue())

    def test_normal_protocol_result_is_saved_and_waits_for_content_review(self):
        from research_harness.goal_runner import GoalRequest
        helper=GoalServiceTests();helper.setUp();self.addCleanup(helper.doCleanups)
        attempts=[]
        def run(b,recheck):
            attempts.append(b['run_id']);recheck()
            req=GoalRequest(helper.source.request['plan'],b['source_sha'])
            run_binding=runtime.GoalRunBinding(b['source_sha'],b['plan_digest'],b['request_id'],b['run_id'])
            pending,detail,_=self.exercise('normal',req=req,run_binding=run_binding)
            self.assertIsNone(detail)
            change=GoalChange('output/report.md',None,GoalEntry('output/report.md','file',0o644,b'fixture candidate\n'))
            candidate=GoalChangeCandidate(b['source_sha'],('output',),'c'*64,'d'*64,(change,))
            return GoalResult(**{k:b[k] for k in ('request_id','run_id','issue_number','plan_digest','source_sha')},
                model=req.plan['model'],candidate_outcome=pending.candidate.outcome,
                candidate_summary=pending.candidate.summary,candidate=candidate,elapsed_seconds=pending.elapsed_seconds)
        helper.runtime.run=run
        self.assertEqual(helper.controller.process(ISSUE)['phase'],'RESULT_AWAITING_RESTART')
        self.assertIsNotNone(helper.state.get(ISSUE)['result'])
        helper.restart()
        self.assertEqual(helper.controller.process(ISSUE)['phase'],'AWAITING_CONTENT_REVIEW')
        self.assertEqual(len(attempts),1);self.assertEqual(helper.writes,0)

    def test_manager_negative_reply_logs_fixed_reason_before_leaving_pipe(self):
        b=dict(source_sha='a'*40,plan_digest='b'*64,request_id='req-1',run_id='run-1',issue_number=ISSUE,issue_content_sha256='d'*64)
        incoming=io.StringIO(canonical({'op':'recheck'}).decode()+'\n');outgoing=io.StringIO()
        pipe=launcher.GoalPipeRuntime(incoming,outgoing,None,profile_fixture())
        def reject():raise RecheckRejected('source_timeout',origin='manager')
        log=io.StringIO()
        with redirect_stderr(log),self.assertRaises(RecheckRejected):pipe.run(b,reject)
        self.assertEqual(json.loads(log.getvalue())['approval_failure'],{'reason':'source_timeout','origin':'manager'})
        negative=json.loads(outgoing.getvalue().splitlines()[-1])
        self.assertEqual(negative,{'approved':False,'reason':'source_timeout','origin':'manager'})

    def test_manager_recheck_rejection_is_durably_blocked_without_failure_frame(self):
        helper=GoalServiceTests();helper.setUp();self.addCleanup(helper.doCleanups)
        attempts=[]
        def run(b,recheck):
            attempts.append(b['run_id'])
            pipe=launcher.GoalPipeRuntime(io.StringIO(canonical({'op':'recheck'}).decode()+'\n'),
                io.StringIO(),helper.runtime.current_epoch(),profile_fixture())
            def reject():raise RecheckRejected('source_timeout',origin='manager')
            return pipe.run(b,reject)
        helper.runtime.run=run;log=io.StringIO()
        with redirect_stderr(log),self.assertRaises(RecheckRejected):helper.controller.process(ISSUE)
        self.assertEqual(helper.state.get(ISSUE)['phase'],'BLOCKED')
        self.assertIsNone(helper.state.get(ISSUE)['result'])
        helper.restart();self.assertEqual(helper.controller.process(ISSUE)['phase'],'BLOCKED')
        self.assertEqual(len(attempts),1)
        self.assertEqual(json.loads(log.getvalue())['approval_failure']['reason'],'source_timeout')

    def test_closed_failure_schema_rejects_secret_or_forged_fields(self):
        _, detail, _ = self.exercise()
        for changed in [dict(detail,reason=SECRET),dict(detail,stage=SECRET),dict(detail,extra=SECRET),
                dict(detail,observed_child_returncode=True),dict(detail,observed_child_returncode=999),
                dict(detail,app_server={**detail['app_server'],'method':SECRET}),
                dict(detail,app_server={**detail['app_server'],'recheck_origin':SECRET})]:
            self.assertIsNone(runtime.validated_goal_failure(changed))
