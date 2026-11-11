"""Installation is opt-in, OFF, and permits only the exact local Docker socket."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import stat
from research_harness.execution_docker import validate_docker_authority

from research_harness.resident_deployment import validate_resident_deployment
from research_harness.schema import ContractError
from test_harness_execution import configured
from test_harness_resident_deployment import facts,prepare
from test_harness_actions_inbox import policy
from tests.test_harness_goal_deployment import launcher
import test_harness_resident as resident_fixture
from test_harness_restart_boundary import epoch
import io

class ExecutionSetupTests(unittest.TestCase):
    def test_socket_permission_guard_rejects_any_worker_or_runner_group(self):
        binary=SimpleNamespace(st_mode=stat.S_IFREG|0o755,st_uid=0)
        for gid,mode in ((999,0o660),(10001,0o660),(10002,0o660),(10003,0o660),(999,0o666)):
            socket=SimpleNamespace(st_mode=stat.S_IFSOCK|mode,st_uid=0,st_gid=gid)
            with patch('research_harness.execution_docker.os.geteuid',return_value=0),\
                    patch('research_harness.execution_docker.Path.lstat',return_value=socket),\
                    patch('research_harness.execution_docker.Path.stat',return_value=binary):
                if gid==999 and mode==0o660:validate_docker_authority()
                else:
                    with self.assertRaises(ContractError):validate_docker_authority()

    def socket_facts(self):
        data,args=facts()
        data['Mounts'].append(dict(Type='bind',Source='/var/run/docker.sock',Destination='/run/c3-docker.sock',RW=True))
        data['HostConfig']['Binds']=[args['volume_name']+':/control',args['volume_name'].replace('_goal-control','_runner-state')+':/runner',
            '/var/run/docker.sock:/run/c3-docker.sock']
        return data,args

    def test_socket_overlay_requires_opt_in_and_exact_source_destination(self):
        data,args=self.socket_facts()
        validate_resident_deployment(data,**args,docker_control=True)
        with self.assertRaises(ContractError):validate_resident_deployment(data,**args)
        for change in ({'Source':'/other.sock'},{'Destination':'/var/run/docker.sock'},{'RW':False}):
            altered=deepcopy(data);altered['Mounts'][-1].update(change)
            with self.subTest(change=change),self.assertRaises(ContractError):
                validate_resident_deployment(altered,**args,docker_control=True)

    def test_legacy_install_rejects_execution_and_resident_keeps_intake_off(self):
        plan,profile=configured()
        profile.pop('direct_pr_scope')
        with self.assertRaises(ContractError):prepare.common.validate_profile(profile,plan)
        intake=policy();intake['enabled']=False
        prepare.validate_resident_profile(profile,plan,intake)
        installed=prepare.common.installed_profile(profile,plan,allow_execution=True)
        self.assertIn('execution_containers',installed)
        intake['enabled']=True
        with self.assertRaises(ContractError):prepare.validate_resident_profile(profile,plan,intake)

    def test_docker_authority_flag_is_required_before_freshness_or_credentials(self):
        plan,profile=configured();intake=policy();intake['enabled']=False
        profile.pop('direct_pr_scope')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,value in (('profile',profile),('plan',plan),('intake-policy',intake)):
                (root/name).write_text(json.dumps(value))
            args=['--approve-new-resident-deployment','--approve-worker-github-access']
            values=dict(project='c3-execution-offline',profile=str(root/'profile'),plan=str(root/'plan'),
                **{'intake-policy':str(root/'intake-policy'),'runner-version':'2.338.1','runner-sha256':'a'*64})
            for name in ('codex-auth-file','github-hosts-file','worker-pr-config','worker-github-token-file'):
                values[name]='unused'
            for key,value in values.items():args.extend(['--'+key,value])
            with patch.object(prepare.common,'require_fresh_deployment_id') as fresh,patch.object(prepare.common,'read_credential') as auth:
                with self.assertRaisesRegex(ContractError,'local Docker control'):prepare.main(args)
                fresh.assert_not_called();auth.assert_not_called()

    def test_disposable_runtime_refuses_native_execution_before_prepare(self):
        plan,profile=configured();outgoing=io.StringIO()
        runtime=launcher.GoalPipeRuntime(io.StringIO(),outgoing,epoch(),profile)
        with self.assertRaises(ContractError):runtime.prepare_plan(plan)
        self.assertEqual(outgoing.getvalue(),'')

    def test_resident_dispatches_native_budget_without_cpu_workspace_staging(self):
        plan,profile=configured()
        profile.pop('direct_pr_scope')
        plan['resource_budget']['memory_bytes']=2*1024**3
        profile['execution_containers']['c3-work']['resource_limits']['memory_bytes']=2*1024**3
        installed=prepare.common.installed_profile(profile,plan,allow_execution=True)
        incoming=io.StringIO('{"prepared":true}\n');outgoing=io.StringIO()
        runtime=resident_fixture.resident.ResidentGoalPipeRuntime(incoming,outgoing,epoch(),installed)
        runtime.prepare_plan(plan)
        frame=json.loads(outgoing.getvalue())
        self.assertEqual(frame['plan']['resource_budget']['memory_bytes'],2*1024**3)
        self.assertEqual(installed['resource_limits']['memory_bytes'],1024**3)

if __name__=='__main__':unittest.main()
