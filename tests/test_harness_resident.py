"""Resident unit/integration fixtures; no Docker, credentials, GitHub or model.

Actual Linux subreaping, file deletion, SQLite and formal approval parsing are
exercised. Kernel UID/capability/cgroup and runner coexistence require the
separately approved Docker diagnostic; fake resource facts do not prove them.
"""
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from research_harness.resident_runtime import ResidentGoalSupervisor, erase_tree_contents, ResidentBlocked
from research_harness import goal_runtime
from research_harness.goal_runner import GoalRequest
from research_harness.manager_runtime import RecheckRejected
from research_harness.uid_supervisor import Identity, SupervisorBlocked
from research_harness.actions_inbox import Inbox, IntakeBlocked
from research_harness.schema import ContractError
from test_harness_uid_supervisor import FakeOS, NamespaceExit, worker
from test_harness_direct_pr import (DirectServiceTests, MultipleSources, direct_source,
    ISSUE, DirectRuntime)
from tests.test_harness_goal_deployment import launcher as common
from test_harness_actions_inbox import receipt
from test_harness_goal_runner import Transport, frames

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('resident_launcher_fixture', ROOT/'tools/research_resident/launcher.py')
resident = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, goal_launcher=common): spec.loader.exec_module(resident)


class ResidentOS(FakeOS):
    def __init__(self):
        super().__init__()
        self.ctx.update(pid=10, proc_self='10', nspid=(10,), parent_pid=1, subreaper=True)
        self.members.update({10: Identity(10, 110, (0,)*4, 'S'),
                             3: Identity(3, 103, (10001,)*4, 'S'),
                             4: Identity(4, 104, (10003,)*4, 'S')})
        self.children_empty = True
    def block_service(self): self.events.append(('blocked',))
    def admit_goal(self, request): self.events.append(('goal_admission_fixture',))
    def launch_goal(self,request):
        self.members[20]=worker(20)
        return SimpleNamespace(process=SimpleNamespace(pid=20,poll=lambda:None))


class ResidentSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.os = ResidentOS()
        self.supervisor = ResidentGoalSupervisor(self.os, controller_pid=3, runner_pid=4)

    def test_reserve_and_pidfd_cleanup_signal_only_worker(self):
        reservation = self.supervisor.reserve()
        self.os.members[20] = worker(20)
        self.os.members[21] = worker(21)  # Different session has the same UID.
        self.supervisor.stop_owned(reservation)
        signals = [e[1] for e in self.os.events if e[0]=='signal']
        self.assertEqual(signals, [20,21])
        self.assertEqual(set(self.os.members), {1,3,4,10})
        self.assertNotIn(('blocked',), self.os.events)

    def test_other_root_process_or_preexisting_worker_refuses_reservation(self):
        for uid in (0,10002,10001):
            with self.subTest(uid=uid):
                self.os.members[20] = Identity(20,120,(uid,)*4,'S')
                with self.assertRaises(SupervisorBlocked): self.supervisor.reserve()

    def test_actual_job_context_and_resource_bounds_are_not_consent_flags(self):
        for key,value in [('subreaper',False),('parent_pid',3),('threads',2),('cap_eff',0)]:
            saved=self.os.ctx[key];self.os.ctx[key]=value
            with self.subTest(key=key),self.assertRaises(SupervisorBlocked):self.supervisor.reserve()
            self.os.ctx[key]=saved
        self.os.limits['pids']=65
        with self.assertRaises(SupervisorBlocked):self.supervisor.reserve()

    def test_echild_uncertainty_latches_and_exits_job_not_success(self):
        reservation=self.supervisor.reserve(); self.os.children_empty=False
        with self.assertRaises(NamespaceExit):self.supervisor.stop_owned(reservation)
        self.assertIn(('blocked',),self.os.events)

    def test_trusted_pid_reuse_or_unknown_peer_latches(self):
        for mutate in (lambda: self.os.members.update({4:replace(self.os.members[4],start_ticks=999)}),
                       lambda: self.os.members.update({20:Identity(20,120,(9999,)*4,'S')})):
            self.setUp();reservation=self.supervisor.reserve();mutate()
            with self.assertRaises(NamespaceExit):self.supervisor.stop_owned(reservation)
            self.assertIn(('blocked',),self.os.events)

    def test_runner_state_changes_are_not_pid_reuse(self):
        reservation=self.supervisor.reserve()
        self.os.members[4]=replace(self.os.members[4],state='R')
        self.supervisor.stop_owned(reservation)

    def test_resident_failure_exports_only_fixed_reason_and_numeric_exit(self):
        private=ResidentBlocked('arbitrary remote error',child_exit='private')
        self.assertEqual(private.code,'CLEANUP_UNVERIFIED');self.assertIsNone(private.child_exit)
        actual=ResidentBlocked('JOB_EXITED_UNCLEAN',child_exit=124)
        self.assertEqual(actual.code,'JOB_EXITED_UNCLEAN');self.assertEqual(actual.child_exit,124)


