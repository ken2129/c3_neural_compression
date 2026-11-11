"""GPU path CPU fixtures: no SSH credential, Docker access or GPU allocation."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research_harness.gpu_contract import (MAX_RESULT_BYTES, definition_digest,
    installed_target, resident_job_seconds, validate_gpu_request, validate_gpu_result, validate_targets)
from research_harness.gpu_backend import GPUCleanupUnverified, SSHDockerGPUBackend, ssh_argv
from research_harness.gpu_agent import Control, collect_files, validate_recipe
from research_harness.schema import ContractError, canonical


def binding():
    return dict(issue_number=71, issue_content_sha256='a'*64, request_id='approve-71',
                run_id='goal-71-fixture', plan_digest='b'*64, source_sha='c'*40)


def request():
    return dict(machine='fixture', recipe='small', max_seconds=30, max_wait_seconds=5)


def target_profile():
    return dict(gpu_targets=dict(fixture=dict(transport='ssh-docker-stdio-v1',
        ssh_host='gpu.example.test', ssh_user='research', ssh_port=22, container='research-work',
        container_uid=1001, python='/opt/venv/bin/python', agent='/opt/source/tools/research_gpu/agent.py',
        registry='/private/recipes.json', identity_file='/control/gpu-key',
        known_hosts_file='/control/gpu-known-hosts', recipes=dict(small=dict(
            definition_sha256='d'*64,max_seconds=120,max_wait_seconds=60,public_outputs=['public/result.json'])))))


def result(outcome='completed'):
    return dict(schema='c3-gpu-result-v1', binding=binding(), machine='fixture', recipe='small',
        definition_sha256='d'*64, outcome=outcome,
        reason=None if outcome=='completed' else 'CANCELLED', elapsed_seconds=.1, cleanup_verified=True,
        files={'public/result.json':'{"value":7}\n'} if outcome=='completed' else {})


class GPUContractTests(unittest.TestCase):
    def test_gpu_is_opt_in_and_never_increases_cpu_worker_deadline(self):
        with self.assertRaises(ContractError):installed_target({},request())
        self.assertEqual(resident_job_seconds({'max_seconds':600}),600)
        plan=dict(max_seconds=600,gpu_experiment=request())
        self.assertEqual(resident_job_seconds(plan),600+30+5+30)
        self.assertEqual(plan['max_seconds'],600)
        installed_target(target_profile(),request())

    def test_machine_recipe_and_operator_ceiling_are_closed(self):
        for change in ({'machine':'other'},{'recipe':'other'},{'max_seconds':121},
                       {'max_wait_seconds':61},{'argv':['anything']},{'max_seconds':True}):
            with self.subTest(change=change),self.assertRaises(ContractError):
                installed_target(target_profile(),dict(request(),**change))
        for change in ({'ssh_host':'-oProxyCommand=bad'},{'container':'work; touch bad'},
                       {'identity_file':'/work/worker-home/key'},{'ssh_port':False}):
            profile=target_profile();profile['gpu_targets']['fixture'].update(change)
            with self.subTest(change=change),self.assertRaises(ContractError):validate_targets(profile['gpu_targets'])

    def test_ssh_has_fixed_remote_docker_exec_and_no_issue_shell(self):
        target,_=installed_target(target_profile(),request());argv=ssh_argv(target)
        self.assertEqual(argv[0],'/usr/bin/ssh')
        self.assertIn('StrictHostKeyChecking=yes',argv);self.assertIn('ForwardAgent=no',argv)
        self.assertIn('ClearAllForwardings=yes',argv);self.assertIn('ControlMaster=no',argv)
        self.assertIn('docker exec -i --user 1001 research-work',argv[-1])
        self.assertNotIn('docker.sock',str(argv))

    def test_result_is_bound_bounded_and_uses_only_installed_public_paths(self):
        _,definition=installed_target(target_profile(),request())
        validate_gpu_result(result(),binding=binding(),request=request(),definition=definition)
        for mutate in (lambda x:x.update(cleanup_verified=False),
                       lambda x:x.update(definition_sha256='e'*64),
                       lambda x:x['binding'].update(run_id='other'),
                       lambda x:x.update(files={'private.log':'secret'}),
                       lambda x:x['files'].update({'public/result.json':'x'*MAX_RESULT_BYTES}),
                       lambda x:x.update(reason='RUN_TIMEOUT'),
                       lambda x:x.update(elapsed_seconds=float('nan'))):
            value=result();mutate(value)
            with self.assertRaises(ContractError):
                validate_gpu_result(value,binding=binding(),request=request(),definition=definition)

    def test_public_export_rejects_links_and_special_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'public').mkdir();(root/'private').write_text('private')
            (root/'public/result.json').symlink_to(root/'private')
            with self.assertRaises(OSError):collect_files(root,['public/result.json'])

    def test_control_polls_cancel_instead_of_ignoring_zero_timeout(self):
        read,write=os.pipe()
        try:
            incoming=os.fdopen(read,'rb',buffering=0)
            control=Control(incoming,io.BytesIO())
            os.write(write,canonical({'op':'cancel','binding':binding()})+b'\n')
            from research_harness.gpu_agent import AgentStop
            with self.assertRaises(AgentStop):control.check(binding())
            incoming.close()
        finally:os.close(write)


class GPUTransportTests(unittest.TestCase):
    def backend(self, script, *, status=None):
        seen=[]
        def launch(argv,**kwargs):
            seen.append(argv)
            return subprocess.Popen([sys.executable,'-u','-c',script],**kwargs)
        backend=SSHDockerGPUBackend(target_profile(),request(),popen=launch,
            credential_check=lambda target:None,status=status)
        return backend,seen

    def test_real_pipes_return_completed_recipe_without_executing_ssh(self):
        value=result()
        script=("import sys,json\nr=json.loads(sys.stdin.readline())\n"
                "assert r['op']=='execute' and 'argv' not in r\n"
                "print(json.dumps({'op':'status','phase':'waiting_gpu','elapsed_seconds':0}))\n"
                + "print("+repr(json.dumps({'op':'result','result':value}))+ ")\n")
        phases=[];backend,seen=self.backend(script,status=lambda p,e:phases.append(p))
        calls=[];persist=[]
        got=backend.run(binding(),lambda:calls.append(1),on_result=persist.append)
        self.assertEqual(got,value);self.assertEqual(persist,[value]);self.assertGreater(len(calls),1)
        self.assertEqual(phases,['waiting_gpu']);self.assertEqual(len(seen),1)

    def test_revocation_waits_for_verified_cancel_then_preserves_original_error(self):
        value=result('cancelled')
        script=("import sys,json\nr=json.loads(sys.stdin.readline())\n"
                "print(json.dumps({'op':'status','phase':'running','elapsed_seconds':0}))\n"
                "c=json.loads(sys.stdin.readline());assert c=={'op':'cancel','binding':r['binding']}\n"
                + "print("+repr(json.dumps({'op':'result','result':value}))+ ")\n")
        backend,_=self.backend(script);calls=[];persist=[]
        def check():
            calls.append(1)
            if len(calls)>=3:raise ValueError('owner revoked')
        with self.assertRaisesRegex(ValueError,'owner revoked'):
            backend.run(binding(),check,on_result=persist.append)
        self.assertEqual(persist,[value])

    def test_truncation_forged_binding_or_missing_cleanup_is_uncertain(self):
        frames=['private error without a terminal receipt']
        for change in ({'cleanup_verified':False},{'machine':'other'}):
            value=result();value.update(change);frames.append(json.dumps({'op':'result','result':value}))
        for frame in frames:
            backend,_=self.backend("import sys\nsys.stdin.readline()\nprint("+repr(frame)+")")
            with self.subTest(frame=frame[:20]),self.assertRaises(GPUCleanupUnverified) as got:
                backend.run(binding(),lambda:None)
            self.assertEqual(str(got.exception),'GPU_REMOTE_CLEANUP_UNVERIFIED')


if __name__=='__main__':unittest.main()
