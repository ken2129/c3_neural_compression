"""Offline diagnostic contract checks; no Docker/model/credentials are used."""
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest

from research_harness.resident_deployment import validate_resident_deployment,validate_diagnostic_deployment
from research_harness.resident_diagnostic_fixture import A,B,LocalSources,LocalWorkerAPI,DUMMY_TOKEN,plan
from research_harness.owner_approval import inspect_once,RevokedApproval
from research_harness.service_state import ServiceState,initialize
from research_harness.schema import ContractError
from test_harness_resident_deployment import facts


class DiagnosticContractTests(unittest.TestCase):
    def diagnostic_facts(self):
        data,args=facts()
        data['Config']['Labels']['c3.purpose']='dedicated-resident-diagnostic'
        data['Config']['Entrypoint']=['python','-I','-B','/opt/service/resident_diagnostic.py']
        data['HostConfig']['RestartPolicy']['Name']='no'
        data['AppArmorProfile']='docker-default'
        return data,args

    def test_diagnostic_exact_identity_does_not_admit_production_or_mutate_inspect(self):
        data,args=self.diagnostic_facts();before=deepcopy(data)
        self.assertEqual(validate_diagnostic_deployment(data,**args)['restart_policy'],'no')
        self.assertEqual(data,before)
        with self.assertRaises(ContractError):validate_resident_deployment(data,**args)
        original,args=facts()
        with self.assertRaises(ContractError):validate_diagnostic_deployment(original,**args)

    def test_diagnostic_still_rejects_restart_caps_and_apparmor_relaxation(self):
        for field,value in [('RestartPolicy',{'Name':'always'}),('CapAdd',['SYS_ADMIN']),('ReadonlyRootfs',False)]:
            data,args=self.diagnostic_facts();data['HostConfig'][field]=value
            with self.subTest(field=field),self.assertRaises(ContractError):validate_diagnostic_deployment(data,**args)
        data,args=self.diagnostic_facts();data['AppArmorProfile']='unconfined'
        with self.assertRaises(ContractError):validate_diagnostic_deployment(data,**args)

    def test_actual_formal_verifier_accepts_synthetic_then_rejects_A_cancellation_only(self):
        present=False;source=LocalSources('1'*32,worker_present=lambda:present)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o700);initialize(root)
            state=ServiceState(root)
            try:
                inspect_once(source,source,state.intake,A,time.time)
                present=True
                with self.assertRaises(RevokedApproval):inspect_once(source,source,state.intake,A,time.time)
                inspect_once(source,source,state.intake,B,time.time)
            finally:state.close()

    def test_only_dummy_actor_lookup_is_possible_never_an_api_write(self):
        api=LocalWorkerAPI(DUMMY_TOKEN.decode())
        self.assertEqual(api.request('GET','/user')['login'],'resident-diag-worker')
        for method,path in [('POST','/pulls'),('GET','/repos/ken2129/c3_neural_compression/issues'),('DELETE','/user')]:
            with self.assertRaises(ContractError):api.request(method,path)
        with self.assertRaises(ContractError):LocalWorkerAPI('not-the-fixed-dummy')

    def test_non_fixture_issue_and_scanning_are_forbidden(self):
        source=LocalSources('1'*32,worker_present=lambda:False)
        with self.assertRaises(ContractError):source.get_issue(83)
        with self.assertRaises(ContractError):source.list_candidates()


if __name__=='__main__':unittest.main()
