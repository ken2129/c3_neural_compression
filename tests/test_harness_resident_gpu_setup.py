"""Installer opt-in and private SSH-material contract; no credential transfer."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from research_harness.goal_plan import require_installed_goal, validate_goal_plan
from research_harness.goal_runner import goal_prompt
from research_harness.schema import ContractError, canonical
from research_harness.gpu_contract import PENDING
import test_harness_direct_pr as direct_fixture
from test_harness_direct_pr import direct_plan, direct_profile, prepare, SOURCE_SHA
from test_harness_gpu import request, target_profile, result, binding
from test_harness_resident_deployment import prepare as resident_prepare


class GPUSetupTests(unittest.TestCase):
    def configured(self):
        plan=direct_plan();plan['gpu_experiment']=request()
        profile=direct_profile(plan);profile.update(target_profile())
        target=profile['gpu_targets']['fixture']
        target['identity_file']='/control/gpu-fixture.key'
        target['known_hosts_file']='/control/gpu-fixture.known-hosts'
        return plan,profile

    def test_legacy_install_does_not_silently_enable_gpu_transport(self):
        plan,profile=self.configured()
        with self.assertRaises(ContractError):prepare.validate_profile(profile,plan)
        checked=prepare.validate_profile(profile,plan,allow_gpu=True)
        installed=prepare.installed_profile(profile,plan,allow_gpu=True)
        require_installed_goal(installed,checked)
        self.assertIn('gpu_targets',installed)

    def test_cpu_plan_and_resource_budget_are_unchanged(self):
        plan,profile=self.configured()
        checked=validate_goal_plan(plan,SOURCE_SHA)
        original=direct_plan()
        self.assertEqual(checked['resource_budget'],original['resource_budget'])
        self.assertEqual(checked['max_seconds'],original['max_seconds'])
        self.assertNotIn('gpu_targets',prepare.profile_template())

    def test_gpu_worker_prompt_reads_data_without_remote_credentials_or_rerun(self):
        plan,profile=self.configured();text=goal_prompt(plan)
        self.assertIn('gpu-result.json',text)
        self.assertIn('already stopped',text)
        self.assertIn('Do not restart',text)
        self.assertNotIn(profile['gpu_targets']['fixture']['identity_file'],text)

    def test_only_explicit_registered_flat_ssh_records_are_read(self):
        plan,profile=self.configured()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);key=root/'key';known=root/'known';mapping=root/'mapping'
            key.write_text('offline fixture key');known.write_text('offline fixture known host')
            key.chmod(0o600);known.chmod(0o600)
            value={'/control/gpu-fixture.key':str(key),'/control/gpu-fixture.known-hosts':str(known)}
            mapping.write_text(json.dumps(value));mapping.chmod(0o600)
            records=resident_prepare.gpu_ssh_records(profile,str(mapping))
            self.assertEqual(set(records),{'gpu-fixture.key','gpu-fixture.known-hosts'})
            value['/control/goal-runtime.json']=str(key);mapping.write_text(json.dumps(value))
            with self.assertRaises(ContractError):resident_prepare.gpu_ssh_records(profile,str(mapping))

    def test_no_implicit_credential_discovery_or_extra_material(self):
        plan,profile=self.configured()
        with self.assertRaises(ContractError):resident_prepare.gpu_ssh_records(profile,None)
        with self.assertRaises(ContractError):resident_prepare.gpu_ssh_records({},'unrequested-file')
        self.assertEqual(resident_prepare.gpu_ssh_records({},None),{})

    def test_nonresident_runtime_refuses_gpu_before_sending_prepare(self):
        from tests.test_harness_goal_deployment import launcher
        from test_harness_restart_boundary import epoch
        plan,profile=self.configured()
        installed=prepare.installed_profile(profile,plan,allow_gpu=True)
        outgoing=io.StringIO()
        runtime=launcher.GoalPipeRuntime(io.StringIO(),outgoing,epoch(),installed)
        with self.assertRaisesRegex(ContractError,'resident runtime'):runtime.prepare_plan(plan)
        self.assertEqual(outgoing.getvalue(),'')


class GPUWorkerHomeTests(unittest.TestCase):
    setUp=direct_fixture.DirectWorkerHomeTests.setUp
    validate=direct_fixture.DirectWorkerHomeTests.validate

    def prepare(self):
        self.plan['gpu_experiment']=request()
        self.records['approved-plan.json']=canonical(self.plan)
        self.records['gpu-result.json']=canonical(PENDING)

    def test_pending_slot_is_present_before_preflight_without_exposing_manager_files(self):
        self.prepare();self.assertIsNone(self.validate())
        self.assertEqual(self.opened[-1][0],'/work/worker-home/gpu-result.json')

    def test_completed_data_must_match_exact_approved_plan(self):
        self.prepare();value=result()
        value['binding']['plan_digest']=hashlib.sha256(canonical(self.plan)).hexdigest()
        self.records['gpu-result.json']=canonical(value)
        self.assertIsNone(self.validate())
        value['binding']['plan_digest']='0'*64;self.records['gpu-result.json']=canonical(value)
        with self.assertRaisesRegex(ContractError,'plan binding'):self.validate()

    def test_cpu_only_worker_still_rejects_gpu_slot(self):
        self.records['gpu-result.json']=canonical(PENDING)
        with self.assertRaises(ContractError):self.validate()


if __name__=='__main__':unittest.main()
