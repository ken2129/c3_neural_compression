"""Goal controller integration, fake runtime only: no model or deployment."""
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from research_harness.schema import ContractError
from research_harness.goal_service import GoalController
from research_harness.goal_result import GoalResult
from research_harness.goal_artifacts import GoalChangeCandidate,GoalChange,GoalEntry
from research_harness.goal_plan import GOAL_RESOURCE_BUDGET
from research_harness.goal_publisher import GoalPublicationState
from research_harness.service_state import initialize,ServiceState
from research_harness.intake_flow_fixture import FIXTURE_NOW
from test_harness_service import ServiceSource,SOURCE_SHA,ISSUE
from test_harness_service_restart import EpochRuntime
from test_harness_restart_boundary import epoch
from test_harness_goal_runner import plan


class Runtime(EpochRuntime):
    def prepare_plan(self,value):self.plan=value
    def run(self,binding,recheck):
        self.runs+=1
        recheck()
        if self.run_error:raise self.run_error
        change=GoalChange('output/report.md',None,GoalEntry('output/report.md','file',0o644,b'Public research summary\n'))
        candidate=GoalChangeCandidate(SOURCE_SHA,('output',),'c'*64,'d'*64,(change,))
        return GoalResult(**{k:binding[k] for k in ('request_id','run_id','issue_number','plan_digest','source_sha')},
            model=self.plan['model'],candidate_outcome='completed',candidate_summary='Private model prose',
            candidate=candidate,elapsed_seconds=.5)


class GoalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);initialize(self.root)
        self.state=ServiceState(self.root,require_epochs=True)
        self.addCleanup(lambda:self.state.close())
        self.source=ServiceSource()
        self.source.request['plan']=plan()
        self.source.request['plan']['source_sha']=SOURCE_SHA
        self.source.render(refresh_receipt=True)
        self.runtime=Runtime();self.allowed=False;self.writes=0
        self.now=FIXTURE_NOW
        self.make()

    def make(self,enabled=True):
        def factory(persist):
            owner=self
            class Publisher:
                def publish(self,request,*,state,authorized,authorize_callback):
                    if not authorized or not authorize_callback():raise ContractError('not approved')
                    owner.writes+=1
                    final=GoalPublicationState(request.digest,request.branch,phase='PUBLISHED',
                        tree_sha='e'*40,head_sha='f'*40,pr_number=123)
                    persist(final)
                    return final
            return Publisher()
        self.controller=GoalController(self.state,self.source,self.source,self.runtime,factory,
            source_sha=SOURCE_SHA,publication_base_getter=lambda:'a'*40,
            clock=lambda:self.now,enabled=enabled,
            resource_limits=dict(max_seconds=120,**GOAL_RESOURCE_BUDGET),
            content_authorizer=lambda request:self.allowed)

    def run_once(self):
        result=self.controller.process(ISSUE)
        self.assertEqual(result['phase'],'RESULT_AWAITING_RESTART')
        self.assertEqual(self.runtime.runs,1)
        self.assertEqual(self.writes,0)

    def restart(self):
        self.state.close();self.state=ServiceState(self.root,require_epochs=True)
        self.runtime.epoch=epoch(pid1_start_ticks=2000)
        self.make()

    def test_disabled_does_not_claim_or_run(self):
        self.make(enabled=False)
        self.assertEqual(self.controller.process(ISSUE)['phase'],'GOAL_DISABLED')
        self.assertIsNone(self.state.get(ISSUE));self.assertEqual(self.runtime.runs,0)

    def test_execution_result_persists_then_waits_for_content_review(self):
        self.run_once();self.restart()
        self.assertEqual(self.controller.process(ISSUE)['phase'],'AWAITING_CONTENT_REVIEW')
        self.assertEqual(self.runtime.runs,1);self.assertEqual(self.writes,0)
        self.assertEqual(self.state.get(ISSUE)['result']['base_sha'],'a'*40)
        self.assertEqual(self.state.get(ISSUE)['result']['result']['source_sha'],SOURCE_SHA)

    def test_approved_artifacts_publish_without_rerun(self):
        self.run_once();self.restart();self.allowed=True
        self.assertEqual(self.controller.process(ISSUE)['phase'],'PUBLISHED')
        self.assertEqual(self.writes,1);self.assertEqual(self.runtime.runs,1)
        self.assertEqual(self.controller.process(ISSUE)['phase'],'PUBLISHED')
        self.assertEqual(self.writes,1)
        self.restart();self.assertEqual(self.controller.process(ISSUE)['phase'],'PUBLISHED')

    def test_same_generation_cannot_release_result(self):
        self.run_once();self.allowed=True
        with self.assertRaises(ContractError):self.controller.process(ISSUE)
        self.assertEqual(self.writes,0)

    def test_old_canary_plan_cannot_enable_goal(self):
        self.source.request['plan']=dict(kind='cpu-canary-v1',source_sha=SOURCE_SHA,publish_result_pr=True)
        self.source.render(refresh_receipt=True)
        with self.assertRaises(ContractError):self.controller.process(ISSUE)
        self.assertIsNone(self.state.get(ISSUE));self.assertEqual(self.runtime.runs,0)

    def test_failed_claim_never_reexecutes(self):
        self.runtime.run_error=RuntimeError('fixture')
        with self.assertRaises(RuntimeError):self.controller.process(ISSUE)
        self.assertEqual(self.state.get(ISSUE)['phase'],'BLOCKED')
        self.runtime.run_error=None
        self.assertEqual(self.controller.process(ISSUE)['phase'],'BLOCKED')
        self.assertEqual(self.runtime.runs,1)

    def test_expired_execution_can_publish_only_with_fresh_content_approval(self):
        self.run_once();self.restart()
        self.now += 24*3600
        self.assertEqual(self.controller.process(ISSUE)['phase'],'AWAITING_CONTENT_REVIEW')
        self.allowed=True
        self.assertEqual(self.controller.process(ISSUE)['phase'],'PUBLISHED')
        self.assertEqual(self.runtime.runs,1)

    def test_cancel_before_admission_creates_no_claim(self):
        self.source.revoke()
        with self.assertRaises(ContractError):self.controller.process(ISSUE)
        self.assertIsNone(self.state.get(ISSUE));self.assertEqual(self.runtime.runs,0)

    def test_changed_goal_after_preflight_does_not_claim(self):
        original=self.runtime.preflight
        def changed():
            result=original()
            self.source.request['plan']['goal']='A different unapproved goal'
            self.source.render()
            return result
        self.runtime.preflight=changed
        with self.assertRaises(ContractError):self.controller.process(ISSUE)
        self.assertIsNone(self.state.get(ISSUE));self.assertEqual(self.runtime.runs,0)

    def test_content_authorization_read_failure_does_not_publish(self):
        self.run_once();self.restart()
        def unavailable(request):raise TimeoutError('private transport text')
        self.controller.content_authorizer=unavailable
        with self.assertRaises(TimeoutError):self.controller.process(ISSUE)
        self.assertEqual(self.writes,0)
        self.assertEqual(self.state.get(ISSUE)['phase'],'RESULT_READY')
        self.assertIsNone(self.state.get(ISSUE)['publication'])

    def test_empty_candidate_is_not_reported_as_ready_for_publication(self):
        original=self.runtime.run
        def empty(binding,recheck):
            result=original(binding,recheck)
            return replace(result,candidate=GoalChangeCandidate(SOURCE_SHA,('output',),'c'*64,'d'*64,()))
        self.runtime.run=empty
        self.run_once();self.restart();self.allowed=True
        report=self.controller.process(ISSUE)
        self.assertEqual(report['phase'],'NO_FILE_CHANGES')
        self.assertIs(report['research_success_verified'],False)
        self.assertEqual(self.writes,0);self.assertEqual(self.runtime.runs,1)

    def test_changed_issue_cannot_publish_saved_output(self):
        self.run_once();self.restart();self.allowed=True
        self.source.issue['title']+=' changed'
        with self.assertRaises(ContractError):self.controller.process(ISSUE)
        self.assertEqual(self.writes,0)

if __name__=='__main__':unittest.main()
