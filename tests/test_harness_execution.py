"""Local execution contracts and Docker policy; no daemon, credentials or GPU."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import Mock,patch

from research_harness.execution_contract import installed_container,validate_containers,execution_limits
from research_harness.execution_docker import LocalDocker,validate_container
from research_harness.goal_plan import validate_goal_plan,require_installed_goal,direct_pr_scope
from research_harness.goal_runner import GoalRequest,goal_prompt,GoalAppServerRunner
from research_harness.schema import ContractError
from test_harness_direct_pr import direct_plan,direct_profile,frames,Transport,SOURCE_SHA


def target():
    return dict(name='research-work',container_id='c'*64,create_image=None,python='/usr/local/bin/python',
        codex='/opt/codex/bin/codex',worker_uid=10002,workspace='/work/goal',
        resource_limits=dict(max_seconds=600,**direct_plan()['resource_budget']),cpu_cores=1,
        gpu_uuids=[],max_wait_seconds=0,sampled_limits_accepted=True)


def configured():
    plan=direct_plan();plan['execution_container']='c3-work'
    profile=direct_profile(plan);profile['direct_pr_scope']=direct_pr_scope(plan);profile.update(execution_containers={'c3-work':target()},docker_control_approved=True)
    return plan,profile


def inspect_fixture(spec=None):
    value=target() if spec is None else spec
    return dict(Id='c'*64,Name='/'+value['name'],Image='sha256:'+'d'*64,
        Config=dict(Labels={}),State=dict(Running=True),Mounts=[],
        HostConfig=dict(Privileged=False,PidMode='',IpcMode='private',NetworkMode='bridge',
            Devices=[],DeviceCgroupRules=[],CapAdd=[],Memory=1024**3,MemorySwap=1024**3,PidsLimit=64,NanoCpus=10**9))


class ExecutionContractTests(unittest.TestCase):
    def test_issue_selects_only_registered_container_alias(self):
        plan,profile=configured();validate_goal_plan(plan,SOURCE_SHA)
        require_installed_goal(profile,plan)
        self.assertEqual(installed_container(profile,plan)['container_id'],'c'*64)
        for change in ('unknown','../other','--privileged','c3-work;docker stop other'):
            with self.subTest(value=change),self.assertRaises(ContractError):
                installed_container(profile,dict(plan,execution_container=change))

    def test_explicit_docker_opt_in_and_closed_operator_spec(self):
        plan,profile=configured();profile['docker_control_approved']=False
        with self.assertRaises(ContractError):installed_container(profile,plan)
        for key,value in (('privileged',True),('worker_uid',0),('workspace','/var/lib/c3-execution'),
                          ('environment',{'DOCKER_HOST':'anything'}),('container_id','other'),
                          ('sampled_limits_accepted',False)):
            spec=dict(target(),**{key:value})
            with self.subTest(key=key),self.assertRaises(ContractError):validate_containers({'c3-work':spec})

    def test_native_budget_can_exceed_manager_budget_without_changing_cpu_path(self):
        plan,profile=configured();before=deepcopy(profile['resource_limits'])
        profile['execution_containers']['c3-work']['resource_limits']['memory_bytes']=8*1024**3
        self.assertEqual(execution_limits(profile,plan)['memory_bytes'],8*1024**3)
        self.assertEqual(profile['resource_limits'],before)
        self.assertEqual(execution_limits(profile,direct_plan()),before)

    def test_legacy_recipe_schema_is_rejected(self):
        plan=direct_plan();plan['gpu_experiment']={'machine':'leo','recipe':'fixed'}
        with self.assertRaises(ContractError):validate_goal_plan(plan,SOURCE_SHA)

    def test_native_prompt_and_one_session_allow_failure_repair_and_pr(self):
        plan,_=configured();text=goal_prompt(plan)
        self.assertIn('normal shell and file tools',text)
        self.assertIn('same session',text);self.assertNotIn('run_experiment.py',text)
        values=frames()
        for frame in values:
            if frame.get('method')=='item/completed' and frame['params']['item']['type']=='agentMessage':
                value=json.loads(frame['params']['item']['text']);value['pr_url']='https://github.com/ken2129/c3_neural_compression/pull/123'
                frame['params']['item']['text']=json.dumps(value)
            if frame.get('method')=='turn/completed':
                frame['params']['turn']['items'][0]['text']=json.dumps(dict(outcome='completed',summary='done',pr_url='https://github.com/ken2129/c3_neural_compression/pull/123'))
        # Use equal final item text to keep the existing final-snapshot contract.
        final=next(f['params']['item']['text'] for f in values if f.get('method')=='item/completed' and f['params']['item']['type']=='agentMessage')
        values[-1]['params']['turn']['items'][0]['text']=final
        transport=Transport(values)
        result=GoalAppServerRunner(enabled=True).run(GoalRequest(plan,SOURCE_SHA),transport,
            admit=lambda request:None,recheck=lambda:None)
        self.assertEqual(result.outcome,'completed')
        self.assertEqual([f.get('method') for f in transport.sent].count('initialize'),1)
        self.assertEqual([f.get('method') for f in transport.sent].count('turn/start'),1)

    def test_research_workspace_is_operator_selected_not_in_issue(self):
        plan,_=configured()
        request=GoalRequest(plan,SOURCE_SHA,workspace='/research/project',publication_helper='/opt/runtime/worker_pr.py')
        request.validated_plan()
        with self.assertRaises(ContractError):GoalRequest(direct_plan(),SOURCE_SHA,workspace='/research/project').validated_plan()


class DockerPolicyTests(unittest.TestCase):
    @unittest.skipUnless(os.getuid()==0,'bootstrap ownership test runs in the isolated Linux container')
    def test_bootstrap_archive_is_public_immutable_and_versioned_by_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';(source/'research_harness').mkdir(parents=True)
            for path in (source/'research_harness/module.py',source/'execution_worker.py',source/'worker_pr.py'):
                path.write_text('# public immutable fixture\n');path.chmod(0o644)
            inputs=root/'inputs';inputs.mkdir();(inputs/'input.txt').write_text('public')
            manifest=root/'manifest.json';manifest.write_text(json.dumps(dict(source_sha='a'*40,
                files={'input.txt':{'size_bytes':6,'sha256':hashlib.sha256(b'public').hexdigest()}})))
            copied=[]
            def run(argv,**kwargs):
                if 'cp' in argv:copied.append(kwargs['input'])
                return subprocess.CompletedProcess(argv,0,b'INSTALL\n' if 'exec' in argv else b'')
            engine=LocalDocker(run=run)
            path=engine.install('c'*64,'/usr/local/bin/python','b'*40,
                source_root=source,input_root=inputs,manifest_path=manifest)
            self.assertTrue(path.startswith('/opt/c3-execution/'+'b'*40+'-'))
            with tarfile.open(fileobj=io.BytesIO(copied[0])) as archive:
                entries=archive.getmembers();self.assertTrue(all(m.uid==0 and m.mode==0o444 for m in entries))
                self.assertEqual({m.name for m in entries},{'research_harness/module.py','execution_worker.py',
                    'worker_pr.py','goal-inputs.json','goal-inputs/input.txt'})
            manifest.write_text(manifest.read_text()+'\n')
            other=engine.install('c'*64,'/usr/local/bin/python','b'*40,
                source_root=source,input_root=inputs,manifest_path=manifest)
            self.assertNotEqual(path,other)

    def test_exact_identity_and_host_access_rejection(self):
        spec=target();data=inspect_fixture();self.assertEqual(validate_container(data,spec),'c'*64)
        for key,value in (('Privileged',True),('PidMode','host'),('PidMode','container:other'),
                          ('IpcMode','host'),('NetworkMode','host'),('NetworkMode','container:other'),
                          ('Devices',[{}]),('CapAdd',['SYS_ADMIN'])):
            altered=deepcopy(data);altered['HostConfig'][key]=value
            with self.subTest(key=key),self.assertRaises(ContractError):validate_container(altered,spec)
        for mutation in ({'Id':'e'*64},{'Name':'/other'},
                         {'Mounts':[dict(Type='bind',Destination='/anything')]}):
            with self.subTest(mutation=mutation),self.assertRaises(ContractError):validate_container(dict(data,**mutation),spec)

    def test_reuse_never_creates_or_changes_unrelated_container(self):
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv,0,json.dumps([inspect_fixture()]).encode())
        engine=LocalDocker(run=run)
        self.assertEqual(engine.acquire(target(),'c3-work','1'*32),'c'*64)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][-1],'c'*64)

    def test_creation_uses_only_pinned_image_and_fixed_safe_options(self):
        spec=dict(target(),container_id=None,create_image='sha256:'+'d'*64,sampled_limits_accepted=False)
        data=inspect_fixture(spec);data['Config']['Labels']={'c3.execution-deployment':'1'*32,'c3.execution-alias':'c3-work'}
        calls=[]
        def run(argv,**kwargs):
            calls.append(argv)
            if 'inspect' in argv and len(calls)==1:return subprocess.CompletedProcess(argv,1,b'')
            if 'image' in argv:return subprocess.CompletedProcess(argv,0,json.dumps([dict(Id=spec['create_image'])]).encode())
            return subprocess.CompletedProcess(argv,0,json.dumps([data]).encode() if 'inspect' in argv else b'c'*64)
        engine=LocalDocker(run=run);self.assertEqual(engine.acquire(spec,'c3-work','1'*32),'c'*64)
        create=next(c for c in calls if 'create' in c)
        self.assertIn(spec['create_image'],create);self.assertIn('no-new-privileges:true',create)
        self.assertIn('--pull=never',create)
        self.assertNotIn('--privileged',create);self.assertNotIn('--volume',create)
        self.assertNotIn('--pid',create)

    def test_name_collision_is_rejected_instead_of_adopted(self):
        spec=dict(target(),container_id=None,create_image='sha256:'+'d'*64)
        engine=LocalDocker(run=lambda argv,**kwargs:subprocess.CompletedProcess(argv,0,json.dumps([inspect_fixture(spec)]).encode()))
        with self.assertRaises(ContractError):engine.acquire(spec,'c3-work','1'*32)

if __name__=='__main__':unittest.main()
