import json
from pathlib import Path
import tempfile
import unittest

from research_harness.intake_flow_fixture import FixtureSource, FIXTURE_NOW, dummy_result, run_fixture
from research_harness.issue_intake import Intake
from research_harness.schema import ContractError


class IntakeFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = Path(self.root.name) / 'state.sqlite'
        self.intake = Intake(self.path)
        self.addCleanup(lambda: self.intake.close())
        self.source = FixtureSource()
        self.calls = []

    def worker(self, job):
        self.calls.append(job)
        return dummy_result(job)

    def test_success_saves_and_reopen_does_not_dispatch(self):
        result = run_fixture(self.intake, self.source, self.worker)
        self.assertEqual(result['phase'], 'COMPLETED')
        self.assertFalse(result['execution_allowed'])
        saved = self.intake.db.execute('SELECT result FROM offline_dummy_runs').fetchone()[0]
        self.assertEqual(json.loads(saved), dummy_result(self.calls[0]))
        self.intake.close(); self.intake = Intake(self.path)
        duplicate = run_fixture(self.intake, self.source, self.worker)
        self.assertTrue(duplicate['duplicate'])
        self.assertFalse(duplicate['dispatched'])
        self.assertEqual(len(self.calls), 1)

    def test_crash_claim_never_relaunches(self):
        def fail(job):
            self.calls.append(job)
            raise RuntimeError('simulated uncertain transport')
        with self.assertRaises(RuntimeError): run_fixture(self.intake, self.source, fail)
        again = run_fixture(self.intake, self.source, self.worker)
        self.assertEqual(again['phase'], 'BLOCKED')
        self.assertFalse(again['dispatched'])
        self.assertEqual(len(self.calls), 1)

    def test_uncertain_claim_after_process_death_stays_claimed(self):
        run_fixture(self.intake, self.source, self.worker)
        self.intake.db.execute("UPDATE offline_dummy_runs SET phase='CLAIMED',result=NULL")
        self.intake.db.commit()
        again=run_fixture(self.intake, self.source, self.worker)
        self.assertEqual(again['phase'], 'CLAIMED')
        self.assertFalse(again['dispatched'])
        self.assertEqual(len(self.calls),1)

    def test_changed_plan_rejected_before_dispatch(self):
        run_fixture(self.intake, self.source, self.worker)
        self.source.issue['body'] += ' changed'
        with self.assertRaises(ContractError): run_fixture(self.intake, self.source, self.worker)
        self.assertEqual(len(self.calls),1)

    def test_cancelled_and_expired_reject_initial_dispatch(self):
        body=json.loads(self.source.comment['body']);body['decision']='cancel-execution'
        self.source.comment['body']=json.dumps(body)
        with self.assertRaises(ContractError): run_fixture(self.intake,self.source,self.worker)
        self.assertEqual(self.calls,[])
        other=Intake(':memory:')
        try:
            with self.assertRaises(ContractError): run_fixture(other,FixtureSource(),self.worker,lambda:2000000000)
        finally: other.close()
        self.assertEqual(self.calls,[])

    def test_expiry_between_claim_and_dispatch(self):
        ticks=iter([FIXTURE_NOW,2000000000])
        with self.assertRaises(ContractError): run_fixture(self.intake,self.source,self.worker,lambda:next(ticks))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.intake.db.execute('SELECT phase FROM offline_dummy_runs').fetchone()[0],'BLOCKED')

    def test_cancel_after_worker_rejects_result(self):
        def revoke(job):
            result=self.worker(job)
            body=json.loads(self.source.comment['body']);body['decision']='cancel-execution'
            self.source.comment['body']=json.dumps(body)
            return result
        with self.assertRaises(ContractError): run_fixture(self.intake,self.source,revoke)
        self.assertEqual(self.intake.db.execute('SELECT phase,result FROM offline_dummy_runs').fetchone(),('BLOCKED',None))

    def test_invalid_worker_output_not_saved_and_not_retried(self):
        with self.assertRaises(ContractError): run_fixture(self.intake,self.source,lambda job:{'execution_allowed':True})
        again=run_fixture(self.intake,self.source,self.worker)
        self.assertFalse(again['dispatched'])
        self.assertEqual(self.calls,[])

    def test_live_source_rejected(self):
        with self.assertRaises(ContractError): run_fixture(self.intake,object(),self.worker)
        self.assertEqual(self.calls,[])

    def test_worker_has_no_arbitrary_command_field(self):
        with self.assertRaises(ContractError): dummy_result({'command':'echo hello'})

    def test_build_context_reexcludes_parent_contents(self):
        root=Path(__file__).resolve().parents[1]
        lines=(root/'tools/container_boundary/Dockerfile.flow.dockerignore').read_text().splitlines()
        for parent in ('research_harness','tools','tools/container_boundary'):
            index=lines.index('!'+parent+'/')
            self.assertEqual(lines[index+1],parent+'/**')
        permitted=[s for s in lines if s.startswith('!') and not s.endswith('/')]
        self.assertEqual(set(permitted),{
            '!research_harness/__init__.py','!research_harness/schema.py',
            '!research_harness/store.py','!research_harness/issue_intake.py',
            '!research_harness/owner_approval.py','!research_harness/intake_flow_fixture.py',
            '!tools/container_boundary/Dockerfile.flow','!tools/container_boundary/probe.py',
            '!tools/container_boundary/flow.py'})
