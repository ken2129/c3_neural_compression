"""Formal Issue/owner/SQLite → real CPU GPU-agent → modeled analysis/PR.

No real GPU, SSH connection, Codex call or GitHub write. Kernel isolation of the
production resident stays covered separately by its existing diagnostic.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from research_harness.gpu_backend import SSHDockerGPUBackend, GPUCleanupUnverified
from research_harness.gpu_contract import definition_digest
from research_harness.schema import ContractError
import test_harness_direct_pr as direct_fixture
from test_harness_direct_pr import direct_profile, prepare, ISSUE, PR_URL
from test_harness_gpu import request, target_profile
from test_harness_gpu_agent import registry, agent_process
from test_harness_resident import resident

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform=='linux' and hasattr(os,'pidfd_open'),'Linux resident GPU fixture')
class ResidentGPUFlowTests(unittest.TestCase):
    setUp=direct_fixture.DirectServiceTests.setUp
    make=direct_fixture.DirectServiceTests.make

    def prepare(self, *, busy=False):
        self.source.request['plan']['gpu_experiment']=request()
        self.source.render(refresh_receipt=True)
        gpu_root=self.root/'gpu';gpu_root.mkdir(mode=0o700)
        path,recipe=registry(gpu_root)
        profile=direct_profile(self.source.request['plan'])
        profile.update(target_profile())
        profile['gpu_targets']['fixture']['recipes']['small']['definition_sha256']=definition_digest(recipe)
        self.profile=prepare.installed_profile(profile,self.source.request['plan'],allow_gpu=True)
        self.installed=resident.common  # Existing protected-file/IPC implementation.
        broker=self.root/'broker';broker.mkdir()
        self.child=subprocess.Popen([sys.executable,'-B',str(ROOT/'tests/resident_gpu_pipe_fixture.py'),str(broker)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        epoch=self.runtime.epoch
        self.runtime=resident.ResidentGoalPipeRuntime(self.child.stdout,self.child.stdin,epoch,self.profile)
        self.make()
        self.gpu_calls=[];self.gpu_results=[];self.phases=[]
        def factory(profile,req,*,status):
            def launch(argv,**kwargs):
                self.gpu_calls.append(argv)
                return agent_process(path,'busy' if busy else 'idle',**kwargs)
            def observe(phase,elapsed):
                self.phases.append(phase);status(phase,elapsed)
                if getattr(self,'cancel_when_waiting',False) and phase=='waiting_gpu':
                    self.source.revoke();self.now+=6;self.mono+=6
            return SSHDockerGPUBackend(profile,req,popen=launch,credential_check=lambda target:None,status=observe)
        self.factory=factory;self.broker=broker
        self.addCleanup(self.close_child)

    def close_child(self):
        if self.child.poll() is None:self.child.terminate()
        self.child.wait(timeout=5)
        self.child.stdin.close();self.child.stdout.close();self.child.stderr.close()

    def finish(self):
        resident.common.write_frame(self.child.stdin,{'op':'job_finished'})
        self.assertEqual(resident.common.read_frame(self.child.stdout),{'cleaned':True})
        self.assertEqual(self.child.wait(timeout=5),0)

    def process(self):
        with patch.object(resident,'SSHDockerGPUBackend',side_effect=self.factory), \
             patch.object(resident,'dispatch_profile',return_value=self.profile), \
             patch.object(resident,'persist_gpu_result',side_effect=lambda binding,result:self.gpu_results.append(result)):
            return resident.process_event(self.controller,{'issue_number':ISSUE},finish_runtime=self.finish)

    def test_approved_issue_runs_gpu_once_then_analysis_and_result_pr_report(self):
        self.prepare();answer=self.process()
        self.assertEqual(answer['phase'],'WORKER_FINISHED');self.assertEqual(answer['pr_url'],PR_URL)
        self.assertEqual(len(self.gpu_calls),1);self.assertEqual(self.gpu_results[0]['outcome'],'completed')
        self.assertEqual(json.loads((self.broker/'events.json').read_text()),
                         ['prepared','preflight','gpu_staged','cpu_analysis','cleaned'])
        saved=self.state.get(ISSUE);self.assertEqual(saved['phase'],'WORKER_FINISHED')
        self.assertEqual(self.controller.process(ISSUE)['duplicate'],True)
        self.assertEqual(len(self.gpu_calls),1)

    def test_uninstalled_gpu_recipe_is_rejected_before_claim_or_launch(self):
        self.prepare();self.profile.pop('gpu_targets')
        self.assertEqual(self.process()['phase'],'NOT_APPROVED')
        self.assertIsNone(self.state.get(ISSUE));self.assertEqual(self.gpu_calls,[])

    def test_disabled_controller_never_starts_gpu(self):
        self.prepare();self.make(enabled=False)
        answer=self.controller.process(ISSUE)
        self.assertEqual(answer['phase'],'DIRECT_PR_DISABLED');self.assertEqual(self.gpu_calls,[])

    def test_revocation_while_waiting_cancels_without_starting_cpu_analysis(self):
        self.prepare(busy=True);self.cancel_when_waiting=True
        answer=self.process()
        self.assertEqual(answer['phase'],'BLOCKED')
        self.assertEqual(self.gpu_results[0]['outcome'],'cancelled')
        events=json.loads((self.broker/'events.json').read_text())
        self.assertNotIn('cpu_analysis',events);self.assertNotIn('gpu_staged',events)

    def test_uncertain_remote_cleanup_does_not_ack_cleaned_or_launch_analysis(self):
        self.prepare()
        def bad(*args,**kwargs):raise GPUCleanupUnverified()
        self.factory=lambda *a,**kw:type('Broken',(),{'run':bad})()
        with self.assertRaises(resident.ResidentHandshakeBlocked):self.process()
        self.assertEqual(self.state.get(ISSUE)['phase'],'BLOCKED')
        self.assertFalse((self.broker/'gpu-result.json').exists())


if __name__=='__main__':unittest.main()