class ResidentRealLinuxTests(unittest.TestCase):
    def test_subreaper_adopts_double_fork_setsid_and_requires_echild(self):
        script = '''
import os, signal, time
from research_harness.resident_runtime import install_subreaper, reap_children
install_subreaper()
r,w=os.pipe()
pid=os.fork()
if pid==0:
    os.close(r);os.setsid()
    if os.fork(): os._exit(0)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    os.write(w,str(os.getpid()).encode()+b'\\n');os.close(w)
    time.sleep(4);os._exit(0)
os.close(w)
with os.fdopen(r,'rb') as stream: descendant=int(stream.readline())
try:
    assert not reap_children(), 'live escaped descendant was called empty'
finally:
    os.kill(descendant,signal.SIGKILL)
until=time.monotonic()+3
while not reap_children():
    assert time.monotonic()<until
    time.sleep(.01)
print('ECHILD')
'''
        done=subprocess.run([sys.executable,'-B','-c',script],cwd=ROOT,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)
        self.assertEqual(done.returncode,0,done.stderr.decode())
        self.assertEqual(done.stdout,b'ECHILD\n')

    def test_real_fd_cleanup_unlinks_symlinks_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'work';root.mkdir(); outside=Path(tmp)/'outside';outside.write_bytes(b'keep')
            (root/'nested').mkdir();(root/'nested'/'auth.json').write_bytes(b'fixture-not-auth')
            (root/'link').symlink_to(outside);(root/'fifo').touch()
            os.mkfifo(root/'pipe')
            erase_tree_contents(root,switch_uid=lambda uid:self.assertEqual(uid,os.getuid()),
                root_uid=os.getuid(),worker_uid=os.getuid())
            self.assertEqual(list(root.iterdir()),[]);self.assertEqual(outside.read_bytes(),b'keep')

    def test_cleanup_budget_refuses_success_and_restores_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'file').write_bytes(b'x');switches=[]
            with self.assertRaises(SupervisorBlocked):
                erase_tree_contents(root,switch_uid=switches.append,root_uid=os.getuid(),
                    worker_uid=os.getuid(),maximum_entries=0)
            self.assertTrue((root/'file').exists());self.assertEqual(switches[-1],os.getuid())


