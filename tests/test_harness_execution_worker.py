"""Native CPU processes/UIDs/pipes/cleanup. GPU/model/GitHub/Docker are fixtures."""
import base64
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from research_harness.execution_delivery import ContainerSession
from research_harness.execution_docker import ExecutionCleanupUnverified
from test_harness_direct_pr import enabled_config
from test_harness_execution import configured
from test_harness_execution_delivery import binding

ROOT=Path(__file__).resolve().parents[1]
def permissions():
    if sys.platform!='linux' or os.getuid()!=0:return False
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    caps=int(status['CapEff'].strip(),16)
    return all(caps&(1<<bit) for bit in (0,1,5,6,7))

@unittest.skipUnless(permissions(),'requires isolated Linux CPU fixture with UID/cleanup capabilities')
class NativeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.root.chmod(0o755)
        self.workspace=self.root/'workspace';self.workspace.mkdir(mode=0o700);os.chown(self.workspace,10002,10002)
        self.control=self.root/'control';self.control.mkdir(mode=0o700)
        self.private=self.control/'manager-secret';self.private.write_text('OFFLINE-NOT-A-CREDENTIAL')
        self.plan,self.profile=configured()
        runtime=self.root/'runtime';(runtime/'goal-inputs').mkdir(parents=True,mode=0o755)
        manifest=dict(source_sha=self.plan['source_sha'],files={})
        for name in self.plan['inputs']:
            path=runtime/'goal-inputs'/name;path.parent.mkdir(parents=True,exist_ok=True)
            raw=b'OFFLINE PUBLIC INPUT\n';path.write_bytes(raw);path.chmod(0o444)
            manifest['files'][name]=dict(size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        (runtime/'goal-inputs.json').write_text(json.dumps(manifest))
        spec=self.profile['execution_containers']['c3-work']
        spec.update(workspace=str(self.workspace),codex=str(ROOT/'tests/execution_codex_fixture.py'),
            environment={'C3_TEST_PRIVATE':str(self.private),'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8'})
        self.credentials=dict(codex_auth=base64.b64encode(json.dumps(dict(auth_mode='chatgpt',tokens=dict(
            access_token='OFFLINE-ACCESS',refresh_token='OFFLINE-REFRESH',id_token='OFFLINE-ID',account_id='offline-account'))).encode()).decode(),
            worker_token=base64.b64encode(b'OFFLINE_WORKER_TOKEN_123456789').decode(),worker_config=enabled_config())
        owner=self
        class Docker:
            def acquire(self,*args):return 'c'*64
            def install(self,*args):return '/test'
            def worker(self,*args):
                return subprocess.Popen([sys.executable,'-B',str(ROOT/'tests/execution_worker_fixture.py')],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0,
                    env=dict(os.environ,C3_TEST_CONTROL_ROOT=str(owner.control),C3_TEST_RUNTIME=str(owner.root/'runtime')))
        self.docker=Docker()

    def session(self):
        value=ContainerSession(self.profile,self.plan,docker=self.docker);self.addCleanup(value.close)
        return value

    def output(self,name):return self.workspace/'output'/('issue-'+str(self.plan['direct_pr']['issue_number']))/name

    def test_native_failure_edit_retry_wait_same_session_and_private_uid_boundary(self):
        retained=self.workspace/'retained.txt';retained.write_text('existing research artifact')
        session=self.session();session.prepare();value=session.run(binding(self.plan),lambda:None,self.credentials)
        if value.outcome!='completed':self.fail('modeled Codex did not complete')
        evidence=json.loads(self.output('native-session.json').read_text())
        self.assertEqual(evidence['uid'],10002);self.assertTrue(evidence['private_access_denied'])
        self.assertEqual(evidence['initialize_count'],1)
        self.assertEqual(json.loads(self.output('metrics.json').read_text()),{'value':7})
        self.assertEqual(retained.read_text(),'existing research artifact')
        self.assertFalse(Path('/work/codex-home').exists());self.assertFalse(Path('/work/worker-home').exists())
        self.assertTrue(session.verified)

    def test_second_issue_reuses_workspace_without_deleting_previous_results(self):
        session=self.session();session.prepare();session.run(binding(self.plan),lambda:None,self.credentials)
        first=self.output('metrics.json');old=first.read_bytes()
        self.plan['direct_pr']['issue_number']+=1
        session=self.session();session.prepare();identity=binding(self.plan);identity['run_id']='execution-second'
        session.run(identity,lambda:None,self.credentials)
        self.assertEqual(first.read_bytes(),old);self.assertTrue(self.output('metrics.json').exists())

    def test_auth_refresh_returns_on_private_channel_without_entering_result(self):
        self.profile['execution_containers']['c3-work']['environment']['C3_TEST_MODE']='refresh'
        session=self.session();session.prepare();saved=[];reports=[]
        result=session.run(binding(self.plan),lambda:None,self.credentials,
            auth_update=saved.append,persist=reports.append)
        self.assertEqual(json.loads(saved[0])['tokens']['access_token'],'OFFLINE-REFRESHED-ACCESS')
        self.assertNotIn('OFFLINE-REFRESHED-ACCESS',json.dumps(reports))
        self.assertNotIn('auth_state',reports[0]);self.assertEqual(result.outcome,'completed')
        self.assertFalse(Path('/work/codex-home').exists())

    def test_cancel_reaps_escaped_experiment_without_touching_an_unrelated_process(self):
        self.profile['execution_containers']['c3-work']['environment']['C3_TEST_MODE']='hang'
        sentinel=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
        self.addCleanup(lambda:(sentinel.terminate(),sentinel.wait(timeout=5)))
        session=self.session();session.prepare()
        class Revoked(Exception):pass
        def recheck():
            if self.output('escaped.pid').exists():raise Revoked()
        with self.assertRaises(Revoked):session.run(binding(self.plan),recheck,self.credentials)
        self.assertTrue(session.verified);self.assertIsNone(sentinel.poll())
        escaped=int(self.output('escaped.pid').read_text());self.assertFalse(Path('/proc',str(escaped)).exists())

    def test_disconnect_cancels_native_job_and_removes_credential_copies(self):
        self.profile['execution_containers']['c3-work']['environment']['C3_TEST_MODE']='hang'
        session=self.session();session.prepare();identity=binding(self.plan)
        session.send(dict(op='execute',binding=identity,credentials=self.credentials))
        deadline=time.monotonic()+10
        while not self.output('escaped.pid').exists() and time.monotonic()<deadline:
            session.send(dict(op='lease',binding=identity));time.sleep(.1)
        self.assertTrue(self.output('escaped.pid').exists())
        session.process.stdin.close()
        while time.monotonic()<deadline:
            frame=session.receive()
            if frame is not None and frame.get('op')=='worker_failure':break
        else:self.fail('no verified EOF cleanup')
        self.assertTrue(frame['cleanup_verified']);session.process.wait(timeout=5)
        self.assertFalse(Path('/work/worker-home').exists())

    def test_prior_uncertain_claim_refuses_another_job(self):
        runs=self.control/'runs';runs.mkdir();(runs/'uncertain.json').write_text('{"phase":"CLAIMED"}')
        session=self.session()
        with self.assertRaises(ExecutionCleanupUnverified):session.prepare()
        self.assertFalse(Path('/work/worker-home').exists())

if __name__=='__main__':unittest.main()
