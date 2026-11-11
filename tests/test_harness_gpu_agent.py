"""Real Linux pidfd/subreaper fixtures; no actual GPU is visible or queried."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from research_harness.gpu_contract import definition_digest
from research_harness.gpu_agent import validate_recipe
from test_harness_gpu import binding, request, target_profile

ROOT=Path(__file__).resolve().parents[1]
GPU='GPU-'+'a'*32


def registry(root, *, escaped=False, command_fail=False, oversized=False, expanded=False):
    root=Path(root);runs=root/'runs';runs.mkdir(mode=0o700)
    script=root/'fixed_recipe.py'
    script.write_text("import json,os,subprocess,sys,time\nfrom pathlib import Path\nr=Path(sys.argv[1])\n"
        + ("child=subprocess.Popen([sys.executable,'-c','import os,time;os.setsid();time.sleep(60)'])\n"
           "(r/'escaped.pid').write_text(str(child.pid))\n" if escaped else '')
        + ("raise SystemExit(7)\n" if command_fail else '')
        + "(r/'public').mkdir()\n(r/'public/result.json').write_text("
        + repr('x'*17000 if oversized else 'á'*6000 if expanded else '{"value":7}\n')+")\n")
    recipe=dict(argv=[sys.executable,str(script),'{run_dir}'],collect_argv=[],cwd=str(root),environment={},
        source_files={str(script):hashlib.sha256(script.read_bytes()).hexdigest()},gpu_uuids=[GPU],
        limits=dict(max_seconds=120,max_wait_seconds=5,cpu_cores=1,memory_bytes=256*1024**2,
                    max_processes=32,max_artifact_bytes=1024**2,disk_reserve_bytes=1,idle_samples=1),
        public_outputs=['public/result.json'])
    validate_recipe(recipe)
    value=dict(schema='c3-gpu-registry-v1',root=str(runs),recipes=dict(small=recipe))
    path=root/'registry.json';path.write_text(json.dumps(value));path.chmod(0o600)
    return path,recipe


def agent_process(path, mode='idle', **kwargs):
    return subprocess.Popen([sys.executable,'-B',str(ROOT/'tests/gpu_agent_fixture.py'),str(path),mode],**kwargs)


@unittest.skipUnless(sys.platform=='linux' and hasattr(os,'pidfd_open'), 'Linux GPU agent process contract')
class GPUAgentTests(unittest.TestCase):
    def execute(self, path, recipe, *, mode='idle', req=None):
        proc=agent_process(path,mode,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        msg=dict(op='execute',binding=binding(),request=request() if req is None else req,
                 definition_sha256=definition_digest(recipe))
        # Keep stdin open while the recipe runs: EOF intentionally means cancel.
        proc.stdin.write(json.dumps(msg).encode()+b'\n');proc.stdin.flush()
        frames=[]
        while True:
            line=proc.stdout.readline()
            if not line:break
            frame=json.loads(line);frames.append(frame)
            if frame.get('op')=='result':break
        proc.stdin.close();proc.wait(timeout=10)
        error=proc.stderr.read().decode();proc.stdout.close();proc.stderr.close()
        self.assertEqual(proc.returncode,0,error)
        return frames

    def test_success_reaps_escaped_child_without_touching_unrelated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path,recipe=registry(directory,escaped=True)
            sentinel=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
            try:
                frames=self.execute(path,recipe);value=frames[-1]['result']
                self.assertEqual(value['outcome'],'completed');self.assertTrue(value['cleanup_verified'])
                self.assertEqual(json.loads(value['files']['public/result.json']),{'value':7})
                pid=int((Path(directory)/'runs'/binding()['run_id']/'escaped.pid').read_text())
                self.assertFalse(Path('/proc',str(pid)).exists())
                self.assertIsNone(sentinel.poll())
            finally:sentinel.terminate();sentinel.wait(timeout=5)

    def test_busy_gpu_times_out_without_starting_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            path,recipe=registry(directory)
            frames=self.execute(path,recipe,mode='busy',req=dict(request(),max_wait_seconds=0))
            value=frames[-1]['result'];self.assertEqual(value['reason'],'WAIT_TIMEOUT')
            self.assertTrue(value['cleanup_verified'])
            self.assertFalse((Path(directory)/'runs'/binding()['run_id']/'running.log').exists())

    def test_failed_command_and_oversize_export_are_not_success(self):
        for kwargs,reason in (({'command_fail':True},'COMMAND_FAILED'),({'oversized':True},'OUTPUT_REJECTED'),
                              ({'expanded':True},'OUTPUT_REJECTED')):
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as directory:
                path,recipe=registry(directory,**kwargs)
                value=self.execute(path,recipe)[-1]['result']
                self.assertEqual(value['outcome'],'failed');self.assertTrue(value['cleanup_verified'])
                self.assertEqual(value['reason'],reason);self.assertEqual(value['files'],{})

    def test_changed_source_is_rejected_before_recipe_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path,recipe=registry(directory)
            Path(next(iter(recipe['source_files']))).write_text('raise RuntimeError("changed")')
            value=self.execute(path,recipe)[-1]['result']
            self.assertEqual(value['reason'],'INPUT_CHANGED');self.assertEqual(value['files'],{})

    def test_existing_run_directory_is_never_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path,recipe=registry(directory)
            self.execute(path,recipe)
            proc=agent_process(path,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            raw=json.dumps(dict(op='execute',binding=binding(),request=request(),
                                definition_sha256=definition_digest(recipe))).encode()+b'\n'
            out,error=proc.communicate(raw,timeout=10)
            self.assertNotEqual(proc.returncode,0);self.assertNotIn(b'"outcome":"completed"',out)


if __name__=='__main__':unittest.main()
