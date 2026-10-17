import copy
import json
import unittest
from unittest.mock import patch
from research_harness.model_deployment import validate_model_config,prepare_model,validate_chatgpt_auth
from research_harness.schema import ContractError

class ModelDeploymentTests(unittest.TestCase):
    def config(self):
        return dict(source_sha='a'*40,model='gpt-5.3-codex',codex_version='0.160.0',
          sandbox_mode='externalSandbox',external_network='enabled',
          external_boundary_approved=True,worker_auth_exposure_acknowledged=True,
          usage_without_hard_cost_cap_approved=True)
    def test_explicit_configuration_builds_only_fixed_request(self):
        request=validate_model_config(self.config(),source_sha='a'*40,model='gpt-5.3-codex')
        self.assertEqual(request.expected_version,'0.160.0')
        self.assertEqual(request.cwd,'/work/canary')
    def test_every_consent_is_strict(self):
        for key in ('external_boundary_approved','worker_auth_exposure_acknowledged','usage_without_hard_cost_cap_approved'):
            for value in (False,1,None):
                config=self.config();config[key]=value
                with self.assertRaises(ContractError):validate_model_config(config,source_sha='a'*40,model='gpt-5.3-codex')
    def test_network_and_model_are_not_silently_translated(self):
        for key,value in (('external_network','restricted'),('sandbox_mode','workspace-write'),('model','other'),('source_sha','b'*40),('codex_version','0.159.2')):
            config=self.config();config[key]=value
            with self.assertRaises(ContractError):validate_model_config(config,source_sha='a'*40,model='gpt-5.3-codex')
    def test_extra_configuration_cannot_inject_commands(self):
        config=self.config();config['command']='anything'
        with self.assertRaises(ContractError):validate_model_config(config,source_sha='a'*40,model='gpt-5.3-codex')
    def test_auth_shape_requires_every_nonempty_token_without_disclosure(self):
        auth={'auth_mode':'chatgpt','tokens':dict(access_token='dummy-access',refresh_token='dummy-refresh',id_token='dummy-id',account_id='dummy-account')}
        validate_chatgpt_auth(json.dumps(auth).encode())
        for name in auth['tokens']:
            for value in ('',None,1):
                bad=copy.deepcopy(auth);bad['tokens'][name]=value
                with self.assertRaisesRegex(ContractError,'^MODEL_DEDICATED_CHATGPT_LOGIN_REQUIRED$'):
                    validate_chatgpt_auth(json.dumps(bad).encode())
        auth['tokens']={}
        with self.assertRaises(ContractError):validate_chatgpt_auth(json.dumps(auth).encode())

    def test_non_pid1_refuses_before_credential_read(self):
        with patch('research_harness.model_deployment.os.getpid',return_value=42),patch('research_harness.model_deployment.read_private') as read:
            with self.assertRaises(ContractError):prepare_model(source_sha='a'*40,model='gpt-5.3-codex')
            read.assert_not_called()
