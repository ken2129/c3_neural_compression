"""Real framing and durable approval/claim tests; no real Docker/model/PR."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research_harness.execution_delivery import ContainerSession
from research_harness.execution_docker import ExecutionCleanupUnverified
from research_harness.goal_runner import GoalRequest
from research_harness.schema import ContractError
from test_harness_execution import configured
import test_harness_direct_pr as direct

ROOT=Path(__file__).resolve().parents[1]

def binding(plan):
    return dict(issue_number=plan['direct_pr']['issue_number'],issue_content_sha256='a'*64,
        request_id='execution-approval',run_id='execution-fixture',source_sha=plan['source_sha'],
        plan_digest=GoalRequest(plan,plan['source_sha']).digest)

class DockerFixture:
    def __init__(self,root,mode='success'):self.root=Path(root);self.mode=mode;self.calls=[]
    def acquire(self,*args):self.calls.append(('acquire',args));return 'c'*64
    def install(self,*args):self.calls.append(('install',args));return '/fixture'
    def worker(self,*args):
        self.calls.append(('worker',args))
        return subprocess.Popen([sys.executable,'-B',str(ROOT/'tests/execution_pipe_fixture.py'),self.mode,
            str(self.root/'started')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)

class ExecutionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.plan,self.profile=configured();self.docker=DockerFixture(self.temp.name)

    def session(self,mode='success'):
        self.docker.mode=mode
        value=ContainerSession(self.profile,self.plan,docker=self.docker);self.addCleanup(value.close)
        return value

    def test_preflight_does_not_start_model_or_transfer_credentials(self):
        session=self.session();epoch=session.prepare()
        self.assertEqual(epoch.container_id,'c'*64)
        self.assertFalse((Path(self.temp.name)/'started').exists())
        self.assertFalse(session.executed)

    def test_bound_completion_is_persisted_only_after_cleanup(self):
        session=self.session();session.prepare();saved=[]
        result=session.run(binding(self.plan),lambda:None,{'offline':'fixture'},persist=saved.append)
        self.assertTrue(session.verified);self.assertEqual(len(saved),1)
        self.assertTrue(saved[0]['cleanup_verified']);self.assertEqual(result.binding,binding(self.plan))

    def test_cancel_waits_for_verified_cleanup_before_rethrowing(self):
        session=self.session('cancel');session.prepare()
        class Revoked(Exception):pass
        def recheck():
            if (Path(self.temp.name)/'started').exists():raise Revoked()
        with self.assertRaises(Revoked):session.run(binding(self.plan),recheck,{})
        self.assertTrue(session.verified)

    def test_wrong_identity_missing_cleanup_and_partial_frame_are_not_success(self):
        for mode in ('wrong_identity','no_cleanup','truncate'):
            with self.subTest(mode=mode):
                session=self.session(mode)
                with self.assertRaises(ContractError):
                    session.prepare();session.run(binding(self.plan),lambda:None,{})
                self.assertFalse(session.verified)

    def test_different_plan_never_reaches_execute(self):
        session=self.session();session.prepare();wrong=binding(self.plan);wrong['plan_digest']='0'*64
        with self.assertRaises(ContractError):session.run(wrong,lambda:None,{})
        self.assertFalse(session.executed)


class ExecutionControllerTests(unittest.TestCase):
    setUp=direct.DirectServiceTests.setUp
    make=direct.DirectServiceTests.make

    def configure(self):
        plan,profile=configured()
        self.source.request['plan']=plan;self.source.render(refresh_receipt=True)
        self.profile.update(profile)
        docker=DockerFixture(self.root)
        class Runtime:
            deployment_contract_verified=True
            def prepare_plan(this,plan):this.session=ContainerSession(profile,plan,docker=docker)
            def preflight(this):return this.session.prepare()
            def run(this,binding,recheck):return this.session.run(binding,recheck,{})
        self.runtime=Runtime();self.make()
        return docker

    def test_formal_owner_receipt_claim_result_and_duplicate_gate(self):
        docker=self.configure();report=self.controller.process(direct.ISSUE)
        self.assertEqual(report['phase'],'WORKER_FINISHED');self.assertFalse(report['pr_verified'])
        before=list(docker.calls);again=self.controller.process(direct.ISSUE)
        self.assertTrue(again['duplicate']);self.assertEqual(docker.calls,before)

    def test_unregistered_target_and_disabled_service_never_contact_docker(self):
        docker=self.configure();self.source.request['plan']['execution_container']='other'
        self.source.render(refresh_receipt=True)
        with self.assertRaises(ContractError):self.controller.process(direct.ISSUE)
        self.assertEqual(docker.calls,[]);self.assertIsNone(self.state.get(direct.ISSUE))
        self.controller.enabled=False
        self.assertEqual(self.controller.process(direct.ISSUE)['phase'],'DIRECT_PR_DISABLED')
        self.assertEqual(docker.calls,[])

if __name__=='__main__':unittest.main()
