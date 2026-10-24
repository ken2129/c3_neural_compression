import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
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
        for name in ('access_token','refresh_token','id_token'):
            for value in ('',None,1):
                bad=copy.deepcopy(auth);bad['tokens'][name]=value
                with self.assertRaisesRegex(ContractError,'^MODEL_DEDICATED_CHATGPT_LOGIN_REQUIRED$'):
                    validate_chatgpt_auth(json.dumps(bad).encode())
        auth['tokens']={}
        with self.assertRaises(ContractError):validate_chatgpt_auth(json.dumps(auth).encode())

    def test_fresh_login_optional_account_id(self):
        auth={'auth_mode':'chatgpt','tokens':dict(access_token='dummy-access',refresh_token='dummy-refresh',id_token='dummy-id')}
        validate_chatgpt_auth(json.dumps(auth).encode())
        auth['tokens']['account_id']=None
        validate_chatgpt_auth(json.dumps(auth).encode())
        for invalid in ('',1,False,{},'x'*32769):
            auth['tokens']['account_id']=invalid
            with self.assertRaises(ContractError):validate_chatgpt_auth(json.dumps(auth).encode())

    def test_non_pid1_refuses_before_credential_read(self):
        with patch('research_harness.model_deployment.os.getpid',return_value=42),patch('research_harness.model_deployment.read_private') as read:
            with self.assertRaises(ContractError):prepare_model(source_sha='a'*40,model='gpt-5.3-codex')
            read.assert_not_called()

    def test_version_probe_side_effects_never_pollute_or_receive_runtime_auth(self):
        # Simulate v0.160 arg0 dispatch, which creates tmp/arg0 even for --version.
        # Ordinary temporary files and fake subprocess/UID operations only.
        for succeeds in (True,False):
            with self.subTest(succeeds=succeeds),tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
                root=Path(directory);work=root/'work';work.mkdir()
                binary=root/'codex';binary.touch()
                protected=root/'protected';protected.mkdir()
                sentinel=protected/'retain';sentinel.write_text('outside probe tree')
                def mapped(path):
                    if path=='/opt/codex/bin/codex':return binary
                    if path=='/work':return work
                    if str(path).startswith('/work/'):return work/str(path)[len('/work/'):]
                    return Path(path)
                auth=json.dumps({'auth_mode':'chatgpt','tokens':dict(
                    access_token='dummy-access',refresh_token='dummy-refresh',id_token='dummy-id')}).encode()
                def fake_read(path,*,limit):
                    return json.dumps(self.config()).encode() if path.name=='model.json' else auth
                def fake_run(argv,**kwargs):
                    env=kwargs['env'];probe=work/'codex-version-probe'
                    self.assertEqual(Path(env['CODEX_HOME']),probe/'codex-home')
                    self.assertEqual(Path(env['HOME']),probe/'home')
                    self.assertEqual(set(env),{'PATH','HOME','CODEX_HOME'})
                    self.assertEqual((kwargs['user'],kwargs['group'],kwargs['extra_groups']),(10002,10002,[]))
                    self.assertFalse((work/'codex-home').exists())
                    self.assertFalse((work/'worker-home').exists())
                    self.assertEqual(list((probe/'codex-home').iterdir()),[])
                    self.assertEqual(list((probe/'home').iterdir()),[])
                    (probe/'codex-home/tmp/arg0').mkdir(parents=True)
                    (probe/'home/cache').mkdir()
                    (probe/'codex-home/outside-link').symlink_to(protected,target_is_directory=True)
                    return SimpleNamespace(returncode=0 if succeeds else 1,
                        stdout=b'codex-cli 0.160.0\n',stderr=b'')
                stack.enter_context(patch('research_harness.model_deployment.Path',side_effect=mapped))
                stack.enter_context(patch('research_harness.model_deployment.os.getpid',return_value=1))
                stack.enter_context(patch('research_harness.model_deployment.os.getresuid',return_value=(0,0,0)))
                uid=stack.enter_context(patch('research_harness.model_deployment.os.seteuid'))
                stack.enter_context(patch('research_harness.model_deployment.read_private',side_effect=fake_read))
                run=stack.enter_context(patch('research_harness.model_deployment.subprocess.run',side_effect=fake_run))
                if succeeds:
                    prepare_model(source_sha='a'*40,model='gpt-5.3-codex')
                    self.assertEqual({p.name for p in (work/'codex-home').iterdir()},{'auth.json'})
                    self.assertEqual((work/'codex-home/auth.json').read_bytes(),auth)
                    self.assertEqual(list((work/'worker-home').iterdir()),[])
                else:
                    with self.assertRaisesRegex(ContractError,'^MODEL_CODEX_VERSION_MISMATCH$'):
                        prepare_model(source_sha='a'*40,model='gpt-5.3-codex')
                    self.assertFalse((work/'codex-home').exists())
                self.assertFalse((work/'codex-version-probe').exists())
                self.assertEqual(sentinel.read_text(),'outside probe tree')
                run.assert_called_once()
                self.assertEqual(uid.call_args.args,(0,))

    def test_version_probe_refuses_existing_tree_without_removing_it(self):
        from research_harness.model_deployment import _probe_codex_version
        with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
            root=Path(directory);probe=root/'probe';probe.mkdir()
            retained=probe/'retain';retained.write_text('not owned by this probe')
            binary=root/'codex';binary.touch()
            stack.enter_context(patch('research_harness.model_deployment.Path',side_effect=lambda path:
                binary if path=='/opt/codex/bin/codex' else probe))
            uid=stack.enter_context(patch('research_harness.model_deployment.os.seteuid'))
            run=stack.enter_context(patch('research_harness.model_deployment.subprocess.run'))
            with self.assertRaises(FileExistsError):_probe_codex_version()
            self.assertEqual(retained.read_text(),'not owned by this probe')
            run.assert_not_called()
            self.assertEqual(uid.call_args.args,(0,))