class ResidentProtocolLifecycleTests(unittest.TestCase):
    def run_fixture(self, mode):
        system=ResidentOS()
        supervisor=ResidentGoalSupervisor(system,controller_pid=3,runner_pid=4)
        request=GoalRequest(direct_source().request['plan'],'b'*40)
        binding=goal_runtime.GoalRunBinding(request.source_sha,request.digest,'request-1','run-1')
        values=frames()
        for frame in values:
            if frame.get('method')=='item/completed' and frame['params']['item'].get('type')=='agentMessage':
                frame['params']['item']['text']=json.dumps(dict(outcome='completed',summary='fixture',pr_url=None))
            if frame.get('method')=='turn/completed':
                frame['params']['turn']['items'][0]['text']=json.dumps(dict(outcome='completed',summary='fixture',pr_url=None))
        transport=Transport(values);transport.close=lambda:None
        def recheck():
            if system.released and mode=='cancel':raise RecheckRejected('revoked')
        receive=transport.receive
        def observed():
            if mode=='timeout':system.t=121
            if mode=='protocol':return {'id':999,'result':{}}
            return receive()
        transport.receive=observed
        with patch.object(goal_runtime,'BoundedStdioTransport',return_value=transport):
            if mode=='normal':
                value=supervisor.run_goal(request,recheck,binding=binding,quarantine=lambda _:None,enabled=True)
                self.assertEqual(value.candidate.outcome,'completed')
            else:
                with self.assertRaises(ContractError):
                    supervisor.run_goal(request,recheck,binding=binding,quarantine=lambda _:None,enabled=True)
        self.assertNotIn(20,system.members)
        self.assertEqual(set(system.members),{1,3,4,10})
        self.assertNotIn(('blocked',),system.events)
        self.assertIn(('close_child',),system.events)
        self.assertFalse(any(e[0]=='disarm' for e in system.events))

    def test_normal_formal_app_server_frames_use_common_runner(self):self.run_fixture('normal')
    def test_protocol_failure_stops_only_owned_worker(self):self.run_fixture('protocol')
    def test_real_approval_callback_cancellation_stops_only_owned_worker(self):self.run_fixture('cancel')
    def test_deadline_stops_only_owned_worker(self):self.run_fixture('timeout')


class ResidentDeliveryServiceTests(unittest.TestCase):
    # Reuse public approval fixtures and actual ServiceState, not a stub that
    # unconditionally approves. DirectRuntime alone replaces model transport.
    setUp=DirectServiceTests.setUp
    make=DirectServiceTests.make

    def event(self, number, run):
        inbox=Inbox(self.root/'events.sqlite')
        inbox.enqueue(receipt(run=run,issue=number)); item=inbox.take()
        def finish():
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp);(path/'worker-auth').write_bytes(b'dummy')
                erase_tree_contents(path,switch_uid=lambda _:None,root_uid=os.getuid(),worker_uid=os.getuid())
                self.assertEqual(list(path.iterdir()),[])
        answer=resident.process_event(self.controller,item,finish_runtime=finish)
        inbox.finish(item['receipt_id'],answer)
        return answer

    def test_two_distinct_approved_events_same_epoch_then_duplicate(self):
        second=direct_source(ISSUE+1)
        second.request['plan']['goal']='Second independently approved CPU task.'
        second.render(refresh_receipt=True)
        self.issues=MultipleSources(self.source,second);self.make()
        original_epoch=deepcopy(self.runtime.epoch)
        for index,number in enumerate((ISSUE,ISSUE+1),1):
            answer=self.event(number,index)
            self.assertEqual(answer['phase'],'WORKER_FINISHED')
            self.assertTrue(answer['dispatched'])
        self.assertEqual(self.runtime.epoch,original_epoch)
        self.assertEqual(self.runtime.runs,2)
        self.assertFalse(self.event(ISSUE,3)['dispatched']);self.assertEqual(self.runtime.runs,2)

    def test_unapproved_event_creates_no_claim_later_new_approval_can_dispatch(self):
        self.source.approval_present=False
        self.runtime.failed_issue=None
        self.assertFalse(self.event(ISSUE,1)['dispatched'])
        self.assertIsNone(self.state.get(ISSUE));self.assertEqual(self.runtime.runs,0)
        self.source.approval_present=True
        self.assertTrue(self.event(ISSUE,2)['dispatched'])

    def test_failure_blocks_existing_claim_and_later_event_does_not_rerun(self):
        self.runtime.failed_issue=None
        self.runtime.run_error=ContractError('fixture transport failure')
        self.assertEqual(self.event(ISSUE,1)['phase'],'BLOCKED')
        self.runtime.run_error=None
        self.assertEqual(self.event(ISSUE,2)['phase'],'BLOCKED')
        self.assertEqual(self.runtime.runs,1)

    def test_no_issue_discovery_and_no_normal_namespace_exit(self):
        source=(ROOT/'tools/research_resident/launcher.py').read_text()
        for forbidden in ('list_candidates(','.once(',"docker restart",'finish_allocation('):
            self.assertNotIn(forbidden,source)


del DirectServiceTests  # unittest must not rediscover the imported fixture class.

if __name__=='__main__':unittest.main()
