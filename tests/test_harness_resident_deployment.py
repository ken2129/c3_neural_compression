"""Validate recorded Docker facts, not a substitute for a real Docker test."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from research_harness.resident_deployment import validate_resident_deployment
from research_harness.goal_deployment import validate_goal_deployment
from research_harness.schema import ContractError
from tests.test_harness_goal_deployment import goal_inspect, profile_fixture
from test_harness_direct_pr import direct_plan
from test_harness_actions_inbox import policy

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('resident_prepare_fixture',ROOT/'tools/research_resident/prepare.py')
prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)


def facts():
    data=goal_inspect();ident=data['Id'];volume='fresh-resident_goal-control';runner='fresh-resident_runner-state'
    data['Config']['Labels']['c3.purpose']='dedicated-resident-controller'
    data['Config']['Entrypoint']=['python','-I','-B','/opt/service/resident_launcher.py']
    data['HostConfig']['Tmpfs']['/control-ipc']='rw,noexec,nosuid,nodev,size=1048576,mode=0711,uid=10001,gid=10001'
    volumes={}
    data['Mounts']=[]
    for key,name,destination in [('control',volume,'/control'),('runner',runner,'/runner')]:
        source='/var/lib/docker/volumes/'+name+'/_data'
        volumes[key]=dict(Name=name,Driver='local',Scope='local',Options=None,Mountpoint=source,
            Labels={'c3.deployment-id':'1'*32})
        data['Mounts'].append(dict(Type='volume',Name=name,Destination=destination,Source=source,RW=True))
    profile=profile_fixture()
    return data,dict(consumers={'control':[ident],'runner':[ident]},volume_name=volume,volume_inspect=volumes,
        runtime_source_sha=profile['runtime_source_sha'],input_source_sha=profile['input_source_sha'],
        resource_limits=profile['resource_limits'],deployment_id=profile['deployment_id'])


class ResidentDeploymentTests(unittest.TestCase):
    def test_exact_two_private_volumes_and_immutable_entrypoint(self):
        data,args=facts();before=deepcopy((data,args))
        result=validate_resident_deployment(data,**args)
        self.assertEqual(result['container_id'],data['Id']);self.assertEqual((data,args),before)
        with self.assertRaises(ContractError):validate_goal_deployment(data,**args)

    def test_each_volume_exclusivity_and_real_mountpoint_required(self):
        for key in ('control','runner'):
            data,args=facts();args['consumers'][key].append('f'*64)
            with self.subTest(key=key),self.assertRaises(ContractError):validate_resident_deployment(data,**args)
            data,args=facts();args['volume_inspect'][key]['Mountpoint']='/different'
            with self.assertRaises(ContractError):validate_resident_deployment(data,**args)

    def test_bind_gpu_host_pid_capability_and_root_write_refused(self):
        for key,value in [('Binds',['/tmp:/runner']),('Devices',[{}]),('DeviceRequests',[{}]),
                          ('PidMode','host'),('IpcMode','host'),('CapAdd',['SYS_ADMIN']),
                          ('ReadonlyRootfs',False),('PidsLimit',65),('NetworkMode','host'),('Init',True)]:
            data,args=facts();data['HostConfig'][key]=value
            with self.subTest(key=key),self.assertRaises(ContractError):validate_resident_deployment(data,**args)

    def test_installer_requires_initial_intake_off_and_full_risk_consents(self):
        profile=profile_fixture();plan=direct_plan()
        profile.update(input_source_sha=plan['source_sha'],model=plan['model'])
        config=policy();config['enabled']=False
        prepare.validate_resident_profile(profile,plan,config)
        config['enabled']=True
        with self.assertRaises(ContractError):prepare.validate_resident_profile(profile,plan,config)
        config['enabled']=False;profile['risk_acknowledgements']['arbitrary_cpu_commands']=False
        with self.assertRaises(ContractError):prepare.validate_resident_profile(profile,plan,config)

    def test_installer_rejects_missing_approval_before_any_docker_or_auth(self):
        args=[]
        for key in ('project','profile','plan','intake-policy','codex-auth-file','github-hosts-file',
                    'worker-pr-config','worker-github-token-file','runner-version','runner-sha256'):
            args.extend(['--'+key,'unused'])
        with patch.object(prepare.common,'run') as run,patch.object(prepare.common,'read_credential') as auth:
            with self.assertRaises(ContractError):prepare.main(args)
        run.assert_not_called();auth.assert_not_called()

    def test_installer_never_starts_registers_or_changes_legacy_service(self):
        source=(ROOT/'tools/research_resident/prepare.py').read_text()
        for forbidden in ("['docker','start'", "['docker','stop'", "['docker','update'", "'--token'"):
            self.assertNotIn(forbidden,source)
        self.assertIn("'create','--build','controller'",source)
        self.assertIn('common.copy_records(',source)
        dockerfile=(ROOT/'tools/research_resident/Dockerfile').read_text()
        self.assertIn('sha256sum --check --strict',dockerfile)
        self.assertNotIn('docker.sock',dockerfile)


if __name__=='__main__':unittest.main()
